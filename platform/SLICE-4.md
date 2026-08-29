# Slice 4a — The agent runtime and the verification agent

**Status: complete and running.** The agentic core is in place: a purpose-built
runtime with structured output, cost ceilings, idempotency and full audit; a provider
abstraction that defaults to offline; layered prompt-injection defences with an adversarial
test corpus; and a verification agent whose checks are deterministic and whose official checks
refuse to pretend.

## Verified in this environment

| Check | Result |
|---|---|
| Test suite | **150 passed** (108 → 150), **order-independent** (random ordering enabled permanently) |
| `mypy --strict` | **clean, 52 files** |
| `ruff` | **clean** |
| Architecture contracts | **2 kept, 0 broken** |
| Migrations | 23 tables up → 0 down → 23 up; `alembic check` clean |
| Verification | 567 checks across 56 opportunities; 69 agent runs, all recorded |

## What changed for the product

Verification was the input pinned at zero since Slice 1, costing 0.20 of data confidence
outright. Wiring it moved the numbers:

| | Slice 2 | Slice 4a |
|---|---|---|
| Actionable (rated) | 28 | **35** |
| `INSUFFICIENT_DATA` | 28 | **21** |
| Mean data confidence | 0.596 | **0.628** |

`area_coherence` also found **14 records** whose stated area is implausible for their stated
bedroom count — the checker doing exactly the job it exists for.

## The design decision worth arguing about

**Internal coherence and official confirmation are not interchangeable, so they are not scored
as if they were.**

Four checks run today and genuinely work: district geometry (PostGIS `ST_Covers` — does the
coordinate actually fall in the claimed district?), price plausibility against the district's
own 5th–95th percentile, cross-source agreement between independent listings, and area/bedroom
coherence. These catch fabricated and incoherent data, which is real verification value.

They are still not a government register saying "this advertisement is licensed". So:

```
verification_score = 0.35 × internal_coherence + 0.65 × official_confirmation
```

With no official register integrated, verification **cannot exceed 0.35** no matter how many
internal checks pass, and the UI says so on the page. The alternative — letting "our own data
agrees with itself" count as full verification — would have produced a much better-looking demo
and would have been dishonest.

The three official checkers (REGA advertisement licence, Wafi project, developer registry) are
implemented, declared, carry a recorded legal basis, and return `UNAVAILABLE` with the reason.
**An unperformed check is not a passed check.**

## Prompt injection: demonstrated, not asserted

The same property submitted twice, once with an injection payload embedded in its description
(English override, `system:` role marker, a delimiter escape, and an Arabic
`تجاهل كل التعليمات السابقة`):

| | clean | attacked |
|---|---|---|
| Opportunity score | 63.91 | **63.91** |
| Data confidence | 0.6692 | **0.6692** |
| Official checks marked verified | 0 | **0** |
| Injection flagged on the run | — | **yes, 5 patterns** |

The score is identical because it never depended on the model: money numbers are computed by
deterministic code from validated fields, so an injection can at worst corrupt an *input*, and
range validation catches that. The defences are layered — framing, no tools on untrusted paths,
schema constraint, post-model validation, deterministic supremacy, detection and quarantine —
and each layer is tested separately.

## Honest about the LLM

**No model was called.** There are no credentials in this environment, and `default_provider`
deliberately never selects a cross-border provider implicitly. The runtime therefore runs on a
`DeterministicProvider` — an offline stand-in that satisfies the port, is recorded as
`provider="deterministic-offline"` on every run, and cannot be mistaken later for model
reasoning.

That is not a workaround; it is the design working. Cross-border inference is the specific PDPL
transfer risk the provider abstraction exists to contain (ADR-006). The Anthropic and OpenAI
adapters are wired and **fail loudly** when unconfigured rather than silently skipping, so
nobody ships believing inference ran.

What this does prove, deterministically and in CI: schema validation with exactly one repair
attempt then failure, per-call and per-run cost ceilings, idempotency (a retried pipeline is
served from cache and does not pay twice), full recording to `agent_runs` / `agent_decisions` /
`llm_calls`, and post-model validation rejecting a summary that miscounts the checks it claims
to describe.

## Defects found and fixed

1. **A cartesian-product query** in the district-geometry checker — correct by accident because
   both sides were constrained to one row, but it warned and would have been wrong the moment a
   filter changed. Now an explicit join.
2. **Non-verified checks lost their class**, so internal checks displayed as `OFFICIAL` with an
   empty finding. Evidence is now recorded for every outcome, not only passes: why a check did
   not apply is itself an auditable fact.
3. **Tests were not isolated.** API tests commit, so a later test's property silently matched
   one an earlier module had committed and entity resolution merged them — assertions became
   order-dependent. There is now an `isolated` fixture that truncates the property graph while
   keeping reference data, and **random test ordering is permanently enabled** so a regression
   surfaces immediately rather than on someone else's machine.

## Running it

```bash
make db-up && make migrate && make seed && make corpus
make run     # verification panel on any opportunity detail page
make check
```

New endpoints: `GET /api/v1/opportunities/{id}/verification` (checks with evidence and the
score ceiling) and `GET /api/v1/admin/agents` (runs, cost, cost per opportunity, injection
flags).

## What is still not done

- **No official register is integrated**, so verification stays capped at 0.35. Lifting that cap
  needs the REGA and Wafi interfaces, which are `REQUIRES VALIDATION` in the source matrix.
- **No real model has been called.** The adapters need credentials, a recorded cross-border
  transfer assessment, and a first live run before anyone should trust the LLM path.
- Extraction, document intelligence and the investment memo (the rest of Slice 4) are not built.
- **Assumption A-01 remains unvalidated**; both corpora are still synthetic and labelled.
