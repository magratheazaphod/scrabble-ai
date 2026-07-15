#!/usr/bin/env python3
"""Audit tracker ↔ live-Woogles ↔ repo-file consistency for OCR-reconstructed
Curley games.

SCOPE (per Jesse, 2026-07-15): only games the /otb-scrabble-upload OCR
pipeline reconstructed from paper photos are audited — they are the ones
where a transcription/solver error can exist. Games uploaded by any other
means (Quackle self-exports, live Woogles play) are trusted as uploaded and
never audited. The audited set is the id list in .github/ocr-game-manifest.txt,
which the OCR pipeline appends to after each upload.

For each audited game (which must have a Curley-tracker row):
  1. fetch the live GCG (GetGCG) and replay it with verify_gcg's engine —
     the live game must replay cleanly;
  2. the live final scores must match the tracker's me/jc cells;
  3. if a repo file `Practice Games/James Curley/<Game #>_*.gcg` exists, its
     event sequence (position + play + score + cumulative) must match the
     live game's — nicknames and rack spellings are ignored (the server
     rewrites nicks; racks are multisets).

Motivation: game #90 carried a 10-point live-vs-placements inconsistency for
two days before an unrelated sweep caught it. Known, documented,
deliberately-kept defects are allowlisted below.

Usage:
  python3 scripts/audit_woogles_consistency.py
      sweep every game in the OCR manifest (the daily woogles-report workflow
      runs this last);
  python3 scripts/audit_woogles_consistency.py --game-id <id>
      audit one game — the OCR pipeline's final step, right after the upload,
      tracker phase-1 update, and manifest append.
Exit 0 = no unexpected discrepancies (allowlisted ones are reported as such).

Env: WOOGLES_API_KEY, GOOGLE_SA_KEYFILE, CURLEY_TRACKER_SHEET_ID (all as in
update_curley_tracker.py; .env at repo root is honored).
"""
import sys, os, json, glob, argparse, tempfile

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(REPO, '.claude', 'skills', 'otb-scrabble-upload', 'scripts'))
from update_curley_tracker import load_dotenv, open_worksheet, build_header_map
import verify_gcg as vg
from otb_solver import LEXICON_PATH, load_lexicon

MANIFEST_PATH = os.path.join(REPO, '.github', 'ocr-game-manifest.txt')

# game_id -> reason a replay failure / mismatch is expected and accepted
ALLOWLIST = {
    '9G2uCPfVaXKhXpT9tCR84w':
        'game #90: LAGGIER table scoring error, kept as-played per Jesse '
        '(2026-07-15, corrective comment posted; see skill known-issues.md)',
}


def fetch_gcg(hdrs, game_id):
    r = requests.post('https://woogles.io/api/game_service.GameMetadataService/GetGCG',
                      headers=hdrs, data=json.dumps({'game_id': game_id}), timeout=30)
    if r.status_code != 200:
        return None, f'GetGCG {r.status_code}: {r.text[:120]}'
    return r.json().get('gcg', ''), None


def event_signature(text):
    """(type, pos, play, score, cum) per event — nick- and rack-insensitive."""
    sig = []
    for e in vg.parse_events(text.splitlines()):
        sig.append((e['type'], e.get('pos', ''), e.get('play', e.get('exchanged', '')),
                    e.get('score'), e.get('cum')))
    return sig


def jesse_side(cums):
    """Split verify_gcg final cums into (jesse_score, opponent_score)."""
    je = [v for k, v in cums.items() if k.upper() == 'JD' or 'JESSE' in k.upper()]
    opp = [v for k, v in cums.items() if not (k.upper() == 'JD' or 'JESSE' in k.upper())]
    return (je[0] if je else None), (opp[0] if opp else None)


def load_manifest():
    """Game ids of OCR-reconstructed uploads — the only games ever audited."""
    ids = []
    if os.path.exists(MANIFEST_PATH):
        for line in open(MANIFEST_PATH):
            line = line.split('#', 1)[0].strip()
            if line:
                ids.append(line)
    return ids


def main():
    ap = argparse.ArgumentParser(description='Audit OCR-reconstructed Curley games.')
    ap.add_argument('--game-id',
                    help='audit only this game (the OCR pipeline runs this right '
                         'after upload + tracker update + manifest append); '
                         'default: sweep every game in the OCR manifest')
    args = ap.parse_args()

    load_dotenv(os.path.join(REPO, '.env'))
    key = os.environ.get('WOOGLES_API_KEY', '').strip()
    if not key:
        sys.exit('WOOGLES_API_KEY not set')
    hdrs = {'Content-Type': 'application/json', 'X-Api-Key': key}
    lex = load_lexicon() if os.path.exists(LEXICON_PATH) else None

    targets = [args.game_id] if args.game_id else load_manifest()
    if not targets:
        print(f'OCR manifest ({MANIFEST_PATH}) is empty or missing — nothing to audit.')
        return

    ws = open_worksheet()
    rows = ws.get_all_values()
    field_col, unresolved = build_header_map(rows[0])
    for need in ('game_id', 'jesse_score', 'opp_score'):
        if need not in field_col:
            sys.exit(f'tracker header missing column for {need!r} (unresolved: {unresolved})')
    gnum_col = next((i + 1 for i, h in enumerate(rows[0])
                     if h.strip().lower() in ('game #', 'game#', 'game number')), None)

    gid_col = field_col['game_id']
    tracker = {}  # game_id -> (row_index, row)
    for ridx, row in enumerate(rows[1:], start=2):
        rgid = row[gid_col - 1].strip() if gid_col <= len(row) else ''
        if rgid:
            tracker[rgid] = (ridx, row)

    issues, allowed, ok = [], [], 0
    for gid in targets:
        if gid not in tracker:
            issues.append(f'{gid}: no tracker row — run update_curley_tracker.py '
                          'phase 1 (or fix the OCR manifest entry)')
            continue
        ridx, row = tracker[gid]

        def cell(f):
            c = field_col.get(f)
            return row[c - 1].strip() if c and c <= len(row) else ''
        gnum = row[gnum_col - 1].strip() if gnum_col and gnum_col <= len(row) else ''
        label = f'row {ridx} (game #{gnum or "?"}, {gid})'

        gcg, err = fetch_gcg(hdrs, gid)
        if err:
            issues.append(f'{label}: cannot fetch live GCG — {err}')
            continue

        with tempfile.NamedTemporaryFile('w', suffix='.gcg', delete=False) as f:
            f.write(gcg)
            tmp = f.name
        try:
            errors, _warn, _tiles, cums = vg.verify(tmp, lex)
        except Exception as e:
            errors, cums = [f'replay crashed: {e}'], {}
        finally:
            os.unlink(tmp)

        problems = []
        if errors:
            problems.append(f'live game does not replay cleanly: {errors[0]}')
        je, opp = jesse_side(cums)
        me_s, jc_s = cell('jesse_score'), cell('opp_score')
        if me_s and je is not None and str(je) != me_s:
            problems.append(f'tracker me={me_s} but live final is {je}')
        if jc_s and opp is not None and str(opp) != jc_s:
            problems.append(f'tracker jc={jc_s} but live final is {opp}')

        if gnum:
            matches = glob.glob(os.path.join(REPO, 'Practice Games', 'James Curley',
                                             f'{gnum}_*.gcg'))
            if matches:
                if event_signature(open(matches[0]).read()) != event_signature(gcg):
                    problems.append(f'repo file {os.path.basename(matches[0])} events '
                                    'differ from the live game')

        if not problems:
            ok += 1
        elif gid in ALLOWLIST:
            allowed.append(f'{label}: {problems[0]} — ALLOWED ({ALLOWLIST[gid]})')
        else:
            issues.append(f'{label}: ' + '; '.join(problems))

    print(f'{ok} OCR game(s) fully consistent; {len(allowed)} allowlisted; '
          f'{len(issues)} issue(s).')
    for a in allowed:
        print('  allowed:', a)
    for i in issues:
        print('  ISSUE:', i)
    if issues:
        sys.exit(1)


if __name__ == '__main__':
    main()
