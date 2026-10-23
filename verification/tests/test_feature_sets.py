#!/usr/bin/env python3
"""Unit + adversarial tests for tools/feature_sets.py.

Stdlib only (unittest). Fixtures are generated at test time in temp dirs; the
real plan artifacts are never mutated. Run from inventory-platform/:

    python3 -m unittest verification/tests/test_feature_sets.py -v
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TOOL = Path(__file__).resolve().parents[2] / "tools" / "feature_sets.py"
BASE = "https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/book_N2249433.html"


def make_manifest(sets, schema_version="1.0", plan_version="1.0.0"):
    return {
        "schema_version": schema_version,
        "plan_version": plan_version,
        "feature_sets": sets,
    }


def make_set(
    set_id,
    *,
    contract=None,
    version="1.0.0",
    deps=None,
    phase=1,
    wave="0A",
    backend_only=True,
):
    provides = [{"contract": contract or ("svc." + set_id.lower().replace("-", "_")), "version": version}]
    entry = [
        "python tools/feature_sets.py validate",
        "python tools/feature_sets.py check-env --set %s --env $ENV" % set_id,
    ]
    exit_checks = [
        "docker compose run --rm api alembic upgrade head",
        "pytest app/tests/integration/%s -q" % set_id.lower(),
        "python tools/feature_sets.py record --set %s --env $ENV --status verified" % set_id,
    ]
    return {
        "id": set_id,
        "name": "Test set %s" % set_id,
        "phase": phase,
        "wave": wave,
        "backend_only": backend_only,
        "summary": "Test fixture feature set.",
        "source_docs": [BASE],
        "depends_on": deps or [],
        "provides": provides,
        "modules": ["app/modules/%s" % set_id.lower()],
        "migrations": ["alembic:0001_%s" % set_id.lower()],
        "flags": ["test.%s.enabled" % set_id.lower().replace("-", "_")],
        "entry_checks": entry,
        "exit_checks": exit_checks,
        "acceptance": ["Given a fixture, when checked, then the gate behaves."],
        "rollback": "flag off; forward-fix; migrations additive.",
    }


def dep(set_id, contract, version=">=1.0.0 <2.0.0"):
    return {"id": set_id, "contract": contract, "version": version}


class ToolTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="fs-tool-test-")
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    # -- helpers ---------------------------------------------------------
    def write_json(self, name, payload):
        path = self.tmp / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return path

    def write_text(self, name, text):
        path = self.tmp / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def run_tool(self, *args):
        return subprocess.run(
            [sys.executable, str(TOOL), *args],
            cwd=str(self.tmp),
            capture_output=True,
            text=True,
            timeout=120,
        )

    def run_manifest(self, sets, *args):
        manifest = self.write_json("design/feature-sets.json", make_manifest(sets))
        return self.run_tool(args[0], "--manifest", str(manifest), *args[1:])

    # -- valid fixture ---------------------------------------------------
    def valid_sets(self):
        return [
            make_set("FS-00", contract="platform.core", wave="0A"),
            make_set("FS-01", contract="item.master", wave="0B", deps=[dep("FS-00", "platform.core")]),
            make_set(
                "FS-02",
                contract="pricing.engine",
                wave="0B",
                deps=[dep("FS-00", "platform.core"), dep("FS-01", "item.master")],
            ),
        ]


class TestValidate(ToolTestCase):
    def test_valid_manifest_passes(self):
        result = self.run_manifest(self.valid_sets(), "validate")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("VALIDATE OK", result.stdout)
        self.assertIn("3 feature sets", result.stdout)
        self.assertIn("3 dependency edges", result.stdout)

    def test_cycle_rejected_with_path(self):
        sets = self.valid_sets()
        sets[1]["depends_on"] = [dep("FS-02", "pricing.engine")]
        result = self.run_manifest(sets, "validate")
        self.assertEqual(result.returncode, 2)
        self.assertIn("E-CYCLE", result.stderr)
        self.assertIn("FS-01", result.stderr)
        self.assertIn("FS-02", result.stderr)
        # cycle path rendered with arrows
        self.assertIn("->", result.stderr)

    def test_dangling_dependency_rejected(self):
        sets = self.valid_sets()
        sets[1]["depends_on"] = [dep("FS-99", "platform.core")]
        result = self.run_manifest(sets, "validate")
        self.assertEqual(result.returncode, 2)
        self.assertIn("E-DEP-MISSING", result.stderr)
        self.assertIn("FS-99", result.stderr)

    def test_self_dependency_rejected(self):
        sets = self.valid_sets()
        sets[1]["depends_on"] = [dep("FS-01", "item.master")]
        result = self.run_manifest(sets, "validate")
        self.assertEqual(result.returncode, 2)
        self.assertIn("E-DEP-SELF", result.stderr)

    def test_phase_inversion_rejected(self):
        sets = self.valid_sets()
        sets[1]["phase"] = 1
        sets[0]["phase"] = 2
        result = self.run_manifest(sets, "validate")
        self.assertEqual(result.returncode, 2)
        self.assertIn("E-PHASE-MONO", result.stderr)

    def test_wave_inversion_rejected(self):
        sets = self.valid_sets()
        sets[0]["wave"] = "1A"
        sets[1]["wave"] = "0B"
        result = self.run_manifest(sets, "validate")
        self.assertEqual(result.returncode, 2)
        self.assertIn("E-WAVE-MONO", result.stderr)

    def test_duplicate_contract_rejected(self):
        sets = self.valid_sets()
        sets[2]["provides"] = [{"contract": "item.master", "version": "1.0.0"}]
        result = self.run_manifest(sets, "validate")
        self.assertEqual(result.returncode, 2)
        self.assertIn("E-CONTRACT-DUP", result.stderr)

    def test_missing_acceptance_rejected(self):
        sets = self.valid_sets()
        sets[1]["acceptance"] = []
        result = self.run_manifest(sets, "validate")
        self.assertEqual(result.returncode, 2)
        self.assertIn("E-FIELD", result.stderr)
        self.assertIn("acceptance", result.stderr)

    def test_missing_backend_only_rejected(self):
        sets = self.valid_sets()
        sets[1]["backend_only"] = False
        result = self.run_manifest(sets, "validate")
        self.assertEqual(result.returncode, 2)
        self.assertIn("E-BACKEND-ONLY", result.stderr)

    def test_bad_semver_range_rejected(self):
        sets = self.valid_sets()
        sets[1]["depends_on"] = [dep("FS-00", "platform.core", ">=banana")]
        result = self.run_manifest(sets, "validate")
        self.assertEqual(result.returncode, 2)
        self.assertIn("E-SEMVER-PARSE", result.stderr)

    def test_unsatisfiable_semver_range_rejected(self):
        sets = self.valid_sets()
        sets[1]["depends_on"] = [dep("FS-00", "platform.core", ">=2.0.0 <1.0.0")]
        result = self.run_manifest(sets, "validate")
        self.assertEqual(result.returncode, 2)
        self.assertIn("E-SEMVER-UNSAT", result.stderr)

    def test_provided_version_outside_range_rejected(self):
        sets = self.valid_sets()
        sets[1]["depends_on"] = [dep("FS-00", "platform.core", ">=2.0.0 <3.0.0")]
        result = self.run_manifest(sets, "validate")
        self.assertEqual(result.returncode, 2)
        self.assertIn("E-DEP-RANGE", result.stderr)

    def test_dep_contract_not_provided_by_dep_rejected(self):
        sets = self.valid_sets()
        sets[1]["depends_on"] = [dep("FS-00", "not.provided")]
        result = self.run_manifest(sets, "validate")
        self.assertEqual(result.returncode, 2)
        self.assertIn("E-DEP-CONTRACT", result.stderr)

    def test_phase_string_is_coerced_with_warning(self):
        sets = self.valid_sets()
        sets[0]["phase"] = "1"
        result = self.run_manifest(sets, "validate")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("W-PHASE-STR", result.stderr)

    def test_empty_modules_rejected(self):
        sets = self.valid_sets()
        sets[1]["modules"] = []
        result = self.run_manifest(sets, "validate")
        self.assertEqual(result.returncode, 2)
        self.assertIn("modules", result.stderr)

    def test_invalid_json_manifest_exit_2(self):
        self.write_text("design/feature-sets.json", "{not json")
        result = self.run_tool("validate", "--manifest", "design/feature-sets.json")
        self.assertEqual(result.returncode, 2)
        self.assertIn("not valid JSON", result.stderr)

    def test_real_plan_validates(self):
        real = Path(__file__).resolve().parents[2] / "design" / "feature-sets.json"
        result = subprocess.run(
            [sys.executable, str(TOOL), "validate", "--manifest", str(real)],
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("16 feature sets", result.stdout)
        self.assertIn("116 dependency edges", result.stdout)
        self.assertIn("21 contracts", result.stdout)


class TestOrderAndGraph(ToolTestCase):
    def test_order_is_topological(self):
        result = self.run_manifest(self.valid_sets(), "order", "--phase", "1")
        self.assertEqual(result.returncode, 0, result.stderr)
        out = result.stdout
        self.assertLess(out.index("FS-00"), out.index("FS-01"))
        self.assertLess(out.index("FS-01"), out.index("FS-02"))

    def test_order_json_machine_readable(self):
        result = self.run_manifest(self.valid_sets(), "order", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        ids = [row["id"] for row in payload["order"]]
        self.assertEqual(ids, ["FS-00", "FS-01", "FS-02"])
        self.assertIn("0A", payload["waves"])

    def test_order_cycle_aborts(self):
        sets = self.valid_sets()
        sets[1]["depends_on"] = [dep("FS-02", "pricing.engine")]
        result = self.run_manifest(sets, "order")
        self.assertEqual(result.returncode, 2)
        self.assertIn("E-CYCLE", result.stderr)

    def test_who_depends_on_lists_consumers(self):
        manifest = self.write_json("design/feature-sets.json", make_manifest(self.valid_sets()))
        result = self.run_tool("who-depends-on", "--manifest", str(manifest), "--contract", "item.master")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("FS-02", result.stdout)
        self.assertIn("consumer impact list: 1", result.stdout)

    def test_who_depends_on_unknown_contract(self):
        manifest = self.write_json("design/feature-sets.json", make_manifest(self.valid_sets()))
        result = self.run_tool("who-depends-on", "--manifest", str(manifest), "--contract", "no.such")
        self.assertEqual(result.returncode, 2)

    def test_deploy_order_runbook_table(self):
        manifest = self.write_json("design/feature-sets.json", make_manifest(self.valid_sets()))
        result = self.run_tool("deploy-order", "--manifest", str(manifest), "--phase", "1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("| Wave | # | FS |", result.stdout)
        self.assertIn("check-env --set FS-01", result.stdout)
        self.assertIn("record --set FS-01", result.stdout)
        self.assertIn("Wave sequence: 0A -> 0B", result.stdout)


class TestCheckEnvAndRecord(ToolTestCase):
    def setUp(self):
        super().setUp()
        self.manifest = self.write_json("design/feature-sets.json", make_manifest(self.valid_sets()))

    def state(self, payload):
        return self.write_json("state.json", payload)

    def test_check_env_pass(self):
        state = self.state(
            {
                "FS-00": {"status": "verified", "contracts": {"platform.core": "1.0.0"}},
            }
        )
        result = self.run_tool("check-env", "--manifest", str(self.manifest), "--set", "FS-01", "--env", "dev", "--state", str(state))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("ENTRY GATE PASS", result.stdout)

    def test_check_env_unverified_dep_fails(self):
        state = self.state(
            {
                "FS-00": {"status": "deployed", "contracts": {"platform.core": "1.0.0"}},
            }
        )
        result = self.run_tool("check-env", "--manifest", str(self.manifest), "--set", "FS-01", "--env", "dev", "--state", str(state))
        self.assertEqual(result.returncode, 3)
        self.assertIn("ENTRY GATE FAILED", result.stderr)
        self.assertIn("expected 'verified'", result.stderr)

    def test_check_env_missing_dep_fails(self):
        state = self.state({})
        result = self.run_tool("check-env", "--manifest", str(self.manifest), "--set", "FS-01", "--env", "dev", "--state", str(state))
        self.assertEqual(result.returncode, 3)
        self.assertIn("not present in deployment state", result.stderr)

    def test_check_env_contract_range_mismatch_fails(self):
        state = self.state(
            {
                "FS-00": {"status": "verified", "contracts": {"platform.core": "2.0.0"}},
            }
        )
        result = self.run_tool("check-env", "--manifest", str(self.manifest), "--set", "FS-01", "--env", "dev", "--state", str(state))
        self.assertEqual(result.returncode, 3)
        self.assertIn("does not satisfy required range", result.stderr)

    def test_check_env_missing_contract_fails(self):
        state = self.state({"FS-00": {"status": "verified", "contracts": {}}})
        result = self.run_tool("check-env", "--manifest", str(self.manifest), "--set", "FS-01", "--env", "dev", "--state", str(state))
        self.assertEqual(result.returncode, 3)
        self.assertIn("not recorded as deployed", result.stderr)

    def test_check_env_root_set_passes_on_greenfield(self):
        result = self.run_tool(
            "check-env", "--manifest", str(self.manifest), "--set", "FS-00", "--env", "dev", "--state", str(self.tmp / "missing-state.json")
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("0/0 dependency contract", result.stdout)

    def test_check_env_unreadable_state_is_gate_failure(self):
        bad = self.write_text("bad-state.json", "{oops")
        result = self.run_tool("check-env", "--manifest", str(self.manifest), "--set", "FS-01", "--env", "dev", "--state", str(bad))
        self.assertEqual(result.returncode, 3)

    def test_check_env_unknown_set_exit_2(self):
        state = self.state({})
        result = self.run_tool("check-env", "--manifest", str(self.manifest), "--set", "FS-99", "--env", "dev", "--state", str(state))
        self.assertEqual(result.returncode, 2)

    def test_record_verified_requires_evidence(self):
        state_path = self.tmp / "deployment-state.dev.json"
        result = self.run_tool(
            "record", "--manifest", str(self.manifest), "--set", "FS-00", "--env", "dev", "--status", "verified", "--state", str(state_path)
        )
        self.assertEqual(result.returncode, 3)
        self.assertIn("requires --evidence", result.stderr)
        self.assertFalse(state_path.exists())

    def test_record_verified_missing_evidence_file_refused(self):
        state_path = self.tmp / "deployment-state.dev.json"
        result = self.run_tool(
            "record",
            "--manifest",
            str(self.manifest),
            "--set",
            "FS-00",
            "--env",
            "dev",
            "--status",
            "verified",
            "--evidence",
            str(self.tmp / "nope.log"),
            "--state",
            str(state_path),
        )
        self.assertEqual(result.returncode, 3)
        self.assertIn("does not exist", result.stderr)

    def test_record_verified_with_unverified_dependency_refused(self):
        state_path = self.tmp / "deployment-state.dev.json"
        evidence = self.write_text("evidence.log", "all exit checks passed\n")
        result = self.run_tool(
            "record",
            "--manifest",
            str(self.manifest),
            "--set",
            "FS-01",
            "--env",
            "dev",
            "--status",
            "verified",
            "--evidence",
            str(evidence),
            "--state",
            str(state_path),
        )
        self.assertEqual(result.returncode, 3)
        self.assertIn("dependencies are not verified", result.stderr)

    def test_record_verified_ok_and_state_schema(self):
        state_path = self.tmp / "deployment-state.dev.json"
        evidence = self.write_text("evidence.log", "all exit checks passed\n")
        result = self.run_tool(
            "record",
            "--manifest",
            str(self.manifest),
            "--set",
            "FS-00",
            "--env",
            "dev",
            "--status",
            "verified",
            "--evidence",
            str(evidence),
            "--state",
            str(state_path),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        state = json.loads(state_path.read_text())
        self.assertEqual(state["FS-00"]["status"], "verified")
        self.assertEqual(state["FS-00"]["contracts"], {"platform.core": "1.0.0"})
        self.assertTrue(state["FS-00"]["evidence"])
        self.assertIn("updated_at", state["FS-00"])
        leftovers = [p for p in self.tmp.iterdir() if p.name.startswith(".deployment-state.")]
        self.assertEqual(leftovers, [])

    def test_record_deployed_then_verified_chain(self):
        state_path = self.tmp / "deployment-state.dev.json"
        evidence = self.write_text("evidence.log", "ok\n")
        r1 = self.run_tool(
            "record", "--manifest", str(self.manifest), "--set", "FS-00", "--env", "dev", "--status", "deployed", "--state", str(state_path)
        )
        self.assertEqual(r1.returncode, 0, r1.stderr)
        r2 = self.run_tool(
            "record",
            "--manifest",
            str(self.manifest),
            "--set",
            "FS-00",
            "--env",
            "dev",
            "--status",
            "verified",
            "--evidence",
            str(evidence),
            "--state",
            str(state_path),
        )
        self.assertEqual(r2.returncode, 0, r2.stderr)
        r3 = self.run_tool(
            "record", "--manifest", str(self.manifest), "--set", "FS-01", "--env", "dev", "--status", "verified", "--evidence", str(evidence), "--state", str(state_path)
        )
        self.assertEqual(r3.returncode, 0, r3.stderr)
        state = json.loads(state_path.read_text())
        self.assertEqual(sorted(state), ["FS-00", "FS-01"])

    def test_record_failed_without_deps_allowed(self):
        state_path = self.tmp / "deployment-state.dev.json"
        result = self.run_tool(
            "record", "--manifest", str(self.manifest), "--set", "FS-01", "--env", "dev", "--status", "failed", "--state", str(state_path)
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(state_path.read_text())["FS-01"]["status"], "failed")

    def test_record_bad_status_rejected_by_argparse(self):
        result = self.run_tool(
            "record", "--manifest", str(self.manifest), "--set", "FS-00", "--env", "dev", "--status", "banana"
        )
        self.assertEqual(result.returncode, 2)


class TestCoverage(ToolTestCase):
    def setUp(self):
        super().setUp()
        self.manifest = self.write_json("design/feature-sets.json", make_manifest(self.valid_sets()))
        self.research = self.tmp / "research"
        self.research.mkdir()
        (self.research / "features-item-inventory.json").write_text(
            json.dumps([{"id": "iri-%03d" % i, "title": "iri topic %d" % i} for i in range(1, 4)]), encoding="utf-8"
        )
        (self.research / "features-mfg-procurement-wms.json").write_text(
            json.dumps([{"id": "mpw-%03d" % i, "title": "mpw topic %d" % i} for i in range(1, 3)]), encoding="utf-8"
        )
        self.ids = ["iri-001", "iri-002", "iri-003", "mpw-001", "mpw-002"]

    def coverage(self, assignments=None, extra_totals=None):
        assignments = assignments or {
            "iri-001": "FS-00",
            "iri-002": "FS-01",
            "iri-003": "FS-02",
            "mpw-001": "FS-00",
            "mpw-002": "FS-01",
        }
        entries = {i: {"disposition": d, "reason": "fixture rule"} for i, d in assignments.items()}
        by_fs = {fs: 0 for fs in ("FS-00", "FS-01", "FS-02")}
        for value in assignments.values():
            if value in by_fs:
                by_fs[value] += 1
        totals = {
            "total": len(assignments),
            "by_catalog": {
                "iri": sum(1 for i in assignments if i.startswith("iri-")),
                "mpw": sum(1 for i in assignments if i.startswith("mpw-")),
            },
            "by_disposition": {
                **{k: 0 for k in ("DEFERRED-UI", "DEFERRED-MOBILE", "DEFERRED-ANALYTICS", "DEFERRED-INTEGRATION", "DEFERRED-SUITEAPP", "EXCLUDED-SCM-MOBILE")},
                **by_fs,
            },
            "by_fs": by_fs,
        }
        if extra_totals:
            totals.update(extra_totals)
        payload = dict(entries)
        payload["totals"] = totals
        return self.write_json("design/feature-coverage.json", payload)

    def run_coverage(self, path=None, extra_args=()):
        manifest = json.loads(self.manifest.read_text())
        # map fixture catalog ids to FS ids that exist in the manifest
        coverage_path = path or self.coverage()
        return self.run_tool(
            "coverage", "--manifest", str(self.manifest), "--coverage", str(coverage_path), "--research-dir", str(self.research), *extra_args
        )

    def test_coverage_ok(self):
        result = self.run_coverage()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("COVERAGE OK: 5/5", result.stdout)

    def test_coverage_missing_id_rejected(self):
        assignments = {i: "FS-00" for i in self.ids[:-1]}
        result = self.run_coverage(path=self.coverage(assignments))
        self.assertEqual(result.returncode, 2)
        self.assertIn("E-COV-MISSING", result.stderr)

    def test_coverage_extra_id_rejected(self):
        assignments = {i: "FS-00" for i in self.ids}
        assignments["iri-004"] = "FS-00"
        result = self.run_coverage(path=self.coverage(assignments))
        self.assertEqual(result.returncode, 2)
        self.assertIn("E-COV-EXTRA", result.stderr)

    def test_coverage_invalid_disposition_rejected(self):
        assignments = {i: "FS-00" for i in self.ids}
        assignments["iri-001"] = "FS-99"
        result = self.run_coverage(path=self.coverage(assignments))
        self.assertEqual(result.returncode, 2)
        self.assertIn("E-COV-DISPOSITION", result.stderr)

    def test_coverage_totals_mismatch_rejected(self):
        assignments = {i: "FS-00" for i in self.ids}
        result = self.run_coverage(path=self.coverage(assignments, extra_totals={"total": 999}))
        self.assertEqual(result.returncode, 2)
        self.assertIn("E-COV-TOTALS", result.stderr)

    def test_coverage_duplicate_key_rejected(self):
        path = self.tmp / "design" / "feature-coverage.json"
        raw = '{"iri-001": {"disposition": "FS-00", "reason": "a"}, "iri-001": {"disposition": "FS-00", "reason": "b"}}'
        path.write_text(raw, encoding="utf-8")
        result = self.run_coverage(path=path)
        self.assertEqual(result.returncode, 2)
        self.assertIn("E-COV-DUP-KEY", result.stderr)

    def test_coverage_real_plan(self):
        real = Path(__file__).resolve().parents[2]
        result = subprocess.run(
            [sys.executable, str(TOOL), "coverage", "--manifest", str(real / "design/feature-sets.json"), "--coverage", str(real / "design/feature-coverage.json"), "--research-dir", str(real / "research")],
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("1348/1348", result.stdout)
        self.assertIn("iri=412", result.stdout)
        self.assertIn("mpw=936", result.stdout)


class TestSemverHelpers(unittest.TestCase):
    """Direct unit tests for the range semantics (no subprocess)."""

    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(TOOL.parent))
        import feature_sets as fs_mod  # noqa: E402

        cls.fs = fs_mod

    def test_range_contains(self):
        r = self.fs.parse_range(">=1.0.0 <2.0.0")
        self.assertTrue(r.contains((1, 0, 0)))
        self.assertTrue(r.contains((1, 9, 9)))
        self.assertFalse(r.contains((2, 0, 0)))
        self.assertFalse(r.contains((0, 9, 9)))

    def test_caret_and_tilde(self):
        self.assertTrue(self.fs.parse_range("^1.2.3").contains((1, 9, 0)))
        self.assertFalse(self.fs.parse_range("^1.2.3").contains((2, 0, 0)))
        self.assertTrue(self.fs.parse_range("~1.2.3").contains((1, 2, 9)))
        self.assertFalse(self.fs.parse_range("~1.2.3").contains((1, 3, 0)))

    def test_wildcards(self):
        self.assertTrue(self.fs.parse_range("1.x").contains((1, 4, 2)))
        self.assertFalse(self.fs.parse_range("1.x").contains((2, 0, 0)))
        self.assertTrue(self.fs.parse_range("1.2.x").contains((1, 2, 7)))
        self.assertFalse(self.fs.parse_range("1.2.x").contains((1, 3, 0)))

    def test_satisfiability(self):
        self.assertTrue(self.fs.parse_range(">=1.0.0 <2.0.0").satisfiable())
        self.assertFalse(self.fs.parse_range(">=2.0.0 <1.0.0").satisfiable())
        self.assertFalse(self.fs.parse_range(">1.0.0 <1.0.1").satisfiable())
        self.assertFalse(self.fs.parse_range("==1.0.0 >=2.0.0").satisfiable())

    def test_wave_ranking(self):
        ranks = [self.fs.wave_rank(w) for w in ["0A", "0B", "0C", "1A", "1F", "2", "10A"]]
        self.assertEqual(ranks, sorted(ranks))


if __name__ == "__main__":
    unittest.main(verbosity=2)
