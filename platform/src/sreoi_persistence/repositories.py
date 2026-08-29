"""Repositories. All geospatial work is done by PostGIS (ADR-002)."""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from decimal import Decimal

from geoalchemy2 import Geography, Geometry
from sqlalchemy import cast, func, select
from sqlalchemy.orm import Session

from sreoi_domain.valuation import MIN_COMPARABLES, Comparable
from sreoi_persistence.models import District, PriceIndexPoint, Property, Source, Transaction

# Expanding search: stop at the first radius that yields enough evidence.
RADIUS_STEPS_M = (750.0, 1500.0, 3000.0, 6000.0)
LOOKBACK_MONTHS = 24
AREA_LOWER_FACTOR = 0.65
AREA_UPPER_FACTOR = 1.50


class ComparableRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def find(
        self,
        *,
        longitude: float,
        latitude: float,
        property_class: str,
        area_sqm: float,
        as_of: date,
        min_comparables: int = MIN_COMPARABLES,
    ) -> tuple[list[Comparable], float]:
        """Expanding-radius comparable search.

        Returns the comparables and the radius that produced them, so the
        caller can penalise confidence when the search had to reach beyond the
        immediate neighbourhood -- a comparable from further away is a weaker
        claim and the output must say so.
        """
        subject = cast(func.ST_SetSRID(func.ST_MakePoint(longitude, latitude), 4326), Geography)
        cutoff = as_of - timedelta(days=int(LOOKBACK_MONTHS * 30.44))

        found: list[Comparable] = []
        used_radius = RADIUS_STEPS_M[-1]

        for radius in RADIUS_STEPS_M:
            stmt = (
                select(
                    Transaction,
                    func.ST_Distance(Transaction.location, subject).label("distance_m"),
                )
                .where(
                    Transaction.property_class == property_class,
                    Transaction.transacted_on >= cutoff,
                    Transaction.transacted_on <= as_of,
                    Transaction.area_sqm >= area_sqm * AREA_LOWER_FACTOR,
                    Transaction.area_sqm <= area_sqm * AREA_UPPER_FACTOR,
                    func.ST_DWithin(Transaction.location, subject, radius),
                )
                .order_by("distance_m")
                .limit(200)
            )
            rows = self._session.execute(stmt).all()
            if len(rows) >= min_comparables or radius == RADIUS_STEPS_M[-1]:
                found = [
                    Comparable(
                        id=txn.id,
                        price=Decimal(txn.price),
                        area_sqm=float(txn.area_sqm),
                        transacted_on=txn.transacted_on,
                        distance_m=float(distance),
                        property_class=txn.property_class,
                        district_id=txn.district_id,
                        project_id=txn.project_id,
                        build_year=txn.build_year,
                        floor=txn.floor,
                    )
                    for txn, distance in rows
                ]
                used_radius = radius
                break

        return found, used_radius


class PropertyRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def coordinates(self, property_id: uuid.UUID) -> tuple[float, float]:
        """Read (longitude, latitude) back out of the geography column."""
        geom = cast(Property.location, Geometry)
        row = self._session.execute(
            select(func.ST_X(geom), func.ST_Y(geom)).where(Property.id == property_id)
        ).one_or_none()
        if row is None:
            raise LookupError(f"property {property_id} not found")
        return float(row[0]), float(row[1])


class DistrictRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def by_name(self, name_en: str) -> District | None:
        return self._session.scalar(select(District).where(District.name_en == name_en))

    def containing(self, longitude: float, latitude: float) -> District | None:
        point = func.ST_SetSRID(func.ST_MakePoint(longitude, latitude), 4326)
        return self._session.scalar(
            select(District).where(
                District.boundary.isnot(None),
                func.ST_Covers(cast(District.boundary, Geometry), point),
            )
        )

    def centroid_of(self, district_id: uuid.UUID) -> tuple[float, float]:
        geom = cast(District.centroid, Geometry)
        row = self._session.execute(
            select(func.ST_X(geom), func.ST_Y(geom)).where(District.id == district_id)
        ).one()
        return float(row[0]), float(row[1])

    def all(self) -> list[District]:
        return list(self._session.scalars(select(District).order_by(District.name_en)))


class PriceIndexRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def series(self, sector: str, tier: str = "NATIONAL") -> dict[str, float]:
        rows = self._session.scalars(
            select(PriceIndexPoint).where(
                PriceIndexPoint.sector == sector, PriceIndexPoint.tier == tier
            )
        )
        return {row.period: float(row.value) for row in rows}

    def latest(self, sector: str, tier: str = "NATIONAL") -> tuple[str, float] | None:
        row = self._session.scalar(
            select(PriceIndexPoint)
            .where(PriceIndexPoint.sector == sector, PriceIndexPoint.tier == tier)
            .order_by(PriceIndexPoint.period.desc())
            .limit(1)
        )
        return (row.period, float(row.value)) if row else None


class SourceRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def by_key(self, key: str) -> Source | None:
        return self._session.scalar(select(Source).where(Source.key == key))

    def require(self, key: str) -> Source:
        source = self.by_key(key)
        if source is None:
            raise LookupError(f"source '{key}' is not registered")
        if not source.enabled:
            raise PermissionError(f"source '{key}' is disabled")
        return source

    def all(self) -> list[Source]:
        return list(self._session.scalars(select(Source).order_by(Source.key)))


def as_uuid(value: str | uuid.UUID) -> uuid.UUID:
    return value if isinstance(value, uuid.UUID) else uuid.UUID(value)
