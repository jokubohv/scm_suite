"""Pure WMS strategy engine (FS-11 §5): putaway/pick resolution, FEFO/FIFO ordering, pick roll-up.

The service loads candidate bins/positions/blocks from the database and calls these deterministic
functions; unit tests cover the rules without a database. No SQLAlchemy imports here.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import date
from typing import Any

from app.modules.wms.errors import ValidationFailed

SORT_DIRECTIONS = ("FIFO", "LIFO", "FEFO", "BIN_SEQUENCE")


def is_expired(expiry_date: date | None, *, as_of: date) -> bool:
    """A lot is expired strictly after its expiry date (expiry date itself is still pickable)."""
    if expiry_date is None:
        return False
    return expiry_date < as_of


def _bin_code(row: Mapping[str, Any]) -> str:
    return str(row.get("bin_code") or row.get("code") or "")


def _lot_created(row: Mapping[str, Any]) -> Any:
    return row.get("lot_created_at") or row.get("created_at")


def _lot_expiry(row: Mapping[str, Any]) -> date | None:
    return row.get("expiry_date") or row.get("lot_expiry_date")


def sort_positions(
    positions: Sequence[Mapping[str, Any]], *, direction: str = "FIFO", descending: bool = False
) -> list[Mapping[str, Any]]:
    """Deterministically order pick positions by the documented strategy sort direction."""
    normalised = str(direction or "BIN_SEQUENCE").upper()
    if normalised not in SORT_DIRECTIONS:
        raise ValidationFailed(f"unknown sort direction {direction!r}")
    if normalised == "BIN_SEQUENCE":
        return sorted(positions, key=lambda row: (_bin_code(row), str(row.get("id"))), reverse=descending)
    if normalised == "FEFO":
        return sorted(
            positions,
            key=lambda row: (
                _lot_expiry(row) is None,
                _lot_expiry(row) or date.max,
                _bin_code(row),
                str(row.get("id")),
            ),
            reverse=descending,
        )
    created_key = lambda row: (str(_lot_created(row) or ""), _bin_code(row), str(row.get("id")))  # noqa: E731
    newest_first = normalised == "LIFO"
    return sorted(positions, key=created_key, reverse=newest_first ^ bool(descending))


def filter_blocked(positions: Iterable[Mapping[str, Any]], blocked_bin_ids: Iterable[Any]) -> list[Mapping[str, Any]]:
    blocked = {str(value) for value in blocked_bin_ids}
    return [row for row in positions if str(row.get("bin_id")) not in blocked]


def filter_expired(
    positions: Iterable[Mapping[str, Any]], *, as_of: date, guard_enabled: bool = True
) -> tuple[list[Mapping[str, Any]], list[Mapping[str, Any]]]:
    """Split positions into (pickable, expired-exceptions) when the pick-expired guard is enabled."""
    if not guard_enabled:
        return list(positions), []
    pickable: list[Mapping[str, Any]] = []
    expired: list[Mapping[str, Any]] = []
    for row in positions:
        if is_expired(_lot_expiry(row), as_of=as_of):
            expired.append(row)
        else:
            pickable.append(row)
    return pickable, expired


def match_putaway_rule(
    rule: Mapping[str, Any],
    *,
    item_process_group_id: Any,
    zone_id: Any,
    bin_type: str | None,
) -> bool:
    """A putaway rule matches when its scoped constraints are unset or equal the candidate position."""
    rule_group = rule.get("item_process_group_id")
    rule_zone = rule.get("zone_id")
    rule_bin_type = rule.get("bin_type")
    if rule_group is not None and str(rule_group) != str(item_process_group_id or ""):
        return False
    if rule_zone is not None and str(rule_zone) != str(zone_id or ""):
        return False
    if rule_bin_type is not None and str(rule_bin_type).upper() != str(bin_type or "").upper():
        return False
    return True


def resolve_putaway_bin(
    rules: Sequence[Mapping[str, Any]],
    candidate_bins: Sequence[Mapping[str, Any]],
    *,
    item_process_group_id: Any,
    qty: Any,
    on_hand_by_bin: Mapping[str, Any] | None = None,
) -> Mapping[str, Any] | None:
    """First matching rule (by ``sequence``) then the first candidate bin that can hold ``qty``.

    Candidates are expected ordered by zone priority/bin sequence by the caller. ``max_qty_per_bin`` is
    enforced against the current on-hand projection.
    """
    on_hand = {str(key): value for key, value in (on_hand_by_bin or {}).items()}
    ordered_rules = sorted(rules, key=lambda rule: int(rule.get("sequence") or 0))
    for rule in ordered_rules:
        if rule.get("is_active") is False:
            continue
        limit = rule.get("max_qty_per_bin")
        for candidate in candidate_bins:
            if not match_putaway_rule(
                rule,
                item_process_group_id=item_process_group_id,
                zone_id=candidate.get("zone_id"),
                bin_type=candidate.get("bin_type"),
            ):
                continue
            if limit is not None:
                current = on_hand.get(str(candidate.get("bin_id")), 0)
                if _as_decimal(current) + _as_decimal(qty) > _as_decimal(limit):
                    continue
            return candidate
    return None


def _as_decimal(value: Any) -> Any:
    from decimal import Decimal

    if isinstance(value, Decimal):
        return value
    return Decimal(str(value or 0))


def decompose_pick_tasks(
    lines: Sequence[Mapping[str, Any]], *, prefer_zone_merge: bool = True
) -> list[dict[str, Any]]:
    """Deterministic pick-task decomposition (DM-WMS-07 input): one task per (source_type, source_ref).

    Zone merge (multi-order/zone picking) is an optimisation: when ``prefer_zone_merge`` is set and every
    line carries a zone, lines with the same zone are grouped; otherwise grouping is per source document.
    """
    groups: dict[tuple[str, ...], list[Mapping[str, Any]]] = {}
    for index, line in enumerate(lines):
        source_type = str(line.get("source_type") or "SALES_ORDER").upper()
        key: tuple[str, ...]
        if prefer_zone_merge and line.get("zone_id"):
            key = ("ZONE", str(line["zone_id"]))
        else:
            key = (source_type, str(line.get("source_ref") or f"wave-{index}"))
        groups.setdefault(key, []).append(line)
    tasks: list[dict[str, Any]] = []
    for key, grouped in groups.items():
        tasks.append(
            {
                "task_type": "ORDER" if key[0] != "WORK_ORDER" else "WORK_ORDER",
                "group_key": list(key),
                "lines": list(grouped),
            }
        )
    return tasks


def pick_quantities(requested: Any, picked: Any) -> tuple[Any, Any]:
    """Validate qty_picked ≤ qty_requested and return (picked, short) decimals."""
    from decimal import Decimal, InvalidOperation

    try:
        requested_qty = Decimal(str(requested))
        picked_qty = Decimal(str(picked))
    except (InvalidOperation, ValueError) as exc:  # pragma: no cover - router validates types first
        raise ValidationFailed("pick quantities must be decimals") from exc
    if requested_qty <= 0:
        raise ValidationFailed("qty_requested must be > 0")
    if picked_qty < 0 or picked_qty > requested_qty:
        raise ValidationFailed("qty_picked must be between 0 and qty_requested")
    return picked_qty, requested_qty - picked_qty


def assign_worker(
    strategy: str,
    *,
    candidates: Sequence[Mapping[str, Any]],
    open_counts: Mapping[str, int] | None = None,
) -> Mapping[str, Any] | None:
    """Queue assignment: round-robin rotation marker, least-open workload, or manual (returns None)."""
    normalised = str(strategy or "MANUAL").upper()
    if not candidates:
        return None
    if normalised == "ROUND_ROBIN":
        return candidates[0]
    if normalised == "LEAST_OPEN":
        counts = {str(key): value for key, value in (open_counts or {}).items()}
        return min(candidates, key=lambda candidate: (counts.get(str(candidate.get("id")), 0), str(candidate.get("id"))))
    return None


__all__ = [
    "SORT_DIRECTIONS",
    "assign_worker",
    "decompose_pick_tasks",
    "filter_blocked",
    "filter_expired",
    "is_expired",
    "match_putaway_rule",
    "pick_quantities",
    "resolve_putaway_bin",
    "sort_positions",
]
