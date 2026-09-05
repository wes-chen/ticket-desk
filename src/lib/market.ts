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

export interface SourceQuote {
  low: number;
  /** Null when the source publishes no high. Gametime never does. */
  high: number | null;
  observedDate: string;
}

export interface MarketGame {
  gameId: number;
  date: string;
  /** Cheapest listing in the arena, all-in, from the primary source. */
  low: number;
  /** Priciest listing in the arena. Null when the source publishes no high. */
  high: number | null;
  observedDate: string;
  observations: number;
  lowFirst?: number;
  lowFirstDate?: string;
  lowDelta?: number;
  /**
   * Other sources' latest quotes, kept ALONGSIDE the primary rather than blended.
   * Two sources disagreeing is the signal that one has gone wrong; averaging them
   * destroys exactly that signal.
   */
  otherSources?: Record<string, SourceQuote>;
}

export interface SourceRatio {
  /** Games this source and the primary BOTH priced. Its own n, not a pooled one. */
  games: number;
  /** This source's low / the primary's low, median over those games. */
  medianRatioToPrimary: number;
  minRatio: number;
  maxRatio: number;
}

/**
 * Per source, never pooled.
 *
 * A single pooled median was wrong and actively misleading (ops#43/ops#44). The
 * rolling-window sources publish only a forward slice, and that slice is cheaper - so a
 * pooled figure mixed "secondary sources ask more" with "early-season games cost less".
 * Measured 2026-09-05, the pooled median said +3.3%; per source it was Gametime +6.0%,
 * TicketNetwork level, and ScoreBig -5.4%. The pooled number was directionally wrong for
 * two of the three.
 */
export interface CrossSource {
  perSource: Record<string, SourceRatio>;
  /** Games every source priced - the only like-for-like set, and usually small. */
  commonGames: number;
}

export interface MarketSummary {
  /** Primary source name - the one driving `low`/`high`. */
  source: string;
  /** All sources with data. */
  sources: string[];
  /** Agreement between sources, or null with fewer than two. */
  crossSource: CrossSource | null;
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
export type Standing = "below_cheapest" | "inside_range" | "above_priciest" | "unknown_top";

export function standing(breakEven: number, m: MarketGame): Standing {
  if (breakEven < m.low) return "below_cheapest";
  // A source may publish no high - Gametime never does. Without an upper bound there is
  // no "above every asking price" claim to make, so say so rather than treating a
  // missing high as infinite (which would silently read as inside_range).
  if (m.high == null) return "unknown_top";
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
    case "unknown_top":
      return "Break-even is above the cheapest seat, but this source publishes no upper bound, so where it sits in the range is unknown.";
  }
}

/**
 * How many days old the market series is, computed in the browser rather than baked at
 * build time - a bundle built on Monday and viewed on Friday would otherwise claim to be
 * fresh. `null` when there is no data at all.
 */
export function staleDays(now: Date = new Date()): number | null {
  if (!MARKET.lastObservedDate) return null;
  const last = new Date(`${MARKET.lastObservedDate}T00:00:00Z`);
  const today = new Date(`${now.toISOString().slice(0, 10)}T00:00:00Z`);
  return Math.round((today.getTime() - last.getTime()) / 86_400_000);
}

/** Matches STALE_DAYS in scripts/check_data_freshness.py. */
export const STALE_THRESHOLD = 2;
