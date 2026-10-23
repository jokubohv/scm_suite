"""Pure taxonomy/eligibility unit tests."""

from __future__ import annotations

import pytest

from app.modules.item_master.taxonomy import ITEM_TYPE_CODES, PostingOperation, posting_eligibility


def test_all_24_types_have_eligibility_map() -> None:
    assert len(ITEM_TYPE_CODES) == 24
    for code in ITEM_TYPE_CODES:
        assert isinstance(posting_eligibility(code, PostingOperation.POST).eligible, bool)


def test_charge_only_never_eligible() -> None:
    assert posting_eligibility("PAYMENT", PostingOperation.SO).eligible is False
    assert posting_eligibility("DISCOUNT", PostingOperation.PO).eligible is False


def test_unknown_operation_rejected() -> None:
    with pytest.raises(ValueError):
        posting_eligibility("INVENTORY", "NOT_AN_OP")


def test_build_requires_bom() -> None:
    assert posting_eligibility("ASSEMBLY", PostingOperation.BUILD).eligible is True
    assert posting_eligibility("INVENTORY", PostingOperation.BUILD).eligible is False
