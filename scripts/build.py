#!/usr/bin/env python3
"""Assemble dist/ from the current sources, THEN run the gates over it (ops#374).

`npm run build` used to be a one-line chain that ran the privacy and read-only checks
FIRST and copied the site into dist/ afterwards. Two consequences, both measured:

  - On a fresh worktree that has `.private-patterns`, the literal pass found no dist/
    and failed with "dist/ not built - run `npm run build` first" - which is the command
    that had just failed. Every new worktree hit it and was hand-built around.
  - On every other run, the dist/ pass scanned the PREVIOUS build's output. The build
    that introduced a change was never the build whose output got checked; a stale
    dist/ holding a value since removed from source failed a clean build instead.

So the order here is: clear dist/, copy this build's files in, then run the gates
against what was just produced. A gate failure removes dist/ again, so a red build
leaves no output that could be previewed or uploaded as if it had passed.

Why a script rather than a reordered package.json one-liner: the ordering IS the fix,
and a shell chain cannot be tested. This file self-tests the ordering against stub
gates, so moving the copy back after the checks turns the suite red. The gates
themselves are untouched - check_privacy.py is in the protected set (CLAUDE.md rule 5).

The file lists below are deliberately the same ones .github/workflows/deploy.yml
assembles for Pages. That workflow does not call this script; if the served set
changes, change both.
"""

import contextlib
import io
import json
import pathlib
import shutil
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent

SITE_FILES = ["index.html", "styles.css", "app.js"]
DATA_FILES = ["data/schedule.json", "data/outcomes.json"]
# Order matters only for which failure is reported first; both must pass.
GATES = ["scripts/check_privacy.py", "scripts/check_readonly.py"]


def assemble(root: pathlib.Path) -> pathlib.Path:
    dist = root / "dist"
    # Clear first. Copying over an existing dist/ would leave files from an earlier
    # build in place, and the literal pass would scan them as if this build made them.
    if dist.exists():
        shutil.rmtree(dist)
    (dist / "data").mkdir(parents=True)
    for rel in SITE_FILES:
        shutil.copy2(root / rel, dist / pathlib.Path(rel).name)
    for rel in DATA_FILES:
        shutil.copy2(root / rel, dist / "data" / pathlib.Path(rel).name)
    return dist


def build(root: pathlib.Path = ROOT) -> int:
    dist = root / "dist"
    try:
        assemble(root)
    except OSError as e:
        # A missing source must not leave a half-built dist/ behind, nor fall through
        # to the gates, which would then scan an incomplete site and could pass it.
        shutil.rmtree(dist, ignore_errors=True)
        print(f"build: could not assemble dist/: {e}", file=sys.stderr)
        return 1

    for gate in GATES:
        r = subprocess.run([sys.executable, str(root / gate)], cwd=str(root))
        if r.returncode != 0:
            shutil.rmtree(dist, ignore_errors=True)
            print(f"build: {gate} failed (exit {r.returncode}) - dist/ removed",
                  file=sys.stderr)
            return r.returncode or 1
    print("build: dist/ assembled and passed every gate")
    return 0


# Stub gate for the self-test. It records what it saw in dist/ at the moment it ran,
# which is the only way to observe ORDERING from outside: the real gates' verdicts
# would be identical either way whenever dist/ happens to be current.
STUB_GATE = '''
import json, pathlib, sys
root = pathlib.Path(__file__).resolve().parent.parent
dist = root / "dist"
name = pathlib.Path(__file__).stem
seen = {
    "dist_exists": dist.exists(),
    "files": sorted(str(p.relative_to(dist)) for p in dist.rglob("*") if p.is_file())
             if dist.exists() else [],
    "index": (dist / "index.html").read_text() if (dist / "index.html").exists() else None,
}
(root / f"seen_{name}.json").write_text(json.dumps(seen))
rc_file = root / f"rc_{name}"
sys.exit(int(rc_file.read_text()) if rc_file.exists() else 0)
'''


# Named literally rather than unpacked from GATES, for the same reason as expected_files.
PRIVACY = "scripts/check_privacy.py"
READONLY = "scripts/check_readonly.py"


def self_test() -> int:
    fails = []

    def check(name, got, want):
        if got != want:
            fails.append(f"{name}: got {got!r}, want {want!r}")

    def fixture(tmp: pathlib.Path) -> pathlib.Path:
        root = tmp / "repo"
        (root / "scripts").mkdir(parents=True)
        (root / "data").mkdir()
        for rel in SITE_FILES + DATA_FILES:
            (root / rel).write_text(f"source:{rel}\n")
        # Both stubs exist whatever GATES says, so a gate dropped from GATES shows up
        # as a stub that never ran rather than as a crash in the fixture.
        for gate in (PRIVACY, READONLY):
            (root / gate).write_text(STUB_GATE)
        return root

    def build(root):
        # build()'s own progress lines are expected noise here; swallowed so the
        # runner's one-line summary is this self-test's verdict, not a build message.
        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            return _build(root)

    def seen(root, gate):
        p = root / f"seen_{pathlib.Path(gate).stem}.json"
        return json.loads(p.read_text()) if p.exists() else None

    privacy, readonly = PRIVACY, READONLY
    # Spelled out, not derived from SITE_FILES/DATA_FILES: an expectation computed from
    # the constant under test cannot notice that constant losing a file.
    expected_files = ["app.js", "data/outcomes.json", "data/schedule.json",
                      "index.html", "styles.css"]

    # 1. Fresh tree, no dist/: the case that used to fail on the circular message.
    with tempfile.TemporaryDirectory() as t:
        root = fixture(pathlib.Path(t))
        rc = build(root)
        check("fresh tree builds green", rc, 0)
        s = seen(root, privacy)
        check("privacy gate sees dist/ already built", s and s["dist_exists"], True)
        check("privacy gate sees THIS build's index.html", s and s["index"],
              "source:index.html\n")
        check("privacy gate sees every served file", s and s["files"], expected_files)
        check("readonly gate also runs after assembly",
              (seen(root, readonly) or {}).get("dist_exists"), True)
        check("green build leaves dist/ in place", (root / "dist/index.html").exists(), True)

    # 2. Stale dist/ from a previous build: the gate must see the new source, not the
    #    old output, and nothing left over from the earlier build.
    with tempfile.TemporaryDirectory() as t:
        root = fixture(pathlib.Path(t))
        (root / "dist/data").mkdir(parents=True)
        (root / "dist/index.html").write_text("previous build\n")
        (root / "dist/leftover.txt").write_text("previous build\n")
        build(root)
        s = seen(root, privacy) or {}
        check("stale index.html replaced before the gate runs", s.get("index"),
              "source:index.html\n")
        check("stale file from a previous build is gone when the gate runs",
              "leftover.txt" in s.get("files", []), False)

    # 3. Privacy gate fails: build fails, nothing left to serve, later gates skipped.
    with tempfile.TemporaryDirectory() as t:
        root = fixture(pathlib.Path(t))
        (root / "rc_check_privacy").write_text("1")
        rc = build(root)
        check("privacy failure fails the build", rc != 0, True)
        check("privacy failure leaves no dist/", (root / "dist").exists(), False)

    # 4. Read-only gate fails on its own: same contract, and proves it is in GATES.
    with tempfile.TemporaryDirectory() as t:
        root = fixture(pathlib.Path(t))
        (root / "rc_check_readonly").write_text("1")
        rc = build(root)
        check("readonly failure fails the build", rc != 0, True)
        check("readonly failure leaves no dist/", (root / "dist").exists(), False)

    # 5. A missing source fails before any gate runs and leaves nothing half-built.
    with tempfile.TemporaryDirectory() as t:
        root = fixture(pathlib.Path(t))
        (root / "app.js").unlink()
        rc = build(root)
        check("missing source fails the build", rc != 0, True)
        check("missing source leaves no partial dist/", (root / "dist").exists(), False)
        check("missing source does not reach the gates", seen(root, privacy), None)

    for f in fails:
        print(f"  FAIL {f}", file=sys.stderr)
    print(f"self-test: {'FAILED' if fails else 'passed'} ({len(fails)} failure(s))")
    return 1 if fails else 0


_build = build

if __name__ == "__main__":
    if "--self-test" in sys.argv:
        sys.exit(self_test())
    sys.exit(build())
