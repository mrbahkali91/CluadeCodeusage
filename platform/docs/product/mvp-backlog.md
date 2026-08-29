# MVP Backlog

Format: **Epic → Feature → User story → Acceptance criteria → Priority → Dependencies**.
Priorities: **P0** must ship for MVP · **P1** ships if the slice completes · **P2** post-MVP.

Estimates are relative (S ≈ 1–2 days, M ≈ 3–5 days, L ≈ 1–2 weeks) for one full-stack engineer.

---

## E0 — Discovery validation *(blocks everything; no code)*

| ID | Feature | Acceptance criteria | Pri | Est | Depends |
|---|---|---|---|---|---|
| E0.1 | **Validate MOJ transaction open data (A-01)** | From a KSA egress, download at least two quarterly resources; confirm transaction-level rows with price, area, district, date; document schema and record counts; obtain licence text | **P0** | M | — |
| E0.2 | Counsel review of KSA Open Data License for commercial derivative use (A-02) | Written opinion on file | **P0** | M | E0.1 |
| E0.3 | Counsel opinion: are we a regulated advertiser if we link to third-party listings? (A-04) | Written opinion; product surface constraints documented | **P0** | M | — |
| E0.4 | Riyadh comparable-density study (A-03) | For each of the 4 target districts, count usable comps for a median apartment; report `n_effective` distribution | **P0** | S | E0.1 |
| E0.5 | Data-sharing enquiries: Infath, NHC, REGA | Written enquiries sent; responses logged | P1 | S | — |
| E0.6 | Commercial enquiries: Wasalt, SPL, one transaction-data vendor (A-01 fallback) | Terms and pricing on file | P1 | M | — |
| E0.7 | PDPL processing assessment incl. cross-border inference (A-09) | Assessment recorded; residency decision made | **P0** | M | — |

> **Gate:** if E0.1 fails, stop and re-scope. Do not begin E2/E4 on the assumption it will pass.

---

## E1 — Foundation

| ID | Feature | User story | Acceptance criteria | Pri | Est | Depends |
|---|---|---|---|---|---|---|
| E1.1 | Monorepo + tooling | As an engineer I want one command to run everything locally | `docker compose up` yields web+api+worker+Postgres/PostGIS+Redis+MinIO; `make test lint typecheck` green | P0 | M | — |
| E1.2 | Database + migrations | As an engineer I want schema changes to be reversible | Alembic up/down verified in CI; PostGIS enabled; partitioning templates for snapshot tables | P0 | M | E1.1 |
| E1.3 | AuthN/AuthZ | As a user I want to sign in with SSO | OIDC login; RBAC roles; org isolation with RLS; cross-tenant access test fails closed | P0 | M | E1.2 |
| E1.4 | Domain skeleton + `Provenanced[T]` | As an engineer I want provenance impossible to omit | Money fields typed so a bare `Decimal` will not compile; import-linter rule forbids I/O in `domain` | P0 | M | E1.1 |
| E1.5 | CI/CD | As a team we want a failing security gate to block merge | Format, lint, typecheck, unit, integration, migration up/down, SAST, SCA, container + IaC scan | P0 | M | E1.1 |
| E1.6 | Observability baseline | As an operator I want a trace across the pipeline | OTel traces API→worker; Grafana dashboard; structured logs with redaction filter, tested | P0 | M | E1.1 |
| E1.7 | AR/EN + RTL shell | As an Arabic-speaking user I want a native-feeling UI | Locale routing; full RTL mirroring; Arabic numerals normalised; no hard-coded strings | P0 | M | E1.1 |

## E2 — Ingestion

| ID | Feature | User story | Acceptance criteria | Pri | Est | Depends |
|---|---|---|---|---|---|---|
| E2.1 | `PropertySource` port + registry | As an engineer I want sources to be swappable | Protocol implemented; a source without `legal_access_method` fails to construct; CI asserts every source appears in the matrix (ADR-008) | P0 | M | E1.4 |
| E2.2 | Raw record store + idempotency | As a compliance officer I want original bytes retained | `content_hash` unique; re-ingest is a no-op; large payloads to object storage; raw is immutable | P0 | M | E2.1 |
| E2.3 | PII redaction at boundary | As a DPO I want no seller contact data stored | Phone/email/national-ID patterns (Arabic + Latin numerals) redacted pre-storage and pre-LLM; unit-tested against a fixture corpus | P0 | M | E2.2 |
| E2.4 | **Manual/analyst opportunity entry** | As an analyst I want to enter an Infath lot in under 3 minutes | Form with validation; document attachment; enters the same pipeline as any connector; measured p50 < 3 min | **P0** | M | E2.1 |
| E2.5 | MOJ transaction bulk loader | As the system I need comparables | Quarterly resource ingested, normalised, geocoded to district, loaded into `transactions`; idempotent re-run | **P0** | L | E2.1, E0.1 |
| E2.6 | KAPSARC index connector | As the valuation engine I need time adjustment | Nightly pull of district/city/national index series; gaps handled; **CONFIRMED source, buildable today** | P0 | S | E2.1 |
| E2.7 | District boundaries into PostGIS | As the system I need a geospatial unit | Riyadh district polygons loaded; point-in-polygon assignment; provenance of boundary source recorded | P0 | M | E1.2 |
| E2.8 | Broker/user submission portal | As a broker I want to submit an opportunity | Authenticated submission with consent capture; moderation queue | P1 | M | E2.4 |
| E2.9 | Source health monitoring | As an admin I want to know a connector died | Per-source last-success, latency, error rate, freshness; alert on staleness; label-drift check against the matrix | P0 | M | E2.1 |

## E3 — Property intelligence

| ID | Feature | User story | Acceptance criteria | Pri | Est | Depends |
|---|---|---|---|---|---|---|
| E3.1 | Normalisation to typed `Property` | As the system I want consistent data | Units, Arabic/Hijri dates, Eastern-Arabic numerals, property-type taxonomy normalised; per-field provenance written | P0 | M | E2.2 |
| E3.2 | Geolocation | As a user I want properties on a map | Coordinates where available, district containment otherwise; `location_precision` always recorded | P0 | M | E2.7 |
| E3.3 | Entity resolution | As a user I want one card per physical unit, not five | Blocking + weighted scoring; ≥0.85 auto-merge; 0.60–0.85 review queue; merges reversible with both timelines preserved | P0 | L | E3.1 |
| E3.4 | Projects & developers registry | As an analyst I want to know who built it | Project/developer entities; properties linked; unknown developer handled as 50, not 0 | P1 | M | E3.1 |
| E3.5 | Property timeline | As an investor I want to see the price trend | Every snapshot retained; timeline API and UI; price-reduction sequence derived, never stored destructively | **P0** | M | E2.2 |

## E4 — Valuation *(core IP)*

| ID | Feature | User story | Acceptance criteria | Pri | Est | Depends |
|---|---|---|---|---|---|---|
| E4.1 | Comparable selection | As an investor I want relevant comps | Expanding-radius selection; kernel weights; expansion beyond district penalises confidence; comps returned with weight breakdown | **P0** | L | E2.5, E3.2 |
| E4.2 | Time adjustment | As an investor I want stale comps corrected | Each comp indexed to today; index tier (district/city/national) recorded and reflected in confidence | **P0** | M | E2.6, E4.1 |
| E4.3 | Fair-value estimator | As an investor I want a defensible value | Weighted median + weighted quartiles; IQR outlier rejection with excluded comps retained and shown; `n_effective < 3` ⇒ refuse | **P0** | M | E4.2 |
| E4.4 | Valuation confidence | As an investor I want to know how much to trust it | Formula per spec §2.3; calibration harness in place | **P0** | M | E4.3 |
| E4.5 | Comparables UI | As an investor I want to see the actual comps | Table + map of comps with weights, distance, date, adjusted price/m²; excluded outliers visible with reason | **P0** | M | E4.3 |
| E4.6 | Rental estimate & yield | As an investor I want the yield | Rental comps; gross and net yield; **assumptions displayed and user-overridable** | P1 | M | E4.1 |
| E4.7 | Back-testing harness | As a product owner I want to know if we are right | Value as-of-T vs. realised sale; MAE and interval-coverage reported | **P0** | M | E4.3 |

## E5 — Opportunity engine

| ID | Feature | User story | Acceptance criteria | Pri | Est | Depends |
|---|---|---|---|---|---|---|
| E5.1 | True acquisition cost | As an investor I want the real cost | Itemised line items with basis; **`UNKNOWN` material item blocks the discount**; refusal names the missing item | **P0** | M | E1.4 |
| E5.2 | Cost rule tables | As the system I need fees and taxes | Versioned rule table with effective dates; counsel-reviewed; changes are migrations, not edits | P0 | M | E5.1 |
| E5.3 | Risk model | As an investor I want the downside | Nine risk dimensions; explicit red flags with evidence | P0 | M | E3.1 |
| E5.4 | Opportunity score | As an investor I want a ranking | Deterministic; components exposed; versioned weight profiles; **golden-fixture bit-identical reproducibility test** | **P0** | M | E4.3, E5.1, E5.3 |
| E5.5 | Confidence gate | As a user I want to be told when we do not know | <0.60 ⇒ `INSUFFICIENT DATA` with no recommendation; 0.60–0.75 ⇒ classification capped at "Worth reviewing" | **P0** | S | E5.4 |
| E5.6 | Per-org weight profiles | As an office I want my own thesis | Profile CRUD; version stored on every score; re-score on change | P1 | M | E5.4 |

## E6 — Product surface

| ID | Feature | User story | Acceptance criteria | Pri | Est | Depends |
|---|---|---|---|---|---|---|
| E6.1 | Opportunity list | As an investor I want ranked opportunities | Filter, sort, cursor pagination; card shows score, price, est. value, discount, area, price/m², yield, location, source, time remaining, confidence, risk; p95 < 500ms | P0 | M | E5.4 |
| E6.2 | Opportunity detail | As an investor I want the full picture | Overview · financials · comparables · timeline · rental · map · project/developer · auction · verification · documents · memo · provenance | P0 | L | E6.1 |
| E6.3 | Map | As an investor I want to search spatially | Radius, polygon draw, district select; clustering; price/m² heatmap; viewport p95 < 700ms | P0 | L | E3.2 |
| E6.4 | Watchlists | As an investor I want to monitor an area | Save filters and polygons; "monitor this area" | P0 | M | E6.3 |
| E6.5 | Alerts | As an investor I want to be told, not to check | Rule builder; triggers per PRD §5.9; in-app + email; ack and feedback capture | **P0** | L | E6.4, E5.4 |
| E6.6 | Provenance view | As a user I want to know where a number came from | Any displayed field expands to source, timestamp, method, confidence, evidence | **P0** | M | E1.4 |
| E6.7 | Accessibility pass | As a user with low vision I want to use this | WCAG 2.1 AA; risk/confidence never colour-only; keyboard navigable; screen-reader tested in AR and EN | P0 | M | E6.2 |

## E7 — Agents

| ID | Feature | User story | Acceptance criteria | Pri | Est | Depends |
|---|---|---|---|---|---|---|
| E7.1 | Agent runtime | As an engineer I want uniform guarantees | Schema validation with one repair retry then fail; cost ceilings; idempotency; full recording to `agent_runs`/`llm_calls` | P0 | M | E1.4 |
| E7.2 | LLM provider abstraction | As a DPO I want to move inference in-Kingdom without a rewrite | Adapters for ≥2 providers + local; tier-based selection; failover verified by a chaos test | P0 | M | E7.1 |
| E7.3 | Extraction agent | As an analyst I want structured data from Arabic text | ≥90% field accuracy on a labelled Riyadh corpus; unsupported fields null, never inferred; range validation | **P0** | L | E7.1 |
| E7.4 | Prompt-injection defences | As a security engineer I want untrusted text contained | Delimited data framing; no tools on untrusted paths; post-model range validation; injection corpus test suite passes | **P0** | M | E7.1 |
| E7.5 | Document intelligence | As an analyst I want the auction PDF read for me | Download, hash, store, classify, extract with page/bbox citation; conclusions traceable to evidence | P1 | L | E7.3 |
| E7.6 | Verification agent | As an investor I want official confirmation | Ad-licence, Wafi, developer checks; **`VERIFIED` requires stored evidence (DB constraint)**; conflicts surfaced as `CONFLICTED` | P1 | M | E7.1 |
| E7.7 | Investment memo agent | As an investor I want a decision-ready summary | Gated at score ≥70 ∧ conf ≥0.60; every figure resolves to a computed field; **fails closed** on unresolved citation | P1 | M | E5.4, E7.1 |
| E7.8 | Natural-language search | As a user I want to describe what I want | Compiles to a structured filter object shown and editable before results; never emits SQL | P1 | M | E6.1 |
| E7.9 | Watch agent | As an investor I want only meaningful interruptions | Materiality decision; digest vs. alert routing; `alert_precision` tracked with user feedback | P1 | M | E6.5 |

## E8 — Admin & hardening

| ID | Feature | Acceptance criteria | Pri | Est | Depends |
|---|---|---|---|---|---|
| E8.1 | Source-health dashboard | Last success, latency, error rate, freshness, volume, legal basis, matrix label drift | P0 | M | E2.9 |
| E8.2 | Entity-resolution review queue | Ambiguous merges reviewed; decisions feed threshold tuning | P1 | M | E3.3 |
| E8.3 | Cost dashboard | Cost per property/opportunity/user/alert; budget alerts; degradation-not-failure verified | P0 | S | E7.1 |
| E8.4 | Data-quality monitoring | Field completeness, confidence distribution, duplicate rate, agent-vs-deterministic disagreement rate | P0 | M | E3.1 |
| E8.5 | Backup & recovery | PITR configured; **restore rehearsed and timed**, not merely configured | P0 | M | E1.2 |
| E8.6 | Load & security testing | Load test to 5× projected; pen test; findings triaged before launch | P0 | M | E6.x |

---

## Acceptance scenario (PRD §7 / brief §25)

> "Monitor apartments around Qurtubah, Sidrah, Al Munsiyah and Al Rimal in Riyadh. Maximum
> total acquisition cost SAR 1.2M. Alert me when an Infath auction, resale, assignment, urgent
> sale or developer unit appears at least 15% below estimated market value."

| # | Step | Verified by |
|---|---|---|
| 1 | NL request compiles to a visible, editable filter | E7.8 |
| 2 | Saved as a watchlist with the four district polygons | E6.4 |
| 3 | A new Infath lot is ingested (analyst-assisted) | E2.4 |
| 4 | Normalised, geolocated, resolved against existing properties | E3.1–E3.3 |
| 5 | Comparables selected and time-adjusted; fair value with band and confidence | E4.1–E4.4 |
| 6 | True acquisition cost itemised — **including remaining installments, or refusing the discount** | E5.1 |
| 7 | Risk assessed; verification attempted with evidence | E5.3, E7.6 |
| 8 | Scored; classification respects the confidence gate | E5.4, E5.5 |
| 9 | Alert dispatched within 5 minutes because discount ≥15% and cost ≤1.2M | E6.5 |
| 10 | User opens the memo and can trace every number to its source | E7.7, E6.6 |
