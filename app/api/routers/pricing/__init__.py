"""FS-02 pricing API router package (exposes ``router`` for the integrator).

The implementation lives in :mod:`app.api.routers.pricing.router` (64 FS-02 §6 operations); this package
re-exports ``router`` and ``ROUTE_SCOPES`` so `app.api.main`/contract tooling can import either form.
"""

from __future__ import annotations

from app.api.routers.pricing.router import ROUTE_SCOPES, router

__all__ = ["ROUTE_SCOPES", "router"]
