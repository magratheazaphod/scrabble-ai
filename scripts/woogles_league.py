#!/usr/bin/env python3
"""Turn a Woogles league season into a report-ready collection.

A league season is a schedule inside one division: games are played
asynchronously over two weeks, and each finished game is auto-analyzed by
BestBot. So unlike a tournament there is nothing to upload and no analysis to
request — the only missing piece is the *collection* that the existing report
pipeline consumes. This module builds it:

    python3 scripts/woogles_league.py                    # current CSW season
    python3 scripts/woogles_league.py --season 18        # a specific season
    python3 scripts/woogles_league.py --league nwl       # a different league
    python3 scripts/woogles_league.py --dry-run          # show the plan only

Re-running is safe and is in fact the intended mode: a season is synced while it
is still in progress, so each run adds whatever games have finished since and
re-seeds the ordering. Only genuinely-changed titles and orderings are written.

**Division size and game count — do not infer one from the other.** Divisions are
NOT all the same size, and a season's schedule is capped at **14 games** (one per
day of the 14-day season, and new seasons start every 14 days). So:

  * a division of 15 or fewer plays a full round robin, size − 1 games — 13
    players is 12 games, 14 players is 13;
  * a division of 16 or 17 is capped at 14 and is therefore *not* a full round
    robin — some pairs never meet.

Season 18 ran divisions of 13 through 17, i.e. 12- to 14-game schedules, all in
the same season. A player with fewer games than the season's leader has very
probably just finished a smaller division's full schedule — check
`games_remaining` on their standing before calling anything unplayed, and never
compare raw game counts across divisions as though they meant the same thing.
This bit a report summary once (2026-07-24), which read a complete 12-game
Division 14 season as "2 games unplayed" against a 14-game leader.

**Ordering.** A league schedule has no meaningful round order — the "round"
number Woogles reports is 0 for every game, and games finish in whatever order
the two players got to them. Ordering by date would therefore be arbitrary. So
games are instead sequenced by *opponent strength*, strongest first, and the
chapter title encodes that as "Seed <n>". The report's ordering column then reads
as "how did I do against the 1st seed, the 2nd seed, ..." — which is the question
worth asking of a round robin. tournament_report.compute_game parses "Seed <n>"
into its `round` field, so the rest of the pipeline needs no league awareness.

Seeds rank the whole division, subject included — a seed is a property of a
player in the division, not of a slot in one person's schedule, and the subject
being the only unseeded row was the odd one out. So the played-game seeds skip
exactly one number: the subject's own.

Strength is each player's Woogles rating *in the format the league is played
in* — CSW correspondence for the Collins league — since that is the rating these
very games move. Ratings are read at sync time, so a season synced mid-flight and
re-synced at the end can legitimately re-seed as ratings shift; the rating used
is recorded alongside the seed so any report says what it was seeded on.

**Standings.** `league_section` renders the division exactly as woogles.io
shows it — live standing order, one row per participant, seed and head-to-head
demoted to columns — and it leads the report rather than trailing it.

**Cross-check.** `cross_check_rows` recomputes the quantities Woogles publishes
per division (woogles.io/leagues/csw) from the analyzed games and diffs them,
catching both a bug here and a game the collection is missing. It runs on every
render but reports to stderr; only a disagreement reaches the report.

**League-wide leaderboard.** `mistake_leaderboard_section` ranks every player in
the season, across all divisions, by average mistakes score — the one figure that
survives the division tiering, since it is measured against BestBot rather than
against whoever was across the board. Top 10 plus the subject's own placement.
Report-only by construction: the only text this module writes back to Woogles is
a collection title and description, and neither is derived from the leaderboard.
"""
import argparse
import json
import os
import random
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import requests

BASE = "https://woogles.io/api"
PROFILE_UUID_LEN = 22  # Woogles account ids are 22-char opaque keys

# Where the sync records what it built, so the report side can find the league
# context (division, standings, seeds) for a collection it only knows by uuid.
LEAGUE_STATE_PATH = "data/league-collections.json"

DEFAULT_USERNAME = "magrathean"
DEFAULT_USER_UUID = "7WyqZfyQuB6SwNa2XjuZUG"

# Only finished games belong in a collection; the rest are still being played.
FINISHED_RESULTS = {"win", "loss", "draw"}


def load_dotenv(path=".env"):
    """Populate os.environ from a .env file (gitignored; holds WOOGLES_API_KEY)."""
    if not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _headers():
    return {
        "Content-Type": "application/json",
        "X-Api-Key": os.environ["WOOGLES_API_KEY"],
    }


def post(endpoint, body, retries=4):
    """POST to a Woogles RPC endpoint, backing off on rate limits and 5xx."""
    for attempt in range(retries):
        r = requests.post(f"{BASE}/{endpoint}", json=body, headers=_headers())
        if r.status_code == 429 or r.status_code >= 500:
            if attempt == retries - 1:
                r.raise_for_status()
            time.sleep((2**attempt) + random.uniform(0, 0.5))
            continue
        r.raise_for_status()
        return r.json()


# --------------------------------------------------------------------------
# League reads
# --------------------------------------------------------------------------


def get_leagues():
    return post("league_service.LeagueService/GetAllLeagues", {}).get("leagues", [])


def get_league(slug):
    for lg in get_leagues():
        if lg.get("slug") == slug:
            return lg
    raise SystemExit(f"no league with slug {slug!r} (have: "
                     f"{', '.join(l.get('slug', '?') for l in get_leagues())})")


def resolve_season(league, season_number=None):
    """The league's current season, or a specific numbered one."""
    if season_number is None:
        resp = post("league_service.LeagueService/GetCurrentSeason",
                    {"league_id": league["slug"]})
        return resp["season"]
    resp = post("league_service.LeagueService/GetAllSeasons",
                {"league_id": league["slug"]})
    for s in resp.get("seasons", []):
        if int(s.get("season_number", -1)) == int(season_number):
            return s
    raise SystemExit(f"{league['name']} has no season {season_number}")


def all_division_standings(season_id):
    """Every division in the season, each with its full standings list."""
    resp = post("league_service.LeagueService/GetAllDivisionStandings",
                {"season_id": season_id})
    return resp.get("divisions", [])


def find_player_division(season_id, username, required=True, divisions=None):
    """(division, standing) for `username` in this season, or (None, None).

    `required=False` is for sweeping every league on the platform, most of which
    the player has never entered — not finding them is the normal case there.
    Pass `divisions` to reuse an already-fetched standings read.
    """
    for div in (all_division_standings(season_id) if divisions is None else divisions):
        for standing in div.get("standings", []):
            if standing.get("username", "").lower() == username.lower():
                return div, standing
    if required:
        raise SystemExit(f"{username} is not in any division of season {season_id}")
    return None, None


def player_season_games(user_id, season_id):
    resp = post("league_service.LeagueService/GetPlayerSeasonGames",
                {"user_id": user_id, "season_id": season_id})
    return resp.get("games", [])


# --------------------------------------------------------------------------
# Ratings and seeding
# --------------------------------------------------------------------------


def _rating_lexicon_family(lexicon):
    """Woogles buckets every lexicon generation into one rating key.

    Mirrors transformLexiconName in liwords pkg/entity/ratings.go — CSW24 games
    are rated under "CSW19", NWL23 under "NWL18", and so on. Getting this wrong
    silently yields no rating rather than a wrong one, but then every opponent
    ties at seed 1.
    """
    prefixes = {
        "NWL": "NWL18", "CSW": "CSW19", "ECWL": "ECWL", "NSF": "NSF21",
        "RD": "RD28", "FRA": "FRA20", "DISC": "DISC", "OSPS": "OSPS",
        "FILE": "FILE",
    }
    for prefix, key in prefixes.items():
        if (lexicon or "").upper().startswith(prefix):
            return key
    return lexicon


def rating_key(league):
    """The `<lexicon>.<variant>.<timecontrol>` key league games are rated under.

    Leagues are always correspondence: liwords only permits a time bank on a
    correspondence game (pkg/entity/sought_game.go), and every league sets one.
    """
    settings = league.get("settings") or {}
    family = _rating_lexicon_family(settings.get("lexicon", ""))
    variant = settings.get("variant") or "classic"
    return f"{family}.{variant}.corres"


def player_ratings(username):
    """{rating_key: rating} for one player, or {} if the profile has none."""
    try:
        resp = post("user_service.ProfileService/GetRatings", {"username": username})
    except requests.exceptions.HTTPError:
        return {}
    try:
        data = json.loads(resp.get("json") or "{}").get("Data") or {}
    except ValueError:
        return {}
    return {k: v.get("r") for k, v in data.items() if isinstance(v, dict) and "r" in v}


def seed_order(ratings):
    """{username.lower(): 1-based seed} over a whole division, strongest first.

    Seeds rank the *division*, not the subject's opponent list, so the subject
    holds a seed like everyone else and every player's seed means the same thing
    in the standings table and in the collection's chapter titles. A consequence
    worth expecting: the subject's own seed is missing from the game seeds, so
    the played-game seeds skip exactly one number.

    Unrated players sort last (a missing rating is not a zero rating), with
    username as the final tie-break so the ordering is stable across runs.
    """
    names = sorted(ratings, key=lambda n: (ratings[n] is None,
                                           -(ratings[n] or 0),
                                           n))
    return {n: i for i, n in enumerate(names, start=1)}


def seed_games(games, key, division):
    """Attach a rating and a division-wide seed to each game, strongest first.

    Ratings come from the division standings in one fan-out; an opponent somehow
    absent from them is looked up individually and seeded after everyone rated.
    """
    ratings = division_ratings(division, key)
    seeds = seed_order(ratings)
    seeded = []
    for g in games:
        uname = g["opponent_username"].lower()
        if uname not in ratings:
            ratings[uname] = player_ratings(g["opponent_username"]).get(key)
            seeds = seed_order(ratings)
        seeded.append({**g, "opp_rating": ratings.get(uname),
                       "seed": seeds.get(uname)})
    seeded.sort(key=lambda g: g["seed"])
    return seeded


def chapter_title(g, me_label="JD"):
    """"Seed 3 - Gabzor89 vs JD" — parsed back into a seed and an opponent name
    by tournament_report.compute_game/get_opp_name, so keep both halves intact
    and keep the opponent unadorned (a rating in here would end up in the
    report's Opponent column)."""
    return f"Seed {g['seed']} - {g['opponent_username']} vs {me_label}"


# --------------------------------------------------------------------------
# Collection sync
# --------------------------------------------------------------------------


def collection_title(league, season):
    return f"{league['name']} League - Season {season['season_number']}"


def find_collection(title):
    offset = 0
    while True:
        resp = post("collections_service.CollectionsService/GetUserCollections",
                    {"limit": 50, "offset": offset})
        cols = resp.get("collections", [])
        for c in cols:
            if c.get("title") == title:
                return c
        if len(cols) < 50:
            return None
        offset += 50


def sync_collection(title, description, seeded, dry_run=False):
    """Create the collection if needed, add missing games, fix titles and order.

    Returns (collection_uuid, list of human-readable actions taken).
    """
    actions = []
    col = find_collection(title)
    if col is None:
        actions.append(f"create collection {title!r}")
        if dry_run:
            return None, actions
        uuid = post("collections_service.CollectionsService/CreateCollection",
                    {"title": title, "description": description, "public": True}
                    )["collection_uuid"]
        existing = {}
    else:
        uuid = col["uuid"]
        full = post("collections_service.CollectionsService/GetCollection",
                    {"collection_uuid": uuid})["collection"]
        existing = {g["game_id"]: g for g in (full.get("games") or [])}

    for g in seeded:
        want = chapter_title(g)
        have = existing.get(g["game_id"])
        if have is None:
            actions.append(f"add {want}")
            if not dry_run:
                post("collections_service.CollectionsService/AddGameToCollection",
                     {"collection_uuid": uuid, "game_id": g["game_id"],
                      "chapter_title": want, "is_annotated": False})
        elif have.get("chapter_title") != want:
            actions.append(f"retitle {have.get('chapter_title')!r} -> {want!r}")
            if not dry_run:
                post("collections_service.CollectionsService/UpdateChapterTitle",
                     {"collection_uuid": uuid, "game_id": g["game_id"],
                      "chapter_title": want})

    desired = [g["game_id"] for g in seeded]
    current = [gid for gid in existing] if existing else []
    # Only reorder when the seeded games' relative order actually differs, so a
    # re-run of an unchanged season issues no writes at all.
    current_seeded = [gid for gid in current if gid in set(desired)]
    if current_seeded != desired:
        actions.append(f"reorder {len(desired)} games into seed order")
        if not dry_run:
            post("collections_service.CollectionsService/ReorderGames",
                 {"collection_uuid": uuid, "game_ids": desired})

    return uuid, actions


# --------------------------------------------------------------------------
# Cross-check against the on-platform standings table
# --------------------------------------------------------------------------


def _fmt(v):
    if v is None:
        return "—"
    if isinstance(v, float):
        # Keep one decimal even on a round average (464.0, not 464) so it reads as
        # an average next to the platform's own figure rather than a count.
        s = f"{v:.2f}".rstrip("0")
        return s + "0" if s.endswith(".") else s
    return str(v)


def _close(a, b, tol):
    if a is None or b is None:
        return False
    return abs(a - b) <= tol


def cross_check_rows(stats, agg, standing):
    """[(metric, computed, platform, ok)] comparing the report against Woogles.

    Averages are recomputed from the platform's own totals rather than read off
    the rendered table, so a divisor disagreement shows up as a difference
    instead of hiding inside a rounded number.
    """
    n = len(stats)
    gp = standing.get("games_played") or 0
    wins = sum(1 for g in stats if g["result"] == "W")
    draws = sum(1 for g in stats if g["result"] == "D")
    losses = n - wins - draws
    spread = sum(g["jesse_score"] - g["opp_score"] for g in stats)

    plat_draws = standing.get("draws") or 0
    plat_record = f"{standing.get('wins', 0)}-{standing.get('losses', 0)}"
    mine_record = f"{wins}-{losses}"
    # Show the draw column on both sides as soon as either side claims one, so a
    # draw the report missed (or invented) surfaces as a record mismatch.
    if plat_draws or draws:
        plat_record += f"-{plat_draws}"
        mine_record += f"-{draws}"

    rows = [
        ("Games", n, gp, n == gp),
        ("Record", mine_record, plat_record, mine_record == plat_record),
        ("Spread", spread, standing.get("spread"), spread == standing.get("spread")),
    ]

    def avg(total):
        return (total / gp) if gp else None

    mine_avg = agg["avg_jesse"]
    plat_avg = avg(standing.get("total_score"))
    rows.append(("Average score", mine_avg, plat_avg and round(plat_avg, 1),
                 _close(mine_avg, plat_avg, 0.05)))

    mine_oavg = agg["avg_opp"]
    plat_oavg = avg(standing.get("total_opponent_score"))
    rows.append(("Average opponent score", mine_oavg, plat_oavg and round(plat_oavg, 1),
                 _close(mine_oavg, plat_oavg, 0.05)))

    rows.append(("Total bingos", agg["total_jb"], standing.get("total_bingos"),
                 agg["total_jb"] == standing.get("total_bingos")))
    rows.append(("Opponent bingos", agg["total_ob"], standing.get("total_opponent_bingos"),
                 agg["total_ob"] == standing.get("total_opponent_bingos")))

    high_game = max((g["jesse_score"] for g in stats), default=None)
    rows.append(("High game", high_game, standing.get("high_game"),
                 high_game == standing.get("high_game")))

    high_turn = max((g.get("jesse_high_turn") or 0 for g in stats), default=None)
    rows.append(("High turn", high_turn, standing.get("high_turn"),
                 high_turn == standing.get("high_turn")))

    # The report deliberately *displays* blanks drawn (a luck indicator: blanks
    # that reached the rack, including any exchanged away or stranded at the end),
    # while the league table counts blanks actually played. Both are computed; only
    # the played figure is comparable, so only it is cross-checked here.
    blanks = sum(g.get("jesse_blanks_played") or 0 for g in stats)
    rows.append(("Blanks played †", blanks, standing.get("blanks_played"),
                 blanks == standing.get("blanks_played")))

    plat_mi = standing.get("avg_mistake_index")
    rows.append(("Average mistakes score", agg["avg_mi"],
                 plat_mi and round(plat_mi, 2), _close(agg["avg_mi"], plat_mi, 0.005)))

    return rows


LEADERBOARD_SIZE = 10

# Analyzed games needed to appear in the leaderboard. Flat on purpose — see
# mistake_leaderboard.
MIN_ANALYZED_GAMES = 6


def _norm_name(s):
    """Analysis `player_name` is the nickname with separators merged out."""
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def bingo_find_counts(user_id, username, season_id, cache=None):
    """(found, missed) bingos for one player across their finished season games.

    Counted exactly as the report's own Bingo Find Rate is: one `played_is_bingo`
    turn is a find, one `missed_bingo` turn is a miss, and the rate is
    found/(found+missed). Unlike `compute_game` this does not re-run
    `validate_bingo` against the history rack — that guard needs a second fetch
    per game (GetGameHistory) purely to catch a rare analysis rack bug, which
    would double the ~150 reads this leaderboard already costs. It is applied
    identically (i.e. not at all) to every player including the subject, so the
    ranking stays internally consistent; on Season 18 the two methods agreed
    exactly, for the subject and for his opponents.

    `cache` is a shared {game_id: analysis} dict — top-10 players play each other,
    and a game analyzed once serves both of its players.
    """
    if cache is None:
        cache = {}
    games = [g for g in player_season_games(user_id, season_id)
             if g.get("result") in FINISHED_RESULTS]
    want = _norm_name(username)
    found = missed = 0
    for g in games:
        gid = g["game_id"]
        if gid not in cache:
            try:
                cache[gid] = post("analysis_service.AnalysisService/GetAnalysisResult",
                                  {"game_id": gid})
            except requests.exceptions.HTTPError:
                cache[gid] = None  # never analyzed; excluded from both halves
        res = (cache[gid] or {}).get("result") or {}
        for t in res.get("turns") or []:
            if _norm_name(t.get("player_name")) != want:
                continue
            if t.get("played_is_bingo"):
                found += 1
            if t.get("missed_bingo"):
                missed += 1
    return found, missed


def _find_rate(found, missed):
    total = found + missed
    if not total:
        return "N/A"
    return f"{found}/{total} ({round(found / total * 100, 1)}%)"


def mistake_leaderboard(divisions, username):
    """(rows, my_row) — every league player ranked by mistakes score.

    A season's divisions are skill-tiered, so the division table only ever says
    how the player is doing against their own tier. Mistakes score is the one
    figure that *is* comparable across tiers — it measures play against BestBot's
    optimal, not against whoever happened to be across the board — so it is worth
    ranking league-wide.

    Qualifying takes `MIN_ANALYZED_GAMES` analyzed games — a flat bar, deliberately
    not a fraction of anyone's schedule. Some cut is needed, since a player two
    games in can post a freak 1.8 average that would otherwise head the table, but
    a proportional bar has to reckon with divisions differing in size and the
    schedule capping at 14 games (see the module docstring), and a small division's
    complete season is then shorter than a large one's. A flat 6 sidesteps that
    entirely: it is a real sample by any division's standard, and no one who has
    played a reasonable share of any schedule is excluded by it. The subject is
    returned separately and is never filtered out, so a report always says where
    its subject stands even when they haven't yet qualified (`rank` is then None).

    Rows are dicts with rank/username/user_id/division/avg_mi/games; rank is dense
    over the qualified list, ties broken by more games then username so the
    ordering is stable across runs.
    """
    everyone = [
        {**s, "division_number": d.get("division_number")}
        for d in divisions
        for s in (d.get("standings") or [])
    ]
    def row(s, rank):
        return {"rank": rank, "username": s.get("username"), "user_id": s.get("user_id"),
                "division": s.get("division_number"), "avg_mi": s.get("avg_mistake_index"),
                "games": s.get("games_analyzed") or 0}

    qualified = sorted(
        (s for s in everyone
         if s.get("avg_mistake_index") is not None
         and (s.get("games_analyzed") or 0) >= MIN_ANALYZED_GAMES),
        key=lambda s: (s["avg_mistake_index"], -(s.get("games_analyzed") or 0),
                       (s.get("username") or "").lower()),
    )
    rows = [row(s, i) for i, s in enumerate(qualified, start=1)]
    me = next((r for r in rows if (r["username"] or "").lower() == username.lower()), None)
    if me is None:
        # Unqualified (or unanalyzed) — show the subject with no rank rather than
        # dropping them, which would silently answer "where do I fit" with nothing.
        mine = next((s for s in everyone
                     if (s.get("username") or "").lower() == username.lower()), None)
        if mine is not None and mine.get("avg_mistake_index") is not None:
            me = row(mine, None)
    return rows, me


def attach_bingo_rates(rows, season_id, max_workers=6):
    """Fill each row's `find_rate` in place, fetching the analyses concurrently.

    Only ever called on the handful of rows a report actually displays (top 10
    plus the subject) — computing it for all ~200 league players would be a few
    thousand reads for numbers nothing prints.
    """
    cache = {}
    lock = threading.Lock()

    def one(r):
        # A per-call cache view keeps the shared dict's writes under the lock while
        # leaving the HTTP calls themselves fully concurrent.
        found, missed = bingo_find_counts(r["user_id"], r["username"], season_id,
                                          cache=_LockedCache(cache, lock))
        r["found"], r["missed"] = found, missed
        r["find_rate"] = _find_rate(found, missed)

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        list(ex.map(one, [r for r in rows if r.get("user_id")]))
    return rows


class _LockedCache(dict):
    """dict view over a shared game_id -> analysis cache, safe across threads."""

    def __init__(self, shared, lock):
        super().__init__()
        self._shared, self._lock = shared, lock

    def __contains__(self, k):
        with self._lock:
            return k in self._shared

    def __getitem__(self, k):
        with self._lock:
            return self._shared[k]

    def __setitem__(self, k, v):
        with self._lock:
            self._shared[k] = v


def mistake_leaderboard_section(divisions, season, league, username):
    """Markdown for the league-wide mistakes-score leaderboard.

    Report-only: nothing here is written back to Woogles. The sync writes exactly
    two pieces of text to the platform — the collection title and description
    (`sync_collection`) — and neither is built from this. Keep it that way; the
    league standings page publishes per-division tables only, and a cross-division
    ranking of named players is not ours to publish on their behalf.
    """
    rows, me = mistake_leaderboard(divisions, username)
    if not rows:
        return ""
    total_players = sum(len(d.get("standings") or []) for d in divisions)

    # The top 10 is chosen on mistakes score alone, as asked; the bingo find rate
    # is a column on those 10, never a factor in who makes the cut.
    shown = rows[:LEADERBOARD_SIZE]
    if me is not None and me not in shown:
        shown = shown + [me]
    attach_bingo_rates(shown, season["uuid"])

    lines = ["## League-Wide Mistakes Score Leaderboard", ""]
    lines.append(
        f"*Lowest average mistakes score across all {len(divisions)} divisions of "
        f"{league['name']} League Season {season['season_number']} "
        f"({len(rows)} of {total_players} players qualified). Divisions are "
        f"skill-tiered, so records and spreads aren't comparable between them — "
        f"but mistakes score is measured against BestBot's optimal play rather "
        f"than against the opponent, so it is. Ranking is on mistakes score alone; "
        f"bingo find rate is shown alongside, computed over the same season games "
        f"the way the report computes it for the subject.*"
    )
    lines.append("")
    lines.append("| # | Player | Div | Avg Mistakes | Bingo Find Rate | Games |")
    lines.append("|---|---|---|---|---|---|")

    def emit(r, bold):
        b = "**" if bold else ""
        rank = str(r["rank"]) if r["rank"] else "—"
        cells = [rank, r["username"], r["division"], _fmt(round(r["avg_mi"], 2)),
                 r.get("find_rate", "—"), r["games"]]
        lines.append("| " + " | ".join(f"{b}{c}{b}" for c in cells) + " |")

    for r in rows[:LEADERBOARD_SIZE]:
        emit(r, me is not None and r["username"] == me["username"])
    if me is not None and (me["rank"] is None or me["rank"] > LEADERBOARD_SIZE):
        lines.append("| … | | | | | |")
        emit(me, True)
    lines.append("")

    if me is None:
        lines.append(
            f"*{username} has no analyzed games in this season yet, so no "
            "league-wide placement can be computed.*"
        )
    elif me["rank"] is None:
        lines.append(
            f"*{username} is unranked here: the table counts only players with at "
            f"least {MIN_ANALYZED_GAMES} analyzed games this season, and {username} "
            f"has {me['games']}. The figures shown are over those games.*"
        )
    else:
        lines.append(
            f"**{username} ranks {_ordinal(me['rank'])} of {len(rows)}** qualified "
            f"players league-wide on mistakes score ({_fmt(round(me['avg_mi'], 2))} "
            f"over {me['games']} games), with a bingo find rate of "
            f"{me.get('find_rate', '—')}. Qualifying takes "
            f"{MIN_ANALYZED_GAMES} analyzed games, so a player two games into the "
            "season can't top the table on a small sample."
        )
    return "\n".join(lines)


def division_ratings(division, key, max_workers=8):
    """{username.lower(): rating} for every player in the division, read live.

    One GetRatings per player (a division is 12-17 people), fanned out. Ratings
    move during a season and the standings are read live too, so the table shows
    the division as it stands right now rather than as it was seeded.
    """
    names = [s.get("username") for s in (division.get("standings") or []) if s.get("username")]
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        ratings = list(ex.map(lambda n: player_ratings(n).get(key), names))
    return {n.lower(): r for n, r in zip(names, ratings)}


def head_to_head(stats):
    """{opponent username.lower(): {result, spread, game_url}} from played games.

    Keyed on the Woogles username rather than the display name because that is
    what the standings table carries; a division member Jesse hasn't played yet
    (or the row for Jesse himself) simply won't be in here.
    """
    out = {}
    for g in stats:
        key = (g.get("opp_username") or g.get("opponent") or "").lower()
        out[key] = {
            "result": g["result"],
            "spread": g["jesse_score"] - g["opp_score"],
            "game_url": g.get("game_url"),
        }
    return out


def division_complete(division):
    """True once every player in the division has played their whole schedule.

    `is_complete` is the platform's own flag; `games_remaining` is the fallback
    for a division that has finished but hasn't been marked yet. Division sizes
    differ, so "everyone has 0 left" is the honest test — never a game count.
    """
    if division.get("is_complete"):
        return True
    standings = division.get("standings") or []
    return bool(standings) and all(
        (s.get("games_remaining") or 0) == 0 for s in standings
    )


def league_section(stats, agg, standing, division, season, league):
    """Markdown for the league standings — the report's lead section.

    ONE table, in the division's live standing order (never seed order), with a
    row per participant and seed demoted to a column: Jesse reads this to see
    where the division finished, and an order that doesn't match what woogles.io
    shows him at that moment is worse than no table. Seed covers every player
    (his own row included); head-to-head comes from his own games, links to the
    game, and is blank for anyone he hasn't played.
    """
    # "Final Standings" only once the whole division is done — a mid-season table
    # is a snapshot, and calling a running season final is exactly the error that
    # once had a summary reporting an in-progress placing as a finish.
    heading = "Final Standings" if division_complete(division) else "Standings"
    lines = [
        f"## {heading} — {league['name']} League, Season "
        f"{season['season_number']}, Division {division.get('division_number')}",
        "",
        "Rating is CSW correspondence rating.",
        "",
    ]

    ratings = division_ratings(division, rating_key(league))
    seeds = seed_order(ratings)
    h2h = head_to_head(stats)
    me_name = (standing.get("username") or "").lower()

    lines.append("| # | Player | Rating | Seed | W-L-D | Spread | Avg Mistakes | Head-to-Head |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for s in sorted(division.get("standings") or [], key=lambda s: s.get("rank", 99)):
        uname = s.get("username") or ""
        is_me = uname.lower() == me_name
        name = f"**{uname}**" if is_me else uname
        rating = ratings.get(uname.lower())
        rec = f"{s.get('wins',0)}-{s.get('losses',0)}-{s.get('draws',0)}"
        ami = s.get("avg_mistake_index")
        game = h2h.get(uname.lower())
        seed = seeds.get(uname.lower())
        if game:
            sp = game["spread"]
            h2h_cell = f"{game['result']} {'+' if sp > 0 else ('−' if sp < 0 else '')}{abs(sp)}"
            if game.get("game_url"):
                h2h_cell = f"[{h2h_cell}]({game['game_url']})"
        else:
            h2h_cell = "—"
        lines.append(
            f"| {s.get('rank')} | {name} | {_fmt(rating and round(rating))} | {_fmt(seed)} | "
            f"{rec} | {s.get('spread')} | {_fmt(ami and round(ami, 2))} | {h2h_cell} |"
        )
    lines.append("")

    # The cross-check itself is a background sanity check, not report content:
    # Jesse reads these reports to see his own play, and a table of "our number
    # equals their number" twelve times over is noise once it is reliably passing.
    # It still RUNS on every render — the result goes to stderr (the Actions log),
    # and a disagreement is loud enough to earn its place back in the report.
    rows = cross_check_rows(stats, agg, standing)
    mismatches = [r for r in rows if not r[3]]
    for metric, mine, plat, ok in rows:
        print(f"cross-check {'OK  ' if ok else 'FAIL'} {metric}: report={_fmt(mine)} "
              f"woogles={_fmt(plat)}", file=sys.stderr)
    if mismatches:
        lines.append("### Cross-Check vs the League Table")
        lines.append("")
        lines.append(
            "**"
            + f"{len(mismatches)} of {len(rows)} figures disagree with the woogles.io "
            "league table.** Investigate before trusting the numbers above — a "
            "disagreement usually means the collection is missing a game or holds one "
            "the division doesn't count."
        )
        lines.append("")
        lines.append("| Metric | This report | Woogles league table | |")
        lines.append("|---|---|---|---|")
        for metric, mine, plat, ok in rows:
            lines.append(f"| {metric} | {_fmt(mine)} | {_fmt(plat)} | {'✅' if ok else '❌'} |")
        lines.append("")
        lines.append(
            "† Cross-checked on blanks *played*, to match what the league table counts. "
            "The per-game table above reports blanks **drawn** — blanks that reached the "
            "rack at all, including any exchanged away or left stranded at the end — which "
            "is the better luck indicator and is intentionally the larger number."
        )
    return "\n".join(lines).rstrip()


def report_extras(collection_uuid, stats, agg):
    """League additions for one collection, or {} if it isn't a league season.

    Returns {"round_label", "sections", "digest_line"} — the render arguments plus
    a one-line summary of the cross-check for the report digest, so the Summary
    can state whether the numbers agree with the platform instead of leaving the
    reader to compare two tables by eye.

    The report pipeline can call this unconditionally. Standings are re-read live
    rather than taken from the sync's snapshot: a season is reported on while it
    is still running, so the cross-check is only meaningful against the table as
    it stands when the report is written.
    """
    entry = load_league_state().get(collection_uuid)
    if not entry:
        return {}
    seeds = entry.get("seeds") or {}
    for s in stats:
        s["opp_rating"] = (seeds.get(s.get("game_id")) or {}).get("rating")
    league = get_league(entry["league_slug"])
    season = resolve_season(league, entry.get("season_number"))
    divisions = all_division_standings(season["uuid"])
    division, standing = find_player_division(season["uuid"], entry["username"],
                                              divisions=divisions)

    n_players = len(division.get("standings") or [])
    if division_complete(division):
        placing = f"finished {_ordinal(standing.get('rank'))} of {n_players}"
    else:
        # Say it is provisional, or a summary will report a mid-season position as
        # a final one — the same class of error as reading a smaller division's
        # completed schedule as unplayed games.
        placing = (
            f"currently {_ordinal(standing.get('rank'))} of {n_players} with the "
            f"season still in progress ({standing.get('games_remaining') or 0} games "
            "left) — this is a provisional standing, not a final placing"
        )
    digest_line = (
        f"League: {league['name']} Season {season['season_number']}, Division "
        f"{division.get('division_number')}, {placing}. Games are ordered by opponent "
        f"rating (seed 1 = strongest), not by round."
    )
    # A passing cross-check is deliberately absent from the digest as well as the
    # report: it is internal QA, and the Summary should never spend a sentence
    # telling Jesse his own numbers agree with Woogles'. Only a failure is worth
    # saying out loud.
    rows = cross_check_rows(stats, agg, standing)
    bad = [r[0] for r in rows if not r[3]]
    if bad:
        digest_line += (
            f" WARNING: {len(bad)} of {len(rows)} figures DISAGREE with the woogles.io "
            f"league table ({', '.join(bad)}) — say so plainly in the summary."
        )

    lb_rows, me = mistake_leaderboard(divisions, entry["username"])
    if me is not None and me["rank"] is not None:
        digest_line += (
            f" League-wide mistakes-score leaderboard (all {len(divisions)} divisions, "
            f"lowest average is best): {entry['username']} ranks "
            f"{_ordinal(me['rank'])} of {len(lb_rows)} qualified players at "
            f"{me['avg_mi']:.2f}. This ranking exists only in this report and is "
            "not published on woogles.io."
        )

    # The standings lead the report; the league-wide leaderboard is supplementary
    # and stays below the per-game tables.
    sections = []
    leaderboard = mistake_leaderboard_section(divisions, season, league, entry["username"])
    if leaderboard:
        sections.append(leaderboard)
    return {
        "round_label": "Seed",
        "lead_sections": [league_section(stats, agg, standing, division, season, league)],
        "sections": sections,
        "digest_line": digest_line,
    }


def _ordinal(n):
    if n is None:
        return "?"
    if 10 <= n % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


# --------------------------------------------------------------------------
# State the report side reads
# --------------------------------------------------------------------------


def load_league_state():
    if not os.path.exists(LEAGUE_STATE_PATH):
        return {}
    try:
        with open(LEAGUE_STATE_PATH) as f:
            state = json.load(f)
        return state if isinstance(state, dict) else {}
    except (OSError, ValueError):
        return {}


def save_league_state(state):
    os.makedirs(os.path.dirname(LEAGUE_STATE_PATH), exist_ok=True)
    with open(LEAGUE_STATE_PATH, "w") as f:
        json.dump(state, f, indent=1, sort_keys=True)
        f.write("\n")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--league", default="csw", help="league slug (default: csw)")
    ap.add_argument("--season", type=int, default=None,
                    help="season number (default: the league's current season)")
    ap.add_argument("--all-leagues", action="store_true",
                    help="sync the current season of every league the player is in "
                         "(what the scheduled automation runs)")
    ap.add_argument("--username", default=DEFAULT_USERNAME)
    ap.add_argument("--user-id", default=DEFAULT_USER_UUID)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    load_dotenv()
    if not os.environ.get("WOOGLES_API_KEY"):
        raise SystemExit("WOOGLES_API_KEY is not set (expected in .env)")

    if args.all_leagues:
        rc = 0
        for lg in get_leagues():
            season = resolve_season(lg, None)
            if not season:
                continue
            division, _ = find_player_division(season["uuid"], args.username,
                                               required=False)
            if division is None:
                print(f"{lg['name']}: {args.username} not registered this season — "
                      "skipping", file=sys.stderr)
                continue
            rc |= sync_one(lg, season, args)
        return rc

    league = get_league(args.league)
    season = resolve_season(league, args.season)
    return sync_one(league, season, args)


def sync_one(league, season, args):
    division, standing = find_player_division(season["uuid"], args.username)
    key = rating_key(league)

    games = player_season_games(args.user_id, season["uuid"])
    finished = [g for g in games if g.get("result") in FINISHED_RESULTS]
    unfinished = len(games) - len(finished)

    title = collection_title(league, season)
    print(f"{title}: division {division.get('division_number')}, "
          f"rank {standing.get('rank')}, {len(finished)} finished game(s)"
          + (f", {unfinished} still in progress" if unfinished else ""),
          file=sys.stderr)

    if not finished:
        print("nothing to sync yet", file=sys.stderr)
        return 0

    seeded = seed_games(finished, key, division)
    for g in seeded:
        print(f"  seed {g['seed']:2d}  {g['opponent_username']:20s} "
              f"{_fmt(g['opp_rating'] and round(g['opp_rating'])):>6s}  "
              f"{g['result']:5s} {g['player_score']}-{g['opponent_score']}",
              file=sys.stderr)

    description = (
        f"{league['name']} League Season {season['season_number']}, "
        f"Division {division.get('division_number')}. Round robin, ordered by "
        f"opponent {key} rating (seed 1 = the division's strongest player)."
    )
    uuid, actions = sync_collection(title, description, seeded, dry_run=args.dry_run)
    for a in actions:
        print(("[dry-run] " if args.dry_run else "") + a, file=sys.stderr)
    if not actions:
        print("collection already up to date", file=sys.stderr)

    if uuid and not args.dry_run:
        state = load_league_state()
        state[uuid] = {
            "title": title,
            "league_slug": league["slug"],
            "league_name": league["name"],
            "season_id": season["uuid"],
            "season_number": season["season_number"],
            "division_id": division.get("uuid"),
            "division_number": division.get("division_number"),
            "username": args.username,
            "rating_key": key,
            "seeds": {g["game_id"]: {"seed": g["seed"],
                                     "opponent": g["opponent_username"],
                                     "rating": g["opp_rating"]} for g in seeded},
        }
        save_league_state(state)
        print(f"recorded league context in {LEAGUE_STATE_PATH}", file=sys.stderr)
        print(f"https://woogles.io/collections/{uuid}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
