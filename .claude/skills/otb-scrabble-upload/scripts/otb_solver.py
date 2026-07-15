#!/usr/bin/env python3
"""OTB scrabble game reconstruction solver.

Given a move list (word, recorded score, direction, row-or-column read from a
board photo), finds the unique placement of every move such that each move's
computed score matches the recorded score exactly, by joint backtracking
search over all start offsets. Scores are the error-detector: with ~25
interlocked moves, exact score matching pins every tile.

Usage: python3 otb_solver.py game_spec.json [--json out.json] [--no-lexicon]

Spec format (JSON):
{
  "moves": [
    {"player": "J", "word": "AIA", "score": 6, "dir": "H", "row": 8},
    {"player": "Je", "word": "sONNETS", "score": 70, "dir": "H", "row": 13},
    {"player": "J", "word": "OPE", "score": 10, "dir": "V", "phony": true},
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
- phony (optional): true = this move stood unchallenged as a phony; skip
  lexicon validation for it (main word AND its cross-words).

Every candidate placement is validated against CSW24 (main word + every
cross-word formed) unless the move is marked phony — invalid-word placements
are rejected outright, so a wrong placement can't survive by fabricating a
phony cross-word. --no-lexicon disables this (e.g. corpus not present).

Output: placements in GCG coordinates, play strings with '.' for play-through,
final board, tile bag audit, and any moves where no exact-score placement
exists (allowing up to 2 flagged mismatches = table scoring errors).
--json writes the finalists machine-readably for author_gcg.py.
"""
import sys, json
from collections import Counter

LEXICON_PATH = '/Users/Siwen/projects/word-game-lexica/CSW24.txt'
LEX = None  # set of valid words, or None when validation is disabled

def load_lexicon(path=LEXICON_PATH):
    # format: WORD<TAB>definition — parse only the first field
    words = set()
    for line in open(path, encoding='utf-8'):
        w = line.split('\t', 1)[0].strip().upper()
        if w:
            words.add(w)
    return words

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

def words_formed(board, word, r0, c0, dr, dc, placed):
    """All words this placement forms: the main word plus every cross-word."""
    words = [word.upper()]
    for r, c, ch in placed:
        xr, xc = dc, dr
        r1, c1 = r, c
        while board.get((r1-xr, c1-xc)) is not None: r1, c1 = r1-xr, c1-xc
        r2, c2 = r, c
        while board.get((r2+xr, c2+xc)) is not None: r2, c2 = r2+xr, c2+xc
        if (r1, c1) == (r2, c2):
            continue
        n = max(abs(r2-r1), abs(c2-c1)) + 1
        s, rr, cc = '', r1, c1
        for _ in range(n):
            s += (ch if (rr, cc) == (r, c) else board[(rr, cc)]).upper()
            rr, cc = rr+xr, cc+xc
        words.append(s)
    return words

def candidates(board, move, exact=True, lex=True):
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
        if not res or (exact and res[0] != target):
            continue
        if lex and LEX is not None and not move.get('phony'):
            if any(w not in LEX for w in words_formed(board, word, r, c, dr, dc, res[1])):
                continue
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
    global LEX
    args = sys.argv[1:]
    json_out = None
    if '--json' in args:
        i = args.index('--json')
        json_out = args[i+1]
        del args[i:i+2]
    if '--no-lexicon' in args:
        args.remove('--no-lexicon')
    else:
        try:
            LEX = load_lexicon()
        except OSError as e:
            sys.exit(f"cannot load lexicon {LEXICON_PATH}: {e}\n"
                     "(pass --no-lexicon to solve without word validation)")
    spec = json.load(open(args[0]))
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
                no_lex = candidates(board, m, exact=True, lex=False) if LEX is not None else []
                if no_lex:
                    rejected = sorted({w for r, c, dr, dc, placed, sc in no_lex
                                       for w in words_formed(board, m['word'], r, c, dr, dc, placed)
                                       if w not in LEX})
                    print(f"first stuck move #{i+1} {m['word']} target {m['score']}: exact-score "
                          f"placements exist but were rejected for forming invalid words: {rejected}")
                    print('(if that word genuinely stood unchallenged, mark the move "phony": true)')
                else:
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
    if best_mm > 0:
        print("** WARNING: no zero-mismatch solution. A reconstruction that only reconciles by "
              "declaring table scoring errors is a red flag, not a result — re-examine the "
              "transcription (esp. endgame tile ownership) before accepting this. **")
    if len(finalists) > 1:
        print("AMBIGUOUS - multiple placements fit. Distinguish using the board photo:")
    json_finalists = []
    for pl, mm in finalists[:4]:
        print("=" * 60)
        board = {}
        jmoves = []
        for i, (r, c, dr, dc, placed, sc) in enumerate(pl):
            m = moves[i]
            gcg_pos = f"{r+1}{chr(65+c)}" if dc else f"{chr(65+c)}{r+1}"
            play = ''
            for j, ch in enumerate(m['word']):
                rr, cc = r+dr*j, c+dc*j
                play += ch if board.get((rr, cc)) is None else '.'
            formed = words_formed(board, m['word'], r, c, dr, dc, placed)
            for rr, cc, ch in placed: board[(rr, cc)] = ch
            flag = '' if sc == m['score'] else f"  ** recorded {m['score']}, board-true {sc} — table error? use board-true in GCG **"
            print(f"{i+1:3d}. {m['player']:3s} {gcg_pos:4s} {play:10s} +{sc}{flag}")
            jmoves.append({
                'index': i+1, 'player': m['player'], 'word': m['word'], 'dir': m['dir'],
                'gcg_pos': gcg_pos, 'play': play, 'score': sc,
                'score_recorded': m['score'], 'mismatch': sc != m['score'],
                'words_formed': formed,
                'placed': [{'row': rr+1, 'col': chr(65+cc), 'letter': ch, 'blank': ch.islower()}
                           for rr, cc, ch in placed],
            })
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
        json_finalists.append({
            'mismatch_count': len(mm),
            'moves': jmoves,
            'board': [''.join(board.get((r, c), '.') for c in range(15)) for r in range(15)],
            'bag': {'accounted': sum(total.values()),
                    'overdrawn': dict(over), 'never_seen': dict(unseen)},
        })
    if json_out:
        with open(json_out, 'w') as f:
            json.dump({'solutions_total': len(sols), 'minimal_mismatches': best_mm,
                       'leftover': spec.get('leftover', ''), 'finalists': json_finalists}, f, indent=1)
        print(f"wrote {json_out}")

if __name__ == '__main__':
    main()
