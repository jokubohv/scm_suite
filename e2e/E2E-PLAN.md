# Live E2E plan — 2 companies, multiple warehouses, full API

**Goal:** reproduce production bugs locally and validate the mounted API end-to-end with the same
security topology as production (non-superuser app role + RLS + operator bootstrap), using two
independent companies and multiple warehouses.

## 1. Environment (local, production-like)

- PostgreSQL 16 and Redis 7 from the dev compose stack.
- Dedicated database `inventory_e2e`; migrations run as the DB owner.
- API runs with a **login role** `inventory_e2e` that is a member of `platform_app`, `platform_auth`,
  `finance_adapter` — **not** a superuser, so RLS and the auth-service role switching are exercised.
- `platform_auth` has `BYPASSRLS` (auth only); normal routes use request-scoped `app.tenant_id`.
- `SUPER_ADMIN_TOKEN` enabled; `ACCOUNT_CREATION_SUPER_ADMIN_ONLY=true`;
  `SSO_JIT_PROVISIONING_ENABLED=false`.
- API bound to `127.0.0.1:28080`; evidence written under `verification/evidence/E2E/`.

## 2. Actors and data

| Actor | Purpose |
|---|---|
| Operator | Calls `/api/v1/bootstrap/*` with `X-Super-Admin-Token` |
| Company A admin | Tenant `E2E-A-<run>` admin, created by operator |
| Company B admin | Tenant `E2E-B-<run>` admin, created by operator |
| Company A warehouses | `WH-A1`, `WH-A2` (two locations, separate bins) |
| Company B warehouse | `WH-B1` |

All names/codes/emails are run-unique so the harness is repeatable on a shared local DB.

## 3. Test phases

### P0 — environment and account policy
- `/healthz` 200; `/readyz` 200 with DB `ok`, Redis `ok`, migration head `0030_rls_grants_verify`.
- `/api/v1/bootstrap/whoami` 200 with operator token, 401 without, 401 with wrong token.
- Bootstrap two tenants with their first admin; verify IDs, RBAC counts (255 scopes/7 roles/591 links).
- Verify duplicate tenant code → 409; duplicate admin membership → 409.
- Login both admins; verify separate tenant IDs and JWT access tokens.
- Verify ordinary tenant-admin account creation without operator token is rejected under hardened mode.
- Verify no public signup route exists (public allowlist check).

### P1 — master data and multiple warehouses
- Create two warehouses/locations for Company A and one for Company B; create bins/zones.
- Create items (stockable, lot/serial where relevant), UOMs, item-location assignments.
- Create a pricing record and a vendor for each company.
- Verify cross-tenant reads return 404, not another company's data.

### P2 — inventory operations (Company A)
- Inventory adjustment in WH-A1 → posted ledger + balance.
- Transfer WH-A1 → WH-A2; verify source/destination balances and one company only.
- Commitment/reservation and release.
- Count plan for WH-A1, snapshot, capture, approve/post variance; verify snapshot immutability.
- Query stock ledger, balances, availability, and reporting views.

### P3 — procurement (Company A)
- Vendor + item-vendor + PO → approve → receipt (WH-A1) → post → bill → 3-way match.
- Verify open PO quantity, receipt bill match, on-hand/commitment effects, landed-cost read path.
- Credit/return path if exposed by the live contract.

### P4 — WMS (Company A, both warehouses)
- Inbound shipment/receipt → putaway to bins in WH-A1; outbound wave → pick → pack → ship from WH-A2.
- Verify WMS tasks, inventory quantities and location/bin effects.
- Attempt pick in a blocked bin and verify rejection.

### P5 — manufacturing (Company A)
- Create assembly, BOM, routing (if needed); work order release/issue/complete; build/unbuild.
- Verify WIP/build/unbuild postings and component consumption.
- Exercise compatibility validation and unbuild disposition if the contract supports it.

### P6 — quality / consigned / extensions / reporting (Company A and B)
- Create quality plan/inspection; accept/reject; verify hold/status behavior.
- Create consignment agreement/item (if tenant flags enabled); receive/consume.
- Create warranty/customer part/UPC extension records.
- Query all reporting views; verify tenant isolation and read-only behavior.

### P7 — API surface sweep and negative tests
- Load `/api/v1/openapi.json`; assert expected operation count and uniqueness.
- Sweep all mounted **GET list/detail** routes with each tenant token and appropriate sample IDs:
  no 5xx; expected 401/403/404 for cross-tenant/invalid paths.
- Verify all write operations used above return expected statuses and idempotent replays where supported.
- Record route coverage: contexts exercised / total contexts and operations hit.

## 4. Pass/fail

**PASS** requires:
- P0–P7 no unexpected 5xx;
- both companies operate independently (cross-tenant 404/403);
- inventory/value invariants hold (balances, transfers, PO open qty, count variance);
- all 16 contexts have at least one successful lifecycle or documented read-only path;
- evidence contains request/response status, failure bodies, tenant IDs, coverage and checksums.

Any 5xx, RLS leak, auth bypass, data mismatch or bootstrap failure is a **bug** and captured with the
request, response, tenant and timestamp. The harness continues after failures where safe and exits
non-zero if any check fails.

## 5. Harness

- `e2e/run_live_e2e.py`: httpx-based live API driver; unique run ID; JSON + Markdown evidence.
- `e2e/setup_local_e2e.sh`: creates the local DB/roles, runs migrations, starts the API with the
  app role, and prints the environment needed by the driver.
- No plan-doc changes; this is verification tooling.


## 6. Latest result

`e2e/run_live_e2e.py` latest run: **170 checks, 0 failures**; 2 tenants, 3 warehouses;
details in `verification/evidence/E2E/REPORT.md`. Production bugs found/fixed during this run:
global-currency RLS, traceability flag mismatch, procurement idempotency begins, bootstrap RLS
context, and failed-login tenant-code event handling.
