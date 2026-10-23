# Multi-Tenant Users — Binding Design Addendum (Δ-MT, plan v1.1.0)

**Status:** Lead-approved binding amendment to the verified v1.0.0 plan package. Applies to Phase 1
(core backend only; no UI/mobile). Where this addendum conflicts with v1.0.0 text, **this addendum wins**;
all affected documents must be updated to conform (map in §10).
**Origin:** user requirement "the plan must be able to handle multi-tenant users."
**Marking:** the shared-identity/membership model is an **EXT**ension beyond the NetSuite SCM help
(NetSuite accounts are separate and the SCM docs do not specify a SaaS identity model). It does not
change feature coverage: FS-00 still owns 4 catalog topics; no new catalog ids.

---

## 1. What already exists vs what this adds

Already in v1.0.0 and unchanged: one deployment / one database with pooled tenants; `tenant_id` on every
tenant-owned row; first column of every key/index; composite tenant FKs; PostgreSQL RLS with
`app.tenant_id`; application-side tenant filter + flush guard; tenant-scoped RBAC
(`role`/`permission_scope`/`role_permission`/`user_role`), data scopes, per-tenant feature flags,
per-tenant number sequences, tenant lifecycle (`ACTIVE/SUSPENDED/OFFBOARDING`, `DM-PLAT-09`), tenant
offboarding, tenant-keyed events/jobs/object storage.

What Δ-MT adds: a **global user identity** plus a **per-tenant membership principal**, so a real person
can hold accounts in one or many tenants, select/switch tenants at login, be invited into a tenant, and
be revoked independently per tenant — **without weakening tenant isolation**. Single-tenant users are the
default case (exactly one ACTIVE membership).

---

## 2. Identity model (two levels)

| Level | Table | Scope | Purpose |
|---|---|---|---|
| Global identity | `platform.user_identity` **(new)** | **not tenant-scoped** (auth-only exception) | One row per real human: login identity, email, credentials/external-IdP subject, MFA flag, global status, platform-admin flag |
| Tenant membership | `platform.app_user` (extended) | tenant-scoped | The tenant principal used by all FKs/audit/roles; one per `(tenant, identity)`; carries membership lifecycle + `token_version` |
| Invitation | `platform.tenant_invitation` **(new)** | tenant-scoped | Invite an email into a tenant with roles; single-use expiring token; creates identity on acceptance if new |
| Session | `platform.user_session` **(new)** | tenant-scoped | Refresh-token session bound to one tenant principal; rotation/replay detection/revocation |

### 2.1 `platform.user_identity` (global; not RLS-tenant-scoped)
`id UUID PK (v7)`, `email CITEXT NOT NULL UNIQUE`, `display_name`, `password_hash NULL` (SSO/external IdP
mode; local password optional), `sso_subject TEXT NULL`, `mfa_enabled BOOL DEFAULT false`,
`is_platform_admin BOOL DEFAULT false`, `status (ACTIVE/DISABLED/LOCKED)`, `last_login_at`,
`created_at`, `updated_at`, `version`. EXT. Access restricted to the auth service role; **never** exposed
through tenant business routes; tenant-owned tables never FK to it except `app_user`.

### 2.2 `platform.app_user` (extend existing)
Add: `identity_id UUID NOT NULL` → `user_identity.id` (documented cross-scope FK exception),
`membership_status (INVITED/ACTIVE/SUSPENDED)`, `joined_at`, `invited_by UUID NULL`,
`token_version INTEGER NOT NULL DEFAULT 0`. Change unique key `(tenant_id, email)` → **`(tenant_id,
identity_id)`**; keep `email` only as a denormalized read-only display snapshot synced from the identity
(no tenant business dependency on it). Existing `user_role` / `user_data_scope` remain keyed by
`app_user` (per tenant) — roles are per-membership, never global.

### 2.3 `platform.tenant_invitation` (new, tenant-scoped)
`id`, `tenant_id`, `email CITEXT`, `intended_role_ids UUID[]` (validated against tenant roles),
`token_hash TEXT` (single-use, 7-day expiry), `status (PENDING/ACCEPTED/REVOKED/EXPIRED)`, `invited_by
app_user.id`, `expires_at`, `accepted_at NULL`, `accepted_app_user_id NULL`; partial unique
`(tenant_id, email) WHERE status='PENDING'`.

### 2.4 `platform.user_session` (new, tenant-scoped)
`id`, `tenant_id`, `app_user_id`, `identity_id`, `refresh_token_hash TEXT UNIQUE`, `status
(ACTIVE/REVOKED/EXPIRED)`, `issued_at`, `expires_at`, `revoked_at NULL`, `revoked_reason NULL`, `ip`,
`user_agent`, `last_seen_at`; indexes `(tenant_id, app_user_id, status)`, partial active
`WHERE status='ACTIVE'`. Access tokens are stateless and short-lived; refresh tokens are hashed at rest,
rotated on use, and bound to `(session, tenant)`.

---

## 3. Authentication and tenant context

- **Access token** (JWT, 15 min): `sub`=identity_id, `uid`=app_user_id, `tid`=tenant_id, `tv`=token_version,
  `jti`, `iat/exp`, compact `scopes` claim. The **only** source of the active tenant is the verified token
  claim; no header/query parameter may override it.
- **Refresh token** (30 days, rotating): random opaque value, hash stored; presented with tenant context;
  reuse of a rotated token revokes the session (replay detection).
- **Login** `POST /api/v1/auth/login` (`email`, password or external-IdP assertion):
  one ACTIVE membership → issue tenant token pair; multiple → `200 {status:"TENANT_SELECTION_REQUIRED",
  selection_token (5 min, identity-scoped), tenant_choices[]}`; none → `403 NO_ACTIVE_MEMBERSHIP`.
- **Tenant selection** `POST /api/v1/auth/select-tenant` (selection token + tenant) → token pair.
- **Switch tenant** `POST /api/v1/auth/switch-tenant` (authenticated) → new token pair for another ACTIVE
  membership; old session remains valid until revoked/expired; audit `auth.tenant_switched`.
- **Current user** `GET /api/v1/me` → identity + active membership + effective scopes;
  `GET /api/v1/me/tenants` → the caller's own memberships (tenant code/name, status, role summary).
- **Refresh/logout**: `POST /api/v1/auth/refresh`, `POST /api/v1/auth/logout` (revokes session).
- **Revocation**: membership SUSPENDED/revoked or user disabled → `token_version++`, all sessions revoked;
  refresh fails immediately, writes revalidate membership status, access tokens become unusable at the
  next write/refresh within the ≤15-min TTL bound.
- **External IdP/SSO/OIDC/SCIM** is a Phase-2 integration; Phase 1 stores `sso_subject` and keeps
  `password_hash NULL` when an external IdP is used.

---

## 4. Tenant provisioning and user administration

- **Platform admin** (global `user_identity.is_platform_admin=true`, no tenant role can grant it):
  `POST /api/v1/platform/tenants` (create tenant: code/slug, name, base currency, timezone, bootstrap
  admin invite), `GET /api/v1/platform/tenants`, `GET|PATCH /api/v1/platform/tenants/{id}`,
  `POST .../{id}/suspend|reactivate|offboard`, `POST .../{id}/bootstrap-admin`.
  Provisioning seeds: tenant settings defaults, permission catalogue, system roles, number sequences,
  feature flags (all default off), fiscal/currency projections; emits `tenant.provisioned`. Suspend
  blocks writes (`DM-PLAT-09`) and revokes sessions; offboarding is soft-state + export + archive, never a
  cascade delete of ledger/audit.
- **Tenant admin** (tenant-scoped scopes `platform.users.manage`, `platform.invitations.manage`,
  `platform.sessions.revoke`):
  `GET|POST /api/v1/admin/users`, `GET|PATCH /api/v1/admin/users/{id}`, `POST
  /api/v1/admin/users/{id}/revoke-sessions`, `GET|POST /api/v1/admin/invitations`,
  `POST /api/v1/admin/invitations/{token}/accept`, `POST /api/v1/admin/invitations/{id}/revoke`.
  Tenant admins cannot create tenants or grant platform-admin; platform-admin APIs require
  `platform.tenant.manage` and are audited with `SYSTEM_TENANT_ID` as actor tenant.
- **Invitation acceptance** binds the invited email to the accepting identity; creates the identity only
  if it does not exist, creates exactly one `app_user` membership + `user_role` rows (validated against
  tenant role catalogue) atomically; token single-use/expiring; audit `membership.activated`.

---

## 5. Isolation, security and enforcement

- Active tenant comes only from the verified token; request context sets RLS `app.tenant_id`; every
  tenant table access is RLS- and app-filtered; cross-tenant ids return **404** (no existence leak).
- `user_identity` / login / invitation acceptance are the **only** cross-tenant-aware paths; they are
  auth-service-only, return no tenant business data, and are excluded from tenant route handlers.
  `/me/tenants` queries only the caller's own identity membership rows.
- Platform-admin endpoints are global-only and audited with `SYSTEM_TENANT_ID`; no tenant role can grant
  those scopes. Tenant data returned to a platform-admin request still requires an explicit target-tenant
  context and is audited.
- Jobs/events/attachments remain tenant-keyed; session/refresh operations are tenant-bound; rate limiting
  (basic per-tenant/IP in config) applies to login/refresh/invitation endpoints.
- GDPR/PII: identity tables are the erasure/pseudonymisation target; ledger/audit never contain PII;
  identity retained while any membership/audit obligation exists.

---

## 6. Normative rules and invariants

| ID | Rule |
|---|---|
| `DM-PLAT-12` | `user_identity` is global and auth-only (EXT): no tenant business table may reference it; it is not exposed through tenant routes; `app_user` is the only link and is tenant-scoped. |
| `DM-PLAT-13` | Exactly one `app_user` per `(tenant_id, identity_id)`; tenant tokens are issued only for `membership_status=ACTIVE`; suspension/revocation bumps `token_version` and revokes sessions; refresh requires an ACTIVE session bound to that tenant principal. |
| `DM-PLAT-14` | Invitations are single-use, expiring, hashed, email-bound; role assignments are validated against the tenant role catalogue; only tenant admins (or platform admin for bootstrap) may invite/admin users; every membership, role, invitation and session change is audited. |
| `INV-30` | **Tenant/user isolation:** for every API request and job, the active tenant equals the token/context tenant and every row read/written belongs to it; cross-tenant probes return 404; identity/session tables expose no tenant business data; platform-admin operations are audited. |

**Count updates (mandatory across docs):** platform tables **28 → 31** (+3); total base tables
**274 → 277**; reporting views stay **21**; scoped rules **134 → 137** (+3); total testable rules
**144 → 147** (137 + 10 global); invariants **INV-01..INV-29 → INV-01..INV-30** (+1). Posting types
(31) and non-posting commands (6) unchanged. Migration ownership: new platform tables live in the
platform security migration family (`0001_platform_core` tenant/registry, `0002_platform_security`
identity/membership/invitation/session/RBAC, `0003_platform_events` outbox/jobs/idempotency).
Manifest `plan_version` becomes **1.1.0** (feature-set ids/edges/contracts unchanged).

---

## 7. Feature-set impact

- **FS-00 Platform Core only** (no new feature set, no DAG/contract-version change). FS-00 provided
  contracts are additive within `platform.core@1.0.0` / `platform.events@1.0.0` (auth endpoints, events).
- FS-00 exit gate adds the **tenant/user isolation matrix** (every endpoint × list/get/mutate ×
  cross-tenant probe) and membership/session revocation tests.
- Other feature sets are consumers only: they resolve `tenant_id` + `app_user_id` from context; **no
  changes to their tables, edges or gates**.

---

## 8. Acceptance tests (FS-00 L3 additions)

1. Single-membership user logs in → tenant-scoped token; `/me` returns active tenant; cannot access any
   other tenant's data by id guessing (404) on every endpoint class (list/get/create/update/delete/export/
   search/report/job trigger).
2. Multi-membership user logs in → `TENANT_SELECTION_REQUIRED`; select either tenant → token works only
   for that tenant; `switch-tenant` issues a valid second token; `/me/tenants` lists exactly own
   memberships; no tenant data leaks in choices.
3. Tampered/forged tenant claim (`tid`) → invalid token; valid token for tenant A used on tenant B route →
   404/403 consistently; platform admin without target-tenant context cannot read business data.
4. Invite flow: tenant admin invites email → token single-use → accept creates identity/membership/roles
   atomically; duplicate/expired/revoked invites rejected; cross-tenant invite token use rejected.
5. Membership revocation/suspension: writes fail immediately, refresh fails, access token unusable within
   TTL bound; session replay detection revokes session; `revoke-sessions` works per user/tenant.
6. Tenant lifecycle: platform admin provisions tenant (seed catalogue/flags/sequences + bootstrap invite);
   suspend blocks writes and revokes sessions; reactivate restores; offboard blocks writes, retains
   ledger/audit; tenant admins cannot perform platform-admin actions.
7. Audit: every identity/membership/invitation/session/role change produces an append-only audit event
   with actor, tenant context and diff; platform-admin actions record `SYSTEM_TENANT_ID`.
8. Backward compatibility: migration backfills one identity per existing email and one ACTIVE membership
   per tenant user; N-1 compatibility preserved; single-tenant behavior unchanged.

---

## 9. Out of scope / deferred (envelope)

SSO/OIDC/SCIM implementation and IdP admin UI; tenant self-service signup, billing/plan tiers and
quotas beyond basic config; custom domains; tenant-switch UI; user/role/invitation screens; cross-tenant
analytics or support-impersonation tooling (if added later, requires explicit audited consent flow);
enterprise hierarchy/tenant groups; mobile clients. All are Phase-2+ and do not alter Phase-1 backend scope.

---

## 9b. Resolved implementation clarifications (added during the amendment)

- **Decision-log id:** the Δ-MT decision is recorded as **D-29** in `ARCHITECTURE.md` §8
  (an earlier draft label D-24 collides with the v1.0.0 WMS/counts server-side-task-records decision).
- **API prefix:** the canonical base path is `/api/v1` (PLAN §12, addendum routes use it verbatim).
  Domain-resource specs may keep the shorter `/v1` spelling as non-normative shorthand; the new
  auth/platform/admin/me routes are canonical `/api/v1`.
- **`user_session.identity_id`:** a denormalized **logical reference** without an FK (token/session
  convenience); the single documented cross-scope FK to the global identity table is
  `app_user.identity_id`, so `DM-PLAT-12` remains literally true.
- **Refresh-token uniqueness:** `refresh_token_hash` is globally unique (tenant binding is enforced by
  the session row and token claims), exactly as written in addendum §2.4.

---

## 10. Document update map (binding for the amendment)

| Document | Required changes |
|---|---|
| `design/ARCHITECTURE.md` | §0 counts (277/137/147/INV-30); §5.1 identity/tenant model + auth-token tenant context; §5.2 authn token/session/membership flows and new scopes; §7.3 INV-30 + isolation-matrix test; §8 decision log (D-29 EXT; D-24 was already used); §11 assumptions |
| `design/DATA-MODEL.md` | §2 counts (platform 31, total 277); §3 `user_identity`, `tenant_invitation`, `user_session`, extended `app_user`, 3 new DM-PLAT rules; §20 rule/invariant counts; §21 M00 creates/ownership/seed; §22 ERD G00; §24 checklist totals |
| `feature-sets/FS-00.md` | tables/count, identity/membership/invitation/session use cases, auth/user/platform APIs, events, config, tests (± endpoint/event counts), §11 gate additions, DoD |
| `design/FEATURE-SETS.md` | §1 FS-00 row (31 tables; totals 277/21 views), §7.1 summary scope; no DAG changes |
| `design/feature-sets.json` | `plan_version` 1.1.0 only; edges/contracts/flags/gates unchanged |
| `PLAN.md` | §3.1 decision D9/Δ-MT, new §4.2 tenancy & multi-tenant users, §6.1/§10 wording if needed, §12 assumptions, §13 unchanged; regenerate generated tables |
| `README.md` | table/view counts, multi-tenant user capability line, verified version |
| `verification/VERIFICATION-REPORT.md` | delta re-audit section, refreshed snapshot, updated counts; verdict remains PASS expected |
