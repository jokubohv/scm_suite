"""FS-07 counts use-case service: plans, generation, snapshot, capture/recount, approval, T-20 posting.

Business rules live here; the router is a thin transport. The service orchestrates the pure engine
(:mod:`app.modules.counts.engine`), the repository (:mod:`app.modules.counts.repository`), FS-04 balance
reads + posting facade and the transactional outbox. All writes are tenant-scoped and idempotent where
the spec requires it.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.models import platform as platform_models
from app.modules.counts import engine as e
from app.modules.counts.errors import (
    CountAlreadyPosted,
    CountsConflict,
    CountsDisabled,
    CountsNotFound,
    CountStateError,
    CountsValidationError,
)
from app.modules.counts.repository import CountsRepository

COUNTS_POLICY_VERSION = "counts.engine@1.0.0"


def _uuid(value: str | UUID | None, *, field: str = "id") -> UUID:
    try:
        return UUID(str(value))
    except (ValueError, TypeError) as exc:
        raise CountsValidationError(f"{field} must be a UUID") from exc


def _date(value: str | date | None) -> date:
    if isinstance(value, date):
        return value
    if value is None:
        raise CountsValidationError("date is required")
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise CountsValidationError("dates must be ISO-8601") from exc


def _dec(value: Decimal | int | str | None) -> Decimal:
    if value is None:
        return Decimal("0")
    return e.q12(value)


class CountsService:
    def __init__(self, session: AsyncSession, tenant_id: UUID | str) -> None:
        self.session = session
        self.tenant_id = _uuid(tenant_id, field="tenant_id")
        self.repo = CountsRepository(session, tenant_id)

    # ------------------------------------------------------------------ settings ------------
    async def settings(self) -> e.CountSettings:
        table = platform_models.metadata.tables["platform.tenant_setting"]
        keys = [
            "counts.post_on_approve",
            "counts.tolerance_qty",
            "counts.tolerance_percent",
            "counts.auto_approve_within_tolerance",
            "counts.default_variance_account",
            "counts.default_interval_days",
            "counts.abc_class_intervals",
            "counts.draft_ttl_days",
            "counts.require_lot_serial_prerequisite",
        ]
        result = await self.session.execute(
            select(table.c.key, table.c.value).where(
                table.c.tenant_id == self.tenant_id, table.c.namespace == "inventory", table.c.key.in_([f"counts.{key}" for key in keys])
            )
        )
        values = {str(row.key): row.value for row in result.all()}
        flag_table = platform_models.metadata.tables["platform.feature_flag"]
        flag = await self.session.execute(
            select(flag_table.c.is_enabled).where(
                flag_table.c.tenant_id == self.tenant_id, flag_table.c.flag_key == "inventory.counts.enabled"
            )
        )
        enabled = flag.scalar_one_or_none()

        def _bool(key: str, default: bool) -> bool:
            value = values.get(key)
            if value is None:
                return default
            if isinstance(value, bool):
                return value
            return str(value).lower() in {"1", "true", "yes", "on"}

        def _int(key: str, default: int) -> int:
            value = values.get(key)
            return int(value) if value is not None else default

        def _decimal(key: str) -> Decimal:
            value = values.get(key)
            return Decimal(str(value)) if value is not None else Decimal("0")

        intervals = values.get("counts.abc_class_intervals")
        if not isinstance(intervals, Mapping):
            intervals = {"A": 7, "B": 30, "C": 90}
        return e.CountSettings(
            enabled=True if enabled is None else bool(enabled),
            post_on_approve=_bool("counts.post_on_approve", True),
            tolerance_qty=_decimal("counts.tolerance_qty"),
            tolerance_percent=_decimal("counts.tolerance_percent"),
            auto_approve_within_tolerance=_bool("counts.auto_approve_within_tolerance", False),
            default_variance_account=(
                str(values["counts.default_variance_account"])
                if values.get("counts.default_variance_account") is not None
                else None
            ),
            default_interval_days=_int("counts.default_interval_days", 30),
            abc_class_intervals={str(k): int(v) for k, v in dict(intervals).items()},
            draft_ttl_days=_int("counts.draft_ttl_days", 30),
            require_lot_serial_prerequisite=_bool("counts.require_lot_serial_prerequisite", True),
        )

    async def _require_enabled(self) -> e.CountSettings:
        settings = await self.settings()
        if not settings.enabled:
            raise CountsDisabled("inventory counts are disabled for this tenant")
        return settings

    # ------------------------------------------------------------------ plans ----------------
    async def list_plans(self, *, status: str | None = None, frequency: str | None = None, next_run_before: str | None = None, limit: int = 100) -> dict[str, Any]:
        items = await self.repo.list_plans(
            status=status, frequency=frequency,
            next_run_before=_date(next_run_before) if next_run_before else None, limit=limit,
        )
        return {"items": items, "count": len(items), "next_cursor": None}

    async def get_plan(self, plan_id: str) -> dict[str, Any]:
        plan = await self.repo.get_plan(plan_id)
        if plan is None:
            raise CountsNotFound("count plan not found")
        plan["next_count_date"] = e.next_count_date(
            last_count_date=plan["next_run_date"],
            frequency=plan["frequency"],
        )[0].isoformat()
        return plan

    async def create_plan(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        await self._require_enabled()
        code = str(payload.get("code", "")).strip().upper()
        if not code:
            raise CountsValidationError("code is required")
        frequency = str(payload.get("frequency", "MONTHLY")).upper()
        if frequency not in e.FREQUENCIES:
            raise CountsValidationError(f"frequency must be one of {e.FREQUENCIES}")
        filters = [e.FilterSpec(**spec) for spec in payload.get("filters") or []]
        existing = await self.repo.list_plans(limit=500)
        if any(plan["code"].upper() == code for plan in existing):
            raise CountsConflict("count plan code already exists", code="COUNT_PLAN_CODE_TAKEN")
        plan = await self.repo.create_plan(
            {
                "code": code,
                "name": str(payload.get("name", "")).strip(),
                "frequency": frequency,
                "next_run_date": _date(payload.get("next_run_date") or date.today()),
                "status": "ACTIVE",
                "blind_count": bool(payload.get("blind_count", False)),
                "approval_required": bool(payload.get("approval_required", True)),
                "include_zero_qty": bool(payload.get("include_zero_qty", False)),
                "notes": payload.get("notes"),
            },
            [
                {
                    "filter_type": spec.filter_type,
                    "filter_operator": spec.filter_operator,
                    "value_text": spec.value_text,
                    "value_uuid": spec.value_uuid,
                    "sort_order": spec.sort_order,
                }
                for spec in filters
            ],
        )
        return plan

    async def update_plan(self, plan_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        plan = await self.get_plan(plan_id)
        if plan["status"] == "ARCHIVED":
            raise CountStateError("an archived plan cannot be updated")
        values: dict[str, Any] = {}
        for key in ("name", "notes"):
            if payload.get(key) is not None:
                values[key] = str(payload[key])
        for key in ("blind_count", "approval_required", "include_zero_qty"):
            if payload.get(key) is not None:
                values[key] = bool(payload[key])
        if payload.get("frequency") is not None:
            frequency = str(payload["frequency"]).upper()
            if frequency not in e.FREQUENCIES:
                raise CountsValidationError(f"frequency must be one of {e.FREQUENCIES}")
            values["frequency"] = frequency
        if payload.get("next_run_date") is not None:
            values["next_run_date"] = _date(payload["next_run_date"])
        filters = None
        if payload.get("filters") is not None:
            specs = [e.FilterSpec(**spec) for spec in payload["filters"]]
            filters = [
                {
                    "filter_type": spec.filter_type,
                    "filter_operator": spec.filter_operator,
                    "value_text": spec.value_text,
                    "value_uuid": spec.value_uuid,
                    "sort_order": spec.sort_order,
                }
                for spec in specs
            ]
        return await self.repo.update_plan(plan_id, values, filters)

    async def pause_plan(self, plan_id: str, reason: str | None = None) -> dict[str, Any]:
        plan = await self.get_plan(plan_id)
        e.assert_plan_transition(plan["status"], "PAUSED")
        return await self.repo.set_plan_status(plan_id, "PAUSED")

    async def resume_plan(self, plan_id: str) -> dict[str, Any]:
        plan = await self.get_plan(plan_id)
        e.assert_plan_transition(plan["status"], "ACTIVE")
        next_run = max(plan["next_run_date"], date.today().isoformat())
        return await self.repo.set_plan_status(plan_id, "ACTIVE", next_run_date=_date(next_run))

    async def archive_plan(self, plan_id: str, reason: str | None = None) -> dict[str, Any]:
        await self.get_plan(plan_id)
        open_counts = await self.repo.list_counts(status="OPEN")
        if any(str(count.get("count_plan_id")) == plan_id for count in open_counts):
            raise CountsConflict("plan has open counts and cannot be archived", code="COUNT_PLAN_OPEN_COUNTS")
        return await self.repo.set_plan_status(plan_id, "ARCHIVED")

    # ------------------------------------------------------------------ generation -----------
    async def generate_from_plan(self, plan_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        settings = await self._require_enabled()
        plan = await self.get_plan(plan_id)
        if plan["status"] != "ACTIVE":
            raise CountStateError("only ACTIVE plans generate counts")
        count_date = _date(payload.get("count_date") or date.today())
        item_ids = [str(item) for item in payload.get("item_ids") or []]
        positions = await self.repo.load_positions(
            item_ids=item_ids,
            location_ids=[str(spec["value_uuid"]) for spec in plan["filters"] if spec["filter_type"] == "LOCATION" and spec["value_uuid"]],
            status_ids=[str(spec["value_uuid"]) for spec in plan["filters"] if spec["filter_type"] == "STATUS" and spec["value_uuid"]],
            include_bins=any(spec["filter_type"] == "BIN" for spec in plan["filters"]),
        )
        filters = [
            e.FilterSpec(
                filter_type=spec["filter_type"],
                filter_operator=spec["filter_operator"],
                value_text=spec.get("value_text"),
                value_uuid=str(spec["value_uuid"]) if spec.get("value_uuid") else None,
                sort_order=int(spec.get("sort_order", 0)),
            )
            for spec in plan["filters"]
        ]
        selected = e.select_positions(
            positions, filters=filters,
            include_zero_qty=bool(plan["include_zero_qty"]), max_lines=payload.get("max_lines"),
        )
        if item_ids:
            wanted = {str(item) for item in item_ids}
            selected = [position for position in selected if position.item_id in wanted]
        if not selected:
            raise CountsConflict("plan scope resolved to no positions", code="COUNT_SCOPE_EMPTY")
        count = await self._create_count_document(
            plan=plan,
            count_type="CYCLE",
            count_date=count_date,
            blind=bool(plan["blind_count"]),
            approval_required=bool(plan["approval_required"]),
            assignee_id=payload.get("assignee_id"),
            variance_account_ref=payload.get("variance_account_ref"),
            scope={"filters": [spec.model_dump() if hasattr(spec, "model_dump") else spec for spec in plan["filters"]], "item_ids": item_ids},
            line_count=len(selected),
        )
        frequency = plan["frequency"]
        next_run, _, _ = e.next_count_date(last_count_date=count_date, frequency=frequency, settings=settings)
        await self.repo.set_plan_status(plan_id, plan["status"], next_run_date=next_run, last_run_at=datetime.now(UTC))
        await self.repo.emit(
            "count.generated",
            aggregate_id=count["id"],
            payload={
                "count_id": count["id"], "count_number": count["count_number"], "count_type": count["count_type"],
                "count_plan_id": plan_id, "count_date": count_date.isoformat(), "line_count": len(selected),
            },
        )
        return e.count_ref(count)

    async def create_count(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        await self._require_enabled()
        count_type = str(payload.get("count_type", "MANUAL")).upper()
        if count_type not in e.COUNT_TYPES:
            raise CountsValidationError(f"count_type must be one of {e.COUNT_TYPES}")
        scope = dict(payload.get("scope") or {})
        count_date = _date(payload.get("count_date") or date.today())
        item_ids = [str(item) for item in scope.get("items") or []]
        location_ids = [str(location) for location in scope.get("locations") or []]
        status_ids = [str(status) for status in scope.get("statuses") or []]
        if count_type == "MANUAL" and not (item_ids or location_ids):
            raise CountsValidationError("manual count requires an explicit item or location scope")
        if count_type == "CALCULATED" and not scope:
            raise CountsValidationError("calculated count requires scope inputs (items/locations/abc)")
        positions = await self.repo.load_positions(
            item_ids=item_ids, location_ids=location_ids, status_ids=status_ids,
            include_bins=bool(scope.get("bins")),
        )
        abc = {str(value).upper() for value in scope.get("abc") or []}
        if abc:
            positions = [position for position in positions if position.abc_class in abc]
        if item_ids:
            wanted = {str(item) for item in item_ids}
            positions = [position for position in positions if position.item_id in wanted]
        if not positions:
            raise CountsConflict("count scope resolved to no positions", code="COUNT_SCOPE_EMPTY")
        blind = bool(payload.get("blind_count", False))
        approval_required = bool(payload.get("approval_required", True))
        count = await self._create_count_document(
            plan=None, count_type=count_type, count_date=count_date,
            blind=blind, approval_required=approval_required, assignee_id=None,
            variance_account_ref=payload.get("variance_account_ref"), scope=scope, line_count=len(positions),
        )
        await self.repo.emit(
            "count.generated",
            aggregate_id=count["id"],
            payload={
                "count_id": count["id"], "count_number": count["count_number"], "count_type": count_type,
                "count_plan_id": None, "count_date": count_date.isoformat(), "line_count": len(positions),
            },
        )
        return e.count_ref(count)

    async def _create_count_document(
        self, *, plan: Mapping[str, Any] | None, count_type: str, count_date: date,
        blind: bool, approval_required: bool, assignee_id: str | None,
        variance_account_ref: str | None, scope: Mapping[str, Any], line_count: int,
    ) -> dict[str, Any]:
        count_number = await self._next_count_number()
        count = await self.repo.create_count(
            {
                "count_number": count_number,
                "count_plan_id": plan["id"] if plan else None,
                "count_type": count_type,
                "status": "DRAFT",
                "count_date": count_date,
                "blind_count": blind,
                "approval_required": approval_required,
                "variance_account_ref": variance_account_ref,
                "count_round": 1,
                "scope": {**dict(scope), "expected_lines": line_count},
                "created_by": _uuid(assignee_id) if assignee_id else None,
            }
        )
        # DRAFT counts hold no snapshot or lines; start captures them.
        return count

    async def _next_count_number(self) -> str:
        table = platform_models.metadata.tables["platform.number_sequence"]
        row = (
            await self.session.execute(
                select(table).where(
                    table.c.tenant_id == self.tenant_id, table.c.doc_type == "CNT"
                ).with_for_update()
            )
        ).mappings().first()
        if row is not None:
            value = int(row["next_value"])
            await self.session.execute(
                table.update().where(table.c.id == row["id"]).values(next_value=value + 1, version=table.c.version + 1)
            )
            return f"{row['prefix']}{value:0{int(row['padding'])}d}"
        existing = await self.repo.list_counts(limit=1000)
        return f"CNT-{len(existing) + 1:06d}"

    # ------------------------------------------------------------------ snapshot / start -----
    async def start_count(self, count_id: str) -> dict[str, Any]:
        await self._require_enabled()
        count = await self._get_count(count_id)
        e.assert_count_transition(count["status"], "OPEN")
        if count["status"] != "DRAFT":
            raise CountStateError("count has already been started")
        scope = dict(count.get("scope") or {})
        positions = await self.repo.load_positions(
            item_ids=[str(item) for item in (scope.get("item_ids") or scope.get("items") or [])],
            location_ids=[str(location) for location in (scope.get("locations") or [])],
            include_bins=False,
        )
        if scope.get("expected_lines") and not positions:
            raise CountsConflict("no on-hand positions available for the count", code="COUNT_SCOPE_EMPTY")
        if not positions:
            raise CountsConflict("no on-hand positions available for the count", code="COUNT_SCOPE_EMPTY")
        snapshot_at = datetime.now(UTC)
        snapshot_rows = e.build_snapshot_rows(
            inventory_count_id=count_id, positions=positions, snapshot_at=snapshot_at
        )
        await self.repo.insert_snapshots([row.as_values(self.tenant_id) for row in snapshot_rows])
        line_drafts = e.build_count_lines(inventory_count_id=count_id, positions=positions)
        await self.repo.insert_lines([draft.as_values(self.tenant_id) for draft in line_drafts])
        updated = await self.repo.update_count(
            count_id,
            {"status": "OPEN", "snapshot_at": snapshot_at, "recalc_snapshot_on_reject": True},
        )
        return {"status": updated["status"], "snapshot_at": updated["snapshot_at"], "line_count": len(line_drafts)}

    async def _get_count(self, count_id: str) -> dict[str, Any]:
        count = await self.repo.get_count(count_id)
        if count is None:
            raise CountsNotFound("inventory count not found")
        return count

    # ------------------------------------------------------------------ capture / recount -----
    async def list_lines(self, count_id: str, **filters: Any) -> dict[str, Any]:
        count = await self._get_count(count_id)
        blind = bool(count["blind_count"]) and count["status"] not in {"PENDING_REVIEW", "APPROVED", "POSTED"}
        items = await self.repo.list_lines(count_id, **filters)
        shaped = e.shape_lines(items, blind=blind)
        return {"items": shaped, "count": len(shaped), "blind": blind}

    async def capture_lines(self, count_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        await self._require_enabled()
        count = await self._get_count(count_id)
        if count["status"] not in {"OPEN", "COUNTING"}:
            raise CountStateError("lines can only be captured on OPEN/COUNTING counts")
        lines = []
        for entry in payload.get("lines") or []:
            line = await self._resolve_line(count_id, entry)
            counted = _dec(entry.get("counted_qty"))
            if counted < 0:
                raise CountsValidationError("counted_qty must be non-negative")
            if line.get("serial_id") is not None and counted > 1:
                raise CountsValidationError("serial count line counted_qty must be 0 or 1")
            lines.append((line, counted, entry.get("notes")))
        for line, counted, notes in lines:
            e.assert_line_transition(line["count_status"], "COUNTED")
            await self.repo.update_line(
                line["id"],
                {"counted_qty": counted, "count_status": "COUNTED", "counted_at": datetime.now(UTC), "notes": notes},
            )
        if count["status"] == "OPEN":
            await self.repo.update_count(count_id, {"status": "COUNTING"})
        blind = bool(count["blind_count"])
        accepted = [{"line_id": line["id"], "count_status": "COUNTED"} for line, _, _ in lines]
        return {"accepted": accepted, "rejected": [], "blind": blind}

    async def _resolve_line(self, count_id: str, entry: Mapping[str, Any]) -> dict[str, Any]:
        if entry.get("line_id"):
            line = await self.repo.get_line(str(entry["line_id"]))
            if line is None or str(line["inventory_count_id"]) != count_id:
                raise CountsNotFound("count line not found")
            return line
        position = entry.get("position") or {}
        lines = await self.repo.list_lines(count_id)
        for line in lines:
            if all(
                str(line.get(field)) == str(position.get(field))
                for field in ("item_id", "location_id", "bin_id", "lot_id", "serial_id", "inventory_status_id", "ownership")
                if position.get(field) is not None
            ):
                return line
        raise CountsNotFound("count line position not found")

    async def recount(self, count_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        await self._require_enabled()
        count = await self._get_count(count_id)
        if count["status"] not in {"COUNTING", "PENDING_REVIEW", "REJECTED"}:
            raise CountStateError("recount is only allowed before posting on COUNTING/PENDING_REVIEW/REJECTED counts")
        existing = await self.repo.list_recount(count_id)
        attempts: dict[str, int] = {}
        for row in existing:
            attempts[str(row["inventory_count_line_id"])] = max(attempts.get(str(row["inventory_count_line_id"]), 0), int(row["attempt"]))
        refs: list[dict[str, Any]] = []
        for entry in payload.get("lines") or []:
            line = await self._resolve_line(count_id, entry)
            counted = _dec(entry.get("counted_qty"))
            if counted < 0:
                raise CountsValidationError("recount quantity must be non-negative")
            attempt = attempts.get(line["id"], 0) + 1
            await self.repo.insert_recount(
                {
                    "inventory_count_line_id": line["id"], "attempt": attempt, "counted_qty": counted,
                    "counted_by": _uuid(entry.get("counted_by")) if entry.get("counted_by") else None,
                    "variance_qty": e.line_variance(counted, line["snapshot_qty"]), "notes": entry.get("notes"),
                }
            )
            await self.repo.update_line(
                line["id"], {"counted_qty": counted, "count_status": "COUNTED", "counted_at": datetime.now(UTC)}
            )
            refs.append({"line_id": line["id"], "attempt": attempt})
            await self.repo.emit(
                "count.recounted", aggregate_id=count_id,
                payload={"count_id": count_id, "line_id": line["id"], "attempt": attempt, "counted_qty": e.qty_str(counted)},
            )
        if count["status"] == "REJECTED":
            await self.repo.update_count(count_id, {"status": "COUNTING"})
        return {"status": "COUNTED", "recounts": refs}

    # ------------------------------------------------------------------ review / approve -----
    async def submit(self, count_id: str) -> dict[str, Any]:
        await self._require_enabled()
        count = await self._get_count(count_id)
        settings = await self.settings()
        e.assert_count_transition(count["status"], "PENDING_REVIEW")
        if count["status"] not in {"COUNTING", "PENDING_REVIEW"}:
            raise CountStateError("only a COUNTING count can be submitted")
        lines = await self.repo.list_lines(count_id)
        uncounted = [line for line in lines if line["counted_qty"] is None]
        if uncounted:
            raise CountsConflict("all lines must be counted before submit", code="COUNT_LINES_INCOMPLETE", details={"uncounted": len(uncounted)})
        tolerance = e.evaluate_tolerance(lines, settings)
        updated = await self.repo.update_count(count_id, {"status": "PENDING_REVIEW"})
        return {"status": updated["status"], "approval_request_id": None, "tolerance": tolerance.as_dict()}

    async def approve(self, count_id: str, payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
        settings = await self._require_enabled()
        count = await self._get_count(count_id)
        e.assert_count_transition(count["status"], "APPROVED")
        if count["status"] != "PENDING_REVIEW":
            raise CountStateError("only a PENDING_REVIEW count can be approved")
        lines = await self.repo.list_lines(count_id)
        actor = (payload or {}).get("approved_by")
        updated = await self.repo.update_count(
            count_id,
            {"status": "APPROVED", "approved_at": datetime.now(UTC), "approved_by": _uuid(actor) if actor else None},
        )
        totals = e.variance_totals(lines)
        await self.repo.emit(
            "count.approved", aggregate_id=count_id,
            payload={
                "count_id": count_id, "approved_by": str(actor) if actor else None,
                "approved_at": updated["approved_at"], "line_count": len(lines),
                "variance_qty_total": e.qty_str(totals["variance_qty_total"]),
            },
        )
        if settings.post_on_approve:
            posted = await self.post_variance(count_id)
            return {"status": posted["status"], "posted_transaction_id": posted.get("posted_transaction_id"), "auto_posted": True}
        return {"status": updated["status"], "posted_transaction_id": None, "auto_posted": False}

    async def reject(self, count_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        await self._require_enabled()
        count = await self._get_count(count_id)
        e.assert_count_transition(count["status"], "REJECTED")
        if count["status"] not in {"PENDING_REVIEW", "APPROVED"}:
            raise CountStateError("only PENDING_REVIEW/APPROVED counts can be rejected")
        reason = str(payload.get("reason", "")).strip()
        if not reason:
            raise CountsValidationError("rejection reason is required")
        recalc = bool(payload.get("recalc_snapshot", count.get("recalc_snapshot_on_reject", True)))
        updated = await self.repo.update_count(
            count_id, {"status": "REJECTED", "reason": reason, "count_round": int(count["count_round"]) + 1}
        )
        snapshot_revision = None
        if recalc:
            snapshot_revision = await self.recalculate_snapshot(count_id, {"reason": reason}, status_override="REJECTED")
        await self.repo.emit(
            "count.rejected", aggregate_id=count_id,
            payload={"count_id": count_id, "reason": reason, "recalc_snapshot": recalc},
        )
        return {"status": updated["status"], "snapshot_revision": snapshot_revision}

    async def recalculate_snapshot(self, count_id: str, payload: Mapping[str, Any], *, status_override: str | None = None) -> int:
        count = await self._get_count(count_id)
        if count["status"] in {"POSTED", "CANCELLED"}:
            raise CountStateError("posted/cancelled counts cannot recalculate their snapshot")
        lines = await self.repo.list_lines(count_id)
        old_snapshots = await self.repo.list_snapshots(count_id)
        if not old_snapshots:
            raise CountsConflict("count has no snapshot revision")
        latest_by_position: dict[tuple[Any, ...], dict[str, Any]] = {}
        for row in old_snapshots:
            key = (row["item_id"], row["location_id"], row.get("bin_id"), row.get("lot_id"), row.get("serial_id"), row["inventory_status_id"], row.get("ownership"))
            current = latest_by_position.get(key)
            if current is None or str(row["snapshot_at"]) >= str(current["snapshot_at"]):
                latest_by_position[key] = row
        positions = await self.repo.load_positions(include_bins=False)
        current_by_key = {position.key: position for position in positions}
        snapshot_at = datetime.now(UTC)
        recalculated_from_id = str(latest_by_position[next(iter(latest_by_position))]["id"]) if latest_by_position else None
        new_rows: list[dict[str, Any]] = []
        for line in lines:
            key = (line["item_id"], line["location_id"], line.get("bin_id"), line.get("lot_id"), line.get("serial_id"), line["inventory_status_id"], line.get("ownership"))
            position = current_by_key.get(key)
            new_qty = position.qty_on_hand if position is not None else e.q12(line["snapshot_qty"])
            new_value = position.value_base if position is not None else None
            snapshot_row = e.SnapshotRow(
                inventory_count_id=count_id,
                position=e.CountPosition(
                    item_id=line["item_id"], location_id=line["location_id"],
                    inventory_status_id=line["inventory_status_id"], uom_id=line["uom_id"],
                    qty_on_hand=new_qty, value_base=new_value if new_value is not None else Decimal("0"),
                    bin_id=line.get("bin_id"), lot_id=line.get("lot_id"), serial_id=line.get("serial_id"),
                    ownership=line.get("ownership", "OWNED"),
                ),
                snapshot_at=snapshot_at,
                recalculated_from_id=recalculated_from_id,
            )
            new_rows.append(snapshot_row.as_values(self.tenant_id))
        await self.repo.insert_snapshots(new_rows)
        for line in lines:
            key = (line["item_id"], line["location_id"], line.get("bin_id"), line.get("lot_id"), line.get("serial_id"), line["inventory_status_id"], line.get("ownership"))
            position = current_by_key.get(key)
            if position is not None:
                await self.repo.update_line(line["id"], {"snapshot_qty": position.qty_on_hand})
        revision = len({str(row["snapshot_at"]) for row in old_snapshots}) + 1
        if status_override is None:
            await self.repo.update_count(count_id, {"count_round": int(count["count_round"]) + 1})
        await self.repo.emit(
            "count.snapshot.recalculated", aggregate_id=count_id,
            payload={"count_id": count_id, "snapshot_revision": revision, "recalculated_from_id": recalculated_from_id},
        )
        return revision

    # ------------------------------------------------------------------ posting ---------------
    async def post_variance(self, count_id: str) -> dict[str, Any]:
        settings = await self._require_enabled()
        count = await self._get_count(count_id)
        if count["status"] == "POSTED":
            raise CountAlreadyPosted(
                "count variance has already been posted",
                details={"count_id": count_id, "posted_transaction_id": count.get("posted_transaction_id")},
            )
        e.assert_count_transition(count["status"], "POSTED")
        if count["status"] != "APPROVED":
            raise CountStateError("only an APPROVED count can post its variance")
        lines = await self.repo.list_lines(count_id)
        methods = await self.repo.item_costing_methods([line["item_id"] for line in lines])
        group_average_items = [item_id for item_id, method in methods.items() if str(method).upper() == "GROUP_AVERAGE"]
        e.assert_postable_lines(lines, group_average_item_ids=group_average_items)
        movements = e.build_t20_movements(count=count, lines=lines)
        if not movements:
            await self.repo.update_count(count_id, {"status": "POSTED"})
            return {"status": "POSTED", "posted_transaction_id": None, "adjustment_links": [], "line_count": 0}
        result = await self.repo.post_count_variance(count, lines, settings)
        transaction_id = result.get("transaction_id")
        updated = await self.repo.update_count(count_id, {"status": "POSTED", "posted_transaction_id": transaction_id})
        links: list[dict[str, Any]] = []
        if transaction_id:
            await self.repo.document_links(count_id, str(transaction_id))
            links.append({"target_type": "INVENTORY_TRANSACTION", "target_id": str(transaction_id)})
        totals = e.variance_totals(lines)
        await self.repo.emit(
            "count.variance_posted", aggregate_id=count_id,
            payload={
                "count_id": count_id, "inventory_transaction_id": str(transaction_id) if transaction_id else None,
                "ledger_sequence": result.get("ledger_sequence"), "line_count": len(movements),
                "variance_value_base": result.get("total_value_base") or e.qty_str(totals["variance_value_base"]),
            },
        )
        return {
            "status": updated["status"],
            "posted_transaction_id": str(transaction_id) if transaction_id else None,
            "adjustment_links": links,
            "line_count": len(movements),
        }

    # ------------------------------------------------------------------ reads ----------------
    async def list_counts(self, **filters: Any) -> dict[str, Any]:
        items = await self.repo.list_counts(**filters)
        return {"items": items, "count": len(items), "next_cursor": None}

    async def get_count(self, count_id: str, *, include_lines: bool = False) -> dict[str, Any]:
        count = await self._get_count(count_id)
        blind = bool(count["blind_count"]) and count["status"] not in {"PENDING_REVIEW", "APPROVED", "POSTED"}
        result = dict(count)
        if blind:
            result.pop("variance_account_ref", None)
        if include_lines:
            lines = await self.repo.list_lines(count_id)
            result["lines"] = e.shape_lines(lines, blind=blind)
            result["blind"] = blind
        return result

    async def variance_preview(self, count_id: str) -> dict[str, Any]:
        count = await self._get_count(count_id)
        blind = bool(count["blind_count"]) and count["status"] not in {"PENDING_REVIEW", "APPROVED", "POSTED"}
        lines = await self.repo.list_lines(count_id)
        snapshots = {str(row["item_id"]): row for row in await self.repo.list_snapshots(count_id)}
        items = []
        for line in lines:
            item = dict(line)
            snapshot = snapshots.get(str(line["item_id"]))
            item["snapshot_value_base"] = snapshot["snapshot_value_base"] if snapshot else "0"
            items.append(e.blind_line(item) if blind else item)
        totals = e.variance_totals(lines)
        return {"items": items, "totals": {key: e.qty_str(value) for key, value in totals.items()}, "blind": blind}

    async def snapshot(self, count_id: str, *, revision: int | None = None) -> dict[str, Any]:
        await self._get_count(count_id)
        rows = await self.repo.list_snapshots(count_id)
        if revision is not None:
            revisions = sorted({str(row["snapshot_at"]) for row in rows})
            if revision < 1 or revision > len(revisions):
                raise CountsNotFound("snapshot revision not found")
            rows = [row for row in rows if str(row["snapshot_at"]) == revisions[revision - 1]]
        return {"items": rows, "revision_count": len({str(row["snapshot_at"]) for row in rows}), "count": len(rows)}

    async def next_count(self, item_id: str, location_id: str | None = None) -> dict[str, Any]:
        settings = await self.settings()
        last = await self.repo.last_posted_count(item_id=item_id, location_id=location_id)
        plans = await self.repo.list_plans(status="ACTIVE", limit=500)
        frequency = plans[0]["frequency"] if plans else None
        next_date, interval, source = e.next_count_date(
            last_count_date=last, frequency=frequency, settings=settings
        )
        return {"next_count_date": next_date.isoformat(), "interval_days": interval, "source": source}

    # ------------------------------------------------------------------ jobs ------------------
    async def run_cycle_generation(self, *, run_date: date | None = None) -> dict[str, Any]:
        as_of = run_date or date.today()
        due = await self.repo.due_plans(as_of=as_of)
        generated: list[dict[str, Any]] = []
        for plan in due:
            try:
                ref = await self.generate_from_plan(plan["id"], {"count_date": as_of.isoformat()})
                generated.append(ref)
            except Exception as exc:  # noqa: BLE001 - a failed plan must not block the batch
                generated.append({"count_plan_id": plan["id"], "error": str(exc)})
        return {"run_date": as_of.isoformat(), "plans": len(due), "generated": generated}

    async def stale_cleanup(self, *, dry_run: bool = True) -> dict[str, Any]:
        settings = await self.settings()
        counts = await self.repo.list_counts(status="DRAFT", limit=500)
        cutoff = date.today().toordinal() - settings.draft_ttl_days
        stale = [
            count for count in counts
            if date.fromisoformat(str(count["count_date"])).toordinal() < cutoff
        ]
        cancelled = 0
        if not dry_run:
            for count in stale:
                await self.repo.update_count(count["id"], {"status": "CANCELLED", "reason": "stale DRAFT cleanup"})
                cancelled += 1
        return {"checked": len(counts), "stale": len(stale), "cancelled": cancelled, "dry_run": dry_run}


__all__ = ["COUNTS_POLICY_VERSION", "CountsService"]
