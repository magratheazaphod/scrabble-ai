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

Imports made by the /otb-scrabble-upload pipeline (--otb) are appended to
data/otb-upload-log.jsonl — a record of past pipeline RUNS, since a
photo-reconstructed game is the only kind whose fidelity is in question.
Normal uploads (Quackle exports, archive backfills) are trusted and NOT logged.
The record is written before any step that can fail, so superseded and stuck
uploads appear too — an ImportGCG is irreversible, so a game that exists must
be findable later even if the run died afterwards. See log_entry().
"""
import sys, os, json, argparse, subprocess, atexit, hashlib, re
from datetime import datetime, timezone

import requests

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE = 'https://woogles.io/api'
UPLOAD_LOG = os.path.join(REPO, 'data', 'otb-upload-log.jsonl')


def load_env():
    path = os.path.join(REPO, '.env')
    if os.path.exists(path):
        for line in open(path):
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                k, v = line.split('=', 1)
                os.environ.setdefault(k, v.strip().strip('"').strip("'"))


def rack_stats(gcg):
    """Per-player (turns, turns whose declared rack is a full 7 tiles).

    Racks written as played-tiles-only are the historical defect behind games
    #91/#92 (blank BestBot stats): the log records this so a partial-rack
    upload is visible without re-fetching the GCG.
    """
    stats = {}
    for line in gcg.splitlines():
        m = re.match(r'^>(\S+):\s+(\S+)\s', line)
        if not m or m.group(2).startswith('('):
            continue
        player, rack = m.group(1).rstrip(':'), m.group(2)
        t, f = stats.get(player, (0, 0))
        stats[player] = (t + 1, f + (len(rack) == 7))
    return {p: {'turns': t, 'full_racks': f} for p, (t, f) in stats.items()}


def log_entry(entry):
    """Append one record to the upload log. Registered via atexit so it fires
    on sys.exit() paths too — the log must never be less complete than reality."""
    entry['status'] = entry.get('status', 'incomplete')
    os.makedirs(os.path.dirname(UPLOAD_LOG), exist_ok=True)
    with open(UPLOAD_LOG, 'a', encoding='utf-8') as fh:
        fh.write(json.dumps(entry, sort_keys=True) + '\n')


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
    ap.add_argument('--otb', action='store_true',
                    help='this upload came from the /otb-scrabble-upload photo pipeline — '
                         'record it in data/otb-upload-log.jsonl. Only reconstructed games '
                         'are logged; normally-uploaded games are trusted and left out.')
    ap.add_argument('--game-number', type=int,
                    help='tracker game # (logged with --otb)')
    ap.add_argument('--photos', help='comma-separated source photo filenames (logged with --otb)')
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
    verify = os.path.join(REPO, 'scripts', 'verify_gcg.py')
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

    # 3b. log the import immediately — it is irreversible, so a pipeline run
    # gets recorded before verification, collection, or comment can fail and
    # abort it. Superseded reconstructions are exactly what this log is for:
    # three copies of game #91 reached Woogles and only two were written down
    # anywhere.
    entry = None
    if args.otb:
        entry = {
            'uploaded_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
            'game_number': args.game_number,
            'game_id': game_id,
            'url': f'https://woogles.io/anno/{game_id}',
            'gcg_file': os.path.relpath(os.path.abspath(args.gcg), REPO),
            'gcg_sha256': hashlib.sha256(gcg_contents.encode('utf-8')).hexdigest(),
            'photos': [p.strip() for p in args.photos.split(',')] if args.photos else None,
            'lexicon': args.lexicon,
            'challenge_rule': args.challenge,
            'events': n_events,
            'collection': args.collection,
            'chapter': args.chapter,
            'players': [l.split(None, 2)[1] for l in gcg_contents.splitlines()
                        if l.startswith('#player')],
            'rack_stats': rack_stats(gcg_contents),
            'status': 'imported',
        }
        atexit.register(log_entry, entry)

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

    if entry is not None:
        entry['status'] = 'complete'
    print(f'DONE: {game_id}')


if __name__ == '__main__':
    main()
