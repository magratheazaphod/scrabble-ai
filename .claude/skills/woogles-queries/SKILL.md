---
name: woogles-queries
description: Run and optimize SQL reporting queries against the woogles.io production reporting database (repo ~/projects/liwords, folder reporting/). Use this whenever Jesse asks to write, run, debug, or speed up a reporting query, mentions the Wireguard tunnel / reporting DB, the DBHub MCP ("woogles-db"), or wants query results as CSV/terminal output.
---

# Woogles.io Reporting Queries

Jesse (magrathean) maintains ad-hoc SQL reporting queries against the woogles.io
production/reporting Postgres DB. Queries live in `~/projects/liwords/reporting/`
(a **different repo** from `scrabble-ai`) as `.sql` files, e.g.
`reporting/omgwords/games_per_month.sql`.

> **READ-ONLY / PRODUCTION SAFETY — non-negotiable.** This is the live production
> database. It is for **analytics and reporting only**. NEVER run anything that
> mutates data or schema — no `INSERT`, `UPDATE`, `DELETE`, `TRUNCATE`, `COPY … FROM`,
> `ALTER`, `DROP`, `CREATE`, or any other DDL/DML write. Only `SELECT` /
> `EXPLAIN` / `WITH … SELECT` and other read-only statements. Session-scoped tuning
> knobs that don't touch data (`SET work_mem`, `SET statement_timeout`) are fine. Both
> access paths enforce this at the server (`default_transaction_read_only` / DBHub
> `readonly = true`), but the rule stands regardless of tooling: if a task would
> change a row, refuse and surface it rather than run it.

## Running a query

Use the wrapper script — never PgAdmin's GUI runner, it's cumbersome and gives no
good way to view results:

```bash
~/projects/liwords/reporting/scripts/run_query.sh reporting/omgwords/games_per_month.sql
~/projects/liwords/reporting/scripts/run_query.sh reporting/omgwords/games_per_month.sql --no-open
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

## Querying conversationally via Claude Code (DBHub MCP)

For authoring, iterating on, and `EXPLAIN`-analyzing queries *inside a Claude Code
session*, the reporting DB is wired in as the **`woogles-db`** MCP server. This is
the AI-native path: ask Claude to write/run/optimize a query and it executes over
MCP and reads the results directly — no copy-pasting into a GUI.

**Before your first query (preflight):**
1. Bring up the tunnel: `scutil --nc start Woogles` (the MCP won't auto-start it).
2. Confirm the server is live: `claude mcp list` → `woogles-db … ✔ Connected`. If it's
   missing, run the `claude mcp add …` command below, then **restart the session** —
   MCP servers load only at startup.
3. Smoke-test: ask Claude to run `select now(), current_user;` via the `execute_sql`
   tool. One row back = you're good.

Then ask for what you want in plain English — Claude writes the SQL, runs it read-only
through `execute_sql`, and can introspect the schema (tables/columns/joins) to get it right.

- **Server**: [DBHub](https://github.com/bytebase/dbhub) (`@bytebase/dbhub`, MIT,
  actively maintained). *Not* crystaldba's `postgres-mcp` — that stalled after its
  Sept 2025 Temporal acquisition.
- **Config**: `~/projects/liwords/reporting/dbhub.toml` (gitignored, `chmod 600`).
  Read-only (`readonly = true` on `execute_sql`), `max_rows = 5000`, a client-side
  `query_timeout = 60`s, and a **server-side** `statement_timeout=60000` in the DSN
  `options` so a runaway query cancels the backend instead of orphaning it. The DSN
  omits the password → `pg` reads it from `~/.pgpass`, same as `run_query.sh`.
- **Bring the VPN up first** — the MCP does **not** auto-start the tunnel the way
  `run_query.sh` does: `scutil --nc start Woogles`.
- **(Re)install the MCP server** if `claude mcp list` doesn't show `woogles-db`:
  ```bash
  claude mcp add --scope user woogles-db -- \
    npx -y @bytebase/dbhub@latest --transport stdio \
    --config=/Users/Siwen/projects/liwords/reporting/dbhub.toml
  ```
- **`EXPLAIN`** works as a normal read-only statement — run
  `EXPLAIN (ANALYZE, BUFFERS) …` through the MCP for optimization work. DBHub has no
  automated index-tuner, so the manual `EXPLAIN` discipline in *DB performance notes*
  below still applies.

**When to use which:** MCP for interactive authoring / iteration / `EXPLAIN`;
`run_query.sh` remains canonical for heavy, one-shot, or scheduled runs — it owns the
VPN auto-reconnect and the duplicate-run lock the MCP path lacks. The MCP's only
guard against orphaned heavy backends is the `statement_timeout` above, so bump it
inline for a deliberately long query rather than removing it.

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

## Viewing results beyond the terminal table

For anything more than a glance, open a throwaway Jupyter notebook. Two ways:

**Against a CSV** produced by `run_query.sh`:

```python
import pandas as pd
df = pd.read_csv('reporting/_results/<file>.csv', parse_dates=['month'])  # adjust date cols
df
```

**Or query the DB directly** with the reusable helper
`reporting/scripts/nb_helper.py` (read-only, 60s statement timeout, password from `~/.pgpass`;
VPN must be up). Copy `reporting/scripts/query_scratch_template.ipynb` and:

```python
from nb_helper import run_sql, run_file
df = run_sql("select count(*) from games where created_at > '2025-01-01'")
df = run_file('omgwords/games_per_month.sql')   # plain single-statement .sql only
```

Needs `pandas`, `sqlalchemy`, `psycopg[binary]`, `matplotlib` in the kernel — one-time
setup (run from anywhere; paths are absolute):

```bash
python3 -m venv ~/projects/liwords/reporting/.venv
source ~/projects/liwords/reporting/.venv/bin/activate
pip install pandas sqlalchemy "psycopg[binary]" matplotlib jupyterlab ipykernel
python -m ipykernel install --user --name woogles-reporting --display-name "Woogles reporting"
```

Then pick the **Woogles reporting** kernel in Jupyter/VSCode (`.venv/` is gitignored).
`venv` = isolated Python sandbox; `activate` enters it; `pip install` adds the libs;
`ipykernel install` registers it as a pickable Jupyter kernel. For psql-specific
files (`\set`, multiple statements) use `run_query.sh` instead. Save scratch
notebooks as `reporting/_results/<name>_scratch.ipynb` and open in VSCode — these are
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
- **`jsonb_each` on profile JSON blows up on some rows** — `profiles.stats` /
  `profiles.ratings` are not always `{"Data": {...}}`; some rows are null or a
  non-object, and `jsonb_each(p.stats->'Data')` fails the whole query with
  `ERROR: cannot call jsonb_each on a non-object`. Guard every expansion:
  ```sql
  jsonb_each(CASE WHEN jsonb_typeof(p.stats->'Data') = 'object'
                  THEN p.stats->'Data' ELSE '{}'::jsonb END)
  ```
  Useful shapes inside, per variant key (e.g. `CSW19.classic.regular`):
  games played = `stats->'Data'-><variant>->'d1'->'Games'->>'t'`, rating =
  `ratings->'Data'-><variant>->>'r'`. Summing these per user is far cheaper than
  counting from the 12M-row `games` table.
- The `games` table is ~12M rows (`created_at > '2020-01-01'` alone matches all of
  them) — full-table CTEs joining against it are inherently heavy. Prefer adding a
  `WHERE` filter as early as possible in the CTE chain rather than filtering after a
  join, and check `EXPLAIN` for a seq scan where an index scan should apply.

## Connection details (for reference, not to be duplicated elsewhere)

- Host `10.0.0.76:5432`, db `liwords`, user `jesse`, reached only over the "Woogles"
  WireGuard VPN.
- Credentials live in `~/projects/liwords/reporting/.env` (connection params) and
  `~/.pgpass` (password) — both gitignored/outside any repo. Never hardcode the
  password in a query file, notebook, or committed script.
