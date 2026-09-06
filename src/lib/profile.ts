/**
 * The user's private data. None of this is ever committed to the repo or served
 * from GitHub Pages.
 *
 * Transfer between devices happens through a URL *fragment* (everything after `#`).
 * Fragments are never transmitted in an HTTP request - they don't reach the origin
 * server, don't appear in access logs, and aren't visible to the host. So a link
 * generated on the desktop and opened on a phone moves the data device-to-device
 * even though the app itself is served from a public URL.
 *
 * The encoding is obfuscation, not encryption. Anyone who obtains the link can read
 * it. Treat the link like the data itself.
 */

export type Tier = "A+" | "A" | "B" | "C" | "D" | "PRESEASON";

export const TIERS: Tier[] = ["A+", "A", "B", "C", "D", "PRESEASON"];

/**
 * How a game's tickets actually left our hands. ops#22.
 *
 * `exchanged` is deliberately distinct from `unsold`. Returning for credit is a CHOSEN
 * exit that paid the tier credit; going unsold paid $0. Conflating them would bias any
 * future P(sell) fit toward pessimism by treating a deliberate, successful fallback as
 * a failure to sell.
 */
export type OutcomeKind = "sold" | "exchanged" | "unsold" | "instant";

export interface Outcome {
  kind: OutcomeKind;
  /** ISO date the outcome happened. */
  on: string;
  /** List price at the moment of the outcome, per ticket. Null when not applicable. */
  atList: number | null;
  /** Actual net received per ticket, if known. */
  netPerSeat: number | null;
  /**
   * For `exchanged` only: has the account credit actually been SPENT?
   *
   * ops#13 is explicit that unspent credit is not the same as cash, and
   * economics.json says credit "only has value if it gets spent". Treating an unspent
   * balance as recovered money would overstate the season's recovery - so this is
   * tracked rather than assumed, and undefined means "not yet known" rather than false.
   */
  creditSpent?: boolean;
  note?: string;
}

export interface ListPriceObservation {
  price: number;
  /** ISO instant this entry was RECORDED. */
  at: string;
  /**
   * Set when this entry preserves a price that already existed before history began.
   *
   * `at` is then when history started, NOT when the price was set - we do not know the
   * latter and never will. Marking it is the difference between preserving a value and
   * inventing a timestamp for it.
   */
  backfilled?: true;
}

export interface Profile {
  v: 1;
  seats: {
    section: string;
    row: string;
    seats: string[];
  };
  /** Season invoice total across ALL seats, as billed. */
  invoiceTotal: number | null;
  /** Exchange credit per seat, by tier. Depends on seat location, hence private. */
  credits: Partial<Record<Tier, number>>;
  /** Per-game intended list price, keyed by NHL gameId. The CURRENT value only. */
  listPrices: Record<string, number>;
  /**
   * Append-only history of what we have asked, per game.
   *
   * `listPrices` holds one number and overwrites it, so before this existed a price
   * change destroyed its predecessor. We ran four collectors recording other sellers'
   * asks every day and kept no record of our own - which made "did raising the price
   * change anything" not hard to answer but IMPOSSIBLE, because the input did not exist.
   *
   * Never truncated. It is a few entries per game and its entire value is being complete.
   *
   * KNOWN LIMIT, stated rather than left to be discovered: this records prices that were
   * SET. Clearing the field removes the current price without appending anything, so a
   * delisting is not distinguishable from a typo correction. Recording one as the other
   * would be worse than recording neither.
   */
  listPriceHistory?: Record<string, ListPriceObservation[]>;
  /**
   * Recorded outcomes, keyed by NHL gameId. The ONLY path to ops#8 ever existing:
   * asking prices alone cannot fit a probability of sale, and an outcome is not
   * recoverable once a game has been played.
   */
  outcomes?: Record<string, Outcome>;
  /**
   * Observed (list, net) pairs from the seller page, for ops#9. Kept as a list rather
   * than a single rate so a stepped or tiered fee shows up as disagreement between
   * observations instead of being averaged away.
   */
  feeObservations?: { list: number; net: number; on: string; note?: string }[];
  /**
   * Instant "Sell Your Tickets Now" offers, keyed by NHL gameId, oldest first. ops#12.
   *
   * Login-only, so this is manual-paste data - and it is the ONLY price signal we have
   * from the channel these tickets are actually sold on, since Ticketmaster blocks
   * collection from both a runner and a residential browser. A bid, not an ask.
   */
  instantOffers?: Record<string, { on: string; offerPerTicket: number }[]>;
}

export const EMPTY_PROFILE: Profile = {
  v: 1,
  seats: { section: "", row: "", seats: [] },
  invoiceTotal: null,
  credits: {},
  listPrices: {},
  // listPriceHistory is deliberately absent, not {}. It is optional, recordListPrice
  // creates it on first use, and the profile travels between devices inside a URL
  // FRAGMENT - so an empty object here would add bytes to every transfer link and make
  // the round-trip encoding no longer identity for profiles that have never had a price.
  outcomes: {},
  feeObservations: [],
  instantOffers: {},
};

/**
 * Record a list price, preserving what it replaced. Pure - returns a new Profile.
 *
 * Appends only on an actual CHANGE, so re-saving the same number does not pad the series
 * with entries that carry no information. A pre-existing price with no history is
 * backfilled first and marked as such.
 */
export function recordListPrice(
  p: Profile,
  gameId: number,
  price: number | null,
  now: Date = new Date(),
): Profile {
  const key = String(gameId);
  const prices = { ...p.listPrices };
  const history: Record<string, ListPriceObservation[]> = { ...(p.listPriceHistory ?? {}) };
  const entries = [...(history[key] ?? [])];
  const previous = p.listPrices[key];

  if (price === null || Number.isNaN(price)) {
    delete prices[key];
    // Deliberately no entry - see the KNOWN LIMIT on listPriceHistory.
    return { ...p, listPrices: prices, listPriceHistory: history };
  }

  // A price that predates history is preserved, but its timestamp is honest about being
  // the moment history began rather than the moment the price was chosen.
  //
  // Deliberately NOT conditioned on `previous !== price`. It was, and that lost the
  // marker in exactly the case the marker exists for: a legacy price re-saved unchanged
  // produced a single UNFLAGGED entry, which reads as "set at this instant" when its real
  // vintage is unknown. Found in review. When previous === price the backfilled entry is
  // pushed here and the append below correctly declines to duplicate it.
  if (entries.length === 0 && previous !== undefined) {
    entries.push({ price: previous, at: now.toISOString(), backfilled: true });
  }
  if (entries.length === 0 || entries[entries.length - 1].price !== price) {
    entries.push({ price, at: now.toISOString() });
  }

  prices[key] = price;
  history[key] = entries;
  return { ...p, listPrices: prices, listPriceHistory: history };
}

export function isConfigured(p: Profile): boolean {
  return p.seats.section.trim() !== "" && Object.keys(p.credits).length > 0;
}

export function seatCount(p: Profile): number {
  return Math.max(p.seats.seats.length, 1);
}

export function invoicePerSeat(p: Profile): number | null {
  if (p.invoiceTotal == null) return null;
  return p.invoiceTotal / seatCount(p);
}

// --- fragment transfer ---------------------------------------------------

function toBase64Url(s: string): string {
  const bytes = new TextEncoder().encode(s);
  let bin = "";
  for (const b of bytes) bin += String.fromCharCode(b);
  return btoa(bin).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function fromBase64Url(s: string): string {
  const b64 = s.replace(/-/g, "+").replace(/_/g, "/");
  const bin = atob(b64 + "=".repeat((4 - (b64.length % 4)) % 4));
  const bytes = Uint8Array.from(bin, (c) => c.charCodeAt(0));
  return new TextDecoder().decode(bytes);
}

export function encodeProfile(p: Profile): string {
  return toBase64Url(JSON.stringify(p));
}

export function decodeProfile(encoded: string): Profile | null {
  try {
    const parsed = JSON.parse(fromBase64Url(encoded)) as Profile;
    if (parsed?.v !== 1 || typeof parsed.seats?.section !== "string") return null;
    return { ...EMPTY_PROFILE, ...parsed };
  } catch {
    return null;
  }
}

export function shareUrl(p: Profile): string {
  const base = `${location.origin}${location.pathname}`;
  return `${base}#p=${encodeProfile(p)}`;
}

/**
 * Read a profile out of the current URL fragment, then scrub it from the address
 * bar so it isn't left sitting in history or accidentally bookmarked/shared.
 */
export function consumeProfileFromHash(): Profile | null {
  const m = location.hash.match(/[#&]p=([A-Za-z0-9_-]+)/);
  if (!m) return null;
  const p = decodeProfile(m[1]);
  if (p) history.replaceState(null, "", location.pathname + location.search);
  return p;
}
