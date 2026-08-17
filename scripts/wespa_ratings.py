"""WESPA over-the-board ratings: fetch, cache, and look up by name.

The only module here that talks to wespa.xerafin.net — the live successor to
Aardvark, which WESPA's own site links to as "Official OTB Ratings". (The old
legacy.wespa.org/aardvark host is dead; don't reach for it.)

Two upstream sources, because neither alone is sufficient:

  * `/api/players.php?idsonly=1` — the whole roster as JSON in one request
    (~9,100 players, ~690 KB): playerid, full name, country, current rating.
    The playerid is what a profile link needs, and the names are untruncated,
    which is what makes name matching viable. Takes no query parameters — it is
    all-or-nothing by design, meant to be cached and searched locally, which is
    exactly what their own site does.
  * `latest.txt` — the current rating run as fixed-width text. Carries the one
    thing the JSON omits: the player's title (GM/IM/M). Names here are truncated
    to 20 characters, so it is joined *onto* the JSON roster rather than used as
    the roster itself.

The two are joined on the truncated name. Measured against a live snapshot,
8,719 of 9,088 roster entries match; the misses are historical players absent
from the current rating run, who keep their rating and simply read as untitled,
which is correct. Only three truncated keys collide, and each of those is one
person entered twice upstream (see the site's own duplicates.txt), so the title
lands on the right human regardless.

Where the two disagree on rating, the JSON wins: it is what the site's own
player page displays, and it moves first.

Cached to data/wespa-ratings.json, which is gitignored — this repo is public and
the cache is a bulk copy of someone else's dataset. Refreshed daily by the
existing report workflow; ratings only move when a rating run completes, so a
stale cache is never wrong by much and a missing one is never fatal.
"""
import json
import os
import random
import re
import sys
import time

import requests

BASE = "https://wespa.xerafin.net"
ROSTER_URL = f"{BASE}/api/players.php?idsonly=1"
LATEST_URL = f"{BASE}/latest.txt"
PROFILE_URL = f"{BASE}/player.html?id={{}}"
CACHE_PATH = "data/wespa-ratings.json"
# Committed, unlike the ratings cache: it is small, hand-confirmed, and worth
# keeping under review. See load_aliases for why it exists and why it is public.
ALIAS_PATH = ".github/wespa-aliases.json"

# The host 403s the default requests User-Agent. It serves the same public data
# to a browser, so this identifies us as one rather than working around any
# access control.
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                         "AppleWebKit/537.36 (KHTML, like Gecko) "
                         "Chrome/126.0.0.0 Safari/537.36"}

# Name truncation width in latest.txt, and the layout of one of its rows:
# a 4-char nick and 3-char country code, then the payload — name, games played,
# rating, last-played date, deviation, title, norm marker (norms unused).
LATEST_NAME_WIDTH = 20
_LATEST_PREFIX = re.compile(r"^(.{4})\s(.{3})\s")
_LATEST_ROW = re.compile(
    r"^(.+?)\s+(\d+)\s+(\d+)\s+(\d{8})\s+(\d+)\s+(GM|IM|M|--)\s+(\*\*|\*|--)?\s*$"
)

_CACHE = None


def _norm(s):
    """Fold a name for matching. Deliberately identical in shape to
    tournament_report._norm_label — accents aside, WESPA and Woogles disagree
    mostly about punctuation and case."""
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _get(url, retries=4):
    """GET with the project's standard backoff posture (CLAUDE.md: exponential
    plus jitter on 429/5xx). Returns None rather than raising — every caller
    here degrades to "no ratings", which must never fail a report."""
    for attempt in range(retries):
        try:
            r = requests.get(url, headers=HEADERS, timeout=30)
            if r.status_code == 429 or r.status_code >= 500:
                if attempt == retries - 1:
                    return None
                time.sleep((2 ** attempt) + random.uniform(0, 0.5))
                continue
            r.raise_for_status()
            return r.text
        except requests.RequestException:
            if attempt == retries - 1:
                return None
            time.sleep((2 ** attempt) + random.uniform(0, 0.5))
    return None


def parse_latest(text):
    """{truncated name: title} from latest.txt, titled players only.

    Unparseable lines are skipped — the first line is a header, and a format
    drift upstream should cost us the title column, not the whole feature.
    """
    out = {}
    for line in (text or "").splitlines():
        if not line.strip() or not _LATEST_PREFIX.match(line):
            continue
        m = _LATEST_ROW.match(line[9:])
        if not m:
            continue
        title = m.group(6)
        if title != "--":
            out[m.group(1).strip()] = title
    return out


def build(roster_json, latest_text):
    """Join the roster with the current rating run into cacheable player rows."""
    titles = parse_latest(latest_text)
    players = []
    for p in (roster_json or {}).get("players", []):
        name = (p.get("name") or "").strip()
        if not name or not p.get("playerid"):
            continue
        title = titles.get(name[:LATEST_NAME_WIDTH].strip())
        players.append({
            "playerid": p["playerid"],
            "name": name,
            "country": p.get("country"),
            "rating": p.get("cswrating"),
            "title": title,
        })
    return players


def refresh():
    """Fetch both sources and rewrite the cache. Returns the player list, or
    None if the roster could not be fetched (the cache is left untouched)."""
    raw = _get(ROSTER_URL)
    if raw is None:
        return None
    try:
        roster = json.loads(raw)
    except ValueError:
        return None
    # The title feed is optional: without it every player simply reads as
    # untitled, which is a smaller loss than having no ratings at all.
    players = build(roster, _get(LATEST_URL))
    if not players:
        return None
    os.makedirs(os.path.dirname(CACHE_PATH) or ".", exist_ok=True)
    tmp = CACHE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"fetched": time.time(), "players": players}, f)
    os.replace(tmp, CACHE_PATH)  # never leave a half-written cache behind
    return players


def _index(players):
    """{normalized name: row}, dropping any name that maps to more than one
    distinct player.

    This is the guardrail on automatic matching: nothing here is curated by
    hand, so an ambiguous name must resolve to nothing rather than to a coin
    flip that would eventually attach a stranger's rating to an opponent. It
    costs almost nothing — of ~9,100 players only three names repeat, and each
    of those is one person entered twice upstream.
    """
    seen = {}
    for p in players:
        key = _norm(p["name"])
        if not key:
            continue
        if key in seen and seen[key] and seen[key]["playerid"] != p["playerid"]:
            seen[key] = None
        else:
            seen.setdefault(key, p)
    return {k: v for k, v in seen.items() if v}


def load_aliases():
    """{normalized label: playerid or None} — the confirmed-match catalog.

    Exists because a name in a GCG player line and a name in the WESPA roster
    disagree in ways no algorithm should be trusted to reconcile: a spelling
    drift ("Anand Buddhev" / "Anand Buddhdev"), or a different name altogether
    ("Henry Yeo" is "Yeo Kien Hung" upstream). Automatic matching that stretched
    far enough to catch those would also be loose enough to attach a stranger's
    rating to an opponent, which is the one outcome worth engineering against.

    So each entry is asserted by a human, not inferred. `--suggest` proposes
    candidates; Jesse says yes or no; the answer lands here. A **null** value is
    a recorded "no": it says this label has no WESPA player (a nickname like
    "Bnjy", a one-off opponent) and stops it being proposed again.

    Safe to commit, unlike the Woogles name registry: both halves of an entry —
    a real name and a public WESPA id — already appear in the rendered report.
    """
    try:
        with open(ALIAS_PATH) as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return {}
    return {_norm(k): v for k, v in (raw.get("aliases") or {}).items()}


def load(force=False):
    """The name index, read from the cache. Returns an empty dict when there is
    no cache, in which case callers render exactly as they did before this
    feature existed.

    Deliberately does *not* fetch. Refreshing is an explicit step (`--refresh`,
    run by the report workflow), so rendering a report never depends on WESPA
    being reachable, and never surprises an offline run with a 690 KB download.
    A cache that is days stale costs a handful of rating points; a hidden network
    dependency in the render path costs the whole report.
    """
    return _loaded(force)["by_name"]


def _loaded(force=False):
    global _CACHE
    if _CACHE is not None and not force:
        return _CACHE
    try:
        with open(CACHE_PATH) as f:
            players = json.load(f).get("players") or []
    except (OSError, ValueError):
        players = []
    _CACHE = {
        "players": players,
        "by_name": _index(players),
        # Keyed by id, so an alias can reach a player whose *name* is ambiguous —
        # the id is unambiguous by definition, which is the whole point of
        # confirming a match as an id rather than as a spelling.
        "by_id": {p["playerid"]: p for p in players},
    }
    return _CACHE


def cache_age_days():
    """Age of the cache in days, or None if there is no readable cache."""
    try:
        with open(CACHE_PATH) as f:
            return (time.time() - json.load(f)["fetched"]) / 86400.0
    except (OSError, ValueError, KeyError):
        return None


def lookup(name):
    """The WESPA row for a player, or None.

    Two exact paths, in order: a confirmed alias, then the roster name itself.
    Never fuzzy — a near-match is a suggestion for a human (see `suggest`), not
    an answer.
    """
    try:
        key = _norm(name)
        aliases = load_aliases()
        if key in aliases:
            pid = aliases[key]
            if pid is None:
                return None  # recorded as having no WESPA entry
            return _loaded()["by_id"].get(pid)
        return load().get(key)
    except Exception:
        return None  # a report must never fail because WESPA misbehaved


def _edit_distance(a, b, limit=3):
    """Levenshtein, abandoned once it exceeds `limit` — this runs against every
    roster name, so bailing early matters more than an exact large distance."""
    if abs(len(a) - len(b)) > limit:
        return limit + 1
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i] + [0] * len(b)
        for j, cb in enumerate(b, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb))
        if min(cur) > limit:
            return limit + 1
        prev = cur
    return prev[-1]


def suggest(name, limit=4):
    """Ranked WESPA candidates for an unmatched label — a shortlist for a human
    to accept or reject, never applied on its own.

    Two kinds of near-miss show up in practice, so two signals:

      * a spelling drift, caught by edit distance over the whole folded name
        ("Anand Buddhev" -> "Anand Buddhdev");
      * a different *form* of the name, caught by a shared word — reordering,
        a dropped or added given name, a romanization ("Henry Yeo" ->
        "Yeo Kien Hung"). Edit distance is hopeless at these; a shared surname
        is the only real handle on them.

    Tokens shorter than 3 characters are ignored, being initials and particles
    that would match half the roster.
    """
    key = _norm(name)
    tokens = {t for t in re.split(r"[^a-z0-9]+", (name or "").lower()) if len(t) >= 3}
    scored = []
    for p in _loaded()["players"]:
        cand = _norm(p["name"])
        if not cand:
            continue
        d = _edit_distance(key, cand)
        shared = tokens & {t for t in re.split(r"[^a-z0-9]+", p["name"].lower())
                           if len(t) >= 3}
        if d > 2 and not shared:
            continue
        # Fewer edits is better; a shared word outranks a merely similar string.
        scored.append(((0 if d <= 2 else 1), -len(shared), d, p))
    scored.sort(key=lambda s: s[:3])
    return [s[3] for s in scored[:limit]]


def profile_url(player):
    return PROFILE_URL.format(player["playerid"])


def credential(player):
    """A player's WESPA title ("GM"/"IM"/"M"), or "" if they hold none."""
    return (player or {}).get("title") or ""


def opponents_in(snapshot_path):
    """Every distinct opponent label across a snapshot's collections. Reads the
    raw player lines rather than going through tournament_report, so this stays
    usable when a game is too broken to compute stats for."""
    with open(snapshot_path) as f:
        snap = json.load(f)
    names = set()
    for col in snap.get("collections", []):
        for game in col.get("games") or []:
            players = (game.get("history") or {}).get("history", {}).get("players", [])
            for p in players:
                nick = (p.get("real_name") or p.get("nickname") or "").strip()
                if nick:
                    names.add(nick)
    return sorted(names)


def suggest_main(argv):
    """Print unmatched opponents with candidate WESPA matches, for Jesse to
    confirm into the alias catalog. Read-only: it proposes, and never writes."""
    path = "data/golden-snapshot.json"
    if "--snapshot" in argv:
        path = argv[argv.index("--snapshot") + 1]
    try:
        names = opponents_in(path)
    except OSError:
        print(f"wespa: no snapshot at {path}", file=sys.stderr)
        return 1
    aliases = load_aliases()
    unresolved = [n for n in names
                  if not lookup(n) and _norm(n) not in aliases]
    print(f"{len(names) - len(unresolved)}/{len(names)} opponents resolved; "
          f"{len(unresolved)} to confirm\n")
    for n in unresolved:
        cands = suggest(n)
        if not cands:
            print(f"{n}\n    (no candidates)")
        else:
            print(n)
            for c in cands:
                title = f" {c['title']}" if c["title"] else ""
                print(f"    {c['playerid']:>6}  {c['name']} "
                      f"({c['country'] or '??'} {c['rating']}{title})")
        print()
    print(f"Confirmed matches go in {ALIAS_PATH} as \"Label\": playerid; "
          f"use null to record \"no WESPA entry\".")
    return 0


def main(argv):
    if "--refresh" in argv:
        players = refresh()
        if players is None:
            print("wespa: refresh failed (leaving any existing cache in place)",
                  file=sys.stderr)
            return 1
        titled = sum(1 for p in players if p["title"])
        print(f"wespa: cached {len(players)} players "
              f"({titled} titled) -> {CACHE_PATH}")
        return 0
    if "--suggest" in argv:
        return suggest_main(argv)
    if "--lookup" in argv:
        name = argv[argv.index("--lookup") + 1]
        p = lookup(name)
        if not p:
            print(f"wespa: no unique match for {name!r}")
            return 1
        print(json.dumps(p, indent=2))
        print(profile_url(p))
        return 0
    print(__doc__.strip().split("\n")[0])
    print("\nusage: wespa_ratings.py --refresh"
          "\n       wespa_ratings.py --lookup NAME"
          "\n       wespa_ratings.py --suggest [--snapshot PATH]")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
