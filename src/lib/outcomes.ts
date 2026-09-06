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

import { exchangeDeadline, hoursUntil, type Game } from "./economics.ts";
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

/**
 * The date to DEFAULT an outcome to, given when it is being recorded.
 *
 * Not simply today. `Outcome.on` is documented as the date the outcome HAPPENED, and it
 * is the dependent variable for P(sell | price, days-to-game) - so recording visit timing
 * instead biases days-to-game toward zero and makes sales look later than they were.
 *
 * No outcome can happen after puck drop: a sale, an exchange and an unsold verdict are
 * all settled by then. So the default is the earlier of today and the game's local date,
 * which bounds the error rather than letting it grow with how long someone took to open
 * the app. It is still only a default - the user is the only party who knows the real
 * date, and the UI must let them say so.
 */
export const ARENA_TZ = "America/Los_Angeles";

/** Today's calendar date AT THE ARENA, not in UTC. */
export function arenaToday(now: Date = new Date()): string {
  // en-CA formats as YYYY-MM-DD, which is the shape game.date already uses.
  return new Intl.DateTimeFormat("en-CA", { timeZone: ARENA_TZ }).format(now);
}

export function defaultOutcomeDate(game: Game, now: Date = new Date()): string {
  // ARENA-local today, compared against an arena-local game.date. Using
  // now.toISOString() here compared a UTC calendar date against a Pacific one - and
  // Pacific trails UTC by 7-8h, so UTC's date rolls over first. A genuine sale at 21:00
  // Pacific on the evening BEFORE a game returned the game's date, one day late: bounded
  // to a day rather than weeks, but it reintroduced the exact late-side bias this
  // function exists to remove, every evening rather than only once. Found in review.
  const today = arenaToday(now);
  return today < game.date ? today : game.date;
}

/**
 * Store an explicit outcome date. Pure, so "a supplied date is kept, not silently
 * replaced with today" is testable - ops#54 asked for that assertion and the first
 * attempt at this left the setter in the component where nothing could reach it.
 *
 * An empty date is refused rather than stored: a blank `on` would be worse than a
 * slightly wrong one, because every consumer treats it as a real date.
 */
export function withOutcomeDate(profile: Profile, gameId: number, on: string): Profile {
  const key = String(gameId);
  const existing = profile.outcomes?.[key];
  if (!existing || !on) return profile;
  return { ...profile, outcomes: { ...profile.outcomes, [key]: { ...existing, on } } };
}

export function tally(profile: Profile, games: Game[], now: Date = new Date()): OutcomeTally {
  const byKind: Record<OutcomeKind, number> = { sold: 0, exchanged: 0, unsold: 0, instant: 0 };
  let total = 0;

  for (const g of games) {
    const o = outcomeFor(profile, g.gameId);
    if (o) {
      byKind[o.kind] += 1;
      total += 1;
    }
  }

  // Derived from pending(), not re-computed. Both used to apply the same rule to the same
  // inputs by separate code paths - they agreed, and could drift, at which point the
  // banner and the panel beneath it would contradict each other about the same games.
  const missed = pending(profile, games, now).played;

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
