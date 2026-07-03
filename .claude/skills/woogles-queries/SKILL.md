---
name: woogles-queries
description: Run and optimize SQL reporting queries against the woogles.io production reporting database (repo ~/projects/liwords, folder reporting/). Use this whenever Jesse asks to write, run, debug, or speed up a reporting query, mentions the Wireguard tunnel / reporting DB, or wants query results as CSV/terminal output.
---

# Woogles.io Reporting Queries

Jesse (magrathean) maintains ad-hoc SQL reporting queries against the woogles.io
production/reporting Postgres DB. Queries live in `~/projects/liwords/reporting/`
(a **different repo** from `scrabble-ai`) as `.sql` files, e.g.
`reporting/omgwords/games_per_month.sql`.

## Running a query

Use the wrapper script — never PgAdmin's GUI runner, it's cumbersome and gives no
good way to view results:

```bash
~/projects/liwords/reporting/run_query.sh reporting/omgwords/games_per_month.sql
~/projects/liwords/reporting/run_query.sh reporting/omgwords/games_per_month.sql --no-open
```

What it does:
- Ensures the "Woogles" WireGuard VPN tunnel is up (`scutil --nc start Woogles`),
  auto-reconnecting if it dropped — waits up to 15s for the DB to become reachable.
- Runs the query with `psql`, using connection details from `reporting/.env`
  (gitignored: host/port/db/user) and the password from `~/.pgpass`
  (`10.0.0.76:5432:liwords:jesse:...`) — no password prompt.
- Prints results to the terminal (`column -s, -t`), saves them to
  `reporting/_results/<basename>_<timestamp>.csv`, and opens the CSV (unless
  `--no-open`).
- **Duplicate-run guard**: refuses to start a second run while one is already in
  flight (atomic `mkdir`-based lock at `reporting/_results/.run_query.lock`, with
  PID-liveness checking so a stale lock self-heals). If blocked, it prints the PID
  and query of the running job and a manual override
  (`kill <pid> && rm -rf .../.run_query.lock`). This exists because orphaned
  duplicate queries previously stacked up on the DB server — see Troubleshooting.

## Viewing results beyond the terminal table

For anything more than a glance, open a throwaway Jupyter notebook against the CSV:

```python
import pandas as pd
df = pd.read_csv('reporting/_results/<file>.csv', parse_dates=['month'])  # adjust date cols
df
```

Save it as `reporting/_results/<name>_scratch.ipynb` and open in VSCode — these are
throwaway, no need to keep them tidy or commit them (`_results/` is gitignored).

## All-in-VSCode alternative

`~/projects/liwords/.vscode/settings.json` has a pre-configured connection profile
("Woogles reporting DB") for the official `ms-ossdata.vscode-pgsql` extension, as an
alternative to `run_query.sh` when you want to edit + run + browse results without
leaving VSCode.

## DB performance notes (for query optimization work)

- **`work_mem` is already raised to 256MB at the role level** (`ALTER ROLE jesse SET
  work_mem = '256MB'`), scoped only to Jesse's login — do not use
  `ALTER SYSTEM SET work_mem` (global, would affect the production app and risk OOM
  under concurrent load). If a query still spills to disk, check
  `EXPLAIN (ANALYZE, BUFFERS)` for `Sort Method: external merge` / hash-join spills
  before considering a further per-session bump (`SET work_mem = 'Xmb';` inline in a
  one-off query, not a role-wide change) — verify against `shared_buffers` /
  `effective_cache_size` first, don't just guess bigger is safe.
- **Orphaned duplicate queries are the most common cause of a query "suddenly"
  taking 10x longer.** Killing the local `psql`/script process does NOT reliably
  cancel the server-side backend. Check for stragglers:
  ```sql
  SELECT pid, state, wait_event, query_start, left(query, 80)
  FROM pg_stat_activity
  WHERE usename = 'jesse' AND state != 'idle';
  ```
  Cancel with `SELECT pg_cancel_backend(<pid>);`. `run_query.sh`'s duplicate-run
  guard prevents new stacking but won't clean up backends orphaned before it existed
  or killed outside the script.
- The `games` table is ~12M rows (`created_at > '2020-01-01'` alone matches all of
  them) — full-table CTEs joining against it are inherently heavy. Prefer adding a
  `WHERE` filter as early as possible in the CTE chain rather than filtering after a
  join, and check `EXPLAIN` for a seq scan where an index scan should apply.

## Scope: stay inside reporting/

`reporting/` is Jesse's folder within the `liwords` repo — he owns it outright and
uses it independently of the rest of the app. **Never let changes spill outside
`reporting/`** (app code, other top-level dirs, root configs, etc.), even
incidentally:
- File edits, new scripts, cron/launchd automation, and generated artifacts
  (CSVs, logs, `.env`-style credential files) all belong under `reporting/`.
- When staging or committing, `git add` only paths under `reporting/` — never a
  broad `git add -A`/`git add .` from the repo root.
- If a PR or commit against `liwords` is ever needed for this work, its diff
  should touch only `reporting/**`. If a task seems to require a change outside
  `reporting/`, stop and check with Jesse before touching it — don't assume it's
  in scope just because it's convenient.

## Connection details (for reference, not to be duplicated elsewhere)

- Host `10.0.0.76:5432`, db `liwords`, user `jesse`, reached only over the "Woogles"
  WireGuard VPN.
- Credentials live in `~/projects/liwords/reporting/.env` (connection params) and
  `~/.pgpass` (password) — both gitignored/outside any repo. Never hardcode the
  password in a query file, notebook, or committed script.
