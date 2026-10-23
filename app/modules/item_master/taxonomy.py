"""FS-01 item-type taxonomy: 24 seeded system types, flags and posting/document eligibility.

Pure module (no DB/network). DB rows are seeded per tenant from :data:`ITEM_TYPE_SEEDS`; the service
falls back to the SYSTEM-tenant seed catalogue and lazily provisions tenant rows on first use.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class PostingOperation(StrEnum):
    POST = "POST"  # inventory on-hand posting (ledger)
    PO = "PO"  # purchase order line
    SO = "SO"  # sales order / fulfilment line
    WMS = "WMS"  # warehouse execution
    BUILD = "BUILD"  # manufacturing build/unbuild


@dataclass(frozen=True, slots=True)
class ItemTypeSeed:
    code: str
    name: str
    can_stock: bool = False
    requires_bom: bool = False
    requires_numbering: bool = False
    is_charge_only: bool = False
    sort_order: int = 100
    purchasable: bool = True
    sellable: bool = True


#: 24 documented item types (DATA-MODEL §4.3; flags per FS-01 §4.3 proposal).
ITEM_TYPE_SEEDS: tuple[ItemTypeSeed, ...] = (
    ItemTypeSeed("INVENTORY", "Inventory Item", can_stock=True, sort_order=10),
    ItemTypeSeed("SERIALIZED", "Serialized Item", can_stock=True, requires_numbering=True, sort_order=20),
    ItemTypeSeed("LOT_NUMBERED", "Lot Numbered Item", can_stock=True, requires_numbering=True, sort_order=30),
    ItemTypeSeed("ASSEMBLY", "Assembly / Bill of Materials", can_stock=True, requires_bom=True, sort_order=40),
    ItemTypeSeed("WIP", "Work in Process", can_stock=True, requires_bom=True, sort_order=50, sellable=False),
    ItemTypeSeed("PHANTOM", "Phantom Assembly", requires_bom=True, sort_order=60, purchasable=False, sellable=False),
    ItemTypeSeed("OUTSOURCED", "Outsourced Manufacturing", can_stock=True, requires_bom=True, sort_order=70),
    ItemTypeSeed("MATRIX_PARENT", "Matrix Parent", sort_order=80, purchasable=False, sellable=False),
    ItemTypeSeed("MATRIX_CHILD", "Matrix Child", can_stock=True, sort_order=90),
    ItemTypeSeed("KIT", "Kit / Package", sort_order=100, purchasable=False),
    ItemTypeSeed("GROUP", "Item Group", sort_order=110, purchasable=False),
    ItemTypeSeed("NON_INVENTORY", "Non-inventory Item", sort_order=120),
    ItemTypeSeed("DROP_SHIP", "Drop Ship Item", sort_order=130),
    ItemTypeSeed("SPECIAL_ORDER", "Special Order Item", sort_order=140),
    ItemTypeSeed("SERVICE", "Service", sort_order=150),
    ItemTypeSeed("DOWNLOAD", "Download Item", sort_order=160),
    ItemTypeSeed("GIFT_CERTIFICATE", "Gift Certificate", sort_order=170),
    ItemTypeSeed("EXPENSE", "Expense Item", is_charge_only=True, sort_order=180, purchasable=False, sellable=False),
    ItemTypeSeed("OTHER_CHARGE", "Other Charge", is_charge_only=True, sort_order=190, purchasable=False, sellable=False),
    ItemTypeSeed("DESCRIPTION", "Description Line", is_charge_only=True, sort_order=200, purchasable=False, sellable=False),
    ItemTypeSeed("DISCOUNT", "Discount Line", is_charge_only=True, sort_order=210, purchasable=False, sellable=False),
    ItemTypeSeed("MARKUP", "Markup Line", is_charge_only=True, sort_order=220, purchasable=False, sellable=False),
    ItemTypeSeed("SUBTOTAL", "Subtotal Line", is_charge_only=True, sort_order=230, purchasable=False, sellable=False),
    ItemTypeSeed("PAYMENT", "Payment Line", is_charge_only=True, sort_order=240, purchasable=False, sellable=False),
)

ITEM_TYPE_BY_CODE: dict[str, ItemTypeSeed] = {seed.code: seed for seed in ITEM_TYPE_SEEDS}
ITEM_TYPE_CODES: tuple[str, ...] = tuple(ITEM_TYPE_BY_CODE)


@dataclass(frozen=True, slots=True)
class Eligibility:
    eligible: bool
    code: str | None = None
    reasons: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {"eligible": self.eligible, "code": self.code, "reasons": list(self.reasons)}


def item_type_seed(code: str) -> ItemTypeSeed:
    try:
        return ITEM_TYPE_BY_CODE[str(code).upper()]
    except KeyError as exc:
        raise KeyError(f"unknown item type {code!r}") from exc


def posting_eligibility(item_type_code: str, operation: PostingOperation | str, *, is_active: bool = True) -> Eligibility:
    """Derive posting/document eligibility from taxonomy flags + code class (RULE-ITEM-04)."""
    seed = item_type_seed(item_type_code)
    op = PostingOperation(str(operation).upper())
    reasons: list[str] = []
    if not is_active:
        reasons.append("INACTIVE_ITEM")
    if seed.is_charge_only:
        reasons.append("CHARGE_ONLY_TYPE")
    elif op is PostingOperation.POST:
        if not seed.can_stock:
            reasons.append("NON_STOCKABLE_TYPE")
    elif op is PostingOperation.PO:
        if not seed.purchasable:
            reasons.append("NOT_PURCHASABLE")
        if seed.is_charge_only:
            reasons.append("CHARGE_ONLY_TYPE")
    elif op is PostingOperation.SO:
        if not seed.sellable:
            reasons.append("NOT_SELLABLE")
    elif op is PostingOperation.WMS:
        if not seed.can_stock:
            reasons.append("NON_STOCKABLE_TYPE")
        if seed.code in {"KIT", "GROUP", "MATRIX_PARENT", "PHANTOM"}:
            reasons.append("NON_SCANNABLE_TYPE")
    elif op is PostingOperation.BUILD:
        if not seed.requires_bom:
            reasons.append("NO_BOM_TYPE")
    code = "ELIGIBLE" if not reasons else (reasons[0] if reasons else None)
    return Eligibility(eligible=not reasons, code=code, reasons=tuple(reasons))


def is_stockable(item_type_code: str) -> bool:
    return item_type_seed(item_type_code).can_stock


def is_charge_only(item_type_code: str) -> bool:
    return item_type_seed(item_type_code).is_charge_only


__all__ = [
    "ITEM_TYPE_BY_CODE",
    "ITEM_TYPE_CODES",
    "ITEM_TYPE_SEEDS",
    "Eligibility",
    "ItemTypeSeed",
    "PostingOperation",
    "is_charge_only",
    "is_stockable",
    "item_type_seed",
    "posting_eligibility",
]
