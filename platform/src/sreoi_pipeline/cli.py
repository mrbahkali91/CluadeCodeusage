"""Operational entry points: migrate, seed, demo."""

from __future__ import annotations

import argparse
import sys

from sreoi_persistence.db import ensure_postgis, session_scope
from sreoi_pipeline.ingest import ingest_manual_submission
from sreoi_pipeline.seed import seed_all

DEMO_SUBMISSIONS: list[dict[str, object]] = [
    {
        "external_id": "demo-sidrah-assignment",
        "title": "Assignment (تنازل) — 3BR apartment, Sidrah",
        "opportunity_type": "ASSIGNMENT",
        "property_class": "APARTMENT",
        "district": "Sidrah",
        "area_sqm": 140,
        "bedrooms": 3,
        "floor": 4,
        "build_year": 2023,
        "developer_name": "ROSHN",
        "seller_payment": 120000,
        "remaining_installments": 600000,
        "description": "Seller exiting before handover. Contact 0551234567",
    },
    {
        "external_id": "demo-sidrah-assignment-unknown",
        "title": "Assignment — installments not disclosed",
        "opportunity_type": "ASSIGNMENT",
        "property_class": "APARTMENT",
        "district": "Sidrah",
        "area_sqm": 140,
        "bedrooms": 3,
        "seller_payment": 120000,
        "description": "Urgent. Remaining balance to developer not disclosed.",
    },
    {
        "external_id": "demo-infath-qurtubah",
        "title": "Infath auction lot — apartment, Qurtubah",
        "opportunity_type": "AUCTION",
        "property_class": "APARTMENT",
        "district": "Qurtubah",
        "area_sqm": 155,
        "bedrooms": 4,
        "floor": 2,
        "build_year": 2019,
        "seller_payment": 780000,
        "auction_commission": 19500,
        "registration": 4000,
        "renovation": 45000,
    },
    {
        "external_id": "demo-munsiyah-resale",
        "title": "Urgent resale — apartment, Al Munsiyah",
        "opportunity_type": "RESALE",
        "property_class": "APARTMENT",
        "district": "Al Munsiyah",
        "area_sqm": 165,
        "bedrooms": 4,
        "floor": 3,
        "build_year": 2021,
        "seller_payment": 940000,
        "brokerage": 23500,
    },
]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sreoi")
    parser.add_argument(
        "command", choices=["seed", "demo", "reset-and-demo"], help="operation to run"
    )
    parser.add_argument(
        "--offline", action="store_true", help="skip the live KAPSARC index pull"
    )
    args = parser.parse_args(argv)

    ensure_postgis()

    if args.command in {"seed", "reset-and-demo"}:
        with session_scope() as session:
            counts = seed_all(session, live_index=not args.offline)
        print(f"seeded: {counts}")

    if args.command in {"demo", "reset-and-demo"}:
        with session_scope() as session:
            for submission in DEMO_SUBMISSIONS:
                opportunity, result = ingest_manual_submission(session, dict(submission))
                score = result.score
                label = score.classification.label if score else "no score"
                total = f"{score.total:.1f}" if score else "—"
                print(f"  {opportunity.title[:48]:50} {total:>6}  {label}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
