# ADR-004: Postgres-backed durable jobs; Temporal deferred

**Status:** Accepted · **Date:** 2026-08-29

## Problem
The evaluation pipeline is a multi-step DAG per property with retries, partial failure and
resumption. Scheduled ingestion and the watch loop need durable scheduling. The brief suggests
Temporal "or a strong alternative".

## Options considered
1. **Temporal** — durable execution, first-class retries, replay, visibility.
2. **Celery / RQ / Dramatiq + Redis** — familiar task queues.
3. **Postgres-backed durable queue** (`FOR UPDATE SKIP LOCKED`) + an explicit `pipeline_runs`
   DAG state table.

## Decision
Option 3 for Phase 1–5, with explicit adoption triggers for Temporal.

## Why
- **Shape of the work:** a short (seconds to minutes), idempotent DAG per property. That is not
  the long-lived, human-in-the-loop, compensation-heavy workflow Temporal is built to excel at.
- **Transactional consistency is worth more here than workflow features.** A pipeline step's
  state change and its domain write commit in the *same transaction*. With an external
  orchestrator, workflow state and domain state can diverge — and reconciling that divergence is
  precisely the class of bug that would undermine auditability.
- Temporal is a cluster plus a database plus workers plus a new on-call surface. For a small
  team in Phase 1 that operational cost is not yet earned.
- `SKIP LOCKED` is a mature, boring, well-understood pattern. Boring is a feature on the
  critical path.
- Redis-backed queues (option 2) were rejected for durability: our jobs must survive a Redis
  restart, and layering durability onto Redis reproduces what Postgres already gives us.

## Trade-offs
- **Accepted:** we build our own retry/backoff, scheduling and visibility. Mitigated by keeping
  the DAG explicit in a table, which makes a Grafana view straightforward.
- **Accepted:** no free replay or time-travel debugging. Partially compensated by append-only
  raw records — we can always re-run a pipeline from original bytes, which is arguably better
  evidence than a replay log.
- **Accepted:** Postgres carries queue load. At our volume this is thousands of jobs/day, not
  millions; partitioned and vacuumed appropriately it is not a concern.

## Revisit when — adopt Temporal if **any** holds
- A workflow spans > 24 hours or requires human approval steps mid-flight.
- Compensating transactions across external systems become necessary (e.g. partner-side actions).
- Job volume exceeds ~1M/day and queue contention appears in Postgres wait statistics.
- More than three teams need independent workflow visibility.
