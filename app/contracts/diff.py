"""Additive-only contract diffing (OpenAPI + event JSON Schema) with semantic version helpers.

Framework §5 L1: OpenAPI diffs and event-schema compatibility are checked against the last deployed
version and must be additive-only within a major version. This module is dependency-free and works
on the committed JSON artefacts (the YAML mirrors are generated, so diff the JSON).

Exit codes used by ``python -m app.contracts.cli diff``:
* ``0`` — additive/neutral change (or no change)
* ``2`` — breaking change detected (removed path/method/operation, removed response, removed event
  required field, narrowed enum, changed type, etc.)
* ``1`` — usage/parse error
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

SEMVER_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
HTTP_METHODS = {"get", "post", "put", "patch", "delete", "head", "options"}


def parse_semver(value: str) -> tuple[int, int, int]:
    match = SEMVER_RE.match((value or "").strip())
    if not match:
        raise ValueError(f"not a semantic version: {value!r}")
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def compare_versions(baseline: str, current: str) -> int:
    """Return -1/0/1 for baseline<current/equal/greater."""
    left, right = parse_semver(baseline), parse_semver(current)
    return (left > right) - (left < right)


def is_major_bump(baseline: str, current: str) -> bool:
    return parse_semver(current)[0] != parse_semver(baseline)[0]


def load_document(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def diff_openapi(baseline: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    breaking: list[str] = []
    additive: list[str] = []
    neutral: list[str] = []

    if baseline.get("openapi") != current.get("openapi"):
        neutral.append(f"openapi version {baseline.get('openapi')} -> {current.get('openapi')}")
    base_paths = baseline.get("paths") or {}
    curr_paths = current.get("paths") or {}
    for raw_path in sorted(set(base_paths) - set(curr_paths)):
        breaking.append(f"removed path: {raw_path}")
    for raw_path in sorted(set(curr_paths) - set(base_paths)):
        additive.append(f"added path: {raw_path}")
    for raw_path in sorted(set(base_paths) & set(curr_paths)):
        base_item = base_paths.get(raw_path) or {}
        curr_item = curr_paths.get(raw_path) or {}
        for method in sorted(set(base_item) - set(curr_item)):
            if method.lower() in HTTP_METHODS:
                breaking.append(f"removed operation: {method.upper()} {raw_path}")
        for method in sorted(set(curr_item) - set(base_item)):
            if method.lower() in HTTP_METHODS:
                additive.append(f"added operation: {method.upper()} {raw_path}")
        for method in sorted(set(base_item) & set(curr_item)):
            if method.lower() not in HTTP_METHODS:
                continue
            old_op, new_op = base_item[method], curr_item[method]
            if old_op.get("operationId") != new_op.get("operationId"):
                breaking.append(f"operationId changed: {method.upper()} {raw_path}: {old_op.get('operationId')} -> {new_op.get('operationId')}")
            old_scopes = set(old_op.get("x-required-scopes") or [])
            new_scopes = set(new_op.get("x-required-scopes") or [])
            for removed in sorted(old_scopes - new_scopes):
                breaking.append(f"removed required scope: {removed} on {method.upper()} {raw_path}")
            for added in sorted(new_scopes - old_scopes):
                additive.append(f"added required scope: {added} on {method.upper()} {raw_path}")
            old_responses = set((old_op.get("responses") or {}).keys())
            new_responses = set((new_op.get("responses") or {}).keys())
            for removed in sorted(old_responses - new_responses):
                breaking.append(f"removed response {removed}: {method.upper()} {raw_path}")
            if not old_op.get("x-skeleton") and new_op.get("x-skeleton"):
                neutral.append(f"operation marked skeleton: {method.upper()} {raw_path}")
    base_schemas = set(((baseline.get("components") or {}).get("schemas") or {}).keys())
    curr_schemas = set(((current.get("components") or {}).get("schemas") or {}).keys())
    for removed in sorted(base_schemas - curr_schemas):
        breaking.append(f"removed component schema: {removed}")
    for added in sorted(curr_schemas - base_schemas):
        additive.append(f"added component schema: {added}")
    return {"additive": additive, "breaking": breaking, "neutral": neutral, "breaking_count": len(breaking), "additive_count": len(additive)}


def diff_event_schema(baseline: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    breaking: list[str] = []
    additive: list[str] = []
    neutral: list[str] = []

    if baseline.get("type") != current.get("type"):
        breaking.append(f"root type changed: {baseline.get('type')} -> {current.get('type')}")
    old_required = set(baseline.get("required") or [])
    new_required = set(current.get("required") or [])
    for removed in sorted(old_required - new_required):
        breaking.append(f"removed required envelope/field: {removed}")
    for added in sorted(new_required - old_required):
        additive.append(f"added required field: {added}")
    old_props = baseline.get("properties") or {}
    new_props = current.get("properties") or {}
    for name in sorted(set(old_props) - set(new_props)):
        breaking.append(f"removed property: {name}")
    for name in sorted(set(new_props) - set(old_props)):
        additive.append(f"added property: {name}")
    for name in sorted(set(old_props) & set(new_props)):
        old_prop, new_prop = old_props[name], new_props[name]
        if old_prop.get("type") != new_prop.get("type"):
            breaking.append(f"property {name} type changed: {old_prop.get('type')} -> {new_prop.get('type')}")
        old_enum = set(old_prop.get("enum") or [])
        new_enum = set(new_prop.get("enum") or [])
        for removed in sorted(old_enum - new_enum):
            breaking.append(f"property {name} enum value removed: {removed}")
        for added in sorted(new_enum - old_enum):
            additive.append(f"property {name} enum value added: {added}")
    if baseline.get("additionalProperties") is not current.get("additionalProperties"):
        if current.get("additionalProperties") is False:
            breaking.append("additionalProperties narrowed to false (consumers may send extra fields)")
        else:
            neutral.append("additionalProperties changed")
    if baseline.get("x-event-version") != current.get("x-event-version"):
        try:
            if compare_versions(str(baseline.get("x-event-version", "0.0.0")), str(current.get("x-event-version", "0.0.0"))) >= 0:
                breaking.append("event version did not increase")
        except ValueError:
            neutral.append("event version not semver")
    return {"additive": additive, "breaking": breaking, "neutral": neutral, "breaking_count": len(breaking), "additive_count": len(additive)}


def diff_artifacts(kind: str, baseline_path: str | Path, current_path: str | Path) -> dict[str, Any]:
    baseline = load_document(baseline_path)
    current = load_document(current_path)
    if kind == "openapi":
        result = diff_openapi(baseline, current)
    elif kind == "event":
        result = diff_event_schema(baseline, current)
    else:
        raise ValueError(f"unknown diff kind: {kind!r} (expected 'openapi' or 'event')")
    result["kind"] = kind
    result["baseline"] = str(baseline_path)
    result["current"] = str(current_path)
    return result
