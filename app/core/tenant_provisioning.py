"""Tenant provisioning defaults registry (FS-00) — sequences/settings/flags + count approval.

Values mirror the per-tenant seeds declared by migrations ``0013``–``0030`` (and the FS-00 base seed
data).  Provisioning and the existing-tenant backfill both read this single registry so new tenants and
already-provisioned tenants converge on the same rows.  Generated once from the migration constants;
keep in sync when a migration adds a new seeded object (unit test asserts registry ↔ seed catalogue).

Configurable default: ``INVENTORY_COUNT_APPROVAL_DEFINITION`` is the enabled single-step definition
inserted by provisioning.  Override it per tenant by editing the definition row (``platform.
approval_definition``) or pass explicit ``name/steps`` to
:func:`provision_tenant_defaults`; provisioning never overwrites an existing definition.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.models import platform as p

SYSTEM_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000000000")

#: Enabled single-step default approval definition for required inventory counts (FS-07 bridge).
INVENTORY_COUNT_APPROVAL_DEFINITION: dict[str, Any] = {
    "name": "Default inventory count approval",
    "condition": {},
    "steps": [
        {
            "step": 1,
            "name": "Inventory count approval",
            "mode": "SEQUENTIAL",
            "roles": ["warehouse_operator"],
            "required_approvals": 1,
        }
    ],
    "is_enabled": True,
    "priority": 100,
}

TENANT_SETTING_DEFAULTS: tuple[tuple[str, str, object, str], ...] = (
    ('inventory', 'negative_inventory_policy', 'BLOCK', 'STRING'),
    ('inventory', 'rounding_mode', 'PER_MOVEMENT', 'STRING'),
    ('inventory', 'multi_location_enabled', True, 'BOOL'),
    ('inventory', 'uom_enabled', True, 'BOOL'),
    ('inventory', 'retention_ledger_years', 7, 'NUMBER'),
    ('rbac', 'sod_enforcement', 'HARD', 'STRING'),
    ('auth', 'local_login_enabled', True, 'BOOL'),
    ('platform', 'approval_definitions_required', True, 'BOOL'),
    ('inventory', 'numbered.enabled', False, 'boolean'),
    ('inventory', 'numbered.fefo_enabled', True, 'boolean'),
    ('inventory', 'numbered.require_expiry_for_fefo', True, 'boolean'),
    ('inventory', 'numbered.commit_allocates_serials', True, 'boolean'),
    ('inventory', 'numbered.serial_auto_numbering', False, 'boolean'),
    ('inventory', 'numbered.expiry_warning_days', [30, 60, 90], 'json'),
    ('inventory', 'numbered.max_serials_per_posting', 1000, 'number'),
    ('inventory', 'counts.post_on_approve', 'true', 'boolean'),
    ('inventory', 'counts.tolerance_qty', '0', 'number'),
    ('inventory', 'counts.tolerance_percent', '0', 'number'),
    ('inventory', 'counts.auto_approve_within_tolerance', 'false', 'boolean'),
    ('inventory', 'counts.default_interval_days', '30', 'number'),
    ('inventory', 'counts.abc_class_intervals', '{"A":7,"B":30,"C":90}', 'json'),
    ('inventory', 'counts.draft_ttl_days', '30', 'number'),
    ('inventory', 'counts.require_lot_serial_prerequisite', 'true', 'boolean'),
    ('inventory', 'procurement.advanced_receiving_mode', 'true', 'boolean'),
    ('inventory', 'procurement.allow_over_receipt', 'false', 'boolean'),
    ('inventory', 'procurement.over_receipt_tolerance_percent', '0', 'number'),
    ('inventory', 'procurement.bill_match_tolerance_amount', '0.01', 'number'),
    ('inventory', 'procurement.bill_match_tolerance_percent', '1.0', 'number'),
    ('inventory', 'procurement.match.require_3way', 'true', 'boolean'),
    ('inventory', 'procurement.vendor_bill_approval_required', 'false', 'boolean'),
    ('inventory', 'procurement.multiple_vendors_enabled', 'false', 'boolean'),
    ('inventory', 'procurement.purchase_contracts_enabled', 'false', 'boolean'),
    ('inventory', 'procurement.blanket_orders_enabled', 'false', 'boolean'),
    ('inventory', 'procurement.requisitions_enabled', 'false', 'boolean'),
    ('inventory', 'procurement.purchase_requests_enabled', 'false', 'boolean'),
    ('inventory', 'procurement.rfq_enabled', 'false', 'boolean'),
    ('inventory', 'procurement.drop_ship_enabled', 'false', 'boolean'),
    ('inventory', 'procurement.receipt_exchange_rate_source', 'PO', 'string'),
    ('inventory', 'procurement.estimated_landed_cost_default', 'false', 'boolean'),
    ('inventory', 'procurement.default_variance_account_ref', None, 'string'),
    ('inventory', 'procurement.credit_limit_enforcement', 'false', 'boolean'),
    ('manufacturing', 'wip.method_guard', 'ENFORCE', 'string'),
    ('manufacturing', 'wip.default_wip_account_ref', None, 'string'),
    ('manufacturing', 'unbuild.default_variance_account_ref', None, 'string'),
    ('manufacturing', 'component.commit_on_release', 'true', 'boolean'),
    ('manufacturing', 'component.use_yield', 'true', 'boolean'),
    ('manufacturing', 'component.round_up_qty', 'false', 'boolean'),
    ('manufacturing', 'over_issue_tolerance_pct', '0', 'number'),
    ('manufacturing', 'completion.validate_sequence', 'true', 'boolean'),
    ('manufacturing', 'operations.overlap_enabled', 'false', 'boolean'),
    ('manufacturing', 'scheduling.default_method', 'BACKWARD', 'string'),
    ('manufacturing', 'scheduling.policy_version', 'mfg-sched-v1', 'string'),
    ('manufacturing', 'scheduling.max_horizon_days', '365', 'number'),
    ('manufacturing', 'work_center.default_capacity_hours_per_day', '8', 'number'),
    ('manufacturing', 'actual_production_dates_mode', 'AUTO', 'string'),
    ('manufacturing', 'allow_purchase_of_assemblies', 'false', 'boolean'),
    ('manufacturing', 'backflush.explicit_enabled', 'false', 'boolean'),
    ('manufacturing', 'wo.create_from_sales_order', 'false', 'boolean'),
    ('manufacturing', 'plan.generate_planned_work_orders', 'false', 'boolean'),
    ('manufacturing', 'bom.max_depth', '20', 'number'),
    ('manufacturing', 'unbuild.require_original_build', 'false', 'boolean'),
    ('manufacturing', 'eco.implement_on_final_approval', 'false', 'boolean'),
    ('manufacturing', 'available_to_build.policy_version', 'atb-v1', 'string'),
    ('manufacturing', 'available_to_build.level_mode', 'ALL_LEVELS', 'string'),
    ('manufacturing', 'available_to_build.basis', 'ON_HAND', 'string'),
    ('manufacturing', 'available_to_build.include_subassemblies', 'true', 'boolean'),
    ('manufacturing', 'available_to_build.kit_expansion', 'true', 'boolean'),
    ('manufacturing', 'component.pick_interface.mode', 'EVENT_AND_PULL', 'string'),
    ('manufacturing', 'compat.enforcement_default', 'HARD', 'string'),
    ('manufacturing', 'compat.allow_override', 'true', 'boolean'),
    ('manufacturing', 'compat.rule_engine.max_rules_per_evaluation', '500', 'number'),
    ('manufacturing', 'compat.rule_set_cache_ttl_seconds', '60', 'number'),
    ('manufacturing', 'compat.validation.simulation_context', 'ATB', 'string'),
    ('manufacturing', 'unbuild.default_disposition', 'RETURN_TO_STOCK', 'string'),
    ('manufacturing', 'unbuild.value_policy', 'ORIGINAL_COST', 'string'),
    ('manufacturing', 'unbuild.value_policy_by_assembly', '{}', 'json'),
    ('manufacturing', 'unbuild.nrv_source', 'CURRENT_COST', 'string'),
    ('manufacturing', 'unbuild.default_used_condition_code', 'USED_A', 'string'),
    ('manufacturing', 'unbuild.default_used_status_code', 'USED', 'string'),
    ('manufacturing', 'unbuild.default_hold_status_code', 'HOLD', 'string'),
    ('manufacturing', 'unbuild.allow_resale_default', 'false', 'boolean'),
    ('manufacturing', 'unbuild.scrap_variance_account_ref', None, 'string'),
    ('inventory', 'replenishment.horizon_days', '90', 'number'),
    ('inventory', 'replenishment.horizon.max_days', '365', 'number'),
    ('inventory', 'replenishment.bucket_granularity', 'WEEK', 'string'),
    ('inventory', 'replenishment.time_fence_default_days', '0', 'number'),
    ('inventory', 'replenishment.demand_change_threshold_percent', '20', 'number'),
    ('inventory', 'replenishment.demand.method_default', 'MOVING_AVERAGE', 'string'),
    ('inventory', 'replenishment.demand.moving_average_window', '3', 'number'),
    ('inventory', 'replenishment.demand.season_length', '12', 'number'),
    ('inventory', 'replenishment.aim_formula_policy', '{"id":"aim-default-v1","version":1}', 'json'),
    ('inventory', 'replenishment.atp_method', '{"id":"atp-earliest-v1","version":1}', 'json'),
    ('inventory', 'replenishment.aim.analysis_interval_days', '30', 'number'),
    ('inventory', 'replenishment.aim.seasonal_interval_days', '365', 'number'),
    ('inventory', 'replenishment.aim.seasonal_mode', 'WEEKDAY', 'string'),
    ('inventory', 'replenishment.aim.order_interval_days', '7', 'number'),
    ('inventory', 'replenishment.aim.default_lead_time_days', '7', 'number'),
    ('inventory', 'replenishment.aim.change_epsilon', '0.000000001', 'string'),
    ('inventory', 'replenishment.supply.default_lot_sizing', 'LOT_FOR_LOT', 'string'),
    ('inventory', 'replenishment.allocation.reserve_commitments', 'false', 'boolean'),
    ('inventory', 'replenishment.atp.horizon_days', '90', 'number'),
    ('inventory', 'replenishment.atp.consistency_lag_seconds', '300', 'number'),
    ('inventory', 'replenishment.planning.readonly_guard', 'ENFORCE', 'string'),
    ('inventory', 'wms.max_lines_per_wave', '5000', 'number'),
    ('inventory', 'wms.pick_expired_guard_default', 'true', 'boolean'),
    ('inventory', 'wms.deferred_receipt_posting_default', 'false', 'boolean'),
    ('inventory', 'wms.assignment_timeout_hours', '8', 'number'),
    ('inventory', 'wms.abandon_hours', '24', 'number'),
    ('inventory', 'wms.ship_manifest_timeout_minutes', '60', 'number'),
    ('inventory', 'wms.device_session_retention_days', '30', 'number'),
    ('inventory', 'quality.hold_status_code', 'HOLD', 'text'),
    ('inventory', 'quality.default_queue_code', 'DEFAULT', 'text'),
    ('inventory', 'quality.auto_coa', False, 'boolean'),
    ('inventory', 'quality.coa_sampling_eligible', ['FULL', 'SAMPLE_RANDOM'], 'json'),
    ('inventory', 'quality.spec_line_limit', 35, 'number'),
    ('inventory', 'quality.derived_fields', [], 'json'),
    ('inventory', 'quality.notification_recipients', [], 'json'),
    ('inventory', 'quality.inspection_assignment', 'MANUAL', 'text'),
    ('inventory', 'quality.hold_requires_approval', True, 'boolean'),
    ('inventory', 'quality.hold_release_status_code', 'AVAILABLE', 'text'),
    ('inventory', 'consigned.assembly_incompatible_guard', 'true', 'boolean'),
    ('inventory', 'consigned.default_consigned_status_code', 'CONSIGNED_GOOD', 'string'),
    ('inventory', 'consigned.default_owned_status_code', 'AVAILABLE', 'string'),
    ('inventory', 'consigned.payment_trigger_default', 'ON_SALE', 'string'),
    ('inventory', 'consigned.consumption_requires_external_ref', 'true', 'boolean'),
    ('inventory', 'consigned.auto_bill_on_consumption', 'true', 'boolean'),
    ('inventory', 'consigned.reconcile_strict', 'true', 'boolean'),
    ('inventory', 'extensions.upc.auto_assign', 'false', 'boolean'),
    ('inventory', 'extensions.upc.company_prefix', '', 'string'),
    ('inventory', 'extensions.upc.auto_assign_batch_limit', '1000', 'number'),
    ('inventory', 'extensions.warranty.default_duration_months', '12', 'number'),
    ('inventory', 'extensions.warranty.expiry_scan_days', '1', 'number'),
    ('inventory', 'extensions.claims.allow_unregistered', 'false', 'boolean'),
    ('inventory', 'extensions.claims.override_requires_approval', 'true', 'boolean'),
    ('inventory', 'extensions.customer_part.import_limit', '5000', 'number'),
    ('inventory', 'extensions.warranty.registration_import_limit', '5000', 'number'),
)

TENANT_SEQUENCE_DEFAULTS: tuple[tuple[str, str, int], ...] = (
    ('AB', 'AB-', 6),
    ('AIM', 'AIM', 6),
    ('ALC', 'ALC', 6),
    ('ASN', 'ASN', 6),
    ('AU', 'AU-', 6),
    ('CAG', 'CAG', 6),
    ('CNT', 'CNT', 6),
    ('COA', 'COA-', 6),
    ('CONS', 'CONS', 6),
    ('CTR', 'CTR', 6),
    ('DP', 'DP', 6),
    ('ECN', 'ECN-', 6),
    ('IA', 'IA', 6),
    ('IMPORT', 'IMP', 6),
    ('INSP', 'INSP-', 6),
    ('IR', 'IR', 6),
    ('IT', 'IT', 6),
    ('IW', 'IW', 6),
    ('LC', 'LC', 6),
    ('MFGSCH', 'SCH-', 6),
    ('MFST', 'MFST', 6),
    ('PO', 'PO', 6),
    ('RCV', 'RCV', 6),
    ('REP', 'REP', 6),
    ('REV', 'REV', 6),
    ('RPL', 'RPL', 6),
    ('SHIP', 'SHIP', 6),
    ('SP', 'SP', 6),
    ('TASK', 'TASK', 6),
    ('TO', 'TO', 6),
    ('VB', 'VB', 6),
    ('VC', 'VC', 6),
    ('VRA', 'VRA', 6),
    ('WAVE', 'WAVE', 6),
    ('WC', 'WC', 6),
    ('WIP', 'WIP-', 6),
    ('WO', 'WO-', 6),
    ('WR', 'WR', 6),
)

TENANT_FLAG_DEFAULTS: tuple[str, ...] = (
    'consigned.assembly_incompatible_guard',
    'consigned.inventory.enabled',
    'costing.engine.enabled',
    'extensions.registry.enabled',
    'extensions.upc.auto_assign',
    'inventory.counts.enabled',
    'inventory.item_master.enabled',
    'inventory.ledger.enabled',
    'inventory.numbered.enabled',
    'traceability.registry.enabled',
    'manufacturing.enabled',
    'manufacturing.wip.enabled',
    'platform.core.enabled',
    'pricing.engine.enabled',
    'procurement.blanket_orders_enabled',
    'procurement.drop_ship_enabled',
    'procurement.multiple_vendors_enabled',
    'procurement.p2p.enabled',
    'procurement.purchase_contracts_enabled',
    'procurement.purchase_requests_enabled',
    'procurement.requisitions_enabled',
    'procurement.rfq_enabled',
    'quality.management.enabled',
    'replenishment.planning.enabled',
    'reporting.readmodels.enabled',
    'wms.execution.enabled',
)



async def _insert_sequence(session: AsyncSession, tenant_id: uuid.UUID, doc_type: str, prefix: str, padding: int) -> bool:
    """Insert one sequence unless a live (period_key IS NULL) row already exists (NULL-safe idempotency)."""
    existing = await session.execute(
        select(p.number_sequence.c.id).where(
            p.number_sequence.c.tenant_id == tenant_id,
            p.number_sequence.c.doc_type == doc_type,
            p.number_sequence.c.scope_key == "TENANT",
            p.number_sequence.c.period_key.is_(None),
        )
    )
    if existing.first() is not None:
        return False
    await session.execute(
        p.number_sequence.insert().values(
            id=uuid.uuid4(), tenant_id=tenant_id, doc_type=doc_type, scope_key="TENANT",
            prefix=prefix, padding=int(padding), next_value=1, reset_policy="NEVER",
            gap_policy="NON_GAPLESS", period_key=None,
        )
    )
    return True


async def _insert_setting(session: AsyncSession, tenant_id: uuid.UUID, namespace: str, key: str, value: Any, value_type: str) -> bool:
    existing = await session.execute(
        select(p.tenant_settings.c.id).where(
            p.tenant_settings.c.tenant_id == tenant_id,
            p.tenant_settings.c.namespace == namespace,
            p.tenant_settings.c.key == key,
        )
    )
    if existing.first() is not None:
        return False
    await session.execute(
        p.tenant_settings.insert().values(
            id=uuid.uuid4(), tenant_id=tenant_id, namespace=namespace, key=key,
            value=value, value_type=_normalise_value_type(value_type), is_default=True,
        )
    )
    return True


def _normalise_value_type(value_type: str) -> str:
    return str(value_type or "STRING").upper()


async def _insert_flag(session: AsyncSession, tenant_id: uuid.UUID, flag_key: str) -> bool:
    existing = await session.execute(
        select(p.feature_flag.c.id).where(
            p.feature_flag.c.tenant_id == tenant_id, p.feature_flag.c.flag_key == flag_key
        )
    )
    if existing.first() is not None:
        return False
    await session.execute(
        p.feature_flag.insert().values(
            id=uuid.uuid4(), tenant_id=tenant_id, flag_key=flag_key, is_enabled=False, source="DEFAULT"
        )
    )
    return True


async def _insert_count_approval(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    name: str | None = None,
    steps: Sequence[Mapping[str, Any]] | None = None,
    condition: Mapping[str, Any] | None = None,
    priority: int | None = None,
) -> bool:
    definition = INVENTORY_COUNT_APPROVAL_DEFINITION
    chosen_name = name or str(definition["name"])
    existing = await session.execute(
        select(p.approval_definition.c.id).where(
            p.approval_definition.c.tenant_id == tenant_id,
            p.approval_definition.c.document_type == "INVENTORY_COUNT",
            p.approval_definition.c.name == chosen_name,
        )
    )
    if existing.first() is not None:
        return False
    await session.execute(
        p.approval_definition.insert().values(
            id=uuid.uuid4(), tenant_id=tenant_id, document_type="INVENTORY_COUNT", name=chosen_name,
            condition=dict(condition if condition is not None else definition["condition"]),
            steps=list(steps if steps is not None else definition["steps"]),
            is_enabled=bool(definition["is_enabled"]),
            priority=int(priority if priority is not None else definition["priority"]),
        )
    )
    return True


async def provision_tenant_defaults(
    session: AsyncSession,
    tenant_id: uuid.UUID | str,
    *,
    include_sequences: bool = True,
    include_settings: bool = True,
    include_flags: bool = True,
    include_count_approval: bool = True,
    approval_name: str | None = None,
    approval_steps: Sequence[Mapping[str, Any]] | None = None,
    approval_condition: Mapping[str, Any] | None = None,
) -> dict[str, int]:
    """Idempotently clone the tenant-scoped defaults (sequences/settings/flags + count approval).

    Safe for new tenants (called by the platform-admin create flow) and for existing tenants (backfill
    CLI). Returns inserted counts; existing rows are never modified, so replay is a no-op.
    """
    tenant_uuid = tenant_id if isinstance(tenant_id, uuid.UUID) else uuid.UUID(str(tenant_id))
    if tenant_uuid == SYSTEM_TENANT_ID:
        return {"sequences": 0, "settings": 0, "flags": 0, "approval_definitions": 0}
    # Serialise concurrent provisioning attempts for the same tenant.
    await session.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"), {"key": f"tenant-provision:{tenant_uuid}"})
    counts = {"sequences": 0, "settings": 0, "flags": 0, "approval_definitions": 0}
    if include_sequences:
        for doc_type, prefix, padding in TENANT_SEQUENCE_DEFAULTS:
            counts["sequences"] += int(await _insert_sequence(session, tenant_uuid, doc_type, prefix, padding))
    if include_settings:
        for namespace, key, value, value_type in TENANT_SETTING_DEFAULTS:
            counts["settings"] += int(await _insert_setting(session, tenant_uuid, namespace, key, value, value_type))
    if include_flags:
        for flag_key in TENANT_FLAG_DEFAULTS:
            counts["flags"] += int(await _insert_flag(session, tenant_uuid, flag_key))
    if include_count_approval:
        counts["approval_definitions"] += int(
            await _insert_count_approval(
                session, tenant_uuid, name=approval_name, steps=approval_steps, condition=approval_condition
            )
        )
    return counts


async def backfill_tenant_defaults(session: AsyncSession, tenant_id: uuid.UUID | str) -> dict[str, int]:
    """Existing-tenant backfill path (used by ``python -m app.seed.backfill``)."""
    return await provision_tenant_defaults(session, tenant_id)


async def provision_all_tenants(session: AsyncSession) -> dict[str, dict[str, int]]:
    """Backfill every non-SYSTEM tenant; returns per-tenant inserted counts."""
    rows = await session.execute(select(p.tenant.c.id).where(p.tenant.c.id != SYSTEM_TENANT_ID).order_by(p.tenant.c.created_at))
    result: dict[str, dict[str, int]] = {}
    for (tenant_id,) in rows.all():
        result[str(tenant_id)] = await provision_tenant_defaults(session, tenant_id)
    return result


def expected_default_counts() -> dict[str, int]:
    """Canonical full-clone counts used by provisioning tests/reporting."""
    return {
        "sequences": len(TENANT_SEQUENCE_DEFAULTS),
        "settings": len(TENANT_SETTING_DEFAULTS),
        "flags": len(TENANT_FLAG_DEFAULTS),
        "approval_definitions": 1,
    }


__all__ = [
    "INVENTORY_COUNT_APPROVAL_DEFINITION",
    "SYSTEM_TENANT_ID",
    "TENANT_FLAG_DEFAULTS",
    "TENANT_SEQUENCE_DEFAULTS",
    "TENANT_SETTING_DEFAULTS",
    "backfill_tenant_defaults",
    "expected_default_counts",
    "provision_all_tenants",
    "provision_tenant_defaults",
]
