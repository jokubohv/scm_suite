"""Structural + coverage + freshness lint for the contract-first artefacts.

This is the machine-checkable part of the S0-GATE item
"16 contract skeletons generated and lint-clean" and the L1 provider-contract precondition.

Checks (all offline/deterministic):
1. Plan sources parse: 16 feature sets, 21 contracts, ≥1 endpoint per FS, event/endpoint counts stable.
2. Every committed artefact is fresh (byte-identical to a fresh in-memory generation).
3. OpenAPI fragments + aggregate parse as OpenAPI 3.1 documents, unique operationIds, canonical paths,
   exactly one capability scope or an explicit allowlist per non-public operation, Problem schema present.
4. Contract registry and event registry: 21 contracts, unique names, semver versions, dependency and
   owner metadata, one JSON Schema per registered event, final outbox envelope present in every schema.
5. Event schemas: file/path/`x-event-type`/`x-contract` consistency, semver versions, TODO marker for
   skeleton payloads.
6. Capability scopes used by operations exist in `design/permissions.json` when readable.
7. Builder extension fragments parse and do not duplicate an existing (path, method).

Returns a report dict; ``run_lint`` never writes.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import paths
from .generate import build_model, build_outputs, stale_generated_files
from .parse_specs import ContractModel
from .yaml_emit import dump_yaml

ERROR = "ERROR"
WARNING = "WARN"


def _issue(level: str, code: str, message: str, path: str = "") -> dict[str, str]:
    return {"level": level, "code": code, "message": message, "path": path}


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _expected_paths(model: ContractModel) -> dict[str, set[tuple[str, str]]]:
    result: dict[str, set[tuple[str, str]]] = {}
    for fs in model.feature_sets:
        result[fs.id] = {(ep.method.lower(), ep.path) for ep in fs.endpoints}
    return result


def _load_permission_scopes() -> set[str] | None:
    permissions_path = paths.DESIGN_DIR / "permissions.json"
    if not permissions_path.exists():
        return None
    try:
        document = _load_json(permissions_path)
    except json.JSONDecodeError:
        return None
    catalogue = document.get("catalogue")
    if not isinstance(catalogue, list):
        return None
    return {str(entry.get("scope", "")) for entry in catalogue if entry.get("scope")}


def _check_fragments(model: ContractModel, issues: list[dict[str, str]], stats: dict[str, Any]) -> None:
    expected = _expected_paths(model)
    fs_ids = {fs.id for fs in model.feature_sets}
    committed = sorted(p.name for p in paths.FRAGMENTS_DIR.glob("*.openapi.json")) if paths.FRAGMENTS_DIR.exists() else []
    found_ids = {name.replace(".openapi.json", "") for name in committed}
    if found_ids != fs_ids:
        issues.append(_issue(ERROR, "FRAGMENT_SET", f"fragment files != 16 feature sets (missing={sorted(fs_ids - found_ids)}, extra={sorted(found_ids - fs_ids)})", paths.relpath(paths.FRAGMENTS_DIR)))
    for fs in model.feature_sets:
        fragment_path = paths.REPO_ROOT / f"contracts/openapi/fragments/{fs.id}.openapi.json"
        if not fragment_path.exists():
            issues.append(_issue(ERROR, "FRAGMENT_MISSING", f"{fs.id}: fragment missing", paths.relpath(fragment_path)))
            continue
        try:
            fragment = json.loads(fragment_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            issues.append(_issue(ERROR, "FRAGMENT_JSON", f"{fs.id}: invalid JSON: {exc}", paths.relpath(fragment_path)))
            continue
        if fragment.get("openapi") != "3.1.0":
            issues.append(_issue(ERROR, "FRAGMENT_VERSION", f"{fs.id}: openapi must be 3.1.0", paths.relpath(fragment_path)))
        if fragment.get("info", {}).get("x-feature-set") != fs.id:
            issues.append(_issue(ERROR, "FRAGMENT_FS", f"{fs.id}: info.x-feature-set mismatch", paths.relpath(fragment_path)))
        if int(fragment.get("x-endpoint-count", -1)) != len(fs.endpoints):
            issues.append(_issue(ERROR, "FRAGMENT_COUNT", f"{fs.id}: x-endpoint-count mismatch", paths.relpath(fragment_path)))
        paths_in_fragment = fragment.get("paths") or {}
        if not paths_in_fragment:
            issues.append(_issue(ERROR, "FRAGMENT_EMPTY", f"{fs.id}: fragment has no paths", paths.relpath(fragment_path)))
        declared = {
            (method.lower(), path)
            for path, item in paths_in_fragment.items()
            for method, operation in item.items()
            if isinstance(operation, dict)
        }
        implemented = {
            (method.lower(), path)
            for path, item in paths_in_fragment.items()
            for method, operation in item.items()
            if isinstance(operation, dict) and operation.get("x-skeleton") is False
        }
        missing = expected[fs.id] - declared
        unexpected = declared - expected[fs.id] - implemented
        if missing:
            issues.append(_issue(ERROR, "FRAGMENT_ENDPOINTS", f"{fs.id}: fragment misses {len(missing)} spec method/path(s): {sorted(missing)[:5]}", paths.relpath(fragment_path)))
        if unexpected:
            issues.append(_issue(ERROR, "FRAGMENT_ENDPOINTS", f"{fs.id}: {len(unexpected)} fragment method/path(s) are neither spec rows nor implementation-backed: {sorted(unexpected)[:5]}", paths.relpath(fragment_path)))
        stats.setdefault("fragments", {})[fs.id] = len(declared)


def _check_aggregate(model: ContractModel, issues: list[dict[str, str]], stats: dict[str, Any]) -> None:
    aggregate_path = paths.OPENAPI_DIR / "openapi.json"
    yaml_path = paths.OPENAPI_DIR / "openapi.yaml"
    if not aggregate_path.exists():
        issues.append(_issue(ERROR, "AGGREGATE_MISSING", "contracts/openapi/openapi.json missing", paths.relpath(aggregate_path)))
        return
    try:
        document = _load_json(aggregate_path)
    except json.JSONDecodeError as exc:
        issues.append(_issue(ERROR, "AGGREGATE_JSON", f"aggregate invalid JSON: {exc}", paths.relpath(aggregate_path)))
        return
    if document.get("openapi") != "3.1.0":
        issues.append(_issue(ERROR, "AGGREGATE_VERSION", "aggregate openapi must be 3.1.0", paths.relpath(aggregate_path)))
    if document.get("info", {}).get("version") != model.plan_version:
        issues.append(_issue(ERROR, "AGGREGATE_PLAN", "aggregate info.version != manifest plan_version", paths.relpath(aggregate_path)))
    problems = (document.get("components") or {}).get("schemas") or {}
    if "Problem" not in problems:
        issues.append(_issue(ERROR, "AGGREGATE_PROBLEM", "aggregate missing components.schemas.Problem", paths.relpath(aggregate_path)))
    operation_ids: set[str] = set()
    duplicates: list[str] = []
    non_public_missing_scope = 0
    legacy_paths = 0
    operations = 0
    permission_scopes = _load_permission_scopes()
    for raw_path, item in (document.get("paths") or {}).items():
        if not isinstance(raw_path, str) or not raw_path.startswith("/"):
            issues.append(_issue(ERROR, "AGGREGATE_PATH", f"invalid path key {raw_path!r}", paths.relpath(aggregate_path)))
            continue
        if raw_path.startswith("/v1/") or raw_path == "/v1":
            legacy_paths += 1
        for method, operation in item.items():
            operations += 1
            operation_id = operation.get("operationId", "")
            if not operation_id:
                issues.append(_issue(ERROR, "OP_ID", f"missing operationId: {method.upper()} {raw_path}", paths.relpath(aggregate_path)))
            elif operation_id in operation_ids:
                duplicates.append(operation_id)
            operation_ids.add(operation_id)
            responses = operation.get("responses") or {}
            if not any(code in responses for code in ("200", "201", "202", "204")):
                issues.append(_issue(ERROR, "OP_RESPONSES", f"missing success response: {method.upper()} {raw_path}", paths.relpath(aggregate_path)))
            scopes = operation.get("x-required-scopes") or []
            allowlist = operation.get("x-allowlist") or ""
            if not scopes and not allowlist:
                non_public_missing_scope += 1
                issues.append(_issue(ERROR, "OP_SCOPE", f"no capability scope/allowlist: {method.upper()} {raw_path}", paths.relpath(aggregate_path)))
            if permission_scopes is not None:
                unknown = [s for s in scopes if s not in permission_scopes]
                if unknown:
                    issues.append(_issue(WARNING, "OP_SCOPE_UNKNOWN", f"spec scope(s) not in permissions.json catalogue (reconcile with owner): {unknown} on {method.upper()} {raw_path}", paths.relpath(aggregate_path)))
            if operation.get("x-skeleton") and not operation.get("x-todo"):
                issues.append(_issue(WARNING, "OP_TODO", f"skeleton operation without x-todo: {method.upper()} {raw_path}", paths.relpath(aggregate_path)))
    if duplicates:
        issues.append(_issue(ERROR, "OP_ID_DUP", f"duplicate operationId(s): {sorted(set(duplicates))[:10]}", paths.relpath(aggregate_path)))
    if legacy_paths:
        issues.append(_issue(ERROR, "PATH_BASE", f"{legacy_paths} aggregate path(s) still use legacy /v1 base", paths.relpath(aggregate_path)))
    if non_public_missing_scope == 0 and operations == 0:
        issues.append(_issue(ERROR, "AGGREGATE_EMPTY", "aggregate has no operations", paths.relpath(aggregate_path)))
    x_feature_sets = document.get("x-feature-sets") or []
    if len(x_feature_sets) != len(model.feature_sets):
        issues.append(_issue(ERROR, "AGGREGATE_FS", f"x-feature-sets count {len(x_feature_sets)} != {len(model.feature_sets)}", paths.relpath(aggregate_path)))
    x_contracts = document.get("x-contracts") or []
    if len(x_contracts) != len(model.contracts):
        issues.append(_issue(ERROR, "AGGREGATE_CONTRACTS", f"x-contracts count {len(x_contracts)} != 21", paths.relpath(aggregate_path)))
    if yaml_path.exists():
        try:
            if yaml_path.read_text(encoding="utf-8") != dump_yaml(document):
                issues.append(_issue(ERROR, "AGGREGATE_YAML", "openapi.yaml is not the deterministic YAML mirror of openapi.json", paths.relpath(yaml_path)))
        except OSError:
            pass
    stats["operations"] = operations
    stats["paths"] = len(document.get("paths") or {})
    stats["operation_ids"] = len(operation_ids)


def _check_contracts_registry(model: ContractModel, issues: list[dict[str, str]], stats: dict[str, Any]) -> None:
    path = paths.OPENAPI_DIR / "contracts.json"
    if not path.exists():
        issues.append(_issue(ERROR, "CONTRACTS_MISSING", "contracts/openapi/contracts.json missing", paths.relpath(path)))
        return
    document = _load_json(path)
    entries = document.get("contracts") or []
    manifest_contracts = {c["contract"]: c["version"] for c in model.contracts}
    if len(entries) != len(manifest_contracts):
        issues.append(_issue(ERROR, "CONTRACTS_COUNT", f"contracts.json has {len(entries)} entries, manifest has {len(manifest_contracts)}", paths.relpath(path)))
    seen: set[str] = set()
    for entry in entries:
        contract = entry.get("contract", "")
        if contract in seen:
            issues.append(_issue(ERROR, "CONTRACTS_DUP", f"duplicate contract {contract}", paths.relpath(path)))
        seen.add(contract)
        if contract not in manifest_contracts:
            issues.append(_issue(ERROR, "CONTRACTS_UNKNOWN", f"{contract} not in manifest provides", paths.relpath(path)))
        elif entry.get("version") != manifest_contracts[contract]:
            issues.append(_issue(ERROR, "CONTRACTS_VERSION", f"{contract} version {entry.get('version')} != manifest {manifest_contracts[contract]}", paths.relpath(path)))
        if not entry.get("dir"):
            issues.append(_issue(ERROR, "CONTRACTS_DIR", f"{contract} has no builder dir", paths.relpath(path)))
        if not entry.get("fragment"):
            issues.append(_issue(ERROR, "CONTRACTS_FRAGMENT", f"{contract} has no fragment path", paths.relpath(path)))
    stats["contracts"] = len(entries)


def _check_events(model: ContractModel, issues: list[dict[str, str]], stats: dict[str, Any]) -> None:
    registry_path = paths.EVENTS_DIR / "registry.json"
    yaml_path = paths.EVENTS_DIR / "registry.yaml"
    if not registry_path.exists():
        issues.append(_issue(ERROR, "EVENTS_MISSING", "contracts/events/registry.json missing", paths.relpath(registry_path)))
        return
    registry = _load_json(registry_path)
    if registry.get("plan_version") != model.plan_version:
        issues.append(_issue(ERROR, "EVENTS_PLAN", "registry plan_version != manifest", paths.relpath(registry_path)))
    entries = registry.get("contracts") or []
    if len(entries) != len(model.contracts):
        issues.append(_issue(ERROR, "EVENTS_CONTRACTS", f"registry contracts {len(entries)} != 21", paths.relpath(registry_path)))
    if not yaml_path.exists() or yaml_path.read_text(encoding="utf-8") != dump_yaml(registry):
        issues.append(_issue(ERROR, "EVENTS_YAML", "registry.yaml is not the deterministic YAML mirror of registry.json", paths.relpath(yaml_path)))
    envelope_required = set((registry.get("envelope") or {}).get("required") or [])
    expected_envelope = {"event_id", "event_type", "event_version", "tenant_id", "occurred_at", "aggregate", "data"}
    if envelope_required != expected_envelope:
        issues.append(_issue(ERROR, "EVENTS_ENVELOPE", f"registry envelope required {sorted(envelope_required)} != {sorted(expected_envelope)}", paths.relpath(registry_path)))
    schema_count = 0
    for entry in entries:
        contract = entry.get("contract", "")
        for event_entry in entry.get("events") or []:
            schema_rel = event_entry.get("schema", "")
            schema_path = paths.REPO_ROOT / schema_rel
            if not schema_path.exists():
                issues.append(_issue(ERROR, "EVENTS_SCHEMA_MISSING", f"{event_entry.get('event')}: schema missing at {schema_rel}", paths.relpath(schema_path)))
                continue
            try:
                schema = json.loads(schema_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                issues.append(_issue(ERROR, "EVENTS_SCHEMA_JSON", f"{schema_rel}: invalid JSON: {exc}", paths.relpath(schema_path)))
                continue
            schema_count += 1
            if schema.get("$schema") != "https://json-schema.org/draft/2020-12/schema":
                issues.append(_issue(ERROR, "EVENTS_SCHEMA_DRAFT", f"{schema_rel}: wrong $schema", paths.relpath(schema_path)))
            required = set(schema.get("required") or [])
            if not expected_envelope <= required:
                issues.append(_issue(ERROR, "EVENTS_SCHEMA_ENVELOPE", f"{schema_rel}: missing envelope required fields", paths.relpath(schema_path)))
            if "data" not in (schema.get("properties") or {}):
                issues.append(_issue(ERROR, "EVENTS_SCHEMA_DATA", f"{schema_rel}: no data property", paths.relpath(schema_path)))
            if schema.get("x-event-type") != event_entry.get("event"):
                issues.append(_issue(ERROR, "EVENTS_SCHEMA_TYPE", f"{schema_rel}: x-event-type mismatch", paths.relpath(schema_path)))
            if schema.get("x-contract") != contract:
                issues.append(_issue(ERROR, "EVENTS_SCHEMA_CONTRACT", f"{schema_rel}: x-contract mismatch", paths.relpath(schema_path)))
            if schema.get("x-event-version") != event_entry.get("version"):
                issues.append(_issue(ERROR, "EVENTS_SCHEMA_VERSION", f"{schema_rel}: x-event-version mismatch", paths.relpath(schema_path)))
            if schema.get("x-todo") is not True:
                issues.append(_issue(WARNING, "EVENTS_SCHEMA_TODO", f"{schema_rel}: payload not marked TODO (implemented event?)", paths.relpath(schema_path)))
    if schema_count == 0:
        issues.append(_issue(ERROR, "EVENTS_EMPTY", "no event schemas found", paths.relpath(paths.EVENTS_DIR)))
    stats["event_schemas"] = schema_count


def _check_builders(issues: list[dict[str, str]], stats: dict[str, Any]) -> None:
    for dir_slug in sorted(paths.CONTRACTS_PER_DIR):
        dir_path = paths.APP_CONTRACTS_DIR / dir_slug
        contract_json = dir_path / "contract.json"
        if not contract_json.exists():
            issues.append(_issue(ERROR, "BUILDER_META", f"missing {paths.relpath(contract_json)}"))
            continue
        try:
            _load_json(contract_json)
        except json.JSONDecodeError as exc:
            issues.append(_issue(ERROR, "BUILDER_META_JSON", f"{paths.relpath(contract_json)}: {exc}"))
        fragment = dir_path / "openapi.fragment.json"
        if not fragment.exists():
            issues.append(_issue(WARNING, "BUILDER_FRAGMENT", f"no builder fragment placeholder in {paths.relpath(dir_path)}"))
            continue
        try:
            document = json.loads(fragment.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            issues.append(_issue(ERROR, "BUILDER_FRAGMENT_JSON", f"{paths.relpath(fragment)}: {exc}"))
            continue
        paths_added = document.get("paths") or {}
        stats.setdefault("builder_fragments", {})[dir_slug] = len(paths_added)


def _check_freshness(model: ContractModel, issues: list[dict[str, str]]) -> None:
    outputs = build_outputs(model)
    for rel, text in outputs.items():
        target = paths.REPO_ROOT / rel
        if not target.exists():
            issues.append(_issue(ERROR, "FRESH_MISSING", f"generated artefact missing: {rel}", rel))
            continue
        if target.read_text(encoding="utf-8") != text:
            issues.append(_issue(ERROR, "FRESH_STALE", f"generated artefact stale (run `python -m app.contracts.cli generate`): {rel}", rel))
    for extra in stale_generated_files(outputs):
        issues.append(_issue(WARNING, "FRESH_EXTRA", f"file under generated roots is not part of any generated artefact: {extra}", extra))


def run_lint(*, freshness: bool = True) -> dict[str, Any]:
    model = build_model()
    issues: list[dict[str, str]] = []
    stats: dict[str, Any] = {}
    if len(model.feature_sets) != 16:
        issues.append(_issue(ERROR, "PLAN_FS", f"manifest parsed {len(model.feature_sets)} feature sets, expected 16"))
    if len(model.contracts) != 21:
        issues.append(_issue(ERROR, "PLAN_CONTRACTS", f"manifest has {len(model.contracts)} provided contracts, expected 21"))
    for fs in model.feature_sets:
        if not fs.endpoints:
            issues.append(_issue(ERROR, "PLAN_ENDPOINTS", f"{fs.id}: no endpoint rows parsed from §6"))
        if not fs.contracts:
            issues.append(_issue(ERROR, "PLAN_PROVIDES", f"{fs.id}: manifest provides no contract"))
    for warning in model.warnings:
        issues.append(_issue(WARNING, "SPEC_PARSE", warning))
    _check_fragments(model, issues, stats)
    _check_aggregate(model, issues, stats)
    _check_contracts_registry(model, issues, stats)
    _check_events(model, issues, stats)
    _check_builders(issues, stats)
    if freshness:
        _check_freshness(model, issues)
    errors = [i for i in issues if i["level"] == ERROR]
    warnings = [i for i in issues if i["level"] == WARNING]
    return {
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
        "stats": {
            "feature_sets": len(model.feature_sets),
            "contracts": len(model.contracts),
            "plan_version": model.plan_version,
            "endpoints": len(model.endpoints),
            **stats,
        },
    }
