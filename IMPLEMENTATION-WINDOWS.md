# Implementation Windows — S0 Kickoff Briefing

**Plan status:** v1.5.0 **VERIFIED PASS** (0 open findings). **No implementation code exists yet.**
**Implementation order:** S0 scaffold → S0-GATE → FS-00 → B2 → B3 → B4 wide build → B5; deployment stays wave-ordered.

## Schedule (one-off, America/New_York)

| Window | Time | Action |
|---|---|---|
| 1 | **2026-10-23 00:00 – 02:00** | Execute S0 scaffold tasks; stop at 02:00 |
| — | 02:00 – 06:00 | Paused; running work interrupted, state saved |
| 2 | **2026-10-23 06:00 →** | Resume S0; pass S0-GATE; begin FS-00 if ready (no stop time specified) |

Scheduled via session reminders (one-off for tonight):
- `schedule-28f5cb67-07d2-440b-8913-f66c5c85270e` — 00:00 start
- `schedule-a024574e-6c03-4723-bf8e-7853d9ce3df8` — 02:00 stop
- `schedule-c9533c70-9f74-4241-9a5f-28c4b40502d5` — 06:00 resume

This file is the shared briefing for each window.

## Guardrails (do not violate)

1. Read `PLAN.md` §7.5 and `design/PARALLEL-IMPLEMENTATION-ADDENDUM.md` first.
2. **S0 before FS-00.** Do not start FS-00 or any domain set until `python tools/check_parallel_plan.py --s0` reports execution-ready YES (this requires actual scaffold files to exist).
3. Only disjoint write scopes per agent; shared files (route registry, contract aggregation, Alembic chain order, `conftest`, seed, compose, scripts) are integrator-owned.
4. Plan docs (`design/**`, `feature-sets/**`, `PLAN.md`, `README.md`, manifest) are **read-only** during implementation.
5. No deployment beyond dev; deploy remains wave-ordered; no FS set is "done" until L2/L3 gates pass.
6. At each window stop: interrupt running teammates, record state, run available gates, update this file.

## S0 task execution order (from `design/parallel-plan.json`)

1. **S0-01** repo/toolchain/CI (`app/` layout, `pyproject.toml`, ruff/mypy/pytest/import-linter, `.github/workflows`, Makefile, `.gitignore`, route registry placeholders) — *scaffolder*.
2. **S0-03** Alembic base + chain guard (`alembic.ini`, `app/migrations/` env/template) — after S0-01.
3. **S0-04** contract-first infrastructure: OpenAPI aggregation, event JSON-Schema registry, **contract skeletons for all 16 FS**, provider/consumer harness — after S0-01 (largest S0 task).
4. **S0-02** local stack: `Dockerfile`, compose (api/worker/outbox-relay/postgres/redis/minio + Dex dev client/connector), env templates — after S0-01.
5. **S0-05** test harness: `conftest.py`, testcontainers/compose profiles, tenant fixtures, invariant/golden harness — after S0-01/S0-03.
6. **S0-06** scripts/gates: `deploy_set.sh`, `enable_flag.sh`, `provision_sso_dex.sh`, seed runner, check-env wrapper, worktree helper — after S0-02.
7. **S0-07** seed/reference bootstrap: SYSTEM_TENANT, permission catalogue + 7 role templates from `design/permissions.json`, currency/UoM/transaction types/statuses/flags/sequences; idempotent loader — after S0-03/S0-04.

**S0-GATE:** `python tools/check_parallel_plan.py --s0` (execution-ready YES) + all S0 exit checks from `design/parallel-plan.json`; record evidence under `verification/evidence/S0/`.
| 2 | 2026-10-23 09:14 | in progress | **FS-00 verified / 0A deployed**: C0 contract freeze recorded (`verification/evidence/C0/C0-FREEZE.md`), 17-command exit gate green (`verification/evidence/FS00-EXIT/attempt-3.log`), `deployment-state.dev.json` FS-00 verified; stack rebuilt/recovered; seed SQLAlchemy repo done; B2 started (FS-01 ∥ FS-02 build) | C0 gates: contract lint/generate/route-scope strict PASS; provider contract tests PASS; full suite green; Docker 7/7 healthy; `docker compose run api alembic upgrade head` PASS | FS-01/FS-02 in build (spec-wave0a/spec-wave0b); then C1 freeze + 0B deploy; FS-02 deploy after FS-01 |

| 3 | 2026-10-23 10:18 | in progress | **0B/0C deployed**: FS-01/02 verified (0B), FS-03/04/05 verified (0C); C1/C2 freezes recorded; B4a opened (FS-06/07/08 build) | contract lint/generate/route-scope PASS; full suite green (900+ tests); stack 7/7; heads 0012; deployment-state has FS-00..05 verified | B4a FS-06/07/08 implementation; then 1A/1B deploy + C3 freeze; B4b (FS-09..14) + FS-15 after |

| 4 | 2026-10-23 11:00 | in progress | **B4a deployed/verified**: FS-06/FS-07 (1A), FS-08 (1B) verified; C3 freeze recorded; B4b opened (FS-09…FS-15 build, migration slots 0018–0030 serialized) | full suite green (1000+ tests); contract lint/generate PASS; stack 7/7; head 0017; deployment-state FS-00..08 verified | B4b agents: FS-09/10/11/12/13+14/15; then deploy 1C→1F/2, C4/C5 freeze, final verification |

| 5 | 2026-10-23 11:30 | in progress/final | **All 16 sets deployed/verified** (waves 0A–2); C3/C4/C5 freezes; full suite **1163 passed/1 skipped**; contract lint 0/0; final audit running | deployment-state 16/16 verified; head 0030; app OpenAPI 719 paths/943 ops; dev stack 7/7 | Independent final audit (`verification/evidence/FINAL-AUDIT/`), clean commit and push to https://github.com/jokubohv/scm_suite.git |

| 6 | 2026-10-23 12:50 | in progress/final | **Residual remediation batch**: 159/159 event payloads synced, namespace CHECK widened, FS-07 approvals + FS-08 landed-cost bridge wired, FS-06 0013 scratch down/up verified, contract/runtime drift checker added; full suite 1185 passed/1 skipped | FS-07/08 re-verified + re-recorded; contract lint 0/0; 16/16 deployment state unchanged | Commit/push residual fixes; remaining residuals documented in `verification/evidence/RESIDUALS/REMEDIATION-REPORT.md` |

| 7 | 2026-10-23 13:35 | in progress/final | **Residual wave 2**: FS-15 V-21 ATB pass-through, job registry 57/57, tenant provisioning 201 rows/tenant, landed-cost T-06/T-27 true-up, FS-00 producers/routes, 235/246 declared-unmounted routes closed with canonical aliases; remaining 11 frozen-spec/infra skeletons | full suite **1287 passed/1 skipped**; contract generate/lint PASS 0/0; live ops 1251 vs aggregate 1262; 0 implemented-unmounted/0 mounted-undeclared; route-surface tests added | Commit/push wave 2; document final residual register |


## Suggested agent assignment (reuse inactive teammates; 8-member cap)

| Agent (reused) | Tasks |
|---|---|
| scaffolder owner | S0-01, S0-03, S0-06 |
| contract owner | S0-04 |
| infra owner | S0-02 |
| harness/seed owner | S0-05, S0-07 |
| Lead | coordination, shared-file merge queue, gate runs, progress log |

Up to 3 concurrent file-writing agents in S0 (disjoint scopes). Do not exceed 8 durable teammates; batch extra work.

## Progress log

| Window | Start | End | Completed | Gates | Open / next |
|---|---|---|---|---|---|
| 1 | 2026-10-23 00:00 | 02:00 (scheduled stop) | **S0-01…S0-07 complete + S0-GATE PASS**; **B1/FS-00 substantially built**: migrations 0001–0003 + 34 models; security services (60 tests/~93%); 11 registered API routers (auth/me, RBAC, SSO, config, platform tenants, approvals+decide, API keys, artifacts, ops); core services (sequences/flags/settings/idempotency/custom fields/outbox+relay/approvals); live-stack smoke passed | `check_parallel_plan --s0`=YES; plan gates PASS; compileall/ruff/mypy clean; full pytest green; chain guard 3 revisions; contract lint PASS; Docker image rebuilt + 7/7 stack healthy (later degraded); e2e 401/200 smoke | Disk 1.8GiB free; compose stop cleanup failed; seed SQLAlchemy repo still stub (task-48 queued); FS-00 contract freeze C0/exit gate/0A deploy pending. See `verification/evidence/W1/STATUS.md` |

## Useful commands

```bash
cd inventory-platform
python tools/check_parallel_plan.py --s0     # execution-ready (currently NO: 9 scaffold paths missing)
python tools/check_parallel_plan.py          # plan graph
python tools/feature_sets.py validate
python tools/feature_sets.py permissions
python tools/feature_sets.py coverage
python tools/render_plan_tables.py --check
```

Implementation repo root: `inventory-platform/app/` (framework §3 layout); plan package stays at `inventory-platform/`.
