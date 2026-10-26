#!/usr/bin/env python3
"""Live E2E driver: two companies, multiple warehouses, production-like app role.

Usage:
  E2E_BASE=http://127.0.0.1:28080 \
  E2E_SUPER_TOKEN=... \
  python3 e2e/run_live_e2e.py

Creates unique tenant data, exercises core/cross-context flows, sweeps every GET operation,
and writes verification/evidence/E2E/latest.md + latest.json. Exits non-zero on unexpected 5xx,
cross-tenant leaks, auth/account-policy violations, or failed core assertions.
"""
from __future__ import annotations

import json
import os
import secrets
import sys
import traceback
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx

BASE = os.environ.get("E2E_BASE", "http://127.0.0.1:28080").rstrip("/")
SUPER = os.environ.get("E2E_SUPER_TOKEN", "").strip()
ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "verification/evidence/E2E"
RUN = "E2E" + datetime.now(UTC).strftime("%m%d%H%M") + secrets.token_hex(2).upper()
TODAY = datetime.now(UTC).date().isoformat()


@dataclass
class Result:
    checks: list[dict[str, Any]] = field(default_factory=list)
    failures: list[dict[str, Any]] = field(default_factory=list)
    data: dict[str, Any] = field(default_factory=dict)

    def check(self, name: str, ok: bool, status: int | None = None, detail: Any = None) -> bool:
        row = {"name": name, "ok": bool(ok), "status": status, "detail": detail}
        self.checks.append(row)
        if not ok:
            self.failures.append(row)
        print(("PASS " if ok else "FAIL ") + name + (f" [{status}]" if status else ""))
        return ok

    def crash(self, name: str, exc: Exception) -> None:
        row = {"name": name, "ok": False, "error": f"{type(exc).__name__}: {exc}", "traceback": traceback.format_exc()}
        self.checks.append(row)
        self.failures.append(row)
        print("FAIL " + name + " " + row["error"])


def client() -> httpx.Client:
    return httpx.Client(base_url=BASE, timeout=40)


def json_or_text(response: httpx.Response) -> Any:
    try:
        return response.json()
    except Exception:
        return response.text[:1000]


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def idem() -> dict[str, str]:
    return {"Idempotency-Key": secrets.token_hex(16)}


def request(c: httpx.Client, method: str, path: str, *, token: str | None = None, expected: int | tuple[int, ...] = 200,
            body: Any = None, headers: dict[str, str] | None = None, result: Result | None = None,
            name: str | None = None) -> httpx.Response:
    h = dict(headers or {})
    if token:
        h.update(auth(token))
    r = c.request(method, BASE + path if path.startswith("/") else path, json=body, headers=h)
    expected_set = {expected} if isinstance(expected, int) else set(expected)
    ok = r.status_code in expected_set
    if result is not None:
        result.check(name or f"{method} {path}", ok, r.status_code, json_or_text(r) if not ok else None)
    return r


def bootstrap_tenant(c: httpx.Client, result: Result, label: str) -> dict[str, Any]:
    code = f"{RUN}_{label}"
    email = f"e2e-{RUN.lower()}-{label.lower()}@example.com"
    password = "E2E-" + secrets.token_urlsafe(18)
    r = request(c, "POST", "/api/v1/bootstrap/tenants", headers={"X-Super-Admin-Token": SUPER},
                expected=201, body={"code": code, "name": f"E2E Company {label}", "timezone": "UTC",
                                    "base_currency_code": "USD", "admin_email": email,
                                    "admin_display_name": f"E2E {label} Admin", "admin_password": password},
                result=result, name=f"bootstrap tenant {label}")
    if r.status_code != 201:
        raise RuntimeError(f"bootstrap failed: {r.text}")
    tenant = r.json()
    login = request(c, "POST", "/api/v1/auth/login", expected=200,
                    body={"email": email, "password": password, "tenant": code},
                    result=result, name=f"login tenant {label}")
    if login.status_code != 200:
        raise RuntimeError(f"login failed: {login.text}")
    token = login.json()["access_token"]
    # Enable every tenant flag plus the canonical traceability flag (the seeded key is stale).
    flags = request(c, "GET", "/api/v1/admin/flags", token=token, result=result, name=f"list flags {label}").json()["items"]
    for flag in flags:
        request(c, "PUT", f"/api/v1/admin/feature-flags/{flag['flag_key']}", token=token,
                expected=(200, 201), body={"is_enabled": True, "notes": "e2e"}, result=result,
                name=f"enable {label} {flag['flag_key']}")
    for key in ("traceability.registry.enabled", "traceability.numbered.enabled"):
        request(c, "PUT", f"/api/v1/admin/feature-flags/{key}", token=token, expected=(200, 201),
                body={"is_enabled": True, "notes": "e2e canonical flag"}, result=result,
                name=f"enable {label} {key}")
    return {"code": tenant["code"], "id": tenant["tenant_id"], "email": email, "password": password,
            "token": token, "admin": tenant["admin"], "flags": flags}


def create_warehouse_environment(c: httpx.Client, result: Result, tenant: dict[str, Any], suffix: str,
                                 with_bins: bool = True) -> dict[str, Any]:
    token = tenant["token"]
    loc = request(c, "POST", "/api/v1/locations", token=token, expected=201,
                  body={"code": f"{tenant['code']}-WH-{suffix}", "name": f"Warehouse {suffix}",
                        "location_type": "WAREHOUSE", "is_bins_enabled": True, "is_statuses_enabled": True},
                  headers=idem(), result=result, name=f"location {suffix}").json()["location"]
    bins: dict[str, dict[str, Any]] = {}
    if with_bins:
        for btype, bcode in (("RECEIVING", "RCV"), ("STORAGE", "STG"), ("PICKING", "PICK")):
            rb = request(c, "POST", "/api/v1/bins", token=token, expected=201,
                         body={"location_id": loc["id"], "code": f"{bcode}-{suffix}", "name": bcode,
                               "bin_type": btype, "is_pickable": btype == "PICKING"},
                         headers=idem(), result=result, name=f"bin {suffix} {bcode}")
            if rb.status_code == 201:
                bins[bcode] = rb.json()["bin"]
    wh_body: dict[str, Any] = {"location_id": loc["id"], "code": f"WH-{suffix}", "name": f"Warehouse {suffix}",
                               "is_wms_enabled": True}
    if len(bins) == 3:
        wh_body.update({"receiving_bin_id": bins["RCV"]["id"], "staging_bin_id": bins["STG"]["id"],
                        "shipping_bin_id": bins["PICK"]["id"]})
    wh = request(c, "POST", "/api/v1/warehouses", token=token, expected=201, body=wh_body,
                 headers=idem(), result=result, name=f"warehouse {suffix}").json()
    statuses = {x["code"]: x for x in request(c, "GET", "/api/v1/inventory-statuses", token=token,
                                              result=result, name=f"statuses {suffix}").json()["items"]}
    return {"location": loc, "bins": bins, "warehouse": wh, "statuses": statuses}


def create_item(c: httpx.Client, result: Result, tenant: dict[str, Any], suffix: str, price: str = "10") -> dict[str, Any]:
    number = f"{tenant['code']}-{suffix}"
    r = request(c, "POST", "/api/v1/items", token=tenant["token"], expected=201,
                body={"item_number": number, "display_name": number, "item_type_code": "INVENTORY",
                      "inventory": {"costing_method": "AVERAGE", "initial_cost": price}},
                headers=idem(), result=result, name=f"item {suffix}")
    return r.json()


def run_api_key_integration(c: httpx.Client, result: Result, tenant: dict[str, Any],
                            item: dict[str, Any]) -> dict[str, Any]:
    token = tenant["token"]
    created = request(c, "POST", "/api/v1/admin/api-keys", token=token, expected=201,
                      body={"name": f"e2e-integration-{RUN}", "scopes": ["items.read"]},
                      result=result, name="api key create")
    if created.status_code != 201:
        return {}
    payload = created.json()
    secret = payload["secret"]
    key_id = payload["id"]
    result.check("api key secret returned once", bool(secret) and secret.startswith(payload["key_prefix"] + "."),
                 created.status_code)
    key_headers = {"X-API-Key": secret}
    allowed = request(c, "GET", "/api/v1/items", headers=key_headers, expected=200,
                      result=result, name="api key read items")
    result.check("api key authenticates business GET", allowed.status_code == 200, allowed.status_code)
    spec = request(c, "GET", "/api/v1/openapi.json", headers=key_headers, expected=200,
                   result=result, name="api key openapi access")
    result.check("api key can fetch runtime OpenAPI", spec.status_code == 200 and "paths" in spec.json(),
                 spec.status_code)
    denied = request(c, "GET", "/api/v1/admin/flags", headers=key_headers, expected=403,
                     result=result, name="api key scope denied")
    result.check("api key cannot exceed scopes", denied.status_code == 403, denied.status_code)
    write_denied = request(c, "POST", "/api/v1/items", headers=key_headers, expected=403,
                           body={"item_number": f"{tenant['code']}-KEY-{RUN}", "display_name": "key test",
                                 "item_type_code": "INVENTORY"},
                           result=result, name="api key write scope denied")
    result.check("api key cannot write without items.update", write_denied.status_code == 403, write_denied.status_code)
    request(c, "DELETE", f"/api/v1/admin/api-keys/{key_id}", token=token, expected=204,
            result=result, name="api key revoke")
    revoked = request(c, "GET", "/api/v1/items", headers=key_headers, expected=401,
                      result=result, name="api key revoked")
    result.check("revoked api key rejected", revoked.status_code == 401, revoked.status_code)
    return {"id": key_id, "prefix": payload["key_prefix"]}


def run_procurement(c: httpx.Client, result: Result, tenant: dict[str, Any],
                    env: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
    token = tenant["token"]
    vendor = request(c, "POST", "/api/v1/vendors", token=token, expected=201, headers=idem(),
                     body={"vendor_number": f"V-{RUN}", "name": f"Vendor {RUN}", "currency_code": "USD",
                           "credit_limit": "50000"}, result=result, name="procurement vendor").json()
    request(c, "POST", "/api/v1/item-vendors", token=token, expected=201, headers=idem(),
            body={"item_id": item["id"], "vendor_id": vendor["id"], "is_preferred": True,
                  "lead_time_days": 2, "min_order_qty": "1"}, result=result, name="procurement item-vendor")
    po = request(c, "POST", "/api/v1/purchase-orders", token=token, expected=201, headers=idem(),
                 body={"vendor_id": vendor["id"], "po_type": "STANDARD", "currency_code": "USD",
                       "transaction_date": TODAY, "ship_to_location_id": env["location"]["id"],
                       "lines": [{"item_id": item["id"], "qty_ordered": 8, "unit_price": "10",
                                  "expected_receipt_date": "2026-11-01"}]},
                 result=result, name="procurement PO").json()
    po_id = po["id"]
    submitted = request(c, "POST", f"/api/v1/purchase-orders/{po_id}/submit", token=token,
                        expected=200, headers=idem(), body={}, result=result, name="procurement PO submit")
    if submitted.status_code == 200 and submitted.json().get("status") != "APPROVED":
        request(c, "POST", f"/api/v1/purchase-orders/{po_id}/approve", token=token, expected=200,
                headers=idem(), body={}, result=result, name="procurement PO approve")
    detail = request(c, "GET", f"/api/v1/purchase-orders/{po_id}", token=token, result=result,
                     name="procurement PO detail").json()
    line = detail["lines"][0]
    receipt = request(c, "POST", "/api/v1/item-receipts", token=token, expected=201, headers=idem(),
                      body={"purchase_order_id": po_id, "vendor_id": vendor["id"], "receipt_type": "PO",
                            "received_at": "2026-10-25T10:00:00Z", "location_id": env["location"]["id"],
                            "lines": [{"purchase_order_line_id": line["id"], "item_id": item["id"],
                                       "qty_received": 8, "unit_cost": line["unit_price"],
                                       "location_id": env["location"]["id"],
                                       "inventory_status_id": env["statuses"]["AVAILABLE"]["id"],
                                       "details": [{"qty_received": 8,
                                                    "inventory_status_id": env["statuses"]["AVAILABLE"]["id"],
                                                    "ownership": "OWNED"}]}]},
                      result=result, name="procurement receipt").json()
    request(c, "POST", f"/api/v1/item-receipts/{receipt['id']}/post", token=token, expected=200,
            headers=idem(), body={}, result=result, name="procurement receipt post")
    bill = request(c, "POST", "/api/v1/vendor-bills", token=token, expected=201, headers=idem(),
                   body={"vendor_id": vendor["id"], "vendor_bill_number": f"BILL-{RUN}",
                         "purchase_order_id": po_id, "item_receipt_id": receipt["id"],
                         "currency_code": "USD", "transaction_date": TODAY,
                         "lines": [{"item_id": item["id"], "qty": 8, "unit_price": "10",
                                    "purchase_order_line_id": line["id"],
                                    "item_receipt_line_id": receipt["lines"][0]["id"]}]},
                   result=result, name="procurement bill").json()
    bid = bill["id"]
    request(c, "POST", f"/api/v1/vendor-bills/{bid}/match", token=token, expected=200,
            headers=idem(), body={}, result=result, name="procurement bill match")
    bill_submit = request(c, "POST", f"/api/v1/vendor-bills/{bid}/submit", token=token, expected=200,
                          headers=idem(), body={}, result=result, name="procurement bill submit")
    if bill_submit.status_code == 200 and bill_submit.json().get("status") != "APPROVED":
        request(c, "POST", f"/api/v1/vendor-bills/{bid}/approve", token=token, expected=200,
                headers=idem(), body={}, result=result, name="procurement bill approve")
    request(c, "POST", f"/api/v1/vendor-bills/{bid}/post", token=token, expected=200,
            headers=idem(), body={}, result=result, name="procurement bill post")
    return {"vendor": vendor, "po": po, "receipt": receipt, "bill": bill}


def run_quality(c: httpx.Client, result: Result, tenant: dict[str, Any], env: dict[str, Any],
                item: dict[str, Any]) -> None:
    token = tenant["token"]
    spec = request(c, "POST", "/api/v1/quality/specs", token=token, expected=201, headers=idem(),
                   body={"code": f"SPEC-{RUN}", "name": f"Spec {RUN}", "item_id": item["id"],
                         "lines": [{"characteristic": "length", "result_type": "NUMERIC",
                                    "min_value": "9", "max_value": "11"},
                                   {"characteristic": "visual", "result_type": "PASS_FAIL"}]},
                   result=result, name="quality spec").json()
    spec_id = spec["spec"]["id"]
    request(c, "POST", f"/api/v1/quality/specs/{spec_id}/activate", token=token, expected=200,
            headers=idem(), body={}, result=result, name="quality spec activate")
    plan = request(c, "POST", "/api/v1/quality/inspection-plans", token=token, expected=201, headers=idem(),
                   body={"code": f"PLAN-{RUN}", "name": f"Plan {RUN}", "item_id": item["id"],
                         "quality_spec_id": spec_id, "inspection_type": "INCOMING",
                         "sampling_method": "FULL", "sample_size": 1},
                   result=result, name="quality plan").json()
    inspection = request(c, "POST", "/api/v1/quality/inspections", token=token, expected=201, headers=idem(),
                         body={"inspection_plan_id": plan["plan"]["id"], "item_id": item["id"],
                               "location_id": env["location"]["id"], "source_entity_type": "ITEM_RECEIPT",
                               "source_entity_id": str(uuid4())},
                         result=result, name="quality inspection").json()
    iid = inspection["inspection"]["id"]
    request(c, "POST", f"/api/v1/quality/inspections/{iid}/start", token=token, expected=200,
            body={}, result=result, name="quality start")
    request(c, "POST", f"/api/v1/quality/inspections/{iid}/results", token=token, expected=200,
            body={"results": [{"sequence": 1, "characteristic": "length", "result_type": "NUMERIC",
                               "measured_value_number": "10"},
                              {"sequence": 2, "characteristic": "visual", "result_type": "PASS_FAIL",
                               "measured_value_text": "PASS"}]},
            result=result, name="quality results")
    request(c, "POST", f"/api/v1/quality/inspections/{iid}/complete", token=token, expected=200,
            headers=idem(), body={"notes": "e2e"}, result=result, name="quality complete")


def run_wms(c: httpx.Client, result: Result, tenant: dict[str, Any], env: dict[str, Any],
            item: dict[str, Any], vendor: dict[str, Any]) -> None:
    token = tenant["token"]
    warehouse = env["warehouse"]
    location = env["location"]
    asn = request(c, "POST", "/api/v1/inbound-shipments", token=token, expected=201, headers=idem(),
                  body={"location_id": location["id"], "vendor_id": vendor["id"], "carrier_code": "UPS",
                        "expected_arrival_date": TODAY,
                        "lines": [{"item_id": item["id"], "qty_expected": "4", "unit_cost": "5"}]},
                  result=result, name="wms inbound shipment").json()
    request(c, "POST", f"/api/v1/inbound-shipments/{asn['id']}/arrive", token=token, expected=200,
            headers=idem(), body={}, result=result, name="wms inbound arrive")
    order = request(c, "POST", "/api/v1/receiving-orders", token=token, expected=201, headers=idem(),
                    body={"inbound_shipment_id": asn["id"]}, result=result, name="wms receiving order").json()
    oid = order["id"]
    request(c, "POST", f"/api/v1/receiving-orders/{oid}/start", token=token, expected=200,
            headers=idem(), body={}, result=result, name="wms receiving start")
    request(c, "POST", f"/api/v1/receiving-orders/{oid}/complete", token=token, expected=200, headers=idem(),
            body={"lines": [{"line_number": 1, "qty_received": "4"}]},
            result=result, name="wms receiving complete")
    tasks = request(c, "GET", "/api/v1/putaway-tasks?status=PENDING", token=token, result=result,
                    name="wms putaway list").json().get("items", [])
    if tasks:
        tid = tasks[0]["id"]
        request(c, "POST", f"/api/v1/putaway-tasks/{tid}/assign", token=token, expected=200, headers=idem(),
                body={"assigned_user_id": tenant["admin"]["app_user_id"]},
                result=result, name="wms putaway assign")
        request(c, "POST", f"/api/v1/putaway-tasks/{tid}/start", token=token, expected=200,
                headers=idem(), body={}, result=result, name="wms putaway start")
        request(c, "POST", f"/api/v1/putaway-tasks/{tid}/complete", token=token, expected=200,
                headers=idem(), body={}, result=result, name="wms putaway complete")
    bins = request(c, "GET", f"/api/v1/locations/{location['id']}/bins", token=token,
                   result=result, name="wms bins").json()["items"]
    pick = next((b for b in bins if b["code"].startswith("PICK")), None)
    if pick:
        request(c, "POST", f"/api/v1/warehouses/{warehouse['id']}/inventory", token=token, expected=201,
                headers=idem(), body={"item_id": item["id"], "bin_id": pick["id"], "qty": "10",
                                      "inventory_status_id": env["statuses"]["AVAILABLE"]["id"]},
                result=result, name="wms warehouse inventory")
        wave = request(c, "POST", "/api/v1/waves", token=token, expected=201, headers=idem(),
                       body={"location_id": warehouse["location_id"],
                             "lines": [{"source_type": "SALES_ORDER", "source_ref": f"SO-{RUN}",
                                        "source_line_ref": "1", "item_id": item["id"], "qty": "3"}]},
                       result=result, name="wms wave").json()
        released = request(c, "POST", f"/api/v1/waves/{wave['id']}/release", token=token, expected=200,
                           headers=idem(), body={}, result=result, name="wms wave release")
        if released.status_code == 200:
            tids = released.json().get("pick_task_ids", [])
            if tids:
                tid = tids[0]
                request(c, "POST", f"/api/v1/pick-tasks/{tid}/assign", token=token, expected=200,
                        headers=idem(), body={"assigned_user_id": tenant["admin"]["app_user_id"]},
                        result=result, name="wms pick assign")
                request(c, "POST", f"/api/v1/pick-tasks/{tid}/start", token=token, expected=200,
                        headers=idem(), body={}, result=result, name="wms pick start")
                request(c, "POST", f"/api/v1/pick-tasks/{tid}/complete", token=token, expected=200,
                        headers=idem(), body={"lines": [{"line_number": 1, "qty_picked": "3",
                                                         "actual_bin_id": pick["id"]}]},
                        result=result, name="wms pick complete")


def run_manufacturing(c: httpx.Client, result: Result, tenant: dict[str, Any],
                      item: dict[str, Any], component: dict[str, Any]) -> None:
    token = tenant["token"]
    assembly_item = request(c, "POST", "/api/v1/items", token=token, expected=201, headers=idem(),
                            body={"item_number": f"{tenant['code']}-ASM-{RUN}", "display_name": "E2E Assembly",
                                  "item_type_code": "ASSEMBLY",
                                  "inventory": {"costing_method": "AVERAGE", "initial_cost": "5"}},
                            result=result, name="manufacturing assembly item").json()
    assembly = request(c, "POST", "/api/v1/assemblies", token=token, expected=201, headers=idem(),
                       body={"item_id": assembly_item["id"], "assembly_type": "STANDARD"},
                       result=result, name="manufacturing assembly").json()
    aid = assembly["id"]
    bom = request(c, "POST", "/api/v1/boms", token=token, expected=201, headers=idem(),
                  body={"assembly_item_id": aid, "bom_code": f"BOM-{RUN}", "name": f"BOM {RUN}"},
                  result=result, name="manufacturing BOM").json()["bom"]
    request(c, "POST", f"/api/v1/boms/{bom['id']}/activate", token=token, expected=200,
            headers=idem(), body={}, result=result, name="manufacturing BOM activate")
    revision = request(c, "POST", f"/api/v1/boms/{bom['id']}/revisions", token=token, expected=201,
                       headers=idem(), body={"revision_code": "R1", "effective_from": "2020-01-01"},
                       result=result, name="manufacturing BOM revision").json()["revision"]
    request(c, "POST", f"/api/v1/bom-revisions/{revision['id']}/components", token=token, expected=201,
            headers=idem(), body={"line_number": 1, "component_item_id": component["id"],
                                  "qty_per": "2", "uom_id": assembly_item["base_uom_id"]},
            result=result, name="manufacturing BOM component")
    wo = request(c, "POST", "/api/v1/work-orders", token=token, expected=201, headers=idem(),
                 body={"assembly_item_id": aid, "qty_ordered": "2", "start_date": TODAY,
                       "end_date": TODAY, "uom_id": assembly_item["base_uom_id"]},
                 result=result, name="manufacturing work order").json()["work_order"]
    request(c, "POST", f"/api/v1/work-orders/{wo['id']}/release", token=token, expected=200,
            headers=idem(), body={}, result=result, name="manufacturing WO release")


def main() -> int:
    result = Result()
    if not SUPER:
        print("E2E_SUPER_TOKEN is required", file=sys.stderr)
        return 2
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    c = client()
    result.data["run"] = RUN
    result.data["base"] = BASE
    try:
        readiness = request(c, "GET", "/readyz", expected=200, result=result, name="readiness")
        result.check("readiness db ok", readiness.json().get("checks", {}).get("database") == "ok", readiness.status_code)
        request(c, "GET", "/api/v1/bootstrap/whoami", headers={"X-Super-Admin-Token": SUPER},
                result=result, name="bootstrap whoami")
        request(c, "GET", "/api/v1/bootstrap/whoami", expected=(401, 403, 503), result=result,
                name="bootstrap whoami without token")
        request(c, "GET", "/api/v1/openapi.json", result=result, name="runtime openapi")

        tenants = []
        for label in ("A", "B"):
            try:
                tenants.append(bootstrap_tenant(c, result, label))
            except Exception as exc:
                result.crash(f"bootstrap/login {label}", exc)
        if len(tenants) != 2:
            raise RuntimeError("both tenants required")
        result.data["tenants"] = tenants
        a, b = tenants

        # Create multiple warehouses for A and one for B.
        env_a1 = create_warehouse_environment(c, result, a, "A1")
        env_a2 = create_warehouse_environment(c, result, a, "A2")
        env_b1 = create_warehouse_environment(c, result, b, "B1")
        result.data["warehouses"] = {"A1": env_a1, "A2": env_a2, "B1": env_b1}
        item_a = create_item(c, result, a, "ITEM-A")
        item_a2 = create_item(c, result, a, "ITEM-B")
        item_b = create_item(c, result, b, "ITEM-A")
        result.data["items"] = {"A1": item_a, "A2": item_a2, "B1": item_b}
        try:
            result.data["api_key"] = run_api_key_integration(c, result, a, item_a)
        except Exception as exc:
            result.crash("api key integration phase", exc)

        # Opening adjustment in A1, then transfer A1 -> A2.
        adj = request(c, "POST", "/api/v1/inventory-adjustments", token=a["token"], expected=201, headers=idem(),
                      body={"transaction_date": TODAY, "memo": "e2e opening", "lines": [{
                          "item_id": item_a["id"], "location_id": env_a1["location"]["id"],
                          "inventory_status_id": env_a1["statuses"]["AVAILABLE"]["id"],
                          "qty_delta": "10", "unit_cost": "10"}]}, result=result, name="create adjustment")
        if adj.status_code == 201:
            aid = adj.json()["id"]
            for action in ("submit", "post"):
                request(c, "POST", f"/api/v1/inventory-adjustments/{aid}/{action}", token=a["token"],
                        expected=200, headers=idem(), body={}, result=result, name=f"{action} adjustment")
            balances = request(c, "GET", f"/api/v1/inventory/balances/search?item_id={item_a['id']}",
                               token=a["token"], result=result, name="read balances after adjustment")
            if balances.status_code == 200:
                total = sum(float(row.get("qty_on_hand", 0)) for row in balances.json().get("items", []))
                result.check("on-hand after adjustment == 10", total == 10.0, balances.status_code, total)

        transfer = request(c, "POST", "/api/v1/inventory-transfers", token=a["token"], expected=201, headers=idem(),
                           body={"transaction_date": TODAY, "memo": "e2e transfer",
                                 "from_location_id": env_a1["location"]["id"],
                                 "to_location_id": env_a2["location"]["id"],
                                 "lines": [{"item_id": item_a["id"], "qty": "4",
                                            "inventory_status_id": env_a1["statuses"]["AVAILABLE"]["id"]}]},
                           result=result, name="create transfer")
        if transfer.status_code == 201:
            tid = transfer.json()["id"]
            for action in ("submit", "post"):
                request(c, "POST", f"/api/v1/inventory-transfers/{tid}/{action}", token=a["token"],
                        expected=200, headers=idem(), body={}, result=result, name=f"{action} transfer")
            a1_bal = request(c, "GET", f"/api/v1/inventory/balances/search?item_id={item_a['id']}&location_id={env_a1['location']['id']}",
                             token=a["token"], result=result, name="A1 balance").json().get("items", [])
            a2_bal = request(c, "GET", f"/api/v1/inventory/balances/search?item_id={item_a['id']}&location_id={env_a2['location']['id']}",
                             token=a["token"], result=result, name="A2 balance").json().get("items", [])
            result.check("A1 on-hand after transfer == 6", sum(float(x.get("qty_on_hand", 0)) for x in a1_bal) == 6.0)
            result.check("A2 on-hand after transfer == 4", sum(float(x.get("qty_on_hand", 0)) for x in a2_bal) == 4.0)

        # Manual count: start, capture every line, submit, approve with a second admin.
        approver_email = f"e2e-{RUN.lower()}-approver@example.com"
        approver_pw = "E2E-" + secrets.token_urlsafe(18)
        appr = request(c, "POST", "/api/v1/bootstrap/users", expected=201,
                       headers={"X-Super-Admin-Token": SUPER},
                       body={"tenant_id": a["id"], "email": approver_email, "display_name": "E2E Approver",
                             "password": approver_pw, "role_codes": ["admin"]}, result=result, name="create approver")
        approver_token = None
        if appr.status_code == 201:
            login2 = request(c, "POST", "/api/v1/auth/login", expected=200,
                             body={"email": approver_email, "password": approver_pw, "tenant": a["code"]},
                             result=result, name="login approver")
            if login2.status_code == 200:
                approver_token = login2.json()["access_token"]
        count = request(c, "POST", "/api/v1/inventory-counts", token=a["token"], expected=201, headers=idem(),
                        body={"count_type": "MANUAL", "count_date": TODAY, "blind_count": False,
                              "approval_required": False, "scope": {"items": [item_a["id"]]}},
                        result=result, name="create count")
        if count.status_code == 201:
            cid = count.json()["count_id"]
            request(c, "POST", f"/api/v1/inventory-counts/{cid}/start", token=a["token"], expected=200,
                    headers=idem(), body={}, result=result, name="start count")
            lines = request(c, "GET", f"/api/v1/inventory-counts/{cid}/lines", token=a["token"],
                            result=result, name="count lines")
            if lines.status_code == 200:
                line_rows = lines.json().get("items", [])
                payload = [{"line_id": row["id"], "counted_qty": "5"} for row in line_rows]
                if payload:
                    request(c, "POST", f"/api/v1/inventory-counts/{cid}/lines", token=a["token"], expected=200,
                            headers=idem(), body={"client_batch_id": uuid4().hex, "lines": payload},
                            result=result, name="capture count")
                    request(c, "POST", f"/api/v1/inventory-counts/{cid}/submit", token=a["token"], expected=200,
                            headers=idem(), body={}, result=result, name="submit count")
                    if approver_token:
                        request(c, "POST", f"/api/v1/inventory-counts/{cid}/approve", token=approver_token,
                                expected=200, headers=idem(), body={}, result=result, name="approve count")
                result.data["count"] = {"id": cid, "lines": len(line_rows)}

        # Procurement / quality / WMS / manufacturing phases.
        try:
            procurement = run_procurement(c, result, a, env_a1, item_a)
            result.data["procurement"] = procurement
            vendor = procurement["vendor"]
        except Exception as exc:
            result.crash("procurement phase", exc)
            vendor = None
        try:
            run_quality(c, result, a, env_a1, item_a)
        except Exception as exc:
            result.crash("quality phase", exc)
        try:
            if vendor:
                run_wms(c, result, a, env_a1, item_a, vendor)
        except Exception as exc:
            result.crash("wms phase", exc)
        try:
            run_manufacturing(c, result, a, item_a, item_a2)
        except Exception as exc:
            result.crash("manufacturing phase", exc)

        # Cross-tenant isolation.
        for path, resource in ((f"/api/v1/items/{item_a['id']}", "item"), (f"/api/v1/locations/{env_a1['location']['id']}", "location"),
                               (f"/api/v1/warehouses/{env_a1['warehouse']['id']}", "warehouse")):
            r = request(c, "GET", path, token=b["token"], expected=(403, 404), result=result,
                        name=f"cross-tenant {resource} denied")
            result.check(f"cross-tenant {resource} no data leak", r.status_code in (403, 404), r.status_code)

        # Reporting reads.
        request(c, "GET", "/api/v1/reports", token=a["token"], result=result, name="report catalogue")
        for report in ("inventory-balance", "stock-ledger", "inventory-valuation", "inventory-snapshot",
                       "negative-inventory", "lot-expiry", "manufacturing-workbench", "planning-workbench",
                       "atp-workbench", "quality-inspections", "consigned-balance", "purchase-history"):
            request(c, "GET", f"/api/v1/reports/{report}", token=a["token"], expected=(200, 404), result=result,
                    name=f"report {report}")

        # API surface sweep: every GET operation without path params, using tenant A token.
        spec = c.get(BASE + "/api/v1/openapi.json").json()
        swept = 0
        errors: list[dict[str, Any]] = []
        for path, item in sorted(spec["paths"].items()):
            if "get" not in item or "{" in path or path.startswith("/api/v1/bootstrap"):
                continue
            r = c.get(BASE + path, headers=auth(a["token"]))
            swept += 1
            if r.status_code >= 500:
                errors.append({"path": path, "status": r.status_code, "body": r.text[:300]})
        result.data["get_sweep"] = {"attempted": swept, "5xx": errors}
        result.check("GET surface sweep has no 5xx", not errors, None, errors[:10])
    except Exception as exc:
        result.crash("main flow", exc)
    finally:
        c.close()

    summary = {"run": RUN, "checked": len(result.checks), "failure_count": len(result.failures),
               "failures": result.failures, "checks": result.checks, "data": result.data}
    (EVIDENCE / "latest.json").write_text(json.dumps(summary, indent=2))
    lines = [f"# Live E2E — {RUN}", "", f"Base: `{BASE}`", "",
             f"Checks: **{len(result.checks)}**, failures: **{len(result.failures)}**", ""]
    for row in result.checks:
        lines.append(f"- {'PASS' if row['ok'] else 'FAIL'} — {row['name']}" +
                     (f" ({row.get('status')})" if row.get("status") else "") +
                     (f" — `{row.get('error')}`" if row.get("error") else ""))
    (EVIDENCE / "latest.md").write_text("\n".join(lines) + "\n")
    print(f"\n{len(result.checks)} checks, {len(result.failures)} failures; evidence: {EVIDENCE/'latest.md'}")
    return 1 if result.failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
