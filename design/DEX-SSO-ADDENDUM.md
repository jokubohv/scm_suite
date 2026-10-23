# Dex + Enterprise SSO — Binding Design Addendum (Δ-SSO, plan v1.3.0)

**Status:** Lead-approved binding amendment to the verified v1.2.0 package. Where this addendum conflicts
with earlier text, **this addendum wins**; all affected documents conform (§10).
**Origin:** user requirement — keep the proposed stack, use **Dex** for auth, and add **SSO for companies**
(per-tenant enterprise identity federation).
**Marking:** SaaS identity federation / per-tenant IdP onboarding is an **EXT/ASSUMPTION** beyond the
NetSuite SCM help (which documents account users, not SaaS SSO). No new catalog topics; coverage stays
1,348 (769 in-scope / 579 deferred).

---

## 1. What changes vs v1.2.0

v1.2.0 had **platform-local authentication**: `user_identity.password_hash`, platform-issued JWT
access/refresh tokens, tenant memberships, and a deferred "SSO/OIDC/SCIM" envelope. Δ-SSO replaces the
human authentication path with **Dex as an OIDC identity broker**, adds **per-tenant (company) SSO
connections** to upstream IdPs, and keeps the platform's own authorization/session model unchanged:

- **Dex authenticates** (authorization-code + PKCE; federates to the company's SAML 2.0 / OIDC / LDAP-AD
  IdP via Dex connectors). Dex issues an OIDC ID token.
- **The platform authorizes and issues its own tokens**: after validating the Dex ID token, the API
  resolves/creates the global `user_identity` + tenant membership, then issues the existing platform
  access JWT (`sub/uid/tid/tv/jti` + scopes) + rotating refresh session. **Δ-MT and Δ-RBAC are unchanged**
  — multi-tenant users, memberships, tenant switching, RBAC scopes, SoD and effective permissions all
  continue to apply to SSO-authenticated users.
- **Per-company SSO:** a tenant admin configures one or more `tenant_sso_connection` rows (own IdP,
  email domains, enforced-SSO flag, JIT provisioning, group→role mappings). Login is routed to the
  tenant's connection.
- **Local authentication is retained as fallback** for tenants without SSO and for bootstrap/break-glass
  (platform password, Argon2id). A tenant can enforce SSO (`auth.local_login_enabled=false`): local
  password logins are rejected for its members.
- **Deferred (Phase 2+):** SCIM provisioning, IdP-initiated (unsolicited) SAML, RP-initiated
  single-logout across company IdPs, multiple simultaneous IdPs per single login, just-in-time admin
  elevation, per-field ABAC (unchanged), SSO UI screens.

## 2. Dex deployment model (reference)

**Reference model: one isolated Dex issuer per SSO connection** (tenant-scoped). This is the honest way
to support dynamic per-company connectors: Dex connectors are static in config, so each enterprise
connection gets its own issuer (e.g. `https://sso-<tenant-slug>.auth.example.com`), its own Dex config
generated from the connection, and its own Dex storage. Benefits: per-tenant isolation, independent
upstream trust config/certs, no cross-tenant connector visibility, clean revocation (delete issuer).
Operational cost is acceptable because Dex is small and only runs for tenants with SSO.

- Dex storage: **PostgreSQL** (`dex` database/schema, owned by Dex, not queried by the platform).
- Platform owns lifecycle: `draft → testing → active → disabled/error`; provisioning generates Dex config
  (client = platform API; connector = tenant IdP; issuer; secrets from secret manager), deploys/refreshes
  the issuer, health-checks discovery/JWKS, then marks the connection active.
- Alternative (documented, not reference): a **shared Dex per environment with static connectors** for
  a small number of companies — simpler ops, but connector changes require controlled Dex rollout and
  weaken per-tenant isolation. Phase-1 implementation may start in shared mode for dev/test only.
- Secrets (OIDC client secrets, SAML signing certs, LDAP bind credentials) live in the existing secret
  store; the DB stores only `*_secret_ref` pointers, never secret values.

## 3. Data model (3 new platform tables; no other schema changes)

All tenant-scoped, RLS, composite tenant FKs, audited, `inactive`/status lifecycle. The three tables are
created **inside the existing platform-security migration family (`0002_platform_security`)** — no new
Alembic revision and **no renumbering of domain migrations** (the chain stays `0001_platform_core`,
`0002_platform_security`, `0003_platform_events`, `0004_item_master`, …).

### 3.1 `platform.tenant_sso_connection`
`id`, `tenant_id`, `name` (unique per tenant), `protocol (SAML|OIDC|LDAP_AD)`, `status
(DRAFT/TESTING/ACTIVE/DISABLED/ERROR)`, `dex_issuer_url`, `dex_instance_ref`, `client_id`,
`client_secret_ref`, `connector_config JSONB` (metadata URL/entity id/signing cert refs; OIDC
issuer/client; LDAP host/base DN/bind secret ref/filter — validated per protocol), `email_domains
TEXT[]` (lower-cased; app-enforced uniqueness in ACTIVE connections per tenant), `is_enforced BOOL`
(default false), `jit_provisioning BOOL` (default false), `default_role_ids UUID[]`, `group_claim_name
TEXT` (default `groups`), `last_tested_at`, `last_error`, `created_by`, `updated_by`, `version`,
`inactive_at`. Unique `(tenant_id, name)`; partial indexes on status/issuer; state machine in §3.4.

### 3.2 `platform.user_identity_link`
`id`, `tenant_id`, `identity_id` FK → `user_identity`, `connection_id` FK → `tenant_sso_connection`,
`issuer`, `subject`, `email_at_idp`, `email_verified BOOL`, `linked_at`, `last_login_at`, `status
(ACTIVE/REVOKED)`, `revoked_at`. Unique `(tenant_id, connection_id, issuer, subject)`; unique
`(tenant_id, connection_id, identity_id)`; index identity/email. JIT/link rules in §5.

### 3.3 `platform.sso_group_role_map`
`id`, `tenant_id`, `connection_id` FK, `match_type (EXACT|REGEX)`, `group_value`, `role_id` FK → role,
`priority`, `is_active`, `created_by`, `created_at`. Unique `(tenant_id, connection_id, group_value,
role_id)`. Mapped roles must be **assignable tenant roles/templates**; platform-only scopes can never be
granted by a mapping (validated against `design/permissions.json`).

### 3.4 State machine
`tenant_sso_connection`: DRAFT → TESTING [test passes] → ACTIVE; TESTING/ACTIVE → ERROR [health/config
failure]; ACTIVE → DISABLED [admin]; DISABLED → TESTING [re-enable]; any unposted → deleted only if no
ACTIVE identity links (else DISABLED). `user_identity_link`: ACTIVE → REVOKED (never hard-deleted;
re-link creates a new row or reactivates).

### 3.5 Count changes
**Tables 277 → 280 (platform 31 → 34)**; views 21 unchanged; posting types 31 + 6 commands unchanged;
state-machine specs **41 → 43** covering **50 → 52** aggregates; scoped rules **140 → 143** (+DM-SSO-01/
02/03), total **150 → 153** (143 + 10 global); invariants **INV-01..INV-32** (+INV-32 SSO/authn
integrity); `plan_version` **1.3.0**. New scopes: `platform.sso.manage`, `platform.sso.read` →
catalogue **251 = 237 assignable + 13 service-only + 1 platform-only** (+1 alias not seeded);
public allowlist **6 → 9** (`GET /api/v1/auth/providers`, `POST /api/v1/auth/sso/start`,
`GET /api/v1/auth/sso/callback`); self-service allowlist stays 6; roles 7 / SoD 14 / approval 18 unchanged.

## 4. Authentication flows (platform API remains the token issuer)

1. **Discovery** `GET /api/v1/auth/providers?email_or_tenant=<email|code>` (public): returns enabled
   methods — local (if allowed) and zero/one/more SSO connections (tenant name/code + protocol + login
   URL); no tenant business data beyond public routing info.
2. **Start** `POST /api/v1/auth/sso/start` (public): `{email_or_tenant, ...}` → resolves the SSO
   connection (by tenant code or verified email domain; ambiguities require explicit tenant choice),
   creates an OIDC authorization request (state, nonce, PKCE S256, 5-min TTL in Redis) and returns the
   Dex authorization URL for that connection.
3. **Callback** `GET /api/v1/auth/sso/callback?code&state` (public): validates state/nonce (single-use,
   redirect-URI- and tenant-bound), exchanges the code with Dex (client secret from secret store),
   validates the ID token (issuer, audience, signature via JWKS, exp/iat/nonce, `email_verified`),
   enforces email-domain policy, resolves/creates identity + link (+ JIT membership if enabled), maps
   groups → roles (validated), applies enforced-SSO policy, audits, then issues the **platform** access
   JWT + rotating refresh session (or `TENANT_SELECTION_REQUIRED` if the identity has multiple active
   memberships and none was specified). Dex tokens are not accepted as platform bearer tokens.
4. **Errors** are typed and audited: `SSO_PROVIDER_NOT_FOUND` (404), `SSO_DISABLED`/`SSO_ENFORCED_LOCAL_DENIED`
   (403), `SSO_STATE_INVALID` (400, replay-safe), `SSO_IDP_UNAVAILABLE` (503 + Retry-After, no fallback
   unless tenant allows), `SSO_EMAIL_DOMAIN_NOT_ALLOWED` (403), `JIT_PROVISIONING_DISABLED` (403 with
   invitation path), `IDENTITY_LINK_CONFLICT` (409), `SSO_GROUP_MAPPING_INVALID` (422).
5. **Logout/revocation**: platform logout/revoke semantics unchanged (Δ-MT sessions). Removing a link or
   disabling a connection revokes the corresponding platform sessions; Dex upstream session logout is
   optional/deferred (documented limitation: company IdP SSO cookie may persist until Dex session ends).
6. **Local fallback**: unchanged password flow (`Argon2id`) where enabled; if a tenant has an ACTIVE
   enforced connection, local logins for its members are rejected; platform-admin bootstrap/break-glass
   remains local, audited, and can be disabled after platform-operator SSO is configured.

## 5. Identity resolution, JIT and group mapping

- Link key is `(tenant, connection, issuer, subject)`. On callback: known ACTIVE link → use its
  `identity_id`; otherwise, if `email_verified` and policy `link_by_verified_email=true` (default) and a
  global identity with that email exists **and** belongs to the tenant (invited/active), link it;
  otherwise create a new identity when `jit_provisioning=true` and the email domain is allowed.
- JIT creates exactly one `user_identity` (verified email) + one ACTIVE `app_user` membership for that
  tenant + `user_role` rows from `default_role_ids` (validated as assignable tenant roles) +
  `identity_link`; it does **not** create other tenant memberships.
- Group mapping: for each matching mapping (priority order), assign the mapped role(s); the union is
  bounded by assignable tenant roles; **no platform-only/service-only scope can be granted via SSO**;
  conflicts with SoD pairs are rejected (`409 SOD_CONFLICT`); mapping changes and role diffs are audited.
- Link removal/revocation, tenant suspension and connection disable immediately block new SSO logins;
  existing platform sessions are revoked per Δ-MT revocation semantics.
- One identity may link to multiple tenant connections (multi-tenant users, Δ-MT); links never cross
  tenants and are invisible to other tenants.

## 6. APIs added to FS-00 (Δ-SSO surface; 17 endpoints)

- Public: `GET /api/v1/auth/providers`, `POST /api/v1/auth/sso/start`,
  `GET /api/v1/auth/sso/callback`.
- Tenant admin (`platform.sso.manage`): `GET|POST /api/v1/admin/sso-connections`;
  `GET|PATCH|DELETE /api/v1/admin/sso-connections/{id}`;
  `POST /api/v1/admin/sso-connections/{id}/test`;
  `POST /api/v1/admin/sso-connections/{id}/rotate-secret`;
  `GET|PUT /api/v1/admin/sso-connections/{id}/group-mappings`;
  `GET /api/v1/admin/sso-connections/{id}/links` (identity links, paginated, no secrets);
  `DELETE /api/v1/admin/sso-connections/{id}/links/{link_id}` (revoke + session revoke);
  `GET|POST /api/v1/admin/users/{id}/identity-links` and
  `DELETE /api/v1/admin/users/{id}/identity-links/{link_id}`.
- Read scope `platform.sso.read` for GETs; all writes `platform.sso.manage`; connection tests/secret
  rotation audited; no endpoint returns client secrets/cert private material.

## 7. Dex operations and deployment (reference stack extension; nothing else changes)

- New compose/deployment units: **`dex` (one instance per active SSO connection in production)**,
  **Dex storage database** (`dex` schema/database), and the existing `api`/`worker`/`outbox-relay`/
  `postgres`/`redis`. Dev/test may run one shared Dex with test connectors.
- Dex pinned to a tested 2.4x release; OIDC client = platform API; connectors = tenant IdP
  (SAML 2.0/OIDC/LDAP-AD); issuer per connection; TLS everywhere; JWKS cached with rotation.
- Provisioning/refresh is an admin-triggered workflow (`POST /test` → deploy/refresh → discovery +
  JWKS health check → ACTIVE). `scripts/provision_sso_dex.sh` (implementation deliverable) renders Dex
  config from `tenant_sso_connection` + secret refs and applies it; failures set `ERROR` + audit.
- Local password auth, Argon2id, platform JWT/refresh sessions and API keys are unchanged; Dex only
  replaces the *human authentication source*.

## 8. Rules, invariants, and verification

| ID | Rule |
|---|---|
| `DM-SSO-01` | `tenant_sso_connection` is tenant-scoped and RLS-protected; only tenant admins with `platform.sso.manage` (or platform admin) manage it; secrets are secret-store refs only; issuer/config changes are audited and re-tested before ACTIVE. |
| `DM-SSO-02` | Identity links are unique per `(tenant, connection, issuer, subject)` and per `(tenant, connection, identity)`; link/JIT only with verified email and allowed domain; no cross-tenant link reuse; link revocation blocks new logins and revokes sessions. |
| `DM-SSO-03` | Dex authenticates, the platform authorizes: platform tokens are issued only after an ACTIVE membership; group→role mappings may grant only assignable tenant roles (never platform-only/service-only scopes) and cannot violate SoD; enforced-SSO tenants reject local-password logins. |
| `INV-32` | **SSO/authn integrity:** every SSO login resolves to exactly one verified identity link and one active membership, with no cross-tenant leakage; state/nonce/PKCE validated and single-use; group/role mapping bounded by RBAC; connection/link lifecycle is audited; Dex unavailability never silently downgrades to local auth. |

**Counts:** tables 280 (platform 34); views 21; posting 31 + 6; scoped rules 143 (total 153);
invariants INV-01..INV-32; scopes 251 = 237 assignable + 13 service-only + 1 platform-only (+1 alias,
not seeded); 7 roles; 14 SoD pairs; public allowlist 9 / self-service 6; FS-00: 34 tables, 102 endpoints,
33 events, 6 jobs, 3 migrations (`0001`–`0003`; SSO tables in `0002`), exit gate 17 commands. `plan_version` **1.3.0**.

## 9. Test plan (FS-00 exit-gate additions)

- OIDC flow with a test Dex instance (authorization code + PKCE, state/nonce single-use, ID-token
  validation incl. wrong issuer/audience/nonce/expired, replay, callback tenant binding).
- Per-tenant/company SSO isolation: connections, links, sessions and group mappings never leak across
  tenants; a user can SSO into two tenants through their respective IdPs and switch memberships.
- JIT provisioning on/off, verified-email linking, domain allowlists, enforced-SSO local rejection,
  break-glass platform admin path.
- Group→role mapping (exact/regex, priority, role bounds, SoD rejection, platform-only rejection) and
  mapping change propagation on next login/session.
- Connector config generation/validation for SAML 2.0 metadata, OIDC discovery, LDAP/AD bind (unit +
  fixture-based; no live enterprise IdP required in CI).
- Connection lifecycle: draft/test/active/error/disabled, secret rotation, IdP outage 503 + no silent
  fallback, cert/metadata expiry monitoring job.
- Security: secrets never returned/logged, state/PKCE entropy, redirect-URI allowlist, rate limits,
  audit coverage, session revocation on link removal.
- Regression: local login, Δ-MT multi-tenant users, Δ-RBAC role/permission/SoD gates all unchanged.

## 10. Document update map (binding)

| Document | Required changes |
|---|---|
| `design/DEX-SSO-ADDENDUM.md` | **This file (authoritative)** |
| `design/ARCHITECTURE.md` | §1.2 runtime topology ✚ Dex units/storage; §1.6 IdP boundary rewritten (Dex broker, per-tenant SSO, local fallback); §5.2 authn flows/endpoints/claims/linking/JIT/group mapping/fallback + scopes `platform.sso.manage/read`; §7.3 INV-32; §8 D-31; §0 counts (280/143/153/INV-32/251); §10/§11 |
| `design/DATA-MODEL.md` | §3 three new tables + DM-SSO-01/02/03 + INV-32 note; §2 counts (platform 34 / total 280); §19 two state machines; §20 counts (143/153); §21 unchanged migration chain (SSO tables in `0002_platform_security`) + seeds (`platform.sso.*` scopes, Dex client refs, no secrets); §22/§23 EXT; §24 totals |
| `feature-sets/FS-00.md` | tables 34; auth/SSO use cases; public + tenant-admin endpoints (§6); events; config (Dex issuer/client refs, secret refs, state TTL, redirect allowlist, domain policy, JIT defaults); §10 tests (SSO flow/isolation/group mapping/connector config); §11 exit gate +3 SSO tests; counts 102 endpoints/33 events/3 migrations; DM-SSO rules/INV-32; §14 amendment record |
| `design/permissions.json` | +2 scopes (`platform.sso.manage`, `platform.sso.read`, assignable); public allowlist +3 routes (9 total); counts → 251/237/13/1/7/14/18/6/9; admin template includes both new scopes |
| `design/FEATURE-SETS.md` | §1 FS-00 tables 34; totals 280/21; §7.1 Δ-SSO paragraph (Dex broker, per-company SSO, local fallback, scopes, DS-SSO rules, INV-32, exit-gate tests) |
| `design/feature-sets.json` | `plan_version` 1.3.0; FS-00 exit_checks +3 SSO test commands (before `record`), everything else unchanged |
| `PLAN.md` | §3.1 Δ-SSO row (D-31); new §4.4 "Authentication: Dex + enterprise SSO"; §4 reference architecture auth bullet; §12 assumptions (SSO now Phase-1; SCIM/deferred list); counts (280/143/153/INV-32/251); header stamp after verification |
| `README.md` | stack/auth line (Dex OIDC broker, per-company SSO), counts (280 tables/platform 34/251 scopes/143 rules), status |
| `verification/VERIFICATION-REPORT.md` | Δ-SSO delta audit, refreshed snapshot, final verdict |
| `design/FEATURE-SET-FRAMEWORK.md` | §3 Auth row: Dex OIDC broker + platform-issued JWT/refresh + API keys; Deploy row: + Dex units/storage; note local fallback |

## 11. Resolved clarifications

- **Dex is authentication, not authorization.** Platform JWT/session, Δ-MT memberships/tenant switching
  and Δ-RBAC scopes/roles/SoD remain authoritative; Dex groups map to platform roles only through
  validated `sso_group_role_map` rows.
- **Per-company SSO** = per-tenant `tenant_sso_connection`; reference deployment is one isolated Dex
  issuer per connection; shared-Dex is an allowed dev/test or small-fleet alternative.
- **Local fallback retained** (password) for non-SSO tenants/bootstrap; enforced-SSO tenants reject local
  login for their members; platform-admin break-glass is local and audited until operator SSO is set.
- **SCIM, IdP-initiated SAML, company-wide single logout, multiple IdPs per login** are deferred; email
  domains + JIT + group mapping cover the Phase-1 enterprise-SSO need.
- No NetSuite catalog topics are added; this is an EXT platform capability. Feature-set DAG, contracts
  (except FS-00 exit gate additions/plan_version), tables outside platform and all other 15 specs are
  unchanged.
