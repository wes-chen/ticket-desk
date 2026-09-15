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

case "${1:-}" in
  --list)
    [[ $# -eq 1 ]] || { echo "--list takes no arguments" >&2; usage >&2; exit 2; }
    git -C "$CANON" worktree list
    exit 0 ;;
  --remove)
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
