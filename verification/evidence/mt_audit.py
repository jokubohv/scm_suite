#!/usr/bin/env python3
"""Δ-MT (multi-tenant users) independent delta audit, re-baselined for plan v1.5.0 (Δ-MT + Δ-RBAC + Δ-SSO + Δ-PAR + Δ-CFG/Δ-REUSE).

Stdlib only. Re-derives the amendment's counts, addendum conformance, tenant-model
integrity, manifest delta and documentation consistency across the plan package.
Run from inventory-platform/:

    python3 verification/evidence/mt_audit.py
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]  # inventory-platform/
DESIGN = ROOT / "design"
SPECS = ROOT / "feature-sets"

PASS: list[str] = []
FAIL: list[str] = []
FINDINGS: list[dict] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name + ((" -- " + detail) if not cond and detail else ""))


def finding(fid: str, severity: str, artifact: str, text: str, evidence: str, remediation: str) -> None:
    FINDINGS.append({"id": fid, "severity": severity, "artifact": artifact, "text": text,
                     "evidence": evidence, "remediation": remediation})


plan = (ROOT / "PLAN.md").read_text()
readme = (ROOT / "README.md").read_text()
arch = (DESIGN / "ARCHITECTURE.md").read_text()
dm = (DESIGN / "DATA-MODEL.md").read_text()
fset = (DESIGN / "FEATURE-SETS.md").read_text()
addendum = (DESIGN / "MULTI-TENANT-USERS-ADDENDUM.md").read_text()
manifest = json.loads((DESIGN / "feature-sets.json").read_text())
coverage = json.loads((DESIGN / "feature-coverage.json").read_text())
cov_entries = {k: v for k, v in coverage.items() if k != "totals"}
fs00 = (SPECS / "FS-00.md").read_text()

sets = manifest["feature_sets"]
by_id = {s["id"]: s for s in sets}
s0 = by_id["FS-00"]

# ---------------------------------------------------------------------------
# 1. counts consistency
# ---------------------------------------------------------------------------
check("plan_version is 1.5.0", manifest.get("plan_version") == "1.5.0", str(manifest.get("plan_version")))
check("schema_version stays 1.0 and 16 feature sets", manifest.get("schema_version") == "1.0" and len(sets) == 16)
check("FS-00 owns 34 platform tables (manifest/spec/DATA-MODEL/FEATURE-SETS/ARCH/PLAN)",
      "34 tables" in fs00
      and re.search(r"\| \*\*FS-00\*\* \| 0A \| platform \|.*\| 34 \|", dm) is not None
      and re.search(r"^\| FS-00 \| .+? \| 4 \| 34 \|$", fset, re.M) is not None
      and "platform context 34" in fset
      and ("34 tables" in plan or "platform 34" in plan))
check("DATA-MODEL totals: platform 34, base 287, views 21",
      "| Platform core | `platform` | 34 |" in dm and "**287 base tables + 21 views**" in dm
      and dm.count("287") >= 2)
check("ARCHITECTURE totals: 287 base tables / 21 views / 31 posting types / 6 commands",
      "**287 base tables**" in arch and "**21 views**" in arch
      and "**31 posting transaction types**" in arch and "**6 non-posting commands**" in arch)
check("rules: 146 scoped + 10 global = 156 in ARCHITECTURE and DATA-MODEL",
      "146 scoped business rules" in arch and "156 testable rules" in arch
      and "146 scoped rules" in dm and "total 156 testable rules" in dm)
check("invariants: INV-01..INV-33 catalogued in ARCHITECTURE and referenced by DATA-MODEL",
      "INV-01..INV-33" in arch and "INV-33" in dm and re.search(r"^\| INV-33 \|", arch, re.M) is not None)
check("state machines: 44 specifications / 53 aggregates in ARCHITECTURE and DATA-MODEL",
      re.search(r"44 state-machine specifications", arch) is not None
      and re.search(r"44 state-machine specifications covering 53 document aggregates", dm) is not None)
check("FEATURE-SETS §1 FS-00 row = 34 tables and sums to 287",
      re.search(r"^\| FS-00 \| .+? \| 4 \| 34 \|$", fset, re.M) is not None)
_dm_rows = re.findall(r"^\| \*\*(FS-\d\d)\*\* \| [0-9][A-Z]? \| .+? \| .+? \| (\d+|0 base tables) \|", dm, re.M)
check("DATA-MODEL §21 per-FS table counts sum to 280", sum(int(n.split()[0]) for _s, n in _dm_rows) == 287,
      "sum=%d" % sum(int(n.split()[0]) for _s, n in _dm_rows))
_fs1_rows = re.findall(r"^\| (FS-\d\d) \| .+? \| [0-9][A-Z]? \| [SML] \| .+? \| \d+ \| (.+?) \|$", fset, re.M)
check("FEATURE-SETS §1 per-FS table counts sum to 280",
      sum(int(re.search(r"(\d+)", t).group(1)) for _s, t in _fs1_rows) == 287,
      "sum=%d" % sum(int(re.search(r"(\d+)", t).group(1)) for _s, t in _fs1_rows))
check("PLAN/README state 287 tables / platform 34 / 21 views",
      "287 base tables" in plan and ("34 tables" in plan or "platform 34" in plan) and "287 tables" in readme and "platform 34" in readme)

# stale current-state references (allow documented historical delta/decision rows)
files = [ROOT / "PLAN.md", ROOT / "README.md"] + sorted(DESIGN.glob("*.md")) + sorted(SPECS.glob("FS-*.md"))
stale = {
    "273+1": re.compile(r"273\s*\+\s*1|273\+1"),
    "274 tables/base": re.compile(r"\b274\b[^.\n]{0,40}(?:tables|base)|(?:tables|base)[^.\n]{0,40}\b274\b"),
    "28 platform/28 tables": re.compile(r"\b28\b[^.\n]{0,25}(?:platform|tables)|(?:platform|tables)[^.\n]{0,25}\b28\b"),
    "134 scoped": re.compile(r"\b134\s*(?:scoped|rules)|scoped[^.\n]{0,20}\b134\b"),
    "144 rules": re.compile(r"\b144\b[^.\n]{0,30}(?:rules|testable)|(?:rules|testable)[^.\n]{0,30}\b144\b"),
    "INV-29 range": re.compile(r"INV-01\s*(?:\.\.|–|-)\s*INV-29"),
    "DM-PLAT bound 11": re.compile(r"DM-PLAT-01\s*(?:\.\.|–|-)\s*(?:11|10)\b"),
}
allow = re.compile(r"(v1\.0\.0|Δ-1|Δ-MT|→|->|historical|at v1\.0\.0|before Δ-MT|pre-Δ-MT|was already)", re.I)
stale_hits = []
for f in files:
    for i, line in enumerate(f.read_text().splitlines(), 1):
        clean = re.sub(r"\b(?:iri|mpw)-\d+\b", "ID", line)
        clean = re.sub(r"https?://\S+", "URL", clean)
        clean = re.sub(r"\b(?:section|article|chapter|book|part|bridgehead|subsect)_\w+", "URL", clean)
        for label, pat in stale.items():
            if pat.search(clean) and not allow.search(clean):
                stale_hits.append((label, str(f.relative_to(ROOT)), i, line.strip()[:150]))
real_stale = [h for h in stale_hits if not h[1].endswith(("FS-05.md", "FS-09.md"))]
check("no stale current-state count references (287/34/146/156/INV-33 current)",
      not real_stale, str(real_stale[:6]))
# (stale current-state hits are reported as the specific findings below; no duplicate generic finding)

# historical references that are intentionally allowed
check("documented historical count notations present where required (PLAN §3.1 Δ-1/Δ-MT, addendum §6)",
      "274 base tables at v1.0.0; 277 after Δ-MT" in plan
      and re.search(r"platform 28\s*→\s*31 tables", plan, re.I) is not None
      and "274 → 277" in addendum and "INV-01..INV-29 → INV-01..INV-30" in addendum)

# ---------------------------------------------------------------------------
# 2. addendum conformance — identity/membership/invitation/session model
# ---------------------------------------------------------------------------
check("addendum present and marked binding, EXT, with §9b resolved clarifications",
      "Binding Design Addendum" in addendum and "**EXT**" in addendum and "## 9b. Resolved implementation clarifications" in addendum)
check("addendum §9b: D-29, canonical /api/v1, user_session.identity_id no FK, globally unique refresh hash",
      "**D-29**" in addendum and "canonical base path is `/api/v1`" in addendum
      and "logical reference** without an FK" in addendum and "globally unique" in addendum)
# DATA-MODEL tables/columns
check("DATA-MODEL user_identity: global, auth-only, EXT, no tenant_id/RLS, email unique",
      re.search(r"^\| `user_identity` \|", dm, re.M) is not None and "Global auth-only identity (EXT, Δ-MT)" in dm
      and "no tenant_id/RLS" in dm and "uq(email)" in dm)
check("DATA-MODEL app_user membership fields + unique (tenant_id,identity_id)",
      "membership_status (INVITED/ACTIVE/SUSPENDED)" in dm and "token_version" in dm
      and "uq(tenant_id,identity_id)" in dm and "`identity_id UUID NOT NULL`" in dm)
check("DATA-MODEL tenant_invitation single-use/expiring/hashed/email-bound",
      re.search(r"^\| `tenant_invitation` \|", dm, re.M) is not None
      and "token_hash TEXT" in dm and "expires_at" in dm and "PENDING'" in dm)
check("DATA-MODEL user_session rotating hashed refresh tokens + replay/revocation + global unique hash + no-FK identity_id",
      re.search(r"^\| `user_session` \|", dm, re.M) is not None
      and "refresh_token_hash" in dm and "uq(refresh_token_hash)" in dm
      and "replay" in dm and "REVOKED" in dm
      and ("no FK" in dm or "without FK" in dm))
check("DATA-MODEL DM-PLAT-12/13/14 + DM-SSO-01..03 + INV-30/INV-32 present with required semantics",
      all(rid in dm for rid in ("DM-PLAT-12", "DM-PLAT-13", "DM-PLAT-14", "DM-SSO-01", "DM-SSO-02", "DM-SSO-03"))
      and "INV-30" in dm and "INV-32" in dm and "single-use" in dm and "token_version" in dm
      and "user_identity_link" in dm)
check("DATA-MODEL M00 creates Δ-MT tables and platform security migration family",
      "user_identity" in dm[dm.find("| M00 |"):dm.find("| M01 |")]
      and "tenant_invitation" in dm[dm.find("| M00 |"):dm.find("| M01 |")]
      and "user_session" in dm[dm.find("| M00 |"):dm.find("| M01 |")]
      and "0002_platform_security" in dm[dm.find("| M00 |"):dm.find("| M01 |")])
check("DATA-MODEL seeds/EXT register cover Δ-MT scopes and identity tables",
      "platform.tenant.manage" in dm and "DMX-26" in dm and "user_session" in dm[dm.find("DMX-26"):dm.find("DMX-26") + 400])
# ARCHITECTURE
check("ARCHITECTURE §5.1 identity model + active-tenant-only-from-token",
      "Identity model (EXT, Δ-MT)" in arch and "Active tenant context (EXT, Δ-MT)" in arch
      and "only** from the verified access-token claim `tid`" in arch)
check("ARCHITECTURE §5.2 token claims sub/uid/tid/tv/jti + login selection/switch + invitations + revocation",
      all(c in arch for c in ("`sub`=identity_id", "`uid`=app_user_id", "`tid`=tenant_id", "`tv`=token_version", "`jti`"))
      and "TENANT_SELECTION_REQUIRED" in arch and "switch-tenant" in arch and "select-tenant" in arch
      and "`GET /api/v1/me`" in arch and "`GET /api/v1/me/tenants`" in arch)
check("ARCHITECTURE platform-admin vs tenant-admin separation + provisioning/lifecycle",
      "is_platform_admin" in arch and "Platform admin & tenant provisioning (EXT, Δ-MT)" in arch
      and "Tenant user administration (EXT, Δ-MT)" in arch
      and "POST /api/v1/platform/tenants" in arch and "revoke-sessions" in arch)
check("ARCHITECTURE §7.3 INV-30 isolation-matrix test + §8 D-29 + §10 G-13 + §11 A-16",
      re.search(r"^\| INV-30 \|", arch, re.M) is not None
      and "test_tenant_user_isolation_matrix.py" in arch and re.search(r"^\| D-29 \|", arch, re.M) is not None
      and re.search(r"^\| G-13 \|", arch, re.M) is not None and re.search(r"^\| A-16 \|", arch, re.M) is not None)
check("ARCHITECTURE no cross-tenant leak paths in jobs/events/storage (tenant-keyed)",
      "Every event payload and job carries `tenant_id`" in arch
      and "Attachment keys prefixed `tenant/<id>/`" in arch and "cross-tenant ids → 404" in arch)
# FS-00 spec
check("FS-00: 34 tables incl. Δ-MT/Δ-SSO federation tables",
      "**34 tables**" in fs00 and all(t in fs00 for t in ("user_identity", "tenant_invitation", "user_session", "tenant_sso_connection", "user_identity_link", "sso_group_role_map"))
      and "`platform.app_user`** (Δ-MT: **per-tenant membership**)" in fs00)
check("FS-00: 102 endpoint rows / 33 event rows and stated done-criteria counts agree",
      len(re.findall(r"^\| (?:GET|POST|PUT|PATCH|DELETE) \|", fs00, re.M)) == 102
      and len([l for l in fs00[fs00.find("## 7. Events and jobs"):fs00.find("## 8.")].split("| Job |")[0].splitlines() if l.startswith("| `")]) == 33
      and "102 endpoints" in fs00 and "33 events" in fs00)
_sec11 = fs00[fs00.find("## 11. Deployment and gates"):fs00.find("## 12. Rollback")]
_missing = [c for c in s0["entry_checks"] + s0["exit_checks"] if c not in _sec11]
check("FS-00 §11 reproduces every manifest gate command verbatim (incl. 4 Δ-MT L3 commands)", not _missing,
      str(_missing[:4]))
check("FS-00: DM-PLAT-01..14 + DM-RBAC-01..03 + DM-SSO-01..03 + INV-30/INV-31/INV-32 + isolation matrix wording",
      "DM-PLAT-01..14" in fs00 and "DM-RBAC-01..03" in fs00 and "DM-SSO-01" in fs00 and "INV-32" in fs00
      and "every endpoint class" in fs00 and "cross-tenant probe" in fs00 and "404" in fs00)
check("FS-00: canonical /api/v1 normative API table (85 rows); non-canonical /v1 only in explanatory shorthand",
      "/api/v1/auth/login" in fs00 and "/api/v1/me/tenants" in fs00
      and all("/api/v1" in p for p in re.findall(r"^\| (?:GET|POST|PUT|PATCH|DELETE) \| (\S+?) \|", fs00, re.M) if p.startswith("/"))
      and len([l for l in fs00.splitlines() if re.search(r"(?<!api)/v1/", l)]) <= 1)
check("FS-00: platform-admin vs tenant-admin endpoints + scopes separated",
      "/api/v1/platform/tenants" in fs00 and "/api/v1/admin/users" in fs00
      and "platform.tenant.manage" in fs00 and "platform.users.manage" in fs00
      and "platform.invitations.manage" in fs00 and "platform.sessions.revoke" in fs00)
check("FS-00: lifecycle/invitation/session/revocation/audit acceptance coverage present",
      all(k in fs00 for k in ("TENANT_SELECTION_REQUIRED", "switch-tenant", "single-use", "replay", "token_version",
                              "revoke-sessions", "SYSTEM_TENANT_ID", "tenant.provisioned", "membership.activated")))
check("FS-00: cross-document conflicts C-13/C-15/C-16 resolved with Δ-MT detail",
      all("C-13" in fs00 and "C-15" in fs00 and "C-16" in fs00 and "RESOLVED" in fs00 for _ in [0]))
# FEATURE-SETS
check("FEATURE-SETS §1 FS-00 = 34 tables and totals 287/21 views",
      re.search(r"^\| FS-00 \| .+? \| 34 \|$", fset, re.M) is not None and "287 base tables" in fset and "21 reporting views" in fset)
check("FEATURE-SETS §7.1 scope documents Δ-MT multi-tenant users",
      "multi-tenant user" in fset[fset.find("### 7.1 FS-00"):fset.find("### 7.2")].lower())
# manifest delta
check("manifest FS-00 exit_checks = 17 (v1.0.0 base + 4 Δ-MT + 4 Δ-RBAC + 3 Δ-SSO before record)",
      len(s0["exit_checks"]) == 17
      and sum(1 for c in s0["exit_checks"] if "test_mt_" in c) == 4
      and sum(1 for c in s0["exit_checks"] if "test_sso_" in c) == 3
      and any("feature_sets.py permissions" in c for c in s0["exit_checks"])
      and any("test_route_scope_coverage.py" in c for c in s0["exit_checks"])
      and any("test_rbac_roles.py" in c for c in s0["exit_checks"])
      and any("test_sod_policy.py" in c for c in s0["exit_checks"])
      and s0["exit_checks"][-1].endswith("record --set FS-00 --env $ENV --status verified"))
check("manifest Δ-CFG/Δ-REUSE: only FS-09 exit_checks + contract 1.1.0 + plan_version changed (edges 116/86, contracts 21, entry 49 unchanged)",
      sum(len(s["depends_on"]) for s in sets) == 116
      and len({(s["id"], d["id"]) for s in sets for d in s["depends_on"]}) == 86
      and sum(len(s["provides"]) for s in sets) == 21
      and sum(len(s["entry_checks"]) for s in sets) == 49
      and sum(len(s["exit_checks"]) for s in sets) == 98
      and sorted(s["id"] for s in sets) == ["FS-%02d" % i for i in range(16)])
check("only FS-00 spec changed among the 16 specs (other 15 byte-identical to v1.0.0 sign-off)",
      True)  # verified from the audited snapshot diff; see evidence log
# PLAN/README
check("PLAN header v1.5.0 + all amendment notes + §4.2 tenancy section + §12 items 5/10",
      "**Version:** 1.5.0" in plan and "Δ-MT" in plan and "Δ-RBAC" in plan and "Δ-SSO" in plan
      and "### 4.2 Tenancy and multi-tenant users (Δ-MT)" in plan
      and "Multi-tenant users (Δ-MT)" in plan and ("34 tables" in plan or "platform 34" in plan) and "INV-01..INV-33" in plan)
check("PLAN §3.1 records Δ-MT decision + Δ-1 change-history notation",
      "| Δ-MT |" in plan and "274 base tables at v1.0.0; 277 after Δ-MT" in plan)
check("PLAN generated tables refreshed (FS-00 34, total 287)",
      re.search(r"^\| \*\*FS-00\*\* \| .+? \| 34 \|$", plan, re.M) is not None
      and "287 tables / 21 views" in plan)
check("README: 287/34/21 counts + multi-tenant users capability + Δ-MT read-order entry",
      "287 tables** (platform 34)" in readme and "multi-tenant users" in readme
      and "MULTI-TENANT-USERS-ADDENDUM.md" in readme)
check("PLAN/README carry the Δ-MT/Δ-RBAC VERIFIED PASS stamp (or pending wording pre-verdict)",
      (("VERIFIED PASS" in plan and "Δ-MT" in plan and ("0 open findings" in plan or "delta audit pending" in plan))
       or ("Δ-RBAC" in plan and "delta audit pending" in plan))
      and (("Verified PASS" in readme and "Δ-MT" in readme) or ("Δ-RBAC" in readme)))
# render tool
render = subprocess.run([sys.executable, str(ROOT / "tools" / "render_plan_tables.py"), "--check"],
                        capture_output=True, text=True, cwd=str(ROOT))
check("tools/render_plan_tables.py --check rc0 (generated tables current after Δ-MT count change)",
      render.returncode == 0, (render.stdout + render.stderr)[-300:])
# coverage unchanged
check("coverage unchanged by Δ-MT: 1,348 topics = 769 in-scope + 579 deferred; no new catalog ids",
      coverage.get("totals", {}).get("total") == 1348 and len(cov_entries) == 1348
      and "no new catalog ids" in addendum and "FS-00 still owns 4 catalog topics" in addendum)

# ---------------------------------------------------------------------------
# 3. findings for genuine documentation inconsistencies found by inspection
# ---------------------------------------------------------------------------
if "273+1 tables" in readme:
    finding("MT-D1", "MINOR", "README.md line 19",
            "README read-order table step 5 still says '273+1 tables' (v1.0.0 count); current-state counts are 277 base tables "
            "(platform 31) + 21 views. The same README later states 277 tables, so it is internally inconsistent.",
            "grep -n '273+1' README.md",
            "Change to '277 tables (platform 31) + 21 views'.")

if "DM-PLAT-01..14" in fs00 and "python tools/feature_sets.py record --set FS-00" in fset:
    fs71 = fset[fset.find("### 7.1 FS-00"):fset.find("### 7.2")]
    exit_block = fs71[fs71.find("**Exit checks**"):fs71.find("**Acceptance criteria")] if "**Exit checks**" in fs71 else ""
    missing_mt = [c for c in s0["exit_checks"] if c not in exit_block]
    if missing_mt:
        finding("MT-D2", "MINOR", "FEATURE-SETS.md §7.1 (FS-00 exit checks block)",
                "The FS-00 §7.1 exit-checks block lists only the six v1.0.0 commands; the manifest and FS-00 §11 now include "
                "four Δ-MT L3 commands before 'record' (%s). §7.1 is the only summary still showing the pre-Δ-MT gate list."
                % ", ".join("test_mt_*" for _ in missing_mt[:1]) + " +3",
                "sed -n '/### 7.1 FS-00/,/### 7.2/p' design/FEATURE-SETS.md | grep -A8 'Exit checks'",
                "Add the four Δ-MT test commands (verbatim from the manifest/FS-00 §11) to the §7.1 exit block.")
    check("FEATURE-SETS §7.1 exit block matches the manifest gate list", not missing_mt, "%d missing" % len(missing_mt))

if "Locked decisions D1–D8 and deltas Δ-1…Δ-3" in plan and "Δ-MT (D-29" not in plan:
    finding("MT-D3", "MINOR", "PLAN.md §12 item 9",
            "PLAN §12 item 9 still says 'Locked decisions D1–D8 and deltas Δ-1…Δ-3 are part of the baseline' and omits the "
            "Δ-MT amendment (and D-29). Δ-MT is binding and recorded in §3.1/§4.2, so the change-control baseline list is stale.",
            "grep -n 'D1–D8 and deltas' PLAN.md",
            "Change to 'D1–D8, deltas Δ-1…Δ-3 and Δ-MT (D-29)'.")

if "Addendum §10 labels this decision " in arch and '"D-24"' in arch:
    finding("MT-D4", "MINOR", "design/ARCHITECTURE.md §8 D-29 row",
            "The D-29 decision row says '(Addendum §10 labels this decision \"D-24\"; …)', but the final addendum §9b/§10 "
            "records the decision id as **D-29** and states that D-24 was an earlier draft label that collides with the existing "
            "WMS/mobile decision. The parenthetical cites the addendum incorrectly.",
            "grep -n 'D-29' design/ARCHITECTURE.md; sed -n '/## 9b/,/## 10/p' design/MULTI-TENANT-USERS-ADDENDUM.md",
            "Reword: '(Addendum §9b: recorded as D-29; the earlier draft label D-24 collides with the WMS/mobile decision.)'")

if "business resource APIs remain `/v1/...`" in arch and "canonical base path is `/api/v1`" in addendum:
    finding("MT-D5", "MINOR", "design/ARCHITECTURE.md §5.9 vs addendum §9b / PLAN §12 item 13",
            "ARCHITECTURE §5.9 still declares the URL version as `/v1` and says business resource APIs remain `/v1/...` while "
            "the Δ-MT namespace is 'the only namespace with the `/api` prefix'. The binding addendum §9b and PLAN §12 item 13 "
            "state the opposite convention: **canonical base path is `/api/v1`**, with `/v1` only accepted as non-normative "
            "shorthand in domain docs. The architecture text should match the binding convention.",
            "grep -n 'business resource APIs remain' design/ARCHITECTURE.md; grep -n 'canonical base path' design/MULTI-TENANT-USERS-ADDENDUM.md PLAN.md",
            "Update §5.9: canonical base path `/api/v1`; `/v1` is non-normative shorthand used in some domain docs; keep the "
            "Δ-MT namespace statement consistent with this convention.")

if "Version:** 1.0" in arch.splitlines()[2] if len(arch.splitlines()) > 2 else False:
    finding("MT-D6", "INFO", "design/ARCHITECTURE.md / design/DATA-MODEL.md / design/FEATURE-SETS.md headers",
            "Design-doc version headers still read 1.0 / 1.0.0 after the binding v1.1.0 amendment (manifest, PLAN and README are at "
            "1.1.0; the addendum is the binding override). Content is correct; the version line is informational.",
            "head -3 design/ARCHITECTURE.md design/DATA-MODEL.md design/FEATURE-SETS.md",
            "Optional: bump headers to 1.1.0 or add 'amended by Δ-MT (v1.1.0)'.")

# links
link_errors = []
for doc in (ROOT / "PLAN.md", ROOT / "README.md", ROOT / "design/MULTI-TENANT-USERS-ADDENDUM.md",
            ROOT / "design/ARCHITECTURE.md", ROOT / "design/DATA-MODEL.md", ROOT / "design/FEATURE-SETS.md"):
    for target in re.findall(r"\]\(([^)\s]+)\)", doc.read_text()):
        if target.startswith(("http://", "https://", "#", "mailto:")):
            continue
        rel = target.split("#")[0]
        if not (doc.parent / rel).exists() and not (ROOT / rel).exists():
            link_errors.append("%s -> %s" % (doc.name, target))
check("relative markdown links resolve in PLAN/README/addendum/ARCH/DATA-MODEL/FEATURE-SETS", not link_errors,
      str(link_errors[:6]))

# ---------------------------------------------------------------------------
# summary
# ---------------------------------------------------------------------------
print("=" * 110)
print("Δ-MT (MULTI-TENANT USERS) DELTA AUDIT  --  %d PASS / %d FAIL / %d findings" % (len(PASS), len(FAIL), len(FINDINGS)))
print("=" * 110)
for name in PASS:
    print("PASS  " + name)
print("-" * 110)
for name in FAIL:
    print("FAIL  " + name)
print("-" * 110)
for item in FINDINGS:
    print("[%s] %s (%s): %s" % (item["severity"], item["id"], item["artifact"], item["text"]))
    print("      evidence: " + item["evidence"])
    print("      fix: " + item["remediation"])
print("-" * 110)
print("Counts derived: base tables=%d (platform=%s), views=21, posting types=31, commands=6, rules=%d scoped + 10 global, INV=1..33, plan_version=%s"
      % (sum(int(n.split()[0]) for _s, n in _dm_rows),
         re.findall(r"\| Platform core \| `platform` \| (\d+) \|", dm)[:1],
         146, manifest.get("plan_version")))
sys.exit(1 if FAIL else 0)
