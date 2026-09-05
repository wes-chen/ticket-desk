#!/usr/bin/env python3
"""Resolve Ticketmaster Discovery event ids and sales windows for every home game.

WHAT THIS IS NOT, AND WHY. This file replaces scripts/collect_discovery.py, which was
built to record per-event price ranges as an unbreakable baseline series. The first live
run falsified that premise (ops#5):

  * `priceRanges` is ABSENT from every Discovery response for this team and venue -
    0 of 91 search results, and absent on the detail endpoint too, at HTTP 200.
    The old fixtures asserted that field from TM's published docs because no key
    existed to check it. Documentation is not measurement.
  * The `tmEventId` values harvested from the NHL feed's ticketsLink are Ticketmaster
    LEGACY web-URL ids (1C0064E79AA99B1B). Discovery uses a different namespace
    (G5vYZ_CrQn_ih) and 404s (DIS1004) on all 42 of them.

So Discovery is not a price source. It is an id and metadata resolver feeding the real
collector (ops#6), and that is all this script claims to be. Price collection now depends
entirely on ops#16.

WHAT IT IS GOOD FOR, measured 2026-09-05:
  * Discovery ids for all 44 home games - including the two preseason games, which had
    no tmEventId at all. Coverage 42/44 -> 44/44.
  * The legacy id, recovered from each event's own `url`, for those same two games.
  * `sales.public` start/end - a real, dated sales window.
  * Metadata that no bot wall can take away.

VALIDATION. Same discipline as fetch_schedule.py, and for the same reason: a silently
wrong id would misdirect the collector at every game for a whole season. The join is
required to agree on THREE independent keys, not one:

  1. local date, exactly one TM event per scheduled date and no orphans either way
  2. the opponent's name appearing in the TM event name
  3. the legacy id parsed out of the TM event `url` matching the tmEventId already
     stored in schedule.json

Key 3 is what makes this a cross-check rather than an inference. It matched 42/42 with
zero mismatches on capture day; a future rename or reschedule that breaks the date join
would have to break the legacy id in the same direction to slip through.

Usage:
    TM_DISCOVERY_API_KEY=... python3 scripts/resolve_tm_events.py
    python3 scripts/resolve_tm_events.py --self-test    # real fixtures, no key, no network
"""

import argparse
import json
import os
import pathlib
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCHEDULE = ROOT / "data" / "schedule.json"
DEST = ROOT / "data" / "tm_events.json"
FIXTURES = ROOT / "tests" / "fixtures"

SEARCH_URL = "https://app.ticketmaster.com/discovery/v2/events.json"

# SAP Center at San Jose. Filtering server-side by venue is deliberate: the same search
# by keyword returns 91 events, 47 of them AWAY games at other arenas, and a client-side
# name match on "SAP Center at San Jose" would break on any venue rename. The venue guard
# below still runs as defence in depth.
VENUE_ID = "KovZpZAJelvA"
VENUE_NAME = "SAP Center at San Jose"
CLASSIFICATION = "Hockey"
PAGE_SIZE = 200

UA = "ticket-desk/0.1 (personal season-ticket tool)"
TIMEOUT = 30


def load_key() -> str | None:
    key = os.environ.get("TM_DISCOVERY_API_KEY")
    if key:
        return key.strip()
    envfile = ROOT / ".env.local"
    if envfile.exists():
        for ln in envfile.read_text().splitlines():
            ln = ln.strip()
            if ln.startswith("TM_DISCOVERY_API_KEY="):
                return ln.split("=", 1)[1].strip().strip("'\"")
    return None


def legacy_id(url: str | None) -> str | None:
    """Pull the legacy web-URL id out of a Discovery event's `url`.

    This is the field that makes the join verifiable: TM serves both namespaces at once,
    the modern id as `event.id` and the legacy id inside `event.url`.
    """
    m = re.search(r"/event/([A-Za-z0-9]+)", url or "")
    return m.group(1) if m else None


def search(key: str) -> tuple[list[dict], str | None]:
    """Fetch every hockey event at the venue. Returns (events, error)."""
    events: list[dict] = []
    page = 0
    while True:
        qs = urllib.parse.urlencode({
            "apikey": key, "venueId": VENUE_ID, "classificationName": CLASSIFICATION,
            "sort": "date,asc", "size": PAGE_SIZE, "page": page,
        })
        req = urllib.request.Request(f"{SEARCH_URL}?{qs}",
                                     headers={"User-Agent": UA, "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                payload = json.load(r)
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read(300).decode("utf-8", "replace")
            except Exception:  # noqa: BLE001
                pass
            return events, f"http {e.code}: {body[:300]}"
        except Exception as e:  # noqa: BLE001
            return events, f"{type(e).__name__}: {e}"

        events += (payload.get("_embedded") or {}).get("events") or []
        info = payload.get("page") or {}
        page += 1
        if page >= (info.get("totalPages") or 1):
            break
    return events, None


def price_range_probe(events: list[dict]) -> dict:
    """Count events carrying priceRanges.

    Kept deliberately. The whole point of ops#5 was a price series, and the finding that
    killed it was an ABSENT field. If TM ever starts populating it - closer to an event,
    or when primary inventory is live - nobody would notice unless something looks. This
    looks, every run, and costs nothing.
    """
    with_pr = [e for e in events if e.get("priceRanges")]
    return {"checked": len(events), "withPriceRanges": len(with_pr),
            "sample": (with_pr[0].get("priceRanges") if with_pr else None)}


def home_events(events: list[dict]) -> tuple[list[dict], list[str]]:
    """Keep events actually at the venue. Returns (kept, problems)."""
    kept, problems = [], []
    for e in events:
        v = ((e.get("_embedded") or {}).get("venues") or [{}])[0]
        if v.get("id") == VENUE_ID or v.get("name") == VENUE_NAME:
            kept.append(e)
        else:
            # Server-side venueId should already have excluded these. If one appears the
            # filter is not doing what it claims, which is worth failing over.
            problems.append(
                f"venue filter leaked an event at {v.get('name')!r} "
                f"({e.get('dates', {}).get('start', {}).get('localDate')}) - "
                f"server-side venueId filter is not behaving as measured"
            )
    return kept, problems


def join(events: list[dict], schedule: dict) -> tuple[list[dict], list[str]]:
    games = schedule["games"]
    by_date: dict[str, list[dict]] = {}
    for e in events:
        ld = ((e.get("dates") or {}).get("start") or {}).get("localDate")
        by_date.setdefault(ld, []).append(e)

    problems, out, matched = [], [], set()

    for g in games:
        date = g["date"]
        cands = by_date.get(date) or []
        if not cands:
            problems.append(f"NO TM EVENT: {date} vs {g['opponent']['abbrev']} has no Discovery event")
            continue
        if len(cands) > 1:
            problems.append(
                f"AMBIGUOUS: {date} matched {len(cands)} Discovery events "
                f"({', '.join(c['id'] for c in cands)}) - date is not a unique key here"
            )
            continue
        e = cands[0]
        matched.add(date)

        # Key 2: the opponent must appear in the TM event name. Catches a date collision
        # that a pure date join would accept.
        tm_name = (e.get("name") or "")
        opp_full = (g["opponent"]["name"] or "").strip()
        opp_last = opp_full.split()[-1].lower() if opp_full else ""
        if opp_last and opp_last not in tm_name.lower():
            problems.append(
                f"OPPONENT MISMATCH on {date}: schedule says {opp_full!r}, "
                f"TM event is named {tm_name!r}"
            )

        # Key 3: the legacy id inside the TM url must equal the one already stored.
        lg = legacy_id(e.get("url"))
        stored = g.get("tmEventId")
        if lg is None:
            problems.append(f"NO LEGACY ID: {date} TM url {e.get('url')!r} has no /event/<id> segment")
        elif stored and lg != stored:
            problems.append(
                f"LEGACY ID MISMATCH on {date}: schedule.json has {stored}, "
                f"TM url has {lg} - the id mapping is not what it was measured to be"
            )

        sales = (e.get("sales") or {}).get("public") or {}
        out.append({
            "gameId": g["gameId"],
            "date": date,
            "gameType": g["gameType"],
            "opponent": g["opponent"]["abbrev"],
            "discoveryId": e["id"],
            # Recovered for the two preseason games, which had none. Kept for both
            # namespaces because TM's own web pages are keyed by the legacy id and the
            # scraper (ops#6) will need it.
            "legacyId": lg,
            "tmName": tm_name,
            "status": ((e.get("dates") or {}).get("status") or {}).get("code"),
            "salesPublicStart": sales.get("startDateTime"),
            "salesPublicEnd": sales.get("endDateTime"),
        })

    for date, cands in by_date.items():
        if date not in matched and date is not None:
            problems.append(
                f"ORPHAN TM EVENT: {date} ({cands[0].get('name')!r}) matches no scheduled home game"
            )

    return out, problems


def run(key: str, dest: pathlib.Path) -> int:
    schedule = json.loads(SCHEDULE.read_text())
    events, err = search(key)
    if err:
        print(f"\nDiscovery search failed: {err}", file=sys.stderr)
        if err.startswith("http 403"):
            print("403 is the ops#4 signature - a datacenter block reaching the API. "
                  "Re-run from a residential IP before concluding anything else.", file=sys.stderr)
        return 1

    probe = price_range_probe(events)
    kept, venue_problems = home_events(events)
    resolved, join_problems = join(kept, schedule)
    problems = venue_problems + join_problems

    print(f"discovery search:  {len(events)} events at venue {VENUE_ID}")
    print(f"after venue guard: {len(kept)}")
    print(f"resolved:          {len(resolved)}/{len(schedule['games'])} home games")
    got_legacy = sum(1 for r in resolved if r["legacyId"])
    recovered = sum(1 for r in resolved
                    if r["legacyId"] and not next(g for g in schedule["games"]
                                                  if g["gameId"] == r["gameId"]).get("tmEventId"))
    print(f"legacy ids:        {got_legacy} present, {recovered} recovered for games that had none")
    print(f"priceRanges probe: {probe['withPriceRanges']}/{probe['checked']} events carry it")
    if probe["withPriceRanges"]:
        print("  !! priceRanges has APPEARED. ops#5 was closed on its absence - reopen the "
              "price-source question.")
        print(f"  sample: {json.dumps(probe['sample'])}")

    if problems:
        print(f"\n{len(problems)} PROBLEM(S):", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        print("\nNothing written - a wrong id map is worse than a stale one.", file=sys.stderr)
        return 1

    dest.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "_comment": ("Ticketmaster Discovery event ids for each home game, resolved by "
                     "scripts/resolve_tm_events.py. PUBLIC event metadata only - no prices, "
                     "no seats, no account data. Discovery publishes no priceRanges for this "
                     "venue; see ops#5."),
        # Stamped only when something actually changes. A wall-clock timestamp churned
        # this file on every daily run, so the CI commit conflicted with any local run
        # over a line carrying no information.
        "generatedAt": None,
        "venueId": VENUE_ID,
        "priceRangeProbe": {k: probe[k] for k in ("checked", "withPriceRanges")},
        "events": resolved,
    }
    previous = {}
    if dest.exists():
        try:
            previous = json.loads(dest.read_text())
        except json.JSONDecodeError:
            previous = {}
    same = (previous.get("events") == resolved
            and previous.get("venueId") == VENUE_ID
            and previous.get("priceRangeProbe") == payload["priceRangeProbe"])
    payload["generatedAt"] = (previous.get("generatedAt") if same
                              else datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
    dest.write_text(json.dumps(payload, indent=2) + "\n")
    shown = dest.relative_to(ROOT) if dest.is_relative_to(ROOT) else dest
    print(f"\n{'unchanged' if same else 'wrote'} {shown}")
    return 0


# ------------------------------------------------------------------- self-test

def self_test() -> int:
    """Runs against REAL captured responses in tests/fixtures/.

    The previous version of this file self-tested against fixtures invented from TM's
    documentation, passed cleanly, and was wrong about the one field the whole design
    rested on. Fixtures are now captures, never inventions.
    """
    fails = []

    def check(label, got, want):
        if got != want:
            fails.append(f"{label}: got {got!r}, want {want!r}")

    search_fx = json.loads((FIXTURES / "discovery_search.json").read_text())
    detail_fx = json.loads((FIXTURES / "discovery_detail.json").read_text())
    err_fx = json.loads((FIXTURES / "discovery_404.json").read_text())
    events = search_fx["_embedded"]["events"]

    # The finding that killed the original design, asserted so a regression is loud.
    check("no priceRanges in captured search", price_range_probe(events)["withPriceRanges"], 0)
    check("no priceRanges in captured detail", "priceRanges" in detail_fx, False)
    check("captured 404 is DIS1004", err_fx["errors"][0]["code"], "DIS1004")
    check("captured 404 names the legacy id",
          "1C0064E79AA99B1B" in err_fx["errors"][0]["detail"], True)

    # Venue guard drops the away game.
    kept, vp = home_events(events)
    check("venue guard keeps 2 home events", len(kept), 2)
    check("venue guard flags the away event", len(vp), 1)
    check("away venue named in the problem", "Honda Center" in vp[0], True)

    # Legacy id extraction, including the shape that has no id at all.
    check("legacy id parsed",
          legacy_id("https://www.ticketmaster.com/x-10-01-2026/event/1C0064E79AA99B1B"),
          "1C0064E79AA99B1B")
    check("no legacy id in a bare team page",
          legacy_id("https://www.nhl.com/sharks/tickets/"), None)
    check("legacy id from None url", legacy_id(None), None)

    # Join against a schedule slice matching the two captured home games.
    sched = {"games": [
        {"gameId": 1, "date": "2026-09-22", "gameType": "preseason",
         "opponent": {"abbrev": "VGK", "name": "Vegas Golden Knights"}, "tmEventId": None},
        {"gameId": 2, "date": "2026-10-01", "gameType": "regular",
         "opponent": {"abbrev": "FLA", "name": "Florida Panthers"},
         "tmEventId": "1C0064E79AA99B1B"},
    ]}
    resolved, probs = join(kept, sched)
    check("clean join has no problems", probs, [])
    check("both games resolved", len(resolved), 2)
    check("discovery id captured", resolved[1]["discoveryId"], "G5vYZ_CrQn_ih")
    check("legacy id matches the stored one", resolved[1]["legacyId"], "1C0064E79AA99B1B")
    check("legacy id recovered for preseason", bool(resolved[0]["legacyId"]), True)
    check("sales window carried through", bool(resolved[1]["salesPublicStart"]), True)

    # Each validation key must actually fire when broken.
    bad = json.loads(json.dumps(sched))
    bad["games"][1]["tmEventId"] = "1C0000000000DEAD"
    check("legacy id mismatch is caught",
          any("LEGACY ID MISMATCH" in p for p in join(kept, bad)[1]), True)

    bad = json.loads(json.dumps(sched))
    bad["games"][1]["opponent"] = {"abbrev": "BOS", "name": "Boston Bruins"}
    check("opponent mismatch is caught",
          any("OPPONENT MISMATCH" in p for p in join(kept, bad)[1]), True)

    bad = json.loads(json.dumps(sched))
    bad["games"].append({"gameId": 3, "date": "2027-05-05", "gameType": "regular",
                         "opponent": {"abbrev": "NYR", "name": "New York Rangers"}, "tmEventId": None})
    check("missing TM event is caught",
          any("NO TM EVENT" in p for p in join(kept, bad)[1]), True)

    check("orphan TM event is caught",
          any("ORPHAN TM EVENT" in p for p in join(kept, {"games": [sched["games"][0]]})[1]), True)

    dup = kept + [kept[1]]
    check("ambiguous date is caught",
          any("AMBIGUOUS" in p for p in join(dup, sched)[1]), True)

    # And the probe must notice if priceRanges ever comes back.
    check("probe detects a populated priceRanges",
          price_range_probe([{"priceRanges": [{"min": 1, "max": 2}]}])["withPriceRanges"], 1)

    for f in fails:
        print(f"  FAIL {f}", file=sys.stderr)
    print(f"self-test: {'FAILED' if fails else 'passed'} ({len(fails)} failure(s))")
    return 1 if fails else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--dest", type=pathlib.Path, default=DEST)
    args = ap.parse_args()
    if args.self_test:
        return self_test()
    key = load_key()
    if not key:
        print("No TM_DISCOVERY_API_KEY (env or .env.local). See ops#5.", file=sys.stderr)
        return 2
    return run(key, args.dest)


if __name__ == "__main__":
    sys.exit(main())
