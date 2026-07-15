# Known issues / open corrections

## Game #91 (JD vs James Curley, 2026-07-12, IMG_1518+IMG_1519) needs re-reconstruction

**Status as of 2026-07-13: WRONG, still live, not yet fixed.**

- Currently uploaded at https://woogles.io/anno/XaTrFqhzE3yEpZLm2m7eLh
- Filed as `Practice Games/James Curley/91_jul12_26.gcg`
- Curley tracker sheet row 91 currently reads: date 7/12/2026, me 400, jc 448,
  game id `XaTrFqhzE3yEpZLm2m7eLh`
- Reconstructed by a Sonnet 5 background subagent, 2026-07-13. See the
  "Model requirement" note at the top of `SKILL.md` — this is the confirmed
  bad run that prompted it.

None of the above (Woogles game, GCG file, tracker row) have been corrected
yet. Whoever picks this up next needs to fix all three, and needs an
Opus-class model to do the reconstruction (see `SKILL.md`).

### The two confirmed errors (from Jesse directly, 2026-07-13)

1. **Missed Jesse's opening-turn exchange.** The move list this run built
   starts with James's `BI` at 8G, then Jesse's `QAT` — but Jesse's actual
   first turn was an **exchange**, not a play, and it's missing from the
   move list entirely. Likely cause: the scoresheet row for that turn wasn't
   recognized as Jesse's exchange notation and got treated as alignment
   filler instead of a real (played-nothing) turn. Jesse's notation (see
   `SKILL.md` Step 2): `xABCD` (an `x` + the specific tiles exchanged) or
   `xN` (an `x` + a count only, e.g. `x2`, meaning his worst N tiles without
   recording which). Re-transcribe turn 1 specifically looking for this —
   check the cell for a leading "x" before assuming it's blank/filler.

   **Ground truth for this exact turn (confirmed by Jesse, 2026-07-13):** the
   sheet says `x2`, but he specifically recalls exchanging **L and W**
   (`xLW`), keeping **AINRT** — his opening rack was therefore `AINRTLW` (7
   tiles), he exchanged L and W, kept A/I/N/R/T, and drew 2 replacement
   tiles (which 2 is not recoverable from the sheet — treat as unknown/drawn
   from the bag per normal exchange rules, not something to solve for). Use
   `xLW` — a specific, known exchange — not a generic `x2`, when rebuilding
   the move list and GCG for turn 1.

2. **Endgame: the solver gave Jesse's V to James.** Jesse confirms his rack
   after playing ESTERASE should be the full **four tiles ADHV**, not the
   three-tile `ADH` this run used. Look at what the wrong file actually
   contains (`Practice Games/James Curley/91_jul12_26.gcg`, last 6 lines):

   ```
   >JD: ADH L3 AH +26 400
   >James_Curley: UEVEE 6I .UE +13 421
   >JD: D - +0 400
   >James_Curley: VEE 14G VEE +23 444
   >James_Curley:  (D) +4 448
   ```

   The run dropped Jesse's V and folded it into James's rack instead
   (`UEVEE`/`VEE`), and James's resulting `VEE` play at 14G forms an
   **invalid cross-word `EHV`** with `HEM` at 13G — i.e. the run silently
   fabricated a phony placement to force the tile-bag arithmetic to close,
   rather than finding the placement where Jesse legitimately keeps the V.
   **400/448 are not the true final scores.** They also don't account for
   error #1 (a whole missing turn shifts everything downstream anyway).

### What the next run needs to do

1. Re-transcribe the scoresheet from scratch (don't trust the existing
   move-spec JSON or GCG from this run — the opening-turn error invalidates
   the whole sequence, not just the end).
2. Explicitly identify and encode Jesse's opening exchange as its own event.
3. Treat `ADHV` as ground truth for Jesse's rack after ESTERASE; solve for
   the placement(s) where Jesse holds all four tiles through the end and
   James's actual final play(s) are valid words (no phony cross-words).
4. Once a corrected, verified GCG exists:
   - Woogles has no delete for a *finished* game (per `gcg-upload` skill) —
     ask Jesse how he wants `XaTrFqhzE3yEpZLm2m7eLh` handled (leave it with a
     corrective comment pointing to a new correct upload, or some other
     resolution) before creating a replacement game.
   - Replace `91_jul12_26.gcg` in the repo with the corrected content.
   - Fix Curley tracker row 91 (date/me/jc/game id) to match the corrected
     game — do NOT leave the wrong 400/448 in the sheet.
   - Update this file (`known-issues.md`) to mark the issue resolved, with
     the new game_id and true final score.
