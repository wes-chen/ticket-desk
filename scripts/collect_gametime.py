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
import tempfile
import time
from datetime import datetime, timezone
from unittest.mock import patch

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


def join(events: dict[str, dict], schedule: dict, today: str
         ) -> tuple[list[dict], list[str], list[str]]:
    """Returns (rows, problems, notes). Problems are fatal; notes are expected absences.

    `today` is the venue-local date (ms.venue_today()). It is a required argument rather
    than read from the clock here, so the self-test's fixed fixture dates never silently
    change meaning as the real season passes them.

    PLAYED GAMES (ops#351). Gametime delists a game once it has been played, so a played
    game missing from the page is the season running forward, not a join failure. It leaves
    the required set - it is not carried forward, and no price is invented for it. A game
    NOT yet played that is missing still refuses the whole run. A played game that is
    still listed joins like any other.
    """
    games = schedule["games"]
    problems: list[str] = []
    notes: list[str] = []
    out = []
    matched = set()

    for g in games:
        key = g["startTimeUTC"].replace("Z", "")
        e = events.get(key)
        if e is None:
            if ms.is_played(g, today):
                notes.append(f"already played: {g['date']} vs {g['opponent']['abbrev']}")
                continue
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
    return out, problems, notes


def collect(store: pathlib.Path, raw_dir: pathlib.Path | None) -> int:
    schedule = json.loads(SCHEDULE.read_text())
    today = ms.venue_today()
    # SEASON-OVER EXIT, and what it does to PLAYOFFS (ops#371). "Season" here means
    # data/schedule.json, which holds whatever club-schedule-season listed at the last
    # fetch - today 42 regular and 2 preseason home games, NO playoff games, because none
    # are scheduled. So this exit fires from the day after the last regular-season home
    # game and stops collection THROUGH any playoff home games: playoff listings are not
    # collected. Written down rather than changed - collecting playoff games is a
    # separate question and was out of scope where this was found. When the NHL API
    # lists playoff home games, fetch_schedule.py EXCLUDES them from data/schedule.json "games"
    # (recording them under "excludedPlayoffGames", which nothing here reads),
    # logs each one, and exits 0 (ops#377, measured against a captured response), so the
    # schedule never gains them and this exit still fires after the last regular-season
    # game. Making it wait for playoffs means committing them, which is the collection
    # decision - not a fix to this exit.
    if schedule["games"] and all(ms.is_played(g, today) for g in schedule["games"]):
        print("season over: all scheduled home games have been played; nothing to collect")
        return 0
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
    rows, problems, notes = join(home, schedule, today)

    now = datetime.now(timezone.utc)
    observed_at = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    observed_date = now.strftime("%Y-%m-%d")
    for r in rows:
        r["observedAt"] = observed_at
        r["observedDate"] = observed_date

    print(f"page: {len(html):,}B  ld+json events: {len(events)}  "
          f"at {VENUE} after dedupe: {len(home)}")
    print(f"joined: {len(rows)}/{len(schedule['games'])} home games")
    if notes:
        print(f"already played: {len(notes)} game(s) delisted after being played "
              f"(expected - no longer required)")

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
    # Fixture games are 2026-09-22 and 2026-10-01, so "today" is pinned before both.
    T0 = "2026-09-01"
    rows, probs, notes = join(home, sched, T0)
    check("clean join has no problems", probs, [])
    check("both games joined", len(rows), 2)
    check("row carries the game date, not the UTC date", rows[0]["date"], "2026-09-22")
    check("row has a price", isinstance(rows[0]["low"], (int, float)), True)

    # Each validation key must fire when broken.
    bad = json.loads(json.dumps(sched))
    bad["games"][0]["opponent"] = {"abbrev": "BOS", "name": "Boston Bruins"}
    check("opponent mismatch caught",
          any("OPPONENT MISMATCH" in p for p in join(home, bad, T0)[1]), True)

    bad = json.loads(json.dumps(sched))
    bad["games"][0]["startTimeUTC"] = "2026-09-22T02:00:00Z"  # local date, the wrong key
    check("a local-date join key is caught as a missing event",
          any("NO GAMETIME EVENT" in p for p in join(home, bad, T0)[1]), True)

    check("orphan event caught",
          any("ORPHAN" in p for p in join(home, {"games": [sched["games"][0]]}, T0)[1]), True)

    # ---- played games (ops#351) ----
    # The live failure: a game already played has been delisted, and the whole run was
    # refused as though the join were broken. Model it as the real page did - the played
    # game's event is simply gone.
    delisted = {k: v for k, v in home.items() if k != "2026-09-23T02:00:00"}
    rows, probs, notes = join(delisted, sched, "2026-09-27")
    check("a played-and-delisted game does not refuse the run", probs, [])
    check("the remaining game still joins", [r["gameId"] for r in rows], [2])
    check("the played game is reported as a note", len(notes), 1)
    check("and the note names it", bool(notes) and "2026-09-22" in notes[0], True)
    # The same page, but the game has NOT been played yet: that is a real gap and the
    # refuse-the-whole-run guard must still fire.
    rows, probs, notes = join(delisted, sched, "2026-09-01")
    check("an unplayed missing game still refuses the run",
          any("NO GAMETIME EVENT" in p and "2026-09-22" in p for p in probs), True)
    # Game DAY is not "played": the boundary is the day after.
    rows, probs, notes = join(delisted, sched, "2026-09-22")
    check("a game missing on its own game day still refuses the run",
          any("NO GAMETIME EVENT" in p for p in probs), True)
    # A played game the source still lists joins normally - exclusion never drops data.
    rows, probs, notes = join(home, sched, "2026-09-27")
    check("a played game still listed still joins", (len(rows), probs, notes), (2, [], []))
    rows, probs, notes = join({}, sched, "2026-10-02")
    check("an empty page after the season has no join problems", probs, [])
    check("played games are still reported", len(notes), 2)
    with tempfile.TemporaryDirectory() as td:
        schedule_path = pathlib.Path(td) / "schedule.json"
        store_path = pathlib.Path(td) / "market.jsonl"
        schedule_path.write_text(json.dumps(sched))
        with patch.dict(globals(), {"SCHEDULE": schedule_path}):
            with patch.object(ms, "venue_today", return_value="9999-01-01"), \
                 patch.object(ms, "get", return_value=("", "synthetic outage")) as fetch:
                check("the collector exits cleanly after the season", collect(store_path, None), 0)
                check("the season-end collector skips the fetch", fetch.call_count, 0)
            # One game is played; the last game is still required on its own date.
            for today in ("2026-09-27", sched["games"][-1]["date"]):
                with patch.object(ms, "venue_today", return_value=today), \
                     patch.object(ms, "get", return_value=("", "synthetic outage")) as fetch:
                    check(f"outage with unplayed game on {today} fails",
                          collect(store_path, None) != 0, True)
                    check(f"outage with unplayed game on {today} fetches once",
                          fetch.call_count, 1)
            # ops#371: which date collect() hands to join(). Every join() test above
            # passes a date by hand, so collect() could pass any date at all and they
            # would stay green. A far-future one makes every missing game read as
            # "already played" - a mid-season hole passing silently, the ops#351 class -
            # and a far-past one refuses every run once a game has been delisted. So
            # drive collect() itself over a page with game 1 delisted, on both sides of
            # its date, and also record the date join() actually receives.
            page = "".join(
                f'<script type="application/ld+json">{json.dumps(e)}</script>'
                for e in events if e.get("startDate") != "2026-09-23T02:00:00")
            seen = []
            real_join = join

            def spy(ev, sc, today):
                seen.append(today)
                return real_join(ev, sc, today)

            for today, want_ok in (("2026-09-01", False), ("2026-09-27", True)):
                seen.clear()
                with patch.dict(globals(), {"join": spy}), \
                     patch.object(ms, "venue_today", return_value=today), \
                     patch.object(ms, "get", return_value=(page, None)):
                    rc = collect(store_path, None)
                check(f"collect() passes its own run date to join on {today}", seen, [today])
                check(f"collect() with game 1 delisted on {today} "
                      f"{'succeeds' if want_ok else 'refuses the run'}", rc == 0, want_ok)
            schedule_path.write_text(json.dumps({"games": []}))
            with patch.object(ms, "venue_today", return_value="9999-01-01"), \
                 patch.object(ms, "get", return_value=("", "synthetic outage")) as fetch:
                check("empty schedule does not take the season-end exit",
                      collect(store_path, None) != 0, True)
                check("empty schedule still fetches once", fetch.call_count, 1)

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
