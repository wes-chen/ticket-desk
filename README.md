# Ticket Desk

Decision support for a season ticket holder: should I list this game, at what price, or return it
for credit?

Currently configured for the **San Jose Sharks, 2026-27 season, 44 home games**. Everything
team-specific lives in `config/`; the model itself is not team-specific. Everything *person*-specific
lives in the browser and never touches this repo.

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

## Invoice reconciliation

Tier credits are hand-entered, so the app checks them against the season invoice:

```
sum(tierCredit x gamesInTier)  ==?  invoiceTotal / seatCount
```

Three outcomes, all informative:

- **Sums to the invoice** - credit equals face, no hidden fees to price around.
- **Falls short** - the residual is non-refundable service fee, the slice of the season the exchange
  structurally cannot return.
- **Overshoots** - a credit was mistyped, caught before it silently misprices a season.

It also independently validates the hand-transcribed tier table: a misfiled game skews the sum by
the price gap between two tiers, not by a plausible-looking fee.

## Privacy model

GitHub Pages sites are reachable by anyone, even when the source repo is private - Pages access
control is Enterprise-only. So the deployed bundle is deliberately **empty of personal data**:

| Public, in this repo | Private, browser only |
| --- | --- |
| NHL schedule, 44 home games | Seat section / row / numbers |
| Tier assignment per game (from the public marketing graphic) | Season invoice total |
| Seller fee rate, 48h deadline, the model itself | Exchange credit amounts per tier |
| | Per-game list prices |

Personal data lives in `localStorage` and moves between devices through a URL **fragment**.
Fragments are never transmitted in an HTTP request - they don't reach GitHub and don't appear in
access logs. The encoding is obfuscation, not encryption: anyone holding the link can read it.

`scripts/check_privacy.py` enforces this and runs as part of `npm run build`. It exists because the
leak already happened once: the config files were scrubbed correctly, but form *placeholders* were
written using real seat and invoice values and shipped them anyway. Scrubbing config is not
sufficient - personal data leaks through UI copy, examples, and documentation just as easily.

Two halves:

- **Structural** - rejects forbidden keys (`creditPerSeat`, `invoiceTotal`, `costBasis`, ...) in
  committed JSON. Runs everywhere, including CI.
- **Literal** - greps the built output *and every git-tracked file* for real private values listed in
  a gitignored `.private-patterns`. Local only, by design: committing that file would defeat it.

Both halves are verified against deliberately introduced leaks.

## Layout

```
config/economics.json       fee model, exchange rules, instant-offer findings  (public)
config/tiers.json           game -> tier assignments                           (public)
scripts/fetch_schedule.py   pulls the NHL public API and VALIDATES the tier join
scripts/check_privacy.py    fails the build if anything personal would ship
scripts/make_icons.py       generates PWA icons with no image dependency
data/schedule.json          generated: 44 home games with tier attached
src/lib/economics.ts        the money model
src/lib/profile.ts          private profile + URL-fragment device transfer
```

Schedule source, no auth required:
`https://api-web.nhle.com/v1/club-schedule-season/SJS/20262027`

`fetch_schedule.py` does not just fetch - it cross-checks every tier entry against the live schedule
on both date and opponent, and fails loudly on any mismatch, orphan, or count drift. The tier table
was transcribed from a JPEG by hand; one misread date would silently misprice a game for a whole
season.

## Status

**Built:** schedule ingestion + validation, tier/fee/exchange/break-even math, invoice
reconciliation, private profile with cross-device transfer, installable PWA with offline support,
enforced privacy checks.

**Not built yet:** the market collector, and the sell-timing curve.

The timing model is deliberately absent rather than faked. This is a first selling season, so there
is no sell-through history to fit against - any probability-of-sale number today would be invented.
The collector has to run first and accumulate price-vs-days-to-puck-drop data; the model comes after.

## Development

```bash
npm install
npm run schedule    # refresh + validate data/schedule.json
npm run icons       # regenerate PWA icons
npm run dev
npm run build       # type-check, build, then enforce the privacy checks
```
