#!/usr/bin/env python3
"""Collect per-event price ranges from Gametime over plain HTTP.

The project's SECOND comp source, and the point of a second one is not more price data -
it is that two sources catch each other going stale. This codebase has produced silent
wrongness repeatedly, and a single source that quietly starts serving nonsense is
indistinguishable from a market that moved.

WHY GAMETIME, AND WHY IT TOOK THIS LONG. ops#4 never got a verdict on it because both
probes pointed at URLs that do not exist - a 404 that reads identically to a block. The
real performer page came out of gametime.co/sitemap/sport-performers.xml, which
robots.txt advertises:

    https://gametime.co/san-jose-sharks-tickets/performers/nhlsjs

Measured 2026-09-05: 200, 3.1MB, 62 distinct prices, 137 ld+json blocks carrying
schema.org AggregateOffer - the same shape TickPick serves, so this collector is the
same shape too.

Gametime skews last-minute, which makes it disproportionately interesting near the T-48h
deadline, where the model is weakest and TickPick's whole-arena low moves least.

SCOPE. Not seat-level: robots.txt says `Disallow: /*listings`, the same boundary TickPick
sets on /ajax/. So this is a whole-event min/max, and ops#11's comp work still needs the
manual-paste route in ops#23.

Three structural quirks of this page, all measured rather than assumed:
  * every event's ld+json block appears TWICE, byte-identical - deduped by startDate
  * the performer page lists AWAY games too - filtered on venue
  * two entries are season-ticket packages with no price - filtered on lowPrice
  * `startDate` has no timezone suffix but is UTC, and equals our schedule's
    startTimeUTC minus the "Z". That is the join key; joining on local date would be
    off by one for every night game.
"""

import argparse
import json
import pathlib
import re
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import market_store as ms  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCHEDULE = ROOT / "data" / "schedule.json"
STORE = ROOT / "data" / "market" / "gametime.jsonl"

PERFORMER_URL = "https://gametime.co/san-jose-sharks-tickets/performers/nhlsjs"
VENUE = "SAP Center"
LDJSON_RE = re.compile(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', re.S)


def sports_events(html: str) -> list[dict]:
    """Every SportsEvent/Event block on the page. Per-block parse failures are skipped
    rather than discarding the whole page."""
    out = []
    for raw in LDJSON_RE.findall(html):
        try:
            d = json.loads(raw)
        except json.JSONDecodeError:
            continue
        for item in (d if isinstance(d, list) else [d]):
            if isinstance(item, dict) and item.get("@type") in ("SportsEvent", "Event"):
                out.append(item)
    return out


def venue_name(e: dict) -> str | None:
    loc = e.get("location") or {}
    if isinstance(loc, list):
        loc = loc[0] if loc else {}
    return loc.get("name") if isinstance(loc, dict) else None


def offer(e: dict) -> dict:
    o = e.get("offers") or {}
    if isinstance(o, list):
        o = o[0] if o else {}
    return {
        "low": o.get("lowPrice"),
        "high": o.get("highPrice"),
        "currency": o.get("priceCurrency"),
        "availability": (o.get("availability") or "").rsplit("/", 1)[-1] or None,
    }


def away_name(e: dict) -> str:
    a = e.get("awayTeam")
    if isinstance(a, dict):
        return a.get("name") or ""
    return a or ""


def home_events(events: list[dict]) -> dict[str, dict]:
    """Venue-filtered, priced, deduped by startDate. Keyed by startDate."""
    uniq: dict[str, dict] = {}
    for e in events:
        if venue_name(e) != VENUE:
            continue
        if offer(e)["low"] is None:
            # Season-ticket packages carry no price. Not an error, not a game.
            continue
        start = e.get("startDate")
        if start:
            uniq.setdefault(start, e)
    return uniq


def join(events: dict[str, dict], schedule: dict) -> tuple[list[dict], list[str]]:
    games = schedule["games"]
    problems: list[str] = []
    out = []
    matched = set()

    for g in games:
        key = g["startTimeUTC"].replace("Z", "")
        e = events.get(key)
        if e is None:
            problems.append(f"NO GAMETIME EVENT: {g['date']} vs {g['opponent']['abbrev']} "
                            f"(startTimeUTC {g['startTimeUTC']})")
            continue
        matched.add(key)

        # Second key. A UTC timestamp collision is unlikely but the opponent check costs
        # nothing and is the discipline fetch_schedule.py established.
        last = (g["opponent"]["name"] or "").split()[-1].lower()
        got = away_name(e)
        if last and last not in got.lower():
            problems.append(f"OPPONENT MISMATCH on {g['date']}: schedule says "
                            f"{g['opponent']['name']!r}, Gametime away team is {got!r}")

        o = offer(e)
        row = {
            "eventId": (e.get("url") or key).rstrip("/").rsplit("/", 1)[-1],
            "gameId": g["gameId"],
            "date": g["date"],
            "source": "gametime",
            "ok": True,
        }
        row.update({k: v for k, v in o.items() if v is not None})
        out.append(row)

    for key in events:
        if key not in matched:
            problems.append(f"ORPHAN GAMETIME EVENT at {key} "
                            f"({(events[key].get('name') or '')[:50]!r}) matches no home game")
    return out, problems


def collect(store: pathlib.Path, raw_dir: pathlib.Path | None) -> int:
    schedule = json.loads(SCHEDULE.read_text())
    html, err = ms.get(PERFORMER_URL)
    if err:
        print(f"\nGametime fetch failed: {err}", file=sys.stderr)
        if err.startswith("http 403"):
            print("403 - Gametime was measured reachable over plain HTTP from both a "
                  "residential and an Actions IP on 2026-09-05. A 403 means that changed; "
                  "re-run scripts/probe_sources.py before assuming a bug.", file=sys.stderr)
        return 1

    events = sports_events(html)
    home = home_events(events)
    rows, problems = join(home, schedule)

    now = datetime.now(timezone.utc)
    observed_at = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    observed_date = now.strftime("%Y-%m-%d")
    for r in rows:
        r["observedAt"] = observed_at
        r["observedDate"] = observed_date

    print(f"page: {len(html):,}B  ld+json events: {len(events)}  "
          f"at {VENUE} after dedupe: {len(home)}")
    print(f"joined: {len(rows)}/{len(schedule['games'])} home games")

    if problems:
        print(f"\n{len(problems)} PROBLEM(S):", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        print("\nNothing written - a wrong join would attribute one game's prices to "
              "another all season.", file=sys.stderr)
        return 1

    merged = ms.commit_rows(store, rows, total_failure=not rows)
    if raw_dir and rows:
        raw_dir.mkdir(parents=True, exist_ok=True)
        p = raw_dir / f"gametime-{observed_date}.json"
        p.write_text(json.dumps(
            {"observedAt": observed_at,
             "events": [{"startDate": k, "ldjson": v} for k, v in sorted(home.items())]},
            indent=2) + "\n")
        print(f"raw -> {p} ({p.stat().st_size:,}B, artifact only, never git)")
    print(f"store now holds: {len(merged)} rows -> {ms.rel(store, ROOT)}")
    return 0


def self_test() -> int:
    fails = []

    def check(label, got, want):
        if got != want:
            fails.append(f"{label}: got {got!r}, want {want!r}")

    fx = json.loads((ROOT / "tests" / "fixtures" / "gametime_ldjson.json").read_text())
    events = fx["events"]

    check("fixture carries away and home games", len(events) >= 3, True)
    home = home_events(events)
    check("venue filter and dedupe leave the SAP games", len(home), 2)
    check("away game excluded", all(venue_name(e) == VENUE for e in home.values()), True)

    # The season-ticket package has no price and must not become a game.
    check("priceless package excluded",
          any("Season Tickets" in (e.get("name") or "") for e in home.values()), False)

    o = offer(list(home.values())[0])
    check("lowPrice parsed", isinstance(o["low"], (int, float)), True)
    check("availability normalised", o["availability"], "InStock")
    check("missing offers is not a crash", offer({})["low"], None)
    check("offers as a list", offer({"offers": [{"lowPrice": 7}]})["low"], 7)

    # The join is on startTimeUTC, not local date. Getting this wrong is off-by-one for
    # every night game, which is all of them.
    sched = {"games": [
        {"gameId": 1, "date": "2026-09-22", "startTimeUTC": "2026-09-23T02:00:00Z",
         "opponent": {"abbrev": "VGK", "name": "Vegas Golden Knights"}},
        {"gameId": 2, "date": "2026-10-01", "startTimeUTC": "2026-10-02T02:00:00Z",
         "opponent": {"abbrev": "FLA", "name": "Florida Panthers"}},
    ]}
    rows, probs = join(home, sched)
    check("clean join has no problems", probs, [])
    check("both games joined", len(rows), 2)
    check("row carries the game date, not the UTC date", rows[0]["date"], "2026-09-22")
    check("row has a price", isinstance(rows[0]["low"], (int, float)), True)

    # Each validation key must fire when broken.
    bad = json.loads(json.dumps(sched))
    bad["games"][0]["opponent"] = {"abbrev": "BOS", "name": "Boston Bruins"}
    check("opponent mismatch caught",
          any("OPPONENT MISMATCH" in p for p in join(home, bad)[1]), True)

    bad = json.loads(json.dumps(sched))
    bad["games"][0]["startTimeUTC"] = "2026-09-22T02:00:00Z"  # local date, the wrong key
    check("a local-date join key is caught as a missing event",
          any("NO GAMETIME EVENT" in p for p in join(home, bad)[1]), True)

    check("orphan event caught",
          any("ORPHAN" in p for p in join(home, {"games": [sched["games"][0]]})[1]), True)

    # ld+json extraction must survive a junk sibling block.
    noisy = ('<script type="application/ld+json">{"@type":"WebSite"}</script>'
             '<script type="application/ld+json">not json</script>'
             f'<script type="application/ld+json">{json.dumps(events[0])}</script>')
    check("junk sibling blocks do not hide the event", len(sports_events(noisy)), 1)
    check("no blocks -> no events", sports_events("<html></html>"), [])

    for f in fails:
        print(f"  FAIL {f}", file=sys.stderr)
    print(f"self-test: {'FAILED' if fails else 'passed'} ({len(fails)} failure(s))")
    return 1 if fails else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--store", type=pathlib.Path, default=STORE)
    ap.add_argument("--raw-dir", type=pathlib.Path, default=ROOT / "raw-out")
    args = ap.parse_args()
    if args.self_test:
        return self_test()
    return collect(args.store, args.raw_dir)


if __name__ == "__main__":
    sys.exit(main())
