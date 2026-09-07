#!/usr/bin/env python3
"""Which sections are comparable to a given section, learned rather than assumed.

ops#11 exists because Wesley guessed that the opposite corner on the same side might
price like our section, and hardcoding that guess would quietly corrupt every comp the
tool produces. It proposed learning the answer from price MOVEMENT: correlate each
section against ours over weeks of whole-arena snapshots.

**That method is still blocked and this script is not it.** Section-level prices come
only from the TM member map, which is a manual capture - three games, one observation
each. There is no per-section time series to correlate, and there will not be one until
something collects the member map repeatedly.

What is available instead is stronger than geometry and weaker than the movement study:
**Ticketmaster's own equivalence declaration.** Within a game, TM prices the 50 sections
at only 8-13 distinct levels. Sections sharing a level are being called interchangeable
by the seller. ops#60 measured that this grouping survives across tiers - 0.94-0.97 pair
agreement between games - while section RANK does not.

So a comp set here means: sections TM priced identically to this one, in every game we
have seen. That is a claim about the seller's pricing policy, not about demand, and the
docstring says so because the two are easy to confuse.

## Do not read the agreement counts as probabilities

With three games, a pair agrees in 0, 1, 2 or 3 of them. Reporting "0.67" would invent
resolution the data does not have, so this prints "2/3 games" and ranks on the integer.
When the store grows past a handful of games that judgement should be revisited.
"""
import itertools
import json
import pathlib
import sys
from collections import defaultdict

ROOT = pathlib.Path(__file__).resolve().parent.parent
STORE = ROOT / "data" / "primary" / "prices.jsonl"
SCHEDULE = ROOT / "data" / "schedule.json"
OUT = ROOT / "data" / "primary" / "comps.json"

# A comp must have been priced identically in EVERY game where both were observed.
# Anything less is a near-miss and is reported separately rather than blended in:
# a section that matched twice and diverged once is a different kind of fact from one
# that always matched, and averaging them would hide which.
STRICT = 1.0


def load(store=STORE):
    by = defaultdict(dict)
    for line in store.read_text().splitlines():
        if line.strip():
            r = json.loads(line)
            by[r["gameId"]][r["section"]] = r["fromPrice"]
    return dict(by)


def pair_counts(by):
    """(section, other) -> (games agreeing, games where both were observed)."""
    agree = defaultdict(int)
    seen = defaultdict(int)
    for prices in by.values():
        for a, b in itertools.combinations(sorted(prices), 2):
            seen[(a, b)] += 1
            if prices[a] == prices[b]:
                agree[(a, b)] += 1
    return {k: (agree.get(k, 0), v) for k, v in seen.items()}


def comps_for(section, counts):
    """Sections grouped with `section`, split into always-agreed and sometimes-agreed."""
    always, sometimes = [], []
    for (a, b), (ag, n) in counts.items():
        if section not in (a, b) or ag == 0:
            continue
        other = b if a == section else a
        (always if ag == n else sometimes).append((other, ag, n))
    return sorted(always), sorted(sometimes, key=lambda t: (-t[1], t[0]))


def position(price, prices):
    """Where `price` sits among a game's section prices, as a rank not a percentile.

    Deliberately not a percentile: with 8-13 distinct levels a percentile implies a
    smooth distribution that is not there, and would read as precision.
    """
    levels = sorted(set(prices))
    cheaper = sum(1 for p in levels if p < price)
    return {"level": cheaper + 1, "ofLevels": len(levels),
            "sectionsAtOrBelow": sum(1 for p in prices if p <= price),
            "ofSections": len(prices)}


def build(by):
    counts = pair_counts(by)
    sections = sorted({s for p in by.values() for s in p})
    out = {}
    for s in sections:
        always, sometimes = comps_for(s, counts)
        out[str(s)] = {
            "comps": [a for a, _, _ in always],
            "nearMisses": [{"section": a, "agreed": ag, "of": n} for a, ag, n in sometimes],
            "observedIn": sum(1 for p in by.values() if s in p),
        }
    return out


def main():
    by = load()
    if not by:
        print("NOT CHECKED - primary store is empty")
        return 0
    sched = json.loads(SCHEDULE.read_text())
    games = {g["gameId"]: g for g in (sched["games"] if isinstance(sched, dict) else sched)}
    table = build(by)

    payload = {
        "_comment": "Comp sets derived from TM member-map price equality. NOT a demand "
                    "measurement and NOT the movement study ops#11 asked for - see "
                    "scripts/comps.py docstring.",
        "_method": "sections priced identically in every game where both were observed",
        "games": [{"gameId": g, "date": games.get(g, {}).get("date"),
                   "tier": games.get(g, {}).get("tier"),
                   "distinctPrices": len(set(by[g].values()))} for g in sorted(by)],
        "sections": table,
    }
    OUT.write_text(json.dumps(payload, indent=1, sort_keys=True) + "\n")

    n_games = len(by)
    print(f"{len(table)} sections across {n_games} game(s) -> {OUT.relative_to(ROOT)}")
    if n_games < 2:
        print("  only one game: every equality is a coincidence until a second confirms it")
    sizes = [len(v["comps"]) for v in table.values()]
    alone = [s for s, v in table.items() if not v["comps"]]
    print(f"  comp-set size: min {min(sizes)}, median {sorted(sizes)[len(sizes)//2]}, max {max(sizes)}")
    if alone:
        print(f"  {len(alone)} section(s) with NO stable comp: {', '.join(alone)}")
        print("    (these are the ones geometry would have guessed wrong about silently)")
    return 0


def self_test():
    fails = []

    def check(name, got, want):
        if got != want:
            fails.append(f"{name}: got {got!r}, want {want!r}")

    # Two games. 101/102 agree in both; 101/103 agree in one; 201 never agrees.
    by = {
        1: {101: 10.0, 102: 10.0, 103: 10.0, 201: 5.0},
        2: {101: 20.0, 102: 20.0, 103: 30.0, 201: 5.0},
    }
    counts = pair_counts(by)
    check("always-agreed pair", counts[(101, 102)], (2, 2))
    check("partly-agreed pair", counts[(101, 103)], (1, 2))

    always, sometimes = comps_for(101, counts)
    check("strict comp only", [a for a, _, _ in always], [102])
    check("near miss kept separate", [(a, ag, n) for a, ag, n in sometimes], [(103, 1, 2)])

    # A section observed in only one game must not gain a comp from that single game
    # ... unless the pair was only ever co-observed once, which IS ag == n. That is the
    # honest reading - it is a comp on the evidence available - so `observedIn` is
    # published alongside, and the caller can discount it.
    by2 = {1: {101: 7.0, 999: 7.0}}
    t = build(by2)
    check("single-game comp is reported", t["101"]["comps"], [999])
    check("with its evidence count", t["101"]["observedIn"], 1)

    # Position is a level rank, not a percentile, and ties count as one level.
    check("position of cheapest", position(5.0, [5.0, 5.0, 9.0, 12.0]),
          {"level": 1, "ofLevels": 3, "sectionsAtOrBelow": 2, "ofSections": 4})
    check("position of dearest", position(12.0, [5.0, 5.0, 9.0, 12.0]),
          {"level": 3, "ofLevels": 3, "sectionsAtOrBelow": 4, "ofSections": 4})

    # A price between observed levels lands above the levels below it, not inside one.
    check("interpolated price", position(10.0, [5.0, 9.0, 12.0])["level"], 3)

    # Symmetry: comps_for must find a section whichever side of the pair key it is on.
    check("comp found from the high side", [a for a, _, _ in comps_for(102, counts)[0]], [101])

    for f in fails:
        print("FAIL", f)
    print(f"comps self-test: {'PASS' if not fails else str(len(fails)) + ' FAILURES'}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(self_test() if "--self-test" in sys.argv else main())
