"""Pure document-link validation/serialisation rules (no database)."""

from __future__ import annotations

from uuid import UUID

import pytest

from app.core.repositories.artifacts import (
    ArtifactsValidation,
    normalize_document_link_payload,
    serialize_document_link,
)

FROM_ID = UUID("33333333-3333-7333-8333-333333333333")
TO_ID = UUID("44444444-4444-7444-8444-444444444444")


def _payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "link_type": "CONVERTED_FROM",
        "from_type": "PURCHASE_ORDER",
        "from_id": str(FROM_ID),
        "to_type": "ITEM_RECEIPT",
        "to_id": str(TO_ID),
    }
    payload.update(overrides)
    return payload


def test_valid_link_normalises_uuids_and_optional_amounts() -> None:
    values = normalize_document_link_payload(_payload(qty="2.5", amount_base="100.25"))
    assert values["link_type"] == "CONVERTED_FROM"
    assert values["from_id"] == FROM_ID
    assert values["to_id"] == TO_ID
    assert str(values["qty"]) == "2.5"
    assert str(values["amount_base"]) == "100.25"


def test_link_type_must_be_upper_snake_case() -> None:
    with pytest.raises(ArtifactsValidation):
        normalize_document_link_payload(_payload(link_type="converted_from"))


def test_self_link_is_rejected() -> None:
    with pytest.raises(ArtifactsValidation) as exc:
        normalize_document_link_payload(
            _payload(from_type="PURCHASE_ORDER", to_type="PURCHASE_ORDER", to_id=str(FROM_ID))
        )
    assert exc.value.code == "VALIDATION_ERROR"


def test_negative_or_float_amounts_are_rejected() -> None:
    with pytest.raises(ArtifactsValidation):
        normalize_document_link_payload(_payload(qty="-1"))
    with pytest.raises(ArtifactsValidation):
        normalize_document_link_payload(_payload(amount_base=1.5))


def test_bad_uuids_are_rejected() -> None:
    with pytest.raises(ArtifactsValidation):
        normalize_document_link_payload(_payload(from_id="nope"))
    with pytest.raises(ArtifactsValidation):
        normalize_document_link_payload(_payload(to_id=123))


def test_serialize_document_link_exposes_decimal_strings_only() -> None:
    row = {
        "id": FROM_ID,
        "link_type": "RECEIPT_FOR",
        "from_type": "ITEM_RECEIPT",
        "from_id": FROM_ID,
        "to_type": "PURCHASE_ORDER",
        "to_id": TO_ID,
        "qty": None,
        "amount_base": None,
        "created_at": None,
        "created_by": None,
    }
    payload = serialize_document_link(row)
    assert payload["id"] == str(FROM_ID)
    assert payload["link_type"] == "RECEIPT_FOR"
    assert payload["qty"] is None
    assert payload["amount_base"] is None
