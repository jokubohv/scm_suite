"""The RBAC catalogue is loaded from the authoritative plan artifact and validated strictly."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.seed.constants import (
    DEFAULT_PERMISSIONS_SOURCE,
    DOCUMENTED_ROLE_SIZES,
    EXPECTED_CATALOGUE_COUNTS,
)
from app.seed.permissions import (
    SeedValidationError,
    load_permissions,
    resolve_permissions_source,
    validate_permissions_payload,
)


def test_authoritative_permissions_source_exists() -> None:
    assert DEFAULT_PERMISSIONS_SOURCE.is_file(), f"missing authoritative catalogue: {DEFAULT_PERMISSIONS_SOURCE}"
    assert resolve_permissions_source() == DEFAULT_PERMISSIONS_SOURCE


def test_catalogue_counts_match_plan() -> None:
    catalogue = load_permissions()
    counts = catalogue.counts()
    for key, expected in EXPECTED_CATALOGUE_COUNTS.items():
        assert counts[key] == expected
    assert len(catalogue.assignable_scopes) == 241
    assert len(catalogue.service_only_scopes) == 13
    assert len(catalogue.platform_only_scopes) == 1
    assert len(catalogue.approval_scopes) == 18


def test_role_templates_are_assignable_and_sized_as_documented() -> None:
    catalogue = load_permissions()
    assert len(catalogue.roles) == EXPECTED_CATALOGUE_COUNTS["roles"]
    assert [role.code for role in catalogue.roles] == sorted(DOCUMENTED_ROLE_SIZES)
    for role in catalogue.roles:
        assert role.system is True
        assert role.editable is False
        assert role.cloneable is True
        assert set(role.scopes) <= catalogue.assignable_scopes
        assert len(role.scopes) == DOCUMENTED_ROLE_SIZES[role.code]
    # admin is the break-glass template containing every assignable tenant scope.
    assert set(catalogue.role("admin").scopes) == catalogue.assignable_scopes


def test_sod_pairs_are_catalogued_and_non_admin_roles_are_clean() -> None:
    catalogue = load_permissions()
    assert len(catalogue.sod_pairs) == EXPECTED_CATALOGUE_COUNTS["sod_pairs"]
    known = catalogue.permission_by_scope
    for pair in catalogue.sod_pairs:
        assert pair.a in known and pair.b in known
        assert pair.enforcement == "HARD"
        for role in catalogue.roles:
            if role.code == "admin":
                continue
            assert not {pair.a, pair.b} <= set(role.scopes), f"{role.code} combines SoD pair {pair.a}/{pair.b}"


def test_alias_is_not_seeded_and_points_at_real_scope() -> None:
    catalogue = load_permissions()
    assert catalogue.aliases
    for legacy, target in catalogue.aliases.items():
        assert legacy not in catalogue.permission_by_scope
        assert target in catalogue.permission_by_scope


def test_seed_rows_are_sorted_and_complete() -> None:
    catalogue = load_permissions()
    permission_rows = catalogue.permission_rows()
    role_permission_rows = catalogue.role_permission_rows()
    assert len(permission_rows) == 255
    assert len(role_permission_rows) == sum(DOCUMENTED_ROLE_SIZES.values())
    assert permission_rows == sorted(permission_rows, key=lambda row: row["scope"])
    assert role_permission_rows == sorted(role_permission_rows, key=lambda row: (row["role_code"], row["scope"]))


def test_validation_rejects_drifted_counts_and_bad_roles(tmp_path: Path) -> None:
    raw = json.loads(DEFAULT_PERMISSIONS_SOURCE.read_text(encoding="utf-8"))
    drifted = json.loads(json.dumps(raw))
    drifted["counts"]["scopes"] = 254
    errors, _ = validate_permissions_payload(drifted)
    assert any("declared counts.scopes" in error for error in errors)

    broken = json.loads(json.dumps(raw))
    broken["role_templates"][1]["scopes"].append("does.not.exist")
    errors, _ = validate_permissions_payload(broken)
    assert any("unknown scopes" in error for error in errors)

    duplicate = json.loads(json.dumps(raw))
    duplicate["catalogue"].append(dict(duplicate["catalogue"][0]))
    errors, _ = validate_permissions_payload(duplicate)
    assert any("duplicate catalogue scopes" in error for error in errors)

    bad_file = tmp_path / "permissions.json"
    bad_file.write_text("{}", encoding="utf-8")
    with pytest.raises(SeedValidationError):
        load_permissions(bad_file)
