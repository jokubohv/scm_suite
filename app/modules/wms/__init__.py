"""FS-11 WMS module (wave 1E; contract ``wms.execution@1.0.0``).

Server-side warehouse execution: warehouse/strategy configuration, inbound receiving/putaway, wave
release and pick/pack/ship tasks, WMS replenishment and directed status-change tasks, cart/device
session backend containers (EXT; no mobile protocol, D8). Inventory effects flow only through the
frozen FS-04 ``PostingPort``; receipts through FS-08 ``procurement.p2p``; work-order builds through
FS-09 when deployed; WMS cycle-count requests through FS-07 ``counts.engine``.
"""

from app.modules.wms.facade import (
    EVENT_TYPES,
    JOB_NAMES,
    WMS_CONTRACT_VERSION,
    WmsFacade,
)

__all__ = ["EVENT_TYPES", "JOB_NAMES", "WMS_CONTRACT_VERSION", "WmsFacade"]
