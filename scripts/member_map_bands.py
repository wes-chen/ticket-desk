#!/usr/bin/env python3
"""Row-range band BOUNDARIES, derived from member-map row prices already in the store.

WHY THIS EXISTS. `config/price_bands.json` records a correction measured on 2026-09-05:
price bands are ROW RANGES WITHIN sections, not whole sections, and "the chart draws row
bands but prints no row numbers, so the boundaries are visible [as pixels] and not as
data". Row numbers are the one thing the official chart structurally cannot give.

The member map gives them, and it was captured a year before anything read it.
`data/primary/prices.jsonl` carries row-level prices for 9 section-games (ops#60,
ticket-desk@6295c24) and nothing consumed the `rows` field at all - `comps.py` and
`section_rank.py` both read only `fromPrice`. This reads the part that was sitting idle.

WHAT IT REFUSES TO DO, AND THIS IS THE POINT OF THE FILE. It does not name the bands.
`check_row_prices.py` identifies a band by matching a row's price against the published
prices in `config/price_bands.json`, which is correct for the paste IT grades - a per-seat
price lookup quoting published prices. Member-map prices are a DIFFERENT QUANTITY: a
per-game member/exchange price for one game. $96.50 is not $92 rounded; the two measure
different things, and a section's member price changes from game to game while its
published band average does not. Matching them would manufacture a band assignment out of
a unit mismatch - which is the "inferred treated as measured" failure ops#113 lists as a
non-goal. So this emits boundaries with `band: null` and says why.

WHAT A BOUNDARY IS HERE. Within one (game, section) the observed rows fall into price
levels. A boundary is the gap between the last row of a dearer level and the first row of
the next. If those two rows are adjacent the boundary is EXACT; if rows between them were
never observed the boundary is an INTERVAL, and it is reported as one rather than
collapsed to its lower edge. A boundary nobody observed the far side of is not a boundary.

Across games the intervals for a section are INTERSECTED, because each game is an
independent observation of the same physical seating map. Two games agreeing narrows the
answer and raises confidence; two games disagreeing is a contradiction and is reported,
never averaged.

MEASURED 2026-09-13 against the real store: section 110 puts its boundary between rows
16 and 17 in BOTH observed games, at prices that differ by 32% between those games - so
the boundary is a property of the seating map and not of the price level, which is the
whole reason it transfers. Section 101 independently lands after row 16 as well.

Usage:
    python3 scripts/member_map_bands.py
    python3 scripts/member_map_bands.py --out data/primary/row_bands.json
    python3 scripts/member_map_bands.py --self-test
"""

import argparse
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
STORE = ROOT / "data" / "primary" / "prices.jsonl"
OUT = ROOT / "data" / "primary" / "row_bands.json"


def load(store: pathlib.Path = STORE) -> list[dict]:
    """Member-map observations that actually carry row-level prices."""
    out = []
    for line in store.read_text().splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        if r.get("rows"):
            out.append(r)
    return out


def levels(rows: dict) -> list[tuple[float, list[int]]]:
    """Price levels within one section-game, dearest first, each with its observed rows.

    Row keys are strings in the store and must be compared as INTEGERS - "9" > "16" is
    true as strings, which would put row 9 outside a band that contains it and invent a
    boundary in the wrong place.
    """
    by: dict[float, list[int]] = {}
    for k, v in rows.items():
        by.setdefault(float(v), []).append(int(k))
    return [(p, sorted(rs)) for p, rs in sorted(by.items(), key=lambda kv: -kv[0])]


def boundaries(rows: dict) -> list[dict]:
    """Boundaries between adjacent price levels in one section-game.

    `after` is the last row known to be in the dearer level and `before` the first known
    to be in the cheaper one. The true boundary lies in (after, before]. When
    before == after + 1 that interval holds exactly one position and the boundary is
    exact.
    """
    lv = levels(rows)
    out = []
    for (hi_price, hi_rows), (lo_price, lo_rows) in zip(lv, lv[1:]):
        after, before = hi_rows[-1], lo_rows[0]
        if before <= after:
            # A cheaper row nearer the ice than a dearer one. Real data, wrong shape -
            # transposition while reading a long scrolling panel is the error
            # primary_store.py already refuses on, so it is reported, not repaired.
            out.append({"after": after, "before": before, "contradiction": True,
                        "dearerPrice": hi_price, "cheaperPrice": lo_price})
            continue
        out.append({
            "after": after,
            "before": before,
            "exact": before == after + 1,
            "dearerPrice": hi_price,
            "cheaperPrice": lo_price,
            "ratio": round(hi_price / lo_price, 3) if lo_price else None,
        })
    return out


def intersect(a: tuple[int, int], b: tuple[int, int]) -> tuple[int, int] | None:
    """Intersect two (after, before] intervals. None when they cannot both be true."""
    lo, hi = max(a[0], b[0]), min(a[1], b[1])
    return (lo, hi) if lo < hi else None


def combine(observations: list[dict]) -> dict:
    """Fold every observation into per-section boundary findings across games."""
    # Every OBSERVED section is seeded, including ones that yield no boundary at all.
    # Dropping those would make the output an inventory of successes rather than of
    # evidence, and ops#113 asks for the inventory - a section seen at a single price
    # level is a real observation that resolves nothing, which is worth saying out loud.
    by_sec: dict[int, list[dict]] = {o["section"]: [] for o in observations}
    for o in observations:
        for b in boundaries(o["rows"]):
            by_sec.setdefault(o["section"], []).append(
                {**b, "gameId": o["gameId"], "observedDate": o["observedDate"],
                 "rowsComplete": bool(o.get("rowsComplete"))})

    seen_games: dict[int, list[int]] = {}
    for o in observations:
        seen_games.setdefault(o["section"], []).append(o["gameId"])
    seen_games = {k: sorted(set(v)) for k, v in seen_games.items()}

    sections: dict[str, dict] = {}
    for sec, bs in sorted(by_sec.items()):
        good = [b for b in bs if not b.get("contradiction")]
        bad = [b for b in bs if b.get("contradiction")]
        entry: dict = {
            "observations": len(bs),
            "games": sorted({b["gameId"] for b in bs}) or seen_games.get(sec, []),
            "contradictions": bad,
        }
        if good:
            # One boundary per section is assumed ONLY where every observation yields
            # exactly one. A section showing two boundaries in one game (102 does) has
            # more structure than a single interval can express, so it is left listed
            # rather than forced into one answer.
            per_game: dict[int, list[dict]] = {}
            for b in good:
                per_game.setdefault(b["gameId"], []).append(b)
            single = all(len(v) == 1 for v in per_game.values())
            if single and len(per_game) >= 1:
                iv: tuple[int, int] | None = None
                agree = True
                for g, (b,) in sorted(per_game.items()):
                    cur = (b["after"], b["before"])
                    if iv is None:
                        iv = cur
                    else:
                        nxt = intersect(iv, cur)
                        if nxt is None:
                            agree = False
                            break
                        iv = nxt
                if agree and iv:
                    entry["boundary"] = {"after": iv[0], "before": iv[1],
                                         "exact": iv[1] == iv[0] + 1}
                    entry["confidence"] = ("measured" if len(per_game) >= 2
                                           else "measured_single_point")
                    entry["ratios"] = [b["ratio"] for bs2 in per_game.values()
                                       for b in bs2]
                else:
                    entry["confidence"] = "contradictory"
            else:
                entry["confidence"] = "multiple-boundaries"
            entry["perGame"] = {str(g): v for g, v in sorted(per_game.items())}
        else:
            entry["confidence"] = "unresolved"
        # Band IDENTITY is never asserted - see the module docstring.
        entry["band"] = None
        sections[str(sec)] = entry
    return sections


def report(sections: dict) -> None:
    for sec, e in sorted(sections.items(), key=lambda kv: int(kv[0])):
        b = e.get("boundary")
        if b:
            where = (f"between rows {b['after']} and {b['before']}" if b["exact"]
                     else f"after row {b['after']}, at or before row {b['before']}")
            ratios = ", ".join(f"{r:.2f}x" for r in e.get("ratios", []) if r)
            print(f"  section {sec}: boundary {where}  "
                  f"[{e['confidence']}, {len(e['games'])} game(s); {ratios}]")
        else:
            print(f"  section {sec}: no boundary derivable "
                  f"[{e['confidence']}, {len(e['games'])} game(s)]")
        for c in e.get("contradictions", []):
            print(f"      CONTRADICTION: row {c['before']} at ${c['cheaperPrice']:.2f} "
                  f"is nearer the ice than row {c['after']} at ${c['dearerPrice']:.2f}")


def run(out_path: pathlib.Path | None) -> int:
    obs = load()
    if not obs:
        print("no row-level observations in the store", file=sys.stderr)
        return 1
    sections = combine(obs)
    print(f"{len(obs)} section-game observation(s) carrying row prices, "
          f"{len(sections)} section(s)")
    report(sections)

    resolved = [s for s, e in sections.items() if e.get("boundary")]
    print(f"\n{len(resolved)} of {len(sections)} section(s) yield a boundary; "
          f"the rest stay unresolved rather than being filled in")
    print("band IDENTITY is deliberately null everywhere - member-map prices are a "
          "different quantity from the published band prices, see the docstring")

    if out_path:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps({
            "_comment": "Row-range band BOUNDARIES derived from member-map row prices "
                        "in data/primary/prices.jsonl by scripts/member_map_bands.py. "
                        "ops#113.",
            "_why": "config/price_bands.json records that bands are row ranges within "
                    "sections and that the official chart prints no row numbers. These "
                    "are those row numbers, where the captured data supports them.",
            "_bandIdentity": "NEVER asserted here. Member-map prices are per-game "
                             "member/exchange prices; the published band prices in "
                             "config/price_bands.json are full-season averages per "
                             "game. Matching the two would invent a band assignment out "
                             "of a unit mismatch.",
            "_interval": "A boundary lies in (after, before]. exact=true means those "
                         "rows are adjacent and the boundary is pinned; otherwise the "
                         "rows between were never observed and the interval is real.",
            "sections": sections,
        }, indent=2, sort_keys=True) + "\n")
        rel = out_path.relative_to(ROOT) if out_path.is_relative_to(ROOT) else out_path
        print(f"\nwrote {rel}")
    return 0


# --------------------------------------------------------------------------- tests

def self_test() -> int:
    failures = []

    def check(name, cond, detail=""):
        if not cond:
            failures.append(f"{name}: {detail}")

    # ---- unit behaviour, on shapes chosen to isolate one rule each
    lv = levels({"9": 10.0, "16": 10.0, "17": 5.0})
    check("levels sorts dearest first", [p for p, _ in lv] == [10.0, 5.0], str(lv))
    check("rows compare as integers", lv[0][1] == [9, 16], str(lv[0][1]))

    b = boundaries({"15": 10.0, "16": 10.0, "17": 5.0})
    check("adjacent rows give an exact boundary",
          b[0]["exact"] and (b[0]["after"], b[0]["before"]) == (16, 17), str(b))
    b = boundaries({"16": 10.0, "20": 5.0})
    check("a gap gives an interval, not a point",
          not b[0]["exact"] and (b[0]["after"], b[0]["before"]) == (16, 20), str(b))

    check("intersect narrows", intersect((16, 20), (16, 17)) == (16, 17))
    check("intersect refuses the impossible", intersect((16, 17), (20, 21)) is None)

    # Cheaper row nearer the ice = transposition, reported not repaired.
    b = boundaries({"5": 5.0, "20": 10.0})
    check("transposition is flagged", b[0].get("contradiction"), str(b))

    # ---- against the REAL store, not an invented fixture (ops#113 requires this)
    obs = load()
    check("store still carries row-level observations", len(obs) >= 9, f"got {len(obs)}")
    sections = combine(obs)

    s110 = sections.get("110", {})
    check("section 110 resolves a boundary", "boundary" in s110, str(s110.get("confidence")))
    if "boundary" in s110:
        check("110's boundary is between rows 16 and 17",
              (s110["boundary"]["after"], s110["boundary"]["before"]) == (16, 17),
              str(s110["boundary"]))
        check("110's boundary is exact", s110["boundary"]["exact"])
        check("110 is measured across two games",
              s110["confidence"] == "measured" and len(s110["games"]) == 2,
              f"{s110['confidence']} {s110['games']}")

    s101 = sections.get("101", {})
    check("section 101 resolves a boundary", "boundary" in s101, str(s101.get("confidence")))
    if "boundary" in s101:
        check("101's boundary starts after row 16", s101["boundary"]["after"] == 16,
              str(s101["boundary"]))
        check("101's boundary is an INTERVAL, not exact - row 17 was never observed",
              not s101["boundary"]["exact"], str(s101["boundary"]))

    # 102 shows three price levels in one game: two boundaries, so no single answer.
    s102 = sections.get("102", {})
    check("section 102 refuses to collapse two boundaries into one",
          s102.get("confidence") == "multiple-boundaries" and "boundary" not in s102,
          str(s102.get("confidence")))

    # Sections seen at a single price level yield nothing, and must say so.
    for sec in ("109", "128"):
        e = sections.get(sec, {})
        check(f"section {sec} stays unresolved",
              e.get("confidence") == "unresolved" and "boundary" not in e,
              f"{sec}: {e.get('confidence')}")

    check("band identity is null everywhere",
          all(e["band"] is None for e in sections.values()),
          "a band name was asserted somewhere")

    if failures:
        for f in failures:
            print(f"FAIL {f}")
        return 1
    print(f"member_map_bands self-test: OK ({len(sections)} sections from the real store)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", nargs="?", const=str(OUT),
                    help="write the derived boundaries here (default data/primary/row_bands.json)")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()
    if args.self_test:
        return self_test()
    return run(pathlib.Path(args.out) if args.out else None)


if __name__ == "__main__":
    sys.exit(main())
