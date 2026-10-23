# Inventory Platform — Implementation

Implementation repo for the verified plan in `../PLAN.md` / `../design/`.
Plan status: **v1.5.0 VERIFIED PASS** (see `../verification/VERIFICATION-REPORT.md`).

## Stack

Python 3.12 · FastAPI · Pydantic v2 · PostgreSQL 16 · SQLAlchemy 2.0 (async) · Alembic ·
Redis 7 + RQ · transactional outbox → Redis Streams · Dex OIDC broker · Docker Compose.

## Layout

```
app/
  api/           FastAPI app, route registry, dependencies
  core/          config, logging, authz/audit/outbox (added by FS-00)
  db/            async engine/session helpers
  modules/       bounded contexts (one package per FS module)
  contracts/     OpenAPI fragments + event schemas (S0-04)
  migrations/    single Alembic chain (S0-03; FS-00 starts 0001)
  tests/         unit / integration / invariant / contract suites
  seed/          idempotent reference-data loader (S0-07)
  workers/       RQ worker entrypoint
contracts/       aggregated OpenAPI + event registry (S0-04)
scripts/         deploy/flag/Dex/seed/worktree helpers (S0-06)
ops/dex/         Dex dev config (S0-02)
tools/           plan-package gates (read-only reference)
```

## Commands

```bash
make install          # editable install + dev deps
make lint             # ruff
make typecheck        # mypy
make test             # pytest
make plan-gates       # plan-package gates (validate/permissions/coverage/render/parallel)
make check-parallel   # S0 execution-ready check (NO until scaffold complete)
make up / down        # docker compose (when S0-02 lands)
make migrate          # alembic upgrade head in api container
make seed             # idempotent reference-data bootstrap
```

## Workflow rules

- `app/modules/<context>/` is owned by that feature set's builder; shared files (`app/api/main.py`,
  `app/api/registry.py`, contract aggregation, Alembic chain order, `app/tests/conftest.py`, `app/seed/`,
  compose/scripts) are integrator-owned.
- Do not edit plan docs (`../design/**`, `../feature-sets/**`, `../PLAN.md`, `../README.md`) from the
  implementation repo; plan changes go through the Lead.
- Build is parallel (B4 = up to 8 FS cards) but deployment stays wave-ordered. S0 must pass
  `python tools/check_parallel_plan.py --s0` before FS-00 starts.
