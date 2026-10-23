"""FS-08 provider contract: `procurement.p2p@1.0.0` surface (API + events + facade + migrations)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
CONTRACT_DIR = REPO_ROOT / "app" / "contracts" / "procurement"


def _json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _manifest() -> dict:
    return _json(REPO_ROOT / "design" / "feature-sets.json")


def _catalogue_scopes() -> set[str]:
    return {entry["scope"] for entry in _json(REPO_ROOT / "design" / "permissions.json")["catalogue"]}


def test_contract_metadata_versions_and_migrations() -> None:
    from app.contracts.procurement.contract import CONTRACT_NAME, PROCUREMENT_CONTRACT_VERSION
    from app.modules.procurement.facade import EVENT_TYPES, JOB_NAMES

    assert CONTRACT_NAME == "procurement.p2p"
    assert PROCUREMENT_CONTRACT_VERSION == "1.0.0"
    fs08 = next(feature_set for feature_set in _manifest()["feature_sets"] if feature_set["id"] == "FS-08")
    provides = {entry["contract"]: entry["version"] for entry in fs08["provides"]}
    assert provides == {"procurement.p2p": "1.0.0"}
    assert fs08["migrations"] == [
        "alembic:0015_procurement_vendors",
        "alembic:0016_procurement_ordering",
        "alembic:0017_procurement_billing",
    ]
    assert len(EVENT_TYPES) == 15
    assert len(JOB_NAMES) == 4
    for job in ("procurement.match.run", "procurement.po.close_overdue", "procurement.receipt.reconcile", "procurement.partition.maintenance"):
        assert job in JOB_NAMES
    contract_meta = _json(CONTRACT_DIR / "contract.json")
    contracts = {entry["contract"]: entry for entry in contract_meta["contracts"]}
    assert contracts["procurement.p2p"]["version"] == "1.0.0"
    assert contracts["procurement.p2p"]["feature_set"] == "FS-08"
    assert contracts["procurement.p2p"]["fragment"] == "contracts/openapi/fragments/FS-08.openapi.json"


def test_openapi_fragment_is_implementation_backed_and_scope_complete() -> None:
    from app.api.routers.procurement import ROUTE_SCOPES

    fragment = _json(CONTRACT_DIR / "openapi.fragment.json")
    assert fragment["openapi"] == "3.1.0"
    assert fragment["info"]["x-contract"] == "procurement.p2p"
    assert fragment["info"]["x-feature-set"] == "FS-08"
    assert fragment["info"]["x-implemented-operations"] == sum(len(item) for item in fragment["paths"].values())
    catalogue = _catalogue_scopes()
    operations: dict[tuple[str, str], dict] = {}
    for path, item in fragment["paths"].items():
        assert path.startswith("/api/v1/"), path
        for method, operation in item.items():
            operations[(method.upper(), path)] = operation
            assert operation.get("x-implemented") is True and operation.get("x-skeleton") is False
            assert operation.get("x-contract") == ["procurement.p2p"]
            scopes = operation.get("x-required-scopes") or []
            assert len(scopes) == 1, (method, path, scopes)
            assert scopes[0] in catalogue, (method, path, scopes)
            assert scopes[0] in ROUTE_SCOPES[(method.upper(), path)]
            assert "TODO" not in json.dumps(operation)
            assert any(code in operation["responses"] for code in ("200", "201", "204"))
    assert set(operations) == set(ROUTE_SCOPES)
    assert len(operations) >= 80, len(operations)


def test_fs08_fragment_covers_all_manifest_spec_operations_or_adds_implemented_ones() -> None:
    fragment = _json(CONTRACT_DIR / "openapi.fragment.json")
    paths = {path: set(item) for path, item in fragment["paths"].items()}
    required = {
        "/api/v1/vendors": {"get", "post"},
        "/api/v1/vendor-categories": {"get", "post"},
        "/api/v1/item-vendors": {"get", "post"},
        "/api/v1/vendor-contracts": {"get", "post"},
        "/api/v1/purchase-requisitions": {"get", "post"},
        "/api/v1/purchase-requests": {"get", "post"},
        "/api/v1/rfqs": {"get", "post"},
        "/api/v1/purchase-orders": {"get", "post"},
        "/api/v1/item-receipts": {"get", "post"},
        "/api/v1/vendor-bills": {"get", "post"},
        "/api/v1/vendor-credits": {"get", "post"},
        "/api/v1/vendor-returns": {"get", "post"},
        "/api/v1/special-order-links": {"get", "post"},
    }
    for path, methods in required.items():
        assert path in paths, path
        assert methods <= paths[path], path
    # lifecycle actions required by the frozen contract surface
    assert {"post"} <= paths["/api/v1/purchase-orders/{po_id}/approve"]
    assert {"post"} <= paths["/api/v1/item-receipts/{receipt_id}/post"]
    assert {"post"} <= paths["/api/v1/item-receipts/{receipt_id}/reverse"]
    assert {"post"} <= paths["/api/v1/vendor-bills/{bill_id}/match"]
    assert {"post"} <= paths["/api/v1/vendor-bills/{bill_id}/post"]
    assert {"post"} <= paths["/api/v1/vendor-returns/{return_id}/ship"]
    assert {"post"} <= paths["/api/v1/special-order-links/{link_id}/fulfil"]


def test_event_payload_schemas_are_final_envelope_and_fields() -> None:
    from app.modules.procurement.facade import EVENT_TYPES

    events_dir = CONTRACT_DIR / "events"
    schemas = {path.stem.replace(".schema", ""): _json(path) for path in events_dir.glob("*.schema.json")}
    assert set(schemas) == set(EVENT_TYPES), sorted(set(schemas) ^ set(EVENT_TYPES))
    envelope_required = {"event_id", "event_type", "event_version", "tenant_id", "occurred_at", "aggregate", "data"}
    for event, schema in schemas.items():
        assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
        assert envelope_required <= set(schema["required"])
        assert "data" in schema["properties"]
        assert schema["x-event-type"] == event and schema["x-event-version"] == "1.0.0"
        assert schema["x-contract"] == "procurement.p2p" and schema["x-feature-set"] == "FS-08"
        assert schema.get("x-todo") is not True
        assert schema["x-payload-key-fields"]


def test_in_process_facade_surface() -> None:
    from app.contracts.procurement.contract import ProcurementFacade

    expected = {
        "get_vendor", "create_draft_po", "get_open_po_supply", "get_po", "get_receipt",
        "list_receipts_for_po", "create_vendor_bill_for_consignment", "get_vendor_purchase_history",
    }
    for name in expected:
        assert callable(getattr(ProcurementFacade, name, None)), name


def test_migrations_metadata_and_deferred_fks() -> None:
    from app.modules.procurement import tables as t

    assert len(t.ALL_TABLES) == 29
    assert (len(t.REVISION_0015_TABLES), len(t.REVISION_0016_TABLES), len(t.REVISION_0017_TABLES)) == (6, 11, 12)
    assert len(t.metadata.tables) == 29
    versions = REPO_ROOT / "app" / "migrations" / "versions"
    migration_0015 = (versions / "0015_procurement_vendors.py").read_text(encoding="utf-8")
    migration_0016 = (versions / "0016_procurement_ordering.py").read_text(encoding="utf-8")
    migration_0017 = (versions / "0017_procurement_billing.py").read_text(encoding="utf-8")
    assert 'revision = "0015_procurement_vendors"' in migration_0015
    assert 'down_revision = "0014_counts"' in migration_0015
    assert 'down_revision = "0015_procurement_vendors"' in migration_0016
    assert 'down_revision = "0016_procurement_ordering"' in migration_0017
    assert "fk_item_purchasing_preferred_vendor" in migration_0015
    assert "fk_landed_cost_vendor" in migration_0017
    assert "fk_landed_alloc_receipt_line" in migration_0017
    assert "fk_cost_variance_vendor_bill_line" in migration_0017
    assert "SETTINGS_NAMESPACE = \"inventory\"" in migration_0015 and "procurement." in migration_0015
    for job in ("procurement.match.run", "procurement.po.close_overdue", "procurement.receipt.reconcile", "procurement.partition.maintenance"):
        assert job in migration_0017


def test_no_direct_ledger_or_costing_writes_in_procurement() -> None:
    module_dir = REPO_ROOT / "app" / "modules" / "procurement"
    forbidden = ("INSERT INTO inventory_ledger.", "UPDATE inventory_ledger.", "INSERT INTO costing.", "UPDATE costing.")
    offenders: list[str] = []
    for path in module_dir.glob("*.py"):
        source = path.read_text(encoding="utf-8")
        for needle in forbidden:
            if needle in source:
                offenders.append(f"{path.name}: {needle}")
    assert offenders == [], offenders
    service = (module_dir / "service.py").read_text(encoding="utf-8")
    for posting in ("post_receipt", "reverse_receipt", "post_vendor_bill", "post_variance", "post_vendor_credit", "post_vendor_return", "post_drop_ship"):
        assert posting in service, posting


def test_builder_artifacts_are_current() -> None:
    result = subprocess.run(
        [sys.executable, str(CONTRACT_DIR / "build_from_app.py"), "--check"],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=180,
    )
    assert result.returncode == 0, result.stdout + result.stderr
