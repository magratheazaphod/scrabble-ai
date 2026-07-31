"""Deterministic stats/report logic for Woogles tournament collections.

Single source of truth for SKILL.md's Steps 5-8 (per-game stats, aggregation,
notes, report template). Both the automation (generate_report_email.py) and
the interactive tournament-analysis skill call this module — see
.claude/skills/tournament-analysis/SKILL.md for the semantic contracts these
functions implement and for the upstream data-fetch steps (1-4) that produce
the `{"meta", "analysis", "history"}` game dicts these functions consume.
"""
import hashlib
import json
import os
import re
import time
import random

import requests

BASE = "https://woogles.io/api"
PROFILE_URL = "https://woogles.io/profile/{}"


def woogles_username(player):
    """The player's real Woogles account name, or None if they have no account.

    Only games actually played on Woogles identify their players by account. In a
    game created through the annotator the "player" is just a free-text label the
    uploader typed, and turning that into a profile link is wrong — usually the
    link is dead, and worse, a label that happens to collide with some stranger's
    username would point at that stranger.

    Annotator uploads have been observed to synthesise `user_id` two different
    ways: `internal-<nickname>` (e.g. `internal-Michael_Donegan`) and the bare
    nickname itself (`user_id: "JamesCurley"`). Both are caught by the same rule:
    a real Woogles account's id is an opaque 22-character key
    (`ZyTogV4LzXY2AFsT7wCW8T`) that is never derived from the chosen nickname.
    Checking the id rather than the collection's `is_annotated` flag keeps this
    correct per-player, so a collection mixing uploads with played games still
    links only the real accounts.
    """
    uid = (player.get("user_id") or "").strip()
    nick = (player.get("nickname") or "").strip()
    if not uid or not nick or uid.startswith("internal-") or uid == nick:
        return None
    return nick


# A person's real name is public in tournament results; their Woogles handle is
# public on their profile. The *link between the two* is neither, and is not ours
# to publish — someone may deliberately keep their online identity separate from
# their real-world one. So this mapping is private-by-construction and opt-in:
#
#   * it is asserted by hand, never inferred from a name that merely resembles a
#     username (that guess would eventually point at an innocent stranger);
#   * it lives in data/, which this public repo gitignores in full, so it cannot
#     be committed by accident;
#   * automation reads it from WOOGLES_NAME_REGISTRY (a GitHub Actions secret)
#     rather than from the repo;
#   * when neither source is present the feature is simply off, which is the
#     correct default for any report rendered for anyone but its owner.
#
# Format is {username: [aliases as they appear in game data]}, e.g.
#   {"james": ["James Curley", "JamesCurley", "JC"]}
NAME_REGISTRY_PATH = "data/woogles-usernames.json"
_NAME_REGISTRY = None


def _norm_label(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def load_name_registry(force=False):
    """{normalized alias: username}. Empty when no private registry is configured."""
    global _NAME_REGISTRY
    if _NAME_REGISTRY is not None and not force:
        return _NAME_REGISTRY
    raw = None
    inline = os.environ.get("WOOGLES_NAME_REGISTRY", "").strip()
    if inline:
        try:
            raw = json.loads(inline)
        except ValueError:
            raw = None
    if raw is None and os.path.exists(NAME_REGISTRY_PATH):
        try:
            with open(NAME_REGISTRY_PATH) as f:
                raw = json.load(f)
        except (OSError, ValueError):
            raw = None
    registry = {}
    for username, aliases in (raw or {}).items():
        if isinstance(aliases, str):
            aliases = [aliases]
        for alias in list(aliases or []) + [username]:
            registry[_norm_label(alias)] = username
    _NAME_REGISTRY = registry
    return registry


def _label_matches_username(name, username):
    return _norm_label(name) == _norm_label(username)


def opponent_cell(name, username, seed=None):
    """Opponent as displayed in the report: "Real Name - username (#seed)", where
    the username links to their Woogles profile. Drops the leading name when it
    adds nothing (no real name on file, so `name` is already the username), and
    drops the "(#n)" when the collection has no seeding (a normal tournament,
    where the ordering column is the round).

    `username` comes from the game's own account data when the game was played on
    Woogles; for an annotator upload there is no account, so fall back to the
    private name registry above (off unless configured).
    """
    username = username or load_name_registry().get(_norm_label(name))
    suffix = f" (#{seed})" if seed is not None else ""
    if not username:
        return f"{name}{suffix}"
    link = f"[{username}]({PROFILE_URL.format(username)})"
    if _label_matches_username(name, username):
        return f"{link}{suffix}"
    return f"{name} - {link}{suffix}"


def opponent_handle(g):
    """Bare Woogles username for the compact missed-bingo tables.

    Those tables are scanned, not read: a short handle is easier to place at a
    glance than a display name, and the full "Real Name - username" cell already
    appears once per game in the per-game table above. Falls back to the display
    name when there is no account behind the player (an annotator upload with no
    registry entry).
    """
    name = g["opponent"]
    username = g.get("opp_username") or load_name_registry().get(_norm_label(name))
    return username or name


def is_jesse(p):
    nick = (p.get("nickname") or "").lower().replace("_", "")
    real = (p.get("real_name") or "").lower()
    uid = (p.get("user_id") or "").lower()
    # "magrathean" is the nickname on games actually played on Woogles (league,
    # casual); the rest are labels used on annotator uploads. Matching it by
    # nickname as well as user_id means identification no longer depends on the
    # profile happening to carry a real name.
    return (nick in ("jd", "jessed", "jesseday", "magrathean")
            or "jesse" in real or uid == "magrathean")


def _normalize_nick(nick):
    return re.sub(r"[^a-z]", "", (nick or "").lower())


def make_is_subject(subject):
    """Return a predicate matching `is_jesse`'s signature, for the `subject` override.

    subject is None -> the default is_jesse matcher (byte-identical behavior).
    subject is {"nickname", "real_name"} -> match players by normalizing
    GameHistory players[].nickname against subject["nickname"].
    """
    if subject is None:
        return is_jesse
    target = _normalize_nick(subject["nickname"])
    return lambda p: _normalize_nick(p.get("nickname")) == target


def summary_for_index(analysis, player_index):
    """Select a `player_summaries` entry by turn `player_index`, not by name.
    `player_summaries` only carries `player_name` (a nickname that varies —
    "JD", "JesseDay", bare "Jesse"), so an allowlist silently drops variants it
    omits (this dropped Jesse's mistake score in 18 King's Cup 2019 games). Turns
    carry both `player_name` and `player_index` and match summaries exactly, so
    pair them to tie the summary to the already-robust `jesse_idx`/`opp_idx`."""
    names_at_index = {
        t["player_name"] for t in analysis["turns"] if t.get("player_index") == player_index
    }
    return next(
        (s for s in analysis["player_summaries"] if s.get("player_name") in names_at_index),
        None,
    )


def format_real_name(r):
    if not r:
        return ""
    m = re.match(r"^(.+),\s*(.+)$", r)
    if m:
        first = re.sub(r"[A-Z]$", "", m.group(2)).strip()  # strip trailing initial
        return f"{first} {m.group(1)}"
    return r


def get_opp_name(meta, players, jesse_idx):
    opp = players[1 - jesse_idx]
    real = format_real_name(opp.get("real_name", ""))
    title = re.sub(r"^\([^)]+\)\s*", "", meta["chapter_title"])
    title = re.sub(
        r"^(Round\s+\d+|Rd\s*\d+|Seed\s*\d+|Game\s*#?\d+(?:\s*[-–]\s*\d{4}-\d{2}-\d{2})?)\s*[-–]?\s*",
        "",
        title,
        flags=re.I,
    )
    m = re.match(r"^(.+?)\s+vs\.?\s+(.+)$", title, re.I)
    title_name = None
    if m:
        p1, p2 = m.group(1).strip(), m.group(2).strip()
        jesse_names = {"jd", "jesse", "jessed"}
        is_j1 = p1.lower().replace(" ", "").replace("_", "") in jesse_names
        is_j2 = p2.lower().replace(" ", "").replace("_", "") in jesse_names
        if is_j2 and not is_j1:
            title_name = p1
        elif is_j1 and not is_j2:
            title_name = p2
    # Prefer real_name when more complete (has a space but title_name doesn't)
    if title_name:
        if real and " " in real and " " not in title_name:
            return real
        return title_name
    if real and " " in real:
        return real
    return real or (opp.get("nickname") or "").replace("_", " ")


def iter_move_events(events):
    """Yield (event_idx, event, challenged_off) once per analysis turn, in order.

    The single walk shared by every events↔analysis alignment in this module:
    only TILE_PLACEMENT_MOVE / EXCHANGE / PASS have a matching `analysis["turns"]`
    entry, so the n-th yield lines up with `analysis["turns"][n]`. History always
    carries one extra move (the final PASS) with no analysis turn, which is
    harmless — the extra yield is simply never indexed.

    PHONY_TILES_RETURNED (the play was challenged off) and CHALLENGE_BONUS are not
    moves and consume no slot; a challenged placement yields once, with
    `challenged_off` True, and the two events are stepped over together.
    """
    i = 0
    while i < len(events):
        ev = events[i]
        et = ev.get("type", "")
        if et == "TILE_PLACEMENT_MOVE":
            challenged = (
                i + 1 < len(events) and events[i + 1].get("type") == "PHONY_TILES_RETURNED"
            )
            yield i, ev, challenged
            i += 2 if challenged else 1
            continue
        if et in ("EXCHANGE", "PASS"):
            yield i, ev, False
        i += 1


def build_turn_event_indices(events):
    """Raw `events` index of each analysis turn: index i -> events index of turns[i].

    Used to deep-link a turn on woogles.io — the client's `?turn=` parameter counts
    events, not analysis turns, so the two diverge as soon as a play is challenged
    off (see turn_url).
    """
    return [i for i, _, _ in iter_move_events(events)]


def turn_url(game_url, event_idx):
    """Link to the board position BEFORE the move at `event_idx` (0-based, raw events).

    woogles.io examines with `?turn=N`, which replays N-1 *events* and stops — so
    N = event_idx + 1 lands on the position with the mover still to play, which is
    what a missed bingo wants to show (board plus the rack the bingo was in).
    Verified against liwords-ui `table.tsx` (`handleExamineGoTo(turn - 1)`, and
    examinedTurn slices the flat event list) and `CommentsDrawer.tsx`, which links
    to the position AFTER a move as event_idx + 2.
    """
    return f"{game_url}?turn={event_idx + 1}"


def build_snapshots_and_racks(events):
    """Board state (15x15) and rack BEFORE each analysis turn.

    Returns (snapshots, racks). Index i corresponds to analysis turns[i].
    A play challenged off leaves the board unchanged (its tiles never stuck).
    """
    board = [["" for _ in range(15)] for _ in range(15)]
    snapshots, racks = [], []
    for _, ev, challenged in iter_move_events(events):
        snapshots.append([row[:] for row in board])
        racks.append(ev.get("rack") or "")
        if ev.get("type") != "TILE_PLACEMENT_MOVE" or challenged:
            continue
        dr = 1 if ev["direction"] == "VERTICAL" else 0
        dc = 0 if ev["direction"] == "VERTICAL" else 1
        r2, c2 = ev["row"], ev["column"]
        for ch in (ev.get("played_tiles") or ""):
            if ch != ".":
                board[r2][c2] = ch.upper()
            r2 += dr
            c2 += dc
    return snapshots, racks


def validate_bingo(optimal_move, history_rack):
    """Return False if history rack cannot supply the non-board tiles in optimal_move.

    Catches rare analysis bugs where the analysis rack differs from the actual game rack,
    causing false-positive missed_bingo flags.
    """
    parts = optimal_move.strip().split()
    if len(parts) < 2:
        return True
    word = parts[1]
    needed = [ch.upper() for ch in word if ch != "."]
    rack = list(history_rack.upper())
    for ch in needed:
        if ch in rack:
            rack.remove(ch)
        elif "?" in rack:
            rack.remove("?")
        else:
            return False
    return True


def resolve_bingo_word(optimal_move, board):
    """Replace '.' in optimal_move word with '(X)' where X is the board tile at that position.

    Position format:
      '8G WORD'  -> horizontal, row 8 (0-indexed: 7), col G (0-indexed: 6)
      'G8 WORD'  -> vertical,   col G (0-indexed: 6), row 8 (0-indexed: 7)
    Lowercase letters = blank played as that letter (preserved).
    (X) = board tile X was already there.
    """
    parts = optimal_move.strip().split()
    if len(parts) < 2:
        return optimal_move
    position, word = parts[0], parts[1]
    mh = re.match(r"^(\d+)([A-Oa-o])$", position)
    mv = re.match(r"^([A-Oa-o])(\d+)$", position)
    if mh:
        row = int(mh.group(1)) - 1
        col = ord(mh.group(2).upper()) - ord("A")
        dr, dc = 0, 1
    elif mv:
        col = ord(mv.group(1).upper()) - ord("A")
        row = int(mv.group(2)) - 1
        dr, dc = 1, 0
    else:
        return word
    result = ""
    r, c = row, col
    for ch in word:
        if ch == ".":
            tile = board[r][c] if 0 <= r < 15 and 0 <= c < 15 else ""
            result += f"({tile})" if tile else "(?)"
        else:
            result += ch
        r += dr
        c += dc
    return result


def build_played_words(events):
    """Resolve the actual word played on each turn (same index-skip trick as
    build_snapshots_and_racks, so index i lines up with analysis['turns'][i]).

    `challenged` is True only when the event log shows the play was challenged off
    (PHONY_TILES_RETURNED). It is NOT the same as "is this a phony" — an unchallenged
    phony stands on the board and is_phony must come from analysis['turns'][i]['is_phony'].

    `words_formed` is the event's own list of every word the play created (main word +
    any cross words). IMPORTANT: when a phony play forms more than one word, the analysis
    is_phony flag only tells you the PLAY was illegal, not WHICH of the formed words was
    the actual violation — don't assume it's the primary/longest one. Show all of them
    (see game_note's `*`-marking below) rather than guessing.

    Returns a list of {'word': str|None, 'words_formed': list[str], 'challenged': bool,
    'player_index': int}. word/words_formed are None/[] for EXCHANGE/PASS turns.
    """
    board = [["" for _ in range(15)] for _ in range(15)]
    moves = []
    for _, ev, challenged in iter_move_events(events):
        if ev.get("type") != "TILE_PLACEMENT_MOVE":
            moves.append(
                {"word": None, "words_formed": [], "challenged": False, "player_index": ev.get("player_index")}
            )
            continue
        word = resolve_bingo_word(f"{ev['position']} {ev.get('played_tiles') or ''}", board)
        moves.append(
            {
                "word": word,
                "words_formed": ev.get("words_formed") or [word],
                "challenged": challenged,
                "player_index": ev["player_index"],
            }
        )
        if challenged:
            continue
        dr = 1 if ev["direction"] == "VERTICAL" else 0
        dc = 0 if ev["direction"] == "VERTICAL" else 1
        r2, c2 = ev["row"], ev["column"]
        for ch in (ev.get("played_tiles") or ""):
            if ch != ".":
                board[r2][c2] = ch.upper()
            r2 += dr
            c2 += dc
    return moves


def readable_move(move, board):
    """A turn's move string with board tiles spelled out: "L1 K(I)NDA".

    `played_move`/`optimal_move` come back as "<position> <tiles>", where "." means
    "a tile already on the board" — unreadable on its own in a report row, so resolve
    the dots against the position the move was made from. Non-placement moves
    ("(Pass)", "(exch ABC)") have no position and are returned unchanged.
    """
    parts = (move or "").strip().split()
    if len(parts) < 2:
        return move or ""
    position, word = parts[0], parts[1]
    if not re.match(r"^(\d+[A-Oa-o]|[A-Oa-o]\d+)$", position):
        return move
    return f"{position} {resolve_bingo_word(move, board)}"


STAGES = ("early", "mid", "pre-endgame", "endgame")


def game_stage(turn):
    """Which stage of the game a turn belongs to: early / mid / pre-endgame / endgame.

    BestBot's own `phase` field is too coarse at the top — it lumps the whole game
    before the pre-endgame into one `PHASE_EARLY_MID` bucket (4538 of 5630 turns in
    the archive), which is useless for asking "where do my errors happen". So the
    unseen-tile count does the splitting instead: the bag holds 86 tiles once both
    racks are drawn, and 50 is roughly the point where the board is committed and
    the opening is over. Below that, bag empty = endgame and 1-14 in the bag =
    pre-endgame — Jesse's own boundary, and the right one: the pre-endgame is the
    stage defined by tracking exactly what is left in the bag, and that starts two
    turns' worth of tiles earlier than the analysis' `PHASE_*PREENDGAME` (bag ≤ 7)
    admits. Widening it here is deliberate; don't "fix" it back to the enum.
    """
    bag = turn.get("tiles_in_bag")
    if bag is None:
        return None
    if bag == 0:
        return "endgame"
    if bag <= 14:
        return "pre-endgame"
    return "early" if bag > 50 else "mid"


# A turn is worth logging as an error when it cost win probability at all, OR
# when win% barely moved but real spread did. The second case is the endgame and
# pre-endgame pattern Jesse cares about: with the game already won or already
# lost, every candidate wins with the same probability and the simulation is
# ranking on spread alone — those turns are invisible to a win%-only filter but
# are exactly where the point margin is thrown away.
FLAT_WIN_PROB = 0.005  # 0.5% — "win% didn't move"
SPREAD_ONLY_EQUITY = 5  # points of equity lost that make a flat-win% turn an error


def _sim_split(cand):
    """(my scoring, opponent's scoring) over a simulated candidate's plies.

    A sim ply alternates sides starting with the opponent's reply, so odd plies
    are theirs and even plies are mine; the candidate's own score is the first
    thing on my side of the ledger. `score_mean` is the mean over the same
    iteration count for every candidate in the turn (verified across the whole
    archive), so two candidates' splits are directly comparable.
    """
    plies = cand.get("ply_stats") or []
    mine = (cand.get("score") or 0) + sum(p["score_mean"] for p in plies if p["ply"] % 2 == 0)
    opp = sum(p["score_mean"] for p in plies if p["ply"] % 2 == 1)
    return mine, opp


def _variation_split(var):
    """(my scoring, opponent's scoring) over an endgame variation's move sequence.

    `move_number` starts at 1 with the play under consideration, so odd moves are
    mine and even ones the opponent's. Unlike the simulated split these are exact
    — the endgame is solved, not sampled."""
    moves = var.get("moves") or []
    mine = sum(m["score"] for m in moves if m["move_number"] % 2 == 1)
    opp = sum(m["score"] for m in moves if m["move_number"] % 2 == 0)
    return mine, opp


def equity_breakdown(turn):
    """How much spread a turn cost, split into its offensive and defensive halves.

    Returns {"equity_lost", "offense_delta", "defense_delta"}, or None when the
    analysis carries no comparable line for the played move (a challenge or a
    pass, or a turn the simulator never sat on).

    * `equity_lost` — spread given up against the play BestBot ranked first, from
      the simulation itself (`equity`), or the exact solved margin in an endgame.
      Negative means the played move was *better* on spread than the top move,
      which is normal when the top move buys win probability by protecting a lead.
    * `offense_delta` / `defense_delta` — the same comparison decomposed into
      scoring: how many more points the played move scores for me across the
      simulated line, and how many fewer it concedes to the opponent. Both are
      signed so that positive is good for Jesse, and they sum to roughly
      -equity_lost. The gap between that sum and the equity figure is everything
      the spread depends on besides the plays' own scores — the value of the
      leave at the end of a simulated line, the going-out bonus and unplayed
      tiles at the end of a solved endgame — so never "reconcile" the two by
      deriving one from the other.
    """
    sims = turn.get("top_sim_plays") or []
    if sims:
        # sims[0] is the analysis' own `optimal_move` in every turn of the archive
        # — i.e. the play the report's "Best" column names — so the comparison the
        # columns make is always against the move shown beside them.
        best = sims[0]
        played = next((p for p in sims if p.get("is_played_move")), None)
        if played is None:
            return None
        equity_lost = best["equity"] - played["equity"]
        split = _sim_split
    else:
        best = turn.get("principal_variation")
        variations = turn.get("other_variations") or []
        played = next(
            (v for v in variations
             if (v.get("moves") or [{}])[0].get("move_description") == turn.get("played_move")),
            None,
        )
        if not best or played is None:
            return None
        equity_lost = best["final_spread"] - played["final_spread"]
        split = _variation_split
    mine_p, opp_p = split(played)
    mine_b, opp_b = split(best)
    return {
        "equity_lost": equity_lost,
        "offense_delta": mine_p - mine_b,
        "defense_delta": opp_b - opp_p,
    }


def opp_racks_complete(analysis_turns, opp_idx):
    """True iff the opponent's rack is fully known on every turn.

    In a partially-annotated game (only Jesse's side tracked), the opponent's
    recorded `rack` is just the tiles they played that turn — so any opponent
    rack shorter than 7 while the bag still has tiles marks the game partial.
    Short racks are legitimate only once the bag is empty (endgame)."""
    return all(
        len(t.get("rack") or "") == 7 or (t.get("tiles_in_bag") or 0) == 0
        for t in analysis_turns
        if t.get("player_index") == opp_idx
    )


def compute_game(r, subject=None):
    """Compute per-game stats. `subject` is None (default: Jesse) or
    {"nickname", "real_name"} to re-key identity/naming to a different player
    (see make_is_subject)."""
    is_subject = make_is_subject(subject)

    meta = r["meta"]
    history = r["history"]["history"]
    analysis = r["analysis"]["result"]
    events = history.get("events") or []

    jesse_idx = next(i for i, p in enumerate(history["players"]) if is_subject(p))
    opp_idx = 1 - jesse_idx

    jesse_score = history["final_scores"][jesse_idx]
    opp_score = history["final_scores"][opp_idx]
    opp_name = get_opp_name(meta, history["players"], jesse_idx)
    opp_username = woogles_username(history["players"][opp_idx])
    # Annotator uploads live at /anno/<id>, games actually played on Woogles at
    # /game/<id> — linking a played game as /anno/ yields a dead page.
    url_path = "anno" if meta.get("is_annotated") else "game"
    game_url = f'https://woogles.io/{url_path}/{meta["game_id"]}'

    summary = summary_for_index(analysis, jesse_idx)
    mistake_index = summary["mistake_index"] if summary else None

    opp_summary = summary_for_index(analysis, opp_idx)
    opp_mistake_index = opp_summary["mistake_index"] if opp_summary else None
    game_is_over = history.get("play_state") == "GAME_OVER"
    opp_fully_annotated = (
        opp_mistake_index is not None
        and game_is_over
        and opp_racks_complete(analysis["turns"], opp_idx)
    )  # full 7-tile racks every opp turn AND game finished — see note above

    jesse_bingos = opp_bingos = 0
    for t in analysis["turns"]:
        if t.get("played_is_bingo"):
            if t["player_index"] == jesse_idx:
                jesse_bingos += 1
            else:
                opp_bingos += 1

    # blanks_played and high_turn are what the Woogles league standings table
    # publishes, so track them separately from "blanks drawn" (which also counts
    # blanks exchanged away or stranded on the final rack) to keep the league
    # cross-check comparing like with like.
    jesse_blanks = jesse_blanks_played = jesse_high_turn = 0
    for ev in (history.get("events") or []):
        if ev["player_index"] != jesse_idx:
            continue
        if ev["type"] == "TILE_PLACEMENT_MOVE":
            blanks = sum(1 for c in (ev.get("played_tiles") or "") if c.islower())
            jesse_blanks += blanks
            jesse_blanks_played += blanks
            jesse_high_turn = max(jesse_high_turn, ev.get("score") or 0)
        elif ev["type"] == "EXCHANGE":
            jesse_blanks += (ev.get("exchanged") or "").count("?")
    last_racks = history.get("last_known_racks") or []
    if len(last_racks) > jesse_idx and last_racks[jesse_idx]:
        jesse_blanks += last_racks[jesse_idx].count("?")

    # Blanks the opponent DREW. Only meaningful when their racks are fully known
    # (every league game; only occasionally a tournament upload) — the report
    # shows "—" otherwise, since a single-side annotation can only ever see the
    # blanks they actually played.
    opp_blanks = 0
    for ev in (history.get("events") or []):
        if ev["player_index"] != opp_idx:
            continue
        if ev["type"] == "TILE_PLACEMENT_MOVE":
            opp_blanks += sum(1 for c in (ev.get("played_tiles") or "") if c.islower())
        elif ev["type"] == "EXCHANGE":
            opp_blanks += (ev.get("exchanged") or "").count("?")
    if len(last_racks) > opp_idx and last_racks[opp_idx]:
        opp_blanks += last_racks[opp_idx].count("?")

    snapshots, racks = build_snapshots_and_racks(events)
    played_words = build_played_words(events)
    turn_events = build_turn_event_indices(events)

    endgame_spread_lost = win_prob_lost = phonies_played = missed_bingos = 0
    opp_win_prob_lost = opp_endgame_spread_lost = 0
    missed_bingo_words = []
    opp_missed_bingo_words = []
    missed_bingo_urls = []  # parallel to *_words: the ?turn= link for each entry
    opp_missed_bingo_urls = []
    jesse_phonies = []  # [{'words_formed', 'challenged'}]
    opp_phonies = []  # [{'words_formed', 'challenged'}]
    errors = []  # every one of the subject's turns that cost win%, richest-first later
    for turn_idx, t in enumerate(analysis["turns"]):
        # is_phony/missed_bingo are evaluated for BOTH players — mention them either way
        if t.get("is_phony"):
            mv = played_words[turn_idx] if turn_idx < len(played_words) else None
            entry = {
                "words_formed": mv["words_formed"] if mv else [],
                "challenged": mv["challenged"] if mv else t.get("phony_challenged"),
            }
            (jesse_phonies if t["player_index"] == jesse_idx else opp_phonies).append(entry)
        if t.get("missed_bingo"):
            om = t.get("optimal_move") or ""
            hist_rack = racks[turn_idx] if turn_idx < len(racks) else ""
            if validate_bingo(om, hist_rack):  # else: analysis false positive — rack mismatch
                word = resolve_bingo_word(om, snapshots[turn_idx]) if turn_idx < len(snapshots) else om
                # Deep link to the position the bingo was available in, so the
                # report row is one click from the board it came off.
                url = (
                    turn_url(game_url, turn_events[turn_idx])
                    if turn_idx < len(turn_events)
                    else game_url
                )
                if t["player_index"] == jesse_idx:
                    missed_bingos += 1
                    missed_bingo_words.append(word)
                    missed_bingo_urls.append(url)
                else:
                    opp_missed_bingo_words.append(word)
                    opp_missed_bingo_urls.append(url)

        if t["player_index"] != jesse_idx:
            if opp_fully_annotated:
                opp_win_prob_lost += t.get("win_prob_loss") or 0
                if t.get("tiles_in_bag") == 0:
                    opp_endgame_spread_lost += t.get("spread_loss") or 0
            continue
        if t.get("tiles_in_bag") == 0:
            endgame_spread_lost += t.get("spread_loss") or 0
        win_prob_lost += t.get("win_prob_loss") or 0
        if t.get("is_phony"):
            phonies_played += 1

        # The error log. Every turn that cost win% is listed, not just the big ones:
        # the point of the section is to be studyable end to end, and the ranking
        # already buries the trivia at the bottom. A turn that cost no win% but a
        # real amount of spread is an error too (see FLAT_WIN_PROB). was_optimal
        # turns are never errors, whatever the numbers say.
        wpl = t.get("win_prob_loss") or 0
        eq = equity_breakdown(t)
        eq_lost = eq["equity_lost"] if eq else 0
        spread_only = wpl <= FLAT_WIN_PROB and eq_lost >= SPREAD_ONLY_EQUITY
        if (wpl > 0 or spread_only) and not t.get("was_optimal"):
            board = snapshots[turn_idx] if turn_idx < len(snapshots) else None
            errors.append({
                "turn_number": t.get("turn_number"),
                "rack": t.get("rack") or "",
                "played": readable_move(t.get("played_move"), board) if board else (t.get("played_move") or ""),
                "played_score": t.get("played_score"),
                "optimal": readable_move(t.get("optimal_move"), board) if board else (t.get("optimal_move") or ""),
                "optimal_score": t.get("optimal_score"),
                "win_prob_loss": wpl,
                "spread_loss": t.get("spread_loss") or 0,
                # None (not 0) when the analysis offers nothing to compare against —
                # the table shows "—" rather than implying a cost of zero.
                "equity_lost": eq["equity_lost"] if eq else None,
                "offense_delta": eq["offense_delta"] if eq else None,
                "defense_delta": eq["defense_delta"] if eq else None,
                "spread_only": spread_only,
                "mistake_size": t.get("mistake_size"),
                "stage": game_stage(t),
                "is_phony": bool(t.get("is_phony")),
                "missed_bingo": bool(t.get("missed_bingo")),
                "url": (
                    turn_url(game_url, turn_events[turn_idx])
                    if turn_idx < len(turn_events)
                    else game_url
                ),
            })

    # "Seed <n>" is how league collections encode their ordering: a league is a
    # round robin with no meaningful round order, so games are sequenced by
    # opponent strength instead (see woogles_league.py).
    rd_match = re.search(r"(?:Rd\.?\s*|Round\s*|Seed\s*)(\d+)", meta["chapter_title"], re.I)
    rnd = int(rd_match.group(1)) if rd_match else meta["chapter_number"]

    return {
        "round": rnd,
        "game_id": meta["game_id"],
        "title": meta["chapter_title"],
        "opponent": opp_name,
        "opp_username": opp_username,
        "game_url": game_url,
        "lexicon": history.get("lexicon"),
        # VOID (the Woogles league default) rejects invalid plays outright, so a
        # phony can never reach the board and every phony statistic is trivially
        # zero — see void_challenge in aggregate().
        "challenge_rule": history.get("challenge_rule"),
        "jesse_score": jesse_score,
        "opp_score": opp_score,
        # Draws are rare but real (equal final scores) — never fold one into "L".
        "result": "W" if jesse_score > opp_score else ("L" if jesse_score < opp_score else "D"),
        "mistake_index": mistake_index,
        "opp_mistake_index": opp_mistake_index,
        "opp_fully_annotated": opp_fully_annotated,
        "jesse_bingos": jesse_bingos,
        "opp_bingos": opp_bingos,
        "jesse_blanks": jesse_blanks,
        "opp_blanks": opp_blanks,  # only trustworthy when opp_fully_annotated
        "jesse_blanks_played": jesse_blanks_played,
        "jesse_high_turn": jesse_high_turn,
        "endgame_spread_lost": endgame_spread_lost,
        "win_prob_lost": win_prob_lost,  # multiply by 100 for %
        "opp_endgame_spread_lost": opp_endgame_spread_lost,  # only valid when opp_fully_annotated
        "opp_win_prob_lost": opp_win_prob_lost,  # only valid when opp_fully_annotated; multiply by 100 for %
        "phonies_played": phonies_played,
        "opp_phonies_played": len(opp_phonies),
        "missed_bingos": missed_bingos,
        "missed_bingo_words": missed_bingo_words,
        "opp_missed_bingo_words": opp_missed_bingo_words,
        "missed_bingo_urls": missed_bingo_urls,
        "opp_missed_bingo_urls": opp_missed_bingo_urls,
        "jesse_phonies": jesse_phonies,
        "opp_phonies": opp_phonies,
        "errors": errors,
    }


def check_words(lexicon, words, retries=4):
    """Batch-check word validity in a lexicon via woogles.io's public word_service.

    One call per lexicon (see check_phony_words below) — never per word or per game.
    That keeps this well clear of rate limits regardless of collection size, but the
    retry/backoff still guards against a transient 429/5xx from the shared endpoint."""
    if not lexicon or not words:
        return {}
    for attempt in range(retries):
        try:
            r = requests.post(
                f"{BASE}/word_service.WordService/DefineWords",
                json={"lexicon": lexicon, "words": sorted(words), "definitions": False, "anagrams": False},
            )
            if r.status_code == 429 or r.status_code >= 500:
                if attempt == retries - 1:
                    return {}
                time.sleep((2 ** attempt) + random.uniform(0, 0.5))
                continue
            r.raise_for_status()
            return {w: res["v"] for w, res in r.json()["results"].items()}
        except requests.RequestException:
            if attempt == retries - 1:
                return {}  # no egress / persistent hiccup — caller falls back to starring the whole play
            time.sleep((2 ** attempt) + random.uniform(0, 0.5))
    return {}


def check_phony_words(stats):
    """Populate each phony entry's `invalid_words` — the subset of words_formed that
    are actually not in the lexicon. Batches one API call per distinct lexicon seen."""
    by_lexicon = {}
    for g in stats:
        for p in g.get("jesse_phonies", []) + g.get("opp_phonies", []):
            by_lexicon.setdefault(g.get("lexicon"), set()).update(w.upper() for w in p["words_formed"])

    validity = {lex: check_words(lex, words) for lex, words in by_lexicon.items()}

    for g in stats:
        lex_validity = validity.get(g.get("lexicon"), {})
        for p in g.get("jesse_phonies", []) + g.get("opp_phonies", []):
            # empty dict (API unreachable) -> invalid_words stays None -> caller falls back
            p["invalid_words"] = (
                [w for w in p["words_formed"] if not lex_validity.get(w.upper(), True)] if lex_validity else None
            )


def sp_str(v):
    return f"+{v}" if v >= 0 else f"−{abs(v)}"


def pts_str(v):
    """A points figure to one decimal, "—" when the analysis gave us none.

    Simulated equity is a mean over thousands of iterations, so it is a real
    fraction rather than a whole number of points; endgame figures are exact but
    print the same way so a column never mixes formats."""
    return "—" if v is None else f"{v:.1f}".replace("-", "−")


def signed_pts_str(v):
    """Same, with an explicit + on gains — for columns where the sign is the point."""
    return "—" if v is None else (f"+{v:.1f}" if v >= 0 else f"−{abs(v):.1f}")


def aggregate(stats):
    n = len(stats)
    wins = sum(1 for g in stats if g["result"] == "W")
    draws = sum(1 for g in stats if g["result"] == "D")
    mi_games = [g for g in stats if g["mistake_index"] is not None]
    opp_ann_games = [g for g in stats if g["opp_fully_annotated"]]  # opponent racks known all game — see Notes

    total_jb = sum(g["jesse_bingos"] for g in stats)
    total_mb = sum(g["missed_bingos"] for g in stats)
    total_ob = sum(g["opp_bingos"] for g in stats)
    total_bl = sum(g["jesse_blanks"] for g in stats)
    total_eg = sum(g["endgame_spread_lost"] for g in stats)
    # Opponent counterparts to Jesse's own stats. A league game is played, not
    # annotated, so both racks are known on every turn and the opponent can be
    # measured exactly as Jesse is; a tournament upload usually can't be, so
    # every one of these is computed over `opp_ann_games` only.
    n_ann = len(opp_ann_games)
    total_opp_mb = sum(len(g["opp_missed_bingo_words"]) for g in opp_ann_games)
    total_ob_ann = sum(g["opp_bingos"] for g in opp_ann_games)
    total_opp_bl = sum(g["opp_blanks"] for g in opp_ann_games)
    total_opp_eg = sum(g["opp_endgame_spread_lost"] for g in opp_ann_games)

    total_ph = sum(g["phonies_played"] for g in stats)
    total_opp_ph = sum(g["opp_phonies_played"] for g in stats)
    total_sp = sum(g["jesse_score"] - g["opp_score"] for g in stats)

    # VOID challenge = the client refuses an invalid play, so nobody CAN play a
    # phony. Reporting "0 phonies" there is not a finding, and praising it is
    # actively wrong; every phony stat is suppressed for such a collection.
    void_challenge = bool(stats) and all(g.get("challenge_rule") == "VOID" for g in stats)

    return {
        "void_challenge": void_challenge,
        # "9-10" normally, "9-10-1" only when a draw actually happened — a trailing
        # "-0" on every report would churn every cached digest to say nothing.
        "record": f"{wins}-{n-wins-draws}{f'-{draws}' if draws else ''} {sp_str(total_sp)}",
        "avg_jesse": round(sum(g["jesse_score"] for g in stats) / n, 1),
        "avg_opp": round(sum(g["opp_score"] for g in stats) / n, 1),
        "avg_mi": round(sum(g["mistake_index"] for g in mi_games) / len(mi_games), 2) if mi_games else None,
        "total_jb": total_jb,
        "bingo_find_rate": (
            f"{total_jb}/{total_jb+total_mb} ({round(total_jb/(total_jb+total_mb)*100,1)}%)"
            if (total_jb + total_mb)
            else "N/A"
        ),
        "total_ob": total_ob,
        "total_bl": total_bl,
        "avg_eg": round(total_eg / n, 1),
        "avg_wpl": round(sum(g["win_prob_lost"] for g in stats) / n * 100, 1),
        "total_phonies": None if void_challenge else total_ph,
        "total_opp_phonies": None if void_challenge else total_opp_ph,
        "games_per_mb": round(n / total_mb, 1) if total_mb else None,
        "games_per_phony": round(n / total_ph, 1) if total_ph and not void_challenge else None,
        "n_opp_annotated": n_ann,
        "opp_bingo_find_rate": (
            f"{total_ob_ann}/{total_ob_ann+total_opp_mb} "
            f"({round(total_ob_ann/(total_ob_ann+total_opp_mb)*100,1)}%)"
            if (total_ob_ann + total_opp_mb)
            else "N/A"
        ),
        "total_opp_mb": total_opp_mb,
        "games_per_opp_mb": round(n_ann / total_opp_mb, 1) if total_opp_mb else None,
        "total_opp_bl": total_opp_bl,
        "avg_opp_eg": round(total_opp_eg / n_ann, 1) if n_ann else None,
        "avg_opp_mi": (
            round(sum(g["opp_mistake_index"] for g in opp_ann_games) / len(opp_ann_games), 2)
            if opp_ann_games
            else None
        ),
        "avg_opp_wpl": (
            round(sum(g["opp_win_prob_lost"] for g in opp_ann_games) / len(opp_ann_games) * 100, 1)
            if opp_ann_games
            else None
        ),
        "n": n,
        "mi_games": len(mi_games),
    }


def _game_note(g, missed_bingo_counter):
    """Build one game's note text. Returns (note, updated_missed_bingo_counter)."""
    mi = g["mistake_index"] if g["mistake_index"] is not None else 0.0
    wpl = round(g["win_prob_lost"] * 100, 1)
    eg = g["endgame_spread_lost"]
    mb = g["missed_bingos"]
    jb = g["jesse_bingos"]
    ob = g["opp_bingos"]
    sp = g["jesse_score"] - g["opp_score"]
    res = g["result"]
    ws = g.get("missed_bingo_words", [])
    opp_ws = g.get("opp_missed_bingo_words", [])
    opp_name = g["opponent"]
    parts = []
    # Primary — exactly one (elif chain, most notable wins). Skipped entirely if nothing
    # matches AND secondary facts exist below — don't force a generic label onto an
    # already-informative note.
    if g["jesse_score"] >= 570:
        parts.append(f'{g["jesse_score"]}-pt monster')
    elif mi <= 0.8:
        parts.append("very clean")
    elif mi > 3.5:
        parts.append("errorful win" if res == "W" else "errorful")
    elif abs(sp) <= 15:
        # A draw is sp == 0, so it lands here — and "narrow win" would be wrong.
        parts.append({"W": "narrow win", "L": "narrow loss", "D": "draw"}[res])
    elif res == "L" and abs(sp) >= 175:
        parts.append("blowout")
    elif res == "W" and sp >= 150:
        parts.append(f"+{sp} dominant")
    # Jesse's own phonies — always named, `*` marks the phony, all words_formed shown
    # since the specific invalid word isn't identifiable when the play formed several
    for p in g.get("jesse_phonies", []):
        tag = " (unchallenged)" if not p["challenged"] else ""
        parts.append(f"phony {'/'.join(p['words_formed'])}*{tag}")
    # Missed bingos — cumulative numbering + word, one clause per game
    if mb == 1:
        missed_bingo_counter += 1
        parts.append(f"missed bingo #{missed_bingo_counter} ({ws[0]})")
    elif mb > 1:
        start = missed_bingo_counter + 1
        missed_bingo_counter += mb
        parts.append(f"missed bingos #{start}–#{missed_bingo_counter} ({', '.join(ws)})")
    # Opponent's phonies and missed bingos — named, attributed by name
    for p in g.get("opp_phonies", []):
        tag = " (unchallenged)" if not p["challenged"] else ""
        parts.append(f"{opp_name} phony {'/'.join(p['words_formed'])}*{tag}")
    miss_verb = "passed up" if "nigel" in opp_name.lower() else "missed"
    for w in opp_ws:
        parts.append(f"{opp_name} {miss_verb} {w}")
    # Everything else stays brief and low-priority
    if len(parts) < 4 and (ob >= 4 or (ob >= 3 and res == "L")):
        parts.append(f"opp {ob} bingos")
    if len(parts) < 4 and jb >= 4 and res == "W":
        parts.append(f"{jb} bingos")
    if len(parts) < 4 and eg >= 50:
        parts.append(f"{eg}-pt endgame")
    elif len(parts) < 4 and eg >= 30 and g["jesse_score"] < 570:
        parts.append("big endgame")
    if len(parts) < 4 and wpl >= 40:
        parts.append("equity leak")
    if not parts:
        parts.append("solid win" if res == "W" else "competitive")
    return "; ".join(parts), missed_bingo_counter


def game_notes(stats):
    """Return one note string per game in `stats` (already sorted by round).

    Owns the missed_bingo_counter as a local closure — resets to 0 each call, so
    numbering matches the Missed Bingos table row order for THIS report render.
    """
    missed_bingo_counter = 0
    notes = []
    for g in stats:
        note, missed_bingo_counter = _game_note(g, missed_bingo_counter)
        notes.append(note)
    return notes


def _progression(stats):
    # 🟩 win / 🟥 loss / 🟨 draw
    box = {"W": "\U0001f7e9", "L": "\U0001f7e5", "D": "\U0001f7e8"}
    boxes = [box[g["result"]] for g in stats]
    return " ".join("".join(boxes[i : i + 5]) for i in range(0, len(boxes), 5))


def _missed_bingo_cells(g, words_key, urls_key):
    """Each missed bingo as a markdown link to the turn it was available on."""
    return [
        f"[{w}]({url})"
        for w, url in zip(g.get(words_key) or [], g.get(urls_key) or [])
    ]


def error_log_rows(stats):
    """Every error the subject made across the collection, worst first.

    One row per turn that cost win probability or spread, ranked by the win% loss
    — the whole point is that the study order is "biggest leak first", not
    chronological, so the ordering is global across games rather than per game.
    Spread-only errors (win% flat, equity thrown away) therefore land in a block
    at the foot of the table, ordered among themselves by the spread they cost.
    """
    rows = [dict(e, round=g["round"], game=g) for g in stats for e in g.get("errors") or []]
    # Ties broken by equity lost then by game/turn, so the table is stable across
    # renders (a shuffling table churns the report diff for no reason).
    rows.sort(key=lambda e: (-e["win_prob_loss"], -(e["equity_lost"] or 0),
                             e["round"], e["turn_number"] or 0))
    return rows


def error_log_section(stats, short_label="Rd"):
    """The "All Errors" table, or "" when the collection has no analyzed errors."""
    rows = error_log_rows(stats)
    if not rows:
        return ""
    lines = [
        "## All Errors",
        "",
        "*Every turn that cost win probability, worst first, plus turns where win% "
        "barely moved but at least "
        f"{SPREAD_ONLY_EQUITY} points of equity went with it (flagged \"spread only\", "
        "and grouped at the foot of the table). The turn number links to that position "
        "on woogles.io, with the rack still to play. Lowercase = blank tile; (X) = a "
        "tile already on the board.*",
        "",
        "*Equity Lost is the spread the simulation says the played move gave up "
        "against Best; a negative figure means the played move was better on spread "
        "and Best bought win probability with it, which is normal when protecting a "
        "lead. Off Δ and Def Δ split that comparison into scoring — how many more "
        "points the played move scores for Jesse across the simulated line, and how "
        "many fewer it concedes to the opponent — both signed so positive is good. "
        "They sum to roughly −Equity Lost; the remainder is what the spread depends "
        "on besides the plays' own scores — the value of the leave, and the going-out "
        "bonus and unplayed tiles at the end of an endgame.*",
        "",
        f"| Win% Lost | Equity Lost | Off Δ | Def Δ | {short_label} | Opponent | Turn | "
        "Stage | Rack | Played | Best | Flags |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for e in rows:
        g = e["game"]
        flags = []
        if e["missed_bingo"]:
            flags.append("missed bingo")
        if e["is_phony"]:
            flags.append("phony")
        if e["spread_only"]:
            flags.append("spread only")
        turn_cell = f"[{e['turn_number']}]({e['url']})" if e["turn_number"] is not None else f"[↗]({e['url']})"
        lines.append(
            f"| {round(e['win_prob_loss'] * 100, 1)}% | {pts_str(e['equity_lost'])} | "
            f"{signed_pts_str(e['offense_delta'])} | {signed_pts_str(e['defense_delta'])} | "
            f"{e['round']} | {opponent_handle(g)} | "
            f"{turn_cell} | {e['stage'] or '—'} | {e['rack']} | {e['played']} ({e['played_score']}) | "
            f"{e['optimal']} ({e['optimal_score']}) | {', '.join(flags)} |"
        )
    return "\n".join(lines)


def render_report(stats, agg, notes, title, summary_md=None, subject_display="Jesse Day",
                  round_label=None, extra_sections=None, lead_sections=None,
                  error_log=False):
    """Render the report markdown.

    `round_label` renames the ordering column when games aren't sequenced by
    round — a league is a round robin whose games have no meaningful order, so it
    passes "Seed" and the column reports opponent strength instead. Setting it
    also suppresses the missing-rounds note, which is meaningless for an ordering
    that is contiguous by construction.

    `lead_sections` is a list of ready-made markdown blocks placed directly under
    the header, before Aggregate Stats — a league's standings go here, since the
    division table is the first thing worth seeing in a league report.

    `extra_sections` is a list of ready-made markdown blocks appended after the
    per-game tables and before the Summary (the league cross-check uses it).

    `error_log` adds the ranked "All Errors" section (see error_log_section). It is
    off by default so that existing tournament reports render byte-identically;
    league reports turn it on (woogles_league.report_extras).
    """
    n = agg["n"]
    col_label = round_label or "Rnd"
    short_label = round_label or "Rd"
    # Only a seeded collection (a league) has a seed to show next to the opponent;
    # in a normal tournament the ordering column is the round number, not a rank.
    seed_of = (lambda g: g["round"]) if round_label == "Seed" else (lambda g: None)
    lines = [f"# {title}"]

    missing_rounds_note = ""
    rounds = sorted(g["round"] for g in stats)
    expected = list(range(rounds[0], rounds[-1] + 1)) if rounds else []
    missing = sorted(set(expected) - set(rounds))
    if missing and round_label is None:
        label = "Rd" if len(missing) == 1 else "Rds"
        missing_rounds_note = f" ({label} {', '.join(map(str, missing))} missing)"
    lines.append(
        f"**Collection:** {title} | **Games:** {n}{missing_rounds_note} | **Record:** {agg['record']}"
    )
    lines.append("")
    lines.append(_progression(stats))
    lines.append("")
    for section in lead_sections or []:
        lines.append(section.strip())
        lines.append("")
    lines.append(f"## Aggregate Stats ({subject_display})")
    lines.append("")
    lines.append("| Stat | Value |")
    lines.append("|---|---|")

    mi_qualifier = ""
    if agg["mi_games"] < n:
        unavailable_rounds = sorted(g["round"] for g in stats if g["mistake_index"] is None)
        mi_qualifier = (
            f" (over {agg['mi_games']} games; {short_label}s "
            f"{', '.join(map(str, unavailable_rounds))} unavailable)"
        )
    avg_mi_str = f"{agg['avg_mi']:.2f}{mi_qualifier}" if agg["avg_mi"] is not None else "—"
    lines.append(f"| Average Mistakes Score | {avg_mi_str} |")
    lines.append(f"| Average Score | {agg['avg_jesse']:.1f} |")
    lines.append(f"| Average Opponent Score | {agg['avg_opp']:.1f} |")
    lines.append(f"| Total Bingos | {agg['total_jb']} ({round(agg['total_jb']/n,2):.2f}/game) |")
    lines.append(f"| Bingo Find Rate | {agg['bingo_find_rate']} |")
    lines.append(f"| Opponent Total Bingos | {agg['total_ob']} ({round(agg['total_ob']/n,2):.2f}/game) |")
    lines.append(f"| Total Blanks Drawn | {agg['total_bl']} ({round(agg['total_bl']/n,2):.2f}/game) |")
    lines.append(f"| Avg Endgame Spread Lost | {agg['avg_eg']:.1f} |")
    lines.append(f"| Avg Win% Lost | {agg['avg_wpl']:.1f}% |")
    if not agg.get("void_challenge"):
        lines.append(f"| Total Phonies Played | {agg['total_phonies']} |")
        lines.append(f"| Opponent Phonies Played | {agg['total_opp_phonies']} |")
    if agg["games_per_mb"] is not None:
        lines.append(f"| Games per Missed Bingo | {agg['games_per_mb']} |")
    if agg["games_per_phony"] is not None:
        lines.append(f"| Games per Phony Played | {agg['games_per_phony']} |")

    if agg["n_opp_annotated"] > 0:
        n_ann = agg["n_opp_annotated"]
        opp_qualifier = "" if n_ann == n else f" (over {n_ann} fully-annotated games)"
        lines.append(f"| Average Opponent Mistakes Score | {agg['avg_opp_mi']:.2f}{opp_qualifier} |")
        lines.append(f"| Opponent Bingo Find Rate | {agg['opp_bingo_find_rate']}{opp_qualifier} |")
        lines.append(
            f"| Opponent Missed Bingos | {agg['total_opp_mb']} "
            f"({round(agg['total_opp_mb']/n_ann,2):.2f}/game){opp_qualifier} |"
        )
        # Only meaningful while misses are rarer than one a game; past that the
        # "(N/game)" rate above already says it, and "0.8 games per miss" reads
        # like nonsense.
        if agg["games_per_opp_mb"] is not None and agg["games_per_opp_mb"] >= 1:
            lines.append(f"| Games per Opponent Missed Bingo | {agg['games_per_opp_mb']}{opp_qualifier} |")
        lines.append(
            f"| Opponent Blanks Drawn | {agg['total_opp_bl']} "
            f"({round(agg['total_opp_bl']/n_ann,2):.2f}/game){opp_qualifier} |"
        )
        lines.append(f"| Avg Opponent Endgame Spread Lost | {agg['avg_opp_eg']:.1f}{opp_qualifier} |")
        lines.append(f"| Average Opponent Win% Lost | {agg['avg_opp_wpl']:.1f}%{opp_qualifier} |")

    lines.append("")
    lines.append("## Per-Game Breakdown")
    lines.append("")
    phony_note = (
        "These games are played under the VOID challenge rule, which rejects invalid plays "
        "outright — a phony cannot be played at all, so no phony statistics are reported and "
        "mistakes scores run slightly lower here than in challenge-rule play, where playing a "
        "phony is an available error. "
        if agg.get("void_challenge")
        else "`*` marks a phony (all words formed by the play; the specific invalid word "
        "isn't always identifiable when multiple words were formed — CSW is the configured "
        "lexicon for every game). "
    )
    lines.append(
        f"*Notes: {phony_note}The Opp columns measure the opponent exactly as the "
        "columns to their left measure Jesse, and show \"—\" for games where the "
        "opponent's rack wasn't fully known (not livestreamed or double-annotated).*"
    )
    lines.append("")

    show_opp_cols = agg["n_opp_annotated"] > 0
    header = (
        f"| {col_label} | Game | Opponent | Result | Jesse | Opp | Spread | Mistakes | Jesse Bingos | "
        "Missed Bingos | Opp Bingos | Jesse Blanks | Endgame Spread Lost | Win% Lost"
    )
    sep = "|---|---|---|---|---|---|---|---|---|---|---|---|---|---"
    if show_opp_cols:
        # Mirrors Jesse's own columns, in the same order, for the games where the
        # opponent's every rack is known.
        header += (
            " | Opp Mistakes | Opp Missed Bingos | Opp Blanks | Opp Endgame Spread Lost "
            "| Opp Win% Lost"
        )
        sep += "|---|---|---|---|---"
    header += " | Notes |"
    sep += "|---|"
    lines.append(header)
    lines.append(sep)

    for g, note in zip(stats, notes):
        mi_str = f"{g['mistake_index']:.1f}" if g["mistake_index"] is not None else "—"
        row = (
            f"| {g['round']} | [↗]({g['game_url']}) | "
            f"{opponent_cell(g['opponent'], g.get('opp_username'), seed=seed_of(g))} | {g['result']} | "
            f"{g['jesse_score']} | {g['opp_score']} | {sp_str(g['jesse_score']-g['opp_score'])} | "
            f"{mi_str} | {g['jesse_bingos']} | {g['missed_bingos']} | {g['opp_bingos']} | "
            f"{g['jesse_blanks']} | {g['endgame_spread_lost']} | {round(g['win_prob_lost']*100,1)}%"
        )
        if show_opp_cols:
            ann = g["opp_fully_annotated"]
            opp_mi_str = f"{g['opp_mistake_index']:.1f}" if ann else "—"
            opp_wpl_str = f"{round(g['opp_win_prob_lost']*100,1)}%" if ann else "—"
            opp_mb_str = str(len(g["opp_missed_bingo_words"])) if ann else "—"
            opp_bl_str = str(g["opp_blanks"]) if ann else "—"
            opp_eg_str = str(g["opp_endgame_spread_lost"]) if ann else "—"
            row += f" | {opp_mi_str} | {opp_mb_str} | {opp_bl_str} | {opp_eg_str} | {opp_wpl_str}"
        row += f" | {note} |"
        lines.append(row)

    avg_mi_cell = f"{agg['avg_mi']:.2f}" if agg["avg_mi"] is not None else "—"
    avg_row = (
        f"| **Avg** | | | | **{agg['avg_jesse']:.1f}** | **{agg['avg_opp']:.1f}** | "
        f"**{sp_str(round(sum(g['jesse_score']-g['opp_score'] for g in stats)/n,1))}** | "
        f"**{avg_mi_cell}** | **{round(agg['total_jb']/n,2):.2f}** | "
        f"**{round(sum(g['missed_bingos'] for g in stats)/n,2):.2f}** | "
        f"**{round(agg['total_ob']/n,2):.2f}** | **{round(agg['total_bl']/n,2):.2f}** | "
        f"**{agg['avg_eg']:.1f}** | **{agg['avg_wpl']:.1f}%**"
    )
    if show_opp_cols:
        n_ann = agg["n_opp_annotated"]
        # Only worth spelling out the denominator when it isn't every game —
        # "(12 games)" under a 12-game report is noise.
        opp_note = "" if n_ann == n else f" ({n_ann} games)"
        avg_row += (
            f" | **{agg['avg_opp_mi']:.2f}**\\*{opp_note} "
            f"| **{round(agg['total_opp_mb']/n_ann,2):.2f}**\\*{opp_note} "
            f"| **{round(agg['total_opp_bl']/n_ann,2):.2f}**\\*{opp_note} "
            f"| **{agg['avg_opp_eg']:.1f}**\\*{opp_note} "
            f"| **{agg['avg_opp_wpl']:.1f}%**\\*{opp_note}"
        )
    avg_row += " | |"
    lines.append(avg_row)

    if show_opp_cols:
        lines.append("")
        lines.append(
            "\\*Every Opp column's average is computed only over the fully-annotated games "
            "(denominator = n_opp_annotated, not the full game count)."
        )

    lines.append("")
    lines.append("## Missed Bingos")
    lines.append("")
    lines.append(
        "*Lowercase = blank tile; (X) = board tile X was already there; "
        "the word links to that turn on woogles.io*"
    )
    lines.append("")
    lines.append(f"| {short_label} | Opponent | Word |")
    lines.append("|---|---|---|")
    for g in stats:
        for cell in _missed_bingo_cells(g, "missed_bingo_words", "missed_bingo_urls"):
            lines.append(f"| {g['round']} | {opponent_handle(g)} | {cell} |")

    # The opponent's own misses, in the identical format. Listed only for games
    # annotated on both sides (in a league, that is every game; in a tournament
    # upload, only the doubly-annotated ones), so the section is omitted entirely
    # when no game qualifies. Keeping the two tables separate is also what stops
    # a miss of Jesse's being attributed to his opponent.
    opp_mb_rows = [
        (g["round"], opponent_handle(g), cell)
        for g in stats
        if g["opp_fully_annotated"]
        for cell in _missed_bingo_cells(g, "opp_missed_bingo_words", "opp_missed_bingo_urls")
    ]
    if opp_mb_rows:
        lines.append("")
        lines.append("## Opponent Missed Bingos")
        lines.append("")
        lines.append(
            "*Lowercase = blank tile; (X) = board tile X was already there; "
            "the word links to that turn on woogles.io*"
        )
        lines.append("")
        lines.append(f"| {short_label} | Opponent | Word |")
        lines.append("|---|---|---|")
        for rnd, opp, cell in opp_mb_rows:
            lines.append(f"| {rnd} | {opp} | {cell} |")

    if error_log:
        section = error_log_section(stats, short_label)
        if section:
            lines.append("")
            lines.append(section)

    for section in extra_sections or []:
        lines.append("")
        lines.append(section.strip())

    if summary_md:
        lines.append("")
        lines.append("## Summary")
        lines.append(summary_md.strip())

    return "\n".join(lines) + "\n"


DIGEST_TOP_ERRORS = 12


def _error_digest_lines(stats, short_label="Rd"):
    """The error-log block of the digest: a stage breakdown plus the worst errors.

    Feeds the Summary the same material the "All Errors" section shows a reader, so
    it can say *where* the win% went (a run of pre-endgame leaks is a different
    lesson from a run of missed bingos) instead of restating the aggregate averages.
    Only the top DIGEST_TOP_ERRORS rows are listed — the tail is a long tail of
    sub-1% turns that would drown the signal and the prompt alike.
    """
    rows = error_log_rows(stats)
    if not rows:
        return []
    total = sum(e["win_prob_loss"] for e in rows)
    equity = sum(e["equity_lost"] or 0 for e in rows)
    lines = [
        f"Errors (every turn that cost win probability, plus turns where win% was flat "
        f"but spread was thrown away; {len(rows)} in total, "
        f"{round(total * 100, 1)}% of win probability and {round(equity, 1)} points of "
        f"equity lost across the collection):"
    ]
    for stage in STAGES:
        in_stage = [e for e in rows if e["stage"] == stage]
        if not in_stage:
            continue
        lost = sum(e["win_prob_loss"] for e in in_stage)
        # A collection can consist entirely of spread-only errors (an endgame-heavy
        # run where nothing swung the result), so never divide by the win% total.
        share = f" ({round(lost / total * 100)}% of the total)" if total else ""
        lines.append(
            f"  {stage}: {len(in_stage)} errors, {round(lost * 100, 1)}% win prob lost"
            f"{share}, {round(sum(e['equity_lost'] or 0 for e in in_stage), 1)} equity lost"
        )
    mb = [e for e in rows if e["missed_bingo"]]
    if mb:
        lines.append(
            f"  of which missed bingos: {len(mb)}, "
            f"{round(sum(e['win_prob_loss'] for e in mb) * 100, 1)}% win prob lost"
        )
    def error_line(e):
        tags = []
        if e["missed_bingo"]:
            tags.append("MISSED BINGO")
        if e["is_phony"]:
            tags.append("phony")
        if e["equity_lost"] is not None:
            # The offence/defence split is what turns "this cost 9 points" into a
            # lesson: points forgone vs points handed over are different mistakes.
            tags.append(
                f"{e['equity_lost']:.1f} equity lost (offense {e['offense_delta']:+.1f}, "
                f"defense {e['defense_delta']:+.1f})"
            )
        tag_str = f" [{', '.join(tags)}]" if tags else ""
        return (
            f"    {round(e['win_prob_loss'] * 100, 1)}% win prob lost — {short_label}"
            f"{e['round']} vs {e['game']['opponent']}, {e['stage']} stage, turn "
            f"{e['turn_number']}: rack {e['rack']}, played {e['played']} "
            f"({e['played_score']}) instead of {e['optimal']} ({e['optimal_score']})"
            f"{tag_str}"
        )

    # rows is ranked by win% lost, so the spread-only errors all sit in a block at
    # the end and would never survive a plain head-of-list cut. They get their own
    # ranked slice so the summary can see them at all.
    by_win_prob = [e for e in rows if not e["spread_only"]]
    spread_only = sorted(
        (e for e in rows if e["spread_only"]),
        key=lambda e: (-(e["equity_lost"] or 0), e["round"], e["turn_number"] or 0),
    )
    if by_win_prob:
        lines.append(f"  Worst {min(DIGEST_TOP_ERRORS, len(by_win_prob))} by win probability, worst first:")
        lines.extend(error_line(e) for e in by_win_prob[:DIGEST_TOP_ERRORS])
    if spread_only:
        lines.append(
            f"  Spread-only errors (win% flat within {FLAT_WIN_PROB * 100:g}%, at least "
            f"{SPREAD_ONLY_EQUITY} points of equity thrown away): {len(spread_only)} in "
            f"total, {round(sum(e['equity_lost'] for e in spread_only), 1)} points; worst "
            f"{min(DIGEST_TOP_ERRORS, len(spread_only))} first:"
        )
        lines.extend(error_line(e) for e in spread_only[:DIGEST_TOP_ERRORS])
    return lines


def build_digest(stats, agg, notes, title, error_log=False, short_label="Rd"):
    """Compact text the Summary LLM call reads AND the SHA-256 cache key input.

    Deterministic (stable ordering everywhere) — hash stability is the cache.
    Contents only: title, record line, progression string, the full agg dict
    (stable key order), and one line per game (round, opponent, result, score,
    spread, mistake index, note). Never raw game/turn data.

    `error_log` appends the ranked error block (see _error_digest_lines). It is off
    by default, and deliberately keyed to the same flag that shows the "All Errors"
    section: a summary should only discuss errors in a report the reader can check
    them in, and leaving it off elsewhere keeps every other collection's digest hash
    — and so its cached summary — untouched.
    """
    lines = [f"Title: {title}", f"Record: {agg['record']}", f"Progression: {_progression(stats)}"]
    if agg.get("void_challenge"):
        # Stated in the digest itself so the summary writer can't credit Jesse for
        # something the rules made impossible, or read a low mistakes score as
        # more impressive than it is. Emitted only for VOID collections, so every
        # other collection's digest hash (and cached summary) is unaffected.
        lines.append(
            "Challenge rule: VOID — invalid plays are rejected by the client, so NO phony can "
            "be played by either side. Do not praise or even mention phony-free play, phony "
            "counts, or word-validity discipline; zero phonies here is a rule, not an "
            "achievement. Mistakes scores also run structurally lower than in challenge-rule "
            "play (playing a phony is not an available error), so do not compare them with "
            "over-the-board tournament figures or call them exceptional on that basis."
        )
    lines.append("Aggregate:")
    # `void_challenge` is reported in prose above, never as a key: including it
    # would change every non-league collection's digest hash and needlessly
    # re-bill all their cached summaries.
    skip = {"void_challenge"}
    if agg.get("void_challenge"):
        skip |= {"total_phonies", "total_opp_phonies", "games_per_phony"}
    for k in sorted(agg.keys()):
        if k in skip:
            continue
        lines.append(f"  {k}: {agg[k]}")
    lines.append("Games:")
    for g, note in zip(stats, notes):
        mi_str = f"{g['mistake_index']:.1f}" if g["mistake_index"] is not None else "-"
        lines.append(
            f"  Rd{g['round']} vs {g['opponent']}: {g['result']} {g['jesse_score']}-{g['opp_score']} "
            f"({sp_str(g['jesse_score']-g['opp_score'])}) MI={mi_str} | {note}"
        )
    # Missed bingos, spelled out by who missed them. The per-game note already
    # carries both sides, but its bare "missed bingo #3 (WORD)" clause for the
    # subject was read as the OPPONENT's miss and published that way (Season 18,
    # BIATHLETE), so state the attribution rather than leaving it to be inferred.
    own = [(g["round"], w) for g in stats for w in g.get("missed_bingo_words", [])]
    opp = [
        (g["round"], g["opponent"], w)
        for g in stats
        for w in g.get("opp_missed_bingo_words", [])
    ]
    if own or opp:
        lines.append(
            "Missed bingos — attribution is exactly as stated here; never swap these two lists:"
        )
        lines.append(
            f"  Missed by the subject of this report: "
            f"{'; '.join(f'Rd{r} {w}' for r, w in own) if own else 'none'}"
        )
        lines.append(
            f"  Missed by opponents: "
            f"{'; '.join(f'Rd{r} {o} {w}' for r, o, w in opp) if opp else 'none'}"
        )
    if error_log:
        lines.extend(_error_digest_lines(stats, short_label))
    return "\n".join(lines)


def digest_hash(digest):
    return hashlib.sha256(digest.encode("utf-8")).hexdigest()
