# ADR-010: Deterministic, versioned, reproducible opportunity scoring

**Status:** Accepted · **Date:** 2026-08-29

## Problem
The Opportunity Score is the product's primary output and the basis on which users may commit
large sums. How is it computed, and how do we make it trustworthy?

## Options considered
1. **LLM-generated score** — an agent reads everything and produces 0–100 with a rationale.
2. **Learned model** (gradient boosting) trained on outcomes.
3. **Explicit weighted model** with piecewise-linear normalisation, versioned weights, and a
   confidence gate.

## Decision
Option 3, exactly as specified in
[valuation-and-scoring](../architecture/valuation-and-scoring.md) §5. Pure functions in
`packages/domain/scoring`, no I/O, no LLM, golden-fixture reproducibility tests in CI.
Weights are per-organisation versioned profiles; the version is stored on every score row.

## Why
- **Reproducibility is a product requirement, not an engineering preference.** A user who asks
  "why did this drop from 87 to 81 overnight?" must get a component-level answer. An LLM score
  (option 1) cannot provide that, cannot be regression-tested, and would drift silently with
  every model update — which is disqualifying for a number people spend money on.
- **We have no outcome labels yet.** A learned model (option 2) needs realised outcomes —
  auction hammer prices, resale prices — and we will not have enough for years. Training on
  proxies would encode our own assumptions while *removing* the ability to inspect them: the
  worst of both approaches.
- Explicit weights are the honest representation of what the score is: a stated investment
  thesis. Different investors hold different theses, which is why weights are per-organisation.
- Deterministic scoring is also the structural defence against prompt injection
  (security §5.5): text from a listing cannot move a score that text does not feed.

## Trade-offs
- **Accepted:** hand-tuned weights are less accurate than a well-trained model would eventually
  be. We prefer explainable and roughly right to opaque and possibly precise.
- **Accepted:** piecewise-linear normalisation has discontinuous gradients at knots. Irrelevant
  for ranking, and the knots are documented and reviewable.
- **Accepted:** more product work to expose components in the UI. That exposure *is* the feature.

## Path forward, not a reversal
Once the auction-outcome dataset is large enough (target ≥ 500 matched estimate/outcome pairs),
a learned model may be introduced as a **challenger scored alongside** the explicit model, with
divergence reported. It replaces the primary score only if it outperforms on back-testing **and**
its outputs can be attributed at component level. An unexplainable improvement is not an
improvement for this product.

## Revisit when
The challenger condition above is met, or a customer segment needs a thesis the weight profile
mechanism cannot express.
