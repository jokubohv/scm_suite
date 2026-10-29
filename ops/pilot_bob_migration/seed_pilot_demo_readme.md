# Pilot BOB demo-data reseed + inventory field verification

Operator runbook for `seed_pilot_demo.py`. Use after the SCM pilot tenant scoped reset and the
guarded USD→BOB base-currency change (`ops/pilot_bob_migration/README.md` in this directory covers
the reset/migration SQL).

The pilot tenant (`intelranch_pilot`) is **explicitly synthetic demo data**. Everything this script
writes is labelled synthetic: document memos/documents carry the `SEED-PILOT-BOB` marker, new master
rows use the `SEED-SYN-*` code prefix, and free-text fields say `sintético`. The script does not
change the tenant base currency, feature flags, permissions, users, or any platform/global reference
row, and it never deletes rows.

---

## 1. What it does

`seed_pilot_demo.py` is a stdlib-only Python 3.11+ script that:

1. Authenticates with an SCM **tenant API key** from `SCM_API_KEY` (`X-API-Key`) and calls only
   `SCM_BASE_URL`/`SCM_API_KEY`. On the Serebo VPS the API container stores the same values as `SEREBO_SCM_BASE_URL`/`SEREBO_SCM_API_KEY`, so the run command maps them explicitly. No other environment variables, hosts, credentials or database access are used.
2. Audits surviving master data and **fills every empty inventory-relevant field** instead of
   duplicating rows:
   - **Items** (all active stockable types): description, category, UPC, external ref, base/stock
     UoM + units type, costing method, initial cost, reorder point / preferred / safety stock / lead
     time, inventory settings (statuses, negative-inventory, account refs), purchasing settings
     (preferred vendor, purchase UoM, BOB purchase price, currency, country/harmonized/tax refs),
     and a preferred item–vendor association.
   - **Vendors** (all active): currency BOB, payment terms, tax id, credit limit, billing/shipping
     addresses, primary contact.
   - **Locations** (all active, except the system `UNALLOCATED` sentinel): address, timezone; two
     warehouse/store locations are selected as posting locations (created with full fields when the
     tenant has none).
   - **Lots**: one complete lot per lot-numbered item (lot number, manufacture date, expiry date,
     BOB cost basis) — existing complete lots are reused.
3. Rebuilds representative transaction history, all posted through the SCM API:
   - one opening-stock **inventory adjustment** (`SEED-PILOT-BOB-OPENING-001`) with a line for every
     stockable item in **two locations** (lots on the lot dimension);
   - one **purchase order** (`SEED-PILOT-BOB PO sintético`, BOB, submitted/approved) and one posted
     **goods receipt** with BOB unit costs, lot details, inventory status and location on every line;
   - one posted **inter-location transfer** (`SEED-PILOT-BOB-TRANSFER-001`);
   - one **inventory count** snapshot: create → start → capture every line → submit
     (`PENDING_REVIEW`; approval needs an operator-only approval scope, see G3).
4. `--verify` re-reads items/vendors/locations/balances/movements/receipts/lots/costs/counts and
   emits a machine-readable JSON coverage report plus a human summary. It is read-only and exits
   non-zero on required-field gaps.

When the tenant has **no** stockable items at all (e.g. a freshly provisioned test tenant), the
script creates a small synthetic demo catalog (6 items covering KG/L/ML/EA, `LOT_NUMBERED` and
`INVENTORY`, FIFO) plus demo vendors and locations. On the pilot tenant the 16 surviving items are
reused.

### FIFO note (G4)

`item_inventory.costing_method` is immutable after the item row exists. The scoped reset only nulls
`initial_cost`, so surviving items keep whatever costing method they were created with. When no
surviving item is FIFO, the script adds **two extra clearly-labelled synthetic FIFO items**
(`SEED-SYN-FIFO-KG-001`, `SEED-SYN-FIFO-EA-001`), so the pilot still demonstrates FIFO costing and
valuation. Inactivate them via the items API after the demo if they are not wanted.

---

## 2. Prerequisites (check before running on production)

| # | Requirement | Why / failure behaviour |
|---|---|---|
| 1 | Tenant base currency is already **BOB** (guarded `PATCH /api/v1/admin/tenant-configuration`) and platform currency `BOB` is active | `--verify` requires BOB on vendors/items/POs/receipts and (deployed) tenant configuration; seed sends BOB everywhere |
| 2 | SCM URL/key passed as `SCM_BASE_URL`/`SCM_API_KEY` to the script inside the running Serebo API container (mapped from the container's `SEREBO_SCM_BASE_URL`/`SEREBO_SCM_API_KEY`) | The script reads only those two variables |
| 3 | API key has the full pilot scope set (`deployment/serebo_pilot_scopes.json`, 227 scopes) | Create/update master data, post adjustments/receipts/transfers, count capture |
| 4 | Feature flags on: `traceability.registry.enabled`, `inventory.ledger.enabled`, `inventory.counts.enabled`, `costing.engine.enabled` | Seed fails fast listing any disabled flag; it never mutates flags |
| 5 | SCM database backup already taken by the migration workflow | Rollback anchor (this script itself never deletes) |

The pilot tenant already had inventory history before the reset, so its flags/statuses/reason
codes/sequences are provisioned. The script only creates a reason code / category / status when the
tenant genuinely has none.

---

## 3. Run on the Serebo VPS (production path)

Everything runs **inside the Serebo API container**, which already holds the SCM env vars. From the
VPS deployment directory (the same place the operator runs `docker compose ... exec` commands):

```bash
# sanity: the SCM env vars exist in the container (no secret is printed)
docker exec serebo-api-1 sh -c '[ -n "$SEREBO_SCM_BASE_URL" ] && [ -n "$SEREBO_SCM_API_KEY" ] && echo scm-env-ok'

# 1) seed (safe to rerun; JSON report to stdout, human summary to stderr)
docker exec -i serebo-api-1 sh -c '
  SCM_BASE_URL="$SEREBO_SCM_BASE_URL" SCM_API_KEY="$SEREBO_SCM_API_KEY" \
  python3 - --json-out /tmp/scm_seed.json' \
  < ops/pilot_bob_migration/seed_pilot_demo.py > /tmp/scm_seed_stdout.json 2> /tmp/scm_seed.log

# 2) read-only verification; exits non-zero on required gaps
docker exec -i serebo-api-1 sh -c '
  SCM_BASE_URL="$SEREBO_SCM_BASE_URL" SCM_API_KEY="$SEREBO_SCM_API_KEY" \
  python3 - --verify --json-out /tmp/scm_verify.json' \
  < ops/pilot_bob_migration/seed_pilot_demo.py > /tmp/scm_verify_stdout.json 2> /tmp/scm_verify.log

# 3) copy evidence out of the container
docker cp serebo-api-1:/tmp/scm_verify.json ./scm_verify.json
docker cp serebo-api-1:/tmp/scm_seed.json  ./scm_seed.json
```

If the operator shell has the script from a git checkout on the VPS, `docker exec -i ... < path`
piping avoids copying files into the image. If the container name differs (`docker ps` shows the
Serebo API service; the production deployment uses a single API service container), substitute it.

Local/CI equivalent (no container, e.g. this repository's local SCM stack):

```bash
export SCM_BASE_URL=http://127.0.0.1:28700
export SCM_API_KEY=<pilot tenant API key, never commit>   # pragma: allowlist secret
python3 ops/pilot_bob_migration/seed_pilot_demo.py            # seed / converge
python3 ops/pilot_bob_migration/seed_pilot_demo.py --verify   # proof
```

Exit codes: `0` success/all required checks pass · `1` verification required-field gaps · `2`
execution error (API/contract). The JSON document is always printed to **stdout**; the human summary
to **stderr**, so `--verify > verify.json` is enough to capture machine evidence.

Additional flags: `--json-out PATH` (also write JSON file), `--seed-date YYYY-MM-DD` (deterministic
seed reference date, default today), `--timeout SECONDS`, `--quiet` (suppress stderr summary).

---

## 4. Idempotency model

A second run converges to **no writes** (`created=0, updated=0, posted=0`). Mechanisms:

- **Read-before-write** for master data: vendors by number, locations by code, items by
  `item_number` + detail read-back, categories/statuses/reason codes by code, lots by item.
- **Stable document numbers/markers**: opening adjustment `SEED-PILOT-BOB-OPENING-001`, transfer
  `SEED-PILOT-BOB-TRANSFER-001`, PO/receipt identified by the `SEED-PILOT-BOB` memo marker, count
  identified by its exact stockable-item scope. Existing documents are reused; DRAFT/
  `PENDING_APPROVAL` leftovers from an interrupted run are submitted/posted instead of recreated.
- **Deterministic synthetic values** (derived from `item_number` hashes) plus **numeric comparison**
  of read-back values, so formatted decimal strings never cause a spurious update.
- **Preference alignment**: if a preferred item–vendor association survived the reset, the item's
  purchasing preferred vendor is aligned to it rather than creating a conflicting preferred vendor.
- **Fixed Idempotency-Key headers** on writes for crash-safe replay.

The local proof runs both tenants twice; the second run produced 0 created / 0 updated / 0 posted
(see `verification/pilot-bob-migration/seed/`).

---

## 5. What `--verify` proves (field coverage map)

JSON: `summary` (pass/fail/warn counts, by area, failed ids), `coverage` (per-area counts and
per-field present/missing with samples), `checks` (id, area, severity, status, expected, actual).
Required gaps are `severity=fail` → exit 1; documented API limitations are `severity=warn` (exit 0)
and their note is printed.

| Area | Field / proof | API source | Check id | Severity |
|---|---|---|---|---|
| tenant | base currency BOB | `GET /admin/tenant-configuration` (deployed; 404 locally = warn G5) | `tenant.base_currency_bob` | fail |
| tenant | BOB active platform currency | `GET /admin/currencies?is_active=true` | `tenant.bob_currency_active` | fail |
| items | description, category, UPC, external ref, type, base UoM, units type | `GET /items/{id}` | `items.<field>` | fail |
| items | costing method, stock UoM, reorder/preferred/safety, lead time, status settings/default status | `GET /items/{id}.inventory` | `items.inventory.<field>` | fail |
| items | FIFO coverage when survivors are not FIFO (immutability, G4) | `GET /items/{id}.inventory.costing_method` | `items.inventory.costing_method_fifo` | warn |
| items | `initial_cost` after the reset (immutability, G4) | `GET /items/{id}.inventory.initial_cost` | `items.inventory.initial_cost` | warn |
| items | preferred vendor, purchase UoM/price, lead time | `GET /items/{id}.purchasing` | `items.purchasing.<field>` | fail |
| items | **purchase price currency == BOB** | same | `items.purchasing.purchase_price_currency` | fail |
| item-vendors | preferred association per stockable item + currency BOB | `GET /item-vendors` | `item_vendors.preferred_association`, `item_vendors.currency_bob` | fail |
| vendors | number, name, tax id, payment terms, credit limit, addresses (+line1), primary contact, currency BOB | `GET /vendors` | `vendors.*` | fail |
| locations | code, name, kind, timezone, address (+line1), ≥2 warehouse/store locations | `GET /locations` | `locations.*` | fail |
| lots | lot number, manufacture/expiry dates, BOB cost basis for every lot-numbered item | `GET /lots` | `lots.*` | fail |
| lots | expiry report readable | `GET /reports/lot-expiry` | `lots.expiry_report` | info/warn |
| balances | every stockable item has on-hand > 0; stock in ≥2 locations; lot-numbered stock on the lot dimension | `GET /inventory/balances` | `balances.*` | fail |
| movements | adjustment + receipt + transfer ledger transactions present, qty/value totals present | `GET /inventory-transactions` | `movements.documents_present`, `movements.qty_value_totals` | fail |
| movements | transaction currency BOB (API gap G1) | same | `movements.currency_bob` | warn |
| receipts | a POSTED seed receipt with BOB header currency, lines with unit cost/status/location/currency, lot details | `GET /item-receipts` + detail | `receipts.*` | fail |
| purchase orders | seed PO present, BOB, priced lines; status submitted/approved | `GET /purchase-orders` | `purchase_orders.*` | fail |
| costing | FS-05 item cost rows for every receipted item, FIFO rows, valuation base value > 0 | `GET /costs/item-costs`, `/costs/valuation` | `costing.*` | fail |
| costing | cost layers vs opening-adjustment quantity (API gap G2) | same | `costing.cost_vs_balance_qty` | warn |
| counts | count with snapshot lines + captured quantities; workflow approval (G3) | `GET /inventory-counts`, lines | `counts.snapshot` / `counts.workflow` | fail / warn |
| opening stock | document POSTED, lines in ≥2 locations, reason codes, lot lines | `GET /inventory-adjustments` + detail | `opening_stock.*` | fail |
| flags | required feature flags enabled | `GET /admin/flags` | `flags.required_enabled` | info/warn |

Coverage also reports, for the Lead's report: stockable items and type mix, per-field present/missing,
vendors/locations counts, lot count, on-hand item/location counts, ledger movements by
`source_doc_type` and currency, receipt/PO/costing/count summaries and valuation value in BOB base
units.

Expected production result: **0 failed checks**; warnings limited to G1 (ledger currency label), G2
(opening adjustment vs cost layers), G3 (count approval scope), and G4 when survivors are not FIFO or
have a null `initial_cost`.

---

## 6. Known API limitations / decisions for the operator (2026-10-29)

These were observed on the local stack (API image ~2026-10-24, schema migration 0031; production runs
schema 0032 and a newer image per `deployment/runtime.openapi.json`). They are reported as `WARN`,
not hidden, and none can be fixed from seed data:

| ID | Limitation | Impact on pilot demo | Fix / decision |
|---|---|---|---|
| **G1** | Inventory-ledger transactions are stamped `currency_code = "USD"`: the adjustment/receipt/transfer posting paths do not pass the document/tenant currency into the ledger command, which defaults to USD. Receipt/PO headers, line currencies, BOB unit costs and `value_base` amounts are correct BOB values. | `GET /inventory-transactions` shows USD labels on a BOB tenant. | SCM code fix recommended (pass tenant base currency / document currency into the ledger posting command); seed cannot change it. Verify warns. |
| **G2** | Opening-stock adjustments post ledger balances but not FS-05 cost layers: only procurement/value posting paths inject the costing adapter. | `item-costs.qty_on_hand` < physical on-hand for opening stock; valuation is proven by the posted receipt. | Accept for demo (valuation and BOB costs are proven), or change the ledger document posting path to inject the costing adapter. Verify warns. |
| **G3** | Count approval/posting requires operator-only scope `inventory.count.approve`, which the Serebo integration API key intentionally does not hold (SoD). | Seeded count stops at `PENDING_REVIEW` with all lines captured (no variance). | Accept, or a human operator approves/posts the count in SCM. Verify warns. |
| **G4** | `item_inventory.initial_cost` / `costing_method` are immutable once the item row exists, even when the surviving value is NULL after the scoped reset. | Surviving items keep `initial_cost = NULL` and their original (non-FIFO) method; FIFO coverage comes from two labelled synthetic FIFO items and BOB purchase prices/receipt costs. | Accept, or fix the item-master API to allow setting immutable fields from NULL (one-line condition change). Verify warns. |
| **G5** | Local stack is older than the deployed API: `/api/v1/admin/tenant-configuration` is 404 locally, local schema is 0031 vs production 0032, the FIFO-layer-persistence fix (~2026-10-26) is not in the local image, and local `platform.currency` had no BOB row (the local harness inserted it). | Local proof cannot show the tenant base-currency GET; production verify should PASS `tenant.base_currency_bob`. BOB must already be active in production before seeding. | Production prereq 1 covers this; production `--verify` must be run to confirm. |
| **G6** | Serialized items (`SERIALIZED`, numbering without lots) and BOM/assembly types are excluded from demo stock because serial numbers/BOMs are out of the pilot demo scope; the sales/accounting item extensions are not written (their columns are not in the deployed schema). | If such survivors exist, `balances.serialized_scope` / notes are reported. | Documented; add serial/BOM demo data only if the pilot needs it. |
| **G7** | When no surviving item is FIFO, the script adds two `SEED-SYN-FIFO-*` items (labelled synthetic) so FIFO valuation is demonstrable. | Pilot catalog grows by 2 items at most. | Inactivate via `POST /items/{id}/inactivate` after the demo if undesired; rerun stays clean. |

Production posting-location choice: the script posts opening stock/receipt/transfer to the first two
active `WAREHOUSE`/`STORE` locations sorted by code (the pilot's 16 items / 13 locations survive the
reset and previously carried balances, so posting eligibility is expected). The remaining surviving
locations are left empty on purpose — the requirement is representative stock in **at least two**
locations.

---

## 7. Rollback / recovery

The script is additive and never deletes. Reversal options (SCM API, operator key):

- Opening adjustment: `POST /api/v1/inventory-adjustments/{id}/reverse` (posted documents cannot be
  voided, only reversed).
- Transfer: `POST /api/v1/inventory-transfers/{id}/reverse`.
- Goods receipt: `POST /api/v1/item-receipts/{id}/reverse`; purchase order: `POST
  /api/v1/purchase-orders/{id}/cancel` (or close).
- Count: leave in `PENDING_REVIEW` (no ledger impact) or reject via the counts API with an
  approval-capable operator; it never posts automatically in this seed.
- Master-data enrichment is in place only for empty fields; the authoritative rollback is the SCM
  database backup taken before the migration (`pg_dump` artifact from the reset workflow). Reruns are
  safe and converge to no-ops, so recovery after a partial run is simply: rerun the seed, then verify.

---

## 8. Local proof (this repository, 2026-10-29)

Evidence: `verification/pilot-bob-migration/seed/` (untracked). Two local scratch BOB tenants were
created through the local bootstrap API (`pilot-bob-surv-final2`, sparse survivor-style master
data; `pilot-bob-fresh-final2`, empty tenant), both with an all-scopes API key from
`deployment/serebo_pilot_scopes.json`:

| Run | Command target | Result |
|---|---|---|
| survivors seed #1 | host Python → local SCM (`http://127.0.0.1:28700`) | OK, 46 actions (18 created / 22 updated / 3 posted), 2 warnings |
| survivors seed #2 | **piped into local SCM API container** (`docker exec -i ... python3 -`) | OK, **9 skipped/reused, 0 created/updated/posted** (idempotent) |
| survivors `--verify` | piped into API container | **exit 0**: 68 passed, 0 failed, 6 warnings (G1, G2, G3, G4×2, G5) |
| fresh seed #1 | piped into API container | OK, 38 actions (28 created / 4 updated / 3 posted) — creates demo catalog |
| fresh seed #2 | host Python | OK, **9 skipped/reused, 0 created/updated/posted** |
| fresh `--verify` | host Python | **exit 0**: 70 passed, 0 failed, 4 warnings (G1, G2, G3, G5) |

Both verification runs proved: all stockable items have every required field populated, purchase
prices/vendor currencies/POs/receipts are BOB, every item has stock in two locations, lot-numbered
stock is on the lot dimension, the receipts and transfers are POSTED with BOB unit costs and lot
details, FS-05 item costs exist for all receipted items and valuation is positive, and the count
snapshot/capture/submit is present. `verification/pilot-bob-migration/seed/SUMMARY.md` has the exact
commands, machine-readable JSON, and the field-coverage table extracted from those runs.

Local harness notes (not part of the deliverable): the local stack's DB had no `BOB` row in
`platform.currency`, so it was inserted directly into the local test DB before bootstrapping the
scratch BOB tenants; local feature flags were enabled on the scratch tenants via the local admin API
only; the local tenant here was created with `base_currency_code=BOB` because the deployed-only
tenant-configuration endpoint does not exist in the older local image. None of this touches
production.


## Production rerun notes (2026-10-29)

* SCM's `Idempotency-Key` cache is not cleared by the scoped reset, so a rerun after a reset
  could replay a cached response pointing at a deleted document. The script therefore salts
  Idempotency-Keys per run (or from `SEED_IDEM_SALT`); read-before-write markers keep it
  idempotent for already-correct data.
* Preserved legacy lots whose declarations are immutable (stock movements exist) are retired
  via `/lots/{id}/inactivate` when a complete synthetic lot already exists for the item.
* Production proof: after reset + BOB switch, seed created opening stock (32 lines), PO000002,
  posted receipt IR000003 (16 BOB lines, 9 lot lines), posted transfer and count snapshot;
  `--verify` returned 69 passed / 0 failed / 5 documented API-limitation warnings.
