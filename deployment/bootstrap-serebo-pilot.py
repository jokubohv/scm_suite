"""One-shot operator bootstrap: tenant + admin + Serebo operational API key.

Runs inside the SCM API container. Inputs are environment variables; output is one JSON document.
Secrets (admin password and API key) are emitted only to the workflow artifact, never to logs.
"""
from __future__ import annotations

import json
import os
import secrets
import urllib.error
import urllib.request

BASE = os.environ.get("SCM_INTERNAL_BASE", "http://127.0.0.1:8000")


def call(method: str, path: str, body: dict | None = None, headers: dict | None = None) -> dict:
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(BASE + path, data=data, method=method)
    request.add_header("Content-Type", "application/json")
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise RuntimeError(f"{method} {path} failed HTTP {exc.code}: {detail[:800]}") from None


def main() -> int:
    token = os.environ["SUPER_ADMIN_TOKEN"]
    code = os.environ["TENANT_CODE"]
    name = os.environ["TENANT_NAME"]
    email = os.environ["ADMIN_EMAIL"]
    password = os.environ.get("ADMIN_PASSWORD") or secrets.token_urlsafe(24)
    boot = call(
        "POST",
        "/api/v1/bootstrap/tenants",
        {
            "code": code,
            "name": name,
            "timezone": "America/La_Paz",
            "base_currency_code": "BOB",
            "negative_inventory_policy": "BLOCK",
            "rounding_mode": "PER_MOVEMENT",
            "retention_ledger_years": 7,
            "admin_email": email,
            "admin_display_name": "Administrador Intel Ranch (sintético)",
            "admin_password": password,
            "admin_role_codes": ["admin"],
        },
        {"X-Super-Admin-Token": token, "Idempotency-Key": f"bootstrap-{code}-v1"},
    )
    tenant_id = boot["tenant_id"]
    login = call("POST", "/api/v1/auth/login", {"email": email, "password": password, "tenant": code})
    access = login["access_token"]
    scopes = json.load(open("/tmp/serebo_pilot_scopes.json", encoding="utf-8"))
    key = call(
        "POST",
        "/api/v1/admin/api-keys",
        {"name": f"{code}-serebo-full", "scopes": scopes, "expires_at": "2027-10-27T00:00:00Z"},
        {"Authorization": f"Bearer {access}"},
    )
    print(json.dumps({
        "tenant_id": tenant_id,
        "tenant_code": code,
        "tenant_name": name,
        "admin_email": email,
        "admin_password": password,
        "api_key_name": key.get("name"),
        "api_key_id": key.get("id"),
        "api_key": key.get("secret"),
        "scopes": len(scopes),
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
