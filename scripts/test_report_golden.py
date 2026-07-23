#!/usr/bin/env python3
"""Golden-corpus regression harness for scripts/tournament_report.py.

Diffs the module's deterministic output (everything before ## Summary) against
the previously LLM-rendered reports cached in .github/report-state.json's
`report_md` fields. Skips (prints a notice, exits 0) if data/golden-snapshot.json
is absent — see SKILL.md's testing section for how to regenerate it.

Run: python3 scripts/test_report_golden.py
"""
import difflib
import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from tournament_report import aggregate, check_phony_words, compute_game, game_notes, render_report

GOLDEN_SNAPSHOT_PATH = "data/golden-snapshot.json"
STATE_PATH = ".github/report-state.json"


def truncate_before_summary(md):
    idx = md.find("## Summary")
    return md[:idx].rstrip() if idx != -1 else md.rstrip()


def main():
    if not os.path.exists(GOLDEN_SNAPSHOT_PATH):
        print(f"{GOLDEN_SNAPSHOT_PATH} absent — skipping golden regression.", file=sys.stderr)
        return 0

    with open(GOLDEN_SNAPSHOT_PATH) as f:
        golden = json.load(f)
    with open(STATE_PATH) as f:
        state = json.load(f)

    golden_by_uuid = {c["uuid"]: c for c in golden.get("collections", [])}

    failures = 0
    checked = 0
    for uuid, entry in state.items():
        cached_md = entry.get("report_md")
        if not cached_md:
            continue
        col = golden_by_uuid.get(uuid)
        if not col:
            print(f"SKIP {entry.get('title')} ({uuid}): not present in golden snapshot", file=sys.stderr)
            continue

        checked += 1
        stats = [compute_game(r) for r in col["games"]]
        stats.sort(key=lambda g: g["round"])
        check_phony_words(stats)
        agg = aggregate(stats)
        notes = game_notes(stats)
        rendered = render_report(stats, agg, notes, col["title"])

        got = truncate_before_summary(rendered)
        want = truncate_before_summary(cached_md)

        if got.strip() == want.strip():
            print(f"OK   {col['title']}", file=sys.stderr)
            continue

        failures += 1
        print(f"DIFF {col['title']} ({uuid}):", file=sys.stderr)
        diff = difflib.unified_diff(
            want.splitlines(keepends=True),
            got.splitlines(keepends=True),
            fromfile="cached(report_md)",
            tofile="module(render_report)",
        )
        print("", file=sys.stdout)
        sys.stdout.writelines(diff)
        print("", file=sys.stderr)

    print(f"\n{checked} collections checked, {failures} with diffs.", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
