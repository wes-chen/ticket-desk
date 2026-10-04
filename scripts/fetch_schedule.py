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

# The key under which excluded playoff home games are written to DEST, beside "games"
# and never inside it. Read by scripts/resolve_tm_events.py - see join() below for why.
EXCLUDED_KEY = "excludedPlayoffGames"


def shown(p: pathlib.Path):
    """A path for log lines: repo-relative when it is in the repo, as-is otherwise.

    The self-test points DEST at a temp directory to exercise the write path, and a bare
    relative_to(ROOT) raises on that.
    """
    return p.relative_to(ROOT) if p.is_relative_to(ROOT) else p


def exclusion_line(e: dict) -> str:
    return (f"EXCLUDED: {e['date']} vs {e['opponent']} (gameId {e['gameId']}) is a playoff "
            f"home game - kept out of the games list in {shown(DEST)}, because collecting playoff "
            f"games is undecided (ops#377)")


def join(raw: dict, tiers: dict) -> tuple[list, list, list]:
    """Join the NHL schedule to the tier table. Returns (games, problems, excluded).

    Pure - no fetch, no file - so the self-test can drive it with captured responses.
    `excluded` is a list of records ({gameId, date, gameType, opponent}), no tier.
    """
    by_date = {g["date"]: g for g in tiers["games"]}
    home = [g for g in raw.get("games", []) if g.get("homeTeam", {}).get("abbrev") == TEAM]

    problems = []
    excluded = []
    matched_dates = set()
    out = []

    for g in sorted(home, key=lambda x: x["startTimeUTC"]):
        date = g["gameDate"]
        opp = g["awayTeam"]["abbrev"]
        gtype = GAME_TYPE.get(g.get("gameType"), "unknown")

        # PLAYOFF HOME GAMES ARE EXCLUDED, LOGGED, AND NOT A FAILURE (ops#377).
        #
        # What happened before this branch existed, measured on a real captured response
        # with a real playoff game added: a playoff game fell through to the tier lookup,
        # found no entry (the tier graphic covers the regular season only), and got tier
        # None - and then the tier-count print crashed on sort_keys comparing None with a
        # str. So the run died on a TypeError, BEFORE the NO TIER line was printed and
        # before the write, the schedule workflow's first step went red, and the commit
        # step never ran. Every later schedule and TM-id refresh was set to freeze from
        # the day the NHL published a playoff home game - at season's end, which is
        # exactly when nobody is watching.
        #
        # Excluding the game here does NOT by itself unfreeze that refresh. The next
        # workflow step, scripts/resolve_tm_events.py, flags every Discovery hockey event
        # at the venue whose date has no game in schedule.json as a fatal ORPHAN TM
        # EVENT, and a Sharks playoff home game is exactly such an event once it is
        # excluded - so the freeze would only have moved one step down (review of PR #38).
        # That is why the exclusions are WRITTEN, under EXCLUDED_KEY beside "games": the
        # resolver reads them and skips orphans on exactly those dates, with a logged
        # reason. Public NHL schedule fields only - gameId, date, opponent - and no tier.
        #
        # Why exclude rather than commit them untiered: every consumer of
        # data/schedule.json treats its games as the games to collect. The three rolling
        # collectors' season-over exit would start waiting for them and their joins would
        # start expecting them, so committing playoff games IS deciding to collect them -
        # and that is a preference for Wesley, not a schedule fix (ops#371, ops#377).
        # Exclusion keeps today's behaviour exactly: playoffs are not collected.
        #
        # Keyed on gameType 3 ONLY. A game of an unknown type still falls through to the
        # tier check and fails loudly; widening this to "anything not regular" would let
        # a misfiled regular-season game vanish from the schedule without a red run.
        if gtype == "playoff":
            excluded.append({"gameId": g["id"], "date": date, "gameType": gtype,
                             "opponent": opp})
            continue

        if gtype == "preseason":
            tier = "PRESEASON"
        else:
            entry = by_date.get(date)
            if entry is None:
                # Names the type it actually has. This said "a regular-season home game"
                # unconditionally, which was false for every game that reached it with
                # any other type.
                problems.append(f"NO TIER: {date} vs {opp} is a home game (gameType {gtype}) with no tier entry")
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

    counts = tier_counts(out)
    expected = {k: v for k, v in tiers["_counts"].items() if not k.startswith("_")}
    for tier, n in expected.items():
        actual = counts.get(tier, 0)
        if actual != n:
            problems.append(f"COUNT MISMATCH for tier {tier}: graphic says {n}, joined {actual}")

    return out, problems, excluded


def tier_counts(out: list) -> dict:
    """Count games per tier, with an untiered game counted under "NONE".

    Keyed on a string on purpose. A None key made json.dumps(sort_keys=True) raise
    TypeError, which turned every NO TIER finding into a crash that hid the finding
    itself (ops#377).
    """
    counts = {}
    for row in out:
        key = row["tier"] if row["tier"] is not None else "NONE"
        counts[key] = counts.get(key, 0) + 1
    return counts


TIERS = ROOT / "config" / "tiers.json"


def main(write: bool = True):
    tiers = json.loads(TIERS.read_text())
    out, problems, excluded = join(fetch(), tiers)

    print(f"home games: {len(out)}")
    print(f"tier counts: {json.dumps(tier_counts(out), sort_keys=True)}")
    # Printed on every run, not once: exclusion is a standing choice, and a log that
    # mentioned it only on the day it started would hide it from every later reader.
    for e in excluded:
        print(exclusion_line(e))

    if problems:
        print(f"\n{len(problems)} PROBLEM(S):", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)

    dest = DEST
    if write:
        doc = {"season": SEASON, "team": TEAM, "games": out}
        # Always written, empty or not, so the resolver never has to tell "no playoff
        # games" from "an older writer that did not record them".
        doc[EXCLUDED_KEY] = excluded
        dest.write_text(json.dumps(doc, indent=2) + "\n")
        print(f"\nwrote {shown(dest)}")
    else:
        # Wording asserted by summarize_market.py's guard; keep DEST in it.
        print(f"\n--dry-run: would write {shown(dest)} ({len(out)} games)")

    return 1 if problems else 0


def self_test() -> int:
    """Offline, against REAL captured NHL responses (tests/fixtures/nhl_schedule_playoff.json).

    No SJS playoff game has ever been captured - none has been scheduled - so the playoff
    case takes a real VGK playoff game and transplants SJS's own captured homeTeam object
    onto it, changing nothing else. Everything the join reads is as served.
    """
    import contextlib
    import copy
    import io
    import tempfile

    fails = []

    def check(label, got, want):
        if got != want:
            fails.append(f"{label}: got {got!r}, want {want!r}")

    fx = json.loads((ROOT / "tests" / "fixtures" / "nhl_schedule_playoff.json").read_text())
    resp, tiers = fx["response"], fx["tiers"]
    sjs_home = next(g["homeTeam"] for g in resp["games"] if g["homeTeam"]["abbrev"] == TEAM)

    def with_extra(game):
        r = copy.deepcopy(resp)
        r["games"].append(game)
        return r

    written = {}

    def run_main(raw, tiers_doc, write=False):
        """main() with fetch, the tier table AND DEST swapped; returns (rc, stdout, error).

        DEST is swapped on both paths, so write=True exercises the real write - which is
        what CI runs - against a temp file, never data/schedule.json. Whatever it wrote
        is left in `written["doc"]` (None when nothing was written).
        """
        global fetch, TIERS, DEST
        saved = fetch, TIERS, DEST
        with tempfile.TemporaryDirectory() as d:
            t = pathlib.Path(d) / "tiers.json"
            t.write_text(json.dumps(tiers_doc))
            dest = pathlib.Path(d) / "schedule.json"
            fetch, TIERS, DEST = (lambda: raw), t, dest
            buf_out, buf_err = io.StringIO(), io.StringIO()
            try:
                with contextlib.redirect_stdout(buf_out), contextlib.redirect_stderr(buf_err):
                    rc = main(write=write)
                return rc, buf_out.getvalue(), None
            except Exception as e:  # a crash is a finding here, not a test-harness error
                return None, buf_out.getvalue(), f"{type(e).__name__}: {e}"
            finally:
                written["doc"] = json.loads(dest.read_text()) if dest.exists() else None
                fetch, TIERS, DEST = saved

    playoff = copy.deepcopy(fx["playoffGame"])
    check("fixture playoff game is gameType 3", playoff["gameType"], 3)
    playoff["homeTeam"] = copy.deepcopy(sjs_home)

    # A. Baseline: today's real schedule joins cleanly. Without this, every assertion
    # below could be passing for a reason unrelated to playoffs.
    out, problems, excluded = join(resp, tiers)
    check("A baseline: home games", len(out), 44)
    check("A baseline: problems", problems, [])
    check("A baseline: nothing excluded", excluded, [])
    check("A baseline: preseason tier", tier_counts(out).get("PRESEASON"), 2)
    rc, _, err = run_main(resp, tiers, write=True)
    check("A baseline write: main exits 0", (rc, err), (0, None))
    check("A baseline write: exclusions written, empty",
          (written["doc"] or {}).get(EXCLUDED_KEY), [])

    # B. A playoff home game: excluded, logged, and NOT a failure (ops#377).
    out, problems, excluded = join(with_extra(playoff), tiers)
    check("B playoff: no problems", problems, [])
    check("B playoff: not written to the schedule",
          playoff["id"] in {g["gameId"] for g in out}, False)
    check("B playoff: regular games all still there", len(out), 44)
    check("B playoff: exclusion recorded once", len(excluded), 1)
    # The exact record, which is what resolve_tm_events.py keys on: public schedule
    # fields only, and no tier.
    check("B playoff: exclusion record", excluded,
          [{"gameId": playoff["id"], "date": playoff["gameDate"], "gameType": "playoff",
            "opponent": playoff["awayTeam"]["abbrev"]}])
    for write in (False, True):
        rc, stdout, err = run_main(with_extra(playoff), tiers, write=write)
        check(f"B playoff write={write}: main does not crash", err, None)
        check(f"B playoff write={write}: main exits 0", rc, 0)
        # Asserted on BOTH paths: CI runs write=True, and a print that happened only on
        # --dry-run would hide the exclusion from every log anyone reads (mutant M11).
        check(f"B playoff write={write}: exclusion is printed by main",
              f"gameId {playoff['id']}" in stdout and "EXCLUDED" in stdout, True)
    doc = written["doc"] or {}
    check("B playoff write: playoff game not in written games",
          playoff["id"] in {g["gameId"] for g in doc.get("games", [])}, False)
    check("B playoff write: regular games all written", len(doc.get("games", [])), 44)
    check("B playoff write: exclusion written beside games", doc.get(EXCLUDED_KEY), excluded)

    # B2. A playoff game must not satisfy the tier table. Put one on the date of a real
    # tier entry whose regular game is missing from the response: the tier entry is
    # still an orphan. (Mutant M12 marked the playoff date matched and hid this; no real
    # calendar does this, which is why only a constructed case can check it.)
    gone = next(g for g in resp["games"] if g["homeTeam"]["abbrev"] == TEAM
                and g["gameType"] == 2)
    r = copy.deepcopy(resp)
    r["games"] = [g for g in r["games"] if g["id"] != gone["id"]]
    on_tier_date = copy.deepcopy(playoff)
    on_tier_date["gameDate"] = gone["gameDate"]
    r["games"].append(on_tier_date)
    _, problems, excluded = join(r, tiers)
    check("B2 playoff on a tier date: still excluded", len(excluded), 1)
    check("B2 playoff on a tier date: tier entry still orphaned",
          any(p.startswith(f"ORPHAN TIER ENTRY: {gone['gameDate']}") for p in problems), True)

    # C. The exclusion is keyed on gameType 3 ONLY. A game of a type this script does not
    # know must still fail loudly, and its message must not call it regular-season.
    odd = copy.deepcopy(playoff)
    odd["gameType"] = 99999
    out, problems, excluded = join(with_extra(odd), tiers)
    check("C unknown type: not excluded", excluded, [])
    check("C unknown type: NO TIER names its real type",
          [p for p in problems if p.startswith("NO TIER")],
          [f"NO TIER: {odd['gameDate']} vs {odd['awayTeam']['abbrev']} is a home game "
           f"(gameType unknown) with no tier entry"])
    rc, _, err = run_main(with_extra(odd), tiers)
    check("C unknown type: main does not crash", err, None)
    check("C unknown type: main exits 1", rc, 1)

    # D. A regular-season game missing from the tier table. Before ops#377 this crashed
    # main on a None tier key, so the NO TIER finding was never printed.
    short = copy.deepcopy(tiers)
    dropped = short["games"].pop()
    out, problems, _ = join(resp, short)
    check("D no tier: NO TIER reported for the regular game",
          any(p.startswith(f"NO TIER: {dropped['date']}") and "(gameType regular)" in p
              for p in problems), True)
    check("D no tier: counted under NONE", tier_counts(out).get("NONE"), 1)
    rc, stdout, err = run_main(resp, short)
    check("D no tier: main does not crash", err, None)
    check("D no tier: main exits 1", rc, 1)

    for f in fails:
        print(f"  FAIL {f}", file=sys.stderr)
    print(f"self-test: {'FAILED' if fails else 'passed'} ({len(fails)} failure(s))")
    return 1 if fails else 0


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
        description=f"Refresh {DEST.relative_to(ROOT)} from the NHL API and VALIDATE it "
                    f"against the hand-transcribed tier table on date and opponent.")
    ap.add_argument("--dry-run", action="store_true",
                    help=f"fetch and validate, but do not write "
                         f"{DEST.relative_to(ROOT)}")
    ap.add_argument("--self-test", action="store_true",
                    help="run the offline self-test against captured NHL responses")
    args = ap.parse_args()
    if args.self_test:
        return self_test()
    return main(write=not args.dry_run)


if __name__ == "__main__":
    sys.exit(_cli())
