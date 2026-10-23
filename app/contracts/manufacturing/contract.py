"""`manufacturing.execution@1.1.0` contract metadata + in-process facade re-export (FS-09).

Dependants import the facade from here: FS-10 planning (open-WO demand/supply, draft WOs, BOM/calendar
reads, available-to-build), FS-11 WMS (component pick requirements, build/completion triggers), FS-12
quality (WO-triggered inspections), FS-13/FS-14 (assembly/unbuild configuration data) and FS-15
(manufacturing/ATB read models). The facade participates in the caller's transaction (never commits).

Contract `1.1.0` is additive over `1.0.0` (Δ-CFG compatibility engine + Δ-REUSE unbuild dispositions);
consumer ranges `>=1.0.0 <2.0.0` remain unchanged.
"""

from __future__ import annotations

from app.modules.manufacturing.facade import (
    CONTRACT_VERSION,
    EVENT_TYPES,
    JOB_NAMES,
    ManufacturingFacade,
)

CONTRACT_NAME = "manufacturing.execution"

__all__ = ["CONTRACT_NAME", "CONTRACT_VERSION", "EVENT_TYPES", "JOB_NAMES", "ManufacturingFacade"]
