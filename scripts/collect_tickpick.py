#!/usr/bin/env python3
"""Collect per-event price ranges from TickPick over plain HTTP.

This is the project's first working price collector. It exists because of a measurement
that reversed a standing assumption (ops#4, ops#16):

    ops#4 concluded "GitHub Actions cannot scrape - IP reputation, not fingerprinting -
    browser flags will not fix it." That was drawn from ONE cell of a 2x2. Filling the
    grid on 2026-09-05:

                    plain HTTP              headless Chromium
      residential   200, 1.4MB, 31 prices   403 "Just a moment..."
      datacenter    200, 1.4MB, 31 prices   403

    The block follows the BROWSER, not the IP. So this collector deliberately uses
    urllib with an ordinary desktop User-Agent and NO Playwright. Reaching for a real
    browser here - the obvious instinct, and what ops#6 assumed - is the thing that
    gets 403'd.

WHY TICKPICK. It is the one platform measured reachable from a free CI runner, and its
prices are all-in, so no buyer-fee reverse engineering is needed. Ticketmaster (the
channel actually sold on) returns a `tm-bl` device challenge from residential and a hard
403 from a runner; SeatGeek 403s everywhere; StubHub is a JS shell. TickPick is
therefore a COMP, not our own channel - see the confidence note in the store.

SCOPE, and why it is not seat-level. ops#6 wants section/row/price for every listing.
TickPick's listing grid is fetched client-side from a path under /ajax/, and
`robots.txt` says `Disallow: /ajax/`. So seat-level collection from TickPick is out of
bounds, not merely hard. What IS available on robots-allowed pages is the schema.org
AggregateOffer - lowPrice and highPrice per event. Coarse, but it is a real market
series, it is exactly what the Discovery API was supposed to provide and did not
(ops#5), and it accrues from tonight.

POLITENESS. 44 requests once a day, spaced. Less traffic than one person browsing the
site. robots.txt sets no crawl-delay. Never authenticated - this touches no account
(CLAUDE.md rule 3), and TickPick is not an account we hold anyway.

STORAGE (Wesley's decision, 2026-09-05): aggregates commit to the public repo; raw
payloads go to Actions artifacts and never into git, because git never forgets.

Usage:
    python3 scripts/collect_tickpick.py --resolve      # rebuild the event id map
    python3 scripts/collect_tickpick.py                # collect today's prices
    python3 scripts/collect_tickpick.py --self-test    # real fixtures, no network
"""

import argparse
import json
import pathlib
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCHEDULE = ROOT / "data" / "schedule.json"
EVENT_MAP = ROOT / "data" / "tickpick_events.json"
STORE = ROOT / "data" / "market" / "tickpick.jsonl"

SITEMAP = "https://www.tickpick.com/sitemap/sports.xml"

# An ordinary desktop Chrome UA. Not a disguise - it is what a normal client sends, and
# the alternative (a bot-identifying UA on a site whose robots.txt permits these paths)
# gets nothing useful. The politeness lever here is request VOLUME, which is ~44/day.
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")

SLEEP_BETWEEN = 1.5
TIMEOUT = 45

# Two caps, and a TRUNCATION CHECK, because a silent cap is how this project keeps
# producing confident wrong answers. A 5MB cap on the 7.4MB sitemap quietly dropped it
# to 39 of 44 events and the resolver reported that as a real coverage gap - the same
# shape as the probe that capped at 400KB and scored a working source as empty. A read
# that hits its cap is now an ERROR, never data.
MAX_BODY_PAGE = 5_000_000
MAX_BODY_SITEMAP = 40_000_000

# /buy-[nhl-preseason-]san-jose-sharks-vs-<opp>-tickets-sap-center[-at-san-jose]-M-D-YY-Hpm/<id>/
EVENT_URL_RE = re.compile(
    r"/buy-(?:nhl-preseason-)?san-jose-sharks-vs-([a-z0-9-]+?)"
    r"-tickets-sap-center(?:-at-san-jose)?"
    r"-(\d{1,2})-(\d{1,2})-(\d{2})-\d{1,2}(?:am|pm)/(\d+)/?$"
)
LDJSON_RE = re.compile(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', re.S)


def get(url: str, max_body: int = MAX_BODY_PAGE) -> tuple[str | None, str | None]:
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    })
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            # Read one byte past the cap so hitting it is detectable.
            data = r.read(max_body + 1)
    except urllib.error.HTTPError as e:
        return None, f"http {e.code}"
    except Exception as e:  # noqa: BLE001
        return None, f"{type(e).__name__}: {e}"
    if len(data) > max_body:
        return None, (f"response exceeded {max_body:,}B cap and was truncated - "
                      f"raise the cap rather than treating a partial body as data")
    return data.decode("utf-8", "ignore"), None


def parse_event_url(url: str) -> dict | None:
    m = EVENT_URL_RE.search(url)
    if not m:
        return None
    opp, mo, d, yy, eid = m.groups()
    return {"date": f"20{yy}-{int(mo):02d}-{int(d):02d}", "oppSlug": opp,
            "eventId": eid, "url": url}


def sports_event(html: str) -> dict | None:
    """Return the schema.org SportsEvent block, or None.

    A page can carry several ld+json blocks (WebSite, SportsTeam, breadcrumbs); only
    SportsEvent has the AggregateOffer. Parse failures on one block must not discard
    the others, hence the per-block try.
    """
    for raw in LDJSON_RE.findall(html):
        try:
            d = json.loads(raw)
        except json.JSONDecodeError:
            continue
        for item in (d if isinstance(d, list) else [d]):
            if isinstance(item, dict) and item.get("@type") == "SportsEvent":
                return item
    return None


def offer(ev: dict) -> dict:
    """Extract the price range. Absent is a real observation, not an error."""
    o = ev.get("offers") or {}
    if isinstance(o, list):
        o = o[0] if o else {}
    return {
        "low": o.get("lowPrice"),
        "high": o.get("highPrice"),
        "currency": o.get("priceCurrency"),
        "availability": (o.get("availability") or "").rsplit("/", 1)[-1] or None,
    }


# ------------------------------------------------------------------- resolving

def resolve() -> int:
    schedule = json.loads(SCHEDULE.read_text())
    games = {g["date"]: g for g in schedule["games"]}

    print(f"fetching {SITEMAP} (robots.txt advertises it; ~7.4MB and growing)")
    body, err = get(SITEMAP, MAX_BODY_SITEMAP)
    if err:
        print(f"sitemap fetch failed: {err}", file=sys.stderr)
        return 1

    locs = re.findall(r"<loc>([^<]+)</loc>", body)
    cands = [u for u in locs if "san-jose-sharks" in u and "sap-center" in u]
    parsed, unparsed = [], []
    for u in cands:
        p = parse_event_url(u)
        (parsed if p else unparsed).append(p or u)

    by_date: dict[str, list] = {}
    for p in parsed:
        by_date.setdefault(p["date"], []).append(p)

    problems = [f"UNPARSEABLE EVENT URL: {u}" for u in unparsed]
    for date, entries in by_date.items():
        if len(entries) > 1:
            problems.append(f"AMBIGUOUS: {date} matched {len(entries)} TickPick events "
                            f"({', '.join(e['eventId'] for e in entries)})")

    out = []
    for date, g in sorted(games.items()):
        entries = by_date.get(date)
        if not entries:
            problems.append(f"NO TICKPICK EVENT: {date} vs {g['opponent']['abbrev']}")
            continue
        e = entries[0]
        # Second key. The date alone would accept a mismatched event on a busy date;
        # the opponent's name must appear in the URL slug too. Same discipline as
        # fetch_schedule.py, which exists because a hand-transcribed table was wrong.
        last = (g["opponent"]["name"] or "").split()[-1].lower()
        if last and last not in e["oppSlug"]:
            problems.append(f"OPPONENT MISMATCH on {date}: schedule says "
                            f"{g['opponent']['name']!r}, TickPick slug is {e['oppSlug']!r}")
        out.append({"gameId": g["gameId"], "date": date, "gameType": g["gameType"],
                    "opponent": g["opponent"]["abbrev"], "eventId": e["eventId"],
                    "url": e["url"]})

    for date in by_date:
        if date not in games:
            problems.append(f"ORPHAN TICKPICK EVENT: {date} matches no scheduled home game")

    print(f"sitemap body: {len(body):,}B  urls: {len(locs)}  "
          f"sharks@sap: {len(cands)}  parsed: {len(parsed)}")
    print(f"resolved: {len(out)}/{len(games)} home games")
    if len(cands) < len(games):
        problems.append(
            f"COVERAGE: sitemap yielded {len(cands)} Sharks/SAP urls for {len(games)} home "
            f"games. Before believing TickPick is missing games, check the fetch: a "
            f"truncated sitemap looks exactly like this."
        )

    if problems:
        print(f"\n{len(problems)} PROBLEM(S):", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        print("\nNothing written - a wrong event map would collect the wrong game's "
              "prices all season.", file=sys.stderr)
        return 1

    EVENT_MAP.write_text(json.dumps({
        "_comment": ("TickPick event ids per home game, resolved from TickPick's own "
                     "sitemap by scripts/collect_tickpick.py. PUBLIC event identifiers "
                     "only - no prices, no seats, no account data."),
        "generatedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": SITEMAP,
        "events": out,
    }, indent=2) + "\n")
    print(f"wrote {rel(EVENT_MAP)}")
    return 0


# ------------------------------------------------------------------ collecting

def rel(path: pathlib.Path) -> str:
    """Display path, tolerating a --store outside the repo (scratch dirs, smoke tests)."""
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def read_store(path: pathlib.Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]


def merge(existing: list[dict], new: list[dict]) -> list[dict]:
    """Upsert on (observedDate, eventId), then sort. A same-day re-run corrects."""
    keyed = {(r["observedDate"], r["eventId"]): r for r in existing}
    for r in new:
        keyed[(r["observedDate"], r["eventId"])] = r
    return sorted(keyed.values(), key=lambda r: (r["observedDate"], r["date"], r["eventId"]))


def write_store(path: pathlib.Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in rows))


def collect(store: pathlib.Path, raw_dir: pathlib.Path | None, limit: int | None) -> int:
    if not EVENT_MAP.exists():
        print(f"No {rel(EVENT_MAP)} - run --resolve first.", file=sys.stderr)
        return 2
    events = json.loads(EVENT_MAP.read_text())["events"]
    if limit:
        events = events[:limit]

    now = datetime.now(timezone.utc)
    observed_at = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    observed_date = now.strftime("%Y-%m-%d")

    rows, raw, ok, no_offer, failed, http_403 = [], [], 0, 0, 0, 0

    for i, ev in enumerate(events):
        html, err = get(ev["url"])
        row = {"observedAt": observed_at, "observedDate": observed_date,
               "eventId": ev["eventId"], "gameId": ev["gameId"], "date": ev["date"],
               "source": "tickpick"}
        if err:
            row["ok"] = False
            row["error"] = err
            failed += 1
            http_403 += err.startswith("http 403")
        else:
            block = sports_event(html)
            if block is None:
                row["ok"] = False
                row["error"] = "no SportsEvent ld+json block"
                failed += 1
            else:
                o = offer(block)
                row["ok"] = True
                row.update({k: v for k, v in o.items() if v is not None})
                # Raw = the whole ld+json block, not the 756KB page. Retaining the page
                # would be ~33MB/day and 3GB over an artifact retention window, for
                # fields nothing reads.
                raw.append({"observedAt": observed_at, "eventId": ev["eventId"],
                            "date": ev["date"], "ldjson": block})
                if o["low"] is not None:
                    ok += 1
                else:
                    no_offer += 1
        rows.append(row)
        if i + 1 < len(events):
            time.sleep(SLEEP_BETWEEN)

    total_failure = bool(events) and failed == len(events)

    # Same guard the Discovery collector needed: a wholesale failure means the source or
    # our access changed, not that the market went quiet. Writing 44 identical error
    # rows a day would bury the series.
    if total_failure:
        merged = read_store(store)
    else:
        merged = merge(read_store(store), rows)
        write_store(store, merged)
        if raw_dir:
            raw_dir.mkdir(parents=True, exist_ok=True)
            p = raw_dir / f"tickpick-{observed_date}.json"
            p.write_text(json.dumps({"observedAt": observed_at, "events": raw}, indent=2) + "\n")
            print(f"raw -> {p} ({p.stat().st_size:,}B, artifact only, never git)")

    print(f"observed {observed_at}")
    print(f"  events queried:  {len(events)}")
    print(f"  with price range: {ok}")
    print(f"  no offer published: {no_offer}")
    print(f"  failed:          {failed}")
    if total_failure:
        print(f"  store UNCHANGED ({len(merged)} rows) - nothing written")
    else:
        print(f"  store now holds: {len(merged)} rows -> {rel(store)}")

    if http_403 and http_403 == len(events):
        print("\nEVERY request got 403. TickPick was measured reachable over plain HTTP "
              "from both residential and datacenter IPs on 2026-09-05; a wholesale 403 "
              "means that changed. Re-run scripts/probe_sources.py before assuming a bug.",
              file=sys.stderr)
        return 1
    if total_failure:
        first = next((r["error"] for r in rows if not r.get("ok")), "?")
        print(f"\nEvery request failed, nothing stored. First error: {first}", file=sys.stderr)
        return 1
    return 0


# ------------------------------------------------------------------- self-test

def self_test() -> int:
    fails = []

    def check(label, got, want):
        if got != want:
            fails.append(f"{label}: got {got!r}, want {want!r}")

    fx = json.loads((ROOT / "tests" / "fixtures" / "tickpick_event_ldjson.json").read_text())
    ev = fx["event"]

    # Real captured payload.
    o = offer(ev)
    check("lowPrice from real payload", o["low"], 86)
    check("highPrice from real payload", o["high"], 1477)
    check("currency", o["currency"], "USD")
    check("availability normalised", o["availability"], "InStock")

    html = f'<html><script type="application/ld+json">{json.dumps(ev)}</script></html>'
    check("SportsEvent found in a page", (sports_event(html) or {}).get("@type"), "SportsEvent")

    # A page whose other blocks are junk must still yield the event.
    noisy = ('<script type="application/ld+json">{"@type":"WebSite"}</script>'
             '<script type="application/ld+json">not json at all</script>'
             f'<script type="application/ld+json">{json.dumps(ev)}</script>')
    check("unparseable sibling block does not hide the event",
          (sports_event(noisy) or {}).get("@type"), "SportsEvent")
    check("no event block -> None", sports_event("<html></html>"), None)

    # Absent offer is an observation, not a crash.
    check("missing offers", offer({"@type": "SportsEvent"})["low"], None)
    check("offers as a list", offer({"offers": [{"lowPrice": 5}]})["low"], 5)

    # URL parsing, both real shapes plus the preseason prefix.
    p = parse_event_url("/buy-san-jose-sharks-vs-florida-panthers-tickets-sap-center-10-1-26-7pm/8135359/")
    check("regular season url date", p["date"], "2026-10-01")
    check("regular season url id", p["eventId"], "8135359")
    p = parse_event_url("/buy-nhl-preseason-san-jose-sharks-vs-vegas-golden-knights-tickets-sap-center-9-22-26-7pm/8075165/")
    check("preseason url date", p["date"], "2026-09-22")
    check("preseason opponent slug", p["oppSlug"], "vegas-golden-knights")
    p = parse_event_url("/buy-san-jose-sharks-vs-florida-panthers-tickets-sap-center-at-san-jose-10-1-26-7pm/8135359/")
    check("long venue variant parses", p["date"] if p else None, "2026-10-01")
    check("an away game must not parse",
          parse_event_url("/buy-anaheim-ducks-vs-san-jose-sharks-tickets-honda-center-9-20-26-1pm/8075153/"), None)

    # Upsert semantics.
    def row(d, ev_id, low):
        return {"observedDate": d, "eventId": ev_id, "date": "2026-10-01", "low": low}
    m = merge([row("2026-09-05", "E1", 86)], [row("2026-09-05", "E1", 91)])
    check("same-day rerun upserts", len(m), 1)
    check("same-day rerun takes the new value", m[0]["low"], 91)
    check("new day appends", len(merge(m, [row("2026-09-06", "E1", 80)])), 2)

    # The resolved map, if present, must cover the schedule exactly.
    if EVENT_MAP.exists():
        evs = json.loads(EVENT_MAP.read_text())["events"]
        sched = json.loads(SCHEDULE.read_text())["games"]
        check("event map covers every home game", len(evs), len(sched))
        check("event map dates are unique", len({e["date"] for e in evs}), len(evs))

    for f in fails:
        print(f"  FAIL {f}", file=sys.stderr)
    print(f"self-test: {'FAILED' if fails else 'passed'} ({len(fails)} failure(s))")
    return 1 if fails else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--resolve", action="store_true", help="rebuild the event id map from the sitemap")
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--limit", type=int, help="query only the first N events")
    ap.add_argument("--store", type=pathlib.Path, default=STORE)
    ap.add_argument("--raw-dir", type=pathlib.Path, default=ROOT / "raw-out",
                    help="where raw ld+json goes; uploaded as an Actions artifact, never committed")
    args = ap.parse_args()

    if args.self_test:
        return self_test()
    if args.resolve:
        return resolve()
    return collect(args.store, args.raw_dir, args.limit)


if __name__ == "__main__":
    sys.exit(main())
