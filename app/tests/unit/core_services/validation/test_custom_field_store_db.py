"""Live-DB tests for custom-field value storage/validation (graceful skip without a database)."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.core import custom_fields as cf
from app.core.models import platform as p

pytestmark = pytest.mark.integration


async def _seed_tenant(db_session: Any) -> str:
    tenant_id = uuid4()
    await db_session.execute(
        p.tenant.insert().values(
            id=tenant_id,
            tenant_id=tenant_id,
            code=f"CF{uuid4().hex[:10].upper()}",
            name="Custom Field Tenant",
            base_currency_code="USD",
            timezone="UTC",
            status="ACTIVE",
            negative_inventory_policy="BLOCK",
            rounding_mode="PER_MOVEMENT",
            retention_ledger_years=7,
            settings={},
            version=1,
        )
    )
    await db_session.flush()
    return str(tenant_id)


async def _seed_def(
    db_session: Any,
    tenant_id: str,
    *,
    entity_type: str = "ITEM",
    field_code: str,
    data_type: str = "TEXT",
    options: list[Any] | None = None,
    validation: Mapping[str, Any] | None = None,
    is_required: bool = False,
) -> str:
    field_id = uuid4()
    await db_session.execute(
        p.custom_field_def.insert().values(
            id=field_id,
            tenant_id=tenant_id,
            entity_type=entity_type,
            field_code=field_code,
            label=field_code.replace("_", " ").title(),
            data_type=data_type,
            options=options,
            is_required=is_required,
            is_searchable=False,
            validation=dict(validation) if validation is not None else None,
            version=1,
        )
    )
    await db_session.flush()
    return str(field_id)


async def _value_rows(db_session: Any, tenant_id: str, entity_id: str) -> list[Any]:
    result = await db_session.execute(
        select(p.custom_field_value).where(
            p.custom_field_value.c.tenant_id == tenant_id,
            p.custom_field_value.c.entity_id == entity_id,
        )
    )
    return list(result.mappings().all())


async def test_typed_upsert_read_update_and_clear(db_session: Any) -> None:
    tenant_id = await _seed_tenant(db_session)
    entity_id = str(uuid4())
    await _seed_def(db_session, tenant_id, field_code="note", data_type="TEXT", validation={"min_length": 2})
    await _seed_def(db_session, tenant_id, field_code="warranty", data_type="NUMBER", validation={"min": 0, "max": 120})
    await _seed_def(db_session, tenant_id, field_code="mfg_date", data_type="DATE")
    await _seed_def(db_session, tenant_id, field_code="fragile", data_type="BOOL")
    await _seed_def(db_session, tenant_id, field_code="coating", data_type="SELECT", options=["painted", "raw"])

    stored = await cf.validate_and_store(
        db_session,
        tenant_id=tenant_id,
        entity_type="item",
        entity_id=entity_id,
        values={
            "note": "hello",
            "warranty": "24",
            "mfg_date": "2026-10-01",
            "fragile": "true",
            "coating": "painted",
        },
    )
    assert {item.field_code for item in stored} == {"note", "warranty", "mfg_date", "fragile", "coating"}

    values = await cf.read_values(db_session, tenant_id=tenant_id, entity_type="ITEM", entity_id=entity_id)
    by_code = {item.field_code: item.value for item in values}
    assert by_code["note"] == "hello"
    assert by_code["warranty"] == Decimal("24")
    assert by_code["mfg_date"] == date(2026, 10, 1)
    assert by_code["fragile"] is True
    assert by_code["coating"] == "painted"

    # Upsert updates in place (one row per field/entity) and bumps version.
    await cf.validate_and_store(
        db_session, tenant_id=tenant_id, entity_type="ITEM", entity_id=entity_id, values={"note": "updated"}
    )
    await _value_rows(db_session, tenant_id, entity_id)
    count = await db_session.execute(
        select(func.count())
        .select_from(p.custom_field_value)
        .where(
            p.custom_field_value.c.tenant_id == tenant_id,
            p.custom_field_value.c.entity_id == entity_id,
        )
    )
    assert int(count.scalar_one()) == 5
    single = await cf.read_value(
        db_session, tenant_id=tenant_id, entity_type="ITEM", entity_id=entity_id, field_code="note"
    )
    assert single is not None and single.value == "updated"

    # None clears the field.
    await cf.validate_and_store(
        db_session, tenant_id=tenant_id, entity_type="ITEM", entity_id=entity_id, values={"note": None}
    )
    assert (
        await cf.read_value(db_session, tenant_id=tenant_id, entity_type="ITEM", entity_id=entity_id, field_code="note")
        is None
    )


async def test_tenant_isolation(db_session: Any) -> None:
    tenant_a = await _seed_tenant(db_session)
    tenant_b = await _seed_tenant(db_session)
    entity_id = str(uuid4())
    await _seed_def(db_session, tenant_a, field_code="serial_grade")
    await _seed_def(db_session, tenant_b, field_code="serial_grade", data_type="TEXT")

    await cf.validate_and_store(
        db_session, tenant_id=tenant_a, entity_type="ITEM", entity_id=entity_id, values={"serial_grade": "A"}
    )
    assert await cf.read_values(db_session, tenant_id=tenant_b, entity_type="ITEM", entity_id=entity_id) == []
    assert [
        item.value
        for item in await cf.read_values(db_session, tenant_id=tenant_a, entity_type="ITEM", entity_id=entity_id)
    ] == ["A"]

    # Field defs are tenant scoped too: tenant B has the same code, so cross-tenant field ids never leak.
    defs_b = await cf.list_definitions(db_session, tenant_id=tenant_b, entity_type="ITEM")
    assert [d.field_code for d in defs_b] == ["serial_grade"]
    with pytest.raises(cf.CustomFieldUnknownError):
        await cf.validate_and_store(
            db_session,
            tenant_id=tenant_a,
            entity_type="ITEM",
            entity_id=entity_id,
            values={"not_a_field": "x"},
        )


async def test_required_and_complete_payload(db_session: Any) -> None:
    tenant_id = await _seed_tenant(db_session)
    entity_id = str(uuid4())
    await _seed_def(db_session, tenant_id, field_code="grade", is_required=True)

    with pytest.raises(cf.CustomFieldRequiredError) as excinfo:
        await cf.validate_and_store(
            db_session,
            tenant_id=tenant_id,
            entity_type="ITEM",
            entity_id=entity_id,
            values={},
            require_complete=True,
        )
    assert excinfo.value.details["missing"] == ["grade"]

    stored = await cf.validate_and_store(
        db_session,
        tenant_id=tenant_id,
        entity_type="ITEM",
        entity_id=entity_id,
        values={"grade": "A"},
        require_complete=True,
    )
    assert [item.value for item in stored] == ["A"]


async def test_validation_rules_and_type_mismatch(db_session: Any) -> None:
    tenant_id = await _seed_tenant(db_session)
    entity_id = str(uuid4())
    await _seed_def(db_session, tenant_id, field_code="warranty", data_type="NUMBER", validation={"min": 0, "max": 24})
    await _seed_def(db_session, tenant_id, field_code="code", data_type="TEXT", validation={"regex": "[A-Z]{2}"})

    with pytest.raises(cf.CustomFieldValidationError):
        await cf.validate_and_store(
            db_session, tenant_id=tenant_id, entity_type="ITEM", entity_id=entity_id, values={"warranty": "25"}
        )
    with pytest.raises(cf.CustomFieldValidationError):
        await cf.validate_and_store(
            db_session, tenant_id=tenant_id, entity_type="ITEM", entity_id=entity_id, values={"code": "ab"}
        )
    with pytest.raises(cf.CustomFieldTypeMismatchError):
        await cf.validate_and_store(
            db_session, tenant_id=tenant_id, entity_type="ITEM", entity_id=entity_id, values={"warranty": "abc"}
        )
    with pytest.raises(cf.UnknownEntityTypeError):
        await cf.validate_and_store(
            db_session, tenant_id=tenant_id, entity_type="CUSTOMER", entity_id=entity_id, values={}
        )


async def test_audit_hook_receives_typed_changes(db_session: Any) -> None:
    tenant_id = await _seed_tenant(db_session)
    entity_id = str(uuid4())
    await _seed_def(db_session, tenant_id, field_code="grade")
    events: list[cf.CustomFieldAuditEvent] = []

    async def _hook(event: cf.CustomFieldAuditEvent) -> None:
        events.append(event)

    await cf.validate_and_store(
        db_session,
        tenant_id=tenant_id,
        entity_type="ITEM",
        entity_id=entity_id,
        values={"grade": "A"},
        audit=_hook,
        actor_id=str(uuid4()),
    )
    await cf.validate_and_store(
        db_session, tenant_id=tenant_id, entity_type="ITEM", entity_id=entity_id, values={"grade": None}, audit=_hook
    )
    assert len(events) == 2
    assert events[0].changes[0].action == "SET"
    assert events[0].changes[0].after == "A"
    assert events[1].changes[0].action == "CLEAR"
    assert events[1].changes[0].before == "A"

    async def _failing_hook(_: cf.CustomFieldAuditEvent) -> None:
        raise RuntimeError("audit failed")

    with pytest.raises(RuntimeError):
        await cf.validate_and_store(
            db_session,
            tenant_id=tenant_id,
            entity_type="ITEM",
            entity_id=entity_id,
            values={"grade": "B"},
            audit=_failing_hook,
        )


async def test_usage_and_malformed_definition_fail_closed(db_session: Any) -> None:
    tenant_id = await _seed_tenant(db_session)
    field_id = uuid4()
    await db_session.execute(
        p.custom_field_def.insert().values(
            id=field_id,
            tenant_id=tenant_id,
            entity_type="ITEM",
            field_code="grade",
            label="Grade",
            data_type="TEXT",
            options=None,
            is_required=False,
            is_searchable=False,
            validation=None,
            version=1,
        )
    )
    await db_session.flush()
    for _ in range(2):
        await cf.validate_and_store(
            db_session,
            tenant_id=tenant_id,
            entity_type="ITEM",
            entity_id=str(uuid4()),
            values={"grade": "A"},
        )
    summary = await cf.usage(db_session, tenant_id=tenant_id, field_def_id=field_id)
    assert summary["value_count"] == 2
    assert summary["data_type"] == "TEXT"

    broken_id = uuid4()
    await db_session.execute(
        p.custom_field_def.insert().values(
            id=broken_id,
            tenant_id=tenant_id,
            entity_type="ITEM",
            field_code="broken",
            label="Broken",
            data_type="TEXT",
            options=None,
            is_required=False,
            is_searchable=False,
            validation={"unsupported_rule": 1},
            version=1,
        )
    )
    await db_session.flush()
    with pytest.raises(cf.CustomFieldDefinitionError):
        await cf.validate_and_store(
            db_session,
            tenant_id=tenant_id,
            entity_type="ITEM",
            entity_id=str(uuid4()),
            values={"broken": "x"},
        )
    # Definitions are read-only for this service: the def row is unchanged by value writes.
    stored = await db_session.execute(
        select(p.custom_field_def.c.validation).where(p.custom_field_def.c.id == broken_id)
    )
    assert stored.scalar_one() == {"unsupported_rule": 1}
