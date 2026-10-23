"""Durable DB idempotency guard over ``platform.idempotency_key`` (FS-00 core service).

This is the transactional adapter for the pure decision table in
:mod:`app.core.security.idempotency` (DM-PLAT-06 / ARCHITECTURE §2.4.1). It owns **all** DB access for
request idempotency so routers/services never touch the table directly:

* tenant + endpoint + key scoped rows (unique per ``(tenant_id, endpoint_id, key_value)``);
* ``guard(...)`` returns the pure :class:`~app.core.security.idempotency.IdempotencyDecision`
  (``PROCEED`` / ``REPLAY`` / ``CONFLICT`` / ``IN_PROGRESS``), reserving an ``IN_PROGRESS`` row inside
  the **caller's** transaction when the request should execute;
* ``complete(...)`` stores the response status/body/resource id (and exposes a deterministic response
  hash for integrity checks on replay); ``abort(...)`` releases an ``IN_PROGRESS`` reservation when the
  caller handles a failure without rolling the transaction back;
* ``purge_expired(...)`` is the retention cleanup for the operational table (TTL default 7 days);
* no canonicalisation is duplicated - keys, canonical JSON, request fingerprints and decision rules are
  imported from the pure helpers.

Async SQLAlchemy Core only; no ORM imports of other contexts, no commits (the caller owns the
transaction boundary).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.models import platform as p
from app.core.security.idempotency import (
    IDEMPOTENCY_TTL_SECONDS,
    IdempotencyDecision,
    IdempotencyOutcome,
    IdempotencyRecord,
    IdempotencyStatus,
    begin_record,
    canonical_json,
    complete_record,
    decide_idempotency,
    request_fingerprint,
    scoped_key,
    sha256_hex,
    utc_now,
    validate_idempotency_key,
)

__all__ = [
    "IDEMPOTENCY_TTL_SECONDS",
    "IdempotencyDecision",
    "IdempotencyOutcome",
    "IdempotencyRecord",
    "IdempotencyStore",
    "StoredResponse",
    "abort",
    "build_fingerprint",
    "complete",
    "get_record",
    "guard",
    "purge_expired",
    "response_hash",
]

_COMPLETED = IdempotencyStatus.COMPLETED.value
_IN_PROGRESS = IdempotencyStatus.IN_PROGRESS.value


@dataclass(frozen=True, slots=True)
class StoredResponse:
    """Stored replay payload plus its deterministic integrity hash."""

    status: int
    body: Mapping[str, Any] | None
    resource_id: str | None
    response_hash: str

    @classmethod
    def from_record(cls, record: IdempotencyRecord) -> StoredResponse:
        if record.status is not IdempotencyStatus.COMPLETED or record.response_status is None:
            raise ValueError("response is only available for COMPLETED idempotency records")
        return cls(
            status=int(record.response_status),
            body=dict(record.response_body) if record.response_body is not None else None,
            resource_id=record.resource_id,
            response_hash=response_hash(record),
        )


# --------------------------------------------------------------------------- helpers ----------
def build_fingerprint(
    *,
    method: str,
    path: str,
    tenant_id: str | UUID,
    endpoint_id: str,
    body: Any = None,
    query: Mapping[str, Any] | None = None,
) -> str:
    """Thin convenience wrapper around the pure request-fingerprint helper (no duplication)."""
    return request_fingerprint(
        method=method,
        path=path,
        tenant_id=tenant_id,
        endpoint_id=endpoint_id,
        body=body,
        query=query,
    )


def _as_uuid(value: str | UUID) -> UUID:
    return value if isinstance(value, UUID) else UUID(str(value))


def _now(now: datetime | None) -> datetime:
    moment = now or utc_now()
    if moment.tzinfo is None:
        raise ValueError("idempotency timestamps must be timezone-aware (UTC)")
    return moment.astimezone(moment.tzinfo) if False else moment


def _record_from_row(row: Mapping[str, Any]) -> IdempotencyRecord:
    status = IdempotencyStatus(str(row["status"]))
    return IdempotencyRecord(
        key=str(row["key_value"]),
        request_hash=str(row["request_hash"]),
        status=status,
        created_at=row["created_at"],
        expires_at=row["expires_at"],
        response_status=int(row["response_status"]) if row["response_status"] is not None else None,
        response_body=dict(row["response_body"]) if row["response_body"] is not None else None,
        resource_id=str(row["resource_id"]) if row["resource_id"] is not None else None,
        completed_at=row.get("updated_at") if status is IdempotencyStatus.COMPLETED else None,
    )


def response_hash(record: IdempotencyRecord) -> str:
    """Deterministic SHA-256 over the stored response (status + canonical body + resource id)."""
    return sha256_hex(
        canonical_json(
            {
                "status": record.response_status,
                "body": record.response_body,
                "resource_id": record.resource_id,
            }
        )
    )


async def _fetch(session: AsyncSession, tenant_id: UUID, endpoint_id: str, key: str) -> Mapping[str, Any] | None:
    result = await session.execute(
        select(p.idempotency_key).where(
            p.idempotency_key.c.tenant_id == tenant_id,
            p.idempotency_key.c.endpoint_id == endpoint_id,
            p.idempotency_key.c.key_value == key,
        )
    )
    return result.mappings().first()


# --------------------------------------------------------------------------- public API --------
async def guard(
    session: AsyncSession,
    *,
    tenant_id: str | UUID,
    endpoint_id: str,
    key: str,
    request_fingerprint_hash: str,
    ttl_seconds: int = IDEMPOTENCY_TTL_SECONDS,
    now: datetime | None = None,
    in_progress_retry_after_seconds: int = 1,
    actor_id: str | UUID | None = None,
) -> IdempotencyDecision:
    """Reserve/evaluate an ``Idempotency-Key`` inside the caller's transaction.

    * ``PROCEED`` — no live row (or an expired one was reset); caller must execute the side effect and
      then call :func:`complete` in the same transaction.
    * ``REPLAY`` — completed row with the same fingerprint; return the stored response.
    * ``CONFLICT`` — same key, different fingerprint (``409 IDEMPOTENCY_KEY_REUSED``).
    * ``IN_PROGRESS`` — concurrent duplicate (``409 IDEMPOTENCY_IN_PROGRESS`` + ``Retry-After``).

    The decision type exposes ``raise_for_conflict()`` so callers do not need to re-map codes.
    """
    if not request_fingerprint_hash:
        raise ValueError("request_fingerprint_hash is required")
    tid = _as_uuid(tenant_id)
    validate_idempotency_key(key)
    moment = now or utc_now()
    row = await _fetch(session, tid, endpoint_id, key)
    decision = decide_idempotency(
        _record_from_row(row) if row is not None else None,
        request_hash=request_fingerprint_hash,
        now=moment,
        ttl_seconds=ttl_seconds,
        in_progress_retry_after_seconds=in_progress_retry_after_seconds,
    )
    if decision.outcome is not IdempotencyOutcome.PROCEED:
        return decision

    fresh = begin_record(
        key=key, request_hash=request_fingerprint_hash, now=moment, ttl_seconds=ttl_seconds
    )
    if row is None:
        insert_result = await session.execute(
            pg_insert(p.idempotency_key)
            .values(
                id=uuid4(),
                tenant_id=tid,
                endpoint_id=endpoint_id,
                key_value=key,
                request_hash=request_fingerprint_hash,
                status=_IN_PROGRESS,
                response_status=None,
                response_body=None,
                resource_id=None,
                expires_at=fresh.expires_at,
                created_at=moment,
                updated_at=moment,
                created_by=_as_uuid(actor_id) if actor_id is not None else None,
                version=1,
            )
            .on_conflict_do_nothing(index_elements=["tenant_id", "endpoint_id", "key_value"])
        )
        if int(getattr(insert_result, "rowcount", 0) or 0) == 1:
            return IdempotencyDecision(outcome=IdempotencyOutcome.PROCEED, record=fresh)
        # Lost a race: re-read and decide against the winner's row.
        row = await _fetch(session, tid, endpoint_id, key)
        if row is None:  # pragma: no cover - conflict without a readable row
            return IdempotencyDecision(outcome=IdempotencyOutcome.PROCEED, record=fresh)
        return decide_idempotency(
            _record_from_row(row),
            request_hash=request_fingerprint_hash,
            now=moment,
            ttl_seconds=ttl_seconds,
            in_progress_retry_after_seconds=in_progress_retry_after_seconds,
        )

    # Expired row exists; reset it in place for the new request (operational-table rewrite, allowed).
    reset = await session.execute(
        update(p.idempotency_key)
        .where(
            p.idempotency_key.c.tenant_id == tid,
            p.idempotency_key.c.endpoint_id == endpoint_id,
            p.idempotency_key.c.key_value == key,
            p.idempotency_key.c.expires_at < moment,
        )
        .values(
            request_hash=request_fingerprint_hash,
            status=_IN_PROGRESS,
            response_status=None,
            response_body=None,
            resource_id=None,
            expires_at=fresh.expires_at,
            created_at=moment,
            updated_at=moment,
            created_by=_as_uuid(actor_id) if actor_id is not None else None,
            version=p.idempotency_key.c.version + 1,
        )
    )
    if int(getattr(reset, "rowcount", 0) or 0) == 1:
        return IdempotencyDecision(outcome=IdempotencyOutcome.PROCEED, record=fresh)
    # Concurrent reset/completion: re-read and decide.
    row = await _fetch(session, tid, endpoint_id, key)
    if row is None:  # pragma: no cover - row vanished between statements
        return IdempotencyDecision(outcome=IdempotencyOutcome.PROCEED, record=fresh)
    return decide_idempotency(
        _record_from_row(row),
        request_hash=request_fingerprint_hash,
        now=moment,
        ttl_seconds=ttl_seconds,
        in_progress_retry_after_seconds=in_progress_retry_after_seconds,
    )


async def get_record(
    session: AsyncSession, *, tenant_id: str | UUID, endpoint_id: str, key: str
) -> IdempotencyRecord | None:
    row = await _fetch(session, _as_uuid(tenant_id), endpoint_id, key)
    return _record_from_row(row) if row is not None else None


async def complete(
    session: AsyncSession,
    *,
    tenant_id: str | UUID,
    endpoint_id: str,
    key: str,
    response_status: int,
    response_body: Mapping[str, Any] | None = None,
    resource_id: str | UUID | None = None,
    now: datetime | None = None,
) -> IdempotencyRecord:
    """Mark a reserved key COMPLETED with the replay payload (same transaction as the side effect)."""
    tid = _as_uuid(tenant_id)
    validate_idempotency_key(key)
    moment = now or utc_now()
    row = await _fetch(session, tid, endpoint_id, key)
    if row is None:
        raise LookupError("no idempotency reservation for this key")
    record = _record_from_row(row)
    if record.status is not IdempotencyStatus.IN_PROGRESS:
        raise ValueError("idempotency key is already completed")
    completed = complete_record(
        record,
        response_status=response_status,
        response_body=response_body,
        resource_id=str(resource_id) if resource_id is not None else None,
        now=moment,
    )
    await session.execute(
        update(p.idempotency_key)
        .where(
            p.idempotency_key.c.tenant_id == tid,
            p.idempotency_key.c.endpoint_id == endpoint_id,
            p.idempotency_key.c.key_value == key,
        )
        .values(
            status=_COMPLETED,
            response_status=completed.response_status,
            response_body=completed.response_body,
            resource_id=_as_uuid(completed.resource_id) if completed.resource_id is not None else None,
            updated_at=moment,
            version=p.idempotency_key.c.version + 1,
        )
    )
    return completed


async def abort(
    session: AsyncSession, *, tenant_id: str | UUID, endpoint_id: str, key: str
) -> bool:
    """Release an IN_PROGRESS reservation (failure handled without rolling the transaction back)."""
    result = await session.execute(
        delete(p.idempotency_key).where(
            p.idempotency_key.c.tenant_id == _as_uuid(tenant_id),
            p.idempotency_key.c.endpoint_id == endpoint_id,
            p.idempotency_key.c.key_value == key,
            p.idempotency_key.c.status == _IN_PROGRESS,
        )
    )
    return int(getattr(result, "rowcount", 0) or 0) == 1


async def purge_expired(
    session: AsyncSession,
    *,
    tenant_id: str | UUID | None = None,
    now: datetime | None = None,
    limit: int = 10_000,
) -> int:
    """Delete expired idempotency rows (retention cleanup; operational table is purgeable)."""
    if limit <= 0:
        raise ValueError("limit must be positive")
    moment = now or utc_now()
    candidate_ids = (
        select(p.idempotency_key.c.id)
        .where(p.idempotency_key.c.expires_at < moment)
        .order_by(p.idempotency_key.c.expires_at)
        .limit(limit)
    )
    if tenant_id is not None:
        candidate_ids = candidate_ids.where(p.idempotency_key.c.tenant_id == _as_uuid(tenant_id))
    subquery = candidate_ids.subquery()
    result = await session.execute(delete(p.idempotency_key).where(p.idempotency_key.c.id.in_(select(subquery.c.id))))
    return int(getattr(result, "rowcount", 0) or 0)


class IdempotencyStore:
    """Thin object facade over the module functions (one instance per request/session scope)."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def guard(
        self,
        *,
        tenant_id: str | UUID,
        endpoint_id: str,
        key: str,
        request_fingerprint_hash: str,
        ttl_seconds: int = IDEMPOTENCY_TTL_SECONDS,
        now: datetime | None = None,
        actor_id: str | UUID | None = None,
    ) -> IdempotencyDecision:
        return await guard(
            self.session,
            tenant_id=tenant_id,
            endpoint_id=endpoint_id,
            key=key,
            request_fingerprint_hash=request_fingerprint_hash,
            ttl_seconds=ttl_seconds,
            now=now,
            actor_id=actor_id,
        )

    async def complete(
        self,
        *,
        tenant_id: str | UUID,
        endpoint_id: str,
        key: str,
        response_status: int,
        response_body: Mapping[str, Any] | None = None,
        resource_id: str | UUID | None = None,
        now: datetime | None = None,
    ) -> IdempotencyRecord:
        return await complete(
            self.session,
            tenant_id=tenant_id,
            endpoint_id=endpoint_id,
            key=key,
            response_status=response_status,
            response_body=response_body,
            resource_id=resource_id,
            now=now,
        )

    async def abort(self, *, tenant_id: str | UUID, endpoint_id: str, key: str) -> bool:
        return await abort(self.session, tenant_id=tenant_id, endpoint_id=endpoint_id, key=key)

    async def get_record(self, *, tenant_id: str | UUID, endpoint_id: str, key: str) -> IdempotencyRecord | None:
        return await get_record(self.session, tenant_id=tenant_id, endpoint_id=endpoint_id, key=key)

    async def purge_expired(
        self, *, tenant_id: str | UUID | None = None, now: datetime | None = None, limit: int = 10_000
    ) -> int:
        return await purge_expired(self.session, tenant_id=tenant_id, now=now, limit=limit)


# Keep the scoped-key helper reachable for callers that build their own composite labels.
def composite_key(*, tenant_id: str | UUID, endpoint_id: str, key: str) -> str:
    return scoped_key(tenant_id=tenant_id, endpoint_id=endpoint_id, key=key)


def expires_at(*, now: datetime, ttl_seconds: int = IDEMPOTENCY_TTL_SECONDS) -> datetime:
    if ttl_seconds <= 0:
        raise ValueError("ttl_seconds must be positive")
    return now + timedelta(seconds=ttl_seconds)
