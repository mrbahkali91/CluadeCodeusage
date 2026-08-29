"""The verification agent.

Checks run deterministically; the agent's job is orchestration, reconciliation
and explanation. It never decides *that* something is verified -- the checkers
do that, and only with stored evidence.

The verification score is deliberately split. Internal coherence and official
confirmation are not interchangeable, so internal checks are capped at a
fraction of the score no matter how many of them pass. Until the official
registers are integrated the score cannot exceed that cap, which is the honest
representation of what we actually know.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from sreoi_agents.checkers import (
    METHOD_VERSION,
    CheckClass,
    Checker,
    CheckOutcome,
    CheckStatus,
    default_checkers,
)
from sreoi_agents.provider import ModelTier
from sreoi_agents.runtime import Agent, AgentContext, AgentRuntime
from sreoi_persistence.models import VerificationCheck

# Internal coherence is genuine evidence -- it catches fabricated and incoherent
# records -- but it is not official confirmation. Capping its contribution is
# what stops "our own data agrees with itself" from masquerading as verification.
INTERNAL_WEIGHT_CAP = 0.35
PROMPT_VERSION = "verification-prompt-v1"


class VerificationSummary(BaseModel):
    """Structured output. Narrative only -- it cannot set a status or a score."""

    headline: str = Field(max_length=240)
    concerns: list[str] = Field(default_factory=list, max_length=6)
    checks_passed: int = Field(ge=0)
    checks_failed: int = Field(ge=0)
    official_checks_available: bool


@dataclass(frozen=True, slots=True)
class VerificationReport:
    outcomes: tuple[CheckOutcome, ...]
    internal_score: float
    official_score: float
    verification_score: float
    official_available: bool
    summary: VerificationSummary | None
    method_version: str = METHOD_VERSION

    @property
    def passed(self) -> tuple[CheckOutcome, ...]:
        return tuple(o for o in self.outcomes if o.status is CheckStatus.VERIFIED)

    @property
    def problems(self) -> tuple[CheckOutcome, ...]:
        return tuple(
            o for o in self.outcomes if o.status in {CheckStatus.FAILED, CheckStatus.CONFLICTED}
        )

    @property
    def score_ceiling_reason(self) -> str | None:
        if self.official_available:
            return None
        return (
            f"No official register is integrated yet, so verification is capped at "
            f"{INTERNAL_WEIGHT_CAP:.0%} of its full weight."
        )


def _class_score(outcomes: list[CheckOutcome]) -> tuple[float, bool]:
    """Fraction of *applicable* checks that passed, and whether any were applicable."""
    applicable = [o for o in outcomes if o.status.counts_toward_score]
    if not applicable:
        return 0.0, False
    verified = sum(1 for o in applicable if o.status is CheckStatus.VERIFIED)
    return verified / len(applicable), True


class VerificationAgent(Agent[dict[str, Any], VerificationSummary]):
    """Explains the checker results. It does not produce them."""

    name = "verification"
    prompt_version = PROMPT_VERSION
    tier = ModelTier.STANDARD
    output_model = VerificationSummary
    call_budget_usd = Decimal("0.10")
    uses_tools = False  # reads attacker-controlled listing text

    def system_prompt(self) -> str:
        return (
            "You summarise the result of property verification checks that have "
            "already been performed by deterministic code. You do not decide "
            "whether anything is verified, you do not compute scores, and you "
            "never invent a check that was not run. Report only what the "
            "supplied results say. Respond with JSON matching the schema."
        )

    def user_prompt(self, payload: dict[str, Any]) -> str:
        return (
            "Verification results for one property:\n"
            f"```json\n{json.dumps(payload['results'], ensure_ascii=False)}\n```\n"
            "Summarise them for an investor."
        )

    def untrusted_content(self, payload: dict[str, Any]) -> list[str]:
        text = payload.get("listing_text")
        return [text] if text else []

    def subject(self, payload: dict[str, Any]) -> tuple[str, UUID | None]:
        return ("opportunity", payload.get("opportunity_id"))

    def input_fingerprint(self, payload: dict[str, Any]) -> Any:
        return {"results": payload["results"], "listing_text": payload.get("listing_text")}

    def validate_output(
        self, output: VerificationSummary, payload: dict[str, Any]
    ) -> VerificationSummary:
        """Post-model validation: counts must match the checks that actually ran."""
        results = payload["results"]
        expected_passed = sum(1 for r in results if r["status"] == CheckStatus.VERIFIED)
        expected_failed = sum(
            1 for r in results if r["status"] in {CheckStatus.FAILED, CheckStatus.CONFLICTED}
        )
        if output.checks_passed != expected_passed or output.checks_failed != expected_failed:
            raise ValueError(
                "summary contradicts the checks that ran: reported "
                f"{output.checks_passed}/{output.checks_failed}, actual "
                f"{expected_passed}/{expected_failed}"
            )
        return output


def deterministic_verification_responder(request: Any) -> str:
    """Offline stand-in for the summary step.

    Reads the results out of the prompt exactly as a model would, and composes
    the same schema. Recorded as `deterministic-offline`, never as reasoning.
    """
    match = re.search(r"```json\n(.*?)\n```", request.user, re.S)
    results = json.loads(match.group(1)) if match else []

    passed = [r for r in results if r["status"] == CheckStatus.VERIFIED]
    problems = [r for r in results if r["status"] in {CheckStatus.FAILED, CheckStatus.CONFLICTED}]
    official = [r for r in results if r["check_class"] == CheckClass.OFFICIAL]
    official_available = any(
        r["status"].counts_toward_score
        if hasattr(r["status"], "counts_toward_score")
        else r["status"] in {"VERIFIED", "FAILED", "CONFLICTED"}
        for r in official
    )

    if problems:
        headline = f"{len(passed)} checks passed, {len(problems)} raised a problem."
    elif passed:
        headline = f"All {len(passed)} applicable checks passed."
    else:
        headline = "No verification checks could be applied to this property."

    concerns = [r["summary"] for r in problems][:4]
    if not official_available:
        concerns.append("No official register was consulted; official verification is pending.")

    return VerificationSummary(
        headline=headline,
        concerns=concerns,
        checks_passed=len(passed),
        checks_failed=len(problems),
        official_checks_available=official_available,
    ).model_dump_json()


def verify_opportunity(
    session: Session,
    *,
    property_id: UUID,
    opportunity_id: UUID,
    listing_text: str | None = None,
    checkers: list[Checker] | None = None,
    context: AgentContext | None = None,
) -> VerificationReport:
    """Run every checker, persist the outcomes, then summarise them."""
    checkers = checkers or default_checkers()
    outcomes = [c.run(session, property_id, opportunity_id) for c in checkers]

    for outcome in outcomes:
        session.add(
            VerificationCheck(
                opportunity_id=opportunity_id,
                check_type=outcome.check_type,
                status=outcome.status.value,
                # Evidence is recorded for every outcome, not only passes: the
                # class and the reason a check did not apply are themselves
                # auditable facts. (The DB constraint requires it for VERIFIED;
                # storing it always is strictly better.)
                evidence={
                    "summary": outcome.summary,
                    "check_class": outcome.check_class.value,
                    "detail": outcome.evidence,
                },
                checked_at=datetime.now(UTC),
            )
        )

    internal = [o for o in outcomes if o.check_class is CheckClass.INTERNAL]
    official = [o for o in outcomes if o.check_class is CheckClass.OFFICIAL]
    internal_score, _ = _class_score(internal)
    official_score, official_available = _class_score(official)

    verification_score = (
        INTERNAL_WEIGHT_CAP * internal_score + (1 - INTERNAL_WEIGHT_CAP) * official_score
    )

    summary: VerificationSummary | None = None
    if context is not None:
        payload = {
            "opportunity_id": opportunity_id,
            "listing_text": listing_text,
            "results": [
                {
                    "check_type": o.check_type,
                    "check_class": o.check_class.value,
                    "status": o.status.value,
                    "summary": o.summary,
                }
                for o in outcomes
            ],
        }
        result = AgentRuntime(context).run(VerificationAgent(), payload)
        summary = result.output

    return VerificationReport(
        outcomes=tuple(outcomes),
        internal_score=internal_score,
        official_score=official_score,
        verification_score=round(verification_score, 4),
        official_available=official_available,
        summary=summary,
    )
