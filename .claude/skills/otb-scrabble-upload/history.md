# OTB pipeline history: benchmarks, incidents, lessons

Reference only — SKILL.md does not require reading this. Newest first.
The pre-tooling prose SKILL.md and original known-issues.md are preserved in
`archive/` (Jesse's request: keep the old way for benchmarking/revert; the
solver's pre-lexicon behavior is also still reachable via `--no-lexicon`).

## 2026-07-15 — Deterministic tooling overhaul

Moved everything checkable out of model judgment into scripts (see SKILL.md
for the pipeline): `prep_photos.py`, `check_transcription.py`, lexicon
validation + `--json` in `otb_solver.py`, `author_gcg.py`, `verify_gcg.py`,
and the shared `scripts/woogles_upload.py`. Motivation and full analysis:
plan "Optimize the /otb-scrabble-upload pipeline" (2026-07-14). Regression:
games #91 and #92 round-trip (ground-truth transcript → checker → solver →
author) to event-for-event matches; the checker catches the dropped-exchange
class, the lexicon-validated solver makes the phony-cross-word class
impossible, and the old wrong endgame reading of #91 survives only as a
1-mismatch solution behind a loud red-flag warning.

Benchmark the NEXT real game against the figures below and record it here.

- Immediately caught on first sweep: game #90's LAGGIER cum inconsistency
  (see known-issues.md) — a 10-point error that had sat unnoticed in a
  "known-good" game.
- Confirmed misread-rack failure mode (Jesse, 2026-07-15): his INANER rack
  was AEINNNR, transcribed AEINNR from hard-to-read handwriting. The checker
  now warns on any mid-game Jesse rack ≠ 7 tiles.

## 2026-07-14 — Game #92: clean end-to-end on Opus 4.8 (pre-tooling baseline)

IMG_1520+IMG_1521 → https://woogles.io/anno/HKCMAaDXSdDeBMHkNbQPXN — main
agent Opus, all inline (no subagent). 0-mismatch solve on the first run, all
35 board words CSW24-valid, endgame racks correct. Independent re-verification
(2026-07-14, this tooling's verify pass): all cumulatives, 100-tile audit,
server-side replay all confirmed. What made it clean, per that session: tile
point-value subscripts to disambiguate glyphs, CSW lexicon checks on every
read, treating non-wordy candidates (a bogus "RR") as misreads. Those tactics
are now Steps 2–3 guidance; the lexicon audit is mechanical in the solver.
Exact token/time figures weren't metered; wall-clock tail: GCG written
21:06, committed 21:10.

## 2026-07-13 — Game #91: the two bad reconstructions and the corrected one

Same game (IMG_1518+IMG_1519), three attempts — the reason for both the
Opus-only model requirement and most of the mechanical guards:

- **Sonnet 5, full pipeline as one background subagent: ~480k tokens, 232
  tool calls, ~83 min — and WRONG twice over**, despite a clean 100-tile
  audit and successful upload (the danger: plausible-looking wrong answers
  that pass every mechanical check then available). (1) Missed Jesse's
  opening exchange (`x2` row read as filler); (2) fabricated a phony
  cross-word (EHV) to force the bag to close, giving Jesse's V to James.
- **Opus 4.8 rerun: OCR subagent ~170k tokens, 48 tool calls, ~28 min**
  (main-agent work on top of that unmetered). Fixed both Sonnet errors but
  introduced its own endgame error: treated all of VEE as James's going-out
  play (mis-scored), dismissing Jesse's lone "V +6" as a tally slip —
  reconciling only by declaring TWO table errors. Lesson: a reconstruction
  that only closes by explaining away anomalies is a red flag; the true
  reading had ZERO mismatches. (Now a solver warning.)
- **Corrected (Jesse-confirmed): Jesse 406 / James 438**,
  https://woogles.io/anno/iv6yiCLPJPptXGGWdH3CJu. Jesse played the V himself
  (REV down column H); James added only the two E's for VEE.

Treat the Sonnet figure as a lower bound on the old cost, not a validated
number for a correct run.

## 2026-07-12 — Game #90: first pipeline run (Fable), proven end-to-end

IMG_1516+IMG_1517 → https://woogles.io/anno/9G2uCPfVaXKhXpT9tCR84w. Believed
error-free until the 2026-07-14 verifier sweep found the LAGGIER cum
inconsistency (known-issues.md). Established the core method: score-
constrained joint placement search instead of trusting tile positions read
off the photo.
