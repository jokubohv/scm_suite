"""Deterministic generation of committed contract artefacts.

All outputs are derived from the read-only plan sources (``design/feature-sets.json`` and
``feature-sets/FS-*.md``) plus optional builder extension fragments under
``app/contracts/<dir>/openapi.fragment.json``. Generation is pure/deterministic: the same inputs
produce byte-identical outputs, which the ``lint`` command enforces (freshness check).

Skeleton contract policy (S0-04): every endpoint row in a spec's §6 table appears in the FS
fragment and the aggregate; every event row in §7 appears as a JSON Schema stub with the final
outbox envelope and a ``TODO(S0-04)`` payload marker. FS builders replace skeletons with
implementation-backed schemas in their own ``app/contracts/<dir>/`` scope.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import paths
from .parse_specs import (
    ContractModel,
    Endpoint,
    Event,
    FeatureSet,
    build_model,
)
from .yaml_emit import dump_json, dump_yaml

TODO_RESPONSE = "TODO(S0-04): response schema pending feature-set implementation."
TODO_EVENT = "TODO(S0-04): payload schema pending feature-set implementation; envelope is final."
EVENT_ENVELOPE_REQUIRED = [
    "event_id",
    "event_type",
    "event_version",
    "tenant_id",
    "occurred_at",
    "aggregate",
    "data",
]
EVENT_ENVELOPE_PROPERTIES: dict[str, Any] = {
    "event_id": {"type": "string", "format": "uuid"},
    "event_type": {"type": "string", "minLength": 3},
    "event_version": {"type": "string", "pattern": r"^\d+\.\d+\.\d+$"},
    "tenant_id": {"type": "string", "format": "uuid"},
    "occurred_at": {"type": "string", "format": "date-time"},
    "aggregate": {
        "type": "object",
        "required": ["type", "id", "version"],
        "properties": {
            "type": {"type": "string"},
            "id": {"type": "string"},
            "version": {"type": "integer", "minimum": 1},
        },
        "additionalProperties": False,
    },
    "correlation_id": {"type": "string"},
    "causation_id": {"type": "string"},
    "trace_id": {"type": "string"},
    "data": {"type": "object", "additionalProperties": True},
    "metadata": {"type": "object", "additionalProperties": True},
}

PROBLEM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": "RFC 9457 problem+json envelope used by all contracts.",
    "required": ["type", "title", "status", "code"],
    "properties": {
        "type": {"type": "string", "format": "uri-reference"},
        "title": {"type": "string"},
        "status": {"type": "integer"},
        "code": {"type": "string"},
        "detail": {"type": "string"},
        "instance": {"type": "string"},
        "errors": {"type": "array", "items": {"type": "object"}},
        "trace_id": {"type": "string"},
    },
    "additionalProperties": True,
}


# --------------------------------------------------------------------------- helpers
def _fs_fragment_relpath(fs_id: str) -> str:
    return f"contracts/openapi/fragments/{fs_id}.openapi.json"


def _event_schema_relpath(contract: str, event: Event) -> str:
    return f"contracts/events/{contract}/{event.name}.schema.json"


def _contract_meta_relpath(dir_slug: str) -> str:
    return f"app/contracts/{dir_slug}/contract.json"


def _contract_readme_relpath(dir_slug: str) -> str:
    return f"app/contracts/{dir_slug}/README.md"


def _operation(endpoint: Endpoint, fragment_relpath: str) -> dict[str, Any]:
    operation: dict[str, Any] = {
        "tags": [endpoint.fs_id, *endpoint.contracts],
        "summary": endpoint.purpose or f"{endpoint.method} {endpoint.path}",
        "operationId": endpoint.operation_id,
        "x-feature-set": endpoint.fs_id,
        "x-contract": list(endpoint.contracts),
        "x-skeleton": True,
        "x-todo": "TODO(S0-04): implement handler + request/response schemas.",
        "x-fragment": fragment_relpath,
        "responses": {
            "200": {"description": TODO_RESPONSE},
            "default": {"description": "problem+json", "content": {"application/problem+json": {"schema": {"$ref": "#/components/schemas/Problem"}}}},
        },
    }
    if endpoint.query:
        operation["x-query-parameters"] = [p for p in endpoint.query.split("&") if p]
    if endpoint.scope:
        operation["x-required-scopes"] = [s.strip() for s in endpoint.scope.split(",") if s.strip()]
    if endpoint.allowlist:
        operation["x-allowlist"] = endpoint.allowlist
    if endpoint.idempotency and endpoint.idempotency not in {"—", "-", "n/a", "none"}:
        operation["x-idempotency"] = endpoint.idempotency
    if endpoint.raw_path != endpoint.path:
        operation["x-original-path"] = endpoint.raw_path
    return operation


def _paths_for(endpoints: list[Endpoint], fragment_relpath: str) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for endpoint in endpoints:
        result.setdefault(endpoint.path, {})[endpoint.method.lower()] = _operation(endpoint, fragment_relpath)
    return dict(sorted(result.items()))


def build_fragment(fs: FeatureSet, model: ContractModel) -> dict[str, Any]:
    relpath = _fs_fragment_relpath(fs.id)
    fragment: dict[str, Any] = {
        "openapi": "3.1.0",
        "info": {
            "title": f"{fs.id} — {fs.name} (contract skeleton)",
            "version": model.plan_version,
            "description": "Generated from feature-sets/FS-*.md §6; operations are skeletons (x-todo).",
            "x-feature-set": fs.id,
            "x-wave": fs.wave,
            "x-contracts": [
                {"contract": p.get("contract"), "version": p.get("version"), "dir": paths.CONTRACT_DIR.get(p.get("contract", ""), "")}
                for p in fs.provides
            ],
            "x-status": "skeleton",
            "x-todo": "TODO(S0-04): FS builder replaces skeletons with implementation-backed contracts.",
        },
        "paths": _paths_for(fs.endpoints, relpath),
        "x-events": [
            {"event": e.name, "version": e.version, "contract": e.contract}
            for e in fs.events
            if not e.wildcard
        ],
        "x-event-families": [e.name for e in fs.events if e.wildcard],
        "x-jobs": fs.jobs,
        "x-endpoint-count": len(fs.endpoints),
    }
    # Builder implementation overlay: only operations explicitly derived from the implemented app.
    implemented_overrides = 0
    implemented_added = 0
    for raw_path, item in _builder_fragments().items():
        for method, operation in item.items():
            if not _is_implemented_operation(operation) or operation.get("x-feature-set") != fs.id:
                continue
            if method in (fragment["paths"].get(raw_path) or {}):
                implemented_overrides += 1
            else:
                implemented_added += 1
            fragment["paths"].setdefault(raw_path, {})[method] = operation
    if implemented_overrides or implemented_added:
        fragment["paths"] = dict(sorted(fragment["paths"].items()))
        fragment["info"]["x-implementation-status"] = "implementation-backed"
        fragment["info"]["x-implemented-operations"] = implemented_overrides + implemented_added
        fragment["info"]["x-implemented-overrides"] = implemented_overrides
        fragment["info"]["x-implemented-additions"] = implemented_added
    return fragment


def _extension_dir_contracts(path: Path) -> set[str]:
    """Contracts advertised by the builder directory that owns an extension event schema."""
    contract_file = path.parents[1] / "contract.json"
    try:
        data = json.loads(contract_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    return {entry.get("contract") for entry in data.get("contracts", []) if entry.get("contract")}


def _event_skeleton(event: Event) -> dict[str, Any]:
    schema: dict[str, Any] = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": f"https://contracts.inventory-platform.local/{event.contract}/{event.name}/{event.version}",
        "title": event.name,
        "description": TODO_EVENT,
        "type": "object",
        "required": list(EVENT_ENVELOPE_REQUIRED),
        "properties": dict(EVENT_ENVELOPE_PROPERTIES),
        "additionalProperties": False,
        "x-event-type": event.name,
        "x-event-version": event.version,
        "x-contract": event.contract,
        "x-feature-set": event.fs_id,
        "x-payload-key-fields": list(event.payload_keys),
        "x-todo": True,
    }
    if event.family:
        schema["x-event-family"] = True
    return schema


def _extension_event_schema(event: Event) -> dict[str, Any] | None:
    """Return the implementation-backed schema when a feature set published one.

    Builders publish either a full final-envelope schema or a payload-only schema whose
    ``properties`` describe the event ``data`` object. Both forms are folded into the canonical
    envelope contract here.
    """
    root = Path(__file__).resolve().parents[2]
    matches = sorted((root / "app" / "contracts").glob(f"*/events/{event.name}.schema.json"))
    for path in matches:
        try:
            schema = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        declared_contract = schema.get("x-contract")
        candidate = declared_contract or event.contract
        if candidate != event.contract and event.contract not in _extension_dir_contracts(path):
            continue
        if schema.get("x-todo") is True:
            continue
        properties = schema.get("properties")
        if not isinstance(properties, dict) or not properties:
            continue
        has_envelope = all(key in properties for key in EVENT_ENVELOPE_REQUIRED) and "data" in properties
        if has_envelope:
            normalized = schema
        else:
            normalized = _event_skeleton(event)
            normalized["description"] = schema.get("description") or f"{event.contract}@{event.version} {event.name} payload."
            normalized["properties"]["data"] = {
                "type": "object",
                "additionalProperties": False,
                "properties": properties,
                "required": list(schema.get("required") or ()),
            }
            normalized.pop("x-todo", None)
        normalized.pop("x-todo", None)
        normalized["x-event-type"] = event.name
        normalized["x-event-version"] = event.version
        normalized["x-contract"] = event.contract
        normalized["x-feature-set"] = event.fs_id
        return normalized
    return None


def build_event_schema(event: Event) -> dict[str, Any]:
    override = _extension_event_schema(event)
    if override is not None:
        return override
    return _event_skeleton(event)


def _contract_entries(model: ContractModel) -> list[dict[str, Any]]:
    grouped: dict[str, list[Event]] = {}
    for event in model.events:
        if event.wildcard:
            grouped.setdefault(event.contract, [])
            continue
        grouped.setdefault(event.contract, []).append(event)
    entries: list[dict[str, Any]] = []
    for contract in sorted({c["contract"] for c in model.contracts}):
        owners = [c for c in model.contracts if c["contract"] == contract]
        owner = owners[0]
        events = sorted(grouped.get(contract, []), key=lambda e: (e.name, e.version))
        entries.append(
            {
                "contract": contract,
                "version": owner["version"],
                "feature_set": owner["feature_set"],
                "wave": owner["wave"],
                "dir": owner["dir"],
                "depends_on": sorted(owner["depends_on"]),
                "fragment": _fs_fragment_relpath(owner["feature_set"]),
                "events": [
                    {"event": e.name, "version": e.version, "schema": _event_schema_relpath(contract, e)}
                    for e in events
                ],
            }
        )
    return entries


def build_registry(model: ContractModel) -> dict[str, Any]:
    entries = _contract_entries(model)
    event_names = sorted({e.name for e in model.events if not e.wildcard})
    return {
        "schema_version": "1.0",
        "plan_version": model.plan_version,
        "generated_by": "app.contracts.cli",
        "envelope": {
            "required": list(EVENT_ENVELOPE_REQUIRED),
            "properties": sorted(EVENT_ENVELOPE_PROPERTIES),
            "note": "Every event schema carries the final envelope; data payloads are implementation-backed where the owning feature set published one and TODO only for remaining unimplemented producers.",
        },
        "contracts": entries,
        "event_families": sorted({e.name for e in model.events if e.wildcard}),
        "counts": {
            "feature_sets": len(model.feature_sets),
            "contracts": len(entries),
            "events": len(event_names),
            "event_schemas": sum(len(c["events"]) for c in entries),
            "endpoints": len(model.endpoints),
        },
        "event_versions": {name: sorted({e.version for e in model.events if e.name == name}) for name in event_names},
    }


def build_contracts_registry(model: ContractModel) -> dict[str, Any]:
    entries = _contract_entries(model)
    return {
        "schema_version": "1.0",
        "plan_version": model.plan_version,
        "generated_by": "app.contracts.cli",
        "contracts": entries,
        "counts": {"feature_sets": len(model.feature_sets), "contracts": len(entries), "events": sum(len(c["events"]) for c in entries)},
    }


def _is_implemented_operation(operation: Any) -> bool:
    """True for builder-fragment operations derived from the implemented app (they replace skeletons)."""
    return (
        isinstance(operation, dict)
        and operation.get("x-implemented") is True
        and operation.get("x-skeleton") is False
    )


def _builder_fragments() -> dict[str, Any]:
    """Merge optional FS-builder extension fragments (``app/contracts/<dir>/openapi.fragment.json``)."""
    merged_paths: dict[str, Any] = {}
    for dir_slug in sorted(paths.CONTRACTS_PER_DIR):
        fragment_path = paths.APP_CONTRACTS_DIR / dir_slug / "openapi.fragment.json"
        if not fragment_path.exists():
            continue
        try:
            fragment = json.loads(fragment_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        for raw_path, item in (fragment.get("paths") or {}).items():
            canonical = paths.canonical_path(raw_path)
            merged_paths.setdefault(canonical, {}).update(item or {})
    return merged_paths


def build_aggregate(model: ContractModel) -> dict[str, Any]:
    paths_out: dict[str, Any] = {}
    for fs in model.feature_sets:
        for raw_path, item in _paths_for(fs.endpoints, _fs_fragment_relpath(fs.id)).items():
            for method, operation in item.items():
                paths_out.setdefault(raw_path, {})
                if method not in paths_out[raw_path]:
                    paths_out[raw_path][method] = operation
    spec_operation_count = sum(len(item) for item in paths_out.values())
    spec_path_count = len(paths_out)
    implemented_overrides = 0
    implemented_added = 0
    implemented_paths_added = 0
    for raw_path, item in _builder_fragments().items():
        added_path = raw_path not in paths_out
        paths_out.setdefault(raw_path, {})
        for method, operation in item.items():
            if not isinstance(operation, dict):
                continue
            if method in paths_out[raw_path]:
                if _is_implemented_operation(operation):
                    paths_out[raw_path][method] = operation
                    implemented_overrides += 1
                continue
            operation.setdefault("x-skeleton", False)
            paths_out[raw_path][method] = operation
            implemented_added += 1
            if added_path:
                implemented_paths_added += 1
                added_path = False
    operation_count = sum(len(item) for item in paths_out.values())
    skeleton_operations = sum(
        1
        for item in paths_out.values()
        for operation in item.values()
        if isinstance(operation, dict) and operation.get("x-skeleton") is True
    )
    contracts = _contract_entries(model)
    return {
        "openapi": "3.1.0",
        "info": {
            "title": "Inventory Platform — Contract Surfaces (S0 skeleton)",
            "version": model.plan_version,
            "description": "Aggregated OpenAPI 3.1 document generated from feature-sets/FS-*.md §6 + design/feature-sets.json + implementation-backed builder fragments; unimplemented operations carry x-todo, implemented ones are merged from app.openapi(). Event contracts are in contracts/events/registry.json.",
            "x-plan-version": model.plan_version,
            "x-status": "skeleton",
            "x-status-detail": "C0 freeze candidate: FS-00 implementation-backed operations merged from app.openapi(); spec skeletons remain for unimplemented surfaces.",
        },
        "servers": [{"url": "/", "description": "relative to deployment origin"}],
        "tags": [{"name": fs.id, "description": f"{fs.name} ({fs.wave})"} for fs in model.feature_sets],
        "paths": dict(sorted(paths_out.items())),
        "components": {
            "schemas": {"Problem": PROBLEM_SCHEMA},
            "securitySchemes": {
                "BearerAuth": {"type": "http", "scheme": "bearer", "bearerFormat": "JWT"},
                "ApiKeyAuth": {"type": "apiKey", "in": "header", "name": "X-API-Key"},
            },
        },
        "x-feature-sets": [
            {"id": fs.id, "name": fs.name, "wave": fs.wave,
             "contracts": [{"contract": p.get("contract"), "version": p.get("version")} for p in fs.provides],
             "fragment": _fs_fragment_relpath(fs.id), "endpoint_count": len(fs.endpoints)}
            for fs in model.feature_sets
        ],
        "x-contracts": contracts,
        "x-counts": {
            "feature_sets": len(model.feature_sets),
            "contracts": len(contracts),
            "paths": len(paths_out),
            "operations": operation_count,
            "events": sum(1 for e in model.events if not e.wildcard),
            "spec_operations": spec_operation_count,
            "spec_paths": spec_path_count,
            "implemented_operations": implemented_overrides + implemented_added,
            "implemented_overrides": implemented_overrides,
            "implemented_additions": implemented_added,
            "implemented_paths_added": implemented_paths_added,
            "skeleton_operations": skeleton_operations,
        },
    }


def _contract_dir_doc(dir_slug: str, contracts: list[dict[str, Any]], model: ContractModel) -> str:
    lines = [
        f"# `app/contracts/{dir_slug}` — builder extension point",
        "",
        "Generated by `app/contracts/generate.py` (S0-04). Read `contracts/README.md` first.",
        "",
        "## Contracts owned by this directory",
        "",
    ]
    for entry in contracts:
        lines.append(f"- **`{entry['contract']}@{entry['version']}`** — {entry['feature_set']} (wave {entry['wave']})")
    lines += [
        "",
        "## How to extend (FS builder)",
        "",
        "1. Edit `openapi.fragment.json` in this directory (OpenAPI 3.1 `paths` only; no `info` merge).",
        "   Operations you add are merged by `python -m app.contracts.cli generate`; keep `operationId` unique.",
        "2. Replace event payload stubs under `contracts/events/<contract>/<event>.schema.json` with the real",
        "   JSON Schema for `data` (keep the final `event_id/event_type/event_version/tenant_id/occurred_at/aggregate` envelope).",
        "3. Bump the contract version in `design/feature-sets.json` (manifest, Lead-owned) for any breaking change;",
        "   additive changes only within a major version (framework §5, `python -m app.contracts.cli diff`).",
        "4. Regenerate artefacts (`python -m app.contracts.cli generate`) and run `lint` before the FS exit gate.",
        "",
        "Do not edit other contracts' directories, the aggregate `contracts/openapi/*`, or the event registry by hand.",
        "",
    ]
    return "\n".join(lines)


def build_outputs(model: ContractModel | None = None) -> dict[str, str]:
    """Return ``relpath -> text`` for every generated artefact (sorted, deterministic)."""
    model = model or build_model()
    outputs: dict[str, str] = {}

    # Per-FS OpenAPI skeletons.
    for fs in model.feature_sets:
        outputs[_fs_fragment_relpath(fs.id)] = dump_json(build_fragment(fs, model))

    # Aggregated OpenAPI + contract registry.
    aggregate = build_aggregate(model)
    outputs["contracts/openapi/openapi.json"] = dump_json(aggregate)
    outputs["contracts/openapi/openapi.yaml"] = dump_yaml(aggregate)
    contracts_registry = build_contracts_registry(model)
    outputs["contracts/openapi/contracts.json"] = dump_json(contracts_registry)

    # Event registry + schemas.
    registry = build_registry(model)
    outputs["contracts/events/registry.json"] = dump_json(registry)
    outputs["contracts/events/registry.yaml"] = dump_yaml(registry)
    for contract in sorted({entry["contract"] for entry in _contract_entries(model)}):
        for event in sorted((e for e in model.events if e.contract == contract and not e.wildcard), key=lambda e: (e.name, e.version)):
            outputs[_event_schema_relpath(contract, event)] = dump_json(build_event_schema(event))

    # Governance docs.
    counts = aggregate["x-counts"]
    outputs["contracts/README.md"] = _root_readme(model, counts)
    outputs["contracts/CHANGELOG.md"] = _changelog(model)

    # Builder extension directories (contract.json + README per directory).
    by_dir: dict[str, list[dict[str, Any]]] = {}
    for entry in _contract_entries(model):
        by_dir.setdefault(entry["dir"], []).append(entry)
    for dir_slug in sorted(by_dir):
        contracts = sorted(by_dir[dir_slug], key=lambda e: e["contract"])
        outputs[_contract_meta_relpath(dir_slug)] = dump_json(
            {
                "dir": dir_slug,
                "feature_set": contracts[0]["feature_set"],
                "wave": contracts[0]["wave"],
                "contracts": contracts,
                "extension_fragment": f"app/contracts/{dir_slug}/openapi.fragment.json",
                "fragments": sorted({c["fragment"] for c in contracts}),
                "events_registry": "contracts/events/registry.json",
                "instructions": "Add OpenAPI paths to openapi.fragment.json and replace event payload stubs in contracts/events/<contract>/; run python -m app.contracts.cli generate && lint.",
                "generated_by": "app.contracts.cli",
                "todo": "TODO(S0-04): FS builder replaces skeleton operations for this contract.",
            }
        )
        outputs[_contract_readme_relpath(dir_slug)] = _contract_dir_doc(dir_slug, contracts, model)
    return dict(sorted(outputs.items()))


def _root_readme(model: ContractModel, counts: dict[str, Any]) -> str:
    return f"""# Contract-first infrastructure (S0-04)

Generated by `app.contracts.cli` from `feature-sets/FS-*.md` §6 (API surface) and §7 (events/jobs)
plus the authoritative manifest `design/feature-sets.json`. **Do not hand-edit generated files**;
edit the specs (Lead-owned) or your builder extension in `app/contracts/<dir>/` and regenerate.

## What is committed

| Artefact | Meaning |
|---|---|
| `openapi/fragments/FS-XX.openapi.json` | Per-feature-set OpenAPI 3.1 skeleton, one operation per endpoint row (16 files). |
| `openapi/openapi.json` · `openapi.yaml` | Aggregated OpenAPI 3.1 document (FS skeletons + builder extension fragments). |
| `openapi/contracts.json` | Registry of the {counts['contracts']} provided contracts with versions, owners, dependencies, fragment and event paths. |
| `events/registry.json` · `events/registry.yaml` | Event contract registry: `{counts['events']}` event types across the contracts, envelope contract, schema paths. |
| `events/<contract>/<event>.schema.json` | Per-event JSON Schema (draft 2020-12) with the final outbox envelope and a `TODO(S0-04)` payload stub. |
| `../app/contracts/<dir>/contract.json` | Builder extension point metadata (one dir per contract group). |
| `../app/contracts/<dir>/openapi.fragment.json` | Builder-editable OpenAPI paths fragment (merged by `generate`). |

Current plan version: **{model.plan_version}**. Feature sets: **{counts['feature_sets']}**; contracts: **{counts['contracts']}**;
paths: **{counts['paths']}**; operations: **{counts['operations']}**; event types: **{counts['events']}**.
All numbers are regenerated — never copy them into docs.

## Commands (stdlib only, no network, deterministic)

```bash
python -m app.contracts.cli generate          # write/refresh all committed artefacts
python -m app.contracts.cli generate --check  # fail if committed artefacts are stale
python -m app.contracts.cli lint              # CI gate: structure + coverage + freshness
python -m app.contracts.cli list              # feature sets, contracts, versions
python -m app.contracts.cli diff --baseline <old openapi.json> --current <new openapi.json>
python -m app.contracts.cli diff --kind event --baseline <old schema> --current <new schema>
```

`lint` exits `0` when clean, `1` on errors (blocking CI), `2` on breaking contract diff. The S0-GATE
check "16 contract skeletons generated and lint-clean" is exactly `python -m app.contracts.cli lint`.

## Conventions

- **Base path:** `/api/v1` for tenant resources (C-02); `/internal/*` and health endpoints keep their
  paths. Spec paths authored as `/v1/...` are canonicalised to `/api/v1/...` by the generator
  (the original is preserved as `x-original-path`).
- **Exactly one capability scope per non-public operation** (`x-required-scopes`), or an explicit
  catalogue allowlist (`x-allowlist: public|self-service`). Scopes come from `design/permissions.json`.
- **Skeletons are marked** `x-skeleton: true` / `x-todo`; a real FS implementation replaces the
  skeleton operation in its own `app/contracts/<dir>/openapi.fragment.json` and removes the marker.
- **Event versions** are normalised to semver (`v1` → `1.0.0`); JSON Schema per event is versioned by
  the registry record and `x-event-version`; additive-only within a major version.
- **YAML outputs are JSON-compatible YAML 1.2** for byte-deterministic diffs without a PyYAML runtime
  dependency. Any YAML 1.2 parser reads them.
- **Change control:** breaking changes need a manifest contract version bump, consumer impact list
  (`tools/feature_sets.py who-depends-on --contract X`) and a diff run (framework §5). `diff` reports
  breaking changes and exits `2`.

## How FS builders extend their contract

1. Find your directory in `app/contracts/` (mapping: `contracts.json` → `dir`).
2. Put implementation-backed OpenAPI paths in `app/contracts/<dir>/openapi.fragment.json`
   (3.1 `paths` object; `operationId` must stay unique across the aggregate).
3. Replace `contracts/events/<contract>/<event>.schema.json` payload `data` with the real schema;
   keep the envelope (`event_id/event_type/event_version/tenant_id/occurred_at/aggregate/...`).
4. Run `python -m app.contracts.cli generate && python -m app.contracts.cli lint` before your exit gate.
"""


def _changelog(model: ContractModel) -> str:
    return f"""# Contract changelog

All provided contracts are semver'd; changes are additive within a major version (framework §5).

## {model.plan_version} — S0-04 contract-first scaffold (2026-10-23)

- Initial generated skeleton: 16 feature-set OpenAPI fragments, aggregated OpenAPI 3.1 document,
  {len([c for c in _contract_entries(model)])} provided-contract registry entries, event JSON-Schema
  stubs with the final outbox envelope, builder extension points under `app/contracts/<dir>/`.
- No contract is frozen yet: freeze points C0–C5 in `design/parallel-plan.json` apply at their
  feature-set gates. Skeleton operations are not a compatibility promise.
"""


def write_outputs(outputs: dict[str, str], *, check: bool = False) -> dict[str, list[str]]:
    written: list[str] = []
    unchanged: list[str] = []
    stale: list[str] = []
    missing: list[str] = []
    for rel, text in outputs.items():
        target = paths.REPO_ROOT / rel
        if target.exists():
            current = target.read_text(encoding="utf-8")
            if current == text:
                unchanged.append(rel)
            elif check:
                stale.append(rel)
            else:
                target.write_text(text, encoding="utf-8")
                written.append(rel)
        else:
            if check:
                missing.append(rel)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(text, encoding="utf-8")
                written.append(rel)
    created_placeholders: list[str] = []
    if not check:
        # Remove stale generated files under the owned generated roots (renames/removals), so the
        # committed tree always equals a fresh generation. Builder-owned app/contracts/** is untouched.
        expected = {paths.REPO_ROOT / rel for rel in outputs}
        for root in (paths.FRAGMENTS_DIR, paths.EVENTS_DIR):
            if not root.exists():
                continue
            for candidate in sorted(root.rglob("*")):
                if candidate.is_file() and candidate not in expected:
                    candidate.unlink()
            for directory in sorted((p for p in root.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
                try:
                    directory.rmdir()
                except OSError:
                    pass
        for dir_slug in sorted(paths.CONTRACTS_PER_DIR):
            placeholder = paths.APP_CONTRACTS_DIR / dir_slug / "openapi.fragment.json"
            if not placeholder.exists():
                placeholder.parent.mkdir(parents=True, exist_ok=True)
                placeholder.write_text(
                    dump_json(
                        {
                            "openapi": "3.1.0",
                            "info": {
                                "title": f"Builder extension fragment ({dir_slug})",
                                "version": model_plan_version_hint(),
                                "description": "TODO(S0-04): FS builder adds implementation-backed paths here; merged by app.contracts.cli generate.",
                                "x-placeholder": True,
                            },
                            "paths": {},
                        }
                    ),
                    encoding="utf-8",
                )
                created_placeholders.append(paths.relpath(placeholder))
    return {"written": sorted(written), "unchanged": sorted(unchanged), "stale": sorted(stale), "missing": sorted(missing), "created_placeholders": sorted(created_placeholders)}


def model_plan_version_hint() -> str:
    try:
        return str(build_model().plan_version)
    except Exception:  # pragma: no cover - defensive
        return "0.0.0"


def stale_generated_files(outputs: dict[str, str]) -> list[str]:
    """Committed generated files that no longer correspond to any builder output."""
    expected = {paths.REPO_ROOT / rel for rel in outputs}
    extras: list[str] = []
    for root in (paths.FRAGMENTS_DIR, paths.EVENTS_DIR, paths.OPENAPI_DIR):
        if not root.exists():
            continue
        for candidate in root.rglob("*"):
            if candidate.is_file() and candidate not in expected:
                extras.append(paths.relpath(candidate))
    return sorted(extras)
