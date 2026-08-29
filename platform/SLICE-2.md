# Slice 2 — "Many opportunities, ranked and mapped"

**Status: complete and running.** Fifty-six Riyadh opportunities are deduplicated across
sources, ranked with working filters, plotted on a PostGIS-backed map, carry a full
append-only timeline, and render in Arabic and English with real RTL. Source health is
monitored on an admin dashboard.

## Verified in this environment

| Check | Result |
|---|---|
| Test suite | **108 passed** (70 → 108) |
| `mypy --strict` | **clean, 43 files** |
| `ruff` | **clean** |
| Architecture contracts | **2 kept, 0 broken** |
| Migrations | **reversible** — 20 tables up, 0 down, 20 back up; `alembic check` clean |
| Corpus | 56 properties · 64 listings · 81 snapshots · 380 timeline events · 25 auto-merges |

## What it does now

**Entity resolution.** The same unit advertised on two sources resolves to one property.
Blocking (same class, ±8% area, 500 m) then a weighted score over spatial distance, area,
project/unit, attribute agreement, price and pg_trgm text similarity. Above 0.85 merges
automatically, 0.60–0.85 goes to a human, below stays separate. Every decision is stored with
its components and is reversible.

**Append-only history.** A price change inserts a snapshot, never an update, and emits a
timeline event. `950k → 920k → 875k → urgent` is retained as a sequence because the sequence is
the signal. Re-observing a listing re-evaluates the existing opportunity instead of creating a
duplicate, and supersedes the previous score rather than overwriting it.

**Filtered search and map.** District, type, max acquisition cost, minimum discount, minimum
score, bounding box, radius and sort — all executed in PostgreSQL, all echoed back to the user
so the applied filters stay visible. The map and the list share one query, so they cannot
disagree.

**Source health.** Per-source state (HEALTHY / STALE / FAILING / UNKNOWN / DISABLED), latency,
record counts, freshness budget, and the legal basis and licence for every source on the same
screen.

**Arabic and English.** Locale routing, full RTL mirroring, Arabic-Indic numerals, translated
enums and district names.

### The brief's acceptance query works

> "Monitor apartments around Qurtubah, Sidrah, Al Munsiyah and Al Rimal. Maximum total
> acquisition cost SAR 1.2M. Alert me at ≥15% below estimated market value."

```
GET /api/v1/search/opportunities
    ?district=Qurtubah&district=Sidrah&district=Al Munsiyah&district=Al Rimal
    &max_cost=1200000&min_discount=15&hide_insufficient=true
→ 13 matches, ranked, with the filters echoed back
```

Alerting itself is Slice 3; the query that drives it is live.

## Five defects found and fixed while building

Recorded because each was a real bug, not a tuning preference.

1. **Entity resolution ignored unit numbers unless a project id also matched.** With no project
   registry yet, the unit number plus the spatial block is often the strongest evidence we have.
   Two listings for the same unit scored 0.818 and went to review instead of merging.
2. **Two units at one address with *different* unit numbers auto-merged at 0.853.** A weighted
   sum cannot express a veto, and merging two different doors corrupts the comparable evidence
   the whole product rests on. Identifier conflicts (unit number, project) now block an
   automatic merge and route to a human — but notation differences (`B-402` / `b 402` / `402`)
   still merge, so the fix does not cause silent splits either.
3. **List filters were silently ignored.** `district=Sidrah` returned all 56 rows: FastAPI needs
   an explicit `Query()` on list parameters inside a dependency. A filter that quietly does
   nothing is worse than one that errors.
4. **The test suite destroyed the development database.** The session fixture drops and
   recreates the schema and defaulted to the same database as `make run`. Tests now derive a
   sibling `_test` database and create it on first run.
5. **The map depended on two third-party services** — a CDN for MapLibre and OpenStreetMap for
   tiles — and rendered blank whenever either was unreachable. Both are now gone: MapLibre is
   vendored, and the base layer is our own district geometry from PostGIS. Beyond reliability
   this matters for posture: a foreign tile server would otherwise observe every viewport a user
   pans to, which is a signal about their investment interest.

Two smaller ones: an unnamed self-referential foreign key broke `alembic downgrade`, and Jinja
autoescaping corrupted a value interpolated into JavaScript (`'name_en'` → `&#39;name_en&#39;`),
so the map template no longer templates anything into script at all.

## Still honest about what this is

**Twenty-eight of fifty-six opportunities read `INSUFFICIENT_DATA`, and none reach "Strong".**
That is the confidence gate working. The verification agent is Slice 4, so `verification_score`
is structurally 0.0 and costs 0.20 of the confidence formula outright. Re-normalising to make
the demo look better would defeat the one mechanism protecting the product's credibility, so it
has not been done.

**The comparable corpus and the opportunity corpus are both synthetic**, labelled at the source,
in the API (`is_synthetic_evidence`) and on the page. Assumption A-01 remains unvalidated.

## Running it

```bash
make db-up && make migrate
make seed && make corpus        # districts, live KAPSARC index, synthetic corpus, 56 opportunities
make run                        # http://127.0.0.1:8000
make check                      # lint + typecheck + contracts + tests
```

## Deferred to later slices

Watchlists and alerting (Slice 3), rental and yield (Slice 3), the verification agent and
investment memo (Slice 4), polygon-draw on the map, clustering at higher point volumes, and a
constraint naming convention before the first real deployment.
