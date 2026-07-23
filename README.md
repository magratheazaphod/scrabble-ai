# scrabble-ai

A personal Scrabble data pipeline for Jesse Day (woogles.io: `magrathean`): a
game archive, an upload path from paper scoresheets or `.gcg` files to
woogles.io, automated BestBot-analysis reporting, and a practice-game
tracker — driven by Claude Code skills and scripts, with GitHub Actions
running the daily work unattended.

## What's here

- **Tournament game archive** — `Tournament Games/<year>/<tournament>/` (2010–2025)
  and `Practice Games/James Curley/`, plus `board-pictures/` for scoresheets
  awaiting reconstruction.
- **Uploading games to Woogles** — `.gcg` files upload directly via the Woogles
  API (`gcg-upload` skill); over-the-board games get reconstructed from
  scoresheet + board photos first, then uploaded the same way
  (`otb-scrabble-upload` skill).
- **Tournament reports** — `reports/` holds generated stats reports (record,
  scores, mistakes, bingos, etc.) pulled from Woogles BestBot analysis
  (`tournament-analysis` skill). A GitHub Actions cron regenerates and emails
  these daily.
- **Curley tracker** — a Google Sheet logging every practice game against
  James Curley, auto-enriched with BestBot stats and kept in sync with a
  matching Woogles collection (`curley-tracker` skill). The same cron audits
  OCR-reconstructed games nightly for tracker/Woogles/repo consistency.
- **Lexicon lookup** — validates words against CSW/NWL lexica for resolving
  OCR reads or confirming plays.
- **Reporting queries** — ad-hoc SQL against the woogles.io production DB
  (lives in the sibling `liwords` repo).

All the deterministic logic lives in `scripts/`; the `.claude/skills/`
directory has one skill per workflow above with the full details. `data/` and
`.github/` hold pipeline state for the automation.

## Generating a new report by hand

Use the `tournament-analysis` skill (`.claude/skills/tournament-analysis/SKILL.md`)
via Claude Code. All API calls use the Woogles `X-Api-Key` stored in `.env` at
the project root (gitignored).

## Reports

- [`reports/causeway-2026-report.md`](reports/causeway-2026-report.md) — Causeway 2026, 9-10 +144
- [`reports/causeway-2026-budak-report.md`](reports/causeway-2026-budak-report.md) — Causeway 2026, Budak
- [`reports/austin-one-day-aug-23-report.md`](reports/austin-one-day-aug-23-report.md) — Austin One-Day Aug '23, 5-1 +500
- [`reports/wsc-2018-finals-nigel-richards-report.md`](reports/wsc-2018-finals-nigel-richards-report.md) — WSC 2018 Finals, Nigel Richards
- [`reports/wespac-2019-final-nigel-richards-report.md`](reports/wespac-2019-final-nigel-richards-report.md) — WESPAC 2019 Final, Nigel Richards
