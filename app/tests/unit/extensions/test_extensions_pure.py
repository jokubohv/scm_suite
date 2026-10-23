"""Pure FS-14 helpers: UPC-A check digit, coverage month math, metadata."""

from __future__ import annotations

from datetime import date

from app.modules.extensions import tables as t
from app.modules.extensions.service import add_months, upc_check_digit, valid_upc_code


def test_upc_check_digit_vectors() -> None:
    # UPC-A check digit vectors (mod-10): data parts and expected check digit.
    assert upc_check_digit("03600029145") == 2  # 036000291452
    assert upc_check_digit("12345678901") == 2  # 123456789012
    assert upc_check_digit("00000000000") == 0
    assert valid_upc_code("036000291452") is True
    assert valid_upc_code("036000291453") is False


def test_warranty_month_boundaries() -> None:
    assert add_months(date(2026, 1, 31), 1) == date(2026, 2, 28)
    assert add_months(date(2024, 2, 29), 12) == date(2025, 2, 28)
    assert add_months(date(2026, 1, 15), 24) == date(2028, 1, 15)


def test_extension_tables() -> None:
    assert len(t.ALL_TABLES) == 7
    assert {table.name for table in t.ALL_TABLES} == {
        "item_warranty",
        "warranty_registration",
        "warranty_claim",
        "warranty_claim_action",
        "warranty_repair_order",
        "customer_part",
        "upc_registry",
    }
