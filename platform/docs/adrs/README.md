# Architecture Decision Records

Each ADR states **Problem · Options considered · Decision · Why · Trade-offs · Revisit when**.
The "revisit when" section is mandatory: a decision without a falsification condition is a
preference, not an engineering decision.

| ADR | Decision | Status |
|---|---|---|
| [001](ADR-001-modular-monolith.md) | Modular monolith with pre-planned extraction seams | Accepted |
| [002](ADR-002-postgresql-postgis.md) | PostgreSQL 16 + PostGIS as the single primary store | Accepted |
| [003](ADR-003-agent-orchestration.md) | Purpose-built agent runtime; no LLM framework | Accepted |
| [004](ADR-004-workflow-engine.md) | Postgres-backed durable jobs; Temporal deferred | Accepted |
| [005](ADR-005-search-architecture.md) | Postgres FTS + trigram + pgvector; no separate search cluster | Accepted |
| [006](ADR-006-llm-provider-abstraction.md) | Thin provider abstraction over OpenAI/Anthropic/Gemini/local | Accepted |
| [007](ADR-007-data-provenance.md) | Field-level provenance as a first-class domain type | Accepted |
| [008](ADR-008-source-ingestion-policy.md) | Source ingestion policy enforced in code and CI | Accepted |
| [009](ADR-009-kubernetes-deployment.md) | Kubernetes + Terraform, cloud-agnostic, in-Kingdom region | Accepted |
| [010](ADR-010-opportunity-scoring.md) | Deterministic, versioned, reproducible scoring | Accepted |
