"""Own-tenant configuration policy; never provisions or administers other tenants."""

from enum import StrEnum
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.models import platform as p
from app.core.repositories.config import ConfigConflict, ConfigError, ConfigNotFound, ConfigRepository, ConfigStale
from app.core.security.tenant_context import SYSTEM_TENANT_ID


class CurrencyChangeBlocker(StrEnum):
    FINANCE_JOURNALS = "finance_journals"
    INVENTORY_HISTORY = "inventory_history"
    INVENTORY_BALANCES = "inventory_balances"
    INVENTORY_DOCUMENTS = "inventory_documents"
    ITEM_COST_CONFIGURATION = "item_cost_configuration"
    COST_HISTORY = "cost_history"
    COST_CONFIGURATION = "cost_configuration"
    PROCUREMENT_DOCUMENTS = "procurement_documents"
    MANUFACTURING_COST_CONFIGURATION = "manufacturing_cost_configuration"
    MANUFACTURING_HISTORY = "manufacturing_history"
    CONSIGNMENT_RECORDS = "consignment_records"
    COUNT_SNAPSHOTS = "count_snapshots"


class CurrencyUsage(Protocol):
    """Read valuation dependencies; lock them before deciding that a change is safe."""

    async def blockers(self, tenant_id: UUID, *, lock: bool = False) -> list[CurrencyChangeBlocker]: ...


class TenantConfigurationRepository:
    def __init__(self, session: AsyncSession, usage: CurrencyUsage) -> None:
        self.session = session
        self.usage = usage

    async def _tenant(self, tenant_id: UUID, *, lock: bool = False) -> dict[str, Any]:
        if tenant_id == SYSTEM_TENANT_ID:
            raise ConfigConflict("The SYSTEM tenant configuration is reserved", code="TENANT_RESERVED")
        stmt = select(p.tenant).where(p.tenant.c.id == tenant_id, p.tenant.c.tenant_id == tenant_id)
        if lock:
            stmt = stmt.with_for_update(nowait=True)
        row = (await self.session.execute(stmt)).mappings().one_or_none()
        if row is None:
            raise ConfigNotFound("Tenant configuration not found")
        return dict(row)

    @staticmethod
    def _view(row: dict[str, Any], blockers: list[CurrencyChangeBlocker]) -> dict[str, Any]:
        return {
            "tenant_id": str(row["id"]),
            "base_currency_code": row["base_currency_code"],
            "version": row["version"],
            "currency_change_blockers": blockers,
        }

    async def get(self, tenant_id: UUID) -> dict[str, Any]:
        return self._view(await self._tenant(tenant_id), await self.usage.blockers(tenant_id))

    async def set_currency(
        self,
        tenant_id: UUID,
        code: str,
        *,
        expected_version: int,
        actor_id: str,
        actor_type: str,
    ) -> dict[str, Any]:
        current = await self._tenant(tenant_id, lock=True)
        if current["version"] != expected_version:
            raise ConfigStale("Tenant version does not match If-Match", details={"current_version": current["version"]})
        # Same-value requests are harmless even after operations have started.
        if current["base_currency_code"] == code:
            return self._view(current, await self.usage.blockers(tenant_id))
        currency = await self.session.scalar(
            select(p.currency.c.code)
            .where(
                p.currency.c.code == code,
                p.currency.c.is_active.is_(True),
                p.currency.c.tenant_id.is_(None),
            )
            .with_for_update(read=True, nowait=True)
        )
        if currency is None:
            raise ConfigError(
                "Base currency must be an active shared currency",
                code="CURRENCY_INVALID",
                details={"base_currency_code": code},
            )
        blockers = await self.usage.blockers(tenant_id, lock=True)
        if blockers:
            raise ConfigConflict(
                "Existing valuation data requires a currency migration before changing the base currency",
                code="BASE_CURRENCY_IN_USE",
                details={"blockers": blockers},
            )
        updated = (
            (
                await self.session.execute(
                    update(p.tenant)
                    .where(
                        p.tenant.c.id == tenant_id,
                        p.tenant.c.tenant_id == tenant_id,
                        p.tenant.c.version == expected_version,
                    )
                    .values(
                        base_currency_code=code,
                        updated_by=UUID(actor_id),
                        version=expected_version + 1,
                        updated_at=func.now(),
                    )
                    .returning(p.tenant)
                )
            )
            .mappings()
            .one()
        )
        await ConfigRepository(self.session).audit(
            tenant_id=tenant_id,
            actor_id=actor_id,
            actor_type=actor_type,
            action="tenant.configuration.updated",
            resource_type="tenant",
            resource_id=tenant_id,
            diff={
                "before": {"base_currency_code": current["base_currency_code"]},
                "after": {"base_currency_code": code},
            },
        )
        return self._view(dict(updated), [])
