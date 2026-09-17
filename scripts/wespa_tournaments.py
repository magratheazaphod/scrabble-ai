#!/usr/bin/env python3
"""WESPA tournament lookup: which real event is this collection, and when was it?

A Woogles collection title is whatever Jesse typed when he made it. WESPA's own
results database knows what the tournament was actually called and — the part
nothing in this repo otherwise has — **the date it was played**. A Woogles game
history carries no date at all, and `added_at` is when the GCG was uploaded,
often years later.

Matching by name does not work. Jesse's "King's Cup 2019" is WESPA's "34th
BRAND's Thailand International Crossword Game"; his "NSC 2019" is "North
American Scrabble Championship - Collins Division". So this matches on the one
thing both sides record exactly: **the round-by-round scores**. Every game in a
collection is a (my score, their score) pair, and so is every round of the WESPA
result. Matching those pairs against each of Jesse's own 40-odd rated events is
an identification, not a guess — and it reports how many games matched, so a
weak match is visibly weak rather than silently wrong.

    python3 scripts/wespa_tournaments.py --list
    python3 scripts/wespa_tournaments.py --identify "WESPAC 2019"
    python3 scripts/wespa_tournaments.py --identify-all --write-dates
    python3 scripts/wespa_tournaments.py --search "Causeway"
    python3 scripts/wespa_tournaments.py --rounds 758

Transport (headers, backoff, the 403-on-default-User-Agent quirk) is reused from
wespa_ratings rather than reimplemented: that module remains the one door onto
wespa.xerafin.net.
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import wespa_ratings as wr  # noqa: E402  — transport + the player roster

# Endpoints, all public and all behind the site's own pages:
#   /api/tournaments/search   what the homepage's Tournament Search box calls
#   /api/v2/player/<id>       the player page: profile, stats, and every rated
#                             event that player has played, with dates
#   /api/v2/player/<id>/tournaments/<tid>   that player's round-by-round result
#   /api/v2/tournament/<tid>  full standings, all divisions
SEARCH_URL = f"{wr.BASE}/api/tournaments/search"
PLAYER_URL = f"{wr.BASE}/api/v2/player/{{}}"
PLAYER_TOURNEY_URL = f"{wr.BASE}/api/v2/player/{{}}/tournaments/{{}}"
TOURNEY_URL = f"{wr.BASE}/api/v2/tournament/{{}}"
TOURNEY_PAGE = f"{wr.BASE}/tournament.html?id={{}}"

# Gitignored, like the ratings cache: this repo is public and the cache is a
# bulk copy of someone else's dataset. Cheap to rebuild — one request per event.
CACHE_PATH = "data/wespa-tournaments.json"
CACHE_TTL_DAYS = 30  # a finished result never changes; only new events appear

DATES_PATH = ".github/event-dates.json"


def _json(url, retries=4):
    text = wr._get(url, retries=retries)
    if text is None:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


# --------------------------------------------------------------------------
# Cache
# --------------------------------------------------------------------------

def _load_cache():
    if not os.path.exists(CACHE_PATH):
        return {"player": {}, "rounds": {}}
    try:
        with open(CACHE_PATH) as f:
            c = json.load(f)
    except (OSError, ValueError):
        return {"player": {}, "rounds": {}}
    c.setdefault("player", {})
    c.setdefault("rounds", {})
    return c


def _save_cache(cache):
    os.makedirs(os.path.dirname(CACHE_PATH) or ".", exist_ok=True)
    tmp = CACHE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cache, f)
    os.replace(tmp, CACHE_PATH)  # never leave a half-written cache behind


def _fresh(entry):
    return entry and (time.time() - entry.get("fetched_at", 0)) < CACHE_TTL_DAYS * 86400


# --------------------------------------------------------------------------
# The player's own event history
# --------------------------------------------------------------------------

def resolve_player(name="Jesse Day"):
    """(playerid, display name) from the ratings roster — never a hardcoded id.

    Goes through wespa_ratings so the name matching, aliases and fuzzy
    suggestions are the ones already used everywhere else in the repo.
    """
    p = wr.lookup(name)
    if not p and not wr.load():
        # No ratings cache at all — a fresh clone. Unlike the report render path,
        # which must never depend on WESPA being reachable, this tool is an
        # explicit online lookup, so fetching the roster here is the expected
        # behaviour rather than a surprise.
        print("No WESPA roster cached — fetching it once...", file=sys.stderr)
        wr.refresh()
        p = wr.lookup(name)
    if not p:
        return None, None
    return p["playerid"], p["name"]


def player_tournaments(playerid, force=False):
    """Every rated event this player has played: name, date, W-L, spread, id.

    Sorted oldest first. This is the candidate set for identification — bounded
    at a few dozen entries, so matching against all of them is cheap and needs
    no name guessing at all.
    """
    cache = _load_cache()
    key = str(playerid)
    if not force and _fresh(cache["player"].get(key)):
        return cache["player"][key]["tournaments"]
    data = _json(PLAYER_URL.format(playerid))
    if not data:
        # Degrade to whatever is cached, however old: a stale event list is far
        # better than none, and every caller here is advisory.
        stale = cache["player"].get(key)
        return stale["tournaments"] if stale else []
    tournaments = sorted(data.get("tournaments") or [], key=lambda t: t.get("date") or "")
    cache["player"][key] = {"fetched_at": time.time(), "tournaments": tournaments}
    _save_cache(cache)
    return tournaments


def player_rounds(playerid, tourneyid, force=False):
    """That player's round-by-round result: opponent, score for, score against."""
    cache = _load_cache()
    key = f"{playerid}:{tourneyid}"
    if not force and _fresh(cache["rounds"].get(key)):
        return cache["rounds"][key]["rounds"]
    data = _json(PLAYER_TOURNEY_URL.format(playerid, tourneyid))
    if not data:
        stale = cache["rounds"].get(key)
        return stale["rounds"] if stale else []
    rounds = data.get("rounds") or []
    cache["rounds"][key] = {"fetched_at": time.time(), "rounds": rounds}
    _save_cache(cache)
    return rounds


def search(q=None, country=None, from_date=None, to_date=None):
    """The site's own tournament search — for events the player didn't play in,
    or when an identification comes back empty and you need to look by hand."""
    params = []
    for k, v in (("q", q), ("country", country), ("from", from_date), ("to", to_date)):
        if v:
            params.append(f"{k}={requests_quote(v)}")
    if not params:
        return []
    data = _json(f"{SEARCH_URL}?{'&'.join(params)}")
    return (data or {}).get("tournaments") or []


def requests_quote(s):
    from urllib.parse import quote
    return quote(str(s))


def standings(tourneyid):
    """Full standings for an event, all divisions — the cross-check for a
    finish position, and the place to read the field's strength."""
    return _json(TOURNEY_URL.format(tourneyid)) or {}


# --------------------------------------------------------------------------
# Identification
# --------------------------------------------------------------------------

def _pairs(rounds):
    """WESPA rounds as a multiset-ish list of (score_for, score_against)."""
    out = []
    for r in rounds:
        sf, sa = r.get("score_for"), r.get("score_against")
        if sf is not None and sa is not None:
            out.append((sf, sa))
    return out


# How far two records of the same game may disagree and still be that game.
# They routinely do: the official score is what the paper scoresheet said, the
# collection's score is what replaying the reconstructed GCG produces, and the
# two part company over an unplayed-tile adjustment or a single mis-transcribed
# turn. Measured across this archive the gap is almost always 1-5 points and
# never approached 15 — well inside the margin that would let a *different*
# game match by accident, since the pairing also has to beat every other round.
SCORE_SLOP = 12


def _name_match(a, b):
    """Do these two spellings denote the same opponent?

    WESPA and a Woogles annotation disagree constantly — "Mcdonald"/"McDonald",
    "Deen-Swaray"/"Deen Swaray", "Toh Weibin"/"Weibin". Punctuation and case are
    folded by the shared normalizer; the rest is token containment, which covers
    a dropped first name without matching two unrelated people.
    """
    na, nb = wr._norm(a), wr._norm(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    ta = {wr._norm(t) for t in (a or "").replace("-", " ").split() if len(t) > 2}
    tb = {wr._norm(t) for t in (b or "").replace("-", " ").split() if len(t) > 2}
    return bool(ta) and bool(tb) and (ta <= tb or tb <= ta)


def _pair_score(game, rnd):
    """How strongly one collection game and one WESPA round look like the same
    game: 0 = not a candidate. Name and scores each carry evidence, and a pair
    needs enough of both — a name alone would match a repeat opponent in the
    wrong round, and slack scores alone would match a coincidence.
    """
    sf, sa = rnd.get("score_for"), rnd.get("score_against")
    if sf is None or sa is None:
        return 0
    dmine = abs((game.get("jesse_score") or 0) - sf)
    dtheirs = abs((game.get("opp_score") or 0) - sa)
    name = _name_match(game.get("opponent"), rnd.get("opponent_name"))
    if dmine == 0 and dtheirs == 0:
        return 4 if name else 3          # exact scores: identification on its own
    if dmine <= SCORE_SLOP and dtheirs <= SCORE_SLOP:
        return 2 if name else 0          # near miss counts only with the name
    return 1 if name and dmine + dtheirs <= 60 else 0  # name + same ballpark


def identify(games, playerid, candidates=None, force=False):
    """Rank a player's events by how well they explain this collection's games.

    `games` is [{"jesse_score", "opp_score", "opponent"}] — exactly the shape
    `tournament_report.compute_game` already returns, so a caller passes its
    stats straight in.

    The signal is the **score pair**, matched without replacement: a collection
    game counts as explained when the event has an unused round with the same
    two scores. Names are only ever a tiebreak, because the two databases spell
    opponents differently and a Woogles annotation may carry a nickname.

    Partial uploads are the normal case (a 32-round event with 30 games on
    Woogles, a deliberately abbreviated collection), so the score is the share
    of the *collection's* games explained, not of the event's rounds. An
    unmatched round is expected; an unmatched game is the thing that should
    worry you.
    """
    results = []
    for t in (candidates if candidates is not None else player_tournaments(playerid, force=force)):
        rounds = player_rounds(playerid, t["tourneyid"], force=force)
        pool = _pairs(rounds)
        # Best-first greedy pairing rather than first-come: a repeated opponent
        # (a final is three games against one player) would otherwise let a weak
        # near-miss consume the round that the exact match needed.
        cand = sorted(
            ((_pair_score(g, r), gi, ri)
             for gi, g in enumerate(games) for ri, r in enumerate(rounds)),
            reverse=True,
        )
        used_g, used_r = set(), set()
        matched = exact = name_hits = 0
        for sc, gi, ri in cand:
            if sc <= 0 or gi in used_g or ri in used_r:
                continue
            used_g.add(gi)
            used_r.add(ri)
            matched += 1
            if sc >= 3:
                exact += 1
            if _name_match(games[gi].get("opponent"), rounds[ri].get("opponent_name")):
                name_hits += 1
        # A single matching score line is a coincidence, not evidence — across
        # this archive every true identification matched most of the collection,
        # while the one-game hits were all unrelated events years apart. Below
        # two, report nothing rather than a plausible-looking wrong answer.
        if matched < 2:
            continue
        results.append({
            "tourneyid": t["tourneyid"],
            "name": t["name"],
            "date": t.get("date"),
            "division": t.get("division"),
            "record": f"{int(t.get('wins') or 0)}-{int(t.get('losses') or 0)}",
            "spread": t.get("spread"),
            "place": t.get("place"),
            "rounds": len(pool),
            "matched": matched,
            "exact": exact,
            "name_hits": name_hits,
            "share": matched / len(games) if games else 0,
            "url": TOURNEY_PAGE.format(t["tourneyid"]),
        })
    results.sort(key=lambda r: (r["matched"], r["exact"], r["name_hits"]), reverse=True)
    return results


def confidence(top, games):
    """A word for how much to trust the top hit, and why.

    Deliberately coarse. Two score pairs colliding across two events is already
    unlikely; a dozen is not something to hedge about. What actually needs
    flagging is the *thin* match — a two-game final, where one coincidence is a
    third of the evidence.
    """
    if not top:
        return "none", "no event shares a single score line with this collection"
    m, n = top["matched"], len(games)
    if m == n and (m >= 5 or top["name_hits"] == m):
        # A short match earns "certain" only when the opponents agree too: a
        # two-game final is three coincidences away from meaning nothing, and
        # the names are the independent evidence that closes it.
        return "certain", f"all {n} games matched, score for score"
    if top["share"] >= 0.85 and m >= 5:
        return "certain", f"{m} of {n} games matched"
    if top["share"] >= 0.6 or m >= 4:
        return "likely", f"{m} of {n} games matched; check the unmatched ones"
    if top["share"] < 0.2 and m < 4:
        # A handful of hits spread over a big collection is background noise —
        # any two events will share a score line eventually. Say "nothing found"
        # rather than dressing coincidence up as a lead.
        return "none", (f"only {m} of {n} games matched anything — background "
                        "coincidence, not an identification")
    return "weak", (f"only {m} of {n} games matched — too thin to trust on its own, "
                    "confirm against the standings before writing a date")


# --------------------------------------------------------------------------
# Closing the loop: the skill graph's date file
# --------------------------------------------------------------------------

def write_dates(entries, path=DATES_PATH):
    """Merge {collection title/uuid: {date, label}} into the overrides file that
    scripts/skill_graph.py reads, preserving every key already there.

    Only ever *adds* a date to a collection that has none, or corrects one this
    run identified — a hand-written label is never clobbered.
    """
    existing = {}
    if os.path.exists(path):
        with open(path) as f:
            existing = json.load(f)
    changed = []
    for key, new in entries.items():
        cur = dict(existing.get(key) or {})
        if cur.get("date") == new.get("date"):
            continue
        cur["date"] = new["date"]
        cur.setdefault("wespa_tourneyid", new.get("tourneyid"))
        cur["wespa_name"] = (new.get("wespa_name") or "").strip()  # upstream pads some names
        existing[key] = cur
        changed.append(key)
    if changed:
        with open(path, "w") as f:
            json.dump(existing, f, indent=2, ensure_ascii=False)
            f.write("\n")
    return changed


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _load_collections(snapshot):
    with open(snapshot) as f:
        return (json.load(f).get("collections") or [])


def _collection_games(col):
    import tournament_report as tr
    games = []
    for r in col.get("games") or []:
        try:
            g = tr.compute_game(r)
        except (StopIteration, KeyError, TypeError):
            continue
        games.append(g)
    return games


def _print_match(title, games, res, verbose=False):
    conf, why = confidence(res[0] if res else None, games)
    print(f"\n{title}  ({len(games)} games)")
    if not res:
        print(f"  [{conf}] {why}")
        return None
    if conf == "none":
        print(f"  [{conf}] {why}")
        return None
    top = res[0]
    print(f"  [{conf}] {why}")
    print(f"  -> {top['name']}  {top['date']}  (div {top['division']}, "
          f"{top['record']} {top['spread']:+}, place {top['place']})")
    print(f"     {top['url']}   matched {top['matched']}/{len(games)} "
          f"of the event's {top['rounds']} rounds")
    for other in res[1:3] if verbose else []:
        print(f"     also: {other['name']} {other['date']} "
              f"({other['matched']} matched)")
    return top


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--player", default="Jesse Day")
    ap.add_argument("--list", action="store_true", help="every rated event this player has played")
    ap.add_argument("--search", metavar="NAME", help="search WESPA's tournament database by name")
    ap.add_argument("--country")
    ap.add_argument("--from-date")
    ap.add_argument("--to-date")
    ap.add_argument("--rounds", type=int, metavar="TOURNEYID", help="round-by-round result")
    ap.add_argument("--standings", type=int, metavar="TOURNEYID")
    ap.add_argument("--identify", metavar="TITLE", help="identify one collection (title substring)")
    ap.add_argument("--identify-all", action="store_true")
    ap.add_argument("--write-dates", action="store_true",
                    help=f"merge confident identifications into {DATES_PATH}")
    ap.add_argument("--snapshot", default="data/woogles-snapshot.json")
    ap.add_argument("--force", action="store_true", help="ignore the local cache")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    if args.search or args.country or args.from_date or args.to_date:
        hits = search(args.search, args.country, args.from_date, args.to_date)
        print(f"{len(hits)} tournaments")
        for t in hits:
            print(f"  {t['start_date']}  {t['tourneyid']:5d}  {t['country'] or '---'}  {t['name']}")
        return 0

    playerid, display = resolve_player(args.player)
    if not playerid:
        print(f"No WESPA player matching '{args.player}'. "
              f"Try: python3 scripts/wespa_ratings.py --suggest '{args.player}'", file=sys.stderr)
        return 1

    if args.rounds:
        for r in player_rounds(playerid, args.rounds, force=args.force):
            print(f"  Rd {r['round']:>2}  {r['result']}  {r['score_for']:>3}-{r['score_against']:<3}  "
                  f"vs {r['opponent_name']}")
        return 0

    if args.standings:
        d = standings(args.standings)
        print(f"{d.get('name')}  {d.get('date')}  ({d.get('total_players')} players)")
        for div in d.get("divisions") or []:
            print(f"  Division {div['name']}")
            for s in div["standings"]:
                print(f"    {s['place']:>3}. {s['name']:<28} {s['wins']:g}-{s['losses']:g} "
                      f"{s['spread']:+6d}  {s['endRating']}")
        return 0

    if args.list:
        ts = player_tournaments(playerid, force=args.force)
        print(f"{display} (WESPA id {playerid}) — {len(ts)} rated events")
        for t in ts:
            print(f"  {t['date']}  {t['tourneyid']:5d}  "
                  f"{int(t['wins'])}-{int(t['losses'])} {t['spread']:+6d}  {t['name']}")
        return 0

    if args.identify or args.identify_all:
        cols = _load_collections(args.snapshot)
        if args.identify:
            needle = args.identify.lower()
            cols = [c for c in cols if needle in c["title"].lower()]
            if not cols:
                print(f"No collection matching '{args.identify}' in {args.snapshot}", file=sys.stderr)
                return 1
        candidates = player_tournaments(playerid, force=args.force)
        to_write = {}
        for col in cols:
            games = _collection_games(col)
            if not games:
                continue
            res = identify(games, playerid, candidates=candidates, force=args.force)
            top = _print_match(col["title"], games, res, verbose=args.verbose)
            conf, _ = confidence(res[0] if res else None, games)
            if top and conf in ("certain", "likely"):
                to_write[col["title"]] = {
                    "date": top["date"], "tourneyid": top["tourneyid"],
                    "wespa_name": top["name"],
                }
        if args.write_dates:
            changed = write_dates(to_write)
            print(f"\n{len(changed)} date(s) written to {DATES_PATH}"
                  + (": " + ", ".join(changed) if changed else ""))
        elif to_write:
            print(f"\n{len(to_write)} collection(s) datable — re-run with --write-dates "
                  f"to merge into {DATES_PATH}")
        return 0

    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
