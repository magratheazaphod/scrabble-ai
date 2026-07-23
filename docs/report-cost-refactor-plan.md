# Implementation plan: deterministic tournament-report pipeline

**Written:** 2026-07-22 · **Status:** approved by Jesse, ready to implement
**Prereq reading:** `docs/report-cost-refactor-handoff.md` (the analysis), then
`scripts/generate_report_email.py` and `.claude/skills/tournament-analysis/SKILL.md`.

## Goal

Stop paying an Opus/high-effort/code-execution call to run deterministic Python. After this
refactor, every report section except `## Summary` is rendered by committed code
(milliseconds, $0), and the only LLM call left is one small no-tools call per *changed*
collection that writes the 2–4 sentence Summary paragraph. Decided by Jesse 2026-07-22:
**no richer per-game LLM commentary for now** — that's a possible later layer, not this work.

Target steady-state cost: $0 on days with no new games; ~one Sonnet-class call (≈2K tokens in,
≈300 out) on days a collection changes.

## Decisions already made — do not relitigate

1. Per-game *compute* caching: **not built**. Recomputing every game in code is free and is
   what keeps the cross-game `missed_bingo_counter` ordinals correct (see handoff).
2. The Summary paragraph stays LLM-authored, cached per collection, keyed on a content hash
   of the stats digest (NOT on `game_count` — an in-place game edit must refresh it).
3. The Summary call reads a compact digest only — **never raw game/turn data**. This is what
   permanently kills the collection-size cost axis.
4. Commit `3e1fa80` on branch `prompt-cache-skill-md` (prompt-caching SKILL.md) is
   **superseded — do not merge it**. Start a fresh branch from `master`.
5. SKILL.md remains the skill's documentation, but the Steps 5–8 *code* moves to a real
   module that both the automation and the interactive skill call (single source of truth
   by reference inversion, per the handoff's constraint).

## Guardrails (violating any of these is a stop-and-ask)

- **Never spend BestBot analysis quota during testing.** Before ANY run of
  `fetch_woogles_snapshot.py`, pin the rate-limit marker forward
  (SKILL.md "Testing changes to fetch_woogles_snapshot.py" section has the exact command),
  and restore `data/rate-limited-until.txt` + back up / restore `data/woogles-snapshot.json`
  afterward. All the fetches this plan needs are read-only APIs, but the marker pin is the
  belt-and-suspenders that Phase 2 of the script issues no `RequestAnalysis`.
- **Do not send email during testing.** Add a `DRY_RUN=1` env check to
  `generate_report_email.py` that prints the assembled body to stdout instead of calling
  `send_email()` (and skips `mark_sent`/`save_state` writes).
- Don't touch `.github/workflows/*.yml` except where this plan says to (it mostly shouldn't
  need changes — the workflow just runs the same two scripts).
- Golden-diff numeric mismatches (Phase 2) are STOP conditions, not things to hand-wave.

---

## Phase 1 — extract the module

Create `scripts/tournament_report.py`. Lift the following **verbatim** from
`.claude/skills/tournament-analysis/SKILL.md` (line refs as of master today):

| Function | SKILL.md lines |
|---|---|
| `is_jesse` | 220–224 |
| `summary_for_index` | 226–236 |
| `format_real_name` | 238–245 |
| `get_opp_name` | 247–268 |
| `build_snapshots_and_racks` | 270–301 |
| `validate_bingo` | 303–322 |
| `resolve_bingo_word` | 324–358 |
| `build_played_words` | 360–400 |
| `opp_racks_complete` | 402–412 |
| `compute_game` | 414–518 |
| `check_words`, `check_phony_words` (Step 5b) | 529–575 |
| `sp_str` + aggregate block (Step 6) → wrap as `aggregate(stats)` | 584–620 |
| `game_note` (Step 7) | 630–683 |

Then add what SKILL.md describes in prose but never wrote as code:

- `game_notes(stats) -> list[str]` — wraps `game_note`, owns the `missed_bingo_counter`
  (make it a local/closure, not a module global; reset per call), iterates `stats` already
  sorted by round. Counter semantics must exactly match SKILL.md:627–685 (numbering matches
  the Missed Bingos table row order).
- `render_report(stats, agg, notes, title, summary_md=None, subject_display="Jesse Day") -> str`
  — the Step 8 template (SKILL.md:687–746) plus the win/loss progression line
  (SKILL.md:763–768). Every conditional in the template is a requirement:
  - progression line: 🟩/🟥 in blocks of 5, right after the header line, no section header;
  - omit the two `avg_opp_*` Aggregate rows entirely when `n_opp_annotated == 0`; drop the
    "(over N fully-annotated games)" qualifier when `n_opp_annotated == n` (SKILL.md:717);
  - omit the Opp Mistakes / Opp Win% Lost *columns* when `n_opp_annotated == 0`; footnote
    the two averages with the game count otherwise (SKILL.md:728);
  - "—" for null `mistake_index` / non-fully-annotated opponent cells; "±" spread signs;
    omit "Games per Phony Played" when `total_phonies == 0` (SKILL.md:744);
  - bold **Avg** row; Missed Bingos table in round order; the italic legend lines;
  - `## Summary` section appended only when `summary_md` is provided.
- `build_digest(stats, agg, notes, title) -> str` — the compact text the Summary LLM call
  reads AND the thing whose SHA-256 becomes the cache key. Contents: title, record line,
  progression string, the full `agg` dict (stable key order), and one line per game:
  round, opponent, result, score, spread, mistake index, note text. Nothing else. Keep it
  deterministic (sorted/stable ordering everywhere) — hash stability is the cache.
- **Subject parametrization** (replaces the one-off `subject_clause` prompt hack at
  `generate_report_email.py:94–101`): give the module a `subject` argument —
  `None` → use `is_jesse` and "Jesse Day" headers (default, byte-identical behavior);
  `{"nickname": ..., "real_name": ...}` → match players by normalizing
  `GameHistory players[].nickname` (lowercase, strip every non-`a-z` char) against
  `nickname` — this absorbs per-game variants like a "(MYS)" suffix — and use `real_name`
  in every title/header where "Jesse Day"/"Jesse" appears. Never match on Woogles login
  username (it doesn't appear in game data). Keep internal dict keys (`jesse_score` etc.)
  unchanged — they're field names, not display strings; only rendered text changes.

Module rules: stdlib + `requests` only; no `anthropic` import; every function importable
and callable on a plain snapshot dict (the `{"meta","analysis","history"}` game shape).

## Phase 2 — golden-corpus regression (before rewiring anything)

The 11 cached reports in `.github/report-state.json` (`report_md` fields) are the test set.

1. Get full game data for all 11 collections: back up `data/woogles-snapshot.json`, pin the
   rate-limit marker forward, then run
   `TARGET_USERNAME=magrathean python3 scripts/fetch_woogles_snapshot.py`
   (~700 read-only HTTP calls, ~2 min — this is the sanctioned way, see SKILL.md's testing
   section). Save the result somewhere gitignored (e.g. `data/golden-snapshot.json` —
   add to `.gitignore`); restore the original snapshot + marker.
2. Write `scripts/test_report_golden.py`: for each collection in the golden snapshot that
   has an entry in `report-state.json`, run the module end-to-end
   (`compute_game` → `check_phony_words` → `aggregate` → `game_notes` → `render_report`)
   and diff against the cached `report_md` **truncated before `## Summary`**.
3. Classify every diff line:
   - **Numeric or word-content difference** (any stat, any note label, any missed-bingo
     word, table row order): a bug in the extraction. STOP, fix, re-run. Zero tolerance.
   - **LLM formatting variance** (the old reports were LLM-rendered): pure
     whitespace/punctuation drift, or a retitle the old LLM took upon itself — one known
     case: cached title `# Jesse Day's Causeway 2026` vs. collection title
     `Jesse Day's (abbreviated) Causeway 2026`. New code uses the collection title
     verbatim; log these as accepted diffs in the PR description.
4. Keep the script in the repo — it's the permanent regression harness for future
   SKILL/module changes (skippable when the golden snapshot file is absent).

Do not proceed to Phase 3 until the diff report is clean under those rules.

## Phase 3 — rewire `generate_report_email.py`

1. Delete `upload_snapshot`, `generate_collection_report`, `read_skill_md`, the
   `subject_clause` prompt, the Files-API/beta-header/code-execution plumbing, and the
   last-text-block + `NO_REPORT_READY` scraping.
2. Per collection: run the module. Compute `digest = build_digest(...)`,
   `digest_hash = sha256(digest)`.
3. Summary call — the only remaining LLM use:
   - `MODEL = "claude-sonnet-5"`, no tools, no thinking config, `max_tokens=500`.
   - Prompt: the digest + an instruction to write a 2–4 sentence narrative summary of the
     tournament for the report's `## Summary` section — factual, specific (name opponents,
     cite the standout numbers from the digest), no headers, no meta-commentary. Include
     one example Summary lifted from a cached report as a style anchor. For one-off
     subject reports, tell it who the subject is; address content to the recipient.
   - On API failure: render the report without a Summary section rather than failing the
     run (log it; next run retries because the hash won't have a stored summary).
4. Cache (`.github/report-state.json`) — new entry shape:
   `{"title", "game_count", "digest_hash", "summary_md", "reported_at"}`.
   Reuse `summary_md` when the stored `digest_hash` matches; regenerate otherwise.
   The deterministic body is re-rendered every run regardless (it's free).
   `report_md` is no longer stored.
   - **Zero-cost migration:** on first run, for entries that have `report_md` but no
     `digest_hash`, extract the existing `## Summary` text from `report_md`, store it as
     `summary_md` with the freshly computed `digest_hash`, drop `report_md`. No LLM calls
     for unchanged collections. (Corner case: if the freshly computed digest wouldn't have
     matched the old report's content, that's exactly an in-place edit the old
     `game_count` key missed — let it regenerate.)
5. One-off flow (`TARGET_USERNAME`/`TARGET_COLLECTION_UUID`): pass
   `snapshot["target"]` into the module's `subject` argument. If `collections` is
   non-empty and `target` is missing, keep the existing abort (line 188–190 today).
6. Keep unchanged: `already_sent_today`/`mark_sent`, pending-note assembly, email
   subject/HTML template, recipients, the workflow YAML contract (same script name, same
   env vars — `ANTHROPIC_API_KEY` still required, just much cheaper now).
7. Add the `DRY_RUN` guard (see Guardrails).

Verify with `DRY_RUN=1` against the golden snapshot: full email body assembles, cached
summaries are reused (run twice; second run must make zero LLM calls), body above each
Summary matches Phase 2 output.

## Phase 4 — SKILL.md surgery

Edit `.claude/skills/tournament-analysis/SKILL.md`:

- Steps 1–4, the API-structure reference, and the entire `## Notes` section stay.
- Replace the *code bodies* of Steps 5–8 with a short section: "Steps 5–8 are implemented
  in `scripts/tournament_report.py` — import and call it; do not re-type or re-derive the
  logic," plus a ~10-line usage snippet (load snapshot → `compute_game` per game →
  `check_phony_words` → `aggregate` → `game_notes` → `render_report`) and a pointer to
  `scripts/test_report_golden.py` as the regression gate for any change to the module.
- Keep, in prose, the semantic contracts a future editor needs: the `missed_bingo_counter`/
  Missed-Bingos-table ordering rule, the `opp_fully_annotated` three-part rule, the
  multi-word phony `*` rule, the Nigel "passed up" rule, missed-bingo rack validation.
  These describe the module's behavior; the module is now where they're enforced.
- The interactive path stays: an in-session report request = fetch data (Steps 1–4),
  call the module, write the file to `reports/`. The Summary paragraph in interactive use
  is written by the in-session model directly (no API sub-call needed).

## Phase 5 — SEPARATE PR: per-game fetch cache

Independent change, own branch/PR, only after Phases 1–4 land. In
`fetch_woogles_snapshot.py`: cache `GetAnalysisResult` + `GetGameHistory` bodies per
`game_id`, keyed on the already-computed history fingerprint
(`data/analyzed-fingerprints.json` machinery), so the hourly jobs stop re-reading ~700
immutable responses. Design considerations to work out in that PR: where the cache lives
(the `woogles-data` branch already carries the full snapshot each run, so total data size
is not new — but structure it as one file, not 230), and invalidation = fingerprint change
or analysis re-request. No LLM involvement; this is runtime/API-load, not spend.

## Acceptance checklist

- [ ] Golden diff clean for all 11 collections (numeric-identical; accepted formatting
      diffs listed in PR).
- [ ] Two consecutive `DRY_RUN=1` runs: first migrates summaries with 0 LLM calls,
      second makes 0 LLM calls and reuses everything.
- [ ] Forced regeneration (delete one entry's `digest_hash`) makes exactly one
      Sonnet call, no tools, and the digest passed is <3K tokens.
- [ ] One-off subject flow produces a correctly re-headed report (spot-check with
      `TARGET_COLLECTION_UUID` + `DRY_RUN=1`).
- [ ] `grep -c "def " scripts/tournament_report.py` roughly matches the table in Phase 1 —
      no logic left behind in prompts.
- [ ] SKILL.md contains no fenced Python implementing Steps 5–8 (only the usage snippet).
- [ ] `data/rate-limited-until.txt` and `data/woogles-snapshot.json` restored to
      pre-test state; no analysis quota spent (assert `RequestAnalysis` never fired).
- [ ] Squash-merge with explicit `--subject`/`--body` (Jesse's standing preference).
