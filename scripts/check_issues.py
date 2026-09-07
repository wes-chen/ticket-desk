#!/usr/bin/env python3
"""Audit issue hygiene in the ops tracker.

Two failures this catches, both of which had actually happened by the time it was
written:

  1. An issue whose work is DONE and whose resolution is already written in a comment,
     but which is still OPEN. Fourteen were sitting like that. The backlog then lies
     about what is left, and the next session re-reads finished work to find out.
  2. An issue CLOSED with no record of why. Six months later nobody can tell whether it
     was delivered, abandoned, or duplicated - and this project's whole discipline is
     that the reasoning matters more than the outcome.

WHY AN EXPLICIT MARKER RATHER THAN KEYWORD SNIFFING. Guessing "does this comment sound
like a resolution" is the kind of heuristic this codebase has been burned by repeatedly -
a probe that could not tell "blocked" from "needs JavaScript", a privacy check keyed on
vocabulary that failed on its own prose. So the convention is a literal marker:

    **Closing - <why>**

The check is then exact. Looser historical forms ("Closing." on its own line, "Suggest
closing") are accepted too, because they predate the convention and reflagging them
forever would be noise - but new closing comments should use the marker.

THE TYPE CONTRACTS. Each issue carries exactly one `type:` label, and the type decides
who acts and what closing it requires - a `type:research` issue closed with no
measurement, or a `type:incident` closed with no guard, is work nobody can audit later.
An OPEN issue with no type at all is unroutable: an unattended session has no other way
to tell whether it is Wesley's to answer or an agent's to build. See CONTRACTS below and
`harness/types/CONTRACTS.md` in the ops repo for the reasoning.

Needs network and `gh`, so it is NOT part of `npm test`, which is deliberately offline.
The classifier is pure and self-tested; only the fetch touches the network.

Usage:
    python3 scripts/check_issues.py                # audit
    python3 scripts/check_issues.py --strict       # non-zero exit if anything is flagged
    python3 scripts/check_issues.py --self-test    # classifier only, no network
"""

import argparse
import datetime as dt
import json
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent

# The tracker lives in a different repo; .claude/issues.json is the declaration.
def tracker() -> str:
    cfg = json.loads((ROOT / ".claude" / "issues.json").read_text())
    return cfg["tracker"]


# Canonical form first. The rest are historical variants kept so the check does not
# nag about comments written before the convention existed.
#
# ANCHORED TO THE START OF A LINE, and that is not cosmetic. An unanchored search matched
# prose *about* the marker: a comment on ops#20 reading "same reasoning as the `**Closing`
# marker" was read as a resolution, and the audit demanded the issue be closed. A checker
# that cannot tell a resolution from a discussion of resolutions is the same
# two-states-look-identical failure the marker exists to prevent.
#
# Real closing comments open with the marker, so requiring it at line start (allowing
# leading whitespace and blockquote markers) keeps every genuine one and drops the
# mentions. A backticked inline mention can never sit at line start after a backtick,
# because the backtick itself is not whitespace.
CANONICAL = re.compile(r"^[ \t>]*\*\*Closing\b", re.I | re.M)
LEGACY = [
    re.compile(r"^\s*Closing\.\s*$", re.I | re.M),
    re.compile(r"^[ \t>]*Closing\s+[-—]", re.I | re.M),
    re.compile(r"\bSuggest closing\b", re.I),
    re.compile(r"\bClosing rather than\b", re.I),
]

# Kept because classify() still reads labels, and because a future staleness rule must
# not nag issues explicitly parked on Wesley.
WAITING_LABELS = {"needs-wesley"}

# The six issue types. Type decides WHO ACTS and WHAT CLOSING REQUIRES - it is a
# contract, not a colour. Reasoning per type lives in harness/types/CONTRACTS.md in the
# ops repo; this file is the source of truth that enforces it.
TYPE_LABELS = {
    "type:decision", "type:input", "type:build",
    "type:research", "type:meta", "type:incident",
}

# Extra markers a CLOSED issue of each type must carry, beyond the universal
# `**Closing - <why>**`.
#
# WHY LITERAL MARKERS RATHER THAN READING THE PROSE. The same reason has_resolution()
# uses one: guessing whether a comment "sounds like" a recorded decision is exactly the
# heuristic this codebase keeps being burned by - a probe that could not tell "blocked"
# from "needs JavaScript", a privacy check keyed on vocabulary that failed on its own
# text. An exact marker is checkable; a vibe is not.
#
# type:build and type:meta deliberately carry no extra marker. Their real contract is a
# merged PR, which lives in git rather than in a comment, and a regex hunting for issue
# or commit references would flag every legitimately abandoned build issue. Enforcing it
# here would be theatre.
CONTRACTS: dict[str, list[tuple[str, str]]] = {
    "type:decision": [
        (r"\*\*Decision\b",
         "**Decision (recorded)** - the choice, Wesley's own words, and what changed"),
    ],
    "type:input": [
        (r"\*\*Input accepted\b",
         "**Input accepted** - the paste received, and its validator's output"),
    ],
    "type:research": [
        (r"\*\*Finding\b",
         "**Finding** - a measurement and its confidence level"),
    ],
    "type:incident": [
        (r"\*\*Cause\b", "**Cause** - why it was possible, not just what triggered it"),
        (r"\*\*Guard\b", "**Guard** - what makes recurrence impossible"),
    ],
}

# BODY CONTRACTS. The protocol promises three things about how an issue is WRITTEN, and
# until now nothing checked any of them - they held only because one session happened to
# write them that way. The first type:decision filed by a tired agent at 3am without a
# prompt block is a decision Wesley cannot answer from his phone, which is the single
# property the whole arrangement was designed around.
#
# Presence only, never quality. Whether a prompt block is any GOOD is exactly the
# judgement a regex cannot make, and pretending otherwise would be the vocabulary-sniffing
# this file already refuses elsewhere. The reviewer agent judges; this counts.
#
# Checked on OPEN issues only. Closed ones predate the scheme and reflagging history
# forever is the noise that got the old empty-issue rule deleted.
BODY_CONTRACTS: dict[str, list[tuple[str, str]]] = {
    "type:decision": [
        (r"copy-paste prompt",
         "a copy-paste prompt block - Wesley answers from a phone, and an issue that "
         "needs the repo open to understand cannot be answered there"),
        (r"^[ \t>]*(?:#+\s*|\*\*)Recommendation",
         "a **Recommendation** - 'you decide' hands the work back to the PM, which is "
         "the one thing this arrangement exists to remove"),
    ],
    "type:input": [
        (r"^[ \t>]*(?:#+\s*|\*\*)Validator",
         "a **Validator** naming what grades the paste - an input contract with no "
         "validator is a wish, and 'looks right' is how a constant gets poisoned"),
    ],
}


# An agent claims an issue before working it, so two agents never spend twice on one
# ticket. A `claimed` label with no claim comment means nobody can say WHICH agent holds
# it - which in practice is an agent that died mid-run and left the ticket locked.
CLAIM = re.compile(r"\*\*Claiming\b", re.I)

# ops#68. An issue that ESTABLISHES a value must say where the value landed. The audit
# measured 5.2% of numeric values in marker-carrying issues living in an issue and nowhere
# else - invisible the moment it closes. Both known losses have this shape: ops#13's cost
# basis was re-derived (badly) from scratch, and ops#10's calendar step went unconfirmed
# for days.
#
# Deliberately NOT a provenance graph. 90.8% of values are already in a store; this closes
# the leak rather than rebuilding the record-keeping.
RECORDED = re.compile(r"^[ \t>]*\*\*Recorded in\*\*", re.I | re.M)

# Refusing to store something is a real answer and must be sayable, or the check becomes a
# nag and gets switched off - the failure ops#68 explicitly warns about, and the one the
# pre-push SKIPPED grep already caused once.
RECORDED_NOWHERE = re.compile(r"^[ \t>]*\*\*Recorded in\*\*\s*[-\u2014:]*\s*nowhere\b",
                              re.I | re.M)

# A path named by the marker. Backticked first (the documented form), else a bare token
# that looks like a path. `ops:` marks the PRIVATE repo, which this checker cannot see.
RECORDED_PATH = re.compile(r"\*\*Recorded in\*\*[^\n]*?`([^`]+)`", re.I)
RECORDED_PATH_BARE = re.compile(
    r"\*\*Recorded in\*\*\s*[-\u2014:]*\s*((?:ops:)?[\w.\-]+/[\w./\-]+)", re.I)

# Existing closed issues predate the marker. 26 carry contract markers and reflagging
# history forever is the noise that got the old empty-issue rule deleted - the same
# reasoning ops#68 asked for and that already scopes the log's `Learned` field.
RECORDED_FROM_ISSUE = 74

# Markers that mean "this issue established something". A type:build or type:meta closes
# on a merged PR rather than on a value, so neither is asked for a pointer.
ESTABLISHING = ("**Finding**", "**Input accepted**")

# The other half of a claim, and the one that was missing. `claimed` with no comment is a
# lock nobody can attribute; a claim with no DISCHARGE is a lock nobody released - the
# actor announced it was working and then stopped, which is invisible until someone else
# wants the ticket. The protocol requires the claim comment to carry a horizon ("when to
# assume I died"), so a horizon in the past on a still-open claim is the mechanical signal.
#
# Accepts a bare date or an ISO timestamp, because both forms are already in use.
HORIZON = re.compile(
    r"\b(\d{4}-\d{2}-\d{2})(?:[T ](\d{2}):(\d{2})(?::\d{2})?\s*Z?)?", re.I)


def claim_horizon(bodies: list[str]) -> dt.datetime | None:
    """Latest horizon stated in any claim comment, as UTC. None if none is parseable."""
    latest = None
    for b in bodies:
        if not b or not CLAIM.search(b):
            continue
        m = HORIZON.search(b)
        if not m:
            continue
        day = dt.date.fromisoformat(m.group(1))
        hh = int(m.group(2)) if m.group(2) else 23
        mm = int(m.group(3)) if m.group(3) else 59
        when = dt.datetime(day.year, day.month, day.day, hh, mm, tzinfo=dt.timezone.utc)
        if latest is None or when > latest:
            latest = when
    return latest


def has_resolution(bodies: list[str]) -> str | None:
    """Return 'canonical', 'legacy', or None."""
    for b in bodies:
        if CANONICAL.search(b or ""):
            return "canonical"
    for b in bodies:
        for pat in LEGACY:
            if pat.search(b or ""):
                return "legacy"
    return None




def recorded_paths(n, title, blob, root=ROOT):
    """Check every path a **Recorded in** marker names. A pointer to nothing is worse
    than no pointer - it reads as provenance while leading somewhere that does not exist.

    An `ops:` prefix names the PRIVATE repo, which this checker cannot see from the
    public one. Those are accepted unverified rather than flagged, because flagging a
    correct pointer is the cry-wolf failure ops#68 warned about; the prefix is what makes
    "unverifiable" distinguishable from "wrong".
    """
    found = RECORDED_PATH.findall(blob) or RECORDED_PATH_BARE.findall(blob)
    if not found:
        return [("flag", f"#{n} has **Recorded in** but names no path - say where, or "
                         f"say 'nowhere, and why': {title}")]
    out = []
    for raw in found:
        path = raw.strip().split("->")[0].split("\u2192")[0].strip().strip("`")
        if path.startswith("ops:"):
            continue  # private repo, not visible from here - see docstring
        if not (root / path).exists():
            out.append(("flag", f"#{n} **Recorded in** names {path!r}, which does not "
                                f"exist - a pointer to nothing reads as provenance: "
                                f"{title}"))
    return out

def classify(issue: dict, bodies: list[str],
             now: dt.datetime | None = None) -> list[tuple[str, str]]:
    """Return (level, message) findings for one issue. Pure - no network."""
    n = issue["number"]
    title = (issue.get("title") or "")[:60]
    state = issue["state"].lower()
    labels = {l.lower() for l in issue.get("labels", [])}
    kind = has_resolution(bodies)
    out: list[tuple[str, str]] = []

    if state == "open" and kind:
        # The one Wesley asked to enforce.
        out.append(("flag", f"#{n} OPEN but a resolution is already written "
                            f"({kind}) - close it: {title}"))
    if state == "closed" and not kind:
        out.append(("flag", f"#{n} CLOSED with no closing comment - the resolution is "
                            f"unrecorded: {title}"))

    types = labels & TYPE_LABELS
    blob = "\n".join(b or "" for b in bodies)

    # An untyped OPEN issue cannot be routed: nothing says whether it is Wesley's to
    # answer or an agent's to build, and an unattended session has no other signal.
    # Closed issues are exempt - 21 of them predate the scheme and reflagging history
    # forever is noise, which is the failure mode noted just below.
    if state == "open" and not types:
        out.append(("flag", f"#{n} has no type: label - unroutable: {title}"))
    if len(types) > 1:
        out.append(("flag", f"#{n} has {len(types)} type: labels ({', '.join(sorted(types))}) "
                            f"- type decides who acts, so it must be exactly one: {title}"))

    # The per-type closing contract. Only meaningful once the issue is closed AND it
    # actually recorded a resolution - otherwise the generic "closed with no closing
    # comment" flag above already says the useful thing, and adding a second line about
    # a missing sub-marker is the same finding twice.
    if state == "closed" and kind:
        for tl in sorted(types):
            for pat, what in CONTRACTS.get(tl, []):
                if not re.search(pat, blob, re.I):
                    out.append(("flag", f"#{n} closed as {tl} without {what}: {title}"))

    # What the issue ASKS. Open issues only.
    #
    # Body AND comments, deliberately. The contract is that the ISSUE carries a prompt
    # block, not that one particular field does - and several issues legitimately gained
    # theirs in a comment when they were retyped under this scheme. A body-only check
    # flagged three of those as non-compliant while they were fully compliant to any
    # reader, which is a checker measuring the wrong thing.
    if state == "open":
        asked = (issue.get("body") or "") + "\n" + blob
        for tl in sorted(types):
            for pat, what in BODY_CONTRACTS.get(tl, []):
                if not re.search(pat, asked, re.I | re.M):
                    out.append(("flag", f"#{n} is {tl} but lacks {what}: {title}"))

    # A lock nobody can attribute. See CLAIM.
    if "claimed" in labels and state == "open" and CLAIM.search(blob):
        horizon = claim_horizon(bodies)
        if horizon is None:
            out.append(("flag", f"#{n} has a claim with no horizon - nothing can tell a "
                                f"live claim from an abandoned one: {title}"))
        elif now is not None and horizon < now:
            out.append(("flag", f"#{n} claim horizon passed {horizon:%Y-%m-%dT%H:%MZ} and "
                                f"it is still open and claimed - discharge it (deliver, "
                                f"or say what blocked you) or release it: {title}"))
    # ---- ops#68: a value established here must say where it landed ----
    if state == "closed" and n >= RECORDED_FROM_ISSUE:
        establishes = [m for m in ESTABLISHING if m.lower() in blob.lower()]
        if establishes:
            if not RECORDED.search(blob):
                out.append(("flag", f"#{n} closed with {establishes[0]} but no "
                                    f"**Recorded in** - the value it established is in "
                                    f"prose only, which is how ops#13 and ops#10 were "
                                    f"lost: {title}"))
            elif not RECORDED_NOWHERE.search(blob):
                out.extend(recorded_paths(n, title, blob))

    if "claimed" in labels and state == "open" and not CLAIM.search(blob):
        out.append(("flag", f"#{n} is labelled claimed but no agent said so - "
                            f"stale lock, release it: {title}"))

    # Deliberately NOT flagged: an open issue with no comments. It fired on five
    # perfectly healthy issues that simply had a complete body and nothing to add, and
    # five lines of noise on every run is how a check teaches people to skip its output.
    # Same reason the privacy check stopped keying on vocabulary.
    return out


def fetch(repo: str, limit: int) -> list[dict]:
    def gh(*args: str) -> str:
        r = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            raise RuntimeError(r.stderr.strip()[:300])
        return r.stdout

    raw = gh("api", f"repos/{repo}/issues?state=all&per_page={limit}",
             "--jq", '[.[] | select(has("pull_request")|not) | '
                     '{number, title, state, body, labels: [.labels[].name], comments}]')
    issues = json.loads(raw)
    out = []
    for it in issues:
        bodies: list[str] = []
        if it.get("comments", 0):
            cr = gh("api", f"repos/{repo}/issues/{it['number']}/comments?per_page=100",
                    "--jq", "[.[].body]")
            bodies = json.loads(cr)
        out.append((it, bodies))
    return out


def run(strict: bool, limit: int) -> int:
    repo = tracker()
    print(f"auditing {repo}")
    try:
        pairs = fetch(repo, limit)
    except Exception as e:  # noqa: BLE001
        print(f"could not reach the tracker: {e}", file=sys.stderr)
        print("SKIPPED - this check needs network and gh auth.", file=sys.stderr)
        return 1 if strict else 0

    flags, notes = [], []
    for issue, bodies in pairs:
        for level, msg in classify(issue, bodies, now=dt.datetime.now(dt.timezone.utc)):
            (flags if level == "flag" else notes).append(msg)

    n_open = sum(1 for i, _ in pairs if i["state"].lower() == "open")
    print(f"{len(pairs)} issues ({n_open} open)")
    for m in notes:
        print(f"  note  {m}")
    if flags:
        print(f"\n{len(flags)} HYGIENE FINDING(S):", file=sys.stderr)
        for m in flags:
            print(f"  - {m}", file=sys.stderr)
        return 1 if strict else 0
    print("\nclean - every resolved issue is closed, every closed issue says why")
    return 0


def self_test() -> int:
    fails = []

    def check(label, got, want):
        if got != want:
            fails.append(f"{label}: got {got!r}, want {want!r}")

    def iss(n, state, labels=("type:build",), title="t", body=""):
        return {"number": n, "state": state, "labels": list(labels), "title": title,
                "body": body}

    GOOD_DECISION = ("## Recommendation\nDo A, medium confidence.\n\n"
                     "<details><summary>copy-paste prompt</summary>...</details>")

    def levels(issue, bodies, now=None):
        return sorted(l for l, _ in classify(issue, bodies, now=now))

    # The canonical marker.
    check("canonical marker detected", has_resolution(["**Closing - built.**"]), "canonical")
    check("canonical is case-insensitive", has_resolution(["**closing** now"]), "canonical")
    # Legacy forms.
    check("bare 'Closing.' line", has_resolution(["all done\n\nClosing."]), "legacy")
    check("em-dash form", has_resolution(["Closing — delivered"]), "legacy")
    check("suggest-closing form", has_resolution(["Suggest closing."]), "legacy")
    # Prose ABOUT the marker must not count as using it. This fired for real: a comment
    # on ops#20 explaining the convention - "same reasoning as the `**Closing` marker" -
    # was read as a resolution and the audit demanded the issue be closed.
    check("an inline mention of the marker is not a resolution",
          has_resolution(["same reasoning as the `**Closing` marker"]), None)
    check("mid-sentence mention is not a resolution",
          has_resolution(["I will add a **Closing note later"]), None)
    check("a real closing comment still counts",
          has_resolution(["**Closing - built and merged.**"]), "canonical")
    check("a closing marker after a blank line still counts",
          has_resolution(["Some preamble.\n\n**Closing - done.**"]), "canonical")
    check("a quoted closing line still counts",
          has_resolution(["> **Closing - done.**"]), "canonical")

    # Prose that must NOT count. This is the whole reason for an explicit marker.
    check("ordinary prose is not a resolution",
          has_resolution(["I am closing in on the bug", "nearly done", "built a prototype"]), None)
    check("the word 'closed' alone is not a resolution",
          has_resolution(["the window has closed by then"]), None)
    check("no comments", has_resolution([]), None)

    # Open with a resolution written -> flag. The failure Wesley asked to enforce.
    check("open + resolution is flagged",
          levels(iss(1, "open"), ["**Closing - built.**"]), ["flag"])
    # Open without one -> nothing (given it has discussion).
    check("open + no resolution is fine",
          levels(iss(2, "open"), ["still working on it"]), [])
    # Closed without a resolution -> flag.
    check("closed + no resolution is flagged",
          levels(iss(3, "closed"), ["some discussion"]), ["flag"])
    check("closed + no comments at all is flagged",
          levels(iss(4, "closed"), []), ["flag"])
    # Closed with one -> clean.
    check("closed + resolution is fine",
          levels(iss(5, "closed"), ["**Closing - done.**"]), [])
    # Open, no comments, parked on Wesley -> not nagged.
    # An open issue with no comments is healthy - it just has a complete body. Asserted
    # so the noisy rule that once existed here does not come back.
    check("needs-wesley with no comments is not nagged",
          levels(iss(6, "open", ["needs-wesley", "type:decision"],
                     body=GOOD_DECISION), []), [])
    check("open with no comments is not nagged either",
          levels(iss(7, "open"), []), [])

    # ---- body contracts: what the issue ASKS (ops#34) ----
    # A decision Wesley cannot answer from his phone is not a decision issue.
    check("a decision with no prompt block and no recommendation flags twice",
          levels(iss(30, "open", ["type:decision"], body="just a question"), []),
          ["flag", "flag"])
    check("a decision with a prompt but no recommendation flags once",
          levels(iss(31, "open", ["type:decision"],
                     body="<summary>copy-paste prompt</summary>"), []), ["flag"])
    check("a complete decision is clean",
          levels(iss(32, "open", ["type:decision"], body=GOOD_DECISION), []), [])
    check("the recommendation heading form works",
          levels(iss(33, "open", ["type:decision"],
                     body="## Recommendation: do A\ncopy-paste prompt"), []), [])
    check("an input naming no validator is flagged",
          levels(iss(34, "open", ["type:input"], body="paste the chart"), []), ["flag"])
    check("an input naming one is clean",
          levels(iss(35, "open", ["type:input"],
                     body="**Validator:** npm run check:bands"), []), [])
    # CLOSED issues are exempt - they predate the scheme, and reflagging history forever
    # is the noise that deleted the old empty-issue rule.
    check("a closed decision with an empty body is not flagged",
          levels(iss(36, "closed", ["type:decision"], body=""),
                 ["**Decision (recorded)** - x", "**Closing - done.**"]), [])
    # Presence only. A prompt block mentioned in passing still counts, deliberately -
    # judging whether it is any GOOD is what the reviewer is for, and a regex pretending
    # to would be the vocabulary-sniffing this file refuses everywhere else.
    check("a prompt block supplied in a COMMENT satisfies the contract",
          levels(iss(38, "open", ["type:decision"], body="bare"),
                 ["## Recommendation\ndo A", "copy-paste prompt here"]), [])

    check("build and research carry no body contract",
          levels(iss(37, "open", ["type:research"], body=""), []), [])

    # ---- routing: type is what tells an unattended session who acts ----
    check("open with no type label is flagged",
          levels(iss(8, "open", []), ["discussion"]), ["flag"])
    check("closed with no type label is NOT flagged - 21 issues predate the scheme",
          levels(iss(9, "closed", []), ["**Closing - done.**"]), [])
    check("two type labels is flagged",
          levels(iss(10, "open", ["type:build", "type:research"]), []), ["flag"])

    # ---- per-type closing contracts ----
    check("decision closed without a recorded decision is flagged",
          levels(iss(11, "closed", ["type:decision"]), ["**Closing - picked B.**"]), ["flag"])
    check("decision closed WITH one is clean",
          levels(iss(12, "closed", ["type:decision"]),
                 ["**Decision (recorded)** - B", "**Closing - implemented.**"]), [])
    check("input closed without validator evidence is flagged",
          levels(iss(13, "closed", ["type:input"]), ["**Closing - got it.**"]), ["flag"])
    check("input closed WITH acceptance is clean",
          levels(iss(14, "closed", ["type:input"]),
                 ["**Input accepted** - check:bands green", "**Closing - transcribed.**"]), [])
    check("research closed without a finding is flagged",
          levels(iss(15, "closed", ["type:research"]), ["**Closing - looked into it.**"]), ["flag"])
    check("research closed WITH a finding is clean",
          levels(iss(16, "closed", ["type:research"]),
                 ["**Finding** - Spearman -0.914, confidence: measured", "**Closing - answered.**"]), [])
    # A negative result is a successful close. Discovery publishing no prices was one of
    # the most valuable findings this project produced.
    check("a negative finding closes research cleanly",
          levels(iss(17, "closed", ["type:research"]),
                 ["**Finding** - priceRanges absent on all 44 events; the premise was wrong.",
                  "**Closing - falsified.**"]), [])
    check("incident needs BOTH cause and guard - one alone is flagged",
          levels(iss(18, "closed", ["type:incident"]),
                 ["**Cause** - fixture used a real value", "**Closing - fixed.**"]), ["flag"])
    check("incident with cause and guard is clean",
          levels(iss(19, "closed", ["type:incident"]),
                 ["**Cause** - fixture used a real value",
                  "**Guard** - pre-commit hook scans added lines", "**Closing - fixed.**"]), [])
    check("build carries no extra marker beyond the universal one",
          levels(iss(20, "closed", ["type:build"]), ["**Closing - merged in #31.**"]), [])
    check("meta carries no extra marker either",
          levels(iss(21, "closed", ["type:meta"]), ["**Closing - rule reworded.**"]), [])
    # The contract is only checked once a resolution exists, so a closed-with-nothing
    # issue reports ONE finding, not two saying the same thing.
    check("closed with nothing at all reports one finding, not two",
          levels(iss(22, "closed", ["type:incident"]), []), ["flag"])

    # ---- claim locks ----
    check("claimed without a claim comment is a stale lock",
          levels(iss(23, "open", ["type:build", "claimed"]), ["some notes"]), ["flag"])
    # ---- ops#68: **Recorded in** ----
    FIND = "**Finding** - the fee is 10%"
    # 1. established a value, said where it went, and the path exists.
    check("recorded in a real path passes",
          levels(iss(80, "closed", ["type:research"]),
                 [FIND, "**Closing - done**\n**Recorded in** `config/economics.json`"]), [])
    # 2. established a value and said nothing.
    got = levels(iss(81, "closed", ["type:research"]), [FIND, "**Closing - done**"])
    check("no Recorded in is flagged", got, ["flag"])
    # 3. a pointer to nothing - worse than no pointer, because it reads as provenance.
    check("a path that does not exist is flagged",
          levels(iss(82, "closed", ["type:research"]),
                 [FIND, "**Closing**\n**Recorded in** `config/nope.json`"]), ["flag"])
    # 4. refusing to store is a real answer and must be sayable, or the check is a nag.
    check("the nowhere form passes",
          levels(iss(83, "closed", ["type:research"]),
                 [FIND, "**Closing**\n**Recorded in** - nowhere, and why: it is a "
                        "worked example in absurd values"]), [])

    # An ops: path is in the PRIVATE repo and cannot be seen from here. Accepting it
    # unverified is the whole reason the prefix exists - flagging a correct pointer is
    # the cry-wolf failure ops#68 warns about.
    check("an ops: path is accepted unverified",
          levels(iss(84, "closed", ["type:research"]),
                 [FIND, "**Closing**\n**Recorded in** `ops:data/profile/snapshots.jsonl`"]),
          [])
    # A key suffix after the path must not be treated as part of the filename.
    check("a -> key suffix is stripped before checking",
          levels(iss(85, "closed", ["type:research"]),
                 [FIND, "**Closing**\n**Recorded in** `config/economics.json -> resale`"]), [])
    # The marker with no path at all is not provenance.
    check("Recorded in naming no path is flagged",
          levels(iss(86, "closed", ["type:research"]),
                 [FIND, "**Closing**\n**Recorded in** somewhere sensible"]), ["flag"])

    # Scope. Existing closed issues predate the marker; reflagging history is the noise
    # that killed the old empty-issue rule.
    check("an issue below the cutoff is exempt",
          levels(iss(RECORDED_FROM_ISSUE - 1, "closed", ["type:research"]),
                 [FIND, "**Closing - done**"]), [])
    # An OPEN issue has not established anything yet - the pointer is due at close.
    check("an open issue is not asked for a pointer",
          levels(iss(87, "open", ["type:research", "ready"]), [FIND]), [])
    # type:build closes on a merged PR, not on a value, so it is never asked.
    check("a build issue with no Finding is not asked",
          levels(iss(88, "closed", ["type:build"]), ["**Closing - merged**"]), [])
    # Input accepted establishes a value just as much as a Finding does.
    check("Input accepted also requires a pointer",
          levels(iss(89, "closed", ["type:input"]),
                 ["**Input accepted**", "**Closing - done**"]), ["flag"])

    # ---- claim discharge: the half that was missing ----
    NOW = dt.datetime(2026, 9, 7, 12, 0, tzinfo=dt.timezone.utc)
    live = "**Claiming** - agent-x, reviewing head abc123, horizon 2026-09-07T20:00:00Z"
    dead = "**Claiming** - agent-x, reviewing head abc123, horizon 2026-09-07T06:00:00Z"
    nohz = "**Claiming** - agent-x, I will get to this"

    check("a live claim is not flagged",
          levels(iss(30, "open", ["type:build", "claimed"]), [live], now=NOW), [])
    check("a claim past its horizon is flagged",
          levels(iss(31, "open", ["type:build", "claimed"]), [dead], now=NOW), ["flag"])
    check("a claim with no horizon is flagged",
          levels(iss(32, "open", ["type:build", "claimed"]), [nohz], now=NOW), ["flag"])
    # A closed issue discharges its claim by being closed - never flag it. Closing with no
    # closing comment is a different finding and the existing rule already covers it.
    check("closing discharges the claim",
          levels(iss(33, "closed", ["type:build", "claimed"]), [dead, "**Closing - done**"],
                 now=NOW), [])
    # A bare date means end of that day, not midnight - otherwise every same-day claim
    # would read as already expired the moment it was written.
    check("a bare date horizon lasts the whole day",
          levels(iss(34, "open", ["type:build", "claimed"]),
                 ["**Claiming** - agent-x, horizon 2026-09-07"], now=NOW), [])
    check("yesterday's bare date is expired",
          levels(iss(35, "open", ["type:build", "claimed"]),
                 ["**Claiming** - agent-x, horizon 2026-09-06"], now=NOW), ["flag"])
    # The latest horizon wins, so an actor may extend its own claim by re-claiming.
    check("a re-claim extends the horizon",
          levels(iss(36, "open", ["type:build", "claimed"]), [dead, live], now=NOW), [])
    # Without a clock the rule must stay silent rather than guess.
    check("no clock means no staleness verdict",
          levels(iss(37, "open", ["type:build", "claimed"]), [dead]), [])

    # Carries a horizon because the protocol requires one (harness/agents/README.md).
    # This fixture used to omit it and asserted "fine", which encoded a weaker contract
    # than the protocol states - the horizon rule above is what caught that.
    check("claimed with a claim comment is fine",
          levels(iss(24, "open", ["type:build", "claimed"]),
                 ["**Claiming** - worker-a, building the validator, horizon 2026-09-09"]), [])

    # Every type must have a routing entry, or an issue could be filed under a label the
    # audit silently ignores - the exact class of gap that let a nested data store go
    # unchecked by the privacy pass.
    check("every contract key is a known type", set(CONTRACTS) <= TYPE_LABELS, True)
    check("all six types exist", len(TYPE_LABELS), 6)

    # The tracker declaration must be readable - the audit is useless pointed at the
    # wrong repo, and this repo's issues are disabled.
    t = tracker()
    check("tracker is the ops repo", t.endswith("-ops"), True)

    for f in fails:
        print(f"  FAIL {f}", file=sys.stderr)
    print(f"self-test: {'FAILED' if fails else 'passed'} ({len(fails)} failure(s))")
    return 1 if fails else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--strict", action="store_true", help="non-zero exit on any finding")
    ap.add_argument("--limit", type=int, default=100)
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()
    return self_test() if args.self_test else run(args.strict, args.limit)


if __name__ == "__main__":
    sys.exit(main())
