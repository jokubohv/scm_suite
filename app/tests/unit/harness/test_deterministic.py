"""Deterministic clock/UUID helpers are stable, UUIDv7-conformant and reproducible."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.tests.utils.deterministic import (
    FIXED_EPOCH,
    DeterministicUUIDs,
    FixedClock,
    is_uuid7,
    uuid7_at,
)


def test_fixed_epoch_is_utc() -> None:
    assert FIXED_EPOCH.tzinfo is not None
    assert FIXED_EPOCH.utcoffset() == timedelta(0)


def test_clock_is_monotonic_and_resettable() -> None:
    clock = FixedClock(FIXED_EPOCH)
    assert clock.now() == FIXED_EPOCH
    assert clock.advance(seconds=60) == FIXED_EPOCH + timedelta(seconds=60)
    assert clock.advance(milliseconds=500) == FIXED_EPOCH + timedelta(seconds=60, milliseconds=500)
    clock.reset()
    assert clock.now() == FIXED_EPOCH
    assert clock.timestamp_ms == int(FIXED_EPOCH.timestamp() * 1000)


def test_clock_rejects_naive_datetime() -> None:
    with pytest.raises(ValueError):
        FixedClock(datetime(2026, 1, 1))  # noqa: DTZ001 - deliberately naive


def test_uuid7_at_is_valid_and_time_ordered() -> None:
    early = uuid7_at(FIXED_EPOCH)
    later = uuid7_at(FIXED_EPOCH + timedelta(seconds=1))
    assert is_uuid7(early) and is_uuid7(later)
    assert early.version == 7
    assert early.variant == "specified in RFC 4122"
    assert str(early) < str(later)  # UUIDv7 textual order follows time
    assert uuid7_at(FIXED_EPOCH) == early  # deterministic


def test_uuid7_sequence_makes_values_unique() -> None:
    values = {uuid7_at(FIXED_EPOCH, sequence) for sequence in range(5000)}
    assert len(values) == 5000
    assert all(is_uuid7(value) for value in values)


def test_uuid7_rejects_naive_datetime() -> None:
    with pytest.raises(ValueError):
        uuid7_at(datetime(2026, 1, 1))  # noqa: DTZ001 - deliberately naive


def test_deterministic_generator_repeats_exactly() -> None:
    first = DeterministicUUIDs()
    second = DeterministicUUIDs()
    first_ids = [first.next() for _ in range(20)]
    second_ids = [second.next() for _ in range(20)]
    assert first_ids == second_ids
    assert len(set(first_ids)) == 20
    first.reset()
    assert first.next() == first_ids[0]


def test_deterministic_at_is_pure_and_indexed() -> None:
    generator = DeterministicUUIDs()
    assert generator.at(7) == generator.at(7)
    assert generator.at(0) != generator.at(1)
    with pytest.raises(ValueError):
        generator.at(-1)


def test_uuid5_names_are_stable() -> None:
    generator = DeterministicUUIDs()
    assert generator.uuid5("item:1") == generator.uuid5("item:1")
    assert generator.uuid5("item:1") != generator.uuid5("item:2")
    assert generator.uuid5("item:1").version == 5


def test_uuids_fixture_matches_helper(uuids: DeterministicUUIDs, uuid7_factory) -> None:
    value = uuids.next()
    assert is_uuid7(value)
    assert uuid7_factory(FIXED_EPOCH, 0) == uuid7_at(FIXED_EPOCH, 0)


def test_slow_clock_alias_is_typed() -> None:
    from app.tests.utils.deterministic import SlowClock

    clock = SlowClock(FIXED_EPOCH)
    assert clock.now() == FIXED_EPOCH
    assert isinstance(clock.advance(1), datetime)
    assert clock.now().tzinfo == UTC
