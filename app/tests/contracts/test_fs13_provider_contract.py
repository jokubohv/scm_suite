"""FS-13 provider contract: `consigned.inventory@1.0.0` surface (API + events + migrations)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
CONTRACT_DIR = REPO_ROOT / "app" / "contracts" / "consigned"


def _json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _catalogue() -> set[str]:
    return {entry["scope"] for entry in _json(REPO_ROOT / "design" / "permissions.json")["catalogue"]}


def test_contract_metadata_and_migration() -> None:
    from app.contracts.consigned.contract import CONTRACT_NAME, CONTRACT_VERSION, EVENT_TYPES, JOB_NAMES

    assert (CONTRACT_NAME, CONTRACT_VERSION) == ("consigned.inventory", "1.0.0")
    assert len(EVENT_TYPES) == 11 and len(JOB_NAMES) == 4
    fs13 = next(feature_set for feature_set in _json(REPO_ROOT / "design" / "feature-sets.json")["feature_sets"] if feature_set["id"] == "FS-13")
    assert {entry["contract"]: entry["version"] for entry in fs13["provides"]} == {"consigned.inventory": "1.0.0"}
    assert fs13["migrations"] == ["alembic:0027_consigned"]
    migration = (REPO_ROOT / "app" / "migrations" / "versions" / "0027_consigned.py").read_text(encoding="utf-8")
    assert 'revision = "0027_consigned"' in migration and 'down_revision = "0026_quality"' in migration
    for needle in ("fk_landed_cost_vendor" if False else "guard_agreement_item", "consigned.consumption.reconcile", "CAG", "CTR"):
        assert needle in migration, needle
    from app.modules.consigned import tables as t

    assert len(t.ALL_TABLES) == 5 and len(t.metadata.tables) == 5


def test_fragment_is_implementation_backed_and_scope_complete() -> None:
    from app.api.routers.consigned import ROUTE_SCOPES

    fragment = _json(CONTRACT_DIR / "openapi.fragment.json")
    assert fragment["info"]["x-contract"] == "consigned.inventory" and fragment["info"]["x-feature-set"] == "FS-13"
    catalogue = _catalogue()
    operations: dict[tuple[str, str], dict] = {}
    for path, item in fragment["paths"].items():
        assert path.startswith("/api/v1/")
        for method, operation in item.items():
            operations[(method.upper(), path)] = operation
            assert operation["x-implemented"] is True and operation["x-skeleton"] is False
            scopes = operation["x-required-scopes"]
            assert len(scopes) == 1 and scopes[0] in catalogue
            assert "TODO" not in json.dumps(operation)
    assert set(operations) == set(ROUTE_SCOPES)
    assert len(operations) == 26
    for path in ("/api/v1/consignment-agreements", "/api/v1/consignment-transfers", "/api/v1/consignment-consumption", "/api/v1/consigned/balances", "/api/v1/consignment/eligibility", "/api/v1/consignment/settings"):
        assert path in fragment["paths"], path


def test_event_payload_schemas() -> None:
    from app.contracts.consigned.contract import EVENT_TYPES

    schemas = {path.stem.replace(".schema", ""): _json(path) for path in (CONTRACT_DIR / "events").glob("*.schema.json")}
    assert set(schemas) == set(EVENT_TYPES)
    envelope = {"event_id", "event_type", "event_version", "tenant_id", "occurred_at", "aggregate", "data"}
    for event, schema in schemas.items():
        assert envelope <= set(schema["required"])
        assert schema["x-event-type"] == event and schema["x-contract"] == "consigned.inventory"
        assert schema.get("x-todo") is not True and schema["x-payload-key-fields"]


def test_no_direct_ledger_or_costing_writes() -> None:
    module_dir = REPO_ROOT / "app" / "modules" / "consigned"
    forbidden = ("INSERT INTO inventory_ledger.", "UPDATE inventory_ledger.", "INSERT INTO costing.", "UPDATE costing.")
    offenders = []
    for path in module_dir.glob("*.py"):
        source = path.read_text(encoding="utf-8")
        for needle in forbidden:
            if needle in source:
                offenders.append(f"{path.name}: {needle}")
    assert offenders == [], offenders
    service = (module_dir / "service.py").read_text(encoding="utf-8")
    for posting in ("post_consigned_receipt", "post_consigned_ownership", "post_consigned_adjustment"):
        assert posting in service


def test_builder_artifacts_current() -> None:
    result = subprocess.run([sys.executable, str(CONTRACT_DIR / "build_from_app.py"), "--check"], cwd=REPO_ROOT, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr
