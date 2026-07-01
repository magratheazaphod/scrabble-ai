#!/usr/bin/env python3
"""Harvest Woogles.io collection/game data and write a snapshot for downstream report generation.

Runs from GitHub Actions (unrestricted egress) since some cloud agent environments
can't reach woogles.io directly. Does NOT compute stats or build reports — that logic
lives in .claude/skills/woogles-tournament-analysis/SKILL.md and is interpreted by the
consumer of this snapshot, so report-format changes only ever need to happen in one place.
"""
import json
import os
import re
import sys

import requests

API_KEY = os.environ["WOOGLES_API_KEY"]
DEFAULT_USER_UUID = "7WyqZfyQuB6SwNa2XjuZUG"  # magrathean (Jesse's own account)
BASE = "https://woogles.io/api"
HDRS = {"Content-Type": "application/json", "X-Api-Key": API_KEY}

SKIP_STATUSES = {"NOT_A_PLAYER", "GAME_NOT_ENDED", "INVALID_VARIANT"}


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


def main():
    target_username = os.environ.get("TARGET_USERNAME", "").strip()
    user_uuid = resolve_user_uuid(target_username) if target_username else DEFAULT_USER_UUID
    if target_username:
        print(f"One-off snapshot for username: {target_username} ({user_uuid})", file=sys.stderr)

    collections = []
    offset = 0
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

    print(f"Collections found: {len(collections)}", file=sys.stderr)

    results, pending = [], []
    rate_limited = False

    for col in collections:
        col_uuid, col_title = col["uuid"], col["title"]
        print(f"--- {col_title} ---", file=sys.stderr)
        col_resp = post(
            "collections_service.CollectionsService/GetCollection",
            {"collection_uuid": col_uuid},
        )
        games = col_resp.get("collection", {}).get("games", [])
        if not games:
            continue

        skipped_ids = set()
        analyzed_ids = set()
        for g in games:
            status_resp = post(
                "analysis_service.AnalysisService/GetAnalysisStatus",
                {"game_id": g["game_id"]},
            )
            status = status_resp.get("status")
            if status == "COMPLETED":
                analyzed_ids.add(g["game_id"])
                continue
            if rate_limited:
                continue
            r = post(
                "analysis_service.AnalysisService/RequestAnalysis",
                {"game_id": g["game_id"], "force": False},
            )
            s = r.get("status", "UNKNOWN")
            if s == "RATE_LIMITED":
                print("  Rate limited — stopping new requests this run", file=sys.stderr)
                rate_limited = True
            elif s in SKIP_STATUSES:
                print(f"  Skipping {g['chapter_title']}: {s}", file=sys.stderr)
                skipped_ids.add(g["game_id"])
            else:
                print(f"  {s}: {g['chapter_title']}", file=sys.stderr)

        reportable_ids = analyzed_ids | skipped_ids
        if len(reportable_ids) < len(games):
            pending.append(
                {"title": col_title, "done": len(reportable_ids), "total": len(games)}
            )
            print(f"  {len(reportable_ids)}/{len(games)} done — deferred", file=sys.stderr)
            continue

        game_entries = []
        for g in games:
            if g["game_id"] in skipped_ids:
                continue
            analysis = post(
                "analysis_service.AnalysisService/GetAnalysisResult",
                {"game_id": g["game_id"]},
            )
            history = post(
                "game_service.GameMetadataService/GetGameHistory",
                {"game_id": g["game_id"]},
            )
            game_entries.append({"meta": g, "analysis": analysis, "history": history})

        results.append({"uuid": col_uuid, "title": col_title, "games": game_entries})
        print(f"  Snapshot ready: {len(game_entries)} games", file=sys.stderr)

    output = {"collections": results, "pending": pending}
    if target_username:
        subject = resolve_subject_identity(results)
        if subject:
            output["target"] = {"username": target_username, **subject}
            print(f"Resolved subject identity: {subject}", file=sys.stderr)
        else:
            print("Could not resolve subject identity from game data (no completed games yet)", file=sys.stderr)

    os.makedirs("data", exist_ok=True)
    with open("data/woogles-snapshot.json", "w") as f:
        json.dump(output, f)

    print(f"Done. {len(results)} collections ready, {len(pending)} deferred.", file=sys.stderr)


if __name__ == "__main__":
    main()
