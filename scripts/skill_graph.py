#!/usr/bin/env python3
"""The skill graph: one stacked bar per event, chronological, split by game stage.

A per-tournament report says how a single event went. This says whether Jesse is
getting better, and *where* — each event is one bar, the bar's height is the
metric (mistake index per game by default), and the stack shows which stage of
the game the metric came from. A shrinking bar whose endgame segment never moves
is a different story from one shrinking evenly, and neither is visible in a
report that looks at one collection at a time.

    python3 scripts/skill_graph.py --out reports/skill-graph.png
    python3 scripts/skill_graph.py --metric win-pct-lost --snapshot data/golden-snapshot.json

The metric is a registry entry (METRICS), not a hardcoded column: adding another
per-stage measure means adding one dict there, because `stage_breakdown` in
tournament_report.py already carries the per-stage numbers for every game.
"""
import argparse
import json
import os
import re
import sys

import matplotlib

matplotlib.use("Agg")  # no display in CI
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.ticker import MaxNLocator  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tournament_report as tr  # noqa: E402

DEFAULT_SNAPSHOT = "data/woogles-snapshot.json"
DEFAULT_OUT = "reports/skill-graph.png"
OVERRIDES_PATH = ".github/event-dates.json"

# --------------------------------------------------------------------------
# Metrics
# --------------------------------------------------------------------------
# `per_game` pulls one stage's contribution out of a game's stage_breakdown.
# Every metric here must be additive across stages, because the chart stacks
# them: the bar height has to be the event's real total, not a coincidence.
METRICS = {
    "mistake-index": {
        "key": "mistake_index",
        "title": "Mistake index per game, by game stage",
        "ylabel": "Mistake index per game",
        "fmt": lambda v: f"{v:.2f}",
        "per_game": lambda sb, stage: sb[stage]["mistake_index"],
    },
    # Read the endgame segment here with care: BestBot scores endgame turns in
    # spread, not win probability, so that segment is structurally near-zero and
    # means "not measured in these units" rather than "played the endgame
    # perfectly". The mistake index has no such hole — it buckets endgame turns
    # on spread and weighs them like any other — which is why it is the default.
    "win-pct-lost": {
        "key": "win_prob_lost",
        "title": "Win probability lost per game, by game stage",
        "ylabel": "Win % lost per game",
        "fmt": lambda v: f"{v:.1f}%",
        "per_game": lambda sb, stage: sb[stage]["win_prob_lost"] * 100,
    },
}

# Stages are ORDERED (early -> endgame), not four unrelated categories, so they
# get an ordinal one-hue ramp rather than the categorical slots: the reader sees
# the order in the color without consulting the legend, and a single-hue ramp is
# colorblind-safe by construction. Steps 250/400/550/700 of the reference blue —
# validated with `validate_palette.js --ordinal --mode light` (monotone
# lightness, every adjacent gap clear, light end 2.06:1 on the surface).
# Light mode only: this PNG's only destination is an email body on white.
STAGE_COLORS = {
    "early": "#86b6ef",
    "mid": "#3987e5",
    "pre-endgame": "#1c5cab",
    "endgame": "#0d366b",
}
SURFACE = "#fcfcfb"
TEXT_PRIMARY = "#0b0b0b"
TEXT_SECONDARY = "#52514e"
GRID = "#e3e2de"

MONTHS = {
    m: i + 1
    for i, m in enumerate(
        "jan feb mar apr may jun jul aug sep oct nov dec".split()
    )
}


# --------------------------------------------------------------------------
# Dating the events
# --------------------------------------------------------------------------

def load_overrides(path=OVERRIDES_PATH):
    """Per-collection overrides, keyed by uuid or by exact title.

    Each value may carry "date" (YYYY-MM-DD or YYYY-MM), "label", and
    "include" (false to keep a collection off the chart even though it dates).
    Absent file = no overrides, which is the right default for a fresh clone.
    """
    if not os.path.exists(path):
        return {}
    with open(path) as f:
        return json.load(f)


def parse_event_date(title):
    """(sort_key, "Mon YYYY"/"YYYY") from a collection title, or None.

    Titles are the only dating evidence there is: a Woogles game history carries
    no date, and `added_at` is when the GCG was uploaded — years after the event
    for most of this archive. So a four-digit year is required, an optional month
    name refines it, and anything with no year at all (the practice-game
    collections) simply isn't an event and drops off the chart. Use the
    overrides file for the rest.
    """
    year = None
    for m in re.finditer(r"(?<!\d)(19|20)\d{2}(?!\d)", title):
        year = int(m.group(0))  # last one wins: "2019 WESPAC Final"/"WESPAC 2019"
    if year is None:
        m = re.search(r"'(\d{2})(?!\d)", title)  # "Austin One-Day Aug '23"
        if m:
            year = 2000 + int(m.group(1))
    if year is None:
        return None
    month = None
    for m in re.finditer(r"[A-Za-z]{3,}", title):
        cand = m.group(0)[:3].lower()
        if cand in MONTHS:
            month = MONTHS[cand]
            break
    # Day 15 as the within-month placeholder, matching month 7 within a year:
    # an inferred date sits mid-interval rather than pretending to a precision
    # the title never had. Real dates come from the overrides file, which
    # scripts/wespa_tournaments.py fills in from WESPA's own results.
    return ((year, month or 7, 15), f"{_mon_name(month)} {year}" if month else str(year))


def _mon_name(month):
    return list(MONTHS)[month - 1].capitalize() if month else ""


def event_date(col, overrides):
    """(sort_key, display) for a collection, honouring the overrides file."""
    ov = overrides.get(col["uuid"]) or overrides.get(col["title"]) or {}
    if ov.get("include") is False:
        return None
    if ov.get("date"):
        parts = [int(p) for p in ov["date"].split("-")[:3]]
        year = parts[0]
        month = parts[1] if len(parts) > 1 else 7
        day = parts[2] if len(parts) > 2 else 15
        # Two events can share a month — a main event and its final are a day
        # apart — so the day has to reach the sort key or they order
        # alphabetically, which is how this axis was wrong before.
        return ((year, month, day), f"{_mon_name(month)} {year}")
    return parse_event_date(col["title"])


def event_label(col, overrides):
    ov = overrides.get(col["uuid"]) or overrides.get(col["title"]) or {}
    return ov.get("label") or col["title"]


# --------------------------------------------------------------------------
# Building the series
# --------------------------------------------------------------------------

def build_events(collections, metric, overrides=None, subject=None):
    """One chart-ready record per datable collection, oldest first.

    Averaged per game, never totalled: a 30-round world championship and a
    six-game one-day would otherwise differ by their length rather than by how
    Jesse played. Games BestBot could not score are excluded from the divisor as
    well as the numerator, so a half-analyzed collection is not silently halved.
    """
    overrides = overrides if overrides is not None else load_overrides()
    spec = METRICS[metric]
    events = []
    for col in collections:
        dated = event_date(col, overrides)
        if not dated:
            continue
        sort_key, date_display = dated

        totals = {s: 0.0 for s in tr.STAGES}
        n = 0
        for r in col.get("games") or []:
            try:
                g = tr.compute_game(r, subject=subject)
            except (StopIteration, KeyError, TypeError):
                continue  # not the subject's game, or no usable analysis
            sb = g.get("stage_breakdown")
            if not sb or g["mistake_index"] is None:
                continue
            for stage in tr.STAGES:
                totals[stage] += spec["per_game"](sb, stage)
            n += 1
        if not n:
            continue
        events.append({
            "uuid": col["uuid"],
            "label": event_label(col, overrides),
            "date_display": date_display,
            "sort_key": sort_key,
            "games": n,
            "values": {s: totals[s] / n for s in tr.STAGES},
            "total": sum(totals.values()) / n,
        })
    # Ties on (year, month) are common — a main event and its final share a date.
    # Break them on the label so the ordering is at least stable run to run.
    events.sort(key=lambda e: (e["sort_key"], e["label"]))
    return events


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------

def _wrap(label, width=18):
    words, lines, cur = label.split(), [], ""
    for w in words:
        if cur and len(cur) + 1 + len(w) > width:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    if cur:
        lines.append(cur)
    return "\n".join(lines[:3])


def render(events, metric, out_path, title=None):
    """Draw the stacked bars and write the PNG. Returns out_path, or None."""
    if not events:
        print("No datable, analyzed events — no chart.", file=sys.stderr)
        return None
    spec = METRICS[metric]

    width = max(6.0, 1.45 * len(events) + 1.8)
    fig, ax = plt.subplots(figsize=(width, 4.6), dpi=200)
    fig.patch.set_facecolor(SURFACE)
    ax.set_facecolor(SURFACE)

    xs = range(len(events))
    bottoms = [0.0] * len(events)
    for stage in tr.STAGES:
        vals = [e["values"][stage] for e in events]
        ax.bar(
            xs, vals, bottom=bottoms, width=0.62,
            color=STAGE_COLORS[stage], label=stage,
            # A surface-colored edge is the 2px gap between stacked segments:
            # without it, two adjacent blues in the ramp read as one block.
            edgecolor=SURFACE, linewidth=1.6,
        )
        bottoms = [b + v for b, v in zip(bottoms, vals)]

    # One direct label per bar — the total, which is the number being compared.
    # Labelling every segment would put four numbers on a bar that is often
    # shorter than the text.
    headroom = max(bottoms) * 1.16 if max(bottoms) else 1
    for x, e in zip(xs, events):
        ax.text(x, e["total"] + headroom * 0.02, spec["fmt"](e["total"]),
                ha="center", va="bottom", fontsize=9, color=TEXT_PRIMARY)

    ax.set_ylim(0, headroom)
    ax.set_xticks(list(xs))
    ax.set_xticklabels(
        [f"{_wrap(e['label'])}\n{e['date_display']} · {e['games']}g" for e in events],
        fontsize=8, color=TEXT_SECONDARY,
    )
    ax.set_ylabel(spec["ylabel"], fontsize=9, color=TEXT_SECONDARY)
    ax.set_title(title or spec["title"], fontsize=12, color=TEXT_PRIMARY,
                 loc="left", pad=14)
    ax.yaxis.set_major_locator(MaxNLocator(nbins=5))
    ax.tick_params(axis="y", labelsize=8, colors=TEXT_SECONDARY, length=0)
    ax.tick_params(axis="x", length=0)
    ax.grid(axis="y", color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(GRID)

    handles, labels = ax.get_legend_handles_labels()
    leg = ax.legend(
        handles[::-1], labels[::-1],  # legend order matches the stack, top-down
        loc="upper left", bbox_to_anchor=(0, -0.28), ncol=4, frameon=False,
        fontsize=8, handlelength=1.0, handleheight=1.0, columnspacing=1.4,
    )
    for text in leg.get_texts():
        text.set_color(TEXT_SECONDARY)

    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    fig.savefig(out_path, bbox_inches="tight", facecolor=SURFACE)
    plt.close(fig)
    return out_path


def table_rows(events, metric):
    """The chart's own numbers as markdown — the non-visual reading of the PNG,
    and what an email client that blocks images is left with."""
    spec = METRICS[metric]
    lines = [
        "| Event | Date | Games | " + " | ".join(s.capitalize() for s in tr.STAGES) + " | Total |",
        "| --- | --- | ---: | " + " | ".join("---:" for _ in tr.STAGES) + " | ---: |",
    ]
    for e in events:
        cells = " | ".join(spec["fmt"](e["values"][s]) for s in tr.STAGES)
        lines.append(
            f"| {e['label']} | {e['date_display']} | {e['games']} | {cells} | "
            f"**{spec['fmt'](e['total'])}** |"
        )
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--snapshot", default=DEFAULT_SNAPSHOT)
    ap.add_argument("--metric", default="mistake-index", choices=sorted(METRICS))
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--title")
    ap.add_argument("--table", action="store_true", help="print the numbers too")
    args = ap.parse_args()

    with open(args.snapshot) as f:
        snapshot = json.load(f)
    events = build_events(snapshot.get("collections") or [], args.metric)
    path = render(events, args.metric, args.out, title=args.title)
    if path:
        print(f"Wrote {path} ({len(events)} events)", file=sys.stderr)
    if args.table:
        print(table_rows(events, args.metric))


if __name__ == "__main__":
    main()
