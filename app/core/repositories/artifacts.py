"""FS-00 artifact metadata repository (task-44): attachments, document links, finance intents.

Async SQLAlchemy 2.0 Core access to the three FS-00 tables that back the platform-core artifact
surface (FS-00 §4, §5.5, §6):

* ``platform.attachment_metadata`` — file metadata only (no blob in the DB). The storage adapter is
  abstracted behind :class:`UploadSignerPort`; the default :class:`NullUploadSigner` performs **no S3
  call** and returns no presigned URL (S0-02/local-dev placeholder; a real adapter is injected by the
  deployment later). Metadata rows are tenant-scoped and audited.
* ``platform.document_link`` — typed, append-only provenance graph (``DM-PLAT-02``: UPDATE/DELETE are
  rejected by a database trigger for every role). Creation is idempotent by its natural key; deletion is
  *guarded*: this module records the rejected attempt and raises :class:`DocumentLinkAppendOnly` instead
  of touching the row.
* ``platform.finance_journal_intent`` — **D2** append-only journal-intent store. This module only reads
  it (``GET /api/v1/finance/journal-intents[...]``); posting/GL writes remain behind the finance port
  and the ``finance_adapter`` role (``DM-PLAT-11``/``RULE-PLAT-11``). There is intentionally no mutation
  method here.

The module is HTTP-free: domain errors (:class:`ArtifactsError`) are mapped to problem+json by
:mod:`app.api.routers.platform.artifacts`. No commits happen here; the request-scoped session owns the
transaction (``get_db``/``session_scope``). Every mutating operation appends a ``platform.audit_event``
row in the same transaction.

Pure validation/serialisation helpers live in this module so ``app/tests/unit/services_artifacts`` can
exercise them without a database.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol, runtime_checkable
from uuid import UUID, uuid4

from sqlalchemy import and_, delete, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.models import platform as p
from app.core.security.errors import IdempotencyConflict, IdempotencyInProgress
from app.core.security.idempotency import (
    IDEMPOTENCY_TTL_SECONDS,
    request_fingerprint,
    validate_idempotency_key,
)

Clock = Callable[[], datetime]


def _now() -> datetime:
    return datetime.now(UTC)


# ---------------------------------------------------------------------------- constants ----------
DEFAULT_MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024  # FS-00 §8 platform.attachments.max_size_mb default 25
DEFAULT_ALLOWED_CONTENT_TYPES = frozenset(
    {
        "application/pdf",
        "image/png",
        "image/jpeg",
        "text/csv",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    }
)
DEFAULT_PAGE_LIMIT = 50
MAX_PAGE_LIMIT = 500
MAX_FILENAME_LENGTH = 255
MAX_STORAGE_KEY_LENGTH = 512
STORAGE_PREFIX = "tenant"
DEFAULT_UPLOAD_TTL_SECONDS = 900

ENTITY_TYPE_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,63}$")
LINK_TYPE_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")
CHECKSUM_PATTERN = re.compile(r"^(?:(sha256|sha1|md5):)?([0-9A-Fa-f]{32,128})$")
SAFE_FILENAME_PATTERN = re.compile(r"^[^/\\\x00-\x1f]{1,255}$")
JOURNAL_INTENT_STATUSES = frozenset({"RECORDED", "ACKNOWLEDGED", "REJECTED", "SUPERSEDED"})
IDEMPOTENCY_ENDPOINT_ATTACHMENTS = "platform.attachments.create"


# ------------------------------------------------------------------------------- errors ----------
class ArtifactsError(ValueError):
    """Domain error mapped to a problem+json response by the router (HTTP-free here)."""

    code: str = "ARTIFACTS_INVALID"
    status_code: int = 422

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message or self.code)
        if code:
            self.code = code
        self.message = message or self.code
        self.details: dict[str, Any] = dict(details or {})


class ArtifactsValidation(ArtifactsError):
    """Request validation failure (422)."""

    code = "VALIDATION_ERROR"


class AttachmentNotFound(ArtifactsError):
    code = "ATTACHMENT_NOT_FOUND"
    status_code = 404


class DocumentLinkNotFound(ArtifactsError):
    code = "DOCUMENT_LINK_NOT_FOUND"
    status_code = 404


class JournalIntentNotFound(ArtifactsError):
    code = "JOURNAL_INTENT_NOT_FOUND"
    status_code = 404


class ArtifactsConflict(ArtifactsError):
    code = "CONFLICT"
    status_code = 409


class DocumentLinkAppendOnly(ArtifactsError):
    """``document_link`` is append-only (DM-PLAT-02); deletion is refused, never executed."""

    code = "DOCUMENT_LINK_APPEND_ONLY"
    status_code = 409


class InvalidCursor(ArtifactsError):
    code = "INVALID_CURSOR"
    status_code = 422


# ------------------------------------------------------------------- storage port (placeholder) --
@dataclass(frozen=True, slots=True)
class PresignedUpload:
    """Presigned-upload instruction returned to the client by a real storage adapter."""

    provider: str
    storage_key: str
    method: str = "PUT"
    url: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    expires_in_seconds: int = DEFAULT_UPLOAD_TTL_SECONDS


@runtime_checkable
class UploadSignerPort(Protocol):
    """Object-storage port: presigned upload/download hand-off (no blob bytes cross this module)."""

    @property
    def provider(self) -> str:
        """Stable provider identifier (``null`` for the metadata-only adapter)."""

    def sign_upload(
        self,
        *,
        tenant_id: UUID,
        storage_key: str,
        content_type: str | None,
        size_bytes: int,
        expires_in_seconds: int = DEFAULT_UPLOAD_TTL_SECONDS,
    ) -> PresignedUpload | None:
        """Return a presigned PUT instruction, or ``None`` when no object store is configured."""

    def sign_download(
        self,
        *,
        tenant_id: UUID,
        storage_key: str,
        expires_in_seconds: int = DEFAULT_UPLOAD_TTL_SECONDS,
    ) -> str | None:
        """Return a presigned GET URL, or ``None`` when no object store is configured."""


@dataclass(frozen=True, slots=True)
class NullUploadSigner:
    """No-op storage adapter: metadata-only registration (no S3 call, no presigned URL).

    This is the Phase-1/local-dev placeholder. A production adapter (MinIO/S3) implements the same
    protocol and is injected through the router's ``get_upload_signer`` dependency.
    """

    provider: str = "null"

    def sign_upload(
        self,
        *,
        tenant_id: UUID,
        storage_key: str,
        content_type: str | None,
        size_bytes: int,
        expires_in_seconds: int = DEFAULT_UPLOAD_TTL_SECONDS,
    ) -> PresignedUpload | None:
        return None

    def sign_download(
        self,
        *,
        tenant_id: UUID,
        storage_key: str,
        expires_in_seconds: int = DEFAULT_UPLOAD_TTL_SECONDS,
    ) -> str | None:
        return None


# ----------------------------------------------------------------- assistive coercions -------
def _as_uuid(value: object, field_name: str) -> UUID:
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except (TypeError, ValueError) as exc:
        raise ArtifactsValidation(
            f"{field_name} must be a UUID", details={"field": field_name}
        ) from exc


def _require_text(value: object, field_name: str, *, max_length: int, pattern: re.Pattern[str] | None = None) -> str:
    if not isinstance(value, str):
        raise ArtifactsValidation(f"{field_name} must be a string", details={"field": field_name})
    text = value.strip()
    if not text:
        raise ArtifactsValidation(f"{field_name} must not be empty", details={"field": field_name})
    if len(text) > max_length:
        raise ArtifactsValidation(
            f"{field_name} exceeds {max_length} characters", details={"field": field_name}
        )
    if pattern is not None and not pattern.match(text):
        raise ArtifactsValidation(f"{field_name} has an invalid format", details={"field": field_name})
    return text


def _optional_text(value: object, field_name: str, *, max_length: int) -> str | None:
    if value is None:
        return None
    return _require_text(value, field_name, max_length=max_length)


def _as_non_negative_decimal(value: object, field_name: str) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, bool) or isinstance(value, float):
        raise ArtifactsValidation(f"{field_name} must be a decimal string/number", details={"field": field_name})
    try:
        decimal_value = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ArtifactsValidation(f"{field_name} must be a decimal", details={"field": field_name}) from exc
    if not decimal_value.is_finite() or decimal_value < 0:
        raise ArtifactsValidation(
            f"{field_name} must be a non-negative decimal", details={"field": field_name}
        )
    return decimal_value


def _decimal_str(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return format(value, "f")
    return str(value)


def _iso(value: object) -> object:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat() if value.tzinfo else value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    return value


def serialize_row(row: Mapping[str, Any], *, fields: Iterable[str] | None = None) -> dict[str, Any]:
    """JSON-safe serialisation of a platform row (UUID/date/datetime/Decimal → strings)."""
    selected = fields or row.keys()
    return {key: _iso(row[key]) for key in selected if key in row}


# ------------------------------------------------------------------ attachment validation -------
def normalize_storage_key(*, tenant_id: UUID, entity_type: str, entity_id: UUID, filename: str) -> str:
    """Build a tenant-prefixed object key (FS-00 §5.5: tenant prefix is mandatory)."""
    safe_name = filename.replace(" ", "_")
    return (
        f"{STORAGE_PREFIX}/{tenant_id}/{entity_type.lower()}/{entity_id}/"
        f"{uuid4().hex}/{safe_name}"
    )


def validate_storage_key(*, tenant_id: UUID, storage_key: str) -> str:
    key = _require_text(storage_key, "storage_key", max_length=MAX_STORAGE_KEY_LENGTH)
    prefix = f"{STORAGE_PREFIX}/{tenant_id}/"
    if not key.startswith(prefix):
        raise ArtifactsValidation(
            "storage_key must be tenant-prefixed", details={"field": "storage_key", "expected_prefix": prefix}
        )
    if ".." in key or "\\" in key:
        raise ArtifactsValidation("storage_key contains invalid path segments", details={"field": "storage_key"})
    return key


def normalize_checksum(value: object) -> str | None:
    if value is None:
        return None
    text = _require_text(value, "checksum", max_length=200).lower()
    match = CHECKSUM_PATTERN.match(text)
    if not match:
        raise ArtifactsValidation(
            "checksum must be a hex digest (optionally prefixed sha256:/sha1:/md5:)",
            details={"field": "checksum"},
        )
    algorithm, digest = match.group(1), match.group(2)
    if algorithm is None:
        algorithm = "sha256" if len(digest) == 64 else "sha1" if len(digest) == 40 else "md5"
    return f"{algorithm}:{digest}"


def normalize_attachment_payload(
    payload: Mapping[str, Any],
    *,
    tenant_id: UUID,
    max_size_bytes: int = DEFAULT_MAX_ATTACHMENT_BYTES,
    allowed_content_types: Iterable[str] = DEFAULT_ALLOWED_CONTENT_TYPES,
) -> dict[str, Any]:
    """Validate an attachment-registration payload and return the DB column values."""
    if not isinstance(payload, Mapping):
        raise ArtifactsValidation("attachment payload must be an object")
    entity_type = _require_text(payload.get("entity_type"), "entity_type", max_length=64, pattern=ENTITY_TYPE_PATTERN)
    entity_id = _as_uuid(payload.get("entity_id"), "entity_id")
    filename = _require_text(payload.get("filename"), "filename", max_length=MAX_FILENAME_LENGTH)
    if not SAFE_FILENAME_PATTERN.match(filename):
        raise ArtifactsValidation(
            "filename must not contain path separators or control characters", details={"field": "filename"}
        )
    content_type = _optional_text(payload.get("content_type"), "content_type", max_length=255)
    if content_type is not None:
        content_type = content_type.lower()
        allowed = {item.lower() for item in allowed_content_types}
        if content_type not in allowed:
            raise ArtifactsValidation(
                "content_type is not allowed", details={"field": "content_type", "allowed_types": sorted(allowed)}
            )
    size_raw = payload.get("size_bytes", 0)
    if isinstance(size_raw, bool) or not isinstance(size_raw, int):
        raise ArtifactsValidation("size_bytes must be an integer", details={"field": "size_bytes"})
    if size_raw < 0 or size_raw > max_size_bytes:
        raise ArtifactsValidation(
            "size_bytes is outside the allowed range",
            details={"field": "size_bytes", "min": 0, "max": max_size_bytes},
        )
    raw_storage_key = payload.get("storage_key", payload.get("storage_ref"))
    storage_key = (
        validate_storage_key(tenant_id=tenant_id, storage_key=str(raw_storage_key))
        if raw_storage_key is not None
        else normalize_storage_key(
            tenant_id=tenant_id, entity_type=entity_type, entity_id=entity_id, filename=filename
        )
    )
    checksum = normalize_checksum(payload.get("checksum"))
    is_quality_image = payload.get("is_quality_image", False)
    if not isinstance(is_quality_image, bool):
        raise ArtifactsValidation("is_quality_image must be a boolean", details={"field": "is_quality_image"})
    return {
        "entity_type": entity_type,
        "entity_id": entity_id,
        "filename": filename,
        "content_type": content_type,
        "size_bytes": size_raw,
        "storage_key": storage_key,
        "checksum": checksum,
        "is_quality_image": is_quality_image,
    }


def serialize_attachment(
    row: Mapping[str, Any], *, download_url: str | None = None, upload: PresignedUpload | None = None
) -> dict[str, Any]:
    payload = {
        "id": _iso(row["id"]),
        "entity_type": row["entity_type"],
        "entity_id": _iso(row["entity_id"]),
        "filename": row["filename"],
        "content_type": row.get("content_type"),
        "size_bytes": row.get("size_bytes"),
        "storage_key": row["storage_key"],
        # Task wording uses ``storage_ref``; expose it as an alias without duplicating storage.
        "storage_ref": row["storage_key"],
        "checksum": row.get("checksum"),
        "is_quality_image": bool(row.get("is_quality_image", False)),
        "uploaded_by": _iso(row.get("uploaded_by")) if row.get("uploaded_by") else None,
        "created_at": _iso(row.get("created_at")),
        "updated_at": _iso(row.get("updated_at")),
        "version": row.get("version", 1),
    }
    if download_url is not None:
        payload["download_url"] = download_url
    if upload is not None:
        payload["upload"] = {
            "provider": upload.provider,
            "method": upload.method,
            "url": upload.url,
            "headers": dict(upload.headers),
            "expires_in_seconds": upload.expires_in_seconds,
            "storage_key": upload.storage_key,
        }
    return payload


# --------------------------------------------------------------- document-link validation ------
def normalize_document_link_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a provenance-link payload; links are append-only and idempotent by natural key."""
    if not isinstance(payload, Mapping):
        raise ArtifactsValidation("document-link payload must be an object")
    link_type = _require_text(payload.get("link_type"), "link_type", max_length=64, pattern=LINK_TYPE_PATTERN)
    from_type = _require_text(payload.get("from_type"), "from_type", max_length=64, pattern=ENTITY_TYPE_PATTERN)
    to_type = _require_text(payload.get("to_type"), "to_type", max_length=64, pattern=ENTITY_TYPE_PATTERN)
    from_id = _as_uuid(payload.get("from_id"), "from_id")
    to_id = _as_uuid(payload.get("to_id"), "to_id")
    if from_type == to_type and from_id == to_id:
        raise ArtifactsValidation("a document cannot be linked to itself", details={"field": "to_id"})
    qty = _as_non_negative_decimal(payload.get("qty"), "qty")
    amount_base = _as_non_negative_decimal(payload.get("amount_base"), "amount_base")
    return {
        "link_type": link_type,
        "from_type": from_type,
        "from_id": from_id,
        "to_type": to_type,
        "to_id": to_id,
        "qty": qty,
        "amount_base": amount_base,
    }


DOCUMENT_LINK_FIELDS = (
    "id",
    "link_type",
    "from_type",
    "from_id",
    "to_type",
    "to_id",
    "qty",
    "amount_base",
    "created_at",
    "created_by",
)


def serialize_document_link(row: Mapping[str, Any]) -> dict[str, Any]:
    payload = serialize_row(row, fields=DOCUMENT_LINK_FIELDS)
    payload["qty"] = _decimal_str(row.get("qty"))
    payload["amount_base"] = _decimal_str(row.get("amount_base"))
    return payload


# --------------------------------------------------------------------- finance-intent read ------
JOURNAL_INTENT_FIELDS = (
    "id",
    "intent_line_no",
    "inventory_transaction_id",
    "ledger_sequence",
    "idempotency_key",
    "source_doc_type",
    "source_doc_id",
    "posting_rule_id",
    "account_ref",
    "debit_amount_base",
    "credit_amount_base",
    "amount_txn",
    "currency_code",
    "exchange_rate",
    "qty",
    "uom_id",
    "segments",
    "status",
    "recorded_at",
    "acknowledged_at",
)


def serialize_journal_intent(row: Mapping[str, Any], *, include_payload: bool = False) -> dict[str, Any]:
    payload = serialize_row(row, fields=JOURNAL_INTENT_FIELDS)
    for money_field in ("debit_amount_base", "credit_amount_base", "amount_txn", "exchange_rate", "qty"):
        payload[money_field] = _decimal_str(row.get(money_field))
    if include_payload:
        payload["intent_payload"] = row.get("intent_payload") or {}
    return payload


def encode_cursor(recorded_at: datetime, intent_id: UUID) -> str:
    """Opaque keyset cursor for ``recorded_at DESC, id DESC`` journal-intent paging."""
    moment = recorded_at.astimezone(UTC) if recorded_at.tzinfo else recorded_at.replace(tzinfo=UTC)
    raw = json.dumps({"recorded_at": moment.isoformat(), "id": str(intent_id)}, separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii").rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, UUID]:
    if not isinstance(cursor, str) or not cursor:
        raise InvalidCursor("cursor must be a non-empty string")
    padded = cursor + "=" * (-len(cursor) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
        decoded = json.loads(raw)
        recorded_at = datetime.fromisoformat(str(decoded["recorded_at"]))
        intent_id = UUID(str(decoded["id"]))
    except (binascii.Error, UnicodeDecodeError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        raise InvalidCursor("cursor is malformed") from exc
    if recorded_at.tzinfo is None:
        recorded_at = recorded_at.replace(tzinfo=UTC)
    return recorded_at.astimezone(UTC), intent_id


def validate_journal_filters(
    *,
    transaction_id: UUID | None,
    source_doc_type: str | None,
    source_doc_id: UUID | None,
    posting_rule_id: str | None,
    status: str | None,
    date_from: date | None,
    date_to: date | None,
) -> dict[str, Any]:
    """Validate/normalise journal-intent read filters (read-only; no mutation possible here)."""
    normalised_status: str | None = None
    if status is not None:
        candidate = status.strip().upper()
        if candidate not in JOURNAL_INTENT_STATUSES:
            raise ArtifactsValidation(
                "status filter is invalid",
                details={"field": "status", "allowed": sorted(JOURNAL_INTENT_STATUSES)},
            )
        normalised_status = candidate
    if date_from is not None and date_to is not None and date_from > date_to:
        raise ArtifactsValidation(
            "from must be on or before to", details={"field": "from"}
        )
    from_ts = datetime.combine(date_from, datetime.min.time(), tzinfo=UTC) if date_from else None
    to_ts = datetime.combine(date_to + timedelta(days=1), datetime.min.time(), tzinfo=UTC) if date_to else None
    return {
        "transaction_id": transaction_id,
        "source_doc_type": source_doc_type.strip() if source_doc_type else None,
        "source_doc_id": source_doc_id,
        "posting_rule_id": posting_rule_id.strip() if posting_rule_id else None,
        "status": normalised_status,
        "from_ts": from_ts,
        "to_ts": to_ts,
    }


def clamp_limit(limit: int) -> int:
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise ArtifactsValidation("limit must be an integer", details={"field": "limit"})
    if limit < 1 or limit > MAX_PAGE_LIMIT:
        raise ArtifactsValidation(
            f"limit must be between 1 and {MAX_PAGE_LIMIT}", details={"field": "limit"}
        )
    return limit


# ------------------------------------------------------------------------- write results --------
@dataclass(frozen=True, slots=True)
class AttachmentWriteResult:
    """Outcome of an attachment registration (idempotency replay returns the stored response)."""

    attachment: dict[str, Any] | None
    upload: PresignedUpload | None
    response_status: int = 201
    replay_body: dict[str, Any] | None = None
    replayed: bool = False


@dataclass(frozen=True, slots=True)
class DocumentLinkWriteResult:
    link: dict[str, Any]
    created: bool


# --------------------------------------------------------------------------- repository ---------
class ArtifactsRepository:
    """Request-scoped, tenant-scoped repository for attachments/links/journal-intent reads."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        signer: UploadSignerPort | None = None,
        max_attachment_bytes: int = DEFAULT_MAX_ATTACHMENT_BYTES,
        allowed_content_types: Iterable[str] = DEFAULT_ALLOWED_CONTENT_TYPES,
        clock: Clock = _now,
    ) -> None:
        self.session = session
        self.signer: UploadSignerPort = signer or NullUploadSigner()
        self.max_attachment_bytes = max_attachment_bytes
        self.allowed_content_types = tuple(allowed_content_types)
        self._clock = clock

    # ------------------------------------------------------------------ audit ---------------
    async def _audit(
        self,
        *,
        tenant_id: UUID,
        actor_id: UUID | None,
        action: str,
        resource_type: str,
        resource_id: str | None,
        diff: Mapping[str, Any] | None = None,
    ) -> None:
        event_id = uuid4()
        await self.session.execute(
            insert(p.audit_event).values(
                id=event_id,
                tenant_id=tenant_id,
                event_id=event_id,
                actor_type="USER",
                actor_id=actor_id,
                action=action,
                resource_type=resource_type,
                resource_id=resource_id,
                diff=dict(diff or {}),
                source="API",
                occurred_at=self._clock(),
            )
        )

    # ------------------------------------------------------- attachments: create -----------
    async def _check_idempotency(
        self,
        *,
        tenant_id: UUID,
        idempotency_key: str,
        request_hash: str,
    ) -> dict[str, Any] | None:
        """Return the stored replay row when the key completed earlier; manage the in-progress row."""
        key = validate_idempotency_key(idempotency_key)
        now = self._clock()
        result = await self.session.execute(
            select(p.idempotency_key).where(
                p.idempotency_key.c.tenant_id == tenant_id,
                p.idempotency_key.c.endpoint_id == IDEMPOTENCY_ENDPOINT_ATTACHMENTS,
                p.idempotency_key.c.key_value == key,
            )
        )
        existing = result.mappings().first()
        if existing is None:
            await self.session.execute(
                insert(p.idempotency_key).values(
                    id=uuid4(),
                    tenant_id=tenant_id,
                    endpoint_id=IDEMPOTENCY_ENDPOINT_ATTACHMENTS,
                    key_value=key,
                    request_hash=request_hash,
                    status="IN_PROGRESS",
                    expires_at=now + timedelta(seconds=IDEMPOTENCY_TTL_SECONDS),
                    created_by=None,
                    updated_by=None,
                )
            )
            return None
        if existing["request_hash"] != request_hash:
            raise IdempotencyConflict("Idempotency-Key was reused with a different request")
        if existing["status"] == "COMPLETED":
            return dict(existing)
        if existing["expires_at"] is not None and existing["expires_at"] > now:
            raise IdempotencyInProgress("a request with this Idempotency-Key is still in progress")
        # Expired in-progress row: safe to reuse the key for a fresh attempt.
        await self.session.execute(
            update(p.idempotency_key)
            .where(p.idempotency_key.c.id == existing["id"])
            .values(request_hash=request_hash, status="IN_PROGRESS", response_status=None,
                    response_body=None, resource_id=None,
                    expires_at=now + timedelta(seconds=IDEMPOTENCY_TTL_SECONDS))
        )
        return None

    async def _complete_idempotency(
        self, *, tenant_id: UUID, idempotency_key: str, response_status: int,
        response_body: Mapping[str, Any], resource_id: UUID,
    ) -> None:
        key = validate_idempotency_key(idempotency_key)
        await self.session.execute(
            update(p.idempotency_key)
            .where(
                p.idempotency_key.c.tenant_id == tenant_id,
                p.idempotency_key.c.endpoint_id == IDEMPOTENCY_ENDPOINT_ATTACHMENTS,
                p.idempotency_key.c.key_value == key,
            )
            .values(status="COMPLETED", response_status=response_status,
                    response_body=dict(response_body), resource_id=resource_id)
        )

    async def create_attachment(
        self,
        *,
        tenant_id: str | UUID,
        actor_id: str | UUID | None,
        payload: Mapping[str, Any],
        idempotency_key: str,
        request_path: str = "/api/v1/attachments",
    ) -> AttachmentWriteResult:
        """Register attachment metadata (idempotent; presigned upload from the injected port)."""
        tenant_uuid = _as_uuid(tenant_id, "tenant_id")
        actor_uuid = _as_uuid(actor_id, "actor_id") if actor_id is not None else None
        values = normalize_attachment_payload(
            payload,
            tenant_id=tenant_uuid,
            max_size_bytes=self.max_attachment_bytes,
            allowed_content_types=self.allowed_content_types,
        )
        request_hash = request_fingerprint(
            method="POST",
            path=request_path,
            tenant_id=tenant_uuid,
            endpoint_id=IDEMPOTENCY_ENDPOINT_ATTACHMENTS,
            body=payload,
        )
        replay_row = await self._check_idempotency(
            tenant_id=tenant_uuid, idempotency_key=idempotency_key, request_hash=request_hash
        )
        if replay_row is not None:
            stored_body = dict(replay_row.get("response_body") or {})
            return AttachmentWriteResult(
                attachment=None,
                upload=None,
                response_status=int(replay_row.get("response_status") or 201),
                replay_body=stored_body,
                replayed=True,
            )

        attachment_id = uuid4()
        try:
            async with self.session.begin_nested():
                await self.session.execute(
                    insert(p.attachment_metadata).values(
                        id=attachment_id,
                        tenant_id=tenant_uuid,
                        entity_type=values["entity_type"],
                        entity_id=values["entity_id"],
                        filename=values["filename"],
                        content_type=values["content_type"],
                        size_bytes=values["size_bytes"],
                        storage_key=values["storage_key"],
                        checksum=values["checksum"],
                        uploaded_by=actor_uuid,
                        is_quality_image=values["is_quality_image"],
                        created_by=actor_uuid,
                        updated_by=actor_uuid,
                    )
                )
        except IntegrityError as exc:
            raise ArtifactsConflict(
                "an attachment with this storage key already exists",
                code="ATTACHMENT_STORAGE_KEY_CONFLICT",
            ) from exc

        upload = self.signer.sign_upload(
            tenant_id=tenant_uuid,
            storage_key=values["storage_key"],
            content_type=values["content_type"],
            size_bytes=values["size_bytes"],
        )
        row = await self.get_attachment(tenant_uuid, attachment_id)
        if row is None:  # pragma: no cover - defensive: the insert just happened in this transaction
            raise AttachmentNotFound("attachment registration failed")
        serialized = serialize_attachment(row)
        response_body = {
            "attachment": serialized,
            "upload": serialized.get("upload"),
            "storage": {
                "provider": self.signer.provider,
                "presigned_upload_supported": upload is not None,
            },
        }
        await self._audit(
            tenant_id=tenant_uuid,
            actor_id=actor_uuid,
            action="attachment.registered",
            resource_type="attachment",
            resource_id=str(attachment_id),
            diff={
                "entity_type": values["entity_type"],
                "entity_id": str(values["entity_id"]),
                "filename": values["filename"],
                "size_bytes": values["size_bytes"],
                "storage_key": values["storage_key"],
            },
        )
        await self._complete_idempotency(
            tenant_id=tenant_uuid,
            idempotency_key=idempotency_key,
            response_status=201,
            response_body=response_body,
            resource_id=attachment_id,
        )
        return AttachmentWriteResult(attachment=serialized, upload=upload, response_status=201)

    # ------------------------------------------------------- attachments: read/delete ------
    async def get_attachment(self, tenant_id: str | UUID, attachment_id: str | UUID) -> dict[str, Any] | None:
        tenant_uuid = _as_uuid(tenant_id, "tenant_id")
        attachment_uuid = _as_uuid(attachment_id, "attachment_id")
        result = await self.session.execute(
            select(p.attachment_metadata).where(
                p.attachment_metadata.c.tenant_id == tenant_uuid,
                p.attachment_metadata.c.id == attachment_uuid,
            )
        )
        row = result.mappings().first()
        return dict(row) if row else None

    async def list_attachments(
        self,
        tenant_id: str | UUID,
        *,
        entity_type: str | None = None,
        entity_id: str | UUID | None = None,
        limit: int = DEFAULT_PAGE_LIMIT,
    ) -> list[dict[str, Any]]:
        tenant_uuid = _as_uuid(tenant_id, "tenant_id")
        query = select(p.attachment_metadata).where(p.attachment_metadata.c.tenant_id == tenant_uuid)
        if entity_type:
            query = query.where(p.attachment_metadata.c.entity_type == _require_text(
                entity_type, "entity_type", max_length=64, pattern=ENTITY_TYPE_PATTERN
            ))
        if entity_id:
            query = query.where(p.attachment_metadata.c.entity_id == _as_uuid(entity_id, "entity_id"))
        query = query.order_by(
            p.attachment_metadata.c.created_at.desc(), p.attachment_metadata.c.id.desc()
        ).limit(clamp_limit(limit))
        result = await self.session.execute(query)
        return [dict(row) for row in result.mappings().all()]

    async def delete_attachment(
        self, *, tenant_id: str | UUID, actor_id: str | UUID | None, attachment_id: str | UUID
    ) -> dict[str, Any]:
        """Delete the metadata **reference** (blob cleanup is the storage port's job, not the DB)."""
        tenant_uuid = _as_uuid(tenant_id, "tenant_id")
        actor_uuid = _as_uuid(actor_id, "actor_id") if actor_id is not None else None
        attachment_uuid = _as_uuid(attachment_id, "attachment_id")
        row = await self.get_attachment(tenant_uuid, attachment_uuid)
        if row is None:
            raise AttachmentNotFound("attachment not found")
        await self.session.execute(
            delete(p.attachment_metadata).where(
                p.attachment_metadata.c.tenant_id == tenant_uuid,
                p.attachment_metadata.c.id == attachment_uuid,
            )
        )
        await self._audit(
            tenant_id=tenant_uuid,
            actor_id=actor_uuid,
            action="attachment.deleted",
            resource_type="attachment",
            resource_id=str(attachment_uuid),
            diff={"storage_key": row["storage_key"], "filename": row["filename"], "metadata_only": True},
        )
        return serialize_attachment(row)

    # ---------------------------------------------------------- document links ------------
    async def _find_document_link(self, tenant_id: UUID, values: Mapping[str, Any]) -> dict[str, Any] | None:
        result = await self.session.execute(
            select(p.document_link).where(
                p.document_link.c.tenant_id == tenant_id,
                p.document_link.c.link_type == values["link_type"],
                p.document_link.c.from_type == values["from_type"],
                p.document_link.c.from_id == values["from_id"],
                p.document_link.c.to_type == values["to_type"],
                p.document_link.c.to_id == values["to_id"],
            )
        )
        row = result.mappings().first()
        return dict(row) if row else None

    async def create_document_link(
        self, *, tenant_id: str | UUID, actor_id: str | UUID | None, payload: Mapping[str, Any]
    ) -> DocumentLinkWriteResult:
        """Create a typed provenance link; idempotent by natural key (append-only, DM-PLAT-02)."""
        tenant_uuid = _as_uuid(tenant_id, "tenant_id")
        actor_uuid = _as_uuid(actor_id, "actor_id") if actor_id is not None else None
        values = normalize_document_link_payload(payload)
        existing = await self._find_document_link(tenant_uuid, values)
        if existing is not None:
            return DocumentLinkWriteResult(link=serialize_document_link(existing), created=False)
        link_id = uuid4()
        try:
            async with self.session.begin_nested():
                await self.session.execute(
                    insert(p.document_link).values(
                        id=link_id,
                        tenant_id=tenant_uuid,
                        link_type=values["link_type"],
                        from_type=values["from_type"],
                        from_id=values["from_id"],
                        to_type=values["to_type"],
                        to_id=values["to_id"],
                        qty=values["qty"],
                        amount_base=values["amount_base"],
                        created_by=actor_uuid,
                        updated_by=actor_uuid,
                    )
                )
        except IntegrityError:
            # Concurrent duplicate: append-only natural key won the race; return the existing row.
            race = await self._find_document_link(tenant_uuid, values)
            if race is None:  # pragma: no cover - defensive
                raise ArtifactsConflict("document link already exists") from None
            return DocumentLinkWriteResult(link=serialize_document_link(race), created=False)
        await self._audit(
            tenant_id=tenant_uuid,
            actor_id=actor_uuid,
            action="document_link.created",
            resource_type="document_link",
            resource_id=str(link_id),
            diff={
                "link_type": values["link_type"],
                "from": f"{values['from_type']}:{values['from_id']}",
                "to": f"{values['to_type']}:{values['to_id']}",
            },
        )
        created = await self.get_document_link(tenant_uuid, link_id)
        if created is None:  # pragma: no cover - defensive
            raise DocumentLinkNotFound("document link creation failed")
        return DocumentLinkWriteResult(link=serialize_document_link(created), created=True)

    async def get_document_link(self, tenant_id: str | UUID, link_id: str | UUID) -> dict[str, Any] | None:
        tenant_uuid = _as_uuid(tenant_id, "tenant_id")
        link_uuid = _as_uuid(link_id, "link_id")
        result = await self.session.execute(
            select(p.document_link).where(
                p.document_link.c.tenant_id == tenant_uuid,
                p.document_link.c.id == link_uuid,
            )
        )
        row = result.mappings().first()
        return dict(row) if row else None

    async def list_document_links(
        self,
        tenant_id: str | UUID,
        *,
        link_type: str | None = None,
        from_type: str | None = None,
        from_id: str | UUID | None = None,
        to_type: str | None = None,
        to_id: str | UUID | None = None,
        limit: int = DEFAULT_PAGE_LIMIT,
    ) -> list[dict[str, Any]]:
        tenant_uuid = _as_uuid(tenant_id, "tenant_id")
        query = select(p.document_link).where(p.document_link.c.tenant_id == tenant_uuid)
        if link_type:
            query = query.where(p.document_link.c.link_type == link_type.strip())
        if from_type:
            query = query.where(p.document_link.c.from_type == from_type.strip())
        if from_id:
            query = query.where(p.document_link.c.from_id == _as_uuid(from_id, "from_id"))
        if to_type:
            query = query.where(p.document_link.c.to_type == to_type.strip())
        if to_id:
            query = query.where(p.document_link.c.to_id == _as_uuid(to_id, "to_id"))
        query = query.order_by(p.document_link.c.created_at.desc(), p.document_link.c.id.desc()).limit(
            clamp_limit(limit)
        )
        result = await self.session.execute(query)
        return [dict(row) for row in result.mappings().all()]

    async def delete_document_link_guarded(
        self, *, tenant_id: str | UUID, actor_id: str | UUID | None, link_id: str | UUID
    ) -> None:
        """Guarded deletion: ``document_link`` is append-only, so the attempt is audited and refused."""
        tenant_uuid = _as_uuid(tenant_id, "tenant_id")
        actor_uuid = _as_uuid(actor_id, "actor_id") if actor_id is not None else None
        link_uuid = _as_uuid(link_id, "link_id")
        row = await self.get_document_link(tenant_uuid, link_uuid)
        if row is None:
            raise DocumentLinkNotFound("document link not found")
        await self._audit(
            tenant_id=tenant_uuid,
            actor_id=actor_uuid,
            action="document_link.delete_rejected",
            resource_type="document_link",
            resource_id=str(link_uuid),
            diff={"reason": "append_only", "rule": "DM-PLAT-02"},
        )
        raise DocumentLinkAppendOnly(
            "document links are append-only; create a compensating link instead of deleting provenance"
        )

    # ------------------------------------------------------- finance journal intents -------
    async def get_finance_journal_intent(
        self, tenant_id: str | UUID, intent_id: str | UUID
    ) -> dict[str, Any] | None:
        tenant_uuid = _as_uuid(tenant_id, "tenant_id")
        intent_uuid = _as_uuid(intent_id, "intent_id")
        result = await self.session.execute(
            select(p.finance_journal_intent).where(
                p.finance_journal_intent.c.tenant_id == tenant_uuid,
                p.finance_journal_intent.c.id == intent_uuid,
            )
        )
        row = result.mappings().first()
        return dict(row) if row else None

    async def list_finance_journal_intents(
        self,
        tenant_id: str | UUID,
        *,
        transaction_id: str | UUID | None = None,
        source_doc_type: str | None = None,
        source_doc_id: str | UUID | None = None,
        posting_rule_id: str | None = None,
        status: str | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
        cursor: str | None = None,
        limit: int = DEFAULT_PAGE_LIMIT,
    ) -> tuple[list[dict[str, Any]], str | None]:
        """Read-only keyset-paginated journal-intent list (tenant scoped; D2 boundary)."""
        tenant_uuid = _as_uuid(tenant_id, "tenant_id")
        filters = validate_journal_filters(
            transaction_id=_as_uuid(transaction_id, "transaction_id") if transaction_id else None,
            source_doc_type=source_doc_type,
            source_doc_id=_as_uuid(source_doc_id, "source_doc_id") if source_doc_id else None,
            posting_rule_id=posting_rule_id,
            status=status,
            date_from=date_from,
            date_to=date_to,
        )
        page_size = clamp_limit(limit)
        table = p.finance_journal_intent
        query = select(table).where(table.c.tenant_id == tenant_uuid)
        if filters["transaction_id"] is not None:
            query = query.where(table.c.inventory_transaction_id == filters["transaction_id"])
        if filters["source_doc_type"]:
            query = query.where(table.c.source_doc_type == filters["source_doc_type"])
        if filters["source_doc_id"] is not None:
            query = query.where(table.c.source_doc_id == filters["source_doc_id"])
        if filters["posting_rule_id"]:
            query = query.where(table.c.posting_rule_id == filters["posting_rule_id"])
        if filters["status"]:
            query = query.where(table.c.status == filters["status"])
        if filters["from_ts"] is not None:
            query = query.where(table.c.recorded_at >= filters["from_ts"])
        if filters["to_ts"] is not None:
            query = query.where(table.c.recorded_at < filters["to_ts"])
        if cursor:
            cursor_ts, cursor_id = decode_cursor(cursor)
            query = query.where(
                (table.c.recorded_at < cursor_ts)
                | and_(table.c.recorded_at == cursor_ts, table.c.id < cursor_id)
            )
        query = query.order_by(table.c.recorded_at.desc(), table.c.id.desc()).limit(page_size)
        result = await self.session.execute(query)
        rows = [dict(row) for row in result.mappings().all()]
        next_cursor = (
            encode_cursor(rows[-1]["recorded_at"], rows[-1]["id"]) if len(rows) == page_size and rows else None
        )
        return rows, next_cursor


__all__ = [
    "ArtifactsConflict",
    "ArtifactsError",
    "ArtifactsRepository",
    "ArtifactsValidation",
    "AttachmentNotFound",
    "AttachmentWriteResult",
    "DEFAULT_ALLOWED_CONTENT_TYPES",
    "DEFAULT_MAX_ATTACHMENT_BYTES",
    "DocumentLinkAppendOnly",
    "DocumentLinkNotFound",
    "DocumentLinkWriteResult",
    "InvalidCursor",
    "JournalIntentNotFound",
    "MAX_PAGE_LIMIT",
    "NullUploadSigner",
    "PresignedUpload",
    "UploadSignerPort",
    "clamp_limit",
    "decode_cursor",
    "encode_cursor",
    "normalize_attachment_payload",
    "normalize_checksum",
    "normalize_document_link_payload",
    "normalize_storage_key",
    "serialize_attachment",
    "serialize_document_link",
    "serialize_journal_intent",
    "serialize_row",
    "validate_journal_filters",
    "validate_storage_key",
]
