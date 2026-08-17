# Handoff: bring-your-own-engine analysis

**Written:** 2026-08-11 · **Status:** premise corrected, evaluation revised, decision
deferred pending Jesse's analysis-style spec and engine briefing · **Branch:** `master`

This is a design handoff, not an implementation plan. It captures a hosting evaluation
for running a Scrabble analysis engine (MAGPIE / Macondo) outside woogles.io, **and the
correction that reframed it**, so a fresh model can plan without re-deriving the research
or repeating the original mistake. Read `CLAUDE.md` and
`docs/report-cost-refactor-handoff.md` before proposing changes.

---

## The correction, first

The original evaluation was framed around **throughput**: escape the BestBot rolling-24h
analysis quota (~15/day) and cheaply backfill the whole archive. **That premise was
wrong.** Jesse's drivers are correctness and teaching. Quota and latency are explicitly
*not* motivations. Do not re-derive the throughput framing; it has already been rejected
once.

A second, narrower correction: an intermediate draft claimed inference-aware simulation is
an order of magnitude more expensive than plain Monte Carlo. **It is not.** Jesse reports
inferred sims are not much more compute-intensive, with the exception of one specific
variety that is costly and will be characterised later. No cost estimate in this document
should assume otherwise.

## Why this project exists

Two co-equal goals.

**1. Correctness, in service of the world-championship goal.** Jesse's aim is to be world
Scrabble champion, so per-position analytical correctness carries real incentive. Woogles'
simmed analysis is good but **models no opponent-rack inference at all**. Jesse's recent
work with MAGPIE indicates that good inference materially shifts simulation outcomes and
changes which play is actually correct at various points in a game. Woogles' analysis is
also a *fixed artifact* - it cannot be re-run under different settings - so a
self-hosted engine is the only route to inference-aware numbers.

**2. Teaching infrastructure for Scrabble, via bring-your-own-engine (BYOE).** Jesse
eventually wants to turn this into teaching infrastructure. Woogles-native analyses are
*good enough* for that purpose; BYOE is wanted for experimentation and flexibility, not
because Woogles is inadequate. The ambition is a pluggable engine, not a hardcoded one.
This is kin to the leagues work's native-Woogles-feature ambition.

## The fork this creates - it governs everything

The two goals have opposite hosting profiles:

- **Research tool** (Jesse iterating on inference): a bursty batch job. What matters is
  the wall-clock of the tweak-and-re-run loop, not total cost.
- **Teaching product** (serving other people): an always-on, multi-user,
  latency-sensitive **service**.

**Today's decision is the research tool only.** The point of BYOE is that a clean engine
interface lets the service question be deferred without foreclosing it. So do not choose
hosting now in a way that assumes a single machine or a single engine.

## Compute: deliberately un-estimated

There is no cost table in this document, on purpose.

The original evaluation assumed **~2s of 4-core CPU per position**, giving ~35 core-hours
to sweep the archive. **Treat that figure as historical only.** It was measured against
nothing - it was an estimate for plain Monte Carlo, and the workload is no longer an
archive sweep. It is recorded here solely so nobody mistakes a later, real measurement for
a regression against it.

What is known: inference does not blow up per-position cost, except for one variety that
does. Which variety Jesse is using, and what it costs, is a pending input. **Produce no
cost model until a real measurement exists** (see *Suggested first task*).

## Methodology requirements

These are correctness constraints and hold regardless of cost or hosting.

- **Compare inference-on vs inference-off within the same engine.** Never MAGPIE against
  Woogles. A cross-engine comparison confounds the inference effect with differing leave
  values, evaluation functions and lexicon editions. This is the easiest available way to
  get a wrong answer that looks convincing.
- **Pin everything that could move:** fixed seeds, pinned lexicon and leave files, a
  recorded engine commit. Without this, a change in the best play cannot be attributed to
  the inference model rather than to Monte Carlo noise.
- **Run enough iterations that the equity gap between candidate plays exceeds sim noise.**
  If the claim is "inference changes the correct play", the difference has to survive
  variance. Whether inference weighting *worsens* effective sample size depends on the
  implementation - importance weighting would, a narrowed sampling pool would not. **To
  confirm during the engine briefing; do not assume either way.**
- Honour the standing lexicon rule in `CLAUDE.md`: always CSW, using the edition current
  at the time of play.

## Available corpus

Measured in-repo, and offered as what *could* be analysed rather than as a target:

- 2,421 `.gcg` under `Tournament Games/`, 78 under `Practice Games/`
- ~25 move-lines per game (sampled over 40 files)
- ~62,000 positions if the whole archive were ever swept

The likely real workload is a curated position set run repeatedly across engine
configurations, not a one-shot sweep.

## Hosting options, weighted for the research loop

- **Local M1 Pro - primary.** 10 cores (8 performance), 16 GB, corpus already resident,
  no upload latency. Best per-core speed of anything considered, likely beating Ampere
  Altra. This is the right home for the tweak-and-re-run loop. Caveat: it is a laptop, so
  long unattended runs mean heat, noise and sleep behaviour.
- **AWS Lambda - burst tier.** The right primitive for wide configuration sweeps:
  embarrassingly parallel, minutes of wall clock, cheap (Graviton ~$0.0000133/GB-s, with
  400,000 GB-s/month free). Watch the 15-minute invocation cap against long sims. Notably,
  this is what Woogles itself runs BestBot on.
- **GitHub Actions - downgraded from the original recommendation.** Public-repo runners
  are 4-core/16 GB, free and unmetered, and the cron muscle already exists
  (`woogles-report.yml`, `woogles-snapshot.yml`, state on the `woogles-data` branch). It
  was the original pick on cost grounds. Demoted because sustained *research* compute is a
  much weaker fit for GitHub's terms than CI, and because 6-hour job caps plus no
  persistent state make experiment iteration awkward.
- **Rented box (Hetzner CAX31, 8 ARM vCPU / 16 GB, ~EUR 20.99/mo) - defer.** Only wins by
  being always-on and not commandeering the laptop, and is probably slower per core than
  the M1 Pro. Revisit if the experiment cadence proves it, or if the teaching service
  becomes real.

**Pricing here is perishable.** Hetzner repriced on 2026-06-15: dedicated-vCPU CCX rose
113-175% (CCX13 EUR 15.99 to EUR 42.99/mo) while ARM CAX rose only ~30%. The same
evaluation run two months earlier produced a different answer. Re-check before acting.

## Integration: an engine interface, not a field swap

`scripts/tournament_report.py`, the Curley tracker, the daily report email and
`scripts/audit_woogles_consistency.py` are all keyed on Woogles BestBot results fetched
through the API.

BYOE implies an abstraction in which **Woogles is one implementation and MAGPIE another**.
Inference-aware output is a *different and better* answer, not a drop-in replacement, so
it must not silently overwrite those fields. Side-by-side comparison ("Woogles says X,
inference-aware MAGPIE says Y") falls out of that interface for free - and the
divergences are the interesting product, both for Jesse's own study and as teaching
material.

The real expense of this project is this integration work, not compute.

## Pending inputs from Jesse

Reserved; fill these in as they arrive. Do not guess at any of them.

- **His game-analysis style.** Jesse will teach this directly. It sets the simulation
  settings and the output shape, so neither the engine nor the hosting decision can be
  finalised without it. This is the gating input.
- **MAGPIE / Macondo briefing.** Jesse will cover both. Includes where his existing MAGPIE
  work lives: no MAGPIE checkout was found under `~/projects`, `~/Desktop` or
  `~/Documents` on this machine, so whether this is "scale up what exists" or "start
  fresh" is genuinely unknown.
- **Which inference variety is the expensive one**, and which one his work uses.

## Suggested first task

**Do not start with hosting.** Establish the local baseline instead:

1. Get MAGPIE building on the M1. It is C with a Makefile and a `setup.sh` that downloads
   lexical data; its documentation does not mention macOS, so prove that build first.
   Macondo, being Go, cross-compiles to macOS/ARM cleanly if MAGPIE fights back.
2. Time one real archive position, inference-on versus inference-off, at a fixed seed.

That single measurement replaces every estimate in this document and establishes whether
hosting is a question at all. If a curated position set runs comfortably on the laptop,
there is nothing to decide yet.

## Sources

- Hetzner June 2026 reprice: <https://byteiota.com/hetzner-june-2026-price-shock/>
- Hetzner pricing calculator: <https://costgoat.com/pricing/hetzner>
- GitHub-hosted runner specs:
  <https://docs.github.com/en/actions/reference/runners/github-hosted-runners>
- GitHub Actions 2026 pricing: <https://cicdcalculator.com/github-actions-free-tier>
- AWS Lambda pricing: <https://costgoat.com/pricing/aws-lambda>
- BestBot algorithms (confirms Lambda + all-cores Monte Carlo):
  <https://blog.woogles.io/posts/2025-05-04-the-mathematics-and-algorithms-behind-bestbot/>
- MAGPIE: <https://github.com/jvc56/MAGPIE>
- Macondo: <https://github.com/domino14/macondo>

## Related

`docs/report-cost-refactor-handoff.md` touches the same pipeline from the opposite end:
that one removes LLM cost from report *generation*, this one adds a better *analysis
source* upstream of it. If both land, the report pipeline becomes deterministic code fed
by a pluggable engine.
