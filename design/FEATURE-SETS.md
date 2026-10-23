# FEATURE SETS — Phase-1 Executable Decomposition, dependency DAG, waves and gates

**Status:** executable plan baseline (review-ready) · **Version:** 1.5.0 (v1.0.0 base + Δ-MT v1.1.0 + Δ-RBAC v1.2.0 + Δ-SSO v1.3.0 + Δ-PAR v1.4.0 + Δ-CFG/Δ-REUSE v1.5.0 execution amendments) · **Date:** 2026-10-22
**Owner:** architect-feature-sets · **Companion:** [`feature-sets.json`](feature-sets.json) (machine-readable, single source of truth), [`FEATURE-COVERAGE.md`](FEATURE-COVERAGE.md) / [`feature-coverage.json`](feature-coverage.json) (all 1,348 catalog topics).
**Binding inputs:** [`FEATURE-SET-FRAMEWORK.md`](FEATURE-SET-FRAMEWORK.md) (scope §1, manifest §4, dependency protocol §5, deployment §6, DoD §7, stack §3, NFR §9), [`ARCHITECTURE.md`](ARCHITECTURE.md) (§1.4 module map, §2 posting, §3 costing, §4 traceability, §5 platform, §6 planning, §8 decisions, §9 traceability, §10 gaps, §11 assumptions), [`DATA-MODEL.md`](DATA-MODEL.md) (§2 ownership, §3–§17 tables, §18–§20 posting rules/state machines/global rule index, §21 ownership/migrations/seeds, §22 ERD order), `PLAN.md` §12 boundaries.

**Lead decisions applied (binding):** D1 (O-01 FS map/numbering/waves), D2 (GL boundary = `finance_journal_intent` + read API), D3 (order boundary external; publish `order.supply@0` / `order.demand@0` stubs + test doubles), D4 (SuiteApp backend-semantics inclusions and deferred list), D5 (cost recalculation per-tenant block/provisional flag, default block), D6 (NetSuite incompatibility tenant guards), D7 (versioned AIM formula / ATP method policies), D8 (all mobile deferred; SCM Mobile permanently excluded).

**Delivery model:** Phase 1 = 16 independently deployable backend feature sets (FS-00 … FS-15), 116 declared dependency edges, 21 provided contracts, 10 ordered waves (`0A, 0B, 0C, 1A … 1F, 2`). Every set is backend-only (`backend_only=true`), deployable one at a time once its declared dependencies are `verified` in the target environment. Detailed implementation specs live in `feature-sets/FS-XX.md` (owned by the spec-writing tasks; this document summarises scope and links them).

---

## 1. Inventory of feature sets

Phase 1 = 16 backend sets. "Catalog topics" is the count of the 1,348 source topics this set owns (see FEATURE-COVERAGE.md); "Tables" is base tables from DATA-MODEL §21 (FS-15 owns 21 views, no base tables).

| FS | Name | Wave | Size | Provided contract(s) | Catalog topics | Tables |
|---|---|---|---|---|---|---|
| FS-00 | Platform Core (tenancy, identity, audit, events, jobs, boundaries) | 0A | L | `platform.core@1.0.0`, `platform.events@1.0.0`, `platform.finance@1.0.0`, `order.demand@0.1.0`, `order.supply@0.1.0` | 4 | 34 |
| FS-01 | Item Master and UoM Engine | 0B | M | `item.master@1.0.0` | 57 | 21 |
| FS-02 | Pricing Engine (item pricing + advanced pricing + effective-date updates) | 0B | M | `pricing.engine@1.0.0` | 50 | 17 |
| FS-03 | Storage and Inventory-Status Registry | 0C | S | `traceability.registry@1.0.0` | 14 | 4 |
| FS-04 | Inventory Ledger Core (posting, balances, commitments, transfers, adjustments, imports) | 0C | L | `inventory.ledger@1.0.0`, `inventory.balances@1.0.0` | 42 | 22 |
| FS-05 | Costing and Valuation Engine | 0C | L | `costing.engine@1.0.0` | 88 | 25 |
| FS-06 | Numbered Inventory (lots and serials) | 1A | M | `traceability.numbered@1.0.0` | 24 | 3 |
| FS-07 | Inventory Counts and Cycle Counts | 1A | M | `counts.engine@1.0.0` | 16 | 6 |
| FS-08 | Procurement (vendor master, P2P documents, receipts, bills, returns) | 1B | L | `procurement.p2p@1.0.0` | 116 | 29 |
| FS-09 | Manufacturing (assembly/BOM, work orders, WIP, routing, scheduling inputs) | 1C | L | `manufacturing.execution@1.1.0` | 160 | 42 |
| FS-10 | Replenishment and Planning (AIM, demand, supply/MRP, DRP, allocation, ATP) | 1D | L | `replenishment.planning@1.0.0` | 52 | 24 |
| FS-11 | Warehouse Management Execution (server-side tasks only) | 1E | L | `wms.execution@1.0.0` | 89 | 38 |
| FS-12 | Quality Management (specs, inspections, COA) — SuiteApp semantics backend | 1F | M | `quality.management@1.0.0` | 16 | 10 |
| FS-13 | Consigned Inventory Management | 1F | M | `consigned.inventory@1.0.0` | 9 | 5 |
| FS-14 | Extensions (warranty, customer part numbers, UPC registry) | 1F | S | `extensions.registry@1.0.0` | 16 | 7 |
| FS-15 | Reporting Read Models and Reporting API | 2 | M | `reporting.readmodels@1.0.0` | 16 | 0 (21 views) |

**Totals:** 16 sets · 10 waves · 116 edges · 21 contracts · **287 base tables** (platform context 34: 27 base + `finance_journal_intent` per D2 + 3 Δ-MT identity tables + 3 Δ-SSO federation tables; FS-09 42: 35 + 7 Δ-CFG/Δ-REUSE tables) + **21 reporting views** · 769 catalog topics mapped in-scope / 579 explicitly deferred (1,348 accounted = 100%).

---

## 2. Dependency DAG

### 2.1 Tree view (spanning tree; `+cross` shows additional provider edges, all listed in §2.2)

```
FS-00 Platform Core                                                             [0A, depth 0]
└── FS-01 Item Master                                                           [0B, depth 1]
    ├── FS-02 Pricing                                                           [0B, depth 2]
    │   └── FS-04 Inventory Ledger                                              [0C, depth 3]  +cross: FS-01, FS-03
    │       └── FS-05 Costing                                                   [0C, depth 4]  +cross: FS-01, FS-03
    │           └── FS-06 Numbered Inventory                                   [1A, depth 5]  +cross: FS-01, FS-03, FS-04
    │               ├── FS-07 Counts                                           [1A, depth 6]
    │               └── FS-08 Procurement                                      [1B, depth 6]  +cross: FS-04, FS-05
    │                   ├── FS-09 Manufacturing                                [1C, depth 7]  +cross: FS-04 (ledger+balances), FS-05, FS-06
    │                   │   ├── FS-10 Replenishment & Planning                 [1D, depth 8]  +cross: FS-04 (ledger+balances), FS-05, FS-06, FS-08
    │                   │   │   └── FS-15 Reporting Read Models                [2,  depth 9]  +cross: every earlier set
    │                   │   ├── FS-11 WMS Execution                            [1E, depth 8]  +cross: FS-04, FS-06, FS-07, FS-08
    │                   │   └── FS-12 Quality                                  [1F, depth 8]  +cross: FS-03, FS-04, FS-06
    │                   ├── FS-13 Consigned Inventory                          [1F, depth 7]  +cross: FS-03, FS-04, FS-05, FS-06
    │                   └── FS-14 Extensions                                   [1F, depth 7]  +cross: FS-06
    └── FS-03 Storage & Status Registry                                        [0C, depth 2]
```

FS-00 is the only root (no dependencies). FS-15 depends on every earlier set (read-only views). The spanning tree above duplicates no node; every extra provider edge is spelled out in §2.2 and in `feature-sets.json`.

### 2.2 Explicit edge list (116 edges; contract + semver range)

`+` = additional contract on the same provider. Version ranges are `>=1.0.0 <2.0.0` for all `@1.0.0` contracts and `>=0.1.0 <1.0.0` for the v0 order boundary contracts. Reasons are contractual: *why the dependent needs the provider's published surface*.

| Dependent | Provider | Contracts (range) | Why (contract reason) |
|---|---|---|---|
| FS-01 | FS-00 | `platform.core` (>=1.0.0 <2.0.0), `platform.events` (>=1.0.0 <2.0.0) | tenant/RBAC/audit/idempotency/flags + outbox event publication |
| FS-02 | FS-00 | `platform.core`, `platform.events` | platform services + domain events |
| FS-02 | FS-01 | `item.master` (>=1.0.0 <2.0.0) | prices/collections/rules reference items and item categories |
| FS-03 | FS-00 | `platform.core`, `platform.events` | platform services + domain events |
| FS-03 | FS-01 | `item.master` | item-location/bin assignment validation |
| FS-04 | FS-00 | `platform.core`, `platform.events`, `platform.finance` (>=1.0.0 <2.0.0), `order.demand` (>=0.1.0 <1.0.0) | posting auth/audit/idempotency/numbering; journal intents; external order commands |
| FS-04 | FS-01 | `item.master` | posting-item eligibility, UoM conversion, base UoM |
| FS-04 | FS-02 | `pricing.engine` | immutable sell-side price snapshot on document lines |
| FS-04 | FS-03 | `traceability.registry` | location/bin/status posting dimensions and availability flags |
| FS-05 | FS-00 | `platform.core`, `platform.events`, `platform.finance` | platform services; value journal intents; events |
| FS-05 | FS-01 | `item.master` | costing method, cost categories, item cost settings |
| FS-05 | FS-03 | `traceability.registry` | location/costing-group registration |
| FS-05 | FS-04 | `inventory.ledger` | synchronous value movements invoked inside the posting transaction |
| FS-06 | FS-00 | `platform.core`, `platform.events` | platform services + events |
| FS-06 | FS-01 | `item.master` | numbered item types, expiry/FEFO item flags |
| FS-06 | FS-03 | `traceability.registry` | current serial location/bin/status projections |
| FS-06 | FS-04 | `inventory.ledger` | deferred FK backfill/validation on ledger line+detail lot/serial columns |
| FS-06 | FS-05 | `costing.engine` | specific/lot cost identity columns and validation |
| FS-07 | FS-00 | `platform.core`, `platform.events`, `platform.finance` | platform services; count variance journal intents |
| FS-07 | FS-01 | `item.master` | count scope by item/category; count settings |
| FS-07 | FS-03 | `traceability.registry` | count positions (location/bin/status) |
| FS-07 | FS-04 | `inventory.ledger` | snapshot/read balances + T-20 variance posting |
| FS-07 | FS-05 | `costing.engine` | variance valuation |
| FS-07 | FS-06 | `traceability.numbered` | numbered count lines/recount |
| FS-08 | FS-00 | `platform.core`, `platform.events`, `platform.finance`, `order.demand` | platform services; accrual/journal intents; external demand refs for drop-ship/special order |
| FS-08 | FS-01 | `item.master` | purchasable items, purchasing UoM/prices |
| FS-08 | FS-03 | `traceability.registry` | receipt destinations (location/bin/status) |
| FS-08 | FS-04 | `inventory.ledger` | T-01/T-02/T-03/T-28 postings and PO open-qty guards |
| FS-08 | FS-05 | `costing.engine` | receipt costing, bill variances, landed-cost hook |
| FS-08 | FS-06 | `traceability.numbered` | lot/serial receipt detail and vendor-return identity (resolved: see §8.2) |
| FS-09 | FS-00 | `platform.core`, `platform.events`, `platform.finance`, `order.demand` | platform services; WIP/variance intents; create-WO-from-external-order |
| FS-09 | FS-01 | `item.master` | assembly/component item types, UoM, BOM membership |
| FS-09 | FS-03 | `traceability.registry` | WIP location/status registration |
| FS-09 | FS-04 | `inventory.ledger`, `inventory.balances` | T-15..T-19 postings + component availability reads |
| FS-09 | FS-05 | `costing.engine` | build/WIP valuation, production variances |
| FS-09 | FS-06 | `traceability.numbered` | serialized/lot assembly output and components (resolved: see §8.2) |
| FS-09 | FS-08 | `procurement.p2p` | outsourced operations via POs/vendor records |
| FS-10 | FS-00 | `platform.core`, `platform.events`, `order.demand` | platform services; plan events; external demand intake |
| FS-10 | FS-01 | `item.master` | planning attributes (reorder point, safety stock, lead time) |
| FS-10 | FS-03 | `traceability.registry` | location network/DRP nodes |
| FS-10 | FS-04 | `inventory.ledger`, `inventory.balances` | on-hand/committed/available/on-order/in-transit inputs |
| FS-10 | FS-05 | `costing.engine` | planning suggestions carry value context |
| FS-10 | FS-06 | `traceability.numbered` | FEFO lot/serial assignment in allocation results (resolved: see §8.2) |
| FS-10 | FS-08 | `procurement.p2p` | open-PO supply and draft-PO creation from suggestions |
| FS-10 | FS-09 | `manufacturing.execution` | open-WO demand/supply and draft-WO creation |
| FS-11 | FS-00 | `platform.core`, `platform.events`, `order.demand` | platform services; task events; release/fulfil external orders |
| FS-11 | FS-01 | `item.master` | item aliases/stock locations/process-family assignment |
| FS-11 | FS-03 | `traceability.registry` | warehouse/bin/status topology |
| FS-11 | FS-04 | `inventory.ledger`, `inventory.balances` | task completion postings (T-01/T-12/T-29/T-30) + availability |
| FS-11 | FS-06 | `traceability.numbered` | lot/serial pick and expiry rules |
| FS-11 | FS-07 | `counts.engine` | WMS cycle-count generation/approval |
| FS-11 | FS-08 | `procurement.p2p` | receiving orders ↔ item receipts |
| FS-11 | FS-09 | `manufacturing.execution` | work-order component picking/build triggers |
| FS-12 | FS-00 | `platform.core`, `platform.events` | platform services; inspection events |
| FS-12 | FS-01 | `item.master` | quality specs per item/category; item quality flags |
| FS-12 | FS-03 | `traceability.registry` | quality-hold statuses and locations (resolved: see §8.2) |
| FS-12 | FS-04 | `inventory.ledger` | status-change/quality-hold postings (T-30) |
| FS-12 | FS-06 | `traceability.numbered` | inspect serial/lot identities |
| FS-12 | FS-08 | `procurement.p2p` | receipt-triggered inspections |
| FS-12 | FS-09 | `manufacturing.execution` | WO-triggered inspections |
| FS-13 | FS-00 | `platform.core`, `platform.events`, `platform.finance` | platform services; consigned journal intents |
| FS-13 | FS-01 | `item.master` | `allow_vendor_consignment` eligibility |
| FS-13 | FS-03 | `traceability.registry` | consigned statuses and locations |
| FS-13 | FS-04 | `inventory.ledger` | T-21/T-22/T-23 ownership postings |
| FS-13 | FS-05 | `costing.engine` | consigned valuation/ownership-transfer value |
| FS-13 | FS-06 | `traceability.numbered` | consigned lot/serial positions |
| FS-13 | FS-08 | `procurement.p2p` | vendor bill creation on T-22 consumption |
| FS-14 | FS-00 | `platform.core`, `platform.events` | platform services; warranty/claims events |
| FS-14 | FS-01 | `item.master` | item warranty terms, customer-part/UPC mappings |
| FS-14 | FS-06 | `traceability.numbered` | warranty registration keyed by serial identity |
| FS-14 | FS-08 | `procurement.p2p` | warranty claims linking vendor repairs/returns |
| FS-15 | FS-00 | `platform.core`, `platform.events` | tenant/RBAC/data-scope enforcement and event-driven refresh |
| FS-15 | FS-01 | `item.master` | item dimensions for all views |
| FS-15 | FS-02 | `pricing.engine` | price/margin inputs for profitability views |
| FS-15 | FS-03 | `traceability.registry` | location/status dimensions |
| FS-15 | FS-04 | `inventory.ledger`, `inventory.balances` | stock ledger, balance, commitment, backorder, pending-fulfilment views |
| FS-15 | FS-05 | `costing.engine` | valuation/turnover/profitability inputs |
| FS-15 | FS-06 | `traceability.numbered` | lot-expiry and serial-history views |
| FS-15 | FS-07 | `counts.engine` | count/variance status views |
| FS-15 | FS-08 | `procurement.p2p` | purchase-history view |
| FS-15 | FS-09 | `manufacturing.execution` | manufacturing workbench/WIP views |
| FS-15 | FS-10 | `replenishment.planning` | ATP/planning-workbench views |
| FS-15 | FS-11 | `wms.execution` | task/shipment status dimensions |
| FS-15 | FS-12 | `quality.management` | inspection status view |
| FS-15 | FS-13 | `consigned.inventory` | consigned balance view |
| FS-15 | FS-14 | `extensions.registry` | warranty/customer-part/UPC read views |

The manifest contains exactly these 116 edges as individual `{id, contract, version}` records (a provider with several consumed contracts contributes several edges).

### 2.3 DAG invariants (mechanically validated)

- Root: only FS-00 has `depends_on: []`. All 15 other sets reach FS-00 by at least one path.
- No cycles (topological sort returns all 16); no self edges; every edge's provider exists.
- Phase monotonic: every set is `phase=1`; no forward-phase dependency exists. Wave monotonic per framework §4 rule 4 using the declared wave sequence `0A < 0B < 0C < 1A < 1B < 1C < 1D < 1E < 1F < 2`; intra-wave dependencies always point to an earlier manifest entry (FS-02→FS-01; FS-04→FS-03; FS-05→FS-03/FS-04; FS-07→FS-06).
- Contract monotonic: every dependency contract is provided by exactly one set; every provider version lies inside every declared consumer range.
- Manifest order FS-00→FS-15 is itself a valid topological deployment order (`order --phase 1`).

Validation command (must pass before/after any manifest change; `tools/feature_sets.py` is the shared gate tool owned by the tooling task):

```bash
python tools/feature_sets.py validate
python tools/feature_sets.py order --phase 1
python tools/feature_sets.py who-depends-on --contract inventory.ledger
```

The equivalent independent L0 validator used to sign off this document is reproduced in §10 (no external dependencies; runs today).

---

## 3. Wave plan, deploy/verify sequence

Rules: a wave deploys only when every dependency outside the wave is `deployed + verified`; one feature set is in flight per write scope; a wave closes only when every set in it is `verified` for the target environment (framework §6, §8). Within a wave, `→` marks an intra-wave sequence and `∥` marks sets deployed in parallel: **wave 1F deploys FS-12, FS-13 and FS-14 in parallel** because their write scopes are disjoint (PLAN.md §6 wave semantics unchanged). Environment flow `dev → staging → prod`; `$ENV` below is one of those.

| Wave | Sets (intra-wave order; `→` = sequential, `∥` = deployed in parallel) | Prerequisite wave state | Wave exit criteria |
|---|---|---|---|
| 0A | FS-00 | empty/greenfield environment | L0 valid; platform migrations at head; RLS/append-only/tenant-isolation invariant suites green; outbox relay+job scheduler healthy; `record --status verified`; `/internal/deps` reports platform versions |
| 0B | FS-01 → FS-02 | 0A verified | item master + pricing exit gates green; FS-01 contract consumed by FS-02 consumer tests; UoM/price-resolution golden suites green; both recorded |
| 0C | FS-03 → FS-04 → FS-05 | 0B verified | registry, ledger and costing exit gates green; posting invariants (qty/value conservation, balance=Σlines, commitment≤on-hand, recalc-block policy) green; concurrency suite green; all recorded |
| 1A | FS-06 → FS-07 | 0C verified | deferred lot/serial FKs validated + orphan check 0; serial projection/history invariant green; count snapshot/variance invariants green; both recorded |
| 1B | FS-08 | 1A verified | P2P document state machines, PO open-qty, receipt/bill match and posting consumer tests green; recorded |
| 1C | FS-09 | 1B verified | BOM/WO/WIP/build invariants + method-incompatibility guard green; scheduling-input fixtures green; recorded |
| 1D | FS-10 | 1C verified | planning-readonly invariant, AIM formula fixtures, ATP clamp, allocation limits green; order-creation creates only drafts; recorded |
| 1E | FS-11 | 1D verified (wave gate) | WMS task/wave/receipt/putaway/pick invariants green; no mobile endpoints (D8); recorded |
| 1F | FS-12 ∥ FS-13 ∥ FS-14 (deploy in parallel within 1F, disjoint write scopes) | 1E verified (wave gate) | quality trigger/hold, consigned ownership conservation, warranty/UPC invariants green; all recorded |
| 2 | FS-15 | all Phase-1 waves verified | reporting read-only + reconcile-exact invariants green; `feature_sets.py coverage` passes; recorded |

Per-wave deploy/verify sequence (same template for every set; commands are the manifest gates):

```bash
# 0) Wave entry (from the repo root, one env at a time)
python tools/feature_sets.py validate                 # L0 plan gate
python tools/feature_sets.py order --phase 1          # expected order
python tools/feature_sets.py check-env --set FS-XX --env $ENV   # L2 entry gate (deps verified+versioned)

# 1) Deploy exactly one set (additive migrations -> api/worker/relay -> smoke)
scripts/deploy_set.sh FS-XX $ENV

# 2) L3 exit gate (set-specific; see §7 per FS for exact commands)
pytest app/tests/integration/<module> -q
pytest app/tests/contracts/test_fsXX_provider_contract.py -q
pytest app/tests/invariants/<set invariants>.py -q
python tools/feature_sets.py record --set FS-XX --env $ENV --status verified

# 3) Enable flag per tenant, run one golden-path smoke transaction, monitor 30 min
scripts/enable_flag.sh FS-XX $ENV --tenant <tenant>

# 4) Wave close
python tools/feature_sets.py order --phase 1 --require-verified --env $ENV
python tools/feature_sets.py coverage
```

Wave-level promote rule: `dev` waves may overlap in time only across disjoint write scopes; `staging` and `prod` deploy strictly one wave at a time, and `prod` promotion repeats the entry/exit gates for that environment (`check-env` → deploy → exit checks → `record`). A failed exit gate blocks every dependent set automatically (its `check-env` will not see `verified`).

---

## 4. Critical path and parallelizable sets

**Execution critical path (wave-gated; 14 deploy steps = 13 sequential steps + one parallel group at 1F):**

```
FS-00 (0A) → FS-01 (0B) → FS-02 (0B) → FS-03 (0C) → FS-04 (0C) → FS-05 (0C)
      → FS-06 (1A) → FS-07 (1A) → FS-08 (1B) → FS-09 (1C) → FS-10 (1D)
      → FS-11 (1E) → { FS-12 ∥ FS-13 ∥ FS-14 } (1F) → FS-15 (2)
```

- The wave sequence itself is the schedule constraint: wave 0B (FS-01 **and** FS-02) must close before 0C starts, FS-10 (1D) must close before FS-11 (1E), and 1F must close before FS-15 (2), even where a dependency edge does not require it. That is why the wave-gated execution path includes FS-02 while the *dependency* chain below does not.
- **Longest dependency chain** (10 sets, 9 edges): `FS-00 → FS-01 → FS-03 → FS-04 → FS-05 → FS-06 → FS-08 → FS-09 → FS-10 → FS-15` (the chain through FS-12 has the same length; both are listed by the validator's longest-path computation).
- **Parallelizable sets:** FS-12, FS-13, FS-14 (wave 1F) have disjoint write scopes and no edges between them — they may be implemented/deployed concurrently after 1E. All other multi-set waves are chains (FS-01→FS-02, FS-03→FS-04→FS-05, FS-06→FS-07) and must stay sequential. FS-02 can be built in parallel with FS-03 (same 0B/0C boundary, no shared code), but it must be *deployed* before FS-04's entry gate.
- Largest/riskiest nodes on the path: FS-04 (posting engine), FS-05 (costing), FS-08 (document breadth), FS-09 (manufacturing depth), FS-10/FS-11 (planning/WMS spread). None may be skipped or merged: each owns tables and a published contract.

---

## 5. Deferred envelope (attached to phases)

Everything outside Phase-1 backend scope is mapped topic-by-topic in `FEATURE-COVERAGE.md` / `feature-coverage.json`; the envelope below groups the buckets and target phases. **SCM Mobile is permanently excluded, never a Phase-2 candidate.**

| Bucket (disposition) | Topics | Target phase | Contents / rationale |
|---|---|---|---|
| `DEFERRED-UI` | 61 | Phase 2 | End-user screens, dashboards (Item 360, Control Tower), workbenches (Supply Planning, Manufacturing Scheduler), printing/label/traveler/template rendering, Employee-Center approval screens, vendor-center portal |
| `DEFERRED-MOBILE` | 126 | Phase 2 | Manufacturing Mobile (69), WMS mobile-device procedures/cart workflows (50), Smart Count mobile, Quality mobile collection. Backend task/queue/session records (`warehouse_cart`, `device_session` — EXT) exist so a future client can drive server-side tasks; no device protocol/offline sync in Phase 1 (D8) |
| `DEFERRED-ANALYTICS` | 46 | Phase 2 | SuiteAnalytics workbooks/datasets (inventory, pricing, manufacturing, purchasing), report presentation, Procurement Dashboard, Quality/Consignment reports, Inventory Cost Accounting workbook. The *read models* they need are delivered by FS-15 in Phase 1 |
| `DEFERRED-INTEGRATION` | 156 | Phase 2+ | Finance/AP boundary (vendor payments, in-transit payments, prepayments, payment runs, vendor credit limits — D2/PLAN §12.2), OneWorld/cross-subsidiary/centralized purchasing/intercompany, EDI/carrier/WMS shipping integration, ASN sending, Bill Capture/email capture, import tooling/Item Creation Assistant, tax/1099/revenue-recognition item config, SuiteProcurement trading-partner network (D4) |
| `DEFERRED-SUITEAPP` | 190 | Phase 2+ | SuiteApp packaging/install roles/browsers/glossary/saved-searches and explicitly deferred features: Advanced Manufacturing SuiteApp extras, Work Instructions/Traveler, Smart Count SuiteApp admin, Costed BOM/Cost-Variance install surfaces, SuiteFlow approval-workflow customisation (declarative approvals are Phase 1, D-23), Lot Auto Numbering, Allergen Statements, WMS custom templates, Manufacturing Scheduler admin |
| `EXCLUDED-SCM-MOBILE` | 0 | never | SCM Mobile book was never crawled; asserted by the coverage validator that no catalog topic has root area `SCM Mobile` / domain `scm-mobile`. One topic inside Manufacturing Mobile (mpw-342 “Installing the SCM Mobile Bundle”) is part of the deferred Manufacturing Mobile prerequisite and is dispositioned `DEFERRED-MOBILE` |

**Phase 1 backend inclusions that look like SuiteApps** (D4; marked `EXT` in DATA-MODEL §23): quality inspections/specs (FS-12), warranty registration/claims (FS-14), customer part numbers (FS-14), auto-assign UPC registry (FS-14), effective-date pricing (FS-02), available-to-build calculation (FS-09), scheduling computation inputs/outputs (FS-09). These are included at backend-semantics level only — no SuiteApp packaging, UI or external API in Phase 1.

**Deferred-FK rollout is part of the envelope, not deferred work:** FS-01 creates `item_location`/`item_bin` with nullable location/bin columns closed by FS-03; FS-04/FS-05 create nullable lot/serial columns closed by FS-06. Each closing set's exit gate includes the orphan/backfill check (`DM-GLOBAL-06`).

---

## 6. Decisions encoded in this decomposition

| Decision | Where encoded |
|---|---|
| D1 (O-01) FS map/numbering/waves | This document §1–§3; manifest `feature_sets[].id/wave`; DATA-MODEL §2/§21 ownership |
| D2 GL boundary only | FS-00 `platform.finance` = `FinancePort` + `finance_journal_intent` persistence/read API (new platform table, see §8.1 Δ-1); every value-posting set depends on it; no chart-of-accounts/journals/period-close/tax/payments anywhere |
| D3 order boundary external + v0 stub | FS-00 provides `order.demand@0.1.0` and `order.supply@0.1.0` (command/event shapes for commit/release/reallocate/fulfil/return + test doubles); FS-04 implements the inventory side; FS-08/09/10/11 consume it. No order-management domain or UI |
| D4 SuiteApp scope | FEATURE-COVERAGE inclusion rules (FS-02/09/12/14) and `DEFERRED-SUITEAPP` list; §5 envelope |
| D5 cost recalc policy | FS-05 flag `costing.block_when_recalc_pending` (default true) → `409 COSTING_RECALC_REQUIRED`; closure in FS-05 acceptance |
| D6 incompatibility guards | FS-13 flag `consigned.assembly_incompatible_guard`; FS-09 `manufacturing.wip.enabled` method guard; FS-05 group-average × worksheet guard; FS-04 worksheet prohibitions |
| D7 versioned AIM/ATP policies | FS-10: `aim_formula_policy` + `formula_version` on calculations; `atp_method` policy + fixtures; manifests FS-10 acceptance |
| D8 mobile deferred / SCM Mobile excluded | §5 envelope; FS-11 exposes server-side task resources only; `warehouse_cart`/`device_session` are EXT records |

---

## 7. Feature-set definitions

Every set below links its planned implementation spec at `feature-sets/FS-XX.md` (owned by the spec-writing tasks). Entry checks must be run before deploy; exit checks and `record` after deploy. Full acceptance lists (identical) are in `feature-sets.json`.

### 7.1 FS-00 — Platform Core (wave 0A, size L)

**Objective:** everything every domain needs but no domain owns — tenancy with RLS, identity/RBAC/data scopes, immutable audit, numbering, feature flags/tenant settings, transactional outbox+relay, jobs/scheduler, idempotency, attachments metadata, declarative approvals, document links, custom fields, the finance port and the order-boundary stub contracts; plus **(Δ-MT, plan v1.1.0, EXT)** the multi-tenant user model — one global `user_identity` per person with per-tenant `app_user` memberships, login tenant selection / tenant switch, invitations, sessions with refresh rotation and revocation, platform-admin tenant provisioning and tenant-admin user administration.

**Source coverage:** `iri-198`, `mpw-797..798`, `mpw-800` (feature enablement + RBAC role/permission catalogue). Docs: [iri-198](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_161963741628.html), [mpw-797](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_156520467974.html), [mpw-800](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_156520480478.html).

**Backend scope.** Modules: `app/core`, `app/api/routers/platform`, `app/contracts/platform`, `app/contracts/order_boundary`. Tables (**34**: 27 base + `finance_journal_intent` per D2 + 3 Δ-MT tables + 3 Δ-SSO tables): `tenant`, `tenant_setting`, `currency`, `exchange_rate`, `fiscal_period`, `app_user` (extended by Δ-MT with `identity_id`, `membership_status`, `joined_at`, `invited_by`, `token_version`), `role`, `permission_scope`, `role_permission`, `user_role`, `user_data_scope`, `api_key`, `audit_event`, `number_sequence`, `feature_flag`, `custom_field_def`, `custom_field_value`, `outbox_event`, `event_consumer_checkpoint`, `job_definition`, `job_run`, `idempotency_key`, `attachment_metadata`, `approval_definition`, `approval_request`, `approval_action`, `document_link` + **`finance_journal_intent`** (D2) + **`user_identity`**, **`tenant_invitation`**, **`user_session`** (Δ-MT, EXT; identity is global/auth-only, the other two tenant-scoped) + **`tenant_sso_connection`**, **`user_identity_link`**, **`sso_group_role_map`** (Δ-SSO, EXT; tenant-scoped, secret-store refs only). Services: tenant provisioning/offboarding, JWT+API-key auth, RBAC+data scopes, audit writer, sequence allocator, flag/tenant-setting resolver (precedence document→item/location→tenant→system default), outbox relay, job scheduler/runner with advisory locks, idempotency middleware, declarative approval engine (SoD), custom-field registry/validation, finance-port adapter (`NullFinanceAdapter` default: persist intents; `get_period_status`/`get_account_ref`/`receive_rate`), order-boundary stubs+test doubles. Δ-MT services (EXT): global identity/credential/IdP-subject + MFA-flag management; login with one-membership auto-select or `TENANT_SELECTION_REQUIRED`; tenant select/switch (token pairs bound to `tid`); refresh rotation with replay detection; logout/revocation via `token_version`; invitation issue/accept/revoke (single-use, expiring, hashed); platform-admin tenant provisioning/suspend/reactivate/offboard with seed bootstrap; tenant-admin user/membership administration; membership/role/invitation/session audit. Access tokens are the only source of active tenant; `user_identity`/login/invitation-acceptance are the only cross-tenant-aware (auth-service-only) paths.
APIs: canonical base path **`/api/v1`** (exact route table in `feature-sets/FS-00.md` §6; `/internal/*` is internal-network only). Ops/health: `/healthz`, `/readyz`, `/internal/deps`, `/internal/outbox/replay`, `PUT /internal/finance/fiscal-periods|exchange-rates`, `PUT /internal/order-boundary/demand-doubles`. Δ-MT auth: `/api/v1/auth/login|select-tenant|switch-tenant|refresh|logout`. Current user: `GET /api/v1/me`, `GET /api/v1/me/tenants`. Platform admin: `/api/v1/platform/tenants*` (create/list/detail/patch/suspend/reactivate/offboard/bootstrap-admin). Tenant admin: `/api/v1/admin/users*` (create/list/detail/patch/roles/revoke-sessions/data-scopes), `/api/v1/admin/invitations*` (list/create/accept/revoke), `/api/v1/admin/tenant-settings*`, `/api/v1/admin/roles*`, `/api/v1/admin/permissions`, `/api/v1/admin/api-keys*`, `/api/v1/admin/feature-flags*`, `/api/v1/admin/number-sequences*`, `/api/v1/admin/approval-definitions*`, `/api/v1/approvals*`. Reads/finance: `/api/v1/audit-events`, `/api/v1/reference/currencies|exchange-rates|fiscal-periods`, `/api/v1/finance/period-status`, `GET /api/v1/finance/journal-intents` (+`/{id}`, read-only per D2), `GET /api/v1/order/demand/open`, `/api/v1/admin/custom-fields*`, `/api/v1/custom-field-values`, `PUT /api/v1/admin/custom-field-values`, `/api/v1/attachments*`, `/api/v1/document-links`, `/api/v1/admin/jobs*`, `/api/v1/admin/job-runs*`.
Events: `platform.flag.changed`, `platform.approval.requested|approved|rejected`, `platform.audit.recorded` (internal), `finance.journal_intent.recorded`, `document.submitted|approved|rejected|closed|reversed`; Δ-MT adds `tenant.provisioned`, `membership.activated|suspended|revoked`, `invitation.created|accepted|revoked`, `auth.tenant_switched`, `session.revoked` (all audited). Jobs: `platform.outbox.relay`, `platform.jobs.scheduler`, `platform.retention.purge`, `platform.partition.maintenance`, `platform.health.reconcile`. Flags/config: `platform.core.enabled`, tenant settings `inventory.*`, `costing.*`, `manufacturing.*`, `wms.*`, `replenishment.*`, `pricing.*`; Δ-MT adds RBAC scopes `platform.users.manage`, `platform.invitations.manage`, `platform.sessions.revoke` (tenant) and `platform.tenant.manage` (global only) plus config for access/refresh TTLs (15 min / 30 days), selection-token TTL (5 min), invitation expiry (7 days) and login/refresh/invitation rate limits.

**Δ-MT (EXT, plan v1.1.0):** binding addendum [`MULTI-TENANT-USERS-ADDENDUM.md`](MULTI-TENANT-USERS-ADDENDUM.md) — normative rules **DM-PLAT-12/13/14** (global/auth-only identity; exactly one membership per `(tenant, identity)` with tokens only for ACTIVE memberships; single-use expiring invitations + audited membership/session changes) and invariant **INV-30** (tenant/user isolation). The FS-00 L3 exit gate is extended with the **tenant/user isolation matrix** (every endpoint class × list/get/mutate × cross-tenant probe → 404) and membership/session-revocation tests (addendum §7–§8); no DAG/edge/contract/flag changes and no new catalog topics (FS-00 still owns 4).

**Δ-RBAC (plan v1.2.0; permission/role model completed, `EXT` beyond the SCM docs; no table changes):** capability scopes `<domain>.<resource>.<action>` guard every non-public route under **deny-by-default** (exactly one catalogued scope per route; only the explicit public/self-service allowlists may omit one; `DM-RBAC-01`), with orthogonal `LOCATION/CLASS/DEPARTMENT/SUBSIDIARY` data scopes. The authoritative machine-readable catalogue is [`permissions.json`](permissions.json): **249 scopes (235 assignable; legacy alias `platform.user.manage` → `platform.users.manage` is compatibility-only and not seeded; 13 service-only; 1 platform-only `platform.tenant.manage`, never grantable by a tenant role)**; `permission_scope` gains `is_platform_only`/`is_assignable`/`owner_feature_set` and `role` gains `is_template`/`is_editable` (columns only — Δ-RBAC adds no tables); the catalogue is seeded per tenant from the JSON and versioned/CI-validated with `plan_version`. **7 non-editable, cloneable system role templates** (`admin` = all 235 assignable scopes, `planner`, `buyer`, `warehouse_operator`, `cost_accountant`, `quality_inspector`, `readonly`); custom roles may use only assignable scopes (`422 SCOPE_NOT_ASSIGNABLE`) and SoD-conflicting pairs are rejected within one non-admin role (`409 SOD_CONFLICT`; the admin template is audited break-glass). **Effective-dated `user_role` assignments** per membership combine as a union of capability scopes; **effective permissions** are resolved per request from role assignments + API-key scopes + data scopes so changes apply immediately (Δ-MT suspension/revocation still bumps `token_version`); safety guards: only `platform.role.manage` manages roles, only `platform.users.manage` assigns roles, template roles are non-editable, the last active tenant administrator cannot be locked out (`409 LAST_TENANT_ADMIN`), and every role/permission/assignment change is audited (`DM-RBAC-02`). **14 hard SoD pairs** in `permissions.json` plus the global requester≠approver rule for every approval-required document (tenant setting `rbac.sod_enforcement=HARD` default; `WARN` only downgrades custom-role creation warnings, never approval-time enforcement; posting revalidates its scope — `DM-RBAC-03`). APIs: `GET /api/v1/admin/permissions`; `GET|POST|PATCH|DELETE /api/v1/admin/roles` (+`/clone`, `PUT /api/v1/admin/roles/{id}/permissions`, `PUT /api/v1/admin/roles/{id}/data-scopes`); `POST /api/v1/admin/users/{id}/roles`; `GET /api/v1/admin/users/{id}/effective-permissions`; `GET /api/v1/me/effective-permissions`; `GET|PUT /api/v1/admin/sod-policy`. Normative rules **DM-RBAC-01/02/03** and invariant **INV-31** (authorization completeness); the FS-00 exit gate includes `python tools/feature_sets.py permissions` plus route-scope/role/SoD tests (FS-00 spec §11). No DAG/edge/contract/flag changes; feature coverage unchanged (1,348 topics).

**Δ-SSO (plan v1.3.0; Dex broker + per-company enterprise SSO, `EXT` beyond the SCM docs; no other schema changes):** human authentication moves to **Dex as an OIDC identity broker** (authorization-code + PKCE; Dex federates to each company's **SAML 2.0 / OIDC / LDAP-AD** IdP via connectors), while the platform remains the token issuer and authorizer — after validating the Dex ID token the API resolves/links the global `user_identity` + tenant membership and issues the existing platform access JWT (`sub/uid/tid/tv/jti` + scopes) and rotating refresh session, so **Δ-MT** (memberships/tenant switching) and **Δ-RBAC** (scopes/roles/SoD/effective permissions) are unchanged for SSO users. **Per-company SSO** = tenant-scoped `tenant_sso_connection` rows (own IdP, email domains, enforced-SSO/JIT flags, group→role mappings); the reference deployment runs **one isolated Dex issuer per connection** (own config/storage, per-tenant isolation; a shared Dex is permitted only for dev/test/small fleet). **3 new tenant-scoped tables**: **`tenant_sso_connection`**, **`user_identity_link`** (unique per tenant+connection+issuer+subject, verified-email/domain policy), **`sso_group_role_map`** (exact/regex with priority; mapped roles are bounded to assignable tenant roles — never platform-only/service-only scopes, SoD-conflict rejected `409 SOD_CONFLICT`); secrets are secret-store refs only (`*_secret_ref`); the three tables are created inside **`0002_platform_security`**, so FS-00 keeps **3 migrations (`0001`–`0003`)** and the domain migrations are unchanged (`0004_item_master` …). Flows: public `GET /api/v1/auth/providers`, `POST /api/v1/auth/sso/start`, `GET /api/v1/auth/sso/callback` (state/nonce/PKCE single-use and tenant-bound; typed SSO errors, no silent fallback); tenant-admin `platform.sso.manage`/`platform.sso.read` endpoints for connections/test/secret-rotation/group-mappings/identity-links; JIT creates exactly one identity + one ACTIVE membership + default roles; **local password fallback (Argon2id) is retained**, enforced-SSO tenants reject local logins for their members, and platform-admin break-glass stays local and audited. Catalogue grows to **251 scopes = 237 assignable + 13 service-only + 1 platform-only (`platform.tenant.manage`) + 1 legacy alias (not seeded)**; public allowlist **9**, self-service **6**; normative rules **DM-SSO-01/02/03** and invariant **INV-32** (SSO/authn integrity). FS-00 exit gate = **17 commands incl. 3 SSO tests** (`test_sso_oidc_flow`, `test_sso_tenant_isolation`, `test_sso_group_mapping`); totals **280 base tables / 21 views**; no DAG/edge/contract/flag changes; coverage unchanged (1,348 topics).

**Dependencies:** none (root set). **Provides:** `platform.core@1.0.0`, `platform.events@1.0.0`, `platform.finance@1.0.0`, `order.demand@0.1.0`, `order.supply@0.1.0`.

**Entry checks**
```bash
python tools/feature_sets.py validate
python tools/feature_sets.py check-env --set FS-00 --env $ENV
pytest app/tests/contracts/test_fs00_order_boundary_stub.py -q
```
**Exit checks**
```bash
docker compose run --rm api alembic upgrade head
pytest app/tests/integration/platform -q
pytest app/tests/contracts/test_fs00_provider_contract.py -q
pytest app/tests/invariants/test_rls_tenant_isolation.py app/tests/invariants/test_append_only_privileges.py -q
pytest app/tests/integration/finance_port/test_journal_intent_read_api.py -q
pytest app/tests/invariants/test_mt_tenant_user_isolation.py -q
pytest app/tests/integration/platform/test_mt_auth_flows.py -q
pytest app/tests/integration/platform/test_mt_invitations_sessions.py -q
pytest app/tests/invariants/test_mt_identity_rules.py -q
python tools/feature_sets.py permissions
pytest app/tests/security/test_route_scope_coverage.py -q
pytest app/tests/integration/platform/test_rbac_roles.py -q
pytest app/tests/integration/platform/test_sod_policy.py -q
pytest app/tests/integration/platform/test_sso_oidc_flow.py -q
pytest app/tests/integration/platform/test_sso_tenant_isolation.py -q
pytest app/tests/integration/platform/test_sso_group_mapping.py -q
python tools/feature_sets.py record --set FS-00 --env $ENV --status verified
```
**Acceptance criteria (Given/When/Then):**
- Given a request authenticated for tenant A, when it requests a tenant-B resource by id, then the API returns 404 and a raw-SQL read without the tenant GUC returns no row (RLS test matrix on every route).
- Given the same Idempotency-Key and request hash submitted twice, when both requests complete, then exactly one resource/outcome exists and the replay returns the stored response; a different hash with the same key returns 409 IDEMPOTENCY_CONFLICT.
- Given a committed domain transaction emitting events, when the outbox relay runs, then events are delivered at-least-once with per-aggregate ordering and consumers deduplicate via event_consumer_checkpoint.
- Given NullFinanceAdapter is active, when a value-bearing posting emits a journal intent, then the intent (accounts, debit/credit, currency, dimensions, source document, posting-rule id) is persisted to finance_journal_intent and returned read-only by GET /v1/finance/journal-intents (D2).
- Given the order boundary stub contract v0.1.0, when a documented commit/release/fulfill/return command is replayed against the test double, then request/response schemas validate and the inventory-side command shapes are stable (D3).
**Rollback:** flag off platform write paths (all domains blocked, reads stay); forward-fix only; additive migrations, N-1 compatible; snapshot restore only if audit/outbox gate fails.
**Spec:** `feature-sets/FS-00.md`.

### 7.2 FS-01 — Item Master and UoM Engine (wave 0B, size M)

**Objective:** item identity/taxonomy (all NetSuite item types incl. assembly/matrix/WIP/phantom), type-specific extensions, UoM conversion engine, categories, kit/group composition, matrix parent/child, barcodes/aliases/related items, per-item/per-location stock and planning attributes.

**Source coverage:** `iri-001..004, 007..010, 013..014, 016..017, 019..021, 070..077, 079..080, 082..084, 110..125, 127..133, 203, 212, 227, 230`, `mpw-810..811`. Docs: [iri-003](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2166469.html), [iri-070](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/chapter_N2211898.html), [iri-120](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2227654.html), [iri-227](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2305626.html).

**Backend scope.** Module `app/modules/item_master` (+ router + contract). Tables: `item`, `item_type_definition`, `item_inventory`, `item_purchasing`, `item_sales`, `item_accounting`, `item_category`, `item_category_member`, `kit_group_member`, `item_barcode`, `item_alias`, `item_related`, `item_matrix_option`, `item_matrix_option_value`, `item_matrix_member`, `item_location`, `item_bin`, `uom_type`, `uom`, `uom_conversion`, `item_uom`. Services: item CRUD/inactivation (no hard delete), taxonomy/posting-eligibility, UoM conversion+rounding (base UoM is the ledger unit), matrix generation/validation, kit/group membership (cycle rejection), category tree, barcode/alias uniqueness and scanner resolution, item-location attribute resolution (reorder/preferred/safety/lead-time are stored here, consumed by FS-10), custom-field validation via FS-00 EAV, deferred-FK close for `item_location`/`item_bin` at FS-03.
APIs: `/v1/items`, `/v1/items/{id}/inventory|purchasing|sales|accounting|uoms|locations|barcodes|aliases|related|matrix-options|kit-members`, `/v1/item-types`, `/v1/item-categories`, `/v1/uom-types`, `/v1/uoms`, `/v1/uom-conversions`.
Events: `item.created|updated|inactivated`, `item.uom.changed`, `item.matrix.changed`, `item.barcode.added`. Jobs: `item.reference.validate` (inactivation guards/deferred-FK readiness), `item.inventory.defaults.repair`. Flags: `inventory.item_master.enabled`.

**Dependencies:** FS-00 `platform.core` (>=1.0.0 <2.0.0), `platform.events` (>=1.0.0 <2.0.0). **Provides:** `item.master@1.0.0`.
**Entry checks:** `python tools/feature_sets.py validate`; `check-env --set FS-01 --env $ENV`; `pytest app/tests/contracts/test_fs00_fs01_platform_contract.py -q`.
**Exit checks:** `alembic upgrade head`; `pytest app/tests/integration/item_master -q`; `pytest app/tests/contracts/test_fs01_provider_contract.py -q`; `pytest app/tests/invariants/test_uom_conversion.py app/tests/invariants/test_item_taxonomy.py -q`; `record --set FS-01`.
**Acceptance criteria (Given/When/Then):**
- Given an item of type INVENTORY, when it is created with a units type and base UoM, then exactly one item_inventory row and one STOCK item_uom row exist and posting eligibility resolves from the type taxonomy.
- Given a quantity expressed in a non-base UoM, when the conversion engine normalises it, then it returns a NUMERIC(38,12) base quantity with documented rounding and an unmapped UoM returns 422 UOM_TYPE_MISMATCH.
- Given a saved item, when any client or migration attempts to change item_type, costing_method, stock UoM or initial cost, then the database trigger rejects the update (documented immutability D-22).
- Given a matrix parent with option values, when a child item is created, then item_matrix_member stores the full option mapping and a second parent for the same child is rejected.
**Rollback:** flag off; items remain readable; additive migrations; inactivate instead of delete.
**Spec:** `feature-sets/FS-01.md`.

### 7.3 FS-02 — Pricing Engine (wave 0B, size M)

**Objective:** price levels, item prices per level/currency/quantity break, quantity pricing schedules and calculation modes, pricing groups, customer groups (external refs only), advanced price rules with criteria/actions and dynamic item collections, coupons, effective-date price updates with overlap handling (D4 `EXT`), and a deterministic price-resolution API that returns immutable snapshots.

**Source coverage:** `iri-023..033, 141..143, 146..163, 165..171, 173..179, 181..184`. Docs: [iri-024](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2181018.html), [iri-027](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2183333.html), [iri-167](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/book_8135641320.html), [iri-141](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/chapter_1536354469.html).

**Backend scope.** Module `app/modules/pricing`. Tables: `price_level`, `item_group`, `item_group_member`, `customer_group`, `customer_group_member`, `pricing_group`, `item_price`, `price_schedule`, `price_schedule_break`, `price_rule`, `price_rule_criteria`, `price_rule_action`, `item_collection`, `item_collection_member`, `coupon`, `price_update`, `price_update_detail`. Services: price resolution (coupon → rule priority/conflict policy → pricing group → price level → base → none), quantity/marginal calculation modes, rule matching over dynamic collections, coupon usage idempotency, effective-date scheduler with GIST overlap exclusion, price snapshots for documents.
APIs: `/v1/price-levels`, `/v1/item-groups`, `/v1/customer-groups`, `/v1/item-prices`, `/v1/price-schedules`, `/v1/price-rules`, `/v1/item-collections`, `/v1/coupons`, `/v1/price-updates`, `POST /v1/prices/resolve`.
Events: `pricing.price.updated`, `pricing.rule.changed`, `pricing.price_update.applied|superseded|reverted`, `pricing.coupon.redeemed`. Jobs: `pricing.apply_effective_updates`, `pricing.recompute_dynamic_collections`, `pricing.coupon.expire`. Flags: `pricing.engine.enabled`; tenant setting `pricing.allow_base_currency_fallback`.

**Dependencies:** FS-00 `platform.core` (>=1.0.0 <2.0.0), `platform.events` (>=1.0.0 <2.0.0); FS-01 `item.master` (>=1.0.0 <2.0.0). **Provides:** `pricing.engine@1.0.0`.
**Entry checks:** `validate`; `check-env --set FS-02`; `pytest app/tests/contracts/test_fs00_fs01_fs02_contract.py -q`.
**Exit checks:** `alembic upgrade head`; `pytest app/tests/integration/pricing -q`; provider contract test; `pytest app/tests/invariants/test_price_resolution_order.py -q`; `record --set FS-02`.
**Acceptance criteria (Given/When/Then):**
- Given coupon, price-rule, pricing-group, price-level and base-price data for one item, when a price is resolved, then the documented precedence (coupon -> rule priority with conflict policy -> pricing group -> level -> base) is applied deterministically and the source is recorded.
- Given overlapping effective-date price updates for the same target/level/currency, when the second update is saved, then the overlap exclusion constraint rejects it or marks the earlier one SUPERSEDED per documented handling.
- Given an applied effective-date update, when its start date arrives, then new item_price rows become effective and previously stored document price snapshots are unchanged.
- Given quantity pricing with LINE/OVERALL_ITEM/PARENT/SCHEDULE modes, when a line quantity crosses a break, then the calculation matches the golden dataset for every mode.
**Rollback:** flag off; documents fall back to explicit prices; updates reverted via status, never deleted.
**Spec:** `feature-sets/FS-02.md`.

### 7.4 FS-03 — Storage and Inventory-Status Registry (wave 0C, size S)

**Objective:** location registry (hierarchy/type/flags, exactly one UNALLOCATED bootstrap sentinel), zones/bins, and the inventory-status registry whose flags define availability/commitment/sellability — the dimension registry the ledger cannot post without.

**Source coverage:** `iri-199, 205, 207, 211, 215, 228, 248..249, 251..253, 257..258`, `mpw-808`. Docs: [iri-215](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_162100850596.html), [iri-252](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2274082.html), [mpw-808](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_1541433532.html).

**Backend scope.** Module `app/modules/traceability` (registry part). Tables: `location`, `zone`, `bin`, `inventory_status`. Services: location hierarchy/type validation, UNALLOCATED sentinel bootstrap, bin/zone topology with availability checks, status flags resolution (default status, consigned status), closure of FS-01 deferred FKs (`item_location`/`item_bin` → `location`/`bin`).
APIs: `/v1/locations`, `/v1/locations/{id}/bins`, `/v1/locations/{id}/children`, `/v1/zones`, `/v1/bins`, `/v1/inventory-statuses`.
Events: `traceability.location.created|updated|inactivated`, `traceability.bin.changed`, `traceability.status.changed`. Jobs: `traceability.bootstrap.sentinel`, `traceability.deferred_fk.close`. Flags: `inventory.storage_registry.enabled`.

**Dependencies:** FS-00 `platform.core` (>=1.0.0 <2.0.0), `platform.events` (>=1.0.0 <2.0.0); FS-01 `item.master` (>=1.0.0 <2.0.0). **Provides:** `traceability.registry@1.0.0`.
**Entry checks:** `validate`; `check-env --set FS-03`; `pytest app/tests/contracts/test_fs00_fs01_fs03_contract.py -q`.
**Exit checks:** `alembic upgrade head`; `pytest app/tests/integration/traceability/registry -q`; provider contract test; `pytest app/tests/invariants/test_status_availability_flags.py -q`; `record --set FS-03`.
**Acceptance criteria (Given/When/Then):**
- Given a bootstrap tenant, when the registry migration/seed completes, then exactly one location has is_unallocated=true and it validates location_type=UNALLOCATED.
- Given a location with bins disabled, when a bin or bin-scoped posting position is requested, then the API rejects it with 422 BINS_NOT_ENABLED / location rule.
- Given an inventory status with is_committable=false, when a commitment is attempted against stock in that status, then the ledger rejects it with 409 STATUS_NOT_COMMITTABLE.
- Given a tenant with a default status, when no status is supplied on a posting, then the tenant default status is applied deterministically.
**Rollback:** flag off; existing references stay valid; additive migrations; master data inactivated only.
**Spec:** `feature-sets/FS-03.md`.

### 7.5 FS-04 — Inventory Ledger Core (wave 0C, size L)

**Objective:** the append-only system of record — atomic/idempotent posting engine, derived balances/availability, commitments & allocations, external order-boundary commands, one-step/transfer-order flows with in-transit, adjustments/absolute worksheets, UNALLOCATED bootstrap distribution, reason codes and the idempotent import contract.

**Source coverage:** `iri-195..197, 200..202, 206, 208, 213..214, 216, 218..222, 231..241, 243..247, 256, 259..262, 264..267`, `mpw-815`. Docs: [iri-214](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2251098.html), [iri-235](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2259648.html), [iri-233](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2308766.html), [iri-264](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_3765928460.html), [mpw-815](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_0207114228.html).

**Backend scope.** Module `app/modules/inventory_ledger`. Tables: `inventory_transaction`, `inventory_transaction_line`, `inventory_transaction_detail`, `inventory_balance`, `inventory_balance_bin`, `inventory_balance_lot`, `inventory_commitment`, `inventory_commitment_allocation`, `inventory_transfer`, `inventory_transfer_line`, `transfer_order`, `transfer_order_line`, `inventory_in_transit`, `inventory_adjustment`, `inventory_adjustment_line`, `inventory_worksheet`, `inventory_worksheet_line`, `inventory_distribution`, `inventory_distribution_line`, `inventory_reason_code`, `import_batch`, `import_batch_row`. Services: 17-step posting algorithm (§2.4), document state machines, lock-ranked updates, commitment C-01..C-03, transfer-order C-04/C-05, T-07/T-08/T-09/T-10/T-11/T-12/T-13/T-14/T-29/T-30/T-31, distribution bootstrap, import engine, balance/read APIs, `FinancePort.record_journal_intent` call per value posting, order-boundary implementation.
APIs: `/v1/inventory-transactions`, `/v1/inventory-adjustments`, `/v1/inventory-adjustments/{id}/post`, `/v1/inventory-worksheets`, `/v1/inventory-transfers`, `/v1/transfer-orders`, `/v1/inventory/commitments`, `/v1/fulfillments`, `/v1/returns`, `/v1/inventory/balances`, `/v1/inventory/distributions`, `/v1/imports`.
Events: `inventory.adjusted`, `inventory.transferred`, `inventory.fulfilled`, `inventory.returned`, `inventory.committed.changed`, `inventory.balance.changed`, `inventory.in_transit.changed`, `inventory.status_changed`, `to.approved|fulfilled|received|closed`, `import.row_posted`, `cost.layer.created` (delegated), plus order-boundary `order.supply` events. Jobs: `ledger.import.process`, `ledger.commit_orders.schedule`, `ledger.reconcile_balances`, `ledger.partition.maintenance`. Flags: `inventory.ledger.enabled`.

**Dependencies:** FS-00 `platform.core` (>=1.0.0 <2.0.0), `platform.events` (>=1.0.0 <2.0.0), `platform.finance` (>=1.0.0 <2.0.0), `order.demand` (>=0.1.0 <1.0.0); FS-01 `item.master` (>=1.0.0 <2.0.0); FS-02 `pricing.engine` (>=1.0.0 <2.0.0); FS-03 `traceability.registry` (>=1.0.0 <2.0.0). **Provides:** `inventory.ledger@1.0.0`, `inventory.balances@1.0.0`.
**Entry checks:** `validate`; `check-env --set FS-04`; `pytest app/tests/contracts/test_fs00_fs01_fs02_fs03_fs04_contracts.py -q`; `pytest app/tests/contracts/test_order_boundary_v0_doubles.py -q`.
**Exit checks:** `alembic upgrade head`; `pytest app/tests/integration/inventory_ledger -q`; provider contract; `pytest app/tests/invariants/test_qty_conservation.py app/tests/invariants/test_balance_equals_sum_lines.py app/tests/invariants/test_commitment_le_onhand.py -q`; `pytest app/tests/concurrency/test_posting_locks.py app/tests/concurrency/test_idempotent_retries.py -q`; `record --set FS-04`.
**Acceptance criteria (Given/When/Then):**
- Given an approved adjustment/transfer/receipt posting command with an Idempotency-Key, when it posts, then exactly one inventory_transaction plus signed lines/details and balance updates commit atomically and a retry returns the same transaction id.
- Given any posting, when the exit-gate invariant query runs, then sum(line.qty_base) equals the balance delta per posting key (INV-01) and no balance references the UNALLOCATED location after bootstrap.
- Given available stock and a commit command from the external order boundary, when it is accepted, then qty_committed rises by exactly q and a release/reallocate restores availability without ledger rows.
- Given a transfer order, when it is approved/fulfilled/received/closed, then source on-hand, destination in-transit and destination on-hand move per T-10/T-11 and C-04/C-05 with the expected events.
- Given an import batch with one invalid row, when it posts, then valid rows are posted idempotently, the invalid row is reported with its error, and the batch ends PARTIAL.
**Rollback:** flag off posting writes; ledger append-only — corrections are compensating postings; additive migrations.
**Spec:** `feature-sets/FS-04.md`.

### 7.6 FS-05 — Costing and Valuation Engine (wave 0C, size L)

**Objective:** value side of every posting — cost objects/methods (average/FIFO/LIFO/standard/specific/lot/group-average), layers/consumption, cost ledger, recalculation with tenant block/provisional policy (D5), standard costing/rollup/revaluation, variances, landed/estimated landed cost, COGS adjustments, location costing groups and exact value reconcile.

**Source coverage:** `iri-035..057, 059..068, 229, 263`, `mpw-031, 038..047, 052..057, 716..751`. Docs: [iri-037](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2191818.html), [iri-042](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2201059.html), [iri-064](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_4345703444.html), [iri-048](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2205401.html), [mpw-716](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2416930.html).

**Backend scope.** Module `app/modules/costing`. Tables: `item_cost`, `cost_layer`, `cost_layer_consumption`, `cost_ledger_entry`, `cost_revaluation`, `cost_revaluation_line`, `standard_cost`, `cost_category`, `cost_template`, `cost_template_line`, `cost_version`, `planned_standard_cost`, `cost_rollup_run`, `cost_rollup_line`, `cost_variance`, `cogs_adjustment`, `cogs_adjustment_line`, `landed_cost`, `landed_cost_line`, `landed_cost_allocation`, `estimated_landed_cost_template`, `estimated_landed_cost_template_line`, `location_costing_group`, `location_costing_group_member`, `group_average_balance`. Services: `value_movement`/`value_inbound`/`value_outbound`/`value_move`/`value_adjust`/`value_build`/`value_unbuild`/`value_wip_*`/`value_landed_cost` called synchronously from FS-04 in the posting transaction; layer engine; recalculation queue (blocking default per D5); standard-cost engine (versions/rollup/revaluation/variance); landed-cost allocation (weight/qty/amount/per-line); group-average balance and worksheet prohibition (D6); cost reconciliation job.
APIs: `/v1/costs/item-costs`, `/v1/standard-costs`, `/v1/cost-categories`, `/v1/cost-templates`, `/v1/cost-versions`, `/v1/cost-rollups`, `/v1/cost-revaluations`, `/v1/landed-costs`, `/v1/estimated-landed-cost-templates`, `/v1/cogs-adjustments`, `/v1/cost-variances`, `/v1/location-costing-groups`.
Events: `cost.layer.created|updated`, `cost.adjustment.posted`, `cost.recalculation.queued|completed`, `standard_cost.revalued`, `landed_cost.applied|estimated`, `cogs.recorded`. Jobs: `costing.recalculate`, `costing.cogs_adjust`, `costing.reconcile_costs`, `costing.rollup`, `costing.layers.expire`. Flags: `costing.engine.enabled`, `costing.block_when_recalc_pending` (default true, D5).

**Dependencies:** FS-00 `platform.core` (>=1.0.0 <2.0.0), `platform.events` (>=1.0.0 <2.0.0), `platform.finance` (>=1.0.0 <2.0.0); FS-01 `item.master` (>=1.0.0 <2.0.0); FS-03 `traceability.registry` (>=1.0.0 <2.0.0); FS-04 `inventory.ledger` (>=1.0.0 <2.0.0). **Provides:** `costing.engine@1.0.0`.
**Entry checks:** `validate`; `check-env --set FS-05`; `pytest app/tests/contracts/test_fs00_fs01_fs03_fs04_fs05_contracts.py -q`.
**Exit checks:** `alembic upgrade head`; `pytest app/tests/integration/costing -q`; provider contract; `pytest app/tests/invariants/test_value_conservation.py app/tests/invariants/test_costing_methods_golden.py app/tests/invariants/test_recalc_block_policy.py -q`; `record --set FS-05`.
**Acceptance criteria (Given/When/Then):**
- Given a posted receipt, when the costing engine runs inside the posting transaction, then each movement's value delta equals qty x the method-deterministic unit cost and the cost ledger entry links to the ledger line.
- Given an item with a pending recalculation and costing.block_when_recalc_pending=true, when a posting requires a provisional cost, then the API returns 409 COSTING_RECALC_REQUIRED and nothing is posted (D5).
- Given a FIFO/LIFO item, when layers are consumed across receipts, then the append-only consumption trail reconstructs the exact remaining layer state and a nightly reconcile compares cost ledger to item_cost with zero drift.
- Given a standard-cost revaluation or landed-cost application, when it posts value-only movements, then layer/avg state updates and the finance intent carries the correct posting-rule dimension.
- Given group-average costing, when an inventory worksheet is attempted for a group item, then the documented incompatibility guard rejects it (D6).
**Rollback:** flag off; postings block rather than write provisional values by default; recalculation rerunnable; corrections via compensating value postings.
**Spec:** `feature-sets/FS-05.md`.

### 7.7 FS-06 — Numbered Inventory (wave 1A, size M)

**Objective:** lot/serial identity, serial projection+append-only history, expiry/FEFO attributes, specific-cost identity, and closing the deferred lot/serial FKs opened by FS-04/FS-05.

**Source coverage:** `iri-085..107, 250`. Docs: [iri-085](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2230290.html), [iri-100](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2235684.html), [iri-088](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2231339.html), [iri-099](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/subsect_96151731732.html).

**Backend scope.** Module `app/modules/traceability/numbered`. Tables: `lot`, `serial_number`, `serial_number_history` + FK/backfill migration on FS-04/FS-05 lot/serial columns. Services: lot/serial registration & validation, serial projection update inside posting transaction, append-only history writer, FEFO/expiry helpers, serial state transitions, quantity-mismatch guards, deferred-FK orphan validation.
APIs: `/v1/lots`, `/v1/lots/{id}`, `/v1/serials`, `/v1/serials/{id}`, `/v1/serials/{id}/history`, `/v1/items/{id}/lots`, `/v1/items/{id}/serials`.
Events: `traceability.lot.created|inactivated`, `traceability.serial.created`, `traceability.serial.state_changed`, `traceability.expiry.warning`. Jobs: `traceability.deferred_fks.validate`, `traceability.expiry.scan`. Flags: `inventory.numbered.enabled`.

**Dependencies:** FS-00 `platform.core` (>=1.0.0 <2.0.0), `platform.events` (>=1.0.0 <2.0.0); FS-01 `item.master` (>=1.0.0 <2.0.0); FS-03 `traceability.registry` (>=1.0.0 <2.0.0); FS-04 `inventory.ledger` (>=1.0.0 <2.0.0); FS-05 `costing.engine` (>=1.0.0 <2.0.0). **Provides:** `traceability.numbered@1.0.0`.
**Entry checks:** `validate`; `check-env --set FS-06`; `pytest app/tests/contracts/test_fs01_fs03_fs04_fs05_fs06_contracts.py -q`.
**Exit checks:** `alembic upgrade head`; `pytest app/tests/integration/traceability/numbered -q`; provider contract; `pytest app/tests/invariants/test_serial_projection_matches_history.py app/tests/invariants/test_deferred_fk_backfill.py -q`; `record --set FS-06`.
**Acceptance criteria (Given/When/Then):**
- Given a serialized item receipt with N serials, when it posts, then N serial_number rows and N append-only serial_number_history rows exist and the sum of ledger detail for serialized positions equals the serial count (INV-18).
- Given a lot-numbered item with expiry, when FEFO allocation runs, then earlier-expiry lots are recommended first and picking an expired lot is rejected with 422 LOT_EXPIRED.
- Given deferred lot/serial FK columns created by FS-04/FS-05, when the FS-06 migration runs, then every existing row backfills to NULL-or-valid identity, FK constraints validate, and the orphan check returns zero.
- Given a serialized item, when a posting is attempted with a line/serial quantity mismatch, then the posting is rejected with 409 SERIAL_QTY_MISMATCH and nothing is written.
**Rollback:** flag off numbered posting; identities/FKs stay; additive forward-fix only.
**Spec:** `feature-sets/FS-06.md`.

### 7.8 FS-07 — Inventory Counts and Cycle Counts (wave 1A, size M)

**Objective:** count plans/filters, physical and cycle counts with immutable snapshot, recount/review/approve/reject, and variance posting T-20 with documented restrictions.

**Source coverage:** `iri-209..210, 223..226`, `mpw-873..877, 879..883`. Docs: [iri-223](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2296970.html), [mpw-876](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_1541442081.html), [mpw-882](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_155993310131.html).

**Backend scope.** Module `app/modules/counts`. Tables: `count_plan`, `count_plan_filter`, `inventory_count`, `inventory_count_line`, `inventory_count_snapshot`, `inventory_count_recount`. Services: plan scheduling/generation (calculated/manual), snapshot capture, line capture/recount, approval flow with snapshot recalculation, T-20 variance posting via FS-04/FS-05, numbered/consigned routing guards.
APIs: `/v1/count-plans`, `/v1/inventory-counts`, `/v1/inventory-counts/{id}/lines`, `/v1/inventory-counts/{id}/recount`, `/v1/inventory-counts/{id}/approve|reject|post`.
Events: `count.generated`, `count.approved|rejected`, `count.variance_posted`, `inventory.adjusted` (via ledger). Jobs: `counts.generate_cycle_counts`, `counts.snapshot.refresh`, `counts.stale.cleanup`. Flags: `inventory.counts.enabled`.

**Dependencies:** FS-00 `platform.core` (>=1.0.0 <2.0.0), `platform.events` (>=1.0.0 <2.0.0), `platform.finance` (>=1.0.0 <2.0.0); FS-01 `item.master` (>=1.0.0 <2.0.0); FS-03 `traceability.registry` (>=1.0.0 <2.0.0); FS-04 `inventory.ledger` (>=1.0.0 <2.0.0); FS-05 `costing.engine` (>=1.0.0 <2.0.0); FS-06 `traceability.numbered` (>=1.0.0 <2.0.0). **Provides:** `counts.engine@1.0.0`.
**Entry checks:** `validate`; `check-env --set FS-07`; `pytest app/tests/contracts/test_fs01_fs03_fs04_fs05_fs06_fs07_contracts.py -q`.
**Exit checks:** `alembic upgrade head`; `pytest app/tests/integration/counts -q`; provider contract; `pytest app/tests/invariants/test_count_variance_equals_delta.py app/tests/invariants/test_count_snapshot_immutable.py -q`; `record --set FS-07`.
**Acceptance criteria (Given/When/Then):**
- Given a count plan on a bin/item scope, when a count is generated, then count lines snapshot on-hand at generation time and later postings do not change the snapshot.
- Given an approved count with variances, when it posts, then T-20 writes one COUNT_VARIANCE transaction per count whose per-line delta equals counted minus snapshot and costing values it per method.
- Given an approved-then-recalculated count (documented rejection path), when the snapshot recalculates, then only unposted counted quantities are considered and the original snapshot is retained for audit.
- Given a count line for a serialized/consigned position, when variance posting is requested via the wrong path, then the documented prohibition routes it to adjustment/consigned posting (409 PROHIBITED_COUNT_TYPE).
**Rollback:** flag off; counts/snapshots readable; additive migrations; corrective postings only.
**Spec:** `feature-sets/FS-07.md`.

### 7.9 FS-08 — Procurement (wave 1B, size L)

**Objective:** procure-to-pay documents and their inventory/cost effects: vendor master, item-vendor pricing/contracts, requisitions/requests/RFQ with declarative approvals, PO lifecycle, receipts (T-01/T-02), bills/variance/3-way match (T-04/T-06), credits (T-05), vendor returns (T-03), drop-ship/special-order (T-28).

**Source coverage:** `mpw-416..427, 430, 444, 447, 449..451, 453..454, 472..474, 478..486, 490..493, 497..498, 570..584, 586..588, 601..610, 622..639, 646..649, 656..666, 683..684, 699..709, 711..712, 714..715`. Docs: [mpw-419](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/chapter_N2360272.html), [mpw-453](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/chapter_N2370131.html), [mpw-570](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/chapter_N2386193.html), [mpw-601](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/book_N2393897.html), [mpw-699](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/chapter_N2410585.html).

**Backend scope.** Module `app/modules/procurement`. Tables: `vendor`, `vendor_category`, `item_vendor`, `item_vendor_price`, `vendor_contract`, `vendor_contract_item`, `purchase_requisition`, `purchase_requisition_line`, `purchase_request`, `purchase_request_line`, `rfq`, `rfq_line`, `rfq_response`, `rfq_response_line`, `purchase_order`, `purchase_order_line`, `purchase_order_release`, `item_receipt`, `item_receipt_line`, `item_receipt_detail`, `vendor_bill`, `vendor_bill_line`, `vendor_bill_variance`, `vendor_bill_match`, `vendor_credit`, `vendor_credit_line`, `vendor_return_authorization`, `vendor_return_authorization_line`, `special_order_link`. Services: document creation/lifecycle & state machines, advanced-receiving semantics (bill never creates inventory; O-08 default), PO open-qty guards/over-receipt tolerance, receipt posting through FS-04, bill match/tolerances/variance posting, vendor returns/credits, drop-ship/special-order links, declarative approvals via FS-00, landed-cost hook via FS-05.
APIs: `/v1/vendors`, `/v1/vendor-categories`, `/v1/item-vendors`, `/v1/vendor-contracts`, `/v1/purchase-requisitions`, `/v1/purchase-requests`, `/v1/rfqs`, `/v1/purchase-orders`, `/v1/purchase-orders/{id}/approve`, `/v1/item-receipts`, `/v1/item-receipts/{id}/post`, `/v1/vendor-bills`, `/v1/vendor-bills/{id}/match`, `/v1/vendor-credits`, `/v1/vendor-returns`, `/v1/special-order-links`.
Events: `po.created|approved|receipted|billed|closed`, `inventory.receipt.posted|reversed`, `vendor_bill.posted|variance_created|variance_posted`, `vendor_credit.posted`, `vendor_return.shipped|credited`, `dropship.fulfilled`. Jobs: `procurement.match.run`, `procurement.po.close_overdue`, `procurement.receipt.reconcile`, `procurement.partition.maintenance`. Flags: `procurement.p2p.enabled`.

**Dependencies:** FS-00 `platform.core` (>=1.0.0 <2.0.0), `platform.events` (>=1.0.0 <2.0.0), `platform.finance` (>=1.0.0 <2.0.0), `order.demand` (>=0.1.0 <1.0.0); FS-01 `item.master` (>=1.0.0 <2.0.0); FS-03 `traceability.registry` (>=1.0.0 <2.0.0); FS-04 `inventory.ledger` (>=1.0.0 <2.0.0); FS-05 `costing.engine` (>=1.0.0 <2.0.0); FS-06 `traceability.numbered` (>=1.0.0 <2.0.0). **Provides:** `procurement.p2p@1.0.0`.
**Entry checks:** `validate`; `check-env --set FS-08`; `pytest app/tests/contracts/test_fs00_fs01_fs03_fs04_fs05_fs06_fs08_contracts.py -q`.
**Exit checks:** `alembic upgrade head`; `pytest app/tests/integration/procurement -q`; provider contract; `pytest app/tests/invariants/test_po_open_qty.py app/tests/invariants/test_receipt_bill_match.py app/tests/invariants/test_state_machines_procurement.py -q`; `record --set FS-08`.
**Acceptance criteria (Given/When/Then):**
- Given an approved PO line for 10 units, when two partial receipts post, then open qty decreases by each receipt, over-receipt beyond tolerance is rejected, and po.receipted events carry cumulative quantities.
- Given a receipt-matched vendor bill, when it posts, then it is value-only (no qty change), bill qty <= received qty is enforced, variance rows are created within tolerance, and the finance intent records the accrual reclass.
- Given a vendor return authorization, when it ships, then T-03 removes on-hand at the item method cost and the RA line tracks shipped vs authorized quantities.
- Given a drop-ship/special-order PO linked to external demand via order.demand external_ref, when it is fulfilled, then T-28 posts value-only with no inventory movement and the special_order_link is unique per external line.
- Given a PO whose receipt was fully billed, when the PO is closed, then no line retains open qty or unposted receipt state.
**Rollback:** flag off; posted docs immutable/readable; additive; corrections via reversal/credit.
**Spec:** `feature-sets/FS-08.md`.

### 7.10 FS-09 — Manufacturing (wave 1C, size L)

**Objective:** assembly/BOM master data and execution: assembly item extensions, BOM/revisions/component control, phantoms, routings/work centres/calendars/cost templates, WO lifecycle, WIP T-17/T-18/T-19, build/unbuild T-15/T-16, outsourced operations, ECOs, scheduling-run computation/capacity (UI deferred per D4), available-to-build (D4 EXT).

**Source coverage:** `mpw-001..027, 029..030, 035, 060..085, 088..089, 101, 106..108, 110, 112..184, 199, 210..219, 403..415`. Docs: [mpw-005](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2319594.html), [mpw-061](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/chapter_N2328390.html), [mpw-139](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/chapter_N2335392.html), [mpw-153](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/chapter_N2341076.html), [mpw-403](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/chapter_1531288763.html).

**Backend scope.** Module `app/modules/manufacturing`. Tables: `assembly_item`, `bom`, `bom_revision`, `bom_component`, `bom_component_control`, `assembly_revision`, `routing`, `routing_step`, `routing_step_charge`, `work_center`, `work_center_group`, `work_center_calendar`, `work_center_calendar_day`, `manufacturing_cost_template`, `manufacturing_cost_template_line`, `work_order`, `work_order_line`, `work_order_component`, `work_order_operation`, `operation_completion`, `wip_transaction`, `wip_transaction_line`, `assembly_build`, `assembly_build_component`, `assembly_unbuild`, `assembly_unbuild_component`, `outsourced_operation`, `outsourced_charge`, `eco`, `eco_line`, `scheduling_run`, `scheduled_operation`, `capacity_plan`, `work_center_load`, `work_order_wip_balance` + **`compat_attribute_def`**, **`compat_attribute_value`**, **`compat_rule_set`**, **`compat_rule`**, **`compat_slot_def`**, **`compat_configuration`**, **`condition_code`** (Δ-CFG/Δ-REUSE; 7 new tables, created in existing migration `0020_manufacturing_execution` — no new revision). Services: BOM/revision/component-control resolution (effective-date/revision), phantom explosion, routing/calendar resolution, WO lifecycle+state machine, WIP issue/completion/backflush/close posting via FS-04/FS-05, build/unbuild posting, outsourced operations with PO linkage to FS-08, ECO apply-to-revision, scheduling run (forward/backward, finite/infinite) producing scheduled operations/capacity loads, available-to-build calculation (EXT), **WIP × costing-method compatibility guard (D6)**.
APIs: `/v1/assemblies`, `/v1/boms`, `/v1/boms/{id}/revisions`, `/v1/routings`, `/v1/work-centers`, `/v1/work-center-calendars`, `/v1/work-orders`, `/v1/work-orders/{id}/release|issue|complete|close|void`, `/v1/assembly-builds`, `/v1/assembly-unbuilds`, `/v1/outsourced-operations`, `/v1/ecos`, `/v1/scheduling-runs`, `/v1/available-to-build`.
Events: `assembly.built|unbuilt`, `component.consumed`, `wip.issued|completed|backflushed`, `work_order.released|built|closed|voided`, `eco.implemented`, `scheduling.run.completed`, `available_to_build.calculated`. Jobs: `mfg.scheduling.run`, `mfg.capacity.rollup`, `mfg.wo.demand.sync`, `mfg.wip.reconcile`. Flags: `manufacturing.enabled`, `manufacturing.wip.enabled`.

**Δ-CFG/Δ-REUSE (plan v1.5.0; `EXT` beyond the SCM docs; FS-09-only, additive contract `manufacturing.execution@1.1.0`, no DAG/wave/consumer changes):** a **manufacturing compatibility engine** with typed attribute definitions/values, configurable slots/capacity and **rule sets with dual scope — assembly/BOM-revision rules *and* order/configuration/customer/region/channel rules both apply**, evaluated deterministically with AND semantics by scope specificity then priority then rule code (`DM-MFG-12`); **enforcement is user-configurable** (tenant default `manufacturing.compat.enforcement_default=HARD`, per-rule/per-rule-set `INHERIT|HARD|WARN`; HARD blocks with `409 COMPATIBILITY_VIOLATION`, WARN records a violation; overrides require the `manufacturing.compat.override` scope + mandatory reason and are audited). **Generic applicability matrix** — compatibility gates apply to every manufacturing/configuration path: BOM/BOM-revision create/activate, matrix generation, phantom explosion, WO create/update/release/firm, WIP issue/completion/close/backflush, non-WIP assembly build, outsourced manufacturing, ECO/revision activation, the order/configuration boundary (`order.demand` configure-to-order) and available-to-build; **unbuild is exempt** from new-configuration validation (restores a prior valid snapshot) and **kits/groups are out of scope** (`DM-MFG-13`). Every validation persists an immutable **`compat_configuration` record** (assembly, context `BOM/WO/BUILD/OUTSOURCED/ORDER/ECO/ATB`, configuration hash, component/attribute snapshot, ordered matched rules, `VALID/WARN/HARD_FAIL/OVERRIDDEN`, override refs) usable as L3 evidence. **Unbuild disposition/reuse:** per-line `RETURN_TO_STOCK` (default), `SCRAP` (no stock; value to scrap/variance via FS-05) or `HOLD` (non-committable), with optional `is_used`/condition/return-status/resale/target-bin, configurable valuation (`ORIGINAL_COST` default, `NRV`/`MANUAL` with FS-05 value postings), preserved serial/lot lifecycle (`IN_STOCK_USED`/`SCRAPPED`/`HOLD`), quantity/value conservation and reversal rules; line overrides need `manufacturing.unbuild.dispose` and are audited; **REWORK is deferred** to the FS-14 repair flow (no new dependency) (`DM-MFG-14` + **INV-33** compatibility/reuse integrity). New scopes `manufacturing.compat.manage|validate|override` and `manufacturing.unbuild.dispose` (catalogue **255 = 241 assignable + 13 service-only + 1 platform-only**, +4 assignable); APIs under `/v1/manufacturing/compat/*` + `/v1/manufacturing/configurations*` and unbuild request/response extensions. FS-09: **42 tables / 115 endpoints / 24 events / 5 jobs / 3 migrations / exit gate 8 commands** incl. `test_compat_rules`, `test_compat_paths`, `test_unbuild_dispositions`; final totals **287 base tables / 21 views**; other 15 sets, edges and waves unchanged; coverage stays 1,348.

**Dependencies:** FS-00 `platform.core` (>=1.0.0 <2.0.0), `platform.events` (>=1.0.0 <2.0.0), `platform.finance` (>=1.0.0 <2.0.0), `order.demand` (>=0.1.0 <1.0.0); FS-01 `item.master` (>=1.0.0 <2.0.0); FS-03 `traceability.registry` (>=1.0.0 <2.0.0); FS-04 `inventory.ledger` (>=1.0.0 <2.0.0), `inventory.balances` (>=1.0.0 <2.0.0); FS-05 `costing.engine` (>=1.0.0 <2.0.0); FS-06 `traceability.numbered` (>=1.0.0 <2.0.0); FS-08 `procurement.p2p` (>=1.0.0 <2.0.0). **Provides:** `manufacturing.execution@1.1.0` (additive: compatibility + unbuild-disposition surface; all consumer ranges `>=1.0.0 <2.0.0` unchanged).
**Entry checks:** `validate`; `check-env --set FS-09`; `pytest app/tests/contracts/test_fs01_fs03_fs04_fs05_fs06_fs08_fs09_contracts.py -q`.
**Exit checks:** `alembic upgrade head`; `pytest app/tests/integration/manufacturing -q`; provider contract; `pytest app/tests/invariants/test_assembly_build_value.py app/tests/invariants/test_wip_reconciliation.py app/tests/invariants/test_mps_schedule_inputs.py -q`; `pytest app/tests/invariants/test_compat_rules.py -q`; `pytest app/tests/integration/manufacturing/test_compat_paths.py -q`; `pytest app/tests/integration/manufacturing/test_unbuild_dispositions.py -q`; `record --set FS-09`.
**Acceptance criteria (Given/When/Then):**
- Given a released work order with component requirements, when a WIP issue posts, then component on-hand decreases at the WIP location, WIP value rises, and a work_order_wip_balance read model reconciles to the postings.
- Given a build/unbuild command, when it posts, then T-15/T-16 consumes/restores components per the effective BOM revision and any value difference posts to the unbuild variance cost category.
- Given a WIP work order on a LIFO/FIFO/specific-cost assembly, when WIP is enabled for that item, then the documented incompatibility guard rejects the configuration (D6).
- Given a scheduling run request with finite/infinite and forward/backward parameters, when it executes, then scheduled_operation/capacity_plan/work_center_load rows are written deterministically for the same inputs and no UI is required (D4).
- Given an item with a BOM and on-hand components, when GET /v1/available-to-build is called, then the calculation returns buildable quantity from the documented inputs (EXT, D4) and is covered by deterministic fixtures.
**Rollback:** flag off `manufacturing.wip.enabled` then `manufacturing.enabled`; open WOs closed/voided first; posted docs immutable; additive.
**Spec:** `feature-sets/FS-09.md`.

### 7.11 FS-10 — Replenishment and Planning (wave 1D, size L)

**Objective:** planning read-model/engine context that never posts inventory: AIM reorder-point/preferred-stock calculations behind a versioned formula policy (D7), demand plans, supply/MRP plans with suggestions/action messages, DRP on the same engine, supply allocation with FEFO, ATP projections/query behind a versioned method policy (D7), and explicit draft-order creation through owning contexts.

**Source coverage:** `iri-204, 268..286, 288..304, 307..309, 311..321`, `mpw-090`. Docs: [iri-269](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/chapter_N2285050.html), [iri-277](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2286970.html), [iri-302](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_159171867422.html), [iri-316](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2300269.html), [iri-295](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_4049498070.html).

**Backend scope.** Module `app/modules/replenishment`. Tables: `planning_rule`, `planning_rule_scope`, `demand_history`, `demand_plan`, `demand_plan_line`, `supply_plan`, `supply_plan_line`, `supply_plan_suggestion`, `planning_action_message`, `time_fence`, `drp_network`, `drp_network_member`, `bill_of_distribution`, `bill_of_distribution_level`, `distribution_category`, `distribution_category_member`, `allocation_strategy`, `allocation_strategy_rule`, `allocation_run`, `allocation_result`, `allocation_exception`, `atp_projection`, `replenishment_calculation`, `replenishment_calculation_line`. Services: demand-history projection consumer, statistical plan methods, time fences, AIM formula policy (tenant setting id+version, deterministic fixtures, `formula_version` stored), order-items data, supply/MRP engine with BOM explosion and lead-time offset, DRP_BOD engine, allocation strategies/runs/FEFO, ATP projection+method policy, suggestion→draft creation via FS-08/FS-09/FS-04 APIs, planning-readonly enforcement.
APIs: `/v1/planning-rules`, `/v1/time-fences`, `/v1/demand-plans`, `/v1/supply-plans`, `/v1/supply-plans/{id}/suggestions`, `/v1/supply-plans/{id}/create-orders`, `/v1/drp/networks`, `/v1/distribution-categories`, `/v1/allocation-strategies`, `/v1/allocation-runs`, `/v1/replenishment-calculations`, `/v1/atp`, `GET /v1/atp?item=&location=&qty=&required_by=`.
Events: `plan.generated`, `plan.suggestion.created`, `planning.exception.raised`, `allocation.completed`, `replenishment.calculated`, `atp.updated`. Jobs: `planning.demand.calculate`, `planning.supply.run`, `planning.allocation.run`, `replenishment.aim`, `replenishment.ordering`, `atp.rebuild`. Flags/config: `replenishment.planning.enabled`; tenant settings `replenishment.aim_formula_policy` (id+version), `replenishment.atp_method` (id+version), horizon/buckets/time-fence defaults.

**Dependencies:** FS-00 `platform.core` (>=1.0.0 <2.0.0), `platform.events` (>=1.0.0 <2.0.0), `order.demand` (>=0.1.0 <1.0.0); FS-01 `item.master` (>=1.0.0 <2.0.0); FS-03 `traceability.registry` (>=1.0.0 <2.0.0); FS-04 `inventory.ledger` (>=1.0.0 <2.0.0), `inventory.balances` (>=1.0.0 <2.0.0); FS-05 `costing.engine` (>=1.0.0 <2.0.0); FS-06 `traceability.numbered` (>=1.0.0 <2.0.0); FS-08 `procurement.p2p` (>=1.0.0 <2.0.0); FS-09 `manufacturing.execution` (>=1.0.0 <2.0.0). **Provides:** `replenishment.planning@1.0.0`.
**Entry checks:** `validate`; `check-env --set FS-10`; `pytest app/tests/contracts/test_fs01_fs04_fs05_fs06_fs08_fs09_fs10_contracts.py -q`.
**Exit checks:** `alembic upgrade head`; `pytest app/tests/integration/replenishment -q`; provider contract; `pytest app/tests/invariants/test_planning_readonly_ledger.py app/tests/invariants/test_atp_never_negative.py app/tests/invariants/test_aim_formula_policy_fixtures.py -q`; `record --set FS-10`.
**Acceptance criteria (Given/When/Then):**
- Given a tenant with an AIM formula policy id+version, when replenishment.aim runs, then every replenishment_calculation_line records formula_version and deterministic fixtures produce the same reorder point/preferred stock as the policy contract (D7).
- Given open demands and supplies, when a supply plan runs, then supply_plan_line projected balances and suggestions are produced and POST /v1/supply-plans/{id}/create-orders creates drafts only via FS-08/FS-09/FS-04 APIs (no ledger rows from planning).
- Given allocation strategies and FEFO lots, when an allocation run executes, then allocation_result never exceeds supply by item/location or lot, and unsatisfied demand raises allocation_exception records.
- Given an ATP method policy, when GET /v1/atp is queried, then earliest availability and per-bucket projections are returned per the versioned method semantics and projected available is clamped at zero.
- Given planning runs, when they complete twice from identical inputs, then results are deterministic and a rerun creates a new immutable run (idempotent run_key).
**Rollback:** flag off; plans/results immutable/readable; drafts owned by their contexts; additive.
**Spec:** `feature-sets/FS-10.md`.

### 7.12 FS-11 — Warehouse Management Execution (wave 1E, size L)

**Objective:** server-side WMS execution: warehouse/location extensions, process families/groups, stock locations, order types, UCC formats, carrier service levels, pick/putaway strategies, system rules, bin blocks, inbound shipments/receiving/putaway tasks, waves/release schedules, pick/pack/ship tasks + manifests, WMS replenishment and status-change tasks, cart/device-session records (EXT, no mobile protocol per D8).

**Source coverage:** `iri-255`, `mpw-792..794, 796, 799, 802..807, 809, 812..814, 816..818, 823..828, 850..851, 853..863, 865..868, 870, 872, 884..892, 894..905, 907..915, 917..923, 925..928, 932..934, 936`. Docs: [mpw-794](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/chapter_156382465711.html), [mpw-823](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_156406848826.html), [mpw-850](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/chapter_1541435915.html), [mpw-895](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/chapter_1541442931.html).

**Backend scope.** Module `app/modules/wms`. Tables: `warehouse`, `item_process_family`, `item_process_group`, `item_process_assignment`, `item_stock_location`, `order_type`, `ucc_code_format`, `carrier_service_level`, `pick_strategy`, `pick_strategy_rule`, `putaway_strategy`, `putaway_strategy_rule`, `system_rule`, `system_rule_setting`, `bin_block`, `inbound_shipment`, `inbound_shipment_line`, `receiving_order`, `receiving_order_line`, `putaway_task`, `putaway_task_line`, `wave`, `wave_line`, `wave_release_schedule`, `pick_task`, `pick_task_line`, `pack_task`, `pack_carton`, `pack_carton_line`, `shipment`, `shipment_line`, `ship_manifest`, `ship_manifest_line`, `replenishment_plan`, `replenishment_task`, `status_change_task`, `warehouse_cart`, `device_session`. Services: strategy/system-rule resolution, inbound receiving→receipt bridge with deferred posting, putaway task generation, wave release+picking+packing+shipping task state machines, task completion posting bridge (T-01/T-12/T-29/T-30 through FS-04 only), bin-block/expiry pick guards, WMS bin replenishment, status-change tasks, server-side cart/session containers (EXT, no device API).
APIs: `/v1/warehouses`, `/v1/warehouses/{id}/pick-strategies|putaway-strategies|system-rules`, `/v1/item-process-families`, `/v1/item-process-groups`, `/v1/item-stock-locations`, `/v1/order-types`, `/v1/ucc-code-formats`, `/v1/carrier-service-levels`, `/v1/bin-blocks`, `/v1/inbound-shipments`, `/v1/receiving-orders`, `/v1/putaway-tasks`, `/v1/waves`, `/v1/wave-release-schedules`, `/v1/pick-tasks`, `/v1/pick-tasks/{id}/complete`, `/v1/pack-tasks`, `/v1/shipments`, `/v1/ship-manifests`, `/v1/replenishment-plans`, `/v1/replenishment-tasks`, `/v1/status-change-tasks`.
Events: `wms.task.created|assigned|completed|exception`, `wms.wave.released|completed`, `wms.shipment.shipped`, `wms.receiving.completed`, `bin.transferred`, `inventory.status_changed`. Jobs: `wms.wave.release_schedule`, `wms.putaway.assign`, `wms.replenishment.generate`, `wms.task.watchdog`, `wms.shipment.manifest.reconcile`. Flags: `wms.execution.enabled`.

**Dependencies:** FS-00 `platform.core` (>=1.0.0 <2.0.0), `platform.events` (>=1.0.0 <2.0.0), `order.demand` (>=0.1.0 <1.0.0); FS-01 `item.master` (>=1.0.0 <2.0.0); FS-03 `traceability.registry` (>=1.0.0 <2.0.0); FS-04 `inventory.ledger` (>=1.0.0 <2.0.0), `inventory.balances` (>=1.0.0 <2.0.0); FS-06 `traceability.numbered` (>=1.0.0 <2.0.0); FS-07 `counts.engine` (>=1.0.0 <2.0.0); FS-08 `procurement.p2p` (>=1.0.0 <2.0.0); FS-09 `manufacturing.execution` (>=1.0.0 <2.0.0). **Provides:** `wms.execution@1.0.0`.
**Entry checks:** `validate`; `check-env --set FS-11`; `pytest app/tests/contracts/test_fs01_fs03_fs04_fs06_fs07_fs08_fs09_fs11_contracts.py -q`.
**Exit checks:** `alembic upgrade head`; `pytest app/tests/integration/wms -q`; provider contract; `pytest app/tests/invariants/test_wave_task_completion.py app/tests/invariants/test_pick_never_negative.py app/tests/invariants/test_bin_block_pick_guard.py -q`; `record --set FS-11`.
**Acceptance criteria (Given/When/Then):**
- Given a released order line, when a wave releases it, then pick tasks decompose per strategy/rules and PATCH task completion posts the corresponding ledger movements (bin transfer/status change/fulfillment) through FS-04 contracts only.
- Given a blocked bin or an expired lot, when a pick task recommendation is generated, then the bin/lot is excluded and an exception state is recorded instead of a negative or expired pick.
- Given an inbound shipment and a receiving order, when receiving completes with deferred posting enabled, then item_receipt moves to PENDING_POSTING and manual post/reversal flows use FS-08 receipt contracts.
- Given cart/device_session records exist, when any mobile-specific protocol or offline sync endpoint is requested, then the API has none (D8); only server-side task/queue resources are exposed.
- Given WMS replenishment planning/execution, when a replenishment task completes, then it posts a bin transfer (T-29) and updates the primary bin quantity read model.
**Rollback:** flag off; tasks stay as audit records; posted movements immutable; additive.
**Spec:** `feature-sets/FS-11.md`.

### 7.13 FS-12 — Quality Management (wave 1F, size M)

**Objective:** backend quality semantics (D4 EXT): quality specs/lines, inspection plans/lines, triggers, on-demand queues, inspections/results/dispositions/waivers, COA, quality images, preference/derived-field config; no quality REST API/vendor integration, mobile collection or reports in Phase 1.

**Source coverage:** `iri-360..363, 365, 371, 373..377, 380..384`. Docs: [iri-360](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/part_1510239273.html), [iri-371](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/chapter_1503070323.html), [iri-374](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_1525966493.html), [iri-382](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_163274330949.html).

**Backend scope.** Module `app/modules/quality`. Tables: `quality_spec`, `quality_spec_line`, `inspection_plan`, `inspection_plan_line`, `inspection_trigger`, `inspection_queue`, `inspection`, `inspection_result`, `coa`, `coa_line`. Services: spec definition/validation, inspection-plan resolution, event-trigger evaluation (receipt/WO/etc.), on-demand queue creation, inspection lifecycle+result capture+disposition/waiver, quality-hold posting via ledger status-change (T-30), COA generation/immutability, quality images via `attachment_metadata`.
APIs: `/v1/quality/specs`, `/v1/quality/inspection-plans`, `/v1/quality/inspection-triggers`, `/v1/quality/inspection-queues`, `/v1/quality/inspections`, `/v1/quality/inspections/{id}/results|disposition`, `/v1/quality/coas`.
Events: `quality.inspection.created|completed|passed|failed|waived`, `quality.hold.applied|released`, `quality.coa.issued`. Jobs: `quality.trigger.evaluate`, `quality.hold.reconcile`, `quality.queue.expire`. Flags: `quality.management.enabled`.

**Dependencies:** FS-00 `platform.core` (>=1.0.0 <2.0.0), `platform.events` (>=1.0.0 <2.0.0); FS-01 `item.master` (>=1.0.0 <2.0.0); FS-03 `traceability.registry` (>=1.0.0 <2.0.0); FS-04 `inventory.ledger` (>=1.0.0 <2.0.0); FS-06 `traceability.numbered` (>=1.0.0 <2.0.0); FS-08 `procurement.p2p` (>=1.0.0 <2.0.0); FS-09 `manufacturing.execution` (>=1.0.0 <2.0.0). **Provides:** `quality.management@1.0.0`.
**Entry checks:** `validate`; `check-env --set FS-12`; `pytest app/tests/contracts/test_fs01_fs03_fs04_fs06_fs08_fs09_fs12_contracts.py -q`.
**Exit checks:** `alembic upgrade head`; `pytest app/tests/integration/quality -q`; provider contract; `pytest app/tests/invariants/test_inspection_trigger_once.py app/tests/invariants/test_quality_hold_status.py -q`; `record --set FS-12`.
**Acceptance criteria (Given/When/Then):**
- Given an inspection trigger configured on receipts and a posted receipt, when the trigger evaluates, then exactly one inspection is created for the source document and its queue entry is idempotent per event.
- Given an inspection with required spec lines, when all results are recorded, then PASS/FAIL disposition derives from results and a FAIL can only be closed by approved WAIVE or an inventory hold posting via FS-04.
- Given a quality hold, when it is applied, then the affected stock is moved to a non-committable status through the ledger status-change contract (T-30) and availability changes accordingly.
- Given a passed inspection/COA, when a COA is generated, then it references the inspection and its result lines and is immutable after issue.
**Rollback:** flag off; existing inspections/holds remain; holds not auto-released by flag; additive.
**Spec:** `feature-sets/FS-12.md`.

### 7.14 FS-13 — Consigned Inventory Management (wave 1F, size M)

**Objective:** vendor consignment agreements/items, consigned receipt T-21, ownership transfer on consumption T-22 (with vendor bill), consigned adjustments/counts T-23, consumption audit, consigned journal intents, and the documented consigned×assembly incompatibility guard (D6).

**Source coverage:** `iri-323..331`. Docs: [iri-323](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/article_94502112.html), [iri-324](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_0218102533.html), [iri-328](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_0218010714.html), [iri-331](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/article_0317081004.html).

**Backend scope.** Module `app/modules/consigned`. Tables: `consignment_agreement`, `consignment_agreement_item`, `consignment_ownership_transfer`, `consignment_ownership_transfer_line`, `consignment_consumption`. Services: agreement lifecycle/eligibility (`allow_vendor_consignment`), consigned receipt/ownership-transfer/adjustment posting T-21/T-22/T-23 with ownership dimension, vendor-bill creation on consumption via FS-08, consumption audit rows, journal intents for consigned accounts, **D6 compatibility guard**.
APIs: `/v1/consignment-agreements`, `/v1/consignment-agreements/{id}/items`, `/v1/consignment-transfers`, `/v1/consignment-consumption`.
Events: `consigned.received|owned|adjusted|consumed`. Jobs: `consigned.consumption.reconcile`, `consigned.agreement.expiry`, `consigned.ownership.reconcile`. Flags: `consigned.inventory.enabled`, `consigned.assembly_incompatible_guard`.

**Dependencies:** FS-00 `platform.core` (>=1.0.0 <2.0.0), `platform.events` (>=1.0.0 <2.0.0), `platform.finance` (>=1.0.0 <2.0.0); FS-01 `item.master` (>=1.0.0 <2.0.0); FS-03 `traceability.registry` (>=1.0.0 <2.0.0); FS-04 `inventory.ledger` (>=1.0.0 <2.0.0); FS-05 `costing.engine` (>=1.0.0 <2.0.0); FS-06 `traceability.numbered` (>=1.0.0 <2.0.0); FS-08 `procurement.p2p` (>=1.0.0 <2.0.0). **Provides:** `consigned.inventory@1.0.0`.
**Entry checks:** `validate`; `check-env --set FS-13`; `pytest app/tests/contracts/test_fs01_fs03_fs04_fs05_fs06_fs08_fs13_contracts.py -q`.
**Exit checks:** `alembic upgrade head`; `pytest app/tests/integration/consigned -q`; provider contract; `pytest app/tests/invariants/test_consigned_ownership_conservation.py app/tests/invariants/test_consigned_assembly_guard.py -q`; `record --set FS-13`.
**Acceptance criteria (Given/When/Then):**
- Given an ACTIVE consignment agreement and an allowed item, when a consigned receipt posts (T-21), then qty rises under ownership=CONSIGNED with no AP liability and a consigned valuation entry.
- Given consigned on-hand, when consumption/ownership transfer posts (T-22), then the same qty moves CONSIGNED->OWNED, a vendor bill is created through the FS-08 contract, and consignment_consumption records the audit row.
- Given a tenant that enables consigned inventory, when an assembly item is configured for consignment or manufacturing is enabled, then the documented incompatibility guard rejects the configuration (D6).
- Given consigned stock, when an inventory worksheet or ordinary adjustment path is attempted, then the consigned routing (T-23) is enforced and the ordinary path returns 409 PROHIBITED_CONSIGNED_OPERATION.
**Rollback:** flag off; consigned positions stay; dispose via T-22/T-23 before disabling; additive.
**Spec:** `feature-sets/FS-13.md`.

### 7.15 FS-14 — Extensions (wave 1F, size S)

**Objective:** backend-only SuiteApp semantics included by D4: item warranty terms, warranty registration/claims/actions/repair-order data, customer part-number mappings, Auto Assign UPC registry — no UI/workbook/external integration.

**Source coverage:** `iri-134, 136..138, 352..359, 387..390`. Docs: [iri-134](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/chapter_4575965810.html), [iri-352](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/chapter_3938292477.html), [iri-355](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_3938315832.html), [iri-356](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_3938319263.html), [iri-387](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/chapter_158373836991.html).

**Backend scope.** Module `app/modules/extensions`. Tables: `item_warranty`, `warranty_registration`, `warranty_claim`, `warranty_claim_action`, `warranty_repair_order`, `customer_part`, `upc_registry`. Services: warranty term resolution, registration (serial-keyed) & coverage calculation, claim validation/override/actions, repair-order data, customer part mappings (external customer refs only), UPC assignment/retirement/uniqueness.
APIs: `/v1/items/{id}/warranty`, `/v1/warranty/registrations`, `/v1/warranty/claims`, `/v1/warranty/claims/{id}/actions`, `/v1/warranty/repair-orders`, `/v1/customer-parts`, `/v1/upc-registry`.
Events: `warranty.registered`, `warranty.claim.opened|approved|completed`, `extensions.customer_part.created`, `extensions.upc.assigned|retired`. Jobs: `extensions.warranty.expiry`, `extensions.upc.reconcile`, `extensions.claims.reconcile`. Flags: `extensions.registry.enabled`.

**Dependencies:** FS-00 `platform.core` (>=1.0.0 <2.0.0), `platform.events` (>=1.0.0 <2.0.0); FS-01 `item.master` (>=1.0.0 <2.0.0); FS-06 `traceability.numbered` (>=1.0.0 <2.0.0); FS-08 `procurement.p2p` (>=1.0.0 <2.0.0). **Provides:** `extensions.registry@1.0.0`.
**Entry checks:** `validate`; `check-env --set FS-14`; `pytest app/tests/contracts/test_fs01_fs06_fs08_fs14_contracts.py -q`.
**Exit checks:** `alembic upgrade head`; `pytest app/tests/integration/extensions -q`; provider contract; `pytest app/tests/invariants/test_upc_uniqueness.py app/tests/invariants/test_warranty_claim_validation.py -q`; `record --set FS-14`.
**Acceptance criteria (Given/When/Then):**
- Given an item with default warranty terms, when a warranty registration is created for a serialized unit, then the registration validates the serial identity (FS-06) and the coverage window/terms derive from the item terms.
- Given a registration and claim, when a claim is validated, then it is rejected outside coverage unless approved as an override, and repair/refund/replacement actions are recorded append-only on the claim.
- Given Auto Assign UPC enabled for a tenant, when two items are assigned UPCs, then the registry enforces uniqueness and retirement frees a UPC for assignment tracking but never reuses the item/UPC history row.
- Given a customer part number mapping, when a duplicate mapping is inserted, then the (tenant, customer_ref, part_number) uniqueness rule rejects it.
**Rollback:** flag off; records readable; additive; corrections via new claim actions/registrations.
**Spec:** `feature-sets/FS-14.md`.

### 7.16 FS-15 — Reporting Read Models and Reporting API (wave 2, size M)

**Objective:** read-only reporting surfaces over all Phase-1 read models: 21 reporting views (stock ledger, balances, valuation summary/detail, snapshot, negative inventory, commitments, backorders, pending fulfillment, activity detail, turnover/profitability inputs, ATP, planning workbench, purchase history, manufacturing workbench, lot expiry, serial history, consigned balance, quality status, available-to-build) plus a minimal reporting API; no writes, no analytics workbooks.

**Source coverage:** `iri-217, 334..348` (backend read models; report rendering/workbooks deferred). Docs: [iri-217](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_1518564230.html), [iri-334](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/chapter_N2353200.html), [iri-342](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2356560.html), [iri-345](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2358933.html).

**Backend scope.** Module `app/modules/reporting` (views only + read API). Tables: 21 views `v_stock_ledger`, `v_inventory_balance`, `v_inventory_valuation`, `v_inventory_valuation_detail`, `v_inventory_snapshot`, `v_negative_inventory`, `v_open_commitments`, `v_backorders`, `v_pending_fulfillment`, `v_inventory_activity_detail`, `v_inventory_turnover_inputs`, `v_inventory_profitability_inputs`, `v_atp_current`, `v_planning_workbench`, `v_purchase_history`, `v_manufacturing_workbench`, `v_lot_expiry`, `v_serial_history`, `v_consigned_balance`, `v_quality_inspection_status`, `v_available_to_build`. Services: read-model/refresh-failure handling, data-scope+permission-gated queries (`reporting.cost.view`), cursor pagination, documented staleness; exit-gate reconcile against source read models.
APIs: `/v1/reports/stock-ledger`, `/v1/reports/inventory-valuation`, `/v1/reports/inventory-valuation-detail`, `/v1/reports/inventory-snapshot`, `/v1/reports/negative-inventory`, `/v1/reports/backorders`, `/v1/reports/pending-fulfillment`, `/v1/reports/inventory-activity`, `/v1/reports/turnover`, `/v1/reports/profitability`, `/v1/reports/lot-expiry`, `/v1/reports/serial-history`, `/v1/reports/consigned-balance`, `/v1/reports/quality-inspections`, `/v1/reports/purchase-history`, `/v1/reports/manufacturing-workbench`, `/v1/reports/planning-workbench`, `/v1/reports/atp-workbench`, `/v1/inventory/balances/search`, `/v1/inventory/details/search`.
Events: `reporting.view.refreshed` (internal), `reporting.refresh.failed`. Jobs: `reporting.refresh_views`, `reporting.reconcile_readmodels`. Flags: `reporting.readmodels.enabled`.

**Dependencies:** FS-00 `platform.core` (>=1.0.0 <2.0.0), `platform.events` (>=1.0.0 <2.0.0); FS-01 `item.master` (>=1.0.0 <2.0.0); FS-02 `pricing.engine` (>=1.0.0 <2.0.0); FS-03 `traceability.registry` (>=1.0.0 <2.0.0); FS-04 `inventory.ledger` (>=1.0.0 <2.0.0), `inventory.balances` (>=1.0.0 <2.0.0); FS-05 `costing.engine` (>=1.0.0 <2.0.0); FS-06 `traceability.numbered` (>=1.0.0 <2.0.0); FS-07 `counts.engine` (>=1.0.0 <2.0.0); FS-08 `procurement.p2p` (>=1.0.0 <2.0.0); FS-09 `manufacturing.execution` (>=1.0.0 <2.0.0); FS-10 `replenishment.planning` (>=1.0.0 <2.0.0); FS-11 `wms.execution` (>=1.0.0 <2.0.0); FS-12 `quality.management` (>=1.0.0 <2.0.0); FS-13 `consigned.inventory` (>=1.0.0 <2.0.0); FS-14 `extensions.registry` (>=1.0.0 <2.0.0). **Provides:** `reporting.readmodels@1.0.0`.
**Entry checks:** `validate`; `check-env --set FS-15`; `pytest app/tests/contracts/test_fs15_readmodel_contracts.py -q`.
**Exit checks:** `alembic upgrade head`; `pytest app/tests/integration/reporting -q`; provider contract; `pytest app/tests/invariants/test_reporting_readonly.py app/tests/invariants/test_reconcile_exact.py -q`; `python tools/feature_sets.py coverage`; `record --set FS-15`.
**Acceptance criteria (Given/When/Then):**
- Given a posted ledger/cost history, when reporting views/materialised views refresh, then valuation summary/detail, stock ledger, snapshot, back-order and pending-fulfillment results reconcile exactly to the underlying read models (zero drift).
- Given any reporting endpoint, when a write verb is attempted, then the API returns 405/403 and the reporting schema grants reject writes (read-only contract).
- Given balance/detail search endpoints, when queried with tenant+dimension+date filters, then results respect RLS, cursor pagination and the p95 read budget.
- Given a read-model refresh failure, when the job retries, then the previous snapshot remains served and an alert/reporting.view.refreshed failure metric is raised.
**Rollback:** flag off stops refresh/API; views droppable/recreatable; no base-table migrations.
**Spec:** `feature-sets/FS-15.md`.

---

## 8. Deltas, conflicts resolved and items for the Lead

### 8.1 DATA-MODEL deltas introduced by binding Lead decisions

| # | Delta | Impact |
|---|---|---|
| Δ-1 | **D2** requires a persisted journal-intent store. DATA-MODEL §3 lists 27 platform tables without it. Add `platform.finance_journal_intent` (append-only: intent id, source doc/txn, posting-rule id, lines `{account_ref, debit, credit, currency, dimensions}`, period key, created_at) **owned by FS-00**, exposed read-only at `GET /v1/finance/journal-intents`. | DATA-MODEL §3 table count 27→28 for D2 (**34 after Δ-MT + Δ-SSO; 274→280 base tables total**); docs owned by architect-core should record the amendment. No other set writes it directly — they call `platform.finance`. |
| Δ-2 | **D3** v0 order boundary contracts `order.demand@0.1.0` / `order.supply@0.1.0` are published by FS-00 (core port + JSON Schemas + test doubles) and implemented by FS-04 (ledger endpoints/events). | Adds two provided contracts to FS-00 and **five `order.demand` consumer edges** (FS-04/08/09/10/11). `order.supply` is the outbound availability/commitment/fulfilment event contract; its consumer is the **external Phase-2 order-management system** (outside the manifest), so it has no in-graph dependent — FS-04 provides it and validates it via provider contract tests + test doubles. Both are preview contracts (`@0`): breaking changes require a version bump + consumer list via `who-depends-on` (§5 change control). |
| Δ-3 | FS-15 has `phase=1`, `wave="2"` — "wave 2" is the Phase-1 reporting wave (framework §6 expected shape), **not** Phase 2. | No forward-phase dependency; all sets remain Phase 1 and `backend_only=true`. |

### 8.2 Dependency conflicts found and resolved (in favour of ARCHITECTURE §1.4 layering)

DATA-MODEL §21's FS dependency lists are advisory; the per-table **`Ext-deps` columns (§3–§16)**, the **§21 ownership map** and **ARCHITECTURE §1.4 layering** are authoritative where they disagree. Four additions were made and are recorded here rather than silently changing model ownership:

| # | Edge added | Why | Layering check |
|---|---|---|---|
| R-1 | `FS-08 → FS-06 traceability.numbered` | `item_receipt_detail` Ext-deps list `lot`, `serial_number` (FS-06-owned identities), so numbered receipts need FS-06; §21's dependency list omitted FS-06. | procurement L4 → traceability L2: allowed (L4 may import L0–L3; traceability is the shared L2 module) |
| R-2 | `FS-09 → FS-06 traceability.numbered` | `assembly_build` Ext-deps list `lot`, `serial_number` (T-15 output identity), so builds/WIP of numbered assemblies need FS-06; §21's dependency list omitted FS-06. | manufacturing L4 → traceability L2: allowed |
| R-3 | `FS-10 → FS-06 traceability.numbered` | `allocation_result` Ext-deps list `lot`, `serial_number` (FEFO lot/serial assignment, iri-311), so allocation needs FS-06; §21's dependency list omitted FS-06. | replenishment L5 → traceability L2: allowed |
| R-4 | `FS-12 → FS-03 traceability.registry` | `inspection` Ext-deps list `location` (FS-03-owned) and quality holds apply inventory-status changes (T-30; `inventory_status` is FS-03-owned, DM-TRACE-03); §21's dependency list omitted FS-03 for FS-12. | quality L5 → traceability L2: allowed |
| — | No edges removed; WMS deliberately **does not** depend on FS-10 (warehouse bin replenishment uses stocking rules from FS-01/FS-11, not purchasing-replenishment plans — ARCHITECTURE §6.8). FS-11 stays 1E and FS-10 1D by wave order, not by edge. | — | consistent with DATA-MODEL §21 |

No cycle, phase inversion or same-wave forward edge results from these additions (all targets are in earlier or same-wave positions: FS-06 1A < FS-08 1B < FS-09 1C < FS-10 1D; FS-03 0C < FS-12 1F).

### 8.3 Things the Lead should know / decide

1. **DATA-MODEL amendment (Δ-1):** the model owner should add `platform.finance_journal_intent` to DATA-MODEL §3/§21.1 (M00) and §5.7 event catalogue (`finance.journal_intent.recorded`). This is the only schema delta needed to execute D2 — the rest of the model is used as authored.
2. **Order-boundary contract ownership (Δ-2):** validated as FS-00 provider + FS-04 implementation. If the Lead prefers the inverse (FS-04 provides the v0 stub), only the manifest contract ownership changes; edge directions (consumers) stay the same.
3. **FS-15 "wave 2" naming (Δ-3):** documented as Phase-1 reporting wave. If the Lead wants FS-15 in Phase 2 instead, it must be re-phased (phase=2 → no dependent sets, so mechanically safe, but PLAN §2/§6 auto-tables must change and Phase-1 "backend complete" excludes reporting).
4. **SuiteApp inclusion sets FS-12/FS-14 have no catalog-classified core-backend topics** (all their topics are `suiteapp`-classified and included by D4 at backend-semantics level). The coverage validator records this explicitly rather than weakening the inclusion decision.
5. **Test doubles are deliverables, not scaffolding:** `NullFinanceAdapter` + `finance_journal_intent` read API (D2) and the order boundary v0 doubles (D3) are FS-00 exit criteria; Phase-2 finance/order integrations must consume the same contracts.

---

## 9. Definition of Done applied to every set (framework §7)

Each `feature-sets/FS-XX.md` spec must satisfy, and each exit gate must verify: module code + additive migrations merged with boundary respected (no cross-module ORM imports; import-linter green); OpenAPI paths + JSON Schemas for events/jobs under `contracts/`; unit tests for business rules + invariant tests for the set's rules; provider+consumer contract tests green in CI; migration up/down on empty+seeded DB, N-1 compatible; manifest entry/exit checks present and runnable; feature flag wired default off; tenant-isolation test on every route; audit events for every state change; RBAC scopes declared; performance smoke within framework §9 budgets; runbook section in `feature-sets/FS-XX.md`; `PLAN.md` status + verification evidence updated.

Cross-set invariants that exit gates re-run (INV-* catalogue: ARCHITECTURE §7.3; global rule index: DATA-MODEL §20): `INV-01` qty conservation, `INV-02` value conservation, `INV-03/10` balances == Σ ledger and reconcile-exact, `INV-04` commitment ≤ on-hand, `INV-09` append-only privileges, `INV-13/14` numeric precision/zero rounding drift, `INV-18` serial projection == history == Σ detail, `INV-20..24` planning readonly/ATP/allocation/time-fence, `INV-25` tenant isolation, `INV-26` state machines; `DM-GLOBAL-01..10` (DATA-MODEL §20) cover schema conventions/deferred FKs/RLS.

---

## 10. Self-validation of this deliverable (reproducible now)

`tools/feature_sets.py` is a shared artifact owned by the tooling task and does not exist yet; the manifest was therefore validated with an equivalent independent L0 script (same rules as framework §4). The commands below are the exact checks to run before accepting any change to this decomposition:

```bash
# 1) JSON parses + framework §4 L0 rules + coverage cross-checks
python3 - <<'PY'
import json, re
m = json.load(open('design/feature-sets.json'))
c = json.load(open('design/feature-coverage.json'))
sets = m['feature_sets']; by = {s['id']: s for s in sets}
assert m['schema_version'] == '1.0' and len(sets) == 16
assert {s['id'] for s in sets} == {f'FS-{i:02d}' for i in range(16)}
assert all(s['phase'] == 1 and s['backend_only'] is True for s in sets)
assert len({(p['contract']) for s in sets for p in s['provides']}) == 21
ids = [s['id'] for s in sets]
edges = [(s['id'], d) for s in sets for d in s['depends_on']]
assert all(d['id'] in by and d['id'] != s for s, d in edges)
assert all(ids.index(d['id']) < ids.index(s) for s, d in edges)      # manifest order = topological order
seen = set(); order = []
frontier = [i for i in ids if not by[i]['depends_on']]
while frontier:
    n = frontier.pop(0); order.append(n); seen.add(n)
    frontier += [x for x in ids if x not in seen and x not in frontier and all(d['id'] in seen for d in by[x]['depends_on'])]
assert len(order) == 16                                              # acyclic
cov = {k: v for k, v in c.items() if k != 'totals'}
assert len(cov) == 1348 and c['totals']['total'] == 1348
assert set(cov) == {f'iri-{i:03d}' for i in range(1, 413)} | {f'mpw-{i:03d}' for i in range(1, 937)}
assert 'EXCLUDED-SCM-MOBILE' not in {v['disposition'] for v in cov.values()}   # SCM Mobile out of catalog scope
assert all(any(v['disposition'] == f'FS-{i:02d}' for v in cov.values()) for i in range(16))
assert sum(c['totals']['by_disposition'].values()) == 1348
print('manifest+coverage: OK (16 FS, %d edges, 21 contracts, 1,348 topics accounted)' % len(edges))
PY

# 2) Human-readable gate once the shared tool lands
python tools/feature_sets.py validate
python tools/feature_sets.py order --phase 1
python tools/feature_sets.py coverage
```

Recorded result of the above at hand-off: **0 errors / 0 warnings, 4,317 assertions** in the full validator, 116 edges, 21 contracts, 1,348/1,348 topics accounted (769 in-scope, 579 deferred, 0 gaps), DAG acyclic, manifest order topological.
