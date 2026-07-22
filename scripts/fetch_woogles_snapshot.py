#!/usr/bin/env python3
"""Harvest Woogles.io collection/game data and write a snapshot for downstream report generation.

Runs from GitHub Actions (unrestricted egress) since some cloud agent environments
can't reach woogles.io directly. Does NOT compute stats or build reports — that logic
lives in .claude/skills/tournament-analysis/SKILL.md and is interpreted by the
consumer of this snapshot, so report-format changes only ever need to happen in one place.
"""
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone

import requests

API_KEY = os.environ["WOOGLES_API_KEY"]
DEFAULT_USER_UUID = "7WyqZfyQuB6SwNa2XjuZUG"  # magrathean (Jesse's own account)
BASE = "https://woogles.io/api"
HDRS = {"Content-Type": "application/json", "X-Api-Key": API_KEY}

SKIP_STATUSES = {"NOT_A_PLAYER", "GAME_NOT_ENDED", "INVALID_VARIANT"}

# Woogles' RequestAnalysis quota (15/day regular, 30/day volunteer — see
# liwords pkg/analysis/service.go) is a rolling 24h window keyed off each
# individual request's own timestamp (db/queries/analysis.sql:
# "requested_at > NOW() - INTERVAL '24 hours'"), not a calendar-day reset —
# the API exposes no reset time anywhere, so we estimate one. Since this
# script fires its whole batch of requests in one tight burst, the oldest
# request counted in the window is (to a good approximation) the *first*
# successful RequestAnalysis call of the run that hit the limit, not the
# moment of the rejected call — anchoring on the rejection would overshoot
# by the length of the burst. GetAnalysisStatus checks (read-only, not
# quota-consuming) still run every time in case games complete some other
# way (e.g. another contributor's worker picking up the backlog).
RATE_LIMIT_MARKER_PATH = "data/rate-limited-until.txt"

# A FAILED analysis is almost always a permanent defect in the uploaded game
# ('turn 18: rack "AAGIIKSUUY" has 10 tiles, max is 7' — a malformed GCG), not a
# transient queue problem. Re-requesting it every run spends a quota slot out of
# a 15/day rolling budget to reproduce the identical error, forever.
#
# So remember each failure, and re-request only when something could plausibly
# have changed. The reliable signal is the game history itself: repairing a game
# means re-uploading it, which changes its events — and GetGameHistory is a
# read, so checking costs no quota. (Status alone can't tell us: after Jesse
# repaired a game on 2026-07-22 it still reported the old FAILED status with the
# old error until a force re-request was actually issued.) The day-based retry
# is a backstop for fixes on Woogles' side, which no local signal can see.
FAILED_STATE_PATH = "data/failed-analysis.json"
FAILED_RETRY_DAYS = 7


def load_failed_state():
    if not os.path.exists(FAILED_STATE_PATH):
        return {}
    try:
        with open(FAILED_STATE_PATH) as f:
            state = json.load(f)
        return state if isinstance(state, dict) else {}
    except (OSError, ValueError):
        return {}


def save_failed_state(state):
    os.makedirs(os.path.dirname(FAILED_STATE_PATH), exist_ok=True)
    with open(FAILED_STATE_PATH, "w") as f:
        json.dump(state, f, indent=1, sort_keys=True)


def fingerprint_events(history):
    """Hash of a game's events — changes exactly when the game is re-uploaded or
    edited. Takes an already-fetched history so callers that have one don't pay
    for a second read."""
    import hashlib

    events = (history or {}).get("events") or []
    material = json.dumps(
        [[e.get("type"), e.get("rack"), e.get("position"), e.get("played_tiles"),
          e.get("score"), e.get("cumulative")] for e in events],
        sort_keys=True,
    )
    return hashlib.sha1(material.encode()).hexdigest()


def history_fingerprint(game_id):
    """fingerprint_events for a game we don't already hold the history of. None if
    the history can't be read (then we never hold the game back)."""
    try:
        history = post(
            "game_service.GameMetadataService/GetGameHistory", {"game_id": game_id}
        ).get("history") or {}
    except requests.exceptions.HTTPError:
        return None
    return fingerprint_events(history)


# Editing a game does NOT invalidate its analysis, and there is no way for us to
# force one: RequestAnalysis honours force=true only for FAILED jobs or legacy v0
# results — any current (v2) completed analysis comes back ALREADY_REQUESTED,
# "Analysis is already up to date" (liwords pkg/analysis/service.go:547). Only an
# admin RequeueAnalysis, or a fresh upload under a new game_id, actually re-runs it.
#
# So drift is reported, not repaired. Phase 3 already fetches every analyzed
# game's history, so comparing it against the fingerprint recorded when the
# analysis was last seen costs nothing — and it turns "these stats are silently
# stale" into a visible line in the log and a field in the snapshot.
ANALYZED_STATE_PATH = "data/analyzed-fingerprints.json"


def load_analyzed_state():
    if not os.path.exists(ANALYZED_STATE_PATH):
        return {}
    try:
        with open(ANALYZED_STATE_PATH) as f:
            state = json.load(f)
        return state if isinstance(state, dict) else {}
    except (OSError, ValueError):
        return {}


def save_analyzed_state(state):
    os.makedirs(os.path.dirname(ANALYZED_STATE_PATH), exist_ok=True)
    with open(ANALYZED_STATE_PATH, "w") as f:
        json.dump(state, f, indent=1, sort_keys=True)


def failed_needs_request(game_id, error, now, failed_state):
    """(should_request, why) for a game whose analysis is FAILED."""
    prev = failed_state.get(game_id)
    fingerprint = history_fingerprint(game_id)
    info = {"error": error, "history": fingerprint}
    if prev is None:
        return True, "first failure seen", info
    if fingerprint is None or prev.get("history") != fingerprint:
        return True, "game changed since it last failed", info
    last = prev.get("last_requested")
    try:
        age_days = (now - datetime.fromisoformat(last)).days if last else None
    except (TypeError, ValueError):
        age_days = None
    if age_days is None or age_days >= FAILED_RETRY_DAYS:
        return True, f"last retried {age_days if age_days is not None else 'unknown'} days ago", info
    return False, f"unchanged since it failed {age_days}d ago: {error or 'no reason given'}", info


def post(endpoint, body):
    r = requests.post(f"{BASE}/{endpoint}", json=body, headers=HDRS)
    r.raise_for_status()
    return r.json()


def resolve_user_uuid(username):
    profile = post("user_service.ProfileService/GetProfile", {"username": username})
    return profile["user_id"]


def resolve_subject_identity(results):
    """The Woogles login username (e.g. "budak") often doesn't appear anywhere in
    game data — only the in-game nickname ("CedricLewis") and real_name ("Cedric
    Lewis") do, and even those vary slightly game to game (suffixes like "(MYS)",
    inconsistent spacing). Since a one-off snapshot is built from the target's own
    collections, the subject is whichever normalized nickname appears most across
    all games — normalizing first so spelling variants of the same player don't
    split their count across multiple opponents' single-appearance tallies."""

    def normalize(nick):
        return re.sub(r"[^a-z]", "", nick.lower())

    buckets = {}
    total_games = 0
    for col in results:
        for entry in col["games"]:
            total_games += 1
            for p in entry["history"]["history"]["players"]:
                nick = p.get("nickname") or ""
                key = normalize(nick)
                if not key:
                    continue
                bucket = buckets.setdefault(key, {"count": 0, "real_names": {}})
                bucket["count"] += 1
                real_name = p.get("real_name") or ""
                if real_name:
                    bucket["real_names"][real_name] = bucket["real_names"].get(real_name, 0) + 1
    if not buckets or total_games == 0:
        return None
    key, info = max(buckets.items(), key=lambda kv: kv[1]["count"])
    real_name = max(info["real_names"].items(), key=lambda kv: kv[1])[0] if info["real_names"] else key
    return {"nickname": key, "real_name": real_name}


def enumerate_collections(target_username, target_collection_uuid):
    """Return the list of {uuid, title} collections to snapshot for this run."""
    if target_collection_uuid:
        # A specific collection was named directly (e.g. from a woogles.io URL) —
        # skip listing the owner's collections entirely and fetch just this one.
        col_resp = post(
            "collections_service.CollectionsService/GetCollection",
            {"collection_uuid": target_collection_uuid},
        )
        col_title = col_resp.get("collection", {}).get("title", "Untitled collection")
        print(f"One-off snapshot for collection: {col_title} ({target_collection_uuid})", file=sys.stderr)
        return [{"uuid": target_collection_uuid, "title": col_title}]

    user_uuid = resolve_user_uuid(target_username) if target_username else DEFAULT_USER_UUID
    if target_username:
        print(f"One-off snapshot for username: {target_username} ({user_uuid})", file=sys.stderr)

    collections, offset = [], 0
    while True:
        resp = post(
            "collections_service.CollectionsService/GetUserCollections",
            {"user_uuid": user_uuid, "limit": 50, "offset": offset},
        )
        batch = resp.get("collections", [])
        collections.extend(batch)
        if len(batch) < 50:
            break
        offset += 50
    return collections


def read_rate_limit_marker(now):
    if not os.path.exists(RATE_LIMIT_MARKER_PATH):
        return None
    with open(RATE_LIMIT_MARKER_PATH) as f:
        raw = f.read().strip()
    if not raw:
        return None
    known_until = datetime.fromisoformat(raw)
    return known_until if known_until > now else None


def scan_collection(col, rate_limited, now, failed_state):
    """Read-only pass over one collection: classify every game by what it needs.
    Consumes NO analysis quota (GetAnalysisStatus and GetGameHistory are both reads).

    Per-game state:
      done     — COMPLETED; full analysis available.
      skip     — terminally un-analyzable (phantom game_id, etc.); reportable-safe.
      requeue  — analysis FAILED and something has changed since; skip-for-report, but
                 Phase 2 fires a force=True re-analysis so a game whose data Jesse has
                 since corrected heals.
      hold     — analysis FAILED and nothing has changed since it last failed; identical
                 to requeue for reporting, but spends no quota re-proving the failure.
      request  — never analyzed (NOT_FOUND, real game); needs a fresh request.
      wait     — already queued/in-progress on Woogles; blocks reportability, no request.
    """
    col_resp = post(
        "collections_service.CollectionsService/GetCollection",
        {"collection_uuid": col["uuid"]},
    )
    games = col_resp.get("collection", {}).get("games", [])
    if not games:
        return None

    state, failed_info = {}, {}
    for g in games:
        gid = g["game_id"]
        st = post(
            "analysis_service.AnalysisService/GetAnalysisStatus",
            {"game_id": gid},
        )
        status = st.get("status")
        if status == "COMPLETED":
            state[gid] = "done"
            failed_state.pop(gid, None)  # healed — forget it ever failed
        elif status == "FAILED":
            error = (st.get("error_message") or "").strip()
            retry, why, info = failed_needs_request(gid, error, now, failed_state)
            failed_info[gid] = info
            state[gid] = "requeue" if retry else "hold"
            if retry:
                print(f"  {col['title']}: {g['chapter_title']} analysis FAILED, will retry "
                      f"— {why}", file=sys.stderr)
            else:
                print(f"  {col['title']}: holding {g['chapter_title']} — {why}",
                      file=sys.stderr)
        elif status == "NOT_FOUND":
            # NOT_FOUND usually just means "never requested". But it can also be a
            # phantom: a chapter whose game_id points to a game Woogles has no record
            # of (a GCG import that silently failed), which never becomes reportable
            # and would otherwise wedge the whole collection. When we still have quota
            # this run, Phase 2's request will 400 on a phantom and catch it there
            # (no read wasted). But when we're already rate-limited, Phase 2 is
            # skipped — so probe read-only with GetGameHistory, which errors on a
            # phantom, to disambiguate without spending any quota.
            if rate_limited:
                try:
                    post("game_service.GameMetadataService/GetGameHistory", {"game_id": gid})
                    state[gid] = "request"
                except requests.exceptions.HTTPError as e:
                    if e.response is not None and e.response.status_code == 400:
                        print(
                            f"  {col['title']}: skipping {g['chapter_title']} — no such "
                            f"game on Woogles ({e.response.text.strip()})",
                            file=sys.stderr,
                        )
                        state[gid] = "skip"
                    else:
                        raise
            else:
                state[gid] = "request"
        else:
            # Any other (non-terminal) status means Woogles already has this game
            # queued or running — no new request needed, but it isn't done yet.
            state[gid] = "wait"
    return {"uuid": col["uuid"], "title": col["title"], "games": games,
            "state": state, "failed_info": failed_info}


def needs_quota(entry):
    """How many games in this collection still need an analysis request (a quota slot).

    'hold' games are deliberately excluded: they failed and nothing has changed, so
    they need no slot and must not make a finished collection look unfinished."""
    return sum(1 for s in entry["state"].values() if s in ("request", "requeue"))


def main():
    target_username = os.environ.get("TARGET_USERNAME", "").strip()
    target_collection_uuid = os.environ.get("TARGET_COLLECTION_UUID", "").strip()
    one_off = bool(target_username or target_collection_uuid)

    collections = enumerate_collections(target_username, target_collection_uuid)
    print(f"Collections found: {len(collections)}", file=sys.stderr)

    now = datetime.now(timezone.utc)
    failed_state = load_failed_state()
    analyzed_state = load_analyzed_state()
    blocked_until = read_rate_limit_marker(now)
    rate_limited = blocked_until is not None
    first_request_at = None
    if rate_limited:
        print(
            f"Known rate-limited until {blocked_until.isoformat()} — spending no new "
            "analysis quota this run (still scanning statuses so completed games and "
            "phantoms are picked up)",
            file=sys.stderr,
        )

    # --- Phase 1: scan every collection (read-only, no quota) so Phase 2 can route
    # the day's limited analysis quota deliberately instead of letting whichever
    # collection the API lists first drain it.
    scanned = []
    for col in collections:
        print(f"--- scanning {col['title']} ---", file=sys.stderr)
        entry = scan_collection(col, rate_limited, now, failed_state)
        if entry is not None:
            scanned.append(entry)

    # --- Phase 2: spend the analysis quota on the collections CLOSEST TO DONE first.
    # Sorting by how many games still need a request (then by total size) means a
    # nearly-finished tournament gets completed — and its report generated — before a
    # big backlog like the Curley practice games consumes the whole daily quota.
    def issue(gid, force, entry, g):
        """Request analysis for one game; update its state and the rate-limit marker.
        Returns False once the daily cap is hit (caller stops issuing)."""
        nonlocal rate_limited, blocked_until, first_request_at
        request_sent_at = datetime.now(timezone.utc)
        try:
            r = post(
                "analysis_service.AnalysisService/RequestAnalysis",
                {"game_id": gid, "force": force},
            )
        except requests.exceptions.HTTPError as e:
            if e.response is not None and e.response.status_code == 400:
                # Phantom game — Woogles has no record of this game_id at all.
                print(
                    f"  {entry['title']}: skipping {g['chapter_title']} — "
                    f"{e.response.text.strip()}",
                    file=sys.stderr,
                )
                entry["state"][gid] = "skip"
                return True
            raise
        s = r.get("status", "UNKNOWN")
        if s == "RATE_LIMITED":
            # Anchor the 24h window on the first successful request of this run (the
            # oldest one actually counted), not on now — see the module comment.
            anchor = first_request_at if first_request_at else request_sent_at
            blocked_until = anchor + timedelta(hours=24)
            rate_limited = True
            print(
                f"  Daily analysis limit reached — no more requests until {blocked_until.isoformat()}",
                file=sys.stderr,
            )
            return False
        if s == "SUCCESS" and first_request_at is None:
            first_request_at = request_sent_at
        if force:
            # a FAILED requeue stays skip-for-report regardless of the response.
            # Record the attempt so an unchanged game isn't re-requested next run.
            failed_state[gid] = {**entry["failed_info"].get(gid, {}),
                                 "last_requested": request_sent_at.isoformat(),
                                 "chapter": g.get("chapter_title", ""),
                                 "collection": entry["title"]}
            print(f"  {entry['title']}: re-requested (force) {g['chapter_title']} → {s}", file=sys.stderr)
        elif s in SKIP_STATUSES:
            print(f"  {entry['title']}: skipping {g['chapter_title']} — {s}", file=sys.stderr)
            entry["state"][gid] = "skip"
        else:
            entry["state"][gid] = "wait"
            print(f"  {entry['title']}: {s} — {g['chapter_title']}", file=sys.stderr)
        return True

    if rate_limited:
        print("Rate-limited — deferring all new analysis requests to a later run.", file=sys.stderr)
    else:
        for entry in sorted(scanned, key=lambda e: (needs_quota(e), len(e["games"]))):
            if needs_quota(entry) == 0:
                continue
            print(f"--- requesting analysis: {entry['title']} ({needs_quota(entry)} to go) ---", file=sys.stderr)
            gmap = {g["game_id"]: g for g in entry["games"]}
            # Heal FAILED games (requeue) first, then the never-analyzed ones.
            ordered = [gid for gid, s in entry["state"].items() if s == "requeue"]
            ordered += [gid for gid, s in entry["state"].items() if s == "request"]
            for gid in ordered:
                if not issue(gid, entry["state"][gid] == "requeue", entry, gmap[gid]):
                    break
            if rate_limited:
                print("Quota exhausted — remaining collections deferred to a later run.", file=sys.stderr)
                break

    # --- Phase 3: assemble the snapshot. A collection is reportable once every game
    # is done/skip/requeue — nothing left to request and nothing waiting in the queue.
    results, pending, stale = [], [], []
    for entry in scanned:
        state, games = entry["state"], entry["games"]
        reportable_ids = {gid for gid, s in state.items()
                          if s in ("done", "skip", "requeue", "hold")}
        if len(reportable_ids) < len(games):
            pending.append({"title": entry["title"], "done": len(reportable_ids), "total": len(games)})
            print(f"  {entry['title']}: {len(reportable_ids)}/{len(games)} reportable — deferred", file=sys.stderr)
            continue

        game_entries = []
        for g in games:
            if state[g["game_id"]] != "done":
                continue  # skipped / requeued (FAILED) games carry no reportable data
            analysis = post(
                "analysis_service.AnalysisService/GetAnalysisResult",
                {"game_id": g["game_id"]},
            )
            history = post(
                "game_service.GameMetadataService/GetGameHistory",
                {"game_id": g["game_id"]},
            )
            # Free drift check: the history is already in hand.
            gid = g["game_id"]
            fingerprint = fingerprint_events(history.get("history") or {})
            known = analyzed_state.get(gid, {}).get("history")
            edited = known is not None and known != fingerprint
            if edited:
                stale.append({"game_id": gid, "collection": entry["title"],
                              "chapter": g.get("chapter_title", "")})
                print(f"  {entry['title']}: {g.get('chapter_title')} was EDITED after its "
                      f"analysis ran — its stats are stale. Woogles cannot re-analyze a "
                      f"completed game; re-upload it to refresh.", file=sys.stderr)
            else:
                # Only advance the baseline while the two agree, so an edit stays
                # reported every run until the game is actually re-uploaded.
                analyzed_state[gid] = {"history": fingerprint}
            game_entries.append({"meta": g, "analysis": analysis, "history": history,
                                 "analysis_stale": edited})

        results.append({"uuid": entry["uuid"], "title": entry["title"], "games": game_entries})
        print(f"  {entry['title']}: snapshot ready — {len(game_entries)} games", file=sys.stderr)

    output = {"collections": results, "pending": pending, "stale_analyses": stale}
    if one_off:
        subject = resolve_subject_identity(results)
        if subject:
            output["target"] = {"username": target_username, **subject}
            print(f"Resolved subject identity: {subject}", file=sys.stderr)
        else:
            print("Could not resolve subject identity from game data (no completed games yet)", file=sys.stderr)

    os.makedirs("data", exist_ok=True)
    with open("data/woogles-snapshot.json", "w") as f:
        json.dump(output, f)
    with open(RATE_LIMIT_MARKER_PATH, "w") as f:
        f.write(blocked_until.isoformat() if blocked_until else "")
    save_failed_state(failed_state)
    save_analyzed_state(analyzed_state)
    if stale:
        print(f"{len(stale)} game(s) edited since their analysis ran — stats stale until "
              f"re-uploaded:", file=sys.stderr)
        for e in stale:
            print(f"  {e['collection']}: {e['chapter']} ({e['game_id']})", file=sys.stderr)
    held = sum(1 for e in scanned for st in e["state"].values() if st == "hold")
    if held:
        print(f"Held {held} permanently-failing game(s) out of the analysis queue "
              f"(retry forced if the game changes, or after {FAILED_RETRY_DAYS} days).",
              file=sys.stderr)

    print(f"Done. {len(results)} collections ready, {len(pending)} deferred.", file=sys.stderr)


if __name__ == "__main__":
    main()
