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

def _remote_is_public() -> bool | None:
    """Ask GitHub whether the origin repo is public. None if it can't be determined.

    This matters more than it looks. History findings were originally advisory with a
    hardcoded message claiming "this repo is private" - which stayed reassuring after
    the repo went public, and let a real leak through. Ask, don't assume.
    """
    try:
        r = subprocess.run(
            ["gh", "repo", "view", "--json", "visibility", "-q", ".visibility"],
            capture_output=True, text=True, timeout=15, cwd=str(pathlib.Path(__file__).resolve().parent.parent),
        )
        v = r.stdout.strip().upper()
        return v == "PUBLIC" if v in {"PUBLIC", "PRIVATE", "INTERNAL"} else None
    except Exception:  # noqa: BLE001
        return None


REMOTE_PUBLIC = _remote_is_public()

# Fatal if explicitly requested, OR if the repo is actually public - the case where a
# history leak is live rather than hypothetical.
HISTORY_FATAL = os.environ.get("PRIVACY_HISTORY_FATAL") == "1" or REMOTE_PUBLIC is True

ROOT = pathlib.Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
PATTERNS_FILE = ROOT / ".private-patterns"
ACCEPTED_FILE = ROOT / ".privacy-accepted"

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


def scanned_dirs() -> tuple[str, ...]:
    return ("config", "data", "tests")


def _docs(f: pathlib.Path, root: pathlib.Path = ROOT):
    """Yield (label, parsed) for a committed data file.

    .jsonl is a separate case, not a nicety: the collector stores its series one JSON
    object per line, so json.loads on the whole file raises and the old code would have
    reported "invalid JSON" for a perfectly good store - a false alarm that trains
    people to ignore this check. Each line is parsed on its own instead.
    """
    text = f.read_text()
    rel = f.relative_to(root)
    if f.suffix == ".jsonl":
        for i, ln in enumerate(text.splitlines(), 1):
            if not ln.strip():
                continue
            try:
                yield f"{rel}:{i}", json.loads(ln)
            except json.JSONDecodeError as e:
                yield f"{rel}:{i}", e
    else:
        try:
            yield str(rel), json.loads(text)
        except json.JSONDecodeError as e:
            yield str(rel), e


def structural(root: pathlib.Path = ROOT) -> list[str]:
    problems = []
    # Recursive: the market series lives in data/market/, and a check that only sees
    # the top level would silently stop covering new data surfaces as they are added.
    # tests/ is included because fixtures are CAPTURED API RESPONSES, not hand-written
    # stubs. A capture is exactly the kind of file that can carry something personal in
    # without anyone reading it first.
    scanned = scanned_dirs()
    files = sorted(
        f
        for d in scanned
        for pat in ("*.json", "*.jsonl")
        for f in (root / d).rglob(pat)
    )
    for f in files:
        for label, data in _docs(f, root):
            if isinstance(data, json.JSONDecodeError):
                problems.append(f"{label}: invalid JSON ({data})")
                continue
            for path, key, _ in walk(data):
                if key in FORBIDDEN_KEYS:
                    problems.append(
                        f"{label}: forbidden key '{key}' at {path or '<root>'} "
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


def history(root: pathlib.Path = ROOT, patterns_file: pathlib.Path | None = None,
            accepted_file: pathlib.Path | None = None) -> tuple[list[str], bool]:
    """Scan git history, not just the working tree.

    This is the gap that actually matters. Scrubbing the current tree does nothing
    about commits that already shipped the data, and force-pushing does not delete
    unreachable objects from a remote - they stay fetchable by SHA until GitHub
    garbage-collects. So a repo whose tree is clean can still leak everything the
    moment it is made public.
    """
    patterns_file = patterns_file or PATTERNS_FILE
    accepted_file = accepted_file or ACCEPTED_FILE
    if not patterns_file.exists():
        return [], False
    pats = [
        ln.strip()
        for ln in patterns_file.read_text().splitlines()
        if ln.strip() and not ln.startswith("#")
    ]

    # Consciously accepted findings, by SHA. Still reported, just not fatal - an
    # accepted risk should stay visible rather than being erased from the output.
    accepted = set()
    if accepted_file.exists():
        accepted = {
            ln.strip()
            for ln in accepted_file.read_text().splitlines()
            if ln.strip() and not ln.startswith("#")
        }

    def is_accepted(sha: str) -> bool:
        return any(sha.startswith(a) or a.startswith(sha) for a in accepted)

    problems = []
    for p in pats:
        # Commits that added or removed this literal in file content.
        r = subprocess.run(
            ["git", "-C", str(root), "log", "--all", "--oneline", "-S", p],
            capture_output=True, text=True, check=False,
        )
        for line in r.stdout.splitlines():
            sha = line.split()[0] if line.split() else ""
            if is_accepted(sha):
                print(f"  (accepted) commit {sha} contains a private value in file content")
                continue
            problems.append(f"history: commit {line.strip()} contains {p!r} in file content")

    # Commit messages leak just as readily as files. Use an explicit record separator:
    # splitting on a doubled NUL misattributed findings to the wrong commit, because
    # records are SHA\0BODY\0SHA\0BODY with no doubled delimiter between them.
    msgs = subprocess.run(
        ["git", "-C", str(root), "log", "--all", "--format=%H%x00%B%x1e"],
        capture_output=True, text=True, check=False,
    )
    for entry in msgs.stdout.split("\x1e"):
        if "\x00" not in entry:
            continue
        sha, body = entry.split("\x00", 1)
        short = sha.strip()[:9]
        for p in pats:
            if p in body:
                if is_accepted(short):
                    print(f"  (accepted) commit message {short} contains a private value")
                    continue
                problems.append(f"history: commit message {short} contains {p!r}")
    return problems, True


# ------------------------------------------------------------------- self-test
#
# This is the most safety-critical script in the repo and it had no test until ops#17.
# Two of its bugs were real and shipped: the history pass did not exist at all (a clean
# tree read as safe while history leaked everything), and when it was added, splitting
# records on a doubled NUL mis-attributed findings to the wrong commit. Both were the
# kind of thing a test catches and a code read does not.


def self_test() -> int:
    import shutil
    import tempfile

    fails = []

    def check(label, got, want):
        if got != want:
            fails.append(f"{label}: got {got!r}, want {want!r}")

    # walk() must reach nested keys, inside lists too.
    keys = {k for _, k, _ in walk({"a": {"b": [{"creditPerSeat": 1}]}})}
    check("walk reaches keys nested under a list", "creditPerSeat" in keys, True)
    check("walk reaches top-level keys", "a" in keys, True)

    with tempfile.TemporaryDirectory() as d:
        root = pathlib.Path(d)
        (root / "config").mkdir()
        (root / "data" / "market").mkdir(parents=True)
        (root / "tests").mkdir()

        # Clean tree.
        (root / "config" / "ok.json").write_text('{"sellerFeeRate": 0.1}')
        (root / "data" / "market" / "s.jsonl").write_text('{"low": 5}\n{"low": 6}\n')
        check("clean tree passes structural", structural(root), [])

        # Forbidden key nested inside a list inside a JSON file.
        (root / "config" / "bad.json").write_text('{"tiers": [{"creditPerSeat": 120}]}')
        probs = structural(root)
        check("forbidden key in nested json caught", len(probs), 1)
        check("problem names the key", "creditPerSeat" in probs[0], True)
        (root / "config" / "bad.json").unlink()

        # Forbidden key on ONE line of a JSONL - and the line number must be right.
        (root / "data" / "market" / "s.jsonl").write_text(
            # Test values are deliberately absurd. An earlier version of this line used a
            # REAL invoice figure as the fixture value and shipped it to a public repo -
            # caught by this very script's literal pass, but only after the commit was
            # pushed. Never reach for a plausible number here; plausible means real.
            '{"low": 5}\n{"low": 6}\n{"invoiceTotal": 11111111}\n')
        probs = structural(root)
        check("forbidden key in jsonl caught", len(probs), 1)
        check("jsonl finding locates line 3", ":3:" in probs[0], True)

        # A malformed JSONL line is reported per line, and does not mask later lines.
        (root / "data" / "market" / "s.jsonl").write_text('{"low": 5}\nnot json\n{"costBasis": 1}\n')
        probs = structural(root)
        check("malformed jsonl line reported", any("invalid JSON" in x and ":2:" in x for x in probs), True)
        check("line after a malformed one still scanned",
              any("costBasis" in x and ":3:" in x for x in probs), True)

        # A whole-file JSON parse of a JSONL store would have reported a false
        # "invalid JSON" on a perfectly good file. Assert it does not.
        (root / "data" / "market" / "s.jsonl").write_text('{"low": 5}\n{"low": 6}\n')
        check("valid jsonl is not reported as invalid",
              any("invalid JSON" in x for x in structural(root)), False)

        # tests/ is covered - fixtures are captured API responses and can carry anything.
        (root / "tests" / "fx.json").write_text('{"faceValuePerSeat": 92}')
        check("tests/ is scanned", any("faceValuePerSeat" in x for x in structural(root)), True)
        (root / "tests" / "fx.json").unlink()

    # --- history pass, against a REAL temp git repo ---
    if shutil.which("git") is None:
        print("  (skipped history tests: no git)")
    else:
        with tempfile.TemporaryDirectory() as d:
            root = pathlib.Path(d)
            pats = root / ".patterns"
            pats.write_text("SEKRET-VALUE\nOTHER-SEKRET\n")
            env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                   "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}

            def git(*a):
                subprocess.run(["git", "-C", str(root), *a], check=True,
                               capture_output=True, env={**os.environ, **env})

            git("init", "-q")
            (root / "f.txt").write_text("nothing sensitive\n")
            git("add", "f.txt")
            git("commit", "-q", "-m", "clean commit")
            probs, ran = history(root, pats, root / ".none")
            check("history ran", ran, True)
            check("clean history is clean", probs, [])

            # A private literal in FILE CONTENT, later removed. Removal must not hide it.
            (root / "f.txt").write_text("SEKRET-VALUE\n")
            git("add", "f.txt")
            git("commit", "-q", "-m", "add a secret")
            (root / "f.txt").write_text("scrubbed\n")
            git("add", "f.txt")
            git("commit", "-q", "-m", "scrub it")
            probs, _ = history(root, pats, root / ".none")
            check("secret in history found even after removal",
                  any("SEKRET-VALUE" in x and "file content" in x for x in probs), True)

            # A private literal in a COMMIT MESSAGE only. This is the case the doubled-NUL
            # bug mis-attributed.
            (root / "g.txt").write_text("harmless\n")
            git("add", "g.txt")
            git("commit", "-q", "-m", "sold for OTHER-SEKRET dollars")
            probs, _ = history(root, pats, root / ".none")
            msg = [x for x in probs if "commit message" in x and "OTHER-SEKRET" in x]
            check("secret in a commit message found", len(msg), 1)

            # Attribution: the reported SHA must be the commit that actually carries it.
            want = subprocess.run(["git", "-C", str(root), "rev-parse", "--short=9", "HEAD"],
                                  capture_output=True, text=True).stdout.strip()
            check("commit-message finding attributed to the right commit",
                  want in msg[0], True)

            # Accepting a SHA must silence it without silencing others.
            acc = root / ".accepted"
            acc.write_text(want + "\n")
            probs, _ = history(root, pats, acc)
            check("accepted sha no longer fatal",
                  any("commit message" in x and "OTHER-SEKRET" in x for x in probs), False)
            check("accepting one finding leaves the other",
                  any("SEKRET-VALUE" in x for x in probs), True)

            # No patterns file at all must SKIP, not pass.
            probs, ran = history(root, root / ".missing", acc)
            check("absent patterns file skips rather than passes", (probs, ran), ([], False))

    for f in fails:
        print(f"  FAIL {f}", file=sys.stderr)
    print(f"self-test: {'FAILED' if fails else 'passed'} ({len(fails)} failure(s))")
    return 1 if fails else 0


def main() -> int:
    if "--self-test" in sys.argv:
        return self_test()
    problems = structural()
    lit, ran_literal = literal()
    problems += lit
    hist, ran_history = history()

    print(f"structural check: {len(FORBIDDEN_KEYS)} forbidden keys across {'/ '.join(scanned_dirs())}/ (.json + .jsonl, recursive)")
    if ran_literal:
        print(f"literal check:    dist/ + tracked files scanned against {PATTERNS_FILE.name}")
    else:
        print(f"literal check:    SKIPPED (no {PATTERNS_FILE.name}; structural check only)")

    if ran_history:
        n = len(hist)
        state = "CLEAN" if n == 0 else f"{n} finding(s)"
        vis = {True: "PUBLIC", False: "private", None: "visibility unknown"}[REMOTE_PUBLIC]
        mode = "fatal" if HISTORY_FATAL else "advisory"
        print(f"history check:    git log content + messages -> {state}  [remote {vis}, {mode}]")
        if hist and not HISTORY_FATAL:
            print("                  history must be clean BEFORE this repo is made public")
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
