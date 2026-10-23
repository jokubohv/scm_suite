"""FS-05 unit tests: rounding, layer algorithms, standard/landed/COGS/group pure helpers."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.modules.costing.engine import (
    ItemCostState,
    LayerState,
    ReceiptLineBasis,
    RollupInput,
    allocate_largest_remainder,
    assert_group_worksheet_allowed,
    bill_quantity_variance,
    cogs_adjustment_lines,
    compute_estimated_landed_cost,
    compute_rollup,
    consume_layers,
    create_layer,
    current_cost,
    exchange_rate_variance,
    group_fallback_method,
    purchase_price_variance,
    q12,
    recompute_group_average,
    return_unit_cost,
    revalue_layers,
    underwater_true_up,
    validate_standard_windows,
    variance_postings,
)
from app.modules.costing.errors import CostingConflict, CostingPolicyViolation, CostingValidationError


def test_quantize_never_floats() -> None:
    assert q12(Decimal("1.2345678901234")) == Decimal("1.234567890123")
    assert isinstance(q12(Decimal("1.5")), Decimal)


def test_largest_remainder_allocates_exactly() -> None:
    assert allocate_largest_remainder(Decimal("0.10"), [Decimal("1"), Decimal("1"), Decimal("1")]) == [
        Decimal("0.033333333334"),
        Decimal("0.033333333333"),
        Decimal("0.033333333333"),
    ]
    assert sum(allocate_largest_remainder(Decimal("7.77"), [Decimal("1"), Decimal("2")])) == Decimal("7.770000000000")


def test_layer_partial_and_full_consumption_values() -> None:
    layers: list[LayerState] = []
    _, layers = create_layer(layers, qty=10, unit_cost=Decimal("3.333333333333"), layer_date=date(2026, 1, 1))
    new_layers, actions, value, estimated, _ = consume_layers(
        layers, 10, method="FIFO", allow_negative=False, last_purchase_price=None,
        estimated_allowed=False, layer_date=date(2026, 1, 2),
    )
    assert estimated is False
    assert value == Decimal("33.333333333330")
    assert new_layers[0].status == "EXHAUSTED"
    assert new_layers[0].qty_remaining == 0
    assert new_layers[0].value_remaining == 0


def test_layer_revaluation_proportional_and_exact() -> None:
    layers: list[LayerState] = []
    _, layers = create_layer(layers, qty=10, unit_cost=Decimal("1"), layer_date=date(2026, 1, 1))
    updated, actions, applied = revalue_layers(layers, Decimal("0.000000000003"))
    assert applied == Decimal("0.000000000003")
    assert updated[0].value_remaining == Decimal("10.000000000003")
    assert actions[0].action == "REVALUE"


def test_variance_families_are_deterministic() -> None:
    assert purchase_price_variance(billed_unit_cost=Decimal("6.2"), po_unit_cost=Decimal("6"), qty=Decimal("10")) == Decimal("2.000000000000")
    assert bill_quantity_variance(billed_qty=Decimal("10"), received_qty=Decimal("9"), unit_cost=Decimal("2")) == Decimal("2.000000000000")
    assert exchange_rate_variance(amount_txn=Decimal("100"), billed_rate=Decimal("1.1"), receipt_rate=Decimal("1.0")) == Decimal("10.000000000000")
    assert variance_postings("PURCHASE_PRICE", Decimal("2"), item_id="i", account_ref="V")["direction"] == "DEBIT"


def test_underwater_true_up_and_return_policy() -> None:
    assert underwater_true_up(qty=Decimal("5"), true_unit_cost=Decimal("7"), estimated_unit_cost=Decimal("5")) == Decimal("10.000000000000")
    assert return_unit_cost(policy="ORIGINAL_WHEN_TRACEABLE", original_unit_cost=Decimal("4"), current_method_cost=Decimal("6"), traceable=True) == (Decimal("4.000000000000"), False)
    assert return_unit_cost(policy="CURRENT", original_unit_cost=Decimal("4"), current_method_cost=Decimal("6"), traceable=True) == (Decimal("6.000000000000"), False)


def test_rollup_aggregates_per_category_deterministically() -> None:
    inputs = [
        RollupInput("i1", "MATERIAL", 1, Decimal("5")),
        RollupInput("i1", "LABOR", 0, Decimal("2")),
        RollupInput("i1", "MATERIAL", 0, Decimal("3")),
    ]
    outputs = compute_rollup(inputs)
    assert len(outputs) == 2
    material = next(row for row in outputs if row.cost_category_code == "MATERIAL")
    assert material.new_cost == Decimal("8.000000000000")
    assert compute_rollup(inputs) == outputs


def test_landed_cost_estimated_allocation() -> None:
    lines = [ReceiptLineBasis("r1", "i1", qty=Decimal("1"), weight=Decimal("1"), amount=Decimal("20"))]
    estimates = compute_estimated_landed_cost(
        receipt_total=Decimal("100"),
        template_lines=[{"cost_category_id": "c1", "allocation_basis": "AMOUNT", "default_percent": "10"}],
        receipt_lines=lines,
    )
    assert estimates[0]["allocated_amount"] == "10.000000000000"
    assert estimates[0]["estimated"] is True


def test_group_average_and_worksheet_guard() -> None:
    qty, value, avg = recompute_group_average([])
    assert (qty, value, avg) == (Decimal("0.000000000000"),) * 3
    assert group_fallback_method(costing_method="GROUP_AVERAGE", location_in_group=False) == "AVERAGE"
    with pytest.raises(CostingPolicyViolation):
        assert_group_worksheet_allowed(costing_method="GROUP_AVERAGE")


def test_cogs_adjustment_lines_skip_zero_and_require_rows() -> None:
    lines = cogs_adjustment_lines(
        [
            {"item_id": "i", "item_cost_id": "c", "value_delta": "2"},
            {"item_id": "i", "item_cost_id": "c", "value_delta": "0"},
        ]
    )
    assert len(lines) == 1 and lines[0]["value_delta"] == "2.000000000000"
    with pytest.raises(CostingValidationError):
        cogs_adjustment_lines([])


def test_standard_window_overlap_validation() -> None:
    validate_standard_windows(
        [{"item_id": "i", "cost_category_id": "c", "effective_from": date(2026, 1, 1), "effective_to": None}]
    )
    with pytest.raises(CostingConflict):
        validate_standard_windows(
            [
                {"item_id": "i", "cost_category_id": "c", "effective_from": date(2026, 1, 1), "effective_to": date(2026, 3, 1)},
                {"item_id": "i", "cost_category_id": "c", "effective_from": date(2026, 2, 1), "effective_to": None},
            ]
        )


def test_current_cost_helper() -> None:
    assert current_cost(ItemCostState(avg_cost=Decimal("4.2")), "AVERAGE") == Decimal("4.200000000000")
