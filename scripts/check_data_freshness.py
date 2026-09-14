#!/usr/bin/env python3
"""Fail loudly when the market series has holes in it (ops#24).

The collector is live and commits daily. Nothing noticed if it stopped. Four failure
modes were passing in silence, and the last two are the dangerous ones because they look
like success:

  1. The scheduled workflow stops firing. GitHub disables `schedule` triggers on repos
     with no activity for 60 days, and the season runs to April.
  2. TickPick changes its markup and every row fails. The total-failure guard keeps the
     store clean and exits non-zero, so the run goes red - but only if someone looks.
  3. A PARTIAL failure: 3 of 44 games error while 41 succeed. The run stays green and
     the series quietly develops holes in specific games.
  4. The event map goes stale and prices get recorded against a renamed id.

Why detect rather than backfill: TickPick has no historical price endpoint, so a missed
day is genuinely gone. The honest response is to show the hole - which is also what keeps
ops#8 from later fitting a curve to a series with gaps it does not know about.

Two modes on purpose:
  --strict  (collector workflow) staleness and per-game gaps are FATAL. A silent
            collector is worse than a noisy one.
  default   (npm run build) advisory. A local build on a day the collector has not run
            yet must not fail; that would make stale data block deploys, which is
            backwards - a deploy is how a fix ships.
"""

import argparse
import collections
import json
import pathlib
import re
import sys
from datetime import date, datetime, timedelta, timezone

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCHEDULE = ROOT / "data" / "schedule.json"

# Every source is checked, not just the primary. A secondary source going quiet is the
# easiest failure to miss: the summary's cross-source ratio simply disappears, which
# looks like "only one source configured" rather than "a source broke". Missing entirely
# is tolerated (it may not be set up); going STALE after having data is not.
STORES = [
    # (name, path, required, rolling, script). `rolling` means the source publishes a
    # WINDOW rather than the whole season, so games beyond its horizon are unlisted rather
    # than missing. Only set it where that is measured - it trades away a real guarantee.
    #
    # `script` exists so "is this source actually scheduled?" can be DERIVED from the
    # workflows rather than declared here. See scheduled_collectors().
    ("tickpick", ROOT / "data" / "market" / "tickpick.jsonl", True, False,
     "collect_tickpick.py"),
    ("gametime", ROOT / "data" / "market" / "gametime.jsonl", False, False,
     "collect_gametime.py"),
    ("ticketnetwork", ROOT / "data" / "market" / "ticketnetwork.jsonl", False, True,
     "collect_ticketnetwork.py"),
    ("scorebig", ROOT / "data" / "market" / "scorebig.jsonl", False, True,
     "collect_scorebig.py"),
]
STORE = STORES[0][1]
WORKFLOWS = ROOT / ".github" / "workflows"

# One missed day is a hiccup (a delayed cron, a transient 5xx). Two consecutive means
# something is broken.
STALE_DAYS = 2
# A game absent this many days running while others have data is a per-game hole.
GAME_GAP_DAYS = 3
# A day whose success rate falls this far below the previous day's is a partial failure.
SUCCESS_DROP = 0.15

# How far a ROLLING source's coverage may fall day-over-day before it is a fault rather
# than the calendar. A rolling window legitimately shrinks as games are played - but this
# schedule runs 44 games over ~7 months, so roughly one game (about 2%) leaves per day.
# A quarter is an order of magnitude beyond that and means the source served fewer events.
COVERAGE_DROP = 0.25

# ...and at least this many games in absolute terms.
#
# A RELATIVE threshold alone is unstable on a small base, and rolling sources shrink to a
# small base by design as the season ends. One game leaving to the calendar is 33% of a
# three-game window and 50% of a two-game one, so the check would fire on the calendar
# doing exactly what the calendar does - in the final week, when a red run that is actually
# fine is worst, because those are the last games anyone can still act on.
#
# Three games, because calendar attrition is about one game every two days and never three
# at once. The real regression this was built for was 29 -> 16, a drop of thirteen: it
# clears both gates comfortably, and so would any genuine collapse.
COVERAGE_DROP_MIN_GAMES = 3

# A coverage drop must PERSIST this many observation days before it is fatal.
#
# Measured 2026-09-13, and it contradicts what CLAUDE.md had recorded. TicketNetwork was
# believed to serve 16/44 to a datacenter runner and 29/44 to a residential client - an IP
# effect, settled in ops#56 on a single runner observation. The actual runner series is:
#
#   09-06: 16   09-07: 16   09-08: 29   09-11: 29   09-12: 29   09-13: 16
#
# Every one of those days was collected by github-actions[bot], so the runner served 29 on
# three separate days. The source FLAPS; it is not partitioned by IP. A day-over-day gate
# therefore fires on roughly every other run, and a check that cries wolf on a coin flip
# is one people learn to skip - the same reasoning behind the 2-day staleness threshold
# and the deleted empty-issue rule in check_issues.py.
#
# Two days, because that is the smallest window that distinguishes a flap from a step. A
# genuine regression - the source dropping events for good - stays fatal one day later
# than before, which costs nothing: nobody can backfill a rolling window anyway.
COVERAGE_DROP_PERSIST_DAYS = 2

# Outage days whose absence has been reviewed and consciously accepted.
#
# The gap check is right that a hole is permanent - TickPick has no historical endpoint,
# so a missed day is gone. But "permanent" cuts both ways: an unbackfillable hole makes
# --strict fail on EVERY subsequent run, forever, for a reason no future run can fix. Six
# consecutive red collector runs is what that looks like, and a permanently red gate
# cannot signal the next real break.
#
# Same treatment as .privacy-accepted, deliberately: accepted days are still PRINTED,
# they just stop being fatal. An accepted hole should stay visible rather than being
# erased from the output - ops#8 must not later fit a curve across a gap it cannot see.
#
# Committed, unlike .private-patterns, because the runner's --strict run is the one that
# needs it. A gap day NOT listed here is fatal exactly as before.
ACCEPTED_GAPS_FILE = ROOT / ".freshness-accepted"

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# `coverage <source> <n>` - a reviewed low mode for a FLAPPING rolling source. See
# load_accepted() and the COVERAGE FLOOR block in analyse() for what it does and does not
# exempt.
FLOOR_RE = re.compile(r"^coverage\s+([a-z0-9_]+)\s+(\d+)$")


def load_accepted(path: pathlib.Path | None = None) -> tuple[set[str], dict[str, int]]:
    """Reviewed findings: accepted outage dates, and accepted coverage floors.

    Two line forms, because two different things get consciously accepted here and both
    have the same failure mode if they cannot be - a gate that is red forever for a reason
    no future run can fix:

        2026-09-09                 an outage day whose data is unrecoverable
        coverage ticketnetwork 16  a low mode this source is known to flap into

    Everything after `#` is a comment and is the place to record WHY. Returns
    (dates, floors) rather than a bare set; callers want them separately.
    """
    path = path or ACCEPTED_GAPS_FILE
    if not path.exists():
        return set(), {}
    days: set[str] = set()
    floors: dict[str, int] = {}
    for ln in path.read_text().splitlines():
        ln = ln.split("#", 1)[0].strip()
        if not ln:
            continue
        if DATE_RE.match(ln):
            days.add(ln)
            continue
        m = FLOOR_RE.match(ln)
        if m:
            n = int(m.group(2))
            if n <= 0:
                # A floor of 0 exempts every level there is, which does not read as
                # "disable this check" to anyone skimming the file - it reads as a
                # reviewed value. Refuse it rather than accept the most dangerous input
                # the format allows.
                print(f"  ignoring non-positive coverage floor in {path.name}: {ln!r}",
                      file=sys.stderr)
                continue
            floors[m.group(1)] = n
            continue
        # Loud rather than silent: a typo here would quietly un-accept a gap and turn
        # the gate red again for a reason nobody could find.
        print(f"  ignoring unparseable line in {path.name}: {ln!r}", file=sys.stderr)
    return days, floors


def load(path: pathlib.Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]


def scheduled_collectors(workflow_text: dict[str, str]) -> set[str]:
    """Collector scripts that a SCHEDULED workflow actually runs.

    Derived rather than declared. A boolean flag saying "this source is scheduled" is a
    second copy of a fact the workflows already hold, and the hand-maintained copy loses -
    the same reasoning that is retiring the agent registry in ops#38.

    Two things deliberately do NOT count as scheduled:
      * a `--self-test` invocation, which proves nothing about collection
      * a workflow with no `schedule:` trigger, e.g. a manual-dispatch probe

    Passed the file contents rather than reading disk, so the rule is testable offline.
    """
    out: set[str] = set()
    for text in workflow_text.values():
        if "schedule:" not in text:
            continue
        for line in text.splitlines():
            if "--self-test" in line:
                continue
            for m in re.findall(r"scripts/(collect_[a-z_]+\.py)", line):
                out.add(m)
    return out


def workflow_text() -> dict[str, str]:
    if not WORKFLOWS.exists():
        return {}
    return {p.name: p.read_text() for p in sorted(WORKFLOWS.glob("*.yml"))}


def analyse(rows: list[dict], games: list[dict], today: date,
            rolling: bool = False, accepted: set[str] | None = None,
            coverage_floor: int | None = None) -> dict:
    accepted = accepted or set()
    days = sorted({r["observedDate"] for r in rows})
    ok_by_day = collections.Counter()
    tot_by_day = collections.Counter()
    for r in rows:
        tot_by_day[r["observedDate"]] += 1
        if r.get("ok") and r.get("low") is not None:
            ok_by_day[r["observedDate"]] += 1

    findings = []

    if not days:
        # Every key the full return provides. Callers index some of these directly, so an
        # early return with a narrower shape is a KeyError waiting for the first run of a
        # newly added source - which is exactly when it would fire.
        return {"days": [], "findings": [("fatal", "the market store is empty")],
                "staleDays": None, "okByDay": {}, "totByDay": {},
                "horizon": None, "beyondHorizon": [], "acceptedGaps": [],
                "acceptedFloor": None}

    last = date.fromisoformat(days[-1])
    stale = (today - last).days
    if stale >= STALE_DAYS:
        findings.append(("fatal", f"no observation for {stale} days (last {days[-1]}). "
                                  f"The collector may have stopped - check the workflow "
                                  f"is still enabled; GitHub disables schedules after 60 "
                                  f"days of repo inactivity."))

    # Missing calendar days inside the series - distinct from staleness at the end.
    expected = []
    d = date.fromisoformat(days[0])
    while d <= last:
        expected.append(d.isoformat())
        d += timedelta(days=1)
    missing = [x for x in expected if x not in set(days)]
    # An accepted outage day is still a hole and is still reported - it just stops being
    # fatal. Splitting here rather than filtering `missing` earlier keeps the accepted
    # days visible in the returned shape, so the caller can print them.
    seen_accepted = [x for x in missing if x in accepted]
    missing = [x for x in missing if x not in accepted]
    if missing:
        findings.append(("fatal" if len(missing) > 1 else "warn",
                         f"{len(missing)} day(s) missing inside the series: "
                         f"{missing[:5]}{'...' if len(missing) > 5 else ''}. "
                         f"Not backfillable - TickPick has no historical endpoint."))

    # Per-game holes. Only meaningful once the game has appeared at all; a game that has
    # never been collected is a resolver problem, reported separately.
    seen_per_game: dict[int, set] = collections.defaultdict(set)
    for r in rows:
        if r.get("ok") and r.get("low") is not None:
            seen_per_game[r["gameId"]].add(r["observedDate"])

    future = [g for g in games if g["date"] >= today.isoformat()]

    # THE COVERAGE HORIZON. A source may publish a ROLLING WINDOW rather than the whole
    # season: TicketNetwork lists 29 of 44 home games, and every one of the 15 it omits is
    # late-season, with zero orphans (ops#33). Those games are NOT YET LISTED, which is a
    # publisher's editorial choice, not a broken event map.
    #
    # Calling them "never priced" would fire a fatal on every single run, forever. A check
    # that cries wolf daily is precisely how people learn to skip its output - the same
    # reasoning that deleted the empty-issue rule from check_issues.py, and the same
    # reasoning behind the 2-day staleness threshold rather than an hours-based one.
    #
    # So the horizon is the latest game THIS SOURCE has actually priced. A gap at or
    # before it is real and stays fatal. Beyond it, absence of evidence is not evidence of
    # absence, and it is reported as coverage rather than as a fault.
    # OPT-IN, per source. Applying this to every source would silently weaken the
    # guarantee where it matters most: TickPick resolves all 44 events, so a game it has
    # never priced IS a stale event map, and excusing it because it happens to be the
    # latest game would hide exactly the bug this check was written for.
    unpriced = [g for g in future if g["gameId"] not in seen_per_game]
    if rolling:
        priced_dates = [g["date"] for g in games if g["gameId"] in seen_per_game]
        horizon = max(priced_dates) if priced_dates else None
        never = [g for g in unpriced if horizon is None or g["date"] <= horizon]
        beyond = [g for g in unpriced if horizon is not None and g["date"] > horizon]
    else:
        horizon, never, beyond = None, unpriced, []
    if never:
        findings.append(("fatal", f"{len(never)} upcoming game(s) have NEVER been priced: "
                                  f"{[g['date'] for g in never][:5]}. The event map may be "
                                  f"stale - re-run --resolve."))

    # How far a rolling source's coverage may fall before it is a fault rather than the
    # calendar. Defined here rather than at the coverage block below because the accepted
    # low mode is expressed in terms of it - one predicate, so the two cannot drift apart.
    def dropped(base: int, n: int) -> bool:
        lost = base - n
        return bool(base) and lost >= COVERAGE_DROP_MIN_GAMES and lost / base > COVERAGE_DROP

    # ACCEPTED LOW MODE, and which days it covers. See the COVERAGE FLOOR block below for
    # what a floor is and why it is a level rather than a duration.
    #
    # A day is in the source's accepted low mode when its coverage BOTH falls far enough
    # from this source's best day to clear dropped() AND lands at or above the reviewed
    # floor. Both halves matter, and the first one is not decoration:
    #
    #   clears dropped()    - otherwise every day below the series peak qualifies, and a
    #                         source quietly dropping ONE game would have every day after
    #                         its best one treated as an accepted flap. That is not
    #                         hypothetical - it was the first version of this band, and
    #                         the split-hole test below is what caught it.
    #   at or above floor   - below it is the collapse the check exists for, and those
    #                         days must keep counting normally
    #
    # Against the series peak rather than a second recorded constant: the high level
    # decays with the calendar as games are played, and a pinned number would silently
    # stop matching within a week. Late season, when real coverage falls under the floor,
    # the second clause stops matching and every day counts normally again - which is the
    # pre-floor behaviour, and correct.
    low_mode_days: set[str] = set()
    if rolling and coverage_floor is not None and ok_by_day:
        peak = max(ok_by_day.values())
        low_mode_days = {d for d in days
                         if ok_by_day[d] >= coverage_floor and dropped(peak, ok_by_day[d])}
    floor_applied = coverage_floor if low_mode_days else None

    for g in future:
        got = seen_per_game.get(g["gameId"])
        if not got:
            continue
        # Longest run of consecutive observation days where this game is absent but the
        # series as a whole has data. Deliberately not trailing-only: a hole in the
        # middle is just as damaging to ops#8, and a trailing-only check on a short
        # series is indistinguishable from "never priced", which is reported separately.
        #
        # A day in the source's accepted low mode is NEUTRAL here - it neither extends a
        # run nor resets one. THIS IS THE HALF THE FIRST VERSION OF THE FLOOR MISSED, and
        # it made the floor cosmetic: the coverage-drop gate below only fires on day two
        # of a low run, because its baseline is a fixed three-day lookback that slides.
        # From day three the coverage finding vanishes on its own - and these thirteen
        # absent games trip GAME_GAP_DAYS instead, so a three-day flap went red HARDER
        # than a two-day one, through a check the floor never touched. Verified against
        # the real store: a 2-day low run gave 0 fatal findings, a 3-day run gave 13.
        #
        # Neutral rather than reset, so a genuine per-game hole still accumulates ACROSS
        # a flap instead of being laundered by it.
        run = worst = 0
        for d in days:
            if d in low_mode_days:
                continue
            if d in got:
                run = 0
            else:
                run += 1
                worst = max(worst, run)
        if worst >= GAME_GAP_DAYS:
            findings.append(("fatal", f"{g['date']} vs {g['opponent']['abbrev']} absent for "
                                      f"{worst} consecutive observation days while other games "
                                      f"have data - a per-game hole, which stays green in CI."))

    # COVERAGE REGRESSION, for rolling sources only.
    #
    # The horizon rule accepts any coverage level as long as it starts from the front, so
    # a source that suddenly serves half as many events is recorded as a moved horizon
    # rather than a problem. That is not hypothetical: TicketNetwork was measured at 29/44
    # from a residential IP and delivered 16/44 on its first scheduled run from a GitHub
    # runner - a 45% drop, silently accepted, while the other three sources held steady.
    #
    # A rolling window DOES shrink legitimately as games are played, but slowly: this
    # schedule has 44 games over ~7 months, so at most about one game leaves per day,
    # around 2%. A drop of a quarter in a day is not the calendar.
    #
    # Only for rolling sources. A non-rolling source losing games is already fatal via the
    # never-priced check, and running both would report one fault twice.
    #
    # PERSISTENCE, added 2026-09-13. The gate above is necessary but not sufficient: it
    # compares two adjacent days, and TicketNetwork alternates between 29 and 16 on the
    # SAME runner (see COVERAGE_DROP_PERSIST_DAYS). So a drop is now fatal only once it
    # has held for COVERAGE_DROP_PERSIST_DAYS observation days against the level
    # immediately before it; a single-day dip is reported as a warning instead, which
    # keeps it visible without going red on a flap.
    # COVERAGE FLOOR, added 2026-09-14, after the persistence gate above went red on the
    # behaviour it was built to tolerate.
    #
    # Persistence was set to 2 days on 2026-09-13 as "the smallest window that
    # distinguishes a flap from a step". The series in hand at that moment already
    # refuted it: 09-06 and 09-07 were BOTH 16, and 09-08 came back to 29. The longest
    # observed low run was two days, so a two-day gate cannot separate this source's flap
    # from a step - it just delays the false red by one run, which is what it did on
    # 09-14 (09-13: 16, 09-14: 16, fatal).
    #
    # Raising the constant to 3 would be the same mistake with a bigger number: eight
    # observation days cannot support a run-length threshold, and the next four-day flap
    # would go red again. So the accepted LEVEL is recorded instead of a guessed duration.
    # A drop that lands at or above a source's reviewed floor stays a warning however long
    # it holds; below the floor, the persistence rule applies unchanged.
    #
    # What this does NOT exempt, deliberately:
    #   - any level below the floor - there the gate is unchanged, though "unchanged" is
    #     weaker than it sounds and is spelled out in .freshness-accepted rather than
    #     asserted here: dropped() still needs BOTH >= COVERAGE_DROP_MIN_GAMES games lost
    #     AND more than COVERAGE_DROP of the base, so 16 -> 12 was never fatal and is not
    #     fatal now. The floor did not create that hole; claiming it closed one would.
    #   - any other source - floors are per-source and must be reviewed one at a time
    #   - visibility - an exempted drop is still printed on every run, like an accepted
    #     outage day, for the reason .privacy-accepted prints accepted findings
    #
    # It exempts any sustained level AT OR ABOVE the floor, not only the level observed.
    # That is wider than "this source's known low mode" and is deliberate - the flap level
    # itself decays with the calendar, so a check pinned to 16 exactly would stop matching
    # within a week. The cost is that a novel intermediate level is exempted too.
    #
    # Late-season calendar attrition eventually takes coverage below any floor legitimately,
    # but it does so at about one game a day, which never clears dropped() - the floor
    # changes which drops are fatal, not whether the calendar can trigger one.

    if rolling and len(days) >= COVERAGE_DROP_PERSIST_DAYS + 1:
        base_day = days[-(COVERAGE_DROP_PERSIST_DAYS + 1)]
        base_n = ok_by_day[base_day]
        run = days[-COVERAGE_DROP_PERSIST_DAYS:]
        now_n = ok_by_day[run[-1]]
        if all(dropped(base_n, ok_by_day[d]) for d in run):
            if coverage_floor is not None and now_n >= coverage_floor:
                findings.append(("warn",
                                 f"coverage fell from {base_n} to {now_n} games after "
                                 f"{base_day} and has stayed down for {len(run)} "
                                 f"observation day(s) ({run[0]}..{run[-1]}), but {now_n} is "
                                 f"at or above the reviewed floor of {coverage_floor} - "
                                 f"see .freshness-accepted. This source is known to flap "
                                 f"between levels; below {coverage_floor} is still fatal."))
            else:
                findings.append(("fatal",
                                 f"coverage fell from {base_n} to {now_n} games "
                                 f"after {base_day} and has stayed down for "
                                 f"{len(run)} observation day(s) ({run[0]}..{run[-1]}). "
                                 f"A rolling window loses about one game a day to the "
                                 f"calendar, not a quarter of its coverage - the source is "
                                 f"serving fewer events, and the horizon rule would "
                                 f"otherwise record that as expected."))
        elif dropped(ok_by_day[days[-2]], ok_by_day[days[-1]]):
            findings.append(("warn",
                             f"coverage fell from {ok_by_day[days[-2]]} to "
                             f"{ok_by_day[days[-1]]} games between {days[-2]} and "
                             f"{days[-1]}, but has not yet held for "
                             f"{COVERAGE_DROP_PERSIST_DAYS} days. This source is known to "
                             f"flap - fatal if it stays down tomorrow."))
    elif rolling and len(days) >= 2 and dropped(ok_by_day[days[-2]], ok_by_day[days[-1]]):
        # Not enough history to tell a flap from a step. Say so rather than guessing.
        findings.append(("warn",
                         f"coverage fell from {ok_by_day[days[-2]]} to "
                         f"{ok_by_day[days[-1]]} games between {days[-2]} and {days[-1]}, "
                         f"on too short a series to confirm it has persisted."))

    # Partial-failure detection: success rate dropping day over day.
    for prev, cur in zip(days, days[1:]):
        a = ok_by_day[prev] / tot_by_day[prev] if tot_by_day[prev] else 0
        b = ok_by_day[cur] / tot_by_day[cur] if tot_by_day[cur] else 0
        if a - b > SUCCESS_DROP:
            findings.append(("warn", f"success rate fell from {a:.0%} on {prev} to {b:.0%} "
                                     f"on {cur} - a partial failure leaves CI green."))

    return {"days": days, "findings": findings, "staleDays": stale,
            "okByDay": dict(ok_by_day), "totByDay": dict(tot_by_day),
            "horizon": horizon, "beyondHorizon": [g["date"] for g in beyond],
            "acceptedGaps": seen_accepted, "acceptedFloor": floor_applied}


def run(strict: bool, today: date) -> int:
    games = json.loads(SCHEDULE.read_text())["games"]
    fatal: list[str] = []
    warn: list[str] = []
    any_data = False

    sched = scheduled_collectors(workflow_text())
    accepted, floors = load_accepted()
    accepted_seen: set[str] = set()
    floors_seen: set[str] = set()

    # A floor for a source that does not exist is a typo, and a silent one would leave a
    # real source unprotected while the file reads as if it were reviewed.
    for unknown in sorted(set(floors) - {n for n, *_ in STORES}):
        print(f"  ignoring coverage floor for unknown source {unknown!r} in "
              f"{ACCEPTED_GAPS_FILE.name}", file=sys.stderr)
    # A floor is only consulted for a ROLLING source. On any other it is inert - and an
    # inert line in a reviewed-accept file is worse than a missing one, because it reads
    # as protection that was considered and granted.
    for inert in sorted({n for n, _, _, rolling, _ in STORES if not rolling} & set(floors)):
        print(f"  coverage floor for {inert!r} is INERT - floors apply only to rolling "
              f"sources, and this one is not marked rolling in STORES", file=sys.stderr)

    for name, path, required, rolling, script in STORES:
        rows = load(path)
        if not rows:
            if required:
                print(f"{name}: NO DATA")
                fatal.append(f"{name} is the primary source and has no data at all")
            elif script in sched:
                # THE GAP THIS CLOSES (found in review, ops#39). A source that is actually
                # SCHEDULED and has still never produced a row is broken, not unconfigured
                # - and "optional source, skipped" made those two states identical. A
                # rolling source blocked from its very first run - which is exactly what a
                # datacenter IP block looks like - would have stayed green forever.
                print(f"{name}: SCHEDULED BUT EMPTY")
                fatal.append(f"{name} is scheduled in a workflow ({script}) but has never "
                             f"produced a row. That is a broken collector, not an "
                             f"unconfigured one - check the runner's verdict before "
                             f"assuming the source has no listings.")
            else:
                print(f"{name}: not collected (not scheduled in any workflow yet)")
            continue
        any_data = True
        a = analyse(rows, games, today, rolling, accepted, floors.get(name))
        print(f"{name}: {len(rows)} rows across {len(a['days'])} day(s), "
              f"{a['days'][0]} .. {a['days'][-1]}, last {a['staleDays']} day(s) ago")
        for d in a["days"][-3:]:
            print(f"    {d}: {a['okByDay'].get(d, 0)}/{a['totByDay'].get(d, 0)} priced")
        accepted_seen.update(a["acceptedGaps"])
        if a["acceptedGaps"]:
            # Printed on every run, never silently swallowed - the whole point of
            # accepting a finding rather than deleting it.
            print(f"    (accepted outage) {len(a['acceptedGaps'])} day(s) missing and "
                  f"reviewed: {a['acceptedGaps']} - see .freshness-accepted")
        if a["acceptedFloor"] is not None:
            # Printed on every run for the same reason accepted outage days are: an
            # accepted finding stays visible rather than disappearing.
            floors_seen.add(name)
            print(f"    (accepted coverage floor) {a['acceptedFloor']} games - a sustained "
                  f"drop to at least that level is reviewed, see .freshness-accepted")
        if a["beyondHorizon"]:
            print(f"    coverage horizon {a['horizon']}: "
                  f"{len(a['beyondHorizon'])} later game(s) not yet listed "
                  f"(rolling-window source, expected)")
        for lvl, m in a["findings"]:
            (fatal if lvl == "fatal" else warn).append(f"[{name}] {m}")

    if not any_data:
        print("no market data yet - nothing to check")
        return 1 if strict else 0

    for m in warn:
        print(f"  WARN {m}")

    if fatal:
        label = "PROBLEM" if strict else "WARNING (advisory - pass --strict to fail)"
        print(f"\n{len(fatal)} {label}(S):", file=sys.stderr)
        for m in fatal:
            print(f"  - {m}", file=sys.stderr)
        return 1 if strict else 0

    # Not "no gaps detected" - that would contradict the accepted-outage lines printed
    # above, and a check whose summary disagrees with its own body is one people stop
    # trusting. Say what is actually true: nothing unexplained.
    if accepted_seen or floors_seen:
        bits = []
        if accepted_seen:
            bits.append(f"{len(accepted_seen)} accepted outage day"
                        f"{'s' if len(accepted_seen) != 1 else ''}")
        if floors_seen:
            bits.append(f"{len(floors_seen)} accepted coverage floor"
                        f"{'s' if len(floors_seen) != 1 else ''}")
        print(f"\nfresh - no unexplained gaps "
              f"({' and '.join(bits)} still reported above)")
    else:
        print("\nfresh - no gaps detected")
    return 0


def self_test() -> int:
    fails = []

    def check(label, got, want):
        if got != want:
            fails.append(f"{label}: got {got!r}, want {want!r}")

    def games(n, start="2026-10-01"):
        d0 = date.fromisoformat(start)
        return [{"gameId": i, "date": (d0 + timedelta(days=i)).isoformat(),
                 "opponent": {"abbrev": "XXX"}} for i in range(n)]

    def rows(days, gameids, ok=True):
        return [{"observedDate": d, "gameId": g, "ok": ok, "low": 50 if ok else None}
                for d in days for g in gameids]

    today = date(2026, 9, 10)
    gs = games(3, "2026-09-20")
    ids = [0, 1, 2]

    # A healthy series: three consecutive days ending today.
    healthy = ["2026-09-08", "2026-09-09", "2026-09-10"]
    a = analyse(rows(healthy, ids), gs, today)
    check("healthy series is clean", a["findings"], [])

    # Stale: last observation four days ago.
    a = analyse(rows(["2026-09-05", "2026-09-06"], ids), gs, today)
    check("staleness is fatal", any(l == "fatal" and "no observation for" in m
                                    for l, m in a["findings"]), True)

    # A hole in the middle.
    a = analyse(rows(["2026-09-08", "2026-09-10"], ids), gs, today)
    check("interior gap detected", any("missing inside the series" in m
                                       for _, m in a["findings"]), True)

    # A per-game hole while other games are fine. Needs a series long enough that a
    # 3-day absence is distinguishable from the game never having been priced.
    longer = ["2026-09-06", "2026-09-07", "2026-09-08", "2026-09-09", "2026-09-10"]
    r = rows(longer, [0, 1]) + rows(["2026-09-06", "2026-09-07"], [2])
    a = analyse(r, gs, today)
    check("per-game trailing hole detected", any("per-game hole" in m
                                                 for _, m in a["findings"]), True)
    # And a hole in the MIDDLE, which a trailing-only check would miss entirely.
    r = rows(longer, [0, 1]) + rows(["2026-09-06", "2026-09-10"], [2])
    a = analyse(r, gs, today)
    check("per-game interior hole detected", any("per-game hole" in m
                                                 for _, m in a["findings"]), True)
    # A single missed day for one game is noise, not a hole.
    r = rows(longer, [0, 1]) + rows([d for d in longer if d != "2026-09-08"], [2])
    a = analyse(r, gs, today)
    check("one missed day for a game is not flagged", any("per-game hole" in m
                                                          for _, m in a["findings"]), False)

    # ---- coverage regression on a rolling source ----
    # Measured, not hypothetical: TicketNetwork gave 29/44 residential and 16/44 on its
    # first scheduled runner run, and the horizon rule recorded it as "expected".
    # ASSERT THE LEVEL, not just the text. This check previously read
    # `any("coverage fell" in m ...)`, which passes whether the finding is fatal or a
    # warning - so when persistence was added it would have gone on passing while the
    # behaviour it guards silently changed from fatal to warn. Same class of bug as the
    # `grep -c` in ops#56: a plausible green on a question never actually asked.
    def levels(a, needle):
        return sorted({l for l, m in a["findings"] if needle in m})

    # A drop that PERSISTS for two days against the level before it is fatal.
    three = ["2026-09-08", "2026-09-09", "2026-09-10"]
    r = (rows(three[:1], list(range(29)))
         + rows(three[1:2], list(range(16)))
         + rows(three[2:], list(range(16))))
    a = analyse(r, games(30, "2026-09-20"), date(2026, 9, 10), rolling=True)
    check("a sustained 45% coverage drop is fatal", levels(a, "coverage fell"), ["fatal"])

    # THE FLAP, measured rather than imagined: TicketNetwork served 29, 29, then 16 from
    # the same GitHub runner, and 29 again on days either side of earlier 16s. A
    # day-over-day gate calls that fatal on roughly every other run. One dip is a warning.
    r = (rows(three[:1], list(range(29)))
         + rows(three[1:2], list(range(29)))
         + rows(three[2:], list(range(16))))
    a = analyse(r, games(30, "2026-09-20"), date(2026, 9, 10), rolling=True)
    check("a one-day coverage dip is a warning, not fatal",
          levels(a, "coverage fell"), ["warn"])

    # ...and it must still be REPORTED. Silence here would be worse than the false red.
    check("the dip is still reported",
          any("coverage fell" in m for _, m in a["findings"]), True)

    # ---- the accepted coverage floor ----
    # THE REAL SERIES, not a hypothetical. TicketNetwork on the same GitHub runner:
    # 09-06:16 09-07:16 09-08:29 09-11:29 09-12:29 09-13:16 09-14:16. The two-day low run
    # at the start is the one the persistence gate was tuned past on 09-13 without being
    # checked against it, and the two-day low run at the end is what went red on 09-14.
    real = ["2026-09-06", "2026-09-07", "2026-09-08", "2026-09-11",
            "2026-09-12", "2026-09-13", "2026-09-14"]
    hi, lo = list(range(29)), list(range(16))
    r = sum([rows([d], lo if n == 16 else hi)
             for d, n in zip(real, [16, 16, 29, 29, 29, 16, 16])], [])
    gs29 = games(30, "2026-09-20")

    # Unfloored, this is exactly the 2026-09-14 red run.
    a = analyse(r, gs29, date(2026, 9, 14), rolling=True)
    check("the real 09-14 series is fatal with no floor",
          levels(a, "coverage fell"), ["fatal"])
    check("no floor reports none", a["acceptedFloor"], None)

    # Floored at the reviewed low mode, it is a warning - and still reported.
    a = analyse(r, gs29, date(2026, 9, 14), rolling=True, coverage_floor=16)
    check("a sustained drop to the reviewed floor is a warning",
          levels(a, "coverage fell"), ["warn"])
    check("the exempted drop stays visible",
          any("coverage fell" in m for _, m in a["findings"]), True)
    check("the applied floor is reported", a["acceptedFloor"], 16)

    # ...and the floor exempts that level ONLY. One game below it and the gate is live
    # again - otherwise accepting a flap would quietly accept the collapse underneath it.
    r15 = (rows(real[:1], hi) + rows(real[1:2], hi) + rows(real[2:3], hi)
           + rows(real[3:4], hi) + rows(real[4:5], hi)
           + rows(real[5:6], list(range(15))) + rows(real[6:], list(range(15))))
    a = analyse(r15, gs29, date(2026, 9, 14), rolling=True, coverage_floor=16)
    check("a drop BELOW the floor is still fatal",
          levels(a, "coverage fell"), ["fatal"])
    check("a fatal drop reports no applied floor", a["acceptedFloor"], None)

    # THE DEFECT THE FIRST VERSION SHIPPED, and the reason these cases exist at all. The
    # floor was tested only at a 2-day low run - the exact length at which it worked. The
    # coverage-drop gate fires only on day TWO of a run (its baseline is a sliding 3-day
    # lookback), so from day three the coverage finding vanishes by itself and the 13
    # absent games trip GAME_GAP_DAYS instead. A 3-day flap therefore went red HARDER than
    # a 2-day one, through a check the floor never touched. Measured against the real
    # store before the fix: 2-day run -> 0 fatal, 3-day run -> 13 fatal.
    #
    # ASSERT NO FATAL AT ALL, not merely the absence of a "coverage fell" finding. Naming
    # the string would pass against precisely the bug being fixed, since the finding that
    # fired was a per-game hole and never mentioned coverage.
    def fatals(a):
        return [m for l, m in a["findings"] if l == "fatal"]

    for n_low in (2, 3, 5, 9):
        run_days = [(date(2026, 9, 8) + timedelta(days=i)).isoformat()
                    for i in range(3 + n_low)]
        r = (sum([rows([d], hi) for d in run_days[:3]], [])
             + sum([rows([d], lo) for d in run_days[3:]], []))
        a = analyse(r, games(30, "2026-09-20"), date.fromisoformat(run_days[-1]),
                    rolling=True, coverage_floor=16)
        check(f"a {n_low}-day low run at the floor is not fatal", fatals(a), [])
        check(f"the floor is reported on a {n_low}-day low run", a["acceptedFloor"], 16)

    # ...and the neutrality must not launder a REAL per-game hole. Game 0 is absent on the
    # full-coverage days too, which is the source dropping one game rather than flapping.
    run_days = [(date(2026, 9, 8) + timedelta(days=i)).isoformat() for i in range(8)]
    r = (sum([rows([d], list(range(1, 29))) for d in run_days[:3]], [])
         + sum([rows([d], lo) for d in run_days[3:]], []))
    a = analyse(r, games(30, "2026-09-20"), date.fromisoformat(run_days[-1]),
                rolling=True, coverage_floor=16)
    check("a game absent on FULL-coverage days is still a per-game hole",
          any("per-game hole" in m for l, m in a["findings"] if l == "fatal"), True)

    # NEUTRAL, NOT RESET - and this is the case that proves the difference. A per-game
    # hole SPLIT by a flap must still accumulate across it: two absences, a flap, two more
    # absences is a four-day hole, not two two-day ones. Resetting the run on low-mode
    # days passes every other test here and launders exactly this, which is how it
    # survived the first mutation pass.
    split = [(date(2026, 9, 8) + timedelta(days=i)).isoformat() for i in range(7)]
    r = (rows(split[:1], hi)                      # game 0 present
         + sum([rows([d], list(range(1, 29))) for d in split[1:3]], [])   # absent, full
         + sum([rows([d], list(range(1, 17))) for d in split[3:5]], [])   # absent, low mode
         + sum([rows([d], list(range(1, 29))) for d in split[5:]], []))   # absent, full
    a = analyse(r, games(30, "2026-09-20"), date.fromisoformat(split[-1]),
                rolling=True, coverage_floor=16)
    check("a per-game hole split by a flap still accumulates across it",
          any("per-game hole" in m for l, m in a["findings"] if l == "fatal"), True)

    # A sustained drop BELOW the floor must stay live past day two as well - that is the
    # collapse the whole check exists for, and it is the mirror of the bug above.
    r = (sum([rows([d], hi) for d in run_days[:3]], [])
         + sum([rows([d], list(range(11))) for d in run_days[3:]], []))
    a = analyse(r, games(30, "2026-09-20"), date.fromisoformat(run_days[-1]),
                rolling=True, coverage_floor=16)
    check("a sustained drop below the floor is still fatal on day five",
          bool(fatals(a)), True)
    # No in-band day anywhere in THIS fixture, so nothing was exempted and the printed
    # line must not claim otherwise. Not a general property: a real series that flapped
    # before collapsing has in-band days in its history and reports the floor as in
    # effect, which is the honest reading of that line.
    check("a series with no in-band day reports no applied floor",
          a["acceptedFloor"], None)

    # A floor must not resurrect a finding that was never there. A healthy source with a
    # floor set reports neither a drop nor an applied floor.
    r = sum([rows([d], hi) for d in real], [])
    a = analyse(r, gs29, date(2026, 9, 14), rolling=True, coverage_floor=16)
    check("a floor on a healthy source changes nothing",
          any("coverage fell" in m for _, m in a["findings"]), False)
    check("a healthy source reports no applied floor", a["acceptedFloor"], None)

    # Two days is too short to tell a flap from a step, so it warns rather than guessing.
    two = ["2026-09-09", "2026-09-10"]
    r = rows(two[:1], list(range(29))) + rows(two[1:], list(range(16)))
    a = analyse(r, games(30, "2026-09-20"), date(2026, 9, 10), rolling=True)
    check("a drop on too short a series is a warning",
          levels(a, "coverage fell"), ["warn"])

    # ---- accepted outage days ----
    # The real case: two consecutive days lost to the ops#135 outage. Unaccepted they are
    # fatal forever, which is six red runs and counting; accepted they stay visible.
    holed = ["2026-09-06", "2026-09-07", "2026-09-10"]
    a = analyse(rows(holed, ids), gs, today)
    check("an unaccepted multi-day gap is fatal",
          levels(a, "missing inside the series"), ["fatal"])
    check("unaccepted gap reports no accepted days", a["acceptedGaps"], [])

    a = analyse(rows(holed, ids), gs, today, accepted={"2026-09-08", "2026-09-09"})
    check("a fully accepted gap is no longer fatal",
          any("missing inside the series" in m for _, m in a["findings"]), False)
    check("accepted days stay visible in the output",
          a["acceptedGaps"], ["2026-09-08", "2026-09-09"])

    # PARTIAL acceptance must NOT excuse the rest. Accepting one of two missing days
    # leaves a real, unexplained hole, and that stays a finding.
    a = analyse(rows(holed, ids), gs, today, accepted={"2026-09-08"})
    check("an unaccepted day beside an accepted one still fires",
          any("missing inside the series" in m for _, m in a["findings"]), True)
    check("...and the accepted one is still listed", a["acceptedGaps"], ["2026-09-08"])

    # A date that is not missing at all must not be reported as an accepted gap.
    a = analyse(rows(healthy, ids), gs, today, accepted={"2026-09-08"})
    check("accepting a present day reports nothing", a["acceptedGaps"], [])

    # ---- the accepted-gaps file parser ----
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        f = pathlib.Path(td) / ".freshness-accepted"
        f.write_text("# a comment\n\n2026-09-09\n2026-09-10  # trailing reason\n"
                     "not-a-date\ncoverage ticketnetwork 16  # reviewed low mode\n"
                     "coverage 99\n")
        got_days, got_floors = load_accepted(f)
    check("parser reads dates, skips comments and junk", sorted(got_days),
          ["2026-09-09", "2026-09-10"])
    check("parser reads coverage floors", got_floors, {"ticketnetwork": 16})
    check("a missing accepted file is empty, not an error",
          load_accepted(pathlib.Path("/nonexistent/.freshness-accepted")), (set(), {}))

    # The committed file must actually parse - a typo here silently un-accepts a finding
    # and turns the gate red for a reason nobody can find. Reading the real file rather
    # than a fixture is the point: fixtures cannot catch a typo in the thing that ships.
    real_days, real_floors = load_accepted()
    check("the committed accept file names only real sources",
          sorted(set(real_floors) - {n for n, *_ in STORES}), [])

    # PIN THE VALUE, not just the key. A floor is a reviewed number, and the dangerous
    # edit is not a typo'd source name - it is a floor quietly lowered, which disables the
    # coverage check for that source while printing a line that reads like protection.
    # Changing this pin is then a deliberate act with a diff, which is the whole point.
    check("the committed floors are exactly the reviewed ones",
          real_floors, {"ticketnetwork": 16})

    # A floor of 0 exempts every level there is. It must not parse.
    with tempfile.TemporaryDirectory() as td:
        f = pathlib.Path(td) / ".freshness-accepted"
        f.write_text("coverage ticketnetwork 0\n")
        check("a zero floor is refused, not accepted", load_accepted(f)[1], {})

    # LATE SEASON, small base. One game leaving is 33% of a three-game window - over the
    # relative threshold, and pure calendar. Firing here would go red in the final week,
    # on the last games anyone can still act on, which is the worst possible time to be
    # crying wolf.
    small = ["2026-09-09", "2026-09-10"]
    r = rows(small[:1], [0, 1, 2]) + rows(small[1:], [0, 1])
    a = analyse(r, games(30, "2026-09-20"), date(2026, 9, 10), rolling=True)
    check("one game leaving a 3-game window is the calendar, not a fault",
          any("coverage fell" in m for _, m in a["findings"]), False)
    # THE LIMITATION, asserted rather than discovered later. Losing TWO of three games is a
    # 67% collapse and it does NOT fire, because two is below the absolute floor.
    #
    # That is the deliberate trade, not an oversight. The floor only ever binds on a small
    # base, which only happens late in the season - when a missed collapse costs least
    # (the series is nearly complete) and a false alarm costs most (a red run on the last
    # games anyone can act on is one people learn to ignore). Early and mid-season the base
    # is large and the floor never binds, because a real regression is 29 -> 16.
    r = rows(small[:1], [0, 1, 2]) + rows(small[1:], [0])
    a = analyse(r, games(30, "2026-09-20"), date(2026, 9, 10), rolling=True)
    check("losing two of three does NOT fire - the floor binds, deliberately",
          any("coverage fell" in m for _, m in a["findings"]), False)

    # A collapse on a base big enough for the floor to clear still fires.
    r = rows(small[:1], list(range(12))) + rows(small[1:], [0, 1])
    a = analyse(r, games(30, "2026-09-20"), date(2026, 9, 10), rolling=True)
    check("a 12 -> 2 collapse fires", any("coverage fell" in m for _, m in a["findings"]), True)

    # One game leaving to the calendar must NOT fire - that is the whole reason for a
    # threshold rather than any-drop-at-all.
    r = rows(two[:1], list(range(29))) + rows(two[1:], list(range(28)))
    a = analyse(r, games(30, "2026-09-20"), date(2026, 9, 10), rolling=True)
    check("losing one game a day is the calendar, not a fault",
          any("coverage fell" in m for _, m in a["findings"]), False)

    # A NON-rolling source is covered by the never-priced check; running both would report
    # one fault twice.
    r = rows(two[:1], list(range(29))) + rows(two[1:], list(range(16)))
    a = analyse(r, games(30, "2026-09-20"), date(2026, 9, 10), rolling=False)
    check("non-rolling sources are not double-reported",
          any("coverage fell" in m for _, m in a["findings"]), False)

    # A single day cannot regress against anything.
    a = analyse(rows(two[:1], list(range(29))), games(30, "2026-09-20"),
                date(2026, 9, 10), rolling=True)
    check("one day of data cannot regress",
          any("coverage fell" in m for _, m in a["findings"]), False)

    # ---- scheduled-ness is DERIVED from the workflows (ops#39) ----
    # A source that is genuinely scheduled and has never produced a row is BROKEN, not
    # unconfigured. Those two states were identical before this, so a rolling source
    # blocked from its first run - what a datacenter IP block looks like - stayed green.
    sched_wf = ("on:\n  schedule:\n    - cron: \"7 15 * * *\"\n"
                "    - run: python3 scripts/collect_tickpick.py\n")
    check("a scheduled workflow's collector is detected",
          scheduled_collectors({"a.yml": sched_wf}), {"collect_tickpick.py"})
    # A manual-dispatch-only workflow is not a schedule. probe.yml runs collectors' URLs
    # but collects nothing on a cron, and counting it would mark sources scheduled that
    # are not.
    manual = "on:\n  workflow_dispatch:\n    - run: python3 scripts/collect_scorebig.py\n"
    check("workflow_dispatch alone is not scheduled",
          scheduled_collectors({"b.yml": manual}), set())
    # A self-test invocation proves nothing about collection.
    st = ("on:\n  schedule:\n    - cron: \"0 1 * * *\"\n"
          "    - run: python3 scripts/collect_scorebig.py --self-test\n")
    check("a --self-test line does not count as scheduled",
          scheduled_collectors({"c.yml": st}), set())
    # Both together: only the real invocation counts.
    both = st + "    - run: python3 scripts/collect_gametime.py\n"
    check("a real invocation beside a self-test still counts",
          scheduled_collectors({"d.yml": both}), {"collect_gametime.py"})
    check("no workflows at all is empty, not an error", scheduled_collectors({}), set())

    # Every store must name a script, or scheduled-ness cannot be derived for it and the
    # gap silently reopens for that source.
    for entry in STORES:
        if len(entry) != 5 or not entry[4].startswith("collect_"):
            fails.append(f"STORES entry {entry[0]!r} does not name a collector script")

    # ---- the coverage horizon (ops#33, ops#36) ----
    # A rolling-window source that has priced games 0 and 1 but not the later game 2 is
    # covering what it publishes, not failing. Fatal here would fire every run forever.
    a = analyse(rows(healthy, [0, 1]), gs, today, rolling=True)
    check("beyond the horizon is not fatal",
          any("NEVER been priced" in m for _, m in a["findings"]), False)
    check("beyond-horizon games are reported as coverage", a["beyondHorizon"],
          ["2026-09-22"])
    check("horizon is the latest game actually priced", a["horizon"], "2026-09-21")

    # But a hole INSIDE the covered window is a real gap and must stay fatal - otherwise
    # the horizon rule would excuse exactly the failure this check exists to catch.
    a = analyse(rows(healthy, [0, 2]), gs, today, rolling=True)
    check("a gap inside the covered window is still fatal",
          any("NEVER been priced" in m for _, m in a["findings"]), True)
    check("and nothing is excused as beyond-horizon", a["beyondHorizon"], [])

    # A source with no data at all has no horizon, so every future game is required -
    # otherwise a totally broken source would silently excuse itself.
    a = analyse(rows(healthy, []), gs, today, rolling=True)
    check("no data means no horizon, so nothing is excused", a["horizon"], None)

    # The guarantee TickPick relies on must survive all of the above: without rolling,
    # an unpriced later game is still a stale-event-map fatal.
    a = analyse(rows(healthy, [0, 1]), gs, today, rolling=False)
    check("a non-rolling source still fails on a never-priced game",
          any("NEVER been priced" in m for _, m in a["findings"]), True)
    check("and nothing is excused for it", a["beyondHorizon"], [])

    # A game never priced at all.
    a = analyse(rows(healthy, [0, 1]), gs, today)
    check("never-priced game detected", any("NEVER been priced" in m
                                            for _, m in a["findings"]), True)

    # Partial failure: success rate collapses on the last day.
    r = rows(healthy[:2], ids) + [
        {"observedDate": healthy[2], "gameId": 0, "ok": True, "low": 50},
        {"observedDate": healthy[2], "gameId": 1, "ok": False, "low": None},
        {"observedDate": healthy[2], "gameId": 2, "ok": False, "low": None},
    ]
    a = analyse(r, gs, today)
    check("success-rate drop detected", any("success rate fell" in m
                                            for _, m in a["findings"]), True)

    # Past games must not be demanded.
    past = [{"gameId": 9, "date": "2026-08-01", "opponent": {"abbrev": "OLD"}}]
    a = analyse(rows(healthy, ids), gs + past, today)
    check("past games not flagged as missing",
          any("NEVER been priced" in m and "2026-08-01" in m for _, m in a["findings"]), False)

    # Empty store.
    a = analyse([], gs, today)
    check("empty store is fatal", a["findings"][0][0], "fatal")

    # One missed day is a warning, not fatal - a delayed cron should not page anyone.
    a = analyse(rows(["2026-09-08", "2026-09-10"], ids), gs, today)
    lvls = [l for l, m in a["findings"] if "missing inside the series" in m]
    check("single interior gap is a warning", lvls, ["warn"])

    for f in fails:
        print(f"  FAIL {f}", file=sys.stderr)
    print(f"self-test: {'FAILED' if fails else 'passed'} ({len(fails)} failure(s))")
    return 1 if fails else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--strict", action="store_true", help="treat gaps as fatal")
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--today", help="override today's date, for testing")
    args = ap.parse_args()
    if args.self_test:
        return self_test()
    today = date.fromisoformat(args.today) if args.today else datetime.now(timezone.utc).date()
    return run(args.strict, today)


if __name__ == "__main__":
    sys.exit(main())
