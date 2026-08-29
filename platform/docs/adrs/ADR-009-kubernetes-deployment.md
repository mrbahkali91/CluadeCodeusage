# ADR-009: Kubernetes + Terraform, cloud-agnostic, in-Kingdom region

**Status:** Accepted · **Date:** 2026-08-29

## Problem
We need a deployment target that supports independent worker pools, is reproducible, and can be
placed in a Saudi region for PDPL residency — while noting that in-Kingdom region availability
and managed-service parity vary by provider.

## Options considered
1. **Managed Kubernetes + Terraform** on a provider with an in-Kingdom region.
2. **Serverless / PaaS** (managed containers, functions) — least operations.
3. **VMs + systemd/Compose** — simplest, least abstraction.

## Decision
Option 1. Managed Kubernetes, Terraform for all infrastructure, Helm/Kustomize for workloads,
managed PostgreSQL where an in-Kingdom managed PostGIS-capable offering exists — otherwise
Postgres on Kubernetes with an operator, with the trade-off consciously accepted.

## Why
- **Distinct worker classes need distinct scaling and isolation**: agent workers are
  LLM-latency-bound and mostly idle CPU; document workers are CPU-heavy and bursty; ingestion
  workers are network-bound. Kubernetes expresses that with node pools and HPAs; a PaaS usually
  does not, and VMs would mean building it ourselves.
- Portability is not theoretical here. If a residency assessment forces a provider change,
  Terraform plus Kubernetes manifests make that a migration rather than a rebuild.
- Long-running workers with in-process state and durable connections fit poorly with
  function-style serverless (option 2), and cold starts would hurt pipeline latency.

## Trade-offs
- **Accepted:** Kubernetes is operationally heavier than a PaaS. Mitigated by using managed
  control planes and managed data services, and by keeping only two application deployables.
- **Accepted:** in-Kingdom regions may lag on managed-service breadth; self-managed Postgres is
  a real possibility and its backup/PITR burden is planned for, not discovered later.
- **Accepted:** cost floor is higher than serverless at very low traffic.

## Revisit when
Residency requirements relax and traffic stays low enough that a PaaS would be materially
cheaper, or the team shrinks below the point where a cluster can be responsibly operated.
