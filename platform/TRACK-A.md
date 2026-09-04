# Track A (Slice 3) — Rental estimate & yield, watchlists, alerting

Status: **working end to end.** 52 new tests (26 rental, 26 alerting), all green, order-independent
under `pytest-randomly`. `ruff check`, `ruff format --check` and `mypy --strict` clean over every
file in this track. Pre-existing suites re-run because this track edits `evaluate.py`:
`test_pipeline_integration` (11), `test_verification` (12), `test_api`, `test_search_api`,
`test_slice2_integration`, `test_scoring` — all pass.

No Alembic migration was authored (as instructed). One head, `1072e9b65dd2`. The eight new tables
reach a database through `Base.metadata.create_all`; the coordinator writes the consolidated
migration.

---

## 1. What works

### Rental estimate and yield
`sreoi_domain/rental.py` — pure, no I/O imports, no LLM.

* `estimate_rent(subject, comps, as_of=..., subject_completeness=...) -> RentalEstimate`
* `compute_yield(annual_rent=..., true_acquisition_cost=..., cost_is_complete=..., assumptions=...) -> RentalYield | YieldRefused`
* Refusal: `InsufficientRentalEvidenceError` when Kish effective *n* < 3, mirroring
  `InsufficientComparablesError`. We do not extrapolate.

The kernels are **reused, not copied**. `RentalComparable.as_similarity_subject()` presents a lease
to `valuation.compute_weight` as a sale comparable whose `price` is the annual rent; the seven
kernels read only distance, date, area, age and floor, so the weights are identical by
construction. `test_rental_weights_are_the_valuation_kernels_not_a_copy` asserts equality of both
the weight and its breakdown, so a future change to the kernels cannot silently apply to one engine
and not the other. `weighted_median`, `weighted_quantile`, `effective_sample_size` and `iqr_bounds`
come from `sreoi_domain.stats` unchanged.

### Rental comparables
`models_rental.py` + `pipeline/rental.py`. PostGIS `Geography POINT`, `annual_rent`, `area_sqm`,
`contract_date`, `property_class`, `district_id`, plus `build_year`/`floor`/`project_id` for kernel
parity and `ingested_at` (see §4). Expanding-radius selection reuses `ComparableRepository`'s own
`RADIUS_STEPS_M`, `LOOKBACK_MONTHS` and area band constants rather than restating them, so a rental
estimate and a fair value are never drawn from differently-shaped neighbourhoods.

### Watchlists and alerting
`models_alerts.py` (`watchlists`, `watch_rules`, `alerts`, `notifications`, `alert_feedback`),
`pipeline/alerts.py`, `routers/watchlists.py`, `templates/watchlists.html`.

### Acceptance scenario
> "Monitor apartments around Qurtubah, Sidrah, Al Munsiyah and Al Rimal in Riyadh. Maximum total
> acquisition cost SAR 1.2M. Alert me when an Infath auction, resale, assignment, urgent sale or
> developer unit appears at least 15% below estimated market value."

`test_acceptance_scenario_fires_exactly_one_alert_with_a_stated_reason` builds that rule verbatim
against a four-opportunity graph and asserts **exactly one** alert, on the right opportunity, with a
reason naming the user's own conditions:

```
new match for '15% below market, four districts, under SAR 1.2M':
  19.3% below estimated market value; true acquisition cost SAR 720,000; score 77.9
```

Each of the other three fails exactly one clause, so a matcher bug surfaces as a specific wrong row:

| Fixture | Fails on | Why it is the interesting case |
|---|---|---|
| `thin_discount` (Al Rimal, SAR 860k) | discount ~1% | priced at market |
| `over_budget` (Qurtubah 240 m², SAR 1.3M) | budget | a genuine 23% discount, still over 1.2M |
| `unknown_cost` (Al Munsiyah, balance undisclosed) | cost completeness | **recorded** total is SAR 200k — naively the cheapest thing on the market |

---

## 2. Exact formulas and defaults

```
annual_rent  = WeightedMedian({rent_per_sqm_year(c)}, {w(c)}) × area_p
low          = WeightedQuantile(0.25) / spread × area_p
high         = WeightedQuantile(0.75) × spread × area_p
spread       = 1 + 0.6 / sqrt(n_effective)

gross_yield  = annual_rent / true_acquisition_cost
EGI          = annual_rent × occupancy
opex         = service_charges + management_fraction × EGI
                              + maintenance_reserve_fraction × annual_rent
net_yield    = (EGI − opex) / true_acquisition_cost
```

Defaults (`RentalAssumptions`, all overridable, all returned by `describe()`):

| Assumption | Default | Basis of the percentage |
|---|---|---|
| `occupancy` | **0.92** | — |
| `management_fraction` | **0.08** | **effective gross income** (rent actually collected) |
| `maintenance_reserve_fraction` | **0.05** | **gross annual rent** |
| `annual_service_charges` | **None → treated as 0, and flagged** | absolute SAR/year |

"8% management" is ambiguous until you say 8% of what, so both bases are named explicitly in
`describe()` and stored on the row. `annual_service_charges=None` is deliberately distinct from a
user asserting zero: `describe()` returns `annual_service_charges_assumed_zero: true`, so the UI can
show that we guessed. Every assumption is written into `rental_estimates.assumptions` **alongside the
numbers**, not read from configuration at display time, so a stored yield stays reproducible after a
default changes.

Worked check against spec §6 (Sidrah, 140 m², assignment, cost SAR 720,000):

| | spec §6 | this implementation, synthetic corpus |
|---|---|---|
| fair value | 910,000 | 892,253 |
| discount | 20.9% | 19.3% |
| annual rent | 61,000 | 68,065 |
| gross yield | 8.5% | 9.45% |
| net yield | — | 7.53% |

### Two calibration decisions, and why

**`MIN_RENTAL_COMPARABLES = 12`, not the valuation engine's 8.** Rental evidence is thinner and more
dispersed (furnished lets, related-party leases, shorter terms). Measured across all four districts
× four subject areas, an eight-lease target stops the search at 750 m, yields effective *n* of
2.2–3.5, and **refuses the estimate about a third of the time**. Twelve pushes the search one radius
step out and lifts effective *n* to 3.6–11.4 with confidence 0.40–0.73. The refusal floor was **not**
relaxed to compensate — effective *n* < 3 still refuses.

**Rental confidence drops the index term.** Spec §2.3 allocates 0.15 to `index_quality`. No rental
price index exists in any CONFIRMED source, so scoring it as 1.0 would award confidence we have not
earned. That 0.15 is redistributed:

```
confidence = 0.45·min(1, n_eff/12) + 0.30·mean(w) + 0.15·agreement + 0.10·subject_completeness
```

This is a documented **adaptation** of the spec, not an implementation of it. It should be reviewed.

### Yield is refused, not estimated, on an incomplete cost
`compute_yield` returns `YieldRefused` when `cost_is_complete` is false. This extends the §3
invariant to the yield for the same reason it exists for the discount: an incomplete cost is a
too-small denominator, so a partial cost *flatters* the yield — the dangerous direction. The
Sidrah unit with undisclosed installments would show ~57% gross. The row is still written, with a
stated rent, `gross_yield = NULL` and `yield_refused_reason` populated — NULL rather than 0, because
0 asserts a fact.

---

## 3. Matcher / search equivalence

`sreoi_pipeline.alerts._matching_statement` mirrors `sreoi_api.search._base_query` / `_apply` clause
for clause, including the non-obvious one: **a budget may only be compared against a complete cost**
(`cost.is_complete IS TRUE AND cost.total <= :max`). `test_matcher_agrees_with_the_search_read_model`
parametrises seven rule shapes and asserts the matched opportunity-id set equals
`sreoi_api.search.search()`'s, against the real implementation rather than a restatement of it.

The duplication exists because `.importlinter` forbids `sreoi_pipeline` importing `sreoi_api`, so the
matcher cannot call `search()`. **This is a seam the coordinator should close** by moving the read
model down a layer; until then the equivalence test is the only thing preventing drift.

Two documented divergences:
* `min_gross_yield` is an **extension** — `sreoi_api.search` has no yield filter yet, so equivalence
  is asserted only over the shared filter set.
* `polygon` uses `ST_Intersects(Property.location, <polygon>)`, matching `search`'s bbox behaviour
  (`search` has no polygon filter).

---

## 4. Triggers (PRD §5.9)

| Trigger | Fires when | Dedupe discriminator |
|---|---|---|
| `NEW_OPPORTUNITY` | the opportunity satisfies the filter and this rule version has never alerted on it | *(empty — at most once per rule version)* |
| `PRICE_REDUCTION` | latest listing snapshot is below the previous one | snapshot id — each genuine cut alerts once |
| `SCORE_THRESHOLD_CROSSED` | current score ≥ `min_score` **and** the immediately prior score row was below it | score row id |
| `NEW_COMPARABLE` | relevant leases with `ingested_at > rule.last_evaluated_at` | the watermark |
| `AUCTION_DEADLINE` | **stub, explicit `TODO(slice-5)`** — named, wired, never fires | — |

`AUCTION_DEADLINE` cannot be built yet: `opportunities` has no auction window and no bid ladder, and
inventing a deadline from ingestion time would produce alerts about a date nobody published.
`test_auction_deadline_trigger_is_wired_but_deliberately_silent` pins that it is a stub, not a
forgotten branch.

`NEW_COMPARABLE` covers **rental** comparables only. `transactions` has no ingestion timestamp — only
the transaction date — so "new to us" is not answerable for sale evidence without a column on
`models.py`, which this track does not own.

### Deduplication
`dedupe_key = sha256(rule_id | rule_version | opportunity_id | trigger | discriminator)`, pre-checked
by SELECT, inserted inside a savepoint, and backed by `uq_alert_dedupe_key`. So a duplicate is
refused by the **database**, not by application discipline, even under a concurrent scan. Tested:
three consecutive scans of an unchanged rule produce 1 / 0 / 0 alerts.

Rule version is in the key deliberately: editing a rule re-opens alerting, because the user asked a
new question. A rewritten *reason string* does not change identity, so improving the wording cannot
re-alert the back catalogue (`test_dedupe_key_is_stable_and_distinguishes_reason_and_version`).

### Channels
`NotificationChannel` Protocol → `InAppChannel` (persisted, `DELIVERED`) and `LoggingEmailChannel`
(`LOGGED_NOT_SENT`, detail `"no email transport configured in this slice: logged only, NOT sent"`). A
stub recording `DELIVERED` would be a lie an operator discovers only when a user misses an auction.
An unregistered channel records `FAILED` rather than being silently dropped.

---

## 5. Synthetic data labelling

The rental corpus is registered under source `synthetic_rental_fixture`, `is_synthetic=True`,
`source_confidence=0.35`, licence:

> `SYNTHETIC - generated fixture, NOT real lease or Ejar contract data. Replaced by a licensed
> rental-contract feed before any yield is presented to a user as market evidence.`

The generated corpus is itself a `source_records` row with a manifest (generator, seed, per-district
count, unit), so it appears on the admin health dashboard like any other source.
`test_synthetic_rental_corpus_is_labelled_synthetic` asserts the flag, the licence text, and that
**no** rental comparable hangs off any other source.

40 leases per district × 4 Riyadh districts = 160, seed `20260903`, observed range **350–603
SAR/m²/year** (clamped to 350–650). District bases 505 / 475 / 440 / 400 (Qurtubah / Sidrah /
Al Munsiyah / Al Rimal) are set against `seed._BASE_PPSQM` so implied yields on market value land
near 7%, the order of magnitude in spec §6.

---

## 6. Files

Added:
```
src/sreoi_domain/rental.py                  pure rent + yield engine
src/sreoi_persistence/models_rental.py      rental_comparables, rental_estimates,
                                            rental_estimate_comparables
src/sreoi_persistence/models_alerts.py      watchlists, watch_rules, alerts,
                                            notifications, alert_feedback
src/sreoi_pipeline/rental.py                PostGIS selection, synthetic corpus, persistence
src/sreoi_pipeline/alerts.py                matching, triggers, dedupe, channels, dispatch
src/sreoi_api/routers/watchlists.py         API + UI + its own i18n strings
src/sreoi_api/templates/watchlists.html     AR/EN, RTL-aware
tests/test_rental.py                        26 tests
tests/test_alerts.py                        26 tests
TRACK-A.md                                  this file
```

Edited — **`src/sreoi_pipeline/evaluate.py` only, 14 insertions / 1 deletion:**
one import, one call to `estimate_and_store_rent(...)`, and
`gross_yield=None,  # rental engine lands in Slice 3` → `gross_yield=rental.gross_yield if rental is not None else None`.

Structural invariants added: `ck_alert_has_reason` (`btrim(reason) <> ''` — an alert that cannot say
why it fired is noise), `uq_alert_dedupe_key`, `uq_notification_alert_channel`,
`ck_alert_feedback_action`, `ck_watch_rule_version_positive`, positive-area/rent checks on
`rental_comparables`. Both CHECK constraints are covered by tests that expect `IntegrityError`.

---

## 7. Defects and observations in existing code

1. **`python-multipart` is not installed, and a `Form` route breaks the entire app at import.**
   Router discovery runs at `sreoi_api.main` import time, so a single `Annotated[str, Form()]`
   parameter in *any* router raises `RuntimeError` and takes down `test_api`, `test_search_api` and
   every UI route with it. Found by adding one; the watchlist page now posts JSON via `fetch()`
   instead, needing no new dependency. Worth a guard in CI — the failure is total and non-obvious.
   (Note: another track has `pyproject.toml` modified in the working tree; if it adds
   `python-multipart`, this constraint relaxes, but the fragility remains.)
2. **`tests/conftest.py::_MUTABLE_TABLES` is a hand-maintained list** that no new feature can extend
   without editing a shared file. It happens to work here only because `TRUNCATE ... CASCADE` on
   `opportunities` reaches `alerts` and `rental_estimates` through their foreign keys. `watchlists`
   and `watch_rules` are *not* reached, and would leak across tests if created outside a rolled-back
   session. My fixtures create them inside the rolled-back `session` so nothing leaks, but the list
   should become discovery-based like `model_modules.load_all()`.
3. **`RentalComparableRepository` lives in `sreoi_pipeline/rental.py`, not
   `sreoi_persistence/repositories.py`,** purely because that file is shared with concurrent work.
   It belongs beside `ComparableRepository`; please move it on integration.
4. `HTTP_422_UNPROCESSABLE_ENTITY` is deprecated in the installed Starlette. Pre-existing in
   `main.py`; this track matches the surrounding style rather than diverging.

## 8. Deliberately not done

* **No Alembic migration** (per instruction). Eight tables await the consolidated one.
* **No `seed_all` / CLI wiring for the rental corpus.** `seed.py` and `cli.py` are not owned by this
  track. `seed_rental_comparables(session)` is public, idempotent and ready to call; **without it the
  yield component scores 0 for every opportunity**, so the coordinator must add it to `seed_all`
  (and, ideally, `cli.py corpus`). Tests call it directly. This is the single most important
  integration follow-up.
* **No display of the rental estimate on the detail page.** `queries.py`, `schemas.py` and
  `detail.html` are owned elsewhere, so the numbers are stored and served over
  `/api/v1/watchlists/...` and the alert payloads, but the opportunity detail page does not yet show
  rent, yield or the assumption list. Every field the UI needs is on `rental_estimates` and
  `rental_estimate_comparables`, including the cited leases with their weights.
* **No nav link to `/watchlists`.** `templates/base.html` is off-limits; the page renders correctly
  in both locales but is only reachable by URL. One line in `base.html` fixes it — the string
  `nav.watchlists` is already registered in both locales.
* **No score re-computation when a rental estimate arrives late.** The estimate is produced inside
  `evaluate_opportunity`, so a property ingested before the rental corpus existed keeps a score with
  a zero rental component until it is re-ingested. A re-score pass belongs with E5.6.
* **No auction-window triggers** (§4 above), and no digest-vs-alert materiality routing (that is
  E7.9, the watch agent, and is agentic by design).
* **`rental_estimates` has no `superseded_at`.** Unlike `opportunity_scores` it relies on
  latest-by-`computed_at`. Consistent with `valuations`, which does the same.
