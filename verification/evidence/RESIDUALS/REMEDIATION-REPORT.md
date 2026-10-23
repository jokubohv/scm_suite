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
| Contract aggregate vs runtime drift unclear | Added `tools/contract_runtime_diff.py` + `contract-runtime-diff.json`: classifies declared operations as mounted, implemented-but-unmounted (must be 0), or contract-first skeleton. | live **943** ops, aggregate **1317** ops; **0** implemented declared not mounted; **0** mounted not declared; **374** declared spec skeletons (documented). |

## Remaining accepted residuals

1. **374 contract-first skeleton operations** are declared in the spec/aggregate but not mounted as runtime routes (beyond the frozen exit-gate scope). No implemented operation is unmounted; no mounted operation is undeclared.
2. **FS-15 V-21 `v_available_to_build`** remains a typed empty materialized fallback: FS-09 computes ATB via API/contract but has no persisted cache table; adding one would change the verified 42-table FS-09 count (needs a plan amendment).
3. **Scheduler/job handlers** for seeded jobs (counts/planning/procurement/WMS/quality/consigned/extensions/reporting) and worker wiring remain operations work; definitions are seeded.
4. **Tenant provisioning** must clone new sequences/settings for sets 0013–0030; required-approval counts need a tenant `INVENTORY_COUNT` approval definition (absent → deliberate 409).
5. **FS-08/FS-05 landed-cost true-up**: estimate-vs-actual absorption variance and estimated-landed-cost reversal on receipt reversal are later recalculation/true-up work.
6. **FS-00 producer gaps**: some FS-00 events (`auth.*`, `sso.*`, `document.*`, `role.*`, `membership.*`, `tenant.*`, `finance.journal_intent.recorded`, `order.demand.changed`) have no outbox producer yet in `app/modules/platform*`; their payload schemas are structural, derived from the canonical registry payload keys.
7. Earlier accepted residuals unchanged: FS-04 partition deviation, FS-01/02 minor guards/type-migration workflow, runtime grant-role probes skipped in dev, legacy `inventory.*` settings prefixes.

## Evidence

- `verification/evidence/RESIDUALS/contract-runtime-diff.json`
- FS-07: `verification/evidence/FS07-EXIT/attempt-residuals.log`
- FS-08: `verification/evidence/FS08-EXIT/attempt-residuals.log`
- Full suite after remediation: **1185 passed, 1 skipped** (live Postgres, head `0030_rls_grants_verify`).
- Contracts: 21 contracts / 159 implementation-backed event schemas; `generate --check` PASS; `lint` PASS 0 errors / 0 warnings.
- Deployment state: **16/16 verified** unchanged.
