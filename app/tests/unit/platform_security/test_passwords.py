"""Argon2id password service tests (pure)."""

from __future__ import annotations

import pytest

from app.core.security.errors import InvalidCredentials
from app.core.security.passwords import (
    MIN_PASSWORD_LENGTH,
    PasswordPolicy,
    PasswordService,
)


@pytest.fixture(scope="module")
def service() -> PasswordService:
    # Cheaper parameters keep the unit suite fast while still exercising Argon2id.
    return PasswordService(PasswordPolicy(time_cost=1, memory_cost_kib=8192, parallelism=1))


def test_hash_is_argon2id_and_verifies(service: PasswordService) -> None:
    encoded = service.hash("correct horse battery staple")
    assert encoded.startswith("$argon2id$")
    assert service.verify("correct horse battery staple", encoded) is True
    assert service.verify("wrong password value", encoded) is False


def test_hash_is_salted_and_unique(service: PasswordService) -> None:
    first = service.hash("same password value")
    second = service.hash("same password value")
    assert first != second
    assert service.verify("same password value", first)
    assert service.verify("same password value", second)


def test_missing_or_malformed_hash_fails_closed(service: PasswordService) -> None:
    assert service.verify("anything", None) is False
    assert service.verify("anything", "") is False
    assert service.verify("anything", "not-a-hash") is False
    assert service.needs_rehash(None) is True
    assert service.needs_rehash("not-a-hash") is True


def test_password_policy_rejects_short_candidates(service: PasswordService) -> None:
    with pytest.raises(InvalidCredentials):
        service.hash("short")
    with pytest.raises(ValueError):
        PasswordPolicy(min_length=0)
    with pytest.raises(ValueError):
        PasswordPolicy(memory_cost_kib=1024)


def test_rehash_on_login_hook(service: PasswordService) -> None:
    weak = PasswordService(PasswordPolicy(time_cost=1, memory_cost_kib=8192, parallelism=1)).hash("long enough password")
    stronger = PasswordService(PasswordPolicy(time_cost=2, memory_cost_kib=8192, parallelism=1))
    assert stronger.needs_rehash(weak) is True
    result = stronger.verify_and_update("long enough password", weak)
    assert result.valid is True
    assert result.needs_rehash is True
    assert result.new_hash is not None and result.new_hash.startswith("$argon2id$")
    assert stronger.verify("long enough password", result.new_hash)
    # A fresh hash at current policy does not rehash.
    fresh = stronger.hash("long enough password")
    assert stronger.rehash_if_needed("long enough password", fresh) is None


def test_policy_description_has_no_secrets() -> None:
    description = PasswordService.describe()
    assert description["algorithm"] == "argon2id"
    assert description["min_length"] >= MIN_PASSWORD_LENGTH
    assert "secret" not in str(description).lower()
