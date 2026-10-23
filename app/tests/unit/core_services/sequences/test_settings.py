"""Tenant settings tests: declared catalogue, precedence, typed validation, audit/invalidation."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import insert

from app.core.models.platform import tenant_settings
from app.core.settings import (
    ALLOWED_NAMESPACES,
    InvalidNamespace,
    InvalidSettingValue,
    SettingsService,
    SettingType,
    UnknownSetting,
    declared_settings,
)
from app.tests.unit.core_services.sequences.support import create_tenant

pytestmark = pytest.mark.integration


def test_declared_settings_filter_db_namespaces() -> None:
    definitions = declared_settings()
    assert len(definitions) == 7
    assert ("inventory", "negative_inventory_policy") in definitions
    assert ("rbac", "sod_enforcement") in definitions
    assert ("auth", "local_login_enabled") in definitions
    assert ("policy", "approval_definitions_required") not in definitions
    assert definitions[("inventory", "retention_ledger_years")].value_type is SettingType.NUMBER
    assert definitions[("inventory", "multi_location_enabled")].value_type is SettingType.BOOL
    assert "inventory" in ALLOWED_NAMESPACES and "policy" not in ALLOWED_NAMESPACES


def test_definition_validation_rules() -> None:
    definitions = declared_settings()
    with pytest.raises(InvalidSettingValue):
        definitions[("inventory", "multi_location_enabled")].validate("yes")
    with pytest.raises(InvalidSettingValue):
        definitions[("inventory", "retention_ledger_years")].validate(-1)
    with pytest.raises(InvalidSettingValue):
        definitions[("inventory", "retention_ledger_years")].validate("7")
    with pytest.raises(InvalidSettingValue):
        definitions[("inventory", "negative_inventory_policy")].validate("MAYBE")
    with pytest.raises(InvalidSettingValue):
        definitions[("rbac", "sod_enforcement")].validate("LENIENT")
    assert definitions[("rbac", "sod_enforcement")].validate("WARN") == "WARN"


async def test_precedence_tenant_global_default_and_caller(db_session) -> None:
    tenant_id = await create_tenant(db_session, code="SET-A")
    global_tenant = await create_tenant(db_session, code="SET-GLOBAL")
    service = SettingsService(db_session, global_tenant_id=global_tenant)

    # 1) seed catalogue default.
    resolution = await service.resolve(tenant_id, "inventory", "negative_inventory_policy")
    assert (resolution.value, resolution.source) == ("BLOCK", "DEFAULT")

    # 2) global/system-tenant row.
    await db_session.execute(
        insert(tenant_settings).values(
            id=uuid.uuid4(),
            tenant_id=global_tenant,
            namespace="inventory",
            key="negative_inventory_policy",
            value="WARN",
            value_type="STRING",
            is_default=False,
        )
    )
    service.invalidate()
    global_resolution = await service.resolve(tenant_id, "inventory", "negative_inventory_policy")
    assert (global_resolution.value, global_resolution.source) == ("WARN", "GLOBAL")

    # 3) tenant row wins over global/seed.
    await service.set_setting(tenant_id, "inventory", "negative_inventory_policy", "BLOCK")
    tenant_resolution = await service.resolve(tenant_id, "inventory", "negative_inventory_policy")
    assert (tenant_resolution.value, tenant_resolution.source) == ("BLOCK", "TENANT")

    # 4) caller default is last (undeclared key with explicit default).
    caller = await service.resolve(tenant_id, "platform", "unknown_flag", default=42, allow_undeclared=True)
    assert (caller.value, caller.source) == (42, "CALLER")


async def test_set_setting_validates_and_audits(db_session) -> None:
    tenant_id = await create_tenant(db_session, code="SET-B")
    changes = []
    service = SettingsService(db_session, audit=changes.append)
    with pytest.raises(InvalidSettingValue):
        await service.set_setting(tenant_id, "inventory", "multi_location_enabled", "yes")
    with pytest.raises(InvalidNamespace):
        await service.set_setting(tenant_id, "nope", "key", True)
    with pytest.raises(UnknownSetting):
        await service.set_setting(tenant_id, "inventory", "unknown_key", True)

    resolution = await service.set_setting(tenant_id, "inventory", "retention_ledger_years", 10, actor_id=uuid.uuid4())
    assert resolution.value == 10 and resolution.source == "TENANT"
    assert len(changes) == 1 and changes[0].old_value == 7 and changes[0].new_value == 10
    assert await service.get_value(tenant_id, "inventory", "retention_ledger_years") == 10
    effective = await service.effective_namespace(tenant_id, "inventory")
    assert effective["retention_ledger_years"].value == 10
    assert effective["negative_inventory_policy"].value == "BLOCK"

    assert await service.delete_setting(tenant_id, "inventory", "retention_ledger_years") is True
    assert await service.get_value(tenant_id, "inventory", "retention_ledger_years") == 7
    assert await service.delete_setting(tenant_id, "inventory", "retention_ledger_years") is False


async def test_cache_and_invalidation(db_session) -> None:
    tenant_id = await create_tenant(db_session, code="SET-C")
    service = SettingsService(db_session)
    assert await service.get_value(tenant_id, "auth", "local_login_enabled") is True  # seed default
    await db_session.execute(
        insert(tenant_settings).values(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            namespace="auth",
            key="local_login_enabled",
            value=False,
            value_type="BOOL",
            is_default=False,
        )
    )
    assert await service.get_value(tenant_id, "auth", "local_login_enabled") is True  # cached
    service.invalidate(tenant_id=tenant_id, namespace="auth", key="local_login_enabled")
    assert await service.get_value(tenant_id, "auth", "local_login_enabled") is False
