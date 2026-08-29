"""Entity resolution, listings and timeline, end to end against PostGIS."""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from sreoi_persistence.models import (
    Listing,
    ListingSnapshot,
    Opportunity,
    OpportunityScoreRow,
    Property,
    PropertyMerge,
    PropertyTimelineEvent,
)
from sreoi_pipeline.ingest import detect_signals, ingest_manual_submission
from sreoi_pipeline.resolve import TimelineEvent
from tests.conftest import requires_db

pytestmark = requires_db

BASE: dict[str, Any] = {
    "opportunity_type": "ASSIGNMENT",
    "property_class": "APARTMENT",
    "district": "Sidrah",
    "area_sqm": 140,
    "bedrooms": 3,
    "floor": 4,
    "build_year": 2023,
    "seller_payment": 120000,
    "remaining_installments": 600000,
    "longitude": 46.8500,
    "latitude": 24.8700,
    "unit_number": "B-402",
}


def _count(session: Session, model: Any) -> int:
    return int(session.scalar(select(func.count()).select_from(model)) or 0)


def test_same_unit_from_two_sources_resolves_to_one_property(session: Session) -> None:
    first, _ = ingest_manual_submission(
        session, {**BASE, "external_id": "res-a", "title": "Assignment Sidrah B-402"}
    )
    second, _ = ingest_manual_submission(
        session,
        {
            **BASE,
            "external_id": "res-b",
            "title": "تنازل سدرة B-402",
            "area_sqm": 141,
            "unit_number": "b 402",
            "seller_payment": 135000,
        },
    )
    assert second.property_id == first.property_id

    merges = session.scalars(
        select(PropertyMerge).where(PropertyMerge.decision == "AUTO_MERGE")
    ).all()
    assert merges, "an automatic merge must be recorded and therefore reversible"
    assert float(merges[0].score) >= 0.85
    assert merges[0].components, "the decision must expose its components"


def test_different_unit_stays_separate(session: Session) -> None:
    first, _ = ingest_manual_submission(session, {**BASE, "external_id": "res-c"})
    other, _ = ingest_manual_submission(
        session,
        {
            **BASE,
            "external_id": "res-d",
            "area_sqm": 205,
            "bedrooms": 5,
            "floor": 11,
            "unit_number": "C-1105",
            "seller_payment": 300000,
        },
    )
    assert other.property_id != first.property_id


def test_relisting_creates_a_snapshot_not_a_duplicate_opportunity(session: Session) -> None:
    opportunity, _ = ingest_manual_submission(
        session, {**BASE, "external_id": "snap-a", "title": "Assignment"}
    )
    before = _count(session, Opportunity)

    again, _ = ingest_manual_submission(
        session, {**BASE, "external_id": "snap-a", "seller_payment": 108000}
    )
    session.flush()

    assert again.id == opportunity.id, "the same listing is one opportunity, re-evaluated"
    assert _count(session, Opportunity) == before

    snapshots = session.scalars(
        select(ListingSnapshot)
        .join(Listing, Listing.id == ListingSnapshot.listing_id)
        .where(Listing.external_id == "snap-a")
    ).all()
    assert len(snapshots) == 2, "snapshots are append-only"
    assert {float(s.asking_price) for s in snapshots if s.asking_price} == {120000.0, 108000.0}


def test_price_reduction_emits_a_timeline_event_and_signal(session: Session) -> None:
    opportunity, _ = ingest_manual_submission(session, {**BASE, "external_id": "cut-a"})
    ingest_manual_submission(session, {**BASE, "external_id": "cut-a", "seller_payment": 96000})
    session.flush()

    events = session.scalars(
        select(PropertyTimelineEvent).where(
            PropertyTimelineEvent.property_id == opportunity.property_id,
            PropertyTimelineEvent.event_type == TimelineEvent.PRICE_CHANGED,
        )
    ).all()
    assert events, "a price change must be recorded on the timeline"
    assert float(events[0].payload["fraction"]) < 0

    latest = session.scalar(
        select(ListingSnapshot)
        .join(Listing, Listing.id == ListingSnapshot.listing_id)
        .where(Listing.external_id == "cut-a")
        .order_by(ListingSnapshot.observed_at.desc())
        .limit(1)
    )
    assert latest is not None
    assert "REDUCED" in latest.signal_tags


def test_only_one_current_score_survives_re_evaluation(session: Session) -> None:
    opportunity, _ = ingest_manual_submission(session, {**BASE, "external_id": "score-a"})
    ingest_manual_submission(session, {**BASE, "external_id": "score-a", "seller_payment": 111000})
    session.flush()

    rows = session.scalars(
        select(OpportunityScoreRow).where(OpportunityScoreRow.opportunity_id == opportunity.id)
    ).all()
    current = [r for r in rows if r.superseded_at is None]
    assert len(rows) >= 2, "history is retained"
    assert len(current) == 1, "exactly one score is current"


def test_timeline_records_the_full_story(session: Session) -> None:
    opportunity, _ = ingest_manual_submission(session, {**BASE, "external_id": "tl-a"})
    session.flush()
    kinds = {
        e.event_type
        for e in session.scalars(
            select(PropertyTimelineEvent).where(
                PropertyTimelineEvent.property_id == opportunity.property_id
            )
        )
    }
    assert TimelineEvent.PROPERTY_CREATED in kinds
    assert TimelineEvent.LISTING_OBSERVED in kinds
    assert TimelineEvent.OPPORTUNITY_CREATED in kinds
    assert TimelineEvent.SCORED in kinds


def test_merged_property_is_enriched_not_overwritten(session: Session) -> None:
    sparse = {**BASE, "external_id": "enrich-a"}
    sparse.pop("build_year")
    first, _ = ingest_manual_submission(session, sparse)
    ingest_manual_submission(
        session, {**BASE, "external_id": "enrich-b", "build_year": 2022, "unit_number": "b402"}
    )
    session.flush()
    prop = session.get(Property, first.property_id)
    assert prop is not None
    assert prop.build_year == 2022, "a gap should be filled by the second sighting"


def test_signal_detection_handles_arabic_and_english() -> None:
    assert "ASSIGNMENT" in detect_signals({"description": "تنازل عن وحدة"})
    assert "URGENT" in detect_signals({"description": "عاجل للبيع"})
    assert "AUCTION" in detect_signals({"opportunity_type": "AUCTION"})
    assert detect_signals({"description": "spacious family home"}) == []
