#!/usr/bin/env python3
"""Fail loudly when the market series has holes in it (ops#24).

The collector is live and commits daily. Nothing noticed if it stopped. Four failure
modes were passing in silence, and the last two are the dangerous ones because they look
like success:

  1. The scheduled workflow stops firing. GitHub disables `schedule` triggers on repos
     with no activity for 60 days, and the season runs to April.
  2. TickPick changes its markup and every row fails. The total-failure guard keeps the
     store clean and exits non-zero, so the run goes red - but only if someone looks.
  3. A PARTIAL failure: 3 of 44 games error while 41 succeed. The run stays green and
     the series quietly develops holes in specific games.
  4. The event map goes stale and prices get recorded against a renamed id.

Why detect rather than backfill: TickPick has no historical price endpoint, so a missed
day is genuinely gone. The honest response is to show the hole - which is also what keeps
ops#8 from later fitting a curve to a series with gaps it does not know about.

Two modes on purpose:
  --strict  (collector workflow) staleness and per-game gaps are FATAL. A silent
            collector is worse than a noisy one.
  default   (npm run build) advisory. A local build on a day the collector has not run
            yet must not fail; that would make stale data block deploys, which is
            backwards - a deploy is how a fix ships.
"""

import argparse
import collections
import json
import pathlib
import sys
from datetime import date, datetime, timedelta, timezone

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCHEDULE = ROOT / "data" / "schedule.json"

# Every source is checked, not just the primary. A secondary source going quiet is the
# easiest failure to miss: the summary's cross-source ratio simply disappears, which
# looks like "only one source configured" rather than "a source broke". Missing entirely
# is tolerated (it may not be set up); going STALE after having data is not.
STORES = [
    # (name, path, required, rolling). `rolling` means the source publishes a WINDOW
    # rather than the whole season, so games beyond its horizon are unlisted rather than
    # missing. Only set it where that is measured - it trades away a real guarantee.
    ("tickpick", ROOT / "data" / "market" / "tickpick.jsonl", True, False),
    ("gametime", ROOT / "data" / "market" / "gametime.jsonl", False, False),
    ("ticketnetwork", ROOT / "data" / "market" / "ticketnetwork.jsonl", False, True),
]
STORE = STORES[0][1]

# One missed day is a hiccup (a delayed cron, a transient 5xx). Two consecutive means
# something is broken.
STALE_DAYS = 2
# A game absent this many days running while others have data is a per-game hole.
GAME_GAP_DAYS = 3
# A day whose success rate falls this far below the previous day's is a partial failure.
SUCCESS_DROP = 0.15


def load(path: pathlib.Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]


def analyse(rows: list[dict], games: list[dict], today: date,
            rolling: bool = False) -> dict:
    days = sorted({r["observedDate"] for r in rows})
    ok_by_day = collections.Counter()
    tot_by_day = collections.Counter()
    for r in rows:
        tot_by_day[r["observedDate"]] += 1
        if r.get("ok") and r.get("low") is not None:
            ok_by_day[r["observedDate"]] += 1

    findings = []

    if not days:
        # Every key the full return provides. Callers index some of these directly, so an
        # early return with a narrower shape is a KeyError waiting for the first run of a
        # newly added source - which is exactly when it would fire.
        return {"days": [], "findings": [("fatal", "the market store is empty")],
                "staleDays": None, "okByDay": {}, "totByDay": {},
                "horizon": None, "beyondHorizon": []}

    last = date.fromisoformat(days[-1])
    stale = (today - last).days
    if stale >= STALE_DAYS:
        findings.append(("fatal", f"no observation for {stale} days (last {days[-1]}). "
                                  f"The collector may have stopped - check the workflow "
                                  f"is still enabled; GitHub disables schedules after 60 "
                                  f"days of repo inactivity."))

    # Missing calendar days inside the series - distinct from staleness at the end.
    expected = []
    d = date.fromisoformat(days[0])
    while d <= last:
        expected.append(d.isoformat())
        d += timedelta(days=1)
    missing = [x for x in expected if x not in set(days)]
    if missing:
        findings.append(("fatal" if len(missing) > 1 else "warn",
                         f"{len(missing)} day(s) missing inside the series: "
                         f"{missing[:5]}{'...' if len(missing) > 5 else ''}. "
                         f"Not backfillable - TickPick has no historical endpoint."))

    # Per-game holes. Only meaningful once the game has appeared at all; a game that has
    # never been collected is a resolver problem, reported separately.
    seen_per_game: dict[int, set] = collections.defaultdict(set)
    for r in rows:
        if r.get("ok") and r.get("low") is not None:
            seen_per_game[r["gameId"]].add(r["observedDate"])

    future = [g for g in games if g["date"] >= today.isoformat()]

    # THE COVERAGE HORIZON. A source may publish a ROLLING WINDOW rather than the whole
    # season: TicketNetwork lists 29 of 44 home games, and every one of the 15 it omits is
    # late-season, with zero orphans (ops#33). Those games are NOT YET LISTED, which is a
    # publisher's editorial choice, not a broken event map.
    #
    # Calling them "never priced" would fire a fatal on every single run, forever. A check
    # that cries wolf daily is precisely how people learn to skip its output - the same
    # reasoning that deleted the empty-issue rule from check_issues.py, and the same
    # reasoning behind the 2-day staleness threshold rather than an hours-based one.
    #
    # So the horizon is the latest game THIS SOURCE has actually priced. A gap at or
    # before it is real and stays fatal. Beyond it, absence of evidence is not evidence of
    # absence, and it is reported as coverage rather than as a fault.
    # OPT-IN, per source. Applying this to every source would silently weaken the
    # guarantee where it matters most: TickPick resolves all 44 events, so a game it has
    # never priced IS a stale event map, and excusing it because it happens to be the
    # latest game would hide exactly the bug this check was written for.
    unpriced = [g for g in future if g["gameId"] not in seen_per_game]
    if rolling:
        priced_dates = [g["date"] for g in games if g["gameId"] in seen_per_game]
        horizon = max(priced_dates) if priced_dates else None
        never = [g for g in unpriced if horizon is None or g["date"] <= horizon]
        beyond = [g for g in unpriced if horizon is not None and g["date"] > horizon]
    else:
        horizon, never, beyond = None, unpriced, []
    if never:
        findings.append(("fatal", f"{len(never)} upcoming game(s) have NEVER been priced: "
                                  f"{[g['date'] for g in never][:5]}. The event map may be "
                                  f"stale - re-run --resolve."))

    for g in future:
        got = seen_per_game.get(g["gameId"])
        if not got:
            continue
        # Longest run of consecutive observation days where this game is absent but the
        # series as a whole has data. Deliberately not trailing-only: a hole in the
        # middle is just as damaging to ops#8, and a trailing-only check on a short
        # series is indistinguishable from "never priced", which is reported separately.
        run = worst = 0
        for d in days:
            if d in got:
                run = 0
            else:
                run += 1
                worst = max(worst, run)
        if worst >= GAME_GAP_DAYS:
            findings.append(("fatal", f"{g['date']} vs {g['opponent']['abbrev']} absent for "
                                      f"{worst} consecutive observation days while other games "
                                      f"have data - a per-game hole, which stays green in CI."))

    # Partial-failure detection: success rate dropping day over day.
    for prev, cur in zip(days, days[1:]):
        a = ok_by_day[prev] / tot_by_day[prev] if tot_by_day[prev] else 0
        b = ok_by_day[cur] / tot_by_day[cur] if tot_by_day[cur] else 0
        if a - b > SUCCESS_DROP:
            findings.append(("warn", f"success rate fell from {a:.0%} on {prev} to {b:.0%} "
                                     f"on {cur} - a partial failure leaves CI green."))

    return {"days": days, "findings": findings, "staleDays": stale,
            "okByDay": dict(ok_by_day), "totByDay": dict(tot_by_day),
            "horizon": horizon, "beyondHorizon": [g["date"] for g in beyond]}


def run(strict: bool, today: date) -> int:
    games = json.loads(SCHEDULE.read_text())["games"]
    fatal: list[str] = []
    warn: list[str] = []
    any_data = False

    for name, path, required, rolling in STORES:
        rows = load(path)
        if not rows:
            if required:
                print(f"{name}: NO DATA")
                fatal.append(f"{name} is the primary source and has no data at all")
            else:
                print(f"{name}: not collected (optional source, skipped)")
            continue
        any_data = True
        a = analyse(rows, games, today, rolling)
        print(f"{name}: {len(rows)} rows across {len(a['days'])} day(s), "
              f"{a['days'][0]} .. {a['days'][-1]}, last {a['staleDays']} day(s) ago")
        for d in a["days"][-3:]:
            print(f"    {d}: {a['okByDay'].get(d, 0)}/{a['totByDay'].get(d, 0)} priced")
        if a["beyondHorizon"]:
            print(f"    coverage horizon {a['horizon']}: "
                  f"{len(a['beyondHorizon'])} later game(s) not yet listed "
                  f"(rolling-window source, expected)")
        for lvl, m in a["findings"]:
            (fatal if lvl == "fatal" else warn).append(f"[{name}] {m}")

    if not any_data:
        print("no market data yet - nothing to check")
        return 1 if strict else 0

    for m in warn:
        print(f"  WARN {m}")

    if fatal:
        label = "PROBLEM" if strict else "WARNING (advisory - pass --strict to fail)"
        print(f"\n{len(fatal)} {label}(S):", file=sys.stderr)
        for m in fatal:
            print(f"  - {m}", file=sys.stderr)
        return 1 if strict else 0

    print("\nfresh - no gaps detected")
    return 0


def self_test() -> int:
    fails = []

    def check(label, got, want):
        if got != want:
            fails.append(f"{label}: got {got!r}, want {want!r}")

    def games(n, start="2026-10-01"):
        d0 = date.fromisoformat(start)
        return [{"gameId": i, "date": (d0 + timedelta(days=i)).isoformat(),
                 "opponent": {"abbrev": "XXX"}} for i in range(n)]

    def rows(days, gameids, ok=True):
        return [{"observedDate": d, "gameId": g, "ok": ok, "low": 50 if ok else None}
                for d in days for g in gameids]

    today = date(2026, 9, 10)
    gs = games(3, "2026-09-20")
    ids = [0, 1, 2]

    # A healthy series: three consecutive days ending today.
    healthy = ["2026-09-08", "2026-09-09", "2026-09-10"]
    a = analyse(rows(healthy, ids), gs, today)
    check("healthy series is clean", a["findings"], [])

    # Stale: last observation four days ago.
    a = analyse(rows(["2026-09-05", "2026-09-06"], ids), gs, today)
    check("staleness is fatal", any(l == "fatal" and "no observation for" in m
                                    for l, m in a["findings"]), True)

    # A hole in the middle.
    a = analyse(rows(["2026-09-08", "2026-09-10"], ids), gs, today)
    check("interior gap detected", any("missing inside the series" in m
                                       for _, m in a["findings"]), True)

    # A per-game hole while other games are fine. Needs a series long enough that a
    # 3-day absence is distinguishable from the game never having been priced.
    longer = ["2026-09-06", "2026-09-07", "2026-09-08", "2026-09-09", "2026-09-10"]
    r = rows(longer, [0, 1]) + rows(["2026-09-06", "2026-09-07"], [2])
    a = analyse(r, gs, today)
    check("per-game trailing hole detected", any("per-game hole" in m
                                                 for _, m in a["findings"]), True)
    # And a hole in the MIDDLE, which a trailing-only check would miss entirely.
    r = rows(longer, [0, 1]) + rows(["2026-09-06", "2026-09-10"], [2])
    a = analyse(r, gs, today)
    check("per-game interior hole detected", any("per-game hole" in m
                                                 for _, m in a["findings"]), True)
    # A single missed day for one game is noise, not a hole.
    r = rows(longer, [0, 1]) + rows([d for d in longer if d != "2026-09-08"], [2])
    a = analyse(r, gs, today)
    check("one missed day for a game is not flagged", any("per-game hole" in m
                                                          for _, m in a["findings"]), False)

    # ---- the coverage horizon (ops#33, ops#36) ----
    # A rolling-window source that has priced games 0 and 1 but not the later game 2 is
    # covering what it publishes, not failing. Fatal here would fire every run forever.
    a = analyse(rows(healthy, [0, 1]), gs, today, rolling=True)
    check("beyond the horizon is not fatal",
          any("NEVER been priced" in m for _, m in a["findings"]), False)
    check("beyond-horizon games are reported as coverage", a["beyondHorizon"],
          ["2026-09-22"])
    check("horizon is the latest game actually priced", a["horizon"], "2026-09-21")

    # But a hole INSIDE the covered window is a real gap and must stay fatal - otherwise
    # the horizon rule would excuse exactly the failure this check exists to catch.
    a = analyse(rows(healthy, [0, 2]), gs, today, rolling=True)
    check("a gap inside the covered window is still fatal",
          any("NEVER been priced" in m for _, m in a["findings"]), True)
    check("and nothing is excused as beyond-horizon", a["beyondHorizon"], [])

    # A source with no data at all has no horizon, so every future game is required -
    # otherwise a totally broken source would silently excuse itself.
    a = analyse(rows(healthy, []), gs, today, rolling=True)
    check("no data means no horizon, so nothing is excused", a["horizon"], None)

    # The guarantee TickPick relies on must survive all of the above: without rolling,
    # an unpriced later game is still a stale-event-map fatal.
    a = analyse(rows(healthy, [0, 1]), gs, today, rolling=False)
    check("a non-rolling source still fails on a never-priced game",
          any("NEVER been priced" in m for _, m in a["findings"]), True)
    check("and nothing is excused for it", a["beyondHorizon"], [])

    # A game never priced at all.
    a = analyse(rows(healthy, [0, 1]), gs, today)
    check("never-priced game detected", any("NEVER been priced" in m
                                            for _, m in a["findings"]), True)

    # Partial failure: success rate collapses on the last day.
    r = rows(healthy[:2], ids) + [
        {"observedDate": healthy[2], "gameId": 0, "ok": True, "low": 50},
        {"observedDate": healthy[2], "gameId": 1, "ok": False, "low": None},
        {"observedDate": healthy[2], "gameId": 2, "ok": False, "low": None},
    ]
    a = analyse(r, gs, today)
    check("success-rate drop detected", any("success rate fell" in m
                                            for _, m in a["findings"]), True)

    # Past games must not be demanded.
    past = [{"gameId": 9, "date": "2026-08-01", "opponent": {"abbrev": "OLD"}}]
    a = analyse(rows(healthy, ids), gs + past, today)
    check("past games not flagged as missing",
          any("NEVER been priced" in m and "2026-08-01" in m for _, m in a["findings"]), False)

    # Empty store.
    a = analyse([], gs, today)
    check("empty store is fatal", a["findings"][0][0], "fatal")

    # One missed day is a warning, not fatal - a delayed cron should not page anyone.
    a = analyse(rows(["2026-09-08", "2026-09-10"], ids), gs, today)
    lvls = [l for l, m in a["findings"] if "missing inside the series" in m]
    check("single interior gap is a warning", lvls, ["warn"])

    for f in fails:
        print(f"  FAIL {f}", file=sys.stderr)
    print(f"self-test: {'FAILED' if fails else 'passed'} ({len(fails)} failure(s))")
    return 1 if fails else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--strict", action="store_true", help="treat gaps as fatal")
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--today", help="override today's date, for testing")
    args = ap.parse_args()
    if args.self_test:
        return self_test()
    today = date.fromisoformat(args.today) if args.today else datetime.now(timezone.utc).date()
    return run(args.strict, today)


if __name__ == "__main__":
    sys.exit(main())
