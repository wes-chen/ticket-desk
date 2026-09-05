#!/usr/bin/env python3
"""Derive a small per-game market summary the app can import at build time.

The JSONL store is the source of truth; this is a pure derivation from it, so it can be
rebuilt at any time without re-collecting. Kept separate from the collector for exactly
that reason - a derivation that can only be produced by a network call is not
reproducible.

WHAT THESE NUMBERS ARE, and are not. TickPick's AggregateOffer is the cheapest and
priciest listing in the WHOLE ARENA, all-in. It is not a comp for any particular seat:
the low is almost always an upper-deck single. Section-level data would be a real comp,
and TickPick cannot provide it - the listing grid sits behind a /ajax/ path that
robots.txt disallows (see collect_tickpick.py). So `low` answers "what does the cheapest
way into this game cost", which is a genuine demand signal and nothing more.

It is also a COMP MARKET, not the channel Wesley sells on. Ticketmaster blocks both a
runner and a residential browser. Anything downstream that treats these as his own
achievable prices is wrong.
"""

import json
import pathlib
import sys
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCHEDULE = ROOT / "data" / "schedule.json"
DEST = ROOT / "data" / "market" / "summary.json"

# Primary first. TickPick is primary because its prices are all-in by design, and because
# it is measured consistently LOWER than Gametime on every game - so using it for the
# headline figure is the conservative choice.
STORES = [
    ("tickpick", ROOT / "data" / "market" / "tickpick.jsonl"),
    ("gametime", ROOT / "data" / "market" / "gametime.jsonl"),
    # Publishes Offer.price - a scalar ask, so it contributes a LOW and no high. It also
    # covers a rolling window rather than the season, so it is absent for late-season
    # games by design rather than by failure. See collect_ticketnetwork.py.
    ("ticketnetwork", ROOT / "data" / "market" / "ticketnetwork.jsonl"),
]
PRIMARY = "tickpick"


def load_rows(path: pathlib.Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]


def _series_by_game(rows: list[dict]) -> dict[int, list[dict]]:
    by_game: dict[int, list[dict]] = {}
    for r in rows:
        if r.get("ok") and r.get("low") is not None:
            by_game.setdefault(r["gameId"], []).append(r)
    for v in by_game.values():
        v.sort(key=lambda r: r["observedDate"])
    return by_game


def summarize(rows_by_source: dict[str, list[dict]], games: list[dict]) -> dict:
    per_source = {name: _series_by_game(rows) for name, rows in rows_by_source.items()}
    by_game = per_source.get(PRIMARY, {})

    out = []
    ratios: list[float] = []
    for g in games:
        series = by_game.get(g["gameId"], [])
        if not series:
            continue
        first, last = series[0], series[-1]
        entry = {
            "gameId": g["gameId"],
            "date": g["date"],
            "low": last["low"],
            # .get, not []: a source may publish a low without a high. Gametime does on
            # some events, and indexing crashed the whole summary on the first run with
            # two sources. A missing high is an absent field, not a failure.
            "high": last.get("high"),
            "observedDate": last["observedDate"],
            "observations": len(series),
        }
        # A delta is only meaningful with more than one day in hand. Emitting 0 on a
        # single observation would render as "flat", which is a claim we cannot make.
        if len(series) > 1:
            entry["lowFirst"] = first["low"]
            entry["lowFirstDate"] = first["observedDate"]
            entry["lowDelta"] = round(last["low"] - first["low"], 2)

        # Every other source is kept ALONGSIDE rather than blended. Two sources
        # disagreeing is the signal that one has gone wrong; averaging them destroys it.
        others = {}
        for name, bg in per_source.items():
            if name == PRIMARY:
                continue
            s2 = bg.get(g["gameId"], [])
            if s2:
                others[name] = {"low": s2[-1]["low"], "high": s2[-1].get("high"),
                                "observedDate": s2[-1]["observedDate"]}
                if last["low"]:
                    ratios.append(s2[-1]["low"] / last["low"])
        if others:
            entry["otherSources"] = others
        out.append(entry)

    all_rows = [r for rs in rows_by_source.values() for r in rs]
    days = sorted({r["observedDate"] for r in all_rows})

    cross = None
    if ratios:
        rs = sorted(ratios)
        mid = len(rs) // 2
        median = rs[mid] if len(rs) % 2 else (rs[mid - 1] + rs[mid]) / 2
        cross = {
            "comparedGames": len(ratios),
            "medianRatioToPrimary": round(median, 4),
            "minRatio": round(min(ratios), 4),
            "maxRatio": round(max(ratios), 4),
            "_comment": (
                "Each secondary source's low divided by the primary's, per game. Measured "
                "2026-09-05: Gametime sat 1.4%-16.7% above TickPick on ALL 44 games, median "
                "+6.2%, while ranking the games near-identically (Spearman +0.9944). Agreeing "
                "on ORDER but differing in LEVEL is the expected shape - they price the same "
                "demand with different inventory and fee treatment. A sudden move in this "
                "ratio means a SOURCE changed, not the market."
            ),
        }
    return {
        "_comment": (
            "Derived from the per-source stores in data/market/ by "
            "scripts/summarize_market.py. "
            "low/high are the cheapest and priciest listings in the WHOLE ARENA, all-in, "
            "on TickPick - NOT a comp for any specific seat, and NOT the channel these "
            "tickets are sold on. See the script docstring."
        ),
        "generatedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": PRIMARY,
        "sources": sorted(rows_by_source),
        "crossSource": cross,
        "priceBasis": "all_in_whole_arena",
        "isOwnChannel": False,
        "confidence": "measured_single_point" if len(days) < 2 else "measured",
        "observationDays": len(days),
        "firstObservedDate": days[0] if days else None,
        "lastObservedDate": days[-1] if days else None,
        "games": out,
    }


def self_test() -> int:
    fails = []

    def check(label, got, want):
        if got != want:
            fails.append(f"{label}: got {got!r}, want {want!r}")

    games = [{"gameId": 1, "date": "2026-10-01"}, {"gameId": 2, "date": "2026-10-03"}]

    def row(day, gid, low, high=500, ok=True):
        return {"observedDate": day, "gameId": gid, "low": low, "high": high, "ok": ok}

    # Latest observation wins, and the first is retained for the delta.
    s = summarize({"tickpick": [row("2026-09-05", 1, 80), row("2026-09-06", 1, 95)]}, games)
    g = s["games"][0]
    check("latest low", g["low"], 95)
    check("observation count", g["observations"], 2)
    check("first low retained", g["lowFirst"], 80)
    check("delta computed", g["lowDelta"], 15)
    check("two days -> measured", s["confidence"], "measured")

    # A single observation must NOT emit a delta. Rendering 0 would read as "flat",
    # which one point cannot support - this is the assertion that keeps the UI honest.
    s = summarize({"tickpick": [row("2026-09-05", 1, 80)]}, games)
    check("single point has no delta", "lowDelta" in s["games"][0], False)
    check("single point confidence", s["confidence"], "measured_single_point")

    # Out-of-order input must still resolve to the true latest.
    s = summarize({"tickpick": [row("2026-09-07", 1, 70), row("2026-09-05", 1, 80)]}, games)
    check("unsorted input picks latest by date", s["games"][0]["low"], 70)
    check("unsorted input picks earliest as first", s["games"][0]["lowFirst"], 80)

    # Failed and unpriced rows must not become observations.
    s = summarize({"tickpick": [row("2026-09-05", 1, None, ok=True), row("2026-09-06", 1, 60, ok=False)]}, games)
    check("null and failed rows excluded", s["games"], [])

    # Games with no data are omitted rather than emitted as zero.
    s = summarize({"tickpick": [row("2026-09-05", 1, 80)]}, games)
    check("only games with data appear", [g["gameId"] for g in s["games"]], [1])

    # Provenance flags the app relies on to caveat the numbers.
    check("flagged as not our channel", s["isOwnChannel"], False)
    check("price basis recorded", s["priceBasis"], "all_in_whole_arena")

    # --- multi-source behaviour ---
    two = summarize(
        {"tickpick": [row("2026-09-05", 1, 100), row("2026-09-05", 2, 50)],
         "gametime": [row("2026-09-05", 1, 106), row("2026-09-05", 2, 55)]},
        games,
    )
    check("primary drives the headline low", two["games"][0]["low"], 100)
    check("secondary kept alongside, not blended",
          two["games"][0]["otherSources"]["gametime"]["low"], 106)
    check("sources listed", two["sources"], ["gametime", "tickpick"])
    check("cross-source compares both games", two["crossSource"]["comparedGames"], 2)
    # 106/100 = 1.06 and 55/50 = 1.10 -> median 1.08.
    check("median ratio", two["crossSource"]["medianRatioToPrimary"], 1.08)
    check("min ratio", two["crossSource"]["minRatio"], 1.06)
    check("max ratio", two["crossSource"]["maxRatio"], 1.1)

    # A game the secondary has not priced must not invent a ratio for it.
    partial = summarize(
        {"tickpick": [row("2026-09-05", 1, 100), row("2026-09-05", 2, 50)],
         "gametime": [row("2026-09-05", 1, 106)]},
        games,
    )
    check("ratio only where both sources have data", partial["crossSource"]["comparedGames"], 1)
    check("unmatched game has no otherSources", "otherSources" in partial["games"][1], False)

    # A secondary source alone must not become the headline.
    only = summarize({"gametime": [row("2026-09-05", 1, 106)]}, games)
    check("no primary data -> no games summarised", only["games"], [])
    check("no primary data -> no cross-source", only["crossSource"], None)

    # A source publishing a low with no high must not crash the summary. Gametime does
    # this on some events, and it took down the first two-source run.
    nohigh = summarize(
        {"tickpick": [{"observedDate": "2026-09-05", "gameId": 1, "low": 80, "ok": True}],
         "gametime": [{"observedDate": "2026-09-05", "gameId": 1, "low": 85, "ok": True}]},
        games,
    )
    check("missing high on primary is absent, not fatal", nohigh["games"][0]["high"], None)
    check("missing high on secondary is absent, not fatal",
          nohigh["games"][0]["otherSources"]["gametime"]["high"], None)
    check("low still summarised without a high", nohigh["games"][0]["low"], 80)

    check("empty input", summarize({"tickpick": []}, games)["games"], [])
    check("empty input has no dates", summarize({"tickpick": []}, games)["lastObservedDate"], None)

    for f in fails:
        print(f"  FAIL {f}", file=sys.stderr)
    print(f"self-test: {'FAILED' if fails else 'passed'} ({len(fails)} failure(s))")
    return 1 if fails else 0


def main() -> int:
    if "--self-test" in sys.argv:
        return self_test()
    rows_by_source = {name: load_rows(path) for name, path in STORES}
    games = json.loads(SCHEDULE.read_text())["games"]
    summary = summarize(rows_by_source, games)
    for name, rows in rows_by_source.items():
        print(f"  {name}: {len(rows)} rows")
    DEST.parent.mkdir(parents=True, exist_ok=True)
    DEST.write_text(json.dumps(summary, indent=2) + "\n")
    print(f"days: {summary['observationDays']}  "
          f"games with data: {len(summary['games'])}/{len(games)}")
    if summary.get("crossSource"):
        c = summary["crossSource"]
        print(f"cross-source: {c['comparedGames']} games, secondary/primary median "
              f"{c['medianRatioToPrimary']:.3f} "
              f"(range {c['minRatio']:.3f}-{c['maxRatio']:.3f})")
    print(f"confidence: {summary['confidence']}")
    print(f"wrote {DEST.relative_to(ROOT)}")
    if not summary["games"]:
        print("\nNo priced games - the app will show no market context.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
