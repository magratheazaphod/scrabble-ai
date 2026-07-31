#!/usr/bin/env python3
"""Regression suite for scripts/tournament_report.py.

    python3 scripts/test_report.py            # everything
    python3 scripts/test_report.py -v         # name every case as it passes

Reads the committed, anonymized corpus in `tests/fixtures/` (rebuilt by
`scripts/make_test_fixtures.py`), so it runs on a fresh clone with no woogles.io
egress and no `data/`.

## Why this is not a golden-output diff

It used to be: the old harness diffed a full render against the last report the
cron had emailed. That has the wrong failure profile for a file edited most
weeks — every deliberate change to a column or a heading failed it, so
regenerating the expectation became routine, and a test you retrain by reflex
stops being evidence of anything. (It also broke silently and passed for weeks
when the state file stopped storing rendered reports.)

What actually goes wrong in this module is attribution and arithmetic, not prose.
Every bug the reference docs record is of that kind: a missed bingo credited to
the wrong player, phony-ness read off the event log instead of the analysis,
blanks drawn confused with blanks played, a summary matched by name instead of
`player_index`. Those render beautifully and are wrong, unattended, in Jesse's
inbox. So:

- **Semantic invariants** (`test_invariants`) assert the meanings in
  `reference/report-semantics.md` against `compute_game`/`aggregate` output. They
  say nothing about layout, so adding a column or rewording a heading does not
  touch them.
- **Structural render checks** (`test_render_structure`) assert the report is
  well-formed markdown — every row matching its header's column count, no
  `None`/`nan` reaching a cell, every link resolvable — without pinning a single
  word of text. This is what catches a miscounted column the moment one is added.
- **Synthetic cases** (`test_synthetic`) cover branches absent from the corpus
  (draws, VOID challenge, a name-registry hit) by mutating fixture *input* and
  re-deriving, never by hand-building an expected dict.

Numbers are deliberately NOT pinned. If you want a specific figure locked down,
add an invariant that derives it independently — a pinned constant only records
what the code did on the day it was written.
"""
import glob
import gzip
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(__file__))
import tournament_report as tr

FIXTURE_GLOB = os.path.join("tests", "fixtures", "*.json.gz")

_FAILURES = []
_PASSES = [0]
VERBOSE = False


def check(condition, label, detail=""):
    """One assertion. Records rather than raises, so a run reports every failure."""
    if condition:
        _PASSES[0] += 1
        if VERBOSE:
            print(f"  ok   {label}", file=sys.stderr)
        return True
    _FAILURES.append(f"{label}{f' — {detail}' if detail else ''}")
    print(f"  FAIL {label}" + (f"\n       {detail}" if detail else ""), file=sys.stderr)
    return False


def load_fixtures():
    for path in sorted(glob.glob(FIXTURE_GLOB)):
        with gzip.open(path) as f:
            yield os.path.basename(path).replace(".json.gz", ""), json.load(f)


def analyzed(entry):
    return (entry.get("analysis") or {}).get("result") or {}


# --------------------------------------------------------------------------
# Layer 1 — semantic invariants
# --------------------------------------------------------------------------

def test_invariants(slug, col):
    stats = [tr.compute_game(r) for r in col["games"]]
    stats.sort(key=lambda g: g["round"])

    for entry, g in zip(sorted(col["games"], key=lambda r: tr.compute_game(r)["round"]), stats):
        history = entry["history"]["history"]
        result = analyzed(entry)
        who = f"{slug} {g['title']}"
        jesse_idx = next(i for i, p in enumerate(history["players"]) if tr.is_jesse(p))
        opp_idx = 1 - jesse_idx

        # Scores and result. Player order swaps game to game in a real collection,
        # so an index mix-up shows up here and nowhere else.
        check(g["jesse_score"] == history["final_scores"][jesse_idx],
              f"{who}: subject score reads from jesse_idx",
              f"{g['jesse_score']} != {history['final_scores'][jesse_idx]}")
        check(g["opp_score"] == history["final_scores"][opp_idx],
              f"{who}: opponent score reads from opp_idx")
        expected = "W" if g["jesse_score"] > g["opp_score"] else (
            "L" if g["jesse_score"] < g["opp_score"] else "D")
        check(g["result"] == expected, f"{who}: result agrees with the scores")

        # The mistake index must come from the summary at Jesse's player_index —
        # matching on player_name once dropped it in 18 King's Cup games.
        summary = tr.summary_for_index(result, jesse_idx)
        check(g["mistake_index"] == (summary["mistake_index"] if summary else None),
              f"{who}: mistake_index is selected by player_index")

        # Bingos, recounted independently off the event log.
        events = history.get("events") or []
        recount = sum(1 for e in events
                      if e.get("is_bingo") and e["player_index"] == jesse_idx)
        check(g["jesse_bingos"] == recount, f"{who}: bingo count matches the event log",
              f"stat {g['jesse_bingos']} vs events {recount}")

        # Drawn vs played is a documented trap: drawn counts blanks exchanged away
        # or stranded on the last rack, so it can only ever be the larger number.
        check(g["jesse_blanks"] >= g["jesse_blanks_played"],
              f"{who}: blanks drawn >= blanks played")

        # Win% lost is the sum over the subject's turns only.
        expected_wpl = sum(t.get("win_prob_loss") or 0 for t in result.get("turns") or []
                           if t["player_index"] == jesse_idx)
        check(abs(g["win_prob_lost"] - expected_wpl) < 1e-9,
              f"{who}: win_prob_lost sums the subject's turns only")

        # Missed bingos must never change hands — a summary once published one of
        # Jesse's own misses as the opponent's.
        check(len(g["missed_bingo_words"]) == len(g["missed_bingo_urls"]),
              f"{who}: missed-bingo words and links stay parallel")
        check(not (set(g["missed_bingo_urls"]) & set(g["opp_missed_bingo_urls"])),
              f"{who}: no turn is credited as a missed bingo to both players")
        check(g["missed_bingos"] == len(g["missed_bingo_words"]),
              f"{who}: missed bingo count matches the listed words")

        # Opponent-derived figures are only meaningful with full racks.
        if not g["opp_fully_annotated"]:
            partial = [t for t in result.get("turns") or []
                       if t["player_index"] == opp_idx
                       and len(t.get("rack") or "") != 7 and (t.get("tiles_in_bag") or 0) != 0]
            check(bool(partial) or history.get("play_state") != "GAME_OVER"
                  or tr.summary_for_index(result, opp_idx) is None,
                  f"{who}: opp_fully_annotated=False is justified by a short rack")

        # Error rows.
        turns_by_number = {t.get("turn_number"): t for t in result.get("turns") or []}
        for e in g["errors"]:
            turn = turns_by_number.get(e["turn_number"])
            label = f"{who} turn {e['turn_number']}"
            if not check(turn is not None, f"{label}: error row maps to a real turn"):
                continue
            check(turn["player_index"] == jesse_idx, f"{label}: error row is the subject's turn")
            check(not turn.get("was_optimal"), f"{label}: an optimal turn is never an error")
            check(e["win_prob_loss"] > 0 or e["spread_only"],
                  f"{label}: a zero-win%-loss row is flagged spread-only")
            if e["spread_only"]:
                check(e["win_prob_loss"] <= tr.FLAT_WIN_PROB
                      and (e["equity_lost"] or 0) >= tr.SPREAD_ONLY_EQUITY,
                      f"{label}: spread-only row satisfies its own definition",
                      f"wpl={e['win_prob_loss']} equity={e['equity_lost']}")
            if e["equity_lost"] is not None:
                check(e["offense_delta"] is not None and e["defense_delta"] is not None,
                      f"{label}: equity figure comes with both deltas")
                if turn.get("tiles_in_bag") == 0:
                    # Endgames are solved, not sampled: the equity figure must be
                    # exactly the analysis' own spread_loss.
                    check(abs(e["equity_lost"] - (turn.get("spread_loss") or 0)) < 1e-9,
                          f"{label}: endgame equity equals spread_loss",
                          f"{e['equity_lost']} vs {turn.get('spread_loss')}")

    # Off Δ + Def Δ ≈ −Equity Lost. Per row the gap is real and sometimes large
    # (leave value, going-out bonus), so pinning a single row would be false
    # precision — but the gap is noise, not bias, so across a whole collection it
    # must average out near zero. This is what catches the sign or ply-parity of
    # the split being wrong, which no per-row assertion can see.
    residuals = [
        (e["offense_delta"] + e["defense_delta"]) + e["equity_lost"]
        for g in stats for e in g["errors"] if e["equity_lost"] is not None
    ]
    if len(residuals) >= 20:
        mean_residual = sum(residuals) / len(residuals)
        check(abs(mean_residual) < 2.5,
              f"{slug}: the offense/defense split reconciles with equity on average",
              f"mean residual {mean_residual:.2f} over {len(residuals)} rows")

    # A row per game must reach the aggregate, and the record must count draws
    # explicitly rather than deriving losses as n - wins.
    agg = tr.aggregate(stats)
    wins = sum(1 for g in stats if g["result"] == "W")
    losses = sum(1 for g in stats if g["result"] == "L")
    draws = sum(1 for g in stats if g["result"] == "D")
    check(agg["n"] == len(stats), f"{slug}: aggregate covers every game")
    check(agg["record"].startswith(f"{wins}-{losses}" + (f"-{draws}" if draws else " ")),
          f"{slug}: record counts W/L/D explicitly", agg["record"])
    check(agg["n_opp_annotated"] == sum(1 for g in stats if g["opp_fully_annotated"]),
          f"{slug}: opponent-annotated game count matches the per-game flags")
    return stats, agg


# --------------------------------------------------------------------------
# Layer 3 — structural render checks
# --------------------------------------------------------------------------

CELL_PLACEHOLDERS = ("None", "nan", "NaN", "inf", "-inf", "{}", "[]")
LINK = re.compile(r"\[([^\]]*)\]\(([^)]*)\)")


def markdown_tables(md):
    """Yield (header_line_number, header_cells, [(line_number, row_cells)])."""
    lines = md.splitlines()
    i = 0
    while i < len(lines):
        if not lines[i].strip().startswith("|"):
            i += 1
            continue
        header, rows = lines[i], []
        i += 1
        if i < len(lines) and re.fullmatch(r"\|[\s\-:|]+\|", lines[i].strip()):
            i += 1  # the |---|---| separator
        while i < len(lines) and lines[i].strip().startswith("|"):
            rows.append((i + 1, split_row(lines[i])))
            i += 1
        yield len(md.splitlines()[:0]) or 0, split_row(header), rows


def split_row(line):
    return [c.strip() for c in line.strip().strip("|").split("|")]


def test_render_structure(slug, stats, agg, title):
    notes = tr.game_notes(stats)
    for error_log in (False, True):
        md = tr.render_report(stats, agg, notes, title, error_log=error_log)
        tag = f"{slug} (error_log={error_log})"

        for _, header, rows in markdown_tables(md):
            for lineno, cells in rows:
                check(len(cells) == len(header),
                      f"{tag}: every row matches its header's column count",
                      f"line {lineno}: {len(cells)} cells vs {len(header)} — {cells}")
                for cell in cells:
                    check(cell not in CELL_PLACEHOLDERS,
                          f"{tag}: no placeholder value reaches a cell",
                          f"line {lineno}: {cells}")

        for text, url in LINK.findall(md):
            check(url.startswith("https://woogles.io/"),
                  f"{tag}: every link points at woogles.io", url)
            check(text.strip() != "", f"{tag}: no link has empty text", url)

        check("## Aggregate Stats" in md, f"{tag}: aggregate section is present")
        check(md.startswith(f"# {title}"), f"{tag}: report opens with its title")
        check(("## All Errors" in md) == error_log,
              f"{tag}: the error log appears iff it was asked for")
        if error_log:
            # The columns the error table promises, in order. Layout may move, but
            # a silently dropped column would make the numbers beside it lie.
            header = next(h for h in md.splitlines() if h.startswith("| Win% Lost |"))
            check(split_row(header)[:4] == ["Win% Lost", "Equity Lost", "Off Δ", "Def Δ"],
                  f"{tag}: error table leads with its four cost columns", header)

    digest = tr.build_digest(stats, agg, notes, title, error_log=True)
    check("Errors (" in digest, f"{slug}: digest carries the error block")
    # A bare `None` is tolerated on an aggregate key line — it is the deliberate
    # "this collection has no such figure" marker (`games_per_phony` when no phony
    # was played), and rewriting it would change every digest hash and re-bill
    # every cached summary to say the same thing. Anywhere else it is a leak.
    for line in digest.splitlines():
        if "None" in line:
            check(re.fullmatch(r"\s+\w+: None", line),
                  f"{slug}: None appears only as an aggregate no-data marker", line)


# --------------------------------------------------------------------------
# Synthetic cases — branches the corpus cannot reach
# --------------------------------------------------------------------------

def test_synthetic(col):
    """Mutate fixture INPUT and re-derive, so these stay honest tests of the code
    rather than hand-built expectations."""
    games = json.loads(json.dumps(col["games"]))

    # A draw. Rare but real, and `record` must gain a third component only then.
    drawn = json.loads(json.dumps(games[0]))
    history = drawn["history"]["history"]
    history["final_scores"] = [400, 400]
    g = tr.compute_game(drawn)
    check(g["result"] == "D", "synthetic: equal final scores produce a draw, not a loss")
    agg = tr.aggregate([g])
    check(agg["record"].startswith("0-0-1"),
          "synthetic: a draw is counted explicitly in the record", agg["record"])
    # The rule is that a draw is never folded into a loss — not that the note has
    # to mention it. The primary label is an elif chain, so a clean draw is
    # legitimately described as "very clean" and the 🟨 box carries the result.
    note, _ = tr._game_note(g, 0)
    check(not re.search(r"\b(win|won|loss|lost|blowout|dominant)\b", note),
          "synthetic: a drawn game is never described as a win or a loss", note)

    # VOID challenge — no phony can reach the board, so every phony stat must be
    # suppressed and the digest must warn the summary writer off the subject.
    void_games = []
    for entry in json.loads(json.dumps(games)):
        entry["history"]["history"]["challenge_rule"] = "VOID"
        void_games.append(tr.compute_game(entry))
    agg = tr.aggregate(void_games)
    check(agg["void_challenge"], "synthetic: an all-VOID collection is detected")
    check(agg["total_phonies"] is None and agg["games_per_phony"] is None,
          "synthetic: VOID suppresses every phony statistic")
    digest = tr.build_digest(void_games, agg, tr.game_notes(void_games), "VOID test")
    check("VOID" in digest and "not an achievement" in digest,
          "synthetic: the VOID digest warns against praising phony-free play")
    check("total_phonies" not in digest,
          "synthetic: VOID keeps phony counts out of the digest entirely")

    # The private name registry maps a display name to a handle. It is injected by
    # env var, so this covers the lookup without the real (uncommitted) file.
    stat = tr.compute_game(games[0])
    display = stat["opponent"]
    os.environ["WOOGLES_NAME_REGISTRY"] = json.dumps({"fixturehandle": [display]})
    tr.load_name_registry(force=True)
    try:
        check(tr.opponent_handle({"opponent": display, "opp_username": None}) == "fixturehandle",
              "synthetic: a registry alias resolves to the handle")
        check(tr.opponent_handle({"opponent": "Nobody At All", "opp_username": None})
              == "Nobody At All",
              "synthetic: an unregistered player falls back to the display name")
    finally:
        del os.environ["WOOGLES_NAME_REGISTRY"]
        tr.load_name_registry(force=True)

    # Nigel Richards sees the bingos and declines them; his are "passed up", never
    # "missed". Applies to opponent-attributed note text only.
    nigel = dict(stat, opponent="Nigel Richards", opp_missed_bingo_words=["BIATHLETE"],
                 opp_fully_annotated=True)
    note, _ = tr._game_note(nigel, 0)
    check("passed up" in note and "Nigel Richards missed" not in note,
          "synthetic: Nigel Richards passes bingos up rather than missing them", note)


def main():
    global VERBOSE
    VERBOSE = "-v" in sys.argv

    fixtures = list(load_fixtures())
    if not fixtures:
        print(f"No fixtures matching {FIXTURE_GLOB} — run scripts/make_test_fixtures.py "
              "(needs data/golden-snapshot.json).", file=sys.stderr)
        return 1

    for slug, col in fixtures:
        print(f"\n{slug} ({len(col['games'])} games)", file=sys.stderr)
        stats, agg = test_invariants(slug, col)
        test_render_structure(slug, stats, agg, col["title"])
    print("\nsynthetic", file=sys.stderr)
    test_synthetic(fixtures[0][1])

    print(f"\n{_PASSES[0]} checks passed, {len(_FAILURES)} failed.", file=sys.stderr)
    for failure in _FAILURES:
        print(f"  FAIL {failure}", file=sys.stderr)
    return 1 if _FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
