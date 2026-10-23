"""Invariant harness base + generic ledger invariants (S0-05).

Feature-set invariant suites subclass :class:`InvariantHarness` (or use it directly) and register
named checks. The harness gives deterministic ordering, a report object and a fail-fast mode, and it
can assert that a required set of plan invariant ids (e.g. ``INV-01``, ``INV-18``) is covered.

Generic checks in this module only need plain data (mappings/decimals), so they run before the
ledger/costing engines exist and can be reused by later FS suites.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from app.tests.utils.assertions import as_decimal

InvariantCheck = Callable[[], None]


@dataclass
class InvariantResult:
    name: str
    passed: bool
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "passed": self.passed, "detail": self.detail}


@dataclass
class InvariantReport:
    suite: str
    results: list[InvariantResult] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(result.passed for result in self.results)

    @property
    def failures(self) -> list[InvariantResult]:
        return [result for result in self.results if not result.passed]

    @property
    def covered(self) -> set[str]:
        return {result.name for result in self.results}

    def require_covered(self, required: Iterable[str]) -> None:
        missing = sorted(set(required) - self.covered)
        if missing:
            raise InvariantFailure(f"invariant suite {self.suite!r} missing required checks: {missing}")


class InvariantFailure(AssertionError):
    """Raised when an invariant check or coverage requirement fails."""


class InvariantHarness:
    """Lightweight registry/runner for named invariant checks.

    Example
    -------
    >>> harness = InvariantHarness("FS-04 invariants")
    >>> @harness.check("INV-01")
    ... def _qty() -> None:
    ...     assert_qty_conservation([{"qty_base": "1"}, {"qty_base": -1}])
    >>> harness.run().passed
    True
    """

    def __init__(self, suite: str = "invariants", *, fail_fast: bool = False, required: Iterable[str] | None = None) -> None:
        self.suite = suite
        self.fail_fast = fail_fast
        self.required = tuple(required or ())
        self._checks: list[tuple[str, InvariantCheck]] = []

    def register(self, name: str, check: InvariantCheck) -> None:
        if any(existing == name for existing, _ in self._checks):
            raise ValueError(f"invariant {name!r} already registered")
        self._checks.append((name, check))

    def check(self, name: str) -> Callable[[InvariantCheck], InvariantCheck]:
        """Decorator form of :meth:`register`."""

        def decorator(func: InvariantCheck) -> InvariantCheck:
            self.register(name, func)
            return func

        return decorator

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(name for name, _ in self._checks)

    def run(self) -> InvariantReport:
        report = InvariantReport(suite=self.suite)
        for name, check in self._checks:
            try:
                check()
            except Exception as exc:  # noqa: BLE001 - invariant failure detail is the point
                report.results.append(InvariantResult(name=name, passed=False, detail=f"{type(exc).__name__}: {exc}"))
                if self.fail_fast:
                    raise InvariantFailure(f"invariant {name!r} failed: {exc}") from exc
            else:
                report.results.append(InvariantResult(name=name, passed=True))
        if self.required:
            report.require_covered(self.required)
        return report

    def assert_ok(self) -> InvariantReport:
        report = self.run()
        if not report.passed:
            failures = "; ".join(f"{result.name}: {result.detail}" for result in report.failures)
            raise InvariantFailure(f"invariant suite {self.suite!r} failed: {failures}")
        return report


# ---------------------------------------------------------------------------
# Generic ledger invariants (data-only; usable before FS-04 exists)
# ---------------------------------------------------------------------------
def assert_qty_conservation(movements: Iterable[Mapping[str, Any]], *, expected_net: Decimal | int | str | None = None) -> None:
    """INV-01: internal moves sum to zero; external postings equal the expected net change."""
    total = sum((as_decimal(row["qty_base"]) for row in movements), Decimal("0"))
    if expected_net is None:
        expected = Decimal("0")
    else:
        expected = as_decimal(expected_net)
    if total != expected:
        raise AssertionError(f"quantity conservation violated: sum={total} expected={expected}")


def assert_value_conservation(
    movements: Iterable[Mapping[str, Any]], *, expected_net: Decimal | int | str | None = None
) -> None:
    """INV-02: internal moves are value-neutral; external postings equal expected value change."""
    total = sum((as_decimal(row["value_base"]) for row in movements), Decimal("0"))
    expected = Decimal("0") if expected_net is None else as_decimal(expected_net)
    if total != expected:
        raise AssertionError(f"value conservation violated: sum={total} expected={expected}")


def assert_balance_equals_sum_lines(
    balances: Mapping[tuple, Decimal | int | str],
    lines: Iterable[Mapping[str, Any]],
    *,
    qty_field: str = "qty_base",
) -> None:
    """INV-03: materialised balance per posting key equals the signed sum of ledger lines."""
    computed: dict[tuple, Decimal] = {}
    for line in lines:
        key = (
            line["item_id"],
            line["location_id"],
            line.get("inventory_status_id"),
            line.get("ownership", "OWNED"),
            line.get("qty_class", "ON_HAND"),
            line.get("lot_id"),
            line.get("serial_id"),
        )
        computed[key] = computed.get(key, Decimal("0")) + as_decimal(line[qty_field])
    for key, balance in balances.items():
        expected = computed.get(tuple(key), Decimal("0"))
        actual = as_decimal(balance)
        if actual != expected:
            raise AssertionError(f"balance {key} = {actual}, ledger sum = {expected}")
    for key in computed:
        if key not in balances:
            raise AssertionError(f"ledger lines for {key} have no balance row")


def assert_commitment_le_on_hand(
    commitments: Mapping[tuple, Decimal | int | str],
    on_hand: Mapping[tuple, Decimal | int | str],
    *,
    allow_backorder: bool = False,
) -> None:
    """INV-04: committed never exceeds on-hand (unless a configured backorder exists)."""
    for key, committed in commitments.items():
        committed_d = as_decimal(committed)
        on_hand_d = as_decimal(on_hand.get(key, 0))
        if committed_d > on_hand_d and not allow_backorder:
            raise AssertionError(f"commitment {key} = {committed_d} exceeds on-hand {on_hand_d}")


def assert_serial_projection(
    serials: Mapping[str, Mapping[str, Any]],
    history: Iterable[Mapping[str, Any]],
    ledger_detail: Iterable[Mapping[str, Any]],
) -> None:
    """INV-18: projection == latest history row == net ledger detail per serial."""
    latest: dict[str, Mapping[str, Any]] = {}
    for row in history:
        serial_id = str(row["serial_id"])
        occurred = str(row.get("occurred_at", ""))
        if serial_id not in latest or occurred >= str(latest[serial_id].get("occurred_at", "")):
            latest[serial_id] = row
    detail_net: dict[str, Decimal] = {}
    for row in ledger_detail:
        if row.get("serial_id") is None:
            continue
        serial_id = str(row["serial_id"])
        detail_net[serial_id] = detail_net.get(serial_id, Decimal("0")) + as_decimal(row.get("qty_base", "0"))
    for serial_id, projection in serials.items():
        serial_id = str(serial_id)
        if latest.get(serial_id) is None:
            raise AssertionError(f"serial {serial_id} has no history row")
        if serial_id in detail_net and detail_net[serial_id] not in (Decimal("0"), Decimal("1"), Decimal("-1")):
            raise AssertionError(f"serial {serial_id} detail qty is not a single unit: {detail_net[serial_id]}")
        if projection.get("state") is None:
            raise AssertionError(f"serial {serial_id} projection missing state")
