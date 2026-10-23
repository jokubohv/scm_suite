"""FS-12 quality-management use-case service (specs, plans, triggers, inspections, holds, COA).

Backend semantics only (D4 EXT). Quality never writes the ledger: holds/releases are applied through the
FS-04 ``inventory.ledger`` T-30 status-change posting port and linked via ``platform.document_link``
(DM-QA-02). All lifecycle changes are audited and publish outbox events.
"""

from __future__ import annotations

import json
import statistics
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.quality import tables as q
from app.modules.quality.errors import (
    CoaNotFound,
    CoaSamplingIneligible,
    DomainDisabled,
    HoldReleaseDenied,
    HoldTargetNotFound,
    InspectionNotFound,
    InvalidStateTransition,
    PlanNotFound,
    QualityHoldStatusInvalid,
    SpecLineLimit,
    SpecNotFound,
    SpecVersionImmutable,
    TriggerNotFound,
    TriggerTargetRequired,
    ValidationError,
)
from app.modules.quality.repository import QualityRepository, utc_now

__all__ = [
    "CONTRACT_VERSION",
    "DOMAIN_FLAG",
    "MAX_SPEC_LINES",
    "QualityService",
    "derive_disposition",
    "derive_result_summary",
    "evaluate_trigger_frequency",
    "inspection_sampling_eligible",
    "validate_result_row",
]

CONTRACT_VERSION = "1.0.0"
DOMAIN_FLAG = "quality.management.enabled"
MAX_SPEC_LINES = 35
COA_ELIGIBLE_SAMPLING = frozenset({"FULL", "SAMPLE_RANDOM"})
RESULT_TOLERANCE = Decimal("0.000000000001")


def _dec(value: Any) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError) as exc:
        raise ValidationError(f"invalid numeric value {value!r}") from exc


def validate_result_row(row: Mapping[str, Any], spec_line: Mapping[str, Any]) -> dict[str, Any]:
    """Validate/derive one inspection result row against its spec characteristic line."""
    result_type = str(spec_line.get("result_type") or "NUMERIC")
    sequence = int(row.get("sequence") or spec_line.get("sequence") or 0)
    characteristic = str(spec_line.get("characteristic") or row.get("characteristic") or "")
    measured_text = row.get("measured_value_text")
    measured_number = row.get("measured_value_number")
    if measured_text is not None and measured_number is not None:
        raise ValidationError("result must provide exactly one measured value", details={"sequence": sequence})
    is_pass = bool(row.get("is_pass", True))
    if result_type == "NUMERIC":
        if measured_number is None:
            raise ValidationError("numeric result requires measured_value_number", details={"sequence": sequence})
        value = _dec(measured_number)
        minimum = spec_line.get("min_value")
        maximum = spec_line.get("max_value")
        is_pass = True
        if minimum is not None and value < _dec(minimum) - RESULT_TOLERANCE:
            is_pass = False
        if maximum is not None and value > _dec(maximum) + RESULT_TOLERANCE:
            is_pass = False
        measured_text = str(value)
    elif result_type == "PASS_FAIL":
        if measured_text not in {"PASS", "FAIL", "pass", "fail"}:
            raise ValidationError(
                "pass/fail result requires measured_value_text PASS|FAIL", details={"sequence": sequence}
            )
        is_pass = str(measured_text).upper() == "PASS"
        measured_text = str(measured_text).upper()
    elif result_type == "CHOICE":
        choices = spec_line.get("choices") or []
        if measured_text is None or (choices and measured_text not in choices):
            raise ValidationError("choice result must be one of the configured choices", details={"sequence": sequence})
        is_pass = bool(row.get("is_pass", True))
    else:  # TEXT
        if measured_text is None:
            raise ValidationError("text result requires measured_value_text", details={"sequence": sequence})
    return {
        "sequence": sequence,
        "characteristic": characteristic,
        "quality_spec_line_id": spec_line.get("id"),
        "measured_value_text": measured_text,
        "measured_value_number": _dec(measured_number) if measured_number is not None else None,
        "is_pass": is_pass,
        "notes": row.get("notes"),
        "attachment_id": row.get("attachment_id"),
    }


def derive_disposition(result_rows: Sequence[Mapping[str, Any]], *, requested: str | None = None) -> tuple[str, str]:
    """Return ``(status, disposition)`` from required result rows (DM-QA-01)."""
    if not result_rows:
        raise ValidationError("completion requires at least one result")
    failed = [row for row in result_rows if row.get("is_pass") is False]
    if failed:
        disposition = (requested or "REJECT").upper()
        if disposition not in {"REJECT", "HOLD", "REWORK"}:
            raise ValidationError(
                "failed inspection disposition must be REJECT|HOLD|REWORK", details={"disposition": disposition}
            )
        return "FAILED", disposition
    if requested and str(requested).upper() == "HOLD":
        return "PASSED", "HOLD"
    return "PASSED", "ACCEPT"


def derive_result_summary(
    result_rows: Sequence[Mapping[str, Any]], derived_config: Sequence[Mapping[str, Any]] | None = None
) -> dict[str, Any]:
    """Statistical summary over result rows; optional derived-field config (iri-383)."""
    numeric = [
        float(row["measured_value_number"]) for row in result_rows if row.get("measured_value_number") is not None
    ]
    summary: dict[str, Any] = {
        "rows": len(result_rows),
        "passed": sum(1 for row in result_rows if row.get("is_pass") is True),
        "failed": sum(1 for row in result_rows if row.get("is_pass") is False),
        "numeric": {
            "count": len(numeric),
            "min": min(numeric) if numeric else None,
            "max": max(numeric) if numeric else None,
            "avg": (sum(numeric) / len(numeric)) if numeric else None,
            "median": statistics.median(numeric) if numeric else None,
            "stdev": statistics.pstdev(numeric) if len(numeric) > 1 else 0.0,
        },
        "derived": [],
    }
    for config in derived_config or []:
        if not isinstance(config, Mapping):
            continue
        field = str(config.get("field") or "unknown")
        aggregation = str(config.get("aggregation") or "count").lower()
        values: list[float] = []
        for row in result_rows:
            raw_value = row.get(config.get("source") or "measured_value_number")
            if raw_value is not None:
                values.append(float(raw_value))
        if aggregation == "count":
            value: Any = len(values)
        elif aggregation == "avg":
            value = sum(values) / len(values) if values else None
        elif aggregation == "min":
            value = min(values) if values else None
        elif aggregation == "max":
            value = max(values) if values else None
        elif aggregation == "stdev":
            value = statistics.pstdev(values) if len(values) > 1 else 0.0
        else:
            value = None
        summary["derived"].append({"field": field, "aggregation": aggregation, "value": value})
    return summary


def evaluate_trigger_frequency(trigger: Mapping[str, Any]) -> tuple[bool, int]:
    """Return (fires?, next_counter) honouring the documented every-Nth transaction rule (QA-RULE-03)."""
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


def inspection_sampling_eligible(
    plan: Mapping[str, Any] | None, *, eligible: Sequence[str] = tuple(COA_ELIGIBLE_SAMPLING)
) -> bool:
    method = str((plan or {}).get("sampling_method") or "FULL").upper()
    return method in {str(item).upper() for item in eligible}


class QualityService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.repository = QualityRepository(session)

    # ------------------------------------------------------------------ gates ---------------
    async def require_enabled(self, tenant_id: UUID | str) -> None:
        if not await self.repository.domain_enabled(tenant_id):
            raise DomainDisabled("quality.management.enabled is off for this tenant", details={"flag": DOMAIN_FLAG})

    async def _setting(self, tenant_id: UUID | str, key: str, default: Any) -> Any:
        value = await self.repository.get_setting(tenant_id, key)
        if value is None:
            return default
        if isinstance(value, str):
            try:
                return json.loads(value)
            except json.JSONDecodeError:
                return value
        return value

    # ------------------------------------------------------------------ specs ---------------
    async def create_spec(
        self, *, tenant_id: UUID | str, values: Mapping[str, Any], actor_id: UUID | str | None = None
    ) -> dict[str, Any]:
        await self.require_enabled(tenant_id)
        self._validate_spec_scope(values)
        lines = list(values.get("lines") or [])
        self._validate_spec_lines(lines)
        versions = [int(line.get("spec_version", 0)) for line in lines]
        del versions
        code = str(values.get("code") or "").strip()
        if not code:
            raise ValidationError("spec code is required", details={"field": "code"})
        existing = await self.repository.list_specs(tenant_id, limit=1)
        del existing
        next_version = await self.repository.spec_max_version(tenant_id, code) + 1
        spec = await self.repository.insert_spec(
            {
                "id": uuid4(),
                "tenant_id": tenant_id,
                "code": code,
                "name": str(values.get("name") or code),
                "spec_version": next_version,
                "item_id": values.get("item_id"),
                "item_category_id": values.get("item_category_id"),
                "status": "DRAFT",
                "effective_from": values.get("effective_from"),
                "effective_to": values.get("effective_to"),
                "notes": values.get("notes"),
            }
        )
        line_rows = [
            {
                "id": uuid4(),
                "tenant_id": tenant_id,
                "quality_spec_id": spec["id"],
                "sequence": index,
                "characteristic": str(line.get("characteristic") or f"characteristic-{index}"),
                "test_method": line.get("test_method"),
                "result_type": str(line.get("result_type") or "NUMERIC"),
                "target_value_text": line.get("target_value_text"),
                "min_value": line.get("min_value"),
                "max_value": line.get("max_value"),
                "uom_id": line.get("uom_id"),
                "choices": line.get("choices"),
                "is_required": bool(line.get("is_required", True)),
                "is_internal": bool(line.get("is_internal", False)),
            }
            for index, line in enumerate(lines, start=1)
        ]
        await self.repository.insert_spec_lines(line_rows)
        await self.repository.audit(
            tenant_id=tenant_id,
            actor_id=actor_id,
            action="quality.spec.created",
            resource_type="quality.quality_spec",
            resource_id=spec["id"],
            diff={"code": code, "version": next_version},
        )
        await self.repository.emit_event(
            event_type="quality.spec.created",
            tenant_id=tenant_id,
            aggregate_id=spec["id"],
            payload={"spec_id": spec["id"], "code": code, "version": next_version},
        )
        return await self.get_spec(tenant_id, spec["id"])

    def _validate_spec_scope(self, values: Mapping[str, Any]) -> None:
        if (values.get("item_id") is None) == (values.get("item_category_id") is None):
            raise ValidationError("quality spec must target exactly one of item_id or item_category_id")

    def _validate_spec_lines(self, lines: Sequence[Mapping[str, Any]]) -> None:
        if not lines:
            raise ValidationError("quality spec requires at least one characteristic line")
        if len(lines) > MAX_SPEC_LINES:
            raise SpecLineLimit(f"a quality spec may not exceed {MAX_SPEC_LINES} lines", details={"lines": len(lines)})
        for index, line in enumerate(lines):
            result_type = str(line.get("result_type") or "NUMERIC")
            if result_type not in q.RESULT_TYPES:
                raise ValidationError(f"invalid result_type {result_type!r}", details={"line": index + 1})
            minimum, maximum = line.get("min_value"), line.get("max_value")
            if minimum is not None and maximum is not None and _dec(minimum) > _dec(maximum):
                raise ValidationError("min_value must not exceed max_value", details={"line": index + 1})
            if result_type == "CHOICE" and not line.get("choices"):
                raise ValidationError("choice result requires choices", details={"line": index + 1})

    async def get_spec(self, tenant_id: UUID | str, spec_id: UUID | str) -> dict[str, Any]:
        spec = await self.repository.get_spec(tenant_id, spec_id)
        if spec is None:
            raise SpecNotFound("quality spec not found", details={"spec_id": str(spec_id)})
        spec["lines"] = await self.repository.spec_lines(tenant_id, spec_id)
        return spec

    async def list_specs(self, tenant_id: UUID | str, **filters: Any) -> list[dict[str, Any]]:
        return await self.repository.list_specs(tenant_id, **filters)

    async def new_spec_version(
        self,
        *,
        tenant_id: UUID | str,
        spec_id: UUID | str,
        values: Mapping[str, Any],
        actor_id: UUID | str | None = None,
    ) -> dict[str, Any]:
        await self.require_enabled(tenant_id)
        source = await self.get_spec(tenant_id, spec_id)
        if source["status"] != "ACTIVE":
            raise SpecVersionImmutable("only ACTIVE specs can be versioned")
        lines = list(values.get("lines") or source["lines"])
        self._validate_spec_lines(lines)
        next_version = await self.repository.spec_max_version(tenant_id, source["code"]) + 1
        spec = await self.repository.insert_spec(
            {
                "id": uuid4(),
                "tenant_id": tenant_id,
                "code": source["code"],
                "name": source["name"],
                "spec_version": next_version,
                "item_id": source["item_id"],
                "item_category_id": source["item_category_id"],
                "status": "DRAFT",
                "effective_from": values.get("effective_from"),
                "effective_to": None,
                "notes": values.get("notes"),
            }
        )
        await self.repository.insert_spec_lines(
            [
                {
                    "id": uuid4(),
                    "tenant_id": tenant_id,
                    "quality_spec_id": spec["id"],
                    "sequence": index,
                    "characteristic": str(line.get("characteristic")),
                    "test_method": line.get("test_method"),
                    "result_type": str(line.get("result_type") or "NUMERIC"),
                    "target_value_text": line.get("target_value_text"),
                    "min_value": line.get("min_value"),
                    "max_value": line.get("max_value"),
                    "uom_id": line.get("uom_id"),
                    "choices": line.get("choices"),
                    "is_required": bool(line.get("is_required", True)),
                    "is_internal": bool(line.get("is_internal", False)),
                }
                for index, line in enumerate(lines, start=1)
            ]
        )
        await self.repository.audit(
            tenant_id=tenant_id,
            actor_id=actor_id,
            action="quality.spec.versioned",
            resource_type="quality.quality_spec",
            resource_id=spec["id"],
            diff={"from": source["id"]},
        )
        await self.repository.emit_event(
            event_type="quality.spec.versioned",
            tenant_id=tenant_id,
            aggregate_id=spec["id"],
            payload={"spec_id": spec["id"], "code": source["code"], "version": next_version},
        )
        return await self.get_spec(tenant_id, spec["id"])

    async def activate_spec(
        self, *, tenant_id: UUID | str, spec_id: UUID | str, actor_id: UUID | str | None = None
    ) -> dict[str, Any]:
        await self.require_enabled(tenant_id)
        spec = await self.get_spec(tenant_id, spec_id)
        if spec["status"] != "DRAFT":
            raise InvalidStateTransition("only DRAFT specs can be activated")
        updated = await self.repository.update_spec(tenant_id, spec_id, {"status": "ACTIVE"})
        assert updated is not None
        await self.repository.audit(
            tenant_id=tenant_id,
            actor_id=actor_id,
            action="quality.spec.activated",
            resource_type="quality.quality_spec",
            resource_id=spec_id,
        )
        return await self.get_spec(tenant_id, spec_id)

    async def retire_spec(
        self,
        *,
        tenant_id: UUID | str,
        spec_id: UUID | str,
        reason: str | None = None,
        effective_to: date | None = None,
        actor_id: UUID | str | None = None,
    ) -> dict[str, Any]:
        await self.require_enabled(tenant_id)
        spec = await self.get_spec(tenant_id, spec_id)
        if spec["status"] != "ACTIVE":
            raise InvalidStateTransition("only ACTIVE specs can be retired")
        if await self.repository.spec_referenced_by_active_plan(tenant_id, spec_id):
            raise ValidationError("spec is referenced by an active inspection plan")
        updated = await self.repository.update_spec(
            tenant_id,
            spec_id,
            {"status": "RETIRED", "effective_to": effective_to, "notes": reason or spec.get("notes")},
        )
        assert updated is not None
        await self.repository.emit_event(
            event_type="quality.spec.retired",
            tenant_id=tenant_id,
            aggregate_id=spec_id,
            payload={"spec_id": str(spec_id), "reason": reason},
        )
        return await self.get_spec(tenant_id, spec_id)

    # ------------------------------------------------------------------ plans ---------------
    async def create_plan(
        self, *, tenant_id: UUID | str, values: Mapping[str, Any], actor_id: UUID | str | None = None
    ) -> dict[str, Any]:
        await self.require_enabled(tenant_id)
        quality_spec_id = values.get("quality_spec_id")
        if quality_spec_id is None:
            raise ValidationError("inspection plan requires quality_spec_id")
        spec = await self.get_spec(tenant_id, quality_spec_id)
        if spec["status"] != "ACTIVE":
            raise ValidationError("inspection plan requires an ACTIVE quality spec")
        if (values.get("item_id") is None) == (values.get("item_category_id") is None):
            raise ValidationError("inspection plan must target exactly one of item_id or item_category_id")
        sampling = str(values.get("sampling_method") or "FULL").upper()
        if sampling not in q.SAMPLING_METHODS:
            raise ValidationError(f"invalid sampling method {sampling!r}")
        sample_size = int(values.get("sample_size") or 1)
        if sample_size <= 0:
            raise ValidationError("sample_size must be positive")
        plan = await self.repository.insert_plan(
            {
                "id": uuid4(),
                "tenant_id": tenant_id,
                "code": str(values.get("code") or ""),
                "name": str(values.get("name") or values.get("code") or ""),
                "item_id": values.get("item_id"),
                "item_category_id": values.get("item_category_id"),
                "quality_spec_id": spec["id"],
                "inspection_type": str(values.get("inspection_type") or "INCOMING").upper(),
                "sampling_method": sampling,
                "sample_size": sample_size,
                "trigger_condition": values.get("trigger_condition"),
                "is_active": bool(values.get("is_active", True)),
            }
        )
        spec_line_ids = {line["id"] for line in spec["lines"]}
        requested = values.get("lines") or [
            {"quality_spec_line_id": line["id"], "sequence": line["sequence"]} for line in spec["lines"]
        ]
        rows: list[dict[str, Any]] = []
        for item in requested:
            spec_line_id = str(item.get("quality_spec_line_id"))
            if spec_line_id not in spec_line_ids:
                raise ValidationError("plan line must reference a line of the plan's quality spec")
            rows.append(
                {
                    "id": uuid4(),
                    "tenant_id": tenant_id,
                    "inspection_plan_id": plan["id"],
                    "quality_spec_line_id": spec_line_id,
                    "sequence": int(item.get("sequence") or len(rows) + 1),
                    "is_required": bool(item.get("is_required", True)),
                    "sample_size_override": item.get("sample_size_override"),
                }
            )
        await self.repository.insert_plan_lines(rows)
        await self.repository.emit_event(
            event_type="quality.plan.created",
            tenant_id=tenant_id,
            aggregate_id=plan["id"],
            payload={"plan_id": plan["id"], "inspection_type": plan["inspection_type"]},
        )
        return await self.get_plan(tenant_id, plan["id"], plan["item_id"])

    async def get_plan(self, tenant_id: UUID | str, plan_id: UUID | str, item_id: Any = None) -> dict[str, Any]:
        del item_id
        plan = await self.repository.get_plan(tenant_id, plan_id)
        if plan is None:
            raise PlanNotFound("inspection plan not found", details={"plan_id": str(plan_id)})
        plan["lines"] = await self.repository.plan_lines(tenant_id, plan_id)
        return plan

    async def list_plans(self, tenant_id: UUID | str, **filters: Any) -> list[dict[str, Any]]:
        return await self.repository.list_plans(tenant_id, **filters)

    async def update_plan(
        self,
        *,
        tenant_id: UUID | str,
        plan_id: UUID | str,
        values: Mapping[str, Any],
        actor_id: UUID | str | None = None,
    ) -> dict[str, Any]:
        await self.require_enabled(tenant_id)
        plan = await self.get_plan(tenant_id, plan_id)
        allowed = {"name", "sampling_method", "sample_size", "trigger_condition", "is_active"}
        patch = {key: value for key, value in values.items() if key in allowed}
        if "sampling_method" in patch and str(patch["sampling_method"]).upper() not in q.SAMPLING_METHODS:
            raise ValidationError("invalid sampling method")
        if "sample_size" in patch and int(patch["sample_size"]) <= 0:
            raise ValidationError("sample_size must be positive")
        updated = await self.repository.update_plan(tenant_id, plan["id"], patch)
        assert updated is not None
        await self.repository.emit_event(
            event_type="quality.plan.changed",
            tenant_id=tenant_id,
            aggregate_id=plan_id,
            payload={"plan_id": str(plan_id)},
        )
        return await self.get_plan(tenant_id, plan_id)

    # ------------------------------------------------------------------ queues --------------
    async def create_queue(
        self, *, tenant_id: UUID | str, values: Mapping[str, Any], actor_id: UUID | str | None = None
    ) -> dict[str, Any]:
        await self.require_enabled(tenant_id)
        strategy = str(values.get("assignment_strategy") or "MANUAL").upper()
        if strategy not in q.ASSIGNMENT_STRATEGIES:
            raise ValidationError(f"invalid assignment strategy {strategy!r}")
        queue = await self.repository.insert_queue(
            {
                "id": uuid4(),
                "tenant_id": tenant_id,
                "code": str(values.get("code") or ""),
                "name": str(values.get("name") or values.get("code") or ""),
                "location_id": values.get("location_id"),
                "trigger_id": values.get("trigger_id"),
                "assignment_strategy": strategy,
                "is_active": bool(values.get("is_active", True)),
            }
        )
        await self.repository.emit_event(
            event_type="quality.queue.changed",
            tenant_id=tenant_id,
            aggregate_id=queue["id"],
            payload={"queue_id": queue["id"], "strategy": strategy},
        )
        return queue

    async def list_queues(self, tenant_id: UUID | str, **filters: Any) -> list[dict[str, Any]]:
        return await self.repository.list_queues(tenant_id, **filters)

    async def queue_items(self, tenant_id: UUID | str, queue_id: UUID | str, **filters: Any) -> list[dict[str, Any]]:
        return await self.repository.list_inspections(tenant_id, queue_id=queue_id, **filters)

    # ------------------------------------------------------------------ triggers ------------
    async def create_trigger(
        self, *, tenant_id: UUID | str, values: Mapping[str, Any], actor_id: UUID | str | None = None
    ) -> dict[str, Any]:
        await self.require_enabled(tenant_id)
        if not values.get("inspection_plan_id") and not values.get("inspection_queue_id"):
            raise TriggerTargetRequired("inspection trigger requires a plan and/or queue target")
        trigger_event = str(values.get("trigger_event") or "").upper()
        if trigger_event not in q.TRIGGER_EVENTS:
            raise ValidationError(f"invalid trigger_event {trigger_event!r}")
        trigger = await self.repository.insert_trigger(
            {
                "id": uuid4(),
                "tenant_id": tenant_id,
                "code": str(values.get("code") or ""),
                "name": str(values.get("name") or values.get("code") or ""),
                "trigger_event": trigger_event,
                "condition": values.get("condition"),
                "inspection_plan_id": values.get("inspection_plan_id"),
                "inspection_queue_id": values.get("inspection_queue_id"),
                "priority": int(values.get("priority") or 100),
                "is_active": bool(values.get("is_active", True)),
            }
        )
        await self.repository.emit_event(
            event_type="quality.trigger.changed",
            tenant_id=tenant_id,
            aggregate_id=trigger["id"],
            payload={"trigger_id": trigger["id"], "event": trigger_event},
        )
        return trigger

    async def list_triggers(self, tenant_id: UUID | str, **filters: Any) -> list[dict[str, Any]]:
        return await self.repository.list_triggers(tenant_id, **filters)

    async def update_trigger(
        self,
        *,
        tenant_id: UUID | str,
        trigger_id: UUID | str,
        values: Mapping[str, Any],
        actor_id: UUID | str | None = None,
    ) -> dict[str, Any]:
        await self.require_enabled(tenant_id)
        trigger = await self.repository.get_trigger(tenant_id, trigger_id)
        if trigger is None:
            raise TriggerNotFound("inspection trigger not found", details={"trigger_id": str(trigger_id)})
        allowed = {"condition", "inspection_plan_id", "inspection_queue_id", "priority", "is_active"}
        patch = {key: value for key, value in values.items() if key in allowed}
        updated = await self.repository.update_trigger(tenant_id, trigger_id, patch)
        assert updated is not None
        await self.repository.emit_event(
            event_type="quality.trigger.changed",
            tenant_id=tenant_id,
            aggregate_id=trigger_id,
            payload={"trigger_id": str(trigger_id)},
        )
        return updated

    async def reset_trigger_counter(
        self, *, tenant_id: UUID | str, trigger_id: UUID, actor_id: UUID | str | None = None
    ) -> dict[str, Any]:
        await self.require_enabled(tenant_id)
        count = await self.repository.reset_trigger_counters(tenant_id, trigger_id)
        trigger = await self.repository.get_trigger(tenant_id, trigger_id)
        if trigger is None:
            raise TriggerNotFound("inspection trigger not found")
        await self.repository.audit(
            tenant_id=tenant_id,
            actor_id=actor_id,
            action="quality.trigger.counter_reset",
            resource_type="quality.inspection_trigger",
            resource_id=trigger_id,
            diff={"reset": count},
        )
        await self.repository.emit_event(
            event_type="quality.trigger.counter_reset",
            tenant_id=tenant_id,
            aggregate_id=trigger_id,
            payload={"trigger_id": str(trigger_id), "reset_at": utc_now().isoformat()},
        )
        return {"trigger_id": str(trigger_id), "evaluation_counter": 0}

    # ------------------------------------------------------------------ inspections ---------
    async def create_inspection_on_demand(
        self, *, tenant_id: UUID | str, values: Mapping[str, Any], actor_id: UUID | str | None = None
    ) -> dict[str, Any]:
        await self.require_enabled(tenant_id)
        plan = None
        if values.get("inspection_plan_id"):
            plan = await self.get_plan(tenant_id, values["inspection_plan_id"])
        queue_id = values.get("inspection_queue_id")
        if queue_id is None and plan is None:
            default_code = str(await self._setting(tenant_id, "default_queue_code", "DEFAULT"))
            queue = await self.repository.get_queue_by_code(tenant_id, default_code)
            if queue is not None:
                queue_id = queue["id"]
        return await self._insert_inspection(
            tenant_id=tenant_id,
            values=values,
            plan=plan,
            queue_id=queue_id,
            trigger_id=None,
            actor_id=actor_id,
        )

    async def _insert_inspection(
        self,
        *,
        tenant_id: UUID | str,
        values: Mapping[str, Any],
        plan: Mapping[str, Any] | None,
        queue_id: Any,
        trigger_id: Any,
        actor_id: UUID | str | None,
    ) -> dict[str, Any]:
        item_id = values.get("item_id")
        location_id = values.get("location_id")
        if item_id is None or location_id is None:
            raise ValidationError("inspection requires item_id and location_id")
        number = await self.repository.next_number(tenant_id, "INSP")
        spec_version = None
        if plan is not None:
            spec = await self.repository.get_spec(tenant_id, plan["quality_spec_id"])
            spec_version = int(spec["spec_version"]) if spec else None
        try:
            inspection = await self.repository.insert_inspection(
                {
                    "id": uuid4(),
                    "tenant_id": tenant_id,
                    "inspection_number": number,
                    "inspection_plan_id": plan["id"] if plan else None,
                    "inspection_queue_id": queue_id,
                    "trigger_id": trigger_id,
                    "source_entity_type": str(values.get("source_entity_type") or "OTHER").upper(),
                    "source_entity_id": values.get("source_entity_id"),
                    "item_id": item_id,
                    "location_id": location_id,
                    "lot_id": values.get("lot_id"),
                    "serial_id": values.get("serial_id"),
                    "status": "PENDING",
                    "spec_version": spec_version,
                }
            )
        except IntegrityError as exc:
            raw_source_id = values.get("source_entity_id")
            existing = (
                await self.repository.inspection_for_trigger_source(
                    tenant_id,
                    trigger_id=trigger_id,
                    source_entity_type=str(values.get("source_entity_type") or "OTHER").upper(),
                    source_entity_id=str(raw_source_id),
                )
                if trigger_id and raw_source_id
                else None
            )
            if existing is not None:
                return existing
            raise ValidationError("duplicate inspection for trigger/source", details={"error": str(exc)}) from exc
        await self.repository.audit(
            tenant_id=tenant_id,
            actor_id=actor_id,
            action="quality.inspection.created",
            resource_type="quality.inspection",
            resource_id=inspection["id"],
            diff={"number": number},
        )
        await self.repository.emit_event(
            event_type="quality.inspection.created",
            tenant_id=tenant_id,
            aggregate_id=inspection["id"],
            payload={
                "inspection_id": inspection["id"],
                "item_id": str(item_id),
                "source_entity_type": inspection["source_entity_type"],
                "source_entity_id": str(inspection["source_entity_id"]) if inspection.get("source_entity_id") else None,
            },
        )
        return inspection

    async def evaluate_trigger(
        self,
        *,
        tenant_id: UUID | str,
        trigger_id: UUID | str,
        event: Mapping[str, Any],
        actor_id: UUID | str | None = None,
    ) -> dict[str, Any]:
        """Idempotent trigger evaluation (DM-QA-05): one inspection per (trigger, source) forever."""
        await self.require_enabled(tenant_id)
        trigger = await self.repository.get_trigger(tenant_id, trigger_id)
        if trigger is None:
            raise TriggerNotFound("inspection trigger not found")
        source_type = str(event.get("source_entity_type") or "OTHER").upper()
        source_id = event.get("source_entity_id")
        if source_id is None:
            raise ValidationError("trigger evaluation requires source_entity_id")
        source_id = str(source_id)
        existing = await self.repository.inspection_for_trigger_source(
            tenant_id, trigger_id=trigger_id, source_entity_type=source_type, source_entity_id=source_id
        )
        if existing is not None:
            return {"created": False, "inspection": existing, "deduplicated": True}
        fires, next_counter = evaluate_trigger_frequency(trigger)
        await self.repository.bump_trigger_counter(tenant_id, trigger_id)
        if not fires:
            return {"created": False, "inspection": None, "deferred": True, "evaluation_counter": next_counter}
        plan = (
            await self.get_plan(tenant_id, trigger["inspection_plan_id"]) if trigger.get("inspection_plan_id") else None
        )
        inspection = await self._insert_inspection(
            tenant_id=tenant_id,
            values={
                "item_id": event.get("item_id"),
                "location_id": event.get("location_id"),
                "lot_id": event.get("lot_id"),
                "serial_id": event.get("serial_id"),
                "source_entity_type": source_type,
                "source_entity_id": source_id,
            },
            plan=plan,
            queue_id=trigger.get("inspection_queue_id"),
            trigger_id=trigger_id,
            actor_id=actor_id,
        )
        return {"created": True, "inspection": inspection}

    async def get_inspection(self, tenant_id: UUID | str, inspection_id: UUID | str) -> dict[str, Any]:
        inspection = await self.repository.get_inspection(tenant_id, inspection_id)
        if inspection is None:
            raise InspectionNotFound("inspection not found", details={"inspection_id": str(inspection_id)})
        inspection["results"] = await self.repository.list_results(tenant_id, inspection_id)
        inspection["hold_links"] = [
            link
            for link in await self.repository.links(tenant_id, from_type="quality.inspection", from_id=inspection_id)
            if str(link.get("link_type", "")).startswith("QUALITY_HOLD")
        ]
        return inspection

    async def list_inspections(self, tenant_id: UUID | str, **filters: Any) -> list[dict[str, Any]]:
        return await self.repository.list_inspections(tenant_id, **filters)

    async def start_inspection(
        self,
        *,
        tenant_id: UUID | str,
        inspection_id: UUID | str,
        inspector_id: UUID | str | None,
        actor_id: UUID | str | None = None,
    ) -> dict[str, Any]:
        await self.require_enabled(tenant_id)
        inspection = await self.repository.get_inspection(tenant_id, inspection_id)
        if inspection is None:
            raise InspectionNotFound("inspection not found")
        if inspection["status"] != "PENDING":
            raise InvalidStateTransition("only PENDING inspections can be started")
        updated = await self.repository.update_inspection(
            tenant_id,
            inspection_id,
            {"status": "IN_PROGRESS", "inspector_id": inspector_id or actor_id, "started_at": utc_now()},
        )
        assert updated is not None
        return await self.get_inspection(tenant_id, inspection_id)

    async def record_results(
        self,
        *,
        tenant_id: UUID | str,
        inspection_id: UUID | str,
        results: Sequence[Mapping[str, Any]],
        recorded_by: UUID | str | None = None,
    ) -> dict[str, Any]:
        await self.require_enabled(tenant_id)
        inspection = await self.repository.get_inspection(tenant_id, inspection_id)
        if inspection is None:
            raise InspectionNotFound("inspection not found")
        if inspection["status"] not in {"PENDING", "IN_PROGRESS"}:
            raise InvalidStateTransition("results can only be recorded before completion")
        spec_lines: list[dict[str, Any]] = []
        if inspection.get("inspection_plan_id"):
            plan = await self.get_plan(tenant_id, inspection["inspection_plan_id"])
            spec = await self.get_spec(tenant_id, plan["quality_spec_id"])
            spec_lines = spec["lines"]
        validated = []
        for row in results:
            spec_line = next(
                (line for line in spec_lines if str(line["id"]) == str(row.get("quality_spec_line_id"))),
                {
                    "id": row.get("quality_spec_line_id"),
                    "result_type": row.get("result_type") or "TEXT",
                    "sequence": row.get("sequence"),
                    "characteristic": row.get("characteristic") or "characteristic",
                },
            )
            validated.append(
                {**validate_result_row(row, spec_line), "recorded_by": recorded_by, "recorded_at": utc_now()}
            )
        await self.repository.replace_results(tenant_id, inspection_id, validated)
        return {
            "inspection_id": str(inspection_id),
            "results": await self.repository.list_results(tenant_id, inspection_id),
        }

    async def complete_inspection(
        self,
        *,
        tenant_id: UUID | str,
        inspection_id: UUID | str,
        disposition: str | None = None,
        notes: str | None = None,
        actor_id: UUID | str | None = None,
    ) -> dict[str, Any]:
        await self.require_enabled(tenant_id)
        inspection = await self.get_inspection(tenant_id, inspection_id)
        if inspection["status"] not in {"PENDING", "IN_PROGRESS"}:
            raise InvalidStateTransition("only open inspections can be completed")
        results = inspection["results"]
        if not results:
            raise ValidationError("completion requires recorded results")
        status, resolved_disposition = derive_disposition(results, requested=disposition)
        summary = derive_result_summary(results, await self._setting(tenant_id, "derived_fields", []))
        summary["notes"] = notes
        updated = await self.repository.update_inspection(
            tenant_id,
            inspection_id,
            {
                "status": status,
                "disposition": resolved_disposition,
                "completed_at": utc_now(),
                "result_summary": summary,
            },
        )
        assert updated is not None
        await self.repository.audit(
            tenant_id=tenant_id,
            actor_id=actor_id,
            action="quality.inspection.completed",
            resource_type="quality.inspection",
            resource_id=inspection_id,
            diff={"status": status, "disposition": resolved_disposition},
        )
        await self.repository.emit_event(
            event_type="quality.inspection.completed",
            tenant_id=tenant_id,
            aggregate_id=inspection_id,
            payload={
                "inspection_id": str(inspection_id),
                "disposition": resolved_disposition,
                "result_summary": summary,
            },
        )
        await self.repository.emit_event(
            event_type="quality.inspection.passed" if status == "PASSED" else "quality.inspection.failed",
            tenant_id=tenant_id,
            aggregate_id=inspection_id,
            payload={"inspection_id": str(inspection_id), "failed_line_count": summary.get("failed", 0)},
        )
        return await self.get_inspection(tenant_id, inspection_id)

    async def waive_inspection(
        self,
        *,
        tenant_id: UUID | str,
        inspection_id: UUID | str,
        reason: str,
        approval_ref: str | None,
        actor_id: UUID | str | None = None,
    ) -> dict[str, Any]:
        await self.require_enabled(tenant_id)
        inspection = await self.get_inspection(tenant_id, inspection_id)
        if inspection["status"] != "FAILED":
            raise InvalidStateTransition("only FAILED inspections can be waived")
        if not approval_ref:
            raise ValidationError("waiver requires a recorded approval reference (DM-QA-01)")
        updated = await self.repository.update_inspection(
            tenant_id,
            inspection_id,
            {"status": "WAIVED", "disposition": "ACCEPT", "waiver_reason": reason, "approval_ref": approval_ref},
        )
        assert updated is not None
        await self.repository.emit_event(
            event_type="quality.inspection.waived",
            tenant_id=tenant_id,
            aggregate_id=inspection_id,
            payload={"inspection_id": str(inspection_id), "approval_ref": approval_ref},
        )
        return await self.get_inspection(tenant_id, inspection_id)

    async def cancel_inspection(
        self, *, tenant_id: UUID | str, inspection_id: UUID | str, reason: str, actor_id: UUID | str | None = None
    ) -> dict[str, Any]:
        await self.require_enabled(tenant_id)
        inspection = await self.repository.get_inspection(tenant_id, inspection_id)
        if inspection is None:
            raise InspectionNotFound("inspection not found")
        if inspection["status"] not in {"PENDING", "IN_PROGRESS"}:
            raise InvalidStateTransition("only open inspections can be cancelled")
        await self.repository.update_inspection(
            tenant_id, inspection_id, {"status": "CANCELLED", "cancel_reason": reason}
        )
        await self.repository.emit_event(
            event_type="quality.inspection.cancelled",
            tenant_id=tenant_id,
            aggregate_id=inspection_id,
            payload={"inspection_id": str(inspection_id), "reason": reason},
        )
        return await self.get_inspection(tenant_id, inspection_id)

    # ------------------------------------------------------------------ hold bridge ----------
    async def _hold_status(self, tenant_id: UUID | str) -> dict[str, Any]:
        code = str(await self._setting(tenant_id, "hold_status_code", "HOLD"))
        status = await self.repository.hold_status(tenant_id, code)
        if status is None:
            raise QualityHoldStatusInvalid(f"hold status {code!r} is not configured for this tenant")
        if bool(status.get("is_committable")):
            raise QualityHoldStatusInvalid(f"hold status {code!r} must not be committable")
        return status

    async def apply_hold(
        self,
        *,
        tenant_id: UUID | str,
        inspection_id: UUID | str,
        positions: Sequence[Mapping[str, Any]],
        actor_id: UUID | str | None = None,
        transaction_date: date | None = None,
    ) -> dict[str, Any]:
        from app.modules.inventory_ledger.facade import PostingPort

        await self.require_enabled(tenant_id)
        inspection = await self.get_inspection(tenant_id, inspection_id)
        if inspection["status"] != "FAILED" and inspection.get("disposition") not in {"HOLD", "REJECT"}:
            raise ValidationError("quality hold requires a FAILED/HOLD or REJECT inspection")
        if not positions:
            raise ValidationError("hold requires at least one position")
        hold_status = await self._hold_status(tenant_id)
        port = PostingPort(self.session, tenant_id)
        movements: list[dict[str, Any]] = []
        for position in positions:
            qty = _dec(position.get("qty") or 0)
            if qty <= 0:
                raise ValidationError("hold qty must be positive", details={"position": dict(position)})
            current_status_id = position.get("inventory_status_id") or position.get("current_status_id")
            if current_status_id is None:
                raise HoldTargetNotFound("hold position requires its current inventory_status_id")
            for direction, status_id in (("OUT", current_status_id), ("IN", hold_status["id"])):
                movements.append(
                    {
                        "item_id": position.get("item_id"),
                        "location_id": position.get("location_id"),
                        "inventory_status_id": status_id,
                        "ownership": position.get("ownership") or "OWNED",
                        "qty": -qty if direction == "OUT" else qty,
                        "movement_type": "STATUS_CHANGE",
                        "movement_direction": direction,
                        "lot_id": position.get("lot_id"),
                        "serial_id": position.get("serial_id"),
                        "bin_id": position.get("bin_id"),
                    }
                )
        posting = await port.post_status_change(
            transaction_date=transaction_date or date.today(),
            source_doc_type="quality.inspection",
            source_doc_id=inspection_id,
            idempotency_key=f"quality-hold:{inspection_id}",
            movements=movements,
        )
        transaction_id = posting.get("id") or posting.get("transaction_id")
        if transaction_id is None:
            raise ValidationError("hold posting did not return a transaction id")
        transaction_id = str(transaction_id)
        for position in positions:
            await self.repository.link_document(
                tenant_id=tenant_id,
                link_type=f"QUALITY_HOLD:{position.get('inventory_status_id') or position.get('current_status_id')}",
                from_type="quality.inspection",
                from_id=inspection_id,
                to_type="inventory.transaction",
                to_id=transaction_id,
                qty=_dec(position.get("qty") or 0),
            )
        await self.repository.emit_event(
            event_type="quality.hold.applied",
            tenant_id=tenant_id,
            aggregate_id=inspection_id,
            payload={
                "inspection_id": str(inspection_id),
                "inventory_transaction_id": str(transaction_id),
                "positions": len(positions),
                "previous_status": [str(p.get("inventory_status_id") or p.get("current_status_id")) for p in positions],
            },
        )
        return {
            "inspection_id": str(inspection_id),
            "inventory_transaction_id": str(transaction_id),
            "positions": len(positions),
        }

    async def release_hold(
        self,
        *,
        tenant_id: UUID | str,
        inspection_id: UUID | str,
        positions: Sequence[Mapping[str, Any]],
        actor_id: UUID | str | None = None,
        transaction_date: date | None = None,
    ) -> dict[str, Any]:
        from app.modules.inventory_ledger.facade import PostingPort

        await self.require_enabled(tenant_id)
        inspection = await self.get_inspection(tenant_id, inspection_id)
        if inspection["status"] not in {"PASSED", "WAIVED"} and inspection.get("disposition") != "ACCEPT":
            raise HoldReleaseDenied("hold release requires a PASSED/WAIVED inspection (DM-QA-01)")
        links = [
            link
            for link in inspection["hold_links"]
            if not str(link.get("link_type", "")).startswith("QUALITY_RELEASE")
        ]
        if not links:
            raise ValidationError("inspection has no applied holds to release")
        if not positions:
            raise ValidationError("hold release requires the hold positions payload")
        hold_status = await self._hold_status(tenant_id)
        release_code = str(await self._setting(tenant_id, "hold_release_status_code", "AVAILABLE"))
        release_status = await self.repository.hold_status(tenant_id, release_code)
        if release_status is None:
            raise HoldReleaseDenied(f"release status {release_code!r} is not configured")
        port = PostingPort(self.session, tenant_id)
        movements: list[dict[str, Any]] = []
        for position in positions:
            qty = _dec(position.get("qty") or 0)
            if qty <= 0:
                raise ValidationError("release qty must be positive")
            for direction, status_id in (("OUT", hold_status["id"]), ("IN", release_status["id"])):
                movements.append(
                    {
                        "item_id": position.get("item_id"),
                        "location_id": position.get("location_id"),
                        "inventory_status_id": status_id,
                        "ownership": position.get("ownership") or "OWNED",
                        "qty": -qty if direction == "OUT" else qty,
                        "movement_type": "STATUS_CHANGE",
                        "movement_direction": direction,
                        "lot_id": position.get("lot_id"),
                        "serial_id": position.get("serial_id"),
                        "bin_id": position.get("bin_id"),
                    }
                )
        posting = await port.post_status_change(
            transaction_date=transaction_date or date.today(),
            source_doc_type="quality.inspection",
            source_doc_id=inspection_id,
            idempotency_key=f"quality-release:{inspection_id}",
            movements=movements,
        )
        transaction_id: str | None = posting.get("id") or posting.get("transaction_id")
        if transaction_id is None:
            raise ValidationError("release posting did not return a transaction id")
        transaction_id = str(transaction_id)
        for position in positions:
            await self.repository.link_document(
                tenant_id=tenant_id,
                link_type="QUALITY_RELEASE",
                from_type="quality.inspection",
                from_id=inspection_id,
                to_type="inventory.transaction",
                to_id=transaction_id,
                qty=_dec(position.get("qty") or 0),
            )
        await self.repository.emit_event(
            event_type="quality.hold.released",
            tenant_id=tenant_id,
            aggregate_id=inspection_id,
            payload={
                "inspection_id": str(inspection_id),
                "inventory_transaction_id": str(transaction_id),
                "positions": len(positions),
                "release_status": release_code,
            },
        )
        return {
            "inspection_id": str(inspection_id),
            "inventory_transaction_id": str(transaction_id),
            "positions": len(positions),
        }

    # ------------------------------------------------------------------ COA -----------------
    async def create_coa(
        self, *, tenant_id: UUID | str, values: Mapping[str, Any], actor_id: UUID | str | None = None
    ) -> dict[str, Any]:
        await self.require_enabled(tenant_id)
        inspection_id = values.get("inspection_id")
        if inspection_id is None:
            raise ValidationError("COA requires inspection_id")
        inspection = await self.get_inspection(tenant_id, inspection_id)
        plan = (
            await self.get_plan(tenant_id, inspection["inspection_plan_id"])
            if inspection.get("inspection_plan_id")
            else None
        )
        eligible_setting = await self._setting(tenant_id, "coa_sampling_eligible", list(COA_ELIGIBLE_SAMPLING))
        if not inspection_sampling_eligible(plan, eligible=eligible_setting):
            raise CoaSamplingIneligible("inspection sampling method is not COA-eligible")
        lot_id = inspection.get("lot_id") or values.get("lot_id")
        number = await self.repository.next_number(tenant_id, "COA")
        coa = await self.repository.insert_coa(
            {
                "id": uuid4(),
                "tenant_id": tenant_id,
                "coa_number": number,
                "inspection_id": inspection["id"],
                "item_id": inspection["item_id"],
                "lot_id": lot_id,
                "status": "DRAFT",
                "external_customer_ref": values.get("external_customer_ref"),
                "attachment_id": values.get("attachment_id"),
            }
        )
        spec_lines = (
            {
                str(line["id"]): line
                for line in (
                    await self.get_spec(tenant_id, (await self.get_plan(tenant_id, plan["id"]))["quality_spec_id"])
                )["lines"]
            }
            if plan
            else {}
        )
        coa_rows = []
        for index, result in enumerate(inspection["results"], start=1):
            spec_line = spec_lines.get(str(result.get("quality_spec_line_id")), {})
            if spec_line.get("is_internal"):
                continue  # DM-QA-06 supplier-facing COA excludes internal-only characteristics
            coa_rows.append(
                {
                    "id": uuid4(),
                    "tenant_id": tenant_id,
                    "coa_id": coa["id"],
                    "sequence": index,
                    "quality_spec_line_id": result.get("quality_spec_line_id"),
                    "characteristic": str(result.get("characteristic") or ""),
                    "result_value": str(result.get("measured_value_text") or ""),
                    "uom_id": spec_line.get("uom_id"),
                    "is_pass": bool(result.get("is_pass")),
                }
            )
        await self.repository.insert_coa_lines(coa_rows)
        await self.repository.emit_event(
            event_type="quality.coa.created",
            tenant_id=tenant_id,
            aggregate_id=coa["id"],
            payload={"coa_id": coa["id"], "inspection_id": str(inspection["id"])},
        )
        return await self.get_coa(tenant_id, coa["id"])

    async def get_coa(self, tenant_id: UUID | str, coa_id: UUID | str) -> dict[str, Any]:
        coa = await self.repository.get_coa(tenant_id, coa_id)
        if coa is None:
            raise CoaNotFound("COA not found", details={"coa_id": str(coa_id)})
        coa["lines"] = await self.repository.coa_lines(tenant_id, coa_id)
        return coa

    async def list_coas(self, tenant_id: UUID | str, **filters: Any) -> list[dict[str, Any]]:
        return await self.repository.list_coas(tenant_id, **filters)

    async def issue_coa(
        self,
        *,
        tenant_id: UUID | str,
        coa_id: UUID | str,
        issued_at: datetime | None = None,
        actor_id: UUID | str | None = None,
    ) -> dict[str, Any]:
        await self.require_enabled(tenant_id)
        coa = await self.get_coa(tenant_id, coa_id)
        if coa["status"] != "DRAFT":
            raise InvalidStateTransition("only DRAFT COAs can be issued")
        await self.repository.update_coa(
            tenant_id, coa_id, {"status": "ISSUED", "issued_at": issued_at or utc_now(), "issued_by": actor_id}
        )
        await self.repository.emit_event(
            event_type="quality.coa.issued",
            tenant_id=tenant_id,
            aggregate_id=coa_id,
            payload={"coa_id": str(coa_id), "inspection_id": coa["inspection_id"]},
        )
        return await self.get_coa(tenant_id, coa_id)

    async def revoke_coa(
        self, *, tenant_id: UUID | str, coa_id: UUID | str, reason: str, actor_id: UUID | str | None = None
    ) -> dict[str, Any]:
        await self.require_enabled(tenant_id)
        coa = await self.get_coa(tenant_id, coa_id)
        if coa["status"] != "ISSUED":
            raise InvalidStateTransition("only ISSUED COAs can be revoked")
        await self.repository.update_coa(tenant_id, coa_id, {"status": "REVOKED", "revoked_reason": reason})
        await self.repository.emit_event(
            event_type="quality.coa.revoked",
            tenant_id=tenant_id,
            aggregate_id=coa_id,
            payload={"coa_id": str(coa_id), "reason": reason},
        )
        return await self.get_coa(tenant_id, coa_id)

    # ------------------------------------------------------------------ settings/images ------
    SETTING_KEYS = (
        "hold_status_code",
        "default_queue_code",
        "auto_coa",
        "coa_sampling_eligible",
        "spec_line_limit",
        "derived_fields",
        "notification_recipients",
        "inspection_assignment",
        "hold_requires_approval",
        "hold_release_status_code",
    )

    async def get_settings(self, tenant_id: UUID | str) -> dict[str, Any]:
        values = {}
        for key in self.SETTING_KEYS:
            values[key] = await self.repository.get_setting(tenant_id, key)
        return values

    async def update_settings(
        self, *, tenant_id: UUID | str, values: Mapping[str, Any], actor_id: UUID | str | None = None
    ) -> dict[str, Any]:
        await self.require_enabled(tenant_id)
        unknown = set(values) - set(self.SETTING_KEYS)
        if unknown:
            raise ValidationError("unsupported quality settings", details={"keys": sorted(unknown)})
        if "hold_status_code" in values:
            status = await self.repository.hold_status(tenant_id, str(values["hold_status_code"]))
            if status is None:
                raise QualityHoldStatusInvalid("hold status code is not configured")
            if bool(status.get("is_committable")):
                raise QualityHoldStatusInvalid("hold status must not be committable")
        for key, value in values.items():
            await self.repository.set_setting(tenant_id, key, value)
        await self.repository.audit(
            tenant_id=tenant_id,
            actor_id=actor_id,
            action="quality.settings.updated",
            resource_type="quality.settings",
            resource_id=None,
            diff=dict(values),
        )
        return await self.get_settings(tenant_id)

    async def attach_image(
        self,
        *,
        tenant_id: UUID | str,
        inspection_id: UUID | str,
        attachment_id: UUID | str,
        actor_id: UUID | str | None = None,
    ) -> dict[str, Any]:
        await self.require_enabled(tenant_id)
        inspection = await self.get_inspection(tenant_id, inspection_id)
        attachment = await self.repository.attachment(tenant_id, attachment_id)
        if attachment is None:
            raise ValidationError("attachment not found", details={"attachment_id": str(attachment_id)})
        await self.repository.link_document(
            tenant_id=tenant_id,
            link_type="QUALITY_IMAGE",
            from_type="quality.inspection",
            from_id=inspection["id"],
            to_type="platform.attachment",
            to_id=attachment_id,
        )
        return {"inspection_id": str(inspection_id), "attachment_id": str(attachment_id), "is_quality_image": True}
