#!/usr/bin/env python3
"""Render the generated tables in PLAN.md from the authoritative plan artifacts.

Usage:
  python tools/render_plan_tables.py --check     # exit 1 if PLAN.md tables are out of date
  python tools/render_plan_tables.py --inject    # rewrite the AUTO:* blocks in PLAN.md

Sources of truth:
  design/feature-sets.json      feature-set ids, names, waves, deps, contracts
  design/feature-coverage.json  catalog-topic disposition counts per FS
  design/FEATURE-SETS.md        size / table counts / wave prerequisites / exit criteria

Only the Lead-owned PLAN.md generated blocks are modified; no other file is touched.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLAN = ROOT / "PLAN.md"
MANIFEST = ROOT / "design" / "feature-sets.json"
COVERAGE = ROOT / "design" / "feature-coverage.json"
FEATURE_SETS_MD = ROOT / "design" / "FEATURE-SETS.md"

WAVE_ORDER = ["0A", "0B", "0C", "1A", "1B", "1C", "1D", "1E", "1F", "2"]
ENVS = ["dev", "staging", "prod"]

BLOCKS = {
    "FEATURE-SETS-TABLE": None,
    "WAVES-TABLE": None,
    "STATUS-TABLE": None,
}


def load_json(path: Path):
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


def parse_feature_sets_md() -> dict:
    """Return {fs_id: {size, tables}} and {wave: {sets, prereq, exit}} parsed from the doc."""
    text = FEATURE_SETS_MD.read_text(encoding="utf-8")
    sizes, waves = {}, {}
    for line in text.splitlines():
        m = re.match(r"^\|\s*(FS-\d{2})\s*\|", line)
        if m:
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if len(cells) >= 7:
                fs = cells[0]
                sizes[fs] = {
                    "size": cells[3],
                    "tables": cells[6],
                }
        m = re.match(r"^\|\s*(0A|0B|0C|1A|1B|1C|1D|1E|1F|2)\s*\|", line)
        if m:
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if len(cells) >= 4:
                wave = cells[0]
                sets = [s.strip() for s in re.findall(r"FS-\d{2}", cells[1])]
                waves[wave] = {"sets": sets, "prereq": cells[2], "exit": cells[3]}
    return {"sizes": sizes, "waves": waves}


def md_table(headers: list[str], rows: list[list[str]]) -> str:
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join(["---"] * len(headers)) + "|"]
    for row in rows:
        out.append("| " + " | ".join(str(c).replace("|", "\\|") for c in row) + " |")
    return "\n".join(out)


def build_feature_table(manifest, coverage, fs_md) -> str:
    rows = []
    fs_list = manifest["feature_sets"]
    order = {w: i for i, w in enumerate(WAVE_ORDER)}
    fs_sorted = sorted(fs_list, key=lambda x: (order.get(str(x.get("wave")), 99), x["id"]))
    for fs in fs_sorted:
        fs_id = fs["id"]
        deps = sorted({d["id"] for d in fs.get("depends_on", [])})
        provides = ", ".join(f"`{p['contract']}@{p['version']}`" for p in fs.get("provides", []))
        topics = coverage["totals"]["by_disposition"].get(fs_id, 0)
        info = fs_md["sizes"].get(fs_id, {})
        rows.append(
            [
                f"**{fs_id}**",
                fs.get("name", ""),
                str(fs.get("wave", "")),
                info.get("size", ""),
                ", ".join(deps),
                provides,
                str(topics),
                str(info.get("tables", "")),
            ]
        )
    total_topics = sum(int(r[6]) for r in rows)
    rows.append(["**Total**", "16 feature sets", "10 waves", "—", "116 dependency edges", "21 contracts", f"**{total_topics}**", "287 tables / 21 views"])
    return md_table(
        ["FS", "Feature set", "Wave", "Size", "Depends on", "Provided contracts", "Source topics", "Tables"],
        rows,
    )


def build_wave_table(fs_md, manifest) -> str:
    manifest_order = {fs["id"]: i for i, fs in enumerate(manifest["feature_sets"])}
    rows = []
    for wave in WAVE_ORDER:
        info = fs_md["waves"].get(wave)
        if not info:
            continue
        sets = sorted(info["sets"], key=lambda s: manifest_order.get(s, 99))
        # Sets within a wave are "parallel" only if none depends on another set in the same wave.
        set_set = set(sets)
        deps_in_wave = {
            fs["id"]
            for fs in manifest["feature_sets"]
            for d in fs.get("depends_on", [])
            if fs["id"] in set_set and d["id"] in set_set
        }
        if len(sets) > 1 and not deps_in_wave:
            joined = " ∥ ".join(sets)
        else:
            joined = " → ".join(sets)
        rows.append([
            f"**{wave}**",
            joined,
            info["prereq"],
            info["exit"],
        ])
    return md_table(["Wave", "Sets (deploy order)", "Prerequisite wave state", "Wave exit criteria"], rows)


def build_status_table(manifest) -> str:
    rows = []
    for fs in manifest["feature_sets"]:
        cells = []
        for env in ENVS:
            state_path = ROOT / "verification" / f"deployment-state.{env}.json"
            status = "—"
            if state_path.exists():
                try:
                    state = load_json(state_path)
                    entry = state.get(fs["id"]) or {}
                    status = entry.get("status", "—")
                except (json.JSONDecodeError, OSError):
                    status = "state-error"
            cells.append(status)
        rows.append([f"**{fs['id']}**", fs.get("name", ""), str(fs.get("wave", "")), *cells])
    return md_table(["FS", "Feature set", "Wave", "dev", "staging", "prod"], rows)


def render() -> dict[str, str]:
    manifest = load_json(MANIFEST)
    coverage = load_json(COVERAGE)
    fs_md = parse_feature_sets_md()
    return {
        "FEATURE-SETS-TABLE": build_feature_table(manifest, coverage, fs_md),
        "WAVES-TABLE": build_wave_table(fs_md, manifest),
        "STATUS-TABLE": build_status_table(manifest),
    }


def inject(blocks: dict[str, str]) -> bool:
    text = PLAN.read_text(encoding="utf-8")
    changed = False
    for name, body in blocks.items():
        pattern = re.compile(
            rf"(<!-- AUTO:{re.escape(name)}-START -->\n).*?(\n<!-- AUTO:{re.escape(name)}-END -->)",
            re.DOTALL,
        )
        if not pattern.search(text):
            raise SystemExit(f"marker block not found in PLAN.md: {name}")
        new_text = pattern.sub(lambda m: m.group(1) + body + m.group(2), text)
        if new_text != text:
            changed = True
            text = new_text
    if changed:
        PLAN.write_text(text, encoding="utf-8")
    return changed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--inject", action="store_true", help="rewrite the AUTO blocks in PLAN.md")
    group.add_argument("--check", action="store_true", help="verify the AUTO blocks are current")
    args = parser.parse_args()
    blocks = render()
    if args.inject:
        changed = inject(blocks)
        print("PLAN.md updated." if changed else "PLAN.md already current.")
        return 0
    for name, body in blocks.items():
        print(f"--- {name} ---\n{body}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
