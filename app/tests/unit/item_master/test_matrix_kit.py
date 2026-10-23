"""Matrix/kit pure bounds unit tests."""

from __future__ import annotations

from app.modules.item_master.service import MAX_MATRIX_COMBINATIONS
from app.modules.item_master.taxonomy import ITEM_TYPE_SEEDS, is_stockable


def test_matrix_combination_cap_is_2000() -> None:
    assert MAX_MATRIX_COMBINATIONS == 2000


def test_kit_members_must_be_stockable() -> None:
    stockable = {seed.code for seed in ITEM_TYPE_SEEDS if seed.can_stock}
    assert "INVENTORY" in stockable and "SERVICE" not in stockable
    assert is_stockable("INVENTORY") and not is_stockable("KIT")


def test_matrix_parent_is_not_stockable_and_children_are() -> None:
    assert not is_stockable("MATRIX_PARENT")
    assert is_stockable("MATRIX_CHILD")
