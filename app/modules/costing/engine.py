"""FS-05 pure costing engine: methods, layers, value flow, recalculation, standard/landed/COGS/group.

The engine is **pure and synchronous** (no SQLAlchemy/DB/HTTP imports) so it can run inside FS-04's
posting transaction under the cost-object advisory lock and be unit/golden tested deterministically.
All money/quantity math uses :class:`decimal.Decimal` quantized to 12 dp (`NUMERIC(38,12)`, DM-COST-12);
no floats are ever used. Persistence is owned by :mod:`app.modules.costing.repository`.

Key entry points
----------------
* :class:`CostingEngine` — ``value_movement`` + typed helpers (inbound/outbound/move/adjust/build/...)
  returning a :class:`ValueResult` with cost-entry drafts, layer actions, GL-intent drafts, variance and
  event payloads; ``assert_postable`` implements the D5 block/provisional policy.
* :class:`CostObject` / :class:`ItemCostState` / :class:`LayerState` — immutable-ish state snapshots the
  caller (FS-04) loads/returns.
* :func:`replay_cost_ledger` / :class:`RecalculationEngine` — deterministic replay and delta planning.
* Standard costing, landed/estimated landed allocation, variance families, System COGS adjustments,
  underwater true-up and group-average recomputation helpers.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Literal
from uuid import uuid4

from app.modules.costing.errors import (
    CostingConflict,
    CostingDependencyNotDeployed,
    CostingError,
    CostingIncomplete,
    CostingPolicyViolation,
    CostingRecalcRequired,
    CostingValidationError,
)

TWELVE = Decimal("0.000000000001")
ZERO = Decimal("0")
ONE = Decimal("1")

C = Literal["CREATE", "CONSUME", "REVALUE", "CLOSE"]
MovementType = Literal["IN", "OUT", "ADJUST", "TRANSFER", "REVALUE", "LANDED", "VARIANCE", "COGS_ADJUST"]
CostingMethod = Literal["AVERAGE", "FIFO", "LIFO", "GROUP_AVERAGE", "STANDARD", "SPECIFIC", "LOT"]

COSTING_METHODS: tuple[str, ...] = (
    "AVERAGE",
    "FIFO",
    "LIFO",
    "GROUP_AVERAGE",
    "STANDARD",
    "SPECIFIC",
    "LOT",
)
LAYER_METHODS = frozenset({"FIFO", "LIFO", "SPECIFIC", "LOT"})
IDENTITY_METHODS = frozenset({"SPECIFIC", "LOT"})
VALUE_ONLY_MOVEMENTS = frozenset({"REVALUE", "LANDED", "VARIANCE", "COGS_ADJUST"})
VARIANCE_TYPES = (
    "PURCHASE_PRICE",
    "BILL_QUANTITY",
    "BILL_PRICE",
    "EXCHANGE_RATE",
    "PRODUCTION_QUANTITY",
    "PRODUCTION_COST",
    "UNBUILT",
    "WIP_REVALUATION",
    "ROUNDING",
    "LANDED_COST_ABSORPTION",
    "COGS_ADJUSTMENT",
)
ALLOCATION_BASES = ("WEIGHT", "QUANTITY", "AMOUNT", "PER_LINE")
ROUNDING_MODES = ("PER_MOVEMENT", "DOCUMENT")
RETURN_POLICIES = ("ORIGINAL_WHEN_TRACEABLE", "CURRENT")


# ==============================================================================================
# Numeric helpers (no floats)
# ==============================================================================================
def D(value: Decimal | int | str | None) -> Decimal:
    if value is None:
        return ZERO
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):  # pragma: no cover - callers must pass str/Decimal/int
        return Decimal(str(value))
    return Decimal(value)


def q12(value: Decimal | int | str) -> Decimal:
    """Quantize to the ledger/cost precision (12 dp, half-up), never via float."""
    return D(value).quantize(TWELVE, rounding=ROUND_HALF_UP)


def money_str(value: Decimal | int | str) -> str:
    return format(q12(value), "f")


def qty_str(value: Decimal | int | str) -> str:
    return format(q12(value), "f")


def allocate_largest_remainder(
    total: Decimal | int | str,
    weights: Sequence[Decimal | int | str],
) -> list[Decimal]:
    """Allocate ``total`` across ``weights`` exactly to 12 dp (deterministic largest remainder).

    Σ(result) == total exactly; a residual is assigned to the largest fractional remainders (ties by
    index). Used by landed-cost allocation (INV-12) and rollup/variance splits.
    """
    total_d = q12(total)
    weights_d = [D(weight) for weight in weights]
    if not weights_d:
        if total_d != ZERO:
            raise CostingValidationError("cannot allocate a non-zero total across zero weights")
        return []
    if any(weight < ZERO for weight in weights_d):
        raise CostingValidationError("allocation weights must be non-negative")
    weight_sum = sum(weights_d, ZERO)
    if weight_sum <= ZERO:
        raise CostingValidationError("allocation weights sum to zero")
    raw = [(total_d * weight / weight_sum) for weight in weights_d]
    result = [q12(value) for value in raw]
    residual = total_d - sum(result, ZERO)
    if residual != ZERO:
        remainders = sorted(
            range(len(raw)), key=lambda index: (-(raw[index] - result[index]), index)
        )
        step = TWELVE if residual > ZERO else -TWELVE
        for offset in range(int(abs(residual) / TWELVE)):
            result[remainders[offset % len(result)]] += step
        residual = total_d - sum(result, ZERO)
        if residual != ZERO:  # pragma: no cover - safety for absurd magnitudes
            result[0] += residual
    assert sum(result, ZERO) == total_d
    return result


def quantize_document(values: Sequence[Decimal | int | str], total: Decimal | int | str) -> list[Decimal]:
    """Document rounding: distribute a difference across rounded lines so Σ == total (INV-13)."""
    return allocate_largest_remainder(total, values)


# ==============================================================================================
# Cost objects / state snapshots
# ==============================================================================================
@dataclass(frozen=True)
class CostObject:
    """Minimal tuple identifying a cost object (ARCHITECTURE §3.1 / FS-05 §5.1)."""

    item_id: str
    costing_method: str
    location_id: str | None = None
    location_costing_group_id: str | None = None
    lot_id: str | None = None
    serial_id: str | None = None
    item_cost_id: str | None = None

    def __post_init__(self) -> None:
        method = str(self.costing_method).upper()
        if method not in COSTING_METHODS:
            raise CostingValidationError(f"unknown costing method {self.costing_method!r}")
        object.__setattr__(self, "costing_method", method)
        if method == "GROUP_AVERAGE":
            if not self.location_costing_group_id:
                raise CostingValidationError("GROUP_AVERAGE requires a location_costing_group_id")
        elif self.location_costing_group_id is not None:
            raise CostingValidationError("location_costing_group_id is only valid for GROUP_AVERAGE")
        if method in IDENTITY_METHODS:
            if not (self.lot_id or self.serial_id):
                raise CostingDependencyNotDeployed(
                    f"{method} costing requires lot/serial identity (FS-06 numbered inventory)",
                    code="DEPENDENCY_NOT_DEPLOYED",
                )
            if self.serial_id and self.lot_id:
                raise CostingValidationError("a serial identity cannot also carry a lot identity")

    @property
    def key(self) -> tuple[str | None, ...]:
        return (
            self.item_id,
            self.location_id,
            self.location_costing_group_id,
            self.lot_id,
            self.serial_id,
        )

    @property
    def uses_layers(self) -> bool:
        return self.costing_method in LAYER_METHODS


@dataclass(frozen=True)
class CostingSettings:
    """Tenant cost policy snapshot (recorded on entries for deterministic replay)."""

    block_when_recalc_pending: bool = True
    allow_provisional: bool = False
    allow_negative: bool = True
    rounding_mode: str = "PER_MOVEMENT"
    return_policy: str = "ORIGINAL_WHEN_TRACEABLE"
    numbered_inventory_available: bool = False
    period_locked: bool = False

    def __post_init__(self) -> None:
        if self.rounding_mode not in ROUNDING_MODES:
            raise CostingValidationError(f"unknown rounding_mode {self.rounding_mode!r}")
        if self.return_policy not in RETURN_POLICIES:
            raise CostingValidationError(f"unknown return costing policy {self.return_policy!r}")


@dataclass(frozen=True)
class ItemCostState:
    """Snapshot of one ``costing.item_cost`` row."""

    qty_on_hand: Decimal = ZERO
    value_base: Decimal = ZERO
    avg_cost: Decimal = ZERO
    last_purchase_price: Decimal | None = None
    status: str = "CURRENT"
    cost_provisional: bool = False
    last_cost_ledger_seq: int | None = None
    item_cost_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "qty_on_hand", q12(self.qty_on_hand))
        object.__setattr__(self, "value_base", q12(self.value_base))
        object.__setattr__(self, "avg_cost", q12(self.avg_cost))
        if self.status not in {"CURRENT", "PENDING_RECALC", "UNDERWATER"}:
            raise CostingValidationError(f"unknown item_cost status {self.status!r}")

    def with_qty_value(self, qty: Decimal, value: Decimal, **changes: Any) -> ItemCostState:
        return replace(self, qty_on_hand=q12(qty), value_base=q12(value), **changes)


@dataclass(frozen=True)
class LayerState:
    """Snapshot of one ``costing.cost_layer`` row (serialisable for the contract)."""

    layer_id: str
    layer_seq: int
    layer_date: date
    qty_original: Decimal
    qty_remaining: Decimal
    unit_cost: Decimal
    value_remaining: Decimal
    status: str = "OPEN"
    estimated: bool = False
    derived_from_layer_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "qty_original", q12(self.qty_original))
        object.__setattr__(self, "qty_remaining", q12(self.qty_remaining))
        object.__setattr__(self, "unit_cost", q12(self.unit_cost))
        object.__setattr__(self, "value_remaining", q12(self.value_remaining))
        if self.qty_remaining < ZERO or self.value_remaining < ZERO:
            raise CostingValidationError("layer remaining values cannot be negative (INV-15)")
        if self.qty_remaining > self.qty_original:
            raise CostingValidationError("layer remaining qty exceeds original")
        expected = "EXHAUSTED" if self.qty_remaining == ZERO else "OPEN"
        if self.status in {"OPEN", "EXHAUSTED"} and self.status != expected:
            object.__setattr__(self, "status", expected)

    @property
    def remaining_value_ratio(self) -> Decimal:
        if self.qty_remaining == ZERO:
            return ZERO
        return q12(self.value_remaining / self.qty_remaining)


# ==============================================================================================
# Result payloads (plain dataclasses; serialised by app.contracts.costing.schemas)
# ==============================================================================================
@dataclass(frozen=True)
class CostEntryDraft:
    movement_type: str
    qty_delta: Decimal
    unit_cost: Decimal
    value_delta: Decimal
    layer_id: str | None = None
    estimated: bool = False
    rounding_residual: bool = False
    cost_category_id: str | None = None
    variance_type: str | None = None


@dataclass(frozen=True)
class LayerAction:
    action: str
    layer_id: str
    layer_seq: int | None = None
    layer_date: date | None = None
    qty: Decimal = ZERO
    value: Decimal = ZERO
    unit_cost: Decimal = ZERO
    derived_from_layer_id: str | None = None
    estimated: bool = False
    reason: str | None = None


@dataclass(frozen=True)
class GLIntent:
    posting_rule_id: str
    account_ref: str
    debit: Decimal = ZERO
    credit: Decimal = ZERO
    qty: Decimal = ZERO
    cost_category_id: str | None = None
    dimensions: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if (self.debit > ZERO) == (self.credit > ZERO):
            raise CostingValidationError("a GL intent line must be exactly one of debit/credit")


@dataclass(frozen=True)
class EventDraft:
    event_type: str
    aggregate_type: str
    aggregate_id: str
    payload: Mapping[str, Any]
    aggregate_version: int = 1


@dataclass(frozen=True)
class ValueResult:
    unit_cost: Decimal
    value_delta: Decimal
    qty_delta: Decimal
    estimated: bool = False
    provisional: bool = False
    variance_amount: Decimal = ZERO
    cost_entries: tuple[CostEntryDraft, ...] = ()
    layer_actions: tuple[LayerAction, ...] = ()
    gl_intents: tuple[GLIntent, ...] = ()
    events: tuple[EventDraft, ...] = ()
    new_state: ItemCostState | None = None
    new_layers: tuple[LayerState, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "unit_cost": money_str(self.unit_cost),
            "value_delta": money_str(self.value_delta),
            "qty_delta": qty_str(self.qty_delta),
            "estimated": self.estimated,
            "provisional": self.provisional,
            "variance_amount": money_str(self.variance_amount),
            "cost_entries": [
                {
                    "movement_type": entry.movement_type,
                    "qty_delta": qty_str(entry.qty_delta),
                    "unit_cost": money_str(entry.unit_cost),
                    "value_delta": money_str(entry.value_delta),
                    "layer_id": entry.layer_id,
                    "estimated": entry.estimated,
                    "rounding_residual": entry.rounding_residual,
                    "cost_category_id": entry.cost_category_id,
                    "variance_type": entry.variance_type,
                }
                for entry in self.cost_entries
            ],
            "layer_actions": [
                {
                    "action": action.action,
                    "layer_id": action.layer_id,
                    "layer_seq": action.layer_seq,
                    "layer_date": action.layer_date.isoformat() if action.layer_date else None,
                    "qty": qty_str(action.qty),
                    "value": money_str(action.value),
                    "unit_cost": money_str(action.unit_cost),
                    "derived_from_layer_id": action.derived_from_layer_id,
                    "estimated": action.estimated,
                }
                for action in self.layer_actions
            ],
            "gl_intents": [
                {
                    "posting_rule_id": intent.posting_rule_id,
                    "account_ref": intent.account_ref,
                    "debit": money_str(intent.debit),
                    "credit": money_str(intent.credit),
                    "qty": qty_str(intent.qty),
                    "cost_category_id": intent.cost_category_id,
                    "dimensions": dict(intent.dimensions),
                }
                for intent in self.gl_intents
            ],
            "events": [
                {
                    "event_type": event.event_type,
                    "aggregate_type": event.aggregate_type,
                    "aggregate_id": event.aggregate_id,
                    "aggregate_version": event.aggregate_version,
                    "payload": dict(event.payload),
                }
                for event in self.events
            ],
            "new_state": (
                None
                if self.new_state is None
                else {
                    "qty_on_hand": qty_str(self.new_state.qty_on_hand),
                    "value_base": money_str(self.new_state.value_base),
                    "avg_cost": money_str(self.new_state.avg_cost),
                    "last_purchase_price": (
                        None
                        if self.new_state.last_purchase_price is None
                        else money_str(self.new_state.last_purchase_price)
                    ),
                    "status": self.new_state.status,
                    "cost_provisional": self.new_state.cost_provisional,
                    "last_cost_ledger_seq": self.new_state.last_cost_ledger_seq,
                    "item_cost_id": self.new_state.item_cost_id,
                }
            ),
            "new_layers": [
                {
                    "layer_id": layer.layer_id,
                    "layer_seq": layer.layer_seq,
                    "layer_date": layer.layer_date.isoformat(),
                    "qty_original": qty_str(layer.qty_original),
                    "qty_remaining": qty_str(layer.qty_remaining),
                    "unit_cost": money_str(layer.unit_cost),
                    "value_remaining": money_str(layer.value_remaining),
                    "status": layer.status,
                    "estimated": layer.estimated,
                    "derived_from_layer_id": layer.derived_from_layer_id,
                }
                for layer in self.new_layers
            ],
        }


@dataclass(frozen=True)
class ValueMovement:
    """One signed movement passed by the ledger to the costing engine."""

    item_id: str
    qty_delta: Decimal
    movement_type: str
    transaction_date: date
    location_id: str | None = None
    lot_id: str | None = None
    serial_id: str | None = None
    source_line_id: str | None = None
    unit_cost: Decimal | None = None
    value_delta: Decimal | None = None
    actual_unit_cost: Decimal | None = None
    standard_unit_cost: Decimal | None = None
    posting_rule_id: str = ""
    asset_account_ref: str = "ASSET"
    offset_account_ref: str = ""
    variance_account_ref: str = "VARIANCE"
    cost_category_id: str | None = None
    layer_date: date | None = None
    close_layer_id: str | None = None
    derived_from_layer_id: str | None = None
    estimated_allowed: bool | None = None
    return_original_cost: Decimal | None = None
    affects_inventory: bool = True
    reason: str | None = None

    def __post_init__(self) -> None:
        movement_type = str(self.movement_type).upper()
        if movement_type not in {
            "IN", "OUT", "ADJUST", "TRANSFER", "REVALUE", "LANDED", "VARIANCE", "COGS_ADJUST",
        }:
            raise CostingValidationError(f"unknown movement type {self.movement_type!r}")
        object.__setattr__(self, "movement_type", movement_type)
        object.__setattr__(self, "qty_delta", q12(self.qty_delta))
        if movement_type in VALUE_ONLY_MOVEMENTS:
            if self.qty_delta != ZERO:
                raise CostingValidationError(f"{movement_type} movements must be value-only (qty_delta=0)")
            if self.value_delta is None:
                raise CostingValidationError(f"{movement_type} requires value_delta")
        elif self.qty_delta == ZERO:
            raise CostingValidationError(f"{movement_type} movement requires a non-zero qty_delta")


# ==============================================================================================
# Layer algorithms (FIFO/LIFO/specific)
# ==============================================================================================
def open_layers(layers: Sequence[LayerState], *, method: str) -> list[LayerState]:
    candidates = [layer for layer in layers if layer.status == "OPEN"]
    if method == "LIFO":
        return sorted(candidates, key=lambda layer: (layer.layer_date, layer.layer_seq), reverse=True)
    return sorted(candidates, key=lambda layer: (layer.layer_date, layer.layer_seq))


def consume_layers(
    layers: Sequence[LayerState],
    qty: Decimal | int | str,
    *,
    method: str,
    allow_negative: bool,
    last_purchase_price: Decimal | None,
    estimated_allowed: bool,
    layer_date: date,
    max_serial_layer_qty: Decimal = ONE,
) -> tuple[list[LayerState], list[LayerAction], Decimal, bool, Decimal]:
    """Consume ``qty`` from open layers deterministically.

    Returns ``(new_layers, actions, total_value, estimated, estimated_unit_cost)``. Layer quantities
    never go negative (INV-15); any residual is costed from the last purchase price as an estimated
    amount (negative/underwater policy) or raises ``INSUFFICIENT_ON_HAND``/``COSTING_INCOMPLETE``.
    """
    required = q12(qty)
    if required <= ZERO:
        raise CostingValidationError("consume_layers requires a positive quantity")
    candidates = open_layers(layers, method=method)
    by_id = {layer.layer_id: layer for layer in layers}
    actions: list[LayerAction] = []
    consumed_total = ZERO
    remaining = required
    for candidate in candidates:
        if remaining <= ZERO:
            break
        if method in IDENTITY_METHODS and candidate.qty_original > max_serial_layer_qty:
            raise CostingError("specific-cost layer must be a single unit (DM-COST-11)", code="LAYER_INVALID")
        take = min(remaining, candidate.qty_remaining)
        if take <= ZERO:
            continue
        if take == candidate.qty_remaining:
            value = candidate.value_remaining
            new_remaining = ZERO
            new_value = ZERO
            status = "EXHAUSTED"
        else:
            value = q12(take * candidate.unit_cost)
            new_remaining = q12(candidate.qty_remaining - take)
            new_value = q12(candidate.value_remaining - value)
            # A full-value partial consumption must not leave a stranded residual on the final unit.
            if new_remaining == ZERO:
                new_value = ZERO
            status = "OPEN" if new_remaining > ZERO else "EXHAUSTED"
        updated = replace(
            by_id[candidate.layer_id],
            qty_remaining=new_remaining,
            value_remaining=new_value,
            status=status,
        )
        by_id[candidate.layer_id] = updated
        consumed_total += value
        remaining = q12(remaining - take)
        actions.append(
            LayerAction(
                action="CONSUME",
                layer_id=candidate.layer_id,
                layer_seq=candidate.layer_seq,
                layer_date=candidate.layer_date,
                qty=take,
                value=value,
                unit_cost=candidate.unit_cost,
                estimated=candidate.estimated,
            )
        )
    estimated = False
    estimated_unit_cost = ZERO
    if remaining > ZERO:
        if not allow_negative and not estimated_allowed:
            raise CostingIncomplete(
                "no cost basis for the outbound quantity and negative inventory is not allowed",
                code="INSUFFICIENT_ON_HAND",
                details={"missing_qty": qty_str(remaining)},
            )
        if not estimated_allowed and last_purchase_price is None:
            raise CostingIncomplete("estimated cost requires a last purchase price")
        estimated = True
        estimated_unit_cost = q12(last_purchase_price or ZERO)
        consumed_total += q12(remaining * estimated_unit_cost)
        estimated = True
    return list(by_id.values()), actions, q12(consumed_total), estimated, estimated_unit_cost


def create_layer(
    layers: Sequence[LayerState],
    *,
    qty: Decimal | int | str,
    unit_cost: Decimal | int | str,
    layer_date: date,
    estimated: bool = False,
    derived_from_layer_id: str | None = None,
    layer_id: str | None = None,
) -> tuple[LayerState, list[LayerState]]:
    qty_d = q12(qty)
    unit_d = q12(unit_cost)
    if qty_d <= ZERO:
        raise CostingValidationError("created layers require a positive quantity")
    seq = max((layer.layer_seq for layer in layers), default=0) + 1
    layer = LayerState(
        layer_id=layer_id or str(uuid4()),
        layer_seq=seq,
        layer_date=layer_date,
        qty_original=qty_d,
        qty_remaining=qty_d,
        unit_cost=unit_d,
        value_remaining=q12(qty_d * unit_d),
        status="OPEN",
        estimated=estimated,
        derived_from_layer_id=derived_from_layer_id,
    )
    return layer, [*layers, layer]


def revalue_layers(
    layers: Sequence[LayerState],
    value_delta: Decimal,
    *,
    layer_id: str | None = None,
) -> tuple[list[LayerState], list[LayerAction], Decimal]:
    """Apply a value delta to layers proportionally (or to one explicit layer); returns (layers, actions, applied)."""
    delta = q12(value_delta)
    if delta == ZERO:
        return list(layers), [], ZERO
    targets = [layer for layer in layers if layer.layer_id == layer_id] if layer_id else list(layers)
    if layer_id and not targets:
        raise CostingConflict("layer not found for revaluation", code="LAYER_NOT_FOUND")
    if not targets:
        raise CostingConflict("no layers available for revaluation", code="LAYER_NOT_FOUND")
    weights = [layer.value_remaining for layer in targets]
    if sum(weights, ZERO) == ZERO:
        weights = [layer.qty_remaining for layer in targets]
    if sum(weights, ZERO) == ZERO:
        raise CostingValidationError("cannot allocate a revaluation across empty layers")
    parts = allocate_largest_remainder(abs(delta), weights)
    sign = ONE if delta > ZERO else -ONE
    updated_by_id = {layer.layer_id: layer for layer in layers}
    actions: list[LayerAction] = []
    applied = ZERO
    for target, part in zip(targets, parts, strict=True):
        change = q12(part) * sign
        new_value = q12(target.value_remaining + change)
        if new_value < ZERO:
            raise CostingConflict(
                "revaluation would make layer value negative (INV-15)", code="LAYER_VALUE_NEGATIVE"
            )
        unit = q12(new_value / target.qty_remaining) if target.qty_remaining > ZERO else target.unit_cost
        updated = replace(target, value_remaining=new_value, unit_cost=unit)
        updated_by_id[target.layer_id] = updated
        actions.append(
            LayerAction(
                action="REVALUE",
                layer_id=target.layer_id,
                layer_seq=target.layer_seq,
                layer_date=target.layer_date,
                qty=target.qty_remaining,
                value=change,
                unit_cost=unit,
            )
        )
        applied += change
    return [updated_by_id[layer.layer_id] for layer in layers], actions, q12(applied)


# ==============================================================================================
# Engine
# ==============================================================================================
def default_offset_account(posting_rule_id: str) -> str:
    mapping = {
        "RCPT": "ACCRUED_PURCHASES",
        "COGS": "COGS",
        "ADJ": "INVENTORY_ADJUSTMENT",
        "REVALUE": "INVENTORY_REVALUATION",
        "LANDED": "ACCRUED_LANDED_COST",
        "LANDEDEST": "ACCRUED_LANDED_COST",
        "BILLVAR": "PURCHASE_PRICE_VARIANCE",
        "COGSADJ": "COGS_ADJUSTMENT",
        "XFROUT": "IN_TRANSIT",
        "XFRIN": "IN_TRANSIT",
    }
    return mapping.get(posting_rule_id, "INVENTORY_OFFSET")


class CostingEngine:
    """Pure valuation engine (one instance is stateless; all state is passed in)."""

    # ------------------------------------------------------------------ contract guards ---------
    @staticmethod
    def assert_postable(
        ctx: CostObject,
        state: ItemCostState | None,
        settings: CostingSettings,
        *,
        as_of: date | None = None,
    ) -> None:
        """D5/D6 pre-post gate; raises without touching the database.

        * pending recalculation + block policy → ``409 COSTING_RECALC_REQUIRED`` (manifest acceptance);
        * specific/lot without numbered identity → ``409 DEPENDENCY_NOT_DEPLOYED`` (FS-06);
        * closed/locked finance period → ``409 PERIOD_CLOSED`` (finance boundary projection);
        * no state/no usable cost for inbound estimation is allowed (provisional path).
        """
        del as_of  # period policy is supplied via settings.period_locked
        if settings.period_locked:
            raise CostingError("fiscal period is closed for costing", code="PERIOD_CLOSED", status_code=409)
        current = state or ItemCostState()
        if current.status == "PENDING_RECALC":
            if settings.block_when_recalc_pending:
                raise CostingRecalcRequired(
                    "item cost has a pending recalculation; retry after recalculation completes",
                    details={"item_cost_status": current.status, "flag": "costing.block_when_recalc_pending"},
                )
            if not settings.allow_provisional:
                raise CostingRecalcRequired(
                    "provisional costing is disabled for pending recalculation",
                    details={"item_cost_status": current.status},
                )
        if ctx.costing_method in IDENTITY_METHODS:
            if not settings.numbered_inventory_available:
                raise CostingDependencyNotDeployed(
                    f"{ctx.costing_method} costing requires FS-06 numbered inventory",
                    details={"costing_method": ctx.costing_method},
                )

    # ------------------------------------------------------------------ value flow -------------
    def value_movement(
        self,
        movement: ValueMovement,
        ctx: CostObject,
        state: ItemCostState | None,
        layers: Sequence[LayerState],
        settings: CostingSettings | None = None,
    ) -> ValueResult:
        engine_settings = settings or CostingSettings()
        current = state or ItemCostState(item_cost_id=ctx.item_cost_id)
        method = ctx.costing_method
        movement_type = movement.movement_type
        if movement_type in VALUE_ONLY_MOVEMENTS:
            return self._value_only(movement, ctx, current, layers, engine_settings)
        if method in {"AVERAGE", "GROUP_AVERAGE"}:
            return self._value_average(movement, ctx, current, layers, engine_settings)
        if method == "STANDARD":
            return self._value_standard(movement, ctx, current, layers, engine_settings)
        if method in LAYER_METHODS:
            return self._value_layered(movement, ctx, current, layers, engine_settings)
        raise CostingValidationError(f"unsupported costing method {method!r}")

    # typed helpers (FS-04 hook names)
    def value_inbound(self, movement: ValueMovement, ctx: CostObject, state: ItemCostState | None, layers: Sequence[LayerState], settings: CostingSettings | None = None) -> ValueResult:
        return self.value_movement(replace(movement, movement_type="IN"), ctx, state, layers, settings)

    def value_outbound(self, movement: ValueMovement, ctx: CostObject, state: ItemCostState | None, layers: Sequence[LayerState], settings: CostingSettings | None = None) -> ValueResult:
        return self.value_movement(replace(movement, movement_type="OUT"), ctx, state, layers, settings)

    def value_move(self, movement: ValueMovement, ctx: CostObject, state: ItemCostState | None, layers: Sequence[LayerState], settings: CostingSettings | None = None) -> ValueResult:
        return self.value_movement(replace(movement, movement_type="TRANSFER"), ctx, state, layers, settings)

    def value_adjust(self, movement: ValueMovement, ctx: CostObject, state: ItemCostState | None, layers: Sequence[LayerState], settings: CostingSettings | None = None) -> ValueResult:
        return self.value_movement(replace(movement, movement_type="ADJUST"), ctx, state, layers, settings)

    def value_build(self, movement: ValueMovement, ctx: CostObject, state: ItemCostState | None, layers: Sequence[LayerState], settings: CostingSettings | None = None) -> ValueResult:
        return self.value_movement(replace(movement, movement_type="IN"), ctx, state, layers, settings)

    def value_unbuild(self, movement: ValueMovement, ctx: CostObject, state: ItemCostState | None, layers: Sequence[LayerState], settings: CostingSettings | None = None) -> ValueResult:
        return self.value_movement(replace(movement, movement_type="OUT"), ctx, state, layers, settings)

    def value_wip_issue(self, movement: ValueMovement, ctx: CostObject, state: ItemCostState | None, layers: Sequence[LayerState], settings: CostingSettings | None = None) -> ValueResult:
        return self.value_movement(replace(movement, movement_type="OUT"), ctx, state, layers, settings)

    def value_wip_completion(self, movement: ValueMovement, ctx: CostObject, state: ItemCostState | None, layers: Sequence[LayerState], settings: CostingSettings | None = None) -> ValueResult:
        return self.value_movement(replace(movement, movement_type="IN"), ctx, state, layers, settings)

    def value_wip_close(self, movement: ValueMovement, ctx: CostObject, state: ItemCostState | None, layers: Sequence[LayerState], settings: CostingSettings | None = None) -> ValueResult:
        return self.value_movement(replace(movement, movement_type="COGS_ADJUST"), ctx, state, layers, settings)

    def value_return(self, movement: ValueMovement, ctx: CostObject, state: ItemCostState | None, layers: Sequence[LayerState], settings: CostingSettings | None = None) -> ValueResult:
        return self.value_movement(replace(movement, movement_type="IN"), ctx, state, layers, settings)

    def value_ownership_transfer(self, movement: ValueMovement, ctx: CostObject, state: ItemCostState | None, layers: Sequence[LayerState], settings: CostingSettings | None = None) -> ValueResult:
        # Value-neutral ownership move (T-21/22/23): identical movement on the two ownership positions.
        unit = movement.unit_cost if movement.unit_cost is not None else current_cost(state or ItemCostState(), ctx.costing_method)
        return self.value_movement(replace(movement, movement_type="TRANSFER", unit_cost=unit), ctx, state, layers, settings)

    def value_dropship(self, movement: ValueMovement, ctx: CostObject, state: ItemCostState | None, layers: Sequence[LayerState], settings: CostingSettings | None = None) -> ValueResult:
        return self.value_movement(replace(movement, movement_type="VARIANCE"), ctx, state, layers, settings)

    def value_revalue(self, movement: ValueMovement, ctx: CostObject, state: ItemCostState | None, layers: Sequence[LayerState], settings: CostingSettings | None = None) -> ValueResult:
        return self.value_movement(replace(movement, movement_type="REVALUE"), ctx, state, layers, settings)

    def value_cogs_adjust(self, movement: ValueMovement, ctx: CostObject, state: ItemCostState | None, layers: Sequence[LayerState], settings: CostingSettings | None = None) -> ValueResult:
        return self.value_movement(replace(movement, movement_type="COGS_ADJUST"), ctx, state, layers, settings)

    def value_landed_cost(self, movement: ValueMovement, ctx: CostObject, state: ItemCostState | None, layers: Sequence[LayerState], settings: CostingSettings | None = None) -> ValueResult:
        return self.value_movement(replace(movement, movement_type="LANDED"), ctx, state, layers, settings)

    def value_reverse(self, original_entries: Sequence[CostEntryDraft], ctx: CostObject, state: ItemCostState | None, layers: Sequence[LayerState], settings: CostingSettings | None = None) -> ValueResult:
        """Compensating valuation for a reversal: negate each original entry deterministically (R-1..R-4).

        Layers are restored by creating compensating layer/consumption representations; the caller posts
        the returned entries as a new T-02/reversal transaction (never mutates history).
        """
        entries = tuple(
            CostEntryDraft(
                movement_type=entry.movement_type,
                qty_delta=-q12(entry.qty_delta),
                unit_cost=entry.unit_cost,
                value_delta=-q12(entry.value_delta),
                layer_id=entry.layer_id,
                estimated=entry.estimated,
                rounding_residual=entry.rounding_residual,
                cost_category_id=entry.cost_category_id,
                variance_type=entry.variance_type,
            )
            for entry in original_entries
        )
        total = q12(sum((entry.value_delta for entry in entries), ZERO))
        qty = q12(sum((entry.qty_delta for entry in entries), ZERO))
        current = state or ItemCostState(item_cost_id=ctx.item_cost_id)
        new_state = current.with_qty_value(current.qty_on_hand + qty, current.value_base + total)
        return ValueResult(
            unit_cost=q12(abs(total) / abs(qty)) if qty != ZERO else ZERO,
            value_delta=total,
            qty_delta=qty,
            cost_entries=entries,
            new_state=new_state,
            new_layers=tuple(layers),
            events=(
                EventDraft(
                    event_type="cost.layer.updated",
                    aggregate_type="item_cost",
                    aggregate_id=ctx.item_cost_id or ctx.item_id,
                    payload={"reason": "reversal", "value_delta": money_str(total)},
                ),
            ),
        )

    # ------------------------------------------------------------------ method internals ------
    def _resolve_inbound_unit_cost(
        self,
        movement: ValueMovement,
        current: ItemCostState,
        *,
        allow_estimated: bool,
    ) -> tuple[Decimal, bool]:
        if movement.unit_cost is not None:
            return q12(movement.unit_cost), False
        if movement.return_original_cost is not None:
            return q12(movement.return_original_cost), False
        if current.last_purchase_price is not None:
            return q12(current.last_purchase_price), True
        if allow_estimated:
            return ZERO, True
        raise CostingIncomplete(
            "no receipt cost available and estimated costing is disabled",
            details={"item_id": movement.item_id},
        )

    @staticmethod
    def _inventory_state_changes(current: ItemCostState, qty_delta: Decimal, value_delta: Decimal) -> tuple[Decimal, Decimal, Decimal]:
        new_qty = q12(current.qty_on_hand + qty_delta)
        new_value = q12(current.value_base + value_delta)
        if new_qty > ZERO:
            avg = q12(new_value / new_qty)
        elif new_qty == ZERO:
            avg = current.avg_cost
        else:
            avg = current.avg_cost
        return new_qty, new_value, avg

    def _value_average(
        self,
        movement: ValueMovement,
        ctx: CostObject,
        current: ItemCostState,
        layers: Sequence[LayerState],
        settings: CostingSettings,
    ) -> ValueResult:
        if layers and any(layer.status == "OPEN" for layer in layers):
            raise CostingConflict(
                "average/group/standard cost objects must not have open layers (DM-COST-02)",
                code="LAYER_POLICY_VIOLATION",
            )
        qty = movement.qty_delta
        movement_type = movement.movement_type
        estimated = False
        variance = ZERO
        if movement_type in {"IN", "ADJUST"} and qty > ZERO or movement_type == "TRANSFER" and qty > ZERO:
            unit, estimated = self._resolve_inbound_unit_cost(
                movement, current, allow_estimated=True
            )
            value = q12(qty * unit)
            entries = [CostEntryDraft(movement_type="IN", qty_delta=qty, unit_cost=unit, value_delta=value, estimated=estimated)]
            new_qty, new_value, avg = self._inventory_state_changes(current, qty, value)
            new_state = current.with_qty_value(
                new_qty,
                new_value,
                avg_cost=avg,
                last_purchase_price=unit if unit > ZERO else current.last_purchase_price,
            )
            gl = self._gl("RCPT" if movement_type in {"IN", "ADJUST"} else "XFRIN", movement, unit, value, qty)
        elif movement_type in {"OUT", "ADJUST", "TRANSFER"}:
            require = q12(abs(qty))
            unit = current.avg_cost if current.avg_cost > ZERO else (current.last_purchase_price or movement.unit_cost or ZERO)
            if unit == ZERO:
                if not settings.allow_negative and not settings.allow_provisional:
                    raise CostingIncomplete("no average cost basis for outbound movement")
                estimated = True
            value = q12(-require * unit)
            entries = [CostEntryDraft(movement_type="OUT", qty_delta=-require, unit_cost=unit, value_delta=value, estimated=estimated)]
            new_qty, new_value, avg = self._inventory_state_changes(current, -require, value)
            new_state = current.with_qty_value(
                new_qty,
                new_value,
                avg_cost=avg,
                status="UNDERWATER" if new_qty < ZERO else "CURRENT",
            )
            gl = self._gl("COGS" if movement_type == "OUT" else "XFROUT", movement, unit, abs(value), require)
        else:  # pragma: no cover - VALUE_ONLY handled earlier
            raise CostingValidationError(f"unsupported movement {movement_type}")
        provisional = new_state.status == "PENDING_RECALC" or current.status == "PENDING_RECALC"
        events = self._events(ctx, new_state, entries, estimated)
        return ValueResult(
            unit_cost=entries[0].unit_cost,
            value_delta=q12(sum((entry.value_delta for entry in entries), ZERO)),
            qty_delta=q12(sum((entry.qty_delta for entry in entries), ZERO)),
            estimated=estimated,
            provisional=provisional,
            variance_amount=variance,
            cost_entries=tuple(entries),
            gl_intents=tuple(gl),
            events=events,
            new_state=new_state,
            new_layers=tuple(layers),
        )

    def _value_standard(
        self,
        movement: ValueMovement,
        ctx: CostObject,
        current: ItemCostState,
        layers: Sequence[LayerState],
        settings: CostingSettings,
    ) -> ValueResult:
        if layers and any(layer.status == "OPEN" for layer in layers):
            raise CostingConflict("standard cost objects must not have open layers (DM-COST-02)")
        standard = movement.standard_unit_cost
        if standard is None:
            if current.status == "CURRENT" and current.avg_cost > ZERO:
                standard = current.avg_cost
            else:
                raise CostingIncomplete("standard cost is not defined for the item/version")
        standard = q12(standard)
        qty = movement.qty_delta
        variance = ZERO
        entries: list[CostEntryDraft] = []
        gl: list[GLIntent] = []
        if qty > ZERO:
            value = q12(qty * standard)
            actual = q12(movement.actual_unit_cost) if movement.actual_unit_cost is not None else standard
            variance = q12((actual - standard) * qty)
            entries.append(CostEntryDraft("IN", qty, standard, value, estimated=False))
            if variance != ZERO:
                entries.append(
                    CostEntryDraft(
                        "VARIANCE",
                        qty_delta=ZERO,
                        unit_cost=q12(actual - standard),
                        value_delta=variance,
                        variance_type="PURCHASE_PRICE",
                    )
                )
            new_qty, new_value, _ = self._inventory_state_changes(current, qty, value)
            new_state = current.with_qty_value(new_qty, new_value, avg_cost=standard)
            if value != ZERO:
                gl.append(self._intent("RCPT", movement.asset_account_ref, debit=value))
                gl.append(self._intent("RCPT", movement.offset_account_ref or "ACCRUED_PURCHASES", credit=value))
            if variance != ZERO:
                gl.append(self._intent("BILLVAR", movement.variance_account_ref, debit=variance if variance > 0 else 0, credit=abs(variance) if variance < 0 else 0))
        else:
            require = q12(abs(qty))
            value = q12(-require * standard)
            entries.append(CostEntryDraft("OUT", -require, standard, value))
            new_qty, new_value, _ = self._inventory_state_changes(current, -require, value)
            new_state = current.with_qty_value(new_qty, new_value, avg_cost=standard)
            if value != ZERO:
                gl.append(self._intent("COGS", "COGS", debit=abs(value)))
                gl.append(self._intent("COGS", movement.asset_account_ref, credit=abs(value)))
        events = self._events(ctx, new_state, entries, False, variance)
        return ValueResult(
            unit_cost=standard,
            value_delta=q12(sum((entry.value_delta for entry in entries if entry.movement_type != "VARIANCE"), ZERO)),
            qty_delta=qty,
            variance_amount=variance,
            cost_entries=tuple(entries),
            gl_intents=tuple(gl),
            events=events,
            new_state=new_state,
            new_layers=tuple(layers),
        )

    def _value_layered(
        self,
        movement: ValueMovement,
        ctx: CostObject,
        current: ItemCostState,
        layers: Sequence[LayerState],
        settings: CostingSettings,
    ) -> ValueResult:
        qty = movement.qty_delta
        movement_type = movement.movement_type
        layer_date = movement.layer_date or movement.transaction_date
        estimated_allowed = (
            settings.allow_negative if movement.estimated_allowed is None else movement.estimated_allowed
        )
        entries: list[CostEntryDraft] = []
        actions: list[LayerAction] = []
        gl: list[GLIntent] = []
        new_layers = list(layers)
        estimated = False
        variance = ZERO
        if qty > ZERO:
            unit, unit_estimated = self._resolve_inbound_unit_cost(
                movement, current, allow_estimated=estimated_allowed
            )
            value = q12(qty * unit)
            if ctx.costing_method in IDENTITY_METHODS and qty != ONE:
                raise CostingValidationError("specific/lot inbound movement must be exactly one unit")
            new_layer, new_layers = create_layer(
                new_layers,
                qty=qty,
                unit_cost=unit,
                layer_date=layer_date,
                estimated=unit_estimated,
                derived_from_layer_id=movement.derived_from_layer_id,
            )
            actions.append(
                LayerAction(
                    action="CREATE",
                    layer_id=new_layer.layer_id,
                    layer_seq=new_layer.layer_seq,
                    layer_date=new_layer.layer_date,
                    qty=qty,
                    value=value,
                    unit_cost=unit,
                    derived_from_layer_id=new_layer.derived_from_layer_id,
                    estimated=unit_estimated,
                )
            )
            entries.append(CostEntryDraft("IN", qty, unit, value, layer_id=new_layer.layer_id, estimated=unit_estimated))
            new_qty, new_value, _ = self._inventory_state_changes(current, qty, value)
            avg = q12(new_value / new_qty) if new_qty > ZERO else current.avg_cost
            new_state = current.with_qty_value(
                new_qty,
                new_value,
                avg_cost=avg,
                last_purchase_price=unit if unit > ZERO else current.last_purchase_price,
            )
            if value != ZERO:
                gl.append(self._intent("RCPT", movement.asset_account_ref, debit=value))
                gl.append(self._intent("RCPT", movement.offset_account_ref or "ACCRUED_PURCHASES", credit=value))
            estimated = unit_estimated
        else:
            require = q12(abs(qty))
            new_layers, actions, value, estimated, est_unit = consume_layers(
                new_layers,
                require,
                method=ctx.costing_method,
                allow_negative=settings.allow_negative,
                last_purchase_price=current.last_purchase_price,
                estimated_allowed=estimated_allowed,
                layer_date=layer_date,
            )
            consumed_value = q12(-value)
            if estimated and est_unit > ZERO:
                entries.append(
                    CostEntryDraft(
                        "OUT", -require, est_unit, consumed_value, estimated=True
                    )
                )
            else:
                # Split consumed value across layer actions for the audit trail.
                for action in actions:
                    if action.action == "CONSUME":
                        entries.append(
                            CostEntryDraft(
                                "OUT",
                                -action.qty,
                                action.unit_cost,
                                -action.value,
                                layer_id=action.layer_id,
                                estimated=action.estimated,
                            )
                        )
                if sum((entry.value_delta for entry in entries), ZERO) != consumed_value:
                    residual = q12(consumed_value - sum((entry.value_delta for entry in entries), ZERO))
                    entries.append(
                        CostEntryDraft("OUT", ZERO, est_unit, residual, estimated=estimated, rounding_residual=True)
                    )
            new_qty, new_value, _ = self._inventory_state_changes(current, -require, consumed_value)
            avg = q12(new_value / new_qty) if new_qty > ZERO else current.avg_cost
            new_state = current.with_qty_value(
                new_qty,
                new_value,
                avg_cost=avg,
                status="UNDERWATER" if new_qty < ZERO else current.status,
            )
            if consumed_value != ZERO:
                gl.append(self._intent("COGS" if movement_type == "OUT" else "XFROUT", "COGS" if movement_type == "OUT" else movement.asset_account_ref, debit=abs(consumed_value)))
                gl.append(self._intent("COGS" if movement_type == "OUT" else "XFROUT", movement.asset_account_ref, credit=abs(consumed_value)))
        events = self._events(ctx, new_state, entries, estimated)
        return ValueResult(
            unit_cost=entries[0].unit_cost if entries else ZERO,
            value_delta=q12(sum((entry.value_delta for entry in entries), ZERO)),
            qty_delta=qty,
            estimated=estimated,
            variance_amount=variance,
            cost_entries=tuple(entries),
            layer_actions=tuple(actions),
            gl_intents=tuple(gl),
            events=events,
            new_state=new_state,
            new_layers=tuple(new_layers),
        )

    def _value_only(
        self,
        movement: ValueMovement,
        ctx: CostObject,
        current: ItemCostState,
        layers: Sequence[LayerState],
        settings: CostingSettings,
    ) -> ValueResult:
        delta = q12(movement.value_delta or ZERO)
        movement_type = movement.movement_type
        if movement_type == "VARIANCE":
            if not movement.affects_inventory:
                return ValueResult(
                    unit_cost=ZERO,
                    value_delta=delta,
                    qty_delta=ZERO,
                    cost_entries=(CostEntryDraft("VARIANCE", ZERO, ZERO, delta, cost_category_id=movement.cost_category_id),),
                    gl_intents=(
                        self._intent("BILLVAR", movement.variance_account_ref, debit=delta if delta > 0 else 0, credit=abs(delta) if delta < 0 else 0),
                        self._intent("BILLVAR", movement.offset_account_ref or "ACCRUED_PURCHASES", debit=abs(delta) if delta < 0 else 0, credit=delta if delta > 0 else 0),
                    ),
                    new_state=current,
                    new_layers=tuple(layers),
                )
        if delta == ZERO and movement_type not in {"VARIANCE"}:
            raise CostingValidationError(f"{movement_type} requires a non-zero value delta")
        updated_layers: Sequence[LayerState] = layers
        actions: list[LayerAction] = []
        if movement_type == "REVALUE" and ctx.uses_layers and layers:
            updated_layers, actions, applied = revalue_layers(layers, delta)
            if applied != delta:
                raise CostingConflict("revaluation could not be fully applied to layers")
        new_qty, new_value, avg = self._inventory_state_changes(current, ZERO, delta)
        if movement_type == "COGS_ADJUST" and not movement.affects_inventory:
            new_state = current
        else:
            new_state = current.with_qty_value(new_qty, new_value, avg_cost=avg)
        entries = (
            CostEntryDraft(
                movement_type=movement_type,
                qty_delta=ZERO,
                unit_cost=ZERO,
                value_delta=delta,
                cost_category_id=movement.cost_category_id,
                variance_type="WIP_REVALUATION" if movement_type == "REVALUE" else None,
            ),
        )
        rule = {
            "REVALUE": "REVALUE",
            "LANDED": "LANDED",
            "COGS_ADJUST": "COGSADJ",
            "VARIANCE": "BILLVAR",
        }[movement_type]
        debit_side = abs(delta) if delta > 0 else 0
        credit_side = abs(delta) if delta < 0 else 0
        gl = (
            self._intent(rule, movement.asset_account_ref, debit=debit_side, credit=credit_side),
            self._intent(rule, movement.offset_account_ref or default_offset_account(rule), debit=credit_side, credit=debit_side),
        )
        events = self._events(ctx, new_state, entries, False)
        return ValueResult(
            unit_cost=ZERO,
            value_delta=delta,
            qty_delta=ZERO,
            variance_amount=delta if movement_type == "VARIANCE" else ZERO,
            cost_entries=entries,
            layer_actions=tuple(actions),
            gl_intents=tuple(gl),
            events=events,
            new_state=new_state,
            new_layers=tuple(updated_layers),
        )

    # ------------------------------------------------------------------ helpers ----------------
    def _intent(self, posting_rule_id: str, account_ref: str, *, debit: Decimal | int = ZERO, credit: Decimal | int = ZERO) -> GLIntent:
        debit_d, credit_d = q12(debit), q12(credit)
        if debit_d == ZERO and credit_d == ZERO:
            raise CostingValidationError("a GL intent needs a non-zero debit or credit")
        return GLIntent(posting_rule_id=posting_rule_id, account_ref=account_ref or "UNSPECIFIED", debit=debit_d, credit=credit_d)

    def _gl(self, posting_rule_id: str, movement: ValueMovement, unit: Decimal, value: Decimal, qty: Decimal) -> list[GLIntent]:
        del unit
        abs_value = q12(abs(value))
        if abs_value == ZERO:
            return []
        if value >= ZERO:
            return [
                self._intent(posting_rule_id, movement.asset_account_ref, debit=abs_value),
                self._intent(posting_rule_id, movement.offset_account_ref or default_offset_account(posting_rule_id), credit=abs_value),
            ]
        return [
            self._intent(posting_rule_id, movement.offset_account_ref or default_offset_account(posting_rule_id), debit=abs_value),
            self._intent(posting_rule_id, movement.asset_account_ref, credit=abs_value),
        ]

    def _events(
        self,
        ctx: CostObject,
        new_state: ItemCostState,
        entries: Sequence[CostEntryDraft],
        estimated: bool,
        variance: Decimal = ZERO,
    ) -> tuple[EventDraft, ...]:
        events: list[EventDraft] = [
            EventDraft(
                event_type="cost.engine.status",
                aggregate_type="item_cost",
                aggregate_id=ctx.item_cost_id or ctx.item_id,
                payload={
                    "item_id": ctx.item_id,
                    "costing_method": ctx.costing_method,
                    "qty_on_hand": qty_str(new_state.qty_on_hand),
                    "value_base": money_str(new_state.value_base),
                    "avg_cost": money_str(new_state.avg_cost),
                    "status": new_state.status,
                    "estimated": estimated,
                    "entry_count": len(entries),
                },
            )
        ]
        for entry in entries:
            if entry.value_delta == ZERO:
                continue
            events.append(
                EventDraft(
                    event_type="cost.layer.created" if entry.movement_type == "IN" and ctx.uses_layers else "cost.layer.updated",
                    aggregate_type="item_cost",
                    aggregate_id=ctx.item_cost_id or ctx.item_id,
                    payload={
                        "item_id": ctx.item_id,
                        "movement_type": entry.movement_type,
                        "qty_delta": qty_str(entry.qty_delta),
                        "unit_cost": money_str(entry.unit_cost),
                        "value_delta": money_str(entry.value_delta),
                        "layer_id": entry.layer_id,
                        "estimated": entry.estimated,
                    },
                )
            )
        if variance != ZERO:
            events.append(
                EventDraft(
                    event_type="cost.variance.posted",
                    aggregate_type="cost_variance",
                    aggregate_id=ctx.item_cost_id or ctx.item_id,
                    payload={"item_id": ctx.item_id, "amount": money_str(variance), "variance_type": "PURCHASE_PRICE"},
                )
            )
        return tuple(events)


# ==============================================================================================
# Recalculation engine (deterministic replay + delta planning; DM-COST-04)
# ==============================================================================================
@dataclass(frozen=True)
class CostEntrySnapshot:
    item_id: str
    effective_date: date
    movement_type: str
    qty_delta: Decimal
    unit_cost: Decimal
    value_delta: Decimal
    cost_run_id: str | None = None


@dataclass(frozen=True)
class RecalcAdjustment:
    item_id: str
    effective_date: date
    value_delta: Decimal
    qty_delta: Decimal
    reason: str


def replay_cost_ledger(
    entries: Sequence[CostEntrySnapshot],
    initial_qty: Decimal = ZERO,
    initial_value: Decimal = ZERO,
) -> tuple[Decimal, Decimal]:
    """Reconstruct item qty/value by replaying cost entries in (date, insertion) order (RULE-05-02)."""
    qty, value = q12(initial_qty), q12(initial_value)
    for entry in sorted(entries, key=lambda row: row.effective_date):
        qty = q12(qty + entry.qty_delta)
        value = q12(value + entry.value_delta)
    return qty, value


class RecalculationEngine:
    """Plans cost recalculations deterministically; writes only new entries (DM-COST-04)."""

    def __init__(self, engine: CostingEngine | None = None) -> None:
        self.engine = engine or CostingEngine()

    def plan(
        self,
        *,
        recorded_entries: Sequence[CostEntrySnapshot],
        expected_entries: Sequence[CostEntrySnapshot],
        item_id: str,
        cost_run_id: str,
    ) -> list[RecalcAdjustment]:
        recorded_by_key = {
            (entry.effective_date, entry.movement_type, qty_str(entry.qty_delta)): entry
            for entry in recorded_entries
        }
        expected_by_key = {
            (entry.effective_date, entry.movement_type, qty_str(entry.qty_delta)): entry
            for entry in expected_entries
        }
        adjustments: list[RecalcAdjustment] = []
        for key in sorted(set(recorded_by_key) | set(expected_by_key), key=str):
            recorded = recorded_by_key.get(key)
            expected = expected_by_key.get(key)
            recorded_value = recorded.value_delta if recorded else ZERO
            expected_value = expected.value_delta if expected else ZERO
            delta = q12(expected_value - recorded_value)
            if delta != ZERO:
                adjustments.append(
                    RecalcAdjustment(
                        item_id=item_id,
                        effective_date=(expected or recorded).effective_date,  # type: ignore[union-attr]
                        value_delta=delta,
                        qty_delta=ZERO,
                        reason=f"recalc:{cost_run_id}",
                    )
                )
        return adjustments

    def block_policy(self, settings: CostingSettings, state: ItemCostState) -> str:
        """Return ``BLOCK``/``PROVISIONAL``/``PROCEED`` for the D5 policy decision."""
        if state.status != "PENDING_RECALC":
            return "PROCEED"
        if settings.block_when_recalc_pending:
            return "BLOCK"
        return "PROVISIONAL" if settings.allow_provisional else "BLOCK"


# ==============================================================================================
# Standard costing: categories/templates/versions/rollup/revaluation (FS-05 §5.7/§5.8)
# ==============================================================================================
@dataclass(frozen=True)
class RollupInput:
    item_id: str
    cost_category_code: str
    bom_level: int
    amount: Decimal
    breakdown: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RollupOutput:
    item_id: str
    cost_category_code: str
    bom_level: int
    old_cost: Decimal | None
    new_cost: Decimal
    delta: Decimal
    breakdown: Mapping[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "item_id": self.item_id,
            "cost_category_code": self.cost_category_code,
            "bom_level": self.bom_level,
            "old_cost": None if self.old_cost is None else money_str(self.old_cost),
            "new_cost": money_str(self.new_cost),
            "delta": money_str(self.delta),
            "breakdown": dict(self.breakdown),
        }


def compute_rollup(
    inputs: Sequence[RollupInput],
    *,
    old_costs: Mapping[tuple[str, str], Decimal] | None = None,
    material_cost_type: str = "PER_ITEM_DEFAULT",
    include_conversion_costs: bool = True,
) -> list[RollupOutput]:
    """Deterministic standard-cost rollup: aggregate component/conversion amounts per item/category.

    Inputs are the already-exploded BOM amounts per level (FS-09 supplies the BOM explosion; FS-05 owns
    the costing aggregation). Results are sorted by (item_id, category code) for reproducibility; a
    ``material_cost_type`` of ``ZERO``/``NONE`` is not supported (validated).
    """
    if material_cost_type not in {"PER_COSTING_METHOD", "PER_PURCHASE_PRICE", "PER_ITEM_DEFAULT"}:
        raise CostingValidationError(f"unknown material_cost_type {material_cost_type!r}")
    grouped: dict[tuple[str, str], Decimal] = {}
    breakdowns: dict[tuple[str, str], dict[str, Any]] = {}
    for raw in inputs:
        if raw.amount < ZERO:
            raise CostingValidationError("rollup input amounts must be non-negative")
        if not include_conversion_costs and raw.cost_category_code.upper() in {"LABOR", "MACHINE", "OVERHEAD"}:
            continue
        key = (raw.item_id, raw.cost_category_code.upper())
        grouped[key] = q12(grouped.get(key, ZERO) + raw.amount)
        level_breakdown = breakdowns.setdefault(key, {})
        level_breakdown[f"level_{raw.bom_level}"] = money_str(
            q12(D(level_breakdown.get(f"level_{raw.bom_level}", ZERO)) + raw.amount)
        )
    outputs: list[RollupOutput] = []
    for (item_id, category), amount in sorted(grouped.items()):
        old = None if old_costs is None else old_costs.get((item_id, category))
        outputs.append(
            RollupOutput(
                item_id=item_id,
                cost_category_code=category,
                bom_level=0,
                old_cost=old,
                new_cost=amount,
                delta=q12(amount - old) if old is not None else amount,
                breakdown=breakdowns[(item_id, category)],
            )
        )
    return outputs


def compute_revaluation_lines(
    rows: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Validate/compute T-24 revaluation lines: ``value_delta = qty × (new_cost − old_cost)``."""
    lines: list[dict[str, Any]] = []
    for row in rows:
        qty = q12(row["qty"])
        old = q12(row["old_cost"])
        new = q12(row["new_cost"])
        if qty <= ZERO:
            raise CostingValidationError("revaluation quantity must be positive")
        if old < ZERO or new < ZERO:
            raise CostingValidationError("revaluation costs must be non-negative")
        delta = q12(qty * (new - old))
        if delta == ZERO:
            continue
        lines.append(
            {
                "item_id": str(row["item_id"]),
                "location_id": str(row["location_id"]) if row.get("location_id") else None,
                "work_order_id": str(row["work_order_id"]) if row.get("work_order_id") else None,
                "old_cost": money_str(old),
                "new_cost": money_str(new),
                "qty": qty_str(qty),
                "value_delta": money_str(delta),
            }
        )
    if not lines:
        raise CostingValidationError("revaluation has no value-changing lines")
    total = sum((D(line["value_delta"]) for line in lines), ZERO)
    if q12(total) == ZERO:
        raise CostingConflict("revaluation total value delta is zero", code="REVALUATION_NO_DELTA")
    return lines


def validate_standard_windows(rows: Sequence[Mapping[str, Any]]) -> None:
    """Reject overlapping effective windows per item/category (DM-COST-05)."""
    by_key: dict[tuple[str, str], list[tuple[date, date | None]]] = {}
    for row in rows:
        start = row["effective_from"]
        end = row.get("effective_to")
        if not isinstance(start, date):
            start = date.fromisoformat(str(start))
        if end is not None and not isinstance(end, date):
            end = date.fromisoformat(str(end))
        if end is not None and end < start:
            raise CostingValidationError("standard_cost effective_to must be >= effective_from")
        by_key.setdefault((str(row["item_id"]), str(row["cost_category_id"])), []).append((start, end))
    for key, windows in by_key.items():
        ordered = sorted(windows, key=lambda window: window[0])
        for previous, current in zip(ordered, ordered[1:], strict=False):
            previous_end = previous[1] or date.max
            if previous_end >= current[0]:
                raise CostingConflict(
                    "standard cost effective windows overlap (DM-COST-05)",
                    code="STANDARD_COST_OVERLAP",
                    details={"item_id": key[0], "cost_category_id": key[1]},
                )


def validate_version_activation(planned: Sequence[Mapping[str, Any]], *, active: Sequence[Mapping[str, Any]] = ()) -> None:
    """A version can activate only when every planned standard is approved and no other ACTIVE overlaps."""
    if not planned:
        raise CostingValidationError("cannot activate a cost version without planned standards")
    unapproved = [row for row in planned if not row.get("is_approved")]
    if unapproved:
        raise CostingConflict(
            "cost version has unapproved planned standards", code="PLANNED_STANDARD_UNAPPROVED",
            details={"count": len(unapproved)},
        )
    if active:
        raise CostingConflict("another cost version is already active", code="COST_VERSION_ACTIVE")


def version_transition(current: str, target: str) -> None:
    allowed = {"PLANNED": {"ACTIVE"}, "ACTIVE": {"SUPERSEDED"}, "SUPERSEDED": set()}
    if target not in allowed.get(current, set()):
        raise CostingError(
            f"invalid cost_version transition {current}->{target}", code="INVALID_STATE_TRANSITION", status_code=409
        )


# ==============================================================================================
# Variance families (FS-05 §5.9)
# ==============================================================================================
def purchase_price_variance(*, billed_unit_cost: Decimal, po_unit_cost: Decimal, qty: Decimal) -> Decimal:
    return q12((q12(billed_unit_cost) - q12(po_unit_cost)) * q12(qty))


def bill_quantity_variance(*, billed_qty: Decimal, received_qty: Decimal, unit_cost: Decimal) -> Decimal:
    return q12((q12(billed_qty) - q12(received_qty)) * q12(unit_cost))


def bill_price_variance(*, billed_amount: Decimal, expected_amount: Decimal) -> Decimal:
    return q12(q12(billed_amount) - q12(expected_amount))


def exchange_rate_variance(*, amount_txn: Decimal, billed_rate: Decimal, receipt_rate: Decimal) -> Decimal:
    return q12(q12(amount_txn) * (q12(billed_rate) - q12(receipt_rate)))


def production_quantity_variance(*, standard_qty: Decimal, actual_qty: Decimal, standard_unit_cost: Decimal) -> Decimal:
    return q12((q12(actual_qty) - q12(standard_qty)) * q12(standard_unit_cost))


def production_cost_variance(*, actual_cost: Decimal, standard_cost: Decimal) -> Decimal:
    return q12(q12(actual_cost) - q12(standard_cost))


def landed_cost_absorption_variance(*, applied_landed_cost: Decimal, absorbed_landed_cost: Decimal) -> Decimal:
    return q12(q12(applied_landed_cost) - q12(absorbed_landed_cost))


VARIANCE_FUNCTIONS = {
    "PURCHASE_PRICE": purchase_price_variance,
    "BILL_QUANTITY": bill_quantity_variance,
    "BILL_PRICE": bill_price_variance,
    "EXCHANGE_RATE": exchange_rate_variance,
    "PRODUCTION_QUANTITY": production_quantity_variance,
    "PRODUCTION_COST": production_cost_variance,
    "LANDED_COST_ABSORPTION": landed_cost_absorption_variance,
}


def variance_postings(family: str, amount: Decimal, *, item_id: str, account_ref: str) -> dict[str, Any]:
    if family not in VARIANCE_FUNCTIONS:
        raise CostingValidationError(f"unknown variance family {family!r}")
    if q12(amount) == ZERO:
        raise CostingValidationError("variance amount must be non-zero")
    return {
        "variance_type": family,
        "item_id": item_id,
        "amount_base": money_str(amount),
        "account_ref": account_ref,
        "direction": "DEBIT" if amount > ZERO else "CREDIT",
    }


# ==============================================================================================
# Landed cost + estimated landed cost (FS-05 §5.11, INV-12)
# ==============================================================================================
@dataclass(frozen=True)
class ReceiptLineBasis:
    item_receipt_line_id: str
    item_id: str
    qty: Decimal
    weight: Decimal = ZERO
    amount: Decimal = ZERO


def allocate_landed_cost(
    total: Decimal | int | str,
    basis: str,
    receipt_lines: Sequence[ReceiptLineBasis],
) -> dict[str, Decimal]:
    """Allocate a landed-cost line amount across receipt lines exactly (INV-12).

    ``basis`` ∈ WEIGHT/QUANTITY/AMOUNT/PER_LINE; an all-zero basis falls back to PER_LINE. Returns
    ``{item_receipt_line_id: allocated_amount}`` with Σ == total exactly (largest remainder).
    """
    if basis not in ALLOCATION_BASES:
        raise CostingValidationError(f"unknown landed-cost allocation basis {basis!r}")
    if not receipt_lines:
        raise CostingValidationError("landed-cost allocation requires at least one posted receipt line")
    total_d = q12(total)
    if total_d < ZERO:
        raise CostingValidationError("landed-cost total must be non-negative")
    if basis == "QUANTITY":
        weights = [line.qty for line in receipt_lines]
    elif basis == "WEIGHT":
        weights = [line.weight for line in receipt_lines]
    elif basis == "AMOUNT":
        weights = [line.amount for line in receipt_lines]
    else:
        weights = [ONE for _ in receipt_lines]
    if sum((q12(weight) for weight in weights), ZERO) == ZERO:
        weights = [ONE for _ in receipt_lines]
    parts = allocate_largest_remainder(total_d, weights)
    allocation = {
        line.item_receipt_line_id: part for line, part in zip(receipt_lines, parts, strict=True)
    }
    if sum(allocation.values(), ZERO) != total_d:
        raise CostingConflict("landed-cost allocation does not sum to the line amount (INV-12)")
    return allocation


def compute_estimated_landed_cost(
    *,
    receipt_total: Decimal,
    template_lines: Sequence[Mapping[str, Any]],
    receipt_lines: Sequence[ReceiptLineBasis],
) -> list[dict[str, Any]]:
    """Apply an estimated-landed-cost template at receipt time (T-27, once per receipt line)."""
    total_d = q12(receipt_total)
    if total_d < ZERO:
        raise CostingValidationError("receipt total must be non-negative")
    outputs: list[dict[str, Any]] = []
    for line in template_lines:
        basis = str(line.get("allocation_basis", "AMOUNT"))
        if line.get("default_amount") is not None:
            amount = q12(line["default_amount"])
        elif line.get("default_percent") is not None:
            amount = q12(total_d * q12(line["default_percent"]) / Decimal("100"))
        else:
            raise CostingValidationError("estimated landed-cost template line needs amount or percent")
        if amount == ZERO:
            continue
        allocation = allocate_landed_cost(amount, basis, receipt_lines)
        for receipt_line_id, allocated in allocation.items():
            if allocated == ZERO:
                continue
            outputs.append(
                {
                    "cost_category_id": str(line.get("cost_category_id")) if line.get("cost_category_id") else None,
                    "item_receipt_line_id": receipt_line_id,
                    "allocated_amount": money_str(allocated),
                    "basis": basis,
                    "estimated": True,
                }
            )
    return outputs


# ==============================================================================================
# COGS adjustments / underwater / return costing (FS-05 §5.10, O-12)
# ==============================================================================================
def underwater_true_up(*, qty: Decimal, true_unit_cost: Decimal, estimated_unit_cost: Decimal) -> Decimal:
    """T-25 true-up for estimated/underwater costs: ``qty × (true − estimated)`` (RULE-05-09)."""
    return q12(q12(qty) * (q12(true_unit_cost) - q12(estimated_unit_cost)))


def cogs_adjustment_lines(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    lines: list[dict[str, Any]] = []
    for row in rows:
        value = q12(row["value_delta"])
        if value == ZERO:
            continue
        lines.append(
            {
                "item_id": str(row["item_id"]),
                "item_cost_id": str(row["item_cost_id"]),
                "original_transaction_line_id": (
                    str(row["original_transaction_line_id"]) if row.get("original_transaction_line_id") else None
                ),
                "value_delta": money_str(value),
                "estimated": bool(row.get("estimated", False)),
            }
        )
    if not lines:
        raise CostingValidationError("COGS adjustment has no value-changing lines")
    return lines


def return_unit_cost(
    *,
    policy: str,
    original_unit_cost: Decimal | None,
    current_method_cost: Decimal,
    traceable: bool,
) -> tuple[Decimal, bool]:
    """Return costing policy (O-12): original when traceable (default) else current method cost."""
    if policy not in RETURN_POLICIES:
        raise CostingValidationError(f"unknown return costing policy {policy!r}")
    if policy == "ORIGINAL_WHEN_TRACEABLE" and traceable and original_unit_cost is not None:
        return q12(original_unit_cost), False
    return q12(current_method_cost), False


# ==============================================================================================
# Group-average costing (FS-05 §5.12, DM-COST-08/09)
# ==============================================================================================
@dataclass(frozen=True)
class GroupMemberBalance:
    location_id: str
    qty_on_hand: Decimal
    value_base: Decimal


def recompute_group_average(members: Sequence[GroupMemberBalance]) -> tuple[Decimal, Decimal, Decimal]:
    """Recompute group qty/value/avg from member positions deterministically."""
    qty = q12(sum((member.qty_on_hand for member in members), ZERO))
    value = q12(sum((member.value_base for member in members), ZERO))
    avg = q12(value / qty) if qty > ZERO else ZERO
    return qty, value, avg


def assert_group_worksheet_allowed(*, costing_method: str, operation: str = "WORKSHEET") -> None:
    """Group-average items are prohibited on inventory worksheets (DM-COST-09/INV-28)."""
    if str(costing_method).upper() == "GROUP_AVERAGE":
        raise CostingPolicyViolation(
            "inventory worksheets are not allowed for group-average items (DM-COST-09)",
            details={"operation": operation, "costing_method": costing_method},
        )


def group_fallback_method(*, costing_method: str, location_in_group: bool) -> str:
    """Group-average items fall back to AVERAGE outside their costing group (DM-COST-08)."""
    if str(costing_method).upper() == "GROUP_AVERAGE" and not location_in_group:
        return "AVERAGE"
    return str(costing_method).upper()


def validate_group_members(*, group_currency: str, member_currencies: Sequence[str]) -> None:
    if any(currency != group_currency for currency in member_currencies):
        raise CostingConflict(
            "all members of a location costing group must share the base currency (DM-COST-08)",
            code="GROUP_CURRENCY_MISMATCH",
        )


# ==============================================================================================
# Public read helpers (contract surface)
# ==============================================================================================
def current_cost(state: ItemCostState, method: str) -> Decimal:
    method_upper = str(method).upper()
    if method_upper in {"FIFO", "LIFO", "SPECIFIC", "LOT"}:
        return q12(state.avg_cost)
    return q12(state.avg_cost)


def value_on_hand(state: ItemCostState) -> Decimal:
    return q12(state.value_base)


def layer_state(layers: Sequence[LayerState]) -> dict[str, Any]:
    return {
        "open_layers": sum(1 for layer in layers if layer.status == "OPEN"),
        "open_qty": qty_str(sum((layer.qty_remaining for layer in layers if layer.status == "OPEN"), ZERO)),
        "open_value": money_str(sum((layer.value_remaining for layer in layers if layer.status == "OPEN"), ZERO)),
        "total_layers": len(layers),
    }


def verify_value_conservation(entries: Sequence[CostEntryDraft], expected_net: Decimal | None = None) -> Decimal:
    """INV-02: sum of cost entry value deltas equals the external value change (default net 0)."""
    total = q12(sum((entry.value_delta for entry in entries), ZERO))
    expected = ZERO if expected_net is None else q12(expected_net)
    if total != expected:
        raise CostingConflict(
            f"value conservation violated: {money_str(total)} != {money_str(expected)}", code="VALUE_MISMATCH"
        )
    return total


def verify_layer_conservation(layers: Sequence[LayerState]) -> None:
    """INV-11/NV-15: remaining <= original and no negative remaining values."""
    for layer in layers:
        if layer.qty_remaining > layer.qty_original or layer.value_remaining < ZERO:
            raise CostingConflict("layer conservation violated", code="LAYER_CONSERVATION")


__all__ = [
    "ALLOCATION_BASES",
    "COSTING_METHODS",
    "CostEntryDraft",
    "CostEntrySnapshot",
    "CostingEngine",
    "CostingSettings",
    "CostObject",
    "EventDraft",
    "GLIntent",
    "GroupMemberBalance",
    "ItemCostState",
    "LayerAction",
    "LayerState",
    "RecalcAdjustment",
    "RecalculationEngine",
    "ReceiptLineBasis",
    "RollupInput",
    "RollupOutput",
    "VARIANCE_FUNCTIONS",
    "ValueMovement",
    "ValueResult",
    "allocate_landed_cost",
    "allocate_largest_remainder",
    "assert_group_worksheet_allowed",
    "bill_price_variance",
    "bill_quantity_variance",
    "cogs_adjustment_lines",
    "compute_estimated_landed_cost",
    "compute_revaluation_lines",
    "compute_rollup",
    "consume_layers",
    "create_layer",
    "current_cost",
    "exchange_rate_variance",
    "group_fallback_method",
    "landed_cost_absorption_variance",
    "layer_state",
    "money_str",
    "open_layers",
    "production_cost_variance",
    "production_quantity_variance",
    "purchase_price_variance",
    "q12",
    "qty_str",
    "recompute_group_average",
    "replay_cost_ledger",
    "return_unit_cost",
    "revalue_layers",
    "underwater_true_up",
    "validate_group_members",
    "validate_standard_windows",
    "validate_version_activation",
    "value_on_hand",
    "variance_postings",
    "verify_layer_conservation",
    "verify_value_conservation",
    "version_transition",
]
