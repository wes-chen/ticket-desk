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
| structural | forbidden keys in committed JSON and JSONL, recursively under `config/`, `data/`, `tests/` | everywhere, including CI |
| literal | `dist/` + every git-tracked file | only when `.private-patterns` exists |
| history | git log content **and** commit messages | only when `.private-patterns` exists |

The structural pass walks `config/`, `data/`, and `tests/` **recursively**, and parses `.jsonl`
line by line as well as `.json`. It originally globbed only the top level for `*.json`,
which meant the first collector to land a nested or line-delimited store would have
created a committed data surface the check silently ignored - and whole-file
`json.loads` on a JSONL store would have reported a false "invalid JSON", training
people to ignore the output. When you add a new committed data surface, check that this
pass actually covers it.

`.private-patterns` is gitignored and local-only - committing it would defeat its
purpose. **A fresh clone will not have it, so the literal and history passes silently
skip.** A clean build on a fresh clone is therefore weaker evidence than it looks. If
you are about to make a visibility change, recreate that file first.

History findings are **automatically fatal when the remote is public** (checked via `gh`,
not assumed), and can be forced with `PRIVACY_HISTORY_FATAL=1`. This is deliberate: the
original version was advisory with a hardcoded "this repo is private" note, which stayed
reassuring after the repo went public and let a real leak through.

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

## 5. Autonomous sessions should push

Commit and **push** work that is finished and passes `npm run build`. Do not leave it
sitting on local `main`.

A scheduled workflow only exists on the remote: a cron on a local branch never fires, so
unpushed collector work collects nothing, and collected data only accrues forward.
Holding a clean commit back costs days of market data that cannot be recovered later.

The privacy checks are the gate, not a review by Wesley. Before pushing, confirm
`npm run build` is clean **and** that the literal and history passes actually ran rather
than skipping - a fresh clone without `.private-patterns` reports "clean" on evidence it
never gathered.

Stop and ask for anything that is not an ordinary push: force-pushing, changing repo
visibility, rewriting history, adding a secret, or deleting data.

## 6. Known constraints

- **The block follows the BROWSER, not the IP - for TickPick.** This rule used to read
  "GitHub Actions cannot scrape... this is IP reputation, not fingerprinting", which was
  drawn from one cell of a 2x2 and was wrong as a generalisation. The full grid, measured
  2026-09-05 on the same URL:

  |  | plain HTTP | headless Chromium |
  | --- | --- | --- |
  | residential | TickPick 200, 1.4MB, 31 prices | TickPick 403 `Just a moment...` |
  | datacenter (Actions) | TickPick 200, 1.4MB, 31 prices | TickPick 403 |

  So **TickPick collects fine from free CI over plain HTTP**, and reaching for Playwright
  is what breaks it. `scripts/collect_tickpick.py` therefore uses `urllib` on purpose;
  do not "upgrade" it to a real browser.
- **Ticketmaster, SeatGeek and StubHub are still blocked**, and that part of ops#4 holds.
  TM serves a 403 IP-block page to a runner (naming the Azure IP) and a `tm-bl` device
  challenge to residential plain HTTP; SeatGeek 403s everywhere measured; StubHub returns
  a JS shell with no prices and 403s intermittently. TM is the channel actually sold on,
  so its prices remain uncollected - see ops#16.
- **A residential IP is necessary but not sufficient.** Measured 2026-09-05 from
  Wesley's machine, against the same targets ops#4 probed from Actions:
  **TickPick** 403 -> **200 with real listing content** (1.4MB, 31 distinct prices);
  **SeatGeek** 403 -> **403, blocked on residential too**;
  **Ticketmaster** 403 IP-block page -> **401 `{"response":"identify"}` with a `tm-bl: 1`
  header, which is a bot/device check rather than an IP refusal** - plain HTTP cannot
  settle whether a real browser gets through, so that question needs ops#2;
  **StubHub** 403 -> 200 but a JS shell with no prices in the HTML, and it returned 403
  on a second attempt minutes later, so its posture is inconsistent;
  **Gametime** still untested, its URL 404s. See ops#16.
- **The Discovery API is NOT subject to the ops#4 block.** `resolve_tm_events.py`
  resolved all 44 events from a GitHub Actions runner. The block applies to TM's web
  properties, not `app.ticketmaster.com`.
- **The Ticketmaster Discovery API publishes no prices for this venue.** Measured
  2026-09-05 with a live key: `priceRanges` is absent from all 44 home events on both
  the search and detail endpoints, at HTTP 200. Discovery is an id and metadata
  resolver, not a price source. `resolve_tm_events.py` re-probes for the field every
  run and says so loudly if it ever appears. See ops#5.
- **There are two Ticketmaster id namespaces.** The `tmEventId` in `schedule.json` is a
  LEGACY web-URL id (`1C0064E7...`) and Discovery 404s on it; Discovery uses
  `G5vYZ_...`. Both are real and both are needed - the legacy id keys TM's web pages,
  which is what the scraper will use. `data/tm_events.json` carries both for all 44
  games.
- **Actions cron times are nominal, not actual.** Observed once (n=1, so do not treat the
  magnitude as typical): the schedule refresh nominally at 13:17 UTC actually ran at
  16:52 UTC - **215 minutes late**. GitHub queues scheduled runs at low priority, and
  off-the-hour minutes help but do not eliminate it. Consequences: a "daily" collector is
  really "roughly daily, whenever GitHub gets to it", and a run that looks hours overdue
  is probably delayed rather than broken - check `gh run list` for a queued run and the
  workflow's `state` before diagnosing further. This is why the gap detector's staleness
  threshold is **2 days rather than hours**, and it should stay that way.
- **Local Chromium works** as of 2026-09-05 (`sudo npx playwright install-deps chromium`
  was run; verified by launching it). ops#2 is closed. Note it is of limited use for
  collection - see the browser-fingerprinting row above.
- **Git never forgets.** Anything committed is permanent. Do not commit raw scrape
  snapshots and plan to prune them - deleting a file does not remove its blobs. Raw data
  goes to Actions artifacts; only small aggregates get committed.

## Commands

```bash
npm run dev            # local app
npm run build          # type-check, build, enforce privacy checks
npm run schedule       # refresh + VALIDATE data/schedule.json against the NHL API
npm run check:privacy  # privacy checks alone
npm run check:bands    # validate config/price_bands.json (section -> price band map)
npm run check:tiermarket   # cross-check the tier table against collected market prices
npm run collect:tickpick   # TickPick prices -> data/market/tickpick.jsonl (+ raw to raw-out/)
npm run resolve:tickpick   # rebuild data/tickpick_events.json from TickPick's sitemap
npm run summarize:market   # derive data/market/summary.json, which the app imports
npm run resolve:tm     # TM Discovery event ids -> data/tm_events.json
npm run test:tm        # resolver self-test against real captured fixtures; no key, no network
python3 scripts/probe_sources.py --label local  # HTTP-level reachability (no browser needed)
python3 scripts/probe_sources.py --self-test    # replay measured responses through verdict()
node scripts/probe_browser.mjs --label local   # source reachability (needs local Chromium)
```

`resolve_tm_events.py` needs `TM_DISCOVERY_API_KEY`, from the environment or from
`.env.local` (gitignored). It is a read-only public-data key with no connection to the
seller account. It runs inside the schedule workflow, after `fetch_schedule.py`, and
validates the join on **three independent keys** - local date, the opponent's name inside
the TM event name, and the legacy id parsed from the TM event `url` matching the
`tmEventId` already in `schedule.json`. Any disagreement fails the run and writes
nothing: a wrong id map is worse than a stale one.

`scripts/check_price_bands.py` reports **NOT CHECKED**, not "clean", while the section
assignment is missing - the band prices are transcribed but no section has been placed
yet. Its placement checks (arc contiguity and mirror symmetry around each level's ring)
are written and self-tested against a deliberately misfiled section, so they work the
moment the chart is transcribed. See ops#19.

`scripts/collect_tickpick.py` is the working price collector. Plain HTTP, no browser,
44/44 home games, running daily in Actions. **Aggregates commit; raw ld+json goes to a
90-day Actions artifact and never into git** (~102KB/day raw). It is NOT seat-level:
TickPick's listing grid is behind an `/ajax/` path its `robots.txt` disallows, so
section-level comps need a different source. It is also a **comp market, not the channel
these tickets are sold on** - never read these numbers as achievable prices.

`scripts/check_tier_market.py` is an independent check on `config/tiers.json`, which was
transcribed by hand from a JPEG. Thousands of unrelated TickPick sellers have never seen
that graphic, so if a better tier commands a higher ask, the transcription is probably
right. First measurement: Spearman -0.914, tier medians perfectly monotone, no outliers.
It tests CONSISTENCY, not correctness - two adjacent tiers swapped where demand is equal
would still pass.

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
  silently discarded every file, self-test fixtures that asserted an API field copied
  from documentation nobody had checked, and a probe that truncated responses at 400KB
  and scored a working 1.4MB source as empty.
- **Check the instrument before believing the measurement.** Four of those failures were
  the measuring tool, not the thing measured. When a probe says a source is unusable,
  confirm it independently - `curl` the URL by hand - before acting on it.
- **`npm test` runs the suite** (ops#17). Nine scripts self-test against *real captured
  fixtures* - responses actually received from the APIs, plus a throwaway git repo for
  the privacy history pass - never against shapes copied from documentation. That
  distinction is not pedantry: the Discovery collector's fixtures asserted a
  `priceRanges` field taken from TM's docs, passed cleanly, and were wrong about the one
  field the whole design rested on.
- The runner **fails if a script has neither a self-test nor a stated exemption**, so
  adding untested code is caught rather than going unnoticed. Exemptions live in
  `scripts/run_tests.py` with reasons.
- A clean build still is not proof of correctness. Verify behaviour by running the thing.

## Setup on a fresh clone

```bash
npm install
git config core.hooksPath .githooks   # enables the commit-msg private-value check
```

`.private-patterns` is local-only and gitignored, so a fresh clone cannot run the literal
or history privacy passes, and the hooks will say so rather than passing silently.
Recreate it (one private literal per line) before relying on those checks.

Two hooks, covering different surfaces:

- **`commit-msg`** rejects a private value in the commit *message*, which is the one
  artifact that cannot be quietly amended once pushed.
- **`pre-commit`** rejects a private value in staged *content*. Added in response to
  ops#29, where a self-test fixture used the real season invoice total and reached the
  public repo - the build-time literal pass caught it, but only after the push. It scans
  added lines only, so the commit that *scrubs* a value is not itself blocked.

**`npm test` does not run the privacy passes; `npm run build` does.** That gap is exactly
what ops#29 fell through - the fast command is not the safe one. The pre-commit hook now
covers it structurally, but run `npm run build` before pushing regardless.

`.privacy-accepted` records reviewed-and-accepted history findings by SHA only. Accepted
findings are still printed - an accepted risk stays visible rather than disappearing.
