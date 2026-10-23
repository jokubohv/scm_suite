"""Feature-flag service tests: declared catalogue, default-off, tenant overrides, cache invalidation."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.core.flags import (
    FeatureDisabled,
    FlagService,
    UnknownFlag,
    declared_flags,
)
from app.core.models.platform import feature_flag
from app.tests.unit.core_services.sequences.support import create_tenant

pytestmark = pytest.mark.integration


def test_declared_flags_catalogue_is_default_off() -> None:
    flags = declared_flags()
    assert len(flags) == 17
    assert all(flag.endswith(".enabled") for flag in flags)
    assert all(definition.default_enabled is False for definition in flags.values())
    assert "platform.core.enabled" in flags


async def test_default_off_without_tenant_row(db_session) -> None:
    tenant_id = await create_tenant(db_session, code="FLAG-A")
    service = FlagService(db_session)
    resolution = await service.get(tenant_id, "platform.core.enabled")
    assert resolution.enabled is False
    assert resolution.source == "DEFAULT"
    assert resolution.default_enabled is False
    assert await service.is_enabled(None, "platform.core.enabled") is False
    assert await service.is_enabled(uuid.uuid4(), "platform.core.enabled") is False


async def test_tenant_override_and_require_flag(db_session) -> None:
    tenant_id = await create_tenant(db_session, code="FLAG-B")
    changes = []
    service = FlagService(db_session, audit=changes.append)
    with pytest.raises(FeatureDisabled) as error:
        await service.require_flag(tenant_id, "platform.core.enabled")
    assert error.value.code == "DOMAIN_DISABLED" and error.value.http_status == 409

    resolution = await service.set_flag(tenant_id, "platform.core.enabled", True, notes="enable core")
    assert resolution.enabled is True and resolution.source == "OVERRIDE"
    assert await service.is_enabled(tenant_id, "platform.core.enabled") is True
    await service.require_flag(tenant_id, "platform.core.enabled")  # no raise
    assert changes and changes[0].enabled is True and changes[0].previous_enabled is False
    assert changes[0].notes == "enable core"

    row = (
        await db_session.execute(
            select(feature_flag.c.is_enabled, feature_flag.c.source).where(
                feature_flag.c.tenant_id == tenant_id,
                feature_flag.c.flag_key == "platform.core.enabled",
            )
        )
    ).one()
    assert row.is_enabled is True and row.source == "OVERRIDE"

    flags = await service.list_flags(tenant_id)
    assert len(flags) == 17 and flags["platform.core.enabled"].enabled is True


async def test_cache_requires_explicit_invalidation(db_session) -> None:
    tenant_id = await create_tenant(db_session, code="FLAG-C")
    service = FlagService(db_session)
    assert await service.is_enabled(tenant_id, "inventory.ledger.enabled") is False
    # Bypass the service (simulating another instance writing the row) while the cache is warm.
    await db_session.execute(
        feature_flag.insert().values(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            flag_key="inventory.ledger.enabled",
            is_enabled=True,
            source="OVERRIDE",
        )
    )
    assert await service.is_enabled(tenant_id, "inventory.ledger.enabled") is False
    service.invalidate(tenant_id=tenant_id, flag_key="inventory.ledger.enabled")
    assert await service.is_enabled(tenant_id, "inventory.ledger.enabled") is True
    service.invalidate()
    assert await service.is_enabled(tenant_id, "inventory.ledger.enabled") is True


async def test_unknown_flags_and_allow_undeclared(db_session) -> None:
    tenant_id = await create_tenant(db_session, code="FLAG-D")
    service = FlagService(db_session)
    with pytest.raises(UnknownFlag):
        await service.get(tenant_id, "not.a.declared.flag")
    with pytest.raises(UnknownFlag):
        await service.set_flag(tenant_id, "not-a-flag", True)
    undeclared = await service.set_flag(tenant_id, "feature.custom.enabled", True, allow_undeclared=True)
    assert undeclared.enabled is True and undeclared.source == "OVERRIDE"
    assert await service.is_enabled(tenant_id, "feature.custom.enabled", allow_undeclared=True) is True
