"""Central job-handler registry for every seeded ``platform.job_definition`` (FS-00 residual).

``JOB_SPECS`` maps each seeded job name to an async handler (or a documented idempotent no-op);
``resolve_handler``/``get_spec`` are the worker's resolution surface. See ``app/worker.py`` for
execution (tenant-scoped transaction, advisory-lock singleton, timeout/retry envelope).
"""

from app.core.jobs.registry import (
    JOB_HANDLERS,
    JOB_SPECS,
    SEEDED_JOB_NAMES,
    NoopJobError,
    coverage_summary,
    get_spec,
    iter_specs,
    list_jobs,
    resolve_handler,
)
from app.core.jobs.types import JobContext, JobHandler, JobKind, JobSpec, UnknownJobError

__all__ = [
    "JOB_HANDLERS",
    "JOB_SPECS",
    "SEEDED_JOB_NAMES",
    "JobContext",
    "JobHandler",
    "JobKind",
    "JobSpec",
    "NoopJobError",
    "UnknownJobError",
    "coverage_summary",
    "get_spec",
    "iter_specs",
    "list_jobs",
    "resolve_handler",
]
