---
name: woogles-gcg-upload
description: Upload local .gcg tournament game files to woogles.io as annotated games and group them into a collection, via the Woogles Connect RPC API. Use this whenever Jesse asks to upload GCG files to Woogles, add tournament rounds to a Woogles collection, or mentions a folder of .gcg files that need to go up on his profile. Covers the ImportGCG/AddGameToCollection API calls, lexicon/challenge-rule pitfalls, and the GCG endgame-line gotcha that silently breaks the parser.
---

# Woogles.io GCG Upload

Jesse (woogles.io username `Magrathean`) keeps tournament games as local `.gcg` files (e.g. exported from Quackle) and wants them uploaded to woogles.io as **annotated games**, then grouped into a **collection** named after the tournament (e.g. "Austin One-Day Aug '23"), one chapter per round.

Upload this directly via the Woogles Connect RPC API with `requests` — no browser needed. Auth is `X-Api-Key: $WOOGLES_API_KEY` (stored in `.env` at project root, gitignored), same pattern as `woogles-tournament-analysis`. Confirmed working end-to-end 2026-07-01.

```python
import os, json, requests

API_KEY = os.environ['WOOGLES_API_KEY']  # load from .env at project root (gitignored)
HDRS = {'Content-Type': 'application/json', 'X-Api-Key': API_KEY}
BASE = 'https://woogles.io/api'
```

## Jesse's standing defaults — always apply these

- **Lexicon: Jesse always plays CSW, never NWL.** GCG files don't encode a lexicon, so you must pick one explicitly in the `ImportGCG` request. **Watch for "JD" in `#player` lines — that's Jesse Day's own initials, not a stranger.** Don't assume a game is someone else's (and therefore safe to use a different lexicon on) just because the recorded name isn't "Jesse" or "Magrathean".
- **Use the CSW edition current at the time of the tournament**, not today's. E.g. a tournament from August 2023 should use `CSW21` (the CSW edition in force at that time), not whatever the newest CSW edition is now. Ask Jesse if the right historical edition isn't obvious from context. Lexicon codes are short forms like `CSW21`, `CSW24`, `NWL20`, `NWL23` — not `CSW2021`/`NWL2023` (those long forms cause a 500 with a "lexicon file not found" error).
- **Challenge rule: always `ChallengeRule_FIVE_POINT`** (CSW tournaments use the 5-point challenge rule).
- **The lexicon and challenge rule CANNOT be edited after the game is created, and finished games CANNOT be deleted via the API** (`DeleteAnnotatedGame` returns `"you cannot delete a game that is already done"` for any completed game). There is also no working way to hide a wrongly-created game — `SetAnnotatedGamePrivacy` is currently a no-op stub on the server. **Get the lexicon right before calling `ImportGCG`** — there is no clean undo. If genuinely unsure, ask Jesse rather than guessing.

## Before uploading: check the GCG file for the endgame-line gotcha

Read each `.gcg` file before uploading it. GCG files end one of two ways, and **the server-side parser (`gcgio.ParseGCGFromReader`, same code whether via API or the old web form) rejects a file if these are confused** (fails with an opaque `invalid_argument` error, or — worse — silently creates a blank game with no board):

1. **Going-out bonus** (one player plays out their rack, the other is left with tiles): the line has an **empty rack field** (two spaces after the colon), then the opponent's leftover tiles in parentheses, then a **positive** score:
   ```
   >Becky_Dyer:  (CFLO) +18 437
   ```
2. **Six-consecutive-scoreless-turns penalty** (no one goes out; the game ends because both players passed/scored zero six times in a row): each player loses points for their own leftover rack. Here the rack field is **populated** (repeated), then the same tiles in parentheses, then a **negative** score:
   ```
   >JD: L (L) -1 524
   >Becky_Dyer: Q (Q) -10 452
   ```

These two formats are NOT interchangeable — emptying the rack field on a penalty line (or vice versa) breaks the upload. If a file ends with several `>Player: X -  +0 <cum>` zero-score lines (six in a row, alternating players), expect it needs the penalty format above for its final two lines. If it ends with a normal play followed by one bonus line per the first pattern, no edit is needed. When in doubt, check the authoritative spec at https://www.poslfit.com/scrabble/gcg/.

If you have to edit a file to fix this, only touch the rack field and parenthetical/sign — don't otherwise alter scores or moves.

## Step 1: Import the game (create the annotated game)

`POST {BASE}/omgwords_service.GameEventService/ImportGCG`

```python
with open(gcg_path) as f:
    gcg_contents = f.read()

resp = requests.post(
    f'{BASE}/omgwords_service.GameEventService/ImportGCG',
    headers=HDRS,
    data=json.dumps({
        'gcg': gcg_contents,
        'lexicon': 'CSW21',  # short form, era-appropriate — see defaults above
        'rules': {
            'board_layout_name': 'CrosswordGame',
            'letter_distribution_name': 'english',
            'variant_name': 'classic',
        },
        'challenge_rule': 'ChallengeRule_FIVE_POINT',
    }),
    timeout=30,
)
resp.raise_for_status()
game_id = resp.json()['game_id']
```

Notes:
- `gcg` field is capped at 128,000 bytes server-side (`InvalidArg` if exceeded — not a concern for a single game's GCG).
- On success, response is `{"game_id": "<uuid-like string>"}`. The game is viewable at `https://woogles.io/anno/<game_id>`.
- A `500` mentioning a missing `.kwg` file almost always means the lexicon code is wrong (e.g. `NWL2023` instead of `NWL23`) — fix the code, don't retry blindly.
- A blank/empty board at the resulting URL means the GCG didn't parse cleanly — re-check the endgame-line format above before re-importing (as a brand new game; the broken one can't be deleted once "done").

## Step 2: Find or create the tournament's collection

`POST {BASE}/collections_service.CollectionsService/GetUserCollections` (empty `user_uuid` returns the authenticated user's own collections):

```python
resp = requests.post(
    f'{BASE}/collections_service.CollectionsService/GetUserCollections',
    headers=HDRS,
    data=json.dumps({'user_uuid': '', 'limit': 100, 'offset': 0}),
    timeout=30,
)
collections = resp.json().get('collections', [])
match = next((c for c in collections if c['title'] == tournament_title), None)
```

If no match, create it:

```python
resp = requests.post(
    f'{BASE}/collections_service.CollectionsService/CreateCollection',
    headers=HDRS,
    data=json.dumps({'title': tournament_title, 'description': '', 'public': True}),
    timeout=30,
)
collection_uuid = resp.json()['collection_uuid']
```

(Confirm the public/private choice with Jesse if not already established for this tournament — existing collections default to `public: True` per past uploads, but don't assume silently for a brand-new tournament.)

## Step 3: Add the game to the collection

`POST {BASE}/collections_service.CollectionsService/AddGameToCollection`

```python
resp = requests.post(
    f'{BASE}/collections_service.CollectionsService/AddGameToCollection',
    headers=HDRS,
    data=json.dumps({
        'collection_uuid': collection_uuid,
        'game_id': game_id,
        'chapter_title': f'Round {round_num} - {player_a} vs {player_b}',
        'is_annotated': True,
    }),
    timeout=30,
)
resp.raise_for_status()
```

Use a consistent chapter-title naming convention across the tournament, e.g. `Round 4 - JD vs Becky Dyer`. `AddGameToCollectionResponse` is empty on success — a non-2xx status or a JSON `code`/`message` body means it failed (e.g. `permission_denied` if the collection isn't owned by the authenticated user).

## Step 4: Verify

`POST {BASE}/collections_service.CollectionsService/GetCollection` with `{'collection_uuid': collection_uuid}` and check `game_count` matches the number of rounds uploaded, and that each `games[].chapter_title` is correct. Spot check a couple of the resulting `https://woogles.io/anno/<game_id>` pages render a full board (not blank).

## Notes / open questions to flag to Jesse if encountered

- A GCG file with an endgame line format Claude hasn't seen before (not a clean going-out or six-scoreless-turns ending) — don't guess, ask Jesse or check the spec.
- A file whose lexicon/era isn't obvious (tournament may have used a lexicon other than CSW, or an edition Jesse hasn't specified) — confirm before calling `ImportGCG`, since it can't be changed afterward and the game can't be deleted or hidden once finished.
- If `ImportGCG` or `AddGameToCollection` returns an auth error, check that `WOOGLES_API_KEY` is set and current in `.env` — same key used by `woogles-tournament-analysis`.
