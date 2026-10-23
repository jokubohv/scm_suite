"""Golden-file load/compare/update helpers.

Canonical fixtures live in ``app/tests/fixtures/golden/``. Comparison is numeric-tolerant (decimals
are compared as strings/numbers within a tolerance) so cross-platform formatting cannot cause false
failures. Set ``UPDATE_GOLDEN=1`` while developing to rewrite the fixture, then commit the diff.

Never update golden files automatically in CI: the environment variable must be explicit.
"""

from __future__ import annotations

import json
import os
from decimal import Decimal
from pathlib import Path
from typing import Any

GOLDEN_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "golden"
UPDATE_ENV = "UPDATE_GOLDEN"


class GoldenFileError(AssertionError):
    """Golden comparison failure with a concise JSON-path detail."""


def golden_path(name: str, *, base_dir: Path | None = None) -> Path:
    stem = name if name.endswith(".json") else f"{name}.json"
    return (base_dir or GOLDEN_DIR) / stem


def load_golden(name: str, *, base_dir: Path | None = None) -> Any:
    path = golden_path(name, base_dir=base_dir)
    if not path.is_file():
        raise GoldenFileError(f"golden fixture not found: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def write_golden(name: str, payload: Any, *, base_dir: Path | None = None) -> Path:
    path = golden_path(name, base_dir=base_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def assert_matches_golden(
    name: str,
    actual: Any,
    *,
    base_dir: Path | None = None,
    tolerance: Decimal | str = Decimal("0.000000000001"),
) -> None:
    """Compare ``actual`` with the golden fixture; rewrite it first when ``UPDATE_GOLDEN=1``."""
    if os.environ.get(UPDATE_ENV) == "1":
        write_golden(name, actual, base_dir=base_dir)
        return
    expected = load_golden(name, base_dir=base_dir)
    _compare(expected, actual, path="$", tolerance=_as_decimal(tolerance))


def assert_matches_golden_file(
    golden_name: str,
    actual: Any,
    *,
    base_dir: Path | None = None,
    tolerance: Decimal | str = Decimal("0.000000000001"),
) -> None:
    assert_matches_golden(golden_name, actual, base_dir=base_dir, tolerance=tolerance)


def _as_decimal(value: Decimal | str | int | float) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _compare(expected: Any, actual: Any, *, path: str, tolerance: Decimal) -> None:
    if isinstance(expected, dict) and isinstance(actual, dict):
        expected_keys, actual_keys = set(expected), set(actual)
        if expected_keys != actual_keys:
            missing = sorted(expected_keys - actual_keys)
            extra = sorted(actual_keys - expected_keys)
            raise GoldenFileError(f"{path}: key mismatch (missing={missing}, extra={extra})")
        for key in sorted(expected_keys):
            _compare(expected[key], actual[key], path=f"{path}.{key}", tolerance=tolerance)
        return
    if isinstance(expected, list) and isinstance(actual, list):
        if len(expected) != len(actual):
            raise GoldenFileError(f"{path}: list length {len(actual)} != {len(expected)}")
        for index, (expected_item, actual_item) in enumerate(zip(expected, actual, strict=True)):
            _compare(expected_item, actual_item, path=f"{path}[{index}]", tolerance=tolerance)
        return
    if _is_number(expected) and _is_number(actual):
        delta = abs(_as_decimal(actual) - _as_decimal(expected))
        if delta > tolerance:
            raise GoldenFileError(f"{path}: {actual} != {expected} (delta {delta} > {tolerance})")
        return
    if expected != actual:
        raise GoldenFileError(f"{path}: {actual!r} != {expected!r}")


def _is_number(value: Any) -> bool:
    if isinstance(value, bool) or isinstance(value, Decimal):
        return isinstance(value, Decimal)
    if isinstance(value, int | float):
        return True
    if isinstance(value, str):
        try:
            Decimal(value)
        except Exception:  # noqa: BLE001 - not numeric, compare as string
            return False
        return True
    return False
