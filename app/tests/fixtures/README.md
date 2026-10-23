# Test fixtures (`app/tests/fixtures`)

Stable, checked-in inputs for unit/invariant/golden suites. Additive-only: never rename/remove codes
or files; extend with new files and note the owning feature set.

| File | Purpose | Owner / consumer |
|---|---|---|
| `permissions.json` | Byte-identical copy of `design/permissions.json` (255 scopes / 7 role templates / 14 SoD pairs) used for RBAC fixtures and contract tests | FS-00 / all |
| `uom.json` | Canonical UoM pairs (`eaches`, `weight`) with base-unit flags and conversion factors | FS-01, FS-04 |
| `inventory_statuses.json` | Canonical inventory statuses + availability/commit/sell/consigned flags (exactly one default) | FS-03, FS-04 |
| `items.json` | Canonical item samples covering the posting-eligibility matrix (inventory, lot, serial, assembly, WIP, kit, service, drop-ship) | FS-01+ |
| `golden/workflow_journey.json` | Cross-set golden journey skeleton (receipt → TO fulfil/receive → count variance → fulfilment) with expected ledger invariants; `placeholder: true` until FS-04/05 engines land | e2e owner (B3/B4) |

Rules:

- Fixtures are plain JSON (no code imports) so they can be consumed by Python tests, contract-test
  mocks and future fixture loaders. Quantity/money values are decimal strings (`NUMERIC(38,12)`).
- UUIDs are deterministic placeholders produced by `app/tests/utils/deterministic.py` factories; they
  are not production ids.
- `UPDATE_GOLDEN=1 pytest ...` rewrites a golden fixture during development; never leave it on in CI.
- Keep `permissions.json` in sync with `design/permissions.json` (diff is part of S0-GATE/CI); the seed
  loader reads the design file first and falls back to this copy only when the plan package is absent.
