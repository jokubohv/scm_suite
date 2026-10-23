# ARCHITECTURE — Phase-1 Core Backend (Inventory / SCM Platform)

**Status:** design baseline for review · **Version:** 1.0 · **Date:** 2026-10-21
**Owner:** architect-core (teammate) · **Consumers:** `design/DATA-MODEL.md` (companion), feature-set architect (FS-00..FS-1F), implementers, verifier
**Binding inputs:** `design/FEATURE-SET-FRAMEWORK.md` (§3 stack is fixed; §9 NFRs), `inventory-platform/PLAN.md` §3/§12 (GL, order-management, FX boundaries), research catalogs `research/features-item-inventory.{md,json}` (`iri-*` ids) and `research/features-mfg-procurement-wms.{md,json}` (`mpw-*` ids).
**Scope rule:** Phase-1 **core backend logic only**. No UI, mobile, analytics, external integrations, SuiteApp packaging. **SCM Mobile is permanently excluded** (mobile-device procedures are catalogued but deferred; backend records they would need may be modelled, explicitly marked `EXT`).

---

## 0. Reading guide and conventions

| Marker | Meaning |
|---|---|
| `[iri-216]`, `[mpw-152]` | Trace to a catalog topic id. The **Source citation key** at the end of this document maps every id → title → official doc URL. Catalog ids are stable; the JSON is the machine-readable source. |
| `ASSUMPTION` | Design choice not directly stated by the crawled NetSuite help; must be reviewed before implementation. |
| `EXT` / `EXTENSION` | Deliberate extension beyond documented NetSuite behavior (usually because Phase 1 lacks a UI or a neighbouring product area). |
| `BOUNDARY` | Implemented as an **interface/port** only; the counterpart system is out of Phase-1 scope (GL, sales orders, customer master, carriers, EDI). |
| `INV-nn` | Testable invariant (see §7.3); every invariant has a named test in `app/tests/invariants/`. |
| `T-nn` | Posting transaction type in the matrix (§2.3). |
| `FS-xx` | **Feature-set slot** used for ownership/sequencing only. Final ids, names and dependency ranges are the feature-set architect's output (`FEATURE-SETS.md` / `feature-sets.json`); this document fixes the *direction* and *contents*, not the numbering. |

Definitions used throughout: **posting** = atomically appending signed inventory/cost movements and updating derived balances inside one DB transaction; **document** = a business form whose lifecycle eventually produces zero or more postings; **balance** = materialised read model derived from the append-only ledger; **cost object** = the granularity at which unit cost is maintained (item × location or × location-costing-group, plus lot/serial where the method requires it).

**Model at a glance** (authoritative detail in `design/DATA-MODEL.md`): **287 base tables** across 14 table-bearing schemas (13 domain + `platform`) plus a read-only `reporting` schema with **21 views**; **15 feature-set slots FS-00..FS-14** plus reporting `FS-15` (final ids owned by the feature-set architect); **31 posting transaction types** (T-01..T-31) + **6 non-posting commands** (C-01..C-06); **146 scoped business rules** (`DM-*`) + 10 global rules = **156 testable rules**; **invariants INV-01..INV-33**; **RBAC catalogue: 255 capability scopes (241 assignable + 13 service-only + 1 platform-only) / 7 role templates / 14 SoD pairs**; **Dex OIDC broker + per-tenant enterprise SSO (Δ-SSO, EXT)** (`design/permissions.json`, Δ-RBAC); **44 state-machine specifications** covering 53 document aggregates. This document is the behaviour/architecture contract; the data model document is the schema contract.

---

## 1. System shape and rationale

### 1.1 What Phase 1 is

A multi-tenant **SCM transactional backend** that is behaviourally compatible with the documented NetSuite inventory/manufacturing/procurement/WMS semantics, exposed as:

- a versioned **REST API** (OpenAPI 3.1) for document entry, posting commands and queries;
- an append-only **inventory + cost ledger** that is the system of record;
- **domain events** on a transactional outbox for asynchronous consumers (cost recalculation, plans, projections, future UI/integrations);
- **jobs** (recalculation, planning runs, reconciliation, outbox) on a Redis-backed worker;
- **read models** (balances, valuation, ATP, planning workbench, stock ledger views) for fast queries and exit-gate checks.

Out of Phase 1: every screen, all mobile, analytics/BI, EDI/carrier/accounting export, SuiteApp distribution, GL/period-close implementation, sales-order/customer master implementation.

### 1.2 Runtime topology

```
                       ┌──────────────────────────────────────────────────────────┐
   client (Phase 2 UI, │  api  (FastAPI, Python 3.12, N workers)                  │
   partner API, tests) │  - /v1 routers per module, OpenAPI 3.1                   │
                       │  - authn (JWT / API key) → tenant ctx → RBAC scope       │
                       │  - use-case services → posting engine → Postgres tx      │
                       └───────────────┬──────────────────────────┬───────────────┘
                                       │ SQL (SQLAlchemy 2.0)     │ enqueue job
                                       ▼                          ▼
                       ┌──────────────────────────┐   ┌─────────────────────────────┐
                       │ PostgreSQL 16            │   │ Redis 7                     │
                       │ - per-module schemas     │   │ - RQ queues (default,        │
                       │ - inventory/cost ledger  │   │   costing, planning, heavy)  │
                       │ - outbox_event           │   │ - pub/sub + Streams          │
                       │ - read models/views      │   └──────────▲──────────────────┘
                       └────────────┬─────────────┘              │
                                    │ outbox rows (SKIP LOCKED)  │ publish
                       ┌────────────▼─────────────┐              │
                       │ outbox-relay (process)   │──────────────┘
                       │ at-least-once publisher  │  XADD stream `evt.<domain>`
                       └──────────────────────────┘
                                    ▲
                       ┌────────────┴─────────────┐
                       │ worker (RQ, N processes) │
                       │ costing recalc, planning, │
                       │ reconcile, imports, jobs  │
                       └──────────────────────────┘
```

Deployment unit: **one Docker image** containing `api`, `worker`, `outbox-relay` entrypoints (framework §3); core compose services `api`, `worker`, `postgres`, `redis`, `outbox-relay`, plus Dex units for SSO (see below). One logical database, **one schema per bounded context** (`platform`, `item_master`, `pricing`, `traceability`, `inventory_ledger`, `costing`, `counts`, `procurement`, `wms`, `manufacturing`, `replenishment`, `quality`, `consigned`, `extensions`, `reporting`). Schema ownership = module ownership (framework §3 boundaries).
**Δ-SSO extension to the topology (EXT):** a **Dex OIDC identity broker** runs as additional deployment units — in **production one isolated Dex issuer per ACTIVE `tenant_sso_connection`** (own Dex config generated from the connection, own issuer e.g. `https://sso-<tenant-slug>.auth.example.com`, own storage); **dev/test may run one shared Dex** with test connectors. Dex storage is a **dedicated PostgreSQL `dex` database/schema owned by Dex and never queried by the platform API**; Dex is pinned to a tested 2.4x release; Dex↔API traffic is OIDC discovery/authorization/token/JWKS over TLS. Secrets (OIDC client secret, SAML signing certs, LDAP bind) are secret-manager refs only (`*_secret_ref`); Dex authenticates the human, the platform still authorizes and issues its own access JWT + rotating refresh session (Δ-MT/Δ-RBAC unchanged).

### 1.3 Why a modular monolith (and what would have to be true for services)

| Option | Assessment | Verdict for Phase 1 |
|---|---|---|
| **Modular monolith + one Postgres** | Posting touches many contexts in one ACID transaction (receipt → balance → cost layer → commitment → outbox). One DB transaction is the cheapest correct implementation of `INV-01..INV-10`. Single Python language, one migration chain, one compose file, fast testcontainers loop. | **Chosen** (framework §3). |
| Microservices per bounded context | The hard part is not code organisation but **cross-context atomicity and lock ordering** (posting, costing, WMS execution, procurement matching). Phase 1 would need sagas/2PC + distributed idempotency to match a single `BEGIN…COMMIT`, and the team cannot yet pay for it. | Rejected. Revisit only if a context needs independent scaling (candidate: import/recalculation batch, already offloaded to `worker`). |
| Event-sourced ledger as a separate service | Attractive long-term, but documented semantics require mutation-style document lifecycles + recalculation of historical cost; a full event-store rewrite would delay ledger delivery. Outbox + append-only ledger tables give event-sourcing benefits where they matter (ledger, audit) without a global event store. | Rejected for Phase 1; ledger is append-only and replayable, so a later extraction remains possible. |
| Serverless functions | Cold starts and connection-per-request confound posting latency p95 ≤ 300 ms and pooled locking; weaker operational story for a single-host compose target. | Rejected. |
| Document store / JSONB-per-item | Cost layers, commitments, UoM conversions and reconciliation are relational set operations with strict constraints; JSONB would move invariants into application code. JSONB is used only for extension fields (`custom_field_value`, event payloads). | Rejected. |

**Scale assumption** (framework §9): 20 tx/s sustained, 200k ledger lines/h batch, single-host compose for dev/staging, managed Postgres for prod. A single Postgres with partitioned ledger and async projections comfortably exceeds this; throughput bottlenecks are cost-object lock contention and number-sequence contention, both addressed in §2.4/§5.5.

### 1.4 Bounded-context module map and dependency direction

Layers are **strict**: a module may import only modules in strictly lower layers, `core`, and its own package. Enforced in CI by an import-linter contract (decision D-14).

| Layer | Context (package `app/modules/…`) | Schema | Owns (summary) | Allowed imports (contract clients only) | Provides contract | Slot / wave |
|---|---|---|---|---|---|---|
| L0 | `core` (`app/core`) | `platform` | tenancy, **multi-tenant identity/membership (Δ-MT EXT)**, **Dex OIDC broker integration + enterprise SSO federation (Δ-SSO EXT)**, authN/Z, **RBAC permission catalogue/role templates/SoD (Δ-RBAC)**, audit, number sequences, outbox, jobs, idempotency, feature flags, approvals, attachments, custom-field registry, period projection | — (stdlib + infra only) | `platform.core`, `platform.events` | FS-00 / 0A |
| L1 | `item_master` | `item_master` | item taxonomy + type extensions, item locations, UoM types/units/conversions, item UoM sets, matrix, barcodes/aliases, kit/group membership, item categories | L0 | `item.master` | FS-01 / 0B |
| L1 | `pricing` | `pricing` | price levels, item/customer groups, pricing groups, item prices, quantity schedules, price rules, collections, coupons, effective-date price updates | L0, item_master | `pricing.engine` | FS-02 / 0B |
| L2 | `traceability` | `traceability` | location/zone/bin topology, inventory status registry, lots, serials, serial history | L0, item_master | `traceability.registry` | FS-03 (locations/bins/status, 0C) + FS-06 (lot/serial, 1A) |
| L3 | `inventory_ledger` | `inventory_ledger` | transactions/lines/details (append-only), balances, commitments/allocations, transfers & transfer orders, adjustments/worksheets, distribution, import batches, reason codes | L0–L2, pricing (sell-side price snapshot only) | `inventory.ledger`, `inventory.balances` | FS-04 / 0C |
| L3 | `costing` | `costing` | item cost, cost layers/consumption, cost ledger, revaluation, standard cost, variances, COGS adjustment, landed cost, location costing groups | L0–L2, inventory_ledger | `costing.engine` | FS-05 / 0C |
| L4 | `counts` | `counts` | count plans/filters, physical & cycle counts, snapshots, recount, variance generation | L0–L3, traceability | `counts.engine` | FS-07 / 1A |
| L4 | `procurement` | `procurement` | vendors & addresses/categories, item-vendor pricing, contracts/blanket, requisitions/requests/RFQ, PO, receipts, bills/variances/match, credits, vendor returns, drop-ship links | L0–L3, costing (landed-cost/value hook) | `procurement.p2p` | FS-08 / 1B |
| L4 | `manufacturing` | `manufacturing` | assembly definitions, BOM/revisions/controls, routings/work centres/calendars, WO + operations, WIP issue/completion/close, build/unbuild, outsourced mfg, ECO, scheduling/capacity | L0–L3, item_master, procurement (vendor/WO outsource) | `manufacturing.execution` | FS-09 / 1C |
| L5 | `replenishment` | `replenishment` | planning rules/horizons, demand history/plans, supply plans/suggestions, action messages, time fences, DRP, allocation strategies/runs, ATP projections, AIM calculations | L0–L4 | `replenishment.planning` | FS-10 / 1D |
| L5 | `wms` | `wms` | warehouse records/strategies/system rules, inbound shipments/receiving orders, putaway, waves/release schedules, pick/pack/ship tasks, ship manifests, WMS replenishment, status-change tasks, cart/device sessions | L0–L4 | `wms.execution` | FS-11 / 1E |
| L5 | `quality` | `quality` | quality specs/lines, inspection plans/lines, triggers/queues, inspections/results, COA, (extension) warranty | L0–L4 | `quality.management` | FS-12 / 1F |
| L5 | `consigned` | `consigned` | consignment agreements/items, ownership transfers, consigned consumption | L0–L4, procurement | `consigned.inventory` | FS-13 / 1F |
| L6 | `extensions` | `extensions` | item warranty data, warranty registration/claims/actions/repair orders, customer part numbers, UPC assignment registry | L0–L5 | `extensions.registry` | FS-14 / 1F |
| L6 | `reporting` | `reporting` (views only) | stock ledger, valuation, snapshot, back-order, pending fulfillment, turnover/profitability inputs, ATP/workbench views; reporting API surface | read-only views of all | `reporting.readmodels` | FS-15 / 2 |

**Dependency rules (normative):**

1. `api` and `worker` are the only importers of module routers/services; modules never import `api`.
2. A module may call a lower-layer module **only** through its `contract.py` facade (typed functions/DTOs) or read its published read-model views; importing another module's ORM model, table object or internal service is a build failure.
3. Cross-layer **writes** are synchronous calls inside the caller's transaction only when the lower module owns the write (e.g. ledger → costing `value_movement(...)` in the same tx). Asynchronous coupling uses events.
4. No upward imports, no cycles. `traceability` never imports `wms`; `ledger` never imports `procurement`; `costing` never imports `manufacturing`.
5. `reporting` is read-only: no writes, no migrations beyond views/materialised views.
6. Circular-looking needs are resolved by **ports**: e.g. WMS needs ledger to post receipts → ledger's `posting` facade; ledger needs WMS task status → WMS emits `wms.task.completed`, ledger does not read WMS tables.

### 1.5 Request, job and event paths

| Path | Flow | Guarantees |
|---|---|---|
| **Command (posting)** | HTTP `POST /v1/<doc>/:id/post` → authn/tenant/RBAC → idempotency check → load document `FOR UPDATE` → validate → build posting plan → acquire locks in rank order (§2.4.4) → append `inventory_transaction(+line/detail)` → update balances → call costing engine → insert `outbox_event` → commit → return 201/200 with resource + `Idempotency-Key` echo | ACID; exactly-once effect under retry; events at-least-once |
| **Query** | HTTP `GET` → authn → scope filter (tenant + data scopes) → read model/view (never recompute from ledger in request path) | p95 read budget; stale by ≤ outbox lag (documented per endpoint) |
| **Job** | Scheduler/API enqueues `job_run` → worker resolves `job_definition` → worker acquires advisory lock (`job:<name>:<tenant>`) → streams work in chunks → writes results/projections → completes `job_run` | idempotent per `(job_definition, run_key)`; retries with backoff; no half-visible batches (chunk commit + cursor) |
| **Event** | outbox row → relay `FOR UPDATE SKIP LOCKED` batch → publish to Redis stream → mark `published_at` → consumers checkpoint → handler must be idempotent on `event_id` | at-least-once, ordered per aggregate (sequence), schema-versioned |
| **Reconciliation** | nightly job recomputes balances/costs from ledger and compares to read models; drift ⇒ P1 + `reconcile_alert` audit event | zero drift tolerance (framework §9) |

### 1.6 External boundaries (ports declared now, implemented later)

| Boundary | Why it exists | Port (Phase-1 interface) | Phase-1 behaviour |
|---|---|---|---|
| **GL / finance** | NetSuite documents inventory value postings, accruals, variances, revaluation, COGS; the plan explicitly excludes building the GL/period close. | `core.ports.finance.FinancePort`: `record_journal_intent(intent: JournalIntent)`; `get_period_status(tenant, date) -> OPEN/CLOSED/LOCKED`; `get_account_ref(code)`; `receive_rate(base, quote, date)`. `JournalIntent` = list of `{account_ref, debit, credit, currency, dimensions, source_doc, txn_date, posting_rule_id}`. | **Δ-1 (approved):** every GL instruction line is persisted **inside the posting transaction** to append-only `platform.finance_journal_intent` (tenant-scoped; unique per `(inventory_transaction_id, posting_rule_id, intent_line_no)`; linked to the posting `idempotency_key`; amount/qty/currency/account+segment payload/status RECORDED→ACKNOWLEDGED/REJECTED/SUPERSEDED) and exposed read-only via `GET /api/v1/finance/journal-intents`. The default `NullFinanceAdapter` leaves intents `RECORDED` and never fails posting; a future production adapter acknowledges/rejects via status transitions only. **No chart of accounts, no journals table, no period close.** |
| **Order management / customer master** | Selling, commitments, fulfillments and returns are documented inventory effects, but Sales Order Management is a different help area not in the source set. | **Δ-2 (accepted):** FS-00 publishes the versioned boundary stubs `order.demand@0.1.0` (inbound commit/release/fulfil/return commands + required-by dates) and `order.supply@0.1.0` (supply/demand read-model streams); FS-04+ consume them. Semantics behind the stubs: `commit(...)`, `release(...)`, `fulfill(...)`, `return_receipt(...)`; events `inventory.committed/changed/fulfilled/returned`; `CustomerGroupResolver` for pricing. | Ledger accepts these commands via `/v1/inventory/commitments` and `/v1/fulfillments`; external order ids are opaque strings (`external_ref`), no FK to a customer/SO table. |
| **Identity provider — Dex broker + enterprise SSO (EXT, Δ-SSO)** | Human authentication is brokered by **Dex (OIDC)**; each tenant (company) can federate its own SAML 2.0 / OIDC / LDAP-AD IdP through a `tenant_sso_connection`. Dex authenticates; the platform authorizes and issues its own tokens. SCIM, IdP-initiated SAML, company-wide single logout and multiple IdPs per login are Phase-2. | `core.ports.identity.IdentityProviderPort` (`authorize`, `exchange_code`, `validate_id_token`, `subject_for(email)`); Dex OIDC client per connection; secret refs only | Local password (Argon2id) fallback for non-SSO tenants and platform-admin break-glass; enforced-SSO tenants reject local logins for members; audited; no IdP admin UI/SCIM endpoints. |
| **Carrier / shipping** | WMS shipping integration is explicitly deferred (integration class). | `wms.ShippingPort` (manifest/label hand-off) | Ship manifests recorded (`ship_manifest`); no carrier call. |
| **EDI / ecommerce / payroll** | Out of scope | none | — |
| **Currency/FX** | Valuation in base currency; FX revaluation not Phase 1 (PLAN §12.4). | `currency`/`exchange_rate` reference tables are read-only projections; posting stores `currency` + `exchange_rate` used. | No FX revaluation transactions; multi-currency sales prices supported, valuation always base. |
| **WMS mobile app / Manufacturing Mobile / Smart Count mobile / SCM Mobile** | **Permanently excluded.** Backend task/session records may exist so that a future client can drive them, but no device workflow, no scan protocol, no offline sync. | see `warehouse_cart`, `device_session` in DATA-MODEL (marked `EXT`) | Only server-side task queues/tasks; all mobile UI topics are deferred objects. |

---

## 2. Inventory posting model (the core)

The posting model is the system's load-bearing wall: every downstream capability (costing, planning, WMS, procurement, manufacturing, quality holds, reporting) is a workflow that eventually **posts** here. Two rules dominate the design:

1. **The ledger is append-only and is the system of record.** Balances, valuation, availability and plans are derived; they are never authoritative. Nothing in the ledger is updated or hard-deleted (framework §9). Corrections are compensating postings.
2. **One posting = one DB transaction = one atomic set of signed movements + one outbox batch.** A posting either fully happens or does not happen; there is no partially-costed receipt.

### 2.1 Canonical posting dimensions

A **posting key** is the minimal tuple that identifies a stock position. A **posting** is a set of signed movements over posting keys plus one cost/GL effect per movement.

| # | Dimension | Column (ledger line/detail) | Required for | Semantics and rules |
|---|---|---|---|---|
| D1 | **Tenant** | `tenant_id` | always | Never crosses tenants; every unique key and RLS policy begins with it. |
| D2 | **Item** | `item_id` | always | Must be an inventory-capable type for on-hand postings (inventory, assembly, WIP, lot/serial, matrix child); kits/groups are exploded to components (T-14); drop-ship/special-order never hit on-hand (T-28). |
| D3 | **Location** | `location_id` | all on-hand and in-transit postings | Per-line or header location; the *only* dimension that may be `NULL` is the `UNALLOCATED` bootstrap position (§2.8). Location carries bins-enabled/statuses-enabled/consignment flags. |
| D4 | **Bin** | `bin_id` (on `inventory_transaction_detail`) | when item uses bins **and** location is bin-enabled | Bin is an **on-hand-only** dimension: quantity is tracked per bin, but **cost and commitments are not** bin-scoped [iri-248..258, cross-cutting note "on-hand per bin (not cost/committed per bin)"]. |
| D5 | **Inventory status** | `inventory_status_id` | when statuses enabled; else tenant default | Availability-bearing dimension: status flags decide allocation/commit eligibility and WMS pick eligibility (`available_for_allocation`, `committable`, `sellable`). Status change is qty-neutral (T-30) [iri-205, iri-211, mpw-808, mpw-894]. |
| D6 | **Lot** | `lot_id` | lot-numbered items and any lot-tracked receipt | Lot carries expiry/manufacture dates (FEFO input [iri-311]) and specific cost identity. A lot is unique per tenant; quantity per lot is on-hand across locations. |
| D7 | **Serial** | `serial_id` | serialized items | Exactly one unit per serial movement; serial current location/status mirrored; serial history is append-only. Quantity mismatch between line qty and serial detail is rejected [iri-099]. |
| D8 | **UoM** | `uom_id`, `qty_uom`, `qty_base`, `conversion_factor` | always | The **base (stock) UoM is the ledger unit**; transaction UoM and conversion factor are stored for audit; all quantities posted are `NUMERIC(38,12)` base units (framework §3). Reorder/availability display in stock units [iri-073]. |
| D9 | **Currency** | `currency_code`, `exchange_rate`, `value_base`, `value_txn` | all value-bearing postings | Valuation in tenant base currency; transaction currency/rate captured. FX revaluation is out of Phase 1 (PLAN §12.4). |
| D10 | **Ownership** | `ownership` ∈ {`OWNED`,`CONSIGNED`} | always (default `OWNED`) | Consigned and owned quantities are separate positions with separate values, even in the same bin/status [iri-323]. Ownership change is a posting (T-22) and is prohibited to be simulated via status change [iri-323]. |
| D11 | **Cost object** | derived: (`item_id`,`location_id` or `location_costing_group_id`, `lot_id`/`serial_id` when specific) | all value postings | Determines which unit cost applies. Group-average uses the location's costing group, not the item alone [iri-064]. |
| D12 | **Date dimensions** | `transaction_date` (effective), `posting_at` (commit timestamp), `fiscal_period_id` | always | Ordering for costing is `(transaction_date, ledger_sequence)`; the append order is `ledger_sequence`. See §2.6. |
| D13 | **Source reference** | `source_doc_type`, `source_doc_id`, `source_line_id`, `external_ref` | always | Idempotency and traceability; `external_ref` carries opaque external order/return ids across the `BOUNDARY` (§1.6). |
| D14 | **Segments (finance)** | `class_ref`, `department_ref`, `subsidiary_ref` | optional | Opaque finance-port references (documented use on manufacturing transaction lines [mpw-085]); never used for inventory logic. |
| D15 | **Quantity class** | `qty_class` ∈ {`ON_HAND`,`IN_TRANSIT`} | always | In-transit is a separate quantity class at the **destination** location, not a bin: TO fulfilment removes source on-hand and raises destination in-transit (T-10); TO receipt converts in-transit → on-hand (T-11) [iri-233]. |
| D16 | **Movement classification** | `movement_type`, `direction`, `reason_code_id` | always | Drives GL instruction selection (§2.3) and adjustment reason reporting [mpw-815]. |

**Not posting dimensions** (kept outside the ledger key): commitment/reservation state (own tables, §2.5), bin-putaway tasks (WMS state), PO/WO line open quantities (document state), planning buckets (planning tables). This keeps the ledger key small and the hot path predictable.

### 2.2 Sign conventions (normative)

- **Quantity delta** is signed on the *position*: `+q` increases the position, `−q` decreases it. A position is `(tenant, item, location, status, ownership, qty_class, lot, serial)`; bin is a sub-position. A transfer is always **two movements**: `−q` at source key and `+q` at destination key; a status/bin change is likewise a movement pair.
- **Value delta** is signed in **base currency**, and is positive when inventory asset value increases. For a movement: `value_delta = qty_delta × unit_cost_used`, rounded per §3.8; the value of a *pair* of movements (transfer, status change, bin move) nets to zero at company level.
- Every posting command produces a `posting_plan` containing: header (type, dates, source doc, idempotency key), `movements[]` (signed qty + dimensions), `value_movements[]` (derived by the costing engine inside the transaction), `gl_intents[]` (finance port), `events[]`, and `balance_updates[]` (ordered, deduplicated).
- `INV-01 ledger quantity conservation`: for any posting, `Σ movements.qty_delta = 0` **except** for the balance classes that genuinely change inventory (receipts +, issues −). Formally: for each posting, `Σ(ON_HAND deltas) + Σ(IN_TRANSIT deltas) = net external qty change`, and for pure moves (transfer/receipt pair, bin/status/ownership) the sum is 0.
- `INV-02 value conservation`: `Σ value_delta` over a posting equals the posting's net external value change; for pure moves it is 0.

### 2.3 Posting transaction-type matrix

The matrix covers every in-scope document/transaction that writes (or must be proven not to write) inventory/cost movements. `GL instruction` cells name a **posting-rule id** configured with account references; the finance port receives the intent (no GL tables in Phase 1). `Qty` cells use: `+` into on-hand, `−` out of on-hand, `T+`/`T−` into/out of in-transit, `C+`/`C−` consigned ownership. All values in base currency; "cost method" refers to the item's method (§3.2).

| T | Posting type (source document) | Signed qty effects | Value and costing behaviour | GL instruction (finance port) | Events / downstream |
|---|---|---|---|---|---|
| **T-01** | `item_receipt` — PO receipt (full/partial; Advanced Receiving; deferred posting variant) [mpw-699, mpw-701, mpw-703, mpw-856] | `+q` ON_HAND at dest location/bin/status/lot/serial, ownership OWNED; PO line `qty_received += q`, open `on_order −= q` | `+` value at **receipt cost** (not bill cost) [iri-037]; average → new moving average; FIFO/LIFO → new cost layer; standard → at standard + purchase-price variance; specific → layer per lot/serial | `RCPT`: Dr Inventory Asset · Cr Accrued Purchases (std: Dr Inventory@std, Dr/Cr Purchase Price Variance) | `inventory.receipt.posted`, `po.receipted`, `cost.layer.created`, `inventory.balance.changed` |
| **T-02** | `item_receipt_reversal` — reverse a posted receipt [mpw-858] | `−q` ON_HAND same key; `on_order += q` if unbilled | `−` value by replaying the original receipt's layer/consumption trail; if already consumed, consume remaining layers or reject | reverse of `RCPT` | `inventory.receipt.reversed`, `po.open_restored` |
| **T-03** | `vendor_return_shipment` — vendor RMA ship [mpw-570..mpw-580] | `−q` ON_HAND from source key; RA line `qty_shipped += q` | `−` value at item method cost; standard → at standard, variance on credit | `VRET`: Dr Accrued Purchases / Return Clearing · Cr Inventory Asset | `vendor_return.shipped`, `inventory.issued` |
| **T-04** | `vendor_bill` — vendor bill (matched to receipt; or bill-before-receipt per tenant mode) [mpw-453, mpw-683, mpw-478] | 0 qty when receipt-matched; `+q` only in non-Advanced-Receiving mode (bill creates inventory) | **Never changes item costing** [iri-037]: compares bill price/qty/FX to receipt; residual to Accrued Purchases + bill variances | `BILL`: Dr Accrued Purchases ± variance · Cr Accounts Payable | `vendor_bill.posted`, `vendor_bill.variance_created` |
| **T-05** | `vendor_credit` — vendor credit [mpw-581] | 0 qty (return-linked credits pair with T-03) | value reduction: against accrual/AP; price-only credit on received goods → inventory cost adjustment (triggers recalc) | `VCRED`: Dr Accounts Payable · Cr Accrued Purchases / Inventory Asset / Expense | `vendor_credit.posted` |
| **T-06** | `vendor_bill_variance` — quantity/price/exchange-rate variance posting [mpw-478, mpw-479, mpw-480] | 0 qty (qty variance is a value-only reclassification) | reclass accrual vs variance accounts at configured tolerances | `BILLVAR`: Dr/Cr respective variance accounts · Cr/Dr Accrued Purchases | `vendor_bill.variance_posted` |
| **T-07** | `inventory_adjustment` — incremental adjustment (signed) [iri-235, iri-221] | `±q` ON_HAND at chosen key; serialized/lot items **must** use this form [iri-235] | `±` at entered/current unit cost; + creates layer, − consumes layers; standard → standard ± adjustment variance; group average → recalcs group | `ADJ`: Dr/Cr Inventory Asset · Cr/Dr Inventory Adjustment (reason-code account) | `inventory.adjusted`, `inventory.balance.changed` |
| **T-08** | `inventory_worksheet` — absolute reset [iri-235, iri-222] | delta = counted − system → same effects as T-07 | same as T-07; **prohibited** for lot/serial [iri-235], group-average items [iri-064], consigned items [iri-323] | `ADJ` (worksheet variant) | `inventory.adjusted` (worksheet id) |
| **T-09** | `inventory_transfer` — one-step transfer between locations [iri-235] | `−q` source ON_HAND, `+q` dest ON_HAND; lot/serial must match quantities | value moves with stock; no company-level gain/loss; specific cost carried per serial/lot | none (same legal entity) or `XFRMEMO` memo | `inventory.transferred` |
| **T-10** | `transfer_order_fulfillment` [iri-233, iri-236] | `−q` source ON_HAND; `+q` destination **IN_TRANSIT** (`T+`) | value out of source into in-transit at source cost | `XFROUT`: Dr Inventory In Transit · Cr Inventory Asset (source) | `to.fulfilled`, `inventory.in_transit.changed` |
| **T-11** | `transfer_order_receipt` [iri-233, iri-238] | `T−` at destination, `+q` ON_HAND at destination bin/lot/serial | value from in-transit into destination; no revaluation | `XFRIN`: Dr Inventory Asset (dest) · Cr Inventory In Transit | `to.received` |
| **T-12** | `sales_fulfillment` — item fulfillment [iri-260, iri-261, iri-263] | `−q` ON_HAND; commitment `−q` released; consigned lines are converted first (T-22) | COGS at item method cost; average/FIFO/LIFO per §3; underwater path if allowed (§2.7) | `COGS`: Dr Cost of Goods Sold · Cr Inventory Asset | `fulfillment.posted`, `inventory.issued`, `cogs.recorded` |
| **T-13** | `sales_return_receipt` — customer return into stock [iri-063, iri-229] (`EXT`: RMA doc from order boundary) | `+q` ON_HAND at return status/location (often non-sellable status) | at original cost when serial/lot-traceable; else current method; standard → standard ± return variance; multi-location return costing rules apply [iri-229] | `RET`: Dr Inventory Asset · Cr COGS / Return Variance | `return.received`, `inventory.returned` |
| **T-14** | `kit_group_fulfillment` — kit/group component issue [iri-116, iri-117, iri-118] | for `GROUP`: `−q` each member (group never stocked); for `KIT`: `−q` each component; kit has no own balance | COGS = Σ component costs; kit/group own price is a pricing concern | `COGS` per component | `component.issued` |
| **T-15** | `assembly_build` [mpw-016, mpw-005] | `+q` assembly ON_HAND (lot/serial output when numbered); `−q` each component per BOM/yield [mpw-079]; phantom exploded [mpw-011] | component cost out; assembly in at build cost (components + conversion/routing charges [mpw-184]); standard → at standard + production variances [iri-050] | `BLD`: Dr Inventory Asset (assembly) · Cr Inventory Asset (components), ± conversion/variance | `assembly.built`, `component.consumed` |
| **T-16** | `assembly_unbuild` [mpw-017, mpw-019, mpw-020] (+ Δ-REUSE per-line disposition, EXT) | `−q` assembly; `+q` components restored per build snapshot; per line `disposition=RETURN_TO_STOCK` (default) returns to `return_status_id` (optional USED/REFURB) / `SCRAP` returns no stock / `HOLD` returns non-committable | reverse of build at `value_policy=ORIGINAL_COST` (default) or NRV/MANUAL; cost differences to **Unbuild Variance** account [mpw-020]; SCRAP value posts to scrap/variance via FS-05; used components default non-sellable/non-committable statuses | `UNBLD` (reverse build) + per-line disposition/valuation instructions; scrap/variance lines where applicable | `assembly.unbuilt` (payload includes per-line dispositions), `unbuild.disposition.recorded` (used/scrap/hold/NRV/manual), `inventory.returned` | assembly/component items, location, lot/serial identity preserved, condition/return status, target location/bin | unbuild exempt from compatibility re-validation; returned components still in recorded status/qty for reversal; no partial reversals in Phase 1 |
| **T-17** | `wip_issue` [mpw-144] | `−q` components ON_HAND (WIP location), no FG | component cost into WIP asset; standard components at standard | `WIPISS`: Dr WIP Asset · Cr Inventory Asset | `wip.issued` |
| **T-18** | `wip_completion` (incl. backflush) [mpw-146, mpw-150, mpw-151] | `+q` FG ON_HAND; backflush also `−q` remaining components | WIP → FG; average assembly posts last-purchase-price estimate; variance between assembly and component cost [mpw-152]; standard uses WIP account not date | `WIPCMP`: Dr Inventory Asset (FG) · Cr WIP Asset ± variance | `wip.completed`, `wip.backflushed` |
| **T-19** | `work_order_close` [mpw-147] | none (scrap/over-issue write-offs, if configured, are T-07/T-18 sub-effects) | reconciles remaining WIP value; posts final production variance | `WIPCLS`: Dr/Cr WIP Asset · Cr/Dr Production Variance | `work_order.closed` |
| **T-20** | `count_variance_adjustment` — approved count variance [iri-223, mpw-881, mpw-882] | per line delta = counted − snapshot at key incl. bin/lot/serial/status/ownership; no worksheet/serial shortcuts | at count/current cost; standard at standard + variance; count default variance account [iri-209] | `CNTVAR`: Dr/Cr Inventory Asset · Cr/Dr Count Variance | `count.approved`, `inventory.adjusted`, `count.variance_posted` |
| **T-21** | `consigned_receipt` [iri-325, iri-323] | `C+ q` ON_HAND ownership CONSIGNED at consigned status | consigned value tracked, no vendor liability until consumed | `CONSRCPT`: Dr Consigned Inventory · Cr Consigned Inventory Offset (no AP) | `consigned.received` |
| **T-22** | `consigned_ownership_transfer` — consume/ownership transfer on sale [iri-323, iri-329, iri-331] | `C− q`, `+q` OWNED same key; then issue for fulfillment | ownership moves at consignment cost; vendor bill/payable generated at that moment ("pay only when sold") | `CONSOWN`: Dr Inventory Asset (owned) · Cr AP; Dr Consigned Offset · Cr Consigned Inventory | `consigned.owned`, `vendor_bill.created(consignment)` |
| **T-23** | `consigned_adjustment` — consigned adjustment/count [iri-326, iri-327] | `C± q` at consigned key | value delta at consigned cost; no AP | `CONSADJ`: Dr/Cr Consigned Inventory · Cr/Dr Consigned Adjustment | `consigned.adjusted` |
| **T-24** | `standard_cost_revaluation` [iri-048, mpw-152] | 0 qty | revalues on-hand at new standard (and WIP on partially built WOs, one revaluation txn per WO [mpw-152]) | `STDREVAL`: Dr/Cr Inventory Asset · Cr/Dr Standard Cost Revaluation | `standard_cost.revalued` |
| **T-25** | `cost_adjustment` — system COGS adjustment / recalc true-up [iri-056, iri-059] | 0 qty | corrects COGS/inventory from recalculation or underwater true-up; absorbs rounding residuals | `COGSADJ`: Dr/Cr COGS Adjustment · Cr/Dr Inventory Asset | `cost.adjusted`, `cost.recalculation.completed` |
| **T-26** | `landed_cost_application` [mpw-716, mpw-722, mpw-733] | 0 qty | allocates freight/duty/charges to receipt lines/layers by weight, quantity, amount or per line; actual landed cost | `LANDED`: Dr Inventory Asset · Cr Landed Cost Accrual/AP | `landed_cost.applied`, `cost.layer.updated` |
| **T-27** | `estimated_landed_cost_accrual` [mpw-734, mpw-735] | 0 qty | estimate accrued at receipt from template; reversed/replaced by actual | `LANDEDEST`: Dr Inventory Asset · Cr Estimated Landed Cost Accrual (and reversal) | `landed_cost.estimated` |
| **T-28** | `drop_ship_fulfillment` — drop-ship / special-order flow [mpw-684] | none to on-hand (item never stocked); qty is tracked on the linked PO/SO lines only | COGS at vendor cost; no inventory asset movement; AP via bill | `DROPSHIP`: Dr COGS · Cr AP / Accrued Purchases | `dropship.fulfilled`, `po.billed` |
| **T-29** | `bin_transfer` [iri-256] | within one location: source bin `−q`, dest bin `+q`; lot/serial preserved | none (value unchanged) | none | `bin.transferred` |
| **T-30** | `inventory_status_change` [iri-205, iri-211, mpw-894] | qty-neutral: status A `−q`, status B `+q` at same bin/lot/serial | none; affects availability/allocation/commit eligibility | none | `inventory.status_changed`, `inventory.availability.changed` |
| **T-31** | `import_batch` rows — opening-balance / adjustment-import batch wrapper (`EXT` import tooling; `import_batch`/`import_batch_row`) | per row: `+q` for opening-balance rows, `Δ(±qty ON_HAND)` for adjustment rows, worksheet delta for absolute rows; rows are grouped by resulting posting type | cost from row/unit cost (opening) or item method (adjustment); each row is costed as its posting type T-01/T-07/T-08 | per-row `RCPT`/`ADJ`/`WORKSHEET` intent | `import.row_posted`, `inventory.received/adjusted` per row | row dimensions complete (item, location, status, ownership, bin/lot/serial as configured) | batch fully validated before posting; per-row idempotency; opening-balance rows only in `bootstrap_mode` before live operations; row failures do not roll back posted rows |

**Non-posting control and quantity-only commands** (they never append ledger rows; they mutate document/reservation state and emit events):

| C | Command | Effect | Constraints |
|---|---|---|---|
| **C-01** | `commit_orders` — commit inventory to external demand [iri-264, iri-265, iri-266] | `inventory_commitment` rows + `inventory_balance.qty_committed += q`; optional lot/serial/bin allocation | available must suffice (`INV-04`); cannot commit underwater [iri-216]; status must be committable; auto-commit by order-entry order and reallocation supported [iri-266] |
| **C-02** | `release_commitment` | releases commitment; available rises | commitment must be active; idempotent |
| **C-03** | `reallocate_items` | moves commitments between external order lines in priority order [iri-266] | no negative available; audited |
| **C-04** | `transfer_order_approve` [iri-235] | commits source stock and raises destination **on-order** (no ledger rows) | approval state machine; location role restriction |
| **C-05** | `transfer_order_close` [iri-239] | closes short/unfulfilled TO lines; releases commitments/on-order | must not close lines with in-transit qty |
| **C-06** | `work_order_void` [mpw-089] | cancels WO; releases component commitments; no stock impact | no issues/completions posted, or they must be reversed first |

> **Out of the posting matrix by scope decision:** intercompany transfers [iri-242] and non-arm's-length transfer pricing are OneWorld/subsidiary concerns (finance boundary) — recorded as `EXT` and deferred; they reuse C-04/T-09/T-10 mechanics with a `subsidiary_ref` on the intent. FX revaluation, GL journals, payments/prepayments and payment runs are finance-boundary items (PLAN §12) and do not post inventory.

### 2.4 Atomic posting algorithm (normative)

Every posting command executes the following **17 steps inside one serialisable-enough DB transaction** (`READ COMMITTED` + explicit row locks; retry on serialization/deadlock). Steps 1–6 are validations that can run before locks; steps 7–12 are the concurrency-critical core; steps 13–17 finalise.

| Step | Action | Failure behaviour |
|---|---|---|
| 1 | Authenticate (JWT access token or API key; **active tenant only from the verified `tid` claim**, membership `ACTIVE` and `tv`=`token_version` match), resolve tenant, enforce RBAC scope + data scopes (locations/classes) | 401/403, no state change |
| 2 | **Idempotency**: canonicalise request → SHA-256; `INSERT INTO idempotency_key` `ON CONFLICT DO NOTHING`; existing + same hash → return stored response (replay); existing + different hash → `409 IDEMPOTENCY_KEY_REUSED` | replay-safe |
| 3 | Lock and load the source document (`SELECT … FOR UPDATE`, lock rank 0); verify state-machine transition and feature flag | 404/409 `INVALID_STATE_TRANSITION` |
| 4 | **Static validation** (accumulate all errors): document/line fields; item type supports the operation; location/bins/status/lot/serial flags; UoM conversion exists; quantity sign rules; required reason code; permissions (e.g. approve vs post); attachment/approval prerequisites | 422 `VALIDATION_FAILED` with error list |
| 5 | **Build posting plan**: explode kits/groups/assemblies/phantoms; resolve matrix children; assign header/per-line location; convert all quantities to base UoM; compute signed movements; merge duplicate positions deterministically | 422 |
| 6 | **Date/period resolution**: `transaction_date`, `posting_at`, fiscal period via `FinancePort.get_period_status`; backdating rule (§2.6) | 409 `PERIOD_CLOSED` / flag |
| 7 | **Lock acquisition in rank order** (§2.4.4) for every movement item/cost object and every balance key, sorted deterministically | lock timeout → 503 retryable |
| 8 | **Post-lock revalidation** (authoritative): re-read balances/commitments/document remaining quantities; availability & negative-inventory policy; serial availability/uniqueness; lot quantity; WIP compatibility; group-average worksheet prohibition; "limit to committed" preference; over-receipt tolerance; closed-period guard | 409 `INSUFFICIENT_AVAILABLE`, `SERIAL_CONFLICT`, `POLICY_VIOLATION`, … |
| 9 | **Value computation** by the costing engine (§3): per movement unit cost, layer consumption/creation, rounding; build `value_movements` | 409 `COSTING_INCOMPLETE` (item cost not yet established) or 422 |
| 10 | **Append ledger**: `inventory_transaction` header (+ `ledger_sequence`), `inventory_transaction_line`, `inventory_transaction_detail` rows (no updates, no deletes) | any error → rollback |
| 11 | **Update derived balances** for the distinct positions, in sorted order (`INSERT … ON CONFLICT DO UPDATE` + explicit row locks); enforce `CHECK` constraints (`qty_available >= 0`, serial qty ≤ 1) | constraint violation → rollback + mapped error |
| 12 | **Update document/reference state**: source doc lines (`qty_received/fulfilled/billed/returned`), PO/WO/TO open quantities, statuses, `document_link` conversion rows | rollback |
| 13 | **Write cost artefacts**: `cost_ledger_entry`, `cost_layer_consumption`, `item_cost`, standard/variance rows (part of step 9 output, persisted here) | rollback |
| 14 | **Enqueue events + finance intents**: insert `outbox_event` rows (event per aggregate change, `aggregate_version` monotonic) and persist `platform.finance_journal_intent` rows (one per GL-instruction line, append-only, idempotent per posting; ARCHITECTURE §1.6 / DM-PLAT-11) | rollback |
| 15 | **Audit**: append `audit_event` rows for document state change and for privileged/sensitive fields | rollback |
| 16 | **Persist idempotency result**: response status/body reference; bump document `version` (optimistic) | rollback |
| 17 | **Commit**, then emit metrics/traces and schedule post-posting jobs (cost recalculation if backdated, planning invalidation, projection refresh) | job enqueue failure is retried by outbox/recovery job |

**Transaction boundary:** exactly one DB transaction per posting command. No nested separate commits, no "post ledger then async update balances". The outbox insert is inside the same transaction, so an event can never describe a posting that rolled back, and a committed posting always has its events durably queued (framework §3/§9 durability target).

#### 2.4.1 Idempotency contract

| Aspect | Rule |
|---|---|
| Key source | `Idempotency-Key` header, or body field for bulk imports; required on all POSTs that create or post documents. |
| Scope | `(tenant_id, endpoint_id, idempotency_key)` unique. |
| Fingerprint | Canonical JSON (sorted keys, normalised numerics/timezones) hashed SHA-256; stored with the key. |
| Hit behaviour | Same fingerprint → replay stored `{status, body, resource_id, headers}`; different fingerprint → 409; concurrent in-flight same key → 409/425 with retry hint (implemented as `in_progress` row + short advisory lock). |
| Retention | 7 days (configurable per tenant; framework §9 durability). |
| Second line of defence | Posted documents carry a unique `posted_transaction_id` (`UNIQUE`), so a document can be posted at most once per revision even if the idempotency record was pruned. |
| Event consumers | Must dedupe on `event_id` via `event_consumer_checkpoint`; handlers are upserts, never blind increments. |

#### 2.4.2 Reversal, void and no-hard-delete rules

| Rule | Content |
|---|---|
| R-01 | Ledger rows (`inventory_transaction*`, `cost_ledger_entry`, `cost_layer*`, `audit_event`, `outbox_event`) are **append-only**: `UPDATE`/`DELETE` is denied at the DB role level (trigger + privilege), not just by convention. |
| R-02 | A posted document is corrected by a **reversal posting** (`reversal_of_transaction_id` unique) or a compensating business document (vendor credit, count variance, revaluation). Reversal rows carry exact negated quantity/value movements and their own `ledger_sequence`. |
| R-03 | A document that never posted may transition to `VOID` (no ledger rows to reverse). A posted document may **not** be voided; it may be reversed if state allows. |
| R-04 | Reversal is only permitted when no **dependent, non-reversed** postings exist: ledger checks `document_link` dependants (e.g. a receipt with a matched bill needs the bill cancelled/credited first; a receipt whose serials were fulfilled cannot be reversed). Violation → `409 DEPENDENT_DOCUMENTS` listing them. |
| R-05 | Reversal value must equal negated original value on the **same dimensions**. If original cost layers were already consumed by later postings, the engine consumes the currently available layers/lots/serials and posts the remaining difference as `cost_adjustment` (T-25), flagged `reversal_cost_drift=true`; this is a monitored event (P2 alert). |
| R-06 | Reversal date = original `transaction_date` when that period is `OPEN`; otherwise earliest open period with `original_transaction_date` preserved and `period_shifted=true`. Finance port is asked; the ledger never decides period policy alone. |
| R-07 | Reversal triggers the same notifications/cascades as the original: events `*.reversed` including `reversal_of`; open-quantity restorations; cost recalculation from `min(original_date, reversal_date)`; commitment re-evaluation. |
| R-08 | Cancellation of an unposted document with approved state must be audited; approvals are superseded, never deleted. |
| R-09 | Bulk import failures never partially post: import batches commit per chunk with an idempotent cursor, and each row's posting is an independent idempotent command (see §5.10 import API). |

#### 2.4.3 Replay and reconciliation

| Mechanism | Design |
|---|---|
| **Event replay** | Relay can republish `outbox_event` rows by id/sequence range or aggregate (`POST /internal/outbox/replay`). Consumers are idempotent (checkpoint on `event_id`), so replay is safe. Replay never re-posts ledger rows. |
| **Balance rebuild** | `reconcile.balances` job streams the ledger grouped by position and compares to `inventory_balance`; mismatch ⇒ incident + audit; optional `repair=true` rewrites **read models only**. |
| **Cost rebuild** | `reconcile.costs` recomputes `item_cost` and open layers from `cost_ledger_entry` and compares; drift is a P1 (valuation integrity is the framework §9 zero-tolerance item). |
| **Document consistency** | `reconcile.open_quantities` recomputes PO open qty, WO remaining, TO in-transit, commitment outstanding from postings and compares to document state. |
| **Outbox health** | `/internal/deps` exposes outbox depth/oldest age; `reconcile.outbox` alerts if depth > threshold or oldest > 5 min. |
| **Serial/lot audit** | `reconcile.numbered` proves each serial has exactly one current position and every lot balance equals Σ lot details. |
| **Evidence** | Each run writes `job_run` + audit event + metrics; exit gates read the latest successful run (framework §5 L3, §9 consistency). |

#### 2.4.4 Locking order (deadlock-free by construction)

Lock **ranks** are globally ordered; a transaction may only acquire a rank equal to or higher than the highest rank it currently holds. Target acquisition time per posting is O(movements log movements).

| Rank | Resource | How | Why this rank |
|---|---|---|---|
| 0 | Source document row(s) | `SELECT … FOR UPDATE` by `(doc_id)` | Serialises posting of the same document (double-post guard) before any inventory locks. |
| 1 | Idempotency key | `INSERT … ON CONFLICT DO NOTHING` (no long hold) | Early dedupe; never blocks on user data. |
| 2 | **Cost-object advisory locks** | `pg_advisory_xact_lock(hashtext(tenant_id), hashtext(cost_object_key))`, acquired in **ascending numeric key order** for all items/cost groups in the plan | Serialises value computation per item/cost group without locking whole tables; ordering prevents A↔B deadlocks. |
| 3 | Balance rows | `SELECT … FOR UPDATE` sorted by `(item_id, location_id, status_id, ownership, qty_class, lot_id NULLS FIRST, serial_id NULLS FIRST)` | Sorted batch acquisition; plain row locks keep concurrent non-overlapping postings parallel. |
| 4 | Commitment rows | same sort key | Commit/release interleaves with posting. |
| 5 | Cost layers | under rank-2 advisory lock; ordered by `(layer_seq)` | Layer chain mutations are protected by the advisory lock; row order is belt-and-braces. |
| 6 | Number sequence / document counters | `UPDATE number_sequence … RETURNING` | Held last and for microseconds; avoids long sequences stalls. |

Rules: (a) read-only `SELECT` for validation before rank 0 uses no locks and **must be re-validated** after ranks 2–3; (b) multi-item postings (kit, assembly, transfer with several items) acquire rank-2 keys sorted by `md5(cost_object_key)` numeric value; (c) lock timeout 5 s, statement timeout 30 s, deadlock/serialization retry ×3 with jitter then `503 RETRYABLE`; (d) batch imports chunk by item range so a single item's cost lock is not held for a whole file. Concurrency tests C-01..C-08 (§7.2) assert no negative available, no lost updates, no deadlock storms.

### 2.5 Quantity model

| Quantity | Definition (documented source) | Stored where | Derived vs stored | Concurrency semantics |
|---|---|---|---|---|
| **On hand** | "Quantity currently stocked, including Quantity Committed" [iri-216] | `inventory_balance.qty_on_hand` (per position) + `inventory_balance_bin` | **Derived from ledger**, materialised for reads; nightly reconcile proves equality (`INV-03`) | Changed only via posting engine; row locked rank 3; may be negative only under policy §2.7 |
| **Committed** | "Quantity promised to customers on approved sales orders that are not yet fulfilled" [iri-216] | `inventory_balance.qty_committed` + `inventory_commitment` | Stored (updated by C-01/C-02/T-12/T-13) | `UPDATE … WHERE qty_available >= :q` semantics; cannot commit underwater [iri-216]; status must be committable [iri-211] |
| **Available** | "Quantity On Hand less Quantity Committed. Quantity Available is never tracked as a negative quantity" [iri-216] | `inventory_balance.qty_available` (**generated** `GREATEST(qty_on_hand − qty_committed, 0)`) | Derived, stored for index/reads | `CHECK (qty_available >= 0)`; all allocation commands go through the same guarded update |
| **On order** | "Quantity on approved purchase orders pending receipt from the vendor" [iri-216] | `inventory_balance.qty_on_order` read-model column updated by procurement/TO/WO events; source of truth = document lines | Derived | Updated by document state, not ledger; `reconcile.open_quantities` checks it |
| **To order** | "Preferred Stock Level less Quantity Available" [iri-216] | computed in `replenishment_calculation_line` | Derived (planning output) | Recomputed by AIM/planning job; never used for posting |
| **Back ordered** | "Quantity committed to sales for which there is no stock to fill the order" [iri-216] | `inventory_commitment.qty_backordered`; item-level `inventory_balance.qty_backordered` read model | Stored per commitment; derived at balance | Set when fulfilment cannot be filled; external order remains responsible for promising dates |
| **In transit** | "quantity in transit … may appear depending on features" [iri-216]; TO fulfilment moves value to Inventory in Transit [iri-233] | `inventory_balance.qty_in_transit` (class `IN_TRANSIT`) | Derived from ledger | TO fulfilment/receipt pair; `INV-05` in-transit never negative per TO line |
| **Allocated** (bin/lot/serial) | reservation of specific physical units to commitments | `inventory_commitment_allocation.qty` | Stored | `INV-06`: Σ allocations ≤ commitment qty; releasing commitment deletes/zeroes allocations via compensating rows |
| **Allocated** (supply allocation) | planning match of supply to demand by required-by date [iri-307] | `allocation_result.qty` (planning) | Derived (planning run) | Separate from commitments (§6.6); never posts |
| **Reserved for WO/TO component demand** | component demand from released WOs / TO commitments | `inventory_commitment` with `source_type=WORK_ORDER/TRANSFER_ORDER` | Stored | same guarded update path |

**Concurrency semantics (all quantities):** single writer = posting/commitment engine; no in-memory balance caches (framework risk R3); all aggregates updated by SQL arithmetic under row locks; readers see committed snapshot only; generated `qty_available` cannot be written directly. `INV-07`: for every position, `qty_on_hand = Σ signed ledger qty` and `qty_committed = Σ active commitment qty` (reconcile-enforced).

### 2.6 Date and period policy

| Concept | Rule |
|---|---|
| `transaction_date` | Business-effective date entered on the document; drives costing order and reporting period. Required. |
| `posting_at` | Server timestamp of commit; immutable. Audit trail anchor. |
| `document_date` | Date printed on the document; may differ from `transaction_date` (e.g. entered next day) — kept separate for audit. |
| Ledger ordering | `ledger_sequence` is a per-tenant monotonic bigint assigned at append; costing order is `(transaction_date ASC, ledger_sequence ASC)`, so a backdated posting reorders cost history, not ledger truth [iri-059]. |
| Period | `fiscal_period` is a **finance-boundary projection** maintained by `FinancePort`; open/closed status is owned by finance. Posting into a `CLOSED`/`LOCKED` period is rejected. |
| Backdating | Allowed while the target period is `OPEN`. If `transaction_date < latest_effective_date(item, cost_object)` the posting is marked `backdated=true`, the ledger still posts synchronously, and a `costing.recalculate` job is enqueued with lower bound = transaction_date (§3.4). |
| Closed-period correction | Rejected by default. Tenant flag `post_to_open_period_when_closed=true` allows posting to the earliest open period with `original_transaction_date` and `period_shifted=true`; the GL intent carries both dates so finance can decide [documented workaround: inventory adjustment worksheet in current period posting to Accrued Purchases, iri-037]. |
| Cost recalculation trigger | Any insert/removal of a cost-affecting movement dated before the item's latest costed date; reversal; cost/landed-cost/standard-cost change; backdated receipt-consumption. Async, idempotent, item-scoped (§3.4). |
| Period close assumption | Phase 1 **defines the interface only** (read period status, emit intents, block closed posting). It does not implement journals, trial balance, tax or close. A `fiscal_period` row exists so inventory can refuse invalid dates and so reporting can bucket by period. |
| Timezone | All persisted timestamps UTC; `transaction_date` is a `DATE` interpreted in tenant timezone for period assignment. |

### 2.7 Negative inventory and underwater policy

Documented behaviour: NetSuite warns when on-hand goes below zero, permits the transaction, and estimates cost from the last known purchase price; when stock is replenished the true cost is computed and an adjustment posts with the receipt [iri-263, iri-037]. Available is never negative and **items cannot be committed while underwater** [iri-216].

Phase-1 policy is a tenant setting `negative_inventory_policy`:

| Value | Posting behaviour | Costing behaviour | Emitted signals |
|---|---|---|---|
| `BLOCK` (default for fulfilment/issues) | Issue/fulfilment/return-to-vendor that would drive `qty_on_hand < 0` is rejected (`409 INSUFFICIENT_ON_HAND`); adjustments may still be negative if they are the correction | n/a | — |
| `WARN` (default for adjustments/imports) | Posting proceeds; response carries a warning; `negative_inventory` flag on transaction; alert event | estimated unit cost = last purchase price (documented fallback) or zero if no history; `cost_ledger_entry.estimated=true` | `inventory.underwater.detected` |
| `ALLOW` | Posting proceeds for all types (subject to role permission `inventory.allow_negative`) | as `WARN`; on subsequent replenishment the true cost is applied and the difference posts as **System COGS adjustment** (T-25) [iri-056, iri-263] | both above + `cost.underwater.trued_up` |

Hard rules regardless of policy: `qty_available >= 0` (generated + CHECK); commitment against underwater item rejected; any position with `qty_on_hand < 0` is surfaced in `v_negative_inventory`; underwater duration > tenant threshold raises an operational alert. Group-average items handle underwater fulfilment by borrowing the group average [iri-064]; group-average items **cannot** use inventory worksheets [iri-064].

### 2.8 Multi-location bootstrap and inventory distribution

- Locations are optional at the feature level but are **mandatory for every posting in Phase 1** (multi-location is on by default). The only exception is the migration/bootstrap state where pre-existing stock sits in the synthetic `UNALLOCATED` position (`location_id = NULL` sentinel resolved by a reserved location row with `is_unallocated=true`) [iri-200, iri-201, iri-202].
- **Bootstrap sequence:** (1) enable locations; (2) all prior stock is created at `UNALLOCATED` via an import batch marked `bootstrap=true`; (3) `inventory_distribution` document moves it — *simple* distribution moves 100% to one location, *manual* distribution splits quantities by location [iri-201, iri-202]; (4) negative inventory must be resolved **before** distribution (documented requirement); (5) `INV-08`: no balance may remain at `UNALLOCATED` once the distribution completes, and no posting may use `UNALLOCATED` afterwards.
- Location behaviour that the engine must honour: per-line locations on transactions [iri-206, iri-215]; item preferred location, per-location reorder point/preferred stock/safety stock and view of per-location on-hand/on-order/committed/in-transit/value [iri-227]; location hierarchy for reporting and roll-ups; location-based role restrictions [iri-215] and WMS location/bin topology [mpw-803, mpw-804]; non-available inventory settings per location [iri-205].
- Multi-location transfer semantics: one-step transfers move qty/value atomically (T-09); transfer orders add in-transit state, destination on-order on approval and commit at source [iri-233, C-04]; in-transit ownership rules (when title passes) are a tenant/transfer configuration flag mapped to finance `ownership_in_transit` [in-transit ownership topic]; consigned transfers move consigned positions only [iri-328].

### 2.9 Posting error model (domain codes)

| HTTP | Code | When | Retryable |
|---|---|---|---|
| 400 | `MALFORMED_REQUEST` | schema/type failure | no |
| 401/403 | `UNAUTHENTICATED` / `FORBIDDEN_SCOPE` | authn/RBAC/data-scope | no |
| 404 | `NOT_FOUND` | document/entity | no |
| 409 | `INVALID_STATE_TRANSITION` | state machine guard | no |
| 409 | `IDEMPOTENCY_KEY_REUSED` | same key, different payload | no |
| 409 | `INSUFFICIENT_AVAILABLE` / `INSUFFICIENT_ON_HAND` | availability/qty guards | no (re-read and retry with new quantity) |
| 409 | `POLICY_VIOLATION` | negative policy, group-average worksheet, WIP method mismatch, consigned restriction, over-receipt, closed period | no |
| 409 | `DEPENDENT_DOCUMENTS` | reversal blocked by dependants | no |
| 409 | `SERIAL_CONFLICT` / `LOT_CONFLICT` | numbered-inventory uniqueness/state | no |
| 409 | `COSTING_INCOMPLETE` | item has no usable cost and policy forbids estimation | maybe (after cost load) |
| 422 | `VALIDATION_FAILED` | field/aggregate validation (list of `{path, code, message}`) | no |
| 429 | `RATE_LIMITED` | quotas | yes |
| 503 | `LOCK_TIMEOUT` / `SERIALIZATION_FAILURE` | transient concurrency | yes (Retry-After) |
| 500 | `INTERNAL` | unexpected | may retry with idempotency key |

All errors use RFC 9457 `application/problem+json` with `type`, `title`, `status`, `code` (stable domain code), `detail`, `instance`, `trace_id`, and optional `errors[]`. Domain codes are enumerated in `contracts/errors.yaml` and are part of the public contract (semver). See §5.9 for the generic API error model.

---

## 3. Costing and valuation engine

### 3.1 Cost objects, state and value flow

- **Cost object key**: `(item, cost_scope)` where `cost_scope` = location for Average/FIFO/LIFO/Standard/Specific-by-location behaviour, or `location_costing_group` for Group Average, plus `lot`/`serial` identity where the method requires it (Specific/Lot Numbered). Cost state lives in `item_cost` (current unit cost + qty/value aggregates), `cost_layer` (FIFO/LIFO/specific layers) and `group_average_balance` (group aggregates).
- **Value flow per posting movement:** the posting engine calls `costing.value_movement(movement, cost_object_lock)`, which returns `{unit_cost, value_delta, layer_actions[], gl_intent[], estimated?}`. This happens **inside** the posting transaction, under the cost-object advisory lock (rank 2), so no two postings can compute cost for the same object concurrently.
- **Cost ledger is append-only:** every value effect is an immutable `cost_ledger_entry`; recalculation and corrections append new entries (T-25), never rewrite history. This is what makes the framework's "nightly reconcile must be exact" achievable.
- **Source of inbound cost**: the transaction that brings inventory in — receipt cost, adjustment cost, build cost, transfer-in carried value. The vendor bill does **not** re-cost received inventory [iri-037]; receipt-vs-bill differences post as accrued-purchases variances (T-04/T-06) and corrections use revaluation/adjustment paths.
- **Value equation (`INV-10`)**: for every cost object and every position, `inventory value = Σ signed inventory_transaction_line.value_base = Σ cost_ledger_entry.value` and `item_cost.qty/value` equal the rolled-up ledger. Reconcile proves it nightly.

### 3.2 Costing methods

| Method | Cost state maintained | Inbound (receipt/adjust-in/build) | Outbound (issue/fulfil/consumption) | Layer behaviour | Revaluation / recalculation | Constraints and documented notes |
|---|---|---|---|---|---|---|
| **Average (moving)** | running `qty`, `value`, `avg_cost` per cost object | `value += qty_in × unit_cost_in`; `avg = value / qty` when `qty > 0` | at current `avg`; `value −= qty_out × avg` | none | backdated insert recomputes average from that date forward [iri-059] | **Default method** [iri-037]; immutable after item save [iri-037] |
| **FIFO** | ordered layers per cost object | new layer at receipt cost/date | consumes oldest open layer by `(layer_date, layer_seq)`; spans layers if needed | yes (`cost_layer`, `cost_layer_consumption`) | insert/remove of dated txn recalculates layer consumption from that date [iri-059] | LIFO/FIFO interact with Advanced Receiving feature availability [iri-055]; not compatible with WIP assemblies [mpw-152] |
| **LIFO** | ordered layers | new layer | consumes newest open layer | yes | as FIFO | Not available in NetSuite AU edition [iri-037] — regional config flag, not an architecture change; not WIP-compatible [mpw-152] |
| **Group Average** | one `qty`/`value` per (item, location costing group); synced across member locations | every costing txn in any member location recomputes group average = Σvalue/Σqty [iri-064] | at group average; protects underwater fulfilments across group [iri-064] | none | recalculated on every costing-impacting transaction | requires Multi-Location Inventory [iri-064]; items are not assigned to groups — **locations** are; item falls back to Average when its location has no group [iri-064]; **inventory worksheets prohibited** [iri-064]; optional include-in-transit-value preference [iri-064]; group locations must share base currency [iri-064] |
| **Specific** | per serial (and per lot when lot-numbered) exact cost | cost entered/derived for that serial/lot | exact cost of the consumed serial/lot identity | implied (one layer per identity) | none (identity cost) | requires serialized/lot-numbered inventory [iri-085..iri-107]; WIP assemblies incompatible [mpw-152] |
| **Lot Numbered** | per-lot cost | lot receives cost per receipt; lot may carry specific cost | consumes specified lot's cost | implied per lot | recalc on backdated lot receipts | lot selection required on transactions [iri-107]; expiry drives FEFO [iri-311] |
| **Standard** | fixed standard per item (+ cost categories/templates/versions) | at **standard**; purchase price variance for actual-vs-standard [iri-037] | at standard | none | standard cost change ⇒ revaluation transaction (T-24) [iri-048]; WIP revaluation per open WO [mpw-152] | production/purchase/unbuilt variances [iri-038..iri-052]; WIP assemblies compatible [mpw-152] |

Method selection is per item and **immutable after the item is saved** [iri-037]; moving an item between methods requires a new item (domain rule, enforced in `item_inventory.costing_method` immutability trigger per DATA-MODEL).

### 3.3 Cost layers and layer consumption

| Element | Design |
|---|---|
| `cost_layer` | `(tenant, cost_object_id, layer_seq, source_txn_line_id, layer_date, qty_original, qty_remaining, unit_cost, value_remaining, status OPEN/EXHAUSTED/REVALUED, estimated_flag)`. FIFO/LIFO order = `(layer_date, layer_seq)` / reverse for LIFO. |
| `cost_layer_consumption` | `(layer_id, consuming_txn_line_id, qty, value, consumption_seq)`. Provides exact audit trail for reversals and recalculation; never updated. |
| Consumption algorithm | (1) order candidate layers by method; (2) consume `min(require, qty_remaining)` per layer, computing `value = round(qty × unit_cost, 12)`; (3) if all layers exhausted and quantity requirement remains: if policy allows negative, use estimated unit cost = **last purchase price** [iri-037/iri-263] and mark `estimated=true`, else reject `INSUFFICIENT_ON_HAND`; (4) final partial consumption uses `value_remaining` exactly (no residual); (5) layer residual rounding is held in `value_remaining` so `Σconsumption + value_remaining = original value` (`INV-11`). |
| Layer creation | receipts, positive adjustments, builds, returns-in, transfers-in (transfer normally carries existing layers rather than creating new valuation; in-transit keeps a reference to the source layer set), landed cost (adjusts layer value, not qty). |
| Transfer of layers | T-09/T-10/T-11 do not create new valuation layers: layer identity + `qty_remaining` are moved/associated to the destination cost object (for FIFO/LIFO) or value transferred (average/standard). `cost_layer.location_id` is nullable and replaced by `cost_layer_consumption` movements; simpler implementation: close source layer and open destination layer with same unit cost and `derived_from_layer_id` (implementation choice recorded in DATA-MODEL). |
| Negative positions | never store negative layers; a negative qty state is represented in `item_cost.qty < 0` plus estimated cost entries. When replenished, true-up (§3.7) appends T-25 entries. |

### 3.4 Recalculation engine

Documented rationale: when transactions are inserted into or removed from an existing series, NetSuite recalculates item cost history so averages/FIFO layers stay consistent by transaction date [iri-059]; adjustments on inventory adjustments are recalculated too [iri-060]; the cost accounting status is visible per item [iri-062].

| Aspect | Design |
|---|---|
| Trigger set | (a) backdated cost-affecting posting; (b) reversal of a cost-affecting posting; (c) landed-cost application/adjustment; (d) standard cost change (revaluation per item/location/open WO); (e) correction of receipt cost; (f) import batch with `recalculate=true`; (g) manual admin request (audited). |
| Scope | `(tenant, item_id, cost_scope, from_date)`. Only items actually affected are recalculated; multi-item documents enqueue several scoped jobs. |
| Algorithm | (1) load last **cost checkpoint** at/before `from_date` (period-end snapshot of cost state, rebuilt from ledger if missing); (2) stream `cost_ledger_entry`/ledger movements for the item ordered by `(transaction_date, ledger_sequence)`; (3) recompute expected unit cost and value movement per movement using the item's method, including layer consumption; (4) diff expected vs recorded; (5) append `cost_ledger_entry` correction rows + T-25 `cost_adjustment` postings grouped by cost object and accounting period; (6) update `item_cost`/layers to the recomputed state; (7) set `item_cost_status` COMPLETE / REQUIRES_ATTENTION. |
| Concurrency | one run per `(tenant,item)` enforced by advisory lock; chunked commits every N movements with a durable cursor in `job_run`; postings for the same item during recalculation are allowed but take the cost lock, so they serialise; a posting that starts while a recalc is pending marks `cost_provisional=true` only if tenant opted into provisional costing (default: block the affected item with `409 COSTING_INCOMPLETE`). |
| Idempotency | `recalc_run` key = `(tenant, item, from_date, ledger_high_watermark)`; re-running with same key is a no-op; rerun with newer watermark extends scope. |
| Bounds | max movements per run (configurable); max from-date distance; dry-run mode returns planned adjustments without posting (used by verifier and by exit gates). |
| Outputs | `cost_recalculation` job record, adjustments, events `cost.recalculation.completed`, updated `item_cost_status`, metrics (lag, adjustments amount, items affected). |
| Tests | golden corpus G-01..G-12 (§3.11) replayed through normal and recalculation paths must end at identical valuation. |

### 3.5 Standard costing, cost categories, rollup and variances

| Element | Design |
|---|---|
| **Cost categories** | Named cost elements (material, labour, overhead, freight, etc.) used to build standards and rollups [iri-042, mpw-157]. |
| **Inventory cost templates** | Template assigning cost categories/elements to item groups/types [iri-043] and manufacturing cost templates using charge rates [mpw-159]. |
| **Cost versions** | Effective-dated standard cost sets [iri-045]; only one current version per item/date; planned standards live in a version. |
| **Planned standard cost** | The expected standard per item/cost category, entered before activation [iri-046]; activation creates the effective `standard_cost` rows and triggers revaluation if on-hand exists [iri-048]. |
| **Rollup** | Multi-level BOM standard rollup: component standards + routing/conversion costs → assembly standard; produces `cost_rollup_run` + per-item lines; costed-BOM inquiry data [iri-047, mpw-031, mpw-046]; supports fully exploded vs top-level conversion costs [mpw-046]. |
| **Revaluation** | On standard change: T-24 revalues on-hand qty × (new std − old std) and WIP of partially built work orders (one revaluation transaction per WO; WOs not in Released/Built) [iri-048, mpw-152]. |
| **Variances** | Five documented families: (1) **purchase price variance** at receipt (T-01); (2) **vendor bill variance** qty/price/FX (T-04/T-06) [mpw-478]; (3) **production quantity variance** (component usage vs BOM standard); (4) **production cost variance** (conversion cost actual vs standard, cost category subtabs [mpw-055..057]); (5) **unbuilt variance** on unbuild [mpw-020]; plus (6) WIP standard revaluation when component standards change mid-order [mpw-152]. All post via T-18/T-19/T-16/T-24 GL intents to configured variance accounts. |
| **Constraints** | Standard is the only method where outbound cost does not depend on the date/sequence of receipts; WIP feature supports only standard and average assemblies (components may be any of LIFO/FIFO/standard/average/actual) [mpw-152]. Standard-cost assembly completion posts to the WIP account indicated for the order, not by date [mpw-152]. |

### 3.6 Landed cost

| Aspect | Design |
|---|---|
| Document | `landed_cost` header + lines (landed-cost item/category, amount, currency, allocation basis) linked to one or more item receipts/bills [mpw-716, mpw-722]. |
| Categories & allocation | Landed-cost categories and allocation by **weight, quantity, amount or per line** [mpw-716, mpw-733]; costs can be billed together with inventory or separately [mpw-725..mpw-730]; commission/tax variants [mpw-723, mpw-732]. |
| Posting | T-26 allocates the cost to receipt lines and updates existing cost layers/average state (value only, no qty); standard-cost items post variance instead of adjusting inventory value [mpw-731]. |
| Estimated landed cost | T-27 accrues estimates from `estimated_landed_cost_template` at receipt; actual application reverses the estimate and posts the true cost [mpw-734, mpw-735]. |
| Constraints | only against received quantities; cannot cross closed periods without period-shifted intent; allocation total must equal document total (`INV-12`); recalculates item cost forward from receipt date (§3.4). |
| Interface | `costing.landed_cost.apply(receipt_line_refs, allocations[])` called by procurement/vendor-bill posting in the same transaction. |

### 3.7 Multi-currency, precision and rounding

| Concern | Rule |
|---|---|
| Valuation currency | Tenant base currency. Every value movement stores `value_base`; transaction currency amount is stored for traceability (`value_txn`, `currency_code`, `exchange_rate`). |
| FX source | `exchange_rate` reference projection (finance boundary); receipt uses rate on receipt date; bill uses bill date; the difference is a **vendor bill exchange-rate variance** (T-06) [mpw-478, mpw-709], never an inventory re-cost. |
| FX revaluation | Out of Phase 1 (PLAN §12.4). Group-average locations must share base currency [iri-064]. |
| Quantity precision | `NUMERIC(38,12)` base UoM. UoM conversion result must be exact to 12 dp or rejected (`422 UOM_PRECISION_EXCEEDED`) when the item is `must_convert_exactly`. |
| Unit-cost precision | stored `NUMERIC(38,12)`; calculations use Python `decimal.Decimal` with 28-digit context; no floats anywhere in domain code (lint rule). |
| Value rounding | extended value per movement = `quantize(qty × unit_cost, 12)`; posting residual (Σ rounded movements − exact total) is assigned by **largest-remainder** to the movements with the largest fractional part, so `Σ value_delta` equals the exact expected total (`INV-13`). |
| Layer arithmetic | partial layer consumption value = `value_remaining × (consume_qty / qty_remaining)` quantized; final consumption removes the exact remaining value. |
| Presentation rounding | API can return 2-dp money views (`?money_dp=2`), never used for further arithmetic. |
| Rounding policy | tenant setting `rounding_mode ∈ {PER_MOVEMENT (default), PER_TRANSACTION, PER_DOCUMENT}`; mode is recorded on each cost_ledger_entry so replay is deterministic. |
| Adjustment residual | any unavoidable residual (`< 10^-9`) is posted as `cost_adjustment` line on the transaction and flagged `residual=true`; nightly reconcile asserts zero residual drift (`INV-14`). |

### 3.8 Underwater costing and System COGS adjustment

1. When a position is underwater (`qty_on_hand < 0`), outbound cost = **last known purchase price** (documented fallback [iri-037, iri-263]); if no purchase history, cost = 0 and the entry is flagged `estimated=true`.
2. When a receipt later brings the position to `qty >= 0`, the engine applies the true receipt cost to the previously estimated quantity and posts the difference as **System COGS adjustment** (T-25) [iri-056]. Period attribution: the adjustment is posted with the receipt's `transaction_date` (documented behaviour: the correction posts with the item receipt, which may shift cost between periods) [iri-263]; `cost_ledger_entry.adjusts_period` records the period whose original cost was estimated.
3. While a position is underwater, fulfilment of the item is rejected under `BLOCK`, warned under `WARN`, allowed under `ALLOW`; commitments are always rejected ("cannot commit items in an underwater state" [iri-216]).
4. Group-average items borrow the group average for underwater fulfilments, so a negative position at one location is costed at the group average rather than last purchase price [iri-064].
5. Reports/read models expose `estimated_cost=true` flags so consumers know which values may still change; the flag clears only when the true-up posts.

### 3.9 Costing invariants and golden corpus

`INV-10` value equation (ledger vs cost ledger vs item_cost); `INV-11` layer conservation (`Σconsumption + remaining = original`); `INV-12` landed-cost allocation total equality; `INV-13` posting-level value rounding identity; `INV-14` zero residual drift; `INV-15` no negative layer quantity; `INV-16` standard-cost inventory value = qty × active standard (± open variances); `INV-17` every movement has exactly one cost entry (no uncosted ledger line). Golden corpus (pytest fixtures, also used as exit-gate checks):

| Case | Scenario |
|---|---|
| G-01 | Purchase 20@10, 20@15, sell 5 → FIFO 10/unit, LIFO 15/unit, Average 12.50/unit (documented example [iri-037]) |
| G-02 | Standard receipt vs actual → purchase price variance; sell at standard |
| G-03 | Backdated sale inserted before a receipt → recalc cost history, adjusted COGS |
| G-04 | Underwater sale (negative on-hand) then receipt → estimated cost + System COGS adjustment |
| G-05 | Return of a serialized item → original serial cost restored |
| G-06 | Return of non-numbered item in multi-location → original location cost rules [iri-229] |
| G-07 | Transfer order: source cost → in-transit → destination; no value drift |
| G-08 | Group-average: receipt in location A changes cost in location B; underwater fulfilment in B |
| G-09 | Landed cost allocated by weight/quantity/amount/per-line across a receipt |
| G-10 | Standard cost change revalues on-hand and open WIP orders |
| G-11 | Assembly build/unbuild with yield/round-up and unbuild variance |
| G-12 | Recalculation of every above case reproduces identical final valuation |
| G-13 | **Multi-tenant user identity is EXT** beyond the SCM help: NetSuite accounts are separate and the SCM docs do not specify a SaaS identity/membership model. | No catalog-backed behaviour for identity/auth; FS-00 still covers its 4 catalog topics. | Marked EXT (addendum `MULTI-TENANT-USERS-ADDENDUM.md` §7); identity/membership/invitation/session acceptance is defined by the addendum §8; no feature-coverage change. |
| G-14 | **Capability-scope + role-template RBAC is a design choice (EXT)** beyond the SCM help: NetSuite documents role levels/permissions in a UI-centric way rather than an API-capability catalogue. | The 255-scope catalogue/7 templates/14 SoD pairs are our model; RBAC topics (WMS roles [mpw-797/798/800], inventory feature enablement [iri-198]) remain FS-00 coverage with no new catalog ids. | Marked EXT/ASSUMPTION (addendum `PERMISSIONS-ROLES-ADDENDUM.md` §0/§10); authoritative machine-readable catalogue `design/permissions.json`; no feature-coverage change. |
| G-15 | **Dex-brokered enterprise SSO / per-tenant IdP federation is EXT** beyond the SCM help (which documents account users, not SaaS SSO). | The SSO connection/link/group-map model and Dex deployment are our design; RBAC topics remain FS-00 coverage with no new catalog ids. | Marked EXT (`DEX-SSO-ADDENDUM.md` §0); reference deployment and acceptance in addendum §2/§9; no feature-coverage change. |

---

## 4. UoM, traceability, bins/status, matrix, kits

### 4.1 Units-of-measure engine

| Element | Design |
|---|---|
| `uom_type` (units type) | Named set with one **base unit** (stock unit); all conversions are within a type [iri-070, iri-072]. |
| `uom` | Unit belonging to a type; `conversion_factor` = quantity of base units per 1 unit; `base` flag marks the type's base unit. |
| `item_uom` | Per item: `stock_uom` (must be the type base), `purchase_uom`, `sale_uom`, `consumption_uom`, `default_uom`, optional units with explicit conversion overrides [iri-073]. |
| Conversion rule | `qty_base = qty_uom × conversion_factor(item_uom)`; conversion goes **through the type's base** only (no direct cross-unit table). Factor > 0; factor = 1 for base. |
| Rounding | Converted quantity is quantized to 12 dp; if `item_inventory.must_be_whole_base_unit=true`, a non-integral base quantity is rejected (`422 UOM_FRACTIONAL_BASE`). Never round before multiplying value. |
| Posting rule | All ledger quantities are base units; transaction quantities/UoM are stored for audit; unit cost is **always per base unit**. |
| Display/reorder | Reorder points, preferred stock, safety stock, availability and planning quantities are in **stock units** [iri-073]; API can return display quantities in another unit (read-only conversion). |
| Lot/serial + UoM | Numbered inventory is recorded in base units; conversions apply to transaction entry only [iri-074]. |
| Non-inventory items | Items without stock (service, non-inventory, drop ship, etc.) may carry sale/purchase units but never post inventory; their quantities are document-only. |
| Validation | UoM must belong to the item's units type; sale/purchase/consumption UoM must be defined for the item; conversion must exist (no implicit identity across types). |

### 4.2 Lot and serial traceability

| Aspect | Design |
|---|---|
| Identity creation | Lots are created at receipt/build (manual or auto-numbering, `EXT` for auto-numbering as SuiteApp [iri-108]); serials are created at receipt/build or via adjustment `add serial numbers` [iri-089]. |
| Uniqueness | `(tenant, item, serial_number)` unique; `(tenant, item, lot_number)` unique; serial quantity per position is exactly 1 (`INV-18`); a serial has exactly one current position. |
| Lifecycle | Serial: `AVAILABLE → COMMITTED → IN_TRANSIT → FULFILLED/SHIPPED`, plus `RETURNED`, `SCRAPPED`, `UNKNOWN` — derived from ledger + commitment state, with `serial_number_history` append-only events. Lot: `ACTIVE → DEPLETED`, plus expiry-driven FEFO eligibility, status-driven availability. |
| Per-location/serial status | Serial current location/bins/status are projections of the ledger; serial moves are always posted, never mutated [iri-088]. |
| Lot selection | Lot/serial required on every movement for numbered items; selection rules configurable (`FEFO` by expiry [iri-311], manual, specific); WMS prevents picking expired items [mpw-917]. |
| Quantity mismatch guard | Line quantity must equal the sum of lot/serial details (`INV-19`), documented guard [iri-099, iri-218 detail search]. |
| Assembly interaction | Serialized/lot assemblies build outputs create new serials/lots while consuming components [iri-095, iri-105]. |
| Consigned interaction | Consigned numbered items keep consigned ownership through lot/serial movements [iri-323]. |
| History | `serial_number_history` + ledger detail provide "customer serial history" data; external customer reference is opaque (`BOUNDARY`). |

### 4.3 Bins, advanced bins and inventory status

| Concern | Design |
|---|---|
| Topology | `location → zone → bin`; bins belong to one location; zones group bins for WMS strategies; bin has `bin_type` (storage/staging/receiving), capacity attributes, `is_pickable`, `is_active` [iri-252, mpw-804]. |
| Basic vs advanced | Basic bins track quantity per bin only; **advanced bin/numbered inventory** adds per-location bins, lot/serial in bins, and the inventory-detail subrecord per transaction line [iri-250, iri-251]. Phase 1 implements the advanced model; the basic mode is a tenant flag that hides numbering. |
| Inventory detail | `inventory_transaction_detail` is the subrecord: one row per (line, bin, lot, serial, status, ownership). Required when item uses bins/lot/serial/status, and its quantities must reconcile to the line [iri-218, iri-250]. |
| Value/commitment scope | Value and commitments are **not** maintained per bin; bin-level data is quantity only [catalog cross-cutting: bins track on-hand, not cost/committed]. |
| Putaway | Basic bin putaway worksheet [iri-254, iri-255] and WMS putaway tasks/strategies (§6 WMS) both write bin quantities via T-01/T-07/T-29. |
| Inventory status | Status registry with behaviour flags: `available_for_allocation`, `committable`, `sellable`, `is_consigned`, `is_default`; statuses drive allocation/commitment eligibility [iri-205, iri-211] and WMS pick eligibility [mpw-808, mpw-894]. |
| Status changes | Only via T-30 (qty-neutral pair) or by posting into/out of a status; consigned↔non-consigned status change is **prohibited**, ownership changes use T-22 [iri-323]. |
| Bin transfers | T-29 within a location; bin-aware transfer orders pick/pack via WMS [iri-256, mpw-867]. |

### 4.4 Matrix items

- A matrix has a **parent** (non-stocked matrix item carrying the option definitions and default pricing/costing) and **child** items that are real inventory positions (one per option-combination) [iri-120..iri-125]. Up to 2000 combinations are supported [catalog cross-cutting].
- Option definitions: `item_matrix_option` (e.g. Colour, Size) + `item_matrix_option_value`; `item_matrix_member` maps parent → child → option values. Child creation may be bulk/batch (import is `EXT`; Item Creation Assistant is integration-class and deferred) [iri-121/122].
- Posting: matrix children are ordinary items — no special ledger behaviour; parent never posts inventory. Transactions that reference the parent must expand to children before posting (`422 MATRIX_PARENT_NOT_POSTABLE` otherwise).
- Pricing: parent-level price/quantity-pricing rules propagate to children with sibling-aware quantity modes [iri-026, iri-027, catalog note "price/qty behaviour across siblings (by parent quantity)"].
- Matrix assemblies: an assembly parent generates assembly children with their own BOMs [mpw-007..mpw-010]; WIP/matrix interplay follows manufacturing rules.

### 4.5 Kits, groups, assemblies, phantoms

| Item behaviour | Stocked? | Posting behaviour | Pricing | Documented notes |
|---|---|---|---|---|
| **Kit/Package** | No (kit item itself) | Members are decremented at fulfilment (T-14); optional build step is `EXT` (not evidenced in the crawled catalog) | Kit has its own price | [iri-117] |
| **Item Group** | No | Members decrement on sale; group price is the sum of member prices | Group price = Σ members | [iri-116, catalog cross-cutting] |
| **Assembly** | Yes, built/unbuilt | T-15/T-16; own asset/COGS accounts; build cost tracked | Assembly price independent | [iri-119, mpw-016, mpw-017] |
| **WIP assembly** | Yes via WIP | T-17/T-18/T-19 with WIP asset accounts | standard/average only | [mpw-139, mpw-152] |
| **Phantom assembly** | No (explodes) | Components explode into parent build/WO; phantom itself never stocked | n/a | [mpw-011..mpw-014] |
| **Matrix parent** | No | Children are stocked | Parent rules propagate | [iri-120] |
| **Consigned inventory** | Yes, ownership CONSIGNED | T-21/T-22/T-23 | costs tracked separately | [iri-323] — **feature-level incompatibility**: assembly items and consigned inventory are mutually exclusive in NetSuite [iri-323]; Phase 1 enforces a tenant configuration guard rather than silently allowing both |

**Compatibility constraints for configured/assembled products** (attributes/slots/dual-scope rules, generic manufacturing-path gates) and **unbuild disposition/reuse semantics** are specified in **§4.7** (Δ-CFG/Δ-REUSE, EXT).

### 4.6 Item type taxonomy and posting eligibility

| Item type (documented) | Posts inventory? | On PO? | On SO/fulfilment? | Notes |
|---|---|---|---|---|
| Inventory [iri-084] | yes | yes | yes | base stocked type |
| Serial-numbered [iri-085] | yes | yes | yes | serial detail required |
| Lot-numbered [iri-100] | yes | yes | yes | lot detail required |
| Assembly [iri-119], WIP [mpw-142] | yes (built) | optional (allow assembly on purchase txn is a preference [mpw-006]) | yes | BOM required; build/unbuild |
| Kit/Package [iri-117]; Item Group [iri-116] | no (members yes) | kit components configurable | yes (explodes) | T-14 |
| Matrix parent/child [iri-120] | parent no, child yes | child yes | child yes | T-01..T-12 on child |
| Drop Ship [iri-110]; Special Order [iri-111] | no | yes | yes | T-28; linked PO; no stock |
| Non-inventory [iri-112]; Service [iri-113]; Download [iri-114]; Gift certificate [iri-115] | no | yes | yes | document/value only |
| Subtotal/Markup/Payment/Other charge/Description/Discount/Expense [iri-127..iri-133] | no | charges/expenses | charges/expenses | never inventory; amounts only |
| Landed-cost item [mpw-721] | no | yes (expense) | no | drives T-26 |
| Manufacturing charge item [mpw-158] | no | n/a | n/a | conversion cost input to T-15/T-18 |

---

### 4.7 Manufacturing compatibility engine (EXT, Δ-CFG) and unbuild disposition/reuse (EXT, Δ-REUSE)

NetSuite documents BOM/matrix/build/unbuild/variance but **not** a compatibility rule engine nor used-condition disposition. Both are **EXT/ASSUMPTION** capabilities, **FS-09 only** (no new feature set, no DAG/wave/coverage change); FS-09's provided contract is bumped additively `manufacturing.execution@1.0.0 → 1.1.0` (consumer ranges `>=1.0.0 <2.0.0` unchanged).

| Concern | Design |
|---|---|
| **Attributes** | `compat_attribute_def` = tenant-scoped typed attribute catalogue (TEXT/NUMERIC/BOOL/SELECT, unit, multi-valued, applicable item types, active) e.g. `socket`, `chipset`, `form_factor`, `pcie_slots`, `max_gpu_length_mm`, `psu_watts`, `tdp_watts`, `dimm_slots`, `rack_u`; `compat_attribute_value` stores typed values on any item/assembly (`uq(item, attribute)`), provenanced MANUAL/IMPORT/EXT. |
| **Slots & capacity** | `compat_slot_def` per assembly item or BOM revision: `slot_code`, `slot_type` (CPU_SOCKET/PCIe/DIMM/DRIVE/PSU/CASE/…), numeric `capacity`+unit, component attribute constraints, `units_per_component`, required flag, ordering. Capacity math (e.g. PCIe slots consumed per GPU, PSU watts, DIMM population, rack-U) is deterministic and unit-tested. |
| **Dual-scope rule sets (both apply)** | `compat_rule_set.scope_type` covers **assembly/BOM** scopes (`ASSEMBLY`, `BOM_REVISION`) **and** context scopes (`CONFIGURATION`, `ORDER`, `CUSTOMER`, `REGION`, `CHANNEL`). All matching ACTIVE/effective rule sets are evaluated with **AND** semantics; ordering is deterministic by **scope specificity → priority → rule code**, so an order/customer/region rule never replaces the assembly/BOM baseline — both constrain the configuration. Ordering must be input-order-independent (`INV-33`). |
| **Rule types** | `compat_rule` supports `REQUIRES`, `EXCLUDES`, `ONE_OF`, `MIN_QTY`, `MAX_QTY`, `CAPACITY`, `ATTR_EQ`, `ATTR_RANGE`, `SUBSET_OF`; subject = item/attribute/component group/item set; constraint expressions validated JSON with effective dates, severity and message code; rule/rule-set content is immutable once ACTIVE (retire + new version). |
| **Configurable enforcement** | Precedence **per-rule → rule-set `enforcement_mode` → tenant setting `manufacturing.compat.enforcement_default` (default HARD)**. HARD blocks the guarded action with `409 COMPATIBILITY_VIOLATION` (rule ids + offending components); WARN allows it, records an immutable violation on the validation record and emits `manufacturing.config.violation`. Override requires scope `manufacturing.compat.override` + mandatory `override_reason`, is tenant-configurable (`manufacturing.compat.allow_override`) and is audited and stamped on the record. |
| **Validation evidence** | Every validation (API gate or inline manufacturing path) persists an **immutable `compat_configuration`** record: `configuration_hash` (ordered components/attributes + rule-set versions), context (BOM/WO/BUILD/OUTSOURCED/ORDER/ECO/ATB), components/attributes snapshot, ordered matched rule sets/rules, result `VALID/WARN/HARD_FAIL/OVERRIDDEN`, violation count, override refs, timestamp. Same hash + versions ⇒ identical result (idempotent/replayable); records are L3-gate evidence. |
| **Generic applicability** | The gate applies to **every manufacturing/configuration path**: BOM/BOM-revision create/activate; matrix assembly generation (invalid option combinations rejected/flagged); phantom explosion (phantom-flattened components validated, phantom members still subject to parent rules); work-order create/update/release/firm; WIP issue/completion/close/backflush (revalidated at completion); non-WIP assembly build; outsourced manufacturing (subcontract BOM/output); ECO implementation; order/configuration boundary (`order.demand` config requests validated before demand commit, including customer/region/order rule sets); available-to-build (only valid configurations contribute). **Unbuild is exempt** (restores a previously valid build snapshot). Kits/groups are **not** manufacturing paths (out of scope; optional future hook). |
| **APIs & scopes** | Attributes `GET|POST /v1/manufacturing/compat/attributes`, `GET|PATCH …/{id}`, `GET|PUT /v1/items/{item_id}/compat-attributes`; rule sets `GET|POST /v1/manufacturing/compat/rule-sets`, `GET|PATCH|DELETE …/{id}`, `POST …/{id}/activate|retire`, `GET|POST …/{id}/rules`, `PATCH|DELETE …/rules/{id}`; slots `GET|POST /v1/manufacturing/compat/slots`, `PATCH …/{id}`; validation `POST /v1/manufacturing/configurations/validate|simulate`, `GET /v1/manufacturing/configurations/{id}`, `GET /v1/manufacturing/configurations`, `GET /v1/manufacturing/compat/report`. Scopes (**all assignable**): `manufacturing.compat.manage` (attributes/rules/slots), `manufacturing.compat.validate` (validate/simulate/read), `manufacturing.compat.override` (HARD override), `manufacturing.unbuild.dispose` (used/NRV/manual/HOLD dispositions); `admin` template gains them. Contract: `manufacturing.execution@1.1.0` additive; FS-09 grows to **42 tables / 115 endpoints / 24 events / 5 jobs / 3 migrations / 8 exit commands** (3 compatibility+disposition tests added before `record`). |
| **Unbuild disposition (Δ-REUSE)** | `assembly_unbuild_component` carries an optional per-line disposition: **`RETURN_TO_STOCK` (default)** (optional `is_used`, `condition_code_id`, `return_status_id` — USED/REFURB default when used, `target_location_id/bin_id`, `allow_resale`, `value_policy`); **`SCRAP`** (no stock return; component value to scrap/variance via FS-05; serial/lot consumed as scrapped); **`HOLD`** (returns to hold/non-committable status pending inspection). Used statuses are seeded **non-sellable/non-committable by default** (configurable per status). |
| **Unbuild valuation** | `value_policy = ORIGINAL_COST` (**default**) uses the original build cost; `NRV` or `MANUAL` (with `unit_value`) post the difference through the FS-05 revaluation/adjustment path **inside the unbuild transaction**. Policy configurable at tenant, assembly and per-line level; a per-line override requires `manufacturing.unbuild.dispose`. Serial/lot identities are preserved with lifecycle history (`IN_STOCK_USED`, `SCRAPPED`, `HOLD`); quantity/value conservation is enforced. |
| **Unbuild reversal & deferral** | Reversal requires returned components still in the recorded status/quantity and posts compensating movements; **partial reversals are not allowed in Phase 1**. `REWORK` is deferred to the FS-14 repair flow (FS-09 precedes FS-14; no new DAG dependency). Events: `assembly.unbuilt` payload extends with per-line dispositions; `unbuild.disposition.recorded` audit event when used/scrap/hold/NRV/manual is chosen. |

## 5. Platform cross-cutting design

### 5.1 Multi-tenant isolation

| Layer | Mechanism | Enforcement |
|---|---|---|
| Schema | `tenant_id UUID NOT NULL` on **every** tenant-owned table, first column of every PK/unique index; no shared global rows except reference data (`currency`, `uom_type/uom` seed, permission catalogue) and the **auth-only global `user_identity`** (documented EXT exception, Δ-MT). | migration convention + CI schema linter |
| Application | Tenant resolved from auth principal (`tenant_id` claim / API-key binding); a request-scoped context variable; SQLAlchemy `with_loader_criteria` + `before_flush` guard rejects any row whose `tenant_id` ≠ context. | unit test + code review gate |
| Database | PostgreSQL **RLS** on all tenant tables: `USING (tenant_id = current_setting('app.tenant_id')::uuid)`; connection checkout sets `SET LOCAL app.tenant_id`. A second role (migration/admin) bypasses RLS for maintenance only. `user_identity` is excluded from tenant RLS and reachable only via the auth service role. | `tests/security/test_tenant_isolation.py` — every endpoint, list + get + mutate, with cross-tenant ids → 404 (not 403, to avoid existence leaks) |
| Object storage | Attachment keys prefixed `tenant/<id>/`; signed URLs scoped by tenant. | storage adapter test |
| Events/jobs | Every event payload and job carries `tenant_id`; consumers verify context; jobs are per-tenant scoped. | contract test |
| **Identity model (EXT, Δ-MT)** | Two levels: global auth-only `platform.user_identity` (one row per human; no `tenant_id`) + tenant-scoped `platform.app_user` membership (one per `(tenant, identity)`, carries `membership_status`/`token_version`), plus `tenant_invitation`/`user_session`. Roles/data scopes stay per membership, never global. Single-tenant users are the default case (exactly one ACTIVE membership). | addendum `MULTI-TENANT-USERS-ADDENDUM.md` §2; `DM-PLAT-12/13/14`; auth-service-only access; identity never reachable through tenant business routes |
| **Active tenant context (EXT, Δ-MT)** | The active tenant comes **only** from the verified access-token claim `tid`; no header/query may override it. Every request/write revalidates membership `ACTIVE` and `token_version`; the request sets RLS `app.tenant_id`. | `INV-30`; token/membership tests; cross-tenant probes → 404 |

Decision: **both** application filters and RLS (defence in depth). RLS is the backstop against a missing `WHERE`; app filters give good errors and index usage. Tenant deletion is a controlled offboarding job (soft-state offboarding + retention policy), never a cascade in production. The two-level identity model is an **EXT** required to handle multi-tenant users (addendum §7); it adds exactly one global table and three tenant-scoped/auth tables, and **does not weaken** any isolation rule: identity/invitation acceptance/platform-admin paths are the only cross-tenant-aware code paths, are auth-service-only, and return no tenant business data. Tenant deletion/offboarding remains a controlled soft-state job and never cascades identity or ledger data.

### 5.2 Authentication, RBAC and data scopes

| Concept | Design |
|---|---|
| AuthN | Human login is **brokered by Dex (OIDC authorization-code + PKCE)**; the platform validates the Dex ID token then issues its own **access JWT** (15 min; claims `sub`=identity_id, `uid`=app_user_id, `tid`=tenant_id, `tv`=token_version, `jti`, `iat/exp`, `scopes`) + **rotating refresh token** (30 days, hashed at rest, tenant-bound). **API keys** for machine clients. Platform-local password (Argon2id) is retained as fallback/break-glass (Δ-SSO). Endpoints require auth except the **public allowlist (9)**: `/healthz`, `/readyz`, `/openapi.json`, `POST /api/v1/auth/login`, `POST /api/v1/auth/select-tenant`, `POST /api/v1/admin/invitations/{token}/accept`, `GET /api/v1/auth/providers`, `POST /api/v1/auth/sso/start`, `GET /api/v1/auth/sso/callback`; the **self-service allowlist (6)**: `POST /api/v1/auth/switch-tenant`, `POST /api/v1/auth/refresh`, `POST /api/v1/auth/logout`, `GET /api/v1/me`, `GET /api/v1/me/tenants`, `GET /api/v1/me/effective-permissions`; plus internal-network `/internal/deps`. Dex tokens are never accepted as platform bearer tokens. |
| RBAC (Δ-RBAC) | **Capability scopes:** one stable string `<domain>.<resource>.<action>` guards exactly one class of API action; the **authoritative catalogue is `design/permissions.json`** (FINAL v1.5.0: **255 scopes = 241 assignable + 13 service-only job/task/internal + 1 platform-only** `platform.tenant.manage`; alias `platform.user.manage`→`platform.users.manage` not seeded; includes Δ-SSO `platform.sso.manage/read` and Δ-CFG/Δ-REUSE `manufacturing.compat.manage`, `.validate`, `.override`, `manufacturing.unbuild.dispose`; 18 scopes `requires_approval`; owner feature set per scope). Tenant `permission_scope` rows are provisioned from the catalogue; scope names frozen/additive-only; service-only scopes never assignable. |
| **Role templates (Δ-RBAC)** | 7 seeded per tenant, non-editable and cloneable: `admin` (all 241 assignable scopes incl. SSO/compat/unbuild dispose scopes), `planner` (50), `buyer` (71), `warehouse_operator` (81), `cost_accountant` (57), `quality_inspector` (47), `readonly` (44). Exact scope lists in `design/permissions.json.role_templates`; templates are `role.is_template=true`, `role.is_editable=false`. |
| **Custom roles & lifecycle (Δ-RBAC)** | Tenant admins create/clone roles choosing a subset of **assignable** scopes (`POST /api/v1/admin/roles`, `POST /api/v1/admin/roles/{id}/clone`, `PUT /api/v1/admin/roles/{id}/permissions`, `PUT /api/v1/admin/roles/{id}/data-scopes`, inactivate via `DELETE` — never hard delete). Non-assignable/platform-only scope → `422 SCOPE_NOT_ASSIGNABLE`; SoD-conflicting pair in one non-admin role → `409 SOD_CONFLICT`; system templates edit/delete rejected (clone-only). |
| **Assignments & effective permissions (Δ-RBAC)** | `user_role` assignments are **effective-dated per membership** and never cross tenants; multiple roles combine as a union of capability scopes; effective scopes + data scopes are **resolved per request from the DB** (assignments, template/custom role permissions, API-key scopes) so role/permission changes take effect immediately — sessions are not force-refreshed on role edits (membership suspension/revocation still bumps `token_version`, Δ-MT). APIs: `POST /api/v1/admin/users/{id}/roles` (assign/remove; last-admin guard), `GET /api/v1/admin/users/{id}/effective-permissions`, `GET /api/v1/me/effective-permissions`, `GET /api/v1/admin/permissions` (catalogue list/filter incl. `version`). |
| **Route guards — deny by default (Δ-RBAC)** | Every non-public route declares **exactly one** catalogue capability scope; missing/duplicate/unknown scopes fail CI (`test_route_scope_coverage.py`, OpenAPI routes ↔ declared scopes) and unknown scopes fail startup. Only the explicit **public allowlist (9 entries)** and **self-service allowlist (6 entries)** omit a scope. Middleware order: authenticate (Dex ID token or local password / API key) → resolve membership/identity/tenant → membership status → capability scope → data-scope filter/posting validation → domain logic; denial is `403 FORBIDDEN_SCOPE`. Every FS exit gate adds its route-scope tests. |
| **Privilege-escalation & lockout guards (Δ-RBAC)** | Only `platform.role.manage` holders manage roles and only `platform.users.manage` holders assign roles; an actor can never grant a scope absent from the assignable catalogue; platform-only/service-only scopes can never be granted by a tenant role; system templates are clone-only; the **last active tenant administrator** cannot be suspended, deactivated or stripped of the admin role (`409 LAST_TENANT_ADMIN`); every role/permission/assignment/data-scope change is audited with before/after and actor. |
| **SoD policy (Δ-RBAC)** | **14 hard SoD pairs** in `design/permissions.json` (e.g. `procurement.po.create`↔`procurement.po.approve`, `inventory.adjustment.post`↔`inventory.adjustment.approve`, `quality.inspect`↔`quality.inspection.approve`) are enforced at **role assignment** (non-admin roles/templates cannot combine a pair) and at **approval time** requester≠approver for every approval-required document; posting revalidates its scope (entry/approval/post separation). Tenant setting `rbac.sod_enforcement=HARD` by default; `WARN` only downgrades custom-role-creation warnings — approval-time requester≠approver is never disabled. Admin template is the only break-glass role allowed to contain SoD pairs; all its assignment changes are audited. Policy API: `GET|PUT /api/v1/admin/sod-policy`. |
| **RBAC audit & verification (Δ-RBAC)** | Audit events for role created/updated/cloned, permission mapping changed, role assigned/removed, data scope changed, SoD override attempt, last-admin guard trigger (before/after + actor). Verification: `python tools/feature_sets.py permissions` validates `design/permissions.json` (unique scopes, 7 templates, non-admin templates free of SoD pairs, alias valid, 14 pairs; reports **255/7/14**, allowlists 9/6; exit 0/2); FS-00 exit gate adds `test_route_scope_coverage.py`, `test_rbac_roles.py`, `test_sod_policy.py`, SSO flow/isolation suites, FS-09 compat/disposition suites, plus per-FS scope tests (INV-31, INV-32, INV-33). |
| **Identity & membership (EXT, Δ-MT)** | Global `user_identity` holds email/credentials/external-IdP subject/MFA/platform-admin flag; per-tenant `app_user` is the principal (membership lifecycle `INVITED/ACTIVE/SUSPENDED`, `token_version`). Tenant roles/data scopes attach to the membership; exactly one membership per `(tenant, identity)` (`DM-PLAT-13`). |
| **Login / tenant selection / switch (EXT, Δ-MT)** | `POST /api/v1/auth/login` → one ACTIVE membership: token pair; multiple: `200 {status:"TENANT_SELECTION_REQUIRED", selection_token (5 min, identity-scoped), tenant_choices[]}`; none: `403 NO_ACTIVE_MEMBERSHIP`. `POST /api/v1/auth/select-tenant` (selection token), `POST /api/v1/auth/switch-tenant` (authenticated; new token pair, old session valid until revoked/expired; audit `auth.tenant_switched`), `POST /api/v1/auth/refresh`, `POST /api/v1/auth/logout`. `GET /api/v1/me` (identity + active membership + effective scopes), `GET /api/v1/me/tenants` (caller's own memberships only). |
| **Invitations (EXT, Δ-MT)** | `platform.tenant_invitation`: single-use 7-day hashed token, email-bound, intended role ids validated against tenant roles. Acceptance creates the identity if absent and exactly one `app_user` membership + `user_role` rows atomically; audit `membership.activated`; duplicate/expired/revoked/cross-tenant tokens rejected (`DM-PLAT-14`). |
| **Platform admin & tenant provisioning (EXT, Δ-MT)** | Global `is_platform_admin` only; scope `platform.tenant.manage`. `POST /api/v1/platform/tenants` (code/slug, name, base currency, timezone, bootstrap admin invite), `GET /api/v1/platform/tenants`, `GET|PATCH /api/v1/platform/tenants/{id}`, `POST /api/v1/platform/tenants/{id}/suspend|reactivate|offboard|bootstrap-admin`; provisioning seeds tenant settings/permission catalogue/system roles/number sequences/flags/fiscal-currency projections and emits `tenant.provisioned`; audited with `SYSTEM_TENANT_ID`; never grantable by a tenant role. |
| **Tenant user administration (EXT, Δ-MT)** | Scopes `platform.users.manage`, `platform.invitations.manage`, `platform.sessions.revoke`: `GET|POST /api/v1/admin/users`, `GET|PATCH /api/v1/admin/users/{id}`, `POST /api/v1/admin/users/{id}/revoke-sessions`, `GET|POST /api/v1/admin/invitations`, `POST /api/v1/admin/invitations/{token}/accept`, `POST /api/v1/admin/invitations/{id}/revoke`. Cannot create tenants or grant platform-admin. |
| **Membership revocation (EXT, Δ-MT)** | SUSPENDED/revoked membership or DISABLED identity bumps `token_version` and revokes all sessions; refresh fails immediately and writes revalidate membership status; access tokens expire within the ≤15-min TTL bound (`DM-PLAT-13`, `INV-30`). |
| **SSO discovery / start / callback (EXT, Δ-SSO)** | `GET /api/v1/auth/providers?email_or_tenant=` (public) returns enabled methods (local if allowed + zero/one/more SSO connections: tenant name/code, protocol, login URL; no tenant business data); `POST /api/v1/auth/sso/start` (public) resolves the connection by tenant code or verified email domain (ambiguity → explicit tenant choice), creates an OIDC authorization request (`state`, `nonce`, PKCE S256, 5-min TTL in Redis) and returns that connection's Dex authorization URL; `GET /api/v1/auth/sso/callback?code&state` (public) validates state/nonce (single-use, redirect-URI/tenant-bound), exchanges the code with Dex, validates the ID token (issuer, audience, signature via JWKS, exp/iat/nonce, `email_verified`), enforces domain policy, resolves/creates identity + link + optional JIT, maps groups→roles, audits, then issues the **platform** JWT + refresh session (or `TENANT_SELECTION_REQUIRED`). Typed errors: `SSO_PROVIDER_NOT_FOUND` 404, `SSO_DISABLED`/`SSO_ENFORCED_LOCAL_DENIED` 403, `SSO_STATE_INVALID` 400, `SSO_IDP_UNAVAILABLE` 503, `SSO_EMAIL_DOMAIN_NOT_ALLOWED` 403, `JIT_PROVISIONING_DISABLED` 403, `IDENTITY_LINK_CONFLICT` 409, `SSO_GROUP_MAPPING_INVALID` 422. |
| **Identity linking, JIT & group mapping (EXT, Δ-SSO)** | Link key `(tenant, connection, issuer, subject)`; known ACTIVE link → its identity; else if `email_verified` and `link_by_verified_email=true` and a global identity with that email belongs to the tenant → link; else JIT (only if `jit_provisioning=true` and domain allowed) creates exactly one `user_identity` + one ACTIVE `app_user` + `user_role` rows from `default_role_ids` + link — never other memberships. Group→role mapping applies matching `sso_group_role_map` rows in priority order; union bounded to **assignable tenant roles** (never platform-only/service-only scopes) and SoD-checked (`409 SOD_CONFLICT`); mapping/role diffs audited. Link revocation/connection disable blocks new logins and revokes sessions. |
| **Enforced SSO, local fallback & break-glass (EXT, Δ-SSO)** | Tenant setting `auth.local_login_enabled=false` (enforced SSO) rejects local-password logins for its members (`SSO_ENFORCED_LOCAL_DENIED`); platform-admin bootstrap/break-glass stays local and audited until operator SSO is configured; tenants without an ACTIVE connection keep the unchanged local Argon2id flow. Dex unavailability returns 503 + `Retry-After` with **no silent fallback to local auth** unless the tenant explicitly allows it. |
| **Dex operations & connection lifecycle (EXT, Δ-SSO)** | `tenant_sso_connection` lifecycle `DRAFT→TESTING→ACTIVE→DISABLED/ERROR`; provisioning generates Dex config (client = platform API; connector = tenant IdP; issuer; secrets from secret manager); `POST /api/v1/admin/sso-connections/{id}/test` runs discovery/JWKS/config health check before ACTIVE; config/issuer changes audited and re-tested; `rotate-secret` audited; cert/metadata-expiry monitoring job; one isolated Dex issuer per ACTIVE connection in prod (shared Dex dev/test). Secrets are refs only — **no endpoint returns client secrets/cert private material**. |
| **SSO administration API & scopes (EXT, Δ-SSO)** | Scopes **`platform.sso.manage`** (writes) and **`platform.sso.read`** (GETs), assignable and in the `admin` template. Tenant-admin endpoints: `GET|POST /api/v1/admin/sso-connections`; `GET|PATCH|DELETE /api/v1/admin/sso-connections/{id}`; `POST …/{id}/test`; `POST …/{id}/rotate-secret`; `GET|PUT …/{id}/group-mappings`; `GET …/{id}/links` (paginated, no secrets); `DELETE …/{id}/links/{link_id}` (revoke + session revoke); `GET|POST /api/v1/admin/users/{id}/identity-links`; `DELETE /api/v1/admin/users/{id}/identity-links/{link_id}`. All audited; RLS tenant-scoped. |
| Data scopes | `user_data_scope` restricts by `location`, `class`, `department`, `subsidiary` (documented location-based role restrictions [iri-215]); scopes combine per documented precedence, are orthogonal to capability scopes, and are enforced in the query layer (row filter) and posting validation (cannot post to out-of-scope location). |
| SoD (summary) | Full policy in "SoD policy (Δ-RBAC)" above: 14 hard pairs at assignment + requester≠approver at approval + posting-scope revalidation; approver limits enforced in `approval_request`; actor cannot approve own document (INV-31, DM-RBAC-03). |
| Service identity | Internal module calls use in-process contracts, not tokens; internal service principals carry the service-only `platform.internal` scope; tenant-admin endpoints require the Δ-RBAC administrative scopes; platform-admin endpoints require global `is_platform_admin` + `platform.tenant.manage`. |
| Secrets | env/secret store; never in DB or logs; API keys shown once. |
| Session/audit | `app_user.last_login_at` + `platform.user_session` (refresh sessions, rotation/replay detection/revocation); auth failures audit event; access tokens are stateless and become unusable at the next write/refresh within the ≤15-min TTL bound after `token_version` bumps or session revocation. |

### 5.3 Audit trail and immutability

- `audit_event` is append-only (`INSERT` only; DB trigger rejects UPDATE/DELETE) and records: `event_id, tenant, actor(id,type), action, resource_type/id, before/after diff (JSONB, sensitive fields redacted), request_id, trace_id, ip, user_agent, occurred_at, source (api/job/import/integration)`.
- Posting audit: every posting writes document state-change audit + ledger summary; approval actions are audit events and `approval_action` rows; **RBAC changes** (role created/updated/cloned, permission or data-scope mapping changed, role assigned/removed, SoD override attempt, last-admin guard trigger) are audited with before/after and actor (Δ-RBAC).
- Immutable columns: posted documents' key fields (quantities, prices, dates, dimensions) are immutable after posting; corrections are new documents/reversals. Enforced by DB trigger + application guard (DATA-MODEL §1).
- Retention: ledger/audit/outbox append-only for the contract retention horizon; GDPR-style erasure applies only to PII in non-ledger tables (`app_user`, `attachment_metadata`, `vendor`, `device_session`), via pseudonymisation, never to ledger amounts (finance retention). See §5.12.

### 5.4 Idempotency — see §2.4.1 (normative). Applies to: posting commands, document creation, imports, job triggers, webhook-style internal endpoints.

### 5.5 Number sequences

| Aspect | Design |
|---|---|
| Model | `number_sequence` per `(tenant, doc_type, scope, period_key?)` with `prefix`, `next_value`, `padding`, `reset_policy (NEVER/ANNUAL/MONTHLY)`, `gap_policy`. |
| Allocation | single `UPDATE number_sequence SET next_value = next_value + 1 WHERE … RETURNING next_value - 1` inside the posting transaction (lock rank 6); no external sequence service, no cached ranges (correctness over throughput; documented p95 budget does not need sequence batching). |
| Gapless vs non-gapless | Documents that never post may hold a number (non-gapless, default). `gap_policy=GAPLESS` is supported only for externally-regulated documents and is flagged per tenant; it requires allocating numbers at **post** time, not at draft creation. |
| Uniqueness | `(tenant, doc_type, document_number)` unique; numbers never reused, including after reversal/void. |
| Concurrency | Sequence row is the natural serialisation point; contention is bounded by document types (e.g. receipts per tenant), measured by posting p95 test. |
| Bulk | Imports may reserve blocks only if `gap_policy=NON_GAPLESS`; reservation is recorded so unused numbers are never reassigned. |

### 5.6 Document state machines and approval hooks

All documents share a common lifecycle skeleton; each document extends it with domain states (full per-document tables in DATA-MODEL §18):

```
DRAFT → PENDING_APPROVAL → APPROVED → RELEASED/OPEN → PARTIALLY_FULFILLED* → FULFILLED/CLOSED
   ↘ CANCELLED/VOID (only if never posted)         ↘ REVERSED (post-reversal only)
```

Universal hooks (implemented in `core.approval`):
1. **Approval requirement** resolved from `approval_definition` (document type, amount thresholds, department/class, vendor/item category) at submit time; result stored in `approval_request`.
2. State transition guards call `approval_ok(doc)`; posting calls additionally require the document to be in an approvable/released state.
3. Approval actions create `approval_action` rows (actor, decision, comment, at) + audit + outbox events; SoD validated.
4. Rejection returns the document to `DRAFT`/`REJECTED` with a reason; approved-then-edited documents re-enter approval when financially sensitive fields change.
5. Workflow engine: Phase 1 implements **declarative approvals only** (sequential or parallel-by-threshold rule sets). Full SuiteFlow-style workflow customisation is `BOUNDARY`/deferred; hooks are published so a later workflow engine can subscribe (`document.submitted`, `document.approved`).
6. Documented approval examples encoded: vendor bill approval (pending/approved, approval limits) [mpw-481..mpw-483], 3-way match with tolerance/difference limits [mpw-505..mpw-529], requisition/purchase-request approvals [mpw-638..mpw-651], PO approval [mpw-672], transfer-order approval, work-order release/firm [mpw-077, mpw-078], count approval [mpw-882], inventory count rejection with snapshot recalculation [iri-209].
7. **SoD policy (Δ-RBAC):** 14 hard conflict pairs from `design/permissions.json` are enforced at role assignment (non-admin roles cannot combine a pair) and at approval decision (requester≠approver for every approval-required document); posting revalidates its scope. See §5.2 "SoD policy" and INV-31.

### 5.7 Outbox and domain events

| Aspect | Design |
|---|---|
| Table | `outbox_event(event_id UUID, tenant_id, aggregate_type, aggregate_id, aggregate_version, event_type, event_version, payload JSONB, occurred_at, ledger_sequence?, correlation_id, causation_id, published_at, attempts, last_error)`. |
| Relay | `outbox-relay` claims batches (`FOR UPDATE SKIP LOCKED`), publishes to Redis Stream `evt.<domain>` with `event_id` as message id, marks `published_at`; on failure retries with backoff; poison messages to `evt.dlq` + alert. |
| Ordering | Per-aggregate order guaranteed by `aggregate_version` and relay ordering; cross-aggregate consumers must be out-of-order tolerant. Ledger downstream consumers may use `ledger_sequence` for strict global order when needed. |
| Schema | JSON Schema per `event_type@major.minor`, stored in `contracts/events/`; compatibility: additive within major (framework L1); registry validated in CI (jsonschema). |
| Envelope | `event_id, event_type, event_version, tenant_id, occurred_at, aggregate{type,id,version}, correlation_id, causation_id, data, metadata{producer, trace_id}`. |
| Consumers | Must be idempotent (`event_consumer_checkpoint` on event_id); retries; dead-letter + replay tooling. |
| PII | payloads carry ids, not personal data; names resolved by the consumer via API if needed. |
| **Finance journal intents (Δ-1)** | Persisted synchronously in the posting transaction into append-only `platform.finance_journal_intent` (one row per GL instruction line, linked to the posting `idempotency_key`; amount/qty/currency/account+segment payload; status RECORDED/ACKNOWLEDGED/REJECTED/SUPERSEDED). Read-only API `GET /api/v1/finance/journal-intents`; intents are **not** event-only (events remain the async integration surface), and replay-safe by their unique posting linkage. |
| Event catalogue (examples) | `inventory.receipt.posted`, `inventory.adjusted`, `inventory.transferred`, `inventory.fulfilled`, `inventory.returned`, `inventory.committed.changed`, `inventory.status_changed`, `instruction.*`, `assembly.built/unbuilt`, `wip.*`, `work_order.*`, `count.approved`, `po.receipted`, `vendor_bill.posted`, `vendor_bill.variance_posted`, `to.fulfilled/received/closed`, `cost.layer.created`, `cost.adjustment.posted`, `cost.recalculation.completed`, `standard_cost.revalued`, `landed_cost.applied`, `consigned.received/owned/adjusted`, `plan.generated`, `allocation.completed`, `atp.updated`, `wms.task.*`, `quality.inspection.*`, `document.submitted/approved/rejected/closed/reversed`, `auth.tenant_switched`, `membership.activated`, `tenant.provisioned`, `session.revoked`. |

### 5.8 Jobs and scheduler

| Element | Design |
|---|---|
| Model | `job_definition` (name, queue, cron, timeout, retry policy, concurrency key, handler id, enabled, payload schema) + `job_run` (status, started/finished, attempts, cursor/progress, result summary, error, correlation). |
| Execution | RQ worker consumes queues `default`, `costing`, `planning`, `heavy`, `outbox`; job handler resolved by name; each run takes a `pg_advisory_lock` on `(job_name, tenant)` when `singleton=true`. |
| Scheduler | In-process scheduler (api singleton leader election via advisory lock) enqueues cron jobs; missed runs are caught up once (documented). |
| Idempotency | `run_key` (e.g. `recalc:item:<id>:<watermark>`); duplicate enqueue is a no-op while running. |
| Chunking | Jobs process in chunks with committed cursors; restart resumes from cursor; no unbounded transactions. |
| Long-running | cost recalculation, planning runs, imports, reconciliation, outbox replay, projections refresh. |
| Observability | Every run emits metrics + `job_run` row; stuck-run watchdog; `/internal/deps` exposes queue depth and oldest pending run. |
| Retention | `job_run` retained 90 days (config); failed runs retained 1 year for audit. |

### 5.9 API conventions, versioning, errors

| Aspect | Decision |
|---|---|
| Protocol | REST + JSON, OpenAPI 3.1 generated from Pydantic v2; canonical schema in `contracts/openapi/`. |
| Versioning | Canonical URL base path is **`/api/v1`**; additive changes within a major version; breaking changes → `/api/v2` with overlap window. The `/v1/...` spelling in older prose is non-normative shorthand. Event schemas versioned independently (`@major.minor`, additive within major). |
| Resource style | Documents are resources with sub-resource actions under the canonical base: `POST /api/v1/purchase-orders`, `POST /api/v1/purchase-orders/{id}/approve`, `POST /api/v1/item-receipts/{id}/post`, `POST /api/v1/inventory-transfers`. Posting commands always carry `Idempotency-Key`. |
| Finance boundary read API | `GET /api/v1/finance/journal-intents?transaction_id=&source_doc_type=&status=&from=&to=` — cursor paging, tenant + data-scope filtered, read-only (intents are written only by the posting engine; no POST/PATCH/DELETE). |
| **Δ-MT auth/platform/admin route namespace (EXT)** | All routes use the canonical `/api/v1` base path: auth `/api/v1/auth/*`, current-user `/api/v1/me*`, platform admin `/api/v1/platform/*`, tenant admin `/api/v1/admin/*`, and business resources `/api/v1/<resource>` (exact tables in the FS specs/OpenAPI). The `/v1/...` spelling is non-normative shorthand only; there is no separate `/api` exception namespace. These are configuration/identity surfaces, not tenant business resources. |
| **RBAC administration API (Δ-RBAC)** | `GET /api/v1/admin/permissions` (catalogue list/filter incl. `version`); `GET|POST /api/v1/admin/roles`; `GET|PATCH|DELETE /api/v1/admin/roles/{id}` (inactivate, not hard delete); `POST /api/v1/admin/roles/{id}/clone`; `PUT /api/v1/admin/roles/{id}/permissions`; `PUT /api/v1/admin/roles/{id}/data-scopes`; `POST /api/v1/admin/users/{id}/roles`; `GET /api/v1/admin/users/{id}/effective-permissions`; `GET /api/v1/me/effective-permissions`; `GET|PUT /api/v1/admin/sod-policy`. All guarded by Δ-RBAC scopes and audited. |
| Bulk | `POST /api/v1/imports` creates an `import_batch`; rows processed server-side; `GET /api/v1/imports/{id}` progress/errors; CSV/NDJSON upload; max rows per batch configurable. |
| Pagination | Cursor-based (`?cursor=…&limit=`), stable ordering by `(created_at, id)` or domain key; `limit` max 500; no offset paging on ledger. |
| Filtering | Explicit allow-list of filter/sort fields per collection in OpenAPI; unknown filters → 400. Ledger queries require at least tenant + one dimension filter and a date bound. |
| Concurrency | `If-Match: <version>` (ETag) on mutable documents; posting commands are naturally serialised by document lock; 412 on stale version. |
| Partial responses | `fields=` allow-list on read endpoints for performance; never on posting. |
| Errors | RFC 9457 problem+json + stable `code` (see §2.9); `trace_id` always returned; errors documented in `contracts/errors.yaml`. |
| Time/dates | ISO-8601 UTC timestamps with `Z`; dates `YYYY-MM-DD`; tenant timezone used for period resolution only. |
| Quantity payloads | `{"quantity": "12.000000000000", "uom": "EA"}` — strings for decimals to avoid float loss; `NUMERIC(38,12)` persistence. |
| Rate limits | Per API key/tenant token bucket; `429` + `Retry-After`; separate limits for read/post/import. |
| Deprecation | `Deprecation`/`Sunset` headers; announced in contract changelog; consumer impact list via `feature_sets.py who-depends-on`. |
| Internal endpoints | `/healthz`, `/readyz`, `/internal/deps` (dependency contract versions + outbox/job health for L2 gates), `/internal/outbox/replay`; bind to internal network only. |

### 5.10 Feature flags and tenant configuration

- `feature_flag` per `(tenant, flag_key)` with default from env/manifest, `enabled`, `rollout` metadata, audit of changes. Flag keys follow framework §6: `inventory.<domain>.enabled` (e.g. `inventory.ledger.enabled`, `manufacturing.wip.enabled`, `wms.execution.enabled`, `consigned.inventory.enabled`).
- Flags gate **routes + jobs + event handlers**; a disabled flag returns `409 DOMAIN_DISABLED` for writes and hides routes in OpenAPI (per-tenant OpenAPI not generated; deployment-level contract stays stable).
- Tenant configuration is data, not code: `tenant_setting` namespaces `inventory.*`, `manufacturing.*`, `wms.*`, `replenishment.*` (documented preference sets: Inventory Management Preferences [iri-274], Manufacturing Preferences [mpw-219], WMS preferences [mpw-823], accounting/costing preferences [iri-057]). Precedence: document override → item/location setting → tenant setting → system default; resolution is tested per setting.
- Flag/tenant-setting guards encode documented incompatibilities: consigned inventory vs assembly items [iri-323]; WIP vs LIFO/FIFO/specific assemblies [mpw-152]; group-average vs inventory worksheet [iri-064]; bins/statuses vs basic mode.

### 5.11 Observability and operations

| Concern | Mechanism |
|---|---|
| Logs | structured JSON to stdout: `ts, level, service, tenant_id, request_id, trace_id, actor_id, event, duration_ms, outcome`; PII redaction; no SQL payload logging in prod. |
| Traces | OpenTelemetry auto-instrumentation for FastAPI/SQLAlchemy/Redis; posting span includes `posting_type`, `movement_count`, `ledger_sequence`, `cost_object_count`; sampling default 10% (100% on error). |
| Metrics (RED + domain) | request rate/errors/duration per route; posting p50/p95/p99; posting types counter; balance read latency; outbox depth/age; job run duration/failures; reconcile drift gauge; negative-inventory count; recalculation backlog; sequence contention; DB pool saturation; lock wait time. |
| Health | `/healthz` liveness; `/readyz` DB+Redis+outbox readiness; `/internal/deps` dependency contract versions, migrations head, outbox depth, scheduler state (used by L2 entry gate). |
| Alerts | posting p95 > 300 ms 15 min; any reconcile drift > 0; outbox age > 5 min; recalculation backlog > threshold; deadlock rate > 0.1%; negative inventory spike; failed exit gate. |
| Runbooks | per-FS `docs/feature-sets/FS-xx.md` includes dashboards/alerts/rollback (framework §7). |

### 5.12 Data retention and immutability

| Data class | Policy |
|---|---|
| Inventory/cost ledger, audit, approvals, outbox | append-only; retain for contract horizon (default 7 years, tenant-configurable ≥ 1 year); never updated/deleted; reversals only. Partitioned by month, old partitions compressed/archived read-only. |
| Documents | retained; drafts may be purged after N days if never posted (config); posted documents immutable, retained with ledger. |
| Job runs / idempotency / outbox published | operational retention (90 days / 7 days / 30 days after publish) with archive for audit-relevant runs. |
| Attachments | metadata retained; blob lifecycle per tenant policy; quality images/COA documents maybe regulated. |
| PII | only in `user_identity` (global identity), `app_user` (membership), `tenant_sso_connection` (company IdP metadata/email domains), `user_identity_link` (IdP email/subject), `vendor`, `attachment_metadata`, `device_session`; erasure/pseudonymisation targets identity/membership/link tables and never touches ledger amounts; identity retained while any membership/audit obligation exists; Dex stores no platform PII beyond issuer config; audit of erasure. |
| Tenant offboarding | flag tenant `deactivated`, stop jobs/API, export package, then retention-based purge after configurable window. |

---

## 6. Replenishment and planning backend

**Positioning:** planning is a **read-optimised, write-restricted** context. It reads balances/open documents/commitments and the demand history projection, computes plans/suggestions, and **never posts inventory**. Turning a suggestion into a document is an explicit API call that creates a *draft* purchase order/work order/transfer order through the owning context (which then goes through that context's approval/posting rules). Planning also **never writes item-master tables**: planning values (reorder point, preferred stock level, safety stock, lead time, seasonal demand) reach `item_location`/`item_inventory` only through the FS-01 contract `item_master.contract.apply_planning_values` (§1.4 layering, DATA-MODEL DM-PLAN-10). This keeps planning recomputable and harmless.

### 6.1 Inputs, outputs, interfaces

| Direction | Data | Source / sink |
|---|---|---|
| Read | on-hand, committed, available, on-order, in-transit per item/location/status | `inventory_balances` contract / read models (§2.5) |
| Read | open PO quantities/dates, open WO component & output demand, open TO supply/demand | procurement/manufacturing/ledger read models + events |
| Read | demand history buckets (sales/issues by day/week) | `demand_history` projection fed by `sales_fulfillment`/`inventory.issued` events; external SO data only via the order boundary |
| Read | item planning attributes: reorder point, preferred stock level, safety stock, lead time, seasonal demand, preferred location, per-location overrides [iri-227, iri-274, iri-276] | item master (`item_location`, `item_inventory`) |
| Read | BOM structures (multi-level planning), system lead times, work-centre calendars | manufacturing |
| Write | `demand_plan(+lines)`, `supply_plan(+lines/suggestions)`, `planning_action_message`, `time_fence`, `replenishment_calculation(+lines)`, `atp_projection`, `allocation_run/result/exception` | planning tables only |
| Write (via explicit API) | draft POs (`POST /v1/purchase-orders`), draft WOs (`POST /v1/work-orders`), draft TOs (`POST /v1/transfer-orders`) from suggestions [iri-285, iri-286] | procurement / manufacturing / ledger |
| Emit | `plan.generated`, `plan.suggestion.created`, `allocation.completed`, `atp.updated`, `replenishment.calculated`, `planning.exception.raised` | outbox |

### 6.2 Demand planning

| Aspect | Design |
|---|---|
| Methods | Sales-history projection methods documented as linear regression, moving average, seasonal average and sales forecast; alternatively plans built from open opportunities/quotes/orders [catalog cross-cutting for iri-277/iri-279]. Phase 1 implements the statistical methods over `demand_history`; opportunity/quote-based demand arrives through the order boundary as synthetic demand records. |
| Plan structure | `demand_plan` header (method, horizon, buckets daily/weekly, status DRAFT/CALCULATED/APPROVED/ARCHIVED) + `demand_plan_line` per item/location/period bucket with qty + method metadata; manual plan entry/overrides supported [iri-280, iri-283]. |
| Time fences | `time_fence` rows (item/category, days, behaviour FROZEN/SLUSHED) influence how plan changes are accepted [iri-293]. |
| Demand change | Tenant/plan setting controlling how much a forecast may change between runs; excess change raises `planning_action_message` [iri-294]. |
| Status monitoring | Plan status lifecycle is exposed; recomputation is idempotent per `(plan, run_key)` [iri-289, iri-290]. |
| Job | `planning.demand.calculate` (scheduled; calculation day preference [iri-274]) → writes lines; never touches ledger. |

### 6.3 Advanced Inventory Management (AIM) — reorder point / preferred stock automation

| Aspect | Design |
|---|---|
| Inputs | Item lead time, safety stock, preferred stock level overrides, seasonal demand, demand history, analysis intervals, demand-change threshold, time fences, calculation day [iri-274, iri-276]. |
| Calculation | `replenishment.calculate_aim` computes per item/location: average demand over the analysis interval, projected reorder point and preferred stock level; writes `replenishment_calculation(+line)` with inputs/derived values, and applies auto-calculated planning values **through the FS-01 contract `item_master.contract.apply_planning_values`** only when `auto_calculate=true` (never a direct item-master write; DATA-MODEL DM-PLAN-10). Manual values are never overwritten; disabling auto-calculation reverts to manual values (documented behaviour [catalog cross-cutting for iri-269..276]). |
| Formula policy | The exact NetSuite arithmetic is not fully specified in the crawled help. Phase 1 therefore stores the inputs/outputs contract and puts the formula behind `aim_formula_policy` (tenant setting) with one default implementation; **marked ASSUMPTION**, open question Q-03. All calculations are deterministic, versioned (`formula_version` on result) and tested against fixtures. |
| Jobs | nightly/weekly `replenishment.aim` and `replenishment.ordering` (Order Items workflow data); emits `replenishment.calculated`, `replenishment.below_reorder_point`. |
| Downstream | Order Items/quantity-to-order data (preferred stock − available [iri-216]) feeds PO creation suggestions; reorder reminders are derived views (no UI in Phase 1). |

### 6.4 Supply planning / MRP

| Aspect | Design |
|---|---|
| Scope | Planning horizon, planning scope (locations, item categories, planning rules), and planning-rule definitions (which supply types are allowed, transfer behaviour across the network) [iri-302, iri-304] |
| Algorithm | Net requirements per item/location/bucket from demand plan + actual demand, subtract available/on-order/scheduled receipts, explode multi-level BOMs (including phantom assemblies [mpw-014]) to component requirements, offset by lead times/calendars, produce suggestions (purchase, work order, transfer) + exception messages. Work-order demand interacts with reorder-point/preferred-stock calculations as documented [mpw-090]. |
| Outputs | `supply_plan` + `supply_plan_line` (requirement, covered qty, projected balance) + `supply_plan_suggestion` (action, item, location, qty, due date, source rule) + `planning_action_message` (exceptions, e.g. expedite, late, shortage) [iri-294]. |
| Order creation | `POST /v1/supply-plans/{id}/create-orders` turns suggestions into **draft** POs/WOs/TOs (bulk; idempotent per suggestion id); mass work-order creation supported [iri-285, iri-286]. |
| Jobs | `planning.supply.run` (chunked by item scope; single-run lock); status reported; re-run from same inputs is deterministic (`planning_run_key`). |
| Phase boundary | Supply Planning Workbench is a UI over these data (deferred); SuiteAnalytics workbooks deferred [iri-306]. |

### 6.5 DRP (distribution resource planning)

Documented model: distribution categories, distribution network, bill of distribution, `Transfer` supply type to recommend inter-location transfers [iri-295, iri-297, iri-298, iri-299, iri-300, iri-301]. It is documented as superseded by supply-planning rules but still in scope. Design: `drp_network` (members = locations with level/parent), `bill_of_distribution` levels, `distribution_category` + members; DRP run emits **transfer suggestions** into `supply_plan_suggestion` with `supply_type=TRANSFER`, then reuses the same order-creation path. `EXT`: since supply planning has rules that subsume DRP, both are implemented on the same engine with `planning_rule.engine ∈ {MRP, DRP_BOD}` — no duplicate algorithms.

### 6.6 Supply allocation

| Aspect | Design |
|---|---|
| Inputs | supply (on-hand, expected receipts) + demand (orders with **Supply Required By Date** [iri-312]) + `allocation_strategy` rules (priority, fairness, location scope, FEFO lot assignment [iri-311]) |
| Outputs | `allocation_run`, `allocation_result` (demand line ← supply ref, qty, lot/serial for FEFO), `allocation_exception` when demand cannot be met [iri-315] |
| Supported item types | inventory, assembly and kit items [iri-307] — kit assembly allocation expands to components; assembly allocation considers build feasibility |
| Relationship to commitments | Supply allocation **plans**; commitments **reserve** ([C-01]). A committed allocation may trigger C-01 when the strategy says "reserve now"; otherwise it remains a plan and never changes availability. |
| Jobs | `planning.allocation.run` (manual or scheduled), idempotent per run key; emits `allocation.completed`. |

### 6.7 Available-to-promise (ATP)

Documented: projects the earliest ship date at which the full ordered quantity is available by netting expected receipts (purchase, work, transfer orders) against expected shipments (sales and transfer orders) per day [iri-316]; supports multiple methods [iri-317], earliest-availability check [iri-320], and demand-planning integration [iri-321].

| Aspect | Design |
|---|---|
| Projection | **Durable** `atp_projection` per (item, location, date bucket, method): `begin_qty`, `supply_in` (PO/WO/TO expected), `demand_out` (SO/TO expected via order boundary), `projected_available`, `earliest_full_date`, plus persisted **`policy_version`, `shortage_qty`, `shortage_date`** (DATA-MODEL §13); rebuilt by job on any supply/demand event. |
| Method switch | Canonical enum is defined in **DATA-MODEL §13**: `method (DISCRETE_ATP/CUMULATIVE_ATP_WITH_LOOK_AHEAD)` — the two documented ATP methods [iri-317]. An item/location/tenant selects one; the earliest-availability check [iri-320] is a **query output**, not a method. Both methods share the durable projection rows above (with `policy_version`); ASSUMPTION on exact arithmetic → Q-04. |
| Query API | `GET /v1/atp?item=&location=&qty=&required_by=` returns earliest date and per-day projection; read-only, p95 ≤ 150 ms (framework §9 balance-read target). |
| Consistency | Projection lag tolerated (documented in OpenAPI); posting decisions never depend on ATP (ATP is an advisory read model). |
| Events | `atp.updated` on rebuild. |

### 6.8 WMS replenishment (warehouse-internal)

Bin/primary-bin replenishment plans, scheduling, and replenishment tasks are WMS execution concerns [mpw-889, mpw-890, mpw-891, mpw-892]: WMS consumes item stocking rules and planning outputs and creates `replenishment_plan`/`replenishment_task` rows, then posts T-29 bin transfers on completion. Warehouse replenishment is **not** the same as purchasing replenishment; the data model keeps them disjoint (§ DATA-MODEL).

### 6.9 What is Phase-1 backend vs later analytics/UI

| Capability | Phase 1 | Later |
|---|---|---|
| Reorder points, preferred stock, safety stock, lead time, seasonal demand fields | data + validation + AIM calculation + Order Items data | Order Items UI, reminders UI |
| Demand plans (methods, time fences, action messages) | calculation engine + tables + APIs | workbench UI |
| Supply plans / MRP suggestions / order creation | engine + APIs creating drafts | Supply Planning Workbench [iri-305] |
| DRP | network/BOD data + transfer suggestions | workbench |
| Supply allocation | strategies, runs, results, exceptions, FEFO | allocation UI |
| ATP | projection + query API | availability UI [iri-319] |
| Supply Chain Control Tower | **deferred** (UI-only, catalog `iri-322` classified ui-only) | Phase 2+ |
| Analytics workbooks (supply chain/planning) | **deferred** (analytics class) | Phase 2+ |

### 6.10 Planning invariants

`INV-20` planning reads never modify ledger/balances; `INV-21` a suggestion can be converted to at most one document (unique suggestion→document link); `INV-22` ATP projected available is never negative (clamped to 0 with shortage date recorded); `INV-23` allocation does not exceed supply by (item, location) or FEFO lot quantity; `INV-24` time-fenced demand changes are flagged and not silently applied.

---

## 7. Non-functional design, testing, security and deployment

### 7.1 NFR mapping (framework §9)

| NFR target (framework §9) | Design mechanism | Proof / test | Owner |
|---|---|---|---|
| Ledger posting p95 ≤ 300 ms @ 20 tx/s | single-DB tx, row-locked balances, cost-object advisory locks, no remote calls in path, prepared statements, minimal indexes on hot tables, outbox insert only | load test `perf/posting.py` (locust/k6) on compose stack; CI perf smoke (lower rate); release gate on nightly | FS-04 |
| Balance reads p95 ≤ 150 ms | materialised `inventory_balance(_bin/_lot)` + covering indexes; read models never aggregate ledger in request | `perf/balance_read.py`; query plans checked in CI (`EXPLAIN` assertions for top 10 queries) | FS-04 |
| ≥ 200k ledger lines/hour batch import | chunked import batches, `COPY`-free but multi-row inserts, per-item-range chunking, async cost recalc after import, worker parallelism | `perf/import_200k.py`; measured on staging | FS-04 |
| Zero qty/valuation drift; nightly reconcile exact | append-only ledger + derived balances; reconcile jobs (§2.4.3); DB constraints | `reconcile` job exact match on fixtures; invariant suite | FS-04/FS-05 |
| Durability: outbox at-least-once + idempotency → effectively once | transactional outbox, relay retries, idempotency keys, consumer checkpoints | kill-relay test: events redelivered, no duplicate side effects | FS-00 |
| Tenant isolation on every endpoint | RLS + app filter + data scopes | parametrised isolation test over all routes (schemathesis hook) | FS-00 |
| Audit trail immutable | append-only `audit_event`, DB trigger, restricted grants | negative tests: UPDATE/DELETE denied | FS-00 |
| Availability 99.9% prod | managed Postgres, 2 api replicas behind proxy, worker replicas, outbox relay; health checks + restart policies | deployment drill; alerting | FS-00 |
| Retention: ledger append-only, reversals only | §2.4.2, §5.12 | schema tests + DB privilege tests | all |
| OpenAPI/event contracts | schemathesis + jsonschema + provider/consumer contract tests | CI L1 | each FS |
| Concurrency correctness | locking order + guarded updates + retries | concurrency suite C-01..C-08 | FS-04/FS-05 |

### 7.2 Concurrency and locking test suite

| ID | Scenario | Assertion |
|---|---|---|
| C-01 | 50 parallel fulfilments of the last unit of one item/location | exactly one succeeds when policy BLOCK; on-hand never negative; no deadlock escapes retry budget |
| C-02 | 20 parallel receipts for the same item at different costs, same timestamp | FIFO/LIFO layer order deterministic; average correct; `INV-10` holds |
| C-03 | Parallel commitment + availability reads | no commitment exceeds available; readers never see committed > on-hand |
| C-04 | Transfers A→B and B→A in parallel | no deadlock (rank ordering); net zero; both post or one retries cleanly |
| C-05 | Two postings of the same document concurrently (double click) | one posting; second gets replay or `409 INVALID_STATE`; one ledger txn |
| C-06 | Retry with same idempotency key after timeout | same response/resource; no duplicate ledger rows |
| C-07 | Cost recalculation running while new postings arrive | postings serialise on cost lock; no mixed valuation; recalculation result stable |
| C-08 | Import batch chunk failure mid-way | committed chunks remain; retry resumes at cursor; totals reconcile; no duplicate rows |

### 7.3 Invariant catalogue (testable statements)

| ID | Statement | Test (path) |
|---|---|---|
| INV-01 | For each posting, signed movements balance as defined in §2.2 (moves sum to zero; receipts/issues match external delta) | `tests/invariants/test_posting_balance.py` |
| INV-02 | Value movements equal external value change; pure moves are value-neutral | `test_posting_value.py` |
| INV-03 | For every position, `inventory_balance.qty_on_hand == Σ signed ledger qty` | `test_qty_conservation.py` |
| INV-04 | `qty_available >= 0` for every position; committed never exceeds on-hand unless a configured backorder exists | `test_availability.py` |
| INV-05 | Each in-transit balance equals Σ open TO line in-transit quantities; never negative | `test_in_transit.py` |
| INV-06 | Σ commitment allocations ≤ commitment quantity; allocations reference live positions | `test_commitments.py` |
| INV-07 | `qty_committed == Σ active commitment qty`; `qty_on_order == Σ open supply docs` | `test_quantity_rollups.py` |
| INV-08 | No posting/reference to `UNALLOCATED` after bootstrap; no residual unallocated balance after distribution | `test_distribution.py` |
| INV-09 | No hard delete/update of ledger, cost ledger, audit, outbox rows (DB privileges/triggers) | `test_append_only.py` |
| INV-10 | Inventory value = Σ ledger value = Σ cost ledger = item_cost aggregates, per cost object | `test_valuation_equation.py` |
| INV-11 | Layer conservation: Σ consumption + remaining = original qty/value | `test_cost_layers.py` |
| INV-12 | Landed-cost allocation sums to document total per category/currency | `test_landed_cost.py` |
| INV-13 | Σ rounded movement values = exact posting value (largest-remainder) | `test_rounding.py` |
| INV-14 | No residual drift > 1e-9 after any posting/recalc | `test_rounding.py::test_residual` |
| INV-15 | No negative layer qty/value; exhausted layers closed exactly | `test_cost_layers.py::test_no_negative_layer` |
| INV-16 | Standard-cost on-hand value = qty × active standard (± open revaluation) | `test_standard_cost.py` |
| INV-17 | Every inventory/cost movement has exactly one cost entry (no uncosted line) | `test_costing_completeness.py` |
| INV-18 | Each serial has exactly one current position and quantity 1 per position | `test_numbered_inventory.py` |
| INV-19 | Line quantity equals Σ detail (bin/lot/serial) quantity | `test_inventory_detail.py` |
| INV-20 | Planning contexts write no ledger/balance/cost rows | `test_planning_readonly.py` |
| INV-21 | One suggestion → at most one created document | `test_planning_suggestions.py` |
| INV-22 | ATP projection available never negative; shortage recorded | `test_atp.py` |
| INV-23 | Allocation ≤ supply per item/location and per FEFO lot | `test_supply_allocation.py` |
| INV-24 | Time-fenced demand change flagged, not silently applied | `test_time_fences.py` |
| INV-25 | Tenant isolation: no cross-tenant row visible/writable on any table/route | `tests/security/test_tenant_isolation.py` |
| INV-26 | Document state machines: only declared transitions succeed; posted docs immutable | `tests/invariants/test_state_machines.py` |
| INV-27 | Consigned/owned quantities never mix in one position; ownership change only via T-22 | `test_consigned.py` |
| INV-28 | WIP only for standard/average assemblies; group-average never uses worksheet | `test_documented_incompatibilities.py` |
| INV-29 | Every posting GL instruction persists exactly one `finance_journal_intent` per rule line; instruction count = intent count; one set per posting revision; append-only and idempotent on the posting key | `test_finance_intents.py` |
| INV-30 | **Tenant/user isolation (Δ-MT):** for every API request and job the active tenant equals the token/context tenant and every row read/written belongs to it; cross-tenant probes return 404; identity/session tables expose no tenant business data; platform-admin operations are audited | `tests/security/test_tenant_user_isolation_matrix.py` (every endpoint × list/get/mutate/job × cross-tenant probe) |
| INV-31 | **Authorization completeness (Δ-RBAC):** every tenant route is guarded by exactly one catalogued capability scope; scope and data-scope denials are correct; role template catalogue is coherent; SoD is enforced at assignment and approval time; platform-only scopes are unreachable from tenant roles | `test_route_scope_coverage.py`, `test_rbac_roles.py`, `test_sod_policy.py`, `python tools/feature_sets.py permissions` |
| INV-32 | **SSO/authn integrity (Δ-SSO):** every SSO login resolves to exactly one verified identity link and one active membership, with no cross-tenant leakage; state/nonce/PKCE validated and single-use; group/role mapping bounded by RBAC; connection/link lifecycle audited; Dex unavailability never silently downgrades to local auth | SSO flow/isolation tests, `test_route_scope_coverage.py` (public allowlist 9), `test_rbac_roles.py` |
| INV-33 | **Compatibility/reuse integrity (Δ-CFG/Δ-REUSE):** no HARD-violating configuration reaches a build/posted quantity; rule-set/scope evaluation is deterministic and evidence-recorded; every unbuild line conserves quantity and value, preserves serial/lot identity, and used/scrap/hold dispositions match the posted stock status | `test_compat_rules.py`, `test_compat_paths.py`, `test_unbuild_dispositions.py` |

### 7.4 Test strategy

| Layer | Scope | Tooling | Gate |
|---|---|---|---|
| Unit | costing math, UoM conversion, state machines, validators, rule guards | pytest | every commit (CI) |
| Domain invariants | INV-01..INV-33 over seeded scenarios | pytest + testcontainers-postgres | every commit + exit gates |
| Golden costing corpus | G-01..G-12, normal + recalculation path equality | pytest fixtures | every commit (costing) + exit gate |
| Contract (provider) | OpenAPI schemas + event JSON Schemas | schemathesis, jsonschema | CI L1 |
| Contract (consumer) | consumer-side tests against dependency mocks/real stack | pytest + recorded fixtures | CI L1 + L2 |
| Migration | up/down on empty + seeded DB; N-1 compatibility | alembic + pytest | CI |
| Concurrency | C-01..C-08 | pytest + threads/asyncio + separate connections | CI (nightly heavy) + staging |
| Load/perf | posting, balance read, import, planning run | k6/locust script in `perf/` | nightly staging + release gate |
| Security | tenant/user isolation matrix (INV-25/INV-30), RBAC route-scope coverage/role-SoD tests (INV-31), **SSO/authn integrity incl. state/nonce/PKCE, tenant binding, enforced-SSO and no silent fallback (INV-32)**, membership/session revocation, authz matrix, injection, secrets, audit immutability | pytest, bandit, dependency scan, `feature_sets.py permissions` | CI |
| E2E smoke | golden paths through public API on compose stack | pytest | L3 exit gate |
| Reconciliation | rebuild vs read models | pytest + job | L3 exit gate + nightly env |

### 7.5 Security design

| Threat | Control |
|---|---|
| Cross-tenant data access | RLS + app filter + tests (INV-25); opaque UUID ids; 404 on cross-tenant probes |
| Privilege escalation | RBAC scopes, SoD in approvals, least-privilege DB roles (app role cannot DDL; migration role separate), no dynamic SQL |
| Ledger tampering | append-only triggers/grants; audit trail; reconcile detect; restrict `platform.admin` |
| Idempotency abuse | key scoped per tenant/endpoint, payload fingerprint, in-flight guard, quota |
| Injection | SQLAlchemy parameterised queries only; no string SQL from user input; input validation via Pydantic |
| Deserialisation/upload | size/type limits; virus scan hook (`EXT`); attachments not executed |
| Secret leakage | secrets from env/secret store; tokens short-lived; structured logs redact auth headers |
| Replay of requests | JWT `jti`+exp, API key rotation, idempotency for writes, TLS only |
| Unsafe imports | import dry-run, row-level validation, per-row idempotency, audit |
| Denial of service | rate limits per principal, query allow-lists, cost recalculation bounded, imports chunked/queued |
| Dependency/supply chain | pinned lockfile, vulnerability scan, SBOM, image signing (`EXT` for prod) |

### 7.6 Backup, restore, RPO/RTO

| Aspect | Target/Mechanism |
|---|---|
| Postgres RPO | ≤ 5 min (WAL archiving / managed PITR); daily base backup + continuous WAL. |
| Postgres RTO | ≤ 1 h for prod (managed failover); dev/staging best-effort. |
| Restore verification | monthly restore drill into staging; run `reconcile` after restore and compare pre/post ledger high-watermark + invariants; evidence stored under `verification/evidence/`. |
| Redis | non-durable by design (queues/streams rebuildable); outbox in Postgres is the source of truth; workers recover unacked jobs; stream consumer checkpoints in Postgres. |
| Object storage | versioned + lifecycle; attachment metadata in DB allows re-link. |
| Configuration | everything in code/migrations/seed; env secrets in secret store; deployment state files generated. |
| Disaster scenario | lost Redis only → replay outbox + re-enqueue jobs; lost Postgres → PITR restore + reconcile; region loss → rebuild from latest backup (RPO applies). |

### 7.7 Deployment topology per environment

| Env | Topology | Data | Purpose |
|---|---|---|---|
| **dev** | docker compose (api, worker, postgres, redis, outbox-relay, **shared `dex` for tests** + Dex storage schema) on one host; flags default **off** for new FS | ephemeral/local volume; seed reference data; test Dex connectors | feature development, L2/L3 rehearsal, SSO flow tests |
| **staging** | same compose shape (or small managed DB), 2 api replicas, 2 workers, shared Dex issuer(s) okay; TLS via Caddy/nginx optional | clone/refresh from prod-like anonymised data or seeded | promote FS one at a time, L2/L3, perf smoke, SSO connector fixtures, verifier evidence |
| **prod** | managed Postgres (HA, PITR) + container runtime for api/worker/outbox-relay; ≥2 api, ≥2 workers, 1–2 relays; **one isolated Dex issuer per ACTIVE `tenant_sso_connection`** with its own Dex storage; proxy/TLS; backups/alerts | live; append-only retention; secrets via secret-manager refs | 99.9% target, one FS per deployment, flags per tenant, company-SSO isolation |

Migrations: additive expand/contract, one Alembic chain, no down-migrations in prod (framework §3/§6); shadow-DB run for destructive-looking operations; `deployment-state.<env>.json` generated only by tooling.

### 7.8 CI pipeline stages (mapped to framework gate layers)

| Stage | Commands (illustrative) | Gate |
|---|---|---|
| L0 static plan | `tools/feature_sets.py validate` + `tools/feature_sets.py permissions` (255/7/14, allowlists 9/6, + route-scope coverage); import-linter boundary check; migration chain check; OpenAPI/event schema lint | every commit |
| Build | ruff, mypy (strict for domain), unit tests, coverage threshold, `alembic upgrade head` on empty DB, `downgrade` test | every commit |
| L1 contract | provider contract tests, consumer contract tests, OpenAPI diff vs last deployed, JSON Schema compatibility | PR + merge |
| Integration | compose stack up (testcontainers/compose profile), invariant suite, golden corpus, concurrency suite, security isolation, schemathesis | merge to main + nightly |
| Perf smoke | posting/balance/import scripts at reduced rate; thresholds enforced | nightly |
| Deploy L2 | `feature_sets.py check-env --set FS-xx --env <env>` then migrations + rollout | deploy job |
| Exit L3 | smoke + provided-contract tests + invariant re-run + reconcile + `record --status verified` | deploy job |
| Evidence | artifacts (JUnit, OpenAPI diff, event schema diff, perf JSON, reconcile output) to `verification/evidence/FS-xx/` | verifier review |

---

## 8. Architecture and technology decisions

Framework §3 fixes language/API/DB/jobs/events/auth/tests/deploy; **no blocker was found** in that stack for Phase-1 scope. The decisions below resolve the remaining degrees of freedom (and the few places where the framework leaves a choice).

| ID | Decision | Alternatives considered | Rationale | Consequence / gate |
|---|---|---|---|---|
| D-01 | Modular monolith: one image, three entrypoints (`api`, `worker`, `outbox-relay`) | microservices; serverless; event-store service | cross-context ACID posting is the core problem (§1.3) | import-linter boundary check in CI |
| D-02 | One Postgres DB, **one schema per bounded context**, cross-schema FKs allowed only in dependency direction | DB per module; no cross-schema FKs | single transaction + referential integrity, clear ownership | schema grants + migration ownership review |
| D-03 | Append-only ledger + synchronously maintained materialised balances; balances provably rebuildable | ledger-only queries; balance-only (no ledger) | correctness + read performance + reconcile | `INV-03`, reconcile exactness |
| D-04 | `READ COMMITTED` + explicit ordered row locks + bounded retries; `SERIALIZABLE` available per-endpoint as fallback | global SERIALIZABLE | avoids serialization-failure storms on hot items while keeping correctness through guarded updates | concurrency suite C-01..C-08 |
| D-05 | Cost-object serialisation via `pg_advisory_xact_lock`; balance rows via ordered `FOR UPDATE` | table locks; item-level row locks; optimistic retry only | value computation must not interleave per cost object; ordered ranks prevent deadlocks | §2.4.4 documented ranks; deadlock metric |
| D-06 | Transactional outbox → relay → Redis Streams; per-aggregate ordering | Kafka; DB triggers publish; direct webhooks | framework §3, at-least-once with replay, no 2PC | relay kill test; outbox age alert |
| D-07 | RQ (framework allows Celery) | Celery | simpler config, Redis-native, adequate for Phase 1 | worker scaling story documented |
| D-08 | Multi-tenancy: app filter **and** Postgres RLS | app-only; schema-per-tenant; DB-per-tenant | defence in depth; no cross-tenant leak on missing predicate | isolation tests on every route (INV-25) |
| D-09 | UUIDv7 primary keys | UUIDv4; bigserial | indexed locality + non-guessable ids; framework requires UUID | PK type convention in DATA-MODEL |
| D-10 | `NUMERIC(38,12)` quantities and money; decimal strings on the wire | floats; scaled integers | framework §3; no binary-float domain code (lint) | precision tests, `INV-13/14` |
| D-11 | Finance is a **port**: period status in; journal intents persisted append-only in `platform.finance_journal_intent` and readable via `GET /api/v1/finance/journal-intents`; **no GL tables/journals/period close** (Δ-1 approved) | (a) audit/event-only intents; (b) persisted intents + read API; (c) minimal GL stub | (b) chosen: makes posting GL effects assertable by exit gates, immutable and replay-safe, at low cost, without building a GL; PLAN §12 boundary preserved | `FinancePort` contract + `NullFinanceAdapter` + INV-29 + `test_finance_intents.py` |
| D-12 | Order management/customer master external: opaque `external_ref`s; inventory-side commit/fulfil/return commands | build SO/customer modules | PLAN §12; avoids scope explosion | `OrderPort` contract; consumer contract tests |
| D-13 | Base-currency valuation; no FX revaluation in Phase 1 | full multi-currency valuation | PLAN §12.4 | FX revaluation flagged as deferred |
| D-14 | Module boundaries enforced by import-linter (contract facades, no ORM cross-imports) | code review only | framework §3 boundary rule needs to be executable | CI stage L0 |
| D-15 | One Alembic chain, module-prefixed revisions, additive expand/contract only | per-module migration branches | framework §3/§6; N-1 compatibility | migration up/down + N-1 CI |
| D-16 | OpenAPI 3.1 generated from Pydantic; contract diff gate vs last deployed | hand-written spec | framework §3; schemathesis works on generated spec | L1 diff gate |
| D-17 | Event JSON Schemas versioned per `event_type@major.minor`; additive within major | single global schema version | framework §5 L1 compatibility | jsonschema CI + registry |
| D-18 | Feature flags per tenant in DB with env default, audit on change | env-only; launchdarkly-like service | framework §6 flags per tenant; no external dependency | flag audit + disabled-write test |
| D-19 | DB `number_sequence` allocated in posting tx; non-gapless default | Redis INCR; gapless always | correctness + auditability; Redis loss would break numbering | sequence contention test |
| D-20 | Soft-delete/inactivation only for **master data** (`inactive` flag); transactions are never deleted; drafts purgeable if never posted | soft delete everywhere | legal/audit; framework §9 | state-machine tests; retention job |
| D-21 | Partition high-volume append-only tables by month (`RANGE`), hash-by-tenant subpartition only if a tenant exceeds thresholds | no partitioning; tenant partitioning first | keeps vacuum/index maintenance bounded, tenant isolation via RLS | `EXPLAIN` + maintenance jobs |
| D-22 | Immutable-after-save item fields enforced by trigger: `item_type`, `costing_method`, UoM type/base, initial cost [iri-037, catalog note] | app-only validation | documented NetSuite behaviour must survive raw SQL/import | negative DB tests |
| D-23 | Document state machines in code with explicit transition tables + DB guard triggers; approvals declarative | generic workflow engine (SuiteFlow-like) | Phase 1 needs documented approvals, not customisation | state-machine matrix tests |
| D-24 | WMS/counts/manufacturing record **server-side tasks/queues** only; no scanner/device protocol | implement mobile flows | SCM Mobile excluded; mobile procedures deferred | WMS contract excludes device APIs |
| D-25 | Partition/read-model policy: balances sync; on-order/ATP/planning/reporting async projections | sync everything | posting latency budget vs read freshness trade-off, documented per endpoint | staleness documented in OpenAPI |
| D-26 | `SELECT … FOR UPDATE SKIP LOCKED` for outbox relay and job claims | single-threaded relay; external queue | throughput + safety with multiple relay replicas | relay concurrency test |
| D-27 | Testing against real Postgres via testcontainers for all DB-touching tests | sqlite/h2 mocks | locking/constraints/RLS must be tested on the real engine | CI integration stage |
| D-28 | Money/quantity residual policy: largest-remainder allocation, deterministic replay | per-line rounding with drift; banker's rounding everywhere | `INV-13/14` zero-drift target | rounding tests |
| D-29 | **Multi-tenant users (EXT, Δ-MT):** global auth-only `user_identity` + per-tenant `app_user` membership + `tenant_invitation` + `user_session`; active tenant only from the verified token `tid` claim; platform-admin tenant provisioning; tenant admins manage users/invitations/sessions. (Logged as **D-29**; the addendum's earlier draft label "D-24" collides with the WMS/mobile task-records decision — see addendum §9b.) | per-deployment separate identity graph; duplicate identity row per tenant; external IdP only; build tenant groups/hierarchy | supports real multi-tenant users with strict per-tenant isolation and single-tenant default (one ACTIVE membership); global identity is the minimal auth-only exception; no SCM-doc behaviour changes (EXT) | INV-30 isolation matrix; FS-00 exit-gate additions; no feature-set/DAG/contract changes |
| D-30 | **Capability-scope + role-template RBAC model (EXT/ASSUMPTION, Δ-RBAC):** one catalogue scope per route (`design/permissions.json`, 255 scopes/241 assignable/13 service-only/1 platform-only/1 alias), 7 seeded non-editable role templates, custom roles as assignable-scope subsets, effective-dated `user_role` assignments, effective permissions resolved per request, deny-by-default guards with explicit public/self-service allowlists, privilege-escalation + last-admin guards, 14 hard SoD pairs plus requester≠approver. | NetSuite role levels/UI (not reproduced); coarse fixed role enum only; ABAC/policy engine; per-field ACLs beyond documented data scopes | NetSuite RBAC topics [mpw-797/798/800, iri-198] are already FS-00 coverage and the capability-scope model is a design choice beyond the SCM docs; explicit catalogue makes every route/scope machine-checkable and closes the 88→255 seed gap; no new tables, no feature-set/DAG/contract changes | `feature_sets.py permissions`, route-scope/role/SoD tests, INV-31, FS-00 exit gate |
| D-31 | **Dex OIDC identity broker + per-tenant enterprise SSO (EXT, Δ-SSO):** keep the proposed stack and delegate human authentication to **Dex** (OIDC broker; authorization-code+PKCE; federates to company SAML 2.0/OIDC/LDAP-AD connectors); the platform remains the **authorization/token-issuing authority** (validates the Dex ID token, resolves global identity + tenant membership, issues platform access JWT + rotating refresh session; Δ-MT/Δ-RBAC unchanged). Per-tenant `tenant_sso_connection` with email domains, enforced SSO, JIT provisioning and bounded group→role mapping; local Argon2id fallback/break-glass. Reference deployment: one isolated Dex issuer per ACTIVE connection (shared Dex allowed dev/test), Dex storage in PostgreSQL, secrets as refs only. | shared Dex with static connectors for all tenants; local-auth only (no SSO); build a full IdP/federation service; direct IdP integration without a broker | honest per-tenant connector isolation (Dex connectors are static), clean per-company trust/revocation, small Dex footprint; keeps identity/authorization separation; no catalog changes (EXT) | 3 platform tables (created inside `0002_platform_security`; no new migration), INV-32, SSO flow/isolation tests, FS-00 exit-gate additions; no other table/DAG/contract changes |
| D-32 | **Manufacturing compatibility rule engine + unbuild reuse dispositions (EXT/ASSUMPTION, Δ-CFG/Δ-REUSE):** typed attribute/slot model, dual-scope rule sets (assembly/BOM **and** order/configuration/customer/region/channel, both applied with deterministic specificity→priority→code ordering), generic gates on every manufacturing path, configurable enforcement (HARD default; WARN/override with scope+reason, evidence-recorded), and unbuild per-line dispositions (RETURN_TO_STOCK default / SCRAP / HOLD; optional used marking; ORIGINAL_COST default valuation with NRV/MANUAL). | hard-coded per-product configurator; matrix-option-only constraints; separate external CTO/configurator service; no used-condition tracking | the user needs compatible-parts constraints across **all** manufacturing paths with user-configurable enforcement and both assembly and order/customer/region scopes; a tenant-configured rule engine + evidence records avoids per-product code while keeping HARD safety; unbuild reuse closes the teardown/used-parts gap; EXT beyond SCM docs, FS-09 only | 7 FS-09 tables + disposition columns (in existing `0020_manufacturing_execution`), DM-MFG-12/13/14, INV-33, FS-09 contract 1.1.0; no DAG/wave/coverage change |

**Wave/slot refinement (proposal; needs feature-set architect confirmation — open decision O-01).** Framework §6 wave shape is retained, with two dependency-driven refinements inside it:

| Wave | Feature-set slots (logical) | Why this order |
|---|---|---|
| 0A | **FS-00** platform & tenancy | everything depends on tenancy/RBAC/audit/outbox |
| 0B | **FS-01** item master, **FS-02** pricing | master data before any stock; pricing independent of ledger |
| 0C | **FS-03** storage topology & status registry (location/zone/bin/status), **FS-04** inventory ledger core, **FS-05** costing | the ledger cannot post without the dimension registry; costing is invoked by posting, so it must ship with the ledger |
| 1A | **FS-06** numbered inventory & traceability (lot/serial identities, allocation, FEFO, advanced-bin behaviour), **FS-07** counts & cycle counts | numbered behaviour and counts need the core ledger and costed variances |
| 1B | **FS-08** procurement (vendor, contracts, requisition/RFQ/PO, receipts, bills/variances/credits/returns, landed-cost hook) | first document context; needs ledger+costing |
| 1C | **FS-09** manufacturing (assembly/BOM, WO, WIP, routing, outsourced, ECO, scheduling data) | **moved before replenishment**: supply planning generates work orders and consumes open-WO demand [mpw-090, iri-302], so manufacturing must exist first |
| 1D | **FS-10** replenishment & planning (AIM, demand, supply/MRP, DRP, allocation, ATP) | needs procurement **and** manufacturing |
| 1E | **FS-11** WMS execution | needs ledger, counts, numbered inventory, procurement receipts, manufacturing WO picking |
| 1F | **FS-12** quality, **FS-13** consignment, **FS-14** extensions (warranty, customer part, UPC) | depend on the document contexts they attach to |
| 2 | **FS-15** reporting read models/APIs — **Phase-1 backend only** (Δ-3: analytics workbooks/dashboards deferred) | analytics surface over all read models; the core read models already exist and are exercised by exit gates |

This refinement changes only the *placement of traceability* (dimension registry moves before ledger; numbering/advanced-bin behaviour stays after) and *moves manufacturing ahead of replenishment*. Both are forced by dependency direction, not preference; they are flagged to the Lead rather than silently applied.

---

## 9. Traceability — source capability → architecture concern

| Source capability (catalog ids) | Architectural concern | Section | Module |
|---|---|---|---|
| Inventory transaction skeleton; workflow covers purchasing → receiving → manufacturing → selling → fulfilling → replenishing [iri-214, iri-196, iri-259] | posting model, append-only ledger | §2 | inventory_ledger |
| Canonical quantities on hand/committed/available/on order/to order/backordered/in transit [iri-216, iri-227] | quantity model, generated availability | §2.5 | ledger + replenishment |
| Costing methods + immutability + receipt-cost basis + negative fallback [iri-037, iri-036] | costing engine, method registry | §3.2 | costing |
| Costing recalculation and COGS adjustments [iri-059, iri-060, iri-056, iri-062] | recalculation engine, async job, statuses | §3.4/§3.8 | costing |
| Standard costing: categories/templates/versions/planned/rollup/revalue/variances [iri-038..iri-052, mpw-046/047/155-160] | standard engine, variance posting | §3.5 | costing |
| Group average costing [iri-064..iri-068] | group cost object, group balance, worksheet prohibition | §3.2/§4 | costing |
| Landed cost and estimated landed cost [mpw-716..mpw-735] | value-only posting hooks, allocation bases | §3.6 | costing + procurement |
| UoM types/units/conversions/assignment [iri-070..iri-074] | conversion engine, base-unit ledger | §4.1 | item_master |
| Lot/serial tracking, numbered inventory, inventory detail [iri-085..iri-107, iri-218, iri-250] | traceability identities, detail subrecord, mismatch guards | §4.2/§4.3 | traceability |
| Bins/basic vs advanced, bin transfers, putaway worksheets [iri-248..iri-258] | bin topology, on-hand-only dimension | §4.3 | traceability (+WMS) |
| Inventory status and status-based allocation/commit [iri-205, iri-211] | status registry + availability flags; status change posting | §4.3/§T-30 | traceability + ledger |
| Inventory adjustments vs worksheet constraints [iri-235, iri-222, mpw-021/024] | separate posting types T-07/T-08 with documented prohibitions | §2.3 | ledger |
| Inventory counts: calculated/manual, snapshot, approve/reject, variance, recount [iri-223..iri-226, iri-209, mpw-873..883] | counts aggregate + generated variance posting | §2.3 T-20, §5.6 | counts |
| Transfer basics + transfer orders + in-transit + close + replenish/withdraw [iri-235..iri-247] | one-step transfer, TO lifecycle, in-transit class | §2.3 T-09/10/11, C-04/05 | ledger |
| Inventory distribution bootstrap + multi-location setup [iri-200..iri-202, iri-228, iri-215, iri-227] | UNALLOCATED sentinel, distribution doc | §2.8 | ledger + traceability |
| Selling/fulfilling/backorders/underwater/commit/reallocate [iri-259..iri-266] | commitment model, fulfilment/return types, underwater policy | §2.3 T-12/13, §2.7, C-01..03 | ledger + costing |
| Pricing: price levels, quantity pricing, pricing groups, coupons, price rules, effective-date pricing [iri-023..iri-034, iri-141..iri-184, mpw-455..] | pricing context data + resolution rules | DATA-MODEL §5 | pricing |
| Matrix items [iri-120..iri-125, mpw-007..010/132] | matrix parent/child expansion | §4.4 | item_master + manufacturing |
| Kits/groups/assemblies/phantoms [iri-116..iri-119, mpw-003..017] | component explosion, build/unbuild postings | §4.5/§2.3 T-14/15/16 | ledger + manufacturing |
| AIM/reorder/preferred stock/safety stock/lead time [iri-269..iri-276, iri-227] | planning settings, AIM job, formulas behind policy | §6.3 | replenishment |
| Demand planning/time fences/action messages [iri-277..iri-294] | demand plan engine + fences | §6.2 | replenishment |
| DRP [iri-295..iri-301] | distribution network/BOD → transfer suggestions | §6.5 | replenishment |
| Supply planning/MRP + work-order demand [iri-302..iri-306, mpw-090] | supply plan engine, order creation | §6.4 | replenishment |
| Supply allocation + FEFO + required-by dates [iri-307..iri-315] | allocation strategies/runs/exceptions | §6.6 | replenishment |
| ATP [iri-316..iri-321] | ATP projection + query API | §6.7 | replenishment |
| Consigned inventory: receipt/adjust/count/transfer/sell/ownership/journal accounts [iri-323..iri-331] | ownership dimension + T-21/22/23 + incompatibility guards | §2.3/§4.5 | consigned |
| Vendor records/pricing/multiple vendors [mpw-419..mpw-452] | vendor master + item-vendor pricing | DATA-MODEL §9 | procurement |
| Vendor bills, variances, 3-way match, approvals [mpw-453..mpw-503, mpw-478..mpw-529] | bill posting, variance posting, approval hooks | §2.3 T-04/06, §5.6 | procurement |
| Vendor returns/credits [mpw-570..mpw-592] | return shipment + credit posting | §2.3 T-03/T-05 | procurement |
| Requisitions/purchase requests/RFQ/contracts/blanket/PO [mpw-601..mpw-698] | document aggregates + state machines + approvals | DATA-MODEL §9, §18 | procurement |
| Receiving orders, receipts before billing, advanced receiving, PO accounting [mpw-699..mpw-710, mpw-683] | receipt posting + accrued-purchases intent + deferred posting state | §2.3 T-01/T-04 | procurement |
| Manufacturing: assembly records/BOM/revisions/controls/phantom/matrix [mpw-003..mpw-138] | manufacturing master data | DATA-MODEL §11 | manufacturing |
| Work orders, WIP issue/completion/close, backflush, yield [mpw-061..mpw-090, mpw-139..mpw-152] | WO state machine, WIP postings, method constraints | §2.3 T-17/18/19, §3.5 | manufacturing |
| Routing/work centres/calendars/scheduling/capacity [mpw-153..mpw-184, mpw-199..mpw-209] | routing + scheduling data + scheduler runs | DATA-MODEL §11 | manufacturing |
| Outsourced manufacturing [mpw-210..mpw-217] | outsourced operations + PO linkage | DATA-MODEL §11 | manufacturing |
| ECO [mpw-403..mpw-415] | ECO aggregate + approval + BOM revision application | DATA-MODEL §11 | manufacturing |
| WMS setup/strategies/rules/records [mpw-792..mpw-828] | warehouse config, strategies, system rules | DATA-MODEL §10 | wms |
| WMS inbound/receiving/putaway/deferred posting/reversal [mpw-850..mpw-864, mpw-855..mpw-859] | receiving orders, putaway tasks, deferred receipt posting state | §2.3 T-01/T-02, DATA-MODEL §10 | wms + procurement |
| WMS inventory processing/transfers/counts/replenishment/WO picking/status [mpw-865..mpw-894] | WMS tasks posting via ledger | §4.3/§6.8 | wms |
| WMS outbound: release/waves/pick/pack/ship/manifest [mpw-895..mpw-934] | wave/release, pick/pack/ship tasks, manifest records | DATA-MODEL §10 | wms |
| Quality specs/inspections/triggers/queues/COA [iri-360..iri-386] | quality aggregate data (SuiteApp semantics, backend) | DATA-MODEL §13 | quality |
| Warranty registration/claims/repairs [iri-352..iri-359] | warranty data (SuiteApp semantics, backend only) | DATA-MODEL §15 | extensions |
| Customer part numbers / auto UPC [iri-134..iri-140, iri-387..iri-390] | external part mapping + UPC registry | DATA-MODEL §15 | extensions |
| Reporting views: stock ledger/valuation/snapshot/backorder/turnover [iri-334..iri-351] | read models/views only | DATA-MODEL §16 | reporting |
| Mobile/UI/analytics/integration classes (e.g. Smart Count [iri-391..iri-406], Item 360 [iri-407..412], Control Tower [iri-322], WMS mobile [mpw-845..mpw-849, catalog mobile-classified roots]) | excluded/deferred; no architectural surface except task records marked EXT | §1.6/§10 | — |

**Feature-set mapping for this table:** the *module* column maps deterministically to the feature-set slots of §8 and DATA-MODEL §21: `core`→FS-00 (0A); `item_master`→FS-01 (0B); `pricing`→FS-02 (0B); `traceability`→FS-03 (0C, storage registry) and FS-06 (1A, numbered inventory); `inventory_ledger`→FS-04 (0C); `costing`→FS-05 (0C); `counts`→FS-07 (1A); `procurement`→FS-08 (1B); `manufacturing`→FS-09 (1C); `replenishment`→FS-10 (1D); `wms`→FS-11 (1E); `quality`→FS-12 (1F); `consigned`→FS-13 (1F); `extensions`→FS-14 (1F); `reporting`→FS-15 (2). Where a row lists two modules, the capability's data is owned by the first and consumed by the second.

---

## 10. Source-coverage gaps and risks

| ID | Gap / risk | Impact on this design | Handling |
|---|---|---|---|
| G-01 | **SCM Mobile excluded permanently**; mobile-classified topics = 126 (mpw 124 topics, iri 2 topics, plus Smart Count mobile roots). | Device workflows, scanner protocols, offline sync not designed. | Backend task/queue/session records exist (`wms_*`, `device_session` EXT) so a future client can use them; no mobile contract in Phase 1. |
| G-02 | Sales Order Management / Returns / Fulfilment books are **outside** the crawled source set, yet commitments/fulfilment/returns are documented as inventory effects. | Command shapes for commit/fulfil/return are not sourced from docs. | Effects sourced from [iri-260/261/262/263, iri-063, iri-229]; document shapes marked `EXT`/`BOUNDARY` (O-05). |
| G-03 | GL/accounting books not in source set: account mapping, accruals, variances, revaluation accounts, period close. | Cannot validate account semantics or period-close behaviour. | Finance `BOUNDARY` (§1.6, D-11); posting rules carry configurable account refs; accrual/variances grounded in [iri-037, mpw-683, mpw-478] and flagged for finance review. |
| G-04 | Inventory Status help page returned HTTP 404; behaviour evidenced only through the feature-enablement page [iri-198]. | Status flags/behaviour are partly inferred. | Modelled as explicit flags; marked A-02; verifier should sample status behaviour. |
| G-05 | SuiteApp-classified capabilities that are behaviourally backend: Quality Management, Warranty & Repairs, Effective Date Pricing, Auto UPC, Customer Part Numbers, Available To Build, Supply 360, Manufacturing Scheduler, Work Instructions/Traveler, Advanced Manufacturing, Smart Count, SuiteProcurement, WMS shipping integration/EDI. | Scope boundary between "backend semantics" (include) and "UI/workbook/mobile/integration" (defer) is a Lead decision. | Included as backend data/logic with `EXT` where modellable: quality, warranty data, customer part, UPC registry, effective-date price updates, available-to-build calculation, scheduling inputs/outputs. Deferred: workbooks/dashboards, Work Instructions/Traveler printing, Advanced Manufacturing SuiteApp scope, Smart Count mobile, shipping integration/EDI, SuiteProcurement. See O-11. |
| G-06 | Advanced Manufacturing (72 topics) and Manufacturing Mobile (69 topics) not expanded beyond titles/summaries. | Advanced production-planning/shop-floor features may be under-specified. | Deferred as SuiteApp/mobile; core WO/WIP/routing/scheduling modelled from the Manufacturing book core chapters. Backend-processing-logic page [mpw-397] is mobile-adjacent and not expanded — noted. |
| G-07 | Payments, prepayments, payment runs, installments, vendor payment approvals (finance/AP domain). | Procure-to-pay ends at bill/credit/return. | Finance boundary; deferred — intents now persisted per Δ-1, GL/payments remain out of scope. |
| G-08 | OneWorld/subsidiary/consolidation, intercompany & non-arm's-length transfers [iri-242], multi-book accounting. | Only a `subsidiary_ref` segment is carried. | Deferred; reuses posting mechanics if later enabled. |
| G-09 | Customer/vendor hierarchies, CRM, tax, revenue recognition/amortization, discounts/promotions engine beyond documented item pricing. | Not modelled. | Out of scope; item pricing covers documented item-level features. |
| G-10 | Exact AIM/ATP formula arithmetic and some status semantics are not fully stated in fetched summaries. | Formula-level behaviour ambiguous. | Versioned `aim_formula_policy`/`atp_method` policies + fixtures (O-03/O-04). |
| G-11 | Item Creation Assistant / CSV import tooling classified integration; import UX not specified. | Bulk load story is our own. | Import API + `import_batch` designed as `EXT` (§5.9) — deliberately simple and idempotent. |
| G-12 | Documentation drift risk: NetSuite help is versioned; crawl date 2026-10-21. | Behaviour may change upstream. | Catalog ids/URLs pinned in traceability; source index at end; no need to re-crawl. |

**Overall risk posture:** the architecture is defensible against every catalogued core-backend capability (885 topics) because the posting/costing/everything-else decomposition is exactly how those topics nest. The highest residual risks are (a) costing recalculation correctness, (b) GL-boundary semantics, (c) SuiteApp-classified inclusion decisions — all three are called out as Lead decisions, not hidden assumptions.

---

## 11. Assumptions and open questions (for Lead)

### 11.1 Assumptions

| ID | Assumption | If wrong |
|---|---|---|
| A-01 | Backend-only: no UI/analytics/integration work in Phase 1; APIs are the product surface. | scope/effort change |
| A-02 | Inventory-status behaviour flags (availability/committable/sellable/consigned) are the semantic core of the 404'd status page. | status semantics revisit |
| A-03 | Multi-location is enabled by default; single-location is a degenerate configuration. | bootstrap simpler |
| A-04 | Locations map 1:1 to NetSuite-style location records; WMS "warehouse" is a location extension. | WMS topology revisit |
| A-05 | UoM feature is enabled for all tenants in Phase 1 (conversion engine always active). | simplification |
| A-06 | Bins/statuses/lot/serial can be enabled per tenant; posting engine always carries the dimension columns (nullable). | migration churn |
| A-07 | Valuation is base-currency only; FX revaluation deferred. | costing change |
| A-08 | GL/period close/tax/JEs are external; Phase 1 stores intents and reads period status. | large scope increase |
| A-09 | Sales orders/customers are external; order ids are opaque refs. | SO module needed |
| A-10 | Consigned inventory and assembly/manufacturing are mutually exclusive per tenant (documented NetSuite limitation [iri-323]) and enforced by configuration guard. | feature matrix change |
| A-11 | WIP only for standard/average assemblies; LIFO/FIFO/specific assemblies are rejected (documented [mpw-152]). | costing change |
| A-12 | Group-average items cannot use inventory worksheets (documented [iri-064]). | validation relaxation |
| A-13 | NetSuite "assembly item" and "WIP assembly" are item types/extensions in our taxonomy, not separate modules. | data model churn |
| A-14 | Number sequences are non-gapless by default. | numbering change |
| A-15 | Retention horizon default 7 years for ledger/audit (tenant-configurable). | infra cost |
| A-16 | Multi-tenant users are an **EXT** (Δ-MT addendum): Phase 1 supports local identity with optional password or external-IdP assertion; `sso_subject` reserved. SSO/OIDC/SCIM implementation, tenant self-service signup, billing/quotas/custom domains, tenant-switch UI and support impersonation are Phase-2 (addendum §9). | scope/effort only; no Phase-1 behaviour change |
| A-17 | RBAC capability-scope + role-template model is an **EXT/ASSUMPTION** (Δ-RBAC): the authoritative catalogue is `design/permissions.json` (FINAL v1.5.0: 255 scopes = 241 assignable + 13 service-only + 1 platform-only; 7 role templates; 14 SoD pairs; 6 global rules; public 9/self-service 6 allowlists; Δ-SSO adds `platform.sso.manage`/`platform.sso.read`; Δ-CFG/Δ-REUSE adds `manufacturing.compat.manage|validate|override` + `manufacturing.unbuild.dispose`). Permission changes apply on the next request (scopes resolved from DB); sessions are not force-refreshed on role edits. Role/permission admin UI, ABAC/policy engines and SCIM group→role mapping are Phase-2 (addendum §8). | scope/effort only; no Phase-1 behaviour change |
| A-18 | Dex + enterprise SSO is an **EXT** (Δ-SSO addendum): Phase 1 implements Dex as the OIDC broker with per-tenant `tenant_sso_connection`, identity links, JIT and bounded group→role mapping; local password fallback remains. SCIM provisioning, IdP-initiated SAML, company-wide single logout, multiple IdPs per single login, SSO UI screens and operator SSO for platform admins are Phase-2 (addendum §1/§11). | scope/effort only; no Phase-1 behaviour change |
| A-19 | Manufacturing compatibility constraints and unbuild used/scrap/hold reuse are **EXT/ASSUMPTION** (Δ-CFG/Δ-REUSE addendum): rules are tenant-configured (no compatibility rules seeded by default; enforcement default HARD, override policy tenant-configurable), unbuild valuation defaults to original cost with NRV/manual optional; **REWORK deferred to FS-14**, kits/groups outside scope. No catalog topics added; FS-09 contract 1.1.0 additive. | scope/effort only; no Phase-1 behaviour change outside FS-09 |

### 11.2 Open questions / decisions needed

| ID | Question | Options | Recommendation |
|---|---|---|---|
| **O-01** | **Feature-set numbering & wave refinement.** My proposal (§8) splits traceability into dimension registry (0B) + numbered inventory (1A) and moves manufacturing (1C) before replenishment (1D). | (a) accept; (b) keep framework §6 literal (then ledger depends on an FS in a later wave — invalid); (c) different slot numbering | (a) accept; framework §6 explicitly says the wave shape is "to be finalized by architecture" |
| **O-02** | ~~**GL boundary contract depth.** Journal intents only (persist as audit payload), or a real `finance_journal_intent` table + query API?~~ **RESOLVED — Lead-approved Δ-1:** persisted append-only `platform.finance_journal_intent` + read API `GET /api/v1/finance/journal-intents`; `NullFinanceAdapter` default; no GL/journals/period close. | (a) audit/event only; (b) table + read API; (c) full double-entry stub | (b) **accepted**; see §1.6, §5.7, §5.9, D-11, INV-29, DATA-MODEL §3 |
| **O-03** | **AIM formula policy.** Exact reorder-point/preferred-stock arithmetic is not fully documented in the crawl. | (a) implement a documented default + versioned policy; (b) defer AIM auto-calculation to Phase 2; (c) require product owner formula | (a) with formula fixtures and a `formula_version` field |
| **O-04** | **ATP arithmetic policy.** The method enum is settled (canonical `DISCRETE_ATP`/`CUMULATIVE_ATP_WITH_LOOK_AHEAD`, [iri-317], DATA-MODEL §13); exact netting/rounding arithmetic per method remains policy-versioned. | (a) implement both methods behind `policy_version` with fixtures; (b) single method only in Phase 1 | (a) behind `policy_version` + item/location setting; DATA-MODEL §13 |
| **O-05** | ~~**Order/returns boundary contract ownership.** Who defines the external SO/return API contract?~~ **RESOLVED — Lead-approved Δ-2:** FS-00 provides the versioned boundary stubs `order.demand@0.1.0` and `order.supply@0.1.0`; FS-04+ consume them; feature-set architect refines within the major version. | (a) architect-core publishes stub; (b) feature-set architect owns | (a) **accepted**; see §1.6 |
| **O-06** | **SuiteApp-classified scope** (quality, warranty, customer part numbers, auto UPC, effective-date pricing, available-to-build, scheduler inputs): include backend data/logic in Phase 1 or defer? | (a) include backend-only semantics, defer UI/workbooks/mobile (my recommendation); (b) include only quality; (c) defer all | (a); documented in DATA-MODEL ownership map |
| **O-07** | **Cost recalculation vs posting.** Default when a posting hits an item whose recalculation is pending: block (`409`) or allow with `cost_provisional=true`? | (a) block; (b) provisional; (c) per-tenant flag default block | (c) |
| **O-08** | **Vendor bill before receipt.** Phase-1 default assumes Advanced Receiving semantics (bill does not create inventory). Confirm no "bill-only inventory" mode needed. | (a) Advanced Receiving only; (b) both modes | (a) for Phase 1 |
| **O-09** | **WMS enablement prerequisites.** WMS is documented as requiring AIM; do we enforce AIM flags when enabling WMS? | (a) enforce documented prerequisites in flag guard; (b) always-on core capabilities | (a) |
| **O-10** | **Consigned vs assembly incompatibility** — enforce NetSuite limitation (A-10) or allow both with EXT semantics? | (a) enforce guard; (b) allow with EXT | (a) unless product wants otherwise |
| **O-11** | **Composite/kit build step.** Crawled catalog evidences kit component issue but not a "build kit" posting; confirm kits need no build/stock step. | (a) kits/groups never stocked (my model); (b) add build-kit extension | (a); revisit if docs found |
| **O-12** | **Returns costing policy** (original cost vs current cost vs location-specific) is documented as configurable for multi-location [iri-229]; choose Phase-1 default. | (a) original cost when traceable, current otherwise; (b) always current | (a) |
| **O-13** | **Partitioning & retention** — monthly range partitioning + 7y retention accepted? | (a) yes; (b) adjust | (a) |
| **O-14** | **Approval engine depth** — declarative threshold approvals only in Phase 1? | (a) declarative only; (b) include a workflow engine | (a) |
| **O-15** | **Prod topology** — managed Postgres + containers (PLAN §12.6) confirmed; who runs it is outside this design. | — | note for Lead |
| **O-16** | **Custom fields** — generic `custom_field_def`/`custom_field_value` pattern in Phase 1, or defer customisation? | (a) include minimal (item/serial/lot fields needed by docs); (b) defer | (a), limited to documented customisability [iri-098, iri-069] |
| **O-17** | **Tenant model** — shared DB with RLS (default) vs one tenant per deployment for some customers? | (a) shared RLS; (b) hybrid | (a) for Phase 1 |


---

## Appendix A — Source citation key

Catalog topic ids cited by this document (traceability for every claim grounded in the official NetSuite help). Machine-readable source: `research/*.json`.

### Item Record Management + Inventory Management (`iri-*`) — 152 ids

| Id | Topic | Help page |
|---|---|---|
| `iri-023` | Item Pricing | [chapter_N2180614.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/chapter_N2180614.html) |
| `iri-026` | Using Multiple Pricing | [section_N2181607.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2181607.html) |
| `iri-027` | Using Quantity Pricing | [section_N2183333.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2183333.html) |
| `iri-034` | Generating Price Lists | [section_N2185787.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2185787.html) |
| `iri-036` | Setting a Default Inventory Costing Method | [section_4345703007.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_4345703007.html) |
| `iri-037` | Costing Methods | [section_N2191818.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2191818.html) |
| `iri-038` | Standard Costing | [section_N2199708.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2199708.html) |
| `iri-042` | Creating Cost Categories | [section_N2201059.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2201059.html) |
| `iri-043` | Creating Inventory Cost Templates | [section_159673804330.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_159673804330.html) |
| `iri-045` | Defining Cost Versions | [section_N2202327.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2202327.html) |
| `iri-046` | Entering Planned Standard Cost Records | [section_N2202799.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2202799.html) |
| `iri-047` | Standard Cost Rollup | [section_N2204939.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2204939.html) |
| `iri-048` | Revalue Standard Cost Inventory | [section_N2205401.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2205401.html) |
| `iri-050` | Assembly Build Production Cost Variances | [section_N2209083.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2209083.html) |
| `iri-052` | Standard Costing Reporting | [section_N2211461.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2211461.html) |
| `iri-055` | LIFO/FIFO Inventory Costing and Advanced Receiving | [section_N2194541.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2194541.html) |
| `iri-056` | System Cost of Goods Sold Adjustments | [section_N2195087.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2195087.html) |
| `iri-057` | Setting Inventory Costing Preferences | [section_1497451045.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_1497451045.html) |
| `iri-059` | Inventory Costing Recalculations | [section_N2197365.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2197365.html) |
| `iri-060` | Inventory Costing Recalculations on Inventory Adjustments | [bridgehead_N2198841.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/bridgehead_N2198841.html) |
| `iri-062` | Cost Accounting Status on Item Records | [section_N2199228.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2199228.html) |
| `iri-063` | Item Return Costing | [section_N2199328.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2199328.html) |
| `iri-064` | Group Average Costing | [section_4345703444.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_4345703444.html) |
| `iri-068` | Viewing the Balance Location Costing Group Accounts Status Page | [section_4514906066.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_4514906066.html) |
| `iri-069` | Inventory Cost Accounting Workbook | [article_0420024853.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/article_0420024853.html) |
| `iri-070` | Multiple Units of Measure | [chapter_N2211898.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/chapter_N2211898.html) |
| `iri-072` | Setting Up Units of Measure | [section_N2212143.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2212143.html) |
| `iri-073` | Assigning Units of Measure to Items | [section_N2212390.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2212390.html) |
| `iri-074` | Serial and Lot Inventory with Multiple Units of Measure | [section_N2214525.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2214525.html) |
| `iri-084` | Inventory Items | [section_N2247990.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2247990.html) |
| `iri-085` | Serial Numbered Items | [section_N2230290.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2230290.html) |
| `iri-088` | Track Serialized Inventory on Item Records | [section_N2231339.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2231339.html) |
| `iri-089` | Add New Serial Numbers to Inventory | [section_N2231739.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2231739.html) |
| `iri-095` | Building a Serialized Assembly | [section_N2234538.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2234538.html) |
| `iri-098` | Customizing Lot or Serial Numbered Item Records | [section_N2238963.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2238963.html) |
| `iri-099` | Avoiding Quantity Mismatches when Committing Numbered Inventory | [subsect_96151731732.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/subsect_96151731732.html) |
| `iri-100` | Lot Numbered Items | [section_N2235684.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2235684.html) |
| `iri-105` | Building a Lot Numbered Assembly | [section_N2238544.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2238544.html) |
| `iri-107` | Selecting Lot Numbers on Transactions | [bridgehead_N2236107.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/bridgehead_N2236107.html) |
| `iri-108` | Lot Auto Numbering SuiteApp | [article_162780241331.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/article_162780241331.html) |
| `iri-110` | Drop Ship Items | [section_N2239232.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2239232.html) |
| `iri-111` | Special Order Items | [section_N2242662.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2242662.html) |
| `iri-112` | Non-Inventory Items | [section_N2249092.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2249092.html) |
| `iri-113` | Service Items | [section_N2248153.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2248153.html) |
| `iri-114` | Download Items | [section_N2248348.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2248348.html) |
| `iri-115` | Gift Certificates | [section_N2244991.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2244991.html) |
| `iri-116` | Item Groups | [section_N2224824.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2224824.html) |
| `iri-117` | Kit/Package Items | [section_N2225190.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2225190.html) |
| `iri-118` | Groups, Assemblies, and Kit/Packages | [section_N2318089.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2318089.html) |
| `iri-119` | Assembly Items | [chapter_N2319010.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/chapter_N2319010.html) |
| `iri-120` | Matrix Items | [section_N2227654.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2227654.html) |
| `iri-121` | Using the Matrix Item Assistant | [section_N2227918.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2227918.html) |
| `iri-125` | Editing Matrix Items | [section_N2229497.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2229497.html) |
| `iri-127` | Subtotal Items | [section_N2248793.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2248793.html) |
| `iri-133` | Expense Items | [section_4042372470.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_4042372470.html) |
| `iri-134` | Customer Part Number | [chapter_4575965810.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/chapter_4575965810.html) |
| `iri-140` | Import Transactions With Customer Part Numbers | [section_4576567327.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_4576567327.html) |
| `iri-141` | Effective Date Pricing | [chapter_1536354469.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/chapter_1536354469.html) |
| `iri-184` | Effects of Disabling Advanced Pricing | [article_3104808100.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/article_3104808100.html) |
| `iri-196` | Inventory Management Overview | [article_161970666917.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/article_161970666917.html) |
| `iri-198` | Enabling Features for Inventory Management | [section_161963741628.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_161963741628.html) |
| `iri-200` | Distributing Inventory | [section_N2304534.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2304534.html) |
| `iri-201` | Simple Inventory Distribution | [section_N2304987.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2304987.html) |
| `iri-202` | Manual Inventory Distribution | [section_N2305248.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2305248.html) |
| `iri-205` | Non-Available Inventory Settings for Locations | [section_N2307648.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2307648.html) |
| `iri-206` | Configuring Per-Line Locations for Transactions | [section_162024160153.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_162024160153.html) |
| `iri-209` | Setting Up Inventory Count | [section_N2297156.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2297156.html) |
| `iri-211` | Inventory Allocation and Commitment Based on Status | [section_1517948058.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_1517948058.html) |
| `iri-214` | Inventory Workflow | [section_N2251098.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2251098.html) |
| `iri-215` | Inventory Management with Locations | [section_162100850596.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_162100850596.html) |
| `iri-216` | Assessing Stock Levels | [section_N2262573.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2262573.html) |
| `iri-218` | Inventory Detail Search | [section_1518565583.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_1518565583.html) |
| `iri-221` | Entering an Inventory Adjustment | [section_161981111273.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_161981111273.html) |
| `iri-222` | Creating an Inventory Worksheet | [section_161981128590.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_161981128590.html) |
| `iri-223` | Inventory Count | [section_N2296970.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2296970.html) |
| `iri-226` | Working with an Inventory Count | [section_N2299731.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2299731.html) |
| `iri-227` | Item Settings and Stock Levels for Multi-Locations | [section_N2305626.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2305626.html) |
| `iri-228` | Multi-Location Inventory | [section_N2303574.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2303574.html) |
| `iri-229` | Returned-Item Costing Using Multi-Location Inventory | [section_N2307937.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2307937.html) |
| `iri-233` | Inventory Transfer Orders | [section_N2308766.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2308766.html) |
| `iri-235` | Approving Transfer Orders | [section_N2311649.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2311649.html) |
| `iri-236` | Fulfilling Transfer Orders | [section_N2312176.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2312176.html) |
| `iri-238` | Receiving Fulfilled Transfer Orders | [section_N2312912.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2312912.html) |
| `iri-239` | Closing Transfer Orders | [section_N2316558.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2316558.html) |
| `iri-242` | Intercompany Inventory Transfers - Non-Arm's Length | [section_N2313577.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2313577.html) |
| `iri-247` | Withdrawing Inventory | [section_4667027605.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_4667027605.html) |
| `iri-248` | Bin Management | [section_N2270284.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2270284.html) |
| `iri-250` | Advanced Bin / Numbered Inventory Management | [section_N2271791.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2271791.html) |
| `iri-251` | Enabling Bin Management Features | [section_N2273046.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2273046.html) |
| `iri-252` | Creating Bin Records | [section_N2274082.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2274082.html) |
| `iri-254` | Printing Bin Putaway Worksheets | [section_N2275156.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2275156.html) |
| `iri-255` | Updating Bin Putaway Worksheets | [section_N2277819.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2277819.html) |
| `iri-256` | Bin Transfers | [section_N2278346.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2278346.html) |
| `iri-258` | Disabling Use Bins Settings and Features | [section_0512092419.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_0512092419.html) |
| `iri-259` | Inventory Sales and Fulfillment | [section_N2281204.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2281204.html) |
| `iri-260` | Selling Inventory | [section_N2281482.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2281482.html) |
| `iri-261` | Fulfilling Inventory | [section_N2281951.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2281951.html) |
| `iri-263` | Avoiding Underwater Inventory | [section_N2264208.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2264208.html) |
| `iri-264` | Committing Orders | [section_3765928460.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_3765928460.html) |
| `iri-265` | Creating Commit Orders Schedule | [bridgehead_4713119268.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/bridgehead_4713119268.html) |
| `iri-266` | Reallocating Items | [section_N2263567.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2263567.html) |
| `iri-269` | Advanced Inventory Management | [chapter_N2285050.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/chapter_N2285050.html) |
| `iri-274` | Setting Up Advanced Inventory Management | [section_N2285514.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2285514.html) |
| `iri-276` | Lead Time and Safety Stock Per Location | [section_N2286205.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2286205.html) |
| `iri-277` | Demand Planning | [section_N2286970.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2286970.html) |
| `iri-279` | Calculating Item Demand | [section_N2290234.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2290234.html) |
| `iri-280` | Manually Entering an Item Demand Plan | [section_N2292418.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2292418.html) |
| `iri-283` | Manually Entering an Item Supply Plan | [section_N2294552.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2294552.html) |
| `iri-285` | Creating Orders from Supply Plans | [section_N2294794.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2294794.html) |
| `iri-286` | Mass Creating Work Orders | [section_N2330082.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2330082.html) |
| `iri-289` | Monitoring the Demand Plan Status | [section_N2291615.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2291615.html) |
| `iri-290` | Monitoring the Supply Plan Status | [section_N2293790.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2293790.html) |
| `iri-293` | Time Fences | [section_3848058018.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_3848058018.html) |
| `iri-294` | Planning Action Messages | [section_3865354301.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_3865354301.html) |
| `iri-295` | Distribution Resource Planning | [section_4049498070.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_4049498070.html) |
| `iri-297` | Creating a Distribution Category | [section_4049501123.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_4049501123.html) |
| `iri-298` | Creating a Distribution Network | [section_4049501559.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_4049501559.html) |
| `iri-299` | Creating a Bill of Distribution | [section_4049502244.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_4049502244.html) |
| `iri-300` | Associating Distribution Categories | [section_4049502746.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_4049502746.html) |
| `iri-301` | Generate Supply Plans Using DRP | [section_4060580900.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_4060580900.html) |
| `iri-302` | Supply Planning | [section_159171867422.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_159171867422.html) |
| `iri-304` | Setting Up Supply Planning | [section_159172130393.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_159172130393.html) |
| `iri-305` | Supply Planning Workbench | [section_159187932855.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_159187932855.html) |
| `iri-306` | SuiteAnalytics Supply Planning Workbooks | [article_0730021103.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/article_0730021103.html) |
| `iri-307` | Supply Allocation | [section_156424975823.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_156424975823.html) |
| `iri-311` | FEFO Lot Assignments | [article_161009052865.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/article_161009052865.html) |
| `iri-312` | Setting Supply Required By Dates on Order Lines | [section_156639322112.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_156639322112.html) |
| `iri-315` | Supply Allocation Exceptions | [section_156518739289.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_156518739289.html) |
| `iri-316` | Available to Promise | [section_N2300269.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2300269.html) |
| `iri-317` | Available to Promise Methods | [section_N2301636.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2301636.html) |
| `iri-319` | Checking Item Availability | [section_N2302738.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2302738.html) |
| `iri-320` | Available to Promise Earliest Availability | [section_159250317916.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_159250317916.html) |
| `iri-321` | Available to Promise in Demand Planning | [section_0929025924.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_0929025924.html) |
| `iri-322` | Supply Chain Control Tower | [section_1519947103.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_1519947103.html) |
| `iri-323` | Consigned Inventory Management | [article_94502112.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/article_94502112.html) |
| `iri-325` | Purchasing and Receiving Consigned Inventory | [section_0218113017.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_0218113017.html) |
| `iri-326` | Consigned Inventory Adjustments | [section_0218010438.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_0218010438.html) |
| `iri-327` | Consigned Inventory Counts | [section_0327125707.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_0327125707.html) |
| `iri-328` | Transferring Consigned Inventory | [section_0218010714.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_0218010714.html) |
| `iri-329` | Selling and Fulfilling Consigned Inventory | [section_0218011109.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_0218011109.html) |
| `iri-331` | Journal Accounts for Vendor Consigned Inventory | [article_0317081004.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/article_0317081004.html) |
| `iri-334` | Inventory Reporting | [chapter_N2353200.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/chapter_N2353200.html) |
| `iri-351` | Inventory Workbook Templates | [section_156708364339.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_156708364339.html) |
| `iri-352` | Warranty and Repairs Management SuiteApp | [chapter_3938292477.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/chapter_3938292477.html) |
| `iri-359` | Warranty and Repairs Management Considerations | [bridgehead_4389346541.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/bridgehead_4389346541.html) |
| `iri-360` | Quality Management | [part_1510239273.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/part_1510239273.html) |
| `iri-386` | Quality Management Reports | [chapter_1495134914.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/chapter_1495134914.html) |
| `iri-387` | Auto Assign UPC | [chapter_158373836991.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/chapter_158373836991.html) |
| `iri-390` | Monitoring UPCs and Disabling Automatic Assignment | [section_158737099515.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_158737099515.html) |
| `iri-391` | Smart Count | [article_83123952246.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/article_83123952246.html) |
| `iri-406` | GS1 Barcode Scanning for Inventory Counts | [article_1222055829.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/article_1222055829.html) |
| `iri-407` | Item 360 Dashboard | [article_2145345562.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/article_2145345562.html) |

### Manufacturing + Purchasing/Receiving + WMS (`mpw-*`) — 112 ids

| Id | Topic | Help page |
|---|---|---|
| `mpw-003` | Assembly Items | [chapter_N2319010.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/chapter_N2319010.html) |
| `mpw-005` | Assembly Item Records | [section_N2319594.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2319594.html) |
| `mpw-006` | Assemblies on Purchase Transactions, Web Sites, and Work Orders | [article_161487042976.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/article_161487042976.html) |
| `mpw-007` | Matrix Assemblies | [section_4249789892.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_4249789892.html) |
| `mpw-010` | Matrix Assemblies on Transactions | [bridgehead_4250688519.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/bridgehead_4250688519.html) |
| `mpw-011` | Phantom Assemblies | [section_4714298883.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_4714298883.html) |
| `mpw-014` | Phantoms with Manufacturing Routing and Demand Planning | [bridgehead_4724775678.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/bridgehead_4724775678.html) |
| `mpw-016` | Building Assembly Items | [section_N2321340.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2321340.html) |
| `mpw-017` | Unbuilding Assembly Items | [section_N2321882.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2321882.html) |
| `mpw-019` | Unbuilding as Assembly From a New Unbuild Transaction | [article_161495064459.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/article_161495064459.html) |
| `mpw-020` | Assembly Unbuild Variance Posting | [article_161495038125.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/article_161495038125.html) |
| `mpw-021` | Assembly Unbuilds and the Adjust Inventory Worksheet | [article_161495027719.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/article_161495027719.html) |
| `mpw-031` | Costed Bill of Materials | [article_160730538.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/article_160730538.html) |
| `mpw-046` | Conversion Cost Calculations for Top Level Costs and Fully Exploded Costs Subtabs | [section_0614023841.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_0614023841.html) |
| `mpw-055` | Cost Category Subtab | [section_0510112208.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_0510112208.html) |
| `mpw-061` | Assembly Work Orders | [chapter_N2328390.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/chapter_N2328390.html) |
| `mpw-077` | Marking Work Orders Firmed | [article_161538611138.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/article_161538611138.html) |
| `mpw-078` | Marking Work Orders Released | [article_161538605076.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/article_161538605076.html) |
| `mpw-079` | Component Yield Preferences | [section_3727093231.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_3727093231.html) |
| `mpw-085` | Class and Department Fields on Manufacturing Transaction Lines | [section_0114111428.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_0114111428.html) |
| `mpw-089` | Voiding Work Orders | [article_0312032232.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/article_0312032232.html) |
| `mpw-090` | Work Orders and Demand Planning | [section_N2332170.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2332170.html) |
| `mpw-138` | Creating Revision Records for BOM Control | [section_N2335150.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2335150.html) |
| `mpw-139` | Manufacturing Work In Process (WIP) | [chapter_N2335392.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/chapter_N2335392.html) |
| `mpw-142` | Setting Up Items as WIP Assemblies | [section_N2335987.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2335987.html) |
| `mpw-144` | Entering Work Order Issues | [section_N2338862.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2338862.html) |
| `mpw-146` | Entering Work Order Completions | [section_N2339352.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2339352.html) |
| `mpw-147` | Entering Work Order Closes | [section_N2340152.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2340152.html) |
| `mpw-150` | Entering a Completion for an Individual Work Order | [section_N2339540.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2339540.html) |
| `mpw-151` | Entering a Completion With Backflush | [article_161607563571.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/article_161607563571.html) |
| `mpw-152` | WIP and Inventory Costing | [section_N2340709.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2340709.html) |
| `mpw-153` | Manufacturing Routing | [chapter_N2341076.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/chapter_N2341076.html) |
| `mpw-157` | Defining Cost Categories for Manufacturing Routing and Work Center | [section_N2342063.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2342063.html) |
| `mpw-158` | Defining a Manufacturing Charge Item | [section_N2343650.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2343650.html) |
| `mpw-159` | Creating Manufacturing Cost Templates | [section_N2344013.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2344013.html) |
| `mpw-184` | Manufacturing Routing Costing | [section_N2351557.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2351557.html) |
| `mpw-199` | Manufacturing Scheduler | [article_5212015722.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/article_5212015722.html) |
| `mpw-209` | Understanding Legend Colors | [section_0224113953.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_0224113953.html) |
| `mpw-210` | Outsourced Manufacturing | [chapter_157833700001.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/chapter_157833700001.html) |
| `mpw-217` | Outsourced Manufacturing Consolidator | [bridgehead_161183731844.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/bridgehead_161183731844.html) |
| `mpw-219` | Manufacturing Preferences | [section_159112709448.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_159112709448.html) |
| `mpw-397` | Backend Processing Logic | [article_161643551755.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/article_161643551755.html) |
| `mpw-403` | Engineering Change Order | [chapter_1531288763.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/chapter_1531288763.html) |
| `mpw-415` | Viewing Implementation Status | [bridgehead_1531291564.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/bridgehead_1531291564.html) |
| `mpw-419` | Vendor Records | [chapter_N2360272.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/chapter_N2360272.html) |
| `mpw-452` | Multiple Vendors Saved Search | [section_4115931525.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_4115931525.html) |
| `mpw-453` | Vendor Bills | [chapter_N2370131.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/chapter_N2370131.html) |
| `mpw-455` | Bill Capture | [article_164726334180.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/article_164726334180.html) |
| `mpw-478` | Vendor Bill Variances | [section_N2371184.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2371184.html) |
| `mpw-479` | Posting Vendor Bill Variances | [section_N2372745.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2372745.html) |
| `mpw-480` | Vendor Bill Variance Journals | [section_N2373098.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2373098.html) |
| `mpw-481` | Mass Updates for Variance Accounts | [section_N2373402.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2373402.html) |
| `mpw-483` | Setting the Default Vendor Approval Status | [section_N2374176.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2374176.html) |
| `mpw-503` | Approving Vendor Bills in the Employee Center | [section_157528153371.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_157528153371.html) |
| `mpw-505` | Vendor Payments Overview | [section_N2381751.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2381751.html) |
| `mpw-529` | Setting Accounting Preferences for In-Transit Payments | [article_162315127002.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/article_162315127002.html) |
| `mpw-570` | Vendor Returns | [chapter_N2386193.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/chapter_N2386193.html) |
| `mpw-580` | Closing a Line Item on a Vendor Return Authorization | [section_N2391809.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2391809.html) |
| `mpw-581` | Vendor Credits | [chapter_N2391960.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/chapter_N2391960.html) |
| `mpw-592` | Printing Vendor Credits in Bulk | [article_162004656575.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/article_162004656575.html) |
| `mpw-601` | Purchasing and Receiving | [book_N2393897.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/book_N2393897.html) |
| `mpw-638` | Creating Purchase Contracts | [section_4077190978.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_4077190978.html) |
| `mpw-651` | Setting up the Blanket Purchase Order Approval Workflow | [section_4236992073.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_4236992073.html) |
| `mpw-672` | Purchase Order Approval Workflow SuiteApp | [section_N2398841.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2398841.html) |
| `mpw-683` | Accounting for Received Purchase Orders | [section_N2408991.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2408991.html) |
| `mpw-684` | Drop Shipment and Special Order Purchases | [section_N2409296.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2409296.html) |
| `mpw-698` | Crediting a Centralized Vendor Return | [article_162013917948.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/article_162013917948.html) |
| `mpw-699` | Receiving Orders | [chapter_N2410585.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/chapter_N2410585.html) |
| `mpw-701` | Receiving Purchase Orders | [section_N2411320.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2411320.html) |
| `mpw-703` | Receiving a Purchase Order Before Billing | [section_N2415076.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2415076.html) |
| `mpw-709` | Exchange Rates on Item Receipts | [section_N2415572.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2415572.html) |
| `mpw-710` | Printing Labels From Transactions | [article_162626600304.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/article_162626600304.html) |
| `mpw-716` | Landed Cost | [section_N2416930.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2416930.html) |
| `mpw-721` | Creating a Landed Cost Item | [section_N2418506.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2418506.html) |
| `mpw-722` | Entering Landed Cost on a Transaction | [section_N2418831.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2418831.html) |
| `mpw-723` | Landed Cost and Taxation | [section_1554390337.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_1554390337.html) |
| `mpw-725` | Landed Cost and Items Billed Separately | [article_162021551140.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/article_162021551140.html) |
| `mpw-730` | Landed Cost Billed Together With an Inventory Item on One Bill Without Advanced Receiving | [article_162022026857.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/article_162022026857.html) |
| `mpw-731` | Landed Cost With Standard Cost Items | [article_162022111260.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/article_162022111260.html) |
| `mpw-732` | Landed Cost and Commission | [section_N2420792.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2420792.html) |
| `mpw-733` | Landed Cost Allocation per Line | [section_3728979515.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_3728979515.html) |
| `mpw-734` | Estimated Landed Cost | [section_4576786578.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_4576786578.html) |
| `mpw-735` | Estimated Landed Cost Setup Requirements | [section_4683065507.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_4683065507.html) |
| `mpw-792` | Warehouse Management | [book_156356229851.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/book_156356229851.html) |
| `mpw-797` | Warehouse Management Roles and Permissions | [section_156520467974.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_156520467974.html) |
| `mpw-803` | Creating Warehouse Locations | [section_1541433484.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_1541433484.html) |
| `mpw-804` | Configuring Records for Locations With Bins | [section_161314312081.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_161314312081.html) |
| `mpw-808` | Creating Inventory Statuses | [section_1541433532.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_1541433532.html) |
| `mpw-815` | Creating Adjustment Reasons | [section_0207114228.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_0207114228.html) |
| `mpw-823` | Setting Warehouse Management Preferences | [section_156406848826.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_156406848826.html) |
| `mpw-828` | System Rules for NetSuite WMS | [section_156406918320.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_156406918320.html) |
| `mpw-845` | Mobile Device Basics for NetSuite WMS | [chapter_156382508723.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/chapter_156382508723.html) |
| `mpw-849` | Mobile Device Transactions | [section_156382533850.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_156382533850.html) |
| `mpw-850` | Inbound Processing | [chapter_1541435915.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/chapter_1541435915.html) |
| `mpw-855` | Item Receipts in NetSuite WMS | [section_1541435993.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_1541435993.html) |
| `mpw-856` | Enabling Deferred Item Receipt Posting | [section_155993564988.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_155993564988.html) |
| `mpw-858` | Reversing Completed Item Receipt Transactions | [section_1541436036.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_1541436036.html) |
| `mpw-859` | Reversing Inbound Shipment Receiving | [section_0123011407.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_0123011407.html) |
| `mpw-864` | Putting Away Items Using Carts | [section_161894286213.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_161894286213.html) |
| `mpw-865` | Inventory Processing | [chapter_1541441780.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/chapter_1541441780.html) |
| `mpw-867` | Transferring Items Between Bin Locations | [section_1541441872.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_1541441872.html) |
| `mpw-873` | Inventory Counts | [section_1541441798.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_1541441798.html) |
| `mpw-881` | Completing Inventory Counts | [section_155993309646.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_155993309646.html) |
| `mpw-882` | Approving or Rejecting Inventory Counts | [section_155993310131.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_155993310131.html) |
| `mpw-889` | Replenishment for Warehouse Management | [section_1541441806.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_1541441806.html) |
| `mpw-890` | Generating Replenishment Plans | [section_1541442254.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_1541442254.html) |
| `mpw-891` | Scheduling Bin Replenishments | [section_0807023240.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_0807023240.html) |
| `mpw-892` | Replenishing Primary Bin Locations | [section_1541442264.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_1541442264.html) |
| `mpw-894` | Changing the Inventory Status of Items | [section_161894442589.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_161894442589.html) |
| `mpw-895` | Outbound Processing | [chapter_1541442931.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/chapter_1541442931.html) |
| `mpw-917` | Preventing the Picking of Expired Items | [section_1541443212.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_1541443212.html) |
| `mpw-934` | Confirming Customer Shipments | [section_1541443457.html](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_1541443457.html) |
