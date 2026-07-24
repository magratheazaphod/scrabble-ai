#!/usr/bin/env python3
"""Turn a Woogles league season into a report-ready collection.

A league season is a round robin inside one division: every player meets every
other once, games are played asynchronously over two weeks, and each finished
game is auto-analyzed by BestBot. So unlike a tournament there is nothing to
upload and no analysis to request — the only missing piece is the *collection*
that the existing report pipeline consumes. This module builds it:

    python3 scripts/woogles_league.py                    # current CSW season
    python3 scripts/woogles_league.py --season 18        # a specific season
    python3 scripts/woogles_league.py --league nwl       # a different league
    python3 scripts/woogles_league.py --dry-run          # show the plan only

Re-running is safe and is in fact the intended mode: a season is synced while it
is still in progress, so each run adds whatever games have finished since and
re-seeds the ordering. Only genuinely-changed titles and orderings are written.

**Ordering.** A league round robin has no meaningful round order — the "round"
number Woogles reports is 0 for every game, and games finish in whatever order
the two players got to them. Ordering by date would therefore be arbitrary. So
games are instead sequenced by *opponent strength*, strongest first, and the
chapter title encodes that as "Seed <n>". The report's ordering column then reads
as "how did I do against the 1st seed, the 2nd seed, ..." — which is the question
worth asking of a round robin. tournament_report.compute_game parses "Seed <n>"
into its `round` field, so the rest of the pipeline needs no league awareness.

Strength is each opponent's Woogles rating *in the format the league is played
in* — CSW correspondence for the Collins league — since that is the rating these
very games move. Ratings are read at sync time, so a season synced mid-flight and
re-synced at the end can legitimately re-seed as ratings shift; the rating used
is recorded alongside the seed so any report says what it was seeded on.

**Cross-check.** Woogles publishes its own standings table per division
(woogles.io/leagues/csw). `cross_check_section` recomputes the same quantities
from the analyzed games and diffs them, so a report can state outright whether
its numbers agree with the platform's rather than asking the reader to trust
them. This catches both a bug here and a game the collection is missing.
"""
import argparse
import json
import os
import random
import re
import sys
import time

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


def find_player_division(season_id, username, required=True):
    """(division, standing) for `username` in this season, or (None, None).

    `required=False` is for sweeping every league on the platform, most of which
    the player has never entered — not finding them is the normal case there.
    """
    resp = post("league_service.LeagueService/GetAllDivisionStandings",
                {"season_id": season_id})
    for div in resp.get("divisions", []):
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


def seed_games(games, key):
    """Attach a rating and a 1-based seed to each game, strongest opponent first.

    Unrated opponents sort last (a missing rating is not a zero rating), with
    username as the final tie-break so the ordering is stable across runs.
    """
    seeded = []
    for g in games:
        ratings = player_ratings(g["opponent_username"])
        seeded.append({**g, "opp_rating": ratings.get(key)})
    seeded.sort(key=lambda g: (g["opp_rating"] is None,
                               -(g["opp_rating"] or 0),
                               g["opponent_username"].lower()))
    for i, g in enumerate(seeded, start=1):
        g["seed"] = i
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
    losses = n - wins
    spread = sum(g["jesse_score"] - g["opp_score"] for g in stats)

    plat_draws = standing.get("draws") or 0
    plat_record = f"{standing.get('wins', 0)}-{standing.get('losses', 0)}"
    mine_record = f"{wins}-{losses}"
    if plat_draws:
        plat_record += f"-{plat_draws}"
        mine_record += "-0"

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


def seeding_rows(stats):
    return [(g["round"], g.get("opponent"), g.get("opp_rating"), g["result"],
             g["jesse_score"] - g["opp_score"]) for g in stats]


def cross_check_section(stats, agg, standing, division, season, league):
    """Markdown for the league context: seeding basis, division table, cross-check."""
    key = rating_key(league)
    lines = ["## League Context", ""]
    lines.append(
        f"{league['name']} League, Season {season['season_number']}, "
        f"Division {division.get('division_number')} — "
        f"finished **{_ordinal(standing.get('rank'))} of "
        f"{len(division.get('standings') or [])}**."
    )
    lines.append("")
    lines.append(
        f"A league season is a round robin with no meaningful round order, so games "
        f"below are ordered by opponent strength (seed 1 = strongest), using each "
        f"opponent's `{key}` rating at the time this report was generated."
    )
    lines.append("")

    lines.append("### Results by Opponent Seed")
    lines.append("")
    lines.append("| Seed | Opponent | Rating | Result | Spread |")
    lines.append("|---|---|---|---|---|")
    for seed, opp, rating, result, spread in seeding_rows(stats):
        sp = f"+{spread}" if spread > 0 else ("−" + str(abs(spread)) if spread < 0 else "0")
        lines.append(f"| {seed} | {opp} | {_fmt(rating and round(rating))} | {result} | {sp} |")
    lines.append("")

    lines.append("### Division Standings (woogles.io)")
    lines.append("")
    lines.append("| # | Player | W-L-D | Spread | Avg Mistakes |")
    lines.append("|---|---|---|---|---|")
    for s in sorted(division.get("standings") or [], key=lambda s: s.get("rank", 99)):
        me = s.get("username", "").lower() == standing.get("username", "").lower()
        name = f"**{s.get('username')}**" if me else s.get("username")
        rec = f"{s.get('wins',0)}-{s.get('losses',0)}-{s.get('draws',0)}"
        ami = s.get("avg_mistake_index")
        lines.append(f"| {s.get('rank')} | {name} | {rec} | {s.get('spread')} | "
                     f"{_fmt(ami and round(ami, 2))} |")
    lines.append("")

    rows = cross_check_rows(stats, agg, standing)
    mismatches = [r for r in rows if not r[3]]
    lines.append("### Cross-Check vs the League Table")
    lines.append("")
    lines.append(
        "*Every figure this report computes from the analyzed games, next to the same "
        "figure as Woogles publishes it on the league standings page.*"
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
    lines.append("")
    if mismatches:
        lines.append(
            "**"
            + f"{len(mismatches)} figure(s) disagree with the league table: "
            + ", ".join(m[0] for m in mismatches)
            + ".** Investigate before trusting the numbers above — a disagreement "
            "usually means the collection is missing a game or holds one the "
            "division doesn't count."
        )
    else:
        lines.append(
            f"**All {len(rows)} cross-checked figures match the Woogles league table.**"
        )
    return "\n".join(lines)


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
    division, standing = find_player_division(season["uuid"], entry["username"])

    rows = cross_check_rows(stats, agg, standing)
    bad = [r[0] for r in rows if not r[3]]
    verdict = (
        f"all {len(rows)} cross-checked figures match the woogles.io league table"
        if not bad else
        f"{len(bad)} of {len(rows)} figures DISAGREE with the woogles.io league "
        f"table ({', '.join(bad)})"
    )
    digest_line = (
        f"League: {league['name']} Season {season['season_number']}, Division "
        f"{division.get('division_number')}, finished {_ordinal(standing.get('rank'))} "
        f"of {len(division.get('standings') or [])}. Games are ordered by opponent "
        f"rating (seed 1 = strongest), not by round. Cross-check: {verdict}."
    )
    return {
        "round_label": "Seed",
        "sections": [cross_check_section(stats, agg, standing, division, season, league)],
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

    seeded = seed_games(finished, key)
    for g in seeded:
        print(f"  seed {g['seed']:2d}  {g['opponent_username']:20s} "
              f"{_fmt(g['opp_rating'] and round(g['opp_rating'])):>6s}  "
              f"{g['result']:5s} {g['player_score']}-{g['opponent_score']}",
              file=sys.stderr)

    description = (
        f"{league['name']} League Season {season['season_number']}, "
        f"Division {division.get('division_number')}. Round robin, ordered by "
        f"opponent {key} rating (seed 1 = strongest)."
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
