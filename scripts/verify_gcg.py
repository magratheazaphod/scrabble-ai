#!/usr/bin/env python3
"""Independent GCG replay verifier.

Parses a .gcg from scratch and replays it on a fresh board, recomputing every
move's score with the solver's scorer. Replaces the hand-replay in SKILL.md
Step 6.1 — run it (plus scripts/gcg_preflight.py --check) before any upload.

Checks:
- play strings replay legally ('.' cells occupied, letter cells empty — the
  Woogles importer hard-fails on literal play-through letters);
- every play's recomputed score equals the recorded score (incl. +50 bingos);
- both players' cumulative chains, including challenge bonuses, time
  penalties, and the endgame lines;
- going-out bonus = 2 x value of the tiles in parentheses;
- endgame lines match one of the two legal formats (gcg-upload gotcha);
- tile-bag audit: no letter overdrawn vs the standard distribution;
- every word formed is valid CSW24 (WARNING only — phonies can legally stand).

Usage: python3 verify_gcg.py file.gcg [--no-lexicon]
Exit 0 if no errors (warnings allowed), 1 otherwise.
"""
import sys, os, re
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import otb_solver
from otb_solver import score_play, words_formed, load_lexicon, VALS, DIST


def parse_pos(pos):
    m = re.match(r'^(\d{1,2})([A-Oa-o])$', pos)
    if m:
        return int(m.group(1)) - 1, ord(m.group(2).upper()) - 65, 0, 1
    m = re.match(r'^([A-Oa-o])(\d{1,2})$', pos)
    if m:
        return int(m.group(2)) - 1, ord(m.group(1).upper()) - 65, 1, 0
    return None


def parse_score(tok):
    return int(tok.replace('+-', '-').lstrip('+'))


def parse_events(lines):
    """Yield event dicts. Raises ValueError on an unparseable line."""
    for ln, line in enumerate(lines, 1):
        line = line.rstrip('\n')
        if not line.startswith('>'):
            continue
        nick = line[1:line.index(':')]
        toks = line[line.index(':') + 1:].split()
        e = {'line': ln, 'nick': nick, 'raw': line}
        if toks[0].startswith('(') and toks[0] not in ('(challenge)', '(time)'):
            # end-rack bonus: empty rack field, opponent leftover in parens
            e.update(type='endrack_bonus', tiles=toks[0][1:-1],
                     score=parse_score(toks[1]), cum=int(toks[2]))
        elif len(toks) >= 2 and toks[1] == '(challenge)':
            e.update(type='challenge', rack=toks[0],
                     score=parse_score(toks[2]), cum=int(toks[3]))
        elif len(toks) >= 2 and toks[1] == '(time)':
            e.update(type='time', rack=toks[0],
                     score=parse_score(toks[2]), cum=int(toks[3]))
        elif len(toks) >= 2 and toks[1].startswith('(') and toks[1].endswith(')'):
            # six-scoreless-turns penalty: rack repeated in parens, negative
            e.update(type='endrack_penalty', rack=toks[0], tiles=toks[1][1:-1],
                     score=parse_score(toks[2]), cum=int(toks[3]))
        elif len(toks) >= 2 and toks[1] == '--':
            # withdrawal: the previous play was successfully challenged off
            e.update(type='withdrawal', rack=toks[0], score=parse_score(toks[2]),
                     cum=int(toks[3]))
        elif len(toks) >= 2 and toks[1] == '-':
            e.update(type='pass', rack=toks[0], score=parse_score(toks[2]),
                     cum=int(toks[3]))
        elif len(toks) >= 2 and toks[1].startswith('-'):
            e.update(type='exchange', rack=toks[0], exchanged=toks[1][1:],
                     score=parse_score(toks[2]), cum=int(toks[3]))
        elif len(toks) >= 5 and parse_pos(toks[1]):
            e.update(type='play', rack=toks[0], pos=toks[1], play=toks[2],
                     score=parse_score(toks[3]), cum=int(toks[4]))
        else:
            raise ValueError(f"line {ln}: unrecognized event: {line}")
        yield e


def verify(path, lex):
    errors, warnings = [], []
    lines = open(path, encoding='utf-8').read().splitlines()
    events = list(parse_events(lines))
    board = {}
    cums = {}
    last_play = {}  # nick -> (placed, score) of their most recent play
    for i, e in enumerate(events):
        nick, typ = e['nick'], e['type']
        prev = cums.get(nick, 0)
        if typ == 'withdrawal':
            if nick not in last_play:
                errors.append(f"line {e['line']}: withdrawal (--) with no preceding play")
                continue
            placed, sc = last_play.pop(nick)
            if e['score'] != -sc:
                errors.append(f"line {e['line']}: withdrawal score {e['score']} != "
                              f"-{sc} (the withdrawn play's score)")
            for r, c, _ in placed:
                del board[(r, c)]
            cums[nick] = prev + e['score']
        elif typ == 'play':
            r0, c0, dr, dc = parse_pos(e['pos'])
            word, ok = '', True
            for j, ch in enumerate(e['play']):
                r, c = r0 + dr * j, c0 + dc * j
                cur = board.get((r, c))
                if ch == '.':
                    if cur is None:
                        errors.append(f"line {e['line']}: '.' at {chr(65+c)}{r+1} but no tile there")
                        ok = False
                        break
                    word += cur
                else:
                    if cur is not None:
                        errors.append(f"line {e['line']}: literal letter {ch} over occupied "
                                      f"{chr(65+c)}{r+1} — importer rejects this; use '.'")
                        ok = False
                        break
                    word += ch
            if not ok:
                continue
            res = score_play(board, word, r0, c0, dr, dc)
            if res is None:
                errors.append(f"line {e['line']}: illegal placement {e['pos']} {e['play']}")
                continue
            sc, placed = res
            if sc != e['score']:
                errors.append(f"line {e['line']}: recorded {e['score']}, replay computes {sc}")
            withdrawn_next = (i + 1 < len(events)
                              and events[i + 1]['type'] == 'withdrawal'
                              and events[i + 1]['nick'] == nick)
            if lex is not None and not withdrawn_next:
                bad = [w for w in words_formed(board, word, r0, c0, dr, dc, placed)
                       if w not in lex]
                if bad:
                    warnings.append(f"line {e['line']}: invalid CSW24 word(s) {bad} — fine only "
                                    "if the phony genuinely stood unchallenged")
            rack = Counter(e['rack'].upper())
            need = Counter('?' if ch.islower() else ch for _, _, ch in placed)
            if need - rack:
                missing = ''.join(sorted((need - rack).elements()))
                if withdrawn_next:
                    warnings.append(f"line {e['line']}: played tiles {missing} not in declared "
                                    f"rack {e['rack']} — tolerated: the play was challenged off")
                else:
                    errors.append(f"line {e['line']}: played tiles {missing} not in declared "
                                  f"rack {e['rack']}")
            for r, c, ch in placed:
                board[(r, c)] = ch
            last_play[nick] = (placed, e['score'])
            cums[nick] = prev + e['score']
        elif typ in ('challenge', 'time', 'pass', 'exchange', 'endrack_bonus', 'endrack_penalty'):
            if typ == 'endrack_bonus':
                exp = 2 * sum(VALS[c.upper()] if c != '?' else 0 for c in e['tiles'])
                if e['score'] != exp:
                    errors.append(f"line {e['line']}: end-rack bonus {e['score']} != 2 x value "
                                  f"of ({e['tiles']}) = {exp}")
                if e['score'] <= 0:
                    errors.append(f"line {e['line']}: going-out bonus must be positive "
                                  "(negative = six-pass penalty format, which needs the rack repeated)")
            if typ == 'endrack_penalty' and e['score'] >= 0:
                errors.append(f"line {e['line']}: rack-repeated endgame line must be negative")
            if typ in ('pass', 'exchange') and e['score'] != 0:
                errors.append(f"line {e['line']}: {typ} must score 0")
            cums[nick] = prev + e['score']
        if cums[nick] != e['cum']:
            errors.append(f"line {e['line']}: cumulative {e['cum']} but chain gives {cums[nick]} "
                          f"({prev} + {e['score']})")
            cums[nick] = e['cum']  # resync
    # endgame format: a finished game ends with one endrack_bonus, or two
    # endrack_penalty lines; trailing (time) penalty lines are legal after those
    tail = [e['type'] for e in events if e['type'] != 'time'][-2:]
    if not (tail[-1:] == ['endrack_bonus'] or tail == ['endrack_penalty', 'endrack_penalty']):
        errors.append("file does not end in a legal endgame line pair "
                      "(going-out bonus, or two rack-repeated penalty lines) — "
                      "unterminated games import as stuck-unfinished")
    # bag audit
    used = Counter('?' if ch.islower() else ch.upper() for ch in board.values())
    for e in events:
        if e['type'] == 'endrack_bonus':
            used += Counter(e['tiles'].upper())
    over = {k: used[k] - DIST[k] for k in used if used[k] > DIST[k]}
    if over:
        errors.append(f"bag overdrawn: {over}")
    return errors, warnings, sum(used.values()), dict(cums)


def main():
    args = sys.argv[1:]
    lex = None
    if '--no-lexicon' in args:
        args.remove('--no-lexicon')
    else:
        lex = load_lexicon()
    path = args[0]
    try:
        errors, warnings, tiles, cums = verify(path, lex)
    except ValueError as e:
        print(f"ERROR: {e}")
        sys.exit(1)
    for e in errors:
        print(f"ERROR: {e}")
    for w in warnings:
        print(f"WARN:  {w}")
    finals = ', '.join(f"{k} {v}" for k, v in cums.items())
    if errors:
        print(f"\nFAIL: {len(errors)} error(s) in {os.path.basename(path)}")
        sys.exit(1)
    print(f"PASS: {os.path.basename(path)} replays cleanly — {tiles} tiles accounted, "
          f"finals: {finals}" + (f" ({len(warnings)} warning(s))" if warnings else ""))


if __name__ == '__main__':
    main()
