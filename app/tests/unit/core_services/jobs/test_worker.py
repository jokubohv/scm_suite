"""Worker resolution/envelope + representative seeded-job invocations (live DB, rollback-isolated)."""

from __future__ import annotations

from contextlib import asynccontextmanager
from uuid import uuid4

import pytest
from sqlalchemy import insert

from app.core.jobs import UnknownJobError
from app.core.jobs.types import JobKind
from app.core.models import platform as p
from app.worker import JobWorker

pytestmark = pytest.mark.integration


def _factory(session):
    @asynccontextmanager
    async def factory():
        yield session

    return factory


async def _seed_tenant(db_session, *, counts_enabled: bool = True):
    tenant_id = uuid4()
    await db_session.execute(
        insert(p.tenant).values(
            id=tenant_id,
            tenant_id=tenant_id,
            code=f"JOB{tenant_id.hex[:6].upper()}",
            name="Job test tenant",
            timezone="UTC",
            status="ACTIVE",
        )
    )
    for flag_key, enabled in (
        ("inventory.counts.enabled", counts_enabled),
        ("traceability.registry.enabled", True),
    ):
        await db_session.execute(
            insert(p.feature_flag).values(
                id=uuid4(),
                tenant_id=tenant_id,
                flag_key=flag_key,
                is_enabled=enabled,
                source="MIGRATION",
            )
        )
    await db_session.flush()
    return tenant_id


async def test_worker_rejects_unknown_job() -> None:
    worker = JobWorker()
    with pytest.raises(UnknownJobError):
        await worker.run_job("totally.unknown.job", tenant_id=str(uuid4()))


async def test_worker_returns_documented_noop_without_transaction() -> None:
    worker = JobWorker()
    envelope = await worker.run_job("platform.outbox.relay", tenant_id=str(uuid4()), payload={"concurrent": True})
    assert envelope["status"] == "noop"
    assert envelope["attempts"] == 0
    assert envelope["kind"] == JobKind.NOOP.value
    assert "dedicated relay" in envelope["result"]["reason"]


async def test_command_job_without_payload_skips_cleanly(db_session) -> None:
    worker = JobWorker(session_factory=_factory(db_session))
    envelope = await worker.run_job(
        "counts.snapshot.refresh", tenant_id=str(await _seed_tenant(db_session)), use_definition=False
    )
    assert envelope["status"] == "skipped"
    assert "count_id" in envelope["result"]["reason"]


async def test_counts_generate_cycle_counts_sweep(db_session) -> None:
    tenant_id = await _seed_tenant(db_session)
    worker = JobWorker(session_factory=_factory(db_session))
    envelope = await worker.run_job(
        "counts.generate_cycle_counts", tenant_id=str(tenant_id), payload={"run_date": "2026-10-01"}
    )
    assert envelope["status"] == "succeeded", envelope
    assert envelope["job"] == "counts.generate_cycle_counts"
    assert envelope["attempts"] == 1
    assert envelope["result"]["run_date"] == "2026-10-01"
    assert envelope["result"]["plans"] >= 0
    assert isinstance(envelope["result"]["generated"], list)


async def test_reporting_refresh_views_sweep(db_session) -> None:
    tenant_id = await _seed_tenant(db_session)
    worker = JobWorker(session_factory=_factory(db_session))
    envelope = await worker.run_job("reporting.refresh_views", tenant_id=str(tenant_id), payload={"concurrent": False})
    assert envelope["status"] == "succeeded", envelope
    assert envelope["result"]["refreshed"] >= 1
    assert {row["view_name"] for row in envelope["result"]["views"]}


async def test_ledger_and_quality_reconcile_sweeps_are_read_only(db_session) -> None:
    tenant_id = await _seed_tenant(db_session)
    worker = JobWorker(session_factory=_factory(db_session))
    ledger = await worker.run_job("ledger.reconcile_balances", tenant_id=str(tenant_id))
    assert ledger["status"] == "succeeded", ledger
    assert {"balances", "negative_balances", "availability_mismatch", "detail_balance_mismatch"} <= set(
        ledger["result"]
    )
    quality = await worker.run_job("quality.hold.reconcile", tenant_id=str(tenant_id))
    assert quality["status"] == "succeeded", quality
    assert quality["result"]["hold_links"] == 0
    assert quality["result"]["consistent"] is True


async def test_traceability_sentinel_and_expiry_scan_sweeps(db_session) -> None:
    tenant_id = await _seed_tenant(db_session)
    worker = JobWorker(session_factory=_factory(db_session))
    sentinel = await worker.run_job("traceability.bootstrap.sentinel", tenant_id=str(tenant_id))
    assert sentinel["status"] == "succeeded", sentinel
    assert isinstance(sentinel["result"], dict)
    expiry = await worker.run_job("traceability.expiry.scan", tenant_id=str(tenant_id))
    assert expiry["status"] == "succeeded", expiry
    assert expiry["result"]["warnings"] == 0
    deferred = await worker.run_job("traceability.deferred_fks.validate", tenant_id=str(tenant_id))
    assert deferred["status"] == "succeeded", deferred
    assert deferred["result"]["validated"] is True


async def test_worker_result_is_json_serializable_and_definition_aware(db_session) -> None:
    import json

    tenant_id = await _seed_tenant(db_session)
    worker = JobWorker(session_factory=_factory(db_session))
    envelope = await worker.run_job("reporting.reconcile_readmodels", tenant_id=str(tenant_id))
    assert envelope["status"] == "succeeded", envelope
    json.dumps(envelope)  # must not raise
    assert envelope["attempts"] == 1
    definitions = await worker._load_definition("reporting.refresh_views")
    assert definitions is None or definitions["name"] == "reporting.refresh_views"


def test_list_jobs_has_seeded_definition_shape() -> None:
    rows = JobWorker.list_jobs()
    assert len(rows) == 57
    assert all({"name", "kind", "feature_set", "implemented"} <= set(row) for row in rows)
