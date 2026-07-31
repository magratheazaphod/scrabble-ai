# Report semantics

What each figure means and how it must be presented. `scripts/tournament_report.py`
implements all of this; these are the rules behind the code and the constraints on
any prose written around it.

## Identity

- **Opponent name:** prefer `real_name` from `history["players"]` (already has
  spaces). Flip "Last, First". Cross-check against `chapter_title` when
  `real_name` looks abbreviated (single word vs multi-word in the title).
- **Sanity-check identity in every game before computing stats** - a wrong
  `jesse_idx` silently flips all per-game numbers.
- `opponent_cell` renders an opponent as
  `Real Name - [username](profile link) (#seed)`. The leading name is dropped
  when no real name is on file (the label already *is* the username), and the
  `(#n)` suffix appears only for a seeded collection (a league, where
  `render_report` is called with `round_label="Seed"`). Username comes from the
  game's own account data via `woogles_username`. Profile-link and
  name-registry privacy rules: see `CLAUDE.md`.
- **Missed Bingos tables show the bare Woogles username** (`opponent_handle`),
  not the display name - they're scanned rather than read, and the full
  `Real Name - username` cell already appears once per game in the per-game
  table. Falls back to the display name when there's no account behind the
  player.

## Stats

- **Endgame spread lost:** values of 20+ in a single game are worth flagging -
  use per-turn `played_move` / `optimal_move` / `spread_loss` to explain what
  happened.
- **Win% lost:** sum of `win_prob_loss` over Jesse's turns × 100. High in a win =
  nearly gave it away; high in a close loss = structural deficit, not bad luck.
- **Blanks - drawn vs played:** the report displays `jesse_blanks` (**drawn**:
  every blank that reached the rack, including any exchanged away or stranded at
  the end) because that is the luck indicator Jesse wants. `compute_game` also
  returns `jesse_blanks_played`, which is never displayed and exists only so the
  league cross-check can compare like with like - the Woogles standings table
  counts blanks *played*. Do not "fix" the report to match the platform here; the
  difference is intentional.
- **Missed bingo validation:** always cross-check `missed_bingo` against the
  history event rack. The analysis occasionally stores an incorrect rack, causing
  a false positive (confirmed in Causeway R2: analysis showed BEEIORZ, actual was
  BEELORZ). If `validate_bingo(om, history_rack)` returns False, skip the entry.
- **Nigel Richards' "missed" bingos:** when the opponent is Nigel Richards,
  phrase his passed-over bingos as "passed up" rather than "missed" in game notes
  (`game_note`'s `miss_verb` checks for `'nigel' in opp_name.lower()`) - he sees
  them and chooses not to play them. Applies only to opponent-attributed note
  text; Jesse's own wording and the Missed Bingos table are unaffected.
- **Never let a missed bingo change hands.** The per-game note writes Jesse's own
  misses as a bare `missed bingo #N (WORD)`, and a summary once read that as the
  *opponent's* miss and published it (Season 18, BIATHLETE). `build_digest`
  therefore states both lists with explicit attribution, and the two report
  tables are kept separate rather than merged with a "who" column. Preserve both
  when editing.
- **Board reconstruction:** `build_snapshots_and_racks` runs in ~2ms per 19-game
  tournament and uses zero Claude tokens - prefer it over dictionary lookup for
  missed-bingo word resolution. It, `build_played_words`, and
  `build_turn_event_indices` all walk the event log through the one shared
  generator `iter_move_events`; keep new events↔analysis alignment there rather
  than re-implementing the "skip PHONY_TILES_RETURNED / CHALLENGE_BONUS" step.

## The error log ("All Errors")

On by default only in league reports (`error_log=True`), where both racks are
known every turn. One row per turn of Jesse's, ranked by win% lost.

- **What counts as an error.** Any turn that cost win probability, plus any turn
  where win% stayed flat (within `FLAT_WIN_PROB`, 0.5%) but at least
  `SPREAD_ONLY_EQUITY` (5) points of equity went with it - flagged `spread only`.
  That second rule exists for the endgame and pre-endgame, where a game already
  won or already lost gives every candidate the same win probability and the
  simulation is ranking on spread alone; a win%-only filter is blind to exactly
  the turns where the margin gets thrown away. `was_optimal` turns are never
  errors, whatever the numbers say. Because the ranking is by win% lost,
  spread-only rows form a block at the foot of the table and get their own
  ranked slice in the digest - a plain head-of-list cut would never show one.
- **Equity Lost** is the simulation's own verdict on the played move against the
  play in the Best column: `optimal.equity - played.equity`, or the exact
  difference in solved `final_spread` in an endgame. **A negative figure is not a
  bug** - it means the played move was better on spread and BestBot's choice
  bought win probability with it, which is routine when protecting a lead. Never
  clamp it at zero or describe it as "spread gained by the bot".
- **Off Δ / Def Δ** decompose that same comparison into scoring: points the
  played move scores for Jesse across the line, and points it concedes to the
  opponent. Both are signed so **positive is good for Jesse** - `Def Δ` is the
  opponent's scoring *reduction*, not their scoring. They sum to roughly
  −Equity Lost, never exactly: the remainder is everything spread depends on
  besides the plays' own scores (leave value; the going-out bonus and unplayed
  tiles in an endgame). Don't "fix" the gap by deriving one column from another,
  and don't present the two as a decomposition of equity that must balance.
- Rows with no comparable line in the analysis (a challenge, a pass the
  simulator never sat on - 2 of 1,258 rows in the golden corpus) show `—` in all
  three columns rather than a zero, which would read as "this cost nothing".

## Phonies

`is_phony` = word not in the lexicon. Check it for **both** players, not just
Jesse. `analysis['turns'][i]['is_phony']` is authoritative - don't infer
phony-ness from the event log, since an *unchallenged* phony has no
`PHONY_TILES_RETURNED` event and still scores.

Every phony gets named in the per-game note with a trailing `*`
(`phony WORD*`, or `phony WORD* (unchallenged)`); opponent phonies are attributed
by the opponent's name from the Opponent column. If `total_phonies == 0`, omit
"Games per Phony Played" from the report.

**Multi-word plays:** when the play formed more than one word
(`event['words_formed']` has >1 entry - a bingo crossing several tiles, or a
short play forming a cross word), show ALL of them joined by `/` before the `*`
(e.g. `GU/PU*`). Do NOT assume the primary or longest word is the invalid one.
This bit Jesse: two "phonies" (GU, LINUX) turned out to be valid CSW words, and
the actual violation was almost certainly the cross word (PU, NEEL) formed
alongside them. **Never assert which specific word was invalid** - show the full
set and let Jesse judge.

**Under the VOID challenge rule, never credit a phony-free record.**
`history['challenge_rule']` is `VOID` for league and most online play, meaning
the client refuses an invalid play outright - a phony cannot be played at all.
When every game in a collection is VOID, `aggregate` sets `void_challenge` and
nulls the phony aggregates; the report omits the phony rows and the `*` footnote,
and `build_digest` states the rule in prose so the summary writer knows. In any
prose about such a collection, do not praise or even mention zero phonies or
"word-validity discipline". The same applies to the mistakes score: a phony isn't
an available error under VOID, so mistakes scores run structurally lower. Read
league mistakes figures against other league play, never against OTB numbers.

## Opponent columns

`opp_fully_annotated` requires all three of: opponent `mistake_index` non-null,
`history["play_state"] == "GAME_OVER"`, and `opp_racks_complete`. The rack check
is the one that actually discriminates (see `api-fields.md`). Tournament-level
averages (`avg_opp_mi`, `avg_opp_wpl`) must only include `opp_fully_annotated`
games, not the whole collection.

**Measure the opponent the way you measure Jesse, wherever the data allows.**
Whenever `opp_fully_annotated` is true, the opponent gets the same treatment
Jesse does:

- per-game table: `Opp Mistakes | Opp Missed Bingos | Opp Blanks | Opp Endgame
  Spread Lost | Opp Win% Lost`, mirroring the order of Jesse's own columns
- aggregate table: Opponent Bingo Find Rate / Missed Bingos / Blanks Drawn /
  Avg Endgame Spread Lost
- an **Opponent Missed Bingos** section formatted identically to the Missed
  Bingos one (omit entirely when no qualifying game has one)

This is not league-only - it applies to any collection, game by game. A league
game is *played*, so both racks are known every turn and every game qualifies;
the rare tournament equivalent is a doubly-annotated or livestreamed game.
Non-qualifying games show "—" and are excluded from every Opp denominator (which
is `n_opp_annotated`, footnoted under the table).

## Layout

- **Win/Loss Progression:** a single line of 🟩 (win) / 🟥 (loss) / 🟨 (draw)
  boxes, one per game in chronological order, no round numbers and no labels,
  grouped into blocks of 5 separated by a space. Placed immediately after the
  header line, before Aggregate Stats, with no section header of its own.

  ```python
  box = {'W': '🟩', 'L': '🟥', 'D': '🟨'}
  boxes = [box[g['result']] for g in stats]
  progression = ' '.join(''.join(boxes[i:i+5]) for i in range(0, len(boxes), 5))
  ```

- **Draws** are rare but real: `result` is `"W"`/`"L"`/`"D"`, never `"L"` by
  default, and losses are counted rather than derived as `n - wins`. The record
  reads `9-10` normally and `9-10-1` only when a draw occurred (a permanent
  trailing `-0` would churn every cached digest to say nothing). Any new win-rate
  or record logic must count `"D"` explicitly.
