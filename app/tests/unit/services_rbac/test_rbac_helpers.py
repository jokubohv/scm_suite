"""Unit tests for RBAC/admin router+repository pure helpers (no DB)."""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.api.routers.platform.rbac import _role_payload, _validate_setting
from app.core.repositories.rbac import ALLOWED_SETTING_NAMESPACES, hash_invitation_token
from app.core.security.errors import PrivilegeEscalation, SodConflict
from app.core.security.scopes import EffectivePermissions, ScopeRegistry


def test_catalogue_counts_and_templates() -> None:
    registry = ScopeRegistry()
    assert len(registry.catalogue.scopes) == 255
    assert len(registry.catalogue.assignable_scopes()) == 241
    assert len(registry.catalogue.role_templates) == 7
    assert len(registry.catalogue.sod_pairs) == 14
    assert registry.describe()["platform_only"] == 1
    assert registry.describe()["service_only"] == 13


def test_assignability_and_privilege_guard() -> None:
    registry = ScopeRegistry()
    with pytest.raises(Exception) as exc:
        registry.require_assignable(["platform.tenant.manage"])
    assert getattr(exc.value, "code", "") == "SCOPE_NOT_ASSIGNABLE"
    actor = EffectivePermissions(scopes=frozenset({"platform.role.manage"}))
    with pytest.raises(PrivilegeEscalation):
        registry.require_can_grant(actor, ["items.update"])
    # platform admin bypasses the escalation guard (global authority)
    registry.require_can_grant(actor, ["items.update"], is_platform_admin=True)


def test_sod_helpers() -> None:
    registry = ScopeRegistry()
    pair = registry.catalogue.sod_pairs[0]
    with pytest.raises(SodConflict):
        registry.require_no_sod_conflict([pair.a, pair.b])
    # admin break-glass may contain SoD pairs (audited by callers)
    registry.require_no_sod_conflict([pair.a, pair.b], allow_break_glass=True)


def test_last_admin_guard_helper() -> None:
    registry = ScopeRegistry()
    registry.require_not_last_admin(was_active_admin=True, will_remain_active_admin=True, active_admin_count=1)
    with pytest.raises(Exception) as exc:
        registry.require_not_last_admin(was_active_admin=True, will_remain_active_admin=False, active_admin_count=1)
    assert getattr(exc.value, "code", "") == "LAST_TENANT_ADMIN"


def test_invitation_token_hash_is_deterministic_and_single_use_material() -> None:
    token = "invite-token-value"
    assert hash_invitation_token(token) == hash_invitation_token(token)
    assert hash_invitation_token(token) != hash_invitation_token(token + "x")
    assert len(hash_invitation_token(token)) == 64


@pytest.mark.parametrize(
    "namespace,key,value,value_type",
    [
        ("rbac", "sod_enforcement", "WARN", "STRING"),
        ("inventory", "default_lead_time_days", 7, "NUMBER"),
        ("wms", "pick_sequence", "FIFO", "STRING"),
    ],
)
def test_validate_setting_accepts(namespace: str, key: str, value: object, value_type: str) -> None:
    assert namespace in ALLOWED_SETTING_NAMESPACES
    _validate_setting(namespace, key, value, value_type)


def test_validate_setting_rejects_bad_inputs() -> None:
    with pytest.raises(Exception) as exc:
        _validate_setting("nope", "key", 1, "NUMBER")
    assert getattr(exc.value, "status_code", None) == 422
    with pytest.raises(HTTPException):
        _validate_setting("rbac", "sod_enforcement", "OFF", "STRING")
    with pytest.raises(HTTPException):
        _validate_setting("inventory", "Bad Key", 1, "NUMBER")


def test_role_payload_shape() -> None:
    role = {
        "id": "r1", "code": "custom", "name": "Custom", "description": "d", "is_template": False,
        "is_editable": True, "inactive_at": None, "scopes": ["items.read"],
        "data_scopes": [], "assignment_count": 0,
    }
    payload = _role_payload(role)
    assert payload["scopes"] == ["items.read"] and payload["inactive"] is False
