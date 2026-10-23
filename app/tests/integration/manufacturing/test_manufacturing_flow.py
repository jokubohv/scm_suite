"""FS-09 end-to-end document flow: BOM → WO lifecycle → WIP flow, build/unbuild posting capture."""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest

pytestmark = pytest.mark.integration

TODAY = date.today().isoformat()


async def _create_active_bom(client, world, *, code: str = "BOM-A") -> dict[str, Any]:
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
    assert bom.status_code == 201, bom.text
    bom_id = bom.json()["bom"]["id"]
    revision = await client.post(
        f"/api/v1/boms/{bom_id}/revisions",
        json={
            "revision_code": "R1",
            "effective_from": "2020-01-01",
        },
        headers=headers,
    )
    assert revision.status_code == 201, revision.text
    revision_id = revision.json()["revision"]["id"]
    await client.post(f"/api/v1/boms/{bom_id}/activate", headers=headers)
    component = await client.post(
        f"/api/v1/bom-revisions/{revision_id}/components",
        json={
            "line_number": 1,
            "component_item_id": str(world["component_item_id"]),
            "qty_per": "2",
            "uom_id": str(world["uom_id"]),
        },
        headers=headers,
    )
    assert component.status_code == 201, component.text
    activated = await client.post(f"/api/v1/bom-revisions/{revision_id}/activate", headers=headers)
    assert activated.status_code == 200, activated.text
    return {"bom_id": bom_id, "revision_id": revision_id, "component_id": component.json()["component"]["id"]}


async def test_bom_snapshot_and_work_order_lifecycle(client, world) -> None:
    await _create_active_bom(client, world)
    headers = world["headers"]
    created = await client.post(
        "/api/v1/work-orders",
        json={
            "assembly_item_id": str(world["assembly_item_id"]),
            "qty_ordered": "10",
            "start_date": TODAY,
            "end_date": TODAY,
        },
        headers=headers,
    )
    assert created.status_code == 201, created.text
    work_order = created.json()["work_order"]
    assert work_order["status"] == "PLANNED"
    assert len(work_order["components"]) == 1
    assert work_order["components"][0]["qty_required"] == "20.000000000000"
    work_order_id = work_order["id"]

    assert (await client.post(f"/api/v1/work-orders/{work_order_id}/firm", headers=headers)).status_code == 200
    released = await client.post(f"/api/v1/work-orders/{work_order_id}/release", headers=headers)
    assert released.status_code == 200, released.text
    assert released.json()["work_order"]["status"] == "RELEASED"
    assert len(world["posting"].commitments) == 1

    issued = await client.post(f"/api/v1/work-orders/{work_order_id}/issue", json={}, headers=headers)
    assert issued.status_code == 200, issued.text
    assert world["posting"].calls[-1]["posting_type"] == "T-17"
    detail = await client.get(f"/api/v1/work-orders/{work_order_id}", headers=headers)
    assert detail.json()["work_order"]["components"][0]["qty_issued"] == "20.000000000000"

    completed = await client.post(
        f"/api/v1/work-orders/{work_order_id}/complete",
        json={
            "qty": "4",
            "value_completed": "40",
        },
        headers=headers,
    )
    assert completed.status_code == 200, completed.text
    assert world["posting"].calls[-1]["posting_type"] == "T-18"
    balance = await client.get(f"/api/v1/work-orders/{work_order_id}/wip-balance", headers=headers)
    assert balance.json()["qty_completed"] == "4.000000000000"

    rest = await client.post(f"/api/v1/work-orders/{work_order_id}/complete", json={"qty": "6"}, headers=headers)
    assert rest.status_code == 200
    detail = await client.get(f"/api/v1/work-orders/{work_order_id}", headers=headers)
    assert detail.json()["work_order"]["status"] == "BUILT"
    closed = await client.post(f"/api/v1/work-orders/{work_order_id}/close", json={}, headers=headers)
    assert closed.status_code == 200, closed.text
    assert closed.json()["work_order"]["status"] == "CLOSED"
    assert len(world["posting"].reversals) == 0


async def test_phantom_yield_and_roundup_explosion(client, world) -> None:
    headers = world["headers"]
    # Phantom assembly: PHM contains 3x leaf.
    phantom_bom = await client.post(
        "/api/v1/boms",
        json={
            "assembly_item_id": str(world["phantom_assembly_id"]),
            "bom_code": "PHM-BOM",
            "name": "Phantom BOM",
        },
        headers=headers,
    )
    phantom_bom_id = phantom_bom.json()["bom"]["id"]
    await client.post(f"/api/v1/boms/{phantom_bom_id}/activate", headers=headers)
    phantom_revision = await client.post(
        f"/api/v1/boms/{phantom_bom_id}/revisions",
        json={
            "revision_code": "R1",
            "effective_from": "2020-01-01",
        },
        headers=headers,
    )
    await client.post(
        f"/api/v1/bom-revisions/{phantom_revision.json()['revision']['id']}/components",
        json={
            "line_number": 1,
            "component_item_id": str(world["component_item_id"]),
            "qty_per": "3",
            "uom_id": str(world["uom_id"]),
        },
        headers=headers,
    )
    await client.post(f"/api/v1/bom-revisions/{phantom_revision.json()['revision']['id']}/activate", headers=headers)
    # Parent BOM: 2x phantom + 1x leaf with 80% yield and round-up.
    parent_bom = await client.post(
        "/api/v1/boms",
        json={
            "assembly_item_id": str(world["assembly_item_id"]),
            "bom_code": "PARENT",
            "name": "Parent",
        },
        headers=headers,
    )
    parent_bom_id = parent_bom.json()["bom"]["id"]
    await client.post(f"/api/v1/boms/{parent_bom_id}/activate", headers=headers)
    parent_revision = await client.post(
        f"/api/v1/boms/{parent_bom_id}/revisions",
        json={
            "revision_code": "R1",
            "effective_from": "2020-01-01",
        },
        headers=headers,
    )
    parent_revision_id = parent_revision.json()["revision"]["id"]
    phantom_line = await client.post(
        f"/api/v1/bom-revisions/{parent_revision_id}/components",
        json={
            "line_number": 1,
            "component_item_id": str(world["phantom_item_id"]),
            "qty_per": "2",
            "uom_id": str(world["uom_id"]),
            "is_phantom": True,
        },
        headers=headers,
    )
    assert phantom_line.status_code == 201, phantom_line.text
    yield_line = await client.post(
        f"/api/v1/bom-revisions/{parent_revision_id}/components",
        json={
            "line_number": 2,
            "component_item_id": str(world["component_item_id"]),
            "qty_per": "1",
            "uom_id": str(world["uom_id"]),
            "component_yield_percent": "80",
            "round_up_component_qty": True,
        },
        headers=headers,
    )
    assert yield_line.status_code == 201, yield_line.text
    activated_parent = await client.post(f"/api/v1/bom-revisions/{parent_revision_id}/activate", headers=headers)
    assert activated_parent.status_code == 200, activated_parent.text

    inquiry = await client.get(
        f"/api/v1/assemblies/{world['assembly_item_id']}/bom-inquiry", params={"qty": "5"}, headers=headers
    )
    assert inquiry.status_code == 200, inquiry.text
    lines = inquiry.json()["lines"]
    leaf_lines = [line for line in lines if line["component_item_id"] == str(world["component_item_id"])]
    assert leaf_lines, lines
    # phantom 5*2*3 = 30 plus 5*1/0.8 = 6.25 -> ceil 7 => 37
    total_leaf = sum(float(line["qty_required"]) for line in leaf_lines)
    assert abs(total_leaf - 37.0) < 1e-9, total_leaf

    work_order = await client.post(
        "/api/v1/work-orders",
        json={
            "assembly_item_id": str(world["assembly_item_id"]),
            "qty_ordered": "5",
            "start_date": TODAY,
            "end_date": TODAY,
        },
        headers=headers,
    )
    assert work_order.status_code == 201, work_order.text
    components = work_order.json()["work_order"]["components"]
    assert all(not component["is_phantom"] for component in components)
    leaf_component = [
        component for component in components if component["component_item_id"] == str(world["component_item_id"])
    ]
    assert leaf_component and sum(float(component["qty_required"]) for component in leaf_component) == 37.0


async def test_build_and_unbuild_posting_capture(client, world) -> None:
    await _create_active_bom(client, world, code="BOM-B")
    headers = world["headers"]
    build = await client.post(
        "/api/v1/assembly-builds",
        json={
            "assembly_item_id": str(world["assembly_item_id"]),
            "qty_built": "2",
            "location_id": str(world["location_id"]),
        },
        headers=headers,
    )
    assert build.status_code == 201, build.text
    build_id = build.json()["build"]["id"]
    posted = await client.post(f"/api/v1/assembly-builds/{build_id}/post", headers=headers)
    assert posted.status_code == 200, posted.text
    call = world["posting"].calls[-1]
    assert call["posting_type"] == "T-15"
    quantities = [float(movement["qty"]) for movement in call["movements"]]
    assert min(quantities) < 0 and max(quantities) > 0  # components consumed, assembly produced
    unbuild = await client.post(
        "/api/v1/assembly-unbuilds",
        json={
            "assembly_item_id": str(world["assembly_item_id"]),
            "qty_unbuilt": "1",
            "location_id": str(world["location_id"]),
            "lines": [{"component_item_id": str(world["component_item_id"]), "disposition": "RETURN_TO_STOCK"}],
        },
        headers=headers,
    )
    assert unbuild.status_code == 201, unbuild.text
    unbuild_id = unbuild.json()["unbuild"]["id"]
    unposted = await client.post(f"/api/v1/assembly-unbuilds/{unbuild_id}/post", headers=headers)
    assert unposted.status_code == 200, unposted.text
    assert world["posting"].calls[-1]["posting_type"] == "T-16"
    reversed_unbuild = await client.post(f"/api/v1/assembly-unbuilds/{unbuild_id}/reverse", json={}, headers=headers)
    assert reversed_unbuild.status_code == 200, reversed_unbuild.text
    assert len(world["posting"].reversals) == 1
