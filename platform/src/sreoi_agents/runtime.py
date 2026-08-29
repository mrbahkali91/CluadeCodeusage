"""The agent runtime.

Deliberately small and purpose-built rather than a framework (ADR-003): our
requirements here are regulatory, not ergonomic. We must be able to state
exactly what text was sent to which provider, what came back, what it cost, and
whether it validated. A framework that composes prompts for us puts abstraction
between us and the one boundary we are most accountable for.

Every execution gets the same guarantees regardless of which agent it is:

  * structured output only, with one repair retry and then failure -- never
    coerce, never partially accept;
  * untrusted content framed as data, never concatenated into instructions;
  * per-call and per-run cost ceilings;
  * idempotency on (agent, input_hash, prompt_version, model);
  * full recording to agent_runs / agent_decisions / llm_calls.
"""

from __future__ import annotations

import abc
import hashlib
import json
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Generic, TypeVar
from uuid import UUID

from pydantic import BaseModel, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from sreoi_agents.provider import (
    LLMProvider,
    LLMRequest,
    ModelTier,
    ProviderError,
)
from sreoi_agents.untrusted import ScanResult, scan, wrap
from sreoi_persistence.models import AgentDecision, AgentRun, LLMCall

TIn = TypeVar("TIn")
TOut = TypeVar("TOut", bound=BaseModel)

DEFAULT_CALL_BUDGET_USD = Decimal("0.50")
DEFAULT_RUN_BUDGET_USD = Decimal("2.00")


class AgentError(RuntimeError):
    """Agent could not produce a valid result."""


class BudgetExceededError(AgentError):
    """Cost ceiling hit. The run aborts rather than quietly spending more."""


@dataclass(slots=True)
class AgentContext:
    """Everything an agent needs from the outside world, passed explicitly."""

    session: Session
    provider: LLMProvider
    run_budget_usd: Decimal = DEFAULT_RUN_BUDGET_USD
    spent_usd: Decimal = field(default=Decimal("0"))

    def charge(self, amount: Decimal) -> None:
        if self.spent_usd + amount > self.run_budget_usd:
            raise BudgetExceededError(
                f"run budget {self.run_budget_usd} USD would be exceeded "
                f"(spent {self.spent_usd}, this call {amount})"
            )
        self.spent_usd += amount


@dataclass(frozen=True, slots=True)
class AgentResult(Generic[TOut]):
    output: TOut
    run_id: UUID
    cost_usd: Decimal
    cached: bool
    injection: ScanResult
    provider: str

    @property
    def injection_flagged(self) -> bool:
        return self.injection.suspicious


def stable_hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


class Agent(abc.ABC, Generic[TIn, TOut]):
    """Base class. Subclasses describe *what* to ask; the runtime handles how."""

    name: str
    prompt_version: str
    tier: ModelTier = ModelTier.STANDARD
    output_model: type[TOut]
    call_budget_usd: Decimal = DEFAULT_CALL_BUDGET_USD
    # Agents that read attacker-controlled text get no tools. There is then
    # nothing for an injected instruction to actuate.
    uses_tools: bool = False

    @abc.abstractmethod
    def system_prompt(self) -> str: ...

    @abc.abstractmethod
    def user_prompt(self, payload: TIn) -> str: ...

    def untrusted_content(self, payload: TIn) -> list[str]:
        """External text that must be framed as data, never as instruction."""
        return []

    def subject(self, payload: TIn) -> tuple[str, UUID | None]:
        return ("unknown", None)

    def input_fingerprint(self, payload: TIn) -> Any:
        return payload

    def validate_output(self, output: TOut, payload: TIn) -> TOut:
        """Post-model sanity checks. The model is not the last line of defence."""
        return output


class AgentRuntime:
    """Executes agents with uniform guarantees."""

    def __init__(self, context: AgentContext) -> None:
        self._ctx = context

    def run(self, agent: Agent[TIn, TOut], payload: TIn) -> AgentResult[TOut]:
        session = self._ctx.session
        started = time.perf_counter()
        subject_type, subject_id = agent.subject(payload)
        input_hash = stable_hash(agent.input_fingerprint(payload))

        untrusted = agent.untrusted_content(payload)
        injection = scan("\n".join(untrusted)) if untrusted else ScanResult(())

        if agent.uses_tools and untrusted:
            raise AgentError(f"{agent.name} reads untrusted content and must not have tool access")

        cached = self._cached_run(agent, input_hash)
        if cached is not None:
            decision = next((d for d in cached.decisions if d.kind == "output"), None)
            if decision is not None:
                return AgentResult(
                    output=agent.output_model.model_validate(decision.detail),
                    run_id=cached.id,
                    cost_usd=Decimal(cached.cost_usd),
                    cached=True,
                    injection=injection,
                    provider=self._ctx.provider.name,
                )

        run = AgentRun(
            agent=agent.name,
            subject_type=subject_type,
            subject_id=subject_id,
            input_hash=input_hash,
            prompt_version=agent.prompt_version,
            status="FAILED",
            injection_flagged=injection.suspicious,
            started_at=datetime.now(UTC),
        )
        session.add(run)
        session.flush()

        if injection.suspicious:
            session.add(
                AgentDecision(
                    agent_run_id=run.id,
                    kind="injection_scan",
                    outcome="FLAGGED",
                    detail={
                        "summary": injection.summary,
                        "findings": [
                            {"pattern": f.pattern, "excerpt": f.excerpt[:200]}
                            for f in injection.findings
                        ],
                    },
                )
            )

        request = LLMRequest(
            system=agent.system_prompt(),
            user=self._compose_user(agent, payload, untrusted),
            tier=agent.tier,
            schema_name=agent.output_model.__name__,
            json_schema=agent.output_model.model_json_schema(),
            untrusted_blocks=tuple(untrusted),
        )

        output, cost, _retries, error = self._call_with_repair(agent, request, run)
        duration_ms = (time.perf_counter() - started) * 1000

        if output is None:
            run.status = "FAILED"
            run.error = error
            run.duration_ms = duration_ms
            raise AgentError(f"{agent.name} failed: {error}")

        validated = agent.validate_output(output, payload)

        run.status = "SUCCEEDED"
        run.cost_usd = cost
        run.duration_ms = duration_ms
        session.add(
            AgentDecision(
                agent_run_id=run.id,
                kind="output",
                outcome="OK",
                detail=json.loads(validated.model_dump_json()),
            )
        )
        session.flush()

        return AgentResult(
            output=validated,
            run_id=run.id,
            cost_usd=cost,
            cached=False,
            injection=injection,
            provider=self._ctx.provider.name,
        )

    def _compose_user(self, agent: Agent[TIn, TOut], payload: TIn, untrusted: list[str]) -> str:
        parts = [agent.user_prompt(payload)]
        if untrusted:
            parts.append(wrap(untrusted))
        return "\n\n".join(parts)

    def _cached_run(self, agent: Agent[TIn, TOut], input_hash: str) -> AgentRun | None:
        """Idempotency: a retried pipeline re-uses the result instead of paying twice."""
        return self._ctx.session.scalar(
            select(AgentRun)
            .where(
                AgentRun.agent == agent.name,
                AgentRun.input_hash == input_hash,
                AgentRun.prompt_version == agent.prompt_version,
                AgentRun.status == "SUCCEEDED",
            )
            .order_by(AgentRun.started_at.desc())
            .limit(1)
        )

    def _call_with_repair(
        self, agent: Agent[TIn, TOut], request: LLMRequest, run: AgentRun
    ) -> tuple[TOut | None, Decimal, int, str | None]:
        """One repair attempt, then fail. Never coerce a malformed response."""
        total_cost = Decimal("0")
        last_error: str | None = None

        for attempt in range(2):
            call_started = time.perf_counter()
            try:
                response = self._ctx.provider.complete(request)
            except ProviderError as exc:
                return None, total_cost, attempt, str(exc)

            if response.cost_usd > agent.call_budget_usd:
                raise BudgetExceededError(
                    f"{agent.name} call cost {response.cost_usd} exceeds "
                    f"per-call budget {agent.call_budget_usd}"
                )
            self._ctx.charge(response.cost_usd)
            total_cost += response.cost_usd

            valid = True
            parsed: TOut | None = None
            try:
                parsed = agent.output_model.model_validate_json(response.text)
            except ValidationError as exc:
                valid = False
                last_error = f"schema validation failed: {exc.errors()[:3]}"

            self._ctx.session.add(
                LLMCall(
                    agent_run_id=run.id,
                    provider=response.provider,
                    model=response.model,
                    tier=request.tier.value,
                    input_tokens=response.input_tokens,
                    output_tokens=response.output_tokens,
                    cost_usd=response.cost_usd,
                    schema_valid=valid,
                    retry_count=attempt,
                    latency_ms=(time.perf_counter() - call_started) * 1000,
                )
            )

            if parsed is not None:
                return parsed, total_cost, attempt, None

            request = LLMRequest(
                system=request.system,
                user=(
                    f"{request.user}\n\nYour previous response did not validate "
                    f"against the required schema. Error: {last_error}. "
                    "Return only valid JSON matching the schema."
                ),
                tier=request.tier,
                schema_name=request.schema_name,
                json_schema=request.json_schema,
                untrusted_blocks=request.untrusted_blocks,
            )

        return None, total_cost, 1, last_error
