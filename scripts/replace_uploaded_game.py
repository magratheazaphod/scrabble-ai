#!/usr/bin/env python3
"""Swap a re-uploaded game in for a defective one everywhere Woogles knows it.

Usage:
  python3 scripts/replace_uploaded_game.py --old <old_game_id> --new <new_game_id> \
      [--reason "partial racks — BestBot produced no per-player stats"] \
      [--no-comment] [--dry-run]

Why this exists: a game that analyzed successfully can never be re-analyzed
(force:true is honoured only for FAILED or legacy-v0 results), and a finished
game can never be deleted. So the only way to correct an uploaded game is to
upload a fresh copy under a new game_id and move everything that pointed at the
old one — and the old one stays up forever as a discoverable orphan. Doing that
by hand means several RPCs per collection and a comment that is easy to skip;
this codifies it. See /fix-uploaded-game.

What it does, per collection that holds the old game (found via
GetCollectionsForGame, so it catches collections you'd forgotten about):

  1. reads the old chapter's title, is_annotated flag, and position
  2. AddGameToCollection for the new id, reusing that title and flag
  3. RemoveGameFromCollection for the old id
  4. ReorderGames so the new chapter sits in the old one's slot
     (AddGameToCollection appends, so without this the chapter moves to the end)

Then it posts a comment on the OLD game pointing at the replacement, because the
old game cannot be deleted and someone will find it. --no-comment skips that,
but you have to mean it.

What it deliberately does NOT do:
  - the Curley tracker row. That belongs to update_curley_tracker.py, which owns
    the sheet; this script prints the exact command to run.
  - .github/ocr-game-manifest.txt. A one-line edit, and it must be committed
    alongside the corrected .gcg anyway.

Guards: refuses if the new game is not finished server-side (swapping in a stuck
or in-progress game would be worse than the defect), and warns if the new game's
analysis has already FAILED.

Auth: WOOGLES_API_KEY in .env at the repo root.
"""
import sys, os, json, argparse

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


def game_is_finished(hdrs, game_id):
    """(finished, detail). A stuck-unfinished game refuses to hand over history."""
    try:
        h = rpc(hdrs, 'game_service.GameMetadataService/GetGameHistory',
                {'game_id': game_id}).get('history', {})
    except RuntimeError as e:
        return False, str(e)
    state = h.get('play_state')
    # play_state is GAME_OVER (or absent on some historical records that do
    # return a full event list — treat a populated final_scores as proof).
    if state == 'GAME_OVER' or h.get('final_scores'):
        return True, f"play_state={state}, final_scores={h.get('final_scores')}"
    return False, f'play_state={state}'


def analysis_status(hdrs, game_id):
    try:
        r = rpc(hdrs, 'analysis_service.AnalysisService/GetAnalysisStatus',
                {'game_id': game_id})
        return r.get('status'), r.get('analysis_version')
    except RuntimeError:
        return None, None


def main():
    ap = argparse.ArgumentParser(
        description='Swap a re-uploaded game in for a defective one.')
    ap.add_argument('--old', required=True, help='the defective game_id (stays up as an orphan)')
    ap.add_argument('--new', required=True, help='the corrected game_id, already imported')
    ap.add_argument('--reason', help='one clause on what was wrong, used in the orphan comment')
    ap.add_argument('--no-comment', action='store_true',
                    help='skip the corrective comment on the old game')
    ap.add_argument('--dry-run', action='store_true', help='print the plan, change nothing')
    args = ap.parse_args()

    if args.old == args.new:
        sys.exit('--old and --new are the same game_id')

    load_env()
    key = os.environ.get('WOOGLES_API_KEY')
    if not key:
        sys.exit('WOOGLES_API_KEY not set (expected in .env at the repo root)')
    hdrs = {'Content-Type': 'application/json', 'X-Api-Key': key}

    # --- guards on the replacement -----------------------------------------
    ok, detail = game_is_finished(hdrs, args.new)
    if not ok:
        sys.exit(f'new game {args.new} is not finished server-side ({detail}).\n'
                 'Swapping in an unfinished game would replace one defect with a worse '
                 'one — and an unfinished game blocks all further imports. Fix it first.')
    print(f'new game {args.new}: finished ({detail})')

    status, version = analysis_status(hdrs, args.new)
    if status == 'FAILED':
        print(f'WARNING: the new game\'s analysis has already FAILED — the replacement '
              f'carries its own defect. Check GetAnalysisStatus error_message before '
              f'relying on this swap.', file=sys.stderr)
    elif status:
        print(f'new game analysis: {status}'
              + (f' (v{version})' if version is not None else ''))

    # --- find every collection holding the old game -------------------------
    resp = rpc(hdrs, 'collections_service.CollectionsService/GetCollectionsForGame',
               {'game_id': args.old})
    collections = resp.get('collections', []) or []
    if not collections:
        print(f'\nold game {args.old} is in no collection — nothing to swap.')
    else:
        print(f'\nold game {args.old} is in {len(collections)} collection(s):')

    for c in collections:
        uuid, title = c.get('uuid'), c.get('title')
        full = rpc(hdrs, 'collections_service.CollectionsService/GetCollection',
                   {'collection_uuid': uuid}).get('collection', {})
        games = full.get('games', []) or []
        order = [g.get('game_id') for g in games]
        if args.old not in order:
            print(f'  - {title}: old game not in its game list (already swapped?) — skipped')
            continue
        idx = order.index(args.old)
        old_entry = games[idx]
        chapter = old_entry.get('chapter_title') or ''
        annotated = bool(old_entry.get('is_annotated'))
        new_order = [args.new if g == args.old else g for g in order]

        print(f'  - {title}')
        print(f'      chapter {idx + 1}/{len(order)}: "{chapter}" (is_annotated={annotated})')
        if args.dry_run:
            print(f'      would add {args.new}, remove {args.old}, reorder into slot {idx + 1}')
            continue

        rpc(hdrs, 'collections_service.CollectionsService/AddGameToCollection',
            {'collection_uuid': uuid, 'game_id': args.new,
             'chapter_title': chapter, 'is_annotated': annotated})
        rpc(hdrs, 'collections_service.CollectionsService/RemoveGameFromCollection',
            {'collection_uuid': uuid, 'game_id': args.old})
        rpc(hdrs, 'collections_service.CollectionsService/ReorderGames',
            {'collection_uuid': uuid, 'game_ids': new_order})
        print(f'      swapped and restored to slot {idx + 1}')

    # --- corrective comment on the orphan -----------------------------------
    if args.no_comment:
        print('\n--no-comment: the old game is left with no pointer to its replacement.')
    else:
        reason = f' ({args.reason})' if args.reason else ''
        body = (f'Superseded{reason}. This upload cannot be corrected in place — a '
                f'finished game cannot be deleted and a completed analysis cannot be '
                f're-run — so it was re-uploaded. Use the corrected game instead: '
                f'https://woogles.io/anno/{args.new}')
        if args.dry_run:
            print(f'\nwould comment on {args.old}:\n  {body}')
        else:
            rpc(hdrs, 'comments_service.GameCommentService/AddGameComment',
                {'game_id': args.old, 'event_number': 0, 'comment': body})
            print(f'\ncommented on the superseded game {args.old}')

    # --- what the caller still owns ----------------------------------------
    print('\nStill to do by hand:')
    print(f'  - tracker row (Curley games only), repointing Game #<n> at the new id:')
    print(f'      python3 scripts/update_curley_tracker.py --gcg <corrected.gcg> \\')
    print(f'          --game-id {args.new} --game-num <n> --allow-score-mismatch')
    print(f'      then, once BestBot has analyzed it:')
    print(f'      python3 scripts/update_curley_tracker.py --enrich --game-id {args.new}')
    print(f'  - .github/ocr-game-manifest.txt: replace {args.old}')
    print(f'    with {args.new}, and commit it with the corrected .gcg')
    print(f'  - data/curley-enrich-terminal.txt: drop {args.old} if listed')
    return 0


if __name__ == '__main__':
    sys.exit(main())
