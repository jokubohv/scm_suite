"""Deny-by-default route-scope coverage for the mounted FS-00 platform API (C0).

`DM-RBAC-01`: every non-public route declares exactly one catalogued capability scope (or is on an
explicit catalogue allowlist). This test reports — rather than edits — routes that violate the rule.
Known shortfalls are pinned so the suite stays meaningful: a new violation fails the test, and
removing a pinned one requires updating the freeze list. Set ``C0_STRICT_ROUTE_SCOPE=1`` to fail on
the pinned shortfalls too (used in evidence/C0 to show the residual blockers).
"""

from __future__ import annotations

import os

import contract_utils as utils


def _report(rows: list[utils.RouteRow]) -> dict[str, object]:
    scoped = [row for row in rows if not row.framework and not row.public and not row.self_service]
    return {
        "mounted_operations": len(rows),
        "scoped": len(scoped),
        "public": sum(1 for row in rows if row.public),
        "self_service": sum(1 for row in rows if row.self_service),
        "framework": sum(1 for row in rows if row.framework),
        "unknown_scopes": sorted({scope for row in rows for scope in row.scopes if scope not in utils.catalogue_scopes()}),
        "multi_scope": [row.key for row in rows if len(row.scopes) > 1],
        "duplicates": [
            key
            for key in {row.key for row in rows}
            if sum(1 for candidate in rows if candidate.key == key) > 1
        ],
        "unresolved": sorted(row.key for row in rows if row.unresolved and not row.framework),
        "scoped_without_scope": sorted(row.key for row in scoped if not row.scopes and not row.unresolved),
    }


def test_route_scope_coverage_report() -> None:
    rows = utils.route_matrix()
    report = _report(rows)
    print(f"route-scope coverage: {report}")
    assert report["duplicates"] == [], report
    assert report["unknown_scopes"] == [], report
    assert report["multi_scope"] == [], report
    assert report["scoped_without_scope"] == [], report
    # Every unresolved (non-framework) route must be a known, owner-assigned shortfall.
    assert set(report["unresolved"]) == set(utils.KNOWN_UNSCOPED_ROUTES), report
    for key, expected in utils.KNOWN_UNSCOPED_ROUTES.items():
        assert expected.get("owner") and expected.get("reason"), key
    if os.environ.get("C0_STRICT_ROUTE_SCOPE") == "1":
        assert report["unresolved"] == [], (
            "C0 strict route-scope coverage fails: unmapped routes "
            f"{report['unresolved']} (owners: { {k: v['owner'] for k, v in utils.KNOWN_UNSCOPED_ROUTES.items()} })"
        )


def test_scoped_routes_have_exactly_one_catalogued_scope() -> None:
    registry = utils.scope_registry()
    scopes = utils.catalogue_scopes()
    violations: list[str] = []
    for row in utils.route_matrix():
        if row.framework or row.public or row.self_service:
            continue
        if row.key in utils.KNOWN_UNSCOPED_ROUTES:
            # pinned residual gap (asserted separately in the coverage-report test)
            continue
        if len(row.scopes) != 1 or row.scopes[0] not in scopes:
            violations.append(f"{row.key}: scopes={row.scopes}")
        else:
            # route classified as SCOPED by the public/self-service allowlists only
            registry.classify_route(row.method, row.path)
    assert violations == [], violations


def test_public_and_self_service_routes_are_allowlisted() -> None:
    registry = utils.scope_registry()
    problems: list[str] = []
    for row in utils.route_matrix():
        if row.framework:
            continue
        if row.public and not registry.is_public_route(row.method, row.path):
            problems.append(f"{row.key}: public classification drift")
        if row.self_service and not registry.is_self_service_route(row.method, row.path):
            problems.append(f"{row.key}: self-service classification drift")
        if (row.public or row.self_service) and row.scopes:
            problems.append(f"{row.key}: allowlisted route also declares scope {row.scopes}")
    assert problems == [], problems


def test_contract_scope_alignment() -> None:
    """Implemented contract operations must publish the same single scope the router enforces."""
    fragment_ops = utils.fragment_operations(utils.platform_fragment())
    mismatches: list[str] = []
    missing_from_contract: list[str] = []
    for row in utils.route_matrix():
        if row.framework or row.unresolved or not row.in_schema:
            continue
        operation = fragment_ops.get((row.method, row.path))
        if operation is None:
            if row.key in utils.KNOWN_UNSCOPED_ROUTES:
                continue
            missing_from_contract.append(row.key)
            continue
        declared = tuple(operation.get("x-required-scopes") or ())
        if declared != row.scopes:
            mismatches.append(f"{row.key}: router={row.scopes} contract={declared}")
    assert missing_from_contract == [], missing_from_contract
    assert mismatches == [], mismatches
