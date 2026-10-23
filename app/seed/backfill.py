"""Existing-tenant backfill: clone post-0013 tenant defaults into already-provisioned tenants.

Usage::

    python -m app.seed.backfill --tenant <uuid> [--database-url URL] [--json]
    python -m app.seed.backfill --all [--database-url URL] [--json]

Idempotent: only missing number sequences / tenant settings / feature flags / the default enabled
``INVENTORY_COUNT`` approval definition are inserted; existing rows are never modified. This is the
backfill path for tenants created before migrations 0013–0030 added their per-tenant seeds.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Sequence
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.tenant_provisioning import backfill_tenant_defaults, provision_all_tenants


def _database_url(explicit: str | None) -> str:
    value = explicit or os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL") or os.environ.get("SEED_DATABASE_URL")
    if not value:
        raise SystemExit("ERROR: no database URL (pass --database-url or set DATABASE_URL/TEST_DATABASE_URL)")
    return value


async def _run(database_url: str, tenant_id: str | None, all_tenants: bool) -> dict[str, Any]:
    engine = create_async_engine(database_url)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            if all_tenants:
                result = await provision_all_tenants(session)
            else:
                assert tenant_id is not None
                result = {tenant_id: await backfill_tenant_defaults(session, UUID(tenant_id))}
            await session.commit()
            return result
    finally:
        await engine.dispose()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.seed.backfill", description="Backfill tenant provisioning defaults (idempotent).")
    parser.add_argument("--tenant", default=None, help="existing tenant UUID")
    parser.add_argument("--all", action="store_true", dest="all_tenants", help="backfill every non-SYSTEM tenant")
    parser.add_argument("--database-url", default=None, help="SQLAlchemy async database URL")
    parser.add_argument("--json", action="store_true", dest="as_json", help="machine-readable output")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.all_tenants and not args.tenant:
        print("ERROR: pass --tenant <uuid> or --all", file=sys.stderr)
        return 2
    try:
        result = asyncio.run(_run(_database_url(args.database_url), args.tenant, args.all_tenants))
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    if args.as_json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        for tenant, counts in result.items():
            print(f"{tenant}: {counts}")
    return 0


if __name__ == "__main__":  # pragma: no cover - module entry point
    raise SystemExit(main())
