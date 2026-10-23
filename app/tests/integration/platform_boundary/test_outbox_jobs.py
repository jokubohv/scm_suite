"""Live-DB tests for internal outbox replay + job-run operations (task-53)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from app.api.deps import get_current_principal
from app.core.models import platform as p

pytestmark = pytest.mark.integration

TENANT_ID = UUID("9b000000-0000-7000-8000-000000000001")
OTHER_TENANT_ID = UUID("9b000000-0000-7000-8000-000000000002")
JOB_DEFINITION_ID = UUID("9b000000-0000-7000-8000-000000000010")
OTHER_JOB_DEFINITION_ID = UUID("9b000000-0000-7000-8000-000000000012")

REPLAY = "/api/v1/internal/outbox/replay"
JOBS_RUN = "/api/v1/admin/jobs/{name}/run"
JOB_RUNS = "/api/v1/admin/job-runs"


async def _seed_event(
    db_session: Any,
    *,
    tenant_id: UUID,
    event_id: UUID | None = None,
    published_at: Any = None,
) -> dict[str, UUID]:
    event_id = event_id or uuid4()
    aggregate_id = uuid4()
    now = datetime.now(UTC)
    await db_session.execute(
        p.outbox_event.insert().values(
            id=uuid4(),
            tenant_id=tenant_id,
            event_id=event_id,
            aggregate_type="inventory_transaction",
            aggregate_id=aggregate_id,
            aggregate_version=1,
            event_type="inventory.adjusted",
            event_version="v1",
            payload={"test": True},
            occurred_at=now,
            correlation_id=None,
            causation_id=None,
            published_at=published_at,
            attempts=0,
            last_error=None,
        )
    )
    await db_session.flush()
    return {"event_id": event_id, "aggregate_id": aggregate_id}


async def _published_at(db_session: Any, tenant_id: UUID, event_id: UUID) -> Any:
    result = await db_session.execute(
        select(p.outbox_event.c.published_at).where(
            p.outbox_event.c.tenant_id == tenant_id, p.outbox_event.c.event_id == event_id
        )
    )
    return result.scalar_one()


async def test_outbox_replay_dry_run_and_actual(api_client: Any, db_session: Any, seeded: Any) -> None:
    live = await _seed_event(db_session, tenant_id=TENANT_ID, published_at=datetime.now(UTC))
    unpublished = await _seed_event(db_session, tenant_id=TENANT_ID)

    dry = await api_client.post(
        REPLAY,
        json={"event_ids": [str(live["event_id"])], "dry_run": True},
        headers={"Idempotency-Key": uuid4().hex},
    )
    assert dry.status_code == 200, dry.text
    body = dry.json()
    assert body["dry_run"] is True
    assert body["would_replay"] == 1
    assert body["replayed"] == 0
    assert body["skipped"] == 0
    assert await _published_at(db_session, TENANT_ID, live["event_id"]) is not None  # dry run wrote nothing

    replay = await api_client.post(
        REPLAY,
        json={"event_ids": [str(live["event_id"]), str(unpublished["event_id"])]},
        headers={"Idempotency-Key": uuid4().hex},
    )
    assert replay.status_code == 200
    assert replay.json()["replayed"] == 2
    # outbox_event is append-only: replay is a relay handoff, not an UPDATE of published_at.
    assert await _published_at(db_session, TENANT_ID, live["event_id"]) is not None
    audit = await db_session.execute(
        select(p.audit_event.c.action).where(
            p.audit_event.c.tenant_id == TENANT_ID,
            p.audit_event.c.action == "platform.outbox.replayed",
        )
    )
    assert "platform.outbox.replayed" in list(audit.scalars().all())

    missing = await api_client.post(
        REPLAY,
        json={"event_ids": [str(uuid4())]},
        headers={"Idempotency-Key": uuid4().hex},
    )
    assert missing.status_code == 200
    assert missing.json()["replayed"] == 0
    assert missing.json()["skipped"] == 1

    no_selector = await api_client.post(REPLAY, json={}, headers={"Idempotency-Key": uuid4().hex})
    assert no_selector.status_code == 422

    missing_key = await api_client.post(REPLAY, json={"event_ids": [str(uuid4())]})
    assert missing_key.status_code == 422

    malformed_ids = await api_client.post(
        REPLAY, json={"event_ids": ["not-a-uuid"]}, headers={"Idempotency-Key": uuid4().hex}
    )
    assert malformed_ids.status_code == 422


async def test_outbox_replay_tenant_isolation_and_idempotent_replay(
    api_client: Any, db_session: Any, seeded: Any
) -> None:
    other = await _seed_event(db_session, tenant_id=OTHER_TENANT_ID, published_at=datetime.now(UTC))
    key = uuid4().hex
    window = {
        "from_occurred_at": (datetime.now(UTC) - timedelta(minutes=5)).isoformat(),
        "to_occurred_at": (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
    }
    first = await api_client.post(REPLAY, json=window, headers={"Idempotency-Key": key})
    assert first.status_code == 200
    assert first.json()["replayed"] == 0  # other tenant's event is invisible
    assert await _published_at(db_session, OTHER_TENANT_ID, other["event_id"]) is not None

    second = await api_client.post(REPLAY, json=window, headers={"Idempotency-Key": key})
    assert second.status_code == 200
    assert second.json() == first.json()


async def _seed_other_tenant_job_data(db_session: Any) -> dict[str, UUID]:
    await db_session.execute(
        p.job_definition.insert().values(
            id=OTHER_JOB_DEFINITION_ID,
            tenant_id=OTHER_TENANT_ID,
            name="other.tenant.job",
            queue="default",
            handler="tests.noop",
            timeout_seconds=60,
            max_attempts=3,
            singleton=True,
            is_enabled=True,
            version=1,
        )
    )
    run_id = uuid4()
    now = datetime.now(UTC)
    await db_session.execute(
        p.job_run.insert().values(
            id=run_id,
            tenant_id=OTHER_TENANT_ID,
            job_definition_id=OTHER_JOB_DEFINITION_ID,
            run_key=f"other-{uuid4().hex}",
            status="QUEUED",
            attempt=1,
            created_at=now,
            updated_at=now,
            version=1,
        )
    )
    await db_session.flush()
    return {"run_id": run_id}


async def test_job_run_enqueue_dedupe_and_validation(api_client: Any, db_session: Any, seeded: Any) -> None:
    key = uuid4().hex
    first = await api_client.post(
        JOBS_RUN.format(name="ledger.reconcile_balances"),
        json={"params": {"scope": "balances"}},
        headers={"Idempotency-Key": key},
    )
    assert first.status_code == 202, first.text
    body = first.json()
    assert body["status"] == "QUEUED"
    assert body["run"]["status"] == "QUEUED"
    assert body["run"]["deduplicated"] is False
    run_id = body["job_run_id"]

    second = await api_client.post(
        JOBS_RUN.format(name="ledger.reconcile_balances"),
        json={"params": {"scope": "balances"}},
        headers={"Idempotency-Key": key},
    )
    assert second.status_code == 202
    assert second.json()["job_run_id"] == run_id
    assert second.json()["run"]["deduplicated"] is True

    missing_key = await api_client.post(JOBS_RUN.format(name="ledger.reconcile_balances"), json={})
    assert missing_key.status_code == 422

    disabled = await api_client.post(
        JOBS_RUN.format(name="disabled.job"), json={}, headers={"Idempotency-Key": uuid4().hex}
    )
    assert disabled.status_code == 409
    assert disabled.json()["code"] == "CONFLICT"

    unknown = await api_client.post(
        JOBS_RUN.format(name="missing.job"), json={}, headers={"Idempotency-Key": uuid4().hex}
    )
    assert unknown.status_code == 404


async def test_job_run_list_detail_cancel_and_tenant_isolation(api_client: Any, db_session: Any, seeded: Any) -> None:
    other = await _seed_other_tenant_job_data(db_session)
    created = await api_client.post(
        JOBS_RUN.format(name="ledger.reconcile_balances"),
        json={"run_key": "manual-run-1"},
        headers={"Idempotency-Key": uuid4().hex},
    )
    run_id = created.json()["job_run_id"]

    listing = await api_client.get(JOB_RUNS, params={"status": "QUEUED", "limit": 50})
    assert listing.status_code == 200
    ids = [item["job_run_id"] for item in listing.json()["items"]]
    assert run_id in ids
    assert str(other["run_id"]) not in ids

    filtered = await api_client.get(JOB_RUNS, params={"job_definition_id": str(JOB_DEFINITION_ID)})
    assert filtered.status_code == 200
    assert all(item["job_definition_id"] == str(JOB_DEFINITION_ID) for item in filtered.json()["items"])

    detail = await api_client.get(f"{JOB_RUNS}/{run_id}")
    assert detail.status_code == 200
    assert detail.json()["run"]["run_key"] == "manual-run-1"

    other_detail = await api_client.get(f"{JOB_RUNS}/{other['run_id']}")
    assert other_detail.status_code == 404

    bad_filter = await api_client.get(JOB_RUNS, params={"status": "BOGUS"})
    assert bad_filter.status_code == 422

    cancel_key = uuid4().hex
    cancelled = await api_client.post(
        f"{JOB_RUNS}/{run_id}/cancel",
        json={"reason": "operator requested"},
        headers={"Idempotency-Key": cancel_key},
    )
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["status"] == "CANCELLED"
    assert cancelled.json()["run"]["result_summary"] == {"cancel_reason": "operator requested"}

    replay = await api_client.post(
        f"{JOB_RUNS}/{run_id}/cancel",
        json={"reason": "operator requested"},
        headers={"Idempotency-Key": cancel_key},
    )
    assert replay.status_code == 200
    assert replay.json() == cancelled.json()

    conflict = await api_client.post(
        f"{JOB_RUNS}/{run_id}/cancel",
        json={"reason": "operator requested"},
        headers={"Idempotency-Key": uuid4().hex},
    )
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "CONFLICT"

    unknown = await api_client.post(
        f"{JOB_RUNS}/{uuid4()}/cancel",
        json={"reason": "operator requested"},
        headers={"Idempotency-Key": uuid4().hex},
    )
    assert unknown.status_code == 404


async def test_job_scope_denied(api_client: Any, admin_app: Any, limited_principal: Any) -> None:
    admin_app.dependency_overrides[get_current_principal] = lambda: limited_principal
    denied_run = await api_client.post(
        JOBS_RUN.format(name="ledger.reconcile_balances"),
        json={},
        headers={"Idempotency-Key": uuid4().hex},
    )
    assert denied_run.status_code == 403
    assert denied_run.json()["code"] == "FORBIDDEN_SCOPE"
    denied_list = await api_client.get(JOB_RUNS)
    assert denied_list.status_code == 403
    assert denied_list.json()["code"] == "FORBIDDEN_SCOPE"
