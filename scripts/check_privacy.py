#!/usr/bin/env python3
"""Fail the build if anything personal would ship publicly.

This exists because the leak already happened once: form placeholders were written
using real seat and invoice values, which put them straight into the public bundle
even though the config files had been scrubbed. Scrubbing config is not sufficient -
personal data can leak through UI copy, examples, comments, or test fixtures.

Two independent checks:

1. STRUCTURAL (always runs, safe in CI). Committed config/ and data/ must not contain
   keys that hold personal values. Catches "someone put creditPerSeat back".

2. LITERAL (runs only if .private-patterns exists locally). That file lists the actual
   private strings and is gitignored - committing it would defeat the purpose. Catches
   the placeholder-style leak by searching the built output for the real values.
"""

import json
import os
import pathlib
import subprocess
import sys

# History findings are advisory while the repo is private, and fatal before it goes
# public - which is the exact moment they start to matter. Set to 1 in the workflow
# that publishes, or before flipping visibility.
HISTORY_FATAL = os.environ.get("PRIVACY_HISTORY_FATAL") == "1"

ROOT = pathlib.Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
PATTERNS_FILE = ROOT / ".private-patterns"

# Keys that must never appear in committed JSON - they hold seat-dependent or
# account-dependent values.
FORBIDDEN_KEYS = {
    "creditPerSeat",
    "invoiceTotal",
    "seasonInvoiceTotal",
    "perSeatSeason",
    "costBasis",
    "exchangeCreditPerSeat",
    "faceValuePerSeat",
    "invoicePerSeat",
}


def walk(obj, path=""):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield path, k, v
            yield from walk(v, f"{path}.{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from walk(v, f"{path}[{i}]")


def structural() -> list[str]:
    problems = []
    for f in sorted(list((ROOT / "config").glob("*.json")) + list((ROOT / "data").glob("*.json"))):
        try:
            data = json.loads(f.read_text())
        except json.JSONDecodeError as e:
            problems.append(f"{f.relative_to(ROOT)}: invalid JSON ({e})")
            continue
        for path, key, _ in walk(data):
            if key in FORBIDDEN_KEYS:
                problems.append(
                    f"{f.relative_to(ROOT)}: forbidden key '{key}' at {path or '<root>'} "
                    f"- personal values belong in the browser profile, not the repo"
                )
    return problems


def literal() -> tuple[list[str], bool]:
    if not PATTERNS_FILE.exists():
        return [], False
    pats = [
        ln.strip()
        for ln in PATTERNS_FILE.read_text().splitlines()
        if ln.strip() and not ln.startswith("#")
    ]
    problems = []

    # Scan the built output...
    targets = [f for f in DIST.rglob("*") if f.is_file()] if DIST.exists() else []
    if not DIST.exists():
        problems.append(f"{PATTERNS_FILE.name} present but dist/ not built - run `npm run build` first")

    # ...and every git-tracked file. Docs leak just as readily as bundles, and this repo
    # may need to go public for Pages to work on a free plan.
    tracked = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files"], capture_output=True, text=True, check=False
    )
    for rel in tracked.stdout.splitlines():
        p = ROOT / rel
        if p.is_file():
            targets.append(p)

    for f in sorted(set(targets)):
        if f.name == PATTERNS_FILE.name:
            continue
        try:
            text = f.read_text(errors="ignore")
        except OSError:
            continue
        for p in pats:
            if p in text:
                problems.append(f"{f.relative_to(ROOT)}: contains private value {p!r}")
    return problems, True


def history() -> tuple[list[str], bool]:
    """Scan git history, not just the working tree.

    This is the gap that actually matters. Scrubbing the current tree does nothing
    about commits that already shipped the data, and force-pushing does not delete
    unreachable objects from a remote - they stay fetchable by SHA until GitHub
    garbage-collects. So a repo whose tree is clean can still leak everything the
    moment it is made public.
    """
    if not PATTERNS_FILE.exists():
        return [], False
    pats = [
        ln.strip()
        for ln in PATTERNS_FILE.read_text().splitlines()
        if ln.strip() and not ln.startswith("#")
    ]

    problems = []
    for p in pats:
        # Commits that added or removed this literal in file content.
        r = subprocess.run(
            ["git", "-C", str(ROOT), "log", "--all", "--oneline", "-S", p],
            capture_output=True, text=True, check=False,
        )
        for line in r.stdout.splitlines():
            problems.append(f"history: commit {line.strip()} contains {p!r} in file content")

    # Commit messages leak just as readily as files.
    msgs = subprocess.run(
        ["git", "-C", str(ROOT), "log", "--all", "--format=%H%x00%B%x00"],
        capture_output=True, text=True, check=False,
    )
    for entry in msgs.stdout.split("\x00\x00"):
        if "\x00" not in entry:
            continue
        sha, body = entry.split("\x00", 1)
        for p in pats:
            if p in body:
                problems.append(f"history: commit message {sha.strip()[:9]} contains {p!r}")
    return problems, True


def main() -> int:
    problems = structural()
    lit, ran_literal = literal()
    problems += lit
    hist, ran_history = history()

    print(f"structural check: {len(FORBIDDEN_KEYS)} forbidden keys across config/ and data/")
    if ran_literal:
        print(f"literal check:    dist/ + tracked files scanned against {PATTERNS_FILE.name}")
    else:
        print(f"literal check:    SKIPPED (no {PATTERNS_FILE.name}; structural check only)")

    if ran_history:
        n = len(hist)
        state = "CLEAN" if n == 0 else f"{n} finding(s)"
        print(f"history check:    git log content + messages -> {state}")
        if hist and not HISTORY_FATAL:
            print("                  (advisory: this repo is private; history must be clean BEFORE going public)")
    if HISTORY_FATAL:
        problems += hist

    if problems:
        print(f"\n{len(problems)} PRIVACY PROBLEM(S):", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return 1

    print("\nclean - no personal data would ship")
    return 0


if __name__ == "__main__":
    sys.exit(main())
