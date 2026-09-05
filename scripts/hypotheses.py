#!/usr/bin/env python3
"""A hypothesis register, and a runner that refuses to answer questions the data cannot.

WHY THIS EXISTS, AND WHY IT IS SHAPED LIKE THIS. The AI Scientist loop is: generate ideas,
run experiments, write up, score, repeat. Three of those four already exist here - the
proposer role generates, the reviewer scores, and log/ is the write-up. What is missing is
the experiment step over the DATA, as opposed to over the infrastructure.

The trap is obvious and worth naming. That paper's characteristic failure is producing
confident, well-formatted findings that do not survive scrutiny, and this project's
characteristic failure is confident-looking wrong answers. Pointing a discovery loop at
ONE observation day would combine both. So the register's first-class output is not a
finding - it is a READINESS verdict:

    testable now | needs N more observation days (earliest YYYY-MM-DD) | needs outcomes

THE DISTINCTION THAT MAKES THIS USEFUL TODAY. Hypotheses split by what they consume:

  cross_sectional   many games, ONE moment          -> testable now
  longitudinal      one game, MANY moments          -> needs days we do not have
  outcome           needs sell-through results      -> needs games to be played

Time-series questions dominate the backlog, so it is tempting to conclude nothing can be
learned yet. That is wrong: with 44 games priced simultaneously by four independent
markets, a genuine cross-sectional question - does the team's own tier table misprice
weekends? - is answerable today and decision-relevant.

WHAT THIS DELIBERATELY WILL NOT DO. It will not report a p-value, and it will not call a
one-day result a law. A cross-sectional finding here is a SNAPSHOT: it describes the
market on one morning. Whether it holds is itself a longitudinal question, and the
register says so rather than quietly implying permanence.

Usage:
    python3 scripts/hypotheses.py              # readiness report + run what is ready
    python3 scripts/hypotheses.py --readiness  # readiness only, no experiments
    python3 scripts/hypotheses.py --self-test
"""

import argparse
import collections
import datetime as dt
import json
import pathlib
import statistics
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCHEDULE = ROOT / "data" / "schedule.json"
MARKET = ROOT / "data" / "market"

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from check_tier_market import spearman  # noqa: E402  - one implementation, tie-corrected


# --------------------------------------------------------------------------------------
# The register. Each entry states what it needs BEFORE it is run, so "not yet" is a
# first-class answer rather than a failure, and the falsifier is fixed in advance rather
# than chosen after seeing the numbers.
# --------------------------------------------------------------------------------------
REGISTER = [
    {
        "id": "H1-source-agreement",
        "kind": "cross_sectional",
        "question": "Do the four sources rank games the same way?",
        "predicts": "Every pair of sources correlates strongly and positively on game ordering.",
        "falsifier": "Any pair below rho=0.70 means one source is measuring something else - "
                     "a different seat pool, a stale cache, or a broken join - and the "
                     "cross-source agreement figure in summary.json is not what it claims.",
        "requires": {"observationDays": 1, "sources": 2},
        "unblocks": "whether cross-source agreement can be used as a staleness detector at all",
    },
    {
        "id": "H2-weekend-premium",
        "kind": "cross_sectional",
        "question": "Does the market price weekend games above weeknight games in the SAME tier?",
        "predicts": "No systematic gap. The team says its tiers already price on 'day of the "
                    "week' among other factors, so within a tier the residual should be noise.",
        "falsifier": "A consistent weekend premium across tiers and sources means the tier "
                     "table under-prices weekends - which would mean our own weekend games "
                     "are worth listing above the tier-implied break-even.",
        "requires": {"observationDays": 1, "sources": 1},
        "unblocks": "whether tier credit alone is a sufficient pricing anchor",
    },
    {
        "id": "H3-window-selection-bias",
        "kind": "cross_sectional",
        "question": "Do the rolling-window sources cover a biased subset of games?",
        "predicts": "TicketNetwork and ScoreBig list only near-term games, so their covered "
                    "set skews early-season - and early-season games are not a random sample "
                    "of the season.",
        "falsifier": "If their covered games do NOT differ in tier mix or price level from "
                     "the uncovered ones, the windows are harmless. If they do, every "
                     "cross-source statistic computed over them is comparing unlike sets.",
        "requires": {"observationDays": 1, "sources": 2},
        "unblocks": "how to read the cross-source ratio in summary.json",
    },
    {
        "id": "H4-price-decay",
        "kind": "longitudinal",
        "question": "Do asking prices fall as a game approaches?",
        "predicts": "Unknown. This is the question the sell-timing model needs and nobody has "
                    "data for.",
        "falsifier": "Stated when it becomes runnable, not now - naming a falsifier for a "
                     "test that cannot run invites fitting it to whatever arrives.",
        "requires": {"observationDays": 14, "sources": 1},
        "unblocks": "ops#8, the sell-timing model",
    },
    {
        "id": "H5-deadline-effect",
        "kind": "longitudinal",
        "question": "Do prices move sharply in the 48h before the exchange deadline?",
        "predicts": "Unknown.",
        "falsifier": "Stated when runnable.",
        "requires": {"observationDays": 21, "sources": 1, "gamesPlayed": 1},
        "unblocks": "whether the T-48h decision needs intraday collection",
    },
    {
        "id": "H6-sell-through",
        "kind": "outcome",
        "question": "What list price actually sells, and how does that vary with tier and timing?",
        "predicts": "Unknown - and unknowable without outcomes.",
        "falsifier": "Stated when runnable.",
        "requires": {"observationDays": 1, "outcomes": 8},
        "unblocks": "ops#8. This is the only path to a real P(sell) curve.",
    },
]


def inventory() -> dict:
    """What data actually exists right now. Measured, not assumed."""
    days, sources, rows = set(), [], 0
    for p in sorted(MARKET.glob("*.jsonl")):
        d = set()
        n = 0
        for line in p.read_text().splitlines():
            if not line.strip():
                continue
            r = json.loads(line)
            d.add(r["observedDate"])
            n += 1
        if n:
            sources.append(p.stem)
            days |= d
            rows += n
    games = json.loads(SCHEDULE.read_text())["games"]
    return {"observationDays": len(days), "dayList": sorted(days), "sources": sources,
            "rows": rows, "games": len(games)}


def readiness(entry: dict, inv: dict, outcomes: int, games_played: int,
              today: dt.date) -> dict:
    """Can this hypothesis be tested? If not, what is missing and when does it clear?

    Pure. The earliest-date estimate assumes one observation day per day, which is what
    the collectors actually produce - it is an estimate, and it is labelled as one.
    """
    req = entry["requires"]
    missing = []
    need_days = req.get("observationDays", 1)
    have_days = inv["observationDays"]
    if have_days < need_days:
        missing.append(f"{need_days - have_days} more observation day(s)")
    if len(inv["sources"]) < req.get("sources", 1):
        missing.append(f"{req['sources'] - len(inv['sources'])} more source(s)")
    if outcomes < req.get("outcomes", 0):
        missing.append(f"{req['outcomes'] - outcomes} more recorded outcome(s)")
    if games_played < req.get("gamesPlayed", 0):
        missing.append(f"{req['gamesPlayed'] - games_played} more played game(s)")

    earliest = None
    if have_days < need_days:
        # One collection per day. Outcomes and played games are NOT projected - those
        # depend on the schedule and on Wesley recording them, and guessing a date for
        # something a human must do is exactly the false precision this file avoids.
        earliest = (today + dt.timedelta(days=need_days - have_days)).isoformat()
    return {"id": entry["id"], "ready": not missing, "missing": missing,
            "earliestDataDate": earliest}


# --------------------------------------------------------------------------------------
# Experiments. Each returns a finding dict, or None when it declines to answer.
# --------------------------------------------------------------------------------------
def load_latest() -> dict[str, dict[int, dict]]:
    """{source: {gameId: row}} for the most recent observation day of each source."""
    out: dict[str, dict[int, dict]] = {}
    for p in sorted(MARKET.glob("*.jsonl")):
        rows = [json.loads(l) for l in p.read_text().splitlines() if l.strip()]
        rows = [r for r in rows if r.get("ok") and r.get("low") is not None]
        if not rows:
            continue
        latest = max(r["observedDate"] for r in rows)
        out[p.stem] = {r["gameId"]: r for r in rows if r["observedDate"] == latest}
    return out


def h1_source_agreement(latest: dict) -> dict:
    pairs, weak = [], []
    names = sorted(latest)
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            common = sorted(set(latest[a]) & set(latest[b]))
            if len(common) < 3:
                pairs.append({"pair": f"{a}/{b}", "n": len(common), "rho": None,
                              "note": "too few shared games to correlate"})
                continue
            rho = spearman([float(latest[a][g]["low"]) for g in common],
                           [float(latest[b][g]["low"]) for g in common])
            pairs.append({"pair": f"{a}/{b}", "n": len(common),
                          "rho": None if rho is None else round(rho, 4)})
            if rho is not None and rho < 0.70:
                weak.append(f"{a}/{b} rho={rho:.3f} on n={len(common)}")
    return {"id": "H1-source-agreement", "pairs": pairs, "falsified": bool(weak),
            "weakPairs": weak}


def h2_weekend_premium(latest: dict, games: list[dict], primary: str) -> dict:
    """Within each tier, compare weekend to weeknight lows. Ratio of medians."""
    by_id = {g["gameId"]: g for g in games}
    buckets: dict[tuple, list[float]] = collections.defaultdict(list)
    rows = latest.get(primary, {})
    for gid, r in rows.items():
        g = by_id.get(gid)
        if not g or not g.get("tier"):
            continue
        # Local weekday at the arena. Using the local date, not UTC - a 7pm Pacific
        # Saturday game is 02:00 UTC Sunday, and bucketing on UTC would call it a Sunday.
        wd = dt.date.fromisoformat(g["date"]).weekday()  # Mon=0
        buckets[(g["tier"], wd >= 4)].append(float(r["low"]))  # Fri/Sat/Sun = weekend
    per_tier = []
    for tier in sorted({t for t, _ in buckets}):
        we, wk = buckets.get((tier, True), []), buckets.get((tier, False), [])
        if len(we) < 2 or len(wk) < 2:
            per_tier.append({"tier": tier, "weekend": len(we), "weeknight": len(wk),
                             "ratio": None, "note": "too few games on one side"})
            continue
        mw, mk = statistics.median(we), statistics.median(wk)
        per_tier.append({"tier": tier, "weekend": len(we), "weeknight": len(wk),
                         "weekendMedian": mw, "weeknightMedian": mk,
                         "ratio": round(mw / mk, 4) if mk else None})
    usable = [t for t in per_tier if t.get("ratio")]
    consistent = bool(usable) and (all(t["ratio"] > 1.05 for t in usable)
                                   or all(t["ratio"] < 0.95 for t in usable))
    return {"id": "H2-weekend-premium", "perTier": per_tier, "source": primary,
            "tiersUsable": len(usable), "consistentAcrossTiers": consistent}


def h3_window_bias(latest: dict, games: list[dict], primary: str) -> dict:
    by_id = {g["gameId"]: g for g in games}
    prim = latest.get(primary, {})
    out = []
    for name, rows in sorted(latest.items()):
        if name == primary or len(rows) >= len(prim):
            continue
        covered = set(rows)
        cov = [float(prim[g]["low"]) for g in covered if g in prim]
        unc = [float(prim[g]["low"]) for g in prim if g not in covered]
        cov_d = sorted(by_id[g]["date"] for g in covered if g in by_id)
        unc_d = sorted(by_id[g]["date"] for g in prim if g not in covered and g in by_id)
        if len(cov) < 3 or len(unc) < 3:
            out.append({"source": name, "note": "not enough on one side to compare"})
            continue
        out.append({
            "source": name, "covered": len(cov), "uncovered": len(unc),
            "coveredMedianLow": round(statistics.median(cov), 2),
            "uncoveredMedianLow": round(statistics.median(unc), 2),
            "coveredDateRange": [cov_d[0], cov_d[-1]] if cov_d else None,
            "uncoveredDateRange": [unc_d[0], unc_d[-1]] if unc_d else None,
            "priceRatio": round(statistics.median(cov) / statistics.median(unc), 4),
        })
    return {"id": "H3-window-selection-bias", "sources": out, "measuredAgainst": primary}


def run(readiness_only: bool, today: dt.date) -> int:
    inv = inventory()
    games = json.loads(SCHEDULE.read_text())["games"]
    # Outcomes live in the browser, not the repo - see CLAUDE.md rule 1. Zero here means
    # "not visible from the repo", NOT "none exist", and the report says so.
    outcomes, played = 0, sum(1 for g in games if g["date"] < today.isoformat())

    print(f"data: {inv['observationDays']} observation day(s) "
          f"{inv['dayList']}, {len(inv['sources'])} source(s) {inv['sources']}, "
          f"{inv['rows']} rows, {inv['games']} games, {played} played")
    print(f"outcomes visible from the repo: {outcomes} "
          f"(they live in browser storage by design - this is not a count of what exists)\n")

    ready = []
    print("READINESS")
    for e in REGISTER:
        r = readiness(e, inv, outcomes, played, today)
        mark = "READY" if r["ready"] else "waiting"
        extra = "" if r["ready"] else f" - needs {', '.join(r['missing'])}"
        if r["earliestDataDate"]:
            extra += f" (data earliest {r['earliestDataDate']})"
        print(f"  [{mark:7}] {e['id']:26} {e['kind']:15}{extra}")
        if r["ready"]:
            ready.append(e)
    if readiness_only:
        return 0

    latest = load_latest()
    primary = "tickpick" if "tickpick" in latest else (sorted(latest)[0] if latest else None)
    if not primary:
        print("\nno usable market rows - nothing to test")
        return 0

    print(f"\nEXPERIMENTS ({len(ready)} ready)")
    findings = []
    for e in ready:
        if e["id"] == "H1-source-agreement":
            findings.append(h1_source_agreement(latest))
        elif e["id"] == "H2-weekend-premium":
            findings.append(h2_weekend_premium(latest, games, primary))
        elif e["id"] == "H3-window-selection-bias":
            findings.append(h3_window_bias(latest, games, primary))
        else:
            print(f"  {e['id']}: registered as ready but has no runner - that is a bug")
    for f in findings:
        print(f"\n  {f['id']}")
        for k, v in f.items():
            if k == "id":
                continue
            print(f"    {k}: {json.dumps(v)[:300]}")

    print(f"\nAll of the above rest on {inv['observationDays']} observation day(s). "
          f"A cross-sectional result is a SNAPSHOT of one morning, not a law - whether it "
          f"holds is itself a longitudinal question this data cannot yet answer.")
    return 0


def self_test() -> int:
    fails = []

    def check(label, got, want):
        if got != want:
            fails.append(f"{label}: got {got!r}, want {want!r}")

    today = dt.date(2026, 9, 5)
    inv1 = {"observationDays": 1, "sources": ["a", "b"], "dayList": ["2026-09-05"]}

    # Readiness arithmetic.
    cs = next(e for e in REGISTER if e["id"] == "H1-source-agreement")
    check("a cross-sectional test is ready on one day",
          readiness(cs, inv1, 0, 0, today)["ready"], True)
    lg = next(e for e in REGISTER if e["id"] == "H4-price-decay")
    r = readiness(lg, inv1, 0, 0, today)
    check("a longitudinal test is not ready on one day", r["ready"], False)
    check("and it says how many days are missing", r["missing"][0], "13 more observation day(s)")
    check("and projects the earliest data date", r["earliestDataDate"], "2026-09-18")
    oc = next(e for e in REGISTER if e["id"] == "H6-sell-through")
    r = readiness(oc, inv1, 0, 0, today)
    check("an outcome test is not ready with no outcomes", r["ready"], False)
    check("outcomes are NOT projected to a date", r["earliestDataDate"], None)
    check("outcome shortfall is reported", "8 more recorded outcome(s)" in r["missing"], True)
    # Enough outcomes -> ready.
    check("outcomes satisfy it", readiness(oc, inv1, 8, 0, today)["ready"], True)
    # A source shortfall is separate from a day shortfall.
    r = readiness(cs, {"observationDays": 1, "sources": ["a"]}, 0, 0, today)
    check("a single source blocks a two-source test", r["ready"], False)

    # H1: identical rankings correlate at 1.0; reversed at -1.0 and must be flagged.
    same = {"a": {1: {"low": 10}, 2: {"low": 20}, 3: {"low": 30}},
            "b": {1: {"low": 11}, 2: {"low": 21}, 3: {"low": 31}}}
    f = h1_source_agreement(same)
    check("identical ordering correlates perfectly", f["pairs"][0]["rho"], 1.0)
    check("and is not falsified", f["falsified"], False)
    rev = {"a": {1: {"low": 10}, 2: {"low": 20}, 3: {"low": 30}},
           "b": {1: {"low": 30}, 2: {"low": 20}, 3: {"low": 10}}}
    check("reversed ordering is falsified", h1_source_agreement(rev)["falsified"], True)
    # Too few shared games must decline, not correlate on noise.
    few = {"a": {1: {"low": 10}, 2: {"low": 20}}, "b": {1: {"low": 5}, 2: {"low": 6}}}
    check("two shared games is too few", h1_source_agreement(few)["pairs"][0]["rho"], None)

    # H2: weekday bucketing must use the LOCAL date. 2026-09-26 is a Saturday.
    check("Saturday is a weekend", dt.date(2026, 9, 26).weekday() >= 4, True)
    check("Tuesday is not", dt.date(2026, 9, 22).weekday() >= 4, False)
    check("Friday counts as weekend", dt.date(2026, 9, 25).weekday() >= 4, True)
    gs = [{"gameId": i, "date": d, "tier": "A"} for i, d in enumerate(
        ["2026-09-25", "2026-09-26", "2026-09-22", "2026-09-23"])]
    lat = {"p": {0: {"low": 100}, 1: {"low": 110}, 2: {"low": 50}, 3: {"low": 60}}}
    f = h2_weekend_premium(lat, gs, "p")
    check("weekend premium detected", f["perTier"][0]["ratio"], round(105 / 55, 4))
    check("and flagged as consistent", f["consistentAcrossTiers"], True)
    # One game on a side must decline rather than compare medians of one.
    gs2 = [{"gameId": 0, "date": "2026-09-25", "tier": "A"},
           {"gameId": 1, "date": "2026-09-22", "tier": "A"}]
    f = h2_weekend_premium({"p": {0: {"low": 100}, 1: {"low": 50}}}, gs2, "p")
    check("one game per side is refused", f["perTier"][0]["ratio"], None)

    # Every registered hypothesis must declare the fields the report depends on.
    for e in REGISTER:
        for k in ("id", "kind", "question", "predicts", "falsifier", "requires", "unblocks"):
            if k not in e:
                fails.append(f"{e.get('id')} is missing {k}")
        if e["kind"] not in ("cross_sectional", "longitudinal", "outcome"):
            fails.append(f"{e['id']} has an unknown kind {e['kind']!r}")
    check("register ids are unique", len({e["id"] for e in REGISTER}), len(REGISTER))

    for f_ in fails:
        print(f"  FAIL {f_}", file=sys.stderr)
    print(f"self-test: {'FAILED' if fails else 'passed'} ({len(fails)} failure(s))")
    return 1 if fails else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--readiness", action="store_true")
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--today", type=dt.date.fromisoformat, default=dt.date.today())
    a = ap.parse_args()
    return self_test() if a.self_test else run(a.readiness, a.today)


if __name__ == "__main__":
    sys.exit(main())
