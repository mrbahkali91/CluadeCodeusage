# Slice 1 — "One opportunity, end to end"

**Status: complete and running.** An analyst submits a Riyadh property; the system geolocates
it, selects comparable transactions from PostGIS, time-adjusts them against the live
KAPSARC/GASTAT index, estimates fair value with a confidence, itemises the true acquisition
cost, computes a deterministic opportunity score, and renders a page where every number traces
to its evidence.

```
analyst entry → raw record → property → PostGIS comparables → time adjustment
   → fair value → true cost → discount (or refusal) → risk → score → API → detail page
```

## Verified in this environment

| Check | Result |
|---|---|
| Test suite | **70 passed** |
| `mypy --strict` | **clean, 33 files** |
| `ruff check` | **clean** |
| Architecture contracts (import-linter) | **2 kept, 0 broken** |
| Migration `upgrade head` → `downgrade base` → `upgrade head` | **reversible, 17 tables, 4 GIST indexes** |
| Live KAPSARC index pull | **312 index points, HTTP 200** |
| API + UI | **all endpoints 200, pages render** |

## What it computes (worked example, from the running system)

Assignment (تنازل), 3BR apartment, Sidrah, 140 m²:

| | |
|---|---|
| Seller payment | SAR 120,000 |
| Remaining developer installments | SAR 600,000 |
| **True acquisition cost** | **SAR 720,000** |
| Estimated market value | **SAR 894,663** (band 689,051 – 1,188,066) |
| Price / m² | SAR 6,390 |
| **Discount** | **19.5%** |
| Comparables | 11 used, effective n 5.99 |
| Opportunity score | 65.1 — Watchlist, data confidence 63% |

**The same property with installments undisclosed:** a listings site would show the SAR 120,000
ask against a SAR 894,663 valuation — an apparent **86.6% discount**. This system refuses:

> *Discount refused — remaining installments unknown, required before a discount can be computed.*

and the confidence gate drops the classification to `INSUFFICIENT_DATA`. That refusal is
enforced by the domain type and a database constraint, not by a code-review convention.

## Two honest findings from building it

**1. Three of the four demonstration opportunities read `INSUFFICIENT_DATA`.** This is the
system working, not failing. The verification agent is Slice 4, so `verification_score` is
structurally 0.0 and costs 0.20 of the confidence formula outright; thin comparable evidence
supplies the rest of the shortfall. I deliberately did **not** re-normalise the formula to make
the demo look better — that would be gaming the confidence gate, which is the one mechanism
protecting the product's credibility. Instead the UI now names exactly what is holding
confidence down, ranked by shortfall. Expect most opportunities to become actionable when
Slice 4 lands.

**2. The comparable corpus is synthetic and labelled as such everywhere.** Assumption A-01
(transaction-level Ministry of Justice open data) is still unvalidated, so there is no real
transaction data to build on. Rather than stall, the engine runs on a deterministic synthetic
corpus registered under a source flagged `is_synthetic = True`. The API exposes
`is_synthetic_evidence`, and the detail page carries a banner. **No path exists by which
generated transactions can be presented as real registered sales** — that failure mode is
exactly what this product exists to prevent.

## Running it

```bash
make install                 # uv venv + dev extras
make migrate                 # alembic upgrade head  (needs PostgreSQL 16 + PostGIS)
make reset-demo              # seed + ingest four demonstration opportunities
make run                     # http://127.0.0.1:8000
make check                   # lint + typecheck + contracts + tests
```

`SREOI_DATABASE_URL` defaults to `postgresql+psycopg://sreoi:sreoi@127.0.0.1:5432/sreoi`.

## Layout

```
src/sreoi_domain/       pure: provenance, stats, valuation, cost, risk, scoring   (no I/O)
src/sreoi_sources/      PropertySource port, KAPSARC connector, manual entry, PII redaction
src/sreoi_persistence/  SQLAlchemy models, PostGIS repositories, sessions
src/sreoi_pipeline/     evaluation DAG, ingestion, seed, CLI
src/sreoi_api/          FastAPI routes, read models, Jinja templates
migrations/             Alembic (reversible)
tests/                  70 tests; DB-backed ones skip cleanly without PostgreSQL
```

The `domain` package may not import SQLAlchemy, FastAPI, httpx, or any sibling package.
`make contracts` fails the build if that is violated (ADR-001).

## Deviations from the Phase 0 documents

Recorded so they are visible, not silently absorbed.

| Deviation | Why |
|---|---|
| One installable distribution with four layered packages, rather than separate distributions per package | The guarantee that matters is the enforced boundary, which import-linter provides either way. Fewer moving parts for a slice. |
| A `sreoi_pipeline` layer was added between persistence and API | The architecture said the API holds no business logic. The evaluation DAG needed somewhere to live that was not the HTTP layer. |
| `docker-compose.yml` and `Dockerfile` are written but **untested** | No Docker daemon in the build environment; PostGIS was installed natively instead. |
| Rental/yield scores 0.0 | The rental engine is Slice 3. It is weighted at 0.15 and scores zero rather than being dropped, because dropping it would silently re-weight every other dimension upward. |
| Verification score 0.0 | The verification agent is Slice 4. See finding 1. |

## Next slice

Slice 2 — "Many opportunities, ranked and mapped": entity resolution, property timeline, the
opportunity list and map, source-health dashboard, and AR/EN RTL. But the **Phase 0 gate
(Assumption A-01) still has not been cleared**, and it needs a Saudi-resident network to test.
Until it is, every valuation in this system rests on synthetic evidence.
