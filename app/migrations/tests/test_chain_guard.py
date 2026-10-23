"""Tests for the Alembic chain guard (database-free; run with ``pytest app/migrations/tests -q``)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from app.migrations.chain_guard import validate_chain

REPO_ROOT = Path(__file__).resolve().parents[3]


def _write_revision(
    versions: Path,
    revision: str,
    down_revision: str | None,
    *,
    filename: str | None = None,
    body: str = "",
) -> Path:
    path = versions / (filename or f"{revision}.py")
    path.write_text(
        "from __future__ import annotations\n"
        f'revision = "{revision}"\n'
        f"down_revision = {down_revision!r}\n"
        "branch_labels = None\n"
        "depends_on = None\n\n"
        f"{body}",
        encoding="utf-8",
    )
    return path


@pytest.fixture()
def versions(tmp_path: Path) -> Path:
    path = tmp_path / "versions"
    path.mkdir()
    return path


def test_empty_baseline_is_valid(versions: Path) -> None:
    result = validate_chain(versions)
    assert result.ok
    assert result.revisions == []
    assert any("empty baseline" in warning for warning in result.warnings)


def test_valid_linear_chain(versions: Path) -> None:
    _write_revision(versions, "0001_platform_core", None)
    _write_revision(versions, "0002_platform_security", "0001_platform_core")
    _write_revision(versions, "0003_platform_events", "0002_platform_security")

    result = validate_chain(versions)
    assert result.ok, result.errors
    assert result.revisions == ["0001_platform_core", "0002_platform_security", "0003_platform_events"]


def test_bad_prefix_rejected(versions: Path) -> None:
    _write_revision(versions, "1_platform_core", None)
    result = validate_chain(versions)
    assert not result.ok
    assert any("4-digit prefix" in error for error in result.errors)


def test_filename_must_match_revision(versions: Path) -> None:
    _write_revision(versions, "0001_platform_core", None, filename="0001_platform_core_extra.py")
    result = validate_chain(versions)
    assert not result.ok
    assert any("filename stem" in error for error in result.errors)


def test_gap_in_prefixes_rejected(versions: Path) -> None:
    _write_revision(versions, "0001_platform_core", None)
    _write_revision(versions, "0003_platform_events", "0001_platform_core")
    result = validate_chain(versions)
    assert not result.ok
    assert any("not contiguous" in error or "immediate predecessor" in error for error in result.errors)


def test_non_linear_parent_rejected(versions: Path) -> None:
    _write_revision(versions, "0001_platform_core", None)
    _write_revision(versions, "0002_platform_security", "0001_platform_core")
    _write_revision(versions, "0003_platform_events", "0001_platform_core")
    result = validate_chain(versions)
    assert not result.ok
    assert any("branch" in error or "children" in error or "immediate predecessor" in error for error in result.errors)


def test_merge_down_revision_tuple_rejected(versions: Path) -> None:
    _write_revision(versions, "0001_platform_core", None)
    _write_revision(versions, "0002_platform_security", "0001_platform_core")
    merge = versions / "0003_platform_events.py"
    merge.write_text(
        'revision = "0003_platform_events"\n'
        'down_revision = ("0001_platform_core", "0002_platform_security")\n',
        encoding="utf-8",
    )
    result = validate_chain(versions)
    assert not result.ok
    assert any("merge/branch" in error for error in result.errors)


def test_missing_parent_rejected(versions: Path) -> None:
    _write_revision(versions, "0001_platform_core", None)
    _write_revision(versions, "0002_platform_security", "0000_missing")
    result = validate_chain(versions)
    assert not result.ok
    assert any("missing parent" in error for error in result.errors)


def test_duplicate_revision_rejected(versions: Path) -> None:
    _write_revision(versions, "0001_platform_core", None)
    _write_revision(versions, "0001_platform_core", None, filename="0001_platform_core_copy.py")
    result = validate_chain(versions)
    assert not result.ok
    assert any("duplicate revision" in error for error in result.errors)


def _manifest(tmp_path: Path, revisions: list[str]) -> Path:
    path = tmp_path / "manifest.json"
    path.write_text(
        json.dumps({"feature_sets": [{"id": "FS-00", "migrations": [f"alembic:{r}" for r in revisions]}]}),
        encoding="utf-8",
    )
    return path


def test_chain_must_be_prefix_of_manifest(versions: Path, tmp_path: Path) -> None:
    manifest = _manifest(tmp_path, ["0001_platform_core", "0002_platform_security", "0003_platform_events"])
    _write_revision(versions, "0001_platform_core", None)

    from app.migrations.chain_guard import run_guard

    assert run_guard(versions, manifest).ok
    complete = run_guard(versions, manifest, require_complete=True)
    assert not complete.ok
    assert any("incomplete" in error for error in complete.errors)


def test_manifest_with_gap_rejected(versions: Path, tmp_path: Path) -> None:
    manifest = _manifest(tmp_path, ["0001_platform_core", "0003_platform_events"])
    from app.migrations.chain_guard import run_guard

    result = run_guard(versions, manifest)
    assert not result.ok
    assert any("manifest revisions are not contiguous" in error for error in result.errors)


def test_cli_exit_codes(tmp_path: Path) -> None:
    versions = tmp_path / "versions"
    versions.mkdir()
    env = {"PYTHONPATH": str(REPO_ROOT), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"}
    base_cmd = [sys.executable, "-m", "app.migrations.chain_guard", "--versions", str(versions), "--no-manifest"]

    ok = subprocess.run(base_cmd, cwd=REPO_ROOT, env=env, capture_output=True, text=True, check=False)
    assert ok.returncode == 0, ok.stderr
    assert "RESULT: OK" in ok.stdout

    _write_revision(versions, "0001_platform_core", None)
    _write_revision(versions, "0003_platform_events", "0001_platform_core")
    bad = subprocess.run(base_cmd, cwd=REPO_ROOT, env=env, capture_output=True, text=True, check=False)
    assert bad.returncode == 2
    assert "RESULT: INVALID" in bad.stdout
