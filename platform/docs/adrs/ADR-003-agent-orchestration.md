# ADR-003: Purpose-built agent runtime, no LLM framework

**Status:** Accepted · **Date:** 2026-08-29

## Problem
Six agents need consistent structured output, cost ceilings, provider failover, full auditing,
idempotency and prompt-injection containment. Do we adopt an agent framework or build a runtime?

## Options considered
1. **LangChain / LlamaIndex or similar** — broad ecosystem, prebuilt abstractions.
2. **A lighter agent library** — less surface, still a dependency on someone else's control flow.
3. **Purpose-built runtime** (~300 lines) over provider SDKs and Pydantic.

## Decision
Option 3. An `Agent` protocol with typed input/output, wrapped by a runtime that enforces schema
validation, cost ceilings, retry policy, idempotency, injection framing, and recording of every
call to `agent_runs` / `agent_decisions` / `llm_calls`.

## Why
- **Our requirements are regulatory, not ergonomic.** We must be able to state exactly what text
  was sent to which provider, what came back, what it cost, and whether it validated. A framework
  that composes prompts for us puts abstraction between us and the one boundary we are most
  accountable for.
- Prompt-injection containment (security §5) depends on precise control of how untrusted content
  is framed. Frameworks routinely concatenate context into prompts in ways that are hard to audit
  and change between versions.
- We use a small, well-understood subset of what these frameworks offer: structured output,
  retries, and provider abstraction. Each is tens of lines directly.
- Framework churn is a real maintenance cost, and their abstractions change faster than our needs.

## Trade-offs
- **Accepted:** we implement retries, streaming, token accounting and tool-calling plumbing
  ourselves — well-understood work, bounded in scope.
- **Accepted:** no access to community integrations. Our integrations are Saudi-specific and
  would not exist in any framework's ecosystem anyway.
- **Mitigated:** the `LLMProvider` port (ADR-006) means adopting a framework later is confined
  to the runtime layer.

## Revisit when
We need genuinely complex multi-agent negotiation or planning loops that our runtime would have
to grow substantially to support — at which point the build-vs-buy calculation changes.
