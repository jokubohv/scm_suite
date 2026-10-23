"""Transactional outbox writer (FS-00 platform.events).

Events are inserted in the caller's transaction; the relay publishes them to Redis Streams and marks
``published_at``. Payloads are JSON-serialisable dicts validated against the event schema registry by
the emitting module's contract tests (not at runtime hot path).
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.models import platform as models

STREAM_KEY = "inventory.events"


def utc_now() -> datetime:
    return datetime.now(UTC)


def event_envelope(
    *,
    event_type: str,
    tenant_id: str,
    aggregate_type: str,
    aggregate_id: str,
    payload: Mapping[str, Any],
    event_version: str = "1.0.0",
    aggregate_version: int = 1,
    correlation_id: str | None = None,
    causation_id: str | None = None,
) -> dict[str, Any]:
    event_id = str(uuid.uuid4())
    occurred_at = utc_now()
    return {
        "id": str(uuid.uuid4()),
        "tenant_id": tenant_id,
        "event_id": event_id,
        "aggregate_type": aggregate_type,
        "aggregate_id": aggregate_id,
        "aggregate_version": aggregate_version,
        "event_type": event_type,
        "event_version": event_version,
        "payload": {
            "event_id": event_id,
            "event_type": event_type,
            "event_version": event_version,
            "tenant_id": tenant_id,
            "occurred_at": occurred_at.isoformat(),
            "aggregate": {"type": aggregate_type, "id": aggregate_id, "version": aggregate_version},
            "data": dict(payload),
        },
        "occurred_at": occurred_at,
        "correlation_id": correlation_id,
        "causation_id": causation_id,
        "published_at": None,
        "attempts": 0,
        "last_error": None,
    }


async def emit(session: AsyncSession, **kwargs: Any) -> dict[str, Any]:
    """Insert one outbox event in the current transaction (does not commit)."""
    row = event_envelope(**kwargs)
    table = models.metadata.tables["platform.outbox_event"]
    stmt = table.insert().values(**row)
    await session.execute(stmt)
    return row


async def relay_once(
    session: AsyncSession,
    redis: Any,
    *,
    batch_size: int = 100,
    stream_key: str = STREAM_KEY,
) -> int:
    """Publish a batch of unpublished outbox rows to a Redis stream; returns published count."""
    table = models.metadata.tables["platform.outbox_event"]
    from sqlalchemy import select

    query = (
        select(table)
        .where(table.c.published_at.is_(None))
        .order_by(table.c.occurred_at)
        .limit(batch_size)
        .with_for_update(skip_locked=True)
    )
    rows = list(await session.execute(query))
    published = 0
    for row in rows:
        mapping = dict(row._mapping)
        try:
            await redis.xadd(stream_key, {"event_id": mapping["event_id"], "event_type": mapping["event_type"], "payload": str(mapping["payload"])})
            await session.execute(table.update().where(table.c.id == mapping["id"]).values(published_at=utc_now(), attempts=table.c.attempts + 1, last_error=None))
            published += 1
        except Exception as exc:  # noqa: BLE001 - relay must continue on a poison event
            await session.execute(table.update().where(table.c.id == mapping["id"]).values(attempts=table.c.attempts + 1, last_error=str(exc)[:1000]))
    return published
