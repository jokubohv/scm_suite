"""Canonical fixture payloads are valid, internally consistent and in sync with the plan."""

from __future__ import annotations

import json
from pathlib import Path

from app.tests.utils.factories import FIXTURES_DIR, make_item, make_status, make_uom_pair

DESIGN_PERMISSIONS = Path(__file__).resolve().parents[4] / "design" / "permissions.json"


def _load(name: str) -> dict:
    return json.loads((FIXTURES_DIR / name).read_text(encoding="utf-8"))


def test_all_fixture_json_parses() -> None:
    files = sorted(path for path in FIXTURES_DIR.rglob("*.json"))
    assert files, "no fixture JSON files found"
    for path in files:
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload is not None, path


def test_permissions_fixture_is_byte_identical_to_design() -> None:
    assert DESIGN_PERMISSIONS.is_file(), "design/permissions.json missing; plan package is required"
    assert (FIXTURES_DIR / "permissions.json").read_bytes() == DESIGN_PERMISSIONS.read_bytes()


def test_permissions_fixture_counts() -> None:
    catalogue = _load("permissions.json")
    assert len(catalogue["catalogue"]) == 255
    assert sum(1 for row in catalogue["catalogue"] if row["assignable"]) == 241
    assert len(catalogue["role_templates"]) == 7
    assert len(catalogue["sod_pairs"]) == 14
    assert catalogue["plan_version"]


def test_uom_fixture_pairs_are_consistent() -> None:
    payload = _load("uom.json")
    pairs = payload["uom_pairs"]
    assert {pair["name"] for pair in pairs} == {"eaches", "weight"}
    for pair in pairs:
        base_units = [unit for unit in pair["units"] if unit["is_base"]]
        assert len(base_units) == 1
        assert base_units[0]["code"] == pair["base_uom_code"]
        assert base_units[0]["conversion_factor"] == "1"
    # factories consume the same fixture
    assert make_uom_pair("weight").unit("LB")["conversion_factor"] == "0.45359237"


def test_inventory_status_fixture_flags() -> None:
    records = _load("inventory_statuses.json")["records"]
    codes = [row["code"] for row in records]
    assert len(codes) == len(set(codes))
    defaults = [row for row in records if row["is_default"]]
    assert len(defaults) == 1 and defaults[0]["code"] == "AVAILABLE"
    for row in records:
        assert {"is_available_for_allocation", "is_committable", "is_sellable", "is_consigned"} <= set(row)
        if row["is_consigned"]:
            assert row["code"] == "CONSIGNED_GOOD"
    assert make_status("HOLD").is_committable is False


def test_item_fixture_covers_posting_eligibility_matrix() -> None:
    payload = _load("items.json")
    items = payload["items"]
    indexes = [item["index"] for item in items]
    assert indexes == sorted(indexes)
    assert len({item["item_number"] for item in items}) == len(items)
    types = {item["item_type_code"] for item in items}
    assert {"INVENTORY", "LOT_NUMBERED", "SERIALIZED", "ASSEMBLY", "WIP", "KIT", "SERVICE", "DROP_SHIP"} <= types
    uom_pairs = {pair["name"] for pair in _load("uom.json")["uom_pairs"]}
    for item in items:
        assert item["uom_pair"] in uom_pairs
        assert item["flags"]["is_stockable"] == (item["item_type_code"] not in {"KIT", "SERVICE", "DROP_SHIP"})
        assert item["flags"]["is_lot_numbered"] == (item["item_type_code"] == "LOT_NUMBERED")
        assert item["flags"]["is_serialized"] == (item["item_type_code"] == "SERIALIZED")
    # factories mirror the canonical samples' flags for the same type
    assert make_item(1, item_type_code="LOT_NUMBERED").flags["is_lot_numbered"] is True


def test_fixture_plan_version_matches_plan_package() -> None:
    manifest = json.loads(
        (Path(__file__).resolve().parents[4] / "design" / "feature-sets.json").read_text(encoding="utf-8")
    )
    for name in ("uom.json", "inventory_statuses.json", "items.json", "golden/workflow_journey.json"):
        assert _load(name)["plan_version"] == manifest["plan_version"], f"{name} plan_version drift"
