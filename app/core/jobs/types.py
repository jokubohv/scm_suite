"""Shared types for the central job-handler registry (no imports from feature modules)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

__all__ = ["JobContext", "JobHandler", "JobKind", "JobSpec", "UnknownJobError"]


class JobKind(StrEnum):
    """How a seeded job is executed."""

    SWEEP = "sweep"  # tenant-wide idempotent scan/reconcile (safe to re-run)
    COMMAND = "command"  # payload/document-driven; skips cleanly when the payload is absent
    NOOP = "noop"  # seeded but intentionally a documented idempotent no-op


class UnknownJobError(LookupError):
    """Raised when a job name cannot be resolved to a registered handler."""

    code = "UNKNOWN_JOB"

    def __init__(self, job_name: str) -> None:
        super().__init__(f"no handler registered for job {job_name!r}")
        self.job_name = job_name


@dataclass(frozen=True, slots=True)
class JobContext:
    """Execution context passed to every handler (one tenant-scoped DB transaction)."""

    session: AsyncSession
    job_name: str
    tenant_id: str
    payload: Mapping[str, Any] = field(default_factory=dict)
    run_key: str = ""
    actor_id: str | None = None
    attempt: int = 1

    def get(self, key: str, default: Any = None) -> Any:
        return self.payload.get(key, default)


JobHandler = Callable[[JobContext], Awaitable[dict[str, Any]]]


@dataclass(frozen=True, slots=True)
class JobSpec:
    """Registry entry for one seeded ``platform.job_definition.name``."""

    name: str
    handler: JobHandler | None
    kind: JobKind
    description: str
    feature_set: str
    payload_keys: tuple[str, ...] = ()
    noop_reason: str | None = None

    def __post_init__(self) -> None:
        if self.kind is not JobKind.NOOP and self.handler is None:
            raise ValueError(f"job {self.name!r}: non-noop spec requires a handler")
        if self.kind is JobKind.NOOP and not self.noop_reason:
            raise ValueError(f"job {self.name!r}: no-op spec requires a documented reason")
        if self.kind is JobKind.COMMAND and not self.payload_keys:
            raise ValueError(f"job {self.name!r}: command spec requires payload_keys")

    @property
    def implemented(self) -> bool:
        return self.kind is not JobKind.NOOP
