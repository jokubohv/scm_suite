# Manufacturing Compatibility + Unbuild Reuse — Binding Design Addendum (Δ-CFG/Δ-REUSE, plan v1.5.0)

**Status:** Lead-approved binding amendment to the verified v1.4.0 package. Where this addendum conflicts
with earlier text, **this addendum wins**; affected documents conform (§10).
**Origin:** user requirements — (1) compatibility constraints for configured/assembled products (e.g. a
data-center server: Intel CPU → socket/board/case/card/PSU constraints), applicable to **every manufacturing
path that needs compatible parts**, with **user-configurable enforcement** and **both** assembly/BOM and
order/customer/region rule scopes; (2) assembly **unbuild/teardown** returning parts to inventory with an
**optional “used” marking**, configurable valuation (original-cost default), including **scrap**.
**Marking:** both are **EXT/ASSUMPTION** beyond the NetSuite SCM help (the docs define BOM/matrix/build/
unbuild/variance but not a compatibility rule engine or used-condition disposition). No new catalog topics;
coverage stays 1,348 (769/579).
**Scope:** extends **FS-09 Manufacturing only**; **no new feature set, no DAG/wave/contract-consumer changes**.
FS-09's provided contract is bumped additively `manufacturing.execution@1.0.0 → 1.1.0` (existing consumer
ranges `>=1.0.0 <2.0.0` remain satisfied). New tables live in the existing FS-09 migration
`0020_manufacturing_execution` **— no new migration, no renumbering**.

---

## 1. Compatibility model (Δ-CFG)

### 1.1 Attributes and capacity
- `manufacturing.compat_attribute_def` — tenant-scoped typed attribute catalogue (TEXT/NUMERIC/BOOL/SELECT,
  unit, multi-valued, applicable item types, active) e.g. `socket`, `chipset`, `form_factor`,
  `pcie_slots`, `max_gpu_length_mm`, `psu_watts`, `tdp_watts`, `dimm_slots`, `rack_u`.
- `manufacturing.compat_attribute_value` — attribute values on any item/assembly (item id from `item.master`):
  `(tenant, item_id, attribute_def_id)` unique, typed value columns, provenance (`MANUAL/IMPORT/EXT`).
- `manufacturing.compat_slot_def` — configurable slot/capacity definitions per assembly item or BOM revision:
  `slot_code`, `slot_type` (`CPU_SOCKET/PCIe/DIMM/DRIVE/PSU/CASE/…`), `capacity` (numeric), `unit`,
  component attribute constraints, `is_required`, `sort_order`. Capacity rules (`MAX_QTY`, consumed units
  per component) are evaluated deterministically.

### 1.2 Rule sets and rules (dual scope, both apply)
- `manufacturing.compat_rule_set` — `scope_type`:
  - **`ASSEMBLY` / `BOM_REVISION`** — default rules for an assembly/BOM revision (all configurations);
  - **`CONFIGURATION` / `ORDER` / `CUSTOMER` / `REGION` / `CHANNEL`** — context rules for order/config/
    customer/region/channel.
  Rule sets have `status (DRAFT/ACTIVE/RETIRED)`, effective dates, priority, and `enforcement_mode
  (INHERIT/HARD/WARN)`. All matching rule sets apply with AND semantics; evaluation order is deterministic
  by **scope specificity** (order/configuration > customer > region/channel > assembly/BOM) then priority,
  then rule code. Assembly/BOM rules and order/configuration rules therefore **both** apply.
- `manufacturing.compat_rule` — within a rule set: `rule_type`
  (`REQUIRES/EXCLUDES/ONE_OF/MIN_QTY/MAX_QTY/CAPACITY/ATTR_EQ/ATTR_RANGE/SUBSET_OF`), subject
  (item/attribute/component group), target/constraint expressions (validated JSON), `severity
  (INHERIT/HARD/WARN)`, `message_code`, effective dates, `is_active`, priority.
- `manufacturing.condition_code` — configurable condition/grade registry used by unbuild dispositions
  (`NEW/USED_A/USED_B/USED_C/REFURB/SCRAP` seeds; `is_used`, `is_sellable_default`,
  `is_committable_default`, active).

### 1.3 Enforcement (user-configurable)
- Tenant setting `manufacturing.compat.enforcement_default = HARD|WARN` (default **HARD**).
  Precedence: per-rule > rule-set `enforcement_mode` > tenant default.
- **HARD** violations block the guarded action (`409 COMPATIBILITY_VIOLATION` with rule ids + offending
  components); **WARN** allows the action but records an immutable violation on the validation record and
  emits `manufacturing.config.violation`.
- Override: scope `manufacturing.compat.override` + mandatory `override_reason`; HARD→allowed override is
  audited and stamped on the validation record; override policy is tenant-configurable
  (`manufacturing.compat.allow_override`).
- Every validation produces an immutable `manufacturing.compat_configuration` record:
  `assembly_item_id`, context (`BOM/WO/BUILD/OUTSOURCED/ORDER/ECO/ATB`), configuration hash, selected
  components/attributes snapshot, ordered matched rule sets/rules, result (`VALID/WARN/HARD_FAIL/
  OVERRIDDEN`), override refs, timestamp. Records are evidence for L3 gates and support audit.

## 2. Generic applicability — every manufacturing path that needs compatible parts

| Manufacturing path | Compatibility gate |
|---|---|
| Assembly item / BOM / BOM-revision create or activate | validate components + attributes + slot capacity for **all** assembly types (`STANDARD/WIP/PHANTOM/MATRIX_PARENT/OUTSOURCED`) |
| Matrix assembly generation | generate/activate only option combinations that pass rules; invalid children rejected/flagged |
| Phantom assembly explosion | parent config validated with phantom-flattened components; phantom members still subject to parent rules |
| Work order create / update / release / firm | planned configuration validated; HARD blocks release/build (configurable) |
| WIP issue / completion / close / backflush | component set revalidated at completion; violations blocked/warned per policy |
| Assembly build (non-WIP) and unbuild **rebuild** path | actual component selections validated before posting; unbuild itself restores the previously valid build snapshot and is exempt from new-config validation |
| Outsourced manufacturing | subcontract BOM/operation config, vendor build helper and received output validated against the same rules |
| Engineering change order / BOM revision activation | activation blocked if existing/new configurations violate HARD rules; affected assemblies reported |
| Order/configuration boundary (`order.demand` config requests, configure-to-order) | validation API called before demand commit; customer/region/order rule sets included |
| Available-to-build | only valid configurations contribute to buildable quantities (invalid options flagged, not suggested) |
| Kits/groups | not manufacturing paths; optional future hook — out of scope here (documented) |
| Unbuild (T-16) | **exempt from compatibility re-validation** (restores an existing valid build); disposition rules from Δ-REUSE apply |

All API and validation records are tenant-scoped; evaluation is deterministic and idempotent for the same
configuration hash/rule-set versions.

## 3. Unbuild disposition and used parts (Δ-REUSE)

`assembly_unbuild_component` gains per-line disposition (optional, default = normal return):
- `disposition = RETURN_TO_STOCK` (**default**) | `SCRAP` | `HOLD`;
- for `RETURN_TO_STOCK`: optional `is_used` (default false), `condition_code_id` (condition registry),
  `return_status_id` (**USED**/REFURB default when `is_used=true`), `target_location_id/bin_id`,
  `allow_resale` (default false for used), and `value_policy = ORIGINAL_COST` (**default**) | `NRV` |
  `MANUAL` with `unit_value` when not original;
- `SCRAP`: no stock return; component value posts to scrap/variance via FS-05; serial/lot consumed as scrapped;
- `HOLD`: returns to a hold/non-committable status pending inspection;
- serial/lot identities preserved with lifecycle history (`IN_STOCK_USED`, `SCRAPPED`, `HOLD`);
  used statuses are seeded non-sellable/non-committable by default (configurable per status);
- valuation: ORIGINAL_COST uses original build cost (default); NRV/MANUAL post a value difference through the
  FS-05 revaluation/adjustment path in the unbuild transaction. Policy is configurable at tenant, assembly
  and per-line level, with the line override requiring `manufacturing.unbuild.dispose`;
- unbuild reversal rules: reversal requires returned components still in the recorded status/qty and posts
  compensating movements; partial reversals not allowed in Phase 1;
- **REWORK** is deferred to the FS-14 repair flow (FS-09 precedes FS-14 in the DAG; no new dependency).
- `assembly.unbuilt` event payload includes per-line dispositions; a `unbuild.disposition.recorded` audit
  event is emitted when used/scrap/hold/NRV/manual is chosen.

## 4. API surface (FS-09 additions; additive contract 1.1.0)

- Attributes: `GET|POST /v1/manufacturing/compat/attributes`, `GET|PATCH /…/attributes/{id}`,
  `GET|PUT /v1/items/{item_id}/compat-attributes` (manufacturing-scoped item attributes).
- Rule sets/rules: `GET|POST /v1/manufacturing/compat/rule-sets`, `GET|PATCH|DELETE /…/rule-sets/{id}`,
  `POST /…/rule-sets/{id}/activate|retire`, `GET|POST /…/rule-sets/{id}/rules`,
  `PATCH|DELETE /…/rules/{id}`.
- Slots: `GET|POST /v1/manufacturing/compat/slots`, `PATCH /…/slots/{id}`.
- Validation/evidence: `POST /v1/manufacturing/configurations/validate`,
  `POST /v1/manufacturing/configurations/simulate`, `GET /v1/manufacturing/configurations/{id}`,
  `GET /v1/manufacturing/configurations` (filter by item/context/result), and
  `GET /v1/manufacturing/compat/report?assembly_item_id&context`.
- Unbuild: request/response extensions on existing `POST /v1/assembly-unbuilds`, plus
  `GET /v1/assembly-unbuilds/{id}` disposition detail and `POST /v1/assembly-unbuilds/{id}/reverse`
  (full reversal; disposition-aware), no new document endpoint.
- Condition codes: `GET|POST /v1/manufacturing/condition-codes`,
  `PATCH /v1/manufacturing/condition-codes/{id}` (configurable used/refurb grades).
- **API count convention:** the spec expresses this as **20 new API rows** (grouped `GET|POST` rows) covering
  25 additive operations; the manifest gate count is the row-based FS-09 total (115).
- Scopes: `manufacturing.compat.manage` (attributes/rules/slots), `manufacturing.compat.validate`
  (validate/simulate/read configurations), `manufacturing.compat.override` (override HARD violations),
  `manufacturing.unbuild.dispose` (used/NRV/manual/HOLD dispositions) — all assignable; `admin` gets them.

## 5. Rules, invariants and counts

| ID | Rule |
|---|---|
| `DM-MFG-12` | Compatibility is evaluated deterministically against all matching rule sets (assembly/BOM **and** order/config/customer/region scopes), ordered by specificity/priority/code; enforcement is configurable (rule > rule set > tenant default HARD), HARD blocks the guarded action, WARN records a violation, override requires the override scope + reason and is audited. |
| `DM-MFG-13` | Compatibility gates apply to **every manufacturing/configuration path** (BOM/BOM-revision activation, matrix generation, WO create/release, WIP completion/backflush, non-WIP build, outsourced mfg, ECO, order/configuration boundary, available-to-build); unbuild restores prior snapshots and is exempt from new-config validation; every validation persists an immutable configuration record. |
| `DM-MFG-14` | Unbuild disposition is per component line and optional: default RETURN_TO_STOCK; SCRAP returns no stock; HOLD returns non-committable; used marking is optional and sets a used/non-sellable status by default; valuation is original-cost by default and configurable (tenant/assembly/line) to NRV/manual; serial/lot identity and quantity/value conservation are preserved; line overrides require the dispose scope and are audited. |
| `INV-33` | **Compatibility/reuse integrity:** no HARD-violating configuration reaches a build/posted quantity; rule-set/scope evaluation is deterministic and evidence-recorded; every unbuild line conserves quantity and value, preserves serial/lot identity, and used/scrap/hold dispositions match the posted stock status. |

**Counts:** tables **280 → 287** (FS-09 **35 → 42**; +7: `compat_attribute_def`,
`compat_attribute_value`, `compat_rule_set`, `compat_rule`, `compat_slot_def`, `compat_configuration`,
`condition_code`); views 21; posting types 31 + 6 commands unchanged; scoped rules **143 → 146**
(+DM-MFG-12/13/14), total **153 → 156**; invariants **INV-01..INV-33**; state-machine specs **43 → 44**
(`compat_rule_set`: DRAFT/ACTIVE/RETIRED), 52 → 53 aggregates; RBAC catalogue **251 → 255**
(241 assignable + 13 service-only + 1 platform-only; +4 FS-09 scopes), 7 role templates, 14 SoD,
18 approval scopes, public 9 / self-service 6 unchanged; FS-09: **42 tables / 115 endpoints / 24 events /
5 jobs / 3 migrations / 8 exit commands** (3 compatibility + disposition tests added before `record`);
FS-09 provided contract **`manufacturing.execution@1.1.0`** (additive; consumers `>=1.0.0 <2.0.0` unchanged);
`plan_version` **1.5.0**; no DAG/wave/coverage changes; other 15 specs unchanged except a seed-only note in FS-03 adding the FS-03-owned `USED`/`REFURB` status rows.

## 6. Test plan (FS-09 exit-gate additions)

- Rule engine truth tables (all rule types, multi-rule AND, precedence/specificity, priority/code tiebreaks,
  effective dates, conflict handling, attribute typing); capacity math (PCIe/DIMM/PSU/rack/length).
- Dual scope: assembly/BOM rules **and** order/customer/region/channel rules both applied; override
  precedence and override auditing.
- Generic path matrix: BOM/revision activation, matrix generation, phantom explosion, WO create/release,
  WIP completion/backflush, non-WIP build, outsourced manufacturing, ECO implementation, order/configuration
  validation (contract test with order boundary), available-to-build filtering; unbuild exempt.
- Enforcement configurability: tenant default, rule-set mode, per-rule override, HARD block/WARN record,
  override permission + reason, invalid configuration records immutable.
- Unbuild dispositions: return default, optional used/condition/status/resale, scrap (no stock, value to
  scrap), hold (non-committable), valuation policies original/NRV/manual with FS-05 postings, serial/lot
  lifecycle, reversal rules, qty/value conservation, RBAC scopes, audit events.
- Golden data-center configs (e.g. Intel/AMD socket variants, GPU length/slot count, PSU wattage, DIMM
  population) and a full unbuild-to-used-on-shelf-to-salvage journey.

## 7. Document update map (binding)

| Document | Change |
|---|---|
| `design/CONFIG-REUSE-ADDENDUM.md` | this file (authoritative) |
| `design/ARCHITECTURE.md` | §2.3/T-16 unbuild disposition semantics; **§4.7** compatibility engine/dual scopes/generic applicability (pointer from existing §4.5); §8 D-32; §7.3 INV-33; §0 counts (287/146/156/INV-33/255/241/44-53); §11 assumptions |
| `design/DATA-MODEL.md` | §2 counts (FS-09 42/total 287); §12 seven new tables with columns/constraints/indexes/RLS, compat_rule_set state machine, unbuild component disposition columns; §19 SM count (44/53); §20 counts (146/156, platform 20); §21.1 FS-09 `0020_manufacturing_execution` creates new tables (no new revision); §21.2 condition/status seeds; §23 DMX-29 EXT; §24 totals |
| `feature-sets/FS-09.md` | tables 42; compatibility + unbuild disposition use cases/APIs/events/config/tests; 115 endpoints/24 events/5 jobs/3 migrations/8 exit commands; DM-MFG-12/13/14 + INV-33; contract 1.1.0; §14 amendment record |
| `design/FEATURE-SETS.md` | §1 FS-09 42 tables / total 287; §7.10 FS-09 scope paragraph (compat engine dual scope/generic paths + unbuild dispositions); no DAG changes |
| `design/feature-sets.json` | FS-09 provides `manufacturing.execution` 1.0.0→**1.1.0**; FS-09 exit_checks +3 compatibility/disposition tests before `record`; `plan_version` 1.5.0; nothing else |
| `design/permissions.json` | +4 assignable scopes (`manufacturing.compat.manage|validate|override`, `manufacturing.unbuild.dispose`); counts 255/241; admin template updated; plan_version 1.5.0 |
| `PLAN.md` | §3.1 Δ-CFG/Δ-REUSE (D-32) row; new §4.5 manufacturing compatibility & unbuild reuse; §12 item 18; counts (287/146/156/INV-33/255/241); header/status after verification |
| `README.md` | capability line + counts; status |
| `verification/VERIFICATION-REPORT.md` | delta audit, refreshed snapshot, verdict |

## 8. Resolved defaults and deferrals

- **Enforcement default:** HARD, explicitly configurable per rule/rule-set/tenant; override scope+reason
  required and audited.
- **Rule scope:** both assembly/BOM and order/configuration/customer/region/channel; both apply (AND).
- **Used valuation:** ORIGINAL_COST default; configurable at tenant / assembly / per-line. Assembly-level
  policy is a tenant-setting JSONB map (`manufacturing.unbuild.value_policy_by_assembly`) plus per-line
  override (no extra column needed); NRV source defaults to **CURRENT_COST** (FS-05 current cost proxy) with
  `LAST_PURCHASE_PRICE` selectable via tenant setting; MANUAL requires `unit_value`.
- **Unbuild dispositions:** RETURN_TO_STOCK (default), SCRAP, HOLD; used marking optional; **REWORK deferred**
  to the FS-14 repair flow (no new FS-09 dependency).
- **Generic applicability:** all manufacturing paths in §2; kits/groups are not manufacturing (future hook).
- This amendment adds no new feature set and does not change deployment waves; FS-09 growth is additive and
  gated by its existing exit gate.

