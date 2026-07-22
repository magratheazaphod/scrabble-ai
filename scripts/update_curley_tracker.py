#!/usr/bin/env python3
"""Update the "Curley tracker" Google Sheet with James Curley practice games.

The /otb-scrabble-upload skill calls this after uploading a James Curley
practice game so the spreadsheet Jesse keeps of every game against James Curley
stays current without hand-entry. Two phases, keyed by the Woogles game id so
they always land on the same row:

  Phase 1 - from the .gcg, immediately after upload:
      python3 scripts/update_curley_tracker.py \
          --gcg "Practice Games/2026-07-12 James Curley.gcg" \
          --game-id 9G2uCPfVaXKhXpT9tCR84w
    Writes date, both scores, result, spread. Needs no network beyond Sheets.
    Idempotent: re-running updates the same row instead of duplicating it.

  Phase 2 - enrich once the game has been BestBot-analyzed:
      python3 scripts/update_curley_tracker.py --enrich \
          --game-id 9G2uCPfVaXKhXpT9tCR84w
    Fills the per-player analysis columns (bingos, blanks drawn, win% lost,
    mistake index — JD and James each) straight from the Woogles analysis API,
    the same GetAnalysisResult/GetGameHistory calls the tournament-analysis
    skill uses. A player's four cells are left blank unless their side of the
    game was FULLY annotated: game over, mistake_index present, and a full
    7-tile rack on every one of their turns (short racks allowed only once the
    bag is empty) — Woogles scores partially-known racks anyway, so
    mistake_index being non-null is NOT sufficient (per tournament-analysis).
    --enrich-collection does the same for every row that has a game id,
    skipping rows already filled; the woogles-report.yml workflow runs it
    after each report refresh, so newly analyzed games fill in automatically.

Auth: a Google service account (mirrors the WOOGLES_API_KEY-in-.env pattern).
Point GOOGLE_SA_KEYFILE at its JSON key and share the Curley tracker (Editor)
with the service account's client_email. Set CURLEY_TRACKER_SHEET_ID (or
CURLEY_TRACKER_SHEET_URL) to the spreadsheet; optionally CURLEY_TRACKER_WORKSHEET
for a specific tab. All may live in .env.

Column mapping: the sheet's own header row is read at runtime and matched to the
canonical fields below by header text (see COLUMN_ALIASES). Override any mapping
explicitly with a JSON file at CURLEY_TRACKER_COLUMN_MAP ({"field": "Header"}).

--dry-run prints what it would write without touching the sheet, and needs no
credentials, so phase 1 can be validated offline.
"""
import argparse
import json
import os
import re
import sys

# Who is who in the GCG. Jesse's nick is usually "JD" (see otb-scrabble-upload),
# but the historical Quackle archive uses several older self-export conventions.
JESSE_NICKS = {"jd", "jessed", "jesseday", "jesse"}
JESSE_NAMES = {"jesse day", "jesse", "jd", "jessed", "jesseday"}
CURLEY_NAMES = {"james curley", "james_curley", "jamesc", "jc", "james"}

# Canonical field -> acceptable header texts (compared lowercased/stripped,
# exact match, so "score" never collides with "opp score"). Extend freely once
# the real Curley-tracker headers are known.
# IMPORTANT: the Curley tracker is a pre-templated grid. "Game #" is pre-numbered
# and the W / L / T / combined columns (and a hidden column E) are FORMULAS that
# derive the result from the "me" and "jc" scores. The script must therefore only
# ever write the raw inputs (date, me, jc) plus the game-id key into the next
# empty row - never the formula columns. Those fields are intentionally absent
# from COLUMN_ALIASES so they can never be targeted.
COLUMN_ALIASES = {
    # phase 1 (from the .gcg) - the only cells we write per game
    "date": ["date", "game date", "date played", "played"],
    # "recorded score" headers (2026-07-15): the sheet keeps the score as recorded
    # at the table, which may legitimately differ from what the GCG replays to.
    "jesse_score": ["me", "jesse", "jesse score", "my score", "magrathean",
                    "jd recorded score", "jd recorded"],
    "opp_score": ["jc", "curley", "curley score", "opp score", "opponent score", "them",
                  "james recorded score", "james recorded"],
    "game_id": ["game id", "woogles game id", "game_id", "woogles id", "id", "uuid", "gameid"],
    # phase 2 (enrichment) - per-player BestBot analysis stats
    "jesse_bingos": ["jd bingos", "my bingos", "jesse bingos", "bingos"],
    "opp_bingos": ["james bingos", "jc bingos", "opp bingos", "opponent bingos", "curley bingos"],
    "jesse_blanks": ["jd blanks", "my blanks", "jesse blanks", "blanks", "blanks drawn"],
    "opp_blanks": ["james blanks", "jc blanks", "opp blanks", "opponent blanks"],
    "winpct_lost": ["jd win% lost", "win% lost", "win pct lost", "winpct lost", "win % lost"],
    "opp_winpct_lost": ["james win% lost", "jc win% lost", "opp win% lost"],
    "mistakes": ["jd mistake index", "mistakes", "mistakes score", "avg mistakes", "mistake score"],
    "opp_mistakes": ["james mistake index", "jc mistake index", "opp mistakes", "opp mistake index"],
}

# The 8 enrichment cells, in sheet order; and the header written when the sheet
# doesn't have a column for one yet (matching the "JD/James recorded score" style).
STATS_FIELDS = ["jesse_bingos", "opp_bingos", "jesse_blanks", "opp_blanks",
                "winpct_lost", "opp_winpct_lost", "mistakes", "opp_mistakes"]
STATS_HEADERS = {
    "jesse_bingos": "JD bingos", "opp_bingos": "James bingos",
    "jesse_blanks": "JD blanks", "opp_blanks": "James blanks",
    "winpct_lost": "JD win% lost", "opp_winpct_lost": "James win% lost",
    "mistakes": "JD mistake index", "opp_mistakes": "James mistake index",
}


# --------------------------------------------------------------------------- #
# Config / .env
# --------------------------------------------------------------------------- #
def load_dotenv(path=".env"):
    """Populate os.environ from a .env file (does not override real env vars)."""
    if not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def sheet_id_from_env():
    sid = os.environ.get("CURLEY_TRACKER_SHEET_ID", "").strip()
    if sid:
        return sid
    url = os.environ.get("CURLEY_TRACKER_SHEET_URL", "").strip()
    m = re.search(r"/spreadsheets/d/([A-Za-z0-9_-]+)", url)
    if m:
        return m.group(1)
    return url  # allow a bare id in the URL var too


# --------------------------------------------------------------------------- #
# --enrich-collection state (published to the woogles-data branch alongside the
# report pipeline's other markers — see woogles-report.yml)
# --------------------------------------------------------------------------- #
SNAPSHOT_PATH = "data/woogles-snapshot.json"
ENRICH_TERMINAL_PATH = "data/curley-enrich-terminal.txt"
ENRICH_MARKER_PATH = "data/curley-enrich-marker.txt"
# Do one full pass a day regardless, so a row added to the sheet by hand (which
# no snapshot signal can see) is never more than this stale.
ENRICH_MAX_SKIP_HOURS = 24


def load_terminal_ids():
    """Game ids proven un-enrichable: BestBot-analyzed, but with neither side
    fully annotated, so there is nothing to write and never will be unless the
    game is re-annotated on Woogles. Without this the same dead rows are
    re-fetched on every scheduled run, forever."""
    if not os.path.exists(ENRICH_TERMINAL_PATH):
        return set()
    with open(ENRICH_TERMINAL_PATH) as f:
        return {ln.strip() for ln in f if ln.strip() and not ln.startswith("#")}


def save_terminal_ids(ids):
    os.makedirs(os.path.dirname(ENRICH_TERMINAL_PATH), exist_ok=True)
    with open(ENRICH_TERMINAL_PATH, "w") as f:
        f.write("# Games with nothing for --enrich-collection to write, and no prospect\n"
                "# of that changing on its own: analyzed but with neither side fully\n"
                "# annotated, or a permanently FAILED analysis (a malformed upload).\n"
                "# Re-examine them all with --recheck-terminal.\n")
        for gid in sorted(ids):
            f.write(gid + "\n")


def analyzed_fingerprint():
    """Hash of the set of Curley games the fresh snapshot reports as analyzed.

    It changes exactly when a new game's BestBot analysis lands — the only event
    that can hand --enrich-collection something new to write. None when there is
    no usable snapshot, which means "can't tell, do a full pass"."""
    import hashlib

    if not os.path.exists(SNAPSHOT_PATH):
        return None
    try:
        with open(SNAPSHOT_PATH) as f:
            snap = json.load(f)
    except (OSError, ValueError):
        return None
    ids = set()
    for coll in snap.get("collections") or []:
        if "curley" not in (coll.get("title") or "").lower():
            continue
        for g in coll.get("games") or []:
            if (g.get("analysis") or {}).get("found"):
                gid = (g.get("meta") or {}).get("game_id")
                if gid:
                    ids.add(gid)
    if not ids:
        return None
    return hashlib.sha1("\n".join(sorted(ids)).encode()).hexdigest()


def read_enrich_marker():
    """(fingerprint, hours_since) from the last completed pass; (None, None) if
    there is no readable marker."""
    from datetime import datetime, timezone

    if not os.path.exists(ENRICH_MARKER_PATH):
        return None, None
    with open(ENRICH_MARKER_PATH) as f:
        parts = f.read().split()
    if len(parts) != 2:
        return None, None
    fp, stamp = parts
    try:
        when = datetime.fromisoformat(stamp)
    except ValueError:
        return fp, None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return fp, (datetime.now(timezone.utc) - when).total_seconds() / 3600


def write_enrich_marker(fingerprint):
    from datetime import datetime, timezone

    if not fingerprint:
        return
    os.makedirs(os.path.dirname(ENRICH_MARKER_PATH), exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with open(ENRICH_MARKER_PATH, "w") as f:
        f.write(f"{fingerprint} {stamp}\n")


def enrichment_worth_running(force):
    """(should_run, fingerprint, why) — decided from local files only, before
    any Sheets or Woogles call.

    The scheduled workflow fires ~16x a day; enrichment only ever has work when
    a new analysis has landed. Opening the sheet each time just to rediscover
    there is nothing to do is pure cost, and a transient Sheets 503 on one of
    those no-op opens is what turned the run red on 2026-07-21."""
    fp = analyzed_fingerprint()
    if force:
        return True, fp, "--recheck-terminal forces a full pass"
    if fp is None:
        return True, fp, "no usable snapshot to compare against — full pass"
    last_fp, age_h = read_enrich_marker()
    if last_fp != fp:
        return True, fp, "new BestBot analysis since the last pass"
    if age_h is None:
        return True, fp, "last pass has no readable timestamp — full pass"
    if age_h >= ENRICH_MAX_SKIP_HOURS:
        return True, fp, f"last pass was {age_h:.0f}h ago (daily floor)"
    return False, fp, f"no new analysis since the last pass {age_h:.1f}h ago"


# --------------------------------------------------------------------------- #
# GCG parsing (phase 1)
# --------------------------------------------------------------------------- #
def parse_gcg(path, game_id):
    """Return the phase-1 record dict from a .gcg file."""
    with open(path, encoding="utf-8", errors="replace") as f:
        lines = f.readlines()

    players = {}  # nick -> full name
    for l in lines:
        m = re.match(r"#player([12])\s+(\S+)\s+(.+?)\s*$", l)
        if m:
            players[m.group(2)] = m.group(3).strip()
    if len(players) != 2:
        raise ValueError(f"expected 2 #player headers, found {len(players)} in {path}")

    jesse_nick = opp_nick = None
    for nick, name in players.items():
        if nick.lower() in JESSE_NICKS or name.lower() in JESSE_NAMES:
            jesse_nick = nick
        else:
            opp_nick = nick
    if jesse_nick is None or opp_nick is None:
        raise ValueError(f"could not tell Jesse from opponent in {path}: {players}")

    opp_name = players[opp_nick]
    if opp_name.lower() not in CURLEY_NAMES and opp_name.lower().replace("_", " ") not in CURLEY_NAMES:
        raise ValueError(
            f"opponent is {opp_name!r}, not James Curley - refusing "
            f"(this tracker is Curley-only). Use a general games sheet instead.")

    # Final cumulative per nick = the last integer on that nick's last move line.
    scores = {}
    for l in lines:
        if not l.startswith(">"):
            continue
        nick = l[1:].split(":", 1)[0].strip()
        nums = re.findall(r"-?\d+", l)
        if nums:
            scores[nick] = int(nums[-1])
    if jesse_nick not in scores or opp_nick not in scores:
        raise ValueError(f"could not read a final score for both players in {path}")

    jesse_score, opp_score = scores[jesse_nick], scores[opp_nick]

    date = None
    for l in lines:
        if l.startswith("#description"):
            m = re.search(r"(\d{4}-\d{2}-\d{2})", l)
            if m:
                date = m.group(1)
                break
    if date is None:
        m = re.search(r"(\d{4}-\d{2}-\d{2})", os.path.basename(path))
        date = m.group(1) if m else ""

    # Only the raw inputs — the sheet's formulas derive W/L/T/combined from these.
    return {
        "date": mdy(date),
        "jesse_score": jesse_score,
        "opp_score": opp_score,
        "game_id": game_id,
    }


def mdy(iso_date):
    """'2026-07-12' -> '7/12/2026' to match the sheet's date style. Passthrough
    if the string isn't ISO."""
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})$", iso_date or "")
    if not m:
        return iso_date or ""
    y, mo, d = m.groups()
    return f"{int(mo)}/{int(d)}/{int(y)}"


# --------------------------------------------------------------------------- #
# Enrichment (phase 2) - per-player BestBot stats straight from the Woogles API
# (the same GetAnalysisResult / GetGameHistory calls tournament-analysis uses)
# --------------------------------------------------------------------------- #
WOOGLES_BASE = "https://woogles.io/api"

JESSE_SUMMARY_NAMES = {"jd", "jessed", "jesseday", "jesse", "dayjesse"}


def woogles_rpc(endpoint, body):
    """POST to a Woogles RPC, retrying transient 429/5xx with backoff."""
    import random
    import time

    import requests

    hdrs = {"Content-Type": "application/json"}
    key = os.environ.get("WOOGLES_API_KEY", "").strip()
    if key:
        hdrs["X-Api-Key"] = key
    for attempt in range(4):
        r = requests.post(f"{WOOGLES_BASE}/{endpoint}", json=body, headers=hdrs, timeout=30)
        if r.status_code == 429 or r.status_code >= 500:
            if attempt == 3:
                r.raise_for_status()
            time.sleep((2 ** attempt) + random.uniform(0, 0.5))
            continue
        r.raise_for_status()
        return r.json()


def _is_jesse_player(p):
    nick = (p.get("nickname") or "").lower().replace("_", "")
    real = (p.get("real_name") or "").lower()
    return nick in ("jd", "jessed", "jesseday", "jesse") or "jesse" in real


def _blanks_drawn(events, last_racks, idx):
    """Blanks that passed through player idx's rack: played (lowercase tile),
    exchanged ('?'), or still held at the end. Plays that were challenged off
    are skipped — those tiles went back and are counted when actually used."""
    total = 0
    for i, ev in enumerate(events):
        if ev.get("player_index") != idx:
            continue
        if ev.get("type") == "TILE_PLACEMENT_MOVE":
            if i + 1 < len(events) and events[i + 1].get("type") == "PHONY_TILES_RETURNED":
                continue
            total += sum(1 for c in (ev.get("played_tiles") or "") if c.islower())
        elif ev.get("type") == "EXCHANGE":
            total += (ev.get("exchanged") or "").count("?")
    if len(last_racks) > idx and last_racks[idx]:
        total += last_racks[idx].count("?")
    return total


def _racks_complete(turns, idx):
    """True iff player idx's rack is fully known on every turn — a full 7-tile
    rack, or a short rack only once the bag is empty. Woogles infers partial
    racks from played tiles and scores them anyway, so this (not a non-null
    mistake_index) is the signal that the side was fully annotated."""
    return all(
        len(t.get("rack") or "") == 7 or (t.get("tiles_in_bag") or 0) == 0
        for t in turns if t.get("player_index") == idx
    )


def analysis_status(game_id):
    """(status, error_message) from GetAnalysisStatus.

    The error message matters: a FAILED analysis is usually a permanent defect
    in the uploaded game (e.g. 'turn 18: rack "AAGIIKSUUY" has 10 tiles, max is
    7' — a malformed GCG rack), not a transient queue problem, and re-requesting
    it forever accomplishes nothing."""
    r = woogles_rpc("analysis_service.AnalysisService/GetAnalysisStatus",
                    {"game_id": game_id})
    return r.get("status"), (r.get("error_message") or "").strip()


def game_stats_with_status(game_id):
    """(stats_or_None, status, error_message) — the enrichment view of one game,
    keeping *why* there are no stats instead of collapsing it all to None."""
    status, err = analysis_status(game_id)
    if status != "COMPLETED":
        return None, status, err
    return game_stats_from_api(game_id, _status_checked=True), status, err


def game_stats_from_api(game_id, _status_checked=False):
    """The 8 enrichment fields for one game, or None if not yet analyzed.

    A side that wasn't fully annotated gets None for all four of its fields
    (cells stay blank), per Jesse 2026-07-15."""
    if not _status_checked and analysis_status(game_id)[0] != "COMPLETED":
        return None
    analysis = woogles_rpc("analysis_service.AnalysisService/GetAnalysisResult",
                           {"game_id": game_id})["result"]
    history = woogles_rpc("game_service.GameMetadataService/GetGameHistory",
                          {"game_id": game_id})["history"]

    players = history.get("players") or []
    jesse_idx = next((i for i, p in enumerate(players) if _is_jesse_player(p)), None)
    if jesse_idx is None or len(players) != 2:
        print(f"    {game_id}: could not identify Jesse among {players} — skipping stats")
        return None
    turns = analysis.get("turns") or []
    events = history.get("events") or []
    last_racks = history.get("last_known_racks") or []
    game_over = history.get("play_state") == "GAME_OVER"

    mistake_by_side = {}
    for s in analysis.get("player_summaries") or []:
        name = (s.get("player_name") or "").lower().replace("_", "")
        mistake_by_side[name in JESSE_SUMMARY_NAMES] = s.get("mistake_index")

    side_fields = {True: ("jesse_bingos", "jesse_blanks", "winpct_lost", "mistakes"),
                   False: ("opp_bingos", "opp_blanks", "opp_winpct_lost", "opp_mistakes")}
    fields = {}
    for is_jesse, (f_bingos, f_blanks, f_wpl, f_mi) in side_fields.items():
        idx = jesse_idx if is_jesse else 1 - jesse_idx
        mi = mistake_by_side.get(is_jesse)
        if not (game_over and mi is not None and _racks_complete(turns, idx)):
            fields.update({f_bingos: None, f_blanks: None, f_wpl: None, f_mi: None})
            continue
        fields[f_bingos] = sum(1 for t in turns
                               if t.get("player_index") == idx and t.get("played_is_bingo"))
        fields[f_blanks] = _blanks_drawn(events, last_racks, idx)
        fields[f_wpl] = round(sum(t.get("win_prob_loss") or 0 for t in turns
                                  if t.get("player_index") == idx) * 100, 1)
        fields[f_mi] = round(mi, 2)
    return fields


# --------------------------------------------------------------------------- #
# Google Sheet I/O
# --------------------------------------------------------------------------- #
def game_id_cell(gid):
    """The game-id cell is a HYPERLINK to the annotated game, labeled with the
    bare id — so it's clickable for Jesse while every reader (col_values /
    get_all_values return the formatted label) still sees just the id."""
    return f'=HYPERLINK("https://woogles.io/anno/{gid}","{gid}")'
RETRYABLE_SHEETS_CODES = ("429", "500", "502", "503", "504")


def _retry(fn, *args, **kwargs):
    """Retry a gspread call with exponential backoff on Sheets API 429s and 5xxs.

    429: the bulk archive backfill fires enough per-game reads to trip the
    per-minute read quota, and a 429 there must never be mistaken for 'no row
    exists'. 5xx: Google returns transient 503s often enough that one on a
    routine sheet open reddened the nightly workflow on 2026-07-21."""
    import gspread
    import time

    delay = 5
    for attempt in range(6):
        try:
            return fn(*args, **kwargs)
        except gspread.exceptions.APIError as e:
            if not any(c in str(e) for c in RETRYABLE_SHEETS_CODES) or attempt == 5:
                raise
            print(f"    (Sheets API error, retrying in {delay}s: {e})", file=sys.stderr)
            time.sleep(delay)
            delay = min(delay * 2, 60)


def open_worksheet():
    """Authorize via the service account and return the target worksheet."""
    import gspread  # lazy: dry-run needs neither the dep nor credentials

    keyfile = os.environ.get("GOOGLE_SA_KEYFILE", "").strip()
    if not keyfile:
        sys.exit("GOOGLE_SA_KEYFILE is not set (path to the service-account JSON key).")
    if not os.path.exists(keyfile):
        sys.exit(f"service-account key not found: {keyfile}")
    sid = sheet_id_from_env()
    if not sid:
        sys.exit("CURLEY_TRACKER_SHEET_ID (or _URL) is not set.")

    gc = gspread.service_account(filename=keyfile)
    sh = _retry(gc.open_by_key, sid)
    tab = os.environ.get("CURLEY_TRACKER_WORKSHEET", "").strip()
    # worksheet()/sheet1 fetch spreadsheet metadata — also a quota-counted read
    return _retry(sh.worksheet, tab) if tab else _retry(lambda: sh.sheet1)


def build_header_map(header_row):
    """Map canonical field -> 1-based column index using COLUMN_ALIASES.

    An explicit CURLEY_TRACKER_COLUMN_MAP JSON ({"field": "Header"}) wins.
    Returns (field->col, unresolved_fields).
    """
    lowered = {h.strip().lower(): i + 1 for i, h in enumerate(header_row) if h.strip()}
    override = {}
    ov_path = os.environ.get("CURLEY_TRACKER_COLUMN_MAP", "").strip()
    if ov_path and os.path.exists(ov_path):
        override = {k: v.strip().lower() for k, v in json.load(open(ov_path)).items()}

    field_col = {}
    for field, aliases in COLUMN_ALIASES.items():
        if field in override and override[field] in lowered:
            field_col[field] = lowered[override[field]]
            continue
        for alias in aliases:
            if alias in lowered:
                field_col[field] = lowered[alias]
                break
    unresolved = [f for f in COLUMN_ALIASES if f not in field_col]
    return field_col, unresolved


def find_row_for_game(ws, game_col, game_id):
    """Return the 1-based row index whose game_id cell == game_id, or None."""
    for i, val in enumerate(_retry(ws.col_values, game_col), start=1):
        if i == 1:
            continue  # header
        if val.strip() == game_id:
            return i
    return None


def find_row_for_game_number(ws, game_num_col, n):
    """Return the 1-based row index whose 'Game #' cell == n, or None."""
    for i, val in enumerate(_retry(ws.col_values, game_num_col), start=1):
        if i == 1:
            continue  # header
        if val.strip() == str(n):
            return i
    return None


def next_empty_slot(ws, score_col):
    """First data row (>=2) whose 'me' score cell is empty — the next game slot.

    "Game #" is pre-numbered down the column, but the result FORMULAS are not
    dragged into blank rows, so write_new_game() re-creates them for the row.
    """
    return len(_retry(ws.col_values, score_col)) + 1


def locate_formula_cols(header):
    """Locate the result columns so we can rebuild their formulas in a new row.

    Returns a dict with 1-based indices for game_num, me, jc, w, l, combined and
    'helper' (the unlabeled win-value column immediately left of W). Values are
    None when a column isn't found.
    """
    low = {h.strip().lower(): i + 1 for i, h in enumerate(header) if h.strip()}

    def find(*names):
        return next((low[n] for n in names if n in low), None)

    cols = {
        "game_num": find("game #", "game#", "game number"),
        "me": find("me", "jesse", "my score", "jd recorded score"),
        "jc": find("jc", "curley", "opp score", "james recorded score"),
        "w": find("w", "win", "wins"),
        "l": find("l", "loss", "losses"),
        "combined": find("combined", "total"),
    }
    cols["helper"] = cols["w"] - 1 if cols["w"] else None  # hidden win-value col E
    return cols


def write_new_game(ws, field_col, fcols, row, fields):
    """Write a new game into `row`: the raw inputs plus the sheet's own formulas
    for the hidden win-value (E), W, L and combined — replicating how the existing
    rows are built, with the running-total anchor (=SUM($E$2:...)) preserved."""
    import gspread.utils as gu

    def a1(col):
        return gu.rowcol_to_a1(row, col)

    def letter(col):
        return gu.rowcol_to_a1(1, col).rstrip("1")

    cells = []
    # raw inputs (date, me, jc, game_id) via the resolved field columns
    for field in ("date", "jesse_score", "opp_score", "game_id"):
        if field in field_col and field in fields:
            value = (game_id_cell(fields[field]) if field == "game_id"
                     else str(fields[field]))
            cells.append({"range": a1(field_col[field]), "values": [[value]]})

    me, jc, gn, w, hp, cb, ll = (fcols.get(k) for k in
                                 ("me", "jc", "game_num", "w", "helper", "combined", "l"))
    if hp and me and jc:  # hidden win-value: 1 win / 0.5 tie / 0 loss
        cells.append({"range": a1(hp),
                      "values": [[f"=0.5+0.5*(({letter(me)}{row}>{letter(jc)}{row})"
                                  f"-({letter(me)}{row}<{letter(jc)}{row}))"]]})
    if w and hp:  # W = running cumulative win total
        cells.append({"range": a1(w),
                      "values": [[f"=SUM(${letter(hp)}$2:{letter(hp)}{row})"]]})
    if ll and gn and w:  # L = games so far minus W
        cells.append({"range": a1(ll), "values": [[f"={letter(gn)}{row}-{letter(w)}{row}"]]})
    if cb and me and jc:  # combined = me + jc
        cells.append({"range": a1(cb), "values": [[f"={letter(me)}{row}+{letter(jc)}{row}"]]})

    _retry(ws.batch_update, cells, value_input_option="USER_ENTERED")
    return cells


def apply_fields(ws, field_col, ncols, row_index, fields):
    """Append (row_index None) or update the given canonical fields in place."""
    import gspread.utils as gu

    def cell_value(field, value):
        if value is None:
            return ""
        return game_id_cell(value) if field == "game_id" else str(value)

    if row_index is None:
        row = [""] * ncols
        for field, value in fields.items():
            if field in field_col:
                row[field_col[field] - 1] = cell_value(field, value)
        _retry(ws.append_row, row, value_input_option="USER_ENTERED")
        return "appended"

    cells = []
    for field, value in fields.items():
        if field not in field_col:
            continue
        a1 = gu.rowcol_to_a1(row_index, field_col[field])
        cells.append({"range": a1, "values": [[cell_value(field, value)]]})
    if cells:
        _retry(ws.batch_update, cells, value_input_option="USER_ENTERED")
    return f"updated row {row_index}"


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def sheet_configured():
    return bool(os.environ.get("GOOGLE_SA_KEYFILE", "").strip() and sheet_id_from_env())


def main(argv):
    ap = argparse.ArgumentParser(description="Update the Curley tracker sheet.")
    ap.add_argument("--gcg", help="path to the .gcg file (phase 1)")
    ap.add_argument("--game-id", help="Woogles game/anno id (the row key)")
    ap.add_argument("--game-num", type=int,
                    help="backfill mode: locate the existing pre-scored row by its 'Game #' "
                         "(instead of matching/appending by game id), validate the .gcg's "
                         "final scores against that row's me/jc, and write ONLY the game id. "
                         "Refuses to run if the row already has a game id, or if the scores "
                         "don't match. For the historical-archive backfill, not new games.")
    ap.add_argument("--allow-score-mismatch", action="store_true",
                    help="with --game-num: tolerate the sheet's recorded scores differing "
                         "from the .gcg's replayed scores. Per Jesse (2026-07-15) the sheet "
                         "keeps the score as recorded at the table; the GCG is what the tiles "
                         "actually add up to, and the two may legitimately disagree. The "
                         "sheet's scores are never overwritten — only the game id is written.")
    ap.add_argument("--enrich", action="store_true",
                    help="phase 2: fill one game's analysis columns from report-state.json")
    ap.add_argument("--enrich-collection", action="store_true",
                    help="phase 2 for every analyzed JC game that already has a row "
                         "(for the report pipeline; skips games with no row yet)")
    ap.add_argument("--recheck-terminal", action="store_true",
                    help="with --enrich-collection: ignore the remembered set of "
                         "un-enrichable games and re-examine every blank row, even if "
                         "no new analysis has landed. Use after re-uploading a game whose "
                         "analysis had failed, or whose annotation was completed.")
    ap.add_argument("--skip-if-unconfigured", action="store_true",
                    help="exit 0 (instead of erroring) when no sheet credentials are set")
    ap.add_argument("--dry-run", action="store_true",
                    help="print what would be written; touches nothing, no creds needed")
    args = ap.parse_args(argv)
    load_dotenv()

    if args.skip_if_unconfigured and not sheet_configured():
        print("Curley tracker not configured (no GOOGLE_SA_KEYFILE / sheet id) — skipping.")
        return 0

    if args.enrich_collection:
        return run_enrich_collection(args.dry_run, args.recheck_terminal)

    if args.enrich:
        if not args.game_id:
            ap.error("--game-id is required with --enrich")
        stats = game_stats_from_api(args.game_id)
        if stats is None:
            print(f"{args.game_id}: BestBot analysis not completed yet — nothing to enrich.")
            return 0
        fields = {k: v for k, v in stats.items() if v is not None}
        if not fields:
            print(f"{args.game_id}: analyzed, but neither side was fully annotated — "
                  f"stats cells stay blank.")
            return 0
        phase = "enrich"
    else:
        if not args.gcg or not args.game_id:
            ap.error("--gcg and --game-id are required for a phase-1 update")
        fields = parse_gcg(args.gcg, args.game_id)
        phase = "phase 1" if args.game_num is None else f"backfill Game #{args.game_num}"

    print(f"[{phase}] game {args.game_id}")
    for k, v in fields.items():
        print(f"    {k:22} {v}")

    if args.game_num is not None:
        return backfill_by_game_number(args.game_num, fields, args.dry_run,
                                       args.allow_score_mismatch)

    if args.dry_run:
        print("\n(dry run - sheet not touched)")
        return 0

    ws = open_worksheet()
    header = _retry(ws.row_values, 1)
    field_col = report_mapping(ws, fields)
    if args.enrich:
        ensure_stats_columns(ws, field_col, header, dry_run=False)
    row_index = find_row_for_game(ws, field_col["game_id"], args.game_id)

    if args.enrich:
        if row_index is None:
            sys.exit(f"No existing row for {args.game_id}; run phase 1 (--gcg) first.")
        apply_fields(ws, field_col, len(header) + len(STATS_FIELDS), row_index, fields)
        print(f"\nupdated row {row_index}.")
        return 0

    if row_index is None:  # new game -> next empty templated row, with formulas
        row_index = next_empty_slot(ws, field_col["jesse_score"])
        write_new_game(ws, field_col, locate_formula_cols(header), row_index, fields)
        print(f"\nwrote new game to row {row_index}.")
    else:  # re-run -> refresh the raw inputs only; leave the formulas intact
        apply_fields(ws, field_col, len(header), row_index, fields)
        print(f"\nupdated existing row {row_index}.")
    return 0


def report_mapping(ws, fields):
    """Print and return the field->column mapping; abort if there's no key column."""
    header = _retry(ws.row_values, 1)
    field_col, _ = build_header_map(header)
    print(f"\nsheet: {ws.spreadsheet.title!r} / {ws.title!r}  ({len(header)} columns)")
    print("column mapping:")
    for field in fields:
        col = field_col.get(field)
        print(f"    {field:22} -> {'col ' + str(col) if col else 'NO MATCHING COLUMN (skipped)'}")
    if "game_id" not in field_col:
        sys.exit("The sheet has no game-id column, so rows can't be keyed/deduped. "
                 "Add a column (e.g. 'Game ID') or set CURLEY_TRACKER_COLUMN_MAP.")
    return field_col


def backfill_by_game_number(game_num, fields, dry_run, allow_score_mismatch=False):
    """Historical-archive backfill: the row for `game_num` already has hand-entered
    date/me/jc (and working W/L formulas) from years of manual tracking, just no
    game id yet. Locate it by 'Game #' (not by game id — there isn't one), verify
    the .gcg's final scores match what's already in the row (catches a mislabeled
    or misnumbered archive file before it corrupts the sheet), then write ONLY the
    game id. Refuses if the row already has one, or if scores don't match —
    unless --allow-score-mismatch, in which case the sheet keeps its recorded
    scores (they are the score as kept at the table, which may differ from what
    the tiles replay to) and only the game id is written."""
    ws = open_worksheet()
    header = _retry(ws.row_values, 1)
    field_col = report_mapping(ws, fields)
    fcols = locate_formula_cols(header)
    if fcols["game_num"] is None:
        sys.exit("no 'Game #' column found — can't locate the row for --game-num.")

    row_index = find_row_for_game_number(ws, fcols["game_num"], game_num)
    if row_index is None:
        sys.exit(f"no row found for Game #{game_num}.")

    existing = _retry(ws.row_values, row_index)

    def cell(field):
        col = field_col.get(field)
        return existing[col - 1].strip() if col and len(existing) >= col else ""

    existing_gid = cell("game_id")
    if existing_gid:
        sys.exit(f"row {row_index} (Game #{game_num}) already has a game id "
                 f"({existing_gid}) — refusing to overwrite.")

    for field, label in (("jesse_score", "me"), ("opp_score", "jc")):
        existing_val = cell(field)
        if existing_val and existing_val.isdigit() and int(existing_val) != fields[field]:
            if allow_score_mismatch:
                print(f"    note: sheet {label}={existing_val} vs .gcg={fields[field]} — "
                      f"keeping the sheet's recorded score (--allow-score-mismatch).")
                continue
            sys.exit(f"score mismatch at row {row_index} (Game #{game_num}): sheet "
                     f"{label}={existing_val} vs .gcg={fields[field]} — this file may be "
                     f"mislabeled/misnumbered. Not writing anything. "
                     f"(--allow-score-mismatch overrides, keeping the sheet's numbers.)")

    if dry_run:
        print(f"\n(dry run) would backfill game id into row {row_index} "
              f"(Game #{game_num}); scores match.")
        return 0

    apply_fields(ws, field_col, len(header), row_index, {"game_id": fields["game_id"]})
    print(f"\nbackfilled game id into row {row_index} (Game #{game_num}).")
    return 0


def ensure_stats_columns(ws, field_col, header, dry_run):
    """Make sure the sheet has a column for every stats field, appending headers
    (in STATS_FIELDS order) after the last header cell for any that are missing.
    Updates field_col in place."""
    import gspread.utils as gu

    missing = [f for f in STATS_FIELDS if f not in field_col]
    if not missing:
        return
    start = len(header) + 1
    cells = [{"range": gu.rowcol_to_a1(1, start + i), "values": [[STATS_HEADERS[f]]]}
             for i, f in enumerate(missing)]
    if dry_run:
        print(f"(dry run) would add header column(s): {[STATS_HEADERS[f] for f in missing]}")
    else:
        _retry(ws.batch_update, cells, value_input_option="USER_ENTERED")
        print(f"added header column(s): {[STATS_HEADERS[f] for f in missing]}")
    for i, f in enumerate(missing):
        field_col[f] = start + i


def run_enrich_collection(dry_run, recheck_terminal=False):
    """Fill the per-player stats cells for every row that has a game id.

    One read of the whole sheet, one Woogles API sweep (only for rows whose
    stats cells are all still empty), one batch write. Games not yet
    BestBot-analyzed stay blank and are retried on the next run; a row with
    any stats cell already filled is considered done and never re-fetched
    (its remaining blanks mean that side wasn't fully annotated).

    Two guards keep this from re-doing dead work every 90 minutes: the whole
    pass is skipped unless new analysis has landed (see enrichment_worth_running)
    and rows proven un-enrichable are remembered (see load_terminal_ids)."""
    import gspread.utils as gu
    from concurrent.futures import ThreadPoolExecutor

    should_run, fingerprint, why = enrichment_worth_running(recheck_terminal)
    if not should_run:
        print(f"[enrich-collection] skipped — {why}.")
        return 0
    print(f"[enrich-collection] running — {why}.")
    terminal = set() if recheck_terminal else load_terminal_ids()

    ws = open_worksheet()
    rows = _retry(ws.get_all_values)
    header = rows[0]
    field_col, _ = build_header_map(header)
    if "game_id" not in field_col:
        sys.exit("The sheet has no game-id column — can't key rows for enrichment.")
    ensure_stats_columns(ws, field_col, header, dry_run)

    gid_col = field_col["game_id"]
    todo, skipped = [], 0
    for ridx, row in enumerate(rows[1:], start=2):
        gid = row[gid_col - 1].strip() if len(row) >= gid_col else ""
        if not gid:
            continue
        filled = any((row[field_col[f] - 1].strip() if len(row) >= field_col[f] else "")
                     for f in STATS_FIELDS)
        if filled:
            continue
        if gid in terminal:
            skipped += 1
            continue
        todo.append((ridx, gid))
    if skipped:
        print(f"[enrich-collection] skipping {skipped} row(s) already proven un-enrichable "
              f"(re-examine with --recheck-terminal).")
    print(f"[enrich-collection] {len(todo)} row(s) with a game id and no stats yet.")
    if not todo:
        if not dry_run:
            write_enrich_marker(fingerprint)
        return 0

    with ThreadPoolExecutor(max_workers=4) as ex:
        results = list(ex.map(lambda t: game_stats_with_status(t[1]), todo))

    cells, enriched, pending, new_terminal = [], 0, 0, []
    for (ridx, gid), (stats, status, err) in zip(todo, results):
        if stats is None:
            # A FAILED analysis is a defect in the uploaded game, not a queue
            # position — it will fail identically on every retry, so retire the
            # row instead of re-fetching it forever. Anything else really is
            # still in flight.
            if status == "FAILED":
                new_terminal.append(gid)
                print(f"  {gid}: row {ridx} analysis FAILED — {err or 'no reason given'}")
            else:
                pending += 1
                print(f"  {gid}: row {ridx} not analyzed yet (status {status})")
            continue
        wrote = False
        for f, v in stats.items():
            if v is None or f not in field_col:
                continue
            cells.append({"range": gu.rowcol_to_a1(ridx, field_col[f]), "values": [[str(v)]]})
            wrote = True
        if not wrote:
            new_terminal.append(gid)
        print(f"  {gid}: row {ridx} " +
              ("enriched" if wrote else "analyzed but no side fully annotated"))
        enriched += 1 if wrote else 0
    if dry_run:
        print(f"\n(dry run) would write {len(cells)} cell(s), retire {len(new_terminal)} "
              f"un-enrichable row(s); {pending} still awaiting BestBot analysis.")
        return 0
    if cells:
        _retry(ws.batch_update, cells, value_input_option="USER_ENTERED")
    if new_terminal:
        save_terminal_ids(terminal | set(new_terminal))
        print(f"retired {len(new_terminal)} un-enrichable row(s): {', '.join(new_terminal)}")
    write_enrich_marker(fingerprint)
    print(f"\nenriched {enriched} game(s); {pending} still awaiting BestBot analysis.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
