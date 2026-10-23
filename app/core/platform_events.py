"""FS-00 platform event producers (transactional outbox).

Central helper used by the platform services/routers to emit the declared ``platform.core`` /
``platform.finance`` / ``order.demand`` events. Payloads are assembled from the implementation action
point and validated against the committed extension schemas under ``app/contracts/platform/events/``
(required payload keys only; optional keys are documented in the schemas). The outbox insert joins the
caller's transaction, so replaying an idempotent request does not duplicate events.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from functools import cache
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.outbox import emit

EVENT_SCHEMA_DIR = Path(__file__).resolve().parents[1] / "contracts" / "platform" / "events"
SYSTEM_TENANT_ID = "00000000-0000-0000-0000-000000000000"


@cache
def required_payload_keys(event_type: str) -> tuple[str, ...]:
    """Return the required *payload* keys from the implementation-backed extension schema."""
    path = EVENT_SCHEMA_DIR / f"{event_type}.schema.json"
    try:
        schema = json.loads(path.read_text(encoding="utf-8"))
    except OSError:
        return ()
    properties = schema.get("properties") or {}
    if isinstance(properties.get("data"), dict) and "properties" in properties["data"]:
        return tuple(properties["data"].get("required") or ())
    return tuple(schema.get("required") or ())


async def emit_platform_event(
    session: AsyncSession,
    event_type: str,
    *,
    tenant_id: str,
    aggregate_type: str,
    aggregate_id: str,
    payload: Mapping[str, Any],
    aggregate_version: int = 1,
    correlation_id: str | None = None,
    causation_id: str | None = None,
) -> dict[str, Any]:
    """Insert one declared FS-00 event into the outbox in the current transaction."""
    missing = [key for key in required_payload_keys(event_type) if key not in payload]
    if missing:
        raise ValueError(f"{event_type} payload missing required key(s): {missing}")
    return await emit(
        session,
        event_type=event_type,
        tenant_id=str(tenant_id),
        aggregate_type=aggregate_type,
        aggregate_id=str(aggregate_id),
        payload=dict(payload),
        aggregate_version=aggregate_version,
        correlation_id=correlation_id,
        causation_id=causation_id,
    )


async def sod_blocked(session, *, tenant_id: str, actor_app_user_id: str | None, context: str,
                     pair: list[str], rule: str, resource_id: str, decision: str = "BLOCKED") -> dict[str, Any]:
    """Emit ``sod.violation.blocked`` for an enforcement point that blocked a conflict."""
    return await emit_platform_event(
        session, "sod.violation.blocked", tenant_id=str(tenant_id), aggregate_type="sod_policy",
        aggregate_id=str(uuid5(NAMESPACE_URL, f"sod:{resource_id}")),
        payload={"tenant_id": str(tenant_id), "actor_app_user_id": actor_app_user_id,
                 "context": {"kind": context}, "pair": {"a": pair[0] if pair else None,
                                                        "b": pair[1] if len(pair) > 1 else None},
                 "rule": rule, "resource_id": str(resource_id), "decision": decision},
    )


async def audit_recorded(
    session: AsyncSession,
    *,
    tenant_id: str,
    event_id: str,
    actor_type: str,
    action: str,
    resource_type: str,
    resource_id: str | None,
    occurred_at: str,
) -> dict[str, Any]:
    """Emit ``platform.audit.recorded`` for one persisted audit row."""
    return await emit_platform_event(
        session,
        "platform.audit.recorded",
        tenant_id=str(tenant_id),
        aggregate_type="audit_event",
        aggregate_id=event_id,
        payload={
            "event_id": event_id,
            "actor_type": actor_type,
            "action": action,
            "resource_type": resource_type,
            "resource_id": resource_id,
            "occurred_at": occurred_at,
        },
    )


__all__ = ["EVENT_SCHEMA_DIR", "SYSTEM_TENANT_ID", "emit_platform_event", "required_payload_keys",
           "audit_recorded", "sod_blocked"]
