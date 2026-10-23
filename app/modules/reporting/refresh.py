"""FS-15 reporting refresh entrypoints (job handlers).

``refresh_materialized_views`` is a thin async wrapper around the SQL function created by migration
0030; it is idempotent and safe to re-run. It intentionally does **not** recompute any view locally:
the materialised SQL owns the projection and the FS-09/FS-10/FS-11/FS-12/FS-13 source contexts own
authoritative calculations (V-21 is pass-through, never a local ATB formula).
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def refresh_materialized_views(session: AsyncSession, *, concurrent: bool = True) -> list[dict[str, object]]:
    result = await session.execute(
        text("SELECT view_name, refreshed FROM reporting.refresh_materialized_views(:concurrent)"),
        {"concurrent": concurrent},
    )
    return [dict(row._mapping) for row in result.all()]


__all__ = ["refresh_materialized_views"]
