# Agent Architecture

---

## 1. The rule that governs every agent

> **An LLM may read, extract, reconcile, classify and explain. It may never originate a number
> that reaches a money field, and it may never decide that something is verified.**

Applied honestly, this leaves six agents. The brief lists eleven; five of its proposed agents
are better implemented as deterministic modules, and saying so is more useful than dressing
SQL in agent clothing.

| Brief's agent | Verdict | Where the logic actually lives |
|---|---|---|
| Discovery | **Not an agent** | Scheduler + connectors + a signal-term classifier (small model) for unstructured text only |
| Extraction | **Agent** ✓ | LLM with strict structured output |
| Property Resolution | **Hybrid** ✓ | Deterministic blocking + scoring; LLM adjudicates only the ambiguous band |
| Verification | **Agent** ✓ | Orchestrates official lookups, reconciles conflicts, cites evidence |
| Valuation | **Not an agent** | §1–2 of [valuation-and-scoring](valuation-and-scoring.md). Pure functions |
| Rental | **Not an agent** | Same comparable machinery over rental data |
| True Cost | **Not an agent** | Rule table + arithmetic. LLM only *extracts* the installment figure into a provenanced field |
| Risk | **Mostly rules** | Rule engine; LLM contributes narrative red flags from document text, never the numeric risk |
| Opportunity Scoring | **Never an agent** | Pure function. An LLM-generated score would be unreproducible and therefore unusable |
| Watch | **Agent** ✓ | Autonomy genuinely lives here: what changed, does it matter, is it worth interrupting a human |
| Investment Memo | **Agent** ✓ | Synthesis over computed numbers |

Plus one the brief does not name but the product needs: **Natural-Language Search Agent**,
compiling a request into structured filters the user can see and edit.

**Where the system is genuinely agentic** is not the count of agents. It is the autonomy loop:
the platform decides *on its own* what to look at, how much analysis a candidate deserves, when
evidence is contradictory enough to withhold a claim, and when a change is material enough to
wake a human. That loop is the product.

---

## 2. Runtime

No LangChain, LlamaIndex or equivalent (ADR-003). The runtime is roughly 300 lines:

```python
class Agent[TIn, TOut](Protocol):
    name: str
    output_schema: type[TOut]          # Pydantic model
    model_tier: ModelTier              # SMALL | STANDARD | LARGE
    max_cost_usd: Decimal

    async def run(self, ctx: AgentContext, inp: TIn) -> AgentResult[TOut]: ...
```

Every execution is wrapped by the runtime, which provides the guarantees uniformly:

1. **Structured output only.** Response is parsed into the Pydantic schema. Invalid ⇒ one
   repair retry with the validation error ⇒ then fail. **Never coerce, never partially accept.**
2. **Untrusted-input framing.** External text enters inside a delimited block with a standing
   instruction that its content is data, never instruction (see
   [security](../security/security-architecture.md) §5).
3. **Cost ceiling.** Per-call and per-pipeline budgets. Exceeding either aborts the run and
   emits a metric rather than quietly spending.
4. **Full recording.** `agent_runs`, `agent_decisions`, `llm_calls` — prompt hash, model,
   tokens, cost, latency, schema validity, retries. Reconstructable after the fact.
5. **Determinism where possible.** `temperature = 0` for extraction and classification; seeds
   recorded where the provider supports them.
6. **Provider-agnostic.** ADR-006. Model choice is per-agent config, not code.
7. **Idempotent.** Keyed on `(agent, input_hash, prompt_version, model)`. A pipeline retry
   re-uses the prior result instead of paying twice.

---

## 3. Agent specifications

### 3.1 Extraction Agent — `SMALL`/`STANDARD`

**Input:** raw listing text, auction lot description, or document extraction (Arabic or English).
**Output:** a typed record where **every field** is `{value, confidence, evidence_span}`.

Constraints that matter:

- A field not supported by a span in the source is `null`, **not inferred**. "3 bedrooms"
  cannot be guessed from a 140 m² area.
- Numerics are range-validated (area 20–10,000 m²; price 10k–500M SAR; floor −3–80). Out of
  range ⇒ null + quality flag, never clamped.
- Arabic real-estate vocabulary is handled explicitly in the prompt contract: تنازل
  (assignment), عاجل / للبيع بسرعة (urgent), مزاد (auction), إفراغ فوري (immediate transfer),
  دفعة (installment), على الشارع (street-facing), درج (staircase/duplex), شقة/فيلا/دور/أرض.
  These are the terms that carry the opportunity signal, and mistranslating تنازل as a plain
  sale is exactly how a system produces an 87% phantom discount.
- Numerals: Eastern Arabic (٠١٢٣٤٥٦٧٨٩) normalised at the boundary before extraction.
- Hijri dates parsed and stored alongside their Gregorian conversion.

### 3.2 Property Resolution Agent — hybrid, `SMALL` on ambiguity only

Deterministic first, because it is cheaper and more reliable:

```
blocking:  same district AND area within ±8% AND same property_class
scoring:   0.30 spatial (≤50 m) + 0.20 area + 0.15 project+unit
         + 0.15 attribute agreement (beds/floor/age) + 0.10 price proximity
         + 0.10 text embedding cosine (pgvector)
```

`≥0.85` auto-merge · `0.60–0.85` LLM adjudication, then human review if still uncertain ·
`<0.60` distinct. Merges are reversible and preserve both timelines. Images are **not** used
for matching unless the source licence permits it.

### 3.3 Verification Agent — `STANDARD`

Orchestrates official lookups (ad licence, Wafi project, developer registry, auction validity,
location consistency) and reconciles disagreement.

Non-negotiable: **`VERIFIED` requires a stored evidence record.** Enforced by a database check
constraint, not merely agent instructions — an agent's output is not a trustworthy place to put
a safety property. Absence of a lookup is `UNVERIFIED`, never `VERIFIED`. A conflict between
official sources is `CONFLICTED` and is surfaced, not silently resolved in favour of the more
convenient answer.

### 3.4 Watch Agent — `SMALL`, escalating to `STANDARD`

The genuinely autonomous component. Re-evaluates on schedule and on event, then decides
materiality:

- Deterministic triggers: price reduction >3%, score change >5 points, auction ≤48h from close,
  bid crossing the user's recommended maximum, a new comparable moving the fair-value band >2%.
- The agent's job is the harder question — **is this worth a human's attention?** Three
  successive reductions on one unit is a story; three reductions across a district is a market
  move and belongs in the digest, not an alert.
- Alert fatigue is treated as a defect. `alert_precision` is a tracked metric with user feedback,
  and a rule whose precision falls below threshold is automatically flagged for tuning.

### 3.5 Investment Memo Agent — `LARGE`, gated

Runs only at `score ≥ 70 AND confidence ≥ 0.60` — roughly the top few percent of properties,
which is what keeps average cost per opportunity inside the SAR 2.50 target.

**Fails closed:** every figure in the memo must resolve to a computed field id. A memo citing a
number the system cannot produce is rejected and regenerated once, then abandoned with an error.
The memo is not permitted to introduce a price, a yield, or a maximum bid of its own — including
the "maximum recommended purchase price", which is computed as
`fair_value_low × (1 − target_margin)` and merely narrated by the agent.

### 3.6 Natural-Language Search Agent — `SMALL`

Compiles a request into a structured filter object, **always shown to the user before results**:

> "apartments in Riyadh under SAR 1.2M, ≥15% discount, good rental demand, no major legal
> complexity, around Qurtubah/Sidrah/Al Munsiyah/Al Rimal"

```json
{
  "property_type": "APARTMENT",
  "city": "Riyadh",
  "districts": ["Qurtubah", "Sidrah", "Al Munsiyah", "Al Rimal"],
  "max_true_acquisition_cost": 1200000,
  "min_discount_pct": 15,
  "min_gross_yield": 0.06,
  "max_legal_risk": "LOW",
  "_interpreted": {
    "good rental demand": "min_gross_yield ≥ 6% (district median)",
    "no major legal complexity": "legal_risk ≤ LOW"
  }
}
```

The `_interpreted` block is the whole point: vague terms are made explicit and editable. The
agent never queries the database directly — it emits a validated filter object that the
deterministic search layer executes, which also means prompt injection cannot reach SQL.

---

## 4. Cost control

Staged processing, as the brief requires, with the gates made concrete:

| Stage | Population | Model | ~Cost/property |
|---|---|---|---|
| Deterministic filter | 100% | — | 0 |
| Signal classification | unstructured only | SMALL | ~SAR 0.02 |
| Structured extraction | unstructured only | SMALL/STANDARD | ~SAR 0.15 |
| Document intelligence | with documents | STANDARD | ~SAR 0.80 |
| Resolution adjudication | ambiguous band only | SMALL | ~SAR 0.03 |
| Verification | score ≥60 or watched | STANDARD | ~SAR 0.20 |
| Investment memo | score ≥70 ∧ conf ≥0.60 | LARGE | ~SAR 1.50 |

Tracked as `cost_per_property`, `cost_per_opportunity`, `cost_per_active_user`,
`cost_per_alert`. Budget breach raises an alert and degrades to smaller models rather than
failing the pipeline — a slightly worse memo beats no product.

Caching: prompt-prefix caching for stable system prompts; result caching keyed on
`(agent, input_hash, prompt_version, model)`; embeddings computed once and stored.

---

## 5. Failure behaviour

| Failure | Response |
|---|---|
| Schema validation fails twice | Quarantine record; deterministic fields still flow; opportunity shown with reduced confidence |
| Provider outage | Fail over to the next configured provider (ADR-006); on total outage the deterministic pipeline still produces scored opportunities without memos |
| Cost ceiling hit | Abort agent, keep deterministic results, emit metric |
| Injection detected | Discard output, flag the source record, alert admin, quarantine the source if repeated |
| Agent disagrees with deterministic result | **Deterministic wins.** Disagreement is logged as a data-quality signal — a persistent pattern of disagreement is a bug report about our rules, and one of the more valuable signals the system generates |
