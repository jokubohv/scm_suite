"""Own-tenant currency configuration through real API-key auth and PostgreSQL RLS."""

from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import select, text

from app.api import deps
from app.core.models import platform as p
from app.core.repositories.api_keys import derive_api_key_pepper
from app.core.security.tokens import ApiKeyHasher
from app.tests.integration.platform_config.conftest import OTHER_TENANT_ID, TENANT_ID
from app.tests.integration.platform_config.test_currency_api_key_rls import currency_key_client as currency_key_client

PATH = "/api/v1/admin/tenant-configuration"


@pytest.fixture()
async def tenant_config_client(currency_key_client: Any, db_session: Any) -> Any:  # noqa: F811 - reused pytest fixture
    client, keys, role = currency_key_client
    await db_session.execute(text(f"GRANT UPDATE ON platform.tenant TO {role}"))
    for schema in (
        "platform",
        "item_master",
        "inventory_ledger",
        "costing",
        "procurement",
        "manufacturing",
        "consigned",
        "counts",
    ):
        await db_session.execute(text(f"GRANT USAGE ON SCHEMA {schema} TO {role}"))
        await db_session.execute(text(f"GRANT SELECT, UPDATE ON ALL TABLES IN SCHEMA {schema} TO {role}"))
    # Production append-only grants must not be weakened just to acquire table locks.
    await db_session.execute(
        text(f"REVOKE UPDATE ON platform.finance_journal_intent, inventory_ledger.inventory_transaction FROM {role}")
    )
    hasher = ApiKeyHasher(derive_api_key_pepper(deps._settings().jwt_secret))
    for name, tenant_id in (("admin", TENANT_ID), ("other_admin", OTHER_TENANT_ID)):
        material = hasher.generate()
        await db_session.execute(
            p.api_key.insert().values(
                id=uuid4(),
                tenant_id=tenant_id,
                name=name,
                key_prefix=material.key_prefix,
                secret_hash=material.secret_hash,
                scopes=["platform.admin"],
            )
        )
        keys[name] = {"X-API-Key": material.full_key}
    await db_session.execute(p.currency.insert().values(code="BOB", numeric_code=68, minor_units=2))
    return client, keys


async def test_admin_changes_only_own_currency_and_audits(tenant_config_client: Any, db_session: Any) -> None:
    client, keys = tenant_config_client
    before = await client.get(PATH, headers=keys["admin"])
    assert before.status_code == 200, before.text
    assert before.json()["tenant_id"] == str(TENANT_ID)
    assert before.json()["base_currency_code"] == "USD"
    assert before.json()["currency_change_blockers"] == []
    patched = await client.patch(
        PATH, headers={**keys["admin"], "If-Match": before.headers["etag"]}, json={"base_currency_code": "bob"}
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["base_currency_code"] == "BOB"
    assert patched.json()["version"] > before.json()["version"]
    assert (await client.get(PATH, headers=keys["admin"])).json() == patched.json()
    assert (await client.get(PATH, headers=keys["other_admin"])).json()["base_currency_code"] == "USD"
    assert await db_session.scalar(select(p.currency.c.is_active).where(p.currency.c.code == "BOB")) is True
    # A lost response can be reconciled by GET then a same-value PATCH; it does not create another event.
    same = await client.patch(
        PATH, headers={**keys["admin"], "If-Match": patched.headers["etag"]}, json={"base_currency_code": "BOB"}
    )
    assert same.status_code == 200 and same.json() == patched.json(), same.text
    audits = (
        (
            await db_session.execute(
                select(p.audit_event).where(
                    p.audit_event.c.action == "tenant.configuration.updated",
                )
            )
        )
        .mappings()
        .all()
    )
    assert len(audits) == 1
    assert audits[0]["tenant_id"] == TENANT_ID
    assert audits[0]["actor_type"] == "SERVICE"
    assert audits[0]["diff"]["before"] == {"base_currency_code": "USD"}
    assert audits[0]["diff"]["after"] == {"base_currency_code": "BOB"}


async def test_scope_tenant_target_and_platform_controls_are_preserved(tenant_config_client: Any) -> None:
    client, keys = tenant_config_client
    for method in (client.get, client.patch):
        response = await method(PATH, headers=keys["writer"])
        assert response.status_code == 403, response.text
        assert response.json()["code"] == "FORBIDDEN_SCOPE"
    for extra in ({"tenant_id": str(OTHER_TENANT_ID)}, {"status": "SUSPENDED"}, {"settings": {}}):
        denied = await client.patch(
            PATH, headers={**keys["admin"], "If-Match": "1"}, json={"base_currency_code": "BOB", **extra}
        )
        assert denied.status_code == 422, denied.text
    platform = await client.patch(
        f"/api/v1/platform/tenants/{OTHER_TENANT_ID}", headers=keys["admin"], json={"base_currency_code": "BOB"}
    )
    assert platform.status_code == 403, platform.text
    assert (await client.get(PATH, headers=keys["other_admin"])).json()["base_currency_code"] == "USD"


async def test_precondition_and_active_currency_required(tenant_config_client: Any, db_session: Any) -> None:
    client, keys = tenant_config_client
    for version, expected in ((None, 428), ("*", 422), ("0", 422), ("999", 412)):
        headers = keys["admin"] if version is None else {**keys["admin"], "If-Match": version}
        response = await client.patch(PATH, headers=headers, json={"base_currency_code": "BOB"})
        assert response.status_code == expected, response.text
    await db_session.execute(p.currency.update().where(p.currency.c.code == "BOB").values(is_active=False))
    for code in ("BOB", "ZZZ", None):
        response = await client.patch(
            PATH, headers={**keys["admin"], "If-Match": "1"}, json={"base_currency_code": code}
        )
        assert response.status_code == 422, response.text
    assert (await client.get(PATH, headers=keys["admin"])).json()["base_currency_code"] == "USD"


async def test_history_blocks_even_zero_or_reversed_balances(tenant_config_client: Any, db_session: Any) -> None:
    client, keys = tenant_config_client
    await db_session.execute(
        text("""
        INSERT INTO inventory_ledger.inventory_transaction
          (id, tenant_id, transaction_type, transaction_date, ledger_sequence, total_qty_base, total_value_base)
        VALUES (:id, :tenant_id, 'T-05', CURRENT_DATE, 1, 0, 0)
    """),
        {"id": uuid4(), "tenant_id": TENANT_ID},
    )
    before = await client.get(PATH, headers=keys["admin"])
    assert before.status_code == 200, before.text
    assert "inventory_history" in before.json()["currency_change_blockers"]
    blocked = await client.patch(
        PATH, headers={**keys["admin"], "If-Match": before.headers["etag"]}, json={"base_currency_code": "BOB"}
    )
    assert blocked.status_code == 409, blocked.text
    assert blocked.json()["code"] == "BASE_CURRENCY_IN_USE"
    assert (await client.get(PATH, headers=keys["admin"])).json() == before.json()
    # Another tenant's history neither blocks nor leaks into this tenant's configuration.
    other = await client.patch(
        PATH, headers={**keys["other_admin"], "If-Match": "1"}, json={"base_currency_code": "BOB"}
    )
    assert other.status_code == 200, other.text


async def test_unposted_cost_configuration_blocks_change(tenant_config_client: Any, db_session: Any) -> None:
    client, keys = tenant_config_client
    await db_session.execute(
        text("""
        INSERT INTO manufacturing.work_center (id, tenant_id, code, name, cost_per_hour)
        VALUES (:id, :tenant_id, 'LABOR', 'Synthetic labor', 25)
    """),
        {"id": uuid4(), "tenant_id": TENANT_ID},
    )
    response = await client.patch(PATH, headers={**keys["admin"], "If-Match": "1"}, json={"base_currency_code": "BOB"})
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "BASE_CURRENCY_IN_USE"
