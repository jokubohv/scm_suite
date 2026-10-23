"""Argon2id password hashing and rehash-on-login policy (pure; FS-00 platform security).

Policy defaults follow OWASP's Argon2id guidance for interactive logins (m=64 MiB, t=3, p=4) and are
exposed as an immutable :class:`PasswordPolicy` so tenants/deployments can tune cost without changing
call sites. The verifier never reveals *why* a password failed and a dummy-hash path keeps timing
similar for unknown users.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from app.core.security.errors import InvalidCredentials

MIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_LENGTH = 1024


@dataclass(frozen=True, slots=True)
class PasswordPolicy:
    """Argon2id parameters + candidate policy (validated on construction)."""

    time_cost: int = 3
    memory_cost_kib: int = 65536
    parallelism: int = 4
    hash_len: int = 32
    salt_len: int = 16
    min_length: int = MIN_PASSWORD_LENGTH
    max_length: int = MAX_PASSWORD_LENGTH

    def __post_init__(self) -> None:
        if self.time_cost < 1:
            raise ValueError("time_cost must be >= 1")
        if self.memory_cost_kib < 8 * 1024:
            raise ValueError("memory_cost_kib must be >= 8192 (8 MiB)")
        if self.parallelism < 1:
            raise ValueError("parallelism must be >= 1")
        if not 0 < self.min_length <= self.max_length:
            raise ValueError("invalid min/max password length")

    def validate_candidate(self, password: str) -> None:
        """Raise :class:`InvalidCredentials` when a candidate fails the policy (never echo it)."""
        if not isinstance(password, str) or len(password) < self.min_length:
            raise InvalidCredentials("password does not meet the minimum length policy")
        if len(password) > self.max_length:
            raise InvalidCredentials("password exceeds the maximum length policy")

    def to_argon2(self) -> PasswordHasher:
        return PasswordHasher(
            time_cost=self.time_cost,
            memory_cost=self.memory_cost_kib,
            parallelism=self.parallelism,
            hash_len=self.hash_len,
            salt_len=self.salt_len,
            # argon2-cffi defaults to argon2.Type.ID (Argon2id) — keep the library default.
        )


@dataclass(frozen=True, slots=True)
class PasswordHash:
    """Encoded Argon2id hash + the algorithm id for storage/audit."""

    encoded: str
    algorithm: str = "argon2id"


@dataclass(frozen=True, slots=True)
class PasswordVerification:
    """Result of ``verify_and_update``: valid? and a replacement hash when rehashing is due."""

    valid: bool
    needs_rehash: bool = False
    new_hash: str | None = None


class PasswordService:
    """Hash/verify/rehash service (no storage; callers persist the encoded string)."""

    def __init__(self, policy: PasswordPolicy | None = None) -> None:
        self.policy = policy or PasswordPolicy()
        self._hasher = self.policy.to_argon2()
        # Pre-computed dummy hash for unknown-user timing equalisation (never a real credential).
        self._dummy_hash = self._hasher.hash("dummy-password-for-timing-equalisation-only")

    def hash(self, password: str) -> str:
        """Validate against policy, then return an Argon2id encoded hash (never log the password)."""
        self.policy.validate_candidate(password)
        return self._hasher.hash(password)

    def hash_password(self, password: str) -> PasswordHash:
        return PasswordHash(encoded=self.hash(password))

    def verify(self, password: str, encoded: str | None) -> bool:
        """Constant-shape verification; ``False`` for wrong passwords *and* malformed/missing hashes."""
        if not encoded:
            self._verify_dummy(password)
            return False
        try:
            return bool(self._hasher.verify(encoded, password))
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            return False

    def verify_and_update(self, password: str, encoded: str | None) -> PasswordVerification:
        """Verify and, when the stored hash predates the current policy, return a rehash-on-login value."""
        if not encoded or not self.verify(password, encoded):
            return PasswordVerification(valid=False)
        if self.needs_rehash(encoded):
            return PasswordVerification(valid=True, needs_rehash=True, new_hash=self.hash(password))
        return PasswordVerification(valid=True)

    def needs_rehash(self, encoded: str | None) -> bool:
        """True when the encoded hash is invalid or weaker than the current policy parameters."""
        if not encoded:
            return True
        try:
            return bool(self._hasher.check_needs_rehash(encoded))
        except (InvalidHashError, VerificationError):
            return True

    def rehash_if_needed(self, password: str, encoded: str | None) -> str | None:
        """Convenience hook for login: return a new hash only when a valid login needs rehashing."""
        result = self.verify_and_update(password, encoded)
        return result.new_hash

    def _verify_dummy(self, password: str) -> None:
        """Burn similar work for unknown users so login timing does not reveal account existence."""
        try:
            self._hasher.verify(self._dummy_hash, password)
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            return

    @staticmethod
    def describe() -> dict[str, Any]:
        """Non-secret policy description for `/internal/deps`/diagnostics."""
        policy = PasswordPolicy()
        return {
            "algorithm": "argon2id",
            "time_cost": policy.time_cost,
            "memory_cost_kib": policy.memory_cost_kib,
            "parallelism": policy.parallelism,
            "min_length": policy.min_length,
        }


__all__ = [
    "MAX_PASSWORD_LENGTH",
    "MIN_PASSWORD_LENGTH",
    "PasswordHash",
    "PasswordPolicy",
    "PasswordService",
    "PasswordVerification",
]
