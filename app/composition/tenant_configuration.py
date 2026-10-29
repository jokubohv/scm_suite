"""Valuation dependency adapter for the platform's own-tenant configuration boundary.

Only this adapter knows the participating modules' storage. The platform policy consumes
blocker names, not their tables/services. Existing explicitly denominated prices are untouched.
This setup operation takes brief SHARE/NOWAIT locks on mutable valuation tables. Append-only
history retains its INSERT-only grants: the repository's tenant FOR UPDATE lock conflicts with
the foreign-key KEY SHARE lock of inserts into that history. Busy writers cause a retryable
conflict; no waiting or changes to every posting path are needed. All locks last until commit.
"""

from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.repositories.tenant_configuration import CurrencyChangeBlocker, TenantConfigurationRepository

# Fixed identifiers/predicates only: never interpolate caller input. Include drafts and zeroed
# history, since later posting/reversal can still interpret these amounts in the old base unit.
_USAGE = {
    "platform.finance_journal_intent": "true",
    "inventory_ledger.inventory_transaction": "true",
    "inventory_ledger.inventory_balance": "true",
    "inventory_ledger.inventory_adjustment_line": "true",
    "inventory_ledger.inventory_worksheet_line": "true",
    "inventory_ledger.inventory_distribution_line": "true",
    "inventory_ledger.inventory_in_transit": "true",
    "inventory_ledger.transfer_order": "transfer_price IS NOT NULL",
    "item_master.item_inventory": "initial_cost IS NOT NULL",
    "item_master.item_purchasing": "purchase_price IS NOT NULL AND purchase_price_currency IS NULL",
    "costing.item_cost": "true",
    "costing.cost_ledger_entry": "true",
    "costing.cost_layer": "true",
    "costing.cost_variance": "true",
    "costing.cogs_adjustment": "true",
    "costing.landed_cost": "true",
    "costing.group_average_balance": "true",
    "costing.planned_standard_cost": "true",
    "costing.standard_cost": "true",
    "costing.cost_rollup_line": "true",
    "costing.cost_revaluation": "true",
    "costing.cost_template_line": "fixed_amount IS NOT NULL",
    "procurement.purchase_order": "true",
    "procurement.item_receipt": "true",
    "procurement.vendor_bill": "true",
    "procurement.vendor_credit": "true",
    "procurement.vendor_return_authorization": "true",
    "manufacturing.assembly_item": "build_cost <> 0",
    "manufacturing.work_center": "cost_per_hour <> 0",
    "manufacturing.routing_step_charge": "true",
    "manufacturing.manufacturing_cost_template": "true",
    "manufacturing.manufacturing_cost_template_line": "true",
    "manufacturing.wip_transaction": "true",
    "manufacturing.assembly_build": "true",
    "manufacturing.assembly_unbuild": "true",
    "manufacturing.work_order_wip_balance": "true",
    "consigned.consignment_agreement_item": "true",
    "consigned.consignment_ownership_transfer": "true",
    "consigned.consignment_consumption": "true",
    "counts.inventory_count_snapshot": "true",
}


def _blocker(table: str) -> CurrencyChangeBlocker:
    schema, name = table.split(".")
    if schema == "inventory_ledger":
        if name == "inventory_transaction":
            return CurrencyChangeBlocker.INVENTORY_HISTORY
        if name == "inventory_balance":
            return CurrencyChangeBlocker.INVENTORY_BALANCES
        return CurrencyChangeBlocker.INVENTORY_DOCUMENTS
    if schema == "costing":
        if name in {"planned_standard_cost", "standard_cost", "cost_template_line"}:
            return CurrencyChangeBlocker.COST_CONFIGURATION
        return CurrencyChangeBlocker.COST_HISTORY
    if schema == "manufacturing":
        if name in {"wip_transaction", "assembly_build", "assembly_unbuild", "work_order_wip_balance"}:
            return CurrencyChangeBlocker.MANUFACTURING_HISTORY
        return CurrencyChangeBlocker.MANUFACTURING_COST_CONFIGURATION
    return {
        "platform": CurrencyChangeBlocker.FINANCE_JOURNALS,
        "item_master": CurrencyChangeBlocker.ITEM_COST_CONFIGURATION,
        "procurement": CurrencyChangeBlocker.PROCUREMENT_DOCUMENTS,
        "consigned": CurrencyChangeBlocker.CONSIGNMENT_RECORDS,
        "counts": CurrencyChangeBlocker.COUNT_SNAPSHOTS,
    }[schema]


_APPEND_ONLY = frozenset({"platform.finance_journal_intent", "inventory_ledger.inventory_transaction"})


class SqlCurrencyUsage:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def blockers(self, tenant_id: UUID, *, lock: bool = False) -> list[CurrencyChangeBlocker]:
        if lock:
            tables = ", ".join(sorted(_USAGE.keys() - _APPEND_ONLY))
            await self.session.execute(text(f"LOCK TABLE {tables} IN SHARE MODE NOWAIT"))
        query = " UNION ALL ".join(
            f"SELECT '{_blocker(table).value}' AS blocker WHERE EXISTS "
            f"(SELECT 1 FROM {table} WHERE tenant_id = :tenant_id AND ({predicate}))"
            for table, predicate in sorted(_USAGE.items())
        )
        rows = (await self.session.execute(text(query), {"tenant_id": tenant_id})).scalars()
        return sorted({CurrencyChangeBlocker(value) for value in rows})


def tenant_configuration_repository(session: AsyncSession) -> TenantConfigurationRepository:
    return TenantConfigurationRepository(session, SqlCurrencyUsage(session))
