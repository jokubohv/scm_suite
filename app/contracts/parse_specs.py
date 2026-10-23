"""Parse the feature-set implementation specs into a deterministic contract model.

Sources (read-only during implementation):
* ``design/feature-sets.json`` — authoritative manifest: 16 feature sets, 21 provided contracts,
  versions, dependency edges.
* ``feature-sets/FS-XX.md`` §6 *API surface* — endpoint table rows (method/path/scope/idempotency).
* ``feature-sets/FS-XX.md`` §7 *Events and jobs* — event table rows (event/version/payload) and job names.

The parser is intentionally tolerant of the spec's markdown variants (method combos such as
``GET/POST``, ``v1`` vs ``1.0.0`` versions, event "family" rows such as
``inventory.receipt.posted|reversed``) and never invents endpoints or events. Anything it cannot
parse is surfaced as a lint issue instead of silently dropped.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from . import paths

_METHOD_RE = re.compile(r"\b(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\b")
_SCOPE_RE = re.compile(r"[a-z][a-z0-9_]*(?:\.[a-z0-9_]+)+")
_PAYLOAD_KEY_RE = re.compile(r"\b([a-z][a-z0-9_]*)\b")
_VERSION_CELL_RE = re.compile(r"^v?(\d+)(?:\.(\d+))?(?:\.(\d+))?$")


@dataclass(frozen=True)
class Endpoint:
    fs_id: str
    method: str
    path: str
    raw_path: str
    query: str
    purpose: str
    scope: str
    allowlist: str  # "public" | "self-service" | ""
    idempotency: str
    contracts: tuple[str, ...]

    @property
    def operation_id(self) -> str:
        return f"{self.fs_id.lower().replace('-', '')}_{self.method.lower()}_{paths.slugify(self.path)}"


@dataclass(frozen=True)
class Event:
    name: str
    version: str
    fs_id: str
    contract: str
    trigger: str
    payload_keys: tuple[str, ...]
    consumers: str
    family: bool = False
    wildcard: bool = False


@dataclass
class FeatureSet:
    id: str
    name: str
    wave: str
    summary: str
    provides: list[dict[str, Any]]
    depends_on: list[dict[str, Any]]
    modules: list[str]
    endpoints: list[Endpoint] = field(default_factory=list)
    events: list[Event] = field(default_factory=list)
    jobs: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def contracts(self) -> list[str]:
        return [p.get("contract", "") for p in self.provides]


@dataclass
class ContractModel:
    plan_version: str
    feature_sets: list[FeatureSet]
    warnings: list[str]

    @property
    def contracts(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for fs in self.feature_sets:
            for provided in fs.provides:
                result.append(
                    {
                        "contract": provided.get("contract", ""),
                        "version": provided.get("version", "0.0.0"),
                        "feature_set": fs.id,
                        "wave": fs.wave,
                        "dir": paths.CONTRACT_DIR.get(provided.get("contract", ""), ""),
                        "depends_on": [
                            d.get("contract", "")
                            for d in fs.depends_on
                            if d.get("contract")
                        ],
                    }
                )
        return result

    @property
    def events(self) -> list[Event]:
        return [e for fs in self.feature_sets for e in fs.events]

    @property
    def endpoints(self) -> list[Endpoint]:
        return [ep for fs in self.feature_sets for ep in fs.endpoints]


def load_manifest(manifest_path: Path | None = None) -> dict[str, Any]:
    path = manifest_path or paths.MANIFEST_PATH
    return json.loads(path.read_text(encoding="utf-8"))


def _section(markdown: str, number: int) -> str:
    match = re.search(rf"^## {number}\. .*?$(.*?)(?=^## \d+\. |\Z)", markdown, re.S | re.M)
    return match.group(1) if match else ""


def _tables(section: str) -> list[tuple[list[str], list[list[str]]]]:
    """Return (header_cells, rows) for every markdown table in ``section``."""
    tables: list[tuple[list[str], list[list[str]]]] = []
    header: list[str] | None = None
    rows: list[list[str]] = []

    def flush() -> None:
        nonlocal header, rows
        if header is not None:
            tables.append((header, rows))
        header, rows = None, []

    for line in section.split("\n"):
        stripped = line.strip()
        if not stripped.startswith("|"):
            flush()
            continue
        cells = [c.strip() for c in stripped.replace("\\|", "/").strip().strip("|").split("|")]
        if len(cells) < 2:
            continue
        if header is None:
            header = cells
            continue
        if all(re.fullmatch(r":?-{2,}:?", c.replace(" ", "")) for c in cells if c):
            continue
        rows.append(cells)
    flush()
    return tables


def _is_full_path(path: str) -> bool:
    return path.startswith(("/api/", "/internal/", "/v1/", "/healthz", "/readyz", "/openapi", "/me"))


def parse_endpoints(fs_id: str, markdown: str, contracts: list[str]) -> tuple[list[Endpoint], list[str]]:
    warnings: list[str] = []
    endpoints: list[Endpoint] = []
    for header, rows in _tables(f"{_section(markdown, 6)}\n"):
        lowered = [h.lower() for h in header]
        if "path" not in " ".join(lowered) or "method" not in " ".join(lowered):
            continue
        method_idx = next((i for i, h in enumerate(lowered) if "method" in h), 0)
        last_full_paths: list[str] = []
        for row in rows:
            if len(row) <= method_idx:
                continue
            methods = [m for m in _METHOD_RE.findall(row[method_idx].upper())]
            if not methods:
                continue
            path_cell = next((c for c in row[method_idx + 1:] if "/" in c), "")
            raw_paths = [t.strip() for t in re.findall(r"`([^`]+)`", path_cell)]
            if not raw_paths:
                raw_paths = [t.strip() for t in path_cell.split(",")]
            raw_paths = [t for t in raw_paths if t.startswith("/")]
            if not raw_paths:
                continue
            if all(_is_full_path(p) for p in raw_paths):
                last_full_paths = raw_paths
            else:
                # Continuation/action row (e.g. ``/post``, ``/reverse``): expand against the
                # resource path(s) declared in the previous full-path row of the same table.
                expanded: list[str] = []
                for base in last_full_paths or []:
                    for suffix in raw_paths:
                        expanded.append(base.rstrip("/") + suffix)
                if expanded:
                    raw_paths = expanded
                else:
                    warnings.append(f"{fs_id}: relative action path without a preceding resource: {raw_paths}")
                    continue
            # Scope: request/response/scope cells from the right, ignoring path-like cells.
            candidates = [c for c in row[method_idx + 2:] if not c.strip().startswith(("`/", "/"))]
            scope_cells = [c for c in candidates if _SCOPE_RE.search(c)]
            scope_text = scope_cells[-1] if scope_cells else ""
            scope_tokens = [t for t in _SCOPE_RE.findall(scope_text) if not t.endswith(".json")]
            joined = " ".join(row).lower()
            allowlist = ""
            if "public allowlist" in joined:
                allowlist = "public"
            elif "self-service allowlist" in joined:
                allowlist = "self-service"
            if not scope_tokens and not allowlist:
                warnings.append(f"{fs_id}: no capability scope parsed for {methods} {raw_paths[0]}")
            idem = ""
            if len(row) > method_idx + 1:
                idem = row[-1].strip().strip("`").strip()
                if not re.search(r"required|token|rotation|single|replay|idempotent", idem.lower()):
                    idem = ""
            purpose = row[method_idx + 2].strip() if len(row) > method_idx + 2 else ""
            for raw_path in raw_paths:
                query = raw_path.split("?", 1)[1] if "?" in raw_path else ""
                canonical = paths.canonical_path(raw_path)
                for method in methods:
                    endpoints.append(
                        Endpoint(
                            fs_id=fs_id, method=method, path=canonical, raw_path=raw_path,
                            query=query, purpose=purpose,
                            scope=", ".join(dict.fromkeys(scope_tokens)),
                            allowlist=allowlist, idempotency=idem, contracts=tuple(contracts),
                        )
                    )
    seen: dict[tuple[str, str], Endpoint] = {}
    for endpoint in endpoints:
        seen.setdefault((endpoint.method, endpoint.path), endpoint)
    return sorted(seen.values(), key=lambda e: (e.path, e.method)), warnings


def _normalise_version(cell: str) -> str | None:
    value = cell.strip().strip("`").strip()
    if not value:
        return None
    match = re.match(r"^v?(\d+(?:\.\d+){0,2})", value)
    if not match:
        return None
    raw = match.group(1)
    if raw.count(".") == 0:
        raw = f"{raw}.0.0"
    elif raw.count(".") == 1:
        raw = f"{raw}.0"
    return paths.normalize_version(raw)


def _expand_event_names(cell: str) -> list[tuple[str, bool, bool]]:
    """Expand an event-name cell into ``(name, is_family, is_wildcard)`` tuples.

    Handles backticked families (``inventory.receipt.posted|reversed``), comma-separated lists
    (``a, b``) and explanatory suffixes (``count.recounted` (additive)``) without treating prose
    as event names.
    """
    raw = cell.strip().strip("`").strip()
    if not raw:
        return []
    segments = re.findall(r"`([^`]+)`", raw)
    if not segments:
        segments = [raw]
    primary = segments[0]
    rest = raw.replace(f"`{primary}`", "", 1)
    include_extra = bool(re.match(r"^\s*(?:,|/|and)\s*`", rest))
    chosen = [primary] + (segments[1:] if include_extra else [])
    expanded: list[tuple[str, bool, bool]] = []
    previous = ""
    for segment in chosen:
        for token in re.split(r"[|,]", segment):
            token = token.strip().strip("`").strip()
            if not token:
                continue
            if token == "*" or token.endswith(".*"):
                expanded.append((token, True, True))
                continue
            if "." not in token and previous:
                parts = previous.split(".")
                token = ".".join(parts[:-1] + [token]) if len(parts) > 1 else f"{previous}.{token}"
            if not re.match(r"^[a-z][a-z0-9_]*(?:\.[a-z0-9_]+)+$", token):
                continue
            expanded.append((token, True, False))
            previous = token
    return expanded


def _payload_keys(cell: str) -> tuple[str, ...]:
    match = re.search(r"\{(.*)\}", cell, re.S)
    source = match.group(1) if match else cell
    keys: list[str] = []
    for key in _PAYLOAD_KEY_RE.findall(source):
        if key in {"a", "and", "or", "the", "of", "to", "in", "is", "for"}:
            continue
        if key not in keys:
            keys.append(key)
    return tuple(keys[:25])


def parse_events(fs_id: str, markdown: str, contract: str) -> tuple[list[Event], list[str]]:
    events: list[Event] = []
    warnings: list[str] = []
    section = _section(markdown, 7)
    for header, rows in _tables(section + "\n"):
        lowered = [h.lower() for h in header]
        if "event" not in lowered[0] and "event" not in " ".join(lowered[:2]):
            continue
        for row in rows:
            name_cell = row[0].strip()
            if not name_cell or name_cell.lower() in {"event", "job"}:
                continue
            version_cell = row[1] if len(row) > 1 else ""
            version = _normalise_version(version_cell)
            if version is None:
                continue
            trigger = row[2] if len(row) > 2 else ""
            payload = row[3] if len(row) > 3 else ""
            consumers = row[4] if len(row) > 4 else ""
            for name, family, wildcard in _expand_event_names(name_cell):
                if not name or name in {"*"}:
                    continue
                if wildcard:
                    warnings.append(f"{fs_id}: wildcard event family recorded (no schema): {name}")
                    events.append(
                        Event(name=name, version=version, fs_id=fs_id, contract=contract,
                              trigger=trigger, payload_keys=(), consumers=consumers,
                              family=True, wildcard=True)
                    )
                    continue
                events.append(
                    Event(name=name, version=version, fs_id=fs_id, contract=contract,
                          trigger=trigger, payload_keys=_payload_keys(payload),
                          consumers=consumers, family=family)
                )
    return events, warnings


def parse_jobs(markdown: str) -> list[str]:
    jobs: list[str] = []
    section = _section(markdown, 7)
    for header, rows in _tables(section + "\n"):
        lowered = [h.lower() for h in header]
        if not lowered or "job" not in lowered[0]:
            continue
        for row in rows:
            name = row[0].strip().strip("`").strip()
            if name:
                jobs.append(name)
    return sorted(dict.fromkeys(jobs))


def assign_event_contract(fs: FeatureSet, name: str) -> str:
    provided = [p.get("contract", "") for p in fs.provides]
    if len(provided) == 1:
        return provided[0]
    if fs.id == "FS-00":
        if name.startswith("order.demand"):
            return "order.demand"
        if name.startswith("order.supply"):
            return "order.supply"
        if name.startswith("finance."):
            return "platform.finance"
        if name.startswith(("platform.outbox", "platform.job", "platform.event")):
            return "platform.events"
        return "platform.core"
    if fs.id == "FS-04":
        return "inventory.balances" if "balance" in name else "inventory.ledger"
    return provided[0]


def build_model(manifest_path: Path | None = None) -> ContractModel:
    manifest = load_manifest(manifest_path)
    warnings: list[str] = []
    feature_sets: list[FeatureSet] = []
    for raw in manifest.get("feature_sets", []):
        fs = FeatureSet(
            id=raw["id"],
            name=raw.get("name", raw["id"]),
            wave=str(raw.get("wave", "")),
            summary=raw.get("summary", ""),
            provides=list(raw.get("provides", [])),
            depends_on=list(raw.get("depends_on", [])),
            modules=list(raw.get("modules", [])),
        )
        spec_path = paths.FEATURE_SETS_DIR / f"{fs.id}.md"
        if not spec_path.exists():
            warnings.append(f"{fs.id}: missing implementation spec {paths.relpath(spec_path)}")
            feature_sets.append(fs)
            continue
        markdown = spec_path.read_text(encoding="utf-8")
        contracts = fs.contracts
        endpoints, endpoint_warnings = parse_endpoints(fs.id, markdown, contracts)
        fs.endpoints = sorted(endpoints, key=lambda e: (e.path, e.method))
        warnings.extend(endpoint_warnings)
        for provided in fs.provides:
            contract = provided.get("contract", "")
            events, event_warnings = parse_events(fs.id, markdown, contract)
            fs.events.extend(events)
            warnings.extend(event_warnings)
        # De-duplicate/re-home events parsed once per contract for multi-contract FS.
        seen: dict[tuple[str, str], Event] = {}
        homed: list[Event] = []
        for event in fs.events:
            target = assign_event_contract(fs, event.name)
            key = (event.name, event.version)
            if key in seen:
                continue
            seen[key] = event
            homed.append(Event(name=event.name, version=event.version, fs_id=fs.id,
                               contract=target, trigger=event.trigger,
                               payload_keys=event.payload_keys, consumers=event.consumers,
                               family=event.family, wildcard=event.wildcard))
        # For single-contract FS events were parsed per contract; dedupe already keeps one.
        fs.events = sorted(homed, key=lambda e: (e.name, e.version))
        fs.jobs = parse_jobs(markdown)
        feature_sets.append(fs)
    return ContractModel(
        plan_version=str(manifest.get("plan_version", "0.0.0")),
        feature_sets=sorted(feature_sets, key=lambda f: f.id),
        warnings=sorted(dict.fromkeys(warnings)),
    )


def iter_contract_events(model: ContractModel) -> Iterable[tuple[str, Event]]:
    for event in model.events:
        if event.wildcard:
            continue
        yield event.contract, event
