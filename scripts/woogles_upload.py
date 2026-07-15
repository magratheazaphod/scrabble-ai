#!/usr/bin/env python3
"""Upload a .gcg to woogles.io as an annotated game: preflight, import, verify
finished, add to a collection, post a comment. Codifies the upload tail of the
gcg-upload and otb-scrabble-upload skills so it never gets re-implemented ad hoc.

Usage:
  python3 scripts/woogles_upload.py game.gcg --lexicon CSW24 \
      [--challenge FIVE_POINT] \
      [--collection "James Curley practice games"] [--create-collection] \
      [--chapter "2026-07-12 - JD vs James Curley (Game 92)"] \
      [--comment "Reconstructed from ..."] \
      [--dry-run] [--cleanup]

- --lexicon is REQUIRED and cannot be changed after import (finished games
  cannot be deleted). Jesse's own games are ALWAYS CSW — the edition current
  at the time the game was played (e.g. CSW24). Short codes only ("CSW24",
  not "CSW2024").
- --dry-run: run the preflight and print what would happen; no API calls that
  create anything.
- --cleanup: if the account has stuck unfinished games (they block all
  imports), delete them first. Only unfinished games are deletable, so this
  cannot destroy a finished game.
- --collection must already exist unless --create-collection is also given
  (new collections are created public; confirm with Jesse for a new one).
- Exit 0 only when the game is imported AND verified finished server-side.

Auth: WOOGLES_API_KEY in .env at the repo root (same as tournament-analysis).
"""
import sys, os, json, argparse, subprocess

import requests

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE = 'https://woogles.io/api'


def load_env():
    path = os.path.join(REPO, '.env')
    if os.path.exists(path):
        for line in open(path):
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                k, v = line.split('=', 1)
                os.environ.setdefault(k, v.strip().strip('"').strip("'"))


def rpc(hdrs, service, body):
    r = requests.post(f'{BASE}/{service}', headers=hdrs, data=json.dumps(body), timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f'{service}: {r.status_code} {r.text[:300]}')
    return r.json()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('gcg')
    ap.add_argument('--lexicon', required=True)
    ap.add_argument('--challenge', default='FIVE_POINT')
    ap.add_argument('--collection')
    ap.add_argument('--create-collection', action='store_true')
    ap.add_argument('--chapter')
    ap.add_argument('--comment')
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--cleanup', action='store_true')
    ap.add_argument('--verify-warn-only', action='store_true',
                    help='downgrade a verify_gcg replay failure to a warning — ONLY for '
                         'historical files with known, documented defects (e.g. game #90)')
    args = ap.parse_args()

    if args.collection and not args.chapter:
        sys.exit('--collection requires --chapter')

    # 1. preflight (parser-breaking pattern scan)
    pf = subprocess.run([sys.executable, os.path.join(REPO, 'scripts', 'gcg_preflight.py'),
                         args.gcg, '--check'], capture_output=True, text=True)
    print(pf.stdout.strip())
    if pf.returncode != 0:
        sys.exit('preflight FAILED — heal the file first (gcg_preflight.py without --check '
                 'writes .healed.gcg copies); upload the healed content and notify Jesse '
                 'per the gcg-upload skill.')

    # 1b. independent replay verification (import is irreversible — this is the
    # last gate where a defective file can be stopped)
    verify = os.path.join(REPO, '.claude', 'skills', 'otb-scrabble-upload',
                          'scripts', 'verify_gcg.py')
    if os.path.exists(verify):
        v = subprocess.run([sys.executable, verify, args.gcg], capture_output=True, text=True)
        print(v.stdout.strip())
        if v.returncode != 0:
            if args.verify_warn_only:
                print('verify_gcg FAILED — proceeding anyway (--verify-warn-only).')
            else:
                sys.exit('verify_gcg FAILED — the file does not replay cleanly; fix it before '
                         'uploading. For a historical file with a known, documented defect, '
                         'rerun with --verify-warn-only.')
    else:
        print(f'WARNING: {verify} not found — skipping replay verification.')

    gcg_contents = open(args.gcg, encoding='utf-8').read()
    n_events = sum(1 for l in gcg_contents.splitlines() if l.startswith('>'))

    if args.dry_run:
        print(f"DRY RUN: would import {os.path.basename(args.gcg)} ({n_events} events) "
              f"with lexicon={args.lexicon}, challenge=ChallengeRule_{args.challenge}"
              + (f", then add to collection {args.collection!r} as {args.chapter!r}"
                 if args.collection else "")
              + (", then post a game comment" if args.comment else ""))
        return

    load_env()
    key = os.environ.get('WOOGLES_API_KEY')
    if not key:
        sys.exit('WOOGLES_API_KEY not set (expected in .env at repo root)')
    hdrs = {'Content-Type': 'application/json', 'X-Api-Key': key}

    # 2. clear stuck unfinished games (they block every import)
    unfinished = rpc(hdrs, 'omgwords_service.GameEventService/GetMyUnfinishedGames', {})
    stuck = unfinished.get('games', [])
    if stuck:
        if not args.cleanup:
            sys.exit(f'account has {len(stuck)} stuck unfinished game(s) '
                     f'({[g.get("game_id") for g in stuck]}) — they block imports. '
                     'Rerun with --cleanup to delete them (only unfinished games are deletable).')
        for g in stuck:
            rpc(hdrs, 'omgwords_service.GameEventService/DeleteAnnotatedGame',
                {'game_id': g['game_id']})
            print(f"deleted stuck unfinished game {g['game_id']}")

    # 3. import
    try:
        resp = rpc(hdrs, 'omgwords_service.GameEventService/ImportGCG', {
            'gcg': gcg_contents,
            'lexicon': args.lexicon,
            'rules': {'board_layout_name': 'CrosswordGame',
                      'letter_distribution_name': 'english',
                      'variant_name': 'classic'},
            'challenge_rule': f'ChallengeRule_{args.challenge}',
        })
    except RuntimeError as e:
        msg = str(e)
        if '.kwg' in msg or 'lexicon' in msg.lower():
            msg += "\n(a missing-.kwg 500 usually means a bad lexicon code — short form like CSW24)"
        if 'can only pass or challenge' in msg:
            msg += ("\n(partial-rack endgame pitfall — see otb-scrabble-upload SKILL.md; "
                    "the half-created game may now be stuck: rerun with --cleanup after fixing)")
        sys.exit(f'ImportGCG failed: {msg}')
    game_id = resp['game_id']
    print(f'imported: https://woogles.io/anno/{game_id}')

    # 4. verify finished server-side (GetGCG only returns for finished games)
    try:
        remote = rpc(hdrs, 'game_service.GameMetadataService/GetGCG', {'game_id': game_id})
        remote_events = sum(1 for l in remote.get('gcg', '').splitlines() if l.startswith('>'))
    except RuntimeError as e:
        sys.exit(f'game did NOT finish server-side (GetGCG failed: {e}) — it is stuck '
                 'unfinished and blocks future imports; fix the GCG and rerun with --cleanup.')
    if remote_events != n_events:
        sys.exit(f'event count mismatch: uploaded {n_events}, server replays {remote_events} — '
                 'investigate before trusting this game.')
    unfinished = rpc(hdrs, 'omgwords_service.GameEventService/GetMyUnfinishedGames', {})
    if unfinished.get('games'):
        sys.exit('import left an unfinished game behind — investigate.')
    print(f'verified finished ({remote_events} events replayed server-side)')

    # 5. collection
    if args.collection:
        cols = rpc(hdrs, 'collections_service.CollectionsService/GetUserCollections',
                   {'user_uuid': '', 'limit': 100, 'offset': 0}).get('collections', [])
        match = next((c for c in cols if c['title'] == args.collection), None)
        if not match:
            if not args.create_collection:
                sys.exit(f'collection {args.collection!r} not found — existing: '
                         f'{[c["title"] for c in cols]} (pass --create-collection to create it)')
            match = {'uuid': rpc(hdrs, 'collections_service.CollectionsService/CreateCollection',
                                 {'title': args.collection, 'description': '', 'public': True}
                                 )['collection_uuid']}
            print(f'created collection {args.collection!r}')
        rpc(hdrs, 'collections_service.CollectionsService/AddGameToCollection',
            {'collection_uuid': match['uuid'], 'game_id': game_id,
             'chapter_title': args.chapter, 'is_annotated': True})
        print(f'added to {args.collection!r} as {args.chapter!r}')

    # 6. comment
    if args.comment:
        rpc(hdrs, 'comments_service.GameCommentService/AddGameComment',
            {'game_id': game_id, 'event_number': 0, 'comment': args.comment})
        print('posted game comment (event 0)')

    print(f'DONE: {game_id}')


if __name__ == '__main__':
    main()
