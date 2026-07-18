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

### Step 5: Compute per-game stats

```python
import re

def is_jesse(p):
    nick = (p.get('nickname') or '').lower().replace('_', '')
    real = (p.get('real_name') or '').lower()
    uid  = (p.get('user_id')  or '').lower()
    return nick in ('jd', 'jessed', 'jesseday') or 'jesse' in real or uid == 'magrathean'

def summary_for_index(analysis, player_index):
    """Select a `player_summaries` entry by turn `player_index`, not by name.
    `player_summaries` only carries `player_name` (a nickname that varies —
    "JD", "JesseDay", bare "Jesse"), so an allowlist silently drops variants it
    omits (this dropped Jesse's mistake score in 18 King's Cup 2019 games). Turns
    carry both `player_name` and `player_index` and match summaries exactly, so
    pair them to tie the summary to the already-robust `jesse_idx`/`opp_idx`."""
    names_at_index = {t['player_name'] for t in analysis['turns']
                      if t.get('player_index') == player_index}
    return next((s for s in analysis['player_summaries']
                 if s.get('player_name') in names_at_index), None)

def format_real_name(r):
    if not r:
        return ''
    m = re.match(r'^(.+),\s*(.+)$', r)
    if m:
        first = re.sub(r'[A-Z]$', '', m.group(2)).strip()  # strip trailing initial
        return f'{first} {m.group(1)}'
    return r

def get_opp_name(meta, players, jesse_idx):
    opp   = players[1 - jesse_idx]
    real  = format_real_name(opp.get('real_name', ''))
    title = re.sub(r'^\([^)]+\)\s*', '', meta['chapter_title'])
    title = re.sub(r'^(Round\s+\d+|Rd\s*\d+)\s*[-–]?\s*', '', title, flags=re.I)
    m = re.match(r'^(.+?)\s+vs\.?\s+(.+)$', title, re.I)
    title_name = None
    if m:
        p1, p2 = m.group(1).strip(), m.group(2).strip()
        jesse_names = {'jd', 'jesse', 'jessed'}
        is_j1 = p1.lower().replace(' ','').replace('_','') in jesse_names
        is_j2 = p2.lower().replace(' ','').replace('_','') in jesse_names
        if is_j2 and not is_j1: title_name = p1
        elif is_j1 and not is_j2: title_name = p2
    # Prefer real_name when more complete (has a space but title_name doesn't)
    if title_name:
        if real and ' ' in real and ' ' not in title_name:
            return real
        return title_name
    if real and ' ' in real:
        return real
    return real or (opp.get('nickname') or '').replace('_', ' ')

def build_snapshots_and_racks(events):
    """Board state (15×15) and rack BEFORE each analysis turn.

    Returns (snapshots, racks). Index i corresponds to analysis turns[i].
    History always has one extra move (the final PASS) with no matching analysis turn —
    this means snapshots[turn_idx] is always correctly aligned.
    PHONY_TILES_RETURNED: tiles NOT placed on board (play was challenged off).
    CHALLENGE_BONUS: not a move, skipped.
    """
    board = [['' for _ in range(15)] for _ in range(15)]
    snapshots, racks = [], []
    i = 0
    while i < len(events):
        ev = events[i]
        et = ev.get('type', '')
        if et == 'TILE_PLACEMENT_MOVE':
            snapshots.append([row[:] for row in board])
            racks.append(ev.get('rack') or '')
            if i + 1 < len(events) and events[i+1].get('type') == 'PHONY_TILES_RETURNED':
                i += 2; continue  # phony: snapshot taken, board not updated
            dr = 1 if ev['direction'] == 'VERTICAL' else 0
            dc = 0 if ev['direction'] == 'VERTICAL' else 1
            r2, c2 = ev['row'], ev['column']
            for ch in (ev.get('played_tiles') or ''):
                if ch != '.':
                    board[r2][c2] = ch.upper()
                r2 += dr; c2 += dc
        elif et in ('EXCHANGE', 'PASS'):
            snapshots.append([row[:] for row in board])
            racks.append(ev.get('rack') or '')
        i += 1
    return snapshots, racks

def validate_bingo(optimal_move, history_rack):
    """Return False if history rack cannot supply the non-board tiles in optimal_move.

    Catches rare analysis bugs where the analysis rack differs from the actual game rack,
    causing false-positive missed_bingo flags.
    """
    parts = optimal_move.strip().split()
    if len(parts) < 2:
        return True
    word = parts[1]
    needed = [ch.upper() for ch in word if ch != '.']
    rack = list(history_rack.upper())
    for ch in needed:
        if ch in rack:
            rack.remove(ch)
        elif '?' in rack:
            rack.remove('?')
        else:
            return False
    return True

def resolve_bingo_word(optimal_move, board):
    """Replace '.' in optimal_move word with '(X)' where X is the board tile at that position.

    Position format:
      '8G WORD'  → horizontal, row 8 (0-indexed: 7), col G (0-indexed: 6)
      'G8 WORD'  → vertical,   col G (0-indexed: 6), row 8 (0-indexed: 7)
    Lowercase letters = blank played as that letter (preserved).
    (X) = board tile X was already there.
    """
    parts = optimal_move.strip().split()
    if len(parts) < 2:
        return optimal_move
    position, word = parts[0], parts[1]
    mh = re.match(r'^(\d+)([A-Oa-o])$', position)
    mv = re.match(r'^([A-Oa-o])(\d+)$', position)
    if mh:
        row = int(mh.group(1)) - 1
        col = ord(mh.group(2).upper()) - ord('A')
        dr, dc = 0, 1
    elif mv:
        col = ord(mv.group(1).upper()) - ord('A')
        row = int(mv.group(2)) - 1
        dr, dc = 1, 0
    else:
        return word
    result = ''
    r, c = row, col
    for ch in word:
        if ch == '.':
            tile = board[r][c] if 0 <= r < 15 and 0 <= c < 15 else ''
            result += f'({tile})' if tile else '(?)'
        else:
            result += ch
        r += dr; c += dc
    return result

def build_played_words(events):
    """Resolve the actual word played on each turn (same index-skip trick as
    build_snapshots_and_racks, so index i lines up with analysis['turns'][i]).

    `challenged` is True only when the event log shows the play was challenged off
    (PHONY_TILES_RETURNED). It is NOT the same as "is this a phony" — an unchallenged
    phony stands on the board and is_phony must come from analysis['turns'][i]['is_phony'].

    `words_formed` is the event's own list of every word the play created (main word +
    any cross words). IMPORTANT: when a phony play forms more than one word, the analysis
    is_phony flag only tells you the PLAY was illegal, not WHICH of the formed words was
    the actual violation — don't assume it's the primary/longest one. Show all of them
    (see game_note's `*`-marking below) rather than guessing.

    Returns a list of {'word': str|None, 'words_formed': list[str], 'challenged': bool,
    'player_index': int}. word/words_formed are None/[] for EXCHANGE/PASS turns.
    """
    board = [['' for _ in range(15)] for _ in range(15)]
    moves = []
    i = 0
    while i < len(events):
        ev = events[i]
        et = ev.get('type', '')
        if et == 'TILE_PLACEMENT_MOVE':
            challenged = i + 1 < len(events) and events[i+1].get('type') == 'PHONY_TILES_RETURNED'
            word = resolve_bingo_word(f"{ev['position']} {ev.get('played_tiles') or ''}", board)
            moves.append({'word': word, 'words_formed': ev.get('words_formed') or [word],
                           'challenged': challenged, 'player_index': ev['player_index']})
            if challenged:
                i += 2; continue
            dr = 1 if ev['direction'] == 'VERTICAL' else 0
            dc = 0 if ev['direction'] == 'VERTICAL' else 1
            r2, c2 = ev['row'], ev['column']
            for ch in (ev.get('played_tiles') or ''):
                if ch != '.':
                    board[r2][c2] = ch.upper()
                r2 += dr; c2 += dc
        elif et in ('EXCHANGE', 'PASS'):
            moves.append({'word': None, 'words_formed': [], 'challenged': False, 'player_index': ev.get('player_index')})
        i += 1
    return moves

def opp_racks_complete(analysis_turns, opp_idx):
    """True iff the opponent's rack is fully known on every turn.

    In a partially-annotated game (only Jesse's side tracked), the opponent's
    recorded `rack` is just the tiles they played that turn — so any opponent
    rack shorter than 7 while the bag still has tiles marks the game partial.
    Short racks are legitimate only once the bag is empty (endgame)."""
    return all(
        len(t.get('rack') or '') == 7 or (t.get('tiles_in_bag') or 0) == 0
        for t in analysis_turns if t.get('player_index') == opp_idx
    )

def compute_game(r):
    meta     = r['meta']
    history  = r['history']['history']
    analysis = r['analysis']['result']
    events   = history.get('events') or []

    jesse_idx = next(i for i, p in enumerate(history['players']) if is_jesse(p))
    opp_idx   = 1 - jesse_idx

    jesse_score = history['final_scores'][jesse_idx]
    opp_score   = history['final_scores'][opp_idx]
    opp_name    = get_opp_name(meta, history['players'], jesse_idx)
    game_url    = f'https://woogles.io/anno/{meta["game_id"]}'

    summary      = summary_for_index(analysis, jesse_idx)
    mistake_index = summary['mistake_index'] if summary else None

    opp_summary        = summary_for_index(analysis, opp_idx)
    opp_mistake_index  = opp_summary['mistake_index'] if opp_summary else None
    game_is_over        = history.get('play_state') == 'GAME_OVER'
    opp_fully_annotated = (opp_mistake_index is not None and game_is_over
                           and opp_racks_complete(analysis['turns'], opp_idx))  # full 7-tile racks every opp turn AND game finished — see note above

    jesse_bingos = opp_bingos = 0
    for t in analysis['turns']:
        if t.get('played_is_bingo'):
            if t['player_index'] == jesse_idx: jesse_bingos += 1
            else: opp_bingos += 1

    jesse_blanks = 0
    for ev in (history.get('events') or []):
        if ev['player_index'] != jesse_idx:
            continue
        if ev['type'] == 'TILE_PLACEMENT_MOVE':
            jesse_blanks += sum(1 for c in (ev.get('played_tiles') or '') if c.islower())
        elif ev['type'] == 'EXCHANGE':
            jesse_blanks += (ev.get('exchanged') or '').count('?')
    last_racks = history.get('last_known_racks') or []
    if len(last_racks) > jesse_idx and last_racks[jesse_idx]:
        jesse_blanks += last_racks[jesse_idx].count('?')

    snapshots, racks = build_snapshots_and_racks(events)
    played_words = build_played_words(events)

    endgame_spread_lost = win_prob_lost = phonies_played = missed_bingos = 0
    opp_win_prob_lost = 0
    missed_bingo_words = []
    opp_missed_bingo_words = []
    jesse_phonies = []   # [{'words_formed', 'challenged'}]
    opp_phonies = []     # [{'words_formed', 'challenged'}]
    for turn_idx, t in enumerate(analysis['turns']):
        # is_phony/missed_bingo are evaluated for BOTH players — mention them either way
        if t.get('is_phony'):
            mv = played_words[turn_idx] if turn_idx < len(played_words) else None
            entry = {'words_formed': mv['words_formed'] if mv else [],
                     'challenged': mv['challenged'] if mv else t.get('phony_challenged')}
            (jesse_phonies if t['player_index'] == jesse_idx else opp_phonies).append(entry)
        if t.get('missed_bingo'):
            om = t.get('optimal_move') or ''
            hist_rack = racks[turn_idx] if turn_idx < len(racks) else ''
            if validate_bingo(om, hist_rack):  # else: analysis false positive — rack mismatch
                word = resolve_bingo_word(om, snapshots[turn_idx]) if turn_idx < len(snapshots) else om
                if t['player_index'] == jesse_idx:
                    missed_bingos += 1
                    missed_bingo_words.append(word)
                else:
                    opp_missed_bingo_words.append(word)

        if t['player_index'] != jesse_idx:
            if opp_fully_annotated:
                opp_win_prob_lost += t.get('win_prob_loss') or 0
            continue
        if t.get('tiles_in_bag') == 0:
            endgame_spread_lost += t.get('spread_loss') or 0
        win_prob_lost += t.get('win_prob_loss') or 0
        if t.get('is_phony'):
            phonies_played += 1

    rd_match = re.search(r'(?:Rd\.?\s*|Round\s*)(\d+)', meta['chapter_title'], re.I)
    rnd = int(rd_match.group(1)) if rd_match else meta['chapter_number']

    return {
        'round': rnd,
        'title': meta['chapter_title'],
        'opponent': opp_name,
        'game_url': game_url,
        'lexicon': history.get('lexicon'),
        'jesse_score': jesse_score, 'opp_score': opp_score,
        'result': 'W' if jesse_score > opp_score else 'L',
        'mistake_index': mistake_index,
        'opp_mistake_index': opp_mistake_index,
        'opp_fully_annotated': opp_fully_annotated,
        'jesse_bingos': jesse_bingos, 'opp_bingos': opp_bingos,
        'jesse_blanks': jesse_blanks,
        'endgame_spread_lost': endgame_spread_lost,
        'win_prob_lost': win_prob_lost,   # multiply by 100 for %
        'opp_win_prob_lost': opp_win_prob_lost,   # only valid when opp_fully_annotated; multiply by 100 for %
        'phonies_played': phonies_played,
        'opp_phonies_played': len(opp_phonies),
        'missed_bingos': missed_bingos,
        'missed_bingo_words': missed_bingo_words,
        'opp_missed_bingo_words': opp_missed_bingo_words,
        'jesse_phonies': jesse_phonies,
        'opp_phonies': opp_phonies,
    }

stats = [compute_game(r) for r in results]
stats.sort(key=lambda g: g['round'])
```

### Step 5b: Identify the specific invalid word(s) in each phony play

`is_phony` only tells you the *play* was illegal — when it formed multiple words (main word + cross words), it doesn't say which one was the actual violation. Resolve that by checking every word the play formed against the game's actual lexicon via Woogles' public word-validity endpoint (see `/Users/Siwen/projects/scrabble-ai` conversation — confirmed live, no API key needed):

```python
def check_words(lexicon, words, retries=4):
    """Batch-check word validity in a lexicon via woogles.io's public word_service.

    One call per lexicon (see check_phony_words below) — never per word or per game.
    That keeps this well clear of rate limits regardless of collection size, but the
    retry/backoff still guards against a transient 429/5xx from the shared endpoint."""
    if not lexicon or not words:
        return {}
    for attempt in range(retries):
        try:
            r = requests.post(f'{BASE}/word_service.WordService/DefineWords',
                               json={'lexicon': lexicon, 'words': sorted(words),
                                     'definitions': False, 'anagrams': False})
            if r.status_code == 429 or r.status_code >= 500:
                if attempt == retries - 1:
                    return {}
                time.sleep((2 ** attempt) + random.uniform(0, 0.5))
                continue
            r.raise_for_status()
            return {w: res['v'] for w, res in r.json()['results'].items()}
        except requests.RequestException:
            if attempt == retries - 1:
                return {}  # no egress / persistent hiccup — caller falls back to starring the whole play
            time.sleep((2 ** attempt) + random.uniform(0, 0.5))
    return {}

def check_phony_words(stats):
    """Populate each phony entry's `invalid_words` — the subset of words_formed that
    are actually not in the lexicon. Batches one API call per distinct lexicon seen."""
    by_lexicon = {}
    for g in stats:
        for p in g.get('jesse_phonies', []) + g.get('opp_phonies', []):
            by_lexicon.setdefault(g.get('lexicon'), set()).update(
                w.upper() for w in p['words_formed'])

    validity = {lex: check_words(lex, words) for lex, words in by_lexicon.items()}

    for g in stats:
        lex_validity = validity.get(g.get('lexicon'), {})
        for p in g.get('jesse_phonies', []) + g.get('opp_phonies', []):
            # empty dict (API unreachable) -> invalid_words stays None -> caller falls back
            p['invalid_words'] = (
                [w for w in p['words_formed'] if not lex_validity.get(w.upper(), True)]
                if lex_validity else None
            )

check_phony_words(stats)
```

Run this once, right after `stats` is built — `game_note` (Step 7) reads `invalid_words` off each phony entry.

If `summary_for_index` returns None for either side, print `analysis['turns']` names alongside `player_summaries` names — they should match exactly — before rerunning.

### Step 6: Aggregate

```python
def sp_str(v):
    return f'+{v}' if v >= 0 else f'−{abs(v)}'

n     = len(stats)
wins  = sum(1 for g in stats if g['result'] == 'W')
mi_games = [g for g in stats if g['mistake_index'] is not None]
opp_ann_games = [g for g in stats if g['opp_fully_annotated']]  # opponent racks known all game — see Notes

total_jb = sum(g['jesse_bingos']  for g in stats)
total_mb = sum(g['missed_bingos'] for g in stats)
total_ob = sum(g['opp_bingos']    for g in stats)
total_bl = sum(g['jesse_blanks']  for g in stats)
total_eg = sum(g['endgame_spread_lost'] for g in stats)
total_ph = sum(g['phonies_played'] for g in stats)
total_opp_ph = sum(g['opp_phonies_played'] for g in stats)
total_sp = sum(g['jesse_score'] - g['opp_score'] for g in stats)

agg = {
    'record':          f"{wins}-{n-wins} {sp_str(total_sp)}",
    'avg_jesse':       round(sum(g['jesse_score'] for g in stats) / n, 1),
    'avg_opp':         round(sum(g['opp_score']   for g in stats) / n, 1),
    'avg_mi':          round(sum(g['mistake_index'] for g in mi_games) / len(mi_games), 2) if mi_games else None,
    'total_jb':        total_jb,
    'bingo_find_rate': f"{total_jb}/{total_jb+total_mb} ({round(total_jb/(total_jb+total_mb)*100,1)}%)" if (total_jb+total_mb) else 'N/A',
    'total_ob':        total_ob,
    'total_bl':        total_bl,
    'avg_eg':          round(total_eg / n, 1),
    'avg_wpl':         round(sum(g['win_prob_lost'] for g in stats) / n * 100, 1),
    'total_phonies':     total_ph,
    'total_opp_phonies': total_opp_ph,
    'games_per_mb':    round(n / total_mb, 1) if total_mb else None,
    'games_per_phony': round(n / total_ph, 1) if total_ph else None,
    'n_opp_annotated': len(opp_ann_games),
    'avg_opp_mi':      round(sum(g['opp_mistake_index'] for g in opp_ann_games) / len(opp_ann_games), 2) if opp_ann_games else None,
    'avg_opp_wpl':     round(sum(g['opp_win_prob_lost'] for g in opp_ann_games) / len(opp_ann_games) * 100, 1) if opp_ann_games else None,
}
```

`Opponent Phonies Played` sits right after `Total Phonies Played` in the Aggregate Stats table (own row, no "games per" derivative needed).

### Step 7: Per-game notes

Keep notes brief. Every phony or missed bingo — by Jesse **or** the opponent — gets named by word; nothing else gets a percentage or win-prob callout. Missed bingos use cumulative numbering (`missed bingo #N (WORD)`) cross-referenced with the Missed Bingos table, which lists rows in the same order. Track the counter across the whole report as you iterate games in round order:

```python
missed_bingo_counter = 0

def game_note(g):
    global missed_bingo_counter
    mi  = g['mistake_index'] if g['mistake_index'] is not None else 0.0
    wpl = round(g['win_prob_lost'] * 100, 1)
    eg  = g['endgame_spread_lost']
    mb  = g['missed_bingos']
    jb  = g['jesse_bingos']
    ob  = g['opp_bingos']
    sp  = g['jesse_score'] - g['opp_score']
    res = g['result']
    ws  = g.get('missed_bingo_words', [])
    opp_ws = g.get('opp_missed_bingo_words', [])
    opp_name = g['opponent']
    parts = []
    # Primary — exactly one (elif chain, most notable wins). Skipped entirely if nothing
    # matches AND secondary facts exist below — don't force a generic label onto an
    # already-informative note.
    if g['jesse_score'] >= 570:          parts.append(f'{g["jesse_score"]}-pt monster')
    elif mi <= 0.8:                       parts.append('very clean')
    elif mi > 3.5:                        parts.append('errorful win' if res == 'W' else 'errorful')
    elif abs(sp) <= 15:                  parts.append('narrow loss' if res=='L' else 'narrow win')
    elif res=='L' and abs(sp) >= 175:    parts.append('blowout')
    elif res=='W' and sp >= 150:         parts.append(f'+{sp} dominant')
    # Jesse's own phonies — always named, `*` marks the phony, all words_formed shown
    # since the specific invalid word isn't identifiable when the play formed several
    for p in g.get('jesse_phonies', []):
        tag = ' (unchallenged)' if not p['challenged'] else ''
        parts.append(f"phony {'/'.join(p['words_formed'])}*{tag}")
    # Missed bingos — cumulative numbering + word, one clause per game
    if mb == 1:
        missed_bingo_counter += 1
        parts.append(f'missed bingo #{missed_bingo_counter} ({ws[0]})')
    elif mb > 1:
        start = missed_bingo_counter + 1
        missed_bingo_counter += mb
        parts.append(f"missed bingos #{start}–#{missed_bingo_counter} ({', '.join(ws)})")
    # Opponent's phonies and missed bingos — named, attributed by name
    for p in g.get('opp_phonies', []):
        tag = ' (unchallenged)' if not p['challenged'] else ''
        parts.append(f"{opp_name} phony {'/'.join(p['words_formed'])}*{tag}")
    miss_verb = 'passed up' if 'nigel' in opp_name.lower() else 'missed'
    for w in opp_ws:
        parts.append(f'{opp_name} {miss_verb} {w}')
    # Everything else stays brief and low-priority
    if len(parts) < 4 and (ob>=4 or (ob>=3 and res=='L')): parts.append(f'opp {ob} bingos')
    if len(parts) < 4 and jb >= 4 and res=='W':  parts.append(f'{jb} bingos')
    if len(parts) < 4 and eg >= 50:      parts.append(f'{eg}-pt endgame')
    elif len(parts) < 4 and eg >= 30 and g['jesse_score'] < 570: parts.append('big endgame')
    if len(parts) < 4 and wpl >= 40:     parts.append('equity leak')
    if not parts:                         parts.append('solid win' if res=='W' else 'competitive')
    return '; '.join(parts)
```

Reset `missed_bingo_counter = 0` once per report (not per collection run) — it must match the Missed Bingos table row order exactly, so generate notes in round order in the same pass that builds that table.

### Step 8: Write the report

Save to `<project root>/reports/<tournament-slug>-report.md`. Format:

```markdown
# <Collection Title>
**Collection:** <title> | **Games:** <n> (<note if rounds missing>) | **Record:** W-L ±spread

🟩🟩🟥🟩🟩 🟥🟩🟩🟥🟩 ...

## Aggregate Stats (Jesse Day)

| Stat | Value |
|---|---|
| Average Mistakes Score | X.XX (over N games; Rds X–Y unavailable) |
| Average Score | XXX.X |
| Average Opponent Score | XXX.X |
| Total Bingos | N (X.XX/game) |
| Bingo Find Rate | N/N (XX.X%) |
| Opponent Total Bingos | N (X.XX/game) |
| Total Blanks Drawn | N (X.XX/game) |
| Avg Endgame Spread Lost | XX.X |
| Avg Win% Lost | XX.X% |
| Total Phonies Played | N |
| Opponent Phonies Played | N |
| Games per Missed Bingo | N.N |
| Games per Phony Played | N.N |
| Average Opponent Mistakes Score | X.XX (over N fully-annotated games) |
| Average Opponent Win% Lost | XX.X% (over N fully-annotated games) |

Omit the last two rows entirely if `n_opp_annotated == 0` (no game in the collection had the opponent's rack fully known). If `n_opp_annotated == n`, drop the "(over N fully-annotated games)" qualifier from both.

## Per-Game Breakdown

*Notes: `*` marks a phony (all words formed by the play; the specific invalid word isn't always identifiable when multiple words were formed — CSW is the configured lexicon for every game). Opp Mistakes/Opp Win% Lost show "—" for games where the opponent's rack wasn't fully known (not livestreamed or double-annotated).*

| Rnd | Game | Opponent | Result | Jesse | Opp | Spread | Mistakes | Jesse Bingos | Missed Bingos | Opp Bingos | Jesse Blanks | Endgame Spread Lost | Win% Lost | Opp Mistakes | Opp Win% Lost | Notes |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| N | [↗](https://woogles.io/anno/GAME_ID) | Name | W/L | NNN | NNN | ±NNN | X.X | N | N | N | N | N | XX.X% | X.X | XX.X% | note |
| **Avg** | | | | **NNN.N** | **NNN.N** | **±NN.N** | **X.XX** | **X.XX** | **X.XX** | **X.XX** | **X.XX** | **XX.X** | **XX.X%** | **X.XX**\* | **XX.X%**\* | |

\*Opp Mistakes/Opp Win% Lost averages are computed only over the fully-annotated games (denominator = `n_opp_annotated`, not the full game count) — mark the average cells with a footnote stating that count, e.g. `**2.15** (4 games)`. Omit both columns entirely if `n_opp_annotated == 0`.

## Missed Bingos

*Lowercase = blank tile; (X) = board tile X was already there*

| Rd | Opponent | Word |
|---|---|---|
| N | Name | WORD |

## Summary
<2–4 sentence narrative.>
```

**Missed bingo word format:** `resolve_bingo_word(optimal_move, board_snapshot)`. Lowercase = blank, `(X)` = board tile at that position. Always validate the missed bingo against the history rack before displaying — the analysis occasionally has rack mismatches that generate false-positive `missed_bingo` flags.

Omit the mistakes qualifier from the table if all games have data. Show "—" in Mistakes for games where `mistake_index` is None. Use "±" sign for spread (+ or −). Omit "Games per Phony Played" if `total_phonies == 0`.

No spreadsheet output (Jesse's preference as of June 2026).

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
