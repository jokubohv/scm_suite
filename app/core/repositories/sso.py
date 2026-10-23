"""SSO persistence layer (FS-00 Δ-SSO): tenant-scoped connections, links, mappings, JIT/idP users.

One request-scoped repository over the ``platform`` tables (migrations 0001–0003). The repository is
**tenant-scoped by construction** (every connection/link/mapping query filters ``tenant_id``) and
policy-free for SQL shape: authorization, state/nonce/PKCE, JWT/claim validation and group/role bounds
remain in ``app.core.security.authn`` + the API layer. No secrets are stored or returned — only
secret-store references (``client_secret_ref``) are persisted; serializers expose a boolean instead.

Cross-referenced tables: ``tenant_sso_connection``, ``user_identity_link``, ``sso_group_role_map``,
``user_identity`` (global auth-only), ``app_user`` (per-tenant membership), ``user_session``,
``role``/``role_permission``/``permission_scope``/``user_role``, ``tenant`` and ``audit_event``.

Authority: `design/DEX-SSO-ADDENDUM.md` §3–§5, `feature-sets/FS-00.md` §4/§5 (DM-SSO-01..03, INV-32),
`design/DATA-MODEL.md` platform tables.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import and_, delete, func, insert, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.models.platform import (
    app_user,
    audit_event,
    permission_scope,
    role,
    role_permission,
    sso_group_role_map,
    tenant,
    tenant_sso_connection,
    user_identity,
    user_identity_link,
    user_role,
    user_session,
)
from app.core.platform_events import audit_recorded, emit_platform_event

CONNECTION_PROTOCOLS = ("SAML", "OIDC", "LDAP_AD")
CONNECTION_STATUSES = ("DRAFT", "TESTING", "ACTIVE", "DISABLED", "ERROR")
LINK_STATUSES = ("ACTIVE", "REVOKED")
MATCH_TYPES = ("EXACT", "REGEX")

#: Fields accepted on connection create/update (DB column names).
CONNECTION_WRITABLE = (
    "name",
    "protocol",
    "status",
    "dex_issuer_url",
    "dex_instance_ref",
    "client_id",
    "client_secret_ref",
    "connector_config",
    "email_domains",
    "is_enforced",
    "jit_provisioning",
    "default_role_ids",
    "group_claim_name",
)


def utc_now() -> datetime:
    return datetime.now(UTC)


def as_uuid(value: Any) -> UUID | None:
    """Best-effort UUID coercion; invalid/missing ids map to ``None`` (callers return 404)."""
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        return None


def _with_audit(values: Mapping[str, Any], actor_id: UUID | None, *, creating: bool) -> dict[str, Any]:
    result = dict(values)
    if actor_id is not None:
        if creating:
            result.setdefault("created_by", actor_id)
        result.setdefault("updated_by", actor_id)
    return result


class SsoRepository:
    """One request-scoped repository (no commits; the API session dependency owns the transaction)."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # ------------------------------------------------------------------ audit -----------------
    async def audit(
        self,
        *,
        tenant_id: UUID | str,
        actor_id: UUID | str | None,
        action: str,
        resource_type: str,
        resource_id: UUID | str | None,
        diff: Mapping[str, Any] | None = None,
        actor_type: str = "USER",
        source: str = "API",
    ) -> UUID:
        event_id = uuid4()
        actor_uuid = as_uuid(actor_id)
        await self.session.execute(
            insert(audit_event).values(
                id=event_id,
                tenant_id=as_uuid(tenant_id),
                event_id=event_id,
                actor_type=actor_type,
                actor_id=actor_uuid,
                action=action,
                resource_type=resource_type,
                resource_id=str(resource_id) if resource_id is not None else None,
                diff=dict(diff or {}),
                source=source,
                occurred_at=utc_now(),
            )
        )
        await audit_recorded(
            self.session, tenant_id=str(tenant_id), event_id=str(event_id), actor_type=actor_type,
            action=action, resource_type=resource_type,
            resource_id=str(resource_id) if resource_id is not None else None,
            occurred_at=utc_now().isoformat(),
        )
        return event_id

    # ------------------------------------------------------------------ tenants ---------------
    async def get_tenant(self, tenant_id: UUID | str) -> dict[str, Any] | None:
        row = (await self.session.execute(select(tenant).where(tenant.c.id == as_uuid(tenant_id)))).mappings().first()
        return dict(row) if row else None

    async def list_active_connection_rows(self) -> list[dict[str, Any]]:
        """ACTIVE connections in ACTIVE tenants with public-routing tenant fields (start/providers)."""
        rows = (
            (
                await self.session.execute(
                    select(
                        tenant_sso_connection,
                        tenant.c.code.label("tenant_code"),
                        tenant.c.name.label("tenant_name"),
                    )
                    .select_from(tenant_sso_connection)
                    .join(tenant, tenant.c.id == tenant_sso_connection.c.tenant_id)
                    .where(
                        and_(
                            tenant_sso_connection.c.status == "ACTIVE",
                            tenant.c.status == "ACTIVE",
                        )
                    )
                    .order_by(tenant.c.code, tenant_sso_connection.c.name)
                )
            )
            .mappings()
            .all()
        )
        return [dict(row) for row in rows]

    # ---------------------------------------------------------------- connections -------------
    async def get_connection_row(self, tenant_id: UUID | str, connection_id: UUID | str) -> dict[str, Any] | None:
        """Raw tenant-scoped connection row (includes ``client_secret_ref``; never returned to clients)."""
        row = (
            (
                await self.session.execute(
                    select(tenant_sso_connection).where(
                        and_(
                            tenant_sso_connection.c.tenant_id == as_uuid(tenant_id),
                            tenant_sso_connection.c.id == as_uuid(connection_id),
                        )
                    )
                )
            )
            .mappings()
            .first()
        )
        return dict(row) if row else None

    async def list_connections(
        self,
        tenant_id: UUID | str,
        *,
        status: str | None = None,
        protocol: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        query = select(tenant_sso_connection).where(tenant_sso_connection.c.tenant_id == as_uuid(tenant_id))
        if status:
            query = query.where(tenant_sso_connection.c.status == status.upper())
        if protocol:
            query = query.where(tenant_sso_connection.c.protocol == protocol.upper())
        query = query.order_by(tenant_sso_connection.c.name).limit(limit).offset(offset)
        rows = (await self.session.execute(query)).mappings().all()
        return [dict(row) for row in rows]

    async def create_connection(
        self, tenant_id: UUID | str, values: Mapping[str, Any], *, actor_id: UUID | str | None
    ) -> dict[str, Any]:
        connection_id = uuid4()
        payload = {column: values[column] for column in CONNECTION_WRITABLE if column in values}
        payload.update(
            id=connection_id,
            tenant_id=as_uuid(tenant_id),
            created_by=as_uuid(actor_id),
            updated_by=as_uuid(actor_id),
            version=1,
        )
        await self.session.execute(insert(tenant_sso_connection).values(**payload))
        created = await self.get_connection_row(tenant_id, connection_id)
        if created is None:  # pragma: no cover - defensive
            raise RuntimeError("connection insert did not persist")
        return created

    async def update_connection(
        self,
        tenant_id: UUID | str,
        connection_id: UUID | str,
        values: Mapping[str, Any],
        *,
        actor_id: UUID | str | None,
    ) -> dict[str, Any] | None:
        current = await self.get_connection_row(tenant_id, connection_id)
        if current is None:
            return None
        payload = {column: values[column] for column in CONNECTION_WRITABLE if column in values}
        if not payload:
            return current
        payload = _with_audit(payload, as_uuid(actor_id), creating=False)
        await self.session.execute(
            update(tenant_sso_connection)
            .where(
                and_(
                    tenant_sso_connection.c.tenant_id == current["tenant_id"],
                    tenant_sso_connection.c.id == current["id"],
                )
            )
            .values(
                **payload,
                version=tenant_sso_connection.c.version + 1,
                updated_at=utc_now(),
            )
        )
        return await self.get_connection_row(tenant_id, connection_id)

    async def set_connection_status(
        self,
        tenant_id: UUID | str,
        connection_id: UUID | str,
        status: str,
        *,
        last_error: str | None = None,
        tested: bool = False,
        actor_id: UUID | str | None = None,
    ) -> dict[str, Any] | None:
        current = await self.get_connection_row(tenant_id, connection_id)
        if current is None:
            return None
        values: dict[str, Any] = {
            "status": status,
            "last_error": last_error,
            "updated_by": as_uuid(actor_id),
            "updated_at": utc_now(),
            "version": tenant_sso_connection.c.version + 1,
        }
        if tested:
            values["last_tested_at"] = utc_now()
        await self.session.execute(
            update(tenant_sso_connection)
            .where(
                and_(
                    tenant_sso_connection.c.tenant_id == current["tenant_id"],
                    tenant_sso_connection.c.id == current["id"],
                )
            )
            .values(**values)
        )
        return await self.get_connection_row(tenant_id, connection_id)

    async def set_secret_ref(
        self,
        tenant_id: UUID | str,
        connection_id: UUID | str,
        secret_ref: str,
        *,
        actor_id: UUID | str | None,
    ) -> dict[str, Any] | None:
        return await self.update_connection(
            tenant_id, connection_id, {"client_secret_ref": secret_ref}, actor_id=actor_id
        )

    async def count_active_links(self, tenant_id: UUID | str, connection_id: UUID | str) -> int:
        result = await self.session.execute(
            select(func.count())
            .select_from(user_identity_link)
            .where(
                and_(
                    user_identity_link.c.tenant_id == as_uuid(tenant_id),
                    user_identity_link.c.connection_id == as_uuid(connection_id),
                    user_identity_link.c.status == "ACTIVE",
                )
            )
        )
        return int(result.scalar_one() or 0)

    async def delete_connection(
        self, tenant_id: UUID | str, connection_id: UUID | str, *, actor_id: UUID | str | None
    ) -> bool:
        """Hard-delete only when no ACTIVE identity links exist (DM-SSO-02); else callers DISABLE."""
        if await self.get_connection_row(tenant_id, connection_id) is None:
            return False
        await self.audit(
            tenant_id=tenant_id,
            actor_id=actor_id,
            action="sso.connection.deleted",
            resource_type="tenant_sso_connection",
            resource_id=connection_id,
        )
        await self.session.execute(
            delete(tenant_sso_connection).where(
                and_(
                    tenant_sso_connection.c.tenant_id == as_uuid(tenant_id),
                    tenant_sso_connection.c.id == as_uuid(connection_id),
                )
            )
        )
        return True

    # ------------------------------------------------------------------ roles -----------------
    async def roles_by_ids(self, tenant_id: UUID | str, role_ids: Iterable[UUID | str]) -> dict[UUID, dict[str, Any]]:
        wanted = [uid for uid in (as_uuid(value) for value in role_ids) if uid is not None]
        if not wanted:
            return {}
        rows = (
            (
                await self.session.execute(
                    select(role.c.id, role.c.code, role.c.name, role.c.is_template, role.c.is_editable)
                    .where(and_(role.c.tenant_id == as_uuid(tenant_id), role.c.id.in_(wanted)))
                    .order_by(role.c.code)
                )
            )
            .mappings()
            .all()
        )
        return {row["id"]: dict(row) for row in rows}

    async def roles_by_codes(self, tenant_id: UUID | str, codes: Iterable[str]) -> dict[str, dict[str, Any]]:
        wanted = [str(code) for code in codes if code]
        if not wanted:
            return {}
        rows = (
            (
                await self.session.execute(
                    select(role.c.id, role.c.code, role.c.name, role.c.is_template, role.c.is_editable)
                    .where(and_(role.c.tenant_id == as_uuid(tenant_id), role.c.code.in_(wanted)))
                    .order_by(role.c.code)
                )
            )
            .mappings()
            .all()
        )
        return {str(row["code"]): dict(row) for row in rows}

    async def role_scopes(self, tenant_id: UUID | str, role_ids: Iterable[UUID | str]) -> dict[UUID, frozenset[str]]:
        wanted = [uid for uid in (as_uuid(value) for value in role_ids) if uid is not None]
        if not wanted:
            return {}
        rows = (
            (
                await self.session.execute(
                    select(role_permission.c.role_id, permission_scope.c.scope)
                    .select_from(role_permission)
                    .join(
                        permission_scope,
                        and_(
                            permission_scope.c.tenant_id == role_permission.c.tenant_id,
                            permission_scope.c.id == role_permission.c.scope_id,
                        ),
                    )
                    .where(
                        and_(
                            role_permission.c.tenant_id == as_uuid(tenant_id),
                            role_permission.c.role_id.in_(wanted),
                        )
                    )
                )
            )
            .mappings()
            .all()
        )
        grouped: dict[UUID, set[str]] = {}
        for row in rows:
            grouped.setdefault(row["role_id"], set()).add(str(row["scope"]))
        return {role_id: frozenset(scopes) for role_id, scopes in grouped.items()}

    async def active_role_ids_for_user(self, tenant_id: UUID | str, app_user_id: UUID | str) -> list[UUID]:
        now = utc_now()
        rows = (
            (
                await self.session.execute(
                    select(user_role.c.role_id)
                    .where(
                        and_(
                            user_role.c.tenant_id == as_uuid(tenant_id),
                            user_role.c.user_id == as_uuid(app_user_id),
                            user_role.c.effective_from <= now,
                            or_(user_role.c.effective_to.is_(None), user_role.c.effective_to > now),
                        )
                    )
                    .order_by(user_role.c.role_id)
                )
            )
            .scalars()
            .all()
        )
        return list(dict.fromkeys(rows))

    async def assign_roles(
        self,
        tenant_id: UUID | str,
        user_id: UUID | str,
        role_ids: Iterable[UUID | str],
        *,
        actor_id: UUID | str | None,
        now: datetime | None = None,
    ) -> list[UUID]:
        """Assign the requested roles (idempotent: existing active assignments are kept as-is)."""
        tenant_uuid, user_uuid = as_uuid(tenant_id), as_uuid(user_id)
        wanted = [uid for uid in (as_uuid(value) for value in role_ids) if uid is not None]
        current = (
            set(await self.active_role_ids_for_user(tenant_uuid, user_uuid)) if tenant_uuid and user_uuid else set()
        )
        added: list[UUID] = []
        for role_id in wanted:
            if role_id in current:
                continue
            await self.session.execute(
                insert(user_role).values(
                    tenant_id=tenant_uuid,
                    user_id=user_uuid,
                    role_id=role_id,
                    effective_from=now or utc_now(),
                    assigned_by=as_uuid(actor_id),
                )
            )
            added.append(role_id)
        if added:
            await self.audit(
                tenant_id=tenant_id,
                actor_id=actor_id,
                action="user.roles.assigned",
                resource_type="app_user",
                resource_id=user_id,
                diff={"role_ids": [str(role_id) for role_id in added], "source": "SSO_GROUP_MAPPING"},
            )
        return added

    async def end_roles_not_in(
        self,
        tenant_id: UUID | str,
        user_id: UUID | str,
        keep_role_ids: Iterable[UUID | str],
        *,
        actor_id: UUID | str | None,
        now: datetime | None = None,
    ) -> list[UUID]:
        """End active role assignments not present in the SSO-derived role set (mapping propagation)."""
        tenant_uuid, user_uuid = as_uuid(tenant_id), as_uuid(user_id)
        keep = {uid for uid in (as_uuid(value) for value in keep_role_ids) if uid is not None}
        if tenant_uuid is None or user_uuid is None:
            return []
        current = set(await self.active_role_ids_for_user(tenant_uuid, user_uuid))
        removed = [role_id for role_id in current if role_id not in keep]
        for role_id in removed:
            await self.session.execute(
                update(user_role)
                .where(
                    and_(
                        user_role.c.tenant_id == tenant_uuid,
                        user_role.c.user_id == user_uuid,
                        user_role.c.role_id == role_id,
                        user_role.c.effective_to.is_(None),
                    )
                )
                .values(effective_to=now or utc_now())
            )
        if removed:
            await self.audit(
                tenant_id=tenant_id,
                actor_id=actor_id,
                action="user.roles.ended",
                resource_type="app_user",
                resource_id=user_id,
                diff={"role_ids": [str(role_id) for role_id in removed], "source": "SSO_GROUP_MAPPING"},
            )
        return removed

    # ---------------------------------------------------------- group mappings ----------------
    async def list_group_mappings(self, tenant_id: UUID | str, connection_id: UUID | str) -> list[dict[str, Any]]:
        rows = (
            (
                await self.session.execute(
                    select(
                        sso_group_role_map.c.id,
                        sso_group_role_map.c.match_type,
                        sso_group_role_map.c.group_value,
                        sso_group_role_map.c.role_id,
                        sso_group_role_map.c.priority,
                        sso_group_role_map.c.is_active,
                        role.c.code.label("role_code"),
                        role.c.name.label("role_name"),
                    )
                    .select_from(sso_group_role_map)
                    .join(
                        role,
                        and_(
                            role.c.tenant_id == sso_group_role_map.c.tenant_id,
                            role.c.id == sso_group_role_map.c.role_id,
                        ),
                    )
                    .where(
                        and_(
                            sso_group_role_map.c.tenant_id == as_uuid(tenant_id),
                            sso_group_role_map.c.connection_id == as_uuid(connection_id),
                        )
                    )
                    .order_by(sso_group_role_map.c.priority, sso_group_role_map.c.group_value)
                )
            )
            .mappings()
            .all()
        )
        return [dict(row) for row in rows]

    async def replace_group_mappings(
        self,
        tenant_id: UUID | str,
        connection_id: UUID | str,
        mappings: Sequence[Mapping[str, Any]],
        *,
        actor_id: UUID | str | None,
    ) -> list[dict[str, Any]]:
        """Replace the connection's mapping set atomically (PUT semantics); validated by the caller."""
        await self.session.execute(
            delete(sso_group_role_map).where(
                and_(
                    sso_group_role_map.c.tenant_id == as_uuid(tenant_id),
                    sso_group_role_map.c.connection_id == as_uuid(connection_id),
                )
            )
        )
        for mapping in mappings:
            await self.session.execute(
                insert(sso_group_role_map).values(
                    id=uuid4(),
                    tenant_id=as_uuid(tenant_id),
                    connection_id=as_uuid(connection_id),
                    match_type=str(mapping["match_type"]),
                    group_value=str(mapping["group_value"]),
                    role_id=as_uuid(mapping["role_id"]),
                    priority=int(mapping.get("priority", 100)),
                    is_active=bool(mapping.get("is_active", True)),
                    created_by=as_uuid(actor_id),
                )
            )
        await self.audit(
            tenant_id=tenant_id,
            actor_id=actor_id,
            action="sso.group_mappings.updated",
            resource_type="tenant_sso_connection",
            resource_id=connection_id,
            diff={"count": len(mappings)},
        )
        return await self.list_group_mappings(tenant_id, connection_id)

    # -------------------------------------------------------------- identity links -------------
    async def get_active_link(
        self, tenant_id: UUID | str, connection_id: UUID | str, issuer: str, subject: str
    ) -> dict[str, Any] | None:
        row = (
            (
                await self.session.execute(
                    select(user_identity_link).where(
                        and_(
                            user_identity_link.c.tenant_id == as_uuid(tenant_id),
                            user_identity_link.c.connection_id == as_uuid(connection_id),
                            user_identity_link.c.issuer == issuer,
                            user_identity_link.c.subject == subject,
                            user_identity_link.c.status == "ACTIVE",
                        )
                    )
                )
            )
            .mappings()
            .first()
        )
        return dict(row) if row else None

    async def get_link(self, tenant_id: UUID | str, link_id: UUID | str) -> dict[str, Any] | None:
        row = (
            (
                await self.session.execute(
                    select(user_identity_link).where(
                        and_(
                            user_identity_link.c.tenant_id == as_uuid(tenant_id),
                            user_identity_link.c.id == as_uuid(link_id),
                        )
                    )
                )
            )
            .mappings()
            .first()
        )
        return dict(row) if row else None

    async def list_identity_links(
        self,
        tenant_id: UUID | str,
        *,
        connection_id: UUID | str | None = None,
        identity_id: UUID | str | None = None,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        query = (
            select(
                user_identity_link.c.id,
                user_identity_link.c.identity_id,
                user_identity_link.c.connection_id,
                tenant_sso_connection.c.name.label("connection_name"),
                user_identity_link.c.issuer,
                user_identity_link.c.subject,
                user_identity_link.c.email_at_idp,
                user_identity_link.c.email_verified,
                user_identity_link.c.linked_at,
                user_identity_link.c.last_login_at,
                user_identity_link.c.status,
                user_identity_link.c.revoked_at,
                user_identity.c.email.label("identity_email"),
            )
            .select_from(user_identity_link)
            .join(
                tenant_sso_connection,
                and_(
                    tenant_sso_connection.c.tenant_id == user_identity_link.c.tenant_id,
                    tenant_sso_connection.c.id == user_identity_link.c.connection_id,
                ),
            )
            .join(user_identity, user_identity.c.id == user_identity_link.c.identity_id)
            .where(user_identity_link.c.tenant_id == as_uuid(tenant_id))
            .order_by(user_identity_link.c.linked_at.desc())
            .limit(limit)
            .offset(offset)
        )
        if connection_id is not None:
            query = query.where(user_identity_link.c.connection_id == as_uuid(connection_id))
        if identity_id is not None:
            query = query.where(user_identity_link.c.identity_id == as_uuid(identity_id))
        if status:
            query = query.where(user_identity_link.c.status == status.upper())
        rows = (await self.session.execute(query)).mappings().all()
        return [dict(row) for row in rows]

    async def insert_identity_link(
        self,
        tenant_id: UUID | str,
        *,
        connection_id: UUID | str,
        identity_id: UUID | str,
        issuer: str,
        subject: str,
        email_at_idp: str | None,
        email_verified: bool,
        actor_id: UUID | str | None = None,
    ) -> dict[str, Any]:
        link_id = uuid4()
        now = utc_now()
        await self.session.execute(
            insert(user_identity_link).values(
                id=link_id,
                tenant_id=as_uuid(tenant_id),
                identity_id=as_uuid(identity_id),
                connection_id=as_uuid(connection_id),
                issuer=issuer,
                subject=subject,
                email_at_idp=email_at_idp,
                email_verified=email_verified,
                linked_at=now,
                last_login_at=now,
                status="ACTIVE",
            )
        )
        await self.audit(
            tenant_id=tenant_id,
            actor_id=actor_id,
            action="identity.linked",
            resource_type="user_identity_link",
            resource_id=link_id,
            diff={"connection_id": str(connection_id), "issuer": issuer},
        )
        await emit_platform_event(
            self.session, "identity.linked", tenant_id=str(tenant_id), aggregate_type="user_identity_link",
            aggregate_id=str(link_id),
            payload={"tenant_id": str(tenant_id), "app_user_id": str(identity_id),
                     "identity_id": str(identity_id), "connection_id": str(connection_id),
                     "action": "LINKED", "sessions_revoked": 0},
        )
        created = await self.get_link(tenant_id, link_id)
        if created is None:  # pragma: no cover - defensive
            raise RuntimeError("identity link insert did not persist")
        return created

    async def revoke_identity_link(
        self, tenant_id: UUID | str, link_id: UUID | str, *, actor_id: UUID | str | None
    ) -> dict[str, Any] | None:
        current = await self.get_link(tenant_id, link_id)
        if current is None:
            return None
        if current["status"] != "REVOKED":
            await self.session.execute(
                update(user_identity_link)
                .where(
                    and_(
                        user_identity_link.c.tenant_id == as_uuid(tenant_id),
                        user_identity_link.c.id == as_uuid(link_id),
                    )
                )
                .values(status="REVOKED", revoked_at=utc_now())
            )
            await self.audit(
                tenant_id=tenant_id,
                actor_id=actor_id,
                action="identity.link.revoked",
                resource_type="user_identity_link",
                resource_id=link_id,
            )
        return await self.get_link(tenant_id, link_id)

    async def touch_link_login(self, tenant_id: UUID | str, link_id: UUID | str) -> None:
        await self.session.execute(
            update(user_identity_link)
            .where(
                and_(
                    user_identity_link.c.tenant_id == as_uuid(tenant_id),
                    user_identity_link.c.id == as_uuid(link_id),
                )
            )
            .values(last_login_at=utc_now())
        )

    # ------------------------------------------------- identity / membership (JIT) ------------
    async def get_identity(self, identity_id: UUID | str) -> dict[str, Any] | None:
        row = (
            (await self.session.execute(select(user_identity).where(user_identity.c.id == as_uuid(identity_id))))
            .mappings()
            .first()
        )
        return dict(row) if row else None

    async def get_identity_by_email(self, email: str) -> dict[str, Any] | None:
        row = (
            (await self.session.execute(select(user_identity).where(user_identity.c.email == email.strip().lower())))
            .mappings()
            .first()
        )
        return dict(row) if row else None

    async def tenant_member_by_email(self, tenant_id: UUID | str, email: str) -> dict[str, Any] | None:
        row = (
            (
                await self.session.execute(
                    select(app_user, user_identity)
                    .select_from(app_user)
                    .join(user_identity, user_identity.c.id == app_user.c.identity_id)
                    .where(
                        and_(
                            app_user.c.tenant_id == as_uuid(tenant_id),
                            user_identity.c.email == email.strip().lower(),
                        )
                    )
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            return None
        return {"app_user": dict(row), "identity": dict(row)}

    async def get_membership(self, tenant_id: UUID | str, identity_id: UUID | str) -> dict[str, Any] | None:
        row = (
            (
                await self.session.execute(
                    select(app_user).where(
                        and_(
                            app_user.c.tenant_id == as_uuid(tenant_id),
                            app_user.c.identity_id == as_uuid(identity_id),
                        )
                    )
                )
            )
            .mappings()
            .first()
        )
        return dict(row) if row else None

    async def count_active_memberships(self, identity_id: UUID | str) -> int:
        result = await self.session.execute(
            select(func.count())
            .select_from(app_user)
            .where(
                and_(
                    app_user.c.identity_id == as_uuid(identity_id),
                    app_user.c.status == "ACTIVE",
                    app_user.c.membership_status == "ACTIVE",
                )
            )
        )
        return int(result.scalar_one() or 0)

    async def create_identity(self, *, email: str, display_name: str | None = None) -> dict[str, Any]:
        identity_id = uuid4()
        now = utc_now()
        await self.session.execute(
            insert(user_identity).values(
                id=identity_id,
                email=email.strip().lower(),
                display_name=display_name,
                sso_subject=None,
                mfa_enabled=False,
                is_platform_admin=False,
                status="ACTIVE",
                created_at=now,
                updated_at=now,
                version=1,
            )
        )
        created = await self.get_identity(identity_id)
        if created is None:  # pragma: no cover - defensive
            raise RuntimeError("identity insert did not persist")
        return created

    async def create_membership(
        self, tenant_id: UUID | str, *, identity_id: UUID | str, email: str, actor_id: UUID | str | None = None
    ) -> dict[str, Any]:
        user_id = uuid4()
        now = utc_now()
        await self.session.execute(
            insert(app_user).values(
                id=user_id,
                tenant_id=as_uuid(tenant_id),
                identity_id=as_uuid(identity_id),
                email=email.strip().lower(),
                status="ACTIVE",
                membership_status="ACTIVE",
                joined_at=now,
                invited_by=None,
                token_version=0,
                is_service_account=False,
                last_login_at=now,
                created_at=now,
                updated_at=now,
                version=1,
                created_by=as_uuid(actor_id),
                updated_by=as_uuid(actor_id),
            )
        )
        created = await self.get_membership(tenant_id, identity_id)
        if created is None:  # pragma: no cover - defensive
            raise RuntimeError("membership insert did not persist")
        return created

    async def get_member(self, tenant_id: UUID | str, app_user_id: UUID | str) -> dict[str, Any] | None:
        row = (
            (
                await self.session.execute(
                    select(app_user).where(
                        and_(
                            app_user.c.tenant_id == as_uuid(tenant_id),
                            app_user.c.id == as_uuid(app_user_id),
                        )
                    )
                )
            )
            .mappings()
            .first()
        )
        return dict(row) if row else None

    # -------------------------------------------------------------- sessions -----------------
    async def revoke_sessions_for_identity(self, tenant_id: UUID | str, identity_id: UUID | str, *, reason: str) -> int:
        result = await self.session.execute(
            update(user_session)
            .where(
                and_(
                    user_session.c.tenant_id == as_uuid(tenant_id),
                    user_session.c.identity_id == as_uuid(identity_id),
                    user_session.c.status == "ACTIVE",
                )
            )
            .values(status="REVOKED", revoked_at=utc_now(), revoked_reason=reason)
        )
        return int(getattr(result, "rowcount", 0) or 0)
