#!/usr/bin/env python3
"""Mirror Jesse's Woogles collections into the repo's .gcg archive.

Games are now usually annotated on Woogles first, so the archive here would
drift behind unless something pulls them down. This walks every collection on
the profile and makes sure each finished game has a .gcg in the repo.

Which copy is canonical depends on where a game started, recorded per game in
`.github/woogles-archive.json`:

- **origin "repo"** - the game already had a file here when the sync first saw
  it (the older archive, uploaded *from* these files). The file stays as it is:
  the Woogles copy may have been healed by gcg_preflight on the way up, and
  the original is the record.
- **origin "woogles"** - the sync created the file. Woogles is canonical, so a
  later edit there (annotations, a corrected rack) is written back over it.

A game is recognised as already archived by its move sequence, not its file
name or nicknames, with a looser opening-moves key as a fallback for files
that were repaired on upload, and last the final score within the
collection's own folder (a game edited heavily on Woogles after upload).
A new file whose name is already taken (a Curley game number) is likewise
that game, and is never overwritten.

New files land in the folder the collection's other games already live in;
for a collection with none archived yet:

    league seasons        Woogles League/Season <N>/
    the Curley collection Practice Games/James Curley/<N>_<mon><day>_<YY>.gcg
    anything else         Tournament Games/<year>/<collection title>/

Override a collection's folder by editing its "folder" in the manifest.

    python3 scripts/sync_woogles_archive.py --dry-run
    python3 scripts/sync_woogles_archive.py
"""

import argparse
import glob
import hashlib
import json
import os
import re
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fetch_woogles_snapshot as fw  # noqa: E402  — API transport (key, retries)
import verify_gcg as vg  # noqa: E402  — the shared GCG event parser

MANIFEST = ".github/woogles-archive.json"
DATES = ".github/event-dates.json"
ROOTS = ("Tournament Games", "Practice Games", "Woogles League")
CURLEY_TITLE = "James Curley practice games"
LEAGUE = re.compile(r"League - Season (\d+)")
CURLEY_CHAPTER = re.compile(r"Game #(\d+) - (\d{4}-\d{2}-\d{2})")
MAX_WORKERS = 8  # CLAUDE.md: keep Woogles fan-out at 10 or fewer


def moves(text):
    """(nick, pos, word, score, cum) per move line, leniently.

    Deliberately not verify_gcg.parse_events: that is an upload gate and
    rejects older archive files (an UNKNOWN rack placeholder, a Quackle
    oddity) that are still perfectly good evidence of which game this is.
    """
    out = []
    for line in text.splitlines():
        if not line.startswith(">") or ":" not in line:
            continue
        nick, rest = line[1:].split(":", 1)
        toks = rest.split()
        nums = [t for t in toks if re.fullmatch(r"[+-]?-?\d+", t)]
        if len(nums) < 2:
            continue
        pos, word = (toks[1], toks[2]) if len(toks) >= 5 and vg.parse_pos(toks[1]) else ("", "")
        out.append((nick.strip(), pos, word, nums[-2], int(nums[-1])))
    return out


def signatures(text):
    """(exact, loose) keys for a GCG's game, both nickname- and rack-blind.

    exact: every move's position, word, score and cumulative.
    loose: the first six plays' positions and words only - survives a rescored
    turn or a healed endgame line, which is what upload repairs change.
    """
    ms = moves(text)
    exact = [m[1:] for m in ms]
    loose = [m[1:3] for m in ms if m[1]][:6]
    h = lambda x: hashlib.sha1(repr(x).encode()).hexdigest()  # noqa: E731
    return (h(exact) if exact else None), (h(loose) if len(loose) >= 6 else None)


def final_score(text):
    """The two final cumulative scores, order-free, or None."""
    cum = {m[0]: m[4] for m in moves(text)}
    return tuple(sorted(cum.values())) if len(cum) == 2 else None


def folder_scores(folder):
    """{final score: path} for the .gcg files directly in a folder."""
    out = {}
    for path in glob.glob(os.path.join(glob.escape(folder), "*.gcg")):
        with open(path, encoding="utf-8", errors="replace") as f:
            key = final_score(f.read())
        if key:
            out.setdefault(key, path)
    return out


def index_repo():
    """{key: path} for every archived .gcg, exact and loose keys together."""
    idx = {}
    for root in ROOTS:
        for path in glob.glob(os.path.join(root, "**", "*.gcg"), recursive=True):
            with open(path, encoding="utf-8", errors="replace") as f:
                for key in signatures(f.read()):
                    if key:
                        idx.setdefault(key, path)
    return idx


def load_manifest():
    if os.path.exists(MANIFEST):
        with open(MANIFEST) as f:
            return json.load(f)
    return {"collections": {}, "games": {}}


def save_manifest(m):
    with open(MANIFEST, "w") as f:
        json.dump(m, f, indent=2, sort_keys=True, ensure_ascii=False)
        f.write("\n")


def fetch_gcg(game_id):
    """The game's GCG, or None while it is unfinished (GetGCG refuses those)."""
    try:
        return fw.post("game_service.GameMetadataService/GetGCG", {"game_id": game_id}).get("gcg")
    except Exception:  # noqa: BLE001 — any failure just means "not this run"
        return None


def safe(name):
    """A chapter or collection title as a file/folder name."""
    return re.sub(r"\s+", " ", re.sub(r'[/\\:*?"<>|]', "-", name)).strip(" .")


def event_dates(title, uuid):
    if not os.path.exists(DATES):
        return {}
    with open(DATES) as f:
        dates = json.load(f)
    return dates.get(uuid) or dates.get(title) or {}


def event_label(title, uuid):
    """The short name event-dates.json gives the event, if any."""
    return event_dates(title, uuid).get("label")


def event_year(title, uuid):
    """The year the event was played: event-dates.json, else the title."""
    d = event_dates(title, uuid).get("date")
    if d:
        return d[:4]
    m = re.search(r"\b(20\d\d)\b", title) or re.search(r"'(\d\d)\b", title)
    if m:
        return m.group(1) if len(m.group(1)) == 4 else "20" + m.group(1)
    return None


def default_folder(col):
    title = col["title"]
    m = LEAGUE.search(title)
    if m:
        return f"Woogles League/Season {m.group(1)}"
    if title == CURLEY_TITLE:
        return "Practice Games/James Curley"
    year = event_year(title, col["uuid"]) or col.get("created_at", "")[:4]
    return f"Tournament Games/{year}/{safe(event_label(title, col['uuid']) or title)}"


def file_name(col, chapter):
    m = CURLEY_CHAPTER.match(chapter.get("chapter_title") or "")
    if col["title"] == CURLEY_TITLE and m:
        d = date.fromisoformat(m.group(2))
        return f"{m.group(1)}_{d.strftime('%b').lower()}{d.day}_{d.strftime('%y')}.gcg"
    title = chapter.get("chapter_title") or f"Chapter {chapter['chapter_number']}"
    return f"{chapter['chapter_number']:02d} {safe(title)}.gcg"


def sync(dry_run=False):
    manifest = load_manifest()
    games_m, cols_m = manifest["games"], manifest["collections"]
    repo = index_repo()
    written = []

    collections = fw.enumerate_collections(None, None)
    for col in collections:
        full = fw.post("collections_service.CollectionsService/GetCollection",
                       {"collection_uuid": col["uuid"]})["collection"]
        chapters = full.get("games") or []
        entry = cols_m.setdefault(col["uuid"], {})
        entry["title"] = col["title"]

        # Only games we must read: never seen, or Woogles-canonical ones whose
        # annotations may have changed. A finished repo-origin game is settled.
        todo = [c for c in chapters
                if c["game_id"] not in games_m
                or games_m[c["game_id"]]["origin"] == "woogles"]
        with ThreadPoolExecutor(MAX_WORKERS) as ex:
            gcgs = dict(zip((c["game_id"] for c in todo),
                            ex.map(lambda c: fetch_gcg(c["game_id"]), todo)))

        # Match first, so the collection's folder can be inferred from where its
        # already-archived games live before any new file is placed.
        for c in todo:
            gid, text = c["game_id"], gcgs[c["game_id"]]
            if gid in games_m or not text:
                continue
            path = next((repo[k] for k in signatures(text) if k and k in repo), None)
            if path:
                games_m[gid] = {"path": path, "origin": "repo"}
        homes = Counter(os.path.dirname(games_m[c["game_id"]]["path"])
                        for c in chapters if c["game_id"] in games_m)
        folder = entry.get("folder") or (homes.most_common(1)[0][0] if homes else default_folder(col))
        entry["folder"] = folder
        by_score = folder_scores(folder)

        for c in todo:
            gid, text = c["game_id"], gcgs[c["game_id"]]
            if not text:
                continue  # unfinished, or a transient failure: next run
            rec = games_m.get(gid)
            if rec and rec["origin"] == "repo":
                continue
            digest = hashlib.sha1(text.encode()).hexdigest()
            if rec and rec.get("sha1") == digest:
                continue
            if not rec:
                path = os.path.join(folder, file_name(col, c))
                match = path if os.path.exists(path) else by_score.get(final_score(text))
                if match:
                    games_m[gid] = {"path": match, "origin": "repo"}
                    continue
            path = rec["path"] if rec else os.path.join(folder, file_name(col, c))
            written.append((col["title"], path, "updated" if rec else "new"))
            games_m[gid] = {"path": path, "origin": "woogles", "sha1": digest}
            if not dry_run:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "w", encoding="utf-8") as f:
                    f.write(text if text.endswith("\n") else text + "\n")

    for title, path, kind in written:
        print(f"{kind:8} {path}   ({title})")
    print(f"{len(written)} file(s) {'would be ' if dry_run else ''}written; "
          f"{sum(1 for g in games_m.values() if g['origin'] == 'repo')} games matched existing files")
    if not dry_run:
        save_manifest(manifest)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="report, write nothing")
    sync(ap.parse_args(argv).dry_run)


if __name__ == "__main__":
    main()
