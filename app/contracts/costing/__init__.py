"""`costing.engine@1.0.0` contract package (FS-05).

Public surface:
* :class:`CostingPort` — synchronous in-process valuation facade called by FS-04 inside the posting
  transaction (no DB writes; persistence is FS-05-owned, see ``app.modules.costing.repository``).
* :mod:`app.contracts.costing.schemas` — canonical Pydantic/JSON-Schema payloads for value movements,
  results and event payloads.
* ``build_from_app.py`` — deterministic builder for ``openapi.fragment.json`` and event payload schemas
  consumed by ``python -m app.contracts.cli generate``.
"""

from app.contracts.costing.contract import CONTRACT_VERSION, CostingPort

__all__ = ["CONTRACT_VERSION", "CostingPort"]
