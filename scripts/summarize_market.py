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
STORE = ROOT / "data" / "market" / "tickpick.jsonl"
SCHEDULE = ROOT / "data" / "schedule.json"
DEST = ROOT / "data" / "market" / "summary.json"


def load_rows(path: pathlib.Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]


def summarize(rows: list[dict], games: list[dict]) -> dict:
    by_game: dict[int, list[dict]] = {}
    for r in rows:
        if r.get("ok") and r.get("low") is not None:
            by_game.setdefault(r["gameId"], []).append(r)

    out = []
    for g in games:
        series = sorted(by_game.get(g["gameId"], []), key=lambda r: r["observedDate"])
        if not series:
            continue
        first, last = series[0], series[-1]
        entry = {
            "gameId": g["gameId"],
            "date": g["date"],
            "low": last["low"],
            "high": last["high"],
            "observedDate": last["observedDate"],
            "observations": len(series),
        }
        # A delta is only meaningful with more than one day in hand. Emitting 0 on a
        # single observation would render as "flat", which is a claim we cannot make.
        if len(series) > 1:
            entry["lowFirst"] = first["low"]
            entry["lowFirstDate"] = first["observedDate"]
            entry["lowDelta"] = round(last["low"] - first["low"], 2)
        out.append(entry)

    days = sorted({r["observedDate"] for r in rows})
    return {
        "_comment": (
            "Derived from data/market/tickpick.jsonl by scripts/summarize_market.py. "
            "low/high are the cheapest and priciest listings in the WHOLE ARENA, all-in, "
            "on TickPick - NOT a comp for any specific seat, and NOT the channel these "
            "tickets are sold on. See the script docstring."
        ),
        "generatedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "tickpick",
        "priceBasis": "all_in_whole_arena",
        "isOwnChannel": False,
        "confidence": "measured_single_point" if len(days) < 2 else "measured",
        "observationDays": len(days),
        "firstObservedDate": days[0] if days else None,
        "lastObservedDate": days[-1] if days else None,
        "games": out,
    }


def main() -> int:
    rows = load_rows(STORE)
    games = json.loads(SCHEDULE.read_text())["games"]
    summary = summarize(rows, games)
    DEST.parent.mkdir(parents=True, exist_ok=True)
    DEST.write_text(json.dumps(summary, indent=2) + "\n")
    print(f"rows: {len(rows)}  days: {summary['observationDays']}  "
          f"games with data: {len(summary['games'])}/{len(games)}")
    print(f"confidence: {summary['confidence']}")
    print(f"wrote {DEST.relative_to(ROOT)}")
    if not summary["games"]:
        print("\nNo priced games - the app will show no market context.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
