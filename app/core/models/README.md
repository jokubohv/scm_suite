# FS-00 platform models + migrations (task-36, B1 data layer)

**Scope:** 34 `platform` tables split across the three FS-00 revisions exactly per `design/DATA-MODEL.md`
§3 + §21.1 M00 row and `feature-sets/FS-00.md` §4. Authoritative DDL is in the Alembic revisions
(`app/migrations/versions/0001_platform_core.py`, `0002_platform_security.py`, `0003_platform_events.py`);
this package holds the matching SQLAlchemy 2.0 Core metadata for autogenerate checks, tests and later
FS-00 services.

| Revision | Tables | Count |
|---|---|---|
| `0001_platform_core` | schema + `citext`; tenant lifecycle/config/reference (`tenant`, `tenant_setting`, `currency`, `exchange_rate`, `fiscal_period`, `number_sequence`, `feature_flag`, `custom_field_def/value`), RBAC base (`role`, `permission_scope`, `role_permission`, `user_role`, `user_data_scope`), `api_key`, approvals (`approval_definition/request/action`), `document_link`, `attachment_metadata`, legacy `app_user`; seeds: `SYSTEM_TENANT_ID` row + 10 ISO currencies | 21 |
| `0002_platform_security` | Δ-MT `user_identity` + `app_user` expand-contract (membership/`token_version`), `tenant_invitation`, `user_session`; Δ-SSO `tenant_sso_connection`, `user_identity_link`, `sso_group_role_map`; `audit_event` (append-only, monthly partitions); Δ-1 `finance_journal_intent` (append-only); tenant RLS + SYSTEM_TENANT read policies + conditional grants | 8 |
| `0003_platform_events` | `outbox_event` (append-only, monthly partitions), `event_consumer_checkpoint`, `job_definition`, `job_run`, `idempotency_key`; seeds: 4 platform job definitions (`platform.outbox.relay`, `platform.jobs.scheduler`, `platform.retention.purge`, `platform.sso.health.monitor`) | 5 |

## Invariants implemented
- Naming conventions from `app/migrations/base.py` (`pk_/uq_/fk_/ck_/ix_`), UUID PKs, `version` optimistic
  locking, `unique (tenant_id, id)` for composite tenant FKs; global exceptions `currency` (code PK,
  `tenant_id NULL`) and `user_identity` (no `tenant_id`, auth-only).
- **RLS** on every tenant-owned platform table (`tenant_isolation` + `system_tenant_read` policies);
  `user_identity` is excluded (auth-service role only).
- **Append-only** triggers on `audit_event`, `outbox_event`, `finance_journal_intent`, `approval_action`,
  `document_link`; `updated_at` touch triggers for all timestamped tables.
- Monthly RANGE partitions for `audit_event`/`outbox_event` (current ± window; `app.contracts`-like
  maintenance job is a later platform job).
- Additive/N-1 safe: `identity_id` backfilled from legacy `app_user.email` before NOT NULL; every
  revision has a working `downgrade()` (tested empty → head → base → head).

## Migration seeds vs S0-07 seed loader (important)
The migrations seed only **bootstrap** rows: SYSTEM tenant, ISO-4217 currencies, 4 job definitions.
Deferred to the **S0-07 seed loader** (owner: harness/seed agent; `app/seed/`): the 255-scope permission
catalogue + 7 role templates + 14 SoD pairs from `design/permissions.json`, per-tenant settings/flags/
number sequences, SSO scope rows, and any tenant-provisioning defaults. Migration seeds are idempotent
(`ON CONFLICT DO NOTHING`) so the loader can run twice without diff.

## Environment/DB role expectations (S0-06)
`0002`/`0003` grant privileges **only if** roles `platform_app`, `platform_auth`, `finance_adapter`
already exist (`DO $$ … IF EXISTS (pg_roles) … $$`); the compose bootstrap (S0-06) creates roles and the
auth/service split. On a plain dev DB the grants are a no-op and the migration still succeeds.

## Autogenerate wiring (integrator action)
`app/migrations/env.py` collects metadata from `app.db.*` / `app.core.db.*` only
(`_DISCOVERY_CANDIDATES` in `app/migrations/base.py`). The integrator must add `app.core.models` (or
import it in `env.py`) to enable `alembic check`/autogenerate drift detection; until then `alembic check`
reports the platform tables as “removed” (metadata not discovered) while runtime migrations are correct.
Do not duplicate the tables in another metadata.

## Verification (recorded 2026-10-23)
```bash
python3 -m compileall -q app                                              # pass
python3 -m ruff check app/migrations/versions app/core/models             # pass
python3 -m mypy app/core/models                                           # pass (3 files)
python3 -m app.migrations.chain_guard                                     # OK 3 revisions
python3 -m pytest app/tests/migrations app/tests/integration/platform_data -q   # 11 passed
alembic -c alembic.ini upgrade head                                       # head 0003_platform_events
alembic -c alembic.ini downgrade base && alembic -c alembic.ini upgrade head    # round-trip pass
```
Live schema: **34 base tables**, 619 columns, 619 constraints, 127 indexes, RLS enabled on 33 relations
(32 tenant tables + partitioned parent accounting), 66 policies, 2 partitioned parents, 44 triggers,
seeds = 1 SYSTEM tenant / 10 currencies / 4 job definitions.

## Known deviations / notes
1. SSO health job definition is seeded in **0003** (where `job_definition` is created) rather than 0002,
   because DATA-MODEL §21.1 M00 row assigns job tables to `0003_platform_events`; flagged to Lead.
2. `alembic check` drift is expected until the metadata discovery wiring above lands (no schema defect).
3. `chain_guard --require-complete` fails by design until the other 27 pre-allocated revisions merge.
