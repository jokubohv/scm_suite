"""Golden-file helpers: load, tolerant compare, update mode, canonical journey skeleton."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.tests.utils.golden import (
    GOLDEN_DIR,
    GoldenFileError,
    assert_matches_golden,
    golden_path,
    load_golden,
    write_golden,
)


def test_golden_dir_is_fixture_dir() -> None:
    assert GOLDEN_DIR.name == "golden"
    assert GOLDEN_DIR.parent.name == "fixtures"


def test_write_load_compare_round_trip(tmp_path: Path) -> None:
    payload = {"qty": "10.000000000000", "lines": [{"value": "25.0"}], "ok": True}
    path = write_golden("demo", payload, base_dir=tmp_path)
    assert path == tmp_path / "demo.json"
    assert load_golden("demo", base_dir=tmp_path) == payload
    assert_matches_golden("demo", payload, base_dir=tmp_path)


def test_compare_is_decimal_tolerant(tmp_path: Path) -> None:
    write_golden("numbers", {"qty": "10.000000000000"}, base_dir=tmp_path)
    assert_matches_golden("numbers", {"qty": 10}, base_dir=tmp_path)
    assert_matches_golden("numbers", {"qty": "10.000000000001"}, base_dir=tmp_path)
    with pytest.raises(GoldenFileError):
        assert_matches_golden("numbers", {"qty": "10.5"}, base_dir=tmp_path)


def test_compare_reports_key_and_length_differences(tmp_path: Path) -> None:
    write_golden("shape", {"a": 1, "b": [1, 2]}, base_dir=tmp_path)
    with pytest.raises(GoldenFileError, match="key mismatch"):
        assert_matches_golden("shape", {"a": 1, "c": [1, 2]}, base_dir=tmp_path)
    with pytest.raises(GoldenFileError, match="list length"):
        assert_matches_golden("shape", {"a": 1, "b": [1]}, base_dir=tmp_path)


def test_update_mode_rewrites_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_golden("update", {"value": 1}, base_dir=tmp_path)
    monkeypatch.setenv("UPDATE_GOLDEN", "1")
    assert_matches_golden("update", {"value": 2}, base_dir=tmp_path)
    assert load_golden("update", base_dir=tmp_path) == {"value": 2}


def test_missing_golden_fixture_raises(tmp_path: Path) -> None:
    with pytest.raises(GoldenFileError, match="not found"):
        load_golden("missing", base_dir=tmp_path)
    with pytest.raises(GoldenFileError):
        assert_matches_golden("missing", {}, base_dir=tmp_path)


def test_canonical_workflow_journey_fixture() -> None:
    journey = load_golden("workflow_journey")
    assert journey["status"] == "placeholder"
    assert journey["plan_version"]
    assert len(journey["steps"]) == 5
    assert [step["posting_type"] for step in journey["steps"]] == ["T-01", "T-10", "T-11", "T-20", "T-12"]
    for step in journey["steps"]:
        assert step["expected"]["placeholder"] is True
        assert step["expected"]["invariants"]
        for key in ("qty_effect", "value_effect"):
            if key in step["expected"]:
                # decimal strings with 12 dp
                assert step["expected"][key].split(".")[-1].isdigit()
                assert len(step["expected"][key].split(".")[-1]) == 12
    final = journey["final_expectations"]
    assert final["value_reconciles"] is True
    assert final["ledger_append_only"] is True


def test_golden_json_is_sorted_and_readable() -> None:
    raw = json.dumps(load_golden("workflow_journey"), sort_keys=True, indent=2)
    on_disk = (GOLDEN_DIR / "workflow_journey.json").read_text(encoding="utf-8")
    assert json.loads(on_disk) == json.loads(raw)


def test_golden_path_normalises_extension(tmp_path: Path) -> None:
    assert golden_path("x", base_dir=tmp_path) == tmp_path / "x.json"
    assert golden_path("x.json", base_dir=tmp_path) == tmp_path / "x.json"
