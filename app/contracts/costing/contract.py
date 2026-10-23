"""`costing.engine@1.0.0` in-process valuation port (FS-05 facade).

FS-04's posting engine calls this facade synchronously **inside its own transaction** and under the
cost-object advisory lock; the facade itself performs no I/O. Persistence of the returned artefacts is
FS-05-owned (``app.modules.costing.repository.CostingWriter``), invoked by FS-04 in step 13 so a posting
can never commit without its cost entries (INV-17).

The facade is deliberately independent of HTTP/ORM imports so the ledger module can import it without a
circular dependency and the provider contract test can exercise it with synthetic state.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from decimal import Decimal

from app.modules.costing.engine import (
    CostEntryDraft,
    CostEntrySnapshot,
    CostingEngine,
    CostingSettings,
    CostObject,
    ItemCostState,
    LayerState,
    RecalcAdjustment,
    RecalculationEngine,
    ValueMovement,
    ValueResult,
    current_cost,
    layer_state,
    replay_cost_ledger,
    value_on_hand,
)

CONTRACT_VERSION = "1.0.0"


class CostingPort:
    """Stable in-process facade for dependants (FS-04/FS-09/FS-10)."""

    def __init__(self, engine: CostingEngine | None = None) -> None:
        self.engine = engine or CostingEngine()
        self.recalc = RecalculationEngine(self.engine)

    # -- core valuation ---------------------------------------------------------------------
    def value_movement(
        self,
        movement: ValueMovement,
        ctx: CostObject,
        state: ItemCostState | None,
        layers: Sequence[LayerState],
        settings: CostingSettings | None = None,
    ) -> ValueResult:
        return self.engine.value_movement(movement, ctx, state, layers, settings)

    def value_inbound(self, *args, **kwargs) -> ValueResult:
        return self.engine.value_inbound(*args, **kwargs)

    def value_outbound(self, *args, **kwargs) -> ValueResult:
        return self.engine.value_outbound(*args, **kwargs)

    def value_move(self, *args, **kwargs) -> ValueResult:
        return self.engine.value_move(*args, **kwargs)

    def value_adjust(self, *args, **kwargs) -> ValueResult:
        return self.engine.value_adjust(*args, **kwargs)

    def value_build(self, *args, **kwargs) -> ValueResult:
        return self.engine.value_build(*args, **kwargs)

    def value_unbuild(self, *args, **kwargs) -> ValueResult:
        return self.engine.value_unbuild(*args, **kwargs)

    def value_wip_issue(self, *args, **kwargs) -> ValueResult:
        return self.engine.value_wip_issue(*args, **kwargs)

    def value_wip_completion(self, *args, **kwargs) -> ValueResult:
        return self.engine.value_wip_completion(*args, **kwargs)

    def value_wip_close(self, *args, **kwargs) -> ValueResult:
        return self.engine.value_wip_close(*args, **kwargs)

    def value_return(self, *args, **kwargs) -> ValueResult:
        return self.engine.value_return(*args, **kwargs)

    def value_ownership_transfer(self, *args, **kwargs) -> ValueResult:
        return self.engine.value_ownership_transfer(*args, **kwargs)

    def value_dropship(self, *args, **kwargs) -> ValueResult:
        return self.engine.value_dropship(*args, **kwargs)

    def value_reverse(self, *args, **kwargs) -> ValueResult:
        return self.engine.value_reverse(*args, **kwargs)

    def value_revalue(self, *args, **kwargs) -> ValueResult:
        return self.engine.value_revalue(*args, **kwargs)

    def value_cogs_adjust(self, *args, **kwargs) -> ValueResult:
        return self.engine.value_cogs_adjust(*args, **kwargs)

    def value_landed_cost(self, *args, **kwargs) -> ValueResult:
        return self.engine.value_landed_cost(*args, **kwargs)

    # -- pre-post policy (FS-04 step 8) ------------------------------------------------------
    def assert_postable(
        self, ctx: CostObject, state: ItemCostState | None, settings: CostingSettings, *, as_of: date | None = None
    ) -> None:
        self.engine.assert_postable(ctx, state, settings, as_of=as_of)

    # -- read helpers (FS-04 guards/reconcile) ----------------------------------------------
    def current_cost(self, state: ItemCostState, method: str) -> Decimal:
        return current_cost(state, method)

    def value_on_hand(self, state: ItemCostState) -> Decimal:
        return value_on_hand(state)

    def layer_state(self, layers: Sequence[LayerState]) -> dict[str, object]:
        return layer_state(layers)

    # -- recalculation / replay -------------------------------------------------------------
    def replay(self, entries: Sequence[CostEntrySnapshot], *, initial_qty: Decimal = Decimal("0"), initial_value: Decimal = Decimal("0")) -> tuple[Decimal, Decimal]:
        return replay_cost_ledger(entries, initial_qty, initial_value)

    def plan_recalculation(
        self,
        *,
        recorded_entries: Sequence[CostEntrySnapshot],
        expected_entries: Sequence[CostEntrySnapshot],
        item_id: str,
        cost_run_id: str,
    ) -> list[RecalcAdjustment]:
        return self.recalc.plan(
            recorded_entries=recorded_entries,
            expected_entries=expected_entries,
            item_id=item_id,
            cost_run_id=cost_run_id,
        )

    def recalc_policy(self, settings: CostingSettings, state: ItemCostState) -> str:
        return self.recalc.block_policy(settings, state)

    # -- completeness guard -----------------------------------------------------------------
    def assert_complete(self, result: ValueResult, *, ledger_line_count: int = 1) -> None:
        """INV-17: one cost entry per ledger line per movement, and entries balance the value delta."""
        if ledger_line_count > 0 and not result.cost_entries:
            from app.modules.costing.errors import CostingIncomplete

            raise CostingIncomplete("costing produced no cost entries for the posting")
        total = sum((entry.value_delta for entry in result.cost_entries), Decimal("0"))
        if total != result.value_delta:
            from app.modules.costing.errors import CostingConflict

            raise CostingConflict("cost entries do not balance the value delta (INV-17)", code="COSTING_INCOMPLETE")


__all__ = ["CONTRACT_VERSION", "CostingPort", "CostEntryDraft"]
