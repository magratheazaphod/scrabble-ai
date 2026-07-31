# Verified Woogles API response structure

Confirmed June 2026 against the live service. Auth and base URL: see `CLAUDE.md`.

```
GetAnalysisResult → response["result"]["player_summaries"][]
                    response["result"]["turns"][]

GetGameHistory    → response["history"]["players"][]
                    response["history"]["events"][]
                    response["history"]["final_scores"][]
                    response["history"]["last_known_racks"][]
```

## `player_summaries[]`

- `player_name` - camelCase-merged (e.g. `"JD"`, `"MichaelDonegan"`). Differs
  from `nickname` in GameHistory.
- `mistake_index` - the "Mistakes" score shown in the UI. **Null/absent for
  annotated games.** Always handle None gracefully.
- `estimated_elo`

**Full annotation must be detected from rack completeness, NOT from
`mistake_index`.** A non-null opponent `mistake_index` is not a reliable signal
that the opponent's side was fully tracked - Woogles computes a mistake score
even when it only knows the tiles the opponent *played* each turn. Confirmed at
Causeway 2026: all 19 games had a non-null opponent `mistake_index`, but only the
3 games annotated for both sides had real full-rack data.

In a partially-annotated game the opponent's recorded `rack` on each turn is just
their played tiles (2-6 letters), so the trustworthy signal is **a full 7-tile
rack on every opponent turn**, allowing legitimately short racks only once the
bag is empty (`tiles_in_bag == 0`). That is `opp_racks_complete`. The game must
also have concluded (`history["play_state"] == "GAME_OVER"`) - an in-progress or
aborted game can carry stats that aren't meaningful finals. When a game fails
either check, show "—" for that game's opponent stats rather than a partial
number.

## `turns[]`

- `player_index` - 0 or 1; use for reliable Jesse identification
- `rack` - Jesse's rack for this turn as seen by analysis; may differ from
  history in rare cases, so validate against history
- `tiles_in_bag` - 0 once the bag is empty (endgame phase)
- `played_is_bingo` / `optimal_is_bingo` - bools; prefer over parsing notation
- `spread_loss` - spread points lost vs BestBot's optimal move
- `win_prob_loss` - fraction of win probability lost (0-1); sum Jesse's turns for
  total win% lost
- `mistake_size` - `NO_MISTAKE`, `SMALL`, `MEDIUM`, `LARGE`
- `is_phony` - played word is not in the lexicon
- `phony_challenged` - phony was challenged off
- `missed_bingo` - a bingo was available and not played
- `played_move`, `played_score`, `optimal_move`, `optimal_score` - move format is
  `"8D WORD"` (position + tiles placed; lowercase = blank, `.` = existing board
  tile)

### The candidate lines a turn carries

Every turn holds the simulation (or endgame solution) behind its verdict, which
is where the error log's equity columns come from. `tournament_report.
equity_breakdown` is the only thing that should read them; verified over the
whole golden corpus (5,544 turns).

- `top_sim_plays[]` - present on every non-endgame turn, ordered best-first.
  `[0]` is always the turn's own `optimal_move`, and exactly one entry carries
  `is_played_move: true`, so the played and best lines are always both there.
  (`[0]` is *not* always the maximum `win_prob` - it differs on ~4% of turns -
  so rank by list order, never by re-sorting on `win_prob`.)
  - `equity` - mean simulated spread for that candidate. Every candidate in a
    turn runs the same `iterations`, so two candidates' figures are comparable
    and their difference is the spread the played move gave up.
  - `ply_stats[]` - `score_mean` per simulated ply. **Plies alternate starting
    with the opponent's reply: odd `ply` is theirs, even is mine.** The
    candidate's own `score` belongs on my side of the ledger.
- `principal_variation` / `other_variations[]` - endgame turns only
  (`top_sim_plays` is empty there). The solved line for the best play, and one
  line per alternative; find the played one by matching `moves[0].
  move_description` against `played_move` (resolvable on every endgame turn in
  the corpus). `move_number` starts at 1 with the play under consideration, so
  odd moves are mine and even are the opponent's, and the difference in
  `final_spread` equals `spread_loss` exactly.
- `top_peg_plays[]` - always empty in this archive; don't build on it.

## `events[]` (from GetGameHistory)

- `type` - `TILE_PLACEMENT_MOVE`, `EXCHANGE`, `PASS`, `END_RACK_PTS`,
  `PHONY_TILES_RETURNED`, `CHALLENGE_BONUS`, …
- `player_index`
- `rack` - the player's rack *before* the move (authoritative; use this for
  missed-bingo validation)
- `played_tiles` - tiles placed (lowercase = blank played as that letter, `.` =
  board tile reused)
- `row`, `column` - 0-indexed board position of the first tile
- `direction` - `"HORIZONTAL"` or `"VERTICAL"`
- `position` - string notation, e.g. `"8G"` (row 8, col G, horizontal) or `"G8"`
  (col G, row 8, vertical)
- `exchanged` - tiles exchanged (`?` = blank)

## Move count alignment

History `events[]` always has **one extra move** vs `analysis["turns"]` - the
final PASS that ends the game appears in events but has no analysis turn. So
`move_snapshots[turn_idx]` correctly indexes the board state before
`analysis["turns"][turn_idx]` for all valid turn indices.

`PHONY_TILES_RETURNED` and `CHALLENGE_BONUS` are not counted as moves and don't
consume snapshot slots.

## Collection entries

`GetCollection` game entries carry `game_id`, `chapter_number`, `chapter_title`,
`is_annotated`.

- `chapter_number` is position within the collection (1-indexed), **not** the
  tournament round. Extract the real round from `chapter_title` (e.g. "Rd 7
  Vannitha vs JD" → 7) and note missing rounds in the report header.
- `is_annotated` means "has commentary notes". It does **not** indicate BestBot
  analysis - always check that separately.

`GetUserCollections` is more reliable than the profile page; the profile widget
has been observed to omit collections that exist in the API. Page through with
`offset` when `len(collections) == limit`.

## Game URLs

`https://woogles.io/anno/<game_id>` (annotated) or `/game/<game_id>` (played).

**Deep-linking a turn:** `?turn=N` on either URL opens examine mode with **N−1
events replayed**, i.e. just before raw `events[N-1]` - so `turn_url(game_url,
event_idx)` emits `event_idx + 1`. N counts *events*, not analysis turns, and the
two diverge as soon as a play is challenged off, which is why the link needs
`build_turn_event_indices` rather than the analysis turn index. (Confirmed in
liwords-ui: `table.tsx` does `handleExamineGoTo(turn - 1)` over the flat event
list, and `CommentsDrawer.tsx` links to the position *after* a move as
`event_idx + 2`. `/anno/:gameID` routes to the same `GameTable`, so annotated and
played games behave identically.)
