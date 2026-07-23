---
name: tournament-analysis
description: Pull BestBot-analyzed stats (mistakes score, scores, bingos, blanks drawn) out of a woogles.io game collection and turn them into a tournament report. Use this whenever Jesse asks to analyze a Woogles collection/tournament, asks for his "mistakes" score, bingo counts, or wants a report/spreadsheet covering games annotated on woogles.io. Also use proactively when Jesse mentions adding games to a Woogles collection or wants every tournament on his profile covered with a report.
---

# Woogles.io Tournament Analysis

Jesse Day (woogles.io username `magrathean`, in-game display names vary: "Jesse Day", "Jesse", "JD", "JesseD") plays tournaments on woogles.io and groups the games for each tournament into a **collection**. This skill turns a collection into a stats report: record, scores, mistakes score, bingos, blanks drawn, endgame spread lost, win% lost, phonies, and missed bingos.

## Auth

Use the `X-Api-Key` header directly — no browser needed.

```
API_KEY = $WOOGLES_API_KEY   # stored in .env at project root (gitignored)
Base URL: https://woogles.io/api/<package>.<service>/<RpcName>
Every call is POST with Content-Type: application/json
```

Full schema reference: `https://buf.build/domino14/liwords/docs`.

### Automated/cloud runs without woogles.io egress

Some cloud agent environments (e.g. the CCR scheduled routine) can't reach `woogles.io` directly. For those, a GitHub Action (`.github/workflows/woogles-snapshot.yml`) polls the live API on a schedule and publishes a snapshot to:

```
https://raw.githubusercontent.com/magratheazaphod/scrabble-ai/woogles-data/data/woogles-snapshot.json
```

Shape: `{"collections": [{"uuid", "title", "games": [{"meta", "analysis", "history"}]}], "pending": [{"title", "done", "total"}]}` — `meta`/`analysis`/`history` are exactly the `GetCollection` game entry, `GetAnalysisResult`, and `GetGameHistory` response bodies described below. `collections` only includes collections where every included game is analysis-complete (or skip-eligible); anything still pending analysis shows up in `pending` instead, with no per-game data.

When running somewhere without `woogles.io` access, fetch this snapshot instead of calling the live API (skip Steps 1–4 below) and start directly at Step 5 using its `collections`/`pending` arrays in place of `results`/deferred collections. Steps 5–8 (stats computation, aggregation, report template) apply identically regardless of data source.

---

## Verified API response structure (confirmed June 2026)

**`GetAnalysisResult`** response:
```
response["result"]["player_summaries"][]
response["result"]["turns"][]
```

**`GetGameHistory`** response:
```
response["history"]["players"][]
response["history"]["events"][]
response["history"]["final_scores"][]
response["history"]["last_known_racks"][]
```

### player_summaries[] fields
- `player_name` — camelCase-merged (e.g. `"JD"`, `"MichaelDonegan"`). Differs from `nickname` in GameHistory.
- `mistake_index` — the "Mistakes" score shown in the UI. **Null/absent for annotated games.** Always handle None gracefully.
- `estimated_elo`

**Full annotation must be detected from rack completeness, NOT from mistake_index.** A non-null opponent `mistake_index` is **not** a reliable signal that the opponent's side was fully tracked — Woogles computes a mistake score even when it only knows the tiles the opponent *played* each turn (confirmed Causeway 2026: all 19 games had a non-null opponent mistake_index, but only the 3 games annotated for both sides had real full-rack data). In a partially-annotated game, the opponent's recorded `rack` on each turn is just their played tiles (2–6 letters), so the trustworthy signal is: **every opponent turn shows a full 7-tile rack**, allowing legitimately short racks only once the bag is empty (`tiles_in_bag == 0`). See `opp_racks_complete` in Step 5. Additionally the game must have concluded (`history["play_state"] == "GAME_OVER"`) — an in-progress or aborted game can carry stats that aren't meaningful final numbers. When a game fails either check, show "—" for that game's opponent stats rather than a partial number.

### turns[] fields
- `player_index` — 0 or 1; use for reliable Jesse identification
- `rack` — Jesse's rack for this turn (as seen by analysis; may differ from history in rare cases — validate against history)
- `tiles_in_bag` — 0 once bag is empty (endgame phase)
- `played_is_bingo` — bool; prefer over parsing move notation
- `optimal_is_bingo` — bool; whether BestBot's top move was a bingo
- `spread_loss` — spread points lost vs BestBot's optimal move
- `win_prob_loss` — fraction of win probability lost (0–1); sum Jesse's turns → total win% lost
- `mistake_size` — `NO_MISTAKE`, `SMALL`, `MEDIUM`, `LARGE`
- `is_phony` — bool; played word is not in the lexicon
- `phony_challenged` — bool; phony was challenged off
- `missed_bingo` — bool; Jesse had a bingo available but didn't play it
- `played_move`, `played_score`, `optimal_move`, `optimal_score`
  - Move format: `"8D WORD"` (position + tiles placed; lowercase = blank; `.` = existing board tile)

### events[] fields (from GetGameHistory)
- `type` — `TILE_PLACEMENT_MOVE`, `EXCHANGE`, `PASS`, `END_RACK_PTS`, `PHONY_TILES_RETURNED`, `CHALLENGE_BONUS`, etc.
- `player_index`
- `rack` — player's rack *before* the move (authoritative; use for missed-bingo validation)
- `played_tiles` — tiles placed (lowercase = blank played as that letter; `.` = board tile reused)
- `row`, `column` — 0-indexed board position of the first tile
- `direction` — `"HORIZONTAL"` or `"VERTICAL"`
- `position` — string notation e.g. `"8G"` (row 8, col G, horizontal) or `"G8"` (col G, row 8, vertical)
- `exchanged` — tiles exchanged (`?` = blank)

### Move count alignment
History `events[]` always has **one extra move** vs `analysis["turns"]` — the final PASS that ends the game appears in events but has no analysis turn. So `move_snapshots[turn_idx]` correctly indexes the board state before `analysis["turns"][turn_idx]` for all valid turn indices.

`PHONY_TILES_RETURNED` and `CHALLENGE_BONUS` are not counted as moves and don't consume snapshot slots.

---

## Workflow

Use the Bash tool with Python throughout. Use `requests` (not `urllib.request` — SSL issues on macOS Python 3.12).

### Helper — put this at the top of every Python snippet

```python
import json, os, re, time, random, requests
from concurrent.futures import ThreadPoolExecutor

API_KEY = os.environ['WOOGLES_API_KEY']  # load from .env at project root (gitignored)
BASE    = 'https://woogles.io/api'
HDRS    = {'Content-Type': 'application/json', 'X-Api-Key': API_KEY}

def woogles(endpoint, body, retries=4):
    """POST to a woogles.io RPC endpoint, retrying transient overload/rate-limit
    responses with exponential backoff + jitter. Steps 3 and 4 below fan this out
    across 10-20 concurrent games — without backoff, a burst that trips woogles.io's
    own rate limiting turns into a hard failure instead of a brief slowdown."""
    for attempt in range(retries):
        r = requests.post(f'{BASE}/{endpoint}', json=body, headers=HDRS)
        if r.status_code == 429 or r.status_code >= 500:
            if attempt == retries - 1:
                r.raise_for_status()
            time.sleep((2 ** attempt) + random.uniform(0, 0.5))
            continue
        r.raise_for_status()
        return r.json()
```

**Concurrency note:** Steps 3 and 4 fan this helper out concurrently (nested `ThreadPoolExecutor`s), capped at 6 and 10 concurrent requests respectively — down from the original 10/20, which is the most likely place this workflow would trip a third-party rate limit on a large collection. If `woogles()` still raises 429/503 after the retries above, drop `max_workers` further (e.g. to 3-4) before re-running rather than retrying immediately at the same concurrency.

### Step 1: Enumerate collections

```python
resp = woogles('collections_service.CollectionsService/GetUserCollections', {'limit': 50, 'offset': 0})
for c in resp['collections']:
    print(c['title'], c['game_count'], 'games —', c['uuid'])
```

Page through with `offset` if `len(collections) == limit`. **More reliable than the profile page** — the profile widget has been observed to omit collections that exist in the API.

### Step 2: List games in the target collection

```python
resp = woogles('collections_service.CollectionsService/GetCollection', {'collection_uuid': '<uuid>'})
games = resp['collection']['games']
for i, g in enumerate(games):
    print(i, g['chapter_title'], g['game_id'])
```

Each game has `game_id`, `chapter_number`, `chapter_title`, `is_annotated`.

**Round numbers:** `chapter_number` is position within the collection (1-indexed), **not** the tournament round. Extract the real round from `chapter_title` (e.g. "Rd 7 Vannitha vs JD" → Round 7). Note missing rounds in the report header.

**`is_annotated`** = has commentary notes — does **not** indicate BestBot analysis. Always check separately.

### Step 3: Check and request analysis

```python
def check_status(g):
    r = woogles('analysis_service.AnalysisService/GetAnalysisStatus', {'game_id': g['game_id']})
    return {'title': g['chapter_title'], 'game_id': g['game_id'], 'status': r.get('status')}

with ThreadPoolExecutor(max_workers=6) as ex:
    statuses = list(ex.map(check_status, games))

for s in statuses:
    print(s['title'], '→', s['status'])
```

For any not `COMPLETED`, call `RequestAnalysis` with `{"game_id": "<id>", "force": false}`. Response `status` values:
- `SUCCESS` — queued; `message` has queue position
- `ALREADY_REQUESTED` — already in queue; poll
- `RATE_LIMITED` — **daily cap hit** (see below)
- `GAME_NOT_ENDED`, `NOT_A_PLAYER`, `INVALID_VARIANT` — skip and tell Jesse

Analysis takes 2–10 minutes per game (Monte-Carlo simulation). Queue all pending games first, then poll the whole batch every ~30 seconds.

#### Hitting the daily limit
1. Stop requesting new analyses.
2. Write a progress file at `.claude/skills/tournament-analysis/state/<collectionId>.json` recording which games are `COMPLETED`, which are pending, and which are analyzed but not yet aggregated.
3. Tell Jesse how many are done and how many are waiting. **Do not offer to set up a new scheduled task (`schedule` skill, `/loop`, a cloud routine, etc.) for this** — a daily resume-and-report pipeline already exists and requires no new setup (see "Existing automated pipeline" below). Just tell Jesse it'll pick this collection up automatically.
4. On a resumed run, read the progress file and only request analysis for pending games.

### Existing automated pipeline — don't build a new one

`.github/workflows/woogles-report.yml` already runs a GitHub Actions cron multiple times a day (retrying every 30 min), calling `scripts/fetch_woogles_snapshot.py` to request analysis for any pending games across **every collection on Jesse's profile** (respecting the rolling 24h rate limit), then `scripts/generate_report_email.py` to build and email the report once a collection is fully analyzed. State lives in `.github/report-state.json` (committed to master) plus markers on the `woogles-data` branch. Any newly created collection is picked up automatically on the next run — no per-tournament setup, scheduled task, or cloud routine is ever needed to "resume" or "finish" analysis for a collection. If Jesse asks about a stalled/incomplete report, check this workflow's state/runs rather than proposing new automation.

### Testing changes to `fetch_woogles_snapshot.py` — scope the run, don't sweep the archive

A bare `python3 scripts/fetch_woogles_snapshot.py` walks **every** collection on Jesse's profile: a `GetAnalysisStatus` per game in Phase 1, then a `GetAnalysisResult` **and** a `GetGameHistory` per analyzed game in Phase 3. That is ~700 HTTP reads and ~2 minutes per run at the current ~230-game archive — enough to blow past a 120s command timeout. None of it spends analysis quota (they're all reads), but it is a wasteful way to test a code path, and Jesse has called it out as such (2026-07-22).

Scope the run instead. Two env vars, both already honoured by the script:

```bash
# one collection instead of eleven
TARGET_COLLECTION_UUID=55b29df3-10fd-471b-9e87-135ed5bbb2f6 python3 scripts/fetch_woogles_snapshot.py
TARGET_USERNAME=magrathean python3 scripts/fetch_woogles_snapshot.py   # a whole profile, still narrower than the default
```

**Pin the rate-limit marker forward before any test run** so Phase 2 issues no `RequestAnalysis` calls and the test can't eat into the 15/day rolling budget:

```bash
python3 -c "from datetime import datetime,timezone,timedelta; open('data/rate-limited-until.txt','w').write((datetime.now(timezone.utc)+timedelta(hours=2)).isoformat())"
```

For decision logic (which games to re-request, whether an analysis has drifted), skip the script entirely and call the helpers directly — `failed_needs_request()`, `fingerprint_events()`, `history_fingerprint()` are all pure enough to exercise against one game plus a hand-built state dict, and a `copy.deepcopy` of one real history lets you mutate a rack/score/event order to prove the fingerprint is sensitive to each. That's seconds, not minutes.

Restore afterwards: clear `data/rate-limited-until.txt`, and remember `data/woogles-snapshot.json` is Jesse's local file — back it up before a test run overwrites it.

### Step 4: Fetch stats for all games

```python
def fetch_game(g):
    with ThreadPoolExecutor(max_workers=2) as ex:
        fa = ex.submit(woogles, 'analysis_service.AnalysisService/GetAnalysisResult',      {'game_id': g['game_id']})
        fh = ex.submit(woogles, 'game_service.GameMetadataService/GetGameHistory',          {'game_id': g['game_id']})
    return {'meta': g, 'analysis': fa.result(), 'history': fh.result()}

with ThreadPoolExecutor(max_workers=5) as ex:  # nested with the inner pool above, this caps at 10 concurrent requests
    results = list(ex.map(fetch_game, games))
```

### Steps 5-8: Compute stats, aggregate, generate notes, render the report

These are implemented in `scripts/tournament_report.py` — import and call it; do not
re-type or re-derive the logic. That module is the single source of truth (shared with
the `generate_report_email.py` automation), and `scripts/test_report_golden.py` is the
regression gate for any change to it — run it after editing the module.

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
# In interactive use, write the Summary paragraph yourself (no API sub-call needed) and
# pass it as render_report(..., summary_md=your_summary_text).
```

Save the result to `<project root>/reports/<tournament-slug>-report.md`. For a one-off
subject report (someone other than Jesse), pass `subject={"nickname": ..., "real_name": ...}`
to `compute_game` — it re-keys identity via `GameHistory players[].nickname` matching,
never the Woogles login username.

---

## Notes
- **Opponent name:** prefer `real_name` from `history["players"]` (already has spaces). Handle "Last, First" format by flipping. Use `chapter_title` parsing as a cross-check when `real_name` looks abbreviated (single word vs multi-word in title).
- **Sanity-check identity:** confirm Jesse is correctly identified in every game before computing stats — a wrong `jesse_idx` silently flips all per-game numbers.
- Surface `NOT_A_PLAYER`, `GAME_NOT_ENDED`, or `INVALID_VARIANT` errors from `RequestAnalysis` to Jesse rather than silently skipping.
- For very large collections (50+ games), check with Jesse before committing to a full analysis sweep in one sitting.
- **Endgame spread lost context:** values of 20+ in a single game are worth flagging — use per-turn `played_move` / `optimal_move` / `spread_loss` to explain what happened.
- **Win% Lost:** sum of `win_prob_loss` over Jesse's turns × 100. High in a win = nearly gave it away; high in a close loss = structural deficit, not bad luck.
- **Phonies:** `is_phony` = word not in lexicon (games are configured for CSW — the lexicon Jesse always plays, confirmed via `history['lexicon']`, e.g. `CSW21`/`CSW24`); check it for BOTH players, not just Jesse (`analysis['turns'][i]['is_phony']` is the authoritative flag — don't infer phony-ness from the event log alone, since an *unchallenged* phony has no `PHONY_TILES_RETURNED` event and still scores). If `total_phonies == 0`, omit "Games per Phony Played" from the report. Every phony gets named in the per-game note with a trailing `*` (`phony WORD*`, or `phony WORD* (unchallenged)` if it wasn't caught); opponent phonies are attributed by the opponent's name from the Opponent column. **Multi-word plays:** when the play formed more than one word (`event['words_formed']` has >1 entry — e.g. a bingo crossing several tiles, or a short play forming a cross word), show ALL of them joined by `/` before the `*` (e.g. `GU/PU*`) — do NOT assume the primary/longest word is the invalid one. This bit Jesse: two "phonies" (GU, LINUX) turned out to be valid CSW words, and the actual violation was almost certainly the cross word (PU, NEEL) formed alongside them. Never assert which specific word was invalid; let Jesse read the full set and judge for himself.
- **Missed bingo validation:** always cross-check `missed_bingo` against the history event rack. The analysis occasionally stores an incorrect rack, causing a false-positive (confirmed in Causeway R2: analysis showed BEEIORZ, actual was BEELORZ). If `validate_bingo(om, history_rack)` returns False, skip the entry.
- **Nigel Richards' "missed" bingos:** when the opponent is Nigel Richards, phrase his passed-over bingos as "passed up" rather than "missed" in game notes (`game_note`'s `miss_verb` checks for `'nigel' in opp_name.lower()`) — he sees them and chooses not to play them, not a genuine oversight. Applies only to the opponent-attributed note text; Jesse's own missed-bingo wording and the Missed Bingos table (which only tracks Jesse's) are unaffected.
- **Opponent mistake index / Win% Lost:** only trustworthy when `opp_fully_annotated` is True, which requires all three of: opponent's `mistake_index` non-null in `player_summaries`, the game actually finished (`history["play_state"] == "GAME_OVER"`), and **`opp_racks_complete` — a full 7-tile rack on every opponent turn (short racks allowed only when the bag is empty)**. The rack check is the one that actually discriminates: Woogles infers partial racks from played tiles and scores them anyway, so mistake_index is non-null even for single-side annotations (confirmed Causeway 2026 — 19/19 games had non-null opp mistake_index, only 3 were fully annotated). Tournament-level averages (`avg_opp_mi`, `avg_opp_wpl`) must only include `opp_fully_annotated` games, not the full collection.
- **Board reconstruction:** `build_snapshots_and_racks` runs in ~2ms per 19-game tournament and uses zero Claude tokens. Prefer it over dictionary lookup for missed-bingo word resolution.
- **Game URL:** `https://woogles.io/anno/<game_id>`
- **Win/Loss Progression:** a single line of 🟩 (win) / 🟥 (loss) boxes, one per game in chronological order, no round numbers and no labels. Group into blocks of 5 separated by a space for readability (no separator within a block). Built directly from `stats` (already sorted by round):
  ```python
  boxes = ['🟩' if g['result']=='W' else '🟥' for g in stats]
  progression = ' '.join(''.join(boxes[i:i+5]) for i in range(0, len(boxes), 5))
  ```
  Placed immediately after the header line, before Aggregate Stats — no section header of its own.
