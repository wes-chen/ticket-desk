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

import { exchangeDeadline, hoursUntil, type Game } from "./economics";
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

/**
 * How long before the exchange deadline a game starts asking to be recorded.
 *
 * 24 hours, not 48. The deadline itself is T-48h before puck drop, so this fires in the
 * final day of the window - late enough that the decision is real, early enough to act.
 */
export const CLOSING_SOON_HOURS = 24;

export interface Pending {
  /**
   * The exchange window shuts within CLOSING_SOON_HOURS and nothing is recorded.
   *
   * This bucket exists because one of the four outcomes STOPS BEING POSSIBLE at that
   * moment. A prompt that only fires after the game can never separate "chose the credit"
   * from "went unsold" - and conflating them biases any later fit toward pessimism,
   * because a chosen exit would be counted as a failure to sell.
   */
  closing: Game[];
  /** Played, with nothing recorded. The observation is already gone. */
  played: Game[];
}

/**
 * Games that need an outcome recorded NOW, split by which of them is still answerable.
 *
 * Pure, so the two prompts and their tests do not depend on a clock or a render.
 */
export function pending(profile: Profile, games: Game[], now: Date = new Date()): Pending {
  const closing: Game[] = [];
  const played: Game[] = [];
  for (const g of games) {
    if (outcomeFor(profile, g.gameId)) continue;
    if (new Date(g.startTimeUTC) < now) {
      played.push(g);
      continue;
    }
    const h = hoursUntil(exchangeDeadline(g), now);
    // Strictly inside the window: past the deadline the exchange is gone, and that game
    // belongs to the post-game prompt instead. h <= 0 is deliberately NOT closing.
    if (h > 0 && h <= CLOSING_SOON_HOURS) closing.push(g);
  }
  return { closing, played };
}

/**
 * The outcome set as a portable JSON payload.
 *
 * WHY THIS EXISTS AT ALL. Outcomes live in browser localStorage - correctly, because they
 * contain our prices and rule 1 keeps those out of the repo. But that means clearing site
 * data destroys the entire training set, with no copy anywhere. Up to 44 irreplaceable
 * observations behind one browser setting, and they are the only input ops#8 could ever
 * be fit against.
 *
 * A button is the weakest of the three options considered and the only one that exists in
 * time: the first outcomes resolve in mid-September, and a durability scheme still being
 * designed then protects nothing. Writing to the ops repo directly would need a token in
 * the browser, which CLAUDE.md says to stop and ask about rather than add unprompted.
 *
 * Includes the game date and opponent deliberately. This file is for the PRIVATE ops repo
 * or local storage - never the public one - and an outcome without its game is not a
 * training example, it is a number.
 */
export function exportPayload(profile: Profile, games: Game[], now: Date = new Date()) {
  const byId = new Map(games.map((g) => [g.gameId, g]));
  const rows = Object.entries(profile.outcomes ?? {}).map(([gameId, o]) => {
    const g = byId.get(Number(gameId));
    return {
      gameId: Number(gameId),
      date: g?.date ?? null,
      opponent: g?.opponent?.abbrev ?? null,
      tier: g?.tier ?? null,
      ...o,
    };
  });
  rows.sort((a, b) => (a.date ?? "").localeCompare(b.date ?? "") || a.gameId - b.gameId);
  return {
    _what: "Recorded per-game outcomes. PRIVATE - contains our listing prices. Never commit to the public repo.",
    exportedAt: now.toISOString(),
    season: "2026-27",
    count: rows.length,
    outcomes: rows,
  };
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
