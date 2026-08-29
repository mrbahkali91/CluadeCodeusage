"""Filtered opportunity search and map queries.

All filtering, geospatial work and ranking happens in PostgreSQL. Loading rows
into Python to filter them would neither scale nor support the map.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from geoalchemy2 import Geography, Geometry
from sqlalchemy import Float, Select, cast, func, select
from sqlalchemy.orm import Session

from sreoi_persistence.models import (
    District,
    Opportunity,
    OpportunityScoreRow,
    Property,
    TrueAcquisitionCostRow,
    Valuation,
)

SORTS = {
    "score": "highest opportunity score",
    "discount": "largest discount",
    "cost": "lowest acquisition cost",
    "newest": "most recently added",
}


@dataclass(slots=True)
class OpportunityFilters:
    districts: list[str] = field(default_factory=list)
    opportunity_types: list[str] = field(default_factory=list)
    property_class: str | None = None
    max_cost: Decimal | None = None
    min_discount_pct: float | None = None
    min_score: float | None = None
    include_insufficient: bool = True
    bbox: tuple[float, float, float, float] | None = None  # west,south,east,north
    centre: tuple[float, float] | None = None
    radius_m: float | None = None
    sort: str = "score"
    limit: int = 100

    def describe(self) -> dict[str, Any]:
        """What the user actually asked for, echoed back so filters are visible."""
        out: dict[str, Any] = {}
        if self.districts:
            out["districts"] = self.districts
        if self.opportunity_types:
            out["opportunity_types"] = self.opportunity_types
        if self.property_class:
            out["property_class"] = self.property_class
        if self.max_cost is not None:
            out["max_true_acquisition_cost"] = float(self.max_cost)
        if self.min_discount_pct is not None:
            out["min_discount_pct"] = self.min_discount_pct
        if self.min_score is not None:
            out["min_opportunity_score"] = self.min_score
        if not self.include_insufficient:
            out["exclude_insufficient_data"] = True
        if self.bbox:
            out["bbox"] = list(self.bbox)
        if self.centre and self.radius_m:
            out["within_metres_of"] = {
                "lon": self.centre[0],
                "lat": self.centre[1],
                "radius_m": self.radius_m,
            }
        out["sort"] = self.sort
        return out


def _latest(model: Any, order_col: Any) -> Any:
    """Subquery selecting the most recent row per opportunity."""
    return select(
        model,
        func.row_number()
        .over(partition_by=model.opportunity_id, order_by=order_col.desc())
        .label("rn"),
    ).subquery()


def _base_query() -> tuple[Select[Any], Any, Any, Any]:
    valuation = _latest(Valuation, Valuation.computed_at)
    cost = _latest(TrueAcquisitionCostRow, TrueAcquisitionCostRow.computed_at)
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
            valuation.c.base_price_per_sqm,
            valuation.c.confidence,
            cost.c.total,
            cost.c.is_complete,
            score.c.total_score,
            score.c.classification,
            score.c.data_confidence,
            score.c.discount_fraction,
            score.c.discount_refused_reason,
            func.ST_X(geom).cast(Float).label("lon"),
            func.ST_Y(geom).cast(Float).label("lat"),
        )
        .join(Property, Property.id == Opportunity.property_id)
        .outerjoin(District, District.id == Property.district_id)
        .outerjoin(
            valuation,
            (valuation.c.opportunity_id == Opportunity.id) & (valuation.c.rn == 1),
        )
        .outerjoin(cost, (cost.c.opportunity_id == Opportunity.id) & (cost.c.rn == 1))
        .outerjoin(score, (score.c.opportunity_id == Opportunity.id) & (score.c.rn == 1))
        .where(Opportunity.status == "ACTIVE")
    )
    return stmt, valuation, cost, score


def _apply(stmt: Select[Any], cost: Any, score: Any, f: OpportunityFilters) -> Select[Any]:
    if f.districts:
        stmt = stmt.where(District.name_en.in_(f.districts))
    if f.opportunity_types:
        stmt = stmt.where(Opportunity.opportunity_type.in_(f.opportunity_types))
    if f.property_class:
        stmt = stmt.where(Property.property_class == f.property_class)
    if f.max_cost is not None:
        # Only complete cost figures may be compared against a budget.
        stmt = stmt.where(cost.c.is_complete.is_(True), cost.c.total <= f.max_cost)
    if f.min_discount_pct is not None:
        stmt = stmt.where(score.c.discount_fraction >= f.min_discount_pct / 100.0)
    if f.min_score is not None:
        stmt = stmt.where(score.c.total_score >= f.min_score)
    if not f.include_insufficient:
        stmt = stmt.where(score.c.classification != "INSUFFICIENT_DATA")

    if f.bbox:
        west, south, east, north = f.bbox
        envelope = cast(func.ST_MakeEnvelope(west, south, east, north, 4326), Geography)
        stmt = stmt.where(func.ST_Intersects(Property.location, envelope))
    if f.centre and f.radius_m:
        point = cast(func.ST_SetSRID(func.ST_MakePoint(f.centre[0], f.centre[1]), 4326), Geography)
        stmt = stmt.where(func.ST_DWithin(Property.location, point, f.radius_m))

    order = {
        "score": score.c.total_score.desc().nullslast(),
        "discount": score.c.discount_fraction.desc().nullslast(),
        "cost": cost.c.total.asc().nullslast(),
        "newest": Opportunity.created_at.desc(),
    }[f.sort if f.sort in SORTS else "score"]
    return stmt.order_by(order).limit(f.limit)


def search(session: Session, filters: OpportunityFilters) -> list[dict[str, Any]]:
    stmt, _valuation, cost, score = _base_query()
    rows = session.execute(_apply(stmt, cost, score, filters)).all()

    out: list[dict[str, Any]] = []
    for r in rows:
        out.append(
            {
                "id": r.Opportunity.id,
                "title": r.Opportunity.title,
                "opportunity_type": r.Opportunity.opportunity_type,
                "district": r.District.name_en if r.District else None,
                "district_ar": r.District.name_ar if r.District else None,
                "property_class": r.Property.property_class,
                "area_sqm": float(r.Property.built_area_sqm),
                "location_precision": r.Property.location_precision,
                "longitude": r.lon,
                "latitude": r.lat,
                "true_acquisition_cost": Decimal(r.total) if r.is_complete and r.total else None,
                "fair_value_base": Decimal(r.fair_value_base) if r.fair_value_base else None,
                "price_per_sqm": float(r.base_price_per_sqm) if r.base_price_per_sqm else None,
                "discount_percent": float(r.discount_fraction) * 100
                if r.discount_fraction is not None
                else None,
                "discount_refused_reason": r.discount_refused_reason,
                "score": float(r.total_score) if r.total_score is not None else None,
                "classification": r.classification,
                "data_confidence": float(r.data_confidence)
                if r.data_confidence is not None
                else None,
            }
        )
    return out


def geojson(session: Session, filters: OpportunityFilters) -> dict[str, Any]:
    """Map layer. Same filters as the list, so the two views never disagree."""
    features = []
    for row in search(session, filters):
        if row["longitude"] is None or row["latitude"] is None:
            continue
        features.append(
            {
                "type": "Feature",
                "geometry": {
                    "type": "Point",
                    "coordinates": [row["longitude"], row["latitude"]],
                },
                "properties": {
                    "id": str(row["id"]),
                    "title": row["title"],
                    "score": row["score"],
                    "classification": row["classification"],
                    "district": row["district"],
                    "discount_percent": row["discount_percent"],
                    "true_acquisition_cost": float(row["true_acquisition_cost"])
                    if row["true_acquisition_cost"]
                    else None,
                    "price_per_sqm": row["price_per_sqm"],
                    "location_precision": row["location_precision"],
                },
            }
        )
    return {"type": "FeatureCollection", "features": features}


def district_layer(session: Session) -> dict[str, Any]:
    """District polygons with market metrics, for the price/m² shading."""
    from sreoi_persistence.models import Transaction

    metrics = (
        select(
            Transaction.district_id.label("district_id"),
            func.count().label("transaction_count"),
            func.percentile_cont(0.5)
            .within_group((Transaction.price / Transaction.area_sqm).asc())
            .label("median_ppsqm"),
        )
        .group_by(Transaction.district_id)
        .subquery()
    )

    centroid = cast(District.centroid, Geometry)
    rows = session.execute(
        select(
            District,
            func.ST_AsGeoJSON(cast(District.boundary, Geometry)).label("geom"),
            metrics.c.transaction_count,
            metrics.c.median_ppsqm,
            func.ST_X(centroid).label("clon"),
            func.ST_Y(centroid).label("clat"),
        )
        .outerjoin(metrics, metrics.c.district_id == District.id)
        .where(District.boundary.isnot(None))
    ).all()

    import json

    features = []
    for district, geom, count, median, clon, clat in rows:
        features.append(
            {
                "type": "Feature",
                "geometry": json.loads(geom) if geom else None,
                "properties": {
                    "id": str(district.id),
                    "name_en": district.name_en,
                    "name_ar": district.name_ar,
                    "centroid": [float(clon), float(clat)],
                    "median_price_per_sqm": float(median) if median is not None else None,
                    "transaction_count": int(count or 0),
                    "liquidity_score": float(district.liquidity_score),
                    "location_score": float(district.location_score),
                },
            }
        )
    return {"type": "FeatureCollection", "features": features}


def district_metrics(session: Session, district_id: uuid.UUID) -> dict[str, Any] | None:
    from sreoi_persistence.models import Transaction

    district = session.get(District, district_id)
    if district is None:
        return None
    row = session.execute(
        select(
            func.count(),
            func.percentile_cont(0.5)
            .within_group((Transaction.price / Transaction.area_sqm).asc())
            .label("median"),
            func.percentile_cont(0.25)
            .within_group((Transaction.price / Transaction.area_sqm).asc())
            .label("q1"),
            func.percentile_cont(0.75)
            .within_group((Transaction.price / Transaction.area_sqm).asc())
            .label("q3"),
        ).where(Transaction.district_id == district_id)
    ).one()
    return {
        "id": str(district.id),
        "name_en": district.name_en,
        "name_ar": district.name_ar,
        "transaction_count": int(row[0] or 0),
        "median_price_per_sqm": float(row[1]) if row[1] is not None else None,
        "q1_price_per_sqm": float(row[2]) if row[2] is not None else None,
        "q3_price_per_sqm": float(row[3]) if row[3] is not None else None,
        "liquidity_score": float(district.liquidity_score),
        "location_score": float(district.location_score),
    }
