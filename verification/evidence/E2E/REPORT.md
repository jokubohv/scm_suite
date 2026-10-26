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
