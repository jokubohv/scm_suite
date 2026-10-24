"""Capability-scope catalogue and authorization evaluation (pure; FS-00 Δ-RBAC).

Authoritative input: ``design/permissions.json`` (plan v1.5.0: 255 scopes = 241 assignable + 13
service-only + 1 platform-only, 7 role templates, 14 SoD pairs, 18 approval scopes, public/self-service
allowlists, aliases). See ARCHITECTURE §5.2 and design/PERMISSIONS-ROLES-ADDENDUM.md.

This module performs **no I/O beyond reading that read-only JSON catalogue** and never touches a DB or
network. It answers: role → scopes, effective permissions, deny-by-default route guard, SoD conflicts,
assignability, privilege-escalation and last-administrator guards.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.core.security.errors import (
    ForbiddenScope,
    LastAdminLockout,
    PrivilegeEscalation,
    ScopeCatalogueError,
    ScopeNotAssignable,
    SecurityError,
    SodConflict,
    TemplateImmutable,
    UnknownRole,
    UnknownScope,
)

DEFAULT_CATALOGUE_PATH = Path(__file__).resolve().parents[3] / "design" / "permissions.json"
ADMIN_BREAK_GLASS_ROLE = "admin"
ROLE_ADMIN_SCOPE = "platform.role.manage"
USER_ADMIN_SCOPE = "platform.users.manage"


class ScopeClass(StrEnum):
    ASSIGNABLE = "ASSIGNABLE"
    SERVICE_ONLY = "SERVICE_ONLY"
    PLATFORM_ONLY = "PLATFORM_ONLY"
    ALIAS = "ALIAS"
    UNKNOWN = "UNKNOWN"


class RouteClass(StrEnum):
    PUBLIC = "PUBLIC"
    SELF_SERVICE = "SELF_SERVICE"
    SCOPED = "SCOPED"


class SodContext(StrEnum):
    CUSTOM_ROLE_CREATION = "CUSTOM_ROLE_CREATION"
    ROLE_UPDATE = "ROLE_UPDATE"
    ASSIGNMENT = "ASSIGNMENT"
    APPROVAL = "APPROVAL"


# --------------------------------------------------------------------------- models -------------


@dataclass(frozen=True, slots=True)
class CapabilityScope:
    scope: str
    domain: str
    action: str
    requires_approval: bool
    platform_only: bool
    service_only: bool
    assignable: bool
    owner_feature_set: str

    @property
    def scope_class(self) -> ScopeClass:
        if self.platform_only:
            return ScopeClass.PLATFORM_ONLY
        if self.service_only:
            return ScopeClass.SERVICE_ONLY
        return ScopeClass.ASSIGNABLE if self.assignable else ScopeClass.UNKNOWN


@dataclass(frozen=True, slots=True)
class RoleTemplate:
    key: str
    name: str
    system: bool
    editable: bool
    cloneable: bool
    description: str
    scopes: frozenset[str]

    def require_mutable(self) -> None:
        if not self.editable or self.system:
            raise TemplateImmutable(f"system role template {self.key!r} is clone-only")


@dataclass(frozen=True, slots=True)
class SodPair:
    a: str
    b: str
    enforcement: str
    reason: str = ""

    @property
    def as_frozenset(self) -> frozenset[str]:
        return frozenset((self.a, self.b))

    def conflicts_with(self, scopes: frozenset[str]) -> bool:
        return self.a in scopes and self.b in scopes


@dataclass(frozen=True, slots=True)
class EffectivePermissions:
    """Resolved capability + data scopes for one request (immediate; no session refresh needed)."""

    scopes: frozenset[str]
    roles: tuple[str, ...] = ()
    data_scopes: Mapping[str, tuple[str, ...]] = field(default_factory=dict)

    def has(self, scope: str) -> bool:
        return scope in self.scopes

    def require(self, scope: str) -> None:
        if not self.has(scope):
            raise ForbiddenScope(f"missing required capability scope {scope!r}", details={"required_scope": scope})

    def has_any(self, *scopes: str) -> bool:
        return any(scope in self.scopes for scope in scopes)


@dataclass(frozen=True, slots=True)
class SodEvaluation:
    ok: bool
    conflicts: tuple[SodPair, ...] = ()
    enforced: bool = True
    warning: bool = False


@dataclass(frozen=True, slots=True)
class RouteGuardDecision:
    allowed: bool
    route_class: RouteClass
    required_scope: str | None = None
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class PermissionCatalogue:
    """Immutable catalogue snapshot loaded from ``design/permissions.json``."""

    plan_version: str
    scopes: tuple[CapabilityScope, ...]
    role_templates: tuple[RoleTemplate, ...]
    sod_pairs: tuple[SodPair, ...]
    aliases: Mapping[str, str]
    public_allowlist: tuple[str, ...]
    self_service_allowlist: tuple[str, ...]
    service_scopes: tuple[str, ...]
    global_rules: tuple[str, ...]
    counts: Mapping[str, Any]
    scopes_by_name: Mapping[str, CapabilityScope] = field(default_factory=dict, compare=False)
    templates_by_key: Mapping[str, RoleTemplate] = field(default_factory=dict, compare=False)

    @classmethod
    def load(cls, path: Path | str | None = None) -> PermissionCatalogue:
        catalogue_path = Path(path) if path is not None else DEFAULT_CATALOGUE_PATH
        try:
            data = json.loads(catalogue_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:  # pragma: no cover - invalid deployment file
            raise ScopeCatalogueError(f"cannot load permission catalogue: {catalogue_path}") from exc
        catalogue = cls.from_data(data)
        catalogue.validate()
        return catalogue

    @classmethod
    def from_data(cls, data: Mapping[str, Any]) -> PermissionCatalogue:
        scopes = tuple(
            CapabilityScope(
                scope=item["scope"],
                domain=item["domain"],
                action=item["action"],
                requires_approval=bool(item.get("requires_approval", False)),
                platform_only=bool(item.get("platform_only", False)),
                service_only=bool(item.get("service_only", False)),
                assignable=bool(item.get("assignable", False)),
                owner_feature_set=str(item.get("owner_feature_set", "")),
            )
            for item in data.get("catalogue", [])
        )
        templates = tuple(
            RoleTemplate(
                key=item["key"],
                name=item.get("name", item["key"]),
                system=bool(item.get("system", True)),
                editable=bool(item.get("editable", False)),
                cloneable=bool(item.get("cloneable", True)),
                description=str(item.get("description", "")),
                scopes=frozenset(item.get("scopes", ())),
            )
            for item in data.get("role_templates", [])
        )
        sod_pairs = tuple(
            SodPair(
                a=item["a"],
                b=item["b"],
                enforcement=str(item.get("enforcement", "HARD")),
                reason=str(item.get("reason", "")),
            )
            for item in data.get("sod_pairs", [])
        )
        return cls(
            plan_version=str(data.get("plan_version", "unknown")),
            scopes=scopes,
            role_templates=templates,
            sod_pairs=sod_pairs,
            aliases={str(key): str(value) for key, value in data.get("aliases", {}).items()},
            public_allowlist=tuple(str(entry) for entry in data.get("public_allowlist", ())),
            self_service_allowlist=tuple(str(entry) for entry in data.get("self_service_allowlist", ())),
            service_scopes=tuple(str(entry) for entry in data.get("service_scopes", ())),
            global_rules=tuple(str(entry) for entry in data.get("global_rules", ())),
            counts=dict(data.get("counts", {})),
            scopes_by_name={scope.scope: scope for scope in scopes},
            templates_by_key={template.key: template for template in templates},
        )

    # -- lookups -----------------------------------------------------------------------------

    def scope(self, name: str) -> CapabilityScope | None:
        return self.scopes_by_name.get(name)

    def canonical(self, name: str) -> str:
        """Resolve the legacy alias to the seeded scope name; unknown scopes raise."""
        resolved = self.aliases.get(name, name)
        if resolved not in self.scopes_by_name:
            raise UnknownScope(f"unknown capability scope {name!r}", details={"scope": name})
        return resolved

    def template(self, role_key: str) -> RoleTemplate:
        try:
            return self.templates_by_key[role_key]
        except KeyError as exc:
            raise UnknownRole(f"unknown role template {role_key!r}", details={"role": role_key}) from exc

    def scope_class(self, name: str) -> ScopeClass:
        if name in self.aliases:
            return ScopeClass.ALIAS
        scope = self.scopes_by_name.get(name)
        return scope.scope_class if scope is not None else ScopeClass.UNKNOWN

    def assignable_scopes(self) -> frozenset[str]:
        return frozenset(scope.scope for scope in self.scopes if scope.assignable)

    def approval_scopes(self) -> frozenset[str]:
        return frozenset(scope.scope for scope in self.scopes if scope.requires_approval)

    def expand_template(self, role_key: str) -> frozenset[str]:
        return self.template(role_key).scopes

    # -- validation --------------------------------------------------------------------------

    def validate(self) -> None:
        """Catalogue integrity checks (CI also runs ``feature_sets.py permissions``)."""
        if not self.scopes:
            raise ScopeCatalogueError("catalogue contains no scopes")
        if len(self.scopes_by_name) != len(self.scopes):
            raise ScopeCatalogueError("duplicate capability scope names in catalogue")
        for template in self.role_templates:
            unknown = template.scopes - self.scopes_by_name.keys()
            if unknown:
                raise ScopeCatalogueError(f"role template {template.key!r} references unknown scopes: {sorted(unknown)}")
        if not self.role_templates:
            raise ScopeCatalogueError("catalogue contains no role templates")
        if len(self.sod_pairs) != len({pair.as_frozenset for pair in self.sod_pairs}):
            raise ScopeCatalogueError("duplicate SoD pairs in catalogue")
        for pair in self.sod_pairs:
            if pair.a not in self.scopes_by_name or pair.b not in self.scopes_by_name:
                raise ScopeCatalogueError(f"SoD pair references unknown scope: {pair.a}/{pair.b}")
        for alias, target in self.aliases.items():
            if alias in self.scopes_by_name:
                raise ScopeCatalogueError(f"legacy alias {alias!r} must not be seeded as a capability scope")
            if target not in self.scopes_by_name:
                raise ScopeCatalogueError(f"legacy alias {alias!r} points at unknown scope {target!r}")
        for entry in (*self.public_allowlist, *self.self_service_allowlist):
            if not entry or not entry.startswith("/") and " " not in entry:
                raise ScopeCatalogueError(f"invalid allowlist entry {entry!r}")
        service_scopes = set(self.service_scopes)
        if not service_scopes <= self.scopes_by_name.keys():
            raise ScopeCatalogueError("service_scopes contains unknown scopes")
        counts = self.counts
        advertised = counts.get("scopes")
        if isinstance(advertised, int) and advertised != len(self.scopes):
            raise ScopeCatalogueError(f"catalogue count mismatch: file says {advertised}, loaded {len(self.scopes)}")
        advertised_assignable = counts.get("assignable_scopes")
        if isinstance(advertised_assignable, int) and advertised_assignable != len(self.assignable_scopes()):
            raise ScopeCatalogueError("catalogue assignable count mismatch")
        non_admin = [template for template in self.role_templates if template.key != ADMIN_BREAK_GLASS_ROLE]
        for template in non_admin:
            for pair in self.sod_pairs:
                if pair.conflicts_with(template.scopes):
                    raise ScopeCatalogueError(f"non-admin template {template.key!r} contains SoD pair {pair.a}/{pair.b}")


@lru_cache(maxsize=4)
def _load_catalogue_cached(path_text: str) -> PermissionCatalogue:
    return PermissionCatalogue.load(Path(path_text))


def load_catalogue(path: Path | str | None = None) -> PermissionCatalogue:
    """Load (and cache) the authoritative catalogue; read-only, no network/DB."""
    catalogue_path = Path(path) if path is not None else DEFAULT_CATALOGUE_PATH
    return _load_catalogue_cached(str(catalogue_path.resolve()))


# --------------------------------------------------------------------------- route guard --------


def _split_allowlist_entry(entry: str) -> tuple[str | None, str]:
    if " " in entry and not entry.startswith("/"):
        method, path = entry.split(" ", 1)
        return method.upper().strip(), path.strip()
    return None, entry


def _path_matches(template: str, path: str) -> bool:
    template_segments = [segment for segment in template.strip("/").split("/") if segment]
    path_segments = [segment for segment in path.strip("/").split("/") if segment]
    if len(template_segments) != len(path_segments):
        return False
    for expected, actual in zip(template_segments, path_segments, strict=False):
        if expected.startswith("{") and expected.endswith("}"):
            if not actual:
                return False
            continue
        if expected != actual:
            return False
    return True


def normalize_route(method: str, path: str) -> tuple[str, str]:
    """Normalise an HTTP route for allowlist matching (uppercase method, single slashes, no query)."""
    normalised_method = method.upper().strip()
    if not normalised_method:
        raise ValueError("method is required")
    raw_path = path.split("?", 1)[0].strip()
    normalised_path = "/" + "/".join(segment for segment in raw_path.split("/") if segment)
    return normalised_method, normalised_path


#: Operator-only bootstrap operations. They carry no tenant capability scope (they run before any
#: tenant admin exists) and are protected by the SUPER_ADMIN_TOKEN header at the API layer. They are
#: classified as allowlisted so the deny-by-default route-scope gate can distinguish them from
#: accidentally unscoped business routes.
OPERATOR_BOOTSTRAP_ALLOWLIST: tuple[tuple[str, str], ...] = (
    ("GET", "/api/v1/bootstrap/whoami"),
    ("POST", "/api/v1/bootstrap/tenants"),
    ("POST", "/api/v1/bootstrap/users"),
)


class ScopeRegistry:
    """Authorization evaluation over one catalogue snapshot (pure + deterministic)."""

    def __init__(
        self,
        catalogue: PermissionCatalogue | None = None,
        *,
        sod_mode: str = "HARD",
    ) -> None:
        self.catalogue = catalogue or load_catalogue()
        mode = sod_mode.upper()
        if mode not in {"HARD", "WARN"}:
            raise ValueError("sod_mode must be HARD or WARN")
        self.sod_mode = mode

    # -- role expansion / effective permissions ---------------------------------------------

    def expand_roles(
        self,
        role_keys: Iterable[str],
        *,
        custom_roles: Mapping[str, Iterable[str]] | None = None,
    ) -> frozenset[str]:
        """Union of capability scopes for a set of role keys (templates and/or custom roles)."""
        resolved: set[str] = set()
        for role_key in role_keys:
            if role_key in self.catalogue.templates_by_key:
                scopes = self.catalogue.templates_by_key[role_key].scopes
            elif custom_roles is not None and role_key in custom_roles:
                scopes = frozenset(self.catalogue.canonical(scope) for scope in custom_roles[role_key])
                self.require_assignable(scopes)
            else:
                raise UnknownRole(f"unknown role {role_key!r}", details={"role": role_key})
            resolved.update(scopes)
        return frozenset(resolved)

    def effective_permissions(
        self,
        role_keys: Iterable[str] = (),
        *,
        api_key_scopes: Iterable[str] = (),
        custom_roles: Mapping[str, Iterable[str]] | None = None,
        data_scopes: Mapping[str, Sequence[str]] | None = None,
    ) -> EffectivePermissions:
        roles = tuple(dict.fromkeys(role_keys))
        scopes = set(self.expand_roles(roles, custom_roles=custom_roles)) if roles else set()
        for scope in api_key_scopes:
            scopes.add(self.catalogue.canonical(scope))
        normalised_data_scopes = {
            str(scope_type).upper(): tuple(dict.fromkeys(str(value) for value in values))
            for scope_type, values in (data_scopes or {}).items()
        }
        return EffectivePermissions(scopes=frozenset(scopes), roles=roles, data_scopes=normalised_data_scopes)

    # -- scope checks ------------------------------------------------------------------------

    def require_scope(self, permissions: EffectivePermissions, required_scope: str) -> None:
        scope = self.catalogue.canonical(required_scope)
        permissions.require(scope)

    def require_any_scope(self, permissions: EffectivePermissions, *required_scopes: str) -> None:
        if not any(self.catalogue.canonical(scope) in permissions.scopes for scope in required_scopes):
            raise ForbiddenScope(
                "missing one of the required capability scopes",
                details={"required_scopes": list(required_scopes)},
            )

    def require_role_admin(self, permissions: EffectivePermissions) -> None:
        permissions.require(ROLE_ADMIN_SCOPE)

    def require_user_admin(self, permissions: EffectivePermissions) -> None:
        permissions.require(USER_ADMIN_SCOPE)

    def scope_class(self, scope: str) -> ScopeClass:
        return self.catalogue.scope_class(scope)

    def is_assignable(self, scope: str) -> bool:
        try:
            canonical = self.catalogue.canonical(scope)
        except UnknownScope:
            return False
        entry = self.catalogue.scopes_by_name[canonical]
        return entry.assignable and not entry.platform_only and not entry.service_only

    def require_assignable(self, scopes: Iterable[str]) -> frozenset[str]:
        resolved: set[str] = set()
        for scope in scopes:
            canonical = self.catalogue.canonical(scope)
            entry = self.catalogue.scopes_by_name[canonical]
            if entry.platform_only:
                raise ScopeNotAssignable(
                    f"scope {canonical!r} is platform-only and cannot be granted to a tenant role",
                    details={"scope": canonical, "reason": "PLATFORM_ONLY"},
                )
            if entry.service_only:
                raise ScopeNotAssignable(
                    f"scope {canonical!r} is service-only and cannot be granted to a tenant role",
                    details={"scope": canonical, "reason": "SERVICE_ONLY"},
                )
            if not entry.assignable:
                raise ScopeNotAssignable(
                    f"scope {canonical!r} is not assignable",
                    details={"scope": canonical, "reason": "NOT_ASSIGNABLE"},
                )
            resolved.add(canonical)
        return frozenset(resolved)

    def require_template_mutable(self, role_key: str) -> None:
        template = self.catalogue.template(role_key)
        if template.system or not template.editable:
            raise TemplateImmutable(
                f"system role template {role_key!r} is clone-only (create a custom role)",
                details={"role": role_key},
            )

    # -- SoD ---------------------------------------------------------------------------------

    def sod_conflicts(self, scopes: Iterable[str]) -> tuple[SodPair, ...]:
        resolved = frozenset(self.catalogue.canonical(scope) for scope in scopes)
        return tuple(pair for pair in self.catalogue.sod_pairs if pair.conflicts_with(resolved))

    def evaluate_sod(
        self,
        scopes: Iterable[str],
        *,
        context: SodContext = SodContext.ROLE_UPDATE,
        role_keys: Iterable[str] = (),
        allow_break_glass: bool = False,
    ) -> SodEvaluation:
        conflicts = self.sod_conflicts(scopes)
        if not conflicts:
            return SodEvaluation(ok=True)
        break_glass = allow_break_glass or ADMIN_BREAK_GLASS_ROLE in set(role_keys)
        if break_glass:
            # Admin template is the only role permitted to contain SoD pairs (audited by callers).
            return SodEvaluation(ok=True, conflicts=conflicts, enforced=False)
        warn_only = self.sod_mode == "WARN" and context is SodContext.CUSTOM_ROLE_CREATION
        return SodEvaluation(ok=warn_only, conflicts=conflicts, enforced=not warn_only, warning=warn_only)

    def require_no_sod_conflict(
        self,
        scopes: Iterable[str],
        *,
        context: SodContext = SodContext.ROLE_UPDATE,
        role_keys: Iterable[str] = (),
        allow_break_glass: bool = False,
    ) -> SodEvaluation:
        evaluation = self.evaluate_sod(
            scopes,
            context=context,
            role_keys=role_keys,
            allow_break_glass=allow_break_glass,
        )
        if not evaluation.ok:
            pair_list = [{"a": pair.a, "b": pair.b, "reason": pair.reason} for pair in evaluation.conflicts]
            raise SodConflict("separation-of-duties conflict", details={"pairs": pair_list})
        return evaluation

    # -- safety guards -----------------------------------------------------------------------

    def require_can_grant(
        self,
        actor: EffectivePermissions,
        requested_scopes: Iterable[str],
        *,
        is_platform_admin: bool = False,
    ) -> None:
        """Privilege-escalation guard: never grant a capability the actor does not hold."""
        if is_platform_admin:
            return
        requested = {self.catalogue.canonical(scope) for scope in requested_scopes}
        missing = sorted(requested - actor.scopes)
        if missing:
            raise PrivilegeEscalation(
                "actor cannot grant scopes they do not hold",
                details={"missing_scopes": missing},
            )

    def require_can_manage_roles(self, actor: EffectivePermissions, *, is_platform_admin: bool = False) -> None:
        if is_platform_admin:
            return
        actor.require(ROLE_ADMIN_SCOPE)

    def require_can_assign_roles(self, actor: EffectivePermissions, *, is_platform_admin: bool = False) -> None:
        if is_platform_admin:
            return
        actor.require(USER_ADMIN_SCOPE)

    @staticmethod
    def require_not_last_admin(
        *,
        was_active_admin: bool,
        will_remain_active_admin: bool,
        active_admin_count: int,
        action: str = "change",
    ) -> None:
        """Lockout guard: a tenant must always keep >= 1 active administrator (RULE-RBAC-02)."""
        if active_admin_count < 1:
            raise LastAdminLockout("tenant has no active administrator")
        if was_active_admin and not will_remain_active_admin and active_admin_count <= 1:
            raise LastAdminLockout(
                "cannot remove or deactivate the last active tenant administrator",
                details={"action": action},
            )

    # -- route guard (deny by default) ------------------------------------------------------

    def is_public_route(self, method: str, path: str) -> bool:
        normalised_method, normalised_path = normalize_route(method, path)
        if (normalised_method, normalised_path) in OPERATOR_BOOTSTRAP_ALLOWLIST:
            return True
        for entry in self.catalogue.public_allowlist:
            entry_method, entry_path = _split_allowlist_entry(entry)
            if entry_method is not None and entry_method != normalised_method:
                continue
            if _path_matches(entry_path, normalised_path):
                return True
        return False

    def is_self_service_route(self, method: str, path: str) -> bool:
        normalised_method, normalised_path = normalize_route(method, path)
        for entry in self.catalogue.self_service_allowlist:
            entry_method, entry_path = _split_allowlist_entry(entry)
            if entry_method is not None and entry_method != normalised_method:
                continue
            if _path_matches(entry_path, normalised_path):
                return True
        return False

    def classify_route(self, method: str, path: str) -> RouteClass:
        if self.is_public_route(method, path):
            return RouteClass.PUBLIC
        if self.is_self_service_route(method, path):
            return RouteClass.SELF_SERVICE
        return RouteClass.SCOPED

    def guard_route(
        self,
        method: str,
        path: str,
        required_scope: str | None,
        permissions: EffectivePermissions | None,
    ) -> RouteGuardDecision:
        """Pure deny-by-default evaluation: exactly one catalogue scope for every non-allowlisted route."""
        route_class = self.classify_route(method, path)
        if route_class is RouteClass.PUBLIC:
            return RouteGuardDecision(allowed=True, route_class=route_class, required_scope=None)
        if route_class is RouteClass.SELF_SERVICE:
            if permissions is None:
                return RouteGuardDecision(allowed=False, route_class=route_class, reason="AUTHENTICATION_REQUIRED")
            return RouteGuardDecision(allowed=True, route_class=route_class, required_scope=None)
        if not required_scope:
            return RouteGuardDecision(
                allowed=False,
                route_class=route_class,
                reason="MISSING_SCOPE_DECLARATION",
            )
        try:
            canonical = self.catalogue.canonical(required_scope)
        except UnknownScope:
            return RouteGuardDecision(
                allowed=False,
                route_class=route_class,
                required_scope=required_scope,
                reason="UNKNOWN_SCOPE",
            )
        if permissions is None:
            return RouteGuardDecision(
                allowed=False,
                route_class=route_class,
                required_scope=canonical,
                reason="AUTHENTICATION_REQUIRED",
            )
        return RouteGuardDecision(
            allowed=canonical in permissions.scopes,
            route_class=route_class,
            required_scope=canonical,
            reason=None if canonical in permissions.scopes else "FORBIDDEN_SCOPE",
        )

    def enforce_route(
        self,
        method: str,
        path: str,
        required_scope: str | None,
        permissions: EffectivePermissions | None,
    ) -> RouteGuardDecision:
        """Like :meth:`guard_route` but raises typed errors for denials (API middleware adapter)."""
        decision = self.guard_route(method, path, required_scope, permissions)
        if decision.allowed:
            return decision
        if decision.reason == "AUTHENTICATION_REQUIRED":
            from app.core.security.errors import AuthenticationError

            raise AuthenticationError("authentication is required")
        if decision.reason in {"MISSING_SCOPE_DECLARATION", "UNKNOWN_SCOPE"}:
            raise SecurityError(
                f"route scope configuration is invalid: {decision.reason}",
                code="SCOPE_CATALOGUE_INVALID",
                details={"path": path, "method": method, "configured_scope": required_scope},
            )
        raise ForbiddenScope(
            f"missing required capability scope {decision.required_scope!r}",
            details={"required_scope": decision.required_scope},
        )

    # -- convenience -------------------------------------------------------------------------

    @property
    def counts(self) -> Mapping[str, Any]:
        return self.catalogue.counts

    def describe(self) -> dict[str, Any]:
        """Non-secret description for diagnostics (``/internal/deps``)."""
        return {
            "plan_version": self.catalogue.plan_version,
            "scopes": len(self.catalogue.scopes),
            "assignable": len(self.catalogue.assignable_scopes()),
            "service_only": sum(1 for scope in self.catalogue.scopes if scope.service_only),
            "platform_only": sum(1 for scope in self.catalogue.scopes if scope.platform_only),
            "role_templates": len(self.catalogue.role_templates),
            "sod_pairs": len(self.catalogue.sod_pairs),
            "approval_scopes": len(self.catalogue.approval_scopes()),
            "public_allowlist": len(self.catalogue.public_allowlist),
            "self_service_allowlist": len(self.catalogue.self_service_allowlist),
            "sod_mode": self.sod_mode,
        }


__all__ = [
    "ADMIN_BREAK_GLASS_ROLE",
    "DEFAULT_CATALOGUE_PATH",
    "CapabilityScope",
    "EffectivePermissions",
    "PermissionCatalogue",
    "ROLE_ADMIN_SCOPE",
    "RoleTemplate",
    "RouteClass",
    "RouteGuardDecision",
    "ScopeClass",
    "ScopeRegistry",
    "SodContext",
    "SodEvaluation",
    "SodPair",
    "USER_ADMIN_SCOPE",
    "load_catalogue",
    "normalize_route",
]
