"""Shared FastAPI boundary models."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class APIModel(BaseModel):
    """Base for response DTOs; unknown service fields are ignored by the boundary contract."""

    model_config = ConfigDict(extra="ignore", from_attributes=True, populate_by_name=True)


class Problem(APIModel):
    type: str | None = None
    title: str | None = None
    status: int
    code: str
    message: str
    details: list[dict] | None = None
    correlation_id: str | None = None
    trace_id: str | None = None


class Page[T](APIModel):
    items: list[T]
    count: int | None = None
    next_cursor: str | None = None


class ActionResult(APIModel):
    model_config = ConfigDict(extra="allow", from_attributes=True, populate_by_name=True)

    id: str | None = None
    status: str | None = None
    document_number: str | None = None
    posted_transaction_id: str | None = None
    reversal_transaction_id: str | None = None
    approval_request_id: str | None = None
    transaction_id: str | None = None
    message: str | None = None
    ledger_sequence: int | None = None
    posting_at: datetime | None = None
    transaction_type: str | None = None
    source_doc_type: str | None = None
    source_doc_id: str | None = None
    total_qty_base: Decimal | None = None
    total_value_base: Decimal | None = None
    value_only: bool | None = None
    event_ids: list[str] = Field(default_factory=list)
    warnings: list[dict] = Field(default_factory=list)
    replayed: bool | None = None
    commitment_id: str | None = None
    qty_committed: Decimal | None = None
    qty_backordered: Decimal | None = None
    released_qty: Decimal | None = None
    auto_posted: bool | None = None
    tolerance: dict | None = None
    approval_definition_id: str | None = None


class HealthOut(APIModel):
    status: str
    version: str | None = None
    env: str | None = None


class ReadyOut(APIModel):
    status: str
    checks: dict


PROBLEM_RESPONSES: dict[int | str, dict] = {
    code: {
        "description": f"Error {code}",
        "model": Problem,
        "content": {
            "application/problem+json": {
                "schema": {"$ref": "#/components/schemas/Problem"},
            }
        },
    }
    for code in (400, 401, 403, 404, 409, 412, 422, 500, 503)
}
