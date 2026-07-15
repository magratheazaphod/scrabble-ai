# Known issues / open corrections

(Resolved-incident history lives in `history.md`; the 2026-07-14 version of
this file is preserved in `archive/`.)

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
