"""Authentication/session repository over the FS-00 `platform` Core tables.

The repository is the only place that touches `app.core.models.platform` for auth flows; routers
consume frozen dataclass records so no ORM/table objects leak across module boundaries (RULE-DATA-07).

Notes
-----
* Refresh tokens are stored as SHA-256 digests (`user_session.refresh_token_hash`); the plaintext token is
  never persisted. Rotation uses a conditional UPDATE on the old digest so only one concurrent refresh
  can win; the loser is a replay and the router revokes the session.
* Role → capability scopes are resolved from ``user_role`` (effective-dated) → ``role_permission`` →
  ``permission_scope`` so role changes take effect immediately (DM-RBAC-03).
* All timestamps are UTC; session expiry/status checks are done in the router/service layer.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import and_, insert, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.models.platform import (
    app_user,
    permission_scope,
    role,
    role_permission,
    tenant,
    tenant_invitation,
    tenant_sso_connection,
    user_data_scope,
    user_identity,
    user_role,
    user_session,
)

SESSION_ACTIVE = "ACTIVE"
SESSION_REVOKED = "REVOKED"
SESSION_EXPIRED = "EXPIRED"


def utc_now() -> datetime:
    return datetime.now(UTC)


def hash_refresh_token(token: str) -> str:
    """Digest stored at rest for a refresh token (plaintext is never stored)."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class IdentityRecord:
    id: UUID
    email: str
    display_name: str | None
    password_hash: str | None
    status: str
    is_platform_admin: bool
    mfa_enabled: bool


@dataclass(frozen=True, slots=True)
class TenantRecord:
    id: UUID
    code: str
    name: str
    status: str


@dataclass(frozen=True, slots=True)
class MembershipRecord:
    app_user_id: UUID
    identity_id: UUID
    tenant_id: UUID
    tenant_code: str
    tenant_name: str
    tenant_status: str
    email: str
    display_name: str | None
    status: str
    membership_status: str
    is_service_account: bool
    token_version: int
    joined_at: datetime | None
    last_login_at: datetime | None
    roles: tuple[str, ...] = ()
    scopes: frozenset[str] = frozenset()
    data_scopes: Mapping[str, tuple[str, ...]] = field(default_factory=dict)

    @property
    def is_active(self) -> bool:
        return self.status == "ACTIVE" and self.membership_status == "ACTIVE" and self.tenant_status == "ACTIVE"


@dataclass(frozen=True, slots=True)
class SessionRecord:
    id: UUID
    tenant_id: UUID
    app_user_id: UUID
    identity_id: UUID
    refresh_token_hash: str
    status: str
    issued_at: datetime
    expires_at: datetime
    revoked_at: datetime | None
    revoked_reason: str | None

    def is_active(self, now: datetime | None = None) -> bool:
        if self.status != SESSION_ACTIVE:
            return False
        return (now or utc_now()) < self.expires_at


@dataclass(frozen=True, slots=True)
class SsoConnectionRecord:
    id: UUID
    tenant_id: UUID
    tenant_code: str
    tenant_name: str
    name: str
    protocol: str
    status: str
    dex_issuer_url: str | None
    email_domains: tuple[str, ...]
    is_enforced: bool
    jit_provisioning: bool


@dataclass(frozen=True, slots=True)
class InvitationRecord:
    id: UUID
    tenant_id: UUID
    email: str
    intended_role_ids: tuple[UUID, ...]
    token_hash: str
    status: str
    invited_by: UUID
    expires_at: datetime
    accepted_at: datetime | None


def _identity_record(row: Any) -> IdentityRecord:
    return IdentityRecord(
        id=row["id"],
        email=str(row["email"]),
        display_name=row["display_name"],
        password_hash=row["password_hash"],
        status=row["status"],
        is_platform_admin=bool(row["is_platform_admin"]),
        mfa_enabled=bool(row["mfa_enabled"]),
    )


def _session_record(row: Any) -> SessionRecord:
    return SessionRecord(
        id=row["id"],
        tenant_id=row["tenant_id"],
        app_user_id=row["app_user_id"],
        identity_id=row["identity_id"],
        refresh_token_hash=row["refresh_token_hash"],
        status=row["status"],
        issued_at=row["issued_at"],
        expires_at=row["expires_at"],
        revoked_at=row["revoked_at"],
        revoked_reason=row["revoked_reason"],
    )


_MEMBERSHIP_COLUMNS = (
    app_user.c.id.label("app_user_id"),
    app_user.c.identity_id,
    app_user.c.tenant_id,
    app_user.c.email.label("app_user_email"),
    app_user.c.display_name.label("app_user_display_name"),
    app_user.c.status.label("app_user_status"),
    app_user.c.membership_status,
    app_user.c.is_service_account,
    app_user.c.token_version,
    app_user.c.joined_at,
    app_user.c.last_login_at,
    tenant.c.code.label("tenant_code"),
    tenant.c.name.label("tenant_name"),
    tenant.c.status.label("tenant_status"),
)


def _membership_record(
    row: Any,
    roles: Iterable[str] = (),
    scopes: Iterable[str] = (),
    data_scopes: Mapping[str, tuple[str, ...]] | None = None,
) -> MembershipRecord:
    return MembershipRecord(
        app_user_id=row["app_user_id"],
        identity_id=row["identity_id"],
        tenant_id=row["tenant_id"],
        tenant_code=row["tenant_code"],
        tenant_name=row["tenant_name"],
        tenant_status=row["tenant_status"],
        email=str(row["app_user_email"]),
        display_name=row["app_user_display_name"],
        status=row["app_user_status"],
        membership_status=row["membership_status"],
        is_service_account=bool(row["is_service_account"]),
        token_version=int(row["token_version"]),
        joined_at=row["joined_at"],
        last_login_at=row["last_login_at"],
        roles=tuple(roles),
        scopes=frozenset(scopes),
        data_scopes=dict(data_scopes or {}),
    )


def _effective_window(now: datetime):
    """`user_role` rows effective at ``now`` (inclusive start, exclusive/inclusive end)."""
    return and_(
        user_role.c.effective_from <= now,
        or_(user_role.c.effective_to.is_(None), user_role.c.effective_to > now),
    )


class AuthRepository:
    """DB access for local login/session/self-service endpoints (async SQLAlchemy Core)."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # -- identity ----------------------------------------------------------------------------

    async def get_identity_by_email(self, email: str) -> IdentityRecord | None:
        result = await self.session.execute(
            select(user_identity).where(user_identity.c.email == email.strip().lower())
        )
        row = result.mappings().first()
        return _identity_record(row) if row else None

    async def get_identity(self, identity_id: UUID) -> IdentityRecord | None:
        result = await self.session.execute(select(user_identity).where(user_identity.c.id == identity_id))
        row = result.mappings().first()
        return _identity_record(row) if row else None

    async def update_identity_password_hash(self, identity_id: UUID, password_hash: str) -> None:
        await self.session.execute(
            update(user_identity)
            .where(user_identity.c.id == identity_id)
            .values(password_hash=password_hash, updated_at=utc_now(), version=user_identity.c.version + 1)
        )

    async def touch_last_login(self, *, identity_id: UUID, tenant_id: UUID, app_user_id: UUID, now: datetime) -> None:
        await self.session.execute(
            update(user_identity).where(user_identity.c.id == identity_id).values(last_login_at=now)
        )
        await self.session.execute(
            update(app_user)
            .where(and_(app_user.c.id == app_user_id, app_user.c.tenant_id == tenant_id))
            .values(last_login_at=now)
        )

    # -- tenants / memberships ---------------------------------------------------------------

    async def get_tenant(self, tenant_id: UUID) -> TenantRecord | None:
        result = await self.session.execute(select(tenant).where(tenant.c.id == tenant_id))
        row = result.mappings().first()
        return TenantRecord(row["id"], row["code"], row["name"], row["status"]) if row else None

    async def get_tenant_by_code(self, code: str) -> TenantRecord | None:
        result = await self.session.execute(select(tenant).where(tenant.c.code == code.strip()))
        row = result.mappings().first()
        return TenantRecord(row["id"], row["code"], row["name"], row["status"]) if row else None

    async def list_memberships(self, identity_id: UUID, *, now: datetime | None = None) -> list[MembershipRecord]:
        moment = now or utc_now()
        result = await self.session.execute(
            select(*_MEMBERSHIP_COLUMNS)
            .select_from(app_user)
            .join(tenant, tenant.c.id == app_user.c.tenant_id)
            .where(app_user.c.identity_id == identity_id)
            .order_by(tenant.c.code)
        )
        rows = result.mappings().all()
        if not rows:
            return []
        keys = [(row["tenant_id"], row["app_user_id"]) for row in rows]
        roles, scopes = await self._roles_and_scopes(keys, moment)
        return [
            _membership_record(row, roles.get((row["tenant_id"], row["app_user_id"]), ()), scopes.get((row["tenant_id"], row["app_user_id"]), ()))
            for row in rows
        ]

    async def get_membership(self, tenant_id: UUID, app_user_id: UUID) -> MembershipRecord | None:
        result = await self.session.execute(
            select(*_MEMBERSHIP_COLUMNS)
            .select_from(app_user)
            .join(tenant, tenant.c.id == app_user.c.tenant_id)
            .where(and_(app_user.c.tenant_id == tenant_id, app_user.c.id == app_user_id))
        )
        row = result.mappings().first()
        if row is None:
            return None
        key = [(row["tenant_id"], row["app_user_id"])]
        roles, scopes = await self._roles_and_scopes(key, utc_now())
        data_scopes = await self.data_scopes(row["tenant_id"], row["app_user_id"])
        return _membership_record(
            row,
            roles.get((row["tenant_id"], row["app_user_id"]), ()),
            scopes.get((row["tenant_id"], row["app_user_id"]), ()),
            data_scopes,
        )

    async def get_membership_for_identity(
        self, identity_id: UUID, *, tenant_id: UUID | None = None
    ) -> MembershipRecord | None:
        memberships = await self.list_memberships(identity_id)
        if tenant_id is None:
            return memberships[0] if len(memberships) == 1 else None
        return next((membership for membership in memberships if membership.tenant_id == tenant_id), None)

    async def effective_scopes(self, tenant_id: UUID, app_user_id: UUID, *, now: datetime | None = None) -> frozenset[str]:
        _, scopes = await self._roles_and_scopes([(tenant_id, app_user_id)], now or utc_now())
        return scopes.get((tenant_id, app_user_id), frozenset())

    async def data_scopes(self, tenant_id: UUID, app_user_id: UUID) -> dict[str, tuple[str, ...]]:
        role_ids = select(user_role.c.role_id).where(
            and_(user_role.c.tenant_id == tenant_id, user_role.c.user_id == app_user_id)
        )
        result = await self.session.execute(
            select(user_data_scope.c.scope_type, user_data_scope.c.scope_value).where(
                and_(
                    user_data_scope.c.tenant_id == tenant_id,
                    or_(user_data_scope.c.user_id == app_user_id, user_data_scope.c.role_id.in_(role_ids)),
                )
            )
        )
        collected: dict[str, list[str]] = {}
        for scope_type, scope_value in result.all():
            collected.setdefault(str(scope_type), []).append(str(scope_value))
        return {scope_type: tuple(dict.fromkeys(values)) for scope_type, values in collected.items()}

    async def _roles_and_scopes(
        self,
        keys: Sequence[tuple[UUID, UUID]],
        now: datetime,
    ) -> tuple[dict[tuple[UUID, UUID], tuple[str, ...]], dict[tuple[UUID, UUID], frozenset[str]]]:
        if not keys:
            return {}, {}
        by_tenant: dict[UUID, list[UUID]] = {}
        for tenant_id, app_user_id in keys:
            by_tenant.setdefault(tenant_id, []).append(app_user_id)
        tenant_ids = list(by_tenant)
        pairs = set(keys)
        role_rows = await self.session.execute(
            select(user_role.c.tenant_id, user_role.c.user_id, role.c.code)
            .select_from(user_role)
            .join(
                role,
                and_(role.c.tenant_id == user_role.c.tenant_id, role.c.id == user_role.c.role_id),
            )
            .where(
                user_role.c.tenant_id.in_(tenant_ids),
                role.c.inactive_at.is_(None),
                _effective_window(now),
            )
            .order_by(user_role.c.tenant_id, role.c.code)
        )
        roles: dict[tuple[UUID, UUID], list[str]] = {}
        for tenant_id, user_id, role_code in role_rows.all():
            if (tenant_id, user_id) in pairs:
                roles.setdefault((tenant_id, user_id), []).append(str(role_code))
        scope_rows = await self.session.execute(
            select(user_role.c.tenant_id, user_role.c.user_id, permission_scope.c.scope)
            .select_from(user_role)
            .join(role, and_(role.c.tenant_id == user_role.c.tenant_id, role.c.id == user_role.c.role_id))
            .join(
                role_permission,
                and_(
                    role_permission.c.tenant_id == role.c.tenant_id,
                    role_permission.c.role_id == role.c.id,
                ),
            )
            .join(
                permission_scope,
                and_(
                    permission_scope.c.tenant_id == role_permission.c.tenant_id,
                    permission_scope.c.id == role_permission.c.scope_id,
                ),
            )
            .where(
                user_role.c.tenant_id.in_(tenant_ids),
                role.c.inactive_at.is_(None),
                _effective_window(now),
            )
            .order_by(user_role.c.tenant_id, permission_scope.c.scope)
        )
        scopes: dict[tuple[UUID, UUID], set[str]] = {}
        for tenant_id, user_id, scope_name in scope_rows.all():
            if (tenant_id, user_id) in pairs:
                scopes.setdefault((tenant_id, user_id), set()).add(str(scope_name))
        return (
            {key: tuple(values) for key, values in roles.items()},
            {key: frozenset(values) for key, values in scopes.items()},
        )

    # -- sessions ----------------------------------------------------------------------------

    async def create_session(
        self,
        *,
        session_id: UUID | None = None,
        tenant_id: UUID,
        app_user_id: UUID,
        identity_id: UUID,
        refresh_token_hash: str,
        issued_at: datetime,
        expires_at: datetime,
        ip: str | None = None,
        user_agent: str | None = None,
    ) -> SessionRecord:
        sid = session_id or uuid.uuid4()
        await self.session.execute(
            insert(user_session).values(
                id=sid,
                tenant_id=tenant_id,
                app_user_id=app_user_id,
                identity_id=identity_id,
                refresh_token_hash=refresh_token_hash,
                status=SESSION_ACTIVE,
                issued_at=issued_at,
                expires_at=expires_at,
                ip=ip,
                user_agent=user_agent,
                last_seen_at=issued_at,
            )
        )
        return SessionRecord(
            id=sid,
            tenant_id=tenant_id,
            app_user_id=app_user_id,
            identity_id=identity_id,
            refresh_token_hash=refresh_token_hash,
            status=SESSION_ACTIVE,
            issued_at=issued_at,
            expires_at=expires_at,
            revoked_at=None,
            revoked_reason=None,
        )

    async def get_session(self, session_id: UUID) -> SessionRecord | None:
        result = await self.session.execute(select(user_session).where(user_session.c.id == session_id))
        row = result.mappings().first()
        return _session_record(row) if row else None

    async def rotate_session_refresh_hash(
        self,
        session_id: UUID,
        *,
        old_hash: str,
        new_hash: str,
        issued_at: datetime,
        expires_at: datetime,
    ) -> bool:
        """Atomically rotate the stored digest; False means another request already used this token."""
        result = await self.session.execute(
            update(user_session)
            .where(
                and_(
                    user_session.c.id == session_id,
                    user_session.c.refresh_token_hash == old_hash,
                    user_session.c.status == SESSION_ACTIVE,
                )
            )
            .values(
                refresh_token_hash=new_hash,
                issued_at=issued_at,
                expires_at=expires_at,
                last_seen_at=issued_at,
            )
        )
        return bool(getattr(result, "rowcount", 0))

    async def revoke_session(self, session_id: UUID, *, reason: str, now: datetime | None = None) -> bool:
        result = await self.session.execute(
            update(user_session)
            .where(and_(user_session.c.id == session_id, user_session.c.status == SESSION_ACTIVE))
            .values(status=SESSION_REVOKED, revoked_at=now or utc_now(), revoked_reason=reason)
        )
        return bool(getattr(result, "rowcount", 0))

    async def revoke_all_sessions(
        self, *, tenant_id: UUID, app_user_id: UUID, reason: str, now: datetime | None = None
    ) -> int:
        result = await self.session.execute(
            update(user_session)
            .where(
                and_(
                    user_session.c.tenant_id == tenant_id,
                    user_session.c.app_user_id == app_user_id,
                    user_session.c.status == SESSION_ACTIVE,
                )
            )
            .values(status=SESSION_REVOKED, revoked_at=now or utc_now(), revoked_reason=reason)
        )
        return int(getattr(result, "rowcount", 0) or 0)

    async def bump_token_version(self, *, tenant_id: UUID, app_user_id: UUID) -> int:
        result = await self.session.execute(
            update(app_user)
            .where(and_(app_user.c.tenant_id == tenant_id, app_user.c.id == app_user_id))
            .values(token_version=app_user.c.token_version + 1)
            .returning(app_user.c.token_version)
        )
        return int(result.scalar_one())

    # -- invitation accept -------------------------------------------------------------------

    async def create_identity(
        self,
        *,
        email: str,
        display_name: str | None,
        password_hash: str,
        status: str = "ACTIVE",
    ) -> UUID:
        identity_id = uuid.uuid4()
        await self.session.execute(
            insert(user_identity).values(
                id=identity_id,
                email=email.strip().lower(),
                display_name=display_name,
                password_hash=password_hash,
                status=status,
            )
        )
        return identity_id

    async def update_identity_display_name(self, identity_id: UUID, display_name: str | None) -> None:
        if not display_name:
            return
        await self.session.execute(
            update(user_identity)
            .where(user_identity.c.id == identity_id)
            .values(display_name=display_name, updated_at=utc_now(), version=user_identity.c.version + 1)
        )

    async def get_roles_by_ids(self, tenant_id: UUID, role_ids: Sequence[UUID]) -> dict[UUID, str]:
        if not role_ids:
            return {}
        result = await self.session.execute(
            select(role.c.id, role.c.code).where(
                and_(
                    role.c.tenant_id == tenant_id,
                    role.c.id.in_(list(role_ids)),
                    role.c.inactive_at.is_(None),
                )
            )
        )
        return {row["id"]: str(row["code"]) for row in result.mappings().all()}

    async def ensure_membership_for_invitation(
        self,
        *,
        tenant_id: UUID,
        identity_id: UUID,
        email: str,
        display_name: str | None,
        role_ids: Sequence[UUID],
    ) -> UUID:
        """Create or re-activate the INVITED membership and assign the intended roles (same transaction)."""
        now = utc_now()
        existing = await self.session.execute(
            select(app_user.c.id, app_user.c.status, app_user.c.membership_status).where(
                and_(app_user.c.tenant_id == tenant_id, app_user.c.identity_id == identity_id)
            )
        )
        row = existing.mappings().first()
        if row is None:
            app_user_id = uuid.uuid4()
            await self.session.execute(
                insert(app_user).values(
                    id=app_user_id,
                    tenant_id=tenant_id,
                    identity_id=identity_id,
                    email=email.strip().lower(),
                    display_name=display_name,
                    status="ACTIVE",
                    membership_status="ACTIVE",
                    joined_at=now,
                )
            )
        else:
            app_user_id = row["id"]
            await self.session.execute(
                update(app_user)
                .where(and_(app_user.c.tenant_id == tenant_id, app_user.c.id == app_user_id))
                .values(
                    email=email.strip().lower(),
                    display_name=display_name,
                    status="ACTIVE",
                    membership_status="ACTIVE",
                    joined_at=now,
                )
            )
        if role_ids:
            current = await self.session.execute(
                select(user_role.c.role_id).where(
                    and_(
                        user_role.c.tenant_id == tenant_id,
                        user_role.c.user_id == app_user_id,
                        user_role.c.effective_to.is_(None),
                    )
                )
            )
            assigned = {row["role_id"] for row in current.mappings().all()}
            for role_id in dict.fromkeys(role_ids):
                if role_id in assigned:
                    continue
                await self.session.execute(
                    insert(user_role).values(
                        tenant_id=tenant_id,
                        user_id=app_user_id,
                        role_id=role_id,
                        effective_from=now,
                    )
                )
        return app_user_id

    async def accept_invitation(self, invitation_id: UUID, *, app_user_id: UUID, now: datetime) -> bool:
        """Conditional single-use transition PENDING → ACCEPTED (False on expiry/revoke/race)."""
        result = await self.session.execute(
            update(tenant_invitation)
            .where(
                and_(
                    tenant_invitation.c.id == invitation_id,
                    tenant_invitation.c.status == "PENDING",
                    tenant_invitation.c.expires_at > now,
                )
            )
            .values(
                status="ACCEPTED",
                accepted_at=now,
                accepted_app_user_id=app_user_id,
                version=tenant_invitation.c.version + 1,
            )
        )
        return bool(getattr(result, "rowcount", 0))

    async def expire_invitation(self, invitation_id: UUID, *, now: datetime) -> None:
        await self.session.execute(
            update(tenant_invitation)
            .where(and_(tenant_invitation.c.id == invitation_id, tenant_invitation.c.status == "PENDING"))
            .values(status="EXPIRED", version=tenant_invitation.c.version + 1)
        )

    # -- SSO providers -----------------------------------------------------------------------

    async def list_active_sso_connections(self) -> list[SsoConnectionRecord]:
        result = await self.session.execute(
            select(
                tenant_sso_connection.c.id,
                tenant_sso_connection.c.tenant_id,
                tenant_sso_connection.c.name,
                tenant_sso_connection.c.protocol,
                tenant_sso_connection.c.status,
                tenant_sso_connection.c.dex_issuer_url,
                tenant_sso_connection.c.email_domains,
                tenant_sso_connection.c.is_enforced,
                tenant_sso_connection.c.jit_provisioning,
                tenant.c.code.label("tenant_code"),
                tenant.c.name.label("tenant_name"),
            )
            .select_from(tenant_sso_connection)
            .join(tenant, tenant.c.id == tenant_sso_connection.c.tenant_id)
            .where(tenant_sso_connection.c.status == "ACTIVE")
            .order_by(tenant.c.code, tenant_sso_connection.c.name)
        )
        return [
            SsoConnectionRecord(
                id=row["id"],
                tenant_id=row["tenant_id"],
                tenant_code=str(row["tenant_code"]),
                tenant_name=str(row["tenant_name"]),
                name=str(row["name"]),
                protocol=str(row["protocol"]),
                status=str(row["status"]),
                dex_issuer_url=row["dex_issuer_url"],
                email_domains=tuple(str(domain) for domain in (row["email_domains"] or ())),
                is_enforced=bool(row["is_enforced"]),
                jit_provisioning=bool(row["jit_provisioning"]),
            )
            for row in result.mappings().all()
        ]

    # -- invitations -------------------------------------------------------------------------

    async def get_invitation_by_token_hash(self, token_hash: str) -> InvitationRecord | None:
        result = await self.session.execute(
            select(tenant_invitation).where(tenant_invitation.c.token_hash == token_hash)
        )
        row = result.mappings().first()
        if row is None:
            return None
        return InvitationRecord(
            id=row["id"],
            tenant_id=row["tenant_id"],
            email=str(row["email"]),
            intended_role_ids=tuple(row["intended_role_ids"] or ()),
            token_hash=row["token_hash"],
            status=row["status"],
            invited_by=row["invited_by"],
            expires_at=row["expires_at"],
            accepted_at=row["accepted_at"],
        )


__all__ = [
    "SESSION_ACTIVE",
    "SESSION_EXPIRED",
    "SESSION_REVOKED",
    "AuthRepository",
    "IdentityRecord",
    "InvitationRecord",
    "MembershipRecord",
    "SessionRecord",
    "SsoConnectionRecord",
    "TenantRecord",
    "hash_refresh_token",
    "utc_now",
]
