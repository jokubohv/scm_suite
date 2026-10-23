"""Invariant DM-EXT-04 / EXT-RULE-05/06 (FS-14 exit gate): UPC uniqueness incl. retired + item mirror."""

from __future__ import annotations

import pytest
from sqlalchemy import text

from app.modules.extensions.errors import UpcAlreadyAssigned, UpcRetiredNeverReusable
from app.modules.extensions.service import upc_check_digit
from app.modules.extensions.testing import seed_extensions_world

pytestmark = pytest.mark.integration


def _upc(data: str) -> str:
    return data + str(upc_check_digit(data))


async def test_retired_code_is_never_reused_and_mirror_tracks_registry(db_session) -> None:
    world = await seed_extensions_world(db_session, code_prefix="INVUP")
    service = world["service"]
    code = _upc("12345600009")
    assigned = await service.assign_upc({"item_id": str(world["item_id"]), "upc_code": code})
    mirrored = await db_session.execute(
        text("SELECT upc_code FROM item_master.item WHERE tenant_id=:t AND id=:i"),
        {"t": world["tenant_id"], "i": world["item_id"]},
    )
    assert mirrored.scalar_one() == code

    # No second ASSIGNED row for the same item (partial unique index).
    with pytest.raises(UpcAlreadyAssigned):
        await service.assign_upc({"item_id": str(world["item_id"]), "upc_code": _upc("12345600010")})
    retired = await service.retire_upc(assigned["id"], {"reason": "discontinued"})
    assert retired["status"] == "RETIRED"
    cleared = await db_session.execute(
        text("SELECT upc_code FROM item_master.item WHERE tenant_id=:t AND id=:i"),
        {"t": world["tenant_id"], "i": world["item_id"]},
    )
    assert cleared.scalar_one() is None

    with pytest.raises(UpcRetiredNeverReusable):
        await service.assign_upc({"item_id": str(world["serialized_item_id"]), "upc_code": code})
    validation = await service.validate_upc(code)
    assert validation["registered"] is True and validation["valid"] is False


async def test_upc_database_uniqueness_and_checksum(db_session) -> None:
    world = await seed_extensions_world(db_session, code_prefix="INVUQ")
    service = world["service"]
    code = _upc("98765400001")
    await service.assign_upc({"item_id": str(world["item_id"]), "upc_code": code})
    rows = await db_session.execute(
        text("SELECT count(*) FROM extensions.upc_registry WHERE tenant_id=:t AND upc_code=:c"),
        {"t": world["tenant_id"], "c": code},
    )
    assert int(rows.scalar_one()) == 1
    invalid = await service.validate_upc(code[:-1] + str((int(code[-1]) + 1) % 10))
    assert invalid["valid"] is False and "CHECKSUM_INVALID" in invalid["reasons"]
