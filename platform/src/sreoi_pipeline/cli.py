"""Operational entry points: migrate, seed, demo."""

from __future__ import annotations

import argparse
import sys

from sreoi_persistence.db import ensure_extensions, session_scope
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


def _opendata(*, limit: int, dry_run: bool) -> int:
    """Fetch transaction-level records from the Saudi Open Data portal.

    Prints what it mapped and what it skipped before writing anything, because
    the first run of this against a real response is also the moment Assumption
    A-01 is finally answered -- and the answer might be "this is aggregate data,
    rescope the MVP".
    """
    from datetime import UTC, datetime

    from sreoi_sources.opendata import OpenDataSchemaError, OpenDataTransactionSource

    source = OpenDataTransactionSource()
    print(f"  portal   {source.base_url}")
    print(f"  dataset  {source.dataset or '(unset -- see SREOI_OPENDATA_DATASET)'}")

    try:
        ref = next(iter(source.discover(datetime.now(UTC))))
        raw = source.fetch(ref, limit=limit)
        record = source.normalize(raw)
    except OpenDataSchemaError as exc:
        print(f"\n  REFUSED: {exc}")
        return 1

    mapping = record.data["field_mapping"]
    txns = record.data["transactions"]
    print(f"  path     {record.data['source_path']}")
    print("\n  field mapping used:")
    shown = ("price", "area", "date", "district", "city", "lat", "lon", "property_type", "id")
    for logical in shown:
        print(f"    {logical:14} {mapping.get(logical) or '—'}")
    print(f"\n  usable transactions  {len(txns)}")
    print(f"  skipped incomplete   {record.data['skipped_incomplete']}")

    result = source.validate(record)
    if not result.ok:
        for error in result.errors:
            print(f"  ! {error}")

    if dry_run:
        print("\n  --dry-run: nothing written.")
        return 0 if result.ok else 1

    from sreoi_pipeline.ingest_opendata import store_transactions

    with session_scope() as session:
        outcome = store_transactions(session, record)
    print(f"\n  stored {outcome.written} transactions against source '{source.key}'")
    if outcome.located_by_district_centroid:
        print(
            f"  ! {outcome.located_by_district_centroid} of them had no coordinates and were "
            "placed at their district centroid.\n"
            "    Comparable DISTANCE for those rows is an artefact of the centroid, not a\n"
            "    measurement, and every valuation drawing on them inherits that."
        )
    if outcome.skipped_no_location:
        print(f"  ! {outcome.skipped_no_location} skipped: no coordinates and no known district")
    if outcome.skipped_unparseable_date:
        print(f"  ! {outcome.skipped_unparseable_date} skipped: unparseable transaction date")
    return 0 if result.ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sreoi")
    parser.add_argument(
        "command",
        choices=["seed", "demo", "reset-and-demo", "corpus", "health", "opendata"],
        help="operation to run",
    )
    parser.add_argument("--offline", action="store_true", help="skip the live KAPSARC index pull")
    parser.add_argument(
        "--limit", type=int, default=1000, help="opendata: maximum records to fetch"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="opendata: fetch, map and report, but write nothing to the database",
    )
    args = parser.parse_args(argv)

    # `opendata --dry-run` discovers the portal's schema and writes nothing, so
    # it must not require a database. Demanding one turned "find out what the
    # API returns" into "first stand up PostgreSQL", which is backwards: the
    # discovery step is what tells you whether the data is worth a database.
    needs_db = not (args.command == "opendata" and args.dry_run)
    if needs_db:
        ensure_extensions()

    if args.command in {"seed", "reset-and-demo"}:
        with session_scope() as session:
            counts = seed_all(session, live_index=not args.offline)
        print(f"seeded: {counts}")

    if args.command == "corpus":
        from sreoi_pipeline.demo import load_demo

        with session_scope() as session:
            print(f"corpus: {load_demo(session)}")

    if args.command == "opendata":
        return _opendata(limit=args.limit, dry_run=args.dry_run)

    if args.command == "health":
        from sreoi_pipeline.health import run_health_checks, source_statuses

        with session_scope() as session:
            run_health_checks(session)
            session.flush()
            for status in source_statuses(session):
                latency = f"{status.latency_ms:.0f}ms" if status.latency_ms else "—"
                print(f"  {status.key:20} {status.state:9} {latency:>8}  {status.detail or ''}")

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
