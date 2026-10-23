"""FS-09 Manufacturing module (wave 1C; contract ``manufacturing.execution@1.1.0``).

Assembly/BOM master data and execution: work orders, WIP T-17/T-18/T-19, build/unbuild T-15/T-16,
routing/work centres/calendars, outsourced manufacturing, ECOs, scheduling/capacity computation,
available-to-build and the Δ-CFG compatibility engine + Δ-REUSE unbuild dispositions.

Stock/value effects flow only through the frozen FS-04 ``PostingPort``/``BalanceReadPort`` and FS-05
``CostingPort``; cross-context drafts use the FS-08/FS-04 facades. No UI/mobile/analytics scope.
"""

from app.modules.manufacturing.facade import (
    CONTRACT_VERSION,
    EVENT_TYPES,
    JOB_NAMES,
    ManufacturingFacade,
)

__all__ = ["CONTRACT_VERSION", "EVENT_TYPES", "JOB_NAMES", "ManufacturingFacade"]
