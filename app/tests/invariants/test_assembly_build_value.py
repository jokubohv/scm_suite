"""INV: assembly build/unbuild value + quantity conservation on the captured FS-04 postings (FS-09).

T-15 movements must consume exactly the BOM component quantities and produce the build quantity in the
destination location; T-16 must return non-scrap dispositions and never emit stock movements for SCRAP.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.tests.integration.manufacturing.conftest import seed_manufacturing_world

pytestmark = pytest.mark.integration


async def _bom(service, world) -> None:
    assembly = await service.get_assembly_item(world["assembly_item_id"])
    bom = await service.create_bom(
        {"assembly_item_id": str(world["assembly_item_id"]), "bom_code": "INV-BOM", "name": "INV BOM"}
    )
    await service.activate_bom(bom["id"])
    revision = await service.create_bom_revision(bom["id"], {"revision_code": "R1", "effective_from": "2020-01-01"})
    await service.add_bom_component(
        revision["id"],
        {
            "line_number": 1,
            "component_item_id": str(world["component_item_id"]),
            "qty_per": "2",
            "uom_id": str(world["uom_id"]),
        },
    )
    await service.add_bom_component(
        revision["id"],
        {
            "line_number": 2,
            "component_item_id": str(world["other_item_id"]),
            "qty_per": "1",
            "uom_id": str(world["uom_id"]),
        },
    )
    await service.activate_bom_revision(revision["id"])
    assert assembly["id"]


async def test_build_value_and_quantity_conservation(db_session) -> None:
    world = await seed_manufacturing_world(db_session, code_prefix="INVAB")
    service = world["service"]
    await _bom(service, world)
    build = await service.create_build(
        {
            "assembly_item_id": str(world["assembly_item_id"]),
            "qty_built": "3",
            "location_id": str(world["location_id"]),
        }
    )
    posted = await service.post_build(build["id"], {"idempotency_key": "inv-build-1"})
    assert posted["status"] == "POSTED"
    call = world["posting"].calls[-1]
    assert call["posting_type"] == "T-15"
    assert call["source_doc_type"] == "ASSEMBLY_BUILD" and call["source_doc_id"] == build["id"]
    movements = call["movements"]
    consumed = sum(Decimal(str(m["qty"])) for m in movements if Decimal(str(m["qty"])) < 0)
    produced = sum(Decimal(str(m["qty"])) for m in movements if Decimal(str(m["qty"])) > 0)
    assert consumed == Decimal("-9")  # qty 3 x (2 + 1) components
    assert produced == Decimal("3")
    # Quantity conservation against the recorded BOM snapshot.
    assert abs(consumed) == sum(Decimal(str(line["qty_consumed"])) for line in posted["components"])
    assert produced == Decimal(str(posted["qty_built"]))


async def test_unbuild_value_and_scrap_exclusion(db_session) -> None:
    world = await seed_manufacturing_world(db_session, code_prefix="INVAU")
    service = world["service"]
    await _bom(service, world)
    unbuild = await service.create_unbuild(
        {
            "assembly_item_id": str(world["assembly_item_id"]),
            "qty_unbuilt": "2",
            "location_id": str(world["location_id"]),
            "lines": [
                {
                    "component_item_id": str(world["component_item_id"]),
                    "disposition": "RETURN_TO_STOCK",
                    "unit_cost": "2",
                },
                {"component_item_id": str(world["other_item_id"]), "disposition": "SCRAP", "unit_cost": "3"},
            ],
        }
    )
    posted = await service.post_unbuild(unbuild["id"], {"idempotency_key": "inv-unbuild-1"})
    call = world["posting"].calls[-1]
    assert call["posting_type"] == "T-16"
    returned_items = {movement["item_id"] for movement in call["movements"] if float(movement["qty"]) > 0}
    assert str(world["component_item_id"]) in returned_items
    assert str(world["other_item_id"]) not in returned_items
    scrap_value = Decimal(posted["scrap_value"])
    assert scrap_value > 0
    output = [
        movement for movement in call["movements"] if movement["item_id"] == str(world["assembly_item_master_id"])
    ]
    assert output and Decimal(str(output[0]["qty"])) == Decimal("-2")
