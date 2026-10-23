"""Declarative approval lifecycle service (FS-00).

Implements the ``approvals.submit`` / ``approvals.decide`` / ``approvals.status`` contracts from
``feature-sets/FS-00.md`` §5: highest-priority enabled definition matching by condition, request+step
state, sequential and parallel steps, requester≠approver SoD, step authorization, delegate/recall,
terminal-state guards, append-only actions, audit + ``platform.approval.*`` outbox events and
idempotent decision replay.

Steps are stored in ``platform.approval_definition.steps`` (JSONB); the schema is flexible by design:
a list of step objects (``step``/``name``/``mode``/``roles``/``user_ids``/``approvers``/``min_approvals``)
or ``{"steps": [...]}``. No step table exists in migrations 0001–0003, so step progress is derived from
the request's ``current_step`` plus append-only ``approval_action`` rows (actions are the audit source
of truth; request columns are the projection).

Async SQLAlchemy Core only; no commits (caller owns the transaction)."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from fastapi import HTTPException
from sqlalchemy import and_, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.models import platform as p
from app.core.platform_events import audit_recorded, emit_platform_event
from app.core.security.errors import SodConflict

APPROVAL_DECISIONS = frozenset({"APPROVE", "REJECT", "DELEGATE", "RECALL"})
TERMINAL_STATUSES = frozenset({"APPROVED", "REJECTED", "CANCELLED"})
EVENT_VERSION = "1.0.0"
IDEMPOTENCY_TTL = timedelta(hours=24)


# --------------------------------------------------------------------------- pure helpers ------
@dataclass(frozen=True, slots=True)
class StepSpec:
    """Normalized approval step (pure data; safe to unit-test)."""

    step: int
    name: str = ""
    mode: str = "SEQUENTIAL"
    roles: frozenset[str] = frozenset()
    user_ids: frozenset[str] = frozenset()
    min_approvals: int | None = None

    @property
    def required_approvals(self) -> int:
        if self.min_approvals is not None:
            return max(1, self.min_approvals)
        if self.mode == "PARALLEL" and self.user_ids:
            return len(self.user_ids)
        return 1

    def as_dict(self) -> dict[str, Any]:
        return {
            "step": self.step,
            "name": self.name,
            "mode": self.mode,
            "roles": sorted(self.roles),
            "user_ids": sorted(self.user_ids),
            "required_approvals": self.required_approvals,
        }


def _ids(value: Any) -> frozenset[str]:
    if value is None:
        return frozenset()
    if isinstance(value, str):
        return frozenset({value})
    return frozenset(str(item) for item in value)


def normalize_steps(raw: Any) -> tuple[StepSpec, ...]:
    """Normalize a definition's ``steps`` JSONB into ordered :class:`StepSpec` objects."""
    if raw is None:
        return ()
    if isinstance(raw, Mapping):
        raw = raw.get("steps", ())
    if not isinstance(raw, Sequence) or isinstance(raw, str | bytes):
        raise ValueError("approval steps must be a list")
    steps: list[StepSpec] = []
    for index, item in enumerate(raw, start=1):
        if not isinstance(item, Mapping):
            raise ValueError("approval step must be an object")
        mode = str(item.get("mode", "SEQUENTIAL")).upper()
        if mode not in {"SEQUENTIAL", "PARALLEL"}:
            raise ValueError(f"invalid step mode {mode!r}")
        roles = _ids(item.get("roles"))
        users = _ids(item.get("user_ids") or item.get("approvers") or item.get("approver_ids"))
        min_approvals = item.get("min_approvals")
        steps.append(
            StepSpec(
                step=int(item.get("step", index)),
                name=str(item.get("name", "")),
                mode=mode,
                roles=roles,
                user_ids=users,
                min_approvals=int(min_approvals) if min_approvals is not None else None,
            )
        )
    steps.sort(key=lambda spec: spec.step)
    return tuple(steps)


def _compare(actual: Any, expected: Any) -> bool:
    if isinstance(expected, Mapping):
        for op, target in expected.items():
            if op in {"gte", "ge", "min"} and not (actual is not None and actual >= target):
                return False
            if op in {"lte", "le", "max"} and not (actual is not None and actual <= target):
                return False
            if op in {"gt", "min_exclusive"} and not (actual is not None and actual > target):
                return False
            if op in {"lt", "max_exclusive"} and not (actual is not None and actual < target):
                return False
            if op in {"eq", "equals"} and actual != target:
                return False
            if op in {"ne", "not_equals"} and actual == target:
                return False
            if op == "in" and actual not in target:
                return False
            if op == "not_in" and actual in target:
                return False
        return True
    return actual == expected


def match_condition(condition: Mapping[str, Any] | None, context: Mapping[str, Any] | None) -> bool:
    """True when every condition key matches the context (empty/None condition always matches)."""
    if not condition:
        return True
    context = context or {}
    for key, expected in condition.items():
        if key not in context:
            return False
        if not _compare(context[key], expected):
            return False
    return True


def is_terminal(status: str) -> bool:
    return status in TERMINAL_STATUSES


def required_approvals_for(step: StepSpec) -> int:
    return step.required_approvals


def step_is_authorized(step: StepSpec, actor_id: str, actor_roles: Iterable[str]) -> bool:
    """A step with no actor restrictions accepts any approval-capable actor (scope checked by API)."""
    roles = set(actor_roles)
    if step.user_ids or step.roles:
        return actor_id in step.user_ids or bool(step.roles & roles)
    return True


def step_decision_state(
    step: StepSpec,
    *,
    approvals: int,
    rejects: int,
    delegated: bool,
    already_decided: bool,
) -> str:
    """Pure state of one step given its collected decisions: APPROVED|REJECTED|DELEGATED|PENDING."""
    if rejects:
        return "REJECTED"
    if approvals >= required_approvals_for(step):
        return "APPROVED"
    if delegated:
        return "DELEGATED"
    return "PENDING"


def next_position(total_steps: int, current_step: int) -> int | None:
    """Next step after a fully-approved step, or ``None`` when the request is fully approved."""
    if current_step >= total_steps:
        return None
    return current_step + 1


# --------------------------------------------------------------------------- serialization -----
def _ser(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _ser(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_ser(item) for item in value]
    return value


def _row(row: Any) -> dict[str, Any]:
    mapping = getattr(row, "_mapping", row)
    return _ser(dict(mapping))


def _json_safe(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_json_safe(item) for item in value]
    return value


def request_hash(payload: Mapping[str, Any]) -> str:
    canonical = json.dumps(_json_safe(payload), sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- errors ------------
def _not_found(request_id: str) -> HTTPException:
    return HTTPException(status_code=404, detail={"code": "APPROVAL_NOT_FOUND", "message": "approval request not found", "details": {"request_id": request_id}})


def _terminal(status: str) -> HTTPException:
    return HTTPException(status_code=409, detail={"code": "APPROVAL_TERMINAL", "message": f"approval request is already {status}", "details": {"status": status}})


def _bad_step(message: str, **details: Any) -> HTTPException:
    return HTTPException(status_code=403, detail={"code": "STEP_ACTOR_UNAUTHORIZED", "message": message, "details": details})


# --------------------------------------------------------------------------- service -----------
class ApprovalService:
    """Lifecycle service bound to one async session/transaction."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # ------------------------------------------------------------------ reads ---------------
    async def _load_request(self, tenant_id: str, request_id: str) -> dict[str, Any] | None:
        result = await self.session.execute(
            select(p.approval_request).where(
                p.approval_request.c.tenant_id == tenant_id, p.approval_request.c.id == request_id
            )
        )
        row = result.mappings().first()
        return _row(row) if row else None

    async def _load_definition(self, tenant_id: str, definition_id: str | None) -> dict[str, Any] | None:
        if not definition_id:
            return None
        result = await self.session.execute(
            select(p.approval_definition).where(
                p.approval_definition.c.tenant_id == tenant_id, p.approval_definition.c.id == definition_id
            )
        )
        row = result.mappings().first()
        return _row(row) if row else None

    async def _actions(self, tenant_id: str, request_id: str) -> list[dict[str, Any]]:
        result = await self.session.execute(
            select(p.approval_action)
            .where(p.approval_action.c.tenant_id == tenant_id, p.approval_action.c.approval_request_id == request_id)
            .order_by(p.approval_action.c.decided_at, p.approval_action.c.id)
        )
        return [_row(row) for row in result]

    async def _actor_roles(self, tenant_id: str, actor_id: str) -> list[str]:
        result = await self.session.execute(
            select(p.role.c.code)
            .select_from(
                p.app_user.join(
                    p.user_role,
                    and_(p.user_role.c.tenant_id == p.app_user.c.tenant_id, p.user_role.c.user_id == p.app_user.c.id),
                ).join(
                    p.role,
                    and_(p.role.c.tenant_id == p.user_role.c.tenant_id, p.role.c.id == p.user_role.c.role_id),
                )
            )
            .where(
                p.app_user.c.tenant_id == tenant_id,
                p.app_user.c.id == actor_id,
                p.app_user.c.membership_status == "ACTIVE",
                p.user_role.c.effective_to.is_(None),
            )
        )
        return sorted({str(code) for code in result.scalars().all()})

    async def _actor_is_member(self, tenant_id: str, actor_id: str) -> bool:
        result = await self.session.execute(
            select(func.count()).select_from(p.app_user).where(
                p.app_user.c.tenant_id == tenant_id,
                p.app_user.c.id == actor_id,
                p.app_user.c.membership_status == "ACTIVE",
            )
        )
        return int(result.scalar_one()) > 0

    async def get_request(self, tenant_id: str, request_id: str) -> dict[str, Any] | None:
        return await self._load_request(tenant_id, request_id)

    # ------------------------------------------------------------------ events/audit --------
    async def _audit(self, tenant_id: str, actor_id: str | None, action: str, request_id: str, diff: Mapping[str, Any]) -> None:
        event_id = uuid4()
        await self.session.execute(
            p.audit_event.insert().values(
                id=event_id, tenant_id=tenant_id, event_id=event_id, actor_type="USER",
                actor_id=actor_id, action=action, resource_type="approval_request", resource_id=request_id,
                diff=_json_safe(dict(diff)), source="API",
            )
        )
        await audit_recorded(
            self.session, tenant_id=tenant_id, event_id=str(event_id), actor_type="USER", action=action,
            resource_type="approval_request", resource_id=request_id,
            occurred_at=datetime.now(UTC).isoformat(),
        )

    async def _outbox(self, tenant_id: str, event_type: str, request: Mapping[str, Any], data: Mapping[str, Any]) -> None:
        event_id = uuid4()
        occurred_at = datetime.now(UTC)
        envelope = {
            "event_id": str(event_id),
            "event_type": event_type,
            "event_version": EVENT_VERSION,
            "tenant_id": str(tenant_id),
            "occurred_at": occurred_at.isoformat(),
            "aggregate": {"type": "approval_request", "id": str(request["id"]), "version": max(1, int(request.get("version", 1)))},
            "data": _json_safe(dict(data)),
        }
        await self.session.execute(
            p.outbox_event.insert().values(
                id=event_id, tenant_id=tenant_id, event_id=event_id,
                aggregate_type="approval_request", aggregate_id=request["id"],
                aggregate_version=max(1, int(request.get("version", 1))),
                event_type=event_type, event_version=EVENT_VERSION, payload=envelope, occurred_at=occurred_at,
            )
        )

    # ---------------------------------------------------------------- idempotency -----------
    async def _idempotency_begin(self, tenant_id: str, endpoint_id: str, key: str, body: Mapping[str, Any]) -> tuple[dict[str, Any] | None, str]:
        """Return (replay_response|None, request_hash); raise on hash mismatch / in-progress."""
        digest = request_hash(body)
        result = await self.session.execute(
            select(p.idempotency_key).where(
                p.idempotency_key.c.tenant_id == tenant_id,
                p.idempotency_key.c.endpoint_id == endpoint_id,
                p.idempotency_key.c.key_value == key,
            )
        )
        row = result.mappings().first()
        if row is not None:
            if row["request_hash"] != digest:
                raise HTTPException(status_code=409, detail={"code": "IDEMPOTENCY_KEY_REUSED", "message": "idempotency key reused with a different request body"})
            if row["status"] == "COMPLETED":
                stored = row["response_body"] or {}
                return (_json_safe(stored), digest)
            raise HTTPException(status_code=409, detail={"code": "IDEMPOTENCY_IN_PROGRESS", "message": "idempotency key is in progress"})
        await self.session.execute(
            p.idempotency_key.insert().values(
                id=uuid4(), tenant_id=tenant_id, endpoint_id=endpoint_id, key_value=key,
                request_hash=digest, status="IN_PROGRESS", expires_at=datetime.now(UTC) + IDEMPOTENCY_TTL,
            )
        )
        return (None, digest)

    async def _idempotency_complete(self, tenant_id: str, endpoint_id: str, key: str, response: Mapping[str, Any], resource_id: str) -> None:
        await self.session.execute(
            update(p.idempotency_key)
            .where(
                p.idempotency_key.c.tenant_id == tenant_id,
                p.idempotency_key.c.endpoint_id == endpoint_id,
                p.idempotency_key.c.key_value == key,
            )
            .values(status="COMPLETED", response_status=200, response_body=_json_safe(dict(response)), resource_id=resource_id)
        )

    # ------------------------------------------------------------------ submit --------------
    async def submit(
        self,
        tenant_id: str,
        *,
        document_type: str,
        document_id: str,
        requested_by: str,
        amount_base: Decimal | int | float | str | None = None,
        context: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create (or return the existing) approval request for a document. Domain-service entry point."""
        if not await self._actor_is_member(tenant_id, requested_by):
            raise HTTPException(status_code=403, detail={"code": "REQUESTER_NOT_A_MEMBER", "message": "requester is not an active tenant member"})
        existing = await self.session.execute(
            select(p.approval_request).where(
                p.approval_request.c.tenant_id == tenant_id,
                p.approval_request.c.document_type == document_type,
                p.approval_request.c.document_id == document_id,
            )
        )
        existing_row = existing.mappings().first()
        if existing_row is not None:
            return {"request": _row(existing_row), "definition": await self._load_definition(tenant_id, str(existing_row["definition_id"]) if existing_row["definition_id"] else None), "reused": True, "auto_approved": str(existing_row["status"]) == "APPROVED" and existing_row["definition_id"] is None}
        definitions = await self.session.execute(
            select(p.approval_definition)
            .where(
                p.approval_definition.c.tenant_id == tenant_id,
                p.approval_definition.c.document_type == document_type,
                p.approval_definition.c.is_enabled.is_(True),
            )
            .order_by(p.approval_definition.c.priority.desc(), p.approval_definition.c.created_at)
        )
        chosen = None
        for row in definitions.mappings().all():
            if match_condition(row["condition"], context):
                chosen = _row(row)
                break
        amount = Decimal(str(amount_base)) if amount_base is not None else None
        request_id = uuid4()
        now = datetime.now(UTC)
        if chosen is None:
            values = dict(
                id=request_id, tenant_id=tenant_id, document_type=document_type, document_id=document_id,
                definition_id=None, status="APPROVED", current_step=0, amount_base=amount,
                requested_by=requested_by, requested_at=now, decided_at=now,
            )
        else:
            steps = normalize_steps(chosen.get("steps"))
            if not steps:
                steps = (StepSpec(step=1),)
            values = dict(
                id=request_id, tenant_id=tenant_id, document_type=document_type, document_id=document_id,
                definition_id=chosen["id"], status="PENDING", current_step=1, amount_base=amount,
                requested_by=requested_by, requested_at=now,
            )
        result = await self.session.execute(p.approval_request.insert().values(**values).returning(p.approval_request))
        request = _row(result.one())
        await self._audit(tenant_id, requested_by, "approval.submitted", str(request_id), {
            "document_type": document_type, "document_id": document_id,
            "definition_id": str(request["definition_id"]) if request["definition_id"] else None,
            "status": request["status"], "steps": [step.as_dict() for step in steps] if chosen is not None else [],
        })
        if chosen is None:
            await self._outbox(tenant_id, "platform.approval.approved", request, {
                "request_id": str(request_id), "document_type": document_type, "document_id": document_id,
                "decided_at": now.isoformat(), "auto_approved": True,
            })
        else:
            await self._outbox(tenant_id, "platform.approval.requested", request, {
                "request_id": str(request_id), "document_type": document_type, "document_id": document_id,
                "definition_id": str(chosen["id"]), "current_step": 1,
                "amount_base": str(amount) if amount is not None else None,
            })
        if chosen is None:
            await emit_platform_event(
                self.session, "document.approved", tenant_id=tenant_id, aggregate_type="approval_request",
                aggregate_id=str(request_id),
                payload={"document_type": document_type, "document_id": str(document_id),
                         "approval_request_id": str(request_id)},
            )
        await emit_platform_event(
            self.session, "document.submitted", tenant_id=tenant_id, aggregate_type="approval_request",
            aggregate_id=str(request_id),
            payload={"document_type": document_type, "document_id": str(document_id),
                     "approval_request_id": str(request_id),
                     "amount_base": str(amount) if amount is not None else None},
        )
        return {"request": request, "definition": chosen, "reused": False, "auto_approved": chosen is None}

    # ------------------------------------------------------------------ status --------------
    async def status(self, tenant_id: str, request_id: str) -> dict[str, Any]:
        request = await self._load_request(tenant_id, request_id)
        if request is None:
            raise _not_found(request_id)
        definition = await self._load_definition(tenant_id, request["definition_id"])
        steps = normalize_steps((definition or {}).get("steps"))
        if not steps and request["status"] != "APPROVED":
            steps = (StepSpec(step=1),)
        actions = await self._actions(tenant_id, request_id)
        step_views = []
        for step in steps:
            step_actions = [action for action in actions if int(action["step"]) == step.step]
            approvals = sum(1 for action in step_actions if action["decision"] == "APPROVE")
            rejects = sum(1 for action in step_actions if action["decision"] == "REJECT")
            delegated = any(action["decision"] == "DELEGATE" for action in step_actions)
            state = step_decision_state(step, approvals=approvals, rejects=rejects, delegated=delegated, already_decided=bool(step_actions))
            if request["status"] == "PENDING" and step.step == int(request["current_step"]):
                state = "PENDING" if state != "DELEGATED" else "DELEGATED"
            step_views.append({**step.as_dict(), "state": state, "actions": step_actions})
        return {
            "request": request,
            "steps": step_views,
            "actions": actions,
            "current_step": int(request["current_step"]),
            "status": request["status"],
            "approval_ok": request["status"] == "APPROVED",
            "terminal": is_terminal(request["status"]),
        }

    async def approval_ok(self, tenant_id: str, document_type: str, document_id: str) -> bool:
        result = await self.session.execute(
            select(p.approval_request.c.status).where(
                p.approval_request.c.tenant_id == tenant_id,
                p.approval_request.c.document_type == document_type,
                p.approval_request.c.document_id == document_id,
            )
        )
        return result.scalar_one_or_none() == "APPROVED"

    # ------------------------------------------------------------------ decide --------------
    async def decide(
        self,
        tenant_id: str,
        request_id: str,
        *,
        actor_id: str,
        decision: str,
        comment: str | None = None,
        delegated_to: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        decision = decision.upper()
        if decision not in APPROVAL_DECISIONS:
            raise HTTPException(status_code=422, detail={"code": "INVALID_DECISION", "message": f"decision must be one of {sorted(APPROVAL_DECISIONS)}"})
        body = {"request_id": request_id, "actor_id": actor_id, "decision": decision, "comment": comment, "delegated_to": delegated_to}
        replay = digest = None
        if idempotency_key:
            replay, digest = await self._idempotency_begin(tenant_id, "POST /api/v1/approvals/{request_id}/actions", idempotency_key, body)
            if replay is not None:
                return replay
        response = await self._decide(tenant_id, request_id, actor_id=actor_id, decision=decision, comment=comment, delegated_to=delegated_to)
        if idempotency_key and digest is not None:
            await self._idempotency_complete(tenant_id, "POST /api/v1/approvals/{request_id}/actions", idempotency_key, response, request_id)
        return response

    async def _decide(
        self, tenant_id: str, request_id: str, *, actor_id: str, decision: str, comment: str | None, delegated_to: str | None
    ) -> dict[str, Any]:
        request = await self._load_request(tenant_id, request_id)
        if request is None:
            raise _not_found(request_id)
        if is_terminal(request["status"]):
            raise _terminal(request["status"])
        if not await self._actor_is_member(tenant_id, actor_id):
            raise _bad_step("actor is not an active tenant member")
        if decision == "RECALL":
            if str(request["requested_by"]) != str(actor_id):
                raise HTTPException(status_code=403, detail={"code": "RECALL_NOT_REQUESTER", "message": "only the requester can recall an approval request"})
            action = await self._append_action(tenant_id, request_id, int(request["current_step"]), actor_id, "RECALL", comment, None)
            await self._set_state(tenant_id, request_id, "CANCELLED", int(request["current_step"]), decided=True)
            await self._audit(tenant_id, actor_id, "approval.recalled", request_id, {"comment": comment})
            updated = await self._load_request(tenant_id, request_id)
            return {"request": updated, "action": action}
        definition = await self._load_definition(tenant_id, request["definition_id"])
        steps = normalize_steps((definition or {}).get("steps"))
        if not steps:
            steps = (StepSpec(step=1),)
        current_step = int(request["current_step"])
        step = next((item for item in steps if item.step == current_step), steps[-1])
        if str(request["requested_by"]) == str(actor_id):
            raise SodConflict("requester cannot decide their own approval request", details={"request_id": request_id})
        actor_roles = await self._actor_roles(tenant_id, actor_id)
        if not step_is_authorized(step, str(actor_id), actor_roles):
            raise _bad_step("actor is not authorized for the current approval step", step=step.step)
        if decision == "DELEGATE":
            if not delegated_to:
                raise HTTPException(status_code=422, detail={"code": "DELEGATE_TARGET_REQUIRED", "message": "delegated_to is required for DELEGATE"})
            if str(delegated_to) == str(actor_id):
                raise HTTPException(status_code=422, detail={"code": "DELEGATE_TARGET_INVALID", "message": "cannot delegate to yourself"})
            if not await self._actor_is_member(tenant_id, str(delegated_to)):
                raise HTTPException(status_code=422, detail={"code": "DELEGATE_TARGET_INVALID", "message": "delegated_to must be an active tenant member"})
            if str(delegated_to) == str(request["requested_by"]):
                raise SodConflict("requester cannot be the delegate approver", details={"request_id": request_id})
            action = await self._append_action(tenant_id, request_id, current_step, actor_id, "DELEGATE", comment, str(delegated_to))
            await self._audit(tenant_id, actor_id, "approval.delegated", request_id, {"step": current_step, "delegated_to": str(delegated_to)})
            return {"request": await self._load_request(tenant_id, request_id), "action": action}
        actions = await self._actions(tenant_id, request_id)
        step_actions = [action for action in actions if int(action["step"]) == current_step]
        if any(action["actor_id"] == actor_id and action["decision"] in {"APPROVE", "REJECT"} for action in step_actions):
            raise HTTPException(status_code=409, detail={"code": "STEP_ALREADY_DECIDED", "message": "actor already decided this step"})
        action = await self._append_action(tenant_id, request_id, current_step, actor_id, decision, comment, None)
        if decision == "REJECT":
            await self._set_state(tenant_id, request_id, "REJECTED", current_step, decided=True)
            await self._audit(tenant_id, actor_id, "approval.rejected", request_id, {"step": current_step, "comment": comment})
            updated = await self._load_request(tenant_id, request_id)
            assert updated is not None
            await self._outbox(tenant_id, "platform.approval.rejected", updated, {
                "request_id": request_id, "document_type": request["document_type"],
                "document_id": str(request["document_id"]), "actor_id": str(actor_id), "comment": comment,
            })
            await emit_platform_event(
                self.session, "document.rejected", tenant_id=tenant_id, aggregate_type="approval_request",
                aggregate_id=str(request_id),
                payload={"document_type": str(request["document_type"]), "document_id": str(request["document_id"]),
                         "approval_request_id": str(request_id), "actor_id": str(actor_id)},
            )
            return {"request": updated, "action": action}
        approvals = sum(1 for row_action in step_actions if row_action["decision"] == "APPROVE") + 1
        rejects = 0
        delegated = any(row_action["decision"] == "DELEGATE" for row_action in step_actions)
        if step_decision_state(step, approvals=approvals, rejects=rejects, delegated=delegated, already_decided=True) == "APPROVED":
            next_step = next_position(len(steps), current_step)
            if next_step is None:
                await self._set_state(tenant_id, request_id, "APPROVED", current_step, decided=True)
                await self._audit(tenant_id, actor_id, "approval.approved", request_id, {"step": current_step})
                updated = await self._load_request(tenant_id, request_id)
                assert updated is not None
                await self._outbox(tenant_id, "platform.approval.approved", updated, {
                    "request_id": request_id, "document_type": request["document_type"],
                    "document_id": str(request["document_id"]), "decided_at": datetime.now(UTC).isoformat(),
                })
                await emit_platform_event(
                    self.session, "document.approved", tenant_id=tenant_id, aggregate_type="approval_request",
                    aggregate_id=str(request_id),
                    payload={"document_type": str(request["document_type"]),
                             "document_id": str(request["document_id"]),
                             "approval_request_id": str(request_id)},
                )
                return {"request": updated, "action": action}
            await self._set_state(tenant_id, request_id, "PENDING", next_step)
            await self._audit(tenant_id, actor_id, "approval.step_advanced", request_id, {"step": current_step, "next_step": next_step})
            return {"request": await self._load_request(tenant_id, request_id), "action": action}
        await self._audit(tenant_id, actor_id, "approval.approved", request_id, {"step": current_step, "parallel_pending": True})
        return {"request": await self._load_request(tenant_id, request_id), "action": action}

    # ------------------------------------------------------------------ persistence --------
    async def _append_action(self, tenant_id: str, request_id: str, step: int, actor_id: str, decision: str, comment: str | None, delegated_to: str | None) -> dict[str, Any]:
        result = await self.session.execute(
            p.approval_action.insert().values(
                id=uuid4(), tenant_id=tenant_id, approval_request_id=request_id, step=step,
                actor_id=actor_id, decision=decision, comment=comment, delegated_to=delegated_to,
                created_by=actor_id, updated_by=actor_id,
            ).returning(p.approval_action)
        )
        return _row(result.one())

    async def _set_state(self, tenant_id: str, request_id: str, status: str, current_step: int, *, decided: bool = False) -> None:
        values: dict[str, Any] = {"status": status, "current_step": current_step, "updated_at": datetime.now(UTC), "version": p.approval_request.c.version + 1}
        if decided:
            values["decided_at"] = datetime.now(UTC)
        await self.session.execute(
            update(p.approval_request)
            .where(p.approval_request.c.tenant_id == tenant_id, p.approval_request.c.id == request_id)
            .values(**values)
        )


__all__ = [
    "APPROVAL_DECISIONS",
    "ApprovalService",
    "StepSpec",
    "TERMINAL_STATUSES",
    "is_terminal",
    "match_condition",
    "next_position",
    "normalize_steps",
    "request_hash",
    "required_approvals_for",
    "step_decision_state",
    "step_is_authorized",
]
