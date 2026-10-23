"""`extensions.registry@1.0.0` contract metadata (FS-14)."""

from __future__ import annotations

CONTRACT_NAME = "extensions.registry"
CONTRACT_VERSION = "1.0.0"
EVENT_TYPES = ('warranty.terms.updated', 'warranty.registered', 'warranty.registration.voided', 'warranty.registration.expired', 'warranty.claim.opened', 'warranty.claim.validated', 'warranty.claim.approved', 'warranty.claim.rejected', 'warranty.claim.action.recorded', 'warranty.claim.completed', 'warranty.claim.cancelled', 'warranty.repair.created', 'warranty.repair.updated', 'warranty.repair.completed', 'warranty.repair.cancelled', 'extensions.customer_part.created', 'extensions.customer_part.updated', 'extensions.customer_part.deactivated', 'extensions.upc.assigned', 'extensions.upc.retired', 'extensions.upc.batch.completed')
JOB_NAMES: tuple[str, ...] = (
    "extensions.warranty.expiry", "extensions.claims.reconcile", "extensions.upc.reconcile",
    "extensions.upc.assign", "extensions.registration.import.process", "extensions.customer_part.import.process",
)

__all__ = ["CONTRACT_NAME", "CONTRACT_VERSION", "EVENT_TYPES", "JOB_NAMES"]
