"""Δ-REUSE unbuild dispositions (exit gate): RETURN_TO_STOCK/SCRAP/HOLD, used condition/status,
resale flag, ORIGINAL_COST/NRV/MANUAL valuation, serial/lot validation and full-only reversal."""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest

pytestmark = pytest.mark.integration

TODAY = date.today().isoformat()


async def _bom(client, world, *, code: str, qty_per: str = "2") -> str:
    headers = world["headers"]
    bom = await client.post(
        "/api/v1/boms",
        json={
            "assembly_item_id": str(world["assembly_item_id"]),
            "bom_code": code,
            "name": code,
        },
        headers=headers,
    )
    await client.post(f"/api/v1/boms/{bom.json()['bom']['id']}/activate", headers=headers)
    revision = await client.post(
        f"/api/v1/boms/{bom.json()['bom']['id']}/revisions",
        json={
            "revision_code": "R1",
            "effective_from": "2020-01-01",
        },
        headers=headers,
    )
    revision_id = revision.json()["revision"]["id"]
    for line_number, item_id, per in ((1, world["component_item_id"], qty_per), (2, world["other_item_id"], "1")):
        response = await client.post(
            f"/api/v1/bom-revisions/{revision_id}/components",
            json={
                "line_number": line_number,
                "component_item_id": str(item_id),
                "qty_per": per,
                "uom_id": str(world["uom_id"]),
            },
            headers=headers,
        )
        assert response.status_code == 201, response.text
    assert (await client.post(f"/api/v1/bom-revisions/{revision_id}/activate", headers=headers)).status_code == 200
    return revision_id


async def _condition_id(client, world, code: str) -> str:
    response = await client.get("/api/v1/manufacturing/condition-codes", headers=world["headers"])
    assert response.status_code == 200, response.text
    return next(row["id"] for row in response.json()["items"] if row["code"] == code)


async def _unbuild(client, world, lines: list[dict[str, Any]], **extra: Any):
    payload = {
        "assembly_item_id": str(world["assembly_item_id"]),
        "qty_unbuilt": "1",
        "location_id": str(world["location_id"]),
        "lines": lines,
        **extra,
    }
    return await client.post("/api/v1/assembly-unbuilds", json=payload, headers=world["headers"])


def _disposition_line(world, item_id, **overrides: Any) -> dict[str, Any]:
    return {"component_item_id": str(item_id), **overrides}


async def test_return_hold_scrap_dispositions(client, world) -> None:
    await _bom(client, world, code="UB-BOM")
    used_a = await _condition_id(client, world, "USED_A")
    created = await _unbuild(
        client,
        world,
        [
            _disposition_line(
                world,
                world["component_item_id"],
                disposition="RETURN_TO_STOCK",
                is_used=True,
                condition_code_id=used_a,
                return_status_id=str(world["used_status_id"]),
                allow_resale=True,
                value_policy="ORIGINAL_COST",
                unit_cost="2",
                unit_value="4",
            ),
            _disposition_line(world, world["other_item_id"], disposition="SCRAP"),
        ],
    )
    assert created.status_code == 201, created.text
    unbuild = created.json()["unbuild"]
    component_line = next(
        line for line in unbuild["lines"] if line["component_item_id"] == str(world["component_item_id"])
    )
    scrap_line = next(line for line in unbuild["lines"] if line["component_item_id"] == str(world["other_item_id"]))
    assert component_line["disposition"] == "RETURN_TO_STOCK"
    assert component_line["is_used"] is True
    assert component_line["condition_code_id"] == used_a
    assert component_line["return_status_id"] == str(world["used_status_id"])
    assert component_line["allow_resale"] is True
    assert component_line["value_policy"] == "ORIGINAL_COST"
    assert scrap_line["disposition"] == "SCRAP"

    posted = await client.post(f"/api/v1/assembly-unbuilds/{unbuild['id']}/post", headers=world["headers"])
    assert posted.status_code == 200, posted.text
    call = world["posting"].calls[-1]
    assert call["posting_type"] == "T-16"
    movement_items = {movement["item_id"] for movement in call["movements"]}
    assert str(world["component_item_id"]) in movement_items
    assert str(world["other_item_id"]) not in movement_items  # scrap never returns stock
    assert str(world["assembly_item_master_id"]) in movement_items
    assert posted.json()["unbuild"]["status"] == "POSTED"


async def test_nrv_manual_valuation_and_disposition_guards(client, world) -> None:
    await _bom(client, world, code="UB-VAL")
    created = await _unbuild(
        client,
        world,
        [
            _disposition_line(
                world,
                world["component_item_id"],
                disposition="HOLD",
                value_policy="NRV",
                return_status_id=str(world["hold_status_id"]),
                unit_cost="2",
            ),
            _disposition_line(
                world,
                world["other_item_id"],
                disposition="RETURN_TO_STOCK",
                value_policy="MANUAL",
                unit_value="7",
                unit_cost="3",
            ),
        ],
    )
    assert created.status_code == 201, created.text
    lines = {line["component_item_id"]: line for line in created.json()["unbuild"]["lines"]}
    nrv = lines[str(world["component_item_id"])]
    manual = lines[str(world["other_item_id"])]
    assert nrv["value_policy"] == "NRV" and nrv["disposition"] == "HOLD"
    assert manual["value_policy"] == "MANUAL" and manual["unit_value"] == "7.000000000000"

    manual_missing = await _unbuild(
        client,
        world,
        [
            _disposition_line(world, world["component_item_id"], disposition="RETURN_TO_STOCK", value_policy="MANUAL"),
            _disposition_line(world, world["other_item_id"], disposition="SCRAP"),
        ],
    )
    assert manual_missing.status_code == 422, manual_missing.text
    assert manual_missing.json()["code"] == "UNBUILD_DISPOSITION_INVALID"

    scrap_with_stock_return = await _unbuild(
        client,
        world,
        [
            _disposition_line(
                world, world["component_item_id"], disposition="SCRAP", return_status_id=str(world["used_status_id"])
            ),
            _disposition_line(world, world["other_item_id"], disposition="SCRAP"),
        ],
    )
    assert scrap_with_stock_return.status_code == 422

    serial_mismatch = await _unbuild(
        client,
        world,
        [
            _disposition_line(
                world,
                world["component_item_id"],
                disposition="RETURN_TO_STOCK",
                serial_ids=["serial-1", "serial-2", "serial-3"],
            ),
        ],
    )
    assert serial_mismatch.status_code == 422
    assert serial_mismatch.json()["code"] == "UNBUILD_DISPOSITION_INVALID"


async def test_unbuild_full_reversal_only(client, world) -> None:
    await _bom(client, world, code="UB-REV")
    created = await _unbuild(
        client,
        world,
        [
            _disposition_line(world, world["component_item_id"], disposition="RETURN_TO_STOCK"),
            _disposition_line(world, world["other_item_id"], disposition="RETURN_TO_STOCK"),
        ],
    )
    unbuild_id = created.json()["unbuild"]["id"]
    assert (
        await client.post(f"/api/v1/assembly-unbuilds/{unbuild_id}/post", headers=world["headers"])
    ).status_code == 200
    reversed_response = await client.post(
        f"/api/v1/assembly-unbuilds/{unbuild_id}/reverse",
        json={"reason": "wrong disposition"},
        headers=world["headers"],
    )
    assert reversed_response.status_code == 200, reversed_response.text
    assert reversed_response.json()["unbuild"]["status"] == "REVERSED"
    second = await client.post(f"/api/v1/assembly-unbuilds/{unbuild_id}/reverse", json={}, headers=world["headers"])
    assert second.status_code == 409, second.text
