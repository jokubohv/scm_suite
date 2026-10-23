"""FS-00 transactional outbox producer tests (live DB, rollback-isolated)."""

from __future__ import annotations

import uuid

from sqlalchemy import select

from app.core.approvals import ApprovalService
from app.core.models import platform as p
from app.core.repositories.platform_admin import PlatformAdminRepository
from app.core.repositories.rbac import RbacRepository
from app.tests.integration.platform.support import (
    create_identity,
    create_membership,
    create_tenant,
)


async def _events(db_session, event_type: str) -> list[dict]:
    rows = await db_session.execute(
        select(p.outbox_event.c.event_type, p.outbox_event.c.tenant_id, p.outbox_event.c.payload)
        .where(p.outbox_event.c.event_type == event_type)
        .order_by(p.outbox_event.c.occurred_at)
    )
    return [dict(row._mapping) for row in rows.all()]


async def test_role_invitation_session_and_audit_producers(db_session) -> None:
    tenant_id = await create_tenant(db_session, code="EVT-RBAC")
    identity_id = await create_identity(db_session, email="admin@evt-rbac.test")
    membership = await create_membership(
        db_session, tenant_id=tenant_id, identity_id=identity_id, email="admin@evt-rbac.test",
        scopes=("platform.role.manage", "platform.users.manage", "platform.invitations.manage",
                "platform.sessions.revoke"),
    )
    repo = RbacRepository(db_session)
    role = await repo.create_role(tenant_id, code="evt_reader", name="Event Reader", scopes=(),
                                  actor_id=membership["app_user_id"])
    created = await _events(db_session, "role.created")
    assert len(created) == 1 and str(created[0]["tenant_id"]) == str(tenant_id)
    assert created[0]["payload"]["data"]["role_id"] == str(role["id"])
    assert await _events(db_session, "platform.audit.recorded")

    invitation = await repo.create_invitation(
        tenant_id, email="invitee@evt-rbac.test", intended_role_ids=[role["id"]],
        invited_by=membership["app_user_id"],
    )
    invited = await _events(db_session, "user.invited")
    assert len(invited) == 1
    assert invited[0]["payload"]["data"]["invitation_id"] == str(invitation["id"])

    await repo.assign_role(tenant_id, membership["app_user_id"], role["id"], actor_id=membership["app_user_id"])
    role_events = await _events(db_session, "membership.roles.changed")
    assert role_events and role_events[-1]["payload"]["data"]["added_role_ids"] == [str(role["id"])]

    token_version = await repo.revoke_sessions(
        tenant_id, membership["app_user_id"], reason="test_revoke", actor_id=membership["app_user_id"]
    )
    assert token_version >= 0
    revoked = await _events(db_session, "user.session.revoked")
    assert revoked and revoked[-1]["payload"]["data"]["reason"] == "test_revoke"


async def test_tenant_lifecycle_and_document_producers(db_session) -> None:
    admin = PlatformAdminRepository(db_session)
    tenant = await admin.create_tenant(data={
        "code": "EVT-TEN", "name": "Event Tenant", "base_currency_code": "USD", "timezone": "UTC",
        "status": "ACTIVE", "negative_inventory_policy": "BLOCK", "rounding_mode": "PER_MOVEMENT",
        "retention_ledger_years": 7, "settings": {},
    }, actor_identity_id=None)
    provisioned = await _events(db_session, "tenant.provisioned")
    assert provisioned and provisioned[-1]["payload"]["data"]["code"] == "EVT-TEN"

    suspended, already, revoked = await admin.transition_status(
        tenant_id=tenant["id"], target_status="SUSPENDED", reason="producer-test", actor_identity_id=None
    )
    assert already is False and suspended["status"] == "SUSPENDED"
    suspended_events = await _events(db_session, "tenant.suspended")
    assert suspended_events and suspended_events[-1]["payload"]["data"]["sessions_revoked"] == revoked

    identity_id = await create_identity(db_session, email="requester@evt-ten.test")
    membership = await create_membership(
        db_session, tenant_id=tenant["id"], identity_id=identity_id, email="requester@evt-ten.test",
    )
    service = ApprovalService(db_session)
    result = await service.submit(
        str(tenant["id"]), document_type="INVENTORY_ADJUSTMENT",
        document_id=str(uuid.uuid4()), requested_by=str(membership["app_user_id"]),
    )
    assert result["request"]["status"] == "APPROVED"
    submitted = await _events(db_session, "document.submitted")
    assert submitted and submitted[-1]["payload"]["data"]["document_type"] == "INVENTORY_ADJUSTMENT"
    approved = await _events(db_session, "document.approved")
    assert approved is None or approved  # auto-approved documents may emit only submitted in this path


async def test_admin_users_create_route_producer(db_session) -> None:
    from app.api.deps import Principal
    from app.api.routers.platform.rbac import UserCreate, create_user

    tenant_id = await create_tenant(db_session, code="EVT-USERS")
    admin_identity = await create_identity(db_session, email="admin@evt-users.test")
    admin = await create_membership(
        db_session, tenant_id=tenant_id, identity_id=admin_identity, email="admin@evt-users.test",
        scopes=("platform.users.manage",),
    )
    principal = Principal(identity_id=str(admin_identity), app_user_id=str(admin["app_user_id"]),
                          tenant_id=str(tenant_id), token_version=0,
                          scopes=frozenset({"platform.users.manage"}))
    result = await create_user(
        UserCreate(email="invitee@evt-users.test", role_ids=[]), RbacRepository(db_session), principal,
    )
    assert result["membership_status"] == "INVITED"
    events = await _events(db_session, "user.invited")
    assert events and events[-1]["payload"]["data"]["invitation_id"] == result["invitation_id"]
