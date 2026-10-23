"""Serialisation, cursor, filter and storage-port rules for the artifacts surface (no database)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID

import pytest

from app.api.routers.platform.artifacts import ROUTE_SCOPES, router
from app.core.repositories.artifacts import (
    DEFAULT_PAGE_LIMIT,
    MAX_PAGE_LIMIT,
    InvalidCursor,
    NullUploadSigner,
    PresignedUpload,
    UploadSignerPort,
    clamp_limit,
    decode_cursor,
    encode_cursor,
    serialize_attachment,
    serialize_journal_intent,
    validate_journal_filters,
)
from app.core.security.scopes import ScopeRegistry

NOW = datetime(2026, 10, 23, 12, 0, 0, tzinfo=UTC)
INTENT_ID = UUID("55555555-5555-7555-8555-555555555555")
TENANT_ID = UUID("11111111-1111-7111-8111-111111111111")


class _FakeSigner:
    provider = "fake-s3"

    def sign_upload(self, *, tenant_id: UUID, storage_key: str, content_type: str | None,
                    size_bytes: int, expires_in_seconds: int = 900) -> PresignedUpload | None:
        return PresignedUpload(
            provider=self.provider,
            storage_key=storage_key,
            url=f"https://fake-s3.example/{storage_key}?sig=abc",
            headers={"Content-Type": content_type or "application/octet-stream"},
            expires_in_seconds=expires_in_seconds,
        )

    def sign_download(self, *, tenant_id: UUID, storage_key: str,
                      expires_in_seconds: int = 900) -> str | None:
        return f"https://fake-s3.example/{storage_key}?download=1"


def test_endpoints_declare_exactly_one_catalogued_scope() -> None:
    catalogue = ScopeRegistry().catalogue
    assert set(ROUTE_SCOPES.values()) == {
        "platform.attachment.manage",
        "platform.documentlink.read",
        "platform.finance.read",
    }
    for scope in ROUTE_SCOPES.values():
        assert catalogue.canonical(scope) == scope


def test_every_declared_route_exists_on_the_router() -> None:
    declared = {(method, path) for (method, path) in ROUTE_SCOPES}
    actual: set[tuple[str, str]] = set()
    for route in router.routes:
        for method in getattr(route, "methods", set()) or set():
            actual.add((method, route.path))
    assert declared <= actual


def test_contract_canonical_paths_are_present_even_when_aliases_are_hidden() -> None:
    paths = {route.path for route in router.routes}
    assert {"/api/v1/attachments", "/api/v1/attachments/{attachment_id}"} <= paths
    assert {"/api/v1/document-links", "/api/v1/finance/journal-intents"} <= paths
    # Task-44 operational aliases exist but are not part of the OpenAPI schema.
    assert "/api/v1/admin/attachments" in paths
    assert "/api/v1/admin/document-links" in paths


def test_null_signer_is_metadata_only() -> None:
    signer = NullUploadSigner()
    assert signer.provider == "null"
    assert signer.sign_upload(
        tenant_id=TENANT_ID, storage_key="tenant/x/file.pdf", content_type="application/pdf", size_bytes=1
    ) is None
    assert signer.sign_download(tenant_id=TENANT_ID, storage_key="tenant/x/file.pdf") is None


def test_upload_signer_protocol_is_runtime_checkable() -> None:
    assert isinstance(NullUploadSigner(), UploadSignerPort)
    assert isinstance(_FakeSigner(), UploadSignerPort)


def test_serialize_attachment_is_json_safe_and_keeps_storage_ref_alias() -> None:
    row = {
        "id": UUID(int=1),
        "entity_type": "LOT",
        "entity_id": UUID(int=2),
        "filename": "coa.pdf",
        "content_type": "application/pdf",
        "size_bytes": 10,
        "storage_key": "tenant/t/lot/1/coa.pdf",
        "checksum": "sha256:" + "a" * 64,
        "is_quality_image": True,
        "uploaded_by": UUID(int=3),
        "created_at": NOW,
        "updated_at": NOW,
        "version": 2,
    }
    payload = serialize_attachment(row)
    assert payload["id"] == str(UUID(int=1))
    assert payload["entity_id"] == str(UUID(int=2))
    assert payload["storage_key"] == payload["storage_ref"] == "tenant/t/lot/1/coa.pdf"
    assert payload["uploaded_by"] == str(UUID(int=3))
    assert payload["created_at"] == "2026-10-23T12:00:00+00:00"
    assert payload["is_quality_image"] is True
    assert "upload" not in payload


def test_serialize_attachment_can_embed_download_url() -> None:
    row = {
        "id": UUID(int=1),
        "entity_type": "LOT",
        "entity_id": UUID(int=2),
        "filename": "coa.pdf",
        "content_type": None,
        "size_bytes": 0,
        "storage_key": "tenant/t/x",
        "checksum": None,
        "is_quality_image": False,
        "uploaded_by": None,
        "created_at": None,
        "updated_at": None,
        "version": 1,
    }
    payload = serialize_attachment(row, download_url="https://example/dl")
    assert payload["download_url"] == "https://example/dl"


def test_serialize_journal_intent_uses_decimal_strings_and_iso_timestamps() -> None:
    row = {
        "id": INTENT_ID,
        "intent_line_no": 1,
        "inventory_transaction_id": UUID(int=9),
        "ledger_sequence": 42,
        "idempotency_key": "key-1",
        "source_doc_type": "ITEM_RECEIPT",
        "source_doc_id": UUID(int=8),
        "posting_rule_id": "RCPT",
        "account_ref": "INV_ASSET",
        "debit_amount_base": Decimal("100.000000000000"),
        "credit_amount_base": Decimal("0"),
        "amount_txn": None,
        "currency_code": "USD",
        "exchange_rate": Decimal("1"),
        "qty": Decimal("2.5"),
        "uom_id": None,
        "segments": {"class": "A"},
        "status": "RECORDED",
        "recorded_at": NOW,
        "acknowledged_at": None,
        "intent_payload": {"note": "x"},
    }
    payload = serialize_journal_intent(row, include_payload=True)
    assert payload["id"] == str(INTENT_ID)
    assert payload["debit_amount_base"] == "100.000000000000"
    assert payload["qty"] == "2.5"
    assert payload["exchange_rate"] == "1"
    assert payload["segments"] == {"class": "A"}
    assert payload["recorded_at"] == "2026-10-23T12:00:00+00:00"
    assert payload["intent_payload"] == {"note": "x"}
    assert "intent_payload" not in serialize_journal_intent(row)


def test_cursor_roundtrip_is_stable_and_opaque() -> None:
    cursor = encode_cursor(NOW, INTENT_ID)
    assert "2026-10-23" not in cursor  # opaque to clients
    restored = decode_cursor(cursor)
    assert restored == (NOW, INTENT_ID)


@pytest.mark.parametrize("bad", ["", "not-base64!", "e30=", "eyJpZCI6ICJub3QtdXVpZCJ9"])
def test_invalid_cursors_are_rejected(bad: str) -> None:
    with pytest.raises(InvalidCursor):
        decode_cursor(bad)


def test_limit_clamping() -> None:
    assert clamp_limit(1) == 1
    assert clamp_limit(MAX_PAGE_LIMIT) == MAX_PAGE_LIMIT
    for bad in (0, -1, MAX_PAGE_LIMIT + 1):
        with pytest.raises(Exception):
            clamp_limit(bad)


def test_journal_filters_normalise_status_and_dates() -> None:
    filters = validate_journal_filters(
        transaction_id=UUID(int=4),
        source_doc_type="ITEM_RECEIPT",
        source_doc_id=None,
        posting_rule_id="RCPT",
        status="recorded",
        date_from=date(2026, 10, 1),
        date_to=date(2026, 10, 2),
    )
    assert filters["status"] == "RECORDED"
    assert filters["from_ts"] == datetime(2026, 10, 1, tzinfo=UTC)
    assert filters["to_ts"] == datetime(2026, 10, 3, tzinfo=UTC)


def test_journal_filters_reject_bad_status_and_reversed_dates() -> None:
    with pytest.raises(Exception):
        validate_journal_filters(
            transaction_id=None, source_doc_type=None, source_doc_id=None, posting_rule_id=None,
            status="POSTED", date_from=None, date_to=None,
        )
    with pytest.raises(Exception):
        validate_journal_filters(
            transaction_id=None, source_doc_type=None, source_doc_id=None, posting_rule_id=None,
            status=None, date_from=date(2026, 10, 2), date_to=date(2026, 10, 1),
        )


def test_default_page_limit_is_within_bounds() -> None:
    assert 1 <= DEFAULT_PAGE_LIMIT <= MAX_PAGE_LIMIT
