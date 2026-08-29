"""Filtered search, map layers, health and localisation."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.conftest import requires_db

pytestmark = requires_db

SUBMISSIONS = [
    {
        "external_id": "s2-auction-qurtubah",
        "title": "Infath auction lot — Qurtubah",
        "opportunity_type": "AUCTION",
        "property_class": "APARTMENT",
        "district": "Qurtubah",
        "area_sqm": 155,
        "bedrooms": 4,
        "floor": 2,
        "build_year": 2019,
        "seller_payment": 640000,
        "registration": 4000,
        "unit_number": "A-201",
    },
    {
        "external_id": "s2-assignment-sidrah",
        "title": "Assignment — Sidrah",
        "opportunity_type": "ASSIGNMENT",
        "property_class": "APARTMENT",
        "district": "Sidrah",
        "area_sqm": 140,
        "bedrooms": 3,
        "seller_payment": 120000,
        "remaining_installments": 600000,
        "unit_number": "B-402",
    },
    {
        "external_id": "s2-resale-rimal",
        "title": "Urgent resale — Al Rimal",
        "opportunity_type": "RESALE",
        "property_class": "APARTMENT",
        "district": "Al Rimal",
        "area_sqm": 165,
        "seller_payment": 1_450_000,
        "unit_number": "C-707",
    },
]


@pytest.fixture
def client(seeded_db: None) -> Iterator[TestClient]:
    from sreoi_api.main import app

    with TestClient(app) as test_client:
        for payload in SUBMISSIONS:
            test_client.post("/api/v1/opportunities", json=payload)
        yield test_client


def _search(client: TestClient, query: str = "") -> dict[str, Any]:
    response = client.get(f"/api/v1/search/opportunities?limit=500&{query}")
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def test_search_returns_everything_by_default(client: TestClient) -> None:
    assert _search(client)["count"] >= 3


def test_district_filter_applies(client: TestClient) -> None:
    body = _search(client, "district=Sidrah")
    assert body["count"] >= 1
    assert all(r["district"] == "Sidrah" for r in body["results"])


def test_multiple_districts_are_combined(client: TestClient) -> None:
    body = _search(client, "district=Sidrah&district=Qurtubah")
    districts = {r["district"] for r in body["results"]}
    assert districts <= {"Sidrah", "Qurtubah"}
    assert len(districts) >= 2


def test_opportunity_type_filter_applies(client: TestClient) -> None:
    body = _search(client, "opportunity_type=AUCTION")
    assert body["count"] >= 1
    assert all(r["opportunity_type"] == "AUCTION" for r in body["results"])


def test_max_cost_filter_excludes_expensive_and_incomplete(client: TestClient) -> None:
    body = _search(client, "max_cost=800000")
    for r in body["results"]:
        assert r["true_acquisition_cost"] is not None, "a budget filter needs a complete cost"
        assert r["true_acquisition_cost"] <= 800000


def test_hide_insufficient_removes_unrated(client: TestClient) -> None:
    body = _search(client, "hide_insufficient=true")
    assert all(r["classification"] != "INSUFFICIENT_DATA" for r in body["results"])


def test_filters_are_echoed_back(client: TestClient) -> None:
    """The user must be able to see what the system thinks they asked for."""
    body = _search(client, "district=Sidrah&min_discount=15&max_cost=900000")
    applied = body["filters_applied"]
    assert applied["districts"] == ["Sidrah"]
    assert applied["min_discount_pct"] == 15
    assert applied["max_true_acquisition_cost"] == 900000


def test_sorting_by_score_is_descending(client: TestClient) -> None:
    results = _search(client, "sort=score&hide_insufficient=true")["results"]
    scores = [r["score"] for r in results if r["score"] is not None]
    assert scores == sorted(scores, reverse=True)


def test_invalid_bbox_is_rejected(client: TestClient) -> None:
    assert client.get("/api/v1/search/opportunities?bbox=1,2,3").status_code == 422


def test_bbox_filters_spatially(client: TestClient) -> None:
    riyadh = _search(client, "bbox=46.5,24.6,47.0,25.0")["count"]
    elsewhere = _search(client, "bbox=39.0,21.0,39.5,21.5")["count"]
    assert riyadh >= 3
    assert elsewhere == 0


def test_map_geojson_is_valid(client: TestClient) -> None:
    body = client.get("/api/v1/map/opportunities?limit=500").json()
    assert body["type"] == "FeatureCollection"
    assert body["features"]
    feature = body["features"][0]
    assert feature["geometry"]["type"] == "Point"
    lon, lat = feature["geometry"]["coordinates"]
    assert 34 < lon < 56 and 16 < lat < 33, "coordinates must fall inside Saudi Arabia"


def test_district_layer_has_geometry_and_metrics(client: TestClient) -> None:
    body = client.get("/api/v1/map/districts").json()
    assert body["features"]
    for feature in body["features"]:
        assert feature["geometry"] is not None
        assert feature["properties"]["median_price_per_sqm"] is not None


def test_timeline_endpoint(client: TestClient) -> None:
    opportunity = _search(client, "district=Sidrah")["results"][0]
    events = client.get(f"/api/v1/opportunities/{opportunity['id']}/timeline").json()
    assert events
    assert {"event_type", "summary", "occurred_at"} <= set(events[0])


def test_admin_health_reports_state_and_legal_basis(client: TestClient) -> None:
    client.post("/api/v1/admin/health/run")
    rows = client.get("/api/v1/admin/health").json()
    assert rows
    for row in rows:
        assert row["state"] in {"HEALTHY", "STALE", "FAILING", "UNKNOWN", "DISABLED"}
        assert row["legal_access_method"]
        assert row["data_license"]


def test_resolution_decisions_are_auditable(client: TestClient) -> None:
    payload = dict(SUBMISSIONS[1], external_id="s2-assignment-sidrah-alt", area_sqm=141)
    client.post("/api/v1/opportunities", json=payload)
    rows = client.get("/api/v1/admin/resolution").json()
    assert rows
    assert {"decision", "score", "components", "method_version"} <= set(rows[0])


def test_ui_renders_in_english(client: TestClient) -> None:
    page = client.get("/")
    assert page.status_code == 200
    assert 'dir="ltr"' in page.text
    assert "Opportunities" in page.text


def test_ui_renders_in_arabic_with_rtl(client: TestClient) -> None:
    page = client.get("/?lang=ar")
    assert page.status_code == 200
    assert 'dir="rtl"' in page.text
    assert 'lang="ar"' in page.text
    assert "الفرص" in page.text


def test_unknown_locale_falls_back_to_english(client: TestClient) -> None:
    page = client.get("/?lang=zz")
    assert 'dir="ltr"' in page.text


def test_map_and_admin_pages_render(client: TestClient) -> None:
    assert client.get("/map").status_code == 200
    assert client.get("/admin/sources").status_code == 200
    assert client.get("/admin/sources?lang=ar").status_code == 200
