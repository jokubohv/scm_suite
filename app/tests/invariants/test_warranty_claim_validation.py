"""Invariant DM-EXT-01/02 (FS-14 exit gate): claim approval only in-warranty or with explicit override;
serialized items require a serial identity."""

from __future__ import annotations

import pytest

from app.modules.extensions.errors import SerialRequired, ValidationRequired
from app.modules.extensions.testing import seed_extensions_world

pytestmark = pytest.mark.integration


async def test_serial_required_and_warranty_window_validation(db_session) -> None:
    world = await seed_extensions_world(db_session, code_prefix="INVWC")
    service = world["service"]
    with pytest.raises(SerialRequired):
        await service.create_registration({"item_id": str(world["serialized_item_id"]), "source": "MANUAL"})
    registration = await service.create_registration(
        {
            "item_id": str(world["item_id"]),
            "external_customer_ref": "C-1",
            "registered_at": "2026-01-01T00:00:00Z",
            "duration_months": 1,
        }
    )
    in_window = await service.create_claim(
        {
            "warranty_registration_id": registration["id"],
            "item_id": str(world["item_id"]),
            "claim_type": "REPAIR",
            "received_at": "2026-01-15T00:00:00Z",
        }
    )
    await service.validate_claim(in_window["id"])
    approved = await service.approve_claim(in_window["id"], approval_ref="APR-IN")
    assert approved["status"] == "APPROVED"

    out_of_window = await service.create_claim(
        {
            "warranty_registration_id": registration["id"],
            "item_id": str(world["item_id"]),
            "claim_type": "REFUND",
            "received_at": "2027-06-01T00:00:00Z",
        }
    )
    with pytest.raises(ValidationRequired):
        await service.approve_claim(out_of_window["id"])
    overridden = await service.approve_claim(out_of_window["id"], approval_ref="APR-OVR", override_reason="goodwill")
    assert overridden["status"] == "APPROVED"
    assert overridden["validation_result"]["override"]["approval_ref"] == "APR-OVR"
