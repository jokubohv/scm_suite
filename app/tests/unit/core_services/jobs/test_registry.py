"""Registry coverage: every seeded ``platform.job_definition`` name resolves (DB cross-check)."""

from __future__ import annotations

import inspect

import pytest
from sqlalchemy import select

from app.core.jobs import (
    JOB_HANDLERS,
    JOB_SPECS,
    SEEDED_JOB_NAMES,
    JobKind,
    NoopJobError,
    UnknownJobError,
    coverage_summary,
    get_spec,
    iter_specs,
    list_jobs,
    resolve_handler,
)
from app.core.models import platform as p

pytestmark = pytest.mark.integration


async def test_registry_covers_every_live_seeded_job_name(db_session) -> None:
    table = p.metadata.tables["platform.job_definition"]
    rows = (await db_session.execute(select(table.c.name).distinct())).scalars().all()
    live = set(rows)
    assert len(live) == 57, f"unexpected seeded job count {len(live)}"
    assert live == set(SEEDED_JOB_NAMES)
    assert live == set(JOB_SPECS)
    assert len(JOB_HANDLERS) == 38
    summary = coverage_summary()
    assert summary["covered"] is True
    assert summary["missing"] == [] and summary["unseeded_extra"] == []
    assert summary["by_kind"] == {"sweep": 30, "command": 8, "noop": 19}
    assert summary["implemented"] == 38 and summary["noop"] == 19


def test_specs_are_well_formed() -> None:
    names = [spec.name for spec in iter_specs()]
    assert len(names) == len(set(names)) == 57
    for spec in JOB_SPECS.values():
        assert spec.name == spec.name.strip()
        assert isinstance(spec.description, str) and spec.description
        assert spec.feature_set.startswith("FS-")
        if spec.kind is JobKind.NOOP:
            assert spec.handler is None
            assert spec.noop_reason, spec.name
            assert not spec.implemented
        else:
            assert spec.handler is not None and inspect.iscoroutinefunction(spec.handler), spec.name
            assert spec.implemented
        if spec.kind is JobKind.COMMAND:
            assert spec.payload_keys, spec.name
    assert len({spec.feature_set for spec in JOB_SPECS.values()}) == 14


def test_resolution_and_unknown_job_errors() -> None:
    handler = resolve_handler("reporting.refresh_views")
    assert inspect.iscoroutinefunction(handler)
    assert get_spec("reporting.refresh_views").feature_set == "FS-15"
    with pytest.raises(UnknownJobError) as unknown:
        resolve_handler("does.not.exist")
    assert unknown.value.job_name == "does.not.exist"
    with pytest.raises(UnknownJobError):
        get_spec("platform.jobs.scheduler.missing")
    with pytest.raises(NoopJobError) as noop:
        resolve_handler("quality.trigger.evaluate")
    assert "event-driven consumer" in str(noop.value)


def test_listing_and_implemented_filter() -> None:
    all_jobs = list_jobs()
    implemented = list_jobs(implemented_only=True)
    assert len(all_jobs) == 57 and len(implemented) == 38
    assert {row["name"] for row in implemented} == set(JOB_HANDLERS)
    assert all(row["implemented"] for row in implemented)
    noop = [row for row in all_jobs if row["kind"] == "noop"]
    assert len(noop) == 19 and all(row["noop_reason"] for row in noop)
