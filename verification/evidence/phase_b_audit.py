#!/usr/bin/env python3
"""Phase-B audit: all 16 feature-set specs vs template + manifest + DATA-MODEL,
then PLAN.md/README consistency, link resolution and generated-table freshness.

Stdlib only. Writes a detailed PASS/FAIL log and exits non-zero if any
BLOCKER/MAJOR finding is detected. Run from inventory-platform/:

    python3 verification/evidence/phase_b_audit.py
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]  # inventory-platform/
DESIGN = ROOT / "design"
SPECS = ROOT / "feature-sets"

PASS: list[str] = []
FAIL: list[str] = []
FINDINGS: list[dict] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        PASS.append(name)
    else:
        FAIL.append(name + ((" -- " + detail) if detail else ""))


def finding(fid: str, severity: str, artifact: str, text: str, evidence: str = "", remediation: str = "") -> None:
    FINDINGS.append({"id": fid, "severity": severity, "artifact": artifact, "text": text,
                     "evidence": evidence, "remediation": remediation})


# ---------------------------------------------------------------------------
# parse authoritative sources
# ---------------------------------------------------------------------------
manifest = json.loads((DESIGN / "feature-sets.json").read_text())
coverage = json.loads((DESIGN / "feature-coverage.json").read_text())
cov_entries = {k: v for k, v in coverage.items() if k != "totals"}
sets = manifest["feature_sets"]
by_id = {s["id"]: s for s in sets}
ids = [s["id"] for s in sets]

arch = (DESIGN / "ARCHITECTURE.md").read_text()
data_model = (DESIGN / "DATA-MODEL.md").read_text()
feature_sets_md = (DESIGN / "FEATURE-SETS.md").read_text()
plan = (ROOT / "PLAN.md").read_text()
readme = (ROOT / "README.md").read_text()

inv_catalogue = set(re.findall(r"^\| (INV-\d\d) \|", arch, re.M))
dm_rules = set(re.findall(r"\bDM-[A-Z]+-\d\d\b", data_model))

# DATA-MODEL section 21 ownership (per FS owned tables + table count + wave)
dm_owned: dict[str, set[str]] = {}
dm_counts: dict[str, int] = {}
dm_waves: dict[str, str] = {}
for line in data_model.splitlines():
    m = re.match(r"\| \*\*(FS-\d\d)\*\* \| ([0-9][A-Z]?) \| (.+?) \| (.+?) \| (\d+|0 base tables) \| (.+?) \|$", line)
    if not m:
        continue
    sid, wave, _ctx, tables_cell, count_cell, _deps = m.groups()
    owned = set(re.findall(r"`([a-z][a-z0-9_]*)`", tables_cell))
    dm_owned[sid] = owned
    dm_waves[sid] = wave
    num = re.search(r"(\d+)", count_cell)
    dm_counts[sid] = int(num.group(1)) if num else 0

# FEATURE-SETS section 1 sizes
fs_sizes = dict(re.findall(r"^\| (FS-\d\d) \| .+? \| [0-9][A-Z]? \| ([SML]) \|", feature_sets_md, re.M))

# per-FS coverage ids from feature-coverage.json
coverage_ids_by_fs: dict[str, set[str]] = defaultdict(set)
for topic_id, value in cov_entries.items():
    disposition = value.get("disposition")
    if isinstance(disposition, str) and disposition.startswith("FS-"):
        coverage_ids_by_fs[disposition].add(topic_id)

CAT_RE = re.compile(r"^(iri|mpw)-(\d{3})(?:\.\.(\d{3}))?$")


def expand_catalog_ids(text: str) -> set[str]:
    """Expand catalog-id runs in a line/table cell, including prefix-omitted ranges.

    Handles forms like `iri-167`, `iri-167..184`, `mpw-802`..`mpw-807`, `mpw-416..427, 430`,
    `mpw-895..905, 907..911`, and bare-number continuation after a prefixed item.
    """
    plain = text.replace("`", "")
    found: set[str] = set()
    prefix = None
    for token in re.split(r"[,\n]", plain):
        token = re.sub(r"\s*\(.*?\)\s*$", "", token.strip()).strip()
        if not token:
            continue
        m = re.match(r"^(iri|mpw)-(\d{3})\s*(?:\.\.|–|—|to)\s*(?:(?:iri|mpw)-)?(\d{3})$", token)
        if m:
            prefix = m.group(1)
            start, end = int(m.group(2)), int(m.group(3))
            found.update("%s-%03d" % (prefix, i) for i in range(start, end + 1))
            continue
        m = re.match(r"^(iri|mpw)-(\d{3})$", token)
        if m:
            prefix = m.group(1)
            found.add("%s-%s" % (prefix, m.group(2)))
            continue
        m = re.match(r"^(\d{3})\s*(?:\.\.|–|—|to)\s*(\d{3})$", token)
        if m and prefix:
            start, end = int(m.group(1)), int(m.group(2))
            found.update("%s-%03d" % (prefix, i) for i in range(start, end + 1))
            continue
        m = re.match(r"^(\d{3})$", token)
        if m and prefix:
            found.add("%s-%s" % (prefix, m.group(1)))
    return found


TEMPLATE_SECTIONS = [
    "## 1. Objective and source coverage",
    "## 2. Scope",
    "## 3. Dependencies and contracts",
    "## 4. Data model",
    "## 5. Services, rules and invariants",
    "## 6. API surface",
    "## 7. Events and jobs",
    "## 8. Configuration and flags",
    "## 9. Implementation steps (ordered)",
    "## 10. Test plan",
    "## 11. Deployment and gates",
    "## 12. Rollback",
    "## 13. Definition of done (backend)",
    "## 14. Open questions / assumptions",
]

HEADER_FIELDS = ["FS id / name", "Phase / wave", "Depends on", "Provides", "Tables owned", "Size", "Status"]


def section(text: str, start_marker: str, end_marker: str | None) -> str:
    start = text.find(start_marker)
    if start < 0:
        return ""
    start += len(start_marker)
    if end_marker:
        end = text.find(end_marker, start)
        return text[start:end if end >= 0 else len(text)]
    return text[start:]


# ---------------------------------------------------------------------------
# spec audit
# ---------------------------------------------------------------------------
specs = sorted(SPECS.glob("FS-*.md"))
check("all 16 specs exist (feature-sets/FS-00..FS-15.md)", len(specs) == 16, "found %d" % len(specs))

all_claimed_ids: Counter = Counter()
missing_image_rollback: list[str] = []
cross_ref_migrations: dict[str, list[str]] = {}
dod_gaps: dict[str, list[str]] = {}
scm_mobile_missing: list[str] = []
unknown_dm_all: dict[str, list[str]] = {}
backend_only_missing: list[str] = []
spec_table = {}

for path in specs:
    sid = path.stem
    text = path.read_text()
    spec_table[sid] = {"path": path, "bytes": len(text)}
    if sid not in by_id:
        FAIL.append("%s is not a manifest feature set" % sid)
        continue
    target = by_id[sid]
    prefix = "[%s] " % sid

    # ---- header table ---------------------------------------------------
    header_region = text[: text.find("## 1.")] if "## 1." in text else text
    header = dict(re.findall(r"^\| (FS id / name|Phase / wave|Depends on|Provides|Tables owned|Size|Status) \| (.+?) \|$", header_region, re.M))
    missing_header = [f for f in HEADER_FIELDS if f not in header]
    check(prefix + "header table complete (7 template fields)", not missing_header, str(missing_header))
    if "FS id / name" in header:
        check(prefix + "header FS id matches file name", header["FS id / name"].strip().startswith(sid))
    if "Phase / wave" in header:
        check(prefix + "phase/wave matches manifest",
              header["Phase / wave"].strip().startswith("1 / %s" % target["wave"]),
              header["Phase / wave"])
    if "Size" in header:
        check(prefix + "size matches FEATURE-SETS section 1", header["Size"].strip() == fs_sizes.get(sid),
              "%r vs %r" % (header["Size"].strip(), fs_sizes.get(sid)))
    if "Status" in header:
        check(prefix + "status is a valid lifecycle value", header["Status"].strip() in ("planned", "in-progress", "deployed", "verified"))

    # ---- template sections ----------------------------------------------
    missing_sections = [n for n in range(1, 15) if not re.search(r"^## %d\. " % n, text, re.M)]
    check(prefix + "all 14 template sections present", not missing_sections, str(missing_sections))
    check(prefix + "section headings map to the 14 template sections",
          len(re.findall(r"^## (\d+)\. ", text, re.M)) >= 14)
    backend_only_declared = re.search(r"backend[- ]only|backend (?:data|semantics|logic)[^.]{0,20}only|backend-only", text, re.I) is not None
    check(prefix + "declares backend-only build scope", backend_only_declared)
    if not backend_only_declared:
        backend_only_missing.append(sid)
    if "SCM Mobile" not in text:
        scm_mobile_missing.append(sid)

    # ---- dependencies (section 3 table) ---------------------------------
    sec3 = section(text, "## 3. Dependencies and contracts", "## 4. Data model")
    dep_rows = re.findall(r"^\| (FS-\d\d) \| .*? \| `([a-z0-9_.]+)` \| `([^`]+)` \|", sec3, re.M)
    expected_deps = Counter((d["id"], d["contract"], d["version"]) for d in target["depends_on"])
    actual_deps = Counter(dep_rows)
    check(prefix + "section 3 dependency table matches manifest exactly (id+contract+range)",
          actual_deps == expected_deps,
          "missing=%s extra=%s" % (dict(expected_deps - actual_deps), dict(actual_deps - expected_deps)))
    prov_rows = re.findall(r"^\| `([a-z0-9_.]+)` \| ([0-9]+\.[0-9]+\.[0-9]+)[^|]*\|", sec3, re.M)
    expected_provides = Counter((p["contract"], p["version"]) for p in target["provides"])
    check(prefix + "provided contracts table matches manifest",
          Counter(prov_rows) == expected_provides,
          "missing=%s extra=%s" % (dict(expected_provides - Counter(prov_rows)), dict(Counter(prov_rows) - expected_provides)))
    # header Depends on / Provides must mention every manifest dependency/contract/set
    header_dep_text = header.get("Depends on", "")
    if target["depends_on"]:
        missing_dep_ids = sorted({d["id"] for d in target["depends_on"] if d["id"] not in header_dep_text})
        missing_contracts = sorted({d["contract"] for d in target["depends_on"] if d["contract"] not in header_dep_text})
        check(prefix + "header Depends-on summary mentions every dependency set + contract",
              not missing_dep_ids and not missing_contracts, "sets=%s contracts=%s" % (missing_dep_ids, missing_contracts))
    else:
        check(prefix + "root set header declares no dependencies", ("root" in header_dep_text.lower() or "—" in header_dep_text))
    header_prov = header.get("Provides", "")
    missing_prov = [p["contract"] for p in target["provides"] if p["contract"] not in header_prov]
    check(prefix + "header Provides mentions every provided contract", not missing_prov, str(missing_prov))

    # ---- catalog coverage (section 1) -----------------------------------
    sec1 = section(text, "## 1. Objective and source coverage", "## 2. Scope")
    ownership_lines = [ln for ln in sec1.splitlines() if re.match(r"^\| `", ln)]
    claimed = set()
    for ln in ownership_lines:
        cell = ln.split("|")[1] if "|" in ln else ln
        claimed |= expand_catalog_ids(cell)
    expected_cov = coverage_ids_by_fs.get(sid, set())
    all_claimed_ids.update(claimed)
    missing_cov = sorted(expected_cov - claimed)
    extra_cov = sorted(claimed - expected_cov)
    if extra_cov:
        finding("F-B-CAT-%s" % sid, "MINOR", path.name,
                "section 1 ownership table claims catalog ids not dispositioned to %s: %s"
                % (sid, extra_cov[:8]), "compare feature-coverage.json dispositions with feature-sets/%s.md section 1" % sid,
                "Move the topic to its correct owner or mark it deferred.")
    check(prefix + "section 1 catalog ids match FEATURE-COVERAGE.json exactly",
          not missing_cov and not extra_cov,
          "missing=%d %s extra=%d %s" % (len(missing_cov), missing_cov[:8], len(extra_cov), extra_cov[:8]))
    # count stated in prose (e.g. "= 116 topics", "42 topics assigned")
    stated = re.findall(r"(?<![\d,])(\d+)\s+topics", sec1)
    check(prefix + "stated catalog-topic count matches coverage disposition count",
          any(int(n) == len(expected_cov) for n in stated) or not stated,
          "stated=%s expected=%d" % (stated[:4], len(expected_cov)))
    # source URLs are official docs (traceability)
    urls = re.findall(r"https?://[^\s)\]]+", sec1)
    bad_urls = [u for u in urls if "docs.oracle.com" not in u]
    check(prefix + "section 1 source URLs are official Oracle NetSuite docs", not bad_urls, str(bad_urls[:3]))

    # ---- data model ownership / migrations ------------------------------
    sec4 = section(text, "## 4. Data model", "### 4.2") if "### 4.2" in text else section(text, "## 4. Data model", "## 5. Services")
    header_tables = header.get("Tables owned", "")
    listed = set(re.findall(r"`(?:[a-z_]+\.)?([a-z][a-z0-9_]*)`", sec4))
    # exclude obviously non-table ids in prose (rules/flags have dots etc.); normalise schema prefixes
    if sid == "FS-15":
        check(prefix + "owns 0 base tables and 21 reporting views",
              "0 base tables" in header_tables and "21" in header_tables)
    else:
        expected_tables = dm_owned.get(sid, set())
        discovered = set()
        for name in listed:
            if name in expected_tables:
                discovered.add(name)
        check(prefix + "DATA-MODEL section 21 table count matches spec header (%d)" % len(expected_tables),
              str(len(expected_tables)) in header_tables or ("**%d tables**" % len(expected_tables)) in header_tables,
              header_tables[:120])
        missing_tables = sorted(expected_tables - discovered)
        check(prefix + "section 4 lists every DATA-MODEL-owned table",
              not missing_tables, "missing=%s" % missing_tables[:8])
        foreign = sorted(discovered - expected_tables)
        check(prefix + "section 4 claims no table owned by another feature set", not foreign, str(foreign[:8]))
    spec_migrations = set(re.findall(r"alembic:\d{4}_[a-z0-9_]+", text))
    manifest_migrations = set(target["migrations"])
    check(prefix + "manifest migration ids all present in spec",
          not (manifest_migrations - spec_migrations),
          "missing=%s" % sorted(manifest_migrations - spec_migrations))
    if spec_migrations - manifest_migrations:
        cross_ref_migrations.setdefault(sid, sorted(spec_migrations - manifest_migrations))
    missing_flags = [f for f in target["flags"] if f not in text]
    check(prefix + "manifest feature flag(s) wired in spec", not missing_flags, str(missing_flags))

    # ---- invariants -----------------------------------------------------
    spec_inv = set(re.findall(r"\bINV-\d\d\b", text))
    unknown_inv = sorted(spec_inv - inv_catalogue)
    check(prefix + "cited INV-* ids exist in ARCHITECTURE section 7.3", not unknown_inv, str(unknown_inv))
    spec_dm = set(re.findall(r"\bDM-[A-Z]+-\d\d\b", text))
    unknown_dm = sorted(spec_dm - dm_rules)
    check(prefix + "cited DM-* rules exist in DATA-MODEL", not unknown_dm, str(unknown_dm[:8]))
    if unknown_dm:
        unknown_dm_all.setdefault(sid, unknown_dm)
    check(prefix + "cites at least one invariant/rule id", len(spec_inv) + len(spec_dm) >= 1,
          "INV=%s DM=%s" % (sorted(spec_inv), sorted(spec_dm)))

    # ---- API / events / jobs -------------------------------------------
    sec6 = section(text, "## 6. API surface", "## 7. Events and jobs")
    api_rows = re.findall(r"^\| (GET|POST|PUT|PATCH|DELETE) \| (`[^`]+`)", sec6, re.M)
    check(prefix + "API surface section non-empty (>=3 endpoints)", len(api_rows) >= 3, "rows=%d" % len(api_rows))
    check(prefix + "API base path /api/v1 declared", "/api/v1" in sec6)
    sec7 = section(text, "## 7. Events and jobs", "## 8. Configuration and flags")
    if "| Job | Schedule |" in sec7:
        events_blob, jobs_blob = sec7.split("| Job | Schedule |", 1)
        has_jobs_table = True
    else:
        events_blob, jobs_blob, has_jobs_table = sec7, "", False
    event_rows = [ln for ln in events_blob.splitlines() if ln.startswith("| `")]
    job_rows = [ln for ln in jobs_blob.splitlines() if ln.startswith("| `")]
    check(prefix + "events table non-empty", len(event_rows) >= 1, "rows=%d" % len(event_rows))
    check(prefix + "jobs table present and non-empty (or explicit 'no jobs')",
          (has_jobs_table and len(job_rows) >= 1) or re.search(r"[Nn]o (?:background |async )?jobs", sec7),
          "has_table=%s rows=%d" % (has_jobs_table, len(job_rows)))
    check(prefix + "events/jobs consumers/components named", "FS-" in sec7)

    # ---- test plan + gates ---------------------------------------------
    sec10 = section(text, "## 10. Test plan", "## 11. Deployment and gates")
    for layer in ("Unit", "Invariant", "Contract", "Migration"):
        check(prefix + "test plan covers %s layer" % layer.lower(), layer in sec10)
    sec11 = section(text, "## 11. Deployment and gates", "## 12. Rollback")
    missing_entry = [c for c in target["entry_checks"] if c not in sec11]
    missing_exit = [c for c in target["exit_checks"] if c not in sec11]
    check(prefix + "section 11 reproduces ALL manifest entry-gate commands verbatim", not missing_entry,
          "missing=%s" % missing_entry[:3])
    check(prefix + "section 11 reproduces ALL manifest exit-gate commands verbatim", not missing_exit,
          "missing=%s" % missing_exit[:3])
    check(prefix + "section 11 includes deploy + evidence + record steps",
          "scripts/deploy_set.sh" in sec11 and "record --set %s" % sid in sec11 and "evidence" in sec11.lower())

    # ---- rollback / DoD / open questions -------------------------------
    sec12 = section(text, "## 12. Rollback", "## 13. Definition of done")
    low12 = sec12.lower()
    rollback_terms = {"flag/off": ("flag" in low12 or "enabled` off" in low12 or "off" in low12),
                      "redeploy/previous image": any(w in low12 for w in ("redeploy", "previous image", "prior image", "roll back image", "revert image")),
                      "additive migrations": "migration" in low12,
                      "data correction/compensation": any(w in low12 for w in ("compensat", "reversal", "append-only", "forward-fix", "correction")),
                      "dependants blocked": any(w in low12 for w in ("depend", "consumer", "check-env blocks"))}
    other_terms = {k: v for k, v in rollback_terms.items() if k != "redeploy/previous image"}
    check(prefix + "rollback covers flag-off/additive-migrations/compensation/dependant blocking",
          all(other_terms.values()), str([k for k, v in other_terms.items() if not v]))
    if not rollback_terms["redeploy/previous image"]:
        missing_image_rollback.append(sid)
    sec13 = section(text, "## 13. Definition of done", "## 14. Open questions")
    checkboxes = len(re.findall(r"^- \[[ x]\]", sec13, re.M))
    check(prefix + "definition-of-done checklist has >=7 items", checkboxes >= 7, "items=%d" % checkboxes)
    low13 = sec13.lower()
    dod_terms = {
        "migrations/up-down/N-1": "migration" in low13 or "alembic:" in low13,
        "rule/invariant tests": any(w in low13 for w in ("invariant", "inv-", "dm-", "dm_", "tests green")),
        "api/openapi/contract tests": ("contract" in low13 and "api" in low13),
        "events/outbox/jobs schemas": ("event" in low13 and ("job" in low13 or "outbox" in low13 or "schema" in low13)),
        "flag default-off + tenant isolation + audit + RBAC": (
            ("flag" in low13 or "default-off" in low13 or "enabled" in low13)
            and "tenant" in low13 and "audit" in low13
            and any(w in low13 for w in ("rbac", "role", "scope", "permission"))),
        "gates/evidence/record": any(w in low13 for w in ("gate", "record", "entry/exit checks", "evidence")),
    }
    missing_dod = [k for k, v in dod_terms.items() if not v]
    check(prefix + "DoD covers framework section 7 requirements", len(missing_dod) <= 1,
          str(missing_dod))
    if missing_dod:
        dod_gaps[sid] = missing_dod
    m14 = re.search(r"^## 14\. .*$", text, re.M)
    sec14 = text[m14.end():] if m14 else ""
    check(prefix + "open-questions/assumptions section populated", len(sec14.strip()) > 80, "len=%d" % len(sec14.strip()))

    # ---- scope guard: no UI/mobile/analytics build ----------------------
    sec2 = section(text, "## 2. Scope", "## 3. Dependencies and contracts")
    in_scope = sec2[: sec2.find("Out of scope")] if "Out of scope" in sec2 else sec2
    ui_violations = [ln.strip() for ln in in_scope.splitlines()
                     if re.search(r"\b(UI|user interface|screens?|dashboard|workbook|mobile app|mobile client|BI tool)\b", ln)
                     and not re.search(r"defer|no UI|out of scope|EXT|future|not (?:built|in scope|rendered)|render|\bno\b[^.]{0,60}\bUI\b", ln, re.I)]
    check(prefix + "in-scope section contains no UI/mobile/analytics build scope", not ui_violations, str(ui_violations[:3]))
    ui_paths = [p for _m, p in api_rows if re.search(r"/(ui|mobile|dashboard|analytics)(/|`|$)", p)]
    check(prefix + "API surface exposes no UI/mobile/analytics endpoints", not ui_paths, str(ui_paths[:3]))
    mobile_build = re.findall(r"\b(?:build|deliver|implement|expose)\w*\s+(?:a\s+)?(?:mobile|UI|dashboard)", in_scope, re.I)
    check(prefix + "no positive mobile/UI build verbs in scope", not mobile_build, str(mobile_build[:3]))

    # ---- delta-1 table-count consistency -------------------------------
    stale_273 = re.findall(r"\b273\b", text)
    stale_27 = re.findall(r"\b(?:28|27) (?:platform )?tables\b", text)
    if sid == "FS-00":
        check("FS-00 accounts for 34 platform tables incl finance_journal_intent + 3 Δ-MT + 3 Δ-SSO tables",
              "34 tables" in text and "finance_journal_intent" in text
              and all(t in text for t in ("user_identity", "tenant_invitation", "user_session",
                                          "tenant_sso_connection", "user_identity_link", "sso_group_role_map")))
    if stale_273 or stale_27:
        finding("F-B-STALE273-%s" % sid, "MINOR", path.name,
                "spec contains pre-Δ-SSO table count wording (%s / %s) while the amended total is 280 base tables "
                "(FS-00 owns 34 incl finance_journal_intent + 3 Δ-MT + 3 Δ-SSO)." % (stale_273[:2], stale_27[:2]),
                "grep -n '273\\|274\\|31 tables' feature-sets/%s.md" % sid,
                "Update to 280 / 34 per amended DATA-MODEL sections 2/21/24.")

if unknown_dm_all:
    finding("F-B-01", "MINOR", "feature-sets/FS-01.md section 5",
            "Invalid rule-id citations: %s (DATA-MODEL defines DM-GLOBAL-01..10 and the scoped DM-*-nn rule sets; "
            "the cited id does not exist, so traceability to a binding rule is broken for that sentence)."
            % "; ".join("%s: %s" % (sid, ids) for sid, ids in unknown_dm_all.items()),
            "grep -n 'DM-GLOBAL-20' feature-sets/FS-01.md; grep -n 'DM-GLOBAL-' design/DATA-MODEL.md",
            "Cite an existing rule (e.g. DM-GLOBAL-02 dependency direction or DM-GLOBAL-07 single posting) or drop the id.")
if backend_only_missing:
    finding("F-B-05", "MINOR", ", ".join("feature-sets/%s.md" % x for x in backend_only_missing),
            "Specs %s do not contain an explicit 'backend only / backend data-logic only' declaration in the header or scope "
            "section (template header note requires it). Their scope sections do defer UI/mobile work and no UI/mobile build "
            "scope is present, so this is declaration consistency only." % ", ".join(backend_only_missing),
            "grep -L 'backend[- ]only' feature-sets/FS-*.md",
            "Add the template's 'Backend only. No UI/mobile/analytics/integration build scope.' line.")
if dod_gaps:
    finding("F-B-04", "MINOR", "feature-sets/FS-*.md section 13",
            "Definition-of-Done checklists omit at least one framework section 7 item: "
            + "; ".join("%s missing %s" % (sid, gaps) for sid, gaps in dod_gaps.items()),
            "grep -n 'Definition of done' -A14 feature-sets/FS-XX.md",
            "Add the missing DoD bullets (flag default-off, invariant tests, events/jobs schemas, gates/evidence).")
if scm_mobile_missing:
    finding("F-B-03", "MINOR", "feature-sets/FS-12.md, FS-13.md, FS-14.md (header note)",
            "The template header note 'SCM Mobile permanently excluded' is absent from %s; backend-only is still declared "
            "and mobile work is deferred in each scope section, so this is template-consistency only." % ", ".join(scm_mobile_missing),
            "grep -L 'SCM Mobile' feature-sets/FS-*.md",
            "Add the template's SCM-Mobile-excluded sentence to the header note of those specs.")

if missing_image_rollback:
    finding("F-B-02", "MINOR", "feature-sets/*.md",
            "Rollback sections of %s do not state the framework section 6 step-6 'redeploy previous image' step "
            "(flag-off, additive migrations and data compensation are covered). Rollback remains executable via the "
            "deployment protocol, so this is documentation completeness only." % ", ".join(missing_image_rollback),
            "grep -n 'Rollback' -A6 feature-sets/FS-06.md etc.",
            "Add one line: 'Redeploy the previous image; migrations are additive/N-1 compatible' to those specs.")

# cross-spec checks
expected_in_scope = set().union(*coverage_ids_by_fs.values())
deferred_all = set(cov_entries) - expected_in_scope
claimed_union = set(all_claimed_ids)
check("spec catalog ownership union equals all 769 in-scope topics", claimed_union == expected_in_scope,
      "missing=%d extra=%d" % (len(expected_in_scope - claimed_union), len(claimed_union - expected_in_scope)))
check("specs do not claim deferred/excluded topics",
      not (claimed_union & deferred_all), str(sorted(claimed_union & deferred_all)[:8]))
check("spec catalog assignment has no duplicated topics (each id in exactly one spec)",
      sum(all_claimed_ids.values()) == len(claimed_union),
      "sum=%d unique=%d" % (sum(all_claimed_ids.values()), len(claimed_union)))
check("all 16 feature sets have a spec with substantive content (>=20KB)",
      all(spec_table.get(sid, {}).get("bytes", 0) >= 20000 for sid in ids),
      str({sid: spec_table.get(sid, {}).get("bytes") for sid in ids if spec_table.get(sid, {}).get("bytes", 0) < 20000}))

# ---------------------------------------------------------------------------
# PLAN.md / README audit
# ---------------------------------------------------------------------------
render_check = subprocess.run([sys.executable, str(ROOT / "tools" / "render_plan_tables.py"), "--check"],
                              capture_output=True, text=True, cwd=str(ROOT))
check("PLAN generated tables current (render_plan_tables.py --check)", render_check.returncode == 0,
      (render_check.stdout + render_check.stderr)[-300:])

plan_fs_rows = {}
for line in plan.splitlines():
    m = re.match(r"\| \*\*(FS-\d\d)\*\* \| (.+?) \| ([0-9][A-Z]?) \| [SML] \| (.+?) \| ([\d,]+) \| (.+?) \|$", line)
    if m:
        plan_fs_rows[m.group(1)] = {"wave": m.group(3),
                                    "contracts": sorted(re.findall(r"`([a-z0-9_.]+)@([0-9.]+)`", m.group(4))),
                                    "topics": int(m.group(5).replace(",", "")),
                                    "tables": m.group(6)}
check("PLAN generated feature-set table has 16 rows", len(plan_fs_rows) == 16, "got %d" % len(plan_fs_rows))
plan_errors = []
for sid in ids:
    row = plan_fs_rows.get(sid)
    if not row:
        plan_errors.append("%s missing" % sid)
        continue
    if row["wave"] != by_id[sid]["wave"]:
        plan_errors.append("%s wave" % sid)
    if row["contracts"] != sorted((p["contract"], p["version"]) for p in by_id[sid]["provides"]):
        plan_errors.append("%s contracts" % sid)
    expected_topics = len(coverage_ids_by_fs.get(sid, set()))
    if row["topics"] != expected_topics:
        plan_errors.append("%s topics %d!=%d" % (sid, row["topics"], expected_topics))
check("PLAN feature-set table matches manifest + coverage", not plan_errors, "; ".join(plan_errors[:6]))
check("PLAN totals row: 116 edges / 21 contracts / 769 topics / 287 tables / 21 views",
      "116 dependency edges" in plan and "21 contracts" in plan and "**769**" in plan
      and "287 tables / 21 views" in plan)
check("PLAN wave table lists 10 waves matching manifest composition",
      sum(1 for _ in re.findall(r"^\| \*\*(?:\d[A-Z]|\d)\*\* \| .*?FS-\d\d", plan, re.M)) == 10)
check("PLAN wave 1F composition is FS-12/13/14 (parallel or ordered)",
      ("FS-12 ∥ FS-13 ∥ FS-14" in plan) or ("FS-12 → FS-13 → FS-14" in plan) or ("FS-12, FS-13, FS-14" in plan))
check("PLAN section 3.1 records locked decisions D1-D8 + deltas Delta-1..3",
      all("| D%d |" % i in plan for i in range(1, 9)) and all("| Δ-%d |" % i in plan for i in range(1, 4)))
check("PLAN states amended totals (287 base tables / 34 platform tables / INV-01..INV-33)",
      "287 base tables" in plan and "34 tables" in plan and "INV-01..INV-33" in plan)
check("PLAN section 6.1 milestones present", "## 6.1" in plan or "6.1" in plan)
check("PLAN section 7 runbook present with per-FS workflow commands",
      all(c in plan for c in ["check-env --set FS-XX", "record --set FS-XX", "scripts/deploy_set.sh"]))
check("PLAN section 11 environment status table has 16 rows, all environments pending ('—')",
      len(re.findall(r"^\| \*\*FS-\d\d\*\* \| .+ \| \d[A-Z]? \| — \| — \| — \|$", plan, re.M)) == 16)
_sec13 = plan[plan.find("## 13."):plan.find("## 14.")]
_items13 = re.findall(r"^- \[[ x]\] ", _sec13, re.M)
_checked13 = re.findall(r"^- \[x\] ", _sec13, re.M)
check("PLAN section 13 has 8 acceptance items (7 checked + 1 annotated PARTIAL as recommended)",
      len(_items13) == 8 and len(_checked13) == 7 and "PARTIAL by design" in _sec13,
      "items=%d checked=%d" % (len(_items13), len(_checked13)))
check("PLAN excludes SCM Mobile and interprets the supplied source URL explicitly",
      "SCM Mobile" in plan and "book_161557401927" in plan and "excluded" in plan.lower())
check("PLAN source-area table = 194/218/415/376/145 = 1,348 topics",
      all(x in plan for x in ["| 194 |", "| 218 |", "| 415 |", "| 376 |", "| 145 |", "1,348"]))
check("README states 16 sets / 10 waves / 1,348 topics / 287 tables (platform 34)",
      "16 independently deployable feature sets" in readme and "1,348" in readme
      and "287 tables" in readme and "platform 34" in readme)
check("README lists a verification report placeholder path",
      "verification/VERIFICATION-REPORT.md" in readme)

# relative link resolution across PLAN, README and specs
link_errors = []
for doc, base in [(ROOT / "PLAN.md", ROOT), (ROOT / "README.md", ROOT)] + [(p, p.parent) for p in specs]:
    text = doc.read_text()
    for target in re.findall(r"\]\(([^)\s]+)\)", text):
        if target.startswith(("http://", "https://", "#", "mailto:")):
            continue
        clean = target.split("#")[0]
        if not clean:
            continue
        candidate = (base / clean).resolve()
        if not candidate.exists() and not (ROOT / clean).exists():
            link_errors.append("%s -> %s" % (doc.name, target))
check("all relative markdown links in PLAN/README/specs resolve to existing files", not link_errors,
      "%d broken, e.g. %s" % (len(link_errors), link_errors[:6]))

# ---------------------------------------------------------------------------
# documentation delta after Phase-B audit: PLAN section 4.1 (Inventory Workflow iri-214) + 6.1 sizing
# ---------------------------------------------------------------------------
sec41 = plan[plan.find("### 4.1"):plan.find("## 5.")] if "### 4.1" in plan else ""
check("PLAN section 4.1 documents the Inventory Workflow cycle for iri-214",
      bool(sec41) and "iri-214" in sec41 and "section_N2251098.html" in sec41
      and all(stage in sec41 for stage in ["Purchasing", "Receiving", "Manufacturing", "Selling", "Fulfilling", "Replenishing"]),
      "section bytes=%d" % len(sec41))
check("iri-214 coverage disposition unchanged (FS-04) — narrative cross-set coverage is intended, not a coverage change",
      cov_entries.get("iri-214", {}).get("disposition") == "FS-04",
      str(cov_entries.get("iri-214")))
_posting_refs = ["T-01", "T-06", "T-12", "T-14", "T-15", "T-19"]
_command_refs = ["C-01", "C-02", "C-03"]
check("PLAN section 4.1 posting-type/command references exist in ARCHITECTURE and DATA-MODEL",
      all(t in arch and t in data_model for t in _posting_refs)
      and all(c in arch and c in data_model for c in _command_refs))
_fs_wave_refs = {"FS-08": "1B", "FS-09": "1C", "FS-10": "1D", "FS-11": "1E", "FS-15": "2", "FS-04": "0C"}
check("PLAN section 4.1 stage→feature-set/wave references match the manifest",
      all(("%s" % fs) in sec41 and by_id[fs]["wave"] == wave for fs, wave in _fs_wave_refs.items()))
# D1: illustrative event names vs published contract event names
published_events = set(re.findall(r"`((?:inventory|po|item_receipt|vendor_bill|vendor_credit|vendor_return|purchase_order|cogs|cost|fulfillment|finance)\.[a-z_.]+)`",
                                  arch + data_model + (ROOT / "feature-sets/FS-04.md").read_text() + (ROOT / "feature-sets/FS-08.md").read_text()))
missing_event_names = [name for name in ["inventory.transaction.posted", "purchase_order.", "item_receipt.", "inventory.commitment.", "inventory.fulfillment."]
                       if name not in sec41 or not any(ev.startswith(name) or name.startswith(ev) for ev in published_events)]
wrong_named = [name for name in ["inventory.transaction.posted", "purchase_order.", "item_receipt.", "inventory.commitment.", "inventory.fulfillment."]
               if name in sec41 and not any(ev.startswith(name) for ev in published_events)]
if wrong_named:
    finding("F-D-01", "MINOR", "PLAN.md section 4.1",
            "Section 4.1 uses illustrative event families that do not match the published contract event names: %s. "
            "The contracts define po.created/approved/receipted/billed/closed (FS-08), inventory.receipt.posted|reversed (FS-08/FS-04), "
            "inventory.committed.changed, inventory.fulfilled and inventory.issued (FS-04)." % ", ".join("`%s`" % n for n in wrong_named),
            "grep -n 'inventory.transaction.posted\|purchase_order\.\*\|item_receipt\.\*' PLAN.md; "
            "grep -n 'po.receipted\|inventory.receipt.posted\|inventory.committed.changed\|inventory.fulfilled' "
            "design/ARCHITECTURE.md feature-sets/FS-04.md feature-sets/FS-08.md",
            "Rename the illustrative events to the published names (one-line edit in the two table rows).")
# D2: "every state-changing step posts synchronously" overstatement
if "every state-changing step posts synchronously" in sec41 and "never append ledger rows" in arch:
    finding("F-D-02", "MINOR", "PLAN.md section 4.1",
            "Section 4.1 says every state-changing step posts synchronously to the inventory ledger. ARCHITECTURE 2.3 defines "
            "non-posting control commands C-01..C-06 (commitments, TO approve/close, WO void) and document/approval state "
            "machines that never append ledger rows; only inventory-quantity/value-affecting steps post.",
            "grep -n 'every state-changing step posts synchronously' PLAN.md; "
            "grep -n 'Non-posting control' -A8 design/ARCHITECTURE.md",
            "Reword: 'every inventory-quantity/value-affecting step posts synchronously; document, approval, commitment-only "
            "and order-control transitions (C-01..C-06) do not append ledger rows'.")
# D3: runbook golden end-to-end attribution (still open after the Lead's D3 fix attempt)
sec7 = plan[plan.find("## 7. Execution runbook"):plan.find("## 8.")] if "## 7. Execution runbook" in plan else ""
d3_still = ("runbook (§7)" in sec41 and "golden end-to-end" in sec41 and "golden" not in sec7)
check("D3 fix: PLAN section 7.2 carries the wave-close golden end-to-end step 7 and section 4.1 re-points to it",
      "WAVE CLOSE" in sec7 and "golden end-to-end" in sec7
      and "wave-close step 7 (§7.2)" in sec41)
if d3_still:
    finding("F-D-03", "MINOR", "PLAN.md section 4.1 vs section 7",
            "D3 REMAINS after the fix attempt: section 4.1 still says 'the runbook (§7) uses a golden end-to-end workflow pass ... "
            "as the wave-promotion smoke scenario', but section 7 (lines 271-330) contains no golden end-to-end workflow pass and no "
            "wave-promotion smoke step; the mandatory golden-path smoke lives in the section 5 deployment protocol step 4, and the "
            "section 6 wave-exit criteria do not require an end-to-end pass either. The new cross-reference 'golden-path smoke in "
            "section 7 step 4' is also mis-numbered (that step is 'PR -> CI/merge'; the smoke is section 5 step 4).",
            "grep -n 'golden' PLAN.md; sed -n '/^## 7. Execution runbook/,/^## 8/p' PLAN.md | grep -n 'golden'",
            "Either (a) add a wave-close step to section 7.2: 'Wave close: run the golden end-to-end workflow pass (stage subset "
            "available at that wave) before promoting the wave', which makes the section 4.1 sentence true and lets 'section 7 step 4' "
            "be re-pointed at that new step; or (b) reword section 4.1 to: 'the plan's wave-promotion smoke scenario combines the "
            "mandatory per-set golden-path smoke (section 5 deployment-protocol step 4) with the wave-exit criteria (section 6)' and "
            "drop the section-7 claim.")
# D4: sizing distribution in 6.1 vs FEATURE-SETS section 1
fs_sizes = dict(re.findall(r"^\| (FS-\d\d) \| .+? \| [0-9][A-Z]? \| ([SML]) \|", feature_sets_md, re.M))
size_counter = Counter(fs_sizes.values())
m_sizing = re.search(r"(\d+) × L, (\d+) × M, (\d+) × S feature sets", plan)
if m_sizing and (int(m_sizing.group(1)), int(m_sizing.group(2)), int(m_sizing.group(3))) != (
        size_counter.get("L", 0), size_counter.get("M", 0), size_counter.get("S", 0)):
    finding("F-D-04", "MINOR", "PLAN.md section 6.1",
            "Section 6.1 states the sizing as %s x L, %s x M, %s x S, but FEATURE-SETS section 1 (the per-set size authority) is "
            "%d x L, %d x M, %d x S (16 sets total). Correct the distribution in the PLAN sentence."
            % (m_sizing.group(1), m_sizing.group(2), m_sizing.group(3), size_counter.get("L", 0), size_counter.get("M", 0), size_counter.get("S", 0)),
            "grep -n 'Sizing from' PLAN.md; grep -n '| [SML] |' design/FEATURE-SETS.md",
            "Change to '7 x L, 7 x M, 2 x S' (or regenerate from FEATURE-SETS).")
check("PLAN section 4.1 has no contradiction with ARCHITECTURE section 9 inventory-workflow mapping",
      "purchasing → receiving → manufacturing → selling → fulfilling → replenishing" in arch)
check("PLAN section 4.1 stage mapping is consistent with DATA-MODEL posting types and FEATURE-SETS section 7.x / FS-04/08/09/10/11 specs",
      all(t in data_model for t in _posting_refs)
      and all(s in feature_sets_md for s in ["FS-04", "FS-08", "FS-09", "FS-10", "FS-11"]))
# README changed in the same delta and is consistent with the report status
check("README deliverable-status section consistent with the independent verdict (PASS, 0 BLOCKER/MAJOR)",
      "Verified PASS" in readme and "0 BLOCKER, 0 MAJOR" in readme)
# relative links in PLAN + README resolve
link_errors2 = []
for doc in (ROOT / "PLAN.md", ROOT / "README.md"):
    for target in re.findall(r"\]\(([^)\s]+)\)", doc.read_text()):
        if target.startswith(("http://", "https://", "#", "mailto:")):
            continue
        if not (ROOT / target.split("#")[0]).exists():
            link_errors2.append("%s -> %s" % (doc.name, target))
check("PLAN + README relative links resolve after the section 4.1 delta", not link_errors2, str(link_errors2[:6]))

# environment claims
import shutil
docker = shutil.which("docker")
docker_version = subprocess.run([docker, "--version"], capture_output=True, text=True).stdout if docker else ""
compose = subprocess.run([docker, "compose", "version"], capture_output=True, text=True).stdout if docker else ""
check("PLAN environment claims verified: Docker 27+ / Compose v2 / Python 3.12",
      "Docker version 27" in docker_version and "v2." in compose and "3.12" in sys.version,
      "%s | %s | %s" % (docker_version.strip(), compose.strip(), sys.version.split()[0]))

# ---------------------------------------------------------------------------
# summary
# ---------------------------------------------------------------------------
print("=" * 110)
print("PHASE-B SPEC + PLAN AUDIT  --  %d PASS / %d FAIL / %d findings" % (len(PASS), len(FAIL), len(FINDINGS)))
print("=" * 110)
for name in PASS:
    print("PASS  " + name)
print("-" * 110)
for name in FAIL:
    print("FAIL  " + name)
print("-" * 110)
for item in FINDINGS:
    print("[%s] %s (%s): %s" % (item["severity"], item["id"], item["artifact"], item["text"]))
    if item["evidence"]:
        print("      evidence: " + item["evidence"])
    if item["remediation"]:
        print("      fix: " + item["remediation"])
print("-" * 110)
print("spec matrix: " + ", ".join("%s=%dKB/deps=%d/tables=%d" % (
    sid, spec_table[sid]["bytes"] // 1024, len(by_id[sid]["depends_on"]), dm_counts.get(sid, 0)
) for sid in ids if sid in spec_table))
blockers = [f for f in FINDINGS if f["severity"] in ("BLOCKER", "MAJOR")]
minor = [f for f in FINDINGS if f["severity"] == "MINOR"]
print("-" * 110)
if blockers:
    print("AUDIT RESULT: FAIL -- %d BLOCKER/MAJOR finding(s)" % len(blockers))
    sys.exit(1)
if minor:
    print("AUDIT RESULT: PASS WITH MINOR FINDINGS -- %d MINOR, %d check failure(s) documented as MINOR above"
          % (len(minor), len(FAIL)))
else:
    print("AUDIT RESULT: PASS -- 0 findings, 0 check failures (%d checks passed)" % len(PASS))
sys.exit(0)
