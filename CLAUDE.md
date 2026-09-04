# Working in this repo

Read `README.md` for what the project is and why. This file is the rules.

## 0. Start sessions from this directory

`~/Dev/ticket-desk` - the code repo. Not the parent, not the ops repo.

Everything needed is reachable from here: the code, this file, and `.claude/issues.json`
pointing at the tracker. The ops repo does not need a local clone - `gh --repo
wes-chen/ticket-desk-ops` reaches its issues fine.

Note that Claude Code keys its memory to the working directory, so a session started
somewhere else gets a *different* memory set and will silently lack this project's
context. Memories were migrated here on 2026-09-03; the copy under the parent directory
is marked stale on purpose.

## 1. This repo is PUBLIC. Never commit personal data.

Non-negotiable, and it has already been violated once - not through config, but through
**form placeholders** written with real seat and invoice values. Personal data leaks
through UI copy, examples, comments, commit messages, and docs just as readily as
through config files.

Never write into this repo:

- Seat section / row / seat numbers
- Season invoice totals, or any amount paid
- Exchange credit amounts per tier
- Listing prices, payouts, or account identifiers

These live in **browser `localStorage`** (entered by the user through the app's setup
screen) and in the **private ops repo**. Nowhere else.

Market observations that are not seat-identifying - fee ratios, the instant-offer
formula, public listing prices - are fine here.

### Enforcement

`npm run build` runs `scripts/check_privacy.py`, which checks three surfaces:

| Pass | Covers | Runs |
| --- | --- | --- |
| structural | forbidden keys in committed JSON | everywhere, including CI |
| literal | `dist/` + every git-tracked file | only when `.private-patterns` exists |
| history | git log content **and** commit messages | only when `.private-patterns` exists |

`.private-patterns` is gitignored and local-only - committing it would defeat its
purpose. **A fresh clone will not have it, so the literal and history passes silently
skip.** A clean build on a fresh clone is therefore weaker evidence than it looks. If
you are about to make a visibility change, recreate that file first.

History findings are advisory by default and fatal under `PRIVACY_HISTORY_FATAL=1`.

## 2. Issues live in a different repo

Issues are **disabled here**. Tracking is at `wes-chen/ticket-desk-ops` (private), as
declared in `.claude/issues.json`.

File issues there, not here, and note that personal values *are* allowed there - that is
the entire reason for the split. Check `gh issue list --repo wes-chen/ticket-desk-ops`
before starting work; the backlog is real and current.

## 3. Never touch the authenticated Ticketmaster session

The collector scrapes **logged out, always**. That account holds the season tickets. An
account suspension costs far more than any data it could gather. Where a public page
hides something, the answer is manual paste - which is how the tier credits, the fee
ratio, and the instant-offer formula were all obtained.

## 4. Distinguish measured from assumed, always

The project's core discipline. Config carries `confidence` fields (`measured`,
`measured_single_point`, `assumed`) and they are load-bearing - a value that rests on one
observation must say so.

Corollary: **do not invent precision.** The sell-timing model is deliberately unbuilt
because there is no sell-through history to fit it against, and a fabricated
probability-of-sale curve would look authoritative while being made up. Show the tradeoff
and say what is unknown.

## 5. Known constraints

- **GitHub Actions cannot scrape.** Measured, not assumed: 403 from Ticketmaster,
  SeatGeek, TickPick, and StubHub. TM's block page names the network as the cause and
  prints the Azure runner IP. This is IP reputation, not fingerprinting - browser flags
  will not fix it. See ops#4 / ops#16.
- **Local Chromium is missing system libs** (`libnspr4`). Needs
  `sudo npx playwright install-deps chromium`. See ops#2.
- **Git never forgets.** Anything committed is permanent. Do not commit raw scrape
  snapshots and plan to prune them - deleting a file does not remove its blobs. Raw data
  goes to Actions artifacts; only small aggregates get committed.

## Commands

```bash
npm run dev            # local app
npm run build          # type-check, build, enforce privacy checks
npm run schedule       # refresh + VALIDATE data/schedule.json against the NHL API
npm run check:privacy  # privacy checks alone
node scripts/probe_browser.mjs --label local   # source reachability (needs local Chromium)
```

`scripts/fetch_schedule.py` validates rather than trusts: the tier table was transcribed
by hand from a graphic, so every entry is cross-checked against the live NHL schedule on
date *and* opponent, with orphan and count checks. If it exits non-zero, something real
changed - do not paper over it.

## Conventions

- Comments explain **why**, especially why an approach was rejected. Several bugs here
  were fixed twice because the first fix's reasoning was not written down.
- Verify claims by running something. This codebase has repeatedly produced
  confident-looking wrong answers - a probe that could not distinguish "blocked" from
  "needs JavaScript", a privacy check that missed git history, an artifact upload that
  silently discarded every file.
- No test suite exists yet (ops#17). Until there is one, verify behaviour explicitly
  rather than assuming a clean build means correct.

## Setup on a fresh clone

```bash
npm install
git config core.hooksPath .githooks   # enables the commit-msg private-value check
```

`.private-patterns` is local-only and gitignored, so a fresh clone cannot run the literal
or history privacy passes, and the commit-msg hook will say so rather than passing
silently. Recreate it (one private literal per line) before relying on those checks.

`.privacy-accepted` records reviewed-and-accepted history findings by SHA only. Accepted
findings are still printed - an accepted risk stays visible rather than disappearing.
