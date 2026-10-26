#!/usr/bin/env python3
"""Invoke every mounted OpenAPI operation once against a dedicated local E2E API.

This is crash-safety coverage, not business conformance: it sends an empty/minimal body and placeholder
path parameters, accepts expected 2xx/4xx responses, and fails only on unexpected 5xx responses.
Never run destructive writes against production; the local E2E database is disposable.
"""
from __future__ import annotations

import json
import os
import secrets
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx

BASE = os.environ.get("E2E_BASE", "http://127.0.0.1:28080").rstrip("/")
ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "verification/evidence/E2E"
SUPER = os.environ.get("E2E_SUPER_TOKEN", "").strip()
DATA = json.loads((EVIDENCE / "latest.json").read_text())["data"]
TENANT = DATA["tenants"][0]
EMAIL, PASSWORD, TENANT_CODE = TENANT["email"], TENANT["password"], TENANT["code"]
ITEMS = DATA.get("items", {})
WAREHOUSES = DATA.get("warehouses", {})

COMMON = {
    "item_id": ITEMS.get("A1", {}).get("id"),
    "component_item_id": ITEMS.get("A2", {}).get("id"),
    "assembly_item_id": ITEMS.get("A1", {}).get("id"),
    "location_id": WAREHOUSES.get("A1", {}).get("location", {}).get("id"),
    "warehouse_id": WAREHOUSES.get("A1", {}).get("warehouse", {}).get("id"),
    "bin_id": next(iter(WAREHOUSES.get("A1", {}).get("bins", {}).values()), {}).get("id"),
    "status_id": None,
    "inventory_status_id": None,
    "report": "stock-ledger",
    "report_name": "stock-ledger",
}
COMMON = {k: v for k, v in COMMON.items() if v}


def login(c: httpx.Client) -> str:
    r = c.post(BASE + "/api/v1/auth/login", json={"email": EMAIL, "password": PASSWORD, "tenant": TENANT_CODE})
    r.raise_for_status()
    return r.json()["access_token"]


def fill_path(path: str) -> str:
    out = path
    for name, value in COMMON.items():
        out = out.replace("{" + name + "}", str(value))
    # Remaining placeholders get a syntactically valid UUID (or a report alias).
    while "{" in out:
        start = out.index("{")
        end = out.index("}", start)
        name = out[start + 1:end]
        value = str(uuid4())
        if name in {"report", "report_name"}:
            value = "stock-ledger"
        out = out[:start] + value + out[end + 1:]
    return out


def one(client: httpx.Client, method: str, path: str, token: str) -> dict[str, Any]:
    url = BASE + fill_path(path)
    headers = {"Authorization": f"Bearer {token}", "Idempotency-Key": secrets.token_hex(16)}
    body = None if method in {"GET", "DELETE"} else {}
    if path.startswith("/api/v1/bootstrap"):
        headers = {"X-Super-Admin-Token": SUPER, "Idempotency-Key": secrets.token_hex(16)}
    try:
        r = client.request(method, url, json=body, headers=headers, timeout=20)
        return {"method": method, "path": path, "url": url, "status": r.status_code,
                "body": (r.text[:500] if r.status_code >= 500 else ""), "error": None}
    except Exception as exc:  # network/timeout is also a smoke failure
        return {"method": method, "path": path, "url": url, "status": None,
                "body": "", "error": f"{type(exc).__name__}: {exc}"}


def main() -> int:
    if not SUPER:
        print("E2E_SUPER_TOKEN required", file=sys.stderr)
        return 2
    spec = httpx.get(BASE + "/api/v1/openapi.json", timeout=20).json()
    operations: list[tuple[str, str]] = []
    for path, item in spec["paths"].items():
        for method in item:
            if method in {"get", "post", "put", "patch", "delete"}:
                operations.append((method.upper(), path))
    with httpx.Client() as boot:
        token = login(boot)
    results: list[dict[str, Any]] = []
    with httpx.Client() as shared, ThreadPoolExecutor(max_workers=int(os.environ.get("E2E_SWEEP_WORKERS", "1"))) as pool:
        futures = {pool.submit(one, shared, method, path, token): (method, path) for method, path in operations}
        for future in as_completed(futures):
            results.append(future.result())
    failures = [row for row in results if row["error"] or (row["status"] or 500) >= 500]
    by_status: dict[str, int] = {}
    for row in results:
        key = str(row["status"]) if row["status"] else "ERROR"
        by_status[key] = by_status.get(key, 0) + 1
    out = {"run": "SURFACE-" + secrets.token_hex(3).upper(), "base": BASE,
           "operations": len(operations), "responses": by_status,
           "failures": failures, "total_5xx": len(failures)}
    (EVIDENCE / "surface-sweep.json").write_text(json.dumps(out, indent=2))
    print(json.dumps({"operations": len(operations), "responses": by_status, "5xx": len(failures)}, indent=2))
    for row in failures[:30]:
        print("FAIL", row["method"], row["path"], row["status"], row["error"], row["body"][:250])
    print(f"evidence: {EVIDENCE/'surface-sweep.json'}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
