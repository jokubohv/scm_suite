#!/usr/bin/env python3
"""Unit + adversarial tests for the Δ-PAR parallel-plan validator.

Uses temp fixtures (self-contained minimal plan + manifest); each adversarial
mutation must make `tools/check_parallel_plan.py` exit 2. The validator itself is
Lead-authored and is NOT modified by these tests.

Run from inventory-platform/:

    python3 -m unittest verification/tests/test_parallel_plan.py -v
"""

from __future__ import annotations

import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]      # inventory-platform/
TOOL = ROOT / "tools" / "check_parallel_plan.py"


def base_manifest() -> dict:
    return {
        "plan_version": "1.4.0",
        "feature_sets": [
            {"id": "FS-A", "wave": "0A", "migrations": ["0001_a"]},
            {"id": "FS-B", "wave": "0B", "migrations": ["0002_b"]},
        ],
    }


def base_plan() -> dict:
    s0_ids = ["S0-01", "S0-02", "S0-03", "S0-04", "S0-05", "S0-06", "S0-07"]
    s0_tasks = []
    for i, tid in enumerate(s0_ids, start=1):
        s0_tasks.append({
            "id": tid, "name": "scaffold %s" % tid, "kind": "scaffold",
            "owner_role": "scaffolder", "depends_on": (["S0-01"] if tid != "S0-01" else []),
            "write_scopes": ["scaffold/%s/" % tid],
        })
    tasks = s0_tasks + [
        {"id": "PAR-00", "name": "FS-A card", "kind": "fs", "owner_role": "builder",
         "feature_sets": ["FS-A"], "build_phase": "B1", "deploy_wave": "0A",
         "depends_on": [], "start_after_tasks": [], "contract_freezes": [],
         "migration_revisions": ["0001_a"], "write_scopes": ["app/a/", "app/contracts/a/"],
         "exit_checks": ["pytest app/tests/a -q"]},
        {"id": "PAR-01", "name": "FS-B card", "kind": "fs", "owner_role": "builder",
         "feature_sets": ["FS-B"], "build_phase": "B1", "deploy_wave": "0B",
         "depends_on": [], "start_after_tasks": ["PAR-00"], "contract_freezes": ["C0"],
         "migration_revisions": ["0002_b"], "write_scopes": ["app/b/", "app/contracts/b/"],
         "exit_checks": ["pytest app/tests/b -q"]},
    ]
    return {
        "schema_version": "1.0.0",
        "plan_version": "1.4.0",
        "strategy": "fixture",
        "execution": {"max_concurrent_fs_builders": 8, "max_durable_teammates": 8,
                      "contract_first": True, "deploy_cadence": "wave order"},
        "roles": [{"id": "scaffolder", "count": 1, "owns": []},
                  {"id": "builder", "count": 8, "owns": []}],
        "shared_ownership": [{"path": "PLAN.md", "owner": "lead"},
                             {"path": "design/", "owner": "lead"}],
        "contract_freezes": [
            {"id": "C0", "available_phase": "B1", "after_tasks": ["PAR-00"],
             "artifacts": ["a"], "evidence": ["tests"]},
        ],
        "phases": [
            {"id": "B0", "name": "scaffold", "entry": [], "exit": ["S0-GATE"],
             "max_concurrent": 3, "tasks": s0_ids},
            {"id": "B1", "name": "build", "entry": ["C0"], "exit": ["done"],
             "max_concurrent": 8, "tasks": ["PAR-00", "PAR-01"]},
        ],
        "s0_gate": {"required_tasks": s0_ids, "checks": ["all scaffold checks green"]},
        "scaffold_expected_paths": ["pyproject.toml"],
        "tasks": tasks,
    }


class ParallelPlanTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="par-test-")
        self.tmp = Path(self._tmp.name)
        self.plan_path = self.tmp / "parallel-plan.json"
        self.manifest_path = self.tmp / "feature-sets.json"
        # validator no longer accepts --data-model (v1.4.0 final)

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, plan: dict, manifest: dict | None = None) -> None:
        self.plan_path.write_text(json.dumps(plan), encoding="utf-8")
        self.manifest_path.write_text(json.dumps(manifest or base_manifest()), encoding="utf-8")

    def run_tool(self, *extra: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(TOOL), "--plan", str(self.plan_path),
             "--manifest", str(self.manifest_path), *extra],
            cwd=str(self.tmp), capture_output=True, text=True, timeout=120,
        )

    @staticmethod
    def task(plan: dict, tid: str) -> dict:
        return next(t for t in plan["tasks"] if t["id"] == tid)


class TestParallelPlanValid(ParallelPlanTestBase):
    def test_valid_fixture_passes(self):
        self.write(base_plan())
        r = self.run_tool()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("PARALLEL PLAN OK", r.stdout)
        self.assertIn("tasks=9", r.stdout)

    def test_json_report_counts(self):
        self.write(base_plan())
        r = self.run_tool("--json")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        report = json.loads(r.stdout)
        self.assertTrue(report["valid"])
        self.assertEqual(report["counts"]["tasks"], 9)
        self.assertEqual(report["counts"]["scaffold_tasks"], 7)
        self.assertEqual(report["counts"]["fs_cards"], 2)
        self.assertEqual(report["counts"]["feature_sets_covered"], 2)
        self.assertEqual(report["counts"]["migration_revisions"], 2)
        self.assertEqual(report["counts"]["phases"], 2)
        self.assertEqual(report["counts"]["contract_freezes"], 1)

    def test_real_plan_passes_with_expected_counts(self):
        r = subprocess.run([sys.executable, str(TOOL)], cwd=str(ROOT),
                           capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("tasks=22", r.stdout)
        self.assertIn("scaffold=7", r.stdout)
        self.assertIn("fs_cards=15", r.stdout)
        self.assertIn("sets_covered=16/16", r.stdout)
        self.assertIn("migrations=30", r.stdout)
        self.assertIn("phases=6", r.stdout)
        self.assertIn("freezes=7", r.stdout)

    def test_real_s0_reports_not_execution_ready(self):
        r = subprocess.run([sys.executable, str(TOOL), "--s0"], cwd=str(ROOT),
                           capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        self.assertIn("S0 execution-ready: NO", r.stdout)
        self.assertIn("S0 missing:", r.stdout)


class TestParallelPlanAdversarial(ParallelPlanTestBase):
    def test_duplicate_task_id_rejected(self):
        plan = base_plan()
        plan["tasks"].append(copy.deepcopy(self.task(plan, "S0-01")))
        self.write(plan)
        r = self.run_tool()
        self.assertEqual(r.returncode, 2)
        self.assertIn("duplicate task id", r.stdout)

    def test_missing_feature_set_coverage_rejected(self):
        plan = base_plan()
        plan["tasks"] = [t for t in plan["tasks"] if t["id"] != "PAR-01"]
        plan["phases"][1]["tasks"] = ["PAR-00"]
        self.write(plan)
        r = self.run_tool()
        self.assertEqual(r.returncode, 2)
        self.assertIn("FS-B has no build card", r.stdout)

    def test_multiple_feature_set_coverage_rejected(self):
        plan = base_plan()
        self.task(plan, "PAR-01")["feature_sets"] = ["FS-B", "FS-A"]
        self.task(plan, "PAR-01")["migration_revisions"] = ["0002_b", "0001_a"]
        self.write(plan)
        r = self.run_tool()
        self.assertEqual(r.returncode, 2)
        self.assertIn("covered by multiple cards", r.stdout)

    def test_write_scope_overlap_rejected(self):
        plan = base_plan()
        self.task(plan, "PAR-01")["write_scopes"] = ["app/a/", "app/contracts/b/"]
        self.write(plan)
        r = self.run_tool()
        self.assertEqual(r.returncode, 2)
        self.assertIn("write-scope overlap", r.stdout)

    def test_migration_gap_rejected(self):
        plan = base_plan()
        manifest = base_manifest()
        self.task(plan, "PAR-01")["migration_revisions"] = ["0003_b"]
        manifest["feature_sets"][1]["migrations"] = ["0003_b"]
        self.write(plan, manifest)
        r = self.run_tool()
        self.assertEqual(r.returncode, 2)
        self.assertIn("not contiguous", r.stdout)

    def test_duplicate_migration_revision_rejected(self):
        plan = base_plan()
        manifest = base_manifest()
        self.task(plan, "PAR-01")["migration_revisions"] = ["0001_a"]
        manifest["feature_sets"][1]["migrations"] = ["0001_a"]
        self.write(plan, manifest)
        r = self.run_tool()
        self.assertEqual(r.returncode, 2)
        self.assertIn("duplicate migration revisions", r.stdout)

    def test_wrong_card_migration_rejected(self):
        plan = base_plan()
        self.task(plan, "PAR-01")["migration_revisions"] = ["0001_a"]
        self.write(plan)
        r = self.run_tool()
        self.assertEqual(r.returncode, 2)
        self.assertIn("migrations", r.stdout)
        self.assertIn("!= manifest", r.stdout)

    def test_dependency_cycle_rejected(self):
        plan = base_plan()
        self.task(plan, "PAR-00")["start_after_tasks"] = ["PAR-01"]
        self.task(plan, "PAR-01")["start_after_tasks"] = ["PAR-00"]
        self.write(plan)
        r = self.run_tool()
        self.assertEqual(r.returncode, 2)
        self.assertIn("dependency cycle", r.stdout)

    def test_wrong_deploy_wave_rejected(self):
        plan = base_plan()
        self.task(plan, "PAR-01")["deploy_wave"] = "0A"
        self.write(plan)
        r = self.run_tool()
        self.assertEqual(r.returncode, 2)
        self.assertIn("deploy_wave", r.stdout)
        self.assertIn("manifest wave", r.stdout)

    def test_freeze_unavailable_before_phase_rejected(self):
        plan = base_plan()
        plan["contract_freezes"].append(
            {"id": "C1", "available_phase": "B2", "after_tasks": ["PAR-00"],
             "artifacts": ["b"], "evidence": ["tests"]})
        plan["phases"].append({"id": "B2", "name": "later", "entry": [], "exit": ["done"],
                               "max_concurrent": 1, "tasks": []})
        self.task(plan, "PAR-00")["contract_freezes"] = ["C1"]
        self.write(plan)
        r = self.run_tool()
        self.assertEqual(r.returncode, 2)
        self.assertIn("starts before freeze C1", r.stdout)

    def test_concurrency_over_ceiling_rejected(self):
        plan = base_plan()
        plan["phases"][1]["max_concurrent"] = 9
        self.write(plan)
        r = self.run_tool()
        self.assertEqual(r.returncode, 2)
        self.assertIn("declared ceiling", r.stdout)

    def test_unknown_role_rejected(self):
        plan = base_plan()
        self.task(plan, "PAR-01")["owner_role"] = "ghost"
        self.write(plan)
        r = self.run_tool()
        self.assertEqual(r.returncode, 2)
        self.assertIn("unknown owner_role", r.stdout)

    def test_missing_dependency_rejected(self):
        plan = base_plan()
        self.task(plan, "PAR-01")["start_after_tasks"] = ["PAR-99"]
        self.write(plan)
        r = self.run_tool()
        self.assertEqual(r.returncode, 2)
        self.assertIn("does not exist", r.stdout)

    def test_plan_version_mismatch_rejected(self):
        plan = base_plan()
        plan["plan_version"] = "1.4.1"
        self.write(plan)
        r = self.run_tool()
        self.assertEqual(r.returncode, 2)
        self.assertIn("plan_version mismatch", r.stdout)

    def test_freeze_prerequisite_missing_from_start_after_closure_rejected(self):
        plan = base_plan()
        self.task(plan, "PAR-01")["start_after_tasks"] = []
        self.write(plan)
        r = self.run_tool()
        self.assertEqual(r.returncode, 2)
        self.assertIn("does not start after its prerequisite PAR-00", r.stdout)

    def test_shared_path_overlap_in_wide_phase_rejected(self):
        plan = base_plan()
        self.task(plan, "PAR-01")["write_scopes"] = ["design/b/"]
        self.write(plan)
        r = self.run_tool()
        self.assertEqual(r.returncode, 2)
        self.assertIn("overlaps shared path", r.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
