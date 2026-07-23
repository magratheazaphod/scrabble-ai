#!/usr/bin/env python3
"""Generate the Woogles tournament report and email it.

Runs from GitHub Actions. Stats computation, aggregation, notes, and the report
template are all committed code in tournament_report.py (single source of truth,
shared with the interactive tournament-analysis skill — see its SKILL.md). The
only remaining LLM call writes the 2-4 sentence `## Summary` narrative from a
compact per-collection digest, and is cached per collection keyed on a content
hash of that digest so unchanged collections cost nothing to re-render.
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

import tournament_report as tr

CENTRAL = ZoneInfo("America/Chicago")
SENT_MARKER_PATH = "data/last-report-sent.txt"
DRY_RUN = os.environ.get("DRY_RUN", "").strip() == "1"

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

MODEL = "claude-sonnet-5"
DEFAULT_RECIPIENT = "magratheazaphod@gmail.com"
STATE_PATH = ".github/report-state.json"

EXAMPLE_SUMMARY = (
    "A best-of-three final against Nigel Richards that slipped away 1-2, with a lopsided "
    "−304 aggregate spread. Jesse actually played clean, disciplined Scrabble throughout — "
    "a 1.73 average mistakes score, 75% bingo find rate, and zero phonies — but a 685-point "
    "Round 1 onslaught dug a hole too deep to climb out of. The lone bright spot was a "
    "commanding Round 2 (563-417 on four bingos), while Nigel's near-flawless play (0.40 "
    "mistakes, 1.6% win% lost across the two annotated games) ultimately decided the match."
)


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


def compute_collection(col, subject=None):
    """Run the deterministic module end-to-end for one collection.

    Returns (stats, agg, notes, digest, digest_hash).
    """
    stats = [tr.compute_game(r, subject=subject) for r in col["games"]]
    stats.sort(key=lambda g: g["round"])
    tr.check_phony_words(stats)
    agg = tr.aggregate(stats)
    notes = tr.game_notes(stats)
    digest = tr.build_digest(stats, agg, notes, col["title"])
    return stats, agg, notes, digest, tr.digest_hash(digest)


def generate_summary(client, digest, subject=None):
    """The only remaining LLM call — a small no-tools call over a compact digest.
    Returns None on API failure (caller renders without a Summary section)."""
    audience = "Jesse Day, the player" if subject is None else f"the recipient (about Woogles player {subject.get('real_name') or subject['nickname']}, not Jesse Day)"
    subject_note = (
        ""
        if subject is None
        else f"\n\nThis is a one-off report about {subject.get('real_name') or subject['nickname']}, not Jesse Day. Address the summary to {audience}."
    )
    prompt = f"""Here is a compact stats digest for one Woogles tournament collection:

<digest>
{digest}
</digest>
{subject_note}

Write a 2-4 sentence narrative summary of this tournament for the report's "## Summary" section. Be factual and specific: name opponents, cite the standout numbers from the digest. No headers, no meta-commentary, no mention of the digest or your methodology — this is prose for {audience}.

Example of the style and length to match (from a different tournament, do not reuse its content):
{EXAMPLE_SUMMARY}"""

    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=500,
            messages=[{"role": "user", "content": prompt}],
        )
        text_blocks = [b.text for b in response.content if b.type == "text"]
        return text_blocks[-1].strip() if text_blocks else None
    except anthropic.APIError as e:
        print(f"Summary generation failed: {e}", file=sys.stderr)
        return None


def migrate_entry(uuid, entry, golden_col_lookup=None):
    """Zero-cost migration: an old {"report_md", "game_count"} entry becomes
    {"digest_hash", "summary_md"} using the collection's CURRENT data, no LLM call.
    Returns the migrated entry, or the original if it can't be migrated yet
    (collection not present in this run's snapshot)."""
    if "report_md" not in entry:
        return entry
    col = golden_col_lookup.get(uuid) if golden_col_lookup else None
    if not col:
        return entry
    _, _, _, digest, dhash = compute_collection(col)
    old_report_md = entry["report_md"]
    idx = old_report_md.find("## Summary")
    old_summary = old_report_md[idx + len("## Summary"):].strip() if idx != -1 else None

    return {
        "title": entry.get("title", col["title"]),
        "game_count": len(col["games"]),
        "digest_hash": dhash,
        "summary_md": old_summary,
        "reported_at": entry.get("reported_at"),
    }


def build_pending_note(pending):
    if not pending:
        return ""
    lines = "\n".join(f"- {p['title']}: {p['done']}/{p['total']} games analyzed" for p in pending)
    return f"\n\n---\n\n**Still being analyzed:**\n{lines}"


def send_email(body, recipient, subject):
    if DRY_RUN:
        print("=== DRY_RUN: email body ===", file=sys.stderr)
        print(body)
        return
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
    target_collection_uuid = os.environ.get("TARGET_COLLECTION_UUID", "").strip()
    recipient = os.environ.get("RECIPIENT_EMAIL", "").strip() or DEFAULT_RECIPIENT
    one_off = bool(target_username or target_collection_uuid)

    today_str = datetime.now(CENTRAL).strftime("%Y-%m-%d")
    if not one_off and not DRY_RUN and already_sent_today(today_str):
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

    col_by_uuid = {c["uuid"]: c for c in collections}

    # Zero-cost migration: old {"report_md", "game_count"} entries -> {"digest_hash", "summary_md"}.
    if not one_off:
        for uuid, entry in list(state.items()):
            state[uuid] = migrate_entry(uuid, entry, col_by_uuid)

    client = None
    report_sections = []
    updated_state = dict(state)

    for col in collections:
        prior = state.get(col["uuid"]) if not one_off else None

        stats, agg, notes, digest, dhash = compute_collection(col, subject=subject_identity)
        subject_display = "Jesse Day" if subject_identity is None else (
            subject_identity.get("real_name") or subject_identity["nickname"]
        )

        summary_md = None
        if prior and prior.get("digest_hash") == dhash and prior.get("summary_md"):
            print(f"Reusing cached summary for '{col['title']}' (digest unchanged)", file=sys.stderr)
            summary_md = prior["summary_md"]
        else:
            if client is None:
                client = anthropic.Anthropic()
            print(f"Generating summary for '{col['title']}' via Claude...", file=sys.stderr)
            summary_md = generate_summary(client, digest, subject=subject_identity)
            if summary_md is None:
                print(f"No summary for '{col['title']}' this run — rendering without one.", file=sys.stderr)

        report_md = tr.render_report(stats, agg, notes, col["title"], summary_md=summary_md, subject_display=subject_display)
        report_sections.append(report_md)

        if not one_off:
            updated_state[col["uuid"]] = {
                "title": col["title"],
                "game_count": len(col["games"]),
                "digest_hash": dhash,
                "summary_md": summary_md,
                "reported_at": today_str,
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

    if not DRY_RUN:
        mark_sent(today_str)
        save_state(updated_state)
    print("Done.", file=sys.stderr)


if __name__ == "__main__":
    main()
