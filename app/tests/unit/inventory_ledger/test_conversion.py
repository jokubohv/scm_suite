"""FS-04 unit: quantity normalisation/conversion inputs."""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest

from app.modules.inventory_ledger.errors import ValidationFailed
from app.modules.inventory_ledger.service import quantize

ITEM = str(uuid.uuid4())
LOC = str(uuid.uuid4())
STATUS = str(uuid.uuid4())


def test_quantize_12dp_half_up_and_reject_non_finite() -> None:
    assert quantize("1.0000000000005") == Decimal("1.000000000001")
    assert quantize(Decimal("-3.25")) == Decimal("-3.250000000000")
    with pytest.raises(ValidationFailed):
        quantize("NaN")
    with pytest.raises(ValidationFailed):
        quantize("not-a-number")
