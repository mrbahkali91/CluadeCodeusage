"""Watchlists, watch rules and alerts: HTTP surface and UI (Slice 3).

Registered by `sreoi_api.routers.discover()`, and it brings its own
translations via `i18n.register_strings()`, so adding this feature required no
edit to any shared file.

The HTTP layer only translates (ADR-001): validate, call the pipeline,
serialise. Every rule threshold and every alert reason is computed in
`sreoi_pipeline.alerts`, not here.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from sreoi_api.i18n import (
    direction,
    format_number,
    localise_digits,
    normalise_locale,
    register_strings,
    translator,
)
from sreoi_persistence.db import get_session_factory
from sreoi_persistence.models_alerts import (
    Alert,
    AlertFeedback,
    Notification,
    Watchlist,
    WatchRule,
)
from sreoi_pipeline.alerts import (
    DEFAULT_CHANNELS,
    DEFAULT_TRIGGERS,
    ChannelKey,
    TriggerKind,
    matching_opportunities,
    run_alert_scan,
)

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).resolve().parents[1] / "templates"))

router = APIRouter(tags=["watchlists"])

API_PREFIX = "/api/v1"

register_strings(
    "en",
    {
        "nav.watchlists": "Watchlists",
        "watch.title": "Watchlists and alerts",
        "watch.subtitle": (
            "Save what you are monitoring and be told when it appears. A rule that fires "
            "corresponds to a search that returns the same rows."
        ),
        "watch.empty": "No watchlists yet. Create one below.",
        "watch.rules": "Rules",
        "watch.alerts": "Recent alerts",
        "watch.alerts_empty": "No alerts have fired yet.",
        "watch.new": "New watchlist",
        "watch.owner": "Owner reference",
        "watch.name": "Watchlist name",
        "watch.rule_name": "Rule name",
        "watch.districts": "Districts (comma separated)",
        "watch.types": "Opportunity types (comma separated)",
        "watch.property_class": "Property class",
        "watch.max_cost": "Max true acquisition cost (SAR)",
        "watch.min_discount": "Min discount %",
        "watch.min_score": "Min score",
        "watch.min_yield": "Min gross yield %",
        "watch.create": "Create watchlist",
        "watch.scan": "Run alert scan now",
        "watch.matches": "matches now",
        "watch.version": "version",
        "watch.trigger": "Trigger",
        "watch.reason": "Reason",
        "watch.when": "When",
        "watch.channels": "Channels",
        "watch.not_sent": "logged, NOT sent",
        "watch.dedupe": (
            "The same opportunity never re-alerts on the same rule version for the same "
            "reason. Alert fatigue is a defect, not a preference."
        ),
        "watch.assumption_note": (
            "Yield is computed against the true acquisition cost, never the asking price, "
            "and is refused when a material cost item is unknown."
        ),
    },
)
register_strings(
    "ar",
    {
        "nav.watchlists": "قوائم المتابعة",
        "watch.title": "قوائم المتابعة والتنبيهات",
        "watch.subtitle": (
            "احفظ ما تراقبه ودعنا نخبرك عند ظهوره. القاعدة التي تُطلق تنبيهًا تُقابل بحثًا "
            "يعيد النتائج نفسها."
        ),
        "watch.empty": "لا توجد قوائم متابعة بعد. أنشئ واحدة أدناه.",
        "watch.rules": "القواعد",
        "watch.alerts": "أحدث التنبيهات",
        "watch.alerts_empty": "لم تُطلق أي تنبيهات بعد.",
        "watch.new": "قائمة متابعة جديدة",
        "watch.owner": "معرّف المالك",
        "watch.name": "اسم القائمة",
        "watch.rule_name": "اسم القاعدة",
        "watch.districts": "الأحياء (مفصولة بفواصل)",
        "watch.types": "أنواع الفرص (مفصولة بفواصل)",
        "watch.property_class": "نوع العقار",
        "watch.max_cost": "أقصى تكلفة استحواذ فعلية (ريال)",
        "watch.min_discount": "أدنى خصم %",
        "watch.min_score": "أدنى درجة",
        "watch.min_yield": "أدنى عائد إجمالي %",
        "watch.create": "إنشاء قائمة متابعة",
        "watch.scan": "تشغيل فحص التنبيهات الآن",
        "watch.matches": "مطابقة حاليًا",
        "watch.version": "الإصدار",
        "watch.trigger": "المُحفّز",
        "watch.reason": "السبب",
        "watch.when": "الوقت",
        "watch.channels": "القنوات",
        "watch.not_sent": "مُسجَّل ولم يُرسل",
        "watch.dedupe": (
            "لا تتكرر التنبيهات لنفس الفرصة على نفس إصدار القاعدة ولنفس السبب. إجهاد "
            "التنبيهات خلل وليس تفضيلًا."
        ),
        "watch.assumption_note": (
            "يُحسب العائد على التكلفة الفعلية للاستحواذ، وليس على السعر المطلوب، ويُرفض "
            "عند جهل أي بند تكلفة جوهري."
        ),
    },
)


def _session() -> Iterator[Session]:
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


SessionDep = Annotated[Session, Depends(_session)]


# --------------------------------------------------------------- schemas


def polygon_to_ewkt(geojson: dict[str, Any]) -> str:
    """Convert a GeoJSON Polygon to EWKT for the PostGIS geography column.

    Validated here rather than handed straight to PostGIS so a malformed
    drawing produces a 422 naming the problem, not a 500 naming a SQL function.
    """
    if geojson.get("type") != "Polygon":
        raise ValueError("polygon must be a GeoJSON geometry of type 'Polygon'")
    rings = geojson.get("coordinates")
    if not isinstance(rings, list) or not rings:
        raise ValueError("polygon has no coordinate ring")
    if len(rings) > 1:
        raise ValueError(
            "polygons with holes are not supported: a watch area with a hole would "
            "silently include opportunities the user meant to exclude"
        )
    ring = rings[0]
    if not isinstance(ring, list) or len(ring) < 4:
        raise ValueError("a polygon ring needs at least four positions")

    points: list[str] = []
    for position in ring:
        if not isinstance(position, list | tuple) or len(position) < 2:
            raise ValueError("each position must be [longitude, latitude]")
        lon, lat = float(position[0]), float(position[1])
        if not -180.0 <= lon <= 180.0 or not -90.0 <= lat <= 90.0:
            raise ValueError(f"position out of range: [{lon}, {lat}]")
        points.append(f"{lon} {lat}")
    if points[0] != points[-1]:
        points.append(points[0])
    return f"SRID=4326;POLYGON(({', '.join(points)}))"


class WatchRuleIn(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    districts: list[str] = Field(default_factory=list)
    opportunity_types: list[str] = Field(default_factory=list)
    property_class: str | None = None
    max_true_acquisition_cost: Decimal | None = None
    min_discount_pct: float | None = None
    min_score: float | None = None
    min_gross_yield: float | None = None
    polygon: dict[str, Any] | None = None
    triggers: list[str] = Field(default_factory=lambda: list(DEFAULT_TRIGGERS))
    channels: list[str] = Field(default_factory=lambda: list(DEFAULT_CHANNELS))

    @field_validator("triggers")
    @classmethod
    def _known_triggers(cls, value: list[str]) -> list[str]:
        known = {t.value for t in TriggerKind}
        unknown = sorted(set(value) - known)
        if unknown:
            raise ValueError(f"unknown triggers: {unknown}; known: {sorted(known)}")
        return value

    @field_validator("channels")
    @classmethod
    def _known_channels(cls, value: list[str]) -> list[str]:
        known = {c.value for c in ChannelKey}
        unknown = sorted(set(value) - known)
        if unknown:
            raise ValueError(f"unknown channels: {unknown}; known: {sorted(known)}")
        return value


class WatchlistIn(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    owner_ref: str = Field(min_length=1, max_length=120)
    rules: list[WatchRuleIn] = Field(default_factory=list)


class FeedbackIn(BaseModel):
    action: str
    note: str | None = None

    @field_validator("action")
    @classmethod
    def _known_action(cls, value: str) -> str:
        allowed = {"ACKNOWLEDGED", "USEFUL", "NOT_USEFUL"}
        if value not in allowed:
            raise ValueError(f"action must be one of {sorted(allowed)}")
        return value


# --------------------------------------------------------------- helpers


def _build_rule(watchlist_id: uuid.UUID, payload: WatchRuleIn) -> WatchRule:
    rule = WatchRule(
        watchlist_id=watchlist_id,
        name=payload.name,
        version=1,
        districts=payload.districts,
        opportunity_types=payload.opportunity_types,
        property_class=payload.property_class,
        max_true_acquisition_cost=payload.max_true_acquisition_cost,
        min_discount_pct=payload.min_discount_pct,
        min_score=payload.min_score,
        min_gross_yield=payload.min_gross_yield,
        triggers=payload.triggers,
        channels=payload.channels,
    )
    if payload.polygon is not None:
        try:
            rule.polygon = polygon_to_ewkt(payload.polygon)
        except ValueError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    return rule


def _rule_out(session: Session, rule: WatchRule) -> dict[str, Any]:
    return {
        "id": str(rule.id),
        "name": rule.name,
        "version": rule.version,
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
        "has_polygon": rule.polygon is not None,
        "triggers": list(rule.triggers or DEFAULT_TRIGGERS),
        "channels": list(rule.channels or DEFAULT_CHANNELS),
        "enabled": rule.enabled,
        "last_evaluated_at": rule.last_evaluated_at.isoformat() if rule.last_evaluated_at else None,
        "matches_now": len(matching_opportunities(session, rule)),
    }


def _watchlist_out(session: Session, watchlist: Watchlist) -> dict[str, Any]:
    return {
        "id": str(watchlist.id),
        "name": watchlist.name,
        "owner_ref": watchlist.owner_ref,
        "enabled": watchlist.enabled,
        "created_at": watchlist.created_at.isoformat(),
        "rules": [_rule_out(session, r) for r in watchlist.rules],
    }


def _alert_out(session: Session, alert: Alert) -> dict[str, Any]:
    notifications = session.scalars(
        select(Notification).where(Notification.alert_id == alert.id)
    ).all()
    feedback = session.scalars(
        select(AlertFeedback).where(AlertFeedback.alert_id == alert.id)
    ).all()
    return {
        "id": str(alert.id),
        "watchlist_id": str(alert.watchlist_id),
        "watch_rule_id": str(alert.watch_rule_id),
        "opportunity_id": str(alert.opportunity_id),
        "rule_version": alert.rule_version,
        "trigger": alert.trigger,
        "reason": alert.reason,
        "payload": alert.payload,
        "created_at": alert.created_at.isoformat(),
        "notifications": [
            {"channel": n.channel, "status": n.status, "detail": n.detail} for n in notifications
        ],
        "feedback": [{"action": f.action, "note": f.note} for f in feedback],
    }


def _load_watchlist(session: Session, watchlist_id: uuid.UUID) -> Watchlist:
    watchlist = session.get(Watchlist, watchlist_id)
    if watchlist is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "watchlist not found")
    return watchlist


# --------------------------------------------------------------- API


@router.post(f"{API_PREFIX}/watchlists", status_code=201)
def create_watchlist(payload: WatchlistIn, session: SessionDep) -> dict[str, Any]:
    watchlist = Watchlist(name=payload.name, owner_ref=payload.owner_ref)
    session.add(watchlist)
    session.flush()
    for rule_in in payload.rules:
        session.add(_build_rule(watchlist.id, rule_in))
    session.flush()
    session.refresh(watchlist)
    return _watchlist_out(session, watchlist)


@router.get(f"{API_PREFIX}/watchlists")
def list_watchlists(session: SessionDep, owner_ref: str | None = None) -> list[dict[str, Any]]:
    stmt = select(Watchlist).order_by(Watchlist.created_at.desc())
    if owner_ref:
        stmt = stmt.where(Watchlist.owner_ref == owner_ref)
    return [_watchlist_out(session, w) for w in session.scalars(stmt)]


@router.get(f"{API_PREFIX}/watchlists/{{watchlist_id}}")
def get_watchlist(watchlist_id: uuid.UUID, session: SessionDep) -> dict[str, Any]:
    return _watchlist_out(session, _load_watchlist(session, watchlist_id))


@router.post(f"{API_PREFIX}/watchlists/{{watchlist_id}}/rules", status_code=201)
def add_rule(watchlist_id: uuid.UUID, payload: WatchRuleIn, session: SessionDep) -> dict[str, Any]:
    watchlist = _load_watchlist(session, watchlist_id)
    rule = _build_rule(watchlist.id, payload)
    session.add(rule)
    session.flush()
    return _rule_out(session, rule)


@router.patch(f"{API_PREFIX}/watchlists/rules/{{rule_id}}")
def update_rule(rule_id: uuid.UUID, payload: WatchRuleIn, session: SessionDep) -> dict[str, Any]:
    """Editing a rule bumps its version.

    Alerts store the version they fired under, so a change of thesis is always
    distinguishable from a change in the market -- and the new version
    deliberately re-opens alerting, because the user asked a new question.
    """
    rule = session.get(WatchRule, rule_id)
    if rule is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "watch rule not found")
    replacement = _build_rule(rule.watchlist_id, payload)
    for column in (
        "name",
        "districts",
        "opportunity_types",
        "property_class",
        "max_true_acquisition_cost",
        "min_discount_pct",
        "min_score",
        "min_gross_yield",
        "polygon",
        "triggers",
        "channels",
    ):
        setattr(rule, column, getattr(replacement, column))
    rule.version += 1
    rule.updated_at = datetime.now(UTC)
    session.flush()
    return _rule_out(session, rule)


@router.get(f"{API_PREFIX}/watchlists/{{watchlist_id}}/matches")
def rule_matches(watchlist_id: uuid.UUID, session: SessionDep) -> dict[str, Any]:
    """What each rule matches right now, so a user can sanity-check it before saving."""
    watchlist = _load_watchlist(session, watchlist_id)
    return {
        "watchlist_id": str(watchlist.id),
        "rules": [
            {
                "rule_id": str(rule.id),
                "rule_name": rule.name,
                "matches": [
                    {
                        "opportunity_id": str(m.opportunity_id),
                        "title": m.title,
                        "district": m.district,
                        **m.evidence(),
                    }
                    for m in matching_opportunities(session, rule)
                ],
            }
            for rule in watchlist.rules
        ],
    }


@router.post(f"{API_PREFIX}/alerts/scan", status_code=202)
def scan(session: SessionDep) -> dict[str, Any]:
    """Evaluate every enabled rule and dispatch what it newly matches."""
    result = run_alert_scan(session)
    return result.summary()


@router.get(f"{API_PREFIX}/alerts")
def list_alerts(
    session: SessionDep,
    watchlist_id: uuid.UUID | None = None,
    trigger: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    stmt = select(Alert).order_by(Alert.created_at.desc()).limit(max(1, min(limit, 500)))
    if watchlist_id is not None:
        stmt = stmt.where(Alert.watchlist_id == watchlist_id)
    if trigger:
        stmt = stmt.where(Alert.trigger == trigger)
    alerts = session.scalars(stmt).all()
    total = session.scalar(select(func.count()).select_from(Alert)) or 0
    return {
        "total": int(total),
        "count": len(alerts),
        "alerts": [_alert_out(session, a) for a in alerts],
    }


@router.post(f"{API_PREFIX}/alerts/{{alert_id}}/feedback", status_code=201)
def alert_feedback(alert_id: uuid.UUID, payload: FeedbackIn, session: SessionDep) -> dict[str, Any]:
    """Acknowledgement and usefulness, recorded append-only.

    Kept off the alert row: the alert is immutable evidence of an
    interruption, and `alert_precision` is derived from these rows.
    """
    alert = session.get(Alert, alert_id)
    if alert is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "alert not found")
    row = AlertFeedback(alert_id=alert.id, action=payload.action, note=payload.note)
    session.add(row)
    session.flush()
    return {"id": str(row.id), "alert_id": str(alert.id), "action": row.action}


# --------------------------------------------------------------- UI


def _ui_context(request: Request) -> dict[str, Any]:
    """Local copy of the shared UI context.

    Duplicated rather than imported from `sreoi_api.main`, which imports this
    module through router discovery; importing it back would be a cycle.
    """
    locale = normalise_locale(request.query_params.get("lang"))
    return {
        "locale": locale,
        "dir": direction(locale),
        "t": translator(locale),
        "num": lambda v, d=0: format_number(v, locale, d),
        "digits": lambda s: localise_digits(str(s), locale),
        "other_locale": "en" if locale == "ar" else "ar",
        "page": "watchlists",
        "query": request.query_params,
    }


@router.get("/watchlists", response_class=HTMLResponse)
def ui_watchlists(request: Request, session: SessionDep) -> HTMLResponse:
    watchlists = session.scalars(select(Watchlist).order_by(Watchlist.created_at.desc())).all()
    alerts = session.scalars(select(Alert).order_by(Alert.created_at.desc()).limit(50)).all()
    return TEMPLATES.TemplateResponse(
        request=request,
        name="watchlists.html",
        context={
            **_ui_context(request),
            "watchlists": [_watchlist_out(session, w) for w in watchlists],
            "alerts": [_alert_out(session, a) for a in alerts],
            "triggers": [t.value for t in TriggerKind],
        },
    )
