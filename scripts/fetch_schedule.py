#!/usr/bin/env python3
"""Fetch the Sharks home schedule from the NHL public API and join it to the tier table.

Validation is the point of this script, not the fetch. The tier table was transcribed
from a JPEG by hand; a single misread date would silently misprice a game for the whole
season. So every game must match on BOTH date and opponent, and every game must end up
with exactly one tier. Anything that doesn't line up is reported and exits non-zero.
"""

import argparse
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


# ONE definition, referenced by both the write and the --help text. The self-test's
# positive control reads the path out of --help stdout, so a literal in either place
# would let the guard watch a file this script does not write (mutant M4b).
DEST = ROOT / "data" / "schedule.json"


def main(write: bool = True):
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
            # The NHL schedule embeds a Ticketmaster event id - but a LEGACY web-URL
            # one, not the id the Discovery API uses. Discovery 404s (DIS1004) on all
            # 42 of these. Measured 2026-09-05; the earlier claim that this was "the
            # exact key TM's own systems use" was wrong and cost a rewrite.
            # It is still the right key for TM's *web* pages, which the scraper needs.
            # scripts/resolve_tm_events.py maps it to the Discovery namespace, and uses
            # this field as an independent cross-check on that mapping.
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

    dest = DEST
    if write:
        dest.write_text(json.dumps({"season": SEASON, "team": TEAM, "games": out}, indent=2) + "\n")
        print(f"\nwrote {dest.relative_to(ROOT)}")
    else:
        # Wording asserted by summarize_market.py's guard; keep DEST in it.
        print(f"\n--dry-run: would write {dest.relative_to(ROOT)} ({len(out)} games)")

    return 1 if problems else 0


def _cli() -> int:
    """Parse arguments before doing anything with a side effect.

    WHY THIS EXISTS. This script had no argument parsing, so unknown flags were silently
    ignored - and `--help`, which is what anyone types first to find out what a script
    does, performed a LIVE NHL FETCH and rewrote data/schedule.json (ops#171).

    It hid from a naive check, too: the rewrite was byte-identical, so `git status` stayed
    clean while a network call and a write had both happened. The schedule genuinely moves
    - this script exists because dates change - so on a day when it has shifted, the same
    probe silently mutates a tracked store under whoever is working.

    --dry-run fetches and validates without writing, which is the thing `--help` was
    accidentally almost doing, made deliberate and safe.
    """
    ap = argparse.ArgumentParser(
        description="Refresh data/schedule.json from the NHL API and VALIDATE it against "
                    "the hand-transcribed tier table on date and opponent.")
    ap.add_argument("--dry-run", action="store_true",
                    help=f"fetch and validate, but do not write "
                         f"{DEST.relative_to(ROOT)}")
    args = ap.parse_args()
    return main(write=not args.dry_run)


if __name__ == "__main__":
    sys.exit(_cli())
