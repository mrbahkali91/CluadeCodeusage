# Solution Architecture

**Phase:** 0 · **Status:** For review · **Companion documents:**
[diagrams](diagrams.md) · [domain model](domain-model.md) ·
[valuation & scoring](valuation-and-scoring.md) · [agents](agent-architecture.md) ·
[security](../security/security-architecture.md) · [ADRs](../adrs/)

---

## 1. Architectural drivers

The design is driven by five forces, in priority order. Where they conflict, the higher one wins.

1. **Auditability.** Every displayed number must trace to evidence. This forces
   field-level provenance into the *core* data model, not a logging afterthought.
2. **Correctness of money numbers.** Financial arithmetic is deterministic, versioned and
   tested. LLM output can never reach a money field without schema validation.
3. **Source volatility.** Most sources are unverified, some will be replaced, several will
   arrive by partnership months from now. The ingestion boundary must make a source
   swappable without touching the domain.
4. **Small team.** Operational surface is a first-class cost. Every additional runtime is
   a tax paid weekly.
5. **Regulatory posture.** PDPL residency and data minimisation constrain where data lives
   and what may cross a border — including into an LLM provider.

## 2. Shape: modular monolith with extractable seams

One deployable API application plus one worker application, internally partitioned into
modules with enforced boundaries. Not microservices. Rationale and revisit triggers: **ADR-001**.

```
apps/api            FastAPI — HTTP, auth, serialisation. No business logic.
apps/worker         Job runner — ingestion, pipelines, agents, alerts.
apps/web            Next.js — the product surface.

packages/domain     Entities, value objects, invariants. Zero I/O, zero framework imports.
  ├── property/     Property, Unit, Project, Developer, resolution
  ├── valuation/    Comparable selection, fair value, confidence
  ├── cost/         TrueAcquisitionCost line items
  ├── risk/         Risk model
  ├── scoring/      Opportunity score (pure functions)
  └── provenance/   Provenanced[T], Confidence, Evidence
packages/sources    PropertySource port + one adapter per source
packages/agents     Agent runtime, LLM ports, structured-output contracts
packages/persistence SQLAlchemy models, repositories, migrations
packages/schemas    Pydantic contracts shared API↔worker; generates OpenAPI + TS types
packages/ui         React component library
packages/sdk        Generated TypeScript client — the web app never hand-writes a fetch
```

**The boundary that matters most:** `packages/domain` may not import `sources`, `agents`,
`persistence`, or any framework. Enforced by an import-linter rule in CI, not by convention.
This is what keeps the valuation logic testable in milliseconds and portable if the platform
is ever decomposed.

**Extraction seams**, pre-planned for the day load or team size justifies them: document
processing (CPU/GPU-bound, bursty), agent execution (LLM-latency-bound, different scaling
curve), and ingestion (network-bound, isolation valuable). Each already communicates through a
port, so extraction is a deployment change rather than a rewrite.

## 3. Technology decisions

| Concern | Choice | Why | ADR |
|---|---|---|---|
| API | Python 3.12 + FastAPI | The valuation and agent code is Python; splitting languages across the money path would be worse than any framework advantage | — |
| Web | Next.js (App Router) + TypeScript | SSR for the map and list views; strong RTL/i18n story; generated SDK ends type drift | — |
| Primary store | **PostgreSQL 16 + PostGIS** | Geospatial, relational integrity, JSONB for raw payloads, full-text search, `pgvector`, robust statistics in SQL — one engine covers what would otherwise be four | ADR-002 |
| Cache / rate limit | Redis | Session, hot reads, distributed rate limits, idempotency keys | — |
| Search | Postgres FTS + trigram + `pgvector`, Arabic-aware | At MVP scale, a separate cluster buys nothing and costs an operational domain | ADR-005 |
| Object storage | S3-compatible behind a `DocumentStore` port | Auction PDFs; portability across clouds and into a KSA region | — |
| Jobs / orchestration | Postgres-backed durable queue (`FOR UPDATE SKIP LOCKED`) + a `pipeline_runs` DAG table | The evaluation pipeline is a short idempotent DAG; transactional consistency with domain data is worth more than workflow-engine features we would not use yet | ADR-004 |
| Messaging | **None initially.** Postgres queue is the bus | A broker is a failure domain we have not yet earned | ADR-004 |
| Agents | Purpose-built runtime over a thin `LLMProvider` port | Framework abstractions obscure exactly the prompt/response boundary we must audit and cost-control | ADR-003 |
| LLM providers | Provider abstraction: OpenAI, Anthropic, Gemini, local | Residency, cost and capability all argue against a single-vendor commitment | ADR-006 |
| Infra | Docker · Kubernetes · Terraform | Cloud-agnostic; in-Kingdom region requirement is a real possibility | ADR-009 |
| CI/CD | GitLab-compatible pipeline | Per brief | — |
| Observability | OpenTelemetry → Prometheus / Loki / Tempo / Grafana | Vendor-neutral instrumentation | — |
| AuthN/Z | OIDC (Keycloak or managed IdP) + RBAC + org isolation | Enterprise SSO readiness without building identity | — |

### Deliberate omissions

Named so reviewers know they were considered: **no Kafka** (nothing needs a replayable log
yet), **no Elasticsearch** (ADR-005), **no LangChain/LlamaIndex** (ADR-003), **no separate
feature store**, **no data warehouse in MVP** (Postgres read replica serves analytics),
**no service mesh**, **no GraphQL** (REST + generated SDK is simpler and the brief asks for REST).

## 4. Data flow

### 4.1 Ingestion

Every source implements one port:

```python
class PropertySource(Protocol):
    key: str
    legal_access_method: LegalAccessMethod   # required — CI fails without it
    data_license: str

    async def discover(self, since: datetime) -> AsyncIterator[SourceRef]: ...
    async def fetch(self, ref: SourceRef) -> RawRecord: ...
    def normalize(self, raw: RawRecord) -> NormalizedRecord: ...
    def validate(self, rec: NormalizedRecord) -> ValidationResult: ...
    async def health_check(self) -> SourceHealth: ...
```

Guarantees the pipeline enforces regardless of adapter:

- **Raw first.** The payload is hashed and persisted to `source_records` before parsing.
  Extraction is always re-runnable against the original bytes.
- **Idempotent.** `(source_id, external_id, content_hash)` is unique. Re-ingestion with
  unchanged content is a no-op; changed content creates a **new snapshot**.
- **Append-only history.** `listing_snapshots` never updates. A price change is a new row;
  the sequence *is* the price-reduction signal.
- **PII redaction at the boundary.** Phone numbers, national IDs and emails are stripped
  before storage and before any LLM call.
- **Untrusted by default.** All external text is tagged `untrusted` and can only enter a
  prompt inside a delimited data block (see [security](../security/security-architecture.md)).

### 4.2 Evaluation pipeline

A DAG per property, resumable at any node, with each node writing a typed artifact:

```
ingest → normalize → geolocate → resolve_entity → enrich_project
   → select_comparables → value → estimate_rent → compute_true_cost
   → assess_risk → verify → score → [gate] → generate_memo → index → alert
```

**The gate is the cost-control mechanism** and the reason this is affordable. Cheap
deterministic work runs on everything; expensive LLM work runs only on survivors:

| Stage | Runs on | Cost |
|---|---|---|
| Normalise, geolocate, resolve | 100% | ~0 |
| Comparables, valuation, true cost, score | 100% | ~0 (SQL) |
| Structured extraction (small model) | Unstructured input only | low |
| Document intelligence | Documents attached only | medium |
| Verification lookups | Score ≥ 60 **or** user-watched | low |
| Investment memo (large model) | Score ≥ 70 **and** confidence ≥ 0.6 | high |

Consequence: LLM spend concentrates on the few percent of properties that are actually
interesting, and cost per *opportunity* stays flat as ingestion volume grows.

### 4.3 Monitoring loop

The watch agent re-evaluates on schedule and on event. Because snapshots are append-only,
re-evaluation is a pure function of accumulated history — including the derivative signals
that matter most: successive price reductions, an auction deadline approaching, a new
comparable that moves the fair-value band, a bid crossing the user's recommended maximum.

## 5. API design

REST, versioned at `/api/v1`, OpenAPI generated from Pydantic models, TypeScript SDK generated
from OpenAPI in CI. Cursor pagination throughout.

```
GET  /opportunities                       filter, sort, cursor-paginate
GET  /opportunities/{id}
GET  /opportunities/{id}/score            components, weights, weight version
GET  /opportunities/{id}/memo
GET  /opportunities/{id}/provenance
GET  /properties/{id}
GET  /properties/{id}/comparables         the actual comps, with weights
GET  /properties/{id}/valuation
GET  /properties/{id}/timeline
GET  /properties/{id}/documents
GET  /auctions          GET /auctions/{id}
GET  /projects          GET /developers
GET  /market/districts/{id}               price/m², trend, transaction density
POST /search/natural-language             → {filters, explanation, confidence}
GET|POST /watchlists    POST /watchlists/{id}/rules
GET  /alerts            POST /alerts/{id}/ack
POST /submissions                         broker/user opportunity submission
GET  /admin/sources     GET /admin/sources/{id}/health
```

Internal agent and pipeline endpoints are **not** exposed on the public router; they are
worker-internal calls. Nothing an external caller can reach may trigger an unbounded LLM spend.

**Response invariant:** any monetary or derived field serialises as
`{value, unit, confidence, basis, sources[]}` — never a bare number. This is enforced by the
shared schema types, so an endpoint physically cannot return an unattributed price.

## 6. Frontend architecture

Next.js App Router; server components for list/detail reads, client components for map and
filter interaction. MapLibre GL with vector tiles served from PostGIS. Arabic/English with
full RTL mirroring — locale is a routing concern, chosen up front, because retrofitting RTL is
expensive.

Design stance: **institutional, not consumer.** Dense tables, evidence always one click away,
no infinite scroll of pretty photos. Every score badge is a link to its derivation. Risk and
confidence are communicated with icon + label + colour, never colour alone (WCAG 2.1 AA).

## 7. Observability

Traces span the whole pipeline (`ingest → … → alert`) under one trace id carried on the
pipeline run, so a slow or wrong opportunity can be reconstructed end to end.

Metrics that matter beyond the usual RED/USE set:

| Metric | Why it exists |
|---|---|
| `source_freshness_seconds{source}` | The leading indicator of a silently dead connector |
| `duplicate_resolution_rate` | Entity-resolution drift shows here first |
| `valuation_confidence_bucket` | Falling confidence means comp coverage is degrading |
| `llm_cost_usd{agent,model}` | Cost control is a product requirement, not finance's problem |
| `score_distribution` | A sudden shift means a weight or data change, not a market change |
| `alert_precision` (user feedback) | The only true measure of whether the product works |

## 8. Environments and delivery

Local (Docker Compose: Postgres+PostGIS, Redis, MinIO, API, worker, web) → CI (ephemeral) →
staging → production. Every merge runs: format, lint, typecheck, unit, integration
(testcontainers), contract tests, migration up/down, SAST, dependency and container scanning,
IaC scanning. **Migrations are forward-only and reversible; a migration that cannot roll back
does not merge.**

## 9. What would make us revisit this architecture

Stated now so the decision is falsifiable later:

- Sustained ingestion > 10M records/day, or a partner pushing real-time events → introduce a broker.
- Document processing > 30% of worker CPU → extract it as a service.
- A second country with different sources, currency and regulation → domain becomes multi-tenant by jurisdiction.
- Comparable queries exceeding 1s p95 at scale → materialised comparable tiles, then a read model.
- Long-lived multi-day workflows with human approval steps → adopt Temporal per ADR-004's trigger.
