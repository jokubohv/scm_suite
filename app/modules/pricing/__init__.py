"""Bounded context: pricing (FS-02 pricing engine, `pricing.engine@1.0.0`).

Public module surface:

* :mod:`app.modules.pricing.engine` — pure deterministic resolution/quantity/coupon/quote engine;
* :mod:`app.modules.pricing.repository` — async SQLAlchemy Core data access + price-update materialisation;
* :mod:`app.modules.pricing.service` — use-case service (validation/lifecycle/resolution);
* :mod:`app.modules.pricing.item_port` — read-only `item.master` port (default SQL adapter);
* :mod:`app.modules.pricing.tables` — Core metadata for the 17 ``pricing`` tables.
"""

from app.modules.pricing.engine import POLICY_VERSION, PriceRequest, PricingSnapshot, resolve_prices
from app.modules.pricing.service import PricingService, PricingServiceError

__all__ = [
    "POLICY_VERSION",
    "PriceRequest",
    "PricingService",
    "PricingServiceError",
    "PricingSnapshot",
    "resolve_prices",
]
