# FS-XX — <Feature Set Name>

> **Template for implementation specs. Copy this file to `feature-sets/FS-XX.md` and fill it.**
> Authority: `design/FEATURE-SETS.md` + `design/feature-sets.json` (ids, deps, contracts, gates) and
> `design/DATA-MODEL.md` (tables, rules, invariants). This spec must not contradict them; if it does,
> the manifest/data model wins and the conflict is reported to the Lead.
> Backend only. No UI/mobile/analytics/integration build scope. SCM Mobile excluded.

| Field | Value |
|---|---|
| FS id / name | FS-XX — <name> |
| Phase / wave | 1 / <0A\|0B\|0C\|1A…1F\|2> |
| Depends on | `<FS-YY> <contract> <range>` … (must match manifest) |
| Provides | `<contract> vX.Y.Z` … |
| Tables owned | `schema.table`, … (count) |
| Size | S / M / L |
| Status | planned \| in-progress \| deployed \| verified |

## 1. Objective and source coverage

2–4 sentences: the backend capability this set delivers and why it is independently valuable.

Source topics (catalog ids + URLs; must be the ones assigned in `FEATURE-COVERAGE.md`):

| Catalog ids | Topic / chapter | Doc URL |
|---|---|---|
| `iri-…`/`mpw-…` | … | … |

## 2. Scope

**In scope (backend):** bullet list of services/behaviors.
**Out of scope / deferred:** bullet list (UI, mobile, analytics, integrations, SuiteApp UI, phases).

## 3. Dependencies and contracts

| Depends on | Why (used for) | Contract | Range | Entry-gate command |
|---|---|---|---|---|
| FS-YY | … | `contract.name` | `>=x <y` | `python tools/feature_sets.py check-env --set FS-XX --env $ENV` |

Provided contract surface (what dependants may rely on):

| Contract | Version | Surface (API paths / events / read models / jobs / seeds) |
|---|---|---|
| `domain.concept` | 1.0.0 | … |

Contract rules: additive changes only within a major version; breaking change = new version + consumer
impact list from `tools/feature_sets.py who-depends-on --contract <name>`.

## 4. Data model

Owned tables (from `design/DATA-MODEL.md`; repeat only the columns that matter to this set's rules):

| Table | Purpose | Key columns | Constraints / indexes | Notes |
|---|---|---|---|---|
| `schema.table` | … | … | … | … |

- Migration: `alembic:<NNNN>_<slug>` (must match manifest `migrations`); additive only; down-migration
  tested; N-1 compatibility.
- Seeds / reference data: list with codes/values.
- Deferred FKs / cross-context references: list (see DATA-MODEL convention §1.7).

## 5. Services, rules and invariants

Use cases (service method → behavior):

| Use case | Inputs | Behavior / validation | Emits |
|---|---|---|---|
| … | … | … | event, ledger transaction, job |

Business rules and invariants (cite DATA-MODEL rule ids where available, e.g. `INV-LEDGER-03`):

- `RULE-…`: testable statement.
- …

State machines (if this set owns documents): states, transitions, guards, side effects.

## 6. API surface

Base path `/api/v1`, JSON, UTC timestamps, quantities as decimal strings. Auth scope per endpoint.
Errors use the platform error envelope (`code`, `message`, `details`, `correlation_id`).

| Method | Path | Purpose | Request (key fields) | Response (key fields) | Auth scope | Idempotency |
|---|---|---|---|---|---|---|
| POST | … | … | … | … | `scope` | `Idempotency-Key` required |

OpenAPI fragment: `contracts/openapi/FS-XX.yaml` (additive to the versioned spec).

## 7. Events and jobs

| Event | Version | Trigger | Payload (key fields) | Consumers |
|---|---|---|---|---|
| `domain.entity.action` | `v1` | … | … | FS-… |

| Job | Schedule | Purpose | Idempotent? | Failure handling |
|---|---|---|---|---|
| … | cron/on-demand | … | yes | retry/backoff, DLQ, alert |

## 8. Configuration and flags

| Flag / setting | Default | Scope | Purpose |
|---|---|---|---|
| `<domain>.<feature>.enabled` | off | tenant | Deploy-dark then enable |
| … | … | … | policy/versioned formula ids (AIM/ATP), negative inventory policy, cost recalc policy |

## 9. Implementation steps (ordered)

1. Migration + tables/constraints + seed.
2. Domain services + rules + invariant tests.
3. API endpoints + validation + auth + idempotency.
4. Events/outbox + jobs.
5. Contract tests (provider) + consumer fixtures.
6. Gate wiring: entry/exit checks in `feature-sets.json`; deploy-dark, verify, record.

## 10. Test plan

| Layer | Tests (names / files) | Proves |
|---|---|---|
| Unit | `tests/unit/<module>/…` | rules, math, state machines |
| Invariant | `tests/invariants/test_…` | DATA-MODEL invariant ids |
| Contract | `tests/contracts/test_FS-XX_provider.py`, consumer fixtures | provided/received contracts |
| Concurrency | … | competing postings / idempotency |
| Migration | up/down empty + seeded; N-1 | safe deploy/rollback |
| Golden corpus | fixtures with expected outputs | costing/UoM/planning math |

## 11. Deployment and gates

```bash
python tools/feature_sets.py validate
python tools/feature_sets.py check-env --set FS-XX --env $ENV      # entry gate (L2) — must pass
scripts/deploy_set.sh FS-XX $ENV                                    # migrations → api/worker → exit checks
python tools/feature_sets.py record --set FS-XX --env $ENV --status verified   # after L3 evidence
```

Exit evidence (stored under `verification/evidence/FS-XX/`): migration log, smoke test output,
contract test output, invariant test output, deployment-state diff, verification sign-off.

## 12. Rollback

- Flag off (immediate, no data loss); redeploy prior image.
- Migrations additive/N-1 compatible; no prod down-migration.
- Data compensation: forward-fix document/reversal; ledger corrections only via compensating
  transactions (never delete).
- Dependants automatically blocked by `check-env` while status ≠ verified.

## 13. Definition of done (backend)

- [ ] Tables/migrations + seeds merged; constraints/indexes per DATA-MODEL.
- [ ] Services/rules implemented; `INV-*` tests green.
- [ ] API + OpenAPI fragment; provider contract tests green; consumer fixtures for dependencies green.
- [ ] Events/outbox + jobs with retry/DLQ; event schemas published.
- [ ] Flag wired default-off; tenant-isolation test green; audit events emitted; RBAC scopes enforced.
- [ ] Migration up/down tested; N-1 compatibility checked.
- [ ] Entry/exit gate commands added to manifest and passing in `dev`.
- [ ] `verification/evidence/FS-XX/` complete; `record --status verified`.
- [ ] Runbook (this file) updated with any deviations; PLAN.md status updated by Lead.

## 14. Open questions / assumptions

| # | Question | Default assumption | Needs |
|---|---|---|---|
| 1 | … | … | Lead / source doc |

<!-- END TEMPLATE -->
