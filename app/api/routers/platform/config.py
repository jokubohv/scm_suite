"""FS-00 reference/configuration admin API (task-41).

Read + controlled-write endpoints for the platform reference/configuration tables:

* currencies (global ISO-4217 reference rows; reads ``platform.reference.read``, writes are finance/
  reference projection maintenance ``platform.finance.sync`` — there is no catalogued tenant currency
  management scope, and ``currency`` is global reference data per DATA-MODEL §3 / DM-PLAT-10),
* fiscal periods (read ``platform.finance.read``, write ``platform.finance.sync``; finance owns period
  close — DM-PLAT-08/10 guards are enforced here),
* custom-field definitions (``platform.customfield.manage``; ``data_type`` immutable),
* feature flags (``platform.flag.manage``, tenant-scoped upsert, audited + ``platform.flag.changed``),
* number sequences (read-only ``platform.sequence.manage``; allocation/mutation belongs to
  :mod:`app.core.repositories.sequences`, owned by the posting engine).

Every non-public route declares exactly one catalogued capability scope through ``require_scope``
(deny-by-default); errors render through the existing problem+json handlers. The integrator registers
this module's ``router`` in ``app/api/main.py``/registry — this module never edits those files.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field

from app.api.deps import DbDep, PrincipalDep, require_scope
from app.api.schemas.platform import PlatformObject
from app.core.repositories.config import (
    ConfigError,
    ConfigRepository,
    parse_if_match,
)

router = APIRouter(prefix="/api/v1", tags=["platform-config"])

# Capability scopes (all catalogued in design/permissions.json; never invent a new scope string).
REFERENCE_READ = "platform.reference.read"
FINANCE_READ = "platform.finance.read"
FINANCE_SYNC = "platform.finance.sync"
CUSTOM_FIELD_MANAGE = "platform.customfield.manage"
FLAG_MANAGE = "platform.flag.manage"
SEQUENCE_MANAGE = "platform.sequence.manage"

#: (method, full path) -> exactly one catalogued scope; asserted by the router contract test and the
#: future route-scope coverage test.
ROUTE_SCOPES: dict[tuple[str, str], str] = {
    ("GET", "/api/v1/admin/currencies"): REFERENCE_READ,
    ("POST", "/api/v1/admin/currencies"): FINANCE_SYNC,
    ("GET", "/api/v1/admin/currencies/{code}"): REFERENCE_READ,
    ("PATCH", "/api/v1/admin/currencies/{code}"): FINANCE_SYNC,
    ("GET", "/api/v1/admin/fiscal-periods"): FINANCE_READ,
    ("POST", "/api/v1/admin/fiscal-periods"): FINANCE_SYNC,
    ("GET", "/api/v1/admin/custom-fields"): CUSTOM_FIELD_MANAGE,
    ("POST", "/api/v1/admin/custom-fields"): CUSTOM_FIELD_MANAGE,
    ("GET", "/api/v1/admin/custom-fields/{field_def_id}"): CUSTOM_FIELD_MANAGE,
    ("PATCH", "/api/v1/admin/custom-fields/{field_def_id}"): CUSTOM_FIELD_MANAGE,
    ("DELETE", "/api/v1/admin/custom-fields/{field_def_id}"): CUSTOM_FIELD_MANAGE,
    ("GET", "/api/v1/admin/flags"): FLAG_MANAGE,
    ("GET", "/api/v1/admin/flags/{flag_key}"): FLAG_MANAGE,
    ("PATCH", "/api/v1/admin/flags/{flag_key}"): FLAG_MANAGE,
    ("GET", "/api/v1/admin/sequences"): SEQUENCE_MANAGE,
    # FS-00 §6 spec-aligned aliases (additive): the task card names /admin/flags,/admin/sequences;
    # the FS-00 API matrix names /admin/feature-flags,/admin/number-sequences and the read
    # reference endpoints /reference/currencies,/reference/fiscal-periods.
    ("GET", "/api/v1/reference/currencies"): REFERENCE_READ,
    ("GET", "/api/v1/reference/fiscal-periods"): FINANCE_READ,
    ("GET", "/api/v1/admin/feature-flags"): FLAG_MANAGE,
    ("PUT", "/api/v1/admin/feature-flags/{flag_key}"): FLAG_MANAGE,
    ("GET", "/api/v1/admin/number-sequences"): SEQUENCE_MANAGE,
}


def _repo(db: DbDep) -> ConfigRepository:
    return ConfigRepository(db)


RepoDep = Annotated[ConfigRepository, Depends(_repo)]


# --------------------------------------------------------------------------------- errors -----
def _http_error(exc: ConfigError) -> HTTPException:
    return HTTPException(
        status_code=exc.status_code,
        detail={"code": exc.code, "message": exc.message, "details": exc.details},
    )


def _not_found(code: str, message: str, **details: Any) -> HTTPException:
    return HTTPException(status_code=404, detail={"code": code, "message": message, "details": details})


# ------------------------------------------------------------------------------- schemas ------
class CurrencyCreate(BaseModel):
    code: str = Field(min_length=3, max_length=3, description="ISO-4217 alpha-3 code")
    numeric_code: int | None = Field(default=None, ge=1, le=999)
    minor_units: int | None = Field(default=2, ge=0, le=4)
    is_active: bool = True


class CurrencyUpdate(BaseModel):
    # Accepted only to return the documented immutable-primary-key error (repo validates it).
    code: str | None = Field(default=None, description="immutable ISO-4217 code")
    numeric_code: int | None = Field(default=None, ge=1, le=999)
    minor_units: int | None = Field(default=None, ge=0, le=4)
    is_active: bool | None = None


class FiscalPeriodCreate(BaseModel):
    period_key: str = Field(min_length=1, max_length=32, examples=["2026-10"])
    start_date: date
    end_date: date
    status: Literal["OPEN", "CLOSED", "LOCKED"] = "OPEN"
    source_version: int = Field(default=1, ge=1)


class CustomFieldCreate(BaseModel):
    entity_type: str = Field(min_length=1, max_length=32)
    field_code: str = Field(min_length=2, max_length=64)
    label: str = Field(min_length=1, max_length=255)
    data_type: Literal["TEXT", "NUMBER", "DATE", "BOOL", "SELECT"]
    options: list[str] | None = None
    is_required: bool = False
    is_searchable: bool = False
    validation: dict[str, Any] | None = None


class CustomFieldUpdate(BaseModel):
    # ``data_type`` is accepted only to return the documented immutable-field error.
    data_type: Literal["TEXT", "NUMBER", "DATE", "BOOL", "SELECT"] | None = None
    label: str | None = Field(default=None, min_length=1, max_length=255)
    options: list[str] | None = None
    is_required: bool | None = None
    is_searchable: bool | None = None
    validation: dict[str, Any] | None = None


class FeatureFlagUpdate(BaseModel):
    is_enabled: bool
    notes: str | None = Field(default=None, max_length=500)


# ---------------------------------------------------------------------- dependencies -----
ReferenceReader = Annotated[object, Depends(require_scope(REFERENCE_READ))]
FinanceReader = Annotated[object, Depends(require_scope(FINANCE_READ))]
FinanceSyncWriter = Annotated[object, Depends(require_scope(FINANCE_SYNC))]
CustomFieldManager = Annotated[object, Depends(require_scope(CUSTOM_FIELD_MANAGE))]
FlagManager = Annotated[object, Depends(require_scope(FLAG_MANAGE))]
SequenceManager = Annotated[object, Depends(require_scope(SEQUENCE_MANAGE))]


# ------------------------------------------------------------------------ currencies ----------
async def _list_currencies(repo: ConfigRepository, *, is_active: bool | None) -> dict[str, Any]:
    items = await repo.list_currencies(is_active=is_active)
    return {"items": items, "count": len(items)}


@router.get("/admin/currencies", response_model=PlatformObject, dependencies=[Depends(require_scope(REFERENCE_READ))])
async def list_currencies(repo: RepoDep, _: ReferenceReader, is_active: bool | None = None) -> dict[str, Any]:
    return await _list_currencies(repo, is_active=is_active)


@router.get("/reference/currencies", response_model=PlatformObject, dependencies=[Depends(require_scope(REFERENCE_READ))])
async def list_reference_currencies(
    repo: RepoDep, _: ReferenceReader, is_active: bool | None = None
) -> dict[str, Any]:
    """FS-00 §6 reference read alias (read-only global currency list)."""
    return await _list_currencies(repo, is_active=is_active)


@router.post("/admin/currencies", response_model=PlatformObject, status_code=201)
async def create_currency(
    body: CurrencyCreate,
    repo: RepoDep,
    principal: PrincipalDep,
    _: FinanceSyncWriter,
) -> dict[str, Any]:
    try:
        return await repo.create_currency(
            body.model_dump(), actor_id=principal.app_user_id, actor_tenant_id=principal.tenant_id
        )
    except ConfigError as exc:
        raise _http_error(exc) from exc


@router.get("/admin/currencies/{code}", response_model=PlatformObject, dependencies=[Depends(require_scope(REFERENCE_READ))])
async def get_currency(code: str, repo: RepoDep, _: ReferenceReader) -> dict[str, Any]:
    try:
        currency = await repo.get_currency(code)
    except ConfigError as exc:
        raise _http_error(exc) from exc
    if currency is None:
        raise _not_found("CURRENCY_NOT_FOUND", f"currency {code!r} not found")
    return currency


@router.patch("/admin/currencies/{code}", response_model=PlatformObject)
async def update_currency(
    code: str,
    body: CurrencyUpdate,
    repo: RepoDep,
    principal: PrincipalDep,
    _: FinanceSyncWriter,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> dict[str, Any]:
    try:
        expected_version = parse_if_match(if_match)
        return await repo.update_currency(
            code,
            body.model_dump(exclude_unset=True),
            actor_id=principal.app_user_id,
            actor_tenant_id=principal.tenant_id,
            expected_version=expected_version,
        )
    except ConfigError as exc:
        raise _http_error(exc) from exc


# ---------------------------------------------------------------------- fiscal periods ---------
async def _list_fiscal_periods(
    repo: ConfigRepository, *, tenant_id: str, status: str | None, year: int | None
) -> dict[str, Any]:
    try:
        items = await repo.list_fiscal_periods(tenant_id, status=status, year=year)
    except ConfigError as exc:
        raise _http_error(exc) from exc
    return {"items": items, "count": len(items)}


@router.get("/admin/fiscal-periods", response_model=PlatformObject, dependencies=[Depends(require_scope(FINANCE_READ))])
async def list_fiscal_periods(
    repo: RepoDep,
    principal: PrincipalDep,
    _: FinanceReader,
    status: str | None = None,
    year: int | None = Query(default=None, ge=1900, le=2200),
) -> dict[str, Any]:
    return await _list_fiscal_periods(
        repo, tenant_id=principal.tenant_id, status=status, year=year
    )


@router.get("/reference/fiscal-periods", response_model=PlatformObject, dependencies=[Depends(require_scope(FINANCE_READ))])
async def list_reference_fiscal_periods(
    repo: RepoDep,
    principal: PrincipalDep,
    _: FinanceReader,
    status: str | None = None,
    year: int | None = Query(default=None, ge=1900, le=2200),
) -> dict[str, Any]:
    """FS-00 §6 reference read alias (finance period-status projection read-only)."""
    return await _list_fiscal_periods(
        repo, tenant_id=principal.tenant_id, status=status, year=year
    )


@router.post("/admin/fiscal-periods", response_model=PlatformObject, status_code=201)
async def create_fiscal_period(
    body: FiscalPeriodCreate,
    repo: RepoDep,
    principal: PrincipalDep,
    _: FinanceSyncWriter,
) -> dict[str, Any]:
    try:
        return await repo.create_fiscal_period(
            principal.tenant_id, body.model_dump(), actor_id=principal.app_user_id
        )
    except ConfigError as exc:
        raise _http_error(exc) from exc


# ------------------------------------------------------------------------ custom fields --------
@router.get("/admin/custom-fields", response_model=PlatformObject, dependencies=[Depends(require_scope(CUSTOM_FIELD_MANAGE))])
async def list_custom_fields(
    repo: RepoDep,
    principal: PrincipalDep,
    _: CustomFieldManager,
    entity_type: str | None = None,
) -> dict[str, Any]:
    try:
        items = await repo.list_custom_fields(principal.tenant_id, entity_type=entity_type)
    except ConfigError as exc:
        raise _http_error(exc) from exc
    return {"items": items, "count": len(items)}


@router.post("/admin/custom-fields", response_model=PlatformObject, status_code=201)
async def create_custom_field(
    body: CustomFieldCreate,
    repo: RepoDep,
    principal: PrincipalDep,
    _: CustomFieldManager,
) -> dict[str, Any]:
    try:
        return await repo.create_custom_field(
            principal.tenant_id, body.model_dump(), actor_id=principal.app_user_id
        )
    except ConfigError as exc:
        raise _http_error(exc) from exc


@router.get("/admin/custom-fields/{field_def_id}", response_model=PlatformObject, dependencies=[Depends(require_scope(CUSTOM_FIELD_MANAGE))])
async def get_custom_field(field_def_id: str, repo: RepoDep, principal: PrincipalDep, _: CustomFieldManager) -> dict[str, Any]:
    field = await repo.get_custom_field(principal.tenant_id, field_def_id)
    if field is None:
        raise _not_found("CUSTOM_FIELD_NOT_FOUND", "custom field definition not found")
    return field


@router.patch("/admin/custom-fields/{field_def_id}", response_model=PlatformObject)
async def update_custom_field(
    field_def_id: str,
    body: CustomFieldUpdate,
    repo: RepoDep,
    principal: PrincipalDep,
    _: CustomFieldManager,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> dict[str, Any]:
    try:
        expected_version = parse_if_match(if_match)
        return await repo.update_custom_field(
            principal.tenant_id,
            field_def_id,
            body.model_dump(exclude_unset=True),
            actor_id=principal.app_user_id,
            expected_version=expected_version,
        )
    except ConfigError as exc:
        raise _http_error(exc) from exc


@router.delete("/admin/custom-fields/{field_def_id}", response_model=PlatformObject)
async def delete_custom_field(
    field_def_id: str,
    repo: RepoDep,
    principal: PrincipalDep,
    _: CustomFieldManager,
) -> dict[str, Any]:
    try:
        deleted = await repo.delete_custom_field(
            principal.tenant_id, field_def_id, actor_id=principal.app_user_id
        )
    except ConfigError as exc:
        raise _http_error(exc) from exc
    return {"deleted": True, "field_def": deleted}


# ---------------------------------------------------------------------------- flags -----------
async def _list_flags(
    repo: ConfigRepository, *, tenant_id: str, flag_key: str | None
) -> dict[str, Any]:
    try:
        items = await repo.list_feature_flags(tenant_id, flag_key=flag_key)
    except ConfigError as exc:
        raise _http_error(exc) from exc
    return {"items": items, "count": len(items)}


@router.get("/admin/flags", response_model=PlatformObject, response_model_exclude_none=True, dependencies=[Depends(require_scope(FLAG_MANAGE))])
async def list_flags(
    repo: RepoDep,
    principal: PrincipalDep,
    _: FlagManager,
    flag_key: str | None = None,
) -> dict[str, Any]:
    return await _list_flags(repo, tenant_id=principal.tenant_id, flag_key=flag_key)


@router.get("/admin/feature-flags", response_model=PlatformObject, response_model_exclude_none=True, dependencies=[Depends(require_scope(FLAG_MANAGE))])
async def list_feature_flags_alias(
    repo: RepoDep,
    principal: PrincipalDep,
    _: FlagManager,
    flag_key: str | None = None,
) -> dict[str, Any]:
    """FS-00 §6 alias for the feature-flag list."""
    return await _list_flags(repo, tenant_id=principal.tenant_id, flag_key=flag_key)


@router.get("/admin/flags/{flag_key}", response_model=PlatformObject, response_model_exclude_none=True, dependencies=[Depends(require_scope(FLAG_MANAGE))])
async def get_flag(flag_key: str, repo: RepoDep, principal: PrincipalDep, _: FlagManager) -> dict[str, Any]:
    try:
        flag = await repo.get_feature_flag(principal.tenant_id, flag_key)
    except ConfigError as exc:
        raise _http_error(exc) from exc
    if flag is None:
        raise _not_found("FLAG_NOT_FOUND", f"feature flag {flag_key!r} is not overridden for this tenant")
    return flag


@router.patch("/admin/flags/{flag_key}", response_model=PlatformObject)
async def set_flag(
    flag_key: str,
    body: FeatureFlagUpdate,
    repo: RepoDep,
    principal: PrincipalDep,
    _: FlagManager,
) -> dict[str, Any]:
    try:
        return await repo.set_feature_flag(
            principal.tenant_id, flag_key, body.model_dump(), actor_id=principal.app_user_id
        )
    except ConfigError as exc:
        raise _http_error(exc) from exc


@router.put("/admin/feature-flags/{flag_key}", response_model=PlatformObject)
async def set_flag_put_alias(
    flag_key: str,
    body: FeatureFlagUpdate,
    repo: RepoDep,
    principal: PrincipalDep,
    _: FlagManager,
) -> dict[str, Any]:
    """FS-00 §6 PUT alias (same tenant-scoped, audited, idempotent upsert as PATCH)."""
    try:
        return await repo.set_feature_flag(
            principal.tenant_id, flag_key, body.model_dump(), actor_id=principal.app_user_id
        )
    except ConfigError as exc:
        raise _http_error(exc) from exc


# ------------------------------------------------------------------------ sequences -----------
async def _list_sequences(
    repo: ConfigRepository, *, tenant_id: str, doc_type: str | None, limit: int
) -> dict[str, Any]:
    try:
        items = await repo.list_number_sequences(tenant_id, doc_type=doc_type, limit=limit)
    except ConfigError as exc:
        raise _http_error(exc) from exc
    return {"items": items, "count": len(items)}


@router.get("/admin/sequences", response_model=PlatformObject, dependencies=[Depends(require_scope(SEQUENCE_MANAGE))])
async def list_sequences(
    repo: RepoDep,
    principal: PrincipalDep,
    _: SequenceManager,
    doc_type: str | None = None,
    limit: int = Query(default=200, ge=1, le=500),
) -> dict[str, Any]:
    return await _list_sequences(repo, tenant_id=principal.tenant_id, doc_type=doc_type, limit=limit)


@router.get("/admin/number-sequences", response_model=PlatformObject, dependencies=[Depends(require_scope(SEQUENCE_MANAGE))])
async def list_number_sequences_alias(
    repo: RepoDep,
    principal: PrincipalDep,
    _: SequenceManager,
    doc_type: str | None = None,
    limit: int = Query(default=200, ge=1, le=500),
) -> dict[str, Any]:
    """FS-00 §6 alias for sequence heads (read-only in this task)."""
    return await _list_sequences(repo, tenant_id=principal.tenant_id, doc_type=doc_type, limit=limit)


__all__ = ["ROUTE_SCOPES", "router"]
