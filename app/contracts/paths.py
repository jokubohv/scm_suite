"""Filesystem locations and naming conventions for contract artefacts."""

from __future__ import annotations

import re
from pathlib import Path

# app/contracts/paths.py -> app/contracts -> app -> inventory-platform
REPO_ROOT: Path = Path(__file__).resolve().parents[2]
APP_DIR: Path = REPO_ROOT / "app"
CONTRACTS_DIR: Path = REPO_ROOT / "contracts"
OPENAPI_DIR: Path = CONTRACTS_DIR / "openapi"
FRAGMENTS_DIR: Path = OPENAPI_DIR / "fragments"
EVENTS_DIR: Path = CONTRACTS_DIR / "events"
DESIGN_DIR: Path = REPO_ROOT / "design"
FEATURE_SETS_DIR: Path = REPO_ROOT / "feature-sets"
MANIFEST_PATH: Path = DESIGN_DIR / "feature-sets.json"
APP_CONTRACTS_DIR: Path = APP_DIR / "contracts"

#: Directory slug (under app/contracts) for every provided contract.
#: Mirrors the ``modules`` entries in design/feature-sets.json so FS builders own a
#: disjoint directory; FS-00 owns one directory for several platform/order contracts.
CONTRACT_DIR: dict[str, str] = {
    "platform.core": "platform",
    "platform.events": "platform",
    "platform.finance": "platform",
    "order.demand": "order_boundary",
    "order.supply": "order_boundary",
    "item.master": "item_master",
    "pricing.engine": "pricing",
    "traceability.registry": "traceability",
    "inventory.ledger": "inventory_ledger",
    "inventory.balances": "inventory_balances",
    "costing.engine": "costing",
    "traceability.numbered": "traceability_numbered",
    "counts.engine": "counts",
    "procurement.p2p": "procurement",
    "manufacturing.execution": "manufacturing",
    "replenishment.planning": "replenishment",
    "wms.execution": "wms",
    "quality.management": "quality",
    "consigned.inventory": "consigned",
    "extensions.registry": "extensions",
    "reporting.readmodels": "reporting",
}

#: Contracts that live in the same builder directory (FS-00 / FS-04 groups).
CONTRACTS_PER_DIR: dict[str, list[str]] = {}
for _contract, _dir in CONTRACT_DIR.items():
    CONTRACTS_PER_DIR.setdefault(_dir, []).append(_contract)

HTTP_METHODS: tuple[str, ...] = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS")
ALLOWED_METHODS: frozenset[str] = frozenset(HTTP_METHODS)

_VERSION_RE = re.compile(r"^v?(\d+)(?:\.(\d+))?(?:\.(\d+))?$")
_SLUG_RE = re.compile(r"[^a-z0-9]+")


def normalize_version(raw: str) -> str:
    """Normalise spec version spellings (``v1``, ``1.0``, ``1.0.0``) to semver ``X.Y.Z``."""
    value = (raw or "").strip().strip("`").strip()
    match = _VERSION_RE.match(value)
    if not match:
        return "0.0.0"
    major, minor, patch = match.groups()
    return f"{int(major)}.{int(minor or 0)}.{int(patch or 0)}"


def is_semver(value: str) -> bool:
    return bool(re.match(r"^\d+\.\d+\.\d+$", (value or "").strip()))


def canonical_path(path: str) -> str:
    """Canonicalise an OpenAPI path to the ``/api/v1`` base (the plan's C-02 decision).

    ``/v1/ressource`` -> ``/api/v1/ressource``; health/internal routes are unchanged.
    Query strings are stripped (declared separately in ``x-query-parameters``).
    """
    path = (path or "").strip().strip("`").strip()
    if "?" in path:
        path = path.split("?", 1)[0]
    if path.startswith("/v1/"):
        path = "/api/v1/" + path[len("/v1/"):]
    elif path == "/v1":
        path = "/api/v1"
    return path


def slugify(value: str) -> str:
    return _SLUG_RE.sub("-", (value or "").lower()).strip("-") or "root"


def relpath(path: Path) -> str:
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)
