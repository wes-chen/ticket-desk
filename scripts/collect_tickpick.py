#!/usr/bin/env python3
"""Collect TickPick's public team-page AggregateOffer ranges.

Event detail pages began returning 403 from both residential and runner HTTP clients
in October 2026 (ops#415). The logged-out team page still publishes priced SportsEvent
ld+json, but only for a rolling window. One page request replaces dozens of refused
event requests. --resolve remains a no-op until the separately owned workflow removes
its old resolver step; the event map is no longer used for collection.

The logged-out team URL is allowed by the measured robots.txt; the listing grid's
/ajax/ path is disallowed, so these are market-wide ranges, not seat-level comps.
Fetch one page per run and use the shared polite HTTP client rather than a browser.
Only daily aggregates enter git. Raw public ld+json goes to an Actions artifact,
because keeping every scrape in git would preserve its full history forever.
"""

import argparse
import json
import math
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
STORE = ROOT / "data" / "market" / "tickpick.jsonl"
TEAM_URL = "https://www.tickpick.com/nhl/san-jose-sharks-tickets/"
ARENA_TZ = ZoneInfo("America/Los_Angeles")
LDJSON_RE = re.compile(r'<script[^>]*application/ld\+json[^>]*>(.*?)</script>', re.S | re.I)
EVENT_ID_RE = re.compile(r"/(\d+)/?$")


def sports_events(html: str) -> list[dict]:
    """Read individual Event blocks; the SportsTeam.event list lacks offer details."""
    out = []
    for raw in LDJSON_RE.findall(html):
        try:
            block = json.loads(raw.strip())
        except json.JSONDecodeError:
            continue
        for item in (block if isinstance(block, list) else [block]):
            if isinstance(item, dict) and item.get("@type") in ("SportsEvent", "Event"):
                out.append(item)
    return out


def offer(event: dict) -> dict:
    value = event.get("offers") or {}
    if isinstance(value, list):
        value = value[0] if value else {}
    if not isinstance(value, dict):
        return {"low": None, "high": None}
    return {
        "low": value.get("lowPrice"),
        "high": value.get("highPrice"),
        "currency": value.get("priceCurrency"),
        "availability": (value.get("availability") or "").rsplit("/", 1)[-1] or None,
    }


def price_number(value: object) -> float | None:
    """Convert a published price at the source boundary; reject invalid numbers."""
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def venue_name(event: dict) -> str:
    location = event.get("location") or {}
    if isinstance(location, list):
        location = location[0] if location else {}
    return location.get("name") or "" if isinstance(location, dict) else ""


def wall_key(start: str | None) -> str | None:
    if not isinstance(start, str):
        return None
    try:
        dt = datetime.fromisoformat(start)
    except ValueError:
        return None
    return dt.strftime("%Y-%m-%dT%H:%M") if "T" in start else None


def schedule_wall(game: dict) -> str:
    dt = datetime.fromisoformat(game["startTimeUTC"].replace("Z", "+00:00"))
    return dt.astimezone(ARENA_TZ).strftime("%Y-%m-%dT%H:%M")


def home_events(events: list[dict]) -> tuple[dict[str, dict], list[str]]:
    home, problems = {}, []
    for event in events:
        if not venue_name(event).startswith("SAP Center"):
            continue
        key = wall_key(event.get("startDate"))
        if not key:
            problems.append(f"INVALID START: {event.get('name')!r}")
            continue
        if key in home:
            problems.append(f"DUPLICATE HOME START: {key}")
        else:
            home[key] = event
    return home, problems


def join(events: dict[str, dict], schedule: dict, today: str
         ) -> tuple[list[dict], list[str], list[str]]:
    games = schedule["games"]
    problems, notes, rows = [], [], []
    if any(not ms.is_played(g, today) for g in games) and not events:
        return [], ["TOTAL FAILURE: no SAP Center events parsed while games remain"], []
    by_wall = {schedule_wall(g): g for g in games}
    horizon = max((key for key in events if key in by_wall), default=None)
    for key, game in sorted(by_wall.items()):
        event = events.get(key)
        if event is None:
            if ms.is_played(game, today):
                notes.append(f"played: {game['date']}")
            elif horizon and key <= horizon:
                problems.append(f"GAP INSIDE COVERAGE: {game['date']} vs {game['opponent']['abbrev']}")
            else:
                notes.append(f"beyond horizon: {game['date']}")
            continue

        # The captured page prints naive local wall time. If it starts declaring an
        # offset, verify it against the schedule before ever accepting that offset.
        start = datetime.fromisoformat(event["startDate"])
        scheduled = datetime.fromisoformat(game["startTimeUTC"].replace("Z", "+00:00"))
        if start.tzinfo and start.astimezone(timezone.utc) != scheduled:
            problems.append(f"OFFSET MISMATCH: {game['date']} {event['startDate']}")
            continue
        opponent = (game["opponent"]["name"] or "").split()[-1].lower()
        name = event.get("name") or ""
        if not opponent or opponent not in name.lower():
            problems.append(f"OPPONENT MISMATCH: {game['date']} {name!r}")
            continue
        event_id = EVENT_ID_RE.search(event.get("url") or "")
        if not event_id:
            problems.append(f"NO EVENT ID: {game['date']}")
            continue
        prices = offer(event)
        low, high = price_number(prices["low"]), price_number(prices["high"])
        if low is None or high is None or high < low:
            problems.append(f"INVALID PRICE RANGE: {game['date']}")
            continue
        prices["low"], prices["high"] = low, high
        rows.append({
            "eventId": event_id.group(1), "gameId": game["gameId"],
            "date": game["date"], "source": "tickpick", "ok": True,
            **{k: v for k, v in prices.items() if v is not None},
        })
    for key in events:
        if key not in by_wall:
            problems.append(f"ORPHAN HOME EVENT: {key} {events[key].get('name')!r}")
    return rows, problems, notes


def collect(store: pathlib.Path, raw_dir: pathlib.Path | None) -> int:
    schedule = json.loads(SCHEDULE.read_text())
    today = ms.venue_today()
    if schedule["games"] and all(ms.is_played(g, today) for g in schedule["games"]):
        # Playoffs are outside schedule.json and require a separate collection decision.
        print("season over: all scheduled home games have been played")
        return 0
    html, error = ms.get(TEAM_URL)
    if error:
        print(f"TickPick team-page fetch failed: {error}", file=sys.stderr)
        return 1
    events = sports_events(html)
    home, problems = home_events(events)
    rows, join_problems, notes = join(home, schedule, today)
    problems += join_problems
    print(f"page: {len(html):,}B  ld+json events: {len(events)}  SAP Center: {len(home)}")
    print(f"joined: {len(rows)}/{len(schedule['games'])} home games; "
          f"played: {sum(n.startswith('played') for n in notes)}; "
          f"beyond horizon: {sum(n.startswith('beyond') for n in notes)}")
    if problems:
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        print("Nothing written: the page cannot safely be joined to the schedule.", file=sys.stderr)
        return 1

    now = datetime.now(timezone.utc)
    observed_at = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    observed_date = now.strftime("%Y-%m-%d")
    for row in rows:
        row["observedAt"] = observed_at
        row["observedDate"] = observed_date
    merged = ms.commit_rows(store, rows, total_failure=not rows)
    if raw_dir and rows:
        raw_dir.mkdir(parents=True, exist_ok=True)
        path = raw_dir / f"tickpick-{observed_date}.json"
        path.write_text(json.dumps({"observedAt": observed_at, "events": list(home.values())},
                                   indent=2) + "\n")
        print(f"raw -> {path} ({path.stat().st_size:,}B, artifact only)")
    print(f"store now holds: {len(merged)} rows -> {ms.rel(store, ROOT)}")
    return 0


def self_test() -> int:
    fixture = json.loads((ROOT / "tests/fixtures/tickpick_team_ldjson.json").read_text())
    events = fixture["events"]
    html = '<script type="application/ld+json">{"bad":</script>' + "".join(
        f'<script type="application/ld+json">{json.dumps(event)}</script>'
        for event in events)
    parsed = sports_events(html)
    assert len(parsed) == 3
    team_block = {"@type": "SportsTeam", "event": [events[1]]}
    assert sports_events(f'<script type="application/ld+json">{json.dumps(team_block)}</script>') == []
    home, problems = home_events(parsed)
    assert len(home) == 2 and not problems
    assert "2026-10-27T19:30" in home
    assert offer(home["2026-10-27T19:30"])["low"] == events[1]["offers"]["lowPrice"]
    assert wall_key("2026-10-27") is None
    assert wall_key("broken") is None
    invalid = dict(events[1], startDate="broken")
    assert any("INVALID START" in e for e in home_events([invalid])[1])
    assert any("DUPLICATE HOME START" in e for e in home_events([events[1], events[1]])[1])
    games = json.loads(SCHEDULE.read_text())["games"]
    selected = [game for game in games if game["date"] in ("2026-10-27", "2026-10-29")]
    assert len(selected) == 2
    rows, errors, notes = join(home, {"games": selected}, "2026-10-14")
    assert len(rows) == 2 and not errors and not notes
    assert {r["date"] for r in rows} == {"2026-10-27", "2026-10-29"}
    assert join({}, {"games": selected}, "2026-10-14")[1][0].startswith("TOTAL FAILURE")
    bad = dict(events[1], name="San Jose Sharks vs. Imaginary Club")
    assert any("OPPONENT MISMATCH" in e for e in
               join({**home, "2026-10-27T19:30": bad}, {"games": selected}, "2026-10-14")[1])
    bad = dict(events[1], startDate="2026-10-27T19:30:00-08:00")
    assert any("OFFSET MISMATCH" in e for e in
               join({**home, "2026-10-27T19:30": bad}, {"games": selected}, "2026-10-14")[1])
    assert any("GAP INSIDE COVERAGE" in e for e in
               join({"2026-10-29T19:00": home["2026-10-29T19:00"]},
                    {"games": selected}, "2026-10-14")[1])
    assert not join({"2026-10-29T19:00": home["2026-10-29T19:00"]},
                    {"games": selected}, "2026-10-28")[1]
    assert any("ORPHAN HOME EVENT" in e for e in
               join({**home, "2026-11-01T19:00": events[1]},
                    {"games": selected}, "2026-10-14")[1])
    bad = dict(events[1], url="https://example.test/no-id/")
    assert any("NO EVENT ID" in e for e in
               join({**home, "2026-10-27T19:30": bad}, {"games": selected}, "2026-10-14")[1])
    bad = dict(events[1], offers={"lowPrice": "11111111"})
    assert any("INVALID PRICE RANGE" in e for e in
               join({**home, "2026-10-27T19:30": bad}, {"games": selected}, "2026-10-14")[1])
    assert offer(dict(events[1], offers=[events[1]["offers"]]))["low"] == 32
    assert price_number("32.5") == 32.5
    assert price_number(True) is None
    assert price_number(float("nan")) is None
    assert price_number(-1) is None
    # isfinite is load-bearing: float() accepts "Infinity", and inf >= 0 (ops#423, M24).
    assert price_number(float("inf")) is None
    assert price_number("Infinity") is None
    with tempfile.TemporaryDirectory() as dirname:
        store = pathlib.Path(dirname) / "market.jsonl"
        schedule_path = pathlib.Path(dirname) / "schedule.json"
        schedule_path.write_text(json.dumps({"games": selected}))
        with patch.object(ms, "get", return_value=(html, None)), \
             patch.object(ms, "venue_today", return_value="2026-10-14"), \
             patch.dict(globals(), {"SCHEDULE": schedule_path}):
            assert collect(store, None) == 0
        assert len(ms.read_store(store)) == 2

        def page(first: dict, second: dict) -> str:
            return "".join(f'<script type="application/ld+json">{json.dumps(e)}</script>'
                           for e in (first, second))

        def refused(name: str, response: tuple[str | None, str | None],
                    expected: str, *, today: str = "2026-10-14") -> None:
            target = pathlib.Path(dirname) / f"{name}.jsonl"
            with patch.object(ms, "get", return_value=response), \
                 patch.object(ms, "venue_today", return_value=today), \
                 patch.dict(globals(), {"SCHEDULE": schedule_path}):
                from contextlib import redirect_stderr
                from io import StringIO
                err = StringIO()
                with redirect_stderr(err):
                    assert collect(target, None) == 1, name
                assert expected in err.getvalue(), name
            assert not target.exists(), name

        first, second = events[1:3]
        refused("opponent", (page(dict(first, name="wrong opponent"), second), None),
                "OPPONENT MISMATCH")
        refused("offset", (page(dict(first, startDate="2026-10-27T19:30:00-08:00"),
                                second), None), "OFFSET MISMATCH")
        refused("offers_string", (page(dict(first, offers="broken"), second), None),
                "INVALID PRICE RANGE")
        refused("fetch_error", (None, "body cap hit"), "body cap hit")
        for name, changes in (
            ("negative_low", {"lowPrice": -11111111}),
            ("nan_low", {"lowPrice": float("nan")}),
            ("text_low", {"lowPrice": "abc"}),
            ("bool_low", {"lowPrice": True}),
            ("negative_high", {"highPrice": -11111111}),
            ("nan_high", {"highPrice": float("nan")}),
            ("text_high", {"highPrice": "abc"}),
            ("bool_high", {"highPrice": True}),
            ("inverted", {"highPrice": 1}),
            # An infinite LOW is already refused by high < low; only an infinite HIGH
            # reaches the store if isfinite is dropped, so that is the case pinned.
            ("inf_high", {"highPrice": float("inf")}),
        ):
            changed = dict(first, offers={**first["offers"], **changes})
            refused(name, (page(changed, second), None), "INVALID PRICE RANGE")

        # A list-valued AggregateOffer is present in older event-page responses.
        list_page = page(dict(first, offers=[first["offers"]]), second)
        with patch.object(ms, "get", return_value=(list_page, None)), \
             patch.object(ms, "venue_today", return_value="2026-10-14"), \
             patch.dict(globals(), {"SCHEDULE": schedule_path}):
            assert collect(pathlib.Path(dirname) / "list.jsonl", None) == 0
        assert len(ms.read_store(pathlib.Path(dirname) / "list.jsonl")) == 2

        # The store holds the CONVERTED numbers, not the published strings (ops#423,
        # M28), and a successful run writes the raw ld+json artifact to --raw-dir and
        # nowhere else (M30). Absurd prices: rule 1, plausible means real.
        text_page = page(dict(first, offers={**first["offers"], "lowPrice": "11111111",
                                             "highPrice": "22222222.5"}), second)
        text_store = pathlib.Path(dirname) / "text.jsonl"
        raw_dir = pathlib.Path(dirname) / "raw"
        with patch.object(ms, "get", return_value=(text_page, None)), \
             patch.object(ms, "venue_today", return_value="2026-10-14"), \
             patch.dict(globals(), {"SCHEDULE": schedule_path}):
            assert collect(text_store, raw_dir) == 0
        stored = {row["date"]: row for row in ms.read_store(text_store)}
        assert stored[selected[0]["date"]]["low"] == 11111111.0
        assert type(stored[selected[0]["date"]]["low"]) is float
        assert stored[selected[0]["date"]]["high"] == 22222222.5
        assert type(stored[selected[0]["date"]]["high"]) is float
        raw_files = sorted(raw_dir.glob("tickpick-*.json"))
        assert len(raw_files) == 1, raw_files
        raw = json.loads(raw_files[0].read_text())
        assert raw["observedAt"].endswith("Z")
        assert sorted(e["startDate"] for e in raw["events"]) == sorted(
            [first["startDate"], second["startDate"]])
        assert not list(pathlib.Path(dirname).glob("tickpick-*.json"))

        # Once every scheduled home game is played, no fetch or write is needed.
        with patch.object(ms, "get", side_effect=AssertionError("unexpected fetch")), \
             patch.object(ms, "venue_today", return_value="2026-10-30"), \
             patch.dict(globals(), {"SCHEDULE": schedule_path}):
            assert collect(pathlib.Path(dirname) / "season-over.jsonl", None) == 0
        assert not (pathlib.Path(dirname) / "season-over.jsonl").exists()
    print("self-test: passed")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--resolve", action="store_true")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--store", type=pathlib.Path, default=STORE)
    parser.add_argument("--raw-dir", type=pathlib.Path, default=ROOT / "raw-out")
    args = parser.parse_args()
    if args.self_test:
        return self_test()
    if args.resolve:
        print("event-page resolution retired; team-page collection needs no event map")
        return 0
    return collect(args.store, args.raw_dir)


if __name__ == "__main__":
    sys.exit(main())
