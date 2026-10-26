# Headless deployment readiness

**Verdict:** the API is deployable **as a headless service** for dev/demo/customer-sandbox use today.
It is **not turnkey production-ready** without the platform-ops items in §4; the application image,
migrations, API surface, auth and OpenAPI artifact are ready, but this repository does not contain a
production orchestrator, secret integration, DB role bootstrap, metrics stack or scheduler daemon.

## 1. What is ready

| Area | State |
|---|---|
| Headless surface | REST-only FastAPI app (no UI); `/api/v1/docs` is disabled and `/api/v1/openapi.json` requires `X-API-Key: $OPENAPI_API_KEY` in production; workers/outbox-relay are separate entrypoints. |
| Image/runtime | Multi-stage `Dockerfile`, non-root runtime user `10001:10001`, one image for API/worker/relay; `docker-compose.yml` + `docker-compose.dev.yml` for the dev stack. |
| Database | 30 Alembic migrations, single linear chain, head `0030_rls_grants_verify`; RLS enabled (280 tables/569 policies in dev); `scripts/up.sh`/`deploy_set.sh` run migrations. |
| Tests | 1302 passed, 1 skipped across the full suite; contract lint 0 errors/0 warnings; deployment state 16/16 feature sets verified. |
| Auth | JWT access/refresh, RBAC 255 scopes, tenant isolation/RLS, Dex OIDC SSO + local fallback; deny-by-default route guards. |
| Health | `/healthz` liveness; `/readyz` now checks DB (SELECT 1 + migration head) and Redis, returning **503** when either dependency is unavailable; `/internal/deps` is a catalogued scoped operation. |
| Contracts | Runtime OpenAPI 3.1 **981 paths / 1257 operations**, unique operationIds, no TODO operations/unresolved component refs; committed artifact `deployment/runtime.openapi.json` with freshness test. Contract-first aggregate additionally carries 11 deliberate skeleton operations (see §5). |
| Events | 159 canonical event schemas, all implementation-backed (`x-todo` = 0); outbox relay entrypoint; Redis streams. |
| Feature flags | All deploy-dark by default; enable per tenant after deployment. Tenant provisioning seeds 201 rows (38 sequences, 136 settings, 26 flags, default count approval) and `app.seed.backfill` exists for existing tenants. |

## 2. OpenAPI answer

- **Use the runtime spec for clients:** `GET /api/v1/openapi.json` with `X-API-Key` in production
  (also committed as `deployment/runtime.openapi.json`, regenerated/checked by
  `python tools/export_runtime_openapi.py`). It is OpenAPI 3.1, contains only mounted operations, has
  unique operationIds and no TODO entries. Generate SDKs from this file.
- **Do not use the contract aggregate as a strict client spec without filtering:**
  `contracts/openapi/openapi.json|yaml` is the contract-first governance surface (spec declarations +
  implementation overlays). It includes **11** frozen-spec/infrastructure skeletons (10 malformed
  path-stitching operations + hidden `/openapi.json` alias) and 99 spec response TODOs that are not
  mounted. They are intentionally excluded from the runtime artifact.
- Regenerate/verify: `make contracts` (contract lint/generation) and
  `python tools/export_runtime_openapi.py --check`; the test
  `app/tests/contracts/test_runtime_openapi_spec.py` enforces freshness.
- Docs/OpenAPI are currently public; restrict them at the gateway in production if the service is not
  intended for external developers.

See `deployment/VPS-DEPLOYMENT.md` for the VPS Docker Compose + GitHub Actions deployment and the operator-only account bootstrap flow.

### Account creation policy

- There is **no public sign-up**. The public allowlist contains only login/SSO start/providers and
  invitation acceptance; an invitation cannot be created without an authorised admin path.
- Operator-only endpoints `POST /api/v1/bootstrap/tenants` and `POST /api/v1/bootstrap/users` require
  `X-Super-Admin-Token: <SUPER_ADMIN_TOKEN>` (or Bearer). Missing/invalid token → 401; unset/short
  configured token → 503 (`BOOTSTRAP_DISABLED`).
- Set `ACCOUNT_CREATION_SUPER_ADMIN_ONLY=true` in production: tenant-admin user/invitation creation and
  tenant provisioning then also require the operator token. Use the bootstrap endpoints for first-account
  and additional-account creation, then log in normally.
- Set `SSO_JIT_PROVISIONING_ENABLED=false` in production so SSO login cannot auto-create accounts.

## 3. Minimal headless run (dev/sandbox)

```bash
cp .env.example .env                    # dev placeholders only; replace secrets
COMPOSE_PROJECT_NAME=inv-dev scripts/up.sh --profile dev --wait
docker compose run --rm api alembic upgrade head
curl -fsS localhost:8000/healthz
curl -fsS localhost:8000/readyz
curl -fsS -H "X-API-Key: $OPENAPI_API_KEY" localhost:8000/api/v1/openapi.json > runtime.openapi.json
python -m app.seed.cli --profile sqlalchemy --tenant system   # optional reference data
```

Production services to run: `api` (behind a TLS gateway), `worker`, `outbox-relay`, PostgreSQL 16,
Redis 7, S3-compatible object storage, and the tenant IdP/Dex issuer.

## 4. Required before production

1. **Orchestration:** provide Kubernetes/Helm or a production Compose/ECS deployment with TLS ingress;
   only the dev compose stack is in-repo (`scripts/deploy_set.sh` deliberately does not touch
   staging/prod).
2. **Secrets:** inject DB/Redis/S3/JWT/Dex secrets from a secret manager; never use `.env.example`
   values. Rotate the JWT signing key and set the real issuer/audience.
3. **Database roles/RLS:** create login roles and `platform_app`, `platform_auth`, `finance_adapter`
   before migrations; connect the app as a scoped login granted the app roles (not a superuser).
   Migrations grant conditionally only when these roles exist. Use a pooling proxy or per-request
   `SET app.tenant_id` as designed for RLS.
4. **Migration job:** run `alembic upgrade head` as a pre-deploy job (do not auto-migrate in every API
   replica); verify `/readyz` reports the expected head.
5. **Scheduler:** run an external scheduler/leader that enqueues the seeded cron jobs; the worker
   registry handles 46 implemented jobs, but 11 no-ops remain external/payload-driven. Without a
   dispatcher, scheduled jobs will not run.
6. **Observability:** add metrics/tracing/access-log shipping and alerting for `/readyz`, outbox depth,
   worker failures and DB/Redis saturation. Structured logs/correlation IDs exist; there is no
   Prometheus `/metrics` endpoint.
7. **Availability/scale:** run multiple API replicas behind the gateway; workers can scale by queue;
   advisory locks provide singleton semantics. Configure DB/Redis HA and backups.
8. **IdP/SSO:** replace the dev Dex issuer with the production IdP per tenant and set redirect URIs;
   local login can serve as break-glass.
9. **Feature enablement:** after deploy, enable the deploy-dark flags per tenant and run
   `app.seed.backfill` for pre-existing tenants.
10. **Dependencies:** pin/scan images (Postgres, Redis, MinIO, Dex) and set resource limits.

## 5. Known limitations that affect production planning

- 11 contract-first skeleton operations in the frozen spec are not mounted; runtime clients are
  unaffected (use the runtime spec).
- 3 owner-blocked FS-00 events have no producer (`document.closed`, `document.reversed`,
  `finance.journal_intent.recorded`; the latter's writers live in FS-08/FS-13).
- FS-04 transaction/line tables are not partitioned; review volume expectations and add a plan-approved
  partitioning migration before very large ledgers.
- Some FS-04 approval/flag/data-scope enforcement is simplified, and 11 job no-ops are
  external/payload/migration-driven.
- Dev DB lacks runtime grant roles, so grant-level probes are skipped locally.
- Contract aggregate (not runtime) has 11 skeletons/99 spec TODO responses; do not publish it as the
  SDK contract.
