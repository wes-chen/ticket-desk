#!/usr/bin/env python3
"""Run every script's self-test, and refuse to let untested scripts go unnoticed (ops#17).

ops#17 says no test suite exists, and CLAUDE.md's conventions say "until there is one,
verify behaviour explicitly rather than assuming a clean build means correct." Seven
scripts have since grown a `--self-test`, each testing against REAL captured fixtures
rather than invented ones. That is a test suite; it just had no runner.

WHY DISCOVERY, NOT A LIST. A hand-maintained list of test targets drifts, and the drift
is silent - a new script with no test simply never appears. So this discovers
`--self-test` support by reading the sources, and separately asserts that every script
either has a self-test or is on an EXEMPT list with a stated reason. Adding an untested
script fails the suite. That is the same principle as the privacy check's structural
pass: cover the surface automatically, so growth does not create blind spots.

The exemptions are deliberately uncomfortable to read, because two of them should not be
permanent.
"""

import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"

# Scripts with no self-test, and why. Anything not here and not self-testing fails.
EXEMPT = {
    "make_icons.py": "one-off asset generation, output checked by eye",
    "fetch_schedule.py": (
        "IS a validator - it cross-checks the tier table against the live NHL API on "
        "date and opponent and exits non-zero on disagreement. Its 'test' is running it."
    ),
    "probe_browser.mjs": (
        "needs a real browser and network; run manually. Its verdict logic is duplicated "
        "in probe_sources.py, which IS self-tested against captured observations."
    ),
}

# Formerly listed here as acknowledged GAPS and since closed: check_privacy.py (now
# self-tested against a real temp git repo, covering the history pass whose
# record-splitting bug once mis-attributed findings to the wrong commit) and
# summarize_market.py. Both were fixed rather than left documented.

SELF_TEST_RE = re.compile(r'"--self-test"')

SELF = pathlib.Path(__file__).name


def discoverable() -> tuple[list[pathlib.Path], list[pathlib.Path]]:
    """Split scripts into (self-testing, not).

    This runner is skipped before the regex runs, not after. It contains the literal
    "--self-test" in the command it builds, so string-matching discovery classified the
    runner as a test target and re-invoked itself - unbounded recursion that presented as
    a hang rather than an error. Being on the EXEMPT list did not help, because EXEMPT is
    only consulted for scripts that were NOT detected as self-testing.
    """
    tested, untested = [], []
    for f in sorted(SCRIPTS.iterdir()):
        if f.name == SELF or f.name.startswith("_") or f.suffix not in (".py", ".mjs", ".mts"):
            continue
        try:
            src = f.read_text()
        except OSError:
            continue
        (tested if SELF_TEST_RE.search(src) else untested).append(f)
    return tested, untested


def run_one(f: pathlib.Path) -> tuple[bool, str]:
    if f.suffix == ".mts":
        # Node 24 strips TypeScript types natively, so the web libs are testable with
        # no test-runner dependency. Warnings are noise here and are filtered below.
        cmd = ["node", "--experimental-strip-types", "--no-warnings", str(f)]
    elif f.suffix == ".mjs":
        cmd = ["node", str(f)]
    else:
        cmd = [sys.executable, str(f)]
    try:
        r = subprocess.run(cmd + ["--self-test"], capture_output=True, text=True,
                           cwd=str(ROOT), timeout=60)
    except subprocess.TimeoutExpired:
        # A self-test that hangs is a failure, not a pause. Reported rather than waited
        # on, because the first version of this runner hung silently.
        return False, "TIMEOUT after 60s - a self-test must not block on network or stdin"
    out = (r.stdout + r.stderr).strip()
    return r.returncode == 0, out


def main() -> int:
    tested, untested = discoverable()

    print(f"discovered {len(tested)} self-testing script(s)\n")
    failures = []
    for f in tested:
        ok, out = run_one(f)
        last = out.splitlines()[-1] if out else "(no output)"
        print(f"  {'PASS' if ok else 'FAIL'}  {f.name:28s} {last}")
        if not ok:
            failures.append((f.name, out))

    # Coverage: an untested script must be explicitly exempted, with a reason.
    unexplained = [f.name for f in untested if f.name not in EXEMPT]
    gaps = [n for n, why in EXEMPT.items() if why.startswith("GAP")]

    print(f"\ncoverage: {len(tested)} tested, {len(untested)} not tested "
          f"({len(untested) - len(unexplained)} exempted)")
    if gaps:
        print(f"acknowledged gaps ({len(gaps)}), see EXEMPT in this file:")
        for n in gaps:
            print(f"  ! {n}")

    if failures:
        print(f"\n{len(failures)} FAILING SUITE(S):", file=sys.stderr)
        for name, out in failures:
            print(f"\n--- {name} ---", file=sys.stderr)
            print(out, file=sys.stderr)
    if unexplained:
        print(f"\n{len(unexplained)} SCRIPT(S) WITH NO SELF-TEST AND NO EXEMPTION:",
              file=sys.stderr)
        for n in unexplained:
            print(f"  - {n}", file=sys.stderr)
        print("Add a --self-test, or add an entry to EXEMPT in scripts/run_tests.py "
              "saying why not.", file=sys.stderr)

    if failures or unexplained:
        return 1
    print("\nall suites pass")
    return 0


if __name__ == "__main__":
    sys.exit(main())
