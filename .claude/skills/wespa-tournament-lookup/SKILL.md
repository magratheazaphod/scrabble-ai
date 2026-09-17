---
name: wespa-tournament-lookup
description: Identify which real-world tournament a set of Jesse's games came from, using WESPA's official results database (wespa.xerafin.net) - the event's true name, the date it was played, his division, record, spread and finishing place, and the round-by-round official scores. Use whenever a tournament is uploaded or a collection is created, when a collection needs a date for the skill graph's chronological axis, when Jesse asks "which tournament was this" or wants an event's standings/results, or when a Woogles collection title is too vague to place. Covers the score-fingerprint match, its confidence tiers, and what to do when an event isn't WESPA-rated.
---

# WESPA tournament lookup

A Woogles collection title is whatever Jesse typed when he made it. WESPA's
results database knows what the tournament was actually **called**, **when it was
played**, and how he **finished** - and a Woogles game history carries no date at
all (`added_at` is the upload, often years later), so this is the only source of
event dates in the repo.

Run it whenever a tournament lands: right after `/gcg-upload` or
`/otb-scrabble-upload` creates a collection, and before any report or chart that
wants a date.

**`scripts/wespa_tournaments.py` does all of it.** Transport (headers, the
403-on-default-User-Agent quirk, backoff) is reused from `wespa_ratings.py`,
which stays the single door onto wespa.xerafin.net - never write a new one.

## Match on scores, never on names

Names do not match and never will. Jesse's "King's Cup 2019" is WESPA's *34th
BRAND's Thailand International Crossword Game*; his "NSC 2019" is *North American
Scrabble Championship - Collins Division*. The reliable key is the **round-by-round
score pair**: every game in the collection is (his score, theirs), and so is every
round of the official result.

So the script takes Jesse's own rated events - a bounded list of ~42, no guessing
- and asks which one explains this collection's games. Exact score pairs identify
on their own; a near miss counts only when the opponent's name agrees too,
because the two records genuinely disagree by a few points (the official score is
the paper scoresheet; the collection's is a replay of the reconstructed GCG).

```
python3 scripts/wespa_tournaments.py --identify "WESPAC 2019"
python3 scripts/wespa_tournaments.py --identify-all            # every collection
python3 scripts/wespa_tournaments.py --identify-all --write-dates
```

## Read the confidence tier, don't skip it

| Tier | Means | Do |
|---|---|---|
| `certain` | every game matched, or ≥85% of ≥5 | Use it. Write the date. |
| `likely` | most games matched | Use it, but look at what didn't match - a partial upload is normal, a wrong opponent is not |
| `weak` | a handful matched | **Do not write a date.** Confirm against `--standings` or ask Jesse |
| `none` | nothing, or background coincidence | Not a WESPA-rated event. See below |

`--write-dates` only ever writes `certain` and `likely`, and merges into
`.github/event-dates.json` without clobbering a hand-written `label`. That file is
what `scripts/skill_graph.py` reads for its chronological axis.

## When the answer is `none`

It usually means the event genuinely isn't WESPA-rated, which is a real answer,
not a failure. Two known cases in this archive:

- **Club and one-day events** (`Austin One-Day Aug '23`) - NASPA-rated, so WESPA
  has nothing. Date it by hand in `.github/event-dates.json`.
- **A playoff that wasn't rated separately** (`King's Cup 2019 Finals`) - the main
  event is rated but the best-of-three final isn't. Date it a day after its parent
  event, which is what the axis needs anyway.

Before concluding it's unrated, try the name search - it covers the whole
database, not just Jesse's events:

```
python3 scripts/wespa_tournaments.py --search "Causeway"
python3 scripts/wespa_tournaments.py --search "Open" --country USA --from-date 2023-08-01
```

## The rest of the CLI

```
--list                      every rated event Jesse has played, oldest first
--rounds <tourneyid>        his round-by-round result: opponent, score, W/L
--standings <tourneyid>     full standings, all divisions, with ratings
--player "Name"             anyone, not just Jesse (resolved via wespa_ratings)
--force                     ignore the 30-day local cache
```

Results are cached to `data/wespa-tournaments.json` (gitignored - it's a bulk
copy of someone else's dataset, and this repo is public). A finished result never
changes, so the cache is only refreshed for new events.

## What this is good for beyond dates

- **Confirming an upload is complete.** `--identify` reports matched games against
  the event's round count. "27 matched of the event's 32 rounds" says five rounds
  are missing from Woogles - which is either deliberate or a gap worth naming.
- **Catching a mis-transcribed score.** A game that only matches on the name, with
  the scores a few points off, is exactly where a reconstruction went wrong. Note
  it; `/fix-uploaded-game` handles the repair.
- **Report context.** Division, finishing place, spread and the field's ratings all
  come back with the match, and none of it is in the Woogles data.

## Ask Jesse rather than guessing

- Two events match at similar strength (rare - it means a genuinely ambiguous
  upload).
- A `weak` match you're tempted to accept because the name looks right. The name
  looking right is not evidence; that is the whole premise of this skill.
- An event `none` can't place and `--search` can't find - he'll know whether it
  was rated, and under which body.
