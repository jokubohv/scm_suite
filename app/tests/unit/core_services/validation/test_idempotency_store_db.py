"""Live-DB tests for the durable idempotency guard (graceful skip without a database)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import select, update

from app.core import idempotency_store as store
from app.core.models import platform as p
from app.core.security.errors import IdempotencyConflict, IdempotencyInProgress

pytestmark = pytest.mark.integration

ENDPOINT = "items:create"


async def _seed_tenant(db_session: Any, *, code: str | None = None) -> str:
    tenant_id = uuid4()
    await db_session.execute(
        p.tenant.insert().values(
            id=tenant_id,
            tenant_id=tenant_id,
            code=code or f"T{uuid4().hex[:10].upper()}",
            name="Idempotency Tenant",
            base_currency_code="USD",
            timezone="UTC",
            status="ACTIVE",
            negative_inventory_policy="BLOCK",
            rounding_mode="PER_MOVEMENT",
            retention_ledger_years=7,
            settings={},
            version=1,
        )
    )
    await db_session.flush()
    return str(tenant_id)


async def _row(db_session: Any, tenant_id: str, key: str) -> Any:
    result = await db_session.execute(
        select(p.idempotency_key).where(
            p.idempotency_key.c.tenant_id == tenant_id,
            p.idempotency_key.c.endpoint_id == ENDPOINT,
            p.idempotency_key.c.key_value == key,
        )
    )
    return result.mappings().first()


async def test_proceed_replay_conflict_and_in_progress(db_session: Any) -> None:
    tenant_id = await _seed_tenant(db_session)
    key = uuid4().hex
    hash_a = store.build_fingerprint(
        method="POST", path="/api/v1/items", tenant_id=tenant_id, endpoint_id=ENDPOINT, body={"sku": "A"}
    )

    first = await store.guard(
        db_session,
        tenant_id=tenant_id,
        endpoint_id=ENDPOINT,
        key=key,
        request_fingerprint_hash=hash_a,
    )
    assert first.outcome is store.IdempotencyOutcome.PROCEED
    assert (await _row(db_session, tenant_id, key))["status"] == "IN_PROGRESS"

    in_progress = await store.guard(
        db_session, tenant_id=tenant_id, endpoint_id=ENDPOINT, key=key, request_fingerprint_hash=hash_a
    )
    assert in_progress.outcome is store.IdempotencyOutcome.IN_PROGRESS
    assert in_progress.retry_after_seconds == 1
    with pytest.raises(IdempotencyInProgress):
        in_progress.raise_for_conflict()

    completed = await store.complete(
        db_session,
        tenant_id=tenant_id,
        endpoint_id=ENDPOINT,
        key=key,
        response_status=201,
        response_body={"id": "item-1"},
        resource_id=str(uuid4()),
    )
    assert completed.status is store.IdempotencyStatus.COMPLETED

    replay = await store.guard(
        db_session, tenant_id=tenant_id, endpoint_id=ENDPOINT, key=key, request_fingerprint_hash=hash_a
    )
    assert replay.outcome is store.IdempotencyOutcome.REPLAY
    assert replay.record is not None
    assert replay.record.response_status == 201
    assert replay.record.response_body == {"id": "item-1"}
    assert len(store.response_hash(replay.record)) == 64
    replay.raise_for_conflict()

    hash_b = store.build_fingerprint(
        method="POST", path="/api/v1/items", tenant_id=tenant_id, endpoint_id=ENDPOINT, body={"sku": "B"}
    )
    conflict = await store.guard(
        db_session, tenant_id=tenant_id, endpoint_id=ENDPOINT, key=key, request_fingerprint_hash=hash_b
    )
    assert conflict.outcome is store.IdempotencyOutcome.CONFLICT
    with pytest.raises(IdempotencyConflict):
        conflict.raise_for_conflict()


async def test_tenant_isolation(db_session: Any) -> None:
    tenant_a = await _seed_tenant(db_session)
    tenant_b = await _seed_tenant(db_session)
    key = uuid4().hex
    fingerprint = "f" * 64

    await store.guard(
        db_session, tenant_id=tenant_a, endpoint_id=ENDPOINT, key=key, request_fingerprint_hash=fingerprint
    )
    await store.complete(
        db_session,
        tenant_id=tenant_a,
        endpoint_id=ENDPOINT,
        key=key,
        response_status=200,
        response_body={"tenant": "a"},
    )
    other = await store.guard(
        db_session, tenant_id=tenant_b, endpoint_id=ENDPOINT, key=key, request_fingerprint_hash=fingerprint
    )
    assert other.outcome is store.IdempotencyOutcome.PROCEED
    assert await store.get_record(db_session, tenant_id=tenant_b, endpoint_id=ENDPOINT, key=key) is not None


async def test_expired_record_is_reset_and_purgeable(db_session: Any) -> None:
    tenant_id = await _seed_tenant(db_session)
    key = uuid4().hex
    old_hash = "a" * 64
    await store.guard(db_session, tenant_id=tenant_id, endpoint_id=ENDPOINT, key=key, request_fingerprint_hash=old_hash)
    await store.complete(
        db_session,
        tenant_id=tenant_id,
        endpoint_id=ENDPOINT,
        key=key,
        response_status=200,
        response_body={"old": True},
    )
    past = datetime.now(UTC) - timedelta(seconds=5)
    await db_session.execute(
        update(p.idempotency_key)
        .where(
            p.idempotency_key.c.tenant_id == tenant_id,
            p.idempotency_key.c.endpoint_id == ENDPOINT,
            p.idempotency_key.c.key_value == key,
        )
        .values(expires_at=past)
    )
    fresh_hash = "b" * 64
    decision = await store.guard(
        db_session, tenant_id=tenant_id, endpoint_id=ENDPOINT, key=key, request_fingerprint_hash=fresh_hash
    )
    assert decision.outcome is store.IdempotencyOutcome.PROCEED
    row = await _row(db_session, tenant_id, key)
    assert row["status"] == "IN_PROGRESS"
    assert row["request_hash"] == fresh_hash

    # Abort releases the reservation, and purge_expired removes expired rows only.
    await store.complete(
        db_session,
        tenant_id=tenant_id,
        endpoint_id=ENDPOINT,
        key=key,
        response_status=200,
        response_body={"new": True},
    )
    await db_session.execute(
        update(p.idempotency_key)
        .where(
            p.idempotency_key.c.tenant_id == tenant_id,
            p.idempotency_key.c.endpoint_id == ENDPOINT,
            p.idempotency_key.c.key_value == key,
        )
        .values(expires_at=past)
    )
    live_key = uuid4().hex
    await store.guard(
        db_session, tenant_id=tenant_id, endpoint_id=ENDPOINT, key=live_key, request_fingerprint_hash="c" * 64
    )
    purged = await store.purge_expired(db_session, tenant_id=tenant_id)
    assert purged == 1
    assert await _row(db_session, tenant_id, key) is None
    assert await _row(db_session, tenant_id, live_key) is not None


async def test_abort_releases_in_progress(db_session: Any) -> None:
    tenant_id = await _seed_tenant(db_session)
    key = uuid4().hex
    await store.guard(db_session, tenant_id=tenant_id, endpoint_id=ENDPOINT, key=key, request_fingerprint_hash="a" * 64)
    assert await store.abort(db_session, tenant_id=tenant_id, endpoint_id=ENDPOINT, key=key) is True
    assert await store.abort(db_session, tenant_id=tenant_id, endpoint_id=ENDPOINT, key=key) is False
    retry = await store.guard(
        db_session, tenant_id=tenant_id, endpoint_id=ENDPOINT, key=key, request_fingerprint_hash="b" * 64
    )
    assert retry.outcome is store.IdempotencyOutcome.PROCEED


async def test_complete_requires_reservation_and_is_single_use(db_session: Any) -> None:
    tenant_id = await _seed_tenant(db_session)
    key = uuid4().hex
    with pytest.raises(LookupError):
        await store.complete(db_session, tenant_id=tenant_id, endpoint_id=ENDPOINT, key=key, response_status=200)
    await store.guard(db_session, tenant_id=tenant_id, endpoint_id=ENDPOINT, key=key, request_fingerprint_hash="a" * 64)
    await store.complete(db_session, tenant_id=tenant_id, endpoint_id=ENDPOINT, key=key, response_status=200)
    with pytest.raises(ValueError):
        await store.complete(db_session, tenant_id=tenant_id, endpoint_id=ENDPOINT, key=key, response_status=200)


async def test_store_facade_and_fingerprint_helper(db_session: Any) -> None:
    tenant_id = await _seed_tenant(db_session)
    facade = store.IdempotencyStore(db_session)
    key = uuid4().hex
    fingerprint = store.build_fingerprint(
        method="POST", path="/x", tenant_id=tenant_id, endpoint_id=ENDPOINT, body={"b": 2, "a": 1}
    )
    decision = await facade.guard(
        tenant_id=tenant_id, endpoint_id=ENDPOINT, key=key, request_fingerprint_hash=fingerprint
    )
    assert decision.outcome is store.IdempotencyOutcome.PROCEED
    record = await facade.get_record(tenant_id=tenant_id, endpoint_id=ENDPOINT, key=key)
    assert record is not None and record.status is store.IdempotencyStatus.IN_PROGRESS
