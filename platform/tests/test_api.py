"""API contract tests."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from tests.conftest import requires_db

pytestmark = requires_db


@pytest.fixture
def client(seeded_db: None) -> Iterator[TestClient]:
    from sreoi_api.main import app

    with TestClient(app) as test_client:
        yield test_client


SUBMISSION = {
    "external_id": "api-test-1",
    "title": "Infath auction lot — Qurtubah",
    "opportunity_type": "AUCTION",
    "property_class": "APARTMENT",
    "district": "Qurtubah",
    "area_sqm": 155,
    "bedrooms": 4,
    "floor": 2,
    "build_year": 2019,
    "seller_payment": 780000,
    "registration": 4000,
}


def test_health(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_create_and_fetch_opportunity(client: TestClient) -> None:
    created = client.post("/api/v1/opportunities", json=SUBMISSION)
    assert created.status_code == 201, created.text
    body = created.json()

    fetched = client.get(f"/api/v1/opportunities/{body['id']}")
    assert fetched.status_code == 200
    assert fetched.json()["id"] == body["id"]


def test_money_fields_are_never_bare_numbers(client: TestClient) -> None:
    """The response invariant from ADR-007."""
    payload = dict(SUBMISSION, external_id="api-test-money")
    body = client.post("/api/v1/opportunities", json=payload).json()
    for item in body["cost"]["line_items"]:
        amount = item["amount"]
        assert set(amount) >= {"value", "unit", "basis", "confidence", "sources"}
        assert amount["unit"] == "SAR"


def test_comparables_endpoint_exposes_the_evidence(client: TestClient) -> None:
    payload = dict(SUBMISSION, external_id="api-test-comps")
    body = client.post("/api/v1/opportunities", json=payload).json()
    response = client.get(f"/api/v1/opportunities/{body['id']}/comparables")
    assert response.status_code == 200
    data = response.json()
    assert data["count"] >= 1
    assert data["comparables"]
    first = data["comparables"][0]
    assert {"weight", "weight_breakdown", "adjusted_price_per_sqm", "distance_m"} <= set(first)


def test_synthetic_evidence_is_flagged(client: TestClient) -> None:
    """Demonstration data must never be presentable as real transactions."""
    payload = dict(SUBMISSION, external_id="api-test-synthetic")
    body = client.post("/api/v1/opportunities", json=payload).json()
    assert body["valuation"]["is_synthetic_evidence"] is True


def test_score_endpoint_is_decomposable(client: TestClient) -> None:
    payload = dict(SUBMISSION, external_id="api-test-score")
    body = client.post("/api/v1/opportunities", json=payload).json()
    score = client.get(f"/api/v1/opportunities/{body['id']}/score").json()
    assert len(score["components"]) == 7
    total = sum(c["contribution"] for c in score["components"])
    assert total == pytest.approx(score["total"], abs=0.01)


def test_refused_discount_surfaces_a_reason(client: TestClient) -> None:
    payload = {
        "external_id": "api-test-refused",
        "opportunity_type": "ASSIGNMENT",
        "property_class": "APARTMENT",
        "district": "Sidrah",
        "area_sqm": 140,
        "seller_payment": 120000,
    }
    body = client.post("/api/v1/opportunities", json=payload).json()
    assert body["discount_percent"] is None
    assert "remaining installments" in body["score_detail"]["discount_refused_reason"]


def test_provenance_endpoint(client: TestClient) -> None:
    payload = dict(SUBMISSION, external_id="api-test-prov")
    body = client.post("/api/v1/opportunities", json=payload).json()
    rows = client.get(f"/api/v1/opportunities/{body['id']}/provenance").json()
    assert rows
    assert all({"basis", "confidence", "field_name"} <= set(r) for r in rows)


def test_invalid_area_is_rejected(client: TestClient) -> None:
    payload = dict(SUBMISSION, external_id="api-test-bad-area", area_sqm=5)
    assert client.post("/api/v1/opportunities", json=payload).status_code == 422


def test_admin_sources_expose_legal_basis(client: TestClient) -> None:
    sources = client.get("/api/v1/admin/sources").json()
    assert sources
    for source in sources:
        assert source["legal_access_method"]
        assert source["data_license"]
    synthetic = [s for s in sources if s["is_synthetic"]]
    assert synthetic, "the demonstration corpus must be flagged as synthetic"


def test_ui_pages_render(client: TestClient) -> None:
    payload = dict(SUBMISSION, external_id="api-test-ui")
    body = client.post("/api/v1/opportunities", json=payload).json()
    assert client.get("/").status_code == 200
    detail = client.get(f"/opportunities/{body['id']}")
    assert detail.status_code == 200
    assert "Demonstration data" in detail.text


def test_unknown_opportunity_is_404(client: TestClient) -> None:
    response = client.get("/api/v1/opportunities/00000000-0000-0000-0000-000000000000")
    assert response.status_code == 404
