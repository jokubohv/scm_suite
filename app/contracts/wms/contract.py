"""`wms.execution@1.0.0` contract metadata + in-process facade re-export (FS-11).

Dependants import the facade from here: FS-12 inspection/ship samples, FS-13 consignment receiving,
FS-14 extension task boundaries, FS-15 reporting task/shipment status dimensions. The facade
participates in the caller's transaction (see ``app.modules.wms.facade``).
"""

from __future__ import annotations

from app.modules.wms.facade import (
    EVENT_TYPES,
    JOB_NAMES,
    WMS_CONTRACT_VERSION,
    WmsFacade,
)

CONTRACT_NAME = "wms.execution"

__all__ = ["CONTRACT_NAME", "EVENT_TYPES", "JOB_NAMES", "WMS_CONTRACT_VERSION", "WmsFacade"]
