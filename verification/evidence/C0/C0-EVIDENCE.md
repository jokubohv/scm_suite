# C0 contract-freeze candidate — evidence

**Task:** task-49 (FS-00 C0 freeze candidate + route-scope coverage) · **Plan version:** 1.5.0
**Pre-C0 baseline:** `24a9729ff781995d162ed56063db7adab52e0568` (W1-stop: migrations 0001–0003, 11 platform routers, FS-00 services; contract aggregate all-skeleton `934 ops / 710 paths`)
**Artifacts:** FS-00 builder fragments derived from `app.openapi()`, regenerated committed contract artefacts, route-scope + provider contract tests, this evidence directory.
**Environment:** static work (no DB required for these gates). Docker/DB recovery does not affect the freeze evidence below.

## 1. Gate results (logs in this directory)

| # | Gate | Command | Result | Log |
|---|---|---|---|---|
| 01 | Builder fragments fresh | `python3 app/contracts/platform/build_from_app.py --check` | **PASS** (exit 0) | `01-builder-fragments.log` |
| 02 | Generated artefacts fresh | `python3 -m app.contracts.cli generate --check` | **PASS** (exit 0, `written: []`) | `02-generate-check.log` |
| 03 | Contract lint | `python3 -m app.contracts.cli lint` | **PASS** — errors=0, warnings=2 (`extensions.admin` unknown scope, FS-14) | `03-lint.log` |
| 04 | Contract tests (base + FS-00 provider/consumer + route scope) | `python3 -m pytest app/tests/contracts -q` | **PASS** — 33 passed | `04-contract-tests.log` |
| 05 | Route-scope coverage | `python3 -m pytest app/tests/contracts/platform/test_route_scope_coverage.py -q -rA` | **PASS** (pinned known shortfalls reported) | `05-route-scope-tests.log` |
| 06 | Route-scope strict governance (`DM-RBAC-01`) | `C0_STRICT_ROUTE_SCOPE=1 …` | **FAIL (expected blocker)** — 3 unmapped routes; exit 1 | `06-route-scope-strict.log` |
| 07 | Diff vs pre-C0 skeleton baseline | `python3 -m app.contracts.cli diff --baseline <pinned HEAD> --current contracts/openapi/openapi.json` | **exit 2** — pre-freeze normalization: 23 additive, 90 breaking (see §4) | `07-contract-diff.log` |
| 08 | Metrics snapshot | `python3 verification/evidence/C0/count_contracts.py --json …` | **PASS** | `08-counts.log`, `counts.json` |
| 09 | Lint machine-readable | `python3 -m app.contracts.cli lint --json` | **PASS** (exit 0) | `09-lint-json.log` |
| 10 | Git head at evidence time | `git log -1` | pre-C0 W1-stop commit | `10-git-head.log` |
| 11 | Working tree | `git status --short` | our files + unrelated seed/W2 work by other agents (not staged here) | `11-working-tree.log` |
| 12 | Artefact SHA-256 | `python3 verification/evidence/C0/hash_artifacts.py` | recorded | `12-artifact-sha256.log` |

Reproduce everything: `bash verification/evidence/C0/collect_evidence.sh`.

## 2. Frozen FS-00 contract surface (implementation-backed)

`app/contracts/platform/build_from_app.py` derives the builder fragment from the mounted app
(`app.openapi()`): real request/response schemas (component `$ref`s inlined), one catalogue scope per
route resolved from the `require_scope` dependency graph, allowlists for public/self-service routes,
and stable pre-C0 spec `operationId`s retained for operations the spec already declared. The generator
now lets these marked operations (`x-implemented: true`, `x-skeleton: false`) replace the spec
skeleton for the same path+method and append genuinely new implemented operations; unimplemented spec
operations remain `x-skeleton: true` + `x-todo`.

| Metric | Value |
|---|---|
| Mounted OpenAPI operations / paths (`app.openapi()`) | **106 / 78** |
| Builder fragment (`app/contracts/platform/openapi.fragment.json`) | **105 ops / 77 paths**, skipped unscoped: 1 (`GET /api/v1/me/memberships`) |
| FS-00 spec operations (§6) | 102 (77 overridden, 25 skeleton remaining → §3) |
| FS-00 fragment (`contracts/openapi/fragments/FS-00.openapi.json`) | **130 ops / 101 paths**, 105 implementation-backed |
| Aggregate OpenAPI | **962 ops / 727 paths** = 934 spec + 28 implemented additions (17 new paths); 857 skeletons |
| FS-00 added operations not in FS-00 §6 | 28 (additive; need spec/contract-review acceptance → §3.4) |
| Event registry | 21 contracts / **159 event schemas** (envelope-final; all payload schemas still `x-todo` → §3.3) |
| FS-00 contract events | 33; implemented outbox events: `platform.approval.requested|approved|rejected`, `platform.flag.changed` (all registered; 0 missing from registry) |
| Plan version | 1.5.0 (unchanged) |

## 3. Residual gaps blocking an unconditional C0 freeze (owner-assigned)

### 3.1 Route-scope governance (`DM-RBAC-01`) — **BLOCKER**
Strict run fails on 3 mounted routes that are neither catalogue-scoped nor allowlisted:

| Route | Owner | Reason |
|---|---|---|
| `GET /api/v1/me/memberships` | platform tenants router (task-39) + Lead (`design/permissions.json` self-service allowlist) | implemented self-service route missing from catalogue allowlist; excluded from contract until fixed |
| `GET /api/v1/auth/me` | FS-00 auth router (task-38) | compatibility alias `include_in_schema=False`; canonical `/api/v1/me` is allowlisted |
| `POST /api/v1/auth/invitations/{token}/accept` | FS-00 auth router (task-38) | compatibility alias; canonical allowlisted path is `/api/v1/admin/invitations/{token}/accept` |

Non-blocking coverage facts: 120 mounted operations = 99 scoped + 3 unresolved + 8 public + 6 self-service + 4 framework/operational; 0 duplicate routes, 0 multi-scope routes, 0 unknown route scopes.

### 3.2 FS-00 spec operations still skeleton (25) — **freeze content gap**
| Category | Ops (owner) |
|---|---|
| Order boundary stubs — no implementation (owner: FS-00 order boundary / Lead) | `GET /api/v1/order/demand/open`, `PUT /internal/order-boundary/demand-doubles` (contracts `order.demand`/`order.supply@0.1.0` remain stub-only) |
| Operational endpoints with path/schema drift (owner: integrator/contract tooling) | `GET /openapi.json` (app: `/api/v1/openapi.json`), `GET /internal/deps` (app route excluded from schema) |
| Implemented under a different path/param — spec §6 reconciliation needed (owner: FS-00 API owners / Lead) | `GET /audit-events`→`/admin/audit-events`; `POST /admin/api-keys/{key_id}/revoke`→`DELETE /admin/api-keys/{key_id}`; `POST /admin/invitations/{id}/revoke`→`…/{invitation_id}/revoke`; `GET /finance/journal-intents/{id}`→`…/{intent_id}`; `GET /admin/job-runs` + `GET /admin/job-runs/{run_id}`→`GET /admin/jobs/{job_name}/runs` |
| Not implemented (owner: FS-00 follow-up, Lead dispatch) | `GET /custom-field-values`; `PUT /admin/custom-field-values`; `GET /finance/period-status`; `GET /reference/exchange-rates`; `PATCH /admin/approval-definitions/{definition_id}`; `PATCH /admin/number-sequences/{sequence_id}`; `POST /admin/job-runs/{run_id}/cancel`; `POST /admin/jobs/{name}/run`; `POST /admin/users`; `POST /platform/tenants/{tenant_id}/bootstrap-admin`; `POST /platform/tenants/{tenant_id}/offboard`; `POST /internal/outbox/replay`; `PUT /admin/users/{user_id}/data-scopes`; `PUT /internal/finance/exchange-rates`; `PUT /internal/finance/fiscal-periods` |

### 3.3 Event payload TODO debt (159 schemas) — **freeze content gap**
All event schemas have the final outbox envelope but payload `data` remains `x-todo` (no payload schema can be derived generically from the registry stubs). FS-00 owns 32 of 159 (`platform.core` 31 + `platform.finance` 1); the 4 implemented outbox events still carry TODO payload schemas too. Other contracts' payload TODO counts are in `counts.json.events.payload_todo_by_contract` (owner: each FS contract owner; FS-00 subset owner: Lead/FS-00).

### 3.4 Additive implemented operations missing from FS-00 spec §6 (28) — **traceability gap**
`DELETE admin/api-keys/{key_id}`; `DELETE admin/custom-fields/{field_def_id}`; `DELETE attachments/{attachment_id}`; `DELETE document-links/{link_id}`; `GET admin/audit-events`; `GET admin/currencies`; `GET admin/currencies/{code}`; `GET admin/custom-fields/{field_def_id}`; `GET admin/fiscal-periods`; `GET admin/flags`; `GET admin/flags/{flag_key}`; `GET admin/jobs/{job_name}/runs`; `GET admin/sequences`; `GET approvals/{request_id}/status`; `GET document-links/{link_id}`; `GET finance/journal-intents/{intent_id}`; `GET internal/outbox/stats`; `GET platform/tenants/{tenant_id}/memberships`; `PATCH admin/currencies/{code}`; `PATCH admin/flags/{flag_key}`; `PATCH admin/tenant-settings`; `PATCH admin/tenant-settings/{namespace}/{key}`; `POST admin/api-keys/{key_id}/rotate`; `POST admin/currencies`; `POST admin/fiscal-periods`; `POST admin/invitations/{invitation_id}/resend`; `POST admin/invitations/{invitation_id}/revoke`; `POST document-links`.
Owner: FS-00 spec owner/Lead — add §6 rows (or declare as documented extensions) before freeze so the contract stays traceable to the spec.

### 3.5 Other residual items
- **Lint warning:** `extensions.admin` (FS-14 `GET/PATCH /api/v1/extensions/settings`) is not in `design/permissions.json`; owner FS-14/Lead (spec ↔ catalogue reconciliation). Non-blocking warning.
- **Aggregate status:** `info.x-status` remains `"skeleton"` with `x-status-detail` describing the C0 candidate (base contract tests assert the skeleton marker). Freeze-status wording decision: Lead/integrator.
- **Pre-C0 diff:** 23 additive, 90 breaking, 0 neutral, exit 2. Breaking entries are all **skeleton placeholder removals** (77 × generic `default` problem+json response, 13 × generic `200` on ops that actually return 201/204) — expected pre-freeze normalization, not consumer-visible API changes. Post-C0 policy: additive-only within major versions; freeze hashes in `12-artifact-sha256.log` / `counts.json.hashes`.
- **Contract-tooling deltas (outside the strict task-49 path list, no app behavior changed):** `app/contracts/generate.py` (implementation-backed override + FS-fragment overlay + counts), `app/contracts/lint.py` (fragment endpoint check allows implementation-backed supersets), `app/tests/contracts/base/test_contracts_infrastructure.py` (aggregate count assertion accounts for implemented additions). Required to make an implementation-backed C0 candidate expressible/generatable; flagged for Lead/integrator approval.
- **Provider/consumer tests added:** `app/tests/contracts/platform/test_fs00_provider_contract.py` (app↔fragment↔aggregate↔event registry) and `test_route_scope_coverage.py`; 33 contract tests pass.

## 4. Files changed by this task

- `app/contracts/platform/build_from_app.py` (new; deterministic fragment builder + `--check`)
- `app/contracts/platform/openapi.fragment.json` (105 implementation-backed operations)
- `app/contracts/order_boundary/openapi.fragment.json` (explicit stub placeholder, `x-status: pending-implementation`)
- `app/contracts/generate.py`, `app/contracts/lint.py` (contract-tooling support; see §3.5)
- `app/tests/contracts/platform/` (new: `conftest.py`, `contract_utils.py`, route-scope + provider contract tests)
- `app/tests/contracts/base/test_contracts_infrastructure.py` (count assertion for implemented additions)
- `contracts/openapi/openapi.json`, `.yaml` (962 ops/727 paths), `contracts/openapi/fragments/FS-00.openapi.json` (130 ops/101 paths), `contracts/README.md` (counts)
- `verification/evidence/C0/` (this directory)

Unchanged: app behavior/routers/services, migrations/models, specs, plan docs, `design/**`, other contracts' fragments (FS-01…FS-15 generated artifacts byte-identical except none), event registry/schemas.

## 5. Verdict

**CONDITIONAL C0 freeze candidate.** Contract tooling is clean (`generate --check`/lint/test all green; implementation-backed FS-00 surface merged deterministically; 33 contract tests pass), but the freeze is blocked on the owner-assigned residuals in §3 — most importantly the 3 unmapped routes (`DM-RBAC-01` strict failure), the 25 FS-00 spec skeletons/§6 reconciliation, and event payload TODO debt. Once those are resolved/accepted, regenerate and re-run `collect_evidence.sh`; the artefacts' SHA-256 values in `12-artifact-sha256.log` define the freeze fingerprint.
