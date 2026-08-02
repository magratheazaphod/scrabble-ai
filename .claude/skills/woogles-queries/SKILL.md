---
name: woogles-queries
description: Run and optimize SQL reporting queries against the woogles.io production reporting database (repo ~/projects/liwords, folder reporting/). Use this whenever Jesse asks to write, run, debug, or speed up a reporting query, mentions the Wireguard tunnel / reporting DB, the DBHub MCP ("woogles-db"), or wants query results as CSV/terminal output.
---

# Woogles.io reporting queries

Jesse maintains ad-hoc SQL against the woogles.io production/reporting Postgres
DB. Queries live in `~/projects/liwords/reporting/` - **a different repo from
`scrabble-ai`** - as `.sql` files, e.g. `reporting/omgwords/games_per_month.sql`.

Connection details, notebook setup, and DB performance notes:
`reference/db-notes.md`.

> **READ-ONLY / PRODUCTION SAFETY - non-negotiable.** This is the live production
> database, for **analytics and reporting only**. NEVER run anything that mutates
> data or schema - no `INSERT`, `UPDATE`, `DELETE`, `TRUNCATE`, `COPY … FROM`,
> `ALTER`, `DROP`, `CREATE`, or any other DDL/DML write. Only `SELECT` /
> `EXPLAIN` / `WITH … SELECT` and other read-only statements. Session-scoped
> tuning knobs that don't touch data (`SET work_mem`, `SET statement_timeout`)
> are fine. Both access paths enforce this at the server
> (`default_transaction_read_only` / DBHub `readonly = true`), but the rule
> stands regardless of tooling: if a task would change a row, refuse and surface
> it rather than run it.

## Scope: stay inside `reporting/`

`reporting/` is Jesse's folder within the `liwords` repo - he owns it outright and
uses it independently of the rest of the app. **Never let changes spill outside
`reporting/`**, even incidentally:

- File edits, new scripts, cron/launchd automation, and generated artifacts
  (CSVs, logs, `.env`-style credential files) all belong under `reporting/`.
- When staging or committing, `git add` only paths under `reporting/` - never a
  broad `git add -A`/`git add .` from the repo root.
- Any PR or commit against `liwords` should touch only `reporting/**`. If a task
  seems to require a change outside it, **stop and check with Jesse** - don't
  assume it's in scope just because it's convenient.

## Let queries run - do not cancel them

**A slow query is not a stuck query. Let it finish.** Jesse's queries routinely
run for many minutes against a 12M-row `games` table; that is normal and expected,
and the result is usually the whole point of the task.

- **Never `pg_cancel_backend` a query Jesse is waiting on**, and never cancel one
  just because you have decided to rewrite it. Cancelling destroys the evidence -
  the wall-clock time, the actual row counts, the output to diff against.
- The only justification is a genuinely runaway query: **over ~40 minutes** with
  no end in sight, or one that is harming the production DB. Say so and ask first
  if there is any way to ask.
- If Jesse asks *why* something is slow, answer from `EXPLAIN` while it keeps
  running. Killing it to "investigate faster" is backwards.
- Run long queries with `run_in_background: true` so the session stays responsive
  instead of tempting a timeout-driven cancel.

**`EXPLAIN` before running any new candidate.** A plain `EXPLAIN` (no `ANALYZE`)
costs nothing and catches the misestimate-driven nested loops documented in
`reference/db-notes.md` before they burn 20 minutes. When refactoring for
performance, check the plan shape first, then run.

**Prove a refactor didn't change the numbers.** Run the old and new queries and
diff the CSVs column-for-column. If the change also alters logic on purpose, build
a third *perf-only* variant that isolates the two: perf-only must match the old
output exactly, and the intended delta then shows up only in the final version.

## Running a query

Use the wrapper script - never PgAdmin's GUI runner, it's cumbersome and gives no
good way to view results:

```bash
~/projects/liwords/reporting/scripts/run_query.sh reporting/omgwords/games_per_month.sql
~/projects/liwords/reporting/scripts/run_query.sh reporting/omgwords/games_per_month.sql --no-open
```

What it does:

- Ensures the "Woogles" WireGuard tunnel is up (`scutil --nc start Woogles`),
  auto-reconnecting if it dropped - waits up to 15s for the DB to become
  reachable.
- Runs the query with `psql` using `reporting/.env` and `~/.pgpass` - no prompt.
- Prints results to the terminal (`column -s, -t`), saves them to
  `reporting/_results/<basename>_<timestamp>.csv`, and opens the CSV unless
  `--no-open`.
- **Duplicate-run guard:** refuses to start a second run while one is in flight
  (atomic `mkdir` lock at `reporting/_results/.run_query.lock`, with PID-liveness
  checking so a stale lock self-heals). If blocked it prints the running job's PID
  and query plus a manual override. This exists because orphaned duplicate
  queries previously stacked up on the DB server.

## Querying conversationally (DBHub MCP)

For authoring, iterating, and `EXPLAIN`-analyzing queries *inside a Claude Code
session*, the reporting DB is wired in as the **`woogles-db`** MCP server. Ask
for what you want in plain English - Claude writes the SQL, runs it read-only
through `execute_sql`, and can introspect the schema.

**Preflight before the first query:**

1. Bring up the tunnel: `scutil --nc start Woogles` (the MCP won't auto-start it).
2. Confirm it's live: `claude mcp list` → `woogles-db … ✔ Connected`. If missing,
   run the install command below, then **restart the session** - MCP servers load
   only at startup.
3. Smoke-test: run `select now(), current_user;` via `execute_sql`.

```bash
claude mcp add --scope user woogles-db -- \
  npx -y @bytebase/dbhub@latest --transport stdio \
  --config=/Users/Siwen/projects/liwords/reporting/dbhub.toml
```

- **Server:** [DBHub](https://github.com/bytebase/dbhub) (`@bytebase/dbhub`, MIT,
  actively maintained). *Not* crystaldba's `postgres-mcp` - that stalled after its
  Sept 2025 Temporal acquisition.
- **Config:** `~/projects/liwords/reporting/dbhub.toml` (gitignored, `chmod 600`).
  Read-only, `max_rows = 5000`, client-side `query_timeout = 60`s, and a
  **server-side** `statement_timeout=60000` in the DSN `options` so a runaway
  query cancels the backend instead of orphaning it. The DSN omits the password →
  `pg` reads it from `~/.pgpass`, same as `run_query.sh`.
- **`EXPLAIN` works** as a normal read-only statement - run
  `EXPLAIN (ANALYZE, BUFFERS) …` through the MCP for optimization work. DBHub has
  no automated index-tuner, so the manual `EXPLAIN` discipline in
  `reference/db-notes.md` still applies.

**When to use which:** MCP for interactive authoring, iteration, and `EXPLAIN`;
`run_query.sh` remains canonical for heavy, one-shot, or scheduled runs - it owns
the VPN auto-reconnect and the duplicate-run lock the MCP path lacks. The MCP's
only guard against orphaned heavy backends is that `statement_timeout`, so bump
it inline for a deliberately long query rather than removing it.
