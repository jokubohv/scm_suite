"""FS-00 artifact API (task-44): attachments, document links and finance journal-intent reads.

Surface (FS-00 §6 rows + task-44):

* **attachment metadata** — register/list/detail/delete ``platform.attachment_metadata`` metadata only.
  Registration returns the metadata plus a **presigned-upload interface placeholder** from the injected
  :class:`~app.core.repositories.artifacts.UploadSignerPort` (the default ``NullUploadSigner`` performs
  no S3 call and returns ``upload: null``; store the bytes out-of-band and keep only the metadata here).
  Scope ``platform.attachment.manage``. ``POST`` is idempotent (``Idempotency-Key`` required).
* **document links** — create/list/detail/delete ``platform.document_link`` provenance edges. Creation is
  idempotent by natural key; deletion is **guarded**: the table is append-only (``DM-PLAT-02``), so the
  DELETE endpoint audits the refused attempt and returns ``409 DOCUMENT_LINK_APPEND_ONLY`` without
  touching the row. Scope ``platform.documentlink.read`` (the catalogue has no ``documentlink.manage``;
  all link operations are platform-admin read/manage verbs under the one catalogued scope).
* **finance journal intents** — read-only ``GET /api/v1/finance/journal-intents`` (+ detail). **D2
  boundary:** intents are written only by the posting engine inside a posting transaction; this router
  never inserts, updates or deletes an intent (no GL mutation; status transitions belong to the
  finance-adapter role). Scope ``platform.finance.read``.

Paths: the FS-00 §6 contract skeleton defines ``/api/v1/attachments`` and ``/api/v1/document-links``;
the task brief additionally names ``/api/v1/admin/...`` paths. Both are exposed: the §6 paths are the
canonical OpenAPI routes and the ``/admin`` variants are ``include_in_schema=False`` operational aliases
(identical handlers/scopes). Finance paths are identical in both sources.

Router registration is integrator-owned (``app/api/main.py``/``app/api/registry.py``): this module only
exposes ``router`` (+ ``ROUTE_SCOPES`` for the route/scope coverage contract) and never edits those files.
Errors render through the existing problem+json handlers (``install_error_handlers``).
"""

from __future__ import annotations

from datetime import date
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response
from pydantic import AliasChoices, BaseModel, Field

from app.api.deps import DbDep, PrincipalDep, require_scope
from app.api.schemas.platform import PlatformObject
from app.core.repositories.artifacts import (
    DEFAULT_PAGE_LIMIT,
    ArtifactsError,
    ArtifactsRepository,
    NullUploadSigner,
    UploadSignerPort,
    clamp_limit,
    serialize_attachment,
    serialize_document_link,
    serialize_journal_intent,
)

router = APIRouter(prefix="/api/v1", tags=["platform-artifacts"])

# Capability scopes — all catalogued in design/permissions.json (never invent a scope string).
ATTACHMENT_MANAGE = "platform.attachment.manage"
DOCUMENTLINK_READ = "platform.documentlink.read"
FINANCE_READ = "platform.finance.read"

#: (method, full path) -> exactly one catalogued scope; asserted by the router contract test.
ROUTE_SCOPES: dict[tuple[str, str], str] = {
    ("POST", "/api/v1/attachments"): ATTACHMENT_MANAGE,
    ("GET", "/api/v1/attachments"): ATTACHMENT_MANAGE,
    ("GET", "/api/v1/attachments/{attachment_id}"): ATTACHMENT_MANAGE,
    ("DELETE", "/api/v1/attachments/{attachment_id}"): ATTACHMENT_MANAGE,
    ("POST", "/api/v1/admin/attachments"): ATTACHMENT_MANAGE,
    ("GET", "/api/v1/admin/attachments"): ATTACHMENT_MANAGE,
    ("DELETE", "/api/v1/admin/attachments/{attachment_id}"): ATTACHMENT_MANAGE,
    ("POST", "/api/v1/document-links"): DOCUMENTLINK_READ,
    ("GET", "/api/v1/document-links"): DOCUMENTLINK_READ,
    ("GET", "/api/v1/document-links/{link_id}"): DOCUMENTLINK_READ,
    ("DELETE", "/api/v1/document-links/{link_id}"): DOCUMENTLINK_READ,
    ("POST", "/api/v1/admin/document-links"): DOCUMENTLINK_READ,
    ("GET", "/api/v1/admin/document-links"): DOCUMENTLINK_READ,
    ("DELETE", "/api/v1/admin/document-links/{link_id}"): DOCUMENTLINK_READ,
    ("GET", "/api/v1/finance/journal-intents"): FINANCE_READ,
    ("GET", "/api/v1/finance/journal-intents/{intent_id}"): FINANCE_READ,
    ("GET", "/api/v1/finance/journal-intents/{id}"): FINANCE_READ,
}

#: Storage port provider (dependency-injectable in tests / production adapters).
NULL_SIGNER: UploadSignerPort = NullUploadSigner()


def get_upload_signer() -> UploadSignerPort:
    """Return the configured object-storage upload signer (Phase 1: metadata-only null adapter)."""
    return NULL_SIGNER


def _repo(db: DbDep, signer: Annotated[UploadSignerPort, Depends(get_upload_signer)]) -> ArtifactsRepository:
    return ArtifactsRepository(db, signer=signer)


RepoDep = Annotated[ArtifactsRepository, Depends(_repo)]

# Route-level scope markers (the actor principal itself is injected separately for tenant/actor ids).
AttachmentManager = Annotated[object, Depends(require_scope(ATTACHMENT_MANAGE))]
DocumentLinkReader = Annotated[object, Depends(require_scope(DOCUMENTLINK_READ))]
FinanceReader = Annotated[object, Depends(require_scope(FINANCE_READ))]


# --------------------------------------------------------------------------------- errors -----
def _http_error(exc: ArtifactsError) -> HTTPException:
    return HTTPException(
        status_code=exc.status_code,
        detail={"code": exc.code, "message": exc.message, "details": exc.details},
    )


# ------------------------------------------------------------------------------- schemas ------
class AttachmentCreate(BaseModel):
    """Attachment-registration payload (metadata only; bytes never cross this API)."""

    entity_type: str = Field(min_length=1, max_length=64, description="Owning record type, e.g. INVOICE")
    entity_id: UUID = Field(description="Owning record id")
    filename: str = Field(
        min_length=1,
        max_length=255,
        validation_alias=AliasChoices("filename", "file_name"),
        description="Original file name (no path separators)",
    )
    content_type: str | None = Field(default=None, max_length=255)
    size_bytes: int = Field(default=0, ge=0)
    storage_key: str | None = Field(
        default=None,
        max_length=512,
        validation_alias=AliasChoices("storage_key", "storage_ref"),
        description="Tenant-prefixed object key; generated when omitted",
    )
    checksum: str | None = Field(default=None, max_length=200)
    is_quality_image: bool = Field(default=False)


class DocumentLinkCreate(BaseModel):
    """Typed provenance edge between two platform documents (append-only)."""

    link_type: str = Field(min_length=2, max_length=64)
    from_type: str = Field(min_length=1, max_length=64)
    from_id: UUID
    to_type: str = Field(min_length=1, max_length=64)
    to_id: UUID
    qty: str | None = Field(default=None, description="Decimal string (NUMERIC(38,12)); never a float")
    amount_base: str | None = Field(default=None, description="Base-currency decimal string")


# --------------------------------------------------------------------------- attachments -------
@router.post(
    "/attachments", response_model=PlatformObject,
    status_code=201,
    dependencies=[Depends(require_scope(ATTACHMENT_MANAGE))],
)
@router.post(
    "/admin/attachments", response_model=PlatformObject,
    status_code=201,
    include_in_schema=False,
    dependencies=[Depends(require_scope(ATTACHMENT_MANAGE))],
)
async def create_attachment(
    payload: AttachmentCreate,
    repo: RepoDep,
    principal: PrincipalDep,
    _: AttachmentManager,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
) -> Any:
    """Register attachment metadata and return the presigned-upload placeholder + metadata."""
    body = payload.model_dump(mode="json")
    try:
        result = await repo.create_attachment(
            tenant_id=principal.tenant_id,
            actor_id=principal.app_user_id,
            payload=body,
            idempotency_key=idempotency_key,
        )
    except ArtifactsError as exc:
        raise _http_error(exc) from exc
    if result.replay_body is not None:
        return _replay_response(result.replay_body, result.response_status)
    return _attachment_create_payload(result.attachment or {}, result.upload)


def _attachment_create_payload(attachment: dict[str, Any], upload: Any) -> dict[str, Any]:
    return {
        "attachment": attachment,
        "upload": (
            {
                "provider": upload.provider,
                "method": upload.method,
                "url": upload.url,
                "headers": dict(upload.headers),
                "expires_in_seconds": upload.expires_in_seconds,
                "storage_key": upload.storage_key,
            }
            if upload is not None
            else None
        ),
        "storage": {
            "provider": upload.provider if upload is not None else NULL_SIGNER.provider,
            "presigned_upload_supported": upload is not None,
        },
    }


def _replay_response(body: dict[str, Any], status_code: int) -> Any:
    from fastapi.responses import JSONResponse

    return JSONResponse(status_code=status_code, content=body, headers={"Idempotency-Replayed": "true"})


@router.get("/attachments", response_model=PlatformObject, dependencies=[Depends(require_scope(ATTACHMENT_MANAGE))])
@router.get("/admin/attachments", response_model=PlatformObject, include_in_schema=False, dependencies=[Depends(require_scope(ATTACHMENT_MANAGE))])
async def list_attachments(
    repo: RepoDep,
    principal: PrincipalDep,
    _: AttachmentManager,
    entity_type: Annotated[str | None, Query(max_length=64)] = None,
    entity_id: UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = DEFAULT_PAGE_LIMIT,
) -> dict[str, Any]:
    try:
        rows = await repo.list_attachments(
            principal.tenant_id, entity_type=entity_type, entity_id=entity_id, limit=clamp_limit(limit)
        )
    except ArtifactsError as exc:
        raise _http_error(exc) from exc
    return {"items": [serialize_attachment(row) for row in rows], "count": len(rows), "limit": limit}


@router.get("/attachments/{attachment_id}", response_model=PlatformObject, dependencies=[Depends(require_scope(ATTACHMENT_MANAGE))])
@router.get(
    "/admin/attachments/{attachment_id}", response_model=PlatformObject,
    include_in_schema=False,
    dependencies=[Depends(require_scope(ATTACHMENT_MANAGE))],
)
async def get_attachment(
    attachment_id: UUID,
    repo: RepoDep,
    principal: PrincipalDep,
    _: AttachmentManager,
) -> dict[str, Any]:
    row = await repo.get_attachment(principal.tenant_id, attachment_id)
    if row is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "ATTACHMENT_NOT_FOUND", "message": "attachment not found", "details": {}},
        )
    download_url = repo.signer.sign_download(
        tenant_id=UUID(str(principal.tenant_id)), storage_key=row["storage_key"]
    )
    return {"attachment": serialize_attachment(row), "download_url": download_url}


@router.delete("/attachments/{attachment_id}", response_model=PlatformObject, response_model_exclude_none=True, dependencies=[Depends(require_scope(ATTACHMENT_MANAGE))])
@router.delete(
    "/admin/attachments/{attachment_id}", response_model=PlatformObject,
    include_in_schema=False,
    dependencies=[Depends(require_scope(ATTACHMENT_MANAGE))],
)
async def delete_attachment(
    attachment_id: UUID,
    repo: RepoDep,
    principal: PrincipalDep,
    _: AttachmentManager,
) -> dict[str, Any]:
    """Delete the metadata reference (the object-store blob is cleaned up by the storage adapter)."""
    try:
        deleted = await repo.delete_attachment(
            tenant_id=principal.tenant_id, actor_id=principal.app_user_id, attachment_id=attachment_id
        )
    except ArtifactsError as exc:
        raise _http_error(exc) from exc
    return {"deleted": True, "id": deleted["id"], "storage_key": deleted["storage_key"]}


# -------------------------------------------------------------------------- document links ----
@router.post(
    "/document-links", response_model=PlatformObject,
    status_code=201,
    dependencies=[Depends(require_scope(DOCUMENTLINK_READ))],
)
@router.post(
    "/admin/document-links", response_model=PlatformObject,
    status_code=201,
    include_in_schema=False,
    dependencies=[Depends(require_scope(DOCUMENTLINK_READ))],
)
async def create_document_link(
    payload: DocumentLinkCreate,
    repo: RepoDep,
    principal: PrincipalDep,
    response: Response,
    _: DocumentLinkReader,
) -> dict[str, Any]:
    """Create a provenance link; duplicate natural keys return the existing row (idempotent)."""
    try:
        result = await repo.create_document_link(
            tenant_id=principal.tenant_id,
            actor_id=principal.app_user_id,
            payload=payload.model_dump(mode="json"),
        )
    except ArtifactsError as exc:
        raise _http_error(exc) from exc
    if not result.created:
        response.status_code = 200
    return {"link": result.link, "created": result.created}


@router.get("/document-links", response_model=PlatformObject, dependencies=[Depends(require_scope(DOCUMENTLINK_READ))])
@router.get(
    "/admin/document-links", response_model=PlatformObject,
    include_in_schema=False,
    dependencies=[Depends(require_scope(DOCUMENTLINK_READ))],
)
async def list_document_links(
    repo: RepoDep,
    principal: PrincipalDep,
    _: DocumentLinkReader,
    link_type: Annotated[str | None, Query(max_length=64)] = None,
    from_type: Annotated[str | None, Query(max_length=64)] = None,
    from_id: UUID | None = None,
    to_type: Annotated[str | None, Query(max_length=64)] = None,
    to_id: UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = DEFAULT_PAGE_LIMIT,
) -> dict[str, Any]:
    try:
        rows = await repo.list_document_links(
            principal.tenant_id,
            link_type=link_type,
            from_type=from_type,
            from_id=from_id,
            to_type=to_type,
            to_id=to_id,
            limit=clamp_limit(limit),
        )
    except ArtifactsError as exc:
        raise _http_error(exc) from exc
    return {"items": [serialize_document_link(row) for row in rows], "count": len(rows), "limit": limit}


@router.get("/document-links/{link_id}", response_model=PlatformObject, dependencies=[Depends(require_scope(DOCUMENTLINK_READ))])
@router.get(
    "/admin/document-links/{link_id}", response_model=PlatformObject,
    include_in_schema=False,
    dependencies=[Depends(require_scope(DOCUMENTLINK_READ))],
)
async def get_document_link(
    link_id: UUID,
    repo: RepoDep,
    principal: PrincipalDep,
    _: DocumentLinkReader,
) -> dict[str, Any]:
    row = await repo.get_document_link(principal.tenant_id, link_id)
    if row is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "DOCUMENT_LINK_NOT_FOUND", "message": "document link not found", "details": {}},
        )
    return {"link": serialize_document_link(row)}


@router.delete("/document-links/{link_id}", response_model=PlatformObject, response_model_exclude_none=True, dependencies=[Depends(require_scope(DOCUMENTLINK_READ))])
@router.delete(
    "/admin/document-links/{link_id}", response_model=PlatformObject,
    include_in_schema=False,
    dependencies=[Depends(require_scope(DOCUMENTLINK_READ))],
)
async def delete_document_link(
    link_id: UUID,
    repo: RepoDep,
    principal: PrincipalDep,
    _: DocumentLinkReader,
) -> dict[str, Any]:
    """Guarded delete: document_link is append-only (DM-PLAT-02); the attempt is audited + refused."""
    try:
        await repo.delete_document_link_guarded(
            tenant_id=principal.tenant_id, actor_id=principal.app_user_id, link_id=link_id
        )
    except ArtifactsError as exc:
        raise _http_error(exc) from exc
    raise HTTPException(  # pragma: no cover - unreachable: the guard always raises for an existing row
        status_code=500,
        detail={"code": "INTERNAL_ERROR", "message": "append-only guard did not raise", "details": {}},
    )


# -------------------------------------------------------------------- finance intents (read) --
@router.get("/finance/journal-intents", response_model=PlatformObject, dependencies=[Depends(require_scope(FINANCE_READ))])
async def list_finance_journal_intents(
    repo: RepoDep,
    principal: PrincipalDep,
    _: FinanceReader,
    transaction_id: UUID | None = None,
    source_doc_type: Annotated[str | None, Query(max_length=64)] = None,
    source_doc_id: UUID | None = None,
    posting_rule_id: Annotated[str | None, Query(max_length=64)] = None,
    status: Annotated[str | None, Query(max_length=32)] = None,
    valid_from: Annotated[date | None, Query(alias="from")] = None,
    valid_to: Annotated[date | None, Query(alias="to")] = None,
    cursor: Annotated[str | None, Query(max_length=512)] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = DEFAULT_PAGE_LIMIT,
) -> dict[str, Any]:
    """D2: read-only journal-intent rows (append-only store; no posting/GL mutation here)."""
    try:
        rows, next_cursor = await repo.list_finance_journal_intents(
            principal.tenant_id,
            transaction_id=transaction_id,
            source_doc_type=source_doc_type,
            source_doc_id=source_doc_id,
            posting_rule_id=posting_rule_id,
            status=status,
            date_from=valid_from,
            date_to=valid_to,
            cursor=cursor,
            limit=clamp_limit(limit),
        )
    except ArtifactsError as exc:
        raise _http_error(exc) from exc
    return {
        "items": [serialize_journal_intent(row) for row in rows],
        "count": len(rows),
        "limit": limit,
        "next_cursor": next_cursor,
    }


@router.get("/finance/journal-intents/{intent_id}", response_model=PlatformObject, dependencies=[Depends(require_scope(FINANCE_READ))])
async def get_finance_journal_intent(
    intent_id: UUID,
    repo: RepoDep,
    principal: PrincipalDep,
    _: FinanceReader,
) -> dict[str, Any]:
    row = await repo.get_finance_journal_intent(principal.tenant_id, intent_id)
    if row is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "JOURNAL_INTENT_NOT_FOUND", "message": "journal intent not found", "details": {}},
        )
    return serialize_journal_intent(row, include_payload=True)


@router.get("/finance/journal-intents/{id}", response_model=PlatformObject, dependencies=[Depends(require_scope(FINANCE_READ))])
async def get_finance_journal_intent_canonical(
    id: UUID,
    repo: RepoDep,
    principal: PrincipalDep,
    _: FinanceReader,
) -> dict[str, Any]:
    """Canonical ``{id}`` spelling of the frozen FS-00 contract operation."""
    return await get_finance_journal_intent(id, repo, principal, _)


__all__ = ["ROUTE_SCOPES", "get_upload_signer", "router"]
