# Working in this repo

Read `README.md` for what the project is and why. This file is the rules.

## 0. Start sessions from this directory - and if another agent is active, from your own worktree

`~/Dev/ticket-desk` - the code repo. Not the parent, not the ops repo.

Everything needed is reachable from here: the code, this file, and `.claude/issues.json`
pointing at the tracker. The ops repo does not need a local clone - `gh --repo
wes-chen/ticket-desk-ops` reaches its issues fine.

Note that Claude Code keys its memory to the working directory, so a session started
somewhere else gets a *different* memory set and will silently lack this project's
context. Memories were migrated here on 2026-09-03; the copy under the parent directory
is marked stale on purpose.

### More than one agent at a time

**Two agents sharing one checkout is not safe.** `git add -A` from either stages whatever
the other has in progress, and it happened (ops#41): one commit swallowed another
agent's mid-debug script plus a stray `.pyc`, under a message describing neither. The
author of a commit message stops being the author of its changes, which destroys the one
thing this history is good for.

So when a second agent is active:

```bash
scripts/agent_worktree.sh <your-session-id>     # own checkout, branch agent/<id>
scripts/agent_worktree.sh --list
scripts/agent_worktree.sh --remove <id>
```

That script exists rather than a bare `git worktree add` because of the memory trap
above: a worktree at a new path gets a **fresh, empty memory set**, silently losing every
measured constant this project has accumulated. It symlinks the new path's memory
directory at the canonical one - one memory set, many checkouts - and copies
`.private-patterns` across, without which the literal and history privacy passes skip
and report "clean" on evidence they never gathered.

**Whether or not you use a worktree: stage explicit paths, never `git add -A`.** Cheap,
and it is the discipline that fails first when an agent is mid-debug at 3am.

## 1. This repo is PUBLIC. Never commit personal data.

Non-negotiable, and it has been violated **twice**: first through **form placeholders**
written with real seat and invoice values, then through a **self-test fixture** that used
the real invoice total as its example value (ops#29). Both times the config files were
scrubbed correctly. Personal data leaks through UI copy, examples, comments, test
fixtures, commit messages, and docs just as readily as through config.

**What is forbidden is the LINKAGE, not the field.** This wording matters, because the
earlier version banned "listing prices" outright while `config/economics.json` had been
committing them all along as fee-calibration data - a contradiction the README stated the
other way round (ops#21). A rule that the codebase visibly violates is a rule people stop
reading.

Never write into this repo anything that ties **our seats or our account** to a value:

- Seat section / row / seat numbers - never, in any form
- Season invoice totals, or any amount paid
- Exchange credit amounts per tier
- Listing prices, payouts, or offers **attributable to our listings** - which games we
  have listed, at what price, with what net
- Account, listing, or order identifiers

These live in **browser `localStorage`** (entered by the user through the app's setup
screen) and in the **private ops repo**. Nowhere else.

**Fine here**, because they carry no seat and no account:

- Fee *ratios* and the isolated (list, net) pairs they are derived from. A `$70 -> $63.00`
  pair is a measurement of Ticketmaster's fee, not a statement about our seats. It is the
  audit trail for the most load-bearing constant in the model, and moving it out would
  cost more than it protects.
- The instant-offer formula.
- Other people's public listing prices - the whole collected market series.
- Published team pricing, such as the section/band marketing table.

The test: could a reader connect this number to **our** seats or **our** account? If yes
it is private, whatever field it lives in. If no, it is a market observation.

Corollary for fixtures and examples: **plausible means real.** Use absurd values
(`11111111`), never a realistic-looking one. That is precisely how ops#29 happened.

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

### Close what you resolve, and say why

**An issue whose work is done gets a closing comment and then gets closed, in the same
sitting.** Not "suggest closing" - closed. Fourteen issues once sat open with their
resolutions already written in comments, which makes the backlog lie about what is left
and makes the next session re-read finished work to find out.

A closing comment records the **resolution**, not just the fact of it: what shipped,
where the delivered thing deviated from what was asked and why, and what was
deliberately *not* done. Start it with the literal marker so it is machine-findable:

```
**Closing - <why>**
```

Two rules, both checkable:

- an issue with a resolution written must not be open
- a closed issue must carry a closing comment - a resolution nobody recorded is one
  nobody can audit six months later

`npm run issues:audit` checks both against the live tracker. It needs network and `gh`
auth, so it is **not** part of `npm test`, which is deliberately offline; its classifier
is pure and self-tested. Run it after any batch of issue work.

Do **not** close an issue that is merely blocked, waiting on Wesley, or partially
delivered - say so in a comment and leave it open. Where the remaining work is genuinely
a different problem, close it and point at the issue that owns that problem, rather than
retitling and carrying two issues for one gap.

### Issue types are contracts, not labels

Wesley is the **product manager**: he decides, agents implement, and this tracker is the
interface between those two jobs. That only works unattended if a session can route an
issue without him. So every issue carries exactly one `type:` label, and **the type says
who acts and what closing it has to show**:

| type | who acts | closing requires |
| --- | --- | --- |
| `type:decision` | Wesley | `**Decision (recorded)**` - the choice, his own words quoted, and what changed |
| `type:input` | Wesley | `**Input accepted**` - the paste received **and** its validator's output |
| `type:build` | agent | merged PR, acceptance met, a test or a stated exemption |
| `type:research` | agent | `**Finding**` - a measurement with its `confidence` level |
| `type:meta` | agent | the actual rule diff, merged |
| `type:incident` | agent | `**Cause**` **and** `**Guard**`, separately |

`npm run issues:audit` enforces all of it, plus: an **open** issue with no type is
unroutable and flagged; two types at once is flagged; a `claimed` label with no
`**Claiming**` comment is a stale lock from an agent that died mid-run. Closed issues are
exempt from the type requirement - 21 of them predate the scheme, and reflagging history
forever is the noise that got the old empty-issue rule deleted.

`type:build` and `type:meta` deliberately carry no extra marker. Their real contract is a
merged PR, which lives in git rather than in a comment, and a regex hunting for commit
references would flag every legitimately abandoned build issue. Enforcing it here would
be theatre - the gap is deliberate and documented rather than quietly missing.

**An agent never idles waiting for Wesley.** Hitting a fork whose answer is a *preference*
rather than a *fact* means filing a `type:decision` issue - with options, consequences, a
recommendation with honest confidence, and a self-contained copy-paste prompt - and then
moving to the next ticket. "You decide" is not an acceptable output: it hands work back to
the PM, which is the one thing this arrangement exists to remove.

Likewise a `type:input` issue must carry numbered steps, a format spec, a **worked example
in absurd values**, and the name of the validator that grades the paste. An input contract
with no validator is a wish - say so in the header and file the `type:build` for the
validator, rather than asking for data nothing can check.

The full protocol, the ranking rubric, the trust ladder and the four agent role prompts
live in `harness/` in the ops repo. `scripts/check_issues.py` here is the source of truth
for the contracts themselves, because CI cannot read a private repo.

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

Two of those are now enforced on the remote rather than left to an agent's judgement:
`main` rejects force-pushes and branch deletion, with `enforce_admins` on. That last flag
is the load-bearing one - agents push with Wesley's own admin token, so without it the
protection would have exempted exactly the actor it was meant to stop. Pull requests are
deliberately **not** required: the collector and schedule workflows push straight to
`main`, and requiring review would stop the data collection this rule exists to protect.

### What an agent may merge unattended

Trust rung **2**, recorded in `harness/trust.json` in the ops repo. Everything merges
without Wesley **except** a protected set - the privacy checks, the git hooks,
`.private-patterns`, `.privacy-accepted`, and `trust.json` itself - plus repo settings,
secrets, history rewrites, and anything touching money or the seller account.

That set is not "the risky files". It is precisely **the surfaces that, if wrong, disable
the ability to notice they are wrong.** An agent may open a PR against them and argue for
it; merging needs a decision.

Self-*started* work - work an agent proposed rather than was assigned - additionally needs
the blast-radius rule in `trust.json` to pass, all six clauses: reversible by one revert,
no model constant, no money math, no new external dependency, under ~400 lines, and both
`npm run build` and `npm test` green. The rule is self-assessed, which is exactly why it
is written down rather than judged, and why a reviewer agent with fresh context reads
every PR. **A worker does not merge its own PR** - the same context agreeing with itself
is not a review, and this project's characteristic failure is confident-looking wrongness
that more tests do not catch.

Rung 4 is actions that move money. It is **not granted**.

Every run appends to `log/YYYY-MM-DD.md` in the ops repo, including what was **abandoned**
and why. That field is required: the reject and revert rates in those files are the only
evidence for ever widening any of this, and a search process that records only its
successes cannot be audited.

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
- **ScoreBig and TicketNetwork are usable, and were only ever UNREAD.** Measured
  2026-09-05, residential, plain HTTP, logged out: both return **200** with prices in
  `ld+json` - ScoreBig as `AggregateOffer` (`lowPrice`/`highPrice`), TicketNetwork as
  `Offer` (`price`). TicketNetwork carries **47 SAP Center events in a single page
  fetch**; ScoreBig carried 11. Both were previously recorded as 404 in ops#26 because
  **the URLs were guessed**, which is indistinguishable from a block and is precisely
  what left Gametime unmeasured through all of ops#4. Their own sitemaps resolved them in
  minutes. Neither needs a browser or a spoofed User-Agent - plain `urllib` gets the same
  bytes. **Untested from a datacenter IP**, so probe the runner before scheduling
  anything (ops#33, ops#36).

  A `grep` for `$`-prefixed tokens scored both pages as having zero prices. The prices are
  numeric ld+json fields with no dollar sign anywhere. Fifth instance in this project of
  the instrument being the thing that was wrong.

  **Both now measured from a runner too** (2026-09-05, ops#39): TicketNetwork 200 /
  202,701B / 20 prices, ScoreBig 200 / 316,460B / 34 prices. Both collect daily. So the
  reachability grid over plain HTTP is now **four for four** - TickPick, Gametime,
  TicketNetwork and ScoreBig all work from residential *and* datacenter; Ticketmaster,
  SeatGeek and StubHub refuse from both. The same run re-confirmed TickPick is blocked
  under headless Chromium from the runner while fine over plain HTTP, so the browser row
  of the 2x2 still holds.

  One loose end, deliberately left as a log line to read rather than an issue:
  TicketNetwork's runner body is 202KB against 288KB residential with the same price
  count. Because it is a rolling source, being served fewer events would not fail - it
  would silently move the horizon. Compare the first scheduled run against the
  residential baselines of TicketNetwork 29/44 and ScoreBig 19/44.

- **The two new sources disagree about time, and both are right in their own way. Do not
  unify their joins.** Measured 2026-09-05:

  | source | `startDate` | correct join |
  | --- | --- | --- |
  | TicketNetwork | `-07:00` in September - a real, DST-aware offset | normalise to UTC, join on `startTimeUTC` |
  | ScoreBig | `-08:00` in **every** month, including September | **discard the offset**, join on local wall clock |

  ScoreBig stamps a fixed `-08:00` year-round. Honouring it matched **10/44** games;
  joining on wall clock matched **19/19**. The failure is silent - it keeps the PST games
  and drops every PDT one, which reads as ordinary partial coverage rather than as a bug.
  `collect_scorebig.py` therefore slices the timestamp string instead of parsing it,
  because parsing invites honouring the tzinfo that is the problem. A later refactor that
  "unifies" these two joins will break one of them (ops#36).

  **ScoreBig also serves prices as strings** (`"15.20"`), where every other source serves
  numbers. Coerced at that collector's boundary - untouched, it would have reached
  `summarize_market.py`'s delta arithmetic, where `"9.00" > "15.20"` is true.

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
- **Actions cron times are nominal, not actual.** Observed twice, both on the schedule
  refresh nominally at 13:17 UTC: it ran at 16:52 UTC (**215 min late**) and at 16:04 UTC
  (**167 min late**). So hours-late is the norm here, not an outlier - but n=2, so treat
  the range rather than either number as the expectation. GitHub queues scheduled runs at low priority, and
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
