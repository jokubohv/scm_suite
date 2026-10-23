"""``python -m app.seed`` entry point (S0-06 ``scripts/seed.sh`` integration).

Delegates to :func:`app.seed.cli.main`. With no arguments the loader runs in system-only mode
(global reference data: SYSTEM tenant, currencies, transaction-type catalogue) unless
``SEED_TENANT_ID``/``INV_SEED_TENANT_ID`` is set; pass ``--tenant <uuid|system>`` for an explicit target.
"""

from __future__ import annotations

from app.seed.cli import main

if __name__ == "__main__":  # pragma: no cover - module entry point
    raise SystemExit(main())
