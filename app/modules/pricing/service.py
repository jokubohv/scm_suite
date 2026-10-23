"""FS-02 pricing use-case service: validation, lifecycle rules, jobs orchestration.

The service is the only caller of :class:`PricingRepository`; the API router is a thin transport layer.
Rules implemented here (FS-02 §5, DATA-MODEL §20):

* master-data validation (codes, currencies, date ranges, quantity bands, schedule modes/breaks);
* effective-dated ``item_price`` writes with overlap guard (DB trigger) mapped to
  ``409 PRICE_OVERLAP``; no hard deletes (master data is inactivated);
* price rules/actions/criteria and dynamic collection lifecycle (``pricing.rule.changed``);
* coupons (window/uses/scope) and idempotent redemption via ``platform.document_link``;
* effective-dated price updates: create (overlap policy REJECT/SUPERSEDE), apply/revert/cancel,
  scheduled job application;
* deterministic resolution via the pure engine + ``pricing.price.updated`` events;
* bulk upsert, level swap and purchase-price/mass-update job enqueues (jobs are queued here;
  workers own execution — the pricing module exposes the handlers in the repository).
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.models import platform as platform_models
from app.modules.pricing import tables as t
from app.modules.pricing.engine import (
    CostContext,
    EngineError,
    PriceLineInput,
    PriceRequest,
)
from app.modules.pricing.item_port import ItemMasterPort, SqlItemMasterPort
from app.modules.pricing.repository import (
    PricingRepository,
)

__all__ = ["PricingService", "PricingServiceError"]

CODE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")
CURRENCY_RE = re.compile(r"^[A-Z]{3}$")
SCHEDULE_MODES = frozenset({"LINE", "OVERALL_ITEM", "PARENT", "SCHEDULE"})
RULE_TYPES = frozenset({"PRICE_LEVEL", "DISCOUNT", "MARKUP", "FIXED_PRICE"})
CONFLICT_POLICIES = frozenset({"HIGHER_PRIORITY", "FIRST_MATCH", "STOP"})
ACTION_TYPES = frozenset(
    {
        "SET_PRICE_LEVEL",
        "PERCENT_DISCOUNT",
        "AMOUNT_DISCOUNT",
        "PERCENT_MARKUP",
        "AMOUNT_MARKUP",
        "FIXED_PRICE",
        "COST_PLUS",
    }
)
COUPON_TYPES = frozenset({"PERCENT", "AMOUNT"})
PRICE_UPDATE_TYPES = frozenset({"ITEM", "ITEM_CATEGORY", "CUSTOMER", "CUSTOMER_GROUP"})
OVERLAP_POLICIES = frozenset({"REJECT", "SUPERSEDE"})


class PricingServiceError(Exception):
    def __init__(
        self, code: str, message: str, *, status_code: int = 422, details: Mapping[str, Any] | None = None
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = dict(details or {})


def _as_uuid(value: str | UUID | None) -> UUID:
    if value is None:
        raise PricingServiceError("VALIDATION_FAILED", "a UUID is required", details={"value": None})
    try:
        return value if isinstance(value, UUID) else UUID(str(value))
    except (ValueError, TypeError):
        raise PricingServiceError("VALIDATION_FAILED", "invalid UUID", details={"value": str(value)}) from None


def _decimal(value: Any, *, field: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise PricingServiceError("VALIDATION_FAILED", f"{field} must be decimal", details={"field": field}) from None
    if not result.is_finite():
        raise PricingServiceError("VALIDATION_FAILED", f"{field} must be finite", details={"field": field})
    return result


def _date(value: Any, *, field: str) -> date:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        raise PricingServiceError(
            "VALIDATION_FAILED", f"{field} must be YYYY-MM-DD", details={"field": field}
        ) from None


def _code(value: Any, *, field: str = "code") -> str:
    candidate = str(value or "").strip().upper()
    if not CODE_RE.match(candidate):
        raise PricingServiceError("VALIDATION_FAILED", f"{field} has invalid format", details={"field": field})
    return candidate


def _currency(value: Any, *, field: str = "currency_code") -> str:
    candidate = str(value or "").strip().upper()
    if not CURRENCY_RE.match(candidate):
        raise PricingServiceError("VALIDATION_FAILED", f"{field} must be a 3-letter code", details={"field": field})
    return candidate


def _int(value: Any, *, field: str, minimum: int = 0, maximum: int | None = None) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError):
        raise PricingServiceError(
            "VALIDATION_FAILED", f"{field} must be an integer", details={"field": field}
        ) from None
    if result < minimum or (maximum is not None and result > maximum):
        raise PricingServiceError("VALIDATION_FAILED", f"{field} is out of range", details={"field": field})
    return result


class PricingService:
    def __init__(self, session: AsyncSession, *, item_port: ItemMasterPort | None = None) -> None:
        self.session = session
        self.repo = PricingRepository(session, item_port or SqlItemMasterPort(session))

    # ---------------------------------------------------------------- currency check ----------
    async def _currency_exists(self, currency_code: str) -> bool:
        table = platform_models.metadata.tables["platform.currency"]
        result = await self.session.execute(select(table.c.code).where(table.c.code == currency_code))
        return result.scalar_one_or_none() is not None

    # ---------------------------------------------------------------- master CRUD -------------
    async def list_master(
        self, table: Any, tenant_id: str, *, filters: Mapping[str, Any] | None = None, limit: int = 200
    ) -> list[dict[str, Any]]:
        return await self.repo.list_rows(table, tenant_id, filters=filters, limit=min(limit, 500))

    async def get_master(self, table: Any, tenant_id: str, row_id: str) -> dict[str, Any]:
        row = await self.repo.get_row(table, tenant_id, row_id)
        if row is None:
            raise PricingServiceError("NOT_FOUND", "resource not found", status_code=404)
        return row

    async def create_price_level(
        self, *, tenant_id: str, values: Mapping[str, Any], actor_id: str | UUID | None
    ) -> dict[str, Any]:
        currency = _currency(values.get("currency_code") or await self.repo.tenant_currency(tenant_id))
        if not await self._currency_exists(currency):
            raise PricingServiceError(
                "VALIDATION_FAILED", "currency is not an active platform currency", details={"field": "currency_code"}
            )
        payload = {
            "code": _code(values.get("code")),
            "name": str(values.get("name") or "").strip() or _code(values.get("code")),
            "currency_code": currency,
            "is_default": bool(values.get("is_default", False)),
            "is_inactive": bool(values.get("is_inactive", False)),
            "sort_order": _int(values.get("sort_order", 0), field="sort_order"),
        }
        if payload["is_default"]:
            await self._clear_default_level(tenant_id)
        try:
            row = await self.repo.insert_row(t.price_level, tenant_id, payload, actor_id)
        except IntegrityError as exc:
            raise PricingServiceError("DUPLICATE_CODE", "price level code already exists", status_code=409) from exc
        await self.repo.audit(
            tenant_id=_as_uuid(tenant_id),
            actor_id=actor_id,
            action="pricing.price_level.created",
            resource_type="pricing.price_level",
            resource_id=row["id"],
        )
        return row

    async def _clear_default_level(self, tenant_id: str) -> None:
        current = await self.session.execute(
            select(t.price_level.c.id).where(
                t.price_level.c.tenant_id == _as_uuid(tenant_id), t.price_level.c.is_default.is_(True)
            )
        )
        for (row_id,) in current.all():
            await self.repo.update_row(t.price_level, tenant_id, row_id, {"is_default": False}, None)

    async def update_price_level(
        self,
        *,
        tenant_id: str,
        row_id: str,
        values: Mapping[str, Any],
        actor_id: str | UUID | None,
        expected_version: int | None,
    ) -> dict[str, Any]:
        current = await self.get_master(t.price_level, tenant_id, row_id)
        payload: dict[str, Any] = {}
        if "name" in values:
            payload["name"] = str(values["name"]).strip()
        if "sort_order" in values:
            payload["sort_order"] = _int(values["sort_order"], field="sort_order")
        if "is_inactive" in values:
            payload["is_inactive"] = bool(values["is_inactive"])
        if "currency_code" in values:
            currency = _currency(values["currency_code"])
            if not await self._currency_exists(currency):
                raise PricingServiceError("VALIDATION_FAILED", "currency is not an active platform currency")
            payload["currency_code"] = currency
        if "is_default" in values:
            if values["is_default"]:
                await self._clear_default_level(tenant_id)
            payload["is_default"] = bool(values["is_default"])
        row = await self.repo.update_row(
            t.price_level, tenant_id, row_id, payload, actor_id, expected_version=expected_version
        )
        if row is None:
            raise PricingServiceError("NOT_FOUND", "price level not found", status_code=404)
        await self.repo.audit(
            tenant_id=_as_uuid(tenant_id),
            actor_id=actor_id,
            action="pricing.price_level.updated",
            resource_type="pricing.price_level",
            resource_id=row_id,
            diff={"before": current, "after": row},
        )
        return row

    async def create_item_group(
        self, *, tenant_id: str, values: Mapping[str, Any], actor_id: str | UUID | None
    ) -> dict[str, Any]:
        payload = {
            "code": _code(values.get("code")),
            "name": str(values.get("name") or "").strip() or _code(values.get("code")),
            "is_inactive": bool(values.get("is_inactive", False)),
        }
        try:
            row = await self.repo.insert_row(t.item_group, tenant_id, payload, actor_id)
        except IntegrityError as exc:
            raise PricingServiceError("DUPLICATE_CODE", "item group code already exists", status_code=409) from exc
        await self.repo.audit(
            tenant_id=_as_uuid(tenant_id),
            actor_id=actor_id,
            action="pricing.item_group.created",
            resource_type="pricing.item_group",
            resource_id=row["id"],
        )
        return row

    async def create_customer_group(
        self, *, tenant_id: str, values: Mapping[str, Any], actor_id: str | UUID | None
    ) -> dict[str, Any]:
        payload = {
            "code": _code(values.get("code")),
            "name": str(values.get("name") or "").strip() or _code(values.get("code")),
            "is_inactive": bool(values.get("is_inactive", False)),
            "notes": values.get("notes"),
        }
        try:
            row = await self.repo.insert_row(t.customer_group, tenant_id, payload, actor_id)
        except IntegrityError as exc:
            raise PricingServiceError("DUPLICATE_CODE", "customer group code already exists", status_code=409) from exc
        await self.repo.audit(
            tenant_id=_as_uuid(tenant_id),
            actor_id=actor_id,
            action="pricing.customer_group.created",
            resource_type="pricing.customer_group",
            resource_id=row["id"],
        )
        return row

    async def add_item_group_member(
        self, *, tenant_id: str, group_id: str, item_id: str, actor_id: str | UUID | None
    ) -> dict[str, Any]:
        await self.get_master(t.item_group, tenant_id, group_id)
        existing = await self.repo.list_rows(
            t.item_group_member, tenant_id, filters={"item_id": _as_uuid(item_id)}, limit=5
        )
        if existing and any(str(row["item_group_id"]) != str(_as_uuid(group_id)) for row in existing):
            raise PricingServiceError(
                "ITEM_GROUP_CONFLICT", "item already belongs to another pricing item group", status_code=409
            )
        try:
            await self.repo.insert_row(
                t.item_group_member,
                tenant_id,
                {"item_group_id": _as_uuid(group_id), "item_id": _as_uuid(item_id)},
                actor_id,
            )
        except IntegrityError:
            pass  # idempotent membership
        return {"item_group_id": str(_as_uuid(group_id)), "item_id": str(_as_uuid(item_id)), "added": True}

    async def add_customer_group_member(
        self, *, tenant_id: str, group_id: str, external_ref: str, actor_id: str | UUID | None
    ) -> dict[str, Any]:
        await self.get_master(t.customer_group, tenant_id, group_id)
        ref = str(external_ref or "").strip()
        if not ref:
            raise PricingServiceError("VALIDATION_FAILED", "external_customer_ref is required")
        try:
            await self.repo.insert_row(
                t.customer_group_member,
                tenant_id,
                {"customer_group_id": _as_uuid(group_id), "external_customer_ref": ref},
                actor_id,
            )
        except IntegrityError:
            pass
        return {"customer_group_id": str(_as_uuid(group_id)), "external_customer_ref": ref, "added": True}

    async def create_pricing_group(
        self, *, tenant_id: str, values: Mapping[str, Any], actor_id: str | UUID | None
    ) -> dict[str, Any]:
        for table, key in (
            (t.item_group, "item_group_id"),
            (t.customer_group, "customer_group_id"),
            (t.price_level, "price_level_id"),
        ):
            await self.get_master(table, tenant_id, str(values.get(key)))
        payload = {
            "item_group_id": _as_uuid(values["item_group_id"]),
            "customer_group_id": _as_uuid(values["customer_group_id"]),
            "price_level_id": _as_uuid(values["price_level_id"]),
            "is_default": bool(values.get("is_default", False)),
            "discount_percent": _decimal(values["discount_percent"], field="discount_percent")
            if values.get("discount_percent") is not None
            else None,
        }
        try:
            row = await self.repo.insert_row(t.pricing_group, tenant_id, payload, actor_id)
        except IntegrityError as exc:
            raise PricingServiceError(
                "DUPLICATE_PRICING_GROUP",
                "pricing group already exists for this item/customer group pair",
                status_code=409,
            ) from exc
        await self.repo.audit(
            tenant_id=_as_uuid(tenant_id),
            actor_id=actor_id,
            action="pricing.pricing_group.created",
            resource_type="pricing.pricing_group",
            resource_id=row["id"],
        )
        return row

    # ---------------------------------------------------------------- item prices ------------
    async def create_item_price(
        self, *, tenant_id: str, values: Mapping[str, Any], actor_id: str | UUID | None
    ) -> dict[str, Any]:
        await self.get_master(t.price_level, tenant_id, str(values.get("price_level_id")))
        payload: dict[str, Any] = {
            "item_id": _as_uuid(values.get("item_id")),
            "price_level_id": _as_uuid(values.get("price_level_id")),
            "currency_code": _currency(values.get("currency_code")),
            "qty_min": _decimal(values.get("qty_min", 0), field="qty_min"),
            "qty_max": _decimal(values["qty_max"], field="qty_max") if values.get("qty_max") is not None else None,
            "unit_price": _decimal(values.get("unit_price"), field="unit_price"),
            "cost_plus_basis": values.get("cost_plus_basis"),
            "effective_from": _date(values.get("effective_from"), field="effective_from"),
            "effective_to": _date(values["effective_to"], field="effective_to") if values.get("effective_to") else None,
            "is_active": bool(values.get("is_active", True)),
        }
        if payload["qty_min"] < 0 or payload["unit_price"] < 0:
            raise PricingServiceError("VALIDATION_FAILED", "quantity/price must be non-negative")
        if payload["qty_max"] is not None and payload["qty_max"] <= payload["qty_min"]:
            raise PricingServiceError(
                "VALIDATION_FAILED", "qty_max must be greater than qty_min", details={"field": "qty_max"}
            )
        if payload["effective_to"] is not None and payload["effective_to"] <= payload["effective_from"]:
            raise PricingServiceError("VALIDATION_FAILED", "effective_to must be after effective_from")
        try:
            async with self.session.begin_nested():
                row = await self.repo.insert_row(t.item_price, tenant_id, payload, actor_id)
        except IntegrityError as exc:
            raise PricingServiceError(
                "PRICE_OVERLAP", "an active price row already exists for this band/effective range", status_code=409
            ) from exc
        await self.repo.audit(
            tenant_id=_as_uuid(tenant_id),
            actor_id=actor_id,
            action="pricing.item_price.created",
            resource_type="pricing.item_price",
            resource_id=row["id"],
        )
        await self.repo.emit(
            tenant_id=_as_uuid(tenant_id),
            event_type="pricing.price.updated",
            aggregate_type="pricing.item_price",
            aggregate_id=row["id"],
            payload={
                "item_price_id": row["id"],
                "item_id": row["item_id"],
                "price_level_id": row["price_level_id"],
                "unit_price": row["unit_price"],
            },
        )
        return row

    async def update_item_price(
        self,
        *,
        tenant_id: str,
        row_id: str,
        values: Mapping[str, Any],
        actor_id: str | UUID | None,
        expected_version: int | None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        for key in ("unit_price", "qty_min", "qty_max"):
            if key in values:
                payload[key] = _decimal(values[key], field=key) if values[key] is not None else None
        for key in ("effective_from", "effective_to"):
            if key in values:
                payload[key] = _date(values[key], field=key) if values[key] is not None else None
        if "is_active" in values:
            payload["is_active"] = bool(values["is_active"])
        if "cost_plus_basis" in values:
            payload["cost_plus_basis"] = values["cost_plus_basis"]
        try:
            async with self.session.begin_nested():
                row = await self.repo.update_row(
                    t.item_price, tenant_id, row_id, payload, actor_id, expected_version=expected_version
                )
        except IntegrityError as exc:
            raise PricingServiceError(
                "PRICE_OVERLAP", "updated band/effective range overlaps another active row", status_code=409
            ) from exc
        if row is None:
            raise PricingServiceError("NOT_FOUND", "item price not found", status_code=404)
        await self.repo.audit(
            tenant_id=_as_uuid(tenant_id),
            actor_id=actor_id,
            action="pricing.item_price.updated",
            resource_type="pricing.item_price",
            resource_id=row_id,
            diff={"after": row},
        )
        return row

    # ---------------------------------------------------------------- schedules --------------
    async def create_schedule(
        self, *, tenant_id: str, values: Mapping[str, Any], actor_id: str | UUID | None
    ) -> dict[str, Any]:
        mode = str(values.get("calculation_mode") or "").upper()
        if mode not in SCHEDULE_MODES:
            raise PricingServiceError(
                "VALIDATION_FAILED", "invalid calculation_mode", details={"allowed": sorted(SCHEDULE_MODES)}
            )
        payload = {
            "code": _code(values.get("code")),
            "name": str(values.get("name") or "").strip() or _code(values.get("code")),
            "calculation_mode": mode,
            "is_marginal": bool(values.get("is_marginal", False)),
            "currency_code": _currency(values.get("currency_code") or await self.repo.tenant_currency(tenant_id)),
            "is_inactive": bool(values.get("is_inactive", False)),
        }
        try:
            row = await self.repo.insert_row(t.price_schedule, tenant_id, payload, actor_id)
        except IntegrityError as exc:
            raise PricingServiceError("DUPLICATE_CODE", "price schedule code already exists", status_code=409) from exc
        await self.repo.audit(
            tenant_id=_as_uuid(tenant_id),
            actor_id=actor_id,
            action="pricing.price_schedule.created",
            resource_type="pricing.price_schedule",
            resource_id=row["id"],
        )
        return row

    async def replace_schedule_breaks(
        self, *, tenant_id: str, schedule_id: str, breaks: Sequence[Mapping[str, Any]], actor_id: str | UUID | None
    ) -> dict[str, Any]:
        await self.get_master(t.price_schedule, tenant_id, schedule_id)
        normalised: list[dict[str, Any]] = []
        for index, raw in enumerate(breaks):
            method = str(raw.get("pricing_method") or "").upper()
            if method not in {"DISCOUNT_PERCENT", "DISCOUNT_AMOUNT", "FIXED_PRICE"}:
                raise PricingServiceError("VALIDATION_FAILED", "invalid pricing_method", details={"index": index})
            qty_from = _decimal(raw.get("qty_from"), field="qty_from")
            qty_to = _decimal(raw["qty_to"], field="qty_to") if raw.get("qty_to") is not None else None
            if qty_to is not None and qty_to <= qty_from:
                raise PricingServiceError("VALIDATION_FAILED", "qty_to must exceed qty_from", details={"index": index})
            normalised.append(
                {
                    "price_schedule_id": _as_uuid(schedule_id),
                    "qty_from": qty_from,
                    "qty_to": qty_to,
                    "pricing_method": method,
                    "value": _decimal(raw.get("value"), field="value"),
                }
            )
        ordered = sorted(normalised, key=lambda item: item["qty_from"])
        for previous, current in zip(ordered, ordered[1:], strict=False):
            previous_end = previous["qty_to"]
            if previous_end is None or current["qty_from"] < previous_end:
                raise PricingServiceError("BREAK_OVERLAP", "schedule breaks must not overlap", status_code=409)
        await self.session.execute(
            t.price_schedule_break.delete().where(
                t.price_schedule_break.c.tenant_id == _as_uuid(tenant_id),
                t.price_schedule_break.c.price_schedule_id == _as_uuid(schedule_id),
            )
        )
        inserted = []
        for payload in normalised:
            try:
                inserted.append(await self.repo.insert_row(t.price_schedule_break, tenant_id, payload, actor_id))
            except IntegrityError as exc:
                raise PricingServiceError("BREAK_OVERLAP", "schedule breaks must not overlap", status_code=409) from exc
        await self.repo.audit(
            tenant_id=_as_uuid(tenant_id),
            actor_id=actor_id,
            action="pricing.price_schedule.breaks_replaced",
            resource_type="pricing.price_schedule",
            resource_id=schedule_id,
            diff={"breaks": len(inserted)},
        )
        return {"schedule_id": str(_as_uuid(schedule_id)), "breaks": inserted}

    # ---------------------------------------------------------------- rules ------------------
    async def create_rule(
        self, *, tenant_id: str, values: Mapping[str, Any], actor_id: str | UUID | None
    ) -> dict[str, Any]:
        rule_type = str(values.get("rule_type") or "").upper()
        if rule_type not in RULE_TYPES:
            raise PricingServiceError("VALIDATION_FAILED", "invalid rule_type", details={"allowed": sorted(RULE_TYPES)})
        policy = str(values.get("conflict_policy") or "HIGHER_PRIORITY").upper()
        if policy not in CONFLICT_POLICIES:
            raise PricingServiceError("VALIDATION_FAILED", "invalid conflict_policy")
        payload = {
            "code": _code(values.get("code")),
            "name": str(values.get("name") or "").strip() or _code(values.get("code")),
            "rule_type": rule_type,
            "priority": _int(values.get("priority", 0), field="priority"),
            "start_date": _date(values["start_date"], field="start_date") if values.get("start_date") else None,
            "end_date": _date(values["end_date"], field="end_date") if values.get("end_date") else None,
            "is_active": bool(values.get("is_active", True)),
            "conflict_policy": policy,
            "currency_code": _currency(values["currency_code"]) if values.get("currency_code") else None,
        }
        if payload["start_date"] and payload["end_date"] and payload["end_date"] < payload["start_date"]:  # type: ignore[operator]
            raise PricingServiceError("VALIDATION_FAILED", "end_date must be >= start_date")
        try:
            rule = await self.repo.insert_row(t.price_rule, tenant_id, payload, actor_id)
        except IntegrityError as exc:
            raise PricingServiceError("DUPLICATE_CODE", "price rule code already exists", status_code=409) from exc
        return await self._save_rule_parts(tenant_id=tenant_id, rule=rule, values=values, actor_id=actor_id)

    async def _save_rule_parts(
        self, *, tenant_id: str, rule: Mapping[str, Any], values: Mapping[str, Any], actor_id: str | UUID | None
    ) -> dict[str, Any]:
        rule_id = _as_uuid(str(rule["id"]))
        await self.session.execute(
            t.price_rule_criteria.delete().where(
                t.price_rule_criteria.c.tenant_id == _as_uuid(tenant_id),
                t.price_rule_criteria.c.price_rule_id == rule_id,
            )
        )
        await self.session.execute(
            t.price_rule_action.delete().where(
                t.price_rule_action.c.tenant_id == _as_uuid(tenant_id), t.price_rule_action.c.price_rule_id == rule_id
            )
        )
        criteria_payloads = list(values.get("criteria") or [])
        criteria_rows = []
        for raw in criteria_payloads:
            payload = {
                "price_rule_id": rule_id,
                "criterion": str(raw.get("criterion") or "").upper(),
                "operator": str(raw.get("operator") or "").upper(),
                "value_text": raw.get("value_text"),
                "value_number": _decimal(raw["value_number"], field="value_number")
                if raw.get("value_number") is not None
                else None,
                "value_date": _date(raw["value_date"], field="value_date") if raw.get("value_date") else None,
                "group_no": _int(raw.get("group_no", 1), field="group_no", minimum=1),
            }
            criteria_rows.append(await self.repo.insert_row(t.price_rule_criteria, tenant_id, payload, actor_id))
        action_value = values.get("value") if "value" in values else (values.get("action") or {}).get("value")
        action_type = str(
            values.get("action_type") or (values.get("action") or {}).get("action_type") or "FIXED_PRICE"
        ).upper()
        if action_type not in ACTION_TYPES:
            raise PricingServiceError("VALIDATION_FAILED", "invalid action_type")
        if action_type.startswith("PERCENT") and action_value is not None:
            percent = _decimal(action_value, field="value")
            if not 0 <= percent <= 100:
                raise PricingServiceError("VALIDATION_FAILED", "percentage must be 0..100")
        action_payload = {
            "price_rule_id": rule_id,
            "action_type": action_type,
            "price_level_id": _as_uuid((values.get("action") or {}).get("price_level_id"))
            if (values.get("action") or {}).get("price_level_id")
            else (values.get("price_level_id") if values.get("price_level_id") else None),
            "value": _decimal(action_value, field="value") if action_value is not None else None,
            "cost_basis": (values.get("action") or {}).get("cost_basis") or values.get("cost_basis"),
            "apply_to": str((values.get("action") or {}).get("apply_to") or values.get("apply_to") or "UNIT").upper(),
        }
        actions = [await self.repo.insert_row(t.price_rule_action, tenant_id, action_payload, actor_id)]
        await self.repo.audit(
            tenant_id=_as_uuid(tenant_id),
            actor_id=actor_id,
            action="pricing.price_rule.changed",
            resource_type="pricing.price_rule",
            resource_id=rule_id,
            diff={"created": True, "criteria": len(criteria_rows)},
        )
        await self.repo.emit(
            tenant_id=_as_uuid(tenant_id),
            event_type="pricing.rule.changed",
            aggregate_type="pricing.price_rule",
            aggregate_id=rule_id,
            payload={"price_rule_id": str(rule_id), "change": "CREATED", "priority": int(rule["priority"])},
        )
        return {"price_rule": rule, "criteria": criteria_rows, "actions": actions}

    async def update_rule(
        self,
        *,
        tenant_id: str,
        rule_id: str,
        values: Mapping[str, Any],
        actor_id: str | UUID | None,
        expected_version: int | None,
    ) -> dict[str, Any]:
        await self.get_master(t.price_rule, tenant_id, rule_id)
        payload: dict[str, Any] = {}
        for key in ("name", "priority", "is_active", "conflict_policy"):
            if key in values:
                payload[key] = values[key]
        for key in ("start_date", "end_date"):
            if key in values:
                payload[key] = _date(values[key], field=key) if values.get(key) else None
        row = await self.repo.update_row(
            t.price_rule, tenant_id, rule_id, payload, actor_id, expected_version=expected_version
        )
        if row is None:
            raise PricingServiceError("NOT_FOUND", "price rule not found", status_code=404)
        await self.repo.emit(
            tenant_id=_as_uuid(tenant_id),
            event_type="pricing.rule.changed",
            aggregate_type="pricing.price_rule",
            aggregate_id=_as_uuid(rule_id),
            payload={
                "price_rule_id": str(_as_uuid(rule_id)),
                "change": "DISABLED" if values.get("is_active") is False else "UPDATED",
                "priority": int(row["priority"]),
            },
        )
        return row

    # ---------------------------------------------------------------- collections -----------
    async def create_collection(
        self, *, tenant_id: str, values: Mapping[str, Any], actor_id: str | UUID | None
    ) -> dict[str, Any]:
        payload = {
            "code": _code(values.get("code")),
            "name": str(values.get("name") or "").strip() or _code(values.get("code")),
            "criteria": dict(values.get("criteria") or {}),
            "is_dynamic": bool(values.get("is_dynamic", True)),
            "is_inactive": bool(values.get("is_inactive", False)),
        }
        try:
            row = await self.repo.insert_row(t.item_collection, tenant_id, payload, actor_id)
        except IntegrityError as exc:
            raise PricingServiceError("DUPLICATE_CODE", "item collection code already exists", status_code=409) from exc
        await self.repo.audit(
            tenant_id=_as_uuid(tenant_id),
            actor_id=actor_id,
            action="pricing.item_collection.created",
            resource_type="pricing.item_collection",
            resource_id=row["id"],
        )
        return row

    async def recompute_collection(
        self, *, tenant_id: str, collection_id: str | None, actor_id: str | UUID | None
    ) -> dict[str, Any]:
        result = await self.repo.recompute_dynamic_collections(tenant_id=tenant_id, actor_id=actor_id)
        if collection_id is not None:
            matches = [entry for entry in result["errors"] if entry.get("item_collection_id") == collection_id]
            if matches:
                raise PricingServiceError(
                    "COLLECTION_RECOMPUTE_FAILED",
                    "collection recompute failed",
                    status_code=409,
                    details={"errors": matches},
                )
        return result

    async def replace_collection_members(
        self, *, tenant_id: str, collection_id: str, item_ids: Sequence[str], actor_id: str | UUID | None
    ) -> dict[str, Any]:
        await self.get_master(t.item_collection, tenant_id, collection_id)
        await self.session.execute(
            t.item_collection_member.delete().where(
                t.item_collection_member.c.tenant_id == _as_uuid(tenant_id),
                t.item_collection_member.c.item_collection_id == _as_uuid(collection_id),
            )
        )
        added = 0
        for item_id in item_ids:
            try:
                await self.repo.insert_row(
                    t.item_collection_member,
                    tenant_id,
                    {"item_collection_id": _as_uuid(collection_id), "item_id": _as_uuid(item_id)},
                    actor_id,
                )
                added += 1
            except IntegrityError:
                continue
        return {"item_collection_id": str(_as_uuid(collection_id)), "members": added}

    # ---------------------------------------------------------------- coupons ---------------
    async def create_coupon(
        self, *, tenant_id: str, values: Mapping[str, Any], actor_id: str | UUID | None
    ) -> dict[str, Any]:
        coupon_type = str(values.get("coupon_type") or "").upper()
        if coupon_type not in COUPON_TYPES:
            raise PricingServiceError("VALIDATION_FAILED", "invalid coupon_type")
        value = _decimal(values.get("value"), field="value")
        if coupon_type == "PERCENT" and not (Decimal(0) < value <= Decimal(100)):
            raise PricingServiceError("VALIDATION_FAILED", "percent coupon value must be 0..100")
        start = _date(values.get("start_date"), field="start_date")
        end = _date(values.get("end_date"), field="end_date")
        if end < start:
            raise PricingServiceError("VALIDATION_FAILED", "end_date must be >= start_date")
        payload = {
            "code": str(values.get("code") or "").strip().upper(),
            "name": str(values.get("name") or "").strip() or str(values.get("code") or "").strip().upper(),
            "coupon_type": coupon_type,
            "value": value,
            "currency_code": _currency(values.get("currency_code") or await self.repo.tenant_currency(tenant_id)),
            "start_date": start,
            "end_date": end,
            "max_uses": int(values["max_uses"]) if values.get("max_uses") is not None else None,
            "uses_count": 0,
            "item_id": _as_uuid(values["item_id"]) if values.get("item_id") else None,
            "item_category_id": _as_uuid(values["item_category_id"]) if values.get("item_category_id") else None,
            "is_active": bool(values.get("is_active", True)),
        }
        if payload["item_id"] and payload["item_category_id"]:
            raise PricingServiceError("VALIDATION_FAILED", "coupon may target an item OR an item category, not both")
        try:
            row = await self.repo.insert_row(t.coupon, tenant_id, payload, actor_id)
        except IntegrityError as exc:
            raise PricingServiceError("DUPLICATE_CODE", "coupon code already exists", status_code=409) from exc
        await self.repo.audit(
            tenant_id=_as_uuid(tenant_id),
            actor_id=actor_id,
            action="pricing.coupon.created",
            resource_type="pricing.coupon",
            resource_id=row["id"],
        )
        return row

    # ---------------------------------------------------------------- price updates ----------
    async def create_price_update(
        self, *, tenant_id: str, values: Mapping[str, Any], actor_id: str | UUID | None
    ) -> dict[str, Any]:
        update_type = str(values.get("update_type") or "").upper()
        if update_type not in PRICE_UPDATE_TYPES:
            raise PricingServiceError("VALIDATION_FAILED", "invalid update_type")
        overlap_policy = str(
            values.get("overlap_policy") or await self.repo.setting(tenant_id, "price_update.overlap_policy", "REJECT")
        ).upper()
        if overlap_policy not in OVERLAP_POLICIES:
            raise PricingServiceError("VALIDATION_FAILED", "invalid overlap_policy")
        await self.get_master(t.price_level, tenant_id, str(values.get("price_level_id")))
        effective_start = _date(values.get("effective_start"), field="effective_start")
        effective_end = _date(values["effective_end"], field="effective_end") if values.get("effective_end") else None
        if effective_end is not None and effective_end <= effective_start:
            raise PricingServiceError("VALIDATION_FAILED", "effective_end must be after effective_start")
        details = list(values.get("details") or [])
        if not details:
            raise PricingServiceError("VALIDATION_FAILED", "at least one detail row is required")
        header = {
            "update_type": update_type,
            "target_ref": str(values.get("target_ref") or "").strip() or f"{update_type}-{uuid4().hex[:12]}",
            "price_level_id": _as_uuid(values.get("price_level_id")),
            "currency_code": _currency(values.get("currency_code")),
            "effective_start": effective_start,
            "effective_end": effective_end,
            "status": "PENDING",
            "priority": _int(values.get("priority", 0), field="priority"),
            "applied_at": None,
            "scheduled_by": _as_uuid(actor_id) if actor_id else None,
            "overlap_policy": overlap_policy,
        }
        normalised_details = []
        for index, raw in enumerate(details):
            detail = {
                "item_id": _as_uuid(raw["item_id"]) if raw.get("item_id") else None,
                "item_category_id": _as_uuid(raw["item_category_id"]) if raw.get("item_category_id") else None,
                "external_customer_ref": str(raw["external_customer_ref"]).strip()
                if raw.get("external_customer_ref")
                else None,
                "customer_group_id": _as_uuid(raw["customer_group_id"]) if raw.get("customer_group_id") else None,
                "new_price": _decimal(raw.get("new_price"), field=f"details[{index}].new_price"),
                "qty_min": _decimal(raw.get("qty_min", 0), field=f"details[{index}].qty_min"),
                "qty_max": _decimal(raw["qty_max"], field=f"details[{index}].qty_max")
                if raw.get("qty_max") is not None
                else None,
            }
            targets = [
                key
                for key in ("item_id", "item_category_id", "external_customer_ref", "customer_group_id")
                if detail[key] is not None
            ]
            if len(targets) != 1:
                raise PricingServiceError(
                    "VALIDATION_FAILED", "each detail must have exactly one target", details={"index": index}
                )
            if detail["qty_max"] is not None and detail["qty_max"] <= detail["qty_min"]:  # type: ignore[operator]
                raise PricingServiceError("VALIDATION_FAILED", "qty_max must exceed qty_min", details={"index": index})
            normalised_details.append(detail)
        created = await self.repo.create_price_update(
            tenant_id=tenant_id, values=header, details=normalised_details, actor_id=actor_id
        )
        await self.repo.emit(
            tenant_id=_as_uuid(tenant_id),
            event_type="pricing.rule.changed",
            aggregate_type="pricing.price_update",
            aggregate_id=created["price_update"]["id"],
            payload={
                "price_update_id": created["price_update"]["id"],
                "change": "CREATED",
                "priority": header["priority"],
            },
        ) if update_type == "ITEM_CATEGORY" else None
        return created

    async def apply_price_update(
        self, *, tenant_id: str, update_id: str, as_of: date | None, actor_id: str | UUID | None
    ) -> dict[str, Any]:
        return await self.repo.apply_price_update(
            tenant_id=tenant_id, update_id=_as_uuid(update_id), as_of=as_of or date.today(), actor_id=actor_id
        )

    async def revert_price_update(
        self, *, tenant_id: str, update_id: str, reason: str, actor_id: str | UUID | None
    ) -> dict[str, Any]:
        if len(str(reason or "").strip()) < 3:
            raise PricingServiceError("VALIDATION_FAILED", "reason must be at least 3 characters")
        return await self.repo.revert_price_update(
            tenant_id=tenant_id, update_id=_as_uuid(update_id), reason=str(reason), actor_id=actor_id
        )

    async def cancel_price_update(
        self, *, tenant_id: str, update_id: str, reason: str, actor_id: str | UUID | None
    ) -> dict[str, Any]:
        return await self.repo.cancel_price_update(
            tenant_id=tenant_id, update_id=_as_uuid(update_id), reason=str(reason), actor_id=actor_id
        )

    # ---------------------------------------------------------------- bulk/jobs --------------
    async def bulk_upsert_prices(
        self, *, tenant_id: str, values: Mapping[str, Any], actor_id: str | UUID | None
    ) -> dict[str, Any]:
        rows = list(values.get("rows") or [])
        if not rows:
            raise PricingServiceError("VALIDATION_FAILED", "rows are required")
        effective_from = _date(values.get("effective_from"), field="effective_from")
        effective_to = _date(values["effective_to"], field="effective_to") if values.get("effective_to") else None
        return await self.repo.bulk_upsert_prices(
            tenant_id=tenant_id,
            rows=rows,
            effective_from=effective_from,
            effective_to=effective_to,
            actor_id=actor_id,
            price_level_id=values.get("price_level_id"),
            currency_code=_currency(values.get("currency_code") or await self.repo.tenant_currency(tenant_id)),
        )

    async def enqueue_job(
        self, *, tenant_id: str, name: str, payload: Mapping[str, Any], actor_id: str | UUID | None
    ) -> dict[str, Any]:
        run_key = str(payload.get("run_key") or "").strip() or f"{name}:{uuid4().hex[:16]}"
        params = {key: value for key, value in payload.items() if key not in {"run_key", "dry_run"}}
        params["dry_run"] = bool(payload.get("dry_run", False))
        return await self.repo.enqueue_job(
            tenant_id=tenant_id, name=name, run_key=run_key, params=params, actor_id=actor_id
        )

    # ---------------------------------------------------------------- resolution -------------
    async def resolve(self, *, tenant_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        lines = payload.get("lines") or []
        if not lines:
            raise PricingServiceError("VALIDATION_FAILED", "at least one line is required")
        line_inputs = []
        for index, raw in enumerate(lines):
            try:
                quantity = _decimal(raw.get("quantity"), field=f"lines[{index}].quantity")
            except PricingServiceError:
                raise
            if quantity <= 0:
                raise PricingServiceError(
                    "VALIDATION_FAILED", "quantity must be positive", details={"line_ref": raw.get("line_ref")}
                )
            line_inputs.append(
                PriceLineInput(
                    line_ref=str(raw.get("line_ref") or index + 1),
                    item_id=_as_uuid(raw.get("item_id")),
                    quantity=quantity,
                    uom=str(raw.get("uom") or "EA"),
                    parent_item_id=_as_uuid(raw["parent_item_id"]) if raw.get("parent_item_id") else None,
                )
            )
        request = PriceRequest(
            lines=tuple(line_inputs),
            currency_code=_currency(payload.get("currency_code") or await self.repo.tenant_currency(tenant_id)),
            document_date=_date(payload.get("document_date") or date.today(), field="document_date"),
            external_customer_ref=str(payload["external_customer_ref"]).strip()
            if payload.get("external_customer_ref")
            else None,
            customer_group_id=_as_uuid(payload["customer_group_id"]) if payload.get("customer_group_id") else None,
            price_level_id=_as_uuid(payload["price_level_id"]) if payload.get("price_level_id") else None,
            coupon_code=str(payload["coupon_code"]).strip().upper() if payload.get("coupon_code") else None,
            cost_context=(
                CostContext(
                    unit_cost=_decimal(payload["cost_context"]["unit_cost"], field="cost_context.unit_cost")
                    if payload["cost_context"].get("unit_cost") is not None
                    else None,
                    cost_basis=payload["cost_context"].get("cost_basis"),
                )
                if payload.get("cost_context")
                else None
            ),
            include_trace=bool(payload.get("include_trace", await self.repo.setting(tenant_id, "resolve.trace", True))),
        )
        try:
            return await self.repo.resolve(tenant_id=tenant_id, request=request)
        except EngineError as exc:
            raise PricingServiceError(exc.code, exc.message, status_code=422, details=exc.details) from exc

    async def effective_price(self, *, tenant_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        return await self.repo.effective_price(
            tenant_id=tenant_id,
            item_id=_as_uuid(payload.get("item_id")),
            price_level_id=payload.get("price_level_id"),
            currency_code=_currency(payload.get("currency_code") or await self.repo.tenant_currency(tenant_id)),
            as_of=_date(payload.get("effective_date") or date.today(), field="effective_date"),
        )

    async def validate_coupon(self, *, tenant_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        return await self.repo.validate_coupon_context(
            tenant_id=tenant_id,
            code=str(payload.get("coupon_code") or "").strip().upper(),
            item_id=_as_uuid(payload.get("item_id")),
            quantity=_decimal(payload.get("quantity", 1), field="quantity"),
            unit_price=_decimal(payload.get("unit_price"), field="unit_price"),
            currency_code=_currency(payload.get("currency_code") or await self.repo.tenant_currency(tenant_id)),
            document_date=_date(payload.get("document_date") or date.today(), field="document_date"),
        )

    async def redeem_coupon(
        self, *, tenant_id: str, payload: Mapping[str, Any], actor_id: str | UUID | None
    ) -> dict[str, Any]:
        return await self.repo.redeem_coupon(
            tenant_id=tenant_id,
            code=str(payload.get("coupon_code") or "").strip().upper(),
            document_type=str(payload.get("document_type") or "").strip().upper(),
            document_id=str(payload.get("document_id") or "").strip(),
            item_id=_as_uuid(payload.get("item_id")),
            quantity=_decimal(payload.get("quantity", 1), field="quantity"),
            unit_price=_decimal(payload.get("unit_price"), field="unit_price"),
            currency_code=_currency(payload.get("currency_code") or await self.repo.tenant_currency(tenant_id)),
            document_date=_date(payload.get("document_date") or date.today(), field="document_date"),
            actor_id=actor_id,
        )

    async def price_list(self, *, tenant_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        return await self.repo.price_list_rows(
            tenant_id=tenant_id,
            price_level_id=payload.get("price_level_id"),
            currency_code=_currency(payload.get("currency_code") or await self.repo.tenant_currency(tenant_id)),
            as_of=_date(payload.get("as_of") or date.today(), field="as_of"),
            limit=min(int(payload.get("limit", 1000)), 5000),
        )

    async def price_update_detail(self, *, tenant_id: str, update_id: str) -> dict[str, Any]:
        row = await self.repo.get_price_update(tenant_id, _as_uuid(update_id))
        if row is None:
            raise PricingServiceError("NOT_FOUND", "price update not found", status_code=404)
        return row
