---
name: otb-scrabble-upload
description: Reconstruct an over-the-board Scrabble game from photos of Jesse's paper scoresheet and the final board, and upload it to woogles.io as an annotated game. Use whenever Jesse provides scoresheet + board photos of an OTB game (practice or tournament) and wants it on Woogles. Builds on /gcg-upload for the upload half; this skill covers photo transcription, the placement solver, GCG authoring, and the partial-rack endgame pitfall.
---

# OTB Scrabble game upload (photos → annotated Woogles game)

Inputs: (1) photo of Jesse's scoresheet, (2) photo of the final board. The
opponent's scoresheet is normally NOT available — their racks are partial
(just the tiles they played), except near the endgame (handled automatically,
see Step 5).

Every deterministic step is a script (this skill's `scripts/`, plus repo
`scripts/`). **Your job is only the judgment between scripts**: reading
handwriting, reading the board, resolving what the checkers flag, and
conferring with Jesse. If you catch yourself adding numbers, scoring words,
assembling GCG lines, or writing `requests` calls by hand — stop; there is a
script for it, and hand-work is where every historical error came from.

```
photos ─ prep_photos.py ─→ transcribe (YOU) ─ check_transcription.py (loop to PASS)
  ─→ board read (YOU) ─ otb_solver.py ─→ author_gcg.py
  ─→ verify_gcg.py + scripts/gcg_preflight.py --check
  ─→ scripts/woogles_upload.py ─→ tracker (Step 7)
```

Read the `gcg-upload` skill for API background and lexicon rules (Jesse =
CSW, current edition; `FIVE_POINT`). Use `/lexicon-lookup` while reading
words — the solver validates mechanically at solve time, but lexicon checks
resolve ambiguous reads much earlier. Benchmarks and incident history:
`history.md`. Open items: `known-issues.md`.

**Model requirement: Opus-class only** for the judgment steps (2–4). A
Sonnet 5 run produced a finished-looking, score-consistent game that was
wrong twice over (history.md, game #91); Jesse wants the safer tier used
regardless. When invoking via the `Agent` tool, pass `model: 'opus'`
explicitly. (Today's mechanical guards catch that run's specific failure
classes, but revisiting the tier is Jesse's call, not yours.)

**Core principle: never trust your eyes for placements — trust arithmetic.**
Tiles sit proud of the board and parallax shifts them ±1 column; handwriting
is ambiguous. The scores + cumulatives form an exact constraint system: with
~25 interlocked moves, requiring every computed score to match pins every
tile. The solver does this joint search; the checker guarantees the inputs
reconcile before it runs.

## Step 0 — Locate the input photos

`board-pictures/` at the repo root. Check for new `IMG_*.jpeg` before asking.

## Step 1 — Preprocess the photos

```bash
P=.claude/skills/otb-scrabble-upload/scripts
python3 $P/prep_photos.py scoresheet IMG_A.jpeg --outdir <scratch>          # + --rotate 90/180/270
python3 $P/prep_photos.py board IMG_B.jpeg --outdir <scratch> --corners-probe
python3 $P/prep_photos.py board IMG_B.jpeg --outdir <scratch> --corners NWx,NWy,SWx,SWy,SEx,SEy,NEx,NEy
```

Check `sheet_full.png` is upright (rerun with `--rotate` if not); transcribe
from `sheet_left/right.png`. Read the grid's four outer corner pixel coords
off `board_coords.png`, then the `--corners` run gives `board_grid.png`
(labeled 15×15). Expect ±0.3 cell drift — rows/words reliable, columns NOT.
Premium squares are parallax-free anchors; confirm the board is standard
layout. Re-read any ambiguous scoresheet cell with
`--zoom x0,y0,x1,y1 --label <name>` (coords from `sheet_full.png`); crops
come from the ORIGINAL photo — warps don't add detail.

## Step 2 — Transcribe the scoresheet into transcript.json

The JSON schema is documented in `check_transcription.py`'s docstring. Sheet
layout (rotate so handwriting is upright):

- Far-left column: **Jesse's racks**, one per row (7 letters, `?` = blank).
  Racks are a full 7 until the bag empties — if you read fewer, look again
  (confirmed failure mode: `AEINNNR` misread as `AEINNR`).
- Middle + right play columns: one Jesse, one opponent — identify by racks
  containing Jesse's words, and the Me/Them totals boxes. Each row = one
  turn pair; entries are `WORD score/cum`.
- Underlined letters = blanks. In the transcript, lowercase = a blank placed
  BY THAT move; play-through letters stay uppercase even over a blank.
- **Exchanges** (`xABCD` or `x2`-style counts) are real turns — never
  filler. Transcribe them as-is; the checker recovers a count-only
  exchange's tiles by rack intersection automatically.
- **Dashes have two meanings.** Next to a `+5` row in the OTHER column =
  Jesse challenged and the word held — NOT a turn; leave his cell null. With
  an `x`/count and unchanged cumulative = exchange. A genuine standing pass
  = `"pass"`.
- **Challenged-off phony**: opponent 0-score word that is NOT on the final
  board (tiles returned, turn consumed). Transcribe as
  `{"entry": "pass", "phony_off": "GINNIES"}` — never omit the turn.
- `+5` rows: challenge bonus to the player whose word was challenged and
  held; the rack written on that row belongs to their FOLLOWING move.
- Bottom boxes: `+Tiles`, `-Time`, `Total`; blank-designation boxes in the
  tracking grid say what each blank was played as.
- **Tile point-value subscripts disambiguate glyphs** (board photo, but also
  useful cross-checking sheet reads): C₃ vs O₁, Q₁₀ vs G₂, V₄ vs U₁; a blank
  shows a bear icon and NO subscript; only 2 blanks exist in the bag.
- Non-wordy reads are misreads: a phony a strong player actually lays down
  still looks wordy — `RR` is never a play (it was `RE`). Check candidates
  with `/lexicon-lookup`.

```bash
python3 $P/check_transcription.py transcript.json --spec game_spec.json
```

Fix every ERROR by re-reading the flagged cell (`--zoom`) — the messages say
which cell and what value would reconcile. Heed WARNs (short racks,
unrecoverable exchanges). **Do not proceed until PASS.** On PASS it emits the
solver spec so the move list is never hand-built.

## Step 3 — Read the final board (words + rows only)

From `board_grid.png`: every word, its orientation, and row (horizontal) /
column (vertical). Write these into the transcript's word entries as
`"dir": "H"|"V"` plus `"row"`/`"col"` where confident, then rerun the checker
to regenerate the spec. Leave start offsets to the solver. Bear-icon tiles =
blanks (record their cells for finalist disambiguation). Tiles OFF the board
= a player's unplayed leftover (cross-check: 2× value = `+Tiles` box). Board
letter-runs not in any move list are incidental cross-words — interlock
hints, not moves. A play may extend or thread through earlier words. Mark
any word that stood as a phony with `"phony": true`.

## Step 4 — Solve placements

```bash
python3 $P/otb_solver.py game_spec.json --json solution.json
```

Every candidate placement is CSW24-validated (main word + every cross-word)
except `"phony": true` moves, so wrong placements can't survive by
fabricating phony cross-words. Exact score matching does the rest.

- **Zero mismatches is the expectation.** The red-flag warning means the
  reconstruction only closes by declaring table scoring errors — re-examine
  the transcription (especially endgame tile ownership: who owns which
  endgame tile changed game #91's answer) before accepting it.
- Multiple finalists → distinguish by photo detail the output includes
  (e.g. which cell a blank occupies).
- No solution → the diagnostics print the first stuck move, achievable
  scores, and any lexicon-rejected words; a near-miss fingers the misread.
- A genuine flagged mismatch = a table error that stood. The GCG must carry
  board-true scores (Woogles recomputes from placements); `author_gcg.py`
  does this and adds a `#note`. Repeat the deviation in the game comment and
  the summary to Jesse.

## Step 5 — Author the GCG

```bash
python3 $P/author_gcg.py transcript.json solution.json --game-number <N> --out <file>.gcg
```

Machine-handles player order, nicknames (Jesse = `JD`), coordinates,
play-through dots, blank casing, exchange/pass/challenge/time lines, the
end-rack bonus line — and the **partial-rack endgame pitfall**: near the
endgame it derives each player's true full rack from their remaining plays +
leftover (the server otherwise ends the game early: "can only pass or
challenge"). It prints what it derived — sanity-check those racks. Add any
game-specific `#description`/`#note` prose by hand afterwards (see the #91
and #92 files for tone); table-error `#note`s are added automatically.

## Step 6 — Verify, file, upload

1. Both must PASS:
   ```bash
   python3 $P/verify_gcg.py <file>.gcg          # independent replay
   python3 scripts/gcg_preflight.py <file>.gcg --check
   ```
2. Save in the repo. **James Curley**: numbered-folder convention
   `Practice Games/James Curley/<N>_<mon><day>_<YY>.gcg` (e.g.
   `92_jul12_26.gcg`), where `<N>` is the tracker Game # — run Step 7's
   phase-1 update FIRST, then name the file after the row it landed on, and
   set `#description` to `Practice game <N>, YYYY-MM-DD. ...`. Other
   opponents: `Practice Games/YYYY-MM-DD <Opponent>.gcg`; tournament games:
   `Tournament Games/<year>/<event>/`. **Commit and push** (`git push origin
   master`) — standing rule: every uploaded game lands on GitHub.
3. Upload:
   ```bash
   python3 scripts/woogles_upload.py <file>.gcg --lexicon CSW24 \
       --collection "James Curley practice games" \
       --chapter "YYYY-MM-DD - JD vs James Curley (Game <N>)" \
       --comment "<reconstruction notes>"
   ```
   It preflights, imports (FIVE_POINT default), verifies the game finished
   server-side, adds it to the collection, and posts the event-0 comment.
   Include the game # in chapter titles (several same-day chapters already
   exist without it and are ambiguous). Lexicon = CSW edition current at the
   time of play; it CANNOT be changed after import. If the account has stuck
   unfinished games blocking imports, rerun with `--cleanup` (only
   unfinished games are deletable — it cannot destroy a finished one).
4. The comment must describe the reconstruction, any board-true-vs-paper
   deviations, and name any challenged-off phonies. Repeat all of that in
   the summary to Jesse with the `https://woogles.io/anno/<game_id>` link.

## Step 7 — (James Curley only) update the Curley tracker

If and only if the opponent is James Curley — no need to ask:

```bash
python3 scripts/update_curley_tracker.py --gcg "<path>.gcg" --game-id <game_id>
```

Phase 1 writes date/me/jc/game-id into the next templated row and re-creates
that row's result formulas; it is keyed on game_id (re-running is safe) and
refuses other opponents. **That row's Game # is the `<N>` for the Step 6
filename** — run this before naming the file. The BestBot analysis columns
fill in later automatically (the daily `woogles-report` GitHub Action runs
`--enrich-collection`); to backfill one immediately once analyzed:
`--enrich --game-id <id>`. Auth/setup: the script's docstring
(`GOOGLE_SA_KEYFILE`, `CURLEY_TRACKER_SHEET_ID` in `.env`).

## Debugging import failures

`woogles_upload.py` explains the common ones. For anything deeper, the exact
server pipeline is traceable in the local liwords repo
(`pkg/omgwords/service.go` ImportGCG → `pkg/cwgame/api.go` ReplayEvents/
AssignRacks → `pkg/cwgame/game.go` playMove); reading AssignRacks' bag math
is usually enough.

## Future extension (not yet built)

Jesse may later supply the opponent's scoresheet too (arbitrary format).
Same pipeline — put the opponent's racks in the transcript and validate them
like Jesse's; the endgame derivation then just confirms known racks.
