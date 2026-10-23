# FS-00 Exit-Readiness Audit — Independent Gap Report

**Task:** team task-50 · **Auditor:** crawler-item-inventory (independent verifier) · **Date:** 2026-10-23 ~12:20 ET
**Scope:** FS-00 vs `feature-sets/FS-00.md`, `design/feature-sets.json` FS-00, PLAN §7.5, `design/permissions.json`, live stack/DB, app source/tests, migrations 0001–0003, contracts, `verification/evidence/W1/STATUS.md`.
**Write policy:** read-only audit; only `verification/evidence/FS00-AUDIT/` written. No code/spec/plan changes.

## Verdict

**NOT EXIT-READY — 0A/C0 BLOCKED.** 3 BLOCKERS, 3 MAJORS, 4 MINORS. The implementation is substantially real (34/34 tables, live migrations, 255-scope RBAC seed, live API/stack healthy, 367 unit tests pass, 104/105 integration tests pass), but the **exit gate cannot be executed as specified**, the **C0 contract/API surface is not frozen-consistent**, and **security/invariant/event/job evidence is incomplete**.

## Live environment (confirms DB recovered; probes in `db-live.log`, `live-stack.log`)

- DB: `postgresql+asyncpg://…@localhost:25432/inventory` **UP**; alembic head `0003_platform_events`; 34/34 platform base tables (models/spec match); 33 RLS tables / 66 policies; 13 append-only triggers; `audit_event`/`outbox_event` partitioned.
- Stack: compose 7/7 healthy; **API host port is 28000** (`inv-s0-gate-api-1` 0.0.0.0:28000→8000). `/healthz` 200; `/api/v1/openapi.json` 78 paths; `/api/v1/auth/providers` 200; `/api/v1/admin/permissions` 401 problem+json (guard works). **Port 8000 is an unrelated `hive/serebo` uvicorn — do not smoke-test 8000.**
- Seed (task-48 landed): tenant 2 (SYSTEM + dev), currency 102, permission_scope 255, role 7, role_permission 591, number_sequence 27, tenant_setting 8, feature_flag 17, job_definition **4**.
- Not verified (read-only audit): downgrade path (`alembic downgrade`), destructive migration re-run, 0A deploy.

## BLOCKERS

### B1 — Exit-gate commands reference non-existent tests (17-command gate = 1 PASS, 14 FAIL, 2 blocked)
`design/feature-sets.json` FS-00 `exit_checks` (and FS-00 §11) name 14 pytest targets that do not exist in this repo layout; actual tests live under `platform_auth/`, `platform_rbac/`, `platform_sso/`, `platform_api_keys/`, etc.

| Command target | Status | Evidence |
|---|---|---|
| `docker compose run --rm api alembic upgrade head` | READY (schema already at head; not re-run — read-only) | `db-live.log` |
| `pytest app/tests/integration/platform -q` | **FAIL — dir absent** | actual `app/tests/integration/platform_*` |
| `pytest app/tests/contracts/test_fs00_provider_contract.py -q` | **FAIL — file absent** | only `app/tests/contracts/base/` |
| `pytest app/tests/invariants/test_rls_tenant_isolation.py app/tests/invariants/test_append_only_privileges.py -q` | **FAIL — dir absent** | no `app/tests/invariants/` |
| `pytest app/tests/integration/finance_port/test_journal_intent_read_api.py -q` | **FAIL — dir absent** | no `finance_port/` |
| `pytest app/tests/invariants/test_mt_tenant_user_isolation.py -q` | **FAIL — dir absent** | no `app/tests/invariants/` |
| `pytest app/tests/integration/platform/test_mt_auth_flows.py -q` | **FAIL — dir absent** | — |
| `pytest app/tests/integration/platform/test_mt_invitations_sessions.py -q` | **FAIL — dir absent** | — |
| `pytest app/tests/invariants/test_mt_identity_rules.py -q` | **FAIL — dir absent** | — |
| `python tools/feature_sets.py permissions` | **PASS** | `permissions-gate.log` (255/241/13/1/7/14) |
| `pytest app/tests/security/test_route_scope_coverage.py -q` | **FAIL — dir absent** | no `app/tests/security/` |
| `pytest app/tests/integration/platform/test_rbac_roles.py -q` | **FAIL — dir absent** | — |
| `pytest app/tests/integration/platform/test_sod_policy.py -q` | **FAIL — dir absent** | — |
| `pytest app/tests/integration/platform/test_sso_oidc_flow.py -q` | **FAIL — dir absent** | — |
| `pytest app/tests/integration/platform/test_sso_tenant_isolation.py -q` | **FAIL — dir absent** | — |
| `pytest app/tests/integration/platform/test_sso_group_mapping.py -q` | **FAIL — dir absent** | — |
| `python tools/feature_sets.py record --set FS-00 --env $ENV --status verified` | BLOCKED (prerequisites) | — |

The gate cannot be made green by environment recovery. It needs either (a) test files/suites at the manifest paths (preferred; plan-frozen names), or (b) a Lead-approved manifest update. Do not run `record --status verified` until fixed.

### B2 — API-surface parity and C0 contract freeze incomplete
- Spec §6: **102 endpoint rows**; implemented: **78 OpenAPI paths / 106 ops** (117 runtime ops including hidden aliases); FS-00 contract fragment: **84 paths / 102 ops** (`contracts/openapi/fragments/FS-00.openapi.json`).
- **21 real spec endpoints missing** (2 more are framework `/internal/deps`, `/openapi.json`); **27 implemented ops are not in spec**. Fragment vs implementation: 12 impl paths absent from fragment, 12 fragment paths absent from implementation (`contract-fragment.log`, `route-parity.log/json`).
- Missing real endpoints include: `GET /api/v1/order/demand/open`; `GET /api/v1/finance/period-status`; `GET /api/v1/custom-field-values` + `PUT /api/v1/admin/custom-field-values`; `GET /api/v1/reference/exchange-rates`; `GET /api/v1/admin/job-runs`(+`/{id}`) + `POST …/cancel`; `POST /api/v1/admin/jobs/{job_name}/run`; `POST /api/v1/admin/users` (spec §6 line 663; conflicts with C-14 “user creation replaced by invitations/membership”); `PATCH /api/v1/admin/approval-definitions/{id}`; `PATCH /api/v1/admin/number-sequences/{id}`; `POST /api/v1/platform/tenants/{id}/bootstrap-admin` and `/offboard` (spec §6 lines 659–660); `POST /internal/outbox/replay` (line 642); `PUT /internal/finance/exchange-rates`, `/internal/finance/fiscal-periods`, `/internal/order-boundary/demand-doubles` (line 644).
- Extra/alias paths (not in spec) include `/api/v1/admin/audit-events` vs spec `/audit-events`, `/admin/sequences` vs `/admin/number-sequences`, `/admin/flags` vs `/admin/feature-flags`, `/admin/jobs/{name}/runs` vs `/admin/job-runs`, unversioned attachment/document-link paths, `/me/memberships`.
- **API-key method mismatch:** spec `POST /api/v1/admin/api-keys/{key_id}/revoke` (FS-00 line 703) vs implementation `DELETE /api/v1/admin/api-keys/{key_id}` (task-43). Reconcile spec or route before C0.
- C0 **not freezable** until FS-00 fragment is regenerated from the final routers, route/scope coverage passes, and contract fragment vs runtime diff is empty. No C0 evidence file exists under `verification/evidence/FS-00/` yet.

### B3 — Security/invariant/contract test suites absent at required gate paths (L3 evidence missing)
`app/tests/invariants/`, `app/tests/security/`, `app/tests/integration/platform/`, `app/tests/integration/finance_port/`, `app/tests/contracts/test_fs00_provider_contract.py` do not exist. Therefore INV-30 tenant/user isolation, RLS, append-only, route-scope/allowlist coverage, RBAC/SoD flow, SSO flow/isolation/group mapping, and provider/consumer contract tests have **no executable L3 evidence**; the exit gate (B1) and 0A acceptance cannot be proven.

## MAJORS

### M1 — Events/jobs incomplete
- FS-00 §7 expects **33 event types**; app source contains emission points only for `platform.approval.requested/approved/rejected` (`app/core/approvals.py:450/574/589`) and `platform.flag.changed` (`app/core/repositories/config.py:58`). **21 expected event types have no app emission point** (auth.login.failed, auth.tenant_switched, document.approved/closed/rejected/reversed/submitted, finance.journal_intent.recorded, membership.activated/roles.changed/suspended, role.created/updated/cloned/permissions.changed, sod.violation.blocked, tenant.provisioned/suspended, user.invited, user.session.revoked, sso.connection.*, identity-linked events — see `events-jobs.log`).
- Jobs: migration `app/migrations/versions/0003_platform_events.py:31-36` seeds **4/6** specs: `platform.outbox.relay`, `platform.jobs.scheduler`, `platform.retention.purge`, `platform.sso.health.monitor`; **missing `platform.health.reconcile` and `platform.partition.maintenance`**. No job-handler registry (`JOB_HANDLERS`/`register_job` grep empty), so scheduler seed names are not demonstrably executable.

### M2 — Deny-by-default/allowlist gap
- `GET /api/v1/me/memberships` (`app/api/routers/platform/tenants.py:41`) declares **no `require_scope`** and is **not** in `design/permissions.json` self-service allowlist (6 entries); its own docstring calls it a “self-service allowlist route” → INV-31/route-scope gap.
- `POST /api/v1/auth/invitations/{token}/accept` (hidden alias of the allowlisted `/api/v1/admin/invitations/{token}/accept`) is unscoped and not allowlisted; `GET /api/v1/auth/me` hidden alias is unscoped; `/internal/deps` is unscoped (framework/health surface — lower risk).
- No route-scope coverage test exists (B3). `/api/v1/admin/permissions` correctly returns 401 problem+json on the live stack, and platform-tenant routes do carry `platform.tenant.manage` (runtime dependency inspection), but the allowlist matrix is incomplete.

### M3 — Integration suite has one stale failing assertion
`app/tests/integration/platform_data/test_platform_schema_live.py:82-83` expects `platform.tenant == 1` and `platform.currency == 10`; live seeded DB has tenant 2 (SYSTEM+dev) and currency 102 (jobs==4 matches). Result: **104/105 integration tests pass**. Either fix the test to the seed contract (tenant >= 1 / ISO-4217 count) or isolate it to a pristine DB; a failing suite cannot be used as exit evidence.

## MINORS

- **m1** Compose API is exposed on host **28000**, not 8000; `localhost:8000` is an unrelated `hive/serebo` API. Document in runbook to prevent false 0A smoke results (stack itself is healthy).
- **m2** FS-00 §6 still includes `POST /api/v1/admin/users` (line 663) though C-14/Δ-MT replace user creation with membership+invitation; keep spec/route consistent at C0.
- **m3** Endpoint-count semantics: spec says 102 endpoints; fragment 102 ops over 84 paths; runtime 106 ops over 78 paths (aliases/framework). Normalize before freeze.
- **m4** `/internal/deps` is part of the spec internal surface but unscoped in the live app; `/openapi.json` spec mention is framework-only. Mark as INFO framework exceptions in the route-scope matrix.

## Confirmed PASS evidence (not exit gate)
- Tables: 34/34 (`app/core/models/platform.py` metadata == FS-00 table list; revision lists 21+8+5=34); migrations 3 files, single head `0003`, applied.
- RLS/append/partition: 33 RLS tables/66 policies; 13 append-only triggers; `audit_event`/`outbox_event` partitioned (live DB).
- RBAC catalogue: 255 scopes = 241 assignable + 13 service-only + 1 platform-only; 7 templates; 14 SoD pairs; live seeds 255/7/591 role_permission/27 sequences/8 settings/17 flags.
- Tests: **unit 367/367 pass** with DB available; actual implementation integration suites (`platform_auth`, `platform_rbac`, `platform_sso`, `platform_api_keys`, `platform_admin`, `platform_config`, `platform_artifacts`, `platform_approval_actions`) all pass; only the stale `platform_data` seed assertion fails.
- Live stack: 7/7 healthy; `/healthz` 200; OpenAPI 78 paths; public `/auth/providers` 200; guarded `/admin/permissions` 401 problem+json.

## Ranked fix order before C0/0A
1. B1 — create/suite the 14 pytest gate targets at manifest paths (or Lead-approved manifest update); then rerun all 17 commands and record evidence.
2. B2 — reconcile routes (missing/extra/aliases/API-key revoke), regenerate FS-00 OpenAPI/event contract fragment, diff-to-empty, freeze C0.
3. B3 — add `app/tests/invariants/`, `app/tests/security/`, `app/tests/integration/platform/`, `finance_port/`, `contracts/test_fs00_provider_contract.py` covering RLS/append-only/MT isolation/route-scope/RBAC/SoD/SSO/contract.
4. M1 — add missing event emissions + 2 job definitions + handler registry; M2 — scope/allowlist fix for `/me/memberships` + aliases; M3 — fix stale seed test; then run full gate on current DB/stack.
5. Only then `tools/feature_sets.py record --set FS-00 --status verified` and 0A deploy.

## Evidence index (all in `verification/evidence/FS00-AUDIT/`)
`REPORT.md` (this file), `audit.py`, `static-audit.log/json`, `route-parity.log/json`, `contract-fragment.log`, `db-live.log`, `db-probe.log`, `live-stack.log`, `live-http.log`, `events-jobs.log`, `test-evidence.log`, `unit-tests.log`, `integration-tests.log`, `unit-static.log`, `integration-impl.log`.
