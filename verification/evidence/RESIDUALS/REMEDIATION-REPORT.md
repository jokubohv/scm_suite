# Residual remediation report (post-completion)

**Date:** 2026-10-23 · **Scope:** accepted residuals from `FINAL-AUDIT/REPORT.md`; no plan/count changes.

## Closed in this remediation batch

| Residual | Fix | Verification |
|---|---|---|
| Canonical event payload stubs (`x-todo`) | Generator now folds implementation-backed builder schemas into the canonical envelope: full-envelope schemas are copied/normalised; payload-only schemas are wrapped as `data`; extension contract inferred from the owning builder directory. Normalization stamps event/version/contract and removes `x-todo`. | **159/159** canonical schemas have implemented `data`; `x-todo` occurrences **0**; `generate --check` PASS; `lint` PASS 0 errors/0 warnings; contract tests 134 passed. |
| Tenant-setting namespace CHECK only allowed old namespaces | Widened the model CHECK (and live dev constraint via ALTER) to allow dedicated `counts`, `traceability`, `procurement`, `quality`, `consigned`, `extensions`, `reporting` namespaces. Fresh installs get the widened constraint; legacy seeds stay under `inventory.*` prefixes for N-1 compatibility. No migration renumbering. | `platform.tenant_setting` live constraint definition verified; migration roundtrip + full suite green. |
| FS-07 declarative approval null | New `app/modules/counts/approvals.py`; submit creates/links a real FS-00 `approval_request` when required, returns non-null id, 409 if the `INVENTORY_COUNT` definition is missing; approve/reject complete the request with SoD/state guards; auto/ tolerance path creates none. | FS-07 integration **18**, provider 6, invariants 7, unit 11; FS-00 approvals 18; FS-07 exit re-recorded with `attempt-residuals.log`. |
| FS-08 landed-cost bridge absent | Receipt charges create an estimated FS-05 landed cost and post **T-27** value-only on receipt posting; vendor-bill landed-cost lines create actual landed cost and post **T-26** on bill posting; deterministic ledger idempotency keys, document links, rollback/validation, qty unchanged. | FS-08 integration **9** (4 new), invariants 8, provider 8, unit 5; FS-05 integration 11/unit 15/provider 8; exit re-recorded. |
| FS-06 0013 down/up unverified | Scratch DB (`inv_scratch`): upgrade to 0013, downgrade to 0012, assert FS-03 registry objects remain, re-upgrade to 0013, drop. | Scratch run green; FS-03 tables preserved (3/3); final head 0013. |
| FS-15 V-21 `v_available_to_build` typed empty fallback | FS-09 `ManufacturingService.available_to_build()` now emits one transactional `available_to_build.calculated` outbox event per calculation with the authoritative atb-v1 result (`buildable_qty`, `limiting_component`/id, `per_level`, `policy_version=atb-v1`, deterministic `input_hash`, per-component inputs, `degraded=false`); V-21 was redefined as a pass-through materialised view over the latest `platform.outbox_event` `payload->'data'` row per tenant/assembly/location (no new table, no migration revision, no local ATB formula) and the live dev materialised view/index/refresh path was recreated; reporting service now reads materialised-view columns via `pg_attribute` so V-21 (and other MVs) are queryable. | FS-09 ATB event integration **3** (single emission per call, contract-schema validation of the real envelope, batch path); FS-15 V-21 pass-through integration **3** (latest-event selection, tenant isolation, malformed/unrelated events ignored, column allow-list, unique concurrent-refresh index); FS-15 unit guard **2** (SQL/source-guard has no local formula/cache table); full FS-09 (**27**) and FS-15 (**29**) suites green; live `reporting.refresh_materialized_views(false)` refreshed all 5 MVs incl. V-21. |
| Contract aggregate vs runtime drift unclear | Added `tools/contract_runtime_diff.py` + `contract-runtime-diff.json`: classifies declared operations as mounted, implemented-but-unmounted (must be 0), or contract-first skeleton. | live **943** ops, aggregate **1317** ops; **0** implemented declared not mounted; **0** mounted not declared; **374** declared spec skeletons (documented). |

## Remaining accepted residuals

1. **374 contract-first skeleton operations** are declared in the spec/aggregate but not mounted as runtime routes (beyond the frozen exit-gate scope). No implemented operation is unmounted; no mounted operation is undeclared.
2. **Scheduler/job handlers** for seeded jobs (counts/planning/procurement/WMS/quality/consigned/extensions/reporting) and worker wiring remain operations work; definitions are seeded.
3. **Tenant provisioning** must clone new sequences/settings for sets 0013–0030; required-approval counts need a tenant `INVENTORY_COUNT` approval definition (absent → deliberate 409).
4. **FS-08/FS-05 landed-cost true-up**: estimate-vs-actual absorption variance and estimated-landed-cost reversal on receipt reversal are later recalculation/true-up work.
5. **FS-00 producer gaps (remaining 3)**: `document.closed`/`document.reversed` are owned by the domain document sets (FS-04/FS-08/FS-09 close/reverse actions) and `finance.journal_intent.recorded` is owned by the FS-08/FS-13 intent writers; the other 26 declared FS-00 events now have transactional outbox producers.
6. Earlier accepted residuals unchanged: FS-04 partition deviation, FS-01/02 minor guards/type-migration workflow, runtime grant-role probes skipped in dev, legacy `inventory.*` settings prefixes.

## Evidence

- `verification/evidence/RESIDUALS/contract-runtime-diff.json`
- FS-07: `verification/evidence/FS07-EXIT/attempt-residuals.log`
- FS-08: `verification/evidence/FS08-EXIT/attempt-residuals.log`
- Full suite after remediation: **1185 passed, 1 skipped** (live Postgres, head `0030_rls_grants_verify`).
- Contracts: 21 contracts / 159 implementation-backed event schemas; `generate --check` PASS; `lint` PASS 0 errors / 0 warnings.
- Deployment state: **16/16 verified** unchanged.

## Wave 2 remediation (same date)

Additional closed residuals:

| Residual | Fix | Verification |
|---|---|---|
| FS-15 V-21 ATB empty fallback | FS-09 `available_to_build()` now emits a transactional `available_to_build.calculated` outbox event (atb-v1 result, per_level, deterministic input_hash); V-21 is a materialized pass-through over the latest event payload (no new base table/revision; live view recreated). | FS-09 27 / FS-15 29 passed, incl. exactly-one emission, latest-row/tenant-isolation and no-local-formula guards. |
| Seeded jobs had no worker handlers | Central `app/core/jobs` registry + `app/worker.py`: **57/57** seeded job definitions registered (38 implemented service handlers, 19 documented idempotent no-ops with reasons). Unknown job → error; tenant-scoped transactions, advisory locks, retry/timeouts. | 13 worker/registry tests green; coverage test fails on seed drift. |
| Tenant provisioning missing new sequences/settings + count approval | `app/core/tenant_provisioning.py` + create-tenant hook + `app/seed/backfill`: **38 sequences / 136 settings / 26 flags / 1 default INVENTORY_COUNT approval definition = 201 rows** per new tenant; idempotent replay inserts 0; existing-dev backfill CLI. | New tenant/replay/isolation/required-count tests + 82 FS-00 config/sequence tests. |
| Landed-cost true-up/reversal absent | Actual-vs-estimate absorption variance → **T-06** (value-only, costing variance rows, idempotent); receipt reversal → **T-27 reversal** through PostingPort (no double-reverse; rejected after actual). | 6 new true-up tests; FS-08 15 integration/8 invariants/8 provider/5 unit; FS-05 34. |
| Actual booked landed-cost/vendor-bill reversal + variance processing | Posted `OPEN` bill cancellation now reverses **T-04** (value-only) and all linked actual **T-26** landed-cost postings/FS-05 documents (REVERSED) and posted **T-06** absorption variances (costing rows CLOSED, reversal transactions), links `platform.document_link`, decrements PO `qty_billed` and sets the bill `VOID`; double reverse/paid/partially-paid/applied-credit/closed bills are rejected 409. After actual reversal a receipt estimate **T-27** reversal succeeds. Added service-level period-close/true-up handler `CostingService.process_landed_cost_absorption_variances()` (OPEN→CLOSED, idempotent, tenant-scoped; router exposure integrator-owned). | 5 new tests (3 bill/estimate reversal + 2 variance processing); FS-08 focused **45 passed**, FS-05 focused **43 passed**; qty unchanged on T-04/T-26/T-06 reversals; double/paid reversal rejected; `compileall`/`ruff`/`mypy` clean; contract lint `--no-freshness` 0/0. |
| FS-00 missing producers/routes | 26/29 declared FS-00 events now emitted transactionally (3 owner-blocked exceptions remaining); `POST /admin/users`, internal router (deps/outbox/finance/order-boundary) + canonical finance/invitation `{id}` routes added; internal router registered. `sod.violation.blocked` emitted from RBAC role/assignment/invitation and SSO group-mapping SoD-enforcement points; `sso.login.failed` emitted on SSO callback connection/tenant/ID-token failure paths with tenant context. | FS-00 52 tests; generated fragments/aggregate updated; contract lint 0/0. |
| 246 declared-unmounted routes (FS-03/04/05/06/07/08/09/10/12/13/14 + FS-00) | Closed via canonical `{id}` aliases and genuine gaps: FS-03 20/20, FS-04 45/50 (5 malformed spec paths), FS-05 30/35 (5 malformed), FS-06 8/8, FS-07 18/18, FS-08 62/62, FS-09 81/81, FS-10 40/40, FS-12 21/21, FS-13 13/13, FS-14 17/17; FS-00 alias routes added. Runtime/aggregate now: live **1251** ops, aggregate **1262**, **0** implemented-unmounted, **0** mounted-undeclared, **11** contract-first skeletons (10 malformed frozen paths + `/openapi.json` infra alias). | Each set’s route-surface tests + full suite **1287 passed, 1 skipped**; contract `generate --check`/`lint` PASS 0/0; runtime diff ok=true. |

### Remaining (documented, non-blocking)

1. **11 contract-first skeleton operations** — 5 FS-04 + 5 FS-05 malformed path-stitching defects in the frozen spec (no viable resource semantics; real `{id}` equivalents mounted) + hidden `/openapi.json` infrastructure alias.
2. **FS-00 event producer exceptions (3, owner-blocked)** — `document.closed`/`document.reversed` (real close/reverse action points live in the document-owning sets FS-04/FS-08/FS-09; no FS-00 action point without editing another set), `finance.journal_intent.recorded` (intent writers live in FS-08 `app/modules/procurement/service.py` and FS-13 `app/modules/consigned/service.py`; FS-00 owns only the read projection). `sod.violation.blocked` and `sso.login.failed` are now closed. |
3. **FS-04 deviations** — transaction/line/detail tables are regular (not partitioned); simplified approval/flag/data-scope enforcement; inline import rows; some serial/lot dependent reversal checks pending.
4. **FS-01/FS-02 minor guards** — type-migration workflow, UoM deactivation/in-use guards, matrix option-in-use checks, custom-field/EAV wiring; no gate failure.
5. **19 job no-ops** remain for document/API/event-driven jobs (costing, pricing, quality trigger/derived-field, retention, SSO monitor, ledger import/partition) — explicit reasons in `coverage_summary().noop_jobs`.
6. Legacy `inventory.*` settings prefixes retained (namespaces now allowed); dev DB lacks runtime grant roles (`platform_app` etc.) so live grant probes skip; tenant provisioning should run for existing tenants via `app.seed.backfill`.

### Wave-2 evidence

- Full suite: **1287 passed, 1 skipped**; contract generate `--check` PASS; lint PASS 0 errors/0 warnings.
- `verification/evidence/RESIDUALS/contract-runtime-diff.json` (ok=true, 11 skeletons).
- FS-00/05/06/07/08/09/10/12/13/14 route-surface test files under `app/tests/integration/*/test_*_route_surface.py` (renamed unique basenames to avoid pytest import collisions).
- Runtime-diff / aggregate excludes malformed paths only; no mounted operation is undeclared.

## Wave 3 remediation (same date)

| Residual | Outcome |
|---|---|
| FS-01/FS-02 minor guards/type migration | Closed: `convert-to-inventory` workflow, UoM/matrix in-use guards, custom-field/EAV wiring, planning-values durable idempotency, exact LOCATION data-scope filtering, latent location-replace bug; new guard suite 7, FS-01/02 suites green. |
| Remaining FS-00 event producers | Closed `sod.violation.blocked` and `sso.login.failed` producers; FS-00 coverage **26/29**. |
| Job no-op handlers | **46 implemented / 11 no-ops** of 57 seeded jobs; converted costing/pricing/quality/ledger sweeps; remaining no-ops are external/payload/migration-driven with explicit reasons. |
| Actual bill/landed-cost reversal + variance true-up | Posted vendor-bill cancel now reverses **T-04 + actual T-26 + T-06 absorption variance** atomically (paid/credit/double-reverse guards), unlocking receipt **T-27** estimate reversal; service-level `process_landed_cost_absorption_variances` closes OPEN T-06 rows. |
| FS-03/04/05/06/07/08/09/10/12/13/14 declared-unmounted routes | All closed except 10 malformed frozen spec paths; canonical aliases/genuine gaps mounted with route-surface tests. |

Final integrated gate: **1302 passed, 1 skipped**; contract `generate --check` PASS; lint **0 errors / 0 warnings**; runtime surface **1251 live ops / 1262 aggregate**, **0 implemented-unmounted / 0 mounted-undeclared**, **11 contract-first skeletons** (10 malformed frozen paths + hidden `/openapi.json` infra alias).

### Remaining residual floor (documented, plan-bound)

1. 11 frozen-spec/infrastructure skeleton operations (malformed path stitching; no viable resource semantics).
2. 3 owner-blocked FS-00 events: `document.closed`, `document.reversed` (document-owning sets), `finance.journal_intent.recorded` (writers are FS-08/FS-13 action points; FS-00 owns read projection only).
3. FS-04 deviations: transaction/line tables not partitioned; simplified approval/flag/data-scope enforcement; inline import rows; some cross-set serial/lot reversal checks.
4. Cross-set item-inactivation referent checks and full descendant/CLASS/DEPARTMENT/SUBSIDIARY data-scope expansion (need FS-03/FS-04 ports + Principal context wiring).
5. 11 job no-ops: external daemons (`platform.outbox.relay`, `platform.jobs.scheduler`), ops-only jobs, migration-owned partition maintenance, and payload/document-driven procedures.
6. FS-02 manifest exit command points at a missing combined test path (`test_fs00_fs01_fs02_contract.py`); equivalent quote-boundary consumer test passes (spec/test-path drift).
7. Dev DB runtime grant roles absent (`platform_app`/etc.), so live grant probes skip; legacy `inventory.*` settings prefixes retained; existing dev tenants need `app.seed.backfill`.
