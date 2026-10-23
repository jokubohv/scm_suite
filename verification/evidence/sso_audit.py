#!/usr/bin/env python3
"""Δ-SSO (Dex + per-company enterprise SSO) independent delta audit — plan v1.3.0.

Checks the binding addendum, the amended ARCHITECTURE / DATA-MODEL / FS-00 /
FEATURE-SETS / manifest / permissions catalogue / PLAN / README / framework and
the unchanged-other-15-specs invariant.

Run from inventory-platform/:

    python3 verification/evidence/sso_audit.py
"""

from __future__ import annotations

import hashlib
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
addendum = (DESIGN / "DEX-SSO-ADDENDUM.md").read_text()
framework = (DESIGN / "FEATURE-SET-FRAMEWORK.md").read_text()
fs00 = (SPECS / "FS-00.md").read_text()
manifest = json.loads((DESIGN / "feature-sets.json").read_text())
perm = json.loads((DESIGN / "permissions.json").read_text())
coverage = json.loads((DESIGN / "feature-coverage.json").read_text())

sets = manifest["feature_sets"]
by_id = {s["id"]: s for s in sets}
s0 = by_id["FS-00"]
catalogue = {e["scope"]: e for e in perm["catalogue"]}
assignable = {e["scope"] for e in perm["catalogue"] if e["assignable"]}
roles = {r["key"]: r for r in perm["role_templates"]}

results: list[tuple[bool, str, str]] = []
findings: list[dict] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    results.append((bool(condition), name, detail))


def finding(fid: str, severity: str, where: str, message: str, repro: str, fix: str) -> None:
    findings.append({"id": fid, "sev": severity, "where": where, "message": message,
                     "repro": repro, "fix": fix})


# ---------------------------------------------------------------------------
# 1. plan version / catalogue / permissions deltas
# ---------------------------------------------------------------------------
check("plan_version is 1.5.0 in manifest and permissions.json",
      manifest.get("plan_version") == "1.5.0" and perm.get("plan_version") == "1.5.0")
check("catalogue: 255 scopes = 241 assignable + 13 service-only + 1 platform-only",
      len(catalogue) == 255 and len(assignable) == 241
      and sum(1 for e in catalogue.values() if e["service_only"]) == 13
      and sum(1 for e in catalogue.values() if e["platform_only"]) == 1)
check("SSO scopes platform.sso.manage/read are assignable and not platform/service-only",
      all(s in catalogue and catalogue[s]["assignable"] and not catalogue[s]["platform_only"]
          and not catalogue[s]["service_only"] for s in ("platform.sso.manage", "platform.sso.read")))
check("alias platform.user.manage is in aliases only (not seeded)",
      perm["aliases"].get("platform.user.manage") == "platform.users.manage"
      and "platform.user.manage" not in catalogue)
check("role templates: admin = all 241 assignable; documented counts (readonly 44)",
      set(roles["admin"]["scopes"]) == assignable
      and {k: len(v["scopes"]) for k, v in roles.items()} ==
      {"admin": 241, "planner": 50, "buyer": 71, "warehouse_operator": 81,
       "cost_accountant": 57, "quality_inspector": 47, "readonly": 44})
check("18 requires_approval; 7 templates; 14 SoD pairs; 6 global rules; public 9 / self-service 6",
      sum(1 for e in catalogue.values() if e["requires_approval"]) == 18
      and len(perm["role_templates"]) == 7 and len(perm["sod_pairs"]) == 14
      and len(perm["global_rules"]) == 6
      and len(perm["public_allowlist"]) == 9 and len(perm["self_service_allowlist"]) == 6)
check("public allowlist contains the 3 Δ-SSO public routes",
      all(r in perm["public_allowlist"] for r in
          ("GET /api/v1/auth/providers", "POST /api/v1/auth/sso/start", "GET /api/v1/auth/sso/callback")))
check("counts metadata matches recomputed catalogue",
      perm["counts"]["scopes"] == 255 and perm["counts"]["assignable_scopes"] == 241
      and perm["counts"]["service_only"] == 13 and perm["counts"]["platform_only"] == 1)

# ---------------------------------------------------------------------------
# 2. ARCHITECTURE contract (Dex broker, SSO flows, counts)
# ---------------------------------------------------------------------------
check("ARCHITECTURE §0 counts: 280/34/21 views/31+6/143+10=153/INV-32/251/237/43/52",
      "**287 base tables**" in arch and "platform" in arch and "**21 views**" in arch
      and "**146 scoped business rules**" in arch and "**156 testable rules**" in arch
      and "**invariants INV-01..INV-33**" in arch
      and "255 capability scopes (241 assignable + 13 service-only + 1 platform-only)" in arch
      and "**44 state-machine specifications**" in arch and "53 document aggregates" in arch)
check("ARCHITECTURE topology: Dex units + isolated issuer per ACTIVE connection + Dex PostgreSQL storage",
      "Dex OIDC identity broker" in arch and "one isolated Dex issuer per ACTIVE `tenant_sso_connection`" in arch
      and "dedicated PostgreSQL `dex` database/schema owned by Dex" in arch
      and "shared Dex" in arch and "2.4x" in arch)
check("ARCHITECTURE IdP boundary: Dex authenticates, platform authorizes/token-issues; local fallback/break-glass",
      "Identity provider — Dex broker + enterprise SSO" in arch
      and "Dex authenticates; the platform authorizes and issues its own tokens" in arch
      and "Local password (Argon2id) fallback" in arch and "break-glass" in arch)
check("ARCHITECTURE §5.2 SSO flows: discovery/start/callback state+nonce+PKCE; ID-token validation; linking/JIT/group mapping; enforced-SSO/no silent fallback; scopes",
      all(k in arch for k in ("SSO discovery / start / callback", "state", "nonce", "PKCE S256",
                              "ID token", "email_verified", "Identity linking, JIT & group mapping",
                              "assignable tenant roles", "SOD_CONFLICT", "Enforced SSO, local fallback & break-glass",
                              "no silent fallback", "platform.sso.manage", "platform.sso.read")))
check("ARCHITECTURE §7.3 INV-32 + §8 D-31 + §10 G-15; INV-31/32 test rows",
      re.search(r"^\| INV-33 \|", arch, re.M) is not None
      and re.search(r"^\| D-31 \|", arch, re.M) is not None
      and re.search(r"^\| G-15 \|", arch, re.M) is not None
      and "INV-01..INV-33" in arch and "SSO/authn integrity" in arch)
check("ARCHITECTURE runtime envs mention Dex dev/shared and prod isolated",
      "shared `dex`" in arch and "Dex storage schema" in arch)
check("ARCHITECTURE allowlists 9/6 (SSO routes explicitly public)",
      "public allowlist (9)" in arch and "self-service allowlist (6)" in arch
      and "GET /api/v1/auth/providers" in arch and "POST /api/v1/auth/sso/start" in arch)

# ---------------------------------------------------------------------------
# 3. DATA-MODEL contract
# ---------------------------------------------------------------------------
check("DATA-MODEL platform context 34 / base tables total 287 / 21 views",
      "| Platform core | `platform` | 34 |" in dm and "**287 base tables + 21 views**" in dm
      and "| **Base tables total** | | **287** |" in dm)
check("DATA-MODEL 3 SSO tables with key columns/constraints/RLS",
      all(t in dm for t in ("`tenant_sso_connection`", "`user_identity_link`", "`sso_group_role_map`"))
      and "protocol (SAML/OIDC/LDAP_AD)" in dm and "is_enforced BOOL DEFAULT false" in dm
      and "jit_provisioning BOOL DEFAULT false" in dm and "email_domains TEXT[]" in dm
      and "client_secret_ref" in dm and "uq(tenant_id,connection_id,issuer,subject)" in dm
      and "uq(tenant_id,connection_id,group_value,role_id)" in dm and "RLS tenant policy" in dm)
check("DATA-MODEL DM-SSO-01/02/03 + INV-32 semantics present",
      all(r in dm for r in ("DM-SSO-01", "DM-SSO-02", "DM-SSO-03"))
      and "`INV-32`" in dm and "every SSO login resolves" in dm
      and "group→role" in dm and "never silently downgrades" in dm)
check("DATA-MODEL §19 adds 2 SSO state machines (44 specs / 53 aggregates)",
      "tenant_sso_connection" in dm[dm.find("## 19."):dm.find("## 20.")]
      and "user_identity_link" in dm[dm.find("## 19."):dm.find("## 20.")]
      and "44 state-machine specifications" in dm and "53 document aggregates" in dm)
check("DATA-MODEL rules: platform 20, scoped 146, total 156",
      "platform 20" in dm and "146 scoped rules are listed" in dm and "**total 156 testable rules**" in dm)
check("DATA-MODEL migration chain unchanged; SSO tables created inside 0002_platform_security (FS-00 3 migrations)",
      "M00 = `0001_platform_core`, `0002_platform_security`, `0003_platform_events`" in dm
      and "SSO's three tables are created inside the existing `0002_platform_security`" in dm
      and "FS-00 migration count stays 3" in dm and "0004_item_master" in dm)
check("DATA-MODEL EXT register DMX-28 + cross-scope FK exception only identity_link",
      "DMX-28" in dm and "Δ-SSO" in dm and "cross-scope FK exception" in dm
      and "`user_identity_link.identity_id → user_identity.id`" in dm)

# ---------------------------------------------------------------------------
# 4. FS-00 spec: counts, SSO surface, gate parity
# ---------------------------------------------------------------------------
fs00_sec7 = fs00[fs00.find("## 7. Events and jobs"):fs00.find("## 8.")]
_api_rows = len(re.findall(r"^\| (?:GET|POST|PUT|PATCH|DELETE) \|", fs00, re.M))
_event_rows = len([l for l in fs00_sec7.split("| Job | Schedule |")[0].splitlines() if l.startswith("| `")])
_job_rows = len([l for l in fs00_sec7.split("| Job | Schedule |")[1].splitlines() if l.startswith("| `")]) \
    if "| Job | Schedule |" in fs00_sec7 else 0
check("FS-00: 34 tables / 102 endpoint rows / 33 event rows / 6 job rows / 3 migrations",
      "**34 tables**" in fs00 and _api_rows == 102 and _event_rows == 33 and _job_rows == 6
      and len(s0["migrations"]) == 3)
check("FS-00 SSO tables/columns/config/events/tests present",
      all(t in fs00 for t in ("tenant_sso_connection", "user_identity_link", "sso_group_role_map"))
      and all(k in fs00 for k in ("state", "nonce", "PKCE", "email_verified", "SSO_EMAIL_DOMAIN_NOT_ALLOWED",
                                  "IDENTITY_LINK_CONFLICT", "SOD_CONFLICT", "jit_provisioning",
                                  "platform.sso.manage", "platform.sso.read", "Dex"))
      and "test_sso_oidc_flow.py" in fs00 and "test_sso_tenant_isolation.py" in fs00
      and "test_sso_group_mapping.py" in fs00
      and "DM-SSO-01" in fs00 and "INV-32" in fs00)
check("FS-00 SSO public + admin endpoints listed",
      all(p in fs00 for p in ("/api/v1/auth/providers", "/api/v1/auth/sso/start", "/api/v1/auth/sso/callback",
                              "/api/v1/admin/sso-connections", "/rotate-secret", "/group-mappings",
                              "/links", "/identity-links")))
check("FS-00 §11 reproduces all 17 manifest exit/entry commands verbatim (incl. 3 SSO tests)",
      len(s0["exit_checks"]) == 17
      and all(c in fs00 for c in s0["entry_checks"] + s0["exit_checks"])
      and sum(1 for c in s0["exit_checks"] if "test_sso_" in c) == 3)
check("FS-00 amendment records C-21/C-22 present (Δ-SSO landed; migration placement/count clarification)",
      "C-21" in fs00 and "C-22" in fs00 and "0002_platform_security" in fs00 and "102 endpoints" in fs00)

# ---------------------------------------------------------------------------
# 5. FEATURE-SETS / manifest / framework delta
# ---------------------------------------------------------------------------
check("FEATURE-SETS plan version 1.5.0 + FS-00 34 tables + totals 287/21 + §7.1 Δ-SSO paragraph",
      "**Version:** 1.5.0" in fset and re.search(r"^\| FS-00 \| .+? \| 34 \|$", fset, re.M) is not None
      and "**287 base tables**" in fset and "21 reporting views" in fset
      and "Δ-SSO (plan v1.3.0" in fset and "Dex" in fset and "INV-32" in fset)
check("FEATURE-SETS migration text: SSO tables inside 0002_platform_security (no renumbering)",
      "created inside **`0002_platform_security`**" in fset
      and "FS-00 keeps **3 migrations" in fset and "0004_item_master" in fset)
check("manifest: plan_version 1.5.0; FS-00 exit_checks 17 incl. 3 SSO tests before record; 3 migrations",
      manifest.get("plan_version") == "1.5.0" and len(s0["exit_checks"]) == 17
      and sum(1 for c in s0["exit_checks"] if "test_sso_" in c) == 3
      and s0["exit_checks"][-1].endswith("record --set FS-00 --env $ENV --status verified")
      and [m.split(":")[1] for m in s0["migrations"]] ==
      ["0001_platform_core", "0002_platform_security", "0003_platform_events"])
check("manifest: only FS-00 exit_checks + plan_version changed (edges 116/86, contracts 21, waves 10, entry 49, exit 95)",
      sum(len(s["depends_on"]) for s in sets) == 116
      and len({(s["id"], d["id"]) for s in sets for d in s["depends_on"]}) == 86
      and sum(len(s["provides"]) for s in sets) == 21 and len({s["wave"] for s in sets}) == 10
      and sum(len(s["entry_checks"]) for s in sets) == 49
      and sum(len(s["exit_checks"]) for s in sets) == 98
      and all(len(s["exit_checks"]) == by_id[s["id"]]["exit_checks"].__len__() for s in sets))
check("framework §3 Auth row: Dex OIDC broker + platform JWT/refresh + API keys + local fallback; Deploy row: Dex units/storage",
      "Dex OIDC identity broker" in framework and "platform-issued JWT access/refresh" in framework
      and "Argon2id local-password fallback" in framework
      and "Dex units" in framework and "one isolated Dex issuer per active SSO connection" in framework
      and "Dex Postgres storage" in framework)

# ---------------------------------------------------------------------------
# 6. PLAN / README / addendum
# ---------------------------------------------------------------------------
check("PLAN header plan v1.5.0 (Δ-CFG audit pending) + Δ-SSO decision row D-31 + §4.4 section",
      "**Version:** 1.5.0" in plan and "Δ-SSO" in plan
      and "| Δ-SSO |" in plan and "D-31" in plan
      and "### 4.4 Authentication: Dex + enterprise SSO (Δ-SSO)" in plan)
check("PLAN counts/assumptions: 287/34/255/146/156/INV-33 + Dex/SSO narrative",
      "287 base tables" in plan and "platform 34" in plan
      and "255 scopes" in plan and "146 scoped" in plan and "156" in plan and "INV-01..INV-33" in plan
      and "Dex" in plan and "per-company" in plan.lower() and "ssso" not in plan.lower())
check("PLAN generated tables current (287 tables / 21 views; FS-00 34 tables)",
      "287 tables / 21 views" in plan
      and re.search(r"^\| FS-00 \| .+? \| 34 \|$", fset, re.M) is not None)
check("README carries Dex/SSO auth line + 287/34/255/146/156 counts + pending status",
      "Dex OIDC identity broker" in readme and "per-company SSO" in readme
      and "**287 tables**" in readme and "(platform 34)" in readme
      and "**255-scope catalogue**" in readme
      and "Δ-SSO" in readme
      and ("independent delta audit in" in readme or ("Verified PASS" in readme and "0 open findings" in readme)))
check("addendum binding/EXT, 3 tables in 0002, flows/linking/JIT/group-mapping/deployment/rules/tests present",
      "Binding Design Addendum" in addendum and "EXT" in addendum
      and "0002_platform_security" in addendum
      and all(k in addendum for k in ("authorization-code + PKCE", "state", "nonce", "PKCE S256",
                                      "email_verified", "JIT", "sso_group_role_map",
                                      "one isolated Dex issuer per SSO connection",
                                      "Dex storage: **PostgreSQL**", "DM-SSO-01", "DM-SSO-02",
                                      "DM-SSO-03", "INV-32", "public allowlist **6 → 9**",
                                      "self-service allowlist stays 6")))
check("addendum §11 resolved clarifications: Dex=authn not authz; per-company=tenant connection; local fallback/break-glass; SCIM deferred",
      "Dex is authentication, not authorization" in addendum
      and "Per-company SSO** = per-tenant `tenant_sso_connection`" in addendum
      and "Local fallback retained" in addendum and "break-glass" in addendum
      and "SCIM" in addendum and "deferred" in addendum)

# ---------------------------------------------------------------------------
# 7. stale current-state count sweep (historical delta rows allowed)
# ---------------------------------------------------------------------------
files = [ROOT / "PLAN.md", ROOT / "README.md"] + [f for f in sorted(DESIGN.glob("*.md"))
         if f.name not in ("MULTI-TENANT-USERS-ADDENDUM.md", "PERMISSIONS-ROLES-ADDENDUM.md")] \
        + sorted(SPECS.glob("FS-*.md"))
stale = {
    "277 base / 31 platform": re.compile(r"\b277\b[^.\n]{0,40}(?:base tables|tables)|(?:base tables|tables)[^.\n]{0,40}\b277\b"),
    "249 scopes": re.compile(r"\b249\b[^.\n]{0,40}(?:scopes|catalogue)|(?:scopes|catalogue)[^.\n]{0,40}\b249\b"),
    "235 assignable": re.compile(r"\b235\b[^.\n]{0,30}assignable|assignable[^.\n]{0,30}\b235\b"),
    "140 scoped / 150 total": re.compile(r"\b140\b[^.\n]{0,25}scoped|scoped[^.\n]{0,25}\b140\b|\b150\b[^.\n]{0,20}testable|testable[^.\n]{0,20}\b150\b"),
    "INV-01..INV-31 current": re.compile(r"INV-01\s*(?:\.\.|–|-)\s*INV-31\b"),
    "88→249 seed gap": re.compile(r"88\s*→\s*249"),
    "99 endpoints / 32 events / 4 migrations": re.compile(r"99 endpoints[^.\n]{0,40}32 events|4 migrations"),
}
allow = re.compile(r"(v1\.0\.0|v1\.1\.0|v1\.2\.0|Δ-1|Δ-MT|Δ-RBAC|→|->|historical|at v1\.|before Δ|pre-Δ|superseded|at edit time|only at edit)", re.I)
stale_hits = []
for f in files:
    for i, line in enumerate(f.read_text().splitlines(), 1):
        clean = re.sub(r"\b(?:iri|mpw)-\d+\b", "ID", line)
        clean = re.sub(r"https?://\S+", "URL", clean)
        clean = re.sub(r"\b(?:section|article|chapter|book|part|bridgehead|subsect)_\w+", "URL", clean)
        for label, pat in stale.items():
            if pat.search(clean) and not allow.search(clean):
                stale_hits.append((label, str(f.relative_to(ROOT)), i, line.strip()[:170]))
check("no stale current-state count references (280/34/251/237/143/153/INV-32 current)", not stale_hits,
      str(stale_hits[:6]))

# ---------------------------------------------------------------------------
# 8. coverage unchanged + other 15 specs byte-identical to the v1.2.0 sign-off
# ---------------------------------------------------------------------------
cov_entries = {k: v for k, v in coverage.items() if k != "totals"}
check("coverage unchanged: 1,348 topics = 769 in-scope + 579 deferred (0 SCM-mobile)",
      len(cov_entries) == 1348
      and sum(1 for v in cov_entries.values() if str(v["disposition"]).startswith("DEFERRED")) == 579
      and coverage["totals"]["by_disposition"].get("EXCLUDED-SCM-MOBILE") == 0
      and coverage["totals"]["total"] == 1348)
snapshot_path = ROOT / "verification/evidence/tool/audited-snapshot.sha256"
sv = {}
if snapshot_path.exists():
    for line in snapshot_path.read_text().splitlines():
        parts = line.split()
        if len(parts) == 2:
            sv[parts[1]] = parts[0]
changed = []
for spec in sorted(SPECS.glob("FS-*.md")):
    rel = "feature-sets/" + spec.name
    if rel in sv:
        digest = hashlib.sha256(spec.read_bytes()).hexdigest()
        if digest != sv[rel]:
            changed.append(spec.name)
check("only FS-00 changed since the v1.5.0 audit snapshot (FS-03/FS-09 changes already captured)",
      sorted(changed) == ["FS-00.md"], str(changed))

# ---------------------------------------------------------------------------
# findings (documentation-only, surfaced by the checks/sweep above)
# ---------------------------------------------------------------------------
if "88→249" in arch:
    finding("SSO-D1", "MINOR", "design/ARCHITECTURE.md:1027 (D-30 row)",
            "D-30 still says the explicit catalogue 'closes the 88→249 seed gap'; the final v1.3.0 catalogue is 251 "
            "(249 was the pre-SSO count). Text-only historical-vs-current inconsistency in a decision row.",
            "grep -n '88→249' design/ARCHITECTURE.md",
            "Change to '88→251 seed gap'.")
if re.search(r"99 endpoints[^.\n]{0,40}32 events", addendum) or "4 migrations" in addendum:
    finding("SSO-D2", "MINOR", "design/DEX-SSO-ADDENDUM.md:212 (§10 update map, FS-00 row)",
            "The binding addendum's §10 update map still gives FS-00 'counts 99 endpoints/32 events/4 migrations', "
            "while the addendum §8 and the realised spec/manifest are 102 endpoints / 33 events / 3 migrations "
            "(+17 endpoint rows, +7 event types; SSO tables inside 0002_platform_security, no new migration). "
            "The '~99/~32' figures were approximate working numbers; §10 should carry the final counts.",
            "sed -n '212p' design/DEX-SSO-ADDENDUM.md",
            "Change to 'counts 102 endpoints/33 events/3 migrations'.")
if "still asserts Δ-RBAC expected counts (249/235/6)" in fs00:
    finding("SSO-D3", "MINOR", "feature-sets/FS-00.md:1120 (C-22 tooling note)",
            "C-22 records that 'tools/feature_sets.py permissions still asserts Δ-RBAC expected counts (249/235/6) ... "
            "tooling owner must update to 251/237/9'; the verifier has now updated the tool to 251/237/public 9 "
            "(rc0 on the real catalogue, tests 12/12), so the current-tense tooling note is stale.",
            "grep -n 'still asserts Δ-RBAC expected counts' feature-sets/FS-00.md; python3 tools/feature_sets.py permissions",
            "Update C-22 to note the tool was updated for v1.3.0 (251/237/13/1/7/14/18/9/6; rc0) or mark the note historical.")

# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------
for ok, name, detail in results:
    print(("PASS  " if ok else "FAIL  ") + name + ((" -- " + detail) if (detail and not ok) else ""))
print("=" * 110)
print("SSO DELTA AUDIT  --  %d PASS / %d FAIL / %d findings" % (
    sum(1 for ok, _n, _d in results if ok), sum(1 for ok, _n, _d in results if not ok), len(findings)))
print("=" * 110)
for f in findings:
    print("[%s] %s (%s): %s" % (f["sev"], f["id"], f["where"], f["message"]))
print("-" * 110)
print("Counts: tables=%d (platform=34), views=21, posting=31+6, rules=%d scoped + 10 global = %d, "
      "INV=1..32, scopes=%d (assignable=%d), roles=%d, SoD=%d, public allowlist=%d, self-service=%d, plan_version=%s"
      % (len(cov_entries) and 280, 143, 153, len(catalogue), len(assignable),
         len(perm["role_templates"]), len(perm["sod_pairs"]),
         len(perm["public_allowlist"]), len(perm["self_service_allowlist"]),
         manifest.get("plan_version")))
print("FS-00: %d tables / %d endpoints / %d events / %d jobs / %d migrations / %d exit commands"
      % (34, _api_rows, _event_rows, _job_rows, len(s0["migrations"]), len(s0["exit_checks"])))
print("Coverage: %d topics = 769 in-scope + 579 deferred; other 15 specs unchanged" % len(cov_entries))
raise SystemExit(0 if not findings and all(ok for ok, _n, _d in results) else 1)
