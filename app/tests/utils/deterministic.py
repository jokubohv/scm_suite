"""Deterministic time and UUID helpers for tests.

Tests must never depend on wall-clock time or random ids. Use :class:`FixedClock` and
:class:`DeterministicUUIDs` (or the fixtures ``clock``/``fixed_now``/``uuids`` from ``conftest``).

``uuid7_at`` produces spec-conformant UUIDv7 values (48-bit Unix ms timestamp, version 7, RFC 4122
variant, sequence in the remaining bits) so tests also exercise the production UUID layout.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import UUID

#: Deterministic epoch used by default across the suite (matches the plan's reference date).
FIXED_EPOCH = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)

_UUID7_SEQUENCE_MASK = (1 << 12) - 1  # rand_a: 12 bits
_UUID7_RAND_B_MASK = (1 << 62) - 1  # rand_b: 62 bits


def uuid7_at(moment: datetime, sequence: int = 0) -> UUID:
    """Build a deterministic UUIDv7 for ``moment`` (truncated to ms) and ``sequence``.

    Raises ``ValueError`` for a naive datetime so tests are explicit about UTC.
    """
    if moment.tzinfo is None:
        raise ValueError("uuid7_at requires a timezone-aware datetime (use datetime(..., tzinfo=UTC))")
    timestamp_ms = int(moment.timestamp() * 1000) & ((1 << 48) - 1)
    rand_a = sequence & _UUID7_SEQUENCE_MASK
    rand_b = (sequence >> 12) & _UUID7_RAND_B_MASK
    value = (
        (timestamp_ms << 80)
        | (0x7 << 76)  # version 7
        | (rand_a << 64)
        | (0b10 << 62)  # RFC 4122 variant
        | rand_b
    )
    return UUID(int=value)


def uuid7_now(sequence: int = 0) -> UUID:
    """Non-deterministic convenience wrapper for ad-hoc local debugging only."""
    return uuid7_at(datetime.now(UTC), sequence)


def is_uuid7(value: UUID) -> bool:
    """True when ``value`` has UUIDv7 version and RFC 4122 variant bits."""
    return value.version == 7 and value.variant == "specified in RFC 4122"


@dataclass
class FixedClock:
    """Monotonic, deterministic UTC clock for tests."""

    start: datetime = FIXED_EPOCH
    _current: datetime = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if self.start.tzinfo is None:
            raise ValueError("FixedClock.start must be timezone-aware (UTC)")
        self._current = self.start

    def now(self) -> datetime:
        return self._current

    @property
    def timestamp_ms(self) -> int:
        return int(self._current.timestamp() * 1000)

    def advance(self, seconds: float = 0, *, milliseconds: int = 0) -> datetime:
        delta = timedelta(seconds=seconds, milliseconds=milliseconds)
        self._current = self._current + delta
        return self._current

    def reset(self, when: datetime | None = None) -> None:
        self._current = when or self.start


class DeterministicUUIDs:
    """Deterministic UUIDv7 generator: identical call sequence -> identical ids."""

    def __init__(self, start: datetime = FIXED_EPOCH, step_ms: int = 1) -> None:
        if step_ms <= 0:
            raise ValueError("step_ms must be positive")
        self._clock = FixedClock(start)
        self._step_ms = step_ms
        self._counter = 0

    def next(self) -> UUID:
        moment = self._clock.now()
        self._clock.advance(milliseconds=self._step_ms)
        value = uuid7_at(moment, self._counter)
        self._counter += 1
        return value

    def at(self, index: int) -> UUID:
        """Deterministic id for an index without advancing the generator."""
        if index < 0:
            raise ValueError("index must be non-negative")
        moment = self._clock.start + timedelta(milliseconds=self._step_ms * index)
        return uuid7_at(moment, index)

    def reset(self) -> None:
        self._clock.reset()
        self._counter = 0

    @staticmethod
    def uuid5(name: str) -> UUID:
        """Stable id from a name (no time component; for master-data-style fixtures)."""
        from uuid import NAMESPACE_URL, uuid5

        return uuid5(NAMESPACE_URL, f"inventory-platform-test:{name}")


class SlowClock(FixedClock):
    """Alias kept for readability in tests that explicitly advance time."""


def monotonic_ms() -> int:
    """Wall-clock accessor confined to helper code (tests should prefer FixedClock)."""
    return int(time.time() * 1000)
