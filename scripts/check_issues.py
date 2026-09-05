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

Needs network and `gh`, so it is NOT part of `npm test`, which is deliberately offline.
The classifier is pure and self-tested; only the fetch touches the network.

Usage:
    python3 scripts/check_issues.py                # audit
    python3 scripts/check_issues.py --strict       # non-zero exit if anything is flagged
    python3 scripts/check_issues.py --self-test    # classifier only, no network
"""

import argparse
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
CANONICAL = re.compile(r"\*\*Closing\b", re.I)
LEGACY = [
    re.compile(r"^\s*Closing\.\s*$", re.I | re.M),
    re.compile(r"\bClosing\s+[-—]", re.I),
    re.compile(r"\bSuggest closing\b", re.I),
    re.compile(r"\bClosing rather than\b", re.I),
]

# Kept because classify() still reads labels, and because a future staleness rule must
# not nag issues explicitly parked on Wesley.
WAITING_LABELS = {"needs-wesley"}


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


def classify(issue: dict, bodies: list[str]) -> list[tuple[str, str]]:
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
    # Deliberately NOT flagged: an open issue with no comments. It fired on five
    # perfectly healthy issues that simply had a complete body and nothing to add, and
    # five lines of noise on every run is how a check teaches people to skip its output.
    # Same reason the privacy check stopped keying on vocabulary. Two checks only.
    return out


def fetch(repo: str, limit: int) -> list[dict]:
    def gh(*args: str) -> str:
        r = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            raise RuntimeError(r.stderr.strip()[:300])
        return r.stdout

    raw = gh("api", f"repos/{repo}/issues?state=all&per_page={limit}",
             "--jq", '[.[] | select(has("pull_request")|not) | '
                     '{number, title, state, labels: [.labels[].name], comments}]')
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
        for level, msg in classify(issue, bodies):
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

    def iss(n, state, labels=(), title="t"):
        return {"number": n, "state": state, "labels": list(labels), "title": title}

    def levels(issue, bodies):
        return sorted(l for l, _ in classify(issue, bodies))

    # The canonical marker.
    check("canonical marker detected", has_resolution(["**Closing - built.**"]), "canonical")
    check("canonical is case-insensitive", has_resolution(["**closing** now"]), "canonical")
    # Legacy forms.
    check("bare 'Closing.' line", has_resolution(["all done\n\nClosing."]), "legacy")
    check("em-dash form", has_resolution(["Closing — delivered"]), "legacy")
    check("suggest-closing form", has_resolution(["Suggest closing."]), "legacy")
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
          levels(iss(6, "open", ["needs-wesley"]), []), [])
    check("open with no comments is not nagged either",
          levels(iss(7, "open"), []), [])

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
