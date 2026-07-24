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


def opponent_cell(name, username):
    """Opponent as displayed in the report: real name plus their Woogles username
    linked to their profile. Collapses to just the link when the display name adds
    nothing (no real name on file, so `name` is already the username).

    `username` comes from the game's own account data when the game was played on
    Woogles; for an annotator upload there is no account, so fall back to the
    private name registry above (off unless configured).
    """
    username = username or load_name_registry().get(_norm_label(name))
    if not username:
        return name
    link = f"[{username}]({PROFILE_URL.format(username)})"
    return link if _label_matches_username(name, username) else f"{name} ({link})"


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


def build_snapshots_and_racks(events):
    """Board state (15x15) and rack BEFORE each analysis turn.

    Returns (snapshots, racks). Index i corresponds to analysis turns[i].
    History always has one extra move (the final PASS) with no matching analysis turn —
    this means snapshots[turn_idx] is always correctly aligned.
    PHONY_TILES_RETURNED: tiles NOT placed on board (play was challenged off).
    CHALLENGE_BONUS: not a move, skipped.
    """
    board = [["" for _ in range(15)] for _ in range(15)]
    snapshots, racks = [], []
    i = 0
    while i < len(events):
        ev = events[i]
        et = ev.get("type", "")
        if et == "TILE_PLACEMENT_MOVE":
            snapshots.append([row[:] for row in board])
            racks.append(ev.get("rack") or "")
            if i + 1 < len(events) and events[i + 1].get("type") == "PHONY_TILES_RETURNED":
                i += 2
                continue  # phony: snapshot taken, board not updated
            dr = 1 if ev["direction"] == "VERTICAL" else 0
            dc = 0 if ev["direction"] == "VERTICAL" else 1
            r2, c2 = ev["row"], ev["column"]
            for ch in (ev.get("played_tiles") or ""):
                if ch != ".":
                    board[r2][c2] = ch.upper()
                r2 += dr
                c2 += dc
        elif et in ("EXCHANGE", "PASS"):
            snapshots.append([row[:] for row in board])
            racks.append(ev.get("rack") or "")
        i += 1
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
    i = 0
    while i < len(events):
        ev = events[i]
        et = ev.get("type", "")
        if et == "TILE_PLACEMENT_MOVE":
            challenged = i + 1 < len(events) and events[i + 1].get("type") == "PHONY_TILES_RETURNED"
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
                i += 2
                continue
            dr = 1 if ev["direction"] == "VERTICAL" else 0
            dc = 0 if ev["direction"] == "VERTICAL" else 1
            r2, c2 = ev["row"], ev["column"]
            for ch in (ev.get("played_tiles") or ""):
                if ch != ".":
                    board[r2][c2] = ch.upper()
                r2 += dr
                c2 += dc
        elif et in ("EXCHANGE", "PASS"):
            moves.append(
                {"word": None, "words_formed": [], "challenged": False, "player_index": ev.get("player_index")}
            )
        i += 1
    return moves


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

    snapshots, racks = build_snapshots_and_racks(events)
    played_words = build_played_words(events)

    endgame_spread_lost = win_prob_lost = phonies_played = missed_bingos = 0
    opp_win_prob_lost = 0
    missed_bingo_words = []
    opp_missed_bingo_words = []
    jesse_phonies = []  # [{'words_formed', 'challenged'}]
    opp_phonies = []  # [{'words_formed', 'challenged'}]
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
                if t["player_index"] == jesse_idx:
                    missed_bingos += 1
                    missed_bingo_words.append(word)
                else:
                    opp_missed_bingo_words.append(word)

        if t["player_index"] != jesse_idx:
            if opp_fully_annotated:
                opp_win_prob_lost += t.get("win_prob_loss") or 0
            continue
        if t.get("tiles_in_bag") == 0:
            endgame_spread_lost += t.get("spread_loss") or 0
        win_prob_lost += t.get("win_prob_loss") or 0
        if t.get("is_phony"):
            phonies_played += 1

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
        "jesse_score": jesse_score,
        "opp_score": opp_score,
        "result": "W" if jesse_score > opp_score else "L",
        "mistake_index": mistake_index,
        "opp_mistake_index": opp_mistake_index,
        "opp_fully_annotated": opp_fully_annotated,
        "jesse_bingos": jesse_bingos,
        "opp_bingos": opp_bingos,
        "jesse_blanks": jesse_blanks,
        "jesse_blanks_played": jesse_blanks_played,
        "jesse_high_turn": jesse_high_turn,
        "endgame_spread_lost": endgame_spread_lost,
        "win_prob_lost": win_prob_lost,  # multiply by 100 for %
        "opp_win_prob_lost": opp_win_prob_lost,  # only valid when opp_fully_annotated; multiply by 100 for %
        "phonies_played": phonies_played,
        "opp_phonies_played": len(opp_phonies),
        "missed_bingos": missed_bingos,
        "missed_bingo_words": missed_bingo_words,
        "opp_missed_bingo_words": opp_missed_bingo_words,
        "jesse_phonies": jesse_phonies,
        "opp_phonies": opp_phonies,
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


def aggregate(stats):
    n = len(stats)
    wins = sum(1 for g in stats if g["result"] == "W")
    mi_games = [g for g in stats if g["mistake_index"] is not None]
    opp_ann_games = [g for g in stats if g["opp_fully_annotated"]]  # opponent racks known all game — see Notes

    total_jb = sum(g["jesse_bingos"] for g in stats)
    total_mb = sum(g["missed_bingos"] for g in stats)
    total_ob = sum(g["opp_bingos"] for g in stats)
    total_bl = sum(g["jesse_blanks"] for g in stats)
    total_eg = sum(g["endgame_spread_lost"] for g in stats)
    total_ph = sum(g["phonies_played"] for g in stats)
    total_opp_ph = sum(g["opp_phonies_played"] for g in stats)
    total_sp = sum(g["jesse_score"] - g["opp_score"] for g in stats)

    return {
        "record": f"{wins}-{n-wins} {sp_str(total_sp)}",
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
        "total_phonies": total_ph,
        "total_opp_phonies": total_opp_ph,
        "games_per_mb": round(n / total_mb, 1) if total_mb else None,
        "games_per_phony": round(n / total_ph, 1) if total_ph else None,
        "n_opp_annotated": len(opp_ann_games),
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
        parts.append("narrow loss" if res == "L" else "narrow win")
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
    boxes = ["\U0001f7e9" if g["result"] == "W" else "\U0001f7e5" for g in stats]
    return " ".join("".join(boxes[i : i + 5]) for i in range(0, len(boxes), 5))


def render_report(stats, agg, notes, title, summary_md=None, subject_display="Jesse Day",
                  round_label=None, extra_sections=None):
    """Render the report markdown.

    `round_label` renames the ordering column when games aren't sequenced by
    round — a league is a round robin whose games have no meaningful order, so it
    passes "Seed" and the column reports opponent strength instead. Setting it
    also suppresses the missing-rounds note, which is meaningless for an ordering
    that is contiguous by construction.

    `extra_sections` is a list of ready-made markdown blocks appended after the
    per-game tables and before the Summary (the league cross-check uses it).
    """
    n = agg["n"]
    col_label = round_label or "Rnd"
    short_label = round_label or "Rd"
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
    lines.append(f"| Total Phonies Played | {agg['total_phonies']} |")
    lines.append(f"| Opponent Phonies Played | {agg['total_opp_phonies']} |")
    if agg["games_per_mb"] is not None:
        lines.append(f"| Games per Missed Bingo | {agg['games_per_mb']} |")
    if agg["games_per_phony"] is not None:
        lines.append(f"| Games per Phony Played | {agg['games_per_phony']} |")

    if agg["n_opp_annotated"] > 0:
        opp_qualifier = "" if agg["n_opp_annotated"] == n else f" (over {agg['n_opp_annotated']} fully-annotated games)"
        lines.append(f"| Average Opponent Mistakes Score | {agg['avg_opp_mi']:.2f}{opp_qualifier} |")
        lines.append(f"| Average Opponent Win% Lost | {agg['avg_opp_wpl']:.1f}%{opp_qualifier} |")

    lines.append("")
    lines.append("## Per-Game Breakdown")
    lines.append("")
    lines.append(
        "*Notes: `*` marks a phony (all words formed by the play; the specific invalid word "
        "isn't always identifiable when multiple words were formed — CSW is the configured "
        "lexicon for every game). Opp Mistakes/Opp Win% Lost show \"—\" for games "
        "where the opponent's rack wasn't fully known (not livestreamed or double-annotated).*"
    )
    lines.append("")

    show_opp_cols = agg["n_opp_annotated"] > 0
    header = (
        f"| {col_label} | Game | Opponent | Result | Jesse | Opp | Spread | Mistakes | Jesse Bingos | "
        "Missed Bingos | Opp Bingos | Jesse Blanks | Endgame Spread Lost | Win% Lost"
    )
    sep = "|---|---|---|---|---|---|---|---|---|---|---|---|---|---"
    if show_opp_cols:
        header += " | Opp Mistakes | Opp Win% Lost"
        sep += "|---|---"
    header += " | Notes |"
    sep += "|---|"
    lines.append(header)
    lines.append(sep)

    for g, note in zip(stats, notes):
        mi_str = f"{g['mistake_index']:.1f}" if g["mistake_index"] is not None else "—"
        row = (
            f"| {g['round']} | [↗]({g['game_url']}) | "
            f"{opponent_cell(g['opponent'], g.get('opp_username'))} | {g['result']} | "
            f"{g['jesse_score']} | {g['opp_score']} | {sp_str(g['jesse_score']-g['opp_score'])} | "
            f"{mi_str} | {g['jesse_bingos']} | {g['missed_bingos']} | {g['opp_bingos']} | "
            f"{g['jesse_blanks']} | {g['endgame_spread_lost']} | {round(g['win_prob_lost']*100,1)}%"
        )
        if show_opp_cols:
            opp_mi_str = f"{g['opp_mistake_index']:.1f}" if g["opp_fully_annotated"] else "—"
            opp_wpl_str = f"{round(g['opp_win_prob_lost']*100,1)}%" if g["opp_fully_annotated"] else "—"
            row += f" | {opp_mi_str} | {opp_wpl_str}"
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
        opp_note = f" ({agg['n_opp_annotated']} games)"
        avg_row += f" | **{agg['avg_opp_mi']:.2f}**\\*{opp_note} | **{agg['avg_opp_wpl']:.1f}%**\\*{opp_note}"
    avg_row += " | |"
    lines.append(avg_row)

    if show_opp_cols:
        lines.append("")
        lines.append(
            "\\*Opp Mistakes/Opp Win% Lost averages are computed only over the fully-annotated "
            "games (denominator = n_opp_annotated, not the full game count)."
        )

    lines.append("")
    lines.append("## Missed Bingos")
    lines.append("")
    lines.append("*Lowercase = blank tile; (X) = board tile X was already there*")
    lines.append("")
    lines.append(f"| {short_label} | Opponent | Word |")
    lines.append("|---|---|---|")
    for g in stats:
        for w in g.get("missed_bingo_words", []):
            lines.append(f"| {g['round']} | {g['opponent']} | {w} |")

    for section in extra_sections or []:
        lines.append("")
        lines.append(section.strip())

    if summary_md:
        lines.append("")
        lines.append("## Summary")
        lines.append(summary_md.strip())

    return "\n".join(lines) + "\n"


def build_digest(stats, agg, notes, title):
    """Compact text the Summary LLM call reads AND the SHA-256 cache key input.

    Deterministic (stable ordering everywhere) — hash stability is the cache.
    Contents only: title, record line, progression string, the full agg dict
    (stable key order), and one line per game (round, opponent, result, score,
    spread, mistake index, note). Never raw game/turn data.
    """
    lines = [f"Title: {title}", f"Record: {agg['record']}", f"Progression: {_progression(stats)}"]
    lines.append("Aggregate:")
    for k in sorted(agg.keys()):
        lines.append(f"  {k}: {agg[k]}")
    lines.append("Games:")
    for g, note in zip(stats, notes):
        mi_str = f"{g['mistake_index']:.1f}" if g["mistake_index"] is not None else "-"
        lines.append(
            f"  Rd{g['round']} vs {g['opponent']}: {g['result']} {g['jesse_score']}-{g['opp_score']} "
            f"({sp_str(g['jesse_score']-g['opp_score'])}) MI={mi_str} | {note}"
        )
    return "\n".join(lines)


def digest_hash(digest):
    return hashlib.sha256(digest.encode("utf-8")).hexdigest()
