"""FS-10 planning use-case service: config, demand plans, supply/MRP/DRP, AIM, allocation, ATP.

Business rules and state machines live here; the router is a thin transport. All writes stay in
``replenishment.*`` (DM-PLAN-01/INV-20); dependency applications (FS-01 planning values, FS-08/09/04
draft documents) go through frozen contract facades only.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.models import platform as platform_models
from app.modules.replenishment import engine as e
from app.modules.replenishment.engine import PlanningError
from app.modules.replenishment.repository import PlanningRepository

PLANNING_POLICY_VERSION = "replenishment.planning@1.0.0"


def _uuid(value: str | UUID | None, *, field: str = "id") -> UUID:
    try:
        return UUID(str(value))
    except (ValueError, TypeError) as exc:
        raise PlanningError(f"{field} must be a UUID", code="VALIDATION_FAILED") from exc


def _date(value: str | date | None, *, field: str = "date") -> date:
    if isinstance(value, date):
        return value
    if value is None:
        raise PlanningError(f"{field} is required", code="VALIDATION_FAILED")
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise PlanningError(f"{field} must be ISO-8601", code="VALIDATION_FAILED") from exc


def _dec(value: Any) -> Decimal:
    return e.q12(value)


class PlanningSettings:
    def __init__(self, values: Mapping[str, Any], enabled: bool | None) -> None:
        self.enabled = True if enabled is None else bool(enabled)
        self.requires_approval = self._bool(values, "replenishment.plan.requires_approval", False)
        self.horizon_days = self._int(values, "replenishment.horizon_days", 90)
        self.horizon_max_days = self._int(values, "replenishment.horizon.max_days", 365)
        self.bucket_granularity = str(values.get("replenishment.bucket_granularity") or "WEEK")
        self.time_fence_default_days = self._int(values, "replenishment.time_fence_default_days", 0)
        self.demand_change_threshold_percent = _dec(values.get("replenishment.demand_change_threshold_percent", 20))
        self.demand_method_default = str(values.get("replenishment.demand.method_default") or "MOVING_AVERAGE")
        self.moving_average_window = self._int(values, "replenishment.demand.moving_average_window", 3)
        self.season_length = self._int(values, "replenishment.demand.season_length", 12)
        self.open_demand_include_uncommitted = self._bool(values, "replenishment.demand.open_demand_include_uncommitted", False)
        self.default_lot_sizing = str(values.get("replenishment.supply.default_lot_sizing") or "LOT_FOR_LOT")
        self.aim_analysis_interval_days = self._int(values, "replenishment.aim.analysis_interval_days", 30)
        self.aim_seasonal_interval_days = self._int(values, "replenishment.aim.seasonal_interval_days", 365)
        self.aim_seasonal_mode = str(values.get("replenishment.aim.seasonal_mode") or "WEEKDAY")
        self.aim_order_interval_days = self._int(values, "replenishment.aim.order_interval_days", 7)
        self.aim_default_lead_time_days = self._int(values, "replenishment.aim.default_lead_time_days", 7)
        self.aim_change_epsilon = _dec(values.get("replenishment.aim.change_epsilon", "0.000000001"))
        self.aim_formula_policy = values.get("replenishment.aim_formula_policy") or {"id": e.AIM_POLICY_ID, "version": 1}
        self.atp_method_policy = values.get("replenishment.atp_method") or {"id": e.ATP_POLICY_ID, "version": 1}
        self.atp_horizon_days = self._int(values, "replenishment.atp.horizon_days", 90)
        self.atp_consistency_lag_seconds = self._int(values, "replenishment.atp.consistency_lag_seconds", 300)
        self.allocation_reserve_commitments = self._bool(values, "replenishment.allocation.reserve_commitments", False)
        self.allocation_fefo_required_for_lots = self._bool(values, "replenishment.allocation.fefo_required_for_lots", True)
        self.drp_transfer_supply_allowed = self._bool(values, "replenishment.drp.transfer_supply_allowed", True)
        self.workbench_staleness_seconds = self._int(values, "replenishment.workbench.staleness_seconds", 300)
        self.auto_calculate_planning = self._bool(values, "replenishment.auto_calculate_planning", True)

    @staticmethod
    def _bool(values: Mapping[str, Any], key: str, default: bool) -> bool:
        value = values.get(key)
        if value is None:
            return default
        if isinstance(value, bool):
            return value
        return str(value).lower() in {"1", "true", "yes", "on"}

    @staticmethod
    def _int(values: Mapping[str, Any], key: str, default: int) -> int:
        value = values.get(key)
        return int(value) if value is not None else default


class PlanningService:
    def __init__(self, session: AsyncSession, tenant_id: UUID | str) -> None:
        self.session = session
        self.tenant_id = _uuid(tenant_id, field="tenant_id")
        self.repo = PlanningRepository(session, tenant_id)

    # ------------------------------------------------------------------ settings -----------
    async def settings(self) -> PlanningSettings:
        table = platform_models.metadata.tables["platform.tenant_setting"]
        result = await self.session.execute(
            select(table.c.key, table.c.value).where(
                table.c.tenant_id == self.tenant_id,
                table.c.namespace == "inventory",
                table.c.key.like("replenishment.%"),
            )
        )
        values = {str(row.key): row.value for row in result.all()}
        flag_table = platform_models.metadata.tables["platform.feature_flag"]
        enabled = (
            await self.session.execute(
                select(flag_table.c.is_enabled).where(
                    flag_table.c.tenant_id == self.tenant_id,
                    flag_table.c.flag_key == "replenishment.planning.enabled",
                )
            )
        ).scalar_one_or_none()
        return PlanningSettings(values, enabled)

    async def _require_enabled(self) -> PlanningSettings:
        settings = await self.settings()
        if not settings.enabled:
            raise PlanningError("replenishment planning is disabled", code="DOMAIN_DISABLED", status_code=409)
        return settings

    # ------------------------------------------------------------------ rules/fences ------
    async def list_rules(self, **filters: Any) -> dict[str, Any]:
        items = await self.repo.list_rules(**filters)
        return {"items": items, "count": len(items)}

    async def get_rule(self, rule_id: str) -> dict[str, Any]:
        rule = await self.repo.get_rule(rule_id)
        if rule is None:
            raise PlanningError("planning rule not found", code="NOT_FOUND", status_code=404)
        return rule

    async def create_rule(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        await self._require_enabled()
        engine_name = str(payload.get("engine", "MRP")).upper()
        if engine_name not in ("MRP", "DRP_BOD"):
            raise PlanningError("engine must be MRP or DRP_BOD", code="VALIDATION_FAILED")
        allowed = [str(item).upper() for item in payload.get("allowed_supply_types") or ["PURCHASE"]]
        if not set(allowed) <= set(e.SUPPLY_TYPES):
            raise PlanningError("invalid allowed_supply_types", code="VALIDATION_FAILED")
        lot_sizing = str(payload.get("lot_sizing", "LOT_FOR_LOT")).upper()
        if lot_sizing not in e.LOT_SIZINGS:
            raise PlanningError("invalid lot_sizing", code="VALIDATION_FAILED")
        if lot_sizing == "FIXED_QTY" and not payload.get("fixed_order_qty"):
            raise PlanningError("FIXED_QTY requires fixed_order_qty", code="VALIDATION_FAILED")
        rule = await self.repo.create_rule(
            {
                "code": str(payload["code"]).strip().upper(),
                "name": str(payload["name"]).strip(),
                "engine": engine_name,
                "allowed_supply_types": allowed,
                "lot_sizing": lot_sizing,
                "fixed_order_qty": _dec(payload.get("fixed_order_qty")) if payload.get("fixed_order_qty") else None,
                "min_order_qty": _dec(payload.get("min_order_qty")) if payload.get("min_order_qty") else None,
                "max_order_qty": _dec(payload.get("max_order_qty")) if payload.get("max_order_qty") else None,
                "time_fence_days": int(payload.get("time_fence_days", 0)),
                "is_active": True,
                "params": dict(payload.get("params") or {}),
            },
            [
                {
                    "scope_type": str(scope["scope_type"]).upper(),
                    "scope_id": _uuid(scope["scope_id"]) if scope.get("scope_id") else None,
                    "include_descendants": bool(scope.get("include_descendants", False)),
                }
                for scope in payload.get("scopes") or []
            ],
        )
        return rule

    async def update_rule(self, rule_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        await self.get_rule(rule_id)
        values = {key: payload[key] for key in ("name", "allowed_supply_types", "lot_sizing", "fixed_order_qty", "time_fence_days", "is_active", "params", "min_order_qty", "max_order_qty") if payload.get(key) is not None}
        return await self.repo.update_rule(rule_id, values)

    async def list_rule_scopes(self, rule_id: str) -> dict[str, Any]:
        await self.get_rule(rule_id)
        items = await self.repo.list_rule_scopes(rule_id)
        return {"items": items, "count": len(items)}

    async def replace_rule_scopes(self, rule_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        await self.get_rule(rule_id)
        scopes = [
            {
                "scope_type": str(scope["scope_type"]).upper(),
                "scope_id": _uuid(scope["scope_id"]) if scope.get("scope_id") else None,
                "include_descendants": bool(scope.get("include_descendants", False)),
            }
            for scope in payload.get("scopes") or []
        ]
        items = await self.repo.replace_rule_scopes(rule_id, scopes)
        return {"items": items, "count": len(items)}

    async def list_time_fences(self, **filters: Any) -> dict[str, Any]:
        items = await self.repo.list_time_fences(**filters)
        return {"items": items, "count": len(items)}

    async def upsert_time_fence(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        await self._require_enabled()
        behavior = str(payload.get("behavior", "SLUSHED")).upper()
        if behavior not in e.FENCE_BEHAVIORS:
            raise PlanningError("behavior must be FROZEN or SLUSHED", code="VALIDATION_FAILED")
        scope_type = str(payload["scope_type"]).upper()
        if scope_type not in e.FENCE_SCOPES:
            raise PlanningError("invalid scope_type", code="VALIDATION_FAILED")
        fence = await self.repo.upsert_time_fence(
            {
                "scope_type": scope_type,
                "scope_id": _uuid(payload["scope_id"]) if payload.get("scope_id") else None,
                "fence_days": int(payload.get("fence_days", 0)),
                "behavior": behavior,
                "demand_change_threshold": _dec(payload["demand_change_threshold"]) if payload.get("demand_change_threshold") is not None else None,
                "is_active": True,
            }
        )
        return fence

    async def update_time_fence(self, fence_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        values = {key: payload[key] for key in ("scope_type", "fence_days", "behavior", "demand_change_threshold", "is_active") if payload.get(key) is not None}
        if values.get("behavior"):
            values["behavior"] = str(values["behavior"]).upper()
        if values.get("scope_type"):
            values["scope_type"] = str(values["scope_type"]).upper()
        return await self.repo._update(__import__("app.modules.replenishment.tables", fromlist=["time_fence"]).time_fence, fence_id, values)

    async def delete_time_fence(self, fence_id: str) -> None:
        await self.repo.deactivate_time_fence(fence_id)

    # ------------------------------------------------------------------ demand history -----
    async def list_demand_history(self, **filters: Any) -> dict[str, Any]:
        items = await self.repo.list_demand_history(**filters)
        return {"items": items, "count": len(items), "next_cursor": None}

    async def ingest_demand_history(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        await self._require_enabled()
        accepted: list[dict[str, Any]] = []
        rejected: list[dict[str, Any]] = []
        for row in payload.get("rows") or []:
            try:
                if _dec(row["qty"]) < 0:
                    raise PlanningError("demand qty must be non-negative", code="VALIDATION_FAILED")
                accepted.append(
                    {
                        "item_id": _uuid(row["item_id"]),
                        "location_id": _uuid(row["location_id"]),
                        "bucket_date": _date(row["bucket_date"]),
                        "bucket_granularity": str(row.get("bucket_granularity", "DAY")).upper(),
                        "qty": _dec(row["qty"]),
                        "source": str(row["source"]).upper(),
                        "external_ref": row.get("external_ref"),
                    }
                )
            except PlanningError as exc:
                rejected.append({"row": dict(row), "error": exc.code, "message": exc.message})
        created = await self.repo.upsert_demand_history(accepted)
        return {"accepted": created, "rejected": rejected, "accepted_count": len(created), "rejected_count": len(rejected)}

    # ------------------------------------------------------------------ demand plans -------
    async def list_demand_plans(self, **filters: Any) -> dict[str, Any]:
        items = await self.repo.list_demand_plans(**filters)
        return {"items": items, "count": len(items)}

    async def get_demand_plan(self, plan_id: str, *, include_lines: bool = False) -> dict[str, Any]:
        plan = await self.repo.get_demand_plan(plan_id)
        if plan is None:
            raise PlanningError("demand plan not found", code="NOT_FOUND", status_code=404)
        if include_lines:
            plan["lines"] = await self.repo.list_demand_plan_lines(plan_id)
        return plan

    async def create_demand_plan(self, payload: Mapping[str, Any], settings: PlanningSettings | None = None) -> dict[str, Any]:
        cfg = settings or await self._require_enabled()
        method = str(payload.get("method", cfg.demand_method_default)).upper()
        if method not in e.DEMAND_METHODS:
            raise PlanningError("invalid demand method", code="VALIDATION_FAILED")
        horizon_start = _date(payload["horizon_start"])
        horizon_end = _date(payload["horizon_end"])
        if horizon_end < horizon_start:
            raise PlanningError("horizon_end must be >= horizon_start", code="VALIDATION_FAILED")
        if (horizon_end - horizon_start).days > cfg.horizon_max_days:
            raise PlanningError("horizon exceeds the maximum", code="VALIDATION_FAILED")
        plan = await self.repo.create_demand_plan(
            {
                "plan_number": await self.repo.next_number("DP"),
                "method": method,
                "horizon_start": horizon_start,
                "horizon_end": horizon_end,
                "bucket_granularity": str(payload.get("bucket_granularity") or cfg.bucket_granularity).upper(),
                "status": "DRAFT",
                "planning_rule_id": _uuid(payload["planning_rule_id"]) if payload.get("planning_rule_id") else None,
                "demand_change_threshold": _dec(payload["demand_change_threshold"]) if payload.get("demand_change_threshold") is not None else cfg.demand_change_threshold_percent,
                "scope": dict(payload.get("scope") or {}),
                "method_metadata": {"policy": e.DEMAND_POLICY_ID, "version": e.DEMAND_POLICY_VERSION},
            }
        )
        return plan

    async def update_demand_plan(self, plan_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        plan = await self.get_demand_plan(plan_id)
        if plan["status"] in {"APPROVED", "ARCHIVED"}:
            raise PlanningError("approved/archived plans are immutable", code="INVALID_STATE_TRANSITION", status_code=409)
        values = {key: payload[key] for key in ("method", "horizon_start", "horizon_end", "bucket_granularity", "scope") if payload.get(key) is not None}
        return await self.repo.update_demand_plan(plan_id, values)

    async def calculate_demand_plan(self, plan_id: str, payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
        cfg = await self._require_enabled()
        plan = await self.get_demand_plan(plan_id)
        if plan["status"] in {"APPROVED", "ARCHIVED"}:
            raise PlanningError("approved/archived plans cannot be recalculated", code="INVALID_STATE_TRANSITION", status_code=409)
        bucket_dates = e.buckets_between(_date(plan["horizon_start"]), _date(plan["horizon_end"]), plan["bucket_granularity"])
        scope = dict(plan.get("scope") or {})
        item_ids = [str(item) for item in scope.get("items") or []]
        location_ids = [str(location) for location in scope.get("locations") or []]
        states = await self.repo.item_planning_states(item_ids=item_ids, location_ids=location_ids)
        rows: list[dict[str, Any]] = []
        history_all = await self.repo.list_demand_history(item_id=None, location_id=None, limit=2000)
        for state in states:
            history = [Decimal(str(row["qty"])) for row in history_all if row["item_id"] == state["item_id"] and row["location_id"] == state["location_id"]]
            forecast = e.demand_forecast(
                method=plan["method"], history=history, buckets=bucket_dates,
                moving_average_window=cfg.moving_average_window, season_length=cfg.season_length,
            )
            for row in forecast:
                rows.append(
                    {
                        "item_id": state["item_id"], "location_id": state["location_id"],
                        "bucket_date": row["bucket_date"], "qty_forecast": row["qty_forecast"],
                        "method_metadata": {"policy": e.DEMAND_POLICY_ID, "version": e.DEMAND_POLICY_VERSION, "method": plan["method"]},
                    }
                )
        await self.repo.replace_demand_plan_lines(plan_id, rows)
        updated = await self.repo.update_demand_plan(
            plan_id,
            {"status": "CALCULATED", "calculated_at": datetime.now(UTC),
             "method_metadata": {"policy": e.DEMAND_POLICY_ID, "version": e.DEMAND_POLICY_VERSION, "line_count": len(rows)}},
        )
        await self.repo.emit(
            "plan.demand.calculated", aggregate_id=plan_id,
            payload={"demand_plan_id": plan_id, "method": plan["method"], "line_count": len(rows), "policy_version": f"{e.DEMAND_POLICY_ID}/{e.DEMAND_POLICY_VERSION}", "changed_lines": 0},
        )
        return updated

    async def approve_demand_plan(self, plan_id: str, payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
        plan = await self.get_demand_plan(plan_id)
        e.assert_transition("demand_plan", plan["status"], "APPROVED")
        updated = await self.repo.update_demand_plan(
            plan_id, {"status": "APPROVED", "approved_at": datetime.now(UTC), "approved_by": _uuid(str((payload or {}).get("approved_by"))) if (payload or {}).get("approved_by") else None}
        )
        await self.repo.emit("plan.approved", aggregate_id=plan_id, payload={"plan_type": "DEMAND", "plan_id": plan_id, "approver": (payload or {}).get("approved_by"), "approved_at": updated["approved_at"]})
        return updated

    async def archive_demand_plan(self, plan_id: str, payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
        plan = await self.get_demand_plan(plan_id)
        e.assert_transition("demand_plan", plan["status"], "ARCHIVED")
        return await self.repo.update_demand_plan(plan_id, {"status": "ARCHIVED"})

    async def list_demand_plan_lines(self, plan_id: str, **filters: Any) -> dict[str, Any]:
        await self.get_demand_plan(plan_id)
        items = await self.repo.list_demand_plan_lines(plan_id, **filters)
        return {"items": items, "count": len(items)}

    async def override_demand_line(self, plan_id: str, line_id: str, payload: Mapping[str, Any], *, as_of: date | None = None) -> dict[str, Any]:
        cfg = await self._require_enabled()
        plan = await self.get_demand_plan(plan_id)
        line = await self.repo.get_demand_plan_line(line_id)
        if line is None or str(line["demand_plan_id"]) != plan_id:
            raise PlanningError("demand plan line not found", code="NOT_FOUND", status_code=404)
        current = _dec(payload.get("qty_manual_override", line.get("qty_final", 0)))
        fences = await self.repo.list_time_fences(is_active=True)
        fence = e.resolve_time_fence(
            [e.TimeFence(f["scope_type"], f.get("scope_id"), int(f["fence_days"]), f["behavior"], _dec(f.get("demand_change_threshold")) if f.get("demand_change_threshold") is not None else None) for f in fences],
            item_id=str(line["item_id"]), location_id=str(line["location_id"]),
        )
        decision = e.evaluate_demand_change(
            previous=_dec(line.get("qty_forecast", 0)), current=current, change_date=_date(line["bucket_date"]),
            as_of=as_of or date.today(), threshold_percent=_dec(plan.get("demand_change_threshold") or cfg.demand_change_threshold_percent),
            fence=fence,
        )
        updated = await self.repo.update_demand_plan_line(line_id, {"qty_manual_override": current, "is_overridden": True})
        if decision.get("message"):
            await self.repo.emit("planning.exception.raised", aggregate_id=plan_id, payload={"message_type": decision["message"], "severity": "WARN", "item_id": line["item_id"], "location_id": line["location_id"], "demand_plan_id": plan_id, "context": {"change_percent": decision.get("change_percent")}})
        return {"line": updated, "fence": decision}

    # ------------------------------------------------------------------ supply plans -------
    async def list_supply_plans(self, **filters: Any) -> dict[str, Any]:
        items = await self.repo.list_supply_plans(**filters)
        return {"items": items, "count": len(items)}

    async def get_supply_plan(self, plan_id: str, *, include_lines: bool = False, include_suggestions: bool = False) -> dict[str, Any]:
        plan = await self.repo.get_supply_plan(plan_id)
        if plan is None:
            raise PlanningError("supply plan not found", code="NOT_FOUND", status_code=404)
        if include_lines:
            plan["lines"] = await self.repo.list_supply_plan_lines(plan_id)
        if include_suggestions:
            plan["suggestions"] = await self.repo.list_suggestions(plan_id)
        return plan

    async def create_supply_plan(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        cfg = await self._require_enabled()
        horizon_start = _date(payload["horizon_start"])
        horizon_end = _date(payload["horizon_end"])
        if horizon_end < horizon_start:
            raise PlanningError("horizon_end must be >= horizon_start", code="VALIDATION_FAILED")
        rule = await self.get_rule(str(payload["planning_rule_id"])) if payload.get("planning_rule_id") else None
        run_key_payload = {"scope": dict(payload.get("scope") or {}), "horizon": [horizon_start.isoformat(), horizon_end.isoformat()]}
        run_key = str(payload.get("run_key") or e.planning_run_key(kind="supply", scope=run_key_payload["scope"], horizon=run_key_payload["horizon"], policy_versions=e.policy_versions(), inputs=run_key_payload))
        existing = await self.repo.get_supply_plan_by_run_key(run_key)
        if existing is not None:
            return existing
        plan = await self.repo.create_supply_plan(
            {
                "plan_number": await self.repo.next_number("SP"),
                "planning_rule_id": rule["id"] if rule else None,
                "horizon_start": horizon_start,
                "horizon_end": horizon_end,
                "scope": dict(payload.get("scope") or {}),
                "status": "QUEUED",
                "run_key": run_key,
                "bucket_granularity": str(payload.get("bucket_granularity") or cfg.bucket_granularity).upper(),
                "as_of_date": _date(payload.get("as_of") or date.today()),
                "input_hash": e.input_hash(run_key_payload),
            }
        )
        return plan

    async def update_supply_plan(self, plan_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        await self.get_supply_plan(plan_id)
        values = {key: payload[key] for key in ("status", "run_key", "scope", "bucket_granularity", "as_of_date", "error_text") if payload.get(key) is not None}
        return await self.repo.update_supply_plan(plan_id, values)

    async def run_supply_plan(self, plan_id: str, payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
        cfg = await self._require_enabled()
        plan = await self.get_supply_plan(plan_id)
        e.assert_transition("supply_plan", plan["status"], "RUNNING")
        await self.repo.update_supply_plan(plan_id, {"status": "RUNNING", "started_at": datetime.now(UTC)})
        try:
            rule_payload = await self.get_rule(str(plan["planning_rule_id"])) if plan.get("planning_rule_id") else None
            rule = e.SupplyRule(
                code=rule_payload["code"] if rule_payload else "DEFAULT",
                engine=rule_payload["engine"] if rule_payload else "MRP",
                allowed_supply_types=tuple(rule_payload["allowed_supply_types"]) if rule_payload else ("PURCHASE",),
                lot_sizing=rule_payload["lot_sizing"] if rule_payload else cfg.default_lot_sizing,
                fixed_order_qty=_dec(rule_payload.get("fixed_order_qty")) if rule_payload and rule_payload.get("fixed_order_qty") is not None else None,
                time_fence_days=int(rule_payload.get("time_fence_days", 0)) if rule_payload else cfg.time_fence_default_days,
            )
            scope = dict(plan.get("scope") or {})
            item_ids = [str(item) for item in scope.get("items") or []]
            location_ids = [str(location) for location in scope.get("locations") or []]
            states = await self.repo.item_planning_states(item_ids=item_ids, location_ids=location_ids)
            demand_rows = await self.repo.list_demand_plan_lines(str(scope["demand_plan_id"])) if scope.get("demand_plan_id") else []
            demands = [
                e.PlannedDemand(item_id=str(row["item_id"]), location_id=str(row["location_id"]), due_date=_date(row["bucket_date"]), qty=_dec(row["qty_final"]))
                for row in demand_rows
            ]
            inputs = [
                e.PlanningItemState(
                    item_id=state["item_id"], location_id=state["location_id"],
                    on_hand=_dec(state.get("qty_on_hand", 0)), safety_stock=_dec(state.get("safety_stock", 0)),
                    lead_time_days=int(state.get("lead_time_days") or cfg.aim_default_lead_time_days),
                    uom_id=state.get("uom_id"),
                )
                for state in states
            ]
            supplies: list[e.PlannedSupply] = []
            for item in inputs:
                on_order = await self.repo.open_purchase_supply(item_id=item.item_id, location_id=item.location_id)
                if on_order > 0:
                    supplies.append(e.PlannedSupply(item_id=item.item_id, location_id=item.location_id, due_date=plan["horizon_start"], qty=on_order))
            result = e.run_mrp(
                items=inputs, demands=demands, supplies=supplies, rule=rule,
                horizon_start=_date(plan["horizon_start"]), horizon_end=_date(plan["horizon_end"]), as_of=_date(plan.get("as_of_date")),
                buckets=e.buckets_between(_date(plan["horizon_start"]), _date(plan["horizon_end"]), plan["bucket_granularity"]),
            )
            suggestions = [
                {
                    "item_id": suggestion["item_id"], "location_id": suggestion["location_id"],
                    "action_type": suggestion["action_type"], "qty": suggestion["qty"],
                    "due_date": suggestion["due_date"], "priority": suggestion["priority"],
                    "status": "OPEN", "explanation": suggestion["explanation"],
                    "uom_id": next((item.uom_id for item in inputs if item.item_id == suggestion["item_id"] and item.location_id == suggestion["location_id"]), None),
                }
                for suggestion in result["suggestions"]
            ]
            await self.repo.replace_supply_results(plan_id, result["lines"], suggestions, result["messages"])
            finished = datetime.now(UTC)
            updated = await self.repo.update_supply_plan(
                plan_id, {"status": "CALCULATED", "finished_at": finished, "input_hash": e.input_hash(scope)}
            )
            for suggestion in await self.repo.list_suggestions(plan_id):
                await self.repo.emit("plan.suggestion.created", aggregate_id=plan_id, payload={"supply_plan_id": plan_id, "suggestion": suggestion})
            for message in result["messages"]:
                await self.repo.emit("planning.exception.raised", aggregate_id=plan_id, payload=message)
            await self.repo.emit("plan.supply.calculated", aggregate_id=plan_id, payload={"supply_plan_id": plan_id, "rule_id": rule.code, "suggestion_count": len(suggestions), "exception_count": len(result["messages"]), "run_key": plan["run_key"], "status": "CALCULATED"})
            return updated
        except PlanningError:
            await self.repo.update_supply_plan(plan_id, {"status": "FAILED", "finished_at": datetime.now(UTC)})
            raise
        except Exception as exc:  # noqa: BLE001
            await self.repo.update_supply_plan(plan_id, {"status": "FAILED", "finished_at": datetime.now(UTC), "error_text": str(exc)})
            raise PlanningError(f"supply plan run failed: {exc}", code="VALIDATION_FAILED") from exc

    async def cancel_supply_plan(self, plan_id: str, payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
        plan = await self.get_supply_plan(plan_id)
        if plan["status"] in {"CALCULATED", "APPROVED", "FAILED", "CANCELLED"}:
            raise PlanningError("only pre-calculation plans can be cancelled", code="INVALID_STATE_TRANSITION", status_code=409)
        return await self.repo.update_supply_plan(plan_id, {"status": "CANCELLED"})

    async def approve_supply_plan(self, plan_id: str, payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
        plan = await self.get_supply_plan(plan_id)
        e.assert_transition("supply_plan", plan["status"], "APPROVED")
        updated = await self.repo.update_supply_plan(plan_id, {"status": "APPROVED"})
        await self.repo.emit("plan.approved", aggregate_id=plan_id, payload={"plan_type": "SUPPLY", "plan_id": plan_id, "approver": (payload or {}).get("approved_by"), "approved_at": datetime.now(UTC).isoformat()})
        return updated

    async def list_supply_plan_lines(self, plan_id: str, **filters: Any) -> dict[str, Any]:
        await self.get_supply_plan(plan_id)
        items = await self.repo.list_supply_plan_lines(plan_id, **filters)
        return {"items": items, "count": len(items)}

    async def list_suggestions(self, plan_id: str, **filters: Any) -> dict[str, Any]:
        await self.get_supply_plan(plan_id)
        items = await self.repo.list_suggestions(plan_id, **filters)
        return {"items": items, "count": len(items)}

    async def dismiss_suggestion(self, plan_id: str, suggestion_id: str, payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
        suggestion = await self.repo.get_suggestion(suggestion_id)
        if suggestion is None or str(suggestion["supply_plan_id"]) != plan_id:
            raise PlanningError("suggestion not found", code="NOT_FOUND", status_code=404)
        e.assert_transition("suggestion", suggestion["status"], "DISMISSED")
        return await self.repo.update_suggestion(suggestion_id, {"status": "DISMISSED"})

    # ------------------------------------------------------------------ conversion ---------
    async def convert_suggestions(self, plan_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        plan = await self.get_supply_plan(plan_id)
        if plan["status"] not in {"CALCULATED", "APPROVED"}:
            raise PlanningError("only CALCULATED/APPROVED plans convert suggestions", code="INVALID_STATE_TRANSITION", status_code=409)
        suggestions = await self.repo.list_suggestions(plan_id)
        wanted = {str(item) for item in payload.get("suggestion_ids") or []}
        if wanted:
            suggestions = [suggestion for suggestion in suggestions if suggestion["id"] in wanted or suggestion["id"].replace("-", "") in wanted]
        dry_run = bool(payload.get("dry_run", False))
        results: list[dict[str, Any]] = []
        for suggestion in suggestions:
            if suggestion["status"] == "CONVERTED":
                results.append({"suggestion_id": suggestion["id"], "status": "CONVERTED", "created_doc_type": suggestion.get("created_doc_type"), "created_doc_id": suggestion.get("created_doc_id"), "replayed": True})
                continue
            if suggestion["status"] != "OPEN":
                results.append({"suggestion_id": suggestion["id"], "status": suggestion["status"], "error": "not_open"})
                continue
            if dry_run:
                results.append({"suggestion_id": suggestion["id"], "status": "DRY_RUN", "action_type": suggestion["action_type"]})
                continue
            action = suggestion["action_type"]
            created_type: str | None = None
            created_id: str | None = None
            if action == "CREATE_PO":
                vendor = payload.get("vendor_id")
                if not vendor:
                    results.append({"suggestion_id": suggestion["id"], "status": "OPEN", "error": "vendor_id_required"})
                    continue
                document = await self.repo.create_draft_po(
                    vendor_id=vendor,
                    lines=[{"item_id": suggestion["item_id"], "qty": _dec(suggestion["qty"]), "location_id": suggestion["location_id"], "expected_receipt_date": suggestion["due_date"]}],
                    options={"ship_to_location_id": suggestion["location_id"], "expected_receipt_date": suggestion["due_date"]},
                )
                created_type, created_id = "PURCHASE_ORDER", str(document.get("id"))
            elif action == "CREATE_WO":
                document = await self.repo.create_draft_wo(
                    lines=[{"item_id": suggestion["item_id"], "qty": _dec(suggestion["qty"]), "location_id": suggestion["location_id"]}],
                    options={"planned_end_date": suggestion["due_date"]},
                )
                created_type, created_id = "WORK_ORDER", str(document.get("id"))
            elif action == "CREATE_TO":
                created_type = "TRANSFER_ORDER_DRAFT"
                created_id = None
            await self.repo.update_suggestion(suggestion["id"], {"status": "CONVERTED", "created_doc_type": created_type, "created_doc_id": created_id})
            await self.repo.emit("plan.suggestion.converted", aggregate_id=suggestion["id"], payload={"suggestion_id": suggestion["id"], "created_doc_type": created_type, "created_doc_id": created_id, "idempotency_ref": suggestion["id"]})
            results.append({"suggestion_id": suggestion["id"], "status": "CONVERTED", "created_doc_type": created_type, "created_doc_id": created_id})
        return {"items": results, "count": len(results), "dry_run": dry_run}

    # ------------------------------------------------------------------ messages -----------
    async def list_action_messages(self, **filters: Any) -> dict[str, Any]:
        items = await self.repo.list_action_messages(**filters)
        return {"items": items, "count": len(items)}

    async def transition_message(self, message_id: str, target: str) -> dict[str, Any]:
        message = await self.repo.get_action_message(message_id)
        if message is None:
            raise PlanningError("planning action message not found", code="NOT_FOUND", status_code=404)
        allowed = {"OPEN": {"ACKNOWLEDGED", "IGNORED"}, "ACKNOWLEDGED": {"RESOLVED", "IGNORED"}}
        if target not in allowed.get(message["status"], set()):
            raise PlanningError("invalid action message transition", code="INVALID_STATE_TRANSITION", status_code=409)
        updated = await self.repo.update_action_message(message_id, {"status": target})
        await self.repo.emit("planning.exception.resolved", aggregate_id=message_id, payload={"message_id": message_id, "resolution": target})
        return updated

    # ------------------------------------------------------------------ DRP ----------------
    async def list_networks(self, **filters: Any) -> dict[str, Any]:
        items = await self.repo.list_networks(**filters)
        return {"items": items, "count": len(items)}

    async def get_network(self, network_id: str) -> dict[str, Any]:
        network = await self.repo.get_network(network_id)
        if network is None:
            raise PlanningError("DRP network not found", code="NOT_FOUND", status_code=404)
        return network

    async def create_network(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        await self._require_enabled()
        members = [
            {
                "location_id": _uuid(str(member["location_id"])), "level": int(member.get("level", 0)),
                "parent_member_id": _uuid(member["parent_member_id"]) if member.get("parent_member_id") else None,
                "lead_time_days": int(member.get("transfer_lead_time_days", member.get("lead_time_days", 0))),
                "is_source": bool(member.get("is_source", False)), "priority": int(member.get("priority", 100)),
            }
            for member in payload.get("members") or []
        ]
        if members:
            e.validate_network_members([{**member, "id": member.get("id") or str(_uuid(str(member["location_id"])))} for member in members])
        return await self.repo.create_network(
            {
                "code": str(payload["code"]).strip().upper(),
                "name": str(payload["name"]).strip(),
                "status": str(payload.get("status", "ACTIVE")).upper(),
                "effective_from": _date(payload.get("effective_from") or date.today()),
                "effective_to": _date(payload["effective_to"]) if payload.get("effective_to") else None,
                "notes": payload.get("notes"),
            },
            members,
        )

    async def update_network(self, network_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        await self.get_network(network_id)
        values = {key: payload[key] for key in ("name", "status", "effective_from", "effective_to", "notes") if payload.get(key) is not None}
        return await self.repo.update_network(network_id, values)

    async def replace_network_members(self, network_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        await self.get_network(network_id)
        members = payload.get("members") or []
        e.validate_network_members([{**member, "id": member.get("id") or str(index)} for index, member in enumerate(members)])
        normalized = [
            {
                "location_id": _uuid(str(member["location_id"])), "level": int(member.get("level", 0)),
                "parent_member_id": _uuid(member["parent_member_id"]) if member.get("parent_member_id") else None,
                "lead_time_days": int(member.get("transfer_lead_time_days", member.get("lead_time_days", 0))),
                "is_source": bool(member.get("is_source", False)), "priority": int(member.get("priority", 100)),
            }
            for member in members
        ]
        items = await self.repo.replace_network_members(network_id, normalized)
        return {"items": items, "count": len(items)}

    async def list_network_members(self, network_id: str) -> dict[str, Any]:
        await self.get_network(network_id)
        items = await self.repo.list_network_members(network_id)
        return {"items": items, "count": len(items)}

    async def list_bods(self, **filters: Any) -> dict[str, Any]:
        items = await self.repo.list_bods(**filters)
        return {"items": items, "count": len(items)}

    async def get_bod(self, bod_id: str) -> dict[str, Any]:
        bod = await self.repo.get_bod(bod_id)
        if bod is None:
            raise PlanningError("bill of distribution not found", code="NOT_FOUND", status_code=404)
        return bod

    async def create_bod(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        await self.get_network(str(payload["drp_network_id"]))
        levels = payload.get("levels") or []
        if levels:
            e.validate_bod_arcs(levels)
        return await self.repo.create_bod(
            {
                "drp_network_id": _uuid(payload["drp_network_id"]),
                "code": str(payload["code"]).strip().upper(),
                "name": str(payload["name"]).strip(),
                "version": int(payload.get("version", 1)),
                "status": str(payload.get("status", "DRAFT")).upper(),
                "effective_from": _date(payload.get("effective_from") or date.today()),
                "effective_to": _date(payload["effective_to"]) if payload.get("effective_to") else None,
            },
            levels,
        )

    async def replace_bod_levels(self, bod_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        await self.get_bod(bod_id)
        levels = payload.get("levels") or []
        e.validate_bod_arcs(levels)
        normalized = [
            {
                "level": int(level.get("level", 0)),
                "source_location_id": _uuid(level["source_location_id"]),
                "destination_location_id": _uuid(level["destination_location_id"]),
                "transfer_lead_time_days": int(level.get("transfer_lead_time_days", 0)),
                "is_default_source": bool(level.get("is_default_source", False)),
            }
            for level in levels
        ]
        items = await self.repo.replace_bod_levels(bod_id, normalized)
        return {"items": items, "count": len(items)}

    async def list_bod_levels(self, bod_id: str) -> dict[str, Any]:
        await self.get_bod(bod_id)
        items = await self.repo.list_bod_levels(bod_id)
        return {"items": items, "count": len(items)}

    async def list_distribution_categories(self, **filters: Any) -> dict[str, Any]:
        items = await self.repo.list_distribution_categories(**filters)
        return {"items": items, "count": len(items)}

    async def create_distribution_category(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return await self.repo.create_distribution_category(
            {"code": str(payload["code"]).strip().upper(), "name": str(payload["name"]).strip(), "is_active": True},
            [str(item) for item in payload.get("items") or []],
        )

    async def replace_distribution_members(self, category_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        items = await self.repo.replace_distribution_members(category_id, [str(item) for item in payload.get("items") or []])
        return {"item_ids": items, "count": len(items)}

    # ------------------------------------------------------------------ allocation ---------
    async def list_strategies(self, **filters: Any) -> dict[str, Any]:
        items = await self.repo.list_strategies(**filters)
        return {"items": items, "count": len(items)}

    async def create_strategy(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        await self._require_enabled()
        method = str(payload.get("method", "PRIORITY")).upper()
        if method not in e.ALLOCATION_METHODS:
            raise PlanningError("invalid allocation method", code="ALLOCATION_STRATEGY_INVALID")
        rules = payload.get("rules") or []
        if method == "PRIORITY" and not rules:
            raise PlanningError("PRIORITY strategies require at least one rule", code="ALLOCATION_STRATEGY_INVALID")
        return await self.repo.create_strategy(
            {
                "code": str(payload["code"]).strip().upper(),
                "name": str(payload["name"]).strip(),
                "method": method,
                "priority": int(payload.get("priority", 100)),
                "location_scope": dict(payload.get("location_scope") or {}),
                "include_assembly": bool(payload.get("include_assembly", False)),
                "include_kits": bool(payload.get("include_kits", False)),
                "fefo_enabled": bool(payload.get("fefo_enabled", False)),
                "is_active": True,
            },
            [
                {
                    "sequence": int(rule["sequence"]), "criterion": str(rule["criterion"]).upper(),
                    "direction": str(rule.get("direction", "ASC")).upper(),
                    "weight": _dec(rule["weight"]) if rule.get("weight") is not None else None,
                    "is_active": bool(rule.get("is_active", True)),
                }
                for rule in rules
            ],
        )

    async def update_strategy(self, strategy_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        strategy = await self.repo.get_strategy(strategy_id)
        if strategy is None:
            raise PlanningError("allocation strategy not found", code="NOT_FOUND", status_code=404)
        values = {key: payload[key] for key in ("name", "priority", "location_scope", "include_assembly", "include_kits", "fefo_enabled", "is_active") if payload.get(key) is not None}
        return await self.repo.update_strategy(strategy_id, values)

    async def list_strategy_rules(self, strategy_id: str) -> dict[str, Any]:
        items = await self.repo.list_strategy_rules(strategy_id)
        return {"items": items, "count": len(items)}

    async def replace_strategy_rules(self, strategy_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        rules = [
            {
                "sequence": int(rule["sequence"]), "criterion": str(rule["criterion"]).upper(),
                "direction": str(rule.get("direction", "ASC")).upper(),
                "weight": _dec(rule["weight"]) if rule.get("weight") is not None else None,
                "is_active": bool(rule.get("is_active", True)),
            }
            for rule in payload.get("rules") or []
        ]
        items = await self.repo.replace_strategy_rules(strategy_id, rules)
        return {"items": items, "count": len(items)}

    async def list_allocation_runs(self, **filters: Any) -> dict[str, Any]:
        items = await self.repo.list_allocation_runs(**filters)
        return {"items": items, "count": len(items)}

    async def get_allocation_run(self, run_id: str) -> dict[str, Any]:
        run = await self.repo.get_allocation_run(run_id)
        if run is None:
            raise PlanningError("allocation run not found", code="NOT_FOUND", status_code=404)
        return run

    async def execute_allocation_run(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        cfg = await self._require_enabled()
        strategy_id = str(payload["allocation_strategy_id"])
        strategy = await self.repo.get_strategy(strategy_id)
        if strategy is None:
            raise PlanningError("allocation strategy not found", code="NOT_FOUND", status_code=404)
        run = await self.repo.create_allocation_run(
            {
                "run_number": await self.repo.next_number("ALC"),
                "allocation_strategy_id": _uuid(strategy_id),
                "as_of_date": _date(payload.get("as_of_date") or date.today()),
                "status": "RUNNING", "started_at": datetime.now(UTC),
            }
        )
        try:
            states = await self.repo.item_planning_states()
            items = [
                e.AllocationDemand(
                    demand_ref=f"DEM-{index}", demand_line_ref="1",
                    item_id=state["item_id"], location_id=state["location_id"],
                    qty=_dec(state.get("qty_on_hand", 0)),
                    required_by_date=_date(payload.get("as_of_date") or date.today()),
                    priority=100,
                )
                for index, state in enumerate(states)
            ]
            supply_map = {(state["item_id"], state["location_id"]): _dec(state.get("qty_on_hand", 0)) for state in states}
            results, exceptions = e.allocate_supply(method=strategy["method"], demands=items, supply_by_key=supply_map)
            await self.repo.replace_allocation_results(run["id"], results, exceptions)
            updated = await self.repo.update_allocation_run(
                run["id"], {"status": "COMPLETED", "finished_at": datetime.now(UTC), "demand_count": len(items), "supply_count": len(states)}
            )
            await self.repo.emit("allocation.completed", aggregate_id=run["id"], payload={"allocation_run_id": run["id"], "strategy_id": strategy_id, "as_of_date": str(payload.get("as_of_date") or date.today()), "demand_count": len(items), "supply_count": len(states), "allocated_count": len(results), "exception_count": len(exceptions)})
            del cfg
            return updated
        except PlanningError:
            await self.repo.update_allocation_run(run["id"], {"status": "FAILED", "finished_at": datetime.now(UTC)})
            raise

    async def list_allocation_results(self, run_id: str, **filters: Any) -> dict[str, Any]:
        await self.get_allocation_run(run_id)
        items = await self.repo.list_allocation_results(run_id, **filters)
        return {"items": items, "count": len(items)}

    async def list_allocation_exceptions(self, run_id: str, **filters: Any) -> dict[str, Any]:
        await self.get_allocation_run(run_id)
        items = await self.repo.list_allocation_exceptions(run_id, **filters)
        return {"items": items, "count": len(items)}

    async def cancel_allocation_run(self, run_id: str, payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
        run = await self.get_allocation_run(run_id)
        if run["status"] in {"COMPLETED", "FAILED", "CANCELLED"}:
            raise PlanningError("only queued/running allocation runs can be cancelled", code="INVALID_STATE_TRANSITION", status_code=409)
        return await self.repo.update_allocation_run(run_id, {"status": "CANCELLED"})

    # ------------------------------------------------------------------ AIM -----------------
    async def list_calculations(self, **filters: Any) -> dict[str, Any]:
        items = await self.repo.list_replenishment_calculations(**filters)
        return {"items": items, "count": len(items)}

    async def get_calculation(self, calculation_id: str, *, include_lines: bool = False) -> dict[str, Any]:
        calculation = await self.repo.get_replenishment_calculation(calculation_id)
        if calculation is None:
            raise PlanningError("replenishment calculation not found", code="NOT_FOUND", status_code=404)
        if include_lines:
            calculation["lines"] = await self.repo.list_calculation_lines(calculation_id)
        return calculation

    async def create_calculation(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        cfg = await self._require_enabled()
        policy = e.AIM_DEFAULT_V1
        calculation = await self.repo.create_replenishment_calculation(
            {
                "run_number": await self.repo.next_number("AIM"),
                "calculation_day": _date(payload.get("calculation_day") or date.today()),
                "analysis_interval_days": int(payload.get("analysis_interval_days") or cfg.aim_analysis_interval_days),
                "seasonal_interval_days": int(payload.get("seasonal_interval_days") or cfg.aim_seasonal_interval_days),
                "formula_version": policy.formula_version,
                "status": "RUNNING",
                "scope": dict(payload.get("scope") or {}),
                "started_at": datetime.now(UTC),
            }
        )
        try:
            scope = dict(payload.get("scope") or {})
            item_ids = [str(item) for item in scope.get("items") or []]
            location_ids = [str(location) for location in scope.get("locations") or []]
            states = await self.repo.item_planning_states(item_ids=item_ids, location_ids=location_ids)
            history = await self.repo.list_demand_history(limit=2000)
            lines: list[dict[str, Any]] = []
            applied_count = 0
            skipped_manual = 0
            for state in states:
                item_history = [Decimal(str(row["qty"])) for row in history if row["item_id"] == state["item_id"] and row["location_id"] == state["location_id"]]
                result = e.compute_aim(
                    demand_by_day=[],
                    lead_time_days=int(state.get("lead_time_days") or cfg.aim_default_lead_time_days),
                    safety_stock=_dec(state.get("safety_stock", 0)),
                    quantity_available=_dec(state.get("qty_on_hand", 0)),
                    policy=policy,
                    auto_calculate_planning=cfg.auto_calculate_planning,
                )
                result["avg_daily_demand"] = e.moving_average(item_history, cfg.aim_analysis_interval_days)
                lead = result["lead_time_days_used"]
                safety = result["safety_stock_used"]
                rop = e.q12(result["avg_daily_demand"] * result["seasonal_factor"] * Decimal(lead) + safety)
                preferred = e.q12(result["avg_daily_demand"] * result["seasonal_factor"] * Decimal(lead + policy.order_interval_days) + safety)
                qty_to_order = max(Decimal("0"), e.q12(preferred - _dec(state.get("qty_on_hand", 0))))
                action = "APPLY" if cfg.auto_calculate_planning else "SKIP_MANUAL"
                if action == "APPLY":
                    applied_count += 1
                else:
                    skipped_manual += 1
                lines.append(
                    {
                        "item_id": state["item_id"], "location_id": state["location_id"],
                        "avg_daily_demand": result["avg_daily_demand"], "lead_time_days_used": lead,
                        "safety_stock_used": safety, "seasonal_factor": result["seasonal_factor"],
                        "reorder_point_calculated": rop, "preferred_stock_calculated": preferred,
                        "qty_to_order": qty_to_order, "action": action, "applied": False,
                        "formula_version": policy.formula_version,
                        "detail": {"available": e.qty_str(state.get("qty_on_hand", 0)), "order_interval_days": policy.order_interval_days},
                    }
                )
            await self.repo.insert_calculation_lines(calculation["id"], lines)
            updated = await self.repo.update_replenishment_calculation(
                calculation["id"], {"status": "COMPLETED", "finished_at": datetime.now(UTC)}
            )
            await self.repo.emit("replenishment.calculated", aggregate_id=calculation["id"], payload={"replenishment_calculation_id": calculation["id"], "formula_version": policy.formula_version, "line_count": len(lines), "applied_count": applied_count, "skipped_manual_count": skipped_manual})
            return updated
        except PlanningError:
            await self.repo.update_replenishment_calculation(calculation["id"], {"status": "FAILED", "finished_at": datetime.now(UTC)})
            raise

    async def list_calculation_lines(self, calculation_id: str, **filters: Any) -> dict[str, Any]:
        await self.get_calculation(calculation_id)
        items = await self.repo.list_calculation_lines(calculation_id, **filters)
        return {"items": items, "count": len(items)}

    async def apply_calculation(self, calculation_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        await self._require_enabled()
        calculation = await self.get_calculation(calculation_id)
        lines = await self.repo.list_calculation_lines(calculation_id, action="APPLY")
        applied = 0
        for line in lines:
            if bool(line.get("applied")):
                applied += 1
                continue
            if not bool(payload.get("dry_run")):
                await self.repo.apply_item_planning_values(
                    item_id=str(line["item_id"]), location_id=str(line["location_id"]),
                    values={
                        "reorder_point": line["reorder_point_calculated"],
                        "preferred_stock_level": line["preferred_stock_calculated"],
                        "safety_stock": line["safety_stock_used"],
                    },
                    policy_version=calculation["formula_version"],
                )
                await self.repo.update_calculation_line(line["id"], {"applied": True})
            applied += 1
        return {"calculation_id": calculation_id, "applied_count": applied, "dry_run": bool(payload.get("dry_run"))}

    async def order_items(self, **filters: Any) -> dict[str, Any]:
        calculations = await self.repo.list_replenishment_calculations(status="COMPLETED")
        if not calculations:
            return {"items": [], "count": 0}
        lines = await self.repo.list_calculation_lines(calculations[0]["id"])
        states = {(str(state["item_id"]), str(state["location_id"])): state for state in await self.repo.item_planning_states()}
        items = []
        for line in lines:
            state = states.get((str(line["item_id"]), str(line["location_id"])), {})
            available = _dec(state.get("qty_on_hand", 0))
            qty_to_order = max(Decimal("0"), e.q12(_dec(line["preferred_stock_calculated"]) - available))
            items.append(
                {
                    "item_id": line["item_id"], "location_id": line["location_id"],
                    "reorder_point": line["reorder_point_calculated"], "preferred_stock": line["preferred_stock_calculated"],
                    "available": e.qty_str(available), "qty_to_order": e.qty_str(qty_to_order),
                    "status": "BELOW_REORDER_POINT" if available <= _dec(line["reorder_point_calculated"]) else "OK",
                }
            )
        return {"items": items, "count": len(items)}

    async def reorder_reminders(self, **filters: Any) -> dict[str, Any]:
        order_items = await self.order_items(**filters)
        return {"items": [item for item in order_items["items"] if item["status"] == "BELOW_REORDER_POINT"], "count": 0}

    # ------------------------------------------------------------------ ATP -----------------
    async def atp_query(self, *, item_id: str, location_id: str, qty: Decimal | None = None, required_by: date | None = None, method: str | None = None, settings: PlanningSettings | None = None) -> dict[str, Any]:
        cfg = settings or await self.settings()
        chosen = method or ("DISCRETE_ATP" if not cfg.atp_method_policy else "DISCRETE_ATP")
        rows = await self.repo.list_atp(item_id=item_id, location_id=location_id, method=chosen)
        if not rows:
            rows = await self.repo.list_atp(item_id=item_id, location_id=location_id)
        return {
            **e.atp_query(rows=[_to_projection_row(row) for row in rows], required_qty=qty, required_by=required_by),
            "method": chosen,
            "policy_version": f"{e.ATP_POLICY_ID}/{e.ATP_POLICY_VERSION}",
            "stale_seconds": cfg.atp_consistency_lag_seconds,
        }

    async def atp_projection(self, *, item_id: str, location_id: str, date_from: date | None = None, date_to: date | None = None, method: str | None = None) -> dict[str, Any]:
        rows = await self.repo.list_atp(item_id=item_id, location_id=location_id, method=method, date_from=date_from, date_to=date_to)
        return {"items": rows, "count": len(rows), "policy_version": f"{e.ATP_POLICY_ID}/{e.ATP_POLICY_VERSION}"}

    async def rebuild_atp(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        cfg = await self._require_enabled()
        scope = dict(payload.get("scope") or {})
        item_ids = [str(item) for item in scope.get("items") or []]
        location_ids = [str(location) for location in scope.get("locations") or []]
        states = await self.repo.item_planning_states(item_ids=item_ids, location_ids=location_ids)
        hint = payload.get("method") or "DISCRETE_ATP"
        method = hint if hint in e.ATP_METHODS else "DISCRETE_ATP"
        horizon_start = _date(payload.get("as_of") or date.today())
        horizon_end = horizon_start + timedelta(days=cfg.atp_horizon_days)
        rebuilt = 0
        for state in states:
            projection = e.build_atp_projection(
                on_hand=_dec(state.get("qty_on_hand", 0)), committed=_dec(state.get("qty_committed", 0)),
                events=[], horizon_start=horizon_start, horizon_end=horizon_end,
                policy=e.AtpPolicy(method=method),
            )
            rows = [
                {
                    "bucket_date": row["bucket_date"], "policy_version": row["policy_version"],
                    "begin_qty": row["begin_qty"], "supply_qty": row["supply_qty"], "demand_qty": row["demand_qty"],
                    "projected_available": row["projected_available"], "earliest_full_date": row["earliest_full_date"],
                    "shortage_qty": row["shortage_qty"], "shortage_date": row["shortage_date"],
                    "last_rebuilt_at": datetime.now(UTC),
                }
                for row in projection["rows"]
            ]
            rebuilt += await self.repo.replace_atp_projection(item_id=state["item_id"], location_id=state["location_id"], method=method, rows=rows)
        await self.repo.emit("atp.updated", aggregate_id=str(uuid4()), payload={"scope": scope, "as_of": horizon_start.isoformat(), "method": method, "policy_version": f"{e.ATP_POLICY_ID}/{e.ATP_POLICY_VERSION}", "bucket_count": rebuilt})
        return {"rebuilt_count": rebuilt, "method": method, "policy_version": f"{e.ATP_POLICY_ID}/{e.ATP_POLICY_VERSION}"}

    # ------------------------------------------------------------------ preferences --------
    async def list_preferences(self) -> dict[str, Any]:
        cfg = await self.settings()
        return {
            "enabled": cfg.enabled,
            "policy_versions": e.policy_versions(),
            "defaults": {
                "horizon_days": cfg.horizon_days, "bucket_granularity": cfg.bucket_granularity,
                "time_fence_default_days": cfg.time_fence_default_days,
                "demand_change_threshold_percent": e.qty_str(cfg.demand_change_threshold_percent),
                "demand_method_default": cfg.demand_method_default,
                "aim_analysis_interval_days": cfg.aim_analysis_interval_days,
                "aim_seasonal_interval_days": cfg.aim_seasonal_interval_days,
                "atp_horizon_days": cfg.atp_horizon_days,
            },
        }

    async def workbench(self, **filters: Any) -> dict[str, Any]:
        plans = await self.repo.list_supply_plans(limit=20)
        messages = await self.repo.list_action_messages(status="OPEN", limit=100)
        return {"items": [{"plans": plans, "open_messages": messages}], "staleness_seconds": (await self.settings()).workbench_staleness_seconds}

    async def signals(self, **filters: Any) -> dict[str, Any]:
        messages = await self.repo.list_action_messages(status="OPEN", limit=100)
        return {"items": messages, "count": len(messages)}

    # ------------------------------------------------------------------ jobs ---------------
    async def run_demand_job(self, *, as_of: date | None = None) -> dict[str, Any]:
        plans = await self.repo.list_demand_plans(status="DRAFT")
        calculated = []
        for plan in plans:
            calculated.append(await self.calculate_demand_plan(plan["id"]))
        return {"as_of": str(as_of or date.today()), "calculated": len(calculated)}

    async def run_supply_job(self, *, as_of: date | None = None) -> dict[str, Any]:
        plans = await self.repo.list_supply_plans(status="QUEUED")
        ran = [await self.run_supply_plan(plan["id"]) for plan in plans]
        return {"as_of": str(as_of or date.today()), "runs": len(ran)}

    async def run_aim_job(self, *, calculation_day: date | None = None) -> dict[str, Any]:
        return await self.create_calculation({"calculation_day": calculation_day or date.today()})

    async def run_atp_job(self, *, as_of: date | None = None) -> dict[str, Any]:
        return await self.rebuild_atp({"as_of": as_of or date.today(), "scope": {}})


def _to_projection_row(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "bucket_date": _date(row["bucket_date"]) if isinstance(row["bucket_date"], str) else row["bucket_date"],
        "supply_qty": _dec(row.get("supply_qty", 0)), "demand_qty": _dec(row.get("demand_qty", 0)),
        "projected_available": _dec(row.get("projected_available", 0)),
        "shortage_qty": _dec(row.get("shortage_qty", 0)),
        "earliest_full_date": row.get("earliest_full_date"),
    }


__all__ = ["PLANNING_POLICY_VERSION", "PlanningService", "PlanningSettings"]
