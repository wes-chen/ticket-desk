#!/usr/bin/env python3
"""Collect per-event prices from TicketNetwork over plain HTTP.

The project's THIRD comp source. The point of more sources is not more price data - it
is that they catch each other going stale, and a single source quietly serving nonsense
is indistinguishable from a market that moved.

WHY IT TOOK THIS LONG, AND WHY THAT IS THE INTERESTING PART. ops#26 recorded TicketNetwork
as "404 + Cloudflare Just a moment" and moved on. The URL had been GUESSED, and a guessed
404 reads exactly like a block - the same mistake that left Gametime unmeasured through
all of ops#4, and Gametime turned out to work fine. robots.txt advertises a sitemap index;
sitemap/performers/1 carries the real page:

    https://www.ticketnetwork.com/performers/san-jose-sharks-tickets

Measured 2026-09-05, residential, plain HTTP, logged out: 200, 288KB, 60 ld+json
SportsEvent blocks, 47 at SAP Center. Re-measured with a generic urllib User-Agent -
identical counts, so this needs no browser and no UA disguise. Reaching for headless
Chromium is what breaks TickPick; do not "upgrade" this either. See ops#33.

THREE THINGS THAT DIFFER FROM THE GAMETIME COLLECTOR, all measured rather than assumed:

  * `startDate` carries an EXPLICIT UTC OFFSET ("2026-09-22T19:00:00-07:00"), unlike
    Gametime's naive-UTC strings. So it is normalised to UTC and joined on startTimeUTC.
    Joining on the local date instead would be off by one for every night game - and
    every home game here is a night game.

  * Offers are `Offer` with a scalar `price`, not `AggregateOffer` with lowPrice/highPrice.
    So this source yields a LOW only. `high` is genuinely absent, not null-because-broken,
    and summarize_market must not read the missing high as a zero-width spread.

  * COVERAGE IS A ROLLING WINDOW, not the full season. Measured: 29 of 44 home games,
    every one of the 15 missing ones late-season (2027-01-28 onward), and ZERO orphans.
    That is a publisher horizon, not a failure - which matters, because Gametime's policy
    of "any missing game means write nothing" would mean this collector never writes at
    all. See horizon handling in join().

SCOPE. Whole-event low only, like the other two. Seat-level comps still need the manual
paste in ops#23.
"""

import argparse
import json
import pathlib
import re
import sys
from datetime import datetime, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import market_store as ms  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCHEDULE = ROOT / "data" / "schedule.json"
STORE = ROOT / "data" / "market" / "ticketnetwork.jsonl"

PERFORMER_URL = "https://www.ticketnetwork.com/performers/san-jose-sharks-tickets"
VENUE_PREFIX = "SAP Center"
LDJSON_RE = re.compile(r'<script[^>]*application/ld\+json[^>]*>(.*?)</script>', re.S)


def sports_events(html: str) -> list[dict]:
    """Every SportsEvent/Event block. A per-block parse failure is skipped rather than
    discarding the whole page - one malformed block must not cost 46 good ones."""
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


def offer(e: dict) -> dict:
    """TicketNetwork serves Offer.price - a scalar ask, not a range. `high` is absent on
    purpose: inventing one would fabricate a spread this source never published."""
    o = e.get("offers") or {}
    if isinstance(o, list):
        o = o[0] if o else {}
    return {
        "low": o.get("price"),
        "currency": o.get("priceCurrency"),
        "availability": (o.get("availability") or "").rsplit("/", 1)[-1] or None,
    }


def away_name(e: dict) -> str:
    a = e.get("awayTeam")
    if isinstance(a, dict):
        return a.get("name") or ""
    return a or ""


def utc_key(start: str | None) -> str | None:
    """Normalise an offset-bearing ISO timestamp to the schedule's startTimeUTC form.

    Returns None for a date-only string ("2026-09-30"), which is what season-ticket
    packages carry. Those are filtered on price anyway; this is the second guard, because
    a package that ever gained a price would otherwise join to nothing and read as an
    orphan - a fatal condition - rather than as the non-game it is.
    """
    if not start:
        return None
    try:
        dt = datetime.fromisoformat(start)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return None
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def home_events(events: list[dict]) -> dict[str, dict]:
    """Venue-filtered, priced, deduped by UTC start. Keyed by startTimeUTC."""
    uniq: dict[str, dict] = {}
    for e in events:
        if not (venue_name(e) or "").startswith(VENUE_PREFIX):
            continue
        if offer(e)["low"] is None:
            continue  # season-ticket packages carry no price. Not an error, not a game.
        key = utc_key(e.get("startDate"))
        if key:
            uniq.setdefault(key, e)
    return uniq


def join(events: dict[str, dict], schedule: dict) -> tuple[list[dict], list[str], list[str]]:
    """Returns (rows, problems, notes). Problems are fatal; notes are expected coverage.

    THE HORIZON RULE. This source publishes a rolling window, so a late-season game simply
    not being listed is normal. But a game missing from INSIDE the covered window is a
    real gap and must not pass silently. So the horizon is the latest game we did match,
    and only games at or before it are required.

    Stated the other way round: absence of evidence beyond the horizon is not evidence of
    absence, and this is the one place the distinction is cheap to encode.
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
    # Deliberately unconditional on the schedule having future games: if there are games to
    # match and we matched nothing, that is a failure whatever the calendar says. A false
    # alarm here costs one red run and is trivially checkable by hand; the silent version
    # costs a season of missing data nobody noticed.
    if games and not events:
        problems.append("TOTAL FAILURE: the page parsed but yielded no usable events at "
                        "all, so nothing could be matched. This is a scrape failure, not "
                        "a coverage horizon - re-run the probe before assuming the source "
                        "simply has no listings.")
        return [], problems, []

    horizon = max((g["startTimeUTC"] for g in games if g["startTimeUTC"] in events),
                  default=None)

    for g in games:
        key = g["startTimeUTC"]
        e = events.get(key)
        if e is None:
            if horizon and key <= horizon:
                problems.append(f"GAP INSIDE COVERAGE: {g['date']} vs "
                                f"{g['opponent']['abbrev']} is missing but earlier and "
                                f"later games are present")
            else:
                notes.append(f"beyond horizon: {g['date']} vs {g['opponent']['abbrev']}")
            continue
        matched.add(key)

        # Second independent key, the discipline fetch_schedule.py established. A UTC
        # collision is unlikely, but the check costs nothing and a wrong join would
        # misattribute one game's prices to another for a whole season.
        last = (g["opponent"]["name"] or "").split()[-1].lower()
        got = away_name(e)
        if last and got and last not in got.lower():
            problems.append(f"OPPONENT MISMATCH on {g['date']}: schedule says "
                            f"{g['opponent']['name']!r}, TicketNetwork away team is {got!r}")

        o = offer(e)
        row = {
            "eventId": (e.get("url") or key).rstrip("/").rsplit("/", 1)[-1],
            "gameId": g["gameId"],
            "date": g["date"],
            "source": "ticketnetwork",
            "ok": True,
        }
        row.update({k: v for k, v in o.items() if v is not None})
        rows.append(row)

    for key in events:
        if key not in matched:
            problems.append(f"ORPHAN EVENT at {key} "
                            f"({(events[key].get('name') or '')[:50]!r}) matches no home "
                            f"game - the join is wrong, not merely incomplete")
    return rows, problems, notes


def collect(store: pathlib.Path, raw_dir: pathlib.Path | None) -> int:
    schedule = json.loads(SCHEDULE.read_text())
    html, err = ms.get(PERFORMER_URL)
    if err:
        print(f"\nTicketNetwork fetch failed: {err}", file=sys.stderr)
        if err.startswith("http 4"):
            print("Measured 200 over plain HTTP from a residential IP on 2026-09-05 "
                  "(ops#33). A 4xx means that changed, OR that the performer URL moved - "
                  "re-resolve it from sitemap/performers/1 before assuming a block. A "
                  "guessed URL's 404 is what hid this source in the first place.",
                  file=sys.stderr)
        return 1

    events = sports_events(html)
    home = home_events(events)
    rows, problems, notes = join(home, schedule)

    now = datetime.now(timezone.utc)
    observed_at = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    observed_date = now.strftime("%Y-%m-%d")
    for r in rows:
        r["observedAt"] = observed_at
        r["observedDate"] = observed_date

    print(f"page: {len(html):,}B  ld+json events: {len(events)}  "
          f"at {VENUE_PREFIX} after dedupe: {len(home)}")
    print(f"joined: {len(rows)}/{len(schedule['games'])} home games")
    if notes:
        print(f"coverage horizon: {len(notes)} game(s) not yet listed (rolling window, "
              f"expected - see module docstring)")

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
        p = raw_dir / f"ticketnetwork-{observed_date}.json"
        p.write_text(json.dumps(
            {"observedAt": observed_at,
             "events": [{"startTimeUTC": k, "ldjson": v} for k, v in sorted(home.items())]},
            indent=2) + "\n")
        print(f"raw -> {p} ({p.stat().st_size:,}B, artifact only, never git)")
    print(f"store now holds: {len(merged)} rows -> {ms.rel(store, ROOT)}")
    return 0


def self_test() -> int:
    fails = []

    def check(label, got, want):
        if got != want:
            fails.append(f"{label}: got {got!r}, want {want!r}")

    fx = json.loads((ROOT / "tests" / "fixtures" / "ticketnetwork_ldjson.json").read_text())
    evs = fx["events"]

    # ---- the offset is the whole ballgame ----
    check("offset-bearing start normalises to UTC",
          utc_key("2026-09-22T19:00:00-07:00"), "2026-09-23T02:00:00Z")
    check("a date-only package start yields no key", utc_key("2026-09-30"), None)
    check("a naive timestamp is refused rather than guessed",
          utc_key("2026-09-22T19:00:00"), None)
    check("missing start is not a crash", utc_key(None), None)
    check("garbage start is not a crash", utc_key("not a date"), None)

    # ---- filters, each with something real to reject ----
    home = home_events(evs)
    check("away game rejected on venue, package on price, duplicate deduped",
          len(home), 2)
    check("keys are UTC", sorted(home), ["2026-09-23T02:00:00Z", "2026-09-25T02:00:00Z"])

    # ---- offer shape: a low, and NO invented high ----
    o = offer(evs[0])
    check("price parsed as low", o["low"], 16)
    check("no high is fabricated", "high" in o, False)
    check("missing offers is not a crash", offer({})["low"], None)

    # ---- the join ----
    sched = {"games": [
        {"gameId": 1, "date": "2026-09-22", "startTimeUTC": "2026-09-23T02:00:00Z",
         "opponent": {"name": "Vegas Golden Knights", "abbrev": "VGK"}},
        {"gameId": 2, "date": "2026-09-24", "startTimeUTC": "2026-09-25T02:00:00Z",
         "opponent": {"name": "Anaheim Ducks", "abbrev": "ANA"}},
    ]}
    rows, problems, notes = join(home, sched)
    check("both games join", len(rows), 2)
    check("clean join has no problems", problems, [])
    check("row carries the source", rows[0]["source"], "ticketnetwork")
    check("row carries a price", rows[0]["low"], 16)

    # A game BEYOND the horizon is a note, not a problem - this source publishes a
    # rolling window and 15 of 44 games were legitimately unlisted when measured.
    far = dict(sched)
    far["games"] = sched["games"] + [
        {"gameId": 3, "date": "2027-04-10", "startTimeUTC": "2027-04-11T02:00:00Z",
         "opponent": {"name": "Anaheim Ducks", "abbrev": "ANA"}}]
    rows, problems, notes = join(home, far)
    check("beyond the horizon is a note", (len(problems), len(notes)), (0, 1))

    # A game missing from INSIDE the covered window is a real gap and must be fatal.
    inner = dict(sched)
    inner["games"] = [sched["games"][0],
                      {"gameId": 9, "date": "2026-09-23",
                       "startTimeUTC": "2026-09-24T02:00:00Z",
                       "opponent": {"name": "Los Angeles Kings", "abbrev": "LAK"}},
                      sched["games"][1]]
    rows, problems, notes = join(home, inner)
    check("a gap inside coverage is fatal", len(problems), 1)
    check("and it says which game", "2026-09-23" in problems[0], True)

    # An event at the venue matching no home game means the join is wrong, not partial.
    rows, problems, notes = join(home, {"games": [sched["games"][0]]})
    check("orphan event is fatal", any("ORPHAN" in p for p in problems), True)

    # Opponent disagreement must fail loudly rather than storing a wrong attribution.
    wrong = {"games": [dict(sched["games"][0],
                            opponent={"name": "Boston Bruins", "abbrev": "BOS"})]}
    rows, problems, notes = join({k: v for k, v in home.items()
                                  if k == "2026-09-23T02:00:00Z"}, wrong)
    check("opponent mismatch is fatal", any("MISMATCH" in p for p in problems), True)

    # ---- parser robustness ----

    # ---- total-failure guard (found by review) ----
    # An empty events dict means the horizon is None, which routed every game to "beyond
    # horizon" and exited 0 - a complete scrape failure reported as healthy coverage.
    rows, problems, notes = join({}, sched)
    check("a total scrape failure is fatal", len(problems), 1)
    check("and it says so plainly", "TOTAL FAILURE" in problems[0], True)
    check("no notes are emitted that would read as expected coverage", notes, [])
    check("and no rows are produced", rows, [])
    # An empty SCHEDULE is not a failure - there is simply nothing to match.
    rows, problems, notes = join({}, {"games": []})
    check("an empty schedule is not a scrape failure", problems, [])

    check("a malformed block does not discard the page",
          len(sports_events('<script type="application/ld+json">{bad</script>'
                            '<script type="application/ld+json">'
                            '{"@type":"SportsEvent","name":"x"}</script>')), 1)
    check("no ld+json at all is empty, not an error", sports_events("<html></html>"), [])

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
    if args.self_test:
        return self_test()
    return collect(args.store, args.raw_dir)


if __name__ == "__main__":
    sys.exit(main())
