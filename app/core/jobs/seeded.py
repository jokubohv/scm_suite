"""Authoritative names of ``platform.job_definition`` rows seeded by migrations 0003–0030.

The worker resolves handlers by name; tests cross-check this static set against the live
``platform.job_definition`` table so seed drift fails fast. FS-09 (manufacturing) and FS-11 (WMS)
currently seed no scheduled jobs; if they add some, extend this set and the registry together.
"""

from __future__ import annotations

SEEDED_JOB_NAMES: frozenset[str] = frozenset(
    {
        # FS-00 platform core
        "platform.jobs.scheduler",
        "platform.outbox.relay",
        "platform.retention.purge",
        "platform.sso.health.monitor",
        # FS-01 item master
        "item.inventory.defaults.repair",
        "item.reference.validate",
        # FS-02 pricing
        "pricing.apply_effective_updates",
        "pricing.coupon.expire",
        "pricing.level_swap",
        "pricing.mass_price_update",
        "pricing.purchase_price_update",
        "pricing.recompute_dynamic_collections",
        # FS-03 traceability registry
        "traceability.bootstrap.sentinel",
        "traceability.deferred_fk.close",
        # FS-04 inventory ledger
        "ledger.commit_orders.schedule",
        "ledger.import.process",
        "ledger.partition.maintenance",
        "ledger.reconcile_balances",
        # FS-05 costing
        "costing.cogs.true_up",
        "costing.landedcost.allocate",
        "costing.recalculate_item_cost",
        "costing.reconcile",
        "costing.rollup.run",
        "costing.standards.revalue",
        # FS-06 numbered inventory
        "traceability.deferred_fks.validate",
        "traceability.expiry.scan",
        # FS-07 counts
        "counts.generate_cycle_counts",
        "counts.snapshot.refresh",
        "counts.stale.cleanup",
        # FS-08 procurement
        "procurement.match.run",
        "procurement.partition.maintenance",
        "procurement.po.close_overdue",
        "procurement.receipt.reconcile",
        # FS-10 planning / replenishment
        "atp.rebuild",
        "planning.allocation.run",
        "planning.demand.calculate",
        "planning.supply.run",
        "replenishment.aim",
        "replenishment.ordering",
        # FS-12 quality management
        "quality.coa.auto_issue",
        "quality.derived_field.recalc",
        "quality.hold.reconcile",
        "quality.queue.expire",
        "quality.trigger.counter_reset",
        "quality.trigger.evaluate",
        # FS-13 consigned inventory
        "consigned.agreement.expiry",
        "consigned.consumption.reconcile",
        "consigned.ownership.reconcile",
        "consigned.stock.guard",
        # FS-14 extensions
        "extensions.claims.reconcile",
        "extensions.customer_part.import.process",
        "extensions.registration.import.process",
        "extensions.upc.assign",
        "extensions.upc.reconcile",
        "extensions.warranty.expiry",
        # FS-15 reporting
        "reporting.reconcile_readmodels",
        "reporting.refresh_views",
    }
)

__all__ = ["SEEDED_JOB_NAMES"]
