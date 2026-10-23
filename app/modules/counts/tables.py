"""SQLAlchemy Core metadata for the 6 ``counts`` tables (FS-07, DATA-MODEL §9).

Migration ``0014_counts`` compiles this metadata into explicit PostgreSQL DDL; the counts repository
queries these tables with async SQLAlchemy Core. Position FKs reference the frozen item-master and
traceability registries; lot/serial identity columns are logical references validated through the
``traceability.numbered`` contract (FS-06) rather than schema FKs, so the counts migration stays
additive with respect to the FS-06 revision.
"""

from __future__ import annotations

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    Computed,
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
from sqlalchemy.dialects.postgresql import JSONB, UUID

from app.migrations.base import NAMING_CONVENTION

metadata = MetaData(schema="counts", naming_convention=NAMING_CONVENTION)

UUID_T = UUID(as_uuid=True)
QTY = Numeric(38, 12)
MONEY = Numeric(38, 12)
STAMP = DateTime(timezone=True)

FREQUENCIES = ("DAILY", "WEEKLY", "MONTHLY", "QUARTERLY", "ADHOC")
PLAN_STATUSES = ("ACTIVE", "PAUSED", "ARCHIVED")
COUNT_TYPES = ("CALCULATED", "MANUAL", "CYCLE")
COUNT_STATUSES = (
    "DRAFT",
    "OPEN",
    "COUNTING",
    "PENDING_REVIEW",
    "APPROVED",
    "REJECTED",
    "POSTED",
    "CANCELLED",
)
LINE_STATUSES = ("PENDING", "COUNTED", "RECOUNT_REQUIRED", "APPROVED", "REJECTED")
FILTER_TYPES = ("LOCATION", "ZONE", "BIN", "ITEM", "ITEM_CATEGORY", "ABC_CLASS", "STATUS")
FILTER_OPERATORS = ("INCLUDE", "EXCLUDE")
OWNERSHIPS = ("OWNED", "CONSIGNED")
POSITIONS = (
    "item_id",
    "location_id",
    "bin_id",
    "lot_id",
    "serial_id",
    "inventory_status_id",
    "ownership",
)


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


def _tenant_fk(name: str) -> ForeignKeyConstraint:
    return ForeignKeyConstraint(["tenant_id"], ["platform.tenant.id"], name=f"fk_{name}_tenant")


def _fk(local: str, ref_schema: str, ref_table: str, *, nullable: bool = False) -> ForeignKeyConstraint:
    return ForeignKeyConstraint(
        ["tenant_id", local],
        [f"{ref_schema}.{ref_table}.tenant_id", f"{ref_schema}.{ref_table}.id"],
        name=f"fk_{ref_table}_{local}",
    )


def mk(name: str, columns: list, *, extra: list | None = None, indexes: list | None = None) -> Table:
    cols: list = [Column("id", UUID_T, nullable=False), Column("tenant_id", UUID_T, nullable=False), *columns]
    _audit(cols)
    constraints: list = list(extra or [])
    constraints.extend(
        [
            PrimaryKeyConstraint("id", name=f"pk_{name}"),
            UniqueConstraint("tenant_id", "id", name=f"uq_{name}_tenant_id_id"),
            _tenant_fk(name),
        ]
    )
    table = Table(name, metadata, *cols, *constraints)
    for index in indexes or []:
        Index(f"ix_{name}_{index[0]}", table.c.tenant_id, *(table.c[column] for column in index[1:]))
    return table


count_plan = mk(
    "count_plan",
    [
        Column("code", Text, nullable=False),
        Column("name", Text, nullable=False),
        Column("frequency", Text, nullable=False),
        Column("next_run_date", Date, nullable=False),
        Column("last_run_at", STAMP, nullable=True),
        Column("status", Text, nullable=False, server_default=text("'ACTIVE'")),
        Column("blind_count", Boolean, nullable=False, server_default=text("false")),
        Column("approval_required", Boolean, nullable=False, server_default=text("true")),
        Column("include_zero_qty", Boolean, nullable=False, server_default=text("false")),
        Column("notes", Text, nullable=True),
    ],
    extra=[
        UniqueConstraint("tenant_id", "code", name="uq_count_plan_code"),
        CheckConstraint(f"frequency IN {FREQUENCIES!r}".replace("'", "'"), name="ck_count_plan_frequency"),
        CheckConstraint(f"status IN {PLAN_STATUSES!r}".replace("'", "'"), name="ck_count_plan_status"),
        CheckConstraint("length(code) > 0", name="ck_count_plan_code_nonempty"),
    ],
    indexes=[("status_next", "status", "next_run_date")],
)

count_plan_filter = mk(
    "count_plan_filter",
    [
        Column("count_plan_id", UUID_T, nullable=False),
        Column("filter_type", Text, nullable=False),
        Column("filter_operator", Text, nullable=False, server_default=text("'INCLUDE'")),
        Column("value_text", Text, nullable=True),
        Column("value_uuid", UUID_T, nullable=True),
        Column("sort_order", Integer, nullable=False, server_default=text("0")),
    ],
    extra=[
        UniqueConstraint("tenant_id", "count_plan_id", "sort_order", name="uq_count_plan_filter_order"),
        CheckConstraint(f"filter_type IN {FILTER_TYPES!r}".replace("'", "'"), name="ck_count_plan_filter_type"),
        CheckConstraint(f"filter_operator IN {FILTER_OPERATORS!r}".replace("'", "'"), name="ck_count_plan_filter_operator"),
        CheckConstraint("value_text IS NOT NULL OR value_uuid IS NOT NULL", name="ck_count_plan_filter_value"),
        CheckConstraint("sort_order >= 0", name="ck_count_plan_filter_sort"),
        ForeignKeyConstraint(
            ["tenant_id", "count_plan_id"],
            ["counts.count_plan.tenant_id", "counts.count_plan.id"],
            name="fk_count_plan_filter_plan",
        ),
    ],
    indexes=[("plan", "count_plan_id")],
)

inventory_count = mk(
    "inventory_count",
    [
        Column("count_number", Text, nullable=False),
        Column("count_plan_id", UUID_T, nullable=True),
        Column("count_type", Text, nullable=False),
        Column("status", Text, nullable=False, server_default=text("'DRAFT'")),
        Column("count_date", Date, nullable=False),
        Column("snapshot_at", STAMP, nullable=True),
        Column("blind_count", Boolean, nullable=False, server_default=text("false")),
        Column("approval_required", Boolean, nullable=False, server_default=text("true")),
        Column("variance_account_ref", Text, nullable=True),
        Column("recalc_snapshot_on_reject", Boolean, nullable=False, server_default=text("true")),
        Column("approved_at", STAMP, nullable=True),
        Column("approved_by", UUID_T, nullable=True),
        Column("posted_transaction_id", UUID_T, nullable=True),
        Column("count_round", Integer, nullable=False, server_default=text("1")),
        Column("scope", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
        Column("reason", Text, nullable=True),
    ],
    extra=[
        UniqueConstraint("tenant_id", "count_number", name="uq_inventory_count_number"),
        UniqueConstraint(
            "tenant_id", "posted_transaction_id", name="uq_inventory_count_posted_txn",
            postgresql_nulls_not_distinct=True,
        ),
        CheckConstraint(f"count_type IN {COUNT_TYPES!r}".replace("'", "'"), name="ck_count_type"),
        CheckConstraint(f"status IN {COUNT_STATUSES!r}".replace("'", "'"), name="ck_inventory_count_status"),
        CheckConstraint("count_round > 0", name="ck_inventory_count_round"),
        ForeignKeyConstraint(
            ["tenant_id", "count_plan_id"], ["counts.count_plan.tenant_id", "counts.count_plan.id"],
            name="fk_inventory_count_plan",
        ),
        _fk("approved_by", "platform", "app_user", nullable=True),
    ],
    indexes=[("status_date", "status", "count_date"), ("plan", "count_plan_id"), ("type", "count_type")],
)

inventory_count_line = mk(
    "inventory_count_line",
    [
        Column("inventory_count_id", UUID_T, nullable=False),
        Column("line_number", Integer, nullable=False),
        Column("item_id", UUID_T, nullable=False),
        Column("location_id", UUID_T, nullable=False),
        Column("bin_id", UUID_T, nullable=True),
        Column("lot_id", UUID_T, nullable=True),
        Column("serial_id", UUID_T, nullable=True),
        Column("inventory_status_id", UUID_T, nullable=False),
        Column("ownership", Text, nullable=False, server_default=text("'OWNED'")),
        Column("uom_id", UUID_T, nullable=False),
        Column("snapshot_qty", QTY, nullable=False, server_default=text("0")),
        Column("counted_qty", QTY, nullable=True),
        Column("variance_qty", QTY, Computed("counted_qty - snapshot_qty", persisted=True), nullable=True),
        Column("count_status", Text, nullable=False, server_default=text("'PENDING'")),
        Column("counted_by", UUID_T, nullable=True),
        Column("counted_at", STAMP, nullable=True),
        Column("notes", Text, nullable=True),
    ],
    extra=[
        UniqueConstraint(
            "tenant_id", "inventory_count_id", *POSITIONS,
            name="uq_inventory_count_line_position", postgresql_nulls_not_distinct=True,
        ),
        UniqueConstraint("tenant_id", "inventory_count_id", "line_number", name="uq_inventory_count_line_number"),
        CheckConstraint(f"count_status IN {LINE_STATUSES!r}".replace("'", "'"), name="ck_inventory_count_line_status"),
        CheckConstraint("counted_qty IS NULL OR counted_qty >= 0", name="ck_inventory_count_line_counted"),
        CheckConstraint("snapshot_qty = snapshot_qty", name="ck_inventory_count_line_snapshot"),
        CheckConstraint(f"ownership IN {OWNERSHIPS!r}".replace("'", "'"), name="ck_inventory_count_line_ownership"),
        CheckConstraint("serial_id IS NULL OR (counted_qty IS NULL OR counted_qty <= 1)", name="ck_inventory_count_line_serial_qty"),
        CheckConstraint("line_number > 0", name="ck_inventory_count_line_number_positive"),
        ForeignKeyConstraint(
            ["tenant_id", "inventory_count_id"],
            ["counts.inventory_count.tenant_id", "counts.inventory_count.id"],
            name="fk_inventory_count_line_count",
        ),
        _fk("item_id", "item_master", "item"),
        _fk("uom_id", "item_master", "uom"),
        _fk("location_id", "traceability", "location"),
        _fk("bin_id", "traceability", "bin", nullable=True),
        _fk("inventory_status_id", "traceability", "inventory_status"),
    ],
    indexes=[("count_status", "inventory_count_id", "count_status"), ("item_location", "item_id", "location_id")],
)

inventory_count_snapshot = Table(
    "inventory_count_snapshot",
    metadata,
    Column("id", UUID_T, nullable=False),
    Column("tenant_id", UUID_T, nullable=False),
    Column("inventory_count_id", UUID_T, nullable=False),
    Column("item_id", UUID_T, nullable=False),
    Column("location_id", UUID_T, nullable=False),
    Column("bin_id", UUID_T, nullable=True),
    Column("lot_id", UUID_T, nullable=True),
    Column("serial_id", UUID_T, nullable=True),
    Column("inventory_status_id", UUID_T, nullable=False),
    Column("ownership", Text, nullable=False, server_default=text("'OWNED'")),
    Column("snapshot_qty", QTY, nullable=False),
    Column("snapshot_value_base", MONEY, nullable=False, server_default=text("0")),
    Column("snapshot_ledger_sequence", Integer, nullable=True),
    Column("snapshot_at", STAMP, nullable=False, server_default=text("now()")),
    Column("recalculated_from_id", UUID_T, nullable=True),
    Column("created_at", STAMP, nullable=False, server_default=text("now()")),
    PrimaryKeyConstraint("id", name="pk_inventory_count_snapshot"),
    UniqueConstraint("tenant_id", "id", name="uq_inventory_count_snapshot_tenant_id_id"),
    UniqueConstraint(
        "tenant_id", "inventory_count_id", "item_id", "location_id", "bin_id", "lot_id", "serial_id",
        "inventory_status_id", "ownership", "snapshot_at",
        name="uq_inventory_count_snapshot_position", postgresql_nulls_not_distinct=True,
    ),
    CheckConstraint(f"ownership IN {OWNERSHIPS!r}".replace("'", "'"), name="ck_inventory_count_snapshot_ownership"),
    _tenant_fk("inventory_count_snapshot"),
    ForeignKeyConstraint(
        ["tenant_id", "inventory_count_id"],
        ["counts.inventory_count.tenant_id", "counts.inventory_count.id"],
        name="fk_inventory_count_snapshot_count",
    ),
    ForeignKeyConstraint(
        ["tenant_id", "recalculated_from_id"],
        ["counts.inventory_count_snapshot.tenant_id", "counts.inventory_count_snapshot.id"],
        name="fk_inventory_count_snapshot_recalc",
    ),
    _fk("item_id", "item_master", "item"),
    _fk("location_id", "traceability", "location"),
    _fk("inventory_status_id", "traceability", "inventory_status"),
)
Index("ix_inventory_count_snapshot_count", inventory_count_snapshot.c.tenant_id, inventory_count_snapshot.c.inventory_count_id)
Index("ix_inventory_count_snapshot_item", inventory_count_snapshot.c.tenant_id, inventory_count_snapshot.c.item_id)

inventory_count_recount = mk(
    "inventory_count_recount",
    [
        Column("inventory_count_line_id", UUID_T, nullable=False),
        Column("attempt", Integer, nullable=False),
        Column("counted_qty", QTY, nullable=False),
        Column("counted_by", UUID_T, nullable=True),
        Column("counted_at", STAMP, nullable=False, server_default=text("now()")),
        Column("variance_qty", QTY, nullable=True),
        Column("notes", Text, nullable=True),
    ],
    extra=[
        UniqueConstraint("tenant_id", "inventory_count_line_id", "attempt", name="uq_inventory_count_recount_attempt"),
        CheckConstraint("attempt > 0", name="ck_inventory_count_recount_attempt"),
        CheckConstraint("counted_qty >= 0", name="ck_inventory_count_recount_qty"),
        ForeignKeyConstraint(
            ["tenant_id", "inventory_count_line_id"],
            ["counts.inventory_count_line.tenant_id", "counts.inventory_count_line.id"],
            name="fk_inventory_count_recount_line",
        ),
    ],
    indexes=[("line", "inventory_count_line_id")],
)

REVISION_0014_TABLES: tuple[Table, ...] = (
    count_plan,
    count_plan_filter,
    inventory_count,
    inventory_count_line,
    inventory_count_snapshot,
    inventory_count_recount,
)

ALL_TABLES = REVISION_0014_TABLES
assert len(ALL_TABLES) == 6
