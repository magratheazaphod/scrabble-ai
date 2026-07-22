# Known issues / open corrections

(Resolved-incident history lives in `history.md`; the 2026-07-14 version of
this file is preserved in `archive/`.)

## OPEN — Game #90 has a SECOND, undocumented table error: DELS (found 2026-07-15)

Found by the benchmark re-run of IMG_1516+1517 (history.md). The entry below
attributes the whole board-true delta to LAGGIER, but that is arithmetically
impossible: **the LAGGIER mis-add is entirely in JD's column and cannot move
James's score.** James's paper finals (297 + 8 tiles = 305) reach the
chain-true 309 quoted below only because of a separate error:

**`DELS` (James, L1) is recorded as 20; board-true is 24** — D on the L1
double-letter, S on the L4 double-word, plus the EM/LO/SI cross-words. Two
independent solver runs agree, the cell was re-read at zoom (it is
unambiguously "20"), and with DELS=24 the 100-tile bag audit closes and 24 of
25 moves match their recorded score exactly. James's paper cumulatives run 4
low from DELS onward.

So game #90 contains **two independent table errors, one per player** — and
they are of the two *different* classes (see `check_transcription.py`'s
`table_error` docs): LAGGIER breaks the cum chain but its score is board-true
(checker catches it, solver does not); DELS's score is wrong but carried
consistently, so the chain never breaks (solver catches it, checker cannot).

The `309` figure below was therefore right by accident of arithmetic while its
stated cause was wrong. **Open question for Jesse:** the live game #90 and its
2026-07-15 corrective comment describe only the LAGGIER error; the comment is
incomplete and arguably misleading about James's score. Decide whether to post
a follow-up comment there. The clean board-true reconstruction is live at
https://woogles.io/anno/MWnwzEBvrXRzJXkmFitLU4 (benchmark upload, deliberately
outside the collection/tracker).

## RESOLVED (by comment) — Game #90: 10-point cum inconsistency at LAGGIER

Found 2026-07-14 by the new `verify_gcg.py`; Jesse confirmed 2026-07-15 it
was a real table scoring error and chose the resolution: **a correction
comment on the live game only** (posted 2026-07-15, comment id 91fb1782);
the tracker row, repo file, and live game intentionally keep the as-played
509, and no replacement game will ever be uploaded.

Details: `90_jul12_26.gcg` line 12 `LAGGIER +69`, cumulative jumps 134 → 213
(+79). Board-true LAGGIER is 69 (two independent replays agree), so every
later JD cumulative — including the 509 final — is 10 points above what the
placements support (chain-true finals JD 499 / James 309). Consequence:
`verify_gcg.py` will keep flagging this one file; that is expected and not a
reason to "fix" it.

## OPEN — Games #91/#92 have played-tiles-only racks (found 2026-07-22)

Both live games carry each player's PLAYED TILES as the rack on every mid-game
turn, not the true 7-tile rack — so neither side is fully annotated and BestBot
produces no per-player stats (this is why Curley tracker rows 92/93 are blank;
see the tracker's terminal-rows list). Only the endgame racks are true, via
`author_gcg.py`'s endgame derivation.

Cause: `author_gcg.py` uses Jesse's transcribed rack only `if e.get('rack')`
and otherwise falls back silently to `placed_tiles`. The fallback is correct
for James (no opponent scoresheet exists) but wrong for Jesse, whose racks were
sitting on the photo — and nothing anywhere failed when they were skipped.

Both #91 and #92 predate the scripts-first overhaul, when Step 2 did not yet
call for the rack column. (Game #90 also has full racks, but it was a Fable
one-shot that worked this out unaided, not a run of this pipeline — it is not
evidence about the pipeline either way and is not in the run log.) The gap is
now closed at both ends: `check_transcription.py` ERRORs on a Jesse turn with
neither `rack` nor `kept`, and `author_gcg.py` refuses to write the file
(`--allow-partial-racks` is the only, loud, escape hatch).

A full-account census of all 244 annotated games (2026-07-22) confirms **no
uploaded version of either game has complete racks**; the three #91 copies and
one #92 copy are all in `data/otb-upload-log.jsonl`. Fixing requires
re-transcribing the rack column from IMG_1518/1519 and IMG_1520/1521 and
re-uploading as new games — imports are irreversible, so the current copies
stay up superseded. Jesse's call.

## NOTE — Game #91 rack correction (2026-07-15)

Jesse's rack for INANER was **AEINNNR** (7 tiles), not the transcribed
`INANER`/`AEINNR` — hard-to-read handwriting. The local
`91_jul12_26.gcg` now carries AEINNNR (uncommitted until the next push); the
live corrected game (https://woogles.io/anno/iv6yiCLPJPptXGGWdH3CJu) still
carries the played-tiles rack and cannot be edited. Scores unaffected; only
rack fidelity (matters for BestBot analysis quality).
`check_transcription.py` now warns on any mid-game Jesse rack ≠ 7 tiles.

## RESOLVED — Game #91 (2026-07-12, IMG_1518+1519) bad reconstructions

Corrected game live at https://woogles.io/anno/iv6yiCLPJPptXGGWdH3CJu
(Jesse 406 / James 438); corrected GCG in the repo; full post-mortem in
`history.md`. Residual loose end: the superseded wrong upload
(`XaTrFqhzE3yEpZLm2m7eLh`, 400/448) is finished and undeletable — confirm it
carries a corrective comment pointing at the corrected game, and that the
Curley tracker row 91 points at the corrected game_id.
