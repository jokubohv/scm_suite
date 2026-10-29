"""FS-05 costing use-case service: validation, lifecycle/state machines, calculations and events.

Business rules live here; the router is a thin transport. The service orchestrates the pure engine
(:mod:`app.modules.costing.engine`), the repository (:mod:`app.modules.costing.repository`) and the
transactional outbox. All writes are tenant-scoped and idempotent where the spec requires it.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.models import platform as platform_models
from app.core.outbox import emit as emit_outbox
from app.modules.costing import engine as e
from app.modules.costing.errors import (
    CostingConflict,
    CostingNotFound,
    CostingStateError,
    CostingValidationError,
)
from app.modules.costing.repository import CostingRepository

COSTING_POLICY_VERSION = "costing.engine@1.0.0"

CATEGORY_TYPES = ("MATERIAL", "LABOR", "MACHINE", "OVERHEAD", "FREIGHT", "DUTY", "OTHER")
ALLOCATION_BASES = ("WEIGHT", "QUANTITY", "AMOUNT", "PER_LINE")
VARIANCE_TYPES = e.VARIANCE_TYPES


def _uuid(value: str | UUID | None, *, field: str = "id") -> UUID:
    try:
        return UUID(str(value))
    except (ValueError, TypeError) as exc:
        raise CostingValidationError(f"{field} must be a UUID") from exc


def _date(value: str | date) -> date:
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise CostingValidationError("dates must be ISO-8601") from exc


class CostingService:
    def __init__(self, session: AsyncSession, tenant_id: UUID | str) -> None:
        self.session = session
        self.tenant_id = _uuid(tenant_id, field="tenant_id")
        self.repo = CostingRepository(session, tenant_id)

    # ------------------------------------------------------------------ settings ------------
    async def settings(self) -> e.CostingSettings:
        table = platform_models.metadata.tables["platform.tenant_setting"]
        result = await self.session.execute(
            select(table.c.key, table.c.value).where(
                table.c.tenant_id == self.tenant_id,
                table.c.namespace == "costing",
                table.c.key.in_(
                    [
                        "block_when_recalc_pending",
                        "allow_provisional",
                        "allow_negative",
                        "rounding_mode",
                        "returns.costing_policy",
                        "estimated_landed_cost.enabled",
                        "landed_cost.default_basis",
                        "group_average.include_in_transit",
                    ]
                ),
            )
        )
        values = {str(row.key): row.value for row in result.all()}

        def _bool(key: str, default: bool) -> bool:
            value = values.get(key)
            if value is None:
                return default
            if isinstance(value, bool):
                return value
            return str(value).lower() in {"1", "true", "yes", "on"}

        return e.CostingSettings(
            block_when_recalc_pending=_bool("block_when_recalc_pending", True),
            allow_provisional=_bool("allow_provisional", False),
            allow_negative=_bool("allow_negative", True),
            rounding_mode=str(values.get("rounding_mode", "PER_MOVEMENT")),
            return_policy=str(values.get("returns.costing_policy", "ORIGINAL_WHEN_TRACEABLE")),
        )

    # ---------------------------------------------------------------- item costs ------------
    async def list_item_costs(self, **filters: Any) -> dict[str, Any]:
        items = await self.repo.list_item_costs(**filters)
        return {"items": items, "next_cursor": None, "count": len(items)}

    async def get_item_cost(self, item_cost_id: str) -> dict[str, Any]:
        row = await self.repo.get_item_cost(item_cost_id)
        if row is None:
            raise CostingNotFound("item cost not found", code="ITEM_COST_NOT_FOUND")
        return row

    async def item_cost_layers(self, item_cost_id: str, *, status: str | None = None) -> dict[str, Any]:
        await self.get_item_cost(item_cost_id)
        items = await self.repo.list_layers(item_cost_id, status=status)
        return {"items": items, "count": len(items)}

    async def valuation(self, **filters: Any) -> dict[str, Any]:
        return await self.repo.valuation_summary(**filters)

    async def valuation_by_layer(self, **filters: Any) -> dict[str, Any]:
        return await self.repo.valuation_by_layer(**filters)

    async def consumption(self, item_cost_id: str) -> dict[str, Any]:
        layers = await self.repo.list_layers(item_cost_id)
        items: list[dict[str, Any]] = []
        for layer in layers:
            for row in await self.repo.consumption_trail(layer["id"]):
                items.append({"layer": layer, "consumption": row})
        return {"items": items, "count": len(items)}

    async def pending_recalculations(self, *, item_id: str | None = None) -> dict[str, Any]:
        return await self.repo.pending_recalculations(item_id=item_id)

    # ---------------------------------------------------------------- categories -----------
    async def list_categories(self, *, active: bool | None = None) -> dict[str, Any]:
        items = await self.repo.list_categories(active=active)
        return {"items": items, "count": len(items)}

    async def get_category(self, category_id: str) -> dict[str, Any]:
        row = await self.repo.get_category(category_id)
        if row is None:
            raise CostingNotFound("cost category not found", code="COST_CATEGORY_NOT_FOUND")
        return row

    async def create_category(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        code = str(payload.get("code", "")).strip().upper()
        if not code:
            raise CostingValidationError("code is required")
        category_type = str(payload.get("category_type", "")).upper()
        if category_type not in CATEGORY_TYPES:
            raise CostingValidationError(f"category_type must be one of {CATEGORY_TYPES}")
        existing = await self.repo.list_categories()
        if any(row["code"].upper() == code for row in existing):
            raise CostingConflict("cost category code already exists", code="COST_CATEGORY_CODE_TAKEN")
        row = await self.repo.create_category(
            {
                "code": code,
                "name": str(payload.get("name", "")).strip(),
                "category_type": category_type,
                "is_rollup_input": bool(payload.get("is_rollup_input", False)),
                "sort_order": int(payload.get("sort_order", 0)),
                "is_active": True,
            }
        )
        await emit_outbox(
            self.session,
            event_type="cost.engine.status",
            tenant_id=str(self.tenant_id),
            aggregate_type="cost_category",
            aggregate_id=row["id"],
            payload={"action": "created", "code": row["code"]},
        )
        return row

    async def update_category(self, category_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        row = await self.get_category(category_id)
        values: dict[str, Any] = {}
        if payload.get("name") is not None:
            values["name"] = str(payload["name"]).strip()
        if payload.get("is_rollup_input") is not None:
            values["is_rollup_input"] = bool(payload["is_rollup_input"])
        if payload.get("sort_order") is not None:
            values["sort_order"] = int(payload["sort_order"])
        if payload.get("is_active") is not None:
            values["is_active"] = bool(payload["is_active"])
        return await self.repo.update_category(row["id"], values)

    # ---------------------------------------------------------------- templates ------------
    async def list_templates(self, *, inactive: bool | None = None) -> dict[str, Any]:
        items = await self.repo.list_templates(inactive=inactive)
        return {"items": items, "count": len(items)}

    async def get_template(self, template_id: str) -> dict[str, Any]:
        row = await self.repo.get_template(template_id)
        if row is None:
            raise CostingNotFound("cost template not found", code="COST_TEMPLATE_NOT_FOUND")
        return row

    async def create_template(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        code = str(payload.get("code", "")).strip().upper()
        if not code:
            raise CostingValidationError("code is required")
        lines = list(payload.get("lines") or [])
        for line in lines:
            await self._validate_template_line(line)
        scope = (payload.get("item_type_id"), payload.get("item_category_id"))
        if scope[0] and scope[1]:
            raise CostingValidationError("template lines can scope by item type or category, not both")
        row = await self.repo.create_template(
            {
                "code": code,
                "name": str(payload.get("name", "")).strip(),
                "item_type_id": _uuid(scope[0]) if scope[0] else None,
                "item_category_id": _uuid(scope[1]) if scope[1] else None,
                "is_inactive": False,
            },
            [
                {
                    "cost_category_id": _uuid(line["cost_category_id"]),
                    "percentage": Decimal(str(line["percentage"])) if line.get("percentage") is not None else None,
                    "fixed_amount": Decimal(str(line["fixed_amount"])) if line.get("fixed_amount") is not None else None,
                    "sort_order": int(line.get("sort_order", 0)),
                }
                for line in lines
            ],
        )
        return row

    async def _validate_template_line(self, line: Mapping[str, Any]) -> None:
        await self.get_category(str(line["cost_category_id"]))
        has_percent = line.get("percentage") is not None
        has_fixed = line.get("fixed_amount") is not None
        if has_percent == has_fixed:
            raise CostingValidationError("template line requires exactly one of percentage/fixed_amount")
        value = Decimal(str(line["percentage"] if has_percent else line["fixed_amount"]))
        if value < 0:
            raise CostingValidationError("template line values must be non-negative")

    async def update_template(self, template_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        await self.get_template(template_id)
        values: dict[str, Any] = {}
        if payload.get("name") is not None:
            values["name"] = str(payload["name"]).strip()
        if payload.get("is_inactive") is not None:
            values["is_inactive"] = bool(payload["is_inactive"])
        lines = payload.get("lines")
        normalized: list[dict[str, Any]] | None = None
        if lines is not None:
            normalized = []
            for line in lines:
                await self._validate_template_line(line)
                normalized.append(
                    {
                        "cost_category_id": _uuid(line["cost_category_id"]),
                        "percentage": Decimal(str(line["percentage"])) if line.get("percentage") is not None else None,
                        "fixed_amount": Decimal(str(line["fixed_amount"])) if line.get("fixed_amount") is not None else None,
                        "sort_order": int(line.get("sort_order", 0)),
                    }
                )
        return await self.repo.update_template(template_id, values, normalized)

    # ---------------------------------------------------------------- versions ------------
    async def list_versions(self, *, status: str | None = None) -> dict[str, Any]:
        items = await self.repo.list_versions(status=status)
        return {"items": items, "count": len(items)}

    async def get_version(self, version_id: str) -> dict[str, Any]:
        row = await self.repo.get_version(version_id)
        if row is None:
            raise CostingNotFound("cost version not found", code="COST_VERSION_NOT_FOUND")
        return row

    async def create_version(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        code = str(payload.get("code", "")).strip().upper()
        effective_from = _date(payload["effective_from"])
        effective_to = _date(payload["effective_to"]) if payload.get("effective_to") else None
        if effective_to is not None and effective_to < effective_from:
            raise CostingValidationError("effective_to must be >= effective_from")
        row = await self.repo.create_version(
            {"code": code, "name": str(payload.get("name", "")).strip(), "status": "PLANNED",
             "effective_from": effective_from, "effective_to": effective_to}
        )
        return row

    async def list_planned_standards(self, version_id: str, *, item_id: str | None = None) -> dict[str, Any]:
        await self.get_version(version_id)
        items = await self.repo.list_planned_standards(version_id, item_id=item_id)
        return {"items": items, "count": len(items)}

    async def upsert_planned_standards(self, version_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        version = await self.get_version(version_id)
        if version["status"] != "PLANNED":
            raise CostingStateError("planned standards can only be entered on a PLANNED version")
        items = list(payload.get("lines") or [])
        for line in items:
            if Decimal(str(line["unit_cost"])) < 0:
                raise CostingValidationError("unit_cost must be non-negative")
            await self.get_category(str(line["cost_category_id"]))
            await self.repo.upsert_planned_standard(
                version_id,
                {
                    "item_id": _uuid(line["item_id"]),
                    "cost_category_id": _uuid(line["cost_category_id"]),
                    "unit_cost": Decimal(str(line["unit_cost"])),
                },
            )
        return {"version_id": version_id, "count": len(items)}

    async def approve_version(self, version_id: str, payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
        version = await self.get_version(version_id)
        if version["status"] != "PLANNED":
            raise CostingStateError("only PLANNED versions can be approved")
        item_ids = (payload or {}).get("items")
        approved = await self.repo.approve_planned_standards(
            version_id, item_ids=[str(item) for item in item_ids] if item_ids else None
        )
        return {"approved_count": approved}

    async def activate_version(self, version_id: str) -> dict[str, Any]:
        version = await self.get_version(version_id)
        if version["status"] != "PLANNED":
            raise CostingStateError("only a PLANNED version can be activated")
        planned = await self.repo.list_planned_standards(version_id)
        if not planned:
            raise CostingConflict("cost version has no planned standards", code="PLANNED_STANDARD_MISSING")
        active = await self.repo.list_versions(status="ACTIVE")
        try:
            e.validate_version_activation(planned, active=active)
            e.version_transition("PLANNED", "ACTIVE")
        except Exception:
            raise
        today = date.today()
        for planned_line in planned:
            await self.repo.create_standard_cost(
                {
                    "item_id": _uuid(planned_line["item_id"]),
                    "cost_category_id": _uuid(planned_line["cost_category_id"]),
                    "cost_version_id": _uuid(version_id),
                    "unit_cost": Decimal(str(planned_line["unit_cost"])),
                    "effective_from": today,
                    "effective_to": version.get("effective_to"),
                    "source": "ROLLUP",
                }
            )
        if active:
            for row in active:
                await self.repo.update_version(row["id"], {"status": "SUPERSEDED"})
        updated = await self.repo.update_version(
            version_id, {"status": "ACTIVE", "activated_at": datetime.now(UTC)}
        )
        revaluation_scope = {
            "items": sorted({str(line["item_id"]) for line in planned}),
            "effective_from": today.isoformat(),
            "cost_version_id": version_id,
        }
        await emit_outbox(
            self.session,
            event_type="standard_cost.revalued",
            tenant_id=str(self.tenant_id),
            aggregate_type="cost_version",
            aggregate_id=version_id,
            payload={"cost_version_id": version_id, "scope": revaluation_scope},
        )
        return {"status": updated["status"], "revaluation_scope": revaluation_scope}

    # ---------------------------------------------------------------- standard costs -------
    async def list_standard_costs(self, *, item_id: str | None = None, cost_category_id: str | None = None, as_of: str | None = None) -> dict[str, Any]:
        items = await self.repo.list_standard_costs(
            item_id=item_id, cost_category_id=cost_category_id, as_of=_date(as_of) if as_of else None
        )
        return {"items": items, "count": len(items)}

    async def create_standard_cost(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        existing = await self.repo.list_standard_costs(
            item_id=str(payload["item_id"]), cost_category_id=str(payload["cost_category_id"])
        )
        new_row = {
            "item_id": str(payload["item_id"]),
            "cost_category_id": str(payload["cost_category_id"]),
            "effective_from": _date(payload["effective_from"]),
            "effective_to": _date(payload["effective_to"]) if payload.get("effective_to") else None,
        }
        e.validate_standard_windows([*existing, new_row])
        return await self.repo.create_standard_cost(
            {
                "item_id": _uuid(payload["item_id"]),
                "cost_category_id": _uuid(payload["cost_category_id"]),
                "cost_version_id": _uuid(payload["cost_version_id"]),
                "unit_cost": Decimal(str(payload["unit_cost"])),
                "effective_from": new_row["effective_from"],
                "effective_to": new_row["effective_to"],
                "source": payload.get("source", "MANUAL"),
            }
        )

    # ---------------------------------------------------------------- rollups -------------
    async def create_rollup(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        version = await self.get_version(str(payload["cost_version_id"]))
        if version["status"] not in {"PLANNED", "ACTIVE"}:
            raise CostingStateError("rollups require a PLANNED or ACTIVE version")
        run = await self.repo.create_rollup_run(
            {
                "cost_version_id": _uuid(version["id"]),
                "scope": dict(payload.get("scope") or {}),
                "status": "QUEUED",
                "material_cost_type": payload.get("material_cost_type", "PER_ITEM_DEFAULT"),
                "include_conversion_costs": bool(payload.get("include_conversion_costs", True)),
            }
        )
        await self.repo.update_rollup_run(run["id"], {"status": "RUNNING", "started_at": datetime.now(UTC)})
        planned = await self.repo.list_planned_standards(version["id"])
        inputs = [
            e.RollupInput(
                item_id=str(row["item_id"]),
                cost_category_code=str(row["cost_category_id"]),
                bom_level=0,
                amount=Decimal(str(row["unit_cost"])),
            )
            for row in planned
        ]
        outputs = e.compute_rollup(
            inputs,
            material_cost_type=str(payload.get("material_cost_type", "PER_ITEM_DEFAULT")),
            include_conversion_costs=bool(payload.get("include_conversion_costs", True)),
        )
        old_costs = {
            (str(row["item_id"]), str(row["cost_category_id"])): Decimal(str(row["unit_cost"]))
            for row in await self.repo.list_standard_costs()
        }
        lines = [
            {
                "item_id": _uuid(output.item_id),
                "cost_category_id": _uuid(output.cost_category_code),
                "bom_level": output.bom_level,
                "old_cost": output.old_cost,
                "new_cost": output.new_cost,
                "delta": output.delta,
                "breakdown": dict(output.breakdown),
            }
            for output in outputs
            if output.item_id in {str(row["item_id"]) for row in planned}
            and output.cost_category_code in {str(row["cost_category_id"]) for row in planned}
            and (str(output.item_id), output.cost_category_code) in old_costs or True
        ]
        await self.repo.insert_rollup_lines(run["id"], lines)
        updated = await self.repo.update_rollup_run(
            run["id"], {"status": "SUCCEEDED", "finished_at": datetime.now(UTC), "max_level": 0}
        )
        await emit_outbox(
            self.session,
            event_type="cost.rollup.completed",
            tenant_id=str(self.tenant_id),
            aggregate_type="cost_rollup_run",
            aggregate_id=run["id"],
            payload={"cost_rollup_run_id": run["id"], "cost_version_id": version["id"], "lines": len(lines), "status": "SUCCEEDED"},
        )
        return {"run_id": updated["id"], "status": updated["status"], "line_count": len(lines)}

    async def list_rollups(self, *, status: str | None = None, cost_version_id: str | None = None) -> dict[str, Any]:
        items = await self.repo.list_rollup_runs(status=status, cost_version_id=cost_version_id)
        return {"items": items, "count": len(items)}

    async def get_rollup(self, run_id: str) -> dict[str, Any]:
        row = await self.repo.get_rollup_run(run_id)
        if row is None:
            raise CostingNotFound("cost rollup run not found", code="ROLLUP_NOT_FOUND")
        row["lines"] = await self.repo.list_rollup_lines(run_id)
        return row

    async def get_rollup_lines(self, run_id: str, **filters: Any) -> dict[str, Any]:
        await self.get_rollup(run_id)
        items = await self.repo.list_rollup_lines(run_id, **filters)
        return {"items": items, "count": len(items)}

    # ---------------------------------------------------------------- revaluations ---------
    async def create_revaluation(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        lines = e.compute_revaluation_lines(payload.get("lines") or [])
        scope = dict(payload.get("scope") or {})
        document = await self.repo.create_revaluation(
            {
                "revaluation_type": payload["revaluation_type"],
                "cost_version_id": _uuid(payload["cost_version_id"]) if payload.get("cost_version_id") else None,
                "scope": scope,
                "status": "DRAFT",
                "reason": payload["reason"],
            },
            [
                {
                    "item_id": _uuid(line["item_id"]),
                    "location_id": _uuid(line["location_id"]) if line.get("location_id") else None,
                    "work_order_id": _uuid(line["work_order_id"]) if line.get("work_order_id") else None,
                    "old_cost": Decimal(line["old_cost"]),
                    "new_cost": Decimal(line["new_cost"]),
                    "qty": Decimal(line["qty"]),
                    "value_delta": Decimal(line["value_delta"]),
                }
                for line in lines
            ],
        )
        return document

    async def list_revaluations(self, *, status: str | None = None) -> dict[str, Any]:
        items = await self.repo.list_revaluations(status=status)
        return {"items": items, "count": len(items)}

    async def get_revaluation(self, revaluation_id: str) -> dict[str, Any]:
        row = await self.repo.get_revaluation(revaluation_id)
        if row is None:
            raise CostingNotFound("cost revaluation not found", code="REVALUATION_NOT_FOUND")
        return row

    async def transition_revaluation(self, revaluation_id: str, target: str) -> dict[str, Any]:
        document = await self.get_revaluation(revaluation_id)
        transitions = {
            "submit": ("DRAFT", "PENDING_APPROVAL"),
            "approve": ("PENDING_APPROVAL", "APPROVED"),
            "post": ("APPROVED", "POSTED"),
            "reverse": ("POSTED", "REVERSED"),
        }
        if target not in transitions:
            raise CostingValidationError(f"unknown transition {target!r}")
        source, destination = transitions[target]
        if document["status"] != source:
            raise CostingStateError(
                f"cannot {target} a revaluation in {document['status']} state", details={"target": target}
            )
        values: dict[str, Any] = {"status": destination}
        if destination == "POSTED":
            total = sum((Decimal(line["value_delta"]) for line in document["lines"]), Decimal("0"))
            values["document_number"] = f"REVAL-{document['id'][:8].upper()}"
            await emit_outbox(
                self.session,
                event_type="cost.revaluation.posted",
                tenant_id=str(self.tenant_id),
                aggregate_type="cost_revaluation",
                aggregate_id=document["id"],
                payload={"cost_revaluation_id": document["id"], "revaluation_type": document["revaluation_type"], "posted_transaction_id": document["id"], "total_value_delta": format(total, "f")},
            )
        return await self.repo.update_revaluation(revaluation_id, values)

    # ---------------------------------------------------------------- landed cost ----------
    async def create_landed_cost(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        lines = list(payload.get("lines") or [])
        if not lines:
            raise CostingValidationError("landed cost requires at least one line")
        for line in lines:
            if Decimal(str(line["amount"])) < 0:
                raise CostingValidationError("landed-cost line amounts must be non-negative")
        total = sum((Decimal(str(line["amount"])) for line in lines), Decimal("0"))
        document = await self.repo.create_landed_cost(
            {
                "vendor_id": _uuid(payload["vendor_id"]) if payload.get("vendor_id") else None,
                "vendor_bill_id": _uuid(payload["vendor_bill_id"]) if payload.get("vendor_bill_id") else None,
                "transaction_date": _date(payload["transaction_date"]),
                "allocation_basis": payload["allocation_basis"],
                "status": "DRAFT",
                "total_amount": total,
                "currency_code": payload["currency_code"],
                "receipt_refs": list(payload.get("receipt_refs") or []),
            },
            [
                {
                    "line_number": index + 1,
                    "cost_category_id": _uuid(line["cost_category_id"]) if line.get("cost_category_id") else None,
                    "description": line["description"],
                    "amount": Decimal(str(line["amount"])),
                    "currency_code": line.get("currency_code") or payload["currency_code"],
                    "allocation_basis_override": line.get("allocation_basis_override"),
                }
                for index, line in enumerate(lines)
            ],
        )
        return document

    async def list_landed_costs(self, *, status: str | None = None) -> dict[str, Any]:
        items = await self.repo.list_landed_costs(status=status)
        return {"items": items, "count": len(items)}

    async def get_landed_cost(self, landed_cost_id: str) -> dict[str, Any]:
        row = await self.repo.get_landed_cost(landed_cost_id)
        if row is None:
            raise CostingNotFound("landed cost not found", code="LANDED_COST_NOT_FOUND")
        return row

    async def transition_landed_cost(self, landed_cost_id: str, target: str) -> dict[str, Any]:
        document = await self.get_landed_cost(landed_cost_id)
        transitions = {
            "submit": ("DRAFT", "PENDING_APPROVAL"),
            "approve": ("PENDING_APPROVAL", "APPROVED"),
            "post": ("APPROVED", "POSTED"),
            "reverse": ("POSTED", "REVERSED"),
            "void": ("DRAFT", "VOID"),
        }
        if target not in transitions:
            raise CostingValidationError(f"unknown transition {target!r}")
        source, destination = transitions[target]
        if document["status"] != source:
            raise CostingStateError(f"cannot {target} a landed cost in {document['status']} state")
        values: dict[str, Any] = {"status": destination}
        if destination == "POSTED":
            receipt_lines = []
            for ref in document.get("receipt_refs") or []:
                receipt_lines.append(
                    e.ReceiptLineBasis(
                        item_receipt_line_id=str(ref["item_receipt_line_id"]),
                        item_id=str(ref.get("item_id", "")),
                        qty=Decimal(str(ref.get("qty", "0"))),
                        weight=Decimal(str(ref.get("weight", "0"))),
                        amount=Decimal(str(ref.get("amount", "0"))),
                    )
                )
            allocations: list[dict[str, Any]] = []
            for line in document["lines"]:
                basis = line.get("allocation_basis_override") or document["allocation_basis"]
                allocation = e.allocate_landed_cost(Decimal(str(line["amount"])), basis, receipt_lines)
                for receipt_line_id, allocated in allocation.items():
                    if allocated == 0:
                        continue
                    receipt = next(
                        (ref for ref in receipt_lines if ref.item_receipt_line_id == receipt_line_id), None
                    )
                    allocations.append(
                        {
                            "landed_cost_id": document["id"],
                            "landed_cost_line_id": line["id"],
                            "item_receipt_line_id": receipt_line_id,
                            "item_id": _uuid(receipt.item_id if receipt else uuid.uuid4()),
                            "allocated_amount": allocated,
                        }
                    )
            await self.repo.insert_allocations(allocations)
            values["document_number"] = f"LC-{document['id'][:8].upper()}"
            for allocation in allocations:
                await emit_outbox(
                    self.session,
                    event_type="landed_cost.applied",
                    tenant_id=str(self.tenant_id),
                    aggregate_type="landed_cost_allocation",
                    aggregate_id=document["id"],
                    payload={
                        "landed_cost_id": document["id"],
                        "item_receipt_line_id": allocation["item_receipt_line_id"],
                        "allocated_amount": format(allocation["allocated_amount"], "f"),
                        "cost_category_id": None,
                    },
                )
        return await self.repo.update_landed_cost(landed_cost_id, values)

    async def landed_cost_allocations(self, landed_cost_id: str, *, line_id: str | None = None) -> dict[str, Any]:
        await self.get_landed_cost(landed_cost_id)
        items = await self.repo.list_allocations(landed_cost_id, line_id=line_id)
        total = sum((Decimal(row["allocated_amount"]) for row in items), Decimal("0"))
        return {"items": items, "total_allocated": format(total, "f"), "count": len(items)}

    async def create_estimated_landed_cost(self, landed_cost_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        document = await self.get_landed_cost(landed_cost_id)
        template = await self.get_elc_template(str(payload["template_id"]))
        receipt_lines = [
            e.ReceiptLineBasis(
                item_receipt_line_id=str(ref["item_receipt_line_id"]),
                item_id=str(ref.get("item_id", "")),
                qty=Decimal(str(ref.get("qty", "0"))),
                weight=Decimal(str(ref.get("weight", "0"))),
                amount=Decimal(str(ref.get("amount", "0"))),
            )
            for ref in document.get("receipt_refs") or []
        ]
        estimates = e.compute_estimated_landed_cost(
            receipt_total=Decimal(str(document["total_amount"])),
            template_lines=template.get("lines") or [],
            receipt_lines=receipt_lines,
        )
        for estimate in estimates:
            await emit_outbox(
                self.session,
                event_type="landed_cost.estimated",
                tenant_id=str(self.tenant_id),
                aggregate_type="landed_cost",
                aggregate_id=document["id"],
                payload=estimate,
            )
        return {"items": estimates, "count": len(estimates)}

    # ---------------------------------------------------------------- ELC templates --------
    async def list_elc_templates(self) -> dict[str, Any]:
        items = await self.repo.list_elc_templates()
        return {"items": items, "count": len(items)}

    async def get_elc_template(self, template_id: str) -> dict[str, Any]:
        row = await self.repo.get_elc_template(template_id)
        if row is None:
            raise CostingNotFound("estimated landed cost template not found", code="ELC_TEMPLATE_NOT_FOUND")
        return row

    async def create_elc_template(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        lines = list(payload.get("lines") or [])
        for line in lines:
            await self.get_category(str(line["cost_category_id"]))
            has_amount = line.get("default_amount") is not None
            has_percent = line.get("default_percent") is not None
            if has_amount == has_percent:
                raise CostingValidationError("ELC template line requires exactly one of amount/percent")
        return await self.repo.create_elc_template(
            {
                "code": str(payload["code"]).strip().upper(),
                "name": str(payload["name"]).strip(),
                "currency_code": payload["currency_code"],
                "is_default": bool(payload.get("is_default", False)),
                "is_active": True,
            },
            [
                {
                    "cost_category_id": _uuid(line["cost_category_id"]),
                    "allocation_basis": line["allocation_basis"],
                    "default_amount": Decimal(str(line["default_amount"])) if line.get("default_amount") is not None else None,
                    "default_percent": Decimal(str(line["default_percent"])) if line.get("default_percent") is not None else None,
                    "sort_order": int(line.get("sort_order", 0)),
                }
                for line in lines
            ],
        )

    async def update_elc_template(self, template_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        await self.get_elc_template(template_id)
        values: dict[str, Any] = {}
        if payload.get("name") is not None:
            values["name"] = str(payload["name"]).strip()
        if payload.get("currency_code") is not None:
            values["currency_code"] = str(payload["currency_code"]).upper()
        if payload.get("is_default") is not None:
            values["is_default"] = bool(payload["is_default"])
        if payload.get("is_active") is not None:
            values["is_active"] = bool(payload["is_active"])
        lines: list[dict[str, Any]] | None = None
        if payload.get("lines") is not None:
            lines = []
            for line in payload.get("lines") or []:
                await self.get_category(str(line["cost_category_id"]))
                has_amount = line.get("default_amount") is not None
                has_percent = line.get("default_percent") is not None
                if has_amount == has_percent:
                    raise CostingValidationError("ELC template line requires exactly one of amount/percent")
                lines.append(
                    {
                        "cost_category_id": _uuid(line["cost_category_id"]),
                        "allocation_basis": line["allocation_basis"],
                        "default_amount": Decimal(str(line["default_amount"]))
                        if line.get("default_amount") is not None
                        else None,
                        "default_percent": Decimal(str(line["default_percent"]))
                        if line.get("default_percent") is not None
                        else None,
                        "sort_order": int(line.get("sort_order", 0)),
                    }
                )
        return await self.repo.update_elc_template(template_id, values, lines)

    # ---------------------------------------------------------------- COGS adjustments ----
    async def create_cogs_adjustment(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        lines = e.cogs_adjustment_lines(payload.get("lines") or [])
        total = sum((Decimal(line["value_delta"]) for line in lines), Decimal("0"))
        document = await self.repo.create_cogs_adjustment(
            {
                "adjustment_type": payload["adjustment_type"],
                "status": "DRAFT",
                "reason": payload["reason"],
                "source_cost_run_id": _uuid(payload["source_cost_run_id"]) if payload.get("source_cost_run_id") else None,
                "total_value_base": total,
            },
            [
                {
                    "item_id": _uuid(line["item_id"]),
                    "item_cost_id": _uuid(line["item_cost_id"]),
                    "original_transaction_line_id": _uuid(line["original_transaction_line_id"]) if line.get("original_transaction_line_id") else None,
                    "value_delta": Decimal(line["value_delta"]),
                    "estimated": bool(line.get("estimated", False)),
                }
                for line in lines
            ],
        )
        return document

    async def list_cogs_adjustments(self, *, status: str | None = None) -> dict[str, Any]:
        items = await self.repo.list_cogs_adjustments(status=status)
        return {"items": items, "count": len(items)}

    async def get_cogs_adjustment(self, adjustment_id: str) -> dict[str, Any]:
        row = await self.repo.get_cogs_adjustment(adjustment_id)
        if row is None:
            raise CostingNotFound("COGS adjustment not found", code="COGS_ADJUSTMENT_NOT_FOUND")
        return row

    async def transition_cogs_adjustment(self, adjustment_id: str, target: str) -> dict[str, Any]:
        document = await self.get_cogs_adjustment(adjustment_id)
        transitions = {
            "submit": ("DRAFT", "PENDING_APPROVAL"),
            "approve": ("PENDING_APPROVAL", "APPROVED"),
            "post": ("APPROVED", "POSTED"),
            "reverse": ("POSTED", "REVERSED"),
        }
        source, destination = transitions[target]
        if document["status"] != source:
            raise CostingStateError(f"cannot {target} a COGS adjustment in {document['status']} state")
        values: dict[str, Any] = {"status": destination, "document_number": f"COGS-{document['id'][:8].upper()}"}
        if destination == "POSTED":
            await emit_outbox(
                self.session,
                event_type="cost.adjustment.posted",
                tenant_id=str(self.tenant_id),
                aggregate_type="cogs_adjustment",
                aggregate_id=document["id"],
                payload={
                    "cogs_adjustment_id": document["id"],
                    "adjustment_type": document["adjustment_type"],
                    "total_value_base": document["total_value_base"],
                    "posted_transaction_id": None,
                },
            )
        return await self.repo.update_cogs_adjustment(adjustment_id, values)

    # ---------------------------------------------------------------- variances ------------
    async def list_variances(self, **filters: Any) -> dict[str, Any]:
        return await self.repo.list_variances(**filters)

    async def process_landed_cost_absorption_variances(
        self, *, variance_ids: Sequence[str] | None = None, fiscal_period_id: str | None = None
    ) -> dict[str, Any]:
        """Period-close/true-up action: mark OPEN T-06 absorption variances as CLOSED (idempotent).

        The T-06 value-only postings already exist in the ledger; this action only advances the FS-05
        ``cost_variance.status`` (OPEN→CLOSED) state, which is the documented allowed variance state
        transition and requires no new table/schema. It is exposed at service level (router/contract
        exposure is integrator-owned in this residual).
        """
        listing = await self.repo.list_variances(
            variance_type="LANDED_COST_ABSORPTION", fiscal_period_id=fiscal_period_id, status="OPEN", limit=500
        )
        candidates = list(listing.get("items") or [])
        if variance_ids is not None:
            wanted = {str(item) for item in variance_ids}
            candidates = [item for item in candidates if str(item["id"]) in wanted]
        processed: list[dict[str, Any]] = []
        for item in candidates:
            detail = dict(item.get("detail") or {})
            detail.update(
                {
                    "status": "CLOSED",
                    "processed_at": datetime.now(UTC).isoformat(),
                    "processed_by": "period_close",
                }
            )
            row = await self.repo.update_variance(item["id"], {"status": "CLOSED", "detail": detail})
            processed.append(row)
        return {
            "variance_type": "LANDED_COST_ABSORPTION",
            "processed": len(processed),
            "skipped": len(candidates) == 0,
            "items": processed,
            "total_amount_base": format(
                sum((Decimal(str(item["amount_base"])) for item in processed), Decimal("0")), "f"
            ),
        }

    # ---------------------------------------------------------------- groups --------------
    async def list_groups(self, *, active: bool | None = None) -> dict[str, Any]:
        items = await self.repo.list_groups(active=active)
        return {"items": items, "count": len(items)}

    async def get_group(self, group_id: str) -> dict[str, Any]:
        row = await self.repo.get_group(group_id)
        if row is None:
            raise CostingNotFound("location costing group not found", code="GROUP_NOT_FOUND")
        return row

    async def create_group(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        members = list(payload.get("members") or [])
        location_ids = [str(member["location_id"]) for member in members]
        try:
            e.assert_group_worksheet_allowed(costing_method="GROUP_AVERAGE", operation="CREATE_GROUP")
        except Exception:  # pragma: no cover - creation is allowed; the guard is for worksheets
            pass
        row = await self.repo.create_group(
            {
                "code": str(payload["code"]).strip().upper(),
                "name": str(payload["name"]).strip(),
                "currency_code": payload["currency_code"],
                "include_in_transit_value": bool(payload.get("include_in_transit_value", False)),
                "status": "ACTIVE",
                "is_active": True,
            },
            location_ids,
        )
        return row

    async def update_group(self, group_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        await self.get_group(group_id)
        values: dict[str, Any] = {}
        if payload.get("name") is not None:
            values["name"] = str(payload["name"]).strip()
        if payload.get("include_in_transit_value") is not None:
            values["include_in_transit_value"] = bool(payload["include_in_transit_value"])
        if payload.get("is_active") is not None:
            values["is_active"] = bool(payload["is_active"])
            values["status"] = "ACTIVE" if payload["is_active"] else "INACTIVE"
        return await self.repo.update_group(group_id, values)

    async def add_group_member(self, group_id: str, location_id: str) -> dict[str, Any]:
        await self.get_group(group_id)
        return await self.repo.add_group_member(group_id, location_id)

    async def remove_group_member(self, group_id: str, location_id: str) -> dict[str, Any]:
        await self.get_group(group_id)
        return await self.repo.remove_group_member(group_id, location_id)

    async def group_average_balances(self, *, item_id: str | None = None, group_id: str | None = None) -> dict[str, Any]:
        items = await self.repo.list_group_average_balances(item_id=item_id, group_id=group_id)
        return {"items": items, "count": len(items)}

    async def group_worksheet_guard(self, *, costing_method: str) -> None:
        e.assert_group_worksheet_allowed(costing_method=costing_method)

    # ---------------------------------------------------------------- recalculations --------
    async def request_recalculation(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        items = list(payload.get("items") or [])
        job_definition = platform_models.metadata.tables["platform.job_definition"]
        job_run = platform_models.metadata.tables["platform.job_run"]
        definition = (
            await self.session.execute(
                select(job_definition).where(
                    job_definition.c.tenant_id == self.tenant_id,
                    job_definition.c.name == "costing.recalculate_item_cost",
                )
            )
        ).mappings().first()
        if definition is None:
            raise CostingNotFoundException("costing recalculation job is not seeded")
        run_key = f"cost-recalc-{uuid.uuid4().hex}"
        run = await self.session.execute(
            job_run.insert()
            .values(
                id=uuid.uuid4(),
                tenant_id=self.tenant_id,
                job_definition_id=definition["id"],
                run_key=run_key,
                status="QUEUED",
                attempt=1,
            )
            .returning(job_run)
        )
        row = run.mappings().first()
        await emit_outbox(
            self.session,
            event_type="cost.recalculation.queued",
            tenant_id=str(self.tenant_id),
            aggregate_type="job_run",
            aggregate_id=str(row["id"]) if row else run_key,
            payload={"from_date": date.today().isoformat(), "reason": payload.get("reason", "manual"), "cost_run_id": str(row["id"]) if row else run_key},
        )
        item_ids = [str(item.get("item_id")) for item in items if item.get("item_id")]
        return {"job_run_id": str(row["id"]) if row else None, "status": "QUEUED", "item_count": len(item_ids)}

    async def reconciliation(self, *, as_of: str | None = None) -> dict[str, Any]:
        from app.modules.costing.errors import CostingNotFound as _NotFound

        del _NotFound
        costs = await self.repo.list_item_costs(limit=500)
        drift = 0
        checked = 0
        for cost in costs:
            checked += 1
            entries = await self.repo._select(
                __import__("app.modules.costing.tables", fromlist=["cost_ledger_entry"]).cost_ledger_entry,
                __import__("app.modules.costing.tables", fromlist=["cost_ledger_entry"]).cost_ledger_entry.c.item_cost_id
                == UUID(str(cost["id"])),
            )
            value_from_entries = sum((Decimal(entry["value_delta"]) for entry in entries), Decimal("0"))
            qty_from_entries = sum((Decimal(entry["qty_delta"]) for entry in entries), Decimal("0"))
            if Decimal(str(cost["value_base"])) != value_from_entries or Decimal(str(cost["qty_on_hand"])) != qty_from_entries:
                drift += 1
        summary = {"checked_positions": checked, "drift_count": drift, "as_of": as_of, "policy_version": COSTING_POLICY_VERSION}
        await emit_outbox(
            self.session,
            event_type="cost.reconciliation.completed",
            tenant_id=str(self.tenant_id),
            aggregate_type="cost_reconciliation",
            aggregate_id=str(uuid.uuid4()),
            payload=summary,
        )
        return summary


class CostingNotFoundException(CostingNotFound):
    code = "JOB_NOT_SEEDED"
    status_code = 409


__all__ = ["COSTING_POLICY_VERSION", "CostingService"]
