"""Deterministic fixture factories for tenants, principals, items and postings.

These factories are intentionally ORM-free: FS-00+ test suites can use them before/without database
models, and database-backed suites can use them to build insert payloads. Every factory is
deterministic for a given ``index``/seed, so failures reproduce exactly.

RBAC scope sets are read from the checked-in authoritative catalogue copy
``app/tests/fixtures/permissions.json`` (byte-identical to ``design/permissions.json``), never
hard-coded in tests.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

from app.tests.utils.deterministic import FIXED_EPOCH, DeterministicUUIDs

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures"

# Deterministic id namespaces keep entity ids from colliding when factories are called with one index.
_ID_OFFSETS = {
    "tenant": 0,
    "principal": 1_000,
    "identity": 1_500,
    "item": 2_000,
    "location": 3_000,
    "status": 3_500,
    "posting": 4_000,
}


def _uuid(entity: str, index: int) -> UUID:
    if index < 0:
        raise ValueError("index must be non-negative")
    return DeterministicUUIDs().at(_ID_OFFSETS[entity] + index)


@lru_cache(maxsize=1)
def load_permissions_fixture() -> dict[str, Any]:
    """Load the checked-in RBAC catalogue copy used by tests."""
    path = FIXTURES_DIR / "permissions.json"
    return json.loads(path.read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def _role_templates() -> dict[str, frozenset[str]]:
    payload = load_permissions_fixture()
    return {role["key"]: frozenset(role.get("scopes") or []) for role in payload["role_templates"]}


def scopes_for_role(role: str = "admin") -> frozenset[str]:
    """Return the seeded scope set for a system role template."""
    try:
        return _role_templates()[role]
    except KeyError as exc:
        raise KeyError(f"unknown role template {role!r}; known: {sorted(_role_templates())}") from exc


def all_catalogue_scopes() -> frozenset[str]:
    return frozenset(row["scope"] for row in load_permissions_fixture()["catalogue"])


# ---------------------------------------------------------------------------
# Tenant / principal
# ---------------------------------------------------------------------------
@dataclass
class Tenant:
    id: UUID
    code: str
    name: str
    base_currency_code: str = "USD"
    timezone: str = "UTC"
    status: str = "ACTIVE"
    negative_inventory_policy: str = "BLOCK"
    rounding_mode: str = "PER_MOVEMENT"
    settings: dict[str, Any] = field(default_factory=dict)

    def to_payload(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "code": self.code,
            "name": self.name,
            "base_currency_code": self.base_currency_code,
            "timezone": self.timezone,
            "status": self.status,
            "negative_inventory_policy": self.negative_inventory_policy,
            "rounding_mode": self.rounding_mode,
            "settings": self.settings,
        }


@dataclass
class Principal:
    """Authenticated tenant principal (app_user) used by RBAC/tenant-isolation tests."""

    id: UUID
    identity_id: UUID
    tenant_id: UUID
    email: str
    display_name: str
    roles: tuple[str, ...] = ()
    scopes: frozenset[str] = field(default_factory=frozenset)
    data_scopes: dict[str, tuple[str, ...]] = field(default_factory=dict)
    token_version: int = 0
    membership_status: str = "ACTIVE"
    is_platform_admin: bool = False

    def has_scope(self, scope: str) -> bool:
        """``platform.admin`` implies every tenant capability scope (break-glass template)."""
        if "platform.admin" in self.scopes:
            return True
        return scope in self.scopes

    def has_any_scope(self, *scopes: str) -> bool:
        return any(self.has_scope(scope) for scope in scopes)

    @property
    def is_active(self) -> bool:
        return self.membership_status == "ACTIVE"

    def to_payload(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "identity_id": str(self.identity_id),
            "tenant_id": str(self.tenant_id),
            "email": self.email,
            "display_name": self.display_name,
            "roles": list(self.roles),
            "scopes": sorted(self.scopes),
            "data_scopes": {key: list(value) for key, value in sorted(self.data_scopes.items())},
            "token_version": self.token_version,
            "membership_status": self.membership_status,
            "is_platform_admin": self.is_platform_admin,
        }


@dataclass(frozen=True)
class Identity:
    """Global auth identity (Δ-MT ``platform.user_identity``; not tenant-scoped)."""

    id: UUID
    email: str
    display_name: str
    status: str = "ACTIVE"
    mfa_enabled: bool = False
    is_platform_admin: bool = False

    def to_payload(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "email": self.email,
            "display_name": self.display_name,
            "status": self.status,
            "mfa_enabled": self.mfa_enabled,
            "is_platform_admin": self.is_platform_admin,
        }


@dataclass(frozen=True)
class RoleSample:
    """System role template from the authoritative RBAC catalogue (Δ-RBAC)."""

    code: str
    name: str
    system: bool
    editable: bool
    cloneable: bool
    description: str
    scopes: frozenset[str]

    def to_payload(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "name": self.name,
            "is_system": self.system,
            "is_editable": self.editable,
            "is_cloneable": self.cloneable,
            "description": self.description,
            "scopes": sorted(self.scopes),
        }


def make_identity(index: int = 0, *, email: str | None = None, **overrides: Any) -> Identity:
    identity = Identity(
        id=_uuid("identity", index),
        email=email or f"identity{index}@example.test",
        display_name=f"Test Identity {index}",
    )
    if not overrides:
        return identity
    return Identity(**(identity.__dict__ | overrides))


def make_role(code: str = "admin") -> RoleSample:
    payload = load_permissions_fixture()
    for role in payload["role_templates"]:
        if role["key"] == code:
            return RoleSample(
                code=role["key"],
                name=role["name"],
                system=bool(role.get("system", True)),
                editable=bool(role.get("editable", False)),
                cloneable=bool(role.get("cloneable", True)),
                description=role.get("description", ""),
                scopes=frozenset(role.get("scopes") or []),
            )
    raise KeyError(f"unknown role template {code!r}")


def make_tenant(index: int = 0, **overrides: Any) -> Tenant:
    tenant = Tenant(
        id=_uuid("tenant", index),
        code=f"TEN{index:04d}",
        name=f"Test Tenant {index}",
        settings={"feature_flags": {}, **overrides.pop("settings", {})},
    )
    for key, value in overrides.items():
        if not hasattr(tenant, key):
            raise AttributeError(f"Tenant has no field {key!r}")
        setattr(tenant, key, value)
    return tenant


def make_principal(
    tenant: Tenant | None = None,
    index: int = 0,
    *,
    role: str | None = "admin",
    scopes: frozenset[str] | None = None,
    roles: tuple[str, ...] | None = None,
    data_scopes: dict[str, tuple[str, ...]] | None = None,
    is_platform_admin: bool = False,
    membership_status: str = "ACTIVE",
    **overrides: Any,
) -> Principal:
    tenant = tenant or make_tenant(index)
    if scopes is None:
        scopes = scopes_for_role(role) if role is not None else frozenset()
    resolved_roles = roles if roles is not None else ((role,) if role else ())
    principal = Principal(
        id=_uuid("principal", index),
        identity_id=_uuid("identity", index),
        tenant_id=tenant.id,
        email=f"user{index}@{tenant.code.lower()}.example.test",
        display_name=f"Test User {index}",
        roles=resolved_roles,
        scopes=frozenset(scopes),
        data_scopes=data_scopes or {},
        is_platform_admin=is_platform_admin,
        membership_status=membership_status,
    )
    for key, value in overrides.items():
        if not hasattr(principal, key):
            raise AttributeError(f"Principal has no field {key!r}")
        setattr(principal, key, value)
    return principal


# ---------------------------------------------------------------------------
# Item / UoM / location / status
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class UomPairSample:
    uom_type_code: str
    base_uom_code: str
    units: tuple[dict[str, Any], ...]

    def unit(self, code: str) -> dict[str, Any]:
        for unit in self.units:
            if unit["code"] == code:
                return unit
        raise KeyError(code)


@dataclass(frozen=True)
class ItemSample:
    id: UUID
    item_number: str
    display_name: str
    item_type_code: str
    uom: UomPairSample
    costing_method: str
    flags: dict[str, Any] = field(default_factory=dict)

    def to_payload(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "item_number": self.item_number,
            "display_name": self.display_name,
            "item_type_code": self.item_type_code,
            "base_uom_code": self.uom.base_uom_code,
            "uom_type_code": self.uom.uom_type_code,
            "costing_method": self.costing_method,
            "flags": dict(self.flags),
        }


@dataclass(frozen=True)
class LocationSample:
    id: UUID
    code: str
    name: str
    location_type: str = "WAREHOUSE"
    is_bins_enabled: bool = False
    is_statuses_enabled: bool = True
    is_consignment_allowed: bool = False
    is_unallocated: bool = False

    def to_payload(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "code": self.code,
            "name": self.name,
            "location_type": self.location_type,
            "is_bins_enabled": self.is_bins_enabled,
            "is_statuses_enabled": self.is_statuses_enabled,
            "is_consignment_allowed": self.is_consignment_allowed,
            "is_unallocated": self.is_unallocated,
        }


@dataclass(frozen=True)
class InventoryStatusSample:
    code: str
    name: str
    is_available_for_allocation: bool
    is_committable: bool
    is_sellable: bool
    is_consigned: bool = False
    is_default: bool = False

    def to_payload(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "name": self.name,
            "is_available_for_allocation": self.is_available_for_allocation,
            "is_committable": self.is_committable,
            "is_sellable": self.is_sellable,
            "is_consigned": self.is_consigned,
            "is_default": self.is_default,
        }


def _load_fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES_DIR / name).read_text(encoding="utf-8"))


def make_uom_pair(name: str = "eaches") -> UomPairSample:
    """UoM pair from the canonical fixture: ``eaches`` (EA/DZ/CS) or ``weight`` (KG/G/LB)."""
    payload = _load_fixture("uom.json")
    for pair in payload["uom_pairs"]:
        if pair["name"] == name:
            return UomPairSample(
                uom_type_code=pair["uom_type_code"],
                base_uom_code=pair["base_uom_code"],
                units=tuple(pair["units"]),
            )
    raise KeyError(f"unknown UoM pair {name!r}")


def make_item(
    index: int = 0,
    *,
    item_type_code: str = "INVENTORY",
    uom: UomPairSample | None = None,
    costing_method: str = "AVERAGE",
    flags: dict[str, Any] | None = None,
    item_number: str | None = None,
    display_name: str | None = None,
) -> ItemSample:
    uom = uom or make_uom_pair()
    default_flags: dict[str, Any] = {
        "is_lot_numbered": item_type_code == "LOT_NUMBERED",
        "is_serialized": item_type_code == "SERIALIZED",
        "requires_expiry": item_type_code == "LOT_NUMBERED",
        "is_stockable": item_type_code
        in {"INVENTORY", "LOT_NUMBERED", "SERIALIZED", "ASSEMBLY", "WIP", "KIT", "MATRIX_CHILD"},
        "allow_vendor_consignment": False,
    }
    if flags:
        default_flags.update(flags)
    return ItemSample(
        id=_uuid("item", index),
        item_number=item_number or f"ITEM-{index:05d}",
        display_name=display_name or f"Test Item {index}",
        item_type_code=item_type_code,
        uom=uom,
        costing_method=costing_method,
        flags=default_flags,
    )


def make_location(index: int = 0, **overrides: Any) -> LocationSample:
    location = LocationSample(
        id=_uuid("location", index),
        code=f"LOC-{index:03d}",
        name=f"Test Location {index}",
    )
    if not overrides:
        return location
    values = location.__dict__ | overrides
    return LocationSample(**values)


def make_status(code: str = "AVAILABLE", **overrides: Any) -> InventoryStatusSample:
    statuses = {row["code"]: row for row in _load_fixture("inventory_statuses.json")["records"]}
    if code not in statuses:
        raise KeyError(f"unknown inventory status {code!r}")
    row = statuses[code]
    values = {
        "code": row["code"],
        "name": row["name"],
        "is_available_for_allocation": row["is_available_for_allocation"],
        "is_committable": row["is_committable"],
        "is_sellable": row["is_sellable"],
        "is_consigned": row["is_consigned"],
        "is_default": row["is_default"],
    }
    values.update(overrides)
    return InventoryStatusSample(**values)


# ---------------------------------------------------------------------------
# Ledger posting sample
# ---------------------------------------------------------------------------
@dataclass
class LedgerPostingSample:
    """Synthetic posting used when the ledger engine/models are not available yet.

    Quantities/values are :class:`~decimal.Decimal` internally and serialised as decimal strings by
    :meth:`to_payload` (wire format).
    """

    id: UUID
    transaction_type: str
    transaction_date: date = date(2026, 1, 2)
    lines: list[dict[str, Any]] = field(default_factory=list)
    source_doc_type: str = "test_document"
    source_doc_id: UUID | None = None

    @property
    def net_qty_base(self) -> Decimal:
        return sum((Decimal(str(line["qty_base"])) for line in self.lines), Decimal("0"))

    @property
    def net_value_base(self) -> Decimal:
        return sum((Decimal(str(line["value_base"])) for line in self.lines), Decimal("0"))

    def to_payload(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "transaction_type": self.transaction_type,
            "transaction_date": self.transaction_date.isoformat(),
            "source_doc_type": self.source_doc_type,
            "source_doc_id": str(self.source_doc_id) if self.source_doc_id else None,
            "lines": [
                {**line, "qty_base": str(line["qty_base"]), "value_base": str(line["value_base"])}
                for line in self.lines
            ],
            "total_qty_base": str(self.net_qty_base),
            "total_value_base": str(self.net_value_base),
        }


def make_ledger_posting(
    index: int = 0,
    *,
    transaction_type: str = "T-01",
    item: ItemSample | None = None,
    location: LocationSample | None = None,
    status: InventoryStatusSample | None = None,
    qty: Decimal | str = Decimal("10"),
    unit_cost: Decimal | str = Decimal("5"),
    ownership: Literal["OWNED", "CONSIGNED"] = "OWNED",
    internal_move: bool = False,
) -> LedgerPostingSample:
    """Build a receipt-like posting (or a value-neutral internal move pair when ``internal_move``)."""
    item = item or make_item(0)
    location = location or make_location(0)
    status = status or make_status()
    qty_d = Decimal(str(qty))
    unit_cost_d = Decimal(str(unit_cost))
    lines: list[dict[str, Any]] = [
        {
            "item_id": str(item.id),
            "location_id": str(location.id),
            "inventory_status_id": status.code,
            "ownership": ownership,
            "qty_class": "ON_HAND",
            "qty_base": qty_d,
            "value_base": (qty_d * unit_cost_d).quantize(Decimal("0.000000000001")),
            "unit_cost": unit_cost_d,
            "movement_type": "RECEIPT",
        }
    ]
    if internal_move:
        destination = make_location(1)
        lines.append(
            {
                "item_id": str(item.id),
                "location_id": str(destination.id),
                "inventory_status_id": status.code,
                "ownership": ownership,
                "qty_class": "ON_HAND",
                "qty_base": -qty_d,
                "value_base": -(qty_d * unit_cost_d).quantize(Decimal("0.000000000001")),
                "unit_cost": unit_cost_d,
                "movement_type": "TRANSFER_OUT",
            }
        )
    return LedgerPostingSample(
        id=_uuid("posting", index),
        transaction_type=transaction_type,
        lines=lines,
        source_doc_id=_uuid("posting", index),
    )


def fixed_utc(value: datetime | None = None) -> datetime:
    """Timezone-aware UTC datetime defaulting to the suite epoch."""
    return value or FIXED_EPOCH


def utc_timestamp(clock) -> str:
    """ISO-8601 UTC timestamp with ``Z`` suffix (wire format)."""
    moment = clock.now().astimezone(UTC)
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")
