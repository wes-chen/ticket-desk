/**
 * Instant-offer history. ops#12.
 *
 * Ticketmaster's "Sell Your Tickets Now" buyout is a buyer's BID at a round dollar
 * price, paid net of the same seller fee. Four samples, four exact round numbers:
 *
 *   $24.30 / 0.9 = $27    $27.90 / 0.9 = $31
 *   $87.30 / 0.9 = $97    $96.30 / 0.9 = $107
 *
 * Why it is worth tracking: the authenticated seller page leaks a live market price for
 * free, from the channel these tickets are actually sold on. Ticketmaster blocks
 * collection from both a CI runner and a residential browser, so this manual-paste
 * number is the ONLY price signal we have from our own market. TickPick is a neighbour.
 *
 * The caveat that must not be lost, and is repeated here because it will be: this is a
 * BID, not an ask. A floor on market value, not fair value. The spread between it and
 * what a patient listing clears is exactly what patience earns.
 */

export interface InstantOffer {
  on: string;
  offerPerTicket: number;
}

/**
 * What the buyer is actually bidding, before the seller fee is taken out.
 *
 * Inverting the fee is what revealed the structure: the offers are round dollars, which
 * would not happen at any other rate. That was originally used as evidence FOR the 10%
 * rate, which was circular - it assumed 0.9 to get there. The rate is now measured
 * directly at two price points, so this inversion is a consequence rather than an
 * argument.
 */
export function impliedBid(offerPerTicket: number, feeRate: number): number {
  return offerPerTicket / (1 - feeRate);
}

/** Is the implied bid landing on a round dollar, as all four samples did? */
export function isRoundDollar(bid: number, tolerance = 0.01): boolean {
  return Math.abs(bid - Math.round(bid)) <= tolerance;
}

export type OfferVerdict = "below_credit" | "above_credit" | "no_credit";

/**
 * The instant offer versus the exchange credit.
 *
 * In all four samples so far the offer netted BELOW the tier credit, including for
 * marquee A+ opponents. If that holds, the instant offer is never the right exit while
 * the exchange window is open - it is strictly dominated by a guaranteed larger credit.
 */
export function verdictAgainstCredit(
  offerPerTicket: number,
  credit: number | null,
): OfferVerdict {
  if (credit == null) return "no_credit";
  return offerPerTicket < credit ? "below_credit" : "above_credit";
}

export interface OfferSeries {
  gameId: number;
  offers: InstantOffer[];
  latest: InstantOffer | null;
  /** Change in the offer since the first recorded observation, or null with one point. */
  delta: number | null;
}

export function series(gameId: number, offers: InstantOffer[] | undefined): OfferSeries {
  const sorted = [...(offers ?? [])].sort((a, b) => a.on.localeCompare(b.on));
  const latest = sorted.length ? sorted[sorted.length - 1] : null;
  return {
    gameId,
    offers: sorted,
    latest,
    // Absent rather than zero on a single observation: rendering 0 would read as
    // "flat", which one point cannot support.
    delta:
      sorted.length > 1
        ? Number((sorted[sorted.length - 1].offerPerTicket - sorted[0].offerPerTicket).toFixed(2))
        : null,
  };
}
