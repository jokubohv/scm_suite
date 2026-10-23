"""SQLAlchemy Core metadata for the 3 `traceability` numbered-inventory tables (FS-06, DATA-MODEL §6).

Migration ``0013_numbered_inventory`` compiles this metadata into explicit PostgreSQL DDL. The module
repositories query these tables with async SQLAlchemy Core; serial projection/history rows are written by
the migration's posting triggers (``trg_serial_projection_sync``), never by direct API updates.
"""

from __future__ import annotations

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    Date,
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
from sqlalchemy.dialects.postgresql import UUID

from app.migrations.base import NAMING_CONVENTION

metadata = MetaData(schema="traceability", naming_convention=NAMING_CONVENTION)

UUID_T = UUID(as_uuid=True)
MONEY = Numeric(38, 12)
STAMP = DateTime(timezone=True)

SERIAL_STATES = (
    "AVAILABLE",
    "COMMITTED",
    "IN_TRANSIT",
    "FULFILLED",
    "RETURNED",
    "SCRAPPED",
    "UNKNOWN",
)
SERIAL_EVENTS = ("RECEIVED", "MOVED", "COMMITTED", "RELEASED", "FULFILLED", "RETURNED", "ADJUSTED", "SCRAPPED")
OWNERSHIP_VALUES = ("OWNED", "CONSIGNED")


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


lot = mk(
    "lot",
    [
        Column("item_id", UUID_T, nullable=False),
        Column("lot_number", Text, nullable=False),
        Column("manufacture_date", Date, nullable=True),
        Column("expiry_date", Date, nullable=True),
        Column("cost_basis", MONEY, nullable=True),
        Column("is_active", Boolean, nullable=False, server_default=text("true")),
        Column("notes", Text, nullable=True),
        Column("inactive_at", STAMP, nullable=True),
        Column("inactive_reason", Text, nullable=True),
    ],
    extra=[
        UniqueConstraint("tenant_id", "item_id", "lot_number", name="uq_lot_number"),
        CheckConstraint("length(lot_number) > 0", name="ck_lot_number_nonempty"),
        CheckConstraint(
            "manufacture_date IS NULL OR expiry_date IS NULL OR expiry_date >= manufacture_date",
            name="ck_lot_expiry_order",
        ),
        CheckConstraint("cost_basis IS NULL OR cost_basis >= 0", name="ck_lot_cost_basis"),
        ForeignKeyConstraint(
            ["tenant_id", "item_id"], ["item_master.item.tenant_id", "item_master.item.id"], name="fk_lot_item"
        ),
    ],
)
Index("ix_lot_expiry", lot.c.tenant_id, lot.c.expiry_date)
Index("ix_lot_item_active", lot.c.tenant_id, lot.c.item_id, lot.c.is_active)

serial_number = mk(
    "serial_number",
    [
        Column("item_id", UUID_T, nullable=False),
        Column("serial_number", Text, nullable=False),
        Column("state", Text, nullable=False, server_default=text("'UNKNOWN'")),
        Column("current_location_id", UUID_T, nullable=True),
        Column("current_bin_id", UUID_T, nullable=True),
        Column("current_status_id", UUID_T, nullable=True),
        Column("ownership", Text, nullable=False, server_default=text("'OWNED'")),
        Column("specific_cost", MONEY, nullable=True),
        Column("last_transaction_id", UUID_T, nullable=True),
        Column("customer_ref", Text, nullable=True),
    ],
    extra=[
        UniqueConstraint("tenant_id", "item_id", "serial_number", name="uq_serial_number"),
        CheckConstraint("length(serial_number) > 0", name="ck_serial_number_nonempty"),
        CheckConstraint("state IN (" + ",".join(f"'{value}'" for value in SERIAL_STATES) + ")", name="ck_serial_state"),
        CheckConstraint(
            "ownership IN (" + ",".join(f"'{value}'" for value in OWNERSHIP_VALUES) + ")",
            name="ck_serial_ownership",
        ),
        CheckConstraint("specific_cost IS NULL OR specific_cost >= 0", name="ck_serial_specific_cost"),
        ForeignKeyConstraint(
            ["tenant_id", "item_id"], ["item_master.item.tenant_id", "item_master.item.id"], name="fk_serial_item"
        ),
        ForeignKeyConstraint(
            ["tenant_id", "current_location_id"],
            ["traceability.location.tenant_id", "traceability.location.id"],
            name="fk_serial_location",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "current_bin_id"],
            ["traceability.bin.tenant_id", "traceability.bin.id"],
            name="fk_serial_bin",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "current_status_id"],
            ["traceability.inventory_status.tenant_id", "traceability.inventory_status.id"],
            name="fk_serial_status",
        ),
    ],
)
Index("ix_serial_state", serial_number.c.tenant_id, serial_number.c.state)
Index("ix_serial_location", serial_number.c.tenant_id, serial_number.c.current_location_id)
Index("ix_serial_item_state", serial_number.c.tenant_id, serial_number.c.item_id, serial_number.c.state)

#: Append-only serial history; monthly RANGE partitions are created by the migration.
serial_number_history = Table(
    "serial_number_history",
    metadata,
    Column("id", UUID_T, nullable=False),
    Column("tenant_id", UUID_T, nullable=False),
    Column("serial_id", UUID_T, nullable=False),
    Column("inventory_transaction_id", UUID_T, nullable=True),
    Column("inventory_transaction_line_id", UUID_T, nullable=True),
    Column("event", Text, nullable=False),
    Column("from_location_id", UUID_T, nullable=True),
    Column("from_bin_id", UUID_T, nullable=True),
    Column("from_status_id", UUID_T, nullable=True),
    Column("to_location_id", UUID_T, nullable=True),
    Column("to_bin_id", UUID_T, nullable=True),
    Column("to_status_id", UUID_T, nullable=True),
    Column("ownership", Text, nullable=False, server_default=text("'OWNED'")),
    Column("customer_ref", Text, nullable=True),
    Column("occurred_at", STAMP, nullable=False, server_default=text("now()")),
    PrimaryKeyConstraint("id", "occurred_at", name="pk_serial_number_history"),
    UniqueConstraint("tenant_id", "id", "occurred_at", name="uq_serial_number_history_tenant_id_id"),
    CheckConstraint("event IN (" + ",".join(f"'{value}'" for value in SERIAL_EVENTS) + ")", name="ck_history_event"),
    ForeignKeyConstraint(["tenant_id"], ["platform.tenant.id"], name="fk_serial_number_history_tenant"),
    ForeignKeyConstraint(
        ["tenant_id", "serial_id"],
        ["traceability.serial_number.tenant_id", "traceability.serial_number.id"],
        name="fk_history_serial",
    ),
    Index("ix_history_serial", "tenant_id", "serial_id", "occurred_at"),
    Index("ix_history_txn", "tenant_id", "inventory_transaction_id"),
    Index("ix_history_occurred_brin", "occurred_at", postgresql_using="brin"),
    postgresql_partition_by="RANGE (occurred_at)",
)

REVISION_0013_TABLES: tuple[Table, ...] = (lot, serial_number, serial_number_history)
PARTITIONED_TABLES: tuple[str, ...] = ("serial_number_history",)

__all__ = [
    "OWNERSHIP_VALUES",
    "PARTITIONED_TABLES",
    "REVISION_0013_TABLES",
    "SERIAL_EVENTS",
    "SERIAL_STATES",
    "lot",
    "metadata",
    "serial_number",
    "serial_number_history",
]
