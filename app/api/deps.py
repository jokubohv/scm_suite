"""Shared FastAPI dependencies (FS-00 extends with auth/tenant/DB)."""

from __future__ import annotations

from functools import lru_cache

from app.core.config import Settings, get_settings


@lru_cache(maxsize=1)
def _settings() -> Settings:
    return get_settings()


def settings_dep() -> Settings:
    return _settings()
