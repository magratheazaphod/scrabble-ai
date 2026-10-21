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
import json
import os
import re
import sys
from collections import defaultdict
from datetime import date
from urllib.parse import quote

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gcg_preflight as gp  # noqa: E402  — sibling modules, path set above
import wespa_tournaments as wt  # noqa: E402

ARCHIVE = "Tournament Games"
OUT_PATH = "WESPA-COVERAGE.md"
SYNC_MANIFEST = ".github/woogles-archive.json"  # written by sync_woogles_archive.py
DATES_PATH = wt.DATES_PATH
COLLECTION_URL = "https://woogles.io/collections/{}"
# Jesse's in-game nicknames across 15 years of files. Matched against the whole
# nickname, so an opponent called "Jesse_Matthews" is not mistaken for him.
JESSE_NICKS = {"jd", "jesse", "jessed", "jesseday", "jesse_day", "day", "magrathean"}
ROUND = re.compile(r"\b(?:rd|round)\.?\s*(\d+)", re.I)
PLAYER = re.compile(r"^#player[12]\s+(\S+)\s*(.*)$")
MOVE = re.compile(r"^>([^:]+):.*?(-?\d+)\s*$")

# A folder is attributed to an event only when most of its games match it, so a
# folder of club games that happens to share two score lines with an event
# cannot claim it.
MIN_SHARE = 0.5
MIN_NAME_SHARE = 0.6


def _is_jesse(nick, full):
    """Jesse's player line, in any of the forms the archive uses: "JD",
    "jesseday", "Jesse_Day_(USA)", "DayJesse" / "Day, Jesse"."""
    if nick.lower() in JESSE_NICKS:
        return True
    words = set(re.findall(r"[a-z]+", f"{nick} {full}".lower()))
    squashed = re.sub(r"[^a-z]", "", nick.lower())
    return {"jesse", "day"} <= words or squashed in ("jesseday", "dayjesse")


def _loose_name(a, b):
    """Could these be the same opponent? Looser than wespa_tournaments'
    _name_match, because it only ever confirms a pairing the round number has
    already proposed: "Dave"/"David", "Bob Linn"/"Robert Linn", "Olaugh"/
    "John O'Laughlin", "Bnjy"/"Ben Schoenbrun" all have to pass."""
    ta = [re.sub(r"[^a-z]", "", t) for t in (a or "").lower().split()]
    tb = [re.sub(r"[^a-z]", "", t) for t in (b or "").lower().split()]
    for x in filter(None, ta):
        for y in filter(None, tb):
            if x[:3] == y[:3] or (len(x) >= 4 and x in y) or (len(y) >= 4 and y in x):
                return True
    return False


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
            if me is None and _is_jesse(m.group(1), m.group(2)):
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
    rd = ROUND.search(os.path.basename(path))
    return {"jesse_score": cum[me], "opp_score": cum[opp], "opponent": names[opp],
            "path": path, "round": int(rd.group(1)) if rd else None}


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
    pooled = defaultdict(list)  # tourneyid -> games
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
            # A game that fits no round still belongs to the folder's main
            # event: classify_rounds can pair it by its round number.
            tid = fit[-1] if fit[0] > 0 else main["tourneyid"]
            pooled[tid].append(g)
            if folder not in folders[tid]:
                folders[tid].append(folder)
    rows = []
    for t in events:
        tid = t["tourneyid"]
        detail = classify_rounds(wt.player_rounds(playerid, tid), pooled[tid])
        covered = sum(1 for d in detail if d["file"])
        rows.append((t, len(detail), covered, folders[tid], detail))
    return rows


def classify_rounds(rounds, games):
    """One entry per official round: which archived file is that game, if any.

    A file is paired with a round by the round number in its filename when the
    opponent or the scores agree, else by score alone. "score_differs" is the
    common case of a file whose final score is not the one submitted - a
    real-life scoring error, or a transcription that drifted. Either way it is
    the game, and it uploads with a comment giving the submitted score.
    """
    left = list(games)
    out = []
    for r in sorted(rounds, key=lambda r: r.get("round") or 0):
        if r.get("score_for") is None:
            continue

        def ok_by_number(g):
            # Same round number, plus either a plausible name or a score in
            # the right ballpark (nicknames like "Dr Bing" defeat any name test).
            gap = abs(g["jesse_score"] - r["score_for"]) + abs(g["opp_score"] - r["score_against"])
            return (g["round"] == r.get("round")
                    and (_loose_name(g["opponent"], r.get("opponent_name")) or gap <= 80))
        g = next((g for g in left if ok_by_number(g)), None)
        if g is None:
            best = max(left, key=lambda g: _fit(g, r), default=None)
            g = best if best is not None and _fit(best, r)[0] >= 2 else None
        if g is not None:
            left.remove(g)
        exact = g is not None and (g["jesse_score"], g["opp_score"]) == (r["score_for"], r["score_against"])
        out.append({
            "round": r.get("round"),
            "opponent": " ".join((r.get("opponent_name") or "").split()),
            "official": [r["score_for"], r["score_against"]],
            "result": r.get("result"),
            "file": g and g["path"],
            "file_score": g and [g["jesse_score"], g["opp_score"]],
            "status": "missing" if g is None else "match" if exact else "score_differs",
        })
    return out


FULL_BAG = 100


def short_racks(path):
    """Jesse's turns recorded with fewer than 7 tiles while the bag still held
    enough to fill his rack - the tiles played rather than the rack held.
    BestBot "analyzes" such a game successfully but meaninglessly, and a
    completed analysis can never be redone (CLAUDE.md), so these block upload.
    """
    with open(path, encoding="utf-8", errors="replace") as f:
        lines = [l.strip() for l in f]
    me = next((m.group(1) for m in map(PLAYER.match, lines)
               if m and _is_jesse(m.group(1), m.group(2))), None)
    on_board, out = 0, []
    for line in lines:
        if not line.startswith(">") or ":" not in line:
            continue
        nick, rest = line[1:].split(":", 1)
        toks = rest.split()
        if len(toks) < 5 or not gp.parse_pos(toks[1]):
            continue  # not a tile placement
        rack, pos, word = toks[0], toks[1], toks[2]
        if nick.strip() == me and len(rack) < 7 and FULL_BAG - 14 - on_board >= 7:
            out.append(f"{rack} at {pos}")
        on_board += sum(1 for ch in word if ch != ".")
    return out


def upload_status(path, on_woogles):
    """(state, [reasons]) for one archived file.

    state is "on_woogles", "ready" or "blocked". Only problems that make an
    upload unsafe block it: preflight's unhealable flags (an unterminated game
    imports as a stuck unfinished game) and short racks. A score that differs
    from the submitted one does not: that uploads with a comment.
    """
    if path in on_woogles:
        return "on_woogles", []
    issues, _ = gp.scan_file(path)
    reasons = [f"{kind}: {detail}" for kind, detail, healed in issues if not healed]
    reasons += [f"short rack: {r}" for r in short_racks(path)]
    return ("blocked" if reasons else "ready"), reasons


def _load(path):
    if not os.path.exists(path):
        return {}
    with open(path) as f:
        return json.load(f)


def woogles_collections(rows):
    """{tourneyid: [(title, uuid)]} - the Woogles collection(s) for each event.

    event-dates.json says outright which event a collection is, so it wins.
    Otherwise a collection belongs to an event when its archive folder (as the
    sync manifest records it) is one the event's games were found in.
    """
    cols = _load(SYNC_MANIFEST).get("collections", {})
    dates = _load(DATES_PATH)
    by_title = {c["title"]: uuid for uuid, c in cols.items()}
    stated = defaultdict(list)
    for key, d in dates.items():
        uuid = key if key in cols else by_title.get(key)
        if uuid and isinstance(d, dict) and d.get("wespa_tourneyid"):
            stated[d["wespa_tourneyid"]].append(uuid)
    claimed = {u for us in stated.values() for u in us}
    out = {}
    for t, _, _, fs, _ in rows:
        tid = t["tourneyid"]
        uuids = stated.get(tid) or [u for u, c in cols.items()
                                     if u not in claimed and c.get("folder") in fs]
        out[tid] = sorted((cols[u]["title"], u) for u in uuids)
    return out


def render(rows):
    woogles = woogles_collections(rows)
    full = sum(1 for _, n, c, _, _ in rows if n and c >= n)
    part = sum(1 for _, n, c, _, _ in rows if 0 < c < n)
    none = sum(1 for _, _, c, _, _ in rows if c == 0)
    games = sum(c for _, _, c, _, _ in rows)
    rounds = sum(n for _, n, _, _, _ in rows)
    out = [
        "# WESPA tournament GCG coverage",
        "",
        "Every WESPA-rated event Jesse Day has played, and how many of its rounds "
        f"have a `.gcg` in [`{ARCHIVE}/`]({quote(ARCHIVE)}/). Files are paired with "
        "official rounds by the round number in the filename and the opponent, else "
        "by score. A file whose final score differs from the one submitted still counts.",
        "",
        f"🟢 all rounds ({full}) · 🟡 some rounds ({part}) · 🔴 none ({none}) · "
        f"**{games} of {rounds} rounds archived**",
        "",
        "Generated by `scripts/wespa_coverage.py` - edit that, not this file.",
        "",
        "| | Date | Event | Record | GCGs | Folder | Woogles |",
        "| :-: | --- | --- | :-: | --: | --- | --- |",
    ]
    for t, n, c, fs, _ in reversed(rows):
        dot = "🟢" if n and c >= n else "🟡" if c else "🔴"
        name = " ".join((t.get("name") or "").split())
        event = f"[{name}]({wt.TOURNEY_PAGE.format(t['tourneyid'])})"
        record = f"{int(t.get('wins') or 0)}-{int(t.get('losses') or 0)}"
        links = ", ".join(f"[{os.path.relpath(f, ARCHIVE)}]({quote(f)}/)" for f in fs)
        cols = ", ".join(f"[{title}]({COLLECTION_URL.format(u)})" for title, u in woogles[t["tourneyid"]])
        out.append(f"| {dot} | {t.get('date') or ''} | {event} | {record} | {c}/{n} | {links} | {cols} |")
    return "\n".join(out) + "\n"


def triage(rows):
    """Everything the archive-triage page shows, as plain data."""
    on_woogles = {g["path"] for g in _load(SYNC_MANIFEST).get("games", {}).values()}
    woogles = woogles_collections(rows)
    events = []
    for t, n, c, fs, detail in reversed(rows):
        used = {d["file"] for d in detail if d["file"]}
        for d in detail:
            if d["file"]:
                d["upload"], d["reasons"] = upload_status(d["file"], on_woogles)
        unpaired = []
        for f in fs:
            for p in sorted(glob.glob(os.path.join(glob.escape(f), "*.gcg"))):
                if p not in used:
                    g = read_game(p)
                    unpaired.append({"file": p, "file_score": g and [g["jesse_score"], g["opp_score"]],
                                     "opponent": g and g["opponent"]})
        events.append({
            "tourneyid": t["tourneyid"],
            "name": " ".join((t.get("name") or "").split()),
            "date": t.get("date"),
            "record": f"{int(t.get('wins') or 0)}-{int(t.get('losses') or 0)}",
            "wespa_url": wt.TOURNEY_PAGE.format(t["tourneyid"]),
            "folders": fs,
            "collections": [{"title": ti, "url": COLLECTION_URL.format(u)} for ti, u in woogles[t["tourneyid"]]],
            "rounds": detail,
            "unpaired": unpaired,
        })
    return {"events": events}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--player", default="Jesse Day")
    ap.add_argument("--stdout", action="store_true")
    ap.add_argument("--json", metavar="PATH", help="also write per-round triage data here")
    ap.add_argument("--page", metavar="PATH",
                    help="also write the archive-triage page (scripts/archive_triage.html, filled in) here")
    args = ap.parse_args(argv)
    playerid, _ = wt.resolve_player(args.player)
    if not playerid:
        sys.exit(f"No WESPA player found for {args.player!r}")
    rows = coverage(playerid)
    if args.json:
        with open(args.json, "w") as f:
            json.dump(triage(rows), f, indent=1)
        print(f"Wrote {args.json}")
    if args.page:
        data = triage(rows)
        data["generated"] = date.today().isoformat()
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "archive_triage.html")) as f:
            page = f.read()
        # "</" inside inlined JSON would end the <script> element early.
        blob = json.dumps(data).replace("</", "<\\/")
        with open(args.page, "w") as f:
            f.write(page.replace("__TRIAGE_DATA__", blob))
        print(f"Wrote {args.page}")
    md = render(rows)
    if args.stdout:
        print(md, end="")
    else:
        with open(OUT_PATH, "w") as f:
            f.write(md)
        print(f"Wrote {OUT_PATH}")


if __name__ == "__main__":
    main()
