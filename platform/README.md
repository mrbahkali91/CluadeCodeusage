# Saudi Real Estate Opportunity Intelligence

**Phase 0 (Discovery) — complete.**
**Slice 1 — complete:** [SLICE-1.md](SLICE-1.md) (analyst entry → valuation → true cost → score).
**Slice 2 — complete:** [SLICE-2.md](SLICE-2.md) (dedupe, timeline, filters, map, health, AR/EN).
**Slice 4a — complete:** [SLICE-4.md](SLICE-4.md) (agent runtime, provider abstraction, injection defences, verification agent).

> Working name. The product name is treated as configuration (`PLATFORM_NAME`), not a hard-coded string.

This directory holds the **Phase 0 (Discovery)** deliverables for a Saudi real-estate
opportunity-intelligence platform. No production code is written yet, by design: the
single largest risk to this product is not engineering, it is **whether the data that makes
"discount vs. market" a defensible claim is lawfully obtainable at transaction-level
granularity**. Phase 0 answers that before we spend engineering budget.

It lives under `platform/` rather than the repository root `docs/` because that directory is
the VitePress site for the unrelated `ccusage` tooling already in this monorepo; putting
these documents there would break its build and confuse two products.

## Running code

Slice 1 takes an analyst submission through comparables, valuation, true acquisition cost and a
deterministic opportunity score to an evidence-backed detail page. Slice 2 adds entity
resolution across sources, append-only price history, filtered search, a PostGIS map, source
health monitoring and full Arabic/RTL.

Slice 4 adds the agent runtime, prompt-injection defences and verification. The auth slice
adds authentication, RBAC and PostgreSQL-enforced tenant isolation.

**512 tests (order-independent), `mypy --strict` clean over 96 files, reversible migrations,
enforced architecture boundaries.** Start at **[SLICE-1.md](SLICE-1.md)**, then
**[SLICE-2.md](SLICE-2.md)**, **[SLICE-4.md](SLICE-4.md)** and **[SLICE-AUTH.md](SLICE-AUTH.md)**.

## Running it on a laptop

**No Docker required.** One script brings up all three tiers:

```bash
git clone -b claude/saudi-realestate-opportunity-platform-n0bn70 <repo-url>
cd CluadeCodeusage
./run-local.sh
```

Then open <http://127.0.0.1:5173> and sign in as `admin@localhost`; the password
is printed once during startup. Ctrl-C stops everything.

| Tier | Port | Owns |
|---|---|---|
| Python engine | 8000 | valuation, scoring, credentials |
| NestJS API | 3000 | auth, tenancy, queries |
| React client | 5173 | **open this one** |

You need PostgreSQL 16 with PostGIS, Python 3.11+, Node 20+, pnpm and bun.
`./run-local.sh --check` verifies all of them, changes nothing, and prints the
exact install command for whatever is missing on your platform.

```bash
./run-local.sh --check       # verify prerequisites only
./run-local.sh --reset       # rebuild the schema from migrations first
./run-local.sh --no-client   # engine + API only
ADMIN_PASSWORD=secret ./run-local.sh   # choose the password instead of generating one
```

The script provisions what it needs and is safe to re-run: it creates the
application role and both databases if absent, installs the extensions,
migrates, seeds, and bootstraps an organisation and first user. It refuses to
start if any of the three ports is already in use — a health check cannot tell
this run's server from a stale one, and adopting a stranger's process means
verifying the whole stack against the wrong build.

Docker is still available if you prefer it (`docker compose up -d`), but it is
no longer the recommended path. The compose orchestration has never been
executed; the native script above has.

Two things to know before you run the tests:

Two things to know before you run the tests:

- `make check` is the full gate: lint, `mypy --strict`, the architecture contracts, and the
  suite with `SREOI_REQUIRE_DB=1`. That flag turns a missing test database into a hard
  failure. Use it. Plain `make test` *skips* the 209 database-backed tests when PostGIS is
  unreachable — including every tenant-isolation test — and a green run with the security
  tests silently skipped is the most dangerous output this repo can produce.
- The application database role is deliberately **not** a superuser. PostgreSQL exempts
  superusers from row-level security unconditionally, so a superuser app role would leave
  all sixteen tenant policies in place and enforced against nobody. Both the Docker init
  script and `deploy-local.sh` create it `NOSUPERUSER NOBYPASSRLS` and have a superuser
  install the extensions. If you provision the database by hand, do the same.

## Read in this order

| # | Document | What it answers |
|---|---|---|
| 1 | [product/prd.md](docs/product/prd.md) | What we are building, for whom, why it is not a listings portal, what is explicitly out of scope |
| 2 | [data-sources/matrix.md](docs/data-sources/matrix.md) | Which Saudi sources exist, what is lawfully ingestible, and what is **not** |
| 3 | [data-sources/verification-log.md](docs/data-sources/verification-log.md) | Exactly what was probed, what responded, and what remains unverified |
| 4 | [architecture/solution-architecture.md](docs/architecture/solution-architecture.md) | System design, containers, technology choices |
| 5 | [architecture/diagrams.md](docs/architecture/diagrams.md) | System context, containers, ingestion, agent workflow, evaluation, deployment |
| 6 | [architecture/domain-model.md](docs/architecture/domain-model.md) | Domain model and database ERD |
| 7 | [architecture/valuation-and-scoring.md](docs/architecture/valuation-and-scoring.md) | The core IP: comparables, fair value, true cost, opportunity score — all deterministic |
| 8 | [architecture/agent-architecture.md](docs/architecture/agent-architecture.md) | Where LLMs are used, where they are banned, and how they are constrained |
| 9 | [security/security-architecture.md](docs/security/security-architecture.md) | Threat model, PDPL posture, prompt-injection defence |
| 10 | [adrs/](docs/adrs/) | ADR-001 … ADR-010 |
| 11 | [product/mvp-backlog.md](docs/product/mvp-backlog.md) | Epics → features → stories → acceptance criteria |
| 12 | [delivery/plan.md](docs/delivery/plan.md) | Vertical slices, milestones, Definition of Done |
| 13 | [product/risk-register.md](docs/product/risk-register.md) | Top 20 risks, top 10 assumptions to validate before launch |

## The three things a reviewer should take away

1. **The crawler is not the moat, and mostly cannot legally exist.** Most Saudi listing
   portals (Aqar, Haraj, Bayut, Wasalt) cannot be ingested without a commercial agreement.
   Third-party scrapers for them exist and are *explicitly rejected* here. The defensible
   asset is the **valuation + opportunity graph** built on official transaction data plus
   partner and user-submitted opportunities.
2. **One assumption gates the entire business case.** If transaction-level MOJ sale records
   are not obtainable under the KSA Open Data License at usable granularity, the phrase
   "19% below market" is unsupportable and the product reduces to a listings aggregator.
   This is Assumption A-01 and it must be validated in week 1, before Phase 1.
3. **Agentic where it earns its place, deterministic everywhere it matters.** Money numbers —
   price/m², fair value, true acquisition cost, yield, discount, opportunity score — are
   computed by reproducible code with unit tests. LLMs extract, reconcile, verify and
   explain. An LLM is never permitted to originate a price.
