"""Idempotency canonicalisation and decision-table tests (pure)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

import pytest

from app.core.security.errors import IdempotencyConflict, IdempotencyInProgress, InvalidIdempotencyKey
from app.core.security.idempotency import (
    IdempotencyOutcome,
    IdempotencyStatus,
    begin_record,
    canonical_json,
    canonicalize,
    complete_record,
    decide_idempotency,
    request_fingerprint,
    scoped_key,
    validate_idempotency_key,
)
from app.tests.unit.platform_security.helpers import StepClock


class Colour(StrEnum):
    RED = "red"


def test_canonicalisation_is_deterministic_and_typed() -> None:
    payload = {
        "b": [Decimal("1.50"), Decimal("2")],
        "a": datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC),
        "c": UUID("11111111-1111-1111-1111-111111111111"),
        "d": Colour.RED,
        "e": None,
        "f": {"z": True, "y": "text"},
    }
    first = canonical_json(payload)
    second = canonical_json({key: payload[key] for key in reversed(list(payload))})
    assert first == second
    assert '"a":"2026-01-02T03:04:05.000000Z"' in first
    assert '"b":["1.5","2"]' in first
    assert '"d":"red"' in first
    with pytest.raises(TypeError):
        canonicalize(object())


def test_fingerprint_is_stable_and_sensitive_to_request_identity() -> None:
    base = dict(method="POST", path="/api/v1/items", tenant_id="tenant-1", endpoint_id="items.create", body={"name": "x"})
    fingerprint = request_fingerprint(**base)
    assert fingerprint == request_fingerprint(**base)
    assert len(fingerprint) == 64
    assert request_fingerprint(**{**base, "body": {"name": "y"}}) != fingerprint
    assert request_fingerprint(**{**base, "path": "/api/v1/items/1"}) != fingerprint
    assert request_fingerprint(**{**base, "tenant_id": "tenant-2"}) != fingerprint
    assert request_fingerprint(**{**base, "method": "PUT"}) != fingerprint
    # Query parameter ordering does not change the fingerprint (canonical).
    left = request_fingerprint(**{**base, "query": {"b": "2", "a": "1"}})
    right = request_fingerprint(**{**base, "query": {"a": "1", "b": "2"}})
    assert left == right
    # Trailing/duplicate slashes normalise; query strings embedded in path are ignored.
    assert request_fingerprint(**{**base, "path": "//api//v1/items/?x=1"}) == request_fingerprint(**base)


def test_key_validation_and_scoping() -> None:
    key = validate_idempotency_key("abc-123:def.ghi")
    assert key == "abc-123:def.ghi"
    with pytest.raises(InvalidIdempotencyKey):
        validate_idempotency_key("")
    with pytest.raises(InvalidIdempotencyKey):
        validate_idempotency_key("bad key with spaces")
    with pytest.raises(InvalidIdempotencyKey):
        validate_idempotency_key("x" * 256)
    assert scoped_key(tenant_id="tenant-1", endpoint_id="items.create", key=key) == "tenant-1:items.create:abc-123:def.ghi"


def test_decision_table_replay_conflict_in_progress_and_expiry() -> None:
    clock = StepClock()
    fingerprint = request_fingerprint(method="POST", path="/x", tenant_id="t", endpoint_id="e", body={"a": 1})
    record = begin_record(key="key-1", request_hash=fingerprint, now=clock.now())
    assert record.status is IdempotencyStatus.IN_PROGRESS
    assert decide_idempotency(None, request_hash=fingerprint, now=clock.now()).outcome is IdempotencyOutcome.PROCEED

    in_progress = decide_idempotency(record, request_hash=fingerprint, now=clock.now())
    assert in_progress.outcome is IdempotencyOutcome.IN_PROGRESS
    with pytest.raises(IdempotencyInProgress) as error:
        in_progress.raise_for_conflict()
    assert error.value.details["retry_after_seconds"] == 1

    completed = complete_record(record, response_status=201, response_body={"id": "1"}, resource_id="1", now=clock.advance(2))
    replay = decide_idempotency(completed, request_hash=fingerprint, now=clock.now())
    assert replay.outcome is IdempotencyOutcome.REPLAY
    assert replay.record is not None and replay.record.response_status == 201
    replay.raise_for_conflict()  # replay is not an error

    conflict = decide_idempotency(completed, request_hash="different", now=clock.now())
    assert conflict.outcome is IdempotencyOutcome.CONFLICT
    with pytest.raises(IdempotencyConflict):
        conflict.raise_for_conflict()

    clock.advance(7 * 24 * 60 * 60 + 1)
    assert decide_idempotency(completed, request_hash="different", now=clock.now()).outcome is IdempotencyOutcome.PROCEED


def test_complete_record_validation() -> None:
    clock = StepClock()
    record = begin_record(key="k", request_hash="h", now=clock.now())
    with pytest.raises(ValueError):
        complete_record(record, response_status=99, now=clock.now())
    with pytest.raises(ValueError):
        begin_record(key="k", request_hash="h", now=clock.now(), ttl_seconds=0)
