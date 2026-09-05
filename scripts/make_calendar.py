#!/usr/bin/env python3
"""Generate a subscribable calendar of exchange deadlines.

ops#10 argues this is the highest value-per-line-of-code feature in the project, and it
is right: the exchange window closes 48 hours before puck drop, an unsold ticket goes
from being worth the tier credit to being worth exactly $0 at that instant, and a
dashboard you forget to open cannot fire an alert.

WHY A CALENDAR FEED FIRST, over web push. ops#10 lists four options and calls this one
"worth doing first regardless". Agreed, for a reason worth writing down: it has no
runtime. A subscribed .ics is refreshed by the phone's own calendar app, so there is
nothing to keep alive - no service worker, no push endpoint, no server, no credential.
It also degrades gracefully: if this script breaks, the last-fetched copy keeps firing
the alarms it already has.

WHAT IS DELIBERATELY NOT IN HERE, and this is the important part. ops#10 asks the alert
to carry "current bid, break-even, credit, and which exit currently wins". Credit and
break-even are PER-SEAT and therefore personal (CLAUDE.md rule 1), and this file is
served from GitHub Pages, which is world-readable regardless of repo visibility. So the
feed carries only public facts - opponent, deadline, and the whole-arena asking range -
plus a link. The private numbers stay in the browser, where the app can put them into a
downloaded copy if wanted.

That is a real tradeoff, not an oversight: the reminder's job is to make sure the moment
is not missed, and the decision itself is one tap away in the app.
"""

import json
import pathlib
import re
import sys
from datetime import datetime, timedelta, timezone

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCHEDULE = ROOT / "data" / "schedule.json"
SUMMARY = ROOT / "data" / "market" / "summary.json"
DEST = ROOT / "public" / "deadlines.ics"

EXCHANGE_DEADLINE_HOURS = 48
APP_URL = "https://wes-chen.github.io/ticket-desk/"

# Alarms before the deadline. Three, because the failure mode is sleeping through it:
# one with a week to act, one the day before, one at the wire.
ALARMS = [("-P7D", "7 days"), ("-P1D", "24 hours"), ("-PT1H", "1 hour")]


def ics_dt(d: datetime) -> str:
    return d.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def esc(s: str) -> str:
    """Escape per RFC 5545: backslash, semicolon, comma, newline."""
    return (s.replace("\\", "\\\\").replace(";", r"\;")
             .replace(",", r"\,").replace("\n", r"\n"))


def fold(line: str) -> str:
    """RFC 5545 caps content lines at 75 octets; continuations start with a space.

    Not cosmetic - strict parsers reject over-long lines, and the DESCRIPTION here is
    comfortably past 75 characters.
    """
    raw = line.encode("utf-8")
    if len(raw) <= 75:
        return line
    out, first = [], True
    while raw:
        # A continuation line spends one octet on its leading space, so it can carry 74.
        take = min(75 if first else 74, len(raw))
        # Never split a multi-byte character across a fold. Continuation bytes match
        # 0b10xxxxxx; back off until the next byte starts a fresh character.
        while take > 1 and take < len(raw) and (raw[take] & 0xC0) == 0x80:
            take -= 1
        chunk, raw = raw[:take], raw[take:]
        out.append((chunk if first else b" " + chunk).decode("utf-8"))
        first = False
    return "\r\n".join(out)


def build(games: list[dict], market: dict) -> str:
    lows = {g["gameId"]: g for g in market.get("games", [])}
    now = datetime.now(timezone.utc)

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//ticket-desk//exchange deadlines//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-WR-CALNAME:Sharks exchange deadlines",
        "X-WR-CALDESC:Return-For-Credit closes 48h before puck drop. After that an "
        "unsold ticket is worth $0.",
        # Both spellings: Apple honours the X- form, others the standard one.
        "REFRESH-INTERVAL;VALUE=DURATION:PT12H",
        "X-PUBLISHED-TTL:PT12H",
    ]

    for g in games:
        puck = datetime.fromisoformat(g["startTimeUTC"].replace("Z", "+00:00"))
        deadline = puck - timedelta(hours=EXCHANGE_DEADLINE_HOURS)
        opp = g["opponent"]["abbrev"]
        tier = g["tier"] or "?"

        desc = [
            f"Return For Credit closes now for {g['opponent']['name']} "
            f"({tier}) on {g['date']}.",
            "After this moment an unsold ticket is worth $0 - the credit is gone, "
            "not reduced.",
        ]
        m = lows.get(g["gameId"])
        if m:
            desc.append(
                f"Whole-arena asking range on TickPick as of {m['observedDate']}: "
                f"${m['low']}-${m['high']} all-in. This is a comp market, not our "
                f"channel, and not a per-seat price."
            )
        desc.append(f"Your credit and break-even are in the app: {APP_URL}")

        lines += [
            "BEGIN:VEVENT",
            f"UID:exchange-{g['gameId']}@ticket-desk",
            f"DTSTAMP:{ics_dt(now)}",
            f"DTSTART:{ics_dt(deadline)}",
            f"DTEND:{ics_dt(deadline + timedelta(minutes=30))}",
            f"SUMMARY:Exchange deadline - {opp} ({tier})",
            f"DESCRIPTION:{esc(' '.join(desc))}",
            f"URL:{APP_URL}",
            "TRANSP:TRANSPARENT",
        ]
        for trigger, label in ALARMS:
            lines += [
                "BEGIN:VALARM",
                "ACTION:DISPLAY",
                f"TRIGGER:{trigger}",
                f"DESCRIPTION:{esc(f'Exchange deadline for {opp} in {label}')}",
                "END:VALARM",
            ]
        lines.append("END:VEVENT")

    lines.append("END:VCALENDAR")
    return "\r\n".join(fold(l) for l in lines) + "\r\n"


def validate(ics: str) -> list[str]:
    """Structural checks against RFC 5545. Cheap, and the alternative is trusting it.

    A malformed feed does not error - the phone silently declines to subscribe, which
    looks exactly like "no deadlines coming up".
    """
    problems = []
    if not ics.endswith("\r\n"):
        problems.append("file must end with CRLF")
    lines = ics.split("\r\n")[:-1]
    if any("\n" in l for l in lines):
        problems.append("bare LF found; all line breaks must be CRLF")

    for i, l in enumerate(lines, 1):
        n = len(l.encode("utf-8"))
        if n > 75:
            problems.append(f"line {i} is {n} octets, over the 75-octet limit: {l[:40]}...")

    # BEGIN/END nesting must balance.
    stack = []
    for l in lines:
        if l.startswith("BEGIN:"):
            stack.append(l[6:])
        elif l.startswith("END:"):
            if not stack:
                problems.append(f"unmatched {l}")
            elif stack[-1] != l[4:]:
                problems.append(f"{l} closes {stack[-1]}")
            else:
                stack.pop()
    if stack:
        problems.append(f"unclosed blocks: {stack}")

    for required in ("VERSION:2.0", "PRODID:", "BEGIN:VCALENDAR", "END:VCALENDAR"):
        if not any(l.startswith(required) for l in lines):
            problems.append(f"missing required property {required}")

    # Every VEVENT needs a UID and DTSTART, and unique UIDs.
    uids = [l[4:] for l in lines if l.startswith("UID:")]
    n_events = sum(1 for l in lines if l == "BEGIN:VEVENT")
    if len(uids) != n_events:
        problems.append(f"{n_events} VEVENTs but {len(uids)} UIDs")
    if len(set(uids)) != len(uids):
        problems.append("duplicate UIDs - calendars will collapse events")
    if sum(1 for l in lines if l.startswith("DTSTART:")) != n_events:
        problems.append("every VEVENT needs a DTSTART")
    return problems


def self_test() -> int:
    fails = []

    def check(label, got, want):
        if got != want:
            fails.append(f"{label}: got {got!r}, want {want!r}")

    # Folding.
    check("short line untouched", fold("SUMMARY:hi"), "SUMMARY:hi")
    long = "DESCRIPTION:" + "x" * 200
    folded = fold(long)
    for i, l in enumerate(folded.split("\r\n")):
        if len(l.encode()) > 75:
            fails.append(f"folded line {i} is {len(l.encode())} octets")
    check("unfolds back to the original",
          folded.split("\r\n")[0] + "".join(p[1:] for p in folded.split("\r\n")[1:]), long)
    # Multi-byte must not be split. A 3-byte char repeated lands a boundary mid-character
    # unless fold() backs off.
    mb = "DESCRIPTION:" + "\u2014" * 60
    try:
        fold(mb)
    except UnicodeDecodeError:
        fails.append("fold() split a multi-byte character")

    # Escaping. Built with chr() so the expected values cannot be mangled by
    # whatever quoting layer this file was written through.
    bs = chr(92)
    check("escapes comma", esc("a,b"), "a" + bs + ",b")
    check("escapes semicolon", esc("a;b"), "a" + bs + ";b")
    check("escapes newline", esc("a\nb"), "a" + bs + "nb")
    check("escapes backslash", esc("a" + bs + "b"), "a" + bs + bs + "b")

    # A real calendar built from the real schedule must validate.
    games = json.loads(SCHEDULE.read_text())["games"]
    market = json.loads(SUMMARY.read_text()) if SUMMARY.exists() else {}
    ics = build(games, market)
    for p in validate(ics):
        fails.append(f"validate: {p}")
    check("one VEVENT per home game", ics.count("BEGIN:VEVENT"), len(games))
    check("alarms per event", ics.count("BEGIN:VALARM"), len(games) * len(ALARMS))

    # The privacy constraint that shaped this file, asserted on VALUES rather than
    # vocabulary.
    #
    # Two earlier attempts at this check were both wrong, which is instructive. Banning
    # the word "break-even" failed on this file's own prose, where it appears with no
    # number attached. Scanning the source for "credits"/"profile" matched the forbidden
    # word list itself. A check keyed on vocabulary produces false alarms, and false
    # alarms are how people learn to ignore a check.
    #
    # So: every dollar amount that reaches this world-readable file must be one that is
    # already public in data/market/summary.json. A per-seat credit or break-even
    # figure could not satisfy that, because it is not in there.
    public_amounts = set()
    for mg in market.get("games", []):
        for k in ("low", "high"):
            if mg.get(k) is not None:
                public_amounts.add(str(mg[k]))
    # Unfold first. Folding breaks lines at 75 octets, which happily splits "$1826"
    # into "$18" + a continuation starting "26" - and a regex over the folded text then
    # reports fragments like "18" as unexplained amounts. Caught by this check firing
    # on nonsense.
    unfolded = ics.replace("\r\n ", "")
    emitted = set(re.findall(r"\$(\d[\d,]*)", unfolded))
    leaked = {a for a in emitted if a.replace(",", "") not in public_amounts}
    # "$0" is the literal point of the reminder: after the deadline the ticket is
    # worth exactly nothing. It is a constant, not an observation.
    leaked.discard("0")
    if leaked:
        fails.append(f"public calendar carries dollar amounts not present in the public "
                     f"market summary: {sorted(leaked)}")

    # Deadline arithmetic: exactly 48h before puck drop.
    g = games[0]
    puck = datetime.fromisoformat(g["startTimeUTC"].replace("Z", "+00:00"))
    want = ics_dt(puck - timedelta(hours=48))
    if f"DTSTART:{want}" not in ics:
        fails.append(f"first game's DTSTART should be {want}")

    for f in fails:
        print(f"  FAIL {f}", file=sys.stderr)
    print(f"self-test: {'FAILED' if fails else 'passed'} ({len(fails)} failure(s))")
    return 1 if fails else 0


def main() -> int:
    if "--self-test" in sys.argv:
        return self_test()
    games = json.loads(SCHEDULE.read_text())["games"]
    market = json.loads(SUMMARY.read_text()) if SUMMARY.exists() else {}
    ics = build(games, market)
    problems = validate(ics)
    if problems:
        print(f"{len(problems)} INVALID ICS:", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        print("Not written - a malformed feed fails silently on the phone, which looks "
              "exactly like having no deadlines.", file=sys.stderr)
        return 1
    DEST.parent.mkdir(parents=True, exist_ok=True)
    DEST.write_text(ics, newline="")
    n = ics.count("BEGIN:VEVENT")
    print(f"wrote {DEST.relative_to(ROOT)}  {len(ics):,}B  {n} deadlines, "
          f"{len(ALARMS)} alarms each")
    print(f"subscribe: {APP_URL}deadlines.ics")
    soon = [g for g in games
            if datetime.fromisoformat(g["startTimeUTC"].replace("Z", "+00:00"))
            - timedelta(hours=EXCHANGE_DEADLINE_HOURS) > datetime.now(timezone.utc)][:3]
    print("next deadlines:")
    for g in soon:
        dl = datetime.fromisoformat(g["startTimeUTC"].replace("Z", "+00:00")) - timedelta(hours=48)
        days = (dl - datetime.now(timezone.utc)).days
        print(f"  {dl.strftime('%Y-%m-%d %H:%MZ')}  {g['opponent']['abbrev']:4s} "
              f"({g['tier']})  in {days}d")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
