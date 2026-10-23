"""Seed CLI: ``python -m app.seed.cli --tenant <uuid> [--dry-run] [--profile auto|memory|sqlalchemy]``.

Exit codes:
* ``0`` — seed run completed and the report is valid (dry-run included).
* ``1`` — unexpected internal error (traceback suppressed unless ``--verbose``).
* ``2`` — invalid input/seed data or repository error.

Repository profiles:
* ``memory`` — hermetic in-memory repository: loads all 13 datasets. ``--state <path>`` persists it as
  JSON, making "load twice, no diff" verifiable from the shell.
* ``sqlalchemy`` — live Postgres over ``app.core.models.platform`` (8 available datasets); the 5
  datasets without owning tables yet are reported as ``deferred_datasets`` and not written.
* ``auto`` (default) — uses ``sqlalchemy`` when a database URL is configured
  (``--database-url``/``SEED_DATABASE_URL``/``DATABASE_URL``/``TEST_DATABASE_URL``) and reachable,
  otherwise falls back to ``memory`` with a warning. ``--state`` always implies ``memory``.

``--validate-only`` validates all seed sources (RBAC catalogue + all 13 dataset payloads) regardless of
repository profile. The SQLAlchemy profile commits only a valid run (rolls back dry-runs/failures).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from uuid import UUID

from app.seed.constants import DEFAULT_DATA_DIR, SYSTEM_TENANT_ID
from app.seed.loader import SeedLoader, validate_sources
from app.seed.permissions import SeedValidationError
from app.seed.report import SeedReport
from app.seed.repository import (
    MemoryRepository,
    SeedRepository,
    SeedRepositoryError,
    SqlAlchemySeedRepository,
    build_repository,
    database_url_configured,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.seed.cli",
        description="Idempotent seed/reference bootstrap (S0-07 / FS-00).",
    )
    parser.add_argument(
        "--tenant",
        required=False,
        help=(
            "Target tenant UUID, or the symbolic value 'system' for the reserved global tenant "
            f"({SYSTEM_TENANT_ID}), which loads only global/system datasets. Defaults to "
            "SEED_TENANT_ID/INV_SEED_TENANT_ID when set, otherwise system-only mode."
        ),
    )
    parser.add_argument("--dry-run", action="store_true", help="compute changes without writing them")
    parser.add_argument("--json", action="store_true", dest="as_json", help="machine-readable report on stdout")
    parser.add_argument(
        "--profile",
        choices=("auto", "memory", "sqlalchemy"),
        default="auto",
        help=(
            "repository profile (default auto: live SQLAlchemy when a DB URL is configured, else "
            "memory; memory loads all 13 datasets, sqlalchemy the 8 available + deferred report)"
        ),
    )
    parser.add_argument(
        "--database-url",
        default=None,
        help="SQLAlchemy async database URL for the sqlalchemy profile (overrides env vars)",
    )
    parser.add_argument(
        "--state",
        type=Path,
        default=None,
        help="memory-profile state file used to prove idempotency across CLI invocations",
    )
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR, help="seed data directory")
    parser.add_argument(
        "--permissions-source",
        type=Path,
        default=None,
        help="authoritative design/permissions.json (default: repo design/, then packaged fixture copy)",
    )
    parser.add_argument(
        "--report-file",
        type=Path,
        default=None,
        help="write the run report (JSON) to this path in addition to stdout",
    )
    parser.add_argument("--no-system", action="store_true", help="skip global/system datasets")
    parser.add_argument("--validate-only", action="store_true", help="validate seed sources and exit")
    parser.add_argument("--quiet", action="store_true", help="suppress the human-readable summary")
    parser.add_argument("--verbose", action="store_true", help="show tracebacks on unexpected errors")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.validate_only:
        errors = validate_sources(data_dir=args.data_dir, permissions_source=args.permissions_source)
        if args.as_json:
            print(json.dumps({"valid": not errors, "errors": errors}, indent=2))
        elif errors:
            for error in errors:
                print(f"ERROR: {error}", file=sys.stderr)
        else:
            print("seed sources valid")
        return 0 if not errors else 2

    tenant_arg = args.tenant or os.environ.get("SEED_TENANT_ID") or os.environ.get("INV_SEED_TENANT_ID")
    try:
        tenant = _parse_tenant_arg(tenant_arg)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    if tenant_arg is None and not args.quiet and not args.as_json:
        print(
            "note: no --tenant/SEED_TENANT_ID supplied; loading global/system datasets only "
            "(python -m app.seed.cli --tenant <tenant-uuid>)",
            file=sys.stderr,
        )

    try:
        repository, profile_used, deferred_datasets = _build_cli_repository(args, tenant)
    except SeedRepositoryError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    sqlalchemy_repo = isinstance(repository, SqlAlchemySeedRepository)
    try:
        if sqlalchemy_repo:
            repository.check_connection()  # type: ignore[attr-defined]
            repository.check_tenant_exists()  # type: ignore[attr-defined]
        loader = SeedLoader(
            repository,
            data_dir=args.data_dir,
            permissions_source=args.permissions_source,
            dry_run=args.dry_run,
        )
        report = loader.load(tenant, seed_system=not args.no_system)
        if sqlalchemy_repo:
            repository.finalise_report(report)  # type: ignore[attr-defined]
    except (SeedValidationError, SeedRepositoryError) as exc:
        if sqlalchemy_repo:
            _safe_rollback(repository)
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        if sqlalchemy_repo:
            _safe_rollback(repository)
        if args.verbose:
            raise
        print(f"ERROR: unexpected seed failure: {exc}", file=sys.stderr)
        return 1
    else:
        if sqlalchemy_repo:
            if report.valid and not args.dry_run:
                repository.commit()  # type: ignore[attr-defined]
            else:
                _safe_rollback(repository)
    finally:
        if sqlalchemy_repo:
            repository.close()  # type: ignore[attr-defined]

    if not args.dry_run:
        _persist_state(args, repository, report)

    repository_meta = {"profile": profile_used, "deferred_datasets": deferred_datasets}
    if args.report_file is not None:
        _write_report_file(args.report_file, report, repository_meta)

    if args.as_json:
        payload = report.to_dict()
        payload["repository"] = repository_meta
        print(json.dumps(payload, indent=2, sort_keys=True))
    elif not args.quiet:
        print(f"repository profile: {profile_used}")
        if deferred_datasets:
            print(
                "deferred datasets (not written; owning tables not migrated yet): "
                + ", ".join(deferred_datasets)
            )
        print(report.render())

    return 0 if report.valid else 2


def _parse_tenant_arg(value: str | None) -> UUID:
    """Parse a tenant argument: UUID string, or the symbolic values ``system``/``SYSTEM``."""
    if value is None:
        return SYSTEM_TENANT_ID
    text = str(value).strip()
    if text.lower() in {"system", "system_tenant", "system-tenant"}:
        return SYSTEM_TENANT_ID
    try:
        return UUID(text)
    except ValueError as exc:
        raise ValueError(
            f"invalid tenant {value!r}: expected a UUID or the symbolic value 'system'"
        ) from exc


def _build_cli_repository(
    args: argparse.Namespace, tenant: UUID
) -> tuple[SeedRepository, str, list[str]]:
    """Select/build the repository; returns ``(repository, profile_used, deferred_datasets)``."""
    requested = args.profile
    if requested == "auto":
        if args.state is not None:
            requested = "memory"
        elif args.database_url or database_url_configured():
            requested = "sqlalchemy"
        else:
            requested = "memory"

    if requested == "sqlalchemy":
        if args.state is not None:
            raise SeedRepositoryError("--state is only supported by the memory profile")
        repository = build_repository(
            "sqlalchemy", database_url=args.database_url, tenant_id=tenant
        )
        assert isinstance(repository, SqlAlchemySeedRepository)
        try:
            repository.check_connection()
        except SeedRepositoryError as exc:
            repository.close()
            if args.profile == "auto":
                print(
                    f"warning: {exc}; falling back to the in-memory seed repository",
                    file=sys.stderr,
                )
                return _build_memory_repository(args), "memory", []
            raise
        return repository, "sqlalchemy", repository.deferred_datasets

    return _build_memory_repository(args), "memory", []


def _build_memory_repository(args: argparse.Namespace) -> MemoryRepository:
    if args.state is not None and args.state.is_file():
        state = json.loads(args.state.read_text(encoding="utf-8"))
        return MemoryRepository.from_state(state)
    return MemoryRepository()


def _safe_rollback(repository: SeedRepository) -> None:
    try:
        repository.rollback()  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - best-effort rollback on the error path
        pass


def _persist_state(args: argparse.Namespace, repository: SeedRepository, report: SeedReport) -> None:
    if args.state is None or not isinstance(repository, MemoryRepository):
        return
    if not report.valid:
        return  # never persist the result of a failed run
    _atomic_write_text(args.state, json.dumps(repository.to_state(), indent=2, sort_keys=True) + "\n")


def _write_report_file(path: Path, report: SeedReport, repository_meta: dict[str, Any]) -> None:
    payload = report.to_dict()
    payload["repository"] = repository_meta
    _atomic_write_text(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):  # pragma: no cover - defensive
            os.unlink(temp_name)


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
