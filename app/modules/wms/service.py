"""FS-11 Warehouse Management execution service (`wms.execution@1.0.0`).

Server-side WMS workflows only (D8: no mobile/device protocol; ``warehouse_cart``/``device_session`` are
backend containers). WMS tables never change inventory by themselves (``DM-WMS-01``): every task
completion calls the owning contract in the caller's transaction —

* receiving → ``procurement.p2p`` (create/post/reverse item receipt, T-01/T-02),
* putaway/replenishment/status change → ``inventory.ledger`` (T-29 bin transfer / T-30 status change),
* outbound picking/manual shipping → ``inventory.ledger`` fulfilment (T-12),
* work-order picking → ``manufacturing.execution`` when the contract is deployed (lazy bridge),
* WMS cycle-count requests → ``counts.engine``.

All methods participate in the caller's AsyncSession (no commits, no HTTP imports); every mutation
writes ``platform.audit_event`` and emits a WMS outbox event. Document numbers come from the FS-00
``SequenceService`` (``allow_undeclared=True``) and requests are idempotent through the FS-00
``IdempotencyStore``.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.idempotency_store import IdempotencyStore
from app.core.outbox import emit
from app.core.sequences import SequenceError, SequenceService
from app.modules.inventory_ledger.facade import BalanceReadPort, PostingPort
from app.modules.wms import strategies
from app.modules.wms import tables as t
from app.modules.wms.errors import (
    BinBlocked,
    DomainDisabled,
    DuplicateDocument,
    ExpiredLot,
    InvalidStateTransition,
    NotFound,
    StaleVersion,
    TaskConflict,
    ValidationFailed,
    WaveIncomplete,
)
from app.modules.wms.state_machines import OPEN_TASK_STATUSES, pick_rollup, validate_transition

SCHEMA = "wms"
FLAG_KEY = "wms.execution.enabled"
SETTINGS_NAMESPACE = "inventory"
SETTING_PREFIX = "wms."
SYSTEM_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000000000")
QTY_SCALE = Decimal("0.000000000001")
ZERO = Decimal("0")
DEFAULT_PAGE_LIMIT = 100
MAX_PAGE_LIMIT = 500


# ------------------------------------------------------------------------------- utilities ----
def _as_uuid(value: Any) -> uuid.UUID | None:
    if value is None or value == "":
        return None
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


def _qty(value: Any) -> Decimal:
    if value is None or value == "":
        return ZERO
    if isinstance(value, bool):
        raise ValidationFailed("quantity must be a decimal")
    try:
        decimal = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValidationFailed("quantity must be a decimal") from exc
    if not decimal.is_finite():
        raise ValidationFailed("quantity must be finite")
    return decimal.quantize(QTY_SCALE)


def _money(value: Any) -> Decimal:
    return _qty(value)


def _stamp(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _date(value: Any) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, list | tuple | set):
        return [_jsonable(item) for item in value]
    return value


def _row(row: Any) -> dict[str, Any]:
    mapping = getattr(row, "_mapping", row)
    return {key: value for key, value in dict(mapping).items()}


def _rows(rows: Sequence[Any]) -> list[dict[str, Any]]:
    return [_row(row) for row in rows]


def _limit(limit: int | None) -> int:
    try:
        value = int(limit or DEFAULT_PAGE_LIMIT)
    except (TypeError, ValueError) as exc:
        raise ValidationFailed("limit must be an integer") from exc
    if value < 1:
        raise ValidationFailed("limit must be >= 1")
    return min(value, MAX_PAGE_LIMIT)


#: setup entity registry: name -> (table, natural-key columns, allowed columns)
_SETUP_ENTITIES: dict[str, tuple[Any, tuple[str, ...], tuple[str, ...]]] = {
    "families": (t.item_process_family, ("code",), ("code", "name", "is_active")),
    "groups": (t.item_process_group, ("item_process_family_id", "code"),
               ("item_process_family_id", "code", "name", "is_active")),
    "assignments": (t.item_process_assignment, ("item_id", "location_id"),
                    ("item_id", "item_process_group_id", "location_id")),
    "stock-locations": (t.item_stock_location, ("item_id", "location_id", "bin_id"),
                        ("item_id", "location_id", "bin_id", "min_qty", "max_qty", "is_primary",
                         "replenishment_source_bin_id")),
    "order-types": (t.order_type, ("code",), ("code", "name", "direction", "is_active")),
    "ucc-code-formats": (t.ucc_code_format, ("code",), ("code", "name", "format_pattern", "is_active")),
    "carrier-service-levels": (t.carrier_service_level, ("carrier_code", "service_code"),
                               ("carrier_code", "service_code", "name", "is_active")),
}

_STRATEGY_ENTITIES: dict[str, tuple[Any, Any, str]] = {
    "pick-strategies": (t.pick_strategy, t.pick_strategy_rule, "pick_strategy_id"),
    "putaway-strategies": (t.putaway_strategy, t.putaway_strategy_rule, "putaway_strategy_id"),
}


class WmsService:
    """Tenant-scoped WMS execution service (all methods use the caller's transaction)."""

    def __init__(self, session: AsyncSession, tenant_id: str | uuid.UUID) -> None:
        self.session = session
        self.tenant_id = tenant_id if isinstance(tenant_id, uuid.UUID) else uuid.UUID(str(tenant_id))
        self.idempotency = IdempotencyStore(session)

    # ------------------------------------------------------------------ low-level helpers ----
    async def _exec(self, sql: str, **params: Any) -> Any:
        return await self.session.execute(text(sql), {"tenant_id": self.tenant_id, **params})

    async def _one(self, sql: str, **params: Any) -> dict[str, Any] | None:
        result = await self._exec(sql, **params)
        row = result.mappings().first()
        return _row(row) if row else None

    async def _all(self, sql: str, **params: Any) -> list[dict[str, Any]]:
        result = await self._exec(sql, **params)
        return _rows(result.mappings().all())

    async def _scalar(self, sql: str, **params: Any) -> Any:
        return (await self._exec(sql, **params)).scalar_one_or_none()

    async def _flag(self, key: str, default: bool = False) -> bool:
        value = await self._scalar(
            "SELECT is_enabled FROM platform.feature_flag WHERE tenant_id = :tenant_id AND flag_key = :key",
            key=key,
        )
        return bool(default if value is None else value)

    async def _setting(self, key: str, default: Any = None) -> Any:
        row = await self._one(
            "SELECT value FROM platform.tenant_setting "
            "WHERE tenant_id = :tenant_id AND namespace = :namespace AND key = :setting_key",
            namespace=SETTINGS_NAMESPACE,
            setting_key=SETTING_PREFIX + key,
        )
        if row is None:
            return default
        value = row["value"]
        if isinstance(value, str) and len(value) >= 2 and value[0] == '"' and value[-1] == '"':
            return value[1:-1]
        return value

    async def _require_enabled(self) -> None:
        if not await self._flag(FLAG_KEY, False):
            raise DomainDisabled("wms.execution is disabled for this tenant (wms.execution.enabled=false)")

    async def _next_number(self, doc_type: str) -> str:
        try:
            value = await SequenceService(self.session).next(self.tenant_id, doc_type, allow_undeclared=True)
            return value.formatted
        except SequenceError as exc:  # pragma: no cover - sequence misconfiguration
            raise ValidationFailed(f"cannot allocate {doc_type} document number: {exc}") from exc

    async def _audit(
        self,
        action: str,
        resource_type: str,
        resource_id: Any,
        diff: Mapping[str, Any] | None = None,
        actor_id: Any = None,
    ) -> None:
        event_id = uuid.uuid4()
        await self._exec(
            """
            INSERT INTO platform.audit_event
                (id, tenant_id, event_id, actor_type, actor_id, action, resource_type, resource_id, diff,
                 source, occurred_at)
            VALUES (:id, :tenant_id, :event_id, 'USER', :actor, :action, :resource_type, :resource_id,
                    CAST(:diff AS jsonb), 'API', now())
            """,
            id=event_id,
            event_id=event_id,
            actor=_as_uuid(actor_id),
            action=action,
            resource_type=resource_type,
            resource_id=str(resource_id),
            diff=json.dumps(_jsonable(dict(diff or {}))),
        )

    async def _emit(
        self, event_type: str, aggregate_type: str, aggregate_id: Any, payload: Mapping[str, Any], version: int = 1
    ) -> None:
        await emit(
            self.session,
            event_type=event_type,
            tenant_id=str(self.tenant_id),
            aggregate_type=aggregate_type,
            aggregate_id=str(aggregate_id),
            payload=_jsonable(dict(payload)),
            aggregate_version=max(1, int(version)),
        )

    async def _idem_begin(
        self, endpoint_id: str, key: str | None, body: Mapping[str, Any], actor_id: Any = None
    ) -> dict[str, Any] | None:
        if not key:
            return None
        from app.core.idempotency_store import build_fingerprint
        from app.core.security.idempotency import IdempotencyOutcome

        fingerprint = build_fingerprint(
            method="POST", path=endpoint_id, tenant_id=str(self.tenant_id), endpoint_id=endpoint_id, body=body
        )
        decision = await self.idempotency.guard(
            tenant_id=str(self.tenant_id),
            endpoint_id=endpoint_id,
            key=key,
            request_fingerprint_hash=fingerprint,
            actor_id=actor_id,
        )
        if decision.outcome is IdempotencyOutcome.REPLAY:
            stored = decision.record.response_body if decision.record is not None else None
            return {"replay": _jsonable(dict(stored or {}))}
        decision.raise_for_conflict()
        return None

    async def _idem_complete(
        self, endpoint_id: str, key: str | None, response: Mapping[str, Any], resource_id: Any, status: int = 200
    ) -> None:
        if not key:
            return
        await self.idempotency.complete(
            tenant_id=str(self.tenant_id),
            endpoint_id=endpoint_id,
            key=key,
            response_status=status,
            response_body=_jsonable(dict(response)),
            resource_id=str(resource_id),
        )

    async def _require_row(self, table: str, row_id: Any, label: str) -> dict[str, Any]:
        row = await self._one(f"SELECT * FROM {SCHEMA}.{table} WHERE tenant_id = :tenant_id AND id = :id",
                              id=_as_uuid(row_id))
        if row is None:
            raise NotFound(f"{label} not found")
        return row

    async def _default_status_id(self) -> uuid.UUID:
        status_id = await self._scalar(
            "SELECT id FROM traceability.inventory_status "
            "WHERE tenant_id = :tenant_id AND is_default AND NOT is_consigned ORDER BY created_at LIMIT 1"
        )
        if status_id is None:
            raise ValidationFailed("no default inventory status is configured for this tenant")
        return status_id

    async def _item(self, item_id: Any) -> dict[str, Any]:
        item = await self._one(
            "SELECT i.id, i.item_number, i.is_active, i.base_uom_id, it.requires_numbering, it.code AS item_type_code "
            "FROM item_master.item i JOIN item_master.item_type_definition it "
            "ON it.tenant_id = i.tenant_id AND it.id = i.item_type_id "
            "WHERE i.tenant_id = :tenant_id AND i.id = :id",
            id=_as_uuid(item_id),
        )
        if item is None:
            raise NotFound("item not found")
        if not item["is_active"]:
            raise ValidationFailed("item is inactive")
        return item

    def _posting(self) -> PostingPort:
        return PostingPort(self.session, self.tenant_id)

    def _balances(self) -> BalanceReadPort:
        return BalanceReadPort(self.session, self.tenant_id)

    def _counts(self) -> Any:
        from app.contracts.counts.contract import CountsPort

        return CountsPort(self.session, self.tenant_id)

    def _procurement(self) -> Any:
        from app.modules.procurement.service import ProcurementService

        return ProcurementService(self.session, self.tenant_id)

    async def _warehouse(self, warehouse_or_location_id: Any, *, by_location: bool = False) -> dict[str, Any]:
        column = "location_id" if by_location else "id"
        row = await self._one(
            f"SELECT * FROM {SCHEMA}.warehouse WHERE tenant_id = :tenant_id AND {column} = :value",
            value=_as_uuid(warehouse_or_location_id),
        )
        if row is None:
            raise NotFound("warehouse not found")
        return row

    async def _is_bin_blocked(self, bin_id: Any) -> bool:
        row = await self._one(
            "SELECT 1 FROM wms.bin_block WHERE tenant_id = :tenant_id AND bin_id = :bin AND released_at IS NULL "
            "AND blocked_from <= now() AND (blocked_to IS NULL OR blocked_to > now()) LIMIT 1",
            bin=_as_uuid(bin_id),
        )
        return row is not None

    async def _bin_positions(
        self,
        *,
        location_id: Any,
        item_id: Any,
        inventory_status_id: Any | None = None,
        bin_ids: Sequence[Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Per-bin on-hand positions from the FS-04 balance projection (bin quantities only)."""
        clauses = ["b.tenant_id = :tenant_id", "b.location_id = :location", "b.item_id = :item",
                   "b.qty_on_hand > 0", "b.ownership = 'OWNED'"]
        params: dict[str, Any] = {"location": _as_uuid(location_id), "item": _as_uuid(item_id)}
        if inventory_status_id:
            clauses.append("b.inventory_status_id = :status")
            params["status"] = _as_uuid(inventory_status_id)
        if bin_ids:
            clauses.append("b.bin_id = ANY(:bin_ids)")
            params["bin_ids"] = [_as_uuid(value) for value in bin_ids]
        rows = await self._all(
            f"""
            SELECT b.bin_id, b.inventory_status_id, b.lot_id, b.serial_id, b.qty_on_hand,
                   bn.code AS bin_code, bn.zone_id, bn.bin_type, bn.is_pickable,
                   l.expiry_date, l.created_at AS lot_created_at
              FROM inventory_ledger.inventory_balance_bin b
              JOIN traceability.bin bn ON bn.tenant_id = b.tenant_id AND bn.id = b.bin_id
              LEFT JOIN traceability.lot l ON l.tenant_id = b.tenant_id AND l.id = b.lot_id
             WHERE {" AND ".join(clauses)}
             ORDER BY bn.code
            """,
            **params,
        )
        return rows

    # ======================================================================== setup ----------
    async def create_warehouse(self, data: Mapping[str, Any], *, actor_id: Any = None,
                               idempotency_key: str | None = None) -> dict[str, Any]:
        await self._require_enabled()
        replay = await self._idem_begin("POST /api/v1/warehouses", idempotency_key, data, actor_id)
        if replay:
            return replay["replay"]
        location_id = _as_uuid(data.get("location_id"))
        if location_id is None:
            raise ValidationFailed("location_id is required")
        location = await self._one(
            "SELECT id, is_bins_enabled, is_inactive FROM traceability.location "
            "WHERE tenant_id = :tenant_id AND id = :id",
            id=location_id,
        )
        if location is None:
            raise NotFound("location not found")
        if not location["is_bins_enabled"]:
            raise ValidationFailed("WMS requires a bin-enabled location (is_bins_enabled=true)")
        if location["is_inactive"]:
            raise ValidationFailed("location is inactive")
        code = str(data.get("code") or "").strip()
        if not code:
            raise ValidationFailed("code is required")
        warehouse_id = uuid.uuid4()
        try:
            await self._exec(
                """
                INSERT INTO wms.warehouse
                    (id, tenant_id, location_id, code, name, is_wms_enabled, receiving_bin_id, staging_bin_id,
                     shipping_bin_id, default_order_type_id, timezone, is_active, created_by, updated_by,
                     created_at, updated_at, version)
                VALUES (:id, :tenant_id, :location, :code, :name, :enabled, :receiving, :staging, :shipping,
                        :order_type, :timezone, true, :actor, :actor, now(), now(), 1)
                """,
                id=warehouse_id,
                location=location_id,
                code=code,
                name=str(data.get("name") or code),
                enabled=bool(data.get("is_wms_enabled", False)),
                receiving=_as_uuid(data.get("receiving_bin_id")),
                staging=_as_uuid(data.get("staging_bin_id")),
                shipping=_as_uuid(data.get("shipping_bin_id")),
                order_type=_as_uuid(data.get("default_order_type_id")),
                timezone=str(data.get("timezone") or "UTC"),
                actor=_as_uuid(actor_id),
            )
        except IntegrityError as exc:
            raise DuplicateDocument("warehouse code or location already configured") from exc
        await self._audit("warehouse.created", "wms.warehouse", warehouse_id, {"code": code}, actor_id)
        result = await self.get_warehouse(warehouse_id)
        await self._idem_complete("POST /api/v1/warehouses", idempotency_key, result, warehouse_id, 201)
        return result

    async def list_warehouses(self, *, location_id: Any = None, is_active: bool | None = None,
                              limit: int = DEFAULT_PAGE_LIMIT) -> list[dict[str, Any]]:
        clauses = ["w.tenant_id = :tenant_id"]
        params: dict[str, Any] = {}
        if location_id:
            clauses.append("w.location_id = :location")
            params["location"] = _as_uuid(location_id)
        if is_active is not None:
            clauses.append("w.is_active = :active")
            params["active"] = bool(is_active)
        return await self._all(
            f"SELECT w.*, l.code AS location_code, l.name AS location_name FROM wms.warehouse w "
            f"JOIN traceability.location l ON l.tenant_id = w.tenant_id AND l.id = w.location_id "
            f"WHERE {' AND '.join(clauses)} ORDER BY w.code LIMIT :limit",
            limit=_limit(limit),
            **params,
        )

    async def get_warehouse(self, warehouse_id: Any) -> dict[str, Any]:
        row = await self._one(
            "SELECT w.*, l.code AS location_code, l.name AS location_name FROM wms.warehouse w "
            "JOIN traceability.location l ON l.tenant_id = w.tenant_id AND l.id = w.location_id "
            "WHERE w.tenant_id = :tenant_id AND w.id = :id",
            id=_as_uuid(warehouse_id),
        )
        if row is None:
            raise NotFound("warehouse not found")
        row["rules"] = await self.list_system_rules(warehouse_id)
        return row

    async def update_warehouse(self, warehouse_id: Any, data: Mapping[str, Any], *, actor_id: Any = None,
                               expected_version: int | None = None) -> dict[str, Any]:
        await self._require_enabled()
        warehouse = await self._require_row("warehouse", warehouse_id, "warehouse")
        if expected_version is not None and int(warehouse["version"]) != int(expected_version):
            raise StaleVersion("warehouse was modified; reload and retry")
        allowed = {"name", "is_wms_enabled", "receiving_bin_id", "staging_bin_id", "shipping_bin_id",
                   "default_order_type_id", "timezone", "is_active"}
        updates = {key: value for key, value in data.items() if key in allowed}
        if not updates:
            return await self.get_warehouse(warehouse_id)
        sets = []
        params: dict[str, Any] = {"id": _as_uuid(warehouse_id), "actor": _as_uuid(actor_id)}
        for index, (key, value) in enumerate(updates.items()):
            cast = "::uuid" if key.endswith("_id") else ""
            sets.append(f"{key} = :p{index}{cast}")
            params[f"p{index}"] = _as_uuid(value) if key.endswith("_id") else value
        await self._exec(
            f"UPDATE wms.warehouse SET {', '.join(sets)}, updated_by = :actor, updated_at = now(), "
            f"version = version + 1 WHERE tenant_id = :tenant_id AND id = :id",
            **params,
        )
        await self._audit("warehouse.updated", "wms.warehouse", warehouse_id, updates, actor_id)
        return await self.get_warehouse(warehouse_id)

    async def create_setup(self, kind: str, data: Mapping[str, Any], *, actor_id: Any = None,
                           idempotency_key: str | None = None) -> dict[str, Any]:
        await self._require_enabled()
        table, _keys, allowed = self._setup_registry(kind)
        return await self._insert_row(kind, table, allowed, data, actor_id=actor_id,
                                      idempotency_key=idempotency_key)

    def _setup_registry(self, kind: str) -> tuple[Any, tuple[str, ...], tuple[str, ...]]:
        if kind in _STRATEGY_ENTITIES:
            table, _rule_table, _fk = _STRATEGY_ENTITIES[kind]
            columns = tuple(column.name for column in table.columns if column.name not in {
                "id", "tenant_id", "created_at", "updated_at", "created_by", "updated_by", "version"})
            return table, ("code",), columns
        entity = _SETUP_ENTITIES.get(kind)
        if entity is None:
            raise NotFound(f"unknown setup entity {kind!r}")
        return entity

    async def _insert_row(self, kind: str, table: Any, allowed: Sequence[str], data: Mapping[str, Any],
                          *, actor_id: Any = None, idempotency_key: str | None = None,
                          response_key: str = "item") -> dict[str, Any]:
        values: dict[str, Any] = {}
        for column in allowed:
            if column in data and data[column] is not None:
                value = data[column]
                python_type = table.columns[column].type.python_type
                if python_type is uuid.UUID:
                    values[column] = _as_uuid(value)
                elif python_type is Decimal:
                    values[column] = _qty(value)
                elif python_type is bool:
                    values[column] = bool(value)
                else:
                    values[column] = value
        row_id = uuid.uuid4()
        names = ["id", "tenant_id", *values.keys(), "created_by", "updated_by"]
        placeholders = [":id", ":tenant_id", *(f":{name}" for name in values), ":actor", ":actor"]
        await self._exec(
            f"INSERT INTO {SCHEMA}.{table.name} ({', '.join(names)}) VALUES ({', '.join(placeholders)})",
            id=row_id,
            actor=_as_uuid(actor_id),
            **values,
        )
        await self._audit(f"{kind}.created", f"wms.{table.name}", row_id, values, actor_id)
        return await self._row_by_id(table, row_id)

    async def _row_by_id(self, table: Any, row_id: Any) -> dict[str, Any]:
        row = await self._one(f"SELECT * FROM {SCHEMA}.{table.name} WHERE tenant_id = :tenant_id AND id = :id",
                              id=_as_uuid(row_id))
        if row is None:  # pragma: no cover - defensive
            raise NotFound(f"{table.name} row not found")
        return row

    async def list_setup(self, kind: str, *, filters: Mapping[str, Any] | None = None,
                         limit: int = DEFAULT_PAGE_LIMIT) -> list[dict[str, Any]]:
        table, _keys, _allowed = self._setup_registry(kind)
        clauses = ["tenant_id = :tenant_id"]
        params: dict[str, Any] = {}
        for column, value in (filters or {}).items():
            if column not in table.columns or value is None:
                continue
            clauses.append(f"{column} = :{column}")
            params[column] = _as_uuid(value) if table.columns[column].type.python_type is uuid.UUID else value
        order = "sort_order, code" if "sort_order" in table.columns else "created_at, id"
        return await self._all(
            f"SELECT * FROM {SCHEMA}.{table.name} WHERE {' AND '.join(clauses)} ORDER BY {order} LIMIT :limit",
            limit=_limit(limit),
            **params,
        )

    async def update_setup(self, kind: str, row_id: Any, data: Mapping[str, Any], *, actor_id: Any = None,
                           expected_version: int | None = None) -> dict[str, Any]:
        await self._require_enabled()
        table, _keys, allowed = self._setup_registry(kind)
        row = await self._row_by_id(table, row_id)
        if expected_version is not None and int(row["version"]) != int(expected_version):
            raise StaleVersion("row was modified; reload and retry")
        values = {}
        for column in allowed:
            if column in data and data[column] is not None:
                python_type = table.columns[column].type.python_type
                value = data[column]
                values[column] = _as_uuid(value) if python_type is uuid.UUID else (
                    _qty(value) if python_type is Decimal else value)
        if not values:
            return row
        sets = [f"{column} = :{column}" for column in values]
        await self._exec(
            f"UPDATE {SCHEMA}.{table.name} SET {', '.join(sets)}, updated_by = :actor, updated_at = now(), "
            f"version = version + 1 WHERE tenant_id = :tenant_id AND id = :id",
            id=_as_uuid(row_id), actor=_as_uuid(actor_id), **values,
        )
        await self._audit(f"{kind}.updated", f"wms.{table.name}", row_id, values, actor_id)
        return await self._row_by_id(table, row_id)

    async def create_strategy(self, kind: str, data: Mapping[str, Any], *, actor_id: Any = None,
                              idempotency_key: str | None = None) -> dict[str, Any]:
        entity = _STRATEGY_ENTITIES.get(kind)
        if entity is None:
            raise NotFound(f"unknown strategy {kind!r}")
        parent_table, rule_table, fk = entity
        replay = await self._idem_begin(f"POST /api/v1/{kind}", idempotency_key, data, actor_id)
        if replay:
            return replay["replay"]
        result = await self._insert_row(kind, parent_table, (
            "code", "name", "priority", "applies_to_location_id", "staging_bin_id", "is_active"), data,
            actor_id=actor_id)
        await self._replace_rules(kind, rule_table, fk, result["id"], data.get("rules") or [])
        result = await self.get_strategy(kind, result["id"])
        await self._idem_complete(f"POST /api/v1/{kind}", idempotency_key, result, result["id"], 201)
        return result

    async def _replace_rules(self, kind: str, rule_table: Any, fk: str, parent_id: Any,
                             rules: Sequence[Mapping[str, Any]]) -> None:
        await self._exec(f"DELETE FROM {SCHEMA}.{rule_table.name} WHERE tenant_id = :tenant_id AND {fk} = :parent",
                         parent=_as_uuid(parent_id))
        for index, rule in enumerate(rules, start=1):
            entry = dict(rule)
            entry["sequence"] = int(entry.get("sequence") or index)
            columns = [column for column in ("sequence", "zone_id", "item_process_group_id", "sort_direction",
                                             "bin_type", "max_qty_per_bin", "is_active")
                       if column in rule_table.columns]
            values = {}
            for column in columns:
                if column in entry and entry[column] is not None:
                    python_type = rule_table.columns[column].type.python_type
                    values[column] = _as_uuid(entry[column]) if python_type is uuid.UUID else (
                        _qty(entry[column]) if python_type is Decimal else entry[column])
            names = ["id", "tenant_id", fk, *values.keys()]
            placeholders = [":id", ":tenant_id", ":parent", *(f":{name}" for name in values)]
            await self._exec(
                f"INSERT INTO {SCHEMA}.{rule_table.name} ({', '.join(names)}) "
                f"VALUES ({', '.join(placeholders)})",
                id=uuid.uuid4(), parent=_as_uuid(parent_id), **values,
            )

    async def get_strategy(self, kind: str, strategy_id: Any) -> dict[str, Any]:
        entity = _STRATEGY_ENTITIES.get(kind)
        if entity is None:
            raise NotFound(f"unknown strategy {kind!r}")
        parent_table, rule_table, fk = entity
        row = await self._row_by_id(parent_table, strategy_id)
        row["rules"] = await self._all(
            f"SELECT * FROM {SCHEMA}.{rule_table.name} WHERE tenant_id = :tenant_id AND {fk} = :parent "
            f"ORDER BY sequence",
            parent=_as_uuid(strategy_id),
        )
        return row

    async def list_strategies(self, kind: str, *, is_active: bool | None = None,
                              limit: int = DEFAULT_PAGE_LIMIT) -> list[dict[str, Any]]:
        entity = _STRATEGY_ENTITIES.get(kind)
        if entity is None:
            raise NotFound(f"unknown strategy {kind!r}")
        parent_table, _rule_table, _fk = entity
        clauses = ["tenant_id = :tenant_id"]
        params: dict[str, Any] = {}
        if is_active is not None:
            clauses.append("is_active = :active")
            params["active"] = bool(is_active)
        return await self._all(
            f"SELECT * FROM {SCHEMA}.{parent_table.name} WHERE {' AND '.join(clauses)} "
            f"ORDER BY priority, code LIMIT :limit",
            limit=_limit(limit),
            **params,
        )

    async def update_strategy(self, kind: str, strategy_id: Any, data: Mapping[str, Any], *,
                              actor_id: Any = None, expected_version: int | None = None) -> dict[str, Any]:
        entity = _STRATEGY_ENTITIES.get(kind)
        if entity is None:
            raise NotFound(f"unknown strategy {kind!r}")
        parent_table, rule_table, fk = entity
        await self.update_setup(kind, strategy_id, data, actor_id=actor_id,
                                expected_version=expected_version)
        if "rules" in data:
            await self._replace_rules(kind, rule_table, fk, strategy_id, data.get("rules") or [])
        return await self.get_strategy(kind, strategy_id)

    # -- system rules -----------------------------------------------------------------------
    async def list_system_rules(self, warehouse_id: Any | None = None) -> list[dict[str, Any]]:
        rows = await self._all("SELECT * FROM wms.system_rule WHERE tenant_id = :tenant_id ORDER BY code")
        if not rows:
            # Tenants created after the 0023 seed migration still get the frozen default catalogue.
            default_rows: list[dict[str, Any]] = [
                {
                    "id": None,
                    "tenant_id": str(self.tenant_id),
                    "code": code,
                    "name": name,
                    "description": description,
                    "default_enabled": default_enabled,
                    "applies_to": applies_to,
                    "value_type": "BOOLEAN",
                    "is_active": True,
                }
                for code, name, description, default_enabled, applies_to in t.SYSTEM_RULE_SEEDS
            ]
            rows = default_rows
        settings: dict[str, dict[str, Any]] = {}
        if warehouse_id:
            for setting in await self._all(
                "SELECT sr.code, s.is_enabled, s.value FROM wms.system_rule_setting s "
                "JOIN wms.system_rule sr ON sr.tenant_id = s.tenant_id AND sr.id = s.system_rule_id "
                "WHERE s.tenant_id = :tenant_id AND s.warehouse_id = :warehouse",
                warehouse=_as_uuid(warehouse_id),
            ):
                settings[setting["code"]] = setting
        for row in rows:
            override = settings.get(row["code"])
            row["effective_enabled"] = override["is_enabled"] if override else row["default_enabled"]
            row["override"] = {"is_enabled": override["is_enabled"], "value": override["value"]} if override else None
        return rows

    async def set_system_rule(self, warehouse_id: Any, code: str, data: Mapping[str, Any], *,
                              actor_id: Any = None) -> dict[str, Any]:
        await self._require_enabled()
        warehouse = await self._require_row("warehouse", warehouse_id, "warehouse")
        rule = await self._one(
            "SELECT * FROM wms.system_rule WHERE tenant_id = :tenant_id AND code = :code", code=str(code)
        )
        if rule is None:
            seed = next((entry for entry in t.SYSTEM_RULE_SEEDS if entry[0] == str(code)), None)
            if seed is None:
                raise NotFound("system rule not found")
            rule_id = uuid.uuid4()
            await self._exec(
                """
                INSERT INTO wms.system_rule
                    (id, tenant_id, code, name, description, default_enabled, applies_to, value_type, is_active,
                     created_at, updated_at, version)
                VALUES (:id, :tenant_id, :code, :name, :description, :enabled, :applies_to, 'BOOLEAN', true,
                        now(), now(), 1)
                """,
                id=rule_id,
                code=seed[0],
                name=seed[1],
                description=seed[2],
                enabled=seed[3],
                applies_to=seed[4],
            )
            rule = {"id": rule_id, "code": seed[0]}
        enabled = bool(data.get("is_enabled", True))
        value = json.dumps(_jsonable(data.get("value") or {}))
        await self._exec(
            """
            INSERT INTO wms.system_rule_setting
                (id, tenant_id, system_rule_id, warehouse_id, is_enabled, value, created_by, updated_by,
                 created_at, updated_at, version)
            VALUES (:id, :tenant_id, :rule, :warehouse, :enabled, CAST(:value AS jsonb), :actor, :actor,
                    now(), now(), 1)
            ON CONFLICT (tenant_id, system_rule_id, warehouse_id)
            DO UPDATE SET is_enabled = EXCLUDED.is_enabled, value = EXCLUDED.value,
                          updated_by = EXCLUDED.updated_by, updated_at = now(),
                          version = wms.system_rule_setting.version + 1
            """,
            id=uuid.uuid4(),
            rule=_as_uuid(rule["id"]),
            warehouse=_as_uuid(warehouse["id"]),
            enabled=enabled,
            value=value,
            actor=_as_uuid(actor_id),
        )
        await self._audit("system_rule.updated", "wms.system_rule", rule["id"],
                          {"warehouse_id": str(warehouse["id"]), "is_enabled": enabled}, actor_id)
        return {"code": rule["code"], "warehouse_id": str(warehouse["id"]), "is_enabled": enabled}

    # -- bin blocks -------------------------------------------------------------------------
    async def create_bin_block(self, data: Mapping[str, Any], *, actor_id: Any = None,
                               idempotency_key: str | None = None) -> dict[str, Any]:
        await self._require_enabled()
        replay = await self._idem_begin("POST /api/v1/bin-blocks", idempotency_key, data, actor_id)
        if replay:
            return replay["replay"]
        bin_id = _as_uuid(data.get("bin_id"))
        if bin_id is None:
            raise ValidationFailed("bin_id is required")
        await self._require_bin(bin_id)
        block_id = uuid.uuid4()
        await self._exec(
            """
            INSERT INTO wms.bin_block
                (id, tenant_id, bin_id, reason_code_id, reason, blocked_from, blocked_to, created_by, updated_by,
                 created_at, updated_at, version)
            VALUES (:id, :tenant_id, :bin, :reason_code, :reason, COALESCE(:blocked_from, now()), :blocked_to,
                    :actor, :actor, now(), now(), 1)
            """,
            id=block_id,
            bin=bin_id,
            reason_code=_as_uuid(data.get("reason_code_id")),
            reason=data.get("reason"),
            blocked_from=_stamp(data.get("blocked_from")),
            blocked_to=_stamp(data.get("blocked_to")),
            actor=_as_uuid(actor_id),
        )
        await self._audit("bin_block.created", "wms.bin_block", block_id, {"bin_id": str(bin_id)}, actor_id)
        result = await self._row_by_id(t.bin_block, block_id)
        await self._idem_complete("POST /api/v1/bin-blocks", idempotency_key, result, block_id, 201)
        return result

    async def list_bin_blocks(self, *, bin_id: Any = None, released: bool | None = None,
                              limit: int = DEFAULT_PAGE_LIMIT) -> list[dict[str, Any]]:
        clauses = ["tenant_id = :tenant_id"]
        params: dict[str, Any] = {}
        if bin_id:
            clauses.append("bin_id = :bin")
            params["bin"] = _as_uuid(bin_id)
        if released is not None:
            clauses.append("released_at IS NULL" if not released else "released_at IS NOT NULL")
        return await self._all(
            f"SELECT * FROM wms.bin_block WHERE {' AND '.join(clauses)} ORDER BY blocked_from DESC LIMIT :limit",
            limit=_limit(limit),
            **params,
        )

    async def release_bin_block(self, block_id: Any, *, actor_id: Any = None,
                                idempotency_key: str | None = None) -> dict[str, Any]:
        await self._require_enabled()
        block = await self._require_row("bin_block", block_id, "bin block")
        if block["released_at"] is None:
            await self._exec(
                "UPDATE wms.bin_block SET released_at = now(), updated_by = :actor, updated_at = now(), "
                "version = version + 1 WHERE tenant_id = :tenant_id AND id = :id",
                id=_as_uuid(block_id),
                actor=_as_uuid(actor_id),
            )
            await self._audit("bin_block.released", "wms.bin_block", block_id, {}, actor_id)
        return await self._row_by_id(t.bin_block, block_id)

    async def _require_bin(self, bin_id: Any) -> dict[str, Any]:
        bin_row = await self._one(
            "SELECT b.*, z.code AS zone_code FROM traceability.bin b "
            "LEFT JOIN traceability.zone z ON z.tenant_id = b.tenant_id AND z.id = b.zone_id "
            "WHERE b.tenant_id = :tenant_id AND b.id = :id",
            id=_as_uuid(bin_id),
        )
        if bin_row is None:
            raise NotFound("bin not found")
        if not bin_row["is_active"]:
            raise ValidationFailed("bin is inactive")
        return bin_row

    async def assign_item_bins(self, warehouse_id: Any, data: Mapping[str, Any], *, actor_id: Any = None,
                               idempotency_key: str | None = None) -> dict[str, Any]:
        await self._require_enabled()
        warehouse = await self._require_row("warehouse", warehouse_id, "warehouse")
        bin_id = _as_uuid(data.get("bin_id"))
        if bin_id is None:
            raise ValidationFailed("bin_id is required")
        await self._require_bin(bin_id)
        item_ids = [_as_uuid(value) for value in (data.get("item_ids") or []) if value]
        if not item_ids:
            raise ValidationFailed("item_ids must contain at least one item")
        for item_id in item_ids:
            await self._item(item_id)
            existing = await self._one(
                "SELECT 1 FROM item_master.item_bin WHERE tenant_id = :tenant_id AND item_id = :item "
                "AND location_id = :location AND bin_id = :bin",
                item=item_id,
                location=warehouse["location_id"],
                bin=bin_id,
            )
            if existing is None:
                await self._exec(
                    """
                    INSERT INTO item_master.item_bin
                        (tenant_id, item_id, location_id, bin_id, is_preferred, is_default_receiving,
                         is_default_picking, created_by, updated_by, created_at, updated_at, version)
                    VALUES (:tenant_id, :item, :location, :bin, false, false, false, :actor, :actor,
                            now(), now(), 1)
                    """,
                    item=item_id,
                    location=warehouse["location_id"],
                    bin=bin_id,
                    actor=_as_uuid(actor_id),
                )
        await self._audit("item_bin.assigned", "wms.warehouse", warehouse["id"],
                          {"bin_id": str(bin_id), "items": len(item_ids)}, actor_id)
        return {"warehouse_id": str(warehouse["id"]), "bin_id": str(bin_id), "assigned": len(item_ids)}

    # -- inventory reads/writes -------------------------------------------------------------
    async def list_warehouse_inventory(self, warehouse_id: Any, *, item_id: Any = None, bin_id: Any = None,
                                       inventory_status_id: Any = None, limit: int = DEFAULT_PAGE_LIMIT) -> dict[str, Any]:
        warehouse = await self._warehouse(warehouse_id)
        clauses = ["b.tenant_id = :tenant_id", "b.location_id = :location"]
        params: dict[str, Any] = {"location": warehouse["location_id"]}
        if item_id:
            clauses.append("b.item_id = :item")
            params["item"] = _as_uuid(item_id)
        if bin_id:
            clauses.append("b.bin_id = :bin")
            params["bin"] = _as_uuid(bin_id)
        if inventory_status_id:
            clauses.append("b.inventory_status_id = :status")
            params["status"] = _as_uuid(inventory_status_id)
        rows = await self._all(
            f"""
            SELECT b.item_id, i.item_number, b.location_id, b.bin_id, bn.code AS bin_code,
                   b.inventory_status_id, st.code AS status_code, b.lot_id, b.serial_id,
                   b.qty_on_hand, 0::numeric AS qty_committed
              FROM inventory_ledger.inventory_balance_bin b
              JOIN item_master.item i ON i.tenant_id = b.tenant_id AND i.id = b.item_id
              JOIN traceability.bin bn ON bn.tenant_id = b.tenant_id AND bn.id = b.bin_id
              JOIN traceability.inventory_status st ON st.tenant_id = b.tenant_id AND st.id = b.inventory_status_id
             WHERE {" AND ".join(clauses)}
             ORDER BY i.item_number, bn.code LIMIT :limit
            """,
            limit=_limit(limit),
            **params,
        )
        return {"warehouse_id": str(warehouse["id"]), "location_id": str(warehouse["location_id"]),
                "items": rows, "count": len(rows)}

    async def create_warehouse_inventory(self, warehouse_id: Any, data: Mapping[str, Any], *,
                                         actor_id: Any = None,
                                         idempotency_key: str | None = None) -> dict[str, Any]:
        await self._require_enabled()
        warehouse = await self._warehouse(warehouse_id)
        item_id = _as_uuid(data.get("item_id"))
        if item_id is None:
            raise ValidationFailed("item_id is required")
        await self._item(item_id)
        qty = _qty(data.get("qty"))
        if qty <= 0:
            raise ValidationFailed("qty must be > 0")
        bin_id = _as_uuid(data.get("bin_id"))
        if bin_id is None:
            raise ValidationFailed("bin_id is required")
        await self._require_bin(bin_id)
        status_id = _as_uuid(data.get("inventory_status_id")) or await self._default_status_id()
        idem = idempotency_key or f"wms-inventory-create:{warehouse_id}:{uuid.uuid4()}"
        movement = {
            "item_id": str(item_id),
            "location_id": str(warehouse["location_id"]),
            "inventory_status_id": str(status_id),
            "qty": str(qty),
            "bin_id": str(bin_id),
            "lot_id": str(_as_uuid(data.get("lot_id"))) if data.get("lot_id") else None,
            "reason_code_id": str(_as_uuid(data.get("reason_code_id"))) if data.get("reason_code_id") else None,
            "unit_cost": str(_money(data.get("unit_cost") or 0)),
        }
        result = await self._posting().post_adjustment(
            transaction_date=_date(data.get("transaction_date")) or datetime.now(UTC).date(),
            source_doc_type="WMS_INVENTORY_CREATE",
            source_doc_id=_as_uuid(warehouse.get("id")),
            movements=[movement],
            idempotency_key=idem,
        )
        await self._audit("inventory.created", "wms.warehouse", warehouse["id"],
                          {"item_id": str(item_id), "qty": str(qty), "bin_id": str(bin_id)}, actor_id)
        return {"warehouse_id": str(warehouse["id"]), "inventory_transaction_id": result["transaction_id"],
                "qty": str(qty)}

    async def request_cycle_count(self, warehouse_id: Any, data: Mapping[str, Any], *, actor_id: Any = None,
                                  idempotency_key: str | None = None) -> dict[str, Any]:
        await self._require_enabled()
        warehouse = await self._warehouse(warehouse_id)
        scope = {
            "locations": [str(warehouse["location_id"])],
            "items": [str(_as_uuid(value)) for value in (data.get("item_ids") or [])],
            "bins": [str(_as_uuid(value)) for value in (data.get("bin_ids") or [])],
        }
        count = await self._counts().create_count(
            scope,
            {
                "count_date": _date(data.get("plan_date")) or datetime.now(UTC).date(),
                "assignee_id": data.get("assignee_id"),
                "approval_required": bool(data.get("approval_required", True)),
            },
        )
        await self._audit("cycle_count.requested", "wms.warehouse", warehouse["id"],
                          {"count_id": count.get("id")}, actor_id)
        return {"warehouse_id": str(warehouse["id"]), "count_id": count.get("count_id") or count.get("id"),
                "status": count.get("status")}

    # ====================================================================== inbound ----------
    async def create_inbound_shipment(self, data: Mapping[str, Any], *, actor_id: Any = None,
                                      idempotency_key: str | None = None) -> dict[str, Any]:
        await self._require_enabled()
        replay = await self._idem_begin("POST /api/v1/inbound-shipments", idempotency_key, data, actor_id)
        if replay:
            return replay["replay"]
        location_id = _as_uuid(data.get("location_id"))
        if location_id is None:
            raise ValidationFailed("location_id is required")
        await self._warehouse(location_id, by_location=True)
        shipment_id = uuid.uuid4()
        number = str(data.get("shipment_number") or "").strip() or await self._next_number("ASN")
        try:
            await self._exec(
                """
                INSERT INTO wms.inbound_shipment
                    (id, tenant_id, shipment_number, vendor_id, purchase_order_id, location_id,
                     expected_arrival_date, status, carrier_code, tracking_number, origin_country, memo,
                     created_by, updated_by, created_at, updated_at, version)
                VALUES (:id, :tenant_id, :number, :vendor, :po, :location, :expected, 'DRAFT', :carrier,
                        :tracking, :origin, :memo, :actor, :actor, now(), now(), 1)
                """,
                id=shipment_id,
                number=number,
                vendor=_as_uuid(data.get("vendor_id")),
                po=_as_uuid(data.get("purchase_order_id")),
                location=location_id,
                expected=_date(data.get("expected_arrival_date")),
                carrier=data.get("carrier_code"),
                tracking=data.get("tracking_number"),
                origin=data.get("origin_country"),
                memo=data.get("memo"),
                actor=_as_uuid(actor_id),
            )
        except IntegrityError as exc:
            raise DuplicateDocument("inbound shipment number already exists") from exc
        for index, line in enumerate(data.get("lines") or [], start=1):
            item_id = _as_uuid(line.get("item_id"))
            if item_id is None:
                raise ValidationFailed(f"line {index}: item_id is required")
            await self._item(item_id)
            await self._exec(
                """
                INSERT INTO wms.inbound_shipment_line
                    (id, tenant_id, inbound_shipment_id, line_number, item_id, purchase_order_line_id,
                     qty_expected, qty_received, uom_id, lot_id, serial_ids, inventory_status_id, unit_cost)
                VALUES (:id, :tenant_id, :shipment, :line, :item, :po_line, :expected, 0, :uom, :lot,
                        :serials, :status, :cost)
                """,
                id=uuid.uuid4(),
                shipment=shipment_id,
                line=int(line.get("line_number") or index),
                item=item_id,
                po_line=_as_uuid(line.get("purchase_order_line_id")),
                expected=_qty(line.get("qty_expected")),
                uom=_as_uuid(line.get("uom_id")),
                lot=_as_uuid(line.get("lot_id")),
                serials=[_as_uuid(value) for value in (line.get("serial_ids") or [])] or None,
                status=_as_uuid(line.get("inventory_status_id")),
                cost=_money(line["unit_cost"]) if line.get("unit_cost") is not None else None,
            )
        await self._audit("inbound_shipment.created", "wms.inbound_shipment", shipment_id,
                          {"shipment_number": number, "lines": len(data.get("lines") or [])}, actor_id)
        await self._emit("wms.inbound_shipment.created", "inbound_shipment", shipment_id,
                         {"tenant_id": str(self.tenant_id), "inbound_shipment_id": str(shipment_id),
                          "shipment_number": number, "location_id": str(location_id)})
        result = await self.get_inbound_shipment(shipment_id)
        await self._idem_complete("POST /api/v1/inbound-shipments", idempotency_key, result, shipment_id, 201)
        return result

    async def list_inbound_shipments(self, *, location_id: Any = None, status: str | None = None,
                                     limit: int = DEFAULT_PAGE_LIMIT) -> list[dict[str, Any]]:
        clauses = ["s.tenant_id = :tenant_id"]
        params: dict[str, Any] = {}
        if location_id:
            clauses.append("s.location_id = :location")
            params["location"] = _as_uuid(location_id)
        if status:
            clauses.append("s.status = :status")
            params["status"] = status.upper()
        return await self._all(
            f"SELECT s.*, l.code AS location_code FROM wms.inbound_shipment s "
            f"JOIN traceability.location l ON l.tenant_id = s.tenant_id AND l.id = s.location_id "
            f"WHERE {' AND '.join(clauses)} ORDER BY s.created_at DESC LIMIT :limit",
            limit=_limit(limit),
            **params,
        )

    async def get_inbound_shipment(self, shipment_id: Any) -> dict[str, Any]:
        row = await self._require_row("inbound_shipment", shipment_id, "inbound shipment")
        row["lines"] = await self._all(
            "SELECT * FROM wms.inbound_shipment_line WHERE tenant_id = :tenant_id AND inbound_shipment_id = :id "
            "ORDER BY line_number",
            id=_as_uuid(shipment_id),
        )
        return row

    async def transition_inbound_shipment(self, shipment_id: Any, target: str, *, actor_id: Any = None,
                                          idempotency_key: str | None = None) -> dict[str, Any]:
        await self._require_enabled()
        row = await self._require_row("inbound_shipment", shipment_id, "inbound shipment")
        target = target.upper()
        validate_transition("inbound_shipment", row["status"], target)
        values: dict[str, Any] = {"status": target}
        if target == "ARRIVED":
            values["received_at"] = datetime.now(UTC)
        await self._exec(
            "UPDATE wms.inbound_shipment SET status = :status, updated_by = :actor, updated_at = now(), "
            "version = version + 1 WHERE tenant_id = :tenant_id AND id = :id",
            status=target,
            actor=_as_uuid(actor_id),
            id=_as_uuid(shipment_id),
        )
        await self._audit(f"inbound_shipment.{target.lower()}", "wms.inbound_shipment", shipment_id, {}, actor_id)
        if target == "ARRIVED":
            await self._emit("wms.inbound_shipment.arrived", "inbound_shipment", shipment_id,
                             {"tenant_id": str(self.tenant_id), "inbound_shipment_id": str(shipment_id),
                              "status": target})
        return await self.get_inbound_shipment(shipment_id)

    async def create_receiving_order(self, data: Mapping[str, Any], *, actor_id: Any = None,
                                     idempotency_key: str | None = None) -> dict[str, Any]:
        await self._require_enabled()
        replay = await self._idem_begin("POST /api/v1/receiving-orders", idempotency_key, data, actor_id)
        if replay:
            return replay["replay"]
        shipment = None
        if data.get("inbound_shipment_id"):
            shipment = await self.get_inbound_shipment(data["inbound_shipment_id"])
        location_id = _as_uuid(data.get("location_id")) or (_as_uuid(shipment["location_id"]) if shipment else None)
        if location_id is None:
            raise ValidationFailed("location_id is required (or provide an inbound shipment)")
        await self._warehouse(location_id, by_location=True)
        order_id = uuid.uuid4()
        number = await self._next_number("RCV")
        await self._exec(
            """
            INSERT INTO wms.receiving_order
                (id, tenant_id, receiving_order_number, inbound_shipment_id, purchase_order_id, location_id,
                 status, deferred_posting, memo, created_by, updated_by, created_at, updated_at, version)
            VALUES (:id, :tenant_id, :number, :shipment, :po, :location, 'CREATED', :deferred, :memo,
                    :actor, :actor, now(), now(), 1)
            """,
            id=order_id,
            number=number,
            shipment=_as_uuid(data.get("inbound_shipment_id")),
            po=_as_uuid(data.get("purchase_order_id") or (shipment or {}).get("purchase_order_id")),
            location=location_id,
            deferred=bool(data.get("deferred_posting", False)),
            memo=data.get("memo"),
            actor=_as_uuid(actor_id),
        )
        lines = list(data.get("lines") or [])
        if shipment and not lines:
            lines = [
                {"item_id": line["item_id"], "line_number": line["line_number"],
                 "qty_expected": line["qty_expected"], "lot_id": line["lot_id"],
                 "serial_ids": line["serial_ids"], "inventory_status_id": line["inventory_status_id"],
                 "unit_cost": line["unit_cost"], "uom_id": line["uom_id"],
                 "purchase_order_line_id": line["purchase_order_line_id"]}
                for line in shipment["lines"]
            ]
        if not lines:
            raise ValidationFailed("at least one receiving line is required")
        for index, line in enumerate(lines, start=1):
            item_id = _as_uuid(line.get("item_id"))
            if item_id is None:
                raise ValidationFailed(f"line {index}: item_id is required")
            await self._item(item_id)
            await self._exec(
                """
                INSERT INTO wms.receiving_order_line
                    (id, tenant_id, receiving_order_id, line_number, item_id, purchase_order_line_id,
                     qty_expected, qty_received, bin_id, lot_id, serial_ids, inventory_status_id, unit_cost, uom_id)
                VALUES (:id, :tenant_id, :order, :line, :item, :po_line, :expected, :received, :bin, :lot,
                        :serials, :status, :cost, :uom)
                """,
                id=uuid.uuid4(),
                order=order_id,
                line=int(line.get("line_number") or index),
                item=item_id,
                po_line=_as_uuid(line.get("purchase_order_line_id")),
                expected=_qty(line.get("qty_expected")),
                received=_qty(line.get("qty_received", 0)),
                bin=_as_uuid(line.get("bin_id")),
                lot=_as_uuid(line.get("lot_id")),
                serials=[_as_uuid(value) for value in (line.get("serial_ids") or [])] or None,
                status=_as_uuid(line.get("inventory_status_id")),
                cost=_money(line["unit_cost"]) if line.get("unit_cost") is not None else None,
                uom=_as_uuid(line.get("uom_id")),
            )
        await self._audit("receiving_order.created", "wms.receiving_order", order_id,
                          {"receiving_order_number": number}, actor_id)
        result = await self.get_receiving_order(order_id)
        await self._idem_complete("POST /api/v1/receiving-orders", idempotency_key, result, order_id, 201)
        return result

    async def list_receiving_orders(self, *, location_id: Any = None, status: str | None = None,
                                    limit: int = DEFAULT_PAGE_LIMIT) -> list[dict[str, Any]]:
        clauses = ["tenant_id = :tenant_id"]
        params: dict[str, Any] = {}
        if location_id:
            clauses.append("location_id = :location")
            params["location"] = _as_uuid(location_id)
        if status:
            clauses.append("status = :status")
            params["status"] = status.upper()
        return await self._all(
            f"SELECT * FROM wms.receiving_order WHERE {' AND '.join(clauses)} "
            f"ORDER BY created_at DESC LIMIT :limit",
            limit=_limit(limit),
            **params,
        )

    async def get_receiving_order(self, order_id: Any) -> dict[str, Any]:
        row = await self._require_row("receiving_order", order_id, "receiving order")
        row["lines"] = await self._all(
            "SELECT * FROM wms.receiving_order_line WHERE tenant_id = :tenant_id AND receiving_order_id = :id "
            "ORDER BY line_number",
            id=_as_uuid(order_id),
        )
        return row

    async def transition_receiving_order(self, order_id: Any, target: str, *, actor_id: Any = None,
                                         idempotency_key: str | None = None) -> dict[str, Any]:
        await self._require_enabled()
        row = await self._require_row("receiving_order", order_id, "receiving order")
        target = target.upper()
        validate_transition("receiving_order", row["status"], target)
        values: dict[str, Any] = {"status": target}
        if target == "IN_PROGRESS":
            values["started_at"] = datetime.now(UTC)
        await self._exec(
            "UPDATE wms.receiving_order SET status = :status, started_at = COALESCE(:started, started_at), "
            "updated_by = :actor, updated_at = now(), version = version + 1 "
            "WHERE tenant_id = :tenant_id AND id = :id",
            status=target,
            started=values.get("started_at"),
            actor=_as_uuid(actor_id),
            id=_as_uuid(order_id),
        )
        await self._audit(f"receiving_order.{target.lower()}", "wms.receiving_order", order_id, {}, actor_id)
        return await self.get_receiving_order(order_id)

    async def complete_receiving_order(self, order_id: Any, data: Mapping[str, Any], *, actor_id: Any = None,
                                       idempotency_key: str | None = None) -> dict[str, Any]:
        await self._require_enabled()
        order = await self.get_receiving_order(order_id)
        if order["status"] == "COMPLETED":
            return order
        validate_transition("receiving_order", order["status"], "COMPLETED")
        payload_lines = {int(line.get("line_number")): line for line in data.get("lines") or [] if line.get("line_number")}
        receipt_lines: list[dict[str, Any]] = []
        warehouse = await self._warehouse(order["location_id"], by_location=True)
        for line in order["lines"]:
            line_payload = payload_lines.get(int(line["line_number"]), {})
            qty_received = _qty(line_payload.get("qty_received", line["qty_received"]))
            if qty_received <= 0:
                continue
            if qty_received > _qty(line["qty_expected"]) and _qty(line["qty_expected"]) > 0:
                raise ValidationFailed(f"line {line['line_number']}: qty_received exceeds qty_expected")
            bin_id = _as_uuid(line_payload.get("bin_id") or line.get("bin_id") or warehouse.get("receiving_bin_id"))
            if bin_id is None:
                raise ValidationFailed(
                    f"line {line['line_number']}: a receiving bin is required (line bin or warehouse receiving_bin)"
                )
            await self._require_bin(bin_id)
            status_id = _as_uuid(line_payload.get("inventory_status_id") or line.get("inventory_status_id")) \
                or await self._default_status_id()
            receipt_lines.append(
                {
                    "item_id": str(line["item_id"]),
                    "purchase_order_line_id": str(line["purchase_order_line_id"])
                    if line.get("purchase_order_line_id") else None,
                    "qty_received": str(qty_received),
                    "unit_cost": str(_money(line["unit_cost"] or 0)),
                    "location_id": str(order["location_id"]),
                    "inventory_status_id": str(status_id),
                    "bin_id": str(bin_id),
                    "lot_id": str(_as_uuid(line_payload.get("lot_id") or line.get("lot_id")))
                    if (line_payload.get("lot_id") or line.get("lot_id")) else None,
                    "serial_ids": [str(_as_uuid(value)) for value in
                                   (line_payload.get("serial_ids") or line.get("serial_ids") or [])],
                }
            )
        if not receipt_lines:
            raise ValidationFailed("nothing to receive: all lines have qty_received <= 0")
        vendor_id = data.get("vendor_id")
        if vendor_id is None and order.get("inbound_shipment_id"):
            shipment = await self._require_row("inbound_shipment", order["inbound_shipment_id"], "inbound shipment")
            vendor_id = shipment.get("vendor_id")
        deferred = bool(data.get("defer_posting", order["deferred_posting"]))
        procurement = self._procurement()
        receipt = await procurement.create_receipt(
            {
                "purchase_order_id": order.get("purchase_order_id"),
                "vendor_id": str(vendor_id) if vendor_id else None,
                "receipt_type": "INBOUND_SHIPMENT",
                "location_id": str(order["location_id"]),
                "deferred_posting": deferred,
                "received_at": str(data.get("received_at") or datetime.now(UTC)),
                "memo": data.get("memo") or f"WMS receiving order {order['receiving_order_number']}",
                "lines": [
                    {
                        "item_id": line["item_id"],
                        "purchase_order_line_id": line.get("purchase_order_line_id"),
                        "qty_received": line["qty_received"],
                        "unit_cost": line["unit_cost"],
                        "location_id": line["location_id"],
                        "inventory_status_id": line["inventory_status_id"],
                        "details": [
                            {
                                "bin_id": line["bin_id"],
                                "qty_received": line["qty_received"],
                                "lot_id": line.get("lot_id"),
                                "serial_ids": line.get("serial_ids"),
                                "inventory_status_id": line["inventory_status_id"],
                            }
                        ],
                    }
                    for line in receipt_lines
                ],
            },
            actor_id=actor_id,
            idempotency_key=idempotency_key,
        )
        if not deferred:
            await procurement.post_receipt(receipt["id"], actor_id=actor_id)
            receipt = await procurement.get_receipt(receipt["id"])
        # Persist what was actually received so putaway generation/reversal read the executed quantities.
        received_by_line = {
            line["line_number"]: line["qty_received"] for line in receipt.get("lines") or []
        }
        for order_line in order["lines"]:
            received_qty = received_by_line.get(int(order_line["line_number"]))
            if received_qty is None:
                continue
            payload_line = payload_lines.get(int(order_line["line_number"]), {})
            await self._exec(
                "UPDATE wms.receiving_order_line SET qty_received = :qty, "
                "bin_id = COALESCE(:bin, bin_id), updated_at = now(), version = version + 1 "
                "WHERE tenant_id = :tenant_id AND id = :id",
                qty=_qty(received_qty),
                bin=_as_uuid(payload_line.get("bin_id")),
                id=_as_uuid(order_line["id"]),
            )
        await self._exec(
            "UPDATE wms.receiving_order SET status = 'COMPLETED', completed_at = now(), item_receipt_id = :receipt, "
            "updated_by = :actor, updated_at = now(), version = version + 1 "
            "WHERE tenant_id = :tenant_id AND id = :id",
            receipt=_as_uuid(receipt["id"]),
            actor=_as_uuid(actor_id),
            id=_as_uuid(order_id),
        )
        if order.get("inbound_shipment_id"):
            await self._exec(
                "UPDATE wms.inbound_shipment SET status = 'RECEIVED', received_at = now(), updated_at = now(), "
                "version = version + 1 WHERE tenant_id = :tenant_id AND id = :id",
                id=_as_uuid(order["inbound_shipment_id"]),
            )
        generated = await self.generate_putaway_tasks(
            {"receiving_order_id": str(order_id)}, actor_id=actor_id
        ) if bool(data.get("generate_putaway", True)) else {"task_ids": []}
        await self._audit("receiving_order.completed", "wms.receiving_order", order_id,
                          {"item_receipt_id": receipt["id"], "deferred": deferred,
                           "putaway_tasks": len(generated.get("task_ids") or [])}, actor_id)
        await self._emit("wms.receiving.completed", "receiving_order", order_id,
                         {"tenant_id": str(self.tenant_id), "receiving_order_id": str(order_id),
                          "item_receipt_id": receipt["id"], "receipt_status": receipt["status"],
                          "location_id": str(order["location_id"])})
        result = await self.get_receiving_order(order_id)
        result["item_receipt"] = receipt
        result["putaway_task_ids"] = generated.get("task_ids") or []
        return result

    async def reverse_receiving_order(self, order_id: Any, data: Mapping[str, Any], *, actor_id: Any = None,
                                      idempotency_key: str | None = None) -> dict[str, Any]:
        await self._require_enabled()
        order = await self.get_receiving_order(order_id)
        if not order.get("item_receipt_id"):
            raise InvalidStateTransition("receiving order has no posted receipt to reverse")
        procurement = self._procurement()
        reason = str(data.get("reason") or "WMS receiving reversal")
        reversed_transactions: list[str] = []
        open_tasks = await self._all(
            "SELECT * FROM wms.putaway_task WHERE tenant_id = :tenant_id AND receiving_order_line_id IN "
            "(SELECT id FROM wms.receiving_order_line WHERE tenant_id = :tenant_id AND receiving_order_id = :order)",
            order=_as_uuid(order_id),
        )
        for task in open_tasks:
            if task["status"] in OPEN_TASK_STATUSES:
                await self._exec(
                    "UPDATE wms.putaway_task SET status = 'CANCELLED', updated_at = now(), version = version + 1 "
                    "WHERE tenant_id = :tenant_id AND id = :id AND status = ANY(:statuses)",
                    id=_as_uuid(task["id"]),
                    statuses=list(OPEN_TASK_STATUSES),
                )
            elif task.get("posted_transaction_id"):
                try:
                    reversal = await self._posting().reverse(
                        str(task["posted_transaction_id"]),
                        reason=reason,
                        idempotency_key=f"wms-putaway-reverse:{task['id']}",
                    )
                    reversed_transactions.append(reversal["transaction_id"])
                except Exception:  # noqa: BLE001 - reversal of a completed putaway is best-effort compensating
                    pass
        receipt = await procurement.reverse_receipt(order["item_receipt_id"], reason,
                                                    actor_id=actor_id, idempotency_key=idempotency_key)
        await self._audit("receiving_order.reversed", "wms.receiving_order", order_id,
                          {"item_receipt_id": order["item_receipt_id"],
                           "putaway_reversals": reversed_transactions}, actor_id)
        await self._emit("wms.receiving.reversed", "receiving_order", order_id,
                         {"tenant_id": str(self.tenant_id), "receiving_order_id": str(order_id),
                          "item_receipt_id": order["item_receipt_id"],
                          "reversed_transaction_ids": reversed_transactions})
        result = await self.get_receiving_order(order_id)
        result["receipt_reversal"] = receipt
        return result

    async def generate_putaway_tasks(self, data: Mapping[str, Any], *, actor_id: Any = None,
                                     idempotency_key: str | None = None) -> dict[str, Any]:
        await self._require_enabled()
        replay = await self._idem_begin("POST /api/v1/putaway-tasks/generate", idempotency_key, data, actor_id)
        if replay:
            return replay["replay"]
        order_id = _as_uuid(data.get("receiving_order_id"))
        if order_id is None:
            raise ValidationFailed("receiving_order_id is required")
        order = await self.get_receiving_order(order_id)
        warehouse = await self._warehouse(order["location_id"], by_location=True)
        staging_rule = await self._system_rule_enabled(warehouse["id"], "STAGING_AT_RECEIVING")
        staging_bin = _as_uuid(warehouse.get("staging_bin_id")) if staging_rule else None
        receiving_bin = _as_uuid(warehouse.get("receiving_bin_id"))
        strategy = await self._one(
            "SELECT * FROM wms.putaway_strategy WHERE tenant_id = :tenant_id AND is_active "
            "ORDER BY priority, created_at LIMIT 1"
        )
        rules = await self._all(
            "SELECT * FROM wms.putaway_strategy_rule WHERE tenant_id = :tenant_id AND is_active ORDER BY sequence"
        ) if strategy else []
        task_ids: list[str] = []
        for line in order["lines"]:
            if _qty(line["qty_received"]) <= 0:
                continue
            existing = await self._one(
                "SELECT id FROM wms.putaway_task WHERE tenant_id = :tenant_id AND receiving_order_line_id = :line "
                "LIMIT 1",
                line=_as_uuid(line["id"]),
            )
            if existing is not None:
                continue
            item = await self._item(line["item_id"])
            group = await self._one(
                "SELECT item_process_group_id FROM wms.item_process_assignment "
                "WHERE tenant_id = :tenant_id AND item_id = :item AND (location_id IS NULL OR location_id = :location) "
                "ORDER BY location_id NULLS LAST LIMIT 1",
                item=_as_uuid(line["item_id"]),
                location=_as_uuid(order["location_id"]),
            )
            candidates = await self._all(
                "SELECT b.id AS bin_id, b.code AS bin_code, b.zone_id, b.bin_type FROM traceability.bin b "
                "JOIN traceability.zone z ON z.tenant_id = b.tenant_id AND z.id = b.zone_id "
                "WHERE b.tenant_id = :tenant_id AND b.location_id = :location AND b.is_active "
                "AND b.bin_type IN ('STORAGE','PICKING') ORDER BY z.priority, b.code",
                location=_as_uuid(order["location_id"]),
            )
            on_hand = {}
            for bin_row in candidates:
                value = await self._scalar(
                    "SELECT COALESCE(sum(qty_on_hand), 0) FROM inventory_ledger.inventory_balance_bin "
                    "WHERE tenant_id = :tenant_id AND bin_id = :bin",
                    bin=_as_uuid(bin_row["bin_id"]),
                )
                on_hand[str(bin_row["bin_id"])] = value
            resolved = strategies.resolve_putaway_bin(
                rules,
                candidates,
                item_process_group_id=(group or {}).get("item_process_group_id"),
                qty=line["qty_received"],
                on_hand_by_bin=on_hand,
            )
            if resolved is None and candidates:
                resolved = candidates[0]
            task_id = uuid.uuid4()
            number = await self._next_number("TASK")
            await self._exec(
                """
                INSERT INTO wms.putaway_task
                    (id, tenant_id, task_number, receiving_order_line_id, item_id, location_id, source_bin_id,
                     suggested_bin_id, qty, uom_id, strategy_id, status, is_cart_putaway, created_by, updated_by,
                     created_at, updated_at, version)
                VALUES (:id, :tenant_id, :number, :line, :item, :location, :source, :suggested, :qty, :uom,
                        :strategy, 'PENDING', false, :actor, :actor, now(), now(), 1)
                """,
                id=task_id,
                number=number,
                line=_as_uuid(line["id"]),
                item=_as_uuid(line["item_id"]),
                location=_as_uuid(order["location_id"]),
                source=_as_uuid(staging_bin or line.get("bin_id") or receiving_bin),
                suggested=_as_uuid(resolved["bin_id"]) if resolved else None,
                qty=_qty(line["qty_received"]),
                uom=_as_uuid(line.get("uom_id")) or _as_uuid(item["base_uom_id"]),
                strategy=_as_uuid(strategy["id"]) if strategy else None,
                actor=_as_uuid(actor_id),
            )
            await self._exec(
                """
                INSERT INTO wms.putaway_task_line
                    (id, tenant_id, putaway_task_id, line_number, bin_id, qty, lot_id, serial_ids,
                     inventory_status_id, ownership)
                VALUES (:id, :tenant_id, :task, 1, :bin, :qty, :lot, :serials, :status, 'OWNED')
                """,
                id=uuid.uuid4(),
                task=task_id,
                bin=_as_uuid(resolved["bin_id"]) if resolved else _as_uuid(line.get("bin_id") or receiving_bin),
                qty=_qty(line["qty_received"]),
                lot=_as_uuid(line.get("lot_id")),
                serials=[_as_uuid(value) for value in (line.get("serial_ids") or [])] or None,
                status=_as_uuid(line.get("inventory_status_id")) or await self._default_status_id(),
            )
            task_ids.append(str(task_id))
            await self._emit("wms.task.created", "putaway_task", task_id,
                             {"tenant_id": str(self.tenant_id), "task_type": "PUTAWAY", "task_id": str(task_id),
                              "warehouse_id": str(warehouse["id"]), "item_id": str(line["item_id"]),
                              "qty": str(_qty(line["qty_received"]))})
        await self._audit("putaway_tasks.generated", "wms.putaway_task", order_id,
                          {"receiving_order_id": str(order_id), "tasks": len(task_ids)}, actor_id)
        result = {"receiving_order_id": str(order_id), "task_ids": task_ids, "created": len(task_ids)}
        await self._idem_complete("POST /api/v1/putaway-tasks/generate", idempotency_key, result, order_id, 201)
        return result

    async def _system_rule_enabled(self, warehouse_id: Any, code: str) -> bool:
        row = await self._one(
            "SELECT COALESCE(s.is_enabled, r.default_enabled) AS enabled FROM wms.system_rule r "
            "LEFT JOIN wms.system_rule_setting s ON s.tenant_id = r.tenant_id AND s.system_rule_id = r.id "
            "AND s.warehouse_id = :warehouse WHERE r.tenant_id = :tenant_id AND r.code = :code",
            warehouse=_as_uuid(warehouse_id),
            code=code,
        )
        return bool(row and row["enabled"])

    # -- putaway tasks ----------------------------------------------------------------------
    async def list_putaway_tasks(self, *, status: str | None = None, warehouse_id: Any = None,
                                 assigned_user_id: Any = None, limit: int = DEFAULT_PAGE_LIMIT) -> list[dict[str, Any]]:
        clauses = ["t.tenant_id = :tenant_id"]
        params: dict[str, Any] = {}
        if status:
            clauses.append("t.status = :status")
            params["status"] = status.upper()
        if warehouse_id:
            clauses.append("t.location_id = :location")
            params["location"] = _as_uuid(warehouse_id)
        if assigned_user_id:
            clauses.append("t.assigned_user_id = :user")
            params["user"] = _as_uuid(assigned_user_id)
        return await self._all(
            f"SELECT t.*, l.code AS location_code FROM wms.putaway_task t "
            f"JOIN traceability.location l ON l.tenant_id = t.tenant_id AND l.id = t.location_id "
            f"WHERE {' AND '.join(clauses)} ORDER BY t.created_at DESC LIMIT :limit",
            limit=_limit(limit),
            **params,
        )

    async def get_putaway_task(self, task_id: Any) -> dict[str, Any]:
        row = await self._require_row("putaway_task", task_id, "putaway task")
        row["lines"] = await self._all(
            "SELECT * FROM wms.putaway_task_line WHERE tenant_id = :tenant_id AND putaway_task_id = :id "
            "ORDER BY line_number",
            id=_as_uuid(task_id),
        )
        return row

    async def assign_putaway_task(self, task_id: Any, data: Mapping[str, Any], *, actor_id: Any = None,
                                  idempotency_key: str | None = None) -> dict[str, Any]:
        await self._require_enabled()
        task = await self._require_row("putaway_task", task_id, "putaway task")
        validate_transition("putaway_task", task["status"], "ASSIGNED")
        user_id = _as_uuid(data.get("assigned_user_id")) or self._current_actor(actor_id)
        cart_id = _as_uuid(data.get("cart_id"))
        if bool(task.get("is_cart_putaway")) and cart_id is None:
            raise ValidationFailed("cart_id is required for cart putaway tasks")
        await self._exec(
            "UPDATE wms.putaway_task SET status = 'ASSIGNED', assigned_user_id = :user, cart_id = COALESCE(:cart, cart_id), "
            "updated_by = :actor, updated_at = now(), version = version + 1 "
            "WHERE tenant_id = :tenant_id AND id = :id",
            user=user_id,
            cart=cart_id,
            actor=_as_uuid(actor_id),
            id=_as_uuid(task_id),
        )
        await self._audit("putaway_task.assigned", "wms.putaway_task", task_id, {"assigned_user_id": str(user_id)}, actor_id)
        await self._emit("wms.task.assigned", "putaway_task", task_id,
                         {"tenant_id": str(self.tenant_id), "task_type": "PUTAWAY", "task_id": str(task_id),
                          "assigned_user_id": str(user_id)})
        return await self.get_putaway_task(task_id)

    async def start_putaway_task(self, task_id: Any, *, actor_id: Any = None,
                                 idempotency_key: str | None = None) -> dict[str, Any]:
        await self._require_enabled()
        task = await self._require_row("putaway_task", task_id, "putaway task")
        validate_transition("putaway_task", task["status"], "IN_PROGRESS")
        await self._exec(
            "UPDATE wms.putaway_task SET status = 'IN_PROGRESS', updated_by = :actor, updated_at = now(), "
            "version = version + 1 WHERE tenant_id = :tenant_id AND id = :id",
            actor=_as_uuid(actor_id),
            id=_as_uuid(task_id),
        )
        await self._audit("putaway_task.started", "wms.putaway_task", task_id, {}, actor_id)
        return await self.get_putaway_task(task_id)

    async def complete_putaway_task(self, task_id: Any, data: Mapping[str, Any], *, actor_id: Any = None,
                                    idempotency_key: str | None = None) -> dict[str, Any]:
        await self._require_enabled()
        task = await self.get_putaway_task(task_id)
        if task["status"] == "COMPLETED":
            return task
        validate_transition("putaway_task", task["status"], "COMPLETED")
        payload_lines = {int(line.get("line_number", index)): line
                         for index, line in enumerate(data.get("lines") or [], start=1)}
        movements: list[dict[str, Any]] = []
        total = ZERO
        item = await self._item(task["item_id"])
        uom_id = _as_uuid(task.get("uom_id")) or _as_uuid(item["base_uom_id"])
        for line in task["lines"]:
            payload = payload_lines.get(int(line["line_number"]), {})
            destination = _as_uuid(payload.get("bin_id") or line["bin_id"])
            qty = _qty(payload.get("qty", line["qty"]))
            if destination is None:
                raise ValidationFailed(f"line {line['line_number']}: destination bin is required")
            await self._require_bin(destination)
            source_bin = _as_uuid(task.get("source_bin_id"))
            status_id = _as_uuid(line["inventory_status_id"]) or await self._default_status_id()
            if _as_uuid(task.get("suggested_bin_id")) and destination != _as_uuid(task.get("suggested_bin_id")):
                # Directed putaway allows a controlled override; the actual bin is recorded for evidence.
                pass
            movements.append(
                {
                    "item_id": str(task["item_id"]),
                    "location_id": str(task["location_id"]),
                    "inventory_status_id": str(status_id),
                    "qty": str(-qty),
                    "bin_id": str(source_bin) if source_bin else None,
                    "lot_id": str(_as_uuid(line["lot_id"])) if line.get("lot_id") else None,
                    "uom_id": str(uom_id),
                }
            )
            movements.append(
                {
                    "item_id": str(task["item_id"]),
                    "location_id": str(task["location_id"]),
                    "inventory_status_id": str(status_id),
                    "qty": str(qty),
                    "bin_id": str(destination),
                    "lot_id": str(_as_uuid(line["lot_id"])) if line.get("lot_id") else None,
                    "uom_id": str(uom_id),
                }
            )
            total += qty
        if not movements:
            raise ValidationFailed("putaway task has no lines to complete")
        result = await self._posting().post_bin_transfer(
            transaction_date=_date(data.get("transaction_date")) or datetime.now(UTC).date(),
            source_doc_type="WMS_PUTAWAY_TASK",
            source_doc_id=_as_uuid(task_id),
            movements=movements,
            idempotency_key=idempotency_key or f"wms-putaway:{task_id}",
        )
        await self._exec(
            "UPDATE wms.putaway_task SET status = 'COMPLETED', actual_bin_id = :bin, completed_at = now(), "
            "posted_transaction_id = :txn, updated_by = :actor, updated_at = now(), version = version + 1 "
            "WHERE tenant_id = :tenant_id AND id = :id",
            bin=_as_uuid(payload_lines.get(next(iter(payload_lines), 0), {}).get("bin_id")) if payload_lines else None,
            txn=_as_uuid(result["transaction_id"]),
            actor=_as_uuid(actor_id),
            id=_as_uuid(task_id),
        )
        await self._audit("putaway_task.completed", "wms.putaway_task", task_id,
                          {"qty": str(total), "inventory_transaction_id": result["transaction_id"]}, actor_id)
        await self._emit("wms.task.completed", "putaway_task", task_id,
                         {"tenant_id": str(self.tenant_id), "task_type": "PUTAWAY", "task_id": str(task_id),
                          "posted_transaction_id": result["transaction_id"], "qty": str(total)})
        completed = await self.get_putaway_task(task_id)
        completed["posted_transaction_id"] = result["transaction_id"]
        return completed

    async def exception_putaway_task(self, task_id: Any, data: Mapping[str, Any], *, actor_id: Any = None,
                                     idempotency_key: str | None = None) -> dict[str, Any]:
        await self._require_enabled()
        task = await self._require_row("putaway_task", task_id, "putaway task")
        if task["status"] not in OPEN_TASK_STATUSES:
            raise TaskConflict("only open tasks can be put into exception")
        reason = str(data.get("reason_code") or data.get("reason") or "EXCEPTION")
        await self._exec(
            "UPDATE wms.putaway_task SET status = 'EXCEPTION', exception_reason = :reason, updated_by = :actor, "
            "updated_at = now(), version = version + 1 WHERE tenant_id = :tenant_id AND id = :id",
            reason=reason,
            actor=_as_uuid(actor_id),
            id=_as_uuid(task_id),
        )
        await self._audit("putaway_task.exception", "wms.putaway_task", task_id, {"reason": reason}, actor_id)
        await self._emit("wms.task.exception", "putaway_task", task_id,
                         {"tenant_id": str(self.tenant_id), "task_type": "PUTAWAY", "task_id": str(task_id),
                          "reason": reason})
        return await self.get_putaway_task(task_id)

    async def cancel_putaway_task(self, task_id: Any, data: Mapping[str, Any], *, actor_id: Any = None,
                                  idempotency_key: str | None = None) -> dict[str, Any]:
        await self._require_enabled()
        task = await self._require_row("putaway_task", task_id, "putaway task")
        validate_transition("putaway_task", task["status"], "CANCELLED")
        await self._exec(
            "UPDATE wms.putaway_task SET status = 'CANCELLED', updated_by = :actor, updated_at = now(), "
            "version = version + 1 WHERE tenant_id = :tenant_id AND id = :id",
            actor=_as_uuid(actor_id),
            id=_as_uuid(task_id),
        )
        await self._audit("putaway_task.cancelled", "wms.putaway_task", task_id,
                          {"reason": data.get("reason")}, actor_id)
        return await self.get_putaway_task(task_id)

    def _current_actor(self, actor_id: Any) -> uuid.UUID:
        return _as_uuid(actor_id) or uuid.uuid4()

    # ====================================================================== outbound ---------
    async def create_wave(self, data: Mapping[str, Any], *, actor_id: Any = None,
                          idempotency_key: str | None = None) -> dict[str, Any]:
        await self._require_enabled()
        replay = await self._idem_begin("POST /api/v1/waves", idempotency_key, data, actor_id)
        if replay:
            return replay["replay"]
        location_id = _as_uuid(data.get("location_id"))
        if location_id is None:
            raise ValidationFailed("location_id is required")
        await self._warehouse(location_id, by_location=True)
        wave_id = uuid.uuid4()
        number = await self._next_number("WAVE")
        await self._exec(
            """
            INSERT INTO wms.wave
                (id, tenant_id, wave_number, location_id, status, release_schedule_id, created_by, updated_by,
                 created_at, updated_at, version)
            VALUES (:id, :tenant_id, :number, :location, 'CREATED', :schedule, :actor, :actor, now(), now(), 1)
            """,
            id=wave_id,
            number=number,
            location=location_id,
            schedule=_as_uuid(data.get("release_schedule_id")),
            actor=_as_uuid(actor_id),
        )
        lines = list(data.get("lines") or [])
        if not lines:
            raise ValidationFailed("at least one wave line is required")
        seen: set[tuple[str, str, str]] = set()
        for index, line in enumerate(lines, start=1):
            source_type = str(line.get("source_type") or "SALES_ORDER").upper()
            if source_type not in ("SALES_ORDER", "TRANSFER_ORDER", "WORK_ORDER", "VENDOR_RETURN"):
                raise ValidationFailed(f"line {index}: invalid source_type")
            source_ref = str(line.get("source_ref") or "").strip()
            if not source_ref:
                raise ValidationFailed(f"line {index}: source_ref is required")
            item_id = _as_uuid(line.get("item_id"))
            if item_id is None:
                raise ValidationFailed(f"line {index}: item_id is required")
            await self._item(item_id)
            natural = (source_type, source_ref, str(line.get("source_line_ref") or ""))
            if natural in seen:
                raise DuplicateDocument(f"line {index}: duplicate wave source line")
            seen.add(natural)
            await self._exec(
                """
                INSERT INTO wms.wave_line
                    (id, tenant_id, wave_id, line_number, source_type, source_ref, source_line_ref, item_id,
                     qty, uom_id, priority, status)
                VALUES (:id, :tenant_id, :wave, :line, :source_type, :source_ref, :source_line, :item, :qty,
                        :uom, :priority, 'INCLUDED')
                """,
                id=uuid.uuid4(),
                wave=wave_id,
                line=index,
                source_type=source_type,
                source_ref=source_ref,
                source_line=str(line.get("source_line_ref") or ""),
                item=item_id,
                qty=_qty(line.get("qty")),
                uom=_as_uuid(line.get("uom_id")),
                priority=int(line.get("priority") or 0),
            )
        await self._exec(
            "UPDATE wms.wave SET line_count = :count, order_count = :orders, updated_at = now(), "
            "version = version + 1 WHERE tenant_id = :tenant_id AND id = :id",
            count=len(lines),
            orders=len({(line.get("source_type"), line.get("source_ref")) for line in lines}),
            id=wave_id,
        )
        await self._audit("wave.created", "wms.wave", wave_id, {"wave_number": number, "lines": len(lines)}, actor_id)
        await self._emit("wms.wave.created", "wave", wave_id,
                         {"tenant_id": str(self.tenant_id), "wave_id": str(wave_id),
                          "location_id": str(location_id), "line_count": len(lines)})
        result = await self.get_wave(wave_id)
        await self._idem_complete("POST /api/v1/waves", idempotency_key, result, wave_id, 201)
        return result

    async def list_waves(self, *, location_id: Any = None, status: str | None = None,
                         limit: int = DEFAULT_PAGE_LIMIT) -> list[dict[str, Any]]:
        clauses = ["tenant_id = :tenant_id"]
        params: dict[str, Any] = {}
        if location_id:
            clauses.append("location_id = :location")
            params["location"] = _as_uuid(location_id)
        if status:
            clauses.append("status = :status")
            params["status"] = status.upper()
        return await self._all(
            f"SELECT * FROM wms.wave WHERE {' AND '.join(clauses)} ORDER BY created_at DESC LIMIT :limit",
            limit=_limit(limit),
            **params,
        )

    async def get_wave(self, wave_id: Any) -> dict[str, Any]:
        row = await self._require_row("wave", wave_id, "wave")
        row["lines"] = await self._all(
            "SELECT * FROM wms.wave_line WHERE tenant_id = :tenant_id AND wave_id = :id ORDER BY line_number",
            id=_as_uuid(wave_id),
        )
        row["tasks"] = await self._all(
            "SELECT id, task_number, status, task_type FROM wms.pick_task WHERE tenant_id = :tenant_id AND wave_id = :id "
            "ORDER BY sequence",
            id=_as_uuid(wave_id),
        )
        return row

    async def release_wave(self, wave_id: Any, data: Mapping[str, Any] | None = None, *, actor_id: Any = None,
                           idempotency_key: str | None = None) -> dict[str, Any]:
        await self._require_enabled()
        wave = await self.get_wave(wave_id)
        if wave["status"] == "RELEASED" or wave["status"] == "IN_PROGRESS":
            return wave
        validate_transition("wave", wave["status"], "RELEASED")
        data = dict(data or {})
        decomposition = strategies.decompose_pick_tasks(wave["lines"])
        task_ids: list[str] = []
        strategy = await self._one(
            "SELECT * FROM wms.pick_strategy WHERE tenant_id = :tenant_id AND is_active "
            "AND (applies_to_location_id IS NULL OR applies_to_location_id = :location) "
            "ORDER BY priority, created_at LIMIT 1",
            location=_as_uuid(wave["location_id"]),
        )
        for group in decomposition:
            task_id = uuid.uuid4()
            number = await self._next_number("TASK")
            task_type = "ORDER"
            if group["group_key"] and group["group_key"][0] == "WORK_ORDER":
                task_type = "WORK_ORDER"
            await self._exec(
                """
                INSERT INTO wms.pick_task
                    (id, tenant_id, task_number, task_type, wave_id, pick_strategy_id, location_id, status,
                     sequence, priority, created_by, updated_by, created_at, updated_at, version)
                VALUES (:id, :tenant_id, :number, :type, :wave, :strategy, :location, 'PENDING', :sequence, 0,
                        :actor, :actor, now(), now(), 1)
                """,
                id=task_id,
                number=number,
                type=task_type,
                wave=_as_uuid(wave_id),
                strategy=_as_uuid(strategy["id"]) if strategy else None,
                location=_as_uuid(wave["location_id"]),
                sequence=len(task_ids) + 1,
                actor=_as_uuid(actor_id),
            )
            for index, line in enumerate(group["lines"], start=1):
                await self._exec(
                    """
                    INSERT INTO wms.pick_task_line
                        (id, tenant_id, pick_task_id, line_number, item_id, source_ref, source_line_ref,
                         qty_requested, qty_picked, qty_short, uom_id, suggested_bin_id, inventory_status_id,
                         ownership, status)
                    VALUES (:id, :tenant_id, :task, :line, :item, :source_ref, :source_line, :requested, 0,
                            :requested, :uom, :bin, :status, 'OWNED', 'PENDING')
                    """,
                    id=uuid.uuid4(),
                    task=task_id,
                    line=index,
                    item=_as_uuid(line["item_id"]),
                    source_ref=str(line["source_ref"]),
                    source_line=str(line.get("source_line_ref") or ""),
                    requested=_qty(line["qty"]),
                    uom=_as_uuid(line.get("uom_id")),
                    bin=_as_uuid(data.get("suggested_bin_id")),
                    status=await self._default_status_id(),
                )
                await self._exec(
                    "UPDATE wms.wave_line SET status = 'RELEASED', pick_task_id = :task, updated_at = now(), "
                    "version = version + 1 WHERE tenant_id = :tenant_id AND id = :line",
                    task=task_id,
                    line=_as_uuid(line["id"]),
                )
            task_ids.append(str(task_id))
        await self._exec(
            "UPDATE wms.wave SET status = 'RELEASED', released_at = now(), updated_by = :actor, updated_at = now(), "
            "version = version + 1 WHERE tenant_id = :tenant_id AND id = :id",
            actor=_as_uuid(actor_id),
            id=_as_uuid(wave_id),
        )
        await self._audit("wave.released", "wms.wave", wave_id, {"pick_tasks": len(task_ids)}, actor_id)
        await self._emit("wms.wave.released", "wave", wave_id,
                         {"tenant_id": str(self.tenant_id), "wave_id": str(wave_id),
                          "pick_task_ids": task_ids})
        result = await self.get_wave(wave_id)
        result["pick_task_ids"] = task_ids
        return result

    async def cancel_wave(self, wave_id: Any, data: Mapping[str, Any] | None = None, *, actor_id: Any = None,
                          idempotency_key: str | None = None) -> dict[str, Any]:
        await self._require_enabled()
        wave = await self.get_wave(wave_id)
        validate_transition("wave", wave["status"], "CANCELLED")
        for task in wave["tasks"]:
            if task["status"] not in {"PENDING", "ASSIGNED"}:
                raise WaveIncomplete(
                    "cannot cancel a wave with tasks in progress/completed",
                    details={"task_id": str(task["id"]), "status": task["status"]},
                )
        await self._exec(
            "UPDATE wms.pick_task SET status = 'CANCELLED', updated_at = now(), version = version + 1 "
            "WHERE tenant_id = :tenant_id AND wave_id = :id AND status IN ('PENDING','ASSIGNED')",
            id=_as_uuid(wave_id),
        )
        await self._exec(
            "UPDATE wms.wave SET status = 'CANCELLED', updated_by = :actor, updated_at = now(), "
            "version = version + 1 WHERE tenant_id = :tenant_id AND id = :id",
            actor=_as_uuid(actor_id),
            id=_as_uuid(wave_id),
        )
        await self._audit("wave.cancelled", "wms.wave", wave_id, {"reason": (data or {}).get("reason")}, actor_id)
        await self._emit("wms.wave.cancelled", "wave", wave_id,
                         {"tenant_id": str(self.tenant_id), "wave_id": str(wave_id)})
        return await self.get_wave(wave_id)

    # -- wave release schedules -------------------------------------------------------------
    async def create_wave_release_schedule(self, data: Mapping[str, Any], *, actor_id: Any = None,
                                           idempotency_key: str | None = None) -> dict[str, Any]:
        await self._require_enabled()
        replay = await self._idem_begin("POST /api/v1/wave-release-schedules", idempotency_key, data, actor_id)
        if replay:
            return replay["replay"]
        schedule_id = uuid.uuid4()
        await self._exec(
            """
            INSERT INTO wms.wave_release_schedule
                (id, tenant_id, code, name, cron_expr, filter, location_id, is_active, next_run_at,
                 created_by, updated_by, created_at, updated_at, version)
            VALUES (:id, :tenant_id, :code, :name, :cron, CAST(:filter AS jsonb), :location, :active,
                    :next_run, :actor, :actor, now(), now(), 1)
            """,
            id=schedule_id,
            code=str(data.get("code") or "").strip(),
            name=str(data.get("name") or data.get("code") or "").strip(),
            cron=str(data.get("cron_expr") or data.get("cron") or "").strip(),
            filter=json.dumps(_jsonable(data.get("filter") or {})),
            location=_as_uuid(data.get("location_id")),
            active=bool(data.get("is_active", True)),
            next_run=_stamp(data.get("next_run_at")),
            actor=_as_uuid(actor_id),
        )
        await self._audit("wave_release_schedule.created", "wms.wave_release_schedule", schedule_id,
                          {"code": data.get("code")}, actor_id)
        result = await self._row_by_id(t.wave_release_schedule, schedule_id)
        await self._idem_complete("POST /api/v1/wave-release-schedules", idempotency_key, result, schedule_id, 201)
        return result

    async def list_wave_release_schedules(self, *, location_id: Any = None, is_active: bool | None = None,
                                          limit: int = DEFAULT_PAGE_LIMIT) -> list[dict[str, Any]]:
        clauses = ["tenant_id = :tenant_id"]
        params: dict[str, Any] = {}
        if location_id:
            clauses.append("location_id = :location")
            params["location"] = _as_uuid(location_id)
        if is_active is not None:
            clauses.append("is_active = :active")
            params["active"] = bool(is_active)
        return await self._all(
            f"SELECT * FROM wms.wave_release_schedule WHERE {' AND '.join(clauses)} ORDER BY code LIMIT :limit",
            limit=_limit(limit),
            **params,
        )

    async def update_wave_release_schedule(self, schedule_id: Any, data: Mapping[str, Any], *,
                                           actor_id: Any = None,
                                           expected_version: int | None = None) -> dict[str, Any]:
        await self._require_enabled()
        schedule = await self._require_row("wave_release_schedule", schedule_id, "wave release schedule")
        if expected_version is not None and int(schedule["version"]) != int(expected_version):
            raise StaleVersion("schedule was modified; reload and retry")
        allowed = {"name", "cron_expr", "filter", "is_active", "next_run_at"}
        updates = {key: value for key, value in data.items() if key in allowed}
        if updates:
            sets = []
            params: dict[str, Any] = {"id": _as_uuid(schedule_id), "actor": _as_uuid(actor_id)}
            for index, (key, value) in enumerate(updates.items()):
                if key == "filter":
                    sets.append(f"filter = CAST(:p{index} AS jsonb)")
                    params[f"p{index}"] = json.dumps(_jsonable(value))
                elif key == "next_run_at":
                    sets.append(f"next_run_at = :p{index}")
                    params[f"p{index}"] = _stamp(value)
                else:
                    sets.append(f"{key} = :p{index}")
                    params[f"p{index}"] = value
            await self._exec(
                f"UPDATE wms.wave_release_schedule SET {', '.join(sets)}, updated_by = :actor, updated_at = now(), "
                f"version = version + 1 WHERE tenant_id = :tenant_id AND id = :id",
                **params,
            )
            await self._audit("wave_release_schedule.updated", "wms.wave_release_schedule", schedule_id,
                              updates, actor_id)
        return await self._row_by_id(t.wave_release_schedule, schedule_id)

    async def run_wave_release_schedule(self, schedule_id: Any, data: Mapping[str, Any] | None = None, *,
                                        actor_id: Any = None,
                                        idempotency_key: str | None = None) -> dict[str, Any]:
        await self._require_enabled()
        schedule = await self._require_row("wave_release_schedule", schedule_id, "wave release schedule")
        if not schedule["is_active"]:
            raise InvalidStateTransition("wave release schedule is inactive")
        # The actual order-demand selection is external (D3): this run records the schedule execution and
        # releases any CREATED waves matching the schedule filter/location.
        waves = await self._all(
            "SELECT id FROM wms.wave WHERE tenant_id = :tenant_id AND location_id = :location AND status = 'CREATED'",
            location=_as_uuid(schedule["location_id"]),
        )
        released: list[str] = []
        for wave in waves:
            result = await self.release_wave(wave["id"], actor_id=actor_id)
            released.append(str(result["id"]))
        await self._exec(
            "UPDATE wms.wave_release_schedule SET last_run_at = now(), updated_at = now(), version = version + 1 "
            "WHERE tenant_id = :tenant_id AND id = :id",
            id=_as_uuid(schedule_id),
        )
        await self._audit("wave_release_schedule.run", "wms.wave_release_schedule", schedule_id,
                          {"waves": len(released)}, actor_id)
        return {"schedule_id": str(schedule_id), "released_wave_ids": released}

    # -- pick tasks -------------------------------------------------------------------------
    async def list_pick_tasks(self, *, wave_id: Any = None, status: str | None = None, task_type: str | None = None,
                              assigned_user_id: Any = None, limit: int = DEFAULT_PAGE_LIMIT) -> list[dict[str, Any]]:
        clauses = ["tenant_id = :tenant_id"]
        params: dict[str, Any] = {}
        if wave_id:
            clauses.append("wave_id = :wave")
            params["wave"] = _as_uuid(wave_id)
        if status:
            clauses.append("status = :status")
            params["status"] = status.upper()
        if task_type:
            clauses.append("task_type = :type")
            params["type"] = task_type.upper()
        if assigned_user_id:
            clauses.append("assigned_user_id = :user")
            params["user"] = _as_uuid(assigned_user_id)
        return await self._all(
            f"SELECT * FROM wms.pick_task WHERE {' AND '.join(clauses)} ORDER BY sequence, created_at "
            f"LIMIT :limit",
            limit=_limit(limit),
            **params,
        )

    async def get_pick_task(self, task_id: Any) -> dict[str, Any]:
        row = await self._require_row("pick_task", task_id, "pick task")
        row["lines"] = await self._all(
            "SELECT * FROM wms.pick_task_line WHERE tenant_id = :tenant_id AND pick_task_id = :id "
            "ORDER BY line_number",
            id=_as_uuid(task_id),
        )
        return row

    async def assign_pick_task(self, task_id: Any, data: Mapping[str, Any], *, actor_id: Any = None,
                               idempotency_key: str | None = None) -> dict[str, Any]:
        await self._require_enabled()
        task = await self._require_row("pick_task", task_id, "pick task")
        validate_transition("pick_task", task["status"], "ASSIGNED")
        user_id = _as_uuid(data.get("assigned_user_id")) or self._current_actor(actor_id)
        await self._exec(
            "UPDATE wms.pick_task SET status = 'ASSIGNED', assigned_user_id = :user, "
            "cart_id = COALESCE(:cart, cart_id), updated_by = :actor, updated_at = now(), version = version + 1 "
            "WHERE tenant_id = :tenant_id AND id = :id",
            user=user_id,
            cart=_as_uuid(data.get("cart_id")),
            actor=_as_uuid(actor_id),
            id=_as_uuid(task_id),
        )
        await self._audit("pick_task.assigned", "wms.pick_task", task_id, {"assigned_user_id": str(user_id)}, actor_id)
        await self._emit("wms.task.assigned", "pick_task", task_id,
                         {"tenant_id": str(self.tenant_id), "task_type": "PICK", "task_id": str(task_id),
                          "assigned_user_id": str(user_id)})
        return await self.get_pick_task(task_id)

    async def start_pick_task(self, task_id: Any, *, actor_id: Any = None,
                              idempotency_key: str | None = None) -> dict[str, Any]:
        await self._require_enabled()
        task = await self._require_row("pick_task", task_id, "pick task")
        validate_transition("pick_task", task["status"], "IN_PROGRESS")
        await self._exec(
            "UPDATE wms.pick_task SET status = 'IN_PROGRESS', updated_by = :actor, updated_at = now(), "
            "version = version + 1 WHERE tenant_id = :tenant_id AND id = :id",
            actor=_as_uuid(actor_id),
            id=_as_uuid(task_id),
        )
        await self._audit("pick_task.started", "wms.pick_task", task_id, {}, actor_id)
        return await self.get_pick_task(task_id)

    async def complete_pick_task(self, task_id: Any, data: Mapping[str, Any], *, actor_id: Any = None,
                                 idempotency_key: str | None = None) -> dict[str, Any]:
        await self._require_enabled()
        task = await self.get_pick_task(task_id)
        if task["status"] in {"PICKED", "SHORT", "PARTIAL", "REVERSED"} and task.get("posted_transaction_id"):
            task = dict(task)
            task["posted_transaction_id"] = str(task["posted_transaction_id"])
            task["lines"] = [
                {**line, "qty_requested": str(line["qty_requested"]), "qty_picked": str(line["qty_picked"]),
                 "qty_short": str(line["qty_short"])}
                for line in task["lines"]
            ]
            return task
        payload_lines = {int(line.get("line_number", index)): line
                         for index, line in enumerate(data.get("lines") or [], start=1)}
        movements: list[dict[str, Any]] = []
        line_statuses: list[str] = []
        for line in task["lines"]:
            payload = payload_lines.get(int(line["line_number"]), {})
            requested = _qty(line["qty_requested"])
            picked, short = strategies.pick_quantities(requested, payload.get("qty_picked", requested))
            actual_bin = _as_uuid(payload.get("actual_bin_id") or line.get("actual_bin_id")
                                  or line.get("suggested_bin_id"))
            status_id = _as_uuid(payload.get("inventory_status_id") or line.get("inventory_status_id")) \
                or await self._default_status_id()
            if picked > 0:
                if actual_bin is None:
                    raise ValidationFailed(f"line {line['line_number']}: actual bin is required for a pick")
                if await self._is_bin_blocked(actual_bin):
                    raise BinBlocked("bin is blocked from picking",
                                     details={"bin_id": str(actual_bin), "line": line["line_number"]})
                await self._assert_lot_pickable(line.get("lot_id"))
                movements.append(
                    {
                        "item_id": str(line["item_id"]),
                        "location_id": str(task["location_id"]),
                        "inventory_status_id": str(status_id),
                        "qty": str(-picked),
                        "bin_id": str(actual_bin),
                        "lot_id": str(_as_uuid(line["lot_id"])) if line.get("lot_id") else None,
                        "ownership": "OWNED",
                        "qty_class": "ON_HAND",
                    }
                )
            line_status = "PICKED" if picked == requested else ("ZERO" if picked == 0 else "PARTIAL")
            line_statuses.append(line_status)
            await self._exec(
                "UPDATE wms.pick_task_line SET qty_picked = :picked, qty_short = :short, status = :status, "
                "actual_bin_id = :bin WHERE tenant_id = :tenant_id AND id = :id",
                picked=picked,
                short=short,
                status=line_status,
                bin=actual_bin,
                id=_as_uuid(line["id"]),
            )
        if not line_statuses:
            raise ValidationFailed("pick task has no lines")
        if movements:
            result = await self._posting().post_fulfillment(
                transaction_date=_date(data.get("transaction_date")) or datetime.now(UTC).date(),
                source_doc_type="WMS_PICK_TASK",
                source_doc_id=_as_uuid(task_id),
                movements=movements,
                idempotency_key=idempotency_key or f"wms-pick:{task_id}",
            )
            transaction_id = result["transaction_id"]
        else:
            transaction_id = None
        rollup = pick_rollup(line_statuses)
        await self._exec(
            "UPDATE wms.pick_task SET status = :status, completed_at = now(), posted_transaction_id = :txn, "
            "updated_by = :actor, updated_at = now(), version = version + 1 "
            "WHERE tenant_id = :tenant_id AND id = :id",
            status=rollup,
            txn=_as_uuid(transaction_id) if transaction_id else None,
            actor=_as_uuid(actor_id),
            id=_as_uuid(task_id),
        )
        if task.get("wave_id"):
            await self._exec(
                "UPDATE wms.wave_line SET status = CASE WHEN :rollup = 'PICKED' THEN 'PICKED' ELSE status END, "
                "updated_at = now(), version = version + 1 WHERE tenant_id = :tenant_id AND wave_id = :wave "
                "AND source_type = ANY(:source_types) AND source_ref = ANY(:source_refs)",
                rollup=rollup,
                wave=_as_uuid(task["wave_id"]),
                source_types=[line["source_ref"] and "SALES_ORDER" for line in task["lines"]],
                source_refs=[line["source_ref"] for line in task["lines"]],
            )
        await self._audit("pick_task.completed", "wms.pick_task", task_id,
                          {"status": rollup, "inventory_transaction_id": transaction_id}, actor_id)
        await self._emit("wms.task.completed", "pick_task", task_id,
                         {"tenant_id": str(self.tenant_id), "task_type": "PICK", "task_id": str(task_id),
                          "status": rollup, "posted_transaction_id": transaction_id})
        completed = await self.get_pick_task(task_id)
        completed["posted_transaction_id"] = transaction_id
        return completed

    async def _assert_lot_pickable(self, lot_id: Any) -> None:
        if not lot_id:
            return
        lot = await self._one(
            "SELECT expiry_date FROM traceability.lot WHERE tenant_id = :tenant_id AND id = :id",
            id=_as_uuid(lot_id),
        )
        if lot is None:
            raise ValidationFailed("lot not found")
        guard = await self._setting("pick_expired_guard_default", True)
        if guard and strategies.is_expired(lot.get("expiry_date"), as_of=datetime.now(UTC).date()):
            raise ExpiredLot("expired lots cannot be picked", details={"lot_id": str(lot_id)})

    async def reverse_pick_task(self, task_id: Any, data: Mapping[str, Any] | None = None, *, actor_id: Any = None,
                                idempotency_key: str | None = None) -> dict[str, Any]:
        await self._require_enabled()
        task = await self._require_row("pick_task", task_id, "pick task")
        if task["status"] not in {"PICKED", "SHORT", "PARTIAL"} or not task.get("posted_transaction_id"):
            raise InvalidStateTransition("only a posted pick task can be reversed")
        shipment = await self._one(
            "SELECT 1 FROM wms.shipment_line sl JOIN wms.shipment s ON s.tenant_id = sl.tenant_id AND s.id = sl.shipment_id "
            "WHERE sl.tenant_id = :tenant_id AND s.status IN ('SHIPPED','DELIVERED') AND sl.source_ref = ANY(:refs) LIMIT 1",
            refs=[line["source_ref"] for line in (await self.get_pick_task(task_id))["lines"]],
        )
        if shipment is not None:
            raise InvalidStateTransition("pick cannot be reversed after the shipment left the warehouse")
        reversal = await self._posting().reverse(str(task["posted_transaction_id"]), reason="WMS pick reversal",
                                                 idempotency_key=idempotency_key or f"wms-pick-reverse:{task_id}")
        await self._exec(
            "UPDATE wms.pick_task SET status = 'REVERSED', updated_by = :actor, updated_at = now(), "
            "version = version + 1 WHERE tenant_id = :tenant_id AND id = :id",
            actor=_as_uuid(actor_id),
            id=_as_uuid(task_id),
        )
        await self._audit("pick_task.reversed", "wms.pick_task", task_id,
                          {"reversal_transaction_id": reversal["transaction_id"]}, actor_id)
        await self._emit("wms.task.reversed", "pick_task", task_id,
                         {"tenant_id": str(self.tenant_id), "task_type": "PICK", "task_id": str(task_id),
                          "reversal_transaction_id": reversal["transaction_id"]})
        return await self.get_pick_task(task_id)

    # -- pack tasks -------------------------------------------------------------------------
    async def create_pack_task(self, data: Mapping[str, Any], *, actor_id: Any = None,
                               idempotency_key: str | None = None) -> dict[str, Any]:
        await self._require_enabled()
        replay = await self._idem_begin("POST /api/v1/pack-tasks", idempotency_key, data, actor_id)
        if replay:
            return replay["replay"]
        location_id = _as_uuid(data.get("location_id"))
        if location_id is None:
            raise ValidationFailed("location_id is required")
        await self._warehouse(location_id, by_location=True)
        task_id = uuid.uuid4()
        number = await self._next_number("TASK")
        await self._exec(
            """
            INSERT INTO wms.pack_task
                (id, tenant_id, task_number, pick_task_id, location_id, status, created_by, updated_by,
                 created_at, updated_at, version)
            VALUES (:id, :tenant_id, :number, :pick_task, :location, 'PENDING', :actor, :actor, now(), now(), 1)
            """,
            id=task_id,
            number=number,
            pick_task=_as_uuid(data.get("pick_task_id")),
            location=location_id,
            actor=_as_uuid(actor_id),
        )
        await self._audit("pack_task.created", "wms.pack_task", task_id, {"task_number": number}, actor_id)
        result = await self._row_by_id(t.pack_task, task_id)
        await self._idem_complete("POST /api/v1/pack-tasks", idempotency_key, result, task_id, 201)
        return result

    async def list_pack_tasks(self, *, status: str | None = None, limit: int = DEFAULT_PAGE_LIMIT) -> list[dict[str, Any]]:
        clauses = ["tenant_id = :tenant_id"]
        params: dict[str, Any] = {}
        if status:
            clauses.append("status = :status")
            params["status"] = status.upper()
        return await self._all(
            f"SELECT * FROM wms.pack_task WHERE {' AND '.join(clauses)} ORDER BY created_at DESC LIMIT :limit",
            limit=_limit(limit),
            **params,
        )

    async def add_pack_carton(self, task_id: Any, data: Mapping[str, Any], *, actor_id: Any = None,
                              idempotency_key: str | None = None) -> dict[str, Any]:
        await self._require_enabled()
        task = await self._require_row("pack_task", task_id, "pack task")
        if task["status"] not in OPEN_TASK_STATUSES:
            raise TaskConflict("pack task is not open")
        carton_id = uuid.uuid4()
        carton_number = str(data.get("carton_number") or "").strip()
        if not carton_number:
            raise ValidationFailed("carton_number is required")
        try:
            await self._exec(
                """
                INSERT INTO wms.pack_carton
                    (id, tenant_id, pack_task_id, carton_number, carton_type, tracking_number, weight, weight_uom,
                     status, created_at, updated_at, version)
                VALUES (:id, :tenant_id, :task, :number, :type, :tracking, :weight, :weight_uom, 'OPEN',
                        now(), now(), 1)
                """,
                id=carton_id,
                task=_as_uuid(task_id),
                number=carton_number,
                type=str(data.get("carton_type") or "CARTON").upper(),
                tracking=data.get("tracking_number"),
                weight=_money(data["weight"]) if data.get("weight") is not None else None,
                weight_uom=data.get("weight_uom"),
            )
        except IntegrityError as exc:
            raise DuplicateDocument("carton number already exists for this pack task") from exc
        for index, line in enumerate(data.get("lines") or [], start=1):
            pick_line_id = _as_uuid(line.get("pick_task_line_id"))
            if pick_line_id is None:
                raise ValidationFailed(f"carton line {index}: pick_task_line_id is required")
            await self._exec(
                """
                INSERT INTO wms.pack_carton_line
                    (id, tenant_id, pack_carton_id, pick_task_line_id, item_id, qty, uom_id, lot_id, serial_ids)
                VALUES (:id, :tenant_id, :carton, :pick_line, :item, :qty, :uom, :lot, :serials)
                """,
                id=uuid.uuid4(),
                carton=carton_id,
                pick_line=pick_line_id,
                item=_as_uuid(line.get("item_id")),
                qty=_qty(line.get("qty")),
                uom=_as_uuid(line.get("uom_id")),
                lot=_as_uuid(line.get("lot_id")),
                serials=[_as_uuid(value) for value in (line.get("serial_ids") or [])] or None,
            )
        await self._audit("pack_carton.created", "wms.pack_carton", carton_id, {"carton_number": carton_number}, actor_id)
        return await self._row_by_id(t.pack_carton, carton_id)

    async def complete_pack_task(self, task_id: Any, data: Mapping[str, Any] | None = None, *, actor_id: Any = None,
                                 idempotency_key: str | None = None) -> dict[str, Any]:
        await self._require_enabled()
        task = await self._require_row("pack_task", task_id, "pack task")
        if task["status"] == "PACKED":
            return task
        if task["status"] != "IN_PROGRESS":
            validate_transition("pack_task", task["status"], "IN_PROGRESS")
        await self._exec(
            "UPDATE wms.pack_task SET status = 'PACKED', packed_by = :actor, packed_at = now(), "
            "package_count = GREATEST(package_count, :count), updated_by = :actor, updated_at = now(), "
            "version = version + 1 WHERE tenant_id = :tenant_id AND id = :id",
            count=int((data or {}).get("package_count") or 0),
            actor=_as_uuid(actor_id),
            id=_as_uuid(task_id),
        )
        await self._exec(
            "UPDATE wms.pack_carton SET status = 'CLOSED', updated_at = now(), version = version + 1 "
            "WHERE tenant_id = :tenant_id AND pack_task_id = :task AND status = 'OPEN'",
            task=_as_uuid(task_id),
        )
        await self._audit("pack_task.completed", "wms.pack_task", task_id, {}, actor_id)
        await self._emit("wms.pack.completed", "pack_task", task_id,
                         {"tenant_id": str(self.tenant_id), "pack_task_id": str(task_id)})
        return await self._row_by_id(t.pack_task, task_id)

    # -- shipments / manifests --------------------------------------------------------------
    async def create_shipment(self, data: Mapping[str, Any], *, actor_id: Any = None,
                              idempotency_key: str | None = None) -> dict[str, Any]:
        await self._require_enabled()
        replay = await self._idem_begin("POST /api/v1/shipments", idempotency_key, data, actor_id)
        if replay:
            return replay["replay"]
        location_id = _as_uuid(data.get("location_id"))
        if location_id is None:
            raise ValidationFailed("location_id is required")
        await self._warehouse(location_id, by_location=True)
        shipment_id = uuid.uuid4()
        number = await self._next_number("SHIP")
        await self._exec(
            """
            INSERT INTO wms.shipment
                (id, tenant_id, shipment_number, location_id, carrier_code, carrier_service_level_id, status,
                 tracking_number, is_manual_ship, created_by, updated_by, created_at, updated_at, version)
            VALUES (:id, :tenant_id, :number, :location, :carrier, :service, 'DRAFT', :tracking, :manual,
                    :actor, :actor, now(), now(), 1)
            """,
            id=shipment_id,
            number=number,
            location=location_id,
            carrier=data.get("carrier_code"),
            service=_as_uuid(data.get("carrier_service_level_id")),
            tracking=data.get("tracking_number"),
            manual=bool(data.get("is_manual_ship", False)),
            actor=_as_uuid(actor_id),
        )
        for index, line in enumerate(data.get("lines") or [], start=1):
            await self._exec(
                """
                INSERT INTO wms.shipment_line
                    (id, tenant_id, shipment_id, line_number, source_type, source_ref, source_line_ref, item_id,
                     qty, uom_id, lot_id, serial_ids, warehouse_shipment_status)
                VALUES (:id, :tenant_id, :shipment, :line, :source_type, :source_ref, :source_line, :item, :qty,
                        :uom, :lot, :serials, 'SHIPPED')
                """,
                id=uuid.uuid4(),
                shipment=shipment_id,
                line=index,
                source_type=str(line.get("source_type") or "SALES_ORDER").upper(),
                source_ref=str(line.get("source_ref") or ""),
                source_line=str(line.get("source_line_ref") or ""),
                item=_as_uuid(line.get("item_id")),
                qty=_qty(line.get("qty")),
                uom=_as_uuid(line.get("uom_id")),
                lot=_as_uuid(line.get("lot_id")),
                serials=[_as_uuid(value) for value in (line.get("serial_ids") or [])] or None,
            )
        await self._audit("shipment.created", "wms.shipment", shipment_id, {"shipment_number": number}, actor_id)
        result = await self.get_shipment(shipment_id)
        await self._idem_complete("POST /api/v1/shipments", idempotency_key, result, shipment_id, 201)
        return result

    async def list_shipments(self, *, location_id: Any = None, status: str | None = None,
                             limit: int = DEFAULT_PAGE_LIMIT) -> list[dict[str, Any]]:
        clauses = ["tenant_id = :tenant_id"]
        params: dict[str, Any] = {}
        if location_id:
            clauses.append("location_id = :location")
            params["location"] = _as_uuid(location_id)
        if status:
            clauses.append("status = :status")
            params["status"] = status.upper()
        return await self._all(
            f"SELECT * FROM wms.shipment WHERE {' AND '.join(clauses)} ORDER BY created_at DESC LIMIT :limit",
            limit=_limit(limit),
            **params,
        )

    async def get_shipment(self, shipment_id: Any) -> dict[str, Any]:
        row = await self._require_row("shipment", shipment_id, "shipment")
        row["lines"] = await self._all(
            "SELECT * FROM wms.shipment_line WHERE tenant_id = :tenant_id AND shipment_id = :id ORDER BY line_number",
            id=_as_uuid(shipment_id),
        )
        row["manifests"] = await self._all(
            "SELECT * FROM wms.ship_manifest WHERE tenant_id = :tenant_id AND shipment_id = :id ORDER BY created_at",
            id=_as_uuid(shipment_id),
        )
        return row

    async def transition_shipment(self, shipment_id: Any, target: str, data: Mapping[str, Any] | None = None, *,
                                  actor_id: Any = None,
                                  idempotency_key: str | None = None) -> dict[str, Any]:
        await self._require_enabled()
        shipment = await self.get_shipment(shipment_id)
        target = target.upper()
        validate_transition("shipment", shipment["status"], target)
        transaction_id = shipment.get("posted_transaction_id")
        if target == "SHIPPED" and shipment["status"] in {"DRAFT", "READY"}:
            if shipment.get("is_manual_ship") and shipment["lines"]:
                movements = []
                for line in shipment["lines"]:
                    movements.append(
                        {
                            "item_id": str(line["item_id"]),
                            "location_id": str(shipment["location_id"]),
                            "inventory_status_id": str(await self._default_status_id()),
                            "qty": str(-_qty(line["qty"])),
                            "lot_id": str(_as_uuid(line["lot_id"])) if line.get("lot_id") else None,
                        }
                    )
                result = await self._posting().post_fulfillment(
                    transaction_date=_date((data or {}).get("transaction_date")) or datetime.now(UTC).date(),
                    source_doc_type="WMS_SHIPMENT",
                    source_doc_id=_as_uuid(shipment_id),
                    movements=movements,
                    idempotency_key=idempotency_key or f"wms-ship:{shipment_id}",
                )
                transaction_id = result["transaction_id"]
            await self._exec(
                "UPDATE wms.shipment SET status = 'SHIPPED', shipped_at = now(), posted_transaction_id = :txn, "
                "updated_by = :actor, updated_at = now(), version = version + 1 "
                "WHERE tenant_id = :tenant_id AND id = :id",
                txn=_as_uuid(transaction_id) if transaction_id else None,
                actor=_as_uuid(actor_id),
                id=_as_uuid(shipment_id),
            )
            await self._exec(
                "UPDATE wms.ship_manifest SET status = 'CONFIRMED', updated_at = now(), version = version + 1 "
                "WHERE tenant_id = :tenant_id AND shipment_id = :id AND status = 'SENT'",
                id=_as_uuid(shipment_id),
            )
            await self._emit("wms.shipment.shipped", "shipment", shipment_id,
                             {"tenant_id": str(self.tenant_id), "shipment_id": str(shipment_id),
                              "material_fulfilled": transaction_id is not None})
        else:
            await self._exec(
                "UPDATE wms.shipment SET status = :status, updated_by = :actor, updated_at = now(), "
                "version = version + 1 WHERE tenant_id = :tenant_id AND id = :id",
                status=target,
                actor=_as_uuid(actor_id),
                id=_as_uuid(shipment_id),
            )
        await self._audit(f"shipment.{target.lower()}", "wms.shipment", shipment_id, {}, actor_id)
        return await self.get_shipment(shipment_id)

    async def create_ship_manifest(self, data: Mapping[str, Any], *, actor_id: Any = None,
                                   idempotency_key: str | None = None) -> dict[str, Any]:
        await self._require_enabled()
        replay = await self._idem_begin("POST /api/v1/ship-manifests", idempotency_key, data, actor_id)
        if replay:
            return replay["replay"]
        shipment_id = _as_uuid(data.get("shipment_id"))
        if shipment_id is None:
            raise ValidationFailed("shipment_id is required")
        shipment = await self.get_shipment(shipment_id)
        manifest_id = uuid.uuid4()
        number = await self._next_number("MFST")
        await self._exec(
            """
            INSERT INTO wms.ship_manifest
                (id, tenant_id, manifest_number, shipment_id, carrier_code, manifest_date, status, payload,
                 response, created_by, updated_by, created_at, updated_at, version)
            VALUES (:id, :tenant_id, :number, :shipment, :carrier, :manifest_date, 'CREATED', CAST(:payload AS jsonb),
                    '{}'::jsonb, :actor, :actor, now(), now(), 1)
            """,
            id=manifest_id,
            number=number,
            shipment=shipment_id,
            carrier=data.get("carrier_code") or shipment.get("carrier_code"),
            manifest_date=_date(data.get("manifest_date")) or datetime.now(UTC).date(),
            payload=json.dumps(_jsonable(data.get("payload") or {})),
            actor=_as_uuid(actor_id),
        )
        for line in data.get("lines") or []:
            await self._exec(
                """
                INSERT INTO wms.ship_manifest_line
                    (id, tenant_id, ship_manifest_id, carton_number, tracking_number, item_id, qty, weight)
                VALUES (:id, :tenant_id, :manifest, :carton, :tracking, :item, :qty, :weight)
                """,
                id=uuid.uuid4(),
                manifest=manifest_id,
                carton=line.get("carton_number"),
                tracking=str(line.get("tracking_number") or shipment.get("tracking_number") or f"TRK-{manifest_id.hex[:12]}"),
                item=_as_uuid(line.get("item_id")),
                qty=_qty(line["qty"]) if line.get("qty") is not None else None,
                weight=_money(line["weight"]) if line.get("weight") is not None else None,
            )
        await self._audit("ship_manifest.created", "wms.ship_manifest", manifest_id,
                          {"manifest_number": number}, actor_id)
        result = await self._row_by_id(t.ship_manifest, manifest_id)
        await self._idem_complete("POST /api/v1/ship-manifests", idempotency_key, result, manifest_id, 201)
        return result

    async def list_ship_manifests(self, *, status: str | None = None,
                                  limit: int = DEFAULT_PAGE_LIMIT) -> list[dict[str, Any]]:
        clauses = ["tenant_id = :tenant_id"]
        params: dict[str, Any] = {}
        if status:
            clauses.append("status = :status")
            params["status"] = status.upper()
        return await self._all(
            f"SELECT * FROM wms.ship_manifest WHERE {' AND '.join(clauses)} ORDER BY created_at DESC LIMIT :limit",
            limit=_limit(limit),
            **params,
        )

    async def send_ship_manifest(self, manifest_id: Any, data: Mapping[str, Any] | None = None, *,
                                 actor_id: Any = None,
                                 idempotency_key: str | None = None) -> dict[str, Any]:
        """Carrier/EDI boundary stub (DM-WMS-08): record the manifest, make no external call (Phase 1)."""
        await self._require_enabled()
        manifest = await self._require_row("ship_manifest", manifest_id, "ship manifest")
        if manifest["status"] == "SENT":
            return manifest
        if manifest["status"] not in {"CREATED", "ERROR"}:
            raise InvalidStateTransition("manifest is not sendable")
        await self._exec(
            "UPDATE wms.ship_manifest SET status = 'SENT', response = CAST(:response AS jsonb), "
            "updated_by = :actor, updated_at = now(), version = version + 1 "
            "WHERE tenant_id = :tenant_id AND id = :id",
            response=json.dumps({"status": "NOT_CONFIGURED", "adapter": "null",
                                 "note": "carrier/EDI integration deferred (D4)"}),
            actor=_as_uuid(actor_id),
            id=_as_uuid(manifest_id),
        )
        await self._audit("ship_manifest.sent", "wms.ship_manifest", manifest_id,
                          {"adapter": "null"}, actor_id)
        await self._emit("wms.manifest.sent", "ship_manifest", manifest_id,
                         {"tenant_id": str(self.tenant_id), "manifest_id": str(manifest_id),
                          "status": "NOT_CONFIGURED"})
        return await self._row_by_id(t.ship_manifest, manifest_id)

    # -- replenishment ----------------------------------------------------------------------
    async def create_replenishment_plan(self, data: Mapping[str, Any], *, actor_id: Any = None,
                                        idempotency_key: str | None = None) -> dict[str, Any]:
        await self._require_enabled()
        replay = await self._idem_begin("POST /api/v1/replenishment-plans", idempotency_key, data, actor_id)
        if replay:
            return replay["replay"]
        location_id = _as_uuid(data.get("location_id"))
        if location_id is None:
            raise ValidationFailed("location_id is required")
        await self._warehouse(location_id, by_location=True)
        plan_id = uuid.uuid4()
        number = await self._next_number("RPL")
        await self._exec(
            """
            INSERT INTO wms.replenishment_plan
                (id, tenant_id, plan_number, location_id, plan_date, status, trigger, memo, created_by,
                 updated_by, created_at, updated_at, version)
            VALUES (:id, :tenant_id, :number, :location, :plan_date, 'DRAFT', :trigger, :memo, :actor, :actor,
                    now(), now(), 1)
            """,
            id=plan_id,
            number=number,
            location=location_id,
            plan_date=_date(data.get("plan_date")) or datetime.now(UTC).date(),
            trigger=str(data.get("trigger") or "MANUAL").upper(),
            memo=data.get("memo"),
            actor=_as_uuid(actor_id),
        )
        await self._audit("replenishment_plan.created", "wms.replenishment_plan", plan_id,
                          {"plan_number": number}, actor_id)
        result = await self._row_by_id(t.replenishment_plan, plan_id)
        await self._idem_complete("POST /api/v1/replenishment-plans", idempotency_key, result, plan_id, 201)
        return result

    async def list_replenishment_plans(self, *, location_id: Any = None, status: str | None = None,
                                       limit: int = DEFAULT_PAGE_LIMIT) -> list[dict[str, Any]]:
        clauses = ["tenant_id = :tenant_id"]
        params: dict[str, Any] = {}
        if location_id:
            clauses.append("location_id = :location")
            params["location"] = _as_uuid(location_id)
        if status:
            clauses.append("status = :status")
            params["status"] = status.upper()
        return await self._all(
            f"SELECT * FROM wms.replenishment_plan WHERE {' AND '.join(clauses)} "
            f"ORDER BY plan_date DESC LIMIT :limit",
            limit=_limit(limit),
            **params,
        )

    async def approve_replenishment_plan(self, plan_id: Any, data: Mapping[str, Any] | None = None, *,
                                         actor_id: Any = None,
                                         idempotency_key: str | None = None) -> dict[str, Any]:
        await self._require_enabled()
        plan = await self._require_row("replenishment_plan", plan_id, "replenishment plan")
        if plan["status"] == "GENERATED":
            return await self._replenishment_plan_response(plan_id)
        validate_transition("replenishment_plan", plan["status"], "APPROVED")
        rules = await self._all(
            "SELECT * FROM wms.item_stock_location WHERE tenant_id = :tenant_id AND location_id = :location "
            "AND is_primary AND replenishment_source_bin_id IS NOT NULL",
            location=_as_uuid(plan["location_id"]),
        )
        task_ids: list[str] = []
        for rule in rules:
            positions = await self._bin_positions(
                location_id=plan["location_id"], item_id=rule["item_id"], bin_ids=[rule["replenishment_source_bin_id"]]
            )
            available = sum((_qty(position["qty_on_hand"]) for position in positions), ZERO)
            target_bin = rule.get("bin_id")
            if target_bin is None:
                continue
            current = await self._scalar(
                "SELECT COALESCE(sum(qty_on_hand), 0) FROM inventory_ledger.inventory_balance_bin "
                "WHERE tenant_id = :tenant_id AND item_id = :item AND bin_id = :bin",
                item=_as_uuid(rule["item_id"]),
                bin=_as_uuid(target_bin),
            )
            deficit = _qty(rule["max_qty"] or 0) - _qty(current)
            if deficit <= 0 or available <= 0:
                continue
            qty = min(deficit, available)
            task_id = uuid.uuid4()
            number = await self._next_number("TASK")
            await self._exec(
                """
                INSERT INTO wms.replenishment_task
                    (id, tenant_id, task_number, replenishment_plan_id, item_id, location_id, from_bin_id,
                     to_bin_id, qty, status, created_at, updated_at, version)
                VALUES (:id, :tenant_id, :number, :plan, :item, :location, :from_bin, :to_bin, :qty, 'PENDING',
                        now(), now(), 1)
                """,
                id=task_id,
                number=number,
                plan=_as_uuid(plan_id),
                item=_as_uuid(rule["item_id"]),
                location=_as_uuid(plan["location_id"]),
                from_bin=_as_uuid(rule["replenishment_source_bin_id"]),
                to_bin=_as_uuid(target_bin),
                qty=qty,
            )
            task_ids.append(str(task_id))
        await self._exec(
            "UPDATE wms.replenishment_plan SET status = 'GENERATED', task_count = :count, updated_by = :actor, "
            "updated_at = now(), version = version + 1 WHERE tenant_id = :tenant_id AND id = :id",
            count=len(task_ids),
            actor=_as_uuid(actor_id),
            id=_as_uuid(plan_id),
        )
        await self._audit("replenishment_plan.generated", "wms.replenishment_plan", plan_id,
                          {"tasks": len(task_ids)}, actor_id)
        await self._emit("wms.replenishment.generated", "replenishment_plan", plan_id,
                         {"tenant_id": str(self.tenant_id), "plan_id": str(plan_id),
                          "task_ids": task_ids})
        response = await self._replenishment_plan_response(plan_id)
        response["task_ids"] = task_ids
        return response

    async def _replenishment_plan_response(self, plan_id: Any) -> dict[str, Any]:
        row = await self._row_by_id(t.replenishment_plan, plan_id)
        row["tasks"] = await self._all(
            "SELECT * FROM wms.replenishment_task WHERE tenant_id = :tenant_id AND replenishment_plan_id = :id",
            id=_as_uuid(plan_id),
        )
        return row

    async def list_replenishment_tasks(self, *, status: str | None = None,
                                       limit: int = DEFAULT_PAGE_LIMIT) -> list[dict[str, Any]]:
        clauses = ["tenant_id = :tenant_id"]
        params: dict[str, Any] = {}
        if status:
            clauses.append("status = :status")
            params["status"] = status.upper()
        return await self._all(
            f"SELECT * FROM wms.replenishment_task WHERE {' AND '.join(clauses)} "
            f"ORDER BY created_at DESC LIMIT :limit",
            limit=_limit(limit),
            **params,
        )

    async def complete_replenishment_task(self, task_id: Any, data: Mapping[str, Any] | None = None, *,
                                          actor_id: Any = None,
                                          idempotency_key: str | None = None) -> dict[str, Any]:
        await self._require_enabled()
        task = await self._require_row("replenishment_task", task_id, "replenishment task")
        if task["status"] == "COMPLETED":
            return task
        if task["status"] not in OPEN_TASK_STATUSES:
            raise InvalidStateTransition("replenishment task is not open")
        qty = _qty((data or {}).get("qty", task["qty"]))
        if qty <= 0 or qty > _qty(task["qty"]):
            raise ValidationFailed("qty must be > 0 and <= task qty")
        status_id = await self._default_status_id()
        movement = {
            "item_id": str(task["item_id"]),
            "location_id": str(task["location_id"]),
            "inventory_status_id": str(status_id),
            "qty": str(-qty),
            "bin_id": str(task["from_bin_id"]),
        }
        movement_in = dict(movement, qty=str(qty), bin_id=str(task["to_bin_id"]))
        result = await self._posting().post_bin_transfer(
            transaction_date=datetime.now(UTC).date(),
            source_doc_type="WMS_REPLENISHMENT_TASK",
            source_doc_id=_as_uuid(task_id),
            movements=[movement, movement_in],
            idempotency_key=idempotency_key or f"wms-replenishment:{task_id}",
        )
        await self._exec(
            "UPDATE wms.replenishment_task SET status = 'COMPLETED', completed_at = now(), "
            "posted_transaction_id = :txn, updated_by = :actor, updated_at = now(), version = version + 1 "
            "WHERE tenant_id = :tenant_id AND id = :id",
            txn=_as_uuid(result["transaction_id"]),
            actor=_as_uuid(actor_id),
            id=_as_uuid(task_id),
        )
        await self._audit("replenishment_task.completed", "wms.replenishment_task", task_id,
                          {"qty": str(qty)}, actor_id)
        await self._emit("wms.task.completed", "replenishment_task", task_id,
                         {"tenant_id": str(self.tenant_id), "task_type": "REPLENISHMENT",
                          "task_id": str(task_id), "posted_transaction_id": result["transaction_id"]})
        return await self._row_by_id(t.replenishment_task, task_id)

    # -- status-change tasks ----------------------------------------------------------------
    async def create_status_change_task(self, data: Mapping[str, Any], *, actor_id: Any = None,
                                        idempotency_key: str | None = None) -> dict[str, Any]:
        await self._require_enabled()
        replay = await self._idem_begin("POST /api/v1/status-change-tasks", idempotency_key, data, actor_id)
        if replay:
            return replay["replay"]
        required = ("item_id", "location_id", "bin_id", "from_status_id", "to_status_id", "qty")
        for field in required:
            if data.get(field) in (None, ""):
                raise ValidationFailed(f"{field} is required")
        item = await self._item(data["item_id"])
        task_id = uuid.uuid4()
        number = await self._next_number("TASK")
        await self._exec(
            """
            INSERT INTO wms.status_change_task
                (id, tenant_id, task_number, item_id, location_id, bin_id, lot_id, serial_ids, from_status_id,
                 to_status_id, qty, status, created_by, updated_by, created_at, updated_at, version)
            VALUES (:id, :tenant_id, :number, :item, :location, :bin, :lot, :serials, :from_status, :to_status,
                    :qty, 'PENDING', :actor, :actor, now(), now(), 1)
            """,
            id=task_id,
            number=number,
            item=_as_uuid(data["item_id"]),
            location=_as_uuid(data["location_id"]),
            bin=_as_uuid(data["bin_id"]),
            lot=_as_uuid(data.get("lot_id")),
            serials=[_as_uuid(value) for value in (data.get("serial_ids") or [])] or None,
            from_status=_as_uuid(data["from_status_id"]),
            to_status=_as_uuid(data["to_status_id"]),
            qty=_qty(data["qty"]),
            actor=_as_uuid(actor_id),
        )
        await self._audit("status_change_task.created", "wms.status_change_task", task_id,
                          {"item_id": str(item["id"])}, actor_id)
        result = await self._row_by_id(t.status_change_task, task_id)
        await self._idem_complete("POST /api/v1/status-change-tasks", idempotency_key, result, task_id, 201)
        return result

    async def complete_status_change_task(self, task_id: Any, data: Mapping[str, Any] | None = None, *,
                                          actor_id: Any = None,
                                          idempotency_key: str | None = None) -> dict[str, Any]:
        await self._require_enabled()
        task = await self._require_row("status_change_task", task_id, "status change task")
        if task["status"] == "COMPLETED":
            return task
        if task["status"] not in OPEN_TASK_STATUSES:
            raise InvalidStateTransition("status-change task is not open")
        qty = _qty((data or {}).get("qty", task["qty"]))
        from_status = await self._one(
            "SELECT is_consigned FROM traceability.inventory_status WHERE tenant_id = :tenant_id AND id = :id",
            id=_as_uuid(task["from_status_id"]),
        )
        to_status = await self._one(
            "SELECT is_consigned FROM traceability.inventory_status WHERE tenant_id = :tenant_id AND id = :id",
            id=_as_uuid(task["to_status_id"]),
        )
        if from_status and to_status and bool(from_status["is_consigned"]) != bool(to_status["is_consigned"]):
            raise ValidationFailed("consigned<->owned status changes are prohibited; use an ownership transfer")
        movements = [
            {
                "item_id": str(task["item_id"]),
                "location_id": str(task["location_id"]),
                "inventory_status_id": str(task["from_status_id"]),
                "qty": str(-qty),
                "bin_id": str(task["bin_id"]),
            },
            {
                "item_id": str(task["item_id"]),
                "location_id": str(task["location_id"]),
                "inventory_status_id": str(task["to_status_id"]),
                "qty": str(qty),
                "bin_id": str(task["bin_id"]),
            },
        ]
        result = await self._posting().post_status_change(
            transaction_date=datetime.now(UTC).date(),
            source_doc_type="WMS_STATUS_CHANGE_TASK",
            source_doc_id=_as_uuid(task_id),
            movements=movements,
            idempotency_key=idempotency_key or f"wms-status-change:{task_id}",
        )
        await self._exec(
            "UPDATE wms.status_change_task SET status = 'COMPLETED', completed_at = now(), "
            "posted_transaction_id = :txn, updated_by = :actor, updated_at = now(), version = version + 1 "
            "WHERE tenant_id = :tenant_id AND id = :id",
            txn=_as_uuid(result["transaction_id"]),
            actor=_as_uuid(actor_id),
            id=_as_uuid(task_id),
        )
        await self._audit("status_change_task.completed", "wms.status_change_task", task_id,
                          {"qty": str(qty)}, actor_id)
        await self._emit("wms.task.completed", "status_change_task", task_id,
                         {"tenant_id": str(self.tenant_id), "task_type": "STATUS_CHANGE",
                          "task_id": str(task_id), "posted_transaction_id": result["transaction_id"]})
        return await self._row_by_id(t.status_change_task, task_id)

    # -- carts / sessions -------------------------------------------------------------------
    async def create_cart(self, data: Mapping[str, Any], *, actor_id: Any = None,
                          idempotency_key: str | None = None) -> dict[str, Any]:
        await self._require_enabled()
        replay = await self._idem_begin("POST /api/v1/wms/carts", idempotency_key, data, actor_id)
        if replay:
            return replay["replay"]
        location_id = _as_uuid(data.get("location_id"))
        if location_id is None:
            raise ValidationFailed("location_id is required")
        await self._warehouse(location_id, by_location=True)
        cart_id = uuid.uuid4()
        try:
            await self._exec(
                """
                INSERT INTO wms.warehouse_cart
                    (id, tenant_id, cart_code, location_id, status, assigned_user_id, created_at, updated_at, version)
                VALUES (:id, :tenant_id, :code, :location, 'OPEN', :user, now(), now(), 1)
                """,
                id=cart_id,
                code=str(data.get("cart_code") or "").strip() or f"CART-{cart_id.hex[:8]}",
                location=location_id,
                user=_as_uuid(data.get("assigned_user_id")),
            )
        except IntegrityError as exc:
            raise DuplicateDocument("cart code already exists") from exc
        await self._audit("cart.created", "wms.warehouse_cart", cart_id, {}, actor_id)
        result = await self._row_by_id(t.warehouse_cart, cart_id)
        await self._idem_complete("POST /api/v1/wms/carts", idempotency_key, result, cart_id, 201)
        return result

    async def list_carts(self, *, status: str | None = None, limit: int = DEFAULT_PAGE_LIMIT) -> list[dict[str, Any]]:
        clauses = ["tenant_id = :tenant_id"]
        params: dict[str, Any] = {}
        if status:
            clauses.append("status = :status")
            params["status"] = status.upper()
        return await self._all(
            f"SELECT * FROM wms.warehouse_cart WHERE {' AND '.join(clauses)} ORDER BY opened_at DESC LIMIT :limit",
            limit=_limit(limit),
            **params,
        )

    async def update_cart(self, cart_id: Any, data: Mapping[str, Any], *, actor_id: Any = None,
                          idempotency_key: str | None = None) -> dict[str, Any]:
        await self._require_enabled()
        cart = await self._require_row("warehouse_cart", cart_id, "cart")
        target = str(data.get("status") or cart["status"]).upper()
        if target == "CLOSED":
            open_tasks = await self._scalar(
                "SELECT count(*) FROM wms.pick_task WHERE tenant_id = :tenant_id AND cart_id = :cart "
                "AND status = ANY(:statuses)",
                cart=_as_uuid(cart_id),
                statuses=list(OPEN_TASK_STATUSES),
            )
            if int(open_tasks or 0) > 0:
                raise TaskConflict("cannot close a cart with open tasks",
                                   details={"open_tasks": int(open_tasks)})
        await self._exec(
            "UPDATE wms.warehouse_cart SET status = :status, assigned_user_id = COALESCE(:user, assigned_user_id), "
            "closed_at = CASE WHEN :status = 'CLOSED' THEN now() ELSE closed_at END, updated_by = :actor, "
            "updated_at = now(), version = version + 1 WHERE tenant_id = :tenant_id AND id = :id",
            status=target,
            user=_as_uuid(data.get("assigned_user_id")),
            actor=_as_uuid(actor_id),
            id=_as_uuid(cart_id),
        )
        await self._audit("cart.updated", "wms.warehouse_cart", cart_id, {"status": target}, actor_id)
        return await self._row_by_id(t.warehouse_cart, cart_id)

    async def create_device_session(self, data: Mapping[str, Any], *, actor_id: Any = None,
                                    idempotency_key: str | None = None) -> dict[str, Any]:
        """Backend session container (EXT; D8: no device protocol/offline sync endpoints)."""
        await self._require_enabled()
        import hashlib
        import secrets

        warehouse_id = _as_uuid(data.get("warehouse_id"))
        if warehouse_id is None:
            raise ValidationFailed("warehouse_id is required")
        await self._require_row("warehouse", warehouse_id, "warehouse")
        user_id = _as_uuid(data.get("user_id")) or _as_uuid(actor_id) or uuid.uuid4()
        token = secrets.token_urlsafe(32)
        session_id = uuid.uuid4()
        await self._exec(
            """
            INSERT INTO wms.device_session
                (id, tenant_id, warehouse_id, user_id, session_token_hash, started_at, last_seen_at, client_info,
                 created_at, updated_at, version)
            VALUES (:id, :tenant_id, :warehouse, :user, :hash, now(), now(), CAST(:client AS jsonb), now(), now(), 1)
            """,
            id=session_id,
            warehouse=warehouse_id,
            user=user_id,
            hash=hashlib.sha256(token.encode()).hexdigest(),
            client=json.dumps(_jsonable(data.get("client_info") or {})),
        )
        await self._audit("device_session.created", "wms.device_session", session_id, {}, actor_id)
        result = await self._row_by_id(t.device_session, session_id)
        result["session_token"] = token  # returned once; only the hash is persisted
        return result

    async def heartbeat_device_session(self, session_id: Any, *, actor_id: Any = None) -> dict[str, Any]:
        await self._require_enabled()
        session = await self._require_row("device_session", session_id, "device session")
        if session["ended_at"] is not None:
            raise InvalidStateTransition("device session has ended")
        await self._exec(
            "UPDATE wms.device_session SET last_seen_at = now(), updated_at = now(), version = version + 1 "
            "WHERE tenant_id = :tenant_id AND id = :id",
            id=_as_uuid(session_id),
        )
        return await self._row_by_id(t.device_session, session_id)

    async def end_device_session(self, session_id: Any, *, actor_id: Any = None,
                                 idempotency_key: str | None = None) -> dict[str, Any]:
        await self._require_enabled()
        session = await self._require_row("device_session", session_id, "device session")
        if session["ended_at"] is None:
            await self._exec(
                "UPDATE wms.device_session SET ended_at = now(), updated_at = now(), version = version + 1 "
                "WHERE tenant_id = :tenant_id AND id = :id",
                id=_as_uuid(session_id),
            )
            await self._audit("device_session.ended", "wms.device_session", session_id, {}, actor_id)
        return await self._row_by_id(t.device_session, session_id)

    # ====================================================================== read models -----
    async def list_tasks(self, *, task_type: str | None = None, status: str | None = None,
                         warehouse_id: Any = None, assigned_user_id: Any = None,
                         limit: int = DEFAULT_PAGE_LIMIT) -> dict[str, Any]:
        tasks: list[dict[str, Any]] = []
        sources = [
            ("PUTAWAY", "putaway_task", "putaway_task_id"),
            ("PICK", "pick_task", "pick_task_id"),
            ("REPLENISHMENT", "replenishment_task", "replenishment_task_id"),
            ("STATUS_CHANGE", "status_change_task", "status_change_task_id"),
        ]
        for label, table, _key in sources:
            if task_type and task_type.upper() != label:
                continue
            clauses = ["tenant_id = :tenant_id"]
            params: dict[str, Any] = {}
            if status:
                clauses.append("status = :status")
                params["status"] = status.upper()
            if assigned_user_id:
                clauses.append("assigned_user_id = :user")
                params["user"] = _as_uuid(assigned_user_id)
            if warehouse_id:
                columns = [column.name for column in t.metadata.tables[f"wms.{table}"].columns]
                if "location_id" in columns:
                    clauses.append("location_id = :location")
                    params["location"] = _as_uuid(warehouse_id)
            for row in await self._all(
                f"SELECT id, task_number, status, assigned_user_id, created_at FROM wms.{table} "
                f"WHERE {' AND '.join(clauses)} ORDER BY created_at DESC LIMIT :limit",
                limit=_limit(limit),
                **params,
            ):
                row["task_type"] = label
                tasks.append(row)
        tasks.sort(key=lambda row: str(row.get("created_at") or ""), reverse=True)
        return {"items": tasks[: _limit(limit)], "count": len(tasks[: _limit(limit)])}

    async def fulfillment_generations(self, *, wave_id: Any = None, status: str | None = None,
                                      limit: int = DEFAULT_PAGE_LIMIT) -> dict[str, Any]:
        clauses = ["t.tenant_id = :tenant_id", "t.posted_transaction_id IS NOT NULL"]
        params: dict[str, Any] = {}
        if wave_id:
            clauses.append("t.wave_id = :wave")
            params["wave"] = _as_uuid(wave_id)
        if status:
            clauses.append("t.status = :status")
            params["status"] = status.upper()
        rows = await self._all(
            f"SELECT t.id AS pick_task_id, t.task_number, t.wave_id, t.status, t.posted_transaction_id, "
            f"t.completed_at FROM wms.pick_task t WHERE {' AND '.join(clauses)} "
            f"ORDER BY t.completed_at DESC NULLS LAST LIMIT :limit",
            limit=_limit(limit),
            **params,
        )
        return {"items": rows, "count": len(rows)}

    async def order_release_status(self, *, location_id: Any = None, status: str | None = None,
                                   limit: int = DEFAULT_PAGE_LIMIT) -> dict[str, Any]:
        clauses = ["wl.tenant_id = :tenant_id"]
        params: dict[str, Any] = {}
        if location_id:
            clauses.append("w.location_id = :location")
            params["location"] = _as_uuid(location_id)
        if status:
            clauses.append("wl.status = :status")
            params["status"] = status.upper()
        rows = await self._all(
            f"SELECT wl.id AS wave_line_id, w.wave_number, w.status AS wave_status, wl.source_type, wl.source_ref, "
            f"wl.source_line_ref, wl.item_id, wl.qty, wl.status, wl.pick_task_id "
            f"FROM wms.wave_line wl JOIN wms.wave w ON w.tenant_id = wl.tenant_id AND w.id = wl.wave_id "
            f"WHERE {' AND '.join(clauses)} ORDER BY w.created_at DESC, wl.line_number LIMIT :limit",
            limit=_limit(limit),
            **params,
        )
        return {"items": rows, "count": len(rows)}


__all__ = ["FLAG_KEY", "SETTING_PREFIX", "WmsService"]
