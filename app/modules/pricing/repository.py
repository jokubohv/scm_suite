"""Async SQLAlchemy Core repository for the FS-02 pricing engine.

Tenant-scoped primitives for all 17 ``pricing`` tables plus the higher-level operations the service
layer uses: pricing snapshot loading for resolution, coupon redemption (append-only
``platform.document_link`` + counter), effective-date update apply/revert materialisation,
collection/coupon jobs, bulk price writes, and job-run enqueueing. No ORM, no commits (caller owns the
transaction), no writes to FS-01/FS-00 tables except the documented FS-00 contract surfaces
(``platform.document_link`` / ``platform.outbox_event`` / ``platform.audit_event`` /
``platform.job_run``).
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import delete, func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.models import platform as platform_models
from app.core.outbox import emit as emit_outbox
from app.modules.pricing import tables as t
from app.modules.pricing.engine import (
    CouponRow,
    ItemInfo,
    PriceLevelInfo,
    PriceLineInput,
    PriceRequest,
    PriceRow,
    PricingGroupRow,
    PricingSnapshot,
    RuleAction,
    RuleCriterion,
    RuleRow,
    ScheduleBreak,
    ScheduleRow,
    coupon_validation_payload,
    resolve_prices,
    validate_coupon,
)
from app.modules.pricing.item_port import ItemMasterPort

__all__ = ["PricingRepository", "PricingRepositoryError"]

PRICING_POLICY_VERSION = "pricing-engine@1.0.0"
JOB_APPLY_UPDATES = "pricing.apply_effective_updates"
JOB_RECOMPUTE_COLLECTIONS = "pricing.recompute_dynamic_collections"
JOB_COUPON_EXPIRE = "pricing.coupon.expire"


class PricingRepositoryError(Exception):
    def __init__(
        self, code: str, message: str, *, status_code: int = 422, details: Mapping[str, Any] | None = None
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = dict(details or {})


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Decimal):
        return format(value, "f")
    return value


def _row(mapping: Any) -> dict[str, Any]:
    return {key: _iso(value) for key, value in dict(mapping).items()}


def _as_uuid(value: str | UUID) -> UUID:
    return value if isinstance(value, UUID) else UUID(str(value))


def serialize_price_level(row: Mapping[str, Any]) -> dict[str, Any]:
    return _row(row)


class PricingRepository:
    """One request-scoped repository; the router dependency owns commit/rollback."""

    def __init__(self, session: AsyncSession, item_port: ItemMasterPort) -> None:
        self.session = session
        self.item_port = item_port

    # ---------------------------------------------------------------- generic primitives -----
    async def audit(
        self,
        *,
        tenant_id: str | UUID,
        actor_id: str | UUID | None,
        action: str,
        resource_type: str,
        resource_id: str | UUID | None,
        diff: Mapping[str, Any] | None = None,
    ) -> None:
        event_id = uuid.uuid4()
        table = platform_models.metadata.tables["platform.audit_event"]
        await self.session.execute(
            table.insert().values(
                id=event_id,
                tenant_id=_as_uuid(tenant_id),
                event_id=event_id,
                actor_type="USER" if actor_id else "SYSTEM",
                actor_id=_as_uuid(actor_id) if actor_id else None,
                action=action,
                resource_type=resource_type,
                resource_id=str(resource_id) if resource_id is not None else None,
                diff=dict(diff or {}),
                source="API",
                occurred_at=_now(),
            )
        )

    async def emit(
        self,
        *,
        tenant_id: str | UUID,
        event_type: str,
        aggregate_type: str,
        aggregate_id: str | UUID,
        payload: Mapping[str, Any],
        aggregate_version: int = 1,
    ) -> None:
        await emit_outbox(
            self.session,
            event_type=event_type,
            tenant_id=str(tenant_id),
            aggregate_type=aggregate_type,
            aggregate_id=str(aggregate_id),
            payload=dict(payload),
            event_version="1.0.0",
            aggregate_version=aggregate_version,
        )

    async def get_row(self, table: Any, tenant_id: str | UUID, row_id: str | UUID) -> dict[str, Any] | None:
        result = await self.session.execute(
            select(table).where(table.c.tenant_id == _as_uuid(tenant_id), table.c.id == _as_uuid(row_id))
        )
        mapping = result.mappings().first()
        return _row(mapping) if mapping else None

    async def list_rows(
        self,
        table: Any,
        tenant_id: str | UUID,
        *,
        filters: Mapping[str, Any] | None = None,
        order_by: Sequence[Any] | None = None,
        limit: int = 500,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        stmt = select(table).where(table.c.tenant_id == _as_uuid(tenant_id))
        for key, value in (filters or {}).items():
            column = table.c[key]
            stmt = stmt.where(column == value)
        stmt = stmt.order_by(*(order_by or (table.c.created_at.desc(), table.c.id.desc()))).limit(limit).offset(offset)
        return [_row(mapping) for mapping in (await self.session.execute(stmt)).mappings().all()]

    async def insert_row(
        self, table: Any, tenant_id: str | UUID, values: Mapping[str, Any], actor_id: str | UUID | None
    ) -> dict[str, Any]:
        row_id = values.get("id") or uuid.uuid4()
        now = _now()
        payload = {
            "id": row_id,
            "tenant_id": _as_uuid(tenant_id),
            **dict(values),
            "created_at": now,
            "updated_at": now,
            "created_by": _as_uuid(actor_id) if actor_id else None,
            "updated_by": _as_uuid(actor_id) if actor_id else None,
            "version": 1,
        }
        try:
            async with self.session.begin_nested():
                await self.session.execute(table.insert().values(**payload))
        except IntegrityError:
            raise
        fetched = await self.get_row(table, tenant_id, row_id)
        assert fetched is not None
        return fetched

    async def update_row(
        self,
        table: Any,
        tenant_id: str | UUID,
        row_id: str | UUID,
        values: Mapping[str, Any],
        actor_id: str | UUID | None,
        *,
        expected_version: int | None = None,
    ) -> dict[str, Any] | None:
        current = await self.get_row(table, tenant_id, row_id)
        if current is None:
            return None
        if expected_version is not None and int(current["version"]) != expected_version:
            raise PricingRepositoryError(
                "STALE_VERSION",
                "resource version does not match If-Match",
                status_code=412,
                details={"current_version": current["version"]},
            )
        payload = {
            **dict(values),
            "updated_at": _now(),
            "updated_by": _as_uuid(actor_id) if actor_id else None,
            "version": table.c.version + 1,
        }
        await self.session.execute(
            update(table)
            .where(table.c.tenant_id == _as_uuid(tenant_id), table.c.id == _as_uuid(row_id))
            .values(**payload)
        )
        return await self.get_row(table, tenant_id, row_id)

    async def delete_row(self, table: Any, tenant_id: str | UUID, row_id: str | UUID) -> bool:
        result = await self.session.execute(
            delete(table).where(table.c.tenant_id == _as_uuid(tenant_id), table.c.id == _as_uuid(row_id))
        )
        return int(getattr(result, "rowcount", 0) or 0) == 1

    # ---------------------------------------------------------------- tenant lookup ----------
    async def tenant_currency(self, tenant_id: str | UUID) -> str:
        result = await self.session.execute(
            select(platform_models.tenant.c.base_currency_code).where(
                platform_models.tenant.c.id == _as_uuid(tenant_id)
            )
        )
        base = result.scalar_one_or_none()
        return str(base or "USD")

    async def setting(self, tenant_id: str | UUID, key: str, default: Any = None) -> Any:
        table = platform_models.metadata.tables["platform.tenant_setting"]
        result = await self.session.execute(
            select(table.c.value).where(
                table.c.tenant_id == _as_uuid(tenant_id),
                table.c.namespace == "pricing",
                table.c.key == key,
            )
        )
        value = result.scalar_one_or_none()
        return value if value is not None else default

    async def ensure_default_level(self, tenant_id: str | UUID) -> dict[str, Any]:
        """Lazily ensure the tenant has a default price level (seed gap: DATA-MODEL §21.2)."""
        tid = _as_uuid(tenant_id)
        existing = await self.session.execute(
            select(t.price_level).where(t.price_level.c.tenant_id == tid, t.price_level.c.is_default.is_(True))
        )
        mapping = existing.mappings().first()
        if mapping is not None:
            return _row(mapping)
        currency = await self.tenant_currency(tid)
        row = await self.insert_row(
            t.price_level,
            tid,
            {
                "code": "DEFAULT",
                "name": "Default",
                "currency_code": currency,
                "is_default": True,
                "is_inactive": False,
                "sort_order": 0,
            },
            None,
        )
        return row

    # ---------------------------------------------------------------- snapshot ---------------
    async def load_snapshot(self, *, tenant_id: str | UUID, request: PriceRequest) -> PricingSnapshot:
        tid = _as_uuid(tenant_id)
        await self.ensure_default_level(tid)
        item_ids = [line.item_id for line in request.lines]
        items = await self.item_port.load_items(tid, item_ids)
        level_rows = await self.list_rows(
            t.price_level,
            tid,
            filters={"is_inactive": False},
            order_by=(t.price_level.c.sort_order, t.price_level.c.code),
        )
        levels: dict[UUID, PriceLevelInfo] = {}
        default_level_id: UUID | None = None
        for row in level_rows:
            level_id = UUID(str(row["id"]))
            levels[level_id] = PriceLevelInfo(
                id=level_id,
                code=str(row["code"]),
                currency_code=str(row["currency_code"]),
                is_inactive=bool(row["is_inactive"]),
                is_default=bool(row["is_default"]),
            )
            if row["is_default"]:
                default_level_id = level_id
        if default_level_id is None and levels:
            default_level_id = next(iter(levels))
        price_rows_raw = await self.list_rows(
            t.item_price,
            tid,
            filters={"is_active": True},
            order_by=(t.item_price.c.effective_from.desc(), t.item_price.c.created_at.desc()),
            limit=5000,
        )
        prices = tuple(
            PriceRow(
                id=UUID(str(row["id"])),
                item_id=UUID(str(row["item_id"])),
                price_level_id=UUID(str(row["price_level_id"])),
                currency_code=str(row["currency_code"]),
                qty_min=Decimal(str(row["qty_min"])),
                qty_max=Decimal(str(row["qty_max"])) if row["qty_max"] is not None else None,
                unit_price=Decimal(str(row["unit_price"])),
                effective_from=date.fromisoformat(str(row["effective_from"])),
                effective_to=date.fromisoformat(str(row["effective_to"])) if row["effective_to"] else None,
                is_active=bool(row["is_active"]),
                cost_plus_basis=row["cost_plus_basis"],
                created_at=row["created_at"],
            )
            for row in price_rows_raw
            if UUID(str(row["item_id"])) in set(item_ids)
        )
        group_rows = await self.list_rows(t.pricing_group, tid, limit=2000)
        pricing_groups = tuple(
            PricingGroupRow(
                id=UUID(str(row["id"])),
                item_group_id=UUID(str(row["item_group_id"])),
                customer_group_id=UUID(str(row["customer_group_id"])),
                price_level_id=UUID(str(row["price_level_id"])),
                is_default=bool(row["is_default"]),
                discount_percent=Decimal(str(row["discount_percent"])) if row["discount_percent"] is not None else None,
            )
            for row in group_rows
        )
        # item -> pricing item group membership (RULE-PRICE-08: at most one in Phase 1)
        item_group_of_item: dict[UUID, UUID] = {}
        if item_ids:
            member_rows = await self.session.execute(
                select(t.item_group_member.c.item_id, t.item_group_member.c.item_group_id).where(
                    t.item_group_member.c.tenant_id == tid,
                    t.item_group_member.c.item_id.in_(item_ids),
                )
            )
            for item_id, group_id in member_rows.all():
                item_group_of_item.setdefault(UUID(str(item_id)), UUID(str(group_id)))
        items_with_group = {
            item_id: ItemInfo(
                item_id=info.item_id,
                is_active=info.is_active,
                category_id=info.category_id,
                pricing_group_id=item_group_of_item.get(item_id),
                schedule_id=info.schedule_id,
                matrix_parent_id=info.matrix_parent_id,
                qty_behavior=info.qty_behavior,
                initial_cost=info.initial_cost,
                purchase_price=info.purchase_price,
            )
            for item_id, info in items.items()
        }
        # schedules (only those referenced by the requested items)
        schedule_ids = {info.schedule_id for info in items_with_group.values() if info.schedule_id}
        schedules: dict[UUID, ScheduleRow] = {}
        if schedule_ids:
            schedule_rows = await self.session.execute(
                select(t.price_schedule).where(
                    t.price_schedule.c.tenant_id == tid, t.price_schedule.c.id.in_(list(schedule_ids))
                )
            )
            break_rows = await self.session.execute(
                select(t.price_schedule_break)
                .where(
                    t.price_schedule_break.c.tenant_id == tid,
                    t.price_schedule_break.c.price_schedule_id.in_(list(schedule_ids)),
                )
                .order_by(t.price_schedule_break.c.qty_from)
            )
            breaks_by_schedule: dict[UUID, list[ScheduleBreak]] = {}
            for break_row in break_rows.mappings().all():
                breaks_by_schedule.setdefault(UUID(str(break_row["price_schedule_id"])), []).append(
                    ScheduleBreak(
                        qty_from=Decimal(str(break_row["qty_from"])),
                        qty_to=Decimal(str(break_row["qty_to"])) if break_row["qty_to"] is not None else None,
                        pricing_method=str(break_row["pricing_method"]),
                        value=Decimal(str(break_row["value"])),
                    )
                )
            for schedule_row in schedule_rows.mappings().all():
                row = dict(schedule_row)
                schedule_id = UUID(str(row["id"]))
                schedules[schedule_id] = ScheduleRow(
                    id=schedule_id,
                    code=str(row["code"]),
                    calculation_mode=str(row["calculation_mode"]),
                    is_marginal=bool(row["is_marginal"]),
                    currency_code=str(row["currency_code"]),
                    breaks=tuple(breaks_by_schedule.get(schedule_id, ())),
                )
        rules = await self._load_rules(tid, request.document_date)
        collections = await self._load_collections(tid)
        coupon_map: dict[str, CouponRow] = {}
        if request.coupon_code:
            coupon = await self.get_coupon_by_code(tid, request.coupon_code)
            if coupon is not None:
                coupon_map[coupon.code] = coupon
        allow_fallback = bool(await self.setting(tid, "allow_base_currency_fallback", False))
        base_currency = await self.tenant_currency(tid)
        fx_rates = await self._load_fx_rates(tid, base_currency, request.currency_code, request.document_date)
        return PricingSnapshot(
            items=items_with_group,
            levels=levels,
            default_level_id=default_level_id,
            prices=prices,
            pricing_groups=pricing_groups,
            schedules=schedules,
            rules=rules,
            collections=collections,
            coupons=coupon_map,
            base_currency=base_currency,
            allow_base_fallback=allow_fallback,
            fx_rates=fx_rates,
        )

    async def _load_rules(self, tenant_id: UUID, as_of: date) -> tuple[RuleRow, ...]:
        rule_rows = await self.session.execute(
            select(t.price_rule).where(t.price_rule.c.tenant_id == tenant_id, t.price_rule.c.is_active.is_(True))
        )
        rules: list[RuleRow] = []
        for row in rule_rows.mappings().all():
            rule_id = UUID(str(row["id"]))
            criteria_rows = await self.session.execute(
                select(t.price_rule_criteria).where(
                    t.price_rule_criteria.c.tenant_id == tenant_id,
                    t.price_rule_criteria.c.price_rule_id == rule_id,
                )
            )
            criteria = tuple(
                RuleCriterion(
                    criterion=str(c["criterion"]),
                    operator=str(c["operator"]),
                    value_text=c["value_text"],
                    value_number=Decimal(str(c["value_number"])) if c["value_number"] is not None else None,
                    value_date=date.fromisoformat(str(c["value_date"])) if c["value_date"] else None,
                    group_no=int(c["group_no"]),
                )
                for c in criteria_rows.mappings().all()
            )
            action_row = (
                (
                    await self.session.execute(
                        select(t.price_rule_action).where(
                            t.price_rule_action.c.tenant_id == tenant_id,
                            t.price_rule_action.c.price_rule_id == rule_id,
                        )
                    )
                )
                .mappings()
                .first()
            )
            if action_row is None:
                continue
            action = RuleAction(
                action_type=str(action_row["action_type"]),
                price_level_id=UUID(str(action_row["price_level_id"])) if action_row["price_level_id"] else None,
                value=Decimal(str(action_row["value"])) if action_row["value"] is not None else None,
                cost_basis=action_row["cost_basis"],
                apply_to=str(action_row["apply_to"]),
            )
            rules.append(
                RuleRow(
                    id=rule_id,
                    code=str(row["code"]),
                    rule_type=str(row["rule_type"]),
                    priority=int(row["priority"]),
                    conflict_policy=str(row["conflict_policy"]),
                    is_active=bool(row["is_active"]),
                    start_date=date.fromisoformat(str(row["start_date"])) if row["start_date"] else None,
                    end_date=date.fromisoformat(str(row["end_date"])) if row["end_date"] else None,
                    currency_code=row["currency_code"],
                    criteria=criteria,
                    action=action,
                )
            )
        return tuple(rules)

    async def _load_collections(self, tenant_id: UUID) -> dict[UUID, frozenset[UUID]]:
        rows = await self.session.execute(
            select(t.item_collection_member.c.item_collection_id, t.item_collection_member.c.item_id).where(
                t.item_collection_member.c.tenant_id == tenant_id
            )
        )
        result: dict[UUID, set[UUID]] = {}
        for collection_id, item_id in rows.all():
            result.setdefault(UUID(str(collection_id)), set()).add(UUID(str(item_id)))
        return {collection_id: frozenset(members) for collection_id, members in result.items()}

    async def _load_fx_rates(
        self, tenant_id: UUID, base: str, quote: str, as_of: date
    ) -> dict[tuple[str, str, date], Decimal]:
        if base == quote:
            return {}
        table = platform_models.metadata.tables["platform.exchange_rate"]
        rows = await self.session.execute(
            select(table.c.base_currency, table.c.quote_currency, table.c.rate_date, table.c.rate)
            .where(
                table.c.tenant_id == tenant_id,
                table.c.base_currency == base,
                table.c.quote_currency == quote,
                table.c.rate_date <= as_of,
            )
            .order_by(table.c.rate_date.desc())
            .limit(3)
        )
        return {
            (str(row["base_currency"]), str(row["quote_currency"]), row["rate_date"]): Decimal(str(row["rate"]))
            for row in rows.mappings().all()
        }

    # ---------------------------------------------------------------- coupons -----------------
    async def get_coupon_by_code(self, tenant_id: str | UUID, code: str) -> CouponRow | None:
        result = await self.session.execute(
            select(t.coupon).where(t.coupon.c.tenant_id == _as_uuid(tenant_id), t.coupon.c.code == code.strip().upper())
        )
        mapping = result.mappings().first()
        if mapping is None:
            return None
        return CouponRow(
            id=UUID(str(mapping["id"])),
            code=str(mapping["code"]),
            coupon_type=str(mapping["coupon_type"]),
            value=Decimal(str(mapping["value"])),
            currency_code=str(mapping["currency_code"]),
            start_date=date.fromisoformat(str(mapping["start_date"])),
            end_date=date.fromisoformat(str(mapping["end_date"])),
            max_uses=mapping["max_uses"],
            uses_count=int(mapping["uses_count"]),
            is_active=bool(mapping["is_active"]),
            item_id=UUID(str(mapping["item_id"])) if mapping["item_id"] else None,
            item_category_id=UUID(str(mapping["item_category_id"])) if mapping["item_category_id"] else None,
        )

    async def validate_coupon_context(
        self,
        *,
        tenant_id: str | UUID,
        code: str,
        item_id: str | UUID,
        quantity: Decimal,
        unit_price: Decimal,
        currency_code: str,
        document_date: date,
    ) -> dict[str, Any]:
        item = (await self.item_port.load_items(tenant_id, [_as_uuid(item_id)])).get(_as_uuid(item_id))
        if item is None:
            raise PricingRepositoryError("UNKNOWN_ITEM", "item does not exist", status_code=422)
        coupon = await self.get_coupon_by_code(tenant_id, code)
        snapshot = PricingSnapshot(
            items={item.item_id: item},
            levels={},
            default_level_id=None,
            prices=(),
            coupons={coupon.code: coupon} if coupon else {},
            base_currency=currency_code,
        )
        return coupon_validation_payload(
            snapshot,
            code=code,
            item=item,
            quantity=quantity,
            unit_price=unit_price,
            currency_code=currency_code,
            document_date=document_date,
        )

    async def redeem_coupon(
        self,
        *,
        tenant_id: str | UUID,
        code: str,
        document_type: str,
        document_id: str | UUID,
        item_id: str | UUID,
        quantity: Decimal,
        unit_price: Decimal,
        currency_code: str,
        document_date: date,
        actor_id: str | UUID | None,
    ) -> dict[str, Any]:
        tid = _as_uuid(tenant_id)
        # Lock the coupon row for the duration of the caller's transaction.
        result = await self.session.execute(
            select(t.coupon)
            .where(t.coupon.c.tenant_id == tid, t.coupon.c.code == code.strip().upper())
            .with_for_update()
        )
        mapping = result.mappings().first()
        if mapping is None:
            raise PricingRepositoryError("COUPON_NOT_FOUND", "coupon not found", status_code=404)
        coupon = CouponRow(
            id=UUID(str(mapping["id"])),
            code=str(mapping["code"]),
            coupon_type=str(mapping["coupon_type"]),
            value=Decimal(str(mapping["value"])),
            currency_code=str(mapping["currency_code"]),
            start_date=date.fromisoformat(str(mapping["start_date"])),
            end_date=date.fromisoformat(str(mapping["end_date"])),
            max_uses=mapping["max_uses"],
            uses_count=int(mapping["uses_count"]),
            is_active=bool(mapping["is_active"]),
            item_id=UUID(str(mapping["item_id"])) if mapping["item_id"] else None,
            item_category_id=UUID(str(mapping["item_category_id"])) if mapping["item_category_id"] else None,
        )
        item = (await self.item_port.load_items(tid, [_as_uuid(item_id)])).get(_as_uuid(item_id))
        if item is None:
            raise PricingRepositoryError("UNKNOWN_ITEM", "item does not exist", status_code=422)
        snapshot = PricingSnapshot(
            items={item.item_id: item}, levels={}, default_level_id=None, prices=(), coupons={coupon.code: coupon}
        )
        validated, reason = validate_coupon(
            snapshot,
            code=code,
            item=item,
            quantity=quantity,
            currency_code=currency_code,
            document_date=document_date,
        )
        if validated is None:
            raise PricingRepositoryError(reason or "COUPON_INVALID", "coupon is not applicable", status_code=409)
        existing = (
            await self.session.execute(
                select(platform_models.metadata.tables["platform.document_link"].c.id).where(
                    platform_models.metadata.tables["platform.document_link"].c.tenant_id == tid,
                    platform_models.metadata.tables["platform.document_link"].c.link_type == "APPLIED_TO",
                    platform_models.metadata.tables["platform.document_link"].c.from_type == "COUPON",
                    platform_models.metadata.tables["platform.document_link"].c.from_id == coupon.id,
                    platform_models.metadata.tables["platform.document_link"].c.to_type == document_type,
                    platform_models.metadata.tables["platform.document_link"].c.to_id == _as_uuid(document_id),
                )
            )
        ).first()
        already = existing is not None
        discount = Decimal("0")
        if coupon.coupon_type == "PERCENT":
            discount = unit_price * quantity * coupon.value / Decimal(100)
        else:
            discount = min(coupon.value, unit_price * quantity)
        if not already:
            link_table = platform_models.metadata.tables["platform.document_link"]
            await self.session.execute(
                link_table.insert().values(
                    id=uuid.uuid4(),
                    tenant_id=tid,
                    link_type="APPLIED_TO",
                    from_type="COUPON",
                    from_id=coupon.id,
                    to_type=document_type,
                    to_id=_as_uuid(document_id),
                    qty=None,
                    amount_base=discount,
                    created_by=_as_uuid(actor_id) if actor_id else None,
                    created_at=_now(),
                )
            )
            await self.session.execute(
                update(t.coupon)
                .where(t.coupon.c.tenant_id == tid, t.coupon.c.id == coupon.id)
                .values(uses_count=t.coupon.c.uses_count + 1, updated_at=_now(), version=t.coupon.c.version + 1)
            )
            await self.emit(
                tenant_id=tid,
                event_type="pricing.coupon.redeemed",
                aggregate_type="pricing.coupon",
                aggregate_id=coupon.id,
                payload={
                    "coupon_code": coupon.code,
                    "document_type": document_type,
                    "document_id": str(document_id),
                    "uses_count": coupon.uses_count + 1,
                    "max_uses": coupon.max_uses,
                    "discount_amount": format(discount, "f"),
                },
            )
        uses_count = coupon.uses_count + (0 if already else 1)
        return {
            "coupon_code": coupon.code,
            "redeemed": True,
            "already_redeemed": already,
            "discount_amount": format(discount, "f"),
            "uses_count": uses_count,
            "max_uses": coupon.max_uses,
        }

    # ---------------------------------------------------------------- resolution -------------
    async def resolve(self, *, tenant_id: str | UUID, request: PriceRequest) -> dict[str, Any]:
        snapshot = await self.load_snapshot(tenant_id=tenant_id, request=request)
        return resolve_prices(request, snapshot)

    async def effective_price(
        self,
        *,
        tenant_id: str | UUID,
        item_id: str | UUID,
        price_level_id: str | UUID | None,
        currency_code: str,
        as_of: date,
    ) -> dict[str, Any]:
        tid = _as_uuid(tenant_id)
        await self.ensure_default_level(tid)
        level_id = _as_uuid(price_level_id) if price_level_id else None
        if level_id is None:
            default = await self.ensure_default_level(tid)
            level_id = UUID(str(default["id"]))
        request = PriceRequest(
            lines=(PriceLineInput(line_ref="1", item_id=_as_uuid(item_id), quantity=Decimal("1"), uom="EA"),),
            currency_code=currency_code,
            document_date=as_of,
            price_level_id=level_id,
            include_trace=False,
        )
        result = await self.resolve(tenant_id=tid, request=request)
        line = result["lines"][0]
        return {
            "unit_price": line["unit_price"],
            "price_source": line["price_source"],
            "price_level_id": line["price_level_id"],
            "quantity_break": line["quantity_break"],
            "effective_at": line["effective_at"],
            "currency_code": currency_code,
        }

    # ---------------------------------------------------------------- price updates -----------
    async def _advisory_lock(self, tenant_id: UUID, *parts: Any) -> None:
        material = "|".join([str(tenant_id), *(str(part) for part in parts)])
        key = int(hashlib.sha256(material.encode("utf-8")).hexdigest()[:15], 16)
        await self.session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})

    async def create_price_update(
        self,
        *,
        tenant_id: str | UUID,
        values: Mapping[str, Any],
        details: Sequence[Mapping[str, Any]],
        actor_id: str | UUID | None,
    ) -> dict[str, Any]:
        tid = _as_uuid(tenant_id)
        await self._advisory_lock(
            tid,
            values.get("update_type"),
            values.get("target_ref"),
            values.get("price_level_id"),
            values.get("currency_code"),
        )
        try:
            async with self.session.begin_nested():
                header = await self.insert_row(t.price_update, tid, values, actor_id)
        except IntegrityError as exc:
            raise PricingRepositoryError(
                "PRICE_UPDATE_OVERLAP",
                "an overlapping price update already exists for this target/level/currency",
                status_code=409,
            ) from exc
        update_id = UUID(str(header["id"]))
        inserted: list[dict[str, Any]] = []
        for detail in details:
            payload = {key: value for key, value in detail.items() if value is not None}
            payload["price_update_id"] = update_id
            inserted.append(await self.insert_row(t.price_update_detail, tid, payload, actor_id))
        await self.audit(
            tenant_id=tid,
            actor_id=actor_id,
            action="pricing.price_update.created",
            resource_type="pricing.price_update",
            resource_id=update_id,
            diff={"update_type": header["update_type"], "target_ref": header["target_ref"], "details": len(inserted)},
        )
        return {"price_update": header, "details": inserted}

    async def get_price_update(self, tenant_id: str | UUID, update_id: str | UUID) -> dict[str, Any] | None:
        header = await self.get_row(t.price_update, tenant_id, update_id)
        if header is None:
            return None
        details = await self.list_rows(
            t.price_update_detail,
            tenant_id,
            filters={"price_update_id": _as_uuid(update_id)},
            order_by=(t.price_update_detail.c.qty_min, t.price_update_detail.c.id),
        )
        return {"price_update": header, "details": details}

    async def list_price_updates(
        self, tenant_id: str | UUID, *, status: str | None = None, limit: int = 200
    ) -> list[dict[str, Any]]:
        filters = {"status": status} if status else None
        return await self.list_rows(t.price_update, tenant_id, filters=filters, limit=limit)

    async def _link(
        self,
        tenant_id: UUID,
        *,
        link_type: str,
        from_id: UUID,
        to_type: str,
        to_id: UUID,
        actor_id: UUID | None,
        amount: Decimal | None = None,
    ) -> None:
        table = platform_models.metadata.tables["platform.document_link"]
        await self.session.execute(
            table.insert().values(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                link_type=link_type,
                from_type="PRICE_UPDATE",
                from_id=from_id,
                to_type=to_type,
                to_id=to_id,
                qty=None,
                amount_base=amount,
                created_by=actor_id,
                created_at=_now(),
            )
        )

    async def _generated_rule(
        self,
        *,
        tenant_id: UUID,
        update: Mapping[str, Any],
        detail: Mapping[str, Any],
        actor_id: UUID | None,
        index: int,
    ) -> dict[str, Any]:
        update_id = UUID(str(update["id"]))
        detail_price = Decimal(str(detail["new_price"]))
        rule_values: dict[str, Any] = {
            "code": f"UPD{str(update_id).replace('-', '')[:16]}{index}",
            "name": f"Price update {update['target_ref']} #{index}",
            "rule_type": "FIXED_PRICE",
            "priority": int(update["priority"]),
            "start_date": update["effective_start"],
            "end_date": update["effective_end"],
            "is_active": True,
            "conflict_policy": "HIGHER_PRIORITY",
            "currency_code": update["currency_code"],
            "generated_from_update_id": update_id,
        }
        try:
            rule = await self.insert_row(t.price_rule, tenant_id, rule_values, actor_id)
        except IntegrityError:
            rule = (
                await self.list_rows(t.price_rule, tenant_id, filters={"generated_from_update_id": update_id}, limit=1)
            )[0]
        rule_id = UUID(str(rule["id"]))
        await self.insert_row(
            t.price_rule_action,
            tenant_id,
            {"price_rule_id": rule_id, "action_type": "FIXED_PRICE", "value": detail_price, "apply_to": "UNIT"},
            actor_id,
        )
        criterion = "ITEM_CATEGORY" if update["update_type"] == "ITEM_CATEGORY" else "CUSTOMER"
        if update["update_type"] == "CUSTOMER_GROUP":
            criterion = "CUSTOMER_GROUP"
        value_text = (
            str(detail["item_category_id"])
            if detail.get("item_category_id")
            else str(detail.get("customer_group_id") or detail.get("external_customer_ref") or "")
        )
        await self.insert_row(
            t.price_rule_criteria,
            tenant_id,
            {
                "price_rule_id": rule_id,
                "criterion": criterion,
                "operator": "IN",
                "value_text": value_text,
                "group_no": 1,
            },
            actor_id,
        )
        await self.emit(
            tenant_id=tenant_id,
            event_type="pricing.rule.changed",
            aggregate_type="pricing.price_rule",
            aggregate_id=rule_id,
            payload={"price_rule_id": str(rule_id), "change": "GENERATED", "priority": int(update["priority"])},
        )
        return rule

    async def apply_price_update(
        self, *, tenant_id: str | UUID, update_id: str | UUID, as_of: date, actor_id: str | UUID | None
    ) -> dict[str, Any]:
        tid = _as_uuid(tenant_id)
        result = await self.session.execute(
            select(t.price_update)
            .where(t.price_update.c.tenant_id == tid, t.price_update.c.id == _as_uuid(update_id))
            .with_for_update()
        )
        mapping = result.mappings().first()
        if mapping is None:
            raise PricingRepositoryError("PRICE_UPDATE_NOT_FOUND", "price update not found", status_code=404)
        update = dict(mapping)
        if update["status"] not in {"PENDING", "APPLIED"}:
            raise PricingRepositoryError(
                "INVALID_STATE_TRANSITION", f"cannot apply update in status {update['status']}", status_code=409
            )
        effective_start = date.fromisoformat(str(update["effective_start"]))
        if effective_start > as_of:
            raise PricingRepositoryError(
                "PRICE_UPDATE_NOT_EFFECTIVE", "update effective start is in the future", status_code=409
            )
        if update["status"] == "APPLIED":
            return {"price_update": _row(update), "applied": False, "rows_written": 0}
        header = _row(update)
        details = await self.list_rows(
            t.price_update_detail,
            tid,
            filters={"price_update_id": _as_uuid(update_id)},
            order_by=(t.price_update_detail.c.qty_min, t.price_update_detail.c.id),
        )
        rows_written = 0
        pre_images: list[dict[str, Any]] = []
        level_id = UUID(str(update["price_level_id"]))
        currency = str(update["currency_code"])
        effective_end = date.fromisoformat(str(update["effective_end"])) if update["effective_end"] else None
        for index, detail in enumerate(details, start=1):
            if detail.get("item_id"):
                item_id = UUID(str(detail["item_id"]))
                qty_min = Decimal(str(detail.get("qty_min") or 0))
                qty_max = Decimal(str(detail["qty_max"])) if detail.get("qty_max") is not None else None
                new_values = {
                    "qty_max": qty_max,
                    "unit_price": Decimal(str(detail["new_price"])),
                    "effective_from": effective_start,
                    "effective_to": effective_end,
                    "is_active": True,
                }
                # The item_price natural key includes qty_min (not the effective window), so an
                # effective-dated update materialises by updating the existing band row in place and
                # recording the pre-image for revert (append-only audit is the evidence trail).
                existing_natural = (
                    (
                        await self.session.execute(
                            select(t.item_price).where(
                                t.item_price.c.tenant_id == tid,
                                t.item_price.c.item_id == item_id,
                                t.item_price.c.price_level_id == level_id,
                                t.item_price.c.currency_code == currency,
                                t.item_price.c.qty_min == qty_min,
                            )
                        )
                    )
                    .mappings()
                    .first()
                )
                if existing_natural is not None:
                    before = dict(existing_natural)
                    pre_images.append(
                        {
                            "item_price_id": str(before["id"]),
                            "values": {
                                key: _iso(before[key])
                                for key in ("qty_max", "unit_price", "effective_from", "effective_to", "is_active")
                            },
                        }
                    )
                    other_rows = await self.session.execute(
                        select(t.item_price).where(
                            t.item_price.c.tenant_id == tid,
                            t.item_price.c.item_id == item_id,
                            t.item_price.c.price_level_id == level_id,
                            t.item_price.c.currency_code == currency,
                            t.item_price.c.id != UUID(str(before["id"])),
                            t.item_price.c.is_active.is_(True),
                        )
                    )
                    for row in other_rows.mappings().all():
                        await self._link(
                            tid,
                            link_type="SUPERSEDED_BY",
                            from_id=_as_uuid(update_id),
                            to_type="ITEM_PRICE",
                            to_id=UUID(str(row["id"])),
                            actor_id=_as_uuid(actor_id) if actor_id else None,
                        )
                        await self.update_row(t.item_price, tid, UUID(str(row["id"])), {"is_active": False}, actor_id)
                    price_row = await self.update_row(t.item_price, tid, UUID(str(before["id"])), new_values, actor_id)
                else:
                    other_rows = await self.session.execute(
                        select(t.item_price).where(
                            t.item_price.c.tenant_id == tid,
                            t.item_price.c.item_id == item_id,
                            t.item_price.c.price_level_id == level_id,
                            t.item_price.c.currency_code == currency,
                            t.item_price.c.is_active.is_(True),
                        )
                    )
                    for row in other_rows.mappings().all():
                        await self._link(
                            tid,
                            link_type="SUPERSEDED_BY",
                            from_id=_as_uuid(update_id),
                            to_type="ITEM_PRICE",
                            to_id=UUID(str(row["id"])),
                            actor_id=_as_uuid(actor_id) if actor_id else None,
                        )
                        await self.update_row(t.item_price, tid, UUID(str(row["id"])), {"is_active": False}, actor_id)
                    price_row = await self.insert_row(
                        t.item_price,
                        tid,
                        {
                            "item_id": item_id,
                            "price_level_id": level_id,
                            "currency_code": currency,
                            "qty_min": qty_min,
                            **new_values,
                        },
                        actor_id,
                    )
                    await self._link(
                        tid,
                        link_type="APPLIED_TO",
                        from_id=_as_uuid(update_id),
                        to_type="ITEM_PRICE",
                        to_id=UUID(str(price_row["id"])),
                        actor_id=_as_uuid(actor_id) if actor_id else None,
                    )
                assert price_row is not None
                rows_written += 1
                await self.emit(
                    tenant_id=tid,
                    event_type="pricing.price.updated",
                    aggregate_type="pricing.item_price",
                    aggregate_id=price_row["id"],
                    payload={
                        "item_price_id": price_row["id"],
                        "item_id": str(item_id),
                        "price_level_id": str(level_id),
                        "unit_price": price_row["unit_price"],
                    },
                )
            else:
                await self._generated_rule(
                    tenant_id=tid,
                    update=header,
                    detail=detail,
                    actor_id=_as_uuid(actor_id) if actor_id else None,
                    index=index,
                )
                rows_written += 1
        updated = await self.update_row(
            t.price_update, tid, _as_uuid(update_id), {"status": "APPLIED", "applied_at": _now()}, actor_id
        )
        if pre_images:
            await self.audit(
                tenant_id=tid,
                actor_id=actor_id,
                action="pricing.price_update.materialised",
                resource_type="pricing.price_update",
                resource_id=_as_uuid(update_id),
                diff={"rows": pre_images},
            )
        await self.emit(
            tenant_id=tid,
            event_type="pricing.price_update.applied",
            aggregate_type="pricing.price_update",
            aggregate_id=_as_uuid(update_id),
            payload={
                "price_update_id": str(update_id),
                "update_type": update["update_type"],
                "target_ref": update["target_ref"],
                "effective_start": str(update["effective_start"]),
                "effective_end": update["effective_end"],
                "rows_written": rows_written,
            },
        )
        return {"price_update": updated, "applied": True, "rows_written": rows_written}

    async def revert_price_update(
        self, *, tenant_id: str | UUID, update_id: str | UUID, reason: str, actor_id: str | UUID | None
    ) -> dict[str, Any]:
        tid = _as_uuid(tenant_id)
        update_row = await self.get_price_update(tid, update_id)
        if update_row is None:
            raise PricingRepositoryError("PRICE_UPDATE_NOT_FOUND", "price update not found", status_code=404)
        header = update_row["price_update"]
        if header["status"] != "APPLIED":
            raise PricingRepositoryError(
                "INVALID_STATE_TRANSITION", "only APPLIED updates can be reverted", status_code=409
            )
        link_table = platform_models.metadata.tables["platform.document_link"]
        links = await self.session.execute(
            select(link_table.c.link_type, link_table.c.to_type, link_table.c.to_id).where(
                link_table.c.tenant_id == tid,
                link_table.c.from_type == "PRICE_UPDATE",
                link_table.c.from_id == _as_uuid(update_id),
            )
        )
        reverted = 0
        # Restore the pre-images recorded when the update materialised an existing band row.
        audit_table = platform_models.metadata.tables["platform.audit_event"]
        audit_rows = await self.session.execute(
            select(audit_table.c.diff).where(
                audit_table.c.tenant_id == tid,
                audit_table.c.action == "pricing.price_update.materialised",
                audit_table.c.resource_id == str(_as_uuid(update_id)),
            )
        )
        restored_ids: set[str] = set()
        for audit_row in audit_rows.mappings().all():
            for pre_image in (audit_row["diff"] or {}).get("rows", []):
                values = pre_image.get("values") or {}
                restored_ids.add(str(pre_image["item_price_id"]))
                restored = dict(values)
                if restored.get("effective_from") is not None:
                    restored["effective_from"] = date.fromisoformat(str(restored["effective_from"]))
                if restored.get("effective_to") is not None:
                    restored["effective_to"] = date.fromisoformat(str(restored["effective_to"]))
                if restored.get("unit_price") is not None and not isinstance(restored["unit_price"], Decimal):
                    restored["unit_price"] = Decimal(str(restored["unit_price"]))
                if restored.get("qty_max") is not None and not isinstance(restored["qty_max"], Decimal):
                    restored["qty_max"] = Decimal(str(restored["qty_max"]))
                await self.update_row(t.item_price, tid, UUID(str(pre_image["item_price_id"])), restored, actor_id)
                reverted += 1
        for link in links.mappings().all():
            if link["to_type"] != "ITEM_PRICE":
                continue
            target = UUID(str(link["to_id"]))
            if str(target) in restored_ids:
                continue
            await self.update_row(
                t.item_price, tid, target, {"is_active": link["link_type"] == "SUPERSEDED_BY"}, actor_id
            )
            reverted += 1
        generated_rules = await self.list_rows(
            t.price_rule, tid, filters={"generated_from_update_id": _as_uuid(update_id)}, limit=500
        )
        for rule in generated_rules:
            await self.update_row(t.price_rule, tid, UUID(str(rule["id"])), {"is_active": False}, actor_id)
        updated = await self.update_row(t.price_update, tid, _as_uuid(update_id), {"status": "REVERTED"}, actor_id)
        await self.audit(
            tenant_id=tid,
            actor_id=actor_id,
            action="pricing.price_update.reverted",
            resource_type="pricing.price_update",
            resource_id=_as_uuid(update_id),
            diff={"reason": reason, "rows": reverted},
        )
        await self.emit(
            tenant_id=tid,
            event_type="pricing.price_update.reverted",
            aggregate_type="pricing.price_update",
            aggregate_id=_as_uuid(update_id),
            payload={
                "price_update_id": str(update_id),
                "target_ref": header["target_ref"],
                "reverted_at": _now().isoformat(),
            },
        )
        return {"price_update": updated, "reverted_rows": reverted}

    async def cancel_price_update(
        self, *, tenant_id: str | UUID, update_id: str | UUID, reason: str, actor_id: str | UUID | None
    ) -> dict[str, Any]:
        row = await self.get_row(t.price_update, tenant_id, update_id)
        if row is None:
            raise PricingRepositoryError("PRICE_UPDATE_NOT_FOUND", "price update not found", status_code=404)
        if row["status"] not in {"PENDING", "APPLIED"}:
            raise PricingRepositoryError(
                "INVALID_STATE_TRANSITION", f"cannot cancel update in status {row['status']}", status_code=409
            )
        if row["status"] == "APPLIED":
            await self.revert_price_update(tenant_id=tenant_id, update_id=update_id, reason=reason, actor_id=actor_id)
        updated = await self.update_row(t.price_update, tenant_id, update_id, {"status": "CANCELLED"}, actor_id)
        await self.audit(
            tenant_id=_as_uuid(tenant_id),
            actor_id=actor_id,
            action="pricing.price_update.cancelled",
            resource_type="pricing.price_update",
            resource_id=_as_uuid(update_id),
            diff={"reason": reason},
        )
        return {"price_update": updated}

    # ---------------------------------------------------------------- bulk / lists ------------
    async def bulk_upsert_prices(
        self,
        *,
        tenant_id: str | UUID,
        rows: Sequence[Mapping[str, Any]],
        effective_from: date,
        effective_to: date | None,
        actor_id: str | UUID | None,
        price_level_id: str | UUID | None,
        currency_code: str,
    ) -> dict[str, Any]:
        tid = _as_uuid(tenant_id)
        await self.ensure_default_level(tid)
        level_id = (
            _as_uuid(price_level_id) if price_level_id else UUID(str((await self.ensure_default_level(tid))["id"]))
        )
        header = await self.insert_row(
            t.price_update,
            tid,
            {
                "update_type": "ITEM",
                "target_ref": f"BULK-{uuid.uuid4().hex[:12]}",
                "price_level_id": level_id,
                "currency_code": currency_code,
                "effective_start": effective_from,
                "effective_end": effective_to,
                "status": "APPLIED",
                "priority": 0,
                "applied_at": _now(),
                "overlap_policy": "REJECT",
            },
            actor_id,
        )
        applied: list[str] = []
        rejected: list[dict[str, Any]] = []
        for index, row in enumerate(rows):
            try:
                async with self.session.begin_nested():
                    price_row = await self.insert_row(
                        t.item_price,
                        tid,
                        {
                            "item_id": _as_uuid(row["item_id"]),
                            "price_level_id": level_id,
                            "currency_code": currency_code,
                            "qty_min": Decimal(str(row.get("qty_min") or 0)),
                            "qty_max": Decimal(str(row["qty_max"])) if row.get("qty_max") is not None else None,
                            "unit_price": Decimal(str(row["unit_price"])),
                            "effective_from": effective_from,
                            "effective_to": effective_to,
                            "is_active": True,
                        },
                        actor_id,
                    )
            except (IntegrityError, KeyError, ValueError) as exc:
                rejected.append(
                    {"index": index, "code": "ITEM_PRICE_REJECTED", "message": str(getattr(exc, "orig", exc))[:300]}
                )
                continue
            applied.append(str(price_row["id"]))
        await self.emit(
            tenant_id=tid,
            event_type="pricing.price.updated",
            aggregate_type="pricing.price_update",
            aggregate_id=header["id"],
            payload={"price_update_id": header["id"], "applied": len(applied), "rejected": len(rejected)},
        )
        return {"price_update_id": header["id"], "applied": len(applied), "rejected": rejected, "row_ids": applied}

    async def price_list_rows(
        self,
        *,
        tenant_id: str | UUID,
        price_level_id: str | UUID | None,
        currency_code: str,
        as_of: date,
        limit: int = 1000,
    ) -> dict[str, Any]:
        tid = _as_uuid(tenant_id)
        await self.ensure_default_level(tid)
        level = price_level_id or (await self.ensure_default_level(tid))["id"]
        rows = await self.session.execute(
            text(
                """
                SELECT p.id, p.item_id, i.item_number, i.display_name, p.price_level_id, p.currency_code,
                       p.qty_min, p.qty_max, p.unit_price, p.effective_from, p.effective_to
                  FROM pricing.item_price p
                  JOIN item_master.item i ON i.tenant_id = p.tenant_id AND i.id = p.item_id
                 WHERE p.tenant_id = :tenant_id AND p.price_level_id = :level_id
                   AND p.currency_code = :currency AND p.is_active
                   AND p.effective_from <= :as_of AND (p.effective_to IS NULL OR p.effective_to >= :as_of)
                 ORDER BY i.item_number, p.qty_min
                 LIMIT :limit
                """
            ),
            {"tenant_id": str(tid), "level_id": str(level), "currency": currency_code, "as_of": as_of, "limit": limit},
        )
        items = [{key: _iso(value) for key, value in dict(row).items()} for row in rows.mappings().all()]
        return {
            "items": items,
            "price_level_id": str(level),
            "currency_code": currency_code,
            "as_of": as_of.isoformat(),
        }

    async def item_price_rows(
        self, tenant_id: str | UUID, filters: Mapping[str, Any], limit: int = 500
    ) -> list[dict[str, Any]]:
        return await self.list_rows(t.item_price, tenant_id, filters=filters, limit=limit)

    async def schedule_with_breaks(self, tenant_id: str | UUID, schedule_id: str | UUID) -> dict[str, Any] | None:
        schedule = await self.get_row(t.price_schedule, tenant_id, schedule_id)
        if schedule is None:
            return None
        breaks = await self.list_rows(
            t.price_schedule_break,
            tenant_id,
            filters={"price_schedule_id": _as_uuid(schedule_id)},
            order_by=(t.price_schedule_break.c.qty_from,),
        )
        return {"schedule": schedule, "breaks": breaks}

    async def rule_with_parts(self, tenant_id: str | UUID, rule_id: str | UUID) -> dict[str, Any] | None:
        rule = await self.get_row(t.price_rule, tenant_id, rule_id)
        if rule is None:
            return None
        criteria = await self.list_rows(
            t.price_rule_criteria,
            tenant_id,
            filters={"price_rule_id": _as_uuid(rule_id)},
            order_by=(t.price_rule_criteria.c.group_no,),
        )
        actions = await self.list_rows(
            t.price_rule_action, tenant_id, filters={"price_rule_id": _as_uuid(rule_id)}, limit=10
        )
        return {"price_rule": rule, "criteria": criteria, "actions": actions}

    # ---------------------------------------------------------------- jobs --------------------
    async def enqueue_job(
        self,
        *,
        tenant_id: str | UUID,
        name: str,
        run_key: str,
        params: Mapping[str, Any] | None,
        actor_id: str | UUID | None,
    ) -> dict[str, Any]:
        tid = _as_uuid(tenant_id)
        definition = (
            (
                await self.session.execute(
                    select(platform_models.metadata.tables["platform.job_definition"]).where(
                        platform_models.metadata.tables["platform.job_definition"].c.name == name,
                        platform_models.metadata.tables["platform.job_definition"].c.tenant_id.in_(
                            [tid, UUID("00000000-0000-0000-0000-000000000000")]
                        ),
                    )
                )
            )
            .mappings()
            .first()
        )
        if definition is None:
            raise PricingRepositoryError("JOB_NOT_FOUND", "pricing job definition not found", status_code=404)
        job_definition_table = platform_models.metadata.tables["platform.job_definition"]
        if UUID(str(definition["tenant_id"])) != tid:
            # Seed rows live under the SYSTEM tenant; job_run requires a tenant-scoped definition.
            existing_tenant_definition = (
                (
                    await self.session.execute(
                        select(job_definition_table).where(
                            job_definition_table.c.tenant_id == tid,
                            job_definition_table.c.name == name,
                        )
                    )
                )
                .mappings()
                .first()
            )
            if existing_tenant_definition is not None:
                definition = existing_tenant_definition
            else:
                tenant_definition_id = uuid.uuid4()
                now_definition = _now()
                await self.session.execute(
                    job_definition_table.insert().values(
                        id=tenant_definition_id,
                        tenant_id=tid,
                        name=name,
                        queue=definition["queue"],
                        handler=definition["handler"],
                        cron_expr=definition["cron_expr"],
                        timeout_seconds=definition["timeout_seconds"],
                        max_attempts=definition["max_attempts"],
                        singleton=definition["singleton"],
                        is_enabled=definition["is_enabled"],
                        created_at=now_definition,
                        updated_at=now_definition,
                        version=1,
                    )
                )
                definition = {  # type: ignore[assignment]
                    **dict(definition),
                    "id": tenant_definition_id,
                    "tenant_id": tid,
                }
        run_table = platform_models.metadata.tables["platform.job_run"]
        existing = (
            (
                await self.session.execute(
                    select(run_table).where(run_table.c.tenant_id == tid, run_table.c.run_key == run_key)
                )
            )
            .mappings()
            .first()
        )
        if existing is not None:
            return {"job_run_id": str(existing["id"]), "status": existing["status"], "deduplicated": True}
        run_id = uuid.uuid4()
        now = _now()
        await self.session.execute(
            run_table.insert().values(
                id=run_id,
                tenant_id=tid,
                job_definition_id=definition["id"],
                run_key=run_key,
                status="QUEUED",
                attempt=1,
                started_at=None,
                finished_at=None,
                cursor={"params": dict(params or {})} if params else None,
                result_summary=None,
                error=None,
                correlation_id=uuid.uuid4(),
                created_at=now,
                updated_at=now,
                version=1,
            )
        )
        await self.audit(
            tenant_id=tid,
            actor_id=actor_id,
            action="pricing.job.queued",
            resource_type="platform.job_run",
            resource_id=run_id,
            diff={"job": name},
        )
        return {"job_run_id": str(run_id), "status": "QUEUED", "deduplicated": False}

    async def apply_due_updates(self, *, now: datetime | None = None) -> dict[str, int]:
        moment = now or _now()
        as_of = moment.date()
        applied = 0
        reverted = 0
        due = (
            (
                await self.session.execute(
                    select(t.price_update).where(
                        t.price_update.c.status == "PENDING", t.price_update.c.effective_start <= as_of
                    )
                )
            )
            .mappings()
            .all()
        )
        for row in due:
            try:
                await self.apply_price_update(
                    tenant_id=row["tenant_id"], update_id=UUID(str(row["id"])), as_of=as_of, actor_id=None
                )
                applied += 1
            except PricingRepositoryError:
                continue
        expired = (
            (
                await self.session.execute(
                    select(t.price_update).where(
                        t.price_update.c.status == "APPLIED",
                        t.price_update.c.effective_end.is_not(None),
                        t.price_update.c.effective_end < as_of,
                    )
                )
            )
            .mappings()
            .all()
        )
        for row in expired:
            try:
                await self.revert_price_update(
                    tenant_id=row["tenant_id"], update_id=UUID(str(row["id"])), reason="scheduled end", actor_id=None
                )
                reverted += 1
            except PricingRepositoryError:
                continue
        return {"applied": applied, "reverted": reverted}

    async def recompute_dynamic_collections(
        self, *, tenant_id: str | UUID, actor_id: str | UUID | None
    ) -> dict[str, Any]:
        tid = _as_uuid(tenant_id)
        collections = await self.list_rows(t.item_collection, tid, filters={"is_dynamic": True}, limit=500)
        recomputed = 0
        errors: list[dict[str, Any]] = []
        for collection in collections:
            try:
                criteria = collection.get("criteria") or {}
                clauses = ["i.tenant_id = :tenant_id"]
                params: dict[str, Any] = {"tenant_id": str(tid)}
                if criteria.get("item_type_code"):
                    clauses.append("t.code = :item_type_code")
                    params["item_type_code"] = criteria["item_type_code"]
                if criteria.get("item_category_id"):
                    clauses.append("i.item_category_id = :item_category_id")
                    params["item_category_id"] = criteria["item_category_id"]
                if criteria.get("is_active") is not None:
                    clauses.append("i.is_active = :is_active")
                    params["is_active"] = bool(criteria["is_active"])
                if criteria.get("item_number_prefix"):
                    clauses.append("i.item_number LIKE :prefix")
                    params["prefix"] = f"{criteria['item_number_prefix']}%"
                rows = await self.session.execute(
                    text(
                        "SELECT i.id FROM item_master.item i "
                        "JOIN item_master.item_type_definition t ON t.tenant_id = i.tenant_id AND t.id = i.item_type_id "
                        f"WHERE {' AND '.join(clauses)} LIMIT 5000"
                    ),
                    params,
                )
                member_ids = [UUID(str(row["id"])) for row in rows.mappings().all()]
                await self.session.execute(
                    delete(t.item_collection_member).where(
                        t.item_collection_member.c.tenant_id == tid,
                        t.item_collection_member.c.item_collection_id == _as_uuid(collection["id"]),
                    )
                )
                for member_id in member_ids:
                    await self.insert_row(
                        t.item_collection_member,
                        tid,
                        {"item_collection_id": _as_uuid(collection["id"]), "item_id": member_id},
                        actor_id,
                    )
                await self.update_row(
                    t.item_collection, tid, _as_uuid(collection["id"]), {"criteria": criteria}, actor_id
                )
                recomputed += 1
            except Exception as exc:  # keep per-collection isolation
                errors.append({"item_collection_id": collection["id"], "error": str(exc)[:300]})
        return {"recomputed": recomputed, "errors": errors}

    async def expire_coupons(
        self, *, tenant_id: str | UUID, as_of: date, actor_id: str | UUID | None
    ) -> dict[str, Any]:
        tid = _as_uuid(tenant_id)
        expired = await self.session.execute(
            update(t.coupon)
            .where(t.coupon.c.tenant_id == tid, t.coupon.c.end_date < as_of, t.coupon.c.is_active.is_(True))
            .values(is_active=False, updated_at=_now(), version=t.coupon.c.version + 1)
        )
        link_table = platform_models.metadata.tables["platform.document_link"]
        drift: list[dict[str, Any]] = []
        coupons = await self.list_rows(t.coupon, tid, limit=2000)
        for coupon in coupons:
            count = (
                await self.session.execute(
                    select(func.count())
                    .select_from(link_table)
                    .where(
                        link_table.c.tenant_id == tid,
                        link_table.c.from_type == "COUPON",
                        link_table.c.from_id == _as_uuid(coupon["id"]),
                        link_table.c.link_type == "APPLIED_TO",
                    )
                )
            ).scalar_one()
            if int(coupon["uses_count"]) != int(count):
                drift.append(
                    {"coupon_id": coupon["id"], "uses_count": coupon["uses_count"], "redemption_links": int(count)}
                )
        return {"expired": int(getattr(expired, "rowcount", 0) or 0), "drift": drift}
