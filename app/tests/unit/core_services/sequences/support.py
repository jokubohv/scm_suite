"""Shared helpers for the FS-00 core-services tests (test-only code)."""

from __future__ import annotations

import uuid

from sqlalchemy import delete, insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.models.platform import number_sequence, tenant


async def create_tenant(session: AsyncSession, *, code: str = "TEN-1", status: str = "ACTIVE") -> uuid.UUID:
    tenant_id = uuid.uuid4()
    await session.execute(
        insert(tenant).values(
            id=tenant_id,
            tenant_id=tenant_id,  # tenant row is its own tenant scope (DATA-MODEL §3.1)
            code=code,
            name=f"Tenant {code}",
            status=status,
            base_currency_code="USD",
        )
    )
    return tenant_id


async def create_tenant_committed(engine: AsyncEngine, *, code: str = "TEN-CONC") -> uuid.UUID:
    tenant_id = uuid.uuid4()
    async with engine.begin() as connection:
        await connection.execute(
            insert(tenant).values(
                id=tenant_id,
                tenant_id=tenant_id,
                code=code,
                name=f"Tenant {code}",
                status="ACTIVE",
                base_currency_code="USD",
            )
        )
    return tenant_id


async def cleanup_committed(engine: AsyncEngine, tenant_id: uuid.UUID) -> None:
    async with engine.begin() as connection:
        await connection.execute(delete(number_sequence).where(number_sequence.c.tenant_id == tenant_id))
        await connection.execute(delete(tenant).where(tenant.c.id == tenant_id))


__all__ = ["cleanup_committed", "create_tenant", "create_tenant_committed"]
