"""Central handlers for every seeded ``platform.job_definition`` (FS-00 residual task).

Each seeded job maps to either:

* an existing async service method (``sweep``/``command`` handlers below), invoked inside the worker's
  tenant-scoped transaction, or
* a documented idempotent no-op (``noop`` specs with an explicit ``reason``) when the feature is
  deploy-dark, event-driven elsewhere, payload/document-driven, or the repair path is not implemented
  by the owning feature set yet.

Handlers return JSON-able dicts; the worker wraps them with the standard job-result envelope and owns
retries/advisory locks/timeouts. No handler posts inventory directly except through owning services.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import text

from app.core.jobs.types import JobContext, JobKind, JobSpec

__all__ = ["JOB_SPECS", "SEEDED_FEATURE_SETS"]

SEEDED_FEATURE_SETS = (
    "FS-00",
    "FS-01",
    "FS-02",
    "FS-03",
    "FS-04",
    "FS-05",
    "FS-06",
    "FS-07",
    "FS-08",
    "FS-10",
    "FS-12",
    "FS-13",
    "FS-14",
    "FS-15",
)

SYSTEM_TENANT_ID = "00000000-0000-0000-0000-000000000000"


# --------------------------------------------------------------------------- helpers -------------
def _jsonable(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, list | tuple | set):
        return [_jsonable(item) for item in value]
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return value


def _parse_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def _skip(reason: str, **extra: Any) -> dict[str, Any]:
    return {"status": "skipped", "reason": reason, **extra}


async def _tenant_setting(session: Any, tenant_id: str, key: str) -> Any:
    row = await session.execute(
        text(
            "SELECT value FROM platform.tenant_setting "
            "WHERE tenant_id = :tenant AND namespace = 'inventory' AND key = :key"
        ),
        {"tenant": tenant_id, "key": f"quality.{key}"},
    )
    return row.scalar_one_or_none()


# =========================================================================== FS-00 platform =======
async def platform_outbox_relay_job(ctx: JobContext) -> dict[str, Any]:
    return _skip(
        "dedicated relay process owns the outbox polling loop",
        process="python -m app.core.outbox_relay",
        job=ctx.job_name,
    )


async def platform_retention_purge_job(ctx: JobContext) -> dict[str, Any]:
    return _skip(
        "retention purge implementation deferred; retention_ledger_years setting validated by platform admin",
        setting="retention_ledger_years",
    )


async def platform_sso_health_monitor_job(ctx: JobContext) -> dict[str, Any]:
    return _skip("SSO connections are health-checked on demand by the auth context; no scheduled probe yet")


async def platform_scheduler_job(ctx: JobContext) -> dict[str, Any]:
    return _skip("external scheduler/RQ cron drives jobs; this worker executes resolved job names")


# =========================================================================== FS-01 item ===========
async def item_reference_validate_job(ctx: JobContext) -> dict[str, Any]:
    from app.modules.item_master.service import ItemMasterService

    service = ItemMasterService(ctx.session, ctx.tenant_id)
    await service.ensure_reference()
    return {"reference": "validated", "tenant_id": ctx.tenant_id}


async def item_inventory_defaults_repair_job(ctx: JobContext) -> dict[str, Any]:
    result = await ctx.session.execute(
        text(
            """
            SELECT count(*) AS missing_defaults
              FROM item_master.item i
             WHERE i.tenant_id = :tenant
               AND NOT EXISTS (
                   SELECT 1 FROM item_master.item_inventory x
                    WHERE x.tenant_id = i.tenant_id AND x.item_id = i.id
               )
            """
        ),
        {"tenant": ctx.tenant_id},
    )
    missing = int(result.scalar_one())
    return {
        "items_checked": "all",
        "missing_inventory_defaults": missing,
        "repair": "read-only detection; repair runs through the item-master inventory API",
    }


# =========================================================================== FS-02 pricing ========
async def pricing_apply_effective_updates_job(ctx: JobContext) -> dict[str, Any]:
    update_id = ctx.get("update_id")
    if not update_id:
        return _skip("price updates are applied on their effective date by the owning document flow")
    from app.modules.pricing.service import PricingService

    service = PricingService(ctx.session)
    return await service.apply_price_update(
        tenant_id=ctx.tenant_id,
        update_id=str(update_id),
        as_of=_parse_date(ctx.get("as_of")),
        actor_id=ctx.actor_id,
    )


async def pricing_recompute_dynamic_collections_job(ctx: JobContext) -> dict[str, Any]:
    from app.modules.pricing.service import PricingService

    service = PricingService(ctx.session)
    return await service.recompute_collection(
        tenant_id=ctx.tenant_id,
        collection_id=str(ctx.get("collection_id")) if ctx.get("collection_id") else None,
        actor_id=ctx.actor_id,
    )


# =========================================================================== FS-03 traceability ===
async def traceability_bootstrap_sentinel_job(ctx: JobContext) -> dict[str, Any]:
    from app.modules.traceability.service import run_bootstrap_sentinel

    return await run_bootstrap_sentinel(ctx.session, ctx.tenant_id)


async def traceability_deferred_fk_close_job(ctx: JobContext) -> dict[str, Any]:
    from app.modules.traceability.service import run_deferred_fk_close

    return await run_deferred_fk_close(ctx.session, ctx.tenant_id)


# =========================================================================== FS-04 ledger ==========
async def ledger_reconcile_balances_job(ctx: JobContext) -> dict[str, Any]:
    mismatches = await ctx.session.execute(
        text(
            """
            WITH detail_totals AS (
                SELECT d.tenant_id, l.item_id, l.location_id, d.inventory_status_id,
                       d.ownership, d.lot_id, d.serial_id, sum(d.qty_base) AS detail_qty
                  FROM inventory_ledger.inventory_transaction_detail d
                  JOIN inventory_ledger.inventory_transaction_line l
                    ON l.tenant_id = d.tenant_id AND l.id = d.inventory_transaction_line_id
                 WHERE d.tenant_id = :tenant
                 GROUP BY d.tenant_id, l.item_id, l.location_id, d.inventory_status_id,
                          d.ownership, d.lot_id, d.serial_id
            )
            SELECT count(*) AS mismatches
              FROM inventory_ledger.inventory_balance b
              LEFT JOIN detail_totals d
                ON d.tenant_id = b.tenant_id AND d.item_id = b.item_id
               AND d.location_id = b.location_id AND d.inventory_status_id = b.inventory_status_id
               AND d.ownership = b.ownership
               AND d.lot_id IS NOT DISTINCT FROM b.lot_id
               AND d.serial_id IS NOT DISTINCT FROM b.serial_id
             WHERE b.tenant_id = :tenant
               AND b.qty_on_hand <> COALESCE(d.detail_qty, 0)
            """
        ),
        {"tenant": ctx.tenant_id},
    )
    summary = await ctx.session.execute(
        text(
            """
            SELECT count(*) AS balances,
                   count(*) FILTER (WHERE qty_on_hand < 0) AS negative_balances,
                   count(*) FILTER (WHERE qty_on_hand <> qty_committed + qty_available) AS availability_mismatch
              FROM inventory_ledger.inventory_balance
             WHERE tenant_id = :tenant
            """
        ),
        {"tenant": ctx.tenant_id},
    )
    row = dict(summary.mappings().one())
    return {
        "balances": int(row["balances"]),
        "negative_balances": int(row["negative_balances"]),
        "availability_mismatch": int(row["availability_mismatch"]),
        "detail_balance_mismatch": int(mismatches.scalar_one() or 0),
    }


# =========================================================================== FS-05 costing =========
async def costing_reconcile_job(ctx: JobContext) -> dict[str, Any]:
    from app.modules.costing.service import CostingService

    service = CostingService(ctx.session, ctx.tenant_id)
    return await service.reconciliation(as_of=ctx.get("as_of"))


async def costing_recalculate_item_cost_job(ctx: JobContext) -> dict[str, Any]:
    from app.modules.costing.service import CostingService

    service = CostingService(ctx.session, ctx.tenant_id)
    item_id = ctx.get("item_id")
    if item_id:
        return await service.request_recalculation({**dict(ctx.payload), "item_id": str(item_id)})
    return await service.pending_recalculations()


# =========================================================================== FS-06 numbered ========
async def traceability_deferred_fks_validate_job(ctx: JobContext) -> dict[str, Any]:
    from app.modules.traceability_numbered.service import NumberedService

    return await NumberedService(ctx.session).deferred_fk_status()


async def traceability_expiry_scan_job(ctx: JobContext) -> dict[str, Any]:
    from app.modules.traceability_numbered.service import NumberedService

    horizons = ctx.get("horizons") or (30, 60, 90)
    warnings = await NumberedService(ctx.session).expiry_scan(
        ctx.tenant_id, horizons=tuple(int(value) for value in horizons)
    )
    return {"warnings": len(warnings), "items": warnings[:50]}


# =========================================================================== FS-07 counts ===========
async def counts_generate_cycle_counts_job(ctx: JobContext) -> dict[str, Any]:
    from app.modules.counts.service import CountsService

    service = CountsService(ctx.session, ctx.tenant_id)
    return await service.run_cycle_generation(run_date=_parse_date(ctx.get("run_date")))


async def counts_snapshot_refresh_job(ctx: JobContext) -> dict[str, Any]:
    count_id = ctx.get("count_id")
    if not count_id:
        return _skip("count snapshot refresh is document-scoped; pass count_id in the job payload")
    from app.modules.counts.service import CountsService

    service = CountsService(ctx.session, ctx.tenant_id)
    return await service.snapshot(str(count_id), revision=ctx.get("revision"))


async def counts_stale_cleanup_job(ctx: JobContext) -> dict[str, Any]:
    from app.modules.counts.service import CountsService

    service = CountsService(ctx.session, ctx.tenant_id)
    return await service.stale_cleanup(dry_run=bool(ctx.get("dry_run", False)))


# =========================================================================== FS-08 procurement ======
async def procurement_receipt_reconcile_job(ctx: JobContext) -> dict[str, Any]:
    from app.modules.procurement.service import ProcurementService

    service = ProcurementService(ctx.session, ctx.tenant_id)
    return await service.received_not_billed(
        limit=int(ctx.get("limit", 100)),
    )


async def procurement_match_run_job(ctx: JobContext) -> dict[str, Any]:
    bill_id = ctx.get("bill_id")
    if not bill_id:
        return _skip("three-way match runs per bill via the bills API; pass bill_id in the job payload")
    from app.modules.procurement.service import ProcurementService

    service = ProcurementService(ctx.session, ctx.tenant_id)
    return await service.match_bill(
        bill_id,
        override=bool(ctx.get("override", False)),
        override_reason=ctx.get("override_reason"),
        actor_id=ctx.actor_id,
        idempotency_key=str(ctx.get("idempotency_key")) if ctx.get("idempotency_key") else None,
    )


async def procurement_po_close_overdue_job(ctx: JobContext) -> dict[str, Any]:
    po_id = ctx.get("po_id")
    if not po_id:
        return _skip("overdue PO closure is document-scoped; pass po_id in the job payload")
    from app.modules.procurement.service import ProcurementService

    service = ProcurementService(ctx.session, ctx.tenant_id)
    return await service.close_purchase_order(po_id, actor_id=ctx.actor_id)


# =========================================================================== FS-10 planning =========
async def atp_rebuild_job(ctx: JobContext) -> dict[str, Any]:
    from app.modules.replenishment.service import PlanningService

    service = PlanningService(ctx.session, ctx.tenant_id)
    return await service.run_atp_job(as_of=_parse_date(ctx.get("as_of")))


async def planning_demand_calculate_job(ctx: JobContext) -> dict[str, Any]:
    from app.modules.replenishment.service import PlanningService

    return await PlanningService(ctx.session, ctx.tenant_id).run_demand_job(as_of=_parse_date(ctx.get("as_of")))


async def planning_supply_run_job(ctx: JobContext) -> dict[str, Any]:
    from app.modules.replenishment.service import PlanningService

    return await PlanningService(ctx.session, ctx.tenant_id).run_supply_job(as_of=_parse_date(ctx.get("as_of")))


async def planning_allocation_run_job(ctx: JobContext) -> dict[str, Any]:
    if not ctx.payload.get("run_id"):
        return _skip("allocation runs are queued documents; pass run_id in the job payload")
    from app.modules.replenishment.service import PlanningService

    return await PlanningService(ctx.session, ctx.tenant_id).execute_allocation_run(dict(ctx.payload))


async def replenishment_aim_job(ctx: JobContext) -> dict[str, Any]:
    from app.modules.replenishment.service import PlanningService

    return await PlanningService(ctx.session, ctx.tenant_id).run_aim_job(
        calculation_day=_parse_date(ctx.get("calculation_day"))
    )


async def replenishment_ordering_job(ctx: JobContext) -> dict[str, Any]:
    from app.modules.replenishment.service import PlanningService

    payload = {key: value for key, value in ctx.payload.items() if key not in {"job", "run_key"}}
    return await PlanningService(ctx.session, ctx.tenant_id).order_items(**payload)


# =========================================================================== FS-12 quality ==========
async def quality_trigger_counter_reset_job(ctx: JobContext) -> dict[str, Any]:
    trigger_id = ctx.get("trigger_id")
    sql = (
        "UPDATE quality.inspection_trigger SET evaluation_counter = 0, counter_reset_at = now(), "
        "updated_at = now(), version = version + 1 "
        "WHERE tenant_id = :tenant"
    )
    params: dict[str, Any] = {"tenant": ctx.tenant_id}
    if trigger_id:
        sql += " AND id = :trigger"
        params["trigger"] = str(trigger_id)
    result = await ctx.session.execute(text(sql + " RETURNING id::text AS id"), params)
    reset = [row[0] for row in result.all()]
    return {"triggers_reset": len(reset), "trigger_ids": reset[:100]}


async def quality_hold_reconcile_job(ctx: JobContext) -> dict[str, Any]:
    links = await ctx.session.execute(
        text(
            "SELECT link_type, count(*) AS count FROM platform.document_link "
            "WHERE tenant_id = :tenant AND from_type = 'quality.inspection' "
            "AND (link_type LIKE 'QUALITY_HOLD%' OR link_type LIKE 'QUALITY_RELEASE%') "
            "GROUP BY link_type"
        ),
        {"tenant": ctx.tenant_id},
    )
    applied = sum(int(row["count"]) for row in links.mappings() if str(row["link_type"]).startswith("QUALITY_HOLD"))
    released = sum(int(row["count"]) for row in links.mappings() if str(row["link_type"]).startswith("QUALITY_RELEASE"))
    postings = await ctx.session.scalar(
        text(
            "SELECT count(*) FROM inventory_ledger.inventory_transaction "
            "WHERE tenant_id = :tenant AND transaction_type = 'T-30' "
            "AND source_doc_type = 'quality.inspection'"
        ),
        {"tenant": ctx.tenant_id},
    )
    expected_postings = applied + released
    drift = int(postings or 0) - expected_postings
    return {
        "hold_links": applied,
        "release_links": released,
        "t30_postings": int(postings or 0),
        "unreleased_holds": max(applied - released, 0),
        "drift": drift,
        "consistent": drift == 0,
    }


async def quality_coa_auto_issue_job(ctx: JobContext) -> dict[str, Any]:
    setting = await _tenant_setting(ctx.session, ctx.tenant_id, "auto_coa")
    enabled = setting if isinstance(setting, bool) else str(setting).lower() in {"true", "1"}
    if not enabled:
        return _skip("quality.auto_coa is off for this tenant", setting="auto_coa")
    limit = int(ctx.get("limit", 50))
    result = await ctx.session.execute(
        text(
            """
            SELECT i.id::text AS id
              FROM quality.inspection i
             WHERE i.tenant_id = :tenant AND i.status = 'PASSED'
               AND NOT EXISTS (
                   SELECT 1 FROM quality.coa c
                    WHERE c.tenant_id = i.tenant_id AND c.inspection_id = i.id
               )
             ORDER BY i.completed_at NULLS LAST
             LIMIT :limit
            """
        ),
        {"tenant": ctx.tenant_id, "limit": limit},
    )
    inspection_ids = [row["id"] for row in result.mappings()]
    if not inspection_ids:
        return {"issued": 0, "inspection_ids": []}
    from app.modules.quality.service import QualityService

    service = QualityService(ctx.session)
    issued: list[str] = []
    errors: list[dict[str, str]] = []
    for inspection_id in inspection_ids:
        try:
            coa = await service.create_coa(tenant_id=ctx.tenant_id, values={"inspection_id": inspection_id})
            issued.append(str(coa["id"]))
        except Exception as exc:  # noqa: BLE001 - keep sweeping; report per-inspection failures
            errors.append({"inspection_id": inspection_id, "error": str(exc)})
    return {"issued": len(issued), "coa_ids": issued, "errors": errors[:20]}


# =========================================================================== FS-13 consigned ========
async def consigned_stock_guard_job(ctx: JobContext) -> dict[str, Any]:
    from app.modules.consigned.service import ConsignedService

    return await ConsignedService(ctx.session, ctx.tenant_id).validate_guards()


async def consigned_ownership_reconcile_job(ctx: JobContext) -> dict[str, Any]:
    from app.modules.consigned.service import ConsignedService

    return await ConsignedService(ctx.session, ctx.tenant_id).balances()


async def consigned_consumption_reconcile_job(ctx: JobContext) -> dict[str, Any]:
    from app.modules.consigned.service import ConsignedService

    return await ConsignedService(ctx.session, ctx.tenant_id).list_consumption(limit=int(ctx.get("limit", 100)))


async def consigned_agreement_expiry_job(ctx: JobContext) -> dict[str, Any]:
    from app.modules.consigned.service import ConsignedService

    service = ConsignedService(ctx.session, ctx.tenant_id)
    result = await service.list_agreements(status="ACTIVE", limit=int(ctx.get("limit", 500)))
    today = date.today()
    expired = []
    for row in result.get("items", []):
        end_date = _parse_date(row.get("end_date")) if isinstance(row, Mapping) else None
        if end_date is not None and end_date < today:
            expired.append({"agreement_id": row.get("id"), "end_date": end_date.isoformat()})
    return {"active_agreements": len(result.get("items", [])), "expired": len(expired), "agreements": expired}


# =========================================================================== FS-14 extensions =======
async def extensions_claims_reconcile_job(ctx: JobContext) -> dict[str, Any]:
    from app.modules.extensions.service import ExtensionService

    return await ExtensionService(ctx.session, ctx.tenant_id).list_claims(limit=int(ctx.get("limit", 100)))


async def extensions_upc_reconcile_job(ctx: JobContext) -> dict[str, Any]:
    from app.modules.extensions.service import ExtensionService

    return await ExtensionService(ctx.session, ctx.tenant_id).monitoring()


async def extensions_warranty_expiry_job(ctx: JobContext) -> dict[str, Any]:
    from app.modules.extensions.service import ExtensionService

    return await ExtensionService(ctx.session, ctx.tenant_id).monitoring()


async def extensions_upc_assign_job(ctx: JobContext) -> dict[str, Any]:
    data = ctx.get("data") or ctx.get("upc")
    if not isinstance(data, Mapping) or not data.get("item_id"):
        return _skip("UPC assignment is document-scoped; pass data.item_id in the job payload")
    from app.modules.extensions.service import ExtensionService

    return await ExtensionService(ctx.session, ctx.tenant_id).assign_upc(
        data, actor_id=ctx.actor_id, idempotency_key=ctx.get("idempotency_key")
    )


async def extensions_customer_part_import_job(ctx: JobContext) -> dict[str, Any]:
    rows = ctx.get("rows")
    if not rows:
        return _skip("customer-part imports are batch documents; pass rows in the job payload")
    from app.modules.extensions.service import ExtensionService

    return await ExtensionService(ctx.session, ctx.tenant_id).import_customer_parts(rows, actor_id=ctx.actor_id)


async def extensions_registration_import_job(ctx: JobContext) -> dict[str, Any]:
    rows = ctx.get("rows")
    if not rows:
        return _skip("registration imports are batch documents; pass rows in the job payload")
    from app.modules.extensions.service import ExtensionService

    return await ExtensionService(ctx.session, ctx.tenant_id).import_registrations(rows, actor_id=ctx.actor_id)


# =========================================================================== FS-15 reporting ======
async def reporting_refresh_views_job(ctx: JobContext) -> dict[str, Any]:
    from app.modules.reporting.refresh import refresh_materialized_views

    refreshed = await refresh_materialized_views(ctx.session, concurrent=bool(ctx.get("concurrent", True)))
    return {"refreshed": len(refreshed), "views": refreshed}


async def reporting_reconcile_readmodels_job(ctx: JobContext) -> dict[str, Any]:
    from app.modules.reporting.service import ReportService

    service = ReportService(ctx.session, tenant_id=ctx.tenant_id)
    catalogue = await service.catalogue()
    views = None
    if isinstance(catalogue, Mapping):
        views = catalogue.get("views") or catalogue.get("items")
    views = views or []
    stale: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for view in views:
        name = view.get("name") or view.get("view_name") if isinstance(view, Mapping) else view
        if not name:
            continue
        try:
            freshness = await service.get_freshness(str(name))
        except Exception as exc:  # noqa: BLE001 - per-view diagnostics; sweep continues
            errors.append({"view": str(name), "error": str(exc)})
            continue
        degraded = bool(isinstance(freshness, Mapping) and freshness.get("degraded"))
        if degraded:
            stale.append({"view": str(name), **{key: _jsonable(value) for key, value in freshness.items()}})
    return {"views_checked": len(views), "degraded": len(stale), "degraded_views": stale, "errors": errors[:20]}


# =========================================================================== no-op documentation ==
def _noop(name: str, feature_set: str, reason: str, description: str) -> JobSpec:
    return JobSpec(
        name=name, handler=None, kind=JobKind.NOOP, description=description, feature_set=feature_set, noop_reason=reason
    )


def _sweep(name: str, feature_set: str, handler: Any, description: str) -> JobSpec:
    return JobSpec(name=name, handler=handler, kind=JobKind.SWEEP, description=description, feature_set=feature_set)


def _command(name: str, feature_set: str, handler: Any, payload_keys: tuple[str, ...], description: str) -> JobSpec:
    return JobSpec(
        name=name,
        handler=handler,
        kind=JobKind.COMMAND,
        description=description,
        feature_set=feature_set,
        payload_keys=payload_keys,
    )


JOB_SPECS: dict[str, JobSpec] = {
    spec.name: spec
    for spec in (
        # FS-00 platform core
        _noop(
            "platform.jobs.scheduler",
            "FS-00",
            "external scheduler/RQ cron drives due jobs; the worker executes resolved names",
            "Scheduler tick (leader lock handled by the external scheduler)",
        ),
        _noop(
            "platform.outbox.relay",
            "FS-00",
            "dedicated relay loop owns polling (python -m app.core.outbox_relay)",
            "Outbox relay poll",
        ),
        _noop(
            "platform.retention.purge",
            "FS-00",
            "purge implementation deferred; retention_ledger_years is validated by platform admin",
            "Retention purge",
        ),
        _noop(
            "platform.sso.health.monitor",
            "FS-00",
            "SSO connection health is checked on demand by the auth context; no scheduled probe",
            "SSO health monitor",
        ),
        # FS-01 item master
        _sweep(
            "item.reference.validate",
            "FS-01",
            item_reference_validate_job,
            "Ensure item master reference data (uoms/types) exists",
        ),
        _sweep(
            "item.inventory.defaults.repair",
            "FS-01",
            item_inventory_defaults_repair_job,
            "Detect items missing item_inventory defaults (repair via API)",
        ),
        # FS-02 pricing
        _command(
            "pricing.apply_effective_updates",
            "FS-02",
            pricing_apply_effective_updates_job,
            ("update_id",),
            "Apply a price update on its effective date",
        ),
        _noop(
            "pricing.coupon.expire",
            "FS-02",
            "coupon expiry is enforced at redemption/validation time; no scheduled expiry sweep yet",
            "Expire coupons",
        ),
        _noop(
            "pricing.level_swap",
            "FS-02",
            "price-level swaps are document-driven via the pricing API",
            "Price level swap",
        ),
        _noop(
            "pricing.mass_price_update",
            "FS-02",
            "mass price updates run as price-update documents through the pricing API",
            "Mass price update",
        ),
        _noop(
            "pricing.purchase_price_update",
            "FS-02",
            "purchase-price updates run as price-update documents through the pricing API",
            "Purchase price update",
        ),
        _sweep(
            "pricing.recompute_dynamic_collections",
            "FS-02",
            pricing_recompute_dynamic_collections_job,
            "Recompute dynamic pricing collection membership",
        ),
        # FS-03 traceability registry
        _sweep(
            "traceability.bootstrap.sentinel",
            "FS-03",
            traceability_bootstrap_sentinel_job,
            "Ensure the tenant sentinel location/status bootstrap (idempotent)",
        ),
        _sweep(
            "traceability.deferred_fk.close",
            "FS-03",
            traceability_deferred_fk_close_job,
            "Verify FS-01 deferred references and emit the checked event",
        ),
        # FS-04 inventory ledger
        _noop(
            "ledger.commit_orders.schedule",
            "FS-04",
            "commitment scheduling is created by order/document flows, not a cron sweep",
            "Schedule commit orders",
        ),
        _noop(
            "ledger.import.process",
            "FS-04",
            "ledger imports are processed per import batch via the ledger API",
            "Process ledger import batch",
        ),
        _noop(
            "ledger.partition.maintenance",
            "FS-04",
            "ledger partitions are provisioned by migrations; no runtime rotation handler yet",
            "Ledger partition maintenance",
        ),
        _sweep(
            "ledger.reconcile_balances",
            "FS-04",
            ledger_reconcile_balances_job,
            "Read-only balance reconciliation (detail vs balance, negative/availability checks)",
        ),
        # FS-05 costing
        _noop(
            "costing.cogs.true_up",
            "FS-05",
            "COGS true-up is posting/document-driven through the costing API",
            "COGS true-up",
        ),
        _noop(
            "costing.landedcost.allocate",
            "FS-05",
            "landed-cost allocation runs per landed-cost document through the costing API",
            "Landed-cost allocation",
        ),
        _sweep(
            "costing.recalculate_item_cost",
            "FS-05",
            costing_recalculate_item_cost_job,
            "Report pending item-cost recalculations (payload item_id requests one)",
        ),
        _sweep("costing.reconcile", "FS-05", costing_reconcile_job, "Costing reconciliation report"),
        _noop(
            "costing.rollup.run",
            "FS-05",
            "cost rollups are created by the costing API with an explicit rollup payload",
            "Cost rollup run",
        ),
        _noop(
            "costing.standards.revalue",
            "FS-05",
            "standard-cost revaluation runs per revaluation document through the costing API",
            "Standard-cost revalue",
        ),
        # FS-06 numbered inventory
        _sweep(
            "traceability.deferred_fks.validate",
            "FS-06",
            traceability_deferred_fks_validate_job,
            "Validate deferred lot/serial FK closure and orphan counts",
        ),
        _sweep(
            "traceability.expiry.scan",
            "FS-06",
            traceability_expiry_scan_job,
            "Scan lot expiries within configured horizons and emit warnings",
        ),
        # FS-07 counts
        _sweep(
            "counts.generate_cycle_counts",
            "FS-07",
            counts_generate_cycle_counts_job,
            "Generate due cycle-count documents from active plans",
        ),
        _command(
            "counts.snapshot.refresh",
            "FS-07",
            counts_snapshot_refresh_job,
            ("count_id",),
            "Refresh the frozen snapshot of one count",
        ),
        _sweep(
            "counts.stale.cleanup",
            "FS-07",
            counts_stale_cleanup_job,
            "Clean up stale count documents (dry_run supported)",
        ),
        # FS-08 procurement
        _command(
            "procurement.match.run", "FS-08", procurement_match_run_job, ("bill_id",), "Run three-way match for a bill"
        ),
        _noop(
            "procurement.partition.maintenance",
            "FS-08",
            "procurement partitions are provisioned by migrations; no runtime rotation handler yet",
            "Procurement partition maintenance",
        ),
        _command(
            "procurement.po.close_overdue",
            "FS-08",
            procurement_po_close_overdue_job,
            ("po_id",),
            "Close an overdue purchase order",
        ),
        _sweep(
            "procurement.receipt.reconcile",
            "FS-08",
            procurement_receipt_reconcile_job,
            "Received-not-billed reconciliation report",
        ),
        # FS-10 planning
        _sweep("atp.rebuild", "FS-10", atp_rebuild_job, "Rebuild ATP projections"),
        _command(
            "planning.allocation.run",
            "FS-10",
            planning_allocation_run_job,
            ("run_id",),
            "Execute a queued allocation run",
        ),
        _sweep("planning.demand.calculate", "FS-10", planning_demand_calculate_job, "Run demand calculation"),
        _sweep("planning.supply.run", "FS-10", planning_supply_run_job, "Run supply planning"),
        _sweep("replenishment.aim", "FS-10", replenishment_aim_job, "Run AIM replenishment calculation"),
        _sweep(
            "replenishment.ordering", "FS-10", replenishment_ordering_job, "Order replenishment suggestions per rules"
        ),
        # FS-12 quality
        _sweep(
            "quality.coa.auto_issue",
            "FS-12",
            quality_coa_auto_issue_job,
            "Auto-issue COAs for PASSED inspections when quality.auto_coa is on",
        ),
        _noop(
            "quality.derived_field.recalc",
            "FS-12",
            "derived fields are computed on inspection completion; batch recalc deferred",
            "Recompute derived fields",
        ),
        _sweep(
            "quality.hold.reconcile", "FS-12", quality_hold_reconcile_job, "Read-only hold/T-30 drift reconciliation"
        ),
        _noop(
            "quality.queue.expire",
            "FS-12",
            "queue expiry needs a tenant policy surface that is not implemented yet",
            "Expire stale inspection queue items",
        ),
        _sweep(
            "quality.trigger.counter_reset",
            "FS-12",
            quality_trigger_counter_reset_job,
            "Reset trigger evaluation counters (optionally one trigger)",
        ),
        _noop(
            "quality.trigger.evaluate",
            "FS-12",
            "triggers are evaluated by the event-driven consumer; no replay buffer/backstop scan yet",
            "Evaluate deferred inspection triggers",
        ),
        # FS-13 consigned inventory
        _sweep(
            "consigned.agreement.expiry",
            "FS-13",
            consigned_agreement_expiry_job,
            "Report expired active consignment agreements (read-only)",
        ),
        _sweep(
            "consigned.consumption.reconcile",
            "FS-13",
            consigned_consumption_reconcile_job,
            "Consignment consumption reconciliation report",
        ),
        _sweep(
            "consigned.ownership.reconcile",
            "FS-13",
            consigned_ownership_reconcile_job,
            "Consigned ownership balances report",
        ),
        _sweep("consigned.stock.guard", "FS-13", consigned_stock_guard_job, "Validate consigned stock guards"),
        # FS-14 extensions
        _sweep("extensions.claims.reconcile", "FS-14", extensions_claims_reconcile_job, "Claims reconciliation report"),
        _command(
            "extensions.customer_part.import.process",
            "FS-14",
            extensions_customer_part_import_job,
            ("rows",),
            "Process a customer-part import batch",
        ),
        _command(
            "extensions.registration.import.process",
            "FS-14",
            extensions_registration_import_job,
            ("rows",),
            "Process a warranty-registration import batch",
        ),
        _command("extensions.upc.assign", "FS-14", extensions_upc_assign_job, ("data",), "Assign a UPC to an item"),
        _sweep("extensions.upc.reconcile", "FS-14", extensions_upc_reconcile_job, "UPC assignment monitoring report"),
        _sweep(
            "extensions.warranty.expiry", "FS-14", extensions_warranty_expiry_job, "Warranty expiry monitoring report"
        ),
        # FS-15 reporting
        _sweep(
            "reporting.reconcile_readmodels",
            "FS-15",
            reporting_reconcile_readmodels_job,
            "Read-model freshness reconciliation report",
        ),
        _sweep("reporting.refresh_views", "FS-15", reporting_refresh_views_job, "Refresh FS-15 materialized views"),
    )
}
