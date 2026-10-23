"""SQLAlchemy Core metadata for the 4 `traceability` registry tables (FS-03, DATA-MODEL §6).

Migration ``0007_traceability_registry`` compiles this metadata into explicit PostgreSQL DDL; the
registry repositories query these tables with async SQLAlchemy Core. No ORM imports, no writes to
FS-01 tables (deferred FKs are added by the FS-03 migration only).
"""

from __future__ import annotations

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    MetaData,
    Numeric,
    PrimaryKeyConstraint,
    Table,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID

from app.migrations.base import NAMING_CONVENTION

metadata = MetaData(schema="traceability", naming_convention=NAMING_CONVENTION)

UUID_T = UUID(as_uuid=True)
QTY = Numeric(38, 12)
STAMP = DateTime(timezone=True)


def _audit(columns: list) -> None:
    columns.extend(
        [
            Column("created_at", STAMP, nullable=False, server_default=text("now()")),
            Column("updated_at", STAMP, nullable=False, server_default=text("now()")),
            Column("created_by", UUID_T, nullable=True),
            Column("updated_by", UUID_T, nullable=True),
            Column("version", Integer, nullable=False, server_default=text("1")),
        ]
    )


def mk(name: str, columns: list, *, extra: list | None = None) -> Table:
    cols: list = [Column("id", UUID_T, nullable=False), Column("tenant_id", UUID_T, nullable=False), *columns]
    _audit(cols)
    constraints: list = list(extra or [])
    constraints.extend(
        [
            PrimaryKeyConstraint("id", name=f"pk_{name}"),
            UniqueConstraint("tenant_id", "id", name=f"uq_{name}_tenant_id_id"),
            ForeignKeyConstraint(["tenant_id"], ["platform.tenant.id"], name=f"fk_{name}_tenant"),
        ]
    )
    return Table(name, metadata, *cols, *constraints)


location = mk(
    "location",
    [
        Column("code", Text, nullable=False),
        Column("name", Text, nullable=False),
        Column("parent_id", UUID_T, nullable=True),
        Column("location_type", Text, nullable=False),
        Column("is_bins_enabled", Boolean, nullable=False, server_default=text("false")),
        Column("is_statuses_enabled", Boolean, nullable=False, server_default=text("false")),
        Column("is_consignment_allowed", Boolean, nullable=False, server_default=text("false")),
        Column("allow_commit_from_non_available", Boolean, nullable=False, server_default=text("false")),
        Column("subsidiary_ref", Text, nullable=True),
        Column("timezone", Text, nullable=False, server_default=text("'UTC'")),
        Column("address", JSONB, nullable=True),
        Column("is_unallocated", Boolean, nullable=False, server_default=text("false")),
        Column("is_inactive", Boolean, nullable=False, server_default=text("false")),
        Column("inactive_at", STAMP, nullable=True),
        Column("inactive_reason", Text, nullable=True),
    ],
    extra=[
        UniqueConstraint("tenant_id", "code", name="uq_location_code"),
        CheckConstraint(
            "location_type IN ('WAREHOUSE','STORE','RECEIVING','TRANSIT','CONSUMPTION','UNALLOCATED')",
            name="ck_location_type",
        ),
        CheckConstraint("NOT is_unallocated OR location_type = 'UNALLOCATED'", name="ck_location_unallocated_type"),
        CheckConstraint("length(code) > 0", name="ck_location_code_nonempty"),
        ForeignKeyConstraint(
            ["tenant_id", "parent_id"],
            ["traceability.location.tenant_id", "traceability.location.id"],
            name="fk_location_parent",
        ),
    ],
)
Index("uq_location_unallocated", location.c.tenant_id, unique=True, postgresql_where=text("is_unallocated"))
Index("ix_location_parent", location.c.tenant_id, location.c.parent_id)
Index("ix_location_active", location.c.tenant_id, location.c.is_inactive)

zone = mk(
    "zone",
    [
        Column("location_id", UUID_T, nullable=False),
        Column("code", Text, nullable=False),
        Column("name", Text, nullable=False),
        Column("zone_type", Text, nullable=False),
        Column("priority", Integer, nullable=False, server_default=text("0")),
        Column("is_active", Boolean, nullable=False, server_default=text("true")),
    ],
    extra=[
        UniqueConstraint("tenant_id", "location_id", "code", name="uq_zone_code"),
        CheckConstraint("zone_type IN ('STORAGE','RECEIVING','STAGING','PICKING','SHIPPING')", name="ck_zone_type"),
        CheckConstraint("priority >= 0", name="ck_zone_priority"),
        ForeignKeyConstraint(
            ["tenant_id", "location_id"],
            ["traceability.location.tenant_id", "traceability.location.id"],
            name="fk_zone_location",
        ),
    ],
)
Index("ix_zone_location", zone.c.tenant_id, zone.c.location_id, zone.c.priority)

bin = mk(
    "bin",
    [
        Column("location_id", UUID_T, nullable=False),
        Column("zone_id", UUID_T, nullable=True),
        Column("code", Text, nullable=False),
        Column("name", Text, nullable=False),
        Column("bin_type", Text, nullable=False),
        Column("is_pickable", Boolean, nullable=False, server_default=text("false")),
        Column("is_active", Boolean, nullable=False, server_default=text("true")),
        Column("capacity_qty", QTY, nullable=True),
        Column("capacity_uom_id", UUID_T, nullable=True),
        Column("coordinates", JSONB, nullable=True),
    ],
    extra=[
        UniqueConstraint("tenant_id", "location_id", "code", name="uq_bin_code"),
        CheckConstraint("bin_type IN ('STORAGE','PICKING','STAGING','RECEIVING','DOCK')", name="ck_bin_type"),
        CheckConstraint("capacity_qty IS NULL OR capacity_qty > 0", name="ck_bin_capacity"),
        ForeignKeyConstraint(
            ["tenant_id", "location_id"],
            ["traceability.location.tenant_id", "traceability.location.id"],
            name="fk_bin_location",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "zone_id"], ["traceability.zone.tenant_id", "traceability.zone.id"], name="fk_bin_zone"
        ),
        ForeignKeyConstraint(
            ["tenant_id", "capacity_uom_id"],
            ["item_master.uom.tenant_id", "item_master.uom.id"],
            name="fk_bin_capacity_uom",
        ),
    ],
)
Index("ix_bin_zone", bin.c.tenant_id, bin.c.zone_id)
Index(
    "ix_bin_pickable",
    bin.c.tenant_id,
    bin.c.location_id,
    postgresql_where=text("is_pickable AND is_active"),
)

inventory_status = mk(
    "inventory_status",
    [
        Column("code", Text, nullable=False),
        Column("name", Text, nullable=False),
        Column("is_available_for_allocation", Boolean, nullable=False, server_default=text("true")),
        Column("is_committable", Boolean, nullable=False, server_default=text("true")),
        Column("is_sellable", Boolean, nullable=False, server_default=text("true")),
        Column("is_consigned", Boolean, nullable=False, server_default=text("false")),
        Column("is_default", Boolean, nullable=False, server_default=text("false")),
        Column("is_system", Boolean, nullable=False, server_default=text("false")),
        Column("is_active", Boolean, nullable=False, server_default=text("true")),
    ],
    extra=[
        UniqueConstraint("tenant_id", "code", name="uq_status_code"),
        CheckConstraint("NOT (is_consigned AND is_default)", name="ck_status_consigned_default"),
        CheckConstraint("NOT is_system OR is_active", name="ck_status_system_active"),
        CheckConstraint("length(code) > 0", name="ck_status_code_nonempty"),
    ],
)
Index(
    "uq_status_default",
    inventory_status.c.tenant_id,
    unique=True,
    postgresql_where=text("is_default AND NOT is_consigned"),
)
Index("ix_status_active", inventory_status.c.tenant_id, inventory_status.c.is_active)

REVISION_0007_TABLES: tuple[Table, ...] = (location, zone, bin, inventory_status)

__all__ = [
    "REVISION_0007_TABLES",
    "bin",
    "inventory_status",
    "location",
    "metadata",
    "zone",
]
