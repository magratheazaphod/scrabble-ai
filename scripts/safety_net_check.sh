#!/bin/bash
# Safety net for the "Woogles report email" GitHub Actions workflow. GitHub's
# `schedule` cron trigger is best-effort and has silently failed to fire for
# a full day at least twice (2026-07-02, 2026-07-03) with no error surfaced
# anywhere — this script is the independent check that notices and recovers.
#
# Runs daily from a LaunchAgent (see com.jesseday.scrabble-report-safety-net.plist).
# Checks the send-status marker on the woogles-data branch; if today's report
# hasn't gone out yet, manually dispatches the workflow rather than waiting
# on GitHub's scheduler.
set -euo pipefail

REPO_DIR="/Users/Siwen/projects/scrabble-ai"
GH="/opt/homebrew/bin/gh"
GIT="/opt/homebrew/bin/git"
REPO="magratheazaphod/scrabble-ai"

cd "$REPO_DIR"

TODAY=$(TZ="America/Chicago" date +%Y-%m-%d)

"$GIT" fetch origin woogles-data -q
LAST_SENT=$("$GIT" show origin/woogles-data:data/last-report-sent.txt 2>/dev/null || echo "")

echo "$(date): today=$TODAY last-sent=$LAST_SENT"

# A late-queued scheduled run may still be on its way to sending; don't pile
# a second run on top of it (the workflow's concurrency group would make it
# wait and then no-op, but there's no point starting it). If the lookup
# itself fails, fall through and dispatch - a duplicate beats a missed day.
ACTIVE=$("$GH" run list --workflow woogles-report.yml --repo "$REPO" --limit 10 \
    --json status --jq '[.[] | select(.status != "completed")] | length' || echo 0)

if [ "$LAST_SENT" != "$TODAY" ] && [ "$ACTIVE" != "0" ]; then
    echo "$(date): no report recorded for $TODAY yet, but $ACTIVE run(s) already queued/in progress - not dispatching"
elif [ "$LAST_SENT" != "$TODAY" ]; then
    echo "$(date): no report recorded for $TODAY yet — triggering workflow_dispatch"
    "$GH" workflow run woogles-report.yml --repo "$REPO"
else
    echo "$(date): report already sent for $TODAY — nothing to do"
fi
