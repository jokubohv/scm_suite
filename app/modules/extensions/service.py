"""FS-14 extension registries — warranty terms/registrations/claims/repair data, customer part numbers and
the Auto Assign UPC registry.

Data/logic only: this module never posts inventory/order/finance documents. The UPC registry is authoritative
and mirrors the item header through the FS-01 item-master facade in the same transaction.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.idempotency_store import IdempotencyStore
from app.core.outbox import emit
from app.core.sequences import SequenceError, SequenceService
from app.modules.extensions.errors import (
    ClaimActionNotAllowed,
    CustomerPartDuplicate,
    CustomerPartNotFound,
    DomainDisabled,
    ExtensionError,
    InvalidStateTransition,
    ItemNotUpcEligible,
    NotFound,
    RegistrationVoided,
    SerialAlreadyRegistered,
    SerialNotFound,
    SerialRequired,
    UpcAlreadyAssigned,
    UpcChecksumInvalid,
    UpcRetiredNeverReusable,
    ValidationFailed,
    ValidationRequired,
)

EVENT_TYPES = (
    "warranty.terms.updated",
    "warranty.registered",
    "warranty.registration.voided",
    "warranty.registration.expired",
    "warranty.claim.opened",
    "warranty.claim.validated",
    "warranty.claim.approved",
    "warranty.claim.rejected",
    "warranty.claim.action.recorded",
    "warranty.claim.completed",
    "warranty.claim.cancelled",
    "warranty.repair.created",
    "warranty.repair.updated",
    "warranty.repair.completed",
    "warranty.repair.cancelled",
    "extensions.customer_part.created",
    "extensions.customer_part.updated",
    "extensions.customer_part.deactivated",
    "extensions.upc.assigned",
    "extensions.upc.retired",
    "extensions.upc.batch.completed",
)
JOB_NAMES = (
    "extensions.warranty.expiry",
    "extensions.claims.reconcile",
    "extensions.upc.reconcile",
    "extensions.upc.assign",
    "extensions.registration.import.process",
    "extensions.customer_part.import.process",
)
UPC_ITEM_TYPES = frozenset({"INVENTORY", "SERIALIZED", "LOT_NUMBERED", "ASSEMBLY", "MATRIX_CHILD"})
REGISTRATION_ACTIVE = "ACTIVE"


def utc_now() -> datetime:
    return datetime.now(UTC)


def _as_uuid(value: Any) -> uuid.UUID:
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


def _today() -> date:
    """UTC date: DB timestamps/dates are UTC, so local _today() can cross a calendar boundary."""
    return datetime.now(UTC).date()


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


def add_months(start: date, months: int) -> date:
    total = start.year * 12 + (start.month - 1) + int(months)
    year, month = total // 12, total % 12 + 1
    day = min(
        start.day,
        [
            31,
            29 if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0) else 28,
            31,
            30,
            31,
            30,
            31,
            31,
            30,
            31,
            30,
            31,
        ][month - 1],
    )
    return date(year, month, day)


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


def _row(row: Any) -> dict[str, Any]:
    mapping = getattr(row, "_mapping", row)
    return _jsonable(dict(mapping))


def upc_check_digit(eleven_digits: str) -> int:
    """UPC-A mod-10 check digit (odd positions ×3, even ×1)."""
    if len(eleven_digits) != 11 or not eleven_digits.isdigit():
        raise UpcChecksumInvalid("UPC-A data part must be exactly 11 digits")
    total = sum(int(digit) * (3 if index % 2 == 0 else 1) for index, digit in enumerate(eleven_digits))
    return (10 - total % 10) % 10


def valid_upc_code(code: str) -> bool:
    return len(code) == 12 and code.isdigit() and int(code[11]) == upc_check_digit(code[:11])


class ExtensionService:
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
            setting_key=f"extensions.{key}",
        )
        if row is None:
            return default
        value = row["value"]
        return value[1:-1] if isinstance(value, str) and value.startswith('"') else value

    async def _require_enabled(self) -> None:
        if not await self._flag("extensions.registry.enabled", False):
            raise DomainDisabled("extensions.registry is disabled for this tenant (extensions.registry.enabled=false)")

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
            f"SELECT * FROM extensions.{table} WHERE tenant_id = :tenant_id AND id = :id", id=_as_uuid(row_id)
        )
        if row is None:
            raise NotFound(f"{label} not found")
        return row

    async def _item(self, item_id: Any) -> dict[str, Any]:
        item = await self._one(
            """
            SELECT i.id, i.item_number, i.display_name, i.is_active, i.upc_code, i.matrix_parent_id,
                   t.code AS item_type_code, t.can_stock, t.requires_numbering
              FROM item_master.item i JOIN item_master.item_type_definition t ON t.tenant_id = i.tenant_id AND t.id = i.item_type_id
             WHERE i.tenant_id = :tenant_id AND i.id = :id
            """,
            id=_as_uuid(item_id),
        )
        if item is None:
            raise NotFound("item not found")
        return item

    async def _validate_serial(self, item: Mapping[str, Any], serial_id: Any) -> None:
        if not serial_id:
            return
        row = await self._one(
            "SELECT id FROM traceability_numbered.serial_number WHERE tenant_id = :tenant_id AND id = :id AND item_id = :item",
            id=_as_uuid(serial_id),
            item=_as_uuid(item["id"]),
        )
        if row is None:
            raise SerialNotFound("serial number does not exist or does not belong to the item (FS-06)")

    # ------------------------------------------------------------------------- warranty terms --
    async def get_item_warranty(self, item_id: Any) -> dict[str, Any]:
        item = await self._item(item_id)
        row = await self._one(
            "SELECT * FROM extensions.item_warranty WHERE tenant_id = :tenant_id AND item_id = :item",
            item=_as_uuid(item["id"]),
        )
        return {"item_id": str(item["id"]), "item_number": item["item_number"], "warranty": row}

    async def put_item_warranty(
        self, item_id: Any, data: Mapping[str, Any], *, actor_id: str | None = None, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        await self._require_enabled()
        item = await self._item(item_id)
        duration = int(data.get("duration_months", data.get("warranty_duration_months", 12)))
        if duration < 0:
            raise ValidationFailed("warranty duration_months must be >= 0")
        row_id = uuid.uuid4()
        await self._exec(
            """
            INSERT INTO extensions.item_warranty
                (id, tenant_id, item_id, warranty_duration_months, warranty_terms, coverage, is_default_for_item, is_active,
                 created_by, updated_by)
            VALUES (:id, :tenant_id, :item, :duration, :terms, CAST(:coverage AS jsonb), true, :active, :actor, :actor)
            ON CONFLICT (tenant_id, item_id) DO UPDATE
            SET warranty_duration_months = EXCLUDED.warranty_duration_months,
                warranty_terms = EXCLUDED.warranty_terms, coverage = EXCLUDED.coverage,
                is_active = EXCLUDED.is_active, updated_at = now(), version = extensions.item_warranty.version + 1
            """,
            id=row_id,
            item=_as_uuid(item["id"]),
            duration=duration,
            terms=data.get("terms") or data.get("warranty_terms"),
            coverage=json.dumps(_jsonable(data.get("coverage") or {})),
            active=bool(data.get("is_active", True)),
            actor=_as_uuid(actor_id) if actor_id else None,
        )
        await self._emit(
            "warranty.terms.updated",
            "item_warranty",
            item["id"],
            {
                "item_id": str(item["id"]),
                "duration_months": duration,
                "is_active": bool(data.get("is_active", True)),
            },
        )
        await self._audit(
            "warranty.terms.updated", "extensions.item_warranty", item["id"], {"duration_months": duration}, actor_id
        )
        return await self.get_item_warranty(item["id"])

    # ---------------------------------------------------------------------------- registrations --
    async def _coverage(self, item_id: Any, *, start: date, duration_months: int | None = None) -> dict[str, Any]:
        terms = await self._one(
            "SELECT * FROM extensions.item_warranty WHERE tenant_id = :tenant_id AND item_id = :item AND is_active",
            item=_as_uuid(item_id),
        )
        if duration_months is None:
            duration_months = (
                int(terms["warranty_duration_months"])
                if terms
                else int(await self._setting("warranty.default_duration_months", 12))
            )
        end = add_months(start, duration_months)
        return {
            "duration_months": duration_months,
            "warranty_start": start,
            "warranty_end": end,
            "terms_ref": terms["id"] if terms else None,
        }

    async def create_registration(
        self, data: Mapping[str, Any], *, actor_id: str | None = None, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        await self._require_enabled()
        replay = await self._idem_begin("POST /api/v1/warranty/registrations", idempotency_key, data, actor_id)
        if replay:
            return replay["replay"]
        item = await self._item(data.get("item_id"))
        serial_id = data.get("serial_id")
        if bool(item.get("requires_numbering")) and not serial_id:
            raise SerialRequired("serialized items require serial_id (DM-EXT-01)")
        await self._validate_serial(item, serial_id)
        if serial_id and await self._scalar(
            "SELECT 1 FROM extensions.warranty_registration WHERE tenant_id = :tenant_id AND item_id = :item AND serial_id = :serial AND status = 'ACTIVE'",
            item=_as_uuid(item["id"]),
            serial=_as_uuid(serial_id),
        ):
            raise SerialAlreadyRegistered("an ACTIVE registration already exists for this serial (EXT-RULE-02)")
        source = str(data.get("source") or "MANUAL").upper()
        registered_at = _stamp(data.get("registered_at")) or utc_now()
        start = _date(data.get("warranty_start")) or registered_at.date()
        duration = (
            data.get("coverage_override", {}).get("duration_months")
            if isinstance(data.get("coverage_override"), Mapping)
            else data.get("duration_months")
        )
        coverage = await self._coverage(
            item["id"], start=start, duration_months=int(duration) if duration is not None else None
        )
        registration_id = uuid.uuid4()
        try:
            await self._exec(
                """
                INSERT INTO extensions.warranty_registration
                    (id, tenant_id, registration_number, item_id, serial_id, external_customer_ref, external_invoice_ref,
                     registered_at, warranty_start, warranty_end, source, status, notes, created_by, updated_by)
                VALUES (:id, :tenant_id, :number, :item, :serial, :customer, :invoice, :registered_at, :start, :end,
                        :source, 'ACTIVE', :notes, :actor, :actor)
                """,
                id=registration_id,
                number=await self._next_number("WR"),
                item=_as_uuid(item["id"]),
                serial=_as_uuid(serial_id) if serial_id else None,
                customer=data.get("external_customer_ref"),
                invoice=data.get("external_invoice_ref"),
                registered_at=registered_at,
                start=coverage["warranty_start"],
                end=coverage["warranty_end"],
                source=source,
                notes=data.get("notes"),
                actor=_as_uuid(actor_id) if actor_id else None,
            )
        except IntegrityError as exc:
            existing = await self._one(
                "SELECT * FROM extensions.warranty_registration WHERE tenant_id = :tenant_id AND item_id = :item AND serial_id IS NOT DISTINCT FROM :serial AND external_invoice_ref = :invoice AND source = 'AUTO'",
                item=_as_uuid(item["id"]),
                serial=_as_uuid(serial_id) if serial_id else None,
                invoice=data.get("external_invoice_ref"),
            )
            if existing is not None and source == "AUTO":
                return existing
            raise ValidationFailed("duplicate registration") from exc
        await self._emit(
            "warranty.registered",
            "warranty_registration",
            registration_id,
            {
                "registration_id": str(registration_id),
                "item_id": str(item["id"]),
                "serial_id": str(serial_id) if serial_id else None,
                "customer_ref": data.get("external_customer_ref"),
                "warranty_start": coverage["warranty_start"].isoformat(),
                "warranty_end": coverage["warranty_end"].isoformat(),
                "source": source,
            },
        )
        await self._audit(
            "warranty.registered", "extensions.warranty_registration", registration_id, {"source": source}, actor_id
        )
        result = await self.get_registration(registration_id)
        await self._idem_complete("POST /api/v1/warranty/registrations", idempotency_key, result, registration_id, 201)
        return result

    async def import_registrations(
        self, rows: Sequence[Mapping[str, Any]], *, actor_id: str | None = None
    ) -> dict[str, Any]:
        limit = int(await self._setting("warranty.registration_import_limit", 5000))
        if len(rows) > limit:
            raise ValidationFailed(f"import exceeds the configured limit ({limit} rows)")
        results = []
        for index, row in enumerate(rows, start=1):
            try:
                registration = await self.create_registration(
                    {**row, "source": row.get("source") or "IMPORT"}, actor_id=actor_id
                )
                results.append({"row": index, "status": "OK", "registration_id": registration["id"]})
            except ExtensionError as exc:
                results.append({"row": index, "status": "ERROR", "code": exc.code, "message": exc.message})
        return {
            "rows": results,
            "succeeded": sum(1 for row in results if row["status"] == "OK"),
            "failed": sum(1 for row in results if row["status"] == "ERROR"),
        }

    async def get_registration(self, registration_id: Any) -> dict[str, Any]:
        row = await self._require_row("warranty_registration", registration_id, "warranty registration")
        row["claims"] = await self._all(
            "SELECT id, claim_number, status, claim_type FROM extensions.warranty_claim WHERE tenant_id = :tenant_id AND warranty_registration_id = :id ORDER BY created_at",
            id=_as_uuid(registration_id),
        )
        return row

    async def list_registrations(
        self,
        *,
        item_id: str | None = None,
        serial_id: str | None = None,
        status: str | None = None,
        customer_ref: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        clauses = ["tenant_id = :tenant_id"]
        params: dict[str, Any] = {"limit": max(1, min(int(limit), 500))}
        if item_id:
            clauses.append("item_id = :item")
            params["item"] = _as_uuid(item_id)
        if serial_id:
            clauses.append("serial_id = :serial")
            params["serial"] = _as_uuid(serial_id)
        if status:
            clauses.append("status = :status")
            params["status"] = status.upper()
        if customer_ref:
            clauses.append("external_customer_ref = :customer")
            params["customer"] = customer_ref
        return {
            "items": await self._all(
                f"SELECT * FROM extensions.warranty_registration WHERE {' AND '.join(clauses)} ORDER BY registered_at DESC LIMIT :limit",
                **params,
            ),
            "next_cursor": None,
        }

    async def void_registration(
        self, registration_id: Any, reason: str, *, actor_id: str | None = None
    ) -> dict[str, Any]:
        registration = await self._require_row("warranty_registration", registration_id, "warranty registration")
        if registration["status"] != "ACTIVE":
            raise InvalidStateTransition(f"registration is {registration['status']}, expected ACTIVE")
        open_claims = int(
            await self._scalar(
                "SELECT count(*) FROM extensions.warranty_claim WHERE tenant_id = :tenant_id AND warranty_registration_id = :id AND status IN ('OPEN','UNDER_REVIEW','APPROVED')",
                id=_as_uuid(registration_id),
            )
            or 0
        )
        if open_claims:
            raise RegistrationVoided("registration has open claims; resolve them before voiding")
        await self._exec(
            "UPDATE extensions.warranty_registration SET status = 'VOID', voided_at = now(), void_reason = :reason, updated_at = now(), version = version + 1 WHERE tenant_id = :tenant_id AND id = :id",
            reason=reason,
            id=_as_uuid(registration_id),
        )
        await self._emit(
            "warranty.registration.voided",
            "warranty_registration",
            registration_id,
            {"registration_id": str(registration_id), "reason": reason},
        )
        await self._audit(
            "warranty.registration.voided",
            "extensions.warranty_registration",
            registration_id,
            {"reason": reason},
            actor_id,
        )
        return {"id": str(registration_id), "status": "VOID"}

    async def coverage(self, *, item_id: str, serial_id: str | None = None, as_of: str | None = None) -> dict[str, Any]:
        moment = _date(as_of) or _today()
        clauses = ["tenant_id = :tenant_id", "item_id = :item", "status = 'ACTIVE'"]
        params: dict[str, Any] = {"item": _as_uuid(item_id)}
        if serial_id:
            clauses.append("serial_id = :serial")
            params["serial"] = _as_uuid(serial_id)
        registration = await self._one(
            f"SELECT * FROM extensions.warranty_registration WHERE {' AND '.join(clauses)} ORDER BY warranty_end DESC LIMIT 1",
            **params,
        )
        if registration is None:
            return {"covered": False, "reason": "NO_ACTIVE_REGISTRATION"}
        start = _date(registration["warranty_start"])
        end = _date(registration["warranty_end"])
        covered = start is not None and end is not None and start <= moment <= end
        return {
            "covered": covered,
            "registration_id": registration["id"],
            "warranty_start": registration["warranty_start"],
            "warranty_end": registration["warranty_end"],
            "terms_ref": registration.get("registration_number"),
        }

    # ---------------------------------------------------------------------------------- claims --
    async def create_claim(
        self, data: Mapping[str, Any], *, actor_id: str | None = None, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        await self._require_enabled()
        replay = await self._idem_begin("POST /api/v1/warranty/claims", idempotency_key, data, actor_id)
        if replay:
            return replay["replay"]
        item = await self._item(data.get("item_id"))
        serial_id = data.get("serial_id")
        if bool(item.get("requires_numbering")) and not serial_id:
            raise SerialRequired("serialized items require serial_id (DM-EXT-01)")
        await self._validate_serial(item, serial_id)
        claim_type = str(data.get("claim_type") or "").upper()
        if claim_type not in {"REFUND", "REPAIR", "REPLACEMENT"}:
            raise ValidationFailed("claim_type must be REFUND/REPAIR/REPLACEMENT")
        registration_id = data.get("warranty_registration_id") or data.get("registration_id")
        if registration_id:
            registration = await self._require_row("warranty_registration", registration_id, "warranty registration")
            if registration["status"] == "VOID":
                raise RegistrationVoided("registration is voided")
        claim_id = uuid.uuid4()
        received_at = _stamp(data.get("received_at")) or utc_now()
        await self._exec(
            """
            INSERT INTO extensions.warranty_claim
                (id, tenant_id, claim_number, warranty_registration_id, item_id, serial_id, claim_type, status, description,
                 received_at, validation_result, vendor_id, external_return_auth_ref, created_by, updated_by)
            VALUES (:id, :tenant_id, :number, :registration, :item, :serial, :type, 'OPEN', :description, :received_at,
                    '{}'::jsonb, :vendor, :return_ref, :actor, :actor)
            """,
            id=claim_id,
            number=await self._next_number("WC"),
            registration=_as_uuid(registration_id) if registration_id else None,
            item=_as_uuid(item["id"]),
            serial=_as_uuid(serial_id) if serial_id else None,
            type=claim_type,
            description=data.get("description"),
            received_at=received_at,
            vendor=_as_uuid(data["vendor_id"]) if data.get("vendor_id") else None,
            return_ref=data.get("external_return_auth_ref"),
            actor=_as_uuid(actor_id) if actor_id else None,
        )
        await self._emit(
            "warranty.claim.opened",
            "warranty_claim",
            claim_id,
            {
                "claim_id": str(claim_id),
                "registration_id": str(registration_id) if registration_id else None,
                "claim_type": claim_type,
            },
        )
        await self._audit(
            "warranty.claim.opened", "extensions.warranty_claim", claim_id, {"claim_type": claim_type}, actor_id
        )
        await self.validate_claim(claim_id, actor_id=actor_id)
        result = await self.get_claim(claim_id)
        await self._idem_complete("POST /api/v1/warranty/claims", idempotency_key, result, claim_id, 201)
        return result

    async def _validate(
        self, claim: Mapping[str, Any], *, as_of: date | None = None, override_ref: str | None = None
    ) -> dict[str, Any]:
        moment = as_of or (_date(claim["received_at"]) or _today())
        reasons: list[str] = []
        registration = None
        if claim.get("warranty_registration_id"):
            registration = await self._one(
                "SELECT * FROM extensions.warranty_registration WHERE tenant_id = :tenant_id AND id = :id",
                id=_as_uuid(claim["warranty_registration_id"]),
            )
        allow_unregistered = bool(await self._setting("claims.allow_unregistered", False))
        if registration is None:
            if not allow_unregistered:
                reasons.append("NO_REGISTRATION")
        else:
            if registration["status"] != "ACTIVE":
                reasons.append("REGISTRATION_NOT_ACTIVE")
            start = _date(registration["warranty_start"])
            end = _date(registration["warranty_end"])
            if start is None or end is None or not (start <= moment <= end):
                reasons.append("NOT_IN_WARRANTY")
        result = {"pass": not reasons, "reasons": reasons, "as_of": moment.isoformat()}
        if override_ref:
            result["override"] = {"approval_ref": override_ref, "at": utc_now().isoformat()}
        return result

    async def validate_claim(
        self, claim_id: Any, *, as_of: str | None = None, override_ref: str | None = None, actor_id: str | None = None
    ) -> dict[str, Any]:
        claim = await self._require_row("warranty_claim", claim_id, "warranty claim")
        if claim["status"] not in {"OPEN", "UNDER_REVIEW"}:
            raise InvalidStateTransition(f"claim is {claim['status']}; validation applies to open claims")
        result = await self._validate(claim, as_of=_date(as_of), override_ref=override_ref)
        await self._exec(
            "UPDATE extensions.warranty_claim SET validation_result = CAST(:result AS jsonb), status = CASE WHEN status = 'OPEN' THEN 'UNDER_REVIEW' ELSE status END, updated_at = now(), version = version + 1 WHERE tenant_id = :tenant_id AND id = :id",
            result=json.dumps(_jsonable(result)),
            id=_as_uuid(claim_id),
        )
        await self._emit(
            "warranty.claim.validated",
            "warranty_claim",
            claim_id,
            {"claim_id": str(claim_id), "pass": result["pass"], "reasons": result["reasons"]},
        )
        return result

    async def approve_claim(
        self,
        claim_id: Any,
        *,
        approval_ref: str | None = None,
        override_reason: str | None = None,
        actor_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        claim = await self._require_row("warranty_claim", claim_id, "warranty claim")
        if claim["status"] not in {"OPEN", "UNDER_REVIEW"}:
            raise InvalidStateTransition(f"claim is {claim['status']}, expected OPEN/UNDER_REVIEW")
        validation = _jsonable(claim.get("validation_result") or {})
        override = bool(approval_ref or override_reason)
        if not validation.get("pass") and not override:
            raise ValidationRequired("claim validation failed; provide an approval_ref override (DM-EXT-02)")
        if override:
            validation["override"] = {
                "approval_ref": approval_ref,
                "by": str(actor_id),
                "reason": override_reason,
                "at": utc_now().isoformat(),
            }
        await self._exec(
            "UPDATE extensions.warranty_claim SET status = 'APPROVED', approved_by = :actor, approved_at = now(), validation_result = CAST(:validation AS jsonb), updated_at = now(), version = version + 1 WHERE tenant_id = :tenant_id AND id = :id",
            actor=_as_uuid(actor_id) if actor_id else None,
            validation=json.dumps(validation),
            id=_as_uuid(claim_id),
        )
        await self._emit(
            "warranty.claim.approved",
            "warranty_claim",
            claim_id,
            {"claim_id": str(claim_id), "approval_ref": approval_ref},
        )
        await self._audit(
            "warranty.claim.approved", "extensions.warranty_claim", claim_id, {"approval_ref": approval_ref}, actor_id
        )
        return {"id": str(claim_id), "status": "APPROVED", "validation_result": validation}

    async def reject_claim(
        self, claim_id: Any, reason: str, *, actor_id: str | None = None, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        claim = await self._require_row("warranty_claim", claim_id, "warranty claim")
        if claim["status"] not in {"OPEN", "UNDER_REVIEW"}:
            raise InvalidStateTransition(f"claim is {claim['status']}, expected OPEN/UNDER_REVIEW")
        await self._exec(
            "UPDATE extensions.warranty_claim SET status = 'REJECTED', rejected_reason = :reason, updated_at = now(), version = version + 1 WHERE tenant_id = :tenant_id AND id = :id",
            reason=reason,
            id=_as_uuid(claim_id),
        )
        await self._emit(
            "warranty.claim.rejected", "warranty_claim", claim_id, {"claim_id": str(claim_id), "reason": reason}
        )
        await self._audit(
            "warranty.claim.rejected", "extensions.warranty_claim", claim_id, {"reason": reason}, actor_id
        )
        return {"id": str(claim_id), "status": "REJECTED"}

    async def get_claim(self, claim_id: Any) -> dict[str, Any]:
        claim = await self._require_row("warranty_claim", claim_id, "warranty claim")
        claim["actions"] = await self._all(
            "SELECT * FROM extensions.warranty_claim_action WHERE tenant_id = :tenant_id AND warranty_claim_id = :id ORDER BY sequence",
            id=_as_uuid(claim_id),
        )
        claim["repair_orders"] = await self._all(
            "SELECT * FROM extensions.warranty_repair_order WHERE tenant_id = :tenant_id AND warranty_claim_id = :id ORDER BY created_at",
            id=_as_uuid(claim_id),
        )
        return claim

    async def list_claims(
        self, *, status: str | None = None, item_id: str | None = None, serial_id: str | None = None, limit: int = 100
    ) -> dict[str, Any]:
        clauses = ["tenant_id = :tenant_id"]
        params: dict[str, Any] = {"limit": max(1, min(int(limit), 500))}
        if status:
            clauses.append("status = :status")
            params["status"] = status.upper()
        if item_id:
            clauses.append("item_id = :item")
            params["item"] = _as_uuid(item_id)
        if serial_id:
            clauses.append("serial_id = :serial")
            params["serial"] = _as_uuid(serial_id)
        return {
            "items": await self._all(
                f"SELECT * FROM extensions.warranty_claim WHERE {' AND '.join(clauses)} ORDER BY received_at DESC LIMIT :limit",
                **params,
            ),
            "next_cursor": None,
        }

    async def add_claim_action(
        self, claim_id: Any, data: Mapping[str, Any], *, actor_id: str | None = None, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        claim = await self._require_row("warranty_claim", claim_id, "warranty claim")
        if claim["status"] != "APPROVED":
            raise ClaimActionNotAllowed("claim actions are allowed only on APPROVED claims")
        action_type = str(data.get("action_type") or "").upper()
        if action_type not in {"CREDIT_MEMO_REF", "REPAIR", "REPLACEMENT", "REJECT", "REFUND"}:
            raise ValidationFailed("invalid claim action_type")
        replacement_serial_id = data.get("replacement_serial_id")
        if action_type == "REPLACEMENT":
            if not replacement_serial_id:
                raise ClaimActionNotAllowed("REPLACEMENT requires replacement_serial_id")
            await self._validate_serial({"id": claim["item_id"]}, replacement_serial_id)
        sequence = int(
            await self._scalar(
                "SELECT coalesce(max(sequence), 0) + 1 FROM extensions.warranty_claim_action WHERE tenant_id = :tenant_id AND warranty_claim_id = :id",
                id=_as_uuid(claim_id),
            )
            or 1
        )
        action_id = uuid.uuid4()
        await self._exec(
            """
            INSERT INTO extensions.warranty_claim_action
                (id, tenant_id, warranty_claim_id, sequence, action_type, status, performed_by, notes, external_credit_ref,
                 replacement_serial_id)
            VALUES (:id, :tenant_id, :claim, :sequence, :type, :status, :actor, :notes, :credit_ref, :replacement_serial)
            """,
            id=action_id,
            claim=_as_uuid(claim_id),
            sequence=sequence,
            type=action_type,
            status=str(data.get("status") or "PENDING").upper(),
            actor=_as_uuid(actor_id) if actor_id else None,
            notes=data.get("notes"),
            credit_ref=data.get("external_credit_ref"),
            replacement_serial=_as_uuid(replacement_serial_id) if replacement_serial_id else None,
        )
        await self._emit(
            "warranty.claim.action.recorded",
            "warranty_claim",
            claim_id,
            {
                "claim_id": str(claim_id),
                "sequence": sequence,
                "action_type": action_type,
                "status": str(data.get("status") or "PENDING").upper(),
            },
        )
        await self._audit(
            "warranty.claim.action.recorded",
            "extensions.warranty_claim_action",
            action_id,
            {"action_type": action_type},
            actor_id,
        )
        return await self._require_row("warranty_claim_action", action_id, "claim action")

    async def complete_claim(
        self, claim_id: Any, notes: str | None = None, *, actor_id: str | None = None
    ) -> dict[str, Any]:
        claim = await self._require_row("warranty_claim", claim_id, "warranty claim")
        if claim["status"] != "APPROVED":
            raise InvalidStateTransition(f"claim is {claim['status']}, expected APPROVED")
        pending = int(
            await self._scalar(
                "SELECT count(*) FROM extensions.warranty_claim_action WHERE tenant_id = :tenant_id AND warranty_claim_id = :id AND status = 'PENDING'",
                id=_as_uuid(claim_id),
            )
            or 0
        )
        if pending:
            raise ClaimActionNotAllowed("all claim actions must be DONE/FAILED before completing the claim")
        await self._exec(
            "UPDATE extensions.warranty_claim SET status = 'COMPLETED', updated_at = now(), version = version + 1 WHERE tenant_id = :tenant_id AND id = :id",
            id=_as_uuid(claim_id),
        )
        await self._emit(
            "warranty.claim.completed",
            "warranty_claim",
            claim_id,
            {"claim_id": str(claim_id), "status": "COMPLETED", "notes": notes},
        )
        return {"id": str(claim_id), "status": "COMPLETED"}

    async def cancel_claim(self, claim_id: Any, reason: str, *, actor_id: str | None = None) -> dict[str, Any]:
        claim = await self._require_row("warranty_claim", claim_id, "warranty claim")
        if claim["status"] not in {"OPEN", "UNDER_REVIEW"}:
            raise InvalidStateTransition(f"claim is {claim['status']}; only unapproved claims can be cancelled")
        await self._exec(
            "UPDATE extensions.warranty_claim SET status = 'CANCELLED', updated_at = now(), version = version + 1 WHERE tenant_id = :tenant_id AND id = :id",
            id=_as_uuid(claim_id),
        )
        await self._emit(
            "warranty.claim.cancelled", "warranty_claim", claim_id, {"claim_id": str(claim_id), "status": "CANCELLED"}
        )
        return {"id": str(claim_id), "status": "CANCELLED"}

    # --------------------------------------------------------------------------- repair orders --
    async def create_repair_order(
        self, data: Mapping[str, Any], *, actor_id: str | None = None, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        await self._require_enabled()
        item = await self._item(data.get("item_id"))
        serial_id = data.get("serial_id")
        if bool(item.get("requires_numbering")) and not serial_id:
            raise SerialRequired("serialized items require serial_id")
        await self._validate_serial(item, serial_id)
        location_id = data.get("location_id")
        if location_id:
            row = await self._one(
                "SELECT id FROM traceability.location WHERE tenant_id = :tenant_id AND id = :id AND NOT is_inactive",
                id=_as_uuid(location_id),
            )
            if row is None:
                raise ValidationFailed("location not found/inactive")
        repair_id = uuid.uuid4()
        await self._exec(
            """
            INSERT INTO extensions.warranty_repair_order
                (id, tenant_id, repair_order_number, warranty_claim_id, item_id, serial_id, location_id, status, labor_cost,
                 parts_cost, notes, created_by, updated_by)
            VALUES (:id, :tenant_id, :number, :claim, :item, :serial, :location, 'OPEN', :labor, :parts, :notes, :actor, :actor)
            """,
            id=repair_id,
            number=await self._next_number("REP"),
            claim=_as_uuid(data["warranty_claim_id"]) if data.get("warranty_claim_id") else None,
            item=_as_uuid(item["id"]),
            serial=_as_uuid(serial_id) if serial_id else None,
            location=_as_uuid(location_id) if location_id else None,
            labor=Decimal(str(data.get("labor_cost") or 0)),
            parts=Decimal(str(data.get("parts_cost") or 0)),
            notes=data.get("notes"),
            actor=_as_uuid(actor_id) if actor_id else None,
        )
        await self._emit(
            "warranty.repair.created",
            "warranty_repair_order",
            repair_id,
            {
                "repair_order_id": str(repair_id),
                "claim_id": data.get("warranty_claim_id"),
                "item_id": str(item["id"]),
                "serial_id": str(serial_id) if serial_id else None,
                "location_id": location_id,
                "costs": {"labor": str(data.get("labor_cost") or 0), "parts": str(data.get("parts_cost") or 0)},
            },
        )
        await self._audit("warranty.repair.created", "extensions.warranty_repair_order", repair_id, {}, actor_id)
        return await self._require_row("warranty_repair_order", repair_id, "repair order")

    async def list_repair_orders(
        self, *, status: str | None = None, claim_id: str | None = None, item_id: str | None = None, limit: int = 100
    ) -> dict[str, Any]:
        clauses = ["tenant_id = :tenant_id"]
        params: dict[str, Any] = {"limit": max(1, min(int(limit), 500))}
        if status:
            clauses.append("status = :status")
            params["status"] = status.upper()
        if claim_id:
            clauses.append("warranty_claim_id = :claim")
            params["claim"] = _as_uuid(claim_id)
        if item_id:
            clauses.append("item_id = :item")
            params["item"] = _as_uuid(item_id)
        return {
            "items": await self._all(
                f"SELECT * FROM extensions.warranty_repair_order WHERE {' AND '.join(clauses)} ORDER BY created_at DESC LIMIT :limit",
                **params,
            ),
            "next_cursor": None,
        }

    async def update_repair_order(
        self, repair_id: Any, data: Mapping[str, Any], *, actor_id: str | None = None
    ) -> dict[str, Any]:
        repair = await self._require_row("warranty_repair_order", repair_id, "repair order")
        if repair["status"] in {"COMPLETED", "CANCELLED"}:
            raise InvalidStateTransition(f"repair order is {repair['status']}")
        values = {key: data[key] for key in ("status", "labor_cost", "parts_cost", "notes") if key in data}
        if "status" in values:
            target = str(values["status"]).upper()
            allowed = {"OPEN": {"IN_PROGRESS", "COMPLETED", "CANCELLED"}, "IN_PROGRESS": {"COMPLETED", "CANCELLED"}}
            if target not in allowed.get(repair["status"], set()):
                raise InvalidStateTransition(f"repair order cannot transition {repair['status']} -> {target}")
            values["status"] = target
            if target == "IN_PROGRESS" and not repair.get("started_at"):
                values["started_at"] = utc_now()
            if target == "COMPLETED":
                values["completed_at"] = utc_now()
        if values:
            await self._exec(
                "UPDATE extensions.warranty_repair_order SET "
                + ", ".join(f"{key} = :{key}" for key in values)
                + ", updated_at = now(), version = version + 1 WHERE tenant_id = :tenant_id AND id = :id",
                id=_as_uuid(repair_id),
                **values,
            )
        event = {"COMPLETED": "warranty.repair.completed", "CANCELLED": "warranty.repair.cancelled"}.get(
            str(values.get("status") or ""), "warranty.repair.updated"
        )
        await self._emit(
            event,
            "warranty_repair_order",
            repair_id,
            {
                "repair_order_id": str(repair_id),
                "status": values.get("status") or repair["status"],
                "claim_id": repair.get("warranty_claim_id"),
                "item_id": repair["item_id"],
                "serial_id": repair.get("serial_id"),
                "location_id": repair.get("location_id"),
                "costs": {
                    "labor": str(values.get("labor_cost", repair.get("labor_cost"))),
                    "parts": str(values.get("parts_cost", repair.get("parts_cost"))),
                },
            },
        )
        await self._audit("warranty.repair.updated", "extensions.warranty_repair_order", repair_id, values, actor_id)
        return await self._require_row("warranty_repair_order", repair_id, "repair order")

    # -------------------------------------------------------------------------- customer parts --
    async def create_customer_part(
        self, data: Mapping[str, Any], *, actor_id: str | None = None, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        await self._require_enabled()
        item = await self._item(data.get("item_id"))
        customer_ref = str(data.get("external_customer_ref") or "").strip()
        part_number = str(data.get("customer_part_number") or "").strip()
        if not customer_ref or not part_number:
            raise ValidationFailed("external_customer_ref and customer_part_number are required")
        row_id = uuid.uuid4()
        try:
            async with self.session.begin_nested():
                await self._exec(
                    "INSERT INTO extensions.customer_part (id, tenant_id, item_id, external_customer_ref, customer_part_number, description, is_active, created_by, updated_by) "
                    "VALUES (:id, :tenant_id, :item, :customer, :part, :description, true, :actor, :actor)",
                    id=row_id,
                    item=_as_uuid(item["id"]),
                    customer=customer_ref,
                    part=part_number,
                    description=data.get("description"),
                    actor=_as_uuid(actor_id) if actor_id else None,
                )
        except IntegrityError as exc:
            raise CustomerPartDuplicate("customer part number or item/customer mapping already exists") from exc
        await self._emit(
            "extensions.customer_part.created",
            "customer_part",
            row_id,
            {
                "mapping_id": str(row_id),
                "item_id": str(item["id"]),
                "customer_ref": customer_ref,
                "part_number": part_number,
            },
        )
        await self._audit(
            "extensions.customer_part.created",
            "extensions.customer_part",
            row_id,
            {"item_id": str(item["id"])},
            actor_id,
        )
        return await self._require_row("customer_part", row_id, "customer part")

    async def list_customer_parts(
        self,
        *,
        item_id: str | None = None,
        customer_ref: str | None = None,
        is_active: bool | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        clauses = ["tenant_id = :tenant_id"]
        params: dict[str, Any] = {"limit": max(1, min(int(limit), 500))}
        if item_id:
            clauses.append("item_id = :item")
            params["item"] = _as_uuid(item_id)
        if customer_ref:
            clauses.append("external_customer_ref = :customer")
            params["customer"] = customer_ref
        if is_active is not None:
            clauses.append("is_active = :active")
            params["active"] = bool(is_active)
        return {
            "items": await self._all(
                f"SELECT * FROM extensions.customer_part WHERE {' AND '.join(clauses)} ORDER BY customer_part_number LIMIT :limit",
                **params,
            ),
            "next_cursor": None,
        }

    async def get_customer_part(self, row_id: Any) -> dict[str, Any]:
        return await self._require_row("customer_part", row_id, "customer part")

    async def update_customer_part(
        self, row_id: Any, data: Mapping[str, Any], *, actor_id: str | None = None
    ) -> dict[str, Any]:
        row = await self._require_row("customer_part", row_id, "customer part")
        values = {key: data[key] for key in ("description", "is_active") if key in data}
        if values:
            await self._exec(
                "UPDATE extensions.customer_part SET "
                + ", ".join(f"{key} = :{key}" for key in values)
                + ", updated_at = now(), version = version + 1 WHERE tenant_id = :tenant_id AND id = :id",
                id=_as_uuid(row_id),
                **values,
            )
        event = (
            "extensions.customer_part.deactivated"
            if values.get("is_active") is False
            else "extensions.customer_part.updated"
        )
        await self._emit(
            event,
            "customer_part",
            row_id,
            {
                "mapping_id": str(row_id),
                "item_id": row["item_id"],
                "customer_ref": row["external_customer_ref"],
                "part_number": row["customer_part_number"],
            },
        )
        await self._audit(event, "extensions.customer_part", row_id, values, actor_id)
        return await self._require_row("customer_part", row_id, "customer part")

    async def import_customer_parts(
        self, rows: Sequence[Mapping[str, Any]], *, actor_id: str | None = None
    ) -> dict[str, Any]:
        limit = int(await self._setting("customer_part.import_limit", 5000))
        if len(rows) > limit:
            raise ValidationFailed(f"import exceeds the configured limit ({limit} rows)")
        results = []
        for index, row in enumerate(rows, start=1):
            try:
                created = await self.create_customer_part(row, actor_id=actor_id)
                results.append({"row": index, "status": "OK", "mapping_id": created["id"]})
            except ExtensionError as exc:
                results.append({"row": index, "status": "ERROR", "code": exc.code, "message": exc.message})
        return {
            "rows": results,
            "succeeded": sum(1 for row in results if row["status"] == "OK"),
            "failed": sum(1 for row in results if row["status"] == "ERROR"),
        }

    async def resolve_customer_part(self, *, external_customer_ref: str, part_number: str) -> dict[str, Any]:
        row = await self._one(
            "SELECT cp.*, i.item_number FROM extensions.customer_part cp JOIN item_master.item i ON i.tenant_id = cp.tenant_id AND i.id = cp.item_id "
            "WHERE cp.tenant_id = :tenant_id AND cp.external_customer_ref = :customer AND cp.customer_part_number = :part AND cp.is_active",
            customer=external_customer_ref,
            part=part_number,
        )
        if row is None:
            raise CustomerPartNotFound("customer part mapping not found")
        return {"item_id": row["item_id"], "item_number": row["item_number"], "mapping": row}

    async def list_item_customer_parts(self, item_id: Any) -> dict[str, Any]:
        await self._item(item_id)
        return {
            "items": await self._all(
                "SELECT * FROM extensions.customer_part WHERE tenant_id = :tenant_id AND item_id = :item ORDER BY external_customer_ref",
                item=_as_uuid(item_id),
            )
        }

    # ------------------------------------------------------------------------------------- UPC --
    async def _upc_eligible(self, item: Mapping[str, Any]) -> list[str]:
        reasons: list[str] = []
        if not item.get("is_active"):
            reasons.append("ITEM_INACTIVE")
        if str(item.get("item_type_code")).upper() not in UPC_ITEM_TYPES:
            reasons.append("ITEM_TYPE_NOT_ELIGIBLE")
        if not item.get("can_stock"):
            reasons.append("ITEM_NOT_STOCKABLE")
        if item.get("matrix_parent_id") is None:
            children = int(
                await self._scalar(
                    "SELECT count(*) FROM item_master.item WHERE tenant_id = :tenant_id AND matrix_parent_id = :item",
                    item=_as_uuid(item["id"]),
                )
                or 0
            )
            if children:
                reasons.append("MATRIX_PARENT_NOT_ELIGIBLE")
        return reasons

    async def _generate_upc(self, *, prefix: str) -> str:
        clean = "".join(ch for ch in str(prefix or "") if ch.isdigit())
        if len(clean) < 6 or len(clean) > 9:
            raise UpcChecksumInvalid("extensions.upc.company_prefix must be 6-9 digits (GS1 company prefix)")
        sequence = int(
            await self._scalar("SELECT count(*) + 1 FROM extensions.upc_registry WHERE tenant_id = :tenant_id") or 1
        )
        for attempt in range(0, 10000):
            data = clean + str(sequence + attempt).zfill(11 - len(clean))
            code = data + str(upc_check_digit(data))
            if not await self._scalar(
                "SELECT 1 FROM extensions.upc_registry WHERE tenant_id = :tenant_id AND upc_code = :code", code=code
            ):
                return code
        raise UpcAlreadyAssigned("no unused UPC code is available (never reuse retired codes)")

    async def assign_upc(
        self, data: Mapping[str, Any], *, actor_id: str | None = None, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        await self._require_enabled()
        item = await self._item(data.get("item_id"))
        reasons = await self._upc_eligible(item)
        if reasons:
            raise ItemNotUpcEligible("item is not UPC-eligible", details={"reasons": reasons})
        existing = await self._one(
            "SELECT * FROM extensions.upc_registry WHERE tenant_id = :tenant_id AND item_id = :item AND status = 'ASSIGNED'",
            item=_as_uuid(item["id"]),
        )
        if existing is not None:
            raise UpcAlreadyAssigned("item already has an ASSIGNED UPC")
        code = data.get("upc_code")
        if code:
            if not valid_upc_code(str(code)):
                raise UpcChecksumInvalid("UPC-A check digit is invalid")
            retired = await self._one(
                "SELECT 1 FROM extensions.upc_registry WHERE tenant_id = :tenant_id AND upc_code = :code AND status = 'RETIRED'",
                code=str(code),
            )
            if retired:
                raise UpcRetiredNeverReusable("a retired UPC code can never be reused (DM-EXT-04)")
            if await self._scalar(
                "SELECT 1 FROM extensions.upc_registry WHERE tenant_id = :tenant_id AND upc_code = :code",
                code=str(code),
            ):
                raise UpcAlreadyAssigned("UPC code is already assigned to another item")
        else:
            code = await self._generate_upc(prefix=str(await self._setting("upc.company_prefix", "")))
        row_id = uuid.uuid4()
        try:
            await self._exec(
                """
                INSERT INTO extensions.upc_registry
                    (id, tenant_id, item_id, upc_code, assignment_date, status, assignment_source, checksum_valid, created_by, updated_by)
                VALUES (:id, :tenant_id, :item, :code, CURRENT_DATE, 'ASSIGNED', :source, true, :actor, :actor)
                """,
                id=row_id,
                item=_as_uuid(item["id"]),
                code=code,
                source=str(data.get("assignment_source") or "MANUAL").upper(),
                actor=_as_uuid(actor_id) if actor_id else None,
            )
        except IntegrityError as exc:
            raise UpcAlreadyAssigned("UPC code or item assignment already exists") from exc
        from app.modules.item_master.facade import mirror_upc_code

        await mirror_upc_code(self.session, self.tenant_id, item["id"], code, source="FS14")
        await self._emit(
            "extensions.upc.assigned",
            "upc_registry",
            row_id,
            {
                "registry_id": str(row_id),
                "item_id": str(item["id"]),
                "upc_code": code,
                "assignment_source": str(data.get("assignment_source") or "MANUAL").upper(),
                "status": "ASSIGNED",
            },
        )
        await self._audit(
            "extensions.upc.assigned",
            "extensions.upc_registry",
            row_id,
            {"item_id": str(item["id"]), "upc_code": code},
            actor_id,
        )
        return await self._require_row("upc_registry", row_id, "UPC registry row")

    async def list_upc(
        self, *, status: str | None = None, item_id: str | None = None, limit: int = 100
    ) -> dict[str, Any]:
        clauses = ["tenant_id = :tenant_id"]
        params: dict[str, Any] = {"limit": max(1, min(int(limit), 500))}
        if status:
            clauses.append("status = :status")
            params["status"] = status.upper()
        if item_id:
            clauses.append("item_id = :item")
            params["item"] = _as_uuid(item_id)
        return {
            "items": await self._all(
                f"SELECT * FROM extensions.upc_registry WHERE {' AND '.join(clauses)} ORDER BY assignment_date DESC LIMIT :limit",
                **params,
            ),
            "next_cursor": None,
        }

    async def get_upc(self, row_id: Any) -> dict[str, Any]:
        row = await self._require_row("upc_registry", row_id, "UPC registry row")
        if row.get("item_id"):
            item = await self._one(
                "SELECT upc_code FROM item_master.item WHERE tenant_id = :tenant_id AND id = :id",
                id=_as_uuid(row["item_id"]),
            )
            row["item_mirror_upc_code"] = item["upc_code"] if item else None
        return row

    async def retire_upc(self, row_id: Any, data: Mapping[str, Any], *, actor_id: str | None = None) -> dict[str, Any]:
        row = await self._require_row("upc_registry", row_id, "UPC registry row")
        if row["status"] != "ASSIGNED":
            raise InvalidStateTransition(f"UPC registry row is {row['status']}, expected ASSIGNED")
        retirement_date = _date(data.get("retirement_date")) or _today()
        await self._exec(
            "UPDATE extensions.upc_registry SET status = 'RETIRED', retirement_date = :retirement_date, retirement_reason = :reason, updated_at = now(), version = version + 1 WHERE tenant_id = :tenant_id AND id = :id",
            retirement_date=retirement_date,
            reason=data.get("reason"),
            id=_as_uuid(row_id),
        )
        if row.get("item_id"):
            from app.modules.item_master.service import ItemMasterService

            await ItemMasterService(self.session, self.tenant_id).patch_item(row["item_id"], {"upc_code": None})
        await self._emit(
            "extensions.upc.retired",
            "upc_registry",
            row_id,
            {
                "registry_id": str(row_id),
                "item_id": row.get("item_id"),
                "upc_code": row["upc_code"],
                "status": "RETIRED",
            },
        )
        await self._audit(
            "extensions.upc.retired", "extensions.upc_registry", row_id, {"reason": data.get("reason")}, actor_id
        )
        return await self._require_row("upc_registry", row_id, "UPC registry row")

    async def validate_upc(self, upc_code: str) -> dict[str, Any]:
        checksum = valid_upc_code(str(upc_code))
        if not checksum:
            return {"upc_code": upc_code, "valid": False, "reasons": ["CHECKSUM_INVALID"]}
        row = await self._one(
            "SELECT * FROM extensions.upc_registry WHERE tenant_id = :tenant_id AND upc_code = :code",
            code=str(upc_code),
        )
        if row is None:
            return {"upc_code": upc_code, "valid": True, "reasons": [], "registered": False}
        return {
            "upc_code": upc_code,
            "valid": row["status"] == "ASSIGNED",
            "registered": True,
            "status": row["status"],
            "reasons": [] if row["status"] == "ASSIGNED" else ["RETIRED"],
        }

    async def monitoring(self) -> dict[str, Any]:
        assigned = int(
            await self._scalar(
                "SELECT count(*) FROM extensions.upc_registry WHERE tenant_id = :tenant_id AND status = 'ASSIGNED'"
            )
            or 0
        )
        retired = int(
            await self._scalar(
                "SELECT count(*) FROM extensions.upc_registry WHERE tenant_id = :tenant_id AND status = 'RETIRED'"
            )
            or 0
        )
        checksum_failures = sum(
            1
            for row in await self._all("SELECT upc_code FROM extensions.upc_registry WHERE tenant_id = :tenant_id")
            if not valid_upc_code(row["upc_code"])
        )
        eligible_missing = 0
        for item in await self._all(
            """
            SELECT i.id, i.is_active, i.matrix_parent_id, t.code AS item_type_code, t.can_stock
              FROM item_master.item i JOIN item_master.item_type_definition t ON t.tenant_id = i.tenant_id AND t.id = i.item_type_id
             WHERE i.tenant_id = :tenant_id AND i.is_active
            """
        ):
            if not await self._upc_eligible(item):
                if not await self._scalar(
                    "SELECT 1 FROM extensions.upc_registry WHERE tenant_id = :tenant_id AND item_id = :item AND status = 'ASSIGNED'",
                    item=_as_uuid(item["id"]),
                ):
                    eligible_missing += 1
        return {
            "assigned": assigned,
            "retired": retired,
            "eligible_items_missing_upc": eligible_missing,
            "checksum_failures": checksum_failures,
            "auto_assign_enabled": bool(await self._setting("upc.auto_assign", False)),
        }

    async def assign_batch(self, *, limit: int = 100, actor_id: str | None = None) -> dict[str, Any]:
        batch_limit = min(int(limit or 0) or int(await self._setting("upc.auto_assign_batch_limit", 1000)), 5000)
        items = await self._all(
            """
            SELECT i.id, i.is_active, i.matrix_parent_id, t.code AS item_type_code, t.can_stock
              FROM item_master.item i JOIN item_master.item_type_definition t ON t.tenant_id = i.tenant_id AND t.id = i.item_type_id
             WHERE i.tenant_id = :tenant_id AND i.is_active AND i.upc_code IS NULL
             ORDER BY i.created_at LIMIT :limit
            """,
            limit=batch_limit,
        )
        assigned, skipped, failed = 0, 0, 0
        for item in items:
            if await self._upc_eligible(item):
                skipped += 1
                continue
            if await self._scalar(
                "SELECT 1 FROM extensions.upc_registry WHERE tenant_id = :tenant_id AND item_id = :item AND status = 'ASSIGNED'",
                item=_as_uuid(item["id"]),
            ):
                skipped += 1
                continue
            try:
                await self.assign_upc({"item_id": str(item["id"]), "assignment_source": "AUTO"}, actor_id=actor_id)
                assigned += 1
            except ExtensionError:
                failed += 1
        await self._emit(
            "extensions.upc.batch.completed",
            "upc_registry",
            self.tenant_id,
            {
                "job_run_id": str(uuid.uuid4()),
                "assigned": assigned,
                "skipped": skipped,
                "failed": failed,
            },
        )
        return {"assigned": assigned, "skipped": skipped, "failed": failed}

    # -------------------------------------------------------------------------------- settings --
    SETTING_KEYS = (
        "upc.auto_assign",
        "upc.company_prefix",
        "upc.auto_assign_batch_limit",
        "warranty.default_duration_months",
        "warranty.expiry_scan_days",
        "claims.allow_unregistered",
        "claims.override_requires_approval",
        "customer_part.import_limit",
        "warranty.registration_import_limit",
    )

    async def get_settings(self) -> dict[str, Any]:
        items = []
        for key in self.SETTING_KEYS:
            row = await self._one(
                "SELECT value, value_type FROM platform.tenant_setting WHERE tenant_id = :tenant_id AND namespace = 'inventory' AND key = :setting_key",
                setting_key=f"extensions.{key}",
            )
            value = row["value"] if row else None
            if isinstance(value, str) and value.startswith('"') and value.endswith('"'):
                value = value[1:-1]
            items.append({"key": key, "value": value})
        items.append({"key": "enabled", "value": bool(await self._flag("extensions.registry.enabled", False))})
        return {"items": items}

    async def patch_settings(self, data: Mapping[str, Any], *, actor_id: str | None = None) -> dict[str, Any]:
        allowed = set(self.SETTING_KEYS)
        if (
            "upc.company_prefix" in data
            and str(data["upc.company_prefix"] or "")
            and not (6 <= len(str(data["upc.company_prefix"])) <= 9 and str(data["upc.company_prefix"]).isdigit())
        ):
            raise ValidationFailed("upc.company_prefix must be 6-9 digits")
        for key, value in data.items():
            if key not in allowed:
                raise ValidationFailed(f"unknown extension setting {key!r}")
            value_type = (
                "boolean" if isinstance(value, bool) else ("number" if isinstance(value, int | float) else "string")
            )
            raw = (
                json.dumps(value)
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
                key=f"extensions.{key}",
                value=raw,
                value_type=value_type,
            )
        await self._audit("extensions.settings.updated", "extensions.settings", self.tenant_id, dict(data), actor_id)
        return await self.get_settings()
