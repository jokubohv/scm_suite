"""Regression gates for the r5 independent verification must-fixes MF1–MF8."""

from __future__ import annotations

from app.api.main import create_app


def _spec() -> dict:
    return create_app().openapi()


def _ref(schema: dict) -> str:
    return str(schema.get("$ref", ""))


def _resolve(spec: dict, schema: dict) -> dict:
    seen: set[str] = set()
    while isinstance(schema, dict) and "$ref" in schema:
        ref = schema["$ref"]
        if ref in seen:
            break
        seen.add(ref)
        node: dict = spec
        for part in ref[2:].split("/"):
            node = node.get(part, {}) if isinstance(node, dict) else {}
        schema = node
    return schema


def _body(spec: dict, path: str, method: str) -> dict:
    return _resolve(
        spec,
        spec["paths"][path][method]["requestBody"]["content"]["application/json"]["schema"],
    )


def _response(spec: dict, path: str, method: str, code: str = "200") -> dict:
    return spec["paths"][path][method]["responses"][code]["content"]["application/json"]["schema"]


def test_mf1_item_create_update_fields() -> None:
    spec = _spec()
    create = _body(spec, "/api/v1/items", "post")
    assert {"units_type_id", "uoms", "upc_code", "external_ref", "created_from"} <= set(create["properties"])
    assert create["required"] == ["item_number"]
    update = _body(spec, "/api/v1/items/{item_id}", "patch")
    assert {"upc_code", "external_ref"} <= set(update["properties"])
    assert not {"base_uom_id", "inventory", "purchasing", "sales", "accounting"} & set(update["properties"])


def test_mf2_adjustment_create_fields() -> None:
    spec = _spec()
    body = _body(spec, "/api/v1/inventory-adjustments", "post")
    assert "adjustment_source" in body["properties"]
    line = _resolve(spec, body["properties"]["lines"]["items"])
    assert {"serial_id", "uom_id", "value_delta", "reason_code_id", "memo"} <= set(line["properties"])


def test_mf3_write_result_fields() -> None:
    spec = _spec()
    action = set(spec["components"]["schemas"]["ActionResult"]["properties"])
    assert {"ledger_sequence", "posting_at", "total_qty_base", "total_value_base", "warnings", "replayed"} <= action
    ledger = set(spec["components"]["schemas"]["LedgerActionOut"]["properties"])
    assert {"ledger_sequence", "warnings", "replayed"} <= ledger
    assert spec["components"]["schemas"]["ActionResult"]["properties"]["warnings"]["items"] == {"type": "string"}
    assert spec["components"]["schemas"]["LedgerActionOut"]["properties"]["warnings"]["items"] == {"type": "string"}
    variance = set(spec["components"]["schemas"]["CountVarianceOut"]["properties"])
    assert {"totals", "blind"} <= variance
    snapshot = set(spec["components"]["schemas"]["CountSnapshotOut"]["properties"])
    assert "items" in snapshot
    count = set(spec["components"]["schemas"]["CountActionOut"]["properties"])
    assert {"tolerance", "approval_definition_id", "auto_posted"} <= count


def test_mf4_count_reads_and_aliases_are_native() -> None:
    spec = _spec()
    cases = [
        ("/api/v1/inventory-counts/{count_id}/lines", "get", "CountLinePage", "200"),
        ("/api/v1/inventory-counts/{count_id}/variance", "get", "CountVarianceOut", "200"),
        ("/api/v1/inventory-counts/{count_id}/snapshot", "get", "CountSnapshotOut", "200"),
        ("/api/v1/inventory-counts/{id}/lines", "get", "CountLinePage", "200"),
        ("/api/v1/inventory-counts/{count_id}/lines", "post", "CountActionOut", "200"),
        ("/api/v1/inventory-counts/{id}/recount", "post", "CountActionOut", "200"),
    ]
    for path, method, model, code in cases:
        response = _response(spec, path, method, code)
        assert _ref(response).endswith("/" + model), (path, response)
    export = spec["paths"]["/api/v1/reports/{report}/export"]["get"]["responses"]["200"]
    assert not export.get("content") or "application/json" not in export["content"]
    login = _response(spec, "/api/v1/auth/login", "post")
    assert "anyOf" in login


def test_mf6_location_and_status_dtos() -> None:
    spec = _spec()
    location = _body(spec, "/api/v1/locations", "post")
    assert {"code", "name", "location_type"} <= set(location["properties"])
    assert {"is_bins_enabled", "is_statuses_enabled", "is_consignment_allowed", "allow_commit_from_non_available"} <= set(location["properties"])
    update = _body(spec, "/api/v1/locations/{id}", "patch")
    assert "code" in update["properties"]  # passes through to service immutable guard
    status = _body(spec, "/api/v1/inventory-statuses", "post")
    assert {"is_available_for_allocation", "is_committable", "is_sellable", "is_consigned", "is_default"} <= set(status["properties"])


def test_mf7_required_flags() -> None:
    spec = _spec()
    assert _body(spec, "/api/v1/vendors", "post")["required"] == ["name", "vendor_number"]
    assert "uom_type_id" in _body(spec, "/api/v1/uoms", "post")["required"]
    assert "source_line_ref" in _body(spec, "/api/v1/inventory/commitments", "post")["required"]
    assert _body(spec, "/api/v1/items", "post")["required"] == ["item_number"]


def test_mf8_receipt_details_typed() -> None:
    spec = _spec()
    line = spec["components"]["schemas"]["ReceiptCreateLine"]
    details = line["properties"]["details"]
    branch = next(item for item in details.get("anyOf", []) if item.get("type") == "array")
    assert _ref(branch["items"]).endswith("/ReceiptDetailIn")
    detail = spec["components"]["schemas"]["ReceiptDetailIn"]["properties"]
    assert {"qty_received", "bin_id", "lot_id", "serial_id", "inventory_status_id", "ownership"} <= set(detail)
