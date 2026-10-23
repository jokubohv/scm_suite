#!/usr/bin/env python3
"""feature_sets.py -- executable feature-set plan gate.

Implements the L0 static plan gate and the L2/L3 deployment-state gates from
`design/FEATURE-SET-FRAMEWORK.md` sections 4-6:

  validate        L0: schema-ish validation, unique FS ids, dependency targets,
                  no self-deps, DAG (cycles printed as a path), phase/wave
                  monotonicity, semver range parse + satisfiability, unique
                  provided contracts, required fields.
  order           Topological deployment order (optionally up to a phase).
  check-env       L2 entry gate: every dependency must be `verified` in the
                  environment deployment state and its deployed contract version
                  must satisfy the declared range.
  record          L3 bookkeeping: atomically record deploy/verify status for one
                  feature set in one environment (verified requires evidence and
                  verified dependencies).
  who-depends-on  Consumer impact list for a provided contract.
  coverage        Validate design/feature-coverage.json against the research
                  catalogs (every catalog topic dispositioned exactly once).
  deploy-order    Wave-by-wave runbook table (entry/exit gates from manifest).

Exit codes
  0  success
  2  invalid plan / invalid invocation inputs (bad manifest, coverage, args)
  3  deployment gate failure (check-env miss, record refused, bad state file)

Deployment-state schema (`deployment-state.<env>.json`)
  {
    "FS-00": {
      "status": "verified" | "deployed" | "failed",
      "contracts": {"platform.core": "1.0.0", ...},   # actually deployed versions
      "evidence": "verification/evidence/FS-00/exit.log",  # optional path
      "updated_at": "2026-10-21T16:00:00+00:00",
      "health": "ok"                                  # optional; if present must be "ok"
    },
    ...
  }
  The file is written only by `record` (atomic temp-file + os.replace), never
  hand-edited. A missing file means "empty environment" (all sets undeployed).

Live dependency endpoint (`check-env --deps-url URL`)
  GET /internal/deps returns either
    {"environment": "dev", "feature_sets": {<same mapping as state file>}}
  or the bare mapping {<same mapping as state file>}. The tool reads
  status/contracts/health identically to the state-file mode.

Limitations: this tool is the static + deployment-state gate. It does not run
pytest, docker compose, migrations or cloud calls; manifest check commands do
that. It makes no network calls except `check-env --deps-url URL`.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

EXIT_OK = 0
EXIT_INVALID = 2
EXIT_GATE = 3

DEFAULT_MANIFEST = "design/feature-sets.json"
DEFAULT_COVERAGE = "design/feature-coverage.json"
DEFAULT_RESEARCH_DIR = "research"
DEFAULT_PERMISSIONS = "design/permissions.json"
DEFAULT_SPECS_DIR = "feature-sets"

DEFERRED_DISPOSITIONS = (
    "DEFERRED-UI",
    "DEFERRED-MOBILE",
    "DEFERRED-ANALYTICS",
    "DEFERRED-INTEGRATION",
    "DEFERRED-SUITEAPP",
    "EXCLUDED-SCM-MOBILE",
)

FS_ID_RE = re.compile(r"^FS-\d{2}$")
WAVE_RE = re.compile(r"^[Ww]?(\d+)([A-Za-z]*)$")
CONTRACT_RE = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$")


class PlanError(Exception):
    """Invalid plan input (exit 2)."""


class GateFailure(Exception):
    """Deployment gate failure (exit 3)."""


# ---------------------------------------------------------------------------
# semantic version helpers
# ---------------------------------------------------------------------------

_VERSION_RE = re.compile(r"^v?(\d+)(?:\.(\d+))?(?:\.(\d+))?$")


class SemverError(ValueError):
    pass


def parse_version(text: str) -> tuple[int, int, int]:
    """Parse '1', '1.2', '1.2.3' (optional leading v) into a 3-int tuple."""
    if not isinstance(text, str):
        raise SemverError("version must be a string, got %r" % (text,))
    m = _VERSION_RE.match(text.strip())
    if not m:
        raise SemverError("invalid semver version %r" % (text,))
    major, minor, patch = (int(m.group(1)), int(m.group(2) or 0), int(m.group(3) or 0))
    return (major, minor, patch)


class VersionRange:
    """Conservative semver range: whitespace/comma separated comparators.

    Supported tokens: exact '1.2.3', '=', '==', '>=', '>', '<=', '<',
    '^1.2.3', '~1.2.3', '1.x', '1.2.x', '*'.
    """

    def __init__(self, spec: str, comps: list[tuple[str, tuple[int, int, int]]]):
        self.spec = spec
        self.comps = comps

    def contains(self, version: tuple[int, int, int]) -> bool:
        for op, bound in self.comps:
            if op == ">=" and not version >= bound:
                return False
            if op == ">" and not version > bound:
                return False
            if op == "<=" and not version <= bound:
                return False
            if op == "<" and not version < bound:
                return False
            if op in ("=", "==") and version != bound:
                return False
        return True

    def satisfiable(self) -> bool:
        lower = None  # (version, inclusive)
        upper = None  # (version, inclusive)
        exacts = []
        for op, bound in self.comps:
            if op in (">=", ">"):
                if lower is None or bound > lower[0] or (bound == lower[0] and op == ">"):
                    lower = (bound, op == ">=")
            elif op in ("<=", "<"):
                if upper is None or bound < upper[0] or (bound == upper[0] and op == "<"):
                    upper = (bound, op == "<=")
            else:
                exacts.append(bound)
        for ex in exacts:
            if not self.contains(ex):
                return False
        if not lower and not upper:
            return True
        if lower and not upper:
            return True
        if not lower and upper:
            return upper[0] >= (0, 0, 0)
        lo, lo_inc = lower
        hi, hi_inc = upper
        if lo < hi:
            if lo_inc and hi_inc:
                return True
            if not lo_inc:
                candidate = (lo[0], lo[1], lo[2] + 1)
                return candidate <= hi if hi_inc else candidate < hi
            return True  # lo included and lo < hi
        if lo > hi:
            return False
        return lo_inc and hi_inc

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "VersionRange(%r)" % self.spec


def parse_range(spec: str) -> VersionRange:
    if not isinstance(spec, str) or not spec.strip():
        raise SemverError("empty version range")
    tokens = [t for t in re.split(r"[,\s]+", spec.strip()) if t]
    if not tokens:
        raise SemverError("empty version range")
    comps: list[tuple[str, tuple[int, int, int]]] = []
    for tok in tokens:
        if tok in ("*", "x", "X"):
            comps.append((">=", (0, 0, 0)))
            continue
        if tok.startswith("^"):
            base = parse_version(tok[1:])
            comps.append((">=", base))
            comps.append(("<", (base[0] + 1, 0, 0)))
            continue
        if tok.startswith("~"):
            base = parse_version(tok[1:])
            comps.append((">=", base))
            comps.append(("<", (base[0], base[1] + 1, 0)))
            continue
        m = re.match(r"^\d+\.\d+\.x$", tok)
        if m:
            major, minor = [int(x) for x in tok[:-2].split(".")]
            comps.append((">=", (major, minor, 0)))
            comps.append(("<", (major, minor + 1, 0)))
            continue
        m = re.match(r"^\d+\.x$", tok)
        if m:
            major = int(tok[:-2])
            comps.append((">=", (major, 0, 0)))
            comps.append(("<", (major + 1, 0, 0)))
            continue
        m = re.match(r"^(<=|>=|==|=|<|>)(.+)$", tok)
        if m:
            op = "==" if m.group(1) == "=" else m.group(1)
            comps.append((op, parse_version(m.group(2))))
            continue
        comps.append(("==", parse_version(tok)))
    if not comps:
        raise SemverError("empty version range")
    return VersionRange(spec, comps)


def wave_rank(wave) -> tuple[int, str]:
    """Canonical wave ordering: 0A < 0B < ... < 1A < ... < 2 < 10."""
    if isinstance(wave, int) and not isinstance(wave, bool):
        return (wave, "")
    m = WAVE_RE.match(str(wave))
    if not m:
        return (10**6, str(wave))
    return (int(m.group(1)), m.group(2).upper())


# ---------------------------------------------------------------------------
# plan loading + validation
# ---------------------------------------------------------------------------

REQUIRED_LIST_FIELDS = (
    "source_docs",
    "modules",
    "migrations",
    "flags",
    "entry_checks",
    "exit_checks",
    "acceptance",
)


class Plan:
    def __init__(self, path: str):
        self.path = Path(path)
        try:
            raw = self.path.read_text(encoding="utf-8")
        except OSError as exc:
            raise PlanError("cannot read manifest %s: %s" % (self.path, exc))
        try:
            self.data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise PlanError("manifest %s is not valid JSON: %s" % (self.path, exc))
        if not isinstance(self.data, dict):
            raise PlanError("manifest root must be a JSON object")
        self.sets: list[dict] = []
        self.by_id: dict[str, dict] = {}
        self.errors: list[str] = []
        self.warnings: list[str] = []
        # dependency edges: (set_id, dep_id, contract, range)
        self.edges: list[tuple[str, str, str, str]] = []
        self.unique_edges: set[tuple[str, str]] = set()
        self.providers: dict[str, list[tuple[str, str]]] = {}
        self.summary: dict = {}

    # -- helpers ----------------------------------------------------------
    def _err(self, code: str, msg: str) -> None:
        self.errors.append("%s: %s" % (code, msg))

    def _warn(self, code: str, msg: str) -> None:
        self.warnings.append("%s: %s" % (code, msg))

    def _get_set(self, set_id: str):
        return self.by_id.get(set_id)

    # -- main validation --------------------------------------------------
    def validate(self) -> tuple[list[str], list[str], dict]:
        data = self.data
        schema_version = data.get("schema_version")
        if not isinstance(schema_version, str) or not schema_version:
            self._err("E-SCHEMA", "missing/invalid top-level 'schema_version' (expected string)")
        plan_version = data.get("plan_version")
        if not isinstance(plan_version, str):
            self._err("E-SCHEMA", "missing/invalid top-level 'plan_version' (expected string)")
        else:
            try:
                parse_version(plan_version)
            except SemverError as exc:
                self._err("E-SCHEMA", "plan_version is not semver: %s" % exc)

        raw_sets = data.get("feature_sets")
        if not isinstance(raw_sets, list) or not raw_sets:
            self._err("E-SCHEMA", "'feature_sets' must be a non-empty list")
            return self.errors, self.warnings, self._summary()

        for index, raw in enumerate(raw_sets):
            if not isinstance(raw, dict):
                self._err("E-SCHEMA", "feature_sets[%d] is not an object" % index)
                continue
            self._validate_one(index, raw)

        # duplicate ids
        seen_ids: dict[str, int] = {}
        for s in self.sets:
            seen_ids[s["id"]] = seen_ids.get(s["id"], 0) + 1
        for sid, count in seen_ids.items():
            if count > 1:
                self._err("E-ID-DUP", "duplicate feature-set id %s (%d occurrences)" % (sid, count))

        self._build_index()
        self._validate_dependencies()
        self._validate_provides()
        self._validate_monotonicity()
        self._detect_cycles()
        self._validate_gate_commands()
        return self.errors, self.warnings, self._summary()

    def _validate_one(self, index: int, raw: dict) -> None:
        set_id = raw.get("id")
        label = str(set_id) if set_id else "feature_sets[%d]" % index
        if not isinstance(set_id, str) or not FS_ID_RE.match(set_id):
            self._err("E-ID", '%s: id must match "FS-\\d{2}", got %r' % (label, set_id))
            set_id = None
        name = raw.get("name")
        if not isinstance(name, str) or not name.strip():
            self._err("E-FIELD", "%s: 'name' must be a non-empty string" % label)
        phase = raw.get("phase")
        if isinstance(phase, str) and re.match(r"^\d+$", phase.strip()):
            self._warn(
                "W-PHASE-STR",
                "%s: phase %r is a string; coerced to int %d (valid but must be an int in the manifest)"
                % (label, phase, int(phase)),
            )
            phase = int(phase)
            raw = dict(raw)
            raw["phase"] = phase
        if isinstance(phase, bool) or not isinstance(phase, int) or phase < 0:
            self._err("E-PHASE", "%s: 'phase' must be an integer >= 0, got %r" % (label, phase))
            phase = None
        wave = raw.get("wave")
        if not isinstance(wave, str) or not wave.strip():
            self._err("E-WAVE", "%s: 'wave' must be a non-empty string, got %r" % (label, wave))
        elif wave_rank(wave)[0] >= 10**6:
            self._warn("W-WAVE-FMT", "%s: wave %r does not match <digits><letters>; ordering falls back to appearance" % (label, wave))
        backend_only = raw.get("backend_only")
        if backend_only is not True:
            self._err("E-BACKEND-ONLY", "%s: 'backend_only' must be true in Phase 1 (got %r)" % (label, backend_only))
        summary = raw.get("summary")
        if not isinstance(summary, str) or not summary.strip():
            self._warn("W-SUMMARY", "%s: 'summary' is missing/empty" % label)
        rollback = raw.get("rollback")
        if not isinstance(rollback, str) or not rollback.strip():
            self._err("E-FIELD", "%s: 'rollback' must be a non-empty string" % label)
        for field in REQUIRED_LIST_FIELDS:
            value = raw.get(field)
            if not isinstance(value, list) or not value:
                self._err("E-FIELD", "%s: '%s' must be a non-empty list" % (label, field))
                continue
            for item_index, item in enumerate(value):
                if not isinstance(item, str) or not item.strip():
                    self._err(
                        "E-FIELD",
                        "%s: '%s[%d]' must be a non-empty string, got %r"
                        % (label, field, item_index, item),
                    )
            if field == "source_docs":
                for doc in value:
                    if isinstance(doc, str) and doc.strip():
                        if not doc.startswith(("http://", "https://")):
                            self._err("E-SOURCE-DOC", "%s: source_docs entry is not a URL: %r" % (label, doc))
                        elif "docs.oracle.com" not in doc:
                            self._warn("W-SOURCE-DOC", "%s: source_docs entry is not an Oracle help URL: %r" % (label, doc))
        if isinstance(raw.get("flags"), list):
            for flag in raw["flags"]:
                if isinstance(flag, str) and not re.match(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$", flag):
                    self._warn("W-FLAG-FMT", "%s: flag %r does not look like '<domain>.<feature>.enabled'" % (label, flag))
        if isinstance(raw.get("migrations"), list):
            for mig in raw["migrations"]:
                if isinstance(mig, str) and not mig.startswith("alembic:"):
                    self._warn("W-MIG-FMT", "%s: migration id %r does not start with 'alembic:'" % (label, mig))
        if isinstance(raw.get("modules"), list):
            for mod in raw["modules"]:
                if isinstance(mod, str) and not mod.startswith("app/"):
                    self._warn("W-MODULE-FMT", "%s: module path %r does not start with 'app/'" % (label, mod))
        if not isinstance(raw.get("depends_on"), list):
            self._err("E-DEP-FORMAT", "%s: 'depends_on' must be a list" % label)
            raw = dict(raw)
            raw["depends_on"] = []
        if not isinstance(raw.get("provides"), list):
            self._err("E-PROVIDE-FORMAT", "%s: 'provides' must be a list" % label)
            raw = dict(raw)
            raw["provides"] = []
        self.sets.append(raw)

    def _build_index(self) -> None:
        for s in self.sets:
            sid = s.get("id")
            if isinstance(sid, str) and sid not in self.by_id:
                self.by_id[sid] = s

    def _validate_dependencies(self) -> None:
        for s in self.sets:
            sid = s.get("id")
            if not isinstance(sid, str):
                continue
            declared = s.get("depends_on") or []
            seen_pairs: set[tuple[str, str]] = set()
            for dep in declared:
                if not isinstance(dep, dict):
                    self._err("E-DEP-FORMAT", "%s: dependency entry is not an object: %r" % (sid, dep))
                    continue
                dep_id = dep.get("id")
                contract = dep.get("contract")
                version_spec = dep.get("version")
                if not isinstance(dep_id, str) or not FS_ID_RE.match(dep_id):
                    self._err("E-DEP-FORMAT", "%s: dependency id must match FS-\\d{2}, got %r" % (sid, dep_id))
                    continue
                if dep_id == sid:
                    self._err("E-DEP-SELF", "%s: set depends on itself" % sid)
                    continue
                if not self._get_set(dep_id):
                    self._err("E-DEP-MISSING", "%s: depends on unknown feature set %s" % (sid, dep_id))
                    continue
                if not isinstance(contract, str) or not contract.strip():
                    self._err("E-DEP-FORMAT", "%s -> %s: dependency 'contract' must be a non-empty string" % (sid, dep_id))
                    continue
                if not isinstance(version_spec, str) or not version_spec.strip():
                    self._err("E-DEP-FORMAT", "%s -> %s: dependency 'version' range must be a non-empty string" % (sid, dep_id))
                    continue
                pair = (dep_id, contract)
                if pair in seen_pairs:
                    self._warn("W-DEP-DUP", "%s: duplicate dependency edge %s/%s declared more than once" % (sid, dep_id, contract))
                seen_pairs.add(pair)
                try:
                    version_range = parse_range(version_spec)
                except SemverError as exc:
                    self._err("E-SEMVER-PARSE", "%s -> %s: bad version range %r (%s)" % (sid, dep_id, version_spec, exc))
                    continue
                if not version_range.satisfiable():
                    self._err("E-SEMVER-UNSAT", "%s -> %s: version range %r is unsatisfiable" % (sid, dep_id, version_spec))
                self.edges.append((sid, dep_id, contract, version_spec))
                self.unique_edges.add((sid, dep_id))

    def _validate_provides(self) -> None:
        for s in self.sets:
            sid = s.get("id")
            if not isinstance(sid, str):
                continue
            declared = s.get("provides") or []
            if not declared:
                self._err("E-PROVIDE-EMPTY", "%s: must provide at least one contract" % sid)
            for prov in declared:
                if not isinstance(prov, dict):
                    self._err("E-PROVIDE-FORMAT", "%s: provides entry is not an object: %r" % (sid, prov))
                    continue
                contract = prov.get("contract")
                version = prov.get("version")
                if not isinstance(contract, str) or not contract.strip():
                    self._err("E-PROVIDE-FORMAT", "%s: provided contract name must be a non-empty string" % sid)
                    continue
                if not CONTRACT_RE.match(contract):
                    self._warn("W-CONTRACT-FMT", "%s: contract name %r is not dot-namespaced" % (sid, contract))
                if not isinstance(version, str):
                    self._err("E-PROVIDE-FORMAT", "%s: provided contract %s version must be a string" % (sid, contract))
                    continue
                try:
                    parse_version(version)
                except SemverError as exc:
                    self._err("E-PROVIDE-VERSION", "%s: provided contract %s has bad version %r (%s)" % (sid, contract, version, exc))
                    continue
                if version.count(".") < 2:
                    self._warn("W-PROVIDE-VERSION", "%s: provided contract %s version %r is not full major.minor.patch" % (sid, contract, version))
                self.providers.setdefault(contract, []).append((sid, version))
        for contract, providers in sorted(self.providers.items()):
            if len(providers) > 1:
                detail = ", ".join("%s@%s" % p for p in providers)
                self._err("E-CONTRACT-DUP", "contract %r is provided by multiple sets/versions: %s" % (contract, detail))
        for s in self.sets:
            sid = s.get("id")
            if not isinstance(sid, str):
                continue
            provided_here = {c for c, entries in self.providers.items() for (_sid, _v) in entries if _sid == sid}
            for dep in s.get("depends_on") or []:
                if not isinstance(dep, dict):
                    continue
                dep_id = dep.get("id")
                contract = dep.get("contract")
                version_spec = dep.get("version")
                if not isinstance(dep_id, str) or not isinstance(contract, str) or not isinstance(version_spec, str):
                    continue
                target = self._get_set(dep_id)
                if not target:
                    continue
                target_contracts = {p.get("contract"): p.get("version") for p in (target.get("provides") or []) if isinstance(p, dict)}
                if contract not in target_contracts:
                    self._err(
                        "E-DEP-CONTRACT",
                        "%s -> %s: dependency declares contract %r but %s does not provide it"
                        % (sid, dep_id, contract, dep_id),
                    )
                    continue
                if contract not in provided_here and False:  # defensive no-op
                    pass
                try:
                    provided_version = parse_version(str(target_contracts[contract]))
                    version_range = parse_range(version_spec)
                except SemverError:
                    continue
                if not version_range.contains(provided_version):
                    self._err(
                        "E-DEP-RANGE",
                        "%s -> %s: %s provides %s %s which does not satisfy declared range %r"
                        % (sid, dep_id, dep_id, contract, target_contracts[contract], version_spec),
                    )

    def _validate_monotonicity(self) -> None:
        for sid, dep_id, contract, _spec in self.edges:
            s = self._get_set(sid)
            dep = self._get_set(dep_id)
            if not s or not dep:
                continue
            own_phase = s.get("phase")
            dep_phase = dep.get("phase")
            if isinstance(own_phase, int) and isinstance(dep_phase, int) and not isinstance(own_phase, bool):
                if dep_phase > own_phase:
                    self._err(
                        "E-PHASE-MONO",
                        "%s (phase %d) depends on %s (phase %d); dependencies must be at phase <= own phase"
                        % (sid, own_phase, dep_id, dep_phase),
                    )
                    continue
                if dep_phase == own_phase:
                    own_wave = wave_rank(s.get("wave"))
                    dep_wave = wave_rank(dep.get("wave"))
                    if dep_wave[0] < 10**6 and own_wave[0] < 10**6 and dep_wave > own_wave:
                        self._err(
                            "E-WAVE-MONO",
                            "%s (wave %s) depends on %s (wave %s); within a phase a dependency wave must be no later than the dependent wave"
                            % (sid, s.get("wave"), dep_id, dep.get("wave")),
                        )

    def _detect_cycles(self) -> None:
        graph: dict[str, list[str]] = {sid: [] for sid in self.by_id}
        for sid, dep_id in self.unique_edges:
            graph.setdefault(sid, []).append(dep_id)
        WHITE, GREY, BLACK = 0, 1, 2
        color = {node: WHITE for node in graph}
        stack: list[str] = []
        found = 0

        def visit(node: str) -> bool:
            nonlocal found
            color[node] = GREY
            stack.append(node)
            for nxt in graph.get(node, []):
                if color.get(nxt, WHITE) == GREY:
                    try:
                        start = stack.index(nxt)
                    except ValueError:
                        start = 0
                    cycle = stack[start:] + [nxt]
                    self._err("E-CYCLE", "dependency cycle detected: %s" % " -> ".join(cycle))
                    found += 1
                    if found >= 5:
                        return True
                elif color.get(nxt, WHITE) == WHITE:
                    if visit(nxt):
                        return True
            stack.pop()
            color[node] = BLACK
            return False

        for node in graph:
            if color[node] == WHITE:
                if visit(node):
                    break

    def _validate_gate_commands(self) -> None:
        for s in self.sets:
            sid = s.get("id")
            if not isinstance(sid, str):
                continue
            entry = [c for c in (s.get("entry_checks") or []) if isinstance(c, str)]
            exitc = [c for c in (s.get("exit_checks") or []) if isinstance(c, str)]
            if not any(("check-env" in cmd and sid in cmd) for cmd in entry):
                self._warn("W-GATE-ENTRY", "%s: no entry check invokes 'check-env --set %s'" % (sid, sid))
            if not any(("record" in cmd and sid in cmd and "--status verified" in cmd) for cmd in exitc):
                self._warn("W-GATE-EXIT", "%s: no exit check invokes 'record --set %s --status verified'" % (sid, sid))
            if not any("alembic upgrade head" in cmd for cmd in exitc):
                self._warn("W-GATE-MIGRATION", "%s: no exit check applies migrations ('alembic upgrade head')" % sid)

    def _summary(self) -> dict:
        waves = {str(s.get("wave")) for s in self.sets if s.get("wave")}
        phases = sorted({s.get("phase") for s in self.sets if isinstance(s.get("phase"), int) and not isinstance(s.get("phase"), bool)})
        self.summary = {
            "sets": len(self.by_id),
            "declared_sets": len(self.sets),
            "dep_edges": len(self.edges),
            "unique_fs_edges": len(self.unique_edges),
            "contracts": len(self.providers),
            "waves": len(waves),
            "phases": phases,
            "errors": len(self.errors),
            "warnings": len(self.warnings),
        }
        return self.summary


# ---------------------------------------------------------------------------
# graph helpers
# ---------------------------------------------------------------------------

class CycleError(Exception):
    def __init__(self, path: list[str]):
        self.path = path
        super().__init__("dependency cycle: %s" % " -> ".join(path))


def find_cycle(nodes: set[str], deps: dict[str, list[str]]) -> list[str] | None:
    WHITE, GREY, BLACK = 0, 1, 2
    color = {n: WHITE for n in nodes}
    stack: list[str] = []

    def visit(node: str) -> list[str] | None:
        color[node] = GREY
        stack.append(node)
        for nxt in deps.get(node, []):
            if nxt not in color:
                continue
            if color[nxt] == GREY:
                return stack[stack.index(nxt):] + [nxt]
            if color[nxt] == WHITE:
                found = visit(nxt)
                if found:
                    return found
        stack.pop()
        color[node] = BLACK
        return None

    for node in sorted(nodes):
        if color[node] == WHITE:
            found = visit(node)
            if found:
                return found
    return None


def topological_order(plan: Plan, max_phase: int | None = None) -> list[str]:
    include = {
        sid: s
        for sid, s in plan.by_id.items()
        if max_phase is None or (isinstance(s.get("phase"), int) and not isinstance(s.get("phase"), bool) and s["phase"] <= max_phase)
    }
    deps: dict[str, list[str]] = {sid: [] for sid in include}
    for sid, dep_id, _contract, _spec in plan.edges:
        if sid in include and dep_id in include:
            if dep_id not in deps[sid]:
                deps[sid].append(dep_id)
    remaining = {sid: set(deps[sid]) for sid in include}
    indegree = {sid: len(remaining[sid]) for sid in include}
    import heapq

    heap: list[tuple[tuple[int, str], str]] = []
    for sid in include:
        if indegree[sid] == 0:
            heapq.heappush(heap, (wave_rank(include[sid].get("wave")), sid))
    order: list[str] = []
    while heap:
        _key, sid = heapq.heappop(heap)
        order.append(sid)
        for other in include:
            if sid in remaining[other]:
                remaining[other].discard(sid)
                indegree[other] -= 1
                if indegree[other] == 0:
                    heapq.heappush(heap, (wave_rank(include[other].get("wave")), other))
    if len(order) < len(include):
        cycle = find_cycle(set(include), deps)
        if cycle:
            raise CycleError(cycle)
        raise PlanError("dependency graph is not a DAG (unknown cycle)")
    return order


# ---------------------------------------------------------------------------
# deployment state
# ---------------------------------------------------------------------------

def state_path_for(env: str) -> Path:
    return Path("deployment-state.%s.json" % env)


def load_state(path: str | os.PathLike) -> dict:
    p = Path(path)
    if not p.exists():
        return {}
    try:
        raw = p.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        raise GateFailure("cannot read deployment state %s: %s" % (p, exc))
    if not isinstance(data, dict):
        raise GateFailure("deployment state %s must be a JSON object mapping FS id -> state" % p)
    return data


def atomic_write_json(path: Path, payload: dict) -> None:
    path = Path(path)
    directory = path.parent if str(path.parent) else Path(".")
    directory.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".deployment-state.", suffix=".tmp", dir=str(directory))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def dependency_failures(plan: Plan, target: dict, state: dict) -> list[str]:
    failures: list[str] = []
    for dep in target.get("depends_on") or []:
        if not isinstance(dep, dict):
            continue
        dep_id = dep.get("id")
        contract = dep.get("contract")
        version_spec = dep.get("version")
        if not isinstance(dep_id, str) or not isinstance(contract, str) or not isinstance(version_spec, str):
            failures.append("malformed dependency entry in manifest: %r" % (dep,))
            continue
        label = "%s %s" % (dep_id, contract)
        entry = state.get(dep_id)
        if not isinstance(entry, dict):
            failures.append("[%s] dependency set not present in deployment state" % label)
            continue
        status = entry.get("status")
        if status != "verified":
            failures.append("[%s] dependency status=%r, expected 'verified'" % (label, status))
        contracts = entry.get("contracts")
        if not isinstance(contracts, dict):
            failures.append("[%s] no 'contracts' map in deployment state for dependency" % label)
            continue
        deployed = contracts.get(contract)
        if deployed is None:
            failures.append("[%s] contract is not recorded as deployed" % label)
            continue
        try:
            deployed_version = parse_version(str(deployed))
            version_range = parse_range(version_spec)
        except SemverError as exc:
            failures.append("[%s] cannot compare deployed %r with range %r: %s" % (label, deployed, version_spec, exc))
            continue
        if not version_range.contains(deployed_version):
            failures.append(
                "[%s] deployed version %s does not satisfy required range %r" % (label, deployed, version_spec)
            )
        health = entry.get("health")
        if health is not None and health != "ok":
            failures.append("[%s] dependency health=%r, expected 'ok'" % (label, health))
    return failures


def fetch_live_deps(url: str) -> dict:
    try:
        request = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310 - explicit user-supplied local endpoint
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, json.JSONDecodeError, ValueError) as exc:
        raise GateFailure("cannot read live dependency endpoint %s: %s" % (url, exc))
    if isinstance(payload, dict) and isinstance(payload.get("feature_sets"), dict):
        return payload["feature_sets"]
    if isinstance(payload, dict) and all(isinstance(v, dict) for v in payload.values()):
        return payload
    raise GateFailure(
        "live dependency endpoint %s returned an unexpected payload; expected "
        '{"feature_sets": {...}} or a bare {FS-id: {...}} map' % url
    )


# ---------------------------------------------------------------------------
# coverage
# ---------------------------------------------------------------------------

def load_catalog_ids(research_dir: Path) -> tuple[dict[str, dict[str, str]], list[str], list[str]]:
    """Return (catalogs, errors, warnings).

    catalogs maps prefix -> {id: title}. Ids must be unique and contiguous
    from <prefix>-001.
    """
    errors: list[str] = []
    warnings: list[str] = []
    catalogs: dict[str, dict[str, str]] = {}
    files = {
        "iri": research_dir / "features-item-inventory.json",
        "mpw": research_dir / "features-mfg-procurement-wms.json",
    }
    for prefix, path in files.items():
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except OSError as exc:
            errors.append("E-CATALOG-READ: cannot read %s: %s" % (path, exc))
            continue
        except json.JSONDecodeError as exc:
            errors.append("E-CATALOG-JSON: %s is not valid JSON: %s" % (path, exc))
            continue
        if not isinstance(data, list):
            errors.append("E-CATALOG-FORMAT: %s must be a JSON array" % path)
            continue
        entries: dict[str, str] = {}
        for index, item in enumerate(data):
            if not isinstance(item, dict):
                errors.append("E-CATALOG-FORMAT: %s[%d] is not an object" % (path, index))
                continue
            item_id = item.get("id")
            if not isinstance(item_id, str) or not re.match(r"^%s-\d{3}$" % prefix, item_id):
                errors.append("E-CATALOG-ID: %s[%d] has unexpected id %r" % (path, index, item_id))
                continue
            if item_id in entries:
                errors.append("E-CATALOG-DUP: %s has duplicate id %s" % (path, item_id))
                continue
            entries[item_id] = str(item.get("title", ""))
        expected = ["%s-%03d" % (prefix, i) for i in range(1, len(entries) + 1)]
        if sorted(entries, key=lambda x: int(x.split("-")[1])) != expected:
            errors.append(
                "E-CATALOG-GAP: %s ids are not contiguous %s-001..%s-%03d" % (path, prefix, prefix, len(entries))
            )
        catalogs[prefix] = entries
    return catalogs, errors, warnings


def validate_coverage(coverage_path: Path, research_dir: Path, expected_fs: set[str]) -> tuple[list[str], list[str], dict]:
    errors: list[str] = []
    warnings: list[str] = []
    catalogs, cat_errors, _cat_warnings = load_catalog_ids(research_dir)
    errors.extend(cat_errors)
    if errors:
        return errors, warnings, {}
    try:
        text = Path(coverage_path).read_text(encoding="utf-8")
    except OSError as exc:
        return ["E-COV-READ: cannot read %s: %s" % (coverage_path, exc)], warnings, {}
    duplicates: list[str] = []

    def pairs_hook(pairs):
        seen: set[str] = set()
        result = {}
        for key, value in pairs:
            if key in seen:
                duplicates.append(key)
            seen.add(key)
            result[key] = value
        return result

    try:
        data = json.loads(text, object_pairs_hook=pairs_hook)
    except json.JSONDecodeError as exc:
        return ["E-COV-JSON: %s is not valid JSON: %s" % (coverage_path, exc)], warnings, {}
    if not isinstance(data, dict):
        return ["E-COV-FORMAT: coverage root must be a JSON object"], warnings, {}
    for dup in duplicates:
        errors.append("E-COV-DUP-KEY: duplicate coverage key %r" % dup)
    totals = data.get("totals")
    entries = {k: v for k, v in data.items() if k != "totals"}
    if not isinstance(totals, dict):
        errors.append("E-COV-TOTALS: missing 'totals' metadata object")
        totals = {}
    expected_ids: dict[str, str] = {}
    for prefix, entries_map in catalogs.items():
        expected_ids.update(entries_map)
    actual_ids = set(entries)
    expected_set = set(expected_ids)
    missing = sorted(expected_set - actual_ids)
    extra = sorted(actual_ids - expected_set)
    for item in missing:
        errors.append("E-COV-MISSING: catalog topic %s has no disposition" % item)
    for item in extra:
        errors.append("E-COV-EXTRA: coverage key %s is not a catalog topic id" % item)
    if len(actual_ids) != len(entries):
        pass  # dict length already reflects duplicates; duplicates reported separately
    allowed_dispositions = set(expected_fs) | set(DEFERRED_DISPOSITIONS)
    by_disposition: dict[str, int] = {d: 0 for d in DEFERRED_DISPOSITIONS}
    by_fs: dict[str, int] = {fs: 0 for fs in sorted(expected_fs)}
    by_catalog: dict[str, int] = {prefix: 0 for prefix in catalogs}
    for key, value in entries.items():
        if not isinstance(value, dict):
            errors.append("E-COV-FORMAT: disposition for %s is not an object" % key)
            continue
        disposition = value.get("disposition")
        reason = value.get("reason")
        if disposition not in allowed_dispositions:
            errors.append("E-COV-DISPOSITION: %s has invalid disposition %r" % (key, disposition))
            continue
        if not isinstance(reason, str) or not reason.strip():
            errors.append("E-COV-REASON: %s has no human-readable reason" % key)
        if disposition in by_fs:
            by_fs[disposition] += 1
        else:
            by_disposition[disposition] += 1
        prefix = key.split("-")[0]
        if prefix in by_catalog:
            by_catalog[prefix] += 1
        else:
            errors.append("E-COV-PREFIX: %s has unknown catalog prefix" % key)
    total = len(entries)
    # totals consistency
    if totals:
        if totals.get("total") != total:
            errors.append("E-COV-TOTALS: totals.total=%r but coverage has %d topic entries" % (totals.get("total"), total))
        stored_catalog = totals.get("by_catalog")
        if not isinstance(stored_catalog, dict) or stored_catalog != by_catalog:
            errors.append("E-COV-TOTALS: by_catalog=%r does not match recomputed %r" % (stored_catalog, by_catalog))
        stored_disp = totals.get("by_disposition")
        recomputed_disp = {**by_disposition, **by_fs}
        if not isinstance(stored_disp, dict) or stored_disp != recomputed_disp:
            errors.append("E-COV-TOTALS: by_disposition does not match recomputed counts")
        stored_fs = totals.get("by_fs")
        if not isinstance(stored_fs, dict) or stored_fs != by_fs:
            errors.append("E-COV-TOTALS: by_fs does not match recomputed counts")
        if sum(recomputed_disp.values()) != total:
            errors.append("E-COV-TOTALS: disposition counts do not sum to %d" % total)
        for fs in sorted(expected_fs):
            if by_fs.get(fs, 0) < 1:
                errors.append("E-COV-FS-EMPTY: feature set %s owns no catalog topic" % fs)
        if by_disposition.get("EXCLUDED-SCM-MOBILE", 0) != 0:
            errors.append("E-COV-SCM-MOBILE: EXCLUDED-SCM-MOBILE should be 0 (SCM Mobile not catalogued)")
        validation = totals.get("validation")
        if isinstance(validation, dict):
            if validation.get("ids_expected") not in (None, total):
                errors.append("E-COV-TOTALS: validation.ids_expected=%r but total=%d" % (validation.get("ids_expected"), total))
            if validation.get("ids_present") not in (None, total):
                errors.append("E-COV-TOTALS: validation.ids_present=%r but total=%d" % (validation.get("ids_present"), total))
            if validation.get("missing") not in (None, 0):
                errors.append("E-COV-TOTALS: validation.missing=%r but no catalog ids are missing" % (validation.get("missing"),))
            if validation.get("unmapped") not in (None, 0):
                errors.append("E-COV-TOTALS: validation.unmapped=%r" % (validation.get("unmapped"),))
            if validation.get("fs_sets_covered") not in (None, len(expected_fs)):
                errors.append(
                    "E-COV-TOTALS: validation.fs_sets_covered=%r expected %d"
                    % (validation.get("fs_sets_covered"), len(expected_fs))
                )
    summary = {
        "total": total,
        "expected": len(expected_ids),
        "by_catalog": by_catalog,
        "in_scope": sum(by_fs.values()),
        "deferred": sum(by_disposition.values()),
        "by_disposition": {**by_disposition, **by_fs},
        "missing": len(missing),
        "extra": len(extra),
        "duplicates": len(duplicates),
        "errors": len(errors),
    }
    return errors, warnings, summary


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------

def load_and_validate(manifest_path: str) -> Plan:
    plan = Plan(manifest_path)
    plan.validate()
    return plan


def print_errors(errors: list[str], limit: int = 80) -> None:
    for line in errors[:limit]:
        print("ERROR " + line, file=sys.stderr)
    if len(errors) > limit:
        print("... %d more errors (see validate output)" % (len(errors) - limit), file=sys.stderr)


def cmd_validate(args) -> int:
    plan = load_and_validate(args.manifest)
    for warning in plan.warnings:
        print("WARN " + warning, file=sys.stderr)
    if plan.errors:
        print_errors(plan.errors)
        summary = plan.summary
        print(
            "VALIDATE FAILED: %d error(s), %d warning(s) | sets=%d edges=%d contracts=%d"
            % (len(plan.errors), len(plan.warnings), summary["sets"], summary["dep_edges"], summary["contracts"])
        )
        return EXIT_INVALID
    summary = plan.summary
    print(
        "VALIDATE OK: %d feature sets, %d dependency edges (%d unique FS pairs), %d contracts, %d waves, phases=%s"
        % (
            summary["sets"],
            summary["dep_edges"],
            summary["unique_fs_edges"],
            summary["contracts"],
            summary["waves"],
            summary["phases"],
        )
    )
    print("  ids unique, deps resolve, no self-deps, DAG acyclic, phase/wave monotonic, semver ranges satisfiable")
    print("  warnings: %d" % len(plan.warnings))
    return EXIT_OK


def cmd_order(args) -> int:
    plan = load_and_validate(args.manifest)
    if plan.errors:
        print_errors(plan.errors)
        print("ORDER ABORTED: manifest is invalid; fix validate first", file=sys.stderr)
        return EXIT_INVALID
    max_phase = args.phase
    if max_phase is not None and max_phase < 0:
        print("ORDER ABORTED: --phase must be >= 0", file=sys.stderr)
        return EXIT_INVALID
    try:
        order = topological_order(plan, max_phase)
    except CycleError as exc:
        print("ERROR E-CYCLE: %s" % exc, file=sys.stderr)
        return EXIT_INVALID
    except PlanError as exc:
        print("ERROR %s" % exc, file=sys.stderr)
        return EXIT_INVALID
    waves: dict[str, list[str]] = {}
    for sid in order:
        waves.setdefault(str(plan.by_id[sid].get("wave")), []).append(sid)
    if args.json:
        payload = {
            "phase": max_phase,
            "order": [
                {
                    "seq": index + 1,
                    "id": sid,
                    "phase": plan.by_id[sid].get("phase"),
                    "wave": plan.by_id[sid].get("wave"),
                    "name": plan.by_id[sid].get("name"),
                    "depends_on": sorted({dep.get("id") for dep in (plan.by_id[sid].get("depends_on") or []) if isinstance(dep, dict)}),
                }
                for index, sid in enumerate(order)
            ],
            "waves": waves,
        }
        print(json.dumps(payload, indent=2, sort_keys=False))
        return EXIT_OK
    print("DEPLOYMENT ORDER (phase <= %s):" % ("all" if max_phase is None else max_phase))
    for index, sid in enumerate(order, start=1):
        fs = plan.by_id[sid]
        deps = sorted({dep.get("id") for dep in (fs.get("depends_on") or []) if isinstance(dep, dict)})
        print(
            "%2d. [wave %-3s phase %s] %s  %s   deps=%s"
            % (index, fs.get("wave"), fs.get("phase"), sid, fs.get("name"), ",".join(deps) or "-")
        )
    print("waves: " + " -> ".join(waves))
    return EXIT_OK


def _collect_failures(plan: Plan, target: dict, state: dict) -> list[str]:
    return dependency_failures(plan, target, state)


def cmd_check_env(args) -> int:
    plan = load_and_validate(args.manifest)
    if plan.errors:
        print_errors(plan.errors)
        print("CHECK-ENV ABORTED: manifest is invalid", file=sys.stderr)
        return EXIT_INVALID
    target = plan.by_id.get(args.set)
    if target is None:
        print("CHECK-ENV ABORTED: unknown feature set %r" % args.set, file=sys.stderr)
        return EXIT_INVALID
    if args.deps_url:
        try:
            state = fetch_live_deps(args.deps_url)
        except GateFailure as exc:
            print("GATE FAILED (live deps): %s" % exc, file=sys.stderr)
            return EXIT_GATE
        source = "live endpoint %s" % args.deps_url
    else:
        path = Path(args.state) if args.state else state_path_for(args.env)
        try:
            state = load_state(path)
        except GateFailure as exc:
            print("GATE FAILED (state): %s" % exc, file=sys.stderr)
            return EXIT_GATE
        source = "state file %s" % path
    failures = _collect_failures(plan, target, state)
    own_state = state.get(args.set) or {}
    if failures:
        print("ENTRY GATE FAILED for %s (phase %s, wave %s) against %s" % (args.set, target.get("phase"), target.get("wave"), source), file=sys.stderr)
        for failure in failures:
            print("  FAIL %s" % failure, file=sys.stderr)
        print("  %d dependency check(s) failed; deployment must be blocked." % len(failures), file=sys.stderr)
        return EXIT_GATE
    deps = [dep for dep in (target.get("depends_on") or []) if isinstance(dep, dict)]
    print(
        "ENTRY GATE PASS for %s (phase %s, wave %s) against %s: %d/%d dependency contract(s) verified"
        % (args.set, target.get("phase"), target.get("wave"), source, len(deps), len(deps))
    )
    if args.set in state:
        print("  note: %s already recorded as %r in this environment" % (args.set, (state.get(args.set) or {}).get("status")))
    else:
        print("  note: %s has no deployment record yet in this environment" % args.set)
    return EXIT_OK


def cmd_record(args) -> int:
    plan = load_and_validate(args.manifest)
    if plan.errors:
        print_errors(plan.errors)
        print("RECORD ABORTED: manifest is invalid", file=sys.stderr)
        return EXIT_INVALID
    target = plan.by_id.get(args.set)
    if target is None:
        print("RECORD ABORTED: unknown feature set %r" % args.set, file=sys.stderr)
        return EXIT_INVALID
    status = args.status
    if status not in ("deployed", "verified", "failed"):
        print("RECORD ABORTED: --status must be one of deployed|verified|failed", file=sys.stderr)
        return EXIT_INVALID
    path = Path(args.state) if args.state else state_path_for(args.env)
    state = load_state(path)
    if status in ("deployed", "verified"):
        failures = _collect_failures(plan, target, state)
        if failures:
            print("RECORD REFUSED for %s --status %s: dependencies are not verified" % (args.set, status), file=sys.stderr)
            for failure in failures:
                print("  FAIL %s" % failure, file=sys.stderr)
            print("  run: python tools/feature_sets.py check-env --set %s --env %s" % (args.set, args.env), file=sys.stderr)
            return EXIT_GATE
    if status == "verified":
        if not args.evidence:
            print(
                "RECORD REFUSED for %s: --status verified requires --evidence PATH (exit-check evidence file)"
                % args.set,
                file=sys.stderr,
            )
            return EXIT_GATE
        evidence = Path(args.evidence)
        if not evidence.is_file():
            print("RECORD REFUSED for %s: evidence file %s does not exist" % (args.set, evidence), file=sys.stderr)
            return EXIT_GATE
        if evidence.stat().st_size == 0:
            print("RECORD REFUSED for %s: evidence file %s is empty" % (args.set, evidence), file=sys.stderr)
            return EXIT_GATE
    previous = state.get(args.set) if isinstance(state.get(args.set), dict) else {}
    entry: dict = dict(previous)
    entry["status"] = status
    entry["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if status == "failed":
        entry["health"] = "failed"
    else:
        entry.pop("health", None)
        entry["contracts"] = {p.get("contract"): p.get("version") for p in (target.get("provides") or []) if isinstance(p, dict)}
        entry["contracts"] = {k: v for k, v in entry["contracts"].items() if isinstance(k, str) and isinstance(v, str)}
    if args.evidence and status == "verified":
        entry["evidence"] = str(Path(args.evidence).resolve())
    state[args.set] = entry
    atomic_write_json(path, state)
    contracts = entry.get("contracts") or {}
    print(
        "RECORDED %s env=%s status=%s -> %s (contracts: %s)"
        % (args.set, args.env, status, path, ", ".join("%s@%s" % kv for kv in sorted(contracts.items())) or "-")
    )
    if status == "verified":
        print("  evidence: %s" % entry.get("evidence"))
        print("  dependants may now pass their L2 check-env gate for %s in %s" % (args.set, args.env))
    return EXIT_OK


def cmd_who_depends_on(args) -> int:
    plan = load_and_validate(args.manifest)
    if plan.errors:
        print_errors(plan.errors)
        print("WHO-DEPENDS-ON ABORTED: manifest is invalid", file=sys.stderr)
        return EXIT_INVALID
    contract = args.contract
    providers = plan.providers.get(contract)
    if not providers:
        print("ERROR: unknown contract %r" % contract, file=sys.stderr)
        print("known contracts: " + ", ".join(sorted(plan.providers)), file=sys.stderr)
        return EXIT_INVALID
    dependants: list[tuple[str, str, str]] = []
    for s in plan.sets:
        sid = s.get("id")
        for dep in s.get("depends_on") or []:
            if isinstance(dep, dict) and dep.get("contract") == contract:
                dependants.append((str(sid), str(s.get("wave")), str(dep.get("version"))))
    print("CONTRACT %s provided by %s" % (contract, ", ".join("%s@%s" % p for p in providers)))
    print("consumer impact list: %d dependent feature set(s)" % len(dependants))
    for sid, wave, version_range in sorted(dependants):
        print("  %-6s wave %-3s range %s" % (sid, wave, version_range))
    return EXIT_OK


def cmd_coverage(args) -> int:
    plan = load_and_validate(args.manifest)
    if plan.errors:
        print_errors(plan.errors)
        print("COVERAGE ABORTED: manifest is invalid", file=sys.stderr)
        return EXIT_INVALID
    expected_fs = set(plan.by_id)
    errors, warnings, summary = validate_coverage(Path(args.coverage), Path(args.research_dir), expected_fs)
    for warning in warnings:
        print("WARN " + warning, file=sys.stderr)
    if errors:
        print_errors(errors)
        print("COVERAGE FAILED: %d error(s)" % len(errors))
        return EXIT_INVALID
    print(
        "COVERAGE OK: %d/%d catalog topics accounted (iri=%d, mpw=%d); %d in-scope + %d deferred; 0 missing, 0 extra, 0 duplicates"
        % (
            summary["total"],
            summary["expected"],
            summary["by_catalog"].get("iri", 0),
            summary["by_catalog"].get("mpw", 0),
            summary["in_scope"],
            summary["deferred"],
        )
    )
    print("  dispositions: " + ", ".join("%s=%d" % (k, v) for k, v in sorted(summary["by_disposition"].items()) if v))
    print("  every feature set owns >= 1 topic; EXCLUDED-SCM-MOBILE=0; totals consistent")
    return EXIT_OK


def _gate_commands(commands: list, needle: str, fallback_last: bool = False) -> str:
    for command in commands:
        if isinstance(command, str) and needle in command:
            return command
    if fallback_last and commands:
        return str(commands[-1])
    return str(commands[0]) if commands else "-"


def cmd_deploy_order(args) -> int:
    plan = load_and_validate(args.manifest)
    if plan.errors:
        print_errors(plan.errors)
        print("DEPLOY-ORDER ABORTED: manifest is invalid", file=sys.stderr)
        return EXIT_INVALID
    max_phase = args.phase
    try:
        order = topological_order(plan, max_phase)
    except (CycleError, PlanError) as exc:
        print("ERROR %s" % exc, file=sys.stderr)
        return EXIT_INVALID
    print("# Wave-by-wave deployment runbook (generated from design/feature-sets.json)")
    print("")
    print("Pre-flight (every wave): `python tools/feature_sets.py validate`")
    print("")
    print("| Wave | # | FS | Name | Depends on | Entry gate (L2) | First exit check (L3) | Record (L3 last) |")
    print("|---|---|---|---|---|---|---|---|")

    def _first_exit(commands):
        for command in commands:
            if isinstance(command, str) and "record --set" not in command:
                return command
        return str(commands[0]) if commands else "-"

    for index, sid in enumerate(order, start=1):
        fs = plan.by_id[sid]
        wave = str(fs.get("wave"))
        deps = sorted({dep.get("id") for dep in (fs.get("depends_on") or []) if isinstance(dep, dict)})
        entry = _gate_commands(fs.get("entry_checks") or [], "check-env")
        first_exit = _first_exit(fs.get("exit_checks") or [])
        record_cmd = _gate_commands(fs.get("exit_checks") or [], "record --set", fallback_last=True)
        print(
            "| %s | %d | %s | %s | %s | `%s` | `%s` | `%s` |"
            % (wave, index, sid, fs.get("name"), ", ".join(deps) or "-", entry, first_exit, record_cmd)
        )
    print("")
    print("Wave sequence: " + " -> ".join(dict.fromkeys(str(plan.by_id[sid].get("wave")) for sid in order)))
    print("Rule: within a wave deploy rows top-to-bottom; a wave closes only when every set is `verified`.")
    print("Entry gate command form: python tools/feature_sets.py check-env --set FS-XX --env $ENV")
    print("Record command form:       python tools/feature_sets.py record --set FS-XX --env $ENV --status verified --evidence PATH")
    return EXIT_OK


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

HELP_EPILOG = """\
exit codes:
  0  success
  2  invalid plan / invalid inputs (bad manifest, bad coverage, unknown set/contract)
  3  deployment gate failure (check-env miss, record refused, unreadable state)

deployment-state schema (deployment-state.<env>.json, default path):
  {"FS-00": {"status": "verified", "contracts": {"platform.core": "1.0.0"},
             "evidence": "verification/evidence/FS-00/exit.log",
             "updated_at": "2026-10-21T16:00:00+00:00", "health": "ok"}}
  A missing file means an empty/greenfield environment.

live mode (check-env --deps-url URL) expects GET /internal/deps to return
  {"feature_sets": {"FS-00": {"status": "verified", "contracts": {...}}}}
  or the same mapping without the "feature_sets" wrapper.

examples:
  python tools/feature_sets.py validate
  python tools/feature_sets.py order --phase 1
  python tools/feature_sets.py check-env --set FS-04 --env dev
  python tools/feature_sets.py record --set FS-00 --env dev --status verified --evidence verification/evidence/FS-00/exit.log
  python tools/feature_sets.py who-depends-on --contract inventory.ledger
  python tools/feature_sets.py coverage
  python tools/feature_sets.py deploy-order --phase 1
  python tools/feature_sets.py permissions
"""



# ---------------------------------------------------------------------------
# Δ-RBAC permissions & roles catalogue gate (addendum PERMISSIONS-ROLES-ADDENDUM §7)
# ---------------------------------------------------------------------------

PERM_REQUIRED_TOP = (
    "schema_version", "plan_version", "catalogue", "aliases", "role_templates", "sod_pairs",
    "global_rules", "public_allowlist", "self_service_allowlist", "service_scopes", "counts",
)
PERM_EXPECTED = {
    "scopes": 255,
    "assignable": 241,
    "service_only": 13,
    "platform_only": 1,
    "roles": 7,
    "sod_pairs": 14,
    "requires_approval": 18,
    "global_rules": 6,
    "public_allowlist": 9,
    "self_service_allowlist": 6,
}
PERM_PUBLIC_SSO_ROUTES = (
    "GET /api/v1/auth/providers",
    "POST /api/v1/auth/sso/start",
    "GET /api/v1/auth/sso/callback",
)
PERM_SCOPE_RE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*){1,3}$")
PERM_TOKEN_RE = re.compile(r"^[a-z][a-z0-9_]*$")
PERM_ROUTE_RE = re.compile(r"^(?:/|(?:GET|POST|PUT|PATCH|DELETE) /)\S+$")
PERM_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE"}


def _perm_route_set(entries) -> set[str]:
    routes = set()
    for entry in entries:
        if not isinstance(entry, str):
            continue
        text = entry.strip()
        if " " in text:
            method, path = text.split(" ", 1)
            routes.add(method.upper() + " " + path.strip())
        else:
            routes.add(text)
    return routes


def _perm_route_matches(method: str, path: str, allow: set[str]) -> bool:
    return (method.upper() + " " + path) in allow or path in allow


def cmd_permissions(args) -> int:
    """Validate design/permissions.json + reconcile every spec endpoint auth scope."""
    errors: list[str] = []
    warnings: list[str] = []

    def err(code: str, message: str) -> None:
        errors.append("%s: %s" % (code, message))

    def warn(code: str, message: str) -> None:
        warnings.append("%s: %s" % (code, message))

    path = Path(args.permissions)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        print("PERMISSIONS FAILED: cannot read %s: %s" % (path, exc), file=sys.stderr)
        return EXIT_INVALID
    except json.JSONDecodeError as exc:
        print("PERMISSIONS FAILED: %s is not valid JSON: %s" % (path, exc), file=sys.stderr)
        return EXIT_INVALID
    if not isinstance(data, dict):
        print("PERMISSIONS FAILED: root must be a JSON object", file=sys.stderr)
        return EXIT_INVALID

    for key in PERM_REQUIRED_TOP:
        if key not in data:
            err("E-PERM-SCHEMA", "missing required top-level key %r" % key)
    schema_version = data.get("schema_version")
    plan_version = data.get("plan_version")
    try:
        if isinstance(schema_version, str):
            parse_version(schema_version)
        else:
            err("E-PERM-SCHEMA", "schema_version must be a semver string")
        if isinstance(plan_version, str):
            parse_version(plan_version)
        else:
            err("E-PERM-SCHEMA", "plan_version must be a semver string")
    except SemverError as exc:
        err("E-PERM-SCHEMA", str(exc))

    # cross-check plan version against the manifest when available
    manifest_path = Path(args.manifest) if getattr(args, "manifest", None) else None
    if manifest_path and manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if str(manifest.get("plan_version")) != str(plan_version):
                err("E-PERM-VERSION",
                    "permissions.json plan_version=%r does not match manifest plan_version=%r"
                    % (plan_version, manifest.get("plan_version")))
        except (OSError, json.JSONDecodeError) as exc:
            warn("W-PERM-MANIFEST", "could not cross-check manifest %s: %s" % (manifest_path, exc))
    elif manifest_path:
        warn("W-PERM-MANIFEST", "manifest not found at %s; plan_version not cross-checked" % manifest_path)

    # -- catalogue --------------------------------------------------------
    catalogue = data.get("catalogue")
    scopes: dict[str, dict] = {}
    if not isinstance(catalogue, list) or not catalogue:
        err("E-PERM-CATALOGUE", "catalogue must be a non-empty list")
        catalogue = []
    for index, entry in enumerate(catalogue):
        label = "catalogue[%d]" % index
        if not isinstance(entry, dict):
            err("E-PERM-CATALOGUE", "%s is not an object" % label)
            continue
        required = {"scope", "domain", "action", "requires_approval", "platform_only",
                    "service_only", "assignable", "owner_feature_set"}
        missing = sorted(required - set(entry))
        if missing:
            err("E-PERM-CATALOGUE", "%s missing fields %s" % (label, missing))
            continue
        scope = entry.get("scope")
        if not isinstance(scope, str) or not PERM_SCOPE_RE.match(scope):
            err("E-PERM-SCOPE", "%s invalid scope string %r" % (label, scope))
            continue
        if scope in scopes:
            err("E-PERM-DUP", "duplicate scope %r" % scope)
            continue
        domain = entry.get("domain")
        action = entry.get("action")
        if not isinstance(domain, str) or not PERM_TOKEN_RE.match(domain):
            err("E-PERM-DOMAIN", "%s invalid domain %r" % (label, domain))
        if not isinstance(action, str) or not PERM_TOKEN_RE.match(action):
            err("E-PERM-ACTION", "%s invalid action %r" % (label, action))
        if isinstance(domain, str) and isinstance(action, str) and scope != domain + "." + action:
            # 2-4 segment scopes: domain = first segment, action = last segment
            if not (scope.startswith(domain + ".") and scope.endswith("." + action)):
                err("E-PERM-NAMING", "%s scope %r does not match domain.action %r" % (label, scope, domain + "." + action))
        for flag in ("requires_approval", "platform_only", "service_only", "assignable"):
            if not isinstance(entry.get(flag), bool):
                err("E-PERM-FLAG", "%s flag %r must be a boolean" % (label, flag))
        owner = entry.get("owner_feature_set")
        if not isinstance(owner, str) or not re.match(r"^FS-\d{2}$", owner):
            err("E-PERM-OWNER", "%s invalid owner_feature_set %r" % (label, owner))
        expected_assignable = not (entry.get("platform_only") is True or entry.get("service_only") is True)
        if isinstance(entry.get("assignable"), bool) and entry["assignable"] != expected_assignable:
            err("E-PERM-FLAG-COHERENCE",
                "%s assignable=%r but platform_only=%r/service_only=%r"
                % (label, entry.get("assignable"), entry.get("platform_only"), entry.get("service_only")))
        scopes[scope] = entry

    assignable = [e for e in scopes.values() if e.get("assignable") is True]
    platform_only = [e for e in scopes.values() if e.get("platform_only") is True]
    service_only = [e for e in scopes.values() if e.get("service_only") is True]
    requires_approval = [e for e in scopes.values() if e.get("requires_approval") is True]
    if len(platform_only) != 1 or platform_only[0].get("scope") != "platform.tenant.manage":
        err("E-PERM-PLATFORM-ONLY", "expected exactly one platform-only scope 'platform.tenant.manage'")
    service_scopes = data.get("service_scopes")
    if not isinstance(service_scopes, list) or not all(isinstance(x, str) for x in service_scopes):
        err("E-PERM-SERVICE-SCOPES", "service_scopes must be a list of strings")
    else:
        expected_service = {e["scope"] for e in service_only}
        for scope in service_scopes:
            if scope not in expected_service:
                err("E-PERM-SERVICE-SCOPES", "service_scopes contains %r which is not a catalogue service-only scope" % scope)
        if "platform.internal" not in service_scopes:
            err("E-PERM-SERVICE-SCOPES", "service_scopes must include the internal service principal scope 'platform.internal'")
        if len(service_scopes) != len(expected_service):
            warn("W-PERM-SERVICE-SCOPES",
                 "service_scopes lists %d of the %d service-only catalogue scopes (job/task scopes may be system-internal only)"
                 % (len(service_scopes), len(expected_service)))
    counts = data.get("counts")
    if not isinstance(counts, dict):
        err("E-PERM-COUNTS", "counts must be an object")
        counts = {}
    recomputed_domains = {}
    for entry in scopes.values():
        recomputed_domains[entry.get("domain")] = recomputed_domains.get(entry.get("domain"), 0) + 1
    recomputed_counts = {
        "scopes": len(scopes),
        "assignable_scopes": len(assignable),
        "platform_only": len(platform_only),
        "service_only": len(service_only),
        "aliases": len(data.get("aliases") or {}),
        "roles": len(data.get("role_templates") or []),
        "sod_pairs": len(data.get("sod_pairs") or []),
        "domains": dict(sorted(recomputed_domains.items())),
    }
    if counts and counts.get("scopes") != recomputed_counts["scopes"]:
        err("E-PERM-COUNTS", "counts.scopes=%r but catalogue has %d" % (counts.get("scopes"), len(scopes)))
    for key in ("assignable_scopes", "platform_only", "service_only", "aliases", "roles", "sod_pairs"):
        stored = counts.get(key)
        if stored is not None and stored != recomputed_counts[key]:
            err("E-PERM-COUNTS", "counts.%s=%r but recomputed %r" % (key, stored, recomputed_counts[key]))
    if counts.get("domains") not in (None, recomputed_counts["domains"]):
        err("E-PERM-COUNTS", "counts.domains does not match recomputed domain counts")
    expected_now = {
        "scopes": PERM_EXPECTED["scopes"],
        "assignable": PERM_EXPECTED["assignable"],
        "service_only": PERM_EXPECTED["service_only"],
        "platform_only": PERM_EXPECTED["platform_only"],
        "roles": PERM_EXPECTED["roles"],
        "sod_pairs": PERM_EXPECTED["sod_pairs"],
        "requires_approval": PERM_EXPECTED["requires_approval"],
        "global_rules": PERM_EXPECTED["global_rules"],
        "public_allowlist": PERM_EXPECTED["public_allowlist"],
        "self_service_allowlist": PERM_EXPECTED["self_service_allowlist"],
    }
    got_now = {
        "scopes": len(scopes), "assignable": len(assignable), "service_only": len(service_only),
        "platform_only": len(platform_only), "roles": len(data.get("role_templates") or []),
        "sod_pairs": len(data.get("sod_pairs") or []), "requires_approval": len(requires_approval),
        "global_rules": len(data.get("global_rules") or []),
        "public_allowlist": len(data.get("public_allowlist") or []),
        "self_service_allowlist": len(data.get("self_service_allowlist") or []),
    }
    for key, expected in expected_now.items():
        if got_now[key] != expected:
            err("E-PERM-COUNT", "%s=%d, expected %d (plan v1.2.0 Δ-RBAC catalogue)" % (key, got_now[key], expected))

    # -- aliases ----------------------------------------------------------
    aliases = data.get("aliases")
    if not isinstance(aliases, dict):
        err("E-PERM-ALIAS", "aliases must be an object")
        aliases = {}
    for alias, target in aliases.items():
        if not isinstance(alias, str) or not PERM_SCOPE_RE.match(alias):
            err("E-PERM-ALIAS", "invalid alias name %r" % alias)
        if alias in scopes:
            err("E-PERM-ALIAS", "alias %r is also seeded in the catalogue (aliases must not be seeded)" % alias)
        if not isinstance(target, str) or target not in scopes:
            err("E-PERM-ALIAS", "alias %r target %r is not in the catalogue" % (alias, target))

    # -- SoD pairs --------------------------------------------------------
    sod_pairs = data.get("sod_pairs")
    if not isinstance(sod_pairs, list):
        err("E-PERM-SOD", "sod_pairs must be a list")
        sod_pairs = []
    seen_pairs = set()
    for index, pair in enumerate(sod_pairs):
        if not isinstance(pair, dict):
            err("E-PERM-SOD", "sod_pairs[%d] is not an object" % index)
            continue
        a, b = pair.get("a"), pair.get("b")
        if a not in scopes or b not in scopes:
            err("E-PERM-SOD", "sod_pairs[%d] references unknown scope(s): %r/%r" % (index, a, b))
            continue
        if a == b:
            err("E-PERM-SOD", "sod_pairs[%d] pairs a scope with itself" % index)
        key = tuple(sorted((a, b)))
        if key in seen_pairs:
            err("E-PERM-SOD", "duplicate SoD pair %r" % (key,))
        seen_pairs.add(key)
        if pair.get("enforcement") not in ("HARD", "WARN"):
            err("E-PERM-SOD", "sod_pairs[%d] enforcement must be HARD or WARN" % index)
        if not isinstance(pair.get("reason"), str) or not pair["reason"].strip():
            err("E-PERM-SOD", "sod_pairs[%d] missing reason" % index)

    # -- global rules -----------------------------------------------------
    global_rules = data.get("global_rules")
    if not isinstance(global_rules, list) or not global_rules:
        err("E-PERM-RULES", "global_rules must be a non-empty list")
        global_rules = []
    for index, rule in enumerate(global_rules):
        if not isinstance(rule, str) or not rule.strip():
            err("E-PERM-RULES", "global_rules[%d] must be a non-empty string" % index)

    # -- allowlists -------------------------------------------------------
    public_allow = data.get("public_allowlist")
    self_allow = data.get("self_service_allowlist")
    for label, entries in (("public_allowlist", public_allow), ("self_service_allowlist", self_allow)):
        if not isinstance(entries, list) or not entries:
            err("E-PERM-ALLOWLIST", "%s must be a non-empty list" % label)
            continue
        for entry in entries:
            if not isinstance(entry, str) or not PERM_ROUTE_RE.match(entry.strip()):
                err("E-PERM-ALLOWLIST", "%s entry is not a route string: %r" % (label, entry))
    if isinstance(public_allow, list) and len(public_allow) != PERM_EXPECTED["public_allowlist"]:
        warn("W-PERM-ALLOWLIST", "public_allowlist has %d entries (ARCHITECTURE documents %d)"
             % (len(public_allow), PERM_EXPECTED["public_allowlist"]))
    if isinstance(self_allow, list) and len(self_allow) != PERM_EXPECTED["self_service_allowlist"]:
        warn("W-PERM-ALLOWLIST", "self_service_allowlist has %d entries (ARCHITECTURE documents %d)"
             % (len(self_allow), PERM_EXPECTED["self_service_allowlist"]))
    if isinstance(public_allow, list):
        for route in PERM_PUBLIC_SSO_ROUTES:
            if route not in public_allow:
                err("E-PERM-ALLOWLIST", "public allowlist is missing the Δ-SSO public route %r" % route)
    for scope in ("platform.sso.manage", "platform.sso.read"):
        if scope not in scopes:
            err("E-PERM-SSO", "Δ-SSO scope %r missing from the catalogue" % scope)
        elif scopes[scope].get("assignable") is not True:
            err("E-PERM-SSO", "Δ-SSO scope %r must be assignable (tenant-admin managed)" % scope)
    public_routes = _perm_route_set(public_allow) if isinstance(public_allow, list) else set()
    self_routes = _perm_route_set(self_allow) if isinstance(self_allow, list) else set()

    # -- role templates ---------------------------------------------------
    role_templates = data.get("role_templates")
    if not isinstance(role_templates, list):
        err("E-PERM-ROLE", "role_templates must be a list")
        role_templates = []
    role_keys = set()
    role_names = set()
    admin_scopes = None
    assignable_scopes = {e["scope"] for e in assignable}
    for index, role in enumerate(role_templates):
        if not isinstance(role, dict):
            err("E-PERM-ROLE", "role_templates[%d] is not an object" % index)
            continue
        key = role.get("key")
        name = role.get("name")
        if not isinstance(key, str) or not PERM_TOKEN_RE.match(key):
            err("E-PERM-ROLE", "role_templates[%d] invalid key %r" % (index, key))
        elif key in role_keys:
            err("E-PERM-ROLE", "duplicate role template key %r" % key)
        else:
            role_keys.add(key)
        if not isinstance(name, str) or not name.strip():
            err("E-PERM-ROLE", "role_templates[%d] missing name" % index)
        elif name in role_names:
            err("E-PERM-ROLE", "duplicate role template name %r" % name)
        else:
            role_names.add(name)
        role_scopes = role.get("scopes")
        if not isinstance(role_scopes, list) or not role_scopes:
            err("E-PERM-ROLE", "role_templates[%d] scopes must be a non-empty list" % index)
            continue
        if len(role_scopes) != len(set(role_scopes)):
            err("E-PERM-ROLE", "role_templates[%d] has duplicate scopes" % index)
        for scope in role_scopes:
            if not isinstance(scope, str) or scope not in scopes:
                err("E-PERM-ROLE", "role_templates[%d] references unknown scope %r" % (index, scope))
                continue
            entry = scopes[scope]
            if entry.get("platform_only") or entry.get("service_only") or not entry.get("assignable"):
                err("E-PERM-ROLE", "role_templates[%d] contains non-assignable scope %r" % (index, scope))
        if role.get("system") is not True:
            err("E-PERM-ROLE", "role_templates[%d] (%s) must be system=true" % (index, key))
        if role.get("editable") is not False:
            err("E-PERM-ROLE", "role_templates[%d] (%s) must be editable=false (non-editable, cloneable)" % (index, key))
        if role.get("cloneable") is not True:
            err("E-PERM-ROLE", "role_templates[%d] (%s) must be cloneable=true" % (index, key))
        if key == "admin":
            admin_scopes = set(role_scopes)
        else:
            for pair in sod_pairs:
                if not isinstance(pair, dict):
                    continue
                if pair.get("a") in role_scopes and pair.get("b") in role_scopes:
                    err("E-PERM-SOD-ROLE",
                        "non-admin role template %r combines SoD pair %r/%r" % (key, pair.get("a"), pair.get("b")))
    if admin_scopes is not None and admin_scopes != assignable_scopes:
        err("E-PERM-ROLE-ADMIN",
            "admin template must contain exactly the %d assignable scopes (missing=%s extra=%s)"
            % (len(assignable_scopes),
               sorted(assignable_scopes - admin_scopes)[:5], sorted(admin_scopes - assignable_scopes)[:5]))
    if role_templates and "admin" not in role_keys:
        err("E-PERM-ROLE", "no admin/tenant-administrator role template found")

    # -- spec endpoint scope reconciliation ------------------------------
    specs_dir = Path(args.specs_dir)
    spec_files = sorted(specs_dir.glob("FS-*.md")) if specs_dir.is_dir() else []
    if not spec_files:
        err("E-PERM-SPECS", "no FS-*.md specs found under %s" % specs_dir)
    referenced: set[str] = set()
    unknown: list[tuple[str, int, str]] = []
    allowlist_mismatches: list[tuple[str, int, str]] = []
    endpoint_rows = 0
    for spec in spec_files:
        text = spec.read_text(encoding="utf-8")
        start = text.find("## 6. API surface")
        end = text.find("## 7. Events and jobs")
        if start < 0 or end < 0:
            warn("W-PERM-SPEC-SECTION", "%s has no §6 API surface section; skipped" % spec.name)
            continue
        section = text[start:end]
        section_line0 = text[:start].count("\n") + 1
        for offset, line in enumerate(section.splitlines()):
            line_no = section_line0 + offset
            if not re.match(r"^\| (GET|POST|PUT|PATCH|DELETE) \|", line):
                continue
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if len(cells) < 3:
                continue
            endpoint_rows += 1
            method = cells[0].strip()
            path = cells[1].strip().strip("`")
            auth_cell = cells[-2] if len(cells) >= 2 else ""
            allow_match = re.search(r"none\s*\(([^)]*)\)", auth_cell, re.I)
            if allow_match:
                kind = allow_match.group(1).lower()
                routes = public_routes if "public" in kind else self_routes
                if routes and not _perm_route_matches(method, path, routes):
                    allowlist_mismatches.append((spec.name, line_no, "%s %s -> %r" % (method, path, kind)))
                continue
            tokens: list[str] = []
            for raw in re.findall(r"`([^`]+)`", auth_cell):
                tokens.extend(t for t in re.split(r"[,;+/\s]+", raw) if t)
            if not tokens and auth_cell:
                tokens = [t for t in re.split(r"[,;+/\s]+", auth_cell.strip("` ")) if t]
            for token in tokens:
                token = token.strip("` ").strip()
                if not token or token.lower().startswith("none") or not PERM_SCOPE_RE.match(token):
                    continue
                referenced.add(token)
                if token not in scopes:
                    unknown.append((spec.name, line_no, token))
    if allowlist_mismatches:
        for spec_name, line_no, detail in allowlist_mismatches[:10]:
            warn("W-PERM-ALLOWLIST-ROUTE", "%s:%d: %s (route not present in the corresponding allowlist)" % (spec_name, line_no, detail))
    unreferenced = sorted(set(scopes) - referenced)
    if unknown:
        for spec_name, line_no, token in unknown[:30]:
            errors.append("E-PERM-UNKNOWN-SCOPE: %s:%d endpoint declares scope %r not in design/permissions.json" % (spec_name, line_no, token))
        if len(unknown) > 30:
            errors.append("E-PERM-UNKNOWN-SCOPE: ... %d more unknown scope references" % (len(unknown) - 30))
    if unreferenced:
        warn("W-PERM-UNREFERENCED",
             "%d catalogue scope(s) are not referenced by any spec endpoint auth scope: %s"
             % (len(unreferenced), ", ".join(unreferenced[:30]) + (" ..." if len(unreferenced) > 30 else "")))

    # -- report -----------------------------------------------------------
    for message in warnings:
        print("WARN " + message, file=sys.stderr)
    if errors:
        for message in errors:
            print("ERROR " + message, file=sys.stderr)
        print("PERMISSIONS FAILED: %d error(s), %d warning(s) | catalogue %d scopes, %d role templates, %d SoD pairs"
              % (len(errors), len(warnings), len(scopes), len(role_templates), len(sod_pairs)))
        return EXIT_INVALID
    print("PERMISSIONS OK: %d scopes (%d assignable, %d service-only, %d platform-only, %d requires-approval, %d alias(es)); %d role templates; %d SoD pairs; %d global rules"
          % (len(scopes), len(assignable), len(service_only), len(platform_only), len(requires_approval),
             len(aliases), len(role_templates), len(sod_pairs), len(global_rules)))
    print("  allowlists: public=%d, self-service=%d; route-guard test: %s"
          % (len(public_allow or []), len(self_allow or []), data.get("route_guard_test", "-")))
    print("  spec reconciliation: %d specs, %d endpoint rows scanned, %d catalogue scope references, 0 unknown, %d unreferenced warning(s)"
          % (len(spec_files), endpoint_rows, len(referenced), len(unreferenced)))
    if args.json:
        print(json.dumps({
            "scopes": len(scopes), "assignable": len(assignable), "service_only": len(service_only),
            "platform_only": len(platform_only), "requires_approval": len(requires_approval),
            "aliases": len(aliases), "roles": len(role_templates), "sod_pairs": len(sod_pairs),
            "global_rules": len(global_rules), "endpoint_rows": endpoint_rows,
            "spec_scope_references": len(referenced), "unknown_scopes": len(unknown),
            "unreferenced_scopes": unreferenced,
        }, indent=2, sort_keys=False))
    return EXIT_OK

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="feature_sets.py",
        description="Executable feature-set plan gate (framework sections 4-6).",
        epilog=HELP_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--manifest", default=DEFAULT_MANIFEST, help="manifest path (default: %s)" % DEFAULT_MANIFEST)

    p_validate = sub.add_parser("validate", help="L0 static validation (schema, DAG, monotonicity, semver, contracts, fields)")
    add_common(p_validate)
    p_validate.set_defaults(func=cmd_validate)

    p_order = sub.add_parser("order", help="topological deployment order")
    add_common(p_order)
    p_order.add_argument("--phase", type=int, default=None, help="include feature sets with phase <= N (default: all)")
    p_order.add_argument("--json", action="store_true", help="machine-readable JSON output")
    p_order.set_defaults(func=cmd_order)

    p_check = sub.add_parser("check-env", help="L2 entry gate: dependencies verified and contract ranges satisfied")
    add_common(p_check)
    p_check.add_argument("--set", required=True, help="feature-set id, e.g. FS-04")
    p_check.add_argument("--env", required=True, help="environment name (dev|staging|prod), used for the default state path")
    p_check.add_argument("--state", default=None, help="deployment-state path (default: deployment-state.<env>.json)")
    p_check.add_argument("--deps-url", default=None, help="live /internal/deps URL instead of a state file")
    p_check.set_defaults(func=cmd_check_env)

    p_record = sub.add_parser("record", help="L3 bookkeeping: atomically record status for one set in one env")
    add_common(p_record)
    p_record.add_argument("--set", required=True, help="feature-set id, e.g. FS-00")
    p_record.add_argument("--env", required=True, help="environment name (dev|staging|prod)")
    p_record.add_argument("--status", required=True, choices=["deployed", "verified", "failed"], help="new status")
    p_record.add_argument("--evidence", default=None, help="exit-check evidence file (required for --status verified)")
    p_record.add_argument("--state", default=None, help="deployment-state path (default: deployment-state.<env>.json)")
    p_record.set_defaults(func=cmd_record)

    p_who = sub.add_parser("who-depends-on", help="consumer impact list for a provided contract")
    add_common(p_who)
    p_who.add_argument("--contract", required=True, help="provided contract name, e.g. inventory.ledger")
    p_who.set_defaults(func=cmd_who_depends_on)

    p_cov = sub.add_parser("coverage", help="validate feature-coverage.json against the research catalogs")
    add_common(p_cov)
    p_cov.add_argument("--coverage", default=DEFAULT_COVERAGE, help="coverage JSON path (default: %s)" % DEFAULT_COVERAGE)
    p_cov.add_argument("--research-dir", default=DEFAULT_RESEARCH_DIR, help="catalog directory (default: %s)" % DEFAULT_RESEARCH_DIR)
    p_cov.set_defaults(func=cmd_coverage)

    p_perm = sub.add_parser("permissions", help="Δ-RBAC: validate design/permissions.json and reconcile all 16 spec endpoint auth scopes")
    add_common(p_perm)
    p_perm.add_argument("--permissions", default=DEFAULT_PERMISSIONS, help="permissions catalogue path (default: %s)" % DEFAULT_PERMISSIONS)
    p_perm.add_argument("--specs-dir", default=DEFAULT_SPECS_DIR, help="feature-set spec directory (default: %s)" % DEFAULT_SPECS_DIR)
    p_perm.add_argument("--json", action="store_true", help="machine-readable summary JSON")
    p_perm.set_defaults(func=cmd_permissions)

    p_deploy = sub.add_parser("deploy-order", help="wave-by-wave runbook table")
    add_common(p_deploy)
    p_deploy.add_argument("--phase", type=int, default=None, help="include feature sets with phase <= N (default: all)")
    p_deploy.set_defaults(func=cmd_deploy_order)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except PlanError as exc:
        print("ERROR: %s" % exc, file=sys.stderr)
        return EXIT_INVALID
    except GateFailure as exc:
        print("GATE FAILED: %s" % exc, file=sys.stderr)
        return EXIT_GATE
    except BrokenPipeError:  # pragma: no cover - pipe handling
        return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
