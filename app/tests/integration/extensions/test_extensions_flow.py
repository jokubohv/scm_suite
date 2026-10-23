"""FS-14 integration: warranty terms/registration/claim/action, customer parts and Auto Assign UPC."""

from __future__ import annotations

import pytest

from app.modules.extensions.service import upc_check_digit

pytestmark = pytest.mark.integration


def _upc(data: str) -> str:
    return data + str(upc_check_digit(data))


async def test_warranty_registration_claim_action_flow(client, world, db_session) -> None:
    terms = await client.put(
        f"/api/v1/items/{world['item_id']}/warranty",
        json={"duration_months": 24, "terms": "standard", "coverage": {"parts": True, "labor": True}},
        headers=world["headers"],
    )
    assert terms.status_code == 200, terms.text
    registration = await client.post(
        "/api/v1/warranty/registrations",
        json={
            "item_id": str(world["item_id"]),
            "external_customer_ref": "CUST-1",
            "external_invoice_ref": "INV-1",
            "registered_at": "2026-01-15T00:00:00Z",
            "source": "MANUAL",
        },
        headers=world["headers"],
    )
    assert registration.status_code == 201, registration.text
    registration_id = registration.json()["id"]
    assert registration.json()["warranty_end"] == "2028-01-15"
    coverage = await client.get(
        "/api/v1/warranty/coverage",
        params={"item_id": str(world["item_id"]), "as_of": "2027-01-01"},
        headers=world["headers"],
    )
    assert coverage.status_code == 200 and coverage.json()["covered"] is True, coverage.text

    claim = await client.post(
        "/api/v1/warranty/claims",
        json={
            "warranty_registration_id": registration_id,
            "item_id": str(world["item_id"]),
            "claim_type": "REPAIR",
            "received_at": "2027-02-01T00:00:00Z",
            "description": "broken",
        },
        headers=world["headers"],
    )
    assert claim.status_code == 201, claim.text
    claim_id = claim.json()["id"]
    validated = await client.post(f"/api/v1/warranty/claims/{claim_id}/validate", json={}, headers=world["headers"])
    assert validated.status_code == 200 and validated.json()["pass"] is True, validated.text
    approved = await client.post(
        f"/api/v1/warranty/claims/{claim_id}/approve", json={"approval_ref": "APR-1"}, headers=world["headers"]
    )
    assert approved.status_code == 200 and approved.json()["status"] == "APPROVED", approved.text
    action = await client.post(
        f"/api/v1/warranty/claims/{claim_id}/actions",
        json={"action_type": "REPAIR", "status": "DONE", "notes": "fixed"},
        headers=world["headers"],
    )
    assert action.status_code == 200, action.text
    completed = await client.post(f"/api/v1/warranty/claims/{claim_id}/complete", json={}, headers=world["headers"])
    assert completed.status_code == 200 and completed.json()["status"] == "COMPLETED", completed.text


async def test_out_of_warranty_claim_requires_override(client, world) -> None:
    registration = await client.post(
        "/api/v1/warranty/registrations",
        json={
            "item_id": str(world["item_id"]),
            "external_customer_ref": "CUST-2",
            "registered_at": "2026-01-01T00:00:00Z",
            "duration_months": 1,
        },
        headers=world["headers"],
    )
    assert registration.status_code == 201, registration.text
    claim = await client.post(
        "/api/v1/warranty/claims",
        json={
            "warranty_registration_id": registration.json()["id"],
            "item_id": str(world["item_id"]),
            "claim_type": "REFUND",
            "received_at": "2027-01-01T00:00:00Z",
        },
        headers=world["headers"],
    )
    assert claim.status_code == 201, claim.text
    claim_body = await client.get(f"/api/v1/warranty/claims/{claim.json()['id']}", headers=world["headers"])
    assert claim_body.json()["validation_result"]["pass"] is False
    denied = await client.post(
        f"/api/v1/warranty/claims/{claim.json()['id']}/approve", json={}, headers=world["headers"]
    )
    assert denied.status_code == 409, denied.text
    override = await client.post(
        f"/api/v1/warranty/claims/{claim.json()['id']}/approve",
        json={"approval_ref": "OVR-1", "override_reason": "goodwill"},
        headers=world["headers"],
    )
    assert override.status_code == 200 and override.json()["status"] == "APPROVED", override.text


async def test_customer_parts_and_upc_registry(client, world, db_session) -> None:
    part = await client.post(
        "/api/v1/customer-parts",
        json={
            "item_id": str(world["item_id"]),
            "external_customer_ref": "CUST-9",
            "customer_part_number": "CP-100",
            "description": "customer view",
        },
        headers=world["headers"],
    )
    assert part.status_code == 201, part.text
    duplicate = await client.post(
        "/api/v1/customer-parts",
        json={"item_id": str(world["item_id"]), "external_customer_ref": "CUST-9", "customer_part_number": "CP-100"},
        headers=world["headers"],
    )
    assert duplicate.status_code == 409 and duplicate.json()["code"] == "CUSTOMER_PART_DUPLICATE"
    resolved = await client.get(
        "/api/v1/customer-parts/resolve",
        params={"external_customer_ref": "CUST-9", "part_number": "CP-100"},
        headers=world["headers"],
    )
    assert resolved.status_code == 200 and resolved.json()["item_id"] == str(world["item_id"])

    code = _upc("12345600001")
    assigned = await client.post(
        "/api/v1/upc-registry",
        json={"item_id": str(world["item_id"]), "upc_code": code, "assignment_source": "MANUAL"},
        headers=world["headers"],
    )
    assert assigned.status_code == 201, assigned.text
    registry_id = assigned.json()["id"]
    mirrored = await db_session.execute(
        __import__("sqlalchemy").text("SELECT upc_code FROM item_master.item WHERE tenant_id=:t AND id=:i"),
        {"t": world["tenant_id"], "i": world["item_id"]},
    )
    assert mirrored.scalar_one() == code
    retired = await client.post(
        f"/api/v1/upc-registry/{registry_id}/retire", json={"reason": "discontinued"}, headers=world["headers"]
    )
    assert retired.status_code == 200 and retired.json()["status"] == "RETIRED"
    cleared = await db_session.execute(
        __import__("sqlalchemy").text("SELECT upc_code FROM item_master.item WHERE tenant_id=:t AND id=:i"),
        {"t": world["tenant_id"], "i": world["item_id"]},
    )
    assert cleared.scalar_one() is None
    reuse = await client.post(
        "/api/v1/upc-registry", json={"item_id": str(world["item_id"]), "upc_code": code}, headers=world["headers"]
    )
    assert reuse.status_code == 409 and reuse.json()["code"] == "UPC_RETIRED_NEVER_REUSABLE"
    validation = await client.get("/api/v1/upc-registry/validate", params={"upc_code": code}, headers=world["headers"])
    assert validation.status_code == 200 and validation.json()["valid"] is False
    monitoring = await client.get("/api/v1/upc-registry/monitoring", headers=world["headers"])
    assert monitoring.status_code == 200 and monitoring.json()["retired"] >= 1

    auto = await client.post(
        "/api/v1/upc-registry",
        json={"item_id": str(world["serialized_item_id"]), "assignment_source": "AUTO"},
        headers=world["headers"],
    )
    assert auto.status_code == 201, auto.text
    assert len(auto.json()["upc_code"]) == 12
