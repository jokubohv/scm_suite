"""FS-03 `traceability.registry@1.0.0` contract package.

Re-exports the in-process resolver (``LocationResolver``) and the canonical models/schemas so dependants
(FS-04 posting engine, FS-05 cost objects, FS-10 planning, FS-11 WMS) import one stable surface.
"""

from __future__ import annotations

from app.contracts.traceability.schemas import (
    ADDITIVE_EVENT_MODELS,
    EVENT_MODELS,
    REQUIRED_EVENT_MODELS,
    RESOLVER_METHODS,
    Bin,
    BootstrapState,
    InventoryStatus,
    Location,
    PostingEligibility,
    StatusFlags,
    Zone,
    bootstrap_schema,
    event_schemas,
    location_schema,
)
from app.modules.traceability.resolver import LocationResolver

__all__ = [
    "ADDITIVE_EVENT_MODELS",
    "EVENT_MODELS",
    "REQUIRED_EVENT_MODELS",
    "RESOLVER_METHODS",
    "Bin",
    "BootstrapState",
    "InventoryStatus",
    "Location",
    "LocationResolver",
    "PostingEligibility",
    "StatusFlags",
    "Zone",
    "bootstrap_schema",
    "event_schemas",
    "location_schema",
]
