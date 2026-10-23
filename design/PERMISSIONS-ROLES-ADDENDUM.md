# Permissions & Roles — Binding Design Addendum (Δ-RBAC, plan v1.2.0)

**Status:** Lead-approved binding amendment to the verified plan package (v1.0.0 → v1.1.0 → v1.2.0).
Where this addendum conflicts with earlier text, **this addendum wins**; all affected documents must conform (§10).
**Origin:** user requirement "ensure permissions and roles are also part of the plan."
**Marking:** the capability-scope + role-template model is a design choice beyond the NetSuite SCM docs
(NetSuite role levels/UI are not reproduced); RBAC topics are already covered by FS-00 (`mpw-797/798/800`,
`iri-198`) — this addendum makes the model complete, reconciled and machine-checkable. No new catalog topics;
coverage stays 1,348 topics (769 in-scope / 579 deferred).

---

## 1. What existed vs what Δ-RBAC adds

Already in v1.1.0: RBAC tables (`role`, `permission_scope`, `role_permission`, `user_role`,
`user_data_scope`), a ~88-scope FS-00 seed catalogue, 7 seeded system roles, auth scopes declared in each
feature-set endpoint matrix, approval definitions + requester≠approver SoD in the approval engine, and
`require_scope` enforcement text.

**Gap found:** the spec endpoint matrices reference **249 distinct capability scopes**, while FS-00 seeded
only 88; there was no single authoritative catalogue, no machine validation that every route/scope is
catalogued, no role-template/permission assignment lifecycle, no privilege-escalation/last-admin guards,
and no consolidated SoD policy.

Δ-RBAC adds exactly those, with no new database tables:
- **Authoritative machine-readable catalogue** `design/permissions.json`: 249 scopes (235 assignable,
  1 platform-only, 13 service-only, 1 legacy alias), 7 system role templates, 14 SoD pairs, public and
  self-service allowlists, global authorization rules, ownership by feature set.
- **Role lifecycle and administration** (FS-00): role CRUD/clone, permission and data-scope mapping,
  role assignment to memberships, effective-permissions APIs, safety guards.
- **Enforcement + verification:** deny-by-default route-scope coverage, scope/role/SoD tests, a new
  `feature_sets.py permissions` plan gate, and FS-00 exit-gate integration.

---

## 2. Permission model

- **Capability scope** = one stable string `<domain>.<resource>.<action>` (e.g. `procurement.po.approve`,
  `inventory.adjustment.post`, `reporting.read`) granting access to one class of API action.
  Exactly **one capability scope** guards each non-public route. Scope names are frozen once shipped;
  additive only; renaming/removal requires a major contract-style change and consumer impact list.
- **Data scopes** are orthogonal row-level restrictions (`LOCATION/CLASS/DEPARTMENT/SUBSIDIARY`, with
  descendants) applied to queries and posting targets in addition to the capability scope.
- **Scope classes** (`design/permissions.json`):
  - *assignable tenant scopes* (235) — may be granted to tenant roles;
  - *approval scopes* (`requires_approval=true`) — actions that must route through `approval_request`
    when a definition matches;
  - *platform-only* `platform.tenant.manage` — global platform admins only, never grantable by a tenant
    role; service-only `platform.internal` — internal service principals only;
  - alias `platform.user.manage` → `platform.users.manage` (legacy compatibility, not seeded).
- **Catalogue authority:** FS-00 provisions each tenant's `permission_scope` rows from
  `design/permissions.json`; `permission_scope` gains `is_platform_only`, `is_assignable`,
  `owner_feature_set`; the catalogue itself is versioned with `plan_version` and validated in CI.
- **Approval separation:** `*.approve` scopes are distinct from create/post scopes; a requester can never
  approve their own document (global SoD rule), and non-admin role templates cannot combine SoD pairs.

## 3. Role model

- **Seven system role templates** seeded per tenant (non-editable, cloneable):
  `admin` (Tenant Administrator — all 235 assignable scopes), `planner`, `buyer`, `warehouse_operator`,
  `cost_accountant`, `quality_inspector`, `readonly`. Exact scope lists are in
  `design/permissions.json.role_templates` (computed from the catalogue, not hand-maintained prose).
- **Custom roles:** tenant admins create/clone roles and choose a subset of **assignable** scopes;
  platform-only/service-only scopes are rejected (`422 SCOPE_NOT_ASSIGNABLE`); SoD-conflicting pairs are
  rejected in a single non-admin role (`409 SOD_CONFLICT`); the `admin` template is break-glass and exempt
  but every use is audited.
- **Assignments:** `user_role` effective-dated per membership; multiple roles combine as a union of
  capability scopes; data scopes combine per documented precedence; assignments are tenant-scoped and
  never cross tenants.
- **Effective permissions:** resolved per request from the DB (role assignments + API-key scopes + data
  scopes), so role/permission changes take effect immediately; membership suspension/revocation still
  bumps `token_version` and revokes sessions (Δ-MT).
- **Safety guards:** only `platform.role.manage` holders manage roles; only `platform.users.manage`
  holders assign roles; actor cannot grant scopes absent from the assignable catalogue; system templates
  cannot be edited/deleted (cloned only); last active tenant administrator cannot be suspended,
  deactivated or stripped of the admin role (`409 LAST_TENANT_ADMIN`); all role/permission/assignment
  changes are audit events.
- **SoD policy:** 14 hard conflict pairs in `design/permissions.json` (e.g.
  `procurement.po.create` ↔ `procurement.po.approve`, `inventory.adjustment.post` ↔
  `inventory.adjustment.approve`, `quality.inspect` ↔ `quality.inspection.approve`) plus the global
  requester≠approver rule for every approval-required document. Enforcement is HARD by default
  (tenant setting `rbac.sod_enforcement=HARD`; `WARN` only downgrades custom-role creation warnings,
  never approval-time enforcement).

## 4. Enforcement (deny by default)

- Route guards: every non-public route declares exactly one capability scope; missing/duplicate/unknown
  scope fails CI (`test_route_scope_coverage.py`). Exceptions are only the explicit allowlists in
  `permissions.json`: public (`/healthz`, `/readyz`, `/openapi.json`, login, select-tenant, invitation
  accept) and self-service (switch-tenant, refresh, logout, `/me`, `/me/tenants`).
- Middleware order: authenticate (JWT/API key) → resolve membership/identity/tenant → membership status →
  capability scope check → data-scope filter/posting validation → domain logic. Deny with 403
  `FORBIDDEN_SCOPE`; unknown scope in code fails startup/CI.
- Every feature set's exit gate includes its route-scope tests (403 without scope, 200 with scope,
  cross-tenant/data-scope denial) in addition to the catalogue validation.
- Approvals: `approvals.decide` requires an approval-capable scope + definition step authorization +
  requester≠approver; posting scopes are validated again at post time (separation of entry/approval/post).
- Audit: role created/updated/cloned, permission mapping changed, role assigned/removed, data scope
  changed, SoD override attempt, last-admin guard trigger — all audited with before/after and actor.

## 5. API surface added/extended (FS-00)

- `GET /api/v1/admin/permissions` — catalogue list/filter (`domain`, `action`, `assignable`,
  `requires_approval`, `owner_feature_set`, `version`).
- `GET|POST /api/v1/admin/roles`; `GET|PATCH|DELETE /api/v1/admin/roles/{id}` (inactivate, not hard
  delete); `POST /api/v1/admin/roles/{id}/clone`; `PUT /api/v1/admin/roles/{id}/permissions`
  (scope set, diff-audited); `PUT /api/v1/admin/roles/{id}/data-scopes`.
- `POST /api/v1/admin/users/{id}/roles` (assign/remove effective-dated roles; last-admin guard);
  `GET /api/v1/admin/users/{id}/effective-permissions`;
  `GET /api/v1/me/effective-permissions` (self).
- `GET /api/v1/admin/sod-policy`; `PUT /api/v1/admin/sod-policy` (tenant setting `rbac.sod_enforcement`,
  HARD default; cannot disable approval-time requester≠approver).
- Existing tenant-admin endpoints (`/admin/users*`, `/admin/tenant-settings*`, `/admin/api-keys*`) gain
  role-assignment and permission guards per the catalogue.

## 6. Rules, invariants and counts

| ID | Rule |
|---|---|
| `DM-RBAC-01` | **Deny-by-default route coverage:** every non-public route declares exactly one capability scope from `permissions.json`; unknown/duplicate/missing scopes fail CI; only explicit public/self-service allowlists may omit a scope. |
| `DM-RBAC-02` | **Grant safety:** tenant roles contain only assignable scopes; platform-only/service-only scopes never appear in tenant roles; system templates are non-editable (clone-only); SoD-conflicting pairs are rejected in one non-admin role; last active tenant admin cannot be locked out; every role/permission/assignment change is audited. |
| `DM-RBAC-03` | **Effective permissions & separation of duties:** effective scopes/data scopes are resolved per request from role assignments and take effect immediately; every approval requires requester≠approver and approval-scope authorization; posting revalidates its scope; changes emit audited events. |
| `INV-31` | **Authorization completeness:** every tenant route is guarded by exactly one catalogued capability scope; scope and data-scope denials are correct; role template catalogue is coherent; SoD is enforced at assignment and approval time; platform-only scopes are unreachable from tenant roles. |

**Counts:** 249 scopes (235 assignable; 1 platform-only `platform.tenant.manage`; 13 service-only
incl. `platform.internal`; alias `platform.user.manage`), **7 system role templates**, **14 SoD pairs**.
No table-count change: still **277 base tables / platform 31 / 21 views** (columns added to
`permission_scope`/`role`; tenant setting namespace `rbac`). Scoped rules **137 → 140**, total
**147 → 150** (140 + 10 global); invariants **INV-01..INV-31**. Posting types (31), commands (6) and the
feature-set DAG/edges/contracts are unchanged. Manifest `plan_version` → **1.2.0**; FS-00 exit checks gain
`python tools/feature_sets.py permissions`.

## 7. Verification

- `python tools/feature_sets.py permissions` validates `design/permissions.json`:
  unique scope strings; valid domains/actions; assignable/platform-only/service-only coherence; 7 role
  templates reference only catalogue scopes; non-admin templates contain no SoD pair; alias targets valid;
  SoD pairs valid; counts reported (249/7/14); spec endpoint scans flag unknown scope tokens; exit codes
  0/2.
- FS-00 exit gate adds the command above + `test_route_scope_coverage.py` (OpenAPI routes ↔ declared
  scopes), `test_rbac_roles.py` (role CRUD/clone/assignment/last-admin/privilege escalation),
  `test_sod_policy.py` (role assignment conflicts + requester≠approver), and per-feature-set scope tests.
- Each FS spec's §6 Auth-scope column must use only catalogue scopes; the verifier reconciles all 16 specs
  against the catalogue and reports any residual mismatch as a finding.

## 8. Out of scope / deferred

Role/permission admin UI, SuiteFlow-style workflow customisation (D-23), attribute-based access control
(policy engine), per-field/row-level ACLs beyond the documented data scopes, external IdP/SCIM group→role
mapping, delegated administration hierarchies, approval-workflow visual designer. Phase 2+.

## 9. Document update map (binding)

| Document | Required changes |
|---|---|
| `design/permissions.json` | **Authoritative artifact (Lead-authored, exists).** 249 scopes, 7 role templates, 14 SoD pairs, allowlists, global rules, counts, ownership. |
| `design/ARCHITECTURE.md` | §5.2 permission/role model, role lifecycle, privilege-escalation & lockout guards, effective-permission resolution, SoD (assignment+approval), route-scope coverage; §7.3 INV-31; §8 D-30; §0 counts (150/140/INV-31); §11 |
| `design/DATA-MODEL.md` | §3 `permission_scope` (+`is_platform_only`,`is_assignable`,`owner_feature_set`), `role` (+`is_template`,`is_editable`), tenant setting namespace `rbac`; DM-RBAC-01/02/03 + INV-31; §20 counts (140/150/INV-31); §21 seed reference to `design/permissions.json`; §24; no table-count change |
| `feature-sets/FS-00.md` | §4.3 replaced by catalogue summary + link to `design/permissions.json`; role/permission/data-scope/SoD use cases; role-admin & effective-permission APIs; scope/SoD tests; §11 gate command `feature_sets.py permissions`; counts (249/7/14, 140/150/INV-31); fix FS-00/FS-01 endpoint scope cells missing scopes (public/self-service/reference reads) to match allowlists and `items.read` |
| `design/FEATURE-SETS.md` | §1 FS-00 row unchanged tables; §7.1 FS-00 RBAC scope paragraph + permissions gate; plan version references |
| `design/feature-sets.json` | `plan_version` 1.2.0; FS-00 `exit_checks` add `python tools/feature_sets.py permissions`; nothing else |
| `PLAN.md` | §3.1 decision Δ-RBAC (D-30); new §4.3 "Permissions, roles and approvals"; §12 item 9/10 updates (150/140/INV-31/249/7/14); header stamp 1.2.0 after verification; generated tables |
| `README.md` | RBAC capability line; counts (249 scopes / 7 roles / 14 SoD pairs; rules 140/150); status |
| `verification/VERIFICATION-REPORT.md` | Delta audit section, tool `permissions` evidence, refreshed snapshot, verdict |

## 10. Resolved clarifications

- Scope count baseline 88 was a partial seed list; the authoritative reconciled catalogue is **249**
  (endpoint matrices across all 16 specs + FS-00 seed + reference-read scopes + the 28 audit-found action scopes, 13 of which are service-only job/task scopes). No endpoint behaviour changes.
- `platform.user.manage` is a legacy alias of `platform.users.manage`, not an additional seed scope and not seeded.
- Admin template is the only role permitted to contain SoD pairs (break-glass); it is non-editable and
  all its assignment changes are audited.
- Permission changes apply on the next request (scopes resolved from DB); sessions are not force-refreshed
  on role edits, but suspension/revocation does invalidate via `token_version` (Δ-MT).
