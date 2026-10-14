#!/usr/bin/env python3
"""Exercise the collector workflow's failure and cancellation order (ops#415)."""

import pathlib
import os
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
RESOLVE = "Re-resolve event ids"
SETUP = ("actions/checkout@v7", "actions/setup-python@v7")
REQUIRED = ("Self-test", "Re-resolve event ids", *COLLECTORS, SUMMARY,
            CROSS_CHECK, COMMIT, GAPS, GUARD)


def steps(source: str) -> list[tuple[str, str]]:
    """Split job steps at their indentation, including unnamed setup steps."""
    matches = list(re.finditer(r"(?m)^      - (name|uses|run): (.+)$", source))
    return [(m.group(2) if m.group(1) != "run" else f"unnamed run {i}",
             (f"\n        run: {m.group(2)}" if m.group(1) == "run" else "") +
             source[m.end():matches[i + 1].start() if i + 1 < len(matches) else len(source)])
            for i, m in enumerate(matches)]


def field(block: str, key: str) -> str | None:
    match = re.search(rf"(?m)^        {re.escape(key)}: (.+)$", block)
    return match.group(1).strip() if match else None


def fatal_failure(block: str) -> bool:
    """Only an absent or literal false mask leaves a step's failure fatal.

    Expressions can evaluate to true at runtime. Reject those and unknown YAML
    spellings rather than predicting their value from the workflow text.
    """
    values = re.findall(r"(?m)^        continue-on-error:[ \t]*(.*)$", block)
    return len(values) <= 1 and all(value.strip() in ("false", "'false'", '"false"') for value in values)


def run_commands(block: str) -> tuple[str, ...]:
    matches = re.findall(r"(?m)^        run: (.*)$", block)
    if len(matches) != 1:
        return ()
    return (matches[0],) if matches[0] != "|" else tuple(re.findall(r"(?m)^          (.+)$", block))


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
             cancel_during: str | None = None,
             cancel_between: str | None = None) -> tuple[str, set[str]]:
    status = "success"
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
            outcomes.get(src.lower()) == "failure" for src in SOURCES + ("Resolve",)))
        outcome = "cancelled" if name == cancel_during else "failure" if refused else "success"
        if ident:
            outcomes[ident] = outcome
        if refused and field(block, "continue-on-error") != "true" and status != "cancelled":
            status = "failure"
        if name == cancel_during:
            status = "cancelled"
        if name == cancel_between:
            status = "cancelled"
    return status, ran


def commit_behavior(block: str) -> bool:
    """Execute the Commit body with a recording git; a failed push must stay fatal."""
    if "        run: |\n" not in block:
        return False
    body = block.split("        run: |\n", 1)[1]
    lines = []
    for line in body.splitlines():
        if line and not line.startswith("          "):
            break
        lines.append(line[10:])
    script = "\n".join(lines) + "\n"
    with tempfile.TemporaryDirectory() as temp:
        tmp = pathlib.Path(temp)
        fake_git = tmp / "git"
        fake_git.write_text("#!/bin/bash\n"
                            "printf '%s\\n' \"$*\" >> \"$GIT_LOG\"\n"
                            "if [[ \"$1\" == status ]]; then echo ' M data/market/x'; fi\n"
                            "if [[ \"$1\" == push && \"$FAIL_PUSH\" == 1 ]]; then exit 1; fi\n")
        fake_git.chmod(0o755)
        env = os.environ.copy()
        env.update(PATH=f"{temp}:{env['PATH']}", GIT_LOG=str(tmp / "git.log"))
        for fail_push in ("0", "1"):
            (tmp / "git.log").write_text("")
            env["FAIL_PUSH"] = fail_push
            result = subprocess.run(["bash", "-e", "-o", "pipefail", "-c", script],
                                    env=env, capture_output=True, text=True)
            calls = (tmp / "git.log").read_text().splitlines()
            expected = ("config user.name github-actions[bot]",
                        "config user.email github-actions[bot]@users.noreply.github.com",
                        "status --porcelain data/market/ data/tickpick_events.json",
                        "add data/market/ data/tickpick_events.json")
            if (calls[:4] != list(expected) or len(calls) != 7 or
                    not calls[4].startswith("commit -m chore: tickpick prices ") or
                    calls[5:] != ["pull --rebase --autostash", "push"] or
                    (result.returncode == 0) == (fail_push == "1")):
                return False
    return True


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
    require(names[:2] == list(SETUP), "checkout and Python setup precede Self-test")
    for name in SETUP:
        require(names.count(name) == 1 and (name not in blocks or fatal_failure(blocks[name])),
                f"{name} failure stays fatal")
    require(max(names.index(name) for name in COLLECTORS) < names.index(SUMMARY) <
            names.index(CROSS_CHECK) < names.index(COMMIT) < names.index(GAPS) <
            names.index(GUARD), "all collection and derivation precede commit and guard")
    require(names[-1] == GUARD, "refusal guard is final")
    require(field(blocks["Self-test"], "id") == "selftest", "Self-test outcome id")
    require(field(blocks["Re-resolve event ids"], "id") == "resolve", "Re-resolve outcome id")
    require(fatal_failure(blocks["Self-test"]), "Self-test failure stays fatal")
    require(field(blocks[RESOLVE], "continue-on-error") == "true", "resolver refusal allows other sources")
    for name, commands in {
        "Self-test": ("python3 scripts/market_store.py --self-test",
                      "python3 scripts/collect_tickpick.py --self-test",
                      "python3 scripts/collect_gametime.py --self-test",
                      "python3 scripts/collect_ticketnetwork.py --self-test",
                      "python3 scripts/collect_scorebig.py --self-test",
                      "python3 scripts/summarize_market.py --self-test"),
        RESOLVE: ("python3 scripts/collect_tickpick.py --resolve",),
        SUMMARY: ("python3 scripts/summarize_market.py",),
        CROSS_CHECK: ("python3 scripts/check_tier_market.py",),
    }.items():
        require(run_commands(blocks[name]) == commands, f"{name} required commands")
    for name in SOURCES:
        block = blocks["Collect " + name]
        require(field(block, "id") == name.lower(), f"{name} outcome id")
        require(field(block, "continue-on-error") == "true", f"{name} failure captured")
        require(run_commands(block) == (f"python3 scripts/collect_{name.lower()}.py",),
                f"{name} required command")
    for name in (SUMMARY, CROSS_CHECK, COMMIT, GAPS):
        require(fatal_failure(blocks[name]), f"{name} failure stays red")
    require(run_commands(blocks[GAPS]) == ("python3 scripts/check_data_freshness.py --strict",),
            "gap check required command")
    for name, block in ordered:
        if name != COMMIT:
            require(not re.search(r"\bgit\s+(?:push|commit)\b", "\n".join(run_commands(block))),
                    f"{name} cannot commit or push")
    require(commit_behavior(blocks[COMMIT]), "aggregate commit stages, commits and pushes without masking failure")
    commit_script = "\n".join(run_commands(blocks[COMMIT]))
    require(sum(line.strip().startswith("git pull ") for line in commit_script.splitlines()) == 1 and
            any(line.strip() == "git pull --rebase --autostash" for line in commit_script.splitlines()),
            "rebase pull stays fatal")
    guard = blocks[GUARD]
    for name in ("Resolve",) + SOURCES:
        require(bool(re.search(rf"(?m)^          {name.upper()}: \$\{{\{{ steps\.{name.lower()}\.outcome \}}\}}$", guard)),
                f"{name} raw outcome captured")
    require("for src in RESOLVE TICKPICK GAMETIME TICKETNETWORK SCOREBIG; do" in guard,
            "resolver and all sources checked")
    require('if [[ "$outcome" == "failure" ]]; then' in guard and "exit $bad" in guard,
            "refusal makes job red")

    normal = set(COLLECTORS + (SUMMARY, CROSS_CHECK, COMMIT, GAPS, GUARD))
    scenarios = (
        ("all succeed", {}, "success", normal),
        ("TickPick refuses", {"fail": "Collect TickPick"}, "failure", normal),
        ("a secondary refuses", {"fail": "Collect Gametime"}, "failure", normal),
        ("Self-test fails", {"fail": "Self-test"}, "failure", {GAPS, GUARD}),
        ("Re-resolve fails", {"fail": RESOLVE}, "failure", set(COLLECTORS[1:]) | {SUMMARY, CROSS_CHECK, COMMIT, GAPS, GUARD}),
        ("checkout fails", {"fail": SETUP[0]}, "failure", {GAPS, GUARD}),
        ("setup-python fails", {"fail": SETUP[1]}, "failure", {GAPS, GUARD}),
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
    protected = set(COLLECTORS + (SUMMARY, CROSS_CHECK, COMMIT, GAPS))
    for name in names[:names.index(COMMIT)]:
        later = protected & set(names[names.index(name) + 1:])
        for kind in ("cancel_during", "cancel_between"):
            try:
                status, ran = simulate(source, **{kind: name})
                require(status == "cancelled" and not (ran & later) and GUARD in ran,
                        f"{kind} {name}: later steps ran={sorted(ran & later)}, guard={GUARD in ran}")
            except (ValueError, IndexError) as exc:
                errors.append(f"{kind} {name}: {exc}")
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

    def mask(text: str, step: str, value: str) -> str:
        anchor = f"      - {'uses' if step in SETUP else 'name'}: {step}\n"
        return text.replace(anchor, anchor + f"        continue-on-error: {value}\n", 1)

    mutations = (
        ("TickPick after resolver failure", change_condition(source, COLLECTORS[0], "always()")),
        ("secondary after Self-test failure or cancellation", change_condition(source, COLLECTORS[1], "always()")),
        ("commit after Self-test failure or cancellation", change_condition(source, COMMIT, "always()")),
        ("commit after cancellation with !failure()", change_condition(
            source, COMMIT, "${{ !failure() && steps.selftest.outcome == 'success' }}")),
        ("checkout failure masked", source.replace("      - uses: actions/checkout@v7\n",
                                                    "      - uses: actions/checkout@v7\n        continue-on-error: true\n", 1)),
        ("Python setup failure masked", source.replace("      - uses: actions/setup-python@v7\n",
                                                        "      - uses: actions/setup-python@v7\n        continue-on-error: true\n", 1)),
        ("resolver bypassed", source.replace("run: python3 scripts/collect_tickpick.py --resolve",
                                               "run: true", 1)),
        ("Self-test bypassed", source.replace("python3 scripts/market_store.py --self-test",
                                                "true", 1)),
        ("Summarise bypassed", source.replace("run: python3 scripts/summarize_market.py",
                                                "run: true", 1)),
        ("ScoreBig after commit", move_after(source, COLLECTORS[3], COMMIT)),
        ("Summarise after commit", move_after(source, SUMMARY, COMMIT)),
        ("push removed", source.replace("            git push\n", "", 1)),
        ("push echoed", source.replace("            git push\n", "            echo git push\n", 1)),
        ("push failure masked", source.replace("            git push\n", "            git push || true\n", 1)),
        ("commit removed", source.replace('            git commit -m "chore: tickpick prices $(date -u +%Y-%m-%d)"\n', "", 1)),
        ("stage narrowed", source.replace("            git add data/market/ data/tickpick_events.json\n",
                                           "            git add data/market/tickpick.jsonl\n", 1)),
        ("commit failure masked", source.replace("      - name: Commit aggregates\n",
                                                  "      - name: Commit aggregates\n        continue-on-error: true\n", 1)),
        ("gap failure masked", source.replace("      - name: Check for gaps\n",
                                               "      - name: Check for gaps\n        continue-on-error: true\n", 1)),
        *((f"{step} expression mask {value}", mask(source, step, value))
          for step in (*SETUP, "Self-test", COMMIT, GAPS)
          for value in ("${{ true }}", "${{ 'true' }}", "${{ inputs.mask }}"))
    ) + (
        *((f"{step} quoted mask {value}", mask(source, step, value))
          for step in (*SETUP, "Self-test", COMMIT, GAPS)
          for value in ("'true'", '"true"')),
    ) + (
        *((f"{name} command masked", source.replace(f"run: python3 scripts/collect_{name.lower()}.py\n",
                                                  f"run: python3 scripts/collect_{name.lower()}.py || true\n", 1))
          for name in SOURCES),
    ) + (
        *((f"{name} command semicolon mask", source.replace(f"run: python3 scripts/collect_{name.lower()}.py\n",
                                                         f"run: python3 scripts/collect_{name.lower()}.py; true\n", 1))
          for name in SOURCES),
    ) + (
        *((f"{name} command bypassed", source.replace(f"run: python3 scripts/collect_{name.lower()}.py\n",
                                                    "run: true\n", 1))
          for name in SOURCES),
    ) + (
        ("gap strict removed", source.replace("run: python3 scripts/check_data_freshness.py --strict\n",
                                               "run: python3 scripts/check_data_freshness.py\n", 1)),
        ("gap failure masked in shell", source.replace("run: python3 scripts/check_data_freshness.py --strict\n",
                                                       "run: python3 scripts/check_data_freshness.py --strict || true\n", 1)),
        ("pull failure masked", source.replace("            git pull --rebase --autostash\n",
                                               "            git pull --rebase --autostash || true\n", 1)),
        ("extra push before commit", source.replace("      - name: Commit aggregates\n",
                                                    "      - name: Extra push\n        if: always()\n        run: git push\n\n      - name: Commit aggregates\n", 1)),
        ("extra commit before commit", source.replace("      - name: Commit aggregates\n",
                                                      "      - name: Extra commit\n        if: always()\n        run: git commit -am x\n\n      - name: Commit aggregates\n", 1)),
        ("unnamed push before commit", source.replace("      - name: Commit aggregates\n",
                                                     "      - run: git push\n\n      - name: Commit aggregates\n", 1)),
        ("guard disabled", change_condition(source, GUARD, "success()")),
    )
    for label, mutant in mutations:
        if mutant == source or not check(mutant):
            print(f"mutation survived: {label}", file=sys.stderr)
            return 1
    for step in (*SETUP, "Self-test", COMMIT, GAPS):
        for value in ("false", "'false'", '"false"'):
            if check(mask(source, step, value)):
                print(f"literal false rejected on {step}: {value}", file=sys.stderr)
                return 1
    guard = dict(steps(source))[GUARD]
    script = guard.split("        run: |\n", 1)[1]
    script = "\n".join(line[10:] for line in script.splitlines()) + "\n"
    with tempfile.TemporaryDirectory() as temp:
        for refused in (None, "Resolve", *SOURCES):
            env = {name.upper(): "failure" if name == refused else "success" for name in SOURCES}
            env["RESOLVE"] = "failure" if refused == "Resolve" else "success"
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
