"""Data-quality monitoring (backlog E8.4, solution-architecture section 7).

    python -m sreoi_pipeline.quality --json

The metrics here are the ones that fail *quietly*. A dead connector still
returns yesterday's rows; entity resolution that has started over-merging
still produces a clean-looking property graph; a confidence distribution that
has drifted down still renders a page. None of these raise an exception, and
all of them degrade the product. So each is measured, compared against a
stated threshold, and snapshotted -- because a single reading tells you almost
nothing and a series tells you everything.

Every figure is computed from the database as it stands. Where the underlying
evidence is synthetic that is recorded on the snapshot, for the same reason it
is recorded on a back-test run: a stored metric that outlives the knowledge of
what it measured is a number waiting to be quoted out of context.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from sqlalchemy import Integer, func, select
from sqlalchemy.orm import Session

from sreoi_domain.resolution import AUTO_MERGE_THRESHOLD
from sreoi_persistence.db import session_scope
from sreoi_persistence.models import (
    AgentDecision,
    AgentRun,
    DataProvenance,
    Opportunity,
    OpportunityScoreRow,
    Property,
    PropertyMerge,
    Source,
    SourceRecord,
    Valuation,
    VerificationCheck,
)
from sreoi_persistence.models_quality import QualitySnapshot

METHOD_VERSION = "quality-v1"

# Property fields whose absence weakens a valuation. Kept in step with
# `_field_completeness` in the evaluation pipeline, plus the fields that only
# matter for entity resolution.
COMPLETENESS_FIELDS: tuple[str, ...] = (
    "bedrooms",
    "floor",
    "build_year",
    "district_id",
    "developer_name",
    "unit_number",
)

CONFIDENCE_EDGES: tuple[float, ...] = (0.0, 0.45, 0.60, 0.75, 0.90, 1.0)

# How close to the auto-merge cut-off counts as a borderline decision.
BORDERLINE_MARGIN = 0.05

# A verification check that could not run is not a failed check. Only these
# statuses are evidence either way (mirrors CheckStatus.counts_toward_score).
APPLICABLE_STATUSES = ("VERIFIED", "FAILED", "CONFLICTED")


class Severity(StrEnum):
    OK = "OK"
    WARN = "WARN"
    FAIL = "FAIL"

    @property
    def rank(self) -> int:
        return {"OK": 0, "WARN": 1, "FAIL": 2}[self.value]


@dataclass(frozen=True, slots=True)
class Threshold:
    """A stated expectation, so a regression is detected rather than debated."""

    key: str
    label: str
    warn: float
    fail: float
    higher_is_better: bool
    note: str

    def severity(self, value: float | None) -> Severity:
        if value is None:
            return Severity.OK
        if self.higher_is_better:
            if value < self.fail:
                return Severity.FAIL
            return Severity.WARN if value < self.warn else Severity.OK
        if value > self.fail:
            return Severity.FAIL
        return Severity.WARN if value > self.warn else Severity.OK


THRESHOLDS: tuple[Threshold, ...] = (
    Threshold(
        "field_completeness",
        "Property field completeness",
        warn=0.70,
        fail=0.50,
        higher_is_better=True,
        note="Missing subject fields weaken every comparable weight and the confidence figure.",
    ),
    Threshold(
        "mean_data_confidence",
        "Mean data confidence",
        warn=0.60,
        fail=0.45,
        higher_is_better=True,
        note="Below 0.60 the confidence gate suppresses the recommendation entirely.",
    ),
    Threshold(
        "insufficient_data_rate",
        "Share classified INSUFFICIENT_DATA",
        warn=0.30,
        fail=0.60,
        higher_is_better=False,
        note=(
            "Correct behaviour, not a bug -- but a high rate means the product has little "
            "to show, and points at evidence coverage rather than at the scorer."
        ),
    ),
    Threshold(
        "valuation_refusal_rate",
        "Share with no valuation (insufficient comparables)",
        warn=0.20,
        fail=0.40,
        higher_is_better=False,
        note="Refusing is correct; a rising rate means comparable coverage is degrading.",
    ),
    Threshold(
        "refused_discount_rate",
        "Share with the discount refused",
        warn=0.25,
        fail=0.50,
        higher_is_better=False,
        note=(
            "The refusal invariant working as designed. Tracked because it measures how "
            "often material cost data is missing at intake, not whether the rule is right."
        ),
    ),
    Threshold(
        "unknown_basis_share",
        "Provenance entries with basis UNKNOWN",
        warn=0.10,
        fail=0.25,
        higher_is_better=False,
        note="An UNKNOWN is honest; many UNKNOWNs mean intake is not collecting enough.",
    ),
    Threshold(
        "duplicate_resolution_rate",
        "Share of intake resolved onto an existing property",
        warn=0.50,
        fail=0.75,
        higher_is_better=False,
        note=(
            "Entity-resolution drift shows here first. Read it beside the borderline "
            "merge share: a high rate with decisive scores is a duplicate-heavy feed, "
            "a high rate with marginal scores is over-merging."
        ),
    ),
    Threshold(
        "borderline_merge_share",
        "Auto-merges clearing the threshold only narrowly",
        warn=0.20,
        fail=0.40,
        higher_is_better=False,
        note=(
            "A merge is irreversible in effect -- the second record's evidence is folded "
            "in -- so merges decided just above the cut-off are the ones to watch."
        ),
    ),
    Threshold(
        "agent_disagreement_rate",
        "Agent output contradicting the deterministic checks",
        warn=0.02,
        fail=0.10,
        higher_is_better=False,
        note=(
            "The agent may explain checks, never decide them. Anything above zero means "
            "the model is asserting things the checkers did not find."
        ),
    ),
    Threshold(
        "verification_pass_rate",
        "Verification checks passing (of those applicable)",
        warn=0.70,
        fail=0.50,
        higher_is_better=True,
        note="A falling pass rate is either worse intake data or a broken checker.",
    ),
    Threshold(
        "stalest_source_age_days",
        "Age of the stalest enabled source",
        warn=7.0,
        fail=30.0,
        higher_is_better=False,
        note="The leading indicator of a silently dead connector.",
    ),
)
THRESHOLDS_BY_KEY = {t.key: t for t in THRESHOLDS}


@dataclass(frozen=True, slots=True)
class QualityFlag:
    key: str
    label: str
    severity: Severity
    value: float | None
    threshold_warn: float
    threshold_fail: float
    note: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "severity": self.severity.value,
            "value": None if self.value is None else round(self.value, 5),
            "warn_at": self.threshold_warn,
            "fail_at": self.threshold_fail,
            "note": self.note,
        }


@dataclass(frozen=True, slots=True)
class QualityReport:
    captured_at: datetime
    evidence_is_synthetic: bool
    opportunity_count: int
    property_count: int
    field_completeness: dict[str, float]
    field_completeness_overall: float | None
    confidence_distribution: dict[str, Any]
    duplicate_resolution: dict[str, Any]
    source_freshness: tuple[dict[str, Any], ...]
    agent_disagreement: dict[str, Any]
    refusals: dict[str, Any]
    verification: dict[str, Any]
    provenance: dict[str, Any]
    flags: tuple[QualityFlag, ...]
    method_version: str = METHOD_VERSION

    @property
    def overall_status(self) -> Severity:
        if not self.flags:
            return Severity.OK
        return max((f.severity for f in self.flags), key=lambda s: s.rank)

    @property
    def regressions(self) -> tuple[QualityFlag, ...]:
        return tuple(f for f in self.flags if f.severity is not Severity.OK)

    def metric(self, key: str) -> float | None:
        for flag in self.flags:
            if flag.key == key:
                return flag.value
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "method_version": self.method_version,
            "captured_at": self.captured_at.isoformat(),
            # Kept adjacent to the metrics for the same reason the back-test
            # does it: the caveat must travel with the numbers.
            "evidence_is_synthetic": self.evidence_is_synthetic,
            "overall_status": self.overall_status.value,
            "counts": {
                "opportunities": self.opportunity_count,
                "properties": self.property_count,
            },
            "field_completeness": {
                "by_field": {k: round(v, 4) for k, v in self.field_completeness.items()},
                "overall": None
                if self.field_completeness_overall is None
                else round(self.field_completeness_overall, 4),
            },
            "confidence_distribution": self.confidence_distribution,
            "duplicate_resolution": self.duplicate_resolution,
            "source_freshness": list(self.source_freshness),
            "agent_disagreement": self.agent_disagreement,
            "refusals": self.refusals,
            "verification": self.verification,
            "provenance": self.provenance,
            "flags": [f.to_dict() for f in self.flags],
            "regressions": [f.key for f in self.regressions],
        }


def _bucket_label(lower: float, upper: float) -> str:
    return f"{lower:.0%}-{upper:.0%}"


def _distribution(values: list[float], edges: tuple[float, ...]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for i in range(len(edges) - 1):
        lower, upper = edges[i], edges[i + 1]
        last = i == len(edges) - 2
        count = sum(1 for v in values if lower <= v < upper or (last and v == upper))
        out.append(
            {
                "label": _bucket_label(lower, upper),
                "lower": lower,
                "upper": upper,
                "count": count,
                "share": round(count / len(values), 4) if values else None,
            }
        )
    return out


def _live_score_ids(session: Session) -> list[UUID]:
    """Current scores only. Score rows are append-only, so the superseded ones
    would otherwise double-count every rate on this page."""
    return list(
        session.scalars(
            select(OpportunityScoreRow.id).where(OpportunityScoreRow.superseded_at.is_(None))
        )
    )


def field_completeness(session: Session) -> tuple[dict[str, float], float | None, int]:
    """Per-field completeness across live (unmerged) properties."""
    total = int(
        session.scalar(
            select(func.count()).select_from(Property).where(Property.merged_into_id.is_(None))
        )
        or 0
    )
    if not total:
        return {}, None, 0

    columns = [func.count(getattr(Property, name)).label(name) for name in COMPLETENESS_FIELDS]
    row = session.execute(
        select(*columns).select_from(Property).where(Property.merged_into_id.is_(None))
    ).one()
    by_field = {
        name: (int(value) / total) for name, value in zip(COMPLETENESS_FIELDS, row, strict=True)
    }
    overall = sum(by_field.values()) / len(by_field)
    return by_field, overall, total


def confidence_distribution(session: Session) -> dict[str, Any]:
    """Data confidence and valuation confidence, bucketed.

    Falling confidence means comparable coverage is degrading, which is
    invisible in any success/failure metric.
    """
    score_ids = _live_score_ids(session)
    data_conf = [
        float(v)
        for v in session.scalars(
            select(OpportunityScoreRow.data_confidence).where(OpportunityScoreRow.id.in_(score_ids))
        )
        if v is not None
    ]
    val_conf = [float(v) for v in session.scalars(select(Valuation.confidence)) if v is not None]
    return {
        "data_confidence": {
            "count": len(data_conf),
            "mean": round(sum(data_conf) / len(data_conf), 4) if data_conf else None,
            "buckets": _distribution(data_conf, CONFIDENCE_EDGES),
            "below_gate": round(sum(1 for v in data_conf if v < 0.60) / len(data_conf), 4)
            if data_conf
            else None,
        },
        "valuation_confidence": {
            "count": len(val_conf),
            "mean": round(sum(val_conf) / len(val_conf), 4) if val_conf else None,
            "buckets": _distribution(val_conf, CONFIDENCE_EDGES),
        },
    }


def duplicate_resolution(session: Session) -> dict[str, Any]:
    """How much of intake resolves onto an existing property, and how safely.

    The rate alone cannot tell over-merging from a genuinely duplicate-heavy
    feed, so it is reported beside the *margin* by which auto-merges cleared
    the threshold. A hundred merges all scoring 0.98 is a clean feed; a
    hundred scoring 0.86 is a threshold about to start deleting evidence.
    """
    rows = session.execute(
        select(PropertyMerge.decision, func.count())
        .where(PropertyMerge.reversed_at.is_(None))
        .group_by(PropertyMerge.decision)
    ).all()
    by_decision = {str(decision): int(count) for decision, count in rows}
    decisions = sum(by_decision.values())
    auto = by_decision.get("AUTO_MERGE", 0)

    live_properties = int(
        session.scalar(
            select(func.count()).select_from(Property).where(Property.merged_into_id.is_(None))
        )
        or 0
    )
    # Every resolved record either became a property or folded into one.
    resolved = auto + live_properties

    scores = [
        float(v)
        for v in session.scalars(
            select(PropertyMerge.score).where(
                PropertyMerge.decision == "AUTO_MERGE", PropertyMerge.reversed_at.is_(None)
            )
        )
    ]
    borderline = sum(1 for v in scores if v < AUTO_MERGE_THRESHOLD + BORDERLINE_MARGIN)
    return {
        "by_decision": by_decision,
        "decisions": decisions,
        "resolved_records": resolved,
        "duplicate_resolution_rate": round(auto / resolved, 4) if resolved else None,
        "auto_merge_share_of_decisions": round(auto / decisions, 4) if decisions else None,
        "review_queue": by_decision.get("REVIEW", 0),
        "auto_merge_threshold": AUTO_MERGE_THRESHOLD,
        "min_auto_merge_score": round(min(scores), 4) if scores else None,
        "borderline_auto_merges": borderline,
        "borderline_merge_share": round(borderline / len(scores), 4) if scores else None,
        "merged_property_rows": int(
            session.scalar(
                select(func.count())
                .select_from(Property)
                .where(Property.merged_into_id.isnot(None))
            )
            or 0
        ),
        "reversed": int(
            session.scalar(
                select(func.count())
                .select_from(PropertyMerge)
                .where(PropertyMerge.reversed_at.isnot(None))
            )
            or 0
        ),
        "note": (
            "The ingest path resolves onto an existing property before creating a row, so "
            "`merged_property_rows` stays at zero: `properties.merged_into_id` records a "
            "retrospective merge of two already-stored rows, which this path never performs. "
            "Read `duplicate_resolution_rate` from the decision log, not from that column."
        ),
    }


def source_freshness(session: Session) -> tuple[tuple[dict[str, Any], ...], float | None]:
    """Age of the newest record per source, and the worst of them.

    A connector that has stopped working does not raise; it simply stops
    adding rows, and this is the only place that shows.
    """
    now = datetime.now(UTC)
    rows: list[dict[str, Any]] = []
    stalest: float | None = None
    for source in session.scalars(select(Source).order_by(Source.key)):
        last = session.scalar(
            select(func.max(SourceRecord.retrieved_at)).where(SourceRecord.source_id == source.id)
        )
        count = int(
            session.scalar(
                select(func.count())
                .select_from(SourceRecord)
                .where(SourceRecord.source_id == source.id)
            )
            or 0
        )
        age_days: float | None = None
        if last is not None:
            reference = last if last.tzinfo else last.replace(tzinfo=UTC)
            age_days = (now - reference).total_seconds() / 86400.0
        if source.enabled and age_days is not None:
            stalest = age_days if stalest is None else max(stalest, age_days)
        rows.append(
            {
                "key": source.key,
                "name": source.name,
                "enabled": source.enabled,
                "is_synthetic": source.is_synthetic,
                "record_count": count,
                "last_record_at": last.isoformat() if last is not None else None,
                "age_days": None if age_days is None else round(age_days, 3),
                "never_delivered": last is None,
            }
        )
    return tuple(rows), stalest


def _latest_check_batch(session: Session, opportunity_id: UUID) -> list[VerificationCheck]:
    """The current deterministic verdict per check type for one opportunity.

    Checks are append-only, so every re-evaluation adds another row for the
    same check type. The current state is therefore the newest row *per check
    type*, not everything written recently -- a time window looks right and is
    not, because the corpus loader re-evaluates several opportunities inside
    the same second and a window silently sums their batches. Getting this
    wrong reports a 30% agent disagreement rate that does not exist, so it is
    worth the extra query.
    """
    rows = session.scalars(
        select(VerificationCheck)
        .where(VerificationCheck.opportunity_id == opportunity_id)
        .order_by(VerificationCheck.checked_at.desc(), VerificationCheck.id)
    )
    current: dict[str, VerificationCheck] = {}
    for row in rows:
        current.setdefault(row.check_type, row)
    return list(current.values())


def agent_disagreement(session: Session) -> dict[str, Any]:
    """How often the agent's narrative contradicts the deterministic checks.

    Two distinct numbers, because they mean different things:

    * `blocked` -- runs rejected by `validate_output` because the summary's
      counts did not match the checks. The guard working.
    * `stored` -- persisted summaries that *still* disagree. Structurally
      impossible while the guard holds, which is exactly why it is measured:
      a non-zero value means the guard has stopped working, and that is not
      something a passing test suite would reveal.
    """
    total_runs = int(session.scalar(select(func.count()).select_from(AgentRun)) or 0)
    failed = int(
        session.scalar(
            select(func.count()).select_from(AgentRun).where(AgentRun.status == "FAILED")
        )
        or 0
    )
    blocked = int(
        session.scalar(
            select(func.count())
            .select_from(AgentRun)
            .where(AgentRun.status == "FAILED", AgentRun.error.ilike("%contradict%"))
        )
        or 0
    )
    injection_flagged = int(
        session.scalar(
            select(func.coalesce(func.sum(func.cast(AgentRun.injection_flagged, Integer)), 0))
        )
        or 0
    )

    stored = 0
    compared = 0
    examples: list[dict[str, Any]] = []
    rows = session.execute(
        select(AgentRun.subject_id, AgentDecision.detail)
        .join(AgentDecision, AgentDecision.agent_run_id == AgentRun.id)
        .where(
            AgentRun.status == "SUCCEEDED",
            AgentRun.subject_type == "opportunity",
            AgentDecision.kind == "output",
        )
        .order_by(AgentRun.started_at.desc())
    ).all()
    seen: set[UUID] = set()
    for subject_id, detail in rows:
        if subject_id is None or subject_id in seen:
            continue
        seen.add(subject_id)
        checks = _latest_check_batch(session, subject_id)
        if not checks:
            continue
        expected_passed = sum(1 for c in checks if c.status == "VERIFIED")
        expected_failed = sum(1 for c in checks if c.status in {"FAILED", "CONFLICTED"})
        claimed_passed = detail.get("checks_passed")
        claimed_failed = detail.get("checks_failed")
        if claimed_passed is None or claimed_failed is None:
            continue
        compared += 1
        if claimed_passed != expected_passed or claimed_failed != expected_failed:
            stored += 1
            if len(examples) < 5:
                examples.append(
                    {
                        "opportunity_id": str(subject_id),
                        "claimed": {"passed": claimed_passed, "failed": claimed_failed},
                        "deterministic": {
                            "passed": expected_passed,
                            "failed": expected_failed,
                        },
                    }
                )

    return {
        "runs": total_runs,
        "failed_runs": failed,
        "blocked_disagreements": blocked,
        "blocked_rate": round(blocked / total_runs, 5) if total_runs else None,
        "compared_summaries": compared,
        "stored_disagreements": stored,
        "stored_rate": round(stored / compared, 5) if compared else None,
        "injection_flagged_runs": injection_flagged,
        "examples": examples,
    }


def refusals(session: Session) -> dict[str, Any]:
    """Counts the system's deliberate refusals. Each is correct behaviour."""
    score_ids = _live_score_ids(session)
    scored = len(score_ids)
    refused_discount = int(
        session.scalar(
            select(func.count())
            .select_from(OpportunityScoreRow)
            .where(
                OpportunityScoreRow.id.in_(score_ids),
                OpportunityScoreRow.discount_refused_reason.isnot(None),
            )
        )
        or 0
    )
    insufficient = int(
        session.scalar(
            select(func.count())
            .select_from(OpportunityScoreRow)
            .where(
                OpportunityScoreRow.id.in_(score_ids),
                OpportunityScoreRow.classification == "INSUFFICIENT_DATA",
            )
        )
        or 0
    )
    capped = int(
        session.scalar(
            select(func.count())
            .select_from(OpportunityScoreRow)
            .where(OpportunityScoreRow.id.in_(score_ids), OpportunityScoreRow.capped.is_(True))
        )
        or 0
    )
    opportunities = int(session.scalar(select(func.count()).select_from(Opportunity)) or 0)
    with_valuation = int(
        session.scalar(select(func.count(func.distinct(Valuation.opportunity_id)))) or 0
    )
    no_valuation = max(0, opportunities - with_valuation)
    return {
        "scored_opportunities": scored,
        "refused_discounts": refused_discount,
        "refused_discount_rate": round(refused_discount / scored, 4) if scored else None,
        "insufficient_data": insufficient,
        "insufficient_data_rate": round(insufficient / scored, 4) if scored else None,
        "classification_capped": capped,
        "no_valuation": no_valuation,
        "valuation_refusal_rate": round(no_valuation / opportunities, 4) if opportunities else None,
        "note": (
            "Every count here is the system declining to state something it cannot "
            "support. They are tracked as data-coverage signals, not as errors."
        ),
    }


def verification_rates(session: Session) -> dict[str, Any]:
    rows = session.execute(
        select(VerificationCheck.check_type, VerificationCheck.status, func.count())
        .group_by(VerificationCheck.check_type, VerificationCheck.status)
        .order_by(VerificationCheck.check_type)
    ).all()
    by_type: dict[str, dict[str, Any]] = {}
    applicable_total = 0
    verified_total = 0
    for check_type, status, count in rows:
        entry = by_type.setdefault(
            str(check_type), {"statuses": {}, "applicable": 0, "verified": 0}
        )
        entry["statuses"][str(status)] = int(count)
        if str(status) in APPLICABLE_STATUSES:
            entry["applicable"] += int(count)
            applicable_total += int(count)
            if str(status) == "VERIFIED":
                entry["verified"] += int(count)
                verified_total += int(count)
    for entry in by_type.values():
        entry["pass_rate"] = (
            round(entry["verified"] / entry["applicable"], 4) if entry["applicable"] else None
        )
    official_available = any(
        entry["applicable"]
        for name, entry in by_type.items()
        if name in {"rega_advertisement_licence", "wafi_project_licence", "developer_registry"}
    )
    return {
        "by_check_type": by_type,
        "applicable": applicable_total,
        "verified": verified_total,
        "pass_rate": round(verified_total / applicable_total, 4) if applicable_total else None,
        "official_checks_available": official_available,
        "note": (
            "UNAVAILABLE and NOT_APPLICABLE are excluded from the pass rate: a check that "
            "could not run is not a check that failed."
        ),
    }


def provenance_basis(session: Session) -> dict[str, Any]:
    rows = session.execute(
        select(DataProvenance.basis, func.count()).group_by(DataProvenance.basis)
    ).all()
    by_basis = {str(basis): int(count) for basis, count in rows}
    total = sum(by_basis.values())
    return {
        "by_basis": by_basis,
        "total": total,
        "unknown_share": round(by_basis.get("UNKNOWN", 0) / total, 4) if total else None,
        "actual_share": round(by_basis.get("ACTUAL", 0) / total, 4) if total else None,
    }


def _flag(key: str, value: float | None) -> QualityFlag:
    threshold = THRESHOLDS_BY_KEY[key]
    return QualityFlag(
        key=key,
        label=threshold.label,
        severity=threshold.severity(value),
        value=value,
        threshold_warn=threshold.warn,
        threshold_fail=threshold.fail,
        note=threshold.note,
    )


def collect_quality(session: Session) -> QualityReport:
    """Take one full data-quality reading."""
    by_field, overall_completeness, property_count = field_completeness(session)
    confidence = confidence_distribution(session)
    duplicates = duplicate_resolution(session)
    freshness, stalest = source_freshness(session)
    agents = agent_disagreement(session)
    refusal = refusals(session)
    verification = verification_rates(session)
    provenance = provenance_basis(session)

    mean_data_confidence = confidence["data_confidence"]["mean"]
    flags = (
        _flag("field_completeness", overall_completeness),
        _flag("mean_data_confidence", mean_data_confidence),
        _flag("insufficient_data_rate", refusal["insufficient_data_rate"]),
        _flag("valuation_refusal_rate", refusal["valuation_refusal_rate"]),
        _flag("refused_discount_rate", refusal["refused_discount_rate"]),
        _flag("unknown_basis_share", provenance["unknown_share"]),
        _flag("duplicate_resolution_rate", duplicates["duplicate_resolution_rate"]),
        _flag("borderline_merge_share", duplicates["borderline_merge_share"]),
        # The stored rate is the one that must be zero. The blocked rate is the
        # guard doing its job and is reported without being flagged.
        _flag("agent_disagreement_rate", agents["stored_rate"]),
        _flag("verification_pass_rate", verification["pass_rate"]),
        _flag("stalest_source_age_days", stalest),
    )

    synthetic = bool(
        session.scalar(
            select(func.count()).select_from(Source).where(Source.is_synthetic.is_(True))
        )
    )
    return QualityReport(
        captured_at=datetime.now(UTC),
        evidence_is_synthetic=synthetic,
        opportunity_count=int(session.scalar(select(func.count()).select_from(Opportunity)) or 0),
        property_count=property_count,
        field_completeness=by_field,
        field_completeness_overall=overall_completeness,
        confidence_distribution=confidence,
        duplicate_resolution=duplicates,
        source_freshness=freshness,
        agent_disagreement=agents,
        refusals=refusal,
        verification=verification,
        provenance=provenance,
        flags=flags,
    )


def persist_snapshot(session: Session, report: QualityReport) -> QualitySnapshot:
    """Store a reading. Append-only: drift is only visible as a series."""
    snapshot = QualitySnapshot(
        method_version=report.method_version,
        captured_at=report.captured_at,
        overall_status=report.overall_status.value,
        evidence_is_synthetic=report.evidence_is_synthetic,
        opportunity_count=report.opportunity_count,
        property_count=report.property_count,
        field_completeness=report.field_completeness_overall,
        mean_data_confidence=report.confidence_distribution["data_confidence"]["mean"],
        insufficient_data_rate=report.refusals["insufficient_data_rate"],
        refused_discount_rate=report.refusals["refused_discount_rate"],
        duplicate_resolution_rate=report.duplicate_resolution["duplicate_resolution_rate"],
        agent_disagreement_rate=report.agent_disagreement["stored_rate"],
        verification_pass_rate=report.verification["pass_rate"],
        stalest_source_age_seconds=(
            None
            if report.metric("stalest_source_age_days") is None
            else (report.metric("stalest_source_age_days") or 0.0) * 86400.0
        ),
        metrics=report.to_dict(),
        flags=[f.to_dict() for f in report.flags],
    )
    session.add(snapshot)
    session.flush()
    return snapshot


def latest_snapshot(session: Session) -> QualitySnapshot | None:
    return session.scalar(
        select(QualitySnapshot).order_by(QualitySnapshot.captured_at.desc()).limit(1)
    )


def snapshot_history(session: Session, limit: int = 30) -> list[QualitySnapshot]:
    return list(
        session.scalars(
            select(QualitySnapshot).order_by(QualitySnapshot.captured_at.desc()).limit(limit)
        )
    )


def _print_summary(report: QualityReport) -> None:
    if report.evidence_is_synthetic:
        print("!! Synthetic evidence is registered in this database; see TRACK-D.md.")
    print(f"   overall {report.overall_status.value}  ({report.opportunity_count} opportunities)")
    for flag in report.flags:
        value = "n/a" if flag.value is None else f"{flag.value:.4g}"
        print(f"   {flag.severity.value:<4} {flag.key:<28} {value:>10}")
    if report.regressions:
        print("   attention:")
        for flag in report.regressions:
            print(f"     [{flag.severity.value}] {flag.label}: {flag.note}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m sreoi_pipeline.quality",
        description="Take a data-quality reading and store it as a snapshot.",
    )
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--no-persist", action="store_true")
    args = parser.parse_args(argv)

    with session_scope() as session:
        report = collect_quality(session)
        if not args.no_persist:
            persist_snapshot(session, report)
        payload = report.to_dict()

    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        _print_summary(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
