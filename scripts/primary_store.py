#!/usr/bin/env python3
"""Store and VALIDATOR for Ticketmaster primary (member) seat prices. ops#23, ops#19.

WHAT THIS IS FOR. The four collectors in this repo read RESALE comps - what other
sellers ask on markets we do not sell through. This store holds something different and
better: the price the team itself charges for its own unsold inventory, per section and
per row, on the channel these tickets actually sell on. It is the only source found so
far that answers "what is a seat like ours worth on this game" rather than "what is the
cheapest seat anywhere in the building".

WHERE IT COMES FROM, and why there is no collector. The member seat map is behind a
login, and CLAUDE.md rule 3 is absolute: the collector never touches the authenticated
session, because that account holds the season tickets. So this arrives the sanctioned
way - Wesley reads the page himself, an agent reads the screenshots, and this file
refuses anything that does not hold together. ops#32 closed rather than build an OCR
step, on the grounds that an error-prone extractor directly upstream of a pricing
recommendation is worse than no extractor. The checks below are what replaces that
extractor's absence: transcription is done by eye, so every claim it makes is
cross-examined against something independent.

THE TWO SHAPES, and why they are stored together.

  section level  (section) -> from-price, availability, and whether higher levels exist
  row level      (section, row) -> the actual band price

`rowsComplete` marks a section whose Available Seats panel was scrolled to the end. Only
then does the from-price have to EQUAL the cheapest row; on a partial read it may sit
below everything seen, because the cheap band simply was not reached. Getting this wrong
is how a partial capture turns into a fabricated band floor.

The section-level "from" price is the MINIMUM of the available rows, not a band constant.
That distinction is load-bearing and was nearly missed: section 113 read $170 on one game
with 21 seats left and $49.50 on another with 28, which looks like a tier change and is
actually an availability artefact - only expensive rows remained unsold in the first case.
So a from-price is a market observation. A row price is a band observation. Conflating
them would produce a confident section->band map that is really a snapshot of what nobody
bought.

WHAT THE "+" MEANS, measured. The section list renders "$89.00 + each" for some sections
and "$89.00 each" for others. Confirmed with Wesley: the plus means higher price levels
exist in that section. That is worth more than it looks - it tells us which sections even
have internal variation, so row-level capture is only needed where the plus appears.
`hasHigherLevels` records it, and validate() cross-checks it against the rows: a section
flagged single-level that shows two prices is a transcription error, and so is the
reverse.

THE PUBLIC PAGE CARRIES THE CHART BAND NAME, and that is what closes ops#19. Each seat's
"Description" field on the public event page renders the Sharks365 chart's own legend
label - section 110 reads "Club 4 - Club Access" at row 16 and "Lower 4" at rows 21 and
23. So the (section, row) -> band map is READABLE DIRECTLY, per seat, in text, from the
channel these tickets sell on. That is the thing extract_price_bands.py has been trying to
infer from a JPEG at 83% mirror agreement, available authoritatively for the asking.

Two price systems appear on the same seats and must not be mixed. The member map prices
the Lower 4 band in section 110 at $49.50; the public page prices the same band at $82.35
all-in as a "Standard Ticket" - a 66% spread. Both are the team selling. Which one is the
right comparison depends on the question: a member deciding whether to buy more seats
faces $49.50, while a resale buyer choosing between our listing and everything else faces
$82.35. Comparing our resale ask against the MEMBER price understates our competitiveness;
comparing it against the public price is the honest test.

INVENTORY CLASS IS NOT A PRICE BAND. The hover tooltip shows a label like "Teal Plus SY"
(season year). It appears on BOTH price levels within one section, and across sections, so
it identifies which package the seat belongs to and says nothing about the band. An
earlier reading of this file's data assumed it was the band name; it is not. The band is
carried by the PRICE, and separately by the resale page's "Description" field, which does
render chart legend names like "Lower 4".

Usage:
    python3 scripts/primary_store.py --ingest FILE.json [--store data/primary/prices.jsonl]
    python3 scripts/primary_store.py --self-test
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
DEFAULT_STORE = ROOT / "data" / "primary" / "prices.jsonl"
SCHEDULE = ROOT / "data" / "schedule.json"

# The 50 seating sections at SAP Center, as an INDEPENDENT check on transcription.
#
# Derived twice and cross-checked: the price-band chart's label discs give 22 lower + 28
# upper, and the section list transcribed from Ticketmaster gives the same 50 with the
# same gaps. The lower bowl skips 105, 108, 111, 119, 122 and 125 - those numbers do not
# exist. A transcription that invents one is caught here rather than becoming a comp.
LOWER = (101, 102, 103, 104, 106, 107, 109, 110, 112, 113, 114, 115, 116, 117, 118,
         120, 121, 123, 124, 126, 127, 128)
UPPER = tuple(range(201, 229))
SECTIONS = frozenset(LOWER + UPPER)

# A price outside this range is a misread, not a bargain. The observed span across two
# games is $14.00 (upper corner, preseason) to $354.00 (lower centre, regular season).
# Generous on both sides so a real outlier reports rather than silently passes.
MIN_PRICE, MAX_PRICE = 5.0, 2000.0


class Invalid(Exception):
    """A row that failed validation. Carries every problem, not just the first."""


def known_game_ids() -> set[int]:
    if not SCHEDULE.exists():
        return set()
    d = json.loads(SCHEDULE.read_text())
    games = d["games"] if isinstance(d, dict) else d
    return {int(g["gameId"]) for g in games}


def validate(rows, game_ids=None):
    """Every problem with these rows, as a list of strings. Empty means clean.

    REFUSES RATHER THAN TRUSTS, in the same spirit as fetch_schedule.py. Each check
    below exists because the corresponding mistake is one a careful person makes while
    reading forty screenshots, and each one would survive into a price recommendation
    looking exactly like data.
    """
    problems: list[str] = []
    seen: set[tuple] = set()

    for i, r in enumerate(rows):
        where = f"row {i}"
        for field in ("observedDate", "gameId", "section", "fromPrice", "available"):
            if field not in r:
                problems.append(f"{where}: missing '{field}'")
        if problems and any(p.startswith(f"{where}: missing") for p in problems):
            continue

        sec, gid = r["section"], r["gameId"]
        where = f"{where} (game {gid} sec {sec})"

        if sec not in SECTIONS:
            problems.append(f"{where}: section {sec} is not one of the 50 real sections")
        if game_ids is not None and int(gid) not in game_ids:
            problems.append(f"{where}: gameId not in data/schedule.json")

        key = (r["observedDate"], gid, sec)
        if key in seen:
            problems.append(f"{where}: duplicate (observedDate, gameId, section)")
        seen.add(key)

        fp = r["fromPrice"]
        if not isinstance(fp, (int, float)) or isinstance(fp, bool):
            problems.append(f"{where}: fromPrice is not a number")
            continue
        if not MIN_PRICE <= fp <= MAX_PRICE:
            problems.append(f"{where}: fromPrice {fp} outside ${MIN_PRICE}-${MAX_PRICE}")
        if not isinstance(r["available"], int) or r["available"] < 0:
            problems.append(f"{where}: available must be a non-negative int")

        band_rows = r.get("rows") or {}
        if not band_rows:
            continue

        prices, nums = {}, []
        for k, v in band_rows.items():
            try:
                n = int(k)
            except (TypeError, ValueError):
                problems.append(f"{where}: row key {k!r} is not a row number")
                continue
            if not isinstance(v, (int, float)) or isinstance(v, bool):
                problems.append(f"{where}: row {n} price is not a number")
                continue
            if not MIN_PRICE <= v <= MAX_PRICE:
                problems.append(f"{where}: row {n} price {v} outside sane range")
            prices[n] = float(v)
            nums.append(n)

        if not prices:
            continue

        # 1. The from-price and the rows must be consistent - but HOW consistent depends
        #    on whether the panel was scrolled to the end.
        #
        #    This check originally demanded equality outright, and the first real ingest
        #    refused two sections because of it. That was the RULE being wrong, not the
        #    data: in sections 112 and 128 only expensive-band rows had been read, so the
        #    section's cheaper from-price was legitimately below everything observed.
        #    Demanding equality on a partial read would have forced either a false
        #    `rowsComplete` or the quiet deletion of real rows.
        #
        #    So: a from-price ABOVE the cheapest observed row is always an error - the
        #    section list cannot claim a floor higher than a seat actually on sale.
        #    Equality is required only when `rowsComplete` says the panel was exhausted,
        #    and that is where the cross-check has teeth.
        lo = min(prices.values())
        if float(fp) > lo + 0.005:
            problems.append(
                f"{where}: fromPrice {fp} is ABOVE the cheapest observed row {lo} - "
                f"impossible, so one of the two was misread")
        elif r.get("rowsComplete") and abs(lo - float(fp)) > 0.005:
            problems.append(
                f"{where}: rowsComplete but fromPrice {fp} != min row price {lo} - "
                f"the section list and a fully-scrolled row list disagree")

        # 2. Price never RISES as the row number rises. Further from the ice is not more
        #    expensive. A violation means rows were transposed while reading, which is
        #    the single easiest error to make in a long scrolling panel.
        for a, b in zip(sorted(nums), sorted(nums)[1:]):
            if prices[b] > prices[a] + 0.005:
                problems.append(
                    f"{where}: row {b} (${prices[b]}) costs more than row {a} "
                    f"(${prices[a]}) - rows look transposed")

        # 3. The "+" and the rows must tell the same story. This is the one check that
        #    validates a UI affordance against the data behind it, so a change in how
        #    Ticketmaster renders that plus shows up as a contradiction rather than as
        #    a silently wrong single-level section.
        levels = len(set(prices.values()))
        higher = r.get("hasHigherLevels")
        if higher is False and levels > 1:
            problems.append(
                f"{where}: no '+' in the section list but {levels} price levels in rows")
        if higher is True and levels == 1 and len(prices) > 3:
            problems.append(
                f"{where}: '+' in the section list but only one price across "
                f"{len(prices)} rows - a higher band was missed")

    return problems


def merge(existing, new):
    """Upsert on (observedDate, gameId, section), then sort deterministically.

    Same semantics as market_store.merge and for the same reasons: a same-day re-read
    CORRECTS rather than appends, so fixing a misread does not leave both versions in
    the store, and the sort keeps a daily commit diffing cleanly.
    """
    keyed = {(r["observedDate"], r["gameId"], r["section"]): r for r in existing}
    for r in new:
        keyed[(r["observedDate"], r["gameId"], r["section"])] = r
    return sorted(keyed.values(),
                  key=lambda r: (r["observedDate"], r["gameId"], r["section"]))


def read_store(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def write_store(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in rows))


def self_test() -> int:
    fails = []

    def check(label, got, want):
        if got != want:
            fails.append(f"{label}: got {got!r}, want {want!r}")

    base = {"observedDate": "2026-09-07", "gameId": 11111111, "section": 110,
            "fromPrice": 49.50, "available": 88}
    ids = {11111111}

    check("a clean section-level row passes", validate([base], ids), [])
    check("an invented section is refused",
          len(validate([{**base, "section": 105}], ids)), 1)
    check("an unknown gameId is refused",
          len(validate([{**base, "gameId": 22222222}], ids)), 1)
    check("a duplicate section is refused",
          len(validate([base, dict(base)], ids)), 1)
    check("a missing field is refused",
          len(validate([{k: v for k, v in base.items() if k != "fromPrice"}], ids)), 1)

    # The from-price must equal the minimum observed row. This is the cross-check that
    # makes the two shapes verify each other rather than sit side by side.
    ok = {**base, "rows": {"16": 96.50, "17": 49.50, "21": 49.50},
          "hasHigherLevels": True}
    check("rows consistent with the from-price pass", validate([ok], ids), [])
    check("a from-price ABOVE the cheapest row is refused - impossible",
          len(validate([{**ok, "fromPrice": 96.50}], ids)), 1)
    check("a from-price BELOW every observed row is fine on a partial read",
          validate([{**base, "fromPrice": 20.0, "hasHigherLevels": True,
                     "rows": {"5": 96.50}}], ids), [])
    check("but not once rowsComplete is claimed",
          len(validate([{**base, "fromPrice": 20.0, "rowsComplete": True,
                         "hasHigherLevels": True, "rows": {"5": 96.50}}], ids)), 1)
    check("a fully-scrolled section whose floor matches passes",
          validate([{**ok, "rowsComplete": True}], ids), [])

    # Transposition: further from the ice must not cost more.
    check("a row priced above a nearer row is refused",
          len(validate([{**base, "rows": {"5": 49.50, "20": 96.50},
                         "fromPrice": 49.50, "hasHigherLevels": True}], ids)), 1)

    # The "+" affordance must agree with the rows, in both directions.
    check("no '+' but two levels is refused",
          len(validate([{**ok, "hasHigherLevels": False}], ids)), 1)
    check("'+' but one level across many rows is refused",
          len(validate([{**base, "hasHigherLevels": True, "fromPrice": 49.50,
                         "rows": {"17": 49.5, "18": 49.5, "19": 49.5, "20": 49.5}}],
                       ids)), 1)
    check("'+' with one level across few rows is allowed - the band may be unobserved",
          validate([{**base, "hasHigherLevels": True, "fromPrice": 49.50,
                     "rows": {"17": 49.5}}], ids), [])

    check("an absurd price is refused",
          len(validate([{**base, "fromPrice": 99999.0}], ids)), 1)
    check("a negative availability is refused",
          len(validate([{**base, "available": -1}], ids)), 1)
    check("a non-numeric row price is refused",
          len(validate([{**base, "rows": {"17": "49.50"}}], ids)), 1)

    # Upsert, not append - a corrected re-read must replace.
    a = dict(base)
    b = {**base, "fromPrice": 52.0}
    merged = merge([a], [b])
    check("a same-day re-read replaces rather than appends", len(merged), 1)
    check("and the newer value wins", merged[0]["fromPrice"], 52.0)
    check("a different section is a separate row",
          len(merge([a], [{**base, "section": 109}])), 2)
    check("the sort is deterministic",
          [r["section"] for r in merge([{**base, "section": 128}], [a])], [110, 128])

    print(f"self-test: {'passed' if not fails else 'FAILED'} ({len(fails)} failure(s))")
    for f in fails:
        print(f"  FAIL {f}")
    return 1 if fails else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ingest", type=pathlib.Path,
                    help="JSON file: a list of rows, or {\"rows\": [...]}")
    ap.add_argument("--store", type=pathlib.Path, default=DEFAULT_STORE)
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()

    if args.self_test:
        return self_test()
    if not args.ingest:
        print("--ingest is required", file=sys.stderr)
        return 2

    payload = json.loads(args.ingest.read_text())
    new = payload["rows"] if isinstance(payload, dict) else payload

    problems = validate(new, known_game_ids() or None)
    if problems:
        # Writes NOTHING on any problem. A partially-ingested batch is worse than a
        # rejected one: the store would look populated while missing exactly the rows
        # that needed a second look.
        print(f"REFUSING to ingest {len(new)} row(s) - {len(problems)} problem(s):",
              file=sys.stderr)
        for p in problems:
            print(f"  {p}", file=sys.stderr)
        return 1

    merged = merge(read_store(args.store), new)
    write_store(args.store, merged)
    games = sorted({r["gameId"] for r in merged})
    days = sorted({r["observedDate"] for r in merged})
    print(f"ingested {len(new)} row(s); store now {len(merged)} row(s) "
          f"across {len(games)} game(s), {len(days)} day(s)")
    with_rows = sum(1 for r in merged if r.get("rows"))
    print(f"  {with_rows} row(s) carry row-level band prices")
    return 0


if __name__ == "__main__":
    sys.exit(main())
