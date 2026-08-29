# Product Requirements Document
## Saudi Real Estate Opportunity Intelligence

**Phase:** 0 (Discovery) · **Status:** For review · **Date:** 2026-08-29

---

## 1. Executive product interpretation

The brief asks for an agentic platform that finds mispriced Saudi real estate. Stripped to
its economic core, the product makes exactly one claim, over and over:

> **"This asset can be acquired for X. Comparable evidence says it is worth Y. Here is Y's
> derivation, here is our confidence in it, and here is what would have to be true for us to
> be wrong."**

Everything else — the map, the alerts, the memo, the agents — is delivery mechanism for that
claim. This reframing has three hard consequences that shape the entire build:

**1. The product is a valuation company, not a search company.** A user can already find
listings on five portals. What they cannot do is establish, in under a minute and with
auditable evidence, what a property is actually worth and what it will actually cost to
acquire. The comparable engine and the true-acquisition-cost calculator are the product.
The aggregation is table stakes.

**2. Credibility is the product's only real defence.** One confidently-stated "23% below
market" that turns out to be wrong — because we compared an assignment's *seller premium* to
a *full market value*, or used an un-time-adjusted 2021 comparable — destroys more trust than
ten missed opportunities create. Therefore: every number is reproducible, every number carries
a confidence, and **low confidence suppresses the recommendation rather than degrading it**.

**3. The scarce input is not compute or model quality. It is lawful transaction data.**
See §4.

### What "agentic" legitimately means here

The brief is right that the core must be agentic, and also right to warn against agents as
marketing. The honest division of labour:

| Genuinely needs an agent | Must never be an agent |
|---|---|
| Reading a 40-page Arabic auction brochure and extracting the lot schedule | Computing price per m² |
| Deciding whether two listings describe the same unit when the text disagrees | Deciding the fair value number |
| Reconciling contradictory evidence across sources and saying which to trust | Computing the opportunity score |
| Turning a natural-language request into structured filters | Deciding whether a legal risk exists |
| Writing the investment memo that explains the computed numbers | Originating any price, area, or date |

The system is agentic in its **autonomy loop** — it discovers, decides what deserves deeper
(and more expensive) analysis, verifies, monitors, and decides when to interrupt a human. It
is deterministic in its **arithmetic**. That combination is what makes it defensible rather
than a chat wrapper over listings.

---

## 2. Where I disagree with the brief

Stated plainly, as requested, because these change the plan.

### 2.1 The listings-aggregation premise is largely not lawful, and the MVP must not depend on it

The brief lists Aqar, Haraj, Bayut, Wasalt, X and developer sites as opportunity sources.
Research (see [the data source matrix](../data-sources/matrix.md)) finds that **none of these
offers a public API, and all are either proprietary content or user-generated content
containing personal data.** Ingesting them at scale means unauthorised scraping — which the
brief itself, correctly, forbids in §2. The brief therefore contains an internal tension.

**Resolution:** the MVP's opportunity supply comes from (a) Infath's *publicly browsable*
judicial auctions, whose volume is small enough to handle with analyst-assisted structured
entry while a data-sharing agreement is pursued, and (b) **first-party channels** — analyst
entry, a broker submission portal, and user-forwarded opportunities. Both flow through the
identical `PropertySource` interface, so when a licensed feed is signed, nothing downstream
changes. Partnerships are worked in parallel, ranked with Wasalt first because one agreement
covers both listings and Infath-licensed auctions.

This is not a compromise on ambition. It is the recognition that **the intelligence engine,
not the crawler, is the thing worth building**, and it can be built and sold now.

### 2.2 One assumption gates the whole business, and it is not yet verified

"Estimated market value" requires transaction-level comparables with area and location.
Evidence indicates the Ministry of Justice publishes exactly that as open data. **I could not
verify it** — the portal refused connections from this environment. If it turns out to be
aggregate-only, the product cannot honestly say "19% below market" and must either buy a
commercial dataset or become a different, weaker product.

**This must be validated in week 1, from a Saudi-resident network, before Phase 1 begins.**
It is Assumption A-01 and it is a go/no-go, not a risk to monitor.

### 2.3 "Advertised price vs. market value" is the single most dangerous calculation in the product

The brief's own worked example makes the point: a seller asking SAR 120,000 for a unit with
SAR 600,000 of remaining developer installments is not a SAR 120,000 opportunity. The brief
already forbids this comparison, and it deserves elevation to an **invariant enforced in
code**, not a guideline: a valuation may only be compared against a `TrueAcquisitionCost`
object whose line items are complete, and the comparison is refused — not estimated — when
`remaining_installments` is `UNKNOWN` on an off-plan or assignment opportunity. Assignments
(تنازل) are the most common Saudi opportunity type where naive systems produce absurd numbers.

### 2.4 We may be a regulated advertiser, not merely a software product

The brief positions the platform as "intelligence software, not a broker" — correct and
important. But Saudi real-estate advertising is licensed: ads require a licence number and
advertisers are verified. **Re-publishing third-party listings could constitute advertising**
even though we take no commission. Counsel must confirm before we display any third-party
listing content. Design consequence: **deep-link, never re-host**; show our analysis beside a
link to the source, not a reproduction of the ad.

### 2.5 Scoring weights should not be a single global constant

The brief's weights (discount 30%, liquidity 15%, …) encode one investor archetype: a flipper.
A yield-seeking family office and a distressed-asset buyer want materially different rankings
from the same data. Weights must be **per-organisation profiles**, versioned, with the global
default as the brief specifies. Cost: one table and a join. Benefit: the same engine serves
three customer segments.

### 2.6 Temporal is the right idea and the wrong first step

The evaluation pipeline is a short, idempotent DAG per property that completes in minutes, not
a long-lived saga with human-in-the-loop compensation. Introducing Temporal in Phase 1 buys
capability we will not use and costs a cluster, a new failure domain, and a new on-call
surface for a team that will be small. **Postgres-backed durable jobs first**, with explicit
written trigger conditions for adopting Temporal. See ADR-004.

---

## 3. Users and jobs to be done

| Persona | Job | Success looks like |
|---|---|---|
| **Individual investor (primary, MVP)** — Saudi professional, 1–5 properties, evaluates part-time | "Tell me when something genuinely underpriced appears near my districts, and let me trust the number" | Opens an alert, reads the memo, forms a view in <5 minutes, and the comparables hold up when checked |
| **Small investment office / family office** | "Cover more of Riyadh than my two analysts can, with an audit trail" | Replaces a weekly manual sweep; can show a partner why the number is what it is |
| **Broker / agent (secondary)** | "Find assignment and distressed inventory before competitors" | Sources deals rather than reposting them |
| **Internal analyst (day-one user)** | "Enter and enrich opportunities; keep source health green" | Enters an Infath lot in <3 min; sees the pipeline evaluate it |
| **Compliance / admin** | "Prove every number and every ingestion is lawful and traceable" | Any field traces to a source record and a legal basis |

**Deliberately not a target for MVP:** consumer home-buyers. They want search. We are not
building search.

---

## 4. Key assumptions

Ranked by damage-if-wrong. Full validation plan in the [risk register](risk-register.md).

| ID | Assumption | If wrong |
|---|---|---|
| **A-01** | Transaction-level MOJ sale records (price, area, district, date) are lawfully obtainable | **Product thesis fails.** Buy commercial data or pivot to a weaker "signal" product |
| **A-02** | KSA Open Data License permits commercial derivative use | Licensing cost appears; possibly a paid data line item |
| **A-03** | Riyadh comparable density supports ≥8 usable comps for a typical apartment in a target district | Confidence gate suppresses most scores; MVP looks empty |
| **A-04** | Displaying our analysis alongside a link to a third-party listing is not regulated advertising | Product surface must change materially |
| **A-05** | Users will act on a score they cannot fully audit *if* the comparables are visible | The memo/evidence UI is the product, not a feature |
| **A-06** | Analyst-assisted entry can sustain ≥30 quality Riyadh opportunities/week at acceptable cost | MVP has insufficient supply to prove value |
| **A-07** | Infath auction discounts are real and material (the core value hypothesis) | The headline opportunity type is not an opportunity |
| **A-08** | An LLM can extract Arabic auction brochures at ≥90% field accuracy with confidence calibration | Document intelligence becomes manual; cost per opportunity rises |
| **A-09** | PDPL permits our processing with data minimisation and in-Kingdom storage | Architecture must move fully in-Kingdom including inference |
| **A-10** | Buyers will pay subscription pricing for intelligence without transaction execution | Business model must extend into regulated territory |

---

## 5. Functional requirements (MVP)

Priorities: **P0** must ship; **P1** ships if the slice completes; **P2** post-MVP.

### 5.1 Ingestion (P0)
- A `PropertySource` connector interface with `discover / fetch / normalize / validate / health_check`.
- Connectors: first-party analyst entry, broker/user submission, MOJ transaction bulk load, KAPSARC index pull, Infath analyst-assisted entry with document attachment.
- Every ingested record persists its raw payload, `retrieved_at`, source, and legal basis. **Raw payloads are immutable.**
- Re-ingesting the same record is idempotent and produces a new **snapshot**, never an overwrite.

### 5.2 Property intelligence (P0)
- Normalisation to a typed `Property` with per-field provenance and confidence.
- Geolocation to coordinates where possible, district otherwise, with `location_precision` recorded honestly.
- Entity resolution: the same physical unit across sources resolves to one property, with a confidence score and a human override.
- Full timeline: every price change, status change and signal is retained. **Historical asking prices are never overwritten** — the trend is itself the signal.

### 5.3 Valuation and comparables (P0)
- Comparable selection by distance, district, type, area band, age, floor, recency.
- Robust statistics: weighted median price/m², IQR, outlier rejection. Never a bare mean.
- Time adjustment of every comparable using the district/city index.
- Output `fair_value_low / base / high`, comparable count, comparable quality, confidence.
- **The comparables used are always displayed.** A valuation whose evidence cannot be shown is not shipped.

### 5.4 True acquisition cost (P0)
- Itemised: seller payment, remaining installments, auction fees, brokerage, transfer tax/VAT where applicable, registration, renovation estimate, known liabilities.
- Every line item shows value, basis (`ACTUAL / RULE / ESTIMATE / UNKNOWN`), and source.
- **Hard invariant:** discount is computed against true acquisition cost, never advertised price. Refused when a material line item is `UNKNOWN`.

### 5.5 Rental and yield (P1)
- Estimated monthly/annual rent from rental comparables and district indicators; gross and net yield with stated occupancy and cost assumptions.

### 5.6 Risk and verification (P0/P1)
- Risk scoring: legal, occupancy, developer, construction, liquidity, market, valuation uncertainty, data quality, auction. Explicit red flags.
- Verification checks against official sources (ad licence, Wafi project, developer registry) with evidence. **`VERIFIED` is never asserted without a stored evidence record.**

### 5.7 Opportunity scoring (P0)
- Deterministic weighted score, 0–100, with all components exposed. Weights configurable per organisation.
- Classification bands per the brief, with a **confidence gate**: below 60% confidence the UI shows `INSUFFICIENT DATA`, not a score.
- Fully reproducible: same inputs + same weight version ⇒ identical score, asserted in tests.

### 5.8 Discovery experience (P0)
- Opportunity list and detail; PostGIS map with radius, polygon and district search; watchlists including "monitor this polygon".
- Natural-language search that **compiles to visible structured filters** the user can inspect and edit.

### 5.9 Alerts (P0)
- User-defined watch rules (districts, type, max cost, min discount, min score). Triggers: new opportunity, price reduction, auction opening/closing, bid crossing the user's recommended maximum, new relevant comparable.
- Pluggable channels; in-app + email for MVP.

### 5.10 Investment memo (P1)
- LLM-authored memo over computed numbers: opportunity, why now, pricing, comparable evidence, expected returns, risks, **maximum recommended purchase price**, questions before purchase, decision.
- Every figure in the memo is a reference to a computed field. Generation fails closed if a cited number cannot be resolved.

### 5.11 Provenance and audit (P0)
- Any displayed field answers "where did this come from?" with source, timestamp, method and confidence.
- Immutable audit events for ingestion, agent runs, score changes and user actions.

### 5.12 Admin (P0)
- Source health dashboard: last success, latency, error rate, freshness, records ingested, legal basis, and **label drift** against the source matrix.

---

## 6. Non-functional requirements

| Area | Requirement |
|---|---|
| Latency | Opportunity list p95 < 500ms; detail p95 < 800ms; map viewport query p95 < 700ms |
| Freshness | Auction opportunities visible within 60 min of ingestion; alert dispatched within 5 min of evaluation |
| Correctness | Scoring reproducibility asserted by golden tests; valuation regression suite over a fixed comparable corpus |
| Availability | 99.5% MVP; ingestion failure of any single source never degrades read paths |
| Scale target | 500k properties, 5M transactions, 50k opportunities, 2k users — comfortably within a single well-indexed Postgres instance |
| Cost | < SAR 2.50 average LLM cost per evaluated opportunity; deep analysis only on candidates passing deterministic pre-filters |
| Language | Arabic and English throughout, including RTL layout and Arabic-language extraction. **Not a translation afterthought** |
| Accessibility | WCAG 2.1 AA. Risk and quality never signalled by colour alone |
| Residency | Personal data stored in-Kingdom; PII removed before any cross-border inference call |

---

## 7. MVP scope

**Geography:** Riyadh only. Target districts for the acceptance scenario: Qurtubah, Sidrah,
Al Munsiyah, Al Rimal. **Asset types:** apartments and villas. **Opportunity types:** Infath
auction, resale, off-plan assignment, urgent sale, developer inventory.

**In scope:** source ingestion · normalisation · geolocation · comparable engine · valuation ·
true acquisition cost · risk · opportunity scoring · opportunity list & detail · map ·
watchlists · alerts · AI investment memo · natural-language search · provenance · admin source
health.

**Success criteria — the MVP is judged on one question:** *does it surface opportunities a
competent analyst would have missed, with numbers they agree with?* Measured as:
1. ≥50 scored Riyadh opportunities in the graph.
2. Blind review: an independent analyst agrees with the fair-value band on **≥80%** of a sample of 20.
3. **≥3 opportunities** surfaced that the review analyst confirms they would not have found manually.
4. Zero published "strong opportunity" classifications later found to rest on a false verified claim. *(This one is pass/fail — it is the credibility invariant.)*

---

## 8. Explicit non-goals

Not "later" — **not now, deliberately.**

| Non-goal | Reason |
|---|---|
| Payments, escrow, transaction execution | Regulated activity; changes our legal identity |
| Brokerage or commissions | Would make us a licensed broker under REGA |
| Automated auction bidding | Regulatory exposure and unbounded financial risk |
| Financial advice as a regulated service | We publish analysis with stated assumptions, not advice |
| Mortgage marketplace | Separate regulated domain |
| Native mobile apps | Responsive web first; alerts carry mobile use |
| Consumer property search | Directly contradicts the product thesis |
| Cities beyond Riyadh | Comparable density must be proven in one market first |
| Scraping any portal without a licence | Prohibited by policy, not by capacity |
| Chat as the primary interface | Chat is an entry point to structured views, never the product |
| Ingesting seller contact details | PDPL exposure with no product benefit |
| Microservice decomposition | Premature; see ADR-001 |
