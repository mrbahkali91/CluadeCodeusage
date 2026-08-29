"""End-to-end: submission -> property -> comparables -> valuation -> cost -> score."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from sreoi_domain.cost import Discount, DiscountRefused
from sreoi_domain.scoring import Classification
from sreoi_persistence.models import (
    DataProvenance,
    OpportunityScoreRow,
    SourceRecord,
    Transaction,
)
from sreoi_pipeline.ingest import IngestionError, ingest_manual_submission
from tests.conftest import requires_db

pytestmark = requires_db


def _score_rows(session: Session, opportunity_id: uuid.UUID) -> int:
    count = session.scalar(
        select(func.count())
        .select_from(OpportunityScoreRow)
        .where(OpportunityScoreRow.opportunity_id == opportunity_id)
    )
    return int(count or 0)


COMPLETE = {
    "external_id": "test-assignment-complete",
    "title": "Assignment — Sidrah",
    "opportunity_type": "ASSIGNMENT",
    "property_class": "APARTMENT",
    "district": "Sidrah",
    "area_sqm": 140,
    "bedrooms": 3,
    "floor": 4,
    "build_year": 2023,
    "seller_payment": 120000,
    "remaining_installments": 600000,
}


def test_seed_created_comparable_corpus(session: Session) -> None:
    count = session.scalar(select(func.count()).select_from(Transaction))
    assert count and count >= 200


def test_full_pipeline_produces_a_scored_opportunity(session: Session) -> None:
    opportunity, result = ingest_manual_submission(session, dict(COMPLETE))

    assert result.fair_value is not None, result.failure
    assert result.fair_value.low <= result.fair_value.base <= result.fair_value.high
    assert result.fair_value.comparable_count >= 3

    assert result.cost.total == 720000
    assert result.cost.is_complete

    assert isinstance(result.discount, Discount)
    assert 0 < result.discount.fraction < 1

    assert result.score is not None
    assert 0 <= result.score.total <= 100
    assert opportunity.id is not None


def test_missing_installments_refuses_the_discount(session: Session) -> None:
    """The invariant, end to end through the real pipeline."""
    payload = dict(COMPLETE)
    payload["external_id"] = "test-assignment-incomplete"
    del payload["remaining_installments"]

    _, result = ingest_manual_submission(session, payload)

    assert isinstance(result.discount, DiscountRefused)
    assert not result.cost.is_complete
    assert result.score is not None
    # Nothing may be recommended on an incomplete cost basis.
    assert result.score.classification is Classification.INSUFFICIENT_DATA


def test_comparables_and_weights_are_persisted(session: Session) -> None:
    payload = dict(COMPLETE)
    payload["external_id"] = "test-persist-comps"
    _, result = ingest_manual_submission(session, payload)
    session.flush()

    assert result.fair_value is not None
    stored = result.fair_value.comparables
    assert stored, "comparables must be retained for display"
    assert all(0 < c.weight <= 1.0 for c in stored)
    assert all(c.weight_breakdown for c in stored)


def test_provenance_is_written_for_cost_items(session: Session) -> None:
    payload = dict(COMPLETE)
    payload["external_id"] = "test-provenance"
    ingest_manual_submission(session, payload)
    session.flush()

    rows = session.scalars(
        select(DataProvenance).where(DataProvenance.entity_table == "cost_line_items")
    ).all()
    assert rows
    assert all(r.basis for r in rows)


def test_score_history_is_append_only(session: Session) -> None:
    payload = dict(COMPLETE)
    payload["external_id"] = "test-append-only"
    opportunity, _ = ingest_manual_submission(session, payload)
    session.flush()
    before = _score_rows(session, opportunity.id)

    # Re-ingesting identical content re-evaluates rather than duplicating the property.
    opportunity_again, _ = ingest_manual_submission(session, dict(payload))
    session.flush()
    after = _score_rows(session, opportunity.id)

    assert opportunity_again.id == opportunity.id, "identical content must be idempotent"
    assert after > before, "a re-evaluation must insert a new score row, never update"


def test_raw_payload_is_stored_and_redacted(session: Session) -> None:
    payload = dict(COMPLETE)
    payload["external_id"] = "test-raw-redaction"
    payload["description"] = "urgent, call 0551234567"
    opportunity, _ = ingest_manual_submission(session, payload)
    session.flush()

    record = session.get(SourceRecord, opportunity.source_record_id)
    assert record is not None
    assert record.content_hash
    assert "0551234567" not in record.raw_payload["description"]


def test_unknown_district_is_rejected(session: Session) -> None:
    payload = dict(COMPLETE)
    payload["external_id"] = "test-bad-district"
    payload["district"] = "Nowhere"
    with pytest.raises(IngestionError, match="unknown district"):
        ingest_manual_submission(session, payload)


def test_invalid_submission_is_rejected(session: Session) -> None:
    with pytest.raises(IngestionError):
        ingest_manual_submission(session, {"property_class": "APARTMENT"})
