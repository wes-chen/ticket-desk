#!/usr/bin/env python3
"""Does a section's price position hold across game tiers?

The question ops#60 asks is "can we carry a section's standing from one game to
another", because if we can, one member-map capture prices every game and we never
need a per-game read of our own section.

**Spearman on section rank is the wrong instrument for it**, and this script exists
partly to say so. Two reasons, both measured:

1. The arena has two bowls, and every lower-bowl section outranks every upper-bowl one
   in every game observed. A whole-arena rank correlation therefore mostly measures
   "lower beats upper", which nobody doubted. Split by bowl, the same pair that scores
   0.90 across 50 sections scores 0.65 and 0.56. The headline was carried by the bowl
   gap, not by section ordering.

2. There are only 8-13 DISTINCT prices across 50 sections. Most sections are tied, so
   most of the "rank" being correlated is tie-breaking noise: four sections moving from
   a shared $61 to a shared $20 shows up as a 14-place rank swing while nothing about
   their relationship changed.

So the primary measure here is the **band partition** - over every pair of sections, do
two games agree on whether that pair is priced the same? That ignores rank and ignores
the band's price level, and it is the thing that turns out to be stable. Spearman is
still computed, whole-arena and per-bowl, because the gap between those numbers is the
evidence for point 1 and should stay visible rather than being asserted here.
"""
import itertools
import json
import pathlib
import sys
from collections import defaultdict

ROOT = pathlib.Path(__file__).resolve().parent.parent
STORE = ROOT / "data" / "primary" / "prices.jsonl"
SCHEDULE = ROOT / "data" / "schedule.json"

# A section pair agreement below this means the peer-group read has stopped holding
# and the "comp against your band" advice needs revisiting. Chosen as a floor well
# under the three observed pairs (0.940-0.967), not fitted to them.
AGREEMENT_FLOOR = 0.85


def load(store=STORE):
    by = defaultdict(dict)
    for line in store.read_text().splitlines():
        if line.strip():
            r = json.loads(line)
            by[r["gameId"]][r["section"]] = r["fromPrice"]
    return dict(by)


def ranks(prices):
    """Average ranks, ties shared - the tie handling is the point, see the docstring."""
    xs = sorted(prices.items(), key=lambda kv: kv[1])
    out = {}
    i = 0
    while i < len(xs):
        j = i
        while j + 1 < len(xs) and xs[j + 1][1] == xs[i][1]:
            j += 1
        for k in range(i, j + 1):
            out[xs[k][0]] = (i + j) / 2 + 1
        i = j + 1
    return out


def spearman(a, b, keys):
    ra, rb = ranks({k: a[k] for k in keys}), ranks({k: b[k] for k in keys})
    n = len(keys)
    ma, mb = sum(ra.values()) / n, sum(rb.values()) / n
    den = (sum((ra[k] - ma) ** 2 for k in keys) * sum((rb[k] - mb) ** 2 for k in keys)) ** 0.5
    if den == 0:  # one side entirely tied - undefined, not 1.0
        return None
    return sum((ra[k] - ma) * (rb[k] - mb) for k in keys) / den


def band_agreement(a, b, keys):
    """Fraction of section PAIRS on which the two games agree about same-band-ness."""
    same = total = 0
    for x, y in itertools.combinations(sorted(keys), 2):
        total += 1
        if (a[x] == a[y]) == (b[x] == b[y]):
            same += 1
    return (same / total, total) if total else (None, 0)


def compare(a, b):
    keys = sorted(set(a) & set(b))
    lower = [s for s in keys if s < 200]
    upper = [s for s in keys if s >= 200]
    agree, pairs = band_agreement(a, b, keys)
    ratios = sorted(b[k] / a[k] for k in keys)
    return {
        "n": len(keys),
        "bandAgreement": agree,
        "sectionPairs": pairs,
        "spearmanAll": spearman(a, b, keys),
        "spearmanLower": spearman(a, b, lower) if len(lower) > 2 else None,
        "spearmanUpper": spearman(a, b, upper) if len(upper) > 2 else None,
        "ratioMin": ratios[0],
        "ratioMedian": ratios[len(ratios) // 2],
        "ratioMax": ratios[-1],
    }


def main():
    by = load()
    if len(by) < 2:
        print(f"NOT CHECKED - {len(by)} game(s) in the store, need 2 to compare")
        return 0
    sched = json.loads(SCHEDULE.read_text())
    games = {g["gameId"]: g for g in (sched["games"] if isinstance(sched, dict) else sched)}

    for gid in sorted(by):
        g = games.get(gid, {})
        levels = len(set(by[gid].values()))
        print(f"{gid}  {g.get('date','?')}  {g.get('opponent',{}).get('abbrev','?'):>3}  "
              f"tier {str(g.get('tier','?')):<9} {len(by[gid])} sections, {levels} distinct prices")

    worst = 1.0
    print()
    for x, y in itertools.combinations(sorted(by), 2):
        c = compare(by[x], by[y])
        tx, ty = str(games.get(x, {}).get("tier", x)), str(games.get(y, {}).get("tier", y))
        worst = min(worst, c["bandAgreement"])
        print(f"{tx:>9} vs {ty:<9} n={c['n']}")
        print(f"    band agreement {c['bandAgreement']:.4f} over {c['sectionPairs']} section pairs")
        print(f"    spearman  all={c['spearmanAll']:.4f}  lower={c['spearmanLower']:.4f}  "
              f"upper={c['spearmanUpper']:.4f}")
        print(f"    price ratio {c['ratioMin']:.2f}x - {c['ratioMax']:.2f}x, median {c['ratioMedian']:.2f}x")

    print()
    if worst < AGREEMENT_FLOOR:
        print(f"FAIL - band agreement {worst:.4f} below floor {AGREEMENT_FLOOR}: "
              "sections no longer keep their peer group across games")
        return 1
    print(f"OK - lowest band agreement {worst:.4f}, floor {AGREEMENT_FLOOR}")
    return 0


def self_test():
    fails = []

    def check(name, got, want):
        if got != want:
            fails.append(f"{name}: got {got!r}, want {want!r}")

    # Ties share a rank rather than breaking arbitrarily.
    check("ties", ranks({1: 5.0, 2: 5.0, 3: 9.0}), {1: 1.5, 2: 1.5, 3: 3.0})

    # A relabelling that preserves the partition scores 1.0 even though every price
    # changed and the ORDER of the two bands flipped. This is the case Spearman gets
    # wrong and the reason band agreement is the primary measure.
    a = {101: 10.0, 102: 10.0, 201: 20.0, 202: 20.0}
    b = {101: 99.0, 102: 99.0, 201: 1.0, 202: 1.0}
    check("partition survives a flip", band_agreement(a, b, sorted(a))[0], 1.0)
    check("spearman does not", spearman(a, b, sorted(a)), -1.0)

    # Splitting one band in two is a real disagreement, and is counted per PAIR:
    # of the 6 pairs, only 101-102 changes status.
    c = {101: 10.0, 102: 11.0, 201: 20.0, 202: 20.0}
    check("split band", round(band_agreement(a, c, sorted(a))[0], 4), round(5 / 6, 4))

    # Fully-tied input makes Spearman undefined; it must not silently report 1.0.
    flat = {101: 7.0, 102: 7.0, 201: 7.0}
    check("degenerate spearman", spearman(flat, flat, sorted(flat)), None)

    # The bowl split must actually partition on 200, since the whole argument rests on it.
    got = compare({101: 1.0, 102: 2.0, 103: 3.0, 201: 9.0, 202: 8.0, 203: 7.0},
                  {101: 1.0, 102: 2.0, 103: 3.0, 201: 7.0, 202: 8.0, 203: 9.0})
    check("lower bowl agrees", got["spearmanLower"], 1.0)
    check("upper bowl inverts", got["spearmanUpper"], -1.0)

    for f in fails:
        print("FAIL", f)
    print(f"section_rank self-test: {'PASS' if not fails else str(len(fails)) + ' FAILURES'}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(self_test() if "--self-test" in sys.argv else main())
