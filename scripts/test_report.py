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
import skill_graph as sg
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

        # The stage split is the whole basis of the skill graph's stacked bars,
        # and it is only meaningful if the parts add back to the headline number.
        # BestBot's index is a plain sum of per-turn weights (tr.MISTAKE_POINTS),
        # so this is an equality, not an approximation — a drifting weight table
        # or a turn dropped by game_stage shows up here and nowhere else.
        sb = g["stage_breakdown"]
        split_mi = sum(b["mistake_index"] for b in sb.values())
        if g["mistake_index"] is not None:
            check(abs(split_mi - g["mistake_index"]) < 1e-6,
                  f"{who}: stage_breakdown sums to the reported mistake_index",
                  f"{split_mi} != {g['mistake_index']}")
        split_wp = sum(b["win_prob_lost"] for b in sb.values())
        check(abs(split_wp - g["win_prob_lost"]) < 1e-9,
              f"{who}: stage_breakdown sums to the game's win% lost",
              f"{split_wp} != {g['win_prob_lost']}")
        turns_at_idx = sum(1 for t in result["turns"] if t.get("player_index") == jesse_idx)
        check(sum(b["turns"] for b in sb.values()) == turns_at_idx,
              f"{who}: every one of the subject's turns lands in exactly one stage")

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
    md = tr.render_report(stats, agg, notes, title)
    tag = slug

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
    check(("## All Errors" in md) == bool(tr.error_log_rows(stats)),
          f"{tag}: every report with analyzed errors shows the error log")
    if "## All Errors" in md:
        # The columns the error table promises, in order. Layout may move, but
        # a silently dropped column would make the numbers beside it lie. The
        # three cost columns sit just left of Flags, since a wall of numbers up
        # front made the table hard to scan.
        header = next(h for h in md.splitlines() if h.startswith("| Win% Lost |"))
        cells = split_row(header)
        check(cells[0] == "Win% Lost" and cells[-4:] == ["Equity Lost", "Off Δ", "Def Δ", "Flags"],
              f"{tag}: error table sorts by Win% Lost and ends with cost columns then Flags", header)

    digest = tr.build_digest(stats, agg, notes, title)
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

    # WESPA enrichment. Stubbed rather than fetched: the suite must not touch the
    # network, and the real ratings drift with every rating run. Structure is
    # pinned; no number here is.
    real_lookup = tr.wespa_ratings.lookup
    stub = {"playerid": 1958, "name": "Rated Player", "country": "USA",
            "rating": 1793, "title": None}
    try:
        tr.wespa_ratings.lookup = lambda n: stub if n == "Rated Player" else None
        cell = tr.opponent_cell("Rated Player", "somehandle")
        check(f"({stub['rating']})" in cell,
              "wespa: a matched opponent shows their rating", cell)
        check(f"player.html?id={stub['playerid']}" in cell,
              "wespa: the real name links to the WESPA profile", cell)
        check("woogles.io/profile/somehandle" in cell,
              "wespa: the Woogles profile link survives enrichment", cell)

        solo = tr.opponent_cell("Rated Player", None)
        check(f"({stub['rating']})" in solo,
              "wespa: enrichment does not require a Woogles handle", solo)

        stub["title"] = "GM"
        check(f"({stub['rating']} GM)" in tr.opponent_cell("Rated Player", None),
              "wespa: a titled player shows their title beside the rating")

        # The alias catalog is committed and hand-confirmed, so a typo'd id
        # would silently drop a rating. Cheap to catch here.
        alias_file = os.path.join(os.path.dirname(__file__), os.pardir,
                                  ".github", "wespa-aliases.json")
        if os.path.exists(alias_file):
            with open(alias_file) as f:
                aliases = json.load(f)["aliases"]
            by_id = tr.wespa_ratings._loaded()["by_id"]
            if by_id:  # skip when no ratings cache is present (e.g. a fresh clone)
                dangling = [k for k, v in aliases.items() if v is not None and v not in by_id]
                check(not dangling,
                      "wespa: every alias points at a real WESPA player", dangling)

        # The two failure modes that must be invisible: no match, and WESPA down.
        plain = tr.opponent_cell("Nobody At All", "somehandle")
        check(plain == "Nobody At All - [somehandle](https://woogles.io/profile/somehandle)",
              "wespa: an unmatched opponent renders exactly as before", plain)

        def boom(_):
            raise RuntimeError("WESPA unreachable")
        tr.wespa_ratings.lookup = boom
        check(tr.opponent_cell("Rated Player", "somehandle")
              == "Rated Player - [somehandle](https://woogles.io/profile/somehandle)",
              "wespa: an unreachable WESPA never breaks a cell")
    finally:
        tr.wespa_ratings.lookup = real_lookup

    # Nigel Richards sees the bingos and declines them; his are "passed up", never
    # "missed". Applies to opponent-attributed note text only.
    nigel = dict(stat, opponent="Nigel Richards", opp_missed_bingo_words=["BIATHLETE"],
                 opp_fully_annotated=True)
    note, _ = tr._game_note(nigel, 0)
    check("passed up" in note and "Nigel Richards missed" not in note,
          "synthetic: Nigel Richards passes bingos up rather than missing them", note)


# --------------------------------------------------------------------------
# Layer 4 — the cross-event skill graph
# --------------------------------------------------------------------------

def test_skill_graph(fixtures):
    """The chart's arithmetic and its dating, without rendering a pixel."""
    # Dating. Titles are the only evidence a Woogles collection carries, so the
    # parser has to survive every shape in the archive — and refuse the ones that
    # aren't events at all.
    cases = [
        ("WESPAC 2019", (2019, 7, 15)),
        ("Austin One-Day Aug '23", (2023, 8, 15)),
        ("2019 WESPAC Final", (2019, 7, 15)),
        ("Jesse Day's (abbreviated) Causeway 2026", (2026, 7, 15)),
    ]
    for title, expected in cases:
        parsed = sg.parse_event_date(title)
        check(parsed is not None and parsed[0] == expected,
              f"skill graph: dates '{title}'", str(parsed))
    # A practice-game collection has no year and must never become a bar: it is
    # not a discrete event, and one would swamp the axis.
    check(sg.parse_event_date("James Curley practice games") is None,
          "skill graph: an undated collection is not an event")

    # Averaging. The bar height must be the collection's own average mistakes
    # score — the figure the tournament report already prints — or the chart and
    # the report below it disagree in the same email.
    for slug, col in fixtures:
        events = sg.build_events([dict(col, uuid=slug)], "mistake-index",
                                 overrides={slug: {"date": "2020-01"}})
        if not events:
            continue
        e = events[0]
        stats = [tr.compute_game(r) for r in col["games"]]
        agg = tr.aggregate(stats)
        # Half a last place of agg's own 2dp rounding, plus float dust: the two
        # means are summed in a different order, so an exact compare would fail
        # on arithmetic noise rather than on a real disagreement.
        check(abs(e["total"] - agg["avg_mi"]) <= 0.0051,
              f"{slug}: skill graph bar height equals the report's average mistakes score",
              f"{e['total']} != {agg['avg_mi']}")
        check(abs(sum(e["values"].values()) - e["total"]) < 1e-9,
              f"{slug}: the stacked segments add up to the bar")
        check(e["games"] == len([g for g in stats if g["mistake_index"] is not None]),
              f"{slug}: unanalyzed games are out of the divisor as well as the sum")

        # The table is the non-visual reading of the chart — it has to be
        # well-formed markdown like every other table in the report.
        rows = sg.table_rows(events, "mistake-index").splitlines()
        widths = {len(split_row(r)) for r in rows}
        check(len(widths) == 1, f"{slug}: skill-graph table rows all match the header",
              str(widths))
        check(not any("None" in r or "nan" in r for r in rows),
              f"{slug}: no None/nan reaches a skill-graph cell")

    # A real date from the overrides file must reach the sort key at day
    # precision — a main event and its own final sit one day apart, and dropping
    # the day is what put them in alphabetical order on the axis.
    col = {"uuid": "u", "title": "Main"}
    fin = {"uuid": "v", "title": "Final"}
    ov = {"u": {"date": "2019-10-19"}, "v": {"date": "2019-10-20"}}
    check(sg.event_date(col, ov)[0] < sg.event_date(fin, ov)[0],
          "skill graph: same-month events order by day, not alphabetically")

    # Every metric must be additive across stages, since the chart stacks them.
    for name in sg.METRICS:
        events = sg.build_events([dict(fixtures[0][1], uuid="x")], name,
                                 overrides={"x": {"date": "2020-01"}})
        check(events and abs(sum(events[0]["values"].values()) - events[0]["total"]) < 1e-9,
              f"skill graph: '{name}' stacks to its own total")


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
    print("\nskill graph", file=sys.stderr)
    test_skill_graph(fixtures)

    print(f"\n{_PASSES[0]} checks passed, {len(_FAILURES)} failed.", file=sys.stderr)
    for failure in _FAILURES:
        print(f"  FAIL {failure}", file=sys.stderr)
    return 1 if _FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
