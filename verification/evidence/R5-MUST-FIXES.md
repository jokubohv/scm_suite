# R5 independent verification — MF1–MF8 remediation

Source baseline reviewed: `10-r5-independent-verification.md` (HEAD `53c4809`).
Changes are in the working tree, not deployed.

| Must-fix | Status | Change |
|---|---|---|
| MF1 item create/update fields | FIXED | `ItemCreate` adds `units_type_id`, `uoms[]`, `upc_code`, `external_ref`, `created_from`; `display_name`/`item_type_code` optional; `ItemUpdate` adds `upc_code`/`external_ref`, removes ignored fields |
| MF2 adjustment fields | FIXED | `AdjustmentCreate.adjustment_source`; adjustment line adds `serial_id`, `uom_id`, `value_delta`, `reason_code_id`, `memo` |
| MF3 write-result fields | FIXED | `ActionResult` and `LedgerActionOut` expose ledger sequence/posting/totals/warnings/replayed; `CountActionOut` exposes tolerance/approval_definition_id/auto_posted |
| MF4 count native responses/aliases/export | FIXED | native `CountLinePage`, `CountVarianceOut`, `CountSnapshotOut`; capture/recount `CountActionOut`; alias routes copy response models; count plans native; export is a non-JSON `Response` |
| MF5 synthetic response clobbering | FIXED | `response_schemas.py` removed; no `apply_contract_metadata`; no synthetic response injection; auth login retains native union response |
| MF6 location/status DTOs | FIXED | `LocationCreate`, `LocationUpdate`, `InventoryStatusCreate`, `InventoryStatusUpdate` with real fields/flags/enum; routes use them |
| MF7 required flags | FIXED | `VendorCreate.vendor_number` required; `UomCreate.uom_type_id` required; `CommitmentCreate.source_line_ref` required; `ItemCreate` only `item_number` required |
| MF8 receipt details | FIXED | `ReceiptCreateLine.details` is `list[ReceiptDetailIn]` with qty/bin/lot/serial/status/ownership |

## Verification

- New regression gate: `app/tests/integration/platform_integrations/test_r5_must_fixes.py` (7 tests)
- Full suite: **1,335 passed**
- Local E2E: **204 checks, 0 failures**
- Surface sweep: **1,259 operations, 0 × 5xx**
- Runtime OpenAPI and provider contract artifacts regenerated
- No deployment performed
