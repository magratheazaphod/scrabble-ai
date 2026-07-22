#!/usr/bin/env python3
"""Mechanical verifier for a transcribed OTB scoresheet (Jesse's format).

Runs every arithmetic/consistency check SKILL.md Step 2 requires, so none can
be skipped: cumulative chains, exchange rows, rack/word consistency, turn-count
reconciliation between columns, and the +Tiles/-Time/Total boxes. On PASS it
emits the solver spec JSON, so the move list never gets hand-built.

Usage: python3 check_transcription.py transcript.json [--spec game_spec.json]

Transcript format (JSON) — fill this from the scoresheet photos:
{
  "opponent": "James Curley",
  "date": "2026-07-12",
  "first_player": "jesse" | "opponent",
  "rows": [                       // one object per sheet row, in sheet order
    {"jesse":    {"rack": "AILNRTW", "entry": "xLW", "score": 0, "cum": 0},
     "opponent": {"entry": "BI", "score": 8, "cum": 8}},
    {"jesse":    {"rack": "AAINQRT", "entry": "QAT", "score": 24, "cum": 24},
     "opponent": {"entry": "VEGA", "score": 26, "cum": 34}},
    {"jesse":    {"kept": "AINR", "entry": "QAT", "score": 24, "cum": 24},
     "opponent": {"entry": "VEGA", "score": 26, "cum": 34}},   // same turn, leftover form
    ...
  ],
  "boxes": {                      // the totals boxes at the sheet bottom
    "jesse":    {"plus_tiles": 0, "minus_time": 0, "total": 406},
    "opponent": {"plus_tiles": 4, "minus_time": 0, "total": 438}
  },
  "leftover": {"player": "jesse", "tiles": "D"},   // unplayed end-of-game rack
  "blanks": ["S", "Y"]            // blank designations from the tracking grid
}

Jesse's rack column (far left of the sheet) comes in TWO forms, and BOTH are
complete information — record whichever is on the sheet, never convert by hand:
  "rack" — the full 7-tile rack he held ('?' = blank)
  "kept" — only the LEFTOVER tiles he kept after the play, which is what he
           writes when he is in a hurry. author_gcg.py rebuilds the full rack
           as kept + the tiles the play actually took off the rack. "" is a
           valid value (a bingo keeps nothing).
A SHORT cell is auto-read as "kept" when it shares no letters with that row's
play; a short cell that DOES overlap the play is a misread full rack (game
#91's AEINNR), not a leftover, and is flagged. One of the two is REQUIRED on
every Jesse turn — without it the GCG carries played-tiles-only racks, the game
is not fully annotated, and BestBot returns no stats for him (games #91/#92).
Use --allow-partial-racks only for a cell that is genuinely illegible.

Entry values per player per row (omit the player key or use null for no turn):
  "WORD"    played word as spelled on the board; lowercase letter = a blank
            placed BY THIS MOVE. Play-through letters are always uppercase,
            even when the underlying tile is someone's earlier blank.
            (Letters not in the rack are play-through tiles — noted, not fatal)
  "xN"      exchange of N unspecified tiles (e.g. "x2") — the checker recovers
            WHICH tiles via multiset intersection with the next recorded rack
  "xTILES"  exchange of specific tiles (e.g. "xLW")
  "pass"    scoreless standing pass. For a challenged-off phony (opponent
            0-score word NOT on the final board: tiles returned, turn
            consumed) use a pass with the phony named:
            {"entry": "pass", "phony_off": "GINNIES"} — author_gcg.py uses
            the phony's tiles as the rack and reminds you to post the
            mandatory game comment naming it.
  "+5"      five-point challenge bonus row (the rack written on this row
            belongs to the FOLLOWING move)
  null      row-alignment filler, NOT a turn — e.g. the dash Jesse writes in
            his own play column next to an opponent's "+5" row (he
            challenged, the word held; that is not a turn of his)
Optional per word entry, filled in from the board photo (Step 3): "dir" ("H"|"V"),
"row" (1-15) / "col" ("A"-"O"), "phony": true (word stood unchallenged).
"score" may be omitted for x/pass rows (defaults 0). "cum" may be null if
genuinely unreadable — that link of the chain is skipped, not guessed.

  "table_error": true   the score and cum in this cell were BOTH re-read and are
            unambiguous, and they still do not reconcile — i.e. the player's own
            arithmetic is wrong (they wrote one score and added another). Players
            do this often, so a faithful transcript of a real sheet must be able
            to say so: the break downgrades to a warning, the recorded score is
            kept (the solver adjudicates it against the board, which is the only
            arbiter), and the chain resyncs to the WRITTEN cum so every later link
            still cross-checks. Use ONLY after re-reading the cell — never to
            silence a misread. Note this class is invisible to the solver (the
            recorded score is board-true); the converse class — a wrong score
            carried consistently, so the chain never breaks — is invisible to the
            checker and only the solver catches it.

Exit 0 with "PASS" if no errors (warnings allowed); exit 1 listing violations.
"""
import sys, os, json, re
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from otb_solver import VALS, DIST

WORD_RE = re.compile(r'^[A-Za-z]+$')


def tile_value(tiles):
    return sum(0 if t == '?' else VALS[t.upper()] for t in tiles)


def entry_kind(entry):
    if entry is None:
        return 'none'
    if entry == 'pass':
        return 'pass'
    if entry.startswith('+'):
        return 'bonus'
    if entry.startswith('x'):
        return 'exchange'
    if WORD_RE.match(entry):
        return 'word'
    return 'invalid'


def check(t, allow_partial_racks=False):
    """allow_partial_racks downgrades the missing-rack ERROR to a warning — only
    for racks genuinely illegible on the photo; the game then gets no BestBot
    stats for Jesse."""
    errors, warnings, info = [], [], []
    rows = t['rows']
    players = ['jesse', 'opponent']

    # --- per-column entries in order ---
    cols = {p: [(i, row[p]) for i, row in enumerate(rows)
                if row.get(p) not in (None,) and row[p] is not None]
            for p in players}

    # --- entry syntax ---
    for p in players:
        for i, e in cols[p]:
            kind = entry_kind(e.get('entry'))
            if kind == 'invalid':
                errors.append(f"row {i+1} {p}: unrecognized entry {e.get('entry')!r}")
            if kind in ('exchange', 'pass') and e.get('score', 0) not in (0, None):
                errors.append(f"row {i+1} {p}: {kind} rows must score 0, got {e.get('score')}")

    # --- cumulative chains ---
    for p in players:
        prev = 0
        for i, e in cols[p]:
            kind = entry_kind(e.get('entry'))
            if kind in ('none', 'invalid'):
                continue
            score = e.get('score')
            if score is None:
                score = 0 if kind in ('exchange', 'pass') else None
            if kind == 'bonus' and score is None:
                score = int(e['entry'])
            cum = e.get('cum')
            if score is None and cum is None:
                errors.append(f"row {i+1} {p}: word entry needs score and/or cum")
                continue
            if score is None:
                info.append(f"row {i+1} {p}: score inferred from cums: {cum - prev}")
                e['score'] = cum - prev
                prev = cum
                continue
            if cum is None:
                warnings.append(f"row {i+1} {p}: cum unreadable; chain carried as {prev + score}")
                prev += score
                e['cum'] = prev
                continue
            if prev + score != cum:
                msg = (f"row {i+1} {p}: cumulative break: {prev} + {score} != {cum} "
                       f"(if cum is right the score reads {cum - prev}; "
                       f"if score is right the cum reads {prev + score} — re-read that cell)")
                if e.get('table_error'):
                    # Declared table error: both cells re-read and unambiguous, so
                    # the SHEET's arithmetic is wrong, not the transcription. Keep
                    # the recorded score (the solver adjudicates it against the
                    # board) and resync the chain to the written cum so every
                    # later link still cross-checks.
                    warnings.append(msg.replace(' — re-read that cell)', ')')
                                    + " — DECLARED table_error: score kept, chain "
                                      "resynced to the written cum")
                else:
                    errors.append(msg)
                prev = cum  # resync so one bad cell doesn't cascade
            else:
                prev = cum

    # --- rack vs word (jesse's column has racks) ---
    # Jesse's rack is a full 7 tiles until the bag empties; a shorter
    # transcribed rack mid-game is usually a misread cell (confirmed real
    # failure mode: AEINNNR read as AEINNR, game #91). Warn except on his
    # last two entries, where a short rack is legitimately possible.
    jesse_word_rows = [i for i, e in cols['jesse']
                       if entry_kind(e.get('entry')) in ('word', 'exchange')]
    for i, e in cols['jesse']:
        kind = entry_kind(e.get('entry'))
        rack = e.get('rack')
        # Jesse records the rack column either as the full 7-tile rack or as
        # just the LEFTOVER tiles kept after the play ("kept", or a short cell
        # disjoint from the played letters). Both are complete — author_gcg.py
        # rebuilds the full rack as kept + placed. Only a cell that is BOTH
        # short and overlapping the play is a misread (game #91's AEINNR).
        kept = e.get('kept')
        if kept is None and rack and len(rack) < 7 and kind == 'word':
            played = Counter(c.upper() for c in e['entry'] if not c.islower())
            if not (Counter(rack.upper()) & played):
                kept, rack = rack, None
        # NB: `kept` of "" is meaningful — a bingo keeps nothing — so test for
        # presence, never truthiness.
        if kind in ('word', 'exchange') and not rack and kept is None:
            # A MISSING rack is the more serious version of a short one: it
            # means that row of the far-left column was never read at all.
            # Jesse's racks are always on the sheet, so this is an incomplete
            # transcription — it is what left games #91/#92 with no BestBot
            # stats (known-issues.md). Downgraded only under the explicit
            # --allow-partial-racks escape hatch, for an illegible cell.
            (warnings if allow_partial_racks else errors).append(
                f"row {i+1} jesse: no rack transcribed — read it off the "
                "scoresheet's far-left column (7 letters, '?' = blank), or record "
                "the leftover tiles as \"kept\". Without one of them the GCG falls "
                "back to played-tiles-only racks and the game gets no BestBot "
                "stats for Jesse.")
        if (kind in ('word', 'exchange') and rack and len(rack) != 7
                and i not in jesse_word_rows[-2:]):
            warnings.append(f"row {i+1} jesse: rack {rack} has {len(rack)} tiles mid-game — "
                            "racks are 7 until the bag empties; re-read the cell for "
                            "missed letters")
        if kind == 'word' and rack:
            blanks_needed = sum(1 for c in e['entry'] if c.islower())
            have = Counter(rack.upper().replace('?', ''))
            if rack.upper().count('?') < blanks_needed:
                errors.append(f"row {i+1} jesse: word {e['entry']} needs {blanks_needed} blank(s), "
                              f"rack {rack} has {rack.upper().count('?')}")
            missing = Counter(c.upper() for c in e['entry'] if not c.islower()) - have
            if missing:
                info.append(f"row {i+1} jesse: letters {''.join(sorted(missing.elements()))} of "
                            f"{e['entry']} not in rack {rack} — must be play-through tiles; "
                            "verify against the board")

    # --- exchange rows: recover the tiles ---
    jesse_entries = cols['jesse']
    for idx, (i, e) in enumerate(jesse_entries):
        kind = entry_kind(e.get('entry'))
        if kind != 'exchange':
            continue
        spec = e['entry'][1:]
        rack = e.get('rack')
        if spec.isdigit():
            n = int(spec)
            if not rack:
                warnings.append(f"row {i+1} jesse: exchange x{n} with no rack recorded — "
                                "tiles unrecoverable, encode as unknown exchange")
                continue
            nxt = next((e2.get('rack') for _, e2 in jesse_entries[idx+1:] if e2.get('rack')), None)
            if not nxt:
                warnings.append(f"row {i+1} jesse: exchange x{n}: no following rack recorded — "
                                "exchanged tiles genuinely unrecoverable; note the uncertainty, "
                                "do not guess")
                continue
            kept = Counter(rack.upper()) & Counter(nxt.upper())
            exchanged = Counter(rack.upper()) - kept
            if sum(exchanged.values()) != n:
                errors.append(f"row {i+1} jesse: exchange x{n} but rack {rack} minus next rack "
                              f"{nxt} leaves {''.join(sorted(exchanged.elements()))} "
                              f"({sum(exchanged.values())} tiles) — re-read one of the racks or the x-count")
            else:
                tiles = ''.join(sorted(exchanged.elements()))
                info.append(f"row {i+1} jesse: exchange x{n} recovered as x{tiles} "
                            f"(kept {''.join(sorted(kept.elements()))})")
                e['entry'] = 'x' + tiles
        else:
            if rack:
                extra = Counter(spec.upper()) - Counter(rack.upper())
                if extra:
                    errors.append(f"row {i+1} jesse: exchanged tiles {spec} not all in rack {rack}")

    # --- merged turn order + alternation ---
    order = players if t['first_player'] == 'jesse' else ['opponent', 'jesse']
    seq = []
    for i, row in enumerate(rows):
        for p in order:
            e = row.get(p)
            if e is None:
                continue
            kind = entry_kind(e.get('entry'))
            if kind in ('word', 'exchange', 'pass'):
                seq.append((i, p, e))
            elif kind == 'bonus':
                if not any(p2 == p for _, p2, _ in seq):
                    errors.append(f"row {i+1} {p}: challenge bonus before any move by {p}")
    for (i1, p1, _), (i2, p2, _) in zip(seq, seq[1:]):
        if p1 == p2:
            errors.append(f"rows {i1+1}->{i2+1}: {p1} moves twice in a row — a turn is missing "
                          "from the other column (check for an unrecorded exchange/pass, or a "
                          "cell misread as filler)")
    n_turns = Counter(p for _, p, _ in seq)
    if not (0 <= n_turns[order[0]] - n_turns[order[1]] <= 1):
        errors.append(f"turn counts irreconcilable: {order[0]}={n_turns[order[0]]}, "
                      f"{order[1]}={n_turns[order[1]]} with {order[0]} moving first")

    # --- boxes / leftover ---
    leftover = t.get('leftover') or {}
    lo_tiles, lo_player = leftover.get('tiles', ''), leftover.get('player')
    boxes = t.get('boxes', {})
    for p in players:
        b = boxes.get(p)
        if not b:
            warnings.append(f"boxes missing for {p}")
            continue
        last_cum = 0
        for _, e in cols[p]:
            if e.get('cum') is not None:
                last_cum = e['cum']
        expect = last_cum + b.get('plus_tiles', 0) - abs(b.get('minus_time', 0))
        if expect != b.get('total'):
            errors.append(f"{p} totals box: last cum {last_cum} + tiles {b.get('plus_tiles', 0)} "
                          f"- time {abs(b.get('minus_time', 0))} = {expect}, box says {b.get('total')}")
        if lo_player and p != lo_player and lo_tiles:
            if b.get('plus_tiles', 0) != 2 * tile_value(lo_tiles):
                errors.append(f"{p} +Tiles box {b.get('plus_tiles')} != 2 x value of leftover "
                              f"{lo_tiles} ({2 * tile_value(lo_tiles)})")
    if lo_player and boxes.get(lo_player, {}).get('plus_tiles', 0) not in (0, None):
        errors.append(f"{lo_player} held the leftover tiles but has a nonzero +Tiles box")

    # --- blanks ---
    played_blanks = sorted(c.upper()
                           for _, _, e in seq if entry_kind(e.get('entry')) == 'word'
                           for c in e['entry'] if c.islower())
    declared = sorted(b.upper() for b in t.get('blanks', []))
    if played_blanks != declared:
        errors.append(f"blanks played as {played_blanks} but tracking grid declares {declared}")
    if len(declared) > DIST['?']:
        errors.append(f"{len(declared)} blanks declared; the bag has {DIST['?']}")

    return errors, warnings, info, seq


def build_spec(t, seq):
    moves = []
    for i, p, e in seq:
        if entry_kind(e.get('entry')) != 'word':
            continue
        m = {'player': 'JD' if p == 'jesse' else t['opponent'].replace(' ', '_'),
             'word': e['entry'], 'score': e['score']}
        for k in ('dir', 'row', 'col', 'phony'):
            if k in e:
                m[k] = e[k]
        if 'dir' not in m:
            m['dir'] = '?'  # must be filled from the board read before solving
        moves.append(m)
    lo = t.get('leftover') or {}
    return {'moves': moves, 'leftover': lo.get('tiles', '')}


def main():
    args = sys.argv[1:]
    spec_out = None
    if '--spec' in args:
        i = args.index('--spec')
        spec_out = args[i + 1]
        del args[i:i + 2]
    allow_partial = '--allow-partial-racks' in args
    if allow_partial:
        args.remove('--allow-partial-racks')
    t = json.load(open(args[0]))
    errors, warnings, info, seq = check(t, allow_partial_racks=allow_partial)
    for e in errors:
        print(f"ERROR: {e}")
    for w in warnings:
        print(f"WARN:  {w}")
    for n in info:
        print(f"info:  {n}")
    if errors:
        print(f"\nFAIL: {len(errors)} violation(s). Re-read the flagged cells and fix the "
              "transcript — do NOT proceed to the solver.")
        sys.exit(1)
    print(f"\nPASS ({len(warnings)} warning(s)). {len(seq)} turns in strict order.")
    if spec_out:
        spec = build_spec(t, seq)
        missing = [m['word'] for m in spec['moves'] if m['dir'] == '?']
        with open(spec_out, 'w') as f:
            json.dump(spec, f, indent=1)
        print(f"wrote {spec_out}" + (f" — fill dir (and row/col if confident) from the board "
              f"photo for: {', '.join(missing)}" if missing else ""))


if __name__ == '__main__':
    main()
