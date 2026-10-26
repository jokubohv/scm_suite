"""FS-08 Procurement P2P service — vendor master through requisition/PO/receipt/bill/credit/return.

All write methods execute in the caller's transaction (routers provide one AsyncSession) and post
inventory/value effects only through the frozen FS-04 ``PostingPort`` and FS-05 costing contract; no
ledger/cost table is written here. Quantity guards (PO open qty, over-receipt tolerance, receipt-bill
match) are computed from documents and service settings; the ledger remains the quantity authority for
posted receipts/returns.

Document numbers come from the FS-00 ``SequenceService`` (declared ``PO``/``IR``/``VB``/``VC``/``VRA``);
events are written through the transactional outbox; every state change is audited.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.approvals import ApprovalService
from app.core.idempotency_store import IdempotencyStore
from app.core.outbox import emit
from app.core.sequences import SequenceError, SequenceService
from app.modules.procurement.errors import (
    CreditExceedsOpen,
    DependentDocuments,
    DomainDisabled,
    DuplicateDocument,
    InvalidStateTransition,
    ItemNotPurchasable,
    MatchException,
    NotFound,
    QuantityExceedsOrder,
    ValidationFailed,
)

ZERO = Decimal("0")
MONEY_Q = Decimal("0.000000000001")
QTY_Q = Decimal("0.000000000001")


def utc_now() -> datetime:
    return datetime.now(UTC)


def dec(value: Any, default: Decimal = ZERO) -> Decimal:
    if value is None or value == "":
        return default
    return Decimal(str(value))


def qty(value: Any) -> Decimal:
    return dec(value).quantize(QTY_Q, rounding=ROUND_HALF_UP)


def money(value: Any) -> Decimal:
    return dec(value).quantize(MONEY_Q, rounding=ROUND_HALF_UP)


def _jsonable(value: Any) -> Any:
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        text = format(value, "f")
        return text.rstrip("0").rstrip(".") if "." in text else text
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_jsonable(item) for item in value]
    return value


def _num(value: Any) -> str:
    text = format(dec(value), "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _as_uuid(value: Any) -> uuid.UUID:
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


def _date(value: Any) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def _stamp(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=UTC)
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _row(row: Any) -> dict[str, Any]:
    mapping = getattr(row, "_mapping", row)
    return _jsonable(dict(mapping))


def _rows(rows: Sequence[Any]) -> list[dict[str, Any]]:
    return [_row(row) for row in rows]


class ProcurementService:
    def __init__(self, session: AsyncSession, tenant_id: str | uuid.UUID) -> None:
        self.session = session
        self.tenant_id = tenant_id if isinstance(tenant_id, uuid.UUID) else uuid.UUID(str(tenant_id))
        self.idempotency = IdempotencyStore(session)

    # ------------------------------------------------------------------ low-level helpers ----
    async def _exec(self, sql: str, **params: Any):
        return await self.session.execute(__import__("sqlalchemy").text(sql), {"tenant_id": self.tenant_id, **params})

    async def _one(self, sql: str, **params: Any) -> dict[str, Any] | None:
        row = (await self._exec(sql, **params)).mappings().first()
        return _row(row) if row else None

    async def _all(self, sql: str, **params: Any) -> list[dict[str, Any]]:
        return _rows((await self._exec(sql, **params)).mappings().all())

    async def _scalar(self, sql: str, **params: Any) -> Any:
        return (await self._exec(sql, **params)).scalar_one_or_none()

    async def _flag(self, key: str, default: bool = False) -> bool:
        value = await self._scalar(
            "SELECT is_enabled FROM platform.feature_flag WHERE tenant_id = :tenant_id AND flag_key = :key", key=key
        )
        return bool(default if value is None else value)

    async def _setting(self, key: str, default: Any = None) -> Any:
        row = await self._one(
            "SELECT value FROM platform.tenant_setting WHERE tenant_id = :tenant_id AND namespace = 'inventory' AND key = :setting_key",
            setting_key=f"procurement.{key}",
        )
        if row is None:
            return default
        value = row["value"]
        if isinstance(value, str) and value.startswith('"') and value.endswith('"'):
            return value[1:-1]
        return value

    async def _require_enabled(self) -> None:
        if not await self._flag("procurement.p2p.enabled", False):
            raise DomainDisabled("procurement.p2p is disabled for this tenant (procurement.p2p.enabled=false)")

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
                (id, tenant_id, event_id, actor_type, actor_id, action, resource_type, resource_id, diff, source, occurred_at)
            VALUES (:id, :tenant_id, :event_id, 'USER', :actor, :action, :resource_type, :resource_id, CAST(:diff AS jsonb), 'API', now())
            """,
            id=event_id,
            event_id=event_id,
            actor=_as_uuid(actor_id) if actor_id else None,
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
        from app.core.security.idempotency import IdempotencyOutcome

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

    # ------------------------------------------------------------------------------- reads ----
    async def _require_row(self, table: str, row_id: Any, label: str) -> dict[str, Any]:
        row = await self._one(
            f"SELECT * FROM procurement.{table} WHERE tenant_id = :tenant_id AND id = :id", id=_as_uuid(row_id)
        )
        if row is None:
            raise NotFound(f"{label} not found")
        return row

    async def _current_user(self, actor_id: Any) -> str:
        return str(actor_id or uuid.uuid4())

    # ------------------------------------------------------------------------------ vendors ----
    async def create_vendor(
        self, data: Mapping[str, Any], *, actor_id: str | None = None, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        await self._require_enabled()
        replay = await self._idem_begin("POST /api/v1/vendors", idempotency_key, data, actor_id)
        if replay:
            return replay["replay"]
        number = str(data.get("vendor_number") or "").strip()
        name = str(data.get("name") or "").strip()
        if not number or not name:
            raise ValidationFailed("vendor_number and name are required")
        currency = str(data.get("currency_code") or "USD").upper()
        if not await self._scalar("SELECT 1 FROM platform.currency WHERE code = :code AND is_active", code=currency):
            raise ValidationFailed(f"unknown/inactive currency {currency!r}")
        category_id = data.get("vendor_category_id")
        if category_id:
            await self._require_row("vendor_category", category_id, "vendor category")
        vendor_id = uuid.uuid4()
        try:
            await self._exec(
                """
                INSERT INTO procurement.vendor
                    (id, tenant_id, vendor_number, name, vendor_category_id, currency_code, payment_terms, tax_id,
                     is_1099_eligible, credit_limit, default_expense_account_ref, default_payable_account_ref,
                     addresses, primary_contact, created_by, updated_by, created_at, updated_at, version)
                VALUES (:id, :tenant_id, :number, :name, :category, :currency, :terms, :tax_id, :eligible, :credit_limit,
                        :expense_ref, :payable_ref, CAST(:addresses AS jsonb), CAST(:contact AS jsonb), :actor, :actor, now(), now(), 1)
                """,
                id=vendor_id,
                number=number,
                name=name,
                category=_as_uuid(category_id) if category_id else None,
                currency=currency,
                terms=data.get("payment_terms"),
                tax_id=data.get("tax_id"),
                eligible=bool(data.get("is_1099_eligible", False)),
                credit_limit=money(data["credit_limit"]) if data.get("credit_limit") is not None else None,
                expense_ref=data.get("default_expense_account_ref"),
                payable_ref=data.get("default_payable_account_ref"),
                addresses=json.dumps(_jsonable(data.get("addresses") or [])),
                contact=json.dumps(_jsonable(data.get("primary_contact") or {})),
                actor=_as_uuid(actor_id) if actor_id else None,
            )
        except IntegrityError as exc:
            raise DuplicateDocument("vendor_number or name already exists") from exc
        await self._audit(
            "vendor.created", "procurement.vendor", vendor_id, {"vendor_number": number, "name": name}, actor_id
        )
        result = await self.get_vendor(vendor_id)
        await self._idem_complete("POST /api/v1/vendors", idempotency_key, result, vendor_id, 201)
        return result

    async def update_vendor(
        self,
        vendor_id: Any,
        data: Mapping[str, Any],
        *,
        actor_id: str | None = None,
        expected_version: int | None = None,
    ) -> dict[str, Any]:
        await self._require_enabled()
        vendor = await self._require_row("vendor", vendor_id, "vendor")
        fields = {
            key: (money(data[key]) if key in {"credit_limit"} and data[key] is not None else data[key])
            for key in (
                "name",
                "vendor_category_id",
                "currency_code",
                "payment_terms",
                "tax_id",
                "is_1099_eligible",
                "credit_limit",
                "default_expense_account_ref",
                "default_payable_account_ref",
                "addresses",
                "primary_contact",
            )
            if key in data
        }
        if "currency_code" in fields:
            fields["currency_code"] = str(fields["currency_code"]).upper()
        if fields:
            json_fields = {"addresses", "primary_contact"} & set(fields)
            assignments = ", ".join(
                f"{key} = CAST(:{key} AS jsonb)" if key in json_fields else f"{key} = :{key}" for key in fields
            )
            params = {
                key: json.dumps(_jsonable(value)) if key in json_fields else _jsonable(value)
                for key, value in fields.items()
            }
            await self._exec(
                f"UPDATE procurement.vendor SET {assignments}, updated_at = now(), updated_by = :actor, version = version + 1 "
                f"WHERE tenant_id = :tenant_id AND id = :id"
                + (" AND version = :expected_version" if expected_version else ""),
                id=_as_uuid(vendor_id),
                actor=_as_uuid(actor_id) if actor_id else None,
                expected_version=expected_version,
                **params,
            )
        await self._audit(
            "vendor.updated",
            "procurement.vendor",
            vendor_id,
            {"before": vendor, "after": {k: _jsonable(v) for k, v in fields.items()}},
            actor_id,
        )
        return await self.get_vendor(vendor_id)

    async def inactivate_vendor(
        self, vendor_id: Any, reason: str, *, actor_id: str | None = None, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        await self._require_enabled()
        await self._require_row("vendor", vendor_id, "vendor")
        await self._exec(
            "UPDATE procurement.vendor SET is_inactive = true, inactive_at = now(), updated_at = now(), version = version + 1 "
            "WHERE tenant_id = :tenant_id AND id = :id",
            id=_as_uuid(vendor_id),
        )
        await self._audit("vendor.inactivated", "procurement.vendor", vendor_id, {"reason": reason}, actor_id)
        return {"id": str(vendor_id), "is_inactive": True}

    async def merge_vendors(
        self,
        source_vendor_id: Any,
        target_vendor_id: Any,
        *,
        actor_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        await self._require_enabled()
        source = await self._require_row("vendor", source_vendor_id, "vendor")
        target = await self._require_row("vendor", target_vendor_id, "vendor")
        if str(source["id"]) == str(target["id"]):
            raise ValidationFailed("source and target vendor must differ")
        moves: dict[str, int] = {}
        for table, column in (
            ("item_vendor", "vendor_id"),
            ("vendor_contract", "vendor_id"),
            ("purchase_order", "vendor_id"),
            ("item_receipt", "vendor_id"),
            ("vendor_bill", "vendor_id"),
            ("vendor_credit", "vendor_id"),
            ("vendor_return_authorization", "vendor_id"),
            ("rfq_response", "vendor_id"),
            ("purchase_requisition_line", "suggested_vendor_id"),
        ):
            result = await self._exec(
                f"UPDATE procurement.{table} SET {column} = :target WHERE tenant_id = :tenant_id AND {column} = :source",
                target=_as_uuid(target_vendor_id),
                source=_as_uuid(source_vendor_id),
            )
            moves[f"{table}.{column}"] = int(getattr(result, "rowcount", 0) or 0)
        await self._exec(
            "UPDATE procurement.vendor SET is_inactive = true, inactive_at = now(), updated_at = now(), version = version + 1 "
            "WHERE tenant_id = :tenant_id AND id = :id",
            id=_as_uuid(source_vendor_id),
        )
        await self._audit(
            "vendor.merged",
            "procurement.vendor",
            source_vendor_id,
            {"target": str(target_vendor_id), "moves": moves},
            actor_id,
        )
        return {
            "source_vendor_id": str(source_vendor_id),
            "target_vendor_id": str(target_vendor_id),
            "moves": moves,
            "source_inactive": True,
        }

    async def get_vendor(self, vendor_id: Any) -> dict[str, Any]:
        vendor = await self._require_row("vendor", vendor_id, "vendor")
        vendor["item_count"] = int(
            await self._scalar(
                "SELECT count(*) FROM procurement.item_vendor WHERE tenant_id = :tenant_id AND vendor_id = :id",
                id=_as_uuid(vendor_id),
            )
            or 0
        )
        return vendor

    async def list_vendors(
        self,
        *,
        is_inactive: bool | None = None,
        category_id: str | None = None,
        q: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        clauses = ["tenant_id = :tenant_id"]
        params: dict[str, Any] = {"limit": max(1, min(int(limit), 200))}
        if is_inactive is not None:
            clauses.append("is_inactive = :inactive")
            params["inactive"] = bool(is_inactive)
        if category_id:
            clauses.append("vendor_category_id = :category")
            params["category"] = _as_uuid(category_id)
        if q:
            clauses.append("(vendor_number ILIKE :q OR name ILIKE :q)")
            params["q"] = f"%{q}%"
        if cursor:
            clauses.append("id > :cursor")
            params["cursor"] = _as_uuid(cursor)
        rows = await self._all(
            f"SELECT * FROM procurement.vendor WHERE {' AND '.join(clauses)} ORDER BY vendor_number LIMIT :limit",
            **params,
        )
        next_cursor = str(rows[-1]["id"]) if len(rows) >= params["limit"] and rows else None
        return {"items": rows, "next_cursor": next_cursor}

    async def get_vendor_purchase_history(
        self,
        vendor_id: Any,
        *,
        date_from: str | None = None,
        date_to: str | None = None,
        item_id: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        await self._require_row("vendor", vendor_id, "vendor")
        clauses = ["po.tenant_id = :tenant_id", "po.vendor_id = :vendor"]
        params: dict[str, Any] = {"vendor": _as_uuid(vendor_id), "limit": max(1, min(int(limit), 500))}
        if date_from:
            clauses.append("po.transaction_date >= :date_from")
            params["date_from"] = date_from
        if date_to:
            clauses.append("po.transaction_date <= :date_to")
            params["date_to"] = date_to
        if item_id:
            clauses.append("pol.item_id = :item")
            params["item"] = _as_uuid(item_id)
        rows = await self._all(
            f"""
            SELECT po.id AS purchase_order_id, po.document_number, po.status, po.transaction_date, po.total,
                   pol.id AS purchase_order_line_id, pol.item_id, pol.qty_ordered, pol.qty_received, pol.qty_billed
              FROM procurement.purchase_order po
              JOIN procurement.purchase_order_line pol ON pol.tenant_id = po.tenant_id AND pol.purchase_order_id = po.id
             WHERE {" AND ".join(clauses)}
             ORDER BY po.transaction_date DESC LIMIT :limit
            """,
            **params,
        )
        return {"items": rows}

    # --------------------------------------------------------------------------- categories ----
    async def list_vendor_categories(self, *, is_inactive: bool | None = None, limit: int = 100) -> dict[str, Any]:
        clauses = ["tenant_id = :tenant_id"]
        params: dict[str, Any] = {"limit": max(1, min(int(limit), 500))}
        if is_inactive is not None:
            clauses.append("is_inactive = :inactive")
            params["inactive"] = bool(is_inactive)
        return {
            "items": await self._all(
                f"SELECT * FROM procurement.vendor_category WHERE {' AND '.join(clauses)} ORDER BY code LIMIT :limit",
                **params,
            ),
            "next_cursor": None,
        }

    async def create_vendor_category(
        self, data: Mapping[str, Any], *, actor_id: str | None = None, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        await self._require_enabled()
        replay = await self._idem_begin("POST /api/v1/vendor-categories", idempotency_key, data, actor_id)
        if replay:
            return replay["replay"]
        code = str(data.get("code") or "").strip()
        name = str(data.get("name") or "").strip()
        if not code or not name:
            raise ValidationFailed("code and name are required")
        category_id = uuid.uuid4()
        try:
            await self._exec(
                "INSERT INTO procurement.vendor_category (id, tenant_id, code, name, created_by, updated_by) "
                "VALUES (:id, :tenant_id, :code, :name, :actor, :actor)",
                id=category_id,
                code=code,
                name=name,
                actor=_as_uuid(actor_id) if actor_id else None,
            )
        except IntegrityError as exc:
            raise DuplicateDocument("vendor category code already exists") from exc
        await self._audit(
            "vendor_category.created", "procurement.vendor_category", category_id, {"code": code}, actor_id
        )
        await self._require_row("vendor_category", category_id, "vendor category")
        await self._idem_complete(
            "POST /api/v1/vendor-categories", idempotency_key, {"id": str(category_id), "code": code}, category_id, 201
        )
        return {"id": str(category_id), "code": code, "name": name}

    async def update_vendor_category(
        self, category_id: Any, data: Mapping[str, Any], *, actor_id: str | None = None
    ) -> dict[str, Any]:
        await self._require_row("vendor_category", category_id, "vendor category")
        values = {key: data[key] for key in ("name", "is_inactive") if key in data}
        if values:
            await self._exec(
                "UPDATE procurement.vendor_category SET "
                + ", ".join(f"{key} = :{key}" for key in values)
                + ", updated_at = now(), version = version + 1 "
                "WHERE tenant_id = :tenant_id AND id = :id",
                id=_as_uuid(category_id),
                **values,
            )
        await self._audit("vendor_category.updated", "procurement.vendor_category", category_id, values, actor_id)
        return await self._require_row("vendor_category", category_id, "vendor category")

    # -------------------------------------------------------------------------- item-vendor ----
    async def _item(self, item_id: Any) -> dict[str, Any]:
        item = await self._one(
            """
            SELECT i.id, i.item_number, i.display_name, i.is_active, t.code AS item_type_code, t.can_stock
              FROM item_master.item i JOIN item_master.item_type_definition t ON t.tenant_id = i.tenant_id AND t.id = i.item_type_id
             WHERE i.tenant_id = :tenant_id AND i.id = :id
            """,
            id=_as_uuid(item_id),
        )
        if item is None:
            raise NotFound("item not found")
        return item

    async def associate_item_vendor(
        self, data: Mapping[str, Any], *, actor_id: str | None = None, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        await self._require_enabled()
        replay = await self._idem_begin("POST /api/v1/item-vendors", idempotency_key, data, actor_id)
        if replay:
            return replay["replay"]
        item = await self._item(data.get("item_id"))
        if not item.get("is_active"):
            raise ItemNotPurchasable("item is inactive")
        vendor = await self._require_row("vendor", data.get("vendor_id"), "vendor")
        if vendor["is_inactive"]:
            raise ValidationFailed("vendor is inactive")
        if data.get("is_preferred") and await self._scalar(
            "SELECT 1 FROM procurement.item_vendor WHERE tenant_id = :tenant_id AND item_id = :item AND is_preferred AND NOT is_inactive",
            item=_as_uuid(data["item_id"]),
        ):
            raise ValidationFailed("item already has a preferred vendor")
        row_id = uuid.uuid4()
        try:
            await self._exec(
                """
                INSERT INTO procurement.item_vendor
                    (id, tenant_id, item_id, vendor_id, vendor_item_code, is_preferred, lead_time_days, min_order_qty,
                     purchase_uom_id, currency_code, created_by, updated_by)
                VALUES (:id, :tenant_id, :item, :vendor, :code, :preferred, :lead, :moq, :uom, :currency, :actor, :actor)
                """,
                id=row_id,
                item=_as_uuid(data["item_id"]),
                vendor=_as_uuid(data["vendor_id"]),
                code=data.get("vendor_item_code"),
                preferred=bool(data.get("is_preferred", False)),
                lead=int(data.get("lead_time_days") or 0),
                moq=qty(data.get("min_order_qty") or 0),
                uom=_as_uuid(data["purchase_uom_id"]) if data.get("purchase_uom_id") else None,
                currency=str(data.get("currency_code") or vendor["currency_code"]).upper(),
                actor=_as_uuid(actor_id) if actor_id else None,
            )
        except IntegrityError as exc:
            raise DuplicateDocument("item-vendor association already exists") from exc
        await self._audit(
            "item_vendor.associated",
            "procurement.item_vendor",
            row_id,
            {"item_id": str(data["item_id"]), "vendor_id": str(data["vendor_id"])},
            actor_id,
        )
        result = await self._require_row("item_vendor", row_id, "item vendor")
        await self._idem_complete("POST /api/v1/item-vendors", idempotency_key, {"id": str(row_id)}, row_id, 201)
        return {
            "id": str(row_id),
            **{key: result[key] for key in ("item_id", "vendor_id", "vendor_item_code", "is_preferred")},
        }

    async def update_item_vendor(
        self, row_id: Any, data: Mapping[str, Any], *, actor_id: str | None = None
    ) -> dict[str, Any]:
        await self._require_row("item_vendor", row_id, "item vendor")
        values = {
            key: data[key]
            for key in (
                "vendor_item_code",
                "is_preferred",
                "lead_time_days",
                "min_order_qty",
                "purchase_uom_id",
                "currency_code",
                "is_inactive",
            )
            if key in data
        }
        if values:
            await self._exec(
                "UPDATE procurement.item_vendor SET "
                + ", ".join(f"{key} = :{key}" for key in values)
                + ", updated_at = now(), version = version + 1 "
                "WHERE tenant_id = :tenant_id AND id = :id",
                id=_as_uuid(row_id),
                **values,
            )
        await self._audit("item_vendor.updated", "procurement.item_vendor", row_id, values, actor_id)
        return await self._require_row("item_vendor", row_id, "item vendor")

    async def list_item_vendors(
        self,
        *,
        item_id: str | None = None,
        vendor_id: str | None = None,
        is_preferred: bool | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        clauses = ["tenant_id = :tenant_id"]
        params: dict[str, Any] = {"limit": max(1, min(int(limit), 500))}
        if item_id:
            clauses.append("item_id = :item")
            params["item"] = _as_uuid(item_id)
        if vendor_id:
            clauses.append("vendor_id = :vendor")
            params["vendor"] = _as_uuid(vendor_id)
        if is_preferred is not None:
            clauses.append("is_preferred = :preferred")
            params["preferred"] = bool(is_preferred)
        return {
            "items": await self._all(
                f"SELECT * FROM procurement.item_vendor WHERE {' AND '.join(clauses)} ORDER BY item_id, vendor_id LIMIT :limit",
                **params,
            ),
            "next_cursor": None,
        }

    async def add_item_vendor_price(
        self,
        item_vendor_id: Any,
        data: Mapping[str, Any],
        *,
        actor_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        await self._require_row("item_vendor", item_vendor_id, "item vendor")
        replay = await self._idem_begin("POST /api/v1/item-vendors/{id}/prices", idempotency_key, data, actor_id)
        if replay:
            return replay["replay"]
        row_id = uuid.uuid4()
        await self._exec(
            """
            INSERT INTO procurement.item_vendor_price
                (id, tenant_id, item_vendor_id, qty_min, qty_max, unit_price, currency_code, effective_from, effective_to,
                 created_by, updated_by)
            VALUES (:id, :tenant_id, :item_vendor, :qty_min, :qty_max, :price, :currency, :from_date, :to_date, :actor, :actor)
            """,
            id=row_id,
            item_vendor=_as_uuid(item_vendor_id),
            qty_min=qty(data.get("qty_min") or 0),
            qty_max=qty(data["qty_max"]) if data.get("qty_max") is not None else None,
            price=money(data.get("unit_price")),
            currency=str(data.get("currency_code") or "USD").upper(),
            from_date=_date(data.get("effective_from")) or date.today(),
            to_date=_date(data.get("effective_to")),
            actor=_as_uuid(actor_id) if actor_id else None,
        )
        await self._audit(
            "item_vendor.price_set",
            "procurement.item_vendor_price",
            row_id,
            {"item_vendor_id": str(item_vendor_id), "unit_price": str(data.get("unit_price"))},
            actor_id,
        )
        await self._idem_complete(
            "POST /api/v1/item-vendors/{id}/prices", idempotency_key, {"id": str(row_id)}, row_id, 201
        )
        return {"id": str(row_id)}

    async def list_item_vendor_prices(
        self, item_vendor_id: Any, *, currency: str | None = None, as_of: str | None = None
    ) -> dict[str, Any]:
        clauses = ["tenant_id = :tenant_id", "item_vendor_id = :id"]
        params: dict[str, Any] = {"id": _as_uuid(item_vendor_id)}
        if currency:
            clauses.append("currency_code = :currency")
            params["currency"] = currency.upper()
        if as_of:
            clauses.append("effective_from <= :as_of AND (effective_to IS NULL OR effective_to >= :as_of)")
            params["as_of"] = _date(as_of)
        return {
            "items": await self._all(
                f"SELECT * FROM procurement.item_vendor_price WHERE {' AND '.join(clauses)} ORDER BY qty_min, effective_from",
                **params,
            )
        }

    async def resolve_unit_price(
        self, item_id: Any, vendor_id: Any, quantity: Any, *, currency: str = "USD", as_of: date | None = None
    ) -> Decimal:
        moment = _date(as_of) or date.today()
        contract = await self._one(
            """
            SELECT vci.unit_price, vci.discount_percent
              FROM procurement.vendor_contract_item vci
              JOIN procurement.vendor_contract vc ON vc.tenant_id = vci.tenant_id AND vc.id = vci.vendor_contract_id
             WHERE vci.tenant_id = :tenant_id AND vc.vendor_id = :vendor AND vci.item_id = :item
               AND vc.status = 'ACTIVE' AND vc.start_date <= :as_of AND (vc.end_date IS NULL OR vc.end_date >= :as_of)
             ORDER BY vc.start_date DESC LIMIT 1
            """,
            vendor=_as_uuid(vendor_id),
            item=_as_uuid(item_id),
            as_of=moment,
        )
        if contract is not None:
            price = dec(contract["unit_price"]) * (1 - dec(contract["discount_percent"]) / 100)
            return money(price)
        price_row = await self._one(
            """
            SELECT ivp.unit_price FROM procurement.item_vendor_price ivp
              JOIN procurement.item_vendor iv ON iv.tenant_id = ivp.tenant_id AND iv.id = ivp.item_vendor_id
             WHERE ivp.tenant_id = :tenant_id AND iv.item_id = :item AND iv.vendor_id = :vendor
               AND ivp.currency_code = :currency AND ivp.effective_from <= :as_of
               AND (ivp.effective_to IS NULL OR ivp.effective_to >= :as_of)
               AND ivp.qty_min <= :qty AND (ivp.qty_max IS NULL OR ivp.qty_max >= :qty)
             ORDER BY ivp.qty_min DESC, ivp.effective_from DESC LIMIT 1
            """,
            vendor=_as_uuid(vendor_id),
            item=_as_uuid(item_id),
            currency=currency.upper(),
            qty=qty(quantity),
            as_of=moment,
        )
        if price_row is not None:
            return money(price_row["unit_price"])
        fallback = await self._scalar(
            "SELECT purchase_price FROM item_master.item_purchasing WHERE tenant_id = :tenant_id AND item_id = :item",
            item=_as_uuid(item_id),
        )
        if fallback is not None:
            return money(fallback)
        raise ValidationFailed("no vendor price or item purchase price found for this item/vendor")

    # ---------------------------------------------------------------------------- contracts ----
    async def create_contract(
        self, data: Mapping[str, Any], *, actor_id: str | None = None, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        await self._require_enabled()
        replay = await self._idem_begin("POST /api/v1/vendor-contracts", idempotency_key, data, actor_id)
        if replay:
            return replay["replay"]
        if not await self._flag("procurement.purchase_contracts_enabled", False) and not await self._setting(
            "purchase_contracts_enabled", False
        ):
            raise DomainDisabled("purchase contracts are disabled (procurement.purchase_contracts_enabled=false)")
        vendor = await self._require_row("vendor", data.get("vendor_id"), "vendor")
        contract_type = str(data.get("contract_type") or "CONTRACT").upper()
        if contract_type not in {"CONTRACT", "BLANKET_ORDER"}:
            raise ValidationFailed("invalid contract_type")
        contract_id = uuid.uuid4()
        try:
            await self._exec(
                """
                INSERT INTO procurement.vendor_contract
                    (id, tenant_id, contract_number, vendor_id, contract_type, start_date, end_date, status,
                     currency_code, terms, total_committed_amount, created_by, updated_by)
                VALUES (:id, :tenant_id, :number, :vendor, :type, :start, :end, 'DRAFT', :currency, CAST(:terms AS jsonb), :total, :actor, :actor)
                """,
                id=contract_id,
                number=str(data.get("contract_number") or f"CT{uuid.uuid4().hex[:8].upper()}"),
                vendor=_as_uuid(data["vendor_id"]),
                type=contract_type,
                start=_date(data.get("start_date")) or date.today(),
                end=_date(data.get("end_date")),
                currency=str(data.get("currency_code") or vendor["currency_code"]).upper(),
                terms=json.dumps(_jsonable(data.get("terms") or {})),
                total=money(data["total_committed_amount"]) if data.get("total_committed_amount") is not None else None,
                actor=_as_uuid(actor_id) if actor_id else None,
            )
        except IntegrityError as exc:
            raise DuplicateDocument("contract number already exists") from exc
        for line in data.get("items") or []:
            await self._exec(
                """
                INSERT INTO procurement.vendor_contract_item
                    (id, tenant_id, vendor_contract_id, item_id, contracted_qty, unit_price, discount_percent, uom_id)
                VALUES (:id, :tenant_id, :contract, :item, :qty, :price, :discount, :uom)
                """,
                id=uuid.uuid4(),
                contract=contract_id,
                item=_as_uuid(line["item_id"]),
                qty=qty(line.get("contracted_qty")),
                price=money(line.get("unit_price") or 0),
                discount=money(line.get("discount_percent") or 0),
                uom=_as_uuid(line["uom_id"]) if line.get("uom_id") else None,
            )
        await self._audit(
            "contract.created",
            "procurement.vendor_contract",
            contract_id,
            {"contract_number": str(data.get("contract_number"))},
            actor_id,
        )
        result = await self.get_contract(contract_id)
        await self._idem_complete("POST /api/v1/vendor-contracts", idempotency_key, result, contract_id, 201)
        return result

    async def get_contract(self, contract_id: Any) -> dict[str, Any]:
        contract = await self._require_row("vendor_contract", contract_id, "contract")
        contract["items"] = await self._all(
            "SELECT * FROM procurement.vendor_contract_item WHERE tenant_id = :tenant_id AND vendor_contract_id = :id ORDER BY item_id",
            id=_as_uuid(contract_id),
        )
        return contract

    async def list_contracts(
        self,
        *,
        vendor_id: str | None = None,
        status: str | None = None,
        contract_type: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        clauses = ["tenant_id = :tenant_id"]
        params: dict[str, Any] = {"limit": max(1, min(int(limit), 200))}
        if vendor_id:
            clauses.append("vendor_id = :vendor")
            params["vendor"] = _as_uuid(vendor_id)
        if status:
            clauses.append("status = :status")
            params["status"] = status.upper()
        if contract_type:
            clauses.append("contract_type = :type")
            params["type"] = contract_type.upper()
        return {
            "items": await self._all(
                f"SELECT * FROM procurement.vendor_contract WHERE {' AND '.join(clauses)} ORDER BY created_at DESC LIMIT :limit",
                **params,
            ),
            "next_cursor": None,
        }

    async def update_contract(
        self,
        contract_id: Any,
        data: Mapping[str, Any],
        *,
        actor_id: str | None = None,
        expected_version: int | None = None,
    ) -> dict[str, Any]:
        await self._require_row("vendor_contract", contract_id, "contract")
        values = {key: data[key] for key in ("end_date", "terms", "total_committed_amount") if key in data}
        if values:
            assignments = ", ".join(
                f"{key} = CAST(:{key} AS jsonb)" if key == "terms" else f"{key} = :{key}" for key in values
            )
            params = {
                key: (json.dumps(_jsonable(value)) if key == "terms" else _jsonable(value))
                for key, value in values.items()
            }
            await self._exec(
                "UPDATE procurement.vendor_contract SET " + assignments + ", updated_at = now(), version = version + 1 "
                "WHERE tenant_id = :tenant_id AND id = :id",
                id=_as_uuid(contract_id),
                **params,
            )
        return await self.get_contract(contract_id)

    async def _contract_transition(
        self, contract_id: Any, target: str, *, actor_id: str | None = None, reason: str | None = None
    ) -> dict[str, Any]:
        contract = await self._require_row("vendor_contract", contract_id, "contract")
        allowed = {
            "PENDING_APPROVAL": {"DRAFT"},
            "APPROVED": {"DRAFT", "PENDING_APPROVAL"},
            "ACTIVE": {"APPROVED"},
            "CLOSED": {"ACTIVE", "APPROVED"},
            "CANCELLED": {"DRAFT", "PENDING_APPROVAL"},
        }
        if contract["status"] not in allowed[target]:
            raise InvalidStateTransition(f"contract cannot transition {contract['status']} -> {target}")
        await self._exec(
            "UPDATE procurement.vendor_contract SET status = :status, updated_at = now(), version = version + 1 WHERE tenant_id = :tenant_id AND id = :id",
            status=target,
            id=_as_uuid(contract_id),
        )
        await self._audit(
            f"contract.{target.lower()}", "procurement.vendor_contract", contract_id, {"reason": reason}, actor_id
        )
        return await self.get_contract(contract_id)

    # ------------------------------------------------------------------------- requisitions ----
    async def create_requisition(
        self, data: Mapping[str, Any], *, actor_id: str | None = None, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        await self._require_enabled()
        replay = await self._idem_begin("POST /api/v1/purchase-requisitions", idempotency_key, data, actor_id)
        if replay:
            return replay["replay"]
        lines = list(data.get("lines") or [])
        if not lines:
            raise ValidationFailed("at least one requisition line is required")
        requested_by = data.get("requested_by") or actor_id
        if not requested_by:
            raise ValidationFailed("requested_by is required")
        requisition_id = uuid.uuid4()
        total = ZERO
        for index, line in enumerate(lines, start=1):
            if dec(line.get("qty")) <= 0:
                raise ValidationFailed(f"requisition line {index} qty must be > 0")
            total += dec(line.get("qty")) * dec(line.get("estimated_unit_price") or 0)
        await self._exec(
            """
            INSERT INTO procurement.purchase_requisition
                (id, tenant_id, document_number, transaction_date, requested_by, department_ref, status, estimated_total,
                 currency_code, memo, created_by, updated_by)
            VALUES (:id, :tenant_id, :number, :date, :requested_by, :department, 'DRAFT', :total, :currency, :memo, :actor, :actor)
            """,
            id=requisition_id,
            number=await self._next_number("PO"),
            date=_date(data.get("transaction_date")) or date.today(),
            requested_by=_as_uuid(requested_by),
            department=data.get("department_ref"),
            total=money(total),
            currency=str(data.get("currency_code") or "USD").upper(),
            memo=data.get("memo"),
            actor=_as_uuid(actor_id) if actor_id else None,
        )
        for index, line in enumerate(lines, start=1):
            await self._exec(
                """
                INSERT INTO procurement.purchase_requisition_line
                    (id, tenant_id, purchase_requisition_id, line_number, item_id, expense_account_ref, description, qty,
                     estimated_unit_price, uom_id, suggested_vendor_id)
                VALUES (:id, :tenant_id, :requisition, :line, :item, :expense_ref, :description, :qty, :price, :uom, :vendor)
                """,
                id=uuid.uuid4(),
                requisition=requisition_id,
                line=index,
                item=_as_uuid(line["item_id"]) if line.get("item_id") else None,
                expense_ref=line.get("expense_account_ref"),
                description=line.get("description"),
                qty=qty(line.get("qty")),
                price=money(line.get("estimated_unit_price") or 0),
                uom=_as_uuid(line["uom_id"]) if line.get("uom_id") else None,
                vendor=_as_uuid(line["suggested_vendor_id"]) if line.get("suggested_vendor_id") else None,
            )
        await self._audit(
            "requisition.created", "procurement.purchase_requisition", requisition_id, {"lines": len(lines)}, actor_id
        )
        result = await self.get_requisition(requisition_id)
        await self._idem_complete("POST /api/v1/purchase-requisitions", idempotency_key, result, requisition_id, 201)
        return result

    async def get_requisition(self, requisition_id: Any) -> dict[str, Any]:
        row = await self._require_row("purchase_requisition", requisition_id, "purchase requisition")
        row["lines"] = await self._all(
            "SELECT * FROM procurement.purchase_requisition_line WHERE tenant_id = :tenant_id AND purchase_requisition_id = :id ORDER BY line_number",
            id=_as_uuid(requisition_id),
        )
        return row

    async def list_requisitions(
        self,
        *,
        status: str | None = None,
        requested_by: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        clauses = ["tenant_id = :tenant_id"]
        params: dict[str, Any] = {"limit": max(1, min(int(limit), 200))}
        if status:
            clauses.append("status = :status")
            params["status"] = status.upper()
        if requested_by:
            clauses.append("requested_by = :requested_by")
            params["requested_by"] = _as_uuid(requested_by)
        if date_from:
            clauses.append("transaction_date >= :date_from")
            params["date_from"] = _date(date_from)
        if date_to:
            clauses.append("transaction_date <= :date_to")
            params["date_to"] = _date(date_to)
        return {
            "items": await self._all(
                f"SELECT * FROM procurement.purchase_requisition WHERE {' AND '.join(clauses)} ORDER BY created_at DESC LIMIT :limit",
                **params,
            ),
            "next_cursor": None,
        }

    async def update_requisition(
        self, requisition_id: Any, data: Mapping[str, Any], *, actor_id: str | None = None
    ) -> dict[str, Any]:
        requisition = await self._require_row("purchase_requisition", requisition_id, "purchase requisition")
        if requisition["status"] != "DRAFT":
            raise InvalidStateTransition("only DRAFT requisitions can be edited")
        values = {key: data[key] for key in ("department_ref", "currency_code", "memo") if key in data}
        if values:
            await self._exec(
                "UPDATE procurement.purchase_requisition SET "
                + ", ".join(f"{key} = :{key}" for key in values)
                + ", updated_at = now(), version = version + 1 "
                "WHERE tenant_id = :tenant_id AND id = :id",
                id=_as_uuid(requisition_id),
                **values,
            )
        if isinstance(data.get("lines"), list):
            await self._exec(
                "DELETE FROM procurement.purchase_requisition_line WHERE tenant_id = :tenant_id AND purchase_requisition_id = :id",
                id=_as_uuid(requisition_id),
            )
            total = ZERO
            for index, line in enumerate(data["lines"], start=1):
                total += dec(line.get("qty")) * dec(line.get("estimated_unit_price") or 0)
                await self._exec(
                    """
                    INSERT INTO procurement.purchase_requisition_line
                        (id, tenant_id, purchase_requisition_id, line_number, item_id, expense_account_ref, description, qty,
                         estimated_unit_price, uom_id, suggested_vendor_id)
                    VALUES (:id, :tenant_id, :requisition, :line, :item, :expense_ref, :description, :qty, :price, :uom, :vendor)
                    """,
                    id=uuid.uuid4(),
                    requisition=_as_uuid(requisition_id),
                    line=index,
                    item=_as_uuid(line["item_id"]) if line.get("item_id") else None,
                    expense_ref=line.get("expense_account_ref"),
                    description=line.get("description"),
                    qty=qty(line.get("qty")),
                    price=money(line.get("estimated_unit_price") or 0),
                    uom=_as_uuid(line["uom_id"]) if line.get("uom_id") else None,
                    vendor=_as_uuid(line["suggested_vendor_id"]) if line.get("suggested_vendor_id") else None,
                )
            await self._exec(
                "UPDATE procurement.purchase_requisition SET estimated_total = :total, updated_at = now(), version = version + 1 WHERE tenant_id = :tenant_id AND id = :id",
                total=money(total),
                id=_as_uuid(requisition_id),
            )
        await self._audit(
            "requisition.updated",
            "procurement.purchase_requisition",
            requisition_id,
            {"fields": sorted(data)},
            actor_id,
        )
        return await self.get_requisition(requisition_id)

    async def submit_requisition(
        self, requisition_id: Any, *, actor_id: str | None = None, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        requisition = await self._require_row("purchase_requisition", requisition_id, "purchase requisition")
        if requisition["status"] != "DRAFT":
            raise InvalidStateTransition(f"requisition is {requisition['status']}, expected DRAFT")
        amount = dec(requisition["estimated_total"])
        approval = await ApprovalService(self.session).submit(
            str(self.tenant_id),
            document_type="PURCHASE_REQUISITION",
            document_id=str(requisition_id),
            requested_by=str(requisition["requested_by"]),
            amount_base=amount,
            context={"amount": float(amount)},
        )
        request_id = approval["request"]["id"]
        status = "APPROVED" if approval["request"]["status"] == "APPROVED" else "PENDING_APPROVAL"
        await self._exec(
            "UPDATE procurement.purchase_requisition SET status = :status, approval_request_id = :approval, updated_at = now(), version = version + 1 "
            "WHERE tenant_id = :tenant_id AND id = :id",
            status=status,
            approval=_as_uuid(request_id),
            id=_as_uuid(requisition_id),
        )
        await self._audit(
            "requisition.submitted",
            "procurement.purchase_requisition",
            requisition_id,
            {"approval_request_id": str(request_id)},
            actor_id,
        )
        return {"id": str(requisition_id), "status": status, "approval_request_id": str(request_id)}

    async def decide_requisition(
        self,
        requisition_id: Any,
        decision: str,
        comment: str | None = None,
        *,
        actor_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        requisition = await self._require_row("purchase_requisition", requisition_id, "purchase requisition")
        if requisition["status"] not in {"PENDING_APPROVAL", "DRAFT"}:
            raise InvalidStateTransition(f"requisition is {requisition['status']}, expected PENDING_APPROVAL")
        decision = decision.upper()
        if decision not in {"APPROVE", "REJECT"}:
            raise ValidationFailed("decision must be APPROVE or REJECT")
        if requisition["approval_request_id"]:
            await ApprovalService(self.session).decide(
                str(self.tenant_id),
                str(requisition["approval_request_id"]),
                actor_id=str(actor_id),
                decision="APPROVE" if decision == "APPROVE" else "REJECT",
                comment=comment,
            )
        target = "APPROVED" if decision == "APPROVE" else "REJECTED"
        await self._exec(
            "UPDATE procurement.purchase_requisition SET status = :status, updated_at = now(), version = version + 1 WHERE tenant_id = :tenant_id AND id = :id",
            status=target,
            id=_as_uuid(requisition_id),
        )
        await self._audit(
            f"requisition.{target.lower()}",
            "procurement.purchase_requisition",
            requisition_id,
            {"comment": comment},
            actor_id,
        )
        return {"id": str(requisition_id), "status": target}

    async def order_requisition(
        self,
        requisition_id: Any,
        data: Mapping[str, Any],
        *,
        actor_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        requisition = await self._require_row("purchase_requisition", requisition_id, "purchase requisition")
        if requisition["status"] != "APPROVED":
            raise InvalidStateTransition("only APPROVED requisitions can be converted to purchase orders")
        selections = list(data.get("vendor_selection") or [])
        lines = await self._all(
            "SELECT * FROM procurement.purchase_requisition_line WHERE tenant_id = :tenant_id AND purchase_requisition_id = :id ORDER BY line_number",
            id=_as_uuid(requisition_id),
        )
        by_vendor: dict[str, list[dict[str, Any]]] = {}
        for line in lines:
            vendor_id = None
            for selection in selections:
                if selection.get("purchase_requisition_line_id") and str(
                    selection["purchase_requisition_line_id"]
                ) == str(line["id"]):
                    vendor_id = selection.get("vendor_id")
            vendor_id = vendor_id or line.get("suggested_vendor_id")
            if not vendor_id:
                raise ValidationFailed(f"no vendor selected for requisition line {line['line_number']}")
            by_vendor.setdefault(str(vendor_id), []).append(line)
        created = []
        for vendor_id, vendor_lines in by_vendor.items():
            po = await self.create_purchase_order(
                {
                    "vendor_id": vendor_id,
                    "po_type": "STANDARD",
                    "transaction_date": data.get("po_date") or date.today(),
                    "lines": [
                        {
                            "item_id": line["item_id"],
                            "expense_account_ref": line["expense_account_ref"],
                            "description": line["description"],
                            "qty_ordered": line["qty"],
                            "uom_id": line["uom_id"],
                            "unit_price": line["estimated_unit_price"],
                            "purchase_requisition_line_id": line["id"],
                        }
                        for line in vendor_lines
                    ],
                },
                actor_id=actor_id,
            )
            created.append(po)
        await self._exec(
            "UPDATE procurement.purchase_requisition SET status = 'ORDERED', updated_at = now(), version = version + 1 WHERE tenant_id = :tenant_id AND id = :id",
            id=_as_uuid(requisition_id),
        )
        await self._audit(
            "requisition.ordered",
            "procurement.purchase_requisition",
            requisition_id,
            {"purchase_orders": [po["id"] for po in created]},
            actor_id,
        )
        return {"purchase_orders": created}

    # ---------------------------------------------------------------------- purchase requests ----
    async def create_purchase_request(
        self, data: Mapping[str, Any], *, actor_id: str | None = None, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        await self._require_enabled()
        replay = await self._idem_begin("POST /api/v1/purchase-requests", idempotency_key, data, actor_id)
        if replay:
            return replay["replay"]
        lines = list(data.get("lines") or [])
        if not lines:
            raise ValidationFailed("at least one request line is required")
        request_id = uuid.uuid4()
        await self._exec(
            """
            INSERT INTO procurement.purchase_request
                (id, tenant_id, document_number, transaction_date, requested_by, status, currency_code, memo, created_by, updated_by)
            VALUES (:id, :tenant_id, :number, :date, :requested_by, 'DRAFT', :currency, :memo, :actor, :actor)
            """,
            id=request_id,
            number=await self._next_number("PO"),
            date=_date(data.get("transaction_date")) or date.today(),
            requested_by=_as_uuid(data.get("requested_by") or actor_id),
            currency=str(data.get("currency_code") or "USD").upper(),
            memo=data.get("memo"),
            actor=_as_uuid(actor_id) if actor_id else None,
        )
        for index, line in enumerate(lines, start=1):
            await self._exec(
                """
                INSERT INTO procurement.purchase_request_line
                    (id, tenant_id, purchase_request_id, line_number, item_id, expense_account_ref, description, qty,
                     anticipated_unit_price)
                VALUES (:id, :tenant_id, :request, :line, :item, :expense_ref, :description, :qty, :price)
                """,
                id=uuid.uuid4(),
                request=request_id,
                line=index,
                item=_as_uuid(line["item_id"]) if line.get("item_id") else None,
                expense_ref=line.get("expense_account_ref"),
                description=line.get("description"),
                qty=qty(line.get("qty")),
                price=money(line.get("anticipated_unit_price") or 0),
            )
        await self._audit(
            "purchase_request.created", "procurement.purchase_request", request_id, {"lines": len(lines)}, actor_id
        )
        await self._idem_complete(
            "POST /api/v1/purchase-requests", idempotency_key, {"id": str(request_id)}, request_id, 201
        )
        return await self.get_purchase_request(request_id)

    async def get_purchase_request(self, request_id: Any) -> dict[str, Any]:
        row = await self._require_row("purchase_request", request_id, "purchase request")
        row["lines"] = await self._all(
            "SELECT * FROM procurement.purchase_request_line WHERE tenant_id = :tenant_id AND purchase_request_id = :id ORDER BY line_number",
            id=_as_uuid(request_id),
        )
        return row

    async def list_purchase_requests(self, *, status: str | None = None, limit: int = 50) -> dict[str, Any]:
        clauses = ["tenant_id = :tenant_id"]
        params: dict[str, Any] = {"limit": max(1, min(int(limit), 200))}
        if status:
            clauses.append("status = :status")
            params["status"] = status.upper()
        return {
            "items": await self._all(
                f"SELECT * FROM procurement.purchase_request WHERE {' AND '.join(clauses)} ORDER BY created_at DESC LIMIT :limit",
                **params,
            ),
            "next_cursor": None,
        }

    async def decide_purchase_request(
        self, request_id: Any, decision: str, comment: str | None = None, *, actor_id: str | None = None
    ) -> dict[str, Any]:
        request = await self._require_row("purchase_request", request_id, "purchase request")
        if request["status"] not in {"DRAFT", "PENDING_APPROVAL"}:
            raise InvalidStateTransition(f"purchase request is {request['status']}")
        decision = decision.upper()
        if decision not in {"APPROVE", "REJECT"}:
            raise ValidationFailed("decision must be APPROVE or REJECT")
        target = "APPROVED" if decision == "APPROVE" else "CANCELLED"
        await self._exec(
            "UPDATE procurement.purchase_request SET status = :status, approved_at = CASE WHEN :status = 'APPROVED' THEN now() ELSE approved_at END, "
            "updated_at = now(), version = version + 1 WHERE tenant_id = :tenant_id AND id = :id",
            status=target,
            id=_as_uuid(request_id),
        )
        await self._audit(
            f"purchase_request.{target.lower()}",
            "procurement.purchase_request",
            request_id,
            {"comment": comment},
            actor_id,
        )
        return {"id": str(request_id), "status": target}

    async def convert_purchase_request(
        self, request_id: Any, data: Mapping[str, Any], *, actor_id: str | None = None
    ) -> dict[str, Any]:
        request = await self.get_purchase_request(request_id)
        if request["status"] != "APPROVED":
            raise InvalidStateTransition("only APPROVED purchase requests can be converted")
        vendor_id = data.get("vendor_id")
        if not vendor_id:
            raise ValidationFailed("vendor_id is required")
        po = await self.create_purchase_order(
            {
                "vendor_id": vendor_id,
                "po_type": "STANDARD",
                "transaction_date": data.get("po_date") or date.today(),
                "lines": [
                    {
                        "item_id": line["item_id"],
                        "expense_account_ref": line["expense_account_ref"],
                        "description": line["description"],
                        "qty_ordered": line["qty"],
                        "unit_price": line["anticipated_unit_price"],
                    }
                    for line in request["lines"]
                ],
            },
            actor_id=actor_id,
        )
        await self._exec(
            "UPDATE procurement.purchase_request SET status = 'ORDERED', updated_at = now(), version = version + 1 WHERE tenant_id = :tenant_id AND id = :id",
            id=_as_uuid(request_id),
        )
        return {"purchase_order_id": po["id"]}

    # ---------------------------------------------------------------------------------- RFQ ----
    async def create_rfq(
        self, data: Mapping[str, Any], *, actor_id: str | None = None, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        await self._require_enabled()
        replay = await self._idem_begin("POST /api/v1/rfqs", idempotency_key, data, actor_id)
        if replay:
            return replay["replay"]
        lines = list(data.get("lines") or [])
        if not lines:
            raise ValidationFailed("at least one RFQ line is required")
        rfq_id = uuid.uuid4()
        await self._exec(
            """
            INSERT INTO procurement.rfq (id, tenant_id, rfq_number, status, bid_open_date, bid_close_date, currency_code, memo, created_by, updated_by)
            VALUES (:id, :tenant_id, :number, 'DRAFT', :open, :close, :currency, :memo, :actor, :actor)
            """,
            id=rfq_id,
            number=str(data.get("rfq_number") or f"RFQ{uuid.uuid4().hex[:8].upper()}"),
            open=_date(data.get("bid_open_date")),
            close=_date(data.get("bid_close_date")),
            currency=str(data.get("currency_code") or "USD").upper(),
            memo=data.get("memo"),
            actor=_as_uuid(actor_id) if actor_id else None,
        )
        for index, line in enumerate(lines, start=1):
            await self._exec(
                "INSERT INTO procurement.rfq_line (id, tenant_id, rfq_id, line_number, item_id, qty, uom_id, target_price) "
                "VALUES (:id, :tenant_id, :rfq, :line, :item, :qty, :uom, :price)",
                id=uuid.uuid4(),
                rfq=rfq_id,
                line=index,
                item=_as_uuid(line["item_id"]),
                qty=qty(line.get("qty")),
                uom=_as_uuid(line["uom_id"]) if line.get("uom_id") else None,
                price=money(line["target_price"]) if line.get("target_price") is not None else None,
            )
        await self._audit("rfq.created", "procurement.rfq", rfq_id, {"lines": len(lines)}, actor_id)
        await self._idem_complete("POST /api/v1/rfqs", idempotency_key, {"id": str(rfq_id)}, rfq_id, 201)
        return await self.get_rfq(rfq_id)

    async def get_rfq(self, rfq_id: Any) -> dict[str, Any]:
        row = await self._require_row("rfq", rfq_id, "RFQ")
        row["lines"] = await self._all(
            "SELECT * FROM procurement.rfq_line WHERE tenant_id = :tenant_id AND rfq_id = :id ORDER BY line_number",
            id=_as_uuid(rfq_id),
        )
        row["responses"] = await self._all(
            "SELECT * FROM procurement.rfq_response WHERE tenant_id = :tenant_id AND rfq_id = :id ORDER BY received_at",
            id=_as_uuid(rfq_id),
        )
        return row

    async def list_rfqs(
        self, *, status: str | None = None, bid_close_before: str | None = None, limit: int = 50
    ) -> dict[str, Any]:
        clauses = ["tenant_id = :tenant_id"]
        params: dict[str, Any] = {"limit": max(1, min(int(limit), 200))}
        if status:
            clauses.append("status = :status")
            params["status"] = status.upper()
        if bid_close_before:
            clauses.append("bid_close_date <= :before")
            params["before"] = _date(bid_close_before)
        return {
            "items": await self._all(
                f"SELECT * FROM procurement.rfq WHERE {' AND '.join(clauses)} ORDER BY created_at DESC LIMIT :limit",
                **params,
            ),
            "next_cursor": None,
        }

    async def open_rfq(self, rfq_id: Any, *, actor_id: str | None = None) -> dict[str, Any]:
        rfq = await self._require_row("rfq", rfq_id, "RFQ")
        if rfq["status"] != "DRAFT":
            raise InvalidStateTransition(f"RFQ is {rfq['status']}, expected DRAFT")
        await self._exec(
            "UPDATE procurement.rfq SET status = 'OPEN', updated_at = now(), version = version + 1 WHERE tenant_id = :tenant_id AND id = :id",
            id=_as_uuid(rfq_id),
        )
        return {"id": str(rfq_id), "status": "OPEN"}

    async def register_rfq_response(
        self, rfq_id: Any, data: Mapping[str, Any], *, actor_id: str | None = None
    ) -> dict[str, Any]:
        rfq = await self._require_row("rfq", rfq_id, "RFQ")
        if rfq["status"] != "OPEN":
            raise InvalidStateTransition("RFQ is not OPEN")
        response_id = uuid.uuid4()
        await self._exec(
            "INSERT INTO procurement.rfq_response (id, tenant_id, rfq_id, vendor_id, status, currency_code, total_amount) "
            "VALUES (:id, :tenant_id, :rfq, :vendor, 'RECEIVED', :currency, :total)",
            id=response_id,
            rfq=_as_uuid(rfq_id),
            vendor=_as_uuid(data["vendor_id"]),
            currency=str(data.get("currency_code") or "USD").upper(),
            total=money(data.get("total_amount") or 0),
        )
        for line in data.get("lines") or []:
            await self._exec(
                "INSERT INTO procurement.rfq_response_line (id, tenant_id, rfq_response_id, rfq_line_id, unit_price, qty_quoted, lead_time_days) "
                "VALUES (:id, :tenant_id, :response, :rfq_line, :price, :qty, :lead)",
                id=uuid.uuid4(),
                response=response_id,
                rfq_line=_as_uuid(line["rfq_line_id"]),
                price=money(line.get("unit_price")),
                qty=qty(line.get("qty_quoted")),
                lead=int(line.get("lead_time_days") or 0),
            )
        await self._audit(
            "rfq.response_received", "procurement.rfq_response", response_id, {"rfq_id": str(rfq_id)}, actor_id
        )
        return {"response_id": str(response_id)}

    async def list_rfq_responses(self, rfq_id: Any) -> dict[str, Any]:
        await self._require_row("rfq", rfq_id, "RFQ")
        responses = await self._all(
            "SELECT * FROM procurement.rfq_response WHERE tenant_id = :tenant_id AND rfq_id = :id ORDER BY received_at",
            id=_as_uuid(rfq_id),
        )
        for response in responses:
            response["lines"] = await self._all(
                "SELECT * FROM procurement.rfq_response_line WHERE tenant_id = :tenant_id AND rfq_response_id = :id",
                id=_as_uuid(response["id"]),
            )
        return {"items": responses}

    async def award_rfq(self, rfq_id: Any, data: Mapping[str, Any], *, actor_id: str | None = None) -> dict[str, Any]:
        rfq = await self._require_row("rfq", rfq_id, "RFQ")
        if rfq["status"] not in {"OPEN", "CLOSED"}:
            raise InvalidStateTransition("RFQ must be OPEN or CLOSED to award")
        response = await self._require_row("rfq_response", data.get("response_id"), "RFQ response")
        if str(response["rfq_id"]) != str(rfq["id"]):
            raise ValidationFailed("response does not belong to this RFQ")
        await self._exec(
            "UPDATE procurement.rfq SET status = 'AWARDED', awarded_response_id = :response, updated_at = now(), version = version + 1 WHERE tenant_id = :tenant_id AND id = :id",
            response=_as_uuid(response["id"]),
            id=_as_uuid(rfq_id),
        )
        await self._exec(
            "UPDATE procurement.rfq_response SET status = CASE WHEN id = :winner THEN 'AWARDED' ELSE 'REJECTED' END, updated_at = now(), version = version + 1 "
            "WHERE tenant_id = :tenant_id AND rfq_id = :rfq",
            winner=_as_uuid(response["id"]),
            rfq=_as_uuid(rfq_id),
        )
        purchase_order_id = None
        if data.get("create_purchase_order", True):
            response_lines = await self._all(
                "SELECT * FROM procurement.rfq_response_line WHERE tenant_id = :tenant_id AND rfq_response_id = :id",
                id=_as_uuid(response["id"]),
            )
            po_lines = []
            for line in response_lines:
                rfq_line = await self._require_row("rfq_line", line["rfq_line_id"], "RFQ line")
                po_lines.append(
                    {
                        "item_id": rfq_line["item_id"],
                        "qty_ordered": line["qty_quoted"],
                        "uom_id": rfq_line.get("uom_id"),
                        "unit_price": line["unit_price"],
                    }
                )
            if po_lines:
                po = await self.create_purchase_order(
                    {
                        "vendor_id": response["vendor_id"],
                        "po_type": "STANDARD",
                        "transaction_date": data.get("po_date") or date.today(),
                        "lines": po_lines,
                    },
                    actor_id=actor_id,
                )
                purchase_order_id = po["id"]
        await self._audit(
            "rfq.awarded",
            "procurement.rfq",
            rfq_id,
            {"response_id": str(response["id"]), "purchase_order_id": purchase_order_id},
            actor_id,
        )
        return {"id": str(rfq_id), "status": "AWARDED", "purchase_order_id": purchase_order_id}

    async def close_rfq(self, rfq_id: Any, *, actor_id: str | None = None) -> dict[str, Any]:
        rfq = await self._require_row("rfq", rfq_id, "RFQ")
        if rfq["status"] not in {"OPEN", "DRAFT"}:
            raise InvalidStateTransition(f"RFQ is {rfq['status']}, expected OPEN/DRAFT")
        await self._exec(
            "UPDATE procurement.rfq SET status = 'CLOSED', updated_at = now(), version = version + 1 WHERE tenant_id = :tenant_id AND id = :id",
            id=_as_uuid(rfq_id),
        )
        return {"id": str(rfq_id), "status": "CLOSED"}

    # ------------------------------------------------------------------------ purchase orders ----
    async def create_purchase_order(
        self, data: Mapping[str, Any], *, actor_id: str | None = None, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        await self._require_enabled()
        replay = await self._idem_begin("POST /api/v1/purchase-orders", idempotency_key, data, actor_id)
        if replay:
            return replay["replay"]
        vendor = await self._require_row("vendor", data.get("vendor_id"), "vendor")
        if vendor["is_inactive"]:
            raise ValidationFailed("vendor is inactive")
        po_type = str(data.get("po_type") or "STANDARD").upper()
        if po_type not in {"STANDARD", "BLANKET", "DROP_SHIP", "SPECIAL_ORDER"}:
            raise ValidationFailed("invalid po_type")
        lines = list(data.get("lines") or [])
        if not lines:
            raise ValidationFailed("at least one purchase order line is required")
        contract_id = data.get("vendor_contract_id")
        contract_items: dict[str, dict[str, Any]] = {}
        if po_type == "BLANKET":
            if not await self._flag("procurement.blanket_orders_enabled", False) and not await self._setting(
                "blanket_orders_enabled", False
            ):
                raise DomainDisabled("blanket purchase orders are disabled")
            if not contract_id:
                raise ValidationFailed("blanket POs require vendor_contract_id")
        if contract_id:
            contract = await self._require_row("vendor_contract", contract_id, "vendor contract")
            if contract["status"] not in {"ACTIVE", "APPROVED"}:
                raise InvalidStateTransition("vendor contract is not active")
            for item in await self._all(
                "SELECT * FROM procurement.vendor_contract_item WHERE tenant_id = :tenant_id AND vendor_contract_id = :id",
                id=_as_uuid(contract_id),
            ):
                contract_items[str(item["item_id"])] = item
        currency = str(data.get("currency_code") or vendor["currency_code"]).upper()
        po_id = uuid.uuid4()
        document_number = data.get("document_number") or await self._next_number("PO")
        transaction_date = _date(data.get("transaction_date")) or date.today()
        subtotal = ZERO
        prepared = []
        for index, line in enumerate(lines, start=1):
            item_id = line.get("item_id")
            if item_id:
                item = await self._item(item_id)
                if not item.get("is_active"):
                    raise ItemNotPurchasable(f"item {item['item_number']} is inactive")
            qty_ordered = qty(line.get("qty_ordered", line.get("qty")))
            if qty_ordered <= 0:
                raise ValidationFailed(f"line {index} qty_ordered must be > 0")
            unit_price = line.get("unit_price")
            if unit_price is None and item_id:
                unit_price = await self.resolve_unit_price(item_id, vendor["id"], qty_ordered, currency=currency)
            price = money(unit_price or 0)
            is_drop_ship = bool(line.get("is_drop_ship")) or po_type == "DROP_SHIP"
            is_special_order = bool(line.get("is_special_order")) or po_type == "SPECIAL_ORDER"
            if (is_drop_ship or is_special_order) and not line.get("external_sales_ref"):
                raise ValidationFailed("drop-ship/special-order lines require external_sales_ref")
            amount = money(qty_ordered * price)
            subtotal += amount
            contract_item_id = line.get("vendor_contract_item_id")
            if contract_id and item_id and not contract_item_id:
                contract_item = contract_items.get(str(item_id))
                if contract_item is not None:
                    contract_item_id = contract_item["id"]
                    if dec(contract_item["used_qty"]) + qty_ordered > dec(contract_item["contracted_qty"]):
                        raise ValidationFailed("contracted quantity exceeded")
            prepared.append((index, line, qty_ordered, price, contract_item_id, is_drop_ship, is_special_order))
        await self._exec(
            """
            INSERT INTO procurement.purchase_order
                (id, tenant_id, document_number, transaction_date, vendor_id, po_type, status, ship_to_location_id, bill_to,
                 vendor_contract_id, currency_code, exchange_rate, subtotal, total, expected_receipt_date, memo, created_by, updated_by)
            VALUES (:id, :tenant_id, :number, :date, :vendor, :type, 'DRAFT', :ship_to, :bill_to, :contract, :currency,
                    :rate, :subtotal, :total, :expected, :memo, :actor, :actor)
            """,
            id=po_id,
            number=document_number,
            date=transaction_date,
            vendor=_as_uuid(vendor["id"]),
            type=po_type,
            ship_to=_as_uuid(data["ship_to_location_id"]) if data.get("ship_to_location_id") else None,
            bill_to=json.dumps(_jsonable(data.get("bill_to") or {})),
            contract=_as_uuid(contract_id) if contract_id else None,
            currency=currency,
            rate=money(data.get("exchange_rate") or 1),
            subtotal=money(subtotal),
            total=money(subtotal),
            expected=_date(data.get("expected_receipt_date")),
            memo=data.get("memo"),
            actor=_as_uuid(actor_id) if actor_id else None,
        )
        for index, line, qty_ordered, price, contract_item_id, is_drop_ship, is_special_order in prepared:
            await self._exec(
                """
                INSERT INTO procurement.purchase_order_line
                    (id, tenant_id, purchase_order_id, line_number, item_id, expense_account_ref, description, qty_ordered,
                     uom_id, unit_price, currency_code, expected_receipt_date, ship_to_location_id, is_drop_ship,
                     is_special_order, external_sales_ref, vendor_contract_item_id)
                VALUES (:id, :tenant_id, :po, :line, :item, :expense_ref, :description, :qty, :uom, :price, :currency,
                        :expected, :ship_to, :dropship, :special, :external, :contract_item)
                """,
                id=uuid.uuid4(),
                po=po_id,
                line=index,
                item=_as_uuid(line["item_id"]) if line.get("item_id") else None,
                expense_ref=line.get("expense_account_ref"),
                description=line.get("description"),
                qty=qty_ordered,
                uom=_as_uuid(line["uom_id"]) if line.get("uom_id") else None,
                price=price,
                currency=currency,
                expected=_date(line.get("expected_receipt_date") or data.get("expected_receipt_date")),
                ship_to=_as_uuid(line["ship_to_location_id"]) if line.get("ship_to_location_id") else None,
                dropship=is_drop_ship,
                special=is_special_order,
                external=line.get("external_sales_ref"),
                contract_item=_as_uuid(contract_item_id) if contract_item_id else None,
            )
            if contract_item_id:
                await self._exec(
                    "UPDATE procurement.vendor_contract_item SET used_qty = used_qty + :qty, updated_at = now(), version = version + 1 "
                    "WHERE tenant_id = :tenant_id AND id = :id",
                    qty=qty_ordered,
                    id=_as_uuid(contract_item_id),
                )
        await self._emit(
            "po.created",
            "purchase_order",
            po_id,
            {
                "tenant_id": str(self.tenant_id),
                "purchase_order_id": str(po_id),
                "document_number": document_number,
                "vendor_id": str(vendor["id"]),
                "po_type": po_type,
                "status": "DRAFT",
            },
        )
        await self._audit(
            "po.created",
            "procurement.purchase_order",
            po_id,
            {"document_number": document_number, "lines": len(prepared)},
            actor_id,
        )
        result = await self.get_purchase_order(po_id)
        await self._idem_complete("POST /api/v1/purchase-orders", idempotency_key, result, po_id, 201)
        return result

    async def get_purchase_order(self, po_id: Any) -> dict[str, Any]:
        po = await self._require_row("purchase_order", po_id, "purchase order")
        lines = await self._all(
            "SELECT * FROM procurement.purchase_order_line WHERE tenant_id = :tenant_id AND purchase_order_id = :id ORDER BY line_number",
            id=_as_uuid(po_id),
        )
        for line in lines:
            line["open_for_receipt"] = _num(
                dec(line["qty_ordered"]) - dec(line["qty_received"]) - dec(line["qty_cancelled"])
            )
            line["open_for_billing"] = _num(
                dec(line["qty_received"]) - dec(line["qty_billed"]) - dec(line["qty_returned"])
            )
        po["lines"] = lines
        po["receipts"] = await self._all(
            "SELECT id, document_number, status, received_at FROM procurement.item_receipt WHERE tenant_id = :tenant_id AND purchase_order_id = :id ORDER BY received_at",
            id=_as_uuid(po_id),
        )
        po["bills"] = await self._all(
            "SELECT id, document_number, status, total FROM procurement.vendor_bill WHERE tenant_id = :tenant_id AND purchase_order_id = :id ORDER BY created_at",
            id=_as_uuid(po_id),
        )
        return po

    async def list_purchase_orders(
        self,
        *,
        vendor_id: str | None = None,
        status: str | None = None,
        po_type: str | None = None,
        expected_receipt_from: str | None = None,
        expected_receipt_to: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        clauses = ["tenant_id = :tenant_id"]
        params: dict[str, Any] = {"limit": max(1, min(int(limit), 200))}
        if vendor_id:
            clauses.append("vendor_id = :vendor")
            params["vendor"] = _as_uuid(vendor_id)
        if status:
            clauses.append("status = :status")
            params["status"] = status.upper()
        if po_type:
            clauses.append("po_type = :type")
            params["type"] = po_type.upper()
        if expected_receipt_from:
            clauses.append("expected_receipt_date >= :from_date")
            params["from_date"] = _date(expected_receipt_from)
        if expected_receipt_to:
            clauses.append("expected_receipt_date <= :to_date")
            params["to_date"] = _date(expected_receipt_to)
        return {
            "items": await self._all(
                f"SELECT * FROM procurement.purchase_order WHERE {' AND '.join(clauses)} ORDER BY created_at DESC LIMIT :limit",
                **params,
            ),
            "next_cursor": None,
        }

    async def update_purchase_order(
        self, po_id: Any, data: Mapping[str, Any], *, actor_id: str | None = None, expected_version: int | None = None
    ) -> dict[str, Any]:
        po = await self._require_row("purchase_order", po_id, "purchase order")
        if po["status"] not in {"DRAFT", "PENDING_APPROVAL"}:
            raise InvalidStateTransition(f"PO is {po['status']}; recall to DRAFT before editing")
        values = {
            key: data[key] for key in ("ship_to_location_id", "bill_to", "expected_receipt_date", "memo") if key in data
        }
        if values:
            assignments = ", ".join(
                f"{key} = CAST(:{key} AS jsonb)" if key == "bill_to" else f"{key} = :{key}" for key in values
            )
            params = {
                key: (
                    json.dumps(_jsonable(value))
                    if key == "bill_to"
                    else (_date(value) if key == "expected_receipt_date" else _jsonable(value))
                )
                for key, value in values.items()
            }
            await self._exec(
                "UPDATE procurement.purchase_order SET " + assignments + ", updated_at = now(), version = version + 1 "
                "WHERE tenant_id = :tenant_id AND id = :id",
                id=_as_uuid(po_id),
                **params,
            )
        await self._audit("po.updated", "procurement.purchase_order", po_id, {"fields": sorted(data)}, actor_id)
        return await self.get_purchase_order(po_id)

    async def submit_purchase_order(
        self, po_id: Any, *, actor_id: str | None = None, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        po = await self._require_row("purchase_order", po_id, "purchase order")
        if po["status"] != "DRAFT":
            raise InvalidStateTransition(f"PO is {po['status']}, expected DRAFT")
        amount = dec(po["total"])
        approval = await ApprovalService(self.session).submit(
            str(self.tenant_id),
            document_type="PURCHASE_ORDER",
            document_id=str(po_id),
            requested_by=str(po["created_by"] or po["tenant_id"]),
            amount_base=amount,
            context={"amount": float(amount)},
        )
        request_id = approval["request"]["id"]
        status = "APPROVED" if approval["request"]["status"] == "APPROVED" else "PENDING_APPROVAL"
        await self._exec(
            "UPDATE procurement.purchase_order SET status = :status, approval_request_id = :approval, updated_at = now(), version = version + 1 "
            "WHERE tenant_id = :tenant_id AND id = :id",
            status=status,
            approval=_as_uuid(request_id),
            id=_as_uuid(po_id),
        )
        await self._audit(
            "po.submitted", "procurement.purchase_order", po_id, {"approval_request_id": str(request_id)}, actor_id
        )
        return {"id": str(po_id), "status": status, "approval_request_id": str(request_id)}

    async def approve_purchase_order(
        self, po_id: Any, *, actor_id: str | None = None, comment: str | None = None, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        po = await self._require_row("purchase_order", po_id, "purchase order")
        if po["status"] not in {"DRAFT", "PENDING_APPROVAL"}:
            raise InvalidStateTransition(f"PO is {po['status']}, expected DRAFT/PENDING_APPROVAL")
        if po["approval_request_id"]:
            await ApprovalService(self.session).decide(
                str(self.tenant_id),
                str(po["approval_request_id"]),
                actor_id=str(actor_id),
                decision="APPROVE",
                comment=comment,
            )
        await self._exec(
            "UPDATE procurement.purchase_order SET status = 'APPROVED', updated_at = now(), version = version + 1 WHERE tenant_id = :tenant_id AND id = :id",
            id=_as_uuid(po_id),
        )
        await self._emit(
            "po.approved",
            "purchase_order",
            po_id,
            {
                "tenant_id": str(self.tenant_id),
                "purchase_order_id": str(po_id),
                "approved_by": str(actor_id),
                "total": str(po["total"]),
            },
        )
        await self._audit("po.approved", "procurement.purchase_order", po_id, {"comment": comment}, actor_id)
        return {"id": str(po_id), "status": "APPROVED"}

    async def reject_purchase_order(self, po_id: Any, reason: str, *, actor_id: str | None = None) -> dict[str, Any]:
        po = await self._require_row("purchase_order", po_id, "purchase order")
        if po["status"] != "PENDING_APPROVAL":
            raise InvalidStateTransition(f"PO is {po['status']}, expected PENDING_APPROVAL")
        if po["approval_request_id"]:
            await ApprovalService(self.session).decide(
                str(self.tenant_id),
                str(po["approval_request_id"]),
                actor_id=str(actor_id),
                decision="REJECT",
                comment=reason,
            )
        await self._exec(
            "UPDATE procurement.purchase_order SET status = 'DRAFT', updated_at = now(), version = version + 1 WHERE tenant_id = :tenant_id AND id = :id",
            id=_as_uuid(po_id),
        )
        await self._audit("po.rejected", "procurement.purchase_order", po_id, {"reason": reason}, actor_id)
        return {"id": str(po_id), "status": "DRAFT"}

    async def cancel_purchase_order(self, po_id: Any, reason: str, *, actor_id: str | None = None) -> dict[str, Any]:
        po = await self._require_row("purchase_order", po_id, "purchase order")
        if po["status"] in {"PARTIALLY_RECEIVED", "RECEIVED", "BILLED", "CLOSED", "CANCELLED", "VOID"}:
            raise InvalidStateTransition(f"PO is {po['status']}; cancel only before receipts")
        received = await self._scalar(
            "SELECT coalesce(sum(qty_received), 0) FROM procurement.purchase_order_line WHERE tenant_id = :tenant_id AND purchase_order_id = :id",
            id=_as_uuid(po_id),
        )
        if dec(received) > 0:
            raise DependentDocuments("PO has received quantities; close instead of cancel")
        await self._exec(
            "UPDATE procurement.purchase_order SET status = 'CANCELLED', updated_at = now(), version = version + 1 WHERE tenant_id = :tenant_id AND id = :id",
            id=_as_uuid(po_id),
        )
        await self._audit("po.cancelled", "procurement.purchase_order", po_id, {"reason": reason}, actor_id)
        return {"id": str(po_id), "status": "CANCELLED"}

    async def close_purchase_order(
        self,
        po_id: Any,
        *,
        line_ids: Sequence[Any] | None = None,
        reason: str | None = None,
        actor_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        po = await self._require_row("purchase_order", po_id, "purchase order")
        if po["status"] in {"CLOSED", "CANCELLED", "VOID"}:
            raise InvalidStateTransition(f"PO is already {po['status']}")
        lines = await self._all(
            "SELECT * FROM procurement.purchase_order_line WHERE tenant_id = :tenant_id AND purchase_order_id = :id",
            id=_as_uuid(po_id),
        )
        open_receipt = sum(
            (
                max(ZERO, dec(line["qty_ordered"]) - dec(line["qty_received"]) - dec(line["qty_cancelled"]))
                for line in lines
            ),
            ZERO,
        )
        open_billing = sum(
            (
                max(ZERO, dec(line["qty_received"]) - dec(line["qty_billed"]) - dec(line["qty_returned"]))
                for line in lines
            ),
            ZERO,
        )
        pending_receipts = int(
            await self._scalar(
                "SELECT count(*) FROM procurement.item_receipt WHERE tenant_id = :tenant_id AND purchase_order_id = :id AND status IN ('DRAFT','PENDING_POSTING')",
                id=_as_uuid(po_id),
            )
            or 0
        )
        if open_receipt > 0 or open_billing > 0 or pending_receipts:
            raise DependentDocuments(
                "PO cannot be closed while lines have open receipt/bill quantities or unposted receipts",
                details={
                    "open_for_receipt": str(open_receipt),
                    "open_for_billing": str(open_billing),
                    "unposted_receipts": pending_receipts,
                },
            )
        await self._exec(
            "UPDATE procurement.purchase_order_line SET is_closed = true WHERE tenant_id = :tenant_id AND purchase_order_id = :id",
            id=_as_uuid(po_id),
        )
        await self._exec(
            "UPDATE procurement.purchase_order SET status = 'CLOSED', updated_at = now(), version = version + 1 WHERE tenant_id = :tenant_id AND id = :id",
            id=_as_uuid(po_id),
        )
        await self._emit(
            "po.closed",
            "purchase_order",
            po_id,
            {
                "tenant_id": str(self.tenant_id),
                "purchase_order_id": str(po_id),
                "closed_by": str(actor_id),
                "reason": reason,
            },
        )
        await self._audit("po.closed", "procurement.purchase_order", po_id, {"reason": reason}, actor_id)
        return {"id": str(po_id), "status": "CLOSED"}

    async def po_status(self, po_id: Any) -> dict[str, Any]:
        po = await self.get_purchase_order(po_id)
        lines = po["lines"]
        return {
            "id": po["id"],
            "document_number": po["document_number"],
            "status": po["status"],
            "po_type": po["po_type"],
            "lines": [
                {
                    key: line[key]
                    for key in (
                        "id",
                        "line_number",
                        "item_id",
                        "qty_ordered",
                        "qty_received",
                        "qty_billed",
                        "qty_returned",
                        "qty_cancelled",
                        "open_for_receipt",
                        "open_for_billing",
                        "is_closed",
                    )
                }
                for line in lines
            ],
            "received_fully": all(dec(line["open_for_receipt"]) <= 0 for line in lines),
            "billed_fully": all(dec(line["open_for_billing"]) <= 0 for line in lines),
        }

    async def po_history(self, po_id: Any) -> dict[str, Any]:
        po = await self._require_row("purchase_order", po_id, "purchase order")
        audits = await self._all(
            "SELECT action, actor_id, occurred_at, diff FROM platform.audit_event WHERE tenant_id = :tenant_id AND resource_type = 'procurement.purchase_order' AND resource_id = :id ORDER BY occurred_at",
            id=str(po_id),
        )
        receipts = await self._all(
            "SELECT id, document_number, status, received_at FROM procurement.item_receipt WHERE tenant_id = :tenant_id AND purchase_order_id = :id ORDER BY received_at",
            id=_as_uuid(po_id),
        )
        bills = await self._all(
            "SELECT id, document_number, status, total FROM procurement.vendor_bill WHERE tenant_id = :tenant_id AND purchase_order_id = :id ORDER BY created_at",
            id=_as_uuid(po_id),
        )
        returns = await self._all(
            "SELECT id, document_number, status FROM procurement.vendor_return_authorization WHERE tenant_id = :tenant_id AND purchase_order_id = :id ORDER BY created_at",
            id=_as_uuid(po_id),
        )
        return {
            "purchase_order_id": str(po_id),
            "status": po["status"],
            "audit": audits,
            "receipts": receipts,
            "bills": bills,
            "returns": returns,
        }

    async def create_po_release(
        self, po_id: Any, data: Mapping[str, Any], *, actor_id: str | None = None, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        po = await self._require_row("purchase_order", po_id, "purchase order")
        if po["po_type"] != "BLANKET":
            raise InvalidStateTransition("releases are only valid for BLANKET purchase orders")
        if not await self._flag("procurement.blanket_orders_enabled", False) and not await self._setting(
            "blanket_orders_enabled", False
        ):
            raise DomainDisabled("blanket purchase orders are disabled")
        raw_lines = list(data.get("lines") or [])
        if not raw_lines:
            raise ValidationFailed("release lines are required")
        release_id = uuid.uuid4()
        release_number = int(
            data.get("release_number")
            or (
                await self._scalar(
                    "SELECT coalesce(max(release_number), 0) + 1 FROM procurement.purchase_order_release WHERE tenant_id = :tenant_id AND purchase_order_id = :id",
                    id=_as_uuid(po_id),
                )
                or 1
            )
        )
        po_lines = {
            str(line["id"]): line
            for line in await self._all(
                "SELECT * FROM procurement.purchase_order_line WHERE tenant_id = :tenant_id AND purchase_order_id = :id",
                id=_as_uuid(po_id),
            )
        }
        po_line_ids = []
        total_qty = ZERO
        for raw in raw_lines:
            source = po_lines.get(str(raw.get("purchase_order_line_id"))) if raw.get("purchase_order_line_id") else None
            if source is None:
                # allow resolving by contract item id
                for candidate in po_lines.values():
                    if raw.get("vendor_contract_item_id") and str(candidate.get("vendor_contract_item_id")) == str(
                        raw["vendor_contract_item_id"]
                    ):
                        source = candidate
            if source is None:
                raise ValidationFailed("release line does not reference a blanket PO line")
            qty_release = qty(raw.get("qty_released", raw.get("qty")))
            if qty_release <= 0 or qty_release > dec(source["qty_ordered"]) - dec(source["qty_received"]):
                raise QuantityExceedsOrder("release quantity exceeds remaining blanket quantity")
            po_line_ids.append((source, qty_release))
            total_qty += qty_release
        created_po = await self.create_purchase_order(
            {
                "vendor_id": po["vendor_id"],
                "po_type": "STANDARD",
                "transaction_date": data.get("release_date") or date.today(),
                "lines": [
                    {
                        "item_id": source["item_id"],
                        "qty_ordered": qty_release,
                        "unit_price": source["unit_price"],
                        "uom_id": source["uom_id"],
                        "external_sales_ref": source.get("external_sales_ref"),
                    }
                    for source, qty_release in po_line_ids
                ],
            },
            actor_id=actor_id,
        )
        await self._exec(
            """
            INSERT INTO procurement.purchase_order_release
                (id, tenant_id, purchase_order_id, release_number, release_date, qty_released, status, created_po_id, created_by, updated_by)
            VALUES (:id, :tenant_id, :po, :number, :release_date, :qty, 'RELEASED', :created_po, :actor, :actor)
            """,
            id=release_id,
            po=_as_uuid(po_id),
            number=release_number,
            release_date=data.get("release_date") or date.today(),
            qty=total_qty,
            created_po=_as_uuid(created_po["id"]),
            actor=_as_uuid(actor_id) if actor_id else None,
        )
        await self._audit(
            "po.release_created",
            "procurement.purchase_order",
            po_id,
            {"release_number": release_number, "created_po_id": created_po["id"]},
            actor_id,
        )
        return {
            "release_id": str(release_id),
            "created_po_id": created_po["id"],
            "release_number": release_number,
            "qty_released": str(total_qty),
        }

    async def list_po_releases(self, po_id: Any) -> dict[str, Any]:
        await self._require_row("purchase_order", po_id, "purchase order")
        return {
            "items": await self._all(
                "SELECT * FROM procurement.purchase_order_release WHERE tenant_id = :tenant_id AND purchase_order_id = :id ORDER BY release_number",
                id=_as_uuid(po_id),
            )
        }

    # ------------------------------------------------------------------------------ receipts ----
    async def _default_status_id(self) -> uuid.UUID | None:
        value = await self._scalar(
            "SELECT id FROM traceability.inventory_status WHERE tenant_id = :tenant_id AND is_default ORDER BY is_system DESC LIMIT 1"
        )
        return _as_uuid(value) if value else None

    async def _location_id(self, explicit: Any = None) -> uuid.UUID | None:
        if explicit:
            return _as_uuid(explicit)
        value = await self._scalar(
            "SELECT id FROM traceability.location WHERE tenant_id = :tenant_id AND NOT is_inactive ORDER BY created_at LIMIT 1"
        )
        return _as_uuid(value) if value else None

    async def _over_receipt_limit(self, po_line: Mapping[str, Any]) -> Decimal:
        ordered = dec(po_line["qty_ordered"])
        if not await self._flag("procurement.allow_over_receipt", False) and not await self._setting(
            "allow_over_receipt", False
        ):
            return ordered - dec(po_line["qty_cancelled"])
        tolerance = dec(await self._setting("over_receipt_tolerance_percent", 0))
        return ordered * (1 + tolerance / 100)

    async def create_receipt(
        self, data: Mapping[str, Any], *, actor_id: str | None = None, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        await self._require_enabled()
        replay = await self._idem_begin("POST /api/v1/item-receipts", idempotency_key, data, actor_id)
        if replay:
            return replay["replay"]
        po_id = data.get("purchase_order_id")
        po = await self._require_row("purchase_order", po_id, "purchase order") if po_id else None
        if po is not None:
            if po["po_type"] == "BLANKET":
                raise ValidationFailed("blanket POs cannot be receipted directly; release them first")
            if po["status"] not in {"APPROVED", "PENDING_RECEIPT", "PARTIALLY_RECEIVED"}:
                raise InvalidStateTransition(
                    f"PO is {po['status']}; only approved/partially received POs can be receipted"
                )
        vendor_id = data.get("vendor_id") or (po["vendor_id"] if po else None)
        if not vendor_id:
            raise ValidationFailed("vendor_id is required for standalone receipts")
        lines = list(data.get("lines") or [])
        if not lines:
            raise ValidationFailed("at least one receipt line is required")
        receipt_id = uuid.uuid4()
        deferred = bool(data.get("deferred_posting", False))
        status = "PENDING_POSTING" if deferred else "DRAFT"
        default_location = data.get("location_id") or (po.get("ship_to_location_id") if po else None)
        await self._exec(
            """
            INSERT INTO procurement.item_receipt
                (id, tenant_id, document_number, purchase_order_id, vendor_id, receipt_type, status, received_at,
                 location_id, currency_code, exchange_rate, deferred_posting, memo, created_by, updated_by)
            VALUES (:id, :tenant_id, :number, :po, :vendor, :type, :status, :received_at, :location, :currency, :rate,
                    :deferred, :memo, :actor, :actor)
            """,
            id=receipt_id,
            number=await self._next_number("IR"),
            po=_as_uuid(po_id) if po_id else None,
            vendor=_as_uuid(vendor_id),
            type=str(data.get("receipt_type") or "PO").upper(),
            status=status,
            received_at=_stamp(data.get("received_at")) or utc_now(),
            location=_as_uuid(default_location) if default_location else None,
            currency=str(data.get("currency_code") or (po["currency_code"] if po else "USD")).upper(),
            rate=money(data.get("exchange_rate") or (po["exchange_rate"] if po else 1)),
            deferred=deferred,
            memo=data.get("memo"),
            actor=_as_uuid(actor_id) if actor_id else None,
        )
        for index, line in enumerate(lines, start=1):
            item_id = line.get("item_id")
            await self._item(item_id)
            qty_received = qty(line.get("qty_received", line.get("qty")))
            if qty_received <= 0:
                raise ValidationFailed(f"receipt line {index} qty_received must be > 0")
            po_line = None
            if line.get("purchase_order_line_id"):
                po_line = await self._require_row(
                    "purchase_order_line", line["purchase_order_line_id"], "purchase order line"
                )
                if po:
                    po_line = await self._one(
                        "SELECT * FROM procurement.purchase_order_line WHERE tenant_id = :tenant_id AND id = :id FOR UPDATE",
                        id=_as_uuid(line["purchase_order_line_id"]),
                    )
                assert po_line is not None
                limit = await self._over_receipt_limit(po_line)
                already = dec(po_line["qty_received"])
                if already + qty_received > limit:
                    raise QuantityExceedsOrder(
                        "receipt quantity exceeds the PO open quantity / over-receipt tolerance",
                        details={
                            "purchase_order_line_id": str(po_line["id"]),
                            "limit": str(limit),
                            "already_received": str(already),
                        },
                    )
                if line.get("item_id") and str(line["item_id"]) != str(po_line["item_id"]):
                    raise ValidationFailed("receipt line item does not match the PO line item")
            location = line.get("location_id") or default_location
            status_id = line.get("inventory_status_id") or await self._default_status_id()
            line_id = uuid.uuid4()
            await self._exec(
                """
                INSERT INTO procurement.item_receipt_line
                    (id, tenant_id, item_receipt_id, line_number, purchase_order_line_id, item_id, description, qty_expected,
                     qty_received, uom_id, unit_cost, currency_code, exchange_rate, location_id, inventory_status_id,
                     is_landed_cost_estimated, expense_account_ref)
                VALUES (:id, :tenant_id, :receipt, :line, :po_line, :item, :description, :expected, :qty, :uom, :cost,
                        :currency, :rate, :location, :status, :landed, :expense_ref)
                """,
                id=line_id,
                receipt=receipt_id,
                line=index,
                po_line=_as_uuid(line["purchase_order_line_id"]) if line.get("purchase_order_line_id") else None,
                item=_as_uuid(item_id),
                description=line.get("description"),
                expected=qty(line.get("qty_expected") or 0),
                qty=qty_received,
                uom=_as_uuid(line["uom_id"]) if line.get("uom_id") else None,
                cost=money(line.get("unit_cost") or 0),
                currency=str(line.get("currency_code") or data.get("currency_code") or "USD").upper(),
                rate=money(line.get("exchange_rate") or data.get("exchange_rate") or 1),
                location=_as_uuid(location) if location else None,
                status=_as_uuid(status_id) if status_id else None,
                landed=bool(line.get("is_landed_cost_estimated", False)),
                expense_ref=line.get("expense_account_ref"),
            )
            details = line.get("details") or [{}]
            for detail in details:
                await self._exec(
                    """
                    INSERT INTO procurement.item_receipt_detail
                        (id, tenant_id, item_receipt_line_id, bin_id, lot_id, serial_id, inventory_status_id, ownership, qty_received)
                    VALUES (:id, :tenant_id, :line, :bin, :lot, :serial, :status, :ownership, :qty)
                    """,
                    id=uuid.uuid4(),
                    line=line_id,
                    bin=_as_uuid(detail["bin_id"]) if detail.get("bin_id") else None,
                    lot=_as_uuid(detail["lot_id"]) if detail.get("lot_id") else None,
                    serial=_as_uuid(detail["serial_id"]) if detail.get("serial_id") else None,
                    status=_as_uuid(detail.get("inventory_status_id") or status_id)
                    if (detail.get("inventory_status_id") or status_id)
                    else None,
                    ownership=str(detail.get("ownership") or "OWNED").upper(),
                    qty=qty(detail.get("qty_received", detail.get("qty", qty_received))),
                )
        landed_cost_ref = None
        if data.get("landed_cost_charges"):
            landed_cost_ref = await self._create_estimated_landed_cost_document(
                receipt_id, data.get("landed_cost_charges") or [], actor_id=actor_id
            )
        await self._audit(
            "receipt.created",
            "procurement.item_receipt",
            receipt_id,
            {"lines": len(lines), "deferred": deferred, "landed_cost_id": landed_cost_ref["id"] if landed_cost_ref else None},
            actor_id,
        )
        result = await self.get_receipt(receipt_id)
        if landed_cost_ref:
            result["landed_cost"] = landed_cost_ref
        await self._idem_complete("POST /api/v1/item-receipts", idempotency_key, result, receipt_id, 201)
        return result

    async def get_receipt(self, receipt_id: Any) -> dict[str, Any]:
        receipt = await self._require_row("item_receipt", receipt_id, "item receipt")
        lines = await self._all(
            "SELECT * FROM procurement.item_receipt_line WHERE tenant_id = :tenant_id AND item_receipt_id = :id ORDER BY line_number",
            id=_as_uuid(receipt_id),
        )
        for line in lines:
            line["details"] = await self._all(
                "SELECT * FROM procurement.item_receipt_detail WHERE tenant_id = :tenant_id AND item_receipt_line_id = :id",
                id=_as_uuid(line["id"]),
            )
        receipt["lines"] = lines
        return receipt

    async def list_receipts(
        self,
        *,
        purchase_order_id: str | None = None,
        status: str | None = None,
        received_from: str | None = None,
        received_to: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        clauses = ["tenant_id = :tenant_id"]
        params: dict[str, Any] = {"limit": max(1, min(int(limit), 200))}
        if purchase_order_id:
            clauses.append("purchase_order_id = :po")
            params["po"] = _as_uuid(purchase_order_id)
        if status:
            clauses.append("status = :status")
            params["status"] = status.upper()
        if received_from:
            clauses.append("received_at >= :from_date")
            params["from_date"] = _stamp(received_from)
        if received_to:
            clauses.append("received_at <= :to_date")
            params["to_date"] = _stamp(received_to)
        return {
            "items": await self._all(
                f"SELECT * FROM procurement.item_receipt WHERE {' AND '.join(clauses)} ORDER BY received_at DESC LIMIT :limit",
                **params,
            ),
            "next_cursor": None,
        }

    async def update_receipt(
        self, receipt_id: Any, data: Mapping[str, Any], *, actor_id: str | None = None
    ) -> dict[str, Any]:
        receipt = await self._require_row("item_receipt", receipt_id, "item receipt")
        if receipt["status"] not in {"DRAFT", "PENDING_POSTING"}:
            raise InvalidStateTransition(f"receipt is {receipt['status']}; only unposted receipts can be edited")
        values = {key: data[key] for key in ("received_at", "location_id", "memo", "deferred_posting") if key in data}
        if "received_at" in values:
            values["received_at"] = _stamp(values["received_at"])
        if values:
            await self._exec(
                "UPDATE procurement.item_receipt SET "
                + ", ".join(f"{key} = :{key}" for key in values)
                + ", updated_at = now(), version = version + 1 "
                "WHERE tenant_id = :tenant_id AND id = :id",
                id=_as_uuid(receipt_id),
                **values,
            )
        await self._audit("receipt.updated", "procurement.item_receipt", receipt_id, {"fields": sorted(data)}, actor_id)
        return await self.get_receipt(receipt_id)

    async def post_receipt(
        self, receipt_id: Any, *, actor_id: str | None = None, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        from app.modules.inventory_ledger.facade import PostingPort

        receipt = await self.get_receipt(receipt_id)
        if receipt["status"] not in {"DRAFT", "PENDING_POSTING"}:
            raise InvalidStateTransition(f"receipt is {receipt['status']}, expected DRAFT/PENDING_POSTING")
        if not receipt["lines"]:
            raise ValidationFailed("receipt has no lines to post")
        movements = []
        for line in receipt["lines"]:
            if not line.get("location_id"):
                raise ValidationFailed(f"receipt line {line['line_number']} has no location")
            if not line.get("inventory_status_id"):
                raise ValidationFailed(f"receipt line {line['line_number']} has no inventory status")
            details = line["details"] or []
            movements.append(
                {
                    "item_id": str(line["item_id"]),
                    "location_id": str(line["location_id"]),
                    "inventory_status_id": str(line["inventory_status_id"]),
                    "qty": str(qty(line["qty_received"])),
                    "movement_type": "RECEIPT",
                    "unit_cost": str(dec(line["unit_cost"])),
                    "source_line_ref": str(line["line_number"]),
                    "details": [
                        {
                            "bin_id": detail.get("bin_id") or None,
                            "lot_id": detail.get("lot_id") or None,
                            "serial_id": detail.get("serial_id") or None,
                            "inventory_status_id": detail.get("inventory_status_id") or line["inventory_status_id"],
                            "ownership": detail.get("ownership") or "OWNED",
                            "qty": str(qty(detail.get("qty_received", line["qty_received"]))),
                        }
                        for detail in details
                    ],
                }
            )
        port = PostingPort(self.session, self.tenant_id)
        result = await port.post_receipt(
            transaction_date=(receipt.get("received_at") or utc_now()).date()
            if isinstance(receipt.get("received_at"), datetime)
            else date.today(),
            source_doc_type="ITEM_RECEIPT",
            source_doc_id=str(receipt_id),
            idempotency_key=idempotency_key or f"receipt-post:{receipt_id}",
            movements=movements,
        )
        await self._exec(
            "UPDATE procurement.item_receipt SET status = 'POSTED', posted_transaction_id = :txn, updated_at = now(), version = version + 1 "
            "WHERE tenant_id = :tenant_id AND id = :id",
            txn=_as_uuid(result["transaction_id"]),
            id=_as_uuid(receipt_id),
        )
        if receipt.get("purchase_order_id"):
            cumulative = []
            for line in receipt["lines"]:
                if not line.get("purchase_order_line_id"):
                    continue
                updated = await self._one(
                    "UPDATE procurement.purchase_order_line SET qty_received = qty_received + :qty, updated_at = now(), version = version + 1 "
                    "WHERE tenant_id = :tenant_id AND id = :id RETURNING *",
                    qty=qty(line["qty_received"]),
                    id=_as_uuid(line["purchase_order_line_id"]),
                )
                if updated:
                    cumulative.append(
                        {"po_line_id": str(updated["id"]), "qty_received_cumulative": str(qty(updated["qty_received"]))}
                    )
            await self._refresh_po_status(receipt["purchase_order_id"])
            await self._emit(
                "po.receipted",
                "purchase_order",
                receipt["purchase_order_id"],
                {
                    "tenant_id": str(self.tenant_id),
                    "purchase_order_id": str(receipt["purchase_order_id"]),
                    "item_receipt_id": str(receipt_id),
                    "lines": cumulative,
                },
            )
        await self._emit(
            "inventory.receipt.posted",
            "item_receipt",
            receipt_id,
            {
                "tenant_id": str(self.tenant_id),
                "item_receipt_id": str(receipt_id),
                "inventory_transaction_id": result["transaction_id"],
                "location_id": str(receipt.get("location_id")) if receipt.get("location_id") else None,
                "lines": [
                    {
                        "item_receipt_line_id": str(line["id"]),
                        "item_id": str(line["item_id"]),
                        "qty_received": str(line["qty_received"]),
                    }
                    for line in receipt["lines"]
                ],
            },
        )
        landed_cost = await self._post_estimated_landed_cost_for_receipt(receipt, actor_id=actor_id)
        await self._audit(
            "receipt.posted",
            "procurement.item_receipt",
            receipt_id,
            {"transaction_id": result["transaction_id"], "landed_cost": landed_cost},
            actor_id,
        )
        response = {"id": str(receipt_id), "status": "POSTED", "posted_transaction_id": result["transaction_id"]}
        if landed_cost:
            response["landed_cost"] = landed_cost
        return response

    async def reverse_receipt(
        self, receipt_id: Any, reason: str, *, actor_id: str | None = None, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        from app.modules.inventory_ledger.facade import PostingPort

        receipt = await self.get_receipt(receipt_id)
        if receipt["status"] != "POSTED":
            raise InvalidStateTransition(f"receipt is {receipt['status']}, expected POSTED")
        dependents = int(
            await self._scalar(
                "SELECT count(*) FROM procurement.vendor_bill WHERE tenant_id = :tenant_id AND item_receipt_id = :receipt AND status NOT IN ('CANCELLED','VOID')",
                receipt=_as_uuid(receipt_id),
            )
            or 0
        )
        if dependents:
            raise DependentDocuments("receipt has dependent vendor bills", details={"vendor_bills": dependents})
        port = PostingPort(self.session, self.tenant_id)
        result = await port.reverse(
            str(receipt["posted_transaction_id"]),
            reason=reason,
            idempotency_key=idempotency_key or f"receipt-reverse:{receipt_id}",
        )
        landed_cost_reversal = await self._reverse_estimated_landed_cost_for_receipt(
            receipt, reason=reason, actor_id=actor_id
        )
        await self._exec(
            "UPDATE procurement.item_receipt SET status = 'REVERSED', reversal_transaction_id = :txn, updated_at = now(), version = version + 1 "
            "WHERE tenant_id = :tenant_id AND id = :id",
            txn=_as_uuid(result["transaction_id"]),
            id=_as_uuid(receipt_id),
        )
        if receipt.get("purchase_order_id"):
            for line in receipt["lines"]:
                if not line.get("purchase_order_line_id"):
                    continue
                await self._exec(
                    "UPDATE procurement.purchase_order_line SET qty_received = GREATEST(qty_received - :qty, 0), updated_at = now(), version = version + 1 "
                    "WHERE tenant_id = :tenant_id AND id = :id",
                    qty=qty(line["qty_received"]),
                    id=_as_uuid(line["purchase_order_line_id"]),
                )
            await self._refresh_po_status(receipt["purchase_order_id"])
        await self._emit(
            "inventory.receipt.reversed",
            "item_receipt",
            receipt_id,
            {
                "tenant_id": str(self.tenant_id),
                "item_receipt_id": str(receipt_id),
                "reversal_transaction_id": result["transaction_id"],
            },
        )
        await self._audit(
            "receipt.reversed",
            "procurement.item_receipt",
            receipt_id,
            {"reason": reason, "reversal": result["transaction_id"]},
            actor_id,
        )
        response = {"id": str(receipt_id), "status": "REVERSED", "reversal_transaction_id": result["transaction_id"]}
        if landed_cost_reversal:
            response["landed_cost_reversal"] = landed_cost_reversal
        return response

    async def _refresh_po_status(self, po_id: Any) -> str:
        po = await self.get_purchase_order(po_id)
        if po["status"] in {"CLOSED", "CANCELLED", "VOID"}:
            return po["status"]
        lines = po["lines"]
        received_all = all(
            dec(line["qty_received"]) >= dec(line["qty_ordered"]) - dec(line["qty_cancelled"]) for line in lines
        )
        any_received = any(dec(line["qty_received"]) > 0 for line in lines)
        billed_all = received_all and all(
            dec(line["qty_received"]) - dec(line["qty_billed"]) - dec(line["qty_returned"]) <= 0 for line in lines
        )
        if billed_all and any_received:
            status = "BILLED"
        elif received_all and any_received:
            status = "RECEIVED"
        elif any_received:
            status = "PARTIALLY_RECEIVED"
        else:
            status = po["status"] if po["status"] in {"APPROVED", "PENDING_RECEIPT"} else "PENDING_RECEIPT"
        if status != po["status"]:
            await self._exec(
                "UPDATE procurement.purchase_order SET status = :status, updated_at = now(), version = version + 1 WHERE tenant_id = :tenant_id AND id = :id",
                status=status,
                id=_as_uuid(po_id),
            )
        return status

    async def received_not_billed(
        self,
        *,
        vendor_id: str | None = None,
        purchase_order_id: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        clauses = ["r.tenant_id = :tenant_id", "r.status = 'POSTED'"]
        params: dict[str, Any] = {"limit": max(1, min(int(limit), 500))}
        if vendor_id:
            clauses.append("r.vendor_id = :vendor")
            params["vendor"] = _as_uuid(vendor_id)
        if purchase_order_id:
            clauses.append("r.purchase_order_id = :po")
            params["po"] = _as_uuid(purchase_order_id)
        if date_from:
            clauses.append("r.received_at >= :date_from")
            params["date_from"] = _stamp(date_from)
        if date_to:
            clauses.append("r.received_at <= :date_to")
            params["date_to"] = _stamp(date_to)
        rows = await self._all(
            f"""
            SELECT r.id AS item_receipt_id, r.document_number, r.vendor_id, r.purchase_order_id, rl.id AS item_receipt_line_id,
                   rl.item_id, rl.qty_received, rl.unit_cost,
                   coalesce((SELECT sum(bl.qty) FROM procurement.vendor_bill_line bl
                             JOIN procurement.vendor_bill b ON b.tenant_id = bl.tenant_id AND b.id = bl.vendor_bill_id
                             WHERE bl.tenant_id = :tenant_id AND bl.item_receipt_line_id = rl.id AND b.status NOT IN ('CANCELLED','VOID')), 0) AS qty_billed,
                   coalesce((SELECT sum(bl.amount) FROM procurement.vendor_bill_line bl
                             JOIN procurement.vendor_bill b ON b.tenant_id = bl.tenant_id AND b.id = bl.vendor_bill_id
                             WHERE bl.tenant_id = :tenant_id AND bl.item_receipt_line_id = rl.id AND b.status NOT IN ('CANCELLED','VOID')), 0) AS amount_billed
              FROM procurement.item_receipt r
              JOIN procurement.item_receipt_line rl ON rl.tenant_id = r.tenant_id AND rl.item_receipt_id = r.id
             WHERE {" AND ".join(clauses)}
             ORDER BY r.received_at DESC LIMIT :limit
            """,
            **params,
        )
        for row in rows:
            received_value = dec(row["qty_received"]) * dec(row["unit_cost"])
            row["received_value"] = _num(money(received_value))
            row["remaining_qty"] = _num(qty(dec(row["qty_received"]) - dec(row["qty_billed"])))
            row["remaining_accrual"] = _num(money(received_value - dec(row["amount_billed"])))
        return {"items": rows}

    # ------------------------------------------------------------------------------- billing ----
    async def _tolerances(self, item_id: Any = None, vendor_id: Any = None) -> tuple[Decimal, Decimal]:
        amount = dec(await self._setting("bill_match_tolerance_amount", "0.01"))
        percent = dec(await self._setting("bill_match_tolerance_percent", "1.0"))
        return amount, percent

    def _within_tolerance(
        self, difference: Decimal, base: Decimal, tolerance_amount: Decimal, tolerance_percent: Decimal
    ) -> bool:
        if abs(difference) <= tolerance_amount:
            return True
        if base != 0 and abs(difference) / abs(base) * 100 <= tolerance_percent:
            return True
        return False

    async def _insert_finance_intent(
        self, *, bill: Mapping[str, Any], amount: Decimal, rule: str, account_ref: str, debit: bool
    ) -> None:
        await self._exec(
            """
            INSERT INTO platform.finance_journal_intent
                (id, tenant_id, intent_line_no, source_doc_type, source_doc_id, posting_rule_id, account_ref,
                 debit_amount_base, credit_amount_base, currency_code, exchange_rate, status, recorded_at)
            VALUES (:id, :tenant_id, :line, 'VENDOR_BILL', :bill, :rule, :account,
                    :debit, :credit, :currency, :rate, 'RECORDED', now())
            """,
            id=uuid.uuid4(),
            line=1 if debit else 2,
            bill=_as_uuid(bill["id"]),
            rule=rule,
            account=account_ref,
            debit=amount if debit else ZERO,
            credit=ZERO if debit else amount,
            currency=bill["currency_code"],
            rate=dec(bill.get("exchange_rate") or 1),
        )

    async def create_bill(
        self, data: Mapping[str, Any], *, actor_id: str | None = None, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        await self._require_enabled()
        replay = await self._idem_begin("POST /api/v1/vendor-bills", idempotency_key, data, actor_id)
        if replay:
            return replay["replay"]
        vendor = await self._require_row("vendor", data.get("vendor_id"), "vendor")
        bill_number = str(data.get("vendor_bill_number") or "").strip()
        if not bill_number:
            raise ValidationFailed("vendor_bill_number is required")
        if await self._scalar(
            "SELECT 1 FROM procurement.vendor_bill WHERE tenant_id = :tenant_id AND vendor_id = :vendor AND vendor_bill_number = :number",
            vendor=_as_uuid(vendor["id"]),
            number=bill_number,
        ):
            raise DuplicateDocument("duplicate vendor bill number (PROC-BILL-DUP)")
        receipt_id = data.get("item_receipt_id")
        if receipt_id:
            receipt = await self._require_row("item_receipt", receipt_id, "item receipt")
            if receipt["status"] != "POSTED":
                raise InvalidStateTransition("bills can only be matched to posted receipts")
        po_id = data.get("purchase_order_id")
        if po_id:
            await self._require_row("purchase_order", po_id, "purchase order")
        lines = list(data.get("lines") or [])
        if not lines:
            raise ValidationFailed("at least one bill line is required")
        bill_id = uuid.uuid4()
        currency = str(data.get("currency_code") or vendor["currency_code"]).upper()
        rate = money(data.get("exchange_rate") or 1)
        total = ZERO
        prepared = []
        for index, line in enumerate(lines, start=1):
            qty_value = qty(line.get("qty") or 0)
            unit_price = money(line.get("unit_price") or 0)
            amount = money(line["amount"]) if line.get("amount") is not None else money(qty_value * unit_price)
            total += amount
            if line.get("is_landed_cost"):
                if not line.get("item_receipt_line_id"):
                    raise ValidationFailed("landed-cost bill lines must reference a posted receipt line (DM-PROC-09)")
                receipt_line = await self._require_row(
                    "item_receipt_line", line["item_receipt_line_id"], "receipt line"
                )
                parent_receipt = await self._require_row("item_receipt", receipt_line["item_receipt_id"], "item receipt")
                if parent_receipt["status"] != "POSTED":
                    raise ValidationFailed("landed-cost bill line must reference a POSTED item receipt")
                if line.get("item_id") and str(line["item_id"]) != str(receipt_line["item_id"]):
                    raise ValidationFailed("landed-cost bill line item does not match the receipt line item")
                if amount < 0:
                    raise ValidationFailed("landed-cost bill line amount must be >= 0")
                if not line.get("item_id"):
                    line = {**line, "item_id": receipt_line["item_id"]}
            prepared.append((index, line, qty_value, unit_price, amount))
        await self._exec(
            """
            INSERT INTO procurement.vendor_bill
                (id, tenant_id, document_number, vendor_id, vendor_bill_number, purchase_order_id, item_receipt_id, status,
                 transaction_date, currency_code, exchange_rate, subtotal, total, amount_paid, variance_account_ref,
                 payment_terms, memo, created_by, updated_by)
            VALUES (:id, :tenant_id, :number, :vendor, :bill_number, :po, :receipt, 'DRAFT', :date, :currency, :rate,
                    :subtotal, :total, 0, :variance_account, :terms, :memo, :actor, :actor)
            """,
            id=bill_id,
            number=await self._next_number("VB"),
            vendor=_as_uuid(vendor["id"]),
            bill_number=bill_number,
            po=_as_uuid(po_id) if po_id else None,
            receipt=_as_uuid(receipt_id) if receipt_id else None,
            date=_date(data.get("transaction_date")) or date.today(),
            currency=currency,
            rate=rate,
            subtotal=money(total),
            total=money(total),
            variance_account=data.get("variance_account_ref"),
            terms=data.get("payment_terms") or vendor.get("payment_terms"),
            memo=data.get("memo"),
            actor=_as_uuid(actor_id) if actor_id else None,
        )
        for index, line, qty_value, unit_price, amount in prepared:
            await self._exec(
                """
                INSERT INTO procurement.vendor_bill_line
                    (id, tenant_id, vendor_bill_id, line_number, item_id, expense_account_ref, description, qty, uom_id,
                     unit_price, amount, purchase_order_line_id, item_receipt_line_id, is_landed_cost,
                     landed_cost_category_id, variance_amount)
                VALUES (:id, :tenant_id, :bill, :line, :item, :expense_ref, :description, :qty, :uom, :price, :amount,
                        :po_line, :receipt_line, :landed, :category, 0)
                """,
                id=uuid.uuid4(),
                bill=bill_id,
                line=index,
                item=_as_uuid(line["item_id"]) if line.get("item_id") else None,
                expense_ref=line.get("expense_account_ref"),
                description=line.get("description"),
                qty=qty_value,
                uom=_as_uuid(line["uom_id"]) if line.get("uom_id") else None,
                price=unit_price,
                amount=amount,
                po_line=_as_uuid(line["purchase_order_line_id"]) if line.get("purchase_order_line_id") else None,
                receipt_line=_as_uuid(line["item_receipt_line_id"]) if line.get("item_receipt_line_id") else None,
                landed=bool(line.get("is_landed_cost")),
                category=_as_uuid(line["landed_cost_category_id"]) if line.get("landed_cost_category_id") else None,
            )
        await self._exec(
            "INSERT INTO procurement.vendor_bill_match (id, tenant_id, vendor_bill_id, purchase_order_id, match_status) "
            "VALUES (:id, :tenant_id, :bill, :po, 'PENDING')",
            id=uuid.uuid4(),
            bill=bill_id,
            po=_as_uuid(po_id) if po_id else None,
        )
        await self._emit(
            "vendor_bill.created",
            "vendor_bill",
            bill_id,
            {
                "tenant_id": str(self.tenant_id),
                "vendor_bill_id": str(bill_id),
                "vendor_id": str(vendor["id"]),
                "source_doc_type": "PURCHASE_ORDER" if po_id else ("ITEM_RECEIPT" if receipt_id else "STANDALONE"),
                "source_doc_id": str(po_id or receipt_id) if (po_id or receipt_id) else None,
                "total": str(money(total)),
            },
        )
        await self._audit(
            "vendor_bill.created",
            "procurement.vendor_bill",
            bill_id,
            {"vendor_bill_number": bill_number, "total": str(money(total))},
            actor_id,
        )
        result = await self.get_bill(bill_id)
        await self._idem_complete("POST /api/v1/vendor-bills", idempotency_key, result, bill_id, 201)
        return result

    async def get_bill(self, bill_id: Any) -> dict[str, Any]:
        bill = await self._require_row("vendor_bill", bill_id, "vendor bill")
        bill["lines"] = await self._all(
            "SELECT * FROM procurement.vendor_bill_line WHERE tenant_id = :tenant_id AND vendor_bill_id = :id ORDER BY line_number",
            id=_as_uuid(bill_id),
        )
        bill["match"] = await self._one(
            "SELECT * FROM procurement.vendor_bill_match WHERE tenant_id = :tenant_id AND vendor_bill_id = :id",
            id=_as_uuid(bill_id),
        )
        bill["variances"] = await self._all(
            """
            SELECT v.* FROM procurement.vendor_bill_variance v
              JOIN procurement.vendor_bill_line l ON l.tenant_id = v.tenant_id AND l.id = v.vendor_bill_line_id
             WHERE v.tenant_id = :tenant_id AND l.vendor_bill_id = :id ORDER BY v.created_at
            """,
            id=_as_uuid(bill_id),
        )
        return bill

    async def list_bills(
        self,
        *,
        vendor_id: str | None = None,
        status: str | None = None,
        match_status: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        clauses = ["b.tenant_id = :tenant_id"]
        params: dict[str, Any] = {"limit": max(1, min(int(limit), 200))}
        if vendor_id:
            clauses.append("b.vendor_id = :vendor")
            params["vendor"] = _as_uuid(vendor_id)
        if status:
            clauses.append("b.status = :status")
            params["status"] = status.upper()
        if match_status:
            clauses.append("m.match_status = :match_status")
            params["match_status"] = match_status.upper()
        rows = await self._all(
            f"""
            SELECT b.*, m.match_status, m.exception_codes FROM procurement.vendor_bill b
              LEFT JOIN procurement.vendor_bill_match m ON m.tenant_id = b.tenant_id AND m.vendor_bill_id = b.id
             WHERE {" AND ".join(clauses)} ORDER BY b.created_at DESC LIMIT :limit
            """,
            **params,
        )
        return {"items": rows, "next_cursor": None}

    async def update_bill(
        self, bill_id: Any, data: Mapping[str, Any], *, actor_id: str | None = None
    ) -> dict[str, Any]:
        bill = await self._require_row("vendor_bill", bill_id, "vendor bill")
        if bill["status"] not in {"DRAFT", "PENDING_APPROVAL"}:
            raise InvalidStateTransition(f"bill is {bill['status']}; only unposted bills can be edited")
        values = {
            key: data[key]
            for key in ("transaction_date", "exchange_rate", "variance_account_ref", "payment_terms", "memo")
            if key in data
        }
        if "transaction_date" in values:
            values["transaction_date"] = _date(values["transaction_date"])
        if values:
            await self._exec(
                "UPDATE procurement.vendor_bill SET "
                + ", ".join(f"{key} = :{key}" for key in values)
                + ", updated_at = now(), version = version + 1 "
                "WHERE tenant_id = :tenant_id AND id = :id",
                id=_as_uuid(bill_id),
                **values,
            )
            await self._exec(
                "UPDATE procurement.vendor_bill_match SET match_status = 'PENDING', updated_at = now(), version = version + 1 WHERE tenant_id = :tenant_id AND vendor_bill_id = :id",
                id=_as_uuid(bill_id),
            )
        await self._audit("vendor_bill.updated", "procurement.vendor_bill", bill_id, {"fields": sorted(data)}, actor_id)
        return await self.get_bill(bill_id)

    async def match_bill(
        self,
        bill_id: Any,
        *,
        override: bool = False,
        override_reason: str | None = None,
        actor_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        bill = await self.get_bill(bill_id)
        if bill["status"] in {"OPEN", "PAID", "CANCELLED", "VOID"}:
            raise InvalidStateTransition(f"bill is {bill['status']}; matching applies to unposted bills")
        await self._exec(
            "DELETE FROM procurement.vendor_bill_variance WHERE tenant_id = :tenant_id AND vendor_bill_line_id IN "
            "(SELECT id FROM procurement.vendor_bill_line WHERE tenant_id = :tenant_id AND vendor_bill_id = :id) "
            "AND posted_transaction_id IS NULL",
            id=_as_uuid(bill_id),
        )
        tolerance_amount, tolerance_percent = await self._tolerances()
        exceptions: list[str] = []
        variance_candidates: list[dict[str, Any]] = []
        for line in bill["lines"]:
            if line.get("is_landed_cost"):
                if not line.get("item_receipt_line_id"):
                    exceptions.append("LANDED_COST_NO_RECEIPT")
                continue
            if not line.get("item_id"):
                continue
            po_line = None
            receipt_line = None
            if line.get("item_receipt_line_id"):
                receipt_line = await self._require_row(
                    "item_receipt_line", line["item_receipt_line_id"], "receipt line"
                )
                if receipt_line.get("purchase_order_line_id"):
                    po_line = await self._require_row(
                        "purchase_order_line", receipt_line["purchase_order_line_id"], "purchase order line"
                    )
                received_qty = dec(receipt_line["qty_received"])
                if po_line is not None:
                    # Cumulative received for the PO line (all posted receipts) is the billable quantity.
                    received_qty = max(ZERO, dec(po_line["qty_received"]) - dec(po_line["qty_returned"]))
                reference_price = dec(receipt_line.get("unit_cost") or 0)
            elif line.get("purchase_order_line_id"):
                po_line = await self._require_row(
                    "purchase_order_line", line["purchase_order_line_id"], "purchase order line"
                )
                received_qty = max(ZERO, dec(po_line["qty_received"]) - dec(po_line["qty_returned"]))
                reference_price = dec(po_line["unit_price"])
            else:
                exceptions.append("NO_RECEIPT")
                continue
            if po_line is not None:
                previously_billed = dec(
                    await self._scalar(
                        """
                    SELECT coalesce(sum(l.qty), 0) FROM procurement.vendor_bill_line l
                      JOIN procurement.vendor_bill b ON b.tenant_id = l.tenant_id AND b.id = l.vendor_bill_id
                     WHERE l.tenant_id = :tenant_id AND l.purchase_order_line_id = :po_line
                       AND l.vendor_bill_id <> :bill AND b.status NOT IN ('CANCELLED','VOID')
                    """,
                        po_line=_as_uuid(po_line["id"]),
                        bill=_as_uuid(bill_id),
                    )
                    or 0
                )
                available = received_qty - previously_billed
                if dec(line["qty"]) > available:
                    exceptions.append("QTY_BILLED_GT_RECEIVED")
                    variance_candidates.append(
                        {
                            "line": line,
                            "po_line": po_line,
                            "receipt_line": receipt_line,
                            "variance_type": "QUANTITY",
                            "amount": money((dec(line["qty"]) - available) * dec(line["unit_price"])),
                            "qty": qty(dec(line["qty"]) - available),
                            "account_ref": bill.get("variance_account_ref") or "VARIANCE",
                        }
                    )
                price_diff = money((dec(line["unit_price"]) - reference_price) * dec(line["qty"]))
                base = money(reference_price * dec(line["qty"]))
                if price_diff != 0 and not self._within_tolerance(
                    price_diff, base, tolerance_amount, tolerance_percent
                ):
                    exceptions.append("PRICE_DIFF_GT_TOL")
                    variance_candidates.append(
                        {
                            "line": line,
                            "po_line": po_line,
                            "receipt_line": receipt_line,
                            "variance_type": "PRICE",
                            "amount": price_diff,
                            "qty": ZERO,
                            "account_ref": bill.get("variance_account_ref") or "VARIANCE",
                        }
                    )
                po = await self._one(
                    "SELECT exchange_rate, currency_code FROM procurement.purchase_order WHERE tenant_id = :tenant_id AND id = :id",
                    id=_as_uuid(po_line["purchase_order_id"]),
                )
                if po and dec(po["exchange_rate"]) != dec(bill["exchange_rate"]):
                    fx_amount = money(
                        dec(line["unit_price"])
                        * dec(line["qty"])
                        * (dec(bill["exchange_rate"]) - dec(po["exchange_rate"]))
                    )
                    if fx_amount != 0 and not self._within_tolerance(
                        fx_amount,
                        money(dec(line["unit_price"]) * dec(line["qty"])),
                        tolerance_amount,
                        tolerance_percent,
                    ):
                        exceptions.append("FX_DIFF_GT_TOL")
                        variance_candidates.append(
                            {
                                "line": line,
                                "po_line": po_line,
                                "receipt_line": receipt_line,
                                "variance_type": "EXCHANGE_RATE",
                                "amount": fx_amount,
                                "qty": ZERO,
                                "account_ref": bill.get("variance_account_ref") or "VARIANCE",
                            }
                        )
        unique_exceptions = sorted(dict.fromkeys(exceptions))
        match_status = "MATCHED" if not unique_exceptions else "EXCEPTION"
        if override:
            match_status = "OVERRIDDEN"
        await self._exec(
            """
            UPDATE procurement.vendor_bill_match
               SET match_status = :status, tolerance_amount = :amount, tolerance_percent = :percent,
                   exception_codes = :codes, overridden_by = :overridden_by, overridden_at = :overridden_at,
                   notes = :notes, updated_at = now(), version = version + 1
             WHERE tenant_id = :tenant_id AND vendor_bill_id = :bill
            """,
            status=match_status,
            amount=tolerance_amount,
            percent=tolerance_percent,
            codes=unique_exceptions,
            overridden_by=_as_uuid(actor_id) if override and actor_id else None,
            overridden_at=utc_now() if override else None,
            notes=override_reason,
            bill=_as_uuid(bill_id),
        )
        variance_ids = []
        for candidate in variance_candidates:
            variance_id = uuid.uuid4()
            await self._exec(
                """
                INSERT INTO procurement.vendor_bill_variance
                    (id, tenant_id, vendor_bill_line_id, purchase_order_line_id, item_receipt_line_id, variance_type,
                     variance_amount, variance_qty, account_ref, status)
                VALUES (:id, :tenant_id, :line, :po_line, :receipt_line, :type, :amount, :qty, :account, 'POSTED')
                """,
                id=variance_id,
                line=_as_uuid(candidate["line"]["id"]),
                po_line=_as_uuid(candidate["po_line"]["id"]) if candidate["po_line"] else None,
                receipt_line=_as_uuid(candidate["receipt_line"]["id"]) if candidate["receipt_line"] else None,
                type=candidate["variance_type"],
                amount=candidate["amount"],
                qty=candidate["qty"],
                account=candidate["account_ref"],
            )
            variance_ids.append(str(variance_id))
        await self._audit(
            "vendor_bill.matched",
            "procurement.vendor_bill",
            bill_id,
            {"match_status": match_status, "exceptions": unique_exceptions, "override": override},
            actor_id,
        )
        if unique_exceptions or variance_ids:
            await self._emit(
                "vendor_bill.variance_created",
                "vendor_bill",
                bill_id,
                {
                    "tenant_id": str(self.tenant_id),
                    "vendor_bill_id": str(bill_id),
                    "variance_ids": variance_ids,
                    "exception_codes": unique_exceptions,
                },
            )
        if match_status == "EXCEPTION" and not override:
            return {
                "match_status": match_status,
                "exception_codes": unique_exceptions,
                "variances": variance_ids,
                "requires_override": True,
            }
        return {
            "match_status": match_status,
            "exception_codes": unique_exceptions,
            "variances": variance_ids,
            "requires_override": False,
        }

    async def submit_bill(
        self, bill_id: Any, *, actor_id: str | None = None, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        bill = await self._require_row("vendor_bill", bill_id, "vendor bill")
        if bill["status"] != "DRAFT":
            raise InvalidStateTransition(f"bill is {bill['status']}, expected DRAFT")
        amount = dec(bill["total"])
        approval = await ApprovalService(self.session).submit(
            str(self.tenant_id),
            document_type="VENDOR_BILL",
            document_id=str(bill_id),
            requested_by=str(actor_id or bill["tenant_id"]),
            amount_base=amount,
            context={"amount": float(amount)},
        )
        request_id = approval["request"]["id"]
        status = "APPROVED" if approval["request"]["status"] == "APPROVED" else "PENDING_APPROVAL"
        await self._exec(
            "UPDATE procurement.vendor_bill SET status = :status, approval_request_id = :approval, updated_at = now(), version = version + 1 "
            "WHERE tenant_id = :tenant_id AND id = :id",
            status=status,
            approval=_as_uuid(request_id),
            id=_as_uuid(bill_id),
        )
        return {"id": str(bill_id), "status": status, "approval_request_id": str(request_id)}

    async def approve_bill(
        self,
        bill_id: Any,
        *,
        actor_id: str | None = None,
        comment: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        bill = await self._require_row("vendor_bill", bill_id, "vendor bill")
        if bill["status"] not in {"DRAFT", "PENDING_APPROVAL"}:
            raise InvalidStateTransition(f"bill is {bill['status']}")
        if bill["approval_request_id"]:
            await ApprovalService(self.session).decide(
                str(self.tenant_id),
                str(bill["approval_request_id"]),
                actor_id=str(actor_id),
                decision="APPROVE",
                comment=comment,
            )
        await self._exec(
            "UPDATE procurement.vendor_bill SET status = 'APPROVED', updated_at = now(), version = version + 1 WHERE tenant_id = :tenant_id AND id = :id",
            id=_as_uuid(bill_id),
        )
        await self._audit("vendor_bill.approved", "procurement.vendor_bill", bill_id, {"comment": comment}, actor_id)
        return {"id": str(bill_id), "status": "APPROVED"}

    async def cancel_bill(self, bill_id: Any, reason: str, *, actor_id: str | None = None) -> dict[str, Any]:
        """Cancel a draft bill, or reverse a posted OPEN bill (T-04 + actual T-26 + T-06 variance true-up).

        Posted reversal is only allowed while the bill is unreconciled: fully/partially paid bills,
        closed/PAID bills, bills with applied vendor credits, and already reversed/cancelled bills are
        rejected (409) so a second call cannot double-reverse value or landed cost.
        """
        bill = await self.get_bill(bill_id)
        amount_paid = dec(bill.get("amount_paid") or 0)
        if amount_paid > 0:
            raise InvalidStateTransition(
                "partially/fully paid bills cannot be cancelled or reversed",
                details={"amount_paid": _num(amount_paid), "status": bill["status"]},
            )
        if bill["status"] in {"PAID"}:
            raise InvalidStateTransition("paid bills are closed and cannot be reversed", details={"status": "PAID"})
        if bill["status"] in {"CANCELLED", "VOID"}:
            raise InvalidStateTransition(
                f"bill is {bill['status']}; double cancellation/reversal is not allowed",
                details={"status": bill["status"]},
            )
        if bill["status"] == "OPEN":
            return await self._reverse_posted_bill(bill, reason, actor_id=actor_id)
        if bill["status"] not in {"DRAFT", "PENDING_APPROVAL"}:
            raise InvalidStateTransition(
                f"bill is {bill['status']}; only DRAFT/PENDING_APPROVAL cancel or posted OPEN reversal are allowed"
            )
        await self._exec(
            "UPDATE procurement.vendor_bill SET status = 'CANCELLED', updated_at = now(), version = version + 1 WHERE tenant_id = :tenant_id AND id = :id",
            id=_as_uuid(bill_id),
        )
        await self._audit("vendor_bill.cancelled", "procurement.vendor_bill", bill_id, {"reason": reason}, actor_id)
        return {"id": str(bill_id), "status": "CANCELLED"}

    async def _reverse_posted_bill(self, bill: Mapping[str, Any], reason: str, *, actor_id: Any = None) -> dict[str, Any]:
        """Reverse a posted OPEN bill: T-04, actual T-26 landed cost and T-06 absorption variance."""
        from app.modules.inventory_ledger.facade import PostingPort

        bill_id = bill["id"]
        applied_credits = int(
            await self._scalar(
                "SELECT count(*) FROM procurement.vendor_credit WHERE tenant_id = :tenant_id AND vendor_bill_id = :bill "
                "AND status = 'APPLIED'",
                bill=_as_uuid(bill_id),
            )
            or 0
        )
        if applied_credits:
            raise DependentDocuments(
                "bill has applied vendor credits; reverse/apply-off the credits before reversing the bill",
                details={"applied_vendor_credits": applied_credits},
            )
        port = PostingPort(self.session, self.tenant_id)
        reversal = await port.reverse(
            str(bill["posted_transaction_id"]),
            reason=reason,
            idempotency_key=f"vendor-bill:reverse:{bill_id}",
        )
        await self._link_reversal_document(
            link_type="VENDOR_BILL_REVERSAL",
            from_type="VENDOR_BILL",
            from_id=bill_id,
            to_type="INVENTORY_TRANSACTION",
            to_id=reversal["transaction_id"],
            amount=dec(bill["total"]),
            actor_id=actor_id,
        )
        landed_cost_reversals = await self._reverse_actual_landed_cost_for_bill(bill, reason=reason, actor_id=actor_id)
        variance_reversals = await self._reverse_landed_cost_absorption_variance_for_bill(
            bill, reason=reason, actor_id=actor_id
        )
        if bill.get("purchase_order_id"):
            for line in bill.get("lines") or []:
                if line.get("purchase_order_line_id"):
                    await self._exec(
                        "UPDATE procurement.purchase_order_line "
                        "SET qty_billed = GREATEST(qty_billed - :qty, 0), updated_at = now(), version = version + 1 "
                        "WHERE tenant_id = :tenant_id AND id = :id",
                        qty=qty(line["qty"]),
                        id=_as_uuid(line["purchase_order_line_id"]),
                    )
            await self._refresh_po_status(bill["purchase_order_id"])
        await self._exec(
            "UPDATE procurement.vendor_bill SET status = 'VOID', updated_at = now(), version = version + 1 "
            "WHERE tenant_id = :tenant_id AND id = :id",
            id=_as_uuid(bill_id),
        )
        summary = {
            "id": str(bill_id),
            "status": "VOID",
            "reversal_transaction_id": reversal["transaction_id"],
            "original_transaction_id": str(bill["posted_transaction_id"]),
            "reason": reason,
            "landed_cost_reversals": landed_cost_reversals,
            "variance_reversals": variance_reversals,
            "double_reverse_protected": True,
        }
        await self._audit("vendor_bill.reversed", "procurement.vendor_bill", bill_id, summary, actor_id)
        return summary

    async def _reverse_actual_landed_cost_for_bill(
        self, bill: Mapping[str, Any], *, reason: str, actor_id: Any = None
    ) -> list[dict[str, Any]]:
        """Reverse all posted actual (T-26) landed-cost documents linked to the bill (no double-reverse)."""
        from app.modules.costing.service import CostingService
        from app.modules.inventory_ledger.facade import PostingPort

        rows = await self._all(
            "SELECT * FROM costing.landed_cost WHERE tenant_id = :tenant_id AND vendor_bill_id = :bill "
            "AND NOT estimated ORDER BY created_at",
            bill=_as_uuid(bill["id"]),
        )
        costing = CostingService(self.session, self.tenant_id)
        port = PostingPort(self.session, self.tenant_id)
        results: list[dict[str, Any]] = []
        for row in rows:
            if row["status"] == "REVERSED":
                results.append({"landed_cost_id": str(row["id"]), "status": "REVERSED", "replayed": True})
                continue
            if row["status"] != "POSTED" or not row.get("posted_transaction_id"):
                raise InvalidStateTransition(
                    f"actual landed cost is {row['status']} and cannot be reversed",
                    details={"landed_cost_id": str(row["id"])},
                )
            reversal = await port.reverse(
                str(row["posted_transaction_id"]),
                reason=reason,
                idempotency_key=f"landed-cost:actual:reverse:{bill['id']}:{row['id']}",
            )
            await costing.transition_landed_cost(row["id"], "reverse")
            await self._link_reversal_document(
                link_type="LANDED_COST_ACTUAL_REVERSAL",
                from_type="VENDOR_BILL",
                from_id=bill["id"],
                to_type="COSTING_LANDED_COST",
                to_id=row["id"],
                amount=dec(row.get("total_amount") or 0),
                actor_id=actor_id,
            )
            summary = {
                "landed_cost_id": str(row["id"]),
                "status": "REVERSED",
                "posting_type": "T-26",
                "amount": _num(-dec(row.get("total_amount") or 0)),
                "original_transaction_id": str(row["posted_transaction_id"]),
                "reversal_transaction_id": reversal["transaction_id"],
                "value_only": True,
            }
            await self._audit("landed_cost.actual_reversed", "costing.landed_cost", row["id"], summary, actor_id)
            results.append(summary)
        return results

    async def _reverse_landed_cost_absorption_variance_for_bill(
        self, bill: Mapping[str, Any], *, reason: str, actor_id: Any = None
    ) -> list[dict[str, Any]]:
        """Reverse posted T-06 absorption variance postings and mark the costing variance rows CLOSED."""
        from app.modules.inventory_ledger.facade import PostingPort

        rows = await self._all(
            "SELECT * FROM costing.cost_variance WHERE tenant_id = :tenant_id "
            "AND variance_type = 'LANDED_COST_ABSORPTION' AND status = 'OPEN' "
            "AND detail ->> 'vendor_bill_id' = :bill ORDER BY created_at",
            bill=str(bill["id"]),
        )
        port = PostingPort(self.session, self.tenant_id)
        results: list[dict[str, Any]] = []
        for row in rows:
            reversal = None
            if row.get("inventory_transaction_id"):
                reversal = await port.reverse(
                    str(row["inventory_transaction_id"]),
                    reason=reason,
                    idempotency_key=f"landed-cost-variance:reverse:{row['id']}",
                )
            detail = dict(row.get("detail") or {})
            detail.update(
                {
                    "status": "CLOSED",
                    "processed_at": datetime.now(UTC).isoformat(),
                    "processed_by": str(actor_id) if actor_id else None,
                    "reversal_transaction_id": reversal["transaction_id"] if reversal else None,
                    "reversal_reason": reason,
                }
            )
            from app.modules.costing.repository import CostingRepository

            await CostingRepository(self.session, self.tenant_id).update_variance(
                row["id"], {"status": "CLOSED", "detail": detail}
            )
            await self._link_reversal_document(
                link_type="LANDED_COST_VARIANCE_REVERSAL",
                from_type="VENDOR_BILL",
                from_id=bill["id"],
                to_type="COST_VARIANCE",
                to_id=row["id"],
                amount=dec(row["amount_base"]),
                actor_id=actor_id,
            )
            summary = {
                "cost_variance_id": str(row["id"]),
                "status": "CLOSED",
                "posting_type": "T-06",
                "amount": _num(dec(row["amount_base"])),
                "original_transaction_id": str(row["inventory_transaction_id"])
                if row.get("inventory_transaction_id")
                else None,
                "reversal_transaction_id": reversal["transaction_id"] if reversal else None,
                "value_only": True,
            }
            await self._audit("landed_cost.absorption_variance_reversed", "costing.cost_variance", row["id"], summary, actor_id)
            results.append(summary)
        return results

    async def _link_reversal_document(
        self,
        *,
        link_type: str,
        from_type: str,
        from_id: Any,
        to_type: str,
        to_id: Any,
        amount: Decimal | None = None,
        actor_id: Any = None,
    ) -> None:
        await self._exec(
            """
            INSERT INTO platform.document_link
                (id, tenant_id, link_type, from_type, from_id, to_type, to_id, amount_base, created_by, updated_by)
            VALUES (:id, :tenant_id, :link_type, :from_type, :from_id, :to_type, :to_id, :amount, :actor, :actor)
            ON CONFLICT (tenant_id, link_type, from_type, from_id, to_type, to_id) DO NOTHING
            """,
            id=uuid.uuid4(),
            link_type=link_type,
            from_type=from_type,
            from_id=_as_uuid(from_id),
            to_type=to_type,
            to_id=_as_uuid(to_id),
            amount=amount,
            actor=_as_uuid(actor_id) if actor_id else None,
        )

    async def _bill_movements(self, bill: Mapping[str, Any]) -> list[dict[str, Any]]:
        movements = []
        for line in bill["lines"]:
            if not line.get("item_id") or line.get("is_landed_cost"):
                continue
            location = None
            status_id = None
            if line.get("item_receipt_line_id"):
                receipt_line = await self._require_row(
                    "item_receipt_line", line["item_receipt_line_id"], "receipt line"
                )
                location = receipt_line.get("location_id")
                status_id = receipt_line.get("inventory_status_id")
            if not location and bill.get("item_receipt_id"):
                receipt = await self._require_row("item_receipt", bill["item_receipt_id"], "item receipt")
                location = receipt.get("location_id")
            if not location and bill.get("purchase_order_id"):
                po = await self._require_row("purchase_order", bill["purchase_order_id"], "purchase order")
                location = po.get("ship_to_location_id")
            location = await self._location_id(location)
            status_id = status_id or await self._default_status_id()
            if not location or not status_id:
                raise ValidationFailed(
                    "bill line item needs a receipt/location/status to post the value-only ledger entry"
                )
            movements.append(
                {
                    "item_id": str(line["item_id"]),
                    "location_id": str(location),
                    "inventory_status_id": str(status_id),
                    "qty": "0",
                    "value_base": str(dec(line["amount"])),
                    "movement_type": "VENDOR_BILL",
                    "source_line_ref": str(line["line_number"]),
                    "details": [{"qty": "0", "inventory_status_id": str(status_id), "ownership": "OWNED"}],
                }
            )
        return movements

    async def post_bill(
        self, bill_id: Any, *, actor_id: str | None = None, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        from app.modules.inventory_ledger.facade import PostingPort

        bill = await self.get_bill(bill_id)
        if bill["status"] not in {"APPROVED", "DRAFT"}:
            raise InvalidStateTransition(f"bill is {bill['status']}, expected APPROVED")
        if bill["status"] == "DRAFT":
            await self._exec(
                "UPDATE procurement.vendor_bill SET status = 'APPROVED', updated_at = now(), version = version + 1 WHERE tenant_id = :tenant_id AND id = :id",
                id=_as_uuid(bill_id),
            )
            bill = await self.get_bill(bill_id)
        match = bill.get("match") or {}
        if match.get("match_status") not in {"MATCHED", "OVERRIDDEN"}:
            match_result = await self.match_bill(bill_id, actor_id=actor_id)
            if match_result["requires_override"]:
                match = (await self.get_bill(bill_id)).get("match") or {}
                if match.get("match_status") != "MATCHED":
                    raise MatchException(
                        "3-way match exceptions require an approved override before posting",
                        details={"exception_codes": match_result["exception_codes"]},
                    )
        movements = await self._bill_movements(bill)
        transaction_id = None
        if movements:
            port = PostingPort(self.session, self.tenant_id)
            result = await port.post_vendor_bill(
                transaction_date=bill["transaction_date"],
                source_doc_type="VENDOR_BILL",
                source_doc_id=str(bill_id),
                idempotency_key=idempotency_key or f"bill-post:{bill_id}",
                movements=movements,
            )
            transaction_id = result["transaction_id"]
        variance_rows = await self._all(
            """
            SELECT v.* FROM procurement.vendor_bill_variance v
              JOIN procurement.vendor_bill_line l ON l.tenant_id = v.tenant_id AND l.id = v.vendor_bill_line_id
             WHERE v.tenant_id = :tenant_id AND l.vendor_bill_id = :id AND v.posted_transaction_id IS NULL
            """,
            id=_as_uuid(bill_id),
        )
        port = PostingPort(self.session, self.tenant_id)
        for variance in variance_rows:
            line = await self._require_row("vendor_bill_line", variance["vendor_bill_line_id"], "bill line")
            item_id = line.get("item_id")
            if item_id:
                location = await self._location_id(None)
                status_id = await self._default_status_id()
                movements = [
                    {
                        "item_id": str(item_id),
                        "location_id": str(location),
                        "inventory_status_id": str(status_id),
                        "qty": "0",
                        "value_base": str(dec(variance["variance_amount"])),
                        "movement_type": "VARIANCE",
                        "source_line_ref": str(line["line_number"]),
                        "details": [{"qty": "0", "inventory_status_id": str(status_id), "ownership": "OWNED"}],
                    }
                ]
                variance_result = await port.post_variance(
                    transaction_date=bill["transaction_date"],
                    source_doc_type="VENDOR_BILL_VARIANCE",
                    source_doc_id=str(variance["id"]),
                    idempotency_key=f"bill-variance:{variance['id']}",
                    movements=movements,
                )
                await self._exec(
                    "UPDATE procurement.vendor_bill_variance SET posted_transaction_id = :txn, updated_at = now(), version = version + 1 "
                    "WHERE tenant_id = :tenant_id AND id = :id",
                    txn=_as_uuid(variance_result["transaction_id"]),
                    id=_as_uuid(variance["id"]),
                )
                await self._emit(
                    "vendor_bill.variance_posted",
                    "vendor_bill_variance",
                    variance["id"],
                    {
                        "tenant_id": str(self.tenant_id),
                        "vendor_bill_variance_id": str(variance["id"]),
                        "amount_base": str(dec(variance["variance_amount"])),
                        "posted_transaction_id": variance_result["transaction_id"],
                    },
                )
        await self._exec(
            "UPDATE procurement.vendor_bill SET status = 'OPEN', posted_transaction_id = :txn, updated_at = now(), version = version + 1 "
            "WHERE tenant_id = :tenant_id AND id = :id",
            txn=_as_uuid(transaction_id) if transaction_id else None,
            id=_as_uuid(bill_id),
        )
        if bill.get("purchase_order_id"):
            for line in bill["lines"]:
                if line.get("purchase_order_line_id"):
                    await self._exec(
                        "UPDATE procurement.purchase_order_line SET qty_billed = qty_billed + :qty, updated_at = now(), version = version + 1 "
                        "WHERE tenant_id = :tenant_id AND id = :id",
                        qty=qty(line["qty"]),
                        id=_as_uuid(line["purchase_order_line_id"]),
                    )
            po_status = await self._refresh_po_status(bill["purchase_order_id"])
            if po_status == "BILLED":
                await self._emit(
                    "po.billed",
                    "purchase_order",
                    bill["purchase_order_id"],
                    {
                        "tenant_id": str(self.tenant_id),
                        "purchase_order_id": str(bill["purchase_order_id"]),
                        "vendor_bill_id": str(bill_id),
                        "total_billed": str(dec(bill["total"])),
                    },
                )
        await self._insert_finance_intent(
            bill=bill,
            amount=dec(bill["total"]),
            rule="PROC_VENDOR_BILL_ACCRUAL",
            account_ref=bill.get("variance_account_ref") or "ACCRUED_PURCHASES",
            debit=True,
        )
        await self._insert_finance_intent(
            bill=bill,
            amount=dec(bill["total"]),
            rule="PROC_VENDOR_BILL_ACCRUAL",
            account_ref="ACCOUNTS_PAYABLE",
            debit=False,
        )
        await self._emit(
            "vendor_bill.posted",
            "vendor_bill",
            bill_id,
            {
                "tenant_id": str(self.tenant_id),
                "vendor_bill_id": str(bill_id),
                "total": str(dec(bill["total"])),
                "variance_amount": str(sum((dec(v["variance_amount"]) for v in variance_rows), ZERO)),
                "currency_code": bill["currency_code"],
                "posted_transaction_id": transaction_id,
            },
        )
        landed_cost = await self._post_actual_landed_cost_for_bill(bill, actor_id=actor_id)
        await self._audit(
            "vendor_bill.posted",
            "procurement.vendor_bill",
            bill_id,
            {"transaction_id": transaction_id, "landed_cost": landed_cost},
            actor_id,
        )
        result = await self.get_bill(bill_id)
        response = {
            "id": str(bill_id),
            "status": "OPEN",
            "posted_transaction_id": transaction_id,
            "variances": result["variances"],
        }
        if landed_cost:
            response["landed_cost"] = landed_cost
        return response

    async def bill_variances(self, bill_id: Any) -> dict[str, Any]:
        bill = await self.get_bill(bill_id)
        return {"items": bill["variances"], "match": bill["match"]}

    # ------------------------------------------------------------------------------- credits ----
    async def create_credit(
        self, data: Mapping[str, Any], *, actor_id: str | None = None, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        await self._require_enabled()
        replay = await self._idem_begin("POST /api/v1/vendor-credits", idempotency_key, data, actor_id)
        if replay:
            return replay["replay"]
        vendor = await self._require_row("vendor", data.get("vendor_id"), "vendor")
        lines = list(data.get("lines") or [])
        if not lines:
            raise ValidationFailed("at least one credit line is required")
        bill_id = data.get("vendor_bill_id")
        return_id = data.get("vendor_return_id")
        if bill_id:
            bill = await self._require_row("vendor_bill", bill_id, "vendor bill")
            if str(bill["vendor_id"]) != str(vendor["id"]):
                raise ValidationFailed("credit vendor does not match the bill vendor")
        if return_id:
            ra = await self._require_row("vendor_return_authorization", return_id, "vendor return")
            if str(ra["vendor_id"]) != str(vendor["id"]):
                raise ValidationFailed("credit vendor does not match the return vendor")
        credit_id = uuid.uuid4()
        total = ZERO
        prepared = []
        for index, line in enumerate(lines, start=1):
            qty_value = qty(line.get("qty") or 0)
            unit_price = money(line.get("unit_price") or 0)
            amount = money(line["amount"]) if line.get("amount") is not None else money(qty_value * unit_price)
            total += amount
            prepared.append((index, line, qty_value, unit_price, amount))
        await self._exec(
            """
            INSERT INTO procurement.vendor_credit
                (id, tenant_id, document_number, vendor_id, vendor_credit_number, status, transaction_date, currency_code,
                 exchange_rate, total, remaining_amount, vendor_bill_id, vendor_return_id, memo, created_by, updated_by)
            VALUES (:id, :tenant_id, :number, :vendor, :credit_number, 'DRAFT', :date, :currency, :rate, :total, :total,
                    :bill, :return_id, :memo, :actor, :actor)
            """,
            id=credit_id,
            number=await self._next_number("VC"),
            vendor=_as_uuid(vendor["id"]),
            credit_number=str(data.get("vendor_credit_number") or f"VC-{uuid.uuid4().hex[:10].upper()}"),
            date=_date(data.get("transaction_date")) or date.today(),
            currency=str(data.get("currency_code") or vendor["currency_code"]).upper(),
            rate=money(data.get("exchange_rate") or 1),
            total=money(total),
            bill=_as_uuid(bill_id) if bill_id else None,
            return_id=_as_uuid(return_id) if return_id else None,
            memo=data.get("memo"),
            actor=_as_uuid(actor_id) if actor_id else None,
        )
        for index, line, qty_value, unit_price, amount in prepared:
            await self._exec(
                """
                INSERT INTO procurement.vendor_credit_line
                    (id, tenant_id, vendor_credit_id, line_number, item_id, expense_account_ref, qty, unit_price, amount,
                     item_receipt_line_id)
                VALUES (:id, :tenant_id, :credit, :line, :item, :expense_ref, :qty, :price, :amount, :receipt_line)
                """,
                id=uuid.uuid4(),
                credit=credit_id,
                line=index,
                item=_as_uuid(line["item_id"]) if line.get("item_id") else None,
                expense_ref=line.get("expense_account_ref"),
                qty=qty_value,
                price=unit_price,
                amount=amount,
                receipt_line=_as_uuid(line["item_receipt_line_id"]) if line.get("item_receipt_line_id") else None,
            )
        await self._audit(
            "vendor_credit.created", "procurement.vendor_credit", credit_id, {"total": str(money(total))}, actor_id
        )
        await self._idem_complete(
            "POST /api/v1/vendor-credits", idempotency_key, {"id": str(credit_id)}, credit_id, 201
        )
        return await self.get_credit(credit_id)

    async def get_credit(self, credit_id: Any) -> dict[str, Any]:
        credit = await self._require_row("vendor_credit", credit_id, "vendor credit")
        credit["lines"] = await self._all(
            "SELECT * FROM procurement.vendor_credit_line WHERE tenant_id = :tenant_id AND vendor_credit_id = :id ORDER BY line_number",
            id=_as_uuid(credit_id),
        )
        return credit

    async def list_credits(
        self, *, vendor_id: str | None = None, status: str | None = None, limit: int = 50
    ) -> dict[str, Any]:
        clauses = ["tenant_id = :tenant_id"]
        params: dict[str, Any] = {"limit": max(1, min(int(limit), 200))}
        if vendor_id:
            clauses.append("vendor_id = :vendor")
            params["vendor"] = _as_uuid(vendor_id)
        if status:
            clauses.append("status = :status")
            params["status"] = status.upper()
        return {
            "items": await self._all(
                f"SELECT * FROM procurement.vendor_credit WHERE {' AND '.join(clauses)} ORDER BY created_at DESC LIMIT :limit",
                **params,
            ),
            "next_cursor": None,
        }

    async def apply_credit(
        self, credit_id: Any, applications: Sequence[Mapping[str, Any]], *, actor_id: str | None = None
    ) -> dict[str, Any]:
        credit = await self._require_row("vendor_credit", credit_id, "vendor credit")
        if credit["status"] not in {"OPEN", "APPROVED"}:
            raise InvalidStateTransition(f"credit is {credit['status']}; post the credit before applying it")
        for application in applications:
            amount = money(application.get("amount"))
            if amount <= 0:
                raise ValidationFailed("credit application amount must be > 0")
            bill = await self._require_row("vendor_bill", application.get("vendor_bill_id"), "vendor bill")
            if str(bill["vendor_id"]) != str(credit["vendor_id"]):
                raise ValidationFailed("credit and bill vendors differ")
            if bill["status"] not in {"OPEN", "APPROVED"}:
                raise InvalidStateTransition(f"bill is {bill['status']}; apply credits to open bills only")
            open_amount = money(dec(bill["total"]) - dec(bill["amount_paid"]))
            if amount > open_amount:
                raise CreditExceedsOpen(
                    "credit application exceeds the open bill amount", details={"open_amount": str(open_amount)}
                )
            if amount > dec(credit["remaining_amount"]):
                raise CreditExceedsOpen("credit application exceeds the remaining credit amount")
            await self._exec(
                "UPDATE procurement.vendor_bill SET amount_paid = amount_paid + :amount, updated_at = now(), version = version + 1 WHERE tenant_id = :tenant_id AND id = :id",
                amount=amount,
                id=_as_uuid(bill["id"]),
            )
            await self._exec(
                "UPDATE procurement.vendor_credit SET remaining_amount = remaining_amount - :amount, updated_at = now(), version = version + 1 "
                "WHERE tenant_id = :tenant_id AND id = :id",
                amount=amount,
                id=_as_uuid(credit_id),
            )
            credit = await self._require_row("vendor_credit", credit_id, "vendor credit")
        if dec(credit["remaining_amount"]) <= 0:
            await self._exec(
                "UPDATE procurement.vendor_credit SET status = 'APPLIED', updated_at = now(), version = version + 1 WHERE tenant_id = :tenant_id AND id = :id",
                id=_as_uuid(credit_id),
            )
        await self._audit(
            "vendor_credit.applied",
            "procurement.vendor_credit",
            credit_id,
            {"applications": [dict(app) for app in applications]},
            actor_id,
        )
        return await self.get_credit(credit_id)

    async def post_credit(
        self, credit_id: Any, *, actor_id: str | None = None, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        from app.modules.inventory_ledger.facade import PostingPort

        credit = await self.get_credit(credit_id)
        if credit["status"] not in {"DRAFT", "APPROVED"}:
            raise InvalidStateTransition(f"credit is {credit['status']}")
        movements = []
        for line in credit["lines"]:
            if not line.get("item_id"):
                continue
            location = await self._location_id(None)
            status_id = await self._default_status_id()
            if not location or not status_id:
                raise ValidationFailed("credit item line needs a default location/status to post")
            movements.append(
                {
                    "item_id": str(line["item_id"]),
                    "location_id": str(location),
                    "inventory_status_id": str(status_id),
                    "qty": "0",
                    "value_base": str(dec(line["amount"])),
                    "movement_type": "VENDOR_CREDIT",
                    "source_line_ref": str(line["line_number"]),
                    "details": [{"qty": "0", "inventory_status_id": str(status_id), "ownership": "OWNED"}],
                }
            )
        transaction_id = None
        if movements:
            port = PostingPort(self.session, self.tenant_id)
            result = await port.post_vendor_credit(
                transaction_date=credit["transaction_date"],
                source_doc_type="VENDOR_CREDIT",
                source_doc_id=str(credit_id),
                idempotency_key=idempotency_key or f"credit-post:{credit_id}",
                movements=movements,
            )
            transaction_id = result["transaction_id"]
        await self._exec(
            "UPDATE procurement.vendor_credit SET status = 'OPEN', posted_transaction_id = :txn, updated_at = now(), version = version + 1 "
            "WHERE tenant_id = :tenant_id AND id = :id",
            txn=_as_uuid(transaction_id) if transaction_id else None,
            id=_as_uuid(credit_id),
        )
        await self._emit(
            "vendor_credit.posted",
            "vendor_credit",
            credit_id,
            {
                "tenant_id": str(self.tenant_id),
                "vendor_credit_id": str(credit_id),
                "total": str(dec(credit["total"])),
                "applied_amount": str(dec(credit["total"]) - dec(credit["remaining_amount"])),
                "posted_transaction_id": transaction_id,
            },
        )
        await self._audit(
            "vendor_credit.posted", "procurement.vendor_credit", credit_id, {"transaction_id": transaction_id}, actor_id
        )
        return {"id": str(credit_id), "status": "OPEN", "posted_transaction_id": transaction_id}

    # ------------------------------------------------------------------------------- returns ----
    async def create_return(
        self, data: Mapping[str, Any], *, actor_id: str | None = None, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        await self._require_enabled()
        replay = await self._idem_begin("POST /api/v1/vendor-returns", idempotency_key, data, actor_id)
        if replay:
            return replay["replay"]
        vendor = await self._require_row("vendor", data.get("vendor_id"), "vendor")
        lines = list(data.get("lines") or [])
        if not lines:
            raise ValidationFailed("at least one return line is required")
        return_id = uuid.uuid4()
        await self._exec(
            """
            INSERT INTO procurement.vendor_return_authorization
                (id, tenant_id, document_number, vendor_id, purchase_order_id, status, reason_code, transaction_date,
                 currency_code, return_date, memo, created_by, updated_by)
            VALUES (:id, :tenant_id, :number, :vendor, :po, 'DRAFT', :reason, :date, :currency, :return_date, :memo, :actor, :actor)
            """,
            id=return_id,
            number=await self._next_number("VRA"),
            vendor=_as_uuid(vendor["id"]),
            po=_as_uuid(data["purchase_order_id"]) if data.get("purchase_order_id") else None,
            reason=data.get("reason_code"),
            date=_date(data.get("transaction_date")) or date.today(),
            currency=str(data.get("currency_code") or vendor["currency_code"]).upper(),
            return_date=_date(data.get("return_date")),
            memo=data.get("memo"),
            actor=_as_uuid(actor_id) if actor_id else None,
        )
        for index, line in enumerate(lines, start=1):
            item_id = line.get("item_id")
            if not item_id:
                raise ValidationFailed(f"return line {index} requires item_id")
            qty_authorized = qty(line.get("qty_authorized", line.get("qty")))
            if qty_authorized <= 0:
                raise ValidationFailed(f"return line {index} qty_authorized must be > 0")
            if line.get("purchase_order_line_id"):
                po_line = await self._require_row(
                    "purchase_order_line", line["purchase_order_line_id"], "purchase order line"
                )
                available = dec(po_line["qty_received"]) - dec(po_line["qty_returned"])
                if qty_authorized > available:
                    raise QuantityExceedsOrder("return quantity exceeds received/not-yet-returned quantity")
            await self._exec(
                """
                INSERT INTO procurement.vendor_return_authorization_line
                    (id, tenant_id, vendor_return_authorization_id, line_number, item_id, purchase_order_line_id,
                     item_receipt_line_id, qty_authorized, uom_id, unit_cost, location_id, bin_id, lot_id, serial_id)
                VALUES (:id, :tenant_id, :return_id, :line, :item, :po_line, :receipt_line, :qty, :uom, :cost, :location,
                        :bin, :lot, :serial)
                """,
                id=uuid.uuid4(),
                return_id=return_id,
                line=index,
                item=_as_uuid(item_id),
                po_line=_as_uuid(line["purchase_order_line_id"]) if line.get("purchase_order_line_id") else None,
                receipt_line=_as_uuid(line["item_receipt_line_id"]) if line.get("item_receipt_line_id") else None,
                qty=qty_authorized,
                uom=_as_uuid(line["uom_id"]) if line.get("uom_id") else None,
                cost=money(line.get("unit_cost") or 0),
                location=_as_uuid(line["location_id"]) if line.get("location_id") else None,
                bin=_as_uuid(line["bin_id"]) if line.get("bin_id") else None,
                lot=_as_uuid(line["lot_id"]) if line.get("lot_id") else None,
                serial=_as_uuid(line["serial_id"]) if line.get("serial_id") else None,
            )
        await self._audit(
            "vendor_return.created",
            "procurement.vendor_return_authorization",
            return_id,
            {"lines": len(lines)},
            actor_id,
        )
        await self._idem_complete(
            "POST /api/v1/vendor-returns", idempotency_key, {"id": str(return_id)}, return_id, 201
        )
        return await self.get_return(return_id)

    async def get_return(self, return_id: Any) -> dict[str, Any]:
        row = await self._require_row("vendor_return_authorization", return_id, "vendor return")
        row["lines"] = await self._all(
            "SELECT * FROM procurement.vendor_return_authorization_line WHERE tenant_id = :tenant_id AND vendor_return_authorization_id = :id ORDER BY line_number",
            id=_as_uuid(return_id),
        )
        return row

    async def list_returns(
        self, *, vendor_id: str | None = None, status: str | None = None, limit: int = 50
    ) -> dict[str, Any]:
        clauses = ["tenant_id = :tenant_id"]
        params: dict[str, Any] = {"limit": max(1, min(int(limit), 200))}
        if vendor_id:
            clauses.append("vendor_id = :vendor")
            params["vendor"] = _as_uuid(vendor_id)
        if status:
            clauses.append("status = :status")
            params["status"] = status.upper()
        return {
            "items": await self._all(
                f"SELECT * FROM procurement.vendor_return_authorization WHERE {' AND '.join(clauses)} ORDER BY created_at DESC LIMIT :limit",
                **params,
            ),
            "next_cursor": None,
        }

    async def approve_return(self, return_id: Any, *, actor_id: str | None = None) -> dict[str, Any]:
        ra = await self._require_row("vendor_return_authorization", return_id, "vendor return")
        if ra["status"] != "DRAFT":
            raise InvalidStateTransition(f"return is {ra['status']}, expected DRAFT")
        await self._exec(
            "UPDATE procurement.vendor_return_authorization SET status = 'APPROVED', updated_at = now(), version = version + 1 WHERE tenant_id = :tenant_id AND id = :id",
            id=_as_uuid(return_id),
        )
        await self._audit("vendor_return.approved", "procurement.vendor_return_authorization", return_id, {}, actor_id)
        return {"id": str(return_id), "status": "APPROVED"}

    async def ship_return(
        self,
        return_id: Any,
        lines: Sequence[Mapping[str, Any]] | None = None,
        *,
        actor_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        from app.modules.inventory_ledger.facade import PostingPort

        ra = await self.get_return(return_id)
        if ra["status"] != "APPROVED":
            raise InvalidStateTransition(f"return is {ra['status']}, expected APPROVED")
        requested = {str(line["line_id"]): qty(line.get("qty_shipped", line.get("qty"))) for line in (lines or [])}
        movements = []
        total_value = ZERO
        shipped_lines = []
        for line in ra["lines"]:
            remaining = qty(dec(line["qty_authorized"]) - dec(line["qty_shipped"]))
            qty_ship = requested.get(str(line["id"]), remaining)
            if qty_ship <= 0 or qty_ship > remaining:
                raise QuantityExceedsOrder(details={"line_id": str(line["id"]), "remaining": str(remaining)})
            location = line.get("location_id") or await self._location_id(None)
            status_id = await self._default_status_id()
            if not location or not status_id:
                raise ValidationFailed("return line needs a location/status to post T-03")
            unit_cost = dec(line.get("unit_cost") or 0)
            value = money(unit_cost * qty_ship)
            total_value += value
            movements.append(
                {
                    "item_id": str(line["item_id"]),
                    "location_id": str(location),
                    "inventory_status_id": str(status_id),
                    "qty": str(-qty_ship),
                    "value_base": str(-value),
                    "unit_cost": str(unit_cost),
                    "movement_type": "VENDOR_RETURN",
                    "source_line_ref": str(line["line_number"]),
                    "details": [
                        {
                            "qty": str(-qty_ship),
                            "inventory_status_id": str(status_id),
                            "ownership": "OWNED",
                            "lot_id": line.get("lot_id"),
                            "serial_id": line.get("serial_id"),
                            "bin_id": line.get("bin_id"),
                        }
                    ],
                }
            )
            shipped_lines.append((line, qty_ship))
        port = PostingPort(self.session, self.tenant_id)
        result = await port.post_vendor_return(
            transaction_date=ra.get("return_date") or ra["transaction_date"],
            source_doc_type="VENDOR_RETURN",
            source_doc_id=str(return_id),
            idempotency_key=idempotency_key or f"return-ship:{return_id}",
            movements=movements,
        )
        for line, qty_ship in shipped_lines:
            await self._exec(
                "UPDATE procurement.vendor_return_authorization_line SET qty_shipped = qty_shipped + :qty, updated_at = now(), version = version + 1 "
                "WHERE tenant_id = :tenant_id AND id = :id",
                qty=qty_ship,
                id=_as_uuid(line["id"]),
            )
            if line.get("purchase_order_line_id"):
                await self._exec(
                    "UPDATE procurement.purchase_order_line SET qty_returned = qty_returned + :qty, updated_at = now(), version = version + 1 "
                    "WHERE tenant_id = :tenant_id AND id = :id",
                    qty=qty_ship,
                    id=_as_uuid(line["purchase_order_line_id"]),
                )
        await self._exec(
            "UPDATE procurement.vendor_return_authorization SET status = 'SHIPPED', posted_transaction_id = :txn, updated_at = now(), version = version + 1 "
            "WHERE tenant_id = :tenant_id AND id = :id",
            txn=_as_uuid(result["transaction_id"]),
            id=_as_uuid(return_id),
        )
        if ra.get("purchase_order_id"):
            await self._refresh_po_status(ra["purchase_order_id"])
        await self._emit(
            "vendor_return.shipped",
            "vendor_return",
            return_id,
            {
                "tenant_id": str(self.tenant_id),
                "vendor_return_id": str(return_id),
                "inventory_transaction_id": result["transaction_id"],
                "lines": [
                    {"line_id": str(line["id"]), "qty_shipped": str(qty_ship)} for line, qty_ship in shipped_lines
                ],
            },
        )
        await self._audit(
            "vendor_return.shipped",
            "procurement.vendor_return_authorization",
            return_id,
            {"transaction_id": result["transaction_id"]},
            actor_id,
        )
        return {"id": str(return_id), "status": "SHIPPED", "posted_transaction_id": result["transaction_id"]}

    async def credit_return(
        self, return_id: Any, data: Mapping[str, Any], *, actor_id: str | None = None
    ) -> dict[str, Any]:
        ra = await self._require_row("vendor_return_authorization", return_id, "vendor return")
        if ra["status"] not in {"SHIPPED", "CREDITED"}:
            raise InvalidStateTransition(f"return is {ra['status']}; only shipped returns can be credited")
        credit_id = data.get("vendor_credit_id")
        credit = await self._require_row("vendor_credit", credit_id, "vendor credit")
        if str(credit["vendor_id"]) != str(ra["vendor_id"]):
            raise ValidationFailed("credit vendor does not match return vendor")
        await self._exec(
            "UPDATE procurement.vendor_return_authorization SET status = 'CREDITED', vendor_credit_id = :credit, updated_at = now(), version = version + 1 "
            "WHERE tenant_id = :tenant_id AND id = :id",
            credit=_as_uuid(credit_id),
            id=_as_uuid(return_id),
        )
        await self._exec(
            "UPDATE procurement.vendor_return_authorization_line SET qty_credited = qty_shipped, updated_at = now(), version = version + 1 "
            "WHERE tenant_id = :tenant_id AND vendor_return_authorization_id = :id",
            id=_as_uuid(return_id),
        )
        await self._emit(
            "vendor_return.credited",
            "vendor_return",
            return_id,
            {
                "tenant_id": str(self.tenant_id),
                "vendor_return_id": str(return_id),
                "vendor_credit_id": str(credit_id),
            },
        )
        await self._audit(
            "vendor_return.credited",
            "procurement.vendor_return_authorization",
            return_id,
            {"vendor_credit_id": str(credit_id)},
            actor_id,
        )
        return {"id": str(return_id), "status": "CREDITED", "vendor_credit_id": str(credit_id)}

    async def close_return(
        self, return_id: Any, *, line_ids: Sequence[Any] | None = None, actor_id: str | None = None
    ) -> dict[str, Any]:
        ra = await self._require_row("vendor_return_authorization", return_id, "vendor return")
        if ra["status"] in {"CLOSED", "CANCELLED"}:
            raise InvalidStateTransition(f"return is already {ra['status']}")
        await self._exec(
            "UPDATE procurement.vendor_return_authorization_line SET is_closed = true, updated_at = now(), version = version + 1 WHERE tenant_id = :tenant_id AND vendor_return_authorization_id = :id",
            id=_as_uuid(return_id),
        )
        await self._exec(
            "UPDATE procurement.vendor_return_authorization SET status = 'CLOSED', updated_at = now(), version = version + 1 WHERE tenant_id = :tenant_id AND id = :id",
            id=_as_uuid(return_id),
        )
        await self._audit("vendor_return.closed", "procurement.vendor_return_authorization", return_id, {}, actor_id)
        return {"id": str(return_id), "status": "CLOSED"}

    # ------------------------------------------------------------------------- drop-ship links ----
    async def create_special_order_link(
        self, data: Mapping[str, Any], *, actor_id: str | None = None, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        await self._require_enabled()
        replay = await self._idem_begin("POST /api/v1/special-order-links", idempotency_key, data, actor_id)
        if replay:
            return replay["replay"]
        if not await self._flag("procurement.drop_ship_enabled", False) and not await self._setting(
            "drop_ship_enabled", False
        ):
            raise DomainDisabled("drop-ship/special-order links are disabled")
        po_line = await self._require_row(
            "purchase_order_line", data.get("purchase_order_line_id"), "purchase order line"
        )
        if not (po_line.get("is_drop_ship") or po_line.get("is_special_order")):
            raise ValidationFailed("PO line is not marked drop-ship/special-order")
        if not data.get("external_sales_ref") or not data.get("external_sales_line_ref"):
            raise ValidationFailed("external_sales_ref and external_sales_line_ref are required")
        link_id = uuid.uuid4()
        try:
            await self._exec(
                """
                INSERT INTO procurement.special_order_link
                    (id, tenant_id, link_type, external_sales_ref, external_sales_line_ref, item_id, purchase_order_id,
                     purchase_order_line_id, qty, status, created_by, updated_by)
                VALUES (:id, :tenant_id, :type, :sales_ref, :sales_line, :item, :po, :po_line, :qty, 'OPEN', :actor, :actor)
                """,
                id=link_id,
                type=str(
                    data.get("link_type") or ("DROP_SHIP" if po_line.get("is_drop_ship") else "SPECIAL_ORDER")
                ).upper(),
                sales_ref=str(data["external_sales_ref"]),
                sales_line=str(data["external_sales_line_ref"]),
                item=_as_uuid(po_line["item_id"]),
                po=_as_uuid(po_line["purchase_order_id"]),
                po_line=_as_uuid(po_line["id"]),
                qty=qty(data.get("qty") or po_line["qty_ordered"]),
                actor=_as_uuid(actor_id) if actor_id else None,
            )
        except IntegrityError as exc:
            raise DuplicateDocument(
                "a special-order/drop-ship link already exists for this external line (DM-PROC-08)"
            ) from exc
        await self._audit(
            "special_order_link.created",
            "procurement.special_order_link",
            link_id,
            {"external_sales_ref": str(data["external_sales_ref"])},
            actor_id,
        )
        await self._idem_complete(
            "POST /api/v1/special-order-links", idempotency_key, {"id": str(link_id)}, link_id, 201
        )
        return await self.get_special_order_link(link_id)

    async def get_special_order_link(self, link_id: Any) -> dict[str, Any]:
        return await self._require_row("special_order_link", link_id, "special-order link")

    async def list_special_order_links(
        self,
        *,
        purchase_order_id: str | None = None,
        item_id: str | None = None,
        status: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        clauses = ["tenant_id = :tenant_id"]
        params: dict[str, Any] = {"limit": max(1, min(int(limit), 500))}
        if purchase_order_id:
            clauses.append("purchase_order_id = :po")
            params["po"] = _as_uuid(purchase_order_id)
        if item_id:
            clauses.append("item_id = :item")
            params["item"] = _as_uuid(item_id)
        if status:
            clauses.append("status = :status")
            params["status"] = status.upper()
        return {
            "items": await self._all(
                f"SELECT * FROM procurement.special_order_link WHERE {' AND '.join(clauses)} ORDER BY created_at DESC LIMIT :limit",
                **params,
            ),
            "next_cursor": None,
        }

    async def fulfil_dropship(
        self, link_id: Any, data: Mapping[str, Any], *, actor_id: str | None = None, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        from app.modules.inventory_ledger.facade import PostingPort

        link = await self._require_row("special_order_link", link_id, "special-order link")
        if link["status"] != "OPEN":
            raise InvalidStateTransition(f"link is {link['status']}, expected OPEN")
        po_line = await self._require_row("purchase_order_line", link["purchase_order_line_id"], "purchase order line")
        fulfil_qty = qty(data.get("qty") or link["qty"])
        if fulfil_qty <= 0 or fulfil_qty > dec(link["qty"]):
            raise QuantityExceedsOrder("fulfilment quantity exceeds the linked quantity")
        location = await self._location_id(po_line.get("ship_to_location_id"))
        status_id = await self._default_status_id()
        if not location or not status_id:
            raise ValidationFailed("drop-ship fulfilment needs a location/status for the value-only ledger line")
        value = money(dec(po_line["unit_price"]) * fulfil_qty)
        port = PostingPort(self.session, self.tenant_id)
        transaction_id_data = data.get("transaction_date") or date.today()
        result = await port.post_drop_ship(
            transaction_date=transaction_id_data,
            source_doc_type="SPECIAL_ORDER_LINK",
            source_doc_id=str(link_id),
            idempotency_key=idempotency_key or f"dropship:{link_id}",
            movements=[
                {
                    "item_id": str(link["item_id"]),
                    "location_id": str(location),
                    "inventory_status_id": str(status_id),
                    "qty": "0",
                    "value_base": str(value),
                    "movement_type": "DROP_SHIP",
                    "source_line_ref": "1",
                    "details": [{"qty": "0", "inventory_status_id": str(status_id), "ownership": "OWNED"}],
                }
            ],
        )
        await self._exec(
            "UPDATE procurement.special_order_link SET status = 'FULFILLED', fulfilled_at = now(), posted_transaction_id = :txn, "
            "updated_at = now(), version = version + 1 WHERE tenant_id = :tenant_id AND id = :id",
            txn=_as_uuid(result["transaction_id"]),
            id=_as_uuid(link_id),
        )
        await self._exec(
            "UPDATE procurement.purchase_order_line SET qty_received = qty_received + :qty, qty_billed = qty_billed + :qty, updated_at = now(), version = version + 1 "
            "WHERE tenant_id = :tenant_id AND id = :id",
            qty=fulfil_qty,
            id=_as_uuid(link["purchase_order_line_id"]),
        )
        po_status = await self._refresh_po_status(link["purchase_order_id"])
        await self._emit(
            "dropship.fulfilled",
            "special_order_link",
            link_id,
            {
                "tenant_id": str(self.tenant_id),
                "special_order_link_id": str(link_id),
                "purchase_order_line_id": str(link["purchase_order_line_id"]),
                "external_sales_ref": link["external_sales_ref"],
                "inventory_transaction_id": result["transaction_id"],
            },
        )
        if po_status == "BILLED":
            await self._emit(
                "po.billed",
                "purchase_order",
                link["purchase_order_id"],
                {
                    "tenant_id": str(self.tenant_id),
                    "purchase_order_id": str(link["purchase_order_id"]),
                    "vendor_bill_id": None,
                    "total_billed": str(value),
                },
            )
        await self._audit(
            "special_order_link.fulfilled",
            "procurement.special_order_link",
            link_id,
            {"transaction_id": result["transaction_id"]},
            actor_id,
        )
        return {"id": str(link_id), "status": "FULFILLED", "posted_transaction_id": result["transaction_id"]}

    # ----------------------------------------------------------------- landed-cost bridge ------
    async def _receipt_landed_refs(self, receipt: Mapping[str, Any]) -> list[dict[str, Any]]:
        refs = []
        for line in receipt.get("lines") or []:
            qty_received = dec(line["qty_received"])
            unit_cost = dec(line.get("unit_cost") or 0)
            refs.append(
                {
                    "item_receipt_id": str(receipt["id"]),
                    "item_receipt_line_id": str(line["id"]),
                    "item_id": str(line["item_id"]),
                    "qty": _num(qty_received),
                    "amount": _num(money(qty_received * unit_cost)),
                    "weight": "0",
                    "location_id": str(line.get("location_id")) if line.get("location_id") else None,
                    "inventory_status_id": str(line.get("inventory_status_id")) if line.get("inventory_status_id") else None,
                }
            )
        return refs

    async def _create_estimated_landed_cost_document(
        self, receipt_id: Any, charges: Sequence[Mapping[str, Any]], *, actor_id: Any = None
    ) -> dict[str, Any]:
        receipt = await self.get_receipt(receipt_id)
        return await self._create_landed_cost_document(
            vendor_id=receipt["vendor_id"],
            transaction_date=receipt.get("received_at") or receipt.get("created_at"),
            currency_code=receipt["currency_code"],
            receipt_refs=await self._receipt_landed_refs(receipt),
            charges=charges,
            estimated=True,
            actor_id=actor_id,
        )

    async def _create_landed_cost_document(
        self,
        *,
        vendor_id: Any,
        transaction_date: Any,
        currency_code: str,
        receipt_refs: Sequence[Mapping[str, Any]],
        charges: Sequence[Mapping[str, Any]],
        vendor_bill_id: Any = None,
        estimated: bool = False,
        actor_id: Any = None,
    ) -> dict[str, Any]:
        """Create an FS-05 landed-cost document from receipt charges / vendor-bill landed lines."""
        from app.modules.costing.service import CostingService

        lines = []
        for index, charge in enumerate(charges, start=1):
            amount = money(charge.get("amount") or 0)
            if amount < 0:
                raise ValidationFailed(f"landed-cost charge {index} amount must be >= 0")
            line: dict[str, Any] = {
                "description": str(charge.get("description") or f"Landed cost charge {index}"),
                "amount": _num(amount),
                "currency_code": str(charge.get("currency_code") or currency_code).upper(),
            }
            if charge.get("cost_category_id"):
                line["cost_category_id"] = str(charge["cost_category_id"])
            if charge.get("allocation_basis_override"):
                line["allocation_basis_override"] = str(charge["allocation_basis_override"]).upper()
            lines.append(line)
        if not lines:
            raise ValidationFailed("at least one landed-cost charge is required")
        costing = CostingService(self.session, self.tenant_id)
        document = await costing.create_landed_cost(
            {
                "vendor_id": str(vendor_id),
                "vendor_bill_id": str(vendor_bill_id) if vendor_bill_id else None,
                "transaction_date": str(_date(transaction_date)),
                "allocation_basis": "AMOUNT",
                "currency_code": currency_code,
                "receipt_refs": [dict(ref) for ref in receipt_refs],
                "lines": lines,
            }
        )
        if estimated:
            document = await costing.repo.update_landed_cost(document["id"], {"estimated": True})
        await self._link_document(
            link_type="LANDED_COST_ESTIMATE" if estimated else "LANDED_COST_ACTUAL",
            from_type="ITEM_RECEIPT" if estimated else "VENDOR_BILL",
            from_id=(receipt_refs[0].get("item_receipt_id") if estimated else vendor_bill_id),
            to_id=document["id"],
            amount=dec(document.get("total_amount") or 0),
            actor_id=actor_id,
        )
        await self._audit(
            "landed_cost.created" if not estimated else "landed_cost.estimated_created",
            "costing.landed_cost",
            document["id"],
            {
                "vendor_id": str(vendor_id),
                "vendor_bill_id": str(vendor_bill_id) if vendor_bill_id else None,
                "receipt_refs": len(receipt_refs),
                "lines": len(lines),
                "estimated": estimated,
            },
            actor_id,
        )
        return {
            "id": document["id"],
            "status": document["status"],
            "total_amount": _num(document.get("total_amount") or 0),
            "estimated": bool(document.get("estimated")),
            "receipt_refs": len(receipt_refs),
            "lines": len(lines),
        }

    async def _link_document(
        self, *, link_type: str, from_type: str, from_id: Any, to_id: Any, amount: Decimal, actor_id: Any = None
    ) -> None:
        if not from_id:
            return
        await self._exec(
            """
            INSERT INTO platform.document_link
                (id, tenant_id, link_type, from_type, from_id, to_type, to_id, amount_base, created_by, updated_by)
            VALUES (:id, :tenant_id, :link_type, :from_type, :from_id, 'COSTING_LANDED_COST', :to_id, :amount, :actor, :actor)
            ON CONFLICT (tenant_id, link_type, from_type, from_id, to_type, to_id) DO NOTHING
            """,
            id=uuid.uuid4(),
            link_type=link_type,
            from_type=from_type,
            from_id=_as_uuid(from_id),
            to_id=_as_uuid(to_id),
            amount=amount,
            actor=_as_uuid(actor_id) if actor_id else None,
        )

    async def _post_landed_cost_value_only(
        self,
        *,
        landed_cost: Mapping[str, Any],
        posting_type: str,
        idempotency_key: str,
        transaction_date: Any,
        source_doc_type: str,
        source_doc_id: Any,
        receipt_lines: Mapping[str, Mapping[str, Any]],
    ) -> dict[str, Any]:
        from app.modules.costing.service import CostingService
        from app.modules.inventory_ledger.facade import PostingPort

        costing = CostingService(self.session, self.tenant_id)
        landed_cost_id = str(landed_cost["id"])
        document = await costing.get_landed_cost(landed_cost_id)
        if document["status"] == "DRAFT":
            await costing.transition_landed_cost(landed_cost_id, "submit")
            await costing.transition_landed_cost(landed_cost_id, "approve")
            await costing.transition_landed_cost(landed_cost_id, "post")
            document = await costing.get_landed_cost(landed_cost_id)
        elif document["status"] in {"PENDING_APPROVAL", "APPROVED"}:
            if document["status"] == "PENDING_APPROVAL":
                await costing.transition_landed_cost(landed_cost_id, "approve")
            if document["status"] != "POSTED":
                document = await costing.transition_landed_cost(landed_cost_id, "post")
        allocations = (await costing.landed_cost_allocations(landed_cost_id))["items"]
        movements: list[dict[str, Any]] = []
        allocated_total = ZERO
        for allocation in allocations:
            allocated = dec(allocation["allocated_amount"])
            if allocated == 0:
                continue
            line = receipt_lines.get(str(allocation["item_receipt_line_id"]))
            if line is None:
                raise ValidationFailed("landed-cost allocation references an unknown receipt line")
            location_id = line.get("location_id")
            status_id = line.get("inventory_status_id")
            if not location_id or not status_id:
                # fall back to the allocated item/receipt line metadata captured in receipt_refs
                ref = next((ref for ref in document.get("receipt_refs") or [] if str(ref.get("item_receipt_line_id")) == str(allocation["item_receipt_line_id"])), None)
                location_id = location_id or (ref or {}).get("location_id")
                status_id = status_id or (ref or {}).get("inventory_status_id")
            if not location_id or not status_id:
                raise ValidationFailed("landed-cost posting requires location/status on the receipt lines")
            allocated_total += allocated
            movements.append(
                {
                    "item_id": str(allocation["item_id"]),
                    "location_id": str(location_id),
                    "inventory_status_id": str(status_id),
                    "qty": "0",
                    "value_base": _num(allocated),
                    "movement_type": "LANDED_COST",
                    "source_line_ref": str(allocation["item_receipt_line_id"]),
                    "details": [{"qty": "0", "inventory_status_id": str(status_id), "ownership": "OWNED"}],
                }
            )
        transaction_id = None
        if movements:
            port = PostingPort(self.session, self.tenant_id)
            if posting_type == "T-27":
                result = await port.post_estimated_landed_cost(
                    transaction_date=transaction_date,
                    source_doc_type=source_doc_type,
                    source_doc_id=str(source_doc_id),
                    idempotency_key=idempotency_key,
                    movements=movements,
                )
            else:
                result = await port.post_landed_cost(
                    transaction_date=transaction_date,
                    source_doc_type=source_doc_type,
                    source_doc_id=str(source_doc_id),
                    idempotency_key=idempotency_key,
                    movements=movements,
                )
            transaction_id = result["transaction_id"]
            await costing.repo.update_landed_cost(landed_cost_id, {"posted_transaction_id": _as_uuid(transaction_id)})
        return {
            "landed_cost_id": landed_cost_id,
            "posting_type": posting_type,
            "allocated_total": _num(allocated_total),
            "allocations": [
                {
                    "item_receipt_line_id": str(allocation["item_receipt_line_id"]),
                    "item_id": str(allocation["item_id"]),
                    "allocated_amount": _num(allocation["allocated_amount"]),
                }
                for allocation in allocations
            ],
            "posted_transaction_id": transaction_id,
            "value_only": True,
        }

    async def _post_estimated_landed_cost_for_receipt(
        self, receipt: Mapping[str, Any], *, actor_id: Any = None
    ) -> dict[str, Any] | None:
        try:
            landed_cost = await self._one(
                """
                SELECT * FROM costing.landed_cost
                 WHERE tenant_id = :tenant_id AND vendor_bill_id IS NULL AND estimated
                   AND receipt_refs @> CAST(:needle AS jsonb)
                 ORDER BY created_at DESC LIMIT 1
                """,
                needle=json.dumps([{"item_receipt_id": str(receipt["id"])}]),
            )
        except Exception:  # noqa: BLE001 - costing unavailable/rolled-back in isolated unit tests
            return None
        if landed_cost is None:
            return None
        if landed_cost.get("status") == "POSTED" and landed_cost.get("posted_transaction_id"):
            return {
                "landed_cost_id": landed_cost["id"],
                "posting_type": "T-27",
                "status": "POSTED",
                "posted_transaction_id": landed_cost.get("posted_transaction_id"),
                "replayed": True,
            }
        receipt_lines = {
            str(line["id"]): line
            for line in await self._all(
                "SELECT id, item_id, location_id, inventory_status_id FROM procurement.item_receipt_line "
                "WHERE tenant_id = :tenant_id AND item_receipt_id = :id",
                id=_as_uuid(receipt["id"]),
            )
        }
        result = await self._post_landed_cost_value_only(
            landed_cost=landed_cost,
            posting_type="T-27",
            idempotency_key=f"landed-cost:estimated:{receipt['id']}:{landed_cost['id']}",
            transaction_date=_date(receipt.get("received_at")) or _date(landed_cost["transaction_date"]) or date.today(),
            source_doc_type="ITEM_RECEIPT",
            source_doc_id=receipt["id"],
            receipt_lines=receipt_lines,
        )
        await self._audit(
            "landed_cost.estimated_posted", "costing.landed_cost", landed_cost["id"], result, actor_id
        )
        return result

    async def _post_actual_landed_cost_for_bill(self, bill: Mapping[str, Any], *, actor_id: Any = None) -> dict[str, Any] | None:
        landed_lines = [line for line in bill.get("lines") or [] if line.get("is_landed_cost")]
        if not landed_lines:
            return None
        existing = await self._one(
            "SELECT * FROM costing.landed_cost WHERE tenant_id = :tenant_id AND vendor_bill_id = :bill ORDER BY created_at DESC LIMIT 1",
            bill=_as_uuid(bill["id"]),
        )
        if existing is not None and existing.get("status") == "POSTED":
            receipt_lines = {
                str(line["id"]): line
                for line in await self._all(
                    "SELECT id, item_id, location_id, inventory_status_id FROM procurement.item_receipt_line "
                    "WHERE tenant_id = :tenant_id AND item_receipt_id = :id",
                    id=_as_uuid(bill.get("item_receipt_id")),
                )
            } if bill.get("item_receipt_id") else {}
            return await self._post_landed_cost_value_only(
                landed_cost=existing,
                posting_type="T-26",
                idempotency_key=f"landed-cost:actual:{bill['id']}:{existing['id']}",
                transaction_date=bill["transaction_date"],
                source_doc_type="VENDOR_BILL",
                source_doc_id=bill["id"],
                receipt_lines=receipt_lines,
            )
        charges = []
        receipt_refs = []
        for line in landed_lines:
            receipt_line = await self._require_row("item_receipt_line", line["item_receipt_line_id"], "receipt line")
            receipt = await self._require_row("item_receipt", receipt_line["item_receipt_id"], "item receipt")
            if receipt["status"] != "POSTED":
                raise ValidationFailed("landed-cost bill line references an unposted receipt")
            charges.append({
                "description": line.get("description") or f"Landed cost bill line {line['line_number']}",
                "amount": line["amount"],
                "cost_category_id": line.get("landed_cost_category_id"),
            })
            receipt_refs.append({
                "item_receipt_id": str(receipt["id"]),
                "item_receipt_line_id": str(receipt_line["id"]),
                "item_id": str(receipt_line["item_id"]),
                "qty": _num(line["qty"] or receipt_line["qty_received"]),
                "amount": _num(line["amount"]),
                "weight": "0",
                "location_id": str(receipt_line.get("location_id")) if receipt_line.get("location_id") else None,
                "inventory_status_id": str(receipt_line.get("inventory_status_id")) if receipt_line.get("inventory_status_id") else None,
            })
        document = await self._create_landed_cost_document(
            vendor_id=bill["vendor_id"],
            transaction_date=bill["transaction_date"],
            currency_code=bill["currency_code"],
            receipt_refs=receipt_refs,
            charges=charges,
            vendor_bill_id=bill["id"],
            estimated=False,
            actor_id=actor_id,
        )
        receipt_lines = {
            str(line["id"]): line
            for line in await self._all(
                "SELECT id, item_id, location_id, inventory_status_id FROM procurement.item_receipt_line "
                "WHERE tenant_id = :tenant_id AND item_receipt_id = :id",
                id=_as_uuid(receipt_refs[0]["item_receipt_id"]),
            )
        }
        result = await self._post_landed_cost_value_only(
            landed_cost=document,
            posting_type="T-26",
            idempotency_key=f"landed-cost:actual:{bill['id']}:{document['id']}",
            transaction_date=bill["transaction_date"],
            source_doc_type="VENDOR_BILL",
            source_doc_id=bill["id"],
            receipt_lines=receipt_lines,
        )
        variance = await self._post_landed_cost_absorption_variance(
            bill=bill, actual_document=document, actual_result=result, receipt_refs=receipt_refs, actor_id=actor_id
        )
        if variance:
            result["variance"] = variance
        await self._audit("landed_cost.actual_posted", "costing.landed_cost", document["id"], result, actor_id)
        return result

    async def _post_landed_cost_absorption_variance(
        self,
        *,
        bill: Mapping[str, Any],
        actual_document: Mapping[str, Any],
        actual_result: Mapping[str, Any],
        receipt_refs: Sequence[Mapping[str, Any]],
        actor_id: Any = None,
    ) -> dict[str, Any] | None:
        """Record/post T-06 landed-cost absorption variance (actual T-26 − estimated T-27), value-only."""
        from app.modules.costing.repository import CostingRepository
        from app.modules.inventory_ledger.facade import PostingPort

        receipt_ids = sorted({str(ref["item_receipt_id"]) for ref in receipt_refs})
        estimated_documents: dict[str, dict[str, Any]] = {}
        for receipt_id in receipt_ids:
            row = await self._one(
                """
                SELECT * FROM costing.landed_cost
                 WHERE tenant_id = :tenant_id AND estimated
                   AND receipt_refs @> CAST(:needle AS jsonb)
                 ORDER BY created_at DESC LIMIT 1
                """,
                needle=json.dumps([{"item_receipt_id": receipt_id}]),
            )
            if row is not None:
                estimated_documents[str(row["id"])] = row
        if not estimated_documents:
            return None
        existing = await self._one(
            "SELECT * FROM costing.cost_variance WHERE tenant_id = :tenant_id "
            "AND variance_type = 'LANDED_COST_ABSORPTION' AND detail ->> 'vendor_bill_id' = :bill ORDER BY created_at DESC",
            bill=str(bill["id"]),
        )
        if existing is not None:
            return {
                "variance_ids": [str(existing["id"])],
                "posting_type": "T-06",
                "total_variance": _num(dec(existing["amount_base"])),
                "posted_transaction_id": existing.get("inventory_transaction_id"),
                "replayed": True,
            }
        estimated: dict[str, dict[str, Any]] = {}
        for landed_cost_id in estimated_documents:
            allocations = await self._all(
                "SELECT item_receipt_line_id, item_id, allocated_amount FROM costing.landed_cost_allocation "
                "WHERE tenant_id = :tenant_id AND landed_cost_id = :id",
                id=_as_uuid(landed_cost_id),
            )
            for allocation in allocations:
                key = str(allocation["item_receipt_line_id"])
                bucket = estimated.setdefault(key, {"item_id": allocation["item_id"], "amount": ZERO})
                bucket["amount"] += dec(allocation["allocated_amount"])
        actual: dict[str, dict[str, Any]] = {}
        for allocation in actual_result.get("allocations") or []:
            key = str(allocation["item_receipt_line_id"])
            bucket = actual.setdefault(key, {"item_id": allocation["item_id"], "amount": ZERO})
            bucket["amount"] += dec(allocation["allocated_amount"])
        estimated_total = sum((bucket["amount"] for bucket in estimated.values()), ZERO)
        actual_total = sum((bucket["amount"] for bucket in actual.values()), ZERO)
        variance_items: list[dict[str, Any]] = []
        for key in sorted(set(estimated) | set(actual)):
            estimated_amount = estimated.get(key, {}).get("amount", ZERO)
            actual_amount = actual.get(key, {}).get("amount", ZERO)
            difference = money(actual_amount - estimated_amount)
            if difference == 0:
                continue
            receipt_line = await self._one(
                "SELECT id, item_id, location_id, inventory_status_id FROM procurement.item_receipt_line "
                "WHERE tenant_id = :tenant_id AND id = :id",
                id=_as_uuid(key),
            )
            if receipt_line is None:
                raise ValidationFailed("landed-cost variance references an unknown receipt line")
            item_id = (actual.get(key) or estimated.get(key) or {}).get("item_id") or receipt_line["item_id"]
            landed_bill_line = next(
                (
                    line
                    for line in bill.get("lines") or []
                    if line.get("is_landed_cost") and str(line.get("item_receipt_line_id")) == key
                ),
                None,
            )
            variance_items.append(
                {
                    "item_receipt_line_id": key,
                    "item_id": str(item_id),
                    "location_id": str(receipt_line["location_id"]),
                    "inventory_status_id": str(receipt_line["inventory_status_id"]),
                    "estimated_amount": estimated_amount,
                    "actual_amount": actual_amount,
                    "variance_amount": difference,
                    "vendor_bill_line_id": str(landed_bill_line["id"]) if landed_bill_line else None,
                    "line_number": landed_bill_line.get("line_number") if landed_bill_line else None,
                }
            )
        if not variance_items:
            return None
        port = PostingPort(self.session, self.tenant_id)
        movements = [
            {
                "item_id": item["item_id"],
                "location_id": item["location_id"],
                "inventory_status_id": item["inventory_status_id"],
                "qty": "0",
                "value_base": _num(item["variance_amount"]),
                "movement_type": "VARIANCE",
                "source_line_ref": item["item_receipt_line_id"],
                "details": [{"qty": "0", "inventory_status_id": item["inventory_status_id"], "ownership": "OWNED"}],
            }
            for item in variance_items
        ]
        variance_result = await port.post_variance(
            transaction_date=bill["transaction_date"],
            source_doc_type="VENDOR_BILL_LANDED_COST_VARIANCE",
            source_doc_id=str(bill["id"]),
            idempotency_key=f"landed-cost-variance:{bill['id']}",
            movements=movements,
        )
        transaction_id = variance_result["transaction_id"]
        repository = CostingRepository(self.session, self.tenant_id)
        variance_ids: list[str] = []
        for item in variance_items:
            row = await repository.create_variance(
                {
                    "variance_type": "LANDED_COST_ABSORPTION",
                    "item_id": _as_uuid(item["item_id"]),
                    "inventory_transaction_id": _as_uuid(transaction_id),
                    "vendor_bill_line_id": _as_uuid(item["vendor_bill_line_id"]) if item["vendor_bill_line_id"] else None,
                    "amount_base": item["variance_amount"],
                    "account_ref": str(bill.get("variance_account_ref") or "LANDED_COST_ABSORPTION"),
                    "status": "OPEN",
                    "detail": {
                        "vendor_bill_id": str(bill["id"]),
                        "item_receipt_line_id": item["item_receipt_line_id"],
                        "estimated_landed_cost_ids": sorted(estimated_documents),
                        "actual_landed_cost_id": str(actual_document["id"]),
                        "estimated_amount": _num(item["estimated_amount"]),
                        "actual_amount": _num(item["actual_amount"]),
                        "applied_landed_cost": _num(item["actual_amount"]),
                        "absorbed_landed_cost": _num(item["estimated_amount"]),
                        "posting_type": "T-06",
                        "value_only": True,
                    },
                }
            )
            variance_ids.append(str(row["id"]))
        total_variance = sum((item["variance_amount"] for item in variance_items), ZERO)
        await self._audit(
            "landed_cost.absorption_variance_posted",
            "costing.cost_variance",
            bill["id"],
            {
                "variance_ids": variance_ids,
                "posting_type": "T-06",
                "posted_transaction_id": transaction_id,
                "estimated_total": _num(estimated_total),
                "actual_total": _num(actual_total),
                "total_variance": _num(total_variance),
            },
            actor_id,
        )
        return {
            "variance_ids": variance_ids,
            "posting_type": "T-06",
            "posted_transaction_id": transaction_id,
            "estimated_total": _num(estimated_total),
            "actual_total": _num(actual_total),
            "total_variance": _num(total_variance),
            "items": [
                {
                    "item_receipt_line_id": item["item_receipt_line_id"],
                    "item_id": item["item_id"],
                    "estimated_amount": _num(item["estimated_amount"]),
                    "actual_amount": _num(item["actual_amount"]),
                    "variance_amount": _num(item["variance_amount"]),
                }
                for item in variance_items
            ],
            "value_only": True,
        }

    async def _reverse_estimated_landed_cost_for_receipt(
        self, receipt: Mapping[str, Any], *, reason: str, actor_id: Any = None
    ) -> dict[str, Any] | None:
        """Reverse a posted T-27 estimated landed cost when the receipt is reversed (no double-reverse)."""
        from app.modules.costing.service import CostingService
        from app.modules.inventory_ledger.facade import PostingPort

        needle = json.dumps([{"item_receipt_id": str(receipt["id"])}])
        landed_cost = await self._one(
            """
            SELECT * FROM costing.landed_cost
             WHERE tenant_id = :tenant_id AND estimated
               AND receipt_refs @> CAST(:needle AS jsonb)
             ORDER BY created_at DESC LIMIT 1
            """,
            needle=needle,
        )
        if landed_cost is None:
            return None
        if landed_cost["status"] == "REVERSED":
            return {
                "landed_cost_id": str(landed_cost["id"]),
                "posting_type": "T-27",
                "status": "REVERSED",
                "replayed": True,
            }
        actual = await self._one(
            "SELECT id FROM costing.landed_cost WHERE tenant_id = :tenant_id AND NOT estimated AND status = 'POSTED' "
            "AND receipt_refs @> CAST(:needle AS jsonb) ORDER BY created_at DESC LIMIT 1",
            needle=needle,
        )
        if actual is not None:
            raise DependentDocuments(
                "estimated landed cost has been actualized by a vendor bill; reverse the bill/actual landed cost first",
                details={"estimated_landed_cost_id": str(landed_cost["id"]), "actual_landed_cost_id": str(actual["id"])},
            )
        costing = CostingService(self.session, self.tenant_id)
        if landed_cost["status"] == "DRAFT":
            await costing.transition_landed_cost(landed_cost["id"], "void")
            return {
                "landed_cost_id": str(landed_cost["id"]),
                "posting_type": "T-27",
                "status": "VOID",
                "amount": _num(dec(landed_cost.get("total_amount") or 0)),
            }
        if landed_cost["status"] != "POSTED" or not landed_cost.get("posted_transaction_id"):
            raise InvalidStateTransition(f"estimated landed cost is {landed_cost['status']} and cannot be reversed")
        port = PostingPort(self.session, self.tenant_id)
        reversal = await port.reverse(
            str(landed_cost["posted_transaction_id"]),
            reason=reason,
            idempotency_key=f"landed-cost:estimated:reverse:{receipt['id']}:{landed_cost['id']}",
        )
        await costing.transition_landed_cost(landed_cost["id"], "reverse")
        amount = dec(landed_cost.get("total_amount") or 0)
        await self._link_document(
            link_type="LANDED_COST_ESTIMATE_REVERSAL",
            from_type="ITEM_RECEIPT",
            from_id=receipt["id"],
            to_id=landed_cost["id"],
            amount=-amount,
            actor_id=actor_id,
        )
        summary = {
            "landed_cost_id": str(landed_cost["id"]),
            "posting_type": "T-27",
            "status": "REVERSED",
            "amount": _num(-amount),
            "original_transaction_id": str(landed_cost["posted_transaction_id"]),
            "reversal_transaction_id": reversal["transaction_id"],
            "value_only": True,
        }
        await self._audit(
            "landed_cost.estimated_reversed",
            "costing.landed_cost",
            landed_cost["id"],
            {"receipt_id": str(receipt["id"]), "reverse": summary, "reason": reason},
            actor_id,
        )
        return summary
