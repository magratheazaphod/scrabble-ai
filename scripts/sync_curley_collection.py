#!/usr/bin/env python3
"""Sync the "James Curley practice games" Woogles collection to the tracker sheet.

Two things, both idempotent:

1. **Order** — the collection's chapter order is made to match the tracker's
   "Game #" column (ascending). Any collection game with no tracker row keeps
   its relative order and is parked at the end.
2. **Chapter titles** — rewritten to

       Game #<n> - <YYYY-MM-DD> - <first player> vs <second player>

   where the players are named "JD" and "James Curley" (Jesse's standing
   preference) and **whoever moved first is named first**, read off the game's
   own history rather than guessed.

The date comes from the tracker's date column (the sheet is the record of when
a game was played); games with no tracker row fall back to their existing
chapter title's date, then to no date segment.

    python3 scripts/sync_curley_collection.py [--dry-run]

Safe to re-run: it only issues UpdateChapterTitle for chapters whose title
actually changes, and ReorderGames only when the order actually differs.
"""

import argparse
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from update_curley_tracker import (  # noqa: E402
    build_header_map,
    load_dotenv,
    open_worksheet,
    woogles_rpc,
    _retry,
)

COLLECTION_UUID = "55b29df3-10fd-471b-9e87-135ed5bbb2f6"  # James Curley practice games
JD = "JD"
JAMES = "James Curley"


def iso(date_str):
    """'11/3/2022' (sheet style) or '2022-11-03' -> '2022-11-03'; '' if unparseable."""
    s = (date_str or "").strip()
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})$", s)
    if m:
        return s
    # the sheet mixes 4- and 2-digit years ('7/12/2026' and '4/4/26')
    m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{2}|\d{4})$", s)
    if m:
        mo, d, y = m.groups()
        y = int(y) + 2000 if len(y) == 2 else int(y)
        return f"{y}-{int(mo):02d}-{int(d):02d}"
    return ""


def read_tracker():
    """game id -> {'num': int, 'date': iso}, straight off the sheet."""
    ws = open_worksheet()
    rows = _retry(ws.get_all_values)
    if not rows:
        sys.exit("tracker sheet is empty")
    field_col, _ = build_header_map(rows[0])
    if "game_id" not in field_col:
        sys.exit("could not find the 'game id' column in the tracker sheet")
    gid_i = field_col["game_id"] - 1
    date_i = field_col["date"] - 1 if "date" in field_col else None
    # "Game #" is deliberately absent from COLUMN_ALIASES (it must never be a
    # write target), so locate it by header here.
    try:
        num_i = [h.strip().lower() for h in rows[0]].index("game #")
    except ValueError:
        sys.exit("could not find the 'Game #' column in the tracker sheet")

    out = {}
    for r in rows[1:]:
        if len(r) <= gid_i:
            continue
        gid = r[gid_i].strip()
        num = r[num_i].strip() if len(r) > num_i else ""
        if not gid or not num.isdigit():
            continue
        out[gid] = {
            "num": int(num),
            "date": iso(r[date_i]) if date_i is not None and len(r) > date_i else "",
        }
    return out


def first_player_is_jesse(game_id):
    """True if JD made the first move of the game.

    Read from the events (the first event's player_index is definitionally the
    player who moved first), falling back to the history's second_went_first
    flag for a game with no events.
    """
    h = woogles_rpc("game_service.GameMetadataService/GetGameHistory",
                    {"game_id": game_id})["history"]
    players = h.get("players") or []
    if len(players) != 2:
        raise ValueError(f"{game_id}: expected 2 players, got {len(players)}")
    events = h.get("events") or []
    if events:
        idx = events[0].get("player_index", 0)
    else:
        idx = 1 if h.get("second_went_first") else 0
    nick = (players[idx].get("nickname") or "").lower().replace("_", "")
    real = (players[idx].get("real_name") or "").lower()
    return nick in {"jd", "jessed", "jesseday", "jesse", "magrathean"} or real == "jesse day"


def chapter_title(num, date, jesse_first):
    a, b = (JD, JAMES) if jesse_first else (JAMES, JD)
    head = f"Game #{num}" if num is not None else "Game #?"
    parts = [head] + ([date] if date else []) + [f"{a} vs {b}"]
    return " - ".join(parts)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true",
                    help="print the planned order/titles without writing anything")
    ap.add_argument("--collection-uuid", default=COLLECTION_UUID)
    args = ap.parse_args(argv)

    load_dotenv()
    tracker = read_tracker()

    coll = woogles_rpc("collections_service.CollectionsService/GetCollection",
                       {"collection_uuid": args.collection_uuid})["collection"]
    games = coll.get("games") or []
    print(f"{coll['title']}: {len(games)} games, {len(tracker)} tracker rows")

    untracked = [g["game_id"] for g in games if g["game_id"] not in tracker]
    if untracked:
        print(f"  !! {len(untracked)} game(s) with no tracker row, parked at the end: "
              f"{', '.join(untracked)}")

    # Desired order: tracker Game # ascending; untracked games keep their
    # current relative order after everything numbered.
    def sort_key(item):
        i, g = item
        row = tracker.get(g["game_id"])
        return (0, row["num"], 0) if row else (1, 0, i)

    ordered = [g for _, g in sorted(enumerate(games), key=sort_key)]
    desired_ids = [g["game_id"] for g in ordered]
    current_ids = [g["game_id"] for g in games]

    retitles = []
    for g in ordered:
        row = tracker.get(g["game_id"])
        date = row["date"] if row else ""
        if not date:  # keep whatever date the existing title already carried
            m = re.search(r"(\d{4}-\d{2}-\d{2})", g.get("chapter_title") or "")
            date = m.group(1) if m else ""
        want = chapter_title(row["num"] if row else None, date,
                             first_player_is_jesse(g["game_id"]))
        have = g.get("chapter_title") or ""
        if want != have:
            retitles.append((g["game_id"], have, want))

    for gid, have, want in retitles:
        print(f"  retitle {gid}: {have!r} -> {want!r}")
    print(f"{len(retitles)} title(s) to change; order "
          f"{'differs' if desired_ids != current_ids else 'already correct'}")

    if args.dry_run:
        return 0

    for gid, _, want in retitles:
        woogles_rpc("collections_service.CollectionsService/UpdateChapterTitle",
                    {"collection_uuid": args.collection_uuid, "game_id": gid,
                     "chapter_title": want})
    if desired_ids != current_ids:
        woogles_rpc("collections_service.CollectionsService/ReorderGames",
                    {"collection_uuid": args.collection_uuid, "game_ids": desired_ids})
        print("reordered")
    print("done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
