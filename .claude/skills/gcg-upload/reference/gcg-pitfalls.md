# GCG parser and analysis pitfalls

Everything here is live-verified against woogles.io. `scripts/gcg_preflight.py`
detects and (where safe) heals all of the *parser* problems automatically; the
*analysis* problems are caught server-side later and must be fixed by hand.

## Endgame line formats

The server-side parser (`gcgio.ParseGCGFromReader`, same code via API or the old
web form) rejects a file if these two endings are confused - failing with an
opaque `invalid_argument`, or worse, silently creating a blank game with no
board.

1. **Going-out bonus** (one player plays out, the other is left with tiles): the
   line has an **empty rack field** (two spaces after the colon), the opponent's
   leftover tiles in parentheses, then a **positive** score:
   ```
   >Becky_Dyer:  (CFLO) +18 437
   ```
2. **Six-consecutive-scoreless-turns penalty** (nobody goes out): the rack field
   is **populated** (repeated), then the same tiles in parentheses, then a
   **negative** score:
   ```
   >JD: L (L) -1 524
   >Becky_Dyer: Q (Q) -10 452
   ```

These are NOT interchangeable. If a file ends with six alternating
`>Player: X -  +0 <cum>` zero-score lines, expect it needs the penalty format.
If it ends with a normal play followed by one bonus line per the first pattern,
no edit is needed. Authoritative spec: <https://www.poslfit.com/scrabble/gcg/>.

When editing, only touch the rack field and the parenthetical/sign - never the
scores or moves.

## The challenge-before-final-bonus bug

If the going-out bonus line is immediately preceded by a mid-game
`(challenge) +N` bonus line, `ImportGCG` returns 200 with a `game_id` but the
game is server-side broken: permanently stuck "unfinished" (`GetGameHistory`
returns "please wait until the game is over to download GCG"), and it **blocks
all further `ImportGCG` calls on the account** with "please finish or delete your
unfinished games before starting a new one" until deleted.

This is a genuine liwords bug (`pkg/omgwords/service.go` - the dummy-terminal-pass
insertion is skipped specifically when the event before `END_RACK_PTS` is
`CHALLENGE_BONUS`), not fixable via lexicon/rules params. Confirmed 2026-07-03,
WESPAC 2023 Round 13.

**Correct heal (applied automatically by the preflight scanner):** *move* the
trailing `(challenge) +N` line to just before that player's final play, adjusting
cumulatives. The game then finishes with the true final score.

**Do NOT fold the points into the final bonus line.** The server *recomputes*
end-rack points from the leftover tiles and silently discards the extra -
verified: a folded `+17` came back as `+12`, final 415 instead of the true 420.

If you hit `"game not found"` (400) on `RequestAnalysis`/`GetAnalysisStatus` for
a game_id that *is* listed in a collection, check `GetGameHistory` for "no rows in
result set" - that means the record doesn't exist in Woogles' DB at all, a
remnant of this bug from an earlier upload attempt. Remove the collection entry
and replace it once a working game_id exists.

## Live-verified preflight findings (2026-07-06)

- **`+-N` scores on end-rack lines are fine** (e.g. `>JD: X (X) +-8 463`) - the
  parser normalizes them.
- **Lowercase coordinates and column-aligned whitespace are fine** - the parser
  is case- and whitespace-tolerant, and tolerates trailing annotation text after
  the cumulative score.
- **Literal play-through letters are NOT fine.** Transcriptions writing the whole
  word instead of using `.` for tiles already on the board fail with "tried to
  play through a letter already on the board". The scanner heals this via board
  simulation.
- **Stuck-unfinished games ARE deletable.** Only *finished* games are
  undeletable. Delete a stuck game_id immediately and re-import healed content.
- **Delete-probe trick:** calling `DeleteAnnotatedGame` tells you a game's state -
  `400 "you cannot delete a game that is already done"` means it finished
  properly. **Never probe a game you aren't willing to lose**: if it's unfinished,
  the probe deletes it.

Full-archive scan, 2026-07-06: 2,421 files → 2,296 clean, 45 auto-healed (31
challenge-before-final-bonus + play-through rewrites), 80 flagged unterminated
(including the 20 casual 2010 blitz files, which also omit cumulative scores and
won't parse at all).

The heals are more than pattern-verified: all healed challenge-bug files were
replayed through the actual server pipeline (`gcgio.ParseGCGFromReader` + the
ImportGCG dummy-pass logic + `cwgame.ReplayEvents` from the local liwords repo,
via a throwaway Go harness). 29/31 reach `GAME_OVER` with final scores exactly
matching the GCG; the other 2 (`Manhattan Mar '19 Rd 3 Kurt`, `Niagara '18 Rd 1
Caroline Polak Scowcroft`) have pre-existing rack/tile transcription defects that
fail identically before healing and need manual correction.

## Racks with more than 7 tiles

A `>Player: RACK POS WORD +score cum` line whose rack field has **more than 7
tiles** is a transcription error. It imports fine but the analysis worker rejects
it - `GetAnalysisStatus` returns `FAILED` with e.g. `turn N: rack
"ADEEEILRSXY" has 11 tiles, max is 7`. That blocks the game from ever completing
and, since the report pipeline defers a collection until every game is
analysis-complete, silently drops the whole collection from the daily report.

**The played tiles are always a subset of the true rack, so the play tells you
what the rack should have been.** Cleanest case: when that turn's move is a bingo
using all 7 tiles (a `TILE_PLACEMENT_MOVE` whose word has exactly 7 letters
placed - no `.` playthroughs, no lowercase blanks), the rack is *exactly* the
tiles of the played word. Real example (King's Cup 2019 Rd 8 vs Hubert Wee,
confirmed 2026-07-16): `>Hubert_Wee: ADEEEILRSXY 3G DEISEAL +94 469` - `DEISEAL`
is a clean 7-tile bingo, so the rack could only have been `ADEEILS`. Correcting
just the rack field makes the game analyze.

When the play is *not* a full 7-tile bingo, the rack must still contain every
non-`.` tile of the word (lowercase = a blank `?`), but the leftovers can't be
recovered from the play alone - reconstruct from the next turn's rack / bag
state, or ask Jesse. **Only ever edit the rack field**; never the move, score, or
cumulative. Not auto-healed by the preflight scanner (it's caught server-side at
analysis time, not parse time).

After correcting an already-uploaded game in place, its cached `FAILED` result
stays stale until you re-run with `RequestAnalysis {..., "force": true}` - plain
`force: false` returns the cached failure.

## A successfully-analyzed game can never be re-analyzed

**The single most important thing to know before editing an already-uploaded
game.** Confirmed 2026-07-22 against `pkg/analysis/service.go:547`:

```go
} else if req.Msg.Force && existingJob.Status == "completed" {
    // Force re-analysis only allowed for legacy (v0) results
    if partial.AnalysisVersion >= 2 {
        return ALREADY_REQUESTED, "Analysis is already up to date"
    }
```

`force: true` is honoured in exactly two cases: the job **failed**, or the stored
result is a **legacy v0** analysis. For any current (`analysis_version: 2`)
completed result the call returns `ALREADY_REQUESTED` / "Analysis is already up
to date" and does nothing - **even though the game's moves have changed
underneath it.** Editing a game does not invalidate its analysis server-side.
There is no public API to force one; `AnalysisAdminService.RequeueAnalysis` does
exactly the needed reset but requires `rbac.AdminAllAccess`
(`pkg/analysis/admin_service.go:84`).

Consequences to plan around:

- **Check status BEFORE editing.** `GetAnalysisStatus` returns both `status` and
  `analysis_version`. `COMPLETED` with `analysis_version >= 2` means an in-place
  edit will never reach the analysis - stats stay computed against the old moves
  forever.
- **The fix is a fresh upload under a new `game_id`**, which has no existing job.
  Swapping it in and repointing everything keyed on the old id is
  `scripts/replace_uploaded_game.py`; the whole diagnose-and-repair procedure is
  **`/fix-uploaded-game`**. The old game can't be deleted once finished, so it
  becomes an orphan - expected.
- **A partial (<7 tile) rack is the case that bites**, because unlike an over-7
  rack it does NOT fail analysis. It analyzes "successfully" against the short
  rack, which quietly makes that player's `mistake_index` meaningless and blocks
  per-player stats - and by then the result is frozen.

Worked example: game #59 (2024-07-08 vs James Curley), turn 27 `AARSVV` where the
play required 7 tiles. The preceding play (`N9 OF` from `FORSSVV`) left `RSSVV`
and the following rack was `AINRS`, so turn 27 had to be `AARSSVV` - `RSSVV` plus
the drawn `AA`, playing `VAVS` to leave `ARS`. `scripts/verify_gcg.py` (run
automatically by `woogles_upload.py`) confirmed the reconstruction by replaying
the whole file: `100 tiles accounted` and the true finals. Re-uploaded as
`S597yCKxMKXrNm6259BFQ6`.

## Manual API calls (fallback only)

`scripts/woogles_upload.py` does all of this. These are here for debugging a
failure it can't explain.

```python
# 1. Import
POST {BASE}/omgwords_service.GameEventService/ImportGCG
{'gcg': contents, 'lexicon': 'CSW21',
 'rules': {'board_layout_name': 'CrosswordGame',
           'letter_distribution_name': 'english', 'variant_name': 'classic'},
 'challenge_rule': 'ChallengeRule_FIVE_POINT'}
# → {"game_id": "..."}, viewable at https://woogles.io/anno/<game_id>

# 2. Find or create the collection
POST {BASE}/collections_service.CollectionsService/GetUserCollections
{'user_uuid': '', 'limit': 100, 'offset': 0}     # empty uuid = authenticated user
POST {BASE}/collections_service.CollectionsService/CreateCollection
{'title': tournament_title, 'description': '', 'public': True}
# → {"collection_uuid": "..."}

# 3. Add the game
POST {BASE}/collections_service.CollectionsService/AddGameToCollection
{'collection_uuid': ..., 'game_id': ..., 'chapter_title': 'Round 4 - JD vs Becky Dyer',
 'is_annotated': True}
# empty body on success; non-2xx or a JSON code/message means failure
# (e.g. permission_denied if the collection isn't owned by the authenticated user)

# 4. Verify
POST {BASE}/collections_service.CollectionsService/GetCollection
{'collection_uuid': ...}   # check game_count and each games[].chapter_title
```

- The `gcg` field is capped at 128,000 bytes server-side (`InvalidArg` beyond).
- A `500` mentioning a missing `.kwg` file almost always means the lexicon code
  is wrong (`NWL2023` instead of `NWL23`) - fix the code, don't retry blindly.
- A blank board at the resulting URL means the GCG didn't parse cleanly - re-check
  the endgame-line format and re-import as a brand new game.
