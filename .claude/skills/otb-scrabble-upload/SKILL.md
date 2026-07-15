---
name: otb-scrabble-upload
description: Reconstruct an over-the-board Scrabble game from photos of Jesse's paper scoresheet and the final board, and upload it to woogles.io as an annotated game. Use whenever Jesse provides scoresheet + board photos of an OTB game (practice or tournament) and wants it on Woogles. Builds on /gcg-upload for the upload half; this skill covers photo transcription, the placement solver, GCG authoring, and the partial-rack endgame pitfall.
---

# OTB Scrabble game upload (photos → annotated Woogles game)

Pipeline proven end-to-end 2026-07-12 (JD vs James Curley practice game →
https://woogles.io/anno/9G2uCPfVaXKhXpT9tCR84w). Inputs: (1) photo of Jesse's
scoresheet, (2) photo of the final board. Opponent's scoresheet is normally
NOT available — their racks are partial (just the tiles they played), with
exceptions near the endgame (see the CRITICAL pitfall below).

Read the `gcg-upload` skill too: all its API mechanics, lexicon rules
(Jesse = CSW, current edition; FIVE_POINT challenge), and endgame-line
gotchas apply. This skill produces the `.gcg`; gcg-upload uploads it. Also use
the `lexicon-lookup` skill throughout to validate every word against CSW (never
trust your own sense of validity — Step 3).

**Clean end-to-end on Opus 4.8, game #92** (IMG_1520+IMG_1521, JD vs James
Curley 2026-07-12 → https://woogles.io/anno/HKCMAaDXSdDeBMHkNbQPXN): main-agent
Opus doing all transcription/solve/GCG/upload inline (no subagent). The solver
hit a **0-mismatch** solution on the first run and all 35 board words validated
in CSW24 — the target quality bar. What made it clean: leaning on tile
point-value subscripts to disambiguate letters, CSW lexicon checks to resolve
every ambiguous read, and treating any "non-wordy" candidate (a bogus `RR`) as a
misread rather than a play. See the new tactics in Steps 2–3.

**Model requirement: Opus-class only.** A Sonnet 5 background-subagent run
(2026-07-13, game #91 / IMG_1518+IMG_1519, JD vs James Curley 2026-07-12)
produced a finished-looking, score-consistent GCG that was nonetheless wrong
in two concrete ways (see `known-issues.md` in this skill folder for the
full writeup). The run technically "succeeded" — clean 100-tile bag audit,
uploaded fine, Woogles accepted it — which is exactly the danger: a
plausible-looking wrong answer that passes every mechanical check. By
contrast, the very first game this skill ever produced (game #90 /
IMG_1516+IMG_1517, same opponent, same day) was reconstructed successfully
on Fable (2026-07-12) with no known errors — so this isn't "every non-Opus
model fails," but Sonnet has a confirmed bad result on this task and Jesse
wants the safer tier used going forward regardless. Per Jesse (2026-07-13):
do not run this skill's reconstruction judgment (Steps 2–4 especially) on a
Sonnet-tier model. When invoking via the `Agent` tool, pass `model: 'opus'`
explicitly.

**Cost benchmarks** (all game #91 / IMG_1518+IMG_1519, JD vs James Curley
2026-07-12 — the same game, so these are directly comparable):

- **Sonnet 5, full pipeline as one background subagent** (2026-07-13, through
  Woogles upload + collection + comment): **~480k tokens, 232 tool calls,
  ~83 minutes wall-clock**. Output was WRONG (see `known-issues.md`) despite a
  clean 100-tile audit and a successful upload.
- **Opus 4.8 rerun** (2026-07-13): OCR/transcription delegated to an Opus
  subagent (Steps 1–3 only) — **~170k tokens, 48 tool calls, ~28 min** for
  that subagent alone — with the orchestrating main agent (also Opus) doing the
  solve, an independent photo re-check, GCG authoring/verification, the Woogles
  replace, tracker, and commit on top (main-agent usage not separately metered,
  but of the same rough order). This run fixed the two Sonnet errors it looked
  for (recovered Jesse's dropped turn-1 exchange LW; fixed the player order —
  Jesse moved first) **but introduced a NEW endgame error of its own**: it gave
  Jesse's V to James. Jesse's scoresheet endgame reads "AH" then a lone "V +6";
  the correct reading is that *Jesse* played the V (REV down column H, R from
  ESTERASE + E from HEM, for 6) and James then merely added the two E's to make
  VEE across for 13. The Opus run instead treated the whole of VEE as James's
  single going-out play (mis-scored 19) and dismissed Jesse's "+6" as a paper
  tally slip, landing on a wrong 400/444. Jesse corrected it; the true final is
  **Jesse 406 / James 438** (the board itself was identical either way — only
  who-owns-which-endgame-tile changed). Corrected game:
  https://woogles.io/anno/iv6yiCLPJPptXGGWdH3CJu

The lesson from the Opus miss: a "board-true" reconstruction that only
reconciles by declaring a **table scoring error** (here, forcing VEE to a
mismatched score AND deleting an unexplained +6) should be treated as a red
flag, not a result — the true reconstruction of this game has **zero score
mismatches**. Prefer the reading that makes every recorded score exact over
one that "explains away" two anomalies at once. See the new endgame-ownership
and zero-mismatch heuristics in Steps 2 and 4.

Treat the Sonnet figure as a lower bound on cost, not a validated number for a
correct run. Budget accordingly when batching multiple games — this is not a
cheap/quick operation per game even before accounting for a redo. Delegating
just the OCR to a subagent (as in the Opus rerun) keeps the careful-reading
work isolated and gives the main agent a second, independent look at the photos.

**Core principle: never trust your eyes for placements — trust arithmetic.**
Reading tile positions off a photo has ±1-column errors (parallax: tiles sit
proud of the board and shift toward the camera center; handwriting is
ambiguous). The scoresheet's scores + cumulatives give an exact constraint
system: with ~25 interlocked moves, requiring every computed score to equal
the recorded score pins every tile placement uniquely. The solver script does
this joint search. Hand-verified scoring WILL make arithmetic slips (premium
squares on cross-words, DWS on both main and cross word, etc.) — machine only.

## Step 0 — Locate the input photos

Jesse drops scoresheet/board photos in `board-pictures/` at the repo root
(`/Users/Siwen/projects/scrabble-ai/board-pictures/`). Check there first for
new `IMG_*.jpeg` files before asking Jesse where they are.

## Step 1 — Preprocess the photos (PIL, in scratchpad)

- Scoresheet: usually photographed sideways. `Image.open(...).transpose(Image.ROTATE_90)`
  (or 270 — check which way is upright), then crop left/right halves and Read
  them. Zoom (crop + resize 2x) any ambiguous cell.
- Board: perspective-warp to a square, then overlay a labeled 15×15 grid:
  measure the four outer corners of the grid (crop each corner region, read
  pixel coords), then `img.transform((1500,1500), Image.QUAD, (NWx,NWy, SWx,SWy, SEx,SEy, NEx,NEy))`.
  Draw red gridlines every 100px with row/col labels and Read it. Expect
  residual drift of ±0.3 cell — good enough for words/rows, NOT for columns.
  Premium squares visible in the photo are reliable anchors (they're on the
  board surface, no parallax); confirm the board is standard layout.

## Step 2 — Transcribe the scoresheet (Jesse's format)

Layout of his sheet (rotate so handwriting is upright):
- Far-left column: **Jesse's racks**, one per row (7 letters, `?` = blank).
- Middle play column + right play column: one is Jesse, one is the opponent.
  **Identify which by matching racks to words** (Jesse's rack letters must
  contain his played word) and by the totals boxes (Me/Them + Spread).
- Each sheet row = one turn pair. If the left play column belongs to the
  player who moved first, that play precedes the right one in the same row.
- Entries look like `WORD  score/cumulative`. A standalone `+5/cum` row is a
  five-point challenge bonus (opponent challenged a valid word). Underlined
  letters = blanks. The rack written on a `+5` row belongs to the FOLLOWING
  move (Jesse recorded his fresh rack there).
- **Watch for exchanges, especially on turn 1** — confirmed root cause of a
  real bug (`known-issues.md`, game #91): a turn where a player exchanged
  tiles instead of playing can look, at a glance, like a blank/dash row and
  get silently skipped as "alignment filler." Jesse's own exchange notation
  (not a dash): `xABCD` — an `x` followed by the specific tiles exchanged
  (score is always 0, cumulative unchanged from the prior row), or more
  commonly just a count — `x1`, `x2`, `x3`, up to `x7` — an `x` followed by
  how many tiles he exchanged (his worst N) without recording which ones.
  Either form is a real turn and MUST appear as its own event in the move
  list/GCG (GCG exchange line: no score, `-` in place of coordinates/word —
  see the `gcg-upload`/GCG spec for exact syntax) — never drop it or fold it
  into an adjacent row.
- **For a count-only exchange (`x1`/`x2`/`x3`/…), always try to work out
  which tiles.** Don't stop at "he exchanged N tiles, unknown which" — the
  specific tiles usually ARE recoverable even when not written down: take
  the multiset intersection of the rack *before* the exchange and
  Jesse's recorded rack on his *next* turn — those common tiles are what he
  kept; whatever's left over from the pre-exchange rack (should be exactly N
  tiles) is what he exchanged. Example (game #91, turn 1): pre-exchange rack
  `AILNRTW`, sheet says `x2`, next recorded rack `AAINQRT` → intersection
  `AINRT` (kept) → leftover from `AILNRTW` is `L`+`W` → he exchanged `LW`,
  and the two new tiles drawn were the extra `A` and the `Q`. Only works
  when the following turn's rack was actually recorded (not always true —
  see the "rack written on a `+5` row belongs to the FOLLOWING move" note
  above for where else a "next rack" can come from); if it wasn't, the
  exchanged tiles genuinely aren't recoverable and that uncertainty should be
  noted explicitly rather than guessed. Before treating any other dash/blank
  cell as pure filler, also sanity-check that the *other* column's turn count
  still reconciles — a missing played-nothing turn (exchange or pass) throws
  off every move after it.
- **A dash in a play column is not always an exchange** (Jesse, game #92): it
  can also mark the row Jesse split out for a `+5` challenge bonus. When Jesse
  successfully forces the opponent to leave a valid word up (he challenges, it
  holds), he records the `+5` on its own row and puts a dash in his own play
  column for that row (he didn't play — he challenged). So a dash paired with an
  adjacent `+5` row in the *other* column = "I challenged the opponent's last
  word and it held" (opponent +5), NOT an exchange. Game #92 had exactly this:
  two dashes in Jesse's column, each immediately above a James `+5` row (Jesse
  challenged HACKIES and INSIGNE, both valid). Distinguish the two dash meanings
  by looking for a neighbouring `+5`: `+5` nearby ⇒ challenge; `x`/count with a
  0-score, unchanged-cumulative row ⇒ exchange.
- **A challenged-OFF phony scores 0 and is NOT on the final board.** If the
  opponent's word shows a 0 (cumulative unchanged) and you can't find it on the
  board, it was a phony they played that got challenged off — it consumed their
  turn (tiles returned to rack, no net draw). Represent it in the GCG as a
  **pass** (state-equivalent: 0 points, no board change), and ALWAYS add a game
  comment naming the phony (e.g. "James played the phony GINNIES here, challenged
  off"), since its board location usually isn't recoverable from the photo. See
  Step 5.
- Bottom boxes: `+Tiles` (2× opponent's leftover added to the out-player),
  `-Time` (time penalty), `Total`. The blank-designation boxes in the tile
  tracking grid (e.g. `? S Y`) say what each blank was played as.
- **Verify EVERY cumulative** (prev + score = cum) for both columns before
  going further; resolve illegible digits by arithmetic (e.g. Jesse's "4"
  looks like "ч"; "10" superscript looks like "6"/"lo"). The +Tiles and -Time
  boxes must reconcile with the Totals. Do not proceed until both columns
  reconcile perfectly — a misread score will send the solver into the weeds.

## Step 3 — Read the final board (words + rows only)

From the rectified grid image, list every word: its text, orientation, and
row (for horizontal) / column (for vertical). Rows/columns read from band
position are reliable; exact start offsets are not — leave those to the
solver. Note bear-icon tiles = blanks (record their cells). Note any tiles
sitting OFF the board in the photo — that's a player's unplayed leftover rack
(cross-check: 2× its value = the +Tiles box).

**Use the tile point-value subscripts to disambiguate letters** (Jesse's tip,
confirmed invaluable on game #92). Every tile prints its point value in the
bottom-right corner, always the same per letter, so when a glyph is ambiguous
the subscript settles it: `C`=3 vs `O`=1 (a round tile reading "1" is O, not C);
`Q`=10 vs `G`=2 (a rounded tile on a triple-word reading "10" is Q — that's how
QUATE's Q was found); `V`=4 vs `U`=1; `X`=8; `Z`=10; `J`=8; `K`=5. A blank shows
a bear icon and NO subscript, so a lettered tile with a visible number is never a
blank. Crop tight and upscale from the ORIGINAL photo (not the global warp) to
read a subscript — the source file has the resolution; a bigger warp does not add
detail. There are only 2 blanks in the bag total: if your reading needs a 3rd,
something is misread.

**Validate every board word against CSW before trusting it** (Jesse's standing
rule — do NOT rely on your own sense of validity; `SOREE`, `HAOMA`, `MUCIGENS`,
`QUATE`, `XU` are all valid and "look wrong"). Use the `/lexicon-lookup` skill
(`check_words.py`, default CSW24). Two payoffs: (1) resolving an ambiguous read —
enumerate candidate spellings, keep the one that is BOTH lexicon-valid AND fits
the board/scores (on game #92 this turned a misread "SCREE"→the real `SOREE`, and
caught that "RR" could not be a word); (2) an end-of-solve audit — extract every
maximal horizontal/vertical run (len ≥ 2) from the solved board and check them
all; any invalid run that wasn't a deliberately-played, challenged-off phony
means a misread. A nonsense candidate like `RR` is itself the red flag: a phony a
strong player actually plays still looks "wordy" — Jesse would never lay down
`RR`, so that read was wrong (it was `RE` in bad, time-pressured handwriting).

Every scoresheet word must appear on the board; board letter-runs not in any
move list (e.g. EMU/LOT/SI formed across MOITHER/DELS/HUT) are cross-words
formed incidentally — don't look for moves matching them, but they're strong
interlock hints. A played word may extend or thread through earlier words
(FROS later becoming AFROS via VEGA's A; WAUR playing through SUQ's U;
COINED reusing DELS's D; LEWK = single L tile through E,W,K of three other
plays). Racks confirm these: tiles played ⊆ recorded rack.

## Step 4 — Solve placements

Build the move list in strict turn order (first player's move N, then second
player's move N). Write a spec JSON and run:

```bash
python3 .claude/skills/otb-scrabble-upload/scripts/otb_solver.py game_spec.json
```

Spec: per move `{"player","word","score","dir","row"|"col"}` — lowercase
letter in word = blank; omit row/col if unsure. Plus `"leftover": "DIT"`.
The solver backtracks over all start offsets requiring exact score matches
(up to 2 flagged mismatches allowed) and prints GCG coordinates, play strings
with `.` for play-through, a full tile-bag audit (must be exactly 100, no
overdraws), and the final board — **diff that board against the photo**.

- Multiple solutions → distinguish by photo detail it prints (e.g. which cell
  the blank/bear tile occupies).
- A flagged mismatch (recorded ≠ board-true, placement forced by the photo)
  = a real table scoring error that stood. **Use the board-true score in the
  GCG** — Woogles recomputes scores from placements during import and ignores
  the GCG's numbers, so the file must be self-consistent. Document the table
  score in a `#note`, a game comment, and the summary to Jesse.
- No solution → a transcription error upstream; the solver prints the first
  stuck move and achievable scores (a near-miss usually fingers the misread).
- Phony words that stood (unchallenged) are fine: Woogles only validates
  words under VOID challenge rule; we import with FIVE_POINT.

## Step 5 — Author the GCG

```
#character-encoding UTF-8
#player1 James_Curley James Curley
#player2 JD Jesse Day
#description Practice game, YYYY-MM-DD. Reconstructed from Jesse's scoresheet and final board photo.
>James_Curley: AIA 8F AIA +6 6
>JD: AMNNOEY 9E ANOMY +24 24
...
```

- player1 = whoever moved first. Jesse's nick: `JD`.
- Coordinates: horizontal = `<row><ColLetter>` (`8F`), vertical = `<ColLetter><row>` (`D10`).
- Play string: `.` for each play-through tile (`COINE.`, `.UQ`, `MOIT...`,
  `L...`), lowercase for blanks (`sONNETS`, `.yLVINES`).
- Racks: Jesse's from the sheet (`ENNOST?`); opponent's = played tiles only
  (partial), EXCEPT near the endgame (next section). Bingo scores include +50.
- Challenge bonus on its own line right after the challenged play, rack =
  post-draw rack (the challenged player's rack for their NEXT move — with partial
  racks, reuse that next move's rack string on both the `(challenge)` line and the
  next play line, mirroring the sheet): `>JD: AAFHJNR (challenge) +5 218`. Note
  the `+5` goes to the player whose word was challenged-and-upheld; on game #92
  Jesse challenged James's HACKIES and INSIGNE, so each `+5` is on James's side.
- **Challenged-off phony → represent as a pass** (`>Player: RACK - +0 cum`,
  literal dash for the word). State-equivalent to the real event (0 points, tiles
  returned, no net draw), and it's the only option when the phony's board
  location isn't recoverable. The rack you know: it's exactly the tiles of the
  phony they tried (e.g. GINNIES). **You MUST add a game comment naming the phony
  on that passed turn** (Jesse's standing request) so the record isn't silently
  lossy — e.g. "Turn 8 (James) shown as a pass: he laid the phony GINNIES, which
  Jesse challenged off." Do NOT omit the turn entirely — that desyncs alternation
  and shifts every later move.
- Endgame, player X goes out and player Y holds tiles:
  `>X:  (YLEFTOVER) +2N cumX` (empty rack field, two spaces — see gcg-upload).
- Time penalty (after the end-rack line): `>JD: DIT (time) -10 509`.
  Confirmed parsed and applied by the importer.

## CRITICAL pitfall: partial opponent racks near the endgame

`ImportGCG` failing with **"can only pass or challenge"** means the server
prematurely ended the game: at each event it assigns the mover's declared
rack, throws the other rack into the bag, and if the bag has few enough tiles
it pre-deals them; after a move whose declared rack was exactly the played
tiles, the mover's rack is empty — and if the bag is also empty the server
declares them "out" mid-game, so the next tile placement errors.

**Rule: for any move where `100 − tiles_on_board_before_move − len(declared_rack) ≤ 7`,
the declared rack must be the player's TRUE full rack** (not just the played
tiles). At that depth it's derivable: the opponent's remaining plays reveal
their remaining tiles (e.g. DEBURR's rack had to be `DEBURRL` because the L
of LEWK was already in hand — the bag was empty). The final move's rack is
always exactly its played tiles.

If an import fails: the half-created game LINGERS and will block future
imports. `GetMyUnfinishedGames` (empty body) → `DeleteAnnotatedGame` with the
game_id, then fix and retry. (Both under `omgwords_service.GameEventService/`.)

## Step 6 — Verify + file + collection + comment

1. Before upload: replay the GCG with the solver's scorer (parse `>` lines,
   map `.`s from the board) — every move's score and cumulative must match
   (remember to add challenge bonuses when checking cumulatives). Then run
   `python3 scripts/gcg_preflight.py "<folder>" --check`.
2. Save the GCG in the repo. **For James Curley**, use his numbered-folder
   convention (matching his `Quackle's Revenge/James Curley/` archive on
   Jesse's machine, which is *not* itself in this repo yet — that historical
   backfill is a separate, not-yet-done task): `Practice Games/James Curley/<N>_<mon><day>_<YY>.gcg`,
   e.g. `91_jul12_26.gcg`. `<N>` is the **Game #** the Curley tracker sheet
   assigns this game (Step 7 determines it — do Step 7's phase-1 sheet update
   *before* picking the filename, then name the file after the row it landed
   on). `<mon>` is a lowercase 3-letter month abbreviation, `<day>` has no
   leading zero, `<YY>` is a 2-digit year. Set `#description` in the GCG to
   `Practice game <N>, YYYY-MM-DD. ...` so the game number is self-documenting
   inside the file too. For every other opponent (not yet migrated to a
   numbered-folder convention), fall back to the old flat naming:
   `Practice Games/YYYY-MM-DD <Opponent>.gcg`.
   Tournament games → the usual `Tournament Games/<year>/<event>/` convention.
   **After saving, commit and push to GitHub** (`git add`, commit, `git push
   origin master`) — Jesse wants every uploaded game to land on GitHub, not
   just locally, as a standing rule (confirmed 2026-07-13).
3. Upload per gcg-upload (lexicon = current CSW for a practice game played
   today, e.g. CSW24; `ChallengeRule_FIVE_POINT`).
4. Verify finished: `game_service.GameMetadataService/GetGCG {"game_id":...}`
   returns the full GCG only when the game is done (and diff it against what
   you uploaded); `GetMyUnfinishedGames` must be empty. Never delete-probe.
5. Collection: practice games with a regular partner go in an existing
   collection (e.g. "James Curley practice games" — check `GetUserCollections`,
   match on `uuid` field). Chapter title: `YYYY-MM-DD - JD vs <Opponent>`.
6. Post a game comment (event_number 0) describing the reconstruction and any
   score deviations from the paper record; repeat those in the summary to
   Jesse with the `https://woogles.io/anno/<game_id>` link.

## Step 7 — (James Curley only) update the Curley tracker

Jesse keeps a Google Sheet, the **Curley tracker**, of every game he plays
against James Curley. **If and only if the opponent is James Curley**, after the
game is uploaded and confirmed finished (Step 6), append it to that sheet
automatically — no need to ask:

```bash
python3 scripts/update_curley_tracker.py \
    --gcg "Practice Games/James Curley/<N>_<mon><day>_<YY>.gcg" \
    --game-id <the game_id from the anno URL>
```

This is phase 1: it writes only the raw inputs — **date, `me` (Jesse's score),
`jc` (opponent's score), and the `game id`** — into the next empty row of the
pre-templated grid, and re-creates that row's result formulas (the hidden
win-value, the running W/L totals, `combined`) so W/L/T/combined compute
themselves exactly like the existing rows. It never writes those formula columns
as literals. Keyed on the Woogles `game_id`, so re-running is safe (updates the
same row, never duplicates). The script refuses any opponent other than James
Curley — a no-op safety net for other uploads. Report the new row to Jesse with
the Woogles link.

**That row's `Game #` (leftmost column, pre-numbered) is the `<N>` used in the
Step 6.2 filename.** Run this phase-1 update first, read back the `Game #` it
landed on (e.g. via a quick `gspread` read of that row, or by trusting it's
one past the previous game's number), *then* save/rename the local GCG file
to `Practice Games/James Curley/<N>_<mon><day>_<YY>.gcg` and commit+push.

The BestBot analysis columns (mistakes score, bingos, blanks, endgame spread
lost, win% lost) are filled in **later, automatically**: the daily
`woogles-report` GitHub Action runs `update_curley_tracker.py --enrich-collection`
after it regenerates the report, so those cells populate once the game has been
analyzed. Nothing to do by hand. (To backfill one immediately once analyzed:
`python3 scripts/update_curley_tracker.py --enrich --game-id <id>`.)

Setup / auth for this step lives in the script's module docstring (a Google
service account keyed via `GOOGLE_SA_KEYFILE`, the sheet via
`CURLEY_TRACKER_SHEET_ID`, both in `.env`).

## Debugging import failures beyond the pitfall above

The exact server pipeline can be traced in the local liwords repo
(`pkg/omgwords/service.go` ImportGCG → dummy-pass insertion →
`pkg/cwgame/api.go` ReplayEvents/AssignRacks → `pkg/cwgame/game.go`
playMove). A throwaway Go harness replicating that sequence works (see
gcg-upload history) but requires Go ≥1.23 (local machine had 1.17 on
2026-07-12) — reading AssignRacks' bag math is usually enough.

## Future extension (not yet built)

Jesse may later supply the opponent's scoresheet too (arbitrary format).
That gives full racks for both sides — same pipeline, but validate the
opponent's racks against their plays exactly like Jesse's, and drop the
partial-rack special-casing.
