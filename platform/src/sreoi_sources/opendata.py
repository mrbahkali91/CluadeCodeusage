"""Saudi Open Data portal — transaction-level real-estate records.

WHY THIS CONNECTOR IS SHAPED THIS WAY
-------------------------------------
`open.data.gov.sa` resets the TLS connection from a foreign datacenter address
before it sees an HTTP request, so this connector was written without ever
observing a response. That single fact drives every design decision below, and
the alternative -- hard-coding a guessed schema -- would have produced code that
looks finished, imports cleanly, passes a review, and silently maps the wrong
column into `price` the first time anyone runs it.

So: **nothing about the response shape is assumed.** The envelope is located by
trying the shapes open-data portals actually use, and the fields are resolved
from the keys the response really contains. When a required field cannot be
resolved, `normalize` RAISES and names every key it did see, so the operator
maps it explicitly with one environment variable. It never falls back to a
plausible-looking column.

That refusal is the same invariant the valuation engine already holds: an
unknown material value is a first-class state, not a zero.

CONFIGURATION
-------------
    SREOI_OPENDATA_BASE_URL     default https://open.data.gov.sa
    SREOI_OPENDATA_DATASET      dataset id (uuid)
    SREOI_OPENDATA_PATH         explicit path template; else candidates are tried
    SREOI_OPENDATA_API_KEY      sent if set
    SREOI_OPENDATA_API_KEY_HEADER   header name, default `api_key`
    SREOI_OPENDATA_API_KEY_PARAM    send as query parameter instead, if set
    SREOI_OPENDATA_PAGE_STYLE   `offset` (default) or `page`
    SREOI_OPENDATA_PAGE_SIZE    default 100

Explicit field mapping, each overriding discovery:

    SREOI_OPENDATA_FIELD_PRICE / _AREA / _DATE / _DISTRICT / _CITY
    SREOI_OPENDATA_FIELD_LAT / _LON / _PROPERTY_TYPE / _ID

LEGAL BASIS
-----------
KSA Open Data License, unauthenticated public endpoints, no scraping and no
bypass of any control. Labelled REQUIRES_VALIDATION -- not CONFIRMED -- because
no response from this host has ever been observed. Assumption A-01 in
docs/data-sources/matrix.md.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from typing import Any

import httpx

from sreoi_sources.base import (
    AvailabilityLabel,
    LegalAccessMethod,
    NormalizedRecord,
    PropertySource,
    RawRecord,
    SourceHealth,
    SourceRef,
    ValidationResult,
)

DEFAULT_BASE_URL = "https://open.data.gov.sa"

# Tried in order. The portal's own developer page is authoritative; when it
# names a path, set SREOI_OPENDATA_PATH and this list is bypassed entirely.
PATH_CANDIDATES: tuple[str, ...] = (
    "/api/datasets/v1/datasets/{dataset}/resources",
    "/api/datasets/v1/datasets/{dataset}",
    "/data/api/v1/datasets/{dataset}/records",
    "/api/3/action/datastore_search?resource_id={dataset}",
    "/api/explore/v2.1/catalog/datasets/{dataset}/records",
)

# Where a list of records sits inside a response envelope.
ENVELOPE_KEYS: tuple[tuple[str, ...], ...] = (
    ("results",),
    ("records",),
    ("data",),
    ("items",),
    ("result", "records"),
    ("response", "docs"),
)

# Candidate names per logical field, matched on WHOLE TOKENS. Substring
# matching was tried first and reported `accrualPeriodicity` as a date field
# because "periodicity" contains "period"; a tool that manufactures a false
# positive is worse than one that finds nothing.
FIELD_CANDIDATES: dict[str, tuple[str, ...]] = {
    "price": ("price", "amount", "value", "cost", "saleprice", "سعر", "قيمة", "المبلغ"),
    "area": ("area", "sqm", "size", "space", "مساحة"),
    "date": ("date", "transacted", "transactiondate", "day", "month", "تاريخ"),
    "district": ("district", "neighborhood", "neighbourhood", "hood", "حي", "الحي"),
    "city": ("city", "region", "province", "مدينة", "منطقة"),
    "lat": ("lat", "latitude", "y", "خطالعرض"),
    "lon": ("lon", "lng", "long", "longitude", "x", "خطالطول"),
    "property_type": ("type", "category", "classification", "نوع", "تصنيف"),
    "id": ("id", "uuid", "recordid", "transactionid", "identifier"),
}

# A transaction cannot be used as comparable evidence without these four.
# Location is checked separately: either a district name or a coordinate pair.
#
# `property_type` is required, not optional, because the comparable-weighting
# kernel matches on property class: an unmapped class would let a villa weight
# as an apartment comparable, which is the precise shape of a confident wrong
# valuation. Better to refuse the batch and be told which column to map.
REQUIRED = ("price", "area", "date", "property_type")

# Normalised to the vocabulary the platform already stores (APARTMENT, VILLA).
# Anything unrecognised is uppercased and passed through rather than folded
# into APARTMENT -- an unknown class must stay visibly unknown, since the
# kernel can then decline to match it instead of matching it wrongly.
PROPERTY_CLASS_SYNONYMS: dict[str, str] = {
    "apartment": "APARTMENT",
    "flat": "APARTMENT",
    "unit": "APARTMENT",
    "شقة": "APARTMENT",
    "شقه": "APARTMENT",
    "وحدة": "APARTMENT",
    "villa": "VILLA",
    "house": "VILLA",
    "duplex": "VILLA",
    "فيلا": "VILLA",
    "فلة": "VILLA",
    "دوبلكس": "VILLA",
    "land": "LAND",
    "plot": "LAND",
    "أرض": "LAND",
    "ارض": "LAND",
    "قطعة": "LAND",
    "building": "BUILDING",
    "عمارة": "BUILDING",
    "commercial": "COMMERCIAL",
    "office": "COMMERCIAL",
    "تجاري": "COMMERCIAL",
}


def normalise_property_class(value: object) -> str | None:
    """Map a source's own vocabulary onto the platform's."""
    if value in (None, ""):
        return None
    text = str(value).strip()
    if text == "":
        return None
    return PROPERTY_CLASS_SYNONYMS.get(text.lower(), text.upper())


class OpenDataSchemaError(RuntimeError):
    """The response shape could not be mapped, and guessing is not an option."""


def tokenise(key: str) -> set[str]:
    """Split a field name into comparable tokens.

    `transacted_on` -> {transacted, on}; `priceSqm` -> {price, sqm};
    `accrualPeriodicity` -> {accrual, periodicity}, which is exactly why
    "period" no longer matches it.
    """
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", key)
    return {t for t in re.split(r"[^0-9A-Za-z؀-ۿ]+", spaced.lower()) if t}


def resolve_fields(
    observed: Sequence[str], overrides: dict[str, str] | None = None
) -> dict[str, str | None]:
    """Map each logical field to a key that is actually present.

    An override always wins and is not validated against `observed`: the
    operator can see the portal and this code cannot.
    """
    overrides = overrides or {}
    resolved: dict[str, str | None] = {}
    for logical, needles in FIELD_CANDIDATES.items():
        if overrides.get(logical):
            resolved[logical] = overrides[logical]
            continue
        match: str | None = None
        for key in observed:
            tokens = tokenise(key)
            if any(n in tokens for n in needles):
                match = key
                break
        resolved[logical] = match
    return resolved


def find_records(payload: Any) -> list[dict[str, Any]]:
    """Locate the list of records inside an unknown envelope."""
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if not isinstance(payload, dict):
        return []
    for path in ENVELOPE_KEYS:
        node: Any = payload
        for step in path:
            if isinstance(node, dict) and step in node:
                node = node[step]
            else:
                node = None
                break
        if isinstance(node, list):
            rows = [r for r in node if isinstance(r, dict)]
            if rows:
                return rows
    return []


def _env_overrides() -> dict[str, str]:
    out: dict[str, str] = {}
    for logical in FIELD_CANDIDATES:
        value = os.environ.get(f"SREOI_OPENDATA_FIELD_{logical.upper()}")
        if value:
            out[logical] = value
    return out


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "")
    if text == "":
        # `float('')` raises, but `Number('')` is 0 in the TypeScript tier -- the
        # same trap in two languages. An empty cell is unknown, never zero.
        return None
    try:
        return float(text)
    except ValueError:
        return None


class OpenDataTransactionSource(PropertySource):
    key = "open_data_gov_sa"
    name = "Saudi Open Data — real-estate transactions"
    legal_access_method = LegalAccessMethod.OPEN_DATA
    data_license = "KSA Open Data License (attribution)"
    # Deliberately not CONFIRMED: no response from this host has been observed.
    availability = AvailabilityLabel.REQUIRES_VALIDATION
    source_confidence = 0.85

    def __init__(
        self,
        client: httpx.Client | None = None,
        timeout: float = 30.0,
        base_url: str | None = None,
        dataset: str | None = None,
    ) -> None:
        super().__init__()
        self.base_url = (
            base_url or os.environ.get("SREOI_OPENDATA_BASE_URL") or DEFAULT_BASE_URL
        ).rstrip("/")
        self.dataset = dataset or os.environ.get("SREOI_OPENDATA_DATASET", "")
        self.page_size = int(os.environ.get("SREOI_OPENDATA_PAGE_SIZE", "100"))
        self.page_style = os.environ.get("SREOI_OPENDATA_PAGE_STYLE", "offset")
        self._client = client or httpx.Client(timeout=timeout, follow_redirects=True)

    # -------------------------------------------------------------- request
    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        key = os.environ.get("SREOI_OPENDATA_API_KEY")
        if key and not os.environ.get("SREOI_OPENDATA_API_KEY_PARAM"):
            headers[os.environ.get("SREOI_OPENDATA_API_KEY_HEADER", "api_key")] = key
        return headers

    def _params(self, offset: int) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if self.page_style == "page":
            params["page"] = offset // max(1, self.page_size) + 1
            params["size"] = self.page_size
        else:
            params["limit"] = self.page_size
            params["offset"] = offset
        param_name = os.environ.get("SREOI_OPENDATA_API_KEY_PARAM")
        key = os.environ.get("SREOI_OPENDATA_API_KEY")
        if param_name and key:
            params[param_name] = key
        return params

    def _paths(self) -> tuple[str, ...]:
        explicit = os.environ.get("SREOI_OPENDATA_PATH")
        return (explicit,) if explicit else PATH_CANDIDATES

    # ---------------------------------------------------------------- port
    def discover(self, since: datetime) -> Iterator[SourceRef]:
        if not self.dataset:
            raise OpenDataSchemaError(
                "SREOI_OPENDATA_DATASET is not set. Point it at the dataset id "
                "from the portal URL, e.g. the uuid in "
                "open.data.gov.sa/ar/datasets/view/<uuid>."
            )
        yield SourceRef(
            external_id=self.dataset, url=f"{self.base_url}/", hint={"since": since.isoformat()}
        )

    def fetch(self, ref: SourceRef, limit: int = 1000) -> RawRecord:
        """Page through the dataset, trying each candidate path until one works."""
        rows: list[dict[str, Any]] = []
        attempts: list[str] = []
        used_path: str | None = None

        for template in self._paths():
            path = template.format(dataset=ref.external_id)
            offset = 0
            rows = []
            while len(rows) < limit:
                url = f"{self.base_url}{path}"
                try:
                    response = self._client.get(
                        url, headers=self._headers(), params=self._params(offset)
                    )
                except httpx.HTTPError as exc:
                    attempts.append(f"{path}: {type(exc).__name__}: {exc}")
                    break
                if response.status_code >= 400:
                    attempts.append(f"{path}: HTTP {response.status_code}")
                    break
                try:
                    payload = response.json()
                except ValueError:
                    attempts.append(f"{path}: response was not JSON")
                    break
                page = find_records(payload)
                if not page:
                    attempts.append(f"{path}: HTTP 200 but no record list found")
                    break
                rows.extend(page)
                offset += len(page)
                used_path = path
                if len(page) < self.page_size:
                    break
            if rows:
                break

        if not rows:
            # Two very different failures land here and they need different
            # advice. Every attempt failing at the transport layer means the
            # network never reached the portal, and telling that operator to
            # try another path would send them chasing a path that was never
            # the problem.
            transport_failed = attempts and all(
                "ConnectError" in a or "ConnectTimeout" in a or "ReadTimeout" in a for a in attempts
            )
            remedy = (
                "Every attempt failed before an HTTP response, so this is the network, "
                "not the path: the portal resets the connection for traffic outside "
                "Saudi Arabia. Run this from a Saudi-resident network."
                if transport_failed
                else "Read the portal's developer page for the correct path and set "
                "SREOI_OPENDATA_PATH, e.g. '/api/v1/datasets/{dataset}/records'."
            )
            raise OpenDataSchemaError(
                "no records retrieved. Paths tried:\n  "
                + "\n  ".join(attempts or ["(none)"])
                + f"\n\n{remedy}"
            )

        return RawRecord(
            external_id=ref.external_id,
            payload={"path": used_path, "count": len(rows), "records": rows[:limit]},
            url=f"{self.base_url}{used_path or ''}",
        )

    def normalize(self, raw: RawRecord) -> NormalizedRecord:
        rows: list[dict[str, Any]] = list(raw.payload.get("records") or [])
        if not rows:
            raise OpenDataSchemaError("no records to normalise")

        observed = sorted({k for row in rows[:50] for k in row})
        mapping = resolve_fields(observed, _env_overrides())

        missing = [f for f in REQUIRED if mapping.get(f) is None]
        has_location = mapping.get("district") or (mapping.get("lat") and mapping.get("lon"))
        if missing or not has_location:
            need = list(missing) + ([] if has_location else ["district or lat+lon"])
            raise OpenDataSchemaError(
                "cannot map required fields: "
                + ", ".join(need)
                + "\n\nField names observed in the response:\n  "
                + "\n  ".join(observed)
                + "\n\nMap them explicitly, for example:\n"
                + "".join(
                    f"  export SREOI_OPENDATA_FIELD_{f.upper()}=<the column above>\n"
                    for f in (list(missing) + (["district"] if not has_location else []))
                )
                + "\nRefusing to guess: mapping the wrong column into `price` would "
                "produce confident, wrong valuations."
            )

        transactions: list[dict[str, Any]] = []
        skipped = 0
        for row in rows:
            price = _to_float(row.get(mapping["price"] or ""))
            area = _to_float(row.get(mapping["area"] or ""))
            property_class = normalise_property_class(row.get(mapping["property_type"] or ""))
            if price is None or area is None or price <= 0 or area <= 0 or property_class is None:
                # A transaction with no price or no area is not comparable
                # evidence. Counted, never coerced.
                skipped += 1
                continue
            transactions.append(
                {
                    "external_id": str(
                        row.get(mapping["id"] or "") or f"{raw.external_id}:{len(transactions)}"
                    ),
                    "price": price,
                    "area_sqm": area,
                    "transacted_on": row.get(mapping["date"] or ""),
                    "district": row.get(mapping["district"] or "") or None,
                    "city": row.get(mapping["city"] or "") or None,
                    "latitude": _to_float(row.get(mapping["lat"] or "")),
                    "longitude": _to_float(row.get(mapping["lon"] or "")),
                    "property_class": normalise_property_class(
                        row.get(mapping["property_type"] or "")
                    ),
                }
            )

        return NormalizedRecord(
            external_id=raw.external_id,
            kind="transaction_batch",
            data={
                "field_mapping": mapping,
                "observed_fields": observed,
                "transactions": transactions,
                "skipped_incomplete": skipped,
                "source_path": raw.payload.get("path"),
            },
        )

    def validate(self, record: NormalizedRecord) -> ValidationResult:
        errors: list[str] = []
        rows = record.data.get("transactions") or []
        if not rows:
            errors.append("no usable transactions after normalisation")
        # One row per district-quarter rather than one per sale is the outcome
        # that rescopes the MVP, so it is surfaced as a validation finding
        # rather than left for someone to notice in a chart.
        if rows and len({r["external_id"] for r in rows}) < len(rows) / 2:
            errors.append(
                "more than half the records share an id -- this may be aggregate "
                "data (one row per district-period) rather than transaction-level"
            )
        return ValidationResult(ok=not errors, errors=tuple(errors))

    def health_check(self) -> SourceHealth:
        """Probe for *records*, not for a reachable host.

        An earlier version asked the portal root for anything below HTTP 500 and
        called that healthy. That answers "does the domain exist", which the
        dashboard already assumes; it would have reported HEALTHY for a portal
        that had moved its API, renamed its columns, or emptied the dataset --
        exactly the silent-death failure this module exists to catch. So the
        check runs the same discovery the ingest runs, one page deep.
        """
        started = datetime.now(UTC)

        def elapsed() -> float:
            return (datetime.now(UTC) - started).total_seconds() * 1000

        if not self.dataset:
            return SourceHealth(
                source_key=self.key,
                healthy=False,
                checked_at=started,
                detail="not configured: SREOI_OPENDATA_DATASET is unset",
            )

        try:
            ref = next(iter(self.discover(started)))
            raw = self.fetch(ref, limit=1)
        except Exception as exc:
            # `fetch` already converts transport errors into an
            # OpenDataSchemaError that lists every path with its own failure, so
            # the whole message is the diagnosis -- keep it, collapsed to one
            # line for the dashboard. Taking only the first line here left the
            # most likely real-world failure reading "no records retrieved.
            # Paths tried:" with nothing after the colon.
            detail = " ".join(str(exc).split())
            if "ConnectError" in detail or "ConnectTimeout" in detail:
                detail += (
                    " -- from outside Saudi Arabia this host resets the TLS connection; "
                    "run from a Saudi-resident egress."
                )
            return SourceHealth(
                source_key=self.key,
                healthy=False,
                checked_at=started,
                latency_ms=elapsed(),
                detail=detail[:900],
            )

        return SourceHealth(
            source_key=self.key,
            healthy=True,
            checked_at=started,
            latency_ms=elapsed(),
            detail=f"records readable at {raw.payload.get('path')}",
        )
