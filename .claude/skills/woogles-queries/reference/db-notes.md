# Reporting DB reference

Connection details, performance notes, and result-viewing setup for the
woogles.io reporting database. The read-only rule in `SKILL.md` applies to
everything here.

## Connection

- Host `10.0.0.76:5432`, db `liwords`, user `jesse`, reached **only** over the
  "Woogles" WireGuard VPN.
- Credentials: `~/projects/liwords/reporting/.env` (connection params) and
  `~/.pgpass` (password) - both gitignored / outside any repo. **Never hardcode
  the password** in a query file, notebook, or committed script.

## Viewing results beyond the terminal table

For anything more than a glance, open a throwaway Jupyter notebook.

**Against a CSV** produced by `run_query.sh`:

```python
import pandas as pd
df = pd.read_csv('reporting/_results/<file>.csv', parse_dates=['month'])  # adjust date cols
df
```

**Or query the DB directly** with the reusable helper
`reporting/scripts/nb_helper.py` (read-only, 60s statement timeout, password from
`~/.pgpass`; VPN must be up). Copy
`reporting/scripts/query_scratch_template.ipynb` and:

```python
from nb_helper import run_sql, run_file
df = run_sql("select count(*) from games where created_at > '2025-01-01'")
df = run_file('omgwords/games_per_month.sql')   # plain single-statement .sql only
```

For psql-specific files (`\set`, multiple statements) use `run_query.sh` instead.

One-time kernel setup (paths are absolute; run from anywhere):

```bash
python3 -m venv ~/projects/liwords/reporting/.venv
source ~/projects/liwords/reporting/.venv/bin/activate
pip install pandas sqlalchemy "psycopg[binary]" matplotlib jupyterlab ipykernel
python -m ipykernel install --user --name woogles-reporting --display-name "Woogles reporting"
```

Then pick the **Woogles reporting** kernel in Jupyter/VSCode (`.venv/` is
gitignored). Save scratch notebooks as `reporting/_results/<name>_scratch.ipynb`
and open in VSCode - these are throwaway, no need to keep them tidy or commit
them (`_results/` is gitignored).

**All-in-VSCode alternative:** `~/projects/liwords/.vscode/settings.json` has a
pre-configured connection profile ("Woogles reporting DB") for the official
`ms-ossdata.vscode-pgsql` extension, if you want to edit + run + browse results
without leaving VSCode.

## Performance notes

- **`work_mem` is already raised to 256MB at the role level** (`ALTER ROLE jesse
  SET work_mem = '256MB'`), scoped only to Jesse's login. **Do not use
  `ALTER SYSTEM SET work_mem`** - global, would affect the production app and risk
  OOM under concurrent load. If a query still spills to disk, check
  `EXPLAIN (ANALYZE, BUFFERS)` for `Sort Method: external merge` or hash-join
  spills before considering a further per-session bump (`SET work_mem = 'Xmb';`
  inline in a one-off query, not a role-wide change). Verify against
  `shared_buffers` / `effective_cache_size` first; don't assume bigger is safe.

- **Orphaned duplicate queries are the most common cause of a query "suddenly"
  taking 10x longer.** Killing the local `psql`/script process does NOT reliably
  cancel the server-side backend:

  ```sql
  SELECT pid, state, wait_event, query_start, left(query, 80)
  FROM pg_stat_activity
  WHERE usename = 'jesse' AND state != 'idle';
  ```

  Cancel with `SELECT pg_cancel_backend(<pid>);`. `run_query.sh`'s duplicate-run
  guard prevents new stacking but won't clean up backends orphaned before it
  existed or killed outside the script.

- **`jsonb_each` on profile JSON blows up on some rows.** `profiles.stats` /
  `profiles.ratings` are not always `{"Data": {...}}`; some rows are null or a
  non-object, and `jsonb_each(p.stats->'Data')` fails the whole query with
  `ERROR: cannot call jsonb_each on a non-object`. Guard every expansion:

  ```sql
  jsonb_each(CASE WHEN jsonb_typeof(p.stats->'Data') = 'object'
                  THEN p.stats->'Data' ELSE '{}'::jsonb END)
  ```

  Useful shapes inside, per variant key (e.g. `CSW19.classic.regular`): games
  played = `stats->'Data'-><variant>->'d1'->'Games'->>'t'`, rating =
  `ratings->'Data'-><variant>->>'r'`. Summing these per user is far cheaper than
  counting from the 12M-row `games` table.

- **`game_request` JSONB key spellings changed mid-history.** Keys are camelCase
  with string enums before ~Sep 2025 and snake_case with numeric enums after -
  `COALESCE` both spellings or silently lose 5.5 years of data.

- **The `games` table is ~12M rows** (`created_at > '2020-01-01'` alone matches
  all of them), so full-table CTEs joining against it are inherently heavy. Push
  `WHERE` filters as early as possible in the CTE chain rather than filtering
  after a join, and check `EXPLAIN` for a seq scan where an index scan should
  apply.

- **A `rows=2` estimate in `EXPLAIN` is a red flag, not a rounding error.**
  Postgres has no `n_distinct` statistic for a column that a query *manufactured* -
  a `VALUES` list (`Group Key: "*VALUES*".column1`), an unnest, or an expression
  over a CTE - so when such a column is the `GROUP BY` key it falls back to a
  hardcoded guess, typically 2. That guess makes a nested loop look nearly free,
  so the planner picks `Nested Loop` + `Materialize` and rescans the whole inner
  result once per outer row.

  This cost `new_user_funnel_monthly.sql` 19+ minutes (killed, no result) on
  2026-08-02: a `CROSS JOIN LATERAL (VALUES ...)` unpivot fed a per-player
  `GROUP BY`, estimated at 2 rows against a truth near 100k, joined to ~240k
  `users` - roughly 2.4x10^10 comparisons for what should be a cheap join.

  **The fix is structural, not a hint - Postgres has no query hints.** Never join
  a stats-less aggregate at fine grain. Aggregate every side to the *coarse*
  grain first (month, week) and join on the ~70 resulting rows, where a wrong
  estimate cannot hurt. The bad estimate remains in the plan; it just stops being
  load-bearing. Confirm by checking that `Nested Loop ... Join Filter` against a
  large table is gone and `Merge`/`Hash Join` replaced it.

  Related but distinct from the materialized-CTE trap noted in
  `omgwords/games_per_month.sql`: same root cause (no stats), different symptom.

  Measured on 2026-08-02, `new_user_funnel_monthly.sql` whole history: original
  four-scan version **6:13**, single-scan version **2:05** (3x). The broken
  intermediate with the nested loop ran 19+ min and was killed without finishing.

- **Total-cost ratios in `EXPLAIN` are a poor predictor of wall-clock.** The
  funnel refactor showed 72.2M vs 4.16M estimated cost (~17x) and delivered 3x.
  Use costs to compare *plan shapes* - a nested loop against a large table versus
  a merge join - not to promise a speedup. Measure before quoting a number.

- **`COUNT(DISTINCT x)` disqualifies `HashAggregate`.** It forces a sort-based
  `GroupAggregate`, so every input row gets sorted - 8.4M rows in the funnel
  query, spilling to disk. Worth knowing before assuming a slow aggregate is the
  join's fault; it is often just the `DISTINCT`.

## Benchmark reference: `omgwords/games_per_month.sql`

Whole-history full run (seq scan of all ~12M `games` + two `users` LEFT JOINs +
HashAggregate by month), measured with `EXPLAIN (ANALYZE, BUFFERS)` on
2026-07-12:

- **Baseline** (bot flags, end-reason buckets - one `game_request` deref): **~176 s**
- **+ rated / CSW-vs-NWL / language columns** (3-4 extra `game_request` JSONB key
  derefs per row): **~212 s**, i.e. **+~35 s (~20%)**
- Adding the two variant columns (`zomgwords`/`wordsmog`, board-layout + variant
  derefs) is the same class of per-row JSONB lookup - expect a similar small
  marginal bump, not a step change.

Takeaway: extra `->>`/`->` extractions on the *already-loaded* `game_request`
JSONB are cheap relative to the scan+join, but not free - each key adds a per-row
deref over 12M rows (~10 s each here). The scan/join dominates; the plan shape is
unchanged. Single-run numbers with `EXPLAIN ANALYZE` timing overhead, so treat as
±10% and re-measure before optimizing.
