# W0 runtime probes 2 — P1 residual audit (dev tenant, 2026-10-27)

Bounded, synthetic probes against the deployed SCM dev tenant (`X-API-Key`, tenant
`intelranch_serebo`; key never printed/stored). Baseline deployed commit: `0d357d1` (contract r7,
canonical sha `970404b1…`). All synthetic documents use a unique `PROBE-*` reference; no secrets or
personal data are recorded here.

## Probe results (pre-deploy baseline)

| Flow | Calls | Result |
|---|---|---|
| Adjustment | create → post → reverse | **PASS** — 201 / 200 / 200; balance returns to zero after reverse |
| Transfer | create → submit → post → reverse | **PASS** — 201 / 200 / 200 / 200 |
| Lot | create → inactivate | **PASS** — 201 / 200 (lot inactive, `version` bumped) |
| Count | create (synthetic item+location scope, valid balance) | **FAIL** — first count `CNT000001` created; every subsequent count create returned **500 INTERNAL_ERROR** with a correlation id |
| Count | CALCULATED with empty scope | **FAIL** — 500 INTERNAL_ERROR (expected 422/409 validation) |
| Balances read | `GET /inventory/balances?item_id=` | PASS — 200 with the adjusted quantity |

## Confirmed bug fixed — only one unposted count per tenant

Root cause: `counts.inventory_count` declared
`UNIQUE (tenant_id, posted_transaction_id) NULLS NOT DISTINCT`. Every unposted count has
`posted_transaction_id IS NULL`, so the first DRAFT count occupied the single NULL slot and all later
count creates failed with an unhandled unique violation (500). The validation path for
CALCULATED-with-empty-scope also surfaced as 500 on the deployed build; it is re-probed post-deploy.

Fix:
- `app/modules/counts/tables.py` — replace the constraint with a **partial unique index**
  `uq_inventory_count_posted_txn (tenant_id, posted_transaction_id) WHERE posted_transaction_id IS NOT NULL`,
  preserving exactly one posted transaction per count while allowing many unposted counts.
- Migration `0031_count_post_txn_idx` drops the old constraint and creates the partial index; the
  downgrade guard fails if more than one unposted count would violate the old constraint (explicit
  reconciliation instead of silent data loss).
- Regression test `test_multiple_unposted_counts_are_allowed_per_tenant` (two manual counts in the
  same tenant -> two 201s; both visible in the list). CALCULATED validation remains covered by the
  counts suite.

## Contract change (IntelRanch re-freeze required)

`LocationUpdate` no longer publishes `code`/`location_type`/`is_unallocated` (service already rejects
them). The DTO now forbids unknown fields, so an old client sending them fails loudly (422) instead of
a silent no-op. `LocationCreate` keeps the full create surface. Runtime OpenAPI/contract artifacts were
regenerated. **This changes the frozen r7 surface**: IntelRanch must re-fetch/re-verify and re-freeze
its pinned snapshot; this branch does not claim the r7 freeze covers it.

## MCP residual cleanup

- Added curated tools for lots (`lots_list/get/create/update/inactivate`), fulfilments
  (`fulfillments_create/get`) and inventory transactions
  (`inventory_transactions_list/create/get/reverse`).
- Removed the two empty-input list tools (`reports_list`, `work_centers_list`); both remain reachable
  through `scm_api_request`.
- Tool count: 255 curated tools (144 body-bearing, 0 opaque), updated `deployment/MCP.md`.

## Gates run locally

```bash
# counts regression (disposable Postgres, migrations through 0031)
TEST_DB_PROFILE=url TEST_DATABASE_URL=postgresql+asyncpg://... pytest app/tests/integration/counts -q -n 0
# -> 22 passed

# traceability location contract + registry
pytest app/tests/integration/traceability/registry/test_registry_api.py -q -n 0   # 14 passed
pytest app/tests/integration/platform_integrations/test_r5_must_fixes.py -q -n 0  # 7 passed
pytest app/tests/integration/platform_integrations/test_mcp.py -q -n 0            # 9 passed

python -m app.contracts.cli generate --check   # PASS
python tools/export_runtime_openapi.py --check # PASS after regeneration

# migration chain + schema-live gates
pytest app/migrations/tests app/tests/migrations app/tests/integration/platform_data/test_platform_schema_live.py -q -n 0  # 24 passed
# full suite (disposable Postgres, xdist)
pytest -q -n auto                              # exit 0 (no failures)
```

Post-deploy probes re-run the adjustment/count/transfer/lot flows against the deployed image (see
deploy evidence run URL); no contract freeze is claimed.

## Post-deploy verification (v2026.10.27-p1 / commit 85ba928)

Deployed via `deploy-vps.yml` run `40446147555` (verify rerun once after a pre-existing concurrency
deadlock flake; second attempt verified + built + deployed green). Live checks:

| Check | Result |
|---|---|
| `GET https://scm.k1hub.com/readyz` | **200** `{"status":"ok","checks":{"database":"ok","migrations_head":"0031_count_post_txn_idx","redis":"ok"}}` |
| Live OpenAPI | 200, **4,411,670 bytes**, raw sha256 `3277e63fd02dcb2ff0f295425d7187c3b4dfebb3158b4e07703b892fa56ed86a` (r7 970404b1… no longer applies — contract changed, re-freeze required) |
| `LocationUpdate` live schema | `['address','allow_commit_from_non_available','is_bins_enabled','is_consignment_allowed','is_statuses_enabled','name','parent_id','subsidiary_ref','timezone']` — immutable fields gone |
| MCP live catalogue | **255 tools**, `lots_*`=5, `fulfillments_*`=2, `inventory_transactions_*`=4; `reports_list`/`work_centers_list` removed |
| Adjustment create/post/reverse | **201 / 200 / 200** |
| Count create (synthetic item+location) | **201** (previous 500 fixed), then start 200, lines 200, capture 200, variance 200 with `totals` present + `blind=false`, snapshot 200 with `items=1`, submit 200, approve 200 `auto_posted=true`; explicit post 409 `DOCUMENT_ALREADY_POSTED` (expected, approve auto-posted) |
| Transfer create/submit/post/reverse | **201 / 200 / 200 / 200** |
| Item/location/lot create + balances read | **201/201/201/200** |

### Second confirmed bug found post-deploy — CALCULATED count with no real scope

`POST /inventory-counts {"count_type":"CALCULATED"}` returned **201** and created an all-positions
DRAFT count (`expected_lines=10`) instead of 422. Cause: `CountCreate` materialises
`scope={items:[],locations:[],bins:[],statuses:[],abc:[]}`, so the service's `if not scope:` guard was
never true. Fix: validate meaningful scope content (`item_ids/location_ids/status_ids/bins/abc`) before
creating; regression test `test_calculated_count_requires_a_real_scope` covers absent/empty/default
scope (422) and a valid item scope (201). This is included in the follow-up deploy commit and was
verified after the second deploy (live probe: 422 `VALIDATION_FAILED`).
