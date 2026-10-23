"""FS-04 ↔ FS-05 costing adapter (optional injection at the posting engine boundary).

The ledger calls the synchronous, pure `CostingPort` inside its own posting transaction and persists the
returned artefacts through FS-05's `CostingWriter` (same session, no commits). Costing writes stay in
`costing.*`; the ledger never writes FS-05 tables directly. If the FS-05 modules are unavailable the
adapter is simply not constructed (ledger posting remains functional with command-supplied values).
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

MOVEMENT_TYPE_MAP = {
    "RECEIPT": "IN", "VENDOR_RETURN": "OUT", "ADJUSTMENT": "ADJUST", "WORKSHEET": "ADJUST",
    "TRANSFER": "TRANSFER", "TO_FULFILLMENT": "TRANSFER", "TO_RECEIPT": "TRANSFER",
    "FULFILLMENT": "OUT", "RETURN": "IN", "KIT_FULFILLMENT": "OUT", "ASSEMBLY_BUILD": "IN",
    "ASSEMBLY_UNBUILD": "OUT", "WIP_ISSUE": "OUT", "WIP_COMPLETION": "IN", "COUNT_VARIANCE": "ADJUST",
    "CONSIGNED_RECEIPT": "IN", "CONSIGNED_OWNERSHIP": "TRANSFER", "CONSIGNED_ADJUST": "ADJUST",
    "STANDARD_REVALUATION": "REVALUE", "COGS_ADJUST": "COGS_ADJUST", "LANDED_COST": "LANDED",
    "LANDED_COST_EST": "LANDED", "DROP_SHIP": "OUT", "BIN_TRANSFER": "TRANSFER",
    "STATUS_CHANGE": "TRANSFER", "COST_ONLY": "VARIANCE", "REVALUATION": "REVALUE",
    "VARIANCE": "VARIANCE", "BOOTSTRAP_DISTRIBUTION": "IN", "REVERSAL": "ADJUST",
}


class LedgerCostingAdapter:
    def __init__(self, session: AsyncSession, tenant_id: uuid.UUID | str) -> None:
        from app.contracts.costing import CostingPort
        from app.modules.costing.repository import CostingRepository
        from app.modules.costing.service import CostingService

        self.session = session
        self.tenant_id = uuid.UUID(str(tenant_id))
        self.port = CostingPort()
        self.repository = CostingRepository(session, tenant_id)
        self._costing_service = CostingService(session, tenant_id)
        self._settings: Any = None

    async def settings(self):
        if self._settings is None:
            self._settings = await self._costing_service.settings()
        return self._settings

    async def _cost_method(self, item_id: uuid.UUID) -> str:
        result = await self.session.execute(
            text("SELECT costing_method FROM item_master.item_inventory WHERE tenant_id = :tenant AND item_id = :item"),
            {"tenant": self.tenant_id, "item": item_id},
        )
        value = result.scalar_one_or_none()
        return str(value or "AVERAGE").upper()

    async def _cost_object(self, *, item_id: uuid.UUID, location_id: uuid.UUID, lot_id: uuid.UUID | None,
                           serial_id: uuid.UUID | None, costing_method: str | None = None):
        from app.modules.costing.engine import CostObject

        method = costing_method or await self._cost_method(item_id)
        item_cost = await self.repository.get_or_create_item_cost(
            item_id=str(item_id), costing_method=method, location_id=str(location_id) if location_id else None,
            lot_id=str(lot_id) if lot_id else None, serial_id=str(serial_id) if serial_id else None,
        )
        item_cost_id = str(item_cost["id"])
        state, layers = await self.repository.item_cost_state(item_cost_id)
        ctx = CostObject(
            item_id=str(item_id), costing_method=method,
            location_id=str(location_id) if location_id else None,
            lot_id=str(lot_id) if lot_id else None, serial_id=str(serial_id) if serial_id else None,
            item_cost_id=item_cost_id,
        )
        return ctx, state, layers, item_cost_id

    async def assert_line_postable(self, *, item_id: uuid.UUID, location_id: uuid.UUID, transaction_date: date,
                                   lot_id: uuid.UUID | None = None, serial_id: uuid.UUID | None = None) -> None:
        ctx, state, _layers, _item_cost_id = await self._cost_object(item_id=item_id, location_id=location_id, lot_id=lot_id, serial_id=serial_id)
        self.port.assert_postable(ctx, state, await self.settings(), as_of=transaction_date)

    async def value_line(
        self,
        *,
        item_id: uuid.UUID,
        location_id: uuid.UUID,
        qty: Decimal,
        movement_type: str,
        transaction_date: date,
        transaction_id: uuid.UUID,
        line_id: uuid.UUID,
        lot_id: uuid.UUID | None = None,
        serial_id: uuid.UUID | None = None,
        unit_cost: Decimal | None = None,
        value_delta: Decimal | None = None,
        reason: str | None = None,
    ) -> dict[str, Any]:
        from app.modules.costing.engine import ValueMovement

        ctx, state, layers, item_cost_id = await self._cost_object(
            item_id=item_id, location_id=location_id, lot_id=lot_id, serial_id=serial_id
        )
        settings = await self.settings()
        self.port.assert_postable(ctx, state, settings, as_of=transaction_date)
        mapped_type = MOVEMENT_TYPE_MAP.get(str(movement_type).upper())
        if mapped_type is None:
            mapped_type = "IN" if qty >= 0 else "OUT"
        if mapped_type in {"REVALUE", "LANDED", "VARIANCE", "COGS_ADJUST"}:
            if value_delta is None:
                value_delta = Decimal("0")
            movement = ValueMovement(
                item_id=str(item_id), qty_delta=Decimal("0"), movement_type=mapped_type,
                transaction_date=transaction_date, location_id=str(location_id),
                lot_id=str(lot_id) if lot_id else None, serial_id=str(serial_id) if serial_id else None,
                source_line_id=str(line_id), value_delta=value_delta, unit_cost=unit_cost,
                affects_inventory=False, reason=reason,
            )
        else:
            movement = ValueMovement(
                item_id=str(item_id), qty_delta=qty, movement_type=mapped_type,
                transaction_date=transaction_date, location_id=str(location_id),
                lot_id=str(lot_id) if lot_id else None, serial_id=str(serial_id) if serial_id else None,
                source_line_id=str(line_id), unit_cost=unit_cost, value_delta=value_delta,
                affects_inventory=True, reason=reason,
            )
        result = self.port.value_movement(movement, ctx, state, layers, settings)
        persisted = await self.repository.persist_value_result(
            item_cost_id=item_cost_id, transaction_id=str(transaction_id),
            inventory_transaction_line_id=str(line_id), result=result, effective_date=transaction_date,
        )
        return {"unit_cost": result.unit_cost, "value_delta": result.value_delta, "persisted": persisted, "estimated": result.estimated}


__all__ = ["LedgerCostingAdapter"]
