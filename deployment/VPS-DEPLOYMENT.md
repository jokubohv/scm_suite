# VPS deployment (Docker Compose + GitHub Actions, no Kubernetes)

**Target:** a single Linux VPS (e.g. Hostinger) running Docker Engine + Compose v2, with a domain
pointing at it. The workflow builds the image, pushes it to GHCR, then SSHes to the VPS to run DB role
bootstrap, migrations and the compose stack. TLS is handled by Caddy.

## Files

| File | Purpose |
|---|---|
| `docker-compose.prod.yml` | api / worker / outbox-relay / postgres / redis / caddy / migrate |
| `.env.production.example` | the only env file the operator maintains on the VPS |
| `deployment/Caddyfile` | HTTPS reverse proxy for `{$DOMAIN}` |
| `deployment/provision-db-roles.sh` | creates the non-superuser app login + RLS roles before migrations |
| `deployment/bootstrap-first-account.sh` | creates the first tenant + tenant administrator via the operator token |
| `.github/workflows/deploy-vps.yml` | verify → build/push to GHCR → SSH deploy + `/readyz` gate |

## GitHub repository secrets

| Secret | Purpose |
|---|---|
| `VPS_HOST` | DNS name/IP of the VPS |
| `VPS_USER` | SSH user (must be in the `docker` group) |
| `VPS_SSH_KEY` | private SSH key for that user |
| `VPS_DEPLOY_DIR` | absolute path on the VPS, e.g. `/opt/scm-suite` |
| `GHCR_TOKEN` (optional) | PAT with `read:packages` if the GHCR package is private; omit for public packages |

The workflow uses `GITHUB_TOKEN` to push the image. It never writes `.env.production`; create it on the
VPS once from `.env.production.example` and fill every secret.

## One-time VPS preparation

```bash
sudo apt-get update && sudo apt-get install -y ca-certificates curl
# install Docker Engine + compose plugin per Docker's docs, then:
sudo usermod -aG docker "$USER"

sudo mkdir -p /opt/scm-suite && sudo chown "$USER":"$USER" /opt/scm-suite
cd /opt/scm-suite
# copy .env.production.example -> .env.production and replace every CHANGE_ME value
# point DNS A/AAAA for DOMAIN at the VPS; allow inbound 80/443 (SSH restricted to your IP)
```

## Deploy

```bash
# from a local checkout, after merging and creating a release tag:
git tag v0.1.0 && git push origin v0.1.0
# or run the "deploy-vps" workflow manually
```

The workflow runs static/tests gates, builds/pushes `ghcr.io/<owner>/scm-suite-api:<sha>`, then on the VPS:
`pull → provision DB roles → alembic upgrade head → start api/worker/outbox-relay/caddy → GET /readyz`.
`/readyz` must return 200 with `database=ok` and `redis=ok`; the deploy fails otherwise.

## Create the first account (operator only)

There is **no public sign-up**. Account creation is operator-token-gated:

- `POST /api/v1/bootstrap/tenants` — creates a tenant, seeds its RBAC catalogue/roles, creates the first
  tenant administrator with a password, returns the tenant/admin ids.
- `POST /api/v1/bootstrap/users` — creates additional accounts in an existing tenant with selected roles.
- Regular `POST /api/v1/admin/users`, `POST /api/v1/admin/invitations` and tenant provisioning are also
  super-admin-token-gated when `ACCOUNT_CREATION_SUPER_ADMIN_ONLY=true` (the production default).
- The only public account lifecycle endpoint is invitation acceptance
  (`POST /api/v1/admin/invitations/{token}/accept`), which requires a valid single-use invitation.
- SSO JIT provisioning is disabled by `SSO_JIT_PROVISIONING_ENABLED=false` in production.

Run the first bootstrap from your workstation (or the VPS):

```bash
API_BASE=https://inventory.example.com \
TENANT_CODE=MAIN TENANT_NAME="Main Company" \
ADMIN_EMAIL=you@example.com ADMIN_PASSWORD='a-strong-password' \
SUPER_ADMIN_TOKEN='the value from .env.production' \
./deployment/bootstrap-first-account.sh
```

Then log in with `POST /api/v1/auth/login` using the tenant code/email/password. Add more accounts with
`/api/v1/bootstrap/users` (same token). Keep `SUPER_ADMIN_TOKEN` only in the VPS env/secret manager and
rotate it by updating `.env.production` and restarting the API.

## Operations

- **Updates:** push a new tag → workflow redeploys. Migrations run in the one-shot `migrate` service.
- **Logs:** `docker compose --env-file .env.production -f docker-compose.prod.yml logs -f api worker`.
- **Health:** `curl -fsS https://$DOMAIN/readyz` (Caddy) or `docker compose ... exec api python -c ...`.
- **Backups:** back up the `postgres_data` volume (and any external S3 bucket); Redis is not authoritative.
- **Rollback:** deploy a previous image tag with the same workflow/manual compose; Alembic downgrades are
  not automatic (migrations are additive; restore a backup for destructive changes).
- **Docs/spec:** `/api/v1/docs` is disabled; `/api/v1/openapi.json` requires `X-API-Key: $OPENAPI_API_KEY` (set in `.env.production`; deploy fails if missing/weak).
- **Integrations:** business endpoints accept tenant-scoped API keys created via `POST /api/v1/admin/api-keys`; send `X-API-Key: <tenant-key>`. The same tenant key can fetch the global `/api/v1/openapi.json`; `OPENAPI_API_KEY` remains an operator-wide alternative.
  if the API is private.

## Hardening checklist

- Fill every `.env.production` secret; never commit it.
- Keep `ACCOUNT_CREATION_SUPER_ADMIN_ONLY=true` and `SSO_JIT_PROVISIONING_ENABLED=false` unless you have
  an explicit exception.
- Restrict SSH to your IP; use a non-root deploy user in the `docker` group.
- Use a managed Postgres/Redis or volume backups; monitor `/readyz`, disk and container logs.
- Configure external S3 for attachments; set `S3_ENDPOINT` accordingly (the `storage` MinIO profile was
  removed from prod compose to avoid an unpinned image).
