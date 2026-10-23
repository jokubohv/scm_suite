"""D3/Δ-2 external-order-boundary consumer fixture for the priced quote (no database)."""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from uuid import UUID

from app.contracts.pricing.schemas import PricedQuote
from app.modules.pricing.engine import (
    ItemInfo,
    PriceLevelInfo,
    PriceLineInput,
    PriceRequest,
    PriceRow,
    PricingSnapshot,
    resolve_prices,
)

ITEM = UUID("e0000000-0000-7000-8000-000000000001")
LEVEL = UUID("e0000000-0000-7000-8000-000000000010")


def _resolution() -> dict:
    snapshot = PricingSnapshot(
        items={ITEM: ItemInfo(item_id=ITEM, is_active=True)},
        levels={
            LEVEL: PriceLevelInfo(id=LEVEL, code="DEFAULT", currency_code="USD", is_inactive=False, is_default=True)
        },
        default_level_id=LEVEL,
        prices=(
            PriceRow(
                id=UUID("e0000000-0000-7000-8000-000000000020"),
                item_id=ITEM,
                price_level_id=LEVEL,
                currency_code="USD",
                qty_min=Decimal(0),
                qty_max=None,
                unit_price=Decimal("4.25"),
                effective_from=date(2026, 1, 1),
                effective_to=None,
                is_active=True,
                created_at=0,
            ),
        ),
    )
    return resolve_prices(
        PriceRequest(
            lines=(PriceLineInput(line_ref="SO-L1", item_id=ITEM, quantity=Decimal("3"), uom="EA"),),
            currency_code="USD",
            document_date=date(2026, 10, 21),
            external_customer_ref="CUST-1",
        ),
        snapshot,
    )


def test_consumer_snapshots_priced_quote_on_its_own_document() -> None:
    """The external order system stores the JSON quote verbatim (FS-02 persists no sell-side prices)."""
    resolution = _resolution()
    quote_json = json.dumps(resolution["priced_quote"], sort_keys=True)
    restored = PricedQuote.model_validate(json.loads(quote_json))
    assert restored.quote_hash == resolution["priced_quote"]["quote_hash"]
    assert restored.lines[0].price_source == "LEVEL"
    assert restored.lines[0].unit_price == "4.250000000000"
    assert restored.external_customer_ref == "CUST-1"
    assert restored.policy_version == "pricing-engine@1.0.0"


def test_resolution_is_reproducible_for_snapshot_replay() -> None:
    first = _resolution()["priced_quote"]
    second = _resolution()["priced_quote"]
    assert first == second
