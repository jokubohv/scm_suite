"""FS-00/FS-01 canonical event payload schemas are implementation-backed (no x-todo)."""

from __future__ import annotations

import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
EVENTS_DIR = REPO_ROOT / "contracts" / "events"
EXTENSIONS = {
    "platform.core": REPO_ROOT / "app" / "contracts" / "platform" / "events",
    "platform.finance": REPO_ROOT / "app" / "contracts" / "platform" / "events",
    "order.demand": REPO_ROOT / "app" / "contracts" / "platform" / "events",
    "item.master": REPO_ROOT / "app" / "contracts" / "item_master" / "events",
}
EXPECTED = {
    "platform.core": {
        "auth.login.failed", "auth.tenant_switched", "document.approved", "document.closed",
        "document.rejected", "document.reversed", "document.submitted", "identity.linked",
        "membership.activated", "membership.roles.changed", "membership.suspended",
        "platform.approval.approved", "platform.approval.rejected", "platform.approval.requested",
        "platform.audit.recorded", "platform.flag.changed", "role.cloned", "role.created",
        "role.permissions.changed", "role.updated", "sod.violation.blocked",
        "sso.connection.created", "sso.connection.disabled", "sso.connection.error",
        "sso.connection.tested", "sso.connection.updated", "sso.login.failed",
        "tenant.provisioned", "tenant.suspended", "user.invited", "user.session.revoked",
    },
    "platform.finance": {"finance.journal_intent.recorded"},
    "order.demand": {"order.demand.changed"},
    "item.master": {
        "item.barcode.added", "item.created", "item.inactivated", "item.matrix.changed",
        "item.planning_values.updated", "item.uom.changed", "item.updated",
    },
}


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_expected_extension_schemas_present() -> None:
    total = 0
    for contract, events in EXPECTED.items():
        for event in events:
            path = EXTENSIONS[contract] / f"{event}.schema.json"
            assert path.exists(), f"missing extension schema {path}"
            schema = _load(path)
            assert schema["type"] == "object"
            assert schema["properties"], event
            assert schema.get("x-contract") == contract, event
            assert schema.get("x-todo") is not True
            total += 1
    assert total == 40


def test_canonical_schemas_have_folded_payloads_and_no_todo() -> None:
    for contract, events in EXPECTED.items():
        for event in events:
            canonical = _load(EVENTS_DIR / contract / f"{event}.schema.json")
            assert canonical.get("x-todo") is not True, event
            assert canonical["x-event-type"] == event
            assert canonical["x-contract"] == contract
            data = canonical["properties"]["data"]
            extension = _load(EXTENSIONS[contract] / f"{event}.schema.json")
            assert data["properties"] == extension["properties"], event
            assert data.get("required") == extension.get("required", []), event


def test_only_reporting_schemas_remain_todo() -> None:
    todo = []
    for path in sorted(EVENTS_DIR.rglob("*.schema.json")):
        schema = _load(path)
        if schema.get("x-todo") is True:
            todo.append(schema["x-event-type"])
    assert todo == [], f"unexpected TODO event schemas: {todo}"
