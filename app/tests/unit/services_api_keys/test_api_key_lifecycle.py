"""Unit tests for API-key hashing/scopes/expiry/rotation primitives (task-43)."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

import pytest

from app.core.repositories.api_keys import (
    derive_api_key_pepper,
    is_key_active,
    public_key_row,
    validate_api_key_scopes,
    validate_expiry,
)
from app.core.security.errors import ScopeNotAssignable, SodConflict, UnknownScope
from app.core.security.scopes import ScopeRegistry
from app.core.security.tokens import ApiKeyHasher

NOW = datetime(2026, 10, 23, 0, 0, tzinfo=UTC)
REGISTRY = ScopeRegistry()


def test_hasher_stores_only_digest_and_verifies_constant_time() -> None:
    hasher = ApiKeyHasher("test-pepper-with-enough-entropy")
    material = hasher.generate()
    assert material.full_key == f"{material.key_prefix}.{material.secret}"
    assert material.secret not in material.secret_hash
    assert len(material.secret_hash) == 64  # SHA-256 hex
    assert hasher.verify(material.full_key, material.secret_hash) is True
    assert hasher.verify(f"{material.key_prefix}.wrong-secret-value-123456", material.secret_hash) is False
    assert hasher.split_prefix(material.full_key) == material.key_prefix


def test_derived_pepper_is_deterministic_and_domain_separated() -> None:
    first = derive_api_key_pepper("jwt-secret")
    second = derive_api_key_pepper("jwt-secret")
    assert first == second
    assert first != hashlib.sha256(b"jwt-secret").digest()
    with pytest.raises(ValueError):
        derive_api_key_pepper("")


def test_scope_validation_accepts_assignable_non_sod_scopes() -> None:
    scopes = validate_api_key_scopes(REGISTRY, ["items.read", "items.update"])
    assert scopes == ("items.read", "items.update")  # canonical, sorted, de-duplicated
    assert validate_api_key_scopes(REGISTRY, ["items.read", "items.read"]) == ("items.read",)


def test_scope_validation_rejects_unknown_platform_only_and_service_only() -> None:
    with pytest.raises(UnknownScope):
        validate_api_key_scopes(REGISTRY, ["not.a.real.scope"])
    with pytest.raises(ScopeNotAssignable):
        validate_api_key_scopes(REGISTRY, ["platform.tenant.manage"])
    service_only = next(scope.scope for scope in REGISTRY.catalogue.scopes if scope.service_only)
    with pytest.raises(ScopeNotAssignable):
        validate_api_key_scopes(REGISTRY, [service_only])
    with pytest.raises(UnknownScope):
        validate_api_key_scopes(REGISTRY, [])


def test_scope_validation_rejects_sod_pair() -> None:
    pair = REGISTRY.catalogue.sod_pairs[0]
    with pytest.raises(SodConflict):
        validate_api_key_scopes(REGISTRY, [pair.a, pair.b])


def test_expiry_validation_and_active_state() -> None:
    future = NOW + timedelta(days=1)
    assert validate_expiry(future, now=NOW) == future
    naive = datetime(2026, 10, 24, 0, 0)
    normalised = validate_expiry(naive, now=NOW)
    assert normalised is not None and normalised.tzinfo == UTC
    with pytest.raises(ValueError):
        validate_expiry(NOW - timedelta(seconds=1), now=NOW)
    assert is_key_active({"revoked_at": None, "expires_at": future}, now=NOW) is True
    assert is_key_active({"revoked_at": None, "expires_at": NOW - timedelta(seconds=1)}, now=NOW) is False
    assert is_key_active({"revoked_at": NOW, "expires_at": future}, now=NOW) is False
    assert is_key_active({"revoked_at": None, "expires_at": None}, now=NOW) is True


def test_public_serialisation_never_exposes_secret_material() -> None:
    row = {
        "id": "00000000-0000-0000-0000-000000000001", "name": "automation",
        "key_prefix": "ipk_1234", "scopes": ["items.read"], "created_at": NOW,
        "updated_at": NOW, "expires_at": None, "last_used_at": None, "revoked_at": None, "version": 1,
        "secret_hash": "must-not-leak",
    }
    payload = public_key_row(row, now=NOW)
    assert payload["revoked"] is False and payload["expired"] is False
    assert "secret" not in payload and "secret_hash" not in payload
    assert payload["id"] == row["id"]
