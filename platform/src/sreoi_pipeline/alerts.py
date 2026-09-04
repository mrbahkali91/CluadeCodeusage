"""Watch-rule matching, alert generation and dispatch (Slice 3).

Three properties are load-bearing.

**The matcher agrees with search.** The SQL below mirrors
`sreoi_api.search._base_query` / `_apply` filter for filter, including the one
non-obvious rule that a budget may only be compared against a *complete* cost.
A rule that fires must correspond to a search that returns the same row;
`tests/test_alerts.py` asserts that equivalence against the real search code
rather than restating it. The duplication exists because the architecture
contract (ADR-001, `.importlinter`) forbids `sreoi_pipeline` importing
`sreoi_api`, so this module cannot call `search()` directly. That is a seam the
coordinator should close by moving the read model down a layer.

**Deduplication is enforced, not intended.** Alert fatigue is a defect, not a
preference. The same opportunity never re-alerts on the same rule version for
the same reason: a deterministic dedupe key is checked before insert and backed
by a unique index, so even a concurrent scan cannot produce a double.

**Channels are a port.** In-app deliveries are persisted; the email channel has
no transport in this slice and records `LOGGED_NOT_SENT` rather than claiming a
delivery that did not happen.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Protocol

from geoalchemy2 import Geometry
from sqlalchemy import Float, Select, cast, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from sreoi_persistence.models import (
    District,
    Listing,
    ListingSnapshot,
    Opportunity,
    OpportunityScoreRow,
    Property,
    TrueAcquisitionCostRow,
    Valuation,
)
from sreoi_persistence.models_alerts import (
    Alert,
    Notification,
    Watchlist,
    WatchRule,
)
from sreoi_persistence.models_rental import RentalEstimateRow
from sreoi_pipeline.rental import RentalComparableRepository

logger = logging.getLogger(__name__)

METHOD_VERSION = "alerting-v1"


class TriggerKind(StrEnum):
    """Why a user was interrupted. PRD section 5.9."""

    NEW_OPPORTUNITY = "NEW_OPPORTUNITY"
    PRICE_REDUCTION = "PRICE_REDUCTION"
    SCORE_THRESHOLD_CROSSED = "SCORE_THRESHOLD_CROSSED"
    NEW_COMPARABLE = "NEW_COMPARABLE"
    AUCTION_DEADLINE = "AUCTION_DEADLINE"


DEFAULT_TRIGGERS: tuple[str, ...] = (
    TriggerKind.NEW_OPPORTUNITY.value,
    TriggerKind.PRICE_REDUCTION.value,
    TriggerKind.SCORE_THRESHOLD_CROSSED.value,
    TriggerKind.NEW_COMPARABLE.value,
)


class ChannelKey(StrEnum):
    IN_APP = "IN_APP"
    EMAIL = "EMAIL"


DEFAULT_CHANNELS: tuple[str, ...] = (ChannelKey.IN_APP.value, ChannelKey.EMAIL.value)


class DeliveryStatus(StrEnum):
    DELIVERED = "DELIVERED"
    # The email stub. Named so an operator reading the table cannot mistake a
    # logged alert for a sent one.
    LOGGED_NOT_SENT = "LOGGED_NOT_SENT"
    FAILED = "FAILED"


# --------------------------------------------------------------- channels


@dataclass(frozen=True, slots=True)
class AlertEnvelope:
    """Everything a channel needs, with no ORM object in sight."""

    alert_id: uuid.UUID
    recipient: str
    subject: str
    body: str
    trigger: str
    reason: str
    opportunity_id: uuid.UUID


@dataclass(frozen=True, slots=True)
class Delivery:
    channel: str
    status: str
    detail: str | None = None


class NotificationChannel(Protocol):
    """The port. Adding SMS or WhatsApp is a new implementation, not a change here."""

    key: str

    def deliver(self, envelope: AlertEnvelope) -> Delivery: ...


class InAppChannel:
    """In-app delivery: the notification row *is* the delivery."""

    key = ChannelKey.IN_APP.value

    def deliver(self, envelope: AlertEnvelope) -> Delivery:
        return Delivery(
            channel=self.key,
            status=DeliveryStatus.DELIVERED.value,
            detail="available in the in-app alert feed",
        )


class LoggingEmailChannel:
    """Email stub. Logs, and says plainly that nothing was sent.

    A stub that recorded DELIVERED would be a lie an operator discovers only
    when a user misses an auction, so the status is LOGGED_NOT_SENT.
    """

    key = ChannelKey.EMAIL.value

    def deliver(self, envelope: AlertEnvelope) -> Delivery:
        logger.info(
            "EMAIL NOT SENT (no transport configured in this slice): to=%s subject=%r reason=%s",
            envelope.recipient,
            envelope.subject,
            envelope.reason,
        )
        return Delivery(
            channel=self.key,
            status=DeliveryStatus.LOGGED_NOT_SENT.value,
            detail="no email transport configured in this slice: logged only, NOT sent",
        )


def default_channels() -> dict[str, NotificationChannel]:
    return {c.key: c for c in (InAppChannel(), LoggingEmailChannel())}


# --------------------------------------------------------------- matching


@dataclass(frozen=True, slots=True)
class MatchRow:
    """One opportunity that satisfies a rule, with the figures that satisfied it."""

    opportunity_id: uuid.UUID
    property_id: uuid.UUID
    title: str
    opportunity_type: str
    property_class: str
    area_sqm: float
    district: str | None
    longitude: float | None
    latitude: float | None
    true_acquisition_cost: Decimal | None
    fair_value_base: Decimal | None
    discount_fraction: float | None
    score: float | None
    classification: str | None
    score_row_id: uuid.UUID | None
    gross_yield: float | None
    net_yield: float | None
    annual_rent: Decimal | None

    def evidence(self) -> dict[str, Any]:
        """The numbers that made this a match, stored on the alert."""
        return {
            "true_acquisition_cost": float(self.true_acquisition_cost)
            if self.true_acquisition_cost is not None
            else None,
            "fair_value_base": float(self.fair_value_base)
            if self.fair_value_base is not None
            else None,
            "discount_percent": self.discount_fraction * 100.0
            if self.discount_fraction is not None
            else None,
            "score": self.score,
            "classification": self.classification,
            "gross_yield_percent": self.gross_yield * 100.0
            if self.gross_yield is not None
            else None,
            "net_yield_percent": self.net_yield * 100.0 if self.net_yield is not None else None,
            "annual_rent": float(self.annual_rent) if self.annual_rent is not None else None,
        }


def _latest(model: Any, order_col: Any) -> Any:
    """Most recent row per opportunity. Same shape as `search._latest`."""
    return select(
        model,
        func.row_number()
        .over(partition_by=model.opportunity_id, order_by=order_col.desc())
        .label("rn"),
    ).subquery()


def rule_snapshot(rule: WatchRule, *, has_polygon: bool | None = None) -> dict[str, Any]:
    """The rule as it stood when it fired, echoed onto the alert.

    Stored rather than referenced so an alert stays explainable after the rule
    is edited: "the rule changed", not "the market changed".
    """
    return {
        "rule_id": str(rule.id),
        "rule_name": rule.name,
        "rule_version": rule.version,
        "districts": list(rule.districts or []),
        "opportunity_types": list(rule.opportunity_types or []),
        "property_class": rule.property_class,
        "max_true_acquisition_cost": float(rule.max_true_acquisition_cost)
        if rule.max_true_acquisition_cost is not None
        else None,
        "min_discount_pct": float(rule.min_discount_pct)
        if rule.min_discount_pct is not None
        else None,
        "min_score": float(rule.min_score) if rule.min_score is not None else None,
        "min_gross_yield": float(rule.min_gross_yield)
        if rule.min_gross_yield is not None
        else None,
        "has_polygon": (rule.polygon is not None) if has_polygon is None else has_polygon,
        "triggers": list(rule.triggers or DEFAULT_TRIGGERS),
        "method_version": METHOD_VERSION,
    }


def _matching_statement(rule: WatchRule) -> Select[Any]:
    valuation = _latest(Valuation, Valuation.computed_at)
    cost = _latest(TrueAcquisitionCostRow, TrueAcquisitionCostRow.computed_at)
    rental = _latest(RentalEstimateRow, RentalEstimateRow.computed_at)
    score = (
        select(
            OpportunityScoreRow,
            func.row_number()
            .over(
                partition_by=OpportunityScoreRow.opportunity_id,
                order_by=OpportunityScoreRow.computed_at.desc(),
            )
            .label("rn"),
        )
        .where(OpportunityScoreRow.superseded_at.is_(None))
        .subquery()
    )

    geom = cast(Property.location, Geometry)
    stmt = (
        select(
            Opportunity,
            Property,
            District,
            valuation.c.fair_value_base,
            cost.c.total,
            cost.c.is_complete,
            score.c.id.label("score_row_id"),
            score.c.total_score,
            score.c.classification,
            score.c.discount_fraction,
            rental.c.gross_yield,
            rental.c.net_yield,
            rental.c.annual_rent,
            func.ST_X(geom).cast(Float).label("lon"),
            func.ST_Y(geom).cast(Float).label("lat"),
        )
        .join(Property, Property.id == Opportunity.property_id)
        .outerjoin(District, District.id == Property.district_id)
        .outerjoin(
            valuation, (valuation.c.opportunity_id == Opportunity.id) & (valuation.c.rn == 1)
        )
        .outerjoin(cost, (cost.c.opportunity_id == Opportunity.id) & (cost.c.rn == 1))
        .outerjoin(score, (score.c.opportunity_id == Opportunity.id) & (score.c.rn == 1))
        .outerjoin(rental, (rental.c.opportunity_id == Opportunity.id) & (rental.c.rn == 1))
        .where(Opportunity.status == "ACTIVE")
    )

    # Every clause below mirrors `sreoi_api.search._apply`.
    if rule.districts:
        stmt = stmt.where(District.name_en.in_(list(rule.districts)))
    if rule.opportunity_types:
        stmt = stmt.where(Opportunity.opportunity_type.in_(list(rule.opportunity_types)))
    if rule.property_class:
        stmt = stmt.where(Property.property_class == rule.property_class)
    if rule.max_true_acquisition_cost is not None:
        # Only a complete cost figure may be compared against a budget. An
        # incomplete cost is smaller than the real one, so without this a rule
        # would fire hardest on exactly the opportunities it understands least.
        stmt = stmt.where(
            cost.c.is_complete.is_(True), cost.c.total <= rule.max_true_acquisition_cost
        )
    if rule.min_discount_pct is not None:
        stmt = stmt.where(score.c.discount_fraction >= float(rule.min_discount_pct) / 100.0)
    if rule.min_score is not None:
        stmt = stmt.where(score.c.total_score >= float(rule.min_score))
    # Extension beyond the current search filters: no yield filter exists in
    # `sreoi_api.search` yet, so equivalence is asserted over the shared set.
    if rule.min_gross_yield is not None:
        stmt = stmt.where(rental.c.gross_yield >= float(rule.min_gross_yield))
    if rule.polygon is not None:
        # Read the stored geography back as a scalar subquery rather than
        # joining `watch_rules` into the projection, which would cross-join.
        polygon = select(WatchRule.polygon).where(WatchRule.id == rule.id).scalar_subquery()
        stmt = stmt.where(func.ST_Intersects(Property.location, polygon))

    return stmt.order_by(Opportunity.created_at.desc())


def matching_opportunities(session: Session, rule: WatchRule) -> list[MatchRow]:
    """Opportunities currently satisfying a rule's filter."""
    rows = session.execute(_matching_statement(rule)).all()
    return [
        MatchRow(
            opportunity_id=r.Opportunity.id,
            property_id=r.Property.id,
            title=r.Opportunity.title,
            opportunity_type=r.Opportunity.opportunity_type,
            property_class=r.Property.property_class,
            area_sqm=float(r.Property.built_area_sqm),
            district=r.District.name_en if r.District else None,
            longitude=r.lon,
            latitude=r.lat,
            true_acquisition_cost=(
                Decimal(r.total) if r.is_complete and r.total is not None else None
            ),
            fair_value_base=Decimal(r.fair_value_base) if r.fair_value_base is not None else None,
            discount_fraction=(
                float(r.discount_fraction) if r.discount_fraction is not None else None
            ),
            score=float(r.total_score) if r.total_score is not None else None,
            classification=r.classification,
            score_row_id=r.score_row_id,
            gross_yield=float(r.gross_yield) if r.gross_yield is not None else None,
            net_yield=float(r.net_yield) if r.net_yield is not None else None,
            annual_rent=Decimal(r.annual_rent) if r.annual_rent is not None else None,
        )
        for r in rows
    ]


# --------------------------------------------------------------- triggers


@dataclass(frozen=True, slots=True)
class TriggerHit:
    """A reason to interrupt the user, and the discriminator that dedupes it."""

    trigger: str
    reason: str
    # Distinguishes one occurrence of a repeatable trigger from the next (the
    # snapshot that cut the price, the score row that crossed the threshold).
    # Empty for triggers that may fire at most once per rule version.
    discriminator: str = ""
    detail: dict[str, Any] = field(default_factory=dict)


def dedupe_key(
    *, rule_id: uuid.UUID, rule_version: int, opportunity_id: uuid.UUID, hit: TriggerHit
) -> str:
    """Deterministic identity of (rule version, opportunity, reason).

    Including the rule version means editing a rule deliberately re-opens
    alerting for it -- a new thesis deserves a fresh look -- while re-running
    the scanner any number of times against an unchanged rule produces nothing.
    """
    raw = "|".join(
        [str(rule_id), str(rule_version), str(opportunity_id), hit.trigger, hit.discriminator]
    )
    return hashlib.sha256(raw.encode()).hexdigest()


def _price_reduction_hit(session: Session, match: MatchRow) -> TriggerHit | None:
    """The two most recent asking prices for this property, across its listings."""
    rows = list(
        session.execute(
            select(ListingSnapshot.id, ListingSnapshot.asking_price)
            .join(Listing, Listing.id == ListingSnapshot.listing_id)
            .where(Listing.property_id == match.property_id)
            .order_by(ListingSnapshot.observed_at.desc(), ListingSnapshot.id.desc())
            .limit(2)
        ).all()
    )
    if len(rows) < 2:
        return None
    (latest_id, latest_price), (_prev_id, prev_price) = rows
    if latest_price is None or prev_price is None or prev_price <= 0:
        return None
    if latest_price >= prev_price:
        return None
    fraction = float((latest_price - prev_price) / prev_price)
    return TriggerHit(
        trigger=TriggerKind.PRICE_REDUCTION.value,
        reason=(
            f"asking price reduced {abs(fraction) * 100:.1f}% from "
            f"SAR {float(prev_price):,.0f} to SAR {float(latest_price):,.0f}"
        ),
        discriminator=str(latest_id),
        detail={
            "previous_asking_price": float(prev_price),
            "asking_price": float(latest_price),
            "change_fraction": round(fraction, 5),
        },
    )


def _score_crossing_hit(session: Session, rule: WatchRule, match: MatchRow) -> TriggerHit | None:
    """Fires when the score crossed the rule's own threshold from below.

    Requires a `min_score`: without a threshold there is nothing to cross, and
    inventing one would interrupt the user on a number they never named.
    """
    if rule.min_score is None or match.score is None or match.score_row_id is None:
        return None
    threshold = float(rule.min_score)
    if match.score < threshold:
        return None
    previous = session.scalar(
        select(OpportunityScoreRow.total_score)
        .where(
            OpportunityScoreRow.opportunity_id == match.opportunity_id,
            OpportunityScoreRow.id != match.score_row_id,
        )
        .order_by(OpportunityScoreRow.computed_at.desc())
        .limit(1)
    )
    if previous is None or float(previous) >= threshold:
        return None
    return TriggerHit(
        trigger=TriggerKind.SCORE_THRESHOLD_CROSSED.value,
        reason=(
            f"opportunity score rose from {float(previous):.1f} to {match.score:.1f}, "
            f"crossing this rule's threshold of {threshold:.1f}"
        ),
        discriminator=str(match.score_row_id),
        detail={
            "previous_score": float(previous),
            "score": match.score,
            "threshold": threshold,
        },
    )


def _new_comparable_hit(
    session: Session, rule: WatchRule, match: MatchRow, *, now: datetime
) -> TriggerHit | None:
    """Fires when new rental evidence relevant to this property has arrived.

    Scoped to rental comparables because `transactions` carries no ingestion
    timestamp -- only the transaction date -- so "new to us" is not answerable
    for sale evidence without a schema change owned elsewhere. Recorded here
    rather than silently omitted.
    """
    if rule.last_evaluated_at is None:
        # First scan establishes the watermark; it does not fire, or every rule
        # would alert on the entire back catalogue of evidence on creation.
        return None
    if match.longitude is None or match.latitude is None:
        return None
    since = rule.last_evaluated_at
    count = RentalComparableRepository(session).count_since(
        longitude=match.longitude,
        latitude=match.latitude,
        property_class=match.property_class,
        area_sqm=match.area_sqm,
        since=since,
    )
    if count <= 0:
        return None
    return TriggerHit(
        trigger=TriggerKind.NEW_COMPARABLE.value,
        reason=(
            f"{count} new relevant rental comparable(s) since "
            f"{since.isoformat(timespec='seconds')}; the rent and yield evidence changed"
        ),
        discriminator=since.isoformat(timespec="seconds"),
        detail={"new_comparables": count, "since": since.isoformat()},
    )


def _auction_deadline_hit(match: MatchRow) -> TriggerHit | None:
    """Deliberately stubbed.

    TODO(slice-5): PRD section 5.9 also requires "auction opening/closing" and
    "bid crossing the user's recommended maximum". Neither is implementable
    yet: `opportunities` has no auction window and no bid ladder, and inventing
    a deadline from ingestion time would produce alerts about a date nobody
    published. The trigger is named and wired so the schema change is the only
    remaining work.
    """
    return None


def evaluate_triggers(
    session: Session, rule: WatchRule, match: MatchRow, *, now: datetime
) -> list[TriggerHit]:
    """All reasons this rule should interrupt the user about this opportunity."""
    enabled = set(rule.triggers or DEFAULT_TRIGGERS)
    hits: list[TriggerHit] = []

    if TriggerKind.NEW_OPPORTUNITY.value in enabled:
        hits.append(
            TriggerHit(
                trigger=TriggerKind.NEW_OPPORTUNITY.value,
                reason=_new_opportunity_reason(rule, match),
                detail={"matched_at": now.isoformat()},
            )
        )
    if TriggerKind.PRICE_REDUCTION.value in enabled:
        hit = _price_reduction_hit(session, match)
        if hit is not None:
            hits.append(hit)
    if TriggerKind.SCORE_THRESHOLD_CROSSED.value in enabled:
        hit = _score_crossing_hit(session, rule, match)
        if hit is not None:
            hits.append(hit)
    if TriggerKind.NEW_COMPARABLE.value in enabled:
        hit = _new_comparable_hit(session, rule, match, now=now)
        if hit is not None:
            hits.append(hit)
    if TriggerKind.AUCTION_DEADLINE.value in enabled:
        hit = _auction_deadline_hit(match)
        if hit is not None:
            hits.append(hit)
    return hits


def _new_opportunity_reason(rule: WatchRule, match: MatchRow) -> str:
    """Say which of the user's own conditions were met, in their terms."""
    parts: list[str] = []
    if match.discount_fraction is not None:
        parts.append(f"{match.discount_fraction * 100:.1f}% below estimated market value")
    if match.true_acquisition_cost is not None:
        parts.append(f"true acquisition cost SAR {float(match.true_acquisition_cost):,.0f}")
    if match.gross_yield is not None:
        parts.append(f"gross yield {match.gross_yield * 100:.1f}%")
    if match.score is not None:
        parts.append(f"score {match.score:.1f}")
    detail = "; ".join(parts) if parts else "matches the saved filter"
    return f"new match for '{rule.name}': {detail}"


# --------------------------------------------------------------- dispatch


@dataclass(slots=True)
class AlertScanResult:
    rules_evaluated: int = 0
    opportunities_matched: int = 0
    alerts_created: int = 0
    alerts_suppressed: int = 0
    notifications_created: int = 0
    created: list[uuid.UUID] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        return {
            "rules_evaluated": self.rules_evaluated,
            "opportunities_matched": self.opportunities_matched,
            "alerts_created": self.alerts_created,
            "alerts_suppressed_as_duplicate": self.alerts_suppressed,
            "notifications_created": self.notifications_created,
            "method_version": METHOD_VERSION,
        }


def _body(rule: WatchRule, match: MatchRow, hit: TriggerHit) -> str:
    lines = [
        match.title,
        f"district: {match.district or 'unknown'}",
        f"type: {match.opportunity_type}",
        f"why: {hit.reason}",
    ]
    for key, value in match.evidence().items():
        if value is not None:
            lines.append(f"{key}: {value}")
    lines.append(f"rule: {rule.name} (v{rule.version})")
    return "\n".join(lines)


def dispatch(
    session: Session,
    alert: Alert,
    *,
    rule: WatchRule,
    watchlist: Watchlist,
    match: MatchRow,
    hit: TriggerHit,
    channels: dict[str, NotificationChannel],
) -> list[Notification]:
    """Send one alert on every channel the rule asked for, and record the outcome."""
    envelope = AlertEnvelope(
        alert_id=alert.id,
        recipient=watchlist.owner_ref,
        subject=f"[{hit.trigger}] {match.title}",
        body=_body(rule, match, hit),
        trigger=hit.trigger,
        reason=hit.reason,
        opportunity_id=match.opportunity_id,
    )

    created: list[Notification] = []
    for key in rule.channels or DEFAULT_CHANNELS:
        channel = channels.get(key)
        if channel is None:
            delivery = Delivery(
                channel=key,
                status=DeliveryStatus.FAILED.value,
                detail=f"no channel implementation registered for {key!r}",
            )
        else:
            delivery = channel.deliver(envelope)
        notification = Notification(
            alert_id=alert.id,
            channel=delivery.channel,
            status=delivery.status,
            detail=delivery.detail,
        )
        session.add(notification)
        created.append(notification)
    session.flush()
    return created


def _insert_alert(
    session: Session,
    *,
    rule: WatchRule,
    watchlist: Watchlist,
    match: MatchRow,
    hit: TriggerHit,
    key: str,
) -> Alert | None:
    """Insert, or return None when this exact alert already exists.

    The pre-check keeps the common path cheap; the savepoint plus the unique
    index is what makes it correct when two scans race.
    """
    if session.scalar(select(Alert.id).where(Alert.dedupe_key == key)) is not None:
        return None
    alert = Alert(
        watchlist_id=watchlist.id,
        watch_rule_id=rule.id,
        opportunity_id=match.opportunity_id,
        rule_version=rule.version,
        trigger=hit.trigger,
        reason=hit.reason,
        dedupe_key=key,
        payload={
            "rule": rule_snapshot(rule),
            "evidence": match.evidence(),
            "trigger_detail": json.loads(json.dumps(hit.detail, default=str)),
            "opportunity": {
                "id": str(match.opportunity_id),
                "title": match.title,
                "type": match.opportunity_type,
                "district": match.district,
            },
        },
    )
    try:
        with session.begin_nested():
            session.add(alert)
            session.flush()
    except IntegrityError:
        return None
    return alert


def run_alert_scan(
    session: Session,
    *,
    channels: dict[str, NotificationChannel] | None = None,
    now: datetime | None = None,
    rule_ids: list[uuid.UUID] | None = None,
) -> AlertScanResult:
    """Evaluate every enabled rule and dispatch what it newly matches."""
    now = now or datetime.now(UTC)
    channels = channels if channels is not None else default_channels()
    result = AlertScanResult()

    stmt = (
        select(WatchRule, Watchlist)
        .join(Watchlist, Watchlist.id == WatchRule.watchlist_id)
        .where(WatchRule.enabled.is_(True), Watchlist.enabled.is_(True))
        .order_by(WatchRule.created_at)
    )
    if rule_ids:
        stmt = stmt.where(WatchRule.id.in_(rule_ids))

    for rule, watchlist in session.execute(stmt).all():
        result.rules_evaluated += 1
        matches = matching_opportunities(session, rule)
        result.opportunities_matched += len(matches)

        for match in matches:
            for hit in evaluate_triggers(session, rule, match, now=now):
                key = dedupe_key(
                    rule_id=rule.id,
                    rule_version=rule.version,
                    opportunity_id=match.opportunity_id,
                    hit=hit,
                )
                alert = _insert_alert(
                    session, rule=rule, watchlist=watchlist, match=match, hit=hit, key=key
                )
                if alert is None:
                    result.alerts_suppressed += 1
                    continue
                notifications = dispatch(
                    session,
                    alert,
                    rule=rule,
                    watchlist=watchlist,
                    match=match,
                    hit=hit,
                    channels=channels,
                )
                result.alerts_created += 1
                result.notifications_created += len(notifications)
                result.created.append(alert.id)

        # Advance the watermark only after the rule has been fully evaluated,
        # so a failure mid-scan cannot swallow evidence the user never saw.
        rule.last_evaluated_at = now
    session.flush()
    return result
