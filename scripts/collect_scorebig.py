#!/usr/bin/env python3
"""Collect per-event price ranges from ScoreBig over plain HTTP.

The project's FOURTH comp source. More sources are not for more price data - they are so
that sources catch each other going stale, because a single source quietly serving
nonsense is indistinguishable from a market that moved.

WHY IT WAS MISSING. ops#26 recorded ScoreBig as a 404. The URL had been guessed, and a
guessed 404 reads exactly like a block - the same mistake that hid Gametime through all of
ops#4 and TicketNetwork through ops#26. Its sitemaps resolved the real pages in minutes
(ops#33). Three candidate pages exist, and the most specific one wins:

    /performers/san-jose-sharks-904                       11 SAP events (mixed with away)
    /venues/sap-center-31                                  7 Sharks events (mixed with other acts)
    /performers/san-jose-sharks-904/venues/san-jose-sap-center-31   <- 19, all ours

THE OFFSET IS WRONG, AND THIS IS THE WHOLE STORY OF THIS COLLECTOR.

`startDate` looks authoritative: "2026-09-22T19:00:00-08:00". It is not. ScoreBig stamps
**-08:00 on every month** - September, October, November, December alike. September in
California is PDT, which is -07:00; TicketNetwork publishes -07:00 for the same 19:00 puck
drop. So the offset is a fixed year-round constant that is simply wrong for the DST half
of the season.

Measured, on the same page, same minute:

    join on their declared offset, normalised to UTC ->  10/44 games
    join on LOCAL WALL-CLOCK time, offset discarded   ->  19/19 events

Trusting the offset does not fail loudly. It silently matches only the PST games and drops
every PDT one, which looks exactly like ordinary partial coverage - the shape this project
has already been fooled by three times. So this collector **joins on local wall clock and
ignores the declared offset entirely.**

Note this is the OPPOSITE of collect_ticketnetwork.py, where the offset IS authoritative
and a local-date join would be wrong. Two sources, two opposite correct answers, neither
inferable from the other. Do not "unify" these two joins.

COVERAGE is a rolling window: 19 of 44 home games, zero orphans. Like TicketNetwork, and
unlike TickPick, a late-season game simply not being listed is a publisher's horizon
rather than a fault. See the `rolling` flag in check_data_freshness.py.
"""

import argparse
import json
import pathlib
import re
import sys
import tempfile
from datetime import datetime, timezone
from unittest.mock import patch
from zoneinfo import ZoneInfo

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import market_store as ms  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCHEDULE = ROOT / "data" / "schedule.json"
STORE = ROOT / "data" / "market" / "scorebig.jsonl"

EVENT_URL = ("https://www.scorebig.com/performers/san-jose-sharks-904"
             "/venues/san-jose-sap-center-31")
VENUE_PREFIX = "SAP Center"
ARENA_TZ = ZoneInfo("America/Los_Angeles")
LDJSON_RE = re.compile(r'<script[^>]*application/ld\+json[^>]*>(.*?)</script>', re.S)


def sports_events(html: str) -> list[dict]:
    """Every SportsEvent/Event block. A per-block parse failure is skipped rather than
    discarding the page - one malformed block must not cost eighteen good ones."""
    out = []
    for raw in LDJSON_RE.findall(html):
        try:
            d = json.loads(raw.strip())
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


def num(v):
    """Coerce a price to a number, or None.

    ScoreBig serves prices as STRINGS - "15.20", not 15.2 - where TickPick, Gametime and
    TicketNetwork all serve numbers. Storing the string would not fail here; it would fail
    downstream in summarize_market.py, which subtracts lows to compute a daily delta and
    would either crash or, worse, compare strings lexically. "9.00" > "15.20" is true for
    strings and false for money.

    So the coercion happens at the boundary, once, where the shape is known - rather than
    everywhere the value is later used.
    """
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return v
    try:
        return float(str(v).replace(",", "").lstrip("$"))
    except ValueError:
        return None


def offer(e: dict) -> dict:
    o = e.get("offers") or {}
    if isinstance(o, list):
        o = o[0] if o else {}
    return {
        "low": num(o.get("lowPrice")),
        "high": num(o.get("highPrice")),
        "currency": o.get("priceCurrency"),
        "availability": (o.get("availability") or "").rsplit("/", 1)[-1] or None,
    }


def away_name(e: dict) -> str:
    a = e.get("awayTeam")
    if isinstance(a, dict):
        return a.get("name") or ""
    return a or ""


def wall_key(start: str | None) -> str | None:
    """Local wall-clock key, "YYYY-MM-DDTHH:MM", with the declared offset DISCARDED.

    Deliberately a string slice rather than a parse-and-convert. Parsing would invite
    honouring the tzinfo, which is the bug: ScoreBig's offset is a fixed -08:00 all year
    and is wrong for every PDT date. The wall time it prints is correct; only the offset
    it appends is not.

    Returns None for a date-only string ("2026-09-30"), which is what season-ticket
    packages carry - they are filtered on price anyway, and this is the second guard.
    """
    if not start or len(start) < 16 or start[10] != "T":
        return None
    return start[:16]


def schedule_wall(game: dict) -> str:
    """The same key, derived from our own schedule's authoritative UTC timestamp."""
    dt = datetime.fromisoformat(game["startTimeUTC"].replace("Z", "+00:00"))
    return dt.astimezone(ARENA_TZ).strftime("%Y-%m-%dT%H:%M")


def home_events(events: list[dict]) -> dict[str, dict]:
    """Venue-filtered, priced, deduped. Keyed by local wall clock."""
    uniq: dict[str, dict] = {}
    for e in events:
        if not (venue_name(e) or "").startswith(VENUE_PREFIX):
            continue
        if offer(e)["low"] is None:
            continue  # season-ticket packages carry no price. Not an error, not a game.
        key = wall_key(e.get("startDate"))
        if key:
            uniq.setdefault(key, e)
    return uniq


def join(events: dict[str, dict], schedule: dict, today: str
         ) -> tuple[list[dict], list[str], list[str]]:
    """Returns (rows, problems, notes). Problems are fatal; notes are expected coverage.

    `today` is the venue-local date (ms.venue_today()), passed in rather than read from the
    clock so the self-test's fixed fixture dates never change meaning as the season passes.

    PLAYED GAMES (ops#351). A marketplace delists a game once it has been played. The
    horizon is the LATEST matched game, so every played game sits inside it, and a played
    game's absence was read as a GAP INSIDE COVERAGE - which refused the whole run every
    day from 2026-09-23. A played game now leaves the required set before the horizon is
    consulted: no row, no invented price, just a note. A game NOT yet played that is
    missing inside the horizon still refuses the run. A played game still listed joins.

    The horizon rule, as in collect_ticketnetwork.py: this source publishes a window, so a
    game beyond the latest one it lists is unlisted rather than missing. A gap at or before
    that horizon is real and stays fatal.
    """
    games = schedule["games"]
    problems: list[str] = []
    notes: list[str] = []
    rows = []
    matched = set()

    # TOTAL FAILURE GUARD. If the page yielded NO events at all, the horizon is None and
    # every game falls through to "beyond horizon" - so a complete scrape failure would be
    # printed as benign coverage and exit 0. Found by review, reproduced: a markup change,
    # a renamed venue, or a datacenter IP behaving differently from the residential one
    # would all land here and read as "rolling window, expected".
    #
    # That is the exact failure this project keeps writing guards against - a confident,
    # well-formatted wrong answer. collect_tickpick.py has always had an explicit
    # total-failure check; the horizon mechanism introduced this blind spot only for the
    # rolling sources, so it is fixed where the horizon lives.
    #
    # Once all games have been played, an empty page is expected. Until then, an empty
    # page still means the scrape failed rather than that its horizon moved.
    if any(not ms.is_played(g, today) for g in games) and not events:
        problems.append("TOTAL FAILURE: the page parsed but yielded no usable events at "
                        "all, so nothing could be matched. This is a scrape failure, not "
                        "a coverage horizon - re-run the probe before assuming the source "
                        "simply has no listings.")
        return [], problems, []

    by_wall = {schedule_wall(g): g for g in games}
    horizon = max((w for w in by_wall if w in events), default=None)

    for wall, g in sorted(by_wall.items()):
        e = events.get(wall)
        if e is None:
            if ms.is_played(g, today):
                notes.append(f"already played: {g['date']} vs {g['opponent']['abbrev']}")
                continue
            if horizon and wall <= horizon:
                problems.append(f"GAP INSIDE COVERAGE: {g['date']} vs "
                                f"{g['opponent']['abbrev']} is missing but earlier and "
                                f"later games are present")
            else:
                notes.append(f"beyond horizon: {g['date']} vs {g['opponent']['abbrev']}")
            continue
        matched.add(wall)

        # Second independent key. A wall-clock collision is impossible for one venue, but
        # the opponent check is the discipline fetch_schedule.py established and a wrong
        # join would misattribute a game's prices for the whole season.
        last = (g["opponent"]["name"] or "").split()[-1].lower()
        got = away_name(e)
        if last and got and last not in got.lower():
            problems.append(f"OPPONENT MISMATCH on {g['date']}: schedule says "
                            f"{g['opponent']['name']!r}, ScoreBig away team is {got!r}")

        o = offer(e)
        row = {
            "eventId": (e.get("url") or wall).rstrip("/").rsplit("/", 1)[-1],
            "gameId": g["gameId"],
            "date": g["date"],
            "source": "scorebig",
            "ok": True,
        }
        row.update({k: v for k, v in o.items() if v is not None})
        rows.append(row)

    for wall in events:
        if wall not in matched:
            problems.append(f"ORPHAN EVENT at {wall} "
                            f"({(events[wall].get('name') or '')[:50]!r}) matches no home "
                            f"game - the join is wrong, not merely incomplete")
    return rows, problems, notes


def collect(store: pathlib.Path, raw_dir: pathlib.Path | None) -> int:
    schedule = json.loads(SCHEDULE.read_text())
    today = ms.venue_today()
    if schedule["games"] and all(ms.is_played(g, today) for g in schedule["games"]):
        print("season over: all scheduled home games have been played; nothing to collect")
        return 0
    html, err = ms.get(EVENT_URL)
    if err:
        print(f"\nScoreBig fetch failed: {err}", file=sys.stderr)
        if err.startswith("http 4"):
            print("Measured 200 over plain HTTP from a residential IP on 2026-09-05 "
                  "(ops#33). A 4xx means that changed, OR that the page moved - "
                  "re-resolve it from dynamic-sitemap-venues-performer-0.xml before "
                  "assuming a block. A guessed URL's 404 is what hid this source through "
                  "all of ops#26.", file=sys.stderr)
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
          f"at {VENUE_PREFIX} after dedupe: {len(home)}")
    print(f"joined: {len(rows)}/{len(schedule['games'])} home games "
          f"(local wall clock - ScoreBig's declared offset is wrong, see docstring)")
    played = [n for n in notes if n.startswith("already played")]
    beyond = [n for n in notes if n.startswith("beyond horizon")]
    if played:
        print(f"already played: {len(played)} game(s) delisted after being played "
              f"(expected - no longer required)")
    if beyond:
        print(f"coverage horizon: {len(beyond)} game(s) not yet listed (rolling window, "
              f"expected)")

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
        p = raw_dir / f"scorebig-{observed_date}.json"
        p.write_text(json.dumps(
            {"observedAt": observed_at,
             "events": [{"wallClock": k, "ldjson": v} for k, v in sorted(home.items())]},
            indent=2) + "\n")
        print(f"raw -> {p} ({p.stat().st_size:,}B, artifact only, never git)")
    print(f"store now holds: {len(merged)} rows -> {ms.rel(store, ROOT)}")
    return 0


def self_test() -> int:
    fails = []

    def check(label, got, want):
        if got != want:
            fails.append(f"{label}: got {got!r}, want {want!r}")

    fx = json.loads((ROOT / "tests" / "fixtures" / "scorebig_ldjson.json").read_text())
    evs = fx["events"]

    # ---- the offset bug, which is the reason this collector exists in this shape ----
    check("wall key discards the declared offset",
          wall_key("2026-09-22T19:00:00-08:00"), "2026-09-22T19:00")
    check("a wrong offset does not change the key",
          wall_key("2026-09-22T19:00:00-08:00"), wall_key("2026-09-22T19:00:00-07:00"))
    check("date-only package start yields no key", wall_key("2026-09-30"), None)
    check("missing start is not a crash", wall_key(None), None)
    check("a short string is not sliced into nonsense", wall_key("2026-09-22"), None)
    check("a non-ISO string is refused", wall_key("Sep 22 2026 7pm"), None)

    # Our own schedule's UTC converts to the arena's wall clock across the DST boundary.
    # These two are the whole point: a September game is PDT and a December game is PST,
    # and both must land on 19:00 local.
    check("PDT game converts to local 19:00",
          schedule_wall({"startTimeUTC": "2026-09-23T02:00:00Z"}), "2026-09-22T19:00")
    check("PST game converts to local 19:00",
          schedule_wall({"startTimeUTC": "2026-12-11T03:00:00Z"}), "2026-12-10T19:00")

    # ---- filters ----
    home = home_events(evs)
    check("package rejected on price, duplicate deduped", len(home), 2)
    check("keys are local wall clock",
          sorted(home), ["2026-09-22T19:00", "2026-09-24T19:00"])

    # ScoreBig serves prices as strings. Coercion happens at the boundary.
    check("string price coerced to float", num("15.20"), 15.2)
    check("a number passes through", num(16), 16)
    check("thousands separator survives", num("1,826.00"), 1826.0)
    check("a currency symbol survives", num("$70"), 70.0)
    check("None stays None", num(None), None)
    check("garbage becomes None rather than crashing", num("call us"), None)
    # Lexical comparison of price strings is wrong in a way that looks right.
    check("the bug this prevents: '9.00' sorts above '15.20' as a string",
          "9.00" > "15.20", True)
    check("but not as a number", num("9.00") > num("15.20"), False)

    o = offer(evs[0])
    check("lowPrice parsed", o["low"], 15.20)
    check("highPrice parsed", isinstance(o["high"], (int, float)), True)
    check("missing offers is not a crash", offer({})["low"], None)

    # ---- the join ----
    sched = {"games": [
        {"gameId": 1, "date": "2026-09-22", "startTimeUTC": "2026-09-23T02:00:00Z",
         "opponent": {"name": "Vegas Golden Knights", "abbrev": "VGK"}},
        {"gameId": 2, "date": "2026-09-24", "startTimeUTC": "2026-09-25T02:00:00Z",
         "opponent": {"name": "Anaheim Ducks", "abbrev": "ANA"}},
    ]}
    # Fixture games are 2026-09-22 and 2026-09-24, so "today" is pinned before both;
    # the played-game cases below move it explicitly.
    T0 = "2026-09-01"
    rows, problems, notes = join(home, sched, T0)
    check("both games join across the offset bug", len(rows), 2)
    check("clean join has no problems", problems, [])
    check("row carries the source", rows[0]["source"], "scorebig")
    check("row carries a price", rows[0]["low"], 15.20)

    # The regression guard for the actual bug: had we honoured the -08:00 offset, a PDT
    # game's UTC would be an hour late and match nothing. Assert the schedule's real UTC
    # is what maps here, not the offset-derived one.
    bad = datetime.fromisoformat("2026-09-22T19:00:00-08:00").astimezone(
        timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    check("ScoreBig's offset would have produced the WRONG utc",
          bad != "2026-09-23T02:00:00Z", True)

    far = {"games": sched["games"] + [
        {"gameId": 3, "date": "2027-04-10", "startTimeUTC": "2027-04-11T02:00:00Z",
         "opponent": {"name": "Anaheim Ducks", "abbrev": "ANA"}}]}
    rows, problems, notes = join(home, far, T0)
    check("beyond the horizon is a note", (len(problems), len(notes)), (0, 1))

    inner = {"games": [sched["games"][0],
                       {"gameId": 9, "date": "2026-09-23",
                        "startTimeUTC": "2026-09-24T02:00:00Z",
                        "opponent": {"name": "Los Angeles Kings", "abbrev": "LAK"}},
                       sched["games"][1]]}
    rows, problems, notes = join(home, inner, T0)
    check("a gap inside coverage is fatal", len(problems), 1)

    rows, problems, notes = join(home, {"games": [sched["games"][0]]}, T0)
    check("orphan event is fatal", any("ORPHAN" in p for p in problems), True)

    wrong = {"games": [dict(sched["games"][0],
                            opponent={"name": "Boston Bruins", "abbrev": "BOS"})]}
    rows, problems, notes = join({"2026-09-22T19:00": home["2026-09-22T19:00"]}, wrong, T0)
    check("opponent mismatch is fatal", any("MISMATCH" in p for p in problems), True)


    # ---- played games (ops#351) ----
    # The live failure, reproduced: the 2026-09-22 game has been played and delisted, the
    # 2026-09-24 game is still listed, so the played game sits INSIDE the horizon and read
    # as a gap - refusing the whole run. Played games must leave the required set.
    delisted = {k: v for k, v in home.items() if k != "2026-09-22T19:00"}
    rows, problems, notes = join(delisted, sched, "2026-09-27")
    check("a played-and-delisted game does not refuse the run", problems, [])
    check("the remaining game still joins", [r["gameId"] for r in rows], [2])
    check("the played game is a note, not a horizon note",
          [n.split(":")[0] for n in notes], ["already played"])
    # The same page before that game is played: a real gap, still fatal.
    rows, problems, notes = join(delisted, sched, "2026-09-01")
    check("an unplayed missing game inside coverage still refuses the run",
          any("GAP INSIDE COVERAGE" in p and "2026-09-22" in p for p in problems), True)
    # Game DAY is not "played": the boundary is the day after.
    rows, problems, notes = join(delisted, sched, "2026-09-22")
    check("a game missing on its own game day still refuses the run",
          any("GAP INSIDE COVERAGE" in p for p in problems), True)
    # Played games leaving must not mask a real gap between later, unplayed games.
    rows, problems, notes = join(delisted, inner, "2026-09-23")
    check("an unplayed gap is still caught once earlier games are played",
          any("GAP INSIDE COVERAGE" in p and "2026-09-23" in p for p in problems), True)
    # A played game the source still lists joins normally - exclusion never drops data.
    rows, problems, notes = join(home, sched, "2026-09-27")
    check("a played game still listed still joins", (len(rows), problems, notes),
          (2, [], []))

    # ---- total-failure guard (found by review) ----
    # An empty events dict means the horizon is None, which routed every game to "beyond
    # horizon" and exited 0 - a complete scrape failure reported as healthy coverage.
    rows, problems, notes = join({}, sched, T0)
    check("a total scrape failure is fatal", len(problems), 1)
    check("and it says so plainly", bool(problems) and "TOTAL FAILURE" in problems[0], True)
    check("no notes are emitted that would read as expected coverage", notes, [])
    check("and no rows are produced", rows, [])
    # An empty SCHEDULE is not a failure - there is simply nothing to match.
    rows, problems, notes = join({}, {"games": []}, T0)
    check("an empty schedule is not a scrape failure", problems, [])
    rows, problems, notes = join({}, sched, "2026-09-27")
    check("an empty page after the season is not a scrape failure", problems, [])
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
            for today in ("2026-09-23", sched["games"][-1]["date"]):
                with patch.object(ms, "venue_today", return_value=today), \
                     patch.object(ms, "get", return_value=("", "synthetic outage")) as fetch:
                    check(f"outage with unplayed game on {today} fails",
                          collect(store_path, None) != 0, True)
                    check(f"outage with unplayed game on {today} fetches once",
                          fetch.call_count, 1)
            schedule_path.write_text(json.dumps({"games": []}))
            with patch.object(ms, "venue_today", return_value="9999-01-01"), \
                 patch.object(ms, "get", return_value=("", "synthetic outage")) as fetch:
                check("empty schedule does not take the season-end exit",
                      collect(store_path, None) != 0, True)
                check("empty schedule still fetches once", fetch.call_count, 1)

    check("a malformed block does not discard the page",
          len(sports_events('<script type="application/ld+json">{bad</script>'
                            '<script type="application/ld+json">'
                            '{"@type":"SportsEvent","name":"x"}</script>')), 1)

    for f in fails:
        print(f"  FAIL {f}", file=sys.stderr)
    print(f"self-test: {'FAILED' if fails else 'passed'} ({len(fails)} failure(s))")
    return 1 if fails else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--store", type=pathlib.Path, default=STORE)
    ap.add_argument("--raw-dir", type=pathlib.Path, default=None,
                    help="write the raw ld+json here. Artifact only - never commit it.")
    args = ap.parse_args()
    return self_test() if args.self_test else collect(args.store, args.raw_dir)


if __name__ == "__main__":
    sys.exit(main())
