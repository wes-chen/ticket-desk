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

import { exchangeIsRealFloor, type Game } from "./economics.ts";
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
 * The invoice-versus-face residual that has actually been measured: about 0.3%.
 *
 * The invoice slightly exceeds the sum of tier credits, and that gap is the slice of the
 * season the exchange structurally cannot return. It is small and it is real.
 */
export const EXPECTED_RESIDUAL_RATE = 0.003;

/**
 * Above this, the residual is more likely a mistyped tier credit than a fee.
 *
 * Ten times the measured value. Deliberately not tight: the point is to catch a
 * fat-finger, not to police a number that was measured once and may drift. A check that
 * fires on a real 1% residual would be switched off before it ever caught a typo.
 */
export const RESIDUAL_ALARM_RATE = 0.03;

export interface Reconciliation {
  /** Residual as a fraction of the invoice, or null when either side is unknown. */
  rate: number | null;
  /** True when the residual is too large, or negative, to be the fee it claims to be. */
  implausible: boolean;
  message: string | null;
}

/**
 * Judge the reconciliation rather than merely displaying it.
 *
 * The app has always shown "invoice minus total face = non-refundable residual". It never
 * said whether that number was PLAUSIBLE - so a mistyped tier credit renders as a larger,
 * entirely legitimate-looking residual. That matters more than a cosmetic wrong number:
 * tier credits are the cost basis AND the exchange payout, so one bad entry moves every
 * break-even in the model in the same direction, silently.
 *
 * Two ways it can be wrong, and they mean different things:
 *   negative  - total face EXCEEDS what was paid, which cannot happen. A credit is too high.
 *   too large - the residual is many times the measured fee. A credit is too low, or one
 *               tier was entered against the wrong tier.
 */
export function reconciliation(p: SeasonPnl): Reconciliation {
  if (p.invoicePerSeat == null || p.faceTotal == null || p.invoicePerSeat <= 0) {
    return { rate: null, implausible: false, message: null };
  }
  const rate = (p.invoicePerSeat - p.faceTotal) / p.invoicePerSeat;
  if (rate < 0) {
    return {
      rate,
      implausible: true,
      message:
        "Total face exceeds the season invoice, which cannot happen - a tier credit is " +
        "entered too high. Tier credits are both the cost basis and the exchange payout, " +
        "so this moves every break-even in the model.",
    };
  }
  if (rate > RESIDUAL_ALARM_RATE) {
    return {
      rate,
      implausible: true,
      message:
        `The residual is ${(rate * 100).toFixed(1)}% of the invoice, against a measured ` +
        `~${(EXPECTED_RESIDUAL_RATE * 100).toFixed(1)}%. That is more likely a mistyped ` +
        `tier credit than a fee - check the credits against the invoice before trusting ` +
        `any break-even.`,
    };
  }
  return { rate, implausible: false, message: null };
}

export interface DoNothingBaseline {
  /** Cash-equivalent value of exchanging every game that CAN be exchanged, per seat. */
  perSeat: number;
  /** Across all seats. */
  total: number;
  /** Games counted - those with a known credit and a real exchange floor. */
  games: number;
  /**
   * Games excluded because exchanging them yields credit with nowhere to spend it.
   * In practice the final home game: its credit expires at its own puck drop.
   */
  noFloor: number;
  /** Games excluded because their tier has no credit entered. */
  missingBasis: number;
}

/**
 * What the season yields if Wesley does nothing but return every ticket for credit.
 *
 * THE POINT. Every recommendation this tool makes should beat doing nothing, and until
 * now nothing measured against that. A per-game break-even says whether listing beats
 * exchanging THAT game; it never says whether the whole strategy beats the lazy one.
 *
 * NOT simply the sum of tier credits, and the difference is the interesting part.
 * Exchanging the final home game yields credit that can only be spent on a
 * regular-season home game which has not happened - and there is none. That credit is
 * worth zero, so the game is EXCLUDED rather than counted at face. `exchangeIsRealFloor`
 * already encodes this; the naive sum would overstate the baseline by one A+ game.
 *
 * Valued at the haircut, because credit is not cash - it expires at season end, cannot
 * buy playoffs, and does not roll over. Comparing a cash strategy against a credit
 * baseline at face value would flatter the credit.
 */
export function doNothingBaseline(
  profile: Profile,
  games: Game[],
  creditHaircut = 1,
): DoNothingBaseline {
  const seats = Math.max(profile.seats.seats.length, 1);
  let perSeat = 0;
  let counted = 0;
  let noFloor = 0;
  let missingBasis = 0;

  for (const g of games) {
    const basis = creditFor(profile, g);
    if (basis == null) {
      missingBasis += 1;
      continue;
    }
    if (!exchangeIsRealFloor(g, games)) {
      noFloor += 1;
      continue;
    }
    perSeat += basis * creditHaircut;
    counted += 1;
  }

  return { perSeat, total: perSeat * seats, games: counted, noFloor, missingBasis };
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
