"""FS-05 unit tests: document state machines + validation rules."""

from __future__ import annotations

import pytest

from app.modules.costing.engine import (
    compute_revaluation_lines,
    validate_version_activation,
    version_transition,
)
from app.modules.costing.errors import CostingConflict, CostingError, CostingValidationError


def test_version_transitions() -> None:
    version_transition("PLANNED", "ACTIVE")
    version_transition("ACTIVE", "SUPERSEDED")
    with pytest.raises(CostingError):
        version_transition("SUPERSEDED", "ACTIVE")
    with pytest.raises(CostingError):
        version_transition("PLANNED", "SUPERSEDED")


def test_activation_requires_approved_planned_standards() -> None:
    with pytest.raises(CostingValidationError):
        validate_version_activation([])
    with pytest.raises(CostingConflict):
        validate_version_activation([{"is_approved": False}])
    validate_version_activation([{"is_approved": True}])
    with pytest.raises(CostingConflict):
        validate_version_activation([{"is_approved": True}], active=[{"status": "ACTIVE"}])


def test_revaluation_lines_deterministic_and_non_zero() -> None:
    lines = compute_revaluation_lines(
        [{"item_id": "i", "old_cost": "5", "new_cost": "6", "qty": "10", "location_id": "l"}]
    )
    assert lines[0]["value_delta"] == "10.000000000000"
    with pytest.raises(CostingValidationError):
        compute_revaluation_lines([])
    with pytest.raises(CostingValidationError):
        compute_revaluation_lines([{"item_id": "i", "old_cost": "5", "new_cost": "5", "qty": "10"}])
