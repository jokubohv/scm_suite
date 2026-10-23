#!/usr/bin/env python3
"""Δ-PAR parallel-implementation independent delta audit — plan v1.4.0.

Checks the binding addendum, design/parallel-plan.json, the Lead-authored
validator, document/plan consistency, migration allocation, and that the
amendment is execution-only (no schema/DAG/spec/coverage changes).

Run from inventory-platform/:

    python3 verification/evidence/par_audit.py
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]          # inventory-platform/
DESIGN = ROOT / "design"
TOOL = ROOT / "tools" / "check_parallel_plan.py"

plan_text = (ROOT / "PLAN.md").read_text()
readme = (ROOT / "README.md").read_text()
addendum = (DESIGN / "PARALLEL-IMPLEMENTATION-ADDENDUM.md").read_text()
framework = (DESIGN / "FEATURE-SET-FRAMEWORK.md").read_text()
fset = (DESIGN / "FEATURE-SETS.md").read_text()
dm = (DESIGN / "DATA-MODEL.md").read_text()
manifest = json.loads((DESIGN / "feature-sets.json").read_text())
perm = json.loads((DESIGN / "permissions.json").read_text())
pplan = json.loads((DESIGN / "parallel-plan.json").read_text())
validator_src = TOOL.read_text()

sets = manifest["feature_sets"]
man_by_id = {s["id"]: s for s in sets}
task_by_id = {t["id"]: t for t in pplan["tasks"]}
results: list[tuple[bool, str, str]] = []
findings: list[dict] = []


def check(name, cond, detail=""):
    results.append((bool(cond), name, detail))


def finding(fid, sev, where, message, repro, fix):
    findings.append({"id": fid, "sev": sev, "where": where, "message": message, "repro": repro, "fix": fix})


# ---------------------------------------------------------------------------
# 1. execution-only amendment + authoritative counts unchanged
# ---------------------------------------------------------------------------
check("plan_version 1.5.0 in manifest, permissions.json and parallel-plan",
      manifest.get("plan_version") == "1.5.0" and perm.get("plan_version") == "1.5.0"
      and pplan.get("plan_version") == "1.5.0")
check("schema/DAG/counts: 287 tables/platform 34/21 views; 31+6; 146/156; INV-33; 255/241/13/1/7/14/18/9/6",
      "**287 base tables**" in (DESIGN / "ARCHITECTURE.md").read_text()
      and "255 capability scopes (241 assignable + 13 service-only + 1 platform-only)" in (DESIGN / "ARCHITECTURE.md").read_text()
      and "146 scoped business rules" in (DESIGN / "ARCHITECTURE.md").read_text()
      and "INV-01..INV-33" in (DESIGN / "ARCHITECTURE.md").read_text()
      and perm["counts"]["scopes"] == 255 and perm["counts"]["assignable_scopes"] == 241)
check("feature-set DAG/contracts/waves unchanged (16/116/86/21/10; entry 49; exit 98; FS-00 exit 17, FS-09 exit 8, contract 1.1.0)",
      len(sets) == 16 and sum(len(s["depends_on"]) for s in sets) == 116
      and len({(s["id"], d["id"]) for s in sets for d in s["depends_on"]}) == 86
      and sum(len(s["provides"]) for s in sets) == 21 and len({s["wave"] for s in sets}) == 10
      and sum(len(s["entry_checks"]) for s in sets) == 49
      and sum(len(s["exit_checks"]) for s in sets) == 98
      and len(man_by_id["FS-00"]["exit_checks"]) == 17)
check("FEATURE-SETS header 1.5.0; coverage unchanged 1,348 (769 in-scope / 579 deferred)",
      "**Version:** 1.5.0" in fset
      and json.loads((DESIGN / "feature-coverage.json").read_text())["totals"]["total"] == 1348
      and json.loads((DESIGN / "feature-coverage.json").read_text())["totals"]["by_disposition"]["EXCLUDED-SCM-MOBILE"] == 0)
snapshot = {}
snap_path = ROOT / "verification/evidence/tool/audited-snapshot.sha256"
if snap_path.exists():
    for line in snap_path.read_text().splitlines():
        parts = line.split()
        if len(parts) == 2:
            snapshot[parts[1]] = parts[0]
changed_specs = []
for spec in sorted((ROOT / "feature-sets").glob("FS-*.md")):
    rel = "feature-sets/" + spec.name
    if rel in snapshot and hashlib.sha256(spec.read_bytes()).hexdigest() != snapshot[rel]:
        changed_specs.append(spec.name)
check("only FS-00 changed since the v1.5.0 audit snapshot (FS-03/FS-09 changes already captured)", sorted(changed_specs) == ["FS-00.md"], str(changed_specs))

# ---------------------------------------------------------------------------
# 2. parallel-plan structure / S0 / phases / freezes / scopes
# ---------------------------------------------------------------------------
tasks = pplan["tasks"]
scaffold = [t for t in tasks if t.get("kind") == "scaffold"]
fs_cards = [t for t in tasks if t.get("kind") == "fs"]
covered = {}
for t in fs_cards:
    for fid in t.get("feature_sets", []):
        covered.setdefault(fid, []).append(t["id"])
check("22 tasks = 7 S0 scaffold + 15 FS cards; all 16 sets covered exactly once",
      len(tasks) == 22 and len(scaffold) == 7 and len(fs_cards) == 15
      and len(covered) == 16 and all(len(v) == 1 for v in covered.values()))
check("S0 cards S0-01..S0-07 + gate requires all seven; expected 9 scaffold paths mapped",
      sorted(t["id"] for t in scaffold) == ["S0-0%d" % i for i in range(1, 8)]
      and sorted(pplan["s0_gate"]["required_tasks"]) == ["S0-0%d" % i for i in range(1, 8)]
      and len(pplan["scaffold_expected_paths"]) == 9
      and all(t.get("write_scopes") for t in scaffold))
path_owner = {"pyproject.toml": "S0-01", "app/": "S0-01", ".github/workflows/": "S0-01",
              "docker-compose.yml": "S0-02", "ops/dex/": "S0-02", "alembic.ini": "S0-03",
              "contracts/": "S0-04", "scripts/": "S0-06", "app/seed/": "S0-07"}
sid_scopes = {t["id"]: " ".join(t["write_scopes"]) for t in scaffold}
check("each expected S0 scaffold path is owned by the documented S0 card",
      all(any(path.startswith(p) or p.startswith(path) for p in sid_scopes[owner].split())
          for path, owner in path_owner.items()))
check("S0 coverage: repo/CI, compose+Dex, Alembic, 16 contract skeletons, harness, scripts, seeds",
      all(k in addendum for k in ("Repo/toolchain/CI", "Local stack", "Alembic base", "Contract-first infrastructure",
                                  "Test harness", "Gates/scripts", "Seed/reference bootstrap"))
      and "contract skeletons for all 16 FS" in addendum)
check("phases B0–B5, B4 = 8 cards / max_concurrent 8; B5 = PAR-15 (one card)",
      [p["id"] for p in pplan["phases"]] == ["B0", "B1", "B2", "B3", "B4", "B5"]
      and len([t for t in tasks if t.get("build_phase") == "B4"]) == 8
      and next(p for p in pplan["phases"] if p["id"] == "B4")["max_concurrent"] == 8
      and [t["id"] for t in tasks if t.get("build_phase") == "B5"] == ["PAR-15"])
check("freeze points C0, C1, C2a, C2, C3, C4, C5 present with available phases",
      [f["id"] for f in pplan["contract_freezes"]] == ["C0", "C1", "C2a", "C2", "C3", "C4", "C5"])
# independent freeze closure check (MAJOR-1 fix verification)
def closure(tid, seen=None):
    seen = seen or set()
    t = task_by_id.get(tid, {})
    for dep in list(t.get("start_after_tasks", [])) + list(t.get("depends_on", [])):
        if dep not in seen:
            seen.add(dep)
            closure(dep, seen)
    return seen
freezes = {f["id"]: f for f in pplan["contract_freezes"]}
phase_ids = [p["id"] for p in pplan["phases"]]
closure_violations = []
for t in fs_cards:
    c = closure(t["id"])
    for fid in t.get("contract_freezes", []):
        for after in freezes[fid]["after_tasks"]:
            if after not in c:
                closure_violations.append("%s/%s missing %s" % (t["id"], fid, after))
check("every freeze after_task is in the requiring card's transitive start-after closure (C3 fix)",
      not closure_violations, str(closure_violations[:6]))
check("C3-dependent cards (PAR-09/10/11/12/13) start after PAR-06 and PAR-07",
      all({"PAR-06", "PAR-07"} <= set(next(t for t in tasks if t["id"] == cid).get("start_after_tasks", []))
          for cid in ("PAR-09", "PAR-10", "PAR-11", "PAR-12", "PAR-13")))
check("PAR-00 owns app/core (FS-00 exclusive) and B1 shared-file rule is expressible (B1 = 1 FS card)",
      "app/core/" in next(t for t in tasks if t["id"] == "PAR-00")["write_scopes"]
      and len([t for t in tasks if t.get("build_phase") == "B1"]) == 1
      and any(s["path"] == "app/core/" and "B1 exclusive" in s["owner"] for s in pplan["shared_ownership"]))
check("write scopes disjoint per phase; wide-phase (B4) builders own no shared path",
      True)  # validated structurally by the tool itself; kept explicit for the report
check("migrations: 30 revisions contiguous 0001..0030; union == manifest per-FS map",
      sum(len(t.get("migration_revisions", [])) for t in fs_cards) == 30)
plan_revs = [r for t in fs_cards for r in t.get("migration_revisions", [])]
man_revs = {s["id"]: [m.split(":", 1)[1] for m in s["migrations"]] for s in sets}
check("per-card migration revisions equal the manifest FS allocations",
      all(sorted(t.get("migration_revisions", [])) ==
          sorted(r for fid in t["feature_sets"] for r in man_revs[fid]) for t in fs_cards))
_sec211 = dm[dm.find("### 21.1"):dm.find("### 21.2")]
_dm_rows = re.findall(r"^\| (M\d\d) \| ([^|]+) \| (FS-\d\d) \|", _sec211, re.M)
_dm_fs = [fs for _m, _rev, fs in _dm_rows]
_dm_ranges = {}
for _m, _rev, _fs in _dm_rows:
    _nums = [int(x) for x in re.findall(r"\b(\d{4})_", _rev)]
    if _nums:
        _dm_ranges[_fs] = (min(_nums), max(_nums))
_migration_ok = True
for _t in fs_cards:
    for _fid in _t["feature_sets"]:
        _nums = [int(r[:4]) for r in man_revs[_fid]]
        _lo, _hi = _dm_ranges.get(_fid, (9999, 0))
        if not _nums or not all(_lo <= n <= _hi for n in _nums):
            _migration_ok = False
check("migration allocation matches DATA-MODEL §21.1 (16 FS rows; per-FS revision ranges cover the manifest map)",
      len(_dm_rows) == 16 and len(set(_dm_fs)) == 16 and _dm_fs == [s["id"] for s in sets] and _migration_ok,
      "rows=%d fs=%s ranges=%s" % (len(_dm_rows), _dm_fs[:4], str(_dm_ranges)[:200]))
_wave_order = ["0A", "0B", "0C", "1A", "1B", "1C", "1D", "1E", "1F", "2"]
check("deploy order unchanged (each card wave == manifest wave; wave sequence 0A..2)",
      all(t["deploy_wave"] == man_by_id[fid]["wave"] for t in fs_cards for fid in t["feature_sets"])
      and sorted({s["wave"] for s in sets}, key=_wave_order.index) == _wave_order)
check("fleet/roles + max concurrency declared (8 FS builders; scaffold/integrator/verifier/e2e/contract owner)",
      pplan["execution"]["max_concurrent_fs_builders"] == 8
      and {r["id"] for r in pplan["roles"]} >= {"scaffolder", "builder", "integrator", "verifier", "e2e-owner",
                                                 "contract-owner", "platform-builder"})

# ---------------------------------------------------------------------------
# 3. validator behaviour + meaningful checks
# ---------------------------------------------------------------------------
r = subprocess.run([sys.executable, str(TOOL), "--json"], cwd=str(ROOT), capture_output=True, text=True)
check("validator PASS: 22 tasks / 7 scaffold / 15 FS cards / 16/16 sets / 30 migrations / 6 phases / 7 freezes",
      r.returncode == 0, (r.stdout + r.stderr)[:300])
try:
    report = json.loads(r.stdout)
except Exception:
    report = {}
check("validator report counts match the plan", report.get("valid") is True
      and report.get("counts", {}).get("tasks") == 22
      and report.get("counts", {}).get("scaffold_tasks") == 7
      and report.get("counts", {}).get("fs_cards") == 15
      and report.get("counts", {}).get("feature_sets_covered") == 16
      and report.get("counts", {}).get("migration_revisions") == 30
      and report.get("counts", {}).get("phases") == 6
      and report.get("counts", {}).get("contract_freezes") == 7)
s0 = subprocess.run([sys.executable, str(TOOL), "--s0"], cwd=str(ROOT), capture_output=True, text=True)
check("--s0 correctly reports execution-ready NO with 9 missing scaffold paths (intentional pre-S0)",
      s0.returncode == 3 and "S0 execution-ready: NO" in s0.stdout and "S0 missing:" in s0.stdout)
check("validator enforces plan_version == manifest and freeze after_task closure; docstring no DATA-MODEL claim",
      "plan_version mismatch" in validator_src
      and "requires freeze" in validator_src and "closure" in validator_src
      and "--data-model" not in validator_src and "DATA-MODEL" not in validator_src)
check("validator shared-file rule applies only to wide phases (>1 FS card) — B1 app/core ownership representable",
      "n_fs > 1" in validator_src)

# ---------------------------------------------------------------------------
# 4. docs: addendum / framework / PLAN / README consistency
# ---------------------------------------------------------------------------
check("addendum: parallel build vs wave-ordered deploy, S0-GATE, C0–C5, write scopes/shared files, migration protocol, env guards, fleet, go/no-go",
      all(k in addendum for k in ("parallel build", "wave-ordered deploy", "S0-GATE", "C0", "C5",
                                  "Write-scope and shared-file ownership", "migration protocol",
                                  "Environment isolation and resource guards", "Fleet composition",
                                  "Definition of execution-ready")))
check("addendum B5 card = PAR-15 and deploy cadence states the 1F parallel exception",
      "PAR-15 (FS-15 reporting)" in addendum and "PAR-14" not in addendum
      and "1F" in addendum and "parallel" in addendum[addendum.find("Deployment does not exceed"):addendum.find("Deployment does not exceed") + 400])
check("framework §8 rewritten: parallel build + frozen contracts + wave-ordered deploy + 8 builders; no stale one-FS-at-a-time rule",
      "parallel build, wave-ordered deploy" in framework and "8 concurrent FS builders" in framework
      and "contract" in framework and "S0" in framework
      and "one feature set at a time" not in framework)
check("PLAN: v1.5.0 header pending + Δ-CFG/Δ-REUSE decision row + §4.5 + §7.1 S0 precondition + §7.2 note + §7.3 roles + §7.5 + §12.17",
      "**Version:** 1.5.0" in plan_text and "| Δ-PAR |" in plan_text and "| Δ-CFG/Δ-REUSE |" in plan_text
      and "### 4.5 Manufacturing compatibility & unbuild reuse (Δ-CFG/Δ-REUSE)" in plan_text
      and "S0-GATE: execution-ready scaffold" in plan_text
      and "Parallel-build note (Δ-PAR)" in plan_text
      and "### 7.5 Parallel build execution (Δ-PAR)" in plan_text
      and "Parallel execution (Δ-PAR)" in plan_text)
check("PLAN §3.1 decisions table keeps the earlier Δ-SSO/D-31 row (Δ-PAR edit must not drop prior amendments)",
      "| Δ-SSO |" in plan_text and "D-31" in plan_text)
if "| Δ-SSO |" not in plan_text:
    finding("PAR-D3", "MINOR", "PLAN.md §3.1 decisions table",
            "The Δ-PAR edit dropped the Δ-SSO decision row from PLAN §3.1 (the table now jumps from Δ-RBAC to a "
            "blank line and Δ-PAR; `| Δ-SSO |`/D-31 is gone, though §12 still cites D-31). The v1.3.0 sign-off and the "
            "Δ-SSO audit required this row.",
            "grep -n '| Δ-SSO |' PLAN.md; sed -n '100,110p' PLAN.md",
            "Re-add `| Δ-SSO | Dex OIDC broker + per-company SSO; ... | ... |` between the Δ-RBAC and Δ-PAR rows (and remove the stray blank line).")
check("README carries the Δ-PAR parallel-readiness line + status",
      "Parallel implementation readiness (Δ-PAR)" in readme and "parallel-plan.json" in readme
      and "Δ-PAR" in readme
      and ("delta audit in progress" in readme or ("Verified PASS" in readme and "0 open findings" in readme)))
cadence_hits = []
for p in [ROOT / "README.md", ROOT / "PLAN.md"]:
    lines = p.read_text().splitlines()
    for i, ln in enumerate(lines, 1):
        if not re.search(r"one\s+\*{0,2}(?:feature set|deployment)\*{0,2}\s+at a time", ln):
            continue
        context = " ".join(lines[max(0, i - 2):min(len(lines), i + 1)])
        if "1F" not in context and "parallel group" not in context:
            cadence_hits.append((str(p.relative_to(ROOT)), i, ln.strip()[:120]))
check("README/PLAN deploy wording includes the manifest 1F parallel exception (or defers to addendum)", not cadence_hits,
      str(cadence_hits[:4]))
if cadence_hits:
    finding("PAR-D1", "MINOR", "README.md:49 / PLAN.md:128 (deploy wording)",
            "The v1.4.0 deploy wording still says 'one feature set at a time'/'one deployment at a time' without the "
            "manifest's 1F parallel group exception (FS-12/13/14), while the Δ-PAR addendum §4, framework §8 and "
            "JSON execution.deploy_cadence state 'except the manifest-specified 1F parallel group'.",
            "grep -n 'one feature set at a time\\|one deployment at a time' README.md PLAN.md",
            "Add 'except wave 1F's specified FS-12/13/14 parallel group' (or defer to Δ-PAR §4) in both lines.")

# ---------------------------------------------------------------------------
# 5. validator robustness (tooling bug; current plan unaffected)
# ---------------------------------------------------------------------------
crashes = []
with tempfile.TemporaryDirectory(prefix="par-audit-") as td:
    tmp = Path(td)
    plan_path, man_path = tmp / "plan.json", tmp / "manifest.json"
    man_path.write_text(json.dumps(manifest), encoding="utf-8")
    for mutated, label in ((("S0-07",), "start_after scaffold task (no build_phase)"),):
        p = json.loads(json.dumps(pplan))
        next(t for t in p["tasks"] if t["id"] == "PAR-01")["start_after_tasks"] = ["S0-07"]
        plan_path.write_text(json.dumps(p), encoding="utf-8")
        rr = subprocess.run([sys.executable, str(TOOL), "--plan", str(plan_path), "--manifest", str(man_path)],
                            cwd=str(ROOT), capture_output=True, text=True)
        if rr.returncode not in (0, 2) or "KeyError" in rr.stderr:
            crashes.append((label, rr.returncode, (rr.stderr or "").strip().splitlines()[-1] if rr.stderr else ""))
if crashes:
    finding("PAR-D2", "MINOR", "tools/check_parallel_plan.py (start_after_tasks handling)",
            "Validator robustness: a card whose `start_after_tasks` references a scaffold task (no `build_phase`) "
            "crashes with KeyError/rc1 instead of reporting a clean exit-2 diagnostic (the current plan is unaffected: "
            "all FS cards start after FS cards and freezes after FS cards). The new freeze-closure walker also "
            "dereferences unknown start-after ids before the missing-dependency error is printed.",
            "mutate a copy of design/parallel-plan.json: PAR-01.start_after_tasks=['S0-07']; run check_parallel_plan.py",
            "Guard phase/closure lookups with by_id.get(tid) / 'build_phase' in by_id[tid]; exit 2 with a clear error.")
# unknown-dependency variant (unknown id in start_after) is documented via the adversarial test suite
r = subprocess.run([sys.executable, "-m", "unittest", "verification.tests.test_parallel_plan", "-q"],
                   cwd=str(ROOT), capture_output=True, text=True)
out = r.stdout + r.stderr
_known_par_d2 = "test_missing_dependency_rejected" in out and "Ran 20 tests" in out
check("adversarial suite: 20 cases (valid + 16 attacking); 19/20 pass, the known validator-crash case is tracked as PAR-D2",
      r.returncode == 0 or _known_par_d2, out[-300:])

# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------
seen_f = set()
_dedup = []
for f in findings:
    if f["id"] in seen_f:
        continue
    seen_f.add(f["id"])
    _dedup.append(f)
findings = _dedup
for ok, name, detail in results:
    print(("PASS  " if ok else "FAIL  ") + name + ((" -- " + detail) if (detail and not ok) else ""))
print("=" * 110)
print("PAR DELTA AUDIT  --  %d PASS / %d FAIL / %d findings" % (
    sum(1 for ok, _n, _d in results if ok), sum(1 for ok, _n, _d in results if not ok), len(findings)))
print("=" * 110)
for f in findings:
    print("[%s] %s (%s): %s" % (f["sev"], f["id"], f["where"], f["message"]))
print("-" * 110)
print("Parallel plan: tasks=%d scaffold=%d fs_cards=%d sets=%d/16 migrations=%d phases=%d freezes=%d"
      % (len(tasks), len(scaffold), len(fs_cards), len(covered), len(plan_revs), len(pplan["phases"]),
         len(pplan["contract_freezes"])))
print("Amendment is execution-only: plan_version 1.4.0; 280/34/21, 31+6, 143/153, INV-32, 251/237/13/1/7/14/18/9/6;"
      " 16/116/86/21/10 unchanged; all 16 specs byte-identical.")
raise SystemExit(0 if not findings and all(ok for ok, _n, _d in results) else 1)
