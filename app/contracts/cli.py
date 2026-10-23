"""Command line interface for the contract-first toolchain (S0-04).

Commands::

    python -m app.contracts.cli generate [--check]
    python -m app.contracts.cli aggregate
    python -m app.contracts.cli lint [--json] [--no-freshness]
    python -m app.contracts.cli diff --baseline X --current Y [--kind openapi|event]
    python -m app.contracts.cli list [--json]

Deterministic, offline, stdlib-only. Exit codes: 0 success/clean, 1 lint/parse error,
2 breaking contract diff (framework §5 L1 compatibility gate).
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from . import generate, lint
from .diff import diff_artifacts, diff_event_schema, diff_openapi, compare_versions
from .generate import build_outputs, build_model, write_outputs
from .parse_specs import build_model as parse_model


def _print_json(document: Any) -> None:
    print(json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False))


def cmd_generate(args: argparse.Namespace) -> int:
    model = build_model()
    outputs = build_outputs(model)
    result = write_outputs(outputs, check=args.check)
    summary = {
        "command": "generate",
        "check": args.check,
        "plan_version": model.plan_version,
        "feature_sets": len(model.feature_sets),
        "contracts": len(model.contracts),
        "endpoints": len(model.endpoints),
        "events": sum(1 for e in model.events if not e.wildcard),
        "artefacts": len(outputs),
        **result,
    }
    if args.check and (result["stale"] or result["missing"]):
        summary["ok"] = False
        _print_json(summary)
        return 1
    summary["ok"] = True
    _print_json(summary)
    return 0


def cmd_aggregate(args: argparse.Namespace) -> int:
    return cmd_generate(argparse.Namespace(check=False))


def cmd_lint(args: argparse.Namespace) -> int:
    report = lint.run_lint(freshness=not args.no_freshness)
    if args.json:
        _print_json(report)
    else:
        print(f"contract lint: {'PASS' if report['ok'] else 'FAIL'} — errors={len(report['errors'])} warnings={len(report['warnings'])}")
        stats = report["stats"]
        print(
            f"  feature_sets={stats.get('feature_sets')} contracts={stats.get('contracts')} "
            f"fragments={len(stats.get('fragments', {}))} operations={stats.get('operations')} "
            f"paths={stats.get('paths')} event_schemas={stats.get('event_schemas')} plan={stats.get('plan_version')}"
        )
        for issue in report["errors"]:
            print(f"  ERROR [{issue['code']}] {issue['message']}" + (f" ({issue['path']})" if issue["path"] else ""))
        for issue in report["warnings"]:
            print(f"  WARN  [{issue['code']}] {issue['message']}" + (f" ({issue['path']})" if issue["path"] else ""))
    return 0 if report["ok"] else 1


def cmd_diff(args: argparse.Namespace) -> int:
    try:
        report = diff_artifacts(args.kind, args.baseline, args.current)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"contract diff error: {exc}", file=sys.stderr)
        return 1
    _print_json(report)
    return 2 if report["breaking_count"] else 0


def cmd_list(args: argparse.Namespace) -> int:
    model = parse_model()
    payload = {
        "plan_version": model.plan_version,
        "feature_sets": [
            {
                "id": fs.id,
                "wave": fs.wave,
                "contracts": [{"contract": p.get("contract"), "version": p.get("version")} for p in fs.provides],
                "endpoints": len(fs.endpoints),
                "events": sum(1 for e in fs.events if not e.wildcard),
                "jobs": len(fs.jobs),
            }
            for fs in model.feature_sets
        ],
        "contracts": [{"contract": c["contract"], "version": c["version"], "feature_set": c["feature_set"], "dir": c["dir"]} for c in model.contracts],
        "counts": {"feature_sets": len(model.feature_sets), "contracts": len(model.contracts), "endpoints": len(model.endpoints)},
    }
    contract_entries: list[dict[str, object]] = payload["contracts"]  # type: ignore[assignment]
    if args.json:
        _print_json(payload)
        return 0
    print(f"plan v{model.plan_version}: {len(model.feature_sets)} feature sets, {len(model.contracts)} contracts, {len(model.endpoints)} endpoints")
    for entry in contract_entries:
        print(f"  {entry['feature_set']:6s} {entry['contract']:32s} @{entry['version']:8s} app/contracts/{entry['dir']}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.contracts.cli", description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    generate_parser = subparsers.add_parser("generate", help="write/refresh committed contract artefacts")
    generate_parser.add_argument("--check", action="store_true", help="fail if committed artefacts are stale (no writes)")
    generate_parser.set_defaults(func=cmd_generate)

    subparsers.add_parser("aggregate", help="alias for generate (aggregate OpenAPI + registries)").set_defaults(func=cmd_aggregate)

    lint_parser = subparsers.add_parser("lint", help="structural + coverage + freshness lint (CI gate)")
    lint_parser.add_argument("--json", action="store_true", help="machine-readable report")
    lint_parser.add_argument("--no-freshness", action="store_true", help="skip committed-artefact freshness check")
    lint_parser.set_defaults(func=cmd_lint)

    diff_parser = subparsers.add_parser("diff", help="additive-only contract diff (exit 2 on breaking change)")
    diff_parser.add_argument("--baseline", required=True, help="baseline JSON artefact")
    diff_parser.add_argument("--current", required=True, help="current JSON artefact")
    diff_parser.add_argument("--kind", choices=["openapi", "event"], default="openapi")
    diff_parser.set_defaults(func=cmd_diff)

    list_parser = subparsers.add_parser("list", help="list feature sets/contracts/versions")
    list_parser.add_argument("--json", action="store_true")
    list_parser.set_defaults(func=cmd_list)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
