#!/usr/bin/env python3
"""One-command regression benchmark for the OTB pipeline's deterministic half.

Run after changing any script in this folder, when adding a new ground-truth
game, or to benchmark a change:

  python3 .claude/skills/otb-scrabble-upload/scripts/regression.py [--sweep N]

What it checks (exit 0 = all green):

1. ROUND-TRIP, per ground-truth game (Jesse-confirmed-correct GCGs listed in
   GROUND_TRUTH): rebuild the transcript a perfect photo-reading pass would
   have produced, then run checker -> solver -> author and require the output
   to match the real file event-for-event (racks compared as multisets), plus
   a clean verify_gcg replay. This exercises everything downstream of the
   model's eyes.
2. FAILURE INJECTIONS: corrupt the ground-truth inputs the way the historical
   bad runs actually went wrong, and require the guards to catch each one:
   - drop the game #91 opening exchange     -> checker must FAIL (turn counts)
   - shorten a mid-game rack (AEINNNR bug)  -> checker must WARN
   - drop Jesse's racks entirely (#91/#92)  -> checker must ERROR and
     author_gcg must refuse to write a file
   Plus a leftover-rack equivalence check: a turn recorded as "kept" leftover
   tiles must author the identical GCG as the same turn recorded as a full
   "rack" (both the kept="" bingo case and a non-empty leftover).
   - the wrong VEE endgame reading (#91)    -> solver must REJECT it outright
     (it needs a 3rd V) and name the overdrawn V as the misread pointer
   - an invalid word at exact score         -> solver must reject via lexicon
3. --sweep N (optional, slower): verify_gcg over N seeded-random archive
   games; failures must be the known-defective classes only (unterminated
   files, and game #90's documented LAGGIER inconsistency).

The model's photo-reading itself is NOT covered — that is only measurable on
a live run (see history.md for the cost/correctness baselines to compare).
"""
import sys, os, re, json, subprocess, tempfile, time, glob, random
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, '..', '..', '..', '..'))
sys.path.insert(0, HERE)
from verify_gcg import parse_events, parse_pos
from otb_solver import VALS
from check_transcription import entry_kind

GROUND_TRUTH = [
    ('Practice Games/James Curley/91_jul12_26.gcg', 91),
    ('Practice Games/James Curley/92_jul12_26.gcg', 92),
]
# archive files that verify_gcg is EXPECTED to flag
KNOWN_BAD_SUBSTRINGS = [
    'does not end in a legal endgame line pair',   # unterminated transcriptions
]
KNOWN_BAD_FILES = ['90_jul12_26.gcg']              # documented LAGGIER table error


def gcg_to_transcript(path):
    """Rebuild the transcript a perfect photo-reading pass would produce."""
    lines = open(path).read().splitlines()
    p1 = [l for l in lines if l.startswith('#player1')][0].split()[1]
    p2 = [l for l in lines if l.startswith('#player2')][0].split()[1]
    opp = (p1 if p1 != 'JD' else p2).replace('_', ' ')
    first = 'jesse' if p1 == 'JD' else 'opponent'
    date = [l for l in lines if l.startswith('#description')][0].split(',')[1].strip().split('.')[0]
    board, rows, blanks = {}, [], []
    lo_tiles, lo_player = '', None
    minus_time = {'jesse': 0, 'opponent': 0}
    last_cum = {'jesse': 0, 'opponent': 0}
    for e in parse_events(lines):
        p = 'jesse' if e['nick'] == 'JD' else 'opponent'
        rack = {'rack': e['rack']} if p == 'jesse' and e.get('rack') else {}
        if e['type'] == 'play':
            r0, c0, dr, dc = parse_pos(e['pos'])
            word = ''
            for j, ch in enumerate(e['play']):
                r, c = r0 + dr * j, c0 + dc * j
                if ch == '.':
                    word += board[(r, c)].upper()   # play-through: uppercase
                else:
                    word += ch
                    board[(r, c)] = ch
                    if ch.islower():
                        blanks.append(ch.upper())
            cell = {'entry': word, 'score': e['score'], 'cum': e['cum'],
                    'dir': 'H' if dc else 'V', **rack}
            cell['row' if dc else 'col'] = r0 + 1 if dc else chr(65 + c0)
            rows.append({p: cell})
            last_cum[p] = e['cum']
        elif e['type'] == 'pass':
            rows.append({p: {'entry': 'pass', 'score': 0, 'cum': e['cum'], **rack}})
        elif e['type'] == 'exchange':
            rows.append({p: {'entry': 'x' + e['exchanged'], 'score': 0,
                             'cum': e['cum'], **rack}})
        elif e['type'] == 'challenge':
            rows.append({p: {'entry': f"+{e['score']}", 'score': e['score'],
                             'cum': e['cum'], **rack}})
            last_cum[p] = e['cum']
        elif e['type'] == 'endrack_bonus':
            lo_tiles = e['tiles']
            lo_player = 'opponent' if p == 'jesse' else 'jesse'
        elif e['type'] == 'time':
            minus_time[p] = abs(e['score'])
    bonus = 2 * sum(0 if c == '?' else VALS[c.upper()] for c in lo_tiles)
    boxes = {p: {'plus_tiles': bonus if p != lo_player else 0,
                 'minus_time': minus_time[p],
                 'total': last_cum[p] + (bonus if p != lo_player else 0) - minus_time[p]}
             for p in ('jesse', 'opponent')}
    return {'opponent': opp, 'date': date, 'first_player': first, 'rows': rows,
            'boxes': boxes, 'leftover': {'player': lo_player, 'tiles': lo_tiles},
            'blanks': sorted(blanks)}


def norm_events(path_or_text, is_text=False):
    text = path_or_text if is_text else open(path_or_text).read()
    out = []
    for line in text.splitlines():
        if not line.startswith('>'):
            continue
        head, rest = line.rstrip().split(':', 1)
        toks = rest.split()
        if toks and re.fullmatch(r'[A-Za-z?]+', toks[0]):
            toks[0] = ''.join(sorted(toks[0].upper()))
        out.append(head + ':' + ' '.join(toks))
    return out


def run(script, *args, ok_exit=(0,)):
    r = subprocess.run([sys.executable, os.path.join(HERE, script), *args],
                       capture_output=True, text=True)
    return r


def main():
    sweep_n = 0
    if '--sweep' in sys.argv:
        sweep_n = int(sys.argv[sys.argv.index('--sweep') + 1])
    t0 = time.time()
    failures = []
    tmp = tempfile.mkdtemp(prefix='otb_regress_')

    def path(name):
        return os.path.join(tmp, name)

    # ---- 1. round-trips ----
    for rel, num in GROUND_TRUTH:
        g = os.path.join(REPO, rel)
        t = gcg_to_transcript(g)
        json.dump(t, open(path(f't{num}.json'), 'w'))
        r = run('check_transcription.py', path(f't{num}.json'), '--spec', path(f's{num}.json'))
        if r.returncode != 0:
            failures.append(f'#{num}: checker FAILed on ground truth:\n{r.stdout}')
            continue
        r = run('otb_solver.py', path(f's{num}.json'), '--json', path(f'sol{num}.json'))
        if 'minimal 0 mismatch' not in r.stdout:
            failures.append(f'#{num}: solver did not find a 0-mismatch solution:\n{r.stdout[:400]}')
            continue
        r = run('author_gcg.py', path(f't{num}.json'), path(f'sol{num}.json'),
                '--game-number', str(num), '--out', path(f'out{num}.gcg'))
        if r.returncode != 0:
            failures.append(f'#{num}: author failed:\n{r.stdout}{r.stderr}')
            continue
        if norm_events(g) != norm_events(path(f'out{num}.gcg')):
            failures.append(f'#{num}: authored GCG does not match ground truth event-for-event')
        r = run('verify_gcg.py', path(f'out{num}.gcg'))
        if r.returncode != 0:
            failures.append(f'#{num}: verify_gcg failed on authored output:\n{r.stdout}')
        print(f'round-trip #{num}: {"ok" if not failures else "see failures"}')

    # ---- 2. failure injections (game #91 ground truth) ----
    t91 = gcg_to_transcript(os.path.join(REPO, GROUND_TRUTH[0][0]))

    t = json.loads(json.dumps(t91))
    assert t['rows'][0]['jesse']['entry'].startswith('x')
    del t['rows'][0]
    json.dump(t, open(path('inj_drop.json'), 'w'))
    r = run('check_transcription.py', path('inj_drop.json'))
    if r.returncode == 0:
        failures.append('injection: dropped opening exchange NOT caught by checker')
    print('injection dropped-exchange:', 'caught' if r.returncode != 0 else 'MISSED')

    t = json.loads(json.dumps(t91))
    for row in t['rows']:
        j = row.get('jesse')
        if j and j.get('rack') == 'AEINNNR':
            j['rack'] = 'AEINNR'
    json.dump(t, open(path('inj_rack.json'), 'w'))
    r = run('check_transcription.py', path('inj_rack.json'))
    if 'WARN' not in r.stdout or 'AEINNR' not in r.stdout:
        failures.append('injection: short mid-game rack did not WARN')
    print('injection short-rack:', 'warned' if 'WARN' in r.stdout else 'MISSED')

    # A MISSING Jesse rack must be an ERROR in the checker and must stop
    # author_gcg outright — the #91/#92 defect (played-tiles-only racks →
    # no BestBot stats → un-analyzable game, unfixable after upload).
    t = json.loads(json.dumps(t91))
    dropped = 0
    for row in t['rows']:
        j = row.get('jesse')
        if j and j.get('rack') and dropped < 2:
            del j['rack']
            dropped += 1
    json.dump(t, open(path('inj_norack.json'), 'w'))
    r = run('check_transcription.py', path('inj_norack.json'))
    ok_check = r.returncode != 0 and 'no rack transcribed' in r.stdout
    if not ok_check:
        failures.append('injection: missing Jesse rack did not ERROR in the checker')
    r = run('author_gcg.py', path('inj_norack.json'), path('sol91.json'),
            '--out', path('inj_norack.gcg'))
    ok_author = r.returncode != 0 and not os.path.exists(path('inj_norack.gcg'))
    if not ok_author:
        failures.append('injection: author_gcg wrote a GCG despite missing Jesse racks')
    print('injection missing-rack:',
          'checker ERROR + author refused' if ok_check and ok_author else 'MISSED')

    # Leftover ("kept") racks must rebuild to the SAME full racks as the
    # explicit form — Jesse writes either one depending on how rushed he was.
    t = json.loads(json.dumps(t91))
    converted = 0
    for row in t['rows']:
        j = row.get('jesse')
        if not j or not j.get('rack') or entry_kind(j.get('entry')) != 'word':
            continue
        played = Counter(c.upper() for c in j['entry'] if not c.islower())
        left = Counter(j['rack'].upper()) - played
        if sum((played - Counter(j['rack'].upper())).values()):
            continue                      # play-through letters: skip, not a clean case
        j['kept'] = ''.join(sorted(left.elements()))
        del j['rack']
        converted += 1
    json.dump(t, open(path('inj_kept.json'), 'w'))
    r = run('check_transcription.py', path('inj_kept.json'), '--spec', path('s91_kept.json'))
    ok = r.returncode == 0
    if ok:
        r = run('author_gcg.py', path('inj_kept.json'), path('sol91.json'),
                '--out', path('inj_kept.gcg'))
        ok = r.returncode == 0 and norm_events(path('out91.gcg')) == norm_events(path('inj_kept.gcg'))
    if not ok:
        failures.append(f'leftover racks ({converted} rows) did not rebuild to the same '
                        f'GCG as the full-rack form:\n{r.stdout}{r.stderr}')
    print('leftover-rack round-trip:', f'ok ({converted} rows)' if ok else 'FAILED')

    # The round-trip above only reaches the degenerate kept="" case, because
    # neither ground-truth game carries real racks (that IS the #91/#92 defect).
    # So pin the non-empty case explicitly: QAT off a full AAINQRT rack is the
    # same turn as QAT keeping AINR, and must author identically.
    def author_with(jesse_cell_patch, tag):
        t = json.loads(json.dumps(t91))
        for row in t['rows']:
            j = row.get('jesse')
            if j and j.get('entry') == 'QAT':
                j.pop('rack', None)
                j.pop('kept', None)
                j.update(jesse_cell_patch)
        json.dump(t, open(path(f'kept_{tag}.json'), 'w'))
        r = run('author_gcg.py', path(f'kept_{tag}.json'), path('sol91.json'),
                '--out', path(f'kept_{tag}.gcg'))
        return r, path(f'kept_{tag}.gcg')

    r_full, f_full = author_with({'rack': 'AAINQRT'}, 'full')
    r_left, f_left = author_with({'kept': 'AINR'}, 'left')
    ok = (r_full.returncode == 0 and r_left.returncode == 0
          and norm_events(f_full) == norm_events(f_left)
          and any('AAINQRT' in l for l in norm_events(f_left)))
    if not ok:
        failures.append('non-empty leftover rack did not rebuild to the full rack '
                        f'AAINQRT:\n{r_left.stdout}{r_left.stderr}')
    print('leftover-rack (non-empty):', 'ok' if ok else 'FAILED')

    spec = json.load(open(path('s91.json')))
    words = [m['word'] for m in spec['moves']]
    i = words.index('REV')
    del spec['moves'][i]
    spec['moves'][-1].update(word='VEE', score=23, dir='H', row=14)
    spec['leftover'] = 'DV'
    json.dump(spec, open(path('inj_vee.json'), 'w'))
    r = run('otb_solver.py', path('inj_vee.json'))
    # The wrong reading hands James the V that was really Jesse's, so the board
    # would need a third V. Since the tile distribution became a SEARCH
    # constraint (2026-07-15) this is rejected outright instead of surviving as
    # a flagged-but-plausible solution — and the overdrawn tile names the very
    # one whose ownership was misjudged. Require the rejection AND the pointer:
    # the old assertion (a red-flag warning on a returned solution) was the
    # weaker guarantee that the real #91 run rationalised away.
    if 'NO SOLUTION' not in r.stdout or "overdrawn: {'V': 1}" not in r.stdout:
        failures.append('injection: wrong VEE endgame reading not rejected by the bag '
                        'constraint with an overdrawn-V pointer')
    print('injection wrong-VEE-endgame:', 'rejected + V pointer'
          if "overdrawn: {'V': 1}" in r.stdout else 'MISSED')

    json.dump({'moves': [{'player': 'JD', 'word': 'XQ', 'score': 36, 'dir': 'H', 'row': 8}],
               'leftover': ''}, open(path('inj_lex.json'), 'w'))
    r = run('otb_solver.py', path('inj_lex.json'))
    if 'invalid words' not in r.stdout:
        failures.append('injection: invalid word at exact score not lexicon-rejected')
    print('injection invalid-word:', 'rejected' if 'invalid words' in r.stdout else 'MISSED')

    # ---- 3. optional archive sweep ----
    if sweep_n:
        random.seed(7)
        files = (glob.glob(os.path.join(REPO, 'Tournament Games/*/*/*.gcg'))
                 + glob.glob(os.path.join(REPO, 'Practice Games/**/*.gcg'), recursive=True))
        sample = random.sample(files, min(sweep_n, len(files)))
        npass = 0
        for f in sample:
            r = run('verify_gcg.py', f)
            if r.returncode == 0:
                npass += 1
            else:
                out = r.stdout + r.stderr
                expected = (any(s in out for s in KNOWN_BAD_SUBSTRINGS)
                            or any(os.path.basename(f) == k for k in KNOWN_BAD_FILES))
                if not expected:
                    failures.append(f'sweep: unexpected verify failure on {f}:\n{out[:300]}')
        print(f'sweep: {npass}/{len(sample)} replay cleanly '
              f'(non-passing must be known-defective classes)')

    dt = time.time() - t0
    if failures:
        print(f'\nREGRESSION FAIL ({len(failures)} problem(s), {dt:.1f}s):')
        for f in failures:
            print(' -', f)
        sys.exit(1)
    print(f'\nREGRESSION PASS ({dt:.1f}s)')


if __name__ == '__main__':
    main()
