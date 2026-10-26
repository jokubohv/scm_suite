"""Runtime OpenAPI is a private surface in production and is gated by X-API-Key."""

from __future__ import annotations

import httpx
import pytest

from app.api.main import create_app
from app.core.config import get_settings

pytestmark = pytest.mark.asyncio


async def _client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")


def _reset_settings() -> None:
    get_settings.cache_clear()


async def test_production_openapi_requires_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    key = "k" * 48
    monkeypatch.setenv("ENV", "production")
    monkeypatch.setenv("OPENAPI_API_KEY", key)
    _reset_settings()
    try:
        app = create_app()
        async with await _client(app) as client:
            no_key = await client.get("/api/v1/openapi.json")
            assert no_key.status_code == 401
            assert no_key.json()["code"] == "API_KEY_REQUIRED"

            wrong = await client.get("/api/v1/openapi.json", headers={"X-API-Key": "wrong"})
            assert wrong.status_code == 401

            allowed = await client.get("/api/v1/openapi.json", headers={"X-API-Key": key})
            assert allowed.status_code == 200
            assert allowed.json()["openapi"].startswith("3.1")
            assert allowed.headers["cache-control"] == "no-store, private"
            assert "/api/v1/openapi.json" not in allowed.json()["paths"]

            assert (await client.get("/api/v1/docs")).status_code == 404
    finally:
        monkeypatch.delenv("OPENAPI_API_KEY", raising=False)
        monkeypatch.delenv("ENV", raising=False)
        _reset_settings()


async def test_production_openapi_fails_closed_without_configured_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENV", "production")
    monkeypatch.delenv("OPENAPI_API_KEY", raising=False)
    _reset_settings()
    try:
        app = create_app()
        async with await _client(app) as client:
            response = await client.get("/api/v1/openapi.json")
            assert response.status_code == 503
            assert response.json()["code"] == "OPENAPI_KEY_NOT_CONFIGURED"
    finally:
        monkeypatch.delenv("ENV", raising=False)
        _reset_settings()


async def test_prod_alias_also_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENV", "prod")
    monkeypatch.delenv("OPENAPI_API_KEY", raising=False)
    _reset_settings()
    try:
        app = create_app()
        async with await _client(app) as client:
            response = await client.get("/api/v1/openapi.json")
            assert response.status_code == 503
            assert response.json()["code"] == "OPENAPI_KEY_NOT_CONFIGURED"
    finally:
        monkeypatch.delenv("ENV", raising=False)
        _reset_settings()
