"""FS-11 provider contract: `wms.execution@1.0.0` surface (API + events + facade + migrations)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
CONTRACT_DIR = REPO_ROOT / "app" / "contracts" / "wms"


def _json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _manifest() -> dict:
    return _json(REPO_ROOT / "design" / "feature-sets.json")


def _catalogue_scopes() -> set[str]:
    return {entry["scope"] for entry in _json(REPO_ROOT / "design" / "permissions.json")["catalogue"]}


def test_contract_metadata_versions_and_migrations() -> None:
    from app.contracts.wms.contract import CONTRACT_NAME, WMS_CONTRACT_VERSION
    from app.modules.wms.facade import EVENT_TYPES, JOB_NAMES

    assert CONTRACT_NAME == "wms.execution"
    assert WMS_CONTRACT_VERSION == "1.0.0"
    fs11 = next(feature_set for feature_set in _manifest()["feature_sets"] if feature_set["id"] == "FS-11")
    provides = {entry["contract"]: entry["version"] for entry in fs11["provides"]}
    assert provides == {"wms.execution": "1.0.0"}
    assert fs11["migrations"] == [
        "alembic:0023_wms_setup",
        "alembic:0024_wms_inbound",
        "alembic:0025_wms_execution",
    ]
    assert len(EVENT_TYPES) == 10
    assert len(JOB_NAMES) == 8
    for job in (
        "wms.wave.release_schedule",
        "wms.putaway.assign",
        "wms.replenishment.generate",
        "wms.task.watchdog",
        "wms.shipment.manifest.reconcile",
        "wms.fulfillment.post_retry",
        "wms.device_session.purge",
        "wms.backorder.release",
    ):
        assert job in JOB_NAMES
    contract_meta = _json(CONTRACT_DIR / "contract.json")
    contracts = {entry["contract"]: entry for entry in contract_meta["contracts"]}
    assert contracts["wms.execution"]["version"] == "1.0.0"
    assert contracts["wms.execution"]["feature_set"] == "FS-11"
    assert contracts["wms.execution"]["fragment"] == "contracts/openapi/fragments/FS-11.openapi.json"
    registered = {entry["event"]: entry["version"] for entry in contracts["wms.execution"]["events"]}
    assert registered == {event: "1.0.0" for event in EVENT_TYPES}


def test_openapi_fragment_is_implementation_backed_and_scope_complete() -> None:
    from app.api.routers.wms import ROUTE_SCOPES

    fragment = _json(CONTRACT_DIR / "openapi.fragment.json")
    assert fragment["openapi"] == "3.1.0"
    assert fragment["info"]["x-contract"] == "wms.execution"
    assert fragment["info"]["x-feature-set"] == "FS-11"
    assert fragment["info"]["x-implemented-operations"] == sum(len(item) for item in fragment["paths"].values())
    catalogue = _catalogue_scopes()
    operations: dict[tuple[str, str], dict] = {}
    for path, item in fragment["paths"].items():
        assert path.startswith("/api/v1/"), path
        for method, operation in item.items():
            operations[(method.upper(), path)] = operation
            assert operation.get("x-implemented") is True and operation.get("x-skeleton") is False
            assert operation.get("x-contract") == ["wms.execution"]
            scopes = operation.get("x-required-scopes") or []
            assert len(scopes) == 1, (method, path, scopes)
            assert scopes[0] in catalogue, (method, path, scopes)
            assert scopes[0] == ROUTE_SCOPES[(method.upper(), path)]
            assert "TODO" not in json.dumps(operation)
            assert any(code in operation["responses"] for code in ("200", "201", "204"))
    assert set(operations) == set(ROUTE_SCOPES)
    assert len(operations) >= 98, len(operations)


def test_fragment_covers_the_frozen_fs11_contract_surface() -> None:
    fragment = _json(CONTRACT_DIR / "openapi.fragment.json")
    frozen = _json(REPO_ROOT / "contracts" / "openapi" / "fragments" / "FS-11.openapi.json")
    implemented = {(method.upper(), path) for path, item in fragment["paths"].items() for method in item}
    expected = {(method.upper(), path) for path, item in frozen["paths"].items() for method in item}
    assert implemented == expected, sorted(implemented ^ expected)
    # Material lifecycle actions required by FS-11 §6 (DATA-MODEL §11/§18 bridges).
    required = {
        ("POST", "/api/v1/inbound-shipments/{id}/arrive"),
        ("POST", "/api/v1/receiving-orders/{id}/complete"),
        ("POST", "/api/v1/receiving-orders/{id}/reverse-receipt"),
        ("POST", "/api/v1/putaway-tasks/generate"),
        ("POST", "/api/v1/putaway-tasks/{id}/complete"),
        ("POST", "/api/v1/waves/{id}/release"),
        ("POST", "/api/v1/pick-tasks/{id}/complete"),
        ("POST", "/api/v1/pick-tasks/{id}/reverse"),
        ("POST", "/api/v1/shipments/{id}/ship"),
        ("POST", "/api/v1/ship-manifests/{id}/send"),
        ("POST", "/api/v1/replenishment-plans/{id}/approve"),
        ("POST", "/api/v1/status-change-tasks/{id}/complete"),
        ("POST", "/api/v1/warehouses/{id}/cycle-count-requests"),
        ("PUT", "/api/v1/warehouses/{id}/item-bin-assignments"),
    }
    assert required <= implemented


def test_event_payload_schemas_are_final_envelope_and_fields() -> None:
    from app.modules.wms.facade import EVENT_TYPES

    events_dir = CONTRACT_DIR / "events"
    schemas = {path.stem.replace(".schema", ""): _json(path) for path in events_dir.glob("*.schema.json")}
    assert set(schemas) == set(EVENT_TYPES), sorted(set(schemas) ^ set(EVENT_TYPES))
    envelope_required = {"event_id", "event_type", "event_version", "tenant_id", "occurred_at", "aggregate", "data"}
    for event, schema in schemas.items():
        assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
        assert envelope_required <= set(schema["required"])
        assert "data" in schema["properties"]
        assert schema["x-event-type"] == event and schema["x-event-version"] == "1.0.0"
        assert schema["x-contract"] == "wms.execution" and schema["x-feature-set"] == "FS-11"
        assert schema.get("x-todo") is not True
        assert schema["x-payload-key-fields"]


def test_in_process_facade_surface() -> None:
    from app.contracts.wms.contract import WmsFacade

    expected = {
        "get_warehouse", "list_warehouses", "get_task", "list_tasks", "get_shipment",
        "list_shipments", "get_receiving_order", "order_release_status",
    }
    for name in expected:
        assert callable(getattr(WmsFacade, name, None)), name


def test_migrations_metadata_and_chain() -> None:
    from app.modules.wms import tables as t

    assert len(t.ALL_TABLES) == 38
    assert (len(t.REVISION_0023_TABLES), len(t.REVISION_0024_TABLES), len(t.REVISION_0025_TABLES)) == (15, 6, 17)
    assert len(t.metadata.tables) == 38
    versions = REPO_ROOT / "app" / "migrations" / "versions"
    migration_0023 = (versions / "0023_wms_setup.py").read_text(encoding="utf-8")
    migration_0024 = (versions / "0024_wms_inbound.py").read_text(encoding="utf-8")
    migration_0025 = (versions / "0025_wms_execution.py").read_text(encoding="utf-8")
    assert 'revision = "0023_wms_setup"' in migration_0023
    assert 'down_revision = "0022_planning_atp"' in migration_0023
    assert 'down_revision = "0023_wms_setup"' in migration_0024
    assert 'down_revision = "0024_wms_inbound"' in migration_0025
    assert "seeds_0023" in migration_0023
    migration_utils = (REPO_ROOT / "app" / "modules" / "wms" / "migration_utils.py").read_text(encoding="utf-8")
    assert "FLAG_KEY" in migration_utils and "wms.execution.enabled" in migration_utils
    assert 'SETTINGS_NAMESPACE = "inventory"' in migration_utils and "wms." in migration_utils


def test_no_direct_ledger_or_costing_writes_in_wms() -> None:
    module_dir = REPO_ROOT / "app" / "modules" / "wms"
    forbidden = (
        "INSERT INTO inventory_ledger.",
        "UPDATE inventory_ledger.",
        "INSERT INTO costing.",
        "UPDATE costing.",
        "INSERT INTO procurement.item_receipt",
    )
    offenders: list[str] = []
    for path in module_dir.glob("*.py"):
        source = path.read_text(encoding="utf-8")
        for needle in forbidden:
            if needle in source:
                offenders.append(f"{path.name}: {needle}")
    assert offenders == [], offenders
    service = (module_dir / "service.py").read_text(encoding="utf-8")
    for posting in (
        "post_bin_transfer",
        "post_status_change",
        "post_fulfillment",
        "create_receipt",
        "post_receipt",
        "reverse_receipt",
    ):
        assert posting in service, posting
    assert "PostingPort" in service and "CountsPort" in service


def test_builder_artifacts_are_current() -> None:
    result = subprocess.run(
        [sys.executable, str(CONTRACT_DIR / "build_from_app.py"), "--check"],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=180,
    )
    assert result.returncode == 0, result.stdout + result.stderr
