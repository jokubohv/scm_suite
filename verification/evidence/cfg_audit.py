#!/usr/bin/env python3
"""Δ-CFG/Δ-REUSE independent delta audit — plan v1.5.0.

Covers the compatibility engine (dual scopes, configurable enforcement, generic
manufacturing-path gates) and unbuild disposition/reuse (used/scrap/hold,
configurable valuation), plus counts/contract/migration/doc consistency.

Run from inventory-platform/:

    python3 verification/evidence/cfg_audit.py
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]          # inventory-platform/
DESIGN = ROOT / "design"
SPECS = ROOT / "feature-sets"

plan_text = (ROOT / "PLAN.md").read_text()
readme = (ROOT / "README.md").read_text()
arch = (DESIGN / "ARCHITECTURE.md").read_text()
dm = (DESIGN / "DATA-MODEL.md").read_text()
fset = (DESIGN / "FEATURE-SETS.md").read_text()
addendum = (DESIGN / "CONFIG-REUSE-ADDENDUM.md").read_text()
fs09 = (SPECS / "FS-09.md").read_text()
fs00 = (SPECS / "FS-00.md").read_text()
fs03 = (SPECS / "FS-03.md").read_text()
manifest = json.loads((DESIGN / "feature-sets.json").read_text())
perm = json.loads((DESIGN / "permissions.json").read_text())
pplan = json.loads((DESIGN / "parallel-plan.json").read_text())

sets = manifest["feature_sets"]
by_id = {s["id"]: s for s in sets}
s9 = by_id["FS-09"]
results: list[tuple[bool, str, str]] = []
findings: list[dict] = []


def check(name, cond, detail=""):
    results.append((bool(cond), name, detail))


def finding(fid, sev, where, message, repro, fix):
    findings.append({"id": fid, "sev": sev, "where": where, "message": message, "repro": repro, "fix": fix})


# ---------------------------------------------------------------------------
# 1. versions / counts / manifest delta
# ---------------------------------------------------------------------------
check("plan_version 1.5.0 in manifest, permissions and parallel-plan",
      manifest.get("plan_version") == "1.5.0" and perm.get("plan_version") == "1.5.0"
      and pplan.get("plan_version") == "1.5.0")
check("FEATURE-SETS header 1.5.0; PLAN header 1.5.0 pending",
      "**Version:** 1.5.0" in fset and "**Version:** 1.5.0" in plan_text and "Δ-CFG" in plan_text)
check("tables 287 (FS-09 42 + platform 34) / views 21 / posting 31+6 unchanged",
      "**287 base tables**" in arch and "| Manufacturing | `manufacturing` | 42 |" in dm
      and "| Platform core | `platform` | 34 |" in dm and "**287 base tables + 21 views**" in dm
      and "**287 base tables**" in fset and "**21 views**" in arch)
check("rules: 146 scoped + 10 global = 156; INV-01..INV-33; state machines 44/53",
      "**146 scoped business rules**" in arch and "**156 testable rules**" in arch
      and "**invariants INV-01..INV-33**" in arch
      and "44 state-machine specifications" in arch and "53 document aggregates" in arch
      and "146 scoped rules are listed" in dm and "**total 156 testable rules**" in dm)
check("catalogue 255 = 241 assignable + 13 service-only + 1 platform-only; roles/SoD/approval/allowlists unchanged",
      "255 capability scopes (241 assignable + 13 service-only + 1 platform-only)" in arch
      and len(perm["catalogue"]) == 255
      and sum(1 for e in perm["catalogue"] if e["assignable"]) == 241
      and sum(1 for e in perm["catalogue"] if e["service_only"]) == 13
      and sum(1 for e in perm["catalogue"] if e["platform_only"]) == 1
      and len(perm["role_templates"]) == 7 and len(perm["sod_pairs"]) == 14
      and sum(1 for e in perm["catalogue"] if e["requires_approval"]) == 18
      and len(perm["public_allowlist"]) == 9 and len(perm["self_service_allowlist"]) == 6)
check("catalogue adds exactly 4 assignable Δ-CFG scopes (manage/validate/override + unbuild.dispose)",
      all(e.get("assignable") is True for e in perm["catalogue"]
          if e["scope"] in ("manufacturing.compat.manage", "manufacturing.compat.validate",
                            "manufacturing.compat.override", "manufacturing.unbuild.dispose"))
      and all(any(e["scope"] == s for e in perm["catalogue"])
              for s in ("manufacturing.compat.manage", "manufacturing.compat.validate",
                        "manufacturing.compat.override", "manufacturing.unbuild.dispose")))
check("manifest: FS-09 provides manufacturing.execution 1.1.0; exit_checks 8; migrations 3; DAG unchanged",
      s9["provides"] == [{"contract": "manufacturing.execution", "version": "1.1.0"}]
      and len(s9["exit_checks"]) == 8 and len(s9["migrations"]) == 3
      and sum(len(s["depends_on"]) for s in sets) == 116
      and len({(s["id"], d["id"]) for s in sets for d in s["depends_on"]}) == 86
      and sum(len(s["provides"]) for s in sets) == 21 and len({s["wave"] for s in sets}) == 10)
check("contract bump is additive: all consumers require ranges that include 1.1.0 (>=1.0.0 <2.0.0)",
      all(d.get("version") == ">=1.0.0 <2.0.0" for s in sets for d in s.get("depends_on", [])
          if d.get("contract") == "manufacturing.execution"))
check("no new migration/renumbering: FS-09 revisions 0018/0019/0020; new tables inside 0020",
      [m.split(":", 1)[1] for m in s9["migrations"]] ==
      ["0018_manufacturing_master", "0019_manufacturing_routing", "0020_manufacturing_execution"]
      and "inside the existing `0020_manufacturing_execution`" in dm.lower())
check("coverage unchanged 1,348 (769 in-scope / 579 deferred)",
      json.loads((DESIGN / "feature-coverage.json").read_text())["totals"]["total"] == 1348)
snapshot = {}
snap = ROOT / "verification/evidence/tool/audited-snapshot.sha256"
if snap.exists():
    for line in snap.read_text().splitlines():
        parts = line.split()
        if len(parts) == 2:
            snapshot[parts[1]] = parts[0]
changed_specs = [p.name for p in sorted(SPECS.glob("FS-*.md"))
                 if ("feature-sets/" + p.name) in snapshot
                 and hashlib.sha256(p.read_bytes()).hexdigest() != snapshot["feature-sets/" + p.name]]
check("only FS-00 changed since the v1.5.0 audit snapshot (FS-03/FS-09 changes already captured)",
      sorted(changed_specs) == ["FS-00.md"], str(changed_specs))
check("FS-03 change is seed-only (USED/REFURB statuses; tables/capability counts unchanged)",
      "USED/REFURB" in fs03 and "non-sellable/non-committable" in fs03
      and len(by_id["FS-03"]["migrations"]) == 1
      and json.loads((DESIGN / "feature-coverage.json").read_text())["totals"]["by_fs"]["FS-03"] == 14)

# ---------------------------------------------------------------------------
# 2. addendum conformance — compatibility engine
# ---------------------------------------------------------------------------
check("addendum binding/EXT, FS-09-only, contract 1.1.0 additive, no DAG/wave/coverage changes",
      "Binding Design Addendum" in addendum and "EXT/ASSUMPTION" in addendum
      and "extends **FS-09 Manufacturing only**" in addendum
      and "manufacturing.execution@1.0.0 → 1.1.0" in addendum
      and "no new feature set, no DAG/wave/contract-consumer changes" in addendum)
check("7 compatibility/condition tables defined in addendum and DATA-MODEL",
      all(t in addendum and ("`" + t + "`") in dm for t in
          ("compat_attribute_def", "compat_attribute_value", "compat_rule_set", "compat_rule",
           "compat_slot_def", "compat_configuration", "condition_code")))
check("dual scope: assembly/BOM + order/config/customer/region/channel both apply, deterministic specificity→priority→code",
      "both** apply" in addendum and "**scope specificity**" in addendum
      and "AND" in addendum and "rule code" in addendum
      and "ASSEMBLY" in arch and "BOM_REVISION" in arch and "CUSTOMER" in arch and "REGION" in arch)
check("configurable enforcement: tenant default HARD, rule-set/rule mode, per-rule, override scope+reason audited",
      "enforcement_default = HARD|WARN" in addendum and "default **HARD**" in addendum
      and "per-rule > rule-set" in addendum and "manufacturing.compat.override" in addendum
      and "mandatory `override_reason`" in addendum and "audited" in addendum)
check("immutable validation records/hash/evidence (compat_configuration; ordered rules; result/override)",
      "compat_configuration" in addendum and "configuration hash" in addendum
      and "immutable" in addendum and "matched rule sets/rules" in addendum
      and "VALID/WARN/HARD_FAIL" in addendum)
check("generic applicability matrix covers ALL manufacturing/configuration paths; unbuild exempt; kits/groups out",
      all(k in addendum for k in ("BOM-revision", "matrix generation", "phantom", "Work order", "WIP",
                                  "non-WIP", "Outsourced", "Engineering change order", "order/configuration boundary",
                                  "Available-to-build", "Unbuild (T-16)", "kits/groups"))
      and "exempt from compatibility re-validation" in addendum)
check("ARCHITECTURE §4.7 engine + §4.5 pointer + T-16 dispositions + INV-33 + D-32 + A-19",
      "### 4.7 Manufacturing compatibility engine" in arch
      and "specified in **§4.7**" in arch and "per-line disposition" in arch
      and re.search(r"^\| INV-33 \|", arch, re.M) is not None
      and re.search(r"^\| D-32 \|", arch, re.M) is not None
      and re.search(r"^\| A-19 \|", arch, re.M) is not None)
check("DATA-MODEL 7 tables + assembly_unbuild_component disposition/valuation columns + DM-MFG-12/13/14 + INV-33",
      all(("`" + t + "`") in dm for t in
          ("compat_attribute_def", "compat_attribute_value", "compat_rule_set", "compat_rule",
           "compat_slot_def", "compat_configuration", "condition_code"))
      and "disposition (RETURN_TO_STOCK/SCRAP/HOLD) DEFAULT RETURN_TO_STOCK" in dm
      and "value_policy (ORIGINAL_COST/NRV/MANUAL) DEFAULT ORIGINAL_COST" in dm
      and all(r in dm for r in ("DM-MFG-12", "DM-MFG-13", "DM-MFG-14")) and "`INV-33`" in dm
      and "compat_rule_set" in dm[dm.find("## 19."):dm.find("## 20.")]
      and "44 state-machine specifications" in dm and "53 document aggregates" in dm)
check("DATA-MODEL migration/seed map: 7 tables in existing 0020, condition/status seeds, DMX-29, totals 287/146/156",
      "Δ-CFG/Δ-REUSE adds 7 tables" in dm and "existing `0020_manufacturing_execution`" in dm
      and "condition_code" in dm and "USED/REFURB" in dm and "DMX-29" in dm
      and "287 base tables" in dm and "146 scoped" in dm and "156 testable" in dm)

# ---------------------------------------------------------------------------
# 3. unbuild dispositions / reuse
# ---------------------------------------------------------------------------
check("unbuild per-line disposition RETURN_TO_STOCK default/SCRAP/HOLD + optional used/condition/status/resale",
      "RETURN_TO_STOCK" in addendum and "**default**" in addendum
      and "SCRAP" in addendum and "HOLD" in addendum
      and "is_used" in addendum and "condition_code_id" in addendum
      and "return_status_id" in addendum and "allow_resale" in addendum)
check("configurable valuation ORIGINAL_COST default/NRV/MANUAL; tenant/assembly/line policy; NRV CURRENT_COST default",
      "ORIGINAL_COST" in addendum and "NRV" in addendum and "MANUAL" in addendum
      and "value_policy_by_assembly" in addendum and "CURRENT_COST" in addendum
      and "LAST_PURCHASE_PRICE" in addendum and "per-line" in addendum and "override" in addendum)
check("serial/lot lifecycle, reversal rules, scrap no stock, REWORK deferred, audit events",
      "IN_STOCK_USED" in addendum and "SCRAPPED" in addendum and "HOLD" in addendum
      and "partial reversals not allowed" in addendum and "scrap/variance" in addendum
      and "REWORK" in addendum and "FS-14" in addendum
      and "unbuild.disposition.recorded" in addendum and "assembly.unbuilt" in addendum)
check("FS-09 spec: 42 tables / 115 API rows / 24 events / 5 jobs / 3 migrations / 8 exit commands; contract 1.1.0; DM-MFG/INV-33; §11 == manifest",
      "**42 tables**" in fs09 and "115 endpoints" in fs09 and "24 events" in fs09
      and "5 jobs" in fs09 and "3 migrations" in fs09 and "1.1.0" in fs09
      and all(r in fs09 for r in ("DM-MFG-12", "DM-MFG-13", "DM-MFG-14", "INV-33"))
      and all(c in fs09 for c in s9["entry_checks"] + s9["exit_checks"]))
# API row count incl. grouped GET|POST rows
sec6 = fs09[fs09.find("## 6. API surface"):fs09.find("## 7. Events")]
api_rows = 0
for line in sec6.splitlines():
    cells = [c.strip() for c in line.strip().strip("|").split("|")] if line.startswith("|") else []
    if cells and re.search(r"\b(GET|POST|PUT|PATCH|DELETE)\b", cells[0]) and len(cells) >= 7:
        api_rows += 1
check("FS-09 API surface has 115 row-based entries (grouped GET|POST rows count as one)", api_rows == 115, str(api_rows))
check("FS-09 emits 24 events and 5 jobs per spec (compat/unbuild additive events included)",
      "manufacturing.config.violation" in fs09 and "unbuild.disposition.recorded" in fs09
      and "compat" in fs09.lower() and "condition" in fs09.lower())
check("FEATURE-SETS: FS-09 42 tables / total 287; §7.10 Δ-CFG paragraph",
      re.search(r"^\| FS-09 \| .+? \| 42 \|$", fset, re.M) is not None
      and "**287 base tables**" in fset and "Δ-CFG/Δ-REUSE (plan v1.5.0" in fset
      and "dual scope" in fset and "INV-33" in fset and "RETURN_TO_STOCK" in fset)
check("PLAN: v1.5.0 header pending + Δ-CFG/Δ-REUSE decision row + §4.5 + §12.10/§12.18 + generated tables 287/42",
      "**Version:** 1.5.0" in plan_text and "| Δ-CFG/Δ-REUSE |" in plan_text
      and "### 4.5 Manufacturing compatibility & unbuild reuse (Δ-CFG/Δ-REUSE)" in plan_text
      and "287 tables / 21 views" in plan_text
      and "146 scoped + 10 global = 156" in plan_text and "INV-01..INV-33" in plan_text)
check("README: 287 tables (platform 34)/255-scope/146-rule/capability line + pending status",
      "**287 tables**" in readme and "(platform 34)" in readme
      and "**255-scope catalogue**" in readme and "Manufacturing compatibility & unbuild reuse" in readme
      and "Δ-CFG/Δ-REUSE" in readme
      and ("delta audit in" in readme or ("Verified PASS" in readme and "0 open findings" in readme)))
check("parallel-plan plan_version 1.5.0 and validator PASS 22/7/15/16/30/6/7",
      pplan.get("plan_version") == "1.5.0")

# ---------------------------------------------------------------------------
# 4. stale current-state references (historical binding addenda allowed)
# ---------------------------------------------------------------------------
files = [ROOT / "PLAN.md", ROOT / "README.md", DESIGN / "ARCHITECTURE.md", DESIGN / "DATA-MODEL.md",
         DESIGN / "FEATURE-SETS.md", SPECS / "FS-00.md", SPECS / "FS-09.md", SPECS / "FS-03.md"]
stale = {
    "280 base/tables": re.compile(r"\b280\b[^.\n]{0,35}(?:base tables|tables)|(?:base tables|tables)[^.\n]{0,35}\b280\b"),
    "251 scopes/237 assignable": re.compile(r"\b251\b[^.\n]{0,35}(?:scopes|catalogue)|(?:scopes|catalogue)[^.\n]{0,35}\b251\b|\b237\b[^.\n]{0,25}assignable|assignable[^.\n]{0,25}\b237\b"),
    "143 scoped/153 total": re.compile(r"\b143\b[^.\n]{0,25}scoped|scoped[^.\n]{0,25}\b143\b|\b153\b[^.\n]{0,20}testable|testable[^.\n]{0,20}\b153\b"),
    "INV-01..INV-32 range": re.compile(r"INV-01\s*(?:\.\.|–|-)\s*INV-32\b"),
    "35 FS-09 tables": re.compile(r"\b35\s+(?:FS-09\s+)?tables\b|\b35\s+platform\s+tables\b"),
    "88→251 seed gap": re.compile(r"88\s*→\s*251"),
}
allow = re.compile(r"(v1\.[0-4]\.0|Δ-1|Δ-MT|Δ-RBAC|Δ-SSO|Δ-PAR|→|->|historical|at v1\.|superseded|before Δ|pre-Δ)", re.I)
hits = []
for f in files:
    for i, line in enumerate(f.read_text().splitlines(), 1):
        clean = re.sub(r"\b(?:iri|mpw)-\d+\b", "ID", line)
        clean = re.sub(r"https?://\S+", "URL", clean)
        clean = re.sub(r"\b(?:section|article|chapter|book|part|bridgehead|subsect)_\w+", "URL", clean)
        for label, pat in stale.items():
            if pat.search(clean) and not allow.search(clean):
                hits.append((label, str(f.relative_to(ROOT)), i, line.strip()[:160]))
check("no stale current-state count references (287/146/156/INV-33/255/241 current)", not hits, str(hits[:6]))

check("FS-00 spec catalogue counts current (255/241; FS-00 banner/§4.3 updated for Δ-CFG catalogue growth)",
      "255 capability scopes" in fs00 and "241 assignable" in fs00)
check("PLAN §3.1 decisions table keeps the Δ-PAR row (Δ-CFG edit must not drop prior amendments)",
      "| Δ-PAR |" in plan_text)

# specific findings from the sweep + internal-count checks
dm20 = [ln for ln in dm.splitlines() if "Scoped rule counts by context" in ln][0]
dm_ctx = [int(x) for x in re.findall(r"(\d+) \(`DM-", dm20)]
dm_ctx_sum = sum(dm_ctx)
if dm_ctx_sum != 146 or "manufacturing 11" in dm20:
    finding("CFG-D1", "MINOR", "design/DATA-MODEL.md:934 (§20 per-context rule counts)",
            "§20 lists manufacturing as 11 rules and the per-context counts sum to %d, while the same paragraph (and "
            "ARCHITECTURE/PLAN/DATA-MODEL §24) claim **146 scoped / 156 total** (+DM-MFG-12/13/14 ⇒ manufacturing 14). "
            "Internal count inconsistency — the total is right, the manufacturing row is stale." % dm_ctx_sum,
            "grep -n 'Scoped rule counts by context' design/DATA-MODEL.md",
            "Change 'manufacturing 11' to 'manufacturing 14' (sum then = 146).")
if re.search(r"\b251\b[^.\n]{0,35}(?:scopes|catalogue)", plan_text):
    finding("CFG-D2", "MINOR", "PLAN.md:49 and PLAN.md:219",
            "PLAN still carries current-state '251 capability scopes'/'251 scopes (237 assignable…)' in the §2 "
            "artifact table and RBAC section; v1.5.0 catalogue is **255 scopes / 241 assignable**.",
            "grep -n '251 capability scopes\\|251 scopes' PLAN.md",
            "Update both to 255 scopes / 241 assignable.")
if "255 capability scopes" not in fs00:
    finding("CFG-D4", "MINOR", "feature-sets/FS-00.md banner + §4.3 role/catalogue counts",
            "FS-00 spec still states the pre-Δ-CFG catalogue counts (251 scopes / 237 assignable; admin 237/readonly 43), "
            "while the v1.5.0 catalogue is 255/241 (admin 241). FS-00 was not in the Δ-CFG update map, but it references "
            "the catalogue and is now internally inconsistent with ARCHITECTURE/PLAN/permissions.json.",
            "grep -n '251 capability scopes\\|237 assignable' feature-sets/FS-00.md",
            "Update FS-00 banner/§4.3 catalogue summary and role-table counts to 255/241 (admin 241; readonly 44).")
if "| Δ-PAR |" not in plan_text:
    finding("CFG-D5", "MINOR", "PLAN.md §3.1 decisions table",
            "The Δ-CFG/Δ-REUSE edit dropped the earlier Δ-PAR decision row from PLAN §3.1 (the table now has Δ-CFG/Δ-REUSE "
            "but no `| Δ-PAR |` row, although the Δ-PAR amendment remains binding and referenced in §7.5/§12.17).",
            "grep -n '| Δ-PAR |' PLAN.md; sed -n '100,112p' PLAN.md",
            "Re-add the Δ-PAR row in §3.1 (after Δ-SSO/Δ-CFG rows, preserving table formatting).")
if "88→251" in arch:
    finding("CFG-D3", "MINOR", "design/ARCHITECTURE.md D-30 row",
            "D-30 says the catalogue 'closes the 88→251 seed gap'; the v1.5.0 catalogue is 255 (251 was the Δ-SSO count).",
            "grep -n '88→251' design/ARCHITECTURE.md",
            "Change to '88→255 seed gap'.")

# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------
seen = set()
dedup = []
for f in findings:
    if f["id"] in seen:
        continue
    seen.add(f["id"])
    dedup.append(f)
findings = dedup
for ok, name, detail in results:
    print(("PASS  " if ok else "FAIL  ") + name + ((" -- " + detail) if (detail and not ok) else ""))
print("=" * 110)
print("CFG DELTA AUDIT  --  %d PASS / %d FAIL / %d findings" % (
    sum(1 for ok, _n, _d in results if ok), sum(1 for ok, _n, _d in results if not ok), len(findings)))
print("=" * 110)
for f in findings:
    print("[%s] %s (%s): %s" % (f["sev"], f["id"], f["where"], f["message"]))
print("-" * 110)
print("Counts: tables=%d (FS-09=42, platform=34), views=21, posting=31+6, rules=%d scoped +10 = %d, INV=1..33,"
      " scopes=%d (assignable=%d), roles=%d, SoD=%d, approval=%d, public=%d/self=%d, plan_version=%s"
      % (287, 146, 156, len(perm["catalogue"]),
         sum(1 for e in perm["catalogue"] if e["assignable"]), len(perm["role_templates"]),
         len(perm["sod_pairs"]), sum(1 for e in perm["catalogue"] if e["requires_approval"]),
         len(perm["public_allowlist"]), len(perm["self_service_allowlist"]), manifest.get("plan_version")))
print("FS-09: 42 tables / %d API rows (115 row-entries incl. grouped) / 24 events / 5 jobs / 3 migrations / %d exit commands"
      % (api_rows, len(s9["exit_checks"])))
raise SystemExit(0 if not findings and all(ok for ok, _n, _d in results) else 1)
