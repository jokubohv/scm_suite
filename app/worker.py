"""Job worker entrypoint: resolve seeded job names from ``app.core.jobs`` and execute them.

The worker owns the execution envelope that feature-set services do not: one tenant-scoped DB
transaction per attempt, ``app.tenant_id`` for RLS, a session-level advisory lock for singleton jobs,
per-job timeout/retry from ``platform.job_definition``, and the standard result envelope. Scheduled
dispatch itself is external (RQ/cron or a future in-process scheduler with the same leader lock):
``platform.jobs.scheduler`` remains a documented no-op until that scheduler lands.

Usage::

    python -m app.worker --list
    python -m app.worker --job reporting.refresh_views --tenant <uuid> --payload '{"concurrent": false}'
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import time
from collections.abc import Callable, Mapping
from typing import Any

from sqlalchemy import text

from app.core.jobs import (
    JobContext,
    JobKind,
    JobSpec,
    UnknownJobError,
    coverage_summary,
    get_spec,
    list_jobs,
)
from app.core.jobs.types import JobHandler
from app.db.session import session_scope

__all__ = ["JobExecutionError", "JobWorker", "UnknownJobError", "main"]

log = logging.getLogger("app.worker")

SessionFactory = Callable[[], Any]
DEFAULT_TIMEOUT_SECONDS = 300.0
DEFAULT_MAX_ATTEMPTS = 3


class JobExecutionError(RuntimeError):
    """Raised after all worker attempts for a job fail."""

    code = "JOB_EXECUTION_FAILED"

    def __init__(self, job_name: str, attempts: int, cause: Exception) -> None:
        super().__init__(f"job {job_name!r} failed after {attempts} attempt(s): {cause}")
        self.job_name = job_name
        self.attempts = attempts
        self.cause = cause


class _LockNotAcquired(RuntimeError):
    pass


def _jsonable(value: Any) -> Any:
    from app.core.jobs.handlers import _jsonable as _convert

    return _convert(value)


def _is_domain_disabled(exc: Exception) -> bool:
    code = str(getattr(exc, "code", "") or "")
    if code == "DOMAIN_DISABLED" or code.endswith("_DISABLED"):
        return True
    return type(exc).__name__.endswith("Disabled")


class JobWorker:
    """Resolve and execute seeded jobs with tenant scoping, singleton locks, timeouts and retries."""

    def __init__(
        self,
        session_factory: Callable[[], Any] | None = None,
        *,
        default_timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        default_max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    ) -> None:
        self._session_factory = session_factory
        self.default_timeout_seconds = default_timeout_seconds
        self.default_max_attempts = default_max_attempts

    # -- public API ---------------------------------------------------------------------
    @staticmethod
    def list_jobs(*, implemented_only: bool = False) -> list[dict[str, Any]]:
        return list_jobs(implemented_only=implemented_only)

    @staticmethod
    def resolve(job_name: str) -> JobSpec:
        """Resolve a seeded job spec; raises ``UnknownJobError`` for unknown names."""
        return get_spec(job_name)

    async def run_job(
        self,
        job_name: str,
        *,
        tenant_id: str,
        payload: Mapping[str, Any] | None = None,
        actor_id: str | None = None,
        run_key: str | None = None,
        use_definition: bool = True,
    ) -> dict[str, Any]:
        """Execute one job and return the standard envelope.

        Unknown names raise :class:`UnknownJobError`; documented no-ops return ``status="noop"`` without
        opening a transaction; deploy-dark domain-disabled handlers return ``status="skipped"``.
        """
        spec = get_spec(job_name)
        started = time.perf_counter()
        payload = dict(payload or {})
        run_key = run_key or f"{job_name}:{tenant_id}:{int(time.time() * 1000)}"
        if spec.kind is JobKind.NOOP:
            return self._envelope(
                spec,
                started,
                tenant_id=tenant_id,
                status="noop",
                attempts=0,
                result={"reason": spec.noop_reason, "feature_set": spec.feature_set},
            )
        definition = await self._load_definition(job_name) if use_definition else None
        timeout = (
            float(definition.get("timeout_seconds") or self.default_timeout_seconds)
            if definition
            else self.default_timeout_seconds
        )
        max_attempts = (
            int(definition.get("max_attempts") or self.default_max_attempts)
            if definition
            else self.default_max_attempts
        )
        singleton = bool(definition.get("singleton", True)) if definition else True
        assert spec.handler is not None
        last_error: Exception | None = None
        for attempt in range(1, max(1, max_attempts) + 1):
            try:
                result = await self._execute_attempt(
                    spec,
                    spec.handler,
                    tenant_id=tenant_id,
                    payload=payload,
                    actor_id=actor_id,
                    run_key=run_key,
                    attempt=attempt,
                    timeout=timeout,
                    singleton=singleton,
                )
            except _LockNotAcquired:
                return self._envelope(
                    spec,
                    started,
                    tenant_id=tenant_id,
                    status="skipped",
                    attempts=attempt,
                    result={"reason": "singleton_lock_held"},
                )
            except Exception as exc:  # noqa: BLE001 - classify + retry
                if _is_domain_disabled(exc):
                    return self._envelope(
                        spec,
                        started,
                        tenant_id=tenant_id,
                        status="skipped",
                        attempts=attempt,
                        result={"reason": "domain_disabled", "error": str(exc)},
                    )
                last_error = exc
                log.warning("job.attempt_failed", extra={"job": job_name, "attempt": attempt, "error": str(exc)})
                if attempt < max_attempts:
                    await asyncio.sleep(min(0.05 * attempt, 0.5))
                continue
            mapped = _jsonable(result)
            inner_status = mapped.get("status") if isinstance(mapped, dict) else None
            status = "skipped" if inner_status == "skipped" else "succeeded"
            return self._envelope(spec, started, tenant_id=tenant_id, status=status, attempts=attempt, result=mapped)
        assert last_error is not None
        raise JobExecutionError(job_name, max_attempts, last_error) from last_error

    # -- internals ----------------------------------------------------------------------
    def _factory(self) -> Any:
        if self._session_factory is not None:
            return self._session_factory()
        return session_scope()

    async def _load_definition(self, job_name: str) -> dict[str, Any] | None:
        async with self._factory() as session:
            row = (
                (
                    await session.execute(
                        text(
                            "SELECT name, queue, cron_expr, timeout_seconds, max_attempts, singleton, is_enabled "
                            "FROM platform.job_definition WHERE name = :name ORDER BY tenant_id LIMIT 1"
                        ),
                        {"name": job_name},
                    )
                )
                .mappings()
                .first()
            )
            return dict(row) if row else None

    async def _execute_attempt(
        self,
        spec: JobSpec,
        handler: JobHandler,
        *,
        tenant_id: str,
        payload: Mapping[str, Any],
        actor_id: str | None,
        run_key: str,
        attempt: int,
        timeout: float,
        singleton: bool,
    ) -> dict[str, Any]:
        lock_key = f"job:{spec.name}"
        async with self._factory() as session:
            try:
                await session.execute(
                    text("SELECT set_config('app.tenant_id', :tenant, true)"), {"tenant": str(tenant_id)}
                )
                if actor_id:
                    await session.execute(
                        text("SELECT set_config('app.actor_id', :actor, true)"), {"actor": str(actor_id)}
                    )
                if singleton:
                    acquired = await session.scalar(
                        text("SELECT pg_try_advisory_lock(hashtext(:key))"), {"key": lock_key}
                    )
                    if not acquired:
                        raise _LockNotAcquired(lock_key)
                try:
                    ctx = JobContext(
                        session=session,
                        job_name=spec.name,
                        tenant_id=str(tenant_id),
                        payload=payload,
                        run_key=run_key,
                        actor_id=actor_id,
                        attempt=attempt,
                    )
                    return await asyncio.wait_for(handler(ctx), timeout=timeout)
                finally:
                    if singleton:
                        try:
                            await session.execute(text("SELECT pg_advisory_unlock(hashtext(:key))"), {"key": lock_key})
                        except Exception:  # noqa: BLE001 - connection teardown releases the lock anyway
                            log.warning("job.lock_release_failed", extra={"job": spec.name})
            except _LockNotAcquired:
                raise

    def _envelope(
        self,
        spec: JobSpec,
        started: float,
        *,
        tenant_id: str,
        status: str,
        attempts: int,
        result: Any,
    ) -> dict[str, Any]:
        return {
            "job": spec.name,
            "status": status,
            "tenant_id": str(tenant_id),
            "kind": spec.kind.value,
            "feature_set": spec.feature_set,
            "attempts": attempts,
            "duration_ms": round((time.perf_counter() - started) * 1000, 3),
            "result": result,
        }


# --------------------------------------------------------------------------- CLI -------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run or list seeded platform jobs via the central registry")
    parser.add_argument("--list", action="store_true", help="list registered jobs and coverage")
    parser.add_argument("--job", help="seeded job name to execute")
    parser.add_argument("--tenant", help="tenant UUID (required with --job)")
    parser.add_argument("--payload", default="{}", help="JSON payload object")
    parser.add_argument("--actor", default=None, help="optional actor UUID")
    parser.add_argument("--no-definition", action="store_true", help="ignore platform.job_definition timeouts/retries")
    args = parser.parse_args(argv)
    from app.core.logging import configure_logging

    configure_logging("INFO")
    if args.list:
        print(json.dumps({"jobs": JobWorker.list_jobs(), "coverage": coverage_summary()}, default=str))
        return 0
    if not args.job or not args.tenant:
        parser.error("--job and --tenant are required unless --list is used")
    try:
        payload = json.loads(args.payload)
        if not isinstance(payload, dict):
            raise ValueError("payload must be a JSON object")
    except ValueError as exc:
        parser.error(f"invalid --payload: {exc}")
    worker = JobWorker()
    try:
        envelope = asyncio.run(
            worker.run_job(
                args.job,
                tenant_id=args.tenant,
                payload=payload,
                actor_id=args.actor,
                use_definition=not args.no_definition,
            )
        )
    except UnknownJobError as exc:
        print(json.dumps({"job": args.job, "status": "unknown_job", "error": str(exc)}))
        return 2
    except JobExecutionError as exc:
        print(json.dumps({"job": args.job, "status": "failed", "error": str(exc), "attempts": exc.attempts}))
        return 1
    print(json.dumps(envelope, indent=2, default=str))
    return 0 if envelope.get("status") in {"succeeded", "skipped", "noop"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
