"""Serialization helpers + router scope contract for the reference/config API."""

from __future__ import annotations

from datetime import UTC, date, datetime
from uuid import UUID

import pytest

from app.api.routers.platform import config as config_router_module
from app.core.repositories.config import (
    FLAG_CHANGED_EVENT,
    serialize_currency,
    serialize_custom_field,
    serialize_feature_flag,
    serialize_fiscal_period,
    serialize_number_sequence,
    serialize_row,
)
from app.core.security.scopes import ScopeRegistry

SCOPE_REGISTRY = ScopeRegistry()

FIXED_TS = datetime(2026, 10, 23, 12, 0, 0, tzinfo=UTC)


def test_serialize_row_converts_uuid_datetime_and_date() -> None:
    row = {
        "id": UUID("00000000-0000-7000-8000-000000000001"),
        "created_at": FIXED_TS,
        "start_date": date(2026, 10, 1),
        "label": "hello",
        "optional": None,
    }
    serialized = serialize_row(row)
    assert serialized["id"] == "00000000-0000-7000-8000-000000000001"
    assert serialized["created_at"] == "2026-10-23T12:00:00+00:00"
    assert serialized["start_date"] == "2026-10-01"
    assert serialized["label"] == "hello"
    assert serialized["optional"] is None


def test_currency_serialization_shape() -> None:
    payload = serialize_currency(
        {
            "code": "USD",
            "numeric_code": 840,
            "minor_units": 2,
            "is_active": True,
            "version": 1,
            "created_at": FIXED_TS,
            "updated_at": FIXED_TS,
            "tenant_id": None,  # not part of the contract shape
        }
    )
    assert payload["code"] == "USD"
    assert payload["tenant_scope"] == "GLOBAL"
    assert "tenant_id" not in payload
    assert payload["created_at"] == "2026-10-23T12:00:00+00:00"


def test_fiscal_period_serialization_shape() -> None:
    payload = serialize_fiscal_period(
        {
            "id": UUID(int=7),
            "period_key": "2026-10",
            "start_date": date(2026, 10, 1),
            "end_date": date(2026, 10, 31),
            "status": "OPEN",
            "closed_at": None,
            "source_version": 1,
            "version": 2,
            "created_at": FIXED_TS,
            "updated_at": FIXED_TS,
            "tenant_id": "internal",
        }
    )
    assert set(payload) == {
        "id",
        "period_key",
        "start_date",
        "end_date",
        "status",
        "closed_at",
        "source_version",
        "version",
        "created_at",
        "updated_at",
    }
    assert payload["id"] == str(UUID(int=7))
    assert payload["start_date"] == "2026-10-01"


def test_custom_field_serialization_keeps_json_objects() -> None:
    payload = serialize_custom_field(
        {
            "id": UUID(int=8),
            "entity_type": "ITEM",
            "field_code": "warranty_months",
            "label": "Warranty",
            "data_type": "SELECT",
            "options": ["A", "B"],
            "is_required": True,
            "is_searchable": False,
            "validation": {"min": 1},
            "version": 1,
            "created_at": FIXED_TS,
            "updated_at": FIXED_TS,
        }
    )
    assert payload["options"] == ["A", "B"]
    assert payload["validation"] == {"min": 1}
    assert payload["is_required"] is True


def test_feature_flag_serialization_shape() -> None:
    payload = serialize_feature_flag(
        {
            "id": UUID(int=9),
            "flag_key": "inventory.ledger.enabled",
            "is_enabled": False,
            "source": "DEFAULT",
            "changed_by": None,
            "changed_at": None,
            "notes": None,
            "version": 1,
            "created_at": FIXED_TS,
            "updated_at": FIXED_TS,
        }
    )
    assert payload["flag_key"] == "inventory.ledger.enabled"
    assert payload["is_enabled"] is False
    assert payload["source"] == "DEFAULT"
    assert payload["changed_at"] is None


def test_number_sequence_serialization_shape() -> None:
    payload = serialize_number_sequence(
        {
            "id": UUID(int=10),
            "doc_type": "PO",
            "scope_key": "TENANT",
            "prefix": "PO",
            "padding": 6,
            "next_value": 1,
            "reset_policy": "NEVER",
            "gap_policy": "NON_GAPLESS",
            "period_key": None,
            "version": 1,
            "created_at": FIXED_TS,
            "updated_at": FIXED_TS,
        }
    )
    assert payload["next_value"] == 1
    assert payload["gap_policy"] == "NON_GAPLESS"
    assert payload["period_key"] is None


# ----------------------------------------------------------------------------------------------
# Router scope contract: every route declares exactly one *catalogued* capability scope.
# ----------------------------------------------------------------------------------------------
def _route_index() -> dict[tuple[str, str], object]:
    index: dict[tuple[str, str], object] = {}
    for route in config_router_module.router.routes:
        path = str(getattr(route, "path", ""))
        for method in sorted(getattr(route, "methods", ()) or ()):
            index[(method, path)] = route
    return index


def test_router_exposes_expected_routes_only() -> None:
    routes = _route_index()
    assert set(routes) == set(config_router_module.ROUTE_SCOPES)
    assert len(routes) == 15


def test_every_route_scope_is_catalogued_and_singular() -> None:
    scopes = set(config_router_module.ROUTE_SCOPES.values())
    catalogue = SCOPE_REGISTRY.catalogue
    for scope in scopes:
        entry = catalogue.scopes_by_name.get(scope)
        assert entry is not None, f"scope {scope!r} missing from design/permissions.json"
        assert entry.assignable is True
    # Each route maps to exactly one scope in the contract table.
    assert len(config_router_module.ROUTE_SCOPES) == len(set(config_router_module.ROUTE_SCOPES.keys()))


def test_expected_api_surface() -> None:
    assert set(config_router_module.ROUTE_SCOPES) == {
        ("GET", "/api/v1/admin/currencies"),
        ("POST", "/api/v1/admin/currencies"),
        ("GET", "/api/v1/admin/currencies/{code}"),
        ("PATCH", "/api/v1/admin/currencies/{code}"),
        ("GET", "/api/v1/admin/fiscal-periods"),
        ("POST", "/api/v1/admin/fiscal-periods"),
        ("GET", "/api/v1/admin/custom-fields"),
        ("POST", "/api/v1/admin/custom-fields"),
        ("GET", "/api/v1/admin/custom-fields/{field_def_id}"),
        ("PATCH", "/api/v1/admin/custom-fields/{field_def_id}"),
        ("DELETE", "/api/v1/admin/custom-fields/{field_def_id}"),
        ("GET", "/api/v1/admin/flags"),
        ("GET", "/api/v1/admin/flags/{flag_key}"),
        ("PATCH", "/api/v1/admin/flags/{flag_key}"),
        ("GET", "/api/v1/admin/sequences"),
    }


def test_flag_changed_event_constant() -> None:
    assert FLAG_CHANGED_EVENT == "platform.flag.changed"


@pytest.mark.parametrize(
    ("scope", "expected"),
    [
        (config_router_module.REFERENCE_READ, "platform.reference.read"),
        (config_router_module.FINANCE_READ, "platform.finance.read"),
        (config_router_module.FINANCE_SYNC, "platform.finance.sync"),
        (config_router_module.CUSTOM_FIELD_MANAGE, "platform.customfield.manage"),
        (config_router_module.FLAG_MANAGE, "platform.flag.manage"),
        (config_router_module.SEQUENCE_MANAGE, "platform.sequence.manage"),
    ],
)
def test_scope_constants(scope: str, expected: str) -> None:
    assert scope == expected
