"""`item.master@1.0.0` in-process service facade (FS-01 §3).

Dependants call these async functions with their own ``AsyncSession`` + ``tenant_id``; every call is
tenant-scoped and goes through :class:`ItemMasterService` (no cross-module SQL writes). Version 1.0.0 is
additive: FIX-1 planning-values write path and UPC mirror are included without a bump.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.item_master.errors import ItemInactive, ItemMasterError, ItemNotFound, item_error
from app.modules.item_master.service import ItemMasterService
from app.modules.item_master.uom import UomError
from app.modules.item_master.uom import convert_quantity as _convert_decimal

CONTRACT_VERSION = "1.0.0"


def _service(session: AsyncSession, tenant_id: uuid.UUID | str) -> ItemMasterService:
    return ItemMasterService(session, tenant_id)


async def get_item(session: AsyncSession, tenant_id: uuid.UUID | str, item_id: uuid.UUID | str) -> dict[str, Any]:
    return await _service(session, tenant_id).get_item(item_id)


async def require_active_item(session: AsyncSession, tenant_id: uuid.UUID | str, item_id: uuid.UUID | str) -> dict[str, Any]:
    item = await get_item(session, tenant_id, item_id)
    if not item.get("is_active"):
        raise ItemInactive("item is inactive")
    return item


async def posting_eligibility(session: AsyncSession, tenant_id: uuid.UUID | str, item_id: uuid.UUID | str, operation: str) -> dict[str, Any]:
    return await _service(session, tenant_id).posting_eligibility(item_id, operation)


async def base_uom(session: AsyncSession, tenant_id: uuid.UUID | str, item_id: uuid.UUID | str) -> dict[str, Any] | None:
    service = _service(session, tenant_id)
    row = await service._one(
        "SELECT u.id, u.code, u.name FROM item_master.item i JOIN item_master.uom u "
        "ON u.tenant_id = i.tenant_id AND u.id = i.base_uom_id WHERE i.tenant_id = :tenant_id AND i.id = :id",
        id=uuid.UUID(str(item_id)),
    )
    return dict(row) if row else None


async def convert_quantity(
    session: AsyncSession,
    tenant_id: uuid.UUID | str,
    item_id: uuid.UUID | str,
    quantity: Any,
    from_uom_id: uuid.UUID | str,
    to_uom_id: uuid.UUID | str | None = None,
) -> Decimal:
    """Normalise ``quantity`` from one item UoM to the base (or another same-type UoM), 12-dp."""
    service = _service(session, tenant_id)
    item = await service._one("SELECT id, base_uom_id, item_type_id FROM item_master.item WHERE tenant_id = :tenant_id AND id = :id", id=uuid.UUID(str(item_id)))
    if item is None:
        raise ItemNotFound("item not found")
    from_factor = await service._uom_factor(uuid.UUID(str(from_uom_id)))
    to_factor = await service._uom_factor(uuid.UUID(str(to_uom_id))) if to_uom_id else None
    if to_factor is not None and to_factor["uom_type_id"] != from_factor["uom_type_id"]:
        raise ItemMasterError("UOM_TYPE_MISMATCH", code="UOM_TYPE_MISMATCH")
    try:
        result = _convert_decimal(quantity, from_uom=from_factor["factor"], to_uom=to_factor["factor"] if to_factor else None)
    except UomError as exc:
        raise ItemMasterError(str(exc), code=exc.code) from exc
    return result.quantity_base if to_factor is None else quantize_result(result.quantity_to)


def quantize_result(value: Decimal | None) -> Decimal:
    from app.modules.item_master.uom import quantize_quantity

    return quantize_quantity(value or Decimal("0"))


async def get_item_location_settings(
    session: AsyncSession, tenant_id: uuid.UUID | str, item_id: uuid.UUID | str, location_id: uuid.UUID | str
) -> dict[str, Any] | None:
    rows = await _service(session, tenant_id).list_locations(item_id, location_id=str(location_id))
    return rows[0] if rows else None


async def apply_purchase_price_update(
    session: AsyncSession,
    tenant_id: uuid.UUID | str,
    item_ids: Sequence[uuid.UUID | str],
    unit_price: Any,
    currency_code: str,
    effective_date: datetime | None = None,
) -> list[dict[str, Any]]:
    service = _service(session, tenant_id)
    results: list[dict[str, Any]] = []
    price = Decimal(str(unit_price))
    for item_id in item_ids:
        item = await service._one("SELECT id, is_active FROM item_master.item WHERE tenant_id = :tenant_id AND id = :id", id=uuid.UUID(str(item_id)))
        if item is None or not item.get("is_active"):
            results.append({"item_id": str(item_id), "applied": False, "reason": "ITEM_NOT_FOUND_OR_INACTIVE"})
            continue
        await service._exec(
            "UPDATE item_master.item_purchasing SET purchase_price = :price, purchase_price_currency = :currency, updated_at = now(), version = version + 1 "
            "WHERE tenant_id = :tenant_id AND item_id = :item",
            item=uuid.UUID(str(item_id)), price=price, currency=currency_code,
        )
        await service._emit("item.updated", item_id=item_id, payload={"item_id": str(item_id), "changed_fields": ["purchase_price"], "version": 1, "is_active": True})
        results.append({"item_id": str(item_id), "applied": True, "purchase_price": format(price, "f"), "currency_code": currency_code, "effective_date": (effective_date.isoformat() if effective_date else None)})
    return results


async def validate_item_references(session: AsyncSession, tenant_id: uuid.UUID | str) -> dict[str, Any]:
    service = _service(session, tenant_id)
    report = await service._one(
        """
        SELECT
            (SELECT count(*) FROM item_master.item i WHERE i.tenant_id = :tenant_id AND i.is_active AND i.item_type_id IN
                (SELECT id FROM item_master.item_type_definition WHERE tenant_id = :tenant_id AND can_stock)
                AND NOT EXISTS (SELECT 1 FROM item_master.item_inventory inv WHERE inv.tenant_id = i.tenant_id AND inv.item_id = i.id)) AS stockable_without_inventory,
            (SELECT count(*) FROM item_master.item i WHERE i.tenant_id = :tenant_id AND i.matrix_parent_id IS NOT NULL
                AND NOT EXISTS (SELECT 1 FROM item_master.item_matrix_member m WHERE m.tenant_id = i.tenant_id AND m.child_item_id = i.id)) AS child_without_member,
            (SELECT count(*) FROM item_master.item_location l WHERE l.tenant_id = :tenant_id AND l.location_id IS NULL) AS null_location_refs,
            (SELECT count(*) FROM item_master.item_purchasing p WHERE p.tenant_id = :tenant_id AND p.preferred_vendor_id IS NULL) AS null_vendor_refs
        """
    )
    return {"tenant_id": str(tenant_id), "ready": True, **(report or {})}


async def apply_planning_values(
    session: AsyncSession,
    tenant_id: uuid.UUID | str,
    item_id: uuid.UUID | str,
    values: Mapping[str, Any],
    *,
    location_id: uuid.UUID | str | None = None,
    source: str = "MANUAL",
    policy_version: str | None = None,
    effective_at: datetime | None = None,
    expected_version: int | None = None,
    actor_id: str | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    return await _service(session, tenant_id).apply_planning_values(
        item_id,
        location_id=str(location_id) if location_id else None,
        values=values,
        source=source,
        policy_version=policy_version,
        effective_at=effective_at,
        expected_version=expected_version,
        actor_id=actor_id,
        idempotency_key=idempotency_key,
    )


async def get_kit_members(session: AsyncSession, tenant_id: uuid.UUID | str, item_id: uuid.UUID | str) -> list[dict[str, Any]]:
    return (await _service(session, tenant_id).list_kit_members(item_id))["members"]


async def mirror_upc_code(
    session: AsyncSession, tenant_id: uuid.UUID | str, item_id: uuid.UUID | str, upc_code: str, source: str = "FS14"
) -> dict[str, Any]:
    service = _service(session, tenant_id)
    await require_active_item(session, tenant_id, item_id)
    existing = await service._one("SELECT id FROM item_master.item WHERE tenant_id = :tenant_id AND upc_code = :upc AND id <> :id", upc=upc_code, id=uuid.UUID(str(item_id)))
    if existing is not None:
        raise item_error("UPC code already assigned", code="UPC_MISMATCH", http_status=409)
    await service._exec("UPDATE item_master.item SET upc_code = :upc, updated_at = now(), version = version + 1 WHERE tenant_id = :tenant_id AND id = :id", id=uuid.UUID(str(item_id)), upc=upc_code)
    await service._emit("item.updated", item_id=item_id, payload={"item_id": str(item_id), "changed_fields": ["upc_code"], "version": 1, "is_active": True})
    return {"item_id": str(item_id), "upc_code": upc_code, "source": source}


__all__ = [
    "CONTRACT_VERSION",
    "apply_planning_values",
    "apply_purchase_price_update",
    "base_uom",
    "convert_quantity",
    "get_item",
    "get_item_location_settings",
    "get_kit_members",
    "mirror_upc_code",
    "posting_eligibility",
    "require_active_item",
    "validate_item_references",
]
