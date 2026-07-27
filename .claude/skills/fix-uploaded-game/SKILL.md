---
name: fix-uploaded-game
description: Diagnose and correct a game already uploaded to woogles.io — wrong racks, wrong moves or scores, wrong lexicon, a failed or meaningless BestBot analysis, or a stuck unfinished import. Use whenever Jesse says a game on Woogles is wrong, asks why a game's stats are blank or its analysis FAILED, or wants an uploaded game replaced or corrected. Covers which defects can be fixed in place and which force a re-upload under a new game_id, plus swapping the replacement into its collection and tracker.
---

# Fixing a game already on Woogles

Most defects here are **not** fixable in place, and which ones are is not
guessable - it depends on the analysis state, not on how bad the defect looks.
Get the diagnosis right first; the repair is mechanical after that.

Background on why the constraints exist: the irreversibility rules in
`CLAUDE.md`, and `/gcg-upload`'s `reference/gcg-pitfalls.md` for the rack and
re-analysis detail.

## Step 1 - diagnose before touching anything

```python
rpc(hdrs, 'analysis_service.AnalysisService/GetAnalysisStatus', {'game_id': gid})
# → {'status': ..., 'analysis_version': ...}
```

**Always run this before editing an uploaded game.** `status` and
`analysis_version` together decide whether an in-place fix can ever reach the
analysis. Editing a `COMPLETED` v2 game changes what people see on the board
while its stats stay frozen against the old moves - the worst outcome available,
because it looks fixed.

| Symptom | Status | Fix |
| --- | --- | --- |
| Import blocks with "finish or delete your unfinished games" | no history; "please wait until the game is over" | **Delete and re-import.** Unfinished games *are* deletable - `DeleteAnnotatedGame`, then re-import healed content. Not a replacement job. |
| Rack has >7 tiles | `FAILED`, `error_message` names the turn | **Edit in place.** Correct only the rack field, then `RequestAnalysis {force: true}` - force is honoured for FAILED. |
| Any other FAILED analysis | `FAILED` | **Edit in place**, then `force: true`. A FAILED analysis means a malformed upload; fix the cause. |
| Legacy analysis | `COMPLETED`, `analysis_version` 0 | **Edit in place**, then `force: true` - honoured for v0. |
| Rack has <7 tiles (played-tiles-only) | `COMPLETED`, v2 | **Re-upload.** It analyzed "successfully" against a short rack, so the meaningless result is frozen. |
| Wrong move, score, or placement | `COMPLETED`, v2 | **Re-upload.** |
| Wrong lexicon or challenge rule | any | **Re-upload.** Both are immutable after import. |
| Chapter title or collection order wrong | n/a | Not a game defect. `UpdateChapterTitle`, or `scripts/sync_curley_collection.py` for Curley. |
| Recorded score differs from what the GCG replays to | n/a | **Not a defect** for a practice game - the table score stands (`/curley-tracker`). Don't "fix" it. |

The partial-rack row is the one that bites, because unlike an over-7 rack it
does not fail. `GetGameHistory` and check whether every mid-game `rack` is 7
tiles (short racks are legitimate only once the bag is empty). Blank tracker
stats columns are usually this.

## Step 2 - let Jesse choose the remedy

A re-upload is not automatically the right answer even when the defect is real.
The old game can never be deleted, so a replacement leaves a permanent orphan,
and for a practice game the as-played record may be worth more than a clean
board.

**Precedent (game #90, 2026-07-15):** `verify_gcg.py` found a real 10-point
cumulative error. Jesse chose a **correction comment on the live game only** -
the tracker row, repo file, and live game intentionally keep the as-played 509,
and no replacement will ever be uploaded. `verify_gcg.py` still flags that file;
that is expected and not a reason to "fix" it.

So: present the finding, the options, and their costs, then do what Jesse
decides. Never re-upload unilaterally.

## Step 3 - build the corrected file

Reconstruct from the source, not from the live game.

- **Photo-reconstructed game:** re-run `/otb-scrabble-upload` from the
  transcription step. For a rack defect this means re-reading the scoresheet's
  far-left column; the racks were always on the paper.
- **Archive `.gcg`:** edit the repo file. Only ever touch the field that is
  wrong - never the move, score, or cumulative alongside a rack fix.
- Reconstructing a rack: the played tiles are a subset of the true rack, and the
  surrounding turns pin the rest. Worked examples in
  `/gcg-upload`'s `reference/gcg-pitfalls.md`.

Both gates must pass before upload:

```bash
python3 scripts/verify_gcg.py <corrected>.gcg
python3 scripts/gcg_preflight.py <corrected>.gcg --check
```

## Step 4 - upload the replacement

Normal upload, **without** `--collection` - the swap in Step 5 places it, so
adding it here would put the chapter at the end and leave you removing it again.

```bash
python3 scripts/woogles_upload.py <corrected>.gcg --lexicon <era CSW> \
    [--otb --game-number <N> --photos IMG_A.jpeg,IMG_B.jpeg] \
    --comment "<what was wrong and what changed>"
```

Keep `--otb` for a photo-reconstructed game so the run lands in
`data/otb-upload-log.jsonl` - that log is how you find every version of a game
later, and superseded copies are exactly what it exists to record.

## Step 5 - swap it in

```bash
python3 scripts/replace_uploaded_game.py --old <old_id> --new <new_id> \
    --reason "partial racks — BestBot produced no per-player stats" [--dry-run]
```

Finds every collection holding the old game via `GetCollectionsForGame` (so it
catches ones you'd forgotten), reuses the old chapter title and `is_annotated`
flag, restores the new chapter to the old one's slot, and comments on the old
game pointing at the replacement. **Dry-run it first** and read the plan.

It refuses if the new game isn't finished server-side, and warns if the new
game's own analysis has already FAILED.

## Step 6 - repoint everything else

The script prints these with the ids filled in; it doesn't run them, because the
tracker belongs to `update_curley_tracker.py` and the manifest has to be
committed with the `.gcg`.

1. **Tracker row** (Curley only) - repoint the existing row rather than creating
   a new one. `--game-num` targets by Game #:
   ```bash
   python3 scripts/update_curley_tracker.py --gcg <corrected>.gcg \
       --game-id <new_id> --game-num <n> --allow-score-mismatch
   ```
   Then, once BestBot has analyzed the replacement:
   ```bash
   python3 scripts/update_curley_tracker.py --enrich --game-id <new_id>
   ```
   Single-game `--enrich` finds the row **by game id**, so repoint first or it
   exits with "No existing row". Unlike `--enrich-collection` it has no
   already-filled guard, so it will overwrite stats left over from the old game.
2. **`.github/ocr-game-manifest.txt`** - replace the old id with the new one, and
   commit it with the corrected `.gcg`.
3. **`data/curley-enrich-terminal.txt`** - drop the old id if listed, or the
   replacement inherits its "permanently un-enrichable" verdict.
4. **Push the corrected `.gcg`.** Standing rule: every uploaded game lands on
   GitHub.

## Step 7 - verify and report

```bash
python3 scripts/audit_woogles_consistency.py --game-id <new_id>
```

Replays the live game and cross-checks its finals against the tracker row and
its events against the repo file.

Tell Jesse: what was wrong, what changed, the new game link, that the old copy
survives as a superseded orphan (with its link), and anything still pending -
BestBot analysis takes 2-10 minutes and the tracker stats fill in on the next
daily run, so a blank stats row right after a swap is expected, not a failure.

## Open cases

`/otb-scrabble-upload`'s `known-issues.md` tracks games waiting on this
procedure - as of 2026-07-22, **#91 and #92** carry played-tiles-only racks and
need re-transcribing from IMG_1518/1519 and IMG_1520/1521. A full-account census
confirmed no uploaded version of either has complete racks. Jesse's call.
