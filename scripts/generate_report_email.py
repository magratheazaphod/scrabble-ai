#!/usr/bin/env python3
"""Generate the Woogles tournament report via Claude (with code execution) and email it.

Runs from GitHub Actions. SKILL.md is the single source of truth for stats-computation
logic and report format — this script never reimplements it; it hands SKILL.md plus the
data snapshot to Claude and lets the model run the Python it describes via the code
execution tool, so arithmetic is actually executed, not reasoned about.
"""
import os
import smtplib
import sys
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import anthropic
import markdown

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
RECIPIENT = "magratheazaphod@gmail.com"


def read_skill_md():
    with open(".claude/skills/woogles-tournament-analysis/SKILL.md") as f:
        return f.read()


def upload_snapshot(client):
    with open("data/woogles-snapshot.json", "rb") as f:
        uploaded = client.beta.files.upload(
            file=("woogles-snapshot.json", f, "application/json"),
            betas=["files-api-2025-04-14"],
        )
    return uploaded.id


def generate_report(client, skill_md, file_id):
    prompt = f"""Here is the current SKILL.md for Woogles tournament analysis (the authoritative spec for stats computation, aggregation, and report format):

<skill_md>
{skill_md}
</skill_md>

A data snapshot (woogles-snapshot.json) is attached to this message via the code execution container. It has the shape: {{"collections": [{{"uuid", "title", "games": [{{"meta", "analysis", "history"}}]}}], "pending": [{{"title", "done", "total"}}]}}. Each game's "meta"/"analysis"/"history" are exactly the GetCollection game entry / GetAnalysisResult / GetGameHistory response bodies that SKILL.md's Workflow steps expect.

Using the code execution tool, write and run actual Python to:
1. Load the snapshot file from the container filesystem.
2. For each collection in `collections`, follow SKILL.md's Steps 5 through 8 exactly: compute per-game stats and aggregates, generate per-game notes, and build the report using SKILL.md's current report template and aggregation rules. Do the arithmetic in code, not by reasoning.
3. Collections in `pending` have no game data — list them as deferred with their done/total counts.

Then write your final answer as plain text (not a tool call) containing ONLY the report content itself:
- A one-line summary, e.g. "Woogles report: 2 collections analyzed, 1 pending."
- All completed reports concatenated with `---` separators, using SKILL.md's exact markdown template.
- A closing note listing any deferred collections and their pending game counts.

Do not mention SKILL.md, "steps", your methodology, or the code execution process anywhere in this final answer — it's an email to Jesse, not a description of how you produced it. Start directly with the one-line summary.

If there are zero collections with game data (everything pending, nothing ready), your final answer should be exactly: "NO_REPORT_READY" and nothing else."""

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

    text_blocks = [b.text for b in response.content if b.type == "text"]
    return "\n".join(text_blocks).strip()


def send_email(body):
    sender = os.environ["GMAIL_ADDRESS"]
    password = os.environ["GMAIL_APP_PASSWORD"]

    now = datetime.now()
    date_str = f"{now.strftime('%A %B')} {now.day} {now.year}"
    html_body = markdown.markdown(body, extensions=["tables", "nl2br"])

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"Woogles Daily Report: {date_str}"
    msg["From"] = sender
    msg["To"] = RECIPIENT
    msg.attach(MIMEText(body, "plain", "utf-8"))
    msg.attach(MIMEText(HTML_TEMPLATE.format(body=html_body), "html", "utf-8"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(sender, password)
        server.send_message(msg)


def main():
    client = anthropic.Anthropic()
    skill_md = read_skill_md()

    print("Uploading snapshot...", file=sys.stderr)
    file_id = upload_snapshot(client)

    print("Generating report via Claude (code execution)...", file=sys.stderr)
    report = generate_report(client, skill_md, file_id)

    if report.strip() == "NO_REPORT_READY":
        print("No collections ready this run — not sending an email.", file=sys.stderr)
        return

    print("Sending email...", file=sys.stderr)
    send_email(report)
    print("Done.", file=sys.stderr)


if __name__ == "__main__":
    main()
