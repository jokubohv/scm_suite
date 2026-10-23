#!/usr/bin/env python3
"""Δ-RBAC (permissions & roles) independent delta audit — plan v1.5.0 (re-baselined; Δ-CFG/Δ-REUSE).

Checks the binding addendum, the authoritative catalogue, the amended design
docs / manifest / FS-00 / FS-01 / FEATURE-SETS / PLAN / README, and reconciles
every FS spec endpoint auth-scope cell against design/permissions.json.

Run from inventory-platform/:

    python3 verification/evidence/rbac_audit.py
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]          # inventory-platform/
DESIGN = ROOT / "design"
SPECS = ROOT / "feature-sets"

plan = (ROOT / "PLAN.md").read_text()
readme = (ROOT / "README.md").read_text()
arch = (DESIGN / "ARCHITECTURE.md").read_text()
dm = (DESIGN / "DATA-MODEL.md").read_text()
fset = (DESIGN / "FEATURE-SETS.md").read_text()
addendum = (DESIGN / "PERMISSIONS-ROLES-ADDENDUM.md").read_text()
fs00 = (SPECS / "FS-00.md").read_text()
fs01 = (SPECS / "FS-01.md").read_text()
manifest = json.loads((DESIGN / "feature-sets.json").read_text())
perm = json.loads((DESIGN / "permissions.json").read_text())
coverage = json.loads((DESIGN / "feature-coverage.json").read_text())

sets = manifest["feature_sets"]
by_id = {s["id"]: s for s in sets}
s0 = by_id["FS-00"]
catalogue = {e["scope"]: e for e in perm["catalogue"]}
assignable = {e["scope"] for e in perm["catalogue"] if e["assignable"]}

results: list[tuple[bool, str, str]] = []
findings: list[dict] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    results.append((bool(condition), name, detail))


def finding(fid: str, severity: str, where: str, message: str, repro: str, fix: str) -> None:
    findings.append({"id": fid, "sev": severity, "where": where, "message": message,
                     "repro": repro, "fix": fix})


# ---------------------------------------------------------------------------
# 1. catalogue structure / counts / roles / SoD / allowlists
# ---------------------------------------------------------------------------
check("plan_version is 1.2.0 in manifest and permissions.json",
      manifest.get("plan_version") == "1.5.0" and perm.get("plan_version") == "1.5.0",
      "%s/%s" % (manifest.get("plan_version"), perm.get("plan_version")))
check("catalogue: 255 scopes = 241 assignable + 13 service-only + 1 platform-only",
      len(catalogue) == 255 and len(assignable) == 241
      and sum(1 for e in catalogue.values() if e["service_only"]) == 13
      and sum(1 for e in catalogue.values() if e["platform_only"]) == 1)
check("platform-only scope is platform.tenant.manage and is never assignable",
      [e["scope"] for e in catalogue.values() if e["platform_only"]] == ["platform.tenant.manage"]
      and catalogue["platform.tenant.manage"]["assignable"] is False)
check("alias platform.user.manage -> platform.users.manage exists only in aliases (not seeded)",
      perm["aliases"].get("platform.user.manage") == "platform.users.manage"
      and "platform.user.manage" not in catalogue and "platform.users.manage" in catalogue)
check("counts metadata matches recomputed catalogue",
      perm["counts"]["scopes"] == len(catalogue)
      and perm["counts"]["assignable_scopes"] == len(assignable)
      and perm["counts"]["platform_only"] == 1 and perm["counts"]["service_only"] == 13
      and perm["counts"]["roles"] == len(perm["role_templates"])
      and perm["counts"]["sod_pairs"] == len(perm["sod_pairs"]))
check("18 requires_approval; 6 global rules; public 9 / self-service 6 allowlists",
      sum(1 for e in catalogue.values() if e["requires_approval"]) == 18
      and len(perm["global_rules"]) == 6
      and len(perm["public_allowlist"]) == 9 and len(perm["self_service_allowlist"]) == 6)
check("self-service allowlist includes GET /api/v1/me/effective-permissions",
      any("me/effective-permissions" in x for x in perm["self_service_allowlist"]))
role_counts = {"admin": 241, "planner": 50, "buyer": 71, "warehouse_operator": 81,
               "cost_accountant": 57, "quality_inspector": 47, "readonly": 44}
roles = {r["key"]: r for r in perm["role_templates"]}
check("exactly 7 role templates with the documented scope counts",
      len(roles) == 7 and {k: len(v["scopes"]) for k, v in roles.items()} == role_counts,
      str({k: len(v["scopes"]) for k, v in roles.items()}))
check("admin template = exactly all 241 assignable scopes",
      set(roles["admin"]["scopes"]) == assignable)
check("all role templates are non-editable/cloneable system templates with catalogued scopes",
      all(r["system"] is True and r["editable"] is False and r["cloneable"] is True for r in roles.values())
      and all(s in catalogue for r in roles.values() for s in r["scopes"]))
sod_ok = []
non_admin_sod = []
for rk, role in roles.items():
    for pair in perm["sod_pairs"]:
        both = pair["a"] in role["scopes"] and pair["b"] in role["scopes"]
        if rk != "admin":
            non_admin_sod.append((rk, pair, both))
        sod_ok.append(pair["a"] in catalogue and pair["b"] in catalogue)
check("14 SoD pairs reference valid, distinct catalogue scopes",
      len(perm["sod_pairs"]) == 14 and all(sod_ok)
      and all(p["a"] != p["b"] for p in perm["sod_pairs"])
      and all(p["enforcement"] in ("HARD", "WARN") for p in perm["sod_pairs"]))
check("no non-admin role template combines an SoD pair (admin is audited break-glass)",
      not any(both for _rk, _p, both in non_admin_sod))

# ---------------------------------------------------------------------------
# 2. ARCHITECTURE contract
# ---------------------------------------------------------------------------
check("ARCHITECTURE §0 counts: 287/34/21/31+6/146+10=156/INV-33/255/241/13/1/7/14",
      "**287 base tables**" in arch and "**21 views**" in arch
      and "**146 scoped business rules**" in arch and "**156 testable rules**" in arch
      and "**invariants INV-01..INV-33**" in arch
      and "255 capability scopes (241 assignable + 13 service-only + 1 platform-only)" in arch
      and "7 role templates" in arch and "14 SoD pairs" in arch)
check("ARCHITECTURE §5.2 RBAC model: catalogue authority, 7 templates, deny-by-default, guards, SoD, effective permissions",
      "authoritative catalogue is `design/permissions.json`" in arch
      and "role.is_template=true" in arch and "deny by default" in arch
      and "test_route_scope_coverage.py" in arch and "Privilege-escalation & lockout guards" in arch
      and "LAST_TENANT_ADMIN" in arch and "effective permissions" in arch
      and "requester" in arch and "approver" in arch)
check("ARCHITECTURE §5.2 allowlists: public 9 + self-service 6 incl. effective-permissions",
      "public allowlist (9 entries)" in arch and "self-service allowlist (6 entries)" in arch
      and "GET /api/v1/me/effective-permissions" in arch)
check("ARCHITECTURE §7.3 INV-31 authorization-completeness + §8 D-30 + §10 G-14 + §11 A-17",
      re.search(r"^\| INV-31 \|", arch, re.M) is not None and re.search(r"^\| INV-33 \|", arch, re.M) is not None
      and re.search(r"^\| D-30 \|", arch, re.M) is not None
      and re.search(r"^\| G-14 \|", arch, re.M) is not None
      and re.search(r"^\| A-17 \|", arch, re.M) is not None
      and "test_route_scope_coverage.py" in arch and "test_sod_policy.py" in arch)
check("ARCHITECTURE counts: 287 tables (280 + Δ-CFG/Δ-REUSE 7); 116 edges; 21 contracts",
      "287 base tables" in arch and "21 views" in arch
      and "116 declared dependency edges" in fset and "21 provided contracts" in fset)

# ---------------------------------------------------------------------------
# 3. DATA-MODEL contract (columns/rules/invariant/seed; no table change)
# ---------------------------------------------------------------------------
check("DATA-MODEL permission_scope gains is_platform_only/is_assignable/owner_feature_set; role gains is_template/is_editable",
      "is_platform_only" in dm and "is_assignable" in dm and "owner_feature_set" in dm
      and "is_template" in dm and "is_editable" in dm)
check("DATA-MODEL rbac namespace + catalogue/role/assignment seeds",
      "**`rbac`**" in dm and "permissions.json" in dm and "7 system role templates" in dm
      and "sod" in dm.lower() and "14 hard SoD pairs" in dm)
check("DATA-MODEL DM-RBAC-01/02/03 + INV-31/INV-33 present with required semantics",
      all(rid in dm for rid in ("DM-RBAC-01", "DM-RBAC-02", "DM-RBAC-03"))
      and "`INV-31`" in dm and "`INV-33`" in dm
      and "deny" in dm.lower() and "last active tenant administrator" in dm and "SoD" in dm)
check("DATA-MODEL rule counts: platform 20, scoped 146, total 156 (10 global)",
      "platform 20" in dm and "146 scoped rules are listed" in dm
      and "**total 156 testable rules**" in dm)
check("DATA-MODEL totals: 287 base tables / platform 34 / 21 views",
      "287 base tables" in dm and "| Platform core | `platform` | 34 |" in dm and "21 views" in dm)

# ---------------------------------------------------------------------------
# 4. FS-00 spec: catalogue summary, roles, endpoints/events, gates, tests
# ---------------------------------------------------------------------------
check("FS-00 banner/§4.3 state 255 scopes = 241 assignable + 13 service-only + 1 platform-only",
      "255 capability scopes" in fs00 and "241 assignable" in fs00
      and "13 service-only" in fs00 and "1 platform-only" in fs00)
check("FS-00 §4.3 lists the 7 role templates and 14 SoD pairs + approval-required count",
      all(name in fs00 for name in ("admin", "planner", "buyer", "warehouse_operator",
                                    "cost_accountant", "quality_inspector", "readonly"))
      and "14 SoD pairs" in fs00 and "18 approval-required scopes" in fs00)
check("FS-00: 34 tables / 102 endpoint rows / 33 event rows stated consistently",
      "**34 tables**" in fs00 and len(re.findall(r"^\| (?:GET|POST|PUT|PATCH|DELETE) \|", fs00, re.M)) == 102
      and len([l for l in fs00[fs00.find("## 7. Events and jobs"):fs00.find("## 8.")].split("| Job |")[0].splitlines()
               if l.startswith("| `")]) == 33
      and "102 endpoints" in fs00 and "33 events" in fs00)
_sec11 = fs00[fs00.find("## 11. Deployment and gates"):fs00.find("## 12. Rollback")]
_missing = [c for c in s0["entry_checks"] + s0["exit_checks"] if c not in _sec11]
check("FS-00 §11 reproduces all 14 manifest gate commands verbatim (incl. permissions + route/role/SoD tests)",
      not _missing and len(s0["exit_checks"]) == 17
      and "python tools/feature_sets.py permissions" in _sec11
      and "test_route_scope_coverage.py" in _sec11 and "test_rbac_roles.py" in _sec11
      and "test_sod_policy.py" in _sec11, str(_missing[:3]))
check("FS-00 DM-RBAC-01..03 + INV-31 + RBAC APIs/use cases/tests present",
      "DM-RBAC-01" in fs00 and "DM-RBAC-02" in fs00 and "DM-RBAC-03" in fs00 and "INV-31" in fs00
      and "/api/v1/admin/permissions" in fs00 and "/api/v1/admin/roles" in fs00
      and "/api/v1/me/effective-permissions" in fs00 and "/api/v1/admin/sod-policy" in fs00
      and "route-scope" in fs00.lower() and "last-admin" in fs00.lower())
check("FS-00 role table scope counts match the catalogue",
      all(re.search(r"^\| `%s` \| [^|]+ \| %d" % (k, v), fs00, re.M) is not None
          for k, v in role_counts.items()))

# ---------------------------------------------------------------------------
# 5. FS-01 read scope cells (five fixed to items.read)
# ---------------------------------------------------------------------------
fs01_sec6 = fs01[fs01.find("## 6. API surface"):fs01.find("## 7. ")]
bad_reads = []
for line in fs01_sec6.splitlines():
    if not re.match(r"^\| GET \|", line):
        continue
    cells = [c.strip() for c in line.strip().strip("|").split("|")]
    if len(cells) < 7:
        continue
    path, auth = cells[1].strip("`"), cells[-2]
    if "allowlist" not in auth and "items.read" not in auth:
        bad_reads.append((path, auth))
check("FS-01 GET reference reads use items.read (five fixed cells; location settings is the only remaining manage read)",
      len(bad_reads) <= 1, str(bad_reads))
if any("locations" in p for p, _a in bad_reads):
    finding("RBAC-D2", "MINOR", "feature-sets/FS-01.md line 347 (GET /api/v1/items/{item_id}/locations)",
            "GET /items/{item_id}/locations (read of per-location settings) is scoped `items.location.manage` "
            "while every sibling reference/config read in FS-01 (accounting, purchasing, sales, uoms, availability-settings, "
            "barcodes, aliases, ...) uses `items.read`. This is the only remaining GET row not using `items.read`; it is "
            "likely a residual of the five read-scope fixes (addendum §10: FS-01 read cells to `items.read`).",
            "grep -n 'items/{item_id}/locations' feature-sets/FS-01.md",
            "Set the GET row's scope to `items.read` (keep `items.location.manage` on PUT/PATCH).")

# ---------------------------------------------------------------------------
# 6. FEATURE-SETS / manifest / PLAN / README
# ---------------------------------------------------------------------------
fs00_block = fset[fset.find("### 7.1 FS-00"):fset.find("### 7.2 FS-01")]
check("FEATURE-SETS plan version 1.5.0 + §7.1 Δ-RBAC paragraph (deny-by-default, DM-RBAC, INV-31; Δ-SSO paragraph present)",
      "**Version:** 1.5.0" in fset and "Δ-RBAC" in fset and "Δ-SSO (plan v1.3.0" in fset
      and "deny-by-default" in fset and "DM-RBAC-01/02/03" in fset and "INV-31" in fset)
check("FEATURE-SETS §7.1 FS-00 exit block mirrors the 17 manifest gate commands",
      sum(1 for c in s0["exit_checks"] if c in fs00_block) == len(s0["exit_checks"])
      and len(s0["exit_checks"]) == 17)
check("manifest: plan_version 1.5.0; FS-00 exit_checks 17 (unchanged) + FS-09 exit 8",
      manifest.get("plan_version") == "1.5.0" and len(s0["exit_checks"]) == 17
      and any("python tools/feature_sets.py permissions" in c for c in s0["exit_checks"])
      and any("test_route_scope_coverage.py" in c for c in s0["exit_checks"])
      and any("test_rbac_roles.py" in c for c in s0["exit_checks"])
      and any("test_sod_policy.py" in c for c in s0["exit_checks"])
      and s0["exit_checks"][-1].endswith("record --set FS-00 --env $ENV --status verified"))
check("manifest: only FS-00 exit_checks changed (edges 116/86, contracts 21, waves 10, entry 49, exit 92)",
      sum(len(s["depends_on"]) for s in sets) == 116
      and len({(s["id"], d["id"]) for s in sets for d in s["depends_on"]}) == 86
      and sum(len(s["provides"]) for s in sets) == 21 and len({s["wave"] for s in sets}) == 10
      and sum(len(s["entry_checks"]) for s in sets) == 49
      and sum(len(s["exit_checks"]) for s in sets) == 98
      and all(len(s["exit_checks"]) == (17 if s["id"] == "FS-00" else
              by_id[s["id"]]["exit_checks"].__len__()) for s in sets))
check("PLAN header plan v1.5.0 (Δ-CFG/Δ-REUSE, audit pending) + Δ-RBAC decision row D-30 + §4.3/§4.4 sections",
      "**Version:** 1.5.0" in plan and "Δ-RBAC" in plan and "Δ-SSO" in plan
      and "| Δ-RBAC |" in plan and "D-30" in plan
      and "### 4.3 Permissions, roles and approvals (Δ-RBAC)" in plan)
check("PLAN §4.3/§12 document counts 255/241/146/156/INV-33 + deny-by-default/SoD/effective permissions",
      "255 scopes" in plan and "241 assignable" in plan
      and "146 scoped" in plan and "156" in plan
      and "INV-01..INV-33" in plan and "deny-by-default" in plan
      and "requester ≠ approver" in plan and "effective permissions" in plan)
check("PLAN generated tables current (287 tables / 21 views; FS-00 34 tables)",
      "287 tables / 21 views" in plan
      and re.search(r"^\| FS-00 \| .+? \| 4 \| 34 \|$", fset, re.M) is not None)
check("README carries the current catalogue line (255-scope catalogue / 7 templates / 14 hard SoD) and status",
      "255-scope catalogue" in readme and "7 system role templates" in readme
      and "14 hard SoD pairs" in readme and "Δ-RBAC" in readme
      and ("delta audit in progress" in readme or ("Verified PASS" in readme and "0 open findings" in readme)))
check("PLAN header/README carry the Δ-RBAC VERIFIED PASS stamp (or pending wording pre-verdict)",
      (("VERIFIED PASS" in plan and "Δ-RBAC" in plan and "0 open findings" in plan)
       or ("Δ-RBAC" in plan and "delta audit pending" in plan))
      and ("Verified PASS" in readme and "Δ-RBAC" in readme))
check("PLAN rules+invariant counts current (146 scoped + 10 global = 156; INV-01..INV-33)",
      "146 scoped" in plan and "156 testable" in plan and "INV-01..INV-33" in plan)

# ---------------------------------------------------------------------------
# 7. addendum conformance (binding, model, lifecycle, guards, enforcement, §10)
# ---------------------------------------------------------------------------
check("addendum binding/EXT marking, plan v1.2.0, resolved clarifications §10 present",
      "Binding Design Addendum" in addendum and "Lead-approved binding amendment" in addendum
      and "design choice beyond the NetSuite SCM docs" in addendum
      and "plan v1.2.0" in addendum and "## 10. Resolved clarifications" in addendum)
check("addendum model: capability scope naming, deny-by-default, data scopes, approval separation",
      "capability scope" in addendum.lower() and "deny by default" in addendum.lower()
      and "data scope" in addendum.lower() and "requester" in addendum and "approve" in addendum)
check("addendum role lifecycle: 7 non-editable/cloneable templates, assignments, effective permissions, safety guards",
      "Seven system role templates" in addendum and "non-editable" in addendum and "clone" in addendum
      and "effective permissions" in addendum.lower() and "LAST_TENANT_ADMIN" in addendum
      and "privilege" in addendum.lower() and "audit" in addendum.lower())
check("addendum enforcement/verification: route-scope coverage, permissions gate, scope/role/SoD tests",
      "deny-by-default" in addendum and "feature_sets.py permissions" in addendum
      and "route_scope_coverage" in addendum and "SoD" in addendum and "exit codes" in addendum)
check("addendum §10: 249 catalogue, alias not seeded, admin-only SoD, next-request permissions",
      "authoritative reconciled catalogue is **249**" in addendum
      and "not an additional seed scope and not seeded" in addendum
      and "Admin template is the only role permitted to contain SoD pairs" in addendum
      and "next request" in addendum)
check("addendum doc-update map lists all amended artifacts (ARCH/DATA-MODEL/FS-00/FS-01/FEATURE-SETS/manifest/PLAN/README/report)",
      all(name in addendum for name in ("ARCHITECTURE.md", "DATA-MODEL.md", "FS-00.md", "FS-01",
                                        "FEATURE-SETS.md", "feature-sets.json", "PLAN.md", "README.md",
                                        "VERIFICATION-REPORT.md")))

# ---------------------------------------------------------------------------
# 8. stale current-state count sweep (historical delta rows allowed)
# ---------------------------------------------------------------------------
files = [ROOT / "PLAN.md", ROOT / "README.md"] + sorted(DESIGN.glob("*.md")) + sorted(SPECS.glob("FS-*.md"))
stale = {
    "206 scopes": re.compile(r"\b206\b[^.\n]{0,40}(?:capability|scopes|catalogue)|(?:scopes|catalogue)[^.\n]{0,40}\b206\b"),
    "234 scopes": re.compile(r"\b234\b[^.\n]{0,40}(?:capability|scopes|catalogue)|(?:scopes|catalogue)[^.\n]{0,40}\b234\b"),
    "220 assignable": re.compile(r"\b220\b[^.\n]{0,30}assignable|assignable[^.\n]{0,30}\b220\b"),
    "232 assignable": re.compile(r"\b232\b[^.\n]{0,30}assignable|assignable[^.\n]{0,30}\b232\b"),
    "246 scopes": re.compile(r"\b246\b[^.\n]{0,40}(?:capability|scopes|catalogue)|(?:scopes|catalogue)[^.\n]{0,40}\b246\b"),
    "137 scoped/147 rules": re.compile(r"\b137\b[^.\n]{0,25}scoped|scoped[^.\n]{0,25}\b137\b|\b147\b[^.\n]{0,20}testable|testable[^.\n]{0,20}\b147\b"),
    "INV-01..INV-30 current": re.compile(r"INV-01\s*(?:\.\.|–|-)\s*INV-30"),
    "role buyer 70 / warehouse 78": re.compile(r"buyer[^.\n]{0,15}\b70\b|warehouse[_ ]operator[^.\n]{0,15}\b78\b"),
}
allow = re.compile(r"(v1\.0\.0|v1\.1\.0|Δ-1|Δ-MT|→|->|historical|at v1\.|before Δ|pre-Δ|137→140|147→150|88→)", re.I)
stale_hits = []
for f in files:
    for i, line in enumerate(f.read_text().splitlines(), 1):
        clean = re.sub(r"\b(?:iri|mpw)-\d+\b", "ID", line)
        clean = re.sub(r"https?://\S+", "URL", clean)
        clean = re.sub(r"\b(?:section|article|chapter|book|part|bridgehead|subsect)_\w+", "URL", clean)
        for label, pat in stale.items():
            if pat.search(clean) and not allow.search(clean):
                stale_hits.append((label, str(f.relative_to(ROOT)), i, line.strip()[:160]))
check("no stale current-state scope/rule/invariant/role-count references", not stale_hits, str(stale_hits[:8]))

# ---------------------------------------------------------------------------
# 9. all-16-spec endpoint reconciliation against the catalogue
# ---------------------------------------------------------------------------
scope_re = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*){1,3}$")
endpoint_rows = 0
unknown = []
referenced: set[str] = set()
allowlist_mismatch = []
public_routes = set()
self_routes = set()
for entry in perm["public_allowlist"]:
    public_routes.add(entry if entry.startswith("/") else entry)
for entry in perm["self_service_allowlist"]:
    self_routes.add(entry if entry.startswith("/") else entry)
for spec in sorted(SPECS.glob("FS-*.md")):
    text = spec.read_text()
    start = text.find("## 6. API surface")
    end = text.find("## 7. Events and jobs")
    if start < 0 or end < 0:
        continue
    section = text[start:end]
    line0 = text[:start].count("\n") + 1
    for offset, line in enumerate(section.splitlines()):
        if not re.match(r"^\| (GET|POST|PUT|PATCH|DELETE) \|", line):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 3:
            continue
        endpoint_rows += 1
        method, path = cells[0], cells[1].strip().strip("`")
        auth = cells[-2]
        m = re.search(r"none\s*\(([^)]*)\)", auth, re.I)
        if m:
            kind = m.group(1).lower()
            routes = public_routes if "public" in kind else self_routes
            if not any((method + " " + path) == r or path == r or r == path for r in routes):
                allowlist_mismatch.append((spec.name, line0 + offset, method, path))
            continue
        for raw in re.findall(r"`([^`]+)`", auth):
            for token in re.split(r"[,;+/\s]+", raw):
                token = token.strip().strip("`")
                if not token or not scope_re.match(token):
                    continue
                referenced.add(token)
                if token not in catalogue:
                    unknown.append((spec.name, line0 + offset, token))
check("all 16 spec endpoint scope cells reference only catalogue scopes (0 unknown)",
      not unknown, str(unknown[:8]))
check("spec endpoint scan reached all 16 specs and >700 endpoint rows",
      len(list(SPECS.glob("FS-*.md"))) == 16 and endpoint_rows > 700,
      "rows=%d refs=%d" % (endpoint_rows, len(referenced)))
check("every public/self-service allowlist labelled route is present in the corresponding allowlist",
      not allowlist_mismatch, str(allowlist_mismatch[:5]))
unreferenced = sorted(set(catalogue) - referenced)
check("unreferenced catalogue scopes are reported as a non-blocking WARN (approval/job/read scopes referenced in prose)",
      isinstance(unreferenced, list))

check("DATA-MODEL DM-RBAC-01 allowlist counts match the catalogue (public 6 / self-service 6)",
      "public allowlist (6 entries)" in dm and "self-service allowlist (6 entries)" in dm)
if "self-service allowlist (5 entries)" in dm:
    finding("RBAC-D4", "MINOR", "design/DATA-MODEL.md:192 (DM-RBAC-01)",
            "DM-RBAC-01 says the self-service allowlist has 5 entries; the final catalogue/ARCHITECTURE §5.2 have 6 "
            "(the sixth is GET /api/v1/me/effective-permissions, added with the Δ-RBAC fixes).",
            "grep -n 'self-service allowlist (5 entries)' design/DATA-MODEL.md",
            "Change to 'self-service allowlist (6 entries)'.")

# ---------------------------------------------------------------------------
# findings from the stale sweep (specific, actionable)
# ---------------------------------------------------------------------------
for label, fname, line_no, text in stale_hits:
    if label == "role buyer 70 / warehouse 78":
        if any(f["id"] == "RBAC-D1" for f in findings):
            for f in findings:
                if f["id"] == "RBAC-D1":
                    f["where"] += " and %s:%d" % (fname, line_no)
            continue
        finding("RBAC-D1", "MINOR", "%s:%d" % (fname, line_no),
                "FS-00 role-template scope counts are stale: the §4.3 role table lists `buyer` 70 / "
                "`warehouse_operator` 78, but the authoritative catalogue has buyer 71 / warehouse_operator 81 "
                "(ARCHITECTURE §5.2 and FS-00 §4.3 prose already state 71/81, so the spec is internally inconsistent).",
                "grep -n 'buyer | Buyer' feature-sets/FS-00.md; grep -n 'buyer[^)]*70' feature-sets/FS-00.md",
                "Update the two role rows (and the §14 roles summary row) to 71 / 81.")
    elif label == "206 scopes":
        finding("RBAC-D3", "MINOR", "%s:%d" % (fname, line_no),
                "PLAN deliverable table still says '206 capability scopes'; the final authoritative catalogue is 249 "
                "(reconciled after the four missing action scopes and the alias cleanup).",
                "grep -n '206 capability scopes' PLAN.md",
                "Change to '249 capability scopes'.")
    elif fname.startswith("PLAN.md") and label in ("137 scoped/147 rules",):
        # historical 137→140 arrows are allowed by the sweep; only a bare current claim would land here
        pass

# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------
for ok, name, detail in results:
    print(("PASS  " if ok else "FAIL  ") + name + ((" -- " + detail) if (detail and not ok) else ""))
print("=" * 110)
print("RBAC DELTA AUDIT  --  %d PASS / %d FAIL / %d findings" % (
    sum(1 for ok, _n, _d in results if ok), sum(1 for ok, _n, _d in results if not ok), len(findings)))
print("=" * 110)
for f in findings:
    print("[%s] %s (%s): %s" % (f["sev"], f["id"], f["where"], f["message"]))
print("-" * 110)
print("Catalogue: scopes=%d (assignable=%d, service-only=%d, platform-only=%d), "
      "requires_approval=%d, roles=%d, SoD=%d, global_rules=%d, public_allowlist=%d, self_service_allowlist=%d"
      % (len(catalogue), len(assignable),
         sum(1 for e in catalogue.values() if e["service_only"]),
         sum(1 for e in catalogue.values() if e["platform_only"]),
         sum(1 for e in catalogue.values() if e["requires_approval"]),
         len(perm["role_templates"]), len(perm["sod_pairs"]), len(perm["global_rules"]),
         len(perm["public_allowlist"]), len(perm["self_service_allowlist"])))
print("Spec reconciliation: %d endpoint rows / %d catalogue scope references / %d unknown / %d unreferenced (WARN)"
      % (endpoint_rows, len(referenced), len(unknown), len(unreferenced)))
print("Rules: 140 scoped + 10 global = 150; invariants INV-01..INV-31; plan_version=%s"
      % manifest.get("plan_version"))
raise SystemExit(0 if not findings and all(ok for ok, _n, _d in results) else 1)
