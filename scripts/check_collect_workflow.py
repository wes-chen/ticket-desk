#!/usr/bin/env python3
"""Exercise the collector workflow's failure and cancellation order (ops#415).

The checker reads the workflow through a deliberately small CANONICAL GRAMMAR and
fails closed on every line outside it. Four review rounds each found one more YAML
spelling (a quoted key, a space before a colon, a `|-` block, a step starting with
`- if:`, a flow mapping) that the earlier regex reader treated as absent while Actions
read it as a real key. Enumerating spellings cannot close that class, because YAML
has more of them than any list. So the rule is inverted: a line is either parsed
completely by `parse()` below, or the check fails naming the line.

The grammar, in full:

- The whole file is printable ASCII plus newlines. No tabs, CR, BOM or non-ASCII
  (YAML treats some of those as line breaks or indentation; Python splits differently).
- Everything above the steps is pinned line for line (`HEADER`, `JOB_HEADER`), with
  only the cron time free. That shuts out workflow-level `defaults` (a shell override
  for every step) and `env` (`BASH_ENV` runs code before every step), and job-level
  `continue-on-error`, `defaults`, `env` or `if`.
- Every following line, to the end of the file, is part of the steps section:
  a step starts at exactly `      - name: <scalar>` or `      - uses: <scalar>`; step
  keys sit at exactly eight spaces, from an allowlist, written `key: value`; `with:`
  and `env:` hold `key: value` children at exactly ten spaces; the only block scalar
  is `run: |`, whose body starts at exactly ten spaces. No `shell` or
  `working-directory` key exists in the grammar, so no step can carry one.
- A scalar is plain (no leading YAML indicator, no `#`, no `: `), or single- or
  double-quoted with no escapes. Anchors, aliases, merge keys, tags, flow
  collections, folded or chomped blocks and comments after a value are all outside it.

Anything the grammar accepts is a strict subset of YAML whose meaning is not in doubt,
so the semantic checks below run on the structure Actions will see. PyYAML would
read more spellings, but it is not installed in CI (`test.yml` installs nothing) and
adding it would be a new external dependency; a real parser would also still need
this checker to decide what the parsed values mean.

The cost is that an unusual but valid spelling is rejected. That is deliberate: the
fix is to write it canonically, which is one line, while a spelling read wrongly
silently disables a failure the workflow exists to surface.

`if:` conditions are not evaluated. Each step's condition must be exactly one string
from a pinned table (`PINNED_IF`); the scenario simulator looks up that string's meaning
rather than parsing it. A hand-written evaluator was tried and read Actions expressions
differently from Actions (case, type coercion, status calls inside literals); see the
comment above `CONDITIONS`.

What a text check cannot close: any step outside the pinned set is arbitrary shell.
The pinned steps carry exact commands, but a free-text step (Summary, upload, or a new
one) can hide a command (`g""it push`, a Python subprocess), rewrite a collector or
checker the pinned steps then run (`sed -i`), or reach the job environment by
indirection. The check keeps such steps away from the literal word `git` and from
`GITHUB_ENV` / `GITHUB_PATH` (which set env, including `BASH_ENV`, or PATH for every
later step), and refuses step `env`; it does not, and cannot, parse what the shell
will do. Review of an added step is the remaining guard.
"""

import dataclasses
import os
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
RESOLVE = "Re-resolve event ids"
SETUP = ("actions/checkout@v7", "actions/setup-python@v7")
REQUIRED = ("Self-test", "Re-resolve event ids", *COLLECTORS, SUMMARY,
            CROSS_CHECK, COMMIT, GAPS, GUARD)
# Only these steps may set continue-on-error, and only to a bare `true`; every other
# step's failure must stay fatal, so for them the key is absent or a bare `false`.
MASKABLE = (RESOLVE, *COLLECTORS)
# The actions this job uses today. Anything else (a third-party auto-commit action, a
# local `./` action, a docker image) could push or mask without a `git` in sight.
ACTIONS = frozenset({"actions/checkout@v7", "actions/setup-python@v7",
                     "actions/upload-artifact@v7"})
STEP_KEYS = frozenset({"name", "id", "if", "uses", "run", "with", "env",
                       "continue-on-error"})
# Everything above the steps, as regexes over its significant lines, in order. Pinned
# whole rather than parsed: nothing here needs to vary but the cron time, and a
# workflow-level `defaults`/`env` or a job-level `continue-on-error`/`defaults` would
# change what every step runs or whether a failure turns the job red.
HEADER = ("name: Collect market prices", "on:", "  schedule:",
          r'    - cron: "[0-9*/,-]+ [0-9*/,-]+ [0-9*/,-]+ [0-9*/,-]+ [0-9*/,-]+"',
          "  workflow_dispatch:", "permissions:", "  contents: write", "concurrency:",
          "  group: collect-tickpick", "  cancel-in-progress: false", "jobs:")
JOB_HEADER = ("  collect:", "    runs-on: ubuntu-latest", "    steps:")
GUARD_ENV = {name.upper(): f"${{{{ steps.{name.lower()}.outcome }}}}"
             for name in ("Resolve",) + SOURCES}
PLAIN_START = frozenset("-?:,[]{}#&*!|>'\"%@`")
BLOCK_INDENT = " " * 10


class GrammarError(ValueError):
    pass


@dataclasses.dataclass
class Step:
    name: str
    line: int
    text: str = ""
    raw: dict = dataclasses.field(default_factory=dict)       # values as written
    scalars: dict = dataclasses.field(default_factory=dict)   # quotes removed
    maps: dict = dataclasses.field(default_factory=dict)      # with / env children
    body: list = dataclasses.field(default_factory=list)      # `run: |` lines
    block: bool = False

    def field(self, key: str) -> str | None:
        return self.scalars.get(key)

    def script(self) -> str:
        """The run text exactly as the shell receives it (`|` keeps one final newline)."""
        if self.block:
            return "\n".join(self.body).rstrip("\n") + "\n"
        return self.scalars.get("run", "")

    def commands(self) -> tuple[str, ...]:
        return tuple(line for line in self.script().splitlines() if line.strip())


def scalar(value: str, number: int) -> str:
    """Decode one scalar of the canonical grammar, or refuse it."""
    if value.startswith('"'):
        if re.fullmatch(r'"[^"\\]*"', value):
            return value[1:-1]
    elif value.startswith("'"):
        if re.fullmatch(r"'[^']*'", value):
            return value[1:-1]
    elif (value and value == value.strip() and value[0] not in PLAIN_START and
          "#" not in value and ": " not in value and not value.endswith(":")):
        return value
    raise GrammarError(f"line {number}: value outside the canonical grammar: {value!r}")


def significant(line: str) -> bool:
    return bool(line.strip()) and not line.lstrip().startswith("#")


def parse(source: str) -> list[Step]:
    """Parse the workflow under the canonical grammar; raise naming the first bad line."""
    if not source.endswith("\n"):
        # YAML drops a block's final newline when the file has none; refuse rather
        # than track a difference only the last step could see.
        raise GrammarError("the file must end with a newline")
    lines = source.split("\n")[:-1]
    for number, line in enumerate(lines, 1):
        if re.search(r"[^\x20-\x7e]", line):
            raise GrammarError(f"line {number}: tab, CR or non-ASCII character")
    i = 0
    for expected in HEADER + JOB_HEADER:
        while i < len(lines) and not significant(lines[i]):
            i += 1
        if i >= len(lines) or not re.fullmatch(expected, lines[i]):
            raise GrammarError(f"line {i + 1}: workflow header differs from the pinned header")
        i += 1

    ordered: list[Step] = []
    step: Step | None = None
    parent: str | None = None      # `with` / `env` while their children may follow
    in_block = first_block = False

    def finish() -> None:
        if step is None:
            return
        if ("uses" in step.raw) == ("run" in step.raw):
            raise GrammarError(f"line {step.line}: {step.name}: exactly one of uses or run")
        for key, children in step.maps.items():
            if not children:
                raise GrammarError(f"line {step.line}: {step.name}: empty {key} is null in YAML")
        if "with" in step.maps and "uses" not in step.raw:
            raise GrammarError(f"line {step.line}: {step.name}: with requires uses")
        ordered.append(step)

    while i < len(lines):
        line, number = lines[i], i + 1
        i += 1
        if first_block:
            # YAML infers a block's indentation from its first line; pin it, and refuse
            # a leading blank line rather than reason about how it shifts that inference.
            if not re.match(BLOCK_INDENT + r"\S", line):
                raise GrammarError(f"line {number}: a run block starts at exactly ten spaces")
            first_block = False
        if in_block:
            if not line.strip() or line.startswith(BLOCK_INDENT):
                step.body.append(line[10:])
                step.text += line + "\n"
                continue
            in_block = False        # a less-indented line ends the block, as in YAML
        if not significant(line):
            if step is not None:
                step.text += line + "\n"
            continue
        match = re.fullmatch(r"      - (name|uses): (.*)", line)
        if match:
            finish()
            value = scalar(match[2], number)
            step = Step(name=value, line=number, text=line + "\n")
            step.raw[match[1]], step.scalars[match[1]] = match[2], value
            parent = None
            continue
        if step is None:
            raise GrammarError(f"line {number}: a step starts with '- name:' or '- uses:': {line}")
        step.text += line + "\n"
        match = re.fullmatch(r"        ([a-z][a-z-]*):(?: (.*))?", line)
        if match:
            key, value = match.groups()
            if key not in STEP_KEYS:
                raise GrammarError(f"line {number}: step key outside the grammar: {key}")
            if key in step.raw or key in step.maps:
                raise GrammarError(f"line {number}: duplicate step key: {key}")
            if key == "name" and "uses" in step.raw:
                raise GrammarError(f"line {number}: a '- uses:' step takes no name; start it with '- name:'")
            parent = None
            if key in ("with", "env"):
                if value is not None:
                    raise GrammarError(f"line {number}: {key} must be a block mapping")
                step.maps[key] = {}
                parent = key
            elif value is None:
                raise GrammarError(f"line {number}: {key} requires a value")
            elif key == "run" and value == "|":
                step.raw["run"] = "|"
                step.block = in_block = first_block = True
            else:
                step.raw[key], step.scalars[key] = value, scalar(value, number)
            continue
        match = re.fullmatch(BLOCK_INDENT + r"([A-Za-z_][A-Za-z0-9_-]*): (.*)", line)
        if match and parent is not None:
            if match[1] in step.maps[parent]:
                raise GrammarError(f"line {number}: duplicate {parent} key: {match[1]}")
            step.maps[parent][match[1]] = scalar(match[2], number)
            continue
        raise GrammarError(f"line {number}: outside the canonical steps grammar: {line}")
    if first_block:
        raise GrammarError("empty run block at end of file")
    finish()
    if not ordered:
        raise GrammarError("no steps")
    return ordered


def steps(source: str) -> list[tuple[str, Step]]:
    return [(step.name, step) for step in parse(source)]


# `if:` conditions are PINNED, not evaluated (ops#415, round six). An earlier version
# parsed Actions expressions with a hand-written evaluator, and review found three ways
# it read a condition differently from Actions: Actions compares strings ignoring case
# ('FAILURE' == 'failure'), coerces mismatched types to numbers (`success() == ''` is
# true whenever the status is not success), and decides whether to add the implicit
# success() from the parse tree, not from a regex that also matched inside a string
# literal (`${{ 'always()' }}`). Making the evaluator more faithful would only move the
# next disagreement somewhere else, so it is gone. Each condition the workflow uses is
# listed below with its meaning written in Python, and a step's `if:` must be EXACTLY
# one of the spellings allowed for that step. There is nothing to misread: an unlisted
# string is an error naming the step, before the simulator ever sees it.
#
# The only spellings beyond what the workflow writes today are the documented
# equivalents: an expression with or without `${{ }}`, and for a step that has no `if:`
# today, `success()`, which Actions adds when the key is absent. The comparison is on
# the decoded scalar - what Actions receives - so YAML quoting is free; the grammar
# above decodes quotes unambiguously. Every other spelling, including a respaced one
# Actions would read identically, is refused: one line to fix, against a gate that
# silently stops gating.
CONDITIONS = {
    "success()": lambda status, out: status == "success",
    "always()": lambda status, out: True,
    "!cancelled()": lambda status, out: status != "cancelled",
    "!cancelled() && steps.resolve.outcome == 'success'":
        lambda status, out: status != "cancelled" and out.get("resolve") == "success",
    "!cancelled() && success() && steps.selftest.outcome == 'success'":
        lambda status, out: status == "success" and out.get("selftest") == "success",
}
SPELLINGS = {spelling: expr for expr in CONDITIONS
             for spelling in (expr, f"${{{{ {expr} }}}}")}
_RUN_ON_SUCCESS = (None, "success()")
_GATED = "!cancelled() && success() && steps.selftest.outcome == 'success'"
# The meaning each pinned step's condition must have; None is the key absent.
PINNED_IF = {
    **{action: _RUN_ON_SUCCESS for action in SETUP},
    "Self-test": _RUN_ON_SUCCESS,
    RESOLVE: _RUN_ON_SUCCESS,
    COLLECTORS[0]: ("!cancelled() && steps.resolve.outcome == 'success'",),
    **{collector: _RUN_ON_SUCCESS for collector in COLLECTORS[1:]},
    SUMMARY: (_GATED,),
    CROSS_CHECK: (_GATED,),
    COMMIT: (_GATED,),
    GAPS: ("!cancelled()",),
    GUARD: ("always()",),
}
# Steps outside the pinned set (the artifact upload, the Summary, any new step) may run
# on success or always. Neither reads a step outcome, and such a step can neither mask
# its failure, nor reach git, nor touch the job environment (see check()).
FREE_IF = (None, "success()", "always()")


def allowed_if(step: Step) -> bool:
    """True iff the step's `if:` is absent or one pinned spelling allowed for it."""
    allowed = PINNED_IF.get(step.name, FREE_IF)
    raw = step.field("if")
    if raw is None:
        return None in allowed
    # Not `SPELLINGS.get(raw) in allowed`: an unlisted string would map to None, and
    # None means "key absent", which most steps allow.
    return raw in SPELLINGS and SPELLINGS[raw] in allowed


def condition(expr: str | None, status: str, outcomes: dict[str, str]) -> bool:
    """Look up a pinned condition's meaning; raise on anything unlisted.

    A table lookup, not a parser: `check()` refuses an unlisted `if:` before simulating,
    so the raise is a backstop rather than a path.
    """
    if expr is None:
        expr = "success()"
    if expr not in SPELLINGS:
        raise ValueError(f"condition is not pinned: {expr}")
    return CONDITIONS[SPELLINGS[expr]](status, outcomes)


def simulate(source: str, *, fail: str | None = None,
             cancel_during: str | None = None,
             cancel_between: str | None = None) -> tuple[str, set[str]]:
    status = "success"
    outcomes: dict[str, str] = {}
    ran: set[str] = set()
    for step in parse(source):
        name, ident = step.name, step.field("id")
        if not condition(step.field("if"), status, outcomes):
            if ident:
                outcomes[ident] = "skipped"
            continue
        ran.add(name)
        refused = name == fail or (name == GUARD and any(
            outcomes.get(src.lower()) == "failure" for src in SOURCES + ("Resolve",)))
        outcome = "cancelled" if name == cancel_during else "failure" if refused else "success"
        if ident:
            outcomes[ident] = outcome
        if refused and step.raw.get("continue-on-error") != "true" and status != "cancelled":
            status = "failure"
        if name == cancel_during:
            status = "cancelled"
        if name == cancel_between:
            status = "cancelled"
    return status, ran


def commit_behavior(step: Step) -> bool:
    """Execute the Commit body with a recording git; a failed push must stay fatal."""
    if not step.block:
        return False
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
            # cwd: the body is workflow text, and under `npm test` the caller's directory
            # is the checkout - a mutated body must not write files into it.
            result = subprocess.run(["bash", "-e", "-o", "pipefail", "-c", step.script()],
                                    env=env, capture_output=True, text=True, cwd=temp)
            calls =(tmp / "git.log").read_text().splitlines()
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


def guard_behavior(step: Step) -> bool:
    """Run the guard on each raw outcome pattern; any refusal must exit non-zero."""
    if not step.block:
        return False
    names = ("Resolve",) + SOURCES
    cases = [{}] + [{name: "failure"} for name in names] + [
        {"Resolve": "failure", "TickPick": "skipped"},
        {"Resolve": "failure", "TickPick": "skipped", "ScoreBig": "failure"},
        {"TickPick": "skipped"}]
    with tempfile.TemporaryDirectory() as temp:
        for case in cases:
            env = {"PATH": os.environ["PATH"],
                   "GITHUB_STEP_SUMMARY": str(pathlib.Path(temp) / "summary")}
            env.update({name.upper(): case.get(name, "success") for name in names})
            result = subprocess.run(["bash", "-e", "-o", "pipefail", "-c", step.script()],
                                    env=env, capture_output=True, text=True, cwd=temp)
            if (result.returncode != 0) != ("failure" in case.values()):
                return False
    return True


def check(source: str) -> list[str]:
    try:
        ordered = parse(source)
    except GrammarError as exc:
        return [f"workflow grammar: {exc}"]
    names = [step.name for step in ordered]
    blocks = {step.name: step for step in ordered}
    errors: list[str] = []

    def require(ok: bool, message: str) -> None:
        if not ok:
            errors.append(message)

    require(all(names.count(name) == 1 for name in REQUIRED), "required steps present exactly once")
    if not all(names.count(name) == 1 for name in REQUIRED):
        return errors
    require(names.index("Self-test") < names.index("Re-resolve event ids") <
            min(names.index(name) for name in COLLECTORS), "pre-collection order")
    # An action version bump renames a `- uses:` step, so report it as what it is; the
    # ordering message below would otherwise be the only clue, and a misleading one.
    unlisted = [step.field("uses") for step in ordered
                if step.field("uses") is not None and step.field("uses") not in ACTIONS]
    if not unlisted:
        require(names[:2] == list(SETUP), "checkout and Python setup precede Self-test")
    require(max(names.index(name) for name in COLLECTORS) < names.index(SUMMARY) <
            names.index(CROSS_CHECK) < names.index(COMMIT) < names.index(GAPS) <
            names.index(GUARD), "all collection and derivation precede commit and guard")
    require(names[-1] == GUARD, "refusal guard is final")
    require(blocks["Self-test"].field("id") == "selftest", "Self-test outcome id")
    require(blocks[RESOLVE].field("id") == "resolve", "Re-resolve outcome id")
    for step in ordered:
        mask = step.raw.get("continue-on-error")
        if step.name in MASKABLE:
            require(mask == "true", f"{step.name}: failure captured with a bare continue-on-error: true")
        else:
            # Absent or a bare `false` only. Quoted or expression values are refused
            # rather than predicted: `${{ x }}` can be true at runtime.
            require(mask in (None, "false"), f"{step.name}: failure stays fatal")
        uses = step.field("uses")
        require(uses is None or uses in ACTIONS,
                f"{step.name}: action {uses} is not allowlisted (a version bump updates ACTIONS and SETUP)")
        require(allowed_if(step), f"{step.name}: if {step.field('if')!r} is not a pinned condition for this step")
        require("env" not in step.maps or step.name == GUARD, f"{step.name}: no step env")
        # Writing to these files sets env or PATH for every LATER step - `BASH_ENV`
        # through GITHUB_ENV runs code before each bash step, and a GITHUB_PATH entry can
        # shadow python3 or git - which is the step-env channel above by another door.
        # `::set-env` / `::add-path` are the disabled legacy spellings of the same thing.
        # Any mention is refused, values and keys included, rather than parsing shell.
        texts = [step.script(), *step.scalars.values(),
                 *(f"{k} {v}" for children in step.maps.values() for k, v in children.items())]
        require(not any(re.search(r"GITHUB_ENV|GITHUB_PATH|::set-env|::add-path", t, re.I) for t in texts),
                f"{step.name}: may not touch GITHUB_ENV or GITHUB_PATH")
        if step.name != COMMIT:
            # No step but Commit needs git at all. A word match closes the flag forms
            # (`git -c x commit`, `git -C . push`) without trying to parse shell.
            require(not re.search(r"\bgit\b", step.script()), f"{step.name} cannot invoke git")
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
        GAPS: ("python3 scripts/check_data_freshness.py --strict",),
        **{f"Collect {name}": (f"python3 scripts/collect_{name.lower()}.py",) for name in SOURCES},
    }.items():
        require(blocks[name].commands() == commands, f"{name} required commands")
    for name in SOURCES:
        require(blocks["Collect " + name].field("id") == name.lower(), f"{name} outcome id")
    require(commit_behavior(blocks[COMMIT]), "aggregate commit stages, commits and pushes without masking failure")
    commit_script = blocks[COMMIT].commands()
    require(sum(line.strip().startswith("git pull ") for line in commit_script) == 1 and
            any(line.strip() == "git pull --rebase --autostash" for line in commit_script),
            "rebase pull stays fatal")
    guard = blocks[GUARD]
    require(guard.maps.get("env") == GUARD_ENV, "every raw collector outcome captured")
    require(guard.commands()[-1:] == ("exit $bad",), "guard ends with a bare exit $bad")
    require(guard_behavior(guard), "refusal makes job red")

    if unlisted or not all(allowed_if(step) for step in ordered):
        # Simulating an unpinned condition would mean guessing what it means, and a
        # renamed setup step would only add a confusing "checkout fails" line.
        return errors
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
    S = source

    def rep(old: str, new: str, text: str = S) -> str:
        assert old in text, old
        return text.replace(old, new, 1)

    def change_condition(text: str, name: str, replacement: str) -> str:
        step = dict(steps(text))[name]
        current = step.raw.get("if")
        if current is None:
            first = step.text.split("\n", 1)[0] + "\n"
            return rep(first, first + f"        if: {replacement}\n", text)
        return rep(step.text, step.text.replace(f"        if: {current}\n",
                                                f"        if: {replacement}\n", 1), text)

    def move_after(text: str, name: str, after: str) -> str:
        start = text.index(f"      - name: {name}\n")
        end = text.find("      - name:", start + 10)
        block = text[start:end]
        text = text[:start] + text[end:]
        pos = text.index(f"      - name: {after}\n")
        end = text.find("      - name:", pos + 10)
        return text[:end] + block + text[end:]

    def anchor(step: str) -> str:
        return f"      - {'uses' if step in SETUP else 'name'}: {step}\n"

    def mask(text: str, step: str, value: str) -> str:
        return rep(anchor(step), anchor(step) + f"        continue-on-error: {value}\n", text)

    def add(step: str, lines: str) -> str:
        return rep(anchor(step), anchor(step) + lines)

    def before_commit(lines: str) -> str:
        return rep(anchor(COMMIT), lines + "\n" + anchor(COMMIT))

    gametime = "        run: python3 scripts/collect_gametime.py\n"
    gaps = "        run: python3 scripts/check_data_freshness.py --strict\n"
    push = "          git add -A\n          git commit -m x\n          git push\n"
    fatal = (*SETUP, "Self-test", SUMMARY, COMMIT, GAPS, GUARD)
    mutations = [
        # Ordering, gating and command pins (earlier rounds).
        ("TickPick after resolver failure", change_condition(S, COLLECTORS[0], "always()")),
        ("secondary after Self-test failure or cancellation", change_condition(S, COLLECTORS[1], "always()")),
        ("commit after Self-test failure or cancellation", change_condition(S, COMMIT, "always()")),
        ("commit after cancellation with !failure()", change_condition(
            S, COMMIT, "${{ !failure() && steps.selftest.outcome == 'success' }}")),
        ("guard disabled", change_condition(S, GUARD, "success()")),
        ("resolver bypassed", rep("run: python3 scripts/collect_tickpick.py --resolve", "run: true")),
        ("Self-test bypassed", rep("python3 scripts/market_store.py --self-test", "true")),
        ("Summarise bypassed", rep("run: python3 scripts/summarize_market.py", "run: true")),
        ("ScoreBig after commit", move_after(S, COLLECTORS[3], COMMIT)),
        ("Summarise after commit", move_after(S, SUMMARY, COMMIT)),
        ("push removed", rep("            git push\n", "")),
        ("push echoed", rep("            git push\n", "            echo git push\n")),
        ("push failure masked", rep("            git push\n", "            git push || true\n")),
        ("commit removed", rep('            git commit -m "chore: tickpick prices $(date -u +%Y-%m-%d)"\n', "")),
        ("stage narrowed", rep("            git add data/market/ data/tickpick_events.json\n",
                               "            git add data/market/tickpick.jsonl\n")),
        ("pull failure masked", rep("            git pull --rebase --autostash\n",
                                    "            git pull --rebase --autostash || true\n")),
        ("gap strict removed", rep(gaps, "        run: python3 scripts/check_data_freshness.py\n")),
        ("gap failure masked in shell", rep(gaps, gaps.replace("--strict", "--strict || true"))),
        ("gap quoted || true", rep(gaps, '        run: "python3 scripts/check_data_freshness.py --strict || true"\n')),
        *((f"{name} command {how}", rep(f"run: python3 scripts/collect_{name.lower()}.py\n", new(name)))
          for name in SOURCES for how, new in (
              ("masked", lambda n: f"run: python3 scripts/collect_{n.lower()}.py || true\n"),
              ("semicolon mask", lambda n: f"run: python3 scripts/collect_{n.lower()}.py; true\n"),
              ("bypassed", lambda n: "run: true\n"))),
        ("collector mask removed", rep(gametime + "        continue-on-error: true\n", gametime)),
        ("collector mask as expression", rep(gametime + "        continue-on-error: true\n",
                                             gametime + "        continue-on-error: ${{ true }}\n")),
        # Fatal steps: every value spelling and every key spelling (rounds four and five).
        *((f"{step} mask {value}", mask(S, step, value))
          for step in fatal
          for value in ("true", "'true'", '"true"', "${{ true }}", "${{true}}", "${{ 'true' }}",
                        "${{ inputs.mask }}", "${{ github.event_name == 'schedule' }}",
                        "yes", "True", "true # x", "'false'", '"false"', "false # x")),
        *((f"{step} {label}", add(step, lines))
          for step in fatal for label, lines in (
              ("quoted mask key", '        "continue-on-error": true\n'),
              ("single-quoted mask key", "        'continue-on-error': true\n"),
              ("space before mask colon", "        continue-on-error : true\n"),
              ("folded mask value", "        continue-on-error: >-\n          true\n"),
              ("explicit mask key", "        ? continue-on-error\n        : true\n"),
              ("flow mask", "        {continue-on-error: true}\n"))),
        # Push or commit outside Commit (round five).
        *((f"named run: {indicator} push step before Commit",
           before_commit(f"      - name: X\n        if: always()\n        run: {indicator}\n" + push))
          for indicator in ("|", "|-", "|+", ">", ">-", "|2", "| # x")),
        ("unnamed push before commit", before_commit("      - run: git push\n")),
        ("extra push before commit", before_commit("      - name: Extra push\n        if: always()\n        run: git push\n")),
        ("extra commit before commit", before_commit("      - name: Extra commit\n        if: always()\n        run: git commit -am x\n")),
        ("git -c / -C flags", before_commit("      - name: X\n        if: always()\n"
                                            "        run: git -c user.name=x commit -am x && git -C . push\n")),
        ("if-first step pushes", before_commit("      - if: always()\n        run: git commit -am x && git push\n")),
        ("id-first step pushes", before_commit("      - id: x\n        if: always()\n        run: git push\n")),
        ("env-first step pushes", before_commit("      - env:\n          A: b\n        run: git push\n")),
        ("two if-first steps after setup-python", rep('          python-version: "3.12"\n',
                                                      '          python-version: "3.12"\n\n      - if: always()\n        run: echo hi\n\n'
                                                      '      - if: always()\n        run: git commit -am x && git push\n')),
        ("id-first push step after Summary", rep("      # continue-on-error preserves",
                                                 "      - id: x\n        if: always()\n        run: git commit -am x && git push\n\n"
                                                 "      # continue-on-error preserves")),
        ("flow-style push step", before_commit("      - {name: X, if: always(), run: git commit -am x && git push}\n")),
        ("action-based push", before_commit("      - name: X\n        if: always()\n        uses: stefanzweifel/git-auto-commit-action@v5\n")),
        ("local action", before_commit("      - name: X\n        uses: ./push\n")),
        ("Summary step pushes", rep('          echo "## TickPick collection', '          git push\n          echo "## TickPick collection')),
        # Shell, directory and environment overrides on pinned steps.
        ("Gametime shell override", rep(gametime, gametime + "        shell: bash -c 'true' {0}\n")),
        ("Gaps shell override", rep(gaps, gaps + "        shell: bash -c 'true' {0}\n")),
        ("Gametime working-directory", rep(gametime, gametime + "        working-directory: /tmp\n")),
        ("Gaps working-directory", rep(gaps, gaps + "        working-directory: /tmp\n")),
        ("collector BASH_ENV", rep(gametime, gametime + "        env:\n          BASH_ENV: ./x\n")),
        ("workflow defaults shell", rep("jobs:\n", "defaults:\n  run:\n    shell: bash -c 'true' {0}\n\njobs:\n")),
        ("workflow env", rep("jobs:\n", "env:\n  BASH_ENV: ./x\n\njobs:\n")),
        ("quoted workflow defaults", rep("jobs:\n", '"defaults":\n  run:\n    working-directory: /tmp\n\njobs:\n')),
        ("job continue-on-error", rep("    runs-on: ubuntu-latest\n", "    runs-on: ubuntu-latest\n    continue-on-error: true\n")),
        ("job defaults", rep("    runs-on: ubuntu-latest\n", "    runs-on: ubuntu-latest\n    defaults:\n      run:\n        shell: sh\n")),
        # The guard (round five; previously near-equivalent survivors).
        ("guard exit masked", rep("          exit $bad\n", "          exit $bad || true\n")),
        ("guard exit zero", rep("          exit $bad\n", "          exit 0\n")),
        ("guard never sets bad", rep("              bad=1\n", "              bad=0\n")),
        ("guard loses a source", rep("for src in RESOLVE TICKPICK GAMETIME", "for src in RESOLVE GAMETIME")),
        ("guard env renamed", rep("          SCOREBIG: ${{ steps.scorebig.outcome }}\n",
                                  "          SCOREBIG: ${{ steps.scorebig.conclusion }}\n")),
        # Grammar edges: each is YAML some reader accepts, refused here.
        ("tab in a value", rep("        id: selftest\n", "        id:\tselftest\n")),
        ("CR line ending", rep("        id: selftest\n", "        id: selftest\r\n")),
        ("non-breaking space indent", rep("        id: selftest\n", "        id: selftest\n")),
        ("byte-order mark", "﻿" + S),
        ("unicode line separator", rep("        id: selftest\n", "        id: selftest         continue-on-error: true\n")),
        ("duplicate key", rep("        id: selftest\n", "        id: selftest\n        id: other\n")),
        ("duplicate run", rep("        id: selftest\n", "        id: selftest\n        run: echo\n")),
        ("anchor key", rep("        id: selftest\n", "        &a id: selftest\n")),
        ("anchored value", rep("        id: selftest\n", "        id: &a selftest\n")),
        ("alias value", rep("        id: selftest\n", "        id: *a\n")),
        ("tagged value", rep("        id: selftest\n", "        id: !!str selftest\n")),
        ("merge key", rep("        id: selftest\n", "        <<: *defaults\n        id: selftest\n")),
        ("double-quoted escape", rep("        id: selftest\n", '        id: "self\\x74est"\n')),
        ("comment after value", rep("        id: selftest\n", "        id: selftest # x\n")),
        ("seven-space step", rep("      - name: Self-test\n", "       - name: Self-test\n")),
        ("nine-space key", rep("        id: selftest\n", "         id: selftest\n")),
        ("double space after colon", rep("        id: selftest\n", "        id:  selftest\n")),
        ("trailing space", rep("        id: selftest\n", "        id: selftest \n")),
        ("plain continuation line", rep(gametime, gametime + "          || true\n")),
        ("indented first block line", rep("          python3 scripts/market_store.py --self-test\n",
                                          "            python3 scripts/market_store.py --self-test\n")),
        ("blank first block line", rep("        run: |\n          python3 scripts/market_store.py",
                                       "        run: |\n\n          python3 scripts/market_store.py")),
        ("body after an ending comment", rep("          python3 scripts/summarize_market.py --self-test\n",
                                             "      # x\n          python3 scripts/summarize_market.py --self-test\n")),
        ("uses step renamed by key", rep("      - uses: actions/checkout@v7\n",
                                         "      - uses: actions/checkout@v7\n        name: Checkout\n")),
        ("with on a run step", rep(gametime, gametime + "        with:\n          a: b\n")),
        ("uses and run together", rep(gametime, gametime + "        uses: actions/checkout@v7\n")),
        ("unknown step key", rep(gametime, gametime + "        timeout-minutes: 1\n")),
        ("flow with mapping", rep("        with:\n          python-version: \"3.12\"\n",
                                  "        with: {python-version: '3.12'}\n")),
        ("second document", S + "---\njobs: {}\n"),
        ("second job after steps", S + "  other:\n    runs-on: ubuntu-latest\n"),
        ("job-level key after steps", S + "    continue-on-error: true\n"),
        ("quoted top-level key", rep("jobs:\n", '"jobs":\n')),
        ("duplicate top-level key", rep("jobs:\n", "permissions:\n  contents: read\n\njobs:\n")),
        # Conditions Actions reads differently from a hand-written evaluator (round six).
        # Case: Actions compares strings ignoring case.
        ("TickPick after resolver FAILURE (case)", change_condition(S, COLLECTORS[0],
            "${{ !cancelled() && (steps.resolve.outcome == 'success' || steps.resolve.outcome == 'FAILURE') }}")),
        ("Commit after Self-test FAILURE (case)", change_condition(S, COMMIT,
            "${{ !cancelled() && (success() || steps.selftest.outcome == 'FAILURE') }}")),
        ("TickPick outcome SUCCESS (case)", change_condition(S, COLLECTORS[0],
            "${{ !cancelled() && steps.resolve.outcome == 'SUCCESS' }}")),
        ("Gaps !Cancelled() (case)", change_condition(S, GAPS, "${{ !Cancelled() }}")),
        ("guard ALWAYS() (case)", change_condition(S, GUARD, "ALWAYS()")),
        ("Commit SUCCESS() (case)", change_condition(S, COMMIT,
            "${{ !cancelled() && SUCCESS() && steps.selftest.outcome == 'success' }}")),
        # Coercion: mismatched types compare as numbers, so false == '' is true.
        ("Commit after cancellation (coercion)", change_condition(S, COMMIT,
            "${{ (success() || success() == '') && steps.selftest.outcome == 'success' }}")),
        ("TickPick after refusal (coercion)", change_condition(S, COLLECTORS[0],
            "${{ !cancelled() && (steps.resolve.outcome == 'success' || false == '') }}")),
        ("Summarise on cancel (coercion)", change_condition(S, SUMMARY,
            "${{ !cancelled() == 0 || success() && steps.selftest.outcome == 'success' }}")),
        # A status name inside a literal is not a status call: Actions adds success().
        ("guard status inside a literal", change_condition(S, GUARD, "${{ 'always()' }}")),
        ("Gaps status inside a literal", change_condition(S, GAPS, "${{ 'cancelled()' }}")),
        # Pinned per step: a condition valid elsewhere is still refused here.
        ("Gametime gated like TickPick", change_condition(S, COLLECTORS[1],
            "${{ !cancelled() && steps.resolve.outcome == 'success' }}")),
        ("Commit with no if", rep("      - name: Commit aggregates\n"
                                  "        if: ${{ !cancelled() && success() && steps.selftest.outcome == 'success' }}\n",
                                  "      - name: Commit aggregates\n")),
        ("Summary step on failure()", change_condition(S, "Summary", "failure()")),
        ("new step on !cancelled()", before_commit("      - name: X\n        if: ${{ !cancelled() }}\n        run: echo hi\n")),
        # The job environment, written from a step (round six, non-blocking).
        ("BASH_ENV via GITHUB_ENV", before_commit(
            "      - name: Prep\n        run: echo \"BASH_ENV=$PWD/x.sh\" >> \"$GITHUB_ENV\"\n")),
        ("python3 shadowed via GITHUB_PATH", before_commit(
            "      - name: Prep\n        run: echo \"$PWD/bin\" >> \"$GITHUB_PATH\"\n")),
        ("Summary writes GITHUB_ENV", rep('          echo "## TickPick collection',
                                          '          echo X=1 >> $GITHUB_ENV\n          echo "## TickPick collection')),
        ("legacy add-path command", before_commit("      - name: Prep\n        run: echo ::add-path::/tmp\n")),
    ]
    killed = 0
    for label, mutant in mutations:
        if mutant == S or not check(mutant):
            print(f"mutation survived: {label}", file=sys.stderr)
            return 1
        killed += 1
    # A version bump must say what it is, not trip the ordering check (round six).
    for action in sorted(ACTIONS):
        bumped = check(S.replace(action, action[:-1] + "999"))
        if (not any("not allowlisted" in e for e in bumped) or
                any("precede" in e for e in bumped)):
            print(f"bump of {action} misreported: {bumped}", file=sys.stderr)
            return 1
    # Canonical rewrites that Actions reads identically must still pass, or the grammar
    # would be a straitjacket rather than a reader.
    rearranged = move_after(S, "Collect Gametime", "Collect TicketNetwork")
    rearranged = rep(gametime + "        continue-on-error: true\n",
                     "        continue-on-error: true\n\n        # comment between keys\n" + gametime,
                     rearranged)
    rearranged = rep("        id: selftest\n        run: |\n", "        run: |\n", rearranged)
    rearranged = rep("          python3 scripts/summarize_market.py --self-test\n",
                     "          python3 scripts/summarize_market.py --self-test\n        id: selftest\n",
                     rearranged)
    rearranged = rep("      - name: Collect ScoreBig\n", '      - name: "Collect ScoreBig"\n', rearranged)
    rearranged = rep("        id: resolve\n", "        id: 'resolve'\n", rearranged)
    rearranged = rep("permissions:\n", "# a header comment\npermissions:\n", rearranged)
    valid = [("rearranged canonical workflow", rearranged)] + [
        (f"{step} bare false", mask(S, step, "false")) for step in fatal] + [
        # The documented equivalents the pinned table admits.
        ("guard if wrapped in ${{ }}", change_condition(S, GUARD, "${{ always() }}")),
        ("Gametime explicit success()", change_condition(S, COLLECTORS[1], "success()")),
        ("Self-test explicit ${{ success() }}", change_condition(S, "Self-test", "${{ success() }}")),
        ("Gaps quoted bare !cancelled()", change_condition(S, GAPS, "'!cancelled()'")),
        ("Commit bare expression", change_condition(
            S, COMMIT, "\"!cancelled() && success() && steps.selftest.outcome == 'success'\""))]
    for label, variant in valid:
        problems = check(variant)
        if variant == S or problems:
            print(f"valid canonical form rejected: {label}: {problems}", file=sys.stderr)
            return 1
    print(f"workflow check passes; {killed} mutations rejected, {len(valid)} canonical variants accepted")
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
