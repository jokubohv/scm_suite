"""FS-07 pure counts engine: filters, snapshot/line shaping, variance, tolerance, state machines, T-20 plan.

Pure/synchronous helpers only (no SQLAlchemy/DB/HTTP): deterministic filter evaluation, immutable
snapshot/line row builders, blind-count shaping (DM-CNT-04), variance/tolerance policy (EXT Smart Count
semantics), declared state transitions (INV-26), derived next-count schedule and the T-20 posting plan
consumed by FS-04's in-process `PostingPort.post_count_variance`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any
from uuid import UUID, uuid4

from app.modules.counts.errors import (
    CountStateError,
    CountsValidationError,
    ProhibitedCountType,
)

TWELVE = Decimal("0.000000000001")
ZERO = Decimal("0")
ONE = Decimal("1")

FREQUENCIES = ("DAILY", "WEEKLY", "MONTHLY", "QUARTERLY", "ADHOC")
FREQUENCY_DAYS = {"DAILY": 1, "WEEKLY": 7, "MONTHLY": 30, "QUARTERLY": 90, "ADHOC": 0}
PLAN_STATUSES = ("ACTIVE", "PAUSED", "ARCHIVED")
COUNT_TYPES = ("CALCULATED", "MANUAL", "CYCLE")
COUNT_STATUSES = (
    "DRAFT", "OPEN", "COUNTING", "PENDING_REVIEW", "APPROVED", "REJECTED", "POSTED", "CANCELLED",
)
LINE_STATUSES = ("PENDING", "COUNTED", "RECOUNT_REQUIRED", "APPROVED", "REJECTED")
FILTER_TYPES = ("LOCATION", "ZONE", "BIN", "ITEM", "ITEM_CATEGORY", "ABC_CLASS", "STATUS")
FILTER_OPERATORS = ("INCLUDE", "EXCLUDE")
OWNERSHIPS = ("OWNED", "CONSIGNED")

PLAN_TRANSITIONS: dict[str, set[str]] = {
    "ACTIVE": {"PAUSED", "ARCHIVED"},
    "PAUSED": {"ACTIVE", "ARCHIVED"},
    "ARCHIVED": set(),
}

COUNT_TRANSITIONS: dict[str, set[str]] = {
    "DRAFT": {"OPEN", "CANCELLED"},
    "OPEN": {"COUNTING", "CANCELLED", "OPEN"},
    "COUNTING": {"PENDING_REVIEW", "COUNTING", "CANCELLED", "REJECTED"},
    "PENDING_REVIEW": {"APPROVED", "REJECTED", "COUNTING"},
    "APPROVED": {"POSTED", "REJECTED", "CANCELLED"},
    "REJECTED": {"OPEN", "COUNTING", "CANCELLED"},
    "POSTED": set(),
    "CANCELLED": set(),
}

LINE_TRANSITIONS: dict[str, set[str]] = {
    "PENDING": {"COUNTED", "RECOUNT_REQUIRED"},
    "COUNTED": {"PENDING", "COUNTED", "RECOUNT_REQUIRED"},
    "RECOUNT_REQUIRED": {"COUNTED", "RECOUNT_REQUIRED"},
    "APPROVED": set(),
    "REJECTED": {"RECOUNT_REQUIRED"},
}


def q12(value: Decimal | int | str | None) -> Decimal:
    if value is None:
        return ZERO
    if isinstance(value, Decimal):
        return value.quantize(TWELVE, rounding=ROUND_HALF_UP)
    return Decimal(str(value)).quantize(TWELVE, rounding=ROUND_HALF_UP)


def qty_str(value: Decimal | int | str | None) -> str:
    return format(q12(value), "f")


@dataclass(frozen=True)
class CountSettings:
    enabled: bool = False
    post_on_approve: bool = True
    tolerance_qty: Decimal = ZERO
    tolerance_percent: Decimal = ZERO
    auto_approve_within_tolerance: bool = False
    default_variance_account: str | None = None
    default_interval_days: int = 30
    abc_class_intervals: Mapping[str, int] = field(
        default_factory=lambda: {"A": 7, "B": 30, "C": 90}
    )
    draft_ttl_days: int = 30
    require_lot_serial_prerequisite: bool = True

    def __post_init__(self) -> None:
        if self.default_interval_days <= 0:
            raise CountsValidationError("default_interval_days must be positive")
        if self.tolerance_qty < ZERO or self.tolerance_percent < ZERO:
            raise CountsValidationError("tolerances must be non-negative")


@dataclass(frozen=True)
class FilterSpec:
    filter_type: str
    filter_operator: str = "INCLUDE"
    value_text: str | None = None
    value_uuid: str | None = None
    sort_order: int = 0

    def __post_init__(self) -> None:
        if self.filter_type not in FILTER_TYPES:
            raise CountsValidationError(f"unknown filter_type {self.filter_type!r}")
        if self.filter_operator not in FILTER_OPERATORS:
            raise CountsValidationError(f"unknown filter_operator {self.filter_operator!r}")
        if not self.value_text and not self.value_uuid:
            raise CountsValidationError("plan filter requires value_text or value_uuid")


@dataclass(frozen=True)
class CountPosition:
    item_id: str
    location_id: str
    inventory_status_id: str
    uom_id: str
    qty_on_hand: Decimal = ZERO
    value_base: Decimal = ZERO
    ledger_sequence: int | None = None
    bin_id: str | None = None
    lot_id: str | None = None
    serial_id: str | None = None
    ownership: str = "OWNED"
    item_category_id: str | None = None
    abc_class: str | None = None
    location_included: bool = True

    def __post_init__(self) -> None:
        if self.ownership not in OWNERSHIPS:
            raise CountsValidationError(f"unknown ownership {self.ownership!r}")
        object.__setattr__(self, "qty_on_hand", q12(self.qty_on_hand))
        object.__setattr__(self, "value_base", q12(self.value_base))

    @property
    def key(self) -> tuple[str | None, ...]:
        return (
            self.item_id, self.location_id, self.bin_id, self.lot_id, self.serial_id,
            self.inventory_status_id, self.ownership,
        )

    @property
    def is_zero(self) -> bool:
        return self.qty_on_hand == ZERO


def assert_plan_transition(current: str, target: str) -> None:
    if target not in PLAN_TRANSITIONS.get(current, set()):
        raise CountStateError(f"invalid count_plan transition {current}->{target}")


def assert_count_transition(current: str, target: str) -> None:
    if target not in COUNT_TRANSITIONS.get(current, set()):
        raise CountStateError(f"invalid inventory_count transition {current}->{target}")


def assert_line_transition(current: str, target: str) -> None:
    if target not in LINE_TRANSITIONS.get(current, set()):
        raise CountStateError(f"invalid count line transition {current}->{target}")


def evaluate_filters(filters: Sequence[FilterSpec], position: CountPosition) -> bool:
    """Deterministic include/exclude evaluation: excludes win; includes union; empty includes = all."""
    includes: list[bool] = []
    for spec in sorted(filters, key=lambda item: (item.sort_order, item.filter_type)):
        matched = _filter_matches(spec, position)
        if spec.filter_operator == "EXCLUDE" and matched:
            return False
        if spec.filter_operator == "INCLUDE":
            if spec.filter_type == "ABC_CLASS" and position.abc_class is None:
                # Unmapped ABC class: ignore the filter rather than silently mis-scoping (spec §14 #3).
                continue
            includes.append(matched)
    return True if not includes else any(includes)


def _filter_matches(spec: FilterSpec, position: CountPosition) -> bool:
    value = (spec.value_text or spec.value_uuid or "").upper()
    if spec.filter_type == "LOCATION":
        return value in {position.location_id.upper(), ("INCLUDED" if position.location_included else "EXCLUDED")}
    if spec.filter_type == "ZONE":
        return True  # zone resolution happens in the repository via location hierarchy
    if spec.filter_type == "BIN":
        return position.bin_id is not None and position.bin_id.upper() == value
    if spec.filter_type == "ITEM":
        return position.item_id.upper() == value
    if spec.filter_type == "ITEM_CATEGORY":
        return position.item_category_id is not None and position.item_category_id.upper() == value
    if spec.filter_type == "ABC_CLASS":
        return position.abc_class is not None and position.abc_class.upper() == value
    if spec.filter_type == "STATUS":
        return position.inventory_status_id.upper() == value
    return False


def select_positions(
    positions: Sequence[CountPosition],
    *,
    filters: Sequence[FilterSpec] = (),
    include_zero_qty: bool = False,
    max_lines: int | None = None,
) -> list[CountPosition]:
    selected = [
        position for position in positions
        if evaluate_filters(filters, position) and (include_zero_qty or not position.is_zero)
    ]
    selected.sort(key=lambda position: tuple("" if value is None else value for value in position.key))
    if max_lines is not None:
        selected = selected[: max(1, int(max_lines))]
    return selected


# ------------------------------------------------------------------------------ snapshot -----
@dataclass(frozen=True)
class SnapshotRow:
    inventory_count_id: str
    position: CountPosition
    snapshot_at: datetime
    recalculated_from_id: str | None = None

    def as_values(self, tenant_id: str | UUID) -> dict[str, Any]:
        return {
            "id": uuid4(),
            "tenant_id": tenant_id,
            "inventory_count_id": self.inventory_count_id,
            "item_id": self.position.item_id,
            "location_id": self.position.location_id,
            "bin_id": self.position.bin_id,
            "lot_id": self.position.lot_id,
            "serial_id": self.position.serial_id,
            "inventory_status_id": self.position.inventory_status_id,
            "ownership": self.position.ownership,
            "snapshot_qty": self.position.qty_on_hand,
            "snapshot_value_base": self.position.value_base,
            "snapshot_ledger_sequence": self.position.ledger_sequence,
            "snapshot_at": self.snapshot_at,
            "recalculated_from_id": self.recalculated_from_id,
        }


def build_snapshot_rows(
    *,
    inventory_count_id: str,
    positions: Sequence[CountPosition],
    snapshot_at: datetime,
    recalculated_from_id: str | None = None,
) -> list[SnapshotRow]:
    return [
        SnapshotRow(
            inventory_count_id=inventory_count_id,
            position=position,
            snapshot_at=snapshot_at,
            recalculated_from_id=recalculated_from_id,
        )
        for position in positions
    ]


@dataclass(frozen=True)
class CountLineDraft:
    inventory_count_id: str
    line_number: int
    position: CountPosition
    count_status: str = "PENDING"

    def as_values(self, tenant_id: str | UUID) -> dict[str, Any]:
        return {
            "id": uuid4(),
            "tenant_id": tenant_id,
            "inventory_count_id": self.inventory_count_id,
            "line_number": self.line_number,
            "item_id": self.position.item_id,
            "location_id": self.position.location_id,
            "bin_id": self.position.bin_id,
            "lot_id": self.position.lot_id,
            "serial_id": self.position.serial_id,
            "inventory_status_id": self.position.inventory_status_id,
            "ownership": self.position.ownership,
            "uom_id": self.position.uom_id,
            "snapshot_qty": self.position.qty_on_hand,
            "counted_qty": None,
            "count_status": self.count_status,
        }


def build_count_lines(
    *, inventory_count_id: str, positions: Sequence[CountPosition]
) -> list[CountLineDraft]:
    return [
        CountLineDraft(inventory_count_id=inventory_count_id, line_number=index + 1, position=position)
        for index, position in enumerate(positions)
    ]


def line_variance(counted_qty: Decimal | int | str | None, snapshot_qty: Decimal | int | str) -> Decimal | None:
    """DM-CNT-03: variance = counted − snapshot (None while uncounted)."""
    if counted_qty is None:
        return None
    return q12(q12(counted_qty) - q12(snapshot_qty))


def variance_totals(lines: Sequence[Mapping[str, Any]]) -> dict[str, Decimal | int]:
    total = ZERO
    absolute = ZERO
    valued = ZERO
    counted = 0
    for line in lines:
        if line.get("counted_qty") is None:
            continue
        counted += 1
        variance = q12(line.get("variance_qty") if line.get("variance_qty") is not None else line_variance(line["counted_qty"], line["snapshot_qty"]))
        total += variance
        absolute += abs(variance)
        snapshot_value = q12(line.get("snapshot_value_base", 0))
        if q12(line.get("snapshot_qty", 0)) != ZERO:
            valued += abs(q12(variance) * snapshot_value / q12(line["snapshot_qty"]))
    return {"variance_qty_total": total, "variance_qty_abs": absolute, "variance_value_base": valued, "counted_lines": counted}


def blind_line(line: Mapping[str, Any]) -> dict[str, Any]:
    """DM-CNT-04: blind counts never expose snapshot/variance through the API."""
    hidden = dict(line)
    for key in ("snapshot_qty", "snapshot_value_base", "variance_qty"):
        hidden.pop(key, None)
    return hidden


def shape_lines(lines: Sequence[Mapping[str, Any]], *, blind: bool) -> list[dict[str, Any]]:
    return [blind_line(line) if blind else dict(line) for line in lines]


# ------------------------------------------------------------------------- tolerance policy ---
@dataclass(frozen=True)
class ToleranceDecision:
    eligible: bool
    reasons: tuple[str, ...] = ()
    within_tolerance: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {"eligible": self.eligible, "within_tolerance": self.within_tolerance, "reasons": list(self.reasons)}


def evaluate_tolerance(lines: Sequence[Mapping[str, Any]], settings: CountSettings) -> ToleranceDecision:
    """EXT Smart Count: auto-approval eligible only when every line is inside the tenant tolerance."""
    if not settings.auto_approve_within_tolerance:
        return ToleranceDecision(eligible=False, reasons=("auto_approval_disabled",))
    if settings.tolerance_qty == ZERO and settings.tolerance_percent == ZERO:
        return ToleranceDecision(eligible=False, reasons=("tolerance_disabled",))
    reasons: list[str] = []
    for line in lines:
        if line.get("counted_qty") is None:
            reasons.append(f"line_uncounted:{line.get('id')}")
            continue
        variance = abs(q12(line.get("variance_qty") if line.get("variance_qty") is not None else (line_variance(line["counted_qty"], line["snapshot_qty"]) or ZERO)))
        snapshot_value = q12(line.get("snapshot_value_base", ZERO))
        if settings.tolerance_qty > ZERO and variance > settings.tolerance_qty:
            reasons.append(f"qty_tolerance_exceeded:{line.get('id')}")
        if settings.tolerance_percent > ZERO and snapshot_value > ZERO:
            allowed = q12(snapshot_value * settings.tolerance_percent / Decimal("100"))
            if q12(variance * snapshot_value / q12(line["snapshot_qty"] or ONE)) > allowed:
                reasons.append(f"percent_tolerance_exceeded:{line.get('id')}")
    if reasons:
        return ToleranceDecision(eligible=False, reasons=tuple(reasons))
    return ToleranceDecision(eligible=True, within_tolerance=True)


# ------------------------------------------------------------------------ next count date ---
def next_count_date(
    *,
    last_count_date: date | None,
    frequency: str | None = None,
    settings: CountSettings | None = None,
    abc_class: str | None = None,
    as_of: date | None = None,
) -> tuple[date, int, str]:
    """Derived next count date (no stored item column, spec §14 #2)."""
    policy = settings or CountSettings()
    today = as_of or date.today()
    source = "DERIVED"
    interval = policy.default_interval_days
    if frequency and frequency.upper() in FREQUENCY_DAYS and frequency.upper() != "ADHOC":
        source = "PLAN"
        interval = FREQUENCY_DAYS[frequency.upper()]
    elif abc_class and abc_class.upper() in policy.abc_class_intervals:
        source = "TENANT_DEFAULT"
        interval = int(policy.abc_class_intervals[abc_class.upper()])
    if isinstance(last_count_date, str):
        base = date.fromisoformat(last_count_date)
    else:
        base = last_count_date or today
    return base + timedelta(days=max(0, interval)), interval, source


# ---------------------------------------------------------------------------- posting -------
def assert_postable_lines(lines: Sequence[Mapping[str, Any]], *, group_average_item_ids: Sequence[str] = ()) -> None:
    """DM-CNT-06 prohibitions: consigned positions never post via T-20; serial qty ∈ {0,1}."""
    group_average = {str(item).lower() for item in group_average_item_ids}
    for line in lines:
        if line.get("counted_qty") is None:
            continue
        if str(line.get("ownership", "OWNED")).upper() == "CONSIGNED":
            raise ProhibitedCountType(
                "consigned positions cannot post through T-20; route through the consigned adjustment path",
                details={"line_id": str(line.get("id")), "ownership": "CONSIGNED"},
            )
        variance = line_variance(line["counted_qty"], line["snapshot_qty"]) or ZERO
        if variance == ZERO:
            continue
        if line.get("serial_id") is not None and abs(variance) > ONE:
            raise CountsValidationError("serial count line variance must be within one unit")
        if str(line.get("item_id", "")).lower() in group_average:
            raise ProhibitedCountType(
                "group-average positions cannot post count variances",
                details={"line_id": str(line.get("id")), "reason": "GROUP_AVERAGE_WORKSHEET_PROHIBITED"},
            )


def build_t20_movements(
    *,
    count: Mapping[str, Any],
    lines: Sequence[Mapping[str, Any]],
    reason_code_id: str | None = None,
    default_uom_id: str | None = None,
) -> list[dict[str, Any]]:
    """Build the T-20 COUNT_VARIANCE movements (per-line delta = counted − snapshot, DM-CNT-03)."""
    movements: list[dict[str, Any]] = []
    for line in lines:
        if line.get("counted_qty") is None:
            continue
        variance = line_variance(line["counted_qty"], line["snapshot_qty"])
        if variance == ZERO:
            continue
        uom = line.get("uom_id") or default_uom_id
        if uom is None:
            raise CountsValidationError("count line is missing its base UoM", details={"line_id": str(line.get("id"))})
        movement: dict[str, Any] = {
            "item_id": str(line["item_id"]),
            "location_id": str(line["location_id"]),
            "inventory_status_id": str(line["inventory_status_id"]),
            "ownership": str(line.get("ownership", "OWNED")),
            "qty": variance,
            "qty_uom": variance,
            "conversion_factor": ONE,
            "uom_id": str(uom),
            "movement_type": "COUNT_VARIANCE",
            "source_line_ref": str(line.get("id")),
            "unit_cost": (
                q12(line["snapshot_value_base"]) / q12(line["snapshot_qty"])
                if line.get("snapshot_value_base") is not None and q12(line.get("snapshot_qty", 0)) != ZERO
                else None
            ),
            "value_base": ZERO,
        }
        for field_name in ("bin_id", "lot_id", "serial_id"):
            if line.get(field_name) is not None:
                movement[field_name] = str(line[field_name])
        if reason_code_id is not None:
            movement["reason_code_id"] = str(reason_code_id)
        movements.append(movement)
    return movements


def build_t20_command(
    *,
    count: Mapping[str, Any],
    lines: Sequence[Mapping[str, Any]],
    reason_code_id: str | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    movements = build_t20_movements(count=count, lines=lines, reason_code_id=reason_code_id)
    return {
        "posting_type": "T-20",
        "transaction_date": count["count_date"],
        "source_doc_type": "INVENTORY_COUNT",
        "source_doc_id": str(count["id"]),
        "external_ref": str(count["count_number"]),
        "idempotency_key": idempotency_key or f"count:{count['id']}:post",
        "memo": f"Inventory count {count['count_number']} variance",
        "movements": movements,
    }


def count_ref(count: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "count_id": str(count["id"]),
        "count_number": count["count_number"],
        "status": count["status"],
        "count_type": count["count_type"],
        "count_date": str(count["count_date"]),
        "count_round": int(count.get("count_round", 1)),
    }


def assert_snapshot_consistency(lines: Sequence[Mapping[str, Any]], snapshots: Sequence[Mapping[str, Any]]) -> None:
    """DM-CNT-01: line snapshot must equal the latest snapshot revision for its position."""
    latest: dict[tuple[Any, ...], Mapping[str, Any]] = {}
    for snapshot_row in snapshots:
        key = (snapshot_row["item_id"], snapshot_row["location_id"], snapshot_row.get("bin_id"), snapshot_row.get("lot_id"), snapshot_row.get("serial_id"), snapshot_row["inventory_status_id"], snapshot_row.get("ownership"))
        existing = latest.get(key)
        if existing is None or str(snapshot_row["snapshot_at"]) >= str(existing["snapshot_at"]):
            latest[key] = snapshot_row
    for line in lines:
        key = (line["item_id"], line["location_id"], line.get("bin_id"), line.get("lot_id"), line.get("serial_id"), line["inventory_status_id"], line.get("ownership"))
        latest_row = latest.get(key)
        if latest_row is None:
            raise CountsValidationError("count line has no snapshot revision", details={"line_id": str(line.get("id"))})
        if q12(latest_row["snapshot_qty"]) != q12(line["snapshot_qty"]):
            raise CountsValidationError("count line snapshot differs from the latest revision (DM-CNT-01)", details={"line_id": str(line.get("id"))})


def derive_counted_snapshot(lines: Sequence[Mapping[str, Any]], recount: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Apply append-only recount attempts; the accepted (latest) attempt is copied to the line (DM-CNT-07)."""
    accepted: dict[Any, Mapping[str, Any]] = {}
    for recount_row in sorted(recount, key=lambda row: int(row["attempt"])):
        accepted[recount_row["inventory_count_line_id"]] = recount_row
    merged: list[dict[str, Any]] = []
    for line in lines:
        merged_line: dict[str, Any] = dict(line)
        accepted_attempt = accepted.get(line.get("id"))
        if accepted_attempt is not None and merged_line.get("counted_qty") is None:
            merged_line["counted_qty"] = accepted_attempt["counted_qty"]
            merged_line["variance_qty"] = line_variance(accepted_attempt["counted_qty"], line["snapshot_qty"])
        merged.append(merged_line)
    return merged


__all__ = [
    "COUNT_STATUSES",
    "COUNT_TRANSITIONS",
    "COUNT_TYPES",
    "CountLineDraft",
    "CountPosition",
    "CountSettings",
    "FilterSpec",
    "FREQUENCIES",
    "FREQUENCY_DAYS",
    "LINE_STATUSES",
    "PLAN_TRANSITIONS",
    "SnapshotRow",
    "ToleranceDecision",
    "assert_count_transition",
    "assert_line_transition",
    "assert_plan_transition",
    "assert_postable_lines",
    "assert_snapshot_consistency",
    "blind_line",
    "build_count_lines",
    "build_snapshot_rows",
    "build_t20_command",
    "build_t20_movements",
    "count_ref",
    "derive_counted_snapshot",
    "evaluate_filters",
    "evaluate_tolerance",
    "line_variance",
    "next_count_date",
    "q12",
    "qty_str",
    "select_positions",
    "shape_lines",
    "variance_totals",
]
