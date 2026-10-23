# FEATURE COVERAGE — disposition of all 1,348 source topics

**Status:** executable coverage baseline · **Version:** 1.0.0 · **Date:** 2026-10-21
**Owner:** architect-feature-sets · **Machine-readable companion:** [`feature-coverage.json`](feature-coverage.json) (authoritative per-topic disposition + totals).
**Inputs:** `research/features-item-inventory.json` (412 topics, `iri-001..iri-412`), `research/features-mfg-procurement-wms.json` (936 topics, `mpw-001..mpw-936`); binding scope per [`FEATURE-SET-FRAMEWORK.md`](FEATURE-SET-FRAMEWORK.md) §1/§6 and [`FEATURE-SETS.md`](FEATURE-SETS.md); Lead decisions D1–D8.

> **100% accounted:** all 1,348 catalogued topics have exactly one disposition — 769 are owned by a Phase-1 backend feature set (FS-00…FS-15) and 579 are explicitly deferred to Phase 2+ (`DEFERRED-UI` 61, `DEFERRED-MOBILE` 126, `DEFERRED-ANALYTICS` 46, `DEFERRED-INTEGRATION` 156, `DEFERRED-SUITEAPP` 190). Zero missing, zero duplicates, zero unmapped, zero gaps. SCM Mobile is permanently excluded and is absent from the catalogs (0 topics).

---

## 1. Catalog universe and assertions

| Catalog | Topics | Id range | Source area (help root) | Contiguous? |
|---|---|---|---|---|
| `features-item-inventory.json` | 412 | `iri-001 … iri-412` | Item Record Management ([book_N2164398](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/book_N2164398.html)) + Inventory Management ([book_N2249433](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/book_N2249433.html)) | yes (no gaps) |
| `features-mfg-procurement-wms.json` | 936 | `mpw-001 … mpw-936` | Manufacturing ([book_1506002637](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/book_1506002637.html)), Vendors/Purchasing/Receiving ([set_N2360130](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/set_N2360130.html)), Warehouse Management ([book_156356229851](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/book_156356229851.html)) | yes (no gaps) |
| **Total** | **1,348** | | | |

Catalog metadata used: `id`, `domain`, `path[]`, `title`, `url`, `kind`, `summary`, `backend_relevance` (the research crawl's classification: `core-backend` 885, `suiteapp` 204, `mobile` 126, `ui-only` 49, `analytics` 59, `integration` 22, `unclear` 3). Per-topic URLs are preserved in the JSON catalogs and are not repeated here.

**SCM Mobile assertion (validated):** the coverage builder asserts that no catalog topic has root area `SCM Mobile` or domain `scm-mobile` (main SCM Mobile book was never crawled — PLAN §14). The only catalog reference to the bundle is `mpw-342` (“Installing the SCM Mobile Bundle”), which sits inside the catalogued **Manufacturing Mobile** area and is dispositioned `DEFERRED-MOBILE` with the rest of Manufacturing Mobile. `EXCLUDED-SCM-MOBILE` therefore has **0** topics in this coverage set; the disposition value exists for completeness and future re-crawls.

---

## 2. Method: deterministic rule engine

Coverage was built by a Python script from the two catalogs using **ordered rules (first match wins)** — no hand-edited per-topic overrides. Because a topic can match several plausible rules, precedence is fixed and documented:

1. **Explicit exceptions** (Lead decisions / architecture traceability) — evaluated first.
2. **Class buckets** — `backend_relevance` ∈ {`mobile`, `analytics`, `ui-only`, `integration`} → matching deferred bucket (all such topics are Phase-2+; D4/framework §1).
3. **D4 SuiteApp inclusions** — specific `suiteapp`-classified ranges included at backend-semantics level (FS-02 effective-date pricing, FS-09 available-to-build, FS-12 quality, FS-14 warranty/customer-part/UPC), then remaining `suiteapp` topics → `DEFERRED-SUITEAPP`.
4. **Core-backend mapping rules** — explicit `(catalog, id-range) → FS` rules (below), including explicit deferrals where a core-backend-classified topic is out of Phase-1 scope (finance/AP boundary, OneWorld, EDI, SuiteFlow customisation, Work Instructions/Traveler, import tooling).
5. **Unclear classifications** — three topics mapped individually: `iri-172` (Narrative Insights) → `DEFERRED-ANALYTICS`; `mpw-486` (vendor-bill approval FAQ) → FS-08; `mpw-936` (WMS FAQ) → FS-11.
6. No topic may remain unmatched (the builder asserts this).

Every disposition carries a human-readable `reason` in `feature-coverage.json` (rule rationale + the topic title context). The disposition value itself is the machine key: `FS-00` … `FS-15` or one of the five deferred buckets / `EXCLUDED-SCM-MOBILE`.

**Class bucket definitions**

| Rule | Selector | Disposition | Rationale |
|---|---|---|---|
| C-MOBILE | `backend_relevance = mobile` | `DEFERRED-MOBILE` | mobile app/device workflow; all mobile is Phase 2+, SCM Mobile permanently excluded (D8, framework §1) |
| C-ANALYTICS | `backend_relevance = analytics` | `DEFERRED-ANALYTICS` | BI workbooks/datasets/report presentation; Phase 2+ (framework §1) |
| C-UI | `backend_relevance = ui-only` | `DEFERRED-UI` | end-user screens/dashboards/printing; Phase 2+ (framework §1) |
| C-INTEGRATION | `backend_relevance = integration` | `DEFERRED-INTEGRATION` | external integration/import tooling; Phase 2+ (framework §1, PLAN §12) |
| C-SUITEAPP | `backend_relevance = suiteapp` (after D4 inclusions) | `DEFERRED-SUITEAPP` | SuiteApp packaging/install/UI outside D4 backend-semantics inclusion; Phase 2+ |

**Exception evaluated before C-ANALYTICS:** `iri-334..348` → **FS-15** (ARCHITECTURE §9 maps inventory reporting `iri-334..351` to the reporting module: FS-15 owns the backend read models/APIs; report/workbook rendering stays deferred, hence `iri-349..351` workbooks → `DEFERRED-ANALYTICS`).

**D4 SuiteApp inclusions (backend-semantics level, marked `EXT` in DATA-MODEL §23)**

| Disposition | Catalog ids | Content included in Phase 1 | Deferred remainder |
|---|---|---|---|
| FS-02 | `iri-141..143, 146..163, 165..166` | Effective-date pricing: process flow, supported item types, update types, creating/scheduling/overlap/status/troubleshooting semantics | install/roles/browser topics (`iri-144,145`) and CSV import (`iri-164`) |
| FS-09 | `mpw-101, 106..108, 110` | Available-to-build: preference, calculation, calculation on sales order, use without Advanced BOM | install/prereqs/roles/browser/glossary/Item-360 display (`mpw-102..105, 109, 111`) |
| FS-12 | `iri-360..363, 365, 371, 373..377, 380..384` | Quality specs, inspection plans/triggers/queues, inspections/results, COA, images, derived fields/preferences | SuiteApp roles/prereqs/workflow customisation/saved searches/glossary (`iri-364, 366..367, 372, 378..379`), mobile (`iri-385`), integration (`iri-368..370`), reports (`iri-386`) |
| FS-14 | `iri-134, 136..138, 352..359, 387..390` | Warranty preferences/item setup, registration, claims, claim actions, customer part numbers, auto-assign UPC registry | SuiteApp install topic (`iri-135`), import integrations (`iri-139..140`) |

---

## 3. Phase-1 mapping rules (catalog topics owned by each FS)

Each row is the exact ordered rule range(s) yielding the topics owned by that FS: `core-backend`-classified rules plus the D4 SuiteApp inclusions above. `mpw` ids carry the `mpw-` prefix. The contiguous ranges shown are the *resulting* owned ids; for FS-08 and FS-11 each range set also contains one `unclear`-classified FAQ topic mapped by content (§2 step 5: `mpw-486`, `mpw-936`). Rules are disjoint by construction and exhaustive for in-scope topics; the JSON is authoritative per topic.

| FS | Catalog id ranges owned | Scope theme |
|---|---|---|
| FS-00 | `iri-198`; `mpw-797..798, 800` | feature-enablement registry / platform RBAC catalogue (roles, permissions, feature flags) |
| FS-01 | `iri-001..004, 007..010, 013..014, 016..017, 019..021, 070..077, 079..080, 082..084, 110..125, 127..133, 203, 212, 227, 230`; `mpw-810..811` | item records/types/extensions, UoM engine, matrix, kit/group, barcodes/aliases, item-location settings |
| FS-02 | `iri-023..033, 167..184` + D4 inclusions `iri-141..143, 146..163, 165..166` | item pricing, quantity schedules, pricings groups, advanced pricing rules/collections/coupons, effective-date updates |
| FS-03 | `iri-199, 205, 207, 211, 215, 228, 248..249, 251..253, 257..258`; `mpw-808` | locations, zones, bins, inventory statuses/availability flags |
| FS-04 | `iri-195..197, 200..202, 206, 208, 213..214, 216, 218..222, 231..241, 243..247, 256, 259..262, 264..267`; `mpw-815` | ledger posting, balances/availability, commitments, transfers/TOs, adjustments/worksheets, distribution, imports |
| FS-05 | `iri-035..057, 059..068, 229, 263`; `mpw-031, 038..047, 052..057, 716..751` | costing methods, recalculation/COGS, standard costing/rollup/revaluation, group average, landed cost |
| FS-06 | `iri-085..107, 250` | serial/lot identity, history, numbered-inventory tracking, advanced bin×numbered behaviour |
| FS-07 | `iri-209..210, 223..226`; `mpw-873..877, 879..883` | count plans/cycle counts, snapshots, recount, approval, variance posting |
| FS-08 | `mpw-416..427, 430, 444, 447, 449..451, 453..454, 472..474, 478..486, 490..493, 497..498, 570..584, 586..588, 601..610, 622..639, 646..649, 656..666, 683..684, 699..709, 711..712, 714..715` | vendor master/pricing/contracts, requisitions/requests/RFQ, PO lifecycle, receipts, bills/variance/3-way match, credits, vendor returns, drop-ship/special-order |
| FS-09 | `mpw-001..027, 029..030, 035, 060..085, 088..089, 112..184, 199, 210..219, 403..415` + D4 inclusions `mpw-101, 106..108, 110` | assembly/BOM/revisions, work orders, WIP, routing/work centres/calendars, outsourced, ECO, scheduling inputs, available-to-build |
| FS-10 | `iri-204, 268..286, 288..304, 307..309, 311..321`; `mpw-090` | AIM/reorder data, demand plans, supply/MRP/DRP, allocation, ATP, planning action messages |
| FS-11 | `iri-255`; `mpw-792..794, 796, 799, 802..807, 809, 812..814, 816..818, 823..828, 850..851, 853..863, 865..868, 870, 872, 884..892, 894..905, 907..915, 917..923, 925..928, 932..934, 936` | warehouse/process config/strategies/system rules, inbound/putaway, waves/pick/pack/ship/manifest, WMS replenishment, status tasks, cart/device sessions |
| FS-12 | D4 inclusions `iri-360..363, 365, 371, 373..377, 380..384` | quality specs/inspections/triggers/queues/COA (SuiteApp backend semantics) |
| FS-13 | `iri-323..331` | consignment agreements, consigned receipt/ownership/adjust/count/consumption, consigned accounts |
| FS-14 | D4 inclusions `iri-134, 136..138, 352..359, 387..390` | warranty registration/claims/repairs, customer part numbers, auto-assign UPC registry |
| FS-15 | `iri-217, 334..348` | read-model query surface: balance search + reporting read models (presentation deferred) |

---

## 4. Explicit deferrals (beyond class buckets)

Rules applied to `core-backend`-classified topics because the capability is outside Phase-1 backend scope (Lead decisions D2/D4/PLAN §12). All are Phase 2+ work, never silently dropped.

| Bucket | Catalog ids | Reason / target |
|---|---|---|
| `DEFERRED-INTEGRATION` | `iri-005..006, 011..012, 015, 126, 139..140, 164, 180, 242, 368..370` | import tooling/Item Creation Assistant, revenue-recognition & tax item config (finance/tax boundary), intercompany non-arm's-length transfers (OneWorld), quality REST API/plugins (D4) |
| `DEFERRED-INTEGRATION` | `mpw-196, 435..443, 445..446, 448, 455..460, 462, 464..471, 504..514, 517..562, 564..567, 569, 585, 589, 593..600, 686..698, 837..844, 929..931, 935` | Work-Instructions import, vendor time tracking/1099/tax, delivery methods, Bill Capture/email capture, vendor payments/in-transit payments/prepayments/payment runs/credit limits (finance-AP boundary, D2), OneWorld centralized/cross-subsidiary purchasing, WMS shipping integration + EDI, ASN sending |
| `DEFERRED-INTEGRATION` | `mpw-768..775, 778, 780, 782..784, 788..791` | SuiteProcurement trading-partner network and document formats (D4 explicitly deferred) |
| `DEFERRED-SUITEAPP` | `iri-108..109, 135, 144..145, 364, 366..367, 372, 378..379, 391..402, 404..406` | Lot Auto Numbering, Allergen Statements, SCM SuiteApp install, Effective-Date-Pricing install/roles, quality roles/prereqs/workflow customisation/saved searches/glossary, Smart Count SuiteApp admin (mobile counts → `DEFERRED-MOBILE`) |
| `DEFERRED-SUITEAPP` | `mpw-032..034, 036..037, 048..051, 092..100, 102..105, 109, 111, 185..189, 191..192, 194..195, 200..202, 228..241, 253..300, 324..333, 461, 463, 487..489, 494..496, 611..621, 640..645, 650..655, 672..682, 761..767, 776..777, 779, 781, 786..787, 795` | Costed-BOM/Cost-Variance install surfaces, Supply 360, available-to-build install/glossary, Work Instructions/Traveler (D4 deferred), Manufacturing Scheduler admin/UI-support (computation kept in FS-09), Advanced Manufacturing SuiteApp (D4 deferred), Transaction Email Capture install, SuiteFlow approval-workflow customisation (D-23: Phase 1 uses declarative approvals), Purchase-Order Approval Workflow SuiteApp, SuiteProcurement install/UI, WMS SuiteApps install |
| `DEFERRED-UI` | `iri-018, 022, 034, 078, 081, 254, 305, 322, 407..412` | items list/related info, price lists, label printing, bin putaway worksheet printing, Supply Planning Workbench, Supply Chain Control Tower, Item 360 Dashboard |
| `DEFERRED-UI` | `mpw-028, 058..059, 086..087, 091, 190, 193, 197..198, 203..209, 429, 431..434, 475..477, 499..503, 515..516, 563, 568, 590..592, 667..671, 710, 820..822, 906` | BOM/label/work-order printing, Production Control Board, Work-Instructions/Traveler forms and reports, Manufacturing Scheduler views, mailing labels, vendor-center portal, bill/PO/credit printing, Employee-Center approval screens/reminders, payment voucher printing, custom WMS templates, pick-ticket printing |
| `DEFERRED-MOBILE` | `iri-385, 403`; `mpw-242..252, 301..323, 334..402, 801, 829..836, 845..849, 852, 864, 869, 878, 893, 916, 924` | Quality mobile data collection, Smart Count mobile, Manufacturing Mobile (all 69), Advanced Manufacturing mobile, WMS mobile app setup/device basics/scanning/cart workflows, picking/putaway/count/replenishment via app |
| `DEFERRED-ANALYTICS` | `iri-058, 069, 172, 185..194, 287, 306, 310, 332..333, 349..351, 386`; `mpw-220..227, 428, 452, 685, 713, 752..760, 785, 819, 871` | inventory/cost/pricing/manufacturing/purchasing/consignment/quality workbooks & datasets, vendor dashboards, Narrative Insights, SuiteAnalytics templates, Procurement Dashboard, report rendering |

Notes: (a) `DEFERRED-INTEGRATION` deliberately includes the **finance/AP boundary** (payments, prepayments, payment runs, credit limits) — Phase 1 emits `finance_journal_intent` records through the FS-00 `FinancePort` instead (D2), so those topics are integration/Phase-2 rather than missing behaviour. (b) SuiteFlow-based approval-workflow customisation is deferred while *declarative* approvals (thresholds, SoD, 3-way match criteria/states) are Phase 1 in FS-00/FS-08 (D-23). (c) Work Instructions/Traveler is deferred as a SuiteApp even where individual data-subtab topics are core-backend-classified (D4).

---

## 5. Totals

### 5.1 Per disposition

| Disposition | Topics | Share |
|---|---|---|
| **FS-00** | 4 | 0.30% |
| **FS-01** | 57 | 4.23% |
| **FS-02** | 50 | 3.71% |
| **FS-03** | 14 | 1.04% |
| **FS-04** | 42 | 3.12% |
| **FS-05** | 88 | 6.53% |
| **FS-06** | 24 | 1.78% |
| **FS-07** | 16 | 1.19% |
| **FS-08** | 116 | 8.61% |
| **FS-09** | 160 | 11.87% |
| **FS-10** | 52 | 3.86% |
| **FS-11** | 89 | 6.60% |
| **FS-12** | 16 | 1.19% |
| **FS-13** | 9 | 0.67% |
| **FS-14** | 16 | 1.19% |
| **FS-15** | 16 | 1.19% |
| **Subtotal in Phase-1 backend sets** | **769** | **57.05%** |
| DEFERRED-UI | 61 | 4.53% |
| DEFERRED-MOBILE | 126 | 9.35% |
| DEFERRED-ANALYTICS | 46 | 3.41% |
| DEFERRED-INTEGRATION | 156 | 11.57% |
| DEFERRED-SUITEAPP | 190 | 14.09% |
| EXCLUDED-SCM-MOBILE | 0 | 0.00% |
| **Subtotal deferred/excluded** | **579** | **42.95%** |
| **TOTAL** | **1,348** | **100.00%** |

### 5.2 Per FS with catalog class split (why FS-12/FS-14 have no `core-backend`-classified topics)

| FS | Owned topics | of which `core-backend` class | other class | Note |
|---|---|---|---|---|
| FS-00 | 4 | 4 | 0 | — |
| FS-01 | 57 | 57 | 0 | — |
| FS-02 | 50 | 27 | 23 `suiteapp` | 23 effective-date-pricing topics included by D4 |
| FS-03 | 14 | 14 | 0 | — |
| FS-04 | 42 | 42 | 0 | — |
| FS-05 | 88 | 88 | 0 | — |
| FS-06 | 24 | 24 | 0 | — |
| FS-07 | 16 | 16 | 0 | — |
| FS-08 | 116 | 115 | 1 `unclear` (`mpw-486`, mapped by content) | — |
| FS-09 | 160 | 155 | 5 `suiteapp` | available-to-build included by D4 |
| FS-10 | 52 | 52 | 0 | — |
| FS-11 | 89 | 88 | 1 `unclear` (`mpw-936`, mapped by content) | — |
| FS-12 | 16 | 0 | 16 `suiteapp` | Quality Management is SuiteApp-classified; included at backend-semantics level by D4 |
| FS-13 | 9 | 9 | 0 | — |
| FS-14 | 16 | 0 | 16 `suiteapp` | Warranties/customer parts/UPC are SuiteApp-classified; included by D4 |
| FS-15 | 16 | 1 (`iri-217`) | 15 `analytics` (reporting read models per ARCHITECTURE §9) | report rendering/workbooks deferred |
| **Total** | **769** | **692** | **77** | every FS owns ≥1 topic |

**Validation rules satisfied:** each of the 16 feature sets owns at least one catalog topic; FS-12 and FS-14 are the only sets whose topics are exclusively `suiteapp`-classified, and both are mandated by D4 (quality inspections/specs; warranty/customer-part/UPC) — recorded rather than weakening the inclusion decision.

---

## 6. Deferred work grouped with target phase

| Target phase | Buckets | Topics | Representative capability | Notes |
|---|---|---|---|---|
| **Phase 2 (UI/mobile/analytics)** | `DEFERRED-UI` + `DEFERRED-MOBILE` + `DEFERRED-ANALYTICS` | 233 | Item 360, Supply Planning Workbench, Control Tower, dashboards, printing/templates, all mobile apps (Manufacturing Mobile, WMS mobile, Smart Count), SuiteAnalytics workbooks/datasets/reports | backend data/task records already exist (FS-11 server-side tasks, FS-15 read models) so Phase 2 is presentation/client work; SCM Mobile **never** |
| **Phase 2+ (SuiteApp packaging & customisation)** | `DEFERRED-SUITEAPP` | 190 | Advanced Manufacturing SuiteApp, Work Instructions/Traveler, Scheduler UI, Costed BOM/Cost Variance install surfaces, Supply 360, Smart Count admin, SuiteFlow approval customisation, Lot Auto Numbering, Allergen Statements, WMS custom templates | declarative Phase-1 equivalents exist where documented (approvals, cost rollup, scheduling computation) |
| **Phase 2+ (integrations / finance boundary)** | `DEFERRED-INTEGRATION` | 156 | vendor payments/prepayments/payment runs, AP credit limits, bill capture/email capture, EDI/carrier/WMS shipping integration, ASN sending, SuiteProcurement trading partners, OneWorld/cross-subsidiary/intercompany, import tooling | Phase 1 boundary interfaces already published: `platform.finance` journal intents (D2), order boundary v0 (D3), import API (`import_batch`) |
| **Never** | `EXCLUDED-SCM-MOBILE` | 0 | SCM Mobile book | permanently excluded; not catalogued |

All 579 deferred topics have an explicit disposition + reason in `feature-coverage.json`; nothing is "miscellaneous/unknown". The deferred envelope is also summarised in [`FEATURE-SETS.md`](FEATURE-SETS.md) §5.

---

## 7. Gap list

**No gaps.** All 1,348 catalogued topics are accounted for:

- 0 topics with no disposition
- 0 duplicate dispositions (exactly one JSON key per catalog id)
- 0 topics mapped to a non-existent feature set
- 0 forward-phase/dependency-invalid mappings (every owning FS exists in the manifest with wave ≥ its dependencies)
- 0 SCM Mobile topics in the catalogs (assertion passes)
- no in-scope backend capability intentionally left unimplemented: every core-backend-classified topic not owned by a set has an explicit D2/D4/PLAN §12 deferral row in §4 of this document.

---

## 8. Machine-readable format (`feature-coverage.json`)

```jsonc
{
  "iri-001": {"disposition": "FS-01", "reason": "Item record creation/header fields owned by FS-01 (DATA-MODEL §4)"},
  "…": { "…": "…" },                       // exactly 1,348 catalog-id keys
  "totals": {
    "total": 1348,
    "by_catalog": {"iri": 412, "mpw": 936},
    "by_disposition": {"FS-00": 4, "…": 0, "DEFERRED-SUITEAPP": 190, "EXCLUDED-SCM-MOBILE": 0},
    "by_fs": {"FS-00": 4, "…": "FS-15": 16},
    "core_backend_class_by_fs": {"FS-00": 4, "…": "FS-15": 1},
    "validation": {"ids_expected": 1348, "ids_present": 1348, "duplicates": 0, "missing": 0,
                   "unmapped": 0, "fs_sets_covered": 16, "scm_mobile_catalog_roots": 0,
                   "every_fs_has_mapped_topic": true, "…": "…"}
  }
}
```

Consumer contract: `tools/feature_sets.py coverage` (shared tooling task) must read this file and assert at minimum: 1,348 keys + `totals`; every disposition in `FS-00…FS-15` or the five deferred buckets or `EXCLUDED-SCM-MOBILE`; every FS-00…FS-15 present; `sum(by_disposition) == by_catalog.iri + by_catalog.mpw == 1348`; no `EXCLUDED-SCM-MOBILE` entries.

---

## 9. Validation evidence (reproducible)

```bash
# builder (deterministic; rules documented in §2–§4) — writes design/feature-coverage.json
#   loads research/features-item-inventory.json + research/features-mfg-procurement-wms.json,
#   asserts 1,348 unique contiguous ids, no SCM-Mobile root area, applies ordered rules,
#   asserts 0 unmapped and every FS owns ≥1 topic, prints the totals tables above.

# independent checks (no external dependencies; run from inventory-platform/)
python3 - <<'PY'
import json
cov = json.load(open('design/feature-coverage.json'))
m = json.load(open('design/feature-sets.json'))
sets = {s['id'] for s in m['feature_sets']}
d = {k: v for k, v in cov.items() if k != 'totals'}
assert len(d) == 1348 == cov['totals']['total']
assert set(d) == {f'iri-{i:03d}' for i in range(1, 413)} | {f'mpw-{i:03d}' for i in range(1, 937)}
allowed = sets | {'DEFERRED-UI','DEFERRED-MOBILE','DEFERRED-ANALYTICS','DEFERRED-INTEGRATION',
                  'DEFERRED-SUITEAPP','EXCLUDED-SCM-MOBILE'}
assert all(v['disposition'] in allowed and v['reason'] for v in d.values())
counts = {k: sum(1 for v in d.values() if v['disposition'] == k) for k in allowed}
assert counts == cov['totals']['by_disposition']
assert all(counts[fs] >= 1 for fs in sets)
assert counts['EXCLUDED-SCM-MOBILE'] == 0
assert sum(counts.values()) == 1348
print('coverage OK: 1,348/1,348 accounted; %d in-scope, %d deferred' % (
      sum(cov['totals']['by_disposition'][fs] for fs in sets),
      sum(v for k, v in cov['totals']['by_disposition'].items() if k.startswith('DEFERRED'))))
PY

# plan-level gate once the shared tool lands
python tools/feature_sets.py coverage
```

**Recorded result at hand-off:** coverage builder → `1,348 topics, 0 unmapped, 16/16 FS covered`; independent validator → `0 errors / 0 warnings`, dispositions partition exactly: 769 in-scope + 579 deferred = 1,348; `EXCLUDED-SCM-MOBILE = 0`; SCM-Mobile root-area assertion passed.
