# Seed / reference bootstrap (`app/seed`, S0-07)

Idempotent, data-driven, tenant-scoped seed loader for the reference data every feature set needs:
SYSTEM_TENANT, the authoritative RBAC catalogue (255 scopes / 7 role templates / 14 SoD pairs) from
`design/permissions.json`, currencies, UoM types/units, inventory statuses, transaction-type catalogue,
feature-flag definitions, number-sequence templates and default tenant settings.

At S0 the physical SQLAlchemy models do not exist (FS-00 owns them), so the loader is
**repository-agnostic**:

| Profile | Implementation | Use |
|---|---|---|
| `memory` (default) | `MemoryRepository` | tests, CLI gate (`--state` gives load-twice-no-diff), S0 exit check |
| `sqlalchemy` | `SqlAlchemySeedRepository` (integration hook) | FS-00 fills `get/upsert/count` once platform tables exist; no caller/loader changes |

## Usage

```bash
# validate the RBAC catalogue + every seed payload (no writes)
python -m app.seed.cli --validate-only

# dry run: compute the exact inserts/updates without writing
python -m app.seed.cli --tenant <tenant-uuid> --dry-run

# symbolic 'system' tenant = SYSTEM_TENANT (global datasets only) — used by Makefile `make seed`
python -m app.seed.cli --tenant system --dry-run

# S0-06 wrapper: python -m app.seed (system-only unless SEED_TENANT_ID is set)
python -m app.seed --dry-run
scripts/seed.sh dev --check
scripts/seed.sh dev

# real run against the in-memory repository, persisted for idempotency checks
python -m app.seed.cli --tenant <tenant-uuid> --state /tmp/seed-state.json --json
python -m app.seed.cli --tenant <tenant-uuid> --state /tmp/seed-state.json --json
# -> second run: totals.inserted == 0, totals.updated == 0, identical digest

# global/system datasets only (currencies, SYSTEM tenant, transaction-type catalogue)
python -m app.seed.cli --tenant 00000000-0000-0000-0000-000000000000
```

Exit codes: `0` valid run (dry-run included), `1` unexpected failure, `2` invalid input/data/repository
error. `--report-file <path>` writes the JSON report; `--quiet`/`--json` control stdout formatting.
Tenant selection: `--tenant <uuid>` or `--tenant system`; without either flag the loader falls back to
`SEED_TENANT_ID`/`INV_SEED_TENANT_ID`, then to system-only mode (safe on an empty stack, used by
`scripts/seed.sh`/`make seed`).

## Data layout (`app/seed/data/`)

| File | Contents |
|---|---|
| `manifest.json` | ordered dataset list: name, target table, natural key, tenant scope, owner FS, source |
| `system_tenant.json` | reserved `SYSTEM` tenant row (`00000000-0000-0000-0000-000000000000`, UUIDv7-shaped) |
| `currencies.json` | initial ISO-4217 active set with numeric code + minor units (extend additively) |
| `uom.json` | UoM types (`EA_TYPE`, `WEIGHT`, `VOLUME`, `TIME`) and units with base-unit flags |
| `inventory_statuses.json` | `AVAILABLE` (default) … `USED`/`REFURB` with availability/commit/sell/consigned flags |
| `transaction_types.json` | T-01…T-31 reference catalogue with qty effect/value-only/posting-rule/events |
| `feature_flags.json` | every `*.enabled` flag from the manifest, all default OFF |
| `number_sequences.json` | the 27 documented sequence templates (PO…AIM) |
| `tenant_settings.json` | default tenant settings (`negative_inventory_policy=BLOCK`, D5/rbac/auth defaults) |

RBAC is loaded from `design/permissions.json` (authoritative, read-only) with a packaged fallback copy
at `app/tests/fixtures/permissions.json`; the loader never re-declares scope data in code.

## Guarantees

- **Idempotent:** second load of the same seed revision = 0 inserts, 0 updates, identical
  `state_digest()`; `MemoryRepository.to_state/from_state` round-trips exactly.
- **Non-destructive:** no delete API; updates are by natural key only; deactivation/retirement is a
  future status change, never a seed delete.
- **Tenant-isolated:** tenant-scoped rows carry the target `tenant_id`; system rows are global
  (`currency`/`transaction_type` with `tenant_id NULL`, SYSTEM tenant row). Multi-tenant loads never
  touch another tenant's rows.
- **Strict validation:** documented counts (255/241/1/13/7/14/18) must match; role templates must be
  assignable-only; non-admin roles must not combine SoD pairs; admin must equal all assignable scopes.
- **Inherited invariants:** `DM-GLOBAL-10` (SYSTEM rows read-only), Δ-RBAC catalogue authority,
  frameworks' "all flags default off" (`design/FEATURE-SET-FRAMEWORK.md` §6).

## Tests

```bash
python -m pytest app/seed/tests -q
```

Covers catalogue integrity, all data payloads, load-twice-no-diff (single/multi tenant), repository
upsert/digest semantics, dry-run non-mutation, CLI state persistence and error paths.

## Integration hook for FS-00

1. Build `SqlAlchemySeedRepository(session_factory, table_registry)` with the platform table mapping.
2. Call `SeedLoader(repository).load(tenant_id)` inside tenant provisioning (same transaction).
3. Map `manifest.json` dataset names/tables to the physical `platform.*`/`item_master.*`/`traceability.*`
   tables (keys are already natural keys; tenant columns are injected by the loader).
4. Keep `data/*.json` additive-only and bump `manifest.json`/`plan_version` when FS-00 changes reference
   data; `--validate-only` is the CI gate for seed drift.

Ownership: S0-07 / scaffolder-integrator. Do not edit plan documents from this package.
