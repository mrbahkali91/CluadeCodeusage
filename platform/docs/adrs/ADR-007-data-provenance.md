# ADR-007: Field-level provenance as a first-class domain type

**Status:** Accepted · **Date:** 2026-08-29

## Problem
The product's core claim is "this asset is worth Y, and here is why". Users, and eventually
regulators, will ask where any given number came from. Where does provenance live?

## Options considered
1. **Audit logging** — record what happened; reconstruct provenance from logs when asked.
2. **Row-level provenance** — each table row carries `source_id` and `retrieved_at`.
3. **Field-level provenance as a domain type** — `Provenanced[T]` carrying value, confidence,
   basis, source record and evidence, plus a `data_provenance` table for querying.

## Decision
Option 3. Every externally-derived value in the domain is `Provenanced[T]`. API responses
serialise money and derived fields as `{value, unit, confidence, basis, sources[]}`.

## Why
- **A property record is assembled from several sources at once.** Its area may come from an
  auction PDF, its price from a listing, its coordinates from geocoding, its district from a
  polygon. Row-level provenance (option 2) cannot express that, and it is the normal case, not
  an edge case.
- Provenance in logs (option 1) is unqueryable in practice and rots. "Show me every opportunity
  whose discount depends on an *estimated* installment figure" is a question we will be asked,
  and it must be a query.
- The type makes the crucial safety property structural rather than procedural: a
  `TrueAcquisitionCost` containing an `UNKNOWN` material item **cannot** yield a discount,
  because the refusal is encoded where the data is, not in a validation someone might forget.
- Confidence propagation is defined once and reused rather than reinvented per feature.

## Trade-offs
- **Accepted:** more storage and a more verbose domain. Justified — this *is* the product.
- **Accepted:** serialisation is heavier than bare numbers. The SDK is generated, so clients
  absorb this without hand-written mapping.
- **Accepted:** developers must thread provenance through new code. Made hard to bypass by
  typing money fields so a bare `Decimal` does not compile into them.

## Revisit when
Never for the core financial fields. Non-financial descriptive fields may drop to row-level
provenance if the write-path cost becomes measurable.
