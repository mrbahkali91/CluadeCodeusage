# Saudi Real Estate Opportunity Intelligence — Phase 0

> Working name. The product name is treated as configuration (`PLATFORM_NAME`), not a hard-coded string.

This directory holds the **Phase 0 (Discovery)** deliverables for a Saudi real-estate
opportunity-intelligence platform. No production code is written yet, by design: the
single largest risk to this product is not engineering, it is **whether the data that makes
"discount vs. market" a defensible claim is lawfully obtainable at transaction-level
granularity**. Phase 0 answers that before we spend engineering budget.

It lives under `platform/` rather than the repository root `docs/` because that directory is
the VitePress site for the unrelated `ccusage` tooling already in this monorepo; putting
these documents there would break its build and confuse two products.

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
