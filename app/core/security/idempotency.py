"""Idempotency-key canonicalisation, request fingerprinting and conflict decisions (pure).

Implements FS-00 §5/§6 (`idempotency_key`, DM-PLAT-06) and ARCHITECTURE §2.4.1 step 2:

* the key is scoped by ``(tenant, endpoint, key)`` and validated as a bounded token;
* the request body is canonicalised deterministically (sorted keys, decimal strings, UTC timestamps) and
  SHA-256 fingerprinted together with method/path/tenant/endpoint;
* replay-safe decision table: no/expired record → proceed; same key+hash completed → replay; same key with
  a different hash → ``409 IDEMPOTENCY_KEY_REUSED``; in-flight → ``409 IDEMPOTENCY_IN_PROGRESS``.

Pure module: no DB/network. Durable adapters map :class:`IdempotencyRecord` to
``platform.idempotency_key``; the in-memory helpers are for unit tests and middleware tests.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any
from uuid import UUID

from app.core.security.errors import IdempotencyConflict, IdempotencyInProgress, InvalidIdempotencyKey

IDEMPOTENCY_TTL_SECONDS = 7 * 24 * 60 * 60
MAX_KEY_LENGTH = 255
_KEY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,254}$")


def utc_now() -> datetime:
    return datetime.now(UTC)


def _ensure_utc(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        raise ValueError("idempotency timestamps must be timezone-aware (UTC)")
    return moment.astimezone(UTC)


# --------------------------------------------------------------------------- canonicalisation ---


def canonicalize(value: Any) -> Any:
    """Return a deterministic, JSON-safe representation used only for request fingerprinting.

    * mappings: keys sorted (string keys required);
    * sequences: order preserved;
    * ``Decimal`` → normalised plain decimal string (never float);
    * ``datetime`` → UTC ISO-8601 with ``Z``; ``date`` → ISO date;
    * ``UUID``/``Enum`` → string values; bytes → base64 text;
    * unsupported types raise ``TypeError`` (no silent ``repr()`` that could hide a fingerprint bug).
    """
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, Decimal):
        normalised = value.normalize()
        if normalised == normalised.to_integral_value() and normalised == 0:
            return "0"
        return format(normalised, "f")
    if isinstance(value, datetime):
        return _ensure_utc(value).isoformat(timespec="microseconds").replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise TypeError("canonical JSON mapping keys must be strings")
        return {key: canonicalize(value[key]) for key in sorted(value)}  # type: ignore[index]
    if isinstance(value, Sequence) and not isinstance(value, bytes | bytearray | str):
        return [canonicalize(item) for item in value]
    if isinstance(value, bytes | bytearray):
        import base64

        return base64.b64encode(bytes(value)).decode("ascii")
    raise TypeError(f"cannot canonicalize value of type {type(value).__name__}")


def canonical_json(value: Any) -> str:
    """Canonical compact JSON text (sorted keys, UTF-8, no insignificant whitespace)."""
    return json.dumps(canonicalize(value), ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def sha256_hex(payload: str | bytes) -> str:
    data = payload.encode("utf-8") if isinstance(payload, str) else payload
    return hashlib.sha256(data).hexdigest()


# --------------------------------------------------------------------------- keys/fingerprint ---


def validate_idempotency_key(key: str) -> str:
    """Validate and return the key (max 255 chars; letters/digits/``._:-``; no whitespace/controls)."""
    if not isinstance(key, str) or not key:
        raise InvalidIdempotencyKey("Idempotency-Key is required")
    if len(key) > MAX_KEY_LENGTH:
        raise InvalidIdempotencyKey(f"Idempotency-Key exceeds {MAX_KEY_LENGTH} characters")
    if not _KEY_PATTERN.match(key):
        raise InvalidIdempotencyKey("Idempotency-Key contains invalid characters")
    return key


def scoped_key(*, tenant_id: str | UUID, endpoint_id: str, key: str) -> str:
    """Storage key for ``(tenant, endpoint, key)`` (unique index per DM-PLAT-06)."""
    return f"{tenant_id}:{endpoint_id}:{validate_idempotency_key(key)}"


def request_fingerprint(
    *,
    method: str,
    path: str,
    tenant_id: str | UUID,
    endpoint_id: str,
    body: Any = None,
    query: Mapping[str, Any] | None = None,
) -> str:
    """SHA-256 fingerprint of the canonical request identity (used to compare idempotent retries)."""
    normalised_method = method.upper().strip()
    if not normalised_method:
        raise ValueError("method is required")
    path_without_query = path.split("?", 1)[0]
    normalised_path = re.sub(r"/{2,}", "/", "/" + path_without_query.strip("/")) if path_without_query.strip("/") else "/"
    payload = {
        "method": normalised_method,
        "path": normalised_path,
        "tenant_id": str(tenant_id),
        "endpoint_id": endpoint_id,
        "query": canonicalize(dict(query or {})),
        "body": canonicalize(body),
    }
    return sha256_hex(canonical_json(payload))


# --------------------------------------------------------------------------- lifecycle ----------


class IdempotencyStatus(StrEnum):
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"


class IdempotencyOutcome(StrEnum):
    PROCEED = "PROCEED"
    REPLAY = "REPLAY"
    CONFLICT = "CONFLICT"
    IN_PROGRESS = "IN_PROGRESS"


@dataclass(frozen=True, slots=True)
class IdempotencyRecord:
    """Durable idempotency row as seen by the pure decision function."""

    key: str
    request_hash: str
    status: IdempotencyStatus
    created_at: datetime
    expires_at: datetime
    response_status: int | None = None
    response_body: Mapping[str, Any] | None = None
    resource_id: str | None = None
    completed_at: datetime | None = None

    def is_expired(self, now: datetime) -> bool:
        return _ensure_utc(now) >= _ensure_utc(self.expires_at)


@dataclass(frozen=True, slots=True)
class IdempotencyDecision:
    outcome: IdempotencyOutcome
    record: IdempotencyRecord | None = None
    retry_after_seconds: int | None = None

    @property
    def replay(self) -> bool:
        return self.outcome is IdempotencyOutcome.REPLAY

    def raise_for_conflict(self) -> None:
        """Raise the typed error for CONFLICT/IN_PROGRESS outcomes (replay/proceed return normally)."""
        if self.outcome is IdempotencyOutcome.CONFLICT:
            raise IdempotencyConflict("Idempotency-Key was reused with a different request")
        if self.outcome is IdempotencyOutcome.IN_PROGRESS:
            raise IdempotencyInProgress(
                "a request with this Idempotency-Key is still in progress",
                details={"retry_after_seconds": self.retry_after_seconds or 1},
            )


def begin_record(
    *,
    key: str,
    request_hash: str,
    now: datetime,
    ttl_seconds: int = IDEMPOTENCY_TTL_SECONDS,
) -> IdempotencyRecord:
    """Build the row to insert before executing the side effect (status IN_PROGRESS)."""
    if ttl_seconds <= 0:
        raise ValueError("ttl_seconds must be positive")
    issued_at = _ensure_utc(now)
    from datetime import timedelta

    return IdempotencyRecord(
        key=validate_idempotency_key(key),
        request_hash=request_hash,
        status=IdempotencyStatus.IN_PROGRESS,
        created_at=issued_at,
        expires_at=issued_at + timedelta(seconds=ttl_seconds),
    )


def complete_record(
    record: IdempotencyRecord,
    *,
    response_status: int,
    response_body: Mapping[str, Any] | None = None,
    resource_id: str | None = None,
    now: datetime,
) -> IdempotencyRecord:
    """Return the completed row (persist status/response; never re-execute the side effect)."""
    if not 100 <= int(response_status) <= 599:
        raise ValueError("response_status must be a valid HTTP status")
    return IdempotencyRecord(
        key=record.key,
        request_hash=record.request_hash,
        status=IdempotencyStatus.COMPLETED,
        created_at=record.created_at,
        expires_at=record.expires_at,
        response_status=int(response_status),
        response_body=dict(response_body) if response_body is not None else None,
        resource_id=resource_id,
        completed_at=_ensure_utc(now),
    )


def decide_idempotency(
    record: IdempotencyRecord | None,
    *,
    request_hash: str,
    now: datetime,
    ttl_seconds: int = IDEMPOTENCY_TTL_SECONDS,
    in_progress_retry_after_seconds: int = 1,
) -> IdempotencyDecision:
    """Pure decision table for an incoming request with an ``Idempotency-Key``.

    * no record / expired → PROCEED (caller inserts a fresh IN_PROGRESS row);
    * same key + same request hash + COMPLETED → REPLAY (return stored response);
    * same key + different request hash → CONFLICT (`409 IDEMPOTENCY_KEY_REUSED`);
    * same key + IN_PROGRESS (unexpired) → IN_PROGRESS (`409 IDEMPOTENCY_IN_PROGRESS` + Retry-After).
    """
    if record is None or record.is_expired(now):
        return IdempotencyDecision(outcome=IdempotencyOutcome.PROCEED)
    if record.status is IdempotencyStatus.IN_PROGRESS:
        return IdempotencyDecision(
            outcome=IdempotencyOutcome.IN_PROGRESS,
            record=record,
            retry_after_seconds=max(1, int(in_progress_retry_after_seconds)),
        )
    if record.request_hash != request_hash:
        return IdempotencyDecision(outcome=IdempotencyOutcome.CONFLICT, record=record)
    return IdempotencyDecision(outcome=IdempotencyOutcome.REPLAY, record=record)


@dataclass
class InMemoryIdempotencyStore:
    """Tiny deterministic store for unit tests (durable adapter is owned by the DB layer)."""

    records: dict[str, IdempotencyRecord] = field(default_factory=dict)

    def get(self, key: str) -> IdempotencyRecord | None:
        return self.records.get(key)

    def put(self, record: IdempotencyRecord) -> None:
        self.records[record.key] = record

    def purge_expired(self, now: datetime) -> int:
        expired = [key for key, record in self.records.items() if record.is_expired(now)]
        for key in expired:
            del self.records[key]
        return len(expired)


__all__ = [
    "IDEMPOTENCY_TTL_SECONDS",
    "MAX_KEY_LENGTH",
    "IdempotencyDecision",
    "IdempotencyOutcome",
    "IdempotencyRecord",
    "IdempotencyStatus",
    "InMemoryIdempotencyStore",
    "begin_record",
    "canonical_json",
    "canonicalize",
    "complete_record",
    "decide_idempotency",
    "request_fingerprint",
    "scoped_key",
    "sha256_hex",
    "utc_now",
    "validate_idempotency_key",
]
