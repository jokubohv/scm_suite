"""`counts.engine@1.0.0` in-process facade for FS-11 (WMS counts) and FS-15 (reporting).

The facade delegates to :class:`app.modules.counts.service.CountsService` with the caller's session and
tenant (no HTTP between modules, ARCHITECTURE §1.4). Count variance posting stays inside the counts
request transaction and delegates to FS-04's ``PostingPort``/FS-05 valuation.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy.ext.asyncio import AsyncSession

if TYPE_CHECKING:
    from app.modules.counts.service import CountsService

CONTRACT_VERSION = "1.0.0"


class CountsPort:
    def __init__(self, session: AsyncSession, tenant_id: Any) -> None:
        from app.modules.counts.service import CountsService

        self.session = session
        self.tenant_id = tenant_id
        self.service: CountsService = CountsService(session, tenant_id)

    async def generate_from_plan(self, plan_id: str, options: dict[str, Any] | None = None) -> dict[str, Any]:
        return await self.service.generate_from_plan(plan_id, options or {})

    async def create_count(self, scope: dict[str, Any], options: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = dict(options or {})
        payload["scope"] = scope
        return await self.service.create_count(payload)

    async def start_count(self, count_id: str) -> dict[str, Any]:
        return await self.service.start_count(count_id)

    async def capture_lines(self, count_id: str, lines: list[dict[str, Any]]) -> dict[str, Any]:
        return await self.service.capture_lines(count_id, {"lines": lines})

    async def submit(self, count_id: str) -> dict[str, Any]:
        return await self.service.submit(count_id)

    async def approve(self, count_id: str, actor: str | None = None) -> dict[str, Any]:
        return await self.service.approve(count_id, {"approved_by": actor})

    async def reject(self, count_id: str, reason: str, *, recalc_snapshot: bool = True) -> dict[str, Any]:
        return await self.service.reject(count_id, {"reason": reason, "recalc_snapshot": recalc_snapshot})

    async def recount(self, count_id: str, lines: list[dict[str, Any]]) -> dict[str, Any]:
        return await self.service.recount(count_id, {"lines": lines})

    async def post_variance(self, count_id: str) -> dict[str, Any]:
        return await self.service.post_variance(count_id)

    async def get_next_count_date(self, item_id: str, location_id: str | None = None) -> dict[str, Any]:
        return await self.service.next_count(item_id, location_id)


__all__ = ["CONTRACT_VERSION", "CountsPort"]
