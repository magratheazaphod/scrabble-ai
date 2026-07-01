#!/usr/bin/env python3
"""Generate the Woogles tournament report via Claude (with code execution) and email it.

Runs from GitHub Actions. SKILL.md is the single source of truth for stats-computation
logic and report format — this script never reimplements it; it hands SKILL.md plus the
data snapshot to Claude and lets the model run the Python it describes via the code
execution tool, so arithmetic is actually executed, not reasoned about.
"""
import json
import os
import smtplib
import sys
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from zoneinfo import ZoneInfo

import anthropic
import markdown

CENTRAL = ZoneInfo("America/Chicago")
SENT_MARKER_PATH = "data/last-report-sent.txt"

HTML_TEMPLATE = """\
<html>
<head>
<style>
  body {{ font-family: -apple-system, Helvetica, Arial, sans-serif; color: #1a1a1a; line-height: 1.5; max-width: 700px; }}
  h1 {{ font-size: 20px; border-bottom: 2px solid #333; padding-bottom: 6px; }}
  h2 {{ font-size: 16px; margin-top: 28px; color: #333; }}
  table {{ border-collapse: collapse; width: 100%; margin: 12px 0; font-size: 13px; }}
  th, td {{ border: 1px solid #ddd; padding: 6px 10px; text-align: left; }}
  th {{ background: #f4f4f4; }}
  tr:nth-child(even) {{ background: #fafafa; }}
  strong {{ color: #000; }}
  hr {{ border: none; border-top: 1px solid #ccc; margin: 32px 0; }}
</style>
</head>
<body>
{body}
</body>
</html>
"""

MODEL = "claude-opus-4-8"
DEFAULT_RECIPIENT = "magratheazaphod@gmail.com"
STATE_PATH = ".github/report-state.json"


def read_skill_md():
    with open(".claude/skills/woogles-tournament-analysis/SKILL.md") as f:
        return f.read()


def load_state():
    if not os.path.exists(STATE_PATH):
        return {}
    with open(STATE_PATH) as f:
        return json.load(f)


def save_state(state):
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=2, sort_keys=True)
        f.write("\n")


def already_sent_today(today_str):
    if not os.path.exists(SENT_MARKER_PATH):
        return False
    with open(SENT_MARKER_PATH) as f:
        return f.read().strip() == today_str


def mark_sent(today_str):
    with open(SENT_MARKER_PATH, "w") as f:
        f.write(today_str)


def upload_snapshot(client, snapshot):
    payload = json.dumps(snapshot).encode("utf-8")
    uploaded = client.beta.files.upload(
        file=("woogles-snapshot.json", payload, "application/json"),
        betas=["files-api-2025-04-14"],
    )
    return uploaded.id


def generate_collection_report(client, skill_md, file_id, subject=None):
    """Generate the markdown report for exactly one collection (the snapshot behind
    file_id has a single entry in "collections" and an empty "pending"). Regenerating
    one collection at a time — rather than batching all of them into one Claude call —
    is what lets each collection's report be cached and reused independently."""
    subject_clause = ""
    audience = "Jesse"
    if subject:
        nickname, real_name = subject["nickname"], subject.get("real_name") or subject["nickname"]
        subject_clause = f"""

IMPORTANT — this run is NOT about Jesse Day. Wherever SKILL.md's stats-computation logic identifies "Jesse" as the subject player (the is_jesse()/is_jesse_summary() matchers, report titles, column headers like "Jesse Score"/"Jesse Bingos"), instead identify the subject player by normalizing each player's nickname (GameHistory players[].nickname: lowercase it, strip everything except a-z) and checking whether it equals "{nickname}" — this handles per-game spelling variants like suffixes ("(MYS)") or inconsistent casing. Do NOT try to match on their Woogles login username, which does not appear anywhere in game data. Use their real name, "{real_name}" (from GameHistory's real_name field), in place of "Jesse Day" everywhere a report title, section header, or column header would otherwise reference Jesse — e.g. "Aggregate Stats (Jesse Day)" becomes "Aggregate Stats ({real_name})"."""
        audience = f"the recipient (not Jesse — this is a one-off report about Woogles player {real_name})"

    prompt = f"""Here is the current SKILL.md for Woogles tournament analysis (the authoritative spec for stats computation, aggregation, and report format):

<skill_md>
{skill_md}
</skill_md>
{subject_clause}

A data snapshot (woogles-snapshot.json) is attached to this message via the code execution container. It has the shape: {{"collections": [{{"uuid", "title", "games": [{{"meta", "analysis", "history"}}]}}], "pending": []}} — exactly one collection, already confirmed to have complete game data. Each game's "meta"/"analysis"/"history" are exactly the GetCollection game entry / GetAnalysisResult / GetGameHistory response bodies that SKILL.md's Workflow steps expect.

Using the code execution tool, write and run actual Python to load the snapshot and follow SKILL.md's Steps 5 through 8 exactly for this one collection: compute per-game stats and aggregates, generate per-game notes, and build the report using SKILL.md's current report template and aggregation rules. Do the arithmetic in code, not by reasoning.

Then write your final answer as plain text (not a tool call) containing ONLY that collection's markdown report, using SKILL.md's exact template — nothing else. Do not add a summary line, do not mention other collections, do not mention SKILL.md, "steps", your methodology, or the code execution process anywhere in this final answer — it's one section of an email to {audience}, not a description of how you produced it."""

    response = client.messages.create(
        model=MODEL,
        max_tokens=16000,
        thinking={"type": "adaptive"},
        output_config={"effort": "high"},
        tools=[{"type": "code_execution_20260521", "name": "code_execution"}],
        extra_headers={"anthropic-beta": "files-api-2025-04-14"},
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "container_upload", "file_id": file_id},
                ],
            }
        ],
    )

    # Claude narrates between tool calls (progress commentary); only the last
    # text block is the actual final answer we asked for.
    text_blocks = [b.text for b in response.content if b.type == "text"]
    return text_blocks[-1].strip() if text_blocks else ""


def build_pending_note(pending):
    if not pending:
        return ""
    lines = "\n".join(f"- {p['title']}: {p['done']}/{p['total']} games analyzed" for p in pending)
    return f"\n\n---\n\n**Still being analyzed:**\n{lines}"


def send_email(body, recipient, subject):
    sender = os.environ["GMAIL_ADDRESS"]
    password = os.environ["GMAIL_APP_PASSWORD"]

    html_body = markdown.markdown(body, extensions=["tables", "nl2br"])

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = recipient
    msg.attach(MIMEText(body, "plain", "utf-8"))
    msg.attach(MIMEText(HTML_TEMPLATE.format(body=html_body), "html", "utf-8"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(sender, password)
        server.send_message(msg)


def main():
    target_username = os.environ.get("TARGET_USERNAME", "").strip()
    recipient = os.environ.get("RECIPIENT_EMAIL", "").strip() or DEFAULT_RECIPIENT
    one_off = bool(target_username)

    today_str = datetime.now(CENTRAL).strftime("%Y-%m-%d")
    if not one_off and already_sent_today(today_str):
        print(f"Already sent today's report ({today_str}) — skipping.", file=sys.stderr)
        return

    with open("data/woogles-snapshot.json") as f:
        full_snapshot = json.load(f)

    collections = full_snapshot.get("collections", [])
    pending = full_snapshot.get("pending", [])

    subject_identity = None
    if one_off:
        # A one-off request always reports current state in full — the cached-report
        # reuse below only applies to the recurring daily run.
        state = {}
        subject_identity = full_snapshot.get("target")
        if collections and not subject_identity:
            print("Could not resolve subject identity from game data — aborting.", file=sys.stderr)
            return
    else:
        state = load_state()

    if not collections and not pending:
        print("Nothing to report — skipping entirely.", file=sys.stderr)
        return

    client = None
    skill_md = None
    report_sections = []
    updated_state = dict(state)

    for col in collections:
        prior = state.get(col["uuid"]) if not one_off else None
        if prior and prior.get("game_count") == len(col["games"]) and prior.get("report_md"):
            print(f"Reusing cached report for '{col['title']}' (unchanged)", file=sys.stderr)
            report_sections.append(prior["report_md"])
            continue

        if client is None:
            client = anthropic.Anthropic()
            skill_md = read_skill_md()

        print(f"Generating report for '{col['title']}' via Claude (code execution)...", file=sys.stderr)
        file_id = upload_snapshot(client, {"collections": [col], "pending": []})
        report_md = generate_collection_report(client, skill_md, file_id, subject=subject_identity)

        if not report_md or report_md.strip() == "NO_REPORT_READY":
            print(f"Failed to generate report for '{col['title']}' — skipping it this run", file=sys.stderr)
            continue

        report_sections.append(report_md)
        if not one_off:
            updated_state[col["uuid"]] = {
                "title": col["title"],
                "game_count": len(col["games"]),
                "reported_at": today_str,
                "report_md": report_md,
            }

    if not report_sections and not pending:
        print("No reportable collections this run — not sending an email.", file=sys.stderr)
        return

    summary = f"Woogles report: {len(report_sections)} collection{'s' if len(report_sections) != 1 else ''}"
    if pending:
        summary += f", {len(pending)} pending"
    summary += "."
    body = summary + "\n\n" + "\n\n---\n\n".join(report_sections) + build_pending_note(pending)

    now = datetime.now(CENTRAL)
    date_str = f"{now.strftime('%A %B')} {now.day} {now.year}"
    display_name = subject_identity["real_name"] if one_off and subject_identity else None
    email_subject = (
        f"{display_name}'s Woogles report - {date_str}"
        if one_off
        else f"Magrathean's Woogles daily report - {date_str}"
    )

    print(f"Sending email to {recipient}...", file=sys.stderr)
    send_email(body, recipient, email_subject)

    if one_off:
        print("Done.", file=sys.stderr)
        return

    mark_sent(today_str)
    save_state(updated_state)
    print("Done.", file=sys.stderr)


if __name__ == "__main__":
    main()
