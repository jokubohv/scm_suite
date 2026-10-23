"""Pure attachment-payload validation rules (no database)."""

from __future__ import annotations

from uuid import UUID

import pytest

from app.core.repositories.artifacts import (
    DEFAULT_MAX_ATTACHMENT_BYTES,
    ArtifactsValidation,
    normalize_attachment_payload,
    normalize_checksum,
    normalize_storage_key,
    validate_storage_key,
)

TENANT_ID = UUID("11111111-1111-7111-8111-111111111111")
ENTITY_ID = UUID("22222222-2222-7222-8222-222222222222")


def _payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "entity_type": "INVOICE",
        "entity_id": str(ENTITY_ID),
        "filename": "invoice-2026-10.pdf",
        "content_type": "application/pdf",
        "size_bytes": 1024,
    }
    payload.update(overrides)
    return payload


def test_valid_payload_normalises_and_generates_tenant_prefixed_storage_key() -> None:
    values = normalize_attachment_payload(_payload(), tenant_id=TENANT_ID)
    assert values["entity_type"] == "INVOICE"
    assert values["entity_id"] == ENTITY_ID
    assert values["filename"] == "invoice-2026-10.pdf"
    assert values["content_type"] == "application/pdf"
    assert values["size_bytes"] == 1024
    assert values["checksum"] is None
    assert values["is_quality_image"] is False
    assert values["storage_key"].startswith(f"tenant/{TENANT_ID}/invoice/{ENTITY_ID}/")


def test_explicit_tenant_prefixed_storage_key_is_kept() -> None:
    key = f"tenant/{TENANT_ID}/invoice/{ENTITY_ID}/abc/file.pdf"
    values = normalize_attachment_payload(_payload(storage_key=key), tenant_id=TENANT_ID)
    assert values["storage_key"] == key


def test_storage_ref_alias_is_accepted() -> None:
    key = f"tenant/{TENANT_ID}/invoice/{ENTITY_ID}/abc/file.pdf"
    values = normalize_attachment_payload(_payload(storage_ref=key), tenant_id=TENANT_ID)
    assert values["storage_key"] == key


def test_storage_key_must_be_tenant_prefixed() -> None:
    with pytest.raises(ArtifactsValidation) as exc:
        normalize_attachment_payload(_payload(storage_key="other/file.pdf"), tenant_id=TENANT_ID)
    assert exc.value.code == "VALIDATION_ERROR"
    assert "tenant-prefixed" in exc.value.message


def test_storage_key_rejects_traversal() -> None:
    with pytest.raises(ArtifactsValidation):
        validate_storage_key(tenant_id=TENANT_ID, storage_key=f"tenant/{TENANT_ID}/../escape")
    with pytest.raises(ArtifactsValidation):
        validate_storage_key(tenant_id=TENANT_ID, storage_key=f"tenant/{TENANT_ID}/a\\b")


def test_filename_rejects_path_separators() -> None:
    for filename in ("../etc/passwd", "dir/file.pdf", "dir\\file.pdf"):
        with pytest.raises(ArtifactsValidation):
            normalize_attachment_payload(_payload(filename=filename), tenant_id=TENANT_ID)


def test_entity_type_and_id_validation() -> None:
    with pytest.raises(ArtifactsValidation):
        normalize_attachment_payload(_payload(entity_type=""), tenant_id=TENANT_ID)
    with pytest.raises(ArtifactsValidation):
        normalize_attachment_payload(_payload(entity_type="bad type"), tenant_id=TENANT_ID)
    with pytest.raises(ArtifactsValidation):
        normalize_attachment_payload(_payload(entity_id="not-a-uuid"), tenant_id=TENANT_ID)


def test_content_type_allowlist_case_insensitive() -> None:
    values = normalize_attachment_payload(_payload(content_type="Application/PDF"), tenant_id=TENANT_ID)
    assert values["content_type"] == "application/pdf"
    with pytest.raises(ArtifactsValidation):
        normalize_attachment_payload(_payload(content_type="application/zip"), tenant_id=TENANT_ID)


def test_size_bounds() -> None:
    with pytest.raises(ArtifactsValidation):
        normalize_attachment_payload(_payload(size_bytes=-1), tenant_id=TENANT_ID)
    with pytest.raises(ArtifactsValidation):
        normalize_attachment_payload(_payload(size_bytes=DEFAULT_MAX_ATTACHMENT_BYTES + 1), tenant_id=TENANT_ID)
    with pytest.raises(ArtifactsValidation):
        normalize_attachment_payload(_payload(size_bytes="1024"), tenant_id=TENANT_ID)


def test_checksum_normalisation() -> None:
    bare_sha256 = "a" * 64
    assert normalize_checksum(bare_sha256) == f"sha256:{bare_sha256}"
    assert normalize_checksum("SHA256:" + "A" * 64) == "sha256:" + "a" * 64
    assert normalize_checksum("md5:" + "b" * 32) == "md5:" + "b" * 32
    assert normalize_checksum(None) is None
    for bad in ("", "abc", "sha512:" + "c" * 128, "not-a-digest"):
        with pytest.raises(ArtifactsValidation):
            normalize_checksum(bad)


def test_quality_image_flag_must_be_boolean() -> None:
    with pytest.raises(ArtifactsValidation):
        normalize_attachment_payload(_payload(is_quality_image="yes"), tenant_id=TENANT_ID)
    values = normalize_attachment_payload(_payload(is_quality_image=True), tenant_id=TENANT_ID)
    assert values["is_quality_image"] is True


def test_generated_storage_keys_are_unique_per_call() -> None:
    first = normalize_storage_key(
        tenant_id=TENANT_ID, entity_type="ITEM", entity_id=ENTITY_ID, filename="photo.png"
    )
    second = normalize_storage_key(
        tenant_id=TENANT_ID, entity_type="ITEM", entity_id=ENTITY_ID, filename="photo.png"
    )
    assert first != second
    assert " " not in first
