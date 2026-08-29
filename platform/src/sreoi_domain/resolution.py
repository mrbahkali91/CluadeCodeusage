"""Entity resolution: is this the same physical unit?

The same apartment appears on an auction platform, a broker's list and a
developer's inventory under three different descriptions. Presenting it three
times destroys the product's credibility as surely as a wrong valuation, and
merging two genuinely different units is worse still -- it corrupts the
comparable evidence.

So the decision is deliberately conservative and three-way: merge when the
evidence is strong, ask a human in the ambiguous band, and keep separate
otherwise. Merges are reversible.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

METHOD_VERSION = "resolution-v1"

AUTO_MERGE_THRESHOLD = 0.85
REVIEW_THRESHOLD = 0.60

# Blocking: candidates outside these bounds are never even scored.
BLOCK_AREA_TOLERANCE = 0.08
BLOCK_RADIUS_M = 500.0


class ResolutionDecision(StrEnum):
    AUTO_MERGE = "AUTO_MERGE"
    REVIEW = "REVIEW"
    DISTINCT = "DISTINCT"

    @property
    def label(self) -> str:
        return {
            "AUTO_MERGE": "Merged automatically",
            "REVIEW": "Queued for human review",
            "DISTINCT": "Treated as a separate property",
        }[self.value]


@dataclass(frozen=True, slots=True)
class ResolutionFeatures:
    """Everything known about one side of a candidate pair."""

    area_sqm: float
    property_class: str
    district_id: UUID | None = None
    project_id: UUID | None = None
    unit_number: str | None = None
    bedrooms: int | None = None
    floor: int | None = None
    build_year: int | None = None
    price: Decimal | None = None


@dataclass(frozen=True, slots=True)
class MatchComponent:
    name: str
    score: float
    weight: float
    detail: str

    @property
    def contribution(self) -> float:
        return self.score * self.weight


@dataclass(frozen=True, slots=True)
class MatchResult:
    total: float
    decision: ResolutionDecision
    components: tuple[MatchComponent, ...]
    method_version: str = METHOD_VERSION
    conflict: str | None = None

    @property
    def explanation(self) -> str:
        if self.conflict:
            return self.conflict
        top = sorted(self.components, key=lambda c: -c.contribution)[:3]
        return "; ".join(f"{c.name} {c.score:.2f}" for c in top)


def _spatial_score(distance_m: float | None) -> tuple[float, str]:
    if distance_m is None:
        return 0.3, "distance unknown"
    # Two units in the same building are metres apart; 50 m is the practical
    # limit of "same address" given district-precision geocoding.
    score = math.exp(-((distance_m / 50.0) ** 2))
    return score, f"{distance_m:.0f} m apart"


def _area_score(a: float, b: float) -> tuple[float, str]:
    if a <= 0 or b <= 0:
        return 0.0, "area missing"
    deviation = abs(a - b) / max(a, b)
    score = math.exp(-((deviation / 0.05) ** 2))
    return score, f"{a:.0f} vs {b:.0f} m² ({deviation:.1%})"


def _normalise_unit(unit: str | None) -> str | None:
    """B-402, b 402 and B402 are the same unit written three ways."""
    if not unit:
        return None
    return "".join(ch for ch in unit.lower() if ch.isalnum()) or None


def _units_compatible(left: str, right: str) -> bool:
    """Whether two unit labels can plausibly denote the same door.

    "402" and "b402" differ only in whether the block letter was recorded, so
    they are compatible. "b402" and "b905" are two different doors.
    """
    return left == right or left.endswith(right) or right.endswith(left)


def _project_unit_score(left: ResolutionFeatures, right: ResolutionFeatures) -> tuple[float, str]:
    """Project and unit identity.

    Unit number is treated as evidence in its own right, not only when a
    project id is also present: until the project registry exists, the unit
    number plus the spatial block is often the strongest signal we have that
    two records describe the same door.
    """
    same_project = left.project_id is not None and left.project_id == right.project_id
    different_project = (
        left.project_id is not None
        and right.project_id is not None
        and left.project_id != right.project_id
    )

    left_unit = _normalise_unit(left.unit_number)
    right_unit = _normalise_unit(right.unit_number)
    units_known = left_unit is not None and right_unit is not None
    same_unit = units_known and left_unit == right_unit

    if different_project:
        return 0.0, "different projects"
    compatible = units_known and _units_compatible(left_unit or "", right_unit or "")
    if same_project and same_unit:
        return 1.0, "same project and unit number"
    if same_unit:
        return 0.90, f"same unit number ({left.unit_number})"
    if compatible:
        return 0.80, f"compatible unit numbers ({left.unit_number} / {right.unit_number})"
    if units_known:
        # Both sides name a unit and they disagree: evidence against a match.
        return 0.05, f"different unit numbers ({left.unit_number} vs {right.unit_number})"
    if same_project:
        return 0.75, "same project, unit number unknown"
    return 0.4, "project and unit unknown on at least one side"


def _attribute_score(left: ResolutionFeatures, right: ResolutionFeatures) -> tuple[float, str]:
    """Agreement across bedrooms, floor and build year.

    Only compares attributes present on both sides; an unknown attribute is
    neither evidence for nor against a match.
    """
    comparisons: list[float] = []
    notes: list[str] = []

    if left.bedrooms is not None and right.bedrooms is not None:
        agree = left.bedrooms == right.bedrooms
        comparisons.append(1.0 if agree else 0.0)
        notes.append(f"bedrooms {'match' if agree else 'differ'}")

    if left.floor is not None and right.floor is not None:
        agree = left.floor == right.floor
        comparisons.append(1.0 if agree else 0.0)
        notes.append(f"floor {'match' if agree else 'differ'}")

    if left.build_year is not None and right.build_year is not None:
        delta = abs(left.build_year - right.build_year)
        comparisons.append(1.0 if delta == 0 else (0.5 if delta <= 1 else 0.0))
        notes.append(f"build year delta {delta}")

    if not comparisons:
        return 0.5, "no shared attributes to compare"
    return sum(comparisons) / len(comparisons), ", ".join(notes)


def _price_score(left: Decimal | None, right: Decimal | None) -> tuple[float, str]:
    if left is None or right is None or left <= 0 or right <= 0:
        return 0.5, "price unknown on at least one side"
    a, b = float(left), float(right)
    deviation = abs(a - b) / max(a, b)
    # Prices legitimately differ between an auction opening bid and a broker
    # asking price for the same unit, so this is weak evidence by design.
    score = math.exp(-((deviation / 0.25) ** 2))
    return score, f"{deviation:.1%} apart"


def _text_score(similarity: float | None) -> tuple[float, str]:
    if similarity is None:
        # Neutral, not zero and not redistributed: absent evidence must neither
        # block a merge nor silently inflate the other components.
        return 0.5, "no text similarity available"
    return max(0.0, min(1.0, similarity)), f"trigram similarity {similarity:.2f}"


def _conflict(left: ResolutionFeatures, right: ResolutionFeatures) -> str | None:
    """Identifier-level disagreements that no amount of similarity can outweigh.

    Unit and project numbers are identifiers, not attributes: two records at the
    same coordinates with the same area and different unit numbers are two
    apartments in one building, and merging them would corrupt the comparable
    evidence the whole product rests on. A weighted sum cannot express a veto,
    so conflicts are handled explicitly.
    """
    if (
        left.project_id is not None
        and right.project_id is not None
        and left.project_id != right.project_id
    ):
        return "different registered projects"

    left_unit = _normalise_unit(left.unit_number)
    right_unit = _normalise_unit(right.unit_number)
    if left_unit and right_unit and not _units_compatible(left_unit, right_unit):
        return f"conflicting unit numbers ({left.unit_number} vs {right.unit_number})"
    return None


def score_match(
    left: ResolutionFeatures,
    right: ResolutionFeatures,
    *,
    distance_m: float | None,
    text_similarity: float | None = None,
) -> MatchResult:
    """Score a candidate pair. Pure function -- no I/O."""
    if left.property_class != right.property_class:
        return MatchResult(
            total=0.0,
            decision=ResolutionDecision.DISTINCT,
            components=(MatchComponent("property class", 0.0, 1.0, "different property classes"),),
            conflict="different property classes",
        )

    spatial, spatial_note = _spatial_score(distance_m)
    area, area_note = _area_score(left.area_sqm, right.area_sqm)
    project, project_note = _project_unit_score(left, right)
    attributes, attribute_note = _attribute_score(left, right)
    price, price_note = _price_score(left.price, right.price)
    text, text_note = _text_score(text_similarity)

    components = (
        MatchComponent("spatial", spatial, 0.30, spatial_note),
        MatchComponent("area", area, 0.20, area_note),
        MatchComponent("project/unit", project, 0.15, project_note),
        MatchComponent("attributes", attributes, 0.15, attribute_note),
        MatchComponent("price", price, 0.10, price_note),
        MatchComponent("text", text, 0.10, text_note),
    )

    total = round(sum(c.contribution for c in components), 6)

    if total >= AUTO_MERGE_THRESHOLD:
        decision = ResolutionDecision.AUTO_MERGE
    elif total >= REVIEW_THRESHOLD:
        decision = ResolutionDecision.REVIEW
    else:
        decision = ResolutionDecision.DISTINCT

    # An identifier conflict can never merge automatically. It is sent to a
    # human rather than discarded, because unit numbers are also recorded
    # inconsistently and a silent split is its own kind of error.
    conflict = _conflict(left, right)
    if conflict is not None and decision is ResolutionDecision.AUTO_MERGE:
        decision = ResolutionDecision.REVIEW

    return MatchResult(total=total, decision=decision, components=components, conflict=conflict)


def best_match(
    subject: ResolutionFeatures,
    candidates: list[tuple[UUID, ResolutionFeatures, float | None, float | None]],
) -> tuple[UUID, MatchResult] | None:
    """Pick the strongest candidate. Returns None when there is nothing to compare."""
    best: tuple[UUID, MatchResult] | None = None
    for candidate_id, features, distance_m, text_similarity in candidates:
        result = score_match(
            subject, features, distance_m=distance_m, text_similarity=text_similarity
        )
        if best is None or result.total > best[1].total:
            best = (candidate_id, result)
    return best
