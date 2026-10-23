"""Seed CLI: ``python -m app.seed.cli --tenant <uuid> [--dry-run]``.

Exit codes:
* ``0`` — seed run completed and the report is valid (dry-run included).
* ``1`` — unexpected internal error (traceback suppressed unless ``--verbose``).
* ``2`` — invalid input/seed data or repository error (report shows the errors).

The memory profile is the default at S0. ``--state <path>`` persists the in-memory repository as JSON,
which makes "load twice, no diff" verifiable from the shell: run twice with the same state file and the
second run reports ``inserted=0 updated=0`` with the same digest. The SQLAlchemy profile is the FS-00
integration hook (requires platform models/migrations; currently reports a clear error).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path
from uuid import UUID

from app.seed.constants import DEFAULT_DATA_DIR, SYSTEM_TENANT_ID
from app.seed.loader import SeedLoader, validate_sources
from app.seed.permissions import SeedValidationError
from app.seed.report import SeedReport
from app.seed.repository import (
    MemoryRepository,
    SeedRepository,
    SeedRepositoryError,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.seed.cli",
        description="Idempotent seed/reference bootstrap (S0-07).",
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
        choices=("memory", "sqlalchemy"),
        default="memory",
        help="repository profile (default memory; sqlalchemy is the FS-00 integration hook)",
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
        repository = _build_cli_repository(args)
    except SeedRepositoryError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    try:
        loader = SeedLoader(
            repository,
            data_dir=args.data_dir,
            permissions_source=args.permissions_source,
            dry_run=args.dry_run,
        )
        report = loader.load(tenant, seed_system=not args.no_system)
    except (SeedValidationError, SeedRepositoryError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        if args.verbose:
            raise
        print(f"ERROR: unexpected seed failure: {exc}", file=sys.stderr)
        return 1

    if not args.dry_run:
        _persist_state(args, repository, report)

    if args.report_file is not None:
        _write_report_file(args.report_file, report)

    if args.as_json:
        print(report.to_json())
    elif not args.quiet:
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


def _build_cli_repository(args: argparse.Namespace) -> SeedRepository:
    if args.profile == "sqlalchemy":
        # FS-00 hook: the session factory/table registry are supplied by FS-00 wiring, not by the CLI.
        # Fail fast with an actionable message instead of an opaque NotImplementedError from the hook.
        raise SeedRepositoryError(
            "--profile sqlalchemy requires an async session factory and the FS-00 table registry; "
            "platform models/migrations are not present yet at S0 — use the default memory profile "
            "(or call app.seed.loader.SeedLoader with a SqlAlchemySeedRepository built by FS-00)"
        )
    if args.state is not None and args.state.is_file():
        state = json.loads(args.state.read_text(encoding="utf-8"))
        return MemoryRepository.from_state(state)
    return MemoryRepository()


def _persist_state(args: argparse.Namespace, repository: SeedRepository, report: SeedReport) -> None:
    if args.profile != "memory" or args.state is None:
        return
    if not isinstance(repository, MemoryRepository):
        return
    if not report.valid:
        return  # never persist the result of a failed run
    _atomic_write_text(args.state, json.dumps(repository.to_state(), indent=2, sort_keys=True) + "\n")


def _write_report_file(path: Path, report: SeedReport) -> None:
    _atomic_write_text(path, report.to_json())


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
