/**
 * Collected market context.
 *
 * WHAT THIS IS. TickPick's cheapest and priciest listing for a game, across the WHOLE
 * ARENA, all-in (TickPick shows no hidden fees, so these are what a buyer actually
 * pays). Collected daily by scripts/collect_tickpick.py and derived into
 * data/market/summary.json.
 *
 * WHAT IT IS NOT, and the UI must not imply otherwise:
 *
 *  1. Not a comp for a specific seat. The low is almost always an upper-deck single.
 *     Section-level listings would be a real comp; TickPick's grid sits behind an
 *     /ajax/ path its robots.txt disallows, so we cannot have it from this source.
 *  2. Not the channel these tickets are sold on. Ticketmaster 403s a CI runner and
 *     serves a device challenge to a residential browser, so TM's own prices are not
 *     collectable. This is a neighbouring market.
 *  3. Not a trend until there are at least two observation days. `lowDelta` is absent
 *     rather than zero on day one, because rendering 0 would read as "flat" - a claim
 *     we cannot make from a single point.
 *
 * Treating these numbers as achievable prices for our seats would be exactly the
 * invented precision the project refuses to trade in.
 */

import summary from "../../data/market/summary.json";

export interface MarketGame {
  gameId: number;
  date: string;
  /** Cheapest listing in the arena, all-in. */
  low: number;
  /** Priciest listing in the arena, all-in. */
  high: number;
  observedDate: string;
  observations: number;
  lowFirst?: number;
  lowFirstDate?: string;
  lowDelta?: number;
}

export interface MarketSummary {
  source: string;
  priceBasis: string;
  isOwnChannel: boolean;
  confidence: "measured" | "measured_single_point";
  observationDays: number;
  firstObservedDate: string | null;
  lastObservedDate: string | null;
  games: MarketGame[];
}

const MARKET = summary as unknown as MarketSummary;

const BY_GAME = new Map<number, MarketGame>(MARKET.games.map((g) => [g.gameId, g]));

export function market(): MarketSummary {
  return MARKET;
}

export function marketFor(gameId: number): MarketGame | null {
  return BY_GAME.get(gameId) ?? null;
}

/**
 * Where a break-even list price sits against the arena's asking range.
 *
 * Deliberately coarse - three buckets, not a percentile. A percentile would imply the
 * range is a distribution we have sampled, and we have exactly two order statistics.
 */
export type Standing = "below_cheapest" | "inside_range" | "above_priciest";

export function standing(breakEven: number, m: MarketGame): Standing {
  if (breakEven < m.low) return "below_cheapest";
  if (breakEven > m.high) return "above_priciest";
  return "inside_range";
}

export function standingNote(s: Standing): string {
  switch (s) {
    case "below_cheapest":
      return "Break-even is under the cheapest seat in the building - the whole arena is asking more than you need.";
    case "above_priciest":
      return "Break-even is above every asking price in the building. The exchange credit likely beats resale here.";
    case "inside_range":
      return "Break-even falls inside the arena's asking range. Says nothing about your section specifically.";
  }
}
