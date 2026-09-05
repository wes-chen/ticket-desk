#!/usr/bin/env python3
"""Validate config/price_bands.json - the section -> price band map (ops#19).

Same discipline as scripts/fetch_schedule.py, for the same reason. The tier table was
transcribed by hand from a graphic and needed a check that would fail loudly on a single
misread row. The band map is transcribed from a *colour-coded seating chart*, which is
harder to read correctly by eye, not easier. So it gets a validator before it gets data.

Two groups of checks:

  PRICE checks run now, on the transcribed band prices. They catch transposed digits and
  swapped rows - the errors that produce a plausible-looking wrong number.

  PLACEMENT checks need the section assignment and the ring order, neither of which is
  transcribed yet. They are REPORTED AS SKIPPED, loudly. A check that quietly passes on
  absent data is worse than no check: it manufactures confidence. This is the same trap
  check_privacy.py fell into with its history pass.

The placement checks are written now, and self-tested against a deliberately misfiled
section, so the moment the chart is transcribed the check is already known to work.

Usage:
    python3 scripts/check_price_bands.py
    python3 scripts/check_price_bands.py --self-test
"""

import argparse
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config" / "price_bands.json"


# ---------------------------------------------------------------- price checks

def check_prices(cfg: dict) -> list[str]:
    problems = []
    bands = cfg["bands"]

    seen = set()
    for b in bands:
        for field in ("id", "label", "family", "rank", "avgPerGame", "ring", "rowScope", "sections"):
            if field not in b:
                problems.append(f"band {b.get('id', '?')}: missing field '{field}'")
        bid = b.get("id")
        if bid in seen:
            problems.append(f"duplicate band id {bid!r}")
        seen.add(bid)

        avg = b.get("avgPerGame") or {}
        if not avg:
            problems.append(f"band {bid}: avgPerGame is empty - a band with no published price is not a band")
        for k, v in avg.items():
            if k not in ("renew", "new"):
                problems.append(f"band {bid}: unexpected avgPerGame key {k!r}")
            elif not isinstance(v, (int, float)) or v <= 0:
                problems.append(f"band {bid}: avgPerGame.{k} is {v!r}, expected a positive number")

        # The renewal rate is a discount off the new-buyer rate. If renew > new the two
        # columns were read the wrong way round - which is invisible in any single band
        # and obvious across all of them.
        if "renew" in avg and "new" in avg and avg["renew"] > avg["new"]:
            problems.append(
                f"band {bid}: renew {avg['renew']} > new {avg['new']} - renew/new columns look transposed"
            )

    # Within a family, rank 1 is the best seat and must be the most expensive. A pair
    # read off adjacent chart rows in the wrong order shows up here and nowhere else.
    by_family: dict[str, list] = {}
    for b in bands:
        by_family.setdefault(b.get("family", "?"), []).append(b)

    for fam, members in sorted(by_family.items()):
        ranks = [b.get("rank") for b in members]
        if sorted(ranks) != list(range(1, len(members) + 1)):
            problems.append(f"family {fam}: ranks {sorted(ranks)} are not 1..{len(members)}")
        for series in ("renew", "new"):
            priced = [b for b in sorted(members, key=lambda x: x.get("rank", 0))
                      if series in (b.get("avgPerGame") or {})]
            for a, c in zip(priced, priced[1:]):
                if a["avgPerGame"][series] <= c["avgPerGame"][series]:
                    problems.append(
                        f"family {fam} ({series}): {a['id']} rank {a['rank']} at "
                        f"{a['avgPerGame'][series]} is not above {c['id']} rank {c['rank']} at "
                        f"{c['avgPerGame'][series]} - ranks or prices are out of order"
                    )
    return problems


# ------------------------------------------------------------ placement checks

def arcs(ring: list[str], members: set[str]) -> list[list[str]]:
    """Split a band's sections into contiguous runs around a CIRCULAR ring.

    Circular matters: a band spanning the seam between the last and first section of the
    ring is one arc, not two, and treating it as two would report a false error on
    correctly transcribed data.
    """
    idx = [i for i, s in enumerate(ring) if s in members]
    if not idx:
        return []
    if len(idx) == len(ring):
        return [list(ring)]
    runs, cur = [], [idx[0]]
    for prev, i in zip(idx, idx[1:]):
        if i == prev + 1:
            cur.append(i)
        else:
            runs.append(cur)
            cur = [i]
    runs.append(cur)
    # Stitch a run ending at the ring's last slot onto one starting at slot 0.
    if len(runs) > 1 and runs[0][0] == 0 and runs[-1][-1] == len(ring) - 1:
        runs[0] = runs[-1] + runs[0]
        runs.pop()
    return [[ring[i] for i in r] for r in runs]


def check_placement(cfg: dict) -> tuple[list[str], list[str]]:
    """Returns (problems, skipped). A skip is a reported gap, never a silent pass."""
    problems, skipped = [], []
    bands = cfg["bands"]
    rings = cfg.get("rings") or {}

    assigned = [b for b in bands if b.get("sections")]
    if not assigned:
        skipped.append(
            f"section assignment: all {len(bands)} bands have empty `sections` - "
            "nothing to place. Transcribe the chart's colour coding (ops#19)."
        )
        return problems, skipped

    # A section may appear once per rowScope group, not once overall: Promenade Row 1 is
    # a band *inside* promenade sections rather than a set of its own.
    owner: dict[tuple[str, str], str] = {}
    for b in assigned:
        scope = (b.get("ring", ""), b.get("rowScope", "all"))
        for sec in b["sections"]:
            key = (scope, sec)
            if key in owner:
                problems.append(
                    f"section {sec} is in both {owner[key]} and {b['id']} "
                    f"(ring {scope[0]!r}, rows {scope[1]!r}) - a section belongs to one band"
                )
            else:
                owner[key] = b["id"]

    for b in assigned:
        ring_name = b.get("ring")
        if not ring_name:
            problems.append(f"band {b['id']} has sections but no ring - cannot check placement")
            continue
        ring = rings.get(ring_name)
        if not ring:
            skipped.append(f"ring {ring_name!r}: not transcribed, so {b['id']} placement is unchecked")
            continue

        unknown = [s for s in b["sections"] if s not in ring]
        if unknown:
            problems.append(f"band {b['id']}: sections not on ring {ring_name!r}: {unknown}")
            continue

        got = arcs(ring, set(b["sections"]))
        want = b.get("expectedArcs")
        if want is not None and len(got) != want:
            problems.append(
                f"band {b['id']}: sections form {len(got)} contiguous arc(s), expected {want} "
                f"- {[a for a in got]}. A band should occupy whole arcs; a stray section "
                f"is the signature of a misread colour."
            )
        # The arena is symmetric, so a two-arc band's halves must match in size. This is
        # what catches a section taken from one side and given to the neighbouring band:
        # coverage still passes, arc count still passes, lengths do not.
        elif want == 2 and len(got) == 2 and len(got[0]) != len(got[1]):
            problems.append(
                f"band {b['id']}: arcs are asymmetric, {len(got[0])} vs {len(got[1])} "
                f"sections ({got[0]} / {got[1]}) - expected mirror halves"
            )

    # Every section on a transcribed ring must be claimed by some band at the 'all' scope.
    for ring_name, ring in rings.items():
        claimed = {s for (scope, s) in owner if scope == (ring_name, "all")}
        orphans = [s for s in ring if s not in claimed]
        if orphans:
            problems.append(f"ring {ring_name!r}: sections on the chart with no band: {orphans}")

    return problems, skipped


# ------------------------------------------------------------------- reporting

def run(cfg: dict) -> int:
    price = check_prices(cfg)
    place, skipped = check_placement(cfg)

    n_priced = sum(1 for b in cfg["bands"] if b.get("avgPerGame"))
    n_placed = sum(1 for b in cfg["bands"] if b.get("sections"))
    print(f"price checks:     {n_priced}/{len(cfg['bands'])} bands priced -> "
          f"{'CLEAN' if not price else f'{len(price)} finding(s)'}")
    # "CLEAN" on zero placed bands would be a lie of exactly the kind this file exists
    # to prevent: nothing was checked, so nothing came back clean.
    if n_placed == 0:
        placement_state = "NOT CHECKED"
    elif place:
        placement_state = f"{len(place)} finding(s)"
    else:
        placement_state = "CLEAN"
    print(f"placement checks: {n_placed}/{len(cfg['bands'])} bands have sections -> {placement_state}")
    for s in skipped:
        print(f"  SKIPPED - {s}")

    problems = price + place
    if problems:
        print(f"\n{len(problems)} PROBLEM(S):", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return 1
    if skipped:
        # Not a failure - the data is honestly absent, and the config says so. But do not
        # print "clean", which would read as "verified".
        print("\nno contradictions found, but placement is UNVERIFIED - see skips above")
        return 0
    print("\nclean - band prices and section placement both check out")
    return 0


# ------------------------------------------------------------------- self-test

def self_test() -> int:
    fails = []

    def check(label, got, want):
        if got != want:
            fails.append(f"{label}: got {got!r}, want {want!r}")

    def band(bid, **kw):
        b = {"id": bid, "label": bid, "family": "f", "rank": 1, "avgPerGame": {"renew": 10, "new": 11},
             "ring": "r", "rowScope": "all", "sections": [], "expectedArcs": 2}
        b.update(kw)
        return b

    # --- price checks
    check("clean prices pass", check_prices({"bands": [
        band("a", rank=1, avgPerGame={"renew": 100, "new": 105}),
        band("b", rank=2, avgPerGame={"renew": 90, "new": 95}),
    ]}), [])
    p = check_prices({"bands": [
        band("a", rank=1, avgPerGame={"renew": 90, "new": 95}),
        band("b", rank=2, avgPerGame={"renew": 100, "new": 105}),
    ]})
    check("swapped ranks caught", len(p), 2)  # once per series
    p = check_prices({"bands": [band("a", avgPerGame={"renew": 105, "new": 100})]})
    check("transposed renew/new caught", any("transposed" in x for x in p), True)
    p = check_prices({"bands": [band("a", rank=1), band("b", rank=3)]})
    check("non-contiguous ranks caught", any("are not 1..2" in x for x in p), True)
    check("duplicate id caught",
          any("duplicate band id" in x for x in check_prices({"bands": [band("a"), band("a", rank=2)]})), True)

    # --- arcs
    ring = ["1", "2", "3", "4", "5", "6", "7", "8"]
    check("one arc", arcs(ring, {"2", "3", "4"}), [["2", "3", "4"]])
    check("two arcs", arcs(ring, {"2", "3", "6", "7"}), [["2", "3"], ["6", "7"]])
    check("wraps the seam", arcs(ring, {"8", "1", "2"}), [["8", "1", "2"]])
    check("whole ring is one arc", len(arcs(ring, set(ring))), 1)
    check("empty", arcs(ring, set()), [])

    # --- placement: the case this whole file exists for. Two bands, mirror halves,
    # then one section misfiled from band A's right arc into band B.
    good = {"rings": {"r": ring}, "bands": [
        band("a", sections=["8", "1", "4", "5"]),
        band("b", sections=["2", "3", "6", "7"]),
    ]}
    pr, sk = check_placement(good)
    check("correct map is clean", pr, [])
    check("correct map skips nothing", sk, [])

    misfiled = {"rings": {"r": ring}, "bands": [
        band("a", sections=["8", "1", "5"]),          # lost "4" from one half
        band("b", sections=["2", "3", "4", "6", "7"]),
    ]}
    pr, _ = check_placement(misfiled)
    check("misfiled section is caught", len(pr) > 0, True)
    check("asymmetry is named", any("asymmetric" in x or "arc(s)" in x for x in pr), True)

    dup = {"rings": {"r": ring}, "bands": [
        band("a", sections=["1", "2"]), band("b", sections=["2", "3"]),
    ]}
    check("double-assigned section caught", any("belongs to one band" in x for x in check_placement(dup)[0]), True)

    orphan = {"rings": {"r": ring}, "bands": [band("a", sections=["1", "5"], expectedArcs=2)]}
    check("unclaimed sections caught", any("no band" in x for x in check_placement(orphan)[0]), True)

    off = {"rings": {"r": ring}, "bands": [band("a", sections=["99"])]}
    check("section not on the ring caught", any("not on ring" in x for x in check_placement(off)[0]), True)

    # Row-scoped bands share sections with the section-level band legitimately.
    rowscoped = {"rings": {"r": ring}, "bands": [
        band("a", sections=list(ring), expectedArcs=1),
        band("a-row1", sections=["1", "2"], rowScope="row1", expectedArcs=1),
    ]}
    check("row1 band may reuse sections", check_placement(rowscoped)[0], [])

    # Absent data must SKIP, never pass silently.
    pr, sk = check_placement({"rings": {}, "bands": [band("a")]})
    check("no sections -> no problems", pr, [])
    check("no sections -> reported skip", len(sk), 1)
    pr, sk = check_placement({"rings": {}, "bands": [band("a", sections=["1"])]})
    check("sections but no ring -> skip", any("not transcribed" in x for x in sk), True)

    # The real config must at least pass the price checks.
    cfg = json.loads(CONFIG.read_text())
    check("shipped config prices are clean", check_prices(cfg), [])

    for f in fails:
        print(f"  FAIL {f}", file=sys.stderr)
    print(f"self-test: {'FAILED' if fails else 'passed'} ({len(fails)} failure(s))")
    return 1 if fails else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()
    if args.self_test:
        return self_test()
    return run(json.loads(CONFIG.read_text()))


if __name__ == "__main__":
    sys.exit(main())
