#!/usr/bin/env python3
"""Validate Assumption A-01 against the Saudi Open Data portal.

WHY THIS SCRIPT EXISTS
----------------------
Every headline claim this platform makes -- "19% below market", "17 comparable
transactions", "estimated market value SAR 890k-930k" -- needs transaction-level
records carrying location, area, price and date. `docs/data-sources/matrix.md`
records that dependency as Assumption A-01 and says it must be validated "from a
Saudi-resident egress in week 1", because open.data.gov.sa resets the TLS
connection from a foreign datacenter IP before it ever sees an HTTP request. It
could not be checked from the environment this platform was built in.

It is also the highest-leverage open question in the codebase. TRACK-D.md
measures that district-level price index data alone is worth +0.090 of valuation
confidence, moves the median case past the 0.60 gate, and is simultaneously the
fix for the unmet interval-coverage target and the residual valuation bias. One
dataset, three findings.

WHAT IT DOES
------------
Discovers, rather than assumes. Nothing about the portal's API shape is
hard-coded as fact: the script tries a list of candidate base paths, reports
which respond, and only then inspects whatever schema comes back. Anything it
cannot confirm is printed as UNKNOWN rather than guessed.

  python3 tools/validate_open_data.py
  python3 tools/validate_open_data.py --dataset <uuid> --out ./evidence

Standard library only -- no install, no virtualenv, runs on any Python 3.11+.
Read-only: GET requests to public documented endpoints, no authentication, no
credentials, nothing written anywhere but the output directory you choose.

WHAT TO DO WITH THE RESULT
--------------------------
It writes every raw response under --out. Send that directory back and the
connector can be built against observed evidence instead of assumption. If the
verdict is that the data is aggregate-only or lacks area, say so -- that
outcome rescopes the MVP, and knowing it early is worth more than a connector.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request

# The dataset raised in conversation. Override with --dataset.
DEFAULT_DATASET = "c4eb90eb-de47-4996-9eba-8ae503980bcf"
HOST = "https://open.data.gov.sa"
TIMEOUT = 30

# Candidate API roots. This list is a set of GUESSES to be tested, not a claim
# about the portal's design -- the portal's own developer page is authoritative
# and is fetched first so a human can correct this list from what it says.
CANDIDATE_PATHS: tuple[tuple[str, str], ...] = (
    ("developer docs", "/ar/pages/developers-api"),
    ("dataset landing", "/ar/datasets/view/{ds}"),
    ("dataset api page", "/ar/datasets/view/{ds}/api"),
    ("api v1 dataset", "/api/datasets/v1/datasets/{ds}"),
    ("api v1 resources", "/api/datasets/v1/datasets/{ds}/resources"),
    ("data api v1", "/data/api/v1/datasets/{ds}"),
    ("ckan package_show", "/api/3/action/package_show?id={ds}"),
    ("ckan datastore", "/api/3/action/datastore_search?resource_id={ds}&limit=3"),
    ("odata catalog", "/api/explore/v2.1/catalog/datasets?limit=1"),
    # Opendatasoft's records endpoint. Included because KAPSARC runs that
    # platform, so the portal may too -- and because it is the shape that
    # actually returns rows rather than descriptions.
    ("odata records", "/api/explore/v2.1/catalog/datasets/{ds}/records?limit=5"),
    ("ckan resource rows", "/api/3/action/datastore_search?resource_id={ds}&limit=5"),
)

# Field names that would indicate the granularity the product needs. Matched on
# whole tokens, not substrings: a first version of this used `needle in key`
# and reported "FOUND date -> accrualperiodicity", because "periodicity"
# contains "period". A tool whose purpose is to avoid a misleading verdict must
# not manufacture one, so keys are split into tokens on separators and
# camelCase boundaries and the needle must match a token exactly, or be a
# prefix of one for the abbreviations where that is the convention.
WANTED: dict[str, tuple[str, ...]] = {
    "price": ("price", "amount", "value", "cost", "سعر", "قيمة", "المبلغ"),
    "area": ("area", "sqm", "space", "size", "مساحة"),
    "date": ("date", "transacted", "day", "month", "year", "quarter", "تاريخ"),
    "district": ("district", "neighborhood", "neighbourhood", "hood", "حي", "الحي"),
    "city": ("city", "region", "province", "مدينة", "منطقة"),
    "coordinates": ("lat", "latitude", "lon", "lng", "longitude", "coord", "احداثيات"),
    "property_type": ("type", "category", "classification", "نوع", "تصنيف"),
}

# Keys that mean "this response describes a dataset" rather than "this response
# contains rows of it". Their presence is why granularity is only ever assessed
# against records, never against metadata.
METADATA_MARKERS = frozenset(
    {
        "accrualperiodicity", "publisher", "license", "licence", "keyword",
        "theme", "modified", "issued", "landingpage", "dataset_id", "metas",
        "attributions", "records_count", "data-classification", "mimetype",
    }
)


def fetch(url: str) -> tuple[int | None, str, bytes, str | None]:
    """Return (status, content_type, body, error). Never raises."""
    request = urllib.request.Request(
        url,
        headers={
            # An ordinary browser UA. The portal resets foreign datacenter IPs
            # at the TLS layer regardless of headers, so this is politeness,
            # not evasion -- if it is blocked for you too, it is blocked.
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36"
            ),
            "Accept": "application/json, text/html;q=0.9, */*;q=0.8",
            "Accept-Language": "ar,en;q=0.9",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return (
                response.status,
                response.headers.get("Content-Type", ""),
                response.read(),
                None,
            )
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers.get("Content-Type", ""), exc.read(), None
    except (urllib.error.URLError, ssl.SSLError, TimeoutError, OSError) as exc:
        return None, "", b"", f"{type(exc).__name__}: {exc}"


def collect_keys(node: object, found: set[str], depth: int = 0) -> None:
    """Every key name anywhere in a nested structure, to a sane depth."""
    if depth > 8:
        return
    if isinstance(node, dict):
        for key, value in node.items():
            found.add(str(key))
            collect_keys(value, found, depth + 1)
    elif isinstance(node, list):
        for item in node[:20]:
            collect_keys(item, found, depth + 1)


def tokenise(key: str) -> set[str]:
    """Split a field name into comparable tokens.

    `transacted_on` -> {transacted, on}; `priceSqm` -> {price, sqm};
    `accrualPeriodicity` -> {accrual, periodicity} -- which is exactly why
    "period" no longer matches it.
    """
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", key)
    return {t for t in re.split(r"[^0-9A-Za-z\u0600-\u06FF]+", spaced.lower()) if t}


def assess(keys: set[str]) -> dict[str, list[str]]:
    matches: dict[str, list[str]] = {}
    for want, needles in WANTED.items():
        hits = []
        for key in keys:
            tokens = tokenise(key)
            if any(n in tokens for n in needles):
                hits.append(key)
        matches[want] = sorted(hits)
    return matches


def looks_like_records(keys: set[str], parsed: object) -> tuple[bool, str]:
    """Is this response rows of data, or a description of a dataset?

    Granularity claims are only meaningful about records. Reporting "price
    found" from a catalogue entry's metadata would be worse than reporting
    nothing.
    """
    lowered = {k.lower() for k in keys}
    metadata_hits = lowered & METADATA_MARKERS
    # A records payload normally carries a list of uniform objects.
    def widest_list(node: object, depth: int = 0) -> int:
        if depth > 6:
            return 0
        if isinstance(node, list):
            uniform = sum(1 for i in node if isinstance(i, dict))
            return max([uniform] + [widest_list(i, depth + 1) for i in node[:5]])
        if isinstance(node, dict):
            return max([0] + [widest_list(v, depth + 1) for v in node.values()])
        return 0

    rows = widest_list(parsed)
    # Metadata markers veto, and the veto is deliberately absolute. A catalogue
    # response is a *list of uniform objects* too -- one per dataset -- so
    # counting rows alone happily mistakes 7 dataset descriptions for 7 sales.
    # No genuine records endpoint carries `publisher` or `accrualPeriodicity` at
    # row level, so their presence is the reliable signal, and the cost of being
    # wrong in this direction is only a missed endpoint the operator can add,
    # against a fabricated verdict in the other.
    if metadata_hits:
        return False, f"metadata markers present: {sorted(metadata_hits)[:4]}"
    if rows >= 2:
        return True, f"{rows} uniform objects, no metadata markers"
    return False, "no list of uniform objects found"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate Assumption A-01 against open.data.gov.sa.",
    )
    parser.add_argument("--dataset", default=DEFAULT_DATASET)
    parser.add_argument("--out", default="./open-data-evidence")
    parser.add_argument("--host", default=HOST)
    args = parser.parse_args(argv)

    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    print(f"Assumption A-01 validation -- {args.host}")
    print(f"dataset {args.dataset}")
    print(f"evidence -> {out.resolve()}\n")
    print(f"{'probe':<22} {'status':>7} {'bytes':>9}  content-type / error")
    print("-" * 88)

    reachable = False
    record_keys: set[str] = set()
    metadata_keys: set[str] = set()
    json_hits: list[str] = []
    record_sources: list[str] = []

    for label, template in CANDIDATE_PATHS:
        url = args.host + template.format(ds=args.dataset)
        status, ctype, body, error = fetch(url)
        if error is not None:
            print(f"{label:<22} {'--':>7} {'--':>9}  {error[:46]}")
            continue
        reachable = True
        short = (ctype or "").split(";")[0]
        print(f"{label:<22} {status:>7} {len(body):>9}  {short}")

        safe = label.replace(" ", "_")
        if status and 200 <= status < 300 and body:
            suffix = "json" if "json" in short else "html"
            path = out / f"{safe}.{suffix}"
            path.write_bytes(body)
            if suffix == "json":
                try:
                    parsed = json.loads(body)
                except json.JSONDecodeError:
                    continue
                json_hits.append(label)
                keys: set[str] = set()
                collect_keys(parsed, keys)
                is_records, why = looks_like_records(keys, parsed)
                if is_records:
                    record_keys |= keys
                    record_sources.append(f"{label} ({why})")
                else:
                    metadata_keys |= keys
                    print(f"{'':<22} {'':>7} {'':>9}  ^ metadata, not records: {why}")

    print("-" * 88)

    if not reachable:
        print("\nVERDICT: UNREACHABLE from this network too.")
        print("  Every probe failed at the transport layer, so the portal never saw a")
        print("  request. Assumption A-01 remains UNVALIDATED -- this is not evidence")
        print("  that the data is absent, only that it could not be observed here.")
        print("  Try from a browser on the same machine; if that works and this does")
        print("  not, the block is on automated clients and the portal's own terms")
        print("  should be read before going further.")
        return 2

    if not json_hits:
        print("\nVERDICT: PORTAL REACHABLE, NO JSON API CONFIRMED.")
        print("  Something answered, but none of the candidate API paths returned JSON.")
        print(f"  Read {out / 'developer_docs.html'} for the portal's own documented")
        print("  endpoints and re-run with the correct path added to CANDIDATE_PATHS.")
        print("  Do NOT assume the guessed paths above are the real API.")
        return 1

    print(f"\nJSON returned by: {', '.join(json_hits)}")

    if not record_keys:
        print(f"\nVERDICT: METADATA ONLY -- {len(metadata_keys)} field names seen, but no")
        print("  response contained rows of data. The portal answered and describes the")
        print("  dataset, but this run observed no records, so A-01 is NOT validated.")
        print("  Granularity is deliberately not assessed against metadata: a catalogue")
        print("  entry mentioning 'price' says nothing about whether rows carry one.")
        print("  Find the records/download endpoint on the developer page and add it.")
        return 1

    print(f"records from: {'; '.join(record_sources)}")
    print(f"{len(record_keys)} distinct field names observed in records.\n")
    print("Granularity the product needs (assessed against RECORDS only):")
    matches = assess(record_keys)
    for want, hits in matches.items():
        mark = "FOUND  " if hits else "MISSING"
        detail = ", ".join(hits[:6]) if hits else "no matching field name"
        print(f"  {mark} {want:<14} {detail}")

    essential = ("price", "area", "date")
    location = ("district", "coordinates")
    has_essential = all(matches[k] for k in essential)
    has_location = any(matches[k] for k in location)

    print()
    if has_essential and has_location:
        print("VERDICT: A-01 LOOKS SUPPORTED -- price, area, date and a location field")
        print("  are all present. This does NOT yet confirm the rows are")
        print("  transaction-level rather than aggregates: check the saved JSON for one")
        print("  row per sale versus one row per district-quarter. That distinction is")
        print("  the whole assumption.")
    elif has_essential:
        print("VERDICT: PARTIAL -- price, area and date present, but no district or")
        print("  coordinate field was recognised. Without location the comparable")
        print("  selection cannot work; check the saved JSON for a location field this")
        print("  script did not recognise before concluding.")
    else:
        missing = [k for k in essential if not matches[k]]
        print(f"VERDICT: A-01 NOT SUPPORTED by this dataset -- missing {', '.join(missing)}.")
        print("  If other datasets on the portal carry transaction-level sales, point")
        print("  --dataset at them. If none do, the MVP needs rescoping, and")
        print("  docs/data-sources/matrix.md section A-01 says so explicitly.")

    print(f"\nRaw responses saved under {out.resolve()} -- send that directory back")
    print("so the connector can be built from observed evidence.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
