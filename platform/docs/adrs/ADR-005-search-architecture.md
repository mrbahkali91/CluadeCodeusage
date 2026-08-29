# ADR-005: Postgres FTS + trigram + pgvector; no separate search cluster

**Status:** Accepted · **Date:** 2026-08-29

## Problem
We need filtered search over opportunities (structured predicates plus geospatial), keyword
search over Arabic and English listing text, and semantic similarity for deduplication and
natural-language search.

## Options considered
1. **OpenSearch/Elasticsearch** — strongest relevance and analyzer tooling, including Arabic.
2. **Postgres FTS + `pg_trgm` + `pgvector`.**
3. **A hosted search API** — fast to adopt, but a third party holding our index.

## Decision
Option 2 for MVP. Option 1 stays designed-for: search sits behind a `SearchIndex` port with a
documented reindex path from Postgres as source of truth.

## Why
- **Search here is predominantly filtering, not relevance ranking.** "Apartments, four districts,
  under 1.2M, ≥15% discount, ≥80 score" is a structured query with a geospatial predicate.
  Postgres executes that natively and joins it to the map query. Elasticsearch would need every
  filter field mirrored and kept in sync, for no gain on the dominant access pattern.
- Free-text search is a secondary path over a modest corpus, well served by `tsvector` plus
  trigram similarity for the fuzzy Arabic transliteration cases (Qurtubah / Qurtuba / قرطبة).
- Avoids a second stateful system, its sync pipeline, and an entire class of
  "index is stale/diverged" bugs.
- Semantic search and dedup embeddings live in `pgvector`, adjacent to the rows they describe —
  which keeps deduplication a single query rather than a cross-system fan-out.

## Trade-offs
- **Accepted:** weaker Arabic stemming than a tuned Elasticsearch analyzer. Mitigated with a
  custom dictionary, transliteration normalisation, and trigram fallback. Monitored via
  zero-result-rate on Arabic queries — a concrete metric with a threshold, so the decision to
  move is data-driven rather than aesthetic.
- **Accepted:** no faceting engine; facets are computed with grouped aggregate queries against
  materialised views.

## Revisit when
Arabic zero-result rate exceeds 5% of queries and is attributable to analysis quality, or
free-text corpus exceeds ~10M documents, or relevance tuning becomes a recurring product ask.
