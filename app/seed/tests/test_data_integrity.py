"""Seed data payload integrity (manifest, UoM, statuses, transaction types, flags, sequences)."""

from __future__ import annotations

from app.seed.constants import (
    DEFAULT_DATA_DIR,
    DOCUMENTED_INVENTORY_STATUSES,
    DOCUMENTED_NUMBER_SEQUENCES,
    DOCUMENTED_TRANSACTION_TYPES,
    EXPECTED_DATASET_COUNT,
)
from app.seed.loader import SeedLoader, validate_sources
from app.seed.repository import MemoryRepository


def _load(name: str) -> dict:
    import json

    return json.loads((DEFAULT_DATA_DIR / name).read_text(encoding="utf-8"))


def test_validate_sources_reports_no_errors() -> None:
    assert validate_sources() == []


def test_manifest_has_expected_datasets() -> None:
    manifest = _load("manifest.json")
    assert len(manifest["datasets"]) == EXPECTED_DATASET_COUNT
    names = [dataset["name"] for dataset in manifest["datasets"]]
    assert len(set(names)) == len(names)
    assert manifest["plan_version"]
    assert manifest["schema_version"] == "1.0.0"


def test_transaction_types_are_contiguous_t01_t31() -> None:
    records = _load("transaction_types.json")["records"]
    codes = [row["code"] for row in records]
    assert tuple(sorted(codes, key=lambda code: int(code.split("-")[1]))) == DOCUMENTED_TRANSACTION_TYPES
    for row in records:
        assert row["movement_type"]
        assert row["qty_effect"] in {"IN", "OUT", "IN_OUT", "MOVE", "NONE"}
        assert isinstance(row["value_only"], bool)
        assert isinstance(row["events"], list)


def test_currencies_are_unique_and_have_minor_units() -> None:
    records = _load("currencies.json")["records"]
    codes = [row["code"] for row in records]
    assert len(codes) == len(set(codes))
    assert {"USD", "EUR", "JPY", "GBP"}.issubset(codes)
    for row in records:
        assert len(row["code"]) == 3
        assert 0 <= row["minor_units"] <= 4
        assert 0 < row["numeric_code"] < 1000
    assert next(row for row in records if row["code"] == "JPY")["minor_units"] == 0
    assert next(row for row in records if row["code"] == "USD")["minor_units"] == 2


def test_uom_types_and_units_are_consistent() -> None:
    payload = _load("uom.json")["records"]
    types = {row["code"]: row for row in payload["uom_types"]}
    assert types
    for uom_type in types.values():
        assert any(unit["uom_type_code"] == uom_type["code"] and unit["is_base"] for unit in payload["uom_units"])
    for unit in payload["uom_units"]:
        assert unit["uom_type_code"] in types
        if unit["is_base"]:
            assert unit["conversion_factor"] == "1"
            assert types[unit["uom_type_code"]]["base_uom_code"] == unit["code"]
        else:
            assert float(unit["conversion_factor"]) > 0


def test_inventory_statuses_include_documented_set_and_exactly_one_default() -> None:
    records = _load("inventory_statuses.json")["records"]
    codes = {row["code"] for row in records}
    assert codes == set(DOCUMENTED_INVENTORY_STATUSES)
    defaults = [row for row in records if row["is_default"]]
    assert len(defaults) == 1 and defaults[0]["code"] == "AVAILABLE"
    consigned = [row for row in records if row["is_consigned"]]
    assert [row["code"] for row in consigned] == ["CONSIGNED_GOOD"]


def test_feature_flags_are_enablement_flags_default_off() -> None:
    records = _load("feature_flags.json")["records"]
    assert len(records) == len({row["flag_key"] for row in records})
    assert all(row["flag_key"].endswith(".enabled") for row in records)
    assert all(row["default_enabled"] is False for row in records)
    assert "platform.core.enabled" in {row["flag_key"] for row in records}


def test_number_sequences_match_documented_templates() -> None:
    records = _load("number_sequences.json")["records"]
    doc_types = [row["doc_type"] for row in records]
    assert tuple(sorted(doc_types)) == tuple(sorted(DOCUMENTED_NUMBER_SEQUENCES))
    for row in records:
        assert row["padding"] >= 1
        assert row["reset_policy"] in {"NEVER", "ANNUAL", "MONTHLY"}
        assert row["gap_policy"] in {"NON_GAPLESS", "GAPLESS"}


def test_loader_exposes_all_datasets_for_a_probe_tenant() -> None:
    loader = SeedLoader(MemoryRepository(), dry_run=True)
    report = loader.load("00000000-0000-7000-8000-000000000099")
    assert report.valid
    assert len(report.datasets) == EXPECTED_DATASET_COUNT
    assert report.inserted + report.updated == report.total
