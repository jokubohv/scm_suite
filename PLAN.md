# Inventory / SCM Backend — Executable Feature-Set Plan

**Version:** 1.5.0 — **VERIFIED PASS** (v1.0.0 base + Δ-MT + Δ-RBAC + Δ-SSO + Δ-PAR + Δ-CFG/Δ-REUSE; independent delta audits: 0 BLOCKER / 0 MAJOR / 0 open findings) · **Date:** 2026-10-22 · **Owner:** Lead (deepseek agent team)
**Verification:** [`verification/VERIFICATION-REPORT.md`](verification/VERIFICATION-REPORT.md) (v1.0.0 PASS + Δ-MT + Δ-RBAC + Δ-SSO + Δ-PAR PASS + Δ-CFG/Δ-REUSE PASS) · **Execution status:** all feature sets `planned` (nothing deployed yet — this package is the executable plan)
**Scope source:** Oracle NetSuite Applications Suite — SCM (Supply Chain Management) help set
**Source URL (as provided):** https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/book_161557401927.html

---

## 1. What this plan builds

A NetSuite-like **inventory and supply-chain backend**, delivered as **independently deployable feature
sets** that check each other's dependencies before and after every deployment.

- **Phase 1 (this plan, fully specified): core backend logic only.**
  Domain model, transactions and invariants, persistence, services, REST APIs, domain events,
  asynchronous jobs, authorization, audit, reconciliation, tests and deployment gates.
- **Phase 2+ (envelope only, not sized here):** end-user UI, mobile apps, analytics/BI, external
  integrations, SuiteApp packaging.

The feature surface is derived from the five SCM help areas in the source set, **excluding SCM Mobile**:

| # | Source area | Official help root | Catalogued topics |
|---|---|---|---|
| 1 | Item Record Management | [book_N2164398](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/book_N2164398.html) | 194 |
| 2 | Inventory Management | [book_N2249433](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/book_N2249433.html) | 218 |
| 3 | Manufacturing | [book_1506002637](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/book_1506002637.html) | 415 |
| 4 | Vendors, Purchasing, and Receiving | [set_N2360130](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/set_N2360130.html) | 376 |
| 5 | Warehouse Management | [book_156356229851](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/book_156356229851.html) | 145 |
| — | **SCM Mobile** | [book_161557401927](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/book_161557401927.html) | **EXCLUDED** |
| | **Total catalogued** | | **1,348 topics** |

**Scope interpretation (explicit):** the URL supplied with the request points at the *SCM Mobile*
book. SCM Mobile is excluded per the requirement; therefore the plan covers the rest of the parent
**SCM** help set (the five areas above). Mobile workflows that appear *inside* those five areas
(WMS mobile-device procedures, Manufacturing Mobile) are catalogued but classified as deferred
Phase-2 work; they are not part of Phase 1 backend scope.

---

## 2. Plan package — what to read, in order

| Artifact | Purpose |
|---|---|
| `PLAN.md` (this file) | Scope, execution order, gates, status, risks, runbook. Start here. |
| [`design/FEATURE-SET-FRAMEWORK.md`](design/FEATURE-SET-FRAMEWORK.md) | **Binding conventions**: feature-set definition, manifest schema, dependency-check protocol, deployment protocol, reference stack, Definition of Done. |
| [`design/MULTI-TENANT-USERS-ADDENDUM.md`](design/MULTI-TENANT-USERS-ADDENDUM.md) | Binding **Δ-MT amendment**: global identity + per-tenant membership, login/tenant switch, invitations/sessions/revocation, tenant admin APIs, isolation matrix; counts and doc-update map. |
| [`design/PERMISSIONS-ROLES-ADDENDUM.md`](design/PERMISSIONS-ROLES-ADDENDUM.md) | Binding **Δ-RBAC amendment**: permission catalogue, role templates, assignments, effective permissions, SoD, route-scope enforcement and verification. |
| [`design/permissions.json`](design/permissions.json) | **Authoritative machine-readable RBAC catalogue**: 255 capability scopes, 7 system role templates, 14 SoD pairs, allowlists, ownership and global rules. |
| [`design/DEX-SSO-ADDENDUM.md`](design/DEX-SSO-ADDENDUM.md) | Binding **Δ-SSO amendment**: Dex OIDC identity broker, per-company (tenant) SSO connections, identity links/JIT/group mapping, local fallback, tests/gates. |
| [`design/PARALLEL-IMPLEMENTATION-ADDENDUM.md`](design/PARALLEL-IMPLEMENTATION-ADDENDUM.md) | Binding **Δ-PAR amendment**: parallel build vs wave-ordered deploy, S0 scaffold gate, contract freezes, write-scope/shared-file/migration protocols, fleet roles and go/no-go. |
| [`design/CONFIG-REUSE-ADDENDUM.md`](design/CONFIG-REUSE-ADDENDUM.md) | Binding **Δ-CFG/Δ-REUSE amendment**: manufacturing compatibility engine (dual rule scopes, configurable enforcement, all manufacturing paths) + unbuild dispositions with optional used/scrap/hold and configurable valuation. |
| [`design/parallel-plan.json`](design/parallel-plan.json) | Machine-readable S0 + FS task graph, write scopes, migration revisions, contract freezes, phases, roles, max concurrency. |
| [`tools/check_parallel_plan.py`](tools/check_parallel_plan.py) | Executable readiness validator (`--s0` prints execution-ready go/no-go). |
| [`design/ARCHITECTURE.md`](design/ARCHITECTURE.md) | Backend architecture: module map, posting model, costing engine, events, security, NFRs. |
| [`design/DATA-MODEL.md`](design/DATA-MODEL.md) | Canonical domain/data model: entities, tables, constraints, state machines, posting rules, migration order. |
| [`design/FEATURE-SETS.md`](design/FEATURE-SETS.md) | Feature-set catalog, dependency DAG, wave plan, critical path, per-set summaries. |
| [`design/feature-sets.json`](design/feature-sets.json) | **Single source of truth** for the dependency graph and gates (machine-readable). |
| [`design/FEATURE-COVERAGE.md`](design/FEATURE-COVERAGE.md) | Disposition of all 1,348 source topics → owning feature set or deferred bucket. |
| `feature-sets/FS-XX.md` | Implementation-ready specs per feature set. |
| [`tools/feature_sets.py`](tools/feature_sets.py) | Executable plan gate: validate DAG, compute deployment order, check env state, record verification. |
| [`tools/render_plan_tables.py`](tools/render_plan_tables.py) | Regenerates the generated tables in this file from `feature-sets.json` + coverage (`--check` in CI). |
| `_TEMPLATE.md` (in `feature-sets/`) | Mandatory spec template followed by all 16 implementation specs. |
| [`verification/VERIFICATION-REPORT.md`](verification/VERIFICATION-REPORT.md) | Independent audit: coverage, DAG, executability, adversarial tests, evidence. |
| `research/*` | Source-faithful feature catalogs extracted from the official help (with URLs). |
| `README.md` | One-page index + quickstart commands. |

---

## 3. Guiding decisions

1. **Deployable slices, not a big-bang.** Every feature set is useful and deployable on its own once
   its declared dependencies are verified. The full backend is an ordered DAG of such slices.
2. **The inventory ledger is the system of record.** Documents (POs, receipts, work orders, counts,
   picks) are workflows that *post* to an append-only ledger; balances, costing and availability are
   derived and continuously reconciled. This makes every downstream feature set checkable by
   invariant tests rather than UI screenshots.
3. **Contracts between feature sets.** A set may only rely on its dependencies' published HTTP APIs,
   events, read models and seeds — never on another module's internals. Contracts are semver'd and
   tested from both provider and consumer sides.
4. **Checks are executable, not prose.** Static DAG validation, entry gates against environment
   deployment state, and post-deploy exit gates (smoke + contract + invariants) are all commands in
   the manifest. A dependent set's entry gate *is* the proof its dependency is healthy.
5. **Core backend first.** No UI, mobile, analytics or external-integration work in Phase 1. Finance
   (GL posting/period close/tax) and order management (sales orders) are **external boundaries**:
   we define ports/contracts for them and use reference data, not full implementations (see §12).
6. **Source fidelity.** Every feature set cites the catalog topics it implements; coverage is checked
   mechanically; no invented NetSuite behavior without an explicit `ASSUMPTION/EXTENSION` marker.

### 3.1 Locked decisions (authoritative for the manifest and specs)

| # | Decision | Consequence |
|---|---|---|
| D1 | Feature-set map/order = FS-00 platform → FS-15 reporting; 10 waves `0A,0B,0C,1A…1F,2` | DAG and manifest waves are frozen; wave changes are plan changes |
| D2 | GL is a boundary: persist `finance_journal_intent` + `GET /v1/finance/journal-intents`; no GL/period-close/tax/payments | Table count 273→274; value postings emit intents |
| D3 | Order management is external: FS-00 provides `order.demand@0.1.0` / `order.supply@0.1.0` stubs; FS-04+ consume | No order domain/UI; test doubles + v0 contract |
| D4 | SuiteApp-classified features included at **backend-semantics** level only: quality, warranty, customer part numbers, Auto-UPC, effective-date pricing, available-to-build, scheduling inputs | Deferred regardless: UI/workbooks/mobile/EDI/SuiteProcurement/Smart Count app |
| D5 | Cost recalculation policy = per-tenant flag, **default block** (`409 COSTING_RECALC_REQUIRED`); provisional opt-in | Protects zero-drift NFR |
| D6 | Enforce documented incompatibilities as tenant guards: consigned↔assembly, WIP only for standard/average, group-average ≠ worksheets | Rejects invalid configurations at the API layer |
| D7 | AIM and ATP arithmetic are versioned policies (policy id + params + deterministic fixtures), not hard-coded formulas | Under-documented NetSuite math made testable |
| D8 | All mobile deferred; SCM Mobile permanently excluded; backend cart/device-session records may exist as EXT | No mobile endpoints in Phase 1 |
| Δ-1 | `platform.finance_journal_intent` added to DATA-MODEL (FS-00-owned, append-only, read API) | 274 base tables at v1.0.0; 277 after Δ-MT |
| Δ-2 | Boundary contracts owned by FS-00 (`order.demand`/`order.supply`) | Five consumer edges point at FS-00 |
| Δ-3 | FS-15 reporting read models are backend Phase-1, wave `2` | No forward-phase dependency; backend-only retained |
| Δ-MT | Multi-tenant users: global `user_identity` + per-tenant `app_user` membership, invitations, sessions, login tenant-selection/switch, platform-admin tenant provisioning vs tenant-admin user admin; EXT beyond NetSuite SCM docs | Platform 28→31 tables, total 274→277; DM-PLAT-12/13/14 + INV-30; DAG/contracts unchanged |
| Δ-RBAC | Permissions & roles made first-class: authoritative `design/permissions.json` (249 scopes, 7 role templates, 14 SoD pairs, allowlists), role lifecycle/assignments, effective permissions, deny-by-default route-scope coverage, safety guards; capability-scope model is EXT/ASSUMPTION beyond NetSuite SCM docs | DM-RBAC-01/02/03 + INV-31; rules 137→140 (147→150); no table/DAG/contract changes |
| Δ-SSO | **Dex** as OIDC identity broker + **per-company (tenant) SSO** to SAML 2.0/OIDC/LDAP-AD IdPs; platform keeps issuing its JWT/refresh sessions and remains the authorization authority; local password fallback; EXT SaaS capability | Platform 31→34 tables (280 total); DM-SSO-01/02/03 + INV-32; rules 140→143 (153 total); scopes 251; plan_version 1.3.0; no DAG changes |
| Δ-PAR | Parallel **build** allowed (≤8 concurrent FS builders) while **deploy stays wave-ordered**; S0 scaffold + contract-first freezes C0–C5; write-scope/shared-file/migration protocols and fleet roles; execution-only | No tables/rules/scopes/DAG changes; plan_version 1.4.0; adds `parallel-plan.json` + `check_parallel_plan.py` S0 gate |
| Δ-CFG/Δ-REUSE | Manufacturing **compatibility engine** (typed attributes/slots/rule sets; dual scope assembly/BOM + order/customer/region, both applied; configurable HARD/WARN per rule/rule-set/tenant; override+audit) and **unbuild dispositions** (return default / used optional / scrap / hold; valuation original-cost default configurable to NRV/manual; serial/lot lifecycle); applies to **every manufacturing path** and is EXT | FS-09 tables 35→42, total 280→287; rules 143→146 (156 total); INV-33; scopes 251→255 (241 assignable); contract manufacturing.execution 1.1.0 additive; no DAG/wave changes; plan_version 1.5.0 |

---

## 4. Reference architecture (summary)

Full detail in [`design/ARCHITECTURE.md`](design/ARCHITECTURE.md). Phase-1 reference stack (fixed in
[`design/FEATURE-SET-FRAMEWORK.md`](design/FEATURE-SET-FRAMEWORK.md) §3):

- **Modular monolith** in Python 3.12 / FastAPI, PostgreSQL 16 (SQLAlchemy 2.0 + Alembic),
  Redis 7 + RQ worker, transactional outbox → events; Docker Compose deployment.
- **Bounded contexts:** platform core → item master → pricing → inventory ledger → costing →
  traceability (lot/serial/bins/status) → counts → replenishment/planning → procurement → WMS →
  manufacturing → quality/consigned/extensions → reporting read models.
- **One shared database, per-module schema ownership.** Cross-context reads happen through contract
  read models/APIs, never shared ORM models.
- **Multi-tenant by default** (`tenant_id` on every tenant-owned row + RLS + isolation tests), with a
  global user identity and per-tenant memberships so one user can belong to several tenants (§4.2);
  deny-by-default RBAC (capability scopes + data scopes, role templates, SoD — §4.3), **Dex OIDC
identity broker with per-company SSO** (§4.4), append-only audit trail, idempotency keys on every
posting command.
- **Environment flow:** dev → staging → prod, one **deployment** at a time (builds may run in parallel per
  Δ-PAR; deploy remains wave-ordered, except wave 1F's specified FS-12/13/14 parallel group), expand/contract
  migrations, feature flag per set, forward-fix rollback.

### 4.1 The inventory workflow — the business cycle the feature sets implement

The documented **Inventory Workflow** ([`iri-214`](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2251098.html),
in the "Basic Inventory Management" chapter) is a six-process cycle: **Purchasing → Receiving → Manufacturing →
Selling → Fulfilling → Replenishing**, whose purpose is to move product to the customer in the shortest
time at the lowest cost while keeping real-time costs, quantities and asset values accurate. The plan
realizes this cycle *without a single "workflow" module*: each process is a set of documents with
explicit state machines, and **every quantity/value-changing step posts synchronously to the
append-only inventory ledger** (`FS-04`), which is the invariant spine of the whole cycle;
control/commitment/document transitions (C-01…C-06) update documents and emit events without
appending ledger rows. Approvals are declarative state-machine transitions in Phase 1 (full
SuiteFlow-style customisation is deferred).

| Workflow stage | What the source documents | How the plan implements it (surface → posting → events) | Owning feature sets (wave) |
|---|---|---|---|
| **Purchasing** | POs based on replenishment needs; track what/when/who, quantity on order; vendor access | `POST /api/v1/purchase-orders` + approval/release state machine (`FS-08`); open/on-order quantities exposed as FS-04 read models; vendor/contract records in FS-08. Emits `po.created`, `po.approved`, … (`po.*` lifecycle events); no ledger posting until receipt | FS-08 (1B); needs FS-00/01/03/04/05/06 |
| **Receiving** | Enter item receipt when goods arrive; increases stock and asset value | Receipt and its reversal/return/bill-variance posting types T-01..T-06 via `FS-08` (receipt-to-bill chain), optional WMS inbound/putaway (`FS-11`, 1E); `FS-04` posts signed qty/value to the ledger and updates on-order/on-hand; landed cost in FS-05; lot/serial detail in FS-06; bins/status in FS-03. Emits `inventory.receipt.posted`/`inventory.receipt.reversed` (plus `po.receipted`) | FS-08 (1B) + FS-04 (0C) + FS-05 (0C); FS-11 (1E) adds directed putaway |
| **Manufacturing** | Gather kits/groups components or run production for assemblies | Assembly/BOM master + work orders + WIP + build/unbuild (T-15..T-19) in `FS-09`; component issue/consumption posts to FS-04; WIP value, standard-cost variances and rollups in FS-05; optional WMS component picking (1E); lot/serial assembly output in FS-06 | FS-09 (1C) + FS-04/05 (0C); FS-11 (1E) for task-driven picking |
| **Selling** | Sales order commits from available, reserves for the customer; short sales create backorders | Order management is an external boundary (D3/Δ-2): `FS-00` provides `order.demand`/`order.supply` v0 contracts; `FS-04` exposes commitment commands (C-01…C-03) — committed ↑, available ↓ (available = on-hand − committed), backorder when short. Emits `inventory.committed.changed` | FS-04 (0C) + FS-00 boundaries (0A); no sales-order domain/UI in Phase 1 |
| **Fulfilling** | Physically remove and ship items; on-hand and asset value decrease | Fulfillment/return posting commands (T-12…T-14) in `FS-04`, driven by the external order system or WMS pick/pack/ship tasks (`FS-11`, 1E); serial/lot selection in FS-06; COGS and cost relief in FS-05; events `inventory.fulfilled`, `inventory.issued` | FS-04 (0C) + FS-05/06; FS-11 (1E) for warehouse execution |
| **Replenishing** | Examine needs, use transfers/AIM suggestions to identify products and order quantities; closes the cycle back to Purchasing/Manufacturing | `FS-10` AIM reorder points, demand/supply (MRP), DRP, allocation and ATP compute suggestions; replenishment/withdrawal worksheets create **draft** POs/TOs/WOs in FS-08/FS-04/FS-09 — never auto-post; versioned planning policies (D7) | FS-10 (1D); suggestions consume FS-04/08/09 contracts |
| **Assessment** | Item records show real-time on-hand, needed qty and value; reports; period-close inventory checklist | Real-time item/location balances and availability from FS-04 read models, valuation from FS-05, item data from FS-01; reporting views/API in FS-15 (wave 2). Period close itself is a **Finance boundary** (not built); FS-04 records the fiscal period at posting (from the FS-00 `FinancePort` projection) and exposes negative-inventory and mismatch/reconcile data for the checklist | FS-01/04/05 (0B/0C); FS-15 (2) for reporting; analytics workbooks/Item 360 deferred |

**How the cycle is verified while it is deployed in slices.** The workflow is usable *incrementally*:
each stage's behavior only becomes available when its owning feature sets are `verified`, enforced by
the dependency gates in §5. The ledger-stage exit gate (`FS-04`, wave 0C) proves the workflow's core
invariants: quantity conservation, `balance = Σ ledger lines`, `committed ≤ on-hand`, non-negative
available (per tenant negative-inventory policy), value equation and append-only history. Receiving adds
PO-open/receipt posting checks (1B); manufacturing adds BOM/WIP/build conservation (1C); WMS adds
task/wave/pick/putaway conservation (1E); replenishment's gate proves planning is read-only and creates
only draft documents (1D); reporting's gate proves read models reconcile exactly with the ledger (2).
For environments, the runbook's **wave-close step 7 (§7.2)** runs a **golden end-to-end workflow pass** —
purchase → receive → stock → (manufacture) → commit → fulfill → replenish suggestion — before a wave is
promoted; the mandatory per-set check remains the golden-path smoke in the §5 deployment protocol step 4
and the §6 exit criteria. Because selling/fulfillment can run before receiving/manufacturing waves
exist, the pass uses the stage subset available at that wave plus seeded boundary demand.

**Deliberately outside this workflow in Phase 1:** order-management and sales-order screens, mobile
scanning/picking apps, payment/AP processing, GL journals and period close, Item 360/analytics
workbooks, vendor portals, and SCM Mobile (excluded permanently). Those are boundary contracts or
Phase-2 envelopes, not gaps in the cycle model.

### 4.2 Tenancy and multi-tenant users (Δ-MT)

The plan runs **one deployment / one database with pooled tenants**: `tenant_id` on every tenant-owned
row, composite tenant FKs, PostgreSQL RLS keyed from the request token, application-side filters, and
tenant-keyed events/jobs/storage. v1.0.0 already made *tenants* first-class; **Δ-MT** adds the missing
*user* dimension so one person can belong to multiple tenants without weakening isolation
(EXT beyond the NetSuite SCM docs; binding details in
[`design/MULTI-TENANT-USERS-ADDENDUM.md`](design/MULTI-TENANT-USERS-ADDENDUM.md)):

- **`platform.user_identity`** — global, auth-only identity (email, credential/external-IdP subject, MFA,
  status, platform-admin flag). It is *not* tenant-scoped and is never exposed through tenant business
  routes.
- **`platform.app_user`** — the per-tenant membership/principal (exactly one per tenant+identity); all
  FK/audit/role references point at it, roles are per membership, and it carries membership status plus a
  `token_version` used for revocation.
- **`platform.tenant_invitation`** — single-use, expiring, hashed invite with intended roles; acceptance
  atomically creates the identity (if new), membership and role assignments.
- **`platform.user_session`** — refresh session bound to one tenant principal, with hashed rotating
  refresh token, replay detection and revocation.

**Auth flow:** a 15-minute access JWT carries identity, app_user, tenant and token-version claims; login
returns either a tenant session (single membership — the common case) or a short-lived tenant-selection
challenge; `POST /auth/switch-tenant` issues a token for another ACTIVE membership; `GET /me/tenants`
lists only the caller's own memberships. **Administration:** platform admins provision/suspend/offboard
tenants and bootstrap tenant admins; tenant admins invite and manage *their* users. No tenant role can
grant `platform.tenant.manage`. **Enforcement:** the active tenant comes only from the verified token → RLS
context; cross-tenant probes return 404; membership revocation bumps `token_version`, kills sessions and
blocks writes; every identity/membership/invitation/session change is audited; a tenant/user isolation
matrix (every endpoint class × list/get/mutate × cross-tenant probe) is part of FS-00's exit gate.
**Counts (final v1.3.0):** platform **34** tables, total **280** base tables (21 views, 31 posting
types + 6 commands unchanged); rules include DM-PLAT-12/13/14, DM-RBAC-01/02/03 and DM-SSO-01/02/03;
invariants INV-30…INV-32 added; feature-set DAG/contracts and all other sets unchanged.
**Deferred:** SSO/OIDC/SCIM, self-service signup/billing, custom domains, tenant-switch UI, enterprise
tenant hierarchy, cross-tenant support impersonation (Phase 2+); DB-per-tenant is not used.

### 4.3 Permissions, roles and approvals (Δ-RBAC)

Authorization is a first-class, machine-checkable part of the plan, not a convention. The authoritative
artefact is [`design/permissions.json`](design/permissions.json) (binding amendment:
[`design/PERMISSIONS-ROLES-ADDENDUM.md`](design/PERMISSIONS-ROLES-ADDENDUM.md)):

- **Capability scopes:** every non-public route is guarded by **exactly one** scope
  (`<domain>.<resource>.<action>`, e.g. `procurement.po.approve`, `inventory.adjustment.post`); the
  reconciled catalogue has **255 scopes** (241 assignable, 1 platform-only `platform.tenant.manage`,
  13 service-only incl. `platform.internal`, 1 legacy alias). Unknown/missing/duplicate scopes fail CI.
- **Data scopes** (`LOCATION/CLASS/DEPARTMENT/SUBSIDIARY`, with descendants) restrict rows and posting
  targets on top of the capability scope.
- **Roles:** **7 system role templates** seeded per tenant (`admin`, `planner`, `buyer`,
  `warehouse_operator`, `cost_accountant`, `quality_inspector`, `readonly`) — non-editable but cloneable;
  custom roles are subsets of assignable scopes. Role assignments are effective-dated per membership;
  effective permissions are resolved per request so changes apply immediately.
- **Safety guards:** platform-only/service-only scopes can never be granted by tenant roles; only
  role/permission holders can manage roles; SoD-conflicting pairs cannot coexist in a non-admin role;
  the last active tenant administrator cannot be suspended/deactivated/stripped (lockout guard);
  break-glass admin is audited. **14 hard SoD pairs** (e.g. `procurement.po.create` ↔
  `procurement.po.approve`) plus the global **requester ≠ approver** rule for every approval-required
  document; approval scopes are separate from entry/posting scopes.
- **Enforcement & proof:** deny-by-default middleware (authenticate → membership/tenant → scope → data
  scope → domain), with only the explicit public/self-service allowlists in the catalogue. FS-00's exit
  gate runs `python tools/feature_sets.py permissions` plus route-scope coverage
  (`test_route_scope_coverage.py`), role/last-admin/privilege-escalation, effective-permission and SoD
  tests; every feature set's exit gate includes 403-without-scope / 200-with-scope route tests.
- **Counts:** DM-RBAC-01/02/03 + **INV-31** added; scoped rules **137→140**, total **147→150**; tables,
  views, posting types, DAG, edges and contracts are **unchanged**. **Deferred (Phase 2+):** RBAC admin
  UI, ABAC/policy engine, SCIM/IdP group→role sync, delegated administration, per-field ACLs.

### 4.4 Authentication: Dex + enterprise SSO (Δ-SSO)

The stack is unchanged; only the human authentication path changes. **Dex** is the OIDC **identity
broker**, and each company/tenant can connect **its own upstream IdP** (SAML 2.0, OIDC, LDAP/AD) through
a per-tenant SSO connection. Binding details:
[`design/DEX-SSO-ADDENDUM.md`](design/DEX-SSO-ADDENDUM.md).

- **Authentication vs authorization:** Dex authenticates; the platform authorizes. After validating the
  Dex ID token (issuer, audience, signature, nonce, `email_verified`), the API resolves the global
  `user_identity` + tenant membership and issues **platform** access JWT + rotating refresh session.
  Δ-MT multi-tenant memberships/tenant switching and Δ-RBAC scopes/roles/SoD still apply unchanged.
- **Per-company SSO:** tenant admins configure `tenant_sso_connection` rows (protocol, Dex issuer,
  client/secret refs, email domains, enforced-SSO, JIT provisioning, group→role mappings). Reference
  deployment: **one isolated Dex issuer per active SSO connection** (Dex storage in Postgres); shared
  Dex is allowed for dev/test. Secrets live in the secret store; DB stores refs only.
- **Login flows:** discovery (`/auth/providers`) → SSO start (authorization code + PKCE + state/nonce) →
  callback → identity link/JIT (verified email/domain policy) → group→role mapping → platform tokens;
  typed errors, replay-safe state, no silent downgrade when Dex is unavailable. Platform-admin
  break-glass stays local and audited; local password fallback remains for non-SSO tenants.
- **Data:** three platform tables (`tenant_sso_connection`, `user_identity_link`, `sso_group_role_map`);
  two new scopes (`platform.sso.manage`, `platform.sso.read`); rules DM-SSO-01/02/03 and invariant
  **INV-32** (SSO/authn integrity). Counts at v1.3.0: platform 34 / total 280 tables; scopes 251; rules
  143 + 10. Final v1.5.0 totals after Δ-CFG/Δ-REUSE are 287 tables, 255 scopes (241 assignable) and
  146 + 10 = 156 rules (§4.5/§12).
- **Deferred (Phase 2+):** SCIM, IdP-initiated (unsolicited) SAML, cross-company single logout,
  multiple IdPs within one login, SSO admin UI; ABAC and delegated admin remain deferred.

### 4.5 Manufacturing compatibility & unbuild reuse (Δ-CFG/Δ-REUSE)

Two FS-09 extensions answer the configured-hardware/manufacturing questions (binding details:
[`design/CONFIG-REUSE-ADDENDUM.md`](design/CONFIG-REUSE-ADDENDUM.md); EXT beyond the NetSuite SCM docs):

- **Compatibility engine:** typed attributes on items/assemblies (socket, chipset, form factor, PCIe/DIMM
  slots, PSU watts, card length, rack units), configurable slot/capacity definitions, and rule sets with
  **dual scope** — **assembly/BOM** rules and **order/configuration/customer/region/channel** rules both
  apply (deterministic specificity → priority → code ordering). Rule types: requires/excludes/one-of/
  min-max/attribute equality-range/capacity. **Enforcement is user-configurable**: tenant default HARD,
  per-rule-set and per-rule override, WARN records a violation, HARD blocks the guarded action; overrides
  need `manufacturing.compat.override` + reason and are audited. Every validation persists an immutable
  configuration record/hash.
- **All manufacturing paths that need compatible parts are gated:** BOM/revision creation or activation,
  matrix-assembly child generation, phantom explosion, work-order create/update/release, WIP
  completion/backflush, non-WIP assembly build, outsourced manufacturing, ECO/BOM revision activation,
  order/configuration boundary (configure-to-order), and available-to-build. Unbuild restores a previously
  valid build and is exempt from new-configuration validation; kits/groups are not manufacturing paths.
- **Unbuild/teardown disposition (Δ-REUSE):** per component line `RETURN_TO_STOCK` (default) / `SCRAP` /
  `HOLD`; when returning, **optional `used`** marking with condition code, used/refurb inventory status
  (non-sellable/non-committable by default), target bin/location and resale flag; valuation defaults to
  **original build cost** and is user-configurable (tenant/assembly/line) to **NRV** or **manual**, posting
  the value difference through FS-05; serial/lot lifecycle retained; scrap returns no stock; hold is
  non-committable; reversal rules documented; **rework deferred** to the FS-14 repair flow.
- **Counts:** FS-09 **42 tables** (+7: attributes, values, rule sets, rules, slots, configurations,
  condition codes), total **287**; **146 scoped + 10 global = 156** rules; **INV-33**; scopes **255 =
  241 assignable + 13 service-only + 1 platform-only**; state machines 44/53; FS-09 **115 endpoints /
  24 events / 5 jobs / 3 migrations / 8 exit commands**; contract `manufacturing.execution@1.1.0`
  (additive; consumer ranges unchanged); no DAG/wave/coverage changes.

---

## 5. Feature sets, dependency graph and execution order

The decomposition, DAG, wave plan, critical path and per-set summary live in
[`design/FEATURE-SETS.md`](design/FEATURE-SETS.md); the machine-readable definition is
[`design/feature-sets.json`](design/feature-sets.json). Summary table is generated from the manifest:

<!-- AUTO:FEATURE-SETS-TABLE-START -->
| FS | Feature set | Wave | Size | Depends on | Provided contracts | Source topics | Tables |
|---|---|---|---|---|---|---|---|
| **FS-00** | Platform Core (tenancy, identity, audit, events, jobs, boundaries) | 0A | L |  | `platform.core@1.0.0`, `platform.events@1.0.0`, `platform.finance@1.0.0`, `order.demand@0.1.0`, `order.supply@0.1.0` | 4 | 34 |
| **FS-01** | Item Master and UoM Engine | 0B | M | FS-00 | `item.master@1.0.0` | 57 | 21 |
| **FS-02** | Pricing Engine (item pricing + advanced pricing + effective-date updates) | 0B | M | FS-00, FS-01 | `pricing.engine@1.0.0` | 50 | 17 |
| **FS-03** | Storage and Inventory-Status Registry | 0C | S | FS-00, FS-01 | `traceability.registry@1.0.0` | 14 | 4 |
| **FS-04** | Inventory Ledger Core (posting, balances, commitments, transfers, adjustments, imports) | 0C | L | FS-00, FS-01, FS-02, FS-03 | `inventory.ledger@1.0.0`, `inventory.balances@1.0.0` | 42 | 22 |
| **FS-05** | Costing and Valuation Engine | 0C | L | FS-00, FS-01, FS-03, FS-04 | `costing.engine@1.0.0` | 88 | 25 |
| **FS-06** | Numbered Inventory (lots and serials) | 1A | M | FS-00, FS-01, FS-03, FS-04, FS-05 | `traceability.numbered@1.0.0` | 24 | 3 |
| **FS-07** | Inventory Counts and Cycle Counts | 1A | M | FS-00, FS-01, FS-03, FS-04, FS-05, FS-06 | `counts.engine@1.0.0` | 16 | 6 |
| **FS-08** | Procurement (vendor master, P2P documents, receipts, bills, returns) | 1B | L | FS-00, FS-01, FS-03, FS-04, FS-05, FS-06 | `procurement.p2p@1.0.0` | 116 | 29 |
| **FS-09** | Manufacturing (assembly/BOM, work orders, WIP, routing, scheduling inputs) | 1C | L | FS-00, FS-01, FS-03, FS-04, FS-05, FS-06, FS-08 | `manufacturing.execution@1.1.0` | 160 | 42 |
| **FS-10** | Replenishment and Planning (AIM, demand, supply/MRP, DRP, allocation, ATP) | 1D | L | FS-00, FS-01, FS-03, FS-04, FS-05, FS-06, FS-08, FS-09 | `replenishment.planning@1.0.0` | 52 | 24 |
| **FS-11** | Warehouse Management Execution (server-side tasks only) | 1E | L | FS-00, FS-01, FS-03, FS-04, FS-06, FS-07, FS-08, FS-09 | `wms.execution@1.0.0` | 89 | 38 |
| **FS-12** | Quality Management (specs, inspections, COA) — SuiteApp semantics backend | 1F | M | FS-00, FS-01, FS-03, FS-04, FS-06, FS-08, FS-09 | `quality.management@1.0.0` | 16 | 10 |
| **FS-13** | Consigned Inventory Management | 1F | M | FS-00, FS-01, FS-03, FS-04, FS-05, FS-06, FS-08 | `consigned.inventory@1.0.0` | 9 | 5 |
| **FS-14** | Extensions (warranty, customer part numbers, UPC registry) | 1F | S | FS-00, FS-01, FS-06, FS-08 | `extensions.registry@1.0.0` | 16 | 7 |
| **FS-15** | Reporting Read Models and Reporting API | 2 | M | FS-00, FS-01, FS-02, FS-03, FS-04, FS-05, FS-06, FS-07, FS-08, FS-09, FS-10, FS-11, FS-12, FS-13, FS-14 | `reporting.readmodels@1.0.0` | 16 | 0 (21 views) |
| **Total** | 16 feature sets | 10 waves | — | 116 dependency edges | 21 contracts | **769** | 287 tables / 21 views |
<!-- AUTO:FEATURE-SETS-TABLE-END -->

### Dependency checking — how sets check each other

The full protocol is in [`design/FEATURE-SET-FRAMEWORK.md`](design/FEATURE-SET-FRAMEWORK.md) §5.
In one paragraph: **L0** static validation proves the graph is acyclic, phases/waves monotonic and
contract ranges satisfiable; **L1** provider+consumer contract tests prove both sides of every edge;
**L2** entry gate runs *before* deploying a set and asserts every dependency is deployed+verified at
a compatible contract version (via `deployment-state.<env>.json` and live `/internal/deps`); **L3**
exit gate proves the newly deployed set's contract and invariants hold and records it as `verified`.
A dependent set cannot deploy until its dependencies' L3 evidence exists. Runtime reconciliation jobs
continuously prove cross-set consistency (ledger vs balances, receipts vs on-order, etc.).

Commands (implemented by `tools/feature_sets.py`):

```bash
python tools/feature_sets.py validate                         # L0: schema, DAG, phases, semver, uniqueness
python tools/feature_sets.py order --phase 1                  # topological deployment order
python tools/feature_sets.py check-env --set FS-03 --env dev  # L2: dependency/deployment-state gate
python tools/feature_sets.py who-depends-on --contract inventory.ledger
python tools/feature_sets.py record --set FS-03 --env dev --status verified  # after L3 evidence
python tools/feature_sets.py coverage                          # feature coverage sanity check
```

### Deployment protocol (per feature set)

1. `validate` + `order` → confirm the set's position; confirm CI is green.
2. `check-env` (L2) for the target environment → aborts if any dependency is missing/unverified.
3. Build image → run the set's additive Alembic migrations → deploy `api`/`worker` → run exit checks
   (L3) → `record --status verified`.
4. Enable the set's feature flag per tenant; run one golden-path smoke transaction; watch
   error rate/latency for 30 minutes.
5. Update the status table (§11) and `deployment-state.<env>.json`.
6. Rollback: disable flag and redeploy previous image; migrations are additive/N-1 compatible; no
   down-migrations in prod. A failed exit gate blocks dependants automatically.

---

## 6. Execution order (waves) and milestones

Wave composition and the per-set dependency table are authoritative in
[`design/FEATURE-SETS.md`](design/FEATURE-SETS.md). Execution rule: **one feature set in flight per
write scope**; a wave closes only when every set in it is `verified` for the target environment.

<!-- AUTO:WAVES-TABLE-START -->
| Wave | Sets (deploy order) | Prerequisite wave state | Wave exit criteria |
|---|---|---|---|
| **0A** | FS-00 | empty/greenfield environment | L0 valid; platform migrations at head; RLS/append-only/tenant-isolation invariant suites green; outbox relay+job scheduler healthy; `record --status verified`; `/internal/deps` reports platform versions |
| **0B** | FS-01 → FS-02 | 0A verified | item master + pricing exit gates green; FS-01 contract consumed by FS-02 consumer tests; UoM/price-resolution golden suites green; both recorded |
| **0C** | FS-03 → FS-04 → FS-05 | 0B verified | registry, ledger and costing exit gates green; posting invariants (qty/value conservation, balance=Σlines, commitment≤on-hand, recalc-block policy) green; concurrency suite green; all recorded |
| **1A** | FS-06 → FS-07 | 0C verified | deferred lot/serial FKs validated + orphan check 0; serial projection/history invariant green; count snapshot/variance invariants green; both recorded |
| **1B** | FS-08 | 1A verified | P2P document state machines, PO open-qty, receipt/bill match and posting consumer tests green; recorded |
| **1C** | FS-09 | 1B verified | BOM/WO/WIP/build invariants + method-incompatibility guard green; scheduling-input fixtures green; recorded |
| **1D** | FS-10 | 1C verified | planning-readonly invariant, AIM formula fixtures, ATP clamp, allocation limits green; order-creation creates only drafts; recorded |
| **1E** | FS-11 | 1D verified (wave gate) | WMS task/wave/receipt/putaway/pick invariants green; no mobile endpoints (D8); recorded |
| **1F** | FS-12 ∥ FS-13 ∥ FS-14 | 1E verified (wave gate) | quality trigger/hold, consigned ownership conservation, warranty/UPC invariants green; all recorded |
| **2** | FS-15 | all Phase-1 waves verified | reporting read-only + reconcile-exact invariants green; `feature_sets.py coverage` passes; recorded |
<!-- AUTO:WAVES-TABLE-END -->

### 6.1 Indicative effort and milestones (non-binding)

Sizing from `design/FEATURE-SETS.md`: 7 × L, 7 × M, 2 × S feature sets. Planning assumption for
effort: **L ≈ 4–6 engineer-weeks, M ≈ 2–3, S ≈ 1–1.5**, including specs, migrations, tests, gate
wiring and one deployment rehearsal (not UI work, which is out of Phase 1).

| Milestone | Waves closed | Exit evidence |
|---|---|---|
| **M0 Plan accepted** | — | this package + independent verification PASS |
| **M1 Platform + master data** | 0A, 0B | FS-00/01/02 verified in `dev`; tenant isolation, authz, audit, UoM/price-resolution suites green |
| **M2 Ledger + costing core** | 0C | FS-03/04/05 verified; posting/concurrency/costing invariant suites green; zero-drift reconcile |
| **M3 Traceability, counts, procurement** | 1A, 1B | FS-06/07/08 verified; lot/serial history, count snapshot, P2P match suites green |
| **M4 Manufacturing + planning** | 1C, 1D | FS-09/10 verified; BOM/WIP/build + AIM/ATP fixture suites green |
| **M5 WMS + extensions** | 1E, 1F | FS-11/12/13/14 verified; task/wave, quality-hold, consignment, warranty/UPC suites green |
| **M6 Phase-1 backend complete** | 2 | FS-15 verified; reporting read models reconcile-exact; all 16 sets recorded `verified` |
| **M7 Phase 1 promoted** | all | staging + prod repeat entry/exit gates for every set; deployment-state files updated |

Indicative Phase-1 backend calendar time: **≈16–24 weeks with 2–3 backend engineers** executing the
wave order (FS-12/13/14 parallel in 1F; all other waves are chains), or less with more parallel
capacity once contracts are stable. Agent-assisted implementation can compress coding calendar time,
but the entry/exit gates, invariant suites and environment promotions are not compressible and remain
the schedule constraint. Re-baseline M2 after FS-04 actuals.

---

## 7. Execution runbook — how to actually build and deploy it

### 7.1 Preconditions

- Linux/macOS host with Docker 27+ and Docker Compose v2 (verified available in this environment),
  Python 3.12, Git.
- A clean implementation repo rooted at `inventory-platform/app/` (layout in framework §3), plus the
  plan package in `inventory-platform/`.
- **S0-GATE: execution-ready scaffold.** `python tools/check_parallel_plan.py --s0` must report
  execution-ready (repo/CI, compose+Dex dev, Alembic base, 16 contract skeletons, test harness, scripts,
  seed bootstrap) before FS-00 starts; see Δ-PAR addendum §2 and `design/parallel-plan.json` (S0-01…S0-07).
- Environments `dev`, `staging`, `prod` with Postgres 16 + Redis 7 (compose for dev/staging).
- Secrets: DB credentials, JWT signing key, SMTP-less service keys — per env, never committed.
- CI pipeline: lint + type check + unit + manifest `validate` + contract tests + migration up/down
  + compose integration smoke.

### 7.2 Per-feature-set workflow (repeatable, same for every set)

```bash
# 0) Ready: dependencies verified in target env
python tools/feature_sets.py check-env --set FS-XX --env dev

# 1) Worktree/branch
git switch -c feat/fs-XX-<slug>

# 2) Implement: modules, migrations, contracts, tests per feature-sets/FS-XX.md + DATA-MODEL.md
#    (write scope = only that set's modules/migrations/contracts/spec)

# 3) Local gates
python tools/feature_sets.py validate
pytest app/tests -q
docker compose run --rm api alembic upgrade head
docker compose run --rm api pytest app/tests/invariants -q

# 4) PR -> CI runs L0/L1; independent verifier reviews; merge

# 5) Deploy to dev
scripts/deploy_set.sh FS-XX dev        # wraps: check-env -> migrations -> deploy -> exit checks

# 6) Record evidence and proceed
python tools/feature_sets.py record --set FS-XX --env dev --status verified

# 7) WAVE CLOSE -- golden end-to-end workflow pass, then promote the wave
#    purchase -> receive -> stock -> [manufacture] -> commit -> fulfill -> replenish suggestion,
#    using the stage subset available at this wave + seeded boundary demand (see §4.1).
#    Mandatory before promoting a wave; a failure blocks the next wave's entry gates.
pytest app/tests/e2e/test_inventory_workflow.py -q
python tools/feature_sets.py deploy-order --phase 1    # confirm the verified wave/order
python tools/feature_sets.py coverage                 # coverage still 100%
```

> **Parallel-build note (Δ-PAR):** when an FS is scheduled in a parallel build phase (B2–B5), run this
> workflow in the card's dedicated worktree `feat/PAR-xx`; do not edit shared files (route registry,
> contract aggregator, migration chain order, conftest, seed, compose/scripts) — those are integrator-owned.
> Merge-train order and contract freezes are in `design/parallel-plan.json`; deploy remains wave-ordered.

### 7.3 Roles

- **Implementer (agent/human):** one card at a time in an isolated worktree; concurrent FS cards are allowed
  when write scopes are disjoint and contract freezes are satisfied (Δ-PAR); updates status only via evidence.
- **Verifier (independent):** does not implement the same set; runs the manifest/coverage audits,
  re-runs invariant and contract tests, inspects evidence, and can block a deployment.
- **Lead/integrator:** owns `feature-sets.json`, wave sequencing, environment promotion and the
  final status report; resolves conflicting write scopes.
- **Change control:** a new feature set or dependency edge is a manifest change + `validate` +
  plan review; no ad-hoc edges allowed.

### 7.4 Definition of Done per set

See framework §7. In short: code + migrations + OpenAPI/event contracts; unit/contract/invariant
tests; entry/exit checks in the manifest; feature flag; tenant-isolation test; audit events; runbook;
deployment and rollback rehearsed; verification evidence recorded. No set is "done" because code
merged — only because its **exit gate passed in the environment** and dependants can build on it.

---

### 7.5 Parallel build execution (Δ-PAR)

- **S0 first:** all seven scaffold cards (S0-01…S0-07) pass `check_parallel_plan.py --s0`; see Δ-PAR §2.
- **Phases:** B1 FS-00 → B2 FS-01 ∥ FS-02 → B3 FS-03 → FS-04 ∥ FS-05 → **B4 wide build (8 builds:
  FS-06/07, FS-08, FS-09, FS-10, FS-11, FS-12, FS-13+FS-14)** → B5 FS-15. Deployment stays
  `0A→0B→0C→1A…1F→2`; the integrator deploys one set at a time and records L3 evidence.
- **Contract-first:** S0 generates OpenAPI/event skeletons for all 16 sets; freezes C0–C5 gate phase entry
  and merge readiness (`design/parallel-plan.json`). Post-freeze changes are additive-only with contract-owner
  review; breaking changes bump the contract version and re-freeze.
- **Ownership:** builders own only their module/router/contract-fragment/test namespace; the integrator owns
  the route registry, OpenAPI/event aggregation, the single Alembic chain, `conftest`, seed, compose/scripts
  and plan-status updates. Plan docs/specs are read-only for builders.
- **Migrations:** revision numbers are pre-allocated per FS (DATA-MODEL §21.1); builders create revisions in
  their branch; the integrator merges them in chain order and runs upgrade/downgrade on the integration branch.
- **Isolation/resources:** one compose project/DB per builder (`inv-PAR-xx`), testcontainers for tests,
  disk/container headroom checked before the B4 launch; DSH's 8-durable-teammate cap means batch or
  `workflow`-fan-out extra short-lived work.
- **Validator:** `python tools/check_parallel_plan.py` (plan valid) and `--s0` (execution-ready) run in CI.

---

## 8. Testing and verification strategy

| Layer | What it proves | Where |
|---|---|---|
| Static plan gate (L0) | DAG valid, no cycles, dependency ranges satisfiable, coverage complete | `tools/feature_sets.py validate` + CI |
| Unit tests | business rules per service, UoM math, costing math, state machines | per module |
| Domain invariants | qty conservation, valuation equation, no negative available, append-only ledger, balance == Σ ledger, commitment ≤ on hand, etc. | `tests/invariants/` (post-deploy too) |
| Contract tests (L1) | providers keep their published contract; consumers fit it | `tests/contracts/` |
| Migration tests | up/down on empty and seeded DB; N-1 compatibility | CI |
| Concurrency tests | competing postings on the same item/location; deadlock-free locking order; idempotent retries | CI + staging |
| Entry/exit gates (L2/L3) | deployment-state consistency and post-deploy health | `feature_sets.py` + `scripts/deploy_set.sh` |
| Reconciliation jobs | read models/balances match the ledger; open docs vs commitments; outbox drained | nightly per env |
| Independent audit | coverage of all 1,348 topics, adversarial manifest tests, executability of every gate command | `verification/VERIFICATION-REPORT.md` |

**Invariant suite is the acceptance backbone:** every feature set ships invariant tests; a dependent
feature set's exit gate re-runs the invariants of the domains it touches. This is what "checks each
other's dependencies" means in practice.

---

## 9. Non-functional requirements (Phase 1)

From [`design/FEATURE-SET-FRAMEWORK.md`](design/FEATURE-SET-FRAMEWORK.md) §9: ledger posting
p95 ≤ 300 ms @ 20 tx/s; balance reads p95 ≤ 150 ms; ≥ 200k ledger lines/hour batch import; zero
qty/valuation drift tolerance; at-least-once events with idempotency → effectively-once; append-only
audit; per-endpoint tenant **and user** isolation matrix (incl. multi-membership switch/revocation);
99.9% target in prod with managed Postgres. Architecture maps
each NFR to a mechanism and test.

---

## 10. Risks and mitigations

| # | Risk | Impact | Mitigation |
|---|---|---|---|
| R1 | **Scope is very large** (1,348 topics, 5 SCM areas) | Plan looks complete but execution stalls | Strict feature-set slicing with independent deployability; waves; Phase 1 excludes all UI/integration; coverage map keeps deferred work explicit. |
| R2 | **Costing complexity** (FIFO/LIFO/average/group-average/standard/specific, recalculation, backdating) | Incorrect valuation, silent drift | Dedicated costing feature set after ledger; golden-case test corpus; nightly reconcile; cost recalculation engine specified up front; invariants (value equation) enforced. |
| R3 | **Concurrency on hot items** | Oversell/negative stock, deadlocks | Documented lock ordering, DB constraints, idempotency keys, concurrency test suite; no in-memory balances. |
| R4 | **Finance/GL boundary** is fuzzy (posting, COGS, variances, period close) | Scope creep into accounting | GL is an **interface**: account references + posting instructions recorded; no ledger-of-accounts/period close in Phase 1 (see §12). |
| R5 | **Order management boundary** (sales orders drive commitments/fulfillment) | Missing demand side | Core ledger accepts commitment/fulfillment/return commands via API; SO domain is external; seeded test doubles for consumer contract tests. |
| R6 | **NetSuite semantics are nuanced/ambiguous** in docs (some pages 404, SuiteApps vary) | Wrong behavior encoded | Catalog + traceability; mark `ASSUMPTION/EXTENSION`; verifier samples doc claims; design decisions listed for review. |
| R7 | **Single-writer bottleneck** in monolith | Throughput ceilings | Outbox + async projection; batch import path; partitioning by tenant/date; measure with load test. |
| R8 | **Plan/manifest drift** | Gates become fiction | Manifest is source of truth; coverage/status checks in CI; verifier audits; plan regeneration of status tables. |
| R9 | **SuiteApp-only features** (Quality Management, Smart Count, Warranty, Auto UPC, Customer Part Numbers) | Expectation mismatch vs "all features" | Explicitly mapped into Phase-1 backend where semantics are backend (quality inspections, counts, UPC assignment, part numbers) or deferred with reason; coverage map lists each. |

---

## 11. Status tracking

Environment status is recorded in `deployment-state.<env>.json` (generated by the check tool). Plan
status is summarized here:

<!-- AUTO:STATUS-TABLE-START -->
| FS | Feature set | Wave | dev | staging | prod |
|---|---|---|---|---|---|
| **FS-00** | Platform Core (tenancy, identity, audit, events, jobs, boundaries) | 0A | — | — | — |
| **FS-01** | Item Master and UoM Engine | 0B | — | — | — |
| **FS-02** | Pricing Engine (item pricing + advanced pricing + effective-date updates) | 0B | — | — | — |
| **FS-03** | Storage and Inventory-Status Registry | 0C | — | — | — |
| **FS-04** | Inventory Ledger Core (posting, balances, commitments, transfers, adjustments, imports) | 0C | — | — | — |
| **FS-05** | Costing and Valuation Engine | 0C | — | — | — |
| **FS-06** | Numbered Inventory (lots and serials) | 1A | — | — | — |
| **FS-07** | Inventory Counts and Cycle Counts | 1A | — | — | — |
| **FS-08** | Procurement (vendor master, P2P documents, receipts, bills, returns) | 1B | — | — | — |
| **FS-09** | Manufacturing (assembly/BOM, work orders, WIP, routing, scheduling inputs) | 1C | — | — | — |
| **FS-10** | Replenishment and Planning (AIM, demand, supply/MRP, DRP, allocation, ATP) | 1D | — | — | — |
| **FS-11** | Warehouse Management Execution (server-side tasks only) | 1E | — | — | — |
| **FS-12** | Quality Management (specs, inspections, COA) — SuiteApp semantics backend | 1F | — | — | — |
| **FS-13** | Consigned Inventory Management | 1F | — | — | — |
| **FS-14** | Extensions (warranty, customer part numbers, UPC registry) | 1F | — | — | — |
| **FS-15** | Reporting Read Models and Reporting API | 2 | — | — | — |
<!-- AUTO:STATUS-TABLE-END -->

---

## 12. Assumptions, boundaries and open decisions

1. **Reference stack** is an explicit assumption (no existing repo/stack was provided). Framework §3
   can be swapped; the feature-set DAG, contracts and gates are stack-agnostic.
2. **GL/accounting** is external: feature sets capture account references and posting instructions;
   accounts, journals, tax and period close are out of Phase 1 (contract boundary, test double).
3. **Order management / customer master** are external: commitments, fulfillment and customer
   references arrive through APIs/read models; the plan builds the inventory-side behavior only.
4. **Multi-currency**: valuation stored in base currency; transaction currency captured; FX
   revaluation is out of Phase 1 unless it is required by an in-scope documented feature.
5. **Multi-tenant users (Δ-MT)**: one deployment/DB with pooled tenants; RLS + app filters + composite
   tenant FKs; global identity + per-tenant membership (a user may belong to several tenants);
   single-membership users behave exactly as before. Details in §4.2 and the Δ-MT addendum.
6. **Prod topology**: compose for dev/staging; managed Postgres + container runtime for prod.
7. **Timeline/effort** is indicative only; sizing is per feature set (S/M/L) and the critical path is
   in FEATURE-SETS.md. Agent-assisted execution compresses calendar time but not gate rigor.
8. **Notices/copyright**: NetSuite help content is used as a behavioral reference only; no Oracle
   content is reproduced at length; feature titles/URLs are cited for traceability.
9. **Locked decisions D1–D8 and deltas Δ-1…Δ-3, Δ-MT (D-29), Δ-RBAC (D-30) and Δ-SSO (D-31)** (§3.1) are part of the baseline. Changing one is a
   plan change: update the manifest/specs, re-run `tools/feature_sets.py validate`, record it here.
10. **finance_journal_intent (Δ-1)**: FS-00 owns the append-only journal-intent table and read API;
    with Δ-MT (identity/invitation/session) and Δ-SSO (SSO connections/identity links/group maps) the
    platform context holds **34** tables; with Δ-CFG/Δ-REUSE the total is **287 base tables + 21 views**.
    The authorization catalogue is **255 scopes / 7 role templates / 14 SoD pairs** and there are
    **146 scoped + 10 global = 156 testable rules**; invariants **INV-01..INV-33**.
11. **FS-15 (Δ-3)** stays Phase 1 because it is backend read models/query API only; analytics
    workbooks, dashboards and BI tooling remain deferred Phase 2+.
12. **Tooling split**: `tools/feature_sets.py` + `tools/render_plan_tables.py` are plan-package
    tooling (stdlib Python, runnable now). `scripts/deploy_set.sh` and `scripts/enable_flag.sh` are
    *implementation-repo* deliverables specified by this plan, not prerequisites for reading the plan.
13. **API base path**: canonical base path is `/api/v1` (ARCHITECTURE §5.9). The `/v1` spelling that
    appears in some prose/manifest rationale text is shorthand, non-normative; specs use `/api/v1`.
14. **Cross-spec interface resolutions** (recorded in each spec §14; no DAG/contract changes):
    FS-01 additive `apply_planning_values` facade (FS-10 write-through); FS-00 `order.demand` open-demand
    read surface (FS-10 planning); FS-04 generic `POST /v1/inventory-transactions` posting facade for
    internal consumers and ledger-only valuation scope (sell-side prices stay with FS-02/order boundary);
    FS-05 is the cost-revaluation provider with FS-09-supplied WIP scope (no upward FS-05→FS-09 read);
    FS-11/FS-12 consume authoritative FS-09 events (`work_order.built`, `assembly.built`, `wip.completed`);
    FS-15 available-to-build is a pass-through of FS-09 `atb-v1`.
15. **Authorization model (Δ-RBAC)**: capability scopes + role templates + data scopes are an
    `EXT/ASSUMPTION` design (NetSuite role levels/UI are not reproduced); the authoritative catalogue is
    `design/permissions.json` (255 scopes / 241 assignable / 7 system role templates / 14 SoD pairs).
    Role/permission changes are effective on the next request; SCIM role sync, ABAC and delegated
    administration are deferred; final rules 146/156, INV-33 at v1.5.0.
16. **Authentication (Δ-SSO)**: Dex is the OIDC identity broker; per-company SSO connections federate
    SAML 2.0/OIDC/LDAP-AD; the platform remains the authorization authority and token issuer. Reference
    deployment is one isolated Dex issuer per active connection (shared Dex allowed for dev/test);
    local password fallback is retained for non-SSO tenants/bootstrap. SCIM, IdP-initiated SAML,
    cross-company single logout and SSO admin UI are deferred. EXT capability; no catalog changes.
17. **Parallel execution (Δ-PAR)**: build parallelism is bounded by disjoint write scopes + frozen contracts
    + the S0 scaffold gate; deployment stays wave-ordered. Up to 8 concurrent FS builders in the wide wave;
    DSH sessions cap durable teammates at 8, so extra verification/e2e work is batched or run via `workflow`
    fan-out. Execution-only amendment: no DB/contract/DAG/coverage changes.
18. **Manufacturing compatibility & unbuild reuse (Δ-CFG/Δ-REUSE)**: compatibility attributes/slots/rules
    with dual assembly+BOM and order/customer/region scopes, configurable HARD/WARN enforcement and audited
    override, gated at every manufacturing path that needs compatible parts; unbuild dispositions
    return/used/scrap/hold with original-cost default valuation configurable to NRV/manual. FS-09-only,
    EXT beyond the SCM docs; contract `manufacturing.execution@1.1.0` additive; no DAG/wave changes.

---

## 13. Plan acceptance criteria

The plan was independently audited on 2026-10-21 and accepted. Evidence and reproduction commands are in
[`verification/VERIFICATION-REPORT.md`](verification/VERIFICATION-REPORT.md); the checklist below is the
acceptance contract and its current state:

- [x] `python tools/feature_sets.py validate` passes on `design/feature-sets.json` — PASS, 0 warnings
      (16 sets, 116 edges, 21 contracts, 10 waves).
- [x] Dependency DAG has no cycles; every edge has a declared contract range; deployment order is
      computable and respects phases/waves — PASS (`order --phase 1` = 16 steps).
- [x] All 1,348 catalogued source topics are accounted for in `FEATURE-COVERAGE.md` (769 in-scope,
      579 deferred); SCM Mobile excluded — PASS, 0 missing/extra/duplicates.
- [x] Every feature set has entry checks, exit checks, acceptance criteria, rollback and source docs —
      16/16 PASS.
- [x] Every feature-set spec covers data model, APIs, events, jobs, rules/invariants and tests —
      16/16 specs audited against the template (694 checks, 0 findings after delta re-audit).
- [x] `check-env`/`record` gate commands work against sample deployment states; adversarial manifests
      (cycle, dangling dependency, phase inversion, missing gate) are rejected by the tool —
      PASS, 13 adversarial cases + 50/50 unit tests.
- [ ] Runbook commands executable from a clean checkout — **PARTIAL by design**: 49/133 gate commands
      are plan-package commands and were executed/verified; the other 84 are implementation-repo
      commands (`pytest app/tests/…`, `docker compose`, `scripts/deploy_set.sh`) that cannot exist
      before the implementation repo is created. Close this box when the repo skeleton exists.
- [x] Independent verifier signs off with no open high-severity findings — **PASS** (0 BLOCKER,
      0 MAJOR, 0 open MINOR; 2 accepted INFO limitations documented in the report).

---

## 14. Appendix — source mapping and metrics

- SCM set root: https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/set_N1734902.html
- Catalogues (JSON = every topic id/title/URL/classification/summary):
  `research/features-item-inventory.{md,json}` (412 topics),
  `research/features-mfg-procurement-wms.{md,json}` (936 topics).
- Extraction date: 2026-10-21. SCM Mobile book never crawled.
- Known doc gap: one 404 page (`section_N1515696627.html`, Inventory Status) evidenced through the
  feature-enablement page instead.
- Source classification totals: core-backend 885, suiteapp 204, mobile 126, ui-only 49, analytics 59,
  integration 22, unclear 3 (sums to 1,348).
- Coverage disposition (after applying the plan's scope rules D4/D8): 769 topics mapped to an
  owning Phase-1 feature set; 579 deferred (UI 61, MOBILE 126, ANALYTICS 46, INTEGRATION 156,
  SUITEAPP 190); SCM Mobile topics included in the catalogs: 0 (book never crawled).
- Rebuild the generated tables/status with `python tools/render_plan_tables.py --inject`; verify with
  `--check` in CI.
