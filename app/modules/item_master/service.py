"""FS-01 item-master domain service (async SQLAlchemy Core, tenant-scoped, no cross-module writes).

All queries are tenant-scoped via explicit ``tenant_id`` binds (RLS is the defence-in-depth layer).
Migrations 0004/0005 own the schema; this module owns item creation/extension/UoM/matrix/kit/barcode/
planning business rules and emits ``item.*`` outbox events in the caller's transaction.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.outbox import emit
from app.modules.item_master.errors import (
    BarcodeInvalid,
    ItemHasActiveChildren,
    ItemInactive,
    ItemLocationNotFound,
    ItemMasterError,
    ItemNotFound,
    ItemNumberExists,
    ItemTypeFlagMismatch,
    ItemTypeNotFound,
    KitMemberInvalid,
    MatrixChildExists,
    MatrixOptionInvalid,
    MatrixTooManyCombinations,
    PlanningAutoCalcDisabled,
    PlanningValuesInvalid,
    UomAssignmentInvalid,
    item_error,
)
from app.modules.item_master.taxonomy import ITEM_TYPE_SEEDS, PostingOperation, posting_eligibility
from app.modules.item_master.uom import UomError, UomFactor, convert_quantity, quantize_quantity

SYSTEM_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000000000")
MAX_MATRIX_COMBINATIONS = 2000
_CATEGORY_MAX_DEPTH = 10
_UPC_RE = re.compile(r"^\d{12}$")
_EAN_RE = re.compile(r"^\d{13}$")
_GS1_RE = re.compile(r"^\(?\d{2}\)?\d{6,}$")
_CODE128_RE = re.compile(r"^[\x20-\x7e]{1,80}$")


def utc_now() -> datetime:
    return datetime.now(UTC)


def _jsonable(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        text_value = format(value, "f")
        if "." in text_value:
            text_value = text_value.rstrip("0").rstrip(".")
        return text_value or "0"
    if isinstance(value, list | tuple | set):
        return [_jsonable(item) for item in value]
    return value


def _row(row: Mapping[str, Any]) -> dict[str, Any]:
    return _jsonable(dict(row))


def _upc_checksum_ok(barcode: str) -> bool:
    digits = [int(ch) for ch in barcode]
    checksum = digits[-1]
    total = sum(digit * (3 if index % 2 == 0 else 1) for index, digit in enumerate(digits[:-1]))
    return (10 - total % 10) % 10 == checksum


def _ean_checksum_ok(barcode: str) -> bool:
    digits = [int(ch) for ch in barcode]
    total = sum(digit * (1 if index % 2 == 0 else 3) for index, digit in enumerate(digits[:-1]))
    return (10 - total % 10) % 10 == digits[-1]


class ItemMasterService:
    def __init__(self, session: AsyncSession, tenant_id: uuid.UUID | str) -> None:
        self.session = session
        self.tenant_id = tenant_id if isinstance(tenant_id, uuid.UUID) else uuid.UUID(str(tenant_id))

    # -- low-level helpers --------------------------------------------------------------------
    async def _exec(self, sql: str, **params: Any):
        return await self.session.execute(text(sql), {"tenant_id": self.tenant_id, **params})

    async def _one(self, sql: str, **params: Any) -> dict[str, Any] | None:
        row = (await self._exec(sql, **params)).mappings().first()
        return dict(row) if row else None

    async def _all(self, sql: str, **params: Any) -> list[dict[str, Any]]:
        return [dict(row) for row in (await self._exec(sql, **params)).mappings().all()]

    async def _scalar(self, sql: str, **params: Any) -> Any:
        return (await self._exec(sql, **params)).scalar_one_or_none()

    async def _emit(
        self,
        event_type: str,
        *,
        item_id: uuid.UUID | str,
        payload: Mapping[str, Any],
        aggregate_version: int = 1,
        correlation_id: str | None = None,
    ) -> None:
        await emit(
            self.session,
            event_type=event_type,
            tenant_id=str(self.tenant_id),
            aggregate_type="item",
            aggregate_id=str(item_id),
            payload=_jsonable(dict(payload)),
            aggregate_version=aggregate_version,
            correlation_id=correlation_id,
        )

    # -- reference catalogue (lazy per-tenant provisioning) -----------------------------------
    async def ensure_reference(self) -> None:
        """Copy the SYSTEM-tenant taxonomy/UoM/category seed catalogue for a tenant (idempotent)."""
        await self._exec(
            """
            INSERT INTO item_master.item_type_definition
                (id, tenant_id, code, name, description, can_stock, requires_bom, requires_numbering,
                 is_charge_only, sort_order, is_system, created_at, updated_at, version)
            SELECT gen_random_uuid(), :tenant_id, s.code, s.name, s.description, s.can_stock, s.requires_bom,
                   s.requires_numbering, s.is_charge_only, s.sort_order, true, now(), now(), 1
            FROM item_master.item_type_definition s
            WHERE s.tenant_id = :system AND NOT EXISTS (
                SELECT 1 FROM item_master.item_type_definition t
                WHERE t.tenant_id = :tenant_id AND t.code = s.code)
            ON CONFLICT (tenant_id, code) DO NOTHING
            """,
            system=SYSTEM_TENANT_ID,
        )
        await self._exec(
            """
            INSERT INTO item_master.uom_type
                (id, tenant_id, name, code, base_uom_id, is_active, created_at, updated_at, version)
            SELECT gen_random_uuid(), :tenant_id, s.name, s.code, NULL, true, now(), now(), 1
            FROM item_master.uom_type s
            WHERE s.tenant_id = :system AND NOT EXISTS (
                SELECT 1 FROM item_master.uom_type t WHERE t.tenant_id = :tenant_id AND t.code = s.code)
            ON CONFLICT (tenant_id, code) DO NOTHING
            """,
            system=SYSTEM_TENANT_ID,
        )
        await self._exec(
            """
            INSERT INTO item_master.uom
                (id, tenant_id, uom_type_id, code, name, conversion_factor, is_base, rounding_precision,
                 is_active, created_at, updated_at, version)
            SELECT gen_random_uuid(), :tenant_id, tt.id, s.code, s.name, s.conversion_factor, s.is_base,
                   s.rounding_precision, true, now(), now(), 1
            FROM item_master.uom s
            JOIN item_master.uom_type st ON st.tenant_id = s.tenant_id AND st.id = s.uom_type_id
            JOIN item_master.uom_type tt ON tt.tenant_id = :tenant_id AND tt.code = st.code
            WHERE s.tenant_id = :system AND NOT EXISTS (
                SELECT 1 FROM item_master.uom t WHERE t.tenant_id = :tenant_id
                  AND t.uom_type_id = tt.id AND t.code = s.code)
            ON CONFLICT (tenant_id, uom_type_id, code) DO NOTHING
            """,
            system=SYSTEM_TENANT_ID,
        )
        await self._exec(
            """
            UPDATE item_master.uom_type tt SET base_uom_id = tu.id
            FROM item_master.uom tu
            WHERE tt.tenant_id = :tenant_id AND tu.tenant_id = :tenant_id
              AND tu.uom_type_id = tt.id AND tu.is_base AND tt.base_uom_id IS NULL
            """
        )
        await self._exec(
            """
            INSERT INTO item_master.item_category
                (id, tenant_id, name, parent_id, code, is_active, sort_order, created_at, updated_at, version)
            SELECT gen_random_uuid(), :tenant_id, s.name, NULL, s.code, true, s.sort_order, now(), now(), 1
            FROM item_master.item_category s
            WHERE s.tenant_id = :system AND s.parent_id IS NULL AND NOT EXISTS (
                SELECT 1 FROM item_master.item_category t WHERE t.tenant_id = :tenant_id AND t.code = s.code)
            ON CONFLICT (tenant_id, code) DO NOTHING
            """,
            system=SYSTEM_TENANT_ID,
        )

    # -- taxonomy ------------------------------------------------------------------------------
    async def list_item_types(self, *, can_stock: bool | None = None, charge_only: bool | None = None) -> list[dict[str, Any]]:
        await self.ensure_reference()
        rows = await self._all(
            "SELECT code, name, can_stock, requires_bom, requires_numbering, is_charge_only, sort_order "
            "FROM item_master.item_type_definition WHERE tenant_id = :tenant_id ORDER BY sort_order, code"
        )
        items = []
        for row in rows:
            if can_stock is not None and bool(row["can_stock"]) != can_stock:
                continue
            if charge_only is not None and bool(row["is_charge_only"]) != charge_only:
                continue
            items.append({**_row(row), "posting_eligibility": _eligibility_map(str(row["code"]))})
        return items

    async def get_item_type(self, code: str) -> dict[str, Any]:
        await self.ensure_reference()
        row = await self._one(
            "SELECT code, name, can_stock, requires_bom, requires_numbering, is_charge_only, sort_order "
            "FROM item_master.item_type_definition WHERE tenant_id = :tenant_id AND code = :code",
            code=str(code).upper(),
        )
        if row is None:
            raise ItemTypeNotFound(f"item type {code!r} not found")
        return {**_row(row), "posting_eligibility": _eligibility_map(str(row["code"]))}

    async def _item_type(self, code: str) -> dict[str, Any]:
        await self.ensure_reference()
        row = await self._one(
            "SELECT id, code, can_stock, requires_bom, requires_numbering, is_charge_only "
            "FROM item_master.item_type_definition WHERE tenant_id = :tenant_id AND code = :code",
            code=str(code).upper(),
        )
        if row is None:
            raise ItemTypeNotFound(f"item type {code!r} not found")
        return row

    # -- UoM types / units / conversions ------------------------------------------------------
    async def list_uom_types(self) -> list[dict[str, Any]]:
        await self.ensure_reference()
        types = await self._all(
            "SELECT id, code, name, base_uom_id, is_active FROM item_master.uom_type "
            "WHERE tenant_id = :tenant_id ORDER BY code"
        )
        for entry in types:
            entry["units"] = [
                _row(row)
                for row in await self._all(
                    "SELECT id, code, name, conversion_factor, is_base, rounding_precision, is_active "
                    "FROM item_master.uom WHERE tenant_id = :tenant_id AND uom_type_id = :type_id ORDER BY code",
                    type_id=entry["id"],
                )
            ]
        return [_jsonable(entry) for entry in types]

    async def create_uom_type(self, data: Mapping[str, Any]) -> dict[str, Any]:
        await self.ensure_reference()
        code = str(data.get("code") or "").strip().upper()
        name = str(data.get("name") or code)
        if not code:
            raise UomAssignmentInvalid("uom type code is required")
        type_id = uuid.uuid4()
        try:
            await self._exec(
                "INSERT INTO item_master.uom_type (id, tenant_id, name, code, base_uom_id, is_active, created_at, updated_at, version) "
                "VALUES (:id, :tenant_id, :name, :code, NULL, true, now(), now(), 1)",
                id=type_id, name=name, code=code,
            )
        except IntegrityError as exc:
            raise UomAssignmentInvalid(f"uom type {code!r} already exists") from exc
        base = data.get("base_uom") if isinstance(data.get("base_uom"), Mapping) else {}
        units = list(data.get("units") or [])
        if base:
            units.insert(0, {**dict(base), "is_base": True})
        elif units:
            units[0] = {**units[0], "is_base": True}
        for unit in units:
            await self.create_uom({**unit, "uom_type_id": str(type_id)})
        type_row = await self._one("SELECT * FROM item_master.uom_type WHERE tenant_id = :tenant_id AND id = :id", id=type_id)
        return _row(type_row) if type_row else {"id": str(type_id)}

    async def list_uoms(self, *, uom_type_id: str | None = None, is_active: bool | None = None) -> list[dict[str, Any]]:
        await self.ensure_reference()
        rows = await self._all(
            "SELECT id, uom_type_id, code, name, conversion_factor, is_base, rounding_precision, is_active "
            "FROM item_master.uom WHERE tenant_id = :tenant_id "
            + ("AND uom_type_id = :type_id " if uom_type_id else "")
            + ("AND is_active = :active " if is_active is not None else "")
            + "ORDER BY code",
            **({"type_id": uuid.UUID(str(uom_type_id))} if uom_type_id else {}),
            **({"active": is_active} if is_active is not None else {}),
        )
        return [_jsonable(row) for row in rows]

    async def create_uom(self, data: Mapping[str, Any]) -> dict[str, Any]:
        await self.ensure_reference()
        type_id = data.get("uom_type_id")
        code = str(data.get("code") or "").strip().upper()
        if not type_id or not code:
            raise UomAssignmentInvalid("uom_type_id and code are required")
        factor = Decimal(str(data.get("conversion_factor", "1")))
        if factor <= 0:
            raise UomAssignmentInvalid("conversion_factor must be > 0")
        is_base = bool(data.get("is_base", False))
        uom_id = uuid.uuid4()
        try:
            await self._exec(
                "INSERT INTO item_master.uom (id, tenant_id, uom_type_id, code, name, conversion_factor, is_base, rounding_precision, is_active, created_at, updated_at, version) "
                "VALUES (:id, :tenant_id, :type_id, :code, :name, :factor, :is_base, :precision, true, now(), now(), 1)",
                id=uom_id, type_id=uuid.UUID(str(type_id)), code=code,
                name=str(data.get("name") or code), factor=factor, is_base=is_base,
                precision=int(data.get("rounding_precision", 4)),
            )
        except IntegrityError as exc:
            raise UomAssignmentInvalid(f"uom {code!r} already exists in this units type") from exc
        if is_base:
            await self._exec("UPDATE item_master.uom_type SET base_uom_id = :uom WHERE tenant_id = :tenant_id AND id = :type", uom=uom_id, type=uuid.UUID(str(type_id)))
        row = await self._one("SELECT * FROM item_master.uom WHERE tenant_id = :tenant_id AND id = :id", id=uom_id)
        return _row(row) if row else {"id": str(uom_id)}

    async def update_uom_type(self, type_id: str, data: Mapping[str, Any]) -> dict[str, Any]:
        values = {key: data[key] for key in ("name", "is_active") if key in data}
        if values:
            sets = ", ".join(f"{key} = :{key}" for key in values)
            await self._exec(
                f"UPDATE item_master.uom_type SET {sets}, updated_at = now(), version = version + 1 WHERE tenant_id = :tenant_id AND id = :id",
                id=uuid.UUID(str(type_id)), **values,
            )
        row = await self._one("SELECT * FROM item_master.uom_type WHERE tenant_id = :tenant_id AND id = :id", id=uuid.UUID(str(type_id)))
        if row is None:
            raise UomAssignmentInvalid("uom type not found")
        return _row(row)

    async def update_uom(self, uom_id: str, data: Mapping[str, Any]) -> dict[str, Any]:
        values = {key: data[key] for key in ("name", "conversion_factor", "rounding_precision", "is_active") if key in data}
        if "conversion_factor" in values and Decimal(str(values["conversion_factor"])) <= 0:
            raise UomAssignmentInvalid("conversion_factor must be > 0")
        if values:
            sets = ", ".join(f"{key} = :{key}" for key in values)
            await self._exec(
                f"UPDATE item_master.uom SET {sets}, updated_at = now(), version = version + 1 WHERE tenant_id = :tenant_id AND id = :id",
                id=uuid.UUID(str(uom_id)), **values,
            )
        row = await self._one("SELECT * FROM item_master.uom WHERE tenant_id = :tenant_id AND id = :id", id=uuid.UUID(str(uom_id)))
        if row is None:
            raise UomAssignmentInvalid("uom not found")
        return _row(row)

    async def list_conversions(self, *, uom_type_id: str | None = None) -> list[dict[str, Any]]:
        rows = await self._all(
            "SELECT c.id, c.uom_type_id, c.from_uom_id, c.to_uom_id, c.factor, c.notes "
            "FROM item_master.uom_conversion c WHERE c.tenant_id = :tenant_id "
            + ("AND c.uom_type_id = :type_id " if uom_type_id else "")
            + "ORDER BY c.from_uom_id, c.to_uom_id",
            **({"type_id": uuid.UUID(str(uom_type_id))} if uom_type_id else {}),
        )
        return [_jsonable(row) for row in rows]

    async def create_conversion(self, data: Mapping[str, Any]) -> dict[str, Any]:
        await self.ensure_reference()
        uom_type_id = uuid.UUID(str(data["uom_type_id"]))
        from_id = uuid.UUID(str(data["from_uom_id"]))
        to_id = uuid.UUID(str(data["to_uom_id"]))
        factor = Decimal(str(data["factor"]))
        if factor <= 0 or from_id == to_id:
            raise UomAssignmentInvalid("conversion factor must be > 0 and from != to")
        membership = await self._all(
            "SELECT id FROM item_master.uom WHERE tenant_id = :tenant_id AND uom_type_id = :type_id AND id = ANY(:ids)",
            type_id=uom_type_id, ids=[from_id, to_id],
        )
        if len(membership) != 2:
            raise UomAssignmentInvalid("both UoMs must belong to the units type")
        conversion_id = uuid.uuid4()
        try:
            await self._exec(
                "INSERT INTO item_master.uom_conversion (id, tenant_id, uom_type_id, from_uom_id, to_uom_id, factor, notes, created_at, updated_at, version) "
                "VALUES (:id, :tenant_id, :type_id, :from_id, :to_id, :factor, :notes, now(), now(), 1)",
                id=conversion_id, type_id=uom_type_id, from_id=from_id, to_id=to_id,
                factor=factor, notes=data.get("notes"),
            )
        except IntegrityError as exc:
            raise UomAssignmentInvalid("conversion already exists") from exc
        return {"id": str(conversion_id), "uom_type_id": str(uom_type_id), "from_uom_id": str(from_id), "to_uom_id": str(to_id), "factor": format(factor, "f")}

    async def delete_conversion(self, conversion_id: str) -> None:
        await self._exec("DELETE FROM item_master.uom_conversion WHERE tenant_id = :tenant_id AND id = :id", id=uuid.UUID(str(conversion_id)))

    async def resolve_conversion(self, data: Mapping[str, Any]) -> dict[str, Any]:
        quantity = data.get("quantity")
        from_id = uuid.UUID(str(data.get("from_uom_id")))
        to_id = uuid.UUID(str(data.get("to_uom_id"))) if data.get("to_uom_id") else None
        uom_type_id = data.get("uom_type_id")
        override = Decimal(str(data["conversion_override"])) if data.get("conversion_override") else None
        from_uom = await self._uom_factor(from_id)
        to_uom = await self._uom_factor(to_id) if to_id else None
        if uom_type_id and from_uom["uom_type_id"] != str(uom_type_id):
            raise UomAssignmentInvalid("UOM_TYPE_MISMATCH")
        type_factor = None
        if to_uom is None:
            conv = await self._one(
                "SELECT factor FROM item_master.uom_conversion WHERE tenant_id = :tenant_id AND uom_type_id = :type_id AND from_uom_id = :from_id AND to_uom_id IS NULL",
                type_id=from_uom["uom_type_id"], from_id=from_id,
            )
            type_factor = Decimal(str(conv["factor"])) if conv else None
        try:
            from_factor = UomFactor(
                uom_id=from_uom["id"], uom_type_id=from_uom["uom_type_id"], code=from_uom["code"],
                factor=from_uom["factor"], is_base=bool(from_uom.get("is_base")),
            )
            to_factor = None
            if to_uom is not None:
                to_factor = UomFactor(
                    uom_id=to_uom["id"], uom_type_id=to_uom["uom_type_id"], code=to_uom["code"],
                    factor=to_uom["factor"], is_base=bool(to_uom.get("is_base")),
                )
            result = convert_quantity(
                quantity,
                from_uom=from_factor,
                to_uom=to_factor,
                type_conversion_factor=type_factor,
                item_override_factor=override,
                whole_base_required=bool(data.get("whole_base_required", False)),
            )
        except UomError as exc:
            raise ItemMasterError(str(exc), code=exc.code) from exc
        return {
            "quantity": format(result.quantity, "f"),
            "quantity_base": format(result.quantity_base, "f"),
            "quantity_to": format(result.quantity_to, "f") if result.quantity_to is not None else None,
            "uom_code": from_uom["code"],
            "to_uom_code": to_uom["code"] if to_uom else None,
            "rounded_to": 12,
            "factor": format(result.factor, "f"),
            "whole_base_required": result.whole_base_required,
        }

    async def _uom_factor(self, uom_id: uuid.UUID) -> dict[str, Any]:
        row = await self._one(
            "SELECT u.id, u.uom_type_id, u.code, u.conversion_factor, u.is_base, t.code AS type_code "
            "FROM item_master.uom u JOIN item_master.uom_type t ON t.tenant_id = u.tenant_id AND t.id = u.uom_type_id "
            "WHERE u.tenant_id = :tenant_id AND u.id = :id",
            id=uom_id,
        )
        if row is None:
            from app.modules.item_master.uom import UomNotFound
            raise UomNotFound("uom not found")
        factor = {"id": str(row["id"]), "uom_type_id": str(row["uom_type_id"]), "code": str(row["code"]), "factor": Decimal(str(row["conversion_factor"])), "is_base": bool(row["is_base"]), "type_code": str(row["type_code"])}
        return factor

    # -- categories --------------------------------------------------------------------------
    async def list_categories(self, *, parent_id: str | None = None, include_inactive: bool = False) -> list[dict[str, Any]]:
        rows = await self._all(
            "SELECT id, parent_id, code, name, is_active, sort_order FROM item_master.item_category WHERE tenant_id = :tenant_id "
            + ("AND parent_id = :parent " if parent_id else "")
            + (" " if include_inactive else "AND is_active = true ")
            + "ORDER BY sort_order, code",
            **({"parent": uuid.UUID(str(parent_id))} if parent_id else {}),
        )
        return [_jsonable(row) for row in rows]

    async def create_category(self, data: Mapping[str, Any]) -> dict[str, Any]:
        code = str(data.get("code") or "").strip().upper()
        name = str(data.get("name") or code)
        if not code:
            raise ItemMasterError("category code is required", code="CATEGORY_INVALID")
        category_id = uuid.uuid4()
        parent = data.get("parent_id")
        await self._check_category_depth(uuid.UUID(str(parent)) if parent else None)
        try:
            await self._exec(
                "INSERT INTO item_master.item_category (id, tenant_id, parent_id, code, name, is_active, sort_order, created_at, updated_at, version) "
                "VALUES (:id, :tenant_id, :parent, :code, :name, true, :sort_order, now(), now(), 1)",
                id=category_id, parent=uuid.UUID(str(parent)) if parent else None, code=code, name=name,
                sort_order=int(data.get("sort_order", 100)),
            )
        except IntegrityError as exc:
            raise ItemMasterError("category code or name already exists", code="CATEGORY_EXISTS") from exc
        return {"id": str(category_id), "code": code, "name": name}

    async def update_category(self, category_id: str, data: Mapping[str, Any]) -> dict[str, Any]:
        values = {key: data[key] for key in ("name", "sort_order", "is_active") if key in data}
        if values:
            sets = ", ".join(f"{key} = :{key}" for key in values)
            await self._exec(
                f"UPDATE item_master.item_category SET {sets}, updated_at = now(), version = version + 1 WHERE tenant_id = :tenant_id AND id = :id",
                id=uuid.UUID(str(category_id)), **values,
            )
        row = await self._one("SELECT * FROM item_master.item_category WHERE tenant_id = :tenant_id AND id = :id", id=uuid.UUID(str(category_id)))
        if row is None:
            raise item_error("category not found", code="CATEGORY_NOT_FOUND", http_status=404)
        return _row(row)

    async def move_category(self, category_id: str, new_parent_id: str | None) -> dict[str, Any]:
        category_uuid = uuid.UUID(str(category_id))
        parent_uuid = uuid.UUID(str(new_parent_id)) if new_parent_id else None
        if parent_uuid == category_uuid:
            raise ItemMasterError("category cannot be its own parent", code="CATEGORY_CYCLE")
        if parent_uuid is not None:
            # descendants must not include the new parent (cycle check via recursive CTE)
            descendant = await self._scalar(
                """
                WITH RECURSIVE tree AS (
                    SELECT id FROM item_master.item_category WHERE tenant_id = :tenant_id AND id = :category
                    UNION ALL
                    SELECT c.id FROM item_master.item_category c JOIN tree ON c.parent_id = tree.id
                    WHERE c.tenant_id = :tenant_id
                ) SELECT count(*) FROM tree WHERE id = :parent
                """,
                category=str(category_uuid), parent=str(parent_uuid),
            )
            if descendant:
                raise ItemMasterError("category cycle rejected", code="CATEGORY_CYCLE")
            await self._check_category_depth(parent_uuid)
        await self._exec(
            "UPDATE item_master.item_category SET parent_id = :parent, updated_at = now(), version = version + 1 WHERE tenant_id = :tenant_id AND id = :id",
            id=category_uuid, parent=parent_uuid,
        )
        return {"id": str(category_uuid), "parent_id": str(parent_uuid) if parent_uuid else None}

    async def _check_category_depth(self, parent_id: uuid.UUID | None) -> None:
        depth = 0
        current = parent_id
        while current is not None:
            depth += 1
            if depth >= _CATEGORY_MAX_DEPTH:
                raise ItemMasterError("category depth exceeds 10", code="CATEGORY_DEPTH")
            current = await self._scalar("SELECT parent_id FROM item_master.item_category WHERE tenant_id = :tenant_id AND id = :id", id=current)

    async def get_category(self, category_id: str) -> dict[str, Any]:
        row = await self._one("SELECT * FROM item_master.item_category WHERE tenant_id = :tenant_id AND id = :id", id=uuid.UUID(str(category_id)))
        if row is None:
            raise item_error("category not found", code="CATEGORY_NOT_FOUND", http_status=404)
        return _row(row)

    # -- items --------------------------------------------------------------------------------
    async def create_item(self, data: Mapping[str, Any]) -> dict[str, Any]:
        await self.ensure_reference()
        type_row = await self._item_type(str(data.get("item_type_code") or "INVENTORY"))
        item_number = str(data.get("item_number") or "").strip()
        display_name = str(data.get("display_name") or item_number)
        if not item_number:
            raise ItemMasterError("item_number is required", code="ITEM_NUMBER_REQUIRED")
        stockable = bool(type_row["can_stock"])
        charge_only = bool(type_row["is_charge_only"])
        units_type_id = data.get("units_type_id") or await self._default_uom_type_id()
        base_uom_id = data.get("base_uom_id") or await self._default_base_uom_id(units_type_id)
        if stockable and (not units_type_id or not base_uom_id):
            raise UomAssignmentInvalid("stockable items require units_type_id and base_uom_id")
        if base_uom_id and units_type_id:
            await self._require_uom_membership(str(units_type_id), str(base_uom_id))
        item_id = uuid.uuid4()
        created_from = str(data.get("created_from") or "MANUAL").upper()
        category_id = data.get("item_category_id")
        try:
            await self._exec(
                """
                INSERT INTO item_master.item
                    (id, tenant_id, item_number, display_name, description, item_type_id, is_active, matrix_parent_id,
                     base_uom_id, units_type_id, item_category_id, upc_code, external_ref, created_from,
                     created_at, updated_at, created_by, updated_by, version)
                VALUES (:id, :tenant_id, :item_number, :display_name, :description, :item_type_id, true, :matrix_parent,
                        :base_uom, :units_type, :category, :upc_code, :external_ref, :created_from, now(), now(),
                        :actor, :actor, 1)
                """,
                id=item_id, item_number=item_number, display_name=display_name, description=data.get("description"),
                item_type_id=type_row["id"], matrix_parent=data.get("matrix_parent_id"),
                base_uom=base_uom_id, units_type=units_type_id, category=category_id,
                upc_code=data.get("upc_code"), external_ref=data.get("external_ref"), created_from=created_from,
                actor=data.get("actor_id"),
            )
        except IntegrityError as exc:
            raise ItemNumberExists(f"item number {item_number!r} already exists") from exc
        if category_id:
            await self._exec(
                "INSERT INTO item_master.item_category_member (tenant_id, item_id, category_id, is_primary, created_at, updated_at, version) "
                "VALUES (:tenant_id, :item, :category, true, now(), now(), 1) ON CONFLICT DO NOTHING",
                item=item_id, category=uuid.UUID(str(category_id)),
            )
        inventory_data = data.get("inventory")
        inventory: Mapping[str, Any] = inventory_data if isinstance(inventory_data, Mapping) else {}
        if stockable:
            await self._upsert_inventory(item_id, inventory, base_uom_id=base_uom_id, is_new=True)
            await self._exec(
                "INSERT INTO item_master.item_uom (tenant_id, item_id, uom_id, role, conversion_override, is_default_for_role, created_at, updated_at, version) "
                "VALUES (:tenant_id, :item, :uom, 'STOCK', NULL, true, now(), now(), 1) ON CONFLICT DO NOTHING",
                item=item_id, uom=base_uom_id,
            )
        elif charge_only:
            inventory = {}
        for extension in ("purchasing", "sales", "accounting"):
            payload = data.get(extension)
            if isinstance(payload, Mapping) and payload:
                await self.upsert_extension(item_id, extension, payload)
        uoms = data.get("uoms")
        if isinstance(uoms, list) and uoms:
            await self.replace_item_uoms(item_id, uoms, allow_stock=stockable, stock_base_uom_id=base_uom_id)
        version = int(await self._scalar("SELECT version FROM item_master.item WHERE tenant_id = :tenant_id AND id = :id", id=item_id) or 1)
        await self._emit("item.created", item_id=item_id, payload={
            "item_id": str(item_id), "item_number": item_number, "item_type_code": str(type_row["code"]),
            "base_uom_id": str(base_uom_id) if base_uom_id else None,
            "units_type_id": str(units_type_id) if units_type_id else None, "is_active": True,
        }, aggregate_version=version)
        if stockable:
            await self._emit("item.uom.changed", item_id=item_id, payload={"item_id": str(item_id), "stock_uom_id": str(base_uom_id), "roles": ["STOCK"], "conversion_version": 1}, aggregate_version=version)
        return await self.get_item(item_id)

    async def list_items(
        self,
        *,
        q: str | None = None,
        item_type_code: str | None = None,
        is_active: bool | None = None,
        category_id: str | None = None,
        include_inactive: bool = False,
        limit: int = 50,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        clauses = ["i.tenant_id = :tenant_id"]
        params: dict[str, Any] = {"limit": max(1, min(int(limit), 200))}
        if q:
            clauses.append("(i.item_number ILIKE :q OR i.display_name ILIKE :q)")
            params["q"] = f"%{q}%"
        if item_type_code:
            clauses.append("t.code = :type_code")
            params["type_code"] = str(item_type_code).upper()
        if is_active is not None:
            clauses.append("i.is_active = :is_active")
            params["is_active"] = bool(is_active)
        elif not include_inactive:
            clauses.append("i.is_active = true")
        if category_id:
            clauses.append("i.item_category_id = :category_id")
            params["category_id"] = uuid.UUID(str(category_id))
        if cursor:
            params["cursor"] = uuid.UUID(str(cursor))
            clauses.append("i.id > :cursor")
        rows = await self._all(
            "SELECT i.id, i.item_number, i.display_name, i.is_active, i.created_from, t.code AS item_type_code, "
            "i.base_uom_id, i.units_type_id, i.item_category_id, i.version "
            "FROM item_master.item i JOIN item_master.item_type_definition t "
            "ON t.tenant_id = i.tenant_id AND t.id = i.item_type_id "
            f"WHERE {' AND '.join(clauses)} ORDER BY i.id LIMIT :limit",
            **params,
        )
        items = [_jsonable(row) for row in rows]
        next_cursor = items[-1]["id"] if len(items) == params["limit"] else None
        return {"items": items, "next_cursor": next_cursor}

    async def get_item(self, item_id: uuid.UUID | str) -> dict[str, Any]:
        item = await self._one(
            "SELECT i.*, t.code AS item_type_code, c.code AS category_code, c.name AS category_name "
            "FROM item_master.item i JOIN item_master.item_type_definition t ON t.tenant_id = i.tenant_id AND t.id = i.item_type_id "
            "LEFT JOIN item_master.item_category c ON c.tenant_id = i.tenant_id AND c.id = i.item_category_id "
            "WHERE i.tenant_id = :tenant_id AND i.id = :id",
            id=uuid.UUID(str(item_id)),
        )
        if item is None:
            raise ItemNotFound("item not found")
        detail = _row(item)
        detail["inventory"] = await self._extension_row(item["id"], "item_inventory")
        detail["purchasing"] = await self._extension_row(item["id"], "item_purchasing")
        detail["sales"] = await self._extension_row(item["id"], "item_sales")
        detail["accounting"] = await self._extension_row(item["id"], "item_accounting")
        detail["uoms"] = await self.list_item_uoms(item["id"])
        detail["matrix_parent_id"] = str(item["matrix_parent_id"]) if item["matrix_parent_id"] else None
        return _jsonable(detail)

    async def patch_item(self, item_id: uuid.UUID | str, data: Mapping[str, Any], *, expected_version: int | None = None) -> dict[str, Any]:
        item = await self._one("SELECT id, version FROM item_master.item WHERE tenant_id = :tenant_id AND id = :id", id=uuid.UUID(str(item_id)))
        if item is None:
            raise ItemNotFound("item not found")
        if expected_version is not None and int(item["version"]) != int(expected_version):
            raise item_error("version mismatch (If-Match)", code="PRECONDITION_FAILED", http_status=412)
        allowed = {key: data[key] for key in ("display_name", "description", "item_category_id", "upc_code", "external_ref") if key in data}
        if allowed:
            sets = ", ".join(f"{key} = :{key}" for key in allowed)
            await self._exec(
                f"UPDATE item_master.item SET {sets}, updated_at = now(), updated_by = :actor, version = version + 1 "
                "WHERE tenant_id = :tenant_id AND id = :id",
                id=uuid.UUID(str(item_id)), actor=data.get("actor_id"), **allowed,
            )
            await self._emit("item.updated", item_id=item_id, payload={"item_id": str(item_id), "changed_fields": sorted(allowed), "version": int(item["version"]) + 1, "is_active": True})
        return await self.get_item(item_id)

    async def inactivate(self, item_id: uuid.UUID | str, reason: str, *, actor_id: str | None = None) -> dict[str, Any]:
        item = await self._one("SELECT id, is_active, version FROM item_master.item WHERE tenant_id = :tenant_id AND id = :id", id=uuid.UUID(str(item_id)))
        if item is None:
            raise ItemNotFound("item not found")
        children = await self._scalar(
            "SELECT count(*) FROM item_master.item WHERE tenant_id = :tenant_id AND matrix_parent_id = :id AND is_active = true",
            id=uuid.UUID(str(item_id)),
        )
        if int(children or 0) > 0:
            raise ItemHasActiveChildren("matrix parent has active children")
        now = utc_now()
        await self._exec(
            "UPDATE item_master.item SET is_active = false, inactive_at = :now, updated_at = now(), updated_by = :actor, version = version + 1 WHERE tenant_id = :tenant_id AND id = :id",
            id=uuid.UUID(str(item_id)), now=now, actor=actor_id,
        )
        await self._emit("item.inactivated", item_id=item_id, payload={"item_id": str(item_id), "reason": reason, "inactivated_at": now.isoformat()})
        return await self.get_item(item_id)

    async def reactivate(self, item_id: uuid.UUID | str, *, actor_id: str | None = None) -> dict[str, Any]:
        item = await self._one("SELECT id FROM item_master.item WHERE tenant_id = :tenant_id AND id = :id", id=uuid.UUID(str(item_id)))
        if item is None:
            raise ItemNotFound("item not found")
        await self._exec(
            "UPDATE item_master.item SET is_active = true, inactive_at = NULL, updated_at = now(), updated_by = :actor, version = version + 1 WHERE tenant_id = :tenant_id AND id = :id",
            id=uuid.UUID(str(item_id)), actor=actor_id,
        )
        await self._emit("item.updated", item_id=item_id, payload={"item_id": str(item_id), "changed_fields": ["is_active"], "version": int(item.get("version") or 1) + 1, "is_active": True})
        return await self.get_item(item_id)

    async def _extension_row(self, item_id: Any, table: str) -> dict[str, Any] | None:
        row = await self._one(f"SELECT * FROM item_master.{table} WHERE tenant_id = :tenant_id AND item_id = :item", item=item_id)
        return _jsonable(row) if row else None

    async def get_extension(self, item_id: uuid.UUID | str, extension: str) -> dict[str, Any]:
        if extension not in {"inventory", "purchasing", "sales", "accounting"}:
            raise item_error("unknown extension", code="ITEM_EXTENSION_UNKNOWN", http_status=404)
        await self._require_item(item_id)
        row = await self._extension_row(uuid.UUID(str(item_id)), f"item_{extension}")
        if row is None:
            raise item_error(f"{extension} settings not found for item", code="ITEM_EXTENSION_NOT_FOUND", http_status=404)
        return row

    async def _require_item(self, item_id: uuid.UUID | str) -> dict[str, Any]:
        row = await self._one("SELECT id, is_active FROM item_master.item WHERE tenant_id = :tenant_id AND id = :id", id=uuid.UUID(str(item_id)))
        if row is None:
            raise ItemNotFound("item not found")
        return row

    async def _upsert_inventory(self, item_id: uuid.UUID, data: Mapping[str, Any], *, base_uom_id: Any, is_new: bool = False, expected_version: int | None = None) -> dict[str, Any]:
        existing = await self._one("SELECT * FROM item_master.item_inventory WHERE tenant_id = :tenant_id AND item_id = :id", id=item_id)
        allowed = {
            "costing_method", "initial_cost", "asset_account_ref", "cogs_account_ref", "adjustment_account_ref",
            "variance_account_ref", "unbuild_variance_account_ref", "reorder_point", "preferred_stock_level",
            "safety_stock", "lead_time_days", "seasonal_demand", "auto_calculate_planning", "use_bins",
            "bin_required_on_txn", "use_statuses", "default_status_id", "allow_negative_inventory",
            "allow_vendor_consignment", "is_wip", "backflush_default", "yield_percent", "round_up_component_qty",
            "must_be_whole_base_unit", "stock_uom_id",
        }
        payload = {key: data[key] for key in allowed if key in data}
        if payload.get("costing_method") and str(payload["costing_method"]) not in {"AVERAGE", "FIFO", "LIFO", "STANDARD", "GROUP_AVERAGE", "SPECIFIC"}:
            raise ItemMasterError("invalid costing_method", code="COSTING_METHOD_INVALID")
        for key in ("reorder_point", "preferred_stock_level", "safety_stock", "seasonal_demand"):
            if key in payload and Decimal(str(payload[key])) < 0:
                raise PlanningValuesInvalid(f"{key} must be >= 0")
        if existing is None:
            payload.setdefault("costing_method", "AVERAGE")
            payload.setdefault("initial_cost", None)
            payload.setdefault("stock_uom_id", base_uom_id)
            payload["item_id"] = item_id
            payload["tenant_id"] = self.tenant_id
            cols = ["item_id", "tenant_id", *[key for key in payload if key not in {"item_id", "tenant_id"}]]
            placeholders = ", ".join(f":{col}" for col in cols)
            await self._exec(
                f"INSERT INTO item_master.item_inventory ({', '.join(cols)}, created_at, updated_at, version) "
                f"VALUES ({placeholders}, now(), now(), 1)",
                **payload,
            )
        else:
            if expected_version is not None and int(existing["version"]) != int(expected_version):
                raise item_error("version mismatch (If-Match)", code="PRECONDITION_FAILED", http_status=412)
            immutable = {"costing_method", "stock_uom_id", "initial_cost"}
            for key in immutable:
                if key in payload and payload[key] is not None and existing.get(key) != payload[key]:
                    if is_new:
                        continue
                    raise ItemMasterError(f"{key} is immutable after save", code="IMMUTABLE_FIELD")
            payload = {key: value for key, value in payload.items() if key not in immutable or existing.get(key) in (None, payload[key])}
            if payload:
                sets = ", ".join(f"{key} = :{key}" for key in payload)
                await self._exec(
                    f"UPDATE item_master.item_inventory SET {sets}, updated_at = now(), updated_by = :actor, version = version + 1 "
                    "WHERE tenant_id = :tenant_id AND item_id = :item_id",
                    item_id=item_id, actor=data.get("actor_id"), **{key: value for key, value in payload.items()},
                )
        return await self.get_extension(item_id, "inventory")

    async def upsert_extension(self, item_id: uuid.UUID | str, extension: str, data: Mapping[str, Any]) -> dict[str, Any]:
        item_uuid = uuid.UUID(str(item_id))
        item = await self._require_item(item_uuid)
        if extension == "inventory":
            if not item.get("is_active"):
                raise ItemInactive("inactive item")
            return await self._upsert_inventory(item_uuid, data, base_uom_id=data.get("stock_uom_id"), expected_version=data.get("expected_version"))
        if extension not in {"purchasing", "sales", "accounting"}:
            raise item_error("unknown extension", code="ITEM_EXTENSION_UNKNOWN", http_status=404)
        existing = await self._one(f"SELECT * FROM item_master.item_{extension} WHERE tenant_id = :tenant_id AND item_id = :item", item=item_uuid)
        payload = {key: value for key, value in data.items() if key not in {"expected_version", "actor_id"}}
        if extension == "purchasing":
            if payload.get("purchase_price") is not None and Decimal(str(payload["purchase_price"])) < 0:
                raise ItemMasterError("purchase_price must be >= 0", code="PURCHASE_PRICE_INVALID")
            if bool(payload.get("is_drop_ship")) and bool(payload.get("is_special_order")):
                raise ItemTypeFlagMismatch("drop_ship and special_order are mutually exclusive")
        if extension == "sales" and payload.get("weight") is not None and Decimal(str(payload["weight"])) < 0:
            raise ItemMasterError("weight must be >= 0", code="WEIGHT_INVALID")
        if existing is None:
            payload = {**payload, "item_id": item_uuid, "tenant_id": self.tenant_id}
            cols = list(payload)
            await self._exec(
                f"INSERT INTO item_master.item_{extension} ({', '.join(cols)}, created_at, updated_at, version) "
                f"VALUES ({', '.join(f':{col}' for col in cols)}, now(), now(), 1)",
                **payload,
            )
        elif payload:
            sets = ", ".join(f"{key} = :{key}" for key in payload)
            await self._exec(
                f"UPDATE item_master.item_{extension} SET {sets}, updated_at = now(), updated_by = :actor, version = version + 1 "
                "WHERE tenant_id = :tenant_id AND item_id = :item",
                item=item_uuid, actor=data.get("actor_id"), **payload,
            )
        await self._emit("item.updated", item_id=item_uuid, payload={"item_id": str(item_uuid), "changed_fields": sorted(payload), "version": 1, "is_active": bool(item.get("is_active", True))})
        return await self.get_extension(item_uuid, extension)

    # -- item UoMs / locations / planning ------------------------------------------------------
    async def list_item_uoms(self, item_id: uuid.UUID | str) -> list[dict[str, Any]]:
        rows = await self._all(
            "SELECT iu.uom_id, iu.role, iu.conversion_override, iu.is_default_for_role, u.code AS uom_code, "
            "u.conversion_factor, u.is_base, u.uom_type_id "
            "FROM item_master.item_uom iu JOIN item_master.uom u ON u.tenant_id = iu.tenant_id AND u.id = iu.uom_id "
            "WHERE iu.tenant_id = :tenant_id AND iu.item_id = :item ORDER BY iu.role, u.code",
            item=uuid.UUID(str(item_id)),
        )
        return [_jsonable(row) for row in rows]

    async def replace_item_uoms(self, item_id: uuid.UUID | str, uoms: Sequence[Mapping[str, Any]], *, allow_stock: bool = True, stock_base_uom_id: Any = None) -> list[dict[str, Any]]:
        item_uuid = uuid.UUID(str(item_id))
        item = await self._one("SELECT id, units_type_id, base_uom_id, item_type_id FROM item_master.item WHERE tenant_id = :tenant_id AND id = :id", id=item_uuid)
        if item is None:
            raise ItemNotFound("item not found")
        units_type_id = item["units_type_id"]
        seen_roles: set[str] = set()
        normalized: list[dict[str, Any]] = []
        for entry in uoms:
            role = str(entry.get("role") or "").upper()
            if role not in {"STOCK", "PURCHASE", "SALE", "CONSUMPTION", "DEFAULT"}:
                raise UomAssignmentInvalid(f"invalid role {role!r}")
            if role == "STOCK" and not allow_stock:
                raise UomAssignmentInvalid("non-stockable items cannot have STOCK UoM")
            uom_id = uuid.UUID(str(entry.get("uom_id")))
            uom = await self._one("SELECT id, uom_type_id, code, is_base FROM item_master.uom WHERE tenant_id = :tenant_id AND id = :id", id=uom_id)
            if uom is None:
                raise UomAssignmentInvalid("uom not found")
            if units_type_id and str(uom["uom_type_id"]) != str(units_type_id):
                raise UomAssignmentInvalid("UOM_TYPE_MISMATCH")
            if role == "STOCK" and stock_base_uom_id and uom_id != uuid.UUID(str(stock_base_uom_id)):
                raise UomAssignmentInvalid("STOCK role must be the item base UoM")
            seen_roles.add(role)
            normalized.append({"uom_id": uom_id, "role": role, "is_default": bool(entry.get("is_default_for_role", False)), "override": entry.get("conversion_override")})
        await self._exec("DELETE FROM item_master.item_uom WHERE tenant_id = :tenant_id AND item_id = :item", item=item_uuid)
        for entry in normalized:
            await self._exec(
                "INSERT INTO item_master.item_uom (tenant_id, item_id, uom_id, role, conversion_override, is_default_for_role, created_at, updated_at, version) "
                "VALUES (:tenant_id, :item, :uom, :role, :override, :is_default, now(), now(), 1)",
                item=item_uuid, uom=entry["uom_id"], role=entry["role"], override=entry["override"], is_default=entry["is_default"],
            )
        await self._emit("item.uom.changed", item_id=item_uuid, payload={"item_id": str(item_uuid), "roles": sorted(seen_roles), "conversion_version": 1})
        return await self.list_item_uoms(item_uuid)

    async def list_locations(self, item_id: uuid.UUID | str, *, location_id: str | None = None) -> list[dict[str, Any]]:
        rows = await self._all(
            "SELECT * FROM item_master.item_location WHERE tenant_id = :tenant_id AND item_id = :item "
            + ("AND location_id = :location " if location_id else "") + "ORDER BY location_id",
            item=uuid.UUID(str(item_id)),
            **({"location": uuid.UUID(str(location_id))} if location_id else {}),
        )
        return [_jsonable(row) for row in rows]

    async def replace_locations(self, item_id: uuid.UUID | str, locations: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        item_uuid = uuid.UUID(str(item_id))
        await self._require_item(item_uuid)
        if sum(1 for entry in locations if entry.get("is_preferred")) > 1:
            raise PlanningValuesInvalid("at most one preferred location")
        for entry in locations:
            location_id = uuid.UUID(str(entry["location_id"]))
            values = self._planning_payload(entry)
            existing = await self._one(
                "SELECT * FROM item_master.item_location WHERE tenant_id = :tenant_id AND item_id = :item AND location_id = :location",
                item=item_uuid, location=location_id,
            )
            if existing is None:
                await self._exec(
                    "INSERT INTO item_master.item_location (tenant_id, item_id, location_id, is_preferred, reorder_point, preferred_stock_level, safety_stock, lead_time_days, seasonal_demand, auto_calculate_planning, default_bin_id, default_status_id, cost_group_override_id, is_consigned_allowed, created_at, updated_at, version) "
                    "VALUES (:tenant_id, :item, :location, :is_preferred, :reorder_point, :preferred_stock_level, :safety_stock, :lead_time_days, :seasonal_demand, :auto_calculate_planning, :default_bin_id, :default_status_id, :cost_group_override_id, :is_consigned_allowed, now(), now(), 1)",
                    item=item_uuid, location=location_id, **values,
                )
            else:
                await self._exec(
                    "UPDATE item_master.item_location SET " + ", ".join(f"{key} = :{key}" for key in values) + ", updated_at = now(), version = version + 1 "
                    "WHERE tenant_id = :tenant_id AND item_id = :item AND location_id = :location",
                    item=item_uuid, location=location_id, **values,
                )
        await self._emit("item.updated", item_id=item_uuid, payload={"item_id": str(item_uuid), "changed_fields": ["locations"], "version": 1, "is_active": True})
        return await self.list_locations(item_uuid)

    def _planning_payload(self, entry: Mapping[str, Any]) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        defaults = {
            "is_preferred": False, "reorder_point": Decimal("0"), "preferred_stock_level": Decimal("0"),
            "safety_stock": Decimal("0"), "lead_time_days": 0, "seasonal_demand": Decimal("0"),
            "auto_calculate_planning": False, "default_bin_id": None, "default_status_id": None,
            "cost_group_override_id": None, "is_consigned_allowed": False,
        }
        for key in defaults:
            if key not in entry:
                continue
            value = entry[key]
            if key in {"reorder_point", "preferred_stock_level", "safety_stock", "seasonal_demand"}:
                value = Decimal(str(value))
                if value < 0:
                    raise PlanningValuesInvalid(f"{key} must be >= 0")
            if key == "lead_time_days" and int(value) < 0:
                raise PlanningValuesInvalid("lead_time_days must be >= 0")
            payload[key] = value
        return payload

    async def patch_location(self, item_id: uuid.UUID | str, location_id: str, data: Mapping[str, Any], *, expected_version: int | None = None) -> dict[str, Any]:
        item_uuid = uuid.UUID(str(item_id))
        loc_uuid = uuid.UUID(str(location_id))
        existing = await self._one("SELECT * FROM item_master.item_location WHERE tenant_id = :tenant_id AND item_id = :item AND location_id = :location", item=item_uuid, location=loc_uuid)
        if existing is None:
            raise ItemLocationNotFound("item location settings not found")
        if expected_version is not None and int(existing["version"]) != int(expected_version):
            raise item_error("version mismatch (If-Match)", code="PRECONDITION_FAILED", http_status=412)
        payload = self._planning_payload(data)
        if payload:
            await self._exec(
                "UPDATE item_master.item_location SET " + ", ".join(f"{key} = :{key}" for key in payload) + ", updated_at = now(), version = version + 1 "
                "WHERE tenant_id = :tenant_id AND item_id = :item AND location_id = :location",
                item=item_uuid, location=loc_uuid, **payload,
            )
        return (await self.list_locations(item_uuid, location_id=str(loc_uuid)))[0]

    async def apply_planning_values(
        self,
        item_id: uuid.UUID | str,
        *,
        location_id: str | None,
        values: Mapping[str, Any],
        source: str,
        policy_version: str | None,
        effective_at: datetime | None = None,
        expected_version: int | None = None,
        actor_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        item_uuid = uuid.UUID(str(item_id))
        item = await self._one("SELECT id, is_active FROM item_master.item WHERE tenant_id = :tenant_id AND id = :id", id=item_uuid)
        if item is None:
            raise ItemNotFound("item not found")
        if not item.get("is_active"):
            raise ItemInactive("inactive item")
        source = str(source).upper()
        if source not in {"MANUAL", "AIM", "OVERRIDE"}:
            raise PlanningValuesInvalid("source must be MANUAL|AIM|OVERRIDE")
        payload = self._planning_payload(values)
        if source == "AIM":
            target = (
                await self._one("SELECT auto_calculate_planning FROM item_master.item_location WHERE tenant_id = :tenant_id AND item_id = :item AND location_id = :location", item=item_uuid, location=uuid.UUID(str(location_id)))
                if location_id
                else await self._one("SELECT auto_calculate_planning FROM item_master.item_inventory WHERE tenant_id = :tenant_id AND item_id = :item", item=item_uuid)
            )
            if target is None or not target.get("auto_calculate_planning"):
                raise PlanningAutoCalcDisabled("source=AIM requires auto_calculate_planning=true")
        if location_id:
            existing = await self._one("SELECT version FROM item_master.item_location WHERE tenant_id = :tenant_id AND item_id = :item AND location_id = :location", item=item_uuid, location=uuid.UUID(str(location_id)))
            if existing is None:
                raise ItemLocationNotFound("item location settings not found")
            if expected_version is not None and int(existing["version"]) != int(expected_version):
                raise item_error("version mismatch (If-Match)", code="PRECONDITION_FAILED", http_status=412)
            if payload:
                await self._exec(
                    "UPDATE item_master.item_location SET " + ", ".join(f"{key} = :{key}" for key in payload) + ", updated_at = now(), version = version + 1 "
                    "WHERE tenant_id = :tenant_id AND item_id = :item AND location_id = :location",
                    item=item_uuid, location=uuid.UUID(str(location_id)), **payload,
                )
            version = int((await self._scalar("SELECT version FROM item_master.item_location WHERE tenant_id = :tenant_id AND item_id = :item AND location_id = :location", item=item_uuid, location=uuid.UUID(str(location_id)))) or 1)
        else:
            existing = await self._one("SELECT version FROM item_master.item_inventory WHERE tenant_id = :tenant_id AND item_id = :item", item=item_uuid)
            if existing is None:
                raise PlanningValuesInvalid("item has no inventory settings")
            if expected_version is not None and int(existing["version"]) != int(expected_version):
                raise item_error("version mismatch (If-Match)", code="PRECONDITION_FAILED", http_status=412)
            if payload:
                await self._exec(
                    "UPDATE item_master.item_inventory SET " + ", ".join(f"{key} = :{key}" for key in payload) + ", updated_at = now(), version = version + 1 "
                    "WHERE tenant_id = :tenant_id AND item_id = :item",
                    item=item_uuid, **payload,
                )
            version = int((await self._scalar("SELECT version FROM item_master.item_inventory WHERE tenant_id = :tenant_id AND item_id = :item", item=item_uuid)) or 1)
        event_payload = {
            "item_id": str(item_uuid), "location_id": str(location_id) if location_id else None,
            "values": _jsonable({key: value for key, value in payload.items()}), "source": source,
            "policy_version": policy_version, "effective_at": (effective_at or utc_now()).isoformat(),
            "applied_by": actor_id, "idempotency_key": idempotency_key,
        }
        await self._emit("item.planning_values.updated", item_id=item_uuid, payload=event_payload, aggregate_version=version)
        return {"item_id": str(item_uuid), "location_id": str(location_id) if location_id else None, "values": _jsonable(payload), "source": source, "policy_version": policy_version, "version": version}

    async def availability_settings(self, item_id: uuid.UUID | str) -> dict[str, Any]:
        item = await self._one("SELECT id FROM item_master.item WHERE tenant_id = :tenant_id AND id = :id", id=uuid.UUID(str(item_id)))
        if item is None:
            raise ItemNotFound("item not found")
        inv = await self._extension_row(uuid.UUID(str(item_id)), "item_inventory")
        if inv is None:
            return {"allow_negative_inventory": False, "allow_vendor_consignment": False, "use_bins": False, "bin_required_on_txn": False, "use_statuses": False, "default_status_id": None, "planning": {}}
        planning_keys = ("reorder_point", "preferred_stock_level", "safety_stock", "lead_time_days", "seasonal_demand", "auto_calculate_planning")
        return {
            "allow_negative_inventory": inv.get("allow_negative_inventory"),
            "allow_vendor_consignment": inv.get("allow_vendor_consignment"),
            "use_bins": inv.get("use_bins"),
            "bin_required_on_txn": inv.get("bin_required_on_txn"),
            "use_statuses": inv.get("use_statuses"),
            "default_status_id": inv.get("default_status_id"),
            "planning": {key: inv.get(key) for key in planning_keys},
        }

    # -- barcodes / aliases / related ----------------------------------------------------------
    async def list_barcodes(self, item_id: uuid.UUID | str, *, barcode_type: str | None = None) -> list[dict[str, Any]]:
        rows = await self._all(
            "SELECT * FROM item_master.item_barcode WHERE tenant_id = :tenant_id AND item_id = :item "
            + ("AND barcode_type = :barcode_type " if barcode_type else "") + "ORDER BY is_primary DESC, barcode",
            item=uuid.UUID(str(item_id)), **({"barcode_type": str(barcode_type).upper()} if barcode_type else {}),
        )
        return [_jsonable(row) for row in rows]

    def _validate_barcode(self, barcode: str, barcode_type: str) -> None:
        barcode_type = barcode_type.upper()
        if barcode_type == "UPC_A" and not (_UPC_RE.match(barcode) and _upc_checksum_ok(barcode)):
            raise BarcodeInvalid("invalid UPC-A barcode/check digit")
        if barcode_type == "UPC_E":
            if len(barcode) not in {6, 8}:
                raise BarcodeInvalid("invalid UPC-E barcode")
        if barcode_type == "EAN_13" and not (_EAN_RE.match(barcode) and _ean_checksum_ok(barcode)):
            raise BarcodeInvalid("invalid EAN-13 barcode/check digit")
        if barcode_type == "GS1" and not _GS1_RE.match(barcode):
            raise BarcodeInvalid("invalid GS1 barcode")
        if barcode_type == "CODE128" and not _CODE128_RE.match(barcode):
            raise BarcodeInvalid("invalid Code128 barcode")

    async def add_barcode(self, item_id: uuid.UUID | str, data: Mapping[str, Any]) -> dict[str, Any]:
        item_uuid = uuid.UUID(str(item_id))
        await self._require_item(item_uuid)
        barcode = str(data.get("barcode") or "").strip()
        barcode_type = str(data.get("barcode_type") or "CODE128").upper()
        self._validate_barcode(barcode, barcode_type)
        is_primary = bool(data.get("is_primary", False))
        barcode_id = uuid.uuid4()
        try:
            if is_primary:
                await self._exec("UPDATE item_master.item_barcode SET is_primary = false WHERE tenant_id = :tenant_id AND item_id = :item AND is_primary", item=item_uuid)
            await self._exec(
                "INSERT INTO item_master.item_barcode (id, tenant_id, item_id, barcode, barcode_type, uom_id, is_primary, is_active, created_at, updated_at, version) "
                "VALUES (:id, :tenant_id, :item, :barcode, :barcode_type, :uom, :is_primary, true, now(), now(), 1)",
                id=barcode_id, item=item_uuid, barcode=barcode, barcode_type=barcode_type,
                uom=uuid.UUID(str(data["uom_id"])) if data.get("uom_id") else None, is_primary=is_primary,
            )
        except IntegrityError as exc:
            raise BarcodeInvalid("barcode already exists for this tenant") from exc
        if is_primary and barcode_type == "UPC_A":
            await self._exec("UPDATE item_master.item SET upc_code = :upc, updated_at = now(), version = version + 1 WHERE tenant_id = :tenant_id AND id = :item", item=item_uuid, upc=barcode)
        await self._emit("item.barcode.added", item_id=item_uuid, payload={"item_id": str(item_uuid), "barcode": barcode, "barcode_type": barcode_type, "is_primary": is_primary})
        row = await self._one("SELECT * FROM item_master.item_barcode WHERE tenant_id = :tenant_id AND id = :id", id=barcode_id)
        return _row(row) if row else {"id": str(barcode_id)}

    async def update_barcode(self, item_id: uuid.UUID | str, barcode_id: str, data: Mapping[str, Any]) -> dict[str, Any]:
        item_uuid = uuid.UUID(str(item_id))
        barcode_uuid = uuid.UUID(str(barcode_id))
        if data.get("is_primary"):
            await self._exec("UPDATE item_master.item_barcode SET is_primary = false WHERE tenant_id = :tenant_id AND item_id = :item AND is_primary", item=item_uuid)
        values = {key: data[key] for key in ("is_primary", "uom_id", "is_active") if key in data}
        if values:
            await self._exec(
                "UPDATE item_master.item_barcode SET " + ", ".join(f"{key} = :{key}" for key in values) + ", updated_at = now(), version = version + 1 "
                "WHERE tenant_id = :tenant_id AND id = :id",
                id=barcode_uuid, **values,
            )
        row = await self._one("SELECT * FROM item_master.item_barcode WHERE tenant_id = :tenant_id AND id = :id", id=barcode_uuid)
        if row is None:
            raise ItemNotFound("barcode not found")
        if row.get("is_primary") and row.get("barcode_type") == "UPC_A":
            await self._exec("UPDATE item_master.item SET upc_code = :upc WHERE tenant_id = :tenant_id AND id = :item", item=item_uuid, upc=row["barcode"])
        return _row(row)

    async def resolve_barcode(self, barcode: str) -> dict[str, Any]:
        row = await self._one(
            "SELECT b.item_id, b.uom_id, b.barcode_type, i.item_number, i.is_active FROM item_master.item_barcode b "
            "JOIN item_master.item i ON i.tenant_id = b.tenant_id AND i.id = b.item_id "
            "WHERE b.tenant_id = :tenant_id AND b.barcode = :barcode AND b.is_active AND i.is_active",
            barcode=barcode,
        )
        if row is None:
            raise ItemNotFound("no active item found for barcode")
        return _row(row)

    async def list_aliases(self, item_id: uuid.UUID | str, *, alias_type: str | None = None) -> list[dict[str, Any]]:
        rows = await self._all(
            "SELECT * FROM item_master.item_alias WHERE tenant_id = :tenant_id AND item_id = :item "
            + ("AND alias_type = :alias_type " if alias_type else "") + "ORDER BY alias",
            item=uuid.UUID(str(item_id)), **({"alias_type": str(alias_type).upper()} if alias_type else {}),
        )
        return [_jsonable(row) for row in rows]

    async def add_alias(self, item_id: uuid.UUID | str, data: Mapping[str, Any]) -> dict[str, Any]:
        item_uuid = uuid.UUID(str(item_id))
        await self._require_item(item_uuid)
        alias = str(data.get("alias") or "").strip()
        if not alias:
            raise ItemMasterError("alias is required", code="ALIAS_INVALID")
        alias_type = str(data.get("alias_type") or "WMS").upper()
        if alias_type not in {"WMS", "SUPPLIER", "CUSTOMER_EXT", "LEGACY"}:
            raise ItemMasterError("invalid alias_type", code="ALIAS_INVALID")
        alias_id = uuid.uuid4()
        try:
            await self._exec(
                "INSERT INTO item_master.item_alias (id, tenant_id, item_id, alias, alias_type, is_active, created_at, updated_at, version) "
                "VALUES (:id, :tenant_id, :item, :alias, :alias_type, true, now(), now(), 1)",
                id=alias_id, item=item_uuid, alias=alias, alias_type=alias_type,
            )
        except IntegrityError as exc:
            raise item_error("alias already exists", code="ALIAS_EXISTS", http_status=409) from exc
        await self._emit("item.updated", item_id=item_uuid, payload={"item_id": str(item_uuid), "changed_fields": ["aliases"], "version": 1, "is_active": True})
        return {"id": str(alias_id), "item_id": str(item_uuid), "alias": alias, "alias_type": alias_type, "is_active": True}

    async def resolve_alias(self, alias: str, alias_type: str = "WMS") -> dict[str, Any]:
        row = await self._one(
            "SELECT a.item_id, a.alias_type, i.item_number, i.is_active FROM item_master.item_alias a "
            "JOIN item_master.item i ON i.tenant_id = a.tenant_id AND i.id = a.item_id "
            "WHERE a.tenant_id = :tenant_id AND a.alias = :alias AND a.alias_type = :alias_type AND a.is_active AND i.is_active",
            alias=alias, alias_type=alias_type.upper(),
        )
        if row is None:
            raise ItemNotFound("no active item found for alias")
        return _row(row)

    async def list_related(self, item_id: uuid.UUID | str, *, relation_type: str | None = None) -> list[dict[str, Any]]:
        rows = await self._all(
            "SELECT r.*, i.item_number AS related_item_number FROM item_master.item_related r "
            "JOIN item_master.item i ON i.tenant_id = r.tenant_id AND i.id = r.related_item_id "
            "WHERE r.tenant_id = :tenant_id AND r.item_id = :item "
            + ("AND r.relation_type = :relation_type " if relation_type else "") + "ORDER BY r.sort_order",
            item=uuid.UUID(str(item_id)), **({"relation_type": str(relation_type).upper()} if relation_type else {}),
        )
        return [_jsonable(row) for row in rows]

    async def add_related(self, item_id: uuid.UUID | str, data: Mapping[str, Any]) -> dict[str, Any]:
        item_uuid = uuid.UUID(str(item_id))
        related_uuid = uuid.UUID(str(data["related_item_id"]))
        if item_uuid == related_uuid:
            raise item_error("item cannot relate to itself", code="ITEM_RELATED_SELF", http_status=422)
        await self._require_item(related_uuid)
        relation_type = str(data.get("relation_type") or "RELATED").upper()
        related_id = uuid.uuid4()
        try:
            await self._exec(
                "INSERT INTO item_master.item_related (id, tenant_id, item_id, related_item_id, relation_type, sort_order, created_at, updated_at, version) "
                "VALUES (:id, :tenant_id, :item, :related, :relation_type, :sort_order, now(), now(), 1)",
                id=related_id, item=item_uuid, related=related_uuid, relation_type=relation_type,
                sort_order=int(data.get("sort_order", 100)),
            )
        except IntegrityError as exc:
            raise item_error("related item already exists", code="ITEM_RELATED_EXISTS", http_status=409) from exc
        return {"id": str(related_id), "item_id": str(item_uuid), "related_item_id": str(related_uuid), "relation_type": relation_type}

    async def delete_related(self, item_id: uuid.UUID | str, related_id: str) -> None:
        await self._exec("DELETE FROM item_master.item_related WHERE tenant_id = :tenant_id AND item_id = :item AND id = :id", item=uuid.UUID(str(item_id)), id=uuid.UUID(str(related_id)))

    # -- matrix -------------------------------------------------------------------------------
    async def list_matrix_options(self, item_id: uuid.UUID | str) -> list[dict[str, Any]]:
        options = await self._all(
            "SELECT id, code, name, sort_order FROM item_master.item_matrix_option WHERE tenant_id = :tenant_id AND parent_item_id = :parent ORDER BY sort_order, code",
            parent=uuid.UUID(str(item_id)),
        )
        for option in options:
            option["values"] = [
                _jsonable(row)
                for row in await self._all(
                    "SELECT id, code, value, sort_order, is_active FROM item_master.item_matrix_option_value WHERE tenant_id = :tenant_id AND option_id = :option ORDER BY sort_order, code",
                    option=option["id"],
                )
            ]
        return [_jsonable(option) for option in options]

    async def create_matrix_option(self, item_id: uuid.UUID | str, data: Mapping[str, Any]) -> dict[str, Any]:
        parent_uuid = uuid.UUID(str(item_id))
        await self._require_item(parent_uuid)
        item_type = await self._one(
            "SELECT t.code FROM item_master.item i JOIN item_master.item_type_definition t ON t.tenant_id = i.tenant_id AND t.id = i.item_type_id WHERE i.tenant_id = :tenant_id AND i.id = :id",
            id=parent_uuid,
        )
        if item_type is None or item_type["code"] != "MATRIX_PARENT":
            raise MatrixOptionInvalid("matrix options require a MATRIX_PARENT item")
        code = str(data.get("code") or "").strip().upper()
        if not code:
            raise MatrixOptionInvalid("option code is required")
        option_id = uuid.uuid4()
        try:
            await self._exec(
                "INSERT INTO item_master.item_matrix_option (id, tenant_id, parent_item_id, name, code, sort_order, created_at, updated_at, version) "
                "VALUES (:id, :tenant_id, :parent, :name, :code, :sort_order, now(), now(), 1)",
                id=option_id, parent=parent_uuid, name=str(data.get("name") or code), code=code,
                sort_order=int(data.get("sort_order", 100)),
            )
        except IntegrityError as exc:
            raise MatrixOptionInvalid("option code already exists") from exc
        for value in data.get("values") or []:
            await self._insert_option_value(option_uuid=option_id, value=value)
        await self._emit("item.matrix.changed", item_id=parent_uuid, payload={"parent_item_id": str(parent_uuid), "changed": ["option"], "children_count": 0})
        return (await self._matrix_option(option_id))

    async def _matrix_option(self, option_id: uuid.UUID) -> dict[str, Any]:
        option = await self._one("SELECT * FROM item_master.item_matrix_option WHERE tenant_id = :tenant_id AND id = :id", id=option_id)
        if option is None:
            raise MatrixOptionInvalid("matrix option not found")
        values = await self._all("SELECT * FROM item_master.item_matrix_option_value WHERE tenant_id = :tenant_id AND option_id = :option ORDER BY sort_order, code", option=option_id)
        return {**_row(option), "values": [_jsonable(value) for value in values]}

    async def _insert_option_value(self, *, option_uuid: uuid.UUID, value: Mapping[str, Any]) -> dict[str, Any]:
        value_code = str(value.get("code") or "").strip().upper()
        if not value_code:
            raise MatrixOptionInvalid("option value code is required")
        try:
            await self._exec(
                "INSERT INTO item_master.item_matrix_option_value (id, tenant_id, option_id, value, code, sort_order, is_active, created_at, updated_at, version) "
                "VALUES (:id, :tenant_id, :option, :value, :code, :sort_order, :is_active, now(), now(), 1)",
                id=uuid.uuid4(), option=option_uuid, value=str(value.get("value") or value_code), code=value_code,
                sort_order=int(value.get("sort_order", 100)), is_active=bool(value.get("is_active", True)),
            )
        except IntegrityError as exc:
            raise MatrixOptionInvalid("option value code already exists") from exc
        return {"code": value_code}

    async def replace_matrix_option_values(self, item_id: uuid.UUID | str, option_id: str, values: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        option_uuid = uuid.UUID(str(option_id))
        option = await self._one("SELECT id FROM item_master.item_matrix_option WHERE tenant_id = :tenant_id AND id = :id", id=option_uuid)
        if option is None:
            raise MatrixOptionInvalid("matrix option not found")
        await self._exec("DELETE FROM item_master.item_matrix_option_value WHERE tenant_id = :tenant_id AND option_id = :option", option=option_uuid)
        for value in values:
            await self._insert_option_value(option_uuid=option_uuid, value=value)
        await self._emit("item.matrix.changed", item_id=item_id, payload={"parent_item_id": str(item_id), "changed": ["option_values"], "children_count": 0})
        return await self._matrix_option(option_uuid)

    async def list_matrix_children(self, item_id: uuid.UUID | str, *, is_active: bool | None = None) -> list[dict[str, Any]]:
        rows = await self._all(
            "SELECT m.child_item_id, m.option_values, i.item_number, i.is_active FROM item_master.item_matrix_member m "
            "JOIN item_master.item i ON i.tenant_id = m.tenant_id AND i.id = m.child_item_id "
            "WHERE m.tenant_id = :tenant_id AND m.parent_item_id = :parent "
            + ("AND i.is_active = :active " if is_active is not None else "") + "ORDER BY i.item_number",
            parent=uuid.UUID(str(item_id)), **({"active": is_active} if is_active is not None else {}),
        )
        return [_jsonable(row) for row in rows]

    async def generate_matrix_children(self, item_id: uuid.UUID | str, combinations: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        parent_uuid = uuid.UUID(str(item_id))
        parent = await self._one("SELECT * FROM item_master.item WHERE tenant_id = :tenant_id AND id = :id", id=parent_uuid)
        if parent is None:
            raise ItemNotFound("matrix parent not found")
        if not combinations:
            raise MatrixOptionInvalid("combinations are required")
        if len(combinations) > MAX_MATRIX_COMBINATIONS:
            raise MatrixTooManyCombinations(f"at most {MAX_MATRIX_COMBINATIONS} combinations")
        options = await self.list_matrix_options(parent_uuid)
        option_codes = {str(option["code"]) for option in options}
        created: list[dict[str, Any]] = []
        for combination in combinations:
            mapping = combination.get("option_values") or {}
            if set(map(str, mapping)) != option_codes:
                raise MatrixOptionInvalid("option_values must cover exactly all parent options")
            item_number = str(combination.get("item_number") or "").strip()
            if not item_number:
                raise MatrixOptionInvalid("item_number is required per combination")
            existing = await self._one("SELECT id FROM item_master.item WHERE tenant_id = :tenant_id AND item_number = :number", number=item_number)
            if existing is not None:
                raise MatrixChildExists(f"item number {item_number!r} already exists")
            child_id = uuid.uuid4()
            child_type = await self._one("SELECT id FROM item_master.item_type_definition WHERE tenant_id = :tenant_id AND code = 'MATRIX_CHILD'")
            if child_type is None:
                raise ItemTypeNotFound("MATRIX_CHILD type missing from tenant taxonomy")
            try:
                await self._exec(
                    "INSERT INTO item_master.item (id, tenant_id, item_number, display_name, description, item_type_id, is_active, matrix_parent_id, base_uom_id, units_type_id, item_category_id, upc_code, external_ref, created_from, created_at, updated_at, version) "
                    "VALUES (:id, :tenant_id, :number, :name, :description, :type_id, true, :parent, :base_uom, :units_type, :category, NULL, NULL, 'MATRIX', now(), now(), 1)",
                    id=child_id, number=item_number, name=str(combination.get("display_name") or item_number),
                    description=parent.get("description"), type_id=child_type["id"], parent=parent_uuid,
                    base_uom=parent.get("base_uom_id"), units_type=parent.get("units_type_id"), category=parent.get("item_category_id"),
                )
            except (IntegrityError, TypeError) as exc:
                raise MatrixChildExists("matrix child creation conflicted") from exc
            await self._exec(
                "INSERT INTO item_master.item_matrix_member (tenant_id, parent_item_id, child_item_id, option_values, created_at, updated_at, version) "
                "VALUES (:tenant_id, :parent, :child, CAST(:values AS jsonb), now(), now(), 1)",
                parent=parent_uuid, child=child_id, values=json.dumps(_jsonable(mapping)),
            )
            created.append({"child_item_id": str(child_id), "item_number": item_number, "option_values": _jsonable(mapping)})
            await self._emit("item.created", item_id=child_id, payload={"item_id": str(child_id), "item_number": item_number, "item_type_code": "MATRIX_CHILD", "base_uom_id": str(parent["base_uom_id"]) if parent.get("base_uom_id") else None, "units_type_id": str(parent["units_type_id"]) if parent.get("units_type_id") else None, "is_active": True})
        await self._emit("item.matrix.changed", item_id=parent_uuid, payload={"parent_item_id": str(parent_uuid), "changed": ["children"], "children_count": len(created)})
        return {"created": created, "skipped": []}

    async def link_matrix_child(self, item_id: uuid.UUID | str, data: Mapping[str, Any]) -> dict[str, Any]:
        parent_uuid = uuid.UUID(str(item_id))
        child_uuid = uuid.UUID(str(data["child_item_id"]))
        child = await self._one("SELECT id, item_type_id FROM item_master.item WHERE tenant_id = :tenant_id AND id = :id", id=child_uuid)
        if child is None:
            raise ItemNotFound("child item not found")
        existing = await self._one("SELECT parent_item_id FROM item_master.item_matrix_member WHERE tenant_id = :tenant_id AND child_item_id = :child", child=child_uuid)
        if existing is not None:
            raise MatrixChildExists("child already has a matrix parent")
        await self._exec(
            "INSERT INTO item_master.item_matrix_member (tenant_id, parent_item_id, child_item_id, option_values, created_at, updated_at, version) "
            "VALUES (:tenant_id, :parent, :child, CAST(:values AS jsonb), now(), now(), 1)",
            parent=parent_uuid, child=child_uuid, values=json.dumps(_jsonable(data.get("option_values") or {})),
        )
        await self._exec("UPDATE item_master.item SET matrix_parent_id = :parent, updated_at = now(), version = version + 1 WHERE tenant_id = :tenant_id AND id = :child", parent=parent_uuid, child=child_uuid)
        await self._emit("item.matrix.changed", item_id=parent_uuid, payload={"parent_item_id": str(parent_uuid), "changed": ["link"], "children_count": 1})
        return {"parent_item_id": str(parent_uuid), "child_item_id": str(child_uuid)}

    # -- kit members --------------------------------------------------------------------------
    async def list_kit_members(self, item_id: uuid.UUID | str) -> dict[str, Any]:
        rows = await self._all(
            "SELECT m.*, i.item_number AS member_item_number FROM item_master.kit_group_member m "
            "JOIN item_master.item i ON i.tenant_id = m.tenant_id AND i.id = m.member_item_id "
            "WHERE m.tenant_id = :tenant_id AND m.parent_item_id = :parent ORDER BY m.sort_order, i.item_number",
            parent=uuid.UUID(str(item_id)),
        )
        parent = await self._one(
            "SELECT t.code FROM item_master.item i JOIN item_master.item_type_definition t ON t.tenant_id = i.tenant_id AND t.id = i.item_type_id WHERE i.tenant_id = :tenant_id AND i.id = :id",
            id=uuid.UUID(str(item_id)),
        )
        return {"parent_type": (parent or {}).get("code"), "members": [_jsonable(row) for row in rows]}

    async def add_kit_member(self, item_id: uuid.UUID | str, data: Mapping[str, Any]) -> dict[str, Any]:
        parent_uuid = uuid.UUID(str(item_id))
        parent = await self._one(
            "SELECT t.code FROM item_master.item i JOIN item_master.item_type_definition t ON t.tenant_id = i.tenant_id AND t.id = i.item_type_id WHERE i.tenant_id = :tenant_id AND i.id = :id",
            id=parent_uuid,
        )
        if parent is None or parent["code"] not in {"KIT", "GROUP"}:
            raise KitMemberInvalid("kit members require a KIT/GROUP parent")
        member_uuid = uuid.UUID(str(data["member_item_id"]))
        if member_uuid == parent_uuid:
            raise KitMemberInvalid("item cannot contain itself")
        member = await self._one("SELECT id, item_type_id FROM item_master.item WHERE tenant_id = :tenant_id AND id = :id", id=member_uuid)
        if member is None:
            raise ItemNotFound("member item not found")
        member_type = await self._one("SELECT code FROM item_master.item_type_definition WHERE tenant_id = :tenant_id AND id = :id", id=member["item_type_id"])
        if member_type is None or member_type["code"] not in {seed.code for seed in ITEM_TYPE_SEEDS if seed.can_stock}:
            raise KitMemberInvalid("kit members must be stockable items")
        qty = Decimal(str(data.get("qty", 1)))
        if qty <= 0:
            raise KitMemberInvalid("qty must be > 0")
        try:
            await self._exec(
                "INSERT INTO item_master.kit_group_member (id, tenant_id, parent_item_id, member_item_id, qty, uom_id, is_optional, sort_order, member_type, created_at, updated_at, version) "
                "VALUES (:id, :tenant_id, :parent, :member, :qty, :uom, :optional, :sort_order, :member_type, now(), now(), 1)",
                id=uuid.uuid4(), parent=parent_uuid, member=member_uuid, qty=qty,
                uom=uuid.UUID(str(data["uom_id"])) if data.get("uom_id") else None,
                optional=bool(data.get("is_optional", False)), sort_order=int(data.get("sort_order", 100)),
                member_type="GROUP_MEMBER" if parent["code"] == "GROUP" else "KIT_COMPONENT",
            )
        except IntegrityError as exc:
            raise KitMemberInvalid("member already exists in this kit/group") from exc
        await self._emit("item.updated", item_id=parent_uuid, payload={"item_id": str(parent_uuid), "changed_fields": ["kit_members"], "version": 1, "is_active": True})
        return await self.list_kit_members(parent_uuid)

    async def update_kit_member(self, item_id: uuid.UUID | str, member_id: str, data: Mapping[str, Any]) -> dict[str, Any]:
        values = {key: data[key] for key in ("qty", "uom_id", "is_optional", "sort_order") if key in data}
        if "qty" in values and Decimal(str(values["qty"])) <= 0:
            raise KitMemberInvalid("qty must be > 0")
        if values:
            await self._exec(
                "UPDATE item_master.kit_group_member SET " + ", ".join(f"{key} = :{key}" for key in values) + ", updated_at = now(), version = version + 1 "
                "WHERE tenant_id = :tenant_id AND parent_item_id = :parent AND id = :id",
                parent=uuid.UUID(str(item_id)), id=uuid.UUID(str(member_id)), **values,
            )
        return await self.list_kit_members(item_id)

    async def delete_kit_member(self, item_id: uuid.UUID | str, member_id: str) -> None:
        await self._exec("DELETE FROM item_master.kit_group_member WHERE tenant_id = :tenant_id AND parent_item_id = :parent AND id = :id", parent=uuid.UUID(str(item_id)), id=uuid.UUID(str(member_id)))

    # -- defaults / helpers -------------------------------------------------------------------
    async def _default_uom_type_id(self) -> uuid.UUID | None:
        row = await self._one("SELECT id FROM item_master.uom_type WHERE tenant_id = :tenant_id AND code = 'EA'")
        return row["id"] if row else None

    async def _default_base_uom_id(self, units_type_id: Any) -> uuid.UUID | None:
        if units_type_id is None:
            return None
        row = await self._one("SELECT base_uom_id FROM item_master.uom_type WHERE tenant_id = :tenant_id AND id = :id", id=uuid.UUID(str(units_type_id)))
        return row["base_uom_id"] if row else None

    async def _require_uom_membership(self, units_type_id: str, uom_id: str) -> None:
        row = await self._one("SELECT id, is_base FROM item_master.uom WHERE tenant_id = :tenant_id AND id = :id AND uom_type_id = :type_id", id=uuid.UUID(str(uom_id)), type_id=uuid.UUID(str(units_type_id)))
        if row is None:
            raise UomAssignmentInvalid("UOM_TYPE_MISMATCH")

    async def posting_eligibility(self, item_id: uuid.UUID | str, operation: str) -> dict[str, Any]:
        item = await self._one(
            "SELECT i.is_active, t.code FROM item_master.item i JOIN item_master.item_type_definition t ON t.tenant_id = i.tenant_id AND t.id = i.item_type_id WHERE i.tenant_id = :tenant_id AND i.id = :id",
            id=uuid.UUID(str(item_id)),
        )
        if item is None:
            raise ItemNotFound("item not found")
        return posting_eligibility(str(item["code"]), operation, is_active=bool(item["is_active"])).as_dict()


def _eligibility_map(code: str) -> dict[str, dict[str, object]]:
    return {operation.value: posting_eligibility(code, operation).as_dict() for operation in PostingOperation}


__all__ = ["MAX_MATRIX_COMBINATIONS", "SYSTEM_TENANT_ID", "ItemMasterService", "quantize_quantity", "utc_now"]
