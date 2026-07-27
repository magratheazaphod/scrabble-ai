---
name: lexicon-lookup
description: Validate words (and look up definitions) against official word-game lexica — CSW (Collins Scrabble Words), NWL/TWL (North American), and foreign-language sets — using the local word-game-lexica corpus. Use whenever you need to check whether a word is playable/valid in a given lexicon, confirm a Scrabble/crossword word, resolve an ambiguous OCR read by dictionary validity, or look up a word's definition and part of speech. Default lexicon is CSW24. Do NOT rely on your own intuition of validity — always check the list.
---

# Lexicon lookup / word validation

Never trust your own sense of whether an obscure word is valid. Scrabble lexica
contain thousands of words that "look wrong" (`SOREE`, `HAOMA`, `MUCIGENS`, `XU`,
`QUATE` are all valid CSW) and exclude things that "look right". **Always check
the actual word list.** Standing instruction from Jesse.

## Quick check

```bash
python3 .claude/skills/lexicon-lookup/scripts/check_words.py WORD1 WORD2 ...
python3 .claude/skills/lexicon-lookup/scripts/check_words.py --lexicon CSW21 --defs QUATE SOREE
```

- Prints `ok` / `INVALID` per word; exit code 1 if any word is invalid.
- `--lexicon NAME` picks the edition (default CSW24).
- `--defs` also prints the definition and part of speech for valid words.
- Case-insensitive. `?`/blanks are not letters - pass concrete letters.

## Which lexicon

Jesse plays CSW, always, at the edition current at the relevant time - see
`CLAUDE.md`. **CSW24 is today's default.** Historical: CSW15 / CSW19 / CSW21.
North American: NWL18 / NWL20 / NWL23, TWL06 / TWL98.

Foreign sets are present too: FRA20/FRA24 (French), OSPS40-50 (Polish), Deutsch
(German), FISE09/FISE2 (Spanish), NSF21/22 (Norwegian), ECWL, and others. Run
`ls /Users/Siwen/projects/word-game-lexica/*.txt` for the full set.

## The corpus

Local git clone of <https://github.com/domino14/word-game-lexica>:

```
/Users/Siwen/projects/word-game-lexica/*.txt
```

One file per lexicon. **Format is `WORD<TAB>definition [part-of-speech]`**, one
entry per line, word already uppercased - so parse the FIRST tab-separated field.
Uppercasing the whole line and testing membership silently fails, because the
definition text is attached:

```python
def load_lexicon(name='CSW24'):
    words = {}
    for line in open(f'/Users/Siwen/projects/word-game-lexica/{name}.txt', encoding='utf-8'):
        parts = line.rstrip('\n').split('\t', 1)
        w = parts[0].strip().upper()
        if w:
            words[w] = parts[1].strip() if len(parts) > 1 else ''
    return words
```

## Common uses

- **Resolving ambiguous OCR / handwriting:** when a photo or scoresheet read is
  uncertain (`SORE` vs `SCREE`, `RR` vs `RE`), enumerate the candidate readings
  and keep only the lexicon-valid one that also fits the board and score. Pair
  this with tile point-values on a board photo (C=3 vs O=1, Q=10 vs G=2) - the two
  together disambiguate almost any tile.
- **Auditing a reconstructed board:** extract every maximal horizontal and
  vertical run (length ≥ 2) from the final board and validate them all. A single
  unexpected invalid word - one that wasn't a deliberately-played, challenged-off
  phony - means a misread somewhere. A genuine phony that STOOD is fine, but it
  should at least look "wordy"; a nonsense run like `RR` is a red flag that the
  read, not the play, is wrong.
