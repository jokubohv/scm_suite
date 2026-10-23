# Dex in the local stack (`ops/dex/`)

Dex is the OIDC identity broker for human authentication (Δ-SSO addendum). **Dex authenticates;
the platform authorizes and issues its own JWT/refresh tokens** — Dex tokens are never platform
bearer tokens (`RULE-SSO-01`). This directory owns only the **shared dev/test issuer** for the
local compose stack; production per-company issuers are rendered by
`scripts/provision_sso_dex.sh` from `platform.tenant_sso_connection` + secret-manager references.

Owned by: S0-02 (local stack + Dex dev). Referenced by: `docker-compose.yml` (service `dex`),
`app/core/config.py` (`dex_issuer`, `dex_client_id`, `dex_client_secret`), FS-00 SSO implementation.

---

## 1. Dev vs production deployment model

| | Local dev / test (this directory) | Production |
|---|---|---|
| Issuer | **one shared dev issuer**, `http://localhost:5556/dex` (Lead-pinned issuer contract) | **one isolated Dex issuer per ACTIVE `tenant_sso_connection`** (DEX-SSO-ADDENDUM §2), e.g. `https://sso-<tenant-slug>.auth.example.com` |
| Connectors | static password connector (`local`); add mock/test connectors locally | per-tenant upstream IdP: SAML 2.0 / OIDC / LDAP-AD, rendered by `scripts/provision_sso_dex.sh` from `tenant_sso_connection.connector_config` |
| Storage | PostgreSQL database `dex` on the compose `postgres` (created by `initdb/01-create-dex-db.sql`); Dex owns/migrates it | dedicated Dex storage per issuer (PostgreSQL); platform never queries it |
| Client secret | `secretEnv: DEX_CLIENT_SECRET` read from the container environment (dev placeholder in `.env.example`) | secret-manager reference (`client_secret_ref`) resolved at deploy time; never in DB/logs |
| Lifecycle | static config mounted read-only; `docker compose restart dex` to reload | platform-owned: `draft → testing → active → disabled/error`, health-checked discovery/JWKS, clean revocation by deleting the issuer |
| Isolation | shared — acceptable for dev/test only | per-tenant: separate issuer/config/storage/certs; no cross-tenant connector visibility |

**Why per-tenant issuers in production:** Dex connectors are static config. Giving each company SSO
its own issuer allows company-specific SAML metadata/signing certs/OIDC or LDAP settings, independent
rotation and clean revocation, with per-tenant isolation. The operational cost is acceptable because
Dex is small and runs only for tenants with SSO configured.

**Documented alternative (not the reference):** a shared Dex per environment with static connectors
is simpler for a handful of companies but weakens tenant isolation and requires a controlled Dex
rollout for every connector change. Dev/test uses this shared mode deliberately (DEX addendum §2/§7).

---

## 2. Files

| File | Purpose |
|---|---|
| `config.docker.yaml` | **gomplate template** consumed by the Dex v2.41 image entrypoint (the image renders it with the container env, then runs `dex serve`). Dev issuer `http://localhost:5556/dex`, static client (`DEX_CLIENT_ID`), static password connector, PostgreSQL storage. No secrets in the file. |
| `initdb/01-create-dex-db.sql` | Creates the `dex` database on first Postgres initialisation (Dex migrates its own tables at startup). |
| `README.md` | This document. |

The compose stack mounts this directory read-only at `/etc/dex` (see `docker-compose.yml`), so edits
here take effect on the next `docker compose up -d`/`restart dex` — no image rebuild is needed.

### Dev identity (local only)

| Field | Value |
|---|---|
| Email | `dev@example.com` |
| Username | `dev` |
| Password | `dev-password` |
| Dex subject (`userID`) | `dev-user-0001` |
| OIDC client id | `inventory-platform` (matches `app/core/config.py` default) |
| Redirect URI | `http://localhost:8000/api/v1/auth/sso/callback` |

The password is hashed (bcrypt, cost 10) in `config.docker.yaml`. The hash is one-way and dev-only;
replace it with your own local hash if desired (see the comment in the template). Production
identities come from company IdPs via `user_identity_link`; this dev user exists only for the
shared dev issuer.

---

## 3. Running and verifying

```bash
# From inventory-platform/ — start the full local stack (api/worker/outbox-relay/postgres/redis/
# minio/shared Dex); `--wait` blocks until healthchecks pass.
scripts/up.sh --profile dev --wait

# Service health / logs
docker compose ps
docker compose logs -f dex

# Dex discovery (issuer must match DEX_ISSUER exactly)
curl -fsS http://localhost:5556/dex/.well-known/openid-configuration | head

# Non-browser smoke test of the dev static-password connector (password grant):
curl -fsS -X POST http://localhost:5556/dex/token \
  -d grant_type=password \
  -d username=dev@example.com \
  -d password=dev-password \
  -d client_id=inventory-platform \
  -d client_secret="$DEX_CLIENT_SECRET" \
  -d scope='openid email profile'

# Stop the stack (add --volumes to destroy local DB/Redis/MinIO data — dev only)
scripts/down.sh
```

Platform-side SSO endpoints (implemented by FS-00, not by Dex): `GET /api/v1/auth/providers`,
`GET /api/v1/auth/sso/start`, `GET /api/v1/auth/sso/callback`. A tenant's enforced-SSO policy rejects
local password logins for its members; Dex unavailability returns `503 SSO_IDP_UNAVAILABLE` and must
never silently fall back to local auth (`INV-32`).

---

## 4. Security rules

- **No secrets committed:** the client secret is injected as `DEX_CLIENT_SECRET` (dev placeholder in
  `.env.example`, intended for localhost dev only; production toggles `secretEnv` to a secret-manager
  value). Only `*_secret_ref` pointers may be stored in the platform DB (`DM-SSO-01`).
- `config.docker.yaml` is dev-only and is never used to render production issuers.
- `.env` is gitignored; commit only `.env.example` (placeholders). Never bake secrets into images.
- TLS is required for production issuers; local dev uses plain HTTP on localhost only.
- `/dex` is a path-prefixed issuer (tokens carry `iss=http://localhost:5556/dex`); tests assert exact
  issuer/audience/nonce matching, so do not change `DEX_ISSUER` without updating the platform env.

---

## 5. Troubleshooting

| Symptom | Check |
|---|---|
| `dex` unhealthy | `docker compose logs dex`: gomplate render errors mean a broken template/env var; PostgreSQL errors mean the `dex` database is missing (reset with `scripts/down.sh --volumes && scripts/up.sh --profile dev`). |
| discovery 404 / issuer mismatch | `DEX_ISSUER` must be `http://localhost:5556/dex`; login uses the path-prefixed endpoints (`/dex/auth`, `/dex/token`). |
| password grant fails | Password grant is enabled for dev (`oauth2.passwordConnector: local` + `enablePasswordDB: true`); verify the dev hash corresponds to `dev-password` and use client id `inventory-platform`. |
| platform SSO 401/403 | The platform validates Dex `iss`/`aud`/`nonce`/PKCE and email-domain/JIT policy; see FS-00 SSO rules (`DM-SSO-02/03`, `INV-32`). |
| port 5556 busy | Another compose project is running; use a distinct `COMPOSE_PROJECT_NAME` or override `DEX_*`/ports for that namespace (Δ-PAR §7 isolation). |

---

## 6. Deferred (Phase 2+)

SCIM provisioning, IdP-initiated (unsolicited) SAML, RP-initiated single logout across company IdPs,
multiple simultaneous IdPs for one login, per-field ABAC and SSO management UI are deferred
(DEX-SSO-ADDENDUM §1). This directory intentionally contains no such configuration.
