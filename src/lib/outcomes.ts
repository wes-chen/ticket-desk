/**
 * Recorded outcomes, and what can honestly be said from them. ops#22.
 *
 * The point of recording outcomes is that they are the only input a sell-timing model
 * (ops#8) could ever be fit against, and they are NOT recoverable after the fact - a
 * game happens once, and whether it sold at $70 on day 12 is gone unless it was written
 * down at the time. There are 44 a season.
 *
 * This module deliberately does NOT estimate a probability of sale. With a handful of
 * outcomes any such curve would be invented, and the project's standing rule is to show
 * the tradeoff and say what is unknown rather than manufacture authority. What it does
 * is count, and say plainly how far from useful the count still is.
 */

import type { Game } from "./economics";
import type { Outcome, OutcomeKind, Profile } from "./profile";

export const OUTCOME_LABEL: Record<OutcomeKind, string> = {
  sold: "Sold",
  exchanged: "Returned for credit",
  unsold: "Went unsold",
  instant: "Instant offer taken",
};

/** Outcomes needed before a probability-of-sale fit is worth attempting at all. */
export const FIT_THRESHOLD = 20;

export function outcomeFor(profile: Profile, gameId: number): Outcome | null {
  return profile.outcomes?.[String(gameId)] ?? null;
}

export interface OutcomeTally {
  total: number;
  byKind: Record<OutcomeKind, number>;
  /** Games already played with no outcome recorded - permanently lost observations. */
  missed: Game[];
  /** How many more outcomes before a timing fit is even worth attempting. */
  needed: number;
}

export function tally(profile: Profile, games: Game[], now: Date = new Date()): OutcomeTally {
  const byKind: Record<OutcomeKind, number> = { sold: 0, exchanged: 0, unsold: 0, instant: 0 };
  let total = 0;
  const missed: Game[] = [];

  for (const g of games) {
    const o = outcomeFor(profile, g.gameId);
    if (o) {
      byKind[o.kind] += 1;
      total += 1;
    } else if (new Date(g.startTimeUTC) < now) {
      // A played game with nothing recorded. Surfaced rather than ignored, because this
      // is the one kind of data loss that cannot be undone by collecting harder later.
      missed.push(g);
    }
  }

  return { total, byKind, missed, needed: Math.max(0, FIT_THRESHOLD - total) };
}

/**
 * What the recorded outcomes support saying, stated conservatively.
 *
 * The sell rate here counts only games that reached a market conclusion - sold, unsold,
 * or instant. Exchanged games are excluded from the DENOMINATOR rather than counted as
 * failures: returning for credit is a chosen exit taken before the deadline, usually
 * while the ticket was still listed, so it is censored data in the survival-analysis
 * sense. Counting it as "did not sell" would understate the sell rate, and counting it
 * as "sold" would overstate it.
 */
export interface SellRate {
  sold: number;
  concluded: number;
  rate: number | null;
  censored: number;
  trustworthy: boolean;
}

export function sellRate(t: OutcomeTally): SellRate {
  const sold = t.byKind.sold;
  const concluded = sold + t.byKind.unsold + t.byKind.instant;
  return {
    sold,
    concluded,
    rate: concluded === 0 ? null : sold / concluded,
    censored: t.byKind.exchanged,
    trustworthy: concluded >= FIT_THRESHOLD,
  };
}
