# R1–R3 SCM fix request — remediation

Baseline: `e5f68ef`.

| ID | Status | Fix |
|---|---|---|
| R1 | FIXED | `ActionResult.warnings` and `LedgerActionOut.warnings` are `list[str]`, matching the ledger service's `["NEGATIVE_INVENTORY_WARN_POLICY"]` shape |
| R2 | FIXED | `CountVarianceOut` adds `totals: dict | None` and `blind: bool | None`; runtime totals/blind preserved |
| R3 | FIXED | `CountSnapshotOut` adds `items: list[dict]`; snapshot rows are no longer dropped |

Acceptance coverage:
- `test_negative_inventory_warn_policy_returns_string_warnings` sets tenant policy WARN and asserts a successful posting with string warnings.
- count variance test asserts `items`, `blind`, and the four `totals` keys (`variance_qty_total`, `variance_qty_abs`, `variance_value_base`, `counted_lines`).
- count snapshot test asserts `items` contains snapshot rows.
- R5 regression test asserts warning item schemas are strings and count variance/snapshot fields are published.

Optional residuals also addressed:
- `LocationUpdate` immutable identity fields **removed from the PATCH DTO** (the service already rejects
  mutation): `code`/`location_type`/`is_unallocated` are no longer published; the DTO forbids unknown
  fields so older clients get a loud 422 instead of a silent no-op. `LocationCreate` keeps the full
  create surface. **This is a contract change** — IntelRanch must re-generate/re-freeze its pinned
  snapshot (do not claim the r7 freeze covers this revision).
- `CountPlanOut.next_count_date` added.

Verification: full suite, E2E and surface sweep run before deployment; runtime OpenAPI and contract artifacts regenerated. Deployment hash/time and live spec sha256 will be reported after deploy.
