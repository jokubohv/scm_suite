#!/usr/bin/env python3
"""FS-00 exit-readiness static audit (task-50, read-only)."""
from __future__ import annotations

import ast
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]      # inventory-platform/
SPEC = (ROOT / "feature-sets/FS-00.md").read_text()
MANIFEST = json.loads((ROOT / "design/feature-sets.json").read_text())
PERM = json.loads((ROOT / "design/permissions.json").read_text())
FS00 = next(s for s in MANIFEST["feature_sets"] if s["id"] == "FS-00")


def norm_path(path: str) -> str:
    path = path.strip().strip("`").split("?")[0]
    path = re.sub(r"\{[^}]+\}", "{}", path)
    if path.startswith("/v1/"):
        path = "/api/v1/" + path[len("/v1/"):]
    return path.rstrip("/") or "/"


def methods_of(cell: str) -> list[str]:
    return [m for m in re.findall(r"GET|POST|PUT|PATCH|DELETE", cell, re.I)]


# ---------------------------------------------------------------- expected routes
spec_sec6 = SPEC[SPEC.find("## 6. API surface"):SPEC.find("## 7. Events and jobs")]
expected: dict[tuple[str, str], dict[str, Any]] = {}
spec_rows = 0
for line in spec_sec6.splitlines():
    if not line.startswith("|"):
        continue
    cells = [c.strip() for c in line.strip().strip("|").split("|")]
    if len(cells) < 7:
        continue
    methods = methods_of(cells[0])
    if not methods:
        continue
    spec_rows += 1
    path = norm_path(cells[1])
    auth = cells[-2]
    scope_match = re.findall(r"`([a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+)`", auth)
    if "public allowlist" in auth.lower():
        scope = "PUBLIC_ALLOWLIST"
    elif "self-service allowlist" in auth.lower():
        scope = "SELF_SERVICE_ALLOWLIST"
    elif scope_match:
        scope = scope_match[0]
    else:
        scope = "UNKNOWN:" + auth[:40]
    for method in methods:
        expected[(method, path)] = {"scope": scope, "spec_auth": auth, "line": line}

# ---------------------------------------------------------------- actual routes (OpenAPI)
sys.path.insert(0, str(ROOT))
from app.api.main import create_app  # noqa: E402

app = create_app()
openapi = app.openapi()
implemented: set[tuple[str, str]] = set()
for path, operations in openapi.get("paths", {}).items():
    for method in operations:
        if method.upper() in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
            implemented.add((method.upper(), norm_path(path)))

missing = sorted(set(expected) - implemented)
extra = sorted(implemented - set(expected))

# ---------------------------------------------------------------- static scope map (AST)
scope_map: dict[tuple[str, str], str] = {}
route_files = sorted((ROOT / "app/api/routers").rglob("*.py"))

def _scope_call(node: ast.AST, constants: dict[str, str]) -> str | None:
    if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "require_scope" and node.args:
        arg = node.args[0]
        if isinstance(arg, ast.Constant):
            return str(arg.value)
        if isinstance(arg, ast.Name):
            return constants.get(arg.id)
    return None


def _dependency_scope(value: ast.AST, constants: dict[str, str]) -> str | None:
    found = _scope_call(value, constants)
    if found:
        return found
    for child in ast.iter_child_nodes(value):
        found = _dependency_scope(child, constants)
        if found:
            return found
    return None


for route_file in route_files:
    source = route_file.read_text()
    try:
        tree = ast.parse(source)
    except SyntaxError:
        continue
    constants: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    constants[target.id] = node.value.value
    scope_aliases: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            found = _dependency_scope(node.value, constants)
            if found:
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        scope_aliases[target.id] = found
    module_prefix = ""
    router_defaults: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", "") == "router" for t in node.targets):
            if isinstance(node.value, ast.Call):
                for kw in node.value.keywords:
                    if kw.arg == "prefix" and isinstance(kw.value, ast.Constant):
                        module_prefix = str(kw.value.value)
                    if kw.arg == "dependencies":
                        found = _dependency_scope(kw.value, constants)
                        if found:
                            router_defaults.append(found)
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            if not isinstance(dec, ast.Call) or not isinstance(dec.func, ast.Attribute):
                continue
            if getattr(dec.func.value, "id", "") != "router":
                continue
            method = dec.func.attr.lower()
            if method not in {"get", "post", "put", "patch", "delete"}:
                continue
            route_path = str(dec.args[0].value) if dec.args and isinstance(dec.args[0], ast.Constant) else ""
            scope = None
            for kw in dec.keywords:
                if kw.arg == "dependencies":
                    scope = _dependency_scope(kw.value, constants)
            if scope is None:  # scope often comes via module-level Annotated aliases in params
                for arg in list(node.args.args) + list(node.args.kwonlyargs):
                    annotation = arg.annotation
                    if isinstance(annotation, ast.Name) and annotation.id in scope_aliases:
                        scope = scope_aliases[annotation.id]
                        break
                    found = _dependency_scope(annotation, constants) if annotation else None
                    if found:
                        scope = found
                        break
                for default in list(node.args.defaults) + [kw.value for kw in node.args.kw_defaults if kw.value]:
                    if isinstance(default, ast.Name) and default.id in scope_aliases:
                        scope = scope_aliases[default.id]
                        break
                    found = _dependency_scope(default, constants)
                    if found:
                        scope = found
                        break
            full = route_path if route_path.startswith("/api/v1") else module_prefix + route_path
            scope_map[(method.upper(), norm_path(full))] = scope or (router_defaults[0] if router_defaults else "NO_SCOPE")

# annotate allowlisted routes (public/self-service) as explicit allowlist references
_public = set()
_self_service = set()
for entry in PERM["public_allowlist"]:
    parts = entry.split()
    _public.add((parts[0].upper(), norm_path(parts[1])) if len(parts) == 2 else ("*", norm_path(parts[0])))
for entry in PERM["self_service_allowlist"]:
    parts = entry.split()
    _self_service.add((parts[0].upper(), norm_path(parts[1])) if len(parts) == 2 else ("*", norm_path(parts[0])))
for key in list(scope_map):
    if scope_map[key] == "NO_SCOPE":
        if key in _public or ("*", key[1]) in _public:
            scope_map[key] = "PUBLIC_ALLOWLIST"
        elif key in _self_service or ("*", key[1]) in _self_service:
            scope_map[key] = "SELF_SERVICE_ALLOWLIST"

# ---------------------------------------------------------------- tables / migrations
table_line = [ln for ln in SPEC.splitlines() if ln.startswith("| Tables owned |")][0]
spec_tables = set(re.findall(r"`([a-z_]+)`", table_line)) | set(re.findall(r"`(platform\.[a-z_]+)`", table_line))
from app.core.models import platform as p  # noqa: E402

model_tables = {name.split(".")[-1] for name in p.metadata.tables if name.startswith("platform.")}
migration_names: set[str] = set()
migration_files = sorted((ROOT / "app/migrations/versions").glob("*.py"))
for mf in migration_files:
    text = mf.read_text()
    migration_names.update(re.findall(r'create_table\(\s*["\']([a-z_]+)["\']', text, re.S))
    migration_names.update(re.findall(r'["\']([a-z_]+)["\']', text) if "create_tables(op" in text else [])
table_gap = sorted((spec_tables and {t.split(".")[-1] for t in spec_tables}) - model_tables)
model_extra = sorted(model_tables - {t.split(".")[-1] for t in spec_tables})

# migrations chain
revs = {}
for mf in migration_files:
    text = mf.read_text()
    rev = re.search(r"revision\s*=\s*[\"']([^\"']+)", text)
    down = re.search(r"down_revision\s*=\s*[\"']([^\"']+)", text)
    if rev:
        revs[rev.group(1)] = down.group(1) if down else None
heads = [r for r in revs if r not in set(revs.values())]

# ---------------------------------------------------------------- events / jobs
spec_sec7 = SPEC[SPEC.find("## 7. Events and jobs"):SPEC.find("## 8.")]
events: list[str] = []
jobs: list[str] = []
_jobs_header = spec_sec7.find("| Job | Schedule |")
_events_section = spec_sec7[:_jobs_header if _jobs_header > 0 else len(spec_sec7)]
_jobs_section = spec_sec7[_jobs_header:] if _jobs_header > 0 else ""
for line in _events_section.splitlines():
    cells = [c.strip() for c in line.strip().strip("|").split("|")] if line.startswith("|") else []
    if len(cells) >= 2:
        match = re.match(r"`([a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+)`", cells[0])
        if match:
            events.append(match.group(1))
for line in _jobs_section.splitlines():
    cells = [c.strip() for c in line.strip().strip("|").split("|")] if line.startswith("|") else []
    if len(cells) >= 2:
        match = re.match(r"`([a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+)`", cells[0])
        if match:
            jobs.append(match.group(1))
events = sorted(dict.fromkeys(events))
jobs = sorted(dict.fromkeys(jobs))
app_sources = "\n".join(f.read_text(errors="ignore") for f in list((ROOT / "app").rglob("*.py")) if "tests" not in f.parts)
contracts_sources = "\n".join(f.read_text(errors="ignore") for f in list((ROOT / "contracts").rglob("*.y*ml")) + list((ROOT / "app/contracts").rglob("*.py")))
missing_events = [e for e in events if e not in app_sources and e not in contracts_sources]
missing_jobs = [j for j in jobs if j not in app_sources and j not in contracts_sources]

# ---------------------------------------------------------------- exit commands
exit_checks = FS00["exit_checks"]
exit_status = []
for cmd in exit_checks:
    entry: dict[str, Any] = {"command": cmd}
    if cmd.startswith("pytest "):
        parts = cmd.split()
        test_paths = [x for x in parts[1:] if not x.startswith("-") and "/" in x]
        missing_paths = [x for x in test_paths if not (ROOT / x).exists()]
        entry["status"] = "BLOCKED_DB" if not missing_paths else "FAIL_MISSING_TEST"
        entry["missing"] = missing_paths
        entry["tests"] = test_paths
    elif "alembic upgrade" in cmd:
        entry["status"] = "BLOCKED_DB"
    elif "feature_sets.py permissions" in cmd:
        result = subprocess.run([sys.executable, "tools/feature_sets.py", "permissions"], cwd=ROOT,
                                capture_output=True, text=True, timeout=120)
        entry["status"] = "PASS_STATIC" if result.returncode == 0 else "FAIL"
        entry["output"] = (result.stdout + result.stderr).splitlines()[-2:]
    elif "record --set FS-00" in cmd:
        entry["status"] = "BLOCKED_GATE"
    else:
        entry["status"] = "BLOCKED_ENV"
    exit_status.append(entry)

# ---------------------------------------------------------------- security evidence
scope_counts = {
    "scopes": len(PERM["catalogue"]),
    "assignable": sum(1 for e in PERM["catalogue"] if e["assignable"]),
    "service_only": sum(1 for e in PERM["catalogue"] if e["service_only"]),
    "platform_only": sum(1 for e in PERM["catalogue"] if e["platform_only"]),
    "roles": len(PERM["role_templates"]),
    "sod_pairs": len(PERM["sod_pairs"]),
}
expected_tests = [
    "app/tests/invariants/test_rls_tenant_isolation.py",
    "app/tests/invariants/test_append_only_privileges.py",
    "app/tests/invariants/test_mt_tenant_user_isolation.py",
    "app/tests/invariants/test_mt_identity_rules.py",
    "app/tests/security/test_route_scope_coverage.py",
    "app/tests/integration/platform/test_mt_auth_flows.py",
    "app/tests/integration/platform/test_mt_invitations_sessions.py",
    "app/tests/integration/platform/test_rbac_roles.py",
    "app/tests/integration/platform/test_sod_policy.py",
    "app/tests/integration/platform/test_sso_oidc_flow.py",
    "app/tests/integration/platform/test_sso_tenant_isolation.py",
    "app/tests/integration/platform/test_sso_group_mapping.py",
    "app/tests/integration/finance_port/test_journal_intent_read_api.py",
    "app/tests/contracts/test_fs00_provider_contract.py",
]
test_existence = {t: (ROOT / t).exists() for t in expected_tests}
# actual test files (collection discovery)
actual_tests = sorted(str(x.relative_to(ROOT)) for x in (ROOT / "app/tests").rglob("test_*.py"))

# DB probe
db_status = "BLOCKED_DB"
try:
    import asyncio
    import asyncpg
    async def _probe():
        conn = await asyncpg.connect("postgresql://inventory:inventory-dev-password@localhost:25432/inventory", timeout=2)
        await conn.close()
    asyncio.run(_probe())
    db_status = "UP"
except Exception as exc:  # pragma: no cover - environment-dependent
    db_status = f"BLOCKED_DB ({type(exc).__name__}: {str(exc)[:80]})"

# emission points only from app source (contracts merely define schemas)
missing_event_emissions = [e for e in events if e not in app_sources]
framework_paths = {"/healthz", "/readyz", "/openapi.json", "/internal/deps"}
real_missing = [m for m in missing if norm_path(m[1]) not in framework_paths]
real_extra = [m for m in extra if norm_path(m[1]) not in framework_paths]
from difflib import SequenceMatcher

def _candidate(method: str, path: str) -> list[str]:
    out = []
    for em, ep in real_extra:
        if em != method:
            continue
        ratio = SequenceMatcher(None, norm_path(path), norm_path(ep)).ratio()
        shared = set(norm_path(path).split("/")) & set(norm_path(ep).split("/"))
        if ratio >= 0.6 or len(shared) >= 3:
            out.append(f"{em} {ep} (similarity {ratio:.2f})")
    return out[:2]

missing_classified = [{"op": f"{m} {p}", "alias_candidates": _candidate(m, p)} for m, p in real_missing]
scope_missing = sorted(f"{m} {p}" for (m, p) in implemented
                       if scope_map.get((m, p), "NO_SCOPE") == "NO_SCOPE" and norm_path(p) not in framework_paths)

report = {
    "spec_rows": spec_rows, "expected_ops": len(expected), "implemented_ops": len(implemented),
    "missing_ops": missing, "extra_ops": extra,
    "missing_scope_routes": sorted(f"{m} {p}" for (m, p) in implemented if scope_map.get((m, p), "NO_SCOPE") == "NO_SCOPE"),
    "tables": {"spec": len(spec_tables), "models": len(model_tables), "migration_names": len(migration_names),
                "table_gap": table_gap, "model_extra": model_extra},
    "migrations": {"files": len(migration_files), "revisions": sorted(revs), "heads": heads},
    "events": {"expected": len(events), "missing": missing_events},
    "jobs": {"expected": len(jobs), "expected_names": jobs, "missing": missing_jobs},
    "exit_checks": exit_status,
    "security": scope_counts,
    "test_existence": test_existence,
    "actual_test_count": len(actual_tests),
    "db": db_status, "missing_event_emissions": {"count": len(missing_event_emissions), "names": missing_event_emissions},
    "missing_classified": missing_classified, "real_missing": len(real_missing), "real_extra": len(real_extra),
    "scope_missing": scope_missing[:20], "scope_missing_count": len(scope_missing),
}
print(json.dumps(report, indent=1, default=str))
(ROOT / "verification/evidence/FS00-AUDIT/static-audit.json").write_text(json.dumps(report, indent=1, default=str))
