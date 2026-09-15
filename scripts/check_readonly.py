#!/usr/bin/env python3
"""Static guard for the read-only dashboard (ops#165).

The site is display-only: Rumi (chat -> git) is the input interface. This script
fails if any write path or input surface reappears in the served site files:

  - localStorage / sessionStorage / document.cookie writes
  - form elements: <input>, <select>, <textarea>, <form>
  - URL-fragment profile import (location.hash reads)
  - backup import/export affordances (file download/upload of state)

It also spot-checks that data/outcomes.json renders the timeline: 44 games,
known statuses, and at least one `listed` game with its market fields.
"""

import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
SITE_FILES = [ROOT / "index.html", ROOT / "app.js", ROOT / "styles.css"]
OUTCOMES = ROOT / "data" / "outcomes.json"

WRITE_PATTERNS = [
    (r"localStorage", "localStorage use"),
    (r"sessionStorage", "sessionStorage use"),
    (r"document\.cookie\s*=", "cookie write"),
    (r"location\.hash", "URL-fragment read (profile import surface)"),
]

FORM_PATTERNS = [
    (r"<input[\s>]", "<input> element"),
    (r"<select[\s>]", "<select> element"),
    (r"<textarea[\s>]", "<textarea> element"),
    (r"<form[\s>]", "<form> element"),
    (r'type\s*=\s*["\']file["\']', "file input (backup import surface)"),
]

ALLOWED_STATUSES = {"undecided", "attending", "listed", "sold", "exchanged"}


def check_site(files=None):
    problems = []
    files = files if files is not None else SITE_FILES
    for f in files:
        if not f.is_file():
            problems.append(f"{f.name}: missing - the site is incomplete")
            continue
        text = f.read_text(errors="ignore")
        for pat, label in WRITE_PATTERNS + FORM_PATTERNS:
            if re.search(pat, text):
                problems.append(f"{f.name}: {label} found - write paths are not allowed")
    return problems


def check_outcomes(path=None):
    problems = []
    path = path or OUTCOMES
    if not path.is_file():
        return [f"{path.name}: missing - dashboard has no data"]
    try:
        doc = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        return [f"{path.name}: invalid JSON ({e})"]
    games = doc.get("games")
    if not isinstance(games, list) or len(games) != 44:
        problems.append(f"{path.name}: expected 44 games, found "
                        f"{len(games) if isinstance(games, list) else 'non-list'}")
        return problems
    for g in games:
        if g.get("status") not in ALLOWED_STATUSES:
            problems.append(f"{path.name}: game {g.get('date')}: bad status {g.get('status')!r}")
        for k in ("marketMedian", "marketCount"):
            if k not in g:
                problems.append(f"{path.name}: game {g.get('date')}: missing {k}")
    listed = [g for g in games if g["status"] == "listed"]
    if not listed:
        problems.append(f"{path.name}: no `listed` game - spot-check needs one")
    return problems


# ------------------------------------------------------------------ self-test

def self_test():
    import tempfile
    fails = []

    def check(label, got, want=True):
        if bool(got) != bool(want):
            fails.append(f"{label}: got {got!r}")

    with tempfile.TemporaryDirectory() as d:
        root = pathlib.Path(d)
        good_js = root / "app.js"
        good_js.write_text("// renders only\nfetch('data/outcomes.json').then(r=>r.json());\n")
        check("clean js passes", check_site([good_js]), [])

        bad = root / "bad.js"
        bad.write_text("localStorage.setItem('profile', x);")
        check("localStorage caught", check_site([bad]))

        bad2 = root / "bad.html"
        bad2.write_text('<form><input type="text" name="list"></form>')
        check("form elements caught", check_site([bad2]))

        bad3 = root / "bad2.js"
        bad3.write_text("const p = JSON.parse(atob(location.hash.slice(1)));")
        check("hash import caught", check_site([bad3]))

        missing = root / "nope.js"
        check("missing file flagged", check_site([missing]))

        # outcomes spot-check
        ok = {"games": [
            {"date": f"2026-10-{i:02d}", "status": "listed" if i == 1 else "undecided",
             "marketMedian": 80 if i == 1 else None, "marketCount": 3 if i == 1 else 0}
            for i in range(1, 45)
        ]}
        oj = root / "outcomes.json"
        oj.write_text(json.dumps(ok))
        check("valid outcomes passes", check_outcomes(oj), [])

        bad_status = json.loads(oj.read_text())
        bad_status["games"][0]["status"] = "maybe"
        oj.write_text(json.dumps(bad_status))
        check("bad status caught", check_outcomes(oj))

    # the real tree, right now
    real = check_site() + check_outcomes()
    check("real site tree passes", real, [])
    if real:
        for p in real:
            print(f"  TREE {p}", file=sys.stderr)

    for f in fails:
        print(f"  FAIL {f}", file=sys.stderr)
    print(f"self-test: {'FAILED' if fails else 'passed'} ({len(fails)} failure(s))")
    return 1 if fails else 0


def main():
    if "--self-test" in sys.argv:
        return self_test()
    problems = check_site() + check_outcomes()
    if problems:
        print(f"{len(problems)} READ-ONLY VIOLATION(S):", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return 1
    print("clean - site is read-only, outcomes feed renders")
    return 0


if __name__ == "__main__":
    sys.exit(main())
