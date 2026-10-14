#!/usr/bin/env python3
"""Exercise the collector workflow's failure and cancellation order (ops#415)."""

import pathlib
import re
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github/workflows/collect-tickpick.yml"
SOURCES = ("TickPick", "Gametime", "TicketNetwork", "ScoreBig")
COLLECTORS = tuple("Collect " + name for name in SOURCES)
SUMMARY = "Summarise"
CROSS_CHECK = "Cross-check tiers against the market"
COMMIT = "Commit aggregates"
GAPS = "Check for gaps"
GUARD = "Collectors must not refuse silently"
REQUIRED = ("Self-test", "Re-resolve event ids", *COLLECTORS, SUMMARY,
            CROSS_CHECK, COMMIT, GAPS, GUARD)


def steps(source: str) -> list[tuple[str, str]]:
    """Split job steps at their indentation, including unnamed setup steps."""
    matches = list(re.finditer(r"(?m)^      - (?:name|uses): (.+)$", source))
    return [(m.group(1), source[m.end():matches[i + 1].start() if i + 1 < len(matches) else len(source)])
            for i, m in enumerate(matches)]


def field(block: str, key: str) -> str | None:
    match = re.search(rf"(?m)^        {re.escape(key)}: (.+)$", block)
    return match.group(1).strip() if match else None


def condition(expr: str | None, status: str, outcomes: dict[str, str]) -> bool:
    """Evaluate the small Actions-expression subset used by this workflow.

    Actions adds an implicit success() when no status function appears. Reject unknown
    syntax rather than silently guessing how a changed workflow will execute.
    """
    expr = (expr or "success()").strip()
    if expr.startswith("${{") and expr.endswith("}}"):
        expr = expr[3:-2].strip()
    tokens = re.findall(r"\s*(&&|\|\||==|!|\(|\)|'(?:[^']*)'|\"(?:[^\"]*)\"|[A-Za-z_][\w.]*)", expr)
    if "".join(tokens).replace(" ", "") != re.sub(r"\s+", "", expr):
        raise ValueError(f"unsupported condition: {expr}")
    pos = 0

    def atom() -> bool | str:
        nonlocal pos
        if pos >= len(tokens):
            raise ValueError(f"incomplete condition: {expr}")
        token = tokens[pos]
        pos += 1
        if token == "!":
            return not bool(atom())
        if token == "(":
            value = either()
            if pos >= len(tokens) or tokens[pos] != ")":
                raise ValueError(f"unclosed condition: {expr}")
            pos += 1
            return value
        if token in ("success", "failure", "cancelled", "always"):
            if tokens[pos:pos + 2] != ["(", ")"]:
                raise ValueError(f"bad status function: {expr}")
            pos += 2
            return {"success": status == "success", "failure": status == "failure",
                    "cancelled": status == "cancelled", "always": True}[token]
        if token.startswith("steps.") and token.endswith(".outcome"):
            return outcomes.get(token.split(".")[1], "")
        if token.startswith(("'", '"')):
            return token[1:-1]
        raise ValueError(f"unsupported condition token: {token}")

    def comparison() -> bool:
        nonlocal pos
        left = atom()
        if pos < len(tokens) and tokens[pos] == "==":
            pos += 1
            return left == atom()
        return bool(left)

    def both() -> bool:
        nonlocal pos
        value = comparison()
        while pos < len(tokens) and tokens[pos] == "&&":
            pos += 1
            right = comparison()
            value = value and right
        return value

    def either() -> bool:
        nonlocal pos
        value = both()
        while pos < len(tokens) and tokens[pos] == "||":
            pos += 1
            right = both()
            value = value or right
        return value

    value = either()
    if pos != len(tokens):
        raise ValueError(f"trailing condition tokens: {expr}")
    has_status = bool(re.search(r"\b(?:success|failure|cancelled|always)\s*\(", expr))
    return value and (has_status or status == "success")


def simulate(source: str, *, fail: str | None = None,
             cancel_after: str | None = None, setup_failure: bool = False) -> tuple[str, set[str]]:
    status = "failure" if setup_failure else "success"
    outcomes: dict[str, str] = {}
    ran: set[str] = set()
    for name, block in steps(source):
        ident = field(block, "id")
        if not condition(field(block, "if"), status, outcomes):
            if ident:
                outcomes[ident] = "skipped"
            continue
        ran.add(name)
        refused = name == fail or (name == GUARD and any(
            outcomes.get(src.lower()) == "failure" for src in SOURCES))
        outcome = "failure" if refused else "success"
        if ident:
            outcomes[ident] = outcome
        if refused and field(block, "continue-on-error") != "true" and status != "cancelled":
            status = "failure"
        if name == cancel_after:
            status = "cancelled"
    return status, ran


def check(source: str) -> list[str]:
    ordered = steps(source)
    names = [name for name, _ in ordered]
    blocks = dict(ordered)
    errors: list[str] = []

    def require(ok: bool, message: str) -> None:
        if not ok:
            errors.append(message)

    require(all(names.count(name) == 1 for name in REQUIRED), "required steps present exactly once")
    if not all(names.count(name) == 1 for name in REQUIRED):
        return errors
    require(names.index("Self-test") < names.index("Re-resolve event ids") <
            min(names.index(name) for name in COLLECTORS), "pre-collection order")
    require(max(names.index(name) for name in COLLECTORS) < names.index(SUMMARY) <
            names.index(CROSS_CHECK) < names.index(COMMIT) < names.index(GAPS) <
            names.index(GUARD), "all collection and derivation precede commit and guard")
    require(names[-1] == GUARD, "refusal guard is final")
    require(field(blocks["Self-test"], "id") == "selftest", "Self-test outcome id")
    require(field(blocks["Re-resolve event ids"], "id") == "resolve", "Re-resolve outcome id")
    for name in SOURCES:
        block = blocks["Collect " + name]
        require(field(block, "id") == name.lower(), f"{name} outcome id")
        require(field(block, "continue-on-error") == "true", f"{name} failure captured")
    for name in (SUMMARY, CROSS_CHECK, COMMIT, GAPS):
        require(field(blocks[name], "continue-on-error") != "true", f"{name} failure stays red")
    require("git push" in blocks[COMMIT], "aggregate commit pushes")
    guard = blocks[GUARD]
    for name in SOURCES:
        require(bool(re.search(rf"(?m)^          {name.upper()}: \$\{{\{{ steps\.{name.lower()}\.outcome \}}\}}$", guard)),
                f"{name} raw outcome captured")
    require("for src in TICKPICK GAMETIME TICKETNETWORK SCOREBIG; do" in guard,
            "all sources checked")
    require('if [[ "$outcome" == "failure" ]]; then' in guard and "exit $bad" in guard,
            "refusal makes job red")

    normal = set(COLLECTORS + (SUMMARY, CROSS_CHECK, COMMIT, GAPS, GUARD))
    scenarios = (
        ("all succeed", {}, "success", normal),
        ("TickPick refuses", {"fail": "Collect TickPick"}, "failure", normal),
        ("a secondary refuses", {"fail": "Collect Gametime"}, "failure", normal),
        ("Self-test fails", {"fail": "Self-test"}, "failure", {GAPS, GUARD}),
        ("Re-resolve fails", {"fail": "Re-resolve event ids"}, "failure", {GAPS, GUARD}),
        ("setup fails", {"setup_failure": True}, "failure", {GAPS, GUARD}),
        ("cancelled", {"cancel_after": "Self-test"}, "cancelled", {GUARD}),
        ("Summarise fails", {"fail": SUMMARY}, "failure", set(COLLECTORS) | {SUMMARY, GAPS, GUARD}),
        ("Cross-check fails", {"fail": CROSS_CHECK}, "failure", set(COLLECTORS) | {SUMMARY, CROSS_CHECK, GAPS, GUARD}),
    )
    for label, kwargs, expected_status, expected in scenarios:
        try:
            status, ran = simulate(source, **kwargs)
            actual = ran & normal
            require(status == expected_status and actual == expected,
                    f"{label}: {status}, ran={sorted(actual)}")
        except (ValueError, IndexError) as exc:
            errors.append(f"{label}: {exc}")
    return errors


def self_test(source: str) -> int:
    errors = check(source)
    if errors:
        print("baseline workflow invalid: " + ", ".join(errors), file=sys.stderr)
        return 1
    def change_condition(text: str, name: str, replacement: str) -> str:
        block = dict(steps(text))[name]
        current = field(block, "if")
        if current is None:
            return text.replace(f"      - name: {name}\n",
                                f"      - name: {name}\n        if: {replacement}\n", 1)
        else:
            changed = block.replace(f"        if: {current}\n", f"        if: {replacement}\n", 1)
        return text.replace(block, changed, 1)

    def move_after(text: str, name: str, after: str) -> str:
        start = text.index(f"      - name: {name}\n")
        end = text.find("      - name:", start + 10)
        block = text[start:end]
        text = text[:start] + text[end:]
        pos = text.index(f"      - name: {after}\n")
        end = text.find("      - name:", pos + 10)
        return text[:end] + block + text[end:]

    mutations = (
        ("TickPick after resolver failure", change_condition(source, COLLECTORS[0], "always()")),
        ("secondary after Self-test failure or cancellation", change_condition(source, COLLECTORS[1], "always()")),
        ("commit after Self-test failure or cancellation", change_condition(source, COMMIT, "always()")),
        ("ScoreBig after commit", move_after(source, COLLECTORS[3], COMMIT)),
        ("Summarise after commit", move_after(source, SUMMARY, COMMIT)),
        ("push removed", source.replace("            git push\n", "", 1)),
        ("commit failure masked", source.replace("      - name: Commit aggregates\n",
                                                  "      - name: Commit aggregates\n        continue-on-error: true\n", 1)),
        ("gap failure masked", source.replace("      - name: Check for gaps\n",
                                               "      - name: Check for gaps\n        continue-on-error: true\n", 1)),
        ("guard disabled", change_condition(source, GUARD, "success()")),
    )
    for label, mutant in mutations:
        if mutant == source or not check(mutant):
            print(f"mutation survived: {label}", file=sys.stderr)
            return 1
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
