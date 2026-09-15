#!/usr/bin/env python3
"""Derive a small per-game market summary the app can import at build time.

The JSONL store is the source of truth; this is a pure derivation from it, so it can be
rebuilt at any time without re-collecting. Kept separate from the collector for exactly
that reason - a derivation that can only be produced by a network call is not
reproducible.

WHAT THESE NUMBERS ARE, and are not. TickPick's AggregateOffer is the cheapest and
priciest listing in the WHOLE ARENA, all-in. It is not a comp for any particular seat:
the low is almost always an upper-deck single. Section-level data would be a real comp,
and TickPick cannot provide it - the listing grid sits behind a /ajax/ path that
robots.txt disallows (see collect_tickpick.py). So `low` answers "what does the cheapest
way into this game cost", which is a genuine demand signal and nothing more.

It is also a COMP MARKET, not the channel Wesley sells on. Ticketmaster blocks both a
runner and a residential browser. Anything downstream that treats these as his own
achievable prices is wrong.
"""

import argparse
import json
import os
import subprocess
import pathlib
import sys
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCHEDULE = ROOT / "data" / "schedule.json"
DEST = ROOT / "data" / "market" / "summary.json"

# Primary first. TickPick is primary because its prices are all-in by design and because
# it covers all 44 home games. Both matter: a headline low that hides fees would misprice
# every break-even downstream, and a source publishing only a rolling window cannot be the
# spine of a season-long series.
#
# It is NOT chosen for being the cheapest, and the comment here used to say it was -
# "measured consistently LOWER than Gametime on every game, so the conservative choice".
# That rested on a two-source comparison, and a third source contradicts it: ScoreBig asks
# ~5% less than TickPick on all 19 games they share (max ratio 1.025, so essentially never
# above). Recorded rather than quietly corrected, because a rule the codebase visibly
# contradicts is one people stop reading (ops#21, ops#46).
#
# Revisit deliberately, not by drift, if a full-coverage all-in source is ever measured
# consistently below TickPick across the whole season.
STORES = [
    ("tickpick", ROOT / "data" / "market" / "tickpick.jsonl"),
    ("gametime", ROOT / "data" / "market" / "gametime.jsonl"),
    # Publishes Offer.price - a scalar ask, so it contributes a LOW and no high. It also
    # covers a rolling window rather than the season, so it is absent for late-season
    # games by design rather than by failure. See collect_ticketnetwork.py.
    ("ticketnetwork", ROOT / "data" / "market" / "ticketnetwork.jsonl"),
    # Rolling window too, and its declared UTC offset is a fixed -08:00 that is wrong
    # for half the season - collect_scorebig.py joins on wall clock. Prices arrive as
    # STRINGS from this source and are coerced at its boundary, not here.
    ("scorebig", ROOT / "data" / "market" / "scorebig.jsonl"),
]
PRIMARY = "tickpick"


def load_rows(path: pathlib.Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]


def _series_by_game(rows: list[dict]) -> dict[int, list[dict]]:
    by_game: dict[int, list[dict]] = {}
    for r in rows:
        if r.get("ok") and r.get("low") is not None:
            by_game.setdefault(r["gameId"], []).append(r)
    for v in by_game.values():
        v.sort(key=lambda r: r["observedDate"])
    return by_game


def summarize(rows_by_source: dict[str, list[dict]], games: list[dict]) -> dict:
    per_source = {name: _series_by_game(rows) for name, rows in rows_by_source.items()}
    by_game = per_source.get(PRIMARY, {})

    out = []
    # Ratios kept PER SOURCE, never pooled. Pooling was the bug ops#43 found: the two
    # rolling sources contribute only their forward window, which is measurably cheaper
    # (ScoreBig covered $33 vs uncovered $48), so a single pooled median silently mixed
    # "secondary sources ask more" with "early-season games cost less".
    ratios_by_source: dict[str, list[float]] = {}
    for g in games:
        series = by_game.get(g["gameId"], [])
        if not series:
            continue
        first, last = series[0], series[-1]
        entry = {
            "gameId": g["gameId"],
            "date": g["date"],
            "low": last["low"],
            # .get, not []: a source may publish a low without a high. Gametime does on
            # some events, and indexing crashed the whole summary on the first run with
            # two sources. A missing high is an absent field, not a failure.
            "high": last.get("high"),
            "observedDate": last["observedDate"],
            "observations": len(series),
        }
        # A delta is only meaningful with more than one day in hand. Emitting 0 on a
        # single observation would render as "flat", which is a claim we cannot make.
        if len(series) > 1:
            entry["lowFirst"] = first["low"]
            entry["lowFirstDate"] = first["observedDate"]
            entry["lowDelta"] = round(last["low"] - first["low"], 2)

        # Every other source is kept ALONGSIDE rather than blended. Two sources
        # disagreeing is the signal that one has gone wrong; averaging them destroys it.
        others = {}
        for name, bg in per_source.items():
            if name == PRIMARY:
                continue
            s2 = bg.get(g["gameId"], [])
            if s2:
                others[name] = {"low": s2[-1]["low"], "high": s2[-1].get("high"),
                                "observedDate": s2[-1]["observedDate"]}
                if last["low"]:
                    ratios_by_source.setdefault(name, []).append(s2[-1]["low"] / last["low"])
        if others:
            entry["otherSources"] = others
        out.append(entry)

    all_rows = [r for rs in rows_by_source.values() for r in rs]
    days = sorted({r["observedDate"] for r in all_rows})

    def _median(v: list[float]) -> float:
        rs = sorted(v)
        mid = len(rs) // 2
        return rs[mid] if len(rs) % 2 else (rs[mid - 1] + rs[mid]) / 2

    cross = None
    if ratios_by_source:
        per = {}
        for name in sorted(ratios_by_source):
            v = ratios_by_source[name]
            per[name] = {
                "games": len(v),
                "medianRatioToPrimary": round(_median(v), 4),
                "minRatio": round(min(v), 4),
                "maxRatio": round(max(v), 4),
            }
        # Games every CONTRIBUTING source priced. The only set on which the sources can be
        # compared like for like - reported so a reader can see how narrow it is, rather
        # than discovering later that a headline number rested on it.
        #
        # Restricted to sources that actually produced a ratio, plus the primary. The
        # first version intersected over every source in STORES, so a single EMPTY store -
        # a collector that has not run yet, a newly added fifth source, a run that wrote
        # nothing - forced this to 0 while the populated sources overlapped perfectly.
        #
        # The failure direction is what makes it worth fixing rather than noting: it
        # UNDERSTATES agreement, on the figure this commit added to make cross-source
        # honesty visible, on the page Wesley reads before pricing. A metric whose whole
        # job is "how much can you trust the comparison" must not itself report less
        # confidence than the data supports. Found in review.
        contributing = set(ratios_by_source) | {PRIMARY}
        covered = [set(per_source[n].keys()) for n in contributing
                   if per_source.get(n)]
        common = len(set.intersection(*covered)) if covered else 0
        cross = {
            "perSource": per,
            "commonGames": common,
            "_comment": (
                "Each secondary source's low divided by the primary's, PER SOURCE and never "
                "pooled. Pooling was wrong and is why this shape changed (ops#43/ops#44): the "
                "rolling-window sources publish only a forward slice, and that slice is "
                "measurably cheaper - ScoreBig's covered games had a $33 median low against "
                "$48 for the ones it omits, with the date ranges disjoint. A pooled median "
                "therefore mixed 'secondary sources ask more' with 'early-season games cost "
                "less' and was read as the first. "
                "Each source's figure now stands over its own games, with n stated. "
                "Sources agreeing on ORDER but differing in LEVEL is the expected shape - "
                "they price the same demand with different inventory and fee treatment "
                "(all six pairwise Spearman rho 0.98-0.99). A sudden move in one source's "
                "ratio means THAT SOURCE changed, not the market."
            ),
        }
    return {
        "_comment": (
            "Derived from the per-source stores in data/market/ by "
            "scripts/summarize_market.py. "
            "low/high are the cheapest and priciest listings in the WHOLE ARENA, all-in, "
            "on TickPick - NOT a comp for any specific seat, and NOT the channel these "
            "tickets are sold on. See the script docstring."
        ),
        "generatedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": PRIMARY,
        "sources": sorted(rows_by_source),
        "crossSource": cross,
        "priceBasis": "all_in_whole_arena",
        "isOwnChannel": False,
        "confidence": "measured_single_point" if len(days) < 2 else "measured",
        "observationDays": len(days),
        "firstObservedDate": days[0] if days else None,
        "lastObservedDate": days[-1] if days else None,
        "games": out,
    }


# Which tracked store each script writes.
_WRITES = {"summarize_market.py": "data/market/summary.json",
           "fetch_schedule.py": "data/schedule.json",
           "comps.py": "data/primary/comps.json"}

# Which of them can be run with --dry-run INSIDE THIS SUITE. fetch_schedule.py cannot:
# its main() calls fetch() unconditionally and --dry-run gates only the write, so running
# it here would put a live NHL request inside a suite CLAUDE.md says is offline - and at
# the head of collect-tickpick.yml, whose step 1 is this self-test. That is not a
# hypothetical: it shipped in the first version of this guard and review caught it.
_OFFLINE_DRY_RUN = {"summarize_market.py", "comps.py"}


def _help_has_no_side_effect(script: str) -> str | None:
    """Assert `--help` neither writes a tracked store nor is silently ignored.

    THE REGRESSION THIS PINS (ops#171). Neither script parsed arguments, so unknown flags
    were ignored and `--help` - the first thing anyone types to find out what a script
    does - ran the whole job. summarize_market.py rewrote data/market/summary.json;
    fetch_schedule.py was worse and better hidden, performing a live NHL fetch and writing
    BYTE-IDENTICAL content, so `git status` stayed clean while a network call had happened.

    Checked by mtime, not content, for exactly that reason: a byte-identical rewrite is
    still a write. Note mtime here is ~4ms-granular, not nanosecond, despite the field
    name - the margin over a ~40ms subprocess spawn is about 10x, and the failure
    direction is a silent pass rather than a flaky red.

    OFFLINE BY CONSTRUCTION. argparse handles --help and --nope before main() runs, so
    neither reaches the network for either script. Only --dry-run does, which is why
    fetch_schedule.py is excluded from that half - see _OFFLINE_DRY_RUN.
    """
    # GAP APPLYING TO EVERY SCRIPT THIS GUARD COVERS, named rather than glossed: every
    # assertion below is that the target did NOT change, so a script that stops writing
    # altogether satisfies all of them. `if write:` -> `if False:` survives for all three.
    # A missing test, not an equivalence. Filed as ops#177.
    #
    # Deliberately at function scope: the first version of this note sat inside the
    # `not in _OFFLINE_DRY_RUN` branch, which only fetch_schedule.py enters - so it named
    # three scripts from a path two of them never execute, invisible to exactly the reader
    # tracing the bug.
    root = pathlib.Path(__file__).resolve().parent.parent
    target = root / _WRITES[script]
    path = root / "scripts" / script

    # A MISSING STORE IS A FAILURE, NOT A PASS. This read `if f.exists()`, so an absent
    # store was silently unwatched and *creating* it counted as clean - failing open, the
    # same shape as the fresh-clone `.private-patterns` hole rule 1 warns about.
    if not target.exists():
        return f"{script}: watched store {target.name} is missing, so this proves nothing"

    before = target.stat().st_mtime_ns
    r = subprocess.run([sys.executable, str(path), "--help"],
                       capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        return f"{script} --help exited {r.returncode}, want 0"
    if target.stat().st_mtime_ns != before:
        return f"{script} --help wrote {target.name}"

    bogus = subprocess.run([sys.executable, str(path), "--nope"],
                           capture_output=True, text=True, timeout=60)
    if bogus.returncode == 0:
        return f"{script} silently ignored an unknown flag instead of erroring"

    if script not in _OFFLINE_DRY_RUN:
        # POSITIVE CONTROL for a script we cannot dry-run here. Without one, mis-pointing
        # _WRITES makes the "--help wrote the store" assertion above watch the wrong file
        # and pass vacuously (mutant M4b) - the same defect mutation already found for the
        # other script. Its --help text names the destination, printed from the same DEST
        # constant the write uses, so no network is needed to check it.
        # Subject to M12's class too, in the same way and for the same reason: this reads
        # a path the script PRINTS, so a literal typed next to DEST in either the help
        # string or the write would decouple them and pass. Neither half of this guard is
        # drift-proof; both are cheaper than a real run that mutates the tree.
        #
        # ANCHOR ON THE MARKER, AND COMPARE FOR EQUALITY. A bare substring test over all
        # of --help was the first version and it was too weak two ways: argparse prints
        # the destination in more than one place, only one of which derives from DEST, so
        # moving DEST still matched the other; and `data/x.json` is a substring of
        # `data/x.json.bak`, so even an exact-token containment test passes. This is the
        # "a source-mention check is NOT enough" lesson one level up from where it is
        # already written down, three lines above.
        joined = " ".join(r.stdout.split())   # argparse wraps help text at terminal width
        marker = "do not write "
        claimed = joined.split(marker, 1)[1].split()[0] if marker in joined else None
        if claimed != _WRITES[script]:
            return (f"{script} --help says it would write {claimed!r}, but _WRITES says "
                    f"{_WRITES[script]!r} - the write check above is watching the wrong store")
        # KNOWN GAP, named rather than glossed: its WRITE GATE is still unpinned -
        # reverting `if write:` there survives this suite, because only --dry-run exercises
        # it and --dry-run fetches. A missing test, not an equivalence. Filed as ops#175.
        return None

    # --dry-run, separately, because --help does NOT exercise the write gate: argparse
    # exits before main() runs, so reverting `if write:` leaves --help untouched. Found by
    # mutation - two mutants survived a --help-only guard, which was a missing test.
    before2 = target.stat().st_mtime_ns
    # RUN IT UNDER A POISONED PROXY. _OFFLINE_DRY_RUN is a hand-maintained set with
    # nothing tying it to whether the script actually fetches - the same shape as the
    # `watched` literal that mutation already killed once. Without this, adding a network
    # call to a script in that set leaves the suite green on a connected machine and red
    # only where there was no network anyway, which is precisely the regression this guard
    # exists to prevent and which shipped in its first version.
    #
    # Honest limit: this catches urllib and requests, which honour proxy env vars, not a
    # raw socket. Every fetch in this repo is urllib, so it covers the realistic case.
    offline_env = {**os.environ, "https_proxy": "http://127.0.0.1:1",
                   "http_proxy": "http://127.0.0.1:1", "no_proxy": ""}
    dry = subprocess.run([sys.executable, str(path), "--dry-run"], env=offline_env,
                         capture_output=True, text=True, timeout=60)
    if dry.returncode != 0:
        return (f"{script} --dry-run exited {dry.returncode}; cannot judge the write gate: "
                f"{dry.stderr.strip()[-200:]}")
    if target.stat().st_mtime_ns != before2:
        return f"{script} --dry-run wrote {target.name}"

    # POSITIVE CONTROL. Everything above asserts the target did NOT change, so a _WRITES
    # entry naming a file the script never touches passes vacuously - the guard would be
    # watching the wrong store and could not tell. Mutation found exactly that.
    #
    # The path is taken from --dry-run's own output, which is printed from DEST - the same
    # object the write uses. That is a CONVENTION, not a constraint: replacing the f-string
    # with a literal decouples them and this control passes while the write goes elsewhere
    # (mutant M12). A missing test, not an equivalence.
    #
    # An earlier version ran the script FOR REAL and restored the bytes afterwards, and it
    # WAS immune to M12. It was dropped because it made an offline suite mutate a tracked
    # store: review measured it silently reverting a concurrent peer write 20/20, and ~55ms
    # in which a SIGKILL stranded a dirty tree. Reading the path costs nothing and proves
    # nearly the same thing, which is the better trade - but "nearly" is the honest word.
    claimed = [ln.split("would write", 1)[1].strip().split()[0]
               for ln in dry.stdout.splitlines() if "would write" in ln]
    if not claimed:
        return f"{script} --dry-run did not say what it would write; cannot verify the target"
    if claimed[0] != _WRITES[script]:
        return (f"{script} --dry-run would write {claimed[0]}, but _WRITES says "
                f"{_WRITES[script]} - the guard is watching the wrong store")
    return None


def self_test() -> int:
    fails = []

    def check(label, got, want):
        if got != want:
            fails.append(f"{label}: got {got!r}, want {want!r}")

    games = [{"gameId": 1, "date": "2026-10-01"}, {"gameId": 2, "date": "2026-10-03"}]

    def row(day, gid, low, high=500, ok=True):
        return {"observedDate": day, "gameId": gid, "low": low, "high": high, "ok": ok}

    # Latest observation wins, and the first is retained for the delta.
    s = summarize({"tickpick": [row("2026-09-05", 1, 80), row("2026-09-06", 1, 95)]}, games)
    g = s["games"][0]
    check("latest low", g["low"], 95)
    check("observation count", g["observations"], 2)
    check("first low retained", g["lowFirst"], 80)
    check("delta computed", g["lowDelta"], 15)
    check("two days -> measured", s["confidence"], "measured")

    # A single observation must NOT emit a delta. Rendering 0 would read as "flat",
    # which one point cannot support - this is the assertion that keeps the UI honest.
    s = summarize({"tickpick": [row("2026-09-05", 1, 80)]}, games)
    check("single point has no delta", "lowDelta" in s["games"][0], False)
    check("single point confidence", s["confidence"], "measured_single_point")

    # Out-of-order input must still resolve to the true latest.
    s = summarize({"tickpick": [row("2026-09-07", 1, 70), row("2026-09-05", 1, 80)]}, games)
    check("unsorted input picks latest by date", s["games"][0]["low"], 70)
    check("unsorted input picks earliest as first", s["games"][0]["lowFirst"], 80)

    # Failed and unpriced rows must not become observations.
    s = summarize({"tickpick": [row("2026-09-05", 1, None, ok=True), row("2026-09-06", 1, 60, ok=False)]}, games)
    check("null and failed rows excluded", s["games"], [])

    # Games with no data are omitted rather than emitted as zero.
    s = summarize({"tickpick": [row("2026-09-05", 1, 80)]}, games)
    check("only games with data appear", [g["gameId"] for g in s["games"]], [1])

    # Provenance flags the app relies on to caveat the numbers.
    check("flagged as not our channel", s["isOwnChannel"], False)
    check("price basis recorded", s["priceBasis"], "all_in_whole_arena")

    # --- multi-source behaviour ---
    two = summarize(
        {"tickpick": [row("2026-09-05", 1, 100), row("2026-09-05", 2, 50)],
         "gametime": [row("2026-09-05", 1, 106), row("2026-09-05", 2, 55)]},
        games,
    )
    check("primary drives the headline low", two["games"][0]["low"], 100)
    check("secondary kept alongside, not blended",
          two["games"][0]["otherSources"]["gametime"]["low"], 106)
    check("sources listed", two["sources"], ["gametime", "tickpick"])
    gt = two["crossSource"]["perSource"]["gametime"]
    check("cross-source compares both games", gt["games"], 2)
    # 106/100 = 1.06 and 55/50 = 1.10 -> median 1.08.
    check("median ratio", gt["medianRatioToPrimary"], 1.08)
    check("min ratio", gt["minRatio"], 1.06)
    check("max ratio", gt["maxRatio"], 1.1)
    check("both games priced by every source", two["crossSource"]["commonGames"], 2)
    check("no pooled figure is emitted at all",
          "medianRatioToPrimary" in two["crossSource"], False)

    # A game the secondary has not priced must not invent a ratio for it.
    partial = summarize(
        {"tickpick": [row("2026-09-05", 1, 100), row("2026-09-05", 2, 50)],
         "gametime": [row("2026-09-05", 1, 106)]},
        games,
    )
    check("ratio only where both sources have data",
          partial["crossSource"]["perSource"]["gametime"]["games"], 1)
    check("unmatched game has no otherSources", "otherSources" in partial["games"][1], False)

    # THE BUG THIS SHAPE EXISTS TO PREVENT (ops#43/ops#44).
    # A third source that covers only the CHEAP half of the schedule must not move any
    # other source's figure. Under the old pooled median it did: its cheap ratios were
    # thrown into one list with everyone else's, so adding a rolling source silently
    # dragged the headline and the result was read as "secondaries ask less".
    #
    # Measured on the real data, the pooled median said +3.3% while per source it was
    # Gametime +6.0%, TicketNetwork level, ScoreBig -5.4% - directionally wrong for two of
    # the three. So this asserts a property, not an arithmetic result: gametime's figure
    # must be IDENTICAL with and without the cheap third source present.
    base = summarize(
        {"tickpick": [row("2026-09-05", 1, 100), row("2026-09-05", 2, 50)],
         "gametime": [row("2026-09-05", 1, 106), row("2026-09-05", 2, 55)]},
        games,
    )["crossSource"]["perSource"]["gametime"]
    withcheap = summarize(
        {"tickpick": [row("2026-09-05", 1, 100), row("2026-09-05", 2, 50)],
         "gametime": [row("2026-09-05", 1, 106), row("2026-09-05", 2, 55)],
         # Covers only game 2 - the cheap one - and asks well under the primary.
         "scorebig": [row("2026-09-05", 2, 40)]},
        games,
    )["crossSource"]
    check("a cheap partial source does not move another source's median",
          withcheap["perSource"]["gametime"], base)
    check("and it reports its own figure separately",
          withcheap["perSource"]["scorebig"]["medianRatioToPrimary"], 0.8)
    check("with its own n", withcheap["perSource"]["scorebig"]["games"], 1)
    # commonGames shrinks to the intersection, which is the honest like-for-like set.
    check("commonGames is the intersection", withcheap["commonGames"], 1)

    # An EMPTY source must not collapse commonGames. Found in review: intersecting over
    # every declared store meant one unpopulated collector reported "no overlap" while the
    # populated sources agreed on everything. It understates confidence, which is the one
    # direction this particular number must not fail in.
    withempty = summarize(
        {"tickpick": [row("2026-09-05", 1, 100), row("2026-09-05", 2, 50)],
         "gametime": [row("2026-09-05", 1, 106), row("2026-09-05", 2, 55)],
         "notyetcollected": []},
        games,
    )["crossSource"]
    check("an empty source does not collapse commonGames", withempty["commonGames"], 2)
    check("and it contributes no ratio of its own",
          "notyetcollected" in withempty["perSource"], False)
    check("the populated source is unaffected",
          withempty["perSource"]["gametime"]["games"], 2)

    # A secondary source alone must not become the headline.
    only = summarize({"gametime": [row("2026-09-05", 1, 106)]}, games)
    check("no primary data -> no games summarised", only["games"], [])
    check("no primary data -> no cross-source", only["crossSource"], None)

    # A source publishing a low with no high must not crash the summary. Gametime does
    # this on some events, and it took down the first two-source run.
    nohigh = summarize(
        {"tickpick": [{"observedDate": "2026-09-05", "gameId": 1, "low": 80, "ok": True}],
         "gametime": [{"observedDate": "2026-09-05", "gameId": 1, "low": 85, "ok": True}]},
        games,
    )
    check("missing high on primary is absent, not fatal", nohigh["games"][0]["high"], None)
    check("missing high on secondary is absent, not fatal",
          nohigh["games"][0]["otherSources"]["gametime"]["high"], None)
    check("low still summarised without a high", nohigh["games"][0]["low"], 80)

    check("empty input", summarize({"tickpick": []}, games)["games"], [])
    check("empty input has no dates", summarize({"tickpick": []}, games)["lastObservedDate"], None)

    for _script in ("summarize_market.py", "fetch_schedule.py", "comps.py"):
        _err = _help_has_no_side_effect(_script)
        if _err:
            fails.append(_err)

    for f in fails:
        print(f"  FAIL {f}", file=sys.stderr)
    print(f"self-test: {'FAILED' if fails else 'passed'} ({len(fails)} failure(s))")
    return 1 if fails else 0


def main(write: bool = True) -> int:
    rows_by_source = {name: load_rows(path) for name, path in STORES}
    games = json.loads(SCHEDULE.read_text())["games"]
    summary = summarize(rows_by_source, games)
    for name, rows in rows_by_source.items():
        print(f"  {name}: {len(rows)} rows")
    if write:
        DEST.parent.mkdir(parents=True, exist_ok=True)
        DEST.write_text(json.dumps(summary, indent=2) + "\n")
    print(f"days: {summary['observationDays']}  "
          f"games with data: {len(summary['games'])}/{len(games)}")
    if summary.get("crossSource"):
        c = summary["crossSource"]
        print(f"cross-source vs {PRIMARY} (per source, never pooled; "
              f"{c['commonGames']} games priced by all):")
        for name, v in c["perSource"].items():
            arrow = "above" if v["medianRatioToPrimary"] > 1 else (
                "below" if v["medianRatioToPrimary"] < 1 else "level with")
            print(f"    {name:15} {v['games']:3} games  median "
                  f"{v['medianRatioToPrimary']:.3f} "
                  f"({abs(v['medianRatioToPrimary'] - 1) * 100:.1f}% {arrow}) "
                  f"range {v['minRatio']:.3f}-{v['maxRatio']:.3f}")
    print(f"confidence: {summary['confidence']}")
    # The --dry-run wording is asserted by _help_has_no_side_effect; keep DEST in it.
    print(f"wrote {DEST.relative_to(ROOT)}" if write
          else f"--dry-run: would write {DEST.relative_to(ROOT)}")
    if not summary["games"]:
        print("\nNo priced games - the app will show no market context.", file=sys.stderr)
    return 0


def _cli() -> int:
    """See fetch_schedule.py's _cli docstring - same defect, found by the same audit.

    `--help` here wrote data/market/summary.json, a tracked file whose `generatedAt`
    changes every run, so the probe dirtied the working tree every time.
    """
    ap = argparse.ArgumentParser(
        description="Derive data/market/summary.json from the four market stores.")
    ap.add_argument("--dry-run", action="store_true",
                    help="compute and report, but do not write data/market/summary.json")
    # KEEP THIS FLAG. main() used to sniff sys.argv for it; adding argparse without
    # declaring it made the parser reject the runner's own invocation, and `npm test`
    # went red for this script. Caught because the suite was read in full rather than
    # tailed - a two-line tail showed the coverage summary and hid the FAIL above it.
    ap.add_argument("--self-test", action="store_true",
                    help="run the self-test against captured fixtures; no network, no write")
    args = ap.parse_args()
    if args.self_test:
        return self_test()
    return main(write=not args.dry_run)


if __name__ == "__main__":
    sys.exit(_cli())
