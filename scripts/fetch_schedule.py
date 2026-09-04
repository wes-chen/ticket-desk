#!/usr/bin/env python3
"""Fetch the Sharks home schedule from the NHL public API and join it to the tier table.

Validation is the point of this script, not the fetch. The tier table was transcribed
from a JPEG by hand; a single misread date would silently misprice a game for the whole
season. So every game must match on BOTH date and opponent, and every game must end up
with exactly one tier. Anything that doesn't line up is reported and exits non-zero.
"""

import json
import pathlib
import re
import sys
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
TEAM = "SJS"
SEASON = "20262027"
API = f"https://api-web.nhle.com/v1/club-schedule-season/{TEAM}/{SEASON}"

GAME_TYPE = {1: "preseason", 2: "regular", 3: "playoff"}


def fetch():
    req = urllib.request.Request(API, headers={"User-Agent": "ticket-desk/0.1"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def tm_event_id(link: str | None) -> str | None:
    if not link:
        return None
    m = re.search(r"ticketmaster\.com/event/([A-Za-z0-9]+)", link)
    return m.group(1) if m else None


def main():
    tiers = json.loads((ROOT / "config" / "tiers.json").read_text())
    by_date = {g["date"]: g for g in tiers["games"]}

    raw = fetch()
    home = [g for g in raw.get("games", []) if g.get("homeTeam", {}).get("abbrev") == TEAM]

    problems = []
    matched_dates = set()
    out = []

    for g in sorted(home, key=lambda x: x["startTimeUTC"]):
        date = g["gameDate"]
        opp = g["awayTeam"]["abbrev"]
        gtype = GAME_TYPE.get(g.get("gameType"), "unknown")

        if gtype == "preseason":
            tier = "PRESEASON"
        else:
            entry = by_date.get(date)
            if entry is None:
                problems.append(f"NO TIER: {date} vs {opp} is a regular-season home game with no tier entry")
                tier = None
            elif entry["opp"] != opp:
                problems.append(
                    f"OPPONENT MISMATCH on {date}: tier table says {entry['opp']}, NHL API says {opp}"
                )
                tier = entry["tier"]
                matched_dates.add(date)
            else:
                tier = entry["tier"]
                matched_dates.add(date)

        out.append({
            "gameId": g["id"],
            "date": date,
            "startTimeUTC": g["startTimeUTC"],
            "venueTimezone": g.get("venueTimezone", "US/Pacific"),
            "gameType": gtype,
            "opponent": {
                "abbrev": opp,
                "name": f"{g['awayTeam'].get('placeName', {}).get('default', '')} "
                        f"{g['awayTeam'].get('commonName', {}).get('default', '')}".strip(),
                "logo": g["awayTeam"].get("logo"),
            },
            "tier": tier,
            "ticketsLink": g.get("ticketsLink"),
            # The NHL schedule embeds the Ticketmaster event id, which is the exact key
            # TM's own systems use. Saves fuzzy-matching games to listings later.
            "tmEventId": tm_event_id(g.get("ticketsLink")),
        })

    for date, entry in by_date.items():
        if date not in matched_dates:
            problems.append(
                f"ORPHAN TIER ENTRY: {date} vs {entry['opp']} ({entry['tier']}) matches no home game"
            )

    counts = {}
    for row in out:
        counts[row["tier"]] = counts.get(row["tier"], 0) + 1

    print(f"home games: {len(out)}")
    print(f"tier counts: {json.dumps(counts, sort_keys=True)}")
    expected = {k: v for k, v in tiers["_counts"].items() if not k.startswith("_")}
    for tier, n in expected.items():
        actual = counts.get(tier, 0)
        if actual != n:
            problems.append(f"COUNT MISMATCH for tier {tier}: graphic says {n}, joined {actual}")

    if problems:
        print(f"\n{len(problems)} PROBLEM(S):", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)

    dest = ROOT / "data" / "schedule.json"
    dest.write_text(json.dumps({"season": SEASON, "team": TEAM, "games": out}, indent=2) + "\n")
    print(f"\nwrote {dest.relative_to(ROOT)}")

    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
