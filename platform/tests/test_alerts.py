"""Watch rules, triggers, deduplication and dispatch.

The centrepiece is the brief's acceptance scenario (PRD section 7):

    "Monitor apartments around Qurtubah, Sidrah, Al Munsiyah and Al Rimal in
    Riyadh. Maximum total acquisition cost SAR 1.2M. Alert me when an Infath
    auction, resale, assignment, urgent sale or developer unit appears at least
    15% below estimated market value."

It must fire exactly once, on the right opportunity, for a stated reason -- and
not at all on an opportunity that fails any one of the user's conditions.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from sreoi_api.routers.watchlists import polygon_to_ewkt
from sreoi_api.search import OpportunityFilters, search
from sreoi_persistence.models import Opportunity
from sreoi_persistence.models_alerts import Alert, Notification, Watchlist, WatchRule
from sreoi_persistence.models_rental import RentalComparable as RentalComparableRow
from sreoi_pipeline.alerts import (
    DeliveryStatus,
    TriggerHit,
    TriggerKind,
    dedupe_key,
    default_channels,
    matching_opportunities,
    run_alert_scan,
)
from sreoi_pipeline.ingest import ingest_manual_submission
from sreoi_pipeline.rental import seed_rental_comparables
from tests.conftest import requires_db

pytestmark = requires_db

OWNER = "analyst@example.com"
DISTRICTS = ["Qurtubah", "Sidrah", "Al Munsiyah", "Al Rimal"]
ACCEPTANCE_TYPES = [
    "AUCTION",
    "RESALE",
    "ASSIGNMENT",
    "OFF_PLAN_RESALE",
    "DEVELOPER_INVENTORY",
]


def _rule(session: Session, name: str, **columns: Any) -> WatchRule:
    watchlist = Watchlist(name=f"watchlist for {name}", owner_ref=OWNER)
    session.add(watchlist)
    session.flush()
    rule = WatchRule(watchlist_id=watchlist.id, name=name, **columns)
    session.add(rule)
    session.flush()
    return rule


def _acceptance_rule(session: Session, **overrides: Any) -> WatchRule:
    """The brief's scenario, expressed exactly as the user stated it."""
    columns: dict[str, Any] = {
        "districts": DISTRICTS,
        "opportunity_types": ACCEPTANCE_TYPES,
        "property_class": "APARTMENT",
        "max_true_acquisition_cost": Decimal("1200000"),
        "min_discount_pct": 15.0,
        "triggers": [TriggerKind.NEW_OPPORTUNITY.value],
        "channels": ["IN_APP", "EMAIL"],
    }
    columns.update(overrides)
    return _rule(session, "15% below market, four districts, under SAR 1.2M", **columns)


@pytest.fixture
def graph(isolated: None, session: Session) -> Iterator[dict[str, Opportunity]]:
    """A small opportunity graph with one qualifying and three failing cases.

    Each failure isolates one of the user's conditions, so a matcher bug shows
    up as a specific wrong row rather than a vague count mismatch.
    """
    seed_rental_comparables(session)
    out: dict[str, Opportunity] = {}

    # Qualifies: fair value ~SAR 892k, complete cost SAR 720k => ~19% discount.
    out["qualifying"], _ = ingest_manual_submission(
        session,
        {
            "external_id": "alert-qualifying",
            "title": "Assignment (تنازل) — Sidrah S-401",
            "opportunity_type": "ASSIGNMENT",
            "property_class": "APARTMENT",
            "district": "Sidrah",
            "area_sqm": 140,
            "bedrooms": 3,
            "floor": 4,
            "build_year": 2023,
            "unit_number": "S-401",
            "longitude": 46.8500,
            "latitude": 24.8700,
            "seller_payment": 120000,
            "remaining_installments": 600000,
        },
    )

    # Fails only the discount clause: priced close to market.
    out["thin_discount"], _ = ingest_manual_submission(
        session,
        {
            "external_id": "alert-thin-discount",
            "title": "Urgent resale — Al Rimal R-118",
            "opportunity_type": "RESALE",
            "property_class": "APARTMENT",
            "district": "Al Rimal",
            "area_sqm": 150,
            "bedrooms": 3,
            "floor": 2,
            "build_year": 2020,
            "unit_number": "R-118",
            "longitude": 46.8300,
            "latitude": 24.8450,
            "seller_payment": 860000,
        },
    )

    # Fails only the budget clause: a genuine discount, but over SAR 1.2M.
    out["over_budget"], _ = ingest_manual_submission(
        session,
        {
            "external_id": "alert-over-budget",
            "title": "Developer inventory — Qurtubah Q-900",
            "opportunity_type": "DEVELOPER_INVENTORY",
            "property_class": "APARTMENT",
            "district": "Qurtubah",
            "area_sqm": 240,
            "bedrooms": 5,
            "floor": 6,
            "build_year": 2024,
            "unit_number": "Q-900",
            "longitude": 46.7600,
            "latitude": 24.8200,
            "seller_payment": 1300000,
        },
    )

    # Fails because the cost is not knowable: installments undisclosed.
    out["unknown_cost"], _ = ingest_manual_submission(
        session,
        {
            "external_id": "alert-unknown-cost",
            "title": "Assignment — Al Munsiyah M-207, balance undisclosed",
            "opportunity_type": "ASSIGNMENT",
            "property_class": "APARTMENT",
            "district": "Al Munsiyah",
            "area_sqm": 160,
            "bedrooms": 4,
            "floor": 3,
            "build_year": 2022,
            "unit_number": "M-207",
            "longitude": 46.7900,
            "latitude": 24.8300,
            "seller_payment": 200000,
        },
    )
    session.flush()
    yield out


# --------------------------------------------------------------- pure


def test_dedupe_key_is_stable_and_distinguishes_reason_and_version() -> None:
    rule_id, opportunity_id = uuid.uuid4(), uuid.uuid4()
    hit = TriggerHit(trigger="NEW_OPPORTUNITY", reason="because")
    base = dedupe_key(rule_id=rule_id, rule_version=1, opportunity_id=opportunity_id, hit=hit)

    assert base == dedupe_key(
        rule_id=rule_id, rule_version=1, opportunity_id=opportunity_id, hit=hit
    )
    # A different reason is a different interruption.
    assert base != dedupe_key(
        rule_id=rule_id,
        rule_version=1,
        opportunity_id=opportunity_id,
        hit=TriggerHit(trigger="PRICE_REDUCTION", reason="cut"),
    )
    # A different occurrence of the same trigger is a different interruption.
    assert base != dedupe_key(
        rule_id=rule_id,
        rule_version=1,
        opportunity_id=opportunity_id,
        hit=TriggerHit(trigger="NEW_OPPORTUNITY", reason="because", discriminator="snap-2"),
    )
    # Editing the rule deliberately re-opens alerting.
    assert base != dedupe_key(
        rule_id=rule_id, rule_version=2, opportunity_id=opportunity_id, hit=hit
    )
    # The reason text alone does not change identity: an improved wording must
    # not re-alert the whole back catalogue.
    assert base == dedupe_key(
        rule_id=rule_id,
        rule_version=1,
        opportunity_id=opportunity_id,
        hit=TriggerHit(trigger="NEW_OPPORTUNITY", reason="a better sentence"),
    )


def test_polygon_geojson_is_validated_before_it_reaches_postgis() -> None:
    ewkt = polygon_to_ewkt(
        {
            "type": "Polygon",
            "coordinates": [[[46.84, 24.86], [46.86, 24.86], [46.86, 24.88], [46.84, 24.88]]],
        }
    )
    assert ewkt.startswith("SRID=4326;POLYGON((")
    # The ring is closed for the caller rather than rejected.
    assert ewkt.count("46.84 24.86") == 2

    for bad in (
        {"type": "Point", "coordinates": [46.8, 24.8]},
        {"type": "Polygon", "coordinates": []},
        {"type": "Polygon", "coordinates": [[[46.8, 24.8], [46.9, 24.8]]]},
        {
            "type": "Polygon",
            "coordinates": [[[999.0, 24.8], [46.9, 24.8], [46.9, 24.9], [46.8, 24.8]]],
        },
    ):
        with pytest.raises(ValueError):
            polygon_to_ewkt(bad)


def test_polygons_with_holes_are_refused_rather_than_silently_flattened() -> None:
    square = [[46.8, 24.8], [46.9, 24.8], [46.9, 24.9], [46.8, 24.9], [46.8, 24.8]]
    hole = [[46.82, 24.82], [46.83, 24.82], [46.83, 24.83], [46.82, 24.82]]
    with pytest.raises(ValueError, match="holes"):
        polygon_to_ewkt({"type": "Polygon", "coordinates": [square, hole]})


# --------------------------------------------------------------- matching


def test_the_acceptance_rule_matches_exactly_the_qualifying_opportunity(
    graph: dict[str, Opportunity], session: Session
) -> None:
    rule = _acceptance_rule(session)
    matched = {m.opportunity_id for m in matching_opportunities(session, rule)}
    assert matched == {graph["qualifying"].id}


def test_an_incomplete_cost_never_satisfies_a_budget_rule(
    graph: dict[str, Opportunity], session: Session
) -> None:
    """The most dangerous filter bug this product could ship.

    The undisclosed-balance assignment has a *recorded* total of SAR 200,000.
    Compared naively against a SAR 1.2M budget it is the cheapest thing on the
    market; in reality nobody knows what it costs. A budget rule must refuse it.
    """
    rule = _rule(
        session,
        "budget only",
        districts=DISTRICTS,
        max_true_acquisition_cost=Decimal("1200000"),
    )
    matched = {m.opportunity_id for m in matching_opportunities(session, rule)}
    assert graph["unknown_cost"].id not in matched
    assert graph["qualifying"].id in matched
    assert graph["over_budget"].id not in matched


@pytest.mark.parametrize(
    "columns,filters",
    [
        (
            {"districts": DISTRICTS},
            {"districts": DISTRICTS},
        ),
        (
            {"opportunity_types": ["ASSIGNMENT"]},
            {"opportunity_types": ["ASSIGNMENT"]},
        ),
        (
            {"property_class": "APARTMENT"},
            {"property_class": "APARTMENT"},
        ),
        (
            {"max_true_acquisition_cost": Decimal("1200000")},
            {"max_cost": Decimal("1200000")},
        ),
        (
            {"min_discount_pct": 15.0},
            {"min_discount_pct": 15.0},
        ),
        (
            {"min_score": 70.0},
            {"min_score": 70.0},
        ),
        (
            {
                "districts": DISTRICTS,
                "opportunity_types": ACCEPTANCE_TYPES,
                "property_class": "APARTMENT",
                "max_true_acquisition_cost": Decimal("1200000"),
                "min_discount_pct": 15.0,
            },
            {
                "districts": DISTRICTS,
                "opportunity_types": ACCEPTANCE_TYPES,
                "property_class": "APARTMENT",
                "max_cost": Decimal("1200000"),
                "min_discount_pct": 15.0,
            },
        ),
    ],
)
def test_matcher_agrees_with_the_search_read_model(
    graph: dict[str, Opportunity],
    session: Session,
    columns: dict[str, Any],
    filters: dict[str, Any],
) -> None:
    """A rule that fires must correspond to a search returning the same rows.

    Asserted against the real `sreoi_api.search` implementation rather than a
    restatement of it, because the two live in different layers and would
    otherwise drift.
    """
    rule = _rule(session, "equivalence", **columns)
    from_rule = {m.opportunity_id for m in matching_opportunities(session, rule)}
    from_search = {r["id"] for r in search(session, OpportunityFilters(**filters))}
    assert from_rule == from_search


def test_a_polygon_rule_includes_what_is_inside_and_excludes_what_is_not(
    graph: dict[str, Opportunity], session: Session
) -> None:
    sidrah_box = polygon_to_ewkt(
        {
            "type": "Polygon",
            "coordinates": [
                [[46.840, 24.860], [46.860, 24.860], [46.860, 24.880], [46.840, 24.880]]
            ],
        }
    )
    rule = _rule(session, "sidrah polygon", polygon=sidrah_box)
    matched = {m.opportunity_id for m in matching_opportunities(session, rule)}
    assert graph["qualifying"].id in matched
    assert graph["thin_discount"].id not in matched
    assert graph["over_budget"].id not in matched


# --------------------------------------------------------------- scanning


def test_acceptance_scenario_fires_exactly_one_alert_with_a_stated_reason(
    graph: dict[str, Opportunity], session: Session
) -> None:
    rule = _acceptance_rule(session)
    result = run_alert_scan(session, rule_ids=[rule.id])

    assert result.alerts_created == 1
    alerts = session.scalars(select(Alert).where(Alert.watch_rule_id == rule.id)).all()
    assert len(alerts) == 1

    alert = alerts[0]
    assert alert.opportunity_id == graph["qualifying"].id
    assert alert.trigger == TriggerKind.NEW_OPPORTUNITY.value
    assert alert.rule_version == 1
    assert "below estimated market value" in alert.reason
    assert "true acquisition cost" in alert.reason

    # The alert carries the rule it fired under and the figures that satisfied it.
    assert alert.payload["rule"]["min_discount_pct"] == 15.0
    assert alert.payload["rule"]["max_true_acquisition_cost"] == 1200000.0
    assert alert.payload["evidence"]["discount_percent"] >= 15.0
    assert alert.payload["evidence"]["true_acquisition_cost"] <= 1200000.0


def test_non_qualifying_opportunities_produce_no_alert(
    graph: dict[str, Opportunity], session: Session
) -> None:
    rule = _acceptance_rule(session)
    run_alert_scan(session, rule_ids=[rule.id])
    alerted = {
        a.opportunity_id
        for a in session.scalars(select(Alert).where(Alert.watch_rule_id == rule.id))
    }
    for key in ("thin_discount", "over_budget", "unknown_cost"):
        assert graph[key].id not in alerted, f"{key} must not alert"


def test_rescanning_an_unchanged_rule_creates_no_duplicate_alerts(
    graph: dict[str, Opportunity], session: Session
) -> None:
    """Alert fatigue is a defect, not a preference."""
    rule = _acceptance_rule(session)
    first = run_alert_scan(session, rule_ids=[rule.id])
    second = run_alert_scan(session, rule_ids=[rule.id])
    third = run_alert_scan(session, rule_ids=[rule.id])

    assert first.alerts_created == 1
    assert second.alerts_created == 0
    assert second.alerts_suppressed == 1
    assert third.alerts_created == 0
    total = session.scalars(select(Alert).where(Alert.watch_rule_id == rule.id)).all()
    assert len(total) == 1


def test_editing_a_rule_bumps_its_version_and_deliberately_re_opens_alerting(
    graph: dict[str, Opportunity], session: Session
) -> None:
    rule = _acceptance_rule(session)
    run_alert_scan(session, rule_ids=[rule.id])
    rule.version += 1
    session.flush()
    again = run_alert_scan(session, rule_ids=[rule.id])

    assert again.alerts_created == 1
    versions = sorted(
        a.rule_version for a in session.scalars(select(Alert).where(Alert.watch_rule_id == rule.id))
    )
    assert versions == [1, 2]


def test_a_price_reduction_fires_its_own_alert_once(
    graph: dict[str, Opportunity], session: Session
) -> None:
    rule = _acceptance_rule(session, triggers=[TriggerKind.PRICE_REDUCTION.value])
    assert run_alert_scan(session, rule_ids=[rule.id]).alerts_created == 0

    # The seller cuts the price. Same listing, a new append-only snapshot.
    ingest_manual_submission(
        session,
        {
            "external_id": "alert-qualifying",
            "title": "Assignment (تنازل) — Sidrah S-401 — URGENT",
            "opportunity_type": "ASSIGNMENT",
            "property_class": "APARTMENT",
            "district": "Sidrah",
            "area_sqm": 140,
            "bedrooms": 3,
            "floor": 4,
            "build_year": 2023,
            "unit_number": "S-401",
            "longitude": 46.8500,
            "latitude": 24.8700,
            "seller_payment": 95000,
            "remaining_installments": 600000,
        },
    )
    session.flush()

    after = run_alert_scan(session, rule_ids=[rule.id])
    assert after.alerts_created == 1
    alert = session.scalars(select(Alert).where(Alert.watch_rule_id == rule.id)).one()
    assert alert.trigger == TriggerKind.PRICE_REDUCTION.value
    assert "reduced" in alert.reason
    assert alert.payload["trigger_detail"]["change_fraction"] < 0

    # And it does not repeat on the next scan.
    assert run_alert_scan(session, rule_ids=[rule.id]).alerts_created == 0


def test_score_crossing_fires_only_when_the_threshold_is_crossed_from_below(
    graph: dict[str, Opportunity], session: Session
) -> None:
    opportunity = graph["qualifying"]
    current = max(
        matching_opportunities(session, _rule(session, "probe")),
        key=lambda m: m.score or 0.0,
    )
    assert current.score is not None

    # A threshold just above the current score: nothing has crossed it yet.
    rule = _rule(
        session,
        "score crossing",
        districts=DISTRICTS,
        min_score=current.score + 1.0,
        triggers=[TriggerKind.SCORE_THRESHOLD_CROSSED.value],
    )
    assert run_alert_scan(session, rule_ids=[rule.id]).alerts_created == 0

    # A price cut raises the discount and therefore the score.
    ingest_manual_submission(
        session,
        {
            "external_id": "alert-qualifying",
            "title": "Assignment — Sidrah S-401 — reduced",
            "opportunity_type": "ASSIGNMENT",
            "property_class": "APARTMENT",
            "district": "Sidrah",
            "area_sqm": 140,
            "bedrooms": 3,
            "floor": 4,
            "build_year": 2023,
            "unit_number": "S-401",
            "longitude": 46.8500,
            "latitude": 24.8700,
            "seller_payment": 60000,
            "remaining_installments": 600000,
        },
    )
    session.flush()

    after = run_alert_scan(session, rule_ids=[rule.id])
    assert after.alerts_created == 1
    alert = session.scalars(select(Alert).where(Alert.watch_rule_id == rule.id)).one()
    assert alert.opportunity_id == opportunity.id
    assert alert.trigger == TriggerKind.SCORE_THRESHOLD_CROSSED.value
    assert "crossing this rule's threshold" in alert.reason
    assert (
        alert.payload["trigger_detail"]["previous_score"]
        < alert.payload["trigger_detail"]["threshold"]
    )


def test_new_comparable_fires_only_after_the_first_scan_sets_the_watermark(
    graph: dict[str, Opportunity], session: Session
) -> None:
    """The first scan must not alert on the entire back catalogue of evidence."""
    rule = _acceptance_rule(session, triggers=[TriggerKind.NEW_COMPARABLE.value])
    t1 = datetime.now(UTC) - timedelta(hours=2)
    assert run_alert_scan(session, rule_ids=[rule.id], now=t1).alerts_created == 0
    assert rule.last_evaluated_at == t1

    existing = session.scalars(select(RentalComparableRow).limit(1)).one()
    session.add(
        RentalComparableRow(
            source_id=existing.source_id,
            district_id=existing.district_id,
            location="SRID=4326;POINT(46.8501 24.8701)",
            annual_rent=Decimal("68000.00"),
            area_sqm=140.0,
            contract_date=datetime.now(UTC).date(),
            property_class="APARTMENT",
            build_year=2023,
            floor=4,
            ingested_at=datetime.now(UTC) - timedelta(hours=1),
        )
    )
    session.flush()

    after = run_alert_scan(session, rule_ids=[rule.id], now=datetime.now(UTC))
    assert after.alerts_created == 1
    alert = session.scalars(select(Alert).where(Alert.watch_rule_id == rule.id)).one()
    assert alert.trigger == TriggerKind.NEW_COMPARABLE.value
    assert alert.payload["trigger_detail"]["new_comparables"] >= 1


def test_auction_deadline_trigger_is_wired_but_deliberately_silent(
    graph: dict[str, Opportunity], session: Session
) -> None:
    """It is a named stub, not a forgotten branch: it must not crash or fire."""
    rule = _acceptance_rule(session, triggers=[TriggerKind.AUCTION_DEADLINE.value])
    assert run_alert_scan(session, rule_ids=[rule.id]).alerts_created == 0


# --------------------------------------------------------------- dispatch


def test_in_app_is_delivered_and_email_is_recorded_as_not_sent(
    graph: dict[str, Opportunity], session: Session
) -> None:
    rule = _acceptance_rule(session)
    run_alert_scan(session, rule_ids=[rule.id])
    alert = session.scalars(select(Alert).where(Alert.watch_rule_id == rule.id)).one()

    notifications = {
        n.channel: n
        for n in session.scalars(select(Notification).where(Notification.alert_id == alert.id))
    }
    assert set(notifications) == {"IN_APP", "EMAIL"}
    assert notifications["IN_APP"].status == DeliveryStatus.DELIVERED.value
    # The stub must not claim a delivery that did not happen.
    assert notifications["EMAIL"].status == DeliveryStatus.LOGGED_NOT_SENT.value
    assert "NOT sent" in (notifications["EMAIL"].detail or "")


def test_an_unregistered_channel_is_recorded_as_failed_not_silently_dropped(
    graph: dict[str, Opportunity], session: Session
) -> None:
    rule = _acceptance_rule(session, channels=["IN_APP", "SMS"])
    run_alert_scan(session, rule_ids=[rule.id], channels=default_channels())
    alert = session.scalars(select(Alert).where(Alert.watch_rule_id == rule.id)).one()
    statuses = {
        n.channel: n.status
        for n in session.scalars(select(Notification).where(Notification.alert_id == alert.id))
    }
    assert statuses["SMS"] == DeliveryStatus.FAILED.value


def test_a_disabled_rule_is_never_evaluated(
    graph: dict[str, Opportunity], session: Session
) -> None:
    rule = _acceptance_rule(session, enabled=False)
    result = run_alert_scan(session, rule_ids=[rule.id])
    assert result.rules_evaluated == 0
    assert result.alerts_created == 0


def test_an_alert_without_a_reason_is_rejected_by_the_database(
    graph: dict[str, Opportunity], session: Session
) -> None:
    """An alert that cannot say why it fired is noise, so the schema forbids it."""
    rule = _acceptance_rule(session)
    with pytest.raises(IntegrityError), session.begin_nested():
        session.add(
            Alert(
                watchlist_id=rule.watchlist_id,
                watch_rule_id=rule.id,
                opportunity_id=graph["qualifying"].id,
                rule_version=rule.version,
                trigger=TriggerKind.NEW_OPPORTUNITY.value,
                reason="   ",
                dedupe_key="reasonless",
            )
        )
        session.flush()


def test_the_dedupe_key_is_unique_in_the_database(
    graph: dict[str, Opportunity], session: Session
) -> None:
    rule = _acceptance_rule(session)
    run_alert_scan(session, rule_ids=[rule.id])
    existing = session.scalars(select(Alert).where(Alert.watch_rule_id == rule.id)).one()
    with pytest.raises(IntegrityError), session.begin_nested():
        session.add(
            Alert(
                watchlist_id=rule.watchlist_id,
                watch_rule_id=rule.id,
                opportunity_id=graph["qualifying"].id,
                rule_version=rule.version,
                trigger=TriggerKind.NEW_OPPORTUNITY.value,
                reason="a duplicate slipping past the application check",
                dedupe_key=existing.dedupe_key,
            )
        )
        session.flush()
