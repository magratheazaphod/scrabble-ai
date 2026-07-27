---
name: gcg-upload
description: Upload local .gcg tournament game files to woogles.io as annotated games and group them into a collection, via the Woogles Connect RPC API. Use this whenever Jesse asks to upload GCG files to Woogles, add tournament rounds to a Woogles collection, or mentions a folder of .gcg files that need to go up on his profile. Covers the ImportGCG/AddGameToCollection API calls, lexicon/challenge-rule pitfalls, and the GCG endgame-line gotcha that silently breaks the parser.
---

# Woogles.io GCG upload

Upload `.gcg` files (usually Quackle exports) to woogles.io as **annotated
games**, grouped into a **collection** named after the tournament, one chapter
per round.

Lexicon, challenge rule, API auth, and the irreversibility rules are in
`CLAUDE.md` - **read those before importing anything**, because an import can't
be undone. Parser gotchas, rack repair, the re-analysis rule, and raw API calls
for debugging: `reference/gcg-pitfalls.md`.

## Finding the files

Jesse's archive lives under `Tournament Games/<year>/<tournament name>/`.
**GitHub is canonical** - `git pull origin master` before reading (see
`CLAUDE.md`). If the working directory isn't a clone of `magratheazaphod/scrabble-ai`,
fetch from GitHub directly rather than trusting a local path.

Even when Jesse just names a tournament, look for a matching folder under
`Tournament Games/<year>/` before asking. Folder names don't always match the
canonical event name (e.g. "WESPAC '23" for "WESPAC 2023 (Las Vegas)", "WSC '18"
for "World Scrabble Championship 2018") - search by year and fuzzy name. If no
matching folder exists, **say so and ask** rather than silently substituting a
differently-named event.

Tournament folders also hold non-game files (`equity` is Jesse's per-round notes;
also `words`, `toughies`, `stats`). Never upload one - a real GCG starts with a
`#player1`/`#character-encoding` header followed by `>Name:` move lines.

## Step 1 - preflight (always, no exceptions)

```bash
python3 scripts/gcg_preflight.py "Tournament Games/<year>/<tournament>"
```

Detects every known parser-breaking pattern and writes auto-healed copies as
`<name>.healed.gcg` (originals untouched; `--in-place` heals originals with a
`.bak`; `--check` reports only, exit 1 if anything needs attention). Upload the
`.healed.gcg` where one was written, the original otherwise, and delete the
`.healed.gcg` files afterwards rather than committing them.

It also **FLAGS (without healing) unterminated games** - files that don't end in
endgame bonus/penalty lines, typically abandoned transcriptions ending at a
`#rack` line. **Never upload a flagged-unterminated file**: it imports as a stuck
unfinished game that blocks all further imports on the account until deleted.

## Step 2 - upload

```bash
python3 scripts/woogles_upload.py "path/to/game.gcg" --lexicon CSW21 \
    --collection "Austin One-Day Aug '23" --chapter "Round 4 - JD vs Becky Dyer" \
    [--comment "..."] [--create-collection] [--dry-run] [--cleanup]
```

One script does the whole tail: preflight → `ImportGCG` → verify the game
finished server-side → collection add → comment. It refuses to import when
preflight fails or when the independent replay verifier (`verify_gcg.py`) finds
the file doesn't replay cleanly - for a historical file with a known, documented
defect, pass `--verify-warn-only`.

- `--create-collection` is required to create a new one; **confirm public/private
  with Jesse for a brand-new collection** (existing ones default to public).
- `--cleanup` deletes stuck unfinished games (it cannot touch finished ones).
- Chapter titles: one consistent convention per tournament,
  `Round N - JD vs <Opponent>`. For the Curley collection, follow the tracker
  convention instead (`/curley-tracker`).

## Step 3 - verify

`GetCollection` and check `game_count` matches the rounds uploaded and that each
`games[].chapter_title` is right. Spot-check a couple of
`https://woogles.io/anno/<game_id>` pages render a full board, not a blank one.

## Notifying Jesse of healed games (mandatory)

Whenever a game is uploaded from healed content, Jesse wants to know so he can
review it. **Both channels are required:**

1. **A comment on the game itself**, via
   `comments_service.GameCommentService/AddGameComment` with `event_number: 0`,
   describing *what* changed (use the scanner's per-file output) so the edit can
   be audited against the original in the repo:
   > Note: original GCG required automated repair before upload - moved trailing
   > "(challenge) +5" line before the final play to work around a Woogles import
   > bug (true final score preserved). Original file: "Austin '23 Rd 5 David
   > Whitley.gcg".
2. **A "Healed games" section in the end-of-run summary** to Jesse: one line per
   healed game with round, opponent, what was healed, and the game link.

Silent healing is not acceptable. If a comment can't be posted, say so explicitly
in the summary.

## When a round can't be uploaded

If a file won't parse cleanly and you decide to skip that round rather than guess
at a fix, **do not put the skip note in the next game's `chapter_title`.** Keep
every chapter title clean and record the note as a comment on the next game you
do upload:

```python
requests.post(f'{BASE}/comments_service.GameCommentService/AddGameComment',
    headers=HDRS, timeout=30,
    data=json.dumps({
        'game_id': next_game_id,   # the next round's game_id, once imported
        'event_number': 0,          # attaches before the first move
        'comment': 'Round 4 vs Prince Omosefe skipped - GCG parsing issue',
    }))
```

`GetGameComments` reads them back. Confirmed working 2026-07-03.

## Ask Jesse rather than guessing

- An endgame line format you haven't seen before (not a clean going-out or
  six-scoreless-turns ending).
- A tournament whose lexicon or CSW era isn't obvious from context - it can't be
  changed after import and the game can't be deleted or hidden once finished.
- An auth error from `ImportGCG` or `AddGameToCollection`: check `WOOGLES_API_KEY`
  is set and current in `.env`.
