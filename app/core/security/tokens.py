"""Platform token services (pure): JWT access/refresh/selection tokens, rotation and API keys.

Design authority: ARCHITECTURE §5.2, FS-00 §5.2/§6, Δ-MT §3, Δ-SSO §4.

* Access token (15 min): ``sub``=identity_id, ``uid``=app_user_id, ``tid``=tenant_id,
  ``tv``=token_version, ``jti``, ``iat``/``exp``/``iss``/``aud``, ``typ=access``, compact ``scopes``.
* Refresh token (30 days): a **JWT** carrying a server-side ``jti`` + ``sid`` (session id); the caller
  stores it hashed and rotates it. Re-use of an already-rotated ``jti`` is replay → session revoked.
* Selection token (5 min): identity-scoped, single-use; grants no tenant access by itself.
* API keys: ``"<prefix>.<secret>"``; only an HMAC-SHA256(secret, pepper) digest is stored, compared with
  ``hmac.compare_digest``. Prefixes are indexed for lookup; secrets are never logged.

The module is pure: no DB/network imports. Signer/key material is injected (HS256 dev secret or an
RS256-ready key reference), and time comes from an injected clock so tests are deterministic.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Protocol

import jwt
from jwt.exceptions import ExpiredSignatureError, InvalidAudienceError, InvalidIssuerError, InvalidTokenError

from app.core.security.errors import (
    CrossTenantAccessDenied,
    InvalidCredentials,
    MembershipInactive,
    SessionRevoked,
    TenantSuspended,
    TokenExpired,
    TokenInvalid,
    TokenVersionStale,
)

Clock = Callable[[], datetime]
JtiFactory = Callable[[], str]

MIN_HMAC_SECRET_BYTES = 32
ACCESS_TTL_SECONDS = 15 * 60
REFRESH_TTL_SECONDS = 30 * 24 * 60 * 60
SELECTION_TTL_SECONDS = 5 * 60


def utc_now() -> datetime:
    return datetime.now(UTC)


def ensure_utc(moment: datetime) -> datetime:
    """Require timezone-aware datetimes (all token math is UTC)."""
    if moment.tzinfo is None:
        raise ValueError("token timestamps must be timezone-aware (UTC)")
    return moment.astimezone(UTC)


def _default_jti() -> str:
    return uuid.uuid4().hex


class TokenType(StrEnum):
    ACCESS = "access"
    REFRESH = "refresh"
    SELECTION = "selection"


@dataclass(frozen=True, slots=True)
class TokenConfig:
    """Issuer/audience/TTL policy (no secrets; deployment may override TTLs)."""

    issuer: str = "inventory-platform"
    audience: str = "inventory-platform-api"
    access_ttl_seconds: int = ACCESS_TTL_SECONDS
    refresh_ttl_seconds: int = REFRESH_TTL_SECONDS
    selection_ttl_seconds: int = SELECTION_TTL_SECONDS
    leeway_seconds: int = 0

    def __post_init__(self) -> None:
        if not self.issuer or not self.audience:
            raise ValueError("issuer and audience are required")
        for name in ("access_ttl_seconds", "refresh_ttl_seconds", "selection_ttl_seconds"):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")
        if self.leeway_seconds < 0:
            raise ValueError("leeway_seconds must be >= 0")


# --------------------------------------------------------------------------- signers ------------


class SigningKeyRef(Protocol):
    """RS256-ready key reference interface (secret manager resolution happens outside this module)."""

    @property
    def algorithm(self) -> str: ...

    @property
    def key_id(self) -> str | None: ...

    def private_key(self) -> str | bytes: ...

    def public_key(self) -> str | bytes: ...


@dataclass(frozen=True, slots=True)
class StaticSigningKeyRef:
    """A resolved key pair (used by dev HS256 and by tests; production resolves refs at startup)."""

    algorithm: str
    private_key_pem: str | bytes
    public_key_pem: str | bytes | None = None
    key_id: str | None = None

    def private_key(self) -> str | bytes:
        return self.private_key_pem

    def public_key(self) -> str | bytes:
        return self.public_key_pem if self.public_key_pem is not None else self.private_key_pem


class TokenSigner(Protocol):
    """Sign/verify interface (HS256 dev + RS256-ready asymmetric implementations)."""

    @property
    def algorithm(self) -> str: ...

    @property
    def key_id(self) -> str | None: ...

    def sign(self, claims: Mapping[str, Any]) -> str: ...

    def verify(self, token: str, *, audience: str, issuer: str, leeway: int = 0) -> dict[str, Any]: ...


class HmacTokenSigner:
    """Symmetric HS256/384/512 signer for development and single-service deployments."""

    def __init__(self, secret: str | bytes, *, algorithm: str = "HS256", key_id: str | None = None) -> None:
        if not secret:
            raise ValueError("HMAC signing secret is required")
        secret_bytes = secret.encode("utf-8") if isinstance(secret, str) else secret
        if len(secret_bytes) < MIN_HMAC_SECRET_BYTES:
            raise ValueError(f"HMAC signing secret must be >= {MIN_HMAC_SECRET_BYTES} bytes (RFC 7518 §3.2)")
        self._secret = secret_bytes
        self._algorithm = algorithm
        self._key_id = key_id

    @property
    def algorithm(self) -> str:
        return self._algorithm

    @property
    def key_id(self) -> str | None:
        return self._key_id

    def sign(self, claims: Mapping[str, Any]) -> str:
        headers = {"kid": self._key_id} if self._key_id else None
        return jwt.encode(dict(claims), self._secret, algorithm=self._algorithm, headers=headers)

    def verify(self, token: str, *, audience: str, issuer: str, leeway: int = 0) -> dict[str, Any]:
        # Time claims are validated against the injected clock by TokenService (deterministic tests).
        return jwt.decode(
            token,
            self._secret,
            algorithms=[self._algorithm],
            audience=audience,
            issuer=issuer,
            leeway=leeway,
            options={
                "require": ["exp", "iat", "iss", "aud"],
                "verify_exp": False,
                "verify_nbf": False,
                "verify_iat": False,
            },
        )


class AsymmetricTokenSigner:
    """RS256/RS384/RS512 signer backed by PEM key material (RS256-ready production interface)."""

    def __init__(
        self,
        private_key_pem: str | bytes,
        *,
        public_key_pem: str | bytes | None = None,
        algorithm: str = "RS256",
        key_id: str | None = None,
    ) -> None:
        self._private_key = private_key_pem
        self._public_key = public_key_pem or private_key_pem
        self._algorithm = algorithm
        self._key_id = key_id

    @classmethod
    def from_ref(cls, key_ref: SigningKeyRef) -> AsymmetricTokenSigner:
        return cls(
            key_ref.private_key(),
            public_key_pem=key_ref.public_key(),
            algorithm=key_ref.algorithm,
            key_id=getattr(key_ref, "key_id", None),
        )

    @property
    def algorithm(self) -> str:
        return self._algorithm

    @property
    def key_id(self) -> str | None:
        return self._key_id

    def sign(self, claims: Mapping[str, Any]) -> str:
        headers = {"kid": self._key_id} if self._key_id else None
        return jwt.encode(dict(claims), self._private_key, algorithm=self._algorithm, headers=headers)

    def verify(self, token: str, *, audience: str, issuer: str, leeway: int = 0) -> dict[str, Any]:
        # Time claims are validated against the injected clock by TokenService (deterministic tests).
        return jwt.decode(
            token,
            self._public_key,
            algorithms=[self._algorithm],
            audience=audience,
            issuer=issuer,
            leeway=leeway,
            options={
                "require": ["exp", "iat", "iss", "aud"],
                "verify_exp": False,
                "verify_nbf": False,
                "verify_iat": False,
            },
        )


def signer_from_key_ref(key_ref: SigningKeyRef) -> TokenSigner:
    """Build the signer matching a key reference algorithm (HS* → HMAC, RS* → asymmetric)."""
    algorithm = key_ref.algorithm.upper()
    if algorithm.startswith("HS"):
        return HmacTokenSigner(key_ref.private_key(), algorithm=algorithm, key_id=getattr(key_ref, "key_id", None))
    if algorithm.startswith(("RS", "ES")):
        return AsymmetricTokenSigner.from_ref(key_ref)
    raise ValueError(f"unsupported signing algorithm {algorithm!r}")


# --------------------------------------------------------------------------- claims ------------


@dataclass(frozen=True, slots=True)
class AccessTokenClaims:
    sub: str
    uid: str
    tid: str
    tv: int
    jti: str
    scopes: frozenset[str]
    iat: datetime
    exp: datetime
    iss: str
    aud: str
    typ: str = TokenType.ACCESS.value


@dataclass(frozen=True, slots=True)
class RefreshTokenClaims:
    sub: str
    uid: str
    tid: str
    tv: int
    jti: str
    sid: str
    iat: datetime
    exp: datetime
    iss: str
    aud: str
    typ: str = TokenType.REFRESH.value


@dataclass(frozen=True, slots=True)
class SelectionTokenClaims:
    sub: str
    jti: str
    iat: datetime
    exp: datetime
    iss: str
    aud: str
    typ: str = TokenType.SELECTION.value


@dataclass(frozen=True, slots=True)
class TokenPair:
    access_token: str
    refresh_token: str
    access_claims: AccessTokenClaims
    refresh_claims: RefreshTokenClaims

    @property
    def access_expires_at(self) -> datetime:
        return self.access_claims.exp

    @property
    def refresh_expires_at(self) -> datetime:
        return self.refresh_claims.exp

    @property
    def token_type(self) -> str:
        return "Bearer"

    @property
    def expires_in(self) -> int:
        return max(0, int((self.access_claims.exp - self.access_claims.iat).total_seconds()))


def _as_datetime(epoch_seconds: Any) -> datetime:
    if not isinstance(epoch_seconds, int | float):
        raise TokenInvalid("token timestamp claim is not numeric")
    return datetime.fromtimestamp(float(epoch_seconds), tz=UTC)


def _require_str(claims: Mapping[str, Any], key: str) -> str:
    value = claims.get(key)
    if not isinstance(value, str) or not value:
        raise TokenInvalid(f"token claim {key!r} is missing or invalid")
    return value


def _require_int(claims: Mapping[str, Any], key: str) -> int:
    value = claims.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise TokenInvalid(f"token claim {key!r} is missing or invalid")
    return value


# --------------------------------------------------------------------------- service -----------


class RefreshRotationStore(Protocol):
    """Persistence port for refresh-token rotation/replay detection (DB/Redis adapter elsewhere).

    Implementations must make ``consume`` atomic: it returns ``False`` only when the ``(session, jti)``
    pair was already consumed (replay), and that call site revokes the session.
    """

    def consume(self, session_id: str, jti: str, *, expires_at: datetime) -> bool: ...

    def revoke_session(self, session_id: str) -> None: ...

    def is_session_revoked(self, session_id: str) -> bool: ...


class InMemoryRefreshRotationStore:
    """Deterministic in-memory store for unit tests (not for production use)."""

    def __init__(self) -> None:
        self._consumed: dict[str, set[str]] = {}
        self._revoked: set[str] = set()

    def consume(self, session_id: str, jti: str, *, expires_at: datetime) -> bool:
        del expires_at  # in-memory store is test-scoped; TTL is enforced by the durable adapter
        seen = self._consumed.setdefault(session_id, set())
        if jti in seen:
            return False
        seen.add(jti)
        return True

    def revoke_session(self, session_id: str) -> None:
        self._revoked.add(session_id)

    def is_session_revoked(self, session_id: str) -> bool:
        return session_id in self._revoked


class TokenService:
    """Issue/verify platform tokens; all time and signer material are injected."""

    def __init__(
        self,
        signer: TokenSigner,
        *,
        config: TokenConfig | None = None,
        clock: Clock = utc_now,
        jti_factory: JtiFactory = _default_jti,
    ) -> None:
        self._signer = signer
        self.config = config or TokenConfig()
        self._clock = clock
        self._jti = jti_factory

    def now(self) -> datetime:
        return ensure_utc(self._clock())

    # -- issue -------------------------------------------------------------------------------

    def issue_access_token(
        self,
        *,
        identity_id: str,
        app_user_id: str,
        tenant_id: str,
        token_version: int,
        scopes: frozenset[str] | set[str] | tuple[str, ...],
        now: datetime | None = None,
        ttl_seconds: int | None = None,
        jti: str | None = None,
    ) -> tuple[str, AccessTokenClaims]:
        issued_at = ensure_utc(now or self.now())
        expires_at = issued_at + timedelta(seconds=ttl_seconds or self.config.access_ttl_seconds)
        claims = AccessTokenClaims(
            sub=str(identity_id),
            uid=str(app_user_id),
            tid=str(tenant_id),
            tv=int(token_version),
            jti=jti or self._jti(),
            scopes=frozenset(scopes),
            iat=issued_at,
            exp=expires_at,
            iss=self.config.issuer,
            aud=self.config.audience,
        )
        return self._signer.sign(self._access_payload(claims)), claims

    def issue_refresh_token(
        self,
        *,
        identity_id: str,
        app_user_id: str,
        tenant_id: str,
        token_version: int,
        session_id: str,
        now: datetime | None = None,
        jti: str | None = None,
    ) -> tuple[str, RefreshTokenClaims]:
        issued_at = ensure_utc(now or self.now())
        claims = RefreshTokenClaims(
            sub=str(identity_id),
            uid=str(app_user_id),
            tid=str(tenant_id),
            tv=int(token_version),
            jti=jti or self._jti(),
            sid=str(session_id),
            iat=issued_at,
            exp=issued_at + timedelta(seconds=self.config.refresh_ttl_seconds),
            iss=self.config.issuer,
            aud=self.config.audience,
        )
        return self._signer.sign(self._refresh_payload(claims)), claims

    def issue_token_pair(
        self,
        *,
        identity_id: str,
        app_user_id: str,
        tenant_id: str,
        token_version: int,
        scopes: frozenset[str] | set[str] | tuple[str, ...],
        session_id: str,
        now: datetime | None = None,
    ) -> TokenPair:
        issued_at = ensure_utc(now or self.now())
        access_token, access_claims = self.issue_access_token(
            identity_id=identity_id,
            app_user_id=app_user_id,
            tenant_id=tenant_id,
            token_version=token_version,
            scopes=scopes,
            now=issued_at,
        )
        refresh_token, refresh_claims = self.issue_refresh_token(
            identity_id=identity_id,
            app_user_id=app_user_id,
            tenant_id=tenant_id,
            token_version=token_version,
            session_id=session_id,
            now=issued_at,
        )
        return TokenPair(access_token=access_token, refresh_token=refresh_token, access_claims=access_claims, refresh_claims=refresh_claims)

    def issue_selection_token(
        self,
        *,
        identity_id: str,
        now: datetime | None = None,
        jti: str | None = None,
    ) -> tuple[str, SelectionTokenClaims]:
        issued_at = ensure_utc(now or self.now())
        claims = SelectionTokenClaims(
            sub=str(identity_id),
            jti=jti or self._jti(),
            iat=issued_at,
            exp=issued_at + timedelta(seconds=self.config.selection_ttl_seconds),
            iss=self.config.issuer,
            aud=self.config.audience,
        )
        payload = {
            "iss": claims.iss,
            "aud": claims.aud,
            "sub": claims.sub,
            "jti": claims.jti,
            "iat": int(claims.iat.timestamp()),
            "exp": int(claims.exp.timestamp()),
            "typ": claims.typ,
        }
        return self._signer.sign(payload), claims

    # -- decode ------------------------------------------------------------------------------

    def decode_access_token(self, token: str, *, now: datetime | None = None) -> AccessTokenClaims:
        payload = self._decode(token, expected_type=TokenType.ACCESS, now=now or self.now())
        iat = _as_datetime(payload.get("iat"))
        exp = _as_datetime(payload.get("exp"))
        scopes_raw = payload.get("scopes", [])
        if not isinstance(scopes_raw, list) or not all(isinstance(item, str) for item in scopes_raw):
            raise TokenInvalid("token claim 'scopes' must be a list of strings")
        return AccessTokenClaims(
            sub=_require_str(payload, "sub"),
            uid=_require_str(payload, "uid"),
            tid=_require_str(payload, "tid"),
            tv=_require_int(payload, "tv"),
            jti=_require_str(payload, "jti"),
            scopes=frozenset(scopes_raw),
            iat=iat,
            exp=exp,
            iss=_require_str(payload, "iss"),
            aud=_require_str(payload, "aud"),
        )

    def decode_refresh_token(self, token: str, *, now: datetime | None = None) -> RefreshTokenClaims:
        payload = self._decode(token, expected_type=TokenType.REFRESH, now=now or self.now())
        return RefreshTokenClaims(
            sub=_require_str(payload, "sub"),
            uid=_require_str(payload, "uid"),
            tid=_require_str(payload, "tid"),
            tv=_require_int(payload, "tv"),
            jti=_require_str(payload, "jti"),
            sid=_require_str(payload, "sid"),
            iat=_as_datetime(payload.get("iat")),
            exp=_as_datetime(payload.get("exp")),
            iss=_require_str(payload, "iss"),
            aud=_require_str(payload, "aud"),
        )

    def decode_selection_token(self, token: str, *, now: datetime | None = None) -> SelectionTokenClaims:
        payload = self._decode(token, expected_type=TokenType.SELECTION, now=now or self.now())
        return SelectionTokenClaims(
            sub=_require_str(payload, "sub"),
            jti=_require_str(payload, "jti"),
            iat=_as_datetime(payload.get("iat")),
            exp=_as_datetime(payload.get("exp")),
            iss=_require_str(payload, "iss"),
            aud=_require_str(payload, "aud"),
        )

    # -- validate / rotate -------------------------------------------------------------------

    def validate_access_claims(
        self,
        claims: AccessTokenClaims,
        *,
        expected_token_version: int,
        membership_status: str = "ACTIVE",
        tenant_status: str = "ACTIVE",
        expected_identity_id: str | None = None,
        expected_tenant_id: str | None = None,
    ) -> None:
        """Revalidate a decoded access token against the current principal state (per-request)."""
        if expected_identity_id is not None and claims.sub != expected_identity_id:
            raise TokenInvalid("token identity does not match the resource owner")
        if expected_tenant_id is not None and claims.tid != expected_tenant_id:
            raise CrossTenantAccessDenied("token tenant does not match the requested tenant")
        if claims.tv != int(expected_token_version):
            raise TokenVersionStale("token version is stale; sign in again")
        if membership_status.upper() != "ACTIVE":
            raise MembershipInactive("membership is not active")
        if tenant_status.upper() != "ACTIVE":
            raise TenantSuspended("tenant is not active")

    def validate_refresh_claims(
        self,
        claims: RefreshTokenClaims,
        *,
        expected_token_version: int,
        session_active: bool = True,
    ) -> None:
        if claims.tv != int(expected_token_version):
            raise TokenVersionStale("refresh token version is stale; sign in again")
        if not session_active:
            raise SessionRevoked("session is revoked")

    def rotate_refresh_token(
        self,
        refresh_claims: RefreshTokenClaims,
        *,
        store: RefreshRotationStore,
        expected_token_version: int,
        scopes: frozenset[str] | set[str] | tuple[str, ...],
        session_active: bool = True,
        now: datetime | None = None,
    ) -> TokenPair:
        """Rotate a refresh token exactly once; replay detection revokes the session (single use)."""
        self.validate_refresh_claims(refresh_claims, expected_token_version=expected_token_version, session_active=session_active)
        if store.is_session_revoked(refresh_claims.sid) or not store.consume(
            refresh_claims.sid, refresh_claims.jti, expires_at=refresh_claims.exp
        ):
            store.revoke_session(refresh_claims.sid)
            raise SessionRevoked("refresh token replay detected; session revoked")
        return self.issue_token_pair(
            identity_id=refresh_claims.sub,
            app_user_id=refresh_claims.uid,
            tenant_id=refresh_claims.tid,
            token_version=expected_token_version,
            scopes=scopes,
            session_id=refresh_claims.sid,
            now=now or self.now(),
        )

    # -- helpers -----------------------------------------------------------------------------

    @staticmethod
    def _access_payload(claims: AccessTokenClaims) -> dict[str, Any]:
        return {
            "iss": claims.iss,
            "aud": claims.aud,
            "sub": claims.sub,
            "uid": claims.uid,
            "tid": claims.tid,
            "tv": claims.tv,
            "jti": claims.jti,
            "iat": int(claims.iat.timestamp()),
            "exp": int(claims.exp.timestamp()),
            "typ": claims.typ,
            "scopes": sorted(claims.scopes),
        }

    @staticmethod
    def _refresh_payload(claims: RefreshTokenClaims) -> dict[str, Any]:
        return {
            "iss": claims.iss,
            "aud": claims.aud,
            "sub": claims.sub,
            "uid": claims.uid,
            "tid": claims.tid,
            "tv": claims.tv,
            "jti": claims.jti,
            "sid": claims.sid,
            "iat": int(claims.iat.timestamp()),
            "exp": int(claims.exp.timestamp()),
            "typ": claims.typ,
        }

    def _decode(self, token: str, *, expected_type: TokenType, now: datetime | None) -> dict[str, Any]:
        if not token or not isinstance(token, str):
            raise TokenInvalid("token is missing")
        current = ensure_utc(now) if now is not None else self.now()
        try:
            payload = self._signer.verify(
                token,
                audience=self.config.audience,
                issuer=self.config.issuer,
                leeway=self.config.leeway_seconds,
            )
        except ExpiredSignatureError as exc:
            raise TokenExpired("token has expired") from exc
        except InvalidAudienceError as exc:
            raise TokenInvalid("token audience mismatch", details={"reason": "AUDIENCE_MISMATCH"}) from exc
        except InvalidIssuerError as exc:
            raise TokenInvalid("token issuer mismatch", details={"reason": "ISSUER_MISMATCH"}) from exc
        except InvalidTokenError as exc:
            raise TokenInvalid("token is invalid", details={"reason": type(exc).__name__}) from exc
        token_type = payload.get("typ")
        if token_type != expected_type.value:
            raise TokenInvalid(f"expected a {expected_type.value} token, got {token_type!r}")
        expires_at = _as_datetime(payload.get("exp"))
        issued_at = _as_datetime(payload.get("iat"))
        leeway = float(self.config.leeway_seconds)
        if current.timestamp() > expires_at.timestamp() + leeway:
            raise TokenExpired("token has expired", details={"exp": expires_at.isoformat()})
        if issued_at.timestamp() > current.timestamp() + leeway:
            raise TokenInvalid("token was issued in the future")
        return payload


# --------------------------------------------------------------------------- API keys ----------


def constant_time_compare(left: str | bytes, right: str | bytes) -> bool:
    """Constant-time equality for secrets/tokens (never short-circuits on first mismatch)."""
    left_bytes = left.encode("utf-8") if isinstance(left, str) else left
    right_bytes = right.encode("utf-8") if isinstance(right, str) else right
    return hmac.compare_digest(left_bytes, right_bytes)


@dataclass(frozen=True, slots=True)
class ApiKeyParts:
    prefix: str
    secret: str

    @property
    def full_key(self) -> str:
        return f"{self.prefix}.{self.secret}"


@dataclass(frozen=True, slots=True)
class ApiKeyMaterial:
    """Generated API key: show ``full_key`` once; persist ``key_prefix`` + ``secret_hash`` only."""

    key_prefix: str
    secret: str
    full_key: str
    secret_hash: str


class ApiKeyHasher:
    """API-key generation/lookup: prefix-indexed, HMAC-SHA256(secret, pepper), constant-time verify."""

    SEPARATOR = "."
    MAX_PREFIX_LENGTH = 32
    MIN_SECRET_LENGTH = 24

    def __init__(self, pepper: str | bytes) -> None:
        if not pepper:
            raise ValueError("API-key pepper is required")
        self._pepper = pepper.encode("utf-8") if isinstance(pepper, str) else pepper

    def generate(self, *, prefix: str | None = None) -> ApiKeyMaterial:
        key_prefix = prefix or f"ipk_{secrets.token_hex(4)}"
        self._validate_prefix(key_prefix)
        secret = secrets.token_urlsafe(32)
        full_key = f"{key_prefix}{self.SEPARATOR}{secret}"
        return ApiKeyMaterial(
            key_prefix=key_prefix,
            secret=secret,
            full_key=full_key,
            secret_hash=self.hash_secret(secret),
        )

    def hash_secret(self, secret: str) -> str:
        if not secret:
            raise ValueError("API-key secret is required")
        return hmac.new(self._pepper, secret.encode("utf-8"), hashlib.sha256).hexdigest()

    def verify(self, full_key: str, stored_hash: str) -> bool:
        """Verify ``<prefix>.<secret>`` against a stored digest (constant time; malformed → False)."""
        try:
            parts = self.parse(full_key)
        except InvalidCredentials:
            return False
        if not stored_hash:
            return False
        expected = self.hash_secret(parts.secret)
        return constant_time_compare(expected, stored_hash)

    def parse(self, full_key: str) -> ApiKeyParts:
        if not full_key or self.SEPARATOR not in full_key:
            raise InvalidCredentials("API key is malformed")
        prefix, _, secret = full_key.partition(self.SEPARATOR)
        try:
            self._validate_prefix(prefix)
        except ValueError as exc:
            raise InvalidCredentials("API key prefix is malformed") from exc
        if len(secret) < self.MIN_SECRET_LENGTH:
            raise InvalidCredentials("API key secret is malformed")
        return ApiKeyParts(prefix=prefix, secret=secret)

    def split_prefix(self, full_key: str) -> str:
        """Extract the indexable prefix without validating the secret (safe for lookup)."""
        if self.SEPARATOR not in full_key:
            raise InvalidCredentials("API key is malformed")
        return full_key.partition(self.SEPARATOR)[0]

    @classmethod
    def _validate_prefix(cls, prefix: str) -> None:
        if not 4 <= len(prefix) <= cls.MAX_PREFIX_LENGTH:
            raise ValueError("API key prefix length is invalid")
        if not all(character.isalnum() or character in "_-" for character in prefix):
            raise ValueError("API key prefix contains invalid characters")


__all__ = [
    "ACCESS_TTL_SECONDS",
    "MIN_HMAC_SECRET_BYTES",
    "REFRESH_TTL_SECONDS",
    "SELECTION_TTL_SECONDS",
    "AccessTokenClaims",
    "ApiKeyHasher",
    "ApiKeyMaterial",
    "ApiKeyParts",
    "AsymmetricTokenSigner",
    "Clock",
    "HmacTokenSigner",
    "InMemoryRefreshRotationStore",
    "JtiFactory",
    "RefreshRotationStore",
    "RefreshTokenClaims",
    "SelectionTokenClaims",
    "SigningKeyRef",
    "StaticSigningKeyRef",
    "TokenConfig",
    "TokenPair",
    "TokenService",
    "TokenSigner",
    "TokenType",
    "constant_time_compare",
    "ensure_utc",
    "signer_from_key_ref",
    "utc_now",
]
