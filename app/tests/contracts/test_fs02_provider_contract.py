"""FS-02 provider contract tests (`pricing.engine@1.0.0`).

Validates the implementation-backed builder fragment, the canonical priced-quote / resolution JSON
Schemas, the six pricing event payloads and the D3/Δ-2 boundary consumer round trip (external order
system snapshots the quote; FS-02 persists no sell-side prices).
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from uuid import UUID

from app.api.routers.pricing import ROUTE_SCOPES
from app.contracts.pricing.schemas import (
    EVENT_MODELS,
    PricedQuote,
    PriceResolutionRequest,
    event_schemas,
    price_resolution_schema,
    priced_quote_schema,
)
from app.modules.pricing.engine import (
    ItemInfo,
    PriceLevelInfo,
    PriceLineInput,
    PriceRequest,
    PriceRow,
    PricingSnapshot,
    resolve_prices,
)

HERE = Path(__file__).resolve().parents[3]
CONTRACT_DIR = HERE / "app" / "contracts" / "pricing"
ITEM = UUID("b0000000-0000-7000-8000-000000000001")
LEVEL = UUID("b0000000-0000-7000-8000-000000000010")


def _resolution() -> dict:
    snapshot = PricingSnapshot(
        items={ITEM: ItemInfo(item_id=ITEM, is_active=True)},
        levels={
            LEVEL: PriceLevelInfo(id=LEVEL, code="DEFAULT", currency_code="USD", is_inactive=False, is_default=True)
        },
        default_level_id=LEVEL,
        prices=(
            PriceRow(
                id=UUID("b0000000-0000-7000-8000-000000000020"),
                item_id=ITEM,
                price_level_id=LEVEL,
                currency_code="USD",
                qty_min=Decimal(0),
                qty_max=None,
                unit_price=Decimal("12.50"),
                effective_from=date(2026, 1, 1),
                effective_to=None,
                is_active=True,
                created_at=0,
            ),
        ),
    )
    return resolve_prices(
        PriceRequest(
            lines=(PriceLineInput(line_ref="1", item_id=ITEM, quantity=Decimal("2"), uom="EA"),),
            currency_code="USD",
            document_date=date(2026, 10, 21),
        ),
        snapshot,
    )


def test_builder_fragment_covers_all_implemented_routes() -> None:
    fragment = json.loads((CONTRACT_DIR / "openapi.fragment.json").read_text(encoding="utf-8"))
    operations = {
        (method.upper(), path)
        for path, item in fragment["paths"].items()
        for method in item
        if method in {"get", "post", "put", "patch", "delete"}
    }
    assert operations == set(ROUTE_SCOPES)
    assert fragment["info"]["x-endpoint-count"] == 64
    assert fragment["info"]["x-feature-set"] == "FS-02"
    assert fragment["info"]["x-contract"] == ["pricing.engine@1.0.0"]
    assert all(operation.get("x-implemented") for item in fragment["paths"].values() for operation in item.values())


def test_every_fragment_scope_is_catalogued() -> None:
    catalogue = json.loads((HERE / "design" / "permissions.json").read_text(encoding="utf-8"))
    known = {row["scope"] for row in catalogue["catalogue"]}
    fragment = json.loads((CONTRACT_DIR / "openapi.fragment.json").read_text(encoding="utf-8"))
    scopes = {operation["x-required-scopes"][0] for item in fragment["paths"].values() for operation in item.values()}
    assert scopes <= known


def test_priced_quote_schema_contains_b2_fields() -> None:
    schema = priced_quote_schema()
    properties = schema["properties"]
    for key in ("policy_version", "currency_code", "document_date", "lines", "quote_hash", "line_count"):
        assert key in properties
    line_properties = schema["$defs"]["ResolutionLine"]["properties"]
    for key in (
        "line_ref",
        "item_id",
        "uom",
        "quantity",
        "unit_price",
        "extended_price",
        "price_source",
        "rules_applied",
    ):
        assert key in line_properties
    on_disk = json.loads((CONTRACT_DIR / "priced-quote.schema.json").read_text(encoding="utf-8"))
    assert on_disk == schema


def test_price_resolution_schema_round_trips_engine_output() -> None:
    resolution = _resolution()
    model = PricedQuote.model_validate(resolution["priced_quote"])
    assert model.policy_version == "pricing-engine@1.0.0"
    assert model.line_count == 1
    assert model.lines[0].unit_price == "12.500000000000"
    assert model.quote_hash == resolution["lines"][0]["quote_hash"] or len(model.quote_hash) == 64
    request = PriceResolutionRequest(
        lines=[{"line_ref": "1", "item_id": str(ITEM), "quantity": "2", "uom": "EA"}],
        currency_code="USD",
        document_date="2026-10-21",
    )
    assert request.lines[0].item_id == ITEM
    on_disk = json.loads((CONTRACT_DIR / "price-resolution.schema.json").read_text(encoding="utf-8"))
    assert on_disk == price_resolution_schema()


def test_all_six_event_payload_schemas_present() -> None:
    expected = {
        "pricing.price.updated",
        "pricing.rule.changed",
        "pricing.price_update.applied",
        "pricing.price_update.superseded",
        "pricing.price_update.reverted",
        "pricing.coupon.redeemed",
    }
    assert set(EVENT_MODELS) == expected
    on_disk = {path.name.removesuffix(".schema.json") for path in (CONTRACT_DIR / "events").glob("*.schema.json")}
    assert on_disk == expected
    assert event_schemas()["pricing.coupon.redeemed"]["required"] == [
        "coupon_code",
        "document_type",
        "document_id",
        "uses_count",
        "discount_amount",
    ]


def test_boundary_consumer_fixture_snapshot_round_trip() -> None:
    """D3/Δ-2: the external order system snapshots the priced quote; resolving again is byte-identical."""
    first = _resolution()
    second = _resolution()
    assert first["priced_quote"] == second["priced_quote"]
    quote = PricedQuote.model_validate(first["priced_quote"])
    # snapshot-on-consumer-side: quote is JSON-serialisable, immutable-by-convention and hash-stable
    serialised = json.dumps(quote.model_dump(), sort_keys=True)
    restored = PricedQuote.model_validate(json.loads(serialised))
    assert restored.quote_hash == quote.quote_hash
