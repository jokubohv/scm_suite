"""Pure tests for the durable idempotency guard helpers (no database)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.core.idempotency_store import (
    IdempotencyDecision,
    IdempotencyOutcome,
    IdempotencyRecord,
    IdempotencyStatus,
    StoredResponse,
    build_fingerprint,
    composite_key,
    expires_at,
    response_hash,
)
from app.core.security.errors import IdempotencyConflict, IdempotencyInProgress, InvalidIdempotencyKey
from app.core.security.idempotency import complete_record

TENANT = "11111111-1111-1111-1111-111111111111"
NOW = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)


def _record(**overrides: object) -> IdempotencyRecord:
    base: dict[str, object] = {
        "key": "key-1",
        "request_hash": "a" * 64,
        "status": IdempotencyStatus.COMPLETED,
        "created_at": NOW,
        "expires_at": NOW + timedelta(days=7),
        "response_status": 201,
        "response_body": {"id": "abc"},
        "resource_id": "abc",
        "completed_at": NOW,
    }
    base.update(overrides)
    return IdempotencyRecord(**base)  # type: ignore[arg-type]


class TestFingerprint:
    def test_fingerprint_is_canonical_and_tenant_scoped(self) -> None:
        left = build_fingerprint(
            method="post",
            path="/api/v1/things",
            tenant_id=TENANT,
            endpoint_id="things:create",
            body={"b": 2, "a": 1},
        )
        right = build_fingerprint(
            method="POST",
            path="//api/v1/things/",
            tenant_id=TENANT,
            endpoint_id="things:create",
            body={"a": 1, "b": 2},
        )
        assert left == right
        other_tenant = build_fingerprint(
            method="POST",
            path="/api/v1/things",
            tenant_id=str(uuid4()),
            endpoint_id="things:create",
            body={"a": 1, "b": 2},
        )
        assert other_tenant != left
        different_body = build_fingerprint(
            method="POST",
            path="/api/v1/things",
            tenant_id=TENANT,
            endpoint_id="things:create",
            body={"a": 1, "b": 3},
        )
        assert different_body != left

    def test_fingerprint_rejects_empty_method(self) -> None:
        with pytest.raises(ValueError):
            build_fingerprint(method=" ", path="/x", tenant_id=TENANT, endpoint_id="e")


class TestResponseStorage:
    def test_stored_response_exposes_hash_and_payload(self) -> None:
        record = _record()
        stored = StoredResponse.from_record(record)
        assert stored.status == 201
        assert stored.body == {"id": "abc"}
        assert stored.resource_id == "abc"
        assert stored.response_hash == response_hash(record)
        assert len(stored.response_hash) == 64

    def test_response_hash_changes_with_response(self) -> None:
        base = _record()
        changed = _record(response_body={"id": "xyz"})
        assert response_hash(base) != response_hash(changed)

    def test_stored_response_requires_completed_record(self) -> None:
        with pytest.raises(ValueError):
            StoredResponse.from_record(_record(status=IdempotencyStatus.IN_PROGRESS))
        with pytest.raises(ValueError):
            StoredResponse.from_record(_record(response_status=None))

    def test_completed_record_helper_roundtrip(self) -> None:
        record = _record(status=IdempotencyStatus.IN_PROGRESS, response_status=None, response_body=None)
        completed = complete_record(
            record, response_status=200, response_body={"ok": True}, resource_id=str(uuid4()), now=NOW
        )
        assert completed.status is IdempotencyStatus.COMPLETED
        assert StoredResponse.from_record(completed).status == 200


class TestDecisionErgonomics:
    def test_conflict_and_in_progress_raise_typed_errors(self) -> None:
        with pytest.raises(IdempotencyConflict):
            IdempotencyDecision(outcome=IdempotencyOutcome.CONFLICT).raise_for_conflict()
        with pytest.raises(IdempotencyInProgress):
            IdempotencyDecision(outcome=IdempotencyOutcome.IN_PROGRESS, retry_after_seconds=2).raise_for_conflict()

    def test_proceed_and_replay_do_not_raise(self) -> None:
        IdempotencyDecision(outcome=IdempotencyOutcome.PROCEED).raise_for_conflict()
        decision = IdempotencyDecision(outcome=IdempotencyOutcome.REPLAY, record=_record())
        decision.raise_for_conflict()
        assert decision.replay is True


class TestKeyAndExpiryHelpers:
    def test_composite_key_is_scoped(self) -> None:
        key = composite_key(tenant_id=TENANT, endpoint_id="things:create", key="abc")
        assert key.startswith(f"{TENANT}:things:create:")
        with pytest.raises(InvalidIdempotencyKey):
            composite_key(tenant_id=TENANT, endpoint_id="e", key="bad key")

    def test_expires_at_bounds(self) -> None:
        assert expires_at(now=NOW, ttl_seconds=60) == NOW + timedelta(seconds=60)
        with pytest.raises(ValueError):
            expires_at(now=NOW, ttl_seconds=0)
