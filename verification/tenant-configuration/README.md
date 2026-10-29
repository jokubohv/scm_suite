# Own-tenant base currency configuration

Branch: `feat/tenant-currency-configuration`, based on `114f38f3899572d71780f9ba9c8b69e82b01f0a0`.
Verified locally on 2026-10-29 with Python 3.12.4 and PostgreSQL 16.

## API

`GET /api/v1/admin/tenant-configuration` and `PATCH` on the same path require the
existing assignable `platform.admin` scope. The verified API key or bearer token
selects the tenant. The request accepts no tenant ID, lifecycle controls, or arbitrary
settings. The global `/platform/tenants/{id}` permission remains unchanged.

GET returns `tenant_id`, `base_currency_code`, `version`, and
`currency_change_blockers`, plus an `ETag`. To select BOB, PATCH with the returned
ETag in `If-Match` and this body:

```json
{"base_currency_code": "BOB"}
```

The target must be an active shared currency. A successful change returns the new
configuration and ETag, and writes `tenant.configuration.updated` to the tenant's
audit log in the same transaction. A same-value request with the current ETag is a
no-op. After a lost response, GET the configuration before retrying.

| Response | Meaning |
|---|---|
| 403 `FORBIDDEN_SCOPE` | Key lacks `platform.admin`. |
| 422 `CURRENCY_INVALID` | Target is missing, inactive, or not a shared currency. |
| 428 `PRECONDITION_REQUIRED` | GET the configuration and send its ETag. |
| 422 `PRECONDITION_INVALID` | If-Match is not a positive version/quoted ETag. |
| 412 `PRECONDITION_FAILED` | Configuration changed; reread it. |
| 409 `BASE_CURRENCY_IN_USE` | Existing valuation data needs an explicit migration. |
| 409 `CONFIGURATION_BUSY` | A competing write holds a lock; reread and retry later. |

Blockers are stable business reason codes, such as `inventory_history`,
`cost_configuration`, and `manufacturing_cost_configuration`; storage table names
are not part of the public API. The full enum is in the generated runtime schema.
The GET projection is advisory; PATCH rechecks it under locks.

## Preservation and concurrency

This is a base-unit setup operation, not an FX conversion. It rejects inventory
history even if net quantity/value is zero, as well as balances, financial journals,
cost configuration/history, monetary drafts, manufacturing rates/WIP, consignment
records and count snapshots. Explicitly denominated catalog prices are preserved.
No values, exchange rates, documents, shared currency rows or other tenant records
are rewritten. No UI/specimen change applies: this is an API integration surface.

The platform repository owns permission-independent configuration policy and its
own tenant write. A narrow `CurrencyUsage` boundary keeps cross-module valuation
storage knowledge in the composition adapter. No existing global-admin repository
was reused or given tenant privileges, and no behavior-preserving refactor was needed.

PATCH locks the tenant row with `FOR UPDATE NOWAIT`, then checks the version, locks
the target currency against concurrent deactivation, and takes short `SHARE NOWAIT`
locks on mutable valuation tables. Append-only history keeps its existing grants:
inserts take foreign-key `KEY SHARE` locks on the tenant and therefore conflict with
the tenant row lock. All reads retain the request's tenant RLS context.

Tradeoff: mutable-table locks briefly cover other tenants too, and their active
writes may produce `CONFIGURATION_BUSY`. This rare setup operation avoids changes
to every posting path, new privileged database functions, or relaxed ledger grants.
Locks never wait for active writers and are released on commit/rollback. New monetary
storage added by future modules must be included in the dependency adapter.

## Evidence

- The new API-key regression first failed at the expected missing endpoint (404).
  An initial fixture schema typo was corrected before capturing that behavioral red.
- Five acceptance cases exercise real API-key hashing/lookup, scope enforcement,
  RLS-constrained SQL, BOB update/read-back/audit, other-tenant preservation,
  rejected tenant/lifecycle fields, stale/missing preconditions, inactive/missing
  currencies, zero-value history, and unposted manufacturing costs.
- The combined platform configuration, platform-admin, and their existing validation
  suites passed **138 tests**, no skips. After changing public blockers to stable
  reason codes, the five affected acceptance cases passed again.
- [check_http.py](check_http.py) runs a separate Uvicorn process with production-style
  `platform_app`/`platform_auth` grants and a non-superuser, non-BYPASSRLS app login.
  [http-result.json](http-result.json) records durable BOB read-back, other-tenant
  preservation, concurrent append and mutable-cost conflicts, retry after rollback,
  exactly one winner for competing configuration changes, and committed audits.
- The first HTTP harness launch timed out during app startup at its original two-second
  timeout. The ten-second startup timeout passed; API acceptance uses fifteen seconds.
- A stricter append-only grant regression caught the initial table-lock permission
  issue. The final implementation uses the existing tenant FK lock for those tables;
  no production permissions or schema were expanded.
- Ruff, plan/permission/coverage gates, Compose configuration, and contract lint passed.
  Fourteen runtime-contract, provider-contract, route-scope and OpenAPI-auth tests passed.
  [contract-diff.json](contract-diff.json) proves exactly two added runtime operations,
  with no removed or changed existing operations. Generated artifacts are updated.
- Repository-wide mypy still reports **343 errors in 14 files**. Its error diagnostics
  exactly match a fresh archive of the base commit; no new diagnostics were added.

Repeat the integration check using a disposable migrated PostgreSQL database:

```sh
TEST_DB_PROFILE=url TEST_DATABASE_URL='<operator asyncpg URL>' \
  python3 -m pytest -n 0 app/tests/integration/platform_config \
  app/tests/integration/platform_admin app/tests/unit/services_config \
  app/tests/unit/services_platform_admin
```

For the HTTP check, provision the repository's normal app/auth roles before migrating
a fresh disposable database. Set `ENV=test`, a synthetic `JWT_SECRET`,
`TEST_DATABASE_URL` to its localhost operator URL, and `DATABASE_URL` to its restricted
app URL, then run `PYTHONPATH=. python3 verification/tenant-configuration/check_http.py`.
Only synthetic fixtures are used; dispose of the database afterward.

## Release boundary

This endpoint has not been deployed, and Intel Ranch's production base currency has
not been changed by this work. No migration is required. Application rollback removes
the additive endpoint and retains configuration/audit rows; it does not undo a chosen
base currency. Once valuation data exists, reversal also requires a currency migration.
After release, read Intel Ranch's configuration with its existing admin-scoped key;
change to BOB only if the guarded PATCH accepts it, then read back the result. An
existing-data conflict must be resolved by an explicitly scoped migration, not bypassed.
