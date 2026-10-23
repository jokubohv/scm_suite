# Inventory / SCM Backend — Executable Plan Package

An executable, dependency-checked plan to build a NetSuite-like **inventory and supply-chain core backend**,
derived from the official Oracle NetSuite SCM help areas (minus SCM Mobile), split into
**16 independently deployable feature sets** across **10 waves**.

> **Phase 1 = core backend logic only.** No UI, mobile, analytics, BI or external integrations are built
> in this phase; SCM Mobile is permanently excluded. Everything deferred is tracked topic-by-topic.

## Read in this order

| Step | Document | What you get |
|---|---|---|
| 1 | [`PLAN.md`](PLAN.md) | Scope, execution order, gates, runbook, risks, status. **Start here.** |
| 2 | [`design/FEATURE-SET-FRAMEWORK.md`](design/FEATURE-SET-FRAMEWORK.md) | Binding conventions: manifest schema, dependency-check protocol, deploy protocol, stack, DoD. |
| 2b | [`design/MULTI-TENANT-USERS-ADDENDUM.md`](design/MULTI-TENANT-USERS-ADDENDUM.md) | Binding Δ-MT amendment: multi-tenant user identity/membership model, auth/tenant switch, invitations/sessions, tenant admin APIs, isolation matrix. |
| 3 | [`design/FEATURE-SETS.md`](design/FEATURE-SETS.md) | The 16 feature sets, dependency DAG, wave plan, critical path, gates. |
| 4 | [`design/feature-sets.json`](design/feature-sets.json) | Machine-readable single source of truth for the DAG/gates. |
| 5 | [`design/ARCHITECTURE.md`](design/ARCHITECTURE.md) + [`design/DATA-MODEL.md`](design/DATA-MODEL.md) | Posting engine, costing, platform; 287 tables (platform 34) + 21 views, 31 posting types, invariants. |
| 6 | [`design/FEATURE-COVERAGE.md`](design/FEATURE-COVERAGE.md) | Disposition of all **1,348** source topics (769 in-scope, 579 deferred, SCM Mobile excluded). |
| 7 | `feature-sets/FS-00.md` … `FS-15.md` | Implementation-ready specs per set (template: `feature-sets/_TEMPLATE.md`). |
| 8 | [`verification/VERIFICATION-REPORT.md`](verification/VERIFICATION-REPORT.md) | Independent audit, coverage/DAG/adversarial evidence, sign-off. |

## Quickstart (plan validation)

Requires Python 3.12 (stdlib only for the plan tooling).

```bash
cd inventory-platform

# 1) Validate the dependency DAG, phases/waves, contracts, required gates
python tools/feature_sets.py validate

# 2) Compute the deployment order / wave-by-wave runbook
python tools/feature_sets.py order --phase 1
python tools/feature_sets.py deploy-order --phase 1

# 3) Coverage: all 1,348 source topics accounted for
python tools/feature_sets.py coverage

# 4) Regenerate the PLAN.md status/feature tables from the manifest
python tools/render_plan_tables.py --check

# 5) Check a feature set's entry gate against an environment deployment state
python tools/feature_sets.py check-env --set FS-03 --env dev
```

To execute the plan, follow `PLAN.md` §7 (runbook) and `design/FEATURE-SETS.md` §3 (wave sequence):
deploy **one feature set at a time** (builds may run in parallel per Δ-PAR; exception: wave 1F's specified FS-12/13/14 parallel group), L2 entry gate → additive migrations → deploy → L3 exit gate →
`record --status verified`; dependants can only deploy after their dependencies are `verified`.

## Scope at a glance

| Source area (official help) | Topics | Feature sets |
|---|---|---|
| Item Record Management | 194 | FS-01, FS-02 (plus pricing) |
| Inventory Management | 218 | FS-03, FS-04, FS-05, FS-06, FS-07, FS-10, FS-12, FS-13, FS-14 |
| Manufacturing | 415 | FS-09 (plus extensions) |
| Vendors, Purchasing, Receiving | 376 | FS-08 (plus FS-14 part numbers/warranty) |
| Warehouse Management | 145 | FS-11 |
| **SCM Mobile** | — | **excluded permanently** |

- 16 feature sets: FS-00 platform → FS-15 reporting read models
- 116 declared dependency edges, 21 provided contracts, 10 waves (`0A, 0B, 0C, 1A … 1F, 2`)
- **287 tables** (platform 34) + 21 reporting views, 31 posting transaction types, 6 non-posting commands
- **Multi-tenant by default, including multi-tenant users**: pooled tenants with PostgreSQL RLS,
  composite tenant FKs, a global `user_identity` + per-tenant memberships, login tenant selection/switching,
  invitations/sessions/revocation, and platform-admin vs tenant-admin separation (Δ-MT addendum).
- **Permissions & roles (Δ-RBAC)**: deny-by-default capability scopes + data scopes, an authoritative
  **255-scope catalogue**, **7 system role templates** (cloneable), effective-dated assignments,
  effective-permissions APIs, **14 hard SoD pairs**, route-scope coverage checks and a
  `tools/feature_sets.py permissions` plan gate (Δ-RBAC addendum + `design/permissions.json`).
- **Authentication:** Dex OIDC identity broker with **per-company SSO** (SAML 2.0 / OIDC / LDAP-AD via `tenant_sso_connection`), platform-issued JWT/refresh sessions, local-password fallback (Δ-SSO addendum)
- Reference stack: Python 3.12 / FastAPI / PostgreSQL 16 / Redis + RQ / transactional outbox / Dex / Docker Compose
- **Manufacturing compatibility & unbuild reuse (Δ-CFG/Δ-REUSE)**: configurable compatibility rule engine
  (typed attributes/slots; dual assembly/BOM + order/customer/region scopes; HARD/WARN/override) enforced
  across every manufacturing path, plus unbuild dispositions (return/used/scrap/hold; original-cost default
  valuation configurable to NRV/manual) — FS-09 extension.
- **Parallel implementation readiness (Δ-PAR)**: S0 scaffold gate + parallel build cards for up to 8
  concurrent FS builders (deploy stays wave-ordered); see [`PARALLEL-IMPLEMENTATION-ADDENDUM.md`](design/PARALLEL-IMPLEMENTATION-ADDENDUM.md),
  [`parallel-plan.json`](design/parallel-plan.json) and `tools/check_parallel_plan.py --s0`

## Plan package layout

```
inventory-platform/
├── PLAN.md                     # master plan (scope, waves, gates, runbook, status)
├── README.md                   # this file
├── design/                     # framework, architecture, data model, feature sets, coverage, manifest
├── feature-sets/               # FS-XX.md implementation specs + _TEMPLATE.md
├── tools/                      # feature_sets.py (plan gate CLI), render_plan_tables.py
├── verification/               # independent audit, tests, evidence, deployment-state files
└── research/                   # extracted official-help catalogs (412 + 936 topics, URLs, classifications)
```

## Deliverable status

**v1.0.0: Verified PASS** — independently audited on 2026-10-21: 0 BLOCKER, 0 MAJOR, 0 open findings.
**v1.1.0 (Δ-MT multi-tenant users): Verified PASS** — independent delta audit complete: 0 BLOCKER,
0 MAJOR, 0 open findings (32-file audited snapshot refreshed). See
[`verification/VERIFICATION-REPORT.md`](verification/VERIFICATION-REPORT.md) and `verification/evidence/`.
Binding amendments: [`design/DEX-SSO-ADDENDUM.md`](design/DEX-SSO-ADDENDUM.md),
[`design/MULTI-TENANT-USERS-ADDENDUM.md`](design/MULTI-TENANT-USERS-ADDENDUM.md),
[`design/PERMISSIONS-ROLES-ADDENDUM.md`](design/PERMISSIONS-ROLES-ADDENDUM.md).
**v1.2.0 (Δ-RBAC permissions & roles): Verified PASS** — independent delta audit: 0 BLOCKER, 0 MAJOR,
0 open findings. **v1.3.0 (Δ-SSO Dex + enterprise SSO): Verified PASS** — independent delta audit complete:
0 BLOCKER, 0 MAJOR, 0 open findings. **v1.4.0 (Δ-PAR parallel implementation): Verified PASS** — independent
delta audit complete: 0 BLOCKER, 0 MAJOR, 0 open findings (38-file audited snapshot refreshed); S0 scaffold
tasks and the parallel build graph are ready to execute. **v1.5.0 (Δ-CFG/Δ-REUSE): Verified PASS** — manufacturing compatibility + unbuild-reuse amendment;
0 BLOCKER, 0 MAJOR, 0 open findings (39-file audited snapshot refreshed). See
[`verification/VERIFICATION-REPORT.md`](verification/VERIFICATION-REPORT.md) and `verification/evidence/`.

This package is a **plan**, not the implemented system: it contains the executable decomposition,
design, coverage, per-set specs, gates and verification. Implementation begins with `FS-00` and
proceeds wave-by-wave per `PLAN.md` §7. Runtime behavior of the specified APIs/migrations (gates L1–L3)
is proven during implementation; the plan-package gates (DAG/coverage/ordering, sample
`check-env`/`record`, adversarial manifests) are executable now.
