# Woogles league seasons

Jesse plays the Woogles Collins league (`woogles.io/leagues/csw`). A season runs
14 days inside one division; every game is played on Woogles and auto-analyzed by
BestBot, so there is nothing to upload and no analysis quota to spend. The only
missing piece is the collection, and `scripts/woogles_league.py` builds it - do
not hand-roll this.

```bash
python3 scripts/woogles_league.py                 # current CSW season
python3 scripts/woogles_league.py --season 18     # a specific season
python3 scripts/woogles_league.py --league nwl    # a different league
python3 scripts/woogles_league.py --all-leagues   # what the cron runs
python3 scripts/woogles_league.py --dry-run       # plan only
```

It is idempotent and meant to be re-run mid-season. The daily
`woogles-report.yml` cron runs `--all-leagues` before the snapshot step, so a
game that finished since the last run joins its collection automatically.

## Division sizes - do not read a low game count as unplayed games

**Divisions are not all the same size, and the schedule caps at 14 games** (one
per day of the 14-day season). A division of ≤15 plays a full round robin of
size − 1 games; a division of 16-17 is capped at 14 and is *not* a full round
robin. Season 18 ran 12-, 13-, and 14-game divisions side by side, with Jesse's
Division 14 (13 players) complete at 12 games.

Check `games_remaining` on the player's standing before saying anything about
games left. Telling Jesse "you have 2 games left" about a season he had finished
is a mistake already made once (2026-07-24); a below-leader game count is a
division-size difference far more often than an actual gap.

## What differs from a tournament report

All four are handled by the module; this is the reasoning behind it.

- **Ordering.** League games have no round (`round` is 0 for all of them), so
  chapter titles are `Seed <n> - <opponent> vs JD`, seeded by rating **in the
  league's own format**: `CSW19.classic.corres`. Leagues are correspondence, and
  Woogles rates every CSW lexicon under the legacy `CSW19` key (see
  `transformLexiconName` in liwords). `compute_game` parses `Seed <n>` into
  `round`, and `render_report(..., round_label="Seed")` labels the column.

  **Seeds rank the whole division, subject included** (`seed_order`) - a seed is
  a property of a player in the division, not of a slot in one person's schedule,
  so the same number means the same thing in the standings table and in a chapter
  title. Expected consequence: the played-game seeds **skip exactly one number**,
  the subject's own. That is not a missing game.

- **Standings lead the report.** `league_section` renders ONE table, passed as
  `render_report(..., lead_sections=[...])` so it sits directly under the header,
  above Aggregate Stats. Its row order is the division's **live standing order
  and must always match what woogles.io shows at that moment** - never seed
  order, which is why seed is a column rather than the sort key. It lists every
  participant (not just the ones Jesse played), with each player's live CSW
  correspondence rating (`division_ratings`, one GetRatings per player) and seed
  (his own row included), and folds Jesse's own result into a Head-to-Head
  column that links to the game; players he hasn't played show "—".

  The heading is **"Final Standings"** only when `division_complete(division)`
  (the platform's `is_complete`, or every standing at `games_remaining == 0`) and
  plain **"Standings"** otherwise - and the digest says "provisional standing,
  not a final placing" mid-season so no summary reports an in-progress position
  as a finish. The only prose under the heading is "Rating is CSW correspondence
  rating."; keep it that short. The league-wide mistakes leaderboard stays a
  trailing `sections` entry.

- **Cross-check.** `woogles_league.report_extras(uuid, stats, agg)` returns the
  render arguments plus a digest line. **Call it unconditionally** - it returns
  `{}` for any non-league collection. It compares 11 computed figures against the
  division standings table, but that comparison is **background QA, not report
  content**: every row prints to stderr (the Actions log), and the report section
  and digest line stay silent unless a figure disagrees, in which case the full
  table plus a warning comes back. Jesse asked for this (2026-07-25) - a passing
  cross-check restated in every email is noise. Don't reinstate the always-on
  table, and don't have a summary praise the numbers for matching.

- **Played, not annotated.** These are real games: `is_annotated` is false, so
  they live at `/game/<id>` (not `/anno/<id>`), and both players' racks are fully
  known, so `opp_fully_annotated` is true for every game and the opponent columns
  are real numbers rather than "—".

League play is `VOID` challenge rule. See the VOID rules in
`report-semantics.md` before writing any prose about phonies or mistakes scores.
