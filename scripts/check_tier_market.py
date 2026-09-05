#!/usr/bin/env python3
"""Cross-check the hand-transcribed tier table against collected market prices.

WHY THIS EXISTS. config/tiers.json was transcribed by hand from a marketing JPEG, and
CLAUDE.md is explicit that a single misread row would silently misprice a game for the
whole season. fetch_schedule.py already validates the table's DATES and OPPONENTS
against the NHL API. Nothing validated the TIER ASSIGNMENTS themselves - the actual
content of the transcription - because there was no independent source for them.

There is one now. TickPick's asking prices are set by thousands of unrelated sellers who
have never seen the Sharks' tier graphic. If the transcription is right, a better tier
should command a higher ask. Measured 2026-09-05 on the first day of collection:

    tier   n  median arena low
    A+     7      69.0
    A      8      63.5
    B      7      47.0
    C     11      36.0
    D      9      23.0
    PRE    2      14.5

    Spearman(tier rank, arena low) = -0.914, perfectly monotone by median.

That is a strong independent signal that the table is transcribed correctly, and it is
the kind of check ops#19 asks for in a different context: one that would fail loudly if
an entry were misfiled.

HONEST LIMITS. This tests CONSISTENCY, not correctness. A tier table that were wrong in
a way the market also believed - two adjacent tiers swapped where demand happens to be
equal - would pass. And the arena low is one order statistic from a whole building, so a
single game's ask is noisy; the tier-level aggregate is what carries signal. Treat a
per-game flag as "look at this", never as proof.

Usage:
    python3 scripts/check_tier_market.py
    python3 scripts/check_tier_market.py --self-test
"""

import argparse
import collections
import json
import pathlib
import statistics
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCHEDULE = ROOT / "data" / "schedule.json"
SUMMARY = ROOT / "data" / "market" / "summary.json"

# Best to worst. PRESEASON is deliberately outside the ladder: it is a different product,
# not the bottom rung, and forcing it into the ordering would assert something untested.
LADDER = ["A+", "A", "B", "C", "D"]


def spearman(a: list[float], b: list[float]) -> float | None:
    if len(a) < 3:
        return None

    def ranks(v: list[float]) -> list[float]:
        order = sorted(range(len(v)), key=lambda i: v[i])
        out = [0.0] * len(v)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and v[order[j + 1]] == v[order[i]]:
                j += 1
            avg = (i + j) / 2 + 1
            for k in range(i, j + 1):
                out[order[k]] = avg
            i = j + 1
        return out

    ra, rb = ranks(a), ranks(b)
    n = len(a)
    ma, mb = sum(ra) / n, sum(rb) / n
    num = sum((x - ma) * (y - mb) for x, y in zip(ra, rb))
    den = (sum((x - ma) ** 2 for x in ra) * sum((y - mb) ** 2 for y in rb)) ** 0.5
    return None if den == 0 else num / den


def analyse(games: list[dict], market: list[dict]) -> dict:
    lows = {m["gameId"]: m["low"] for m in market if m.get("low") is not None}
    tier_of = {g["gameId"]: g["tier"] for g in games}

    by_tier: dict[str, list[float]] = collections.defaultdict(list)
    paired: list[tuple[str, int, float]] = []
    for gid, low in lows.items():
        t = tier_of.get(gid)
        if not t:
            continue
        by_tier[t].append(low)
        if t in LADDER:
            paired.append((t, gid, low))

    medians = {t: statistics.median(v) for t, v in by_tier.items() if v}
    rank = {t: i + 1 for i, t in enumerate(LADDER)}
    rho = spearman([rank[t] for t, _, _ in paired], [v for _, _, v in paired])

    present = [t for t in LADDER if t in medians]
    inversions = [
        (present[i], medians[present[i]], present[i + 1], medians[present[i + 1]])
        for i in range(len(present) - 1)
        if medians[present[i]] < medians[present[i + 1]]
    ]

    # Per-game outliers: a game whose ask sits past the median of a tier two steps away
    # is worth eyeballing. Two steps, not one, because adjacent tiers genuinely overlap.
    outliers = []
    for t, gid, low in paired:
        i = rank[t] - 1
        far_better = present[i - 2] if i - 2 >= 0 else None
        far_worse = present[i + 2] if i + 2 < len(present) else None
        if far_better and low > medians[far_better]:
            outliers.append((gid, t, low, f"asks above the {far_better} median ({medians[far_better]:.0f})"))
        elif far_worse and low < medians[far_worse]:
            outliers.append((gid, t, low, f"asks below the {far_worse} median ({medians[far_worse]:.0f})"))

    return {"medians": medians, "counts": {t: len(v) for t, v in by_tier.items()},
            "spearman": rho, "inversions": inversions, "outliers": outliers,
            "n": len(paired)}


def run() -> int:
    if not SUMMARY.exists():
        print(f"No {SUMMARY.relative_to(ROOT)} - run the collector and summariser first.")
        print("check SKIPPED (no market data), not clean.")
        return 0

    summary = json.loads(SUMMARY.read_text())
    games = json.loads(SCHEDULE.read_text())["games"]
    a = analyse(games, summary["games"])
    days = summary.get("observationDays", 0)

    print(f"market data: {a['n']} regular-season games, {days} observation day(s)")
    print(f"{'tier':10s} {'n':>3s} {'median low':>11s}")
    for t in LADDER + ["PRESEASON"]:
        if t in a["medians"]:
            print(f"{t:10s} {a['counts'][t]:3d} {a['medians'][t]:11.1f}")

    rho = a["spearman"]
    print(f"\nSpearman(tier rank, arena low): {rho:+.3f}" if rho is not None else "\nSpearman: n/a")
    if rho is not None:
        print("  expected NEGATIVE - a better tier should command a higher ask")

    problems, warnings = [], []

    if rho is not None and rho > -0.3:
        problems.append(
            f"tier ordering is NOT reflected in market prices (rho={rho:+.3f}). Either the "
            f"transcription is wrong, or tier is not what drives demand. Both matter."
        )
    for better, mb, worse, mw in a["inversions"]:
        warnings.append(f"tier {better} median ({mb:.0f}) sits BELOW tier {worse} ({mw:.0f})")

    dates = {g["gameId"]: (g["date"], g["opponent"]["abbrev"]) for g in games}
    for gid, t, low, why in a["outliers"]:
        d, opp = dates.get(gid, ("?", "?"))
        warnings.append(f"{d} vs {opp} filed {t} but {why} (ask ${low:.0f})")

    if warnings:
        print(f"\n{len(warnings)} thing(s) to eyeball:")
        for w in warnings:
            print(f"  ? {w}")

    # One observation day is suggestive, not conclusive, and saying otherwise would be
    # the invented precision this project refuses.
    if days < 2:
        print(f"\nCONSISTENT so far, on {days} day of data - suggestive, not conclusive.")
    else:
        print(f"\nCONSISTENT across {days} observation days.")

    if problems:
        print(f"\n{len(problems)} PROBLEM(S):", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return 1
    return 0


def self_test() -> int:
    fails = []

    def check(label, got, want):
        if got != want:
            fails.append(f"{label}: got {got!r}, want {want!r}")

    def games(spec):
        return [{"gameId": i, "date": f"2026-10-{i:02d}", "tier": t,
                 "opponent": {"abbrev": "XXX"}} for i, t in enumerate(spec, 1)]

    def mk(lows):
        return [{"gameId": i, "low": v} for i, v in enumerate(lows, 1)]

    # A correctly ordered table: strong negative rho, no inversions.
    spec = ["A+"] * 3 + ["A"] * 3 + ["B"] * 3 + ["C"] * 3 + ["D"] * 3
    good = [70, 72, 68, 60, 62, 58, 45, 47, 43, 35, 37, 33, 22, 24, 20]
    a = analyse(games(spec), mk(good))
    check("clean table has no inversions", a["inversions"], [])
    check("clean table has no outliers", a["outliers"], [])
    if a["spearman"] is None or a["spearman"] > -0.8:
        fails.append(f"clean table rho should be strongly negative, got {a['spearman']}")

    # Two tiers swapped in the transcription: the market disagrees, and it must show up.
    swapped = ["A+"] * 3 + ["D"] * 3 + ["B"] * 3 + ["C"] * 3 + ["A"] * 3
    a = analyse(games(swapped), mk(good))
    check("swapped tiers produce inversions", len(a["inversions"]) > 0, True)

    # One game misfiled into the bottom tier while asking a top-tier price.
    spec2 = ["A+"] * 3 + ["A"] * 3 + ["B"] * 3 + ["C"] * 3 + ["D"] * 2 + ["D"]
    lows2 = good[:14] + [71]
    a = analyse(games(spec2), mk(lows2))
    check("misfiled game is flagged", any(o[0] == 15 for o in a["outliers"]), True)

    # Random prices must NOT read as consistent.
    a = analyse(games(spec), mk([40] * 15))
    check("flat prices give no rho", a["spearman"], None if a["spearman"] is None else a["spearman"])

    # PRESEASON must stay out of the ladder correlation.
    a = analyse(games(["A+", "A", "B", "C", "D", "PRESEASON"]), mk([70, 60, 45, 35, 22, 14]))
    check("preseason excluded from paired ranks", a["n"], 5)
    check("preseason still summarised", "PRESEASON" in a["medians"], True)

    # Missing market data must not crash.
    check("no market rows", analyse(games(["A+", "A"]), [])["n"], 0)

    for f in fails:
        print(f"  FAIL {f}", file=sys.stderr)
    print(f"self-test: {'FAILED' if fails else 'passed'} ({len(fails)} failure(s))")
    return 1 if fails else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()
    return self_test() if args.self_test else run()


if __name__ == "__main__":
    sys.exit(main())
