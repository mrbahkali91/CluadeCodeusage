"""Deterministic demonstration corpus.

Generates a realistic spread of Riyadh opportunities across the four target
districts, including deliberate cross-source duplicates and price-reduction
sequences so entity resolution and the timeline can be seen working.

Like the comparable corpus this is SYNTHETIC. It exercises the engine; it is
not market data.
"""

from __future__ import annotations

import random
from typing import Any

from sqlalchemy.orm import Session

from sreoi_pipeline.ingest import ingest_manual_submission

DISTRICTS = {
    "Qurtubah": (46.7600, 24.8200),
    "Al Munsiyah": (46.7900, 24.8300),
    "Al Rimal": (46.8300, 24.8450),
    "Sidrah": (46.8500, 24.8700),
}
DEVELOPERS = ["ROSHN", "NHC", "Retal", "Al Akaria", None]

TYPES = [
    ("AUCTION", "Infath auction lot", 0.22),
    ("ASSIGNMENT", "Assignment (تنازل)", 0.24),
    ("RESALE", "Urgent resale", 0.24),
    ("OFF_PLAN_RESALE", "Off-plan resale", 0.15),
    ("DEVELOPER_INVENTORY", "Developer inventory", 0.15),
]

DESCRIPTIONS = {
    "AUCTION": "Judicial execution sale via Infath. Viewing by appointment.",
    "ASSIGNMENT": "تنازل — seller exiting before handover, urgent.",
    "RESALE": "Owner relocating, quick sale preferred.",
    "OFF_PLAN_RESALE": "Off-plan unit, handover next year.",
    "DEVELOPER_INVENTORY": "Remaining developer inventory, direct sale.",
}


def _pick_type(rng: random.Random) -> tuple[str, str]:
    r = rng.random()
    cumulative = 0.0
    for key, label, weight in TYPES:
        cumulative += weight
        if r <= cumulative:
            return key, label
    return TYPES[-1][0], TYPES[-1][1]


def build_submissions(count: int = 56, seed: int = 4242) -> list[dict[str, Any]]:
    rng = random.Random(seed)
    submissions: list[dict[str, Any]] = []

    for i in range(count):
        district = rng.choice(list(DISTRICTS))
        lon, lat = DISTRICTS[district]
        kind, label = _pick_type(rng)

        area = round(rng.uniform(95, 230), 1)
        base_ppsqm = rng.uniform(5200, 7600)
        market_value = area * base_ppsqm
        # Sellers price somewhere between a real discount and a premium.
        asking = market_value * rng.uniform(0.68, 1.06)

        unit = f"{rng.choice('ABCD')}-{rng.randint(1, 12)}{rng.randint(0, 9)}{rng.randint(1, 9)}"
        submission: dict[str, Any] = {
            "external_id": f"demo-{i:03d}",
            "title": f"{label} — {district} {unit}",
            "opportunity_type": kind,
            "property_class": "APARTMENT" if area < 200 else "VILLA",
            "district": district,
            "area_sqm": area,
            "bedrooms": rng.choice([2, 3, 3, 4, 4, 5]),
            "floor": rng.randint(1, 14),
            "build_year": rng.randint(2015, 2025),
            "developer_name": rng.choice(DEVELOPERS),
            "unit_number": unit,
            "longitude": round(lon + rng.uniform(-0.012, 0.012), 6),
            "latitude": round(lat + rng.uniform(-0.012, 0.012), 6),
            "description": DESCRIPTIONS[kind],
        }

        if kind in {"ASSIGNMENT", "OFF_PLAN_RESALE"}:
            # The assignment structure: a small seller payment plus a large
            # balance owed to the developer.
            remaining = round(asking * rng.uniform(0.55, 0.85), -3)
            submission["seller_payment"] = round(asking - remaining, -3)
            # One in five sellers will not disclose the balance. Those must
            # refuse a discount rather than appear to be enormous bargains.
            if rng.random() < 0.20:
                submission["description"] += " Remaining balance not disclosed."
            else:
                submission["remaining_installments"] = remaining
        else:
            submission["seller_payment"] = round(asking, -3)

        if kind == "AUCTION":
            submission["auction_commission"] = round(asking * 0.025, -2)
            submission["registration"] = 4000
        if kind == "RESALE":
            submission["brokerage"] = round(asking * 0.025, -2)
        if rng.random() < 0.35:
            submission["renovation"] = round(area * rng.uniform(250, 900), -3)

        submissions.append(submission)

    return submissions


def build_duplicates(submissions: list[dict[str, Any]], seed: int = 99) -> list[dict[str, Any]]:
    """Re-list a few properties on another source, as really happens."""
    rng = random.Random(seed)
    duplicates: list[dict[str, Any]] = []
    for original in rng.sample(submissions, k=min(8, len(submissions))):
        duplicate = dict(original)
        duplicate["external_id"] = original["external_id"] + "-alt"
        duplicate["title"] = original["title"].replace("—", "-") + " (re-listed)"
        # Same unit, slightly different measurement and asking price.
        duplicate["area_sqm"] = round(float(original["area_sqm"]) * rng.uniform(0.99, 1.01), 1)
        if original.get("seller_payment"):
            duplicate["seller_payment"] = round(
                float(original["seller_payment"]) * rng.uniform(0.96, 1.04), -3
            )
        duplicates.append(duplicate)
    return duplicates


def build_reductions(submissions: list[dict[str, Any]], seed: int = 7) -> list[dict[str, Any]]:
    """Successive price cuts on the same listing — the strongest signal we carry."""
    rng = random.Random(seed)
    reductions: list[dict[str, Any]] = []
    for original in rng.sample(submissions, k=min(10, len(submissions))):
        if not original.get("seller_payment"):
            continue
        price = float(original["seller_payment"])
        for step in range(rng.randint(1, 3)):
            price *= rng.uniform(0.93, 0.98)
            cut = dict(original)
            cut["seller_payment"] = round(price, -3)
            if step >= 1:
                cut["title"] = original["title"] + " — URGENT"
                cut["description"] = original["description"] + " Price reduced, urgent sale."
            reductions.append(cut)
    return reductions


def load_demo(session: Session, count: int = 56) -> dict[str, int]:
    submissions = build_submissions(count)
    duplicates = build_duplicates(submissions)
    reductions = build_reductions(submissions)

    for payload in [*submissions, *duplicates, *reductions]:
        ingest_manual_submission(session, dict(payload))

    return {
        "submissions": len(submissions),
        "cross_source_duplicates": len(duplicates),
        "price_reductions": len(reductions),
    }
