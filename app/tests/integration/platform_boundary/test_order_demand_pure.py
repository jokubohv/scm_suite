"""Pure tests for the Phase-1 StubOrderDemandAdapter / open-demand filters (no database)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from app.core.repositories.boundary import (
    BoundaryValidationError,
    OpenDemandRecord,
    OrderBoundaryUnavailable,
    StubOrderDemandAdapter,
    decode_cursor,
    encode_cursor,
    parse_demand_rows,
    query_open_demand,
)

ITEM_A = UUID("10000000-0000-7000-8000-000000000001")
ITEM_B = UUID("10000000-0000-7000-8000-000000000002")
LOC_A = UUID("20000000-0000-7000-8000-000000000001")
LOC_B = UUID("20000000-0000-7000-8000-000000000002")


def _record(**overrides: object) -> OpenDemandRecord:
    base: dict[str, object] = {
        "demand_ref": "SO-1-1",
        "source_doc_type": "SALES_ORDER",
        "source_doc_ref": "SO-1",
        "item_id": ITEM_A,
        "location_id": LOC_A,
        "qty_base": Decimal("5"),
        "uom": "EA",
        "required_by": date(2026, 11, 1),
        "priority": 0,
        "committed": False,
        "qty_committed": Decimal("0"),
        "status": "OPEN",
    }
    base.update(overrides)
    return OpenDemandRecord(**base)  # type: ignore[arg-type]


def _adapter() -> StubOrderDemandAdapter:
    rows = (
        _record(demand_ref="SO-1-1", required_by=date(2026, 11, 1), priority=0),
        _record(
            demand_ref="SO-2-1",
            item_id=ITEM_B,
            location_id=LOC_B,
            required_by=date(2026, 10, 28),
            priority=1,
            committed=True,
            qty_committed=Decimal("2"),
            status="PARTIALLY_COMMITTED",
        ),
        _record(demand_ref="SO-3-1", required_by=date(2026, 12, 1), status="COMMITTED"),
    )
    return StubOrderDemandAdapter(rows, fixture_version="fixture-test-1")


def _row(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "demand_ref": "SO-9-1",
        "source_doc_type": "sales_order",
        "item_id": str(ITEM_A),
        "location_id": str(LOC_A),
        "qty": "3",
        "uom": "EA",
        "required_by": "2026-11-05",
        "priority": 2,
        "status": "OPEN",
    }
    base.update(overrides)
    return base


class TestQuery:
    def test_default_page_excludes_fully_committed_demand(self) -> None:
        page = query_open_demand(_adapter())
        assert page["fixture_version"] == "fixture-test-1"
        refs = [item["demand_ref"] for item in page["items"]]
        assert refs == ["SO-2-1", "SO-1-1"]  # required_by ascending
        assert "SO-3-1" not in refs
        first = page["items"][0]
        assert first["qty_base"] == "5"
        assert first["qty_uom"] == "5"
        assert first["qty_committed"] == "2"
        assert first["qty_uncommitted"] == "3"
        assert first["uom"] == "EA"
        assert first["committed"] is True

    def test_filters(self) -> None:
        adapter = _adapter()
        assert [i["demand_ref"] for i in query_open_demand(adapter, item_id=ITEM_B)["items"]] == ["SO-2-1"]
        assert [i["demand_ref"] for i in query_open_demand(adapter, location_id=LOC_A)["items"]] == ["SO-1-1"]
        assert [i["demand_ref"] for i in query_open_demand(adapter, required_by_from=date(2026, 10, 29))["items"]] == [
            "SO-1-1"
        ]
        assert [i["demand_ref"] for i in query_open_demand(adapter, required_by_to=date(2026, 10, 30))["items"]] == [
            "SO-2-1"
        ]
        assert [i["demand_ref"] for i in query_open_demand(adapter, status="PARTIALLY_COMMITTED")["items"]] == [
            "SO-2-1"
        ]
        assert [i["demand_ref"] for i in query_open_demand(adapter, priority=0)["items"]] == ["SO-1-1"]
        assert query_open_demand(adapter, item_id=uuid4())["items"] == []
        assert query_open_demand(adapter, item_id=uuid4())["next_cursor"] is None

    def test_pagination_cursor(self) -> None:
        adapter = _adapter()
        first = query_open_demand(adapter, limit=1)
        assert [i["demand_ref"] for i in first["items"]] == ["SO-2-1"]
        assert first["next_cursor"]
        second = query_open_demand(adapter, limit=1, cursor=first["next_cursor"])
        assert [i["demand_ref"] for i in second["items"]] == ["SO-1-1"]
        assert second["next_cursor"] is None

    def test_invalid_inputs(self) -> None:
        with pytest.raises(BoundaryValidationError):
            query_open_demand(_adapter(), limit=0)
        with pytest.raises(BoundaryValidationError):
            query_open_demand(_adapter(), status="BOGUS")
        with pytest.raises(BoundaryValidationError):
            query_open_demand(_adapter(), cursor="!!!")
        with pytest.raises(BoundaryValidationError):
            query_open_demand(_adapter(), required_by_from=date(2026, 11, 2), required_by_to=date(2026, 11, 1))

    def test_unavailable_adapter_raises_documented_503_code(self) -> None:
        adapter = _adapter()
        adapter.available = False
        with pytest.raises(OrderBoundaryUnavailable) as excinfo:
            query_open_demand(adapter)
        assert excinfo.value.code == "ORDER_BOUNDARY_UNAVAILABLE"
        assert excinfo.value.http_status == 503


class TestAdapterFixtures:
    def test_load_replaces_rows_and_version(self) -> None:
        adapter = _adapter()
        loaded = adapter.load(parse_demand_rows([_row()]), fixture_version="v2")
        assert loaded == 1
        assert adapter.fixture_version == "v2"
        page = query_open_demand(adapter)
        assert [item["demand_ref"] for item in page["items"]] == ["SO-9-1"]

    def test_cursor_helpers_roundtrip(self) -> None:
        cursor = encode_cursor(required_by=date(2026, 10, 1), demand_ref="SO-1-1")
        assert decode_cursor(cursor) == (date(2026, 10, 1), "SO-1-1")

    def test_parse_rows_validation(self) -> None:
        rows = parse_demand_rows([_row()])
        assert rows[0].qty_base == Decimal("3")
        assert rows[0].source_doc_type == "SALES_ORDER"
        assert rows[0].source_doc_ref == "SO-9-1"

        invalid_payloads = [
            [_row(demand_ref="")],
            [_row(item_id="not-a-uuid")],
            [_row(qty=None, qty_base=None)],
            [_row(qty="0")],
            [_row(required_by="not-a-date")],
            [_row(status="UNKNOWN")],
            [_row(priority=-1)],
            [_row(qty_base="5", qty="3")],
            [_row(), _row()],
        ]
        for payload in invalid_payloads:
            with pytest.raises(BoundaryValidationError):
                parse_demand_rows(payload)

    def test_parse_rows_accepts_explicit_qty_base(self) -> None:
        rows = parse_demand_rows([_row(qty=None, qty_base="7.5", committed=True, qty_committed="2.5")])
        assert rows[0].qty_base == Decimal("7.5")
        assert rows[0].committed is True
        assert rows[0].qty_uncommitted == Decimal("5")
