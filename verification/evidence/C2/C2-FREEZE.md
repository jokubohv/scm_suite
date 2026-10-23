# C2 — Freeze record (0C provider contracts)

**Status: FROZEN** · plan v1.5.0 · 2026-10-23 · Lead approval
**Deployment:** FS-00/01/02 (0A/0B) + FS-03/04/05 (0C) all recorded `verified` in `deployment-state.dev.json`.

## Gates (logs in this directory)

| Gate | Result |
|---|---|
| Generated contracts fresh (`generate --check`) | PASS |
| Contract lint | PASS — 0 errors, 2 known warnings (`extensions.admin` FS-14) |
| Contract tests | PASS |
| Full backend suite | PASS |
| Live DB / stack | 7/7 healthy, alembic head `0012_costing_standard` |

## Frozen 0C surface

- FS-03 **traceability.registry@1.0.0** — 29 ops / 4 tables / 0007.
- FS-04 **inventory.ledger@1.0.0**, **inventory.balances@1.0.0** — 22 tables / 0008–0010; 57 paths/67 ops.
- FS-05 **costing.engine@1.0.0** — 25 tables / 0011–0012; 65 ops.
- Aggregate OpenAPI: 819 paths / 1062 operations; x-counts {"contracts": 21, "events": 159, "feature_sets": 16, "implemented_additions": 128, "implemented_operations": 320, "implemented_overrides": 192, "implemented_paths_added": 109, "operations": 1062, "paths": 819, "skeleton_operations": 742, "spec_operations": 934, "spec_paths": 710}; SHA-256 `eefc14a6006d8258ae24f4309b0522e8a40ee3eef5cdf87e574e1345d11e1906`.
- Deployment contracts: {"FS-00": ["order.demand", "order.supply", "platform.core", "platform.events", "platform.finance"], "FS-01": ["item.master"], "FS-02": ["pricing.engine"], "FS-03": ["traceability.registry"], "FS-04": ["inventory.balances", "inventory.ledger"], "FS-05": ["costing.engine"]}

## Accepted residuals (tracked, additive-completable)

1. Contract-fragment accounting: FS-01/FS-04 aggregate fragments remain partially skeleton because no per-FS builder overlay yet; their implementation-backed fragments + provider contract tests are green (`app/contracts/item_master`, `app/contracts/inventory_ledger`). Tooling follow-up, not a route/scope break.
2. FS-04 partitioning deviation (regular tables for transaction/line/detail; partition maintenance pending), simplified approval/flag/data-scope enforcement, inline import rows, serial/lot/dependent reversal checks pending.
3. FS-05 API-driven posting for T-24/T-26/T-27 transitions uses ledger PostingPort path (authoritative); document lifecycle endpoints emit events. LOT method supported by engine but FS-01 costing_method CHECK excludes LOT (cross-doc item to resolve).
4. FS-03 stock-coupled guards degrade to warnings until FS-04 strict triggers; worker handler wiring for bootstrap jobs pending.
5. Event payload schemas: registry/envelope frozen; canonical payload stubs remain TODO for some contracts while real schemas live under `app/contracts/*/events/`; additive completion allowed.
6. Flags `traceability.registry.enabled`, `inventory.ledger.enabled`, `costing.engine.enabled` deploy-dark; tenant enablement is a deployment action.

## Freeze policy

FS-03/04/05 provided contracts are additive-only within major versions. **B4 (FS-06…FS-14) may now build
against the frozen C2 provider surfaces**; deploy remains wave-ordered (1A→1F), one set at a time.
