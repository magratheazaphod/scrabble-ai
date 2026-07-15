#!/usr/bin/env python3
"""Author a Woogles-importable .gcg from a verified transcript + solver solution.

Codifies SKILL.md Step 5 so nothing is hand-assembled: player order, GCG
coordinates, '.'-play-through strings, lowercase blanks, exchange/pass lines,
challenge-bonus placement, the end-rack bonus line, time penalties — and the
partial-rack endgame rule (the "can only pass or challenge" pitfall), which is
enforced mechanically: near the endgame the opponent's declared rack is derived
from their remaining plays + leftover instead of just the played tiles.

Cumulatives are recomputed from board-true scores (the solver's), never copied
from the sheet: Woogles recomputes scores from placements during import, so the
file MUST be self-consistent. Any recorded-vs-board-true deviation gets a #note.

Usage:
  python3 author_gcg.py transcript.json solver.json --game-number 92 --out out.gcg
         [--finalist 0]   which solver finalist to use when >1 (default 0)

transcript.json: the check_transcription.py input (must PASS — this re-runs it).
solver.json:     otb_solver.py --json output.
"""
import sys, os, json, argparse
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from otb_solver import VALS
from check_transcription import check, entry_kind

JESSE_NICK, JESSE_NAME = 'JD', 'Jesse Day'


def rack_str(placed):
    return ''.join(sorted('?' if p['blank'] else p['letter'].upper() for p in placed))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('transcript')
    ap.add_argument('solver')
    ap.add_argument('--game-number', type=int)
    ap.add_argument('--out', required=True)
    ap.add_argument('--finalist', type=int, default=0)
    args = ap.parse_args()

    t = json.load(open(args.transcript))
    sol = json.load(open(args.solver))
    errors, warnings, info, seq = check(t)
    if errors:
        sys.exit("transcript does not PASS check_transcription.py — fix it first:\n  "
                 + "\n  ".join(errors))
    if sol['minimal_mismatches'] > 0:
        print("WARNING: solver solution carries recorded-score mismatches — the GCG will use "
              "board-true scores; each gets a #note. Re-read SKILL.md's zero-mismatch heuristic "
              "before uploading.")
    fin = sol['finalists'][args.finalist]
    if len(sol['finalists']) > 1:
        print(f"note: {len(sol['finalists'])} finalists exist; using #{args.finalist} — "
              "confirm against the board photo (blank cells etc.) before uploading.")

    opp_nick = t['opponent'].replace(' ', '_')
    nick = {'jesse': JESSE_NICK, 'opponent': opp_nick}
    order = ['jesse', 'opponent'] if t['first_player'] == 'jesse' else ['opponent', 'jesse']

    # pair transcript word turns with solver moves, in order
    smoves = list(fin['moves'])
    events = []           # (player, kind, transcript_entry, solver_move|None)
    for i, row in enumerate(t['rows']):
        for p in order:
            e = row.get(p)
            if e is None:
                continue
            kind = entry_kind(e.get('entry'))
            if kind == 'word':
                m = smoves.pop(0)
                assert m['word'] == e['entry'] and m['player'] == nick[p], \
                    f"solver/transcript move order diverged at row {i+1}: {m} vs {e}"
                events.append((p, 'word', e, m))
            elif kind in ('exchange', 'pass', 'bonus'):
                events.append((p, kind, e, None))
    assert not smoves, "solver has more moves than the transcript"

    leftover = t.get('leftover') or {}
    lo_player, lo_tiles = leftover.get('player'), leftover.get('tiles', '')
    last_word_idx = max(i for i, ev in enumerate(events) if ev[1] == 'word')
    out_player = events[last_word_idx][0]
    if lo_player == out_player:
        sys.exit(f"leftover assigned to {lo_player}, but {lo_player} played the last move — "
                 "the out-player cannot hold leftover tiles; fix the transcript")

    # future placed tiles per player (for endgame full-rack derivation)
    future = {p: [] for p in ('jesse', 'opponent')}
    suffix = {'jesse': Counter(), 'opponent': Counter()}
    future_racks = [None] * len(events)
    for i in range(len(events) - 1, -1, -1):
        p, kind, e, m = events[i]
        future_racks[i] = Counter(suffix[p])
        if kind == 'word':
            suffix[p] += Counter('?' if pl['blank'] else pl['letter'].upper()
                                 for pl in m['placed'])

    lines = ['#character-encoding UTF-8',
             f'#player1 {nick[order[0]]} {JESSE_NAME if order[0] == "jesse" else t["opponent"]}',
             f'#player2 {nick[order[1]]} {JESSE_NAME if order[1] == "jesse" else t["opponent"]}']
    gnum = f" {args.game_number}" if args.game_number else ""
    lines.append(f'#description Practice game{gnum}, {t["date"]}. Reconstructed from '
                 "Jesse's scoresheet and final board photo.")

    cum = {'jesse': 0, 'opponent': 0}
    tiles_on_board = 0
    notes = []
    for i, (p, kind, e, m) in enumerate(events):
        if kind == 'word':
            placed_tiles = Counter('?' if pl['blank'] else pl['letter'].upper()
                                   for pl in m['placed'])
            if p == 'jesse' and e.get('rack'):
                rack = ''.join(sorted(e['rack'].upper()))
            else:
                rack = ''.join(sorted(placed_tiles.elements()))
            # partial-rack endgame rule: derive the true full rack near the endgame
            if 100 - tiles_on_board - len(rack) <= 7 and i != last_word_idx:
                full = placed_tiles + future_racks[i] + \
                       (Counter(lo_tiles.upper()) if p == lo_player else Counter())
                if len(rack) < sum(full.values()):
                    derived = ''.join(sorted(full.elements()))
                    if len(derived) > 7:
                        sys.exit(f"event {i+1} ({m['word']}): derived rack {derived} exceeds 7 "
                                 "tiles — endgame tile ownership is wrong somewhere; re-check "
                                 "the transcription")
                    print(f"endgame rule: event {i+1} ({nick[p]} {m['word']}) rack "
                          f"{rack} -> full rack {derived}")
                    rack = derived
            if i == last_word_idx:
                rack = ''.join(sorted(placed_tiles.elements()))  # final move: exactly its tiles
            cum[p] += m['score']
            lines.append(f">{nick[p]}: {rack} {m['gcg_pos']} {m['play']} +{m['score']} {cum[p]}")
            if m['mismatch']:
                notes.append(f"#note {m['word']}: scoresheet recorded {m['score_recorded']}, "
                             f"board-true value is {m['score']}; the file carries board-true "
                             "scores and recomputed cumulatives.")
                lines.append(notes[-1])
            tiles_on_board += len(m['placed'])
        elif kind == 'exchange':
            spec = e['entry'][1:].upper()
            rack = ''.join(sorted((e.get('rack') or spec).upper()))
            lines.append(f">{nick[p]}: {rack} -{spec} +0 {cum[p]}")
        elif kind == 'pass':
            nxt = next((ev for ev in events[i+1:] if ev[0] == p and ev[1] == 'word'), None)
            rack = (e.get('phony_off') or e.get('rack')
                    or (rack_str(nxt[3]['placed']) if nxt else '')).upper()
            rack = ''.join(sorted(rack))
            lines.append(f">{nick[p]}: {rack} - +0 {cum[p]}")
            if e.get('phony_off'):
                print(f"REMINDER: turn {i+1}: pass stands in for the challenged-off "
                      f"phony {e['phony_off']} — you MUST post a game comment naming it "
                      "(Jesse's standing request).")
        elif kind == 'bonus':
            pts = int(e['entry'])
            nxt = next((ev for ev in events[i+1:] if ev[0] == p and ev[1] == 'word'), None)
            rack = e.get('rack')
            if not rack and nxt:
                te = nxt[2]
                rack = te.get('rack') if p == 'jesse' and te.get('rack') else rack_str(nxt[3]['placed'])
            cum[p] += pts
            lines.append(f">{nick[p]}: {''.join(sorted((rack or '').upper()))} (challenge) "
                         f"+{pts} {cum[p]}")

    # endgame: out-player bonus, then any time penalties
    bonus = 2 * sum(0 if c == '?' else VALS[c.upper()] for c in lo_tiles)
    cum[out_player] += bonus
    lines.append(f">{nick[out_player]}:  ({lo_tiles.upper()}) +{bonus} {cum[out_player]}")
    for p in ('jesse', 'opponent'):
        mt = abs((t.get('boxes', {}).get(p) or {}).get('minus_time', 0) or 0)
        if mt:
            r = lo_tiles.upper() if p == lo_player else ''
            cum[p] -= mt
            lines.append(f">{nick[p]}: {r} (time) -{mt} {cum[p]}")

    with open(args.out, 'w') as f:
        f.write('\n'.join(lines) + '\n')
    print(f"wrote {args.out} — finals: {nick['jesse']} {cum['jesse']}, "
          f"{nick['opponent']} {cum['opponent']}")
    print("now run: verify_gcg.py + gcg_preflight.py --check before upload")


if __name__ == '__main__':
    main()
