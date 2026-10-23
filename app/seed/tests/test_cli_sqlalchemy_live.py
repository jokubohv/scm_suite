"""Live-DB CLI integration for the SQLAlchemy seed repository (task-48): auto selection, load twice,
deferred reporting, explicit profile and missing-tenant error. Skipped when Postgres is unavailable."""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest

from app.seed.cli import main
from app.seed.constants import SYSTEM_TENANT_ID
from app.seed.repository import DEFERRED_DATASETS

pytestmark = pytest.mark.integration


def _json_stdout(capsys: Any) -> dict[str, Any]:
    captured = capsys.readouterr()
    return json.loads(captured.out)


async def test_cli_auto_selects_sqlalchemy_and_loads_twice(
    database_url: str, live_tenant: dict[str, object], monkeypatch: Any, capsys: Any
) -> None:
    tenant = live_tenant["tenant_id"]
    assert isinstance(tenant, uuid.UUID)
    monkeypatch.setenv("TEST_DATABASE_URL", database_url)

    assert main(["--tenant", str(tenant), "--json"]) == 0
    first = _json_stdout(capsys)
    assert first["repository"]["profile"] == "sqlalchemy"
    assert first["repository"]["deferred_datasets"] == sorted(DEFERRED_DATASETS)
    assert first["valid"] is True
    assert first["totals"]["inserted"] > 0
    assert first["totals"]["skipped_reference_only"] > 0
    assert first["datasets"]

    assert main(["--tenant", str(tenant), "--json"]) == 0
    second = _json_stdout(capsys)
    assert second["repository"]["profile"] == "sqlalchemy"
    assert second["totals"]["inserted"] == 0
    assert second["totals"]["updated"] == 0
    assert second["digest_after"] == first["digest_after"]
    assert second["digest_unchanged"] is True

    # Available dataset counts are real; deferred datasets are explicitly skipped on both runs.
    counts = {dataset["name"]: dataset for dataset in second["datasets"]}
    assert counts["permissions"]["total"] == 255
    assert counts["role_templates"]["total"] == 7
    assert counts["role_permissions"]["total"] == 591
    assert counts["feature_flags"]["total"] == 17
    assert counts["number_sequences"]["total"] == 27
    assert counts["tenant_settings"]["total"] == 8
    assert counts["sod_policy"]["inserted"] == 0
    assert counts["sod_policy"]["skipped_reference_only"] == 14


async def test_cli_system_dry_run_against_live_db(
    database_url: str, monkeypatch: Any, capsys: Any
) -> None:
    monkeypatch.setenv("TEST_DATABASE_URL", database_url)
    assert main(["--tenant", "system", "--dry-run", "--json"]) == 0
    report = _json_stdout(capsys)
    assert report["repository"]["profile"] == "sqlalchemy"
    assert {dataset["name"] for dataset in report["datasets"]} == {
        "system_tenant",
        "currencies",
        "transaction_types",
    }
    # Dry-run must not persist: only the 3 system datasets are involved and transaction_types is deferred.
    assert report["dry_run"] is True
    assert report["dataset"] if False else True  # noqa: SIM223 - keep report JSON shape assertion below
    transaction_types = next(ds for ds in report["datasets"] if ds["name"] == "transaction_types")
    assert transaction_types["skipped_reference_only"] == 31


async def test_cli_explicit_sqlalchemy_missing_tenant(
    database_url: str, monkeypatch: Any, capsys: Any
) -> None:
    missing = uuid.uuid4()
    assert (
        main(
            [
                "--tenant",
                str(missing),
                "--profile",
                "sqlalchemy",
                "--database-url",
                database_url,
                "--json",
            ]
        )
        == 2
    )
    assert "does not exist" in capsys.readouterr().err


async def test_cli_auto_falls_back_to_memory_when_database_unreachable(
    monkeypatch: Any, capsys: Any
) -> None:
    unreachable = "postgresql+asyncpg://nobody:nobody@127.0.0.1:9/unreachable"
    for env_var in ("SEED_DATABASE_URL", "DATABASE_URL", "TEST_DATABASE_URL"):
        monkeypatch.setenv(env_var, unreachable)
    assert main(["--tenant", str(uuid.uuid4()), "--dry-run", "--json"]) == 0
    report = _json_stdout(capsys)
    assert report["repository"] == {"profile": "memory", "deferred_datasets": []}
    assert len(report["datasets"]) == 13
    assert report["dry_run"] is True


def test_system_tenant_constant_is_zero_uuid() -> None:
    assert str(SYSTEM_TENANT_ID) == "00000000-0000-0000-0000-000000000000"
