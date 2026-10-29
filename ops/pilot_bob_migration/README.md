# SCM pilot tenant scoped reset (USD → BOB migration prerequisite)

Transactional, tenant-scoped reset for the SCM production pilot tenant
`intelranch_pilot` so its base currency can be changed USD → BOB through the
deployed tenant-configuration endpoint. The endpoint refuses the change while
`SqlCurrencyUsage.blockers()` reports any currency blocker rows
(`app/composition/tenant_configuration.py`, `app/core/repositories/tenant_configuration.py`).
This reset deletes exactly that demo/transactional data, keeps the master and
reference data Serebo depends on, and re-runs the exact guard query inside the
transaction before committing.

**The base-currency change itself is NOT performed by these scripts.** After a
successful apply, the endpoint `PATCH /api/v1/tenant-configuration` with
`If-Match: <tenant version>` performs the change (the reset deliberately leaves
`platform.tenant.version` untouched, so the pre-read `If-Match` value stays valid).

## Files (must be copied together — the scripts include `_common.sql`)

| File | Purpose |
| --- | --- |
| `_common.sql` | shared catalog-driven layer: tenant resolution/pinning, exact 40-table guard mirror, protected master list, wipe roots, transitive FK-child closure, safety checks, helper functions. **Not run directly.** |
| `preflight.sql` | read-only evidence: tenant, guard blocker codes/rows, per-table reset counts, protected master counts, baseline line, apply prerequisites. Fails on missing/SYSTEM/wrong tenant or unsafe wipe set. |
| `reset.sql` | transactional apply (`-v mode=apply`, default) or full-path dry run (`-v mode=dry-run`, ends in ROLLBACK). Deletes in child-first FK order, clears the two guard-counted item cost fields, re-runs the guard query, asserts master rows/tenant row/other tenants unchanged, commits. |
| `verify.sql` | read-only post-change proof: guard blockers empty, reset tables empty, protected counts equal preflight baseline, FK closure complete, per-FK orphan scan zero, target currency available, tenant currency is `expected_currency`. |

All three entry scripts create only **session-local TEMPORARY objects** when they
do not apply; `preflight.sql` and `verify.sql` never write application data.

`seed_pilot_demo.py` / `seed_pilot_demo_readme.md` in this directory belong to the
separate reseed task and are not used by these scripts; copy the directory as-is.
No `platform.audit_event` row is written by the reset (there is no safe synthetic
SYSTEM actor; the JSON evidence + the workflow run log are the record).

## Production runbook

Prerequisites:

1. A fresh database backup exists (Lead takes it; `pg_dump` custom format).
2. Target currency `BOB` exists as an active shared (tenant_id NULL) currency.
3. Application traffic is idle enough that the reset can take its locks
   (tenant row `FOR UPDATE NOWAIT` + `SHARE ROW EXCLUSIVE NOWAIT` on the 123
   tables it deletes/updates). Busy sessions make the run fail fast and safely;
   retry when idle. This is the same "brief locks / retryable conflict" model as
   the configuration endpoint itself.
4. The psql session is the compose `POSTGRES_USER` superuser. The reset tables
   contain append-only guard triggers (`reject_mutation`, `item_inventory`
   immutability) that reject DELETE/UPDATE, so the transaction needs
   `SET LOCAL session_replication_role = replica`. The closure is fully checked
   before deleting and `verify.sql` independently scans every FK into the reset
   set, so this cannot hide a missed dependent. Reset refuses to run otherwise.

Copy the whole directory into the postgres container (the scripts use `\ir`, so
run them as files, **not** piped through stdin), then run the four steps:

```bash
COMPOSE="docker compose --env-file .env.production -f docker-compose.prod.yml"
$COMPOSE cp ops/pilot_bob_migration postgres:/tmp/pilot_bob_migration

# 1) Preflight (read-only). Save the whole output: it contains the baseline line.
$COMPOSE exec -T postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
  -v ON_ERROR_STOP=1 \
  -v tenant_code=intelranch_pilot \
  -v expected_tenant_id=7ee5cb96-619f-466e-9b34-9d2fef906f10 \
  -f /tmp/pilot_bob_migration/preflight.sql' > preflight.out

BASELINE="$(sed -n 's/^PILOT_PROTECTED_COUNTS_BASELINE //p' preflight.out)"

# 2) Dry run: executes the complete delete/update path, then ROLLBACKs.
$COMPOSE exec -T postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
  -v ON_ERROR_STOP=1 -v mode=dry-run \
  -v tenant_code=intelranch_pilot \
  -v expected_tenant_id=7ee5cb96-619f-466e-9b34-9d2fef906f10 \
  -f /tmp/pilot_bob_migration/reset.sql' > reset.dry-run.out
# expect exit 0 and PILOT_RESET_OUTCOME dry-run-rolled-back

# 3) Apply.
$COMPOSE exec -T postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
  -v ON_ERROR_STOP=1 -v mode=apply \
  -v tenant_code=intelranch_pilot \
  -v expected_tenant_id=7ee5cb96-619f-466e-9b34-9d2fef906f10 \
  -f /tmp/pilot_bob_migration/reset.sql' > reset.apply.out
# expect exit 0 and PILOT_RESET_OUTCOME applied-committed

# 4) Verify (before the PATCH: base currency is still USD).
$COMPOSE exec -T postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
  -v ON_ERROR_STOP=1 \
  -v tenant_code=intelranch_pilot \
  -v expected_tenant_id=7ee5cb96-619f-466e-9b34-9d2fef906f10 \
  -v expected_currency=USD \
  -v baseline_protected_counts="$BASELINE" \
  -f /tmp/pilot_bob_migration/verify.sql' > verify.before-patch.out
# expect exit 0, PILOT_VERIFY_EVIDENCE ... "verification": "ok"
```

Then perform the guarded API change:

```http
PATCH /api/v1/tenant-configuration
If-Match: <tenant version read before the PATCH, e.g. "1">
{"base_currency_code": "BOB"}
```

and run `verify.sql` again with `-v expected_currency=BOB` and the same baseline.
Re-running this reset after a successful apply is idempotent (deletes 0 rows).

Machine-readable marker lines (always last):

* `PILOT_PREFLIGHT_EVIDENCE <json>` and `PILOT_PROTECTED_COUNTS_BASELINE <json>`
* `PILOT_RESET_OUTCOME <dry-run-rolled-back|applied-committed>` and `PILOT_RESET_EVIDENCE <json>`
* `PILOT_VERIFY_EVIDENCE <json>` (`"verification": "ok"` or non-zero exit)

## What is deleted (reset set)

Roots, then the transitive FK-child closure computed from `pg_catalog`:

* every tenant row in schemas `inventory_ledger`, `costing`, `manufacturing`,
  `consigned`, `counts` — **except** `inventory_ledger.inventory_reason_code`
  (reason codes are master data);
* the procurement documents: `purchase_order*`, `item_receipt*`,
  `vendor_bill*`, `vendor_credit*`, `vendor_return_authorization*`;
* `platform.finance_journal_intent` (finance journal intents);
* FK-child closure outside those roots, catalog-derived. On the 0031/0032
  schema that is:
  `procurement.special_order_link`,
  `wms.inbound_shipment`, `wms.inbound_shipment_line`,
  `wms.receiving_order`, `wms.receiving_order_line`,
  `wms.putaway_task`, `wms.putaway_task_line`
  → 121 tenant-scoped tables in total. If a future migration adds another
  dependent, the closure adds it automatically; if it appears outside the
  known transactional modules (`inventory_ledger`, `costing`, `manufacturing`,
  `consigned`, `counts`, `procurement`, `wms`) the run aborts for review instead
  of guessing.

Not deleted, only cost fields cleared on their preserved rows (the exact values
the guard counts):

* `item_master.item_inventory.initial_cost = NULL`;
* `item_master.item_purchasing.purchase_price = NULL` only where
  `purchase_price_currency IS NULL` (explicitly denominated prices are untouched);
* `updated_at`/`version` are bumped on those rows.

## Protected master/reference tables (never deleted)

* `platform.tenant` + all auth/RBAC/platform reference tables, `app_user`,
  `user_role`, `api_key`, `role`, `role_permission`, `permission_scope`,
  `user_identity`, `tenant_setting`, `number_sequence`, `currency`, audit/outbox
  and other platform rows (everything in `platform` except
  `finance_journal_intent`);
* all `item_master.*` (items, categories, UoMs, item types, item_*
  settings incl. item_inventory/item_purchasing rows);
* all `traceability.*` (locations, bins, zones, inventories statuses, lots,
  serial numbers/history);
* `procurement.vendor`, `vendor_category`, `item_vendor`, `item_vendor_price`,
  `vendor_contract`, `vendor_contract_item`;
* `inventory_ledger.inventory_reason_code`.

The exact list is materialised from the catalog into `_mig_keep` (68 tables on
this revision); the wipe set is aborted if it intersects it. Preflight prints the
counts of 30 key master tables (items/vendors/locations/lots/users/API keys/roles/
scopes/number sequences/…) as the baseline; reset re-counts before/after inside
the transaction and aborts if any decreased, and aborts if any row of any other
tenant (incl. SYSTEM) changed.

## Safety model

* Single tenant resolved by `code`, self-consistency checked (`id = tenant_id`),
  SYSTEM refused, `expected_tenant_id` pin required for `apply`.
* One transaction: locks tenant row (`FOR UPDATE NOWAIT`, same lock the endpoint
  takes), locks all touched tables (`SHARE ROW EXCLUSIVE NOWAIT`), deletes
  child-first in catalog-derived order, updates cost fields, re-runs the exact
  guard query, asserts reset tables empty, master counts not decreased, tenant
  version/base currency unchanged, other tenants untouched.
* Any failed assertion raises; `psql -v ON_ERROR_STOP=1` exits non-zero and the
  whole transaction rolls back — no partial apply. A remaining blocker after the
  deletes is therefore never committed.
* Every DELETE/UPDATE is `WHERE tenant_id = <resolved tenant id>`.
* Locks block concurrent posting, so no new blocker can be inserted between the
  post-delete guard check and COMMIT.
* No schema change, no audit/outbox writes, no sequence resets, no trigger or
  privilege changes (session_replication_role is transaction-local).

## Rollback

* Before apply: run with `-v mode=dry-run` (or just preflight).
* An apply that fails any check rolls itself back.
* After a committed apply the only rollback is the pre-apply `pg_dump` backup
  (Lead-owned backup artifact): `pg_restore` into the database (or restore the
  whole compose volume) — take the instance quiescent first.
* The base-currency PATCH itself is reversible with another PATCH (provided the
  reset left no blockers, which is guaranteed by the same guard).

## Schema compatibility (0031 local vs 0032 production)

Production runs `0032_currency_global_writes`, which only adds
`platform.currency` INSERT/UPDATE RLS policies. The scripts discover tables,
columns and FK relationships from `pg_catalog`/`information_schema`, never from
Alembic revision numbers, and they never touch `platform.currency`; preflight
prints the observed `alembic_version`. The local proof was executed on both a
0031 copy and a copy migrated to 0032 (same result).

## Residual notes / known gaps

* Polymorphic, non-FK references are intentionally left in place:
  `platform.audit_event`, `platform.outbox_event`, `platform.document_link`,
  `platform.attachment_metadata` may still mention now-deleted document ids.
  They are append-only history/evidence and are not currency blockers.
* Procurement `rfq*`, `purchase_request*`, `purchase_requisition*`,
  `vendor_contract*` are masters/config or non-guarded documents and are kept;
  only the five guarded document families plus their closure are reset.
* `pricing.*`, `quality.*`, `replenishment.*`, `extensions.*`, `wms.*` data that
  does not reference a reset table is kept (not currency blockers, preserves
  vendor/item configuration). WMS receiving/putaway rows that reference reset
  procurement documents are deleted through the closure.
* `platform.number_sequence` rows are preserved, so reseeded demo documents
  continue from the existing numbering (no duplicate-document risk).
* The app runs against this tenant while the reset is applied: preflight/report
  the tenant as expected; the endpoint PATCH is the only supported way to change
  the currency afterwards. Run the PATCH immediately after apply; any inventory/
  procurement posting between apply and PATCH can re-create a blocker, in which
  case the PATCH fails safely and the reset can be replayed (idempotent).

## Local proof

See `verification/pilot-bob-migration/` (untracked): copy-DB proof on
`pg_dump` copies of the local `iui_scm` (`iui_scm_bob_test`,
`iui_scm_bob_test_rollback`, `iui_scm_bob_test_0032`), never on `iui_scm`
itself (its counts are captured before/after and are identical). Result:
35/35 expectations met (`summary.json`, `PROOF.md`, raw logs in `logs/`).

It covers preflight → dry-run (rollback) → apply → verify → simulated BOB
PATCH → verify, idempotent re-apply, five safety refusals (missing tenant,
SYSTEM tenant, wrong expected id, apply without expected id, wrong expected
currency), the post-delete-blocker ROLLBACK path (derived test copy of
`reset.sql` re-creates an `item_cost_configuration` blocker; transaction rolls
back and counts are identical after), the same flow on a copy at Alembic 0032,
and `check_guard_mirror.py` proving the SQL guard mirror still matches
`app/composition/tenant_configuration.py` (run it after any change to either
side).
