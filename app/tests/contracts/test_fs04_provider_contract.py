"""FS-04 provider contract: inventory.ledger@1.0.0 + inventory.balances@1.0.0 surfaces."""

from __future__ import annotations

import json
from pathlib import Path

from app.core.outbox import event_envelope
from app.modules.inventory_ledger import facade
from app.modules.inventory_ledger.service import POSTING_TYPES, NegativePolicy, availability, validate_command

REPO_ROOT = Path(__file__).resolve().parents[3]
LEDGER_DIR = REPO_ROOT / "app" / "contracts" / "inventory_ledger"
BALANCES_DIR = REPO_ROOT / "app" / "contracts" / "inventory_balances"


def _json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_contract_versions_and_metadata() -> None:
    assert facade.LEDGER_CONTRACT_VERSION == "1.0.0"
    assert facade.BALANCES_CONTRACT_VERSION == "1.0.0"
    ledger_meta = _json(LEDGER_DIR / "contract.json")["contracts"][0]
    balances_meta = _json(BALANCES_DIR / "contract.json")["contracts"][0]
    assert ledger_meta["contract"] == "inventory.ledger" and ledger_meta["version"] == "1.0.0"
    assert balances_meta["contract"] == "inventory.balances" and balances_meta["version"] == "1.0.0"
    dependencies = set(ledger_meta["depends_on"])
    assert {"platform.core", "platform.events", "platform.finance", "order.demand", "item.master", "pricing.engine", "traceability.registry"} <= dependencies


def test_posting_port_surface() -> None:
    port = facade.PostingPort
    expected = {
        "post", "reverse", "post_receipt", "reverse_receipt", "post_vendor_return", "post_vendor_bill",
        "post_vendor_credit", "post_variance", "post_adjustment", "post_worksheet", "post_transfer",
        "post_fulfillment", "post_return", "post_kit_fulfillment", "post_assembly_build",
        "post_assembly_unbuild", "post_wip_issue", "post_wip_completion", "post_wip_close",
        "post_count_variance", "post_consigned_receipt", "post_consigned_ownership",
        "post_consigned_adjustment", "post_revaluation", "post_cogs_adjustment", "post_landed_cost",
        "post_estimated_landed_cost", "post_drop_ship", "post_bin_transfer", "post_status_change",
        "post_import_rows", "commit", "release_commitment", "get_commitment", "get_balance",
        "list_availability", "apply_on_order_delta",
    }
    for name in expected:
        assert callable(getattr(port, name, None)), name
    assert len(POSTING_TYPES) == 30
    assert set(NegativePolicy) == {NegativePolicy.BLOCK, NegativePolicy.WARN, NegativePolicy.ALLOW}
    assert availability(10, 4) == 6


def test_openapi_fragments_are_implementation_backed() -> None:
    ledger = _json(LEDGER_DIR / "openapi.fragment.json")
    balances = _json(BALANCES_DIR / "openapi.fragment.json")
    assert ledger["info"]["x-contract"] == "inventory.ledger"
    assert balances["info"]["x-contract"] == "inventory.balances"
    assert len(ledger["paths"]) >= 20
    assert len(balances["paths"]) >= 4
    required = {
        "/api/v1/inventory-transactions": {"get", "post"},
        "/api/v1/inventory-adjustments": {"post", "get"},
        "/api/v1/inventory-transfers": {"post"},
        "/api/v1/transfer-orders": {"post"},
        "/api/v1/fulfillments": {"post"},
        "/api/v1/returns": {"post"},
        "/api/v1/imports": {"post"},
    }
    for path, methods in required.items():
        assert path in ledger["paths"], path
        assert methods <= set(ledger["paths"][path]), path
    for path in ("/api/v1/inventory/balances", "/api/v1/inventory/availability", "/api/v1/inventory/in-transit"):
        assert path in balances["paths"], path
    operations = [op for methods in ledger["paths"].values() for op in methods.values()]
    assert all(op.get("x-implemented") is True for op in operations)
    assert all("TODO" not in json.dumps(op) for op in operations)


def test_event_envelope_payload_shape() -> None:
    envelope = event_envelope(
        event_type="inventory.adjusted",
        tenant_id="11111111-1111-1111-1111-111111111111",
        aggregate_type="inventory_transaction",
        aggregate_id="22222222-2222-2222-2222-222222222222",
        payload={"transaction_id": "22222222-2222-2222-2222-222222222222", "ledger_sequence": 1},
    )
    payload = envelope["payload"]
    for key in ("event_id", "event_type", "event_version", "tenant_id", "occurred_at", "aggregate", "data"):
        assert key in payload
    assert payload["event_type"] == "inventory.adjusted"


def test_validate_command_accepts_ledger_owned_types() -> None:
    command = validate_command({
        "posting_type": "T-07", "transaction_date": "2026-10-23",
        "movements": [{
            "item_id": "11111111-1111-1111-1111-111111111111",
            "location_id": "22222222-2222-2222-2222-222222222222",
            "inventory_status_id": "33333333-3333-3333-3333-333333333333",
            "qty": "1", "movement_type": "ADJUSTMENT",
        }],
    })
    assert command["posting_type"] == "T-07"
