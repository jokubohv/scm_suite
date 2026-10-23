"""Purity invariant: the FS-00 security package must stay free of DB/network/API dependencies."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

SECURITY_DIR = Path(__file__).resolve().parents[4] / "app" / "core" / "security"
BANNED_ROOTS = {
    "sqlalchemy",
    "asyncpg",
    "psycopg",
    "psycopg2",
    "redis",
    "rq",
    "httpx",
    "requests",
    "urllib3",
    "fastapi",
    "starlette",
    "minio",
    "socket",
    "http",
    "app.db",
    "app.api",
    "app.core.config",
    "app.seed",
}
ALLOWED_THIRD_PARTY = {"jwt", "argon2"}


def _import_roots(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            roots.add(node.module.split(".")[0])
    return roots


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            modules.add(node.module)
    return modules


def test_security_package_has_no_db_network_or_api_imports() -> None:
    modules = sorted(SECURITY_DIR.glob("*.py"))
    assert modules, "security package modules are missing"
    stdlib = set(sys.stdlib_module_names)
    violations: list[str] = []
    for module in modules:
        roots = _import_roots(module)
        modules_imported = _imported_modules(module)
        for module_path in modules_imported:
            if any(module_path == banned or module_path.startswith(f"{banned}.") for banned in BANNED_ROOTS):
                violations.append(f"{module.name}: banned import {module_path!r}")
        for root in roots:
            if root in BANNED_ROOTS:
                violations.append(f"{module.name}: banned import {root!r}")
            if root == "app":
                continue
            if root not in stdlib and root not in ALLOWED_THIRD_PARTY:
                violations.append(f"{module.name}: unexpected third-party import {root!r}")
    assert not violations, "\n".join(violations)


def test_security_package_imports_without_side_effects() -> None:
    # Importing the package must not attempt DB/network connections (it is used by unit tests only).
    import app.core.security as security_package

    for name in ("authn", "errors", "idempotency", "passwords", "scopes", "tenant_context", "tokens"):
        assert hasattr(security_package, name)
