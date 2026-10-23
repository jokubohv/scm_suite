"""Central resolver for seeded job names → handlers.

The registry is the single source of truth used by the worker (`app/worker.py`), tests and any future
scheduler wiring. Every name in ``platform.job_definition`` (migrations 0003–0030) resolves to either an
implemented handler or a ``JobSpec`` marked as a documented idempotent no-op.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from app.core.jobs.handlers import JOB_SPECS
from app.core.jobs.seeded import SEEDED_JOB_NAMES
from app.core.jobs.types import JobHandler, JobKind, JobSpec, UnknownJobError

__all__ = [
    "JOB_HANDLERS",
    "JOB_SPECS",
    "NoopJobError",
    "SEEDED_JOB_NAMES",
    "coverage_summary",
    "get_spec",
    "iter_specs",
    "list_jobs",
    "resolve_handler",
]


class NoopJobError(LookupError):
    """Raised when a caller asks for a handler of a documented no-op job."""

    code = "NOOP_JOB"

    def __init__(self, job_name: str, reason: str | None = None) -> None:
        super().__init__(f"job {job_name!r} is a documented no-op: {reason}")
        self.job_name = job_name
        self.reason = reason


JOB_HANDLERS: dict[str, JobHandler] = {
    name: spec.handler for name, spec in JOB_SPECS.items() if spec.handler is not None
}


def get_spec(job_name: str) -> JobSpec:
    """Return the registry spec for a seeded job name (raises ``UnknownJobError``)."""
    spec = JOB_SPECS.get(job_name)
    if spec is None:
        raise UnknownJobError(job_name)
    return spec


def resolve_handler(job_name: str) -> JobHandler:
    """Resolve an implemented handler by seeded job name.

    Raises ``UnknownJobError`` for names that are not seeded/registered and ``NoopJobError`` for
    seeded-but-deliberately-unimplemented jobs (the worker returns the documented no-op envelope).
    """
    spec = get_spec(job_name)
    if spec.handler is None:
        raise NoopJobError(job_name, spec.noop_reason)
    return spec.handler


def iter_specs() -> Iterator[JobSpec]:
    return iter(JOB_SPECS.values())


def list_jobs(*, implemented_only: bool = False) -> list[dict[str, Any]]:
    rows = []
    for spec in JOB_SPECS.values():
        if implemented_only and not spec.implemented:
            continue
        rows.append(
            {
                "name": spec.name,
                "kind": spec.kind.value,
                "feature_set": spec.feature_set,
                "implemented": spec.implemented,
                "payload_keys": list(spec.payload_keys),
                "noop_reason": spec.noop_reason,
                "description": spec.description,
            }
        )
    return sorted(rows, key=lambda row: row["name"])


def coverage_summary() -> dict[str, Any]:
    """Handler coverage counts (also consumed by residual-task reports/tests)."""
    specs = list(JOB_SPECS.values())
    by_kind = {kind.value: sum(1 for spec in specs if spec.kind is kind) for kind in JobKind}
    noops = sorted(spec.name for spec in specs if spec.kind is JobKind.NOOP)
    missing = sorted(SEEDED_JOB_NAMES - JOB_SPECS.keys())
    unseeded = sorted(JOB_SPECS.keys() - SEEDED_JOB_NAMES)
    return {
        "seeded": len(SEEDED_JOB_NAMES),
        "registered": len(JOB_SPECS),
        "implemented": sum(1 for spec in specs if spec.implemented),
        "handlers": len(JOB_HANDLERS),
        "by_kind": by_kind,
        "noop": len(noops),
        "noop_jobs": noops,
        "missing": missing,
        "unseeded_extra": unseeded,
        "covered": not missing,
    }
