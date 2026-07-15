#!/usr/bin/env python3
"""Validate words against a word-game lexicon (default CSW24).

Corpus: https://github.com/domino14/word-game-lexica cloned at
/Users/Siwen/projects/word-game-lexica/<LEX>.txt, format `WORD<TAB>definition`.

Usage:
    python3 check_words.py QUATE SOREE MUCIGENS
    python3 check_words.py --lexicon CSW21 --defs XU HAOMA
Exit code 1 if any word is invalid (useful in pipelines).
"""
import argparse
import os
import sys

LEX_DIR = os.environ.get(
    "WORD_GAME_LEXICA_DIR", "/Users/Siwen/projects/word-game-lexica"
)


def load_lexicon(name):
    path = os.path.join(LEX_DIR, f"{name}.txt")
    if not os.path.exists(path):
        avail = sorted(
            f[:-4] for f in os.listdir(LEX_DIR) if f.endswith(".txt")
        )
        sys.exit(f"lexicon '{name}' not found at {path}\navailable: {', '.join(avail)}")
    words = {}
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            parts = line.rstrip("\n").split("\t", 1)
            w = parts[0].strip().upper()
            if w:
                words[w] = parts[1].strip() if len(parts) > 1 else ""
    return words


def main():
    ap = argparse.ArgumentParser(description="Validate words against a lexicon.")
    ap.add_argument("words", nargs="+", help="words to check")
    ap.add_argument("--lexicon", "-l", default="CSW24", help="edition (default CSW24)")
    ap.add_argument("--defs", action="store_true", help="print definitions for valid words")
    args = ap.parse_args()

    lex = load_lexicon(args.lexicon)
    all_ok = True
    for raw in args.words:
        w = raw.strip().upper()
        if w in lex:
            tail = f"  {lex[w]}" if args.defs and lex[w] else ""
            print(f"{w:16} ok{tail}")
        else:
            print(f"{w:16} INVALID  (not in {args.lexicon})")
            all_ok = False
    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
