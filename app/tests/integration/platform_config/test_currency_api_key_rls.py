"""Currency API-key journey with real authentication, SQL and an RLS-constrained role.

Reuse the rollback harness for isolation. Only session plumbing is replaced: key lookup uses
the privileged fixture session; handlers assume a temporary non-owner role with app privileges.
No principal, permission, repository or PostgreSQL policy is mocked.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any
from uuid import uuid4

import httpx
import pytest
from fastapi import Depends, Request
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import deps
from app.api.main import create_app
from app.core.models import platform as p
from app.core.repositories.api_keys import derive_api_key_pepper
from app.core.security.tokens import ApiKeyHasher
from app.tests.integration.platform_config.conftest import OTHER_TENANT_ID, TENANT_ID

PREFIX = "/api/v1/admin/currencies"


@pytest.fixture()
async def currency_key_client(db_session: Any, seeded: Any, monkeypatch: Any) -> AsyncIterator[Any]:
    role = f"currency_test_{uuid4().hex}"
    await db_session.execute(text(f"CREATE ROLE {role} NOLOGIN NOINHERIT NOSUPERUSER NOBYPASSRLS"))
    await db_session.execute(text(f"GRANT USAGE ON SCHEMA platform TO {role}"))
    await db_session.execute(text(f"GRANT SELECT, INSERT, UPDATE ON platform.currency TO {role}"))
    await db_session.execute(text(f"GRANT SELECT ON platform.tenant, platform.audit_event TO {role}"))
    await db_session.execute(text(f"GRANT INSERT ON platform.audit_event TO {role}"))
    hasher = ApiKeyHasher(derive_api_key_pepper(deps._settings().jwt_secret))
    keys = {}
    for name, tenant_id, scopes in (
        ("writer", TENANT_ID, ["platform.reference.read", "platform.finance.sync"]),
        ("reader", OTHER_TENANT_ID, ["platform.reference.read"]),
    ):
        material = hasher.generate()
        await db_session.execute(p.api_key.insert().values(
            id=uuid4(), tenant_id=tenant_id, name=name, key_prefix=material.key_prefix,
            secret_hash=material.secret_hash, scopes=scopes,
        ))
        keys[name] = {"X-API-Key": material.full_key}

    @asynccontextmanager
    async def lookup_session() -> AsyncIterator[Any]:
        yield db_session

    async def request_session() -> AsyncIterator[Any]:
        try:
            async with db_session.begin_nested():
                yield db_session
        finally:
            await db_session.execute(text("RESET ROLE"))

    async def restricted_request_db(
        request: Request,
        db: Annotated[AsyncSession, Depends(deps.get_db)],
        auth: Annotated[deps.AuthContext | None, Depends(deps.get_request_auth)],
    ) -> AsyncSession:
        await deps.get_request_db(request, db, auth)
        await db.execute(text(f"SET LOCAL ROLE {role}"))
        assert await db.scalar(text("SELECT row_security_active('platform.currency')"))
        return db

    monkeypatch.setattr(deps, "session_scope", lookup_session)
    app = create_app()
    app.dependency_overrides[deps.get_db] = request_session
    app.dependency_overrides[deps.get_request_db] = restricted_request_db
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client, keys, role


async def test_api_key_creates_shared_currency_without_changing_base_currency(
    currency_key_client: Any, db_session: Any,
) -> None:
    client, keys, _ = currency_key_client
    created = await client.post(
        PREFIX, json={"code": "BOB", "numeric_code": 68, "minor_units": 2}, headers=keys["writer"],
    )
    assert created.status_code == 201, created.text
    assert created.json()["tenant_scope"] == "GLOBAL"
    fetched = await client.get(f"{PREFIX}/BOB", headers=keys["reader"])
    assert fetched.status_code == 200, fetched.text
    assert fetched.json() == created.json()
    listed = await client.get("/api/v1/reference/currencies", headers=keys["reader"])
    assert listed.status_code == 200, listed.text
    listed_bob = next(row for row in listed.json()["items"] if row["code"] == "BOB")
    assert {field: created.json()[field] for field in listed_bob} == listed_bob

    duplicate = await client.post(PREFIX, json={"code": "BOB"}, headers=keys["writer"])
    assert duplicate.status_code == 409, duplicate.text
    assert duplicate.json()["code"] == "CURRENCY_EXISTS"
    patched = await client.patch(
        f"{PREFIX}/BOB", json={"is_active": False},
        headers={**keys["writer"], "If-Match": str(created.json()["version"])},
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["is_active"] is False
    fetched = await client.get(f"{PREFIX}/BOB", headers=keys["reader"])
    assert fetched.json() == patched.json()

    row = (await db_session.execute(select(p.currency).where(p.currency.c.code == "BOB"))).mappings().one()
    assert row["tenant_id"] is None
    bases = (await db_session.execute(
        select(p.tenant.c.base_currency_code).where(p.tenant.c.id.in_([TENANT_ID, OTHER_TENANT_ID]))
    )).scalars().all()
    assert bases == ["USD", "USD"]
    audits = (await db_session.execute(
        select(p.audit_event).where(p.audit_event.c.resource_type == "currency", p.audit_event.c.resource_id == "BOB")
    )).mappings().all()
    assert {row["action"] for row in audits} == {"platform.currency.created", "platform.currency.updated"}
    assert len(audits) == 2
    assert all(row["tenant_id"] == TENANT_ID for row in audits)


async def test_reference_read_key_cannot_write_global_currency(currency_key_client: Any, db_session: Any) -> None:
    client, keys, _ = currency_key_client
    for response in (
        await client.post(PREFIX, json={"code": "BOB"}, headers=keys["reader"]),
        await client.patch(f"{PREFIX}/USD", json={"is_active": False}, headers=keys["reader"]),
    ):
        assert response.status_code == 403, response.text
        assert response.json()["code"] == "FORBIDDEN_SCOPE"
    assert await db_session.scalar(select(p.currency.c.code).where(p.currency.c.code == "BOB")) is None
    assert await db_session.scalar(select(p.currency.c.is_active).where(p.currency.c.code == "USD")) is True


async def test_currency_write_policy_preserves_tenant_guards(currency_key_client: Any, db_session: Any) -> None:
    _, _, role = currency_key_client
    for context, row_tenant in (("", None), (str(TENANT_ID), OTHER_TENANT_ID)):
        with pytest.raises(DBAPIError) as denied:
            async with db_session.begin_nested():
                await db_session.execute(text(f"SET LOCAL ROLE {role}"))
                await db_session.execute(text("SELECT set_config('app.tenant_id', :tenant_id, true)"), {"tenant_id": context})
                await db_session.execute(p.currency.insert().values(code="BOB", tenant_id=row_tenant))
        # Existing tenant policies cast the context to UUID; empty context also fails closed.
        assert denied.value.orig.sqlstate in {"42501", "22P02"}
    async with db_session.begin_nested():
        await db_session.execute(text(f"SET LOCAL ROLE {role}"))
        await db_session.execute(text("SELECT set_config('app.tenant_id', :tenant_id, true)"), {"tenant_id": str(TENANT_ID)})
        assert await db_session.scalar(select(p.tenant.c.id).where(p.tenant.c.id == OTHER_TENANT_ID)) is None
        await db_session.execute(text("RESET ROLE"))
