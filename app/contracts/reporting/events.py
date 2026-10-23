"""Implementation-backed FS-15 reporting event payload builders.

The reporting refresh job (:mod:`app.modules.reporting.refresh`) refreshes the materialised views and
is the producer for ``reporting.view.refreshed`` / ``reporting.refresh.failed``. These builders produce
the final outbox envelope payloads consumed by the canonical schemas in
``contracts/events/reporting.readmodels/``; the job/worker wiring owns UUIDs/timestamps/idempotency.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

E_REFRESHED = "reporting.view.refreshed"
E_FAILED = "reporting.refresh.failed"
EVENT_VERSION = "1.0.0"


def _now() -> datetime:
    return datetime.now(UTC)


def _envelope(event_type: str, tenant_id: str, view_name: str, data: dict[str, Any]) -> dict[str, Any]:
    return {
        "event_id": str(uuid4()),
        "event_type": event_type,
        "event_version": EVENT_VERSION,
        "tenant_id": tenant_id,
        "occurred_at": _now().isoformat(),
        "aggregate": {"type": "reporting", "id": view_name, "version": 1},
        "data": data,
    }


def view_refreshed_payload(*, tenant_id: str | UUID, view_name: str, duration_ms: int,
                           row_count: int, source_high_watermark: str | None = None,
                           refreshed_at: datetime | None = None) -> dict[str, Any]:
    """Build one ``reporting.view.refreshed`` envelope (spec payload keys included in ``data``)."""
    data = {
        "tenant_id": str(tenant_id),
        "view_name": view_name,
        "refreshed_at": (refreshed_at or _now()).isoformat(),
        "duration_ms": int(duration_ms),
        "row_count": int(row_count),
        "source_high_watermark": source_high_watermark,
    }
    return _envelope(E_REFRESHED, str(tenant_id), view_name, data)


def refresh_failed_payload(*, tenant_id: str | UUID, view_name: str, attempt: int, error_code: str,
                           is_retryable: bool, failed_at: datetime | None = None) -> dict[str, Any]:
    """Build one ``reporting.refresh.failed`` envelope (spec payload keys included in ``data``)."""
    data = {
        "tenant_id": str(tenant_id),
        "view_name": view_name,
        "failed_at": (failed_at or _now()).isoformat(),
        "attempt": int(attempt),
        "error_code": error_code,
        "is_retryable": bool(is_retryable),
    }
    return _envelope(E_FAILED, str(tenant_id), view_name, data)


__all__ = ["E_REFRESHED", "E_FAILED", "EVENT_VERSION", "view_refreshed_payload", "refresh_failed_payload"]
