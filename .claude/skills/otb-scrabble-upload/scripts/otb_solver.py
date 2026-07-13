#!/usr/bin/env python3
"""OTB scrabble game reconstruction solver.

Given a move list (word, recorded score, direction, row-or-column read from a
board photo), finds the unique placement of every move such that each move's
computed score matches the recorded score exactly, by joint backtracking
search over all start offsets. Scores are the error-detector: with ~25
interlocked moves, exact score matching pins every tile.

Usage: python3 otb_solver.py game_spec.json

Spec format (JSON):
{
  "moves": [
    {"player": "J", "word": "AIA", "score": 6, "dir": "H", "row": 8},
    {"player": "Je", "word": "sONNETS", "score": 70, "dir": "H", "row": 13},
    {"player": "J", "word": "OPE", "score": 10, "dir": "V"},
    ...
  ],
  "leftover": "DIT"   // tiles left on the winner-of-nothing's rack (unplayed), "" if none
}
- word: as physically spelled on the board, lowercase letter = blank.
- dir: "H" or "V".
- row (1-15) for H moves / col ("A".."O") for V moves: fix if read confidently
  from the photo; omit to leave fully free (rows read from photo row-bands are
  reliable; column offsets are NOT - leave starts free and let scores decide).
- score: from the scoresheet. MUST reconcile with cumulatives first!

Output: placements in GCG coordinates, play strings with '.' for play-through,
final board, tile bag audit, and any moves where no exact-score placement
exists (allowing up to 2 flagged mismatches = table scoring errors).
"""
import sys, json
from collections import Counter

VALS = {'A':1,'B':3,'C':3,'D':2,'E':1,'F':4,'G':2,'H':4,'I':1,'J':8,'K':5,'L':1,
        'M':3,'N':1,'O':1,'P':3,'Q':10,'R':1,'S':1,'T':1,'U':1,'V':4,'W':4,'X':8,'Y':4,'Z':10}
DIST = Counter({'A':9,'B':2,'C':2,'D':4,'E':12,'F':2,'G':3,'H':2,'I':9,'J':1,'K':1,'L':4,
'M':2,'N':6,'O':8,'P':2,'Q':1,'R':6,'S':4,'T':6,'U':4,'V':2,'W':2,'X':1,'Y':2,'Z':1,'?':2})
TWS = {(0,0),(0,7),(0,14),(7,0),(7,14),(14,0),(14,7),(14,14)}
DWS = {(1,1),(2,2),(3,3),(4,4),(1,13),(2,12),(3,11),(4,10),
       (10,4),(11,3),(12,2),(13,1),(10,10),(11,11),(12,12),(13,13),(7,7)}
TLS = {(1,5),(1,9),(5,1),(5,5),(5,9),(5,13),(9,1),(9,5),(9,9),(9,13),(13,5),(13,9)}
DLS = {(0,3),(0,11),(2,6),(2,8),(3,0),(3,7),(3,14),(6,2),(6,6),(6,8),(6,12),
       (7,3),(7,11),(8,2),(8,6),(8,8),(8,12),(11,0),(11,7),(11,14),(12,6),(12,8),(14,3),(14,11)}

def score_play(board, word, r0, c0, dr, dc):
    """Score placing `word` at (r0,c0,dir). Returns (score, placed) or None if illegal."""
    n = len(word)
    if r0 < 0 or c0 < 0 or r0 + dr*(n-1) > 14 or c0 + dc*(n-1) > 14:
        return None
    placed = []
    for i, ch in enumerate(word):
        r, c = r0 + dr*i, c0 + dc*i
        cur = board.get((r, c))
        if cur is None:
            placed.append((r, c, ch))
        elif cur.upper() != ch.upper():
            return None
    if not placed or len(placed) > 7:
        return None
    if board.get((r0-dr, c0-dc)) is not None or board.get((r0+dr*n, c0+dc*n)) is not None:
        return None  # word would extend beyond declared letters
    if not board:
        if not any((r, c) == (7, 7) for r, c, _ in placed):
            return None
    else:
        touches = len(placed) < n
        for r, c, _ in placed:
            for rr, cc in ((r-1, c), (r+1, c), (r, c-1), (r, c+1)):
                if board.get((rr, cc)) is not None:
                    touches = True
        if not touches:
            return None
    tile_val = lambda ch: 0 if ch.islower() else VALS[ch]
    main, wmult = 0, 1
    for i, ch in enumerate(word):
        r, c = r0 + dr*i, c0 + dc*i
        if board.get((r, c)) is not None:
            main += tile_val(board[(r, c)])
        else:
            main += tile_val(ch) * (3 if (r,c) in TLS else 2 if (r,c) in DLS else 1)
            wmult *= 3 if (r,c) in TWS else 2 if (r,c) in DWS else 1
    total = main * wmult
    for r, c, ch in placed:
        xr, xc = dc, dr
        r1, c1 = r, c
        while board.get((r1-xr, c1-xc)) is not None: r1, c1 = r1-xr, c1-xc
        r2, c2 = r, c
        while board.get((r2+xr, c2+xc)) is not None: r2, c2 = r2+xr, c2+xc
        length = max(abs(r2-r1), abs(c2-c1)) + 1
        if length == 1: continue
        s = tile_val(ch) * (3 if (r,c) in TLS else 2 if (r,c) in DLS else 1)
        wm = 3 if (r,c) in TWS else 2 if (r,c) in DWS else 1
        rr, cc = r1, c1
        for _ in range(length):
            if (rr, cc) != (r, c): s += tile_val(board[(rr, cc)])
            rr, cc = rr+xr, cc+xc
        total += s * wm
    if len(placed) == 7:
        total += 50
    return total, placed

def candidates(board, move, exact=True):
    word, target = move['word'], move['score']
    out = []
    if move['dir'] == 'H':
        rows = [move['row']-1] if 'row' in move else range(15)
        it = ((r, c, 0, 1) for r in rows for c in range(15-len(word)+1))
    else:
        cols = [ord(move['col'])-65] if 'col' in move else range(15)
        it = ((r, c, 1, 0) for c in cols for r in range(15-len(word)+1))
    for r, c, dr, dc in it:
        res = score_play(board, word, r, c, dr, dc)
        if res and (not exact or res[0] == target):
            out.append((r, c, dr, dc, res[1], res[0]))
    return out

def solve(moves, max_mismatch=2):
    solutions = []
    def rec(idx, board, placements, mism):
        if len(mism) > max_mismatch or len(solutions) >= 20:
            return
        if idx == len(moves):
            solutions.append((list(placements), list(mism)))
            return
        cands = candidates(board, moves[idx], exact=True)
        if not cands:
            cands = candidates(board, moves[idx], exact=False)
            flagged = True
        else:
            flagged = False
        for r, c, dr, dc, placed, sc in cands:
            for rr, cc, ch in placed: board[(rr, cc)] = ch
            placements.append((r, c, dr, dc, placed, sc))
            if flagged: mism.append(idx)
            rec(idx+1, board, placements, mism)
            if flagged: mism.pop()
            placements.pop()
            for rr, cc, ch in placed: del board[(rr, cc)]
    rec(0, {}, [], [])
    return solutions

def main():
    spec = json.load(open(sys.argv[1]))
    moves = spec['moves']
    sols = solve(moves)
    if not sols:
        print("NO SOLUTION with <=2 mismatches. Re-check scoresheet transcription:")
        print("run cumulative arithmetic on every column; re-read ambiguous digits.")
        # report deepest reachable prefix
        board = {}
        for i, m in enumerate(moves):
            cands = candidates(board, m, exact=True)
            if not cands:
                all_c = candidates(board, m, exact=False)
                print(f"first stuck move #{i+1} {m['word']} target {m['score']}; "
                      f"achievable: {sorted(set(s for *_, s in all_c))}")
                break
            r, c, dr, dc, placed, sc = cands[0]
            for rr, cc, ch in placed: board[(rr, cc)] = ch
        return
    # prefer fewest mismatches
    sols.sort(key=lambda s: len(s[1]))
    best_mm = len(sols[0][1])
    finalists = [s for s in sols if len(s[1]) == best_mm]
    print(f"{len(sols)} solution(s); {len(finalists)} with minimal {best_mm} mismatch(es).")
    if len(finalists) > 1:
        print("AMBIGUOUS - multiple placements fit. Distinguish using the board photo:")
    for pl, mm in finalists[:4]:
        print("=" * 60)
        board = {}
        for i, (r, c, dr, dc, placed, sc) in enumerate(pl):
            m = moves[i]
            gcg_pos = f"{r+1}{chr(65+c)}" if dc else f"{chr(65+c)}{r+1}"
            play = ''
            for j, ch in enumerate(m['word']):
                rr, cc = r+dr*j, c+dc*j
                play += ch if board.get((rr, cc)) is None else '.'
            for rr, cc, ch in placed: board[(rr, cc)] = ch
            flag = '' if sc == m['score'] else f"  ** recorded {m['score']}, board-true {sc} — table error? use board-true in GCG **"
            print(f"{i+1:3d}. {m['player']:3s} {gcg_pos:4s} {play:10s} +{sc}{flag}")
        used = Counter('?' if ch.islower() else ch for ch in
                       (board[k] for k in board))
        total = used + Counter(spec.get('leftover', '').upper())
        over = {k: total[k]-DIST[k] for k in total if total[k] > DIST[k]}
        unseen = {k: DIST[k]-total[k] for k in DIST if DIST[k] > total[k]}
        print(f"bag audit: {sum(total.values())}/100 accounted; overdrawn: {over or 'none'}; never-seen: {unseen or 'none'}")
        if sum(total.values()) != 100 or over:
            print("** BAG AUDIT FAILED — a word or leftover is wrong **")
        print('   ' + ' '.join('ABCDEFGHIJKLMNO'))
        for r in range(15):
            print(f"{r+1:2d} " + ' '.join(board.get((r, c), '.') for c in range(15)))

if __name__ == '__main__':
    main()
