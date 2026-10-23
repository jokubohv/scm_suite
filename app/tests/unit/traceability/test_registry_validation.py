"""Unit tests for FS-03 registry validation helpers and status-flag semantics (no database)."""

from __future__ import annotations

import pytest

from app.contracts.traceability.schemas import (
    BootstrapState,
    InventoryStatus,
    Location,
    PostingEligibility,
    StatusFlags,
)
from app.modules.traceability.errors import (
    DefaultStatusConflict,
    ValidationError,
)
from app.modules.traceability.service import (
    normalise_code,
    validate_bin_payload,
    validate_location_payload,
    validate_status_flags,
)


def test_normalise_code() -> None:
    assert normalise_code(" wh-main ") == "WH-MAIN"
    assert normalise_code("a.b-c_1") == "A.B-C_1"
    for bad in ("", "  ", "with space", "-leading", "a" * 65, None, 123):
        with pytest.raises(ValidationError):
            normalise_code(bad)


def test_location_create_validation() -> None:
    valid = {
        "code": "wh1",
        "name": "Main warehouse",
        "location_type": "warehouse",
        "is_bins_enabled": True,
        "is_statuses_enabled": True,
        "timezone": "America/New_York",
    }
    payload = validate_location_payload(valid)
    assert payload["code"] == "WH1"
    assert payload["location_type"] == "WAREHOUSE"
    assert payload["parent_id"] is None
    assert payload["is_bins_enabled"] is True

    with pytest.raises(ValidationError):
        validate_location_payload({**valid, "location_type": "MOON"})
    with pytest.raises(ValidationError):
        validate_location_payload({**valid, "code": "with space"})
    with pytest.raises(ValidationError):
        validate_location_payload({**valid, "is_unallocated": True})
    with pytest.raises(ValidationError):
        validate_location_payload({**valid, "timezone": "Mars/Olympus"})
    with pytest.raises(ValidationError):
        validate_location_payload({**valid, "is_consignment_allowed": True, "is_statuses_enabled": False})


def test_location_update_immutables() -> None:
    assert validate_location_payload({"name": "New name"}, update=True) == {"name": "New name"}
    with pytest.raises(ValidationError):
        validate_location_payload({"code": "CHANGED"}, update=True)
    with pytest.raises(ValidationError):
        validate_location_payload({"location_type": "STORE"}, update=True)
    with pytest.raises(ValidationError):
        validate_location_payload({"is_consignment_allowed": True, "is_statuses_enabled": False}, update=True)


def test_bin_validation() -> None:
    payload = validate_bin_payload(
        {"location_id": "a0000000-0000-7000-8000-000000000001", "code": "b-1", "name": "Bin", "bin_type": "storage"}
    )
    assert payload["code"] == "B-1"
    assert payload["bin_type"] == "STORAGE"
    assert payload["capacity_qty"] is None
    with pytest.raises(ValidationError):
        validate_bin_payload({"location_id": "x", "code": "B1", "name": "Bin"})
    with pytest.raises(ValidationError):
        validate_bin_payload(
            {"location_id": "a0000000-0000-7000-8000-000000000001", "code": "B1", "name": "n", "capacity_qty": 0}
        )
    with pytest.raises(ValidationError):
        validate_bin_payload(
            {"location_id": "a0000000-0000-7000-8000-000000000001", "code": "B1", "name": "n"}, update=True
        )


def test_status_flag_matrix() -> None:
    validate_status_flags(
        is_available_for_allocation=True,
        is_committable=True,
        is_sellable=True,
        is_consigned=False,
        is_default=True,
        commit_non_available=False,
    )
    with pytest.raises(DefaultStatusConflict):
        validate_status_flags(
            is_available_for_allocation=True,
            is_committable=True,
            is_sellable=False,
            is_consigned=True,
            is_default=True,
            commit_non_available=False,
        )
    with pytest.raises(ValidationError):
        validate_status_flags(
            is_available_for_allocation=False,
            is_committable=True,
            is_sellable=False,
            is_consigned=False,
            is_default=False,
            commit_non_available=False,
        )
    # tenant opt-in allows committable non-available stock (e.g. committable transit/quarantine)
    validate_status_flags(
        is_available_for_allocation=False,
        is_committable=True,
        is_sellable=False,
        is_consigned=False,
        is_default=False,
        commit_non_available=True,
    )


def test_contract_models_roundtrip() -> None:
    location = Location.model_validate(
        {
            "id": "a0000000-0000-7000-8000-000000000001",
            "code": "WH1",
            "name": "WH",
            "location_type": "WAREHOUSE",
            "is_bins_enabled": True,
            "is_statuses_enabled": True,
            "is_consignment_allowed": False,
            "allow_commit_from_non_available": False,
            "is_unallocated": False,
            "is_inactive": False,
            "timezone": "UTC",
            "version": 1,
        }
    )
    assert location.code == "WH1"
    status = StatusFlags(
        status_id="b",
        code="AVAILABLE",
        available_for_allocation=True,
        committable=True,
        sellable=True,
        consigned=False,
        default=True,
        active=True,
    )
    assert status.committable is True
    bootstrap = BootstrapState(
        tenant_id="a0000000-0000-7000-8000-000000000001",
        sentinel_present=True,
        sentinel_location_id="s",
        status_count=7,
        default_status_code="AVAILABLE",
        seeded=True,
    )
    assert bootstrap.status_count == 7
    status_row = InventoryStatus(
        id="b",
        code="HOLD",
        name="Hold",
        is_available_for_allocation=False,
        is_committable=False,
        is_sellable=False,
        is_consigned=False,
        is_default=False,
        is_system=False,
        is_active=True,
        version=1,
    )
    assert status_row.is_committable is False
    eligible = PostingEligibility(
        item_id="i",
        location_id="l",
        eligible=True,
        normalized={},
        bins_enabled=True,
        statuses_enabled=True,
    )
    assert eligible.eligible is True
