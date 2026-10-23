# C1 — Freeze record (FS-01 item.master, FS-02 pricing.engine)

**Status: FROZEN** · plan v1.5.0 · 2026-10-23 · Lead approval
**Deployment:** FS-00/FS-01/FS-02 recorded `verified` in `deployment-state.dev.json` (wave 0A + 0B).

## Gates (logs in this directory)

| Gate | Result |
|---|---|
| FS-00 builder `--check` / FS-02 pricing builder `--check` | PASS |
| Generated contracts fresh (`contracts.cli generate --check`) | PASS |
| Contract lint | PASS — 0 errors, 2 known warnings (`extensions.admin` FS-14) |
| Contract tests (`pytest app/tests/contracts`) | PASS |
| Route-scope strict (`C0_STRICT_ROUTE_SCOPE=1`) | PASS |
| FS-01 provider contract | PASS |
| FS-02 provider contract | PASS |
| Full backend suite | PASS |

## Frozen surface

- App/runtime OpenAPI: **730 paths / 965 aggregate operations**; aggregate x-counts: {"contracts": 21, "events": 159, "feature_sets": 16, "implemented_additions": 31, "implemented_operations": 247, "implemented_overrides": 216, "implemented_paths_added": 20, "operations": 965, "paths": 730, "skeleton_operations": 718, "spec_operations": 934, "spec_paths": 710}.
- FS-00 provider contracts: platform.core/events/finance@1.0.0, order.demand/supply@0.1.0 (C0).
- FS-01 provider contract: **item.master@1.0.0** — 59 implementation-backed operations (40 paths) in `app/contracts/item_master/openapi.fragment.json`; runtime router registered; migrations 0004–0005.
- FS-02 provider contract: **pricing.engine@1.0.0** — 64 operations in `app/contracts/pricing/openapi.fragment.json`; migrations 0006; runtime router registered.
- Aggregate OpenAPI SHA-256: `fae9b9e497528cace379268b84bc1b5f6f659bdb5abaaa872b137fb63c96b9a1`
- Deployment state includes verified contracts: {"FS-00": ["order.demand", "order.supply", "platform.core", "platform.events", "platform.finance"], "FS-01": ["item.master"], "FS-02": ["pricing.engine"]}

## Accepted residuals (tracked, non-breaking)

1. **Contract-ownership accounting:** aggregate generator currently attributes some FS-01 implemented operations to the FS-00 fragment count (generator mapping limitation); FS-01 fragment itself carries the implementation-backed operations and provider contract test passes. Fix generator per-FS attribution in a later tooling pass.
2. **Event payload schemas:** event registry/envelope frozen; canonical `contracts/events/*` payload stubs remain TODO for item/pricing while real payload schemas live under `app/contracts/item_master/` / `app/contracts/pricing/events/`; event types/envelope/version frozen; additive completion allowed.
3. **FS-01 residual gaps** (from task-55): type-migration workflow, full inactivation referent validators, custom-field EAV wiring, matrix option-in-use guard partial, UoM deactivation guards, job handlers, data-scope filtering, durable planning-values idempotency; deferred FKs intentionally nullable.
4. **FS-02 residual gaps:** worker wiring for `pricing.level_swap`/`purchase_price_update`, canonical event payload TODO sync, effective-dated `item_price` natural-key materialisation documented in DATA-MODEL resolution.
5. Lint warning `extensions.admin` pre-existing FS-14 plan item.

## Freeze policy

FS-01/FS-02 provided contracts are additive-only within major versions (`item.master@1.0.0`,
`pricing.engine@1.0.0`). B3 (FS-03/FS-04/FS-05) may now build against the frozen 0B provider surfaces.
