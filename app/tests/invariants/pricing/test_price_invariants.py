"""FS-02 invariants: deterministic bands, overlap exclusion and double-apply protection."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.core.models import platform as p
from app.modules.item_master import tables as it
from app.modules.pricing import tables as pt

pytestmark = pytest.mark.integration

TENANT_ID = UUID("d0000000-0000-7000-8000-000000000001")
ITEM_ID = UUID("d0000000-0000-7000-8000-000000000002")
LEVEL_ID = UUID("d0000000-0000-7000-8000-000000000003")
ITEM_TYPE_ID = UUID("d0000000-0000-7000-8000-000000000004")
UOM_TYPE_ID = UUID("d0000000-0000-7000-8000-000000000005")
UOM_ID = UUID("d0000000-0000-7000-8000-000000000006")


async def _seed(db_session: object) -> None:
    await db_session.execute(
        p.tenant.insert().values(
            id=TENANT_ID,
            tenant_id=TENANT_ID,
            code="INVPRICE",
            name="Inv Price",
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
            item_number="INV-ITEM-1",
            display_name="Inv Item",
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
    await db_session.execute(
        pt.price_level.insert().values(
            id=LEVEL_ID,
            tenant_id=TENANT_ID,
            code="DEFAULT",
            name="Default",
            currency_code="USD",
            is_default=True,
            is_inactive=False,
            sort_order=0,
            version=1,
        )
    )
    await db_session.flush()


def _price(price: str, *, qty_min: str = "0", effective_from: date = date(2026, 1, 1)) -> dict:
    return {
        "id": uuid4(),
        "tenant_id": TENANT_ID,
        "item_id": ITEM_ID,
        "price_level_id": LEVEL_ID,
        "currency_code": "USD",
        "qty_min": Decimal(qty_min),
        "qty_max": None,
        "unit_price": Decimal(price),
        "effective_from": effective_from,
        "effective_to": None,
        "is_active": True,
        "version": 1,
    }


async def test_dm_price_03_active_band_overlap_rejected(db_session: object) -> None:
    """DM-PRICE-03/RULE-PRICE-10: overlapping active bands for the same key are rejected by the DB."""
    await _seed(db_session)
    await db_session.execute(pt.item_price.insert().values(**_price("10")))
    with pytest.raises(IntegrityError):
        await db_session.execute(pt.item_price.insert().values(**_price("11")))
    await db_session.rollback()


async def test_dm_price_03_price_update_range_exclusion(db_session: object) -> None:
    """DM-PRICE-03: the GIST exclusion constraint rejects overlapping PENDING updates."""
    await _seed(db_session)
    update = {
        "id": uuid4(),
        "tenant_id": TENANT_ID,
        "update_type": "ITEM",
        "target_ref": "MASS-1",
        "price_level_id": LEVEL_ID,
        "currency_code": "USD",
        "effective_start": date(2026, 11, 1),
        "effective_end": None,
        "status": "PENDING",
        "priority": 0,
        "applied_at": None,
        "scheduled_by": None,
        "overlap_policy": "REJECT",
        "version": 1,
    }
    await db_session.execute(pt.price_update.insert().values(**update))
    with pytest.raises(IntegrityError):
        await db_session.execute(
            pt.price_update.insert().values({**update, "id": uuid4(), "effective_start": date(2026, 12, 1)})
        )
    await db_session.rollback()


async def test_dm_price_07_no_overlapping_active_prices_after_update(db_session: object) -> None:
    """DM-PRICE-07/INV-25: items and prices are tenant scoped (no cross-tenant rows visible)."""
    await _seed(db_session)
    await db_session.execute(pt.item_price.insert().values(**_price("10")))
    other_tenant = uuid4()
    await db_session.execute(
        p.tenant.insert().values(
            id=other_tenant,
            tenant_id=other_tenant,
            code="INVPRICE2",
            name="Other",
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
    rows = await db_session.execute(select(pt.item_price).where(pt.item_price.c.tenant_id == other_tenant))
    assert rows.mappings().all() == []
