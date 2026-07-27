---
name: curley-tracker
description: Read or update the "Curley tracker" Google Sheet — Jesse's running log of every practice game against James Curley — including backfilling game ids, the per-player BestBot stats columns, and the recorded-score-vs-GCG-score semantics. Use whenever Jesse asks about the Curley tracker/spreadsheet, why stats cells are blank, adding a Curley game to the sheet, or backfilling archive games. The sheet is auto-maintained; most requests need one script call, not new code.
---

# Curley tracker sheet

One Google Sheet row per lifetime JD-vs-James-Curley practice game, keyed by
Woogles game id. **All read/write logic lives in
`scripts/update_curley_tracker.py`** - extend that script rather than writing
ad-hoc gspread code; it has the auth, header-alias map, 429 retry wrapper, and
formula-preserving row writer.

The sheet is auto-maintained. Most requests need one script call, not new code.

## Setup

- **Auth:** service account via `GOOGLE_SA_KEYFILE`; sheet id in `.env` as
  `CURLEY_TRACKER_SHEET_ID`. All Sheets calls must go through the script's
  `_retry()` - the per-minute read quota trips easily on batch work.
- **Collection:** "James Curley practice games", uuid
  `55b29df3-10fd-471b-9e87-135ed5bbb2f6`. Lexicon per `CLAUDE.md`: CSW21 before
  2025-01-01, CSW24 after.

## Layout (since 2026-07-16)

Sheet1 = A-J core (Game #, game id, date, recorded scores, formula cols), K =
free-text Notes (Jesse's margin notes - **never overwrite**), L-S = the 8 stats
columns. A separate **Summary** tab holds the averages/aggregates panel
(formulas referencing Sheet1).

Game-id cells are `=HYPERLINK("https://woogles.io/anno/<id>","<id>")` - clickable,
but every reader sees the bare id (formatted value = label). Writers must use the
script's `game_id_cell()` helper, never a bare id.

## Score semantics (per Jesse, 2026-07-15)

The "JD recorded score" / "James recorded score" columns hold the score **as kept
at the table**. They may legitimately differ from what the GCG replays to -
addition slips happen; the board is the arbiter, but for practice games the
recorded number stays. **Never "correct" these cells to match a GCG**, and never
treat a small sheet-vs-GCG difference as proof of a wrong game. W/L/T and combined
columns are formulas - never write them directly.

## Script modes

```bash
# phase 1 — new game, right after upload (writes date, scores, game id + formulas)
python3 scripts/update_curley_tracker.py --gcg <file> --game-id <id>

# archive backfill — row already hand-scored, write ONLY the game id into 'Game #' n.
# --allow-score-mismatch: sheet keeps its recorded scores when they differ from the GCG
python3 scripts/update_curley_tracker.py --gcg <file> --game-id <id> --game-num <n> \
    --allow-score-mismatch

# phase 2 — per-player BestBot stats for one game / every rowed game
python3 scripts/update_curley_tracker.py --enrich --game-id <id>
python3 scripts/update_curley_tracker.py --enrich-collection

# collection hygiene — reorder + retitle chapters to match the sheet
python3 scripts/sync_curley_collection.py [--dry-run]
```

## Collection order and chapter titles (per Jesse, 2026-07-22)

The collection mirrors the tracker: chapters ordered by **Game # ascending**, and
every chapter title is

```
Game #<n> - <YYYY-MM-DD> - <first player> vs <second player>
```

e.g. `Game #1 - 2022-11-03 - JD vs James Curley`, or
`Game #2 - 2022-11-03 - James Curley vs JD` when James opened.

Naming rules, all mandatory:

- Jesse is always **"JD"**, James is always **"James Curley"** (see `CLAUDE.md`).
- **Whoever moved first is named first.** Read it from the game's own history
  (`GetGameHistory`, the first event's `player_index`) - never assume JD.
- The date is the tracker's date column, which outranks any date already in a
  chapter title (the sheet is the record of when a game was played).

**Never hand-edit chapter titles or drag chapters around in the Woogles UI** - run
`sync_curley_collection.py`, which is idempotent: it calls `UpdateChapterTitle`
only for chapters whose title actually changes and `ReorderGames` only when the
order actually differs, and parks any collection game with no tracker row at the
end (loudly). Full pass ≈ 77 `GetGameHistory` calls, about a minute.

## Stats columns (phase 2)

8 columns - JD/James bingos, JD/James blanks, JD/James win% lost, JD/James
mistake index - pulled straight from the Woogles analysis API
(`GetAnalysisResult` + `GetGameHistory`, same calls as `/tournament-analysis`).

A player's four cells stay **blank unless their side was fully annotated**: game
over, `mistake_index` present, and a full 7-tile rack on every turn (short racks
only once the bag is empty). Woogles scores partially-known racks anyway, so a
non-null `mistake_index` alone is NOT sufficient. Photo-reconstructed OTB games
have real plays but incomplete racks and stay blank **by design** - don't "fix"
that.

Games not yet analyzed stay blank and are retried next run; a row with any stats
cell filled is considered done and never re-fetched.

## Automation

The daily `woogles-report.yml` workflow already runs `--enrich-collection` and
`sync_curley_collection.py`, so a newly uploaded or newly analyzed game fills in
within days with no manual polling. **Don't build a new cron for it** (see
`CLAUDE.md`). The OTB upload pipeline calls phase 1 and the consistency audit
itself (`/otb-scrabble-upload`).

## Known permanent gaps (as of 2026-07-16)

- **No game and no surviving file:** rows 13-18, 34, 81-83, 85, 86, 88.
- **Truncated Quackle exports** (cut off mid-game), can't be uploaded unless Jesse
  re-exports or reconstructs: games 26 and 43.
- **Repaired on upload** (rack/exchange defects fixed in the uploaded copy, repo
  file untouched, repair documented in a game comment): 5, 8, 11, 29, 35, 45, 63,
  66, 72, 78.
- Everything else 1-92 is uploaded and linked.

Rows 60, 92, and 93 are permanently un-enrichable: analyzed, but not fully
annotated (see the rack rule in `CLAUDE.md`). Correcting one means re-uploading
and repointing the row - see `/fix-uploaded-game`.
