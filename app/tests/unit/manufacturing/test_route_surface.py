"""FS-09 route-surface parity: every declared aggregate operation is mounted (canonical + legacy)."""

from __future__ import annotations

import json
import re
from pathlib import Path

from app.api.routers.manufacturing import ROUTE_SCOPES, router

REPO_ROOT = Path(__file__).resolve().parents[4]
METHODS = ("get", "post", "put", "patch", "delete")


def _aggregate() -> dict:
    return json.loads((REPO_ROOT / "contracts" / "openapi" / "openapi.json").read_text(encoding="utf-8"))


def _declared_fs09() -> dict[tuple[str, str], dict]:
    declared: dict[tuple[str, str], dict] = {}
    for path, item in _aggregate().get("paths", {}).items():
        for method, operation in item.items():
            if method in METHODS and "FS-09" in (operation.get("x-feature-set") or ""):
                declared[(method.upper(), path)] = operation
    return declared


def _mounted() -> set[tuple[str, str]]:
    mounted: set[tuple[str, str]] = set()
    for route in router.routes:
        methods = getattr(route, "methods", None) or set()
        for method in methods:
            if method.lower() in METHODS:
                mounted.add((method.upper(), route.path))
    return mounted


def _normalise(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "{}", path)


def test_every_declared_fs09_operation_is_mounted() -> None:
    declared = _declared_fs09()
    mounted = _mounted()
    missing = sorted(set(declared) - mounted)
    assert missing == [], missing
    assert set(declared) == mounted  # no extra/undeclared manufacturing routes either
    assert len(declared) == 198


def test_route_scopes_cover_every_route_and_are_catalogued() -> None:
    mounted = _mounted()
    assert set(ROUTE_SCOPES) == mounted
    assert len(set(ROUTE_SCOPES.values())) >= 10
    catalogue = {
        entry["scope"]
        for entry in json.loads((REPO_ROOT / "design" / "permissions.json").read_text(encoding="utf-8"))["catalogue"]
    }
    for (method, path), scope in ROUTE_SCOPES.items():
        assert scope in catalogue, (method, path, scope)
    declared = _declared_fs09()
    for (method, path), operation in declared.items():
        scopes = operation.get("x-required-scopes") or []
        assert scopes == [ROUTE_SCOPES[(method, path)]], (method, path, scopes)


GENUINE_GAP_OPERATIONS = {
    ("DELETE", "/api/v1/manufacturing/compat/rule-sets/{id}"),
    ("DELETE", "/api/v1/manufacturing/compat/rules/{id}"),
    ("GET", "/api/v1/ecos/{id}/lines"),
    ("GET", "/api/v1/manufacturing/compat/attributes/{id}"),
    ("GET", "/api/v1/manufacturing/compat/rule-sets/{id}/rules"),
    ("GET", "/api/v1/work-order-operations/{id}/completions"),
    ("PATCH", "/api/v1/boms/{id}"),
    ("PATCH", "/api/v1/routing-step-charges/{id}"),
    ("PATCH", "/api/v1/work-order-components/{id}"),
    ("PATCH", "/api/v1/work-order-operations/{id}"),
    ("POST", "/api/v1/assembly-builds/{id}/unbuild"),
    ("POST", "/api/v1/wip-transactions"),
    ("POST", "/api/v1/wip-transactions/{id}/post"),
    ("POST", "/api/v1/work-orders/mass-create"),
}


def test_canonical_id_aliases_keep_legacy_spellings_mounted() -> None:
    mounted = _mounted()
    canonical = [(method, path) for method, path in mounted if "{id}" in path]
    aliases = 0
    for method, path in canonical:
        assert path.count("{id}") == 1
        normalised = _normalise(path)
        legacy = sorted(
            other_path
            for other_method, other_path in mounted
            if other_method == method and other_path != path and _normalise(other_path) == normalised
        )
        if legacy:
            aliases += 1
            continue
        assert (method, path) in GENUINE_GAP_OPERATIONS, f"canonical route without legacy sibling: {method} {path}"
    assert aliases == 67  # every canonical `{id}` alias keeps its resource-specific legacy route
    gap_with_id = {operation for operation in GENUINE_GAP_OPERATIONS if "{id}" in operation[1]}
    assert len(gap_with_id) == 12
    assert set(canonical) == {operation for operation in mounted if "{id}" in operation[1]}


def test_genuine_gap_operations_are_mounted() -> None:
    mounted = _mounted()
    for operation in (
        ("DELETE", "/api/v1/manufacturing/compat/rule-sets/{id}"),
        ("DELETE", "/api/v1/manufacturing/compat/rules/{id}"),
        ("GET", "/api/v1/ecos/{id}/lines"),
        ("GET", "/api/v1/manufacturing/compat/attributes/{id}"),
        ("GET", "/api/v1/manufacturing/compat/rule-sets/{id}/rules"),
        ("GET", "/api/v1/work-order-operations/{id}/completions"),
        ("PATCH", "/api/v1/boms/{id}"),
        ("PATCH", "/api/v1/routing-step-charges/{id}"),
        ("PATCH", "/api/v1/work-order-components/{id}"),
        ("PATCH", "/api/v1/work-order-operations/{id}"),
        ("POST", "/api/v1/assembly-builds/{id}/unbuild"),
        ("POST", "/api/v1/wip-transactions"),
        ("POST", "/api/v1/wip-transactions/{id}/post"),
        ("POST", "/api/v1/work-orders/mass-create"),
    ):
        assert operation in mounted, operation
