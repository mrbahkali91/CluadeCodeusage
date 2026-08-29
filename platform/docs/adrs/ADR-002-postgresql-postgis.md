# ADR-002: PostgreSQL 16 + PostGIS as the single primary store

**Status:** Accepted · **Date:** 2026-08-29

## Problem
We need relational integrity for the opportunity graph, geospatial queries (radius, polygon,
nearest-neighbour, boundary containment), semi-structured storage for heterogeneous raw
payloads, full-text search across Arabic and English, vector similarity for deduplication and
NL search, and robust statistical aggregation over millions of transactions.

## Options considered
1. **Postgres + PostGIS + JSONB + FTS + pgvector** — one engine.
2. **Postgres + Elasticsearch + a vector database + a document store** — best-of-breed per concern.
3. **A geospatial-first alternative** (e.g. a dedicated spatial platform) with Postgres alongside.

## Decision
Option 1. PostgreSQL 16 with PostGIS, JSONB for raw payloads, native FTS with `pg_trgm`, and
`pgvector` with HNSW indexes. Object storage (S3-compatible) holds only large binaries — PDFs
and oversized raw payloads — referenced by key.

## Why
- PostGIS is the strongest open-source geospatial engine available and the geospatial
  requirement is central, not incidental. Choosing anything else would mean re-implementing it.
- **The comparable engine is a join** between a property, transactions within a radius, a
  district polygon and a time-series index. In one engine that is a single query with a GIST
  index. Across four systems it is application-side joins, consistency headaches, and a
  latency budget we cannot meet.
- Transaction-level consistency between a valuation and the comparables it cites is an
  auditability requirement (ADR-007). Cross-store, that guarantee is gone.
- One engine to operate, back up, and reason about.

## Trade-offs
- **Accepted:** Postgres FTS is weaker than Elasticsearch on relevance tuning and analyzer
  breadth, notably for Arabic morphology. Addressed in ADR-005 with a concrete escape hatch.
- **Accepted:** `pgvector` HNSW is adequate to tens of millions of vectors — well beyond our
  projection — but is not a specialised vector database.
- **Accepted:** vertical scaling limits eventually apply. Read replicas, partitioning and
  materialised views defer this well past the MVP horizon.

## Revisit when
- Search relevance complaints trace specifically to Arabic analysis that `pg_trgm` plus a
  custom dictionary cannot fix, **or**
- transaction volume exceeds ~50M rows with comparable queries above 1s p95 after partitioning
  and materialised comparable tiles.
