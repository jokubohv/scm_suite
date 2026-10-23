"""Pure FS-12 rules: spec line limits, disposition derivation, derived fields, trigger frequency, COA rules."""

from __future__ import annotations

import pytest

from app.contracts.quality.contract import (
    CONTRACT_NAME,
    CONTRACT_VERSION,
    QualityContractError,
    coa_line_visible,
    derive_disposition,
    evaluate_trigger_frequency,
    inspection_sampling_eligible,
    validate_result_payload,
)
from app.modules.quality.errors import SpecLineLimit, ValidationError
from app.modules.quality.service import (
    MAX_SPEC_LINES,
    QualityService,
    derive_result_summary,
    validate_result_row,
)


def test_contract_identity() -> None:
    assert CONTRACT_NAME == "quality.management"
    assert CONTRACT_VERSION == "1.0.0"


def test_spec_line_limit_and_scope_validation() -> None:
    service = QualityService.__new__(QualityService)
    with pytest.raises(ValidationError):
        service._validate_spec_lines([])
    with pytest.raises(SpecLineLimit):
        service._validate_spec_lines([{"result_type": "TEXT"} for _ in range(MAX_SPEC_LINES + 1)])
    with pytest.raises(ValidationError):
        service._validate_spec_scope({"item_id": None, "item_category_id": None})
    with pytest.raises(ValidationError):
        service._validate_spec_scope({"item_id": "i", "item_category_id": "c"})
    service._validate_spec_scope({"item_id": "i", "item_category_id": None})
    service._validate_spec_lines([{"result_type": "NUMERIC", "min_value": 1, "max_value": 2}])


def test_disposition_derivation_dm_qa_01() -> None:
    passed = derive_disposition([{"is_pass": True}, {"is_pass": True}])
    assert (passed.status, passed.disposition) == ("PASSED", "ACCEPT")
    failed = derive_disposition([{"is_pass": True}, {"is_pass": False}], requested="HOLD")
    assert (failed.status, failed.disposition, failed.failed_lines) == ("FAILED", "HOLD", 1)
    with pytest.raises(QualityContractError):
        derive_disposition([])
    with pytest.raises(QualityContractError):
        derive_disposition([{"is_pass": False}], requested="ACCEPT")


def test_result_validation_for_numeric_and_pass_fail() -> None:
    numeric = validate_result_row(
        {"sequence": 1, "measured_value_number": "10.5"},
        {
            "id": "line",
            "sequence": 1,
            "characteristic": "length",
            "result_type": "NUMERIC",
            "min_value": 10,
            "max_value": 10.2,
        },
    )
    assert numeric["is_pass"] is False
    ok = validate_result_row(
        {"sequence": 1, "measured_value_number": "10.1"},
        {
            "id": "line",
            "sequence": 1,
            "characteristic": "length",
            "result_type": "NUMERIC",
            "min_value": 10,
            "max_value": 10.2,
        },
    )
    assert ok["is_pass"] is True
    pass_fail = validate_result_row(
        {"sequence": 2, "measured_value_text": "PASS"},
        {"id": "line2", "sequence": 2, "characteristic": "visual", "result_type": "PASS_FAIL"},
    )
    assert pass_fail["is_pass"] is True
    with pytest.raises(ValidationError):
        validate_result_row(
            {"sequence": 3, "measured_value_text": "MAYBE"},
            {"id": "line3", "sequence": 3, "characteristic": "visual", "result_type": "PASS_FAIL"},
        )


def test_derived_result_summary_statistics() -> None:
    rows = [
        {"measured_value_number": 1, "is_pass": True},
        {"measured_value_number": 2, "is_pass": True},
        {"measured_value_number": 3, "is_pass": False},
    ]
    summary = derive_result_summary(
        rows, [{"field": "avg_len", "aggregation": "avg"}, {"field": "spread", "aggregation": "stdev"}]
    )
    assert summary["rows"] == 3 and summary["passed"] == 2 and summary["failed"] == 1
    assert summary["numeric"]["avg"] == 2.0
    derived = {item["field"]: item["value"] for item in summary["derived"]}
    assert derived["avg_len"] == 2.0
    assert round(derived["spread"], 6) == round(0.816496580927726, 6)


def test_trigger_frequency_and_sampling_rules() -> None:
    fires, counter = evaluate_trigger_frequency({"evaluation_counter": 0, "condition": {"frequency": 3}})
    assert fires is False and counter == 1
    assert evaluate_trigger_frequency({"evaluation_counter": 2, "condition": {"frequency": 3}}) == (True, 3)
    assert evaluate_trigger_frequency({"evaluation_counter": 9}) == (True, 10)
    assert inspection_sampling_eligible({"sampling_method": "FULL"}) is True
    assert inspection_sampling_eligible({"sampling_method": "SAMPLE_RANDOM"}) is True
    assert inspection_sampling_eligible({"sampling_method": "SAMPLE_PERCENT"}) is False


def test_coa_visibility_and_wire_validation() -> None:
    assert coa_line_visible({"is_internal": False}) is True
    assert coa_line_visible({"is_internal": True}) is False
    valid = validate_result_payload({"result_type": "NUMERIC", "measured_value_number": "1"})
    assert valid["result_type"] == "NUMERIC"
    with pytest.raises(QualityContractError):
        validate_result_payload({"result_type": "NUMERIC"})
    with pytest.raises(QualityContractError):
        validate_result_payload({"result_type": "NUMERIC", "measured_value_text": "x", "measured_value_number": "1"})
