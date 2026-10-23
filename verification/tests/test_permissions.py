#!/usr/bin/env python3
"""Unit + adversarial tests for the Δ-RBAC `permissions` command.

Stdlib only (unittest). Fixtures are generated in temp dirs at test time. A valid
fixture is generated with the v1.2.0 expected shape (234 scopes = 220 assignable
+ 13 service-only + 1 platform-only; 7 role templates; 14 SoD pairs; 6 global
rules; 6/5 allowlists) and mutated for the adversarial cases.

Run from inventory-platform/:

    python3 -m unittest verification/tests/test_permissions.py -v
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TOOL = Path(__file__).resolve().parents[2] / "tools" / "feature_sets.py"


def make_catalogue() -> dict:
    """Generate a valid v1.2.0-shaped permissions catalogue fixture."""
    catalogue = []
    # 241 assignable scopes (the first 18 are approval-required); reserve names used below.
    reserved = {"platform.users.manage", "platform.roles.manage", "platform.users.assign",
                "platform.sso.manage", "platform.sso.read"}
    counter = 0
    while len([s for s in catalogue if s["assignable"]]) < 236:
        counter += 1
        scope = "d%02d.res%02d.act%02d" % (counter % 20, counter, counter)
        if scope in reserved:
            continue
        entry = {
            "scope": scope, "domain": scope.split(".")[0], "action": scope.split(".")[-1],
            "requires_approval": len([s for s in catalogue if s["requires_approval"]]) < 18,
            "platform_only": False, "service_only": False, "assignable": True, "owner_feature_set": "FS-00",
        }
        catalogue.append(entry)
    for scope in sorted(reserved):
        catalogue.append({
            "scope": scope, "domain": "platform", "action": scope.split(".")[-1],
            "requires_approval": False, "platform_only": False, "service_only": False,
            "assignable": True, "owner_feature_set": "FS-00",
        })
    # 13 service-only scopes (incl. platform.internal)
    service = ["platform.internal"] + ["svc.job%02d.run" % i for i in range(1, 13)]
    for scope in service:
        catalogue.append({
            "scope": scope, "domain": scope.split(".")[0], "action": scope.split(".")[-1],
            "requires_approval": False, "platform_only": False, "service_only": True,
            "assignable": False, "owner_feature_set": "FS-00",
        })
    # 1 platform-only
    catalogue.append({
        "scope": "platform.tenant.manage", "domain": "platform", "action": "manage",
        "requires_approval": False, "platform_only": True, "service_only": False,
        "assignable": False, "owner_feature_set": "FS-00",
    })
    assignable = [e["scope"] for e in catalogue if e["assignable"]]
    # SoD pairs from assignable scopes (a != b, unique unordered pairs)
    sod_pairs = []
    for i in range(14):
        a = assignable[i % len(assignable)]
        b = assignable[(i + 17) % len(assignable)]
        if a == b:
            b = assignable[(i + 18) % len(assignable)]
        sod_pairs.append({"a": a, "b": b, "enforcement": "HARD", "reason": "fixture pair %d" % i})
    # 7 role templates: admin = all assignable; non-admin avoid SoD-pair combinations.
    templates = [{
        "key": "admin", "name": "Tenant Administrator", "description": "fixture admin",
        "scopes": assignable, "system": True, "editable": False, "cloneable": True,
    }]
    for name in ("planner", "buyer", "warehouse_operator", "cost_accountant", "quality_inspector", "readonly"):
        templates.append({
            "key": name, "name": name.replace("_", " ").title(), "description": "fixture template",
            "scopes": assignable[:3], "system": True, "editable": False, "cloneable": True,
        })
    domains = {}
    for entry in catalogue:
        domains[entry["domain"]] = domains.get(entry["domain"], 0) + 1
    data = {
        "schema_version": "1.0.0",
        "plan_version": "1.2.0",
        "generated_from": "fixture",
        "catalogue": catalogue,
        "aliases": {"platform.user.manage": "platform.users.manage"},
        "role_templates": templates,
        "sod_pairs": sod_pairs,
        "global_rules": ["Deny by default.", "Approval separation.", "Grant safety.",
                         "Last-admin guard.", "Effective permissions from DB.", "Audited RBAC changes."],
        "public_allowlist": ["/healthz", "/readyz", "/openapi.json", "POST /api/v1/auth/login",
                             "POST /api/v1/auth/select-tenant", "POST /api/v1/admin/invitations/{token}/accept",
                             "GET /api/v1/auth/providers", "POST /api/v1/auth/sso/start",
                             "GET /api/v1/auth/sso/callback"],
        "self_service_allowlist": ["POST /api/v1/auth/switch-tenant", "POST /api/v1/auth/refresh",
                                   "POST /api/v1/auth/logout", "GET /api/v1/me", "GET /api/v1/me/tenants",
                                   "/api/v1/me/effective-permissions"],
        "service_scopes": ["platform.internal"],
        "route_guard_test": "app/tests/security/test_route_scope_coverage.py",
        "counts": {
            "scopes": len(catalogue),
            "assignable_scopes": len(assignable),
            "platform_only": 1,
            "service_only": len(service),
            "aliases": 1,
            "roles": len(templates),
            "sod_pairs": len(sod_pairs),
            "domains": dict(sorted(domains.items())),
        },
    }
    return data


def make_spec_text(scopes: list[str]) -> str:
    rows = []
    for i, scope in enumerate(scopes):
        rows.append("| GET | `/api/v1/res%d` | fixture read %d | — | `{}` | `%s` | — |" % (i, i, scope))
    rows.append("| GET | `/healthz` | Liveness | — | `{status:\"ok\"}` | none (public allowlist) | — |")
    return "# FS-00 — fixture\n\n## 6. API surface\n\n| Method | Path | Purpose | Request | Response | Auth scope | Idempotency |\n|---|---|---|---|---|---|---|\n" + "\n".join(rows) + "\n\n## 7. Events and jobs\n\n| Event | Version |\n|---|---|\n"


class PermissionsTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="perm-test-")
        self.tmp = Path(self._tmp.name)
        self.permissions = self.tmp / "permissions.json"
        self.specs = self.tmp / "feature-sets"
        self.specs.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def write_fixture(self, data: dict, scopes: list[str] | None = None) -> None:
        self.permissions.write_text(json.dumps(data, indent=2), encoding="utf-8")
        (self.specs / "FS-00.md").write_text(make_spec_text(scopes or ["platform.users.manage"]), encoding="utf-8")

    def run_tool(self):
        return subprocess.run(
            [sys.executable, str(TOOL), "permissions",
             "--permissions", str(self.permissions), "--specs-dir", str(self.specs)],
            cwd=str(self.tmp), capture_output=True, text=True, timeout=120,
        )


class TestPermissionsValid(PermissionsTestBase):
    def test_valid_catalogue_passes(self):
        data = make_catalogue()
        self.write_fixture(data, scopes=["platform.users.manage"])
        result = self.run_tool()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("PERMISSIONS OK: 255 scopes", result.stdout)
        self.assertIn("241 assignable", result.stdout)
        self.assertIn("13 service-only", result.stdout)
        self.assertIn("1 platform-only", result.stdout)
        self.assertIn("18 requires-approval", result.stdout)
        self.assertIn("7 role templates", result.stdout)
        self.assertIn("14 SoD pairs", result.stdout)
        self.assertIn("6 global rules", result.stdout)

    def test_json_summary(self):
        self.write_fixture(make_catalogue(), scopes=["platform.users.manage"])
        result = subprocess.run(
            [sys.executable, str(TOOL), "permissions", "--permissions", str(self.permissions),
             "--specs-dir", str(self.specs), "--json"],
            cwd=str(self.tmp), capture_output=True, text=True, timeout=120,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        start = result.stdout.find("{")
        payload = json.loads(result.stdout[start:])
        self.assertEqual(payload["scopes"], 255)
        self.assertEqual(payload["assignable"], 241)
        self.assertEqual(payload["unknown_scopes"], 0)


class TestPermissionsAdversarial(PermissionsTestBase):
    def test_duplicate_scope_rejected(self):
        data = make_catalogue()
        data["catalogue"].append(dict(data["catalogue"][0]))
        self.write_fixture(data)
        result = self.run_tool()
        self.assertEqual(result.returncode, 2)
        self.assertIn("E-PERM-DUP", result.stderr)

    def test_platform_only_in_tenant_role_rejected(self):
        data = make_catalogue()
        for role in data["role_templates"]:
            if role["key"] == "planner":
                role["scopes"] = list(role["scopes"]) + ["platform.tenant.manage"]
        self.write_fixture(data)
        result = self.run_tool()
        self.assertEqual(result.returncode, 2)
        self.assertIn("E-PERM-ROLE", result.stderr)

    def test_sod_pair_in_non_admin_role_rejected(self):
        data = make_catalogue()
        pair = data["sod_pairs"][0]
        for role in data["role_templates"]:
            if role["key"] == "buyer":
                role["scopes"] = [pair["a"], pair["b"]]
        self.write_fixture(data)
        result = self.run_tool()
        self.assertEqual(result.returncode, 2)
        self.assertIn("E-PERM-SOD-ROLE", result.stderr)

    def test_unknown_spec_scope_rejected(self):
        data = make_catalogue()
        self.write_fixture(data, scopes=["ghost.scope.read"])
        result = self.run_tool()
        self.assertEqual(result.returncode, 2)
        self.assertIn("E-PERM-UNKNOWN-SCOPE", result.stderr)
        self.assertIn("ghost.scope.read", result.stderr)

    def test_missing_alias_target_rejected(self):
        data = make_catalogue()
        data["aliases"] = {"platform.user.manage": "not.in.catalogue"}
        self.write_fixture(data)
        result = self.run_tool()
        self.assertEqual(result.returncode, 2)
        self.assertIn("E-PERM-ALIAS", result.stderr)

    def test_seeded_alias_rejected(self):
        data = make_catalogue()
        data["catalogue"].append({
            "scope": "platform.user.manage", "domain": "platform", "action": "manage",
            "requires_approval": False, "platform_only": False, "service_only": False,
            "assignable": True, "owner_feature_set": "FS-00",
        })
        data["counts"]["scopes"] = len(data["catalogue"])
        data["counts"]["assignable_scopes"] = len([e for e in data["catalogue"] if e["assignable"]])
        self.write_fixture(data)
        result = self.run_tool()
        self.assertEqual(result.returncode, 2)
        self.assertIn("E-PERM-ALIAS", result.stderr)

    def test_bad_counts_metadata_rejected(self):
        data = make_catalogue()
        data["counts"]["scopes"] = 999
        self.write_fixture(data)
        result = self.run_tool()
        self.assertEqual(result.returncode, 2)
        self.assertIn("E-PERM-COUNTS", result.stderr)

    def test_wrong_total_count_rejected(self):
        data = make_catalogue()
        data["catalogue"].pop()
        data["counts"]["scopes"] = len(data["catalogue"])
        self.write_fixture(data)
        result = self.run_tool()
        self.assertEqual(result.returncode, 2)
        self.assertIn("E-PERM-COUNT", result.stderr)

    def test_bad_scope_naming_rejected(self):
        data = make_catalogue()
        data["catalogue"].append({
            "scope": "BadScope", "domain": "bad", "action": "x",
            "requires_approval": False, "platform_only": False, "service_only": False,
            "assignable": True, "owner_feature_set": "FS-00",
        })
        self.write_fixture(data)
        result = self.run_tool()
        self.assertEqual(result.returncode, 2)
        self.assertIn("E-PERM-SCOPE", result.stderr)

    def test_malformed_permissions_json_rejected(self):
        self.permissions.write_text("{not json", encoding="utf-8")
        (self.specs / "FS-00.md").write_text(make_spec_text(["platform.users.manage"]), encoding="utf-8")
        result = self.run_tool()
        self.assertEqual(result.returncode, 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
