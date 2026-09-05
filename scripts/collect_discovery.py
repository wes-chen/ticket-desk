#!/usr/bin/env python3
"""Record per-event price ranges from the Ticketmaster Discovery API.

This is the project's BASELINE price series, deliberately independent of any scraper.
Scrapers break, and a gap in the timing data is not recoverable after the fact - games
happen once. Discovery is free, officially sanctioned, and returns an aggregate min/max
per event rather than seat-level listings. Coarse, but unbroken. See ops#5.

What it is NOT: a substitute for the collector (ops#6). A min/max says nothing about
which section cleared, so it cannot answer comp questions. It answers "what did the
cheapest seat in the building cost on day N", every day, for the whole season.

Never authenticated. The Discovery key is a read-only public-data key and has no
relationship to the seller account (CLAUDE.md rule 3).

CONFIDENCE NOTE: the response shape below (a top-level `priceRanges` list of
{type, currency, min, max}) is taken from TM's published docs and is ASSUMED, not
measured - no key existed when this was written, so no real response has been seen.
The self-test fixtures encode that assumption rather than verifying it. The first live
run is what confirms it; if `no range published` comes back high on events that
visibly have prices, suspect the shape before suspecting TM.

Usage:
    TM_DISCOVERY_API_KEY=... python3 scripts/collect_discovery.py
    python3 scripts/collect_discovery.py --self-test    # no key, no network
"""

import argparse
import json
import os
import pathlib
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCHEDULE = ROOT / "data" / "schedule.json"
STORE = ROOT / "data" / "market" / "discovery.jsonl"

API_BASE = "https://app.ticketmaster.com/discovery/v2/events"
UA = "ticket-desk/0.1 (personal season-ticket tool)"

# TM documents 5 requests/second on the free tier. 42 events is nothing; be polite
# rather than fast. This whole run should take ~15s.
SLEEP_BETWEEN = 0.25
TIMEOUT = 30


def load_key() -> str | None:
    """Env first, then .env.local - which is gitignored, so a real key can live there."""
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


def fetch_event(event_id: str, key: str) -> tuple[dict | None, str | None]:
    """Return (payload, error). Exactly one is None."""
    qs = urllib.parse.urlencode({"apikey": key})
    req = urllib.request.Request(
        f"{API_BASE}/{event_id}.json?{qs}", headers={"User-Agent": UA, "Accept": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return json.load(r), None
    except urllib.error.HTTPError as e:
        # Keep the status in the message. 401 = bad key, 404 = event id moved,
        # 403 = the Actions block from ops#4 reaching the API too, 429 = rate limit.
        # Those need very different responses, so never collapse them into "failed".
        body = ""
        try:
            body = e.read(200).decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            pass
        return None, f"http {e.code}: {body[:200]}"
    except Exception as e:  # noqa: BLE001
        return None, f"{type(e).__name__}: {e}"


def parse_ranges(payload: dict) -> tuple[dict, str | None]:
    """Pull priceRanges out of an event payload, keyed by range type.

    Defensive on purpose: the field is optional and absent for events TM has not
    published a range for. An absent range is a real observation ("no public price
    that day"), not an error, and must not be confused with a failed fetch.
    """
    out: dict[str, dict] = {}
    currency = None
    for pr in payload.get("priceRanges") or []:
        t = pr.get("type") or "standard"
        lo, hi = pr.get("min"), pr.get("max")
        if lo is None and hi is None:
            continue
        out[t] = {"min": lo, "max": hi}
        currency = currency or pr.get("currency")
    return out, currency


def event_status(payload: dict) -> str | None:
    return ((payload.get("dates") or {}).get("status") or {}).get("code")


def rel(path: pathlib.Path) -> str:
    """Display path, tolerating a --store outside the repo (scratch dirs, smoke tests)."""
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def read_store(path: pathlib.Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for ln in path.read_text().splitlines():
        ln = ln.strip()
        if ln:
            rows.append(json.loads(ln))
    return rows


def merge(existing: list[dict], new: list[dict]) -> list[dict]:
    """Upsert on (observedDate, tmEventId), then sort.

    One row per event per UTC day. Discovery returns a whole-building min/max, which
    moves on a timescale of days; the intraday instrument is the scraper (ops#6), not
    this. Re-running on the same day therefore corrects that day's row instead of
    growing the file - which keeps the committed series small and its diffs readable.
    """
    by_key = {(r["observedDate"], r["tmEventId"]): r for r in existing}
    for r in new:
        by_key[(r["observedDate"], r["tmEventId"])] = r
    return sorted(by_key.values(), key=lambda r: (r["observedDate"], r["date"], r["tmEventId"]))


def write_store(path: pathlib.Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in rows))


def collect(key: str, store: pathlib.Path, limit: int | None = None) -> int:
    sched = json.loads(SCHEDULE.read_text())
    games = [g for g in sched["games"] if g.get("tmEventId")]
    skipped = [g for g in sched["games"] if not g.get("tmEventId")]
    if limit:
        games = games[:limit]

    now = datetime.now(timezone.utc)
    observed_at = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    observed_date = now.strftime("%Y-%m-%d")

    rows, ok, no_range, failed = [], 0, 0, 0
    http_403 = 0

    for i, g in enumerate(games):
        payload, err = fetch_event(g["tmEventId"], key)
        row = {
            "observedAt": observed_at,
            "observedDate": observed_date,
            "tmEventId": g["tmEventId"],
            "gameId": g["gameId"],
            # Game date is kept for legibility when reading the file by eye. Tier and
            # opponent are deliberately NOT copied - they are joined from schedule.json
            # at read time, so a tier correction propagates instead of being frozen into
            # every historical row.
            "date": g["date"],
            "source": "discovery",
        }
        if err:
            row["ok"] = False
            row["error"] = err
            failed += 1
            if err.startswith("http 403"):
                http_403 += 1
        else:
            ranges, currency = parse_ranges(payload)
            row["ok"] = True
            row["status"] = event_status(payload)
            row["ranges"] = ranges
            if currency:
                row["currency"] = currency
            if ranges:
                ok += 1
            else:
                no_range += 1
        rows.append(row)
        if i + 1 < len(games):
            time.sleep(SLEEP_BETWEEN)

    total_failure = bool(games) and failed == len(games)

    # Partial failures ARE recorded: a per-event error row is real gap information, and
    # silently dropping it would make a missing day indistinguishable from a day TM
    # published nothing. A TOTAL failure is different - it means the key, the network,
    # or the block posture is wrong, not that the market went quiet. Writing 42
    # identical error rows a day (and committing them) would bury the series in
    # landfill, so write nothing and let the non-zero exit be the signal.
    if total_failure:
        merged = read_store(store)
    else:
        merged = merge(read_store(store), rows)
        write_store(store, merged)

    print(f"observed {observed_at}")
    print(f"  events queried:   {len(games)}")
    print(f"  with price range: {ok}")
    print(f"  no range published: {no_range}")
    print(f"  fetch failed:     {failed}")
    if skipped:
        # Expected: the two preseason games link to a generic team page, not a TM event.
        print(f"  no tmEventId:     {len(skipped)} ({', '.join(g['date'] for g in skipped)})")
    if total_failure:
        print(f"  store UNCHANGED ({len(merged)} rows) - nothing written, see below")
    else:
        print(f"  store now holds:  {len(merged)} rows -> {rel(store)}")

    if http_403 and http_403 == len(games):
        print(
            "\nEVERY request got HTTP 403. That is the ops#4 signature - the datacenter "
            "block reaching the Discovery API too, not just the web properties. Do not "
            "assume the API is exempt; record this and re-run from a residential IP.",
            file=sys.stderr,
        )
        return 1
    if total_failure:
        first = next((r["error"] for r in rows if not r.get("ok")), "?")
        print(f"\nEvery request failed and nothing was stored. First error: {first}", file=sys.stderr)
        return 1
    return 0


# --------------------------------------------------------------------------------
# Self-test. Runs the parse and merge paths against fixtures, with no key and no
# network, so the plumbing is verifiable before the API key exists. CLAUDE.md:
# verify by running something, and a clean build is not evidence of correctness.
# --------------------------------------------------------------------------------

FIXTURE_WITH_RANGE = {
    "name": "San Jose Sharks v Florida Panthers",
    "dates": {"status": {"code": "onsale"}},
    "priceRanges": [
        {"type": "standard", "currency": "USD", "min": 41.0, "max": 388.5},
        {"type": "standard including fees", "currency": "USD", "min": 52.75, "max": 431.2},
    ],
}
FIXTURE_NO_RANGE = {"name": "x", "dates": {"status": {"code": "onsale"}}}
FIXTURE_EMPTY_RANGE = {"priceRanges": [{"type": "standard", "currency": "USD"}]}


def self_test() -> int:
    import tempfile

    fails = []

    def check(label, got, want):
        if got != want:
            fails.append(f"{label}: got {got!r}, want {want!r}")

    ranges, cur = parse_ranges(FIXTURE_WITH_RANGE)
    check("two range types parsed", sorted(ranges), ["standard", "standard including fees"])
    check("standard min/max", ranges["standard"], {"min": 41.0, "max": 388.5})
    check("currency", cur, "USD")
    check("status", event_status(FIXTURE_WITH_RANGE), "onsale")

    # Absent range is an observation, not a failure.
    check("no priceRanges -> empty dict", parse_ranges(FIXTURE_NO_RANGE)[0], {})
    check("status still read when no ranges", event_status(FIXTURE_NO_RANGE), "onsale")
    check("missing dates key", event_status({}), None)
    # A range entry with neither bound carries no information; drop it rather than
    # storing {"min": None, "max": None}, which would read as a real observation.
    check("range with no bounds dropped", parse_ranges(FIXTURE_EMPTY_RANGE)[0], {})

    # Merge: same day + same event replaces; different day appends.
    def row(d, ev, mn):
        return {"observedDate": d, "tmEventId": ev, "date": "2026-10-01", "ranges": {"standard": {"min": mn}}}

    m = merge([row("2026-09-04", "E1", 40)], [row("2026-09-04", "E1", 45)])
    check("same-day rerun upserts", len(m), 1)
    check("same-day rerun takes new value", m[0]["ranges"]["standard"]["min"], 45)
    m = merge(m, [row("2026-09-05", "E1", 50)])
    check("new day appends", len(m), 2)
    check("sorted by observedDate", [r["observedDate"] for r in m], ["2026-09-04", "2026-09-05"])

    # Round-trip through the file, since that is what the workflow actually commits.
    with tempfile.TemporaryDirectory() as d:
        p = pathlib.Path(d) / "market" / "discovery.jsonl"
        write_store(p, m)
        check("round-trips through jsonl", read_store(p), m)
        check("one line per row", len(p.read_text().strip().splitlines()), 2)
        write_store(p, merge(read_store(p), [row("2026-09-05", "E1", 99)]))
        check("upsert survives a reload", len(read_store(p)), 2)

    # The schedule join the real run depends on.
    sched = json.loads(SCHEDULE.read_text())
    with_id = [g for g in sched["games"] if g.get("tmEventId")]
    check("schedule has 44 home games", len(sched["games"]), 44)
    check("42 carry a tmEventId", len(with_id), 42)
    check("all ids look like TM ids", all(g["tmEventId"].isalnum() for g in with_id), True)

    for f in fails:
        print(f"  FAIL {f}", file=sys.stderr)
    print(f"self-test: {'FAILED' if fails else 'passed'} ({len(fails)} failure(s))")
    return 1 if fails else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--self-test", action="store_true", help="run parse/merge checks, no network")
    ap.add_argument("--limit", type=int, help="query only the first N events (for a smoke test)")
    ap.add_argument("--store", type=pathlib.Path, default=STORE)
    args = ap.parse_args()

    if args.self_test:
        return self_test()

    key = load_key()
    if not key:
        print(
            "No TM_DISCOVERY_API_KEY (env or .env.local). Get a free key at "
            "https://developer.ticketmaster.com/ - see ops#5.",
            file=sys.stderr,
        )
        return 2
    return collect(key, args.store, args.limit)


if __name__ == "__main__":
    sys.exit(main())
