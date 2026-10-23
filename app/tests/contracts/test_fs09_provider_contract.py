"""FS-09 provider contract: `manufacturing.execution@1.1.0` surface (API + events + facade + migrations).

Covers the additive Δ-CFG/Δ-REUSE contract bump: 24 events (incl. `assembly.unbuild.disposition.recorded`),
5 jobs, 42 tables across migrations 0018–0020, 116 implementation-backed operations with exactly one
catalogued scope each, and committed generated artefacts that are current (no skeleton TODO markers).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
CONTRACT_DIR = REPO_ROOT / "app" / "contracts" / "manufacturing"


def _json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _manifest() -> dict:
    return _json(REPO_ROOT / "design" / "feature-sets.json")


def _catalogue_scopes() -> set[str]:
    return {entry["scope"] for entry in _json(REPO_ROOT / "design" / "permissions.json")["catalogue"]}


def test_contract_metadata_versions_migrations_and_tables() -> None:
    from app.contracts.manufacturing.contract import CONTRACT_NAME, CONTRACT_VERSION
    from app.modules.manufacturing import tables
    from app.modules.manufacturing.facade import EVENT_TYPES, JOB_NAMES, ManufacturingFacade

    assert CONTRACT_NAME == "manufacturing.execution"
    assert CONTRACT_VERSION == "1.1.0"
    fs09 = next(feature_set for feature_set in _manifest()["feature_sets"] if feature_set["id"] == "FS-09")
    provides = {entry["contract"]: entry["version"] for entry in fs09["provides"]}
    assert provides == {"manufacturing.execution": "1.1.0"}
    assert fs09["migrations"] == [
        "alembic:0018_manufacturing_master",
        "alembic:0019_manufacturing_routing",
        "alembic:0020_manufacturing_execution",
    ]
    assert len(EVENT_TYPES) == 24 and len(set(EVENT_TYPES)) == 24
    assert "assembly.unbuild.disposition.recorded" in EVENT_TYPES  # Δ-REUSE event
    for event in (
        "manufacturing.config.validated",
        "manufacturing.config.violation",
        "manufacturing.compat.rule_set.activated",
    ):
        assert event in EVENT_TYPES
    assert len(JOB_NAMES) == 5
    for job in (
        "mfg.scheduling.run",
        "mfg.capacity.rollup",
        "mfg.wo.demand.sync",
        "mfg.wip.reconcile",
        "mfg.available_to_build.refresh",
    ):
        assert job in JOB_NAMES
    assert ManufacturingFacade is not None
    assert len(tables.ALL_TABLES) == 42
    assert len(tables.MASTER_TABLES) == 6 and len(tables.ROUTING_TABLES) == 9 and len(tables.EXECUTION_TABLES) == 27
    assert tables.APPEND_ONLY_TABLES == ("compat_configuration",)

    contract_meta = _json(CONTRACT_DIR / "contract.json")
    contracts = {entry["contract"]: entry for entry in contract_meta["contracts"]}
    entry = contracts["manufacturing.execution"]
    assert entry["version"] == "1.1.0"
    assert entry["feature_set"] == "FS-09"
    assert entry["fragment"] == "contracts/openapi/fragments/FS-09.openapi.json"
    event_names = {event["event"] for event in entry["events"]}
    # Spec-derived contract metadata predates the additive Δ-CFG/Δ-REUSE event; the builder/event
    # schema for it is committed and all metadata events must still be declared by the module.
    assert event_names <= set(EVENT_TYPES), sorted(event_names - set(EVENT_TYPES))
    assert "assembly.unbuild.disposition.recorded" in EVENT_TYPES
    assert all(event["schema"].startswith("contracts/events/manufacturing.execution/") for event in entry["events"])


def test_committed_generated_artefacts_are_current() -> None:
    from app.contracts.manufacturing.build_from_app import build_outputs

    outputs = build_outputs()
    assert len(outputs) == 25  # 1 fragment + 24 event schemas
    for path, payload in outputs.items():
        assert path.exists(), path
        assert _json(path) == payload, f"stale generated artefact: {path}"


def test_openapi_fragment_is_implementation_backed_and_scope_complete() -> None:
    from app.api.routers.manufacturing import ROUTE_SCOPES

    fragment = _json(CONTRACT_DIR / "openapi.fragment.json")
    assert fragment["openapi"] == "3.1.0"
    assert fragment["info"]["x-contract"] == "manufacturing.execution"
    assert fragment["info"]["x-feature-set"] == "FS-09"
    assert fragment["info"]["x-implemented-operations"] == sum(len(item) for item in fragment["paths"].values())
    assert fragment["info"]["x-implemented-operations"] == len(ROUTE_SCOPES)
    catalogue = _catalogue_scopes()
    operations: dict[tuple[str, str], dict] = {}
    for path, item in fragment["paths"].items():
        assert path.startswith("/api/v1/"), path
        for method, operation in item.items():
            operations[(method.upper(), path)] = operation
            assert operation.get("x-implemented") is True and operation.get("x-skeleton") is False
            assert operation.get("x-contract") == ["manufacturing.execution"]
            scopes = operation.get("x-required-scopes") or []
            assert len(scopes) == 1, (method, path, scopes)
            assert scopes[0] in catalogue, (method, path, scopes)
            assert scopes[0] == ROUTE_SCOPES[(method.upper(), path)]
            assert "TODO" not in json.dumps(operation)
            assert any(code in operation["responses"] for code in ("200", "201", "204"))
    assert set(operations) == set(ROUTE_SCOPES)
    assert len(operations) >= 110, len(operations)


def test_delta_scopes_and_disposition_contract_are_present() -> None:
    catalogue = _catalogue_scopes()
    for scope in (
        "manufacturing.compat.manage",
        "manufacturing.compat.validate",
        "manufacturing.compat.override",
        "manufacturing.unbuild.dispose",
    ):
        assert scope in catalogue, scope
    fragment = _json(CONTRACT_DIR / "openapi.fragment.json")
    unbuild_operations = [
        operation
        for path, item in fragment["paths"].items()
        if path.startswith("/api/v1/assembly-unbuilds")
        for method, operation in item.items()
        if method in {"post", "patch"}
    ]
    assert unbuild_operations
    used_scopes = {scope for operation in unbuild_operations for scope in operation["x-required-scopes"]}
    assert "manufacturing.post" in used_scopes or "manufacturing.write" in used_scopes


def test_event_schemas_validate_sample_envelopes() -> None:
    from app.contracts.manufacturing.build_from_app import ENVELOPE_REQUIRED, EVENT_FIELDS

    jsonschema = __import__("jsonschema")
    for event, fields in EVENT_FIELDS.items():
        path = CONTRACT_DIR / "events" / f"{event}.schema.json"
        schema = _json(path)
        assert schema["x-contract"] == "manufacturing.execution"
        assert schema["x-event-type"] == event
        assert schema["$id"].endswith(f"/{event}/1.0.0")
        sample = {
            "event_id": "00000000-0000-7000-8000-000000000001",
            "event_type": event,
            "event_version": "1.0.0",
            "tenant_id": "00000000-0000-7000-8000-000000000002",
            "occurred_at": "2026-01-01T00:00:00+00:00",
            "aggregate": {"type": "WORK_ORDER", "id": "00000000-0000-7000-8000-000000000003", "version": 1},
            "data": {
                field: ([] if field in {"lines", "components", "operations"} else "x")
                for field in fields
                if field != "tenant_id"
            },
        }
        jsonschema.validate(sample, schema)
        assert set(ENVELOPE_REQUIRED).issubset(schema["required"])
        sample["data"].pop(next(iter([field for field in fields if field != "tenant_id"])))
        try:
            jsonschema.validate(sample, schema)
        except jsonschema.ValidationError:
            pass
        else:  # pragma: no cover - schema must require documented key fields
            raise AssertionError(f"event {event} does not require documented payload fields")


def test_contract_builder_check_mode() -> None:
    from app.contracts.manufacturing import build_from_app

    assert build_from_app.main(["--check"]) == 0


if __name__ == "__main__":
    sys.exit(__import__("pytest").main([__file__, "-q"]))
