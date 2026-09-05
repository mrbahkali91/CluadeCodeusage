"""Tests for the Saudi Open Data connector.

The host is unreachable from this build environment, so every test here works
on payloads rather than the network. That is the point: the connector's job is
to cope with a response shape nobody has seen, and *that* logic is pure and can
be pinned down completely without a single request.
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from sreoi_sources.base import AvailabilityLabel, RawRecord
from sreoi_sources.opendata import (
    OpenDataSchemaError,
    OpenDataTransactionSource,
    find_records,
    normalise_property_class,
    resolve_fields,
    tokenise,
)


def _raw(records: list[dict[str, object]]) -> RawRecord:
    return RawRecord(external_id="ds-1", payload={"path": "/p", "records": records})


class TestTokenisation:
    def test_splits_on_separators_and_camel_case(self) -> None:
        assert tokenise("transacted_on") == {"transacted", "on"}
        assert tokenise("priceSqm") == {"price", "sqm"}
        assert tokenise("accrualPeriodicity") == {"accrual", "periodicity"}

    def test_period_no_longer_matches_periodicity(self) -> None:
        """The false positive that made a first version report a metadata field
        as a transaction date."""
        assert "period" not in tokenise("accrualPeriodicity")

    def test_keeps_arabic_tokens(self) -> None:
        assert tokenise("سعر_البيع") == {"سعر", "البيع"}


class TestFieldResolution:
    def test_maps_a_plausible_english_schema(self) -> None:
        observed = [
            "id",
            "price",
            "area_sqm",
            "transaction_date",
            "district_name",
            "city",
            "latitude",
            "longitude",
            "property_type",
        ]
        m = resolve_fields(observed)
        assert m["price"] == "price"
        assert m["area"] == "area_sqm"
        assert m["date"] == "transaction_date"
        assert m["district"] == "district_name"
        assert m["lat"] == "latitude" and m["lon"] == "longitude"

    def test_maps_an_arabic_schema(self) -> None:
        m = resolve_fields(["سعر", "مساحة", "تاريخ", "الحي"])
        assert m["price"] == "سعر"
        assert m["area"] == "مساحة"
        assert m["date"] == "تاريخ"
        assert m["district"] == "الحي"

    def test_returns_none_rather_than_a_wrong_guess(self) -> None:
        m = resolve_fields(["publisher", "accrualPeriodicity", "license", "theme"])
        assert m["price"] is None
        assert m["area"] is None
        assert m["date"] is None

    def test_an_override_wins_and_is_not_second_guessed(self) -> None:
        # The operator can see the portal; this code cannot.
        m = resolve_fields(["a", "b"], {"price": "some_unguessable_column"})
        assert m["price"] == "some_unguessable_column"


class TestEnvelopeDiscovery:
    @pytest.mark.parametrize(
        "payload",
        [
            {"results": [{"price": 1}]},
            {"records": [{"price": 1}]},
            {"data": [{"price": 1}]},
            {"items": [{"price": 1}]},
            {"result": {"records": [{"price": 1}]}},
            [{"price": 1}],
        ],
    )
    def test_finds_the_record_list_in_the_shapes_portals_actually_use(
        self, payload: object
    ) -> None:
        assert find_records(payload) == [{"price": 1}]

    def test_returns_empty_when_there_is_no_record_list(self) -> None:
        assert find_records({"title": "a dataset", "publisher": "x"}) == []
        assert find_records("not json at all") == []


class TestNormalisation:
    def test_maps_a_transaction_batch(self) -> None:
        source = OpenDataTransactionSource(dataset="ds-1")
        record = source.normalize(
            _raw(
                [
                    {
                        "id": "t1",
                        "price": "1200000",
                        "area_sqm": "150",
                        "date": "2025-03-01",
                        "district": "Qurtubah",
                        "city": "Riyadh",
                        "lat": "24.82",
                        "lon": "46.76",
                        "type": "apartment",
                    },
                    {
                        "id": "t2",
                        "price": 980000,
                        "area_sqm": 120,
                        "date": "2025-04-11",
                        "district": "Sidrah",
                        "city": "Riyadh",
                        "lat": 24.87,
                        "lon": 46.85,
                        "type": "فيلا",
                    },
                ]
            )
        )
        txns = record.data["transactions"]
        assert len(txns) == 2
        assert txns[0]["price"] == 1200000.0
        assert txns[0]["area_sqm"] == 150.0
        assert txns[1]["district"] == "Sidrah"

    def test_refuses_rather_than_guessing_when_price_cannot_be_mapped(self) -> None:
        source = OpenDataTransactionSource(dataset="ds-1")
        with pytest.raises(OpenDataSchemaError) as exc:
            source.normalize(
                _raw([{"publisher": "MOJ", "accrualPeriodicity": "quarterly", "theme": "housing"}])
            )
        message = str(exc.value)
        # The operator gets the observed keys and the exact export to run.
        assert "cannot map required fields" in message
        assert "accrualPeriodicity" in message
        assert "SREOI_OPENDATA_FIELD_PRICE" in message

    def test_refuses_when_there_is_no_location_at_all(self) -> None:
        source = OpenDataTransactionSource(dataset="ds-1")
        with pytest.raises(OpenDataSchemaError, match="district or lat"):
            source.normalize(
                _raw([{"price": 1, "area_sqm": 2, "date": "2025-01-01", "type": "apartment"}])
            )

    def test_accepts_coordinates_instead_of_a_district_name(self) -> None:
        source = OpenDataTransactionSource(dataset="ds-1")
        record = source.normalize(
            _raw(
                [
                    {
                        "price": 1,
                        "area_sqm": 2,
                        "date": "2025-01-01",
                        "latitude": 24.8,
                        "longitude": 46.7,
                        "type": "apartment",
                    }
                ]
            )
        )
        assert len(record.data["transactions"]) == 1

    def test_an_empty_price_is_skipped_never_read_as_free(self) -> None:
        source = OpenDataTransactionSource(dataset="ds-1")
        record = source.normalize(
            _raw(
                [
                    {
                        "price": "",
                        "area_sqm": "150",
                        "date": "2025-01-01",
                        "district": "Q",
                        "type": "apartment",
                    },
                    {
                        "price": "0",
                        "area_sqm": "150",
                        "date": "2025-01-01",
                        "district": "Q",
                        "type": "apartment",
                    },
                    {
                        "price": "500000",
                        "area_sqm": "150",
                        "date": "2025-01-01",
                        "district": "Q",
                        "type": "apartment",
                    },
                ]
            )
        )
        assert len(record.data["transactions"]) == 1
        assert record.data["skipped_incomplete"] == 2

    def test_records_the_mapping_it_used_so_a_stored_batch_is_auditable(self) -> None:
        source = OpenDataTransactionSource(dataset="ds-1")
        record = source.normalize(
            _raw(
                [
                    {
                        "price": 1,
                        "area_sqm": 2,
                        "date": "2025-01-01",
                        "district": "Q",
                        "type": "apartment",
                    }
                ]
            )
        )
        assert record.data["field_mapping"]["price"] == "price"
        assert "observed_fields" in record.data


class TestPropertyClass:
    def test_normalises_english_and_arabic_onto_the_platform_vocabulary(self) -> None:
        assert normalise_property_class("apartment") == "APARTMENT"
        assert normalise_property_class("شقة") == "APARTMENT"
        assert normalise_property_class("فيلا") == "VILLA"
        assert normalise_property_class("أرض") == "LAND"

    def test_leaves_an_unknown_class_visibly_unknown(self) -> None:
        """Folding an unrecognised class into APARTMENT would let the
        comparable kernel match it wrongly. Passed through instead, so the
        kernel can decline."""
        assert normalise_property_class("chalet") == "CHALET"
        assert normalise_property_class("مستودع") == "مستودع".upper()

    def test_treats_blank_as_absent(self) -> None:
        assert normalise_property_class("") is None
        assert normalise_property_class("   ") is None
        assert normalise_property_class(None) is None

    def test_normalisation_maps_the_class_onto_each_transaction(self) -> None:
        source = OpenDataTransactionSource(dataset="ds-1")
        record = source.normalize(
            _raw(
                [{"price": 1, "area_sqm": 2, "date": "2025-01-01", "district": "Q", "type": "شقة"}]
            )
        )
        assert record.data["transactions"][0]["property_class"] == "APARTMENT"

    def test_refuses_a_batch_with_no_property_type_column(self) -> None:
        """A villa weighted as an apartment comparable is the precise shape of a
        confident wrong valuation, so the batch is refused rather than
        defaulted."""
        source = OpenDataTransactionSource(dataset="ds-1")
        with pytest.raises(OpenDataSchemaError, match="property_type"):
            source.normalize(
                _raw([{"price": 1, "area_sqm": 2, "date": "2025-01-01", "district": "Q"}])
            )


class TestValidation:
    def test_flags_what_looks_like_aggregate_rather_than_transaction_data(self) -> None:
        """The outcome that rescopes the MVP, surfaced as a finding rather than
        left for someone to notice in a chart."""
        source = OpenDataTransactionSource(dataset="ds-1")
        record = source.normalize(
            _raw(
                [
                    {
                        "id": "same",
                        "price": 1,
                        "area_sqm": 2,
                        "date": "2025-01-01",
                        "district": "Q",
                        "type": "apartment",
                    },
                    {
                        "id": "same",
                        "price": 1,
                        "area_sqm": 2,
                        "date": "2025-01-01",
                        "district": "Q",
                        "type": "apartment",
                    },
                    {
                        "id": "same",
                        "price": 1,
                        "area_sqm": 2,
                        "date": "2025-01-01",
                        "district": "Q",
                        "type": "apartment",
                    },
                ]
            )
        )
        result = source.validate(record)
        assert not result.ok
        assert any("aggregate" in e for e in result.errors)


class TestRegistration:
    def test_is_labelled_requires_validation_not_confirmed(self) -> None:
        # No response from this host has ever been observed, and the source
        # matrix must not claim otherwise.
        source = OpenDataTransactionSource(dataset="ds-1")
        assert source.availability is AvailabilityLabel.REQUIRES_VALIDATION

    def test_discover_refuses_without_a_dataset_id(self) -> None:
        source = OpenDataTransactionSource(dataset="")
        with pytest.raises(OpenDataSchemaError, match="SREOI_OPENDATA_DATASET"):
            list(source.discover(__import__("datetime").datetime.now(__import__("datetime").UTC)))


class TestHealthCheck:
    """The check must fail when there is no data, not merely when the host is down.

    A reachability probe that accepted any non-5xx would report HEALTHY for a
    portal that had moved its API or emptied the dataset — the silent-death
    failure source monitoring exists to catch.
    """

    @staticmethod
    def _source(handler: object) -> OpenDataTransactionSource:
        transport = httpx.MockTransport(handler)  # type: ignore[arg-type]
        return OpenDataTransactionSource(client=httpx.Client(transport=transport), dataset="ds-1")

    def test_unconfigured_dataset_is_not_healthy(self) -> None:
        source = OpenDataTransactionSource(dataset="")
        health = source.health_check()
        assert health.healthy is False
        assert "SREOI_OPENDATA_DATASET" in (health.detail or "")

    def test_records_readable_is_healthy_and_names_the_path(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if "datastore_search" not in str(request.url):
                return httpx.Response(404)
            return httpx.Response(
                200,
                json={
                    "result": {
                        "records": [
                            {
                                "price": 1,
                                "area_sqm": 2,
                                "date": "2025-01-01",
                                "district": "Q",
                                "type": "apartment",
                            },
                        ]
                    }
                },
            )

        health = self._source(handler).health_check()
        assert health.healthy is True
        assert "datastore_search" in (health.detail or "")

    def test_reachable_host_serving_no_records_is_not_healthy(self) -> None:
        # HTTP 200 with an empty envelope is the shape of a dataset that was
        # withdrawn or renamed. It must not read as HEALTHY.
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"result": {"records": []}})

        health = self._source(handler).health_check()
        assert health.healthy is False
        assert "no records" in (health.detail or "")

    def test_connection_reset_says_where_to_run_it_from(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("reset by peer")

        health = self._source(handler).health_check()
        assert health.healthy is False
        assert "Saudi-resident egress" in (health.detail or "")
        # The per-path diagnosis must survive into the dashboard detail, not
        # be truncated to the headline.
        assert "ConnectError" in (health.detail or "")


class TestFetchFailureAdvice:
    """A transport failure and a wrong path need different remedies.

    Telling an operator whose network never reached the portal to try another
    path sends them chasing something that was never the problem.
    """

    @staticmethod
    def _source(handler: object) -> OpenDataTransactionSource:
        transport = httpx.MockTransport(handler)  # type: ignore[arg-type]
        return OpenDataTransactionSource(client=httpx.Client(transport=transport), dataset="ds-1")

    def test_all_paths_refused_at_the_transport_blames_the_network(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("reset by peer")

        source = self._source(handler)
        ref = next(iter(source.discover(datetime.now(UTC))))
        with pytest.raises(OpenDataSchemaError) as err:
            source.fetch(ref, limit=1)
        assert "Saudi-resident network" in str(err.value)
        assert "SREOI_OPENDATA_PATH" not in str(err.value)

    def test_http_errors_point_at_the_path_variable(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404)

        source = self._source(handler)
        ref = next(iter(source.discover(datetime.now(UTC))))
        with pytest.raises(OpenDataSchemaError, match="SREOI_OPENDATA_PATH"):
            source.fetch(ref, limit=1)
