"""Integration: fresh/existing tenant provisioning clones sequences/settings/flags + count approval."""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select, text

from app.core.models import platform as p
from app.core.tenant_provisioning import (
    backfill_tenant_defaults,
    expected_default_counts,
    provision_tenant_defaults,
)
from app.modules.counts.approvals import submit_count_request

pytestmark = pytest.mark.integration


async def _seed_tenant(db_session, code: str) -> UUID:
    tenant_id = uuid4()
    await db_session.execute(
        p.tenant.insert().values(
            id=tenant_id, tenant_id=tenant_id, code=code, name=f"Prov {code}",
            base_currency_code="USD", timezone="UTC", status="ACTIVE",
            negative_inventory_policy="BLOCK", rounding_mode="PER_MOVEMENT",
            retention_ledger_years=7, settings={}, version=1,
        )
    )
    return tenant_id


async def _seed_member(db_session, tenant_id: UUID) -> UUID:
    identity_id = uuid4()
    app_user_id = uuid4()
    await db_session.execute(
        text("INSERT INTO platform.user_identity (id, email, display_name, status, created_at, updated_at, version) "
             "VALUES (:id, :email, 'Provision Tester', 'ACTIVE', now(), now(), 1)"),
        {"id": identity_id, "email": f"prov-{identity_id}@example.test"},
    )
    await db_session.execute(
        p.app_user.insert().values(
            id=app_user_id, tenant_id=tenant_id, identity_id=identity_id,
            email=f"prov-{identity_id}@example.test", display_name="Provision Tester",
            status="ACTIVE", membership_status="ACTIVE", token_version=0, is_service_account=False,
        )
    )
    return app_user_id


async def _counts(db_session, tenant_id: UUID) -> dict[str, int]:
    sequences = await db_session.scalar(select(func.count()).select_from(p.number_sequence).where(p.number_sequence.c.tenant_id == tenant_id))
    settings = await db_session.scalar(select(func.count()).select_from(p.tenant_settings).where(p.tenant_settings.c.tenant_id == tenant_id))
    flags = await db_session.scalar(select(func.count()).select_from(p.feature_flag).where(p.feature_flag.c.tenant_id == tenant_id))
    approvals = await db_session.scalar(select(func.count()).select_from(p.approval_definition).where(p.approval_definition.c.tenant_id == tenant_id))
    return {"sequences": int(sequences), "settings": int(settings), "flags": int(flags), "approval_definitions": int(approvals)}


async def test_fresh_tenant_clones_all_defaults_and_replay_is_idempotent(db_session) -> None:
    tenant_id = await _seed_tenant(db_session, f"PROV-{uuid4().hex[:6]}")
    first = await provision_tenant_defaults(db_session, tenant_id)
    assert first == expected_default_counts()
    assert await _counts(db_session, tenant_id) == expected_default_counts()

    replay = await provision_tenant_defaults(db_session, tenant_id)
    assert replay == {"sequences": 0, "settings": 0, "flags": 0, "approval_definitions": 0}
    assert await _counts(db_session, tenant_id) == expected_default_counts()

    flags = (await db_session.execute(select(p.feature_flag.c.is_enabled).where(p.feature_flag.c.tenant_id == tenant_id))).scalars().all()
    assert flags and not any(flags)
    definition = (await db_session.execute(
        select(p.approval_definition).where(
            p.approval_definition.c.tenant_id == tenant_id,
            p.approval_definition.c.document_type == "INVENTORY_COUNT",
        )
    )).mappings().one()
    assert definition["is_enabled"] is True
    assert len(definition["steps"]) == 1
    setting_keys = set((await db_session.execute(
        select(p.tenant_settings.c.namespace, p.tenant_settings.c.key).where(p.tenant_settings.c.tenant_id == tenant_id)
    )).all())
    assert ("inventory", "counts.tolerance_qty") in setting_keys
    assert ("manufacturing", "bom.max_depth") in setting_keys
    sequence_types = set((await db_session.execute(
        select(p.number_sequence.c.doc_type).where(p.number_sequence.c.tenant_id == tenant_id)
    )).scalars().all())
    assert {"INSP", "COA", "ASN", "RCV", "MFST", "RPL", "CAG", "CTR", "WR", "WC", "REP"} <= sequence_types


async def test_required_count_submit_uses_provisioned_default_definition(db_session) -> None:
    tenant_id = await _seed_tenant(db_session, f"PROV-{uuid4().hex[:6]}")
    await provision_tenant_defaults(db_session, tenant_id)
    app_user_id = await _seed_member(db_session, tenant_id)
    count = {"id": str(uuid4()), "count_type": "MANUAL", "amount_base": None, "approval_required": True}
    result = await submit_count_request(
        db_session, tenant_id, count=count, requested_by=str(app_user_id),
        context={"count_type": "MANUAL"},
    )
    assert result["approval_request_id"]
    assert result["status"] == "PENDING"
    assert result["definition_id"]
    existing = (await db_session.execute(
        select(p.approval_request.c.id).where(
            p.approval_request.c.tenant_id == tenant_id,
            p.approval_request.c.document_type == "INVENTORY_COUNT",
        )
    )).scalar_one()
    assert str(existing) == result["approval_request_id"]


async def test_backfill_existing_tenant_and_isolation(db_session) -> None:
    existing_tenant = await _seed_tenant(db_session, f"OLD-{uuid4().hex[:6]}")
    other_tenant = await _seed_tenant(db_session, f"OTHER-{uuid4().hex[:6]}")
    counts = await backfill_tenant_defaults(db_session, existing_tenant)
    assert counts == expected_default_counts()
    assert await _counts(db_session, other_tenant) == {"sequences": 0, "settings": 0, "flags": 0, "approval_definitions": 0}
    replay = await backfill_tenant_defaults(db_session, existing_tenant)
    assert all(value == 0 for value in replay.values())
