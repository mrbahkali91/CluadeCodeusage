# ADR-001: Modular monolith with pre-planned extraction seams

**Status:** Accepted · **Date:** 2026-08-29

## Problem
The platform spans ingestion, geospatial analysis, valuation, agent execution, document
processing, alerting and a web product. That surface tempts a service-per-concern
decomposition. We must choose a deployment topology before Phase 1 because it is expensive to
reverse once teams form around it.

## Options considered
1. **Microservices from day one** — service per bounded context, async messaging.
2. **Single monolith** — one deployable, no internal boundaries.
3. **Modular monolith** — one API deployable + one worker deployable, strict internal module
   boundaries, pre-identified extraction seams.

## Decision
Option 3. `apps/api` and `apps/worker` share `packages/*`. `packages/domain` is pure and may
not import any I/O, framework, or adapter package; this is enforced by an import-linter rule
in CI. Extraction seams are identified in advance: document processing, agent execution, ingestion.

## Why
- The dominant early risk is **data availability and valuation correctness**, not scale.
  Microservices would spend the team's budget on distributed-systems problems we do not have
  while the actual product question (is the data obtainable? is the number right?) went unanswered.
- The evaluation pipeline is **transactionally coupled**: a property, its valuation, its score
  and its provenance should commit together. Across services that becomes a saga with
  compensations — significant complexity to solve a problem a single transaction solves for free.
- A small team pays the operational cost of every additional runtime weekly, forever.
- The boundary that actually delivers the benefit of microservices — a pure domain that cannot
  reach out and touch a database — is achievable inside a monolith and is where most of the
  value lies.

## Trade-offs
- **Accepted:** components scale together; a worker memory leak can affect other workers in the
  same pool (mitigated by separate pools per workload class); language choice is uniform.
- **Rejected risk:** boundary erosion under delivery pressure. Mitigated by the CI import rule —
  a violation fails the build, so erosion is a visible act rather than a gradual drift.

## Revisit when
- Sustained ingestion > 10M records/day, **or**
- document processing > 30% of worker CPU, **or**
- more than three teams contribute, **or**
- one workload's scaling profile diverges enough that co-deployment wastes >40% of resources.
