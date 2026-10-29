# Global currency writes

Candidate: `fix/admin-currency-create`, based on `05d6089` (updated local `main`, tracking
`origin/master`). Verified locally on 2026-10-28, Python 3.12.4 / PostgreSQL 16.

## Failure and fix

The new API-key regression reproduced `POST /api/v1/admin/currencies` with BOB as
`500 INTERNAL_ERROR`. PostgreSQL rejected the insert with SQLSTATE `42501`:
`new row violates row-level security policy for table "currency"`.
`0002_platform_security` permits global currency reads, but its tenant write policy
cannot accept the endpoint's correct `tenant_id = NULL` row. The prior integration
tests used an owner session and bypassed this restriction.

`0032_currency_global_writes` adds INSERT/UPDATE policies for global currency rows
with a nonempty request tenant context. Existing table grants, RLS and tenant-owned
policies remain in place; the API still requires `platform.finance.sync`. It does
not seed BOB, change a tenant's base currency, or alter the API schema. Capability
authorization remains at the API boundary, consistent with the existing platform.
No UI/specimen change applies: this branch exposes the API consumer contract and
has no currency-management browser journey.

## Evidence

- Before migration: the new suite reported one expected creation failure and two
  passing authorization/isolation checks. BOB creation returned 500 with the RLS error.
- After migration: **73 tests passed**, no skips, covering platform configuration,
  its existing validation coverage, platform schema checks, and the empty-database
  migration upgrade/downgrade/re-upgrade round trip.
- The new regression uses real key hashing/lookup, scope checks, request tenant
  context, repository SQL and PostgreSQL RLS. Its temporary non-owner role has no
  superuser or BYPASSRLS privileges. Session plumbing uses the existing rollback
  fixture, so it does not prove network transport or durable commits by itself.
- A separate Uvicorn process and localhost HTTP client verified the unmodified app
  against a fresh migrated database using a non-superuser login and production-style
  `platform_app`/`platform_auth` roles. Results are in [http-result.json](http-result.json).
  This covered durable BOB creation, another tenant's read-back, duplicate conflict,
  denied reader writes, PATCH, audit rows, and unchanged USD base currencies.
- The HTTP run downgraded to 0031 with BOB and audit data present, verified those
  records were unchanged and writes failed again, then upgraded to 0032 and verified
  preserved records plus successful writes. This was confined to disposable data.
- Ruff, complete migration-chain guard, plan gates, contract lint and Compose
  configuration checks passed. No router/schema changes required regenerated clients.
- Repository-wide mypy reports **343 errors in 14 files**. Its diagnostics are
  identical to a clean archive of `05d6089`; this change adds no type errors.
  The full repository test suite was not run; acceptance used the bounded set below.

Repeatable regression command, using a disposable, migrated PostgreSQL database and
an administrative test login able to create rollback-scoped roles/databases:

```sh
export TEST_DB_PROFILE=url
export TEST_DATABASE_URL='<disposable PostgreSQL asyncpg URL>'
export ALEMBIC_DATABASE_URL="$TEST_DATABASE_URL"
export LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8
python3 -m alembic upgrade head
python3 -m pytest -n 0 \
  app/tests/integration/platform_config \
  app/tests/unit/services_config \
  app/tests/migrations/test_0001_0002_0003.py \
  app/tests/integration/platform_data/test_platform_schema_live.py
```

## Release boundary

No production deployment or production BOB creation was performed. Applying 0032
through the normal migration workflow is required for the fix; the existing API
code can then create BOB with numeric code 68 and minor units 2 using an authorized
key. Read back `/api/v1/admin/currencies/BOB` after creation and verify the tenant's
base currency is unchanged. If BOB already exists, read it back instead of replaying
an uncertain create. The migration downgrade removes only the two new policies and
retains reference/audit rows, but restores the broken global write behavior; production
recovery should follow the existing forward-fix release policy.
