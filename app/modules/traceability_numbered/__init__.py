"""FS-06 numbered inventory: lot/serial identity, projection/history and FEFO hooks.

Serial current state and history are derived inside the FS-04 posting transaction by the triggers created
in migration ``0013_numbered_inventory``; this package owns identity registration, FEFO planning,
dry-run validation, reconciliation and the FS-06 API/facade surface.
"""

from app.modules.traceability_numbered.service import NumberedService

__all__ = ["NumberedService"]
