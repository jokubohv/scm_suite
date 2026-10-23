"""Fixture factories are deterministic, valid and RBAC-consistent."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.tests.utils.assertions import assert_money_string, assert_quantity_string, assert_uuid7
from app.tests.utils.factories import (
    make_identity,
    make_item,
    make_ledger_posting,
    make_location,
    make_principal,
    make_role,
    make_status,
    make_tenant,
    make_uom_pair,
    scopes_for_role,
)


def test_tenant_factory_is_deterministic_and_overridable() -> None:
    first = make_tenant(3)
    second = make_tenant(3)
    assert first == second
    assert first.id != make_tenant(4).id
    assert first.code == "TEN0003"
    custom = make_tenant(3, code="ACME", base_currency_code="EUR")
    assert custom.code == "ACME" and custom.base_currency_code == "EUR"
    with pytest.raises(AttributeError):
        make_tenant(0, does_not_exist=True)


def test_principal_defaults_to_admin_catalogue_scopes() -> None:
    tenant = make_tenant(0)
    principal = make_principal(tenant)
    assert principal.has_scope("platform.users.manage")
    assert principal.has_scope("procurement.po.approve")
    assert principal.roles == ("admin",)
    assert principal.scopes == scopes_for_role("admin")
    assert principal.is_active
    assert principal.to_payload()["tenant_id"] == str(tenant.id)


def test_platform_admin_implies_all_tenant_scopes() -> None:
    principal = make_principal(make_tenant(1), scopes=frozenset({"platform.admin"}))
    assert principal.has_scope("inventory.adjustment.post")
    assert principal.has_scope("reporting.cost.view")
    assert principal.has_any_scope("no.such.scope", "items.read")


def test_readonly_role_is_read_only() -> None:
    readonly = make_principal(make_tenant(0), role="readonly")
    assert readonly.has_scope("items.read")
    assert readonly.has_scope("reporting.read")
    assert not readonly.has_scope("items.update")
    assert not readonly.has_scope("procurement.po.approve")


def test_suspended_membership_is_not_active() -> None:
    principal = make_principal(make_tenant(0), membership_status="SUSPENDED")
    assert principal.is_active is False


def test_uom_pair_fixture_is_consistent() -> None:
    pair = make_uom_pair("eaches")
    assert pair.uom_type_code == "EA_TYPE"
    assert pair.base_uom_code == "EA"
    assert pair.unit("CS")["conversion_factor"] == "24"
    assert pair.unit("EA")["is_base"] is True
    assert make_uom_pair("weight").base_uom_code == "KG"
    with pytest.raises(KeyError):
        make_uom_pair("nope")


def test_item_factory_sets_type_flags_and_cost_method() -> None:
    lot_item = make_item(1, item_type_code="LOT_NUMBERED", costing_method="FIFO")
    assert lot_item.flags["is_lot_numbered"] is True
    assert lot_item.flags["requires_expiry"] is True
    assert lot_item.flags["is_serialized"] is False
    assert lot_item.to_payload()["costing_method"] == "FIFO"
    serial_item = make_item(2, item_type_code="SERIALIZED", costing_method="SPECIFIC")
    assert serial_item.flags["is_serialized"] is True
    assert serial_item.flags["is_lot_numbered"] is False
    custom = make_item(3, flags={"allow_vendor_consignment": True})
    assert custom.flags["allow_vendor_consignment"] is True
    assert custom.flags["is_stockable"] is True


def test_location_and_status_factories() -> None:
    location = make_location(0)
    assert location.location_type == "WAREHOUSE"
    assert location.is_bins_enabled is False
    bins_location = make_location(1, is_bins_enabled=True, location_type="RECEIVING")
    assert bins_location.is_bins_enabled is True
    available = make_status()
    assert available.is_default is True and available.is_committable is True
    hold = make_status("HOLD")
    assert hold.is_committable is False
    consigned = make_status("CONSIGNED_GOOD")
    assert consigned.is_consigned is True
    with pytest.raises(KeyError):
        make_status("NOT_A_STATUS")


def test_ledger_posting_factory_serialises_decimal_strings() -> None:
    posting = make_ledger_posting(0, qty="10", unit_cost="2.5")
    payload = posting.to_payload()
    assert payload["transaction_type"] == "T-01"
    assert posting.net_qty_base == Decimal("10")
    assert posting.net_value_base == Decimal("25.0")
    assert_quantity_string(payload["lines"][0]["qty_base"])
    assert_money_string(payload["lines"][0]["value_base"])
    assert_uuid7(payload["id"])
    assert payload["transaction_date"] == "2026-01-02"


def test_internal_move_is_value_neutral() -> None:
    move = make_ledger_posting(1, transaction_type="T-09", qty="4", unit_cost="3", internal_move=True)
    assert move.net_qty_base == 0
    assert move.net_value_base == 0
    assert len(move.lines) == 2
    assert move.lines[0]["location_id"] != move.lines[1]["location_id"]


def test_factories_are_idempotent_for_same_index() -> None:
    assert make_item(5).to_payload() == make_item(5).to_payload()
    assert make_location(5).to_payload() == make_location(5).to_payload()
    assert make_ledger_posting(5).to_payload() == make_ledger_posting(5).to_payload()


def test_fixture_factories_available(tenant, principal, uom_pair_eaches, item_factory) -> None:
    assert tenant.id == principal.tenant_id
    assert uom_pair_eaches.base_uom_code == "EA"
    assert item_factory(0, item_type_code="INVENTORY").item_type_code == "INVENTORY"


def test_identity_factory_is_global_and_deterministic() -> None:
    identity = make_identity(3)
    assert make_identity(3) == identity
    assert identity.id != make_identity(4).id
    assert identity.status == "ACTIVE"
    assert identity.is_platform_admin is False
    assert make_identity(3, email="custom@example.test").email == "custom@example.test"


def test_role_factory_matches_authoritative_templates() -> None:
    admin = make_role("admin")
    assert admin.system is True and admin.editable is False and admin.cloneable is True
    assert admin.scopes == scopes_for_role("admin")
    assert len(admin.scopes) == 241
    readonly = make_role("readonly")
    assert readonly.code == "readonly"
    assert "items.read" in readonly.scopes
    assert "items.update" not in readonly.scopes
    assert make_role("admin").to_payload()["scopes"] == sorted(admin.scopes)
    with pytest.raises(KeyError):
        make_role("not-a-role")
