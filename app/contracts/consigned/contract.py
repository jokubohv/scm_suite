"""`consigned.inventory@1.0.0` contract metadata (FS-13)."""

from __future__ import annotations

CONTRACT_NAME = "consigned.inventory"
CONTRACT_VERSION = "1.0.0"
EVENT_TYPES = ('consigned.agreement.created', 'consigned.agreement.activated', 'consigned.agreement.suspended', 'consigned.agreement.terminated', 'consigned.received', 'consigned.owned', 'consigned.consumed', 'consigned.adjusted', 'consigned.returned', 'consigned.reversed', 'consigned.guard.rejected')
JOB_NAMES: tuple[str, ...] = (
    "consigned.consumption.reconcile", "consigned.agreement.expiry",
    "consigned.ownership.reconcile", "consigned.stock.guard",
)

__all__ = ["CONTRACT_NAME", "CONTRACT_VERSION", "EVENT_TYPES", "JOB_NAMES"]
