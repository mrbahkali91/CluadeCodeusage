# ADR-006: Thin LLM provider abstraction

**Status:** Accepted · **Date:** 2026-08-29

## Problem
Agents need model access. Provider capabilities, prices and availability change frequently, and
— decisively — **PDPL cross-border transfer rules may require in-Kingdom or self-hosted
inference**, which we cannot predict at design time.

## Options considered
1. **Single provider SDK directly** — simplest, fastest.
2. **Thin internal port** with per-provider adapters.
3. **Third-party gateway product** — routing, caching, observability out of the box.

## Decision
Option 2. An `LLMProvider` port exposing `complete(messages, schema, tier, budget)` with adapters
for OpenAI, Anthropic, Gemini and an OpenAI-compatible local/self-hosted endpoint. Model
selection is per-agent configuration by **tier** (`SMALL`/`STANDARD`/`LARGE`), not by hard-coded
model id.

## Why
- **Residency is the deciding argument, not cost.** If the transfer assessment forbids sending
  even redacted content abroad, we must move inference in-Kingdom. With the port, that is a
  configuration change and an adapter. Without it, it is a rewrite of every agent under
  regulatory time pressure.
- Tier-based selection lets us route cheap extraction to a small model and memo synthesis to a
  large one, and re-tune that mapping as prices move, without touching agent code.
- Failover across providers keeps the product alive through an outage.
- A third-party gateway (option 3) was rejected because it becomes a fourth party in the data
  path — an additional cross-border processor to assess, for convenience we do not need.

## Trade-offs
- **Accepted:** we normalise to a lowest-common-denominator interface and adapt provider-specific
  structured-output mechanisms per adapter.
- **Accepted:** prompts may need per-provider tuning. Prompt versions are recorded per agent and
  per model so behaviour differences are attributable rather than mysterious.

## Revisit when
A capability we depend on exists at only one provider and cannot be adapted — at which point we
consciously accept the coupling and record it.
