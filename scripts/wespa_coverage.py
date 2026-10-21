#!/usr/bin/env python3
"""Which of Jesse's WESPA-rated events have their games archived as .gcg here?

Renders `WESPA-COVERAGE.md` at the repo root: one row per rated event, coloured
by how many of its rounds have a GCG under `Tournament Games/`.

Folder names are whatever Jesse typed at the time ("King's Cup '19", "WESPAC
'17"), so nothing here matches on names. Each folder's games are reduced to
score pairs and identified against WESPA's round-by-round results by the same
fingerprint `wespa_tournaments.identify` uses for Woogles collections. A round
counts as covered when some archived game matches it.

    python3 scripts/wespa_coverage.py            # rewrite WESPA-COVERAGE.md
    python3 scripts/wespa_coverage.py --stdout   # print instead
"""

import argparse
import glob
import os
import re
import sys
from collections import defaultdict
from datetime import date
from urllib.parse import quote

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import wespa_tournaments as wt  # noqa: E402  — sibling module, path set above

ARCHIVE = "Tournament Games"
OUT_PATH = "WESPA-COVERAGE.md"
JESSE = re.compile(r"\b(jesse|jd|jessed|magrathean)\b", re.I)
PLAYER = re.compile(r"^#player[12]\s+(\S+)\s*(.*)$")
MOVE = re.compile(r"^>([^:]+):.*?(-?\d+)\s*$")

# A folder is attributed to an event only when most of its games match it, so a
# folder of club games that happens to share two score lines with an event
# cannot claim it.
MIN_SHARE = 0.5
MIN_NAME_SHARE = 0.6


def read_game(path):
    """{"jesse_score", "opp_score", "opponent"} from a GCG, or None if it is not
    one of Jesse's games (or not a game at all - folders hold notes files too)."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = [l.strip() for l in f]
    except OSError:
        return None
    names, me = {}, None
    for l in lines:
        m = PLAYER.match(l)
        if m:
            names[m.group(1)] = m.group(2) or m.group(1)
            if JESSE.search(l):
                me = m.group(1)
    if me is None or len(names) != 2:
        return None
    cum = {}
    for l in lines:
        m = MOVE.match(l)
        if m:
            cum[m.group(1).strip()] = int(m.group(2))
    opp = next(n for n in names if n != me)
    if me not in cum or opp not in cum:
        return None
    return {"jesse_score": cum[me], "opp_score": cum[opp], "opponent": names[opp]}


def folder_games():
    """{event folder: [game, ...]} for every folder under the archive."""
    out = defaultdict(list)
    for path in sorted(glob.glob(os.path.join(ARCHIVE, "**", "*.gcg"), recursive=True)):
        g = read_game(path)
        if g:
            out[os.path.dirname(path)].append(g)
    return out


def _fit(game, rnd):
    """(strength, -score gap) of a game as this round, strength 0 = not it.
    The gap breaks ties: a semi-final against the same opponent as a main-draw
    round goes to whichever scores are closer."""
    s = wt._pair_score(game, rnd)
    gap = abs(game["jesse_score"] - rnd["score_for"]) + abs(game["opp_score"] - rnd["score_against"])
    return (s, -gap) if s else (0, 0)


def _same_event(a, b):
    """Within a week of each other: a final or playoff filed beside its main
    draw. Anything further apart is a different event that shares score lines
    or opponents, never a sub-event of this folder's."""
    try:
        return abs((date.fromisoformat(a["date"]) - date.fromisoformat(b["date"])).days) <= 7
    except (TypeError, ValueError):
        return False


def coverage(playerid):
    """[(event, rounds, covered, [folder, ...])] for every rated event, oldest first."""
    events = wt.player_tournaments(playerid)
    by_id = {t["tourneyid"]: t for t in events}
    # Attribute per game, not per folder: a final is often filed beside its
    # main draw ("NSC '19 Final Rd 1" in the NSC '19 folder) but is a separate
    # WESPA result. The folder's events are those that explain at least two of
    # its games and fall within a week of the main one; each game then goes to
    # whichever of them it fits best, ties to the main event.
    pooled = defaultdict(list)
    folders = defaultdict(list)
    for folder, games in folder_games().items():
        hits = wt.identify(games, playerid, candidates=events)
        # Names as well as scores: a folder of casual games against a regular
        # opponent can reach MIN_SHARE on ballpark score matches alone.
        if (not hits or hits[0]["share"] < MIN_SHARE
                or hits[0]["name_hits"] < MIN_NAME_SHARE * len(games)):
            continue
        main = hits[0]
        rounds = {h["tourneyid"]: wt.player_rounds(playerid, h["tourneyid"])
                  for h in hits if h is main or _same_event(h, main)}
        for g in games:
            fit = max(((*_fit(g, r), -i, tid) for i, (tid, rs) in enumerate(rounds.items())
                       for r in rs), default=(0,))
            if fit[0] > 0:
                pooled[fit[-1]].append(g)
                if folder not in folders[fit[-1]]:
                    folders[fit[-1]].append(folder)
    rows = []
    for t in events:
        tid = t["tourneyid"]
        n = len(wt._pairs(wt.player_rounds(playerid, tid)))
        hit = wt.identify(pooled[tid], playerid, candidates=[by_id[tid]]) if pooled[tid] else []
        covered = min(hit[0]["matched"], n) if hit else 0
        rows.append((t, n, covered, folders[tid]))
    return rows


def render(rows):
    full = sum(1 for _, n, c, _ in rows if n and c >= n)
    part = sum(1 for _, n, c, _ in rows if 0 < c < n)
    none = sum(1 for _, _, c, _ in rows if c == 0)
    games = sum(c for _, _, c, _ in rows)
    rounds = sum(n for _, n, _, _ in rows)
    out = [
        "# WESPA tournament GCG coverage",
        "",
        "Every WESPA-rated event Jesse Day has played, and how many of its rounds "
        f"have a `.gcg` in [`{ARCHIVE}/`]({quote(ARCHIVE)}/). Games are matched to "
        "rounds by score, not by folder name.",
        "",
        f"🟢 all rounds ({full}) · 🟡 some rounds ({part}) · 🔴 none ({none}) · "
        f"**{games} of {rounds} rounds archived**",
        "",
        "Generated by `scripts/wespa_coverage.py` - edit that, not this file.",
        "",
        "| | Date | Event | Record | GCGs | Folder |",
        "| :-: | --- | --- | :-: | --: | --- |",
    ]
    for t, n, c, fs in reversed(rows):
        dot = "🟢" if n and c >= n else "🟡" if c else "🔴"
        name = " ".join((t.get("name") or "").split())
        event = f"[{name}]({wt.TOURNEY_PAGE.format(t['tourneyid'])})"
        record = f"{int(t.get('wins') or 0)}-{int(t.get('losses') or 0)}"
        links = ", ".join(f"[{os.path.relpath(f, ARCHIVE)}]({quote(f)}/)" for f in fs)
        out.append(f"| {dot} | {t.get('date') or ''} | {event} | {record} | {c}/{n} | {links} |")
    return "\n".join(out) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--player", default="Jesse Day")
    ap.add_argument("--stdout", action="store_true")
    args = ap.parse_args(argv)
    playerid, _ = wt.resolve_player(args.player)
    if not playerid:
        sys.exit(f"No WESPA player found for {args.player!r}")
    md = render(coverage(playerid))
    if args.stdout:
        print(md, end="")
    else:
        with open(OUT_PATH, "w") as f:
            f.write(md)
        print(f"Wrote {OUT_PATH}")


if __name__ == "__main__":
    main()
