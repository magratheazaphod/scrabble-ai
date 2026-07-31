---
name: tournament-analysis
description: Pull BestBot-analyzed stats (mistakes score, scores, bingos, blanks drawn) out of a woogles.io game collection and turn them into a tournament report. Use this whenever Jesse asks to analyze a Woogles collection/tournament, asks for his "mistakes" score, bingo counts, or wants a report/spreadsheet covering games annotated on woogles.io. Also use proactively when Jesse mentions adding games to a Woogles collection or wants every tournament on his profile covered with a report.
---

# Woogles.io tournament analysis

Turn a Woogles **collection** into a stats report: record, scores, mistakes
score, bingos, blanks drawn, endgame spread lost, win% lost, phonies, and missed
bingos.

Auth, base URL, retry/concurrency policy, and the analysis-quota rules are in
`CLAUDE.md`. Reference material for this skill:

- `reference/api-fields.md` - verified response structure for every field used here
- `reference/report-semantics.md` - what each figure means and how it must be presented
- `reference/leagues.md` - Woogles league seasons (seeding, standings, cross-check)

**Steps 5-8 are already implemented.** `scripts/tournament_report.py` is the
single source of truth for stats, aggregation, and rendering, shared with the
email automation. Import and call it; never re-derive the logic.

## Step 1 - enumerate collections

```python
resp = woogles('collections_service.CollectionsService/GetUserCollections', {'limit': 50, 'offset': 0})
for c in resp['collections']:
    print(c['title'], c['game_count'], 'games —', c['uuid'])
```

## Step 2 - list games in the target collection

```python
resp = woogles('collections_service.CollectionsService/GetCollection', {'collection_uuid': '<uuid>'})
games = resp['collection']['games']
```

Round numbers come from `chapter_title`, not `chapter_number`; `is_annotated`
does not mean "analyzed". See `reference/api-fields.md`.

## Step 3 - check and request analysis

```python
def check_status(g):
    r = woogles('analysis_service.AnalysisService/GetAnalysisStatus', {'game_id': g['game_id']})
    return {'title': g['chapter_title'], 'game_id': g['game_id'], 'status': r.get('status')}

with ThreadPoolExecutor(max_workers=6) as ex:
    statuses = list(ex.map(check_status, games))
```

For any not `COMPLETED`, call `RequestAnalysis` with
`{"game_id": "<id>", "force": false}`. Response `status` values:

- `SUCCESS` - queued; `message` has queue position
- `ALREADY_REQUESTED` - already in queue; poll
- `RATE_LIMITED` - rolling-24h cap hit (below)
- `GAME_NOT_ENDED`, `NOT_A_PLAYER`, `INVALID_VARIANT` - skip and **tell Jesse**
  rather than silently dropping the game

Analysis takes 2-10 minutes per game (Monte-Carlo simulation). Queue every
pending game first, then poll the batch every ~30 seconds.

**On hitting the limit:** stop requesting, write a progress file at
`state/<collectionId>.json` recording which games are `COMPLETED`, pending, or
analyzed-but-not-aggregated, and tell Jesse the counts. Then tell him the daily
pipeline will pick this collection up automatically - **do not offer to set up a
scheduled task, `/loop`, or cloud routine for it** (see the automation section in
`CLAUDE.md`). On a resumed run, read the progress file and only request the
pending games.

## Step 4 - fetch stats for all games

```python
def fetch_game(g):
    with ThreadPoolExecutor(max_workers=2) as ex:
        fa = ex.submit(woogles, 'analysis_service.AnalysisService/GetAnalysisResult', {'game_id': g['game_id']})
        fh = ex.submit(woogles, 'game_service.GameMetadataService/GetGameHistory',    {'game_id': g['game_id']})
    return {'meta': g, 'analysis': fa.result(), 'history': fh.result()}

with ThreadPoolExecutor(max_workers=5) as ex:   # nested with the inner pool: caps at 10 concurrent
    results = list(ex.map(fetch_game, games))
```

## Steps 5-8 - compute, aggregate, render

```python
import sys
sys.path.insert(0, 'scripts')
from tournament_report import (
    compute_game, check_phony_words, aggregate, game_notes, render_report, build_digest,
)

stats = [compute_game(r) for r in results]   # r is {'meta', 'analysis', 'history'}
stats.sort(key=lambda g: g['round'])
check_phony_words(stats)                     # populates invalid_words on phony entries
agg = aggregate(stats)
notes = game_notes(stats)                    # owns the missed_bingo_counter internally

report_md = render_report(stats, agg, notes, title)
```

In interactive use, write the Summary paragraph yourself (no API sub-call needed)
and pass it as `render_report(..., summary_md=your_summary_text)`. Always call
`woogles_league.report_extras(uuid, stats, agg)` too - it returns `{}` for
non-league collections (see `reference/leagues.md`).

Save to `reports/<tournament-slug>-report.md`. For a one-off subject report
(someone other than Jesse), pass
`subject={"nickname": ..., "real_name": ...}` to `compute_game` - it re-keys
identity via `GameHistory players[].nickname` matching, never the Woogles login
username.

**`scripts/test_report.py` is the regression gate.** Run it after any edit to
`tournament_report.py`; it needs no network and no `data/`.

It is **not** a golden-output diff - the previous harness was, and that was the
wrong shape for a file edited most weeks (every deliberate change failed it, so
regenerating the expectation became reflex; it then broke silently and passed for
weeks when the state file stopped storing rendered reports). Instead it asserts
what the report *means* and how it is *shaped*:

- **Invariants** re-derive each figure independently - scores read from the right
  player index, `mistake_index` selected by `player_index`, bingos recounted off
  the event log, error rows belonging to the subject's own non-optimal turns,
  endgame equity equal to `spread_loss`. Adding a column moves none of them.
- **Structural checks** assert every table row matches its header's column count,
  no `None`/`nan` reaches a cell, and every link points at woogles.io - without
  pinning one word of text.
- **Synthetic cases** cover branches the corpus lacks (draws, VOID, a registry
  hit) by mutating fixture input, never by hand-writing an expected dict.

Numbers are deliberately not pinned; a pinned constant only records what the code
did the day it was written. If you want a figure locked down, add an invariant
that derives it another way.

The corpus is `tests/fixtures/*.json.gz` (committed, ~500 KB, 20 games across 4
collections). It is **anonymized**: every non-subject identity is replaced,
`user_id`s and game ids are synthetic, and `original_gcg` is dropped - a fixture
pairing `real_name`/`nickname`/`user_id` would be a reconstruction of the private
name registry. Jesse's own aliases are kept deliberately, since `is_jesse` is the
production identity path. Rebuild with `scripts/make_test_fixtures.py` (needs the
snapshot); it refuses to write if any real name survives.

## Running without woogles.io egress

Some cloud agent environments can't reach `woogles.io`. `woogles-snapshot.yml`
publishes a snapshot to:

```
https://raw.githubusercontent.com/magratheazaphod/scrabble-ai/woogles-data/data/woogles-snapshot.json
```

Shape: `{"collections": [{"uuid", "title", "games": [{"meta", "analysis",
"history"}]}], "pending": [{"title", "done", "total"}]}` - `meta`/`analysis`/
`history` are exactly the `GetCollection` entry, `GetAnalysisResult`, and
`GetGameHistory` bodies. `collections` only includes collections where every
game is analysis-complete or skip-eligible; anything still pending shows up in
`pending` with no per-game data.

Fetch this instead of calling the live API (skip Steps 1-4) and start at Step 5
with its `collections`/`pending` arrays in place of `results`.

## Testing changes to `fetch_woogles_snapshot.py`

A bare run walks **every** collection on the profile: ~700 HTTP reads and ~2
minutes at the current ~230-game archive, enough to blow past a 120s command
timeout. None of it spends quota (all reads), but it's a wasteful way to test a
code path and Jesse has called it out as such (2026-07-22).

```bash
TARGET_COLLECTION_UUID=<uuid> python3 scripts/fetch_woogles_snapshot.py
TARGET_USERNAME=magrathean  python3 scripts/fetch_woogles_snapshot.py
```

Pin the rate-limit marker forward first so Phase 2 issues no `RequestAnalysis`
calls:

```bash
python3 -c "from datetime import datetime,timezone,timedelta; open('data/rate-limited-until.txt','w').write((datetime.now(timezone.utc)+timedelta(hours=2)).isoformat())"
```

For decision logic (which games to re-request, whether an analysis has drifted),
skip the script entirely and call the helpers directly - `failed_needs_request()`,
`fingerprint_events()`, and `history_fingerprint()` are pure enough to exercise
against one game plus a hand-built state dict, and a `copy.deepcopy` of one real
history lets you mutate a rack/score/event order to prove the fingerprint is
sensitive to each. Seconds, not minutes.

Afterwards: clear `data/rate-limited-until.txt`, and remember
`data/woogles-snapshot.json` is Jesse's local file - back it up before a test run
overwrites it.

## Before a big sweep

For very large collections (50+ games), check with Jesse before committing to a
full analysis sweep in one sitting.
