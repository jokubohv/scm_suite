"""Provider contract facade for `quality.management@1.0.0` (FS-12).

Consumers (FS-15 reporting; hold/task info for FS-11) rely only on this module: pure disposition/result
rules, the inspection-status read shape, the COA visibility rule (internal-only lines excluded) and a
DB-backed facade for read projections + trigger evaluation. Quality never writes the ledger: holds flow
through the FS-04 T-30 status-change command (DM-QA-02).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

__all__ = [
    "CONTRACT_NAME",
    "CONTRACT_VERSION",
    "COA_ELIGIBLE_SAMPLING",
    "InspectionStatusView",
    "QualityDisposition",
    "QualityFacade",
    "coa_line_visible",
    "derive_disposition",
    "evaluate_trigger_frequency",
    "inspection_sampling_eligible",
    "validate_result_payload",
]

CONTRACT_NAME = "quality.management"
CONTRACT_VERSION = "1.0.0"
COA_ELIGIBLE_SAMPLING = frozenset({"FULL", "SAMPLE_RANDOM"})
RESULT_TYPES = frozenset({"NUMERIC", "TEXT", "PASS_FAIL", "CHOICE"})
RESULT_TOLERANCE = Decimal("0.000000000001")


class QualityContractError(ValueError):
    code = "VALIDATION_FAILED"


def validate_result_payload(row: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the wire shape of one result row (consumer-facing, pure)."""
    result_type = str(row.get("result_type") or "NUMERIC").upper()
    if result_type not in RESULT_TYPES:
        raise QualityContractError(f"invalid result_type {result_type!r}")
    text_value = row.get("measured_value_text")
    number_value = row.get("measured_value_number")
    if (text_value is None) == (number_value is None):
        raise QualityContractError("result must provide exactly one measured value")
    if result_type == "NUMERIC" and number_value is None:
        raise QualityContractError("numeric result requires measured_value_number")
    return {"result_type": result_type, "measured_value_text": text_value, "measured_value_number": number_value}


@dataclass(frozen=True, slots=True)
class QualityDisposition:
    status: str
    disposition: str
    failed_lines: int

    def to_dict(self) -> dict[str, Any]:
        return {"status": self.status, "disposition": self.disposition, "failed_lines": self.failed_lines}


def derive_disposition(result_rows: Sequence[Mapping[str, Any]], *, requested: str | None = None) -> QualityDisposition:
    """DM-QA-01: required results decide PASSED/FAILED; FAILED maps to REJECT|HOLD|REWORK."""
    if not result_rows:
        raise QualityContractError("completion requires at least one result")
    failed = sum(1 for row in result_rows if row.get("is_pass") is False)
    if failed:
        disposition = (requested or "REJECT").upper()
        if disposition not in {"REJECT", "HOLD", "REWORK"}:
            raise QualityContractError("failed inspection disposition must be REJECT|HOLD|REWORK")
        return QualityDisposition("FAILED", disposition, failed)
    return QualityDisposition("PASSED", "ACCEPT", 0)


def inspection_sampling_eligible(
    plan: Mapping[str, Any] | None, *, eligible: Sequence[str] = tuple(COA_ELIGIBLE_SAMPLING)
) -> bool:
    method = str((plan or {}).get("sampling_method") or "FULL").upper()
    return method in {str(item).upper() for item in eligible}


def evaluate_trigger_frequency(trigger: Mapping[str, Any]) -> tuple[bool, int]:
    """Every-Nth-transaction frequency rule (QA-RULE-03)."""
    condition = trigger.get("condition") or {}
    frequency = condition.get("frequency") if isinstance(condition, Mapping) else None
    counter = int(trigger.get("evaluation_counter") or 0) + 1
    if frequency is None:
        return True, counter
    try:
        every = max(int(frequency), 1)
    except (TypeError, ValueError):
        every = 1
    return counter % every == 0, counter


def coa_line_visible(spec_line: Mapping[str, Any] | None) -> bool:
    """DM-QA-06: supplier-facing COA data excludes internal-only characteristics."""
    return not bool((spec_line or {}).get("is_internal"))


@dataclass(frozen=True, slots=True)
class InspectionStatusView:
    inspection_id: str
    inspection_number: str
    status: str
    disposition: str | None
    item_id: str
    location_id: str
    lot_id: str | None
    serial_id: str | None
    source_entity_type: str
    source_entity_id: str | None
    completed_at: datetime | str | None

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> InspectionStatusView:
        return cls(
            inspection_id=str(row["id"]),
            inspection_number=str(row.get("inspection_number")),
            status=str(row.get("status")),
            disposition=row.get("disposition"),
            item_id=str(row.get("item_id")),
            location_id=str(row.get("location_id")),
            lot_id=str(row["lot_id"]) if row.get("lot_id") else None,
            serial_id=str(row["serial_id"]) if row.get("serial_id") else None,
            source_entity_type=str(row.get("source_entity_type") or "OTHER"),
            source_entity_id=str(row["source_entity_id"]) if row.get("source_entity_id") else None,
            completed_at=row.get("completed_at"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "inspection_id": self.inspection_id,
            "inspection_number": self.inspection_number,
            "status": self.status,
            "disposition": self.disposition,
            "item_id": self.item_id,
            "location_id": self.location_id,
            "lot_id": self.lot_id,
            "serial_id": self.serial_id,
            "source_entity_type": self.source_entity_type,
            "source_entity_id": self.source_entity_id,
            "completed_at": self.completed_at,
        }


class QualityFacade:
    """DB-backed provider facade (read projections + trigger evaluation) inside the caller transaction."""

    def __init__(self, session: Any) -> None:
        from app.modules.quality.service import QualityService

        self._service = QualityService(session)

    async def get_inspection_status(self, tenant_id: Any, inspection_id: Any) -> InspectionStatusView:
        inspection = await self._service.get_inspection(tenant_id, inspection_id)
        return InspectionStatusView.from_row(inspection)

    async def list_open_inspections(
        self, tenant_id: Any, *, item_id: Any = None, limit: int = 100
    ) -> list[InspectionStatusView]:
        rows = await self._service.list_inspections(tenant_id, status="PENDING", item_id=item_id, limit=limit)
        return [InspectionStatusView.from_row(row) for row in rows]

    async def evaluate_trigger(self, *, tenant_id: Any, trigger_id: Any, event: Mapping[str, Any]) -> dict[str, Any]:
        return await self._service.evaluate_trigger(tenant_id=tenant_id, trigger_id=trigger_id, event=event)

    async def get_settings(self, tenant_id: Any) -> dict[str, Any]:
        return await self._service.get_settings(tenant_id)
