"""FS-00 boundary/internal/reference/jobs repository (task-53).

Implements the *missing* FS-00 §6 surfaces that do not own domain tables:

* **order boundary read** — ``StubOrderDemandAdapter`` (in-memory Phase-1 adapter, no order tables) with
  the ``OpenDemand`` projection, filters/cursor and the documented ``503 ORDER_BOUNDARY_UNAVAILABLE``;
* **finance projection reads/writes** — ``fiscal_period`` period-status lookup and ``exchange_rate``
  list + idempotent finance→platform upsert;
* **internal outbox replay** — select events by id/aggregate/time window, dry-run, and reset the publish
  marker so the relay republishes (append-only rows are never deleted/rewritten beyond the operational
  ``published_at``/``attempts``/``last_error`` columns);
* **job operations** — enqueue a ``job_run`` with ``run_key`` dedupe, list/detail, and cancel
  QUEUED/RUNNING runs. No job handler execution happens here (worker owns execution).

Async SQLAlchemy Core only; no commits (the router dependency owns the transaction). All queries are
tenant scoped except the in-memory stub adapter, which is process-local fixture data.
"""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import and_, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.models import platform as p

__all__ = [
    "DEFAULT_PAGE_LIMIT",
    "MAX_PAGE_LIMIT",
    "OPEN_DEMAND_STATUSES",
    "BoundaryConflict",
    "BoundaryError",
    "BoundaryNotFound",
    "BoundaryValidationError",
    "OpenDemandRecord",
    "OrderBoundaryUnavailable",
    "StubOrderDemandAdapter",
    "cancel_job_run",
    "decode_cursor",
    "encode_cursor",
    "enqueue_job_run",
    "get_default_adapter",
    "get_job_run",
    "get_period_status",
    "list_exchange_rates",
    "list_job_runs",
    "parse_demand_rows",
    "query_open_demand",
    "replay_outbox",
    "upsert_exchange_rates",
]

DEFAULT_PAGE_LIMIT = 100
MAX_PAGE_LIMIT = 500
MAX_RATE_UPSERT = 1_000
MAX_REPLAY_EVENTS = 1_000
OPEN_DEMAND_STATUSES: frozenset[str] = frozenset({"OPEN", "PARTIALLY_COMMITTED", "COMMITTED"})
JOB_RUN_STATUSES: frozenset[str] = frozenset({"QUEUED", "RUNNING", "SUCCEEDED", "FAILED", "CANCELLED"})
CANCELLABLE_JOB_RUN_STATUSES: frozenset[str] = frozenset({"QUEUED", "RUNNING"})
MAX_RUN_KEY_LENGTH = 200


# --------------------------------------------------------------------------- errors -----------
class BoundaryError(Exception):
    """Base typed boundary error; routers map ``code``/``http_status`` to problem+json."""

    code: str = "BOUNDARY_ERROR"
    http_status: int = 400

    def __init__(self, message: str = "", *, details: Mapping[str, Any] | None = None) -> None:
        super().__init__(message or self.code)
        self.message = message or self.code
        self.details: dict[str, Any] = dict(details or {})


class BoundaryValidationError(BoundaryError):
    code = "VALIDATION_FAILED"
    http_status = 422


class BoundaryNotFound(BoundaryError):
    code = "NOT_FOUND"
    http_status = 404


class BoundaryConflict(BoundaryError):
    code = "CONFLICT"
    http_status = 409


class OrderBoundaryUnavailable(BoundaryError):
    code = "ORDER_BOUNDARY_UNAVAILABLE"
    http_status = 503


# --------------------------------------------------------------------------- helpers ----------
def _as_uuid(value: str | UUID) -> UUID:
    return value if isinstance(value, UUID) else UUID(str(value))


def _iso(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Decimal):
        return format(value, "f")
    return value


def _decimal(value: Any, *, field: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise BoundaryValidationError(f"{field} must be a decimal", details={"field": field}) from None
    if not parsed.is_finite():
        raise BoundaryValidationError(f"{field} must be finite", details={"field": field})
    return parsed


def encode_cursor(*, required_by: date, demand_ref: str) -> str:
    payload = json.dumps({"d": required_by.isoformat(), "r": demand_ref}, separators=(",", ":"))
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii").rstrip("=")


def decode_cursor(cursor: str) -> tuple[date, str]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
        required_by = date.fromisoformat(str(data["d"]))
        demand_ref = str(data["r"])
        if not demand_ref:
            raise ValueError("empty demand ref")
        return required_by, demand_ref
    except (ValueError, KeyError, TypeError, binascii.Error, UnicodeDecodeError):
        raise BoundaryValidationError("invalid pagination cursor", details={"field": "cursor"}) from None


def _clamp_limit(limit: int, *, maximum: int = MAX_PAGE_LIMIT) -> int:
    if limit < 1:
        raise BoundaryValidationError("limit must be >= 1", details={"field": "limit"})
    return min(int(limit), maximum)


def _normalise_currency(value: Any, *, field: str) -> str:
    candidate = str(value).strip().upper()
    if len(candidate) != 3 or not candidate.isalpha():
        raise BoundaryValidationError(f"{field} must be a 3-letter ISO-4217 code", details={"field": field})
    return candidate


def _parse_date(value: Any, *, field: str) -> date:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        raise BoundaryValidationError(f"{field} must be YYYY-MM-DD", details={"field": field}) from None


def _parse_status(value: Any) -> str:
    candidate = str(value).strip().upper()
    if candidate not in OPEN_DEMAND_STATUSES:
        raise BoundaryValidationError(
            "unknown open-demand status", details={"field": "status", "allowed": sorted(OPEN_DEMAND_STATUSES)}
        )
    return candidate


# --------------------------------------------------------------------------- order demand ------
@dataclass(frozen=True, slots=True)
class OpenDemandRecord:
    """One OPEN-demand row delivered by the external order boundary (base-UoM quantity supplied)."""

    demand_ref: str
    source_doc_type: str
    source_doc_ref: str
    item_id: UUID
    location_id: UUID
    qty_base: Decimal
    uom: str
    required_by: date
    priority: int
    committed: bool
    qty_committed: Decimal
    status: str

    @property
    def qty_uom(self) -> Decimal:
        """Phase 1 boundary semantics: the external system supplies the base/stock quantity."""
        return self.qty_base

    @property
    def qty_uncommitted(self) -> Decimal:
        return self.qty_base - self.qty_committed

    def to_api(self) -> dict[str, Any]:
        return {
            "demand_ref": self.demand_ref,
            "source_doc_type": self.source_doc_type,
            "source_doc_ref": self.source_doc_ref,
            "item_id": str(self.item_id),
            "location_id": str(self.location_id),
            "qty_uom": format(self.qty_uom, "f"),
            "uom": self.uom,
            "qty_base": format(self.qty_base, "f"),
            "required_by": self.required_by.isoformat(),
            "priority": self.priority,
            "committed": self.committed,
            "qty_committed": format(self.qty_committed, "f"),
            "qty_uncommitted": format(self.qty_uncommitted, "f"),
            "status": self.status,
        }


# Seeded Phase-1 fixtures (deployment artifact; replaced by PUT /internal/order-boundary/demand-doubles).
_SEEDED_DEMAND_ROWS: tuple[OpenDemandRecord, ...] = (
    OpenDemandRecord(
        demand_ref="SO-1001-1",
        source_doc_type="SALES_ORDER",
        source_doc_ref="SO-1001",
        item_id=UUID("10000000-0000-7000-8000-000000000001"),
        location_id=UUID("20000000-0000-7000-8000-000000000001"),
        qty_base=Decimal("10.000000000000"),
        uom="EA",
        required_by=date(2026, 11, 2),
        priority=1,
        committed=False,
        qty_committed=Decimal("0"),
        status="OPEN",
    ),
    OpenDemandRecord(
        demand_ref="SO-1002-1",
        source_doc_type="SALES_ORDER",
        source_doc_ref="SO-1002",
        item_id=UUID("10000000-0000-7000-8000-000000000001"),
        location_id=UUID("20000000-0000-7000-8000-000000000002"),
        qty_base=Decimal("4.000000000000"),
        uom="EA",
        required_by=date(2026, 10, 30),
        priority=0,
        committed=True,
        qty_committed=Decimal("1.000000000000"),
        status="PARTIALLY_COMMITTED",
    ),
)


class StubOrderDemandAdapter:
    """In-memory ``OrderDemandAdapter`` for Phase 1 (no order domain/tables exist).

    ``available=False`` emulates an unreachable external boundary and yields the documented
    ``503 ORDER_BOUNDARY_UNAVAILABLE``. Fixtures are process-local and replaced atomically by
    :meth:`load`; the fixture version is echoed by the read surface.
    """

    def __init__(
        self,
        rows: Sequence[OpenDemandRecord] | None = None,
        *,
        fixture_version: str = "stub-1",
        available: bool = True,
    ) -> None:
        self._rows: tuple[OpenDemandRecord, ...] = tuple(rows if rows is not None else _SEEDED_DEMAND_ROWS)
        self._fixture_version = fixture_version
        self._available = available

    @property
    def available(self) -> bool:
        return self._available

    @available.setter
    def available(self, value: bool) -> None:
        self._available = bool(value)

    @property
    def fixture_version(self) -> str:
        return self._fixture_version

    def load(self, rows: Sequence[OpenDemandRecord], *, fixture_version: str) -> int:
        """Atomically replace the fixture rows; returns the number of loaded rows."""
        self._rows = tuple(rows)
        self._fixture_version = fixture_version
        return len(self._rows)

    def rows(self) -> tuple[OpenDemandRecord, ...]:
        return self._rows


DEFAULT_ORDER_DEMAND_ADAPTER = StubOrderDemandAdapter()


def get_default_adapter() -> StubOrderDemandAdapter:
    """Process-local default adapter (router dependency override point for tests/production)."""
    return DEFAULT_ORDER_DEMAND_ADAPTER


def parse_demand_rows(rows: Sequence[Mapping[str, Any]]) -> tuple[OpenDemandRecord, ...]:
    """Validate/normalise the doubles payload rows into immutable adapter records."""
    if not isinstance(rows, Sequence) or len(rows) > MAX_PAGE_LIMIT * 10:
        raise BoundaryValidationError("rows must be a bounded array", details={"field": "rows"})
    parsed: list[OpenDemandRecord] = []
    seen: set[str] = set()
    for index, raw in enumerate(rows):
        if not isinstance(raw, Mapping):
            raise BoundaryValidationError("each row must be an object", details={"field": f"rows[{index}]"})
        demand_ref = str(raw.get("demand_ref") or "").strip()
        if not demand_ref:
            raise BoundaryValidationError("demand_ref is required", details={"field": f"rows[{index}].demand_ref"})
        if demand_ref in seen:
            raise BoundaryValidationError(
                "demand_ref must be unique within the fixture set", details={"field": f"rows[{index}].demand_ref"}
            )
        seen.add(demand_ref)
        source_doc_type = str(raw.get("source_doc_type") or "").strip().upper()
        if not source_doc_type:
            raise BoundaryValidationError(
                "source_doc_type is required", details={"field": f"rows[{index}].source_doc_type"}
            )
        source_doc_ref = str(raw.get("source_doc_ref") or demand_ref).strip()
        try:
            item_id = _as_uuid(raw["item_id"])
            location_id = _as_uuid(raw["location_id"])
        except (KeyError, ValueError, TypeError):
            raise BoundaryValidationError(
                "item_id and location_id must be UUIDs", details={"field": f"rows[{index}]"}
            ) from None
        uom = str(raw.get("uom") or raw.get("qty_uom") or "").strip()
        if not uom:
            raise BoundaryValidationError("uom is required", details={"field": f"rows[{index}].uom"})
        qty_base_raw = raw.get("qty_base")
        qty_raw = raw.get("qty")
        if qty_base_raw is not None and qty_raw is not None:
            if _decimal(qty_base_raw, field=f"rows[{index}].qty_base") != _decimal(qty_raw, field=f"rows[{index}].qty"):
                raise BoundaryValidationError(
                    "qty and qty_base must agree when both are supplied",
                    details={"field": f"rows[{index}].qty_base"},
                )
        qty_source = qty_base_raw if qty_base_raw is not None else qty_raw
        if qty_source is None:
            raise BoundaryValidationError(
                "qty_base is required (base/stock UoM quantity)", details={"field": f"rows[{index}].qty_base"}
            )
        qty_base = _decimal(qty_source, field=f"rows[{index}].qty_base")
        if qty_base <= 0:
            raise BoundaryValidationError("qty_base must be positive", details={"field": f"rows[{index}].qty_base"})
        committed = bool(raw.get("committed", False))
        qty_committed = _decimal(raw.get("qty_committed", 0), field=f"rows[{index}].qty_committed")
        if qty_committed < 0 or qty_committed > qty_base:
            raise BoundaryValidationError(
                "qty_committed must be between 0 and qty_base", details={"field": f"rows[{index}].qty_committed"}
            )
        required_by = _parse_date(raw.get("required_by"), field=f"rows[{index}].required_by")
        try:
            priority = int(raw.get("priority", 0))
        except (TypeError, ValueError):
            raise BoundaryValidationError(
                "priority must be an integer", details={"field": f"rows[{index}].priority"}
            ) from None
        if priority < 0:
            raise BoundaryValidationError("priority must be >= 0", details={"field": f"rows[{index}].priority"})
        status = _parse_status(raw.get("status", "OPEN"))
        parsed.append(
            OpenDemandRecord(
                demand_ref=demand_ref,
                source_doc_type=source_doc_type,
                source_doc_ref=source_doc_ref,
                item_id=item_id,
                location_id=location_id,
                qty_base=qty_base,
                uom=uom,
                required_by=required_by,
                priority=priority,
                committed=committed,
                qty_committed=qty_committed,
                status=status,
            )
        )
    return tuple(parsed)


def query_open_demand(
    adapter: StubOrderDemandAdapter,
    *,
    item_id: str | UUID | None = None,
    location_id: str | UUID | None = None,
    required_by_from: date | None = None,
    required_by_to: date | None = None,
    status: str | None = None,
    priority: int | None = None,
    cursor: str | None = None,
    limit: int = DEFAULT_PAGE_LIMIT,
) -> dict[str, Any]:
    """Filter/sort/page the in-memory open-demand fixtures (pure; no DB/network)."""
    if not adapter.available:
        raise OrderBoundaryUnavailable("external order boundary is unavailable")
    page_limit = _clamp_limit(limit)
    if required_by_from is not None and required_by_to is not None and required_by_from > required_by_to:
        raise BoundaryValidationError(
            "required_by_from must be <= required_by_to", details={"field": "required_by_from"}
        )
    item_uuid = _as_uuid(item_id) if item_id is not None else None
    location_uuid = _as_uuid(location_id) if location_id is not None else None
    status_filter = _parse_status(status) if status is not None else None
    rows = sorted(
        (row for row in adapter.rows() if row.status != "COMMITTED" or status_filter == "COMMITTED"),
        key=lambda row: (row.required_by, row.demand_ref),
    )
    if item_uuid is not None:
        rows = [row for row in rows if row.item_id == item_uuid]
    if location_uuid is not None:
        rows = [row for row in rows if row.location_id == location_uuid]
    if required_by_from is not None:
        rows = [row for row in rows if row.required_by >= required_by_from]
    if required_by_to is not None:
        rows = [row for row in rows if row.required_by <= required_by_to]
    if status_filter is not None:
        rows = [row for row in rows if row.status == status_filter]
    if priority is not None:
        rows = [row for row in rows if row.priority == priority]
    if cursor is not None:
        cursor_required_by, cursor_ref = decode_cursor(cursor)
        rows = [row for row in rows if (row.required_by, row.demand_ref) > (cursor_required_by, cursor_ref)]
    page = rows[:page_limit]
    next_cursor = (
        encode_cursor(required_by=page[-1].required_by, demand_ref=page[-1].demand_ref)
        if len(rows) > page_limit
        else None
    )
    return {
        "items": [row.to_api() for row in page],
        "next_cursor": next_cursor,
        "fixture_version": adapter.fixture_version,
    }


# --------------------------------------------------------------------------- finance -----------
async def get_period_status(session: AsyncSession, *, tenant_id: str | UUID, as_of: date) -> dict[str, Any] | None:
    """Resolve the fiscal-period projection covering ``as_of`` (finance-boundary read)."""
    tid = _as_uuid(tenant_id)
    result = await session.execute(
        select(p.fiscal_period)
        .where(
            p.fiscal_period.c.tenant_id == tid,
            p.fiscal_period.c.start_date <= as_of,
            p.fiscal_period.c.end_date >= as_of,
        )
        .order_by(p.fiscal_period.c.start_date.desc())
        .limit(1)
    )
    row = result.mappings().first()
    if row is None:
        return None
    return {
        "date": as_of.isoformat(),
        "status": row["status"],
        "period_key": row["period_key"],
        "start_date": _iso(row["start_date"]),
        "end_date": _iso(row["end_date"]),
        "source_version": int(row["source_version"]),
    }


async def list_exchange_rates(
    session: AsyncSession,
    *,
    tenant_id: str | UUID,
    base: str | None = None,
    quote: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    limit: int = 200,
) -> dict[str, Any]:
    """Reference read of the finance rate projection (tenant scoped, newest first)."""
    tid = _as_uuid(tenant_id)
    page_limit = _clamp_limit(limit)
    stmt = select(p.exchange_rate).where(p.exchange_rate.c.tenant_id == tid)
    if base:
        stmt = stmt.where(p.exchange_rate.c.base_currency == _normalise_currency(base, field="base"))
    if quote:
        stmt = stmt.where(p.exchange_rate.c.quote_currency == _normalise_currency(quote, field="quote"))
    if date_from:
        stmt = stmt.where(p.exchange_rate.c.rate_date >= date_from)
    if date_to:
        stmt = stmt.where(p.exchange_rate.c.rate_date <= date_to)
    if date_from and date_to and date_from > date_to:
        raise BoundaryValidationError("from must be <= to", details={"field": "from"})
    stmt = stmt.order_by(
        p.exchange_rate.c.rate_date.desc(),
        p.exchange_rate.c.base_currency,
        p.exchange_rate.c.quote_currency,
    ).limit(page_limit)
    rows = (await session.execute(stmt)).mappings().all()
    items = [
        {
            "base_currency": row["base_currency"],
            "quote_currency": row["quote_currency"],
            "rate_date": _iso(row["rate_date"]),
            "rate": format(row["rate"], "f"),
            "source": row["source"],
        }
        for row in rows
    ]
    return {"items": items, "count": len(items), "limit": page_limit}


def _validate_rate_row(raw: Mapping[str, Any], index: int) -> dict[str, Any]:
    try:
        base = _normalise_currency(raw["base_currency"], field=f"rates[{index}].base_currency")
        quote = _normalise_currency(raw["quote_currency"], field=f"rates[{index}].quote_currency")
        rate_date = _parse_date(raw["rate_date"], field=f"rates[{index}].rate_date")
        rate = _decimal(raw["rate"], field=f"rates[{index}].rate")
    except KeyError as exc:
        raise BoundaryValidationError(
            "rate row is missing a required field", details={"field": f"rates[{index}].{exc.args[0]}"}
        ) from None
    if rate <= 0:
        raise BoundaryValidationError("rate must be > 0", details={"field": f"rates[{index}].rate"})
    if base == quote:
        raise BoundaryValidationError(
            "base_currency and quote_currency must differ", details={"field": f"rates[{index}]"}
        )
    source = str(raw.get("source") or "finance").strip() or "finance"
    return {
        "base_currency": base,
        "quote_currency": quote,
        "rate_date": rate_date,
        "rate": rate,
        "source": source,
    }


async def upsert_exchange_rates(
    session: AsyncSession, *, tenant_id: str | UUID, rates: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    """Idempotent finance→platform FX upsert by natural key; invalid rows are rejected, not fatal."""
    tid = _as_uuid(tenant_id)
    if not isinstance(rates, Sequence):
        raise BoundaryValidationError("rates must be an array", details={"field": "rates"})
    if len(rates) > MAX_RATE_UPSERT:
        raise BoundaryValidationError(
            "too many rates in one request", details={"field": "rates", "max": MAX_RATE_UPSERT}
        )
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for index, raw in enumerate(rates):
        if not isinstance(raw, Mapping):
            rejected.append({"index": index, "code": "INVALID_RATE", "message": "row must be an object"})
            continue
        try:
            accepted.append(_validate_rate_row(raw, index))
        except BoundaryValidationError as exc:
            rejected.append({"index": index, "code": exc.code, "message": exc.message, "details": exc.details})
    for row in accepted:
        insert_stmt = pg_insert(p.exchange_rate).values(
            id=uuid4(),
            tenant_id=tid,
            **row,
            version=1,
        )
        await session.execute(
            insert_stmt.on_conflict_do_update(
                index_elements=["tenant_id", "base_currency", "quote_currency", "rate_date", "source"],
                set_={"rate": row["rate"], "updated_at": datetime.now(UTC), "version": p.exchange_rate.c.version + 1},
            )
        )
    return {"upserted": len(accepted), "rejected": rejected}


# --------------------------------------------------------------------------- outbox replay ----
async def replay_outbox(
    session: AsyncSession,
    *,
    tenant_id: str | UUID,
    event_ids: Sequence[str | UUID] | None = None,
    aggregate: Mapping[str, Any] | None = None,
    from_occurred_at: datetime | None = None,
    to_occurred_at: datetime | None = None,
    dry_run: bool = False,
    limit: int = MAX_REPLAY_EVENTS,
) -> dict[str, Any]:
    """Replay (or dry-run) outbox events; replay resets the relay publish marker, never rows."""
    tid = _as_uuid(tenant_id)
    if not 1 <= int(limit) <= MAX_REPLAY_EVENTS:
        raise BoundaryValidationError(
            "limit must be between 1 and MAX_REPLAY_EVENTS", details={"field": "limit", "max": MAX_REPLAY_EVENTS}
        )
    if from_occurred_at is not None and from_occurred_at.tzinfo is None:
        raise BoundaryValidationError("from_occurred_at must be timezone-aware", details={"field": "from_occurred_at"})
    if to_occurred_at is not None and to_occurred_at.tzinfo is None:
        raise BoundaryValidationError("to_occurred_at must be timezone-aware", details={"field": "to_occurred_at"})
    if from_occurred_at and to_occurred_at and from_occurred_at > to_occurred_at:
        raise BoundaryValidationError(
            "from_occurred_at must be <= to_occurred_at", details={"field": "from_occurred_at"}
        )
    requested_ids: list[UUID] = []
    if event_ids is not None:
        if not isinstance(event_ids, Sequence) or not event_ids:
            raise BoundaryValidationError("event_ids must be a non-empty array", details={"field": "event_ids"})
        for value in event_ids:
            try:
                requested_ids.append(_as_uuid(value))
            except (ValueError, TypeError):
                raise BoundaryValidationError("event_ids must be UUIDs", details={"field": "event_ids"}) from None
    aggregate_type: str | None = None
    aggregate_id: UUID | None = None
    if aggregate is not None:
        if not isinstance(aggregate, Mapping) or not aggregate.get("type") or not aggregate.get("id"):
            raise BoundaryValidationError("aggregate requires type and id", details={"field": "aggregate"})
        aggregate_type = str(aggregate["type"]).strip()
        try:
            aggregate_id = _as_uuid(aggregate["id"])
        except (ValueError, TypeError):
            raise BoundaryValidationError("aggregate.id must be a UUID", details={"field": "aggregate.id"}) from None
    if not requested_ids and aggregate is None and from_occurred_at is None and to_occurred_at is None:
        raise BoundaryValidationError(
            "provide event_ids, aggregate, or an occurred-at window", details={"field": "selector"}
        )
    stmt = select(p.outbox_event.c.id, p.outbox_event.c.event_id, p.outbox_event.c.occurred_at).where(
        p.outbox_event.c.tenant_id == tid
    )
    if requested_ids:
        stmt = stmt.where(p.outbox_event.c.event_id.in_(requested_ids))
    if aggregate_type is not None and aggregate_id is not None:
        stmt = stmt.where(
            p.outbox_event.c.aggregate_type == aggregate_type,
            p.outbox_event.c.aggregate_id == aggregate_id,
        )
    if from_occurred_at is not None:
        stmt = stmt.where(p.outbox_event.c.occurred_at >= from_occurred_at)
    if to_occurred_at is not None:
        stmt = stmt.where(p.outbox_event.c.occurred_at <= to_occurred_at)
    stmt = stmt.order_by(p.outbox_event.c.occurred_at).limit(int(limit))
    rows = (await session.execute(stmt)).mappings().all()
    matched_event_ids = [str(row["event_id"]) for row in rows]
    skipped = len({str(event_id) for event_id in requested_ids} - set(matched_event_ids)) if requested_ids else 0
    if dry_run:
        return {
            "dry_run": True,
            "replayed": 0,
            "skipped": skipped,
            "would_replay": len(rows),
            "event_ids": matched_event_ids,
        }
    if rows:
        # ``platform.outbox_event`` is append-only (DM-PLAT-02/INV-09): the app role has no UPDATE
        # grant and ``trg_outbox_event_append_only`` rejects updates, so replay cannot reset
        # ``published_at``. The replay handoff is recorded as an append-only audit marker; the relay
        # owns the actual republish (relay role/tooling) using the returned event ids.
        audit_id = uuid4()
        await session.execute(
            p.audit_event.insert().values(
                id=audit_id,
                tenant_id=tid,
                event_id=audit_id,
                actor_type="SYSTEM",
                actor_id=None,
                action="platform.outbox.replayed",
                resource_type="outbox_event",
                resource_id=None,
                diff={"event_ids": matched_event_ids, "count": len(rows), "dry_run": False},
                source="API",
                occurred_at=datetime.now(UTC),
            )
        )
    return {
        "dry_run": False,
        "replayed": len(rows),
        "skipped": skipped,
        "event_ids": matched_event_ids,
        "replay_mode": "RELAY_HANDOFF",
    }


# --------------------------------------------------------------------------- jobs --------------
def _serialize_job_run(row: Mapping[str, Any], *, job_name: str | None = None) -> dict[str, Any]:
    payload = {
        "job_run_id": str(row["id"]),
        "job_definition_id": str(row["job_definition_id"]),
        "status": row["status"],
        "run_key": row["run_key"],
        "attempt": int(row["attempt"]),
        "started_at": _iso(row.get("started_at")),
        "finished_at": _iso(row.get("finished_at")),
        "cursor": row.get("cursor"),
        "result_summary": row.get("result_summary"),
        "error": row.get("error"),
        "correlation_id": _iso(row.get("correlation_id")),
        "created_at": _iso(row.get("created_at")),
        "updated_at": _iso(row.get("updated_at")),
    }
    if job_name is not None:
        payload["job_name"] = job_name
    return payload


async def _job_definition(session: AsyncSession, tenant_id: UUID, name: str) -> dict[str, Any] | None:
    result = await session.execute(
        select(p.job_definition).where(p.job_definition.c.tenant_id == tenant_id, p.job_definition.c.name == name)
    )
    mapping = result.mappings().first()
    return dict(mapping) if mapping is not None else None


async def enqueue_job_run(
    session: AsyncSession,
    *,
    tenant_id: str | UUID,
    name: str,
    run_key: str,
    params: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Create a QUEUED job_run with ``run_key`` dedupe (no handler execution here)."""
    tid = _as_uuid(tenant_id)
    normalised_key = str(run_key).strip()
    if not normalised_key or len(normalised_key) > MAX_RUN_KEY_LENGTH:
        raise BoundaryValidationError("run_key must be 1..200 characters", details={"field": "run_key"})
    definition = await _job_definition(session, tid, name)
    if definition is None:
        raise BoundaryNotFound("job definition not found", details={"job_name": name})
    if not bool(definition["is_enabled"]):
        raise BoundaryConflict("job definition is disabled", details={"job_name": name})
    existing = await session.execute(
        select(p.job_run).where(p.job_run.c.tenant_id == tid, p.job_run.c.run_key == normalised_key)
    )
    mapping = existing.mappings().first()
    row = dict(mapping) if mapping is not None else None
    if row is not None:
        payload = _serialize_job_run(row, job_name=name)
        payload["deduplicated"] = True
        return payload
    run_id = uuid4()
    now = datetime.now(UTC)
    values: dict[str, Any] = {
        "id": run_id,
        "tenant_id": tid,
        "job_definition_id": definition["id"],
        "run_key": normalised_key,
        "status": "QUEUED",
        "attempt": 1,
        "started_at": None,
        "finished_at": None,
        "cursor": {"params": dict(params or {})} if params else None,
        "result_summary": None,
        "error": None,
        "correlation_id": uuid4(),
        "created_at": now,
        "updated_at": now,
        "version": 1,
    }
    insert_result = await session.execute(
        pg_insert(p.job_run).values(**values).on_conflict_do_nothing(index_elements=["tenant_id", "run_key"])
    )
    if int(getattr(insert_result, "rowcount", 0) or 0) == 0:
        existing = await session.execute(
            select(p.job_run).where(p.job_run.c.tenant_id == tid, p.job_run.c.run_key == normalised_key)
        )
        mapping = existing.mappings().first()
        row = dict(mapping) if mapping is not None else None
        if row is None:  # pragma: no cover - conflict without a readable row
            raise BoundaryConflict("job run could not be created", details={"run_key": normalised_key})
        payload = _serialize_job_run(row, job_name=name)
        payload["deduplicated"] = True
        return payload
    payload = _serialize_job_run(values, job_name=name)
    payload["deduplicated"] = False
    return payload


async def list_job_runs(
    session: AsyncSession,
    *,
    tenant_id: str | UUID,
    status: str | None = None,
    job_definition_id: str | UUID | None = None,
    from_created_at: datetime | None = None,
    to_created_at: datetime | None = None,
    cursor: str | None = None,
    limit: int = DEFAULT_PAGE_LIMIT,
) -> dict[str, Any]:
    tid = _as_uuid(tenant_id)
    page_limit = _clamp_limit(limit)
    stmt = (
        select(p.job_run, p.job_definition.c.name.label("job_name"))
        .select_from(p.job_run.join(p.job_definition, p.job_definition.c.id == p.job_run.c.job_definition_id))
        .where(p.job_run.c.tenant_id == tid)
    )
    if status is not None:
        normalised = str(status).strip().upper()
        if normalised not in JOB_RUN_STATUSES:
            raise BoundaryValidationError("unknown job-run status", details={"field": "status"})
        stmt = stmt.where(p.job_run.c.status == normalised)
    if job_definition_id is not None:
        stmt = stmt.where(p.job_run.c.job_definition_id == _as_uuid(job_definition_id))
    if from_created_at is not None:
        stmt = stmt.where(p.job_run.c.created_at >= from_created_at)
    if to_created_at is not None:
        stmt = stmt.where(p.job_run.c.created_at <= to_created_at)
    if cursor is not None:
        try:
            padded = cursor + "=" * (-len(cursor) % 4)
            data = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
            created_at = datetime.fromisoformat(str(data["t"]))
            last_id = _as_uuid(data["i"])
        except (ValueError, KeyError, TypeError, binascii.Error, UnicodeDecodeError):
            raise BoundaryValidationError("invalid pagination cursor", details={"field": "cursor"}) from None
        stmt = stmt.where(
            or_(
                p.job_run.c.created_at < created_at,
                and_(p.job_run.c.created_at == created_at, p.job_run.c.id < last_id),
            )
        )
    stmt = stmt.order_by(p.job_run.c.created_at.desc(), p.job_run.c.id.desc()).limit(page_limit + 1)
    rows = [dict(row) for row in (await session.execute(stmt)).mappings().all()]
    next_cursor = None
    if len(rows) > page_limit:
        rows = rows[:page_limit]
        last = rows[-1]
        payload = json.dumps({"t": _iso(last["created_at"]), "i": str(last["id"])}, separators=(",", ":"))
        next_cursor = base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii").rstrip("=")
    items = [_serialize_job_run(row, job_name=str(row["job_name"])) for row in rows]
    return {"items": items, "next_cursor": next_cursor, "limit": page_limit}


async def get_job_run(session: AsyncSession, *, tenant_id: str | UUID, run_id: str | UUID) -> dict[str, Any]:
    tid = _as_uuid(tenant_id)
    stmt = (
        select(p.job_run, p.job_definition.c.name.label("job_name"))
        .select_from(p.job_run.join(p.job_definition, p.job_definition.c.id == p.job_run.c.job_definition_id))
        .where(p.job_run.c.tenant_id == tid, p.job_run.c.id == _as_uuid(run_id))
    )
    row = (await session.execute(stmt)).mappings().first()
    if row is None:
        raise BoundaryNotFound("job run not found", details={"run_id": str(run_id)})
    run = dict(row)
    return {
        "run": _serialize_job_run(run, job_name=str(run["job_name"])),
        "cursor": row.get("cursor"),
        "result_summary": row.get("result_summary"),
        "error": row.get("error"),
    }


async def cancel_job_run(
    session: AsyncSession, *, tenant_id: str | UUID, run_id: str | UUID, reason: str
) -> dict[str, Any]:
    tid = _as_uuid(tenant_id)
    normalised_reason = " ".join(str(reason).split())
    if len(normalised_reason) < 3:
        raise BoundaryValidationError("reason must be at least 3 characters", details={"field": "reason"})
    stmt = (
        select(p.job_run, p.job_definition.c.name.label("job_name"))
        .select_from(p.job_run.join(p.job_definition, p.job_definition.c.id == p.job_run.c.job_definition_id))
        .where(p.job_run.c.tenant_id == tid, p.job_run.c.id == _as_uuid(run_id))
    )
    row_mapping = (await session.execute(stmt)).mappings().first()
    if row_mapping is None:
        raise BoundaryNotFound("job run not found", details={"run_id": str(run_id)})
    row = dict(row_mapping)
    if row["status"] not in CANCELLABLE_JOB_RUN_STATUSES:
        raise BoundaryConflict(
            "only QUEUED or RUNNING job runs can be cancelled",
            details={"run_id": str(run_id), "status": row["status"]},
        )
    now = datetime.now(UTC)
    await session.execute(
        update(p.job_run)
        .where(p.job_run.c.tenant_id == tid, p.job_run.c.id == _as_uuid(run_id))
        .values(
            status="CANCELLED",
            finished_at=now,
            updated_at=now,
            result_summary={"cancel_reason": normalised_reason},
            version=p.job_run.c.version + 1,
        )
    )
    return {
        "run": {
            **_serialize_job_run(row, job_name=str(row["job_name"])),
            "status": "CANCELLED",
            "finished_at": _iso(now),
            "result_summary": {"cancel_reason": normalised_reason},
        },
        "status": "CANCELLED",
    }
