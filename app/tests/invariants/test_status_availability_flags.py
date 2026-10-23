"""FS-03 invariants: sentinel/status-flag/availability semantics (DM-TRACE-01..04).

Pure invariants run everywhere; database invariants use the shared rollback-isolated ``db_session`` and
skip gracefully when no live Postgres is configured. These are the guarantees FS-04/FS-05 rely on before
committing stock: exactly one UNALLOCATED sentinel per tenant, exactly one owned default status, immutable
system flags, and statuses whose flags are internally consistent.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.core.models import platform as p
from app.modules.item_master import tables as it
from app.modules.traceability import tables as t
from app.modules.traceability.errors import DefaultStatusConflict, ValidationError
from app.modules.traceability.service import (
    LOCATION_TYPES,
    TraceabilityService,
    validate_location_payload,
    validate_status_flags,
)

TENANT_ID = UUID("b0000000-0000-7000-8000-000000000001")
ITEM_ID = UUID("b0000000-0000-7000-8000-000000000002")
ITEM_TYPE_ID = UUID("b0000000-0000-7000-8000-000000000003")
UOM_TYPE_ID = UUID("b0000000-0000-7000-8000-000000000004")
UOM_ID = UUID("b0000000-0000-7000-8000-000000000005")
LOCATION_ID = UUID("b0000000-0000-7000-8000-000000000006")


# --------------------------------------------------------------------------- pure ------------
def test_dm_trace_01_sentinel_payload_is_never_client_creatable() -> None:
    with pytest.raises(ValidationError):
        validate_location_payload(
            {"code": "UNALLOC", "name": "x", "location_type": "UNALLOCATED", "is_unallocated": True}
        )
    with pytest.raises(ValidationError):
        validate_location_payload({"code": "UNALLOC2", "name": "x", "location_type": "UNALLOCATED"})
    assert "UNALLOCATED" in LOCATION_TYPES


def test_dm_trace_03_status_flag_availability_matrix() -> None:
    # committable requires availability unless the tenant explicitly opts into non-available commitment
    with pytest.raises(ValidationError):
        validate_status_flags(
            is_available_for_allocation=False,
            is_committable=True,
            is_sellable=False,
            is_consigned=False,
            is_default=False,
            commit_non_available=False,
        )
    validate_status_flags(
        is_available_for_allocation=False,
        is_committable=True,
        is_sellable=False,
        is_consigned=False,
        is_default=False,
        commit_non_available=True,
    )
    # consigned statuses are never the owned default
    with pytest.raises(DefaultStatusConflict):
        validate_status_flags(
            is_available_for_allocation=True,
            is_committable=True,
            is_sellable=True,
            is_consigned=True,
            is_default=True,
            commit_non_available=False,
        )


def test_dm_trace_04_seed_catalogue_semantics() -> None:
    from app.modules.traceability.repository import STATUS_SEEDS

    by_code = {
        code: (available, committable, sellable, consigned, default, system)
        for code, _name, available, committable, sellable, consigned, default, system in STATUS_SEEDS
    }
    assert set(by_code) == {"AVAILABLE", "HOLD", "DAMAGED", "RETURNED", "CONSIGNED_GOOD", "USED", "REFURB"}
    assert by_code["AVAILABLE"] == (True, True, True, False, True, True)
    assert by_code["CONSIGNED_GOOD"][3] is True and by_code["CONSIGNED_GOOD"][4] is False
    for code in ("USED", "REFURB"):
        available, committable, sellable, consigned, default, system = by_code[code]
        assert available is True and committable is False and sellable is False
        assert consigned is False and default is False and system is False
    for code in ("HOLD", "DAMAGED", "RETURNED"):
        assert by_code[code][0] is False and by_code[code][1] is False


# --------------------------------------------------------------------------- DB --------------
async def _seed(db_session: Any) -> None:
    await db_session.execute(
        p.tenant.insert().values(
            id=TENANT_ID,
            tenant_id=TENANT_ID,
            code="INVTRACE",
            name="Inv Trace",
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
    await db_session.execute(
        p.feature_flag.insert().values(
            id=uuid.uuid4(),
            tenant_id=TENANT_ID,
            flag_key="traceability.registry.enabled",
            is_enabled=True,
            source="TEST",
            version=1,
        )
    )
    await db_session.execute(
        it.item_type_definition.insert().values(
            id=ITEM_TYPE_ID,
            tenant_id=TENANT_ID,
            code="INVENTORY",
            name="Inventory",
            description=None,
            can_stock=True,
            requires_bom=False,
            requires_numbering=False,
            is_charge_only=False,
            sort_order=10,
            is_system=True,
            version=1,
        )
    )
    await db_session.execute(
        it.uom_type.insert().values(
            id=UOM_TYPE_ID,
            tenant_id=TENANT_ID,
            name="Quantity",
            code="QTY",
            base_uom_id=None,
            is_active=True,
            version=1,
        )
    )
    await db_session.execute(
        it.uom.insert().values(
            id=UOM_ID,
            tenant_id=TENANT_ID,
            uom_type_id=UOM_TYPE_ID,
            code="EA",
            name="Each",
            conversion_factor=Decimal("1"),
            is_base=True,
            rounding_precision=4,
            is_active=True,
            version=1,
        )
    )
    await db_session.execute(
        it.item.insert().values(
            id=ITEM_ID,
            tenant_id=TENANT_ID,
            item_number="INV-TRACE-1",
            display_name="Inv Trace",
            description=None,
            item_type_id=ITEM_TYPE_ID,
            is_active=True,
            matrix_parent_id=None,
            base_uom_id=UOM_ID,
            units_type_id=UOM_TYPE_ID,
            item_category_id=None,
            upc_code=None,
            external_ref=None,
            created_from="MANUAL",
            version=1,
        )
    )
    await db_session.flush()


async def _bootstrap(db_session: Any) -> None:
    await _seed(db_session)
    await TraceabilityService(db_session).ensure_bootstrap(TENANT_ID)
    await db_session.flush()
    row = await db_session.execute(
        text("SELECT id FROM traceability.location WHERE tenant_id = :t AND is_unallocated"), {"t": str(TENANT_ID)}
    )
    uuid_val = row.scalar_one()
    global LOCATION_ID
    LOCATION_ID = UUID(str(uuid_val))


async def test_dm_trace_01_exactly_one_sentinel(db_session: Any) -> None:
    await _bootstrap(db_session)
    with pytest.raises(IntegrityError):
        await db_session.execute(
            t.location.insert().values(
                id=uuid.uuid4(),
                tenant_id=TENANT_ID,
                code="SECOND-SENTINEL",
                name="x",
                parent_id=None,
                location_type="UNALLOCATED",
                is_unallocated=True,
                is_inactive=False,
                is_bins_enabled=False,
                is_statuses_enabled=True,
                is_consignment_allowed=False,
                allow_commit_from_non_available=False,
                timezone="UTC",
                version=1,
            )
        )
    await db_session.rollback()


async def test_dm_trace_03_single_default_and_system_flags(db_session: Any) -> None:
    await _bootstrap(db_session)
    statuses = (
        (await db_session.execute(t.inventory_status.select().where(t.inventory_status.c.tenant_id == TENANT_ID)))
        .mappings()
        .all()
    )
    assert [row["code"] for row in statuses if row["is_default"]] == ["AVAILABLE"]
    with pytest.raises(IntegrityError):
        await db_session.execute(
            t.inventory_status.insert().values(
                id=uuid.uuid4(),
                tenant_id=TENANT_ID,
                code="SECOND-DEFAULT",
                name="x",
                is_available_for_allocation=True,
                is_committable=True,
                is_sellable=True,
                is_consigned=False,
                is_default=True,
                is_system=False,
                is_active=True,
                version=1,
            )
        )
    await db_session.rollback()


async def test_dm_trace_02_zone_location_mismatch_rejected(db_session: Any) -> None:
    await _bootstrap(db_session)
    warehouse = uuid.uuid4()
    other = uuid.uuid4()
    for loc_id, code in ((warehouse, "WH-A"), (other, "WH-B")):
        await db_session.execute(
            t.location.insert().values(
                id=loc_id,
                tenant_id=TENANT_ID,
                code=code,
                name=code,
                parent_id=None,
                location_type="WAREHOUSE",
                is_bins_enabled=True,
                is_statuses_enabled=True,
                is_consignment_allowed=False,
                allow_commit_from_non_available=False,
                timezone="UTC",
                is_unallocated=False,
                is_inactive=False,
                version=1,
            )
        )
    await db_session.flush()
    zone_id = uuid.uuid4()
    await db_session.execute(
        t.zone.insert().values(
            id=zone_id,
            tenant_id=TENANT_ID,
            location_id=other,
            code="Z1",
            name="Z1",
            zone_type="STORAGE",
            priority=0,
            is_active=True,
            version=1,
        )
    )
    await db_session.flush()
    with pytest.raises(IntegrityError):
        await db_session.execute(
            t.bin.insert().values(
                id=uuid.uuid4(),
                tenant_id=TENANT_ID,
                location_id=warehouse,
                zone_id=zone_id,
                code="BAD-BIN",
                name="B",
                bin_type="STORAGE",
                is_pickable=False,
                is_active=True,
                version=1,
            )
        )
    await db_session.rollback()


async def test_dm_trace_01_hierarchy_cycle_trigger(db_session: Any) -> None:
    await _bootstrap(db_session)
    parent = uuid.uuid4()
    child = uuid.uuid4()
    await db_session.execute(
        t.location.insert().values(
            id=parent,
            tenant_id=TENANT_ID,
            code="HIER-P",
            name="P",
            parent_id=None,
            location_type="WAREHOUSE",
            is_bins_enabled=False,
            is_statuses_enabled=True,
            is_consignment_allowed=False,
            allow_commit_from_non_available=False,
            timezone="UTC",
            is_unallocated=False,
            is_inactive=False,
            version=1,
        )
    )
    await db_session.execute(
        t.location.insert().values(
            id=child,
            tenant_id=TENANT_ID,
            code="HIER-C",
            name="C",
            parent_id=parent,
            location_type="STORE",
            is_bins_enabled=False,
            is_statuses_enabled=True,
            is_consignment_allowed=False,
            allow_commit_from_non_available=False,
            timezone="UTC",
            is_unallocated=False,
            is_inactive=False,
            version=1,
        )
    )
    await db_session.flush()
    with pytest.raises(IntegrityError):
        await db_session.execute(
            text("UPDATE traceability.location SET parent_id = :child WHERE tenant_id = :tenant AND id = :parent"),
            {"child": str(child), "tenant": str(TENANT_ID), "parent": str(parent)},
        )
    await db_session.rollback()


async def test_deferred_fks_closed_on_item_master(db_session: Any) -> None:
    await _bootstrap(db_session)
    with pytest.raises(IntegrityError):
        await db_session.execute(
            text(
                "INSERT INTO item_master.item_location (tenant_id, item_id, location_id, version) "
                "VALUES (:tenant, :item, :location, 1)"
            ),
            {"tenant": str(TENANT_ID), "item": str(ITEM_ID), "location": str(uuid.uuid4())},
        )
    await db_session.rollback()
    await _bootstrap(db_session)
    await db_session.execute(
        text(
            "INSERT INTO item_master.item_location (tenant_id, item_id, location_id, version) "
            "VALUES (:tenant, :item, :location, 1)"
        ),
        {"tenant": str(TENANT_ID), "item": str(ITEM_ID), "location": str(LOCATION_ID)},
    )
    count = await db_session.execute(
        text("SELECT count(*) FROM item_master.item_location WHERE tenant_id = :tenant AND item_id = :item"),
        {"tenant": str(TENANT_ID), "item": str(ITEM_ID)},
    )
    assert int(count.scalar_one()) == 1  # one valid FS-01 reference accepted by the closed FK
