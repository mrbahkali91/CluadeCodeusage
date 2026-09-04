# Valuation, True Cost and Opportunity Scoring

**This document specifies the core intellectual property of the platform.** Everything here is
deterministic, versioned and unit-testable. No LLM participates in any calculation on this page.
LLMs may *explain* these outputs; they may never produce them.

Every formula below is versioned (`method_version`). A stored result records the version that
produced it, so historical results remain reproducible after the method changes.

---

## 1. Comparable selection

### 1.1 Candidate generation

From `transactions` (actual registered sales — never asking prices), for subject property `p`:

```sql
WHERE property_class  = p.property_class
  AND transacted_on  >= now() - interval '24 months'
  AND area_sqm BETWEEN p.area * 0.65 AND p.area * 1.50
  AND ST_DWithin(location, p.location, :radius_m)
```

Radius expands in steps `750m → 1500m → 3000m → district → adjacent districts` and stops at the
first step yielding ≥ `MIN_COMPS` (default 8). Expanding beyond the district is recorded and
penalises confidence — a comparable from another district is a weaker claim, and the output
must say so.

### 1.2 Similarity weighting

Each candidate `c` receives weight `w(c) ∈ (0,1]` as a product of independent Gaussian/decay
kernels. A product, not a sum: one badly mismatched dimension *should* be able to veto a
comparable, which a weighted sum cannot express.

| Kernel | Form | Default |
|---|---|---|
| Distance | `exp(-(d/λd)²)` | `λd = 1200 m` |
| Recency | `exp(-Δt/λt)` | `λt = 9 months` |
| Area | `exp(-((A_c-A_p)/A_p / λa)²)` | `λa = 0.20` |
| Age | `exp(-(|age_c-age_p| / λg)²)` | `λg = 8 years` |
| Same project | multiplier | `1.35` |
| Same district | multiplier | `1.20` |
| Floor delta (apartments) | `exp(-(|f_c-f_p| / 6)²)` | — |

```
w(c) = K_dist · K_time · K_area · K_age · M_project · M_district · K_floor
```

Multipliers are applied then the vector is renormalised so `max(w) = 1`, keeping weights
interpretable as "how comparable is this, relative to the best comparable we have".

### 1.3 Time adjustment — the step most systems skip

A sale 14 months ago is evidence about *then*. Each comparable's price per m² is indexed
forward to today using the district (fallback: city, fallback: national) index from
KAPSARC/GASTAT and REGA:

```
adj_ppsqm(c) = ppsqm(c) × Index(district, today) / Index(district, t_c)
```

Skipping this in a market that moved double digits would produce systematically wrong
"discounts" — flattering in a rising market, which is precisely the dangerous direction.
`Index` availability is itself recorded; where only a national index exists, confidence drops.

### 1.4 Outlier rejection

Robust, and applied before the estimator rather than trusting the estimator to cope:
compute Q1/Q3 of `adj_ppsqm`, drop points outside `[Q1 − 1.5·IQR, Q3 + 1.5·IQR]`, retain the
dropped rows flagged as excluded so the UI can show them and say why.

---

## 2. Fair market value

### 2.1 Estimator

Weighted median, not mean — Saudi transaction data contains genuine extremes (family
transfers at nominal prices, land assembly premiums) and a mean is not robust to them:

```
base_ppsqm = WeightedMedian({adj_ppsqm(c)}, {w(c)})
fair_value_base = base_ppsqm × area_p
```

### 2.2 Interval — `valuation-v2`

The band is an empirical weighted quantile pair, with **no inflation term**:

```
n_effective = (Σw)² / Σw²                  # Kish effective sample size

low  = WeightedQuantile(0.05) × area_p
high = WeightedQuantile(0.95) × area_p
```

`n_effective` rather than a raw count is still what makes the *confidence* honest — twenty
comparables of which nineteen are marginal is not twenty comparables — but it no longer moves
the band.

**Why v1 was replaced.** v1 took the (Q25, Q75) pair and widened both ends by
`1 + 0.6/sqrt(n_effective)`. At the effective sample sizes the engine actually achieves — median
`n_effective` 5.4, so a factor of ~1.26 on each end — that turned a 12%-wide band into one
**58.8% of value wide**, reaching 98.7% interval coverage against a 70% target. Coverage bought
that way is worthless: a band spanning −28%/+28% cannot separate "20% below market" from "fairly
priced", which is the single decision this product exists to support. The §5.1 discount
normalisation awards 50/100 at a 10% discount and 80/100 at 20%; under v1 both sat comfortably
inside the band.

The quantile pair was chosen by sweeping it against both targets on the back-test harness rather
than by argument. Of seven candidate pairs (measured with no index, tier `NONE`), exactly one
satisfies coverage ≥ 70% **and** width ≤ 30%:

| Pair | Median width | Coverage | Both targets |
|---|---|---|---|
| 0.25 / 0.75 | 12.0% | 40.1% | no |
| 0.20 / 0.80 | 14.5% | 49.3% | no |
| 0.15 / 0.85 | 18.5% | 61.2% | no |
| 0.10 / 0.90 | 21.9% | 67.1% | no |
| **0.05 / 0.95** | **27.9%** | **71.7%** | **yes** |
| 0.02 / 0.98 | 30.6% | 76.3% | no |
| 0.00 / 1.00 | 34.0% | 77.6% | no |

**With the live NATIONAL index present, no pair meets both targets.** Q05/Q95 gives 29.1% width
at 68.4% coverage and the next pair out breaches the width ceiling to reach 73.7%. Q05/Q95 is
retained as the least-bad point on that frontier: it holds the width ceiling — the target that
makes the band usable at all — and misses coverage by 1.6 points. The coverage target is
**currently unmet, and cannot be met by choosing a different quantile**: the error distribution's
heavy tail (p90 25.75% against a 7.27% median) is driven by cases where the comparables agree
closely and are collectively wrong, because the NATIONAL index does not track district-level
drift. A band built from comparable spread cannot anticipate that. District-level index coverage
is the fix, and it is the same fix for the confidence ceiling and the residual bias.

Thin evidence is now reported where it belongs — in the confidence figure — rather than by
making the value claim vaguer. A band that widens with uncertainty and a confidence score that
falls with it are not two views of one fact: the first degrades the product's only actionable
output, and the second does not.

### 2.3 Valuation confidence — `valuation-v2`

```
dispersion   = IQR / median
agreement    = max(0, 1 − dispersion / 0.30)

confidence_valuation =
      0.40 · agreement                        # do the comparables agree with EACH OTHER
    + 0.25 · min(1, n_effective / 12)          # evidence quantity
    + 0.10 · mean(w)                            # how similar they are to the subject
    + 0.15 · index_quality                      # district=1.0, city=0.7, national=0.4
    + 0.10 · subject_completeness                # known fields on the subject
```

**Why the weights moved.** v1 gave 0.25 to `mean(w)` and only 0.15 to agreement. `mean(w)`
measures how *similar* the comparables are to the subject, which says nothing about whether they
agree on a price: ten near-identical units in a heterogeneous pocket are all excellent
comparables that disagree wildly. Track D measured the consequence — **AUC 0.329** for
predicting an interval hit, and Spearman(confidence, |error|) of **+0.151**, the wrong sign. A
higher stated confidence went with a *larger* error. v2 inverts the two weights, and the sign
inverts with them: Spearman **−0.193**, point-error AUC **0.603**.

**The level is deliberately not rescaled.** Stated confidence averages 44% while the estimate
lands within 12% about 66% of the time, so the score looks under-confident. It is not
recalibrated upward, because on this corpus the gap is not calibration error — it is the score
correctly reporting two real evidence deficiencies:

| Term | Weight | Median contribution | % of max |
|---|---|---|---|
| Comparable agreement | 0.40 | 0.239 | 60% |
| Evidence quantity (`n_eff` 5.41 / 12) | 0.25 | 0.113 | 45% |
| **Index quality** (NATIONAL, 0.4) | 0.15 | 0.060 | **40%** |
| Subject completeness | 0.10 | 0.075 | 75% |
| Similarity to subject | 0.10 | 0.027 | 27% |
| **Total** | 1.00 | **0.514** | **51%** |

Against the gates, 16% of cases clear 0.60 and none clear 0.75. What raises it, each row changing
one thing: CITY index → 0.560; **DISTRICT index → 0.605 (53% clear 0.60)**; `n_eff` 12 → 0.651;
**both → 0.741 (84% clear 0.60, 45% clear 0.75)**. The realised 66% hit rate comes
from the *fixture generator* being benign, not from the evidence being strong; fitting a scale
factor to it would erase a true signal in order to match a synthetic distribution. The level
must be re-fit on real registered transactions, and until then under-confidence is the safe
direction — it over-refuses rather than over-promises. **District-level index coverage is the
highest-leverage fix**, worth up to +0.090 on every valuation.

**Refusal condition:** `n_effective < 3` ⇒ no valuation is produced. The property is stored with
`INSUFFICIENT_COMPARABLES` and shown without a fair-value claim. We do not extrapolate from two sales.

---

## 3. True acquisition cost

The number the brief is right to insist on, and the one that most distinguishes this product
from a listings site.

```
true_acquisition_cost = Σ line_items
```

| Line item | Typical basis | Notes |
|---|---|---|
| Seller payment / winning bid | ACTUAL | What actually changes hands to the seller |
| Remaining developer installments | ACTUAL → RULE → **UNKNOWN** | The assignment killer. `UNKNOWN` blocks the discount |
| Auction commission | RULE | Per the auction operator's published terms |
| Brokerage | RULE | Configurable; market-standard default |
| Real Estate Transaction Tax / VAT | RULE | Rule table with effective dates and exemptions; **must be reviewed by counsel and re-verified per rate change, not hard-coded once** |
| Registration / deed transfer | RULE | |
| Renovation | ESTIMATE | Condition-band × area × SAR/m² table |
| Known liabilities | ACTUAL / UNKNOWN | Outstanding fees, occupancy encumbrances |

**Hard invariant, enforced in the domain type and the database:**

```
if any(item.basis == UNKNOWN and item.material for item in items):
    discount = REFUSED          # not estimated, not zero, not omitted
```

The UI then shows the cost breakdown with the gap named explicitly ("remaining installments
unknown — required before a discount can be computed") and prompts the analyst to supply it.
A refusal that tells the user exactly what is missing is more useful than a confident guess,
and it is the difference between a tool professionals trust and one they check twice.

```
discount = (fair_value_base − true_acquisition_cost) / fair_value_base
```

Never against advertised price. Never against seller ask alone.

---

## 4. Rental estimate and yield

```
annual_rent   = WeightedMedian(rental comps, same kernels) × area_p
gross_yield   = annual_rent / true_acquisition_cost
net_yield     = (annual_rent × occupancy − opex) / true_acquisition_cost
```

Defaults, all displayed and all overridable by the user — an assumption the user cannot see is
an assumption they cannot disagree with:
`occupancy = 0.92`, `opex = service charges + 8% management + 5% maintenance reserve`.

---

## 5. Opportunity score

### 5.1 Component normalisation

Each dimension maps to 0–100 by an explicit, monotonic, piecewise-linear function. No neural
scoring, no learned black box, no LLM judgement.

**Discount** (`d` = discount fraction):

| d | ≤0 | 0.05 | 0.10 | 0.15 | 0.20 | 0.25 | ≥0.35 |
|---|---|---|---|---|---|---|---|
| score | 0 | 25 | 50 | 68 | 80 | 88 | 100 |

**Rental** (`y` = gross yield): `0→0, 0.04→30, 0.06→55, 0.07→70, 0.08→82, ≥0.10→100`

**Liquidity:** `0.4·transaction_density_percentile + 0.3·median_days_on_market_score + 0.3·district_turnover_score`

**Location:** district composite — price trend, amenity access, infrastructure pipeline, school
and transport proximity. Recomputed nightly per district, not per property.

**Developer / project:** delivery track record, Wafi licence status, escrow compliance,
completion history, price retention of prior projects. Unknown developer ⇒ 50, not 0 — absence
of evidence is not evidence of a bad developer, and defaulting to 0 would silently bury
legitimate opportunities.

**Risk:** `100 − weighted(legal, occupancy, developer, construction, liquidity, market, valuation_uncertainty, data_quality, auction)`

**Confidence:** `100 × data_confidence` (§5.3).

### 5.2 Aggregation

```
total = Σ (weight_i × component_i)      # weights from the org's profile, default per brief
```

Default profile (`v1`): discount 0.30 · liquidity 0.15 · rental 0.15 · location 0.10 ·
developer 0.10 · risk 0.10 · confidence 0.10.

The profile is a versioned row. `OPPORTUNITY_SCORES` stores `weight_profile_version`, making
every historical score reproducible and every ranking change explainable ("the score moved
because the weights changed, not because the market did").

### 5.3 Data confidence

```
data_confidence =
      0.30 · confidence_valuation
    + 0.20 · cost_completeness          # fraction of material line items with basis ≠ UNKNOWN
    + 0.20 · verification_score         # official checks passed with evidence
    + 0.15 · source_confidence
    + 0.15 · field_completeness
```

### 5.4 Classification and the confidence gate

```
if data_confidence < 0.60:  →  INSUFFICIENT_DATA        # no score headline, no recommendation
elif data_confidence < 0.75: →  cap classification at "Worth reviewing"
else:
    ≥90 Exceptional · 80–89 Strong · 70–79 Worth reviewing · 60–69 Watchlist · <60 Weak
```

The cap in the middle branch is the specific mechanism that prevents the failure the brief
warns about: a thinly-evidenced property scoring 91 on an optimistic discount and being
presented as "Exceptional". Under this rule it presents as "Worth reviewing" with its
confidence stated — which is what an honest analyst would say.

### 5.5 Reproducibility contract

Asserted by golden-fixture tests in CI:

1. Same inputs + same `method_version` + same `weight_profile_version` ⇒ **bit-identical** score.
2. Every component exposes the complete input set that produced it (`score_components.inputs`).
3. Changing a method version requires a new version identifier and a backfill decision recorded
   in the migration. Silent recalculation of historical scores is prohibited.
4. A score row is never updated. Re-evaluation inserts and marks the prior row superseded.

---

## 6. Worked example (the brief's scenario)

Sidrah, Riyadh · apartment · 140 m² · assignment (تنازل)

| Input | Value | Basis |
|---|---|---|
| Seller required payment | SAR 120,000 | ACTUAL — submission |
| Remaining developer installments | SAR 600,000 | ACTUAL — developer statement |
| Transfer + registration + brokerage | SAR ~0 (per structure) | RULE |
| **True acquisition cost** | **SAR 720,000** | derived |
| Comparables (24 mo, ≤1.5 km, 91–210 m²) | 17, `n_effective` 11.4 | — |
| Weighted median adj. price/m² | SAR 6,500 | — |
| **Fair value base / band** | **SAR 910,000** (868k–952k) | — |
| **Discount** | **20.9%** → component 82 | — |
| Annual rent (weighted median) | SAR 61,000 → gross yield 8.5% → component 86 | — |
| Liquidity 84 · Location 88 · Developer 76 · Risk 80 (low legal) | | |
| Valuation confidence 0.86 · cost completeness 1.0 · verification 0.8 | **data_confidence 0.86** | |

```
total = 0.30(82) + 0.15(84) + 0.15(86) + 0.10(88) + 0.10(76) + 0.10(80) + 0.10(86)
      = 24.6 + 12.6 + 12.9 + 8.8 + 7.6 + 8.0 + 8.6 = 83.1  →  Strong
```

Note what the arithmetic does that a naive system would not: had `remaining_installments` been
unknown, the same property would have shown a seller ask of SAR 120,000 against a SAR 910,000
valuation — an apparent **87% discount**, and complete nonsense. The invariant in §3 is the
single most valuable line of logic in the platform.

---

## 7. Validation before launch

| Method | Test |
|---|---|
| Back-testing | Value properties as of `T`, compare against their actual sale at `T+n`. Target: median absolute error ≤ 12%, and — more importantly — an interval that contains the realised price ≥ 70% of the time |
| Analyst blind review | 20 properties; ≥80% agreement with the fair-value band |
| Auction outcome tracking | Record opening price, our estimate, and final hammer price. This is the dataset nobody else has, and it is what eventually turns the opportunity graph into a moat |
| Confidence calibration | Bucket predictions by stated confidence; verify realised accuracy tracks the claim. **A confidence score that is not calibrated is worse than none**, because it invites trust it has not earned |
