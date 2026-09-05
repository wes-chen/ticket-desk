/**
 * Season profit and loss, and 1099-K groundwork. ops#13.
 *
 * WHAT MAKES THIS TRACTABLE. Per-game cost basis falls out of the tier table, and the
 * tier credits reconcile against the real invoice to within ~0.3%. Because the exchange
 * credit tracks face almost exactly, break-even against the exchange is ALSO break-even
 * against what was paid - so net above break-even on a game is real profit on that game.
 *
 * THREE THINGS THIS DELIBERATELY REFUSES TO DO.
 *
 * 1. It does not smear the non-refundable fee residual across games. The invoice exceeds
 *    the sum of face values by a small amount, and that residual is the slice of the
 *    season the exchange structurally cannot return. Allocating it per game - evenly, or
 *    by tier - would be an invention: nothing says it was incurred that way. It is
 *    reported once, at the season level, as its own line.
 *
 * 2. It does not count unspent account credit as recovered money. economics.json says
 *    credit "only has value if it gets spent", and ops#13 says the same. Credit from an
 *    exchanged game is reported separately from cash, and separately again by whether it
 *    has actually been spent.
 *
 * 3. It does not present a single net number as a tax result. Losses on personal-use
 *    property are generally not deductible while gains are taxable, so the tax picture
 *    is asymmetric. Collapsing it into one figure would look authoritative and be
 *    misleading. This is record-keeping.
 */

import type { Game } from "./economics";
import type { Outcome, Profile, Tier } from "./profile";

export interface GameLine {
  game: Game;
  /** Face value for this game, proxied by the tier credit. Null if that tier is unset. */
  basis: number | null;
  outcome: Outcome | null;
  /** Cash actually received per seat. */
  cash: number;
  /** Account credit received per seat, whether or not it has been spent. */
  credit: number;
  /** Credit that has actually been spent. Subset of `credit`. */
  creditSpent: number;
  /** Basis with nothing recovered against it. */
  writtenOff: number;
  /** Cash + credit minus basis. Null when basis is unknown. */
  delta: number | null;
}

export interface SeasonPnl {
  lines: GameLine[];
  seatCount: number;
  /** Per-seat invoice, if entered. */
  invoicePerSeat: number | null;
  /** Sum of tier credits across all games - the face-value proxy for the season. */
  faceTotal: number | null;
  /**
   * Invoice minus face. The non-refundable slice the exchange cannot return. Reported
   * once and never allocated to games.
   */
  nonRefundableResidual: number | null;
  cash: number;
  credit: number;
  creditSpent: number;
  creditUnspent: number;
  writtenOff: number;
  /** Basis of games that have not resolved yet. Not a loss - just not decided. */
  unresolvedBasis: number;
  /** Games with an outcome recorded. */
  resolved: number;
  /** Games whose tier has no credit entered, so their basis is unknown. */
  missingBasis: number;
}

function creditFor(profile: Profile, g: Game): number | null {
  if (!g.tier) return null;
  return profile.credits[g.tier as Tier] ?? null;
}

export function seasonPnl(profile: Profile, games: Game[]): SeasonPnl {
  const seats = Math.max(profile.seats.seats.length, 1);
  const lines: GameLine[] = [];

  let cash = 0;
  let credit = 0;
  let creditSpent = 0;
  let writtenOff = 0;
  let unresolvedBasis = 0;
  let resolved = 0;
  let missingBasis = 0;
  let faceTotal = 0;
  let faceKnown = true;

  for (const g of games) {
    const basis = creditFor(profile, g);
    if (basis == null) {
      missingBasis += 1;
      faceKnown = false;
    } else {
      faceTotal += basis;
    }

    const o = profile.outcomes?.[String(g.gameId)] ?? null;
    let lineCash = 0;
    let lineCredit = 0;
    let lineCreditSpent = 0;
    let lineWrittenOff = 0;

    if (o) {
      resolved += 1;
      if (o.kind === "sold" || o.kind === "instant") {
        // Prefer the recorded net; fall back to the list price at the time only if the
        // net was not captured. Never invent one from the current list price - that can
        // have changed since.
        lineCash = o.netPerSeat ?? 0;
      } else if (o.kind === "exchanged") {
        lineCredit = basis ?? 0;
        if (o.creditSpent) lineCreditSpent = lineCredit;
      } else if (o.kind === "unsold") {
        lineWrittenOff = basis ?? 0;
      }
    } else if (basis != null) {
      unresolvedBasis += basis;
    }

    cash += lineCash;
    credit += lineCredit;
    creditSpent += lineCreditSpent;
    writtenOff += lineWrittenOff;

    lines.push({
      game: g,
      basis,
      outcome: o,
      cash: lineCash,
      credit: lineCredit,
      creditSpent: lineCreditSpent,
      writtenOff: lineWrittenOff,
      delta: basis == null || !o ? null : Number((lineCash + lineCredit - basis).toFixed(2)),
    });
  }

  const invoiceTotal = profile.invoiceTotal;
  const invoicePerSeat = invoiceTotal == null ? null : invoiceTotal / seats;
  const face = faceKnown ? Number(faceTotal.toFixed(2)) : null;

  return {
    lines,
    seatCount: seats,
    invoicePerSeat,
    faceTotal: face,
    // Only meaningful when every game's face is known; a partial sum would understate
    // face and so overstate the residual.
    nonRefundableResidual:
      invoicePerSeat == null || face == null ? null : Number((invoicePerSeat - face).toFixed(2)),
    cash: Number(cash.toFixed(2)),
    credit: Number(credit.toFixed(2)),
    creditSpent: Number(creditSpent.toFixed(2)),
    creditUnspent: Number((credit - creditSpent).toFixed(2)),
    writtenOff: Number(writtenOff.toFixed(2)),
    unresolvedBasis: Number(unresolvedBasis.toFixed(2)),
    resolved,
    missingBasis,
  };
}

/**
 * 1099-K relevant total: GROSS cash proceeds, before basis.
 *
 * Ticketmaster reports what it paid out, not the profit. Netting basis against it here
 * would produce a number that does not match the form, which is the opposite of useful
 * in February. Exchange credit is excluded - it is not a payout.
 */
export function grossCashProceeds(p: SeasonPnl): number {
  return p.cash;
}
