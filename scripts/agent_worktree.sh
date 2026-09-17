#!/usr/bin/env bash
# Give an agent session its own checkout, so two agents cannot publish each other's
# half-finished work. See ops#41.
#
# THE PROBLEM. Both sessions were editing the same directory. `git add -A` from either
# one stages whatever the other has in progress, and it happened: one commit swallowed
# another agent's mid-debug script plus a stray .pyc, under a message describing
# neither. The author of the message was not the author of the change, which destroys
# the one thing this project's history is actually good for.
#
# THE TRAP THIS HAS TO AVOID, and it is documented in CLAUDE.md rule 0: Claude Code keys
# its MEMORY to the working directory. A naive worktree at a new path therefore gets a
# fresh, empty memory set and silently loses every measured constant and preference this
# project has accumulated - the precise failure that made rule 0 necessary in the first
# place. So this script symlinks the new path's memory directory at the canonical one.
# One memory set, many checkouts.
#
# Usage:  scripts/agent_worktree.sh --help
#
# Deliberately not repeated here. This comment used to carry its own copy of the three
# invocation forms, which is a second source for one string - and it was already stale,
# missing --help. One usage() function, referenced.

set -euo pipefail

# Checked up front rather than let `git check-ref-format` fail with 127 inside an `||`,
# where it would reject a perfectly valid name and blame the name. Third time in this
# script that a message named the wrong cause - the other two were `fatal: invalid
# reference` reading as "no such branch", and `--help` being taken for a worktree name.
command -v git >/dev/null || { echo "git not found on PATH" >&2; exit 127; }

CANON="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROJECTS="$HOME/.claude/projects"
# Claude Code's project key is the absolute path with every "/" replaced by "-".
key() { echo "${1//\//-}"; }
CANON_KEY="$(key "$CANON")"
CANON_MEM="$PROJECTS/$CANON_KEY/memory"

usage() {
  cat <<'USAGE'
usage: agent_worktree.sh <name>        give session <name> its own checkout
       agent_worktree.sh --list        list existing worktrees
       agent_worktree.sh --remove <n>  remove a worktree and its memory symlink
       agent_worktree.sh --help        this message

<name> is a session id, e.g. f33859f9. It must not begin with "-".

Creates ../ticket-desk-<name> on branch agent/<name>, symlinks its Claude Code
memory directory at the canonical checkout's, and copies .private-patterns - which is
gitignored and local-only, so a fresh worktree without it silently SKIPS the literal
and history privacy passes. (.privacy-accepted is tracked and arrives with the
checkout; the copy loop covers both but only ever fires for the first.)
USAGE
}

# ----------------------------------------------------------------- self-test
# ops#184. This script is the one CLAUDE.md rule 0 tells every agent to run, and it
# shipped a wrong-cause diagnostic in two consecutive PRs - ops#179 (`--help` taken for
# a worktree name, creating a branch, a worktree, a symlink and a copy of
# .private-patterns) and ops#181 (the check-ref-format guard blaming the NAME whenever
# git was off PATH). Neither was catchable by the suite, because the suite did not look
# at this file. Every case below is one of those regressions or an acceptance case from
# them.
#
# The old EXEMPT entry argued a self-test "would have to create git branches, worktrees
# and a symlink under $HOME - and doing it in-repo IS the ops#179 bug". That conflates
# IN-REPO with IMPOSSIBLE. Nothing here touches the real checkout or the real
# ~/.claude: the script under test is copied into a throwaway `git init` repo inside a
# temp dir, so its own CANON/PROJECTS derivation lands entirely within that dir, and
# every invocation runs under `env -i` so nothing inherits the caller's environment.

st_pass=0; st_fail=0

st_ok()   { st_pass=$((st_pass + 1)); printf '  ok    %s\n' "$1"; }
st_bad()  { st_fail=$((st_fail + 1)); printf '  FAIL  %s\n          %s\n' "$1" "$2" >&2; }

st_rc() { # name expected actual
  [[ "$2" == "$3" ]] && st_ok "$1" || st_bad "$1" "expected rc $2, got $3"
}

st_has() { # name needle haystack
  case "$3" in
    *"$2"*) st_ok "$1" ;;
    *)      st_bad "$1" "output does not mention '$2'" ;;
  esac
}

st_true() { # name condition-already-evaluated-as-rc
  [[ "$2" == 0 ]] && st_ok "$1" || st_bad "$1" "condition did not hold"
}

# A PATH built from symlinks to just the tools this script uses, so `git` can be left
# out ON PURPOSE. Without the control case below, a PATH missing something else
# entirely would produce the same rc 127 and read as a passing test.
st_bindir() { # $1 = dir, $2 = with-git | no-git
  mkdir -p "$1"
  local tools=(bash sh env dirname basename cat mkdir ln readlink rm rmdir cp find sort)
  [[ "$2" == with-git ]] && tools+=(git)
  local t p
  for t in "${tools[@]}"; do
    p="$(command -v "$t" 2>/dev/null)" || continue
    ln -sf "$p" "$1/$t"
  done
}

st_tree() { ( cd "$1" && find . -print 2>/dev/null | LC_ALL=C sort ); }

self_test() {
  local self repo home bin bin_nogit out rc before after
  self="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")"
  # ST_TMP is global on purpose: as a `local` it is out of scope by the time the EXIT
  # trap fires, so cleanup died on "unbound variable" and took the failure report with
  # it - a cleanup path that worked only when nothing was wrong.
  ST_TMP="$(mktemp -d)"; tmp="$ST_TMP"
  # No `set -e` surprises: every invocation below captures its rc explicitly.
  trap 'rm -rf "$ST_TMP"' EXIT

  repo="$tmp/ticket-desk"
  home="$tmp/fakehome"
  bin="$tmp/bin"
  bin_nogit="$tmp/bin-nogit"

  mkdir -p "$repo/scripts" "$home/.claude/projects"
  cp "$self" "$repo/scripts/agent_worktree.sh"
  chmod +x "$repo/scripts/agent_worktree.sh"
  # A DECOY private-patterns. Rule 1's corollary is that plausible means real, so the
  # literal here is absurd on purpose and the real file is never copied into the temp
  # dir - which would put it somewhere this script then deletes.
  printf '11111111\n' > "$repo/.private-patterns"
  # Gitignored exactly as in the real checkout. Without this the fixture COMMITS it, the
  # worktree receives it from the checkout, and the assertion below passes no matter what
  # the copy loop does - which is how deleting that loop left the suite green (mutant M9).
  printf '.private-patterns\n' > "$repo/.gitignore"
  git -C "$repo" init -q
  git -C "$repo" config user.email "selftest@example.invalid"
  git -C "$repo" config user.name "self test"
  git -C "$repo" add -A
  git -C "$repo" commit -qm "self-test fixture"

  # The canonical memory set the script is supposed to share rather than re-create.
  local canon_key canon_mem
  canon_key="${repo//\//-}"
  canon_mem="$home/.claude/projects/$canon_key/memory"
  mkdir -p "$canon_mem"
  printf 'decoy memory\n' > "$canon_mem/MEMORY.md"

  st_bindir "$bin" with-git
  st_bindir "$bin_nogit" no-git

  # Fixture setup above runs under the file-level `set -e`, so a broken fixture still
  # aborts loudly. From here down it must NOT: every assertion deliberately runs a
  # command that is expected to fail sometimes, and errexit would end the suite at the
  # first one - reporting a crash instead of a named failure and skipping the rest.
  set +e

  run() { # run <rc-var> <out-var> args...
    local __rcvar="$1" __outvar="$2" __rc=0 __out
    shift 2
    __out="$(env -i HOME="$home" PATH="$bin" \
             bash "$repo/scripts/agent_worktree.sh" "$@" 2>&1)" || __rc=$?
    printf -v "$__rcvar" '%s' "$__rc"
    printf -v "$__outvar" '%s' "$__out"
  }

  echo "agent_worktree.sh --self-test"
  echo
  echo "  flags that must create nothing"

  # --- --help / -h, and the ops#179 regression: they must CREATE NOTHING ------------
  before="$(st_tree "$tmp")"
  run rc out --help
  st_rc "--help exits 0" 0 "$rc"
  st_has "--help prints usage" "usage: agent_worktree.sh" "$out"
  run rc out -h
  st_rc "-h exits 0" 0 "$rc"
  after="$(st_tree "$tmp")"
  [[ "$before" == "$after" ]]; st_true "--help/-h create nothing (ops#179)" $?

  # --- any unrecognised flag is an error naming itself, never a worktree name -------
  before="$(st_tree "$tmp")"
  run rc out --nope
  st_rc "unknown flag exits 2" 2 "$rc"
  st_has "unknown flag names itself" "unknown option: --nope" "$out"
  after="$(st_tree "$tmp")"
  [[ "$before" == "$after" ]]; st_true "unknown flag creates nothing" $?

  run rc out ""
  st_rc "no name exits 2" 2 "$rc"

  echo
  echo "  names git would refuse, refused HERE with a message about the name"

  # ops#181: these reached `worktree add` and failed with "fatal: invalid reference",
  # which reads as "no such branch" rather than "bad name".
  local bad
  for bad in "a/b" "abc.lock" ".." ".hidden" "abc."; do
    run rc out "$bad"
    st_rc "name '$bad' exits 2" 2 "$rc"
    st_has "name '$bad' blamed as a name" "invalid name" "$out"
  done

  echo
  echo "  arity"

  # The transposition that silently CREATED zz9 and ignored the rest.
  before="$(st_tree "$tmp")"
  run rc out zz9 --remove
  st_rc "'zz9 --remove' exits 2" 2 "$rc"
  st_has "'zz9 --remove' blamed as arity" "expected one name" "$out"
  after="$(st_tree "$tmp")"
  [[ "$before" == "$after" ]]; st_true "'zz9 --remove' creates nothing" $?

  run rc out --list extra
  st_rc "'--list extra' exits 2" 2 "$rc"
  st_has "'--list extra' blamed as arity" "--list takes no arguments" "$out"

  run rc out --remove a b
  st_rc "'--remove a b' exits 2" 2 "$rc"
  st_has "'--remove a b' blamed as arity" "--remove takes exactly one name" "$out"

  run rc out --remove
  st_rc "'--remove' with no name exits 2" 2 "$rc"

  echo
  echo "  the happy path, and that it is idempotent"

  local wt wt_key link
  wt="$tmp/ticket-desk-zz9"
  wt_key="${wt//\//-}"
  link="$home/.claude/projects/$wt_key/memory"

  run rc out zz9
  st_rc "creating zz9 exits 0" 0 "$rc"
  [[ -d "$wt" ]]; st_true "worktree directory exists" $?
  git -C "$repo" rev-parse --verify -q "refs/heads/agent/zz9" >/dev/null 2>&1
  st_true "branch agent/zz9 exists" $?
  [[ -L "$link" ]]; st_true "memory is a SYMLINK, not a fresh set (rule 0)" $?
  [[ "$(readlink "$link")" == "$canon_mem" ]]
  st_true "memory symlink points at the canonical set" $?
  [[ -f "$wt/.private-patterns" ]]
  st_true ".private-patterns copied (else the literal/history passes SKIP)" $?
  [[ "$(cat "$wt/.private-patterns" 2>/dev/null)" == "11111111" ]]
  st_true ".private-patterns copied with its contents" $?

  # Re-running must be a no-op that says so, not a second branch or a clobbered link.
  run rc out zz9
  st_rc "re-running zz9 exits 0" 0 "$rc"
  st_has "re-run reports the existing worktree" "worktree already exists" "$out"
  st_has "re-run reports the existing link" "memory already linked" "$out"
  [[ -L "$link" ]]; st_true "re-run leaves the symlink intact" $?

  run rc out --list
  st_rc "--list exits 0" 0 "$rc"
  st_has "--list shows the worktree" "ticket-desk-zz9" "$out"

  echo
  echo "  --remove cleans up all three"

  run rc out --remove zz9
  st_rc "--remove exits 0" 0 "$rc"
  [[ ! -e "$wt" ]]; st_true "worktree directory gone" $?
  [[ ! -e "$link" ]]; st_true "memory symlink gone" $?
  [[ ! -d "$home/.claude/projects/$wt_key" ]]
  st_true "empty project dir gone (an empty memory set is the rule 0 failure)" $?
  [[ -d "$canon_mem" ]]; st_true "canonical memory set untouched" $?
  [[ -f "$canon_mem/MEMORY.md" ]]; st_true "canonical memory CONTENTS untouched" $?

  echo
  echo "  git absent - control first, or a broken PATH reads as a pass"

  # ops#181 is exactly this: without the up-front `command -v git`, check-ref-format
  # failed with 127 inside an `||` and the script blamed the NAME.
  local rc2 out2
  out2="$(env -i HOME="$home" PATH="$bin_nogit" \
          bash "$repo/scripts/agent_worktree.sh" --help 2>&1)" && rc2=0 || rc2=$?
  # CONTROL: the same restricted PATH, WITH git, must still work. Asserted before the
  # negative case so that a PATH missing bash or coreutils cannot masquerade as "git
  # is correctly detected as missing".
  local rc3 out3
  out3="$(env -i HOME="$home" PATH="$bin" \
          bash "$repo/scripts/agent_worktree.sh" --help 2>&1)" && rc3=0 || rc3=$?
  st_rc "CONTROL: restricted PATH with git runs --help" 0 "$rc3"
  st_rc "restricted PATH without git exits 127" 127 "$rc2"
  st_has "git-absent blames GIT, not the name" "git not found on PATH" "$out2"
  case "$out2" in
    *"invalid name"*) st_bad "git-absent does not blame the name (ops#181)" \
                             "output still says 'invalid name'" ;;
    *) st_ok "git-absent does not blame the name (ops#181)" ;;
  esac

  # A name is refused for being a name even when git IS present - guarding against a
  # future 'fix' that makes every refusal look like the git-absent one.
  out3="$(env -i HOME="$home" PATH="$bin" \
          bash "$repo/scripts/agent_worktree.sh" "a/b" 2>&1)" && rc3=0 || rc3=$?
  st_rc "with git present, a bad name still exits 2" 2 "$rc3"

  echo
  echo "  nothing escaped the temp dir"

  [[ ! -e "$HOME/.claude/projects/$wt_key" ]]
  st_true "no project dir under the REAL \$HOME" $?
  local real_sibling
  real_sibling="$(dirname "$CANON")/$(basename "$CANON")-zz9"
  [[ ! -e "$real_sibling" ]]; st_true "no worktree beside the REAL checkout" $?
  git -C "$CANON" rev-parse --verify -q "refs/heads/agent/zz9" >/dev/null 2>&1 \
    && st_bad "no agent/zz9 branch in the REAL repo" "branch was created" \
    || st_ok "no agent/zz9 branch in the REAL repo"

  echo
  echo "$st_pass passed, $st_fail failed"
  [[ "$st_fail" -eq 0 ]]
}

case "${1:-}" in
  --list)
    [[ $# -eq 1 ]] || { echo "--list takes no arguments" >&2; usage >&2; exit 2; }
    git -C "$CANON" worktree list
    exit 0 ;;
  --remove)
    [[ $# -eq 2 ]] || { echo "--remove takes exactly one name, got $(($# - 1))" >&2
                        usage >&2; exit 2; }
    name="${2:-}"
    # Not ${2:?...}: that exits 1 with a bash-internal message and no usage, so this
    # was a THIRD error path while the PR claimed two.
    [[ -n "$name" ]] || { echo "--remove needs a name" >&2; usage >&2; exit 2; }
    wt="$(dirname "$CANON")/$(basename "$CANON")-$name"
    git -C "$CANON" worktree remove "$wt" --force
    proj="$PROJECTS/$(key "$wt")"
    [[ -L "$proj/memory" ]] && rm "$proj/memory" && echo "removed memory symlink"
    # Remove the project directory too, but only if empty. Leaving it behind is worse
    # than clutter: a future session started from that path would find an EMPTY memory
    # set with no symlink, and silently lack every constant this project has measured -
    # the exact failure CLAUDE.md rule 0 exists to prevent.
    rmdir "$proj" 2>/dev/null && echo "removed empty project dir $proj" \
      || { [[ -d "$proj" ]] && echo "NOTE: $proj is not empty; left in place" >&2; }
    echo "removed worktree $wt"
    exit 0 ;;
  -h|--help)
    usage; exit 0 ;;
  # The double quotes around this pattern are LOAD-BEARING, not style. run_tests.py
  # discovers self-testing scripts with SELF_TEST_RE = /"--self-test"/, matching the
  # literal WITH quotes, so the idiomatic bare `--self-test)` is not detected - and a
  # real self-test written that way is classified untested, which used to be silently
  # shielded by this file's EXEMPT entry. Quoted here rather than widening the regex,
  # because a looser regex would also match any script that merely documents the flag.
  "--self-test")
    [[ $# -eq 1 ]] || { echo "--self-test takes no arguments" >&2; usage >&2; exit 2; }
    self_test; exit $? ;;
  "")
    usage >&2
    exit 2 ;;
  -*)
    # ANY unrecognised flag is an ERROR, never a worktree name. Without this, `--help`
    # fell through to `name="$1"` and CREATED things: a branch `agent/--help` (which
    # `git check-ref-format` happily accepts), a worktree directory outside the repo, a
    # symlink into ~/.claude, and a COPY OF .private-patterns at a new path. Typing
    # --help at a script to find out what it does is not a reasonable way to acquire a
    # git branch, and a mistyped flag became a worktree rather than a complaint.
    #
    # Fifth instance of the same defect found on 2026-09-15 (ops#171/172/176/179), and
    # the worst: the others wrote a tracked store or fetched a page, this one mutated
    # git state and duplicated the private-literals file. See ops#179.
    echo "unknown option: $1" >&2
    usage >&2
    exit 2 ;;
esac

name="$1"
# A POSITIVE check, not just "does not start with -". Two holes this closes, both
# measured: a transposition like `agent_worktree.sh zz9 --remove` silently CREATED zz9
# and ignored the rest, and a name containing "/" created an intermediate directory
# that is not a worktree and that --remove leaves orphaned.
[[ $# -eq 1 ]] || { echo "expected one name, got $#: $*" >&2; usage >&2; exit 2; }
[[ "$name" =~ ^[A-Za-z0-9._-]+$ ]] || {
  echo "invalid name: $name (use letters, digits, dot, underscore, dash)" >&2
  usage >&2; exit 2; }
# ...and then ASK GIT rather than encoding its rules here. The pattern above allows "."
# so it lets through names git refuses - abc.lock, .., .hidden, abc. - which then failed
# at `worktree add` with "fatal: invalid reference: agent/abc.lock", a message that reads
# as "no such branch" rather than "bad name". Nothing was half-created, but the
# diagnosis was wrong. refs/heads/ form, not --branch: --branch resolves @{-1} shorthand.
git check-ref-format "refs/heads/agent/$name" 2>/dev/null || {
  echo "invalid name: $name (must form a valid branch agent/$name)" >&2
  usage >&2; exit 2; }
wt="$(dirname "$CANON")/$(basename "$CANON")-$name"

if [[ -e "$wt" ]]; then
  echo "worktree already exists: $wt"
else
  git -C "$CANON" worktree add "$wt" -b "agent/$name" 2>/dev/null \
    || git -C "$CANON" worktree add "$wt" "agent/$name"
  echo "created worktree $wt on branch agent/$name"
fi

# Share the canonical memory rather than starting a fresh, empty set.
if [[ ! -d "$CANON_MEM" ]]; then
  echo "WARNING: canonical memory not found at $CANON_MEM - not linking" >&2
else
  target_dir="$PROJECTS/$(key "$wt")"
  mkdir -p "$target_dir"
  link="$target_dir/memory"
  if [[ -L "$link" ]]; then
    echo "memory already linked: $link -> $(readlink "$link")"
  elif [[ -e "$link" ]]; then
    # A real directory here means a session already ran from this path and accumulated
    # its own memories. Refuse rather than clobber them.
    echo "REFUSING to link: $link exists as a real directory. Merge it into" >&2
    echo "$CANON_MEM by hand, then delete it and re-run." >&2
    exit 1
  else
    ln -s "$CANON_MEM" "$link"
    echo "linked memory: $link -> $CANON_MEM"
  fi
fi

# The privacy checks are local-only by design and a fresh checkout has neither file, so
# the literal and history passes would silently skip - which CLAUDE.md warns reads as
# "clean" on evidence never gathered.
for f in .private-patterns .privacy-accepted; do
  if [[ -f "$CANON/$f" && ! -e "$wt/$f" ]]; then
    cp "$CANON/$f" "$wt/$f"
    echo "copied $f (gitignored, local-only, needed for the literal/history passes)"
  fi
done

cat <<EOF

Done. Work from:  $wt
  - own index and working files; nothing you stage can pick up another agent's edits
  - shared history; push and pull as normal
  - on branch agent/$name, so merge to main via a normal push or PR
  - same memory set as the canonical checkout (symlinked)

Still run 'npm install' there once - node_modules is not shared.
EOF
