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
    snapshot_revision: int | None = None
    count_id: UUID | None = None
    count_number: str | None = None
    count_type: str | None = None
    count_date: date | None = None
    count_round: int | None = None
