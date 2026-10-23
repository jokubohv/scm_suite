"""Fixtures for approval lifecycle integration tests (task-47; live DB, graceful skip)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI

from app.api.deps import Principal, get_current_principal, get_db
from app.api.errors import install_error_handlers
from app.api.routers.platform import approval_actions
from app.core.models import platform as p

TENANT_ID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
REQUESTER_ID = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
APPROVER1_ID = UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")
APPROVER2_ID = UUID("dddddddd-dddd-dddd-dddd-dddddddddddd")
OUTSIDER_ID = UUID("eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee")
APPROVAL_SCOPES = frozenset({"platform.approval.act"})


@pytest.fixture()
async def seeded(db_session: Any) -> dict[str, Any]:
    await db_session.execute(
        p.tenant.insert().values(
            id=TENANT_ID, tenant_id=TENANT_ID, code="APPR", name="Approval Co",
            base_currency_code="USD", timezone="UTC", status="ACTIVE",
            negative_inventory_policy="BLOCK", rounding_mode="PER_MOVEMENT",
        )
    )
    actors = [
        (REQUESTER_ID, "requester@approval.test", "Requester"),
        (APPROVER1_ID, "approver1@approval.test", "Approver One"),
        (APPROVER2_ID, "approver2@approval.test", "Approver Two"),
        (OUTSIDER_ID, "outsider@approval.test", "Outsider"),
    ]
    for user_id, email, display in actors:
        identity_id = uuid4()
        await db_session.execute(
            p.user_identity.insert().values(id=identity_id, email=email, display_name=display, status="ACTIVE")
        )
        await db_session.execute(
            p.app_user.insert().values(
                id=user_id, tenant_id=TENANT_ID, identity_id=identity_id, email=email,
                display_name=display, status="ACTIVE", membership_status="ACTIVE", token_version=0,
            )
        )
    role_ids = {}
    for code in ("approver", "viewer"):
        role_id = uuid4()
        role_ids[code] = role_id
        await db_session.execute(
            p.role.insert().values(
                id=role_id, tenant_id=TENANT_ID, code=code, name=code.title(),
                is_system=False, is_template=False, is_editable=True,
            )
        )
    for user_id in (APPROVER1_ID, APPROVER2_ID):
        await db_session.execute(
            p.user_role.insert().values(tenant_id=TENANT_ID, user_id=user_id, role_id=role_ids["approver"])
        )
    await db_session.execute(
        p.user_role.insert().values(tenant_id=TENANT_ID, user_id=OUTSIDER_ID, role_id=role_ids["viewer"])
    )
    await db_session.flush()
    return {"roles": role_ids}


@pytest.fixture()
async def app_client(db_session: Any, seeded: dict[str, Any]) -> AsyncIterator[Any]:
    import httpx

    app = FastAPI()
    install_error_handlers(app)
    app.include_router(approval_actions.router)

    async def _get_db() -> AsyncIterator[Any]:
        yield db_session

    app.dependency_overrides[get_db] = _get_db
    app.dependency_overrides[get_current_principal] = lambda: principal_for(APPROVER1_ID)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client


def principal_for(app_user_id: UUID) -> Principal:
    return Principal(
        identity_id=str(uuid4()), app_user_id=str(app_user_id), tenant_id=str(TENANT_ID),
        token_version=0, scopes=APPROVAL_SCOPES,
    )


def as_actor(client: Any, app_user_id: UUID) -> None:
    client._transport.app.dependency_overrides[get_current_principal] = lambda: principal_for(app_user_id)


def make_definition_values(*, steps: list[dict[str, Any]], condition: dict[str, Any] | None = None, priority: int = 100) -> dict[str, Any]:
    return {
        "id": uuid4(), "tenant_id": TENANT_ID, "document_type": "PURCHASE_ORDER", "name": f"Rule {uuid4().hex[:6]}",
        "condition": condition or {}, "steps": steps, "is_enabled": True, "priority": priority,
        "created_at": datetime.now(UTC), "updated_at": datetime.now(UTC), "version": 1,
    }


@pytest.fixture()
def definition_factory(db_session: Any):
    async def factory(**kwargs: Any) -> dict[str, Any]:
        values = make_definition_values(**kwargs)
        await db_session.execute(p.approval_definition.insert().values(**values))
        await db_session.flush()
        return values
    return factory
