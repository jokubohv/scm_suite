"""Top-level conftest fixtures behave as documented (settings, tenant, clients, context, skip)."""

from __future__ import annotations

import json
import os
from uuid import UUID

import pytest

from app.tests.utils.context import current_context
from app.tests.utils.deterministic import is_uuid7


def test_settings_fixture_is_isolated(settings) -> None:
    assert settings.env == "test"
    assert settings.app_name == "inventory-platform-test"
    assert "change-me" not in settings.jwt_secret
    assert settings.database_url.startswith("postgresql+asyncpg://")
    assert settings.public_dict()["env"] == "test"


def test_env_is_forced_to_test() -> None:
    assert os.environ["ENV"] == "test"


def test_tenant_and_principal_fixtures(tenant, tenant_id, principal) -> None:
    assert isinstance(tenant_id, UUID)
    assert is_uuid7(tenant_id)
    assert principal.tenant_id == tenant_id
    assert principal.has_scope("items.read")
    assert principal.email.endswith(".example.test")


def test_rbac_catalogue_fixture_matches_authoritative_counts(rbac_catalogue) -> None:
    assert len(rbac_catalogue["catalogue"]) == 255
    assert len(rbac_catalogue["role_templates"]) == 7
    assert len(rbac_catalogue["sod_pairs"]) == 14


def test_principal_factory_binds_tenant(principal_factory) -> None:
    buyer = principal_factory(role="buyer", index=5)
    assert buyer.roles == ("buyer",)
    assert buyer.has_scope("procurement.po.write")
    assert not buyer.has_scope("procurement.po.approve")


def test_tenant_context_fixture_binds_and_clears(principal, tenant_context) -> None:
    assert current_context() is tenant_context
    assert tenant_context.tenant_id == principal.tenant_id
    assert tenant_context.has_scope("platform.users.manage")


def test_auth_headers_placeholder(principal, auth_headers) -> None:
    assert auth_headers["Authorization"].startswith("Bearer test-token:")
    assert auth_headers["X-Test-Tenant-Id"] == str(principal.tenant_id)


def test_sync_client_health(sync_client) -> None:
    response = sync_client.get("/healthz")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["env"] == "test"


async def test_async_client_health(client) -> None:
    response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_deterministic_fixtures(clock, fixed_now, uuids, uuid7_factory) -> None:
    assert clock.now() == fixed_now
    assert is_uuid7(uuids.next())
    assert is_uuid7(uuid7_factory(fixed_now))


def test_app_instance_factory(app_instance) -> None:
    assert app_instance.title == "Inventory Platform"


def test_db_config_fixture_reports_profile(db_config) -> None:
    from app.tests.utils.db import DB_PROFILES

    assert db_config.profile in DB_PROFILES
    assert db_config.available == (db_config.profile != "skip")
    if not db_config.available:
        assert db_config.reason


@pytest.mark.integration
async def test_db_session_rolls_back(db_session) -> None:
    """Executes only when a real Postgres URL/container is available (skipped otherwise)."""
    sqlalchemy = pytest.importorskip("sqlalchemy")
    result = await db_session.execute(sqlalchemy.text("SELECT 1"))
    assert result.scalar_one() == 1


def test_fixtures_json_available() -> None:
    from app.tests.utils.factories import FIXTURES_DIR

    assert json.loads((FIXTURES_DIR / "permissions.json").read_text())["schema_version"]
