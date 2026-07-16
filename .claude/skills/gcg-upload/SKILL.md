---
name: gcg-upload
description: Upload local .gcg tournament game files to woogles.io as annotated games and group them into a collection, via the Woogles Connect RPC API. Use this whenever Jesse asks to upload GCG files to Woogles, add tournament rounds to a Woogles collection, or mentions a folder of .gcg files that need to go up on his profile. Covers the ImportGCG/AddGameToCollection API calls, lexicon/challenge-rule pitfalls, and the GCG endgame-line gotcha that silently breaks the parser.
---

# Woogles.io GCG Upload

Jesse (woogles.io username `Magrathean`) keeps tournament games as `.gcg` files (e.g. exported from Quackle) and wants them uploaded to woogles.io as **annotated games**, then grouped into a **collection** named after the tournament (e.g. "Austin One-Day Aug '23"), one chapter per round.

Upload this directly via the Woogles Connect RPC API with `requests` — no browser needed. Auth is `X-Api-Key: $WOOGLES_API_KEY` (stored in `.env` at project root, gitignored), same pattern as `tournament-analysis`. Confirmed working end-to-end 2026-07-01.

## Where to find the files

Jesse's full tournament game archive (2010–2025) lives under `Tournament Games/<year>/<tournament name>/` in the `magratheazaphod/scrabble-ai` GitHub repo. **As of 2026-07-06, Jesse's explicit preference is that GitHub is the canonical source** — not whatever happens to be sitting in the local working directory. This project's working directory is normally a clone of that same repo (check `git remote -v` shows `magratheazaphod/scrabble-ai`), so in practice: confirm `git status` is clean, then `git pull origin master` before reading any files under `Tournament Games/` for an upload, so you're never working off a stale local mirror. If the working directory isn't a clone of that repo, fetch the files from GitHub directly instead of trusting a local path.

Even if Jesse just names a tournament without saying where the files are, look for a matching folder under `Tournament Games/<year>/` before asking. Folder names don't always match a tournament's full/canonical name (e.g. "WESPAC '23" for "WESPAC 2023 (Las Vegas)", "WSC '18" for "World Scrabble Championship 2018") — search by year and fuzzy name match. As of 2026-07-03 all actual game files in the archive carry a `.gcg` extension; if you ever find one that doesn't, treat it the same as the others (content, not extension, is what matters). If no matching folder exists for what Jesse describes, say so and ask rather than guessing at a substitute (don't silently pick a differently-named event as a stand-in).

**Every tournament folder typically also has a file named `equity`** (no extension) — this is Jesse's personal notes file for that tournament (mistake/decision commentary per round), not a game. It never carries a `.gcg` extension and should never be uploaded or treated as a game file. Same goes for other non-game files that sometimes appear alongside the rounds (`words`, `toughies`, `stats`, tough-word lists) — a real GCG game file always starts with a `#player1`/`#character-encoding` header line followed by `>Name: ...` move lines; `equity` and its siblings don't look like that.

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

## Preferred path: the scripted uploader

As of 2026-07-15 the whole upload tail (preflight → ImportGCG → verify
finished server-side → collection add → comment) is one script — use it
instead of re-implementing the `requests` calls below:

```bash
python3 scripts/woogles_upload.py "path/to/game.gcg" --lexicon CSW21 \
    --collection "Austin One-Day Aug '23" --chapter "Round 4 - JD vs Becky Dyer" \
    [--comment "..."] [--create-collection] [--dry-run] [--cleanup]
```

It refuses to import when preflight fails (heal first, per below) or when the
independent replay verifier (`verify_gcg.py`) finds the file doesn't replay
cleanly — for a historical file with a known, documented defect pass
`--verify-warn-only`. It also verifies the game actually finished (a
stuck-unfinished game blocks all future imports), only creates a collection
when `--create-collection` is passed
(confirm public/private with Jesse for a brand-new one), and `--cleanup`
deletes stuck unfinished games (it cannot touch finished ones). The sections
below remain the reference for lexicon choice, healing rules, and manual
debugging.

## Before uploading: ALWAYS run the pre-flight scanner first

```bash
python3 scripts/gcg_preflight.py "Tournament Games/<year>/<tournament>"
```

It detects every known parser-breaking pattern and writes auto-healed copies as `<name>.healed.gcg` (originals untouched; `--in-place` heals originals, saving a `.bak`; `--check` reports only, exit 1 if anything needs attention). Upload the `.healed.gcg` content where one was written, the original otherwise; delete `.healed.gcg` files afterwards rather than committing them. It also FLAGS (without healing) **unterminated games** — files that don't end in endgame bonus/penalty lines, typically abandoned transcriptions ending in a `#rack` line. Never upload a flagged-unterminated file: it imports as a stuck unfinished game that blocks all further `ImportGCG` calls until deleted. A 2026-07-06 full-archive scan: 2,421 files → 2,296 clean, 45 auto-healed (31 challenge-before-final-bonus + play-through rewrites), 80 flagged unterminated (incl. the 20 casual 2010 blitz files, which additionally omit cumulative scores and won't parse at all).

The heals aren't just pattern-verified: all healed challenge-bug files were replayed through the actual server pipeline (`gcgio.ParseGCGFromReader` + the ImportGCG dummy-pass logic + `cwgame.ReplayEvents` from the local liwords repo, driven by a throwaway Go harness) — 29/31 reach `GAME_OVER` with final scores exactly matching the GCG; the other 2 (`Manhattan Mar '19 Rd 3 Kurt`, `Niagara '18 Rd 1 Caroline Polak Scowcroft`) have pre-existing rack/tile transcription defects that fail identically before healing and need manual correction.

Live-verified findings (2026-07-06), superseding some earlier notes below:

- **`+-N` scores on end-rack lines are fine** (e.g. `>JD: X (X) +-8 463`) — the parser normalizes them; no fix needed.
- **Lowercase coordinates and column-aligned whitespace are fine** — the parser is case- and whitespace-tolerant, and tolerates trailing annotation text after the cumulative score.
- **Literal play-through letters are NOT fine**: transcriptions that write the whole word instead of using `.` for tiles already on the board fail with `"tried to play through a letter already on the board"`. The scanner heals this via board simulation.
- **Best heal for the challenge-before-final-bonus bug** (details in the gotcha section below): *move* the trailing `(challenge) +N` line to just before that player's final play, adjusting cumulatives — the game then finishes properly with the true final score. Do NOT fold the points into the final bonus line: the server *recomputes* end-rack points from the leftover tiles and silently discards the extra (verified — a folded `+17` came back as `+12`, final score 415 instead of the true 420). The scanner applies the reorder heal automatically.
- **Stuck-unfinished games ARE deletable**: `DeleteAnnotatedGame` succeeds on a game stuck by the challenge bug — only *finished* games are undeletable. If you hit the bug, delete the stuck game_id immediately and re-import healed content.
- **Delete-probe trick**: calling `DeleteAnnotatedGame` on a game tells you its state — `400 "you cannot delete a game that is already done"` means it finished properly. Never probe a game you aren't willing to lose: if it's unfinished, the probe deletes it.

### Racks with more than 7 tiles — reconstruct from the play

A `>Player: RACK POS WORD +score cum` line whose rack field has **more than 7 tiles** is a transcription error (extra letters typed into the rack). It imports fine but the analysis worker rejects it — `GetAnalysisStatus` returns `FAILED` with `error_message` like `turn N: rack "ADEEEILRSXY" has 11 tiles, max is 7`, which then blocks that game from ever completing (and, since the report pipeline defers a collection until every game is analysis-complete, silently drops the whole collection from the daily report — see the parent project's report-collection-jam notes).

**The played tiles are always a subset of the true rack, so the play tells you what the rack should have been.** The cleanest case: when that turn's move is a **bingo that uses all 7 tiles** (a `TILE_PLACEMENT_MOVE` whose word has exactly 7 letters placed — no `.` playthroughs — and no lowercase blanks), the rack is *exactly* the tiles of the played word. Real example (King's Cup 2019 Rd 8 vs Hubert Wee, confirmed 2026-07-16): `>Hubert_Wee: ADEEEILRSXY 3G DEISEAL +94 469` — the play `DEISEAL` is a clean 7-tile bingo, so his rack could only have been its tiles, `ADEEILS`. Correcting the rack field to `ADEEILS` (leaving move, score, and cumulative untouched) makes the game analyze.

Caveats when the play is *not* a full 7-tile bingo: the rack still must contain every non-`.` tile of the word (with lowercase = a blank `?`), but the remaining leftover tiles can't be recovered from the play alone — reconstruct those from the next turn's rack / bag state, or ask Jesse. Only ever edit the rack field; never touch the move, score, or cumulative. This defect isn't auto-healed by `gcg_preflight.py` (it's caught server-side at analysis time, not at parse time), so fix it by hand.

After correcting an already-uploaded game in place, its cached `FAILED` analysis result stays stale until you re-run analysis with `RequestAnalysis {..., "force": true}` (plain `force:false` returns the cached failure without re-running).

### Jesse must be notified of every healed game (mandatory)

Whenever a game is uploaded from healed content (i.e. the scanner changed anything about the file), Jesse wants to know so he can review it. Two channels, both required:

1. **A comment on the game itself**, via `comments_service.GameCommentService/AddGameComment` with `event_number: 0` (same call as in the skipped-round section below), e.g.:
   `Note: original GCG required automated repair before upload — moved trailing "(challenge) +5" line before the final play to work around a Woogles import bug (true final score preserved). Original file: "Austin '23 Rd 5 David Whitley.gcg".`
   Describe *what* was changed (use the scanner's per-file output) so the edit can be audited against the original file in the repo.
2. **A "Healed games" section in the end-of-run summary** to Jesse: one line per healed game with round, opponent, what was healed, and the `https://woogles.io/anno/<game_id>` link.

Silent healing is not acceptable — if for some reason a comment can't be posted, say so explicitly in the summary.

## Background: the endgame-line gotchas (all detected by the scanner)

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

**A third, separate gotcha (confirmed 2026-07-03, WESPAC 2023 Round 13):** if the going-out bonus line is immediately preceded by a mid-game `(challenge) +N` bonus line (i.e. the last event before the final bonus is a successful challenge, not a normal play), `ImportGCG` returns 200 with a `game_id`, but the game is server-side broken — it gets stuck permanently "unfinished" (`GetGameHistory` returns `"please wait until the game is over to download GCG"`, and it blocks all further `ImportGCG` calls on the account with `"please finish or delete your unfinished games before starting a new one"` until deleted via `DeleteAnnotatedGame`). This is a genuine bug in liwords' `ImportGCG` handler (`pkg/omgwords/service.go`, the dummy-terminal-pass-event insertion is skipped specifically when the event before `END_RACK_PTS` is `CHALLENGE_BONUS`), not something fixable by editing lexicon/rules params. Workaround: use the reorder heal described above (applied automatically by `scripts/gcg_preflight.py`) — the older fold-the-points-into-the-final-bonus approach loses the challenge points because the server recomputes end-rack points, so don't use it anymore. If you hit `"game not found"` (`400`) calling `RequestAnalysis`/`GetAnalysisStatus` on a game_id that's listed in a collection, check `GetGameHistory` for `"no rows in result set"` — that means the game record doesn't exist in Woogles' DB at all (likely a remnant of this exact bug from an earlier upload attempt that was never cleaned up), and the collection entry should be removed and replaced once a working game_id exists.

## When a round can't be uploaded (parsing failure)

If a `.gcg` file won't parse cleanly (endgame-line gotcha above, or a non-standard construct you can't confidently fix — e.g. a Quackle-exported `(challenge)` bonus line that isn't part of the two documented endgame formats) and you decide to skip that round rather than guess at a fix, **do not put the skip note in the next game's `chapter_title`.** Keep every `chapter_title` clean (`Round N - JD vs Player`) and instead record the note as a comment on the next game you do upload, via `comments_service.GameCommentService/AddGameComment`:

```python
resp = requests.post(
    f'{BASE}/comments_service.GameCommentService/AddGameComment',
    headers=HDRS,
    data=json.dumps({
        'game_id': next_game_id,   # the next round's game_id, once it's been imported
        'event_number': 0,          # attaches before the first move
        'comment': 'Round 4 vs Prince Omosefe skipped - GCG parsing issue',
    }),
    timeout=30,
)
```

This keeps collection chapter titles readable while still leaving a durable, in-context note on the game that follows the gap. (Confirmed working 2026-07-03; `GetGameComments`/`{'game_id': ...}` reads them back.)

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
- If `ImportGCG` or `AddGameToCollection` returns an auth error, check that `WOOGLES_API_KEY` is set and current in `.env` — same key used by `tournament-analysis`.
