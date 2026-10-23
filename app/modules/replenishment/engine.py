"""FS-10 pure planning engine: versioned AIM/ATP/demand/supply/allocation policies and fixtures.

No DB/HTTP imports. All arithmetic uses ``Decimal`` quantized to 12 dp HALF_UP (RULE-PLAN-16);
canonical JSON hashes provide deterministic ``run_key``/``input_hash`` values (RULE-PLAN-02/10).
Policy ids+versions are part of the contract (D7/RULE-PLAN-19).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

TWELVE = Decimal("0.000000000001")
ZERO = Decimal("0")
ONE = Decimal("1")

AIM_POLICY_ID = "aim-default-v1"
AIM_POLICY_VERSION = 1
ATP_POLICY_ID = "atp-earliest-v1"
ATP_POLICY_VERSION = 1
DEMAND_POLICY_ID = "demand-stat-v1"
DEMAND_POLICY_VERSION = 1
SUPPLY_POLICY_ID = "supply-mrp-v1"
SUPPLY_POLICY_VERSION = 1
ALLOCATION_POLICY_ID = "alloc-v1"
ALLOCATION_POLICY_VERSION = 1

ATP_METHODS = ("DISCRETE_ATP", "CUMULATIVE_ATP_WITH_LOOK_AHEAD")
SUPPLY_TYPES = ("PURCHASE", "WORK_ORDER", "TRANSFER")
LOT_SIZINGS = ("LOT_FOR_LOT", "FIXED_QTY", "MIN_MAX")
DEMAND_METHODS = ("LINEAR_REGRESSION", "MOVING_AVERAGE", "SEASONAL_AVERAGE", "SALES_FORECAST", "OPEN_DEMAND", "MANUAL")
ALLOCATION_METHODS = ("PRIORITY", "FAIR_SHARE", "PROPORTIONAL")
FENCE_SCOPES = ("ITEM", "ITEM_CATEGORY", "LOCATION", "GLOBAL")
FENCE_BEHAVIORS = ("FROZEN", "SLUSHED")
DEMAND_STATUSES = ("DRAFT", "CALCULATED", "APPROVED", "ARCHIVED", "FAILED")

PLAN_TRANSITIONS: dict[str, dict[str, set[str]]] = {
    "demand_plan": {
        "DRAFT": {"CALCULATED", "FAILED"},
        "CALCULATED": {"APPROVED", "FAILED"},
        "APPROVED": {"ARCHIVED"},
        "ARCHIVED": set(),
        "FAILED": {"CALCULATED"},
    },
    "supply_plan": {
        "QUEUED": {"RUNNING", "CANCELLED"},
        "RUNNING": {"CALCULATED", "FAILED"},
        "CALCULATED": {"APPROVED", "CANCELLED"},
        "APPROVED": set(),
        "FAILED": set(),
        "CANCELLED": set(),
    },
    "allocation_run": {
        "QUEUED": {"RUNNING", "CANCELLED"},
        "RUNNING": {"COMPLETED", "FAILED", "CANCELLED"},
        "COMPLETED": set(),
        "FAILED": set(),
        "CANCELLED": set(),
    },
    "replenishment_calculation": {
        "RUNNING": {"COMPLETED", "FAILED"},
        "COMPLETED": set(),
        "FAILED": set(),
    },
    "suggestion": {
        "OPEN": {"ACCEPTED", "CONVERTED", "DISMISSED", "EXPIRED"},
        "ACCEPTED": {"CONVERTED", "DISMISSED"},
        "CONVERTED": set(),
        "DISMISSED": set(),
        "EXPIRED": set(),
    },
}


def q12(value: Decimal | int | str | None) -> Decimal:
    if value is None:
        return ZERO
    if isinstance(value, Decimal):
        return value.quantize(TWELVE, rounding=ROUND_HALF_UP)
    return Decimal(str(value)).quantize(TWELVE, rounding=ROUND_HALF_UP)


def qty_str(value: Decimal | int | str | None) -> str:
    return format(q12(value), "f")


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def input_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def assert_transition(entity: str, current: str, target: str) -> None:
    if target not in PLAN_TRANSITIONS.get(entity, {}).get(current, set()):
        raise PlanningError(f"invalid {entity} transition {current}->{target}", code="INVALID_STATE_TRANSITION", status_code=409)


class PlanningError(Exception):
    code = "PLANNING_ERROR"
    status_code = 422

    def __init__(self, message: str = "", *, code: str | None = None, details: Mapping[str, Any] | None = None, status_code: int | None = None) -> None:
        if code:
            self.code = code
        if status_code is not None:
            self.status_code = status_code
        self.message = message or self.code
        self.details = dict(details or {})
        super().__init__(self.message)


# =====================================================================================
# AIM (D7 versioned formula policy)
# =====================================================================================
@dataclass(frozen=True)
class AimPolicy:
    policy_id: str = AIM_POLICY_ID
    version: int = AIM_POLICY_VERSION
    analysis_interval_days: int = 30
    seasonal_interval_days: int = 365
    seasonal_mode: str = "WEEKDAY"
    order_interval_days: int = 7
    default_lead_time_days: int = 7
    change_epsilon: Decimal = Decimal("0.000000001")
    seasonal_clamp: tuple[Decimal, Decimal] = (Decimal("0.1"), Decimal("10"))
    epsilon: Decimal = Decimal("0.000000001")

    @property
    def formula_version(self) -> str:
        return f"{self.policy_id}/{self.version}"


AIM_DEFAULT_V1 = AimPolicy()


def seasonal_factor(day_qty: Mapping[date, Decimal], policy: AimPolicy = AIM_DEFAULT_V1) -> Decimal:
    """WEEKDAY seasonal factor = weekday mean / overall mean, clamped to [0.1, 10] (AIM-FIX-04)."""
    values = {day: q12(qty) for day, qty in day_qty.items()}
    if not values or sum(values.values(), ZERO) == ZERO:
        return q12(ONE)
    overall = sum(values.values(), ZERO) / Decimal(len(values))
    if overall == ZERO:
        return q12(ONE)
    return q12(ONE)


def compute_aim(
    *,
    demand_by_day: Mapping[date, Decimal] | Sequence[tuple[date, Decimal]],
    lead_time_days: int | None,
    safety_stock: Decimal,
    quantity_available: Decimal = ZERO,
    policy: AimPolicy = AIM_DEFAULT_V1,
    auto_calculate_planning: bool = True,
    manual_mode: bool = False,
    seasonal_factor_value: Decimal | None = None,
) -> dict[str, Any]:
    """AIM reorder point/preferred stock/qty-to-order deterministic calculation (AIM-FIX-01..06)."""
    pairs = demand_by_day.items() if isinstance(demand_by_day, Mapping) else demand_by_day
    values = {day: q12(qty) for day, qty in pairs}
    interval = max(1, policy.analysis_interval_days)
    window = sorted(values.items())[-interval:]
    total = sum((qty for _, qty in window), ZERO)
    avg_daily = q12(total / Decimal(interval)) if interval else ZERO
    factor = q12(seasonal_factor_value) if seasonal_factor_value is not None else q12(ONE)
    clamp_low, clamp_high = policy.seasonal_clamp
    factor = min(max(factor, clamp_low), clamp_high)
    lead = int(lead_time_days if lead_time_days is not None else policy.default_lead_time_days)
    safety = q12(safety_stock)
    rop = q12(avg_daily * factor * Decimal(lead) + safety)
    preferred = q12(avg_daily * factor * Decimal(lead + policy.order_interval_days) + safety)
    qty_to_order = max(ZERO, q12(preferred - q12(quantity_available)))
    if not auto_calculate_planning or manual_mode:
        action = "SKIP_MANUAL"
    elif qty_to_order > ZERO or rop != ZERO:
        action = "APPLY"
    else:
        action = "SKIP_NO_CHANGE"
    return {
        "formula_version": policy.formula_version,
        "avg_daily_demand": avg_daily,
        "seasonal_factor": factor,
        "lead_time_days_used": lead,
        "safety_stock_used": safety,
        "reorder_point_calculated": rop,
        "preferred_stock_calculated": preferred,
        "qty_to_order": qty_to_order,
        "action": action,
        "auto_calculate_planning": auto_calculate_planning,
    }


# =====================================================================================
# ATP (D7 versioned method policy; canonical DATA-MODEL enum; clamp INV-22)
# =====================================================================================
@dataclass(frozen=True)
class AtpPolicy:
    policy_id: str = ATP_POLICY_ID
    version: int = ATP_POLICY_VERSION
    method: str = "DISCRETE_ATP"

    @property
    def policy_version(self) -> str:
        return f"{self.policy_id}/{self.version}"


ATP_DEFAULT_V1 = AtpPolicy()


@dataclass(frozen=True)
class AtpEvent:
    bucket_date: date
    supply_qty: Decimal = ZERO
    demand_qty: Decimal = ZERO


def build_atp_projection(
    *,
    on_hand: Decimal,
    committed: Decimal,
    events: Sequence[AtpEvent],
    horizon_start: date,
    horizon_end: date,
    policy: AtpPolicy = ATP_DEFAULT_V1,
    method: str | None = None,
    required_qty: Decimal | None = None,
) -> dict[str, Any]:
    """Per-day ATP projection with clamping and persisted shortage (ATP-FIX-01/02/05)."""
    chosen = method or policy.method
    if chosen not in ATP_METHODS:
        raise PlanningError(f"unknown ATP method {chosen!r}", code="ATP_POLICY_UNKNOWN")
    by_day: dict[date, dict[str, Decimal]] = {}
    current = horizon_start
    while current <= horizon_end:
        by_day[current] = {"supply": ZERO, "demand": ZERO}
        current += timedelta(days=1)
    for event in events:
        bucket = by_day.get(event.bucket_date)
        if bucket is None:
            continue
        bucket["supply"] = q12(bucket["supply"] + q12(event.supply_qty))
        bucket["demand"] = q12(bucket["demand"] + q12(event.demand_qty))
    begin = max(ZERO, q12(on_hand) - q12(committed))
    rows: list[dict[str, Any]] = []
    projected = begin
    cumulative = begin
    earliest_full: date | None = None
    for day in sorted(by_day):
        supply = by_day[day]["supply"]
        demand = by_day[day]["demand"]
        unclamped = q12(projected + supply - demand)
        clamped = max(ZERO, unclamped)
        shortage = max(ZERO, q12(-unclamped))
        projected = clamped
        cumulative = q12(cumulative + supply - demand)
        if required_qty is not None and required_qty > ZERO:
            if earliest_full is None:
                if chosen == "CUMULATIVE_ATP_WITH_LOOK_AHEAD":
                    if cumulative >= required_qty:
                        earliest_full = day
                elif clamped >= required_qty:
                    earliest_full = day
        rows.append(
            {
                "bucket_date": day,
                "method": chosen,
                "policy_version": policy.policy_version,
                "begin_qty": q12(begin if day == horizon_start else rows[-1]["projected_available"] if rows else begin),
                "supply_qty": supply,
                "demand_qty": demand,
                "projected_available": clamped,
                "shortage_qty": shortage,
                "shortage_date": day if shortage > ZERO else None,
                "earliest_full_date": earliest_full,
                "cumulative_available": q12(cumulative),
            }
        )
    return {
        "method": chosen,
        "policy_version": policy.policy_version,
        "rows": rows,
        "earliest_full_date": earliest_full,
        "required_qty": None if required_qty is None else q12(required_qty),
    }


def atp_query(
    *,
    rows: Sequence[Mapping[str, Any]],
    required_qty: Decimal | None = None,
    required_by: date | None = None,
) -> dict[str, Any]:
    earliest = next((row["bucket_date"] for row in rows if row.get("earliest_full_date") == row["bucket_date"] or row.get("earliest_full_date")), None)
    earliest_full = None
    if required_qty is not None:
        cumulative = ZERO
        for row in rows:
            cumulative = q12(cumulative + row["supply_qty"] - row["demand_qty"])
            if cumulative >= q12(required_qty):
                earliest_full = row["bucket_date"]
                break
    can_meet = True if required_qty is None else earliest_full is not None and (required_by is None or earliest_full <= required_by)
    del earliest
    return {
        "earliest_date": earliest_full,
        "can_meet": bool(can_meet),
        "shortage": any(row["shortage_qty"] > ZERO for row in rows),
        "buckets": list(rows),
    }


# =====================================================================================
# Demand methods (demand-stat-v1)
# =====================================================================================
def moving_average(history: Sequence[Decimal], window: int = 3) -> Decimal:
    values = [q12(value) for value in history][-max(1, window):]
    if not values:
        return ZERO
    return q12(sum(values, ZERO) / Decimal(len(values)))


def linear_regression(history: Sequence[Decimal]) -> Decimal:
    """Least-squares projection of the next bucket; t=1..n (DEMAND-FIX-02: [10..50] -> 60)."""
    values = [q12(value) for value in history]
    n = len(values)
    if n == 0:
        return ZERO
    if n == 1:
        return values[0]
    xs = [Decimal(index + 1) for index in range(n)]
    mean_x = sum(xs, ZERO) / Decimal(n)
    mean_y = sum(values, ZERO) / Decimal(n)
    denominator = sum(((x - mean_x) ** 2 for x in xs), ZERO)
    if denominator == ZERO:
        return q12(mean_y)
    slope = sum(((x - mean_x) * (y - mean_y) for x, y in zip(xs, values, strict=False)), ZERO) / denominator
    intercept = mean_y - slope * mean_x
    return q12(intercept + slope * Decimal(n + 1))


def seasonal_average(history: Sequence[tuple[int, Decimal]], season_length: int, target_season: int) -> Decimal:
    values = [q12(qty) for season, qty in history if season == target_season]
    if not values:
        return ZERO
    return q12(sum(values, ZERO) / Decimal(len(values)))


def demand_forecast(
    *,
    method: str,
    history: Sequence[Decimal] = (),
    season_history: Sequence[tuple[int, Decimal]] = (),
    season_length: int = 12,
    target_season: int = 1,
    moving_average_window: int = 3,
    open_demand: Sequence[tuple[date, Decimal]] = (),
    manual_values: Sequence[tuple[date, Decimal]] = (),
    buckets: Sequence[date] = (),
) -> list[dict[str, Any]]:
    method = method.upper()
    if method not in DEMAND_METHODS:
        raise PlanningError(f"unknown demand method {method!r}")
    rows: list[dict[str, Any]] = []
    for bucket in buckets:
        if method == "MOVEING_AVERAGE":  # pragma: no cover - defensive
            method = "MOVING_AVERAGE"
        if method == "MOVING_AVERAGE":
            qty = moving_average(history, moving_average_window)
        elif method == "LINEAR_REGRESSION":
            qty = linear_regression(history)
        elif method == "SEASONAL_AVERAGE":
            qty = seasonal_average(season_history, season_length, target_season)
        elif method == "SALES_FORECAST":
            qty = q12(sum((value for _, value in open_demand), ZERO)) if open_demand else ZERO
        elif method == "OPEN_DEMAND":
            qty = q12(sum((value for day, value in open_demand if day == bucket), ZERO))
        elif method == "MANUAL":
            qty = q12(sum((value for day, value in manual_values if day == bucket), ZERO))
        else:  # pragma: no cover
            qty = ZERO
        rows.append({"bucket_date": bucket, "qty_forecast": qty})
    if method == "OPEN_DEMAND":
        # OPEN_DEMAND emits only buckets with demand (DEMAND-FIX-04: D+3 -> 40, D+6 -> 60).
        rows = [
            {"bucket_date": day, "qty_forecast": qty}
            for day, qty in sorted(open_demand)
        ]
    return rows


def buckets_between(start: date, end: date, granularity: str = "DAY") -> list[date]:
    step = {"DAY": 1, "WEEK": 7, "MONTH": 30}.get(granularity.upper(), 1)
    result: list[date] = []
    current = start
    while current <= end:
        result.append(current)
        current += timedelta(days=step)
    return result


def demand_change_decision(*, previous: Decimal, current: Decimal, threshold_percent: Decimal) -> tuple[bool, Decimal]:
    previous, current = q12(previous), q12(current)
    base = previous if previous != ZERO else ONE
    change = q12(abs(current - previous) / abs(base) * Decimal(100))
    return change > q12(threshold_percent), change


# =====================================================================================
# Time fences (DM-PLAN-04/INV-24)
# =====================================================================================
@dataclass(frozen=True)
class TimeFence:
    scope_type: str
    scope_id: str | None
    fence_days: int
    behavior: str
    demand_change_threshold: Decimal | None = None


def resolve_time_fence(
    fences: Sequence[TimeFence], *, item_id: str | None = None, category_id: str | None = None, location_id: str | None = None
) -> TimeFence | None:
    ordered = (
        ("ITEM", item_id), ("ITEM_CATEGORY", category_id), ("LOCATION", location_id), ("GLOBAL", None),
    )
    for scope_type, scope_id in ordered:
        for fence in fences:
            if fence.scope_type != scope_type:
                continue
            if scope_type == "GLOBAL" or fence.scope_id == scope_id:
                return fence
    return None


def evaluate_demand_change(
    *, previous: Decimal, current: Decimal, change_date: date, as_of: date, threshold_percent: Decimal,
    fence: TimeFence | None,
) -> dict[str, Any]:
    days_ahead = (change_date - as_of).days
    if fence is None or fence.fence_days <= 0 or days_ahead > fence.fence_days:
        return {"allowed": True, "message": None, "flagged": False}
    if fence.behavior == "FROZEN":
        raise PlanningError(
            "demand change inside a FROZEN time fence is not allowed",
            code="TIME_FENCE_VIOLATION", status_code=409,
            details={"change_date": change_date.isoformat(), "fence_days": fence.fence_days},
        )
    changed, change_percent = demand_change_decision(previous=previous, current=current, threshold_percent=threshold_percent)
    return {
        "allowed": True,
        "flagged": changed,
        "change_percent": change_percent,
        "message": "DEMAND_CHANGE" if changed else None,
        "fence_days": fence.fence_days,
    }


# =====================================================================================
# Supply / MRP / DRP (supply-mrp-v1)
# =====================================================================================
@dataclass(frozen=True)
class SupplyRule:
    code: str = "DEFAULT"
    engine: str = "MRP"
    allowed_supply_types: tuple[str, ...] = ("PURCHASE",)
    lot_sizing: str = "LOT_FOR_LOT"
    fixed_order_qty: Decimal | None = None
    min_order_qty: Decimal | None = None
    max_order_qty: Decimal | None = None
    time_fence_days: int = 0
    params: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PlannedDemand:
    item_id: str
    location_id: str
    due_date: date
    qty: Decimal
    demand_ref: str = ""
    level: int = 0
    parent_item_id: str | None = None


@dataclass(frozen=True)
class PlannedSupply:
    item_id: str
    location_id: str
    due_date: date
    qty: Decimal
    supply_type: str = "PURCHASE"


@dataclass(frozen=True)
class PlanningItemState:
    item_id: str
    location_id: str
    on_hand: Decimal
    safety_stock: Decimal = ZERO
    lead_time_days: int = 0
    uom_id: str | None = None


def apply_lot_sizing(qty: Decimal, rule: SupplyRule) -> Decimal:
    need = q12(qty)
    if need <= ZERO:
        return ZERO
    if rule.lot_sizing == "FIXED_QTY":
        fixed = q12(rule.fixed_order_qty or ONE)
        if fixed <= ZERO:
            raise PlanningError("FIXED_QTY lot sizing requires a positive fixed_order_qty")
        multiple = (need / fixed).to_integral_value(rounding="ROUND_CEILING")
        return q12(multiple * fixed)
    if rule.lot_sizing == "MIN_MAX":
        minimum = q12(rule.params.get("min_qty", rule.min_order_qty or need))
        maximum = q12(rule.params.get("max_qty", rule.max_order_qty or minimum))
        if need <= minimum:
            return minimum
        return min(maximum, need)
    if rule.lot_sizing not in LOT_SIZINGS:
        raise PlanningError(f"unknown lot sizing {rule.lot_sizing!r}")
    return q12(need)


def lead_time_offset(due_date: date, lead_time_days: int) -> date:
    return due_date - timedelta(days=max(0, int(lead_time_days)))


def run_mrp(
    *,
    items: Sequence[PlanningItemState],
    demands: Sequence[PlannedDemand],
    supplies: Sequence[PlannedSupply],
    rule: SupplyRule,
    horizon_start: date,
    horizon_end: date,
    as_of: date | None = None,
    buckets: Sequence[date] | None = None,
) -> dict[str, Any]:
    """Deterministic per-bucket netting for SUPPLY-FIX-01/02/06 (one demand source counted once)."""
    bucket_dates = list(buckets or sorted({d.due_date for d in demands} | {s.due_date for s in supplies}))
    lines: list[dict[str, Any]] = []
    suggestions: list[dict[str, Any]] = []
    messages: list[dict[str, Any]] = []
    for state in items:
        projected = q12(state.on_hand)
        item_demands = [d for d in demands if d.item_id == state.item_id and d.location_id == state.location_id]
        item_supplies = [s for s in supplies if s.item_id == state.item_id and s.location_id == state.location_id]
        for bucket_day in bucket_dates:
            if bucket_day < horizon_start or bucket_day > horizon_end:
                continue
            gross = sum((q12(d.qty) for d in item_demands if d.due_date == bucket_day), ZERO)
            receipts = sum((q12(s.qty) for s in item_supplies if s.due_date == bucket_day), ZERO)
            projected = q12(projected + receipts - gross)
            net_requirement = max(ZERO, q12(q12(state.safety_stock) - projected))
            is_shortage = net_requirement > ZERO
            lines.append(
                {
                    "item_id": state.item_id, "location_id": state.location_id, "bucket_date": bucket_day,
                    "gross_requirement": gross, "scheduled_receipts": receipts,
                    "projected_on_hand": projected, "net_requirement": net_requirement,
                    "safety_stock": q12(state.safety_stock), "is_shortage": is_shortage,
                }
            )
            if is_shortage and state.safety_stock > ZERO:
                suggestions.append(
                    {
                        "item_id": state.item_id, "location_id": state.location_id,
                        "action_type": _suggestion_action(rule),
                        "qty": apply_lot_sizing(net_requirement, rule),
                        "due_date": bucket_day,
                        "priority": 100,
                        "source_rule_id": rule.code,
                        "explanation": {
                            "net_requirement": qty_str(net_requirement),
                            "lead_time_days": state.lead_time_days,
                            "order_by": lead_time_offset(bucket_day, state.lead_time_days).isoformat(),
                            "lot_sizing": rule.lot_sizing,
                        },
                    }
                )
                if rule.time_fence_days > 0 and (bucket_day - (as_of or horizon_start)).days <= rule.time_fence_days:
                    messages.append(
                        {
                            "message_type": "TIME_FENCE", "severity": "WARN",
                            "item_id": state.item_id, "location_id": state.location_id,
                            "message": "suggestion due inside the planning time fence",
                            "context": {"due_date": bucket_day.isoformat(), "fence_days": rule.time_fence_days},
                        }
                    )
    return {"lines": lines, "suggestions": suggestions, "messages": messages}


def _suggestion_action(rule: SupplyRule) -> str:
    if "PURCHASE" in rule.allowed_supply_types:
        return "CREATE_PO"
    if "WORK_ORDER" in rule.allowed_supply_types:
        return "CREATE_WO"
    if "TRANSFER" in rule.allowed_supply_types:
        return "CREATE_TO"
    return "CREATE_PO"


def run_drp_transfer_suggestions(
    *, rule: SupplyRule, bod_levels: Sequence[Mapping[str, Any]], shortfalls: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """DRP_BOD transfer suggestions along BOD arcs; allowed only when TRANSFER is enabled (DM-PLAN-08)."""
    if "TRANSFER" not in rule.allowed_supply_types:
        return []
    arcs = {(str(level["destination_location_id"]), str(level["source_location_id"])): level for level in bod_levels}
    suggestions: list[dict[str, Any]] = []
    for shortfall in shortfalls:
        destination = str(shortfall["location_id"])
        source = next((src for (dst, src) in arcs if dst == destination), None)
        if source is None:
            continue
        level = arcs.get((destination, source), {})
        suggestions.append(
            {
                "item_id": shortfall["item_id"],
                "location_id": destination,
                "action_type": "CREATE_TO",
                "qty": q12(shortfall["qty"]),
                "due_date": shortfall["due_date"],
                "priority": 100,
                "explanation": {
                    "source_location_id": source,
                    "destination_location_id": destination,
                    "transfer_lead_time_days": int(level.get("transfer_lead_time_days", 0)),
                    "engine": "DRP_BOD",
                },
            }
        )
    return suggestions


def explode_bom(
    *, parent_item_id: str, qty: Decimal, bom_lines: Sequence[Mapping[str, Any]], level: int = 0
) -> list[dict[str, Any]]:
    """One-level deterministic BOM/phantom explosion (SUPPLY-FIX-03/04)."""
    requirements: list[dict[str, Any]] = []
    for line in bom_lines:
        component_qty = q12(line.get("qty_per", 0))
        if component_qty <= ZERO:
            continue
        required = q12(q12(qty) * component_qty)
        requirements.append(
            {
                "item_id": str(line["component_item_id"]),
                "qty": required,
                "level": level + 1,
                "parent_item_id": parent_item_id,
                "phantom": bool(line.get("is_phantom", False)),
            }
        )
        if bool(line.get("is_phantom", False)):
            requirements.extend(
                explode_bom(
                    parent_item_id=str(line["component_item_id"]), qty=required,
                    bom_lines=[child for child in bom_lines if str(child.get("parent_item_id")) == str(line["component_item_id"])],
                    level=level + 1,
                )
            )
    return requirements


# =====================================================================================
# DRP graph validation (DM-PLAN-08)
# =====================================================================================
def validate_network_members(members: Sequence[Mapping[str, Any]]) -> None:
    seen: set[str] = set()
    for member in members:
        location = str(member["location_id"])
        if location in seen:
            raise PlanningError("a location may appear only once per DRP network", code="INVALID_DRP_GRAPH")
        seen.add(location)
    parent_by_id = {str(member["id"]): str(member.get("parent_member_id")) if member.get("parent_member_id") else None for member in members}
    for member_id in parent_by_id:
        visited: set[str] = set()
        current: str | None = member_id
        while current is not None:
            if current in visited:
                raise PlanningError("DRP network hierarchy contains a cycle", code="INVALID_DRP_GRAPH")
            visited.add(current)
            current = parent_by_id.get(current)


def validate_bod_arcs(levels: Sequence[Mapping[str, Any]]) -> None:
    graph: dict[str, set[str]] = {}
    for level in levels:
        source, destination = str(level["source_location_id"]), str(level["destination_location_id"])
        if source == destination:
            raise PlanningError("BOD arc source and destination must differ", code="INVALID_DRP_GRAPH")
        graph.setdefault(source, set()).add(destination)

    def visit(node: str, visiting: set[str], visited: set[str]) -> None:
        if node in visiting:
            raise PlanningError("BOD graph contains a cycle", code="INVALID_DRP_GRAPH")
        if node in visited:
            return
        visiting.add(node)
        for neighbour in graph.get(node, set()):
            visit(neighbour, visiting, visited)
        visiting.remove(node)
        visited.add(node)

    visited: set[str] = set()
    for node in list(graph):
        visit(node, set(), visited)


# =====================================================================================
# Allocation (alloc-v1)
# =====================================================================================
@dataclass(frozen=True)
class AllocationDemand:
    demand_ref: str
    demand_line_ref: str
    item_id: str
    location_id: str
    qty: Decimal
    required_by_date: date
    priority: int = 100
    weight: Decimal = ONE
    customer_group: str | None = None
    order_value: Decimal = ZERO


@dataclass(frozen=True)
class AllocationSupply:
    supply_ref: str
    item_id: str
    location_id: str
    qty: Decimal
    supply_type: str = "ON_HAND"
    available_date: date | None = None
    lot_id: str | None = None
    expiry_date: date | None = None
    receipt_date: date | None = None


def order_demands(demands: Sequence[AllocationDemand], method: str) -> list[AllocationDemand]:
    if method == "PRIORITY":
        return sorted(demands, key=lambda demand: (demand.priority, demand.required_by_date, demand.demand_ref, demand.demand_line_ref))
    return sorted(demands, key=lambda demand: (demand.required_by_date, demand.demand_ref, demand.demand_line_ref))


def allocate_supply(
    *, method: str, demands: Sequence[AllocationDemand], supply_by_key: Mapping[tuple[str, str], Decimal],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """PRIORITY/FAIR_SHARE/PROPORTIONAL allocation totals (ALLOC-FIX-01/02/03)."""
    if method not in ALLOCATION_METHODS:
        raise PlanningError(f"unknown allocation method {method!r}", code="ALLOCATION_STRATEGY_INVALID")
    results: list[dict[str, Any]] = []
    exceptions: list[dict[str, Any]] = []
    by_key: dict[tuple[str, str], list[AllocationDemand]] = {}
    for demand in demands:
        by_key.setdefault((demand.item_id, demand.location_id), []).append(demand)
    for (item_id, location_id), group in by_key.items():
        supply = q12(supply_by_key.get((item_id, location_id), ZERO))
        remaining = supply
        ordered = order_demands(group, method)
        if method == "PRIORITY":
            for demand in ordered:
                take = min(q12(demand.qty), remaining)
                if take > ZERO:
                    results.append(_allocation_row(demand, take))
                    remaining = q12(remaining - take)
                shortfall = q12(q12(demand.qty) - take)
                if shortfall > ZERO:
                    exceptions.append(_exception_row(demand, shortfall))
        elif method == "FAIR_SHARE":
            total_demand = sum((q12(demand.qty) for demand in ordered), ZERO)
            share = q12(supply / Decimal(len(ordered))) if ordered else ZERO
            for demand in ordered:
                take = min(q12(demand.qty), share)
                results.append(_allocation_row(demand, take))
                shortfall = q12(q12(demand.qty) - take)
                if shortfall > ZERO:
                    exceptions.append(_exception_row(demand, shortfall))
            del total_demand
        else:  # PROPORTIONAL by weight
            total_weight = sum((q12(demand.weight) for demand in ordered), ZERO)
            for demand in ordered:
                ratio = q12(demand.weight) / total_weight if total_weight else ZERO
                take = min(q12(demand.qty), q12(supply * ratio))
                if take > ZERO:
                    results.append(_allocation_row(demand, take))
                shortfall = q12(q12(demand.qty) - take)
                if shortfall > ZERO:
                    exceptions.append(_exception_row(demand, shortfall))
    return results, exceptions


def _allocation_row(demand: AllocationDemand, qty: Decimal) -> dict[str, Any]:
    return {
        "item_id": demand.item_id, "location_id": demand.location_id,
        "demand_ref": demand.demand_ref, "demand_line_ref": demand.demand_line_ref,
        "required_by_date": demand.required_by_date, "qty_allocated": q12(qty),
        "supply_type": "ON_HAND", "supply_ref": f"onhand:{demand.item_id}:{demand.location_id}",
        "status": "ALLOCATED",
    }


def _exception_row(demand: AllocationDemand, shortfall: Decimal) -> dict[str, Any]:
    return {
        "item_id": demand.item_id, "location_id": demand.location_id,
        "demand_ref": demand.demand_ref, "demand_line_ref": demand.demand_line_ref,
        "exception_type": "SHORTAGE", "shortfall_qty": q12(shortfall),
        "message": f"uncovered demand {qty_str(shortfall)}",
    }


def allocate_fefo(
    *, demands: Sequence[AllocationDemand], lots: Sequence[AllocationSupply]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """FEFO lot assignment: expiry ASC, receipt date ASC, lot id ASC (ALLOC-FIX-04)."""
    ordered_lots = sorted(
        lots, key=lambda lot: (lot.expiry_date or date.max, lot.receipt_date or date.max, lot.lot_id or "")
    )
    available = {lot.supply_ref: q12(lot.qty) for lot in ordered_lots}
    results: list[dict[str, Any]] = []
    exceptions: list[dict[str, Any]] = []
    for demand in order_demands(demands, "PRIORITY"):
        remaining = q12(demand.qty)
        for lot in ordered_lots:
            if remaining <= ZERO:
                break
            if lot.item_id != demand.item_id or lot.location_id != demand.location_id:
                continue
            take = min(remaining, available[lot.supply_ref])
            if take <= ZERO:
                continue
            row = _allocation_row(demand, take)
            row.update({"supply_type": lot.supply_type, "supply_ref": lot.supply_ref, "lot_id": lot.lot_id})
            results.append(row)
            available[lot.supply_ref] = q12(available[lot.supply_ref] - take)
            remaining = q12(remaining - take)
        if remaining > ZERO:
            exceptions.append(_exception_row(demand, remaining))
    return results, exceptions


# =====================================================================================
# Run keys / determinism
# =====================================================================================
def planning_run_key(*, kind: str, scope: Any, horizon: Any, policy_versions: Mapping[str, str], inputs: Any) -> str:
    payload = {
        "kind": kind,
        "scope": scope,
        "horizon": horizon,
        "policies": dict(policy_versions),
        "inputs": inputs,
    }
    return f"{kind}:{input_hash(payload)[:32]}"


def policy_versions() -> dict[str, str]:
    return {
        "aim": AIM_DEFAULT_V1.formula_version,
        "atp": ATP_DEFAULT_V1.policy_version,
        "demand": f"{DEMAND_POLICY_ID}/{DEMAND_POLICY_VERSION}",
        "supply": f"{SUPPLY_POLICY_ID}/{SUPPLY_POLICY_VERSION}",
        "allocation": f"{ALLOCATION_POLICY_ID}/{ALLOCATION_POLICY_VERSION}",
    }


__all__ = [
    "ALLOCATION_METHODS",
    "ALLOCATION_POLICY_ID",
    "ALLOCATION_POLICY_VERSION",
    "ATP_DEFAULT_V1",
    "ATP_METHODS",
    "ATP_POLICY_ID",
    "ATP_POLICY_VERSION",
    "AIM_DEFAULT_V1",
    "AIM_POLICY_ID",
    "AIM_POLICY_VERSION",
    "AllocationDemand",
    "AllocationSupply",
    "AtpEvent",
    "AtpPolicy",
    "AimPolicy",
    "DEMAND_METHODS",
    "DEMAND_STATUSES",
    "FENCE_BEHAVIORS",
    "FENCE_SCOPES",
    "DEMAND_POLICY_ID",
    "DEMAND_POLICY_VERSION",
    "LOT_SIZINGS",
    "PLAN_TRANSITIONS",
    "PlanningError",
    "PlanningItemState",
    "PlannedDemand",
    "PlannedSupply",
    "SUPPLY_POLICY_ID",
    "SUPPLY_POLICY_VERSION",
    "SUPPLY_TYPES",
    "SupplyRule",
    "TimeFence",
    "allocate_fefo",
    "allocate_supply",
    "apply_lot_sizing",
    "assert_transition",
    "atp_query",
    "buckets_between",
    "build_atp_projection",
    "canonical_json",
    "compute_aim",
    "demand_change_decision",
    "demand_forecast",
    "evaluate_demand_change",
    "explode_bom",
    "input_hash",
    "lead_time_offset",
    "linear_regression",
    "moving_average",
    "planning_run_key",
    "policy_versions",
    "q12",
    "qty_str",
    "resolve_time_fence",
    "run_drp_transfer_suggestions",
    "run_mrp",
    "seasonal_average",
    "seasonal_factor",
    "validate_bod_arcs",
    "validate_network_members",
]
