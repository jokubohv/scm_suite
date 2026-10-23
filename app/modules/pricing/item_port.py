"""Read-only item-master port used by the pricing engine (FS-01 `item.master`).

Pricing must not import FS-01 ORM/table models. Until the FS-01 item-master contract facade is
published, the default adapter reads the frozen FS-01 read surface with parameterised SQL (read-only:
``item``, ``item_sales``, ``item_matrix_member``, ``item_inventory``, ``item_purchasing``,
``item_category``). The port is the single swap point for the final contract client and can be replaced
via dependency injection in tests / later waves. It never writes FS-01 tables.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.pricing.engine import ItemInfo


@dataclass(frozen=True, slots=True)
class ItemMasterSnapshotRow:
    item_id: UUID
    item_number: str
    display_name: str
    is_active: bool
    item_type_code: str | None
    category_id: UUID | None
    category_code: str | None
    schedule_id: UUID | None
    matrix_parent_id: UUID | None
    matrix_qty_behavior: str
    initial_cost: Decimal | None
    purchase_price: Decimal | None


class ItemMasterPort(Protocol):
    """Contract consumed by pricing; implemented by the FS-01 contract client (or SQL fallback)."""

    async def load_items(self, tenant_id: str | UUID, item_ids: list[UUID]) -> dict[UUID, ItemInfo]: ...

    async def apply_purchase_price_update(
        self,
        tenant_id: str | UUID,
        *,
        item_id: UUID,
        purchase_price: Decimal,
        currency_code: str,
        actor_id: str | UUID | None = None,
    ) -> dict[str, Any]:
        """Delegate purchase-price writes to FS-01 (`item.master`); never write item tables here."""
        ...


class SqlItemMasterPort:
    """Default Phase-1 adapter: read-only SQL over the frozen FS-01 item-master read surface."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def load_items(self, tenant_id: str | UUID, item_ids: list[UUID]) -> dict[UUID, ItemInfo]:
        if not item_ids:
            return {}
        rows = await self.session.execute(
            text(
                """
                SELECT i.id, i.display_name, i.is_active, i.matrix_parent_id, i.item_category_id,
                       i.item_type_id, i.base_uom_id,
                       cat.code AS category_code,
                       s.quantity_pricing_schedule_id, s.matrix_parent_qty_behavior,
                       inv.initial_cost, pur.purchase_price
                  FROM item_master.item i
                  LEFT JOIN item_master.item_sales s
                         ON s.tenant_id = i.tenant_id AND s.item_id = i.id
                  LEFT JOIN item_master.item_inventory inv
                         ON inv.tenant_id = i.tenant_id AND inv.item_id = i.id
                  LEFT JOIN item_master.item_purchasing pur
                         ON pur.tenant_id = i.tenant_id AND pur.item_id = i.id
                  LEFT JOIN item_master.item_category cat
                         ON cat.tenant_id = i.tenant_id AND cat.id = i.item_category_id
                 WHERE i.tenant_id = :tenant_id AND i.id = ANY(:item_ids)
                """
            ),
            {"tenant_id": str(tenant_id), "item_ids": [str(item_id) for item_id in item_ids]},
        )
        result: dict[UUID, ItemInfo] = {}
        for row in rows.mappings().all():
            item_id = UUID(str(row["id"]))
            result[item_id] = ItemInfo(
                item_id=item_id,
                is_active=bool(row["is_active"]),
                category_id=UUID(str(row["item_category_id"])) if row["item_category_id"] else None,
                schedule_id=(
                    UUID(str(row["quantity_pricing_schedule_id"])) if row["quantity_pricing_schedule_id"] else None
                ),
                matrix_parent_id=UUID(str(row["matrix_parent_id"])) if row["matrix_parent_id"] else None,
                qty_behavior=str(row["matrix_parent_qty_behavior"] or "LINE"),
                initial_cost=Decimal(str(row["initial_cost"])) if row["initial_cost"] is not None else None,
                purchase_price=Decimal(str(row["purchase_price"])) if row["purchase_price"] is not None else None,
            )
        return result

    async def apply_purchase_price_update(
        self,
        tenant_id: str | UUID,
        *,
        item_id: UUID,
        purchase_price: Decimal,
        currency_code: str,
        actor_id: str | UUID | None = None,
    ) -> dict[str, Any]:
        raise NotImplementedError(
            "purchase-price writes are owned by FS-01 (item.master.apply_purchase_price_update); "
            "the FS-01 contract client is not wired yet — the pricing job records the rows as rejected"
        )


__all__ = ["ItemMasterPort", "ItemMasterSnapshotRow", "SqlItemMasterPort"]
