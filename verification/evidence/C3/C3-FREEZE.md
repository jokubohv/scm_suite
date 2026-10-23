# C3 — Freeze record (1A/1B provider contracts)

**Status: FROZEN** · plan v1.5.0 · 2026-10-23 · Lead approval
**Deployment:** FS-06 (1A), FS-07 (1A), FS-08 (1B) verified; FS-00..05 prior waves verified.

## Gates (logs in this directory)

| Gate | Result |
|---|---|
| Generated contracts fresh | PASS |
| Contract lint | PASS — 0 errors, 2 known warnings (`extensions.admin` FS-14) |
| Contract tests | PASS |
| Full backend suite | PASS |
| Live DB / stack | 7/7 healthy, alembic head `0017_procurement_billing` |

## Frozen surface

- FS-06 **traceability.numbered@1.0.0** — 3 tables / 0013.
- FS-07 **counts.engine@1.0.0** — 6 tables / 0014.
- FS-08 **procurement.p2p@1.0.0** — 29 tables / 0015–0017; 89 ops/67 paths.
- Aggregate OpenAPI 894 paths / 1149 operations; SHA-256 `78377a25fa3acdd533d9be9aa03c792c9947cfd118364f8e91d72efd9b62dbaf`.
- Deployment contracts: {"FS-00": ["order.demand", "order.supply", "platform.core", "platform.events", "platform.finance"], "FS-01": ["item.master"], "FS-02": ["pricing.engine"], "FS-03": ["traceability.registry"], "FS-04": ["inventory.balances", "inventory.ledger"], "FS-05": ["costing.engine"], "FS-06": ["traceability.numbered"], "FS-07": ["counts.engine"], "FS-08": ["procurement.p2p"]}

## Accepted residuals
1. Contract fragment accounting still partial for FS-01/FS-04 (per-FS builder overlay pending); provider tests green.
2. FS-06: 2 frozen-FS-04 columns absent (`inventory_transfer_line.serial_id`, `transfer_order_line.serial_id`); 21 deferred FKs closed; 0013 down/up re-verification pending after schema-safe fix.
3. FS-07: FS-00 declarative approval rows not wired (`approval_request_id: null`); job handlers integration-owned.
4. FS-08: landed-cost T-26/T-27 bridge not invoked from receipts/bills yet; job handlers stubs; transition idempotency state-guard only.
5. Settings namespace workarounds (`inventory` + `counts.`/`procurement.` prefixes) to be reconciled by future FS-00 amendment.
6. Event payload canonical stubs TODO for some contracts; real schemas under `app/contracts/*/events/`.

## Freeze policy

FS-06/07/08 contracts additive-only within major versions. **B4b (FS-09…FS-15) may build against C3**;
deploy waves 1C–1F/2 remain serial/ordered (1F parallel group only as specified).
