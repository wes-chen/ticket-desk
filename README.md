# Ticket Desk

A read-only season dashboard for a San Jose Sharks season ticket holder: 44 home games,
per-game status, market context, and the resale economics. Currently configured for the
**San Jose Sharks, 2026-27 season**.

The site is display-only. There are no inputs, no accounts, no stored profile - updates
happen by messaging Rumi, and the dashboard re-renders from `data/outcomes.json`, which is
regenerated daily from the private ops repo. See `scripts/check_readonly.py`.

## The problem

Every home game has four possible exits, and they pay very differently:

| Exit | Payout | Guaranteed? | Deadline |
| --- | --- | --- | --- |
| List on Ticketmaster and it sells | list x 0.90 | no | puck drop |
| Sharks Exchange "Return For Credit" | tier credit, as **account credit** | yes | **48h before puck drop** |
| Ticketmaster instant "Sell Now" offer | bid x 0.90, cash | yes | puck drop |
| Goes unsold | $0 | - | - |

## The load-bearing insight

The exchange is not an alternative to resale. It is an **expiring floor underneath** resale.

While the exchange window is open, listing at any price above break-even *weakly dominates*
exchanging outright: if it sells you beat the credit, and if it doesn't you delist and return it at
the deadline anyway. Patience is free. The instant the 48-hour window closes that reverses - an
unsold ticket becomes worth exactly zero, and the only question is what clears the market.

**Break-even list price = tier credit / 0.90.**

## Measured mechanics

Established from a real Ticketmaster account rather than assumed. Amounts are deliberately absent
here - see the privacy model below.

- **Seller fee is exactly 10%.** A listing's displayed "you will make" figure is exactly 0.90x the
  list price. Verified to the cent.
- **Exchange credit is face price only**, explicitly excluding service fees already paid.
- **Credit is a pure tier constant.** Samples across different opponents, weekdays, and start times
  matched exactly within each tier. Not opponent-priced, not slot-priced.
- **Delist -> exchange works.** An active listing was delisted and the Return For Credit flow
  accepted it. This is what makes "list high, fall back to the credit" safe in practice rather than
  just in theory.
- **The instant offer is a bid, not a lowball formula.** See below.
- Payout arrives ~7 days post-event and requires taxpayer/TIN info on file. Expect a 1099-K.

## Instant offer: solved

Ticketmaster's "Sell Your Tickets Now" buyout is a buyer's **bid at a round dollar price**, paid out
net of the same 10% seller fee. Across three sampled games, `offer / 0.9` landed on an exact round
number every time.

Two consequences:

1. **The authenticated seller page leaks a live market price for free** - no scraping needed for
   that number. It is login-only, so it's manual-paste data.
2. It is a **bid, not an ask** - a floor on market value, not fair value. The bid/ask spread is
   precisely what a patient listing captures, and measuring it is a core job of the collector.

In every sample so far the implied bid netted *below* the tier exchange credit, including for
marquee opponents. If that holds, the instant offer is never the right exit while the exchange
window is still open.

## Privacy model

GitHub Pages sites are reachable by anyone, even when the source repo is private - Pages access
control is Enterprise-only. So this repo is deliberately **empty of personal data**:

| Public, in this repo | Private, elsewhere |
| --- | --- |
| NHL schedule, 44 home games | Seat section / row / numbers |
| Tier assignment per game (from the public marketing graphic) | Season invoice total |
| Seller fee rate, 48h deadline, the model itself | Exchange credit amounts per tier |
| Collected market prices - other sellers, whole arena | **Our** list prices, nets, and offers |
| Isolated (list, net) pairs, as fee measurements | Which games we have listed, and at what |
| Published team pricing, e.g. the section/band table | Account, listing, and order identifiers |
| Per-game status labels (`listed`, `sold`, ...) + outcome counts | **How many seats we hold** |
| A per-seat season face that **reproduces** a published band price x 44 | A per-seat figure that reproduces none |

The line is the **linkage, not the field**. A `$70 -> $63.00` pair is a measurement of
Ticketmaster's fee and carries no seat; *our* asking price on a named game does. The test: could
a reader connect this number to our seats or our account?

**One named exception** (ops#167, decided 2026-09-15): a per-seat season **face** is public when it
equals `config/price_bands.json -> bands[id].avgPerGame.new x gamesInFullSeason`, because that is
the team's own published pricing. It identifies our price *band*, which is coarse and never
forbidden - section, row and seat stay private, and so does the seat count, since face x count is
the invoice total. CLAUDE.md rule 1 is authoritative and states the test in full; this table follows
it rather than restating it, because ops#21 was the two documents disagreeing.

Personal data lives in the private ops repo and in the chat interface - never in this repo, and
since ops#165, never in the browser either. The old localStorage profile, URL-fragment transfer,
and backup import/export are gone.

`scripts/check_privacy.py` enforces this and runs in CI on every push and PR. It exists because the
leak has happened **twice**: first form *placeholders* written with real seat and invoice values,
then a *self-test fixture* that used the real invoice total as its example value. Both times the
config files were scrubbed correctly. Scrubbing config is not sufficient - personal data leaks
through UI copy, examples, test fixtures, and documentation just as easily. Corollary:
**plausible means real** - example values should be absurd, never realistic.

Three passes:

- **Structural** - rejects forbidden keys (`creditPerSeat`, `invoiceTotal`, `costBasis`,
  `tmListingId`, ...) in committed JSON and JSONL, recursively under `config/`, `data/`, and
  `tests/`. Runs everywhere, including CI.
- **Literal** - greps the deployed files *and every git-tracked file* for real private values listed in
  a gitignored `.private-patterns`. Local only, by design: committing that file would defeat it.
- **History** - git log content *and* commit messages, since scrubbing the tree does nothing about
  commits that already shipped. Automatically fatal when the remote is public.

All three are verified against deliberately introduced leaks. Two hooks back them up:
`commit-msg` for messages, `pre-commit` for staged content.

**A clean build on a fresh clone is weaker evidence than it looks.** Without
`.private-patterns` the literal and history passes silently skip, so CI stays green on
findings only a local build can see.

`scripts/check_readonly.py` is the second gate: it fails if any write path (localStorage,
form elements, URL-fragment imports) reappears in the site, and spot-checks that
`data/outcomes.json` renders the 44-game timeline.

## Layout

```
index.html / styles.css / app.js   the dashboard (static, zero dependencies)
data/schedule.json                 44 home games with tier attached (public NHL data)
data/outcomes.json                 generated daily: per-game status + market medians (anonymized)
scripts/check_privacy.py           fails the build if anything personal would ship
scripts/check_readonly.py          fails if any write path reappears in the site
scripts/run_tests.py               runs every script's --self-test; untested scripts fail
config/economics.json              fee model, exchange rules, instant-offer findings (public)
```

Schedule source, no auth required:
`https://api-web.nhle.com/v1/club-schedule-season/SJS/20262027`

`scripts/fetch_schedule.py` does not just fetch - it cross-checks every tier entry against the live schedule
on both date and opponent, and fails loudly on any mismatch, orphan, or count drift. The tier table
was transcribed from a JPEG by hand; one misread date would silently misprice a game for a whole
season.

`data/outcomes.json` is generated by `scripts/export_public_outcomes.py` in the **private ops repo**,
which reads the listing state and the TM snapshots, keeps the comparable-section filter and all
personal values on the private side, and pushes only the anonymized feed here. It refreshes daily.

## Status

**Built:** read-only dashboard (season mix, 44-game timeline, market pulse, tier context, economics),
anonymized outcomes feed, read-only + privacy gates, schedule ingestion + validation, tier/fee/exchange
math, enforced privacy checks.

**Also built:** the market collector. Two independent sources (TickPick and Gametime) run daily
in GitHub Actions over plain HTTP, 44/44 home games, and cross-check each other -
Spearman +0.994 on game ordering, with the level gap between them monitored so a source going
wrong is detectable. Their prices also independently validate the hand-transcribed tier table
(Spearman -0.92 against tier rank, medians monotone). Aggregates commit here; raw payloads go to
90-day Actions artifacts and never into git.

It is **event-level, not seat-level**: both sources put their listing grids behind
robots-disallowed paths, so per-section comps need a different route. And it is a **comp market,
not our channel** - Ticketmaster blocks collection from a CI runner and a residential browser
alike, so its prices are not collectable.

**Not built yet:** the sell-timing curve.

The timing model is deliberately absent rather than faked. This is a first selling season, so there
is no sell-through history to fit against - any probability-of-sale number today would be invented.
Outcome recording now exists to accumulate that history; the model comes after enough of it.

## Development

```bash
python3 scripts/run_tests.py       # every script's self-test; untested scripts fail
python3 scripts/check_readonly.py  # read-only gate
python3 scripts/check_privacy.py   # privacy gate
python3 scripts/fetch_schedule.py  # refresh + validate data/schedule.json
```

The dashboard itself is static: open `index.html` (served over HTTP so `fetch` works, e.g.
`python3 -m http.server`) after generating `data/outcomes.json`. Deploys to GitHub Pages on
every push to `main`; no build step.
