# scrabble-ai

Jesse Day's personal Scrabble data pipeline: a game archive, an upload path from
paper scoresheets or `.gcg` files to woogles.io, automated BestBot-analysis
reporting, and a practice-game tracker. See `README.md` for the tour.

This file holds the facts that are true across *every* workflow here. Each
workflow's own procedure lives in a skill (index at the bottom); skills assume
this file and do not repeat it.

## Standing facts about Jesse's games

- **Woogles username `magrathean`.** In-game display names vary: "Jesse Day",
  "Jesse", "JD", "JesseD". **"JD" in a `#player` line is Jesse's own initials** -
  never read it as a stranger.
- **Lexicon: always CSW, never NWL.** No exceptions, including games where the
  recorded name isn't obviously his. Use the **CSW edition current at the time of
  play**, not today's: CSW15 / CSW19 / CSW21 / CSW24. For practice games, CSW21
  before 2025-01-01 and CSW24 after. Codes are short forms (`CSW21`, not
  `CSW2021`; the long form 500s with "lexicon file not found").
- **Never trust your own sense of whether a word is valid.** Scrabble lexica
  contain thousands of words that look wrong and exclude things that look right.
  Always check the list - `/lexicon-lookup`. This is a standing instruction, not
  a suggestion.
- **Challenge rule** is `FIVE_POINT` for OTB tournament uploads and `VOID` for
  Woogles league and most online play. Under VOID an invalid word cannot be
  played at all, so a phony-free record is the rule rather than an achievement -
  never praise it, and never compare a VOID mistakes score against an OTB one.
- **Opponent naming:** James Curley is always "James Curley", never "James"/
  "JC"/"Curley"; Jesse is always "JD" in chapter titles and GCG player lines.

## The Woogles API

Every workflow that touches woogles.io uses the Connect RPC API directly with
`requests` - no browser, ever. `urllib.request` has SSL problems on macOS Python
3.12; don't reach for it.

```python
API_KEY = os.environ['WOOGLES_API_KEY']   # from .env at project root (gitignored)
BASE    = 'https://woogles.io/api'        # POST {BASE}/<package>.<Service>/<RpcName>
HDRS    = {'Content-Type': 'application/json', 'X-Api-Key': API_KEY}
```

Full schema reference: <https://buf.build/domino14/liwords/docs>. The server
source is readable in the sibling `~/projects/liwords` clone when behaviour needs
explaining (`pkg/omgwords/service.go`, `pkg/cwgame/`, `pkg/analysis/`).

Retry 429/5xx with exponential backoff plus jitter, and keep fan-out modest
(≤10 concurrent requests); a burst that trips Woogles' own rate limiting turns
into a hard failure otherwise.

## Irreversibility - read before writing anything to Woogles

These are the traps that have actually cost time here. All confirmed against the
live service.

- **`ImportGCG` cannot be undone.** Lexicon and challenge rule cannot be edited
  after creation, and a *finished* game cannot be deleted (`DeleteAnnotatedGame`
  refuses) or hidden (`SetAnnotatedGamePrivacy` is a server-side no-op stub). Get
  it right first; if genuinely unsure, ask Jesse.
- **A successfully-analyzed game can never be re-analyzed.** `force: true` is
  honoured only for *failed* jobs or legacy v0 results; for a completed
  `analysis_version >= 2` result it returns `ALREADY_REQUESTED` and does nothing,
  **even after the game's moves have changed underneath it**. Check
  `GetAnalysisStatus` *before* editing an uploaded game. The only fix is a fresh
  upload under a new `game_id`, then swapping it into the collection and
  repointing anything keyed on the old id.
- **Racks are part of the deliverable.** A game whose racks are only the tiles
  played is not fully annotated: BestBot yields no per-player stats, so it never
  reaches a tracker row or a report. A short rack still analyzes "successfully",
  which freezes the meaningless result forever. Games #91/#92 are permanently
  un-analyzable for exactly this reason.
- **An unterminated GCG imports as a stuck unfinished game** that blocks *all*
  further `ImportGCG` calls on the account until deleted. Unfinished games are
  deletable; finished ones aren't.

## Scripts-first

**All deterministic logic lives in `scripts/`.** Judgment - reading handwriting,
reading a board, choosing a lexicon, deciding what to tell Jesse - is your job;
arithmetic, scoring, GCG assembly, API calls, and stats are the scripts' job. If
you catch yourself adding numbers, hand-writing GCG lines, or re-typing
`requests` calls that a script already makes, stop and use the script. Extend a
script rather than writing ad-hoc equivalents beside it. Hand-work is where every
historical error in this repo came from.

| Script | Does |
| --- | --- |
| `woogles_upload.py` | preflight → ImportGCG → verify finished → collection add → comment. The only sanctioned upload path. |
| `gcg_preflight.py` | scan/heal `.gcg` parser-breaking patterns; `--check` to report only |
| `tournament_report.py` | all stats/aggregation/report rendering (single source of truth, shared with the email job) |
| `test_report_golden.py` | regression gate - run after any `tournament_report.py` edit |
| `fetch_woogles_snapshot.py` | harvest collections/games into `data/woogles-snapshot.json` |
| `generate_report_email.py` | build and send the daily report email |
| `woogles_league.py` | turn a league season into a report-ready collection |
| `update_curley_tracker.py` | all Curley tracker sheet reads/writes |
| `sync_curley_collection.py` | reorder/retitle the Curley collection to match the sheet |
| `audit_woogles_consistency.py` | tracker ↔ live game ↔ repo file cross-check for OCR games |
| `otb_solver.py` | board/scoring/lexicon engine + OTB placement solver. Shared. |
| `verify_gcg.py` | independent GCG replay verifier - the hard gate on **every** upload, not just OTB |
| `scripts/otb/` | OTB-only steps: `prep_photos.py`, `check_transcription.py`, `author_gcg.py`, `regression.py` |

`otb_solver.py` and `verify_gcg.py` sit at the top level rather than under
`otb/` because `woogles_upload.py` and `audit_woogles_consistency.py` depend on
them for every upload and every nightly audit. `scripts/otb/regression.py` is the
regression gate for all six - run it after changing any of them.

## Automation already exists - don't build more

Two GitHub Actions crons do the recurring work unattended. **Never propose a new
scheduled task, `/loop`, or cloud routine to "resume" or "finish" analysis, fill
in tracker stats, or refresh a report** - it is already covered, and offering it
has annoyed Jesse before.

- **`.github/workflows/woogles-report.yml`** (every 30 min in two daily windows):
  syncs league seasons, requests BestBot analysis for pending games across *every*
  collection on the profile within the rolling 24h quota, generates and emails the
  report once a collection is fully analyzed, runs `--enrich-collection` for the
  Curley tracker, runs `sync_curley_collection.py`, and re-audits every
  OCR-reconstructed game.
- **`.github/workflows/woogles-snapshot.yml`** (hourly window): publishes
  `data/woogles-snapshot.json` to the `woogles-data` branch for environments that
  can't reach woogles.io.

State lives in `.github/report-state.json` (on master) and marker files on the
`woogles-data` branch. A newly created collection is picked up on the next run
with no per-tournament setup. If a report looks stalled, check this workflow's
runs and state - don't build a replacement.

**BestBot analysis quota is a rolling 24h window** (not calendar-day), ~15/day,
and the fetch script backs off via a persisted `data/rate-limited-until.txt`
marker. Don't burn it on testing: pin the marker forward and scope test runs with
`TARGET_COLLECTION_UUID=` or `TARGET_USERNAME=` rather than sweeping the whole
archive (~700 reads, ~2 minutes).

## Repo layout and git

```
Tournament Games/<year>/<event>/     .gcg archive, 2010-2025 (+ non-game notes files)
Practice Games/James Curley/         <N>_<mon><day>_<YY>.gcg, N = tracker Game #
Practice Games/YYYY-MM-DD <Opp>.gcg  other opponents
board-pictures/                      scoresheet/board photos awaiting reconstruction (gitignored)
reports/                             generated markdown reports
scripts/                             all deterministic logic
data/                                pipeline state and logs (gitignored, local-only)
.github/                             workflows + committed pipeline state
```

- **GitHub is the canonical source, not the local clone.** Confirm `git status`
  is clean and `git pull origin master` before reading anything under
  `Tournament Games/` - never work off a stale mirror.
- A tournament folder usually also holds non-game files (`equity`, `words`,
  `toughies`, `stats`). A real GCG starts with a `#player1`/`#character-encoding`
  header followed by `>Name:` move lines; those files don't. Never upload one.
- **Every uploaded game lands on GitHub.** Commit the `.gcg` and push to master.
- **Never `git add -A` / `git add .`** - `data/`, `.env`, `.secrets/`, and photos
  are gitignored for good reason and a broad add is how they escape.
- **Don't delete untracked files** without checking `git status` from the start of
  the session; they may be Jesse's own prior work.
- Squash merges need an explicit `--subject`/`--body` on `gh pr merge --squash`;
  GitHub's default concatenates every commit body.

## This repo is public

- `.env`, `.secrets/`, and `data/` are gitignored. Keep it that way.
- **The real-name ↔ Woogles-handle registry stays private** and uncommitted
  (`data/woogles-usernames.json` or the `WOOGLES_NAME_REGISTRY` secret). Players
  may keep those identities separate deliberately; that mapping is not ours to
  publish. Unset means the feature is off, which is the right default for anyone
  but Jesse.
- Only link a Woogles profile when the player's `user_id` is a real account key.
  Annotator uploads synthesise `internal-<nickname>` or a bare nickname, and a
  link built from those is usually dead or points at a stranger.
- Local logs (`data/otb-upload-log.jsonl`, `data/woogles-upload-log.jsonl`) are
  local-only by design - never force-add them to a branch.

## Skills

| Skill | Use for |
| --- | --- |
| `/tournament-analysis` | turning a Woogles collection into a stats report; league seasons |
| `/gcg-upload` | uploading `.gcg` files to Woogles and grouping them into a collection |
| `/otb-scrabble-upload` | reconstructing a game from scoresheet + board photos, then uploading it |
| `/curley-tracker` | the James Curley practice-game Google Sheet and its collection |
| `/lexicon-lookup` | validating words / definitions against CSW, NWL, TWL, foreign sets |
| `/woogles-queries` | SQL against the woogles.io reporting DB (lives in `~/projects/liwords/reporting/`) |
