#!/usr/bin/env python3
"""List-or-exchange readout per game, from our own row band rather than the arena floor.

WHY THIS EXISTS. ops#139 recovered the five regular-season tier credits, so break-even
(credit / (1 - sellerFeeRate)) is computable for every game for the first time. The
market side was already collected. This joins them - and refuses the join that looks
obvious and is wrong.

THE COMPARISON THIS REFUSES TO MAKE. The daily collectors report the cheapest all-in
listing in the WHOLE ARENA. Comparing that to our break-even is apples to oranges: the
arena floor is an upper-bowl seat and ours is lower bowl, so the gap it shows is mostly
the bowl gap. ops#147 nearly shipped on that comparison. Worse, a SECTION-level comp is
still not enough - ops#113 measured that our section splits into two price bands at a row
boundary, priced about 1.93x apart. Averaging our section would compare our seats against
rows that are not comparable to them.

So the comp here is the price of the band CONTAINING OUR ROW, and the script verifies
that identification from row-level data rather than assuming it.

THE FEE CHAIN, AND THE ONE THING NOBODY MEASURED. The member map quotes PRIMARY
(team-sold) inventory. To compare it against a resale list price of ours, it has to be
carried through two different fee rates that economics.json is explicit must not be
blended: primaryBuyerFeeRate (0.211) on their side, buyerFeeRate (0.21) on ours.

But `priceIncludesFees` is **null on all 150 rows** - nobody recorded whether the member
map quotes all-in or pre-fee. That is a real unknown and it moves the comp by ~21%, so
this reports BOTH readings rather than picking one. Where the verdict is the same under
both, it says so, and that verdict is robust to the unknown. Where they disagree, the
game is reported as UNDECIDED - which is the honest output, not a failure.

WHAT THE VERDICT ACTUALLY MEANS - and this is the part most easily misread. A comp below
break-even does NOT mean "exchange now". Listing is free optionality while the exchange
window is open: if a listing does not sell, the credit is still there until T-48h. So
EV(list at P) = credit + p * (net(P) - credit), which is >= credit for any P above
break-even and any probability p. The action is therefore almost always "list above
break-even and let the credit be the floor". What the comp changes is how likely a sale
is - the EXPECTATION, not the ACTION.

The decision this readout genuinely informs is where to set the ask, and the risk it
cannot cover is forgetting to exchange before T-48h, after which an unsold ticket is
worth $0 rather than the credit. That is ops#63/ops#148, not this file.

PRIVACY. This script is in the PUBLIC repo and every input that could identify our seats
is read at runtime from the private snapshots store via --snapshots: the tier credits and
our section and row. None is hardcoded, and --out refuses any path inside this repo.

Usage:
    python3 scripts/exchange_readout.py --snapshots ../ops/data/profile/snapshots.jsonl
    python3 scripts/exchange_readout.py --snapshots <path> --out ../ops/data/profile/readout.json
    python3 scripts/exchange_readout.py --self-test
"""

import argparse
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
TIERS = ROOT / "config" / "tiers.json"
ECON = ROOT / "config" / "economics.json"
PRICES = ROOT / "data" / "primary" / "prices.jsonl"
ROW_BANDS = ROOT / "data" / "primary" / "row_bands.json"
SCHEDULE = ROOT / "data" / "schedule.json"


class Missing(Exception):
    """A required private input is absent. Never substituted with a guess."""


def econ() -> dict:
    d = json.loads(ECON.read_text())
    tm = d["resale"]["platforms"]["ticketmaster"]
    return {
        "sellerFeeRate": tm["sellerFeeRate"],
        "buyerFeeRate": tm["buyerFeeRate"]["value"],
        "primaryBuyerFeeRate": tm["primaryBuyerFeeRate"]["value"],
        "primaryBuyerFeeConfidence": tm["primaryBuyerFeeRate"]["confidence"],
    }


def profile(snapshots: pathlib.Path) -> dict:
    """Credits and seat identity from the PRIVATE store. Later lines supersede earlier."""
    credits: dict[str, float] = {}
    seats: dict = {}
    for raw in snapshots.read_text().splitlines():
        if not raw.strip():
            continue
        d = json.loads(raw)
        for tier, v in (d.get("tierCredits") or {}).items():
            if isinstance(v, dict) and isinstance(v.get("perSeat"), (int, float)):
                credits[tier] = float(v["perSeat"])
        if isinstance(d.get("seats"), dict) and d["seats"].get("section") is not None:
            seats = d["seats"]
    if not credits:
        raise Missing("no tier credits in the snapshots store")
    if not seats:
        raise Missing("no seat identity in the snapshots store")
    return {"credits": credits, "seats": seats}


def tiers_by_date() -> dict[str, str]:
    return {g["date"]: g["tier"] for g in json.loads(TIERS.read_text())["games"]}


def observations(section: int) -> list[dict]:
    out = []
    for line in PRICES.read_text().splitlines():
        if line.strip():
            r = json.loads(line)
            if r["section"] == section:
                out.append(r)
    return out


def boundary_for(section: int) -> dict | None:
    """The ops#113 row boundary for a section, if one was resolved."""
    if not ROW_BANDS.exists():
        return None
    doc = json.loads(ROW_BANDS.read_text())
    e = (doc.get("sections") or {}).get(str(section)) or {}
    b = e.get("boundary")
    return {**b, "confidence": e.get("confidence"), "games": e.get("games", [])} if b else None


def our_band_price(obs: dict, row: int, boundary: dict | None = None) -> tuple[float | None, str]:
    """Price of the band containing our row, and how it was established.

    Three cases, deliberately distinguished rather than collapsed:

    - row-level data covers our row            -> exact, the level our row sits in
    - row-level data exists and puts our row in the CHEAPEST level -> fromPrice is that
      level's price, so fromPrice is our band and is used with that stated
    - no row-level data                        -> fromPrice is the cheapest band in the
      section, which is only OUR band if our row is known to be in it from another game
    """
    rows = obs.get("rows") or {}
    if rows:
        by = {int(k): float(v) for k, v in rows.items()}
        if row in by:
            return by[row], "exact - our row was observed directly"
        levels = sorted(set(by.values()))
        cheapest = levels[0]
        cheap_rows = sorted(k for k, v in by.items() if v == cheapest)
        if cheap_rows and row > max(
                [k for k, v in by.items() if v > cheapest] or [-1]):
            return cheapest, (f"our row is beyond the last row of every dearer level "
                              f"(highest dearer row observed: "
                              f"{max([k for k, v in by.items() if v > cheapest], default=None)}), "
                              f"so it sits in the cheapest band")
        return None, "row-level data does not place our row"

    # No row-level data for THIS game. ops#113 measured that the band boundary is a
    # property of the seating map, not of the price level - it held in the same place
    # across games whose prices differed by 32%. So a boundary measured in other games
    # transfers, and when it puts our row past the last dearer row, the section's
    # fromPrice IS our band's price. Labelled as inferred, never as observed, and only
    # accepted when the boundary itself is `measured` rather than single-point.
    if boundary and boundary.get("confidence") == "measured" and row > boundary["after"]:
        fp = obs.get("fromPrice")
        if isinstance(fp, (int, float)):
            return float(fp), (f"INFERRED - no row data this game, but the ops#113 "
                               f"boundary (after row {boundary['after']}, measured "
                               f"across {len(boundary['games'])} games) puts our row in "
                               f"the cheapest band, whose price is fromPrice")
    return None, "no row-level data for this game"


def comparable_list_prices(primary_price: float, e: dict) -> dict[str, float]:
    """What WE would have to list at to match that primary seat, under both readings.

    Both readings carry the primary price to an all-in buyer price, then invert OUR
    resale buyer fee to get the equivalent list. The two rates are close, which is why
    the 'excludesFees' reading lands near the raw number - that is arithmetic, not a bug.
    """
    return {
        # The member map already shows what a buyer pays.
        "includesFees": primary_price / (1 + e["buyerFeeRate"]),
        # The member map shows a pre-fee price; a buyer pays more than it says.
        "excludesFees": primary_price * (1 + e["primaryBuyerFeeRate"]) / (1 + e["buyerFeeRate"]),
    }


def build(prof: dict, e: dict) -> list[dict]:
    section = int(prof["seats"]["section"])
    row = int(prof["seats"]["row"])
    by_date = tiers_by_date()
    sched = json.loads(SCHEDULE.read_text())
    date_of: dict[int, str] = {}

    def walk(o):
        if isinstance(o, dict):
            gid = o.get("gameId") or o.get("id")
            d = o.get("date") or o.get("gameDate")
            if isinstance(gid, int) and isinstance(d, str):
                date_of[gid] = d[:10]
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(sched)

    boundary = boundary_for(section)
    out = []
    for obs in observations(section):
        date = date_of.get(obs["gameId"], "?")
        tier = by_date.get(date, "PRESEASON")
        credit = prof["credits"].get(tier)
        price, how = our_band_price(obs, row, boundary)
        entry = {
            "gameId": obs["gameId"], "date": date, "tier": tier,
            "observedDate": obs.get("observedDate"),
            "ourBandPrice": price, "ourBandBasis": how,
            "credit": credit,
            "breakEven": round(credit / (1 - e["sellerFeeRate"]), 2) if credit else None,
        }
        if price is not None and credit:
            cmp_ = comparable_list_prices(price, e)
            entry["comparableList"] = {k: round(v, 2) for k, v in cmp_.items()}
            be = entry["breakEven"]
            verdicts = {k: ("above" if v > be else "below") for k, v in cmp_.items()}
            entry["verdicts"] = verdicts
            entry["robust"] = len(set(verdicts.values())) == 1
            entry["verdict"] = (list(verdicts.values())[0] if entry["robust"]
                                else "UNDECIDED")
        else:
            entry["verdict"] = "no comp"
            entry["robust"] = False
        out.append(entry)
    return sorted(out, key=lambda r: r["date"])


def report(rows: list[dict], e: dict) -> None:
    print(f"seller fee {e['sellerFeeRate']:.0%}  |  resale buyer fee "
          f"{e['buyerFeeRate']:.1%}  |  primary buyer fee "
          f"{e['primaryBuyerFeeRate']:.1%} ({e['primaryBuyerFeeConfidence']})")
    print("\ncomp = the price of the band containing OUR ROW, not the section and not "
          "the arena floor\n")
    for r in rows:
        print(f"  {r['date']}  tier {r['tier']:<9} break-even "
              f"{('$%.2f' % r['breakEven']) if r['breakEven'] else 'n/a':>9}")
        if r["ourBandPrice"] is None:
            print(f"      no comp - {r['ourBandBasis']}")
            continue
        c = r["comparableList"]
        print(f"      our band on the member map: ${r['ourBandPrice']:.2f}  "
              f"({r['ourBandBasis']})")
        print(f"      equivalent list if that price INCLUDES fees: ${c['includesFees']:.2f}"
              f"  -> {r['verdicts']['includesFees']} break-even")
        print(f"      equivalent list if it EXCLUDES fees:         ${c['excludesFees']:.2f}"
              f"  -> {r['verdicts']['excludesFees']} break-even")
        if r["robust"]:
            print(f"      => comp is {r['verdict'].upper()} break-even under BOTH "
                  f"readings - robust to the unrecorded fee basis")
        else:
            print("      => UNDECIDED: the two readings disagree, so the unrecorded "
                  "fee basis decides it. Do not act on this row.")
    print("\nWHAT TO DO WITH THIS. A comp below break-even is NOT an instruction to "
          "exchange.\nListing is free while the exchange window is open - an unsold "
          "listing still falls back\nto the credit until T-48h, so EV(list) >= credit "
          "for any ask above break-even.\nThe comp changes how LIKELY a sale is, not "
          "what to do. The action is: list above\nbreak-even, let the credit be the "
          "floor, and exchange before T-48h if unsold.\nThe expensive failure is "
          "forgetting that deadline - see ops#63/ops#148 - not mispricing.")


def run(args) -> int:
    e = econ()
    try:
        prof = profile(pathlib.Path(args.snapshots))
    except (Missing, FileNotFoundError) as ex:
        print(f"REFUSED: {ex}. Tier credits and seat identity are private and are read "
              f"from the ops store; they are deliberately not in this repo.",
              file=sys.stderr)
        return 1
    rows = build(prof, e)
    if not rows:
        print("no member-map observations for our section", file=sys.stderr)
        return 1
    report(rows, e)

    if args.out:
        dest = pathlib.Path(args.out).resolve()
        if dest == ROOT or ROOT in dest.parents:
            print(f"REFUSED: {dest} is inside the PUBLIC repo. This readout names our "
                  f"section, our row and our break-evens.", file=sys.stderr)
            return 1
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(json.dumps(
            {"_what": "Per-game list-or-exchange readout. PRIVATE - names our seats "
                      "and our break-evens.",
             "_method": "comp is the member-map price of the band containing our row "
                        "(ops#113), carried through the primary and resale buyer fees, "
                        "reported under both readings of the unrecorded priceIncludesFees.",
             "_action": "A comp below break-even is not an instruction to exchange. "
                        "Listing is free optionality until T-48h.",
             "fees": e, "games": rows}, indent=2, sort_keys=True) + "\n")
        print(f"\nwrote {dest}")
    return 0


# --------------------------------------------------------------------------- tests

def self_test() -> int:
    failures = []

    def check(name, cond, detail=""):
        if not cond:
            failures.append(f"{name}: {detail}")

    e = {"sellerFeeRate": 0.10, "buyerFeeRate": 0.21,
         "primaryBuyerFeeRate": 0.211, "primaryBuyerFeeConfidence": "x"}

    # ABSURD values - never a plausible price (ops#29).
    rows = {"5": 99999999.0, "16": 99999999.0, "17": 11111111.0, "22": 11111111.0}
    p, how = our_band_price({"rows": rows}, 22)
    check("our row read directly when observed", p == 11111111.0 and "exact" in how,
          f"{p} {how}")

    # Our row not observed, but beyond every dearer row -> cheapest band.
    p, how = our_band_price({"rows": {"5": 99999999.0, "16": 99999999.0,
                                      "17": 11111111.0}}, 22)
    check("unobserved row beyond the dearer level falls in the cheapest band",
          p == 11111111.0 and "cheapest band" in how, f"{p} {how}")

    # Our row sits INSIDE the dearer range but was not observed -> refuse.
    p, how = our_band_price({"rows": {"5": 99999999.0, "16": 99999999.0,
                                      "17": 11111111.0}}, 10)
    check("an unobserved row inside a dearer level is refused, not guessed",
          p is None, f"{p} {how}")

    p, how = our_band_price({"fromPrice": 1.0}, 22)
    check("no row data yields no comp without a boundary", p is None, how)

    meas = {"after": 16, "before": 17, "confidence": "measured", "games": [1, 2]}
    p, how = our_band_price({"fromPrice": 22222222.0}, 22, meas)
    check("a MEASURED boundary transfers to a game with no row data",
          p == 22222222.0 and "INFERRED" in how, f"{p} {how}")
    p, how = our_band_price({"fromPrice": 22222222.0}, 10, meas)
    check("the transfer is refused when our row is not past the boundary",
          p is None, f"{p} {how}")
    single = {**meas, "confidence": "measured_single_point"}
    p, how = our_band_price({"fromPrice": 22222222.0}, 22, single)
    check("a single-point boundary does NOT transfer", p is None, f"{p} {how}")

    c = comparable_list_prices(121.0, e)
    check("includesFees reading divides out our buyer fee",
          abs(c["includesFees"] - 100.0) < 1e-6, str(c))
    check("excludesFees reading is the higher of the two",
          c["excludesFees"] > c["includesFees"], str(c))

    # Real config must still parse and carry the three distinct rates.
    real = econ()
    check("seller fee is 10%", real["sellerFeeRate"] == 0.10, str(real))
    check("primary and resale buyer fees are NOT blended",
          real["buyerFeeRate"] != real["primaryBuyerFeeRate"], str(real))

    # Verdict robustness logic, on a break-even between the two readings.
    be = 110.0
    c2 = comparable_list_prices(121.0, e)          # ~100.00 and ~121.11
    v = {k: ("above" if x > be else "below") for k, x in c2.items()}
    check("a break-even between the two readings is UNDECIDED",
          len(set(v.values())) == 2, str(v))

    check("the store still holds member-map rows", PRICES.exists())

    if failures:
        for f in failures:
            print(f"FAIL {f}")
        return 1
    print("exchange_readout self-test: OK")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--snapshots", help="PRIVATE ops snapshots.jsonl - credits and seats")
    ap.add_argument("--out", help="write the readout here; refused inside this repo")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()
    if args.self_test:
        return self_test()
    if not args.snapshots:
        ap.error("--snapshots is required: credits and seat identity are private")
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
