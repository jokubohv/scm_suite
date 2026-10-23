"""FS-09 manufacturing domain service (assembly/BOM/WO/WIP/build/unbuild/outsourced/ECO/scheduling/
capacity/available-to-build/compatibility).

All stock and value effects go through the frozen FS-04 ``PostingPort`` / ``BalanceReadPort`` and
FS-05 ``CostingPort``; procurement drafts go through the FS-08 ``ProcurementFacade``. The service
participates in the caller's AsyncSession transaction (no commits, no HTTP), and persists manufacturing
documents/read models only. Modules may substitute the ports in tests (``posting=``/``balances=``).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_CEILING, Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.manufacturing import compat as compat_engine
from app.modules.manufacturing import tables as t
from app.modules.manufacturing.errors import (
    BomCycleError,
    CompatibilityViolation,
    DomainDisabled,
    InsufficientAvailable,
    InvalidStateTransition,
    NotFoundError,
    PolicyViolation,
    UnbuildDispositionInvalid,
    UnbuildNotReversible,
    ValidationFailed,
    WipMethodIncompatible,
    WoOverBuild,
)
from app.modules.manufacturing.state_machines import (
    is_same_state,
    require_reopen_allowed,
    require_transition,
)

ZERO = Decimal("0")
TENANT_SCOPED = {name: table for name, table in t.metadata.tables.items()}
TABLE_BY_NAME = {table.name: table for table in t.ALL_TABLES}
CONTEXT_SCOPES = frozenset({"CONFIGURATION", "ORDER", "CUSTOMER", "REGION", "CHANNEL"})


def utc_now() -> datetime:
    return datetime.now(UTC)


def _uuid(value: Any, *, field: str = "id") -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError) as exc:
        raise ValidationFailed(f"{field} must be a UUID", details={"field": field}) from exc


def dec(value: Any) -> Decimal:
    if value is None:
        return ZERO
    return value if isinstance(value, Decimal) else Decimal(str(value))


def qty(value: Any) -> Decimal:
    return dec(value).quantize(Decimal("0.000000000001"))


def to_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError as exc:
        raise ValidationFailed(f"invalid date {value!r}", details={"value": str(value)}) from exc


def _jsonify(value: Any) -> Any:
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(k): _jsonify(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_jsonify(v) for v in value]
    return value


def serialize(row: Mapping[str, Any] | None) -> dict[str, Any]:
    if row is None:
        return {}
    return {key: _jsonify(value) for key, value in dict(row).items()}


def _rows(result: Any) -> list[dict[str, Any]]:
    return [dict(row) for row in result.mappings().all()]


def _row(result: Any) -> dict[str, Any] | None:
    mapping = result.mappings().first()
    return dict(mapping) if mapping is not None else None


YIELD_EPSILON = Decimal("0.000000000001")


def component_requirement(
    qty_per: Any, qty_ordered: Any, *, yield_percent: Any = None, round_up: bool = False
) -> Decimal:
    """Component quantity math (RULE-MFG-11): yield-adjusted, round-up in stock UoM precision."""
    base = dec(qty_per) * dec(qty_ordered)
    yield_value = dec(yield_percent) if yield_percent is not None else Decimal("100")
    if yield_value <= 0 or yield_value > 100:
        raise ValidationFailed("component yield_percent must be > 0 and <= 100")
    required = base / (yield_value / Decimal("100"))
    if round_up:
        return required.to_integral_value(rounding=ROUND_CEILING).quantize(Decimal("0.000000000001"))
    return required.quantize(Decimal("0.000000000001"))


def component_requirement_rounded(qty_per: Any, qty_ordered: Any, *, yield_percent: Any = None) -> Decimal:
    """``ceil`` variant used by ``round_up_component_qty`` BOM members."""
    return component_requirement(qty_per, qty_ordered, yield_percent=yield_percent, round_up=True)


class ManufacturingService:
    """Request-scoped FS-09 service (caller owns the transaction)."""

    def __init__(
        self,
        session: AsyncSession,
        tenant_id: uuid.UUID | str,
        *,
        actor_id: uuid.UUID | str | None = None,
        posting: Any | None = None,
        balances: Any | None = None,
        procurement: Any | None = None,
    ) -> None:
        self.session = session
        self.tenant_id = _uuid(tenant_id, field="tenant_id")
        self.actor_id = _uuid(actor_id, field="actor_id") if actor_id else None
        self._posting = posting
        self._balances = balances
        self._procurement = procurement

    # ------------------------------------------------------------------ ports ----------------
    @property
    def posting(self) -> Any:
        if self._posting is None:
            from app.modules.inventory_ledger.facade import PostingPort

            self._posting = PostingPort(self.session, self.tenant_id)
        return self._posting

    @property
    def balances(self) -> Any:
        if self._balances is None:
            from app.modules.inventory_ledger.facade import BalanceReadPort

            self._balances = BalanceReadPort(self.session, self.tenant_id)
        return self._balances

    @property
    def procurement(self) -> Any:
        if self._procurement is None:
            from app.modules.procurement.facade import ProcurementFacade

            self._procurement = ProcurementFacade(self.session, self.tenant_id)
        return self._procurement

    # ------------------------------------------------------------------ helpers --------------
    async def _exec(self, sql: str, **params: Any) -> None:
        await self.session.execute(text(sql), {"tenant_id": self.tenant_id, **params})

    async def _one(self, sql: str, **params: Any) -> dict[str, Any] | None:
        return _row(await self.session.execute(text(sql), {"tenant_id": self.tenant_id, **params}))

    async def _all(self, sql: str, **params: Any) -> list[dict[str, Any]]:
        return _rows(await self.session.execute(text(sql), {"tenant_id": self.tenant_id, **params}))

    def _table(self, name: str) -> Any:
        table = TABLE_BY_NAME.get(name)
        if table is None:
            raise NotFoundError(f"unknown manufacturing table {name}")
        return table

    @staticmethod
    def _coerce(column: Any, value: Any) -> Any:
        """Coerce JSON/request scalar strings to the column's Python type (asyncpg is strict)."""
        from datetime import date as _date
        from datetime import datetime as _datetime
        from decimal import Decimal as _Decimal

        from sqlalchemy import JSON, Boolean, Date, DateTime, Numeric
        from sqlalchemy import null as sa_null

        column_type = column.type
        if value is None:
            # SQLAlchemy's JSON type serialises Python None as the JSON literal ``null`` unless told
            # otherwise; JSONB optional columns need SQL NULL (e.g. compat_attribute_value CK).
            return sa_null() if isinstance(column_type, JSON) else None
        if not isinstance(value, str):
            return value
        if isinstance(column_type, DateTime):
            try:
                return _datetime.fromisoformat(value)
            except ValueError:
                return value
        if isinstance(column_type, Date):
            try:
                return _date.fromisoformat(value)
            except ValueError:
                return value
        if isinstance(column_type, Numeric):
            try:
                return _Decimal(value)
            except Exception:  # noqa: BLE001 - leave for the driver to report a typed error
                return value
        if isinstance(column_type, Boolean):
            return value.strip().lower() in {"true", "t", "1", "yes"}
        return value

    async def _create(self, name: str, values: Mapping[str, Any]) -> dict[str, Any]:
        from sqlalchemy import insert

        table = self._table(name)
        payload = {
            str(key): self._coerce(table.c[str(key)], value) for key, value in values.items() if str(key) in table.c
        }
        payload.setdefault("id", uuid.uuid4())
        payload["tenant_id"] = self.tenant_id
        payload.setdefault("created_by", self.actor_id)
        payload.setdefault("updated_by", self.actor_id)
        row = _row(await self.session.execute(insert(table).values(**payload).returning(table)))
        assert row is not None
        return row

    async def _update(
        self,
        name: str,
        row_id: Any,
        values: Mapping[str, Any],
        *,
        allowed: Iterable[str] | None = None,
        status_from: str | None = None,
    ) -> dict[str, Any]:
        from sqlalchemy import update

        table = self._table(name)
        allowed_set = set(allowed or table.c.keys())
        payload = {
            str(key): self._coerce(table.c[str(key)], value)
            for key, value in values.items()
            if str(key) in allowed_set and str(key) in table.c
        }
        if not payload:
            existing = await self._fetch(name, row_id)
            if existing is None:
                raise NotFoundError(f"{name} {row_id} not found")
            return existing
        payload.update(updated_at=utc_now(), updated_by=self.actor_id, version=table.c.version + 1)
        clause = [table.c.tenant_id == self.tenant_id, table.c.id == _uuid(row_id, field="id")]
        if status_from is not None:
            clause.append(table.c.status == status_from)
        row = _row(await self.session.execute(update(table).where(*clause).values(**payload).returning(table)))
        if row is None:
            current = await self._fetch(name, row_id)
            if current is None:
                raise NotFoundError(f"{name} {row_id} not found")
            raise InvalidStateTransition(
                f"{name} is in state {current.get('status')!r}, expected {status_from!r}",
                details={"current": current.get("status"), "expected": status_from},
            )
        return row

    async def _fetch(self, name: str, row_id: Any) -> dict[str, Any] | None:
        from sqlalchemy import select

        table = self._table(name)
        return _row(
            await self.session.execute(
                select(table).where(table.c.tenant_id == self.tenant_id, table.c.id == _uuid(row_id))
            )
        )

    async def _list(
        self, name: str, *, status: str | None = None, limit: int = 100, offset: int = 0
    ) -> list[dict[str, Any]]:
        from sqlalchemy import select

        table = self._table(name)
        query = select(table).where(table.c.tenant_id == self.tenant_id)
        if status and "status" in table.c:
            query = query.where(table.c.status == status)
        query = query.order_by(table.c.created_at.desc()).limit(max(1, min(limit, 500))).offset(offset)
        return _rows(await self.session.execute(query))

    async def _next_number(self, doc_type: str) -> str:
        row = await self._one(
            """
            UPDATE platform.number_sequence
               SET next_value = next_value + 1, updated_at = now(), version = version + 1
             WHERE tenant_id = :tenant_id AND doc_type = :doc_type AND scope_key IN ('TENANT','') AND period_key IS NULL
            RETURNING prefix, padding, next_value - 1 AS value
            """,
            doc_type=doc_type,
        )
        if row is None:
            prefix = {"WO": "WO-", "WIP": "WIP-", "AB": "AB-", "AU": "AU-", "ECN": "ECN-", "MFGSCH": "SCH-"}.get(
                doc_type, doc_type + "-"
            )
            return f"{prefix}000001"
        return f"{row['prefix']}{int(row['value']):0{int(row['padding'])}d}"

    async def _setting(self, key: str, default: Any = None) -> Any:
        row = await self._one(
            "SELECT value FROM platform.tenant_setting WHERE tenant_id = :tenant_id AND namespace = 'manufacturing' AND key = :key",
            key=key,
        )
        if row is None:
            return default
        value = row["value"]
        if isinstance(value, Mapping) and "value" in value and len(value) == 1:  # defensive
            value = value["value"]
        return value

    async def _flag(self, flag: str) -> bool:
        row = await self._one(
            "SELECT is_enabled FROM platform.feature_flag WHERE tenant_id = :tenant_id AND flag_key = :flag",
            flag=flag,
        )
        return bool(row and row["is_enabled"])

    async def _require_enabled(self, *, wip: bool = False) -> None:
        if not await self._flag("manufacturing.enabled"):
            raise DomainDisabled("manufacturing is disabled for this tenant")
        if wip and not await self._flag("manufacturing.wip.enabled"):
            raise DomainDisabled("manufacturing WIP is disabled for this tenant")

    async def _item(self, item_id: Any) -> dict[str, Any]:
        row = await self._one(
            "SELECT i.*, it.code AS item_type_code, ii.costing_method FROM item_master.item i "
            "JOIN item_master.item_type_definition it ON it.tenant_id = i.tenant_id AND it.id = i.item_type_id "
            "LEFT JOIN item_master.item_inventory ii ON ii.tenant_id = i.tenant_id AND ii.item_id = i.id "
            "WHERE i.tenant_id = :tenant_id AND i.id = :id",
            id=_uuid(item_id, field="item_id"),
        )
        if row is None:
            raise NotFoundError("item not found", details={"item_id": str(item_id)})
        return row

    async def _assembly(self, assembly_item_id: Any) -> dict[str, Any]:
        row = await self._one(
            """
            SELECT ai.*, i.item_number, i.display_name, ii.costing_method, i.base_uom_id
              FROM manufacturing.assembly_item ai
              JOIN item_master.item i ON i.tenant_id = ai.tenant_id AND i.id = ai.item_id
              JOIN item_master.item_inventory ii ON ii.tenant_id = i.tenant_id AND ii.item_id = i.id
             WHERE ai.tenant_id = :tenant_id AND ai.id = :id
            """,
            id=_uuid(assembly_item_id, field="assembly_item_id"),
        )
        if row is None:
            raise NotFoundError("assembly item not found", details={"assembly_item_id": str(assembly_item_id)})
        return row

    async def _assembly_item_id(self, assembly_extension_id: Any) -> uuid.UUID:
        """Item-master id for an assembly extension row (stock-movement lines reference items)."""
        row = await self._one(
            "SELECT item_id FROM manufacturing.assembly_item WHERE tenant_id = :tenant_id AND id = :id",
            id=_uuid(assembly_extension_id, field="assembly_item_id"),
        )
        if row is None:
            raise NotFoundError("assembly item not found")
        return _uuid(row["item_id"], field="item_id")

    async def _default_location(self) -> uuid.UUID:
        row = await self._one(
            "SELECT id FROM traceability.location WHERE tenant_id = :tenant_id AND NOT is_inactive ORDER BY code LIMIT 1"
        )
        if row is None:
            raise ValidationFailed("tenant has no active inventory location")
        return row["id"]

    async def _default_status(self) -> uuid.UUID:
        row = await self._one(
            "SELECT id FROM traceability.inventory_status WHERE tenant_id = :tenant_id AND code = 'AVAILABLE' LIMIT 1"
        )
        if row is None:
            raise ValidationFailed("tenant has no AVAILABLE inventory status")
        return row["id"]

    async def _uom(self, item: Mapping[str, Any]) -> uuid.UUID:
        if item.get("base_uom_id"):
            return item["base_uom_id"]
        row = await self._one(
            "SELECT uom_id FROM item_master.item_uom WHERE tenant_id = :tenant_id AND item_id = :id AND is_stock_uom LIMIT 1",
            id=item["id"],
        )
        if row is None:
            raise ValidationFailed("item has no stock UoM", details={"item_id": str(item["id"])})
        return row["uom_id"]

    # ================================================================ assembly / BOM master ==
    async def create_assembly_item(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        await self._require_enabled()
        item = await self._item(payload.get("item_id"))
        assembly_type = str(payload.get("assembly_type") or "STANDARD").upper()
        if assembly_type == "WIP":
            if not await self._flag("manufacturing.wip.enabled"):
                raise DomainDisabled("manufacturing WIP is disabled for this tenant")
            if str(item.get("costing_method") or "").upper() not in {"STANDARD", "AVERAGE"}:
                raise WipMethodIncompatible(
                    "WIP assemblies require STANDARD or AVERAGE costing", details={"item_id": str(item["id"])}
                )
        values = dict(payload)
        values["item_id"] = _uuid(payload.get("item_id"))
        row = await self._create("assembly_item", values)
        return serialize(row) or {}

    async def list_assembly_items(self, *, assembly_type: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        sql = (
            "SELECT ai.*, i.item_number, i.display_name FROM manufacturing.assembly_item ai "
            "JOIN item_master.item i ON i.tenant_id = ai.tenant_id AND i.id = ai.item_id "
            "WHERE ai.tenant_id = :tenant_id"
        )
        params: dict[str, Any] = {"limit": max(1, min(limit, 500))}
        if assembly_type:
            sql += " AND ai.assembly_type = :assembly_type"
            params["assembly_type"] = assembly_type.upper()
        sql += " ORDER BY i.item_number LIMIT :limit"
        return [serialize(row) for row in await self._all(sql, **params)]

    async def get_assembly_item(self, assembly_item_id: Any) -> dict[str, Any]:
        return serialize(await self._assembly(assembly_item_id)) or {}

    async def update_assembly_item(self, assembly_item_id: Any, payload: Mapping[str, Any]) -> dict[str, Any]:
        allowed = {
            "default_bom_id",
            "default_routing_id",
            "unbuild_variance_account_ref",
            "wip_account_ref",
            "build_cost",
            "revision_control",
            "backflush_default",
            "component_yield_percent",
            "round_up_component_qty",
            "is_outsourced",
        }
        row = await self._update("assembly_item", assembly_item_id, payload, allowed=allowed)
        return serialize(row) or {}

    # ------------------------------------------------------------------ BOMs -----------------
    async def create_bom(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        assembly = await self._assembly(payload.get("assembly_item_id"))
        values = dict(payload)
        values["assembly_item_id"] = assembly["id"]
        row = await self._create("bom", values)
        return serialize(row) or {}

    async def list_boms(
        self, *, assembly_item_id: Any | None = None, status: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM manufacturing.bom WHERE tenant_id = :tenant_id"
        params: dict[str, Any] = {"limit": max(1, min(limit, 500))}
        if assembly_item_id:
            query += " AND assembly_item_id = :assembly_item_id"
            params["assembly_item_id"] = _uuid(assembly_item_id)
        if status:
            query += " AND status = :status"
            params["status"] = status.upper()
        query += " ORDER BY created_at DESC LIMIT :limit"
        return [serialize(row) for row in await self._all(query, **params)]

    async def get_bom(self, bom_id: Any) -> dict[str, Any]:
        bom = await self._fetch("bom", bom_id)
        if bom is None:
            raise NotFoundError("BOM not found")
        revisions = await self._all(
            "SELECT * FROM manufacturing.bom_revision WHERE tenant_id = :tenant_id AND bom_id = :bom_id ORDER BY effective_from DESC",
            bom_id=bom["id"],
        )
        result = serialize(bom) or {}
        result["revisions"] = [serialize(row) for row in revisions]
        return result

    async def activate_bom(self, bom_id: Any) -> dict[str, Any]:
        return await self._update("bom", bom_id, {"status": "ACTIVE"})

    async def create_bom_revision(self, bom_id: Any, payload: Mapping[str, Any]) -> dict[str, Any]:
        bom = await self._fetch("bom", bom_id)
        if bom is None:
            raise NotFoundError("BOM not found")
        values = dict(payload)
        values["bom_id"] = bom["id"]
        row = await self._create("bom_revision", values)
        return serialize(row) or {}

    async def list_bom_revisions(self, bom_id: Any) -> list[dict[str, Any]]:
        return [
            serialize(row)
            for row in await self._all(
                "SELECT * FROM manufacturing.bom_revision WHERE tenant_id = :tenant_id AND bom_id = :bom_id ORDER BY effective_from DESC",
                bom_id=_uuid(bom_id),
            )
        ]

    async def get_bom_revision(self, revision_id: Any) -> dict[str, Any]:
        revision = await self._fetch("bom_revision", revision_id)
        if revision is None:
            raise NotFoundError("BOM revision not found")
        components = await self.list_bom_components(revision_id)
        result = serialize(revision) or {}
        result["components"] = components
        return result

    async def activate_bom_revision(self, revision_id: Any) -> dict[str, Any]:
        revision = await self._fetch("bom_revision", revision_id)
        if revision is None:
            raise NotFoundError("BOM revision not found")
        components = await self.list_bom_components(revision_id)
        if not components:
            raise ValidationFailed("cannot activate a BOM revision without components")
        overlap = await self._one(
            """
            SELECT id FROM manufacturing.bom_revision
             WHERE tenant_id = :tenant_id AND bom_id = :bom_id AND id <> :id AND status = 'ACTIVE'
               AND daterange(effective_from, effective_to, '[]') && daterange(:from_date, :to_date, '[]')
             LIMIT 1
            """,
            bom_id=revision["bom_id"],
            id=revision["id"],
            from_date=revision["effective_from"],
            to_date=revision.get("effective_to") or date(9999, 12, 31),
        )
        if overlap is not None:
            raise PolicyViolation("BOM revision effective range overlaps another ACTIVE revision")
        await self._compat_gate(
            revision["bom_id"],
            context="BOM",
            components=components,
            override=False,
            context_refs={},
        )
        row = await self._update(
            "bom_revision",
            revision_id,
            {"status": "ACTIVE", "approved_at": utc_now(), "approved_by": self.actor_id},
        )
        return serialize(row) or {}

    async def update_bom_revision(self, revision_id: Any, payload: Mapping[str, Any]) -> dict[str, Any]:
        allowed = {"revision_code", "effective_from", "effective_to", "notes"}
        return (
            serialize(await self._update("bom_revision", revision_id, payload, allowed=allowed, status_from="DRAFT"))
            or {}
        )

    async def list_bom_components(self, revision_id: Any, *, as_of: date | None = None) -> list[dict[str, Any]]:
        sql = """
            SELECT bc.*, i.item_number, i.display_name,
                   COALESCE(bc.component_yield_percent, ai.component_yield_percent) AS effective_yield_percent,
                   COALESCE(bc.round_up_component_qty, ai.round_up_component_qty) AS effective_round_up
              FROM manufacturing.bom_component bc
              JOIN item_master.item i ON i.tenant_id = bc.tenant_id AND i.id = bc.component_item_id
              LEFT JOIN manufacturing.assembly_item ai ON ai.tenant_id = bc.tenant_id AND ai.item_id = bc.component_item_id
             WHERE bc.tenant_id = :tenant_id AND bc.bom_revision_id = :revision_id
             ORDER BY bc.line_number
        """
        rows = await self._all(sql, revision_id=_uuid(revision_id))
        if as_of is None:
            return [serialize(row) or {} for row in rows]
        included: list[dict[str, Any]] = []
        for row in rows:
            controls = await self._all(
                "SELECT * FROM manufacturing.bom_component_control WHERE tenant_id = :tenant_id AND bom_component_id = :id",
                id=row["id"],
            )
            if not controls:
                included.append(row)
                continue
            active = [
                control
                for control in controls
                if control["effective_from"] <= as_of
                and (control.get("effective_to") is None or control["effective_to"] >= as_of)
                and (control.get("obsolete_date") is None or control["obsolete_date"] > as_of)
            ]
            if any(control["is_included"] for control in active):
                included.append(row)
        return [serialize(row) or {} for row in included]

    async def add_bom_component(self, revision_id: Any, payload: Mapping[str, Any]) -> dict[str, Any]:
        revision = await self._fetch("bom_revision", revision_id)
        if revision is None:
            raise NotFoundError("BOM revision not found")
        if revision["status"] != "DRAFT":
            raise InvalidStateTransition("components can only be edited on DRAFT revisions")
        bom = await self._fetch("bom", revision["bom_id"])
        assert bom is not None
        assembly = await self._fetch("assembly_item", bom["assembly_item_id"])
        assert assembly is not None
        component_item_id = _uuid(payload.get("component_item_id"), field="component_item_id")
        if component_item_id == assembly["item_id"]:
            raise ValidationFailed("a BOM cannot contain its own assembly")
        await self._assert_no_bom_cycle(assembly["item_id"], component_item_id)
        values = dict(payload)
        values["bom_revision_id"] = revision["id"]
        row = await self._create("bom_component", values)
        return serialize(row) or {}

    async def _assert_no_bom_cycle(self, assembly_item_id: uuid.UUID, component_item_id: uuid.UUID) -> None:
        seen: set[uuid.UUID] = set()
        stack = [component_item_id]
        depth = 0
        max_depth = int(await self._setting("bom.max_depth", 20) or 20)
        while stack:
            current = stack.pop()
            if current in seen:
                continue
            seen.add(current)
            depth += 1
            if depth > max_depth:
                raise BomCycleError("BOM explosion depth exceeded", details={"max_depth": max_depth})
            if current == assembly_item_id:
                raise BomCycleError("BOM cycle detected: the assembly is reachable through its components")
            rows = await self._all(
                "SELECT bc.component_item_id FROM manufacturing.bom_component bc "
                "JOIN manufacturing.bom_revision br ON br.tenant_id = bc.tenant_id AND br.id = bc.bom_revision_id "
                "JOIN manufacturing.bom b ON b.tenant_id = br.tenant_id AND b.id = br.bom_id "
                "JOIN manufacturing.assembly_item ai ON ai.tenant_id = b.tenant_id AND ai.id = b.assembly_item_id "
                "WHERE bc.tenant_id = :tenant_id AND ai.item_id = :item_id",
                item_id=current,
            )
            stack.extend(row["component_item_id"] for row in rows)

    async def update_bom_component(self, component_id: Any, payload: Mapping[str, Any]) -> dict[str, Any]:
        row = await self._update(
            "bom_component",
            component_id,
            payload,
            allowed={
                "qty_per",
                "component_yield_percent",
                "round_up_component_qty",
                "operation_sequence",
                "sort_order",
            },
        )
        return serialize(row) or {}

    async def delete_bom_component(self, component_id: Any) -> None:
        row = await self._fetch("bom_component", component_id)
        if row is None:
            raise NotFoundError("BOM component not found")
        revision = await self._fetch("bom_revision", row["bom_revision_id"])
        if revision is None or revision["status"] != "DRAFT":
            raise InvalidStateTransition("components can only be removed from DRAFT revisions")
        await self._exec(
            "DELETE FROM manufacturing.bom_component WHERE tenant_id = :tenant_id AND id = :id", id=row["id"]
        )

    async def upsert_component_control(self, component_id: Any, payload: Mapping[str, Any]) -> dict[str, Any]:
        component = await self._fetch("bom_component", component_id)
        if component is None:
            raise NotFoundError("BOM component not found")
        existing = await self._one(
            "SELECT id FROM manufacturing.bom_component_control WHERE tenant_id = :tenant_id "
            "AND bom_component_id = :component_id AND control_type = :control_type AND effective_from = :effective_from",
            component_id=component["id"],
            control_type=str(payload.get("control_type") or "EFFECTIVE_DATE").upper(),
            effective_from=payload.get("effective_from"),
        )
        values = dict(payload)
        values["bom_component_id"] = component["id"]
        if existing is not None:
            row = await self._update(
                "bom_component_control",
                existing["id"],
                values,
                allowed={"revision_code", "effective_from", "effective_to", "obsolete_date", "is_included"},
            )
        else:
            row = await self._create("bom_component_control", values)
        return serialize(row) or {}

    async def list_component_controls(self, component_id: Any) -> list[dict[str, Any]]:
        return [
            serialize(row)
            for row in await self._all(
                "SELECT * FROM manufacturing.bom_component_control WHERE tenant_id = :tenant_id AND bom_component_id = :id ORDER BY effective_from",
                id=_uuid(component_id),
            )
        ]

    async def resolve_bom(
        self,
        assembly_item_id: Any,
        *,
        as_of: date | None = None,
        bom_revision_id: Any | None = None,
        qty_ordered: Any = 1,
        location_id: Any | None = None,
    ) -> list[dict[str, Any]]:
        """Resolve the effective multi-level component set (phantom-expanded, yield/round-up applied)."""
        moment = as_of or date.today()
        assembly = await self._fetch("assembly_item", assembly_item_id)
        if assembly is None:
            raise NotFoundError("assembly item not found")
        result: list[dict[str, Any]] = []
        await self._explode(
            assembly["id"],
            qty_ordered=qty_ordered,
            as_of=moment,
            revision_id=bom_revision_id,
            level=1,
            result=result,
            visited=(),
        )
        return result

    async def _resolve_revision(
        self, assembly_item_id: uuid.UUID, *, as_of: date, revision_id: Any | None
    ) -> dict[str, Any] | None:
        if revision_id is not None:
            return await self._fetch("bom_revision", revision_id)
        bom = await self._one(
            "SELECT * FROM manufacturing.bom WHERE tenant_id = :tenant_id AND assembly_item_id = :assembly_item_id "
            "AND status = 'ACTIVE' ORDER BY is_default DESC, created_at LIMIT 1",
            assembly_item_id=assembly_item_id,
        )
        if bom is None:
            return None
        revisions = await self._all(
            "SELECT * FROM manufacturing.bom_revision WHERE tenant_id = :tenant_id AND bom_id = :bom_id "
            "AND status = 'ACTIVE' AND effective_from <= :as_of "
            "AND (effective_to IS NULL OR effective_to >= :as_of) ORDER BY effective_from DESC LIMIT 1",
            bom_id=bom["id"],
            as_of=as_of,
        )
        return revisions[0] if revisions else None

    async def _explode(
        self,
        assembly_item_id: uuid.UUID,
        *,
        qty_ordered: Decimal,
        as_of: date,
        revision_id: Any | None,
        level: int,
        result: list[dict[str, Any]],
        visited: tuple[uuid.UUID, ...],
    ) -> None:
        assembly = await self._fetch("assembly_item", assembly_item_id)
        if assembly is None:
            return
        max_depth = int(await self._setting("bom.max_depth", 20) or 20)
        if level > max_depth:
            raise BomCycleError("BOM explosion depth exceeded", details={"max_depth": max_depth})
        if assembly_item_id in visited:
            raise BomCycleError("BOM cycle detected during explosion")
        revision = await self._resolve_revision(assembly_item_id, as_of=as_of, revision_id=revision_id)
        if revision is None:
            return
        components = await self.list_bom_components(revision["id"], as_of=as_of)
        for component in components:
            component_item = await self._item(component["component_item_id"])
            qty_per = dec(component["qty_per"])
            yield_percent = component.get("effective_yield_percent")
            round_up = bool(component.get("effective_round_up") or component.get("round_up_component_qty"))
            required = component_requirement(qty_per, qty_ordered, yield_percent=yield_percent, round_up=round_up)
            component_uom = component.get("uom_id") or await self._uom(component_item)
            child_assembly = await self._one(
                "SELECT * FROM manufacturing.assembly_item WHERE tenant_id = :tenant_id AND item_id = :item_id",
                item_id=_uuid(component["component_item_id"]),
            )
            is_phantom = bool(component.get("is_phantom")) or (
                child_assembly is not None and str(child_assembly["assembly_type"]) == "PHANTOM"
            )
            if is_phantom:
                await self._explode(
                    child_assembly["id"] if child_assembly else component["component_item_id"],
                    qty_ordered=required,
                    as_of=as_of,
                    revision_id=None,
                    level=level + 1,
                    result=result,
                    visited=visited + (assembly_item_id,),
                )
                continue
            result.append(
                {
                    "level": level,
                    "component_item_id": str(component["component_item_id"]),
                    "item_number": component_item["item_number"],
                    "qty_required": str(required),
                    "qty_per": str(qty_per),
                    "uom_id": str(component_uom),
                    "component_yield_percent": str(yield_percent) if yield_percent is not None else None,
                    "round_up_component_qty": round_up,
                    "is_phantom": False,
                    "operation_sequence": component.get("operation_sequence"),
                    "source_bom_component_id": str(component["id"]),
                    "bom_revision_id": str(revision["id"]),
                }
            )

    async def bom_inquiry(self, assembly_item_id: Any, *, qty: Any = 1, as_of: date | None = None) -> dict[str, Any]:
        assembly = await self._assembly(assembly_item_id)
        lines = await self.resolve_bom(assembly["id"], as_of=as_of, qty_ordered=qty)
        return {
            "assembly_item_id": str(assembly["id"]),
            "item_number": assembly["item_number"],
            "qty": str(qty),
            "lines": lines,
        }

    async def where_used(self, component_item_id: Any, *, limit: int = 200) -> dict[str, Any]:
        item = await self._item(component_item_id)
        rows = await self._all(
            """
            SELECT bc.component_item_id, b.bom_code, br.revision_code, br.status, ai.id AS assembly_item_id,
                   i.item_number AS assembly_item_number
              FROM manufacturing.bom_component bc
              JOIN manufacturing.bom_revision br ON br.tenant_id = bc.tenant_id AND br.id = bc.bom_revision_id
              JOIN manufacturing.bom b ON b.tenant_id = br.tenant_id AND b.id = br.bom_id
              JOIN manufacturing.assembly_item ai ON ai.tenant_id = b.tenant_id AND ai.id = b.assembly_item_id
              JOIN item_master.item i ON i.tenant_id = ai.tenant_id AND i.id = ai.item_id
             WHERE bc.tenant_id = :tenant_id AND bc.component_item_id = :item_id
             ORDER BY i.item_number LIMIT :limit
            """,
            item_id=item["id"],
            limit=min(limit, 500),
        )
        return {
            "component_item_id": str(item["id"]),
            "item_number": item["item_number"],
            "parents": [serialize(row) for row in rows],
        }

    async def costed_bom_inquiry(
        self, assembly_item_id: Any, *, qty: Any = 1, as_of: date | None = None
    ) -> dict[str, Any]:
        assembly = await self._assembly(assembly_item_id)
        lines = await self.resolve_bom(assembly["id"], as_of=as_of, qty_ordered=qty)
        total = dec(assembly.get("build_cost") or 0)
        for line in lines:
            line["unit_cost"] = "0"
        return {"assembly_item_id": str(assembly["id"]), "qty": str(qty), "build_cost": str(total), "lines": lines}

    # ================================================================ routing / work centres ==
    async def create_routing(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        await self._item(payload.get("item_id"))
        return serialize(await self._create("routing", payload)) or {}

    async def list_routings(
        self, *, item_id: Any | None = None, status: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM manufacturing.routing WHERE tenant_id = :tenant_id"
        params: dict[str, Any] = {"limit": max(1, min(limit, 500))}
        if item_id:
            query += " AND item_id = :item_id"
            params["item_id"] = _uuid(item_id)
        if status:
            query += " AND status = :status"
            params["status"] = status.upper()
        query += " ORDER BY created_at DESC LIMIT :limit"
        return [serialize(row) for row in await self._all(query, **params)]

    async def get_routing(self, routing_id: Any) -> dict[str, Any]:
        routing = await self._fetch("routing", routing_id)
        if routing is None:
            raise NotFoundError("routing not found")
        steps = await self._all(
            "SELECT rs.*, wc.code AS work_center_code FROM manufacturing.routing_step rs "
            "JOIN manufacturing.work_center wc ON wc.tenant_id = rs.tenant_id AND wc.id = rs.work_center_id "
            "WHERE rs.tenant_id = :tenant_id AND rs.routing_id = :id ORDER BY rs.sequence",
            id=routing["id"],
        )
        result = serialize(routing) or {}
        result["steps"] = [serialize(step) for step in steps]
        return result

    async def activate_routing(self, routing_id: Any) -> dict[str, Any]:
        routing = await self._fetch("routing", routing_id)
        if routing is None:
            raise NotFoundError("routing not found")
        steps = await self._all(
            "SELECT id FROM manufacturing.routing_step WHERE tenant_id = :tenant_id AND routing_id = :id",
            id=routing["id"],
        )
        if not steps:
            raise ValidationFailed("cannot activate a routing without steps")
        return serialize(await self._update("routing", routing_id, {"status": "ACTIVE"})) or {}

    async def update_routing(self, routing_id: Any, payload: Mapping[str, Any]) -> dict[str, Any]:
        return (
            serialize(
                await self._update(
                    "routing",
                    routing_id,
                    payload,
                    allowed={"routing_code", "name", "is_default", "effective_from", "effective_to"},
                    status_from="DRAFT",
                )
            )
            or {}
        )

    async def add_routing_step(self, routing_id: Any, payload: Mapping[str, Any]) -> dict[str, Any]:
        await self._fetch("routing", routing_id)
        values = dict(payload)
        values["routing_id"] = _uuid(routing_id)
        return serialize(await self._create("routing_step", values)) or {}

    async def update_routing_step(self, step_id: Any, payload: Mapping[str, Any]) -> dict[str, Any]:
        allowed = {
            "description",
            "work_center_id",
            "setup_time_minutes",
            "run_time_per_unit_minutes",
            "cost_category_id",
            "is_outsourced",
        }
        return serialize(await self._update("routing_step", step_id, payload, allowed=allowed)) or {}

    async def add_routing_step_charge(self, step_id: Any, payload: Mapping[str, Any]) -> dict[str, Any]:
        await self._fetch("routing_step", step_id)
        values = dict(payload)
        values["routing_step_id"] = _uuid(step_id)
        return serialize(await self._create("routing_step_charge", values)) or {}

    async def list_work_centers(self, limit: int = 100) -> list[dict[str, Any]]:
        return [serialize(row) for row in await self._list("work_center", limit=limit)]

    async def create_work_center(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return serialize(await self._create("work_center", payload)) or {}

    async def update_work_center(self, work_center_id: Any, payload: Mapping[str, Any]) -> dict[str, Any]:
        allowed = {
            "name",
            "location_id",
            "calendar_id",
            "group_id",
            "cost_per_hour",
            "capacity_hours_per_day",
            "efficiency_percent",
            "is_active",
        }
        return serialize(await self._update("work_center", work_center_id, payload, allowed=allowed)) or {}

    async def create_work_center_group(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return serialize(await self._create("work_center_group", payload)) or {}

    async def list_work_center_groups(self) -> list[dict[str, Any]]:
        return [serialize(row) for row in await self._list("work_center_group")]

    async def create_work_center_calendar(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return serialize(await self._create("work_center_calendar", payload)) or {}

    async def list_work_center_calendars(self) -> list[dict[str, Any]]:
        return [serialize(row) for row in await self._list("work_center_calendar")]

    async def upsert_calendar_days(self, calendar_id: Any, days: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        await self._fetch("work_center_calendar", calendar_id)
        stored: list[dict[str, Any]] = []
        for day in days:
            values = dict(day)
            values["calendar_id"] = _uuid(calendar_id)
            existing = await self._one(
                "SELECT id FROM manufacturing.work_center_calendar_day WHERE tenant_id = :tenant_id AND calendar_id = :calendar_id "
                "AND day_of_week IS NOT DISTINCT FROM :day_of_week AND specific_date IS NOT DISTINCT FROM :specific_date AND shift_code = :shift_code",
                calendar_id=values["calendar_id"],
                day_of_week=values.get("day_of_week"),
                specific_date=values.get("specific_date"),
                shift_code=values.get("shift_code", "DAY"),
            )
            if existing is not None:
                row = await self._update("work_center_calendar_day", existing["id"], values)
            else:
                row = await self._create("work_center_calendar_day", values)
            stored.append(serialize(row) or {})
        return stored

    async def create_cost_template(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return serialize(await self._create("manufacturing_cost_template", payload)) or {}

    async def list_cost_templates(self) -> list[dict[str, Any]]:
        return [serialize(row) for row in await self._list("manufacturing_cost_template")]

    async def add_cost_template_line(self, template_id: Any, payload: Mapping[str, Any]) -> dict[str, Any]:
        await self._fetch("manufacturing_cost_template", template_id)
        values = dict(payload)
        values["template_id"] = _uuid(template_id)
        return serialize(await self._create("manufacturing_cost_template_line", values)) or {}

    # ==================================================================== work orders ========
    async def create_work_order(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        await self._require_enabled()
        assembly = await self._assembly(payload.get("assembly_item_id"))
        assembly_type = str(assembly["assembly_type"])
        if assembly_type in {"PHANTOM", "MATRIX_PARENT"}:
            raise PolicyViolation("phantom/matrix-parent assemblies cannot be work orders")
        wo_type = str(payload.get("wo_type") or ("WIP" if assembly_type == "WIP" else "STANDARD")).upper()
        if wo_type == "WIP":
            await self._require_enabled(wip=True)
            if str(assembly.get("costing_method") or "").upper() not in {"STANDARD", "AVERAGE"}:
                raise WipMethodIncompatible("WIP work orders require STANDARD or AVERAGE costing")
        qty_ordered = qty(payload.get("qty_ordered"))
        if qty_ordered <= 0:
            raise ValidationFailed("qty_ordered must be > 0")
        uom_id = payload.get("uom_id") or await self._uom(assembly)
        start_date = to_date(payload.get("start_date")) or date.today()
        end_date = to_date(payload.get("end_date")) or start_date
        if end_date < start_date:
            raise ValidationFailed("end_date must be >= start_date")
        bom_revision = await self._resolve_revision(
            assembly["id"], as_of=start_date, revision_id=payload.get("bom_revision_id")
        )
        document_number = payload.get("document_number") or await self._next_number("WO")
        header = await self._create(
            "work_order",
            {
                "document_number": document_number,
                "transaction_date": start_date,
                "status": "PLANNED",
                "assembly_item_id": assembly["id"],
                "wo_type": wo_type,
                "qty_ordered": qty_ordered,
                "uom_id": uom_id,
                "bom_revision_id": bom_revision["id"] if bom_revision else None,
                "routing_id": payload.get("routing_id"),
                "wip_location_id": payload.get("wip_location_id"),
                "start_date": start_date,
                "end_date": end_date,
                "parent_work_order_id": payload.get("parent_work_order_id"),
                "external_sales_ref": payload.get("external_sales_ref"),
                "memo": payload.get("memo"),
            },
        )
        line = await self._create(
            "work_order_line",
            {
                "work_order_id": header["id"],
                "line_number": 1,
                "assembly_item_id": assembly["id"],
                "qty_ordered": qty_ordered,
                "uom_id": uom_id,
                "due_date": end_date,
                "status": "PLANNED",
                "bom_revision_id": bom_revision["id"] if bom_revision else None,
                "routing_id": payload.get("routing_id"),
            },
        )
        components = await self.resolve_bom(
            assembly["id"],
            as_of=start_date,
            bom_revision_id=bom_revision["id"] if bom_revision else None,
            qty_ordered=qty_ordered,
        )
        for line_number, component in enumerate(components, start=1):
            await self._create(
                "work_order_component",
                {
                    "work_order_line_id": line["id"],
                    "line_number": line_number,
                    "component_item_id": _uuid(component["component_item_id"]),
                    "qty_required": dec(component["qty_required"]),
                    "uom_id": _uuid(component["uom_id"]),
                    "component_yield_percent": dec(component["component_yield_percent"])
                    if component.get("component_yield_percent")
                    else None,
                    "round_up_component_qty": bool(component.get("round_up_component_qty")),
                    "over_issue_tolerance": dec(await self._setting("over_issue_tolerance_pct", 0) or 0),
                    "operation_sequence": component.get("operation_sequence"),
                    "is_phantom": bool(component.get("is_phantom")),
                    "source_bom_component_id": _uuid(component["source_bom_component_id"])
                    if component.get("source_bom_component_id")
                    else None,
                },
            )
        routing = await self._fetch("routing", payload.get("routing_id")) if payload.get("routing_id") else None
        if routing is not None:
            steps = await self._all(
                "SELECT * FROM manufacturing.routing_step WHERE tenant_id = :tenant_id AND routing_id = :id ORDER BY sequence",
                id=routing["id"],
            )
            for step in steps:
                await self._create(
                    "work_order_operation",
                    {
                        "work_order_id": header["id"],
                        "sequence": int(step["sequence"]),
                        "routing_step_id": step["id"],
                        "work_center_id": step["work_center_id"],
                        "status": "PENDING",
                    },
                )
        await self._compat_gate(
            assembly["id"],
            context="WO",
            components=[
                {"item_id": component["component_item_id"], "qty": component["qty_required"]}
                for component in components
            ],
            override=bool(payload.get("compat_override")),
            context_refs=payload.get("compat_context_refs") or {},
            override_reason=payload.get("compat_override_reason"),
            persist=bool(payload.get("compat_validate")),
        )
        return await self.get_work_order(header["id"])

    async def list_work_orders(
        self,
        *,
        status: str | None = None,
        assembly_item_id: Any | None = None,
        external_sales_ref: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        query = (
            "SELECT wo.*, i.item_number, i.display_name FROM manufacturing.work_order wo "
            "JOIN manufacturing.assembly_item ai ON ai.tenant_id = wo.tenant_id AND ai.id = wo.assembly_item_id "
            "JOIN item_master.item i ON i.tenant_id = ai.tenant_id AND i.id = ai.item_id WHERE wo.tenant_id = :tenant_id"
        )
        params: dict[str, Any] = {"limit": max(1, min(limit, 500)), "offset": max(0, offset)}
        if status:
            query += " AND wo.status = :status"
            params["status"] = status.upper()
        if assembly_item_id:
            query += " AND wo.assembly_item_id = :assembly_item_id"
            params["assembly_item_id"] = _uuid(assembly_item_id)
        if external_sales_ref:
            query += " AND wo.external_sales_ref = :external_sales_ref"
            params["external_sales_ref"] = external_sales_ref
        query += " ORDER BY wo.created_at DESC LIMIT :limit OFFSET :offset"
        return [serialize(row) for row in await self._all(query, **params)]

    async def get_work_order(self, work_order_id: Any) -> dict[str, Any]:
        header = await self._one(
            "SELECT * FROM manufacturing.work_order WHERE tenant_id = :tenant_id AND id = :id",
            id=_uuid(work_order_id),
        )
        if header is None:
            raise NotFoundError("work order not found")
        lines = await self._all(
            "SELECT * FROM manufacturing.work_order_line WHERE tenant_id = :tenant_id AND work_order_id = :id ORDER BY line_number",
            id=header["id"],
        )
        components: list[dict[str, Any]] = []
        for line in lines:
            components.extend(
                await self._all(
                    "SELECT woc.*, i.item_number FROM manufacturing.work_order_component woc "
                    "JOIN item_master.item i ON i.tenant_id = woc.tenant_id AND i.id = woc.component_item_id "
                    "WHERE woc.tenant_id = :tenant_id AND woc.work_order_line_id = :line_id ORDER BY woc.line_number",
                    line_id=line["id"],
                )
            )
        operations = await self._all(
            "SELECT * FROM manufacturing.work_order_operation WHERE tenant_id = :tenant_id AND work_order_id = :id ORDER BY sequence",
            id=header["id"],
        )
        result = serialize(header) or {}
        result["lines"] = [serialize(line) for line in lines]
        result["components"] = [serialize(component) for component in components]
        result["operations"] = [serialize(operation) for operation in operations]
        return result

    async def update_work_order(self, work_order_id: Any, payload: Mapping[str, Any]) -> dict[str, Any]:
        header = await self._fetch("work_order", work_order_id)
        if header is None:
            raise NotFoundError("work order not found")
        if header["status"] not in {"PLANNED", "FIRM_PLANNED"}:
            raise InvalidStateTransition("only planned work orders can be edited")
        allowed = {"end_date", "start_date", "wip_location_id", "external_sales_ref", "memo", "routing_id"}
        row = await self._update("work_order", work_order_id, payload, allowed=allowed)
        return await self.get_work_order(row["id"])

    async def firm_work_order(self, work_order_id: Any) -> dict[str, Any]:
        header = await self._fetch("work_order", work_order_id)
        if header is None:
            raise NotFoundError("work order not found")
        if is_same_state(header["status"], "FIRM_PLANNED"):
            return await self.get_work_order(work_order_id)
        require_transition("work_order", header["status"], "FIRM_PLANNED")
        await self._update("work_order", work_order_id, {"status": "FIRM_PLANNED", "firm_planned_at": utc_now()})
        return await self.get_work_order(work_order_id)

    async def release_work_order(self, work_order_id: Any, *, wip_location_id: Any | None = None) -> dict[str, Any]:
        await self._require_enabled()
        header = await self._fetch("work_order", work_order_id)
        if header is None:
            raise NotFoundError("work order not found")
        if is_same_state(header["status"], "RELEASED"):
            return await self.get_work_order(work_order_id)
        require_transition("work_order", header["status"], "RELEASED")
        if header["wo_type"] == "WIP":
            await self._require_enabled(wip=True)
        details = await self.get_work_order(work_order_id)
        await self._compat_gate(
            header["assembly_item_id"],
            context="WO",
            components=[
                {"item_id": component["component_item_id"], "qty": component["qty_required"]}
                for component in details["components"]
            ],
            override=False,
            context_refs={},
            persist=False,
        )
        commit_on_release = bool(await self._setting("component.commit_on_release", True))
        committed = 0
        if commit_on_release:
            for component in details["components"]:
                if dec(component["qty_required"]) <= dec(component.get("qty_issued") or 0):
                    continue
                payload = {
                    "item_id": component["component_item_id"],
                    "location_id": wip_location_id or header.get("wip_location_id") or await self._default_location(),
                    "inventory_status_id": await self._default_status(),
                    "qty": str(dec(component["qty_required"]) - dec(component.get("qty_issued") or 0)),
                    "source_type": "WORK_ORDER",
                    "source_line_ref": component["id"],
                    "required_by_date": str(header["start_date"]),
                }
                try:
                    result = await self.posting.commit(
                        payload, idempotency_key=f"wo-release:{header['id']}:{component['id']}"
                    )
                except Exception as exc:  # insufficient availability is a typed conflict
                    raise InsufficientAvailable(
                        "cannot release work order: component availability insufficient",
                        details={"component_item_id": component["component_item_id"], "error": str(exc)},
                    ) from exc
                commitment_id = result.get("commitment_id")
                if commitment_id:
                    await self._exec(
                        "UPDATE manufacturing.work_order_component SET qty_committed = qty_required, updated_at = now(), version = version + 1 "
                        "WHERE tenant_id = :tenant_id AND id = :id",
                        id=_uuid(component["id"]),
                    )
                committed += 1
        await self._update(
            "work_order",
            work_order_id,
            {
                "status": "RELEASED",
                "released_at": utc_now(),
                "wip_location_id": wip_location_id or header.get("wip_location_id"),
            },
        )
        await self._exec(
            "UPDATE manufacturing.work_order_line SET status = 'RELEASED', updated_at = now(), version = version + 1 "
            "WHERE tenant_id = :tenant_id AND work_order_id = :wo AND status = 'PLANNED'",
            wo=header["id"],
        )
        result = await self.get_work_order(work_order_id)
        result["components_committed"] = committed
        return result

    async def void_work_order(self, work_order_id: Any, *, reason: str = "") -> dict[str, Any]:
        header = await self._fetch("work_order", work_order_id)
        if header is None:
            raise NotFoundError("work order not found")
        if header["status"] in {"BUILT", "CLOSED"}:
            raise InvalidStateTransition("posted work orders must be reversed before voiding")
        posted = await self._one(
            "SELECT id FROM manufacturing.wip_transaction WHERE tenant_id = :tenant_id AND work_order_id = :wo AND status = 'POSTED' LIMIT 1",
            wo=header["id"],
        )
        if posted is not None:
            raise InvalidStateTransition("work order has posted WIP transactions; reverse them first")
        require_transition("work_order", header["status"], "VOID")
        await self._update("work_order", work_order_id, {"status": "VOID"})
        return await self.get_work_order(work_order_id)

    async def reopen_work_order(self, work_order_id: Any, *, reason: str = "") -> dict[str, Any]:
        header = await self._fetch("work_order", work_order_id)
        if header is None:
            raise NotFoundError("work order not found")
        require_transition("work_order", header["status"], "RELEASED")
        require_reopen_allowed(header["wo_type"])
        await self._update("work_order", work_order_id, {"status": "RELEASED"})
        return await self.get_work_order(work_order_id)

    async def pick_requirements(self, work_order_id: Any) -> dict[str, Any]:
        details = await self.get_work_order(work_order_id)
        requirements = []
        for component in details["components"]:
            remaining = dec(component["qty_required"]) - dec(component.get("qty_issued") or 0)
            if remaining <= 0:
                continue
            requirements.append(
                {
                    "component_item_id": component["component_item_id"],
                    "item_number": component.get("item_number"),
                    "qty_required": component["qty_required"],
                    "qty_issued": component.get("qty_issued") or "0",
                    "qty_remaining": str(remaining),
                    "uom_id": component["uom_id"],
                    "wip_location_id": details.get("wip_location_id"),
                    "lot_required": False,
                }
            )
        return {"work_order_id": details["id"], "components": requirements}

    # ==================================================================== WIP flow ============
    async def issue_components(self, work_order_id: Any, payload: Mapping[str, Any]) -> dict[str, Any]:
        await self._require_enabled(wip=True)
        header = await self._fetch("work_order", work_order_id)
        if header is None:
            raise NotFoundError("work order not found")
        if header["status"] not in {"RELEASED", "IN_PROCESS"}:
            raise InvalidStateTransition("components can only be issued to a released/in-process work order")
        details = await self.get_work_order(work_order_id)
        requested = payload.get("lines")
        if requested is None:
            lines = [
                {
                    "work_order_component_id": component["id"],
                    "qty": str(dec(component["qty_required"]) - dec(component.get("qty_issued") or 0)),
                }
                for component in details["components"]
                if dec(component["qty_required"]) > dec(component.get("qty_issued") or 0)
            ]
        else:
            lines = list(requested)
        if not lines:
            raise ValidationFailed("nothing to issue")
        wip_location = payload.get("location_id") or header.get("wip_location_id") or await self._default_location()
        status_id = payload.get("inventory_status_id") or await self._default_status()
        transaction_date = payload.get("transaction_date") or date.today()
        component_map = {component["id"]: component for component in details["components"]}
        doc = await self._create(
            "wip_transaction",
            {
                "document_number": payload.get("document_number") or await self._next_number("WIP"),
                "transaction_date": transaction_date,
                "status": "DRAFT",
                "wip_transaction_type": "ISSUE",
                "work_order_id": header["id"],
                "is_backflush": bool(payload.get("is_backflush", False)),
                "memo": payload.get("memo"),
            },
        )
        movements: list[dict[str, Any]] = []
        for index, line in enumerate(lines, start=1):
            component = component_map.get(str(line.get("work_order_component_id")))
            if component is None:
                raise ValidationFailed("unknown work_order_component_id", details={"line": line})
            quantity = qty(line.get("qty"))
            if quantity <= 0:
                raise ValidationFailed("issue qty must be > 0")
            already = dec(component.get("qty_issued") or 0)
            tolerance = dec(component.get("over_issue_tolerance") or 0)
            allowed = dec(component["qty_required"]) * (1 + tolerance / 100)
            if already + quantity > allowed + YIELD_EPSILON:
                raise WoOverBuild(
                    "component over-issue exceeds tolerance",
                    details={
                        "component_id": component["id"],
                        "max": str(allowed),
                        "requested": str(already + quantity),
                    },
                )
            item = await self._item(component["component_item_id"])
            await self._create(
                "wip_transaction_line",
                {
                    "wip_transaction_id": doc["id"],
                    "line_number": index,
                    "item_id": item["id"],
                    "qty": quantity,
                    "uom_id": component["uom_id"],
                    "unit_cost": ZERO,
                    "value_base": ZERO,
                    "is_assembly_output": False,
                    "work_order_component_id": _uuid(component["id"]),
                    "location_id": _uuid(wip_location),
                    "inventory_status_id": _uuid(status_id),
                },
            )
            movements.append(
                {
                    "item_id": str(item["id"]),
                    "location_id": str(wip_location),
                    "inventory_status_id": str(status_id),
                    "qty": str(-quantity),
                    "movement_type": "WIP_ISSUE",
                    "source_line_ref": str(index),
                }
            )
        result = await self.posting.post_wip_issue(
            transaction_date=transaction_date,
            source_doc_type="WIP_TRANSACTION",
            source_doc_id=str(doc["id"]),
            idempotency_key=payload.get("idempotency_key") or f"wip-issue:{doc['id']}",
            movements=movements,
        )
        for line in lines:
            component = component_map[str(line["work_order_component_id"])]
            quantity = qty(line.get("qty"))
            await self._exec(
                "UPDATE manufacturing.work_order_component SET qty_issued = qty_issued + :qty, updated_at = now(), version = version + 1 "
                "WHERE tenant_id = :tenant_id AND id = :id",
                qty=quantity,
                id=_uuid(component["id"]),
            )
        value_issued = dec(payload.get("value_issued") or 0)
        await self._update(
            "wip_transaction",
            doc["id"],
            {"status": "POSTED", "posted_transaction_id": _uuid(result["transaction_id"], field="transaction_id")},
        )
        if header["status"] == "RELEASED":
            await self._update("work_order", header["id"], {"status": "IN_PROCESS"})
        await self._bump_wip_balance(
            header["id"], qty_issued=sum((qty(line.get("qty")) for line in lines), ZERO), value_issued=value_issued
        )
        return {**(await self.get_wip_transaction(doc["id"])), "posting": result}

    async def get_wip_transaction(self, wip_transaction_id: Any) -> dict[str, Any]:
        transaction = await self._fetch("wip_transaction", wip_transaction_id)
        if transaction is None:
            raise NotFoundError("WIP transaction not found")
        lines = await self._all(
            "SELECT * FROM manufacturing.wip_transaction_line WHERE tenant_id = :tenant_id AND wip_transaction_id = :id ORDER BY line_number",
            id=transaction["id"],
        )
        result = serialize(transaction) or {}
        result["lines"] = [serialize(line) for line in lines]
        return result

    async def get_wip_balance(self, work_order_id: Any) -> dict[str, Any]:
        row = await self._one(
            "SELECT * FROM manufacturing.work_order_wip_balance WHERE tenant_id = :tenant_id AND work_order_id = :id",
            id=_uuid(work_order_id),
        )
        return serialize(row) or {
            "work_order_id": str(work_order_id),
            "qty_issued": "0",
            "value_issued": "0",
            "qty_completed": "0",
            "value_completed": "0",
            "wip_value": "0",
            "status": "OPEN",
        }

    async def _bump_wip_balance(
        self,
        work_order_id: uuid.UUID,
        *,
        qty_issued: Decimal = ZERO,
        value_issued: Decimal = ZERO,
        qty_completed: Decimal = ZERO,
        value_completed: Decimal = ZERO,
    ) -> dict[str, Any]:
        current = await self._one(
            "SELECT * FROM manufacturing.work_order_wip_balance WHERE tenant_id = :tenant_id AND work_order_id = :id",
            id=work_order_id,
        )
        if current is None:
            current = await self._create(
                "work_order_wip_balance",
                {
                    "work_order_id": work_order_id,
                    "qty_issued": ZERO,
                    "value_issued": ZERO,
                    "qty_completed": ZERO,
                    "value_completed": ZERO,
                    "wip_value": ZERO,
                    "status": "OPEN",
                },
            )
        issued_qty = dec(current["qty_issued"]) + qty_issued
        issued_value = dec(current["value_issued"]) + value_issued
        completed_qty = dec(current["qty_completed"]) + qty_completed
        completed_value = dec(current["value_completed"]) + value_completed
        wip_value = issued_value - completed_value
        await self._exec(
            "UPDATE manufacturing.work_order_wip_balance SET qty_issued = :qi, value_issued = :vi, qty_completed = :qc, "
            "value_completed = :vc, wip_value = :wip, updated_at = now(), version = version + 1 "
            "WHERE tenant_id = :tenant_id AND work_order_id = :wo",
            qi=issued_qty,
            vi=issued_value,
            qc=completed_qty,
            vc=completed_value,
            wip=wip_value,
            wo=work_order_id,
        )
        return await self.get_wip_balance(work_order_id)

    async def complete_work_order(self, work_order_id: Any, payload: Mapping[str, Any]) -> dict[str, Any]:
        await self._require_enabled(wip=True)
        header = await self._fetch("work_order", work_order_id)
        if header is None:
            raise NotFoundError("work order not found")
        if header["status"] not in {"RELEASED", "IN_PROCESS"}:
            raise InvalidStateTransition("work order must be released/in-process to complete")
        details = await self.get_work_order(work_order_id)
        quantity = qty(payload.get("qty"))
        if quantity <= 0:
            raise ValidationFailed("completion qty must be > 0")
        remaining = dec(header["qty_ordered"]) - dec(header["qty_built"])
        if quantity > remaining + YIELD_EPSILON:
            raise WoOverBuild("completion exceeds ordered quantity", details={"remaining": str(remaining)})
        transaction_date = payload.get("transaction_date") or date.today()
        location_id = payload.get("location_id") or header.get("wip_location_id") or await self._default_location()
        status_id = payload.get("inventory_status_id") or await self._default_status()
        is_backflush = bool(payload.get("backflush", await self._setting("backflush.explicit_enabled", False)))
        components_to_consume: list[dict[str, Any]] = []
        if is_backflush:
            for component in details["components"]:
                pending = dec(component["qty_required"]) - dec(component.get("qty_issued") or 0)
                if pending > 0:
                    components_to_consume.append({"component": component, "qty": pending})
        doc = await self._create(
            "wip_transaction",
            {
                "document_number": await self._next_number("WIP"),
                "transaction_date": transaction_date,
                "status": "DRAFT",
                "wip_transaction_type": "COMPLETION",
                "work_order_id": header["id"],
                "is_backflush": is_backflush,
            },
        )
        movements: list[dict[str, Any]] = []
        line_number = 0
        for entry in components_to_consume:
            component = entry["component"]
            line_number += 1
            await self._create(
                "wip_transaction_line",
                {
                    "wip_transaction_id": doc["id"],
                    "line_number": line_number,
                    "item_id": component["component_item_id"],
                    "qty": entry["qty"],
                    "uom_id": component["uom_id"],
                    "unit_cost": ZERO,
                    "value_base": ZERO,
                    "is_assembly_output": False,
                    "work_order_component_id": _uuid(component["id"]),
                    "location_id": _uuid(location_id),
                    "inventory_status_id": _uuid(status_id),
                },
            )
            movements.append(
                {
                    "item_id": component["component_item_id"],
                    "location_id": str(location_id),
                    "inventory_status_id": str(status_id),
                    "qty": str(-entry["qty"]),
                    "movement_type": "WIP_CONSUMPTION",
                    "source_line_ref": str(line_number),
                }
            )
            await self._exec(
                "UPDATE manufacturing.work_order_component SET qty_issued = qty_required, updated_at = now(), version = version + 1 "
                "WHERE tenant_id = :tenant_id AND id = :id",
                id=_uuid(component["id"]),
            )
        line_number += 1
        output_item_id = await self._assembly_item_id(details["assembly_item_id"])
        await self._create(
            "wip_transaction_line",
            {
                "wip_transaction_id": doc["id"],
                "line_number": line_number,
                "item_id": output_item_id,
                "qty": quantity,
                "uom_id": details["uom_id"],
                "unit_cost": ZERO,
                "value_base": ZERO,
                "is_assembly_output": True,
                "location_id": _uuid(location_id),
                "inventory_status_id": _uuid(status_id),
            },
        )
        movements.append(
            {
                "item_id": str(output_item_id),
                "location_id": str(location_id),
                "inventory_status_id": str(status_id),
                "qty": str(quantity),
                "movement_type": "WIP_COMPLETION",
                "source_line_ref": str(line_number),
            }
        )
        completion_value = dec(payload.get("value_completed") or 0)
        result = await self.posting.post_wip_completion(
            transaction_date=transaction_date,
            source_doc_type="WIP_TRANSACTION",
            source_doc_id=str(doc["id"]),
            idempotency_key=payload.get("idempotency_key") or f"wip-complete:{doc['id']}",
            movements=movements,
        )
        await self._update(
            "wip_transaction",
            doc["id"],
            {"status": "POSTED", "posted_transaction_id": _uuid(result["transaction_id"], field="transaction_id")},
        )
        new_built = dec(header["qty_built"]) + quantity
        await self._update(
            "work_order",
            header["id"],
            {"qty_built": new_built, "status": "BUILT" if new_built >= dec(header["qty_ordered"]) else "IN_PROCESS"},
        )
        await self._exec(
            "UPDATE manufacturing.work_order_line SET qty_built = :qty, status = CASE WHEN :qty >= qty_ordered THEN 'BUILT' ELSE 'IN_PROCESS' END, "
            "updated_at = now(), version = version + 1 WHERE tenant_id = :tenant_id AND work_order_id = :wo",
            qty=new_built,
            wo=header["id"],
        )
        await self._bump_wip_balance(
            header["id"],
            qty_completed=quantity,
            value_completed=completion_value,
            qty_issued=sum((entry["qty"] for entry in components_to_consume), ZERO) if is_backflush else ZERO,
        )
        return {**(await self.get_wip_transaction(doc["id"])), "posting": result}

    async def backflush_work_order(
        self, work_order_id: Any, payload: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        await self._require_enabled(wip=True)
        header = await self._fetch("work_order", work_order_id)
        if header is None:
            raise NotFoundError("work order not found")
        details = await self.get_work_order(work_order_id)
        components = [
            {
                "work_order_component_id": component["id"],
                "qty": str(dec(component["qty_required"]) - dec(component.get("qty_issued") or 0)),
            }
            for component in details["components"]
            if dec(component["qty_required"]) > dec(component.get("qty_issued") or 0)
        ]
        if not components:
            return {"work_order_id": str(work_order_id), "backflushed": 0}
        result = await self.issue_components(
            work_order_id, {**(payload or {}), "lines": components, "is_backflush": True}
        )
        return {"work_order_id": str(work_order_id), "backflushed": len(components), "wip_transaction_id": result["id"]}

    async def close_work_order(self, work_order_id: Any, payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
        payload = dict(payload or {})
        header = await self._fetch("work_order", work_order_id)
        if header is None:
            raise NotFoundError("work order not found")
        if header["status"] not in {"BUILT", "RELEASED", "IN_PROCESS"}:
            raise InvalidStateTransition("only built/released work orders can be closed")
        wip = await self.get_wip_balance(work_order_id)
        if header["wo_type"] == "WIP":
            await self._require_enabled(wip=True)
            transaction_date = payload.get("transaction_date") or date.today()
            location_id = payload.get("location_id") or header.get("wip_location_id") or await self._default_location()
            close_item_id = await self._assembly_item_id(header["assembly_item_id"])
            residual = dec(wip.get("wip_value") or 0)
            doc = await self._create(
                "wip_transaction",
                {
                    "document_number": await self._next_number("WIP"),
                    "transaction_date": transaction_date,
                    "status": "DRAFT",
                    "wip_transaction_type": "CLOSE",
                    "work_order_id": header["id"],
                },
            )
            await self._create(
                "wip_transaction_line",
                {
                    "wip_transaction_id": doc["id"],
                    "line_number": 1,
                    "item_id": close_item_id,
                    "qty": ZERO if residual > 0 else Decimal("0.000000000001"),
                    "uom_id": header["uom_id"],
                    "unit_cost": residual,
                    "value_base": residual,
                    "is_assembly_output": False,
                    "location_id": _uuid(location_id),
                    "inventory_status_id": await self._default_status(),
                },
            )
            close_result = await self.posting.post_wip_close(
                transaction_date=transaction_date,
                source_doc_type="WIP_TRANSACTION",
                source_doc_id=str(doc["id"]),
                idempotency_key=payload.get("idempotency_key") or f"wip-close:{doc['id']}",
                movements=[
                    {
                        "item_id": str(close_item_id),
                        "location_id": str(location_id),
                        "inventory_status_id": str(await self._default_status()),
                        "qty": "0.000000000001",
                        "movement_type": "WIP_CLOSE",
                        "value": str(residual),
                    }
                ],
            )
            await self._update(
                "wip_transaction",
                doc["id"],
                {
                    "status": "POSTED",
                    "posted_transaction_id": _uuid(close_result["transaction_id"], field="transaction_id"),
                },
            )
            await self._exec(
                "UPDATE manufacturing.work_order_wip_balance SET status = 'CLOSED', wip_value = 0, updated_at = now(), version = version + 1 "
                "WHERE tenant_id = :tenant_id AND work_order_id = :wo",
                wo=header["id"],
            )
        require_transition("work_order", header["status"], "CLOSED")
        await self._update("work_order", work_order_id, {"status": "CLOSED"})
        await self._exec(
            "UPDATE manufacturing.work_order_line SET status = 'CLOSED', updated_at = now(), version = version + 1 "
            "WHERE tenant_id = :tenant_id AND work_order_id = :wo",
            wo=header["id"],
        )
        return await self.get_work_order(work_order_id)

    async def reverse_wip_transaction(
        self, wip_transaction_id: Any, payload: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        payload = dict(payload or {})
        transaction = await self._fetch("wip_transaction", wip_transaction_id)
        if transaction is None:
            raise NotFoundError("WIP transaction not found")
        require_transition("wip_transaction", transaction["status"], "REVERSED")
        if not transaction.get("posted_transaction_id"):
            raise InvalidStateTransition("WIP transaction was not posted")
        await self.posting.reverse(
            str(transaction["posted_transaction_id"]),
            reason=payload.get("reason") or "wip reversal",
            idempotency_key=payload.get("idempotency_key") or f"wip-reverse:{transaction['id']}",
        )
        await self._update("wip_transaction", wip_transaction_id, {"status": "REVERSED"})
        return await self.get_wip_transaction(wip_transaction_id)

    async def add_operation_completion(self, operation_id: Any, payload: Mapping[str, Any]) -> dict[str, Any]:
        operation = await self._fetch("work_order_operation", operation_id)
        if operation is None:
            raise NotFoundError("work order operation not found")
        completed = qty(payload.get("qty_completed"))
        scrapped = qty(payload.get("qty_scrapped") or 0)
        if completed < 0 or scrapped < 0:
            raise ValidationFailed("completion quantities cannot be negative")
        if await self._setting("completion.validate_sequence", True) and not payload.get("override_sequence"):
            prior = await self._one(
                "SELECT id FROM manufacturing.work_order_operation WHERE tenant_id = :tenant_id AND work_order_id = :wo "
                "AND sequence < :sequence AND status <> 'COMPLETED' LIMIT 1",
                wo=operation["work_order_id"],
                sequence=operation["sequence"],
            )
            if prior is not None:
                raise InvalidStateTransition("previous operation must be completed first")
        existing = await self._one(
            "SELECT COALESCE(MAX(completion_number), 0) AS last FROM manufacturing.operation_completion "
            "WHERE tenant_id = :tenant_id AND work_order_operation_id = :id",
            id=operation["id"],
        )
        completion = await self._create(
            "operation_completion",
            {
                "work_order_operation_id": operation["id"],
                "completion_number": int((existing or {}).get("last", 0)) + 1,
                "qty_completed": completed,
                "qty_scrapped": scrapped,
                "labor_hours": dec(payload.get("labor_hours") or 0),
                "machine_hours": dec(payload.get("machine_hours") or 0),
                "completed_by": self.actor_id,
                "completed_at": utc_now(),
                "notes": payload.get("notes"),
            },
        )
        total_completed = dec(operation["qty_completed"]) + completed
        status = "COMPLETED" if completed > 0 and not payload.get("keep_in_progress") else "IN_PROGRESS"
        await self._update(
            "work_order_operation",
            operation["id"],
            {
                "status": status,
                "qty_completed": total_completed,
                "qty_scrapped": dec(operation["qty_scrapped"]) + scrapped,
                "actual_start": operation.get("actual_start") or utc_now(),
                "actual_end": utc_now() if status == "COMPLETED" else None,
                "setup_minutes_actual": dec(operation["setup_minutes_actual"])
                + dec(payload.get("setup_minutes_actual") or 0),
                "run_minutes_actual": dec(operation["run_minutes_actual"])
                + dec(payload.get("run_minutes_actual") or 0),
            },
        )
        return serialize(completion) or {}

    # ==================================================================== build / unbuild ====
    async def create_build(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        await self._require_enabled()
        assembly = await self._assembly(payload.get("assembly_item_id"))
        quantity = qty(payload.get("qty_built"))
        if quantity <= 0:
            raise ValidationFailed("qty_built must be > 0")
        location_id = payload.get("location_id") or await self._default_location()
        uom_id = payload.get("uom_id") or await self._uom(assembly)
        transaction_date = payload.get("transaction_date") or date.today()
        revision = await self._resolve_revision(
            assembly["id"], as_of=transaction_date, revision_id=payload.get("bom_revision_id")
        )
        build = await self._create(
            "assembly_build",
            {
                "document_number": payload.get("document_number") or await self._next_number("AB"),
                "transaction_date": transaction_date,
                "status": "DRAFT",
                "assembly_item_id": assembly["id"],
                "work_order_id": payload.get("work_order_id"),
                "qty_built": quantity,
                "uom_id": uom_id,
                "location_id": _uuid(location_id),
                "bom_revision_id": revision["id"] if revision else None,
                "build_cost": dec(payload.get("build_cost") or assembly.get("build_cost") or 0),
                "lot_id": payload.get("lot_id") if payload.get("lot_id") else None,
                "serial_ids": payload.get("serial_ids"),
                "is_from_original_build": bool(payload.get("is_from_original_build", False)),
            },
        )
        components = await self.resolve_bom(
            assembly["id"],
            as_of=transaction_date,
            bom_revision_id=revision["id"] if revision else None,
            qty_ordered=quantity,
        )
        for line_number, component in enumerate(components, start=1):
            await self._create(
                "assembly_build_component",
                {
                    "assembly_build_id": build["id"],
                    "line_number": line_number,
                    "component_item_id": _uuid(component["component_item_id"]),
                    "qty_consumed": dec(component["qty_required"]),
                    "uom_id": _uuid(component["uom_id"]),
                    "unit_cost": ZERO,
                    "location_id": _uuid(location_id),
                    "inventory_status_id": await self._default_status(),
                    "source_bom_component_id": _uuid(component["source_bom_component_id"])
                    if component.get("source_bom_component_id")
                    else None,
                },
            )
        await self._compat_gate(
            assembly["id"],
            context="BUILD",
            components=[
                {"item_id": component["component_item_id"], "qty": component["qty_required"]}
                for component in components
            ],
            override=bool(payload.get("compat_override")),
            context_refs=payload.get("compat_context_refs") or {},
            persist=bool(payload.get("compat_validate")),
        )
        return await self.get_build(build["id"])

    async def get_build(self, build_id: Any) -> dict[str, Any]:
        build = await self._fetch("assembly_build", build_id)
        if build is None:
            raise NotFoundError("assembly build not found")
        components = await self._all(
            "SELECT * FROM manufacturing.assembly_build_component WHERE tenant_id = :tenant_id AND assembly_build_id = :id ORDER BY line_number",
            id=build["id"],
        )
        result = serialize(build) or {}
        result["components"] = [serialize(component) for component in components]
        return result

    async def list_builds(self, *, assembly_item_id: Any | None = None, limit: int = 100) -> list[dict[str, Any]]:
        query = "SELECT * FROM manufacturing.assembly_build WHERE tenant_id = :tenant_id"
        params: dict[str, Any] = {"limit": max(1, min(limit, 500))}
        if assembly_item_id:
            query += " AND assembly_item_id = :assembly_item_id"
            params["assembly_item_id"] = _uuid(assembly_item_id)
        query += " ORDER BY created_at DESC LIMIT :limit"
        return [serialize(row) for row in await self._all(query, **params)]

    async def post_build(self, build_id: Any, payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
        payload = dict(payload or {})
        build = await self.get_build(build_id)
        require_transition("assembly_build", build["status"], "POSTED")
        movements: list[dict[str, Any]] = []
        for line_number, component in enumerate(build["components"], start=1):
            movements.append(
                {
                    "item_id": component["component_item_id"],
                    "location_id": component["location_id"],
                    "inventory_status_id": component["inventory_status_id"],
                    "qty": str(-dec(component["qty_consumed"])),
                    "movement_type": "ASSEMBLY_CONSUMPTION",
                    "source_line_ref": str(line_number),
                }
            )
        output_index = len(movements) + 1
        build_item_id = await self._assembly_item_id(build["assembly_item_id"])
        movements.append(
            {
                "item_id": str(build_item_id),
                "location_id": build["location_id"],
                "inventory_status_id": str(await self._default_status()),
                "qty": str(dec(build["qty_built"])),
                "movement_type": "ASSEMBLY_BUILD",
                "source_line_ref": str(output_index),
            }
        )
        result = await self.posting.post_assembly_build(
            transaction_date=build["transaction_date"],
            source_doc_type="ASSEMBLY_BUILD",
            source_doc_id=str(build["id"]),
            idempotency_key=payload.get("idempotency_key") or f"build-post:{build['id']}",
            movements=movements,
        )
        await self._update(
            "assembly_build",
            build_id,
            {"status": "POSTED", "posted_transaction_id": _uuid(result["transaction_id"], field="transaction_id")},
        )
        if build.get("work_order_id"):
            wo = await self._fetch("work_order", build["work_order_id"])
            if wo is not None:
                new_built = dec(wo["qty_built"]) + dec(build["qty_built"])
                await self._update(
                    "work_order",
                    wo["id"],
                    {
                        "qty_built": new_built,
                        "status": "BUILT" if new_built >= dec(wo["qty_ordered"]) else "IN_PROCESS",
                    },
                )
        return {**build, "status": "POSTED", "posting": result}

    async def reverse_build(self, build_id: Any, payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
        payload = dict(payload or {})
        build = await self._fetch("assembly_build", build_id)
        if build is None:
            raise NotFoundError("assembly build not found")
        require_transition("assembly_build", build["status"], "REVERSED")
        transaction_id = build.get("posted_transaction_id")
        if transaction_id is None:
            posted = await self._one(
                "SELECT posted_transaction_id AS transaction_id FROM inventory_ledger.inventory_transaction "
                "WHERE tenant_id = :tenant_id AND source_doc_type = 'ASSEMBLY_BUILD' AND source_doc_id = :id LIMIT 1",
                id=build["id"],
            )
            transaction_id = (posted or {}).get("transaction_id")
        if transaction_id is None:
            raise InvalidStateTransition("assembly build was not posted")
        await self.posting.reverse(
            str(transaction_id),
            reason=payload.get("reason") or "build reversal",
            idempotency_key=f"build-reverse:{build_id}",
        )
        await self._update("assembly_build", build_id, {"status": "REVERSED"})
        return serialize(await self._fetch("assembly_build", build_id)) or {}

    async def create_unbuild(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        await self._require_enabled()
        assembly = await self._assembly(payload.get("assembly_item_id"))
        quantity = qty(payload.get("qty_unbuilt"))
        if quantity <= 0:
            raise ValidationFailed("qty_unbuilt must be > 0")
        location_id = payload.get("location_id") or await self._default_location()
        uom_id = payload.get("uom_id") or await self._uom(assembly)
        transaction_date = payload.get("transaction_date") or date.today()
        revision = await self._resolve_revision(
            assembly["id"], as_of=transaction_date, revision_id=payload.get("bom_revision_id")
        )
        unbuild = await self._create(
            "assembly_unbuild",
            {
                "document_number": payload.get("document_number") or await self._next_number("AU"),
                "transaction_date": transaction_date,
                "status": "DRAFT",
                "assembly_item_id": assembly["id"],
                "original_assembly_build_id": payload.get("original_assembly_build_id"),
                "qty_unbuilt": quantity,
                "uom_id": uom_id,
                "location_id": _uuid(location_id),
                "reason_code_id": payload.get("reason_code_id"),
                "unbuild_variance_account_ref": payload.get("unbuild_variance_account_ref")
                or assembly.get("unbuild_variance_account_ref"),
                "bom_revision_id": revision["id"] if revision else None,
            },
        )
        components = await self.resolve_bom(
            assembly["id"],
            as_of=transaction_date,
            bom_revision_id=revision["id"] if revision else None,
            qty_ordered=quantity,
        )
        original_components: list[dict[str, Any]] = []
        if payload.get("original_assembly_build_id"):
            original_components = await self._all(
                "SELECT * FROM manufacturing.assembly_build_component WHERE tenant_id = :tenant_id AND assembly_build_id = :id ORDER BY line_number",
                id=_uuid(payload["original_assembly_build_id"]),
            )
        policy_default = str(await self._setting("unbuild.value_policy", "ORIGINAL_COST") or "ORIGINAL_COST")
        lines = payload.get("lines") or []
        line_payload_map: dict[str, Mapping[str, Any]] = {}
        for line in lines:
            key = str(line.get("component_item_id") or line.get("component_id") or "")
            if key:
                line_payload_map[key] = line
        for line_number, component in enumerate(components, start=1):
            key = str(component["component_item_id"])
            requested = line_payload_map.get(key, {})
            disposition = str(
                requested.get("disposition")
                or await self._setting("unbuild.default_disposition", "RETURN_TO_STOCK")
                or "RETURN_TO_STOCK"
            ).upper()
            is_used = bool(requested.get("is_used", False))
            condition_id = requested.get("condition_code_id")
            return_status = requested.get("return_status_id")
            if is_used and not return_status:
                used_status = await self._one(
                    "SELECT id FROM traceability.inventory_status WHERE tenant_id = :tenant_id AND code = :code LIMIT 1",
                    code=str(await self._setting("unbuild.default_used_status_code", "USED") or "USED"),
                )
                return_status = used_status["id"] if used_status else None
            value_policy = str(requested.get("value_policy") or policy_default).upper()
            unit_value = requested.get("unit_value")
            if value_policy == "MANUAL" and unit_value is None:
                raise UnbuildDispositionInvalid(
                    "MANUAL unbuild valuation requires unit_value", details={"component_item_id": key}
                )
            if disposition == "SCRAP" and (
                return_status or requested.get("target_location_id") or requested.get("target_bin_id")
            ):
                raise UnbuildDispositionInvalid(
                    "SCRAP lines cannot return stock to a status/location", details={"component_item_id": key}
                )
            original_cost = dec(requested.get("unit_cost") or 0)
            if original_cost == 0 and original_components:
                match = next((row for row in original_components if str(row["component_item_id"]) == key), None)
                original_cost = dec(match["unit_cost"]) if match else ZERO
            if value_policy == "ORIGINAL_COST":
                unit_cost_used = original_cost
            elif value_policy == "MANUAL":
                unit_cost_used = dec(unit_value)
            else:  # NRV: use configured source, falling back to original cost
                unit_cost_used = original_cost
            variance_value = (unit_cost_used - original_cost) * dec(component["qty_required"])
            serial_ids = requested.get("serial_ids") or component.get("serial_ids") or []
            if serial_ids and len(serial_ids) != int(dec(component["qty_required"])):
                raise UnbuildDispositionInvalid(
                    "serial identity count must equal returned quantity", details={"component_item_id": key}
                )
            await self._create(
                "assembly_unbuild_component",
                {
                    "assembly_unbuild_id": unbuild["id"],
                    "line_number": line_number,
                    "component_item_id": _uuid(key),
                    "qty_restored": dec(component["qty_required"]),
                    "uom_id": _uuid(component["uom_id"]),
                    "unit_cost": original_cost,
                    "lot_id": requested.get("lot_id"),
                    "serial_ids": serial_ids,
                    "variance_value": variance_value,
                    "location_id": _uuid(location_id),
                    "disposition": disposition,
                    "is_used": is_used,
                    "condition_code_id": _uuid(condition_id) if condition_id else None,
                    "return_status_id": _uuid(return_status) if return_status else None,
                    "target_location_id": _uuid(requested["target_location_id"])
                    if requested.get("target_location_id")
                    else None,
                    "target_bin_id": _uuid(requested["target_bin_id"]) if requested.get("target_bin_id") else None,
                    "allow_resale": bool(requested.get("allow_resale", False)),
                    "value_policy": value_policy,
                    "unit_value": dec(unit_value) if unit_value is not None else None,
                    "reason_code_id": requested.get("reason_code_id") or payload.get("reason_code_id"),
                },
            )
        return await self.get_unbuild(unbuild["id"])

    async def get_unbuild(self, unbuild_id: Any) -> dict[str, Any]:
        unbuild = await self._fetch("assembly_unbuild", unbuild_id)
        if unbuild is None:
            raise NotFoundError("assembly unbuild not found")
        lines = await self._all(
            "SELECT * FROM manufacturing.assembly_unbuild_component WHERE tenant_id = :tenant_id AND assembly_unbuild_id = :id ORDER BY line_number",
            id=unbuild["id"],
        )
        result = serialize(unbuild) or {}
        result["lines"] = [serialize(line) for line in lines]
        return result

    async def list_unbuilds(self, limit: int = 100) -> list[dict[str, Any]]:
        return [serialize(row) for row in await self._list("assembly_unbuild", limit=limit)]

    async def post_unbuild(self, unbuild_id: Any, payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
        payload = dict(payload or {})
        unbuild = await self.get_unbuild(unbuild_id)
        require_transition("assembly_unbuild", unbuild["status"], "POSTED")
        movements: list[dict[str, Any]] = []
        scrap_value = ZERO
        for line_number, line in enumerate(unbuild["lines"], start=1):
            disposition = str(line["disposition"])
            if disposition == "SCRAP":
                scrap_value += dec(line["unit_cost"]) * dec(line["qty_restored"]) + dec(line["variance_value"])
                continue
            status_id = line.get("return_status_id") or str(await self._default_status())
            target_location = line.get("target_location_id") or line["location_id"]
            movements.append(
                {
                    "item_id": line["component_item_id"],
                    "location_id": target_location,
                    "inventory_status_id": status_id,
                    "qty": str(dec(line["qty_restored"])),
                    "movement_type": "ASSEMBLY_UNBUILD_RETURN",
                    "source_line_ref": str(line_number),
                    "unit_cost": line["unit_cost"],
                    "details": [
                        {
                            "lot_id": line.get("lot_id"),
                            "serial_id": serial_id,
                            "inventory_status_id": status_id,
                            "ownership": "OWNED",
                            "qty": "1",
                        }
                        for serial_id in (line.get("serial_ids") or [])
                    ],
                }
            )
        unbuild_item_id = await self._assembly_item_id(unbuild["assembly_item_id"])
        movements.append(
            {
                "item_id": str(unbuild_item_id),
                "location_id": unbuild["location_id"],
                "inventory_status_id": str(await self._default_status()),
                "qty": str(-dec(unbuild["qty_unbuilt"])),
                "movement_type": "ASSEMBLY_UNBUILD",
                "source_line_ref": "output",
            }
        )
        result = await self.posting.post_assembly_unbuild(
            transaction_date=unbuild["transaction_date"],
            source_doc_type="ASSEMBLY_UNBUILD",
            source_doc_id=str(unbuild["id"]),
            idempotency_key=payload.get("idempotency_key") or f"unbuild-post:{unbuild['id']}",
            movements=movements,
        )
        await self._update(
            "assembly_unbuild",
            unbuild_id,
            {"status": "POSTED", "posted_transaction_id": _uuid(result["transaction_id"], field="transaction_id")},
        )
        return {**unbuild, "status": "POSTED", "scrap_value": str(scrap_value), "posting": result}

    async def reverse_unbuild(self, unbuild_id: Any, payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
        payload = dict(payload or {})
        unbuild = await self._fetch("assembly_unbuild", unbuild_id)
        if unbuild is None:
            raise NotFoundError("assembly unbuild not found")
        require_transition("assembly_unbuild", unbuild["status"], "REVERSED")
        transaction_id = unbuild.get("posted_transaction_id")
        if transaction_id is None:
            posted = await self._one(
                "SELECT posted_transaction_id AS transaction_id FROM inventory_ledger.inventory_transaction "
                "WHERE tenant_id = :tenant_id AND source_doc_type = 'ASSEMBLY_UNBUILD' AND source_doc_id = :id LIMIT 1",
                id=unbuild["id"],
            )
            transaction_id = (posted or {}).get("transaction_id")
        if transaction_id is None:
            raise UnbuildNotReversible("assembly unbuild was not posted")
        await self.posting.reverse(
            str(transaction_id),
            reason=payload.get("reason") or "unbuild reversal",
            idempotency_key=f"unbuild-reverse:{unbuild_id}",
        )
        await self._update("assembly_unbuild", unbuild_id, {"status": "REVERSED"})
        return serialize(await self._fetch("assembly_unbuild", unbuild_id)) or {}

    # ==================================================================== outsourced ==========
    async def create_outsourced_operation(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        await self._require_enabled()
        assembly = await self._assembly(payload.get("assembly_item_id"))
        if not assembly.get("is_outsourced"):
            raise PolicyViolation("assembly item is not configured as outsourced")
        values = dict(payload)
        values["assembly_item_id"] = assembly["id"]
        values["vendor_id"] = _uuid(payload.get("vendor_id"), field="vendor_id")
        values["component_location_id"] = _uuid(payload.get("component_location_id") or await self._default_location())
        return serialize(await self._create("outsourced_operation", values)) or {}

    async def list_outsourced_operations(self, *, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        return [serialize(row) for row in await self._list("outsourced_operation", status=status, limit=limit)]

    async def create_outsourced_po(self, operation_id: Any, payload: Mapping[str, Any]) -> dict[str, Any]:
        operation = await self._fetch("outsourced_operation", operation_id)
        if operation is None:
            raise NotFoundError("outsourced operation not found")
        require_transition("outsourced_operation", operation["status"], "ORDERED") if operation[
            "status"
        ] != "ORDERED" else None
        po = await self.procurement.create_draft_po(
            lines=[
                {
                    "item_id": payload.get("charge_item_id") or operation["assembly_item_id"],
                    "description": f"Outsourced operation {operation['operation_code']}",
                    "qty_ordered": str(dec(operation["qty_ordered"])),
                    "unit_price": str(dec(payload.get("unit_price") or 0)),
                    "uom_id": payload.get("uom_id"),
                }
            ],
            vendor=operation["vendor_id"],
            opts={"po_type": "STANDARD", "memo": payload.get("memo") or f"outsourced:{operation['id']}"},
        )
        await self._update(
            "outsourced_operation",
            operation_id,
            {"status": "ORDERED", "purchase_order_id": _uuid(po["purchase_order_id"])},
        )
        return {"outsourced_operation_id": str(operation["id"]), "purchase_order": po}

    async def receive_outsourced_operation(self, operation_id: Any, payload: Mapping[str, Any]) -> dict[str, Any]:
        operation = await self._fetch("outsourced_operation", operation_id)
        if operation is None:
            raise NotFoundError("outsourced operation not found")
        quantity = qty(payload.get("qty_received"))
        remaining = dec(operation["qty_ordered"]) - dec(operation["qty_received"])
        if quantity <= 0 or quantity > remaining:
            raise ValidationFailed("invalid received quantity", details={"remaining": str(remaining)})
        require_transition("outsourced_operation", operation["status"], "RECEIVED")
        build = await self.create_build(
            {
                "assembly_item_id": operation["assembly_item_id"],
                "qty_built": quantity,
                "location_id": payload.get("location_id") or operation["component_location_id"],
                "compatibility_override": payload.get("compat_override"),
                "compat_context_refs": payload.get("compat_context_refs"),
            }
        )
        posted = await self.post_build(build["id"], payload)
        for charge in payload.get("charges") or []:
            await self._create(
                "outsourced_charge",
                {
                    "outsourced_operation_id": operation["id"],
                    "charge_item_id": charge.get("charge_item_id"),
                    "cost_category_id": _uuid(charge.get("cost_category_id"), field="cost_category_id"),
                    "amount": dec(charge.get("amount") or 0),
                    "currency_code": charge.get("currency_code") or "USD",
                    "status": "ACTUAL",
                },
            )
        await self._update(
            "outsourced_operation",
            operation_id,
            {"qty_received": dec(operation["qty_received"]) + quantity, "status": "RECEIVED"},
        )
        return {"outsourced_operation_id": str(operation["id"]), "build": posted}

    async def consolidate_outsourced_operations(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        vendor_id = _uuid(payload.get("vendor_id"), field="vendor_id")
        operations = await self._all(
            "SELECT * FROM manufacturing.outsourced_operation WHERE tenant_id = :tenant_id AND vendor_id = :vendor_id "
            "AND status IN ('PLANNED','ORDERED') AND purchase_order_id IS NULL ORDER BY created_at",
            vendor_id=vendor_id,
        )
        if not operations:
            raise ValidationFailed("no eligible outsourced operations to consolidate")
        lines = [
            {
                "item_id": payload.get("charge_item_id") or operation["assembly_item_id"],
                "description": f"Consolidated outsourced {operation['operation_code']}",
                "qty_ordered": str(dec(operation["qty_ordered"])),
                "unit_price": str(dec(payload.get("unit_price") or 0)),
            }
            for operation in operations
        ]
        po = await self.procurement.create_draft_po(
            lines=lines, vendor=vendor_id, opts={"memo": payload.get("memo") or "outsourced consolidation"}
        )
        for operation in operations:
            await self._update(
                "outsourced_operation",
                operation["id"],
                {"status": "ORDERED", "purchase_order_id": _uuid(po["purchase_order_id"])},
            )
        return {"purchase_order": po, "consolidated_operations": len(operations)}

    async def close_outsourced_operation(self, operation_id: Any) -> dict[str, Any]:
        operation = await self._fetch("outsourced_operation", operation_id)
        if operation is None:
            raise NotFoundError("outsourced operation not found")
        require_transition("outsourced_operation", operation["status"], "CLOSED")
        pending = await self._one(
            "SELECT id FROM manufacturing.outsourced_charge WHERE tenant_id = :tenant_id AND outsourced_operation_id = :id AND status = 'ESTIMATED' LIMIT 1",
            id=operation["id"],
        )
        if pending is not None:
            raise InvalidStateTransition("all outsourced charges must be ACTUAL before closing")
        return serialize(await self._update("outsourced_operation", operation_id, {"status": "CLOSED"})) or {}

    # ==================================================================== ECO ================
    async def create_eco(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        eco = await self._create(
            "eco",
            {
                "eco_number": payload.get("eco_number") or await self._next_number("ECN"),
                "status": "DRAFT",
                "effective_date": payload.get("effective_date"),
                "reason": payload.get("reason") or "",
                "eco_type_id": payload.get("eco_type_id"),
                "memo": payload.get("memo"),
            },
        )
        for line_number, line in enumerate(payload.get("lines") or [], start=1):
            await self._create(
                "eco_line",
                {
                    "eco_id": eco["id"],
                    "line_number": line_number,
                    "item_id": _uuid(line.get("item_id"), field="item_id"),
                    "bom_id": _uuid(line["bom_id"]) if line.get("bom_id") else None,
                    "bom_component_id": _uuid(line["bom_component_id"]) if line.get("bom_component_id") else None,
                    "change_type": line.get("change_type"),
                    "old_value": line.get("old_value") or {},
                    "new_value": line.get("new_value") or {},
                    "status": "PROPOSED",
                },
            )
        return await self.get_eco(eco["id"])

    async def get_eco(self, eco_id: Any) -> dict[str, Any]:
        eco = await self._fetch("eco", eco_id)
        if eco is None:
            raise NotFoundError("ECO not found")
        lines = await self._all(
            "SELECT * FROM manufacturing.eco_line WHERE tenant_id = :tenant_id AND eco_id = :id ORDER BY line_number",
            id=eco["id"],
        )
        result = serialize(eco) or {}
        result["lines"] = [serialize(line) for line in lines]
        return result

    async def list_ecos(self, *, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        return [serialize(row) for row in await self._list("eco", status=status, limit=limit)]

    async def submit_eco(self, eco_id: Any) -> dict[str, Any]:
        eco = await self._fetch("eco", eco_id)
        if eco is None:
            raise NotFoundError("ECO not found")
        require_transition("eco", eco["status"], "PENDING_APPROVAL")
        return serialize(await self._update("eco", eco_id, {"status": "PENDING_APPROVAL"})) or {}

    async def approve_eco(self, eco_id: Any) -> dict[str, Any]:
        eco = await self._fetch("eco", eco_id)
        if eco is None:
            raise NotFoundError("ECO not found")
        require_transition("eco", eco["status"], "APPROVED")
        return serialize(await self._update("eco", eco_id, {"status": "APPROVED", "approved_by": self.actor_id})) or {}

    async def implement_eco(self, eco_id: Any) -> dict[str, Any]:
        eco = await self.get_eco(eco_id)
        require_transition("eco", eco["status"], "IMPLEMENTED")
        applied = 0
        for line in eco["lines"]:
            if line["status"] != "PROPOSED":
                continue
            change_type = line["change_type"]
            if change_type == "ADD_COMPONENT" and line.get("bom_component_id"):
                await self._update(
                    "bom_component",
                    line["bom_component_id"],
                    line.get("new_value") or {},
                    allowed={"qty_per", "component_yield_percent", "round_up_component_qty"},
                )
            elif change_type == "CHANGE_QTY" and line.get("bom_component_id"):
                await self._update(
                    "bom_component", line["bom_component_id"], {"qty_per": (line.get("new_value") or {}).get("qty_per")}
                )
            elif change_type == "CHANGE_EFFECTIVITY" and line.get("bom_component_id"):
                await self._upsert_component_control(line["bom_component_id"], line.get("new_value") or {})
            elif change_type == "CHANGE_REVISION":
                continue
            await self._exec(
                "UPDATE manufacturing.eco_line SET status = 'APPLIED', updated_at = now(), version = version + 1 "
                "WHERE tenant_id = :tenant_id AND id = :id",
                id=_uuid(line["id"]),
            )
            applied += 1
        row = await self._update("eco", eco_id, {"status": "IMPLEMENTED", "implemented_at": utc_now()})
        return {**(serialize(row) or {}), "lines_applied": applied}

    # ==================================================================== scheduling =========
    async def create_scheduling_run(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        method = str(
            payload.get("method") or await self._setting("scheduling.default_method", "BACKWARD") or "BACKWARD"
        ).upper()
        capacity_mode = str(payload.get("capacity_mode") or "INFINITE").upper()
        if method not in {"FORWARD", "BACKWARD"}:
            raise ValidationFailed("scheduling method must be FORWARD or BACKWARD")
        if capacity_mode not in {"INFINITE", "FINITE"}:
            raise ValidationFailed("capacity_mode must be INFINITE or FINITE")
        horizon_start = to_date(payload.get("horizon_start")) or date.today()
        horizon_end = to_date(payload.get("horizon_end")) or (horizon_start + timedelta(days=90))
        run = await self._create(
            "scheduling_run",
            {
                "run_number": await self._next_number("MFGSCH"),
                "method": method,
                "capacity_mode": capacity_mode,
                "horizon_start": horizon_start,
                "horizon_end": horizon_end,
                "status": "QUEUED",
                "work_center_scope": {
                    "scope": payload.get("scope") or {},
                    "policy_version": await self._setting("scheduling.policy_version", "mfg-sched-v1")
                    or "mfg-sched-v1",
                },
            },
        )
        await self.execute_scheduling_run(run["id"])
        return await self.get_scheduling_run(run["id"])

    async def get_scheduling_run(self, run_id: Any) -> dict[str, Any]:
        run = await self._fetch("scheduling_run", run_id)
        if run is None:
            raise NotFoundError("scheduling run not found")
        counted = await self._one(
            "SELECT COUNT(*) AS operations, COUNT(violation_reason) AS violations FROM manufacturing.scheduled_operation "
            "WHERE tenant_id = :tenant_id AND scheduling_run_id = :id",
            id=run["id"],
        )
        result = serialize(run) or {}
        result["operation_count"] = int((counted or {}).get("operations") or 0)
        result["violation_count"] = int((counted or {}).get("violations") or 0)
        return result

    async def list_scheduled_operations(self, run_id: Any, *, limit: int = 500) -> list[dict[str, Any]]:
        return [
            serialize(row)
            for row in await self._all(
                "SELECT * FROM manufacturing.scheduled_operation WHERE tenant_id = :tenant_id AND scheduling_run_id = :id "
                "ORDER BY planned_start, sequence LIMIT :limit",
                id=_uuid(run_id),
                limit=max(1, min(limit, 1000)),
            )
        ]

    async def execute_scheduling_run(self, run_id: Any) -> dict[str, Any]:
        run = await self._fetch("scheduling_run", run_id)
        if run is None:
            raise NotFoundError("scheduling run not found")
        require_transition("scheduling_run", run["status"], "RUNNING")
        await self._update("scheduling_run", run_id, {"status": "RUNNING", "started_at": utc_now()})
        operations = await self._all(
            """
            SELECT woo.*, wo.end_date AS wo_due_date, wo.start_date AS wo_start_date, wo.document_number,
                   wo.qty_ordered, wc.capacity_hours_per_day, wc.efficiency_percent, wcc.id AS calendar_id
              FROM manufacturing.work_order_operation woo
              JOIN manufacturing.work_order wo ON wo.tenant_id = woo.tenant_id AND wo.id = woo.work_order_id
              JOIN manufacturing.work_center wc ON wc.tenant_id = woo.tenant_id AND wc.id = woo.work_center_id
              LEFT JOIN manufacturing.work_center_calendar wcc ON wcc.tenant_id = wc.tenant_id AND wcc.id = wc.calendar_id
             WHERE woo.tenant_id = :tenant_id AND woo.status IN ('PENDING','IN_PROGRESS')
               AND wo.status IN ('RELEASED','IN_PROCESS')
             ORDER BY wo.end_date, woo.sequence
            """
        )
        capacity_used: dict[tuple[str, date], Decimal] = {}
        created = 0
        for operation in operations:
            routing_step = None
            if operation.get("routing_step_id"):
                routing_step = await self._fetch("routing_step", operation["routing_step_id"])
            setup = dec((routing_step or {}).get("setup_time_minutes") or 0) / Decimal("60")
            run_per_unit = dec((routing_step or {}).get("run_time_per_unit_minutes") or 0) / Decimal("60")
            efficiency = dec(operation.get("efficiency_percent") or 100) / Decimal("100")
            duration_hours = (setup + run_per_unit * dec(operation.get("qty_ordered") or 1)) / (
                efficiency or Decimal("1")
            )
            if duration_hours <= 0:
                duration_hours = Decimal("0.5")
            if str(run["method"]) == "BACKWARD":
                end = datetime.combine(operation["wo_due_date"], datetime.min.time(), tzinfo=UTC) + timedelta(hours=17)
            else:
                end = datetime.combine(operation["wo_start_date"], datetime.min.time(), tzinfo=UTC) + timedelta(hours=8)
            violation = None
            if str(run["capacity_mode"]) == "FINITE":
                capacity = dec(operation.get("capacity_hours_per_day") or 8)
                cursor = end - timedelta(hours=float(duration_hours))
                day = cursor.date()
                used = capacity_used.get((operation["work_center_id"], day), ZERO)
                if used + duration_hours > capacity:
                    violation = "INSUFFICIENT_CAPACITY"
                capacity_used[(operation["work_center_id"], day)] = used + duration_hours
            start = end - timedelta(hours=float(duration_hours))
            await self._create(
                "scheduled_operation",
                {
                    "scheduling_run_id": run["id"],
                    "work_order_operation_id": operation["id"],
                    "work_center_id": operation["work_center_id"],
                    "planned_start": start,
                    "planned_end": end,
                    "sequence": operation["sequence"],
                    "is_compressed": False,
                    "violation_reason": violation,
                },
            )
            created += 1
        await self._update("scheduling_run", run_id, {"status": "SUCCEEDED", "finished_at": utc_now()})
        return {"scheduling_run_id": str(run["id"]), "scheduled_operations": created}

    async def apply_scheduling_run(self, run_id: Any, payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
        payload = dict(payload or {})
        run = await self._fetch("scheduling_run", run_id)
        if run is None:
            raise NotFoundError("scheduling run not found")
        if run["status"] != "SUCCEEDED":
            raise InvalidStateTransition("only successful scheduling runs can be applied")
        rows = await self._all(
            "SELECT so.*, woo.work_order_id FROM manufacturing.scheduled_operation so "
            "JOIN manufacturing.work_order_operation woo ON woo.tenant_id = so.tenant_id AND woo.id = so.work_order_operation_id "
            "WHERE so.tenant_id = :tenant_id AND so.scheduling_run_id = :id",
            id=run["id"],
        )
        changed = 0
        if not payload.get("dry_run"):
            for row in rows:
                await self._exec(
                    "UPDATE manufacturing.work_order_operation SET planned_start = :start, planned_end = :end, updated_at = now(), version = version + 1 "
                    "WHERE tenant_id = :tenant_id AND id = :id",
                    start=row["planned_start"],
                    end=row["planned_end"],
                    id=row["work_order_operation_id"],
                )
                changed += 1
        return {
            "scheduling_run_id": str(run["id"]),
            "operations_applied": changed,
            "dry_run": bool(payload.get("dry_run")),
        }

    async def list_capacity_plans(self, *, work_center_id: Any | None = None, limit: int = 500) -> list[dict[str, Any]]:
        query = "SELECT * FROM manufacturing.capacity_plan WHERE tenant_id = :tenant_id"
        params: dict[str, Any] = {"limit": max(1, min(limit, 1000))}
        if work_center_id:
            query += " AND work_center_id = :work_center_id"
            params["work_center_id"] = _uuid(work_center_id)
        query += " ORDER BY bucket_date LIMIT :limit"
        return [serialize(row) for row in await self._all(query, **params)]

    async def rollup_capacity(self, payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
        payload = dict(payload or {})
        days = int(payload.get("days") or 30)
        start = payload.get("start_date") or date.today()
        work_centers = await self._all(
            "SELECT * FROM manufacturing.work_center WHERE tenant_id = :tenant_id AND is_active"
        )
        buckets = 0
        for work_center in work_centers:
            for offset in range(days):
                bucket = start + timedelta(days=offset)
                load_row = await self._one(
                    "SELECT COALESCE(SUM(load_hours), 0) AS load FROM manufacturing.work_center_load WHERE tenant_id = :tenant_id "
                    "AND work_center_id = :wc AND load_date = :bucket",
                    wc=work_center["id"],
                    bucket=bucket,
                )
                load_hours = dec((load_row or {}).get("load") or 0)
                available = dec(work_center["capacity_hours_per_day"]) * (dec(work_center["efficiency_percent"]) / 100)
                status = "OK"
                if load_hours > available:
                    status = "OVERLOADED"
                elif available > 0 and load_hours < available / 2:
                    status = "UNDERLOADED"
                existing = await self._one(
                    "SELECT id FROM manufacturing.capacity_plan WHERE tenant_id = :tenant_id AND work_center_id = :wc AND bucket_date = :bucket",
                    wc=work_center["id"],
                    bucket=bucket,
                )
                if existing:
                    await self._update(
                        "capacity_plan",
                        existing["id"],
                        {"available_hours": available, "load_hours": load_hours, "status": status},
                    )
                else:
                    await self._create(
                        "capacity_plan",
                        {
                            "work_center_id": work_center["id"],
                            "bucket_date": bucket,
                            "available_hours": available,
                            "load_hours": load_hours,
                            "status": status,
                        },
                    )
                buckets += 1
        return {"buckets": buckets, "work_centers": len(work_centers)}

    # ==================================================================== available-to-build =
    async def available_to_build(
        self, item_id: Any, *, location_id: Any | None = None, as_of: date | None = None, level_mode: str | None = None
    ) -> dict[str, Any]:
        assembly = await self._fetch("assembly_item", item_id)
        if assembly is None:
            raise NotFoundError("assembly item not found")
        mode = str(
            level_mode or await self._setting("available_to_build.level_mode", "ALL_LEVELS") or "ALL_LEVELS"
        ).upper()
        basis = str(await self._setting("available_to_build.basis", "ON_HAND") or "ON_HAND").upper()
        moment = as_of or date.today()
        location = location_id or await self._default_location()
        limiting: dict[str, Any] | None = None
        buildable: Decimal | None = None
        on_hand = await self._on_hand(assembly["item_id"], location, basis)
        buildable = on_hand
        components = await self.resolve_bom(assembly["id"], as_of=moment, qty_ordered=1)
        for component in components:
            required_per_unit = dec(component["qty_required"])
            if required_per_unit <= 0:
                continue
            available = await self._on_hand(_uuid(component["component_item_id"]), location, basis)
            units_from_component = (available / required_per_unit).to_integral_value(rounding="ROUND_FLOOR")
            if buildable is None or units_from_component < buildable:
                buildable = units_from_component
                limiting = {
                    "component_item_id": component["component_item_id"],
                    "item_number": component.get("item_number"),
                    "available": str(available),
                    "qty_per": str(required_per_unit),
                }
        buildable = max(buildable or ZERO, ZERO)
        return {
            "assembly_item_id": str(assembly["id"]),
            "item_id": str(assembly["item_id"]),
            "location_id": str(location),
            "as_of": str(moment),
            "level_mode": mode,
            "basis": basis,
            "on_hand": str(on_hand),
            "buildable_qty": str(buildable.to_integral_value(rounding="ROUND_FLOOR")),
            "limiting_component": limiting,
            "policy_version": await self._setting("available_to_build.policy_version", "atb-v1") or "atb-v1",
            "components": components,
        }

    async def _on_hand(self, item_id: uuid.UUID, location_id: Any, basis: str) -> Decimal:
        try:
            availability = await self.balances.availability(item_id=str(item_id), location_id=str(location_id))
            value = availability.get("available") if basis == "AVAILABLE" else availability.get("on_hand")
            return dec(value or 0)
        except Exception:
            row = await self._one(
                "SELECT COALESCE(SUM(qty_on_hand), 0) AS on_hand, COALESCE(SUM(qty_available), 0) AS available "
                "FROM inventory_ledger.inventory_balance WHERE tenant_id = :tenant_id AND item_id = :item_id AND location_id = :location_id",
                item_id=item_id,
                location_id=_uuid(location_id),
            )
            key = "available" if basis == "AVAILABLE" else "on_hand"
            return dec((row or {}).get(key) or 0)

    # ==================================================================== compatibility =======
    async def _compat_gate(
        self,
        assembly_item_id: Any,
        *,
        context: str,
        components: Sequence[Mapping[str, Any]],
        override: bool,
        context_refs: Mapping[str, Any],
        override_reason: str | None = None,
        persist: bool = False,
    ) -> dict[str, Any] | None:
        assembly = await self._fetch("assembly_item", assembly_item_id)
        if assembly is None:
            return None
        rule_sets = await self._load_rule_sets(assembly, context=context, context_refs=context_refs)
        if not rule_sets and not persist:
            return None
        rule_set_views = []
        versions: dict[str, Any] = {}
        for rule_set in rule_sets:
            rules = await self._all(
                "SELECT * FROM manufacturing.compat_rule WHERE tenant_id = :tenant_id AND rule_set_id = :id AND is_active ORDER BY priority, code",
                id=rule_set["id"],
            )
            view_rules = tuple(
                compat_engine.RuleView(
                    code=rule["code"],
                    rule_type=rule["rule_type"],
                    subject_type=rule["subject_type"],
                    subject_expr=rule.get("subject_expr") or {},
                    constraint_expr=rule.get("constraint_expr") or {},
                    severity=rule["severity"],
                    message_code=rule["message_code"],
                    priority=int(rule["priority"]),
                    effective_from=str(rule["effective_from"]) if rule.get("effective_from") else None,
                    effective_to=str(rule["effective_to"]) if rule.get("effective_to") else None,
                    is_active=bool(rule["is_active"]),
                )
                for rule in rules
            )
            rule_set_views.append(
                compat_engine.RuleSetView(
                    code=rule_set["code"],
                    scope_type=rule_set["scope_type"],
                    priority=int(rule_set["priority"]),
                    enforcement_mode=rule_set["enforcement_mode"],
                    rules=view_rules,
                    scope_ref_key=str(rule_set.get("scope_ref_id") or rule_set.get("scope_ref_text") or ""),
                    status=rule_set["status"],
                )
            )
            versions[rule_set["code"]] = f"{rule_set['status']}:{rule_set['version']}"
        slots = []
        if rule_sets:
            slots = [
                dict(row)
                for row in await self._all(
                    "SELECT * FROM manufacturing.compat_slot_def WHERE tenant_id = :tenant_id AND is_active AND (assembly_item_id = :assembly_id OR bom_revision_id IN (SELECT br.id FROM manufacturing.bom_revision br JOIN manufacturing.bom b ON b.tenant_id = br.tenant_id AND b.id = br.bom_id WHERE b.tenant_id = :tenant_id AND b.assembly_item_id = :assembly_id))",
                    assembly_id=assembly["id"],
                )
            ]
        enriched_components = []
        for component in components:
            attributes = await self._attributes_for_item(_uuid(component["item_id"]))
            enriched_components.append({**dict(component), "attributes": attributes})
        effective_override = bool(override)
        if effective_override and not override_reason:
            raise CompatibilityViolation("compatibility override requires a mandatory reason")
        result = compat_engine.evaluate(
            rule_set_views,
            components=enriched_components,
            slots=slots,
            as_of=str(date.today()),
            tenant_default=str(await self._setting("compat.enforcement_default", "HARD") or "HARD"),
            override=effective_override,
        )
        record_id = None
        if rule_sets or persist:
            configuration_hash = compat_engine.configuration_hash(
                assembly_item_id=assembly["id"],
                components=enriched_components,
                attributes={},
                rule_set_versions=versions,
            )
            record = await self._create(
                "compat_configuration",
                {
                    "configuration_hash": configuration_hash,
                    "assembly_item_id": assembly["id"],
                    "context": context,
                    "context_ref_type": "REFERENCE" if context_refs else None,
                    "context_ref_id": next(iter([str(value) for value in context_refs.values() if value]), None),
                    "components_snapshot": enriched_components,
                    "attributes_snapshot": {
                        component["item_id"]: component.get("attributes") or {} for component in enriched_components
                    },
                    "matched_rule_sets": list(result.matched_rule_sets),
                    "matched_rules": list(result.matched_rules),
                    "rule_set_versions": versions,
                    "result": result.result,
                    "violation_count": result.violation_count,
                    "override_by": self.actor_id if effective_override else None,
                    "override_reason": override_reason if effective_override else None,
                    "overridden_at": utc_now() if effective_override else None,
                    "validated_at": utc_now(),
                    "requested_by": self.actor_id,
                },
            )
            record_id = str(record["id"])
        payload = result.as_payload()
        payload["configuration_id"] = record_id
        payload["configuration_hash"] = configuration_hash if "configuration_hash" in locals() else None
        if result.blocked:
            raise CompatibilityViolation(
                "configuration violates a HARD compatibility rule set",
                details={"violations": payload["violations"], "configuration_id": record_id},
            )
        return payload

    async def _load_rule_sets(
        self, assembly: Mapping[str, Any], *, context: str, context_refs: Mapping[str, Any]
    ) -> list[dict[str, Any]]:
        revision = await self._resolve_revision(assembly["id"], as_of=date.today(), revision_id=None)
        rows = await self._all(
            "SELECT * FROM manufacturing.compat_rule_set WHERE tenant_id = :tenant_id AND status = 'ACTIVE' "
            "AND effective_from <= :today AND (effective_to IS NULL OR effective_to >= :today)",
            today=date.today(),
        )
        matched: list[dict[str, Any]] = []
        refs = {str(value) for value in context_refs.values() if value}
        for row in rows:
            scope = str(row["scope_type"]).upper()
            ref = str(row.get("scope_ref_id") or row.get("scope_ref_text") or "")
            if scope == "ASSEMBLY":
                if ref and ref not in {
                    str(assembly["id"]),
                    str(assembly["item_id"]),
                    str(assembly.get("item_number") or ""),
                }:
                    continue
            elif scope == "BOM_REVISION":
                if revision is None or (ref and ref != str(revision["id"])):
                    continue
            elif scope in CONTEXT_SCOPES:
                if ref and ref not in refs:
                    continue
                if not ref and not refs and not context_refs:
                    continue
            else:
                continue
            matched.append(row)
        return matched

    async def _attributes_for_item(self, item_id: uuid.UUID) -> dict[str, Any]:
        rows = await self._all(
            "SELECT d.code AS attribute_code, d.data_type, v.value_text, v.value_number, v.value_bool, v.value_json "
            "FROM manufacturing.compat_attribute_value v "
            "JOIN manufacturing.compat_attribute_def d ON d.tenant_id = v.tenant_id AND d.id = v.attribute_def_id "
            "WHERE v.tenant_id = :tenant_id AND v.item_id = :item_id",
            item_id=item_id,
        )
        result: dict[str, Any] = {}
        for row in rows:
            data_type = str(row["data_type"])
            if data_type == "TEXT":
                result[row["attribute_code"]] = row["value_text"]
            elif data_type == "NUMERIC":
                result[row["attribute_code"]] = str(row["value_number"]) if row["value_number"] is not None else None
            elif data_type == "BOOL":
                result[row["attribute_code"]] = row["value_bool"]
            else:
                result[row["attribute_code"]] = row["value_json"]
        return result

    async def validate_configuration(self, payload: Mapping[str, Any], *, simulate: bool = False) -> dict[str, Any]:
        assembly = await self._assembly(payload.get("assembly_item_id"))
        components = payload.get("components")
        if components is None:
            components = [
                {"item_id": component["component_item_id"], "qty": component["qty_required"]}
                for component in await self.resolve_bom(
                    assembly["id"], as_of=date.today(), qty_ordered=payload.get("qty") or 1
                )
            ]
        override = bool(payload.get("override"))
        result = await self._compat_gate(
            assembly["id"],
            context=str(payload.get("context") or "ATB").upper(),
            components=components,
            override=override,
            context_refs=payload.get("context_refs") or {},
            override_reason=payload.get("override_reason"),
            persist=True,
        )
        return {"simulate": simulate, **(result or {"result": "VALID", "violations": []})}

    async def get_compat_configuration(self, configuration_id: Any) -> dict[str, Any]:
        row = await self._fetch("compat_configuration", configuration_id)
        if row is None:
            raise NotFoundError("compatibility configuration not found")
        return serialize(row) or {}

    async def list_compat_configurations(
        self, *, assembly_item_id: Any | None = None, result: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM manufacturing.compat_configuration WHERE tenant_id = :tenant_id"
        params: dict[str, Any] = {"limit": max(1, min(limit, 500))}
        if assembly_item_id:
            query += " AND assembly_item_id = :assembly_item_id"
            params["assembly_item_id"] = _uuid(assembly_item_id)
        if result:
            query += " AND result = :result"
            params["result"] = result.upper()
        query += " ORDER BY validated_at DESC LIMIT :limit"
        return [serialize(row) for row in await self._all(query, **params)]

    async def compat_report(self, *, assembly_item_id: Any | None = None, limit: int = 200) -> dict[str, Any]:
        rows = await self.list_compat_configurations(assembly_item_id=assembly_item_id, limit=limit)
        violations = [row for row in rows if row["violation_count"]]
        return {"total": len(rows), "with_violations": len(violations), "records": rows}

    # compatibility master data CRUD -----------------------------------------------------------
    async def create_attribute_def(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return serialize(await self._create("compat_attribute_def", payload)) or {}

    async def list_attribute_defs(self) -> list[dict[str, Any]]:
        return [serialize(row) for row in await self._list("compat_attribute_def")]

    async def update_attribute_def(self, attribute_id: Any, payload: Mapping[str, Any]) -> dict[str, Any]:
        allowed = {"name", "unit", "choices", "applicable_item_types", "is_active", "description"}
        return serialize(await self._update("compat_attribute_def", attribute_id, payload, allowed=allowed)) or {}

    async def set_item_attributes(self, item_id: Any, values: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        item = await self._item(item_id)
        stored: list[dict[str, Any]] = []
        for entry in values:
            definition = await self._one(
                "SELECT * FROM manufacturing.compat_attribute_def WHERE tenant_id = :tenant_id AND code = :code",
                code=entry.get("attribute_code") or entry.get("code"),
            )
            if definition is None:
                raise NotFoundError("compatibility attribute definition not found")
            data_type = str(definition["data_type"])
            column = {"TEXT": "value_text", "NUMERIC": "value_number", "BOOL": "value_bool"}.get(
                data_type, "value_json"
            )
            raw = entry.get("value")
            if data_type == "NUMERIC" and raw is not None:
                raw = dec(raw)
            row = await self._one(
                "SELECT id FROM manufacturing.compat_attribute_value WHERE tenant_id = :tenant_id AND item_id = :item_id AND attribute_def_id = :attribute_id",
                item_id=item["id"],
                attribute_id=definition["id"],
            )
            values_payload: dict[str, Any] = {
                "value_text": None,
                "value_number": None,
                "value_bool": None,
                "value_json": None,
            }
            values_payload[column] = raw
            values_payload.update(
                attribute_def_id=definition["id"], item_id=item["id"], provenance="MANUAL", updated_by=self.actor_id
            )
            if row is not None:
                stored.append(serialize(await self._update("compat_attribute_value", row["id"], values_payload)) or {})
            else:
                stored.append(serialize(await self._create("compat_attribute_value", values_payload)) or {})
        return stored

    async def get_item_attributes(self, item_id: Any) -> dict[str, Any]:
        item = await self._item(item_id)
        return {"item_id": str(item["id"]), "attributes": await self._attributes_for_item(item["id"])}

    async def create_rule_set(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return serialize(await self._create("compat_rule_set", payload)) or {}

    async def list_rule_sets(self, *, status: str | None = None) -> list[dict[str, Any]]:
        return [serialize(row) for row in await self._list("compat_rule_set", status=status)]

    async def get_rule_set(self, rule_set_id: Any) -> dict[str, Any]:
        rule_set = await self._fetch("compat_rule_set", rule_set_id)
        if rule_set is None:
            raise NotFoundError("compatibility rule set not found")
        rules = await self._all(
            "SELECT * FROM manufacturing.compat_rule WHERE tenant_id = :tenant_id AND rule_set_id = :id ORDER BY priority, code",
            id=rule_set["id"],
        )
        result = serialize(rule_set) or {}
        result["rules"] = [serialize(rule) for rule in rules]
        return result

    async def update_rule_set(self, rule_set_id: Any, payload: Mapping[str, Any]) -> dict[str, Any]:
        allowed = {
            "name",
            "priority",
            "enforcement_mode",
            "effective_from",
            "effective_to",
            "description",
            "scope_ref_id",
            "scope_ref_text",
        }
        return (
            serialize(await self._update("compat_rule_set", rule_set_id, payload, allowed=allowed, status_from="DRAFT"))
            or {}
        )

    async def activate_rule_set(self, rule_set_id: Any) -> dict[str, Any]:
        rule_set = await self._fetch("compat_rule_set", rule_set_id)
        if rule_set is None:
            raise NotFoundError("compatibility rule set not found")
        require_transition("compat_rule_set", rule_set["status"], "ACTIVE")
        if not await self._all(
            "SELECT id FROM manufacturing.compat_rule WHERE tenant_id = :tenant_id AND rule_set_id = :id",
            id=rule_set["id"],
        ):
            raise ValidationFailed("cannot activate a rule set without rules")
        return serialize(await self._update("compat_rule_set", rule_set_id, {"status": "ACTIVE"})) or {}

    async def retire_rule_set(self, rule_set_id: Any) -> dict[str, Any]:
        rule_set = await self._fetch("compat_rule_set", rule_set_id)
        if rule_set is None:
            raise NotFoundError("compatibility rule set not found")
        require_transition("compat_rule_set", rule_set["status"], "RETIRED")
        return serialize(await self._update("compat_rule_set", rule_set_id, {"status": "RETIRED"})) or {}

    async def add_rule(self, rule_set_id: Any, payload: Mapping[str, Any]) -> dict[str, Any]:
        rule_set = await self._fetch("compat_rule_set", rule_set_id)
        if rule_set is None:
            raise NotFoundError("compatibility rule set not found")
        if rule_set["status"] != "DRAFT":
            raise InvalidStateTransition("rules can only be edited on DRAFT rule sets")
        values = dict(payload)
        values["rule_set_id"] = rule_set["id"]
        return serialize(await self._create("compat_rule", values)) or {}

    async def update_rule(self, rule_id: Any, payload: Mapping[str, Any]) -> dict[str, Any]:
        rule = await self._fetch("compat_rule", rule_id)
        if rule is None:
            raise NotFoundError("compatibility rule not found")
        allowed = {"subject_expr", "constraint_expr", "severity", "message_code", "priority", "is_active"}
        return serialize(await self._update("compat_rule", rule_id, payload, allowed=allowed)) or {}

    async def create_slot(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return serialize(await self._create("compat_slot_def", payload)) or {}

    async def list_slots(self, *, assembly_item_id: Any | None = None) -> list[dict[str, Any]]:
        if assembly_item_id:
            rows = await self._all(
                "SELECT * FROM manufacturing.compat_slot_def WHERE tenant_id = :tenant_id AND assembly_item_id = :id",
                id=_uuid(assembly_item_id),
            )
            return [serialize(row) for row in rows]
        return [serialize(row) for row in await self._list("compat_slot_def")]

    async def update_slot(self, slot_id: Any, payload: Mapping[str, Any]) -> dict[str, Any]:
        allowed = {
            "capacity",
            "component_attribute_constraints",
            "units_per_component",
            "is_required",
            "sort_order",
            "is_active",
            "unit",
        }
        return serialize(await self._update("compat_slot_def", slot_id, payload, allowed=allowed)) or {}

    async def create_condition_code(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return serialize(await self._create("condition_code", payload)) or {}

    async def list_condition_codes(self) -> list[dict[str, Any]]:
        return [serialize(row) for row in await self._list("condition_code")]

    async def update_condition_code(self, condition_id: Any, payload: Mapping[str, Any]) -> dict[str, Any]:
        allowed = {
            "name",
            "is_used",
            "is_sellable_default",
            "is_committable_default",
            "description",
            "sort_order",
            "is_active",
        }
        return serialize(await self._update("condition_code", condition_id, payload, allowed=allowed)) or {}

    # ==================================================================== preferences =========
    async def get_preferences(self) -> dict[str, Any]:
        rows = await self._all(
            "SELECT key, value FROM platform.tenant_setting WHERE tenant_id = :tenant_id AND namespace = 'manufacturing' ORDER BY key"
        )
        flags = await self._all(
            "SELECT flag_key, is_enabled FROM platform.feature_flag WHERE tenant_id = :tenant_id AND flag_key LIKE 'manufacturing.%' ORDER BY flag_key"
        )
        return {
            "settings": {row["key"]: row["value"] for row in rows},
            "flags": {row["flag_key"]: row["is_enabled"] for row in flags},
        }

    async def update_preferences(self, settings: Mapping[str, Any]) -> dict[str, Any]:
        import json as json_module

        for key, value in settings.items():
            raw = json_module.dumps(value) if not isinstance(value, str) else json_module.dumps(value)
            await self._exec(
                "INSERT INTO platform.tenant_setting (id, tenant_id, namespace, key, value, value_type, is_default, created_at, updated_at, version) "
                "VALUES (gen_random_uuid(), :tenant_id, 'manufacturing', :key, CAST(:value AS jsonb), :value_type, false, now(), now(), 1) "
                "ON CONFLICT (tenant_id, namespace, key) DO UPDATE SET value = EXCLUDED.value, updated_at = now(), version = platform.tenant_setting.version + 1",
                key=key,
                value=raw,
                value_type="json",
            )
        return await self.get_preferences()

    # ==================================================================== deferred refs =======
    async def _upsert_component_control(self, bom_component_id: Any, payload: Mapping[str, Any]) -> dict[str, Any]:
        return await self.upsert_component_control(bom_component_id, payload)

    async def get_assembly_item_by_item_id(self, item_id: Any) -> dict[str, Any]:
        """Resolve the assembly extension from an item-master item id (FS-10 draft-WO path)."""
        row = await self._one(
            "SELECT * FROM manufacturing.assembly_item WHERE tenant_id = :tenant_id AND item_id = :item_id",
            item_id=_uuid(item_id, field="item_id"),
        )
        if row is None:
            raise NotFoundError("assembly item not found", details={"item_id": str(item_id)})
        return await self.get_assembly_item(row["id"])


TABLES = TABLE_BY_NAME
