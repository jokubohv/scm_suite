"""Bounded context: traceability (FS-03 storage topology + inventory-status registry).

Public surface: :mod:`tables` (Core metadata), :mod:`repository`, :mod:`service` (registry use cases +
bootstrap/jobs), :mod:`resolver` (in-process ``traceability.registry@1.0.0`` contract), :mod:`errors`.
"""

from app.modules.traceability.errors import TraceabilityError
from app.modules.traceability.resolver import LocationResolver
from app.modules.traceability.service import TraceabilityService

__all__ = ["LocationResolver", "TraceabilityError", "TraceabilityService"]
