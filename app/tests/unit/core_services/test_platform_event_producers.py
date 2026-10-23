"""FS-00 producer/route wiring coverage (unit; no DB).

The central emitter validates payload required keys against the committed builder schemas; the new
internal router and the `POST /admin/users` route close the declared FS-00 runtime gaps. The DB-level
producer behaviour is exercised in `integration/platform_data/test_platform_event_producers.py`.
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import FastAPI

from app.api.routers.platform import internal as internal_router
from app.api.routers.platform import rbac as rbac_router
from app.core.platform_events import required_payload_keys

ROOT = Path(__file__).resolve().parents[4]
EVENT_DIR = ROOT / "app/contracts/platform/events"


def test_every_platform_builder_schema_has_payload_required_keys() -> None:
    schemas = sorted(EVENT_DIR.glob("*.schema.json"))
    assert len(schemas) == 33
    for schema_path in schemas:
        event_type = json.loads(schema_path.read_text())["x-event-type"]
        assert required_payload_keys(event_type), event_type


def test_internal_router_exposes_declared_unprefixed_operations() -> None:
    app = FastAPI()
    app.include_router(internal_router.router)
    paths = set(app.openapi()["paths"])
    assert "/internal/deps" in paths
    assert "/internal/outbox/replay" in paths
    assert "/internal/finance/exchange-rates" in paths
    assert "/internal/finance/fiscal-periods" in paths
    assert "/internal/order-boundary/demand-doubles" in paths
    assert "/openapi.json" not in paths  # hidden to keep the canonical runtime OpenAPI path only


def test_admin_users_create_route_is_declared() -> None:
    app = FastAPI()
    app.include_router(rbac_router.router)
    operations = app.openapi()["paths"]["/api/v1/admin/users"]
    assert "post" in operations
    from app.api.routers.platform.rbac import UserCreate
    assert "email" in UserCreate.model_fields and "identity_id" in UserCreate.model_fields
