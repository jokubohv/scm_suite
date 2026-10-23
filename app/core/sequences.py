"""Transactional document-number sequence allocation (FS-00 `sequences.next`, DM-PLAT-04).

Allocation is a single atomic step inside the caller's posting transaction:

* a transaction-scoped PostgreSQL advisory lock serialises allocators for one
  ``(tenant, doc_type, scope_key, period_key)`` key (needed because ``period_key`` is nullable and the
  unique index therefore cannot be used for ``ON CONFLICT`` on the ``NEVER`` policy);
* an existing row is advanced with ``UPDATE … SET next_value = next_value + 1 … RETURNING next_value-1``;
* a missing row is created once with ``next_value = 2`` and the first value ``1`` is returned.

``reset_policy`` derives the period key from the injected clock (``NEVER`` → ``NULL``,
``ANNUAL`` → ``YYYY``, ``MONTHLY`` → ``YYYY-MM``), so a new period naturally starts a new row at 1.
``preview`` never consumes and never creates rows. Declared sequence names/defaults come from the
seed catalogue (``app/seed/constants.DOCUMENTED_NUMBER_SEQUENCES`` + ``app/seed/data/number_sequences.json``);
no HTTP/ORM imports, async SQLAlchemy Core only.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import insert, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.models.platform import number_sequence

SEED_SEQUENCES_PATH = Path(__file__).resolve().parents[1] / "seed" / "data" / "number_sequences.json"
DOC_TYPE_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{0,31}$")
DEFAULT_PADDING = 6
DEFAULT_SCOPE_KEY = "TENANT"
MAX_PADDING = 12


class SequenceError(Exception):
    """Base error for numbering failures (callers map to their own API envelope)."""

    code = "SEQUENCE_ERROR"


class UnknownSequenceType(SequenceError):
    code = "UNKNOWN_SEQUENCE_TYPE"


class InvalidSequenceDefinition(SequenceError):
    code = "INVALID_SEQUENCE_DEFINITION"


class InvalidSequenceRequest(SequenceError):
    code = "INVALID_SEQUENCE_REQUEST"


class ResetPolicy(StrEnum):
    NEVER = "NEVER"
    ANNUAL = "ANNUAL"
    MONTHLY = "MONTHLY"


class GapPolicy(StrEnum):
    NON_GAPLESS = "NON_GAPLESS"
    GAPLESS = "GAPLESS"


@dataclass(frozen=True, slots=True)
class SequenceDefinition:
    """Per-doc-type numbering template (seed defaults, overridable by callers/tests)."""

    doc_type: str
    prefix: str = ""
    padding: int = DEFAULT_PADDING
    scope_key: str = DEFAULT_SCOPE_KEY
    reset_policy: ResetPolicy = ResetPolicy.NEVER
    gap_policy: GapPolicy = GapPolicy.NON_GAPLESS

    def __post_init__(self) -> None:
        doc_type = self.doc_type.strip().upper()
        if not DOC_TYPE_PATTERN.match(doc_type):
            raise InvalidSequenceDefinition(f"invalid doc_type {self.doc_type!r}")
        if not 1 <= int(self.padding) <= MAX_PADDING:
            raise InvalidSequenceDefinition("padding must be between 1 and 12")
        object.__setattr__(self, "doc_type", doc_type)
        object.__setattr__(self, "reset_policy", ResetPolicy(str(self.reset_policy)))
        object.__setattr__(self, "gap_policy", GapPolicy(str(self.gap_policy)))

    def format_value(self, value: int) -> str:
        return f"{self.prefix}{int(value):0{self.padding}d}"


@dataclass(frozen=True, slots=True)
class SequenceValue:
    """One allocated (or previewed) document number."""

    tenant_id: UUID
    doc_type: str
    scope_key: str
    period_key: str | None
    value: int
    formatted: str
    reset_policy: ResetPolicy
    gap_policy: GapPolicy


@dataclass(frozen=True, slots=True)
class SequenceState:
    """Read-only view of the current allocator row (admin peek/read)."""

    tenant_id: UUID
    doc_type: str
    scope_key: str
    period_key: str | None
    next_value: int
    last_value: int
    prefix: str
    padding: int
    reset_policy: ResetPolicy
    gap_policy: GapPolicy


def utc_now() -> datetime:
    return datetime.now(UTC)


def _require_utc(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        raise InvalidSequenceRequest("now must be timezone-aware (UTC)")
    return moment.astimezone(UTC)


@lru_cache(maxsize=1)
def declared_sequences() -> Mapping[str, SequenceDefinition]:
    """Seed-declared sequence catalogue (27 document types, DATA-MODEL §21.2 / seed data)."""
    from app.seed.constants import DOCUMENTED_NUMBER_SEQUENCES

    try:
        records: list[dict[str, Any]] = json.loads(SEED_SEQUENCES_PATH.read_text(encoding="utf-8"))["records"]
    except (OSError, KeyError, json.JSONDecodeError) as exc:  # pragma: no cover - corrupted deployment
        raise InvalidSequenceDefinition(f"cannot load seed number sequences: {SEED_SEQUENCES_PATH}") from exc
    definitions: dict[str, SequenceDefinition] = {}
    for record in records:
        definition = SequenceDefinition(
            doc_type=str(record["doc_type"]),
            prefix=str(record.get("prefix", record["doc_type"])),
            padding=int(record.get("padding", DEFAULT_PADDING)),
            scope_key=str(record.get("scope_key", DEFAULT_SCOPE_KEY)),
            reset_policy=ResetPolicy(str(record.get("reset_policy", ResetPolicy.NEVER))),
            gap_policy=GapPolicy(str(record.get("gap_policy", GapPolicy.NON_GAPLESS))),
        )
        if definition.doc_type in definitions:
            raise InvalidSequenceDefinition(f"duplicate declared sequence {definition.doc_type!r}")
        definitions[definition.doc_type] = definition
    if set(definitions) != set(DOCUMENTED_NUMBER_SEQUENCES):
        missing = sorted(set(DOCUMENTED_NUMBER_SEQUENCES) - set(definitions))
        extra = sorted(set(definitions) - set(DOCUMENTED_NUMBER_SEQUENCES))
        raise InvalidSequenceDefinition(f"declared sequence drift (missing={missing}, extra={extra})")
    return definitions


def compute_period_key(reset_policy: ResetPolicy | str, now: datetime) -> str | None:
    """Derive the period key for a reset policy (never → None, annual → YYYY, monthly → YYYY-MM)."""
    policy = ResetPolicy(str(reset_policy))
    moment = _require_utc(now)
    if policy is ResetPolicy.NEVER:
        return None
    if policy is ResetPolicy.ANNUAL:
        return f"{moment.year:04d}"
    return f"{moment.year:04d}-{moment.month:02d}"


def format_sequence_value(prefix: str, padding: int, value: int) -> str:
    if not 0 < int(padding) <= MAX_PADDING:
        raise InvalidSequenceDefinition("padding must be between 1 and 12")
    if int(value) < 1:
        raise InvalidSequenceRequest("sequence value must be >= 1")
    return f"{prefix}{int(value):0{int(padding)}d}"


class SequenceService:
    """Allocate document numbers against ``platform.number_sequence`` (async Core, caller transaction)."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        declarations: Mapping[str, SequenceDefinition] | None = None,
    ) -> None:
        self.session = session
        self.declarations = dict(declarations) if declarations is not None else dict(declared_sequences())

    # -- definition/validation ----------------------------------------------------------------

    def definition(self, doc_type: str, *, allow_undeclared: bool = False) -> SequenceDefinition:
        normalised = str(doc_type or "").strip().upper()
        if not DOC_TYPE_PATTERN.match(normalised):
            raise InvalidSequenceRequest(f"invalid doc_type {doc_type!r}")
        if normalised in self.declarations:
            return self.declarations[normalised]
        if allow_undeclared:
            return SequenceDefinition(doc_type=normalised, prefix=normalised, padding=DEFAULT_PADDING)
        raise UnknownSequenceType(
            f"sequence {normalised!r} is not declared in the seed catalogue (declared={sorted(self.declarations)})"
        )

    def declared(self) -> Mapping[str, SequenceDefinition]:
        return dict(self.declarations)

    # -- allocation ---------------------------------------------------------------------------

    async def next(
        self,
        tenant_id: UUID,
        doc_type: str,
        *,
        scope_key: str | None = None,
        period_key: str | None = None,
        now: datetime | None = None,
        allow_undeclared: bool = False,
    ) -> SequenceValue:
        """Allocate the next number atomically (consume one value)."""
        definition = self.definition(doc_type, allow_undeclared=allow_undeclared)
        if not isinstance(tenant_id, UUID):
            raise InvalidSequenceRequest("tenant_id must be a UUID")
        resolved_scope = definition.scope_key if scope_key is None else str(scope_key)
        resolved_period = self._resolve_period(definition, period_key, now)
        await self._lock(tenant_id, definition.doc_type, resolved_scope, resolved_period)

        row = await self._select_row(tenant_id, definition.doc_type, resolved_scope, resolved_period, for_update=True)
        if row is None:
            await self.session.execute(
                insert(number_sequence).values(
                    id=uuid.uuid4(),
                    tenant_id=tenant_id,
                    doc_type=definition.doc_type,
                    scope_key=resolved_scope,
                    prefix=definition.prefix,
                    padding=definition.padding,
                    next_value=2,
                    reset_policy=definition.reset_policy.value,
                    gap_policy=definition.gap_policy.value,
                    period_key=resolved_period,
                )
            )
            value = 1
            prefix, padding = definition.prefix, definition.padding
        else:
            advanced = await self.session.execute(
                update(number_sequence)
                .where(number_sequence.c.id == row["id"])
                .values(next_value=number_sequence.c.next_value + 1)
                .returning(number_sequence.c.next_value - 1)
            )
            value = int(advanced.scalar_one())
            prefix, padding = str(row["prefix"]), int(row["padding"])
        return SequenceValue(
            tenant_id=tenant_id,
            doc_type=definition.doc_type,
            scope_key=resolved_scope,
            period_key=resolved_period,
            value=value,
            formatted=format_sequence_value(prefix, padding, value),
            reset_policy=definition.reset_policy,
            gap_policy=definition.gap_policy,
        )

    async def preview(
        self,
        tenant_id: UUID,
        doc_type: str,
        *,
        scope_key: str | None = None,
        period_key: str | None = None,
        now: datetime | None = None,
        allow_undeclared: bool = False,
    ) -> SequenceValue:
        """Peek at the next number without consuming it or creating a row."""
        definition = self.definition(doc_type, allow_undeclared=allow_undeclared)
        resolved_scope = definition.scope_key if scope_key is None else str(scope_key)
        resolved_period = self._resolve_period(definition, period_key, now)
        row = await self._select_row(tenant_id, definition.doc_type, resolved_scope, resolved_period)
        if row is None:
            value, prefix, padding = 1, definition.prefix, definition.padding
        else:
            value = int(row["next_value"])
            prefix, padding = str(row["prefix"]), int(row["padding"])
        return SequenceValue(
            tenant_id=tenant_id,
            doc_type=definition.doc_type,
            scope_key=resolved_scope,
            period_key=resolved_period,
            value=value,
            formatted=format_sequence_value(prefix, padding, value),
            reset_policy=definition.reset_policy,
            gap_policy=definition.gap_policy,
        )

    async def state(
        self,
        tenant_id: UUID,
        doc_type: str,
        *,
        scope_key: str | None = None,
        period_key: str | None = None,
        now: datetime | None = None,
    ) -> SequenceState | None:
        """Return the current allocator row (admin read), or None when nothing allocated yet."""
        definition = self.definition(doc_type)
        resolved_scope = definition.scope_key if scope_key is None else str(scope_key)
        resolved_period = self._resolve_period(definition, period_key, now)
        row = await self._select_row(tenant_id, definition.doc_type, resolved_scope, resolved_period)
        if row is None:
            return None
        next_value = int(row["next_value"])
        return SequenceState(
            tenant_id=tenant_id,
            doc_type=definition.doc_type,
            scope_key=resolved_scope,
            period_key=resolved_period,
            next_value=next_value,
            last_value=max(0, next_value - 1),
            prefix=str(row["prefix"]),
            padding=int(row["padding"]),
            reset_policy=ResetPolicy(str(row["reset_policy"])),
            gap_policy=GapPolicy(str(row["gap_policy"])),
        )

    # -- internals ----------------------------------------------------------------------------

    @staticmethod
    def _resolve_period(
        definition: SequenceDefinition,
        provided: str | None,
        now: datetime | None,
    ) -> str | None:
        if provided is not None:
            period = str(provided).strip()
            if not period:
                raise InvalidSequenceRequest("period_key must be a non-empty string when provided")
            return period
        return compute_period_key(definition.reset_policy, now or utc_now())

    async def _lock(self, tenant_id: UUID, doc_type: str, scope_key: str, period_key: str | None) -> None:
        lock_key = f"seq:{tenant_id}:{doc_type}:{scope_key}:{period_key or ''}"
        await self.session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
            {"lock_key": lock_key},
        )

    async def _select_row(
        self,
        tenant_id: UUID,
        doc_type: str,
        scope_key: str,
        period_key: str | None,
        *,
        for_update: bool = False,
    ) -> Any:
        condition = [
            number_sequence.c.tenant_id == tenant_id,
            number_sequence.c.doc_type == doc_type,
            number_sequence.c.scope_key == scope_key,
        ]
        condition.append(
            number_sequence.c.period_key.is_(None) if period_key is None else number_sequence.c.period_key == period_key
        )
        statement = select(number_sequence).where(*condition)
        if for_update:
            statement = statement.with_for_update()
        result = await self.session.execute(statement)
        return result.mappings().first()


__all__ = [
    "DEFAULT_PADDING",
    "DEFAULT_SCOPE_KEY",
    "DOC_TYPE_PATTERN",
    "GapPolicy",
    "InvalidSequenceDefinition",
    "InvalidSequenceRequest",
    "ResetPolicy",
    "SEED_SEQUENCES_PATH",
    "SequenceDefinition",
    "SequenceError",
    "SequenceService",
    "SequenceState",
    "SequenceValue",
    "UnknownSequenceType",
    "compute_period_key",
    "declared_sequences",
    "format_sequence_value",
    "utc_now",
]
