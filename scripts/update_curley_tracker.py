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

  Phase 2 - enrich once the game has been BestBot-analyzed and the reporting
  pipeline has regenerated .github/report-state.json:
      python3 scripts/update_curley_tracker.py --enrich \
          --game-id 9G2uCPfVaXKhXpT9tCR84w
    Fills the analysis columns (mistakes score, bingos, blanks, endgame spread
    lost, win% lost, notes) by parsing the per-game table the
    tournament-analysis pipeline already produces. Nothing is recomputed here.

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

# The James Curley practice-games collection, per .github/report-state.json.
CURLEY_COLLECTION_UUID = "55b29df3-10fd-471b-9e87-135ed5bbb2f6"
REPORT_STATE_PATH = ".github/report-state.json"

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
    "jesse_score": ["me", "jesse", "jesse score", "my score", "magrathean"],
    "opp_score": ["jc", "curley", "curley score", "opp score", "opponent score", "them"],
    "game_id": ["game id", "woogles game id", "game_id", "woogles id", "id", "uuid", "gameid"],
    # phase 2 (enrichment) - only written if Jesse adds these columns later
    "mistakes": ["mistakes", "mistakes score", "avg mistakes", "mistake score"],
    "jesse_bingos": ["my bingos", "jesse bingos", "bingos"],
    "opp_bingos": ["jc bingos", "opp bingos", "opponent bingos", "curley bingos"],
    "jesse_blanks": ["my blanks", "jesse blanks", "blanks", "blanks drawn"],
    "endgame_spread_lost": ["endgame spread lost", "endgame spread", "endgame"],
    "winpct_lost": ["win% lost", "win pct lost", "winpct lost", "win % lost"],
    "notes": ["notes", "note", "comment", "comments"],
}

PHASE2_FIELDS = ["mistakes", "jesse_bingos", "opp_bingos", "jesse_blanks",
                 "endgame_spread_lost", "winpct_lost", "notes"]


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
# Enrichment parsing (phase 2) - reuse the reporting pipeline's per-game table
# --------------------------------------------------------------------------- #
# Header text in the report_md table -> canonical enrichment field.
REPORT_COL_MAP = {
    "mistakes": "mistakes",
    "jesse bingos": "jesse_bingos",
    "opp bingos": "opp_bingos",
    "jesse blanks": "jesse_blanks",
    "endgame spread lost": "endgame_spread_lost",
    "win% lost": "winpct_lost",
    "notes": "notes",
}


def _split_md_row(line):
    cells = line.strip().strip("|").split("|")
    return [c.strip() for c in cells]


def parse_report_table(path=REPORT_STATE_PATH):
    """Parse the JC collection's per-game analysis table from report-state.json.

    Returns {game_id: enrichment_dict} for every analyzed game found, or {} if
    there's no report yet.
    """
    if not os.path.exists(path):
        return {}
    state = json.load(open(path))
    entry = state.get(CURLEY_COLLECTION_UUID)
    if not entry or "report_md" not in entry:
        return {}
    lines = entry["report_md"].splitlines()

    # Locate the per-game table: the header row is the one naming "Mistakes".
    header_cells = col_index = None
    for i, line in enumerate(lines):
        if "|" in line and "mistakes" in line.lower() and "jesse bingos" in line.lower():
            header_cells = [c.lower() for c in _split_md_row(line)]
            col_index = i
            break
    if header_cells is None:
        return {}

    field_by_col = {idx: REPORT_COL_MAP[h] for idx, h in enumerate(header_cells)
                    if h in REPORT_COL_MAP}
    game_col = header_cells.index("game") if "game" in header_cells else None

    out = {}
    for line in lines[col_index + 2:]:  # skip the |---| separator
        if "|" not in line:
            break
        cells = _split_md_row(line)
        if game_col is None or game_col >= len(cells):
            continue
        m = re.search(r"/game/([A-Za-z0-9_-]+)", cells[game_col])
        if not m:  # the trailing "Avg" row has no game link
            continue
        out[m.group(1)] = {field: cells[idx] for idx, field in field_by_col.items()
                           if idx < len(cells)}
    return out


def enrichment_from_report_state(game_id, path=REPORT_STATE_PATH):
    """Enrichment dict for one game, or None if it isn't in the report yet."""
    return parse_report_table(path).get(game_id)


# --------------------------------------------------------------------------- #
# Google Sheet I/O
# --------------------------------------------------------------------------- #
def _retry(fn, *args, **kwargs):
    """Retry a gspread call with exponential backoff on Sheets API 429s — the
    bulk archive backfill fires enough per-game reads to trip the per-minute
    read quota, and a 429 there must never be mistaken for 'no row exists'."""
    import gspread
    import time

    delay = 5
    for attempt in range(6):
        try:
            return fn(*args, **kwargs)
        except gspread.exceptions.APIError as e:
            if "429" not in str(e) or attempt == 5:
                raise
            print(f"    (Sheets API rate limited, retrying in {delay}s...)", file=sys.stderr)
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
    sh = gc.open_by_key(sid)
    tab = os.environ.get("CURLEY_TRACKER_WORKSHEET", "").strip()
    return sh.worksheet(tab) if tab else sh.sheet1


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
        "me": find("me", "jesse", "my score"),
        "jc": find("jc", "curley", "opp score"),
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
            cells.append({"range": a1(field_col[field]), "values": [[str(fields[field])]]})

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

    if row_index is None:
        row = [""] * ncols
        for field, value in fields.items():
            if field in field_col:
                row[field_col[field] - 1] = "" if value is None else str(value)
        _retry(ws.append_row, row, value_input_option="USER_ENTERED")
        return "appended"

    cells = []
    for field, value in fields.items():
        if field not in field_col:
            continue
        a1 = gu.rowcol_to_a1(row_index, field_col[field])
        cells.append({"range": a1, "values": [["" if value is None else str(value)]]})
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
    ap.add_argument("--enrich", action="store_true",
                    help="phase 2: fill one game's analysis columns from report-state.json")
    ap.add_argument("--enrich-collection", action="store_true",
                    help="phase 2 for every analyzed JC game that already has a row "
                         "(for the report pipeline; skips games with no row yet)")
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
        return run_enrich_collection(args.dry_run)

    if args.enrich:
        if not args.game_id:
            ap.error("--game-id is required with --enrich")
        fields = enrichment_from_report_state(args.game_id)
        if fields is None:
            print(f"No analysis row for {args.game_id} in {REPORT_STATE_PATH} yet "
                  f"(game not analyzed / report not regenerated). Nothing to enrich.")
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
        return backfill_by_game_number(args.game_num, fields, args.dry_run)

    if args.dry_run:
        print("\n(dry run - sheet not touched)")
        return 0

    ws = open_worksheet()
    header = _retry(ws.row_values, 1)
    field_col = report_mapping(ws, fields)
    row_index = find_row_for_game(ws, field_col["game_id"], args.game_id)

    if args.enrich:
        if row_index is None:
            sys.exit(f"No existing row for {args.game_id}; run phase 1 (--gcg) first.")
        apply_fields(ws, field_col, len(header), row_index, fields)
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


def backfill_by_game_number(game_num, fields, dry_run):
    """Historical-archive backfill: the row for `game_num` already has hand-entered
    date/me/jc (and working W/L formulas) from years of manual tracking, just no
    game id yet. Locate it by 'Game #' (not by game id — there isn't one), verify
    the .gcg's final scores match what's already in the row (catches a mislabeled
    or misnumbered archive file before it corrupts the sheet), then write ONLY the
    game id. Refuses if the row already has one, or if scores don't match."""
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
            sys.exit(f"score mismatch at row {row_index} (Game #{game_num}): sheet "
                     f"{label}={existing_val} vs .gcg={fields[field]} — this file may be "
                     f"mislabeled/misnumbered. Not writing anything.")

    if dry_run:
        print(f"\n(dry run) would backfill game id into row {row_index} "
              f"(Game #{game_num}); scores match.")
        return 0

    apply_fields(ws, field_col, len(header), row_index, {"game_id": fields["game_id"]})
    print(f"\nbackfilled game id into row {row_index} (Game #{game_num}).")
    return 0


def run_enrich_collection(dry_run):
    """Enrich every analyzed JC game that already has a row in the sheet."""
    table = parse_report_table()
    if not table:
        print(f"No analyzed JC games in {REPORT_STATE_PATH} yet — nothing to enrich.")
        return 0
    print(f"[enrich-collection] {len(table)} analyzed game(s) in the report.")
    if dry_run:
        for gid, fields in table.items():
            print(f"  {gid}: {fields}")
        print("\n(dry run - sheet not touched)")
        return 0

    ws = open_worksheet()
    field_col = report_mapping(ws, next(iter(table.values())))
    ncols = len(_retry(ws.row_values, 1))
    enriched = skipped = 0
    for gid, fields in table.items():
        row_index = find_row_for_game(ws, field_col["game_id"], gid)
        if row_index is None:
            print(f"  {gid}: no row yet — skipped")
            skipped += 1
            continue
        apply_fields(ws, field_col, ncols, row_index, fields)
        print(f"  {gid}: enriched row {row_index}")
        enriched += 1
    print(f"\nenriched {enriched}, skipped {skipped} (no row yet).")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
