"""FS-13 consigned inventory service — agreements, ownership transfers (T-21/T-22/T-23) and consumption.

Consigned positions are FS-04 ledger positions with ``ownership=CONSIGNED``. This service writes only
``consigned`` tables + platform audit/outbox/finance-intent rows; every inventory/value effect goes through
the frozen `inventory.ledger` `PostingPort` and vendor bills through the `procurement.p2p` facade (FS-08),
inside the caller's transaction. No ledger/cost table is written here.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.idempotency_store import IdempotencyStore
from app.core.outbox import emit
from app.core.sequences import SequenceError, SequenceService
from app.modules.consigned.errors import (
    AgreementNotActive,
    AssemblyIncompatible,
    AssemblyNotConsignable,
    ConsignedQtyExceeded,
    DependentDocuments,
    DomainDisabled,
    InvalidStateTransition,
    ItemNotConsignable,
    NotFound,
    ProhibitedOperation,
    StockRemains,
    ValidationFailed,
)

ZERO = Decimal("0")
QTY_Q = Decimal("0.000000000001")
MONEY_Q = Decimal("0.000000000001")
ASSEMBLY_TYPES = frozenset({"ASSEMBLY", "WIP", "PHANTOM", "OUTSOURCED", "OUTSOURCED_MANUFACTURING"})
EVENT_TYPES = (
    "consigned.agreement.created",
    "consigned.agreement.activated",
    "consigned.agreement.suspended",
    "consigned.agreement.terminated",
    "consigned.received",
    "consigned.owned",
    "consigned.consumed",
    "consigned.adjusted",
    "consigned.returned",
    "consigned.reversed",
    "consigned.guard.rejected",
)
JOB_NAMES = (
    "consigned.consumption.reconcile",
    "consigned.agreement.expiry",
    "consigned.ownership.reconcile",
    "consigned.stock.guard",
)


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


def _jsonable(value: Any) -> Any:
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return _num(value)
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_jsonable(item) for item in value]
    return value


def _row(row: Any) -> dict[str, Any]:
    mapping = getattr(row, "_mapping", row)
    return _jsonable(dict(mapping))


class ConsignedService:
    def __init__(self, session: AsyncSession, tenant_id: str | uuid.UUID) -> None:
        self.session = session
        self.tenant_id = tenant_id if isinstance(tenant_id, uuid.UUID) else uuid.UUID(str(tenant_id))
        self.idempotency = IdempotencyStore(session)

    # -------------------------------------------------------------------------------- helpers --
    async def _exec(self, sql: str, **params: Any):
        from sqlalchemy import text

        return await self.session.execute(text(sql), {"tenant_id": self.tenant_id, **params})

    async def _one(self, sql: str, **params: Any) -> dict[str, Any] | None:
        row = (await self._exec(sql, **params)).mappings().first()
        return _row(row) if row else None

    async def _all(self, sql: str, **params: Any) -> list[dict[str, Any]]:
        return [_row(row) for row in (await self._exec(sql, **params)).mappings().all()]

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
            setting_key=f"consigned.{key}",
        )
        if row is None:
            return default
        value = row["value"]
        return value[1:-1] if isinstance(value, str) and value.startswith('"') else value

    async def _require_enabled(self) -> None:
        if not await self._flag("consigned.inventory.enabled", False):
            raise DomainDisabled("consigned.inventory is disabled for this tenant (consigned.inventory.enabled=false)")

    async def _next_number(self, doc_type: str) -> str:
        try:
            return (await SequenceService(self.session).next(self.tenant_id, doc_type, allow_undeclared=True)).formatted
        except SequenceError as exc:  # pragma: no cover
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

    async def _emit(self, event_type: str, aggregate_type: str, aggregate_id: Any, payload: Mapping[str, Any]) -> None:
        await emit(
            self.session,
            event_type=event_type,
            tenant_id=str(self.tenant_id),
            aggregate_type=aggregate_type,
            aggregate_id=str(aggregate_id),
            payload=_jsonable(dict(payload)),
        )

    async def _idem_begin(
        self, endpoint: str, key: str | None, body: Mapping[str, Any], actor_id: Any = None
    ) -> dict[str, Any] | None:
        if not key:
            return None
        from app.core.idempotency_store import build_fingerprint
        from app.core.security.idempotency import IdempotencyOutcome

        decision = await self.idempotency.guard(
            tenant_id=str(self.tenant_id),
            endpoint_id=endpoint,
            key=key,
            request_fingerprint_hash=build_fingerprint(
                method="POST", path=endpoint, tenant_id=str(self.tenant_id), endpoint_id=endpoint, body=body
            ),
            actor_id=actor_id,
        )
        if decision.outcome is IdempotencyOutcome.REPLAY:
            return {"replay": _jsonable(decision.record.response_body if decision.record is not None else {})}
        decision.raise_for_conflict()
        return None

    async def _idem_complete(
        self, endpoint: str, key: str | None, response: Mapping[str, Any], resource_id: Any, status: int = 200
    ) -> None:
        if key:
            await self.idempotency.complete(
                tenant_id=str(self.tenant_id),
                endpoint_id=endpoint,
                key=key,
                response_status=status,
                response_body=_jsonable(dict(response)),
                resource_id=str(resource_id),
            )

    async def _require_row(self, table: str, row_id: Any, label: str) -> dict[str, Any]:
        row = await self._one(
            f"SELECT * FROM consigned.{table} WHERE tenant_id = :tenant_id AND id = :id", id=_as_uuid(row_id)
        )
        if row is None:
            raise NotFound(f"{label} not found")
        return row

    async def _finance_intent(
        self, *, transfer: Mapping[str, Any], rule: str, account_ref: str, amount: Decimal, debit: bool, line_no: int
    ) -> None:
        currency = transfer.get("currency_code") or await self._scalar(
            "SELECT currency_code FROM consigned.consignment_agreement WHERE tenant_id = :tenant_id AND id = :id",
            id=_as_uuid(transfer.get("consignment_agreement_id") or transfer.get("agreement_id")),
        ) or "USD"
        await self._exec(
            """
            INSERT INTO platform.finance_journal_intent
                (id, tenant_id, intent_line_no, source_doc_type, source_doc_id, posting_rule_id, account_ref,
                 debit_amount_base, credit_amount_base, currency_code, exchange_rate, status, recorded_at)
            VALUES (:id, :tenant_id, :line, 'CONSIGNMENT_TRANSFER', :transfer, :rule, :account, :debit, :credit,
                    :currency, 1, 'RECORDED', now())
            """,
            id=uuid.uuid4(),
            line=line_no,
            transfer=_as_uuid(transfer["id"]),
            rule=rule,
            account=account_ref,
            debit=amount if debit else ZERO,
            credit=ZERO if debit else amount,
            currency=currency,
        )

    # ------------------------------------------------------------------------------ agreements --
    async def create_agreement(
        self, data: Mapping[str, Any], *, actor_id: str | None = None, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        await self._require_enabled()
        replay = await self._idem_begin("POST /api/v1/consignment-agreements", idempotency_key, data, actor_id)
        if replay:
            return replay["replay"]
        vendor = await self._one(
            "SELECT * FROM procurement.vendor WHERE tenant_id = :tenant_id AND id = :id",
            id=_as_uuid(data.get("vendor_id")),
        )
        if vendor is None:
            raise ValidationFailed("vendor not found (procurement.p2p vendor master)")
        if vendor["is_inactive"]:
            raise ValidationFailed("vendor is inactive")
        agreement_id = uuid.uuid4()
        await self._exec(
            """
            INSERT INTO consigned.consignment_agreement
                (id, tenant_id, agreement_number, vendor_id, status, start_date, end_date, payment_trigger, currency_code,
                 terms, created_by, updated_by)
            VALUES (:id, :tenant_id, :number, :vendor, 'DRAFT', :start, :end, :trigger, :currency, CAST(:terms AS jsonb), :actor, :actor)
            """,
            id=agreement_id,
            number=data.get("agreement_number") or await self._next_number("CAG"),
            vendor=_as_uuid(vendor["id"]),
            start=_date(data.get("start_date")) or date.today(),
            end=_date(data.get("end_date")),
            trigger=str(data.get("payment_trigger") or "ON_SALE").upper(),
            currency=str(data.get("currency_code") or vendor["currency_code"]).upper(),
            terms=json.dumps(_jsonable(data.get("terms") or {})),
            actor=_as_uuid(actor_id) if actor_id else None,
        )
        await self._emit(
            "consigned.agreement.created",
            "consignment_agreement",
            agreement_id,
            {
                "agreement_id": str(agreement_id),
                "vendor_id": str(vendor["id"]),
                "status": "DRAFT",
                "start_date": str(_date(data.get("start_date")) or date.today()),
                "end_date": _jsonable(_date(data.get("end_date"))),
            },
        )
        await self._audit(
            "consigned.agreement.created",
            "consigned.consignment_agreement",
            agreement_id,
            {"vendor_id": str(vendor["id"])},
            actor_id,
        )
        result = await self.get_agreement(agreement_id)
        await self._idem_complete("POST /api/v1/consignment-agreements", idempotency_key, result, agreement_id, 201)
        return result

    async def list_agreements(
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
                f"SELECT * FROM consigned.consignment_agreement WHERE {' AND '.join(clauses)} ORDER BY created_at DESC LIMIT :limit",
                **params,
            ),
            "next_cursor": None,
        }

    async def get_agreement(self, agreement_id: Any) -> dict[str, Any]:
        agreement = await self._require_row("consignment_agreement", agreement_id, "consignment agreement")
        agreement["items"] = await self._all(
            "SELECT * FROM consigned.consignment_agreement_item WHERE tenant_id = :tenant_id AND consignment_agreement_id = :id ORDER BY item_id",
            id=_as_uuid(agreement_id),
        )
        agreement["consigned_on_hand"] = await self._consigned_on_hand(agreement_id=agreement["id"])
        return agreement

    async def update_agreement(
        self,
        agreement_id: Any,
        data: Mapping[str, Any],
        *,
        actor_id: str | None = None,
        expected_version: int | None = None,
    ) -> dict[str, Any]:
        agreement = await self._require_row("consignment_agreement", agreement_id, "consignment agreement")
        if agreement["status"] != "DRAFT":
            raise InvalidStateTransition("only DRAFT agreements can be edited")
        values = {key: data[key] for key in ("end_date", "payment_trigger", "currency_code", "terms") if key in data}
        if "end_date" in values:
            values["end_date"] = _date(values["end_date"])
        if "terms" in values:
            values["terms"] = json.dumps(_jsonable(values["terms"]))
        if values:
            assignments = ", ".join(
                f"{key} = CAST(:{key} AS jsonb)" if key == "terms" else f"{key} = :{key}" for key in values
            )
            await self._exec(
                f"UPDATE consigned.consignment_agreement SET {assignments}, updated_at = now(), version = version + 1 WHERE tenant_id = :tenant_id AND id = :id",
                id=_as_uuid(agreement_id),
                **values,
            )
        return await self.get_agreement(agreement_id)

    async def _transition_agreement(
        self,
        agreement_id: Any,
        target: str,
        *,
        actor_id: str | None = None,
        reason: str | None = None,
        disposition_plan: str | None = None,
    ) -> dict[str, Any]:
        agreement = await self._require_row("consignment_agreement", agreement_id, "consignment agreement")
        allowed = {
            "ACTIVE": {"DRAFT"},
            "SUSPENDED": {"ACTIVE"},
            "TERMINATED": {"ACTIVE", "SUSPENDED"},
            "DRAFT": {"SUSPENDED"},
        }
        if agreement["status"] not in allowed.get(target, set()):
            raise InvalidStateTransition(f"agreement cannot transition {agreement['status']} -> {target}")
        if target == "ACTIVE":
            items = await self._all(
                "SELECT * FROM consigned.consignment_agreement_item WHERE tenant_id = :tenant_id AND consignment_agreement_id = :id AND is_active",
                id=_as_uuid(agreement_id),
            )
            if not items:
                raise ValidationFailed("activation requires at least one active item/location")
            guard = await self.validate_guards()
            if not guard["ok"]:
                raise AssemblyIncompatible(
                    "consigned inventory is incompatible with assembly/manufacturing configuration",
                    details={"reasons": guard["reasons"]},
                )
            await self._exec(
                "UPDATE consigned.consignment_agreement SET status = 'ACTIVE', approved_by = :actor, approved_at = now(), updated_at = now(), version = version + 1 WHERE tenant_id = :tenant_id AND id = :id",
                actor=_as_uuid(actor_id) if actor_id else None,
                id=_as_uuid(agreement_id),
            )
        elif target == "TERMINATED":
            on_hand = await self._consigned_on_hand(agreement_id=agreement_id)
            if on_hand > 0 and not disposition_plan:
                raise StockRemains(
                    "consigned stock remains; provide a disposition plan before termination",
                    details={"consigned_on_hand": _num(on_hand)},
                )
            await self._exec(
                "UPDATE consigned.consignment_agreement SET status = 'TERMINATED', updated_at = now(), version = version + 1 WHERE tenant_id = :tenant_id AND id = :id",
                id=_as_uuid(agreement_id),
            )
        else:
            await self._exec(
                "UPDATE consigned.consignment_agreement SET status = :status, updated_at = now(), version = version + 1 WHERE tenant_id = :tenant_id AND id = :id",
                status=target,
                id=_as_uuid(agreement_id),
            )
        event = {
            "ACTIVE": "consigned.agreement.activated",
            "SUSPENDED": "consigned.agreement.suspended",
            "TERMINATED": "consigned.agreement.terminated",
        }[target]
        await self._emit(
            event,
            "consignment_agreement",
            agreement_id,
            {
                "agreement_id": str(agreement_id),
                "vendor_id": str(agreement["vendor_id"]),
                "status": target,
                "effective_dates": {"start": agreement["start_date"], "end": agreement["end_date"]},
                "reason": reason,
            },
        )
        await self._audit(
            f"consigned.agreement.{target.lower()}",
            "consigned.consignment_agreement",
            agreement_id,
            {"reason": reason},
            actor_id,
        )
        return await self.get_agreement(agreement_id)

    async def activate_agreement(
        self, agreement_id: Any, *, actor_id: str | None = None, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        return await self._transition_agreement(agreement_id, "ACTIVE", actor_id=actor_id)

    async def suspend_agreement(self, agreement_id: Any, reason: str, *, actor_id: str | None = None) -> dict[str, Any]:
        return await self._transition_agreement(agreement_id, "SUSPENDED", actor_id=actor_id, reason=reason)

    async def terminate_agreement(
        self, agreement_id: Any, reason: str, *, actor_id: str | None = None, disposition_plan: str | None = None
    ) -> dict[str, Any]:
        return await self._transition_agreement(
            agreement_id, "TERMINATED", actor_id=actor_id, reason=reason, disposition_plan=disposition_plan
        )

    # ---------------------------------------------------------------------------- agreement items --
    async def _item(self, item_id: Any) -> dict[str, Any]:
        item = await self._one(
            """
            SELECT i.id, i.item_number, i.is_active, t.code AS item_type_code, inv.allow_vendor_consignment
              FROM item_master.item i
              JOIN item_master.item_type_definition t ON t.tenant_id = i.tenant_id AND t.id = i.item_type_id
              LEFT JOIN item_master.item_inventory inv ON inv.tenant_id = i.tenant_id AND inv.item_id = i.id
             WHERE i.tenant_id = :tenant_id AND i.id = :id
            """,
            id=_as_uuid(item_id),
        )
        if item is None:
            raise NotFound("item not found")
        return item

    async def _location(self, location_id: Any) -> dict[str, Any]:
        location = await self._one(
            "SELECT * FROM traceability.location WHERE tenant_id = :tenant_id AND id = :id", id=_as_uuid(location_id)
        )
        if location is None:
            raise NotFound("location not found")
        return location

    async def _status(self, status_id: Any) -> dict[str, Any]:
        status = await self._one(
            "SELECT * FROM traceability.inventory_status WHERE tenant_id = :tenant_id AND id = :id",
            id=_as_uuid(status_id),
        )
        if status is None:
            raise NotFound("inventory status not found")
        return status

    async def add_agreement_item(
        self,
        agreement_id: Any,
        data: Mapping[str, Any],
        *,
        actor_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        await self._require_row("consignment_agreement", agreement_id, "consignment agreement")
        item = await self._item(data.get("item_id"))
        if not item["is_active"]:
            raise ItemNotConsignable("item is inactive")
        if not item.get("allow_vendor_consignment"):
            raise ItemNotConsignable("item is not flagged allow_vendor_consignment (DM-ITEM-10)")
        if str(item["item_type_code"]).upper() in ASSEMBLY_TYPES:
            raise AssemblyNotConsignable("assembly/WIP/phantom/outsourced items cannot be consigned (D6)")
        location = await self._location(data.get("location_id"))
        if not location.get("is_consignment_allowed"):
            raise ItemNotConsignable("location is not flagged is_consignment_allowed")
        consigned_status = await self._status(data.get("consigned_status_id"))
        if not consigned_status.get("is_consigned"):
            raise ValidationFailed("consigned_status_id must have is_consigned=true")
        owned_status = await self._status(data.get("owned_status_id"))
        if owned_status.get("is_consigned"):
            raise ValidationFailed("owned_status_id must not be consigned")
        row_id = uuid.uuid4()
        try:
            await self._exec(
                """
                INSERT INTO consigned.consignment_agreement_item
                    (id, tenant_id, consignment_agreement_id, item_id, location_id, consigned_status_id, owned_status_id,
                     unit_cost, min_qty, max_qty, is_active, created_by, updated_by)
                VALUES (:id, :tenant_id, :agreement, :item, :location, :consigned_status, :owned_status, :cost, :min_qty,
                        :max_qty, true, :actor, :actor)
                """,
                id=row_id,
                agreement=_as_uuid(agreement_id),
                item=_as_uuid(item["id"]),
                location=_as_uuid(location["id"]),
                consigned_status=_as_uuid(consigned_status["id"]),
                owned_status=_as_uuid(owned_status["id"]),
                cost=money(data.get("unit_cost") or 0),
                min_qty=qty(data.get("min_qty") or 0),
                max_qty=qty(data["max_qty"]) if data.get("max_qty") is not None else None,
                actor=_as_uuid(actor_id) if actor_id else None,
            )
        except IntegrityError as exc:
            raise ValidationFailed("item/location is already covered by this agreement") from exc
        await self._audit(
            "consigned.agreement_item.added",
            "consigned.consignment_agreement_item",
            row_id,
            {"agreement_id": str(agreement_id), "item_id": str(item["id"])},
            actor_id,
        )
        return await self._require_row("consignment_agreement_item", row_id, "agreement item")

    async def list_agreement_items(self, agreement_id: Any) -> dict[str, Any]:
        await self._require_row("consignment_agreement", agreement_id, "consignment agreement")
        return {
            "items": await self._all(
                "SELECT * FROM consigned.consignment_agreement_item WHERE tenant_id = :tenant_id AND consignment_agreement_id = :id ORDER BY item_id",
                id=_as_uuid(agreement_id),
            )
        }

    async def update_agreement_item(
        self, agreement_id: Any, item_id: Any, data: Mapping[str, Any], *, actor_id: str | None = None
    ) -> dict[str, Any]:
        row = await self._one(
            "SELECT * FROM consigned.consignment_agreement_item WHERE tenant_id = :tenant_id AND consignment_agreement_id = :agreement AND item_id = :item",
            agreement=_as_uuid(agreement_id),
            item=_as_uuid(item_id),
        )
        if row is None:
            raise NotFound("agreement item not found")
        values = {key: data[key] for key in ("unit_cost", "min_qty", "max_qty", "is_active") if key in data}
        for key in ("unit_cost",):
            if key in values:
                values[key] = money(values[key])
        for key in ("min_qty", "max_qty"):
            if key in values and values[key] is not None:
                values[key] = qty(values[key])
        if values:
            await self._exec(
                "UPDATE consigned.consignment_agreement_item SET "
                + ", ".join(f"{key} = :{key}" for key in values)
                + ", updated_at = now(), version = version + 1 WHERE tenant_id = :tenant_id AND id = :id",
                id=_as_uuid(row["id"]),
                **values,
            )
        await self._audit(
            "consigned.agreement_item.updated", "consigned.consignment_agreement_item", row["id"], values, actor_id
        )
        return await self._require_row("consignment_agreement_item", row["id"], "agreement item")

    # ------------------------------------------------------------------------------- transfers --
    async def _agreement_item(self, agreement_id: Any, item_id: Any, location_id: Any) -> dict[str, Any]:
        row = await self._one(
            "SELECT * FROM consigned.consignment_agreement_item WHERE tenant_id = :tenant_id AND consignment_agreement_id = :agreement AND item_id = :item AND location_id = :location AND is_active",
            agreement=_as_uuid(agreement_id),
            item=_as_uuid(item_id),
            location=_as_uuid(location_id),
        )
        if row is None:
            raise ItemNotConsignable("item/location is not covered by an active agreement item")
        return row

    async def _consigned_balance(self, *, item_id: Any, location_id: Any, status_id: Any | None = None) -> Decimal:
        from app.modules.inventory_ledger.facade import BalanceReadPort

        port = BalanceReadPort(self.session, self.tenant_id)
        filters: dict[str, Any] = {"ownership": "CONSIGNED", "limit": 500}
        if item_id:
            filters["item_id"] = str(item_id)
        if location_id:
            filters["location_id"] = str(location_id)
        if status_id:
            filters["inventory_status_id"] = str(status_id)
        data = await port.balances(**filters)
        total = ZERO
        for row in data.get("items", []):
            if str(row.get("qty_class")) == "ON_HAND":
                total += dec(row.get("qty_on_hand"))
        return total

    async def _consigned_on_hand(self, *, agreement_id: Any | None = None) -> Decimal:
        from app.modules.inventory_ledger.facade import BalanceReadPort

        data = await BalanceReadPort(self.session, self.tenant_id).balances(ownership="CONSIGNED", limit=1000)
        items = data.get("items", [])
        if agreement_id is not None:
            covered = await self._all(
                "SELECT item_id, location_id FROM consigned.consignment_agreement_item WHERE tenant_id = :tenant_id AND consignment_agreement_id = :id",
                id=_as_uuid(agreement_id),
            )
            keys = {(str(row["item_id"]), str(row["location_id"])) for row in covered}
            items = [row for row in items if (str(row.get("item_id")), str(row.get("location_id"))) in keys]
        return sum((dec(row.get("qty_on_hand")) for row in items if str(row.get("qty_class")) == "ON_HAND"), ZERO)

    async def create_transfer(
        self, data: Mapping[str, Any], *, actor_id: str | None = None, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        await self._require_enabled()
        replay = await self._idem_begin("POST /api/v1/consignment-transfers", idempotency_key, data, actor_id)
        if replay:
            return replay["replay"]
        transfer_type = str(data.get("transfer_type") or "").upper()
        if transfer_type not in {"RECEIPT_TO_CONSIGNED", "CONSUME_ON_SALE", "RETURN_TO_VENDOR", "ADJUST"}:
            raise ValidationFailed("invalid transfer_type")
        agreement = await self._require_row(
            "consignment_agreement",
            data.get("consignment_agreement_id") or data.get("agreement_id"),
            "consignment agreement",
        )
        if transfer_type == "RECEIPT_TO_CONSIGNED" and agreement["status"] != "ACTIVE":
            raise AgreementNotActive(f"agreement is {agreement['status']}; receipts require ACTIVE")
        if transfer_type in {"RETURN_TO_VENDOR", "ADJUST"} and agreement["status"] == "DRAFT":
            raise AgreementNotActive("agreement must be approved before adjustments/returns")
        if transfer_type == "CONSUME_ON_SALE":
            require_external = bool(await self._setting("consumption_requires_external_ref", True))
            if require_external and not (data.get("external_ref") or data.get("manual_approval_ref")):
                raise ValidationFailed(
                    "consume requires external_ref or manual_approval_ref (consigned.consumption_requires_external_ref=true)"
                )
        lines = list(data.get("lines") or [])
        if not lines:
            raise ValidationFailed("at least one transfer line is required")
        transfer_id = uuid.uuid4()
        vendor_id = data.get("vendor_id") or agreement["vendor_id"]
        await self._exec(
            """
            INSERT INTO consigned.consignment_ownership_transfer
                (id, tenant_id, document_number, consignment_agreement_id, vendor_id, transfer_type, status, transaction_date,
                 external_ref, memo, created_by, updated_by)
            VALUES (:id, :tenant_id, :number, :agreement, :vendor, :type, 'DRAFT', :date, :external, :memo, :actor, :actor)
            """,
            id=transfer_id,
            number=await self._next_number("CTR"),
            agreement=_as_uuid(agreement["id"]),
            vendor=_as_uuid(vendor_id),
            type=transfer_type,
            date=_date(data.get("transaction_date")) or date.today(),
            external=data.get("external_ref"),
            memo=data.get("memo"),
            actor=_as_uuid(actor_id) if actor_id else None,
        )
        for index, line in enumerate(lines, start=1):
            item = await self._item(line.get("item_id"))
            if str(item["item_type_code"]).upper() in ASSEMBLY_TYPES:
                raise AssemblyNotConsignable("assembly/WIP/phantom/outsourced items cannot be consigned (D6)")
            location = await self._location(line.get("location_id"))
            agreement_item = await self._agreement_item(agreement["id"], item["id"], location["id"])
            qty_value = qty(line.get("qty"))
            if qty_value <= 0:
                raise ValidationFailed(f"line {index} qty must be > 0")
            if transfer_type == "RECEIPT_TO_CONSIGNED":
                from_ownership, to_ownership = "OWNED", "CONSIGNED"
                from_status = agreement_item["owned_status_id"]
                to_status = agreement_item["consigned_status_id"]
            elif transfer_type == "CONSUME_ON_SALE":
                from_ownership, to_ownership = "CONSIGNED", "OWNED"
                from_status = agreement_item["consigned_status_id"]
                to_status = agreement_item["owned_status_id"]
                on_hand = await self._consigned_balance(
                    item_id=item["id"], location_id=location["id"], status_id=from_status
                )
                if qty_value > on_hand:
                    raise ConsignedQtyExceeded(
                        "consigned on-hand is insufficient",
                        details={"available": _num(on_hand), "requested": _num(qty_value)},
                    )
            elif transfer_type == "RETURN_TO_VENDOR":
                from_ownership, to_ownership = "CONSIGNED", "OWNED"
                from_status = agreement_item["consigned_status_id"]
                to_status = agreement_item["owned_status_id"]
                on_hand = await self._consigned_balance(
                    item_id=item["id"], location_id=location["id"], status_id=from_status
                )
                if qty_value > on_hand:
                    raise ConsignedQtyExceeded(
                        "consigned on-hand is insufficient", details={"available": _num(on_hand)}
                    )
            else:  # ADJUST
                direction = str(line.get("ownership") or "CONSIGNED").upper()
                from_ownership, to_ownership = (
                    ("OWNED", "CONSIGNED") if direction == "CONSIGNED" else ("CONSIGNED", "OWNED")
                )
                from_status = agreement_item["owned_status_id" if from_ownership == "OWNED" else "consigned_status_id"]
                to_status = agreement_item["consigned_status_id" if to_ownership == "CONSIGNED" else "owned_status_id"]
                if from_ownership == "CONSIGNED" and to_ownership == "CONSIGNED":
                    raise ValidationFailed("adjust line ownership must differ")
            unit_cost = money(
                line.get("unit_cost") if line.get("unit_cost") is not None else agreement_item["unit_cost"]
            )
            serial_ids = [str(value) for value in (line.get("serial_ids") or [])]
            if serial_ids and len(serial_ids) != int(qty_value):
                raise ValidationFailed("serial_ids count must equal line qty (CONS-RULE-03)")
            await self._exec(
                """
                INSERT INTO consigned.consignment_ownership_transfer_line
                    (id, tenant_id, consignment_ownership_transfer_id, line_number, item_id, location_id, from_ownership,
                     to_ownership, from_status_id, to_status_id, qty, uom_id, lot_id, serial_ids, unit_cost, value_base, bin_id)
                VALUES (:id, :tenant_id, :transfer, :line, :item, :location, :from_ownership, :to_ownership, :from_status,
                        :to_status, :qty, :uom, :lot, :serials, :cost, :value, :bin)
                """,
                id=uuid.uuid4(),
                transfer=transfer_id,
                line=index,
                item=_as_uuid(item["id"]),
                location=_as_uuid(location["id"]),
                from_ownership=from_ownership,
                to_ownership=to_ownership,
                from_status=_as_uuid(from_status),
                to_status=_as_uuid(to_status),
                qty=qty_value,
                uom=_as_uuid(line["uom_id"]) if line.get("uom_id") else None,
                lot=_as_uuid(line["lot_id"]) if line.get("lot_id") else None,
                serials=[_as_uuid(value) for value in serial_ids] or None,
                cost=unit_cost,
                value=money(unit_cost * qty_value),
                bin=_as_uuid(line["bin_id"]) if line.get("bin_id") else None,
            )
        await self._audit(
            "consigned.transfer.created",
            "consigned.consignment_ownership_transfer",
            transfer_id,
            {"transfer_type": transfer_type, "lines": len(lines)},
            actor_id,
        )
        result = await self.get_transfer(transfer_id)
        await self._idem_complete("POST /api/v1/consignment-transfers", idempotency_key, result, transfer_id, 201)
        return result

    async def get_transfer(self, transfer_id: Any) -> dict[str, Any]:
        transfer = await self._require_row("consignment_ownership_transfer", transfer_id, "consignment transfer")
        transfer["lines"] = await self._all(
            "SELECT * FROM consigned.consignment_ownership_transfer_line WHERE tenant_id = :tenant_id AND consignment_ownership_transfer_id = :id ORDER BY line_number",
            id=_as_uuid(transfer_id),
        )
        transfer["consumption"] = await self._all(
            "SELECT * FROM consigned.consignment_consumption WHERE tenant_id = :tenant_id AND source_transfer_id = :id",
            id=_as_uuid(transfer_id),
        )
        return transfer

    async def list_transfers(
        self,
        *,
        agreement_id: str | None = None,
        status: str | None = None,
        vendor_id: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        clauses = ["tenant_id = :tenant_id"]
        params: dict[str, Any] = {"limit": max(1, min(int(limit), 200))}
        if agreement_id:
            clauses.append("consignment_agreement_id = :agreement")
            params["agreement"] = _as_uuid(agreement_id)
        if status:
            clauses.append("status = :status")
            params["status"] = status.upper()
        if vendor_id:
            clauses.append("vendor_id = :vendor")
            params["vendor"] = _as_uuid(vendor_id)
        return {
            "items": await self._all(
                f"SELECT * FROM consigned.consignment_ownership_transfer WHERE {' AND '.join(clauses)} ORDER BY created_at DESC LIMIT :limit",
                **params,
            ),
            "next_cursor": None,
        }

    async def submit_transfer(
        self, transfer_id: Any, *, actor_id: str | None = None, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        transfer = await self._require_row("consignment_ownership_transfer", transfer_id, "consignment transfer")
        if transfer["status"] != "DRAFT":
            raise InvalidStateTransition(f"transfer is {transfer['status']}, expected DRAFT")
        await self._exec(
            "UPDATE consigned.consignment_ownership_transfer SET status = 'PENDING_APPROVAL', updated_at = now(), version = version + 1 WHERE tenant_id = :tenant_id AND id = :id",
            id=_as_uuid(transfer_id),
        )
        await self._audit(
            "consigned.transfer.submitted", "consigned.consignment_ownership_transfer", transfer_id, {}, actor_id
        )
        return {"id": str(transfer_id), "status": "PENDING_APPROVAL"}

    async def approve_transfer(
        self,
        transfer_id: Any,
        *,
        actor_id: str | None = None,
        approval_ref: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        transfer = await self._require_row("consignment_ownership_transfer", transfer_id, "consignment transfer")
        if transfer["status"] not in {"DRAFT", "PENDING_APPROVAL"}:
            raise InvalidStateTransition(f"transfer is {transfer['status']}, expected DRAFT/PENDING_APPROVAL")
        await self._exec(
            "UPDATE consigned.consignment_ownership_transfer SET status = 'APPROVED', approved_by = :actor, approved_at = now(), updated_at = now(), version = version + 1 WHERE tenant_id = :tenant_id AND id = :id",
            actor=_as_uuid(actor_id) if actor_id else None,
            id=_as_uuid(transfer_id),
        )
        await self._audit(
            "consigned.transfer.approved",
            "consigned.consignment_ownership_transfer",
            transfer_id,
            {"approval_ref": approval_ref},
            actor_id,
        )
        return {"id": str(transfer_id), "status": "APPROVED"}

    async def void_transfer(self, transfer_id: Any, reason: str, *, actor_id: str | None = None) -> dict[str, Any]:
        transfer = await self._require_row("consignment_ownership_transfer", transfer_id, "consignment transfer")
        if transfer["status"] not in {"DRAFT", "PENDING_APPROVAL", "APPROVED"}:
            raise InvalidStateTransition(f"transfer is {transfer['status']}; only unposted transfers can be voided")
        await self._exec(
            "UPDATE consigned.consignment_ownership_transfer SET status = 'VOID', updated_at = now(), version = version + 1 WHERE tenant_id = :tenant_id AND id = :id",
            id=_as_uuid(transfer_id),
        )
        await self._audit(
            "consigned.transfer.voided",
            "consigned.consignment_ownership_transfer",
            transfer_id,
            {"reason": reason},
            actor_id,
        )
        return {"id": str(transfer_id), "status": "VOID"}

    async def post_transfer(
        self,
        transfer_id: Any,
        *,
        actor_id: str | None = None,
        idempotency_key: str | None = None,
        external_ref: str | None = None,
    ) -> dict[str, Any]:
        from app.modules.inventory_ledger.facade import PostingPort

        transfer = await self.get_transfer(transfer_id)
        if transfer["status"] not in {"DRAFT", "PENDING_APPROVAL", "APPROVED"}:
            raise InvalidStateTransition(f"transfer is {transfer['status']}, expected unposted")
        if not transfer["lines"]:
            raise ValidationFailed("transfer has no lines")
        transfer_type = transfer["transfer_type"]
        port = PostingPort(self.session, self.tenant_id)
        key = idempotency_key or f"consigned:{transfer_id}"
        transaction_date = transfer["transaction_date"]
        movements: list[dict[str, Any]] = []
        for line in transfer["lines"]:
            base = {
                "item_id": str(line["item_id"]),
                "location_id": str(line["location_id"]),
                "inventory_status_id": str(line["to_status_id"] or line["from_status_id"]),
                "qty": _num(line["qty"]),
                "unit_cost": _num(line["unit_cost"]),
                "value_base": _num(line["value_base"]),
                "source_line_ref": str(line["line_number"]),
                "details": [
                    {
                        "qty": _num(line["qty"]),
                        "inventory_status_id": str(line["to_status_id"]),
                        "ownership": line["to_ownership"],
                    }
                ],
            }
            if transfer_type == "RECEIPT_TO_CONSIGNED":
                movements.append(
                    {
                        **base,
                        "ownership": "CONSIGNED",
                        "movement_type": "CONSIGNED_RECEIPT",
                        "details": [
                            {
                                "qty": _num(line["qty"]),
                                "inventory_status_id": str(line["to_status_id"]),
                                "ownership": "CONSIGNED",
                            }
                        ],
                    }
                )
            elif transfer_type == "CONSUME_ON_SALE":
                # Two ownership positions in one posting: CONSIGNED out, OWNED in (value conserved).
                consigned = {
                    "item_id": str(line["item_id"]),
                    "location_id": str(line["location_id"]),
                    "inventory_status_id": str(line["from_status_id"]),
                    "ownership": "CONSIGNED",
                    "qty": _num(-dec(line["qty"])),
                    "value_base": _num(-dec(line["value_base"])),
                    "unit_cost": _num(line["unit_cost"]),
                    "movement_type": "CONSIGNED_OWNERSHIP",
                    "source_line_ref": str(line["line_number"]),
                    "details": [
                        {
                            "qty": _num(-dec(line["qty"])),
                            "inventory_status_id": str(line["from_status_id"]),
                            "ownership": "CONSIGNED",
                        }
                    ],
                }
                owned = {
                    "item_id": str(line["item_id"]),
                    "location_id": str(line["location_id"]),
                    "inventory_status_id": str(line["to_status_id"]),
                    "ownership": "OWNED",
                    "qty": _num(line["qty"]),
                    "value_base": _num(line["value_base"]),
                    "unit_cost": _num(line["unit_cost"]),
                    "movement_type": "CONSIGNED_OWNERSHIP",
                    "source_line_ref": str(line["line_number"]),
                    "details": [
                        {
                            "qty": _num(line["qty"]),
                            "inventory_status_id": str(line["to_status_id"]),
                            "ownership": "OWNED",
                        }
                    ],
                }
                movements.extend([consigned, owned])
            else:  # RETURN_TO_VENDOR / ADJUST: consigned adjustment (quantity delta may be negative)
                sign = -1 if transfer_type == "RETURN_TO_VENDOR" else (1 if line["to_ownership"] == "CONSIGNED" else -1)
                signed_qty = dec(line["qty"]) * sign
                movements.append(
                    {
                        "item_id": str(line["item_id"]),
                        "location_id": str(line["location_id"]),
                        "inventory_status_id": str(line["from_status_id"] if sign < 0 else line["to_status_id"]),
                        "ownership": "CONSIGNED",
                        "qty": _num(signed_qty),
                        "value_base": _num(dec(line["value_base"]) * sign),
                        "unit_cost": _num(line["unit_cost"]),
                        "movement_type": "CONSIGNED_ADJUST",
                        "source_line_ref": str(line["line_number"]),
                        "details": [
                            {
                                "qty": _num(signed_qty),
                                "inventory_status_id": str(
                                    line["from_status_id"] if sign < 0 else line["to_status_id"]
                                ),
                                "ownership": "CONSIGNED",
                            }
                        ],
                    }
                )
        if transfer_type == "RECEIPT_TO_CONSIGNED":
            result = await port.post_consigned_receipt(
                transaction_date=transaction_date,
                source_doc_type="CONSIGNMENT_TRANSFER",
                source_doc_id=str(transfer_id),
                external_ref=external_ref or transfer.get("external_ref"),
                idempotency_key=key,
                movements=movements,
            )
        elif transfer_type == "CONSUME_ON_SALE":
            result = await port.post_consigned_ownership(
                transaction_date=transaction_date,
                source_doc_type="CONSIGNMENT_TRANSFER",
                source_doc_id=str(transfer_id),
                external_ref=external_ref or transfer.get("external_ref"),
                idempotency_key=key,
                movements=movements,
            )
        else:
            result = await port.post_consigned_adjustment(
                transaction_date=transaction_date,
                source_doc_type="CONSIGNMENT_TRANSFER",
                source_doc_id=str(transfer_id),
                external_ref=external_ref or transfer.get("external_ref"),
                idempotency_key=key,
                movements=movements,
            )
        await self._exec(
            "UPDATE consigned.consignment_ownership_transfer SET status = 'POSTED', posted_transaction_id = :txn, updated_at = now(), version = version + 1 WHERE tenant_id = :tenant_id AND id = :id",
            txn=_as_uuid(result["transaction_id"]),
            id=_as_uuid(transfer_id),
        )
        event = {
            "RECEIPT_TO_CONSIGNED": "consigned.received",
            "CONSUME_ON_SALE": "consigned.owned",
            "RETURN_TO_VENDOR": "consigned.returned",
            "ADJUST": "consigned.adjusted",
        }[transfer_type]
        await self._emit(
            event,
            "consignment_ownership_transfer",
            transfer_id,
            {
                "transfer_id": str(transfer_id),
                "agreement_id": str(transfer["consignment_agreement_id"]),
                "transfer_type": transfer_type,
                "inventory_transaction_id": result["transaction_id"],
                "lines": [
                    {
                        "line_id": str(line["id"]),
                        "item_id": str(line["item_id"]),
                        "location_id": str(line["location_id"]),
                        "qty": _num(line["qty"]),
                        "value_base": _num(line["value_base"]),
                    }
                    for line in transfer["lines"]
                ],
            },
        )
        if transfer_type == "CONSUME_ON_SALE":
            vendor_bill_id = await self._create_consumption_bill(transfer, actor_id=actor_id)
            await self._exec(
                "UPDATE consigned.consignment_ownership_transfer SET vendor_bill_id = :bill, updated_at = now(), version = version + 1 WHERE tenant_id = :tenant_id AND id = :id",
                bill=_as_uuid(vendor_bill_id) if vendor_bill_id else None,
                id=_as_uuid(transfer_id),
            )
            for line in transfer["lines"]:
                consumption_id = uuid.uuid4()
                await self._exec(
                    """
                    INSERT INTO consigned.consignment_consumption
                        (id, tenant_id, consignment_agreement_id, item_id, location_id, consumption_date, period_key,
                         qty_consumed, value_base, source_transfer_id, source_transfer_line_id, vendor_bill_id)
                    VALUES (:id, :tenant_id, :agreement, :item, :location, :date, :period, :qty, :value, :transfer, :line, :bill)
                    ON CONFLICT (tenant_id, source_transfer_line_id) DO NOTHING
                    """,
                    id=consumption_id,
                    agreement=_as_uuid(transfer["consignment_agreement_id"]),
                    item=_as_uuid(line["item_id"]),
                    location=_as_uuid(line["location_id"]),
                    date=_as_uuid(transfer_id) if False else _date(transfer["transaction_date"]),
                    period=str(transfer["transaction_date"])[:7],
                    qty=line["qty"],
                    value=line["value_base"],
                    transfer=_as_uuid(transfer_id),
                    line=_as_uuid(line["id"]),
                    bill=_as_uuid(vendor_bill_id) if vendor_bill_id else None,
                )
                await self._emit(
                    "consigned.consumed",
                    "consignment_consumption",
                    consumption_id,
                    {
                        "consumption_id": str(consumption_id),
                        "agreement_id": str(transfer["consignment_agreement_id"]),
                        "item_id": str(line["item_id"]),
                        "period_key": str(transfer["transaction_date"])[:7],
                        "qty": _num(line["qty"]),
                        "value": _num(line["value_base"]),
                    },
                )
            await self._finance_intent(
                transfer=transfer,
                rule="CONSOWN",
                account_ref="CONSIGNED_INVENTORY",
                amount=dec(
                    transfer.get("total_value") or sum((dec(line["value_base"]) for line in transfer["lines"]), ZERO)
                ),
                debit=True,
                line_no=1,
            )
            await self._finance_intent(
                transfer=transfer,
                rule="CONSOWN",
                account_ref="ACCOUNTS_PAYABLE",
                amount=dec(
                    transfer.get("total_value") or sum((dec(line["value_base"]) for line in transfer["lines"]), ZERO)
                ),
                debit=False,
                line_no=2,
            )
        elif transfer_type == "RECEIPT_TO_CONSIGNED":
            total = sum((dec(line["value_base"]) for line in transfer["lines"]), ZERO)
            await self._finance_intent(
                transfer=transfer,
                rule="CONSRCPT",
                account_ref="CONSIGNED_INVENTORY",
                amount=total,
                debit=True,
                line_no=1,
            )
            await self._finance_intent(
                transfer=transfer, rule="CONSRCPT", account_ref="CONSIGNED_OFFSET", amount=total, debit=False, line_no=2
            )
        elif transfer_type == "ADJUST":
            total = sum((dec(line["value_base"]) for line in transfer["lines"]), ZERO)
            await self._finance_intent(
                transfer=transfer,
                rule="CONSADJ",
                account_ref="CONSIGNED_ADJUSTMENT",
                amount=total,
                debit=True,
                line_no=1,
            )
            await self._finance_intent(
                transfer=transfer,
                rule="CONSADJ",
                account_ref="CONSIGNED_INVENTORY",
                amount=total,
                debit=False,
                line_no=2,
            )
        await self._audit(
            f"consigned.transfer.posted.{transfer_type.lower()}",
            "consigned.consignment_ownership_transfer",
            transfer_id,
            {"transaction_id": result["transaction_id"]},
            actor_id,
        )
        updated = await self.get_transfer(transfer_id)
        return {
            "id": str(transfer_id),
            "status": "POSTED",
            "posted_transaction_id": result["transaction_id"],
            "vendor_bill_id": updated.get("vendor_bill_id"),
            "consumption": updated["consumption"],
        }

    async def _create_consumption_bill(self, transfer: Mapping[str, Any], *, actor_id: Any = None) -> str | None:
        if not await self._setting("auto_bill_on_consumption", True):
            return None
        from app.modules.procurement.facade import ProcurementFacade

        facade = ProcurementFacade(self.session, self.tenant_id)
        lines = [
            {
                "item_id": str(line["item_id"]),
                "qty": _num(line["qty"]),
                "unit_price": _num(line["unit_cost"]),
                "item_receipt_line_id": None,
                "purchase_order_line_id": None,
            }
            for line in transfer["lines"]
        ]
        bill = await facade.create_vendor_bill_for_consignment(
            source_transfer_id=str(transfer["id"]),
            vendor_id=str(transfer["vendor_id"]),
            lines=lines,
            options={
                "transaction_date": transfer["transaction_date"],
                "currency_code": transfer.get("currency_code")
                or await self._scalar(
                    "SELECT currency_code FROM consigned.consignment_agreement WHERE tenant_id = :tenant_id AND id = :id",
                    id=_as_uuid(transfer["consignment_agreement_id"]),
                )
                or "USD",
                "actor_id": actor_id,
            },
        )
        return str(bill["id"])

    async def consume(
        self, data: Mapping[str, Any], *, actor_id: str | None = None, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        payload = {**data, "transfer_type": "CONSUME_ON_SALE"}
        transfer = await self.create_transfer(payload, actor_id=actor_id)
        return await self.post_transfer(
            transfer["id"], actor_id=actor_id, idempotency_key=idempotency_key, external_ref=data.get("external_ref")
        )

    async def reverse_transfer(
        self, transfer_id: Any, reason: str, *, actor_id: str | None = None, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        from app.modules.inventory_ledger.facade import PostingPort

        transfer = await self.get_transfer(transfer_id)
        if transfer["status"] != "POSTED":
            raise InvalidStateTransition(f"transfer is {transfer['status']}, expected POSTED")
        consumption_dependents = int(
            await self._scalar(
                "SELECT count(*) FROM consigned.consignment_consumption WHERE tenant_id = :tenant_id AND source_transfer_id = :id",
                id=_as_uuid(transfer_id),
            )
            or 0
        )
        if transfer["transfer_type"] == "CONSUME_ON_SALE" and consumption_dependents:
            raise DependentDocuments(
                "consumption transfer has vendor bill/consumption links; use vendor credit/reversal documents"
            )
        port = PostingPort(self.session, self.tenant_id)
        result = await port.reverse(
            str(transfer["posted_transaction_id"]),
            reason=reason,
            idempotency_key=idempotency_key or f"consigned-reverse:{transfer_id}",
        )
        await self._exec(
            "UPDATE consigned.consignment_ownership_transfer SET status = 'REVERSED', updated_at = now(), version = version + 1 WHERE tenant_id = :tenant_id AND id = :id",
            id=_as_uuid(transfer_id),
        )
        await self._emit(
            "consigned.reversed",
            "consignment_ownership_transfer",
            transfer_id,
            {
                "transfer_id": str(transfer_id),
                "reversal_transaction_id": result["transaction_id"],
                "linked_bill_id": transfer.get("vendor_bill_id"),
            },
        )
        await self._audit(
            "consigned.transfer.reversed",
            "consigned.consignment_ownership_transfer",
            transfer_id,
            {"reason": reason},
            actor_id,
        )
        return {"id": str(transfer_id), "status": "REVERSED", "reversal_transaction_id": result["transaction_id"]}

    # ------------------------------------------------------------------------------ consumption --
    async def list_consumption(
        self,
        *,
        agreement_id: str | None = None,
        item_id: str | None = None,
        period_key: str | None = None,
        vendor_bill_id: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        clauses = ["tenant_id = :tenant_id"]
        params: dict[str, Any] = {"limit": max(1, min(int(limit), 500))}
        if agreement_id:
            clauses.append("consignment_agreement_id = :agreement")
            params["agreement"] = _as_uuid(agreement_id)
        if item_id:
            clauses.append("item_id = :item")
            params["item"] = _as_uuid(item_id)
        if period_key:
            clauses.append("period_key = :period")
            params["period"] = period_key
        if vendor_bill_id:
            clauses.append("vendor_bill_id = :bill")
            params["bill"] = _as_uuid(vendor_bill_id)
        rows = await self._all(
            f"SELECT * FROM consigned.consignment_consumption WHERE {' AND '.join(clauses)} ORDER BY consumption_date DESC LIMIT :limit",
            **params,
        )
        return {"items": rows, "next_cursor": None}

    # ----------------------------------------------------------------------------------- reads --
    async def balances(
        self,
        *,
        vendor_id: str | None = None,
        agreement_id: str | None = None,
        item_id: str | None = None,
        location_id: str | None = None,
    ) -> dict[str, Any]:
        from app.modules.inventory_ledger.facade import BalanceReadPort

        port = BalanceReadPort(self.session, self.tenant_id)
        filters: dict[str, Any] = {"limit": 500}
        if item_id:
            filters["item_id"] = item_id
        if location_id:
            filters["location_id"] = location_id
        consigned = await port.balances(ownership="CONSIGNED", **filters)
        owned = await port.balances(ownership="OWNED", **filters)
        if agreement_id is not None:
            covered = await self._all(
                "SELECT item_id, location_id FROM consigned.consignment_agreement_item WHERE tenant_id = :tenant_id AND consignment_agreement_id = :id",
                id=_as_uuid(agreement_id),
            )
            keys = {(str(row["item_id"]), str(row["location_id"])) for row in covered}
            consigned["items"] = [
                row
                for row in consigned.get("items", [])
                if (str(row.get("item_id")), str(row.get("location_id"))) in keys
            ]
            owned["items"] = [
                row for row in owned.get("items", []) if (str(row.get("item_id")), str(row.get("location_id"))) in keys
            ]
        return {
            "consigned": [row for row in consigned.get("items", []) if str(row.get("qty_class")) == "ON_HAND"],
            "owned": [row for row in owned.get("items", []) if str(row.get("qty_class")) == "ON_HAND"],
            "inv_27_separated": True,
        }

    async def eligibility(
        self, *, item_id: str, location_id: str, vendor_id: str | None = None, agreement_id: str | None = None
    ) -> dict[str, Any]:
        reasons: list[str] = []
        try:
            item = await self._item(item_id)
            if not item["is_active"]:
                reasons.append("ITEM_INACTIVE")
            if not item.get("allow_vendor_consignment"):
                reasons.append("ITEM_NOT_CONSIGNABLE")
            if str(item["item_type_code"]).upper() in ASSEMBLY_TYPES:
                reasons.append("ASSEMBLY_NOT_CONSIGNABLE")
        except NotFound:
            reasons.append("ITEM_NOT_FOUND")
        try:
            location = await self._location(location_id)
            if not location.get("is_consignment_allowed"):
                reasons.append("LOCATION_NOT_CONSIGNMENT_ALLOWED")
        except NotFound:
            reasons.append("LOCATION_NOT_FOUND")
        if vendor_id:
            vendor = await self._one(
                "SELECT id, is_inactive FROM procurement.vendor WHERE tenant_id = :tenant_id AND id = :id",
                id=_as_uuid(vendor_id),
            )
            if vendor is None:
                reasons.append("VENDOR_NOT_FOUND")
            elif vendor["is_inactive"]:
                reasons.append("VENDOR_INACTIVE")
        if agreement_id:
            agreement = await self._require_row("consignment_agreement", agreement_id, "consignment agreement")
            if agreement["status"] != "ACTIVE":
                reasons.append("AGREEMENT_NOT_ACTIVE")
        return {"eligible": not reasons, "reasons": reasons}

    async def validate_guards(self) -> dict[str, Any]:
        reasons: list[str] = []
        guard_enabled = bool(await self._setting("assembly_incompatible_guard", True)) or await self._flag(
            "consigned.assembly_incompatible_guard", True
        )
        manufacturing_flags = await self._all(
            "SELECT flag_key FROM platform.feature_flag WHERE tenant_id = :tenant_id AND is_enabled AND (flag_key LIKE 'manufacturing.%' OR flag_key LIKE '%.manufacturing.%')"
        )
        consigned_assembly_items = int(
            await self._scalar(
                """
            SELECT count(*) FROM item_master.item i
              JOIN item_master.item_type_definition t ON t.tenant_id = i.tenant_id AND t.id = i.item_type_id
              JOIN item_master.item_inventory inv ON inv.tenant_id = i.tenant_id AND inv.item_id = i.id
             WHERE i.tenant_id = :tenant_id AND i.is_active AND t.code = 'ASSEMBLY' AND inv.allow_vendor_consignment
            """
            )
            or 0
        )
        if guard_enabled and manufacturing_flags:
            reasons.append("MANUFACTURING_ENABLED")
        if guard_enabled and consigned_assembly_items:
            reasons.append("ASSEMBLY_ITEM_FLAGGED_CONSIGNABLE")
        if reasons:
            await self._audit(
                "consigned.guard.rejected", "consigned.tenant_guard", self.tenant_id, {"reasons": reasons}
            )
            await self._emit(
                "consigned.guard.rejected",
                "tenant_guard",
                self.tenant_id,
                {
                    "reason_codes": reasons,
                    "config_snapshot": {"manufacturing_flags": [row["flag_key"] for row in manufacturing_flags]},
                },
            )
        return {"ok": not reasons, "reasons": reasons, "guard_enabled": guard_enabled}

    async def guarded_object_operation(self, operation: str) -> None:
        raise ProhibitedOperation(
            f"ordinary {operation} is prohibited for consigned inventory; use consignment ADJUST transfers (T-23)",
            details={"operation": operation},
        )

    # -------------------------------------------------------------------------------- settings --
    SETTING_KEYS = (
        "assembly_incompatible_guard",
        "default_consigned_status_code",
        "default_owned_status_code",
        "payment_trigger_default",
        "consumption_requires_external_ref",
        "auto_bill_on_consumption",
        "reconcile_strict",
    )

    async def get_settings(self) -> dict[str, Any]:
        items = []
        for key in self.SETTING_KEYS:
            row = await self._one(
                "SELECT value, value_type FROM platform.tenant_setting WHERE tenant_id = :tenant_id AND namespace = 'inventory' AND key = :setting_key",
                setting_key=f"consigned.{key}",
            )
            value = row["value"] if row else None
            if isinstance(value, str) and value.startswith('"') and value.endswith('"'):
                value = value[1:-1]
            items.append({"key": key, "value": value})
        return {"items": items}

    async def patch_settings(self, data: Mapping[str, Any], *, actor_id: str | None = None) -> dict[str, Any]:
        allowed = set(self.SETTING_KEYS)
        import json as _json

        for key, value in data.items():
            if key not in allowed:
                raise ValidationFailed(f"unknown consignment setting {key!r}")
            value_type = (
                "boolean" if isinstance(value, bool) else ("number" if isinstance(value, int | float) else "string")
            )
            raw = (
                _json.dumps(value)
                if value_type == "string"
                else str(value).lower()
                if value_type == "boolean"
                else str(value)
            )
            await self._exec(
                """
                INSERT INTO platform.tenant_setting
                    (id, tenant_id, namespace, key, value, value_type, is_default, created_at, updated_at, version)
                VALUES (gen_random_uuid(), :tenant_id, 'inventory', :key, CAST(:value AS jsonb), :value_type, false, now(), now(), 1)
                ON CONFLICT (tenant_id, namespace, key)
                DO UPDATE SET value = EXCLUDED.value, value_type = EXCLUDED.value_type, is_default = false,
                              updated_at = now(), version = platform.tenant_setting.version + 1
                """,
                key=f"consigned.{key}",
                value=raw,
                value_type=value_type,
            )
        await self._audit("consigned.settings.updated", "consigned.settings", self.tenant_id, dict(data), actor_id)
        return await self.get_settings()

    async def disable(self, *, actor_id: str | None = None) -> dict[str, Any]:
        on_hand = await self._consigned_on_hand()
        if on_hand > 0:
            raise StockRemains(
                "consigned on-hand must be zero before disabling", details={"consigned_on_hand": _num(on_hand)}
            )
        await self._exec(
            "UPDATE platform.feature_flag SET is_enabled = false, updated_at = now(), version = version + 1 WHERE tenant_id = :tenant_id AND flag_key = 'consigned.inventory.enabled'"
        )
        await self._audit(
            "consigned.disabled", "consigned.settings", self.tenant_id, {"consigned_on_hand": "0"}, actor_id
        )
        return {"enabled": False, "consigned_on_hand": "0"}
