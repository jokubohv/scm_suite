"""Inventory-count boundary DTOs."""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from pydantic import Field

from app.api.schemas.common import APIModel, Page


class CountScopeOut(APIModel):
    abc: list[str] = Field(default_factory=list)
    bins: list[UUID] = Field(default_factory=list)
    items: list[UUID] = Field(default_factory=list)
    statuses: list[UUID] = Field(default_factory=list)
    locations: list[UUID] = Field(default_factory=list)
    expected_lines: int | None = None


class CountLineOut(APIModel):
    id: UUID
    inventory_count_id: UUID | None = None
    line_number: int | None = None
    item_id: UUID | None = None
    location_id: UUID | None = None
    bin_id: UUID | None = None
    lot_id: UUID | None = None
    serial_id: UUID | None = None
    inventory_status_id: UUID | None = None
    ownership: str | None = None
    snapshot_qty: Decimal | None = None
    counted_qty: Decimal | None = None
    variance_qty: Decimal | None = None
    count_status: str | None = None
    counted_at: datetime | None = None
    notes: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    version: int | None = None


class CountOut(APIModel):
    id: UUID
    tenant_id: UUID | None = None
    count_number: str
    count_plan_id: UUID | None = None
    count_type: str
    status: str
    count_date: date | None = None
    snapshot_at: datetime | None = None
    blind_count: bool | None = None
    approval_required: bool | None = None
    variance_account_ref: str | None = None
    recalc_snapshot_on_reject: bool | None = None
    approved_at: datetime | None = None
    approved_by: UUID | None = None
    posted_transaction_id: UUID | None = None
    count_round: int | None = None
    scope: CountScopeOut | None = None
    reason: str | None = None
    line_count: int | None = None
    approval_request_id: UUID | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    created_by: UUID | None = None
    updated_by: UUID | None = None
    version: int | None = None
    lines: list[CountLineOut] = Field(default_factory=list)


class CountPage(Page[CountOut]):
    pass


class CountCreateOut(APIModel):
    count_id: UUID
    count_number: str
    status: str
    count_type: str
    count_date: date | None = None
    count_round: int | None = None


class CountActionOut(APIModel):
    status: str | None = None
    snapshot_at: datetime | None = None
    line_count: int | None = None
    approval_request_id: UUID | None = None
    approval_status: str | None = None
    approval_pending: bool | None = None
    posted_transaction_id: UUID | None = None
    transaction_id: UUID | None = None
    accepted: list[dict] | None = None
    rejected: list[dict] | None = None
    blind: bool | None = None
    recounts: list[dict] | None = None
    approval_mode: str | None = None
    tolerance: dict | None = None
    approval_definition_id: UUID | None = None
    auto_posted: bool | None = None
    snapshot_revision: int | None = None
    count_id: UUID | None = None
    count_number: str | None = None
    count_type: str | None = None
    count_date: date | None = None
    count_round: int | None = None


class CountPlanFilterOut(APIModel):
    id: UUID | None = None
    filter_type: str | None = None
    filter_operator: str | None = None
    value_text: str | None = None
    value_uuid: UUID | None = None
    sort_order: int | None = None


class CountPlanOut(APIModel):
    id: UUID
    tenant_id: UUID | None = None
    code: str
    name: str
    frequency: str
    next_run_date: date | None = None
    next_count_date: date | None = None
    last_run_at: datetime | None = None
    status: str
    blind_count: bool | None = None
    approval_required: bool | None = None
    include_zero_qty: bool | None = None
    notes: str | None = None
    filters: list[CountPlanFilterOut] = Field(default_factory=list)
    created_at: datetime | None = None
    updated_at: datetime | None = None
    created_by: UUID | None = None
    updated_by: UUID | None = None
    version: int | None = None


class CountPlanPage(Page[CountPlanOut]):
    pass


class CountLinePage(Page[CountLineOut]):
    blind: bool | None = None
    hidden: bool | None = None


class CountSnapshotOut(APIModel):
    id: UUID | None = None
    count_id: UUID | None = None
    count_number: str | None = None
    revision: int | None = None
    snapshot_at: datetime | None = None
    status: str | None = None
    scope: CountScopeOut | None = None
    lines: list[CountLineOut] = Field(default_factory=list)
    items: list[dict] = Field(default_factory=list)
    count: int | None = None
    revision_count: int | None = None


class CountVarianceOut(APIModel):
    count_id: UUID | None = None
    count_number: str | None = None
    items: list[dict] = Field(default_factory=list)
    lines: list[CountLineOut] = Field(default_factory=list)
    totals: dict | None = None
    blind: bool | None = None
    total_variance_qty: Decimal | None = None
    total_variance_value: Decimal | None = None
    tolerance: dict | None = None
    approval_required: bool | None = None


class CountNextOut(APIModel):
    count_id: UUID | None = None
    count_number: str | None = None
    count_date: date | None = None
    location_id: UUID | None = None
    item_id: UUID | None = None
    line_count: int | None = None
    exists: bool | None = None
    interval_days: int | None = None
    next_count_date: date | None = None
    next_count_at: datetime | None = None
    frequency: str | None = None
