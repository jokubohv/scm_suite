"""FS-01 consumes the FS-00 platform contracts (platform.core + platform.events).

Entry-gate companion: verifies the platform surfaces FS-01 depends on exist and are importable, and that
``item.master@1.0.0`` declares its dependency range without taking a SQL/HTTP dependency on FS-00 internals.
"""

from __future__ import annotations

import json
from pathlib import Path

from app.core.idempotency_store import IdempotencyStore
from app.core.outbox import emit, event_envelope
from app.core.security.scopes import ScopeRegistry

REPO_ROOT = Path(__file__).resolve().parents[3]


def test_platform_core_surfaces_used_by_fs01_exist() -> None:
    assert callable(emit) and callable(event_envelope)
    assert IdempotencyStore is not None
    registry = ScopeRegistry()
    for scope in ("items.create", "items.read", "items.update", "items.uom.manage", "items.planning.manage"):
        assert registry.catalogue.scope(scope) is not None, scope


def test_manifest_declares_platform_dependencies_for_item_master() -> None:
    manifest = json.loads((REPO_ROOT / "design" / "feature-sets.json").read_text(encoding="utf-8"))
    fs01 = next(entry for entry in manifest["feature_sets"] if entry.get("id") == "FS-01")
    dependencies = {(dep["id"], dep["contract"]): dep["version"] for dep in fs01["depends_on"]}
    assert ("FS-00", "platform.core") in dependencies
    assert ("FS-00", "platform.events") in dependencies
    assert dependencies[("FS-00", "platform.core")] == ">=1.0.0 <2.0.0"
    assert fs01["provides"] == [{"contract": "item.master", "version": "1.0.0"}]
