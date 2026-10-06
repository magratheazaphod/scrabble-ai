#!/usr/bin/env python3
"""Pre-flight scanner/healer for .gcg files before uploading to woogles.io.

Detects the failure modes that break the Woogles ImportGCG parser and, where
possible, heals them automatically. Verified against the live API 2026-07-06
(see .claude/skills/gcg-upload/SKILL.md for the full findings).

Issues handled (numbering kept stable; heal 1 was retired):

1. (retired 2026-10-06) challenge-before-final-bonus: a trailing
   "(challenge) +N" line just before the going-out bonus used to leave the
   import stuck unfinished (liwords#1350). Fixed server-side by liwords#1983
   and confirmed live, so such files now upload as written.

2. played-through letters written literally (HEALED): some transcriptions
   (e.g. NSC '15 finals, NSC 2010 annotated games) write the full word
   including tiles already on the board. The parser requires the
   played-through marker "." and rejects these files ("tried to play through
   a letter already on the board"). Heal: simulate the board and replace
   letters that are already on the board with ".". Files where a placement
   conflicts with a *different* letter already on the board are flagged
   unhealable.

3. missing #player headers (HEALED): synthesized from the first two distinct
   move-line nicknames.

4. placeholder rack on a challenge line (HEALED): some exports write
   ">Nick: UNKNOWN (challenge) +5 CUM". ImportGCG reads UNKNOWN as tiles and
   fails with "tried to add a tile (U) that is not in the bag", leaving a stuck
   unfinished game behind. Heal: empty the rack field.

5. Detection only (flagged, no heal): endgame-line rack/sign mismatch (empty
   rack + negative score, or populated rack + positive score), files over the
   128,000-byte ImportGCG cap, files with no move lines.

NOT an issue (verified live): "+-N" scores on end-rack-penalty lines parse
fine; lowercase coordinates and column-aligned whitespace parse fine.

Usage:
    python3 scripts/gcg_preflight.py <file-or-dir> [...]
        Scan and report; healed copies are written next to each problem file
        as <name>.healed.gcg (never overwrites the original).
    python3 scripts/gcg_preflight.py --in-place <file-or-dir> [...]
        Heal originals in place (originals are only touched when a heal
        applies; a .bak copy is written alongside).
    python3 scripts/gcg_preflight.py --check <file-or-dir> [...]
        Report only, write nothing. Exit code 1 if any file needs healing or
        is unhealable.

Upload flow: run this over a tournament folder first; upload the healed
content (the .healed.gcg file when present, otherwise the original).
"""
import os
import re
import sys

MAX_GCG_BYTES = 128_000

# ">Nick: RACK POS WORD +N CUM" — whitespace-flexible, tolerates trailing text
PLAY_RE = re.compile(
    r'^>(?P<nick>[^:]+):\s+(?P<rack>\S+)\s+(?P<pos>\d{1,2}[A-Za-z]|[A-Za-z]\d{1,2})\s+'
    r'(?P<word>[A-Za-z.]+)\s+\+(?P<score>\d+)(?P<rest>.*)$')
# ">Nick: RACK -- -N CUM" — withdrawn (successfully challenged) play
WITHDRAWN_RE = re.compile(r'^>(?P<nick>[^:]+):\s+\S+\s+--\s+-\d+')
# ">Nick: [RACK] (LEFTOVER) +/-N CUM" — going-out bonus (empty rack, +N) or
# six-scoreless penalty (populated rack, -N); "+-N" is server-tolerated
END_RE = re.compile(
    r'^>(?P<nick>[^:]+):(?P<rackfield>[^(]*)\((?P<leftover>[^)]*)\)\s+'
    r'(?P<sign>\+-?|-)(?P<pts>\d+)\s+(?P<cum>\d+)\s*$')
MOVE_LINE_RE = re.compile(r'^>')
# ">Nick: UNKNOWN (challenge) ..." — placeholder rack from some exports
UNKNOWN_CHALLENGE_RE = re.compile(r'^(>[^:]+:)\s+UNKNOWN(\s+\(challenge\))')


def parse_pos(pos):
    """Return (row0, col0, horizontal) from a GCG coordinate."""
    m = re.fullmatch(r'(\d{1,2})([A-Za-z])', pos)
    if m:
        return int(m.group(1)) - 1, ord(m.group(2).upper()) - ord('A'), True
    m = re.fullmatch(r'([A-Za-z])(\d{1,2})', pos)
    if m:
        return int(m.group(2)) - 1, ord(m.group(1).upper()) - ord('A'), False
    return None


class Unhealable(Exception):
    pass


def heal_playthrough(lines):
    """Replace literally-written played-through letters with '.'.

    Simulates the board; raises Unhealable on a genuine conflict. Returns
    (new_lines, n_lines_changed).
    """
    board = [[None] * 15 for _ in range(15)]
    out = list(lines)
    changed = 0
    last_play = None  # (line_idx, [(r, c) placed]) for withdrawn-play undo
    for i, line in enumerate(lines):
        if WITHDRAWN_RE.match(line):
            if last_play:
                for r, c in last_play[1]:
                    board[r][c] = None
                last_play = None
            continue
        m = PLAY_RE.match(line)
        if not m:
            continue
        coords = parse_pos(m.group('pos'))
        if coords is None:
            continue
        row, col, horiz = coords
        word = m.group('word')
        new_word = []
        placed = []
        for j, ch in enumerate(word):
            r, c = (row, col + j) if horiz else (row + j, col)
            if not (0 <= r < 15 and 0 <= c < 15):
                raise Unhealable(f'line {i + 1}: play runs off the board: {line.strip()}')
            cur = board[r][c]
            if ch == '.':
                if cur is None:
                    raise Unhealable(f'line {i + 1}: "." over empty square: {line.strip()}')
                new_word.append(ch)
            elif cur is None:
                board[r][c] = ch.upper()
                placed.append((r, c))
                new_word.append(ch)
            elif cur == ch.upper():
                new_word.append('.')  # played-through letter written literally
            else:
                raise Unhealable(
                    f'line {i + 1}: {ch!r} conflicts with {cur!r} on board: {line.strip()}')
        last_play = (i, placed)
        new_word = ''.join(new_word)
        if new_word != word:
            start, end = m.start('word'), m.end('word')
            out[i] = line[:start] + new_word + line[end:]
            changed += 1
    return out, changed


def scan_file(path):
    """Return (issues, healed_lines_or_None). issues: list of (kind, detail, healed?)."""
    issues = []
    with open(path, encoding='utf-8', errors='replace') as f:
        lines = f.readlines()

    size = os.path.getsize(path)
    if size > MAX_GCG_BYTES:
        issues.append(('exceeds-128kb-cap', f'{size} bytes', False))

    move_lines = [l for l in lines if MOVE_LINE_RE.match(l)]
    if not move_lines:
        issues.append(('no-move-lines', 'not a game file?', False))
        return issues, None

    healed = list(lines)
    did_heal = False

    if not any(l.startswith('#player1') for l in lines):
        nicks = []
        for l in move_lines:
            n = l[1:].split(':', 1)[0].strip()
            if n and n not in nicks:
                nicks.append(n)
            if len(nicks) == 2:
                break
        if len(nicks) == 2:
            hdr = [f'#player1 {nicks[0]} {nicks[0]}\n', f'#player2 {nicks[1]} {nicks[1]}\n']
            insert_at = next((i for i, l in enumerate(healed) if MOVE_LINE_RE.match(l)), 0)
            healed[insert_at:insert_at] = hdr
            issues.append(('missing-player-headers', f'synthesized for {nicks[0]}/{nicks[1]}', True))
            did_heal = True
        else:
            issues.append(('missing-player-headers', 'could not infer both players', False))

    # unterminated game: proper GCG endings always finish with going-out
    # bonus / rack-penalty lines. A file ending on a regular play imports as
    # an UNFINISHED game, which blocks all further ImportGCG calls on the
    # account until deleted. Flag it — the missing ending must be added by
    # hand (or the file skipped).
    if not END_RE.match(move_lines[-1]):
        issues.append(('unterminated-game', 'file does not end with endgame '
                       'bonus/penalty lines; would import as a stuck unfinished game', False))

    # endgame rack/sign mismatch (detection only — needs human judgment)
    m = END_RE.match(move_lines[-1])
    if m:
        rack_empty = m.group('rackfield').strip() == ''
        negative = m.group('sign') in ('-', '+-')
        if rack_empty and negative:
            issues.append(('endgame-mismatch', 'empty rack but negative score '
                           '(going-out bonus should be positive)', False))
        elif not rack_empty and not negative:
            issues.append(('endgame-mismatch', 'populated rack but positive score '
                           '(six-scoreless penalty should be negative)', False))

    n_unknown = sum(bool(UNKNOWN_CHALLENGE_RE.match(l)) for l in healed)
    if n_unknown:
        healed = [UNKNOWN_CHALLENGE_RE.sub(r'\1 \2', l) for l in healed]
        issues.append(('placeholder-challenge-rack',
                       f'{n_unknown} "UNKNOWN (challenge)" line(s) given an empty rack', True))
        did_heal = True

    try:
        healed2, n = heal_playthrough(healed)
        if n:
            healed = healed2
            issues.append(('literal-playthrough-letters', f'{n} play line(s) rewritten with "."', True))
            did_heal = True
    except Unhealable as e:
        issues.append(('playthrough-conflict', f'UNHEALABLE: {e}', False))

    return issues, (healed if did_heal else None)


def main(argv):
    in_place = '--in-place' in argv
    check_only = '--check' in argv
    paths = [a for a in argv if not a.startswith('--')]
    if not paths:
        print(__doc__)
        return 2

    files = []
    for p in paths:
        if os.path.isdir(p):
            for dirpath, _, fns in os.walk(p):
                files.extend(os.path.join(dirpath, fn) for fn in sorted(fns)
                             if fn.lower().endswith('.gcg')
                             and not fn.lower().endswith('.healed.gcg'))
        else:
            files.append(p)

    n_clean = n_healed = n_unhealable = 0
    for path in files:
        issues, healed = scan_file(path)
        if not issues:
            n_clean += 1
            continue
        unhealable = [i for i in issues if not i[2]]
        print(f'\n{path}')
        for kind, detail, ok in issues:
            print(f'  [{"healed" if ok else "FLAG"}] {kind}: {detail}')
        if healed and not check_only:
            if in_place:
                bak = path + '.bak'
                if not os.path.exists(bak):
                    os.replace(path, bak)
                    dest = path
                else:
                    print(f'  !! {bak} already exists; writing .healed.gcg instead')
                    dest = re.sub(r'\.gcg$', '.healed.gcg', path, flags=re.IGNORECASE)
            else:
                dest = re.sub(r'\.gcg$', '.healed.gcg', path, flags=re.IGNORECASE)
            with open(dest, 'w', encoding='utf-8') as f:
                f.writelines(healed)
            print(f'  -> wrote {dest}')
        if unhealable:
            n_unhealable += 1
        elif healed:
            n_healed += 1

    print(f'\n{len(files)} file(s): {n_clean} clean, {n_healed} healed, '
          f'{n_unhealable} with unhealable/flagged issues')
    return 1 if (check_only and (n_healed or n_unhealable)) or n_unhealable else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
