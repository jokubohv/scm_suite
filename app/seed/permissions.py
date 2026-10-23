"""Authoritative RBAC catalogue loading, validation and normalisation.

Single source of truth: ``design/permissions.json`` (Δ-RBAC, plan v1.5.0). The seed loader converts
the catalogue into rows for ``platform.permission_scope``, ``platform.role``, ``platform.role_permission``
and the reference-only SoD policy. Validation is strict: a drift from the documented counts or an
assignability/SoD violation fails the seed run instead of silently seeding a broken catalogue.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.seed.constants import (
    DEFAULT_PERMISSIONS_SOURCE,
    DOCUMENTED_ROLE_SIZES,
    EXPECTED_CATALOGUE_COUNTS,
    FIXTURE_PERMISSIONS_SOURCE,
)


class SeedValidationError(ValueError):
    """Raised when seed source data violates its own invariants."""


@dataclass(frozen=True)
class PermissionScopeRow:
    scope: str
    domain: str
    action: str
    requires_approval: bool
    platform_only: bool
    service_only: bool
    assignable: bool
    owner_feature_set: str

    def to_row(self) -> dict[str, Any]:
        return {
            "scope": self.scope,
            "domain": self.domain,
            "action": self.action,
            "requires_approval": self.requires_approval,
            "platform_only": self.platform_only,
            "service_only": self.service_only,
            "assignable": self.assignable,
            "owner_feature_set": self.owner_feature_set,
        }


@dataclass(frozen=True)
class RoleTemplateRow:
    code: str
    name: str
    system: bool
    editable: bool
    cloneable: bool
    description: str
    scopes: tuple[str, ...]

    def to_row(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "name": self.name,
            "is_system": self.system,
            "is_editable": self.editable,
            "is_cloneable": self.cloneable,
            "description": self.description,
            "scope_count": len(self.scopes),
        }


@dataclass(frozen=True)
class SodPairRow:
    a: str
    b: str
    enforcement: str
    reason: str

    def to_row(self) -> dict[str, Any]:
        return {"a": self.a, "b": self.b, "enforcement": self.enforcement, "reason": self.reason}


@dataclass
class PermissionCatalogue:
    """Validated, normalised view of ``design/permissions.json``."""

    source: Path
    schema_version: str
    plan_version: str
    permissions: list[PermissionScopeRow]
    roles: list[RoleTemplateRow]
    sod_pairs: list[SodPairRow]
    aliases: dict[str, str] = field(default_factory=dict)
    global_rules: list[str] = field(default_factory=list)
    public_allowlist: list[str] = field(default_factory=list)
    self_service_allowlist: list[str] = field(default_factory=list)
    service_scopes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    # ---- derived views --------------------------------------------------------
    @property
    def permission_by_scope(self) -> dict[str, PermissionScopeRow]:
        return {row.scope: row for row in self.permissions}

    @property
    def assignable_scopes(self) -> frozenset[str]:
        return frozenset(row.scope for row in self.permissions if row.assignable)

    @property
    def service_only_scopes(self) -> frozenset[str]:
        return frozenset(row.scope for row in self.permissions if row.service_only)

    @property
    def platform_only_scopes(self) -> frozenset[str]:
        return frozenset(row.scope for row in self.permissions if row.platform_only)

    @property
    def approval_scopes(self) -> frozenset[str]:
        return frozenset(row.scope for row in self.permissions if row.requires_approval)

    def role(self, code: str) -> RoleTemplateRow:
        for role in self.roles:
            if role.code == code:
                return role
        raise KeyError(code)

    # ---- seed rows ------------------------------------------------------------
    def permission_rows(self) -> list[dict[str, Any]]:
        return [row.to_row() for row in sorted(self.permissions, key=lambda row: row.scope)]

    def role_rows(self) -> list[dict[str, Any]]:
        return [row.to_row() for row in sorted(self.roles, key=lambda row: row.code)]

    def role_permission_rows(self) -> list[dict[str, Any]]:
        rows = [
            {"role_code": role.code, "scope": scope}
            for role in self.roles
            for scope in sorted(role.scopes)
        ]
        return sorted(rows, key=lambda row: (row["role_code"], row["scope"]))

    def sod_rows(self) -> list[dict[str, Any]]:
        return [row.to_row() for row in sorted(self.sod_pairs, key=lambda row: (row.a, row.b))]

    def counts(self) -> dict[str, int]:
        return {
            "scopes": len(self.permissions),
            "assignable_scopes": len(self.assignable_scopes),
            "platform_only": len(self.platform_only_scopes),
            "service_only": len(self.service_only_scopes),
            "requires_approval": len(self.approval_scopes),
            "aliases": len(self.aliases),
            "roles": len(self.roles),
            "sod_pairs": len(self.sod_pairs),
        }


def resolve_permissions_source(source: str | Path | None = None) -> Path:
    """Resolve the catalogue path: explicit > design/ > packaged fixture copy."""
    candidates = [Path(source)] if source is not None else [DEFAULT_PERMISSIONS_SOURCE, FIXTURE_PERMISSIONS_SOURCE]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise SeedValidationError(
        "permissions catalogue not found; looked for: " + ", ".join(str(path) for path in candidates)
    )


def load_permissions(source: str | Path | None = None) -> PermissionCatalogue:
    """Load and validate the authoritative RBAC catalogue."""
    path = resolve_permissions_source(source)
    raw = json.loads(path.read_text(encoding="utf-8"))
    errors, warnings = validate_permissions_payload(raw)
    if errors:
        raise SeedValidationError(f"invalid permissions catalogue {path}: " + "; ".join(errors))
    return _normalise(path, raw, warnings)


def validate_permissions_payload(raw: dict[str, Any]) -> tuple[list[str], list[str]]:
    """Validate the raw permissions JSON; returns ``(errors, warnings)``."""
    errors: list[str] = []
    warnings: list[str] = []

    for key in ("catalogue", "role_templates", "sod_pairs", "aliases", "global_rules"):
        if key not in raw:
            errors.append(f"missing top-level key {key!r}")
    if errors:
        return errors, warnings

    declared = raw.get("counts", {})
    for key, expected in EXPECTED_CATALOGUE_COUNTS.items():
        if key in declared and declared[key] != expected:
            errors.append(f"declared counts.{key}={declared[key]} but S0-07 expects {expected}")

    catalogue = raw["catalogue"]
    if not isinstance(catalogue, list) or not catalogue:
        return errors + ["catalogue must be a non-empty list"], warnings
    scopes: list[str] = []
    for index, row in enumerate(catalogue):
        if not isinstance(row, dict) or not row.get("scope"):
            errors.append(f"catalogue[{index}] missing scope")
            continue
        scopes.append(row["scope"])
    duplicates = sorted({scope for scope in scopes if scopes.count(scope) > 1})
    if duplicates:
        errors.append(f"duplicate catalogue scopes: {duplicates[:10]}")
    if len(scopes) != EXPECTED_CATALOGUE_COUNTS["scopes"]:
        errors.append(f"catalogue has {len(scopes)} scopes, expected {EXPECTED_CATALOGUE_COUNTS['scopes']}")

    by_scope = {row["scope"]: row for row in catalogue if isinstance(row, dict) and row.get("scope")}
    assignable = {scope for scope, row in by_scope.items() if row.get("assignable")}
    if any(row.get("platform_only") and row.get("assignable") for row in by_scope.values()):
        errors.append("platform-only scopes must not be assignable")
    if any(row.get("service_only") and row.get("assignable") for row in by_scope.values()):
        errors.append("service-only scopes must not be assignable")

    roles = raw["role_templates"]
    if len(roles) != EXPECTED_CATALOGUE_COUNTS["roles"]:
        errors.append(f"role_templates has {len(roles)} roles, expected {EXPECTED_CATALOGUE_COUNTS['roles']}")
    role_codes = [role.get("key") for role in roles if isinstance(role, dict)]
    if len(set(role_codes)) != len(role_codes):
        errors.append(f"duplicate role template keys: {role_codes}")
    for role in roles:
        if not isinstance(role, dict) or not role.get("key"):
            errors.append("role template missing key")
            continue
        role_scopes = set(role.get("scopes") or [])
        unknown = sorted(role_scopes - set(by_scope))
        if unknown:
            errors.append(f"role {role['key']!r} references unknown scopes: {unknown[:10]}")
        not_assignable = sorted(role_scopes - assignable)
        if not_assignable:
            errors.append(f"role {role['key']!r} contains non-assignable scopes: {not_assignable[:10]}")
        documented = DOCUMENTED_ROLE_SIZES.get(role["key"])
        if documented is not None and len(role_scopes) != documented:
            warnings.append(
                f"role {role['key']!r} has {len(role_scopes)} scopes; plan documents {documented} — catalogue wins"
            )
    role_by_code = {role["key"]: set(role.get("scopes") or []) for role in roles if isinstance(role, dict)}
    admin = role_by_code.get("admin")
    if admin is not None and admin != assignable:
        errors.append("admin role template must contain exactly all assignable scopes")
    for role_code, role_scopes in role_by_code.items():
        if role_code == "admin":
            continue  # break-glass template is exempt from SoD coexistence checks
        for pair in raw["sod_pairs"]:
            a, b = pair.get("a"), pair.get("b")
            if a in role_scopes and b in role_scopes:
                errors.append(f"role {role_code!r} contains SoD-conflicting scopes {a!r} and {b!r}")

    aliases = raw.get("aliases") or {}
    for legacy, target in aliases.items():
        if legacy in by_scope:
            warnings.append(f"alias {legacy!r} should not be in the catalogue")
        if target not in by_scope:
            errors.append(f"alias {legacy!r} points to unknown scope {target!r}")

    sod_pairs = raw["sod_pairs"]
    if len(sod_pairs) != EXPECTED_CATALOGUE_COUNTS["sod_pairs"]:
        errors.append(f"sod_pairs has {len(sod_pairs)}, expected {EXPECTED_CATALOGUE_COUNTS['sod_pairs']}")
    seen_pairs: set[tuple[str, str]] = set()
    for pair in sod_pairs:
        a, b = pair.get("a"), pair.get("b")
        if not a or not b:
            errors.append("SoD pair missing a/b scope")
            continue
        if a not in by_scope or b not in by_scope:
            errors.append(f"SoD pair references unknown scope: {(a, b)!r}")
        pair_key: tuple[str, str] = (a, b) if a <= b else (b, a)
        if pair_key in seen_pairs:
            errors.append(f"duplicate SoD pair {pair_key!r}")
        seen_pairs.add(pair_key)
        if pair.get("enforcement") not in {"HARD", "WARN"}:
            errors.append(f"SoD pair {(a, b)!r} has invalid enforcement {pair.get('enforcement')!r}")

    return errors, warnings


def _normalise(path: Path, raw: dict[str, Any], warnings: list[str]) -> PermissionCatalogue:
    permissions = [
        PermissionScopeRow(
            scope=row["scope"],
            domain=row.get("domain", row["scope"].split(".")[0]),
            action=row.get("action", row["scope"].split(".")[-1]),
            requires_approval=bool(row.get("requires_approval", False)),
            platform_only=bool(row.get("platform_only", False)),
            service_only=bool(row.get("service_only", False)),
            assignable=bool(row.get("assignable", False)),
            owner_feature_set=row.get("owner_feature_set", "FS-00"),
        )
        for row in raw["catalogue"]
    ]
    roles = [
        RoleTemplateRow(
            code=role["key"],
            name=role.get("name", role["key"]),
            system=bool(role.get("system", True)),
            editable=bool(role.get("editable", False)),
            cloneable=bool(role.get("cloneable", True)),
            description=role.get("description", ""),
            scopes=tuple(sorted(role.get("scopes") or [])),
        )
        for role in raw["role_templates"]
    ]
    sod_pairs = [
        SodPairRow(
            a=pair["a"],
            b=pair["b"],
            enforcement=pair.get("enforcement", "HARD"),
            reason=pair.get("reason", ""),
        )
        for pair in raw["sod_pairs"]
    ]
    return PermissionCatalogue(
        source=path,
        schema_version=str(raw.get("schema_version", "")),
        plan_version=str(raw.get("plan_version", "")),
        permissions=sorted(permissions, key=lambda row: row.scope),
        roles=sorted(roles, key=lambda row: row.code),
        sod_pairs=sorted(sod_pairs, key=lambda row: (row.a, row.b)),
        aliases=dict(raw.get("aliases") or {}),
        global_rules=list(raw.get("global_rules") or []),
        public_allowlist=list(raw.get("public_allowlist") or []),
        self_service_allowlist=list(raw.get("self_service_allowlist") or []),
        service_scopes=list(raw.get("service_scopes") or []),
        warnings=warnings,
    )
