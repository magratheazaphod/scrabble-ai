# Handoff: cheapening the Woogles tournament-report pipeline

**Written:** 2026-07-20 · **Status:** RESOLVED 2026-07-22 — see Outcome below · **Branch:**
was `prompt-cache-skill-md`, since deleted; this doc kept on `master` for the reasoning

> **Outcome.** The recommendation in "The key finding that reframes everything" is what
> happened. Commit `3c90a6a`, *"Make the Woogles tournament-report pipeline deterministic,
> drop the LLM cost"*, moved Steps 5–8 into `scripts/tournament_report.py` as committed code.
> The Opus + code-execution + SKILL.md call is gone; the only LLM call left is
> `generate_summary()`, a small no-tools call over a compact digest.
>
> The prompt-caching commit this doc describes was therefore never merged and has been
> deleted: it cached an ~11K-token SKILL.md prefix that no longer gets sent at all. The rule
> it taught survives in `CLAUDE.md` — **never feed SKILL.md to a recurring API call.**
>
> Kept because the diagnosis is still the clearest account of why the pipeline is shaped the
> way it is, and because the trap it names (paying a reasoning model to run deterministic
> code) is easy to walk back into. Everything below is as written on 2026-07-20.

This is a design handoff, not an implementation plan. It captures what we found investigating
why the report pipeline costs $2–$10/day so a fresh model can produce a concrete plan without
re-deriving the analysis. Read the two source files it references before proposing changes:
`scripts/generate_report_email.py` and `.claude/skills/tournament-analysis/SKILL.md`.

---

## The system in one paragraph

A GitHub Actions cron (`.github/workflows/woogles-report.yml`, fires every 30 min in two daily
windows) fetches a fresh Woogles snapshot, then runs `scripts/generate_report_email.py`, which
emails Jesse a daily tournament report. For each collection, it calls **Opus 4.8 with
`effort: high`, adaptive extended thinking, and the `code_execution` tool**, handing the model
`SKILL.md` (the full spec) + a per-collection data snapshot and asking it to run SKILL.md's
Steps 5–8 in Python and return the markdown report. Reports are cached in
`.github/report-state.json`, keyed **only on `game_count`** — a collection regenerates only when
its game count changes (i.e. a new game got analyzed). That `game_count`-only key is
**intentional** (Jesse confirmed): editing SKILL.md deliberately does NOT force existing
collections to regenerate; only new games do.

## Why it costs what it costs

Every cache *miss* (a collection whose game count changed) is one of the most expensive call
shapes available: high-effort + extended-thinking + multi-turn code-execution Opus. Two cost
axes, established during the investigation:

- **Collection *count* axis** — how many distinct collections regenerate in one run. Backfill
  days regenerate several at once → the $10 spikes (e.g. Jul 01).
- **Collection *size* axis** — a 30-game collection (WESPAC 2019, NSC 2019) costs far more per
  regeneration than a 2-game one: more game data flows through the code-exec loop as tool
  results, more thinking, and ~4× the output tokens (a 30-game report is ~7.4K chars vs ~1.8K
  for 2 games). One new game in a 30-game collection re-bills all 30 games' work.

Things that are NOT the cause (ruled out explicitly):
- SKILL.md changes are not busting anything. There was no prompt cache at all until the commit
  on this branch, and the app-level `report-state.json` cache isn't keyed on SKILL.md, so
  editing it neither triggers regeneration nor invalidates a cache.

## What's already been done (this branch)

Commit `3e1fa80` on `prompt-cache-skill-md`: split the Claude prompt so the ~11K-token SKILL.md
block is its own content block marked `cache_control: {"type": "ephemeral"}`, with the
per-collection varying bits (subject clause + snapshot upload) after the breakpoint. Within a
single run's sequential loop (inside the 5-min TTL), collections 2..N reuse that prefix at ~10%
input price. Break-even = 2 collections; wins on multi-collection/backfill days.

**Limits of that fix (why it's not enough):** it only attacks the *count* axis, and only
*within one run*. Runs are 30+ min apart with a 5-min TTL, so there is zero cross-run reuse. It
does nothing for the *size* axis — the big-collection regenerations that dominate cost.

Never pushed; the branch was deleted once the refactor above superseded it.

## The key finding that reframes everything

**SKILL.md's Steps 5–8 are already complete, deterministic Python — including the per-game
notes.** `game_note(g)` (SKILL.md:610–660) is a *pure function* of a game's computed stats:
every label (`'very clean'`, `'errorful win'`, `phony WORD*`, `missed bingo #N (WORD)`) comes
from numeric thresholds and stat fields. There is no LLM creativity per game today.

Implication: the Opus call is essentially **paying a high-effort reasoning model to transcribe
SKILL.md's own committed Python into the code-exec sandbox and press run.** The arithmetic is
already executed in code; the model is a very expensive Python interpreter. This is a deliberate
design choice (per the script's header comment: keep SKILL.md the single source of truth, don't
reimplement it) — the cost is paying Opus every regeneration to run deterministic code.

Consequences for the ideas we discussed:
- **Prompt-caching per game: no.** Prefix cache + 5-min TTL — wrong tool for cross-regeneration
  reuse.
- **App-level caching of per-game *LLM notes*: moot today** — the notes aren't LLM output,
  they're deterministic code. Nothing to cache.
- **The real win is bigger than caching:** run Steps 5–8 as committed code (no LLM) →
  regeneration becomes a local Python run, ~$0, deterministic, milliseconds. Per-game "reuse"
  falls out for free because recomputing *all* games in code costs nothing — you don't cache
  what's free to recompute.

## The one cross-game dependency (matters for any caching design)

Per-game notes are NOT purely local: `missed_bingo_counter` (SKILL.md:663) gives each note a
cumulative "missed bingo #N of the tournament" ordinal, in round order, matched to the Missed
Bingos table. Adding an early-round game shifts every later `#N`. So **naive per-game caching
would serve stale ordinals.** This is deterministic, so a full code recompute just gets it right
every time — but any caching design must handle it.

## Jesse's proposed architecture (the fork to decide tomorrow)

Jesse suggested a **two-layer split with a post-hoc tournament pass** — which is the correct
pattern and the thing that makes per-game caching *safe*:

- **Per-game layer** — local, immutable facts of one finished game (note color, phony words,
  count + words of missed bingos). Keyed on `game_uuid`. Generate/compute once, reuse forever.
- **Post-hoc tournament layer** — all cross-cutting facts: cumulative `#N` numbering, Missed
  Bingos table order, aggregates, win/loss progression, records. Runs over the whole set each
  regeneration, cheaply.

**The discipline that makes it airtight:** a per-game artifact must NEVER bake a tournament-global
count/ordinal into itself. All ordinals ("nth missed bingo of the event", "3rd-best game",
records) live ONLY in the post-hoc layer, injected at assembly. Then a finished game's cached
artifact is always valid because its local facts never change.

### The decision fork

1. **Keep notes deterministic (status quo logic), just stop paying an LLM to run them.**
   Extract Steps 5–8 into committed code; the post-hoc/assembly layer owns numbering + aggregates.
   No LLM in the regeneration path, no cache needed → cost ≈ $0. Attacks both axes. This is the
   minimal change that zeroes cost.

2. **Open the door to richer LLM-authored per-game commentary.** Then build both layers, cache
   per-game LLM prose (immutable game → generate once), and add ONE cheap whole-tournament LLM
   pass for cross-tournament narrative. Crucial property: that pass reads a *compact stats
   summary*, not all raw game data, so it's one call regardless of game count — it does NOT
   reintroduce the size axis.

Jesse's "nth missed bingo of the tournament" note works in either — it just moves from inside
`game_note` into the post-hoc layer.

**Open question Jesse is sleeping on:** keep notes deterministic (option 1, free) or move to
LLM-authored per-game commentary (option 2, cached + tournament pass)? That answer decides
whether we build a cache at all or just the deterministic split.

## Constraint to respect in any plan

**Single source of truth.** SKILL.md is deliberately canonical; the automation must not fork a
second copy of the stats/report logic that drifts. Cleanest resolution floated: move Steps 5–8
into a real importable Python module that BOTH the interactive skill and the automation call, so
SKILL.md references it instead of embedding a copy. Any plan should say exactly where the module
boundary sits and how SKILL.md points at it.

## Suggested first task for tomorrow's model

Before proposing a refactor, **trace the whole generation path end-to-end and confirm whether any
step requires genuine model judgment** (free-text the model is expected to compose, anything not
derivable from stats). Steps 5–8 look fully deterministic on inspection, but confirm it. If even
one genuine-judgment spot exists, the target shape becomes "deterministic code for everything +
one tiny scoped LLM call," which is still a massive cut. Then produce: module boundary, how
SKILL.md references it, the per-game vs post-hoc layer split, and — if option 2 — the cache key
(`game_uuid` + content hash) and the compact-summary schema for the tournament pass.

## Key file/line references

- `scripts/generate_report_email.py:116` — the expensive `client.messages.create` call
  (`effort: high`, adaptive thinking, `code_execution`).
- `scripts/generate_report_email.py:205` — the `game_count`-only cache-reuse check.
- `.claude/skills/tournament-analysis/SKILL.md:193` — Step 5 (per-game stats).
- `.claude/skills/tournament-analysis/SKILL.md:560` — Step 6 (aggregate).
- `.claude/skills/tournament-analysis/SKILL.md:603` — Step 7 (per-game notes; `game_note` at 610).
- `.claude/skills/tournament-analysis/SKILL.md:663` — the cross-game `missed_bingo_counter`.
- `.claude/skills/tournament-analysis/SKILL.md:665` — Step 8 (report template).
