# W0 evidence probes — runtime fixes and regression coverage

Source findings: IntelRanch W0 evidence probes (dev tenant, 2026-10-27).

## Runtime fixes

| Finding | Fix | Regression probe |
|---|---|---|
| FIFO receipt post 409 `COSTING_ERROR` / `fk_entry_layer` FK violation | `CostingRepository.persist_value_result` now maps the engine's in-memory layer UUID to the persisted `cost_layer.id` before inserting `cost_ledger_entry.layer_id` | `test_fifo_receipt_post_and_reverse` |
| Receipt reverse 500 `INTERNAL_ERROR` | Added standalone AVERAGE and FIFO receipt post→reverse probes; both now return 200 with stable problem+json behavior on failures | `test_average_receipt_post_and_reverse`, `test_fifo_receipt_post_and_reverse` |
| LOT_NUMBERED receipt/balance lot dimension null | `_balance_delta` now aggregates at the physical detail dimensions (lot/serial/status/ownership) instead of the line's null lot, so lot balances are created and lot-specific fulfillment sees availability | `test_receipt_lot_detail_creates_lot_balance_and_fulfillment` |

## Probe coverage

- FIFO receipt post creates a persisted cost layer and reverses successfully.
- AVERAGE standalone receipt posts and reverses successfully.
- T-01 receipt with `details[].lot_id` creates a lot-dimension balance row; lot-specific fulfillment succeeds and leaves the expected remaining quantity.
- Serialized receipt → transfer preserves serial-dimension balances at the destination.
- Serialized receipt → fulfillment removes the correct serial balances.
- Serialized receipt → adjustment expands `serial_ids` into serialized lines and posts.
- Full test suite covers all service response DTOs and contract gates.

## Similar-class fixes found during the probe follow-up

- Transfers accepted `serial_ids` but post ignored them: fixed by expanding serial IDs into ledger details.
- Fulfillments ignored `serial_ids`: fixed.
- Transfer-order fulfil/receive ignored lot/serial dimensions: fixed.
- Adjustments accepted `serial_ids` but stored only one `serial_id`: fixed by expansion.
- Balance aggregation now always uses detail dimensions, covering receipts, counts, transfers, fulfillments and transfer orders.

## Remaining audit gaps

- The original dev-tenant receipt-reverse 500 condition was not reproduced locally; AVERAGE/FIFO post→reverse probes pass.
- Returns do not expose `serial_ids`.
- Reverse routes for adjustments/worksheets/transfers/distributions still lack dedicated integration probes.
- Transfer-order serialized probe is not yet added.
- WMS/manufacturing/consignment posting-dimension paths remain outside this probe set.

## Verification

Run before deployment:

```bash
export TMPDIR="$PWD/.tmp" TEST_DATABASE_URL=postgresql+asyncpg://inventory:inventory-dev-password@localhost:5432/inventory
python3 -m pytest app/tests/integration/procurement/test_runtime_probes.py \
  app/tests/integration/inventory_ledger/test_lot_balance_probe.py -q
python3 -m pytest -q
```
