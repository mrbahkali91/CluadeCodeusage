"""Verification checkers.

The hard constraint: **VERIFIED is never asserted without stored evidence**, and
a check we cannot actually perform reports UNAVAILABLE rather than passing. The
database enforces the first half with a CHECK constraint, because an agent's
output is not a trustworthy place to put a safety property.

Checks are split into two classes and they are *not* interchangeable:

  INTERNAL   -- coherence we can establish from our own data: does the geometry
                agree with the claimed district, is the price per m² inside the
                district's own distribution, do two independent listings for the
                same unit agree. This catches fabricated and incoherent data.

  OFFICIAL   -- confirmation against a government register (REGA advertisement
                licence, Wafi off-plan project, developer registry). These are
                declared here but report UNAVAILABLE: the endpoints are
                REQUIRES VALIDATION in the source matrix and unreachable from
                this environment. Reporting them as passed would be the exact
                failure this product exists to prevent.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType
from typing import Any
from uuid import UUID

from geoalchemy2 import Geometry
from sqlalchemy import cast, func, select
from sqlalchemy.orm import Session

from sreoi_persistence.models import (
    District,
    Listing,
    ListingSnapshot,
    Property,
    Transaction,
    Valuation,
)

METHOD_VERSION = "verification-v1"


class CheckClass(StrEnum):
    INTERNAL = "INTERNAL"
    OFFICIAL = "OFFICIAL"


class CheckStatus(StrEnum):
    VERIFIED = "VERIFIED"
    FAILED = "FAILED"
    CONFLICTED = "CONFLICTED"
    UNAVAILABLE = "UNAVAILABLE"
    NOT_APPLICABLE = "NOT_APPLICABLE"

    @property
    def counts_toward_score(self) -> bool:
        return self in {CheckStatus.VERIFIED, CheckStatus.FAILED, CheckStatus.CONFLICTED}


@dataclass(frozen=True, slots=True)
class CheckOutcome:
    check_type: str
    check_class: CheckClass
    status: CheckStatus
    summary: str
    evidence: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.status is CheckStatus.VERIFIED and not self.evidence:
            raise ValueError(
                f"{self.check_type}: VERIFIED requires evidence -- refusing to assert "
                "verification without it"
            )


class Checker(abc.ABC):
    check_type: str
    check_class: CheckClass
    # Recorded for the same reason connectors record one: no check runs against
    # an external system without a stated legal basis.
    legal_basis: str = "internal data only"

    @abc.abstractmethod
    def run(self, session: Session, property_id: UUID, opportunity_id: UUID) -> CheckOutcome: ...


# --------------------------------------------------------------------- internal


class DistrictGeometryChecker(Checker):
    """Do the coordinates actually fall inside the district that was claimed?"""

    check_type = "district_geometry"
    check_class = CheckClass.INTERNAL

    def run(self, session: Session, property_id: UUID, opportunity_id: UUID) -> CheckOutcome:
        prop = session.get(Property, property_id)
        if prop is None or prop.district_id is None:
            return CheckOutcome(
                self.check_type,
                self.check_class,
                CheckStatus.NOT_APPLICABLE,
                "no district claimed",
            )
        district = session.get(District, prop.district_id)
        if district is None or district.boundary is None:
            return CheckOutcome(
                self.check_type,
                self.check_class,
                CheckStatus.UNAVAILABLE,
                "district boundary not loaded",
            )
        inside = session.scalar(
            select(
                func.ST_Covers(cast(District.boundary, Geometry), cast(Property.location, Geometry))
            )
            .select_from(Property)
            .join(District, District.id == Property.district_id)
            .where(Property.id == property_id)
        )
        if inside:
            return CheckOutcome(
                self.check_type,
                self.check_class,
                CheckStatus.VERIFIED,
                f"coordinates fall inside {district.name_en}",
                {
                    "district": district.name_en,
                    "boundary_precision": district.boundary_precision,
                    "method": "PostGIS ST_Covers",
                },
            )
        return CheckOutcome(
            self.check_type,
            self.check_class,
            CheckStatus.FAILED,
            f"coordinates fall outside the claimed district {district.name_en}",
            {"district": district.name_en},
        )


class PricePlausibilityChecker(Checker):
    """Is the implied price per m² inside the district's own transaction range?"""

    check_type = "price_plausibility"
    check_class = CheckClass.INTERNAL

    def run(self, session: Session, property_id: UUID, opportunity_id: UUID) -> CheckOutcome:
        prop = session.get(Property, property_id)
        valuation = session.scalar(
            select(Valuation)
            .where(Valuation.opportunity_id == opportunity_id)
            .order_by(Valuation.computed_at.desc())
            .limit(1)
        )
        if prop is None or valuation is None or prop.district_id is None:
            return CheckOutcome(
                self.check_type,
                self.check_class,
                CheckStatus.NOT_APPLICABLE,
                "no valuation or district to compare against",
            )

        bounds = session.execute(
            select(
                func.percentile_cont(0.05).within_group(
                    (Transaction.price / Transaction.area_sqm).asc()
                ),
                func.percentile_cont(0.95).within_group(
                    (Transaction.price / Transaction.area_sqm).asc()
                ),
                func.count(),
            ).where(Transaction.district_id == prop.district_id)
        ).one()
        low, high, count = bounds
        if not count or low is None or high is None:
            return CheckOutcome(
                self.check_type,
                self.check_class,
                CheckStatus.UNAVAILABLE,
                "no district transactions to compare against",
            )

        ppsqm = float(valuation.base_price_per_sqm)
        evidence = {
            "valuation_price_per_sqm": round(ppsqm, 2),
            "district_p05": round(float(low), 2),
            "district_p95": round(float(high), 2),
            "transaction_count": int(count),
        }
        if float(low) <= ppsqm <= float(high):
            return CheckOutcome(
                self.check_type,
                self.check_class,
                CheckStatus.VERIFIED,
                f"SAR {ppsqm:,.0f}/m² sits inside the district 5th–95th percentile",
                evidence,
            )
        return CheckOutcome(
            self.check_type,
            self.check_class,
            CheckStatus.FAILED,
            f"SAR {ppsqm:,.0f}/m² is outside the district 5th–95th percentile",
            evidence,
        )


class CrossSourceAgreementChecker(Checker):
    """When two independent listings describe one unit, do they agree on price?"""

    check_type = "cross_source_agreement"
    check_class = CheckClass.INTERNAL

    TOLERANCE = 0.15

    def run(self, session: Session, property_id: UUID, opportunity_id: UUID) -> CheckOutcome:
        rows = session.execute(
            select(Listing.id, Listing.external_id, ListingSnapshot.asking_price)
            .join(ListingSnapshot, ListingSnapshot.listing_id == Listing.id)
            .where(Listing.property_id == property_id)
            .order_by(ListingSnapshot.observed_at.desc())
        ).all()

        latest_per_listing: dict[UUID, tuple[str, Decimal]] = {}
        for listing_id, external_id, price in rows:
            if price is None or listing_id in latest_per_listing:
                continue
            latest_per_listing[listing_id] = (external_id, Decimal(price))

        if len(latest_per_listing) < 2:
            return CheckOutcome(
                self.check_type,
                self.check_class,
                CheckStatus.NOT_APPLICABLE,
                "only one source has listed this property",
            )

        prices = [float(p) for _, p in latest_per_listing.values()]
        spread = (max(prices) - min(prices)) / max(prices)
        evidence = {
            "listings": [
                {"external_id": ext, "asking_price": float(p)}
                for ext, p in latest_per_listing.values()
            ],
            "spread": round(spread, 4),
            "tolerance": self.TOLERANCE,
        }
        if spread <= self.TOLERANCE:
            return CheckOutcome(
                self.check_type,
                self.check_class,
                CheckStatus.VERIFIED,
                f"{len(prices)} independent listings agree within {spread:.1%}",
                evidence,
            )
        return CheckOutcome(
            self.check_type,
            self.check_class,
            CheckStatus.CONFLICTED,
            f"independent listings disagree by {spread:.1%}",
            evidence,
        )


class AreaCoherenceChecker(Checker):
    """Does the stated area make sense against the stated bedroom count?"""

    check_type = "area_coherence"
    check_class = CheckClass.INTERNAL

    # Generous Riyadh apartment ranges; the purpose is catching nonsense, not
    # second-guessing an architect.
    BOUNDS = MappingProxyType(
        {1: (35, 130), 2: (60, 190), 3: (90, 260), 4: (110, 360), 5: (140, 500)}
    )

    def run(self, session: Session, property_id: UUID, opportunity_id: UUID) -> CheckOutcome:
        prop = session.get(Property, property_id)
        if prop is None or prop.bedrooms is None:
            return CheckOutcome(
                self.check_type,
                self.check_class,
                CheckStatus.NOT_APPLICABLE,
                "bedroom count not stated",
            )
        bounds = self.BOUNDS.get(min(prop.bedrooms, 5))
        if bounds is None:
            return CheckOutcome(
                self.check_type,
                self.check_class,
                CheckStatus.NOT_APPLICABLE,
                "no range defined for this bedroom count",
            )
        area = float(prop.built_area_sqm)
        evidence = {"bedrooms": prop.bedrooms, "area_sqm": area, "expected_range": list(bounds)}
        if bounds[0] <= area <= bounds[1]:
            return CheckOutcome(
                self.check_type,
                self.check_class,
                CheckStatus.VERIFIED,
                f"{area:.0f} m² is plausible for {prop.bedrooms} bedrooms",
                evidence,
            )
        return CheckOutcome(
            self.check_type,
            self.check_class,
            CheckStatus.FAILED,
            f"{area:.0f} m² is implausible for {prop.bedrooms} bedrooms",
            evidence,
        )


# --------------------------------------------------------------------- official


class _UnavailableOfficialChecker(Checker):
    """An official check we are not yet able to perform.

    It reports UNAVAILABLE with the reason. It must never report VERIFIED: an
    unperformed check is not a passed check, and treating it as one would put a
    false trust badge on the product.
    """

    check_class = CheckClass.OFFICIAL
    reason: str

    def run(self, session: Session, property_id: UUID, opportunity_id: UUID) -> CheckOutcome:
        return CheckOutcome(self.check_type, self.check_class, CheckStatus.UNAVAILABLE, self.reason)


class AdvertisementLicenceChecker(_UnavailableOfficialChecker):
    check_type = "rega_advertisement_licence"
    legal_basis = "REGA advertisement licence inquiry — per-property, on demand, never bulk"
    reason = (
        "REGA advertisement-licence inquiry is REQUIRES VALIDATION in the source "
        "matrix and has no documented machine interface. Not performed."
    )


class WafiProjectChecker(_UnavailableOfficialChecker):
    check_type = "wafi_project_licence"
    legal_basis = "Wafi off-plan project verification — per-project, cached"
    reason = (
        "Wafi project verification has no documented machine interface. Required "
        "before any off-plan assignment can be called verified. Not performed."
    )


class DeveloperRegistryChecker(_UnavailableOfficialChecker):
    check_type = "developer_registry"
    legal_basis = "REGA developer registry lookup"
    reason = "Developer registry lookup is not yet integrated. Not performed."


def default_checkers() -> list[Checker]:
    return [
        DistrictGeometryChecker(),
        PricePlausibilityChecker(),
        CrossSourceAgreementChecker(),
        AreaCoherenceChecker(),
        AdvertisementLicenceChecker(),
        WafiProjectChecker(),
        DeveloperRegistryChecker(),
    ]
