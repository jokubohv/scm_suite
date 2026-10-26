# ruff: noqa: B008 - concise FastAPI Body(...) defaults for this router.
"""FS-13 consigned inventory API (`/api/v1`, catalogued scopes, idempotent writes; tenant isolated)."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header

from app.api.deps import DbDep, PrincipalDep, require_scope
from app.api.routing import SCMAPIRoute
from app.api.schemas import consigned as consigned_dto
from app.api.schemas.common import PROBLEM_RESPONSES
from app.modules.consigned.service import ConsignedService

router = APIRouter(prefix="/api/v1", responses=PROBLEM_RESPONSES, tags=["consigned"], route_class=SCMAPIRoute)

READ = "consigned.read"
AGREEMENT_WRITE = "consigned.agreement.write"
TRANSFER_WRITE = "consigned.transfer.write"
ADMIN = "consigned.admin"

ROUTE_SCOPES: dict[tuple[str, str], str] = {
    ("GET", "/api/v1/consignment-agreements"): READ,
    ("POST", "/api/v1/consignment-agreements"): AGREEMENT_WRITE,
    ("GET", "/api/v1/consignment-agreements/{agreement_id}"): READ,
    ("PATCH", "/api/v1/consignment-agreements/{agreement_id}"): AGREEMENT_WRITE,
    ("POST", "/api/v1/consignment-agreements/{agreement_id}/activate"): AGREEMENT_WRITE,
    ("POST", "/api/v1/consignment-agreements/{agreement_id}/suspend"): AGREEMENT_WRITE,
    ("POST", "/api/v1/consignment-agreements/{agreement_id}/terminate"): ADMIN,
    ("GET", "/api/v1/consignment-agreements/{agreement_id}/items"): READ,
    ("POST", "/api/v1/consignment-agreements/{agreement_id}/items"): AGREEMENT_WRITE,
    ("PATCH", "/api/v1/consignment-agreements/{agreement_id}/items/{item_id}"): AGREEMENT_WRITE,
    ("GET", "/api/v1/consignment-transfers"): READ,
    ("POST", "/api/v1/consignment-transfers"): TRANSFER_WRITE,
    ("GET", "/api/v1/consignment-transfers/{transfer_id}"): READ,
    ("POST", "/api/v1/consignment-transfers/{transfer_id}/submit"): TRANSFER_WRITE,
    ("POST", "/api/v1/consignment-transfers/{transfer_id}/approve"): TRANSFER_WRITE,
    ("POST", "/api/v1/consignment-transfers/{transfer_id}/post"): TRANSFER_WRITE,
    ("POST", "/api/v1/consignment-transfers/{transfer_id}/reverse"): ADMIN,
    ("POST", "/api/v1/consignment-transfers/{transfer_id}/void"): TRANSFER_WRITE,
    ("POST", "/api/v1/consignment-consumption"): TRANSFER_WRITE,
    ("GET", "/api/v1/consignment-consumption"): READ,
    ("GET", "/api/v1/consigned/balances"): READ,
    ("GET", "/api/v1/consignment/eligibility"): READ,
    ("POST", "/api/v1/consignment/guards/validate"): ADMIN,
    ("POST", "/api/v1/consignment/disable"): ADMIN,
    ("GET", "/api/v1/consignment/settings"): READ,
    ("PATCH", "/api/v1/consignment/settings"): ADMIN,
}
Idem = Annotated[str | None, Header(alias="Idempotency-Key")]


def _svc(db: DbDep, principal: PrincipalDep) -> ConsignedService:
    return ConsignedService(db, principal.tenant_id)


@router.get(
    "/consignment-agreements",
    dependencies=[Depends(require_scope(READ))],
    response_model=consigned_dto.ConsignmentAgreementPage,
)
async def list_agreements(
    db: DbDep, principal: PrincipalDep, vendor_id: str | None = None, status: str | None = None, limit: int = 50
) -> dict[str, Any]:
    return await _svc(db, principal).list_agreements(vendor_id=vendor_id, status=status, limit=limit)


@router.post(
    "/consignment-agreements",
    status_code=201,
    dependencies=[Depends(require_scope(AGREEMENT_WRITE))],
    response_model=consigned_dto.ConsignmentAgreementOut,
)
async def create_agreement(
    db: DbDep, principal: PrincipalDep, payload: consigned_dto.ConsignedCreateAgreementIn, idempotency_key: Idem = None
) -> dict[str, Any]:
    payload = payload.model_dump(exclude_unset=True)
    return await _svc(db, principal).create_agreement(
        payload, actor_id=principal.app_user_id, idempotency_key=idempotency_key
    )


@router.get(
    "/consignment-agreements/{agreement_id}",
    dependencies=[Depends(require_scope(READ))],
    response_model=consigned_dto.AgreementDetailOut,
)
async def get_agreement(agreement_id: str, db: DbDep, principal: PrincipalDep) -> dict[str, Any]:
    return await _svc(db, principal).get_agreement(agreement_id)


@router.patch(
    "/consignment-agreements/{agreement_id}",
    dependencies=[Depends(require_scope(AGREEMENT_WRITE))],
    response_model=consigned_dto.ConsignmentAgreementOut,
)
async def patch_agreement(
    agreement_id: str, db: DbDep, principal: PrincipalDep, payload: consigned_dto.ConsignedPatchAgreementIn
) -> dict[str, Any]:
    payload = payload.model_dump(exclude_unset=True)
    return await _svc(db, principal).update_agreement(
        agreement_id, payload, actor_id=principal.app_user_id, expected_version=payload.get("version")
    )


@router.post(
    "/consignment-agreements/{agreement_id}/activate",
    dependencies=[Depends(require_scope(AGREEMENT_WRITE))],
    response_model=consigned_dto.ConsignmentAgreementOut,
)
async def activate_agreement(
    agreement_id: str,
    db: DbDep,
    principal: PrincipalDep,
    payload: consigned_dto.ConsignedActivateAgreementIn = consigned_dto.ConsignedActivateAgreementIn(),
    idempotency_key: Idem = None,
) -> dict[str, Any]:
    payload = payload.model_dump(exclude_unset=True)
    return await _svc(db, principal).activate_agreement(
        agreement_id, actor_id=principal.app_user_id, idempotency_key=idempotency_key
    )


@router.post(
    "/consignment-agreements/{agreement_id}/suspend",
    dependencies=[Depends(require_scope(AGREEMENT_WRITE))],
    response_model=consigned_dto.ConsignmentAgreementOut,
)
async def suspend_agreement(
    agreement_id: str,
    db: DbDep,
    principal: PrincipalDep,
    payload: consigned_dto.ConsignedSuspendAgreementIn = consigned_dto.ConsignedSuspendAgreementIn(),
    idempotency_key: Idem = None,
) -> dict[str, Any]:
    payload = payload.model_dump(exclude_unset=True)
    return await _svc(db, principal).suspend_agreement(
        agreement_id, str(payload.get("reason") or "suspended"), actor_id=principal.app_user_id
    )


@router.post(
    "/consignment-agreements/{agreement_id}/terminate",
    dependencies=[Depends(require_scope(ADMIN))],
    response_model=consigned_dto.ConsignmentAgreementOut,
)
async def terminate_agreement(
    agreement_id: str,
    db: DbDep,
    principal: PrincipalDep,
    payload: consigned_dto.ConsignedTerminateAgreementIn = consigned_dto.ConsignedTerminateAgreementIn(),
    idempotency_key: Idem = None,
) -> dict[str, Any]:
    payload = payload.model_dump(exclude_unset=True)
    return await _svc(db, principal).terminate_agreement(
        agreement_id,
        str(payload.get("reason") or "terminated"),
        actor_id=principal.app_user_id,
        disposition_plan=payload.get("disposition_plan"),
    )


@router.get(
    "/consignment-agreements/{agreement_id}/items",
    dependencies=[Depends(require_scope(READ))],
    response_model=consigned_dto.ConsignmentAgreementItemPage,
)
async def list_agreement_items(agreement_id: str, db: DbDep, principal: PrincipalDep) -> dict[str, Any]:
    return await _svc(db, principal).list_agreement_items(agreement_id)


@router.post(
    "/consignment-agreements/{agreement_id}/items",
    status_code=201,
    dependencies=[Depends(require_scope(AGREEMENT_WRITE))],
    response_model=consigned_dto.ConsignmentAgreementItemOut,
)
async def add_agreement_item(
    agreement_id: str,
    db: DbDep,
    principal: PrincipalDep,
    payload: consigned_dto.ConsignedAddAgreementItemIn,
    idempotency_key: Idem = None,
) -> dict[str, Any]:
    payload = payload.model_dump(exclude_unset=True)
    return await _svc(db, principal).add_agreement_item(
        agreement_id, payload, actor_id=principal.app_user_id, idempotency_key=idempotency_key
    )


@router.patch(
    "/consignment-agreements/{agreement_id}/items/{item_id}",
    dependencies=[Depends(require_scope(AGREEMENT_WRITE))],
    response_model=consigned_dto.ConsignmentAgreementItemOut,
)
async def patch_agreement_item(
    agreement_id: str,
    item_id: str,
    db: DbDep,
    principal: PrincipalDep,
    payload: consigned_dto.ConsignedPatchAgreementItemIn,
) -> dict[str, Any]:
    payload = payload.model_dump(exclude_unset=True)
    return await _svc(db, principal).update_agreement_item(
        agreement_id, item_id, payload, actor_id=principal.app_user_id
    )


@router.get(
    "/consignment-transfers",
    dependencies=[Depends(require_scope(READ))],
    response_model=consigned_dto.ConsignmentOwnershipTransferPage,
)
async def list_transfers(
    db: DbDep,
    principal: PrincipalDep,
    agreement_id: str | None = None,
    status: str | None = None,
    vendor_id: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    return await _svc(db, principal).list_transfers(
        agreement_id=agreement_id, status=status, vendor_id=vendor_id, limit=limit
    )


@router.post(
    "/consignment-transfers",
    status_code=201,
    dependencies=[Depends(require_scope(TRANSFER_WRITE))],
    response_model=consigned_dto.ConsignmentOwnershipTransferOut,
)
async def create_transfer(
    db: DbDep, principal: PrincipalDep, payload: consigned_dto.ConsignedCreateTransferIn, idempotency_key: Idem = None
) -> dict[str, Any]:
    payload = payload.model_dump(exclude_unset=True)
    return await _svc(db, principal).create_transfer(
        payload, actor_id=principal.app_user_id, idempotency_key=idempotency_key
    )


@router.get(
    "/consignment-transfers/{transfer_id}",
    dependencies=[Depends(require_scope(READ))],
    response_model=consigned_dto.TransferDetailOut,
)
async def get_transfer(transfer_id: str, db: DbDep, principal: PrincipalDep) -> dict[str, Any]:
    return await _svc(db, principal).get_transfer(transfer_id)


@router.post(
    "/consignment-transfers/{transfer_id}/submit",
    dependencies=[Depends(require_scope(TRANSFER_WRITE))],
    response_model=consigned_dto.ConsignedActionOut,
)
async def submit_transfer(
    transfer_id: str, db: DbDep, principal: PrincipalDep, idempotency_key: Idem = None
) -> dict[str, Any]:
    return await _svc(db, principal).submit_transfer(
        transfer_id, actor_id=principal.app_user_id, idempotency_key=idempotency_key
    )


@router.post(
    "/consignment-transfers/{transfer_id}/approve",
    dependencies=[Depends(require_scope(TRANSFER_WRITE))],
    response_model=consigned_dto.ConsignedActionOut,
)
async def approve_transfer(
    transfer_id: str,
    db: DbDep,
    principal: PrincipalDep,
    payload: consigned_dto.ConsignedApproveTransferIn = consigned_dto.ConsignedApproveTransferIn(),
    idempotency_key: Idem = None,
) -> dict[str, Any]:
    payload = payload.model_dump(exclude_unset=True)
    return await _svc(db, principal).approve_transfer(
        transfer_id,
        actor_id=principal.app_user_id,
        approval_ref=payload.get("approval_ref"),
        idempotency_key=idempotency_key,
    )


@router.post(
    "/consignment-transfers/{transfer_id}/post",
    dependencies=[Depends(require_scope(TRANSFER_WRITE))],
    response_model=consigned_dto.TransferDetailOut,
)
async def post_transfer(
    transfer_id: str,
    db: DbDep,
    principal: PrincipalDep,
    payload: consigned_dto.ConsignedPostTransferIn = consigned_dto.ConsignedPostTransferIn(),
    idempotency_key: Idem = None,
) -> dict[str, Any]:
    payload = payload.model_dump(exclude_unset=True)
    return await _svc(db, principal).post_transfer(
        transfer_id,
        actor_id=principal.app_user_id,
        idempotency_key=idempotency_key,
        external_ref=payload.get("external_ref"),
    )


@router.post(
    "/consignment-transfers/{transfer_id}/reverse",
    dependencies=[Depends(require_scope(ADMIN))],
    response_model=consigned_dto.ConsignedActionOut,
)
async def reverse_transfer(
    transfer_id: str,
    db: DbDep,
    principal: PrincipalDep,
    payload: consigned_dto.ConsignedReverseTransferIn = consigned_dto.ConsignedReverseTransferIn(),
    idempotency_key: Idem = None,
) -> dict[str, Any]:
    payload = payload.model_dump(exclude_unset=True)
    return await _svc(db, principal).reverse_transfer(
        transfer_id,
        str(payload.get("reason") or "reversal"),
        actor_id=principal.app_user_id,
        idempotency_key=idempotency_key,
    )


@router.post(
    "/consignment-transfers/{transfer_id}/void",
    dependencies=[Depends(require_scope(TRANSFER_WRITE))],
    response_model=consigned_dto.ConsignedActionOut,
)
async def void_transfer(
    transfer_id: str,
    db: DbDep,
    principal: PrincipalDep,
    payload: consigned_dto.ConsignedVoidTransferIn = consigned_dto.ConsignedVoidTransferIn(),
    idempotency_key: Idem = None,
) -> dict[str, Any]:
    payload = payload.model_dump(exclude_unset=True)
    return await _svc(db, principal).void_transfer(
        transfer_id, str(payload.get("reason") or "voided"), actor_id=principal.app_user_id
    )


@router.post(
    "/consignment-consumption",
    dependencies=[Depends(require_scope(TRANSFER_WRITE))],
    response_model=consigned_dto.ConsumptionResultOut,
)
async def consume(
    db: DbDep, principal: PrincipalDep, payload: consigned_dto.ConsignedConsumeIn, idempotency_key: Idem = None
) -> dict[str, Any]:
    payload = payload.model_dump(exclude_unset=True)
    return await _svc(db, principal).consume(payload, actor_id=principal.app_user_id, idempotency_key=idempotency_key)


@router.get(
    "/consignment-consumption",
    dependencies=[Depends(require_scope(READ))],
    response_model=consigned_dto.ConsignmentConsumptionPage,
)
async def list_consumption(
    db: DbDep,
    principal: PrincipalDep,
    agreement_id: str | None = None,
    item_id: str | None = None,
    period_key: str | None = None,
    vendor_bill_id: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    return await _svc(db, principal).list_consumption(
        agreement_id=agreement_id, item_id=item_id, period_key=period_key, vendor_bill_id=vendor_bill_id, limit=limit
    )


@router.get(
    "/consigned/balances",
    dependencies=[Depends(require_scope(READ))],
    response_model=consigned_dto.ConsignedBalancesOut,
)
async def consigned_balances(
    db: DbDep,
    principal: PrincipalDep,
    vendor_id: str | None = None,
    agreement_id: str | None = None,
    item_id: str | None = None,
    location_id: str | None = None,
) -> dict[str, Any]:
    return await _svc(db, principal).balances(
        vendor_id=vendor_id, agreement_id=agreement_id, item_id=item_id, location_id=location_id
    )


@router.get(
    "/consignment/eligibility",
    dependencies=[Depends(require_scope(READ))],
    response_model=consigned_dto.ConsignedEligibilityOut,
)
async def eligibility(
    db: DbDep,
    principal: PrincipalDep,
    item_id: str,
    location_id: str,
    vendor_id: str | None = None,
    agreement_id: str | None = None,
) -> dict[str, Any]:
    return await _svc(db, principal).eligibility(
        item_id=item_id, location_id=location_id, vendor_id=vendor_id, agreement_id=agreement_id
    )


@router.post(
    "/consignment/guards/validate",
    dependencies=[Depends(require_scope(ADMIN))],
    response_model=consigned_dto.ConsignedGuardOut,
)
async def validate_guards(db: DbDep, principal: PrincipalDep, idempotency_key: Idem = None) -> dict[str, Any]:
    return await _svc(db, principal).validate_guards()


@router.post(
    "/consignment/disable",
    dependencies=[Depends(require_scope(ADMIN))],
    response_model=consigned_dto.ConsignedActionOut,
)
async def disable_consigned(db: DbDep, principal: PrincipalDep, idempotency_key: Idem = None) -> dict[str, Any]:
    return await _svc(db, principal).disable(actor_id=principal.app_user_id)


@router.get(
    "/consignment/settings",
    dependencies=[Depends(require_scope(READ))],
    response_model=consigned_dto.ConsignedSettingsOut,
)
async def get_settings(db: DbDep, principal: PrincipalDep) -> dict[str, Any]:
    return await _svc(db, principal).get_settings()


@router.patch(
    "/consignment/settings",
    dependencies=[Depends(require_scope(ADMIN))],
    response_model=consigned_dto.ConsignedSettingsOut,
)
async def patch_settings(
    db: DbDep, principal: PrincipalDep, payload: consigned_dto.ConsignedPatchSettingsIn, idempotency_key: Idem = None
) -> dict[str, Any]:
    payload = payload.model_dump(exclude_unset=True)
    return await _svc(db, principal).patch_settings(payload, actor_id=principal.app_user_id)


# ------------------------------------------------------------------ canonical {id} aliases ----
# The frozen FS-13 contract declares canonical ``{id}`` resource placeholders while the implementation
# historically mounted ``{agreement_id}``/``{transfer_id}``. These aliases mount the declared operations
# without changing concrete URLs or behaviour.
import inspect as _inspect  # noqa: E402

_CONSIGNED_CANONICAL_ALIASES: tuple[tuple[str, str, str, str], ...] = (
    ("GET", "/api/v1/consignment-agreements/{agreement_id}", "/consignment-agreements/{id}", "agreement_id"),
    (
        "POST",
        "/api/v1/consignment-agreements/{agreement_id}/activate",
        "/consignment-agreements/{id}/activate",
        "agreement_id",
    ),
    (
        "GET",
        "/api/v1/consignment-agreements/{agreement_id}/items",
        "/consignment-agreements/{id}/items",
        "agreement_id",
    ),
    (
        "POST",
        "/api/v1/consignment-agreements/{agreement_id}/items",
        "/consignment-agreements/{id}/items",
        "agreement_id",
    ),
    (
        "PATCH",
        "/api/v1/consignment-agreements/{agreement_id}/items/{item_id}",
        "/consignment-agreements/{id}/items/{item_id}",
        "agreement_id",
    ),
    (
        "POST",
        "/api/v1/consignment-agreements/{agreement_id}/suspend",
        "/consignment-agreements/{id}/suspend",
        "agreement_id",
    ),
    (
        "POST",
        "/api/v1/consignment-agreements/{agreement_id}/terminate",
        "/consignment-agreements/{id}/terminate",
        "agreement_id",
    ),
    ("GET", "/api/v1/consignment-transfers/{transfer_id}", "/consignment-transfers/{id}", "transfer_id"),
    ("POST", "/api/v1/consignment-transfers/{transfer_id}/submit", "/consignment-transfers/{id}/submit", "transfer_id"),
    (
        "POST",
        "/api/v1/consignment-transfers/{transfer_id}/approve",
        "/consignment-transfers/{id}/approve",
        "transfer_id",
    ),
    ("POST", "/api/v1/consignment-transfers/{transfer_id}/post", "/consignment-transfers/{id}/post", "transfer_id"),
    (
        "POST",
        "/api/v1/consignment-transfers/{transfer_id}/reverse",
        "/consignment-transfers/{id}/reverse",
        "transfer_id",
    ),
    ("POST", "/api/v1/consignment-transfers/{transfer_id}/void", "/consignment-transfers/{id}/void", "transfer_id"),
)


def _alias_endpoint(endpoint: Any, implementation_param: str) -> Any:
    signature = _inspect.signature(endpoint)
    parameters = [
        parameter.replace(name="id") if parameter.name == implementation_param else parameter
        for parameter in signature.parameters.values()
    ]

    async def _alias(**kwargs: Any) -> Any:
        kwargs[implementation_param] = kwargs.pop("id")
        return await endpoint(**kwargs)

    _alias.__signature__ = signature.replace(parameters=parameters)  # type: ignore[attr-defined]
    _alias.__name__ = f"{endpoint.__name__}_canonical"
    return _alias


def _register_canonical_aliases() -> None:
    routes_by_path: dict[tuple[str, str], Any] = {}
    for route in router.routes:
        for method in getattr(route, "methods", None) or set():
            routes_by_path[(method.upper(), getattr(route, "path", ""))] = route
    for method, declared_path, relative_path, implementation_param in _CONSIGNED_CANONICAL_ALIASES:
        alias_route: Any = routes_by_path.get((method, declared_path))
        if alias_route is None:  # pragma: no cover - defensive: drift detected by route-surface tests
            continue
        alias = _alias_endpoint(alias_route.endpoint, implementation_param)
        router.add_api_route(
            relative_path,
            alias,
            methods=[method],
            status_code=int(getattr(alias_route, "status_code", None) or 200),
            dependencies=[Depends(require_scope(ROUTE_SCOPES[(method, declared_path)]))],
            response_model=getattr(alias_route, "response_model", None),
        )
        ROUTE_SCOPES[(method, "/api/v1" + relative_path)] = ROUTE_SCOPES[(method, declared_path)]


_register_canonical_aliases()


__all__ = ["ROUTE_SCOPES", "router"]
