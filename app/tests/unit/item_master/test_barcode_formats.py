"""UPC/EAN/Code128 validation unit tests (pure helpers from the item service)."""

from __future__ import annotations

import pytest

from app.modules.item_master.errors import BarcodeInvalid
from app.modules.item_master.service import ItemMasterService, _ean_checksum_ok, _upc_checksum_ok

service = ItemMasterService.__new__(ItemMasterService)


def test_valid_upc_and_ean_checksums() -> None:
    assert _upc_checksum_ok("036000291452") is True
    assert _upc_checksum_ok("036000291453") is False
    assert _ean_checksum_ok("4006381333931") is True
    assert _ean_checksum_ok("4006381333932") is False


def test_validate_barcode_types() -> None:
    service._validate_barcode("036000291452", "UPC_A")
    service._validate_barcode("4006381333931", "EAN_13")
    service._validate_barcode("ABC-123", "CODE128")
    with pytest.raises(BarcodeInvalid):
        service._validate_barcode("036000291453", "UPC_A")
    with pytest.raises(BarcodeInvalid):
        service._validate_barcode("not digits", "EAN_13")
