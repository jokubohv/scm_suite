"""`procurement.p2p@1.0.0` contract metadata + in-process facade re-export (FS-08).

Dependants import the facade from here: FS-10 draft POs/open supply, FS-11 receipts bridge,
FS-12 receipt inspections, FS-13 consignment bills, FS-14 warranty vendor linkage, FS-15 purchase
history. The facade participates in the caller's transaction (see ``app.modules.procurement.facade``).
"""

from __future__ import annotations

from app.modules.procurement.facade import (
    EVENT_TYPES,
    JOB_NAMES,
    PROCUREMENT_CONTRACT_VERSION,
    ProcurementFacade,
)

CONTRACT_NAME = "procurement.p2p"

__all__ = ["CONTRACT_NAME", "EVENT_TYPES", "JOB_NAMES", "PROCUREMENT_CONTRACT_VERSION", "ProcurementFacade"]
