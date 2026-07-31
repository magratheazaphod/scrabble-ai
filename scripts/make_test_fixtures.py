#!/usr/bin/env python3
"""Build the committed, anonymized test corpus from data/golden-snapshot.json.

`data/` is gitignored and local-only, so the test suite cannot depend on it. This
turns a handful of collections out of that snapshot into `tests/fixtures/*.json.gz`
— small enough to commit, and stripped of every real identity except Jesse's own.

    python3 scripts/make_test_fixtures.py          # rebuild the corpus
    python3 scripts/make_test_fixtures.py --check  # verify it is reproducible

## What gets removed, and why

None of it is secret one fact at a time: these games are public on woogles.io and
committed reports/ already name opponents. What a fixture would add is the
*aggregation* — `real_name` ↔ `nickname` ↔ `user_id` for every opponent in one
machine-readable file, which is a reconstruction of the real-name ↔ handle
registry CLAUDE.md keeps private and uncommitted. So:

- every non-subject player is replaced by a stable invented identity
- `user_id` never survives: a real account key becomes a synthetic one, and a
  synthesized `internal-<nick>` stays synthesized under the new nickname
- game ids and `uid`s are replaced by ids derived from a hash of the original —
  deterministic across rebuilds, but not resolvable back to a woogles.io game
- `original_gcg` is dropped outright (no report script reads it, and it carries
  `#player` name lines)
- any leftover occurrence of a real name is caught by `assert_scrubbed` before a
  byte is written

## What is deliberately preserved

- **Jesse's own identity.** `is_jesse` matches the literal strings "magrathean",
  "Jesse Day", "JD", "JesseD"; pseudonymizing the subject would make the
  production identity path untestable. Those aliases are already public in
  CLAUDE.md.
- **The shape of each identity.** A nickname that was the player's first name
  stays a first name, initials stay initials, a bare handle stays a bare handle,
  and a player with no `real_name` still has none. `is_jesse`, `get_opp_name`,
  `woogles_username` and `summary_for_index` all branch on that variation, so
  flattening it would quietly gut the identity tests.
- **Everything with no identity in it** — racks, moves, scores, every analysis
  number. That is the material under test and it is copied verbatim.
"""
import argparse
import gzip
import hashlib
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(__file__))
from tournament_report import is_jesse

SNAPSHOT_PATH = "data/golden-snapshot.json"
FIXTURE_DIR = "tests/fixtures"

# Chosen for branch coverage per unit of committed size, not for interest:
#   2018 WSC Finals      — every opponent fully annotated; missed bingos
#   Austin One-Day       — NO opponent fully annotated (the partial-annotation
#                          branch, where opponent columns must suppress)
#   2019 WESPAC Final    — fully annotated, with a phony
#   King's Cup 2019 Finals — annotated and partial mixed inside one collection
# Draws and VOID challenge games appear nowhere in the snapshot, so those two
# branches are covered by synthetic cases in the test module instead.
# The third element is the title the fixture carries, replacing the real event
# name. Partly because an event name can collide with a person ("Austin One-Day"
# trips the identity audit on a real opponent's first name), and partly because a
# title saying what the fixture COVERS is more use in test output than one saying
# which tournament it came from.
FIXTURE_COLLECTIONS = [
    ("wsc-2018-finals", "2018 World Scrabble Championship Finals",
     "Fixture: four-game final, opponent fully annotated"),
    ("austin-one-day", "Austin One-Day Aug '23",
     "Fixture: six-game event, no opponent annotated"),
    ("wespac-2019-final", "2019 WESPAC Final",
     "Fixture: seven-game final with a phony"),
    ("kings-cup-2019-finals", "King's Cup 2019 Finals",
     "Fixture: three-game final, annotation mixed"),
]

# Invented people. Deliberately unlike any real player's name, and fixed in order
# so a rebuild reassigns nobody.
PSEUDO_NAMES = [
    ("Robin", "Alder"), ("Casey", "Brill"), ("Devon", "Cray"), ("Emery", "Dunlop"),
    ("Frankie", "Estes"), ("Glen", "Fairlie"), ("Harper", "Gost"), ("Indigo", "Hale"),
    ("Jules", "Ivers"), ("Kit", "Jarrow"), ("Lane", "Kessel"), ("Marlow", "Lund"),
    ("Noor", "Marsh"), ("Oakley", "Nevin"), ("Peyton", "Orme"), ("Quinn", "Pell"),
    ("Reese", "Quill"), ("Sage", "Roth"), ("Tatum", "Speer"), ("Umber", "Trask"),
    ("Vale", "Umbrey"), ("Wren", "Voss"), ("Xen", "Wilde"), ("Yarrow", "Xanthe"),
    ("Zev", "Yarrow"), ("Ash", "Zell"),
]

ID_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"


def stable_index(key, n):
    """A deterministic slot in [0, n) for `key` — same identity, same pseudonym,
    regardless of which collections are being rebuilt or in what order."""
    digest = hashlib.sha256(key.encode()).digest()
    return int.from_bytes(digest[:8], "big") % n


def synth_id(original, length=22):
    """A woogles-shaped id derived from the original.

    Stable across rebuilds (so the corpus doesn't churn) and one-way (so it can't
    be turned back into a real game URL)."""
    digest = hashlib.sha256(f"fixture:{original}".encode()).digest()
    value = int.from_bytes(digest, "big")
    out = []
    for _ in range(length):
        value, rem = divmod(value, len(ID_ALPHABET))
        out.append(ID_ALPHABET[rem])
    return "".join(out)


def nickname_shape(nick, real):
    """How a nickname relates to the real name, so the pseudonym can mirror it.

    Returns one of "first", "last", "initials", "full", "handle" — the variation
    `is_jesse`/`get_opp_name` branch on."""
    if not nick:
        return "handle"
    n = re.sub(r"[^a-z]", "", nick.lower())
    parts = [p for p in re.split(r"\s+", (real or "").strip()) if p]
    first = re.sub(r"[^a-z]", "", parts[0].lower()) if parts else ""
    last = re.sub(r"[^a-z]", "", parts[-1].lower()) if len(parts) > 1 else ""
    if first and n == first:
        return "first"
    if last and n == last:
        return "last"
    if first and last and n == first + last:
        return "full"
    if first and last and n == first[0] + last[0]:
        return "initials"
    return "handle"


def make_pseudonym(player, roster):
    """A replacement identity for one player, mirroring the original's shape.

    `roster` is shared across the entire build, so one real person keeps one
    pseudonym everywhere they appear (both Jesse's finals are against the same
    opponent, and the fixtures should show that), and two real people never
    collapse into one — which would quietly mask an identity bug."""
    key = _norm(player.get("user_id") or player.get("nickname")
                or player.get("real_name") or "?")
    if key not in roster:
        slot = stable_index(key, len(PSEUDO_NAMES))
        while slot in set(roster.values()):  # linear probe on collision
            slot = (slot + 1) % len(PSEUDO_NAMES)
        roster[key] = slot
    first, last = PSEUDO_NAMES[roster[key]]
    slot = roster[key]

    real = player.get("real_name")
    shape = nickname_shape(player.get("nickname"), real)
    nick = {
        "first": first,
        "last": last,
        "full": f"{first}{last}",
        "initials": f"{first[0]}{last[0]}",
    }.get(shape, f"{first.lower()}{last[0].lower()}{slot:02d}")

    uid = player.get("user_id") or ""
    if uid.startswith("internal-"):
        # An annotator upload synthesises this from the nickname; keep it derived
        # so the "not a real account" branch in woogles_username still fires.
        new_uid = f"internal-{nick}"
    elif uid:
        new_uid = synth_id(uid, length=len(uid))
    else:
        new_uid = uid

    return {
        "nickname": nick,
        # A player with no real_name must still have none — the report falls back
        # to the nickname there, and that fallback needs to stay exercised.
        "real_name": f"{first} {last}" if real else real,
        "user_id": new_uid,
    }


def identity_tokens(player):
    """Every spelling of this player that must not survive, for the final audit.

    Deliberately generous, and matched as substrings of a normalized string rather
    than on word boundaries: the analysis stores its own concatenated spelling of a
    name (`nickname` "Anuj" in the history, `player_name` "AnujShetty" in the
    analysis), and a `\\b`-anchored scrub walks straight past it. That near-miss is
    why nothing here relies on scrubbing text any more."""
    tokens = set()
    for field in ("nickname", "real_name", "user_id"):
        value = (player.get(field) or "").strip()
        if not value:
            continue
        tokens.add(value)
        parts = [p for p in re.split(r"[\s_\-]+", value) if len(p) > 2]
        tokens.update(parts)
        if len(parts) > 1:
            tokens.add("".join(parts))
    return {t for t in tokens if len(_norm(t)) > 2}


def _norm(text):
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def rebuild_chapter_title(title, players, pseudo_by_index):
    """Regenerate "<round prefix> - JD vs <opponent>" from structured data.

    Rewriting beats scrubbing: an annotator's title is free text that can name
    anyone (a third player, an event, a city), and the round prefix is the only
    part any code reads (`compute_game` parses Rd/Round/Seed out of it)."""
    prefix = ""
    match = re.match(r"^\s*((?:Final\s+)?(?:Rd\.?|Round|Seed|Game)\s*\d+|Final\s*\d*)",
                     title or "", re.I)
    if match:
        prefix = match.group(1).strip()
    names = []
    for i, player in enumerate(players):
        pseudo = pseudo_by_index.get(i)
        names.append(pseudo["nickname"] if pseudo else (player.get("nickname") or "JD"))
    body = " vs ".join(names)
    return f"{prefix} - {body}" if prefix else body


def anonymize_game(entry, roster):
    """One {meta, analysis, history} entry, with every non-subject identity replaced.

    Identity-bearing fields are **regenerated from the player list**, never
    search-and-replaced. Free text is dropped rather than cleaned. The returned
    token set is what `assert_scrubbed` then audits the result against."""
    entry = json.loads(json.dumps(entry))  # never mutate the caller's snapshot
    history = entry["history"]["history"]

    forbidden = set()
    pseudo_by_index = {}
    for i, player in enumerate(history["players"]):
        if is_jesse(player):
            continue  # the subject stays himself — see the module docstring
        forbidden |= identity_tokens(player)
        pseudo_by_index[i] = make_pseudonym(player, roster)

    # `original_gcg` is read by no report script and is a second copy of every name
    # in the game. `title`, `description` and per-event `note`s are annotator free
    # text that no report script reads either — all four go rather than get cleaned.
    history.pop("original_gcg", None)
    history["title"] = ""
    history["description"] = ""
    history["uid"] = synth_id(history.get("uid") or entry["meta"]["game_id"])

    meta = entry["meta"]
    meta["chapter_title"] = rebuild_chapter_title(
        meta.get("chapter_title"), history["players"], pseudo_by_index)
    meta["game_id"] = synth_id(meta["game_id"])

    # The analysis keys its summaries to `player_name` alone, so turns and
    # summaries must keep agreeing after the rename — rebuild both off
    # `player_index`, which is what summary_for_index pairs them on anyway.
    result = entry["analysis"].get("result") or {}
    names_by_index = {}
    for turn in result.get("turns") or []:
        idx = turn["player_index"]
        pseudo = pseudo_by_index.get(idx)
        if pseudo:
            names_by_index.setdefault(idx, set()).add(turn.get("player_name"))
            turn["player_name"] = pseudo["nickname"]
    for summary in result.get("player_summaries") or []:
        for idx, originals in names_by_index.items():
            if summary.get("player_name") in originals:
                summary["player_name"] = pseudo_by_index[idx]["nickname"]
                break

    for event in history.get("events") or []:
        event.pop("note", None)
        pseudo = pseudo_by_index.get(event.get("player_index"))
        if pseudo:
            event["nickname"] = pseudo["nickname"]

    for i, pseudo in pseudo_by_index.items():
        history["players"][i].update(pseudo)

    # Names the analysis spelled its own way are identities too, and are exactly
    # what a player-list-only audit would miss.
    for idx, originals in names_by_index.items():
        for original in originals:
            forbidden |= identity_tokens({"nickname": original})

    return entry, forbidden


# Keys whose values are board/rack content, not prose: a played word is allowed to
# spell a real player's name (BING, TIM, NICK are all playable), and auditing them
# would be a permanent false positive on the tiles themselves.
CONTENT_KEYS = {
    "rack", "played_tiles", "exchanged", "words_formed", "move_description",
    "position", "leave", "optimal_move", "played_move", "starting_cgp",
    "letter_distribution", "board_layout", "last_known_racks", "lexicon", "variant",
    "direction", "type", "phase", "mistake_size", "play_state", "id_auth",
}
# Structural words this script itself emits, or that carry no identity.
TOKEN_STOPLIST = {"internal", "final", "round", "seed", "game", "rd", "vs", "player"}


def field_tokens(value):
    """Lowercase word-tokens of a string, splitting camelCase as well as
    punctuation — "AnujShetty" must yield {"anuj", "shetty"}, which is exactly the
    spelling that slipped through the first version of this audit."""
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", value or "")
    return {_norm(part) for part in re.split(r"[^A-Za-z0-9]+", spaced) if _norm(part)}


def audited_strings(node, key=None):
    """Every string in the fixture that could carry a name, with content skipped."""
    if isinstance(node, dict):
        for k, v in node.items():
            if k not in CONTENT_KEYS:
                yield from audited_strings(v, k)
    elif isinstance(node, list):
        for item in node:
            yield from audited_strings(item, key)
    elif isinstance(node, str):
        yield node


def assert_scrubbed(blob, forbidden):
    """Last line of defence: fail loudly rather than commit a real name.

    Two decisions matter here. It runs over the **serialized** fixture, so it sees
    fields this script doesn't know about — which is the entire point of having it.
    And it matches **normalized substrings**, not word boundaries: "Anuj Shetty",
    "AnujShetty" and "anuj_shetty" are the same identity, and an earlier
    `\\b`-anchored version of this check passed a fixture that still named three
    real players.

    `forbidden` is pooled across every collection, so a player named in one
    fixture's free text but playing in another is still caught."""
    seen = set()
    for value in audited_strings(blob):
        seen |= field_tokens(value)
        seen.add(_norm(value))  # a whole field that IS the name, unsplittable
    wanted = {_norm(t) for t in forbidden} - TOKEN_STOPLIST
    leaked = sorted(t for t in wanted if t in seen)
    if leaked:
        raise SystemExit(
            f"REFUSING TO WRITE: {len(leaked)} real identity token(s) survived "
            f"anonymization: {', '.join(leaked)}"
        )


def build(snapshot, collections_by_title):
    """Yield (slug, fixture blob) for every configured collection.

    The audit vocabulary is pooled from the WHOLE snapshot, not just the games
    being written: a player who appears in one collection can be named in another
    one's free text, and a per-collection token list would wave that through."""
    forbidden = set()
    for col in snapshot.get("collections", []):
        for entry in col.get("games") or []:
            for player in entry["history"]["history"]["players"]:
                if not is_jesse(player):
                    forbidden |= identity_tokens(player)

    roster = {}  # real identity -> pseudonym slot, shared by every fixture
    built = []
    for slug, source_title, fixture_title in FIXTURE_COLLECTIONS:
        col = collections_by_title.get(source_title)
        if not col:
            raise SystemExit(
                f"{source_title!r} is not in {SNAPSHOT_PATH} — refresh the snapshot first")
        games = []
        for entry in col["games"]:
            game, tokens = anonymize_game(entry, roster)
            games.append(game)
            forbidden |= tokens
        built.append((slug, {"uuid": synth_id(col["uuid"]), "title": fixture_title,
                             "games": games}))

    # Audit every fixture against the pooled vocabulary, and only then hand any of
    # them back — one leak anywhere must stop the whole rebuild, not just its own
    # file.
    for _, blob in built:
        assert_scrubbed(blob, forbidden)
    return built


def dump(blob):
    """Deterministic bytes — sorted keys and a fixed mtime, so an unchanged corpus
    produces an identical file and `--check` means something."""
    raw = json.dumps(blob, sort_keys=True, separators=(",", ":")).encode()
    return gzip.compress(raw, compresslevel=9, mtime=0)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true",
                    help="verify the committed corpus matches a fresh rebuild; write nothing")
    args = ap.parse_args()

    if not os.path.exists(SNAPSHOT_PATH):
        raise SystemExit(
            f"{SNAPSHOT_PATH} absent — fetch it first (scripts/fetch_woogles_snapshot.py). "
            "The committed corpus in tests/fixtures/ is what the tests read; this script "
            "only rebuilds it."
        )
    with open(SNAPSHOT_PATH) as f:
        snapshot = json.load(f)
    by_title = {c["title"]: c for c in snapshot.get("collections", []) if c.get("games")}

    os.makedirs(FIXTURE_DIR, exist_ok=True)
    stale = 0
    for slug, blob in build(snapshot, by_title):
        path = os.path.join(FIXTURE_DIR, f"{slug}.json.gz")
        payload = dump(blob)
        if args.check:
            current = open(path, "rb").read() if os.path.exists(path) else None
            state = "OK  " if current == payload else "STALE"
            stale += current != payload
            print(f"{state} {path}", file=sys.stderr)
            continue
        with open(path, "wb") as f:
            f.write(payload)
        print(f"wrote {path}  ({len(payload) / 1024:.0f} KB, {len(blob['games'])} games)",
              file=sys.stderr)
    if args.check and stale:
        print(f"\n{stale} fixture(s) differ from a fresh rebuild.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
