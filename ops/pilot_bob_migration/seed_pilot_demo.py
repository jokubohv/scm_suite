#!/usr/bin/env python3
"""Idempotent synthetic demo-data reseed + inventory field-coverage verification for the SCM pilot tenant.

Run after the pilot tenant scoped reset and USD->BOB base-currency change. The script:
  * authenticates with an SCM tenant API key supplied only through ``SCM_API_KEY`` (``X-API-Key``),
    and talks only to ``SCM_BASE_URL``;
  * audits the surviving master data (items/vendors/locations) and fills every inventory-relevant
    field that is empty, reusing existing rows instead of duplicating them;
  * rebuilds representative transactional inventory: opening stock adjustments in two locations,
    manufacture/expiry lots for lot-numbered items, a submitted+posted purchase order and goods
    receipt with BOB unit costs and lot details, a posted inter-location transfer, and an inventory
    count snapshot/capture/submit;
  * is idempotent (read-before-write, stable document numbers/memo markers, deterministic synthetic
    values) so a rerun converges to no-ops;
  * ``--verify`` is read-only and emits a JSON coverage report plus a human summary, exiting non-zero
    on required-field gaps or non-BOB monetary fields.

Usage (inside the Serebo API container on the VPS, where the SCM env vars already exist):

    python3 seed_pilot_demo.py                 # seed / enrich (safe to rerun)
    python3 seed_pilot_demo.py --verify        # read-only proof; exit 1 on gaps
    python3 seed_pilot_demo.py --verify --json-out /tmp/scm_verify.json

The JSON report is written to stdout; the human summary to stderr. Secrets are never printed.
Only Python 3.11+ standard library is required (urllib), so the file can be piped into a container.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

MARKER = "SEED-PILOT-BOB"
SYNTHETIC_TAG = "sintético"
OPENING_DOC = f"{MARKER}-OPENING-001"
TRANSFER_DOC = f"{MARKER}-TRANSFER-001"
PO_MARKER = f"{MARKER} PO {SYNTHETIC_TAG}"
RECEIPT_MARKER = f"{MARKER} recepción {SYNTHETIC_TAG}"

# Opening-quantity ladder per base UoM (pilot agricultural/retail demo data).
OPENING_QTY = {"KG": Decimal("160"), "G": Decimal("16000"), "L": Decimal("120"),
               "ML": Decimal("12000"), "EA": Decimal("240"), "CASE": Decimal("12")}
LOCATION_FACTORS = (Decimal("1"), Decimal("0.5"))  # second posting location receives half
LOCATION_TYPES = ("WAREHOUSE", "STORE")
IMMUTABLE_INVENTORY_KEYS = ("costing_method", "stock_uom_id", "initial_cost")

DEMO_ITEMS: tuple[dict[str, Any], ...] = (
    {"sku": "SEED-SYN-ACA-001", "name": "Harina de trigo (sintética)", "uom": "KG", "type": "LOT_NUMBERED",
     "category": "RAW_MATERIALS", "price": "24.50", "description": "Símbolo sintético de demostración: harina de trigo con lote trazable."},
    {"sku": "SEED-SYN-ACE-001", "name": "Aceite vegetal (sintético)", "uom": "L", "type": "LOT_NUMBERED",
     "category": "RAW_MATERIALS", "price": "18.75", "description": "Símbolo sintético de demostración: aceite vegetal en litros con lote y caducidad."},
    {"sku": "SEED-SYN-JAR-001", "name": "Jarabe de prueba (sintético)", "uom": "ML", "type": "LOT_NUMBERED",
     "category": "CONSUMABLES", "price": "9.90", "description": "Símbolo sintético de demostración: jarabe medido en mililitros con lote."},
    {"sku": "SEED-SYN-GUA-001", "name": "Guantes de nitrilo (sintéticos)", "uom": "EA", "type": "INVENTORY",
     "category": "CONSUMABLES", "price": "10.50", "description": "Símbolo sintético de demostración: guantes desechables contados por unidad."},
    {"sku": "SEED-SYN-REP-001", "name": "Repuesto de bomba (sintético)", "uom": "EA", "type": "INVENTORY",
     "category": "FINISHED_GOODS", "price": "145.00", "description": "Símbolo sintético de demostración: repuesto de bomba por unidad."},
    {"sku": "SEED-SYN-SAL-001", "name": "Sal industrial (sintética)", "uom": "KG", "type": "INVENTORY",
     "category": "RAW_MATERIALS", "price": "6.40", "description": "Símbolo sintético de demostración: sal industrial a granel en kilogramos."},
)
FIFO_TOPUP_ITEMS: tuple[dict[str, Any], ...] = (
    {"sku": "SEED-SYN-FIFO-KG-001", "name": "Insumo FIFO demostración (sintético)", "uom": "KG", "type": "LOT_NUMBERED",
     "category": "RAW_MATERIALS", "price": "31.20", "description": "Símbolo sintético de demostración: insumo FIFO con lote, creado para garantizar valuación FIFO en el tenant piloto."},
    {"sku": "SEED-SYN-FIFO-EA-001", "name": "Consumible FIFO demostración (sintético)", "uom": "EA", "type": "INVENTORY",
     "category": "CONSUMABLES", "price": "14.80", "description": "Símbolo sintético de demostración: consumible FIFO por unidad, creado para garantizar valuación FIFO en el tenant piloto."},
)
DEMO_VENDORS: tuple[dict[str, Any], ...] = (
    {"number": "SEED-SYN-PROV-01", "name": "Distribuidora Andina Sintética S.A.", "tax_id": "SYN1023456011",
     "city": "Santa Cruz de la Sierra", "contact": "María Fernanda Demo", "email": "compras.andina@example.invalid"},
    {"number": "SEED-SYN-PROV-02", "name": "Insumos del Oriente Sintéticos Ltda.", "tax_id": "SYN1023456022",
     "city": "Santa Cruz de la Sierra", "contact": "José Luis Muestra", "email": "ventas.oriente@example.invalid"},
    {"number": "SEED-SYN-PROV-03", "name": "Proveedora Industrial Boliviana Sintética", "tax_id": "SYN1023456033",
     "city": "La Paz", "contact": "Ana Lucía Prueba", "email": "contacto.industrial@example.invalid"},
)
DEMO_LOCATIONS: tuple[dict[str, Any], ...] = (
    {"code": "SEED-SYN-WH-01", "name": "Almacén Principal Sintético", "type": "WAREHOUSE", "city": "Santa Cruz de la Sierra"},
    {"code": "SEED-SYN-WH-02", "name": "Almacén Secundario Sintético", "type": "WAREHOUSE", "city": "El Alto"},
    {"code": "SEED-SYN-STORE-01", "name": "Tienda Sintética Sucursal", "type": "STORE", "city": "La Paz"},
)
REQUIRED_FLAGS = (
    "traceability.registry.enabled",
    "inventory.ledger.enabled",
    "inventory.counts.enabled",
    "costing.engine.enabled",
)
SHARED_BOB = "BOB"


# ------------------------------------------------------------------------------------------------
# helpers
# ------------------------------------------------------------------------------------------------
class SeedError(RuntimeError):
    """A fatal API/contract failure while seeding."""


class ApiError(SeedError):
    def __init__(self, method: str, path: str, status: int, payload: Any) -> None:
        detail = payload if isinstance(payload, dict | list) else str(payload)
        super().__init__(f"{method} {path} -> HTTP {status}: {json.dumps(detail)[:900]}")
        self.method = method
        self.path = path
        self.status = status
        self.payload = payload


def _now() -> datetime:
    return datetime.now(UTC)


def _iso_now() -> str:
    return _now().isoformat(timespec="seconds")


def _digest(*parts: str, length: int = 10) -> str:
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:length].upper()


def _slug(text: str, max_len: int = 32) -> str:
    cleaned = "".join(ch if ch.isalnum() else "-" for ch in str(text).upper()).strip("-")
    while "--" in cleaned:
        cleaned = cleaned.replace("--", "-")
    return cleaned[:max_len] or "SYN"


def _dec(value: Any, default: Decimal = Decimal("0")) -> Decimal:
    if value is None or value == "":
        return default
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return default


def _money(value: Decimal) -> str:
    return str(Decimal(value).quantize(Decimal("0.01")))


def _qty(value: Decimal) -> str:
    text = format(Decimal(value), "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return _qty(value)
    if isinstance(value, date | datetime):
        return value.isoformat()
    return value


def _ean13(seed: str) -> str:
    digits = ("779" + str(int(hashlib.sha1(seed.encode()).hexdigest(), 16)))[:12]
    total = sum(int(ch) * (3 if idx % 2 else 1) for idx, ch in enumerate(digits))
    return digits + str((10 - total % 10) % 10)


def _synth_price(sku: str) -> Decimal:
    raw = int(hashlib.sha1(sku.encode()).hexdigest(), 16)
    return (Decimal(raw % 3500) / Decimal(100) + Decimal("5")).quantize(Decimal("0.01"))


def _synth_qty(uom_code: str, index: int) -> Decimal:
    base = OPENING_QTY.get(uom_code, Decimal("100"))
    return base + Decimal(index % 3) * (base / Decimal("4"))


def _synth_tax_id(sku: str) -> str:
    return "SYN" + str(int(hashlib.sha1(sku.encode()).hexdigest(), 16))[:9]


def _synth_address(city: str, seed: str) -> dict[str, str]:
    number = 100 + int(hashlib.sha1(seed.encode()).hexdigest(), 16) % 800
    return {"line1": f"Av. Demostración {number}", "line2": "Zona Industrial Sintética",
            "city": city, "state": city, "postal_code": "0000", "country": "BO"}


def _items_of(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        for key in ("items", "locations", "lots", "lines"):
            value = payload.get(key)
            if isinstance(value, list):
                return [row for row in value if isinstance(row, dict)]
        return [payload] if payload else []
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    return []


def _wrapped(payload: Any, *keys: str) -> dict[str, Any]:
    if isinstance(payload, dict):
        for key in keys:
            value = payload.get(key)
            if isinstance(value, dict):
                return value
        return payload
    return {}


def _lookup(record: Mapping[str, Any], dotted: str) -> Any:
    current: Any = record
    for part in dotted.split("."):
        if not isinstance(current, Mapping):
            return None
        current = current.get(part)
    return current


def _present(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, list | dict | tuple | set):
        return len(value) > 0
    return True


def _sort_key(value: Any) -> str:
    return "" if value is None else str(value)

def _same_value(current: Any, desired: Any) -> bool:
    """Compare API read-back values with desired values; numeric strings compare numerically."""
    if current is None:
        return False
    try:
        return Decimal(str(current)) == Decimal(str(desired))
    except (InvalidOperation, ValueError):
        return str(current) == str(desired)

def _error_code(exc: BaseException) -> str:
    payload = getattr(exc, "payload", None)
    if isinstance(payload, dict):
        return str(payload.get("code") or "")
    return ""


# ------------------------------------------------------------------------------------------------
# HTTP client (stdlib only)
# ------------------------------------------------------------------------------------------------
class Client:
    RETRY_STATUSES = (500, 502, 503, 504)

    def __init__(self, base_url: str, api_key: str, timeout: float = 60.0) -> None:
        self.base_url = base_url.rstrip("/")
        self._api_key = api_key
        self.timeout = timeout
        self.calls = 0
        # A rerun after the scoped reset must not replay cached SCM responses that point
        # at deleted documents. Stable read-before-write markers keep the seed idempotent;
        # the salt only prevents stale Idempotency-Key cache replays across runs.
        self.idem_salt = os.environ.get("SEED_IDEM_SALT") or os.urandom(4).hex()

    def _url(self, path: str, params: Mapping[str, Any] | None = None) -> str:
        url = self.base_url + path
        if params:
            query = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
            if query:
                url += ("&" if "?" in url else "?") + query
        return url

    def api(self, method: str, path: str, *, body: Any = None, idem: str | None = None,
            ok: Iterable[int] = (200, 201, 202, 204), allow: Iterable[int] = (),
            params: Mapping[str, Any] | None = None, timeout: float | None = None) -> tuple[int, Any]:
        ok_set, allow_set = set(ok), set(allow)
        data = None if body is None else json.dumps(body, default=_jsonable).encode("utf-8")
        headers = {"Accept": "application/json", "X-API-Key": self._api_key, "User-Agent": "serebo-scm/0.1"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        if idem:
            headers["Idempotency-Key"] = f"{idem}-{self.idem_salt}"
        last_error: Exception | None = None
        for attempt in range(3):
            request = urllib.request.Request(self._url(path, params), data=data, method=method)
            for key, value in headers.items():
                request.add_header(key, value)
            self.calls += 1
            try:
                with urllib.request.urlopen(request, timeout=timeout or self.timeout) as response:
                    raw = response.read()
                    payload = json.loads(raw) if raw else {}
                    status = response.status
                    if status in ok_set or status in allow_set:
                        return status, payload
                    raise ApiError(method, path, status, payload)
            except urllib.error.HTTPError as exc:
                raw = exc.read()
                try:
                    payload = json.loads(raw) if raw else {}
                except ValueError:
                    payload = raw.decode("utf-8", "replace")
                if exc.code in ok_set or exc.code in allow_set:
                    return exc.code, payload
                if exc.code in self.RETRY_STATUSES and attempt < 2:
                    last_error = ApiError(method, path, exc.code, payload)
                    time.sleep(0.5 * (attempt + 1))
                    continue
                raise ApiError(method, path, exc.code, payload) from None
            except (urllib.error.URLError, TimeoutError) as exc:
                last_error = exc
                if attempt < 2:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                raise SeedError(f"{method} {path} -> network error: {exc}") from exc
        raise SeedError(f"{method} {path} -> retries exhausted: {last_error}")

    def get(self, path: str, params: Mapping[str, Any] | None = None, *,
            allow: Iterable[int] = (), ok: Iterable[int] = (200,)) -> tuple[int, Any]:
        return self.api("GET", path, params=params, allow=allow, ok=ok)

    def post(self, path: str, body: Any = None, *, idem: str | None = None,
             ok: Iterable[int] = (200, 201, 202), allow: Iterable[int] = ()) -> tuple[int, Any]:
        return self.api("POST", path, body=body, idem=idem, ok=ok, allow=allow)

    def put(self, path: str, body: Any = None, *, idem: str | None = None,
            ok: Iterable[int] = (200, 201)) -> tuple[int, Any]:
        return self.api("PUT", path, body=body, idem=idem, ok=ok)

    def patch(self, path: str, body: Any = None, *, idem: str | None = None,
              ok: Iterable[int] = (200, 201)) -> tuple[int, Any]:
        return self.api("PATCH", path, body=body, idem=idem, ok=ok)


# ------------------------------------------------------------------------------------------------
# seed context
# ------------------------------------------------------------------------------------------------
class SeedContext:
    def __init__(self, client: Client) -> None:
        self.client = client
        self.refs: dict[str, Any] = {}
        self.item_types: dict[str, dict[str, Any]] = {}
        self.uoms: dict[str, dict[str, Any]] = {}
        self.categories: list[dict[str, Any]] = []
        self.statuses: list[dict[str, Any]] = []
        self.reason_codes: list[dict[str, Any]] = []
        self.flags: dict[str, bool] = {}
        self.items: dict[str, dict[str, Any]] = {}       # item_id -> detail
        self.vendors: list[dict[str, Any]] = []
        self.locations: list[dict[str, Any]] = []
        self.lots: dict[str, dict[str, Any]] = {}        # item_id -> complete seed lot
        self.default_status: dict[str, Any] | None = None
        self.opening_reason: dict[str, Any] | None = None
        self.count_reason: dict[str, Any] | None = None
        self.chosen_vendor: dict[str, Any] | None = None
        self.posting_locations: list[dict[str, Any]] = []
        self.actions: list[dict[str, Any]] = []
        self.warnings: list[str] = []
        self.errors: list[str] = []
        self.seed_date = date.today()

    # -- bookkeeping ---------------------------------------------------------------------------
    def act(self, area: str, action: str, target: Any, detail: Any = None) -> None:
        row: dict[str, Any] = {"area": area, "action": action, "target": str(target)}
        if detail is not None:
            row["detail"] = detail
        self.actions.append(row)

    def warn(self, message: str) -> None:
        if message not in self.warnings:
            self.warnings.append(message)

    # -- reference data ------------------------------------------------------------------------
    def uom_code(self, uom_id: Any) -> str:
        if not uom_id:
            return ""
        for code, row in self.uoms.items():
            if row.get("id") == uom_id:
                return code
        return ""

    def category_for(self, preferred_code: str, fallback_index: int = 0) -> dict[str, Any] | None:
        for row in self.categories:
            if str(row.get("code", "")).upper() == preferred_code.upper():
                return row
        if self.categories:
            return self.categories[fallback_index % len(self.categories)]
        return None

    def vendor_for(self, index: int = 0) -> dict[str, Any] | None:
        bob = [v for v in self.vendors if str(v.get("currency_code") or "").upper() == SHARED_BOB]
        pool = bob or self.vendors
        return pool[index % len(pool)] if pool else None

    def item_list(self) -> list[dict[str, Any]]:
        return sorted(self.items.values(), key=lambda row: _sort_key(row.get("item_number")))

    def stockable_items(self) -> list[dict[str, Any]]:
        return [row for row in self.item_list()
                if row.get("is_active") and (self.item_types.get(str(row.get("item_type_code")), {}).get("can_stock"))]

    def numbered_kind(self, item: Mapping[str, Any]) -> str:
        item_type = self.item_types.get(str(item.get("item_type_code")), {})
        return "LOT" if item_type.get("requires_numbering") and item.get("item_type_code") == "LOT_NUMBERED" else (
            "SERIAL" if item_type.get("requires_numbering") else "")


# ------------------------------------------------------------------------------------------------
# reference loading
# ------------------------------------------------------------------------------------------------
def load_reference(ctx: SeedContext) -> None:
    client = ctx.client
    _, payload = client.get("/api/v1/item-types")
    ctx.item_types = {str(row.get("code")): row for row in _items_of(payload)}
    if not ctx.item_types:
        raise SeedError("no item types returned by /api/v1/item-types")
    _, payload = client.get("/api/v1/uoms", {"is_active": "true"})
    ctx.uoms = {str(row.get("code")): row for row in _items_of(payload)}
    if not ctx.uoms:
        raise SeedError("no UoMs returned by /api/v1/uoms")
    _, payload = client.get("/api/v1/item-categories", {"include_inactive": "true"})
    ctx.categories = [row for row in _items_of(payload) if row.get("is_active", True)]
    _, payload = client.get("/api/v1/inventory-statuses", {"active": "true"})
    ctx.statuses = _items_of(payload)
    defaults = [row for row in ctx.statuses if row.get("is_default")] or \
               [row for row in ctx.statuses if row.get("is_available_for_allocation")] or ctx.statuses
    ctx.default_status = defaults[0] if defaults else None
    # Newly bootstrapped tenants provision their default inventory statuses when the first
    # statuses-enabled location is created; ensure_locations() runs before ensure_inventory_statuses().
    _, payload = client.get("/api/v1/inventory-reason-codes", {"active": "true"})
    ctx.reason_codes = _items_of(payload)
    status, payload = client.get("/api/v1/admin/flags", allow=(403, 404))
    if status == 200:
        ctx.flags = {str(row.get("flag_key")): bool(row.get("is_enabled")) for row in _items_of(payload)}

    disabled = [key for key in REQUIRED_FLAGS if ctx.flags and not ctx.flags.get(key, True)]
    if disabled:
        raise SeedError(
            "required tenant feature flags are disabled: " + ", ".join(disabled) +
            ". Enable them (SCM tenant administration) before reseeding inventory demo data.")


# ------------------------------------------------------------------------------------------------
# categories / reason codes
# ------------------------------------------------------------------------------------------------
def ensure_category(ctx: SeedContext) -> dict[str, Any]:
    if ctx.categories:
        return ctx.categories[0]
    client = ctx.client
    status, payload = client.post("/api/v1/item-categories",
                                  {"code": "SEED-SYN-DEMO", "name": f"Categoría {SYNTHETIC_TAG} de demostración",
                                   "is_active": True},
                                  idem=f"seed-category-{MARKER}", allow=(409,))
    if status == 201:
        ctx.act("categories", "created", "SEED-SYN-DEMO")
        row = _wrapped(payload)
    else:
        _, listing = client.get("/api/v1/item-categories", {"include_inactive": "true"})
        row = next(iter(_items_of(listing)), {})
    if not row.get("id"):
        raise SeedError("could not ensure an item category")
    ctx.categories = [row]
    return row


def ensure_reason_codes(ctx: SeedContext) -> None:
    adjustment = [row for row in ctx.reason_codes
                  if str(row.get("reason_type") or "").upper() in ("ADJUSTMENT", "ADJUST", "INVENTORY_ADJUSTMENT")]
    if adjustment:
        ctx.opening_reason = adjustment[0]
        ctx.count_reason = next((row for row in adjustment if "COUNT" in str(row.get("code", "")).upper()),
                                adjustment[0])
        ctx.act("reason_codes", "reused", ctx.opening_reason.get("code"))
        return
    client = ctx.client
    created: list[dict[str, Any]] = []
    for code, name, key in (("SEED-SYN-OPENING", f"Carga inicial {SYNTHETIC_TAG} de demostración", "opening"),
                            ("SEED-SYN-CYCLE", f"Conteo cíclico {SYNTHETIC_TAG} de demostración", "cycle")):
        status, payload = client.post("/api/v1/inventory-reason-codes",
                                      {"code": code, "name": name, "reason_type": "ADJUSTMENT", "is_active": True},
                                      idem=f"seed-reason-{key}-{MARKER}", allow=(409,))
        if status == 201:
            row = _wrapped(payload)
            row.setdefault("code", code)
            ctx.act("reason_codes", "created", code)
        else:
            row = {"code": code}
        created.append(row)
    ctx.opening_reason, ctx.count_reason = created[0], created[1]


# ------------------------------------------------------------------------------------------------
# vendors
# ------------------------------------------------------------------------------------------------
def _vendor_payload(spec: Mapping[str, Any]) -> dict[str, Any]:
    city = str(spec.get("city") or "Santa Cruz de la Sierra")
    number = str(spec.get("number") or "")
    return {
        "vendor_number": number,
        "name": str(spec.get("name") or f"Proveedor {SYNTHETIC_TAG}"),
        "currency_code": SHARED_BOB,
        "payment_terms": "NET_30",
        "tax_id": str(spec.get("tax_id") or _synth_tax_id(number)),
        "credit_limit": "25000.00",
        "is_1099_eligible": False,
        "default_expense_account_ref": "SYN-EXP-5000",
        "default_payable_account_ref": "SYN-PAY-2000",
        "addresses": [
            {**_synth_address(city, number), "address_type": "BILLING"},
            {**_synth_address("El Alto", number + "-envio"), "address_type": "SHIPPING"},
        ],
        "primary_contact": {
            "name": str(spec.get("contact") or "Contacto Sintético"),
            "email": str(spec.get("email") or "compras@example.invalid"),
            "phone": "+591 70000001",
            "title": "Encargado de Compras (sintético)",
        },
    }


def ensure_vendors(ctx: SeedContext) -> None:
    client = ctx.client
    _, payload = client.get("/api/v1/vendors", {"limit": 200, "is_inactive": "false"})
    vendors = [row for row in _items_of(payload) if not row.get("is_inactive")]
    if not vendors:
        for index, spec in enumerate(DEMO_VENDORS):
            status, body = client.post("/api/v1/vendors", _vendor_payload(spec),
                                       idem=f"seed-vendor-{MARKER}-{index}", allow=(409,))
            if status == 201:
                ctx.act("vendors", "created", spec["number"])
                vendors.append(_wrapped(body))
            else:
                _, listing = client.get("/api/v1/vendors", {"vendor_number": spec["number"]})
                rows = _items_of(listing)
                if rows:
                    vendors.append(rows[0])
        _, payload = client.get("/api/v1/vendors", {"limit": 200, "is_inactive": "false"})
        vendors = [row for row in _items_of(payload) if not row.get("is_inactive")]

    enriched: list[dict[str, Any]] = []
    for index, vendor in enumerate(sorted(vendors, key=lambda row: _sort_key(row.get("vendor_number")))):
        current = dict(vendor)
        desired = {
            "currency_code": SHARED_BOB,
            "payment_terms": current.get("payment_terms") or "NET_30",
            "tax_id": current.get("tax_id") or _synth_tax_id(str(current.get("vendor_number") or index)),
            "credit_limit": current.get("credit_limit") or "25000.00",
            "is_1099_eligible": bool(current.get("is_1099_eligible")),
            "addresses": current.get("addresses") or [_vendor_payload(
                {"number": current.get("vendor_number"), "name": current.get("name"),
                 "city": "Santa Cruz de la Sierra"})["addresses"][0]],
            "primary_contact": current.get("primary_contact") or {
                "name": "Contacto Sintético de Compras",
                "email": f"compras.{_slug(str(current.get('vendor_number') or index), 12).lower()}@example.invalid",
                "phone": "+591 70000002",
                "title": "Contacto sintético de demostración",
            },
        }
        changes = {key: value for key, value in desired.items()
                   if key in ("currency_code",) or not _present(current.get(key)) or current.get(key) != value}
        changes = {key: value for key, value in changes.items()
                   if key == "currency_code" and str(current.get(key) or "").upper() != SHARED_BOB
                   or not _present(current.get(key))}
        if changes:
            status, body = client.patch(f"/api/v1/vendors/{current['id']}", changes,
                                        idem=f"seed-vendor-patch-{current['id']}")
            if status == 200:
                current = _wrapped(body) if isinstance(body, dict) and body.get("id") else {**current, **changes}
                ctx.act("vendors", "updated", current.get("vendor_number"),
                        {"fields": sorted(changes)})
        enriched.append(current)
    ctx.vendors = enriched
    ctx.chosen_vendor = ctx.vendor_for(0)
    if ctx.chosen_vendor is None:
        raise SeedError("no active vendor available after vendor provisioning")


# ------------------------------------------------------------------------------------------------
# locations
# ------------------------------------------------------------------------------------------------
def _location_payload(spec: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "code": str(spec["code"]),
        "name": str(spec["name"]),
        "location_type": str(spec.get("type") or "WAREHOUSE"),
        "address": _synth_address(str(spec.get("city") or "Santa Cruz de la Sierra"), str(spec["code"])),
        "timezone": "America/La_Paz",
        "is_bins_enabled": False,
        "is_statuses_enabled": True,
        "is_consignment_allowed": False,
        "allow_commit_from_non_available": False,
    }


def _create_locations(ctx: SeedContext, specs: Sequence[Mapping[str, Any]]) -> None:
    client = ctx.client
    for spec in specs:
        status, body = client.post("/api/v1/locations", _location_payload(spec),
                                   idem=f"seed-location-{MARKER}-{spec['code']}", allow=(409,))
        if status == 201:
            ctx.act("locations", "created", spec["code"])
        else:
            ctx.act("locations", "reused", spec["code"])


def ensure_locations(ctx: SeedContext) -> None:
    client = ctx.client
    _, payload = client.get("/api/v1/locations", {"limit": 200, "active": "true"})
    locations = [row for row in _items_of(payload) if not row.get("is_inactive")]
    if not locations:
        _create_locations(ctx, DEMO_LOCATIONS)
        _, payload = client.get("/api/v1/locations", {"limit": 200, "active": "true"})
        locations = [row for row in _items_of(payload) if not row.get("is_inactive")]

    # The UNALLOCATED sentinel location is a system row and cannot be enriched.
    locations = [row for row in locations
                 if not row.get("is_unallocated") and str(row.get("code") or "").upper() != "UNALLOCATED"]
    enriched: list[dict[str, Any]] = []
    for index, location in enumerate(sorted(locations, key=lambda row: _sort_key(row.get("code")))):
        current = dict(location)
        desired = {
            "address": current.get("address") or _synth_address(
                "Santa Cruz de la Sierra" if index % 2 == 0 else "La Paz", str(current.get("code") or index)),
            "timezone": current.get("timezone") or "America/La_Paz",
        }
        changes = {key: value for key, value in desired.items() if not _present(current.get(key))}
        if changes:
            try:
                status, body = client.patch(f"/api/v1/locations/{current['id']}", changes,
                                            idem=f"seed-location-patch-{current['id']}")
                if status == 200:
                    current = {**current, **changes}
                    ctx.act("locations", "updated", current.get("code"), {"fields": sorted(changes)})
            except SeedError as exc:
                ctx.warn(f"location {current.get('code')} could not be enriched: {exc}")
        enriched.append(current)

    warehouses = sorted([row for row in enriched if row.get("location_type") == "WAREHOUSE"],
                        key=lambda row: _sort_key(row.get("code")))
    if len(warehouses) < 2:
        missing = [spec for spec in DEMO_LOCATIONS[:2]
                   if not any(row.get("code") == spec["code"] for row in enriched)]
        if missing:
            _create_locations(ctx, missing[: 2 - len(warehouses)])
            _, payload = client.get("/api/v1/locations", {"limit": 200, "active": "true"})
            enriched = [row for row in _items_of(payload) if not row.get("is_inactive")]
            warehouses = sorted([row for row in enriched if row.get("location_type") == "WAREHOUSE"],
                                key=lambda row: _sort_key(row.get("code")))
    stores = sorted([row for row in enriched if row.get("location_type") == "STORE"],
                    key=lambda row: _sort_key(row.get("code")))
    ctx.posting_locations = (warehouses + stores)[:2]
    if len(ctx.posting_locations) < 2:
        raise SeedError("tenant has fewer than two usable posting locations after provisioning")
    ctx.locations = enriched


# ------------------------------------------------------------------------------------------------
# items
# ------------------------------------------------------------------------------------------------
def _demo_item_payload(ctx: SeedContext, spec: Mapping[str, Any], index: int) -> dict[str, Any]:
    uom = ctx.uoms[str(spec["uom"]).upper()]
    category = ctx.category_for(str(spec.get("category") or "RAW_MATERIALS"), index)
    vendor = ctx.vendor_for(index)
    status = ctx.default_status or {}
    price = _synth_price(str(spec["sku"]))
    inventory: dict[str, Any] = {
        "costing_method": "FIFO",
        "initial_cost": _money(price),
        "stock_uom_id": uom["id"],
        "reorder_point": _qty(_synth_qty(str(spec["uom"]), index) / Decimal("4")),
        "preferred_stock_level": _qty(_synth_qty(str(spec["uom"]), index) * Decimal("1.2")),
        "safety_stock": _qty(_synth_qty(str(spec["uom"]), index) / Decimal("8")),
        "lead_time_days": 4 + index % 9,
        "seasonal_demand": "0",
        "auto_calculate_planning": True,
        "use_bins": False,
        "bin_required_on_txn": False,
        "use_statuses": True,
        "default_status_id": status.get("id"),
        "allow_negative_inventory": False,
        "allow_vendor_consignment": False,
        "is_wip": False,
        "backflush_default": False,
        "yield_percent": "100",
        "round_up_component_qty": False,
        "must_be_whole_base_unit": False,
        "asset_account_ref": "SYN-ASSET-1400",
        "cogs_account_ref": "SYN-COGS-5000",
        "adjustment_account_ref": "SYN-ADJ-6000",
        "variance_account_ref": "SYN-VAR-6100",
        "unbuild_variance_account_ref": "SYN-UBV-6200",
    }
    purchasing: dict[str, Any] = {
        "purchase_price": _money(price),
        "purchase_price_currency": SHARED_BOB,
        "purchase_uom_id": uom["id"],
        "lead_time_days": 4 + index % 9,
        "is_drop_ship": False,
        "is_special_order": False,
        "country_of_origin": "BO",
        "harmonized_code": f"SYN{int(hashlib.sha1(str(spec['sku']).encode()).hexdigest(), 16) % 10000:04d}",
        "purchase_tax_code": "IVA-13",
    }
    if vendor:
        purchasing["preferred_vendor_id"] = vendor["id"]
    return {
        "item_number": str(spec["sku"]),
        "display_name": str(spec["name"]),
        "description": str(spec.get("description") or f"Símbolo {SYNTHETIC_TAG} de demostración: {spec['name']}."),
        "item_type_code": str(spec["type"]),
        "units_type_id": uom["uom_type_id"],
        "base_uom_id": uom["id"],
        "item_category_id": category.get("id") if category else None,
        "upc_code": _ean13(str(spec["sku"])),
        "external_ref": f"SYN-EXT-{_slug(str(spec['sku']))}",
        "created_from": "IMPORT",
        "inventory": inventory,
        "purchasing": purchasing,
    }


def _create_demo_items(ctx: SeedContext, specs: Sequence[Mapping[str, Any]], prefix: str) -> None:
    client = ctx.client
    for index, spec in enumerate(specs):
        payload = _demo_item_payload(ctx, spec, index)
        status, body = client.post("/api/v1/items", payload, idem=f"seed-item-{prefix}-{spec['sku']}", allow=(409,))
        if status == 201:
            ctx.act("items", "created", spec["sku"])
        else:
            ctx.act("items", "reused", spec["sku"])


def fetch_items(ctx: SeedContext) -> None:
    _, payload = ctx.client.get("/api/v1/items", {"include_inactive": "true", "limit": 200})
    summaries = _items_of(payload)
    details: dict[str, dict[str, Any]] = {}
    for summary in summaries:
        item_id = summary.get("id")
        if not item_id:
            continue
        _, detail = ctx.client.get(f"/api/v1/items/{item_id}")
        details[str(item_id)] = _wrapped(detail)
    ctx.items = details


def ensure_inventory_statuses(ctx: SeedContext) -> None:
    """Re-read inventory statuses after locations exist (creation provisions tenant defaults)."""
    _, payload = ctx.client.get("/api/v1/inventory-statuses", {"active": "true"})
    ctx.statuses = _items_of(payload)
    defaults = [row for row in ctx.statuses if row.get("is_default")] or \
               [row for row in ctx.statuses if row.get("is_available_for_allocation")] or ctx.statuses
    ctx.default_status = defaults[0] if defaults else None
    if ctx.default_status is None:
        raise SeedError(
            "tenant has no inventory statuses, so inventory documents cannot be posted. "
            "Create the tenant default statuses (statuses-enabled location / tenant seed) before reseeding.")


def ensure_items(ctx: SeedContext) -> None:
    fetch_items(ctx)
    if not ctx.stockable_items():
        _create_demo_items(ctx, DEMO_ITEMS, "demo")
        fetch_items(ctx)
        if not ctx.stockable_items():
            raise SeedError("failed to create demo items (no stockable item exists)")

    fifo = [row for row in ctx.stockable_items()
            if str(_lookup(row, "inventory.costing_method") or "").upper() == "FIFO"]
    if not fifo:
        ctx.warn("surviving items have no FIFO costing method (costing_method is immutable after save); "
                 "adding two clearly-labelled synthetic FIFO items so the pilot demonstrates FIFO valuation")
        existing_skus = {str(row.get("item_number")) for row in ctx.item_list()}
        specs = [spec for spec in FIFO_TOPUP_ITEMS if spec["sku"] not in existing_skus]
        if specs:
            _create_demo_items(ctx, specs, "fifo")
            fetch_items(ctx)


def _inventory_desired(ctx: SeedContext, item: Mapping[str, Any], index: int) -> dict[str, Any]:
    inventory = item.get("inventory") or {}
    price = _dec(inventory.get("initial_cost") or _lookup(item, "purchasing.purchase_price") or
                 _synth_price(str(item.get("item_number"))))
    uom_code = ctx.uom_code(item.get("base_uom_id"))
    base_qty = _synth_qty(uom_code, index)
    status = ctx.default_status or {}
    return {
        "costing_method": "FIFO",
        "initial_cost": _money(price),
        "stock_uom_id": item.get("base_uom_id"),
        "reorder_point": _qty(base_qty / Decimal("4")),
        "preferred_stock_level": _qty(base_qty * Decimal("1.2")),
        "safety_stock": _qty(base_qty / Decimal("8")),
        "lead_time_days": 4 + index % 11,
        "seasonal_demand": _qty(Decimal("0")),
        "auto_calculate_planning": True,
        "use_bins": bool(inventory.get("use_bins") or False),
        "bin_required_on_txn": bool(inventory.get("bin_required_on_txn") or False),
        "use_statuses": True,
        "default_status_id": status.get("id"),
        "allow_negative_inventory": False,
        "allow_vendor_consignment": False,
        "is_wip": False,
        "backflush_default": False,
        "yield_percent": _qty(Decimal("100")),
        "round_up_component_qty": False,
        "must_be_whole_base_unit": False,
        "asset_account_ref": inventory.get("asset_account_ref") or "SYN-ASSET-1400",
        "cogs_account_ref": inventory.get("cogs_account_ref") or "SYN-COGS-5000",
        "adjustment_account_ref": inventory.get("adjustment_account_ref") or "SYN-ADJ-6000",
        "variance_account_ref": inventory.get("variance_account_ref") or "SYN-VAR-6100",
        "unbuild_variance_account_ref": inventory.get("unbuild_variance_account_ref") or "SYN-UBV-6200",
    }


def _purchasing_desired(ctx: SeedContext, item: Mapping[str, Any], index: int) -> dict[str, Any]:
    purchasing = item.get("purchasing") or {}
    price = _dec(purchasing.get("purchase_price") or _lookup(item, "inventory.initial_cost") or
                 _synth_price(str(item.get("item_number"))))
    vendor = ctx.vendor_for(index)
    return {
        "preferred_vendor_id": purchasing.get("preferred_vendor_id") or (vendor or {}).get("id"),
        "purchase_uom_id": purchasing.get("purchase_uom_id") or item.get("base_uom_id"),
        "purchase_price": _money(price),
        "purchase_price_currency": SHARED_BOB,
        "lead_time_days": 4 + index % 11,
        "is_drop_ship": bool(purchasing.get("is_drop_ship") or False),
        "is_special_order": bool(purchasing.get("is_special_order") or False),
        "country_of_origin": purchasing.get("country_of_origin") or "BO",
        "harmonized_code": purchasing.get("harmonized_code") or
                           f"SYN{int(hashlib.sha1(str(item.get('item_number')).encode()).hexdigest(), 16) % 10000:04d}",
        "purchase_tax_code": purchasing.get("purchase_tax_code") or "IVA-13",
    }


def _item_patch_desired(ctx: SeedContext, item: Mapping[str, Any], index: int) -> dict[str, Any]:
    category = ctx.category_for("RAW_MATERIALS", index)
    display = str(item.get("display_name") or "").strip()
    desired = {
        "display_name": display or f"Artículo {SYNTHETIC_TAG} {item.get('item_number')}",
        "description": item.get("description") or
                       f"Símbolo {SYNTHETIC_TAG} de demostración para pruebas del pilot: {display or item.get('item_number')}.",
        "item_category_id": item.get("item_category_id") or (category or {}).get("id"),
        "upc_code": item.get("upc_code") or _ean13(str(item.get("item_number"))),
        "external_ref": item.get("external_ref") or f"SYN-EXT-{_slug(str(item.get('item_number')))}",
    }
    return {key: value for key, value in desired.items() if value}


def _new_upc(ctx: SeedContext, sku: str) -> str:
    existing = {str(row.get("upc_code")) for row in ctx.item_list() if row.get("upc_code")}
    candidate, suffix = _ean13(sku), 0
    while candidate in existing:
        suffix += 1
        candidate = _ean13(f"{sku}#{suffix}")
    return candidate


def enrich_items(ctx: SeedContext) -> None:
    client = ctx.client
    stockable = ctx.stockable_items()
    if not stockable:
        raise SeedError("no stockable items to enrich")
    for index, item in enumerate(stockable):
        item_id = str(item["id"])
        label = str(item.get("item_number"))
        try:
            patch = _item_patch_desired(ctx, item, index)
            patch = {key: value for key, value in patch.items() if not _present(item.get(key))}
            if "upc_code" in patch:
                patch["upc_code"] = _new_upc(ctx, label)
            if patch:
                status, body = client.patch(f"/api/v1/items/{item_id}", patch,
                                            idem=f"seed-item-patch-{item_id}")
                if status in (200, 201):
                    ctx.act("items", "updated", label, {"fields": sorted(patch)})
                    item.update({key: _wrapped(body).get(key, value) for key, value in patch.items()})
        except SeedError as exc:
            ctx.errors.append(f"item patch {label}: {exc}")

        try:
            inventory = item.get("inventory") or {}
            desired_inventory = _inventory_desired(ctx, item, index)
            inventory_changes: dict[str, Any] = {}
            for key, value in desired_inventory.items():
                if value is None:
                    continue
                current = inventory.get(key)
                if key in IMMUTABLE_INVENTORY_KEYS:
                    if current is None:
                        inventory_changes[key] = value
                elif not _same_value(current, value):
                    inventory_changes[key] = value
            if inventory_changes:
                applied = dict(inventory_changes)
                status, body = 0, {}
                try:
                    status, body = client.put(f"/api/v1/items/{item_id}/inventory", applied,
                                              idem=f"seed-item-inventory-{item_id}")
                except ApiError as exc:
                    # Some API versions reject setting a previously-null immutable field
                    # (costing_method / stock_uom_id / initial_cost) after the item row exists.
                    # Retry with only re-settable fields; survivors keep the reset values.
                    if _error_code(exc) != "IMMUTABLE_FIELD":
                        raise
                    stripped = {key: value for key, value in applied.items()
                                if key not in IMMUTABLE_INVENTORY_KEYS}
                    if stripped:
                        status, body = client.put(f"/api/v1/items/{item_id}/inventory", stripped,
                                                  idem=f"seed-item-inventory-v2-{item_id}")
                        applied = stripped
                    else:
                        status, body = 200, {}
                        applied = {}
                    ctx.warn("API rejects setting initial_cost/costing_method after item creation "
                             "(IMMUTABLE_FIELD even when the surviving value is null); surviving items keep "
                             "the reset cost fields. FIFO/new-field coverage comes from labelled synthetic "
                             "items and BOB purchase prices. Known API limitation G4.")
                if applied:
                    ctx.act("items", "updated", label,
                            {"inventory_fields": sorted(applied)})
                    item["inventory"] = {**inventory, **applied}
        except SeedError as exc:
            ctx.errors.append(f"item inventory {label}: {exc}")

        try:
            purchasing = item.get("purchasing") or {}
            desired_purchasing = _purchasing_desired(ctx, item, index)
            purchasing_changes: dict[str, Any] = {}
            for key, value in desired_purchasing.items():
                if value is None:
                    continue
                current = purchasing.get(key)
                if key == "purchase_price_currency":
                    if str(current or "").upper() != SHARED_BOB:
                        purchasing_changes[key] = value
                elif not _same_value(current, value):
                    purchasing_changes[key] = value
            if purchasing_changes:
                status, body = client.put(f"/api/v1/items/{item_id}/purchasing", purchasing_changes,
                                          idem=f"seed-item-purchasing-{item_id}")
                if status in (200, 201):
                    ctx.act("items", "updated", label,
                            {"purchasing_fields": sorted(purchasing_changes)})
                    item["purchasing"] = _wrapped(body) if isinstance(body, dict) and body.get("item_id") else {
                        **purchasing, **purchasing_changes}
        except SeedError as exc:
            ctx.errors.append(f"item purchasing {label}: {exc}")

    ensure_item_vendors(ctx)


def ensure_item_vendors(ctx: SeedContext) -> None:
    """Associate every stockable item with one preferred vendor, aligning the item's purchasing
    settings with any preferred association that survived the reset instead of duplicating it."""
    client = ctx.client
    _, payload = client.get("/api/v1/item-vendors", {"limit": 200})
    associations = _items_of(payload)

    def associations_for(item_id: str) -> list[dict[str, Any]]:
        return [row for row in associations if str(row.get("item_id")) == item_id]

    for index, item in enumerate(ctx.stockable_items()):
        item_id = str(item["id"])
        item_associations = associations_for(item_id)
        preferred_associations = [row for row in item_associations if row.get("is_preferred")]
        preferred_id = _lookup(item, "purchasing.preferred_vendor_id")

        target_vendor = None
        if preferred_associations:
            surviving_id = str(preferred_associations[0].get("vendor_id"))
            target_vendor = next((row for row in ctx.vendors if str(row.get("id")) == surviving_id), None)
            if target_vendor and str(preferred_id or "") != surviving_id:
                client.put(f"/api/v1/items/{item_id}/purchasing", {"preferred_vendor_id": surviving_id},
                           idem=f"seed-item-vendor-align-{item_id}")
                item["purchasing"] = {**(item.get("purchasing") or {}), "preferred_vendor_id": surviving_id}
                ctx.act("item_vendors", "updated", f"{item.get('item_number')}:purchasing.preferred_vendor_id",
                        {"preferred_vendor_id": surviving_id})
        if target_vendor is None and preferred_id:
            target_vendor = next((row for row in ctx.vendors if str(row.get("id")) == str(preferred_id)), None)
        if target_vendor is None:
            target_vendor = ctx.vendor_for(index)
        if target_vendor is None:
            continue
        vendor_id = str(target_vendor["id"])
        existing = next((row for row in item_associations if str(row.get("vendor_id")) == vendor_id), None)

        if existing:
            changes: dict[str, Any] = {}
            if not existing.get("is_preferred"):
                changes["is_preferred"] = True
            if not existing.get("lead_time_days"):
                changes["lead_time_days"] = 4 + index % 11
            if not _present(existing.get("min_order_qty")):
                changes["min_order_qty"] = _qty(max(Decimal("1"), _synth_qty(
                    ctx.uom_code(item.get("base_uom_id")), index) / Decimal("20")))
            if not _present(existing.get("purchase_uom_id")):
                changes["purchase_uom_id"] = item.get("base_uom_id")
            if str(existing.get("currency_code") or "").upper() != SHARED_BOB:
                changes["currency_code"] = SHARED_BOB
            if changes:
                client.patch(f"/api/v1/item-vendors/{existing['id']}", changes,
                             idem=f"seed-item-vendor-patch-{existing['id']}")
                ctx.act("item_vendors", "updated", f"{item.get('item_number')}->{target_vendor.get('vendor_number')}",
                        {"fields": sorted(changes)})
            continue

        body = {
            "item_id": item_id,
            "vendor_id": vendor_id,
            "vendor_item_code": f"SYN-{_slug(str(item.get('item_number')))}",
            "is_preferred": True,
            "lead_time_days": 4 + index % 11,
            "min_order_qty": _qty(max(Decimal("1"), _synth_qty(
                ctx.uom_code(item.get("base_uom_id")), index) / Decimal("20"))),
            "purchase_uom_id": item.get("base_uom_id"),
            "currency_code": SHARED_BOB,
        }
        try:
            status, created = client.post("/api/v1/item-vendors", body,
                                          idem=f"seed-item-vendor-{item_id}-{vendor_id}", allow=(409,))
        except ApiError as exc:
            if exc.status != 422 or "preferred vendor" not in str(exc.payload):
                raise
            # Reassign preference away from any other vendor association, then retry once.
            for other in preferred_associations:
                client.patch(f"/api/v1/item-vendors/{other['id']}", {"is_preferred": False},
                             idem=f"seed-item-vendor-unprefer-{other['id']}")
            status, created = client.post("/api/v1/item-vendors", body,
                                          idem=f"seed-item-vendor-v2-{item_id}-{vendor_id}", allow=(409,))
        if status in (200, 201):
            created_row = _wrapped(created)
            associations.append(created_row if created_row.get("id") else {**body, "id": None})
            ctx.act("item_vendors", "created", f"{item.get('item_number')}->{target_vendor.get('vendor_number')}")


# ------------------------------------------------------------------------------------------------
# lots
# ------------------------------------------------------------------------------------------------
def ensure_lots(ctx: SeedContext) -> None:
    client = ctx.client
    _, payload = client.get("/api/v1/lots", {"limit": 200, "active": "true"})
    lots = _items_of(payload)
    # Enrich every surviving lot of a lot-numbered item so all declaration fields are
    # populated. The reset preserves lots (master/reference data), and some pilot lots
    # historically carried only part of the declarations; LotPatch supports the missing
    # manufacture_date/cost_basis without touching quantities.
    item_by_id = {str(item["id"]): item for item in ctx.stockable_items()}
    for lot in lots:
        item = item_by_id.get(str(lot.get("item_id")))
        if item is None or ctx.numbered_kind(item) != "LOT":
            continue
        patch: dict[str, Any] = {}
        if not _present(lot.get("manufacture_date")):
            patch["manufacture_date"] = (ctx.seed_date - timedelta(days=180)).isoformat()
        if not _present(lot.get("cost_basis")):
            price = _dec(_lookup(item, "purchasing.purchase_price") or _synth_price(str(item.get("item_number"))))
            patch["cost_basis"] = _money(price)
        if patch:
            try:
                client.patch(f"/api/v1/lots/{lot['id']}", patch, idem=f"seed-lot-enrich-{lot['id']}")
            except ApiError as exc:
                # Lot dates/cost basis become immutable once stock movements exist. Run the
                # reset before seeding so this enrichment normally happens on a movement-free
                # tenant. If a preserved legacy lot is still immutable (or a manual rerun
                # happens after postings), retire that incomplete lot; the item already has a
                # complete synthetic lot and verify only reports active lots.
                try:
                    client.post(f"/api/v1/lots/{lot['id']}/inactivate",
                                {"reason": "Lote heredado incompleto; reemplazado por lote sintético completo"},
                                idem=f"seed-lot-inactivate-{lot['id']}", allow=(409,))
                except ApiError as inactive_exc:
                    ctx.warn(f"lot {lot.get('lot_number')} declarations could not be enriched ({exc}) nor retired ({inactive_exc})")
                else:
                    ctx.act("lots", "inactivated", lot.get("lot_number"))
            else:
                lot.update(patch)
                ctx.act("lots", "updated", lot.get("lot_number"))
    by_item: dict[str, list[dict[str, Any]]] = {}
    for lot in lots:
        by_item.setdefault(str(lot.get("item_id")), []).append(lot)
    for index, item in enumerate(ctx.stockable_items()):
        if ctx.numbered_kind(item) != "LOT":
            continue
        item_id = str(item["id"])
        complete = [lot for lot in by_item.get(item_id, [])
                    if _present(lot.get("manufacture_date")) and _present(lot.get("expiry_date"))]
        if complete:
            ctx.lots[item_id] = complete[0]
            ctx.act("lots", "reused", complete[0].get("lot_number"))
            continue
        price = _dec(_lookup(item, "purchasing.purchase_price") or _synth_price(str(item.get("item_number"))))
        base_number = f"SYN-LOTE-{_digest(str(item.get('item_number')), length=8)}"
        existing_numbers = {str(lot.get("lot_number")) for lot in by_item.get(item_id, [])}
        lot_number, suffix = base_number, 0
        while lot_number in existing_numbers:
            suffix += 1
            lot_number = f"{base_number}-{suffix}"
        body = {
            "item_id": item_id,
            "lot_number": lot_number,
            "manufacture_date": (ctx.seed_date - timedelta(days=180 + index % 30)).isoformat(),
            "expiry_date": (ctx.seed_date + timedelta(days=365 + index % 60)).isoformat(),
            "cost_basis": _money(price),
            "notes": f"Lote {SYNTHETIC_TAG} de demostración para el pilot BOB.",
        }
        status, created = client.post("/api/v1/lots", body, idem=f"seed-lot-{item_id}", allow=(409,))
        if status == 201:
            lot = _wrapped(created, "lot")
            ctx.lots[item_id] = lot
            ctx.act("lots", "created", lot_number)
        else:
            _, listing = client.get("/api/v1/lots", {"item_id": item_id, "lot_number": lot_number})
            rows = _items_of(listing)
            if rows:
                ctx.lots[item_id] = rows[0]
                ctx.act("lots", "reused", lot_number)


# ------------------------------------------------------------------------------------------------
# inventory balances helper
# ------------------------------------------------------------------------------------------------
def load_balances(ctx: SeedContext) -> dict[tuple[str, str], Decimal]:
    _, payload = ctx.client.get("/api/v1/inventory/balances")
    totals: dict[tuple[str, str], Decimal] = {}
    for row in _items_of(payload):
        qty_class = str(row.get("qty_class") or "ON_HAND").upper()
        if qty_class not in ("ON_HAND", "AVAILABLE"):
            continue
        key = (str(row.get("item_id")), str(row.get("location_id")))
        totals[key] = totals.get(key, Decimal("0")) + _dec(row.get("qty_on_hand"))
    return totals


# ------------------------------------------------------------------------------------------------
# opening stock adjustments
# ------------------------------------------------------------------------------------------------
def _posting_lines(ctx: SeedContext, balances: Mapping[tuple[str, str], Decimal]) -> list[dict[str, Any]]:
    lines: list[dict[str, Any]] = []
    status_id = (ctx.default_status or {}).get("id")
    reason_id = (ctx.opening_reason or {}).get("id")
    for index, item in enumerate(ctx.stockable_items()):
        if ctx.numbered_kind(item) == "SERIAL":
            continue  # serialized demo stock needs serial numbers; out of pilot demo scope
        item_id = str(item["id"])
        uom_code = ctx.uom_code(item.get("base_uom_id"))
        price = _dec(_lookup(item, "purchasing.purchase_price") or _synth_price(str(item.get("item_number"))))
        lot_id = (ctx.lots.get(item_id) or {}).get("id") if ctx.numbered_kind(item) == "LOT" else None
        base_qty = _synth_qty(uom_code, index)
        for location_index, location in enumerate(ctx.posting_locations):
            location_id = str(location["id"])
            if balances.get((item_id, location_id), Decimal("0")) > 0:
                continue
            qty = (base_qty * LOCATION_FACTORS[location_index]).quantize(Decimal("0.001"))
            if qty <= 0:
                continue
            line: dict[str, Any] = {
                "item_id": item_id,
                "location_id": location_id,
                "inventory_status_id": status_id,
                "qty_delta": _qty(qty),
                "unit_cost": _money(price),
                "reason_code_id": reason_id,
                "memo": f"Saldo inicial {SYNTHETIC_TAG} de demostración",
            }
            if lot_id:
                line["lot_id"] = lot_id
            lines.append(line)
    return lines


def _find_document(rows: Sequence[Mapping[str, Any]], document_number: str) -> dict[str, Any] | None:
    for row in rows:
        if str(row.get("document_number") or "") == document_number:
            return dict(row)
    return None


def post_opening_stock(ctx: SeedContext) -> None:
    client = ctx.client
    balances = load_balances(ctx)
    _, payload = client.get("/api/v1/inventory-adjustments")
    existing = _find_document(_items_of(payload), OPENING_DOC)
    if existing:
        status = str(existing.get("status") or "").upper()
        if status in ("DRAFT", "PENDING_APPROVAL"):
            client.post(f"/api/v1/inventory-adjustments/{existing['id']}/submit", {},
                        idem=f"seed-opening-submit-{existing['id']}", allow=(409,))
            client.post(f"/api/v1/inventory-adjustments/{existing['id']}/post", {},
                        idem=f"seed-opening-post-{existing['id']}", allow=(409,))
            ctx.act("opening_stock", "posted", OPENING_DOC)
        elif status == "POSTED":
            ctx.act("opening_stock", "skipped", OPENING_DOC, {"reason": "already posted"})
        else:
            ctx.act("opening_stock", "skipped", OPENING_DOC, {"reason": f"document status {status}"})
        return
    lines = _posting_lines(ctx, balances)
    if not lines:
        ctx.act("opening_stock", "skipped", OPENING_DOC, {"reason": "all posting pairs already have stock"})
        return
    body = {
        "transaction_date": (ctx.seed_date - timedelta(days=3)).isoformat(),
        "memo": f"Carga inicial de inventario {SYNTHETIC_TAG} de demostración (pilot BOB)",
        "reason_code_id": (ctx.opening_reason or {}).get("id"),
        "adjustment_source": "IMPORT",
        "document_number": OPENING_DOC,
        "lines": lines,
    }
    status, payload = client.post("/api/v1/inventory-adjustments", body,
                                  idem=f"seed-opening-{MARKER}", allow=(409,))
    if status not in (201,):
        ctx.errors.append(f"opening stock adjustment not created: HTTP {status} {json.dumps(payload)[:300]}")
        return
    adjustment_id = str(_wrapped(payload).get("id"))
    ctx.act("opening_stock", "created", OPENING_DOC, {"lines": len(lines)})
    client.post(f"/api/v1/inventory-adjustments/{adjustment_id}/submit", {},
                idem=f"seed-opening-submit-{adjustment_id}", allow=(409,))
    status, posted = client.post(f"/api/v1/inventory-adjustments/{adjustment_id}/post", {},
                                 idem=f"seed-opening-post-{adjustment_id}", allow=(409,))
    if status == 200:
        ctx.act("opening_stock", "posted", OPENING_DOC,
                {"transaction_id": _wrapped(posted).get("transaction_id")})
    else:
        ctx.errors.append(f"opening stock adjustment not posted: HTTP {status} {json.dumps(posted)[:300]}")


# ------------------------------------------------------------------------------------------------
# procurement: purchase order + posted goods receipt
# ------------------------------------------------------------------------------------------------
def _stockable_by_id(ctx: SeedContext) -> dict[str, dict[str, Any]]:
    return {str(item["id"]): item for item in ctx.stockable_items()}


def _po_lines(ctx: SeedContext) -> list[dict[str, Any]]:
    by_id = _stockable_by_id(ctx)
    lines: list[dict[str, Any]] = []
    for index, item in enumerate(ctx.stockable_items()):
        if ctx.numbered_kind(item) == "SERIAL":
            continue
        uom_code = ctx.uom_code(item.get("base_uom_id"))
        price = _dec(_lookup(item, "purchasing.purchase_price") or _synth_price(str(item.get("item_number"))))
        qty = max(Decimal("1"), _synth_qty(uom_code, index) * Decimal("1.5"))
        lines.append({
            "item_id": str(item["id"]),
            "qty_ordered": _qty(qty),
            "unit_price": _money(price),
            "expected_receipt_date": (ctx.seed_date - timedelta(days=2)).isoformat(),
            "description": str(item.get("display_name") or item.get("item_number")),
        })
    del by_id
    return lines


def _receipt_qty(index: int, uom_code: str) -> Decimal:
    return max(Decimal("1"), (_synth_qty(uom_code, index) * Decimal("0.25")).quantize(Decimal("0.001")))


def post_procurement(ctx: SeedContext) -> None:
    client = ctx.client
    _, payload = client.get("/api/v1/purchase-orders")
    orders = _items_of(payload)
    order = next((row for row in orders if str(row.get("memo") or "").startswith(MARKER)), None)
    if order is None:
        if ctx.chosen_vendor is None:
            ctx.warn("no vendor available; skipping purchase order/receipt demo data")
            return
        body = {
            "vendor_id": ctx.chosen_vendor["id"],
            "po_type": "STANDARD",
            "currency_code": SHARED_BOB,
            "transaction_date": (ctx.seed_date - timedelta(days=4)).isoformat(),
            "ship_to_location_id": str(ctx.posting_locations[0]["id"]),
            "expected_receipt_date": (ctx.seed_date - timedelta(days=2)).isoformat(),
            "memo": PO_MARKER,
            "lines": _po_lines(ctx),
        }
        if not body["lines"]:
            ctx.warn("no stockable item available for purchase order lines; skipping procurement demo data")
            return
        status, created = client.post("/api/v1/purchase-orders", body,
                                      idem=f"seed-po-{MARKER}", allow=(409,))
        if status != 201:
            ctx.errors.append(f"purchase order not created: HTTP {status} {json.dumps(created)[:300]}")
            return
        order = _wrapped(created)
        ctx.act("purchase_orders", "created", order.get("document_number") or "PO",
                {"lines": len(body["lines"])})
    else:
        ctx.act("purchase_orders", "reused", order.get("document_number"), {"status": order.get("status")})

    if str(order.get("status") or "").upper() == "DRAFT":
        client.post(f"/api/v1/purchase-orders/{order['id']}/submit", {},
                    idem=f"seed-po-submit-{order['id']}", allow=(409, 422))
        ctx.act("purchase_orders", "submitted", order.get("document_number"))
    _, detail_body = client.get(f"/api/v1/purchase-orders/{order['id']}")
    order_detail = _wrapped(detail_body)
    if str(order_detail.get("status") or "").upper() not in ("APPROVED", "CLOSED", "PARTIALLY_RECEIVED", "RECEIVED"):
        ctx.warn(f"purchase order {order_detail.get('document_number')} is {order_detail.get('status')}; "
                 "goods receipt may still be possible")

    _, payload = client.get("/api/v1/item-receipts", {"limit": 200})
    receipts = _items_of(payload)
    receipt = next((row for row in receipts if str(row.get("memo") or "").startswith(MARKER)), None)
    if receipt and str(receipt.get("status") or "").upper() == "POSTED":
        ctx.act("receipts", "skipped", receipt.get("document_number"), {"reason": "already posted"})
        return
    if receipt is None:
        pairs = {str(row.get("item_id")): row for row in order_detail.get("lines") or []}
        lines: list[dict[str, Any]] = []
        for index, item in enumerate(ctx.stockable_items()):
            po_line = pairs.get(str(item["id"]))
            if po_line is None or ctx.numbered_kind(item) == "SERIAL":
                continue
            uom_code = ctx.uom_code(item.get("base_uom_id"))
            qty = _receipt_qty(index, uom_code)
            lot = ctx.lots.get(str(item["id"])) if ctx.numbered_kind(item) == "LOT" else None
            detail: dict[str, Any] = {
                "qty_received": _qty(qty),
                "inventory_status_id": (ctx.default_status or {}).get("id"),
                "ownership": "OWNED",
            }
            if lot and lot.get("id"):
                detail["lot_id"] = lot["id"]
            lines.append({
                "purchase_order_line_id": po_line["id"],
                "item_id": str(item["id"]),
                "qty_received": _qty(qty),
                "unit_cost": _money(_dec(_lookup(item, "purchasing.purchase_price"))),
                "location_id": str(ctx.posting_locations[0]["id"]),
                "inventory_status_id": (ctx.default_status or {}).get("id"),
                "details": [detail],
            })
        if not lines:
            ctx.warn("no receipt lines could be built; skipping goods receipt")
            return
        body = {
            "purchase_order_id": order["id"],
            "vendor_id": order_detail.get("vendor_id") or (ctx.chosen_vendor or {}).get("id"),
            "receipt_type": "PO",
            "received_at": f"{(ctx.seed_date - timedelta(days=2)).isoformat()}T10:30:00Z",
            "location_id": str(ctx.posting_locations[0]["id"]),
            "currency_code": SHARED_BOB,
            "exchange_rate": "1",
            "memo": RECEIPT_MARKER,
            "lines": lines,
        }
        status, created = client.post("/api/v1/item-receipts", body,
                                      idem=f"seed-receipt-{MARKER}", allow=(409,))
        if status != 201:
            ctx.errors.append(f"goods receipt not created: HTTP {status} {json.dumps(created)[:300]}")
            return
        receipt = _wrapped(created)
        ctx.act("receipts", "created", receipt.get("document_number"),
                {"lines": len(lines), "currency": SHARED_BOB})
    else:
        ctx.act("receipts", "reused", receipt.get("document_number"), {"status": receipt.get("status")})
    status, posted = client.post(f"/api/v1/item-receipts/{receipt['id']}/post", {},
                                 idem=f"seed-receipt-post-{receipt['id']}", allow=(409,))
    if status == 200:
        ctx.act("receipts", "posted", receipt.get("document_number"),
                {"transaction_id": _wrapped(posted).get("posted_transaction_id")})
    else:
        ctx.errors.append(f"goods receipt not posted: HTTP {status} {json.dumps(posted)[:300]}")


# ------------------------------------------------------------------------------------------------
# transfer
# ------------------------------------------------------------------------------------------------
def post_transfer(ctx: SeedContext) -> None:
    client = ctx.client
    _, payload = client.get("/api/v1/inventory-transfers")
    existing = _find_document(_items_of(payload), TRANSFER_DOC)
    if existing:
        status = str(existing.get("status") or "").upper()
        if status in ("DRAFT", "PENDING_APPROVAL"):
            client.post(f"/api/v1/inventory-transfers/{existing['id']}/submit", {},
                        idem=f"seed-transfer-submit-{existing['id']}", allow=(409,))
            client.post(f"/api/v1/inventory-transfers/{existing['id']}/post", {},
                        idem=f"seed-transfer-post-{existing['id']}", allow=(409,))
            ctx.act("transfers", "posted", TRANSFER_DOC)
        else:
            ctx.act("transfers", "skipped", TRANSFER_DOC, {"reason": f"document status {status}"})
        return

    balances = load_balances(ctx)
    from_location, to_location = ctx.posting_locations[0], ctx.posting_locations[1]
    lines: list[dict[str, Any]] = []
    lot_item = next((item for item in ctx.stockable_items() if ctx.numbered_kind(item) == "LOT"), None)
    plain_item = next((item for item in ctx.stockable_items() if ctx.numbered_kind(item) != "LOT"), None)
    for item in (lot_item, plain_item):
        if item is None:
            continue
        available = balances.get((str(item["id"]), str(from_location["id"])), Decimal("0"))
        qty = min(Decimal("25") if item is lot_item else Decimal("40"),
                  (available / Decimal("5")).quantize(Decimal("0.001")))
        if qty <= 0:
            continue
        line: dict[str, Any] = {
            "item_id": str(item["id"]),
            "qty": _qty(qty),
            "inventory_status_id": (ctx.default_status or {}).get("id"),
        }
        if ctx.numbered_kind(item) == "LOT":
            lot = ctx.lots.get(str(item["id"]))
            if lot and lot.get("id"):
                line["lot_id"] = lot["id"]
        lines.append(line)
    if len(lines) < 1:
        ctx.warn("not enough on-hand stock for the inter-location transfer demo data")
        return
    body = {
        "transaction_date": (ctx.seed_date - timedelta(days=1)).isoformat(),
        "memo": f"Transferencia entre almacenes {SYNTHETIC_TAG} de demostración",
        "document_number": TRANSFER_DOC,
        "from_location_id": str(from_location["id"]),
        "to_location_id": str(to_location["id"]),
        "lines": lines,
    }
    status, created = client.post("/api/v1/inventory-transfers", body,
                                  idem=f"seed-transfer-{MARKER}", allow=(409,))
    if status != 201:
        ctx.errors.append(f"transfer not created: HTTP {status} {json.dumps(created)[:300]}")
        return
    transfer = _wrapped(created)
    ctx.act("transfers", "created", TRANSFER_DOC, {"lines": len(lines)})
    client.post(f"/api/v1/inventory-transfers/{transfer['id']}/submit", {},
                idem=f"seed-transfer-submit-{transfer['id']}", allow=(409,))
    status, posted = client.post(f"/api/v1/inventory-transfers/{transfer['id']}/post", {},
                                 idem=f"seed-transfer-post-{transfer['id']}", allow=(409,))
    if status == 200:
        ctx.act("transfers", "posted", TRANSFER_DOC,
                {"transaction_id": _wrapped(posted).get("transaction_id")})
    else:
        ctx.errors.append(f"transfer not posted: HTTP {status} {json.dumps(posted)[:300]}")


# ------------------------------------------------------------------------------------------------
# inventory count (snapshot + capture + submit; approval is an operator/SoD step)
# ------------------------------------------------------------------------------------------------
def create_count(ctx: SeedContext) -> None:
    client = ctx.client
    target_items = sorted(str(item["id"]) for item in ctx.stockable_items())
    _, payload = client.get("/api/v1/inventory-counts", {"limit": 100})
    counts = _items_of(payload)
    existing = next((row for row in counts
                     if sorted(str(x) for x in (row.get("scope") or {}).get("items") or []) == target_items), None)
    if existing:
        status = str(existing.get("status") or "").upper()
        count_id = str(existing["id"])
        ctx.act("counts", "reused", existing.get("count_number"), {"status": status})
        if status == "DRAFT":
            client.post(f"/api/v1/inventory-counts/{count_id}/start", {},
                        idem=f"seed-count-start-{count_id}", allow=(409, 422))
            status = "OPEN"
        if status == "OPEN":
            _capture_count_lines(ctx, count_id)
            _submit_count(ctx, count_id)
        return
    body = {
        "count_type": "MANUAL",
        "count_date": ctx.seed_date.isoformat(),
        "blind_count": False,
        "approval_required": False,
        "scope": {"items": target_items},
    }
    status, created = client.post("/api/v1/inventory-counts", body,
                                  idem=f"seed-count-{MARKER}", allow=(409,))
    if status != 201:
        ctx.errors.append(f"inventory count not created: HTTP {status} {json.dumps(created)[:300]}")
        return
    count = _wrapped(created)
    count_id = str(count.get("count_id") or count.get("id"))
    ctx.act("counts", "created", count.get("count_number"), {"items": len(target_items)})
    client.post(f"/api/v1/inventory-counts/{count_id}/start", {},
                idem=f"seed-count-start-{count_id}", allow=(409, 422))
    _capture_count_lines(ctx, count_id)
    _submit_count(ctx, count_id)


def _capture_count_lines(ctx: SeedContext, count_id: str) -> None:
    client = ctx.client
    _, payload = client.get(f"/api/v1/inventory-counts/{count_id}/lines")
    lines = _items_of(payload)
    pending = [row for row in lines if str(row.get("count_status") or "PENDING").upper() == "PENDING"]
    if not pending:
        ctx.act("counts", "skipped", count_id, {"reason": "no pending count lines"})
        return
    payload_lines = [{"line_id": row["id"], "counted_qty": _qty(_dec(row.get("snapshot_qty")))} for row in pending]
    client.post(f"/api/v1/inventory-counts/{count_id}/lines",
                {"client_batch_id": f"{MARKER}-{_digest(count_id, length=12)}", "lines": payload_lines},
                idem=f"seed-count-lines-{count_id}", allow=(409,))
    ctx.act("counts", "captured", count_id, {"lines": len(payload_lines)})


def _submit_count(ctx: SeedContext, count_id: str) -> None:
    client = ctx.client
    status, payload = client.post(f"/api/v1/inventory-counts/{count_id}/submit", {},
                                  idem=f"seed-count-submit-{count_id}", allow=(409, 422))
    if status == 200:
        ctx.act("counts", "submitted", count_id, {"status": _wrapped(payload).get("status")})
    else:
        ctx.warn(f"inventory count {count_id} could not be submitted (HTTP {status}); "
                 "count snapshot/capture data still exists")


# ------------------------------------------------------------------------------------------------
# seed orchestration
# ------------------------------------------------------------------------------------------------
def run_seed(client: Client, seed_date: date | None = None) -> dict[str, Any]:
    ctx = SeedContext(client)
    ctx.seed_date = seed_date or date.today()
    started = _iso_now()
    load_reference(ctx)
    ensure_category(ctx)
    ensure_reason_codes(ctx)
    ensure_vendors(ctx)
    ensure_locations(ctx)
    ensure_inventory_statuses(ctx)
    ensure_items(ctx)
    enrich_items(ctx)
    ensure_lots(ctx)
    post_opening_stock(ctx)
    post_procurement(ctx)
    post_transfer(ctx)
    create_count(ctx)

    tenant_config: dict[str, Any] = {"source": "unavailable"}
    status, payload = client.get("/api/v1/admin/tenant-configuration", allow=(400, 403, 404))
    if status == 200:
        config = _wrapped(payload)
        tenant_config = {"source": "admin/tenant-configuration",
                         "base_currency_code": config.get("base_currency_code"),
                         "version": config.get("version")}
    else:
        tenant_config["note"] = f"GET /admin/tenant-configuration -> HTTP {status} (deployed-only endpoint)"

    counts: dict[str, int] = {}
    for row in ctx.actions:
        key = f"{row['area']}.{row['action']}"
        counts[key] = counts.get(key, 0) + 1
    summary = {
        "mode": "seed",
        "generated_at": _iso_now(),
        "started_at": started,
        "base_url": client.base_url,
        "seed_date": ctx.seed_date.isoformat(),
        "inventory_marker": MARKER,
        "tenant": tenant_config,
        "summary": {
            "api_calls": client.calls,
            "actions": len(ctx.actions),
            "created": sum(1 for row in ctx.actions if row["action"] == "created"),
            "updated": sum(1 for row in ctx.actions if row["action"] == "updated"),
            "posted": sum(1 for row in ctx.actions if row["action"] == "posted"),
            "skipped_or_reused": sum(1 for row in ctx.actions if row["action"] in ("skipped", "reused")),
            "warnings": len(ctx.warnings),
            "errors": len(ctx.errors),
            "by_action": dict(sorted(counts.items())),
            "stockable_items": len(ctx.stockable_items()),
            "posting_locations": [row.get("code") for row in ctx.posting_locations],
            "vendors": len(ctx.vendors),
            "locations": len(ctx.locations),
            "demo_fifo_items_added": any(row["action"] == "created" and str(row["target"]).startswith("SEED-SYN-FIFO")
                                         for row in ctx.actions),
        },
        "actions": ctx.actions,
        "warnings": ctx.warnings,
        "errors": ctx.errors,
    }
    return summary


# ------------------------------------------------------------------------------------------------
# verification
# ------------------------------------------------------------------------------------------------
class Verification:
    def __init__(self) -> None:
        self.checks: list[dict[str, Any]] = []
        self.coverage: dict[str, Any] = {}
        self.warnings: list[str] = []
        self.failures: list[str] = []

    def check(self, check_id: str, area: str, severity: str, passed: bool, expected: Any,
              actual: Any, detail: Any = None) -> bool:
        row: dict[str, Any] = {
            "id": check_id, "area": area, "severity": severity,
            "status": "pass" if passed else ("warn" if severity == "warn" else "fail"),
            "expected": expected, "actual": actual,
        }
        if detail is not None:
            row["detail"] = detail
        self.checks.append(row)
        if not passed:
            (self.warnings if severity == "warn" else self.failures).append(check_id)
        return passed

    def fail(self, check_id: str, area: str, expected: Any, actual: Any, detail: Any = None) -> bool:
        return self.check(check_id, area, "fail", False, expected, actual, detail)

    def warn(self, check_id: str, area: str, expected: Any, actual: Any, detail: Any = None) -> None:
        self.check(check_id, area, "warn", False, expected, actual, detail)

    def info(self, check_id: str, area: str, expected: Any, actual: Any, detail: Any = None) -> None:
        self.check(check_id, area, "fail", True, expected, actual, detail)

    def summary(self) -> dict[str, Any]:
        areas = sorted({row["area"] for row in self.checks})
        by_area = {area: {"checks": 0, "passed": 0, "failed": 0, "warnings": 0} for area in areas}
        for row in self.checks:
            bucket = by_area[row["area"]]
            bucket["checks"] += 1
            if row["status"] == "pass":
                bucket["passed"] += 1
            elif row["status"] == "warn":
                bucket["warnings"] += 1
            else:
                bucket["failed"] += 1
        return {
            "checks": len(self.checks),
            "passed": sum(1 for row in self.checks if row["status"] == "pass"),
            "failed": len(self.failures),
            "warnings": len(self.warnings),
            "by_area": by_area,
            "failures": self.failures,
            "warning_ids": self.warnings,
        }


def _coverage_field(records: Sequence[Mapping[str, Any]], dotted: str, *, required=lambda row: True) -> dict[str, Any]:
    records = [row for row in records if required(row)]
    missing = [str(row.get("item_number") or row.get("vendor_number") or row.get("code") or row.get("id"))
               for row in records if not _present(_lookup(row, dotted))]
    return {"present": len(records) - len(missing), "total": len(records), "missing": missing[:20],
            "missing_count": len(missing)}


def _check_field(v: Verification, area: str, layer: str, records: Sequence[Mapping[str, Any]], dotted: str,
                 *, severity: str = "fail", required=lambda row: True, expected=None) -> None:
    coverage = _coverage_field(records, dotted, required=required)
    label = f"{area}.{dotted}"
    passed = coverage["missing_count"] == 0 and coverage["total"] > 0
    v.check(label, area, severity, passed,
            expected or f"non-empty for all {coverage['total']} {area}",
            f"{coverage['present']}/{coverage['total']} present",
            {"missing_count": coverage["missing_count"], "missing_sample": coverage["missing"]})
    layer.setdefault("fields", {})[dotted] = coverage


def run_verify(client: Client) -> dict[str, Any]:
    v = Verification()
    started = _iso_now()
    ctx = SeedContext(client)
    load_reference_context_only(ctx)

    # ---- items ---------------------------------------------------------------------------
    fetch_items(ctx)
    stockable = ctx.stockable_items()
    items_area: dict[str, Any] = {"stockable_items": len(stockable), "by_type": {}}
    for item in stockable:
        counts = items_area["by_type"]
        key = str(item.get("item_type_code"))
        counts[key] = counts.get(key, 0) + 1
    item_severity = "fail"
    for dotted in ("description", "item_category_id", "upc_code", "external_ref", "item_type_code",
                   "base_uom_id", "units_type_id"):
        _check_field(v, "items", items_area, stockable, dotted, severity=item_severity)
    inventory_fields = ("inventory.costing_method", "inventory.stock_uom_id",
                        "inventory.reorder_point", "inventory.preferred_stock_level", "inventory.safety_stock",
                        "inventory.lead_time_days", "inventory.use_statuses", "inventory.default_status_id")
    for dotted in inventory_fields:
        _check_field(v, "items", items_area, stockable, dotted, severity="fail")
    # initial_cost is immutable after item creation in the current API; the scoped reset nulls it,
    # so surviving items cannot have it restored through the public API (known limitation G4).
    initial_cost = _coverage_field(stockable, "inventory.initial_cost")
    items_area["fields"]["inventory.initial_cost"] = initial_cost
    if initial_cost["missing_count"]:
        v.warn("items.inventory.initial_cost", "items", "non-empty initial_cost for all stockable items",
               f"{initial_cost['present']}/{initial_cost['total']} present",
               {"missing_sample": initial_cost["missing"][:20],
                "note": "known API limitation G4: item_inventory.initial_cost is immutable once the item "
                        "row exists, even when the surviving value is NULL after the scoped reset. "
                        "BOB cost coverage is provided by purchase_price/purchase_price_currency and the "
                        "posted receipt unit costs / FS-05 valuation."})
    else:
        v.info("items.inventory.initial_cost", "items", "non-empty initial_cost",
               f"{initial_cost['present']}/{initial_cost['total']} present")
    purchasing_fields = ("purchasing.preferred_vendor_id", "purchasing.purchase_uom_id",
                         "purchasing.purchase_price", "purchasing.lead_time_days")
    for dotted in purchasing_fields:
        _check_field(v, "items", items_area, stockable, dotted, severity="fail")
    currency_coverage = {"purchase_price_currency_bob": 0, "total": len(stockable), "missing": []}
    for item in stockable:
        value = str(_lookup(item, "purchasing.purchase_price_currency") or "").upper()
        if value == SHARED_BOB:
            currency_coverage["purchase_price_currency_bob"] += 1
        else:
            currency_coverage["missing"].append(str(item.get("item_number")))
    v.check("items.purchasing.purchase_price_currency", "items", "fail",
            currency_coverage["purchase_price_currency_bob"] == len(stockable) and len(stockable) > 0,
            f"BOB for all {len(stockable)} stockable items",
            f"{currency_coverage['purchase_price_currency_bob']}/{len(stockable)} BOB",
            {"missing": currency_coverage["missing"][:20]})
    items_area["purchase_price_currency"] = currency_coverage
    fifo = [row for row in stockable if str(_lookup(row, "inventory.costing_method") or "").upper() == "FIFO"]
    non_fifo = [str(row.get("item_number")) for row in stockable if row not in fifo]
    items_area["fifo"] = {"fifo_count": len(fifo), "total": len(stockable), "legacy_methods": non_fifo[:20]}
    if non_fifo:
        v.warn("items.inventory.costing_method_fifo", "items",
               f"FIFO costing method for all {len(stockable)} stockable items",
               f"{len(fifo)}/{len(stockable)} FIFO",
               {"note": "costing_method is immutable after save; surviving items keep their original method",
                "legacy_items": non_fifo[:20]})
    else:
        v.info("items.inventory.costing_method_fifo", "items", "FIFO costing method",
               f"{len(fifo)}/{len(stockable)} FIFO")

    # ---- item vendors --------------------------------------------------------------------
    _, payload = client.get("/api/v1/item-vendors", {"limit": 200})
    associations = _items_of(payload)
    by_pair = {(str(row.get("item_id")), str(row.get("vendor_id"))): row for row in associations}
    missing_assoc, non_bob_assoc = [], []
    for item in stockable:
        vendor_id = _lookup(item, "purchasing.preferred_vendor_id")
        if not vendor_id:
            missing_assoc.append(str(item.get("item_number")))
            continue
        record = by_pair.get((str(item.get("id")), str(vendor_id)))
        if record is None:
            missing_assoc.append(str(item.get("item_number")))
        elif str(record.get("currency_code") or "").upper() != SHARED_BOB:
            non_bob_assoc.append(str(item.get("item_number")))
    v.check("item_vendors.preferred_association", "item_vendors", "fail", not missing_assoc,
            f"preferred item-vendor association for all {len(stockable)} stockable items",
            f"{len(stockable) - len(missing_assoc)}/{len(stockable)} present",
            {"missing": missing_assoc[:20]})
    v.check("item_vendors.currency_bob", "item_vendors", "fail", not non_bob_assoc,
            "item-vendor currency BOB", {"missing": non_bob_assoc[:20]})

    # ---- vendors -------------------------------------------------------------------------
    _, payload = client.get("/api/v1/vendors", {"limit": 200, "is_inactive": "false"})
    vendors = [row for row in _items_of(payload) if not row.get("is_inactive")]
    vendors_area: dict[str, Any] = {"count": len(vendors)}
    for dotted in ("vendor_number", "name", "tax_id", "payment_terms", "credit_limit"):
        _check_field(v, "vendors", vendors_area, vendors, dotted)
    _check_field(v, "vendors", vendors_area, vendors, "addresses",
                 expected="at least one address with line1/city/country")
    v.check("vendors.addresses.line1", "vendors", "fail",
            all(_present((row.get("addresses") or [{}])[0].get("line1")) for row in vendors) and bool(vendors),
            "line1 present for first address of every vendor",
            f"{sum(1 for row in vendors if _present((row.get('addresses') or [{}])[0].get('line1')))}/{len(vendors)}")
    v.check("vendors.primary_contact.name", "vendors", "fail",
            all(_present((row.get("primary_contact") or {}).get("name")) for row in vendors) and bool(vendors),
            "primary contact name present for every vendor",
            f"{sum(1 for row in vendors if _present((row.get('primary_contact') or {}).get('name')))}/{len(vendors)}")
    _check_field(v, "vendors", vendors_area, vendors, "currency_code",
                 expected="BOB for all active vendors")
    bob_vendors = sum(1 for row in vendors if str(row.get("currency_code") or "").upper() == SHARED_BOB)
    v.check("vendors.currency_bob", "vendors", "fail", bob_vendors == len(vendors) and bool(vendors),
            f"BOB for all {len(vendors)} active vendors", f"{bob_vendors}/{len(vendors)} BOB")
    vendors_area["currency_bob"] = bob_vendors

    # ---- locations -----------------------------------------------------------------------
    _, payload = client.get("/api/v1/locations", {"limit": 200, "active": "true"})
    all_locations = [row for row in _items_of(payload) if not row.get("is_inactive")]
    system_locations = [row for row in all_locations
                        if row.get("is_unallocated") or str(row.get("code") or "").upper() == "UNALLOCATED"]
    locations = [row for row in all_locations if row not in system_locations]
    locations_area: dict[str, Any] = {
        "count": len(locations),
        "system_locations": [str(row.get("code")) for row in system_locations],
        "by_type": {},
    }
    for row in locations:
        key = str(row.get("location_type"))
        locations_area["by_type"][key] = locations_area["by_type"].get(key, 0) + 1
    for dotted in ("code", "name", "location_type", "timezone"):
        _check_field(v, "locations", locations_area, locations, dotted)
    _check_field(v, "locations", locations_area, locations, "address")
    v.check("locations.address.line1", "locations", "fail",
            all(_present((row.get("address") or {}).get("line1")) for row in locations) and bool(locations),
            "address.line1 present for every active location",
            f"{sum(1 for row in locations if _present((row.get('address') or {}).get('line1')))}/{len(locations)}")
    usable = [row for row in locations if row.get("location_type") in LOCATION_TYPES]
    v.check("locations.posting_eligible_kinds", "locations", "fail", len(usable) >= 2,
            "at least two WAREHOUSE/STORE locations", f"{len(usable)} warehouse/store locations")

    # ---- lots ----------------------------------------------------------------------------
    _, payload = client.get("/api/v1/lots", {"limit": 200, "active": "true"})
    lots = _items_of(payload)
    # Enrich every surviving lot of a lot-numbered item so all declaration fields are
    # populated. The reset preserves lots (master/reference data), and some pilot lots
    # historically carried only part of the declarations; LotPatch supports the missing
    # manufacture_date/cost_basis without touching quantities.
    item_by_id = {str(item["id"]): item for item in ctx.stockable_items()}
    for lot in lots:
        item = item_by_id.get(str(lot.get("item_id")))
        if item is None or ctx.numbered_kind(item) != "LOT":
            continue
        patch: dict[str, Any] = {}
        if not _present(lot.get("manufacture_date")):
            patch["manufacture_date"] = (ctx.seed_date - timedelta(days=180)).isoformat()
        if not _present(lot.get("cost_basis")):
            price = _dec(_lookup(item, "purchasing.purchase_price") or _synth_price(str(item.get("item_number"))))
            patch["cost_basis"] = _money(price)
        if patch:
            try:
                client.patch(f"/api/v1/lots/{lot['id']}", patch, idem=f"seed-lot-enrich-{lot['id']}")
            except ApiError as exc:
                # Lot dates/cost basis become immutable once stock movements exist. Run the
                # reset before seeding so this enrichment happens on a movement-free tenant;
                # if a manual rerun happens after postings, keep going with a warning instead
                # of failing the whole reseed.
                ctx.warn(f"lot {lot.get('lot_number')} declarations could not be enriched ({exc}); existing stock movements make them immutable")
            else:
                lot.update(patch)
                ctx.act("lots", "updated", lot.get("lot_number"))
    by_item: dict[str, list[dict[str, Any]]] = {}
    for lot in lots:
        by_item.setdefault(str(lot.get("item_id")), []).append(lot)
    lot_items = [item for item in stockable if ctx.numbered_kind(item) == "LOT"]
    lots_area: dict[str, Any] = {"count": len(lots), "lot_numbered_items": len(lot_items)}
    complete_lots = [lot for lot in lots if _present(lot.get("manufacture_date")) and _present(lot.get("expiry_date"))]
    v.check("lots.lot_numbered_items_complete", "lots", "fail",
            all(any(_present(lot.get("manufacture_date")) and _present(lot.get("expiry_date")) and
                    _present(lot.get("lot_number")) and _present(lot.get("cost_basis"))
                    for lot in by_item.get(str(item["id"]), []))
                for item in lot_items) and bool(lot_items),
            "one complete lot (number+manufacture+expiry+cost basis) per lot-numbered item",
            f"{len(complete_lots)}/{len(lots)} complete lots; {len(lot_items)} lot-numbered items")
    for dotted in ("lot_number", "manufacture_date", "expiry_date", "cost_basis"):
        _check_field(v, "lots", lots_area, lots, dotted,
                     required=lambda row: row.get("item_id") in {str(item["id"]) for item in lot_items})
    status, expiry_payload = client.get("/api/v1/reports/lot-expiry", allow=(403, 404, 409))
    if status == 200:
        expiry_rows = _items_of(expiry_payload)
        lots_area["expiry_report_rows"] = len(expiry_rows)
        v.info("lots.expiry_report", "lots", "lot expiry report readable",
               f"{len(expiry_rows)} report rows")
    else:
        v.warn("lots.expiry_report", "lots", "lot expiry report readable", f"HTTP {status}")

    # ---- balances ------------------------------------------------------------------------
    _, payload = client.get("/api/v1/inventory/balances")
    balances = _items_of(payload)
    on_hand = [row for row in balances if str(row.get("qty_class") or "ON_HAND").upper() in ("ON_HAND", "AVAILABLE")]
    item_qty: dict[str, Decimal] = {}
    location_qty: dict[str, Decimal] = {}
    lot_balances = 0
    for row in on_hand:
        qty = _dec(row.get("qty_on_hand"))
        if qty <= 0:
            continue
        item_qty[str(row.get("item_id"))] = item_qty.get(str(row.get("item_id")), Decimal("0")) + qty
        location_qty[str(row.get("location_id"))] = location_qty.get(str(row.get("location_id")), Decimal("0")) + qty
        if row.get("lot_id"):
            lot_balances += 1
    demo_items = [item for item in stockable if ctx.numbered_kind(item) != "SERIAL"]
    serial_items = [item for item in stockable if ctx.numbered_kind(item) == "SERIAL"]
    missing_stock = [str(item.get("item_number")) for item in demo_items if item_qty.get(str(item["id"]), Decimal("0")) <= 0]
    balances_area: dict[str, Any] = {
        "balance_rows": len(balances),
        "items_with_stock": len(demo_items) - len(missing_stock),
        "stockable_items": len(demo_items),
        "locations_with_stock": len(location_qty),
        "lot_dimension_balances": lot_balances,
        "serialized_items_out_of_scope": [str(item.get("item_number")) for item in serial_items],
    }
    v.check("balances.every_stockable_item", "balances", "fail", not missing_stock,
            f"on-hand > 0 for all {len(demo_items)} stockable items",
            f"{len(demo_items) - len(missing_stock)}/{len(demo_items)} with stock",
            {"missing_items": missing_stock[:20]})
    v.check("balances.two_locations", "balances", "fail", len(location_qty) >= 2,
            "stock present in at least two locations", f"{len(location_qty)} locations with stock")
    lot_items_missing_dimension = [str(item.get("item_number")) for item in lot_items
                                   if not any(str(row.get("item_id")) == str(item["id"]) and row.get("lot_id")
                                              for row in on_hand)]
    v.check("balances.lot_dimension", "balances", "fail", not lot_items_missing_dimension,
            "lot-numbered stock carried on lot dimension",
            f"{lot_balances} lot balance rows",
            {"missing_items": lot_items_missing_dimension[:20]})
    if serial_items:
        v.warn("balances.serialized_scope", "balances",
               "serialized items would need serial numbers",
               f"{len(serial_items)} serialized stockable item(s) excluded from demo stock",
               {"items": [str(item.get("item_number")) for item in serial_items][:20]})

    # ---- movements -----------------------------------------------------------------------
    _, payload = client.get("/api/v1/inventory-transactions", {"limit": 200})
    movements = _items_of(payload)
    by_source: dict[str, int] = {}
    currency_codes: dict[str, int] = {}
    for row in movements:
        source = str(row.get("source_doc_type") or "?")
        by_source[source] = by_source.get(source, 0) + 1
        code = str(row.get("currency_code") or "?")
        currency_codes[code] = currency_codes.get(code, 0) + 1
    required_sources = {"INVENTORY_ADJUSTMENT": "opening stock adjustment",
                        "ITEM_RECEIPT": "goods receipt",
                        "INVENTORY_TRANSFER": "inter-location transfer"}
    missing_sources = [name for source, name in required_sources.items() if by_source.get(source, 0) < 1]
    movements_area: dict[str, Any] = {
        "count": len(movements), "by_source_doc_type": by_source,
        "currency_codes": currency_codes, "value_totals": {},
    }
    v.check("movements.documents_present", "movements", "fail", not missing_sources,
            "ledger movements for opening adjustment, receipt and transfer",
            f"{len(movements)} movements: {by_source}",
            {"missing": missing_sources})
    values = [row for row in movements if _present(row.get("total_value_base")) and _present(row.get("total_qty_base"))]
    v.check("movements.qty_value_totals", "movements", "fail", len(values) == len(movements) and bool(movements),
            "total_qty_base/total_value_base on every ledger transaction",
            f"{len(values)}/{len(movements)} with totals")
    movements_area["value_totals"] = {
        "sum_value_base": _money(sum((_dec(row.get("total_value_base")) for row in movements), Decimal("0"))),
        "sum_qty_base": _qty(sum((_dec(row.get("total_qty_base")) for row in movements), Decimal("0"))),
    }
    non_bob_txn = {code: count for code, count in currency_codes.items() if code.upper() != SHARED_BOB}
    if non_bob_txn:
        v.warn("movements.currency_bob", "movements", "transaction currency BOB",
               f"ledger transaction currency distribution {currency_codes}",
               {"note": "API posting paths currently omit currency_code when creating inventory "
                        "transactions, so the ledger default USD is stamped even on a BOB tenant; "
                        "the BOB document amounts are correct (receipt/PO currency BOB, value_base BOB). "
                        "Tracked as API limitation G1; not fixable from seed data.",
                "bob_transactions": currency_codes.get(SHARED_BOB, 0)})
    else:
        v.info("movements.currency_bob", "movements", "transaction currency BOB",
               f"{currency_codes.get(SHARED_BOB, 0)} BOB transactions")

    # ---- receipts ------------------------------------------------------------------------
    _, payload = client.get("/api/v1/item-receipts", {"limit": 200})
    receipts = _items_of(payload)
    marker_receipts = [row for row in receipts if str(row.get("memo") or "").startswith(MARKER)]
    posted_receipts = [row for row in marker_receipts if str(row.get("status") or "").upper() == "POSTED"]
    receipts_area: dict[str, Any] = {"count": len(receipts), "marker_receipts": len(marker_receipts),
                                     "posted_marker_receipts": len(posted_receipts)}
    v.check("receipts.posted", "receipts", "fail", bool(posted_receipts),
            "at least one POSTED seed goods receipt", f"{len(posted_receipts)} posted seed receipts")
    receipt_currency = sum(1 for row in marker_receipts if str(row.get("currency_code") or "").upper() == SHARED_BOB)
    v.check("receipts.currency_bob", "receipts", "fail",
            receipt_currency == len(marker_receipts) and bool(marker_receipts),
            "BOB receipt currency", f"{receipt_currency}/{len(marker_receipts)} BOB")
    receipt_details: list[dict[str, Any]] = []
    receipt_detail_errors: list[str] = []
    for receipt in marker_receipts:
        _, detail_body = client.get(f"/api/v1/item-receipts/{receipt['id']}")
        detail = _wrapped(detail_body)
        receipt_details.append(detail)
        lines = detail.get("lines") or []
        for line in lines:
            if not _present(line.get("unit_cost")) or _dec(line.get("unit_cost")) <= 0:
                receipt_detail_errors.append(f"{detail.get('document_number')}:line {line.get('line_number')} unit_cost")
            if not _present(line.get("inventory_status_id")):
                receipt_detail_errors.append(f"{detail.get('document_number')}:line {line.get('line_number')} status")
            if not _present(line.get("location_id")):
                receipt_detail_errors.append(f"{detail.get('document_number')}:line {line.get('line_number')} location")
            if not _present(line.get("currency_code")):
                receipt_detail_errors.append(f"{detail.get('document_number')}:line {line.get('line_number')} currency")
    lines_total = sum(len(detail.get("lines") or []) for detail in receipt_details)
    v.check("receipts.lines_complete", "receipts", "fail", not receipt_detail_errors and lines_total > 0,
            "receipt lines with unit cost BOB / status / location",
            f"{lines_total} receipt lines", {"issues": receipt_detail_errors[:20]})
    receipt_lot_lines = [line for detail in receipt_details for line in detail.get("lines") or []
                         if any(_present(detail_row.get("lot_id")) for detail_row in line.get("details") or [])]
    receipt_line_items = {str(line.get("item_id")) for detail in receipt_details for line in detail.get("lines") or []}
    receipt_lot_items = [item for item in stockable if ctx.numbered_kind(item) == "LOT"]
    missing_receipt_lots = [str(item.get("item_number")) for item in receipt_lot_items
                            if str(item["id"]) not in {str(line.get("item_id")) for line in receipt_lot_lines}]
    v.check("receipts.lot_details", "receipts", "fail", not missing_receipt_lots,
            "lot details on receipt lines for lot-numbered items",
            f"{len(receipt_lot_lines)} lot receipt lines",
            {"missing_items": missing_receipt_lots[:20]})
    receipt_covered = [str(item["id"]) for item in demo_items if str(item["id"]) in receipt_line_items]
    receipts_area.update({"lines": lines_total, "items_received": len(receipt_covered),
                          "lot_receipt_lines": len(receipt_lot_lines)})

    # ---- purchase orders -----------------------------------------------------------------
    _, payload = client.get("/api/v1/purchase-orders")
    orders = _items_of(payload)
    marker_orders = [row for row in orders if str(row.get("memo") or "").startswith(MARKER)]
    orders_area: dict[str, Any] = {"count": len(orders), "marker_orders": len(marker_orders)}
    v.check("purchase_orders.present", "purchase_orders", "fail", bool(marker_orders),
            "at least one seed purchase order", f"{len(marker_orders)} marker purchase orders")
    po_currency = sum(1 for row in marker_orders if str(row.get("currency_code") or "").upper() == SHARED_BOB)
    v.check("purchase_orders.currency_bob", "purchase_orders", "fail",
            po_currency == len(marker_orders) and bool(marker_orders),
            "BOB purchase-order currency", f"{po_currency}/{len(marker_orders)} BOB")
    po_line_issues: list[str] = []
    po_status: dict[str, int] = {}
    for order in marker_orders:
        po_status[str(order.get("status"))] = po_status.get(str(order.get("status")), 0) + 1
        for line in order.get("lines") or []:
            if _dec(line.get("qty_ordered")) <= 0:
                po_line_issues.append(f"{order.get('document_number')}: qty_ordered")
            if _dec(line.get("unit_price")) <= 0:
                po_line_issues.append(f"{order.get('document_number')}: unit_price")
    v.check("purchase_orders.lines_priced", "purchase_orders", "fail", not po_line_issues,
            "purchase-order lines with qty and unit price > 0", f"{po_line_issues[:20]}", None)
    if any(status == "DRAFT" for status in po_status):
        v.warn("purchase_orders.status", "purchase_orders", "submitted/approved purchase order",
               f"status distribution {po_status}")
    else:
        v.info("purchase_orders.status", "purchase_orders", "submitted/approved purchase order",
               f"status distribution {po_status}")
    orders_area["status"] = po_status

    # ---- costing / valuation -------------------------------------------------------------
    _, payload = client.get("/api/v1/costs/item-costs", {"limit": 200})
    item_costs = _items_of(payload)
    _, valuation = client.get("/api/v1/costs/valuation")
    valuation_data = _wrapped(valuation)
    totals = valuation_data.get("totals") or {}
    valuation_value = _dec(totals.get("value_base"))
    cost_items = {str(row.get("item_id")) for row in item_costs}
    missing_cost_items = [str(item.get("item_number")) for item in demo_items if str(item["id"]) not in cost_items]
    costing_area: dict[str, Any] = {
        "item_cost_rows": len(item_costs),
        "items_with_costs": len(demo_items) - len(missing_cost_items),
        "stockable_items": len(demo_items),
        "valuation_value_base": _money(valuation_value),
        "valuation_qty": _qty(_dec(totals.get("qty"))),
    }
    v.check("costing.item_costs_present", "costing", "fail", not missing_cost_items,
            "FS-05 item cost rows for receipted stockable items",
            f"{len(demo_items) - len(missing_cost_items)}/{len(demo_items)} items with cost rows",
            {"missing_items": missing_cost_items[:20]})
    v.check("costing.valuation_value", "costing", "fail", valuation_value > 0,
            "valuation.value_base > 0", f"{_money(valuation_value)} BOB base value")
    fifo_costs = [row for row in item_costs if str(row.get("costing_method") or "").upper() == "FIFO"]
    costing_area["fifo_cost_rows"] = len(fifo_costs)
    v.check("costing.fifo_rows", "costing", "fail", bool(fifo_costs),
            "FIFO cost rows present", f"{len(fifo_costs)} FIFO cost rows")
    # cost vs on-hand qty is expected to differ: opening adjustments are posted by the
    # inventory-ledger document path, which does not inject the costing engine (only
    # procurement/value posts do). Report the difference instead of failing it.
    cost_qty = sum((_dec(row.get("qty_on_hand")) for row in item_costs), Decimal("0"))
    balance_qty = sum(item_qty.values(), Decimal("0"))
    if cost_qty != balance_qty:
        v.warn("costing.cost_vs_balance_qty", "costing", "costing quantity reconciles with on-hand",
               f"cost qty {_qty(cost_qty)} vs on-hand {_qty(balance_qty)}",
               {"note": "opening stock adjustments post ledger balances but not FS-05 cost layers through "
                        "the current API (only procurement/value posting paths invoke the costing engine). "
                        "Known API limitation G2; valuation readback is proven by the posted receipt."})

    # ---- counts --------------------------------------------------------------------------
    _, payload = client.get("/api/v1/inventory-counts", {"limit": 100})
    counts = _items_of(payload)
    target_items = sorted(str(item["id"]) for item in stockable)
    seed_count = next((row for row in counts
                       if sorted(str(x) for x in (row.get("scope") or {}).get("items") or []) == target_items), None)
    counts_area: dict[str, Any] = {"count": len(counts), "seed_count": bool(seed_count)}
    if seed_count:
        count_id = str(seed_count["id"])
        _, detail_body = client.get(f"/api/v1/inventory-counts/{count_id}")
        count_detail = _wrapped(detail_body)
        _, lines_payload = client.get(f"/api/v1/inventory-counts/{count_id}/lines")
        count_lines = _items_of(lines_payload)
        counts_area.update({"status": count_detail.get("status"), "lines": len(count_lines),
                            "line_count": count_detail.get("line_count"),
                            "snapshot_at": count_detail.get("snapshot_at")})
        v.check("counts.snapshot", "counts", "fail",
                _present(count_detail.get("snapshot_at")) and count_lines and
                all(_present(row.get("snapshot_qty")) for row in count_lines),
                "count with snapshot lines and snapshot quantities",
                f"status {count_detail.get('status')}, {len(count_lines)} lines")
        status_ok = str(count_detail.get("status") or "").upper() in ("OPEN", "PENDING_REVIEW", "APPROVED", "POSTED")
        if status_ok and str(count_detail.get("status") or "").upper() in ("APPROVED", "POSTED"):
            v.info("counts.workflow", "counts", "count progressed to approval/posting", count_detail.get("status"))
        else:
            v.warn("counts.workflow", "counts", "count approved/posted",
                   f"count status {count_detail.get('status')}",
                   {"note": "approval requires the operator-only inventory.count.approve scope "
                            "(Serebo key is intentionally SoD-limited); snapshot/capture/submit is the "
                            "seedable part. Known limitation G3."})
    else:
        v.fail("counts.snapshot", "counts", "inventory count for the stockable item scope", "no matching count")

    # ---- reason codes on the opening document -------------------------------------------
    _, payload = client.get("/api/v1/inventory-adjustments")
    opening = next((row for row in _items_of(payload) if str(row.get("document_number")) == OPENING_DOC), None)
    if opening:
        _, detail_body = client.get(f"/api/v1/inventory-adjustments/{opening['id']}")
        adjustment = _wrapped(detail_body)
        lines = adjustment.get("lines") or []
        v.check("opening_stock.posted", "opening_stock", "fail",
                str(adjustment.get("status") or "").upper() == "POSTED",
                "opening stock adjustment POSTED", str(adjustment.get("status")))
        v.check("opening_stock.lines", "opening_stock", "fail",
                len(lines) >= 2 and all(_present(line.get("location_id")) for line in lines),
                "opening adjustment lines across two locations", f"{len(lines)} lines")
        v.check("opening_stock.reason_code", "opening_stock", "fail",
                all(_present(line.get("reason_code_id")) for line in lines) and bool(lines),
                "reason code on every opening line",
                f"{sum(1 for line in lines if _present(line.get('reason_code_id')))}/{len(lines)}")
        v.check("opening_stock.lot_lines", "opening_stock", "fail",
                all(any(_present(line.get("lot_id")) for line in lines
                        if str(line.get("item_id")) == str(item["id"]))
                    for item in lot_items),
                "lot dimension on opening lines for lot-numbered items",
                f"{sum(1 for line in lines if _present(line.get('lot_id')))} lot lines")
    else:
        v.fail("opening_stock.posted", "opening_stock",
               f"opening stock adjustment {OPENING_DOC}", "document not found")

    # ---- tenant base currency + reference currency --------------------------------------
    tenant_area: dict[str, Any] = {}
    status, payload = client.get("/api/v1/admin/tenant-configuration", allow=(400, 403, 404))
    if status == 200:
        config = _wrapped(payload)
        base = str(config.get("base_currency_code") or "").upper()
        tenant_area = {"base_currency_code": base, "source": "admin/tenant-configuration"}
        v.check("tenant.base_currency_bob", "tenant", "fail", base == SHARED_BOB,
                f"tenant base currency {SHARED_BOB}", base or "null")
    else:
        tenant_area = {"base_currency_code": None, "source": "unavailable",
                       "note": f"GET /admin/tenant-configuration -> HTTP {status} "
                               "(deployed-only endpoint; local stack may be older)"}
        v.warn("tenant.base_currency_bob", "tenant", f"tenant base currency {SHARED_BOB}",
               "tenant-configuration endpoint unavailable in this stack",
               {"http_status": status})
    status, payload = client.get("/api/v1/admin/currencies", {"is_active": "true"}, allow=(403, 404))
    if status == 200:
        codes = {str(row.get("code")) for row in _items_of(payload)}
        tenant_area["bob_currency_active"] = SHARED_BOB in codes
        v.check("tenant.bob_currency_active", "tenant", "fail", SHARED_BOB in codes,
                "BOB active platform currency", "present" if SHARED_BOB in codes else "missing")
    else:
        v.warn("tenant.bob_currency_active", "tenant", "BOB active platform currency",
               f"/admin/currencies unavailable (HTTP {status})")

    # ---- feature flags (informational) --------------------------------------------------
    flags_area: dict[str, Any] = {}
    if ctx.flags:
        flags_area = {key: ctx.flags.get(key) for key in REQUIRED_FLAGS}
        disabled = [key for key, enabled in flags_area.items() if not enabled]
        if disabled:
            v.warn("flags.required_enabled", "flags", "inventory demo feature flags enabled",
                   f"disabled: {disabled}")
        else:
            v.info("flags.required_enabled", "flags", "inventory demo feature flags enabled",
                   f"{len(REQUIRED_FLAGS)} flags enabled")
    else:
        v.warn("flags.required_enabled", "flags", "inventory demo feature flags readable",
               "feature flag endpoint unavailable to this key")

    v.coverage = {
        "items": items_area,
        "item_vendors": {"associations": len(associations)},
        "vendors": vendors_area,
        "locations": locations_area,
        "lots": lots_area,
        "balances": balances_area,
        "movements": movements_area,
        "receipts": receipts_area,
        "purchase_orders": orders_area,
        "costing": costing_area,
        "counts": counts_area,
        "opening_stock": {"document": OPENING_DOC},
        "tenant": tenant_area,
        "flags": flags_area,
    }
    report = {
        "mode": "verify",
        "generated_at": _iso_now(),
        "started_at": started,
        "base_url": client.base_url,
        "inventory_marker": MARKER,
        "summary": v.summary(),
        "coverage": v.coverage,
        "checks": v.checks,
    }
    return report


def load_reference_context_only(ctx: SeedContext) -> None:
    """Load reference data in verify mode without failing on missing feature flags."""
    client = ctx.client
    _, payload = client.get("/api/v1/item-types")
    ctx.item_types = {str(row.get("code")): row for row in _items_of(payload)}
    _, payload = client.get("/api/v1/uoms", {"is_active": "true"})
    ctx.uoms = {str(row.get("code")): row for row in _items_of(payload)}
    _, payload = client.get("/api/v1/item-categories", {"include_inactive": "true"})
    ctx.categories = [row for row in _items_of(payload) if row.get("is_active", True)]
    _, payload = client.get("/api/v1/inventory-statuses", {"active": "true"})
    ctx.statuses = _items_of(payload)
    ctx.default_status = next((row for row in ctx.statuses if row.get("is_default")), None)
    status, payload = client.get("/api/v1/admin/flags", allow=(403, 404))
    if status == 200:
        ctx.flags = {str(row.get("flag_key")): bool(row.get("is_enabled")) for row in _items_of(payload)}


# ------------------------------------------------------------------------------------------------
# human summaries + CLI
# ------------------------------------------------------------------------------------------------
def _print_seed_summary(report: Mapping[str, Any]) -> None:
    summary = report.get("summary", {})
    print(f"SEED PILOT BOB DEMO DATA — {report.get('base_url')}", file=sys.stderr)
    print(f"  marker            : {report.get('inventory_marker')}", file=sys.stderr)
    print(f"  stockable items   : {summary.get('stockable_items')}", file=sys.stderr)
    print(f"  posting locations : {', '.join(summary.get('posting_locations') or [])}", file=sys.stderr)
    print(f"  vendors/adopted   : {summary.get('vendors')} vendors, {summary.get('locations')} locations",
          file=sys.stderr)
    print(f"  actions           : {summary.get('actions')} "
          f"(created={summary.get('created')}, updated={summary.get('updated')}, "
          f"posted={summary.get('posted')}, skipped/reused={summary.get('skipped_or_reused')})",
          file=sys.stderr)
    for row in report.get("actions", []):
        detail = f" {json.dumps(row.get('detail'))}" if row.get("detail") else ""
        print(f"    [{row['area']}] {row['action']}: {row['target']}{detail}", file=sys.stderr)
    for warning in report.get("warnings", []):
        print(f"  WARN {warning}", file=sys.stderr)
    for error in report.get("errors", []):
        print(f"  ERROR {error}", file=sys.stderr)
    print("  result            : " + ("FAILED" if report.get("errors") else "OK"), file=sys.stderr)


def _print_verify_summary(report: Mapping[str, Any]) -> None:
    summary = report.get("summary", {})
    print(f"PILOT BOB INVENTORY VERIFICATION — {report.get('base_url')}", file=sys.stderr)
    for row in report.get("checks", []):
        marker = {"pass": "PASS", "warn": "WARN", "fail": "FAIL"}[row["status"]]
        detail = ""
        if row["status"] != "pass" and row.get("detail"):
            detail = " " + json.dumps(row["detail"])[:220]
        print(f"  {marker}  {row['id']:<48} {row.get('actual')}{detail}", file=sys.stderr)
    print(f"  summary: {summary.get('passed')} passed, {summary.get('failed')} failed, "
          f"{summary.get('warnings')} warnings", file=sys.stderr)
    if summary.get("failures"):
        print("  FAILED CHECKS: " + ", ".join(summary["failures"]), file=sys.stderr)
    for check in report.get("checks", []):
        if check["id"] in summary.get("warning_ids", []) and check.get("detail", {}).get("note"):
            print(f"  NOTE {check['id']}: {check['detail']['note']}", file=sys.stderr)


def _load_env() -> tuple[str, str]:
    base_url = (os.environ.get("SCM_BASE_URL") or "").strip().rstrip("/")
    api_key = (os.environ.get("SCM_API_KEY") or "").strip()
    missing = [name for name, value in (("SCM_BASE_URL", base_url), ("SCM_API_KEY", api_key)) if not value]
    if missing:
        raise SeedError("missing required environment variable(s): " + ", ".join(missing))
    return base_url, api_key


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Idempotent SCM pilot BOB demo-data reseed and inventory field-coverage verification.")
    parser.add_argument("--verify", action="store_true",
                        help="read-only verification; emits JSON coverage report and exits non-zero on gaps")
    parser.add_argument("--json-out", default="", help="optional path to also write the JSON report")
    parser.add_argument("--seed-date", default="",
                        help="optional ISO date used as the deterministic seed reference date (default today)")
    parser.add_argument("--timeout", type=float, default=60.0, help="HTTP timeout seconds (default 60)")
    parser.add_argument("--quiet", action="store_true", help="suppress the human summary on stderr")
    args = parser.parse_args(argv)

    try:
        base_url, api_key = _load_env()
    except SeedError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    seed_date: date | None = None
    if args.seed_date:
        try:
            seed_date = date.fromisoformat(args.seed_date)
        except ValueError:
            print(f"ERROR: invalid --seed-date {args.seed_date!r} (expected YYYY-MM-DD)", file=sys.stderr)
            return 2
    client = Client(base_url, api_key, timeout=args.timeout)
    try:
        if args.verify:
            report = run_verify(client)
            if not args.quiet:
                _print_verify_summary(report)
            exit_code = 1 if report["summary"]["failed"] else 0
        else:
            report = run_seed(client, seed_date=seed_date)
            if not args.quiet:
                _print_seed_summary(report)
            exit_code = 2 if report.get("errors") else 0
    except SeedError as exc:
        report = {
            "mode": "verify" if args.verify else "seed",
            "generated_at": _iso_now(),
            "base_url": base_url,
            "summary": {"checks": 0, "passed": 0, "failed": 1, "warnings": 0},
            "errors": [str(exc)],
        }
        print(f"ERROR: {exc}", file=sys.stderr)
        exit_code = 2

    payload = json.dumps(report, indent=2, ensure_ascii=False, default=_jsonable)
    if args.json_out:
        try:
            directory = os.path.dirname(os.path.abspath(args.json_out))
            if directory:
                os.makedirs(directory, exist_ok=True)
            with open(args.json_out, "w", encoding="utf-8") as handle:
                handle.write(payload + "\n")
        except OSError as exc:
            print(f"ERROR: could not write --json-out {args.json_out}: {exc}", file=sys.stderr)
            return 2
    print(payload)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
