"""FS-01 item taxonomy invariants (DM-ITEM-01..13): 24 types, flags and posting/document eligibility."""

from __future__ import annotations

import pytest

from app.modules.item_master.taxonomy import (
    ITEM_TYPE_BY_CODE,
    ITEM_TYPE_SEEDS,
    PostingOperation,
    item_type_seed,
    posting_eligibility,
)

EXPECTED_CODES = {
    "INVENTORY", "SERIALIZED", "LOT_NUMBERED", "ASSEMBLY", "WIP", "PHANTOM", "OUTSOURCED",
    "MATRIX_PARENT", "MATRIX_CHILD", "KIT", "GROUP", "NON_INVENTORY", "DROP_SHIP", "SPECIAL_ORDER",
    "SERVICE", "DOWNLOAD", "GIFT_CERTIFICATE", "EXPENSE", "OTHER_CHARGE", "DESCRIPTION", "DISCOUNT",
    "MARKUP", "SUBTOTAL", "PAYMENT",
}


def test_24_documented_types_and_flags() -> None:
    assert len(ITEM_TYPE_SEEDS) == 24
    assert set(ITEM_TYPE_BY_CODE) == EXPECTED_CODES
    assert {seed.code for seed in ITEM_TYPE_SEEDS if seed.can_stock} == {
        "INVENTORY", "SERIALIZED", "LOT_NUMBERED", "ASSEMBLY", "WIP", "OUTSOURCED", "MATRIX_CHILD",
    }
    assert {seed.code for seed in ITEM_TYPE_SEEDS if seed.requires_bom} == {
        "ASSEMBLY", "WIP", "PHANTOM", "OUTSOURCED",
    }
    assert {seed.code for seed in ITEM_TYPE_SEEDS if seed.is_charge_only} == {
        "EXPENSE", "OTHER_CHARGE", "DESCRIPTION", "DISCOUNT", "MARKUP", "SUBTOTAL", "PAYMENT",
    }
    assert {seed.code for seed in ITEM_TYPE_SEEDS if seed.requires_numbering} == {"SERIALIZED", "LOT_NUMBERED"}


def test_dm_item_03_stockable_types_are_posting_eligible() -> None:
    for code in ("INVENTORY", "SERIALIZED", "LOT_NUMBERED", "MATRIX_CHILD"):
        assert posting_eligibility(code, PostingOperation.POST).eligible is True, code
    for code in ("KIT", "GROUP", "MATRIX_PARENT", "SERVICE", "NON_INVENTORY"):
        eligibility = posting_eligibility(code, PostingOperation.POST)
        assert eligibility.eligible is False and eligibility.reasons, code


def test_charge_only_types_rejected_for_all_document_operations() -> None:
    for code in ("EXPENSE", "OTHER_CHARGE", "DISCOUNT", "PAYMENT"):
        assert posting_eligibility(code, PostingOperation.PO).eligible is False
        assert posting_eligibility(code, PostingOperation.SO).eligible is False


def test_inactive_item_is_never_eligible() -> None:
    eligibility = posting_eligibility("INVENTORY", PostingOperation.POST, is_active=False)
    assert eligibility.eligible is False and "INACTIVE_ITEM" in eligibility.reasons


def test_unknown_type_rejected() -> None:
    with pytest.raises(KeyError):
        item_type_seed("NOT_A_TYPE")
