#!/usr/bin/env python3
"""Pin the collector workflow's data-preserving failure order (ops#415)."""

import pathlib
import re
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github/workflows/collect-tickpick.yml"
SOURCES = ("TickPick", "Gametime", "TicketNetwork", "ScoreBig")
DOWNSTREAM = ("Summarise", "Cross-check tiers against the market",
              "Commit aggregates", "Check for gaps")
GUARD = "Collectors must not refuse silently"


def steps(source: str) -> list[tuple[str, str]]:
    """Extract job steps; top-level six-space step markers cannot occur in run blocks."""
    matches = list(re.finditer(r"(?m)^      - name: (.+)$", source))
    return [(m.group(1), source[m.end():matches[i + 1].start() if i + 1 < len(matches) else len(source)])
            for i, m in enumerate(matches)]


def check(source: str) -> list[str]:
    ordered = steps(source)
    blocks = dict(ordered)
    errors = []

    def require(ok: bool, message: str) -> None:
        if not ok:
            errors.append(message)

    for name in SOURCES:
        block = blocks.get("Collect " + name, "")
        ident = name.lower()
        require(bool(re.search(rf"(?m)^        id: {ident}$", block)), f"{name} outcome id")
        require("        if: always()\n" in block, f"{name} independence")
        require("        continue-on-error: true\n" in block, f"{name} preserved failure")
    for name in DOWNSTREAM:
        block = blocks.get(name, "")
        require("        if: always()\n" in block, f"{name} after refusal")
    names = [name for name, _ in ordered]
    require(all(name in names for name in DOWNSTREAM + (GUARD,)), "required steps present")
    if all(name in names for name in DOWNSTREAM + (GUARD,)):
        require(names.index("Commit aggregates") < names.index("Check for gaps") < names.index(GUARD),
                "commit before diagnostic failure")
        require(names[-1] == GUARD, "refusal guard is final")
    guard = blocks.get(GUARD, "")
    require("        if: always()\n" in guard, "guard runs after other failures")
    for name in SOURCES:
        ident = name.upper()
        require(f"          {ident}: ${{{{ steps.{name.lower()}.outcome }}}}" in guard,
                f"{name} raw outcome captured")
    require("for src in TICKPICK GAMETIME TICKETNETWORK SCOREBIG; do" in guard,
            "all sources checked")
    require('if [[ "$outcome" == "failure" ]]; then' in guard and "exit $bad" in guard,
            "refusal makes job red")
    return errors


def self_test(source: str) -> int:
    if check(source):
        print("baseline workflow invalid: " + ", ".join(check(source)), file=sys.stderr)
        return 1
    mutations = {}
    for name in SOURCES:
        prefix = f"      - name: Collect {name}\n        id: {name.lower()}\n"
        mutations[f"{name} independence"] = (prefix + "        if: always()\n", prefix)
        start = source.index(prefix)
        end = source.find("      - name:", start + len(prefix))
        block = source[start:end]
        mutations[f"{name} failure capture"] = (block, block.replace("        continue-on-error: true\n", ""))
        mutations[f"{name} outcome"] = (
            f"          {name.upper()}: ${{{{ steps.{name.lower()}.outcome }}}}\n", "")
    for name in DOWNSTREAM + (GUARD,):
        prefix = f"      - name: {name}\n"
        mutations[f"{name} after refusal"] = (prefix + "        if: always()\n", prefix)
    mutations["all sources checked"] = (
        "for src in TICKPICK GAMETIME TICKETNETWORK SCOREBIG; do",
        "for src in GAMETIME TICKETNETWORK SCOREBIG; do")
    mutations["final refusal failure"] = ("          exit $bad\n", "          exit 0\n")
    for label, (old, new) in mutations.items():
        if old not in source or not check(source.replace(old, new, 1)):
            print(f"mutation survived: {label}", file=sys.stderr)
            return 1
    # Execute the same shell body Actions runs: every individual refusal is red,
    # while a fully successful collection is green.
    guard = dict(steps(source))[GUARD]
    script = guard.split("        run: |\n", 1)[1]
    script = "\n".join(line[10:] for line in script.splitlines()) + "\n"
    with tempfile.TemporaryDirectory() as temp:
        for refused in (None, *SOURCES):
            env = {name.upper(): "failure" if name == refused else "success" for name in SOURCES}
            env["GITHUB_STEP_SUMMARY"] = str(pathlib.Path(temp) / "summary")
            result = subprocess.run(["bash", "-e", "-c", script], env=env,
                                    capture_output=True, text=True)
            if (result.returncode != 0) != (refused is not None):
                print(f"guard verdict wrong for {refused or 'all success'}", file=sys.stderr)
                return 1
    print(f"workflow check passes; {len(mutations)} mutations rejected")
    return 0


if __name__ == "__main__":
    content = WORKFLOW.read_text()
    if sys.argv[1:] == ["--self-test"]:
        sys.exit(self_test(content))
    errors = check(content)
    if errors:
        print("workflow check failed: " + ", ".join(errors), file=sys.stderr)
        sys.exit(1)
    print("workflow check passes")
