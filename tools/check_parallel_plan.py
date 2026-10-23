#!/usr/bin/env python3
"""Validate the Δ-PAR parallel implementation plan against the authoritative plan artifacts.

Usage:
  python tools/check_parallel_plan.py                 # validate the plan graph (exit 0/2)
  python tools/check_parallel_plan.py --s0            # also check S0 scaffold readiness on disk
  python tools/check_parallel_plan.py --json          # machine-readable report on stdout

Checks:
  - schema/roles/phases/tasks, unique ids, dependency DAG (no cycles)
  - S0 scaffold cards + gate checklist present
  - every manifest feature set covered exactly once by a build card
  - plan_version matches the manifest
  - deploy wave of each card equals the manifest wave
  - migration revisions: union == manifest migrations, per-card match, unique, contiguous 0001..N
  - per-phase write-scope disjointness (path-prefix aware)
  - contract-freeze availability + every freeze after_task in the card's transitive start-after set
  - shared-file ownership entries usable (no wide-phase builder owns a shared path)
  - phase concurrency <= declared maxima (8 wide-build ceiling)
Exit codes: 0 plan valid; 2 invalid; 3 --s0 requested but scaffold not execution-ready.
Stdlib only.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PLAN = ROOT / "design" / "parallel-plan.json"
DEFAULT_MANIFEST = ROOT / "design" / "feature-sets.json"


def fail(errors: list[str], msg: str) -> None:
    errors.append(msg)


def overlaps(a: str, b: str) -> bool:
    """True if two workspace-relative path prefixes overlap by prefix relationship."""
    a = a.rstrip("/")
    b = b.rstrip("/")
    return a == b or a.startswith(b + "/") or b.startswith(a + "/")


def phase_index(phases: list[dict], pid: str) -> int:
    for i, p in enumerate(phases):
        if p["id"] == pid:
            return i
    return -1


def manifest_migrations(manifest: dict) -> dict[str, list[str]]:
    out = {}
    for fs in manifest["feature_sets"]:
        revs = []
        for entry in fs.get("migrations", []):
            revs.append(entry.split(":", 1)[1] if ":" in entry else entry)
        out[fs["id"]] = revs
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--plan", default=str(DEFAULT_PLAN))
    ap.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    ap.add_argument("--s0", action="store_true", help="check scaffold paths on disk")
    ap.add_argument("--json", action="store_true", dest="as_json")
    args = ap.parse_args()

    errors: list[str] = []
    warnings: list[str] = []
    plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))
    manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    if plan.get("plan_version") != manifest.get("plan_version"):
        fail(errors, f"plan_version mismatch: parallel-plan={plan.get('plan_version')} manifest={manifest.get('plan_version')}")
    tasks = plan.get("tasks", [])
    by_id = {}
    for t in tasks:
        if t.get("id") in by_id:
            fail(errors, f"duplicate task id {t.get('id')}")
        by_id[t.get("id")] = t

    # ---- roles, phases, execution ----
    roles = {r["id"] for r in plan.get("roles", [])}
    for t in tasks:
        if t.get("owner_role") not in roles and t.get("kind") != "scaffold":
            fail(errors, f"{t['id']}: unknown owner_role {t.get('owner_role')}")
    ph = plan.get("phases", [])
    phase_ids = [p["id"] for p in ph]
    if len(set(phase_ids)) != len(phase_ids):
        fail(errors, "duplicate phase ids")
    assigned: dict[str, str] = {}
    for p in ph:
        for tid in p.get("tasks", []):
            if tid not in by_id:
                fail(errors, f"phase {p['id']} references unknown task {tid}")
            assigned[tid] = p["id"]
    for t in tasks:
        if t["id"] not in assigned:
            fail(errors, f"task {t['id']} is not in any phase")
    max_builders = plan.get("execution", {}).get("max_concurrent_fs_builders", 8)
    for p in ph:
        fs_tasks = [t for t in p.get("tasks", []) if by_id.get(t, {}).get("kind") == "fs"]
        if p["id"] != "B0" and len(fs_tasks) > p.get("max_concurrent", 10**9):
            fail(errors, f"phase {p['id']}: {len(fs_tasks)} FS cards > max_concurrent {p.get('max_concurrent')}")
        if p.get("max_concurrent", 0) > max_builders:
            fail(errors, f"phase {p['id']}: max_concurrent {p.get('max_concurrent')} > declared ceiling {max_builders}")

    # ---- S0 ----
    s0 = [t for t in tasks if t["kind"] == "scaffold"]
    gate = plan.get("s0_gate", {})
    for tid in gate.get("required_tasks", []):
        if tid not in by_id or by_id[tid]["kind"] != "scaffold":
            fail(errors, f"S0 gate requires missing scaffold task {tid}")
    if not gate.get("checks"):
        fail(errors, "s0_gate.checks is empty")
    for t in s0:
        if not t.get("write_scopes"):
            fail(errors, f"{t['id']}: scaffold task has no write scopes")

    # ---- dependency graph over tasks ----
    def deps(t: dict) -> list[str]:
        return list(t.get("depends_on", [])) + list(t.get("start_after_tasks", []))

    graph = {t["id"]: deps(t) for t in tasks}
    for tid, ds in graph.items():
        for d in ds:
            if d not in by_id:
                fail(errors, f"{tid}: dependency {d} does not exist")
    color: dict[str, int] = {}

    def dfs(node: str, stack: list[str]) -> None:
        color[node] = 1
        for d in graph.get(node, []):
            if color.get(d, 0) == 1:
                fail(errors, f"dependency cycle: {' -> '.join(stack + [node, d])}")
                return
            if color.get(d, 0) == 0:
                dfs(d, stack + [node])
                if errors:
                    return
        color[node] = 2

    for n in graph:
        if color.get(n, 0) == 0 and not errors:
            dfs(n, [])

    # ---- feature-set coverage / waves / migrations ----
    fs_meta = {fs["id"]: fs for fs in manifest["feature_sets"]}
    man_revs = manifest_migrations(manifest)
    covered: dict[str, list[str]] = {}
    for t in tasks:
        if t.get("kind") != "fs":
            continue
        for fid in t.get("feature_sets", []):
            covered.setdefault(fid, []).append(t["id"])
    for fid in fs_meta:
        if fid not in covered:
            fail(errors, f"feature set {fid} has no build card")
        elif len(covered[fid]) > 1:
            fail(errors, f"feature set {fid} covered by multiple cards {covered[fid]}")
    for fid in covered:
        if fid not in fs_meta:
            fail(errors, f"build card references unknown feature set {fid}")

    plan_revs: list[str] = []
    for t in tasks:
        if t.get("kind") != "fs":
            continue
        revs = t.get("migration_revisions", [])
        expect = [r for fid in t["feature_sets"] for r in man_revs[fid]]
        if sorted(revs) != sorted(expect):
            fail(errors, f"{t['id']}: migrations {revs} != manifest {expect}")
        plan_revs.extend(revs)
        wave = t.get("deploy_wave")
        for fid in t["feature_sets"]:
            if wave != fs_meta[fid]["wave"]:
                fail(errors, f"{t['id']}: deploy_wave {wave} != manifest wave {fs_meta[fid]['wave']} for {fid}")
        for tid in t.get("start_after_tasks", []):
            other = by_id.get(tid)
            if not other or "build_phase" not in other:
                continue  # missing/invalid deps are reported by the dependency-graph check
            if phase_index(ph, t["build_phase"]) < phase_index(ph, other["build_phase"]):
                fail(errors, f"{t['id']}: starts before {tid} in an earlier phase")
    if len(plan_revs) != len(set(plan_revs)):
        fail(errors, "duplicate migration revisions across cards")
    nums = []
    for r in plan_revs:
        m = re.match(r"^(\d{4})_", r)
        if not m:
            fail(errors, f"migration revision without 4-digit prefix: {r}")
        else:
            nums.append(int(m.group(1)))
    if sorted(nums) != list(range(1, len(nums) + 1)):
        fail(errors, f"migration revisions are not contiguous 0001..{len(nums):04d}")

    # ---- contract freezes + phase entries ----
    freezes = {f["id"]: f for f in plan.get("contract_freezes", [])}
    for fid, f in freezes.items():
        for tid in f.get("after_tasks", []):
            if tid not in by_id:
                fail(errors, f"freeze {fid}: unknown after_task {tid}")
        if f.get("available_phase") not in phase_ids:
            fail(errors, f"freeze {fid}: unknown available_phase {f.get('available_phase')}")
    for t in tasks:
        if t.get("kind") != "fs":
            continue
        ti = phase_index(ph, t["build_phase"])
        for fid in t.get("contract_freezes", []):
            if fid not in freezes:
                fail(errors, f"{t['id']}: unknown contract freeze {fid}")
            elif phase_index(ph, freezes[fid]["available_phase"]) > ti:
                fail(errors, f"{t['id']}: phase {t['build_phase']} starts before freeze {fid} is available")
    for p in ph:
        for entry in p.get("entry", []):
            if entry == "S0-GATE":
                continue
            if entry not in freezes:
                fail(errors, f"phase {p['id']}: unknown entry {entry}")
            elif phase_index(ph, freezes[entry]["available_phase"]) > phase_index(ph, p["id"]):
                fail(errors, f"phase {p['id']}: entry {entry} is not available at phase start")

    # freeze after_tasks must be in the card's transitive start-after/dependency closure
    def closure(tid: str, seen: set[str] | None = None) -> set[str]:
        seen = seen or set()
        node = by_id.get(tid)
        if not node:
            return seen
        for dep in list(node.get("start_after_tasks", [])) + list(node.get("depends_on", [])):
            if dep not in seen:
                seen.add(dep)
                closure(dep, seen)
        return seen
    for t in tasks:
        if t.get("kind") != "fs":
            continue
        c = closure(t["id"])
        for fid in t.get("contract_freezes", []):
            f = freezes.get(fid)
            if not f:
                continue
            for after in f.get("after_tasks", []):
                if after not in c:
                    fail(errors, f"{t['id']}: requires freeze {fid} but does not start after its prerequisite {after}")

    # ---- write-scope disjointness per phase ----
    for p in ph:
        ts = [by_id[t] for t in p.get("tasks", []) if t in by_id]
        for i in range(len(ts)):
            for j in range(i + 1, len(ts)):
                for a in ts[i].get("write_scopes", []):
                    for b in ts[j].get("write_scopes", []):
                        if overlaps(a, b):
                            fail(errors, f"phase {p['id']}: write-scope overlap {ts[i]['id']} <{a}> vs {ts[j]['id']} <{b}>")

    # ---- shared ownership ----
    shared = plan.get("shared_ownership", [])
    if not shared:
        fail(errors, "shared_ownership is empty")
    for s in shared:
        if not s.get("path") or not s.get("owner"):
            fail(errors, "shared_ownership entry missing path/owner")
    wide = []
    for p in ph:
        n_fs = sum(1 for t in p.get("tasks", []) if by_id.get(t, {}).get("kind") == "fs")
        if n_fs > 1:
            wide.append(p["id"])
    for t in tasks:
        if t.get("kind") != "fs" or t["build_phase"] not in wide:
            continue
        for sc in t.get("write_scopes", []):
            for s in shared:
                if overlaps(sc, s["path"]):
                    fail(errors, f"{t['id']}: builder write scope {sc} overlaps shared path {s['path']}")

    # ---- report ----
    report = {
        "plan_version": plan.get("plan_version"),
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
        "counts": {
            "tasks": len(tasks),
            "scaffold_tasks": len(s0),
            "fs_cards": sum(1 for t in tasks if t.get("kind") == "fs"),
            "feature_sets_covered": len(covered),
            "migration_revisions": len(plan_revs),
            "phases": len(ph),
            "contract_freezes": len(freezes),
            "wide_build_concurrency": [p["max_concurrent"] for p in ph if p["id"] == "B4"],
        },
        "deployment_order": [fs["id"] for fs in sorted(manifest["feature_sets"],
                            key=lambda x: (["0A", "0B", "0C", "1A", "1B", "1C", "1D", "1E", "1F", "2"].index(x["wave"]), x["id"]))],
    }

    s0_report = None
    if args.s0:
        expected = plan.get("scaffold_expected_paths", [])
        present = [p for p in expected if (ROOT / p).exists()]
        missing = [p for p in expected if p not in present]
        s0_report = {"execution_ready": not missing, "present": present, "missing": missing,
                     "note": "S0-GATE requires all expected paths plus the s0_gate checks; run S0 tasks first."}
        report["s0"] = s0_report
        if missing and not errors:
            report["warnings"].append("S0 scaffold is not yet present — expected before FS-00 starts.")

    if args.as_json:
        print(json.dumps(report, indent=2))
    else:
        print(f"PARALLEL PLAN {'OK' if not errors else 'FAILED'}: plan_version={report['plan_version']} "
              f"tasks={len(tasks)} scaffold={len(s0)} fs_cards={report['counts']['fs_cards']} "
              f"sets_covered={len(covered)}/{len(fs_meta)} migrations={len(plan_revs)} phases={len(ph)} freezes={len(freezes)}")
        print("  deployment order remains: " + " -> ".join(report["deployment_order"]))
        print(f"  wide-build concurrency: B4 max {plan.get('execution', {}).get('max_concurrent_fs_builders')} builders")
        if warnings:
            for w in warnings:
                print("  WARN:", w)
        for e in errors:
            print("  ERROR:", e)
        if s0_report:
            print(f"  S0 execution-ready: {'YES' if s0_report['execution_ready'] else 'NO'}")
            if s0_report["missing"]:
                print("  S0 missing:", ", ".join(s0_report["missing"]))
    if errors:
        return 2
    if args.s0 and s0_report and not s0_report["execution_ready"]:
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
