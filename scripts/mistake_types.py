#!/usr/bin/env python3
"""Classify each of a player's BestBot mistakes by its *type*, not its stage.

The stage split (tournament_report.stage_breakdown) says *when* a mistake index
was lost; this says *what kind* of mistake lost it. Every mistake turn lands in
exactly one type, weighted by the same MISTAKE_POINTS as the headline index, so
the types sum back to the reported mistake index exactly and stack in the skill
graph the way the stages do.

Types are tested in TYPES order and the first that fits wins:

  phony            a phony of ours that was challenged off (BestBot's flag)
  word knowledge   we failed to challenge an opponent phony, or the best play
                   formed a word that is rare in everyday English, low in
                   Zyzzyva playability, and not known from Jesse's quizzing
  missed bingo     a bingo was available and not played (BestBot's flag) -
                   obscure or not; an obscure missed bingo is still a bingo
  endgame          a turn BestBot judged with its endgame / pre-endgame solver
                   rather than a sim; these carry no sim numbers to split further
  strategy         win% lost well beyond what the equity loss accounts for -
                   the right play for the score situation, not the board
  offense          the rest, where the best play was ahead on our own scoring
                   (this turn plus our next two turns in the sim)
  defense          the rest, where the best play was ahead on what the
                   opponent scores over their next three turns

    python3 scripts/mistake_types.py --snapshot data/woogles-snapshot.json

prints each type's share of the subject's total mistake index, with examples.
"""
import argparse
import collections
import json
import os
import re
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import otb_solver  # noqa: E402
from wordfreq import zipf_frequency  # noqa: E402

TYPES = ("phony", "word knowledge", "missed bingo", "endgame", "strategy",
         "offense", "defense")

# Three signals, and a word must fail all three to count as obscure:
#
# - Everyday English. A word Jesse would know off the street (HEAL, EGRET,
#   CELLO) is rarely a top play and never quizzed, so the Zyzzyva signals alone
#   call it obscure. `wordfreq`'s Zipf scale settles it: below 2.0 is roughly
#   "fewer than one use per ten million words", where the Scrabble-only words
#   live (KARANA 1.57, TAENIA 1.72, MAPPIST 0) and everyday ones don't.
# - Playability. Each lexicon database ranks words within their length by how
#   often they turn up as a top play. Use is heavily skewed - a small share of
#   words come up constantly - so anything in the less-playable half of its
#   length is genuinely obscure. It is a measure of usefulness, not familiarity:
#   vowel dumps like EUOI rank near the top, which is right for this purpose.
# - Jesse's own anagram quizzing (the cardbox databases, all CSW editions). An
#   alphagram he has answered well is a word he knows however unplayable it is.
#   Caveat: this is what he knows *now*, not at the time of a 2016 event.
#
# Twos and threes are exempt: at Jesse's level every one is known, so missing
# one is a vision error, not a knowledge gap.
# Both Zyzzyva signals come from one derived file, built from Jesse's install by
# scripts/zyzzyva_export.py (locally) or checked out of his private
# zyzzyva-data repo (in CI). Never the raw Collins databases themselves.
KNOWLEDGE_PATH = os.environ.get("ZYZZYVA_KNOWLEDGE", "data/zyzzyva-knowledge.sqlite")
OBSCURE_PERCENTILE = 0.5
OBSCURE_MIN_LENGTH = 4
EVERYDAY_ZIPF = 2.0
KNOWN_MIN_CORRECT = 1
KNOWN_MIN_ACCURACY = 0.75

# Across Jesse's archive the median sim trades 0.41 win% per point of equity. A
# play that loses STRATEGY_EXCESS more win% than its equity deficit explains is
# a score-situation error (fishing when behind, opening the board when ahead),
# which is what the strategy type is for - but only when that unexplained part
# is also most of the loss, or every big equity error with a little situational
# noise on top would land here too.
WIN_PER_EQUITY_POINT = 0.0041
STRATEGY_EXCESS = 0.02
STRATEGY_SHARE = 0.5


# --------------------------------------------------------------------------
# Word playability
# --------------------------------------------------------------------------

class Playability:
    """Word familiarity, from the Zyzzyva export (scripts/zyzzyva_export.py).

    `percentile` is 0.0 for the most playable word of its length, 1.0 for the
    least. With no export on disk (a fresh clone, or CI without the private
    repo) every lookup answers None - callers treat that as "can't tell", never
    as "obscure", so the word-knowledge type simply goes unfilled.
    """

    def __init__(self, path=KNOWLEDGE_PATH):
        self.path = path
        self._conn = (sqlite3.connect(f"file:{path}?mode=ro", uri=True)
                      if os.path.exists(path) else None)

    def available(self, lexicon):
        return self._conn is not None and self._conn.execute(
            "SELECT 1 FROM playability WHERE lexicon = ? LIMIT 1", (lexicon,)).fetchone()

    def percentile(self, word, lexicon):
        if self._conn is None:
            return None
        row = self._conn.execute(
            "SELECT percentile FROM playability WHERE lexicon = ? AND word = ?",
            (lexicon, word.upper())).fetchone()
        return row[0] if row else None  # absent: a phony, not our call here

    def known(self, word):
        # A plural is as familiar as its singular, but corpora count them apart:
        # AWLS scores 1.57 while AWL scores 2.41.
        w = word.lower()
        bases = [w] + [w[:-len(suf)] for suf in ("s", "es") if w.endswith(suf)]
        if max(zipf_frequency(b, "en") for b in bases) >= EVERYDAY_ZIPF:
            return True
        if self._conn is None:
            return False
        row = self._conn.execute("SELECT correct, incorrect FROM quiz WHERE alphagram = ?",
                                 ("".join(sorted(word.upper())),)).fetchone()
        c, i = row or (0, 0)
        return c >= KNOWN_MIN_CORRECT and c / (c + i) >= KNOWN_MIN_ACCURACY


# --------------------------------------------------------------------------
# Rebuilding the board before each turn
# --------------------------------------------------------------------------

_POS = re.compile(r"^(?:(\d{1,2})([A-O])|([A-O])(\d{1,2}))$")


def parse_move(desc):
    """'8D PONGALS' / 'O10 K.RANA' -> (row, col, dr, dc, tiles), or None for an
    exchange or pass. Row/col are 0-based; letter-first means vertical."""
    parts = (desc or "").split()
    if len(parts) != 2:
        return None
    m = _POS.match(parts[0])
    if not m:
        return None
    if m.group(1):
        return int(m.group(1)) - 1, ord(m.group(2)) - 65, 0, 1, parts[1]
    return int(m.group(4)) - 1, ord(m.group(3)) - 65, 1, 0, parts[1]


def _place(board, row, col, dr, dc, tiles):
    placed = []
    for i, ch in enumerate(tiles):
        if ch != ".":
            board[(row + dr * i, col + dc * i)] = ch
            placed.append((row + dr * i, col + dc * i))
    return placed


def boards_before_turns(events, turns):
    """{turn_number: board dict} for every placement turn, or {} when the event
    log can't be aligned with the analysis.

    Alignment is on the move itself, not on counts: passes, challenge bonuses and
    end-of-game events don't map one-to-one onto analysis turns, so each analyzed
    placement is matched to the next event with the same position and tiles.
    A phony that was challenged off takes its tiles back off the board.
    """
    board, last = {}, []
    boards = {}
    i = 0
    for t in turns:
        move = parse_move(t.get("played_move"))
        if not move:
            continue
        pos = t["played_move"].split()[0]
        while i < len(events):
            e = events[i]
            i += 1
            if e.get("type") == "PHONY_TILES_RETURNED":
                for sq in last:
                    board.pop(sq, None)
                last = []
            elif e.get("type") == "TILE_PLACEMENT_MOVE":
                hit = e.get("position") == pos and e.get("played_tiles") == move[4]
                if hit:
                    boards[t["turn_number"]] = dict(board)
                dr, dc = (0, 1) if e.get("direction") == "HORIZONTAL" else (1, 0)
                last = _place(board, e["row"], e["column"], dr, dc, e["played_tiles"])
                if hit:
                    break
        else:
            return {}
    # A phony returned after the last analyzed placement still leaves `boards`
    # correct: every snapshot was taken before its own move.
    return boards


def words_of(board, desc):
    """Every word a move forms on `board` (main word plus cross-words), or []."""
    move = parse_move(desc)
    if not move or board is None:
        return []
    row, col, dr, dc, tiles = move
    word = "".join(board.get((row + dr * k, col + dc * k), "?") if ch == "." else ch
                   for k, ch in enumerate(tiles))
    placed = [(row + dr * k, col + dc * k, ch) for k, ch in enumerate(tiles) if ch != "."]
    return [w for w in otb_solver.words_formed(board, word, row, col, dr, dc, placed)
            if "?" not in w]


# --------------------------------------------------------------------------
# Classification
# --------------------------------------------------------------------------

def _sim(t, played):
    for p in t.get("top_sim_plays") or []:
        if played and p.get("is_played_move"):
            return p
        if not played and p["move_description"] == t.get("optimal_move"):
            return p
    return None


def _ply_sum(p, mine):
    """Sum of the sim's mean scores on our own future plies (even) or the
    opponent's (odd). Ply 1 is the opponent's reply."""
    return sum(s["score_mean"] for s in p.get("ply_stats") or []
               if (s["ply"] % 2 == 0) == mine)


def obscure_words(t, board, lexicon, playability):
    """Words the best play forms, and ours doesn't, that are in the less
    playable half of their length and not known from quizzing. None when the
    lexicon database is missing, so the caller can't tell."""
    if board is None or not playability.available(lexicon):
        return None
    ours = set(words_of(board, t.get("played_move")))
    out = []
    for w in words_of(board, t.get("optimal_move")):
        if len(w) < OBSCURE_MIN_LENGTH or w in ours:
            continue
        pct = playability.percentile(w, lexicon)
        if pct is not None and pct >= OBSCURE_PERCENTILE and not playability.known(w):
            out.append(w)
    return out


def classify(t, board=None, lexicon=None, playability=None):
    """The type of one mistake turn (see TYPES), plus a dict of the evidence."""
    if t.get("phony_challenged"):
        return "phony", {}
    if t.get("missed_challenge"):
        return "word knowledge", {"missed_challenge": True}
    if t.get("missed_bingo"):
        return "missed bingo", {}
    opt, ours = _sim(t, played=False), _sim(t, played=True)
    if not (opt and ours):
        return "endgame", {}
    if playability is not None:
        words = obscure_words(t, board, lexicon, playability)
        if words:
            return "word knowledge", {"words": words}
    d_win = opt["win_prob"] - ours["win_prob"]
    d_eq = opt["equity"] - ours["equity"]
    excess = d_win - WIN_PER_EQUITY_POINT * max(d_eq, 0.0)
    if excess >= STRATEGY_EXCESS and excess >= STRATEGY_SHARE * d_win:
        return "strategy", {"win_lost": d_win, "equity_lost": d_eq}
    offense = (opt["score"] - ours["score"]) + _ply_sum(opt, True) - _ply_sum(ours, True)
    defense = _ply_sum(ours, False) - _ply_sum(opt, False)
    return ("offense" if offense >= defense else "defense"), {
        "offense": offense, "defense": defense}


def type_breakdown(history, turns, player_index, weights, playability=None):
    """Per-type {mistake_index, win_prob_lost, turns} for one player's turns,
    plus "none" for
    turns that weren't mistakes, so the parts always sum to the whole.

    `weights` is tournament_report.MISTAKE_POINTS, passed in rather than
    imported so the two modules don't import each other.
    """
    out = {k: {"mistake_index": 0.0, "win_prob_lost": 0.0, "turns": 0}
           for k in TYPES + ("none",)}
    boards = boards_before_turns(history.get("events") or [], turns)
    lexicon = history.get("lexicon")
    for t in turns:
        if t.get("player_index") != player_index:
            continue
        size = t.get("mistake_size") or "NO_MISTAKE"
        if size == "NO_MISTAKE":
            kind = "none"
        else:
            kind, _ = classify(t, boards.get(t["turn_number"]), lexicon, playability)
        out[kind]["mistake_index"] += weights.get(size, 0.0)
        out[kind]["win_prob_lost"] += t.get("win_prob_loss") or 0
        out[kind]["turns"] += 1
    for b in out.values():
        b["mistake_index"] = round(b["mistake_index"], 10)
    return out


# --------------------------------------------------------------------------
# CLI: the archive-wide split, for eyeballing the rules
# --------------------------------------------------------------------------

def impact_table(win_lost, games):
    """Markdown table of what each type costs in win%, per mistake and per game.

    Endgame turns are scored by BestBot in spread, not win%, so their win%
    column is structurally near zero - it does not mean the endgames were good.
    """
    import statistics
    grand = sum(sum(v) for v in win_lost.values()) or 1
    lines = ["| Type | Mistakes | Avg win% lost | Median | Win% lost per game | Share |",
             "| --- | ---: | ---: | ---: | ---: | ---: |"]
    for kind in TYPES:
        v = win_lost.get(kind) or [0.0]
        lines.append(f"| {kind} | {len(win_lost.get(kind) or [])} | {statistics.mean(v):.1f} | "
                     f"{statistics.median(v):.1f} | {sum(v) / max(games, 1):.2f} | "
                     f"{100 * sum(v) / grand:.1f}% |")
    return f"{games} games\n\n" + "\n".join(lines)


def main():
    import tournament_report as tr

    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--snapshot", default="data/woogles-snapshot.json")
    ap.add_argument("--examples", type=int, default=3)
    ap.add_argument("--impact", action="store_true",
                    help="print the average win%% cost of each type instead")
    args = ap.parse_args()

    with open(args.snapshot) as f:
        snapshot = json.load(f)
    play = Playability()
    is_subject = tr.make_is_subject(None)
    total = collections.Counter()
    count = collections.Counter()
    examples = collections.defaultdict(list)
    win_lost = collections.defaultdict(list)
    games = 0
    for col in snapshot.get("collections") or []:
        for r in col.get("games") or []:
            try:
                history = r["history"]["history"]
                turns = r["analysis"]["result"]["turns"]
                idx = next(i for i, p in enumerate(history["players"]) if is_subject(p))
            except (StopIteration, KeyError, TypeError):
                continue
            games += 1
            boards = boards_before_turns(history.get("events") or [], turns)
            for t in turns:
                if t.get("player_index") != idx or t.get("mistake_size") in (None, "NO_MISTAKE"):
                    continue
                kind, why = classify(t, boards.get(t["turn_number"]),
                                     history.get("lexicon"), play)
                total[kind] += tr.MISTAKE_POINTS[t["mistake_size"]]
                count[kind] += 1
                win_lost[kind].append((t.get("win_prob_loss") or 0) * 100)
                if len(examples[kind]) < args.examples:
                    examples[kind].append(
                        f"{col['title']} t{t['turn_number']}: {t['played_move']} "
                        f"(best {t['optimal_move']}) {why or ''}")
    if args.impact:
        print(impact_table(win_lost, games))
        return
    grand = sum(total.values()) or 1
    for kind in TYPES:
        print(f"{kind:15} {count[kind]:5} turns  MI {total[kind]:7.1f}  "
              f"{100 * total[kind] / grand:5.1f}%")
        for ex in examples[kind]:
            print(f"    {ex}")


if __name__ == "__main__":
    main()
