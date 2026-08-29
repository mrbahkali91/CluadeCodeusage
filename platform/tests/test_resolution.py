"""Entity resolution scoring (pure domain)."""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest

from sreoi_domain.resolution import (
    AUTO_MERGE_THRESHOLD,
    REVIEW_THRESHOLD,
    ResolutionDecision,
    ResolutionFeatures,
    best_match,
    score_match,
)

PROJECT = uuid.uuid4()


def _f(**overrides: object) -> ResolutionFeatures:
    base: dict[str, object] = {
        "area_sqm": 140.0,
        "property_class": "APARTMENT",
        "bedrooms": 3,
        "floor": 4,
        "build_year": 2023,
        "price": Decimal("720000"),
    }
    base.update(overrides)
    return ResolutionFeatures(**base)  # type: ignore[arg-type]


def test_identical_unit_across_two_sources_auto_merges() -> None:
    result = score_match(
        _f(unit_number="B-402"),
        _f(area_sqm=141.0, unit_number="b 402", price=Decimal("735000")),
        distance_m=8.0,
        text_similarity=0.4,
    )
    assert result.total >= AUTO_MERGE_THRESHOLD
    assert result.decision is ResolutionDecision.AUTO_MERGE


def test_unit_numbers_are_normalised() -> None:
    """B-402, b 402 and B402 are the same door written three ways."""
    variants = ["B-402", "b 402", "B402", "b-402"]
    for variant in variants:
        result = score_match(
            _f(unit_number="B-402"), _f(unit_number=variant), distance_m=0.0, text_similarity=0.9
        )
        assert result.decision is ResolutionDecision.AUTO_MERGE, variant


def test_different_unit_numbers_are_evidence_against_a_match() -> None:
    same_everything = score_match(
        _f(unit_number="B-402"), _f(unit_number="B-905"), distance_m=0.0, text_similarity=0.95
    )
    assert same_everything.decision is not ResolutionDecision.AUTO_MERGE


def test_clearly_different_properties_stay_distinct() -> None:
    result = score_match(
        _f(unit_number="B-402"),
        _f(area_sqm=205.0, bedrooms=5, floor=11, unit_number="C-1105", price=Decimal("1500000")),
        distance_m=900.0,
        text_similarity=0.2,
    )
    assert result.total < REVIEW_THRESHOLD
    assert result.decision is ResolutionDecision.DISTINCT


def test_different_property_class_short_circuits() -> None:
    result = score_match(_f(), _f(property_class="VILLA"), distance_m=0.0, text_similarity=1.0)
    assert result.total == 0.0
    assert result.decision is ResolutionDecision.DISTINCT


def test_different_projects_never_merge() -> None:
    result = score_match(
        _f(project_id=PROJECT, unit_number="B-402"),
        _f(project_id=uuid.uuid4(), unit_number="B-402"),
        distance_m=0.0,
        text_similarity=0.9,
    )
    assert result.decision is not ResolutionDecision.AUTO_MERGE


def test_missing_text_similarity_is_neutral_not_zero() -> None:
    """Absent evidence must neither block a merge nor inflate other components."""
    without = score_match(_f(unit_number="B-402"), _f(unit_number="B-402"), distance_m=5.0)
    text = next(c for c in without.components if c.name == "text")
    assert text.score == pytest.approx(0.5)
    assert sum(c.weight for c in without.components) == pytest.approx(1.0)


def test_distance_dominates_when_far_apart() -> None:
    near = score_match(_f(), _f(), distance_m=10.0, text_similarity=0.5)
    far = score_match(_f(), _f(), distance_m=400.0, text_similarity=0.5)
    assert near.total > far.total


def test_components_sum_to_total() -> None:
    result = score_match(_f(), _f(), distance_m=20.0, text_similarity=0.7)
    assert sum(c.contribution for c in result.components) == pytest.approx(result.total, abs=1e-6)


def test_best_match_picks_the_strongest_candidate() -> None:
    subject = _f(unit_number="B-402")
    weak = (uuid.uuid4(), _f(area_sqm=150.0, unit_number="D-101"), 300.0, 0.2)
    strong = (uuid.uuid4(), _f(unit_number="B-402"), 5.0, 0.9)
    match = best_match(subject, [weak, strong])
    assert match is not None
    assert match[0] == strong[0]


def test_best_match_with_no_candidates() -> None:
    assert best_match(_f(), []) is None
