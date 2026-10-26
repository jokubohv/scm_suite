"""Shared FastAPI boundary models."""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict


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
