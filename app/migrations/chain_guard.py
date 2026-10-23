"""Migration chain guard — validates the single, linear, 4-digit-prefixed Alembic chain.

Run without a database (used by S0 exit checks and CI):

    python -m app.migrations.chain_guard
    python -m app.migrations.chain_guard --versions app/migrations/versions --json
    python -m app.migrations.chain_guard --manifest design/feature-sets.json --require-complete

What it proves (design/DATA-MODEL.md §21.1, Δ-PAR §6):

* every revision id is ``NNNN_slug`` with a 4-digit prefix (``0001_platform_core`` …);
* revision ids are unique and filename stem == revision id;
* exactly one base (``down_revision = None``), exactly one head, no branches/merges, no cycles;
* every non-base revision points at the immediately previous prefix (linear, no gaps);
* prefixes are contiguous from ``0001`` with no duplicates, in numeric order;
* when ``design/feature-sets.json`` is available, the manifest's migration list is itself valid and the
  on-disk chain is a prefix of it (``--require-complete`` demands equality — used after the final merge).

Exit codes: ``0`` valid, ``2`` invalid. Empty ``versions/`` is a valid S0 baseline (with a warning).
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

REVISION_PATTERN = re.compile(r"^(?P<prefix>\d{4})_(?P<slug>[a-z0-9_]+)$")
SKIP_FILES = {"__init__.py"}
DEFAULT_VERSIONS_DIR = Path(__file__).resolve().parent / "versions"
DEFAULT_MANIFEST = Path(__file__).resolve().parents[2] / "design" / "feature-sets.json"


@dataclass
class GuardResult:
    """Outcome of a chain validation run."""

    revisions: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def as_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "revisions": self.revisions,
            "errors": self.errors,
            "warnings": self.warnings,
        }


def _literal(node: ast.AST) -> object:
    """Safely evaluate an assignment/annotation value (constants, None, tuples/lists of strings)."""
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Tuple | ast.List):
        return tuple(_literal(element) for element in node.elts)
    raise ValueError("unsupported revision value")


def read_revision_file(path: Path) -> tuple[object, object]:
    """Return ``(revision, down_revision)`` values from one Alembic revision file."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: dict[str, object] = {}
    for node in tree.body:
        targets: list[str] = []
        value: ast.AST | None = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            targets = [node.targets[0].id]
            value = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value is not None:
            targets = [node.target.id]
            value = node.value
        for target in targets:
            if target in {"revision", "down_revision"}:
                found[target] = _literal(value) if value is not None else None
    if "revision" not in found:
        raise ValueError("missing module-level `revision`")
    if "down_revision" not in found:
        raise ValueError("missing module-level `down_revision`")
    return found["revision"], found["down_revision"]


def collect_revisions(versions_dir: Path) -> tuple[list[str], dict[str, object], GuardResult]:
    """Read revision files; return ordered ids, ``id -> down_revision`` map and partial errors."""
    result = GuardResult()
    ordered: list[str] = []
    downs: dict[str, object] = {}
    if not versions_dir.exists():
        result.errors.append(f"versions directory does not exist: {versions_dir}")
        return ordered, downs, result
    files = sorted(p for p in versions_dir.glob("*.py") if p.name not in SKIP_FILES)
    for path in files:
        try:
            revision, down = read_revision_file(path)
        except (SyntaxError, ValueError) as exc:
            result.errors.append(f"{path.name}: cannot parse revision ({exc})")
            continue
        rev = str(revision)
        if path.stem != rev:
            result.errors.append(f"{path.name}: filename stem {path.stem!r} != revision id {rev!r}")
        ordered.append(rev)
        downs[rev] = down
    return ordered, downs, result


def _check_manifest(path: Path, result: GuardResult) -> list[str]:
    """Validate the manifest migration list; return revision ids in numeric order."""
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        result.errors.append(f"manifest not readable: {path} ({exc})")
        return []
    revisions: list[str] = []
    seen: set[str] = set()
    for feature_set in manifest.get("feature_sets", []):
        for entry in feature_set.get("migrations", []):
            revision = entry.split(":", 1)[1] if ":" in entry else entry
            if revision in seen:
                result.errors.append(f"manifest lists duplicate revision {revision!r}")
                continue
            seen.add(revision)
            revisions.append(revision)
    return sorted(revisions, key=lambda rev: int(rev.split("_", 1)[0]) if rev[:4].isdigit() else 10**9)


def validate_chain(
    versions_dir: Path = DEFAULT_VERSIONS_DIR,
    expected: list[str] | None = None,
    *,
    require_complete: bool = False,
) -> GuardResult:
    """Validate one linear 4-digit chain; ``expected`` is an optional authoritative revision order."""
    ordered, downs, result = collect_revisions(versions_dir)
    result.revisions = list(ordered)

    if expected is not None:
        _validate_expected_sequence(expected, result)
        if ordered:
            if require_complete and ordered != expected:
                result.errors.append(
                    f"chain is incomplete: on disk {len(ordered)} revision(s), manifest expects {len(expected)}"
                )
            elif ordered != expected[: len(ordered)]:
                mismatch = next(
                    (i for i, (actual, want) in enumerate(zip(ordered, expected, strict=False)) if actual != want),
                    min(len(ordered), len(expected)),
                )
                actual = ordered[mismatch] if mismatch < len(ordered) else "<missing>"
                want = expected[mismatch] if mismatch < len(expected) else "<end of manifest>"
                result.errors.append(f"chain order mismatch at position {mismatch}: on disk {actual!r}, expected {want!r}")

    if not ordered:
        if not result.errors:
            result.warnings.append(
                "empty baseline: no revisions in versions/ (valid before FS-00 merges 0001–0003)"
            )
        return result

    # --- per-revision syntax + uniqueness ------------------------------------------------
    seen: set[str] = set()
    for revision in ordered:
        if not REVISION_PATTERN.match(revision):
            result.errors.append(f"revision {revision!r} must match NNNN_slug with a 4-digit prefix")
        if revision in seen:
            result.errors.append(f"duplicate revision id {revision!r}")
        seen.add(revision)

    # --- graph invariants ---------------------------------------------------------------
    bases = [rev for rev, down in downs.items() if down is None]
    if len(bases) != 1:
        result.errors.append(f"expected exactly one base (down_revision=None); found {sorted(bases)}")
    heads = [rev for rev in downs if rev not in {str(d) for d in downs.values() if d is not None}]
    if len(heads) != 1:
        result.errors.append(f"expected exactly one head; found {sorted(heads)}")
    child_count: dict[str, int] = {}
    for revision, down in downs.items():
        if down is None:
            continue
        if isinstance(down, tuple):
            result.errors.append(f"revision {revision!r} has {len(down)} down_revisions (merge/branch not allowed)")
            continue
        if not isinstance(down, str):
            result.errors.append(f"revision {revision!r} has invalid down_revision {down!r}")
            continue
        if down not in downs:
            result.errors.append(f"revision {revision!r} points at missing parent {down!r}")
            continue
        child_count[str(down)] = child_count.get(str(down), 0) + 1
    for parent, children in sorted(child_count.items()):
        if children > 1:
            result.errors.append(f"revision {parent!r} has {children} children (branch not allowed)")

    # --- prefix contiguity + immediate-previous-parent linear chain ----------------------
    numeric: list[tuple[int, str]] = []
    for revision in ordered:
        match = REVISION_PATTERN.match(revision)
        if match:
            numeric.append((int(match.group("prefix")), revision))
    numeric.sort()
    for offset, (number, revision) in enumerate(numeric, start=1):
        if number != offset:
            result.errors.append(f"revision prefixes are not contiguous from 0001: expected {offset:04d}, found {revision!r}")
            break
    for index in range(1, len(numeric)):
        previous = numeric[index - 1][1]
        current = numeric[index][1]
        if str(downs.get(current)) != previous:
            result.errors.append(f"revision {current!r} must point at its immediate predecessor {previous!r} (linear chain)")

    # --- reachability (guards cycles/hidden parents) ------------------------------------
    if bases:
        cursor: str | None = bases[0]
        visited: list[str] = []
        while cursor is not None:
            if cursor in visited:
                result.errors.append(f"cycle detected at revision {cursor!r}")
                break
            visited.append(cursor)
            child_revs = [rev for rev, down in downs.items() if down == cursor]
            cursor = child_revs[0] if len(child_revs) == 1 else None
        missing = sorted(set(downs) - set(visited))
        if missing:
            result.errors.append(f"revisions unreachable from the base: {missing}")

    return result


def _validate_expected_sequence(expected: list[str], result: GuardResult) -> None:
    """Validate the manifest revision list itself (4-digit, unique, contiguous)."""
    if not expected:
        result.warnings.append("manifest declares no migrations")
        return
    seen: set[str] = set()
    for revision in expected:
        if not REVISION_PATTERN.match(revision):
            result.errors.append(f"manifest revision {revision!r} must match NNNN_slug")
        if revision in seen:
            result.errors.append(f"manifest duplicate revision {revision!r}")
        seen.add(revision)
    prefixes = [int(rev.split("_", 1)[0]) for rev in expected if REVISION_PATTERN.match(rev)]
    for offset, prefix in enumerate(prefixes, start=1):
        if prefix != offset:
            result.errors.append(f"manifest revisions are not contiguous from 0001: expected {offset:04d}, got {prefix:04d}")
            break


def run_guard(
    versions_dir: Path,
    manifest_path: Path | None,
    *,
    require_complete: bool = False,
) -> GuardResult:
    """Convenience entry point used by tests and the CLI (manifest auto-detected when present)."""
    expected: list[str] | None = None
    if manifest_path is not None and manifest_path.exists():
        seed = GuardResult()
        expected = _check_manifest(manifest_path, seed)
        result = validate_chain(versions_dir, expected, require_complete=require_complete)
        result.errors[:0] = seed.errors
        return result
    return validate_chain(versions_dir, None, require_complete=require_complete)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.migrations.chain_guard",
        description="Validate the single linear Alembic chain (4-digit prefixes, no gaps/branches).",
    )
    parser.add_argument("--versions", type=Path, default=DEFAULT_VERSIONS_DIR, help="versions directory")
    parser.add_argument(
        "--manifest",
        type=Path,
        default=DEFAULT_MANIFEST,
        help="manifest with the authoritative migration list (default: design/feature-sets.json)",
    )
    parser.add_argument("--no-manifest", action="store_true", help="ignore the manifest and validate the chain only")
    parser.add_argument(
        "--require-complete",
        action="store_true",
        help="require the on-disk chain to equal the manifest (post-merge integration check)",
    )
    parser.add_argument("--json", action="store_true", dest="as_json", help="machine-readable output")
    args = parser.parse_args(argv)

    manifest = None if args.no_manifest else args.manifest
    result = run_guard(args.versions, manifest, require_complete=args.require_complete)

    if args.as_json:
        print(json.dumps(result.as_dict(), indent=2))
    else:
        print(f"chain guard: {args.versions}")
        print(f"  revisions: {len(result.revisions)} ({', '.join(result.revisions) or 'empty baseline'})")
        for warning in result.warnings:
            print(f"  WARN {warning}")
        for error in result.errors:
            print(f"  ERROR {error}")
        print("RESULT:", "OK" if result.ok else "INVALID")
    return 0 if result.ok else 2


if __name__ == "__main__":
    sys.exit(main())
