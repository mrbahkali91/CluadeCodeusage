# Risk Register and Launch Assumptions

Likelihood and impact: **H / M / L**. Ranked by exposure (likelihood × impact), highest first.

---

## Top 20 technical and product risks

| # | Risk | L | I | Mitigation | Owner |
|---|---|---|---|---|---|
| 1 | **Transaction-level MOJ data is not obtainable** at usable granularity, or the licence forbids commercial derivative use. Every "% below market" claim collapses | M | **H** | E0.1/E0.2 in week 1 as a hard gate; commercial dataset fallback priced in parallel (E0.6); if both fail, pivot to a signal-and-workflow product and say so plainly rather than shipping unsupported numbers | Product |
| 2 | **Comparable density too thin** in target districts, so most opportunities show `INSUFFICIENT DATA` and the product looks empty | M | **H** | E0.4 density study before build; radius-expansion with confidence penalty; widen initial geography to all Riyadh if needed; treat an honest empty state as better than a dishonest full one | Data |
| 3 | **No lawful opportunity supply at MVP** — partnerships slow, scraping prohibited | **H** | M | First-party ingestion as a P0 connector (E2.4/E2.8); Infath analyst entry is tractable at real auction volume; partnership pipeline started week 1 | Product |
| 4 | **A published "Strong" opportunity proves materially wrong** and credibility is lost | M | **H** | Confidence gate (E5.5); comparables always shown; back-testing (E4.7); calibration monitoring; the credibility invariant blocks release | Product |
| 5 | **The advertised-price trap** — a discount computed against a price excluding remaining installments | M | **H** | Domain-level refusal on `UNKNOWN` material line items (E5.1); enforced by type and DB constraint, not by review | Engineering |
| 6 | **Prompt injection** via listing or document text moves an extracted field | **H** | M | Layered defence (security §5); deterministic scoring means injection cannot directly set an output; injection corpus test suite (E7.4) | Security |
| 7 | **PDPL exposure** from ingested personal data or cross-border inference | M | **H** | Boundary redaction (E2.3); minimisation; in-Kingdom storage; provider abstraction enables in-Kingdom inference (E7.2); assessment E0.7 | DPO |
| 8 | **We are deemed a regulated advertiser or broker** | M | **H** | Counsel opinion E0.3; deep-link never re-host; no commission, no execution; regulated activities are explicit non-goals | Legal |
| 9 | **Arabic extraction accuracy below target**, especially تنازل vs. plain sale | M | M | Labelled Riyadh corpus with accuracy gate (E7.3); Arabic vocabulary in the prompt contract; range validation; human review queue | AI |
| 10 | **Entity resolution merges distinct units** or fails to merge duplicates, corrupting counts and metrics | M | M | Conservative thresholds with a review band (E3.3); reversible merges; `duplicate_resolution_rate` monitored | Data |
| 11 | **LLM cost per opportunity exceeds budget** as volume grows | M | M | Staged gating (agent architecture §4); per-call and per-pipeline ceilings; degrade to smaller models rather than fail; cost dashboard (E8.3) | Engineering |
| 12 | **Source silently breaks** — a portal changes, a schema shifts — and stale data is presented as current | **H** | M | Source health with freshness alerting (E2.9/E8.1); matrix label-drift check; data-freshness shown in the UI | Platform |
| 13 | **Alert fatigue** drives churn | M | M | Materiality decision in the watch agent (E7.9); digest vs. alert routing; `alert_precision` tracked; rules below threshold flagged | Product |
| 14 | **Geocoding unavailable or inaccurate** (SPL procurement slow) | M | M | District-containment fallback with honest `location_precision`; boundary import from open sources; provider behind a port | Engineering |
| 15 | **Rent estimates unsupportable** because rental comparables are the weakest data we have | **H** | M | Present yield as a range with visible assumptions; suppress when evidence is thin; pursue Ejar aggregate index | Data |
| 16 | **Market conditions shift** faster than the quarterly index, making time adjustment lag | M | M | Blend quarterly index with a listing-derived high-frequency signal where available; widen bands when index age exceeds a threshold | Data |
| 17 | **Postgres becomes a bottleneck** at transaction volume | L | M | Partitioning from day one; materialised district metrics; read replica; ADR-002/005 revisit triggers | Platform |
| 18 | **Key-person dependency** on the analyst for ground truth | M | M | Document the review methodology; build the labelled corpus as an asset, not tacit knowledge | Product |
| 19 | **Competitor with a scraped dataset moves faster** on coverage | M | L | Compete on defensibility and verification, not volume; the auction-outcome dataset is the durable asset | Product |
| 20 | **Fee and tax rules change** (transaction tax, VAT treatment) and stale rules corrupt every true-cost figure | M | M | Versioned rule tables with effective dates (E5.2); changes are reviewed migrations; historical results retain the rule version that produced them | Legal + Eng |

---

## Top 10 assumptions to validate before commercial launch

Each carries an explicit falsification test. An assumption without one is a hope.

| # | Assumption | Validation | Deadline | If false |
|---|---|---|---|---|
| **A-01** | Transaction-level MOJ sale records (price, area, district, date) are lawfully obtainable | Download two quarterly resources from a KSA egress; verify row granularity and licence | **Week 1 — Phase 0 gate** | Buy commercial data, or pivot away from valuation claims |
| **A-02** | The KSA Open Data License permits commercial derivative use | Counsel review of the licence text | Week 2 | Negotiate a licence; add a data cost line |
| **A-03** | Riyadh comparable density supports ≥8 usable comps for a median apartment in target districts | Density study across the 4 districts | **Week 2 — Phase 0 gate** | Widen geography; lower `MIN_COMPS` with widened bands; accept more `INSUFFICIENT DATA` |
| **A-04** | Showing our analysis beside a link to a third-party listing is not regulated advertising | Counsel opinion | Week 2 | Remove third-party content; first-party and official sources only |
| **A-05** | Users act on a score they cannot fully audit, provided comparables are visible | 10 user interviews with a Slice-1 prototype | End of Slice 1 | Make evidence primary and the score secondary in the UI |
| **A-06** | Analyst-assisted entry sustains ≥30 quality Riyadh opportunities/week at acceptable cost | Two-week timed trial with a real analyst | End of Slice 2 | Accelerate partnerships; narrow to auctions only |
| **A-07** | Infath auction properties genuinely clear below market by a material margin | Track ≥30 auctions: our estimate vs. hammer price | End of Slice 4 | **The core value hypothesis is wrong.** Re-centre on assignments and distressed resale |
| **A-08** | LLM extraction reaches ≥90% field accuracy on Arabic auction and listing text with calibrated confidence | Labelled corpus of 200 records | End of Slice 4 | Increase human-in-the-loop; raise cost per opportunity; narrow to structured sources |
| **A-09** | PDPL permits our processing with minimisation and in-Kingdom storage, including redacted cross-border inference | DPO assessment + counsel | **Week 2 — Phase 0 gate** | Move inference in-Kingdom or self-host (the provider port exists for exactly this) |
| **A-10** | Buyers pay subscription pricing for intelligence without transaction execution | 15 pricing conversations; ≥5 letters of intent | Before launch | Re-examine the business model; regulated extensions require a different licensing strategy |

**A-01, A-03 and A-09 are Phase 0 gates.** The rest are tracked as validation work inside their
slices, each with a named owner and a review date. An assumption that has passed its deadline
without a verdict is escalated, not silently carried forward — carrying unvalidated assumptions
into a build is how a well-engineered product turns out to solve a problem that was not there.
