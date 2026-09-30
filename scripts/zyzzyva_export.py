#!/usr/bin/env python3
"""Export what mistake_types.py needs from Zyzzyva into one small SQLite file.

The word-knowledge check reads two things from Jesse's Collins Zyzzyva install:
each word's playability rank within its length, and his anagram-quiz record.
The raw lexicon databases are 100MB+ apiece (over GitHub's file limit) and hold
the full Collins list with definitions, which is HarperCollins' copyright and
must never reach this public repo. So this writes a slim derived file holding
only a playability percentile per word and the merged quiz counts:

    data/zyzzyva-knowledge.sqlite            (gitignored; read by mistake_types)

and, with --publish, commits it to the private repo the report workflow checks
out (magratheazaphod/zyzzyva-data), so CI classifies word knowledge exactly as a
local run does. Re-run with --publish after a study session to refresh the quiz
record there.

    python3 scripts/zyzzyva_export.py
    python3 scripts/zyzzyva_export.py --publish
"""
import argparse
import os
import sqlite3
import subprocess
import sys
import tempfile

ZYZZYVA_HOME = os.path.expanduser(os.environ.get("ZYZZYVA_HOME", "~/.collinszyzzyva"))
DEFAULT_OUT = "data/zyzzyva-knowledge.sqlite"
EDITIONS = ("CSW15", "CSW19", "CSW21", "CSW24")  # the lexicons Jesse's games use
PRIVATE_REPO = "magratheazaphod/zyzzyva-data"
PUBLISHED_NAME = "zyzzyva-knowledge.sqlite"


def export(out_path, home=ZYZZYVA_HOME):
    tmp = out_path + ".tmp"
    if os.path.exists(tmp):
        os.remove(tmp)
    db = sqlite3.connect(tmp)
    db.executescript("""
        CREATE TABLE playability (lexicon TEXT, word TEXT, percentile REAL,
                                  PRIMARY KEY (lexicon, word)) WITHOUT ROWID;
        CREATE TABLE quiz (alphagram TEXT PRIMARY KEY, correct INTEGER,
                           incorrect INTEGER) WITHOUT ROWID;
    """)
    for lex in EDITIONS:
        path = os.path.join(home, "lexicons", f"{lex}.db")
        if not os.path.exists(path):
            sys.exit(f"missing {path} - is Collins Zyzzyva installed with {lex}?")
        src = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        sizes = dict(src.execute(
            "SELECT length, MAX(playability_order) FROM words GROUP BY length"))
        # Unplayable-rank rows (order 0/NULL) are left out: absent reads as
        # "can't tell", which is what they are.
        rows = [(lex, w, order / sizes[n]) for w, n, order in src.execute(
            "SELECT word, length, playability_order FROM words WHERE playability_order > 0")]
        db.executemany("INSERT INTO playability VALUES (?, ?, ?)", rows)
        src.close()
        print(f"{lex}: {len(rows)} words", file=sys.stderr)

    # Best record per alphagram across every CSW edition's cardbox.
    best = {}
    root = os.path.join(home, "quiz", "data")
    for edition in sorted(os.listdir(root)) if os.path.isdir(root) else []:
        path = os.path.join(root, edition, "Anagrams.db")
        if not edition.startswith("CSW") or not os.path.exists(path):
            continue
        src = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        for q, c, i in src.execute(
                "SELECT question, correct, incorrect FROM questions WHERE correct > 0"):
            if (c or 0) > best.get(q, (0, 0))[0]:
                best[q] = (c or 0, i or 0)
        src.close()
    db.executemany("INSERT INTO quiz VALUES (?, ?, ?)",
                   [(q, c, i) for q, (c, i) in best.items()])
    print(f"quiz: {len(best)} alphagrams", file=sys.stderr)
    db.commit()
    db.execute("VACUUM")
    db.close()
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    os.replace(tmp, out_path)
    return out_path


def publish(path):
    """Commit the export to the private repo, creating it on first use."""
    def run(*cmd, **kw):
        return subprocess.run(cmd, check=True, text=True, capture_output=True, **kw)

    exists = subprocess.run(["gh", "repo", "view", PRIVATE_REPO],
                            capture_output=True).returncode == 0
    if not exists:
        run("gh", "repo", "create", PRIVATE_REPO, "--private",
            "--description", "Derived Zyzzyva data for scrabble-ai's report job. "
            "Contains Collins-derived data: keep private.")
    with tempfile.TemporaryDirectory() as tmp:
        repo = os.path.join(tmp, "repo")
        if exists:
            run("gh", "repo", "clone", PRIVATE_REPO, repo, "--", "--depth=1")
        else:
            os.makedirs(repo)
            run("git", "init", "-q", "-b", "main", cwd=repo)
            run("git", "remote", "add", "origin",
                f"https://github.com/{PRIVATE_REPO}.git", cwd=repo)
        subprocess.run(["cp", path, os.path.join(repo, PUBLISHED_NAME)], check=True)
        run("git", "add", PUBLISHED_NAME, cwd=repo)
        if not run("git", "status", "--porcelain", cwd=repo).stdout.strip():
            print("private repo already up to date", file=sys.stderr)
            return
        run("git", "commit", "-q", "-m", "Refresh Zyzzyva knowledge export", cwd=repo)
        run("git", "push", "-q", "-u", "origin", "HEAD:main", cwd=repo)
    print(f"published to {PRIVATE_REPO}", file=sys.stderr)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--publish", action="store_true",
                    help=f"also commit the export to the private {PRIVATE_REPO}")
    args = ap.parse_args()
    path = export(args.out)
    print(f"wrote {path} ({os.path.getsize(path) / 1e6:.1f} MB)", file=sys.stderr)
    if args.publish:
        publish(path)


if __name__ == "__main__":
    main()
