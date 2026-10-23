"""FS-15 implementation-backed reporting event payload schemas.

The canonical schemas must be folded from `app/contracts/reporting/events/`, carry no `x-todo`, and
accept the payload builders emitted for the reporting refresh job.
"""

from __future__ import annotations

import json
from pathlib import Path

from app.contracts.reporting.events import (
    E_FAILED,
    E_REFRESHED,
    EVENT_VERSION,
    refresh_failed_payload,
    view_refreshed_payload,
)

ROOT = Path(__file__).resolve().parents[3]
CANONICAL = ROOT / "contracts/events/reporting.readmodels"
TENANT = "00000000-0000-0000-0000-000000000001"


def _schema(event: str) -> dict:
    return json.loads((CANONICAL / f"{event}.schema.json").read_text())


def _check_payload(event: str, payload: dict) -> None:
    schema = _schema(event)
    assert "x-todo" not in schema
    assert schema["type"] == "object"
    for key in schema["required"]:
        assert key in payload, (event, key)
    assert payload["event_type"] == event
    assert payload["event_version"] == EVENT_VERSION
    assert payload["aggregate"]["type"] == "reporting"
    assert payload["aggregate"]["id"] == payload["data"]["view_name"]
    data_schema = schema["properties"]["data"]
    for key in data_schema["required"]:
        assert key in payload["data"], (event, key)
    for key in payload["data"]:
        assert key in data_schema["properties"], (event, key)


def test_refreshed_schema_folded_and_payload_builder() -> None:
    payload = view_refreshed_payload(tenant_id=TENANT, view_name="v_inventory_balance",
                                     duration_ms=12, row_count=42, source_high_watermark="2026-10-23T12:00:00Z")
    _check_payload(E_REFRESHED, payload)
    assert payload["data"]["tenant_id"] == TENANT
    assert payload["data"]["source_high_watermark"] == "2026-10-23T12:00:00Z"


def test_failed_schema_folded_and_payload_builder() -> None:
    payload = refresh_failed_payload(tenant_id=TENANT, view_name="v_manufacturing_workbench", attempt=3,
                                     error_code="REFRESH_TIMEOUT", is_retryable=True)
    _check_payload(E_FAILED, payload)
    assert payload["data"]["attempt"] == 3 and payload["data"]["is_retryable"] is True


def test_canonical_reporting_events_have_no_todo() -> None:
    for event in (E_REFRESHED, E_FAILED):
        schema = _schema(event)
        assert "x-todo" not in schema, event
        assert schema["x-contract"] == "reporting.readmodels"
        assert schema["x-event"] == event
        assert schema["properties"]["event_type"]["const"] == event
