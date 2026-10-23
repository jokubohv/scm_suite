"""Seed CLI behaviour (dry-run, state persistence, idempotent second run, validation)."""

from __future__ import annotations

import json
from pathlib import Path

from app.seed.cli import main
from app.seed.constants import SYSTEM_TENANT_ID

TENANT = "33333333-3333-7333-8333-333333333333"


def _json_stdout(capsys) -> dict:
    captured = capsys.readouterr()
    return json.loads(captured.out)


def test_validate_only_exits_zero(capsys) -> None:
    assert main(["--validate-only"]) == 0
    assert "valid" in capsys.readouterr().out


def test_dry_run_does_not_write_state(tmp_path: Path, capsys) -> None:
    state = tmp_path / "state.json"
    assert main(["--tenant", TENANT, "--dry-run", "--state", str(state), "--json"]) == 0
    report = _json_stdout(capsys)
    assert report["dry_run"] is True
    assert report["totals"]["inserted"] > 0
    assert report["digest_unchanged"] is False
    assert not state.exists()


def test_two_cli_runs_are_idempotent_through_state_file(tmp_path: Path, capsys) -> None:
    state = tmp_path / "state.json"
    assert main(["--tenant", TENANT, "--state", str(state), "--json"]) == 0
    first = _json_stdout(capsys)
    assert state.is_file()
    assert first["totals"]["inserted"] == 1076
    assert first["totals"]["updated"] == 0

    assert main(["--tenant", TENANT, "--state", str(state), "--json"]) == 0
    second = _json_stdout(capsys)
    assert second["totals"]["inserted"] == 0
    assert second["totals"]["updated"] == 0
    assert second["totals"]["unchanged"] == 1076
    assert second["digest_before"] == second["digest_after"] == first["digest_after"]
    assert second["digest_unchanged"] is True


def test_report_file_written_on_real_run(tmp_path: Path) -> None:
    state = tmp_path / "state.json"
    report_file = tmp_path / "report.json"
    assert main(["--tenant", TENANT, "--state", str(state), "--report-file", str(report_file), "--quiet"]) == 0
    report = json.loads(report_file.read_text())
    assert report["valid"] is True
    assert report["tenant_id"] == TENANT


def test_system_tenant_only_loads_global_datasets(capsys) -> None:
    assert main(["--tenant", str(SYSTEM_TENANT_ID), "--profile", "memory", "--json"]) == 0
    report = _json_stdout(capsys)
    assert {dataset["name"] for dataset in report["datasets"]} == {
        "system_tenant",
        "currencies",
        "transaction_types",
    }


def test_invalid_tenant_uuid_exits_two(capsys) -> None:
    assert main(["--tenant", "not-a-uuid"]) == 2
    assert "invalid tenant" in capsys.readouterr().err


def test_sqlalchemy_profile_unreachable_database_reports_clean_error(capsys) -> None:
    assert (
        main(
            [
                "--tenant",
                TENANT,
                "--profile",
                "sqlalchemy",
                "--database-url",
                "postgresql+asyncpg://nobody:nobody@127.0.0.1:9/unreachable",
            ]
        )
        == 2
    )
    assert "unavailable" in capsys.readouterr().err


def test_no_system_skips_global_datasets(capsys) -> None:
    assert main(["--tenant", TENANT, "--profile", "memory", "--no-system", "--json"]) == 0
    report = _json_stdout(capsys)
    names = {dataset["name"] for dataset in report["datasets"]}
    assert "currencies" not in names
    assert "permissions" in names


def test_symbolic_system_tenant(capsys) -> None:
    assert main(["--tenant", "system", "--profile", "memory", "--json"]) == 0
    report = _json_stdout(capsys)
    assert report["tenant_id"] == str(SYSTEM_TENANT_ID)
    assert {dataset["name"] for dataset in report["datasets"]} == {
        "system_tenant",
        "currencies",
        "transaction_types",
    }


def test_no_tenant_defaults_to_system_only(capsys) -> None:
    assert main(["--profile", "memory", "--json"]) == 0
    report = _json_stdout(capsys)
    assert report["tenant_id"] == str(SYSTEM_TENANT_ID)
    assert len(report["datasets"]) == 3


def test_seed_tenant_env_var_selects_tenant(monkeypatch, capsys) -> None:
    monkeypatch.setenv("SEED_TENANT_ID", TENANT)
    assert main(["--dry-run", "--profile", "memory", "--json"]) == 0
    report = _json_stdout(capsys)
    assert report["tenant_id"] == TENANT
    assert len(report["datasets"]) == 13


def test_invalid_symbolic_tenant_exits_two(capsys) -> None:
    assert main(["--tenant", "not-a-tenant"]) == 2
    assert "invalid tenant" in capsys.readouterr().err


def test_python_module_entrypoint_runs() -> None:
    import subprocess
    import sys
    from pathlib import Path as _Path

    repo_root = _Path(__file__).resolve().parents[3]
    result = subprocess.run(
        [sys.executable, "-m", "app.seed", "--dry-run", "--json"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["valid"] is True
    assert report["dry_run"] is True


def test_json_report_carries_repository_metadata(capsys) -> None:
    assert main(["--tenant", TENANT, "--profile", "memory", "--dry-run", "--json"]) == 0
    report = _json_stdout(capsys)
    assert report["repository"] == {"profile": "memory", "deferred_datasets": []}
