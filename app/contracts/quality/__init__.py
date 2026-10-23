"""FS-12 provider contract: `quality.management@1.0.0` facade and contract metadata."""

from app.contracts.quality.contract import (
    CONTRACT_NAME,
    CONTRACT_VERSION,
    InspectionStatusView,
    QualityDisposition,
    QualityFacade,
    coa_line_visible,
    derive_disposition,
    evaluate_trigger_frequency,
    inspection_sampling_eligible,
    validate_result_payload,
)

__all__ = [
    "CONTRACT_NAME",
    "CONTRACT_VERSION",
    "InspectionStatusView",
    "QualityDisposition",
    "QualityFacade",
    "coa_line_visible",
    "derive_disposition",
    "evaluate_trigger_frequency",
    "inspection_sampling_eligible",
    "validate_result_payload",
]
