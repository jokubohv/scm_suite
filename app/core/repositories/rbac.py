"""RBAC / tenant-administration repository (FS-00 API task-39).

Async SQLAlchemy 2.0 Core access to the platform tables created by migrations 0001–0003. The
repository is deliberately policy-free for DB shape and *tenant-scoped by construction* (every query
filters ``tenant_id``) while the API layer (or the pure security services) owns authorization/SoD
decisions. All writes append an ``audit_event`` row; append-only/versioning semantics come from the
migration triggers.

Tables: ``role``, ``permission_scope``, ``role_permission``, ``user_role``, ``user_data_scope``,
``app_user``, ``user_identity``, ``tenant_invitation``, ``user_session``, ``tenant_setting``,
``tenant``, ``audit_event``.
"""

from __future__ import annotations

import hashlib
import secrets
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import and_, delete, func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.models import platform as p

ALLOWED_SETTING_NAMESPACES = frozenset(
    {"inventory", "manufacturing", "wms", "replenishment", "costing", "pricing", "platform", "accounting", "rbac", "auth"}
)
INVITATION_TTL = timedelta(days=7)
AUDIT_ACTOR_TYPES = frozenset({"USER", "SERVICE", "SYSTEM"})


def _now() -> datetime:
    return datetime.now(UTC)


def utc(value: datetime | None) -> datetime | None:
    return value.astimezone(UTC) if value is not None else None


def hash_invitation_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class RbacRepository:
    """One request-scoped repository (no commits; the API dependency owns the transaction)."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # ------------------------------------------------------------------ basics / audit ------
    async def get_tenant(self, tenant_id: UUID | str) -> Mapping[str, Any] | None:
        row = await self.session.execute(
            select(p.tenant).where(p.tenant.c.id == tenant_id, p.tenant.c.tenant_id == tenant_id)
        )
        mapping = row.mappings().first()
        return dict(mapping) if mapping else None

    async def get_identity(self, identity_id: UUID | str) -> Mapping[str, Any] | None:
        row = await self.session.execute(select(p.user_identity).where(p.user_identity.c.id == identity_id))
        mapping = row.mappings().first()
        return dict(mapping) if mapping else None

    async def is_platform_admin(self, identity_id: UUID | str) -> bool:
        row = await self.session.execute(
            select(p.user_identity.c.is_platform_admin).where(p.user_identity.c.id == identity_id)
        )
        return bool(row.scalar_one_or_none())

    async def audit(
        self,
        *,
        tenant_id: UUID | str,
        actor_type: str = "USER",
        actor_id: UUID | str | None,
        action: str,
        resource_type: str,
        resource_id: UUID | str | None,
        diff: Mapping[str, Any] | None = None,
        source: str = "API",
    ) -> UUID:
        if actor_type not in AUDIT_ACTOR_TYPES:
            raise ValueError(f"invalid audit actor_type {actor_type!r}")
        event_id = uuid4()
        await self.session.execute(
            insert(p.audit_event).values(
                id=event_id,
                tenant_id=tenant_id,
                event_id=event_id,
                actor_type=actor_type,
                actor_id=actor_id,
                action=action,
                resource_type=resource_type,
                resource_id=str(resource_id) if resource_id is not None else None,
                diff=dict(diff or {}),
                source=source,
                occurred_at=_now(),
            )
        )
        return event_id

    # ---------------------------------------------------------------- permission scopes -----
    async def scope_rows(self, tenant_id: UUID | str, names: Iterable[str]) -> dict[str, Mapping[str, Any]]:
        wanted = list(dict.fromkeys(names))
        if not wanted:
            return {}
        rows = await self.session.execute(
            select(p.permission_scope).where(
                p.permission_scope.c.tenant_id == tenant_id, p.permission_scope.c.scope.in_(wanted)
            )
        )
        return {row["scope"]: dict(row) for row in rows.mappings().all()}

    async def ensure_permission_scopes(self, tenant_id: UUID | str, entries: Sequence[Mapping[str, Any]]) -> None:
        """Provision the requested catalogue scopes for a tenant (idempotent; used by tests/provisioning)."""
        existing = await self.scope_rows(tenant_id, [str(entry["scope"]) for entry in entries])
        for entry in entries:
            scope = str(entry["scope"])
            if scope in existing:
                continue
            await self.session.execute(
                insert(p.permission_scope).values(
                    id=uuid4(),
                    tenant_id=tenant_id,
                    scope=scope,
                    domain=str(entry.get("domain", scope.split(".")[0])),
                    action=str(entry.get("action", scope.split(".")[-1])),
                    description=str(entry.get("description", "")),
                    requires_approval=bool(entry.get("requires_approval", False)),
                    is_platform_only=bool(entry.get("platform_only", False)),
                    is_assignable=bool(entry.get("assignable", True)),
                    owner_feature_set=str(entry.get("owner_feature_set", "FS-00")),
                )
            )

    # ------------------------------------------------------------------------- roles --------
    def _role_scope_subquery(self):
        return (
            select(func.count())
            .select_from(p.role_permission)
            .where(p.role_permission.c.tenant_id == p.role.c.tenant_id, p.role_permission.c.role_id == p.role.c.id)
            .scalar_subquery()
        )

    def _role_assignment_subquery(self):
        return (
            select(func.count())
            .select_from(p.user_role)
            .where(
                p.user_role.c.tenant_id == p.role.c.tenant_id,
                p.user_role.c.role_id == p.role.c.id,
                p.user_role.c.effective_to.is_(None),
            )
            .scalar_subquery()
        )

    async def list_roles(
        self, tenant_id: UUID | str, *, include_inactive: bool = False, is_template: bool | None = None
    ) -> list[dict[str, Any]]:
        stmt = select(
            p.role,
            self._role_scope_subquery().label("scope_count"),
            self._role_assignment_subquery().label("assignment_count"),
        ).where(p.role.c.tenant_id == tenant_id)
        if not include_inactive:
            stmt = stmt.where(p.role.c.inactive_at.is_(None))
        if is_template is not None:
            stmt = stmt.where(p.role.c.is_template.is_(is_template))
        rows = await self.session.execute(stmt.order_by(p.role.c.is_template.desc(), p.role.c.code))
        results = []
        for row in rows.mappings().all():
            role = {column.name: row[column.name] for column in p.role.c}
            role["scope_count"] = int(row["scope_count"])
            role["assignment_count"] = int(row["assignment_count"])
            results.append(role)
        return results

    async def get_role(self, tenant_id: UUID | str, role_id: UUID | str) -> dict[str, Any] | None:
        rows = await self.session.execute(
            select(p.role).where(p.role.c.tenant_id == tenant_id, p.role.c.id == role_id)
        )
        mapping = rows.mappings().first()
        if not mapping:
            return None
        role = dict(mapping)
        role["scopes"] = await self.role_scope_names(tenant_id, role_id)
        role["data_scopes"] = await self.role_data_scopes(tenant_id, role_id)
        counts = await self.session.execute(
            select(func.count())
            .select_from(p.user_role)
            .where(
                p.user_role.c.tenant_id == tenant_id,
                p.user_role.c.role_id == role_id,
                p.user_role.c.effective_to.is_(None),
            )
        )
        role["assignment_count"] = int(counts.scalar_one())
        return role

    async def role_scope_names(self, tenant_id: UUID | str, role_id: UUID | str) -> list[str]:
        rows = await self.session.execute(
            select(p.permission_scope.c.scope)
            .select_from(
                p.role_permission.join(
                    p.permission_scope,
                    and_(
                        p.permission_scope.c.id == p.role_permission.c.scope_id,
                        p.permission_scope.c.tenant_id == p.role_permission.c.tenant_id,
                    ),
                )
            )
            .where(p.role_permission.c.tenant_id == tenant_id, p.role_permission.c.role_id == role_id)
            .order_by(p.permission_scope.c.scope)
        )
        return [str(value) for value in rows.scalars().all()]

    async def role_data_scopes(self, tenant_id: UUID | str, role_id: UUID | str) -> list[dict[str, Any]]:
        rows = await self.session.execute(
            select(p.user_data_scope).where(
                p.user_data_scope.c.tenant_id == tenant_id, p.user_data_scope.c.role_id == role_id
            )
        )
        return [
            {
                "scope_type": row["scope_type"],
                "scope_value": row["scope_value"],
                "include_descendants": row["include_descendants"],
            }
            for row in rows.mappings().all()
        ]

    async def role_code_exists(self, tenant_id: UUID | str, code: str) -> bool:
        row = await self.session.execute(
            select(func.count()).select_from(p.role).where(p.role.c.tenant_id == tenant_id, p.role.c.code == code)
        )
        return int(row.scalar_one()) > 0

    async def create_role(
        self,
        tenant_id: UUID | str,
        *,
        code: str,
        name: str,
        description: str = "",
        scopes: Sequence[str] = (),
        data_scopes: Sequence[Mapping[str, Any]] = (),
        is_template: bool = False,
        is_editable: bool = True,
        actor_id: UUID | str | None = None,
    ) -> dict[str, Any]:
        role_id = uuid4()
        await self.session.execute(
            insert(p.role).values(
                id=role_id,
                tenant_id=tenant_id,
                code=code,
                name=name,
                description=description,
                is_system=is_template,
                is_template=is_template,
                is_editable=is_editable,
            )
        )
        await self.replace_role_permissions(tenant_id, role_id, scopes)
        await self.replace_role_data_scopes(tenant_id, role_id, data_scopes)
        await self.audit(
            tenant_id=tenant_id, actor_id=actor_id, action="role.created", resource_type="role",
            resource_id=role_id, diff={"code": code, "scopes": sorted(scopes)},
        )
        role = await self.get_role(tenant_id, role_id)
        assert role is not None
        return role

    async def update_role(
        self,
        tenant_id: UUID | str,
        role_id: UUID | str,
        *,
        name: str | None = None,
        description: str | None = None,
        inactive: bool | None = None,
        actor_id: UUID | str | None = None,
    ) -> dict[str, Any] | None:
        before = await self.get_role(tenant_id, role_id)
        if before is None:
            return None
        values: dict[str, Any] = {}
        if name is not None:
            values["name"] = name
        if description is not None:
            values["description"] = description
        if inactive is not None:
            values["inactive_at"] = _now() if inactive else None
        if values:
            values["updated_at"] = _now()
            await self.session.execute(
                update(p.role)
                .where(p.role.c.tenant_id == tenant_id, p.role.c.id == role_id)
                .values(**values)
            )
        await self.audit(
            tenant_id=tenant_id, actor_id=actor_id, action="role.updated", resource_type="role",
            resource_id=role_id, diff={"before": {k: str(v) for k, v in before.items() if k in values}, "after": {k: str(v) for k, v in values.items()}},
        )
        return await self.get_role(tenant_id, role_id)

    async def replace_role_permissions(
        self, tenant_id: UUID | str, role_id: UUID | str, scopes: Sequence[str], *, actor_id: UUID | str | None = None
    ) -> tuple[list[str], dict[str, list[str]]]:
        before = await self.role_scope_names(tenant_id, role_id)
        scope_map = await self.scope_rows(tenant_id, scopes)
        await self.session.execute(
            delete(p.role_permission).where(
                p.role_permission.c.tenant_id == tenant_id, p.role_permission.c.role_id == role_id
            )
        )
        for name in dict.fromkeys(scopes):
            entry = scope_map[name]
            await self.session.execute(
                insert(p.role_permission).values(tenant_id=tenant_id, role_id=role_id, scope_id=entry["id"])
            )
        after = sorted(dict.fromkeys(scopes))
        diff = {"added": sorted(set(after) - set(before)), "removed": sorted(set(before) - set(after))}
        await self.audit(
            tenant_id=tenant_id, actor_id=actor_id, action="role.permissions_changed", resource_type="role",
            resource_id=role_id, diff=diff,
        )
        return after, diff

    async def replace_role_data_scopes(
        self,
        tenant_id: UUID | str,
        role_id: UUID | str,
        data_scopes: Sequence[Mapping[str, Any]],
        *,
        actor_id: UUID | str | None = None,
    ) -> None:
        await self.session.execute(
            delete(p.user_data_scope).where(
                p.user_data_scope.c.tenant_id == tenant_id, p.user_data_scope.c.role_id == role_id
            )
        )
        for entry in data_scopes:
            await self.session.execute(
                insert(p.user_data_scope).values(
                    id=uuid4(),
                    tenant_id=tenant_id,
                    role_id=role_id,
                    scope_type=str(entry["scope_type"]),
                    scope_value=str(entry["scope_value"]),
                    include_descendants=bool(entry.get("include_descendants", False)),
                )
            )
        await self.audit(
            tenant_id=tenant_id, actor_id=actor_id, action="role.data_scopes_changed", resource_type="role",
            resource_id=role_id, diff={"scopes": [dict(entry) for entry in data_scopes]},
        )

    async def inactivate_role(
        self,
        tenant_id: UUID | str,
        role_id: UUID | str,
        *,
        actor_id: UUID | str | None = None,
    ) -> dict[str, Any] | None:
        return await self.update_role(tenant_id, role_id, inactive=True, actor_id=actor_id)

    # ------------------------------------------------------------------------- users --------
    async def list_users(
        self,
        tenant_id: UUID | str,
        *,
        membership_status: str | None = None,
        email: str | None = None,
        role_id: UUID | str | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> tuple[list[dict[str, Any]], str | None]:
        stmt = (
            select(
                p.app_user.c.id.label("app_user_id"),
                p.app_user.c.identity_id,
                p.app_user.c.email.label("user_email"),
                p.app_user.c.membership_status,
                p.app_user.c.joined_at,
                p.user_identity.c.email.label("identity_email"),
                p.user_identity.c.display_name.label("identity_display_name"),
            )
            .select_from(p.app_user.join(p.user_identity, p.user_identity.c.id == p.app_user.c.identity_id))
            .where(p.app_user.c.tenant_id == tenant_id)
        )
        if membership_status:
            stmt = stmt.where(p.app_user.c.membership_status == membership_status)
        if email:
            stmt = stmt.where(p.app_user.c.email.ilike(f"%{email}%"))
        if role_id:
            stmt = stmt.where(
                p.app_user.c.id.in_(
                    select(p.user_role.c.user_id).where(
                        p.user_role.c.tenant_id == tenant_id,
                        p.user_role.c.role_id == role_id,
                        p.user_role.c.effective_to.is_(None),
                    )
                )
            )
        if cursor:
            stmt = stmt.where(p.app_user.c.id > cursor)
        stmt = stmt.order_by(p.app_user.c.id).limit(limit + 1)
        rows = await self.session.execute(stmt)
        items = list(rows.mappings().all())
        next_cursor = str(items[limit]["app_user_id"]) if len(items) > limit else None
        results = []
        for row in items[:limit]:
            user = {
                "id": row["app_user_id"], "tenant_id": tenant_id, "identity_id": row["identity_id"],
                "email": str(row["user_email"]), "membership_status": row["membership_status"],
                "joined_at": row["joined_at"], "identity_email": row["identity_email"],
                "identity_display_name": row["identity_display_name"],
            }
            user["roles"] = await self.user_role_names(tenant_id, user["id"])
            results.append(user)
        return results, next_cursor

    async def get_user(self, tenant_id: UUID | str, app_user_id: UUID | str) -> dict[str, Any] | None:
        rows = await self.session.execute(
            select(
                p.app_user.c.id.label("app_user_id"),
                p.app_user.c.identity_id,
                p.app_user.c.email.label("user_email"),
                p.app_user.c.display_name.label("user_display_name"),
                p.app_user.c.status.label("user_status"),
                p.app_user.c.membership_status,
                p.app_user.c.joined_at,
                p.app_user.c.is_service_account,
                p.app_user.c.token_version,
                p.app_user.c.locale,
                p.app_user.c.timezone,
                p.user_identity.c.email.label("identity_email"),
                p.user_identity.c.display_name.label("identity_display_name"),
                p.user_identity.c.mfa_enabled,
                p.user_identity.c.is_platform_admin,
                p.user_identity.c.status.label("identity_status"),
            )
            .select_from(
                p.app_user.join(p.user_identity, p.user_identity.c.id == p.app_user.c.identity_id)
            )
            .where(p.app_user.c.tenant_id == tenant_id, p.app_user.c.id == app_user_id)
        )
        row = rows.mappings().first()
        if not row:
            return None
        user = {
            "id": row["app_user_id"], "tenant_id": tenant_id, "identity_id": row["identity_id"],
            "email": str(row["user_email"]), "display_name": row["user_display_name"],
            "status": row["user_status"], "membership_status": row["membership_status"],
            "joined_at": row["joined_at"], "is_service_account": row["is_service_account"],
            "token_version": row["token_version"], "locale": row["locale"], "timezone": row["timezone"],
        }
        user["identity"] = {
            "id": row["identity_id"], "email": str(row["identity_email"]),
            "display_name": row["identity_display_name"], "mfa_enabled": row["mfa_enabled"],
            "is_platform_admin": row["is_platform_admin"], "status": row["identity_status"],
        }
        user["roles"] = await self.user_role_names(tenant_id, app_user_id)
        user["data_scopes"] = await self.user_data_scopes(tenant_id, app_user_id)
        return user

    async def user_role_names(self, tenant_id: UUID | str, app_user_id: UUID | str) -> list[str]:
        rows = await self.session.execute(
            select(p.role.c.code)
            .select_from(
                p.user_role.join(
                    p.role,
                    and_(p.role.c.id == p.user_role.c.role_id, p.role.c.tenant_id == p.user_role.c.tenant_id),
                )
            )
            .where(
                p.user_role.c.tenant_id == tenant_id,
                p.user_role.c.user_id == app_user_id,
                p.user_role.c.effective_to.is_(None),
            )
            .order_by(p.role.c.code)
        )
        return [str(value) for value in rows.scalars().all()]

    async def user_data_scopes(self, tenant_id: UUID | str, app_user_id: UUID | str) -> list[dict[str, Any]]:
        rows = await self.session.execute(
            select(p.user_data_scope.c.scope_type, p.user_data_scope.c.scope_value, p.user_data_scope.c.include_descendants)
            .where(p.user_data_scope.c.tenant_id == tenant_id, p.user_data_scope.c.user_id == app_user_id)
        )
        return [dict(row) for row in rows.mappings().all()]

    async def update_user(
        self,
        tenant_id: UUID | str,
        app_user_id: UUID | str,
        *,
        display_name: str | None = None,
        locale: str | None = None,
        timezone: str | None = None,
        membership_status: str | None = None,
        actor_id: UUID | str | None = None,
    ) -> dict[str, Any] | None:
        user = await self.get_user(tenant_id, app_user_id)
        if user is None:
            return None
        values: dict[str, Any] = {}
        if locale is not None:
            values["locale"] = locale
        if timezone is not None:
            values["timezone"] = timezone
        if membership_status is not None:
            values["membership_status"] = membership_status
        if values:
            values["updated_at"] = _now()
            await self.session.execute(
                update(p.app_user).where(p.app_user.c.tenant_id == tenant_id, p.app_user.c.id == app_user_id).values(**values)
            )
        if display_name is not None:
            await self.session.execute(
                update(p.user_identity)
                .where(p.user_identity.c.id == user["identity_id"])
                .values(display_name=display_name, updated_at=_now())
            )
        if membership_status in {"SUSPENDED"}:
            await self.revoke_sessions(tenant_id, app_user_id, reason="membership_suspended", actor_id=actor_id)
        await self.audit(
            tenant_id=tenant_id, actor_id=actor_id, action="membership.updated", resource_type="app_user",
            resource_id=app_user_id, diff={k: str(v) for k, v in values.items()},
        )
        return await self.get_user(tenant_id, app_user_id)

    async def assign_role(
        self,
        tenant_id: UUID | str,
        app_user_id: UUID | str,
        role_id: UUID | str,
        *,
        effective_from: datetime | None = None,
        effective_to: datetime | None = None,
        actor_id: UUID | str | None = None,
    ) -> None:
        now = effective_from or _now()
        await self.session.execute(
            insert(p.user_role).values(
                tenant_id=tenant_id,
                user_id=app_user_id,
                role_id=role_id,
                effective_from=now,
                effective_to=effective_to,
                assigned_by=actor_id,
            )
        )
        await self.audit(
            tenant_id=tenant_id, actor_id=actor_id, action="role.assigned", resource_type="app_user",
            resource_id=app_user_id, diff={"role_id": str(role_id), "effective_from": now.isoformat()},
        )

    async def end_role_assignment(
        self, tenant_id: UUID | str, app_user_id: UUID | str, role_id: UUID | str, *, actor_id: UUID | str | None = None
    ) -> int:
        result = await self.session.execute(
            update(p.user_role)
            .where(
                p.user_role.c.tenant_id == tenant_id,
                p.user_role.c.user_id == app_user_id,
                p.user_role.c.role_id == role_id,
                p.user_role.c.effective_to.is_(None),
            )
            .values(effective_to=_now())
        )
        await self.audit(
            tenant_id=tenant_id, actor_id=actor_id, action="role.unassigned", resource_type="app_user",
            resource_id=app_user_id, diff={"role_id": str(role_id)},
        )
        return int(getattr(result, "rowcount", 0) or 0)

    async def count_active_admins(self, tenant_id: UUID | str) -> int:
        stmt = (
            select(func.count(func.distinct(p.app_user.c.id)))
            .select_from(
                p.app_user.join(
                    p.user_role,
                    and_(p.user_role.c.tenant_id == p.app_user.c.tenant_id, p.user_role.c.user_id == p.app_user.c.id),
                ).join(
                    p.role,
                    and_(p.role.c.id == p.user_role.c.role_id, p.role.c.tenant_id == p.user_role.c.tenant_id),
                )
            )
            .where(
                p.app_user.c.tenant_id == tenant_id,
                p.app_user.c.membership_status == "ACTIVE",
                p.app_user.c.status == "ACTIVE",
                p.role.c.code == "admin",
                p.user_role.c.effective_to.is_(None),
            )
        )
        return int((await self.session.execute(stmt)).scalar_one())

    async def is_active_admin(self, tenant_id: UUID | str, app_user_id: UUID | str) -> bool:
        user = await self.get_user(tenant_id, app_user_id)
        return bool(user and user["membership_status"] == "ACTIVE" and "admin" in user["roles"])

    async def effective_permissions(self, tenant_id: UUID | str, app_user_id: UUID | str) -> dict[str, Any]:
        user = await self.get_user(tenant_id, app_user_id)
        if user is None:
            return {}
        rows = await self.session.execute(
            select(p.permission_scope.c.scope)
            .select_from(
                p.user_role.join(
                    p.role_permission,
                    and_(p.role_permission.c.role_id == p.user_role.c.role_id, p.role_permission.c.tenant_id == p.user_role.c.tenant_id),
                ).join(
                    p.permission_scope,
                    and_(p.permission_scope.c.id == p.role_permission.c.scope_id, p.permission_scope.c.tenant_id == p.role_permission.c.tenant_id),
                )
            )
            .where(
                p.user_role.c.tenant_id == tenant_id,
                p.user_role.c.user_id == app_user_id,
                p.user_role.c.effective_to.is_(None),
            )
            .order_by(p.permission_scope.c.scope)
        )
        scopes = sorted({str(value) for value in rows.scalars().all()})
        return {
            "app_user_id": str(app_user_id),
            "roles": user["roles"],
            "scopes": scopes,
            "data_scopes": user["data_scopes"],
        }

    async def revoke_sessions(
        self, tenant_id: UUID | str, app_user_id: UUID | str, *, reason: str, actor_id: UUID | str | None = None
    ) -> int:
        await self.session.execute(
            update(p.user_session)
            .where(p.user_session.c.tenant_id == tenant_id, p.user_session.c.app_user_id == app_user_id, p.user_session.c.status == "ACTIVE")
            .values(status="REVOKED", revoked_at=_now(), revoked_reason=reason)
        )
        row = await self.session.execute(
            update(p.app_user)
            .where(p.app_user.c.tenant_id == tenant_id, p.app_user.c.id == app_user_id)
            .values(token_version=p.app_user.c.token_version + 1, updated_at=_now())
            .returning(p.app_user.c.token_version)
        )
        token_version = int(row.scalar_one_or_none() or 0)
        await self.audit(
            tenant_id=tenant_id, actor_id=actor_id, action="sessions.revoked", resource_type="app_user",
            resource_id=app_user_id, diff={"reason": reason, "token_version": token_version},
        )
        return token_version

    async def count_active_sessions(self, tenant_id: UUID | str, app_user_id: UUID | str) -> int:
        row = await self.session.execute(
            select(func.count()).select_from(p.user_session).where(
                p.user_session.c.tenant_id == tenant_id,
                p.user_session.c.app_user_id == app_user_id,
                p.user_session.c.status == "ACTIVE",
            )
        )
        return int(row.scalar_one())

    # ------------------------------------------------------------------- invitations --------
    async def create_invitation(
        self,
        tenant_id: UUID | str,
        *,
        email: str,
        intended_role_ids: Sequence[UUID | str],
        invited_by: UUID | str,
        expires_at: datetime | None = None,
    ) -> dict[str, Any]:
        token = secrets.token_urlsafe(32)
        invitation_id = uuid4()
        expiry = expires_at or (_now() + INVITATION_TTL)
        await self.session.execute(
            insert(p.tenant_invitation).values(
                id=invitation_id,
                tenant_id=tenant_id,
                email=email.lower(),
                intended_role_ids=[role for role in intended_role_ids],
                token_hash=hash_invitation_token(token),
                status="PENDING",
                invited_by=invited_by,
                expires_at=expiry,
            )
        )
        await self.audit(
            tenant_id=tenant_id, actor_id=invited_by, action="invitation.created", resource_type="tenant_invitation",
            resource_id=invitation_id, diff={"email": email.lower(), "roles": [str(r) for r in intended_role_ids]},
        )
        return {"id": invitation_id, "token": token, "expires_at": expiry, "status": "PENDING"}

    async def list_invitations(
        self, tenant_id: UUID | str, *, status: str | None = None, email: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        stmt = select(p.tenant_invitation).where(p.tenant_invitation.c.tenant_id == tenant_id)
        if status:
            stmt = stmt.where(p.tenant_invitation.c.status == status)
        if email:
            stmt = stmt.where(p.tenant_invitation.c.email.ilike(f"%{email}%"))
        rows = await self.session.execute(stmt.order_by(p.tenant_invitation.c.created_at.desc()).limit(limit))
        return [
            {
                "id": row["id"], "email": row["email"], "status": row["status"],
                "intended_role_ids": list(row["intended_role_ids"] or []),
                "expires_at": utc(row["expires_at"]), "invited_by": row["invited_by"],
                "created_at": utc(row["created_at"]),
            }
            for row in rows.mappings().all()
        ]

    async def get_invitation(self, tenant_id: UUID | str, invitation_id: UUID | str) -> Mapping[str, Any] | None:
        row = await self.session.execute(
            select(p.tenant_invitation).where(
                p.tenant_invitation.c.tenant_id == tenant_id, p.tenant_invitation.c.id == invitation_id
            )
        )
        mapping = row.mappings().first()
        return dict(mapping) if mapping else None

    async def resend_invitation(self, tenant_id: UUID | str, invitation_id: UUID | str, *, actor_id: UUID | str | None = None) -> dict[str, Any] | None:
        token = secrets.token_urlsafe(32)
        expiry = _now() + INVITATION_TTL
        result = await self.session.execute(
            update(p.tenant_invitation)
            .where(
                p.tenant_invitation.c.tenant_id == tenant_id,
                p.tenant_invitation.c.id == invitation_id,
                p.tenant_invitation.c.status == "PENDING",
            )
            .values(token_hash=hash_invitation_token(token), expires_at=expiry, updated_at=_now())
            .returning(p.tenant_invitation.c.id)
        )
        if result.scalar_one_or_none() is None:
            return None
        await self.audit(
            tenant_id=tenant_id, actor_id=actor_id, action="invitation.resent", resource_type="tenant_invitation",
            resource_id=invitation_id, diff={"expires_at": expiry.isoformat()},
        )
        return {"id": invitation_id, "token": token, "expires_at": expiry, "status": "PENDING"}

    async def revoke_invitation(
        self, tenant_id: UUID | str, invitation_id: UUID | str, *, reason: str, actor_id: UUID | str | None = None
    ) -> dict[str, Any] | None:
        result = await self.session.execute(
            update(p.tenant_invitation)
            .where(
                p.tenant_invitation.c.tenant_id == tenant_id,
                p.tenant_invitation.c.id == invitation_id,
                p.tenant_invitation.c.status == "PENDING",
            )
            .values(status="REVOKED", updated_at=_now())
            .returning(p.tenant_invitation.c.id)
        )
        if result.scalar_one_or_none() is None:
            return None
        await self.audit(
            tenant_id=tenant_id, actor_id=actor_id, action="invitation.revoked", resource_type="tenant_invitation",
            resource_id=invitation_id, diff={"reason": reason},
        )
        return {"id": invitation_id, "status": "REVOKED"}

    # ---------------------------------------------------------------- tenant settings ------
    async def list_tenant_settings(self, tenant_id: UUID | str, *, namespace: str | None = None) -> list[dict[str, Any]]:
        stmt = select(p.tenant_settings).where(p.tenant_settings.c.tenant_id == tenant_id)
        if namespace:
            stmt = stmt.where(p.tenant_settings.c.namespace == namespace)
        rows = await self.session.execute(stmt.order_by(p.tenant_settings.c.namespace, p.tenant_settings.c.key))
        return [
            {
                "namespace": row["namespace"], "key": row["key"], "value": row["value"],
                "value_type": row["value_type"], "source": "DEFAULT" if row["is_default"] else "OVERRIDE",
                "is_default": row["is_default"], "updated_at": utc(row["updated_at"]),
            }
            for row in rows.mappings().all()
        ]

    async def get_tenant_setting(self, tenant_id: UUID | str, namespace: str, key: str) -> Mapping[str, Any] | None:
        row = await self.session.execute(
            select(p.tenant_settings).where(
                p.tenant_settings.c.tenant_id == tenant_id,
                p.tenant_settings.c.namespace == namespace,
                p.tenant_settings.c.key == key,
            )
        )
        mapping = row.mappings().first()
        return dict(mapping) if mapping else None

    async def set_tenant_setting(
        self,
        tenant_id: UUID | str,
        *,
        namespace: str,
        key: str,
        value: Any,
        value_type: str = "JSON",
        actor_id: UUID | str | None = None,
    ) -> dict[str, Any]:
        before = await self.get_tenant_setting(tenant_id, namespace, key)
        if before:
            await self.session.execute(
                update(p.tenant_settings)
                .where(
                    p.tenant_settings.c.tenant_id == tenant_id,
                    p.tenant_settings.c.namespace == namespace,
                    p.tenant_settings.c.key == key,
                )
                .values(value=value, value_type=value_type, is_default=False, updated_at=_now())
            )
        else:
            await self.session.execute(
                insert(p.tenant_settings).values(
                    id=uuid4(), tenant_id=tenant_id, namespace=namespace, key=key,
                    value=value, value_type=value_type, is_default=False,
                )
            )
        await self.audit(
            tenant_id=tenant_id, actor_id=actor_id, action="tenant_setting.updated", resource_type="tenant_setting",
            resource_id=f"{namespace}.{key}", diff={"value": value, "value_type": value_type},
        )
        row = await self.get_tenant_setting(tenant_id, namespace, key)
        assert row is not None
        return {
            "namespace": row["namespace"], "key": row["key"], "value": row["value"],
            "value_type": row["value_type"], "source": "OVERRIDE", "is_default": row["is_default"],
            "updated_at": utc(row["updated_at"]),
        }

    # ------------------------------------------------------------------- tenancy self ------
    async def list_identity_memberships(self, identity_id: UUID | str) -> list[dict[str, Any]]:
        rows = await self.session.execute(
            select(
                p.app_user.c.id.label("app_user_id"),
                p.app_user.c.tenant_id,
                p.app_user.c.membership_status,
                p.tenant.c.code.label("tenant_code"),
                p.tenant.c.name.label("tenant_name"),
                p.tenant.c.status.label("tenant_status"),
            )
            .select_from(p.app_user.join(p.tenant, p.tenant.c.id == p.app_user.c.tenant_id))
            .where(p.app_user.c.identity_id == identity_id)
            .order_by(p.tenant.c.code)
        )
        items = []
        for row in rows.mappings().all():
            items.append(
                {
                    "tenant_id": row["tenant_id"],
                    "tenant_code": row["tenant_code"],
                    "tenant_name": row["tenant_name"],
                    "tenant_status": row["tenant_status"],
                    "app_user_id": row["app_user_id"],
                    "membership_status": row["membership_status"],
                    "roles": await self.user_role_names(row["tenant_id"], row["app_user_id"]),
                }
            )
        return items

    async def resolve_switch_target(self, identity_id: UUID | str, tenant_id: UUID | str) -> dict[str, Any] | None:
        rows = await self.session.execute(
            select(p.app_user, p.tenant)
            .join(p.tenant, p.tenant.c.id == p.app_user.c.tenant_id)
            .where(p.app_user.c.identity_id == identity_id, p.app_user.c.tenant_id == tenant_id)
        )
        mapping = rows.mappings().first()
        if not mapping:
            return None
        user, tenant = dict(mapping["app_user"]), dict(mapping["tenant"])
        if user["membership_status"] != "ACTIVE" or tenant["status"] != "ACTIVE":
            return None
        return {"app_user_id": user["id"], "tenant_id": tenant["id"], "tenant_code": tenant["code"], "tenant_name": tenant["name"]}

    async def count_roles_with_assignments(self, tenant_id: UUID | str, role_id: UUID | str) -> int:
        row = await self.session.execute(
            select(func.count()).select_from(p.user_role).where(
                p.user_role.c.tenant_id == tenant_id,
                p.user_role.c.role_id == role_id,
                p.user_role.c.effective_to.is_(None),
            )
        )
        return int(row.scalar_one())


__all__ = [
    "ALLOWED_SETTING_NAMESPACES",
    "INVITATION_TTL",
    "RbacRepository",
    "hash_invitation_token",
]
