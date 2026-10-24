"""Super-admin bootstrap credential and public-registration guard tests (no database)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from fastapi import HTTPException

from app.api import deps
from app.core.config import get_settings

ROOT = Path(__file__).resolve().parents[3]
TOKEN = "test-super-admin-token-" + "x" * 40


def _clear_settings() -> None:
    get_settings.cache_clear()
    deps._settings.cache_clear()


def test_super_admin_guard_accepts_header_and_bearer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUPER_ADMIN_TOKEN", TOKEN)
    _clear_settings()
    asyncio.run(deps.require_super_admin(x_super_admin_token=TOKEN, authorization=None))
    asyncio.run(deps.require_super_admin(x_super_admin_token=None, authorization=f"Bearer {TOKEN}"))


def test_super_admin_guard_rejects_missing_or_wrong(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUPER_ADMIN_TOKEN", TOKEN)
    _clear_settings()
    for supplied in (None, "", "wrong-token", TOKEN[:-1]):
        with pytest.raises(HTTPException) as exc:
            asyncio.run(deps.require_super_admin(x_super_admin_token=supplied, authorization=None))
        assert exc.value.status_code == 401


def test_super_admin_guard_disabled_when_unset_or_short(monkeypatch: pytest.MonkeyPatch) -> None:
    for value in ("", "short"):
        monkeypatch.setenv("SUPER_ADMIN_TOKEN", value)
        _clear_settings()
        with pytest.raises(HTTPException) as exc:
            asyncio.run(deps.require_super_admin(x_super_admin_token=value, authorization=None))
        assert exc.value.status_code == 503


def test_account_creation_guard_is_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ACCOUNT_CREATION_SUPER_ADMIN_ONLY", "false")
    _clear_settings()
    asyncio.run(deps.require_account_creation_authority(x_super_admin_token=None, authorization=None))
    monkeypatch.setenv("ACCOUNT_CREATION_SUPER_ADMIN_ONLY", "true")
    monkeypatch.setenv("SUPER_ADMIN_TOKEN", TOKEN)
    _clear_settings()
    with pytest.raises(HTTPException):
        asyncio.run(deps.require_account_creation_authority(x_super_admin_token=None, authorization=None))
    asyncio.run(deps.require_account_creation_authority(x_super_admin_token=TOKEN, authorization=None))


def test_public_allowlist_contains_no_open_registration() -> None:
    catalogue = json.loads((ROOT / "design/permissions.json").read_text(encoding="utf-8"))
    public = [str(entry) for entry in catalogue["public_allowlist"]]
    forbidden = ("register", "signup", "sign-up", "bootstrap/users", "admin/users", "platform/tenants", "create")
    for entry in public:
        lowered = entry.lower()
        assert not any(token in lowered for token in forbidden), entry
    assert any("invitations/{token}/accept" in entry for entry in public), public
