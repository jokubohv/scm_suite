#!/usr/bin/env python3
"""Compare the declared contract aggregate with the mounted FastAPI runtime surface.

The committed aggregate intentionally contains contract-first skeletons for spec operations that
do not have a runtime route yet. This check distinguishes those skeletons from implementation-backed
operations, and fails if an operation declared as ``x-implemented`` is not mounted or if a mounted
operation is absent from the aggregate.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
METHODS = ("get", "post", "put", "patch", "delete")


def _ops(spec: dict) -> dict[tuple[str, str], dict]:
    return {
        (method.upper(), path): operation
        for path, item in spec.get("paths", {}).items()
        for method, operation in item.items()
        if method in METHODS
    }


def main() -> int:
    sys.path.insert(0, str(ROOT))
    from app.api.main import app

    live = _ops(app.openapi())
    aggregate = _ops(json.loads((ROOT / "contracts/openapi/openapi.json").read_text(encoding="utf-8")))
    declared_not_mounted = sorted(set(aggregate) - set(live))
    implemented_not_mounted = sorted(
        op for op in declared_not_mounted if aggregate[op].get("x-implemented") is True
    )
    mounted_not_declared = sorted(set(live) - set(aggregate))
    skeletons = sorted(op for op in declared_not_mounted if op not in set(implemented_not_mounted))
    report = {
        "live_operations": len(live),
        "aggregate_operations": len(aggregate),
        "declared_not_mounted": len(declared_not_mounted),
        "implemented_declared_not_mounted": implemented_not_mounted,
        "mounted_not_declared": mounted_not_declared,
        "contract_first_skeleton_operations": len(skeletons),
        "ok": not implemented_not_mounted and not mounted_not_declared,
    }
    out = ROOT / "verification/evidence/RESIDUALS/contract-runtime-diff.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: value for key, value in report.items() if key != "implemented_declared_not_mounted"}, indent=2))
    print(f"implemented_declared_not_mounted: {len(implemented_not_mounted)}")
    print(f"wrote {out.relative_to(ROOT)}")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
