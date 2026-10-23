# C5 — Freeze record (quality / consigned / extensions / reporting provider contracts)

**Status: FROZEN** · plan v1.5.0 · 2026-10-23 · Lead approval
**Deployment:** FS-12, FS-13, FS-14 (wave 1F) and FS-15 (wave 2) recorded `verified`; all 16 feature sets verified.

## Gates
- Contract generate --check / lint: PASS (0 errors, 0 warnings).
- FS-12/13/14/15 provider + exit suites green; evidence `verification/evidence/FS-{12,13,14,15}-EXIT/attempt.log`.
- Final full suite: **1163 passed, 1 skipped** (live Postgres, alembic head `0030_rls_grants_verify`).
- Deployment state: 16/16 sets `verified`; waves `0A→0B→0C→1A→1B→1C→1D→1E→1F→2` complete.
- Contract aggregate: 21 contracts / 159 event schemas; app OpenAPI 719 paths / 943 operations.

## Frozen contracts
- FS-12 `quality.management@1.0.0` (10 tables, 0026)
- FS-13 `consigned.inventory@1.0.0` (5 tables, 0027)
- FS-14 `extensions.registry@1.0.0` (7 tables, 0028)
- FS-15 `reporting.readmodels@1.0.0` (21 views, 0029–0030)

## Accepted residuals
V-21 `v_available_to_build` typed empty fallback until FS-09 persists an ATB cache; spec/catalogue scope variance (`reporting.admin`, `extensions.admin`) seeded against the frozen 255-scope catalogue; canonical event payload TODO stubs with real schemas under `app/contracts/*/events/`; worker/scheduler handler wiring; FS-00/other-set seed role links when seed loader runs; FS-14 manifest command `docker compose run --restart=no` unsupported locally — equivalent `--rm` used in evidence (documented).
