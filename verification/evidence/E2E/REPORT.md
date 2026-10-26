# Local live E2E report — 2 companies, multiple warehouses

**Run:** `E2E10260104763C` (latest) · **API:** `http://127.0.0.1:28080` · **DB:** local `inventory_e2e` with non-superuser app login + RLS.

## Result

- **170 checks, 0 failures**
- 2 tenants created and operated independently
- 3 warehouses (Company A: WH-A1, WH-A2; Company B: WH-B1), bins and warehouse records
- Cross-tenant reads: 404 for item/location/warehouse
- GET surface sweep: **0 5xx** across every mounted GET operation without path parameters
- Contract/readiness: `/readyz` 200; runtime OpenAPI loaded

## Coverage executed

P0 account/auth: bootstrap 2 tenants + admins, login, operator token, flags.
P1 master data: 3 locations, 9 bins, 3 warehouses, 3 items, statuses, UOM taxonomy.
P2 ledger: opening adjustment (post), transfer A1→A2, balances verified (6/4).
P3 counts: manual count, start, all lines captured, submit, second-admin approval.
P4 procurement: vendor, item-vendor, PO → approve, receipt → post, bill → 3-way match → submit → post.
P5 quality: spec → activate, inspection plan, inspection start/results/complete.
P6 WMS: inbound ASN → arrive → receiving order → complete → putaway; warehouse inventory; wave → release → pick assign/start/complete.
P7 manufacturing: assembly item → assembly extension → BOM → revision → component → work order → release.
P8 reporting: catalogue + 12 report reads (stock ledger, valuation, snapshot, negative, lot expiry, workbenches, etc.).
P9 isolation/sweep: cross-tenant 404s and GET 5xx sweep.

## Full-surface smoke sweep (all mounted operations)

`e2e/run_surface_sweep.py` invoked **1,257 / 1,257** runtime OpenAPI operations against the local E2E API using an authenticated tenant admin, placeholder path IDs and empty/minimal bodies. A response is accepted if it is 2xx/4xx; any 5xx or transport error fails the run.

Latest result: **1,257 operations, 0 5xx**. Response distribution: 263 × 200, 498 × 422, 480 × 404, 8 × 403, 2 × 400, 6 × 204. This is crash-safety coverage, not full business validation for every write path.

Additional defects found by that sweep and fixed:

6. **Empty create payloads returned 500** — 50 operations reached service code with `{}` and raised `KeyError`/NOT NULL errors. Added an authenticated request-body guard: create-style POST/PUT/PATCH with `{}` now returns 422; documented action endpoints (`submit`, `post`, `approve`, `arrive`, etc.) still accept empty bodies.
7. **Missing DELETE grants** — `platform_app` had no DELETE privilege on `item_master.item_related` / `item_master.kit_group_member`, producing 500s for valid delete routes. Grants added to migration 0030 and deployment backfill SQL.

## Defects found locally and fixed

1. **Global currency hidden by RLS** — app role saw 0 of 10 `platform.currency` rows (tenant_id NULL), so vendor creation failed. Added `global_currency_read` policy; provisioned backfill for existing DBs.
2. **Traceability domain flag mismatch** — service checked `traceability.registry.enabled`, tenant provisioning seeded `inventory.storage_registry.enabled`, so locations/statuses were `DOMAIN_DISABLED` after enabling flags. Canonical flag added and legacy key accepted.
3. **Procurement idempotency 500s** — nine create methods called `_idem_complete` without `_idem_begin`, causing 500 when `Idempotency-Key` was supplied (vendor category, item-vendor, item-vendor price, contract, request, RFQ, credit, return, special-order link). Begins added.
4. **Bootstrap RLS empty tenant context** — tenant lookup inserted before `app.tenant_id` was set failed `invalid input syntax for type uuid: ""`. Bootstrap now sets a SYSTEM context; tenant creation sets the new tenant id.
5. **Failed login with tenant code 500** — `auth.login.failed` event used the tenant code as a UUID tenant_id; now uses SYSTEM UUID + `tenant_selector` payload. Wrong password returns 401 as expected.

## Reproduce

```bash
e2e/setup_local_e2e.sh
E2E_BASE=http://127.0.0.1:28080 E2E_SUPER_TOKEN="$(cat /tmp/e2e-super-token)" \
  python3 e2e/run_live_e2e.py
```

Evidence: `verification/evidence/E2E/latest.md` / `latest.json`.

## Not covered

- Exhaustive write matrix for every POST/PATCH route (pricing, consigned, extensions, planning lifecycle beyond reads)
- Load/performance and browser/UI (headless service only)
- Production Cloudflare edge E2E (this run is local by design)


## Production read-only smoke

After a successful production deployment (run `40254816539`), a read-only GET sweep ran against `https://scm.k1hub.com` using the MAIN tenant admin:

- `/readyz` 200; runtime OpenAPI loaded
- 229 no-path-parameter GET operations: 169×200, 27×409, 14×422, 16×503 controlled feature-disabled responses, 3×403; **0 unexpected 5xx**
- canonical `traceability.registry.enabled` flag present for both tenants
- global currency reference now visible: 10 currencies
- item-master DELETE grants verified (2/2)
- no writes performed on production

Production image: `ghcr.io/jokubohv/scm-suite-api:bc35d213...`; deploy job `40258481911` recreated API/worker/outbox/Caddy and passed readiness.

Evidence: `verification/evidence/E2E/prod-read-smoke.json`.


## OpenAPI/docs hardening

The runtime OpenAPI document is no longer a public endpoint:

- built-in FastAPI `/api/v1/openapi.json` and `/api/v1/docs` are disabled
- a custom `/api/v1/openapi.json` route requires `X-API-Key` matching `OPENAPI_API_KEY`
- production fails closed (503) if the key is not configured; wrong/missing key returns 401
- successful spec responses carry `Cache-Control: no-store, private`
- `/api/v1/docs` returns 404
- `/api/v1/openapi.json` accepts either the deployment-wide `OPENAPI_API_KEY` **or any active tenant API key** in `X-API-Key`
- the spec is global (one runtime document), not tenant-specific
- deploy workflow refuses to run without a >=32-character `OPENAPI_API_KEY` in `.env.production`


## Tenant API-key integration

Live E2E now creates a tenant-scoped API key with `items.read`, then verifies:

- `GET /api/v1/items` with `X-API-Key` → **200**
- `GET /api/v1/admin/flags` with the same key → **403** (scope enforced)
- `POST /api/v1/items` with the same key → **403** (items.update absent)
- revoked key on `GET /api/v1/items` → **401**

Latest local run: **183 checks, 0 failures**; full suite **1,314 passed**; full-surface sweep **1,257 operations, 0 × 5xx**.


## Production API-key verification

After deploying `5b3e2b6` to production (`https://scm.k1hub.com`, tenant `intelranch_serebo`):

- created a temporary tenant API key with scope `items.read`
- `GET /api/v1/items` with `X-API-Key` → **200**
- `GET /api/v1/admin/flags` with the same key → **403** (scope enforced)
- revoked the key → `GET /api/v1/items` with the old key → **401**
- the same tenant admin JWT still returns **200**
- machine API keys are rejected on identity/session routes (`/api/v1/auth/*`, `/api/v1/me*`)

No permanent integration key was left behind; create a named key with the scopes your integration needs.


## MCP gateway verification

- API-key-only `GET /api/v1/mcp` discovery and `POST /api/v1/mcp` JSON-RPC
- JWT bearer rejected; no-key rejected; revoked key rejected
- 240 curated tools listed and **all 240 dispatched** through `tools/call`
- successful live calls: `items_list`, `locations_list`, `warehouses_list`, `scm_api_request`
- full tool workflow: create → submit → post → read back an inventory adjustment through MCP
- resources (`scm://openapi`, `scm://mcp/info`, `scm://mcp/tools`) and prompts covered by tests
- latest local E2E: **204 checks, 0 failures**; full suite **1,319 passed**; surface smoke **1,259 operations, 0 × 5xx**


## Production MCP verification

Deployed image `ghcr.io/jokubohv/scm-suite-api:92b0cf48...`:

- `GET /api/v1/mcp` without key → **401**
- full `serebo-intelranch-full` key → **200**, `tool_count=240`
- `initialize` → **200**
- `tools/list` → **200**, 240 tools
- `items_list` → tool result **200 / isError false**
- `warehouses_list` → tool result **200 / isError false**
- `scm_api_request GET /api/v1/items` → tool result **200 / isError false**
- approval API key can open MCP discovery (**200**), with per-tool approval scopes still enforced
- JWT bearer on MCP → **401**
- `locations_list` returned a controlled `409 DOMAIN_DISABLED` because IntelRanch feature flags are still at defaults; enable the relevant flags to use those tools
