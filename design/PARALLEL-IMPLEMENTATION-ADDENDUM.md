# Parallel Implementation — Binding Execution Addendum (Δ-PAR, plan v1.4.0)

**Status:** Lead-approved binding amendment to the verified v1.3.0 package. Supersedes framework §8's
"one feature set at a time" wording. Where this addendum conflicts with earlier text, **this addendum wins**.
**Origin:** user requirement — make the plan ready for implementation by as many subagents as possible.
**Scope:** execution/process amendment only. **No schema, contract, rule, invariant, scope or feature-set
change:** 280 tables (platform 34), 21 views, 31 posting types + 6 commands, 143 scoped + 10 global = 153
rules, INV-01..INV-32, RBAC catalogue 251 = 237 assignable + 13 service-only + 1 platform-only (+alias),
7 role templates, 14 SoD pairs, plan_version **1.4.0**. The feature-set DAG, gates, coverage and all 16
specs are unchanged.

Authoritative companions: [`design/parallel-plan.json`](parallel-plan.json) (machine-readable task graph,
write scopes, contract freezes, phases, roles) and [`tools/check_parallel_plan.py`](../tools/check_parallel_plan.py)
(executable readiness validator). Framework §8, PLAN §7.5 and README are updated to reference this addendum.

---

## 1. Core change: parallel **build**, wave-ordered **deploy**

v1.3.0 conflated the two. Δ-PAR separates them:

1. **Build may run in parallel** across feature sets whose *write scopes are disjoint* and whose
   *dependency contracts are frozen* (contract-first; §3). Up to **8 concurrent FS builders** (+1 integrator,
   +1 verifier, +1 e2e = fleet of up to ~11; DSH session cap is 8 durable teammates, so run 8 durable
   builders and use `workflow` fan-out for short-lived verifier/e2e/audit tasks, or batch in two groups).
2. **Deployment remains strictly wave-ordered** exactly as `feature-sets.json` defines
   (`0A → 0B → 0C → 1A … 1F → 2`); a set is deployed only after its manifest dependencies are `verified`
   (`check-env` L2 gate). Build completion never implies deployment.
3. **A set is "build-complete"** when its module + migrations + contracts + tests are merged and its L0/L1
   gates pass against frozen dependency contracts; it becomes **"done"** only after L2 deploy + L3 exit gate.

## 2. S0 — serial/semi-parallel scaffold phase (precondition, "execution-ready" gate)

No FS builder can start until **S0-GATE** passes. S0 tasks are defined in `parallel-plan.json` (7 cards,
max 3 concurrent after S0-01):

| Card | Deliverable | Write scope | Depends on |
|---|---|---|---|
| S0-01 | Repo/toolchain/CI: `app/` layout, `pyproject.toml` (ruff/mypy/pytest/import-linter), `.github/workflows`, `Makefile`, `.gitignore` | repo root config, `.github/` | — |
| S0-02 | Local stack: `Dockerfile`, `docker-compose.yml` (api/worker/outbox-relay/postgres/redis/minio + Dex dev client/connector), env templates, healthchecks | `Dockerfile`, compose files, `ops/dex/` | S0-01 |
| S0-03 | Alembic base + migration-chain guard (single chain, revision-lint, empty baseline; domain revisions owned by FS cards) | `alembic.ini`, `app/migrations/` (env/template only) | S0-01 |
| S0-04 | **Contract-first infrastructure:** OpenAPI aggregation per FS, event JSON-Schema registry, **contract skeletons for all 16 FS from their specs**, provider/consumer harness, schema/version lint | `contracts/`, `app/contracts/` (registry + generated), contract tests base | S0-01 |
| S0-05 | Test harness: `conftest.py`, testcontainers/compose profiles, tenant fixtures, DB reset, invariant/golden harness, per-FS test-dir convention | `app/tests/conftest.py`, `app/tests/utils/`, `app/tests/fixtures/` | S0-01, S0-03 |
| S0-06 | Gates/scripts: `deploy_set.sh`, `enable_flag.sh`, `provision_sso_dex.sh`, seed runner, `check_env` wrapper, worktree/merge helper, L0-L3 orchestration | `scripts/` | S0-01, S0-02 |
| S0-07 | Seed/reference bootstrap: SYSTEM_TENANT, permission catalogue + 7 role templates from `design/permissions.json`, currency/UoM, transaction types/statuses/flags, number-sequence templates; idempotent loader | `app/seed/` | S0-03, S0-04 |

**S0-GATE / execution-ready checklist** (validated by `check_parallel_plan.py`, `--s0`):
repo and CI exist; compose+Dex up; platform migration chain guard green; 16 contract skeletons generated
and lint-clean; test harness green on empty stack; scripts executable; seeds load twice without diff;
`feature_sets.py validate|permissions|order` green from the repo; every FS card's write scopes disjoint
from every other card and from shared files; migration revision map matches the manifest; contract-freeze
points declared; roles/fleet assignments defined. Record `S0-GATE: execution-ready` in the integration log.

## 3. Contract-first and freeze points

S0-04 generates the **contract skeletons from the plan specs** (OpenAPI fragments + event JSON Schemas +
job names + seeds) for all 16 FS. Builders implement against these frozen interfaces; contract changes go
through change control (§6). Freeze points:

| Freeze | When | Artifacts frozen | Unblocks |
|---|---|---|---|
| **C0** | FS-00 build merged + contract tests green (before 0A deploy) | `platform.core/events/finance`, `order.demand/supply`, auth/SSO contracts | FS-01/02 build |
| **C1** | FS-01 + FS-02 merged, C0 deployed verified (0B) | item.master, pricing.engine | FS-03/04/05 build |
| **C2a** | FS-03 merged; FS-04 ledger contract implementation green on branch | traceability.registry, inventory.ledger/balances | FS-05 build |
| **C2** | FS-03/04/05 merged; 0C deployed verified | ledger, costing, registry | **wide build wave (B4)** |
| **C3** | FS-06/07/08 contracts implemented and green (before deploy 1A/1B) | traceability.numbered, counts.engine, procurement.p2p | FS-09/13/14 build |
| **C4** | FS-09 contract implemented and green | manufacturing.execution | FS-10/11/12 build |
| **C5** | FS-10/11 contracts green | replenishment.planning, wms.execution | FS-15 build |

Freeze evidence: provider contract tests + OpenAPI/JSON-Schema diff vs last frozen version +
`feature_sets.py who-depends-on`. After freeze, additive-only changes within a major version are allowed
with contract-owner review; breaking changes require version bump + consumer migration + re-freeze.

## 4. Build phases, cards and concurrency

Exactly one card per feature set (FS-13+FS-14 may be one card if the same builder owns both; FS-00 may split
into two disjoint sub-cards as defined in `parallel-plan.json`). Full card definitions (write scopes,
migration revisions, exits, owner role) are in `parallel-plan.json`; summary:

| Phase | Cards (build order) | Max concurrent builders | Deploy gate |
|---|---|---|---|
| B0 | S0-01 → {S0-02, S0-03, S0-04} → {S0-05, S0-06, S0-07} | 3 | S0-GATE |
| B1 | PAR-00 (FS-00; optional split 00A/00B) | 2 | wave 0A |
| B2 | PAR-01 (FS-01) ∥ PAR-02 (FS-02) | 2 | wave 0B |
| B3 | PAR-03 (FS-03) → PAR-04 (FS-04) ∥ PAR-05 (FS-05 after C2a) | 3 | wave 0C |
| B4 (wide) | PAR-06 FS-06, PAR-07 FS-07, PAR-08 FS-08, PAR-09 FS-09, PAR-10 FS-10, PAR-11 FS-11, PAR-12 FS-12, PAR-13 FS-13+FS-14 | **8** (contract-first fast lane; C3/C4/C5 govern merge readiness) | waves 1A–1F, then 2 |
| B5 | PAR-15 (FS-15 reporting) — may run concurrently with B4 tail | 1–2 | wave 2 |

- Phase B4 is the "as many subagents as possible" lane: all eight cards have disjoint module/migration/
  contract-fragment scopes; consumers build against frozen C2 contracts and S0 skeletons; providers (FS-08/09)
  implement their contract early so dependants can run provider/consumer tests. If a consumer needs a not-yet-
  implemented provider behavior, it uses the S0 contract mock; integration happens when the provider card
  merges. **FS-15 starts after FS-10/11 contracts freeze (C5) or concurrently with B4 tail using read-model
  contract skeletons** — never before its read-model inputs have frozen contracts.
- Deployment does not exceed one set at a time (integrator), regardless of build concurrency, **except the
  manifest-specified parallel group at wave 1F (FS-12 ∥ FS-13 ∥ FS-14)**.

## 5. Write-scope and shared-file ownership

Builders own only their FS namespaces (defined per card in `parallel-plan.json`):

- `app/modules/<context>/**`, `app/api/routers/<context>.py` (or `<context>/` package),
  `app/contracts/<context>/**`, `app/tests/{unit,integration,invariants,contracts}/<context>/**`,
  and the migration revisions assigned to that FS **only when the integrator has allocated their numbers**.
- Plan package (`design/**`, `feature-sets/**`, `PLAN.md`, `README.md`, `research/**`, `verification/`),
  `design/feature-sets.json`, framework, and this addendum are **read-only for builders** (Lead/verifier own).
- Shared implementation files are owned **only by the integrator/contract owner/scaffolder**, never by
  concurrent builders: `app/api/main.py` + `app/api/registry.py` (route registration), `app/core/**`
  (FS-00 owner/exclusive, then integrator), `app/contracts/registry` + `contracts/openapi/openapi.yaml`
  (aggregator; per-FS fragments are builder-owned), `alembic.ini`, `app/migrations/env.py` +
  `versions/` ordering, `app/tests/conftest.py`, `app/tests/utils/`, `app/seed/**`,
  `pyproject.toml`, `Dockerfile`, `docker-compose*.yml`, `.github/workflows/**`, `scripts/**`,
  `ops/dex/**`.
- Registration convention: each FS exports a `router` and a contract-fragment manifest in its own package;
  the integrator adds one import line in `app/api/registry.py` when merging (serialized merge window), or
  the registry auto-discovers modules via a stable list owned by the integrator. No builder edits shared
  registration files.

## 6. Branch, worktree, merge and migration protocol

1. **Worktrees:** each card gets `git worktree add ../.worktrees/PAR-xx -b feat/PAR-xx <integration-base>`;
   agents work only in their worktree (shared filesystem is fine because directories are disjoint).
2. **Base branch:** per-phase integration branch `integration/B0…B4`; builders PR to it; integrator merges
   in topo/contract order. `main` only receives verified phases.
3. **Contract PRs** carry provider + consumer contract tests and diff vs frozen schema; contract owner
   (or integrator) reviews. Breaking contract changes are not allowed inside a phase.
4. **Migrations:** one linear Alembic chain. Revision numbers are assigned per FS by the DATA-MODEL §21.1 map.
   A builder may create its revision files in its branch, but merge order is enforced by the integrator
   (B0 chain: 0001–0003; B2: 0004–0006; B3: 0007–0012; B4: 0013–0028; B5: 0029–0030). Concurrent
   branches must not reuse a revision number; integrator rebases the chain in order and runs `alembic
   upgrade head` + downgrade test on the integration branch. No down-migrations in prod.
5. **Merge window & cadence:** integrator runs a merge train at least daily; builders rebase on the
   integration branch; conflicts in shared files are resolved only by the integrator. CI per PR runs L0/L1
   (`ruff/mypy/pytest`, contract tests, migration lint); integration branch runs compose integration suite.
6. **Feature flags:** each set's paths are flag-gated (`*.enabled` default off); build merges do not enable
   anything. Deployment enables flags per tenant after L3 evidence.

## 7. Environment isolation and resource guards

- Per-builder compose project/namespace: `COMPOSE_PROJECT_NAME=inv-PAR-xx`, dedicated DB
  `inv_par_xx` (or per-agent testcontainers), Redis DB offset, MinIO bucket, port ranges from env; no two
  builders share a running stack. `scripts/dev_env.sh`/`scripts/up.sh` provision the isolated stack.
- Testcontainers preferred for unit/integration; compose stack for acceptance/L3 only.
- Resource guard: max 8 durable file-writing agents; preflight `df`/memory/container limits, cap log/image
  retention, `docker system prune` in the daily runbook, keep evidence artifacts small (the plan package
  once hit ENOSPC — treat disk headroom as a gate, not an afterthought).
- Runtime cap note: DSH sessions allow 8 durable teammates; for >8 parallel workers use `workflow` fan-out
  with short-lived tasks (verifier/e2e/audits) or batch builders in two groups; file-ownership rules still apply.
- Each builder reports: branch, card id, changed paths, gates run, contract versions used, open questions.

## 8. Fleet composition and responsibilities

| Role | Count | Owns |
|---|---|---|
| Scaffolder/CI owner | 1 | S0-01/02/03/06, contract infra S0-04 support, CI |
| Seed/test-harness owner | 1 | S0-05/07 |
| Platform builder (FS-00) | 1–2 | PAR-00 (optionally split 00A identity/RBAC/SSO, 00B core/events/jobs/audit) |
| FS builders | up to 8 | one card each in B4 (B1–B3 as scheduled) |
| Integrator | 1 | shared files, migrations merge train, deployments, manifest/PLAN status, flags |
| Independent verifier | 1 | L1/L2/L3 evidence, contract/coverage audits, adversarial checks (never same FS author) |
| E2E/golden owner | 1 | cross-set golden workflow journeys, perf/concurrency smoke; starts after FS-04 |
| Contract owner | 1 (can be integrator/scaffolder) | contract freezes C0–C5, diff gates, consumer impact |

## 9. Definition of execution-ready (go/no-go)

`python tools/check_parallel_plan.py --s0` green **and**:
- [ ] Repo scaffold exists and CI runs plan gates (`validate`, `permissions`, `order`) on every PR.
- [ ] Auth/SSO dev path up (Dex dev client + test connector) and compose stack healthy.
- [ ] 16 contract skeletons generated/lint-clean; C0–C5 schedule recorded; `permissions` tool green.
- [ ] Migration chain guard green; revision allocation table (DATA-MODEL §21.1) encoded in the plan.
- [ ] Test harness + seed loader green on empty DB; golden fixture skeleton present.
- [ ] Write-scope matrix and shared-file ownership accepted; integrator/verifier roles assigned.
- [ ] Environment isolation script + resource guard in place.
- [ ] This addendum + PLAN/README references updated; independent verifier sign-off.

Only then may B1 (FS-00) start. FS-00 may not start "while scaffold is being built" except S0-01/03/04 owner
(same agent) if they are explicitly the PAR-00 owner and the S0-GATE is declared once.

## 10. Document update map (binding)

| Document | Change |
|---|---|
| `design/PARALLEL-IMPLEMENTATION-ADDENDUM.md` | this file (authoritative) |
| `design/parallel-plan.json` | machine-readable S0 + FS cards, write scopes, migration revisions, contract freezes, phases, roles, shared ownership, max concurrency |
| `tools/check_parallel_plan.py` | validator for the above against manifest + DATA-MODEL migration map + S0 gate |
| `design/FEATURE-SET-FRAMEWORK.md` | §8 execution model rewritten: parallel build with frozen contracts, wave-ordered deploy; link Δ-PAR |
| `PLAN.md` | header plan v1.4.0; §3.1 Δ-PAR row; §7.1 preconditions add S0; new §7.5 parallel build protocol; §12 execution assumption; status after verification |
| `README.md` | readiness line + S0/parallel execution pointer; status |
| `design/feature-sets.json` | `plan_version` → 1.4.0 only; no set/edge/contract/flag/gate changes |
| `verification/VERIFICATION-REPORT.md` | Δ-PAR delta audit, validator evidence, refreshed snapshot, verdict |

No feature-set, contract, table, rule, invariant, scope, wave or coverage changes. `S0` tasks are execution
tasks, not feature sets; the 16-set DAG and its gates remain authoritative for deployment.
