#!/usr/bin/env python3
"""Grade a pasted block of regular-season tier credits, and emit the store line for it.

WHY THIS EXISTS. The five regular-season tier credits (A+, A, B, C, D) live only in
browser localStorage, so every regular-season break-even is blocked on a manual paste
(ops#139). CLAUDE.md is blunt about what an input contract without a validator is - a
wish - so this is the grader that makes ops#139 a contract. Filed as ops#140.

WHAT IT REFUSES, AND WHY EACH ONE. A missing or duplicated tier silently prices a whole
tier off the wrong number; a non-numeric credit reaches break-even arithmetic as a
string, which is exactly how ScoreBig's string prices nearly reached summarize_market's
delta comparison; a missing CONFIDENCE erases the measured/assumed distinction that
rule 4 calls load-bearing.

WHAT IT ONLY WARNS ON. Non-monotone credits. The tier table implies A+ >= A >= B >= C >=
D and check_tier_market.py measured Spearman -0.914 in support of it, but the APP is the
source of truth for the credits and the table is a hand transcription. A violation is a
"confirm this", not a "you are wrong" - blocking on it would let a correct paste be
rejected by a wrong table.

THE CROSS-CHECK, AND ITS HONEST LIMIT. Season face is $4,048/seat over 44 games
(ops#19: Lower 4 at $92 x 44; reconciled against the invoice in ops#13). This paste
covers the 42 REGULAR-SEASON games, so the sum of count x credit should land below that
face by whatever the 2 preseason games are worth. The preseason credit is a per-tier
exchange credit and therefore PRIVATE (CLAUDE.md rule 1), so it is not in this file: it
is read from the private snapshots store when --snapshots points at one. Without it the
check falls back to a BRACKET - the sum must lie between 42/44 of face and all of face -
which is weaker and says so rather than inventing the missing number.

PRIVACY. This script is in the PUBLIC repo; the values it grades are PRIVATE. So no real
credit appears here, in the fixtures, or in the self-test - fixtures are absurd on
purpose, because ops#29 is what plausible fixtures cost. It also refuses to write
anywhere inside this repo: the emitted line belongs in ops:data/profile/snapshots.jsonl.

Usage:
    python3 scripts/validate_tier_credits.py --file paste.txt
    python3 scripts/validate_tier_credits.py < paste.txt
    python3 scripts/validate_tier_credits.py --file paste.txt \
        --snapshots ../ops/data/profile/snapshots.jsonl \
        --captured-by "session b0e3f7" --append ../ops/data/profile/snapshots.jsonl
    python3 scripts/validate_tier_credits.py --self-test
"""

import argparse
import datetime as dt
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
TIERS = ROOT / "config" / "tiers.json"

# Best to worst. PRESEASON is deliberately not on the ladder - it is a different product
# and is not part of this paste, the same call check_tier_market.py makes.
LADDER = ["A+", "A", "B", "C", "D"]
CONFIDENCES = {"measured", "measured_single_point", "assumed"}

# PUBLIC constants. Both are already in the public repo - the face in CLAUDE.md, the
# game counts in config/tiers.json - so using them here introduces nothing new.
FACE_PER_SEAT = 4048.0
FULL_SEASON_GAMES = 44
TOLERANCE = 0.10

LINE_RE = re.compile(
    r"^TIER=(?P<tier>\S+)\s+CREDIT=(?P<credit>\S+)\s+CONFIDENCE=(?P<conf>\S+)\s*$",
    re.I,
)


class Rejected(Exception):
    """A paste that cannot be graded. The message names what is wrong with it."""


def tier_counts(path: pathlib.Path = TIERS) -> dict[str, int]:
    """Games per tier, COUNTED from the games array rather than read from _counts.

    _counts is a hand-written summary of the same file and could drift from the rows it
    summarises. Counting the rows and then checking _counts agrees catches that drift
    instead of trusting it - the same posture fetch_schedule.py takes towards the table.
    """
    doc = json.loads(path.read_text())
    counted: dict[str, int] = {}
    for g in doc["games"]:
        counted[g["tier"]] = counted.get(g["tier"], 0) + 1
    declared = {k: v for k, v in doc.get("_counts", {}).items() if not k.startswith("_")}
    if declared and declared != counted:
        raise Rejected(
            f"config/tiers.json disagrees with itself: _counts {declared} but the games "
            f"array holds {counted}. Fix the table before grading a paste against it."
        )
    return counted


def parse(text: str) -> dict[str, dict]:
    """Parse the paste into {tier: {perSeat, confidence}}, or raise Rejected."""
    rows: dict[str, dict] = {}
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        # Blank lines, # comments and ``` fences are stripped rather than refused: a
        # paste copied out of a GitHub comment carries fences, and rejecting on them
        # would fail a correct paste for a formatting artefact.
        if not line or line.startswith("#") or line.startswith("```"):
            continue
        m = LINE_RE.match(line)
        if not m:
            raise Rejected(
                f"line {lineno} is not in the required format: {line!r}\n"
                f"  expected: TIER=<A+|A|B|C|D> CREDIT=<dollars per seat> "
                f"CONFIDENCE=<measured|assumed>"
            )
        tier = m.group("tier").upper()
        if tier not in LADDER:
            raise Rejected(
                f"line {lineno}: unknown tier {tier!r}. Expected one of {LADDER}."
            )
        if tier in rows:
            raise Rejected(f"line {lineno}: tier {tier!r} appears more than once.")
        raw_credit = m.group("credit").lstrip("$").replace(",", "")
        try:
            credit = float(raw_credit)
        except ValueError:
            raise Rejected(
                f"line {lineno}: credit {m.group('credit')!r} is not a number."
            ) from None
        if credit <= 0:
            raise Rejected(
                f"line {lineno}: credit {credit} is not positive. A tier with no credit "
                f"would make its break-even zero, which would read as 'sell at any "
                f"price' rather than as missing data."
            )
        conf = m.group("conf").lower()
        if conf not in CONFIDENCES:
            raise Rejected(
                f"line {lineno}: CONFIDENCE={m.group('conf')!r} is not one of "
                f"{sorted(CONFIDENCES)}."
            )
        rows[tier] = {"perSeat": credit, "confidence": conf}

    missing = [t for t in LADDER if t not in rows]
    if missing:
        raise Rejected(
            f"missing tier(s): {missing}. All five regular-season tiers are required - a "
            f"partial paste would leave some games silently unpriceable."
        )
    return rows


def preseason_credit(snapshots: pathlib.Path | None) -> float | None:
    """Latest preseason per-seat credit from the PRIVATE snapshots store, or None.

    Deliberately read at runtime rather than hardcoded: CLAUDE.md rule 1 forbids an
    exchange credit amount per tier from appearing in this repo at all.
    """
    if snapshots is None or not snapshots.exists():
        return None
    found = None
    for raw in snapshots.read_text().splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            doc = json.loads(raw)
        except json.JSONDecodeError:
            continue
        # Append-only store: a later line supersedes an earlier one, so keep scanning.
        pre = (doc.get("tierCredits") or {}).get("PRESEASON") or {}
        if isinstance(pre.get("perSeat"), (int, float)):
            found = float(pre["perSeat"])
    return found


def cross_check(rows: dict[str, dict], counts: dict[str, int],
                pre_credit: float | None, face: float = FACE_PER_SEAT) -> tuple[bool, list[str]]:
    """Compare the implied regular-season total against season face. (ok, notes)."""
    notes = []
    unknown = [t for t in rows if t not in counts]
    if unknown:
        raise Rejected(f"tier(s) {unknown} have no games in config/tiers.json.")
    total = sum(counts[t] * rows[t]["perSeat"] for t in rows)
    reg_games = sum(counts[t] for t in rows)
    pre_games = FULL_SEASON_GAMES - reg_games
    notes.append(
        f"implied regular-season credit total: {total:,.2f}/seat "
        f"across {reg_games} games in {len(rows)} tiers"
    )

    if pre_credit is not None:
        expected = face - pre_games * pre_credit
        lo, hi = expected * (1 - TOLERANCE), expected * (1 + TOLERANCE)
        notes.append(
            f"expected ~{expected:,.2f} = season face {face:,.2f} less {pre_games} "
            f"preseason game(s) at the credit recorded in the private store"
        )
    else:
        # No preseason credit available, so bracket instead of inventing one: the sum
        # cannot exceed full face, and cannot fall below face less preseason valued at
        # the season average. A bracket is weaker, and saying so beats a false point.
        low_anchor = face * reg_games / FULL_SEASON_GAMES
        lo, hi = low_anchor * (1 - TOLERANCE), face * (1 + TOLERANCE)
        notes.append(
            f"no preseason credit available (pass --snapshots to narrow this), so the "
            f"check is a BRACKET: {low_anchor:,.2f} to {face:,.2f} +/- "
            f"{TOLERANCE:.0%}"
        )

    ok = lo <= total <= hi
    notes.append(
        f"{'OK' if ok else 'FAIL'}: {total:,.2f} "
        f"{'is within' if ok else 'is OUTSIDE'} [{lo:,.2f}, {hi:,.2f}]"
    )
    if not ok:
        notes.append(
            "A total this far from the cost basis means a transcription error "
            "somewhere - in the paste, or in the tier table it is multiplied by."
        )
    return ok, notes


def monotone_warnings(rows: dict[str, dict]) -> list[str]:
    out = []
    for better, worse in zip(LADDER, LADDER[1:]):
        if rows[better]["perSeat"] < rows[worse]["perSeat"]:
            out.append(
                f"tier {better} credit ({rows[better]['perSeat']:,.2f}) is BELOW tier "
                f"{worse} ({rows[worse]['perSeat']:,.2f}). The tier table says {better} "
                f"is the better game, so one of the two is wrong. Confirm before use."
            )
    return out


DEFAULT_PROVENANCE = (
    "Manual paste by Wesley into ops#139, graded by "
    "scripts/validate_tier_credits.py (ops#140). Values read off the app profile "
    "screen, which reads browser localStorage."
)


def store_line(rows: dict[str, dict], captured_by: str,
               captured_at: str | None = None,
               provenance: str = DEFAULT_PROVENANCE) -> dict:
    """The exact snapshots.jsonl line to append. A value in an issue is not a record.

    `provenance` is overridable because the paste is not the only way these values can
    arrive. They were in fact RECOVERED from git history rather than pasted, and a line
    claiming Wesley typed them would be a false record of where a load-bearing number
    came from - the precise class of confident-looking wrongness rule 4 exists to stop.
    """
    at = captured_at or dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    # The line carries the WEAKEST confidence of its five rows, not the strongest:
    # a snapshot is only as trustworthy as the shakiest number in it, and rule 4
    # exists to stop exactly that kind of quiet upgrade.
    weakest = max((r["confidence"] for r in rows.values()),
                  key=lambda c: ["measured", "measured_single_point", "assumed"].index(c))
    return {
        # Deliberately says nothing about WHERE the values came from. `provenance` is
        # the single field that answers that, and duplicating it here is how a line ends
        # up asserting two different origins once one of them is overridden.
        "_what": "The five regular-season tier credits, per seat. PRIVATE: this repo "
                 "only, never ticket-desk. See `provenance` for where they came from.",
        "_why": "They were in no structured store, so no session could compute "
                "a break-even for any of the 42 regular-season games "
                "(break-even = credit / (1 - feeRate)). Recorded in a STORE rather than "
                "in the issue, because nothing polls a closed issue.",
        "_supersedes": "The tierCredits._missing note in the 2026-09-07T05:00Z line.",
        "capturedAt": at,
        "capturedBy": captured_by,
        "provenance": provenance,
        "confidence": weakest,
        "tierCredits": {t: dict(rows[t]) for t in LADDER},
    }


def grade(text: str, counts: dict[str, int], pre_credit: float | None,
          face: float = FACE_PER_SEAT) -> tuple[dict, list[str], list[str], bool]:
    """(rows, warnings, notes, cross_check_ok). Raises Rejected on a bad paste."""
    rows = parse(text)
    warnings = monotone_warnings(rows)
    ok, notes = cross_check(rows, counts, pre_credit, face)
    return rows, warnings, notes, ok


def run(args) -> int:
    text = (pathlib.Path(args.file).read_text() if args.file else sys.stdin.read())
    counts = tier_counts()
    snapshots = pathlib.Path(args.snapshots).resolve() if args.snapshots else None
    try:
        rows, warnings, notes, ok = grade(text, counts, preseason_credit(snapshots))
    except Rejected as e:
        print(f"REJECTED: {e}", file=sys.stderr)
        return 1

    print(f"accepted {len(rows)} tiers: {', '.join(LADDER)}")
    for n in notes:
        print(f"  {n}")
    for w in warnings:
        print(f"  WARNING: {w}")
    if not ok:
        print("REJECTED: cross-check against the cost basis failed (above).",
              file=sys.stderr)
        return 1

    line = store_line(rows, args.captured_by, provenance=args.provenance)
    if args.append:
        dest = pathlib.Path(args.append).resolve()
        if dest == ROOT or ROOT in dest.parents:
            # Rule 1 is not a style preference: a credit amount inside this repo is a
            # published personal value, and git never forgets it.
            print(f"REFUSED: {dest} is inside the PUBLIC repo at {ROOT}. Tier credits "
                  f"belong in ops:data/profile/snapshots.jsonl.", file=sys.stderr)
            return 1
        with dest.open("a") as fh:
            fh.write(json.dumps(line, sort_keys=True) + "\n")
        print(f"appended to {dest}")
    else:
        print("\n--- append this line to ops:data/profile/snapshots.jsonl ---")
        print(json.dumps(line, sort_keys=True))
    return 0


# --------------------------------------------------------------------------- tests

# ABSURD ON PURPOSE. Not one of these is a plausible credit, so none can be mistaken for
# a real value and copied - which is precisely how ops#29 leaked one.
GOOD = """```
TIER=A+ CREDIT=55555555 CONFIDENCE=measured
TIER=A  CREDIT=44444444 CONFIDENCE=measured
TIER=B  CREDIT=33333333 CONFIDENCE=measured
TIER=C  CREDIT=22222222 CONFIDENCE=measured
TIER=D  CREDIT=11111111 CONFIDENCE=assumed
```"""

COUNTS = {"A+": 7, "A": 8, "B": 7, "C": 11, "D": 9}


def self_test() -> int:
    failures = []

    def check(name: str, cond: bool, detail: str = "") -> None:
        if not cond:
            failures.append(f"{name}: {detail}")

    def rejects(name: str, text: str, needle: str) -> None:
        try:
            parse(text)
        except Rejected as e:
            check(name, needle in str(e), f"message {str(e)!r} lacks {needle!r}")
        else:
            failures.append(f"{name}: accepted a paste it should have refused")

    # Real counts must agree with the fixture, or the fixture is testing nothing.
    check("tier_counts matches config", tier_counts() == COUNTS,
          f"got {tier_counts()}, fixture says {COUNTS}")

    rows = parse(GOOD)
    check("parses five tiers", sorted(rows) == sorted(LADDER), f"got {sorted(rows)}")
    check("strips fences", rows["A+"]["perSeat"] == 55555555.0, str(rows["A+"]))
    check("keeps confidence", rows["D"]["confidence"] == "assumed", str(rows["D"]))

    rejects("missing tier", "\n".join(GOOD.splitlines()[1:4]), "missing tier")
    rejects("duplicate tier", GOOD + "\nTIER=B CREDIT=1 CONFIDENCE=measured",
            "more than once")
    rejects("unknown tier", "TIER=Z CREDIT=11111111 CONFIDENCE=measured", "unknown tier")
    rejects("non-numeric credit",
            GOOD.replace("CREDIT=33333333", "CREDIT=thirty"), "is not a number")
    rejects("zero credit", GOOD.replace("CREDIT=33333333", "CREDIT=0"),
            "is not positive")
    rejects("negative credit", GOOD.replace("CREDIT=33333333", "CREDIT=-5"),
            "is not positive")
    rejects("bad confidence", GOOD.replace("CONFIDENCE=measured", "CONFIDENCE=pretty-sure", 1),
            "is not one of")
    rejects("missing confidence field",
            "TIER=A+ CREDIT=11111111\n", "not in the required format")

    # $ and thousands separators are what a real paste off a screen looks like.
    tolerant = parse(GOOD.replace("CREDIT=55555555", "CREDIT=$55,555,555"))
    check("tolerates $ and commas", tolerant["A+"]["perSeat"] == 55555555.0,
          str(tolerant["A+"]))

    # Monotone violation WARNS, and grading still succeeds.
    swapped = GOOD.replace("CREDIT=55555555", "CREDIT=1").replace("CREDIT=11111111", "CREDIT=99999999")
    warns = monotone_warnings(parse(swapped))
    check("non-monotone warns", len(warns) >= 1, "no warning raised")
    check("monotone is quiet", monotone_warnings(parse(GOOD)) == [], "spurious warning")

    # Cross-check: passes when the total matches the anchor, fails when it does not.
    good_rows = parse(GOOD)
    total = sum(COUNTS[t] * good_rows[t]["perSeat"] for t in good_rows)
    # face chosen so the absurd total lands exactly on it: 2 preseason games at 0 credit.
    ok, _ = cross_check(good_rows, COUNTS, 0.0, face=total)
    check("cross-check accepts a matching total", ok, "rejected an exact match")
    ok, notes = cross_check(good_rows, COUNTS, 0.0, face=total * 2)
    check("cross-check rejects a doubled anchor", not ok, "accepted a 2x mismatch")
    check("failure is explained", any("transcription error" in n for n in notes),
          "no explanation in notes")
    ok, notes = cross_check(good_rows, COUNTS, None, face=total)
    check("bracket mode says it is a bracket", any("BRACKET" in n for n in notes),
          "bracket not announced")

    # preseason_credit reads the private store shape without a real value in the file.
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        p = pathlib.Path(d) / "snapshots.jsonl"
        p.write_text(
            json.dumps({"tierCredits": {"PRESEASON": {"perSeat": 11111111.0}}}) + "\n"
            + json.dumps({"costBasis": {"invoicePerSeat": 1}}) + "\n"
            + json.dumps({"tierCredits": {"PRESEASON": {"perSeat": 22222222.0}}}) + "\n")
        check("preseason_credit takes the LAST line", preseason_credit(p) == 22222222.0,
              str(preseason_credit(p)))
    check("preseason_credit tolerates absence", preseason_credit(None) is None)

    line = store_line(good_rows, "self-test", captured_at="2026-01-01T00:00:00Z")
    check("store line carries provenance", "ops#139" in line["provenance"])
    custom = store_line(good_rows, "self-test", captured_at="2026-01-01T00:00:00Z",
                        provenance="recovered from commit deadbeef")
    check("provenance is overridable",
          custom["provenance"] == "recovered from commit deadbeef",
          custom["provenance"])
    check("store line worst-cases confidence", line["confidence"] == "assumed",
          line["confidence"])
    check("store line is JSON-serialisable", json.dumps(line).startswith("{"))

    if failures:
        for f in failures:
            print(f"FAIL {f}")
        return 1
    print("validate_tier_credits self-test: OK")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--file", help="paste file; omit to read stdin")
    ap.add_argument("--snapshots", help="ops:data/profile/snapshots.jsonl, for the "
                                        "preseason credit the cross-check narrows with")
    ap.add_argument("--append", help="append the emitted line to this path; refused if "
                                     "it is inside this public repo")
    ap.add_argument("--captured-by", default="manual", help="who ran the paste")
    ap.add_argument("--provenance", default=DEFAULT_PROVENANCE,
                    help="where the values actually came from; override when they were "
                         "not typed by Wesley (e.g. recovered from git history)")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()
    return self_test() if args.self_test else run(args)


if __name__ == "__main__":
    sys.exit(main())
