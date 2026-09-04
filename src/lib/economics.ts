/**
 * The money model.
 *
 * Every game has four possible exits, and they are NOT symmetric:
 *
 *   1. List on Ticketmaster and it sells  -> list x 0.90  (10% seller fee, measured)
 *   2. Sharks Exchange "Return For Credit" -> tier credit, but as ACCOUNT CREDIT, and
 *                                             only until 48h before puck drop
 *   3. Ticketmaster instant "Get Paid"     -> guaranteed cash, historically ~35% of list-net
 *   4. Don't sell it                       -> $0
 *
 * The 48-hour exchange deadline is the load-bearing detail. Before it, exit 2 is a
 * guaranteed floor under exit 1, which changes the optimal strategy completely.
 */

export const FEE_RATE = 0.1; // measured: $77.00 list -> $69.30 net
export const EXCHANGE_DEADLINE_HOURS = 48;

export type Tier = "A+" | "A" | "B" | "C" | "D" | "PRESEASON";

export interface Game {
  gameId: number;
  date: string;
  startTimeUTC: string;
  gameType: "preseason" | "regular" | "playoff";
  opponent: { abbrev: string; name: string; logo?: string | null };
  tier: Tier | null;
  ticketsLink?: string | null;
}

/** What you actually pocket from a listing at `list` dollars. */
export function netPayout(list: number, feeRate: number = FEE_RATE): number {
  return list * (1 - feeRate);
}

/** Inverse: what must you list at to net `target`? */
export function listToNet(target: number, feeRate: number = FEE_RATE): number {
  return target / (1 - feeRate);
}

/**
 * The single most useful number per game: the list price at which resale exactly ties
 * the exchange credit. List above this and resale wins; below it you're better off
 * returning the ticket.
 */
export function breakEvenList(creditPerSeat: number, feeRate: number = FEE_RATE): number {
  return creditPerSeat / (1 - feeRate);
}

export function puckDrop(game: Game): Date {
  return new Date(game.startTimeUTC);
}

export function exchangeDeadline(game: Game): Date {
  return new Date(puckDrop(game).getTime() - EXCHANGE_DEADLINE_HOURS * 3600 * 1000);
}

export type Phase =
  /** Exchange still available. Your downside is capped at the tier credit. */
  | "floor_active"
  /** Inside 48h. Exchange is gone; an unsold ticket is now worth exactly zero. */
  | "floor_expired"
  /** Puck has dropped. */
  | "past";

export function phaseOf(game: Game, now: Date = new Date()): Phase {
  if (now >= puckDrop(game)) return "past";
  if (now >= exchangeDeadline(game)) return "floor_expired";
  return "floor_active";
}

export function hoursUntil(when: Date, now: Date = new Date()): number {
  return (when.getTime() - now.getTime()) / 3_600_000;
}

export interface Exit {
  key: "resale" | "exchange" | "instant" | "unsold";
  label: string;
  perSeat: number | null;
  guaranteed: boolean;
  available: boolean;
  note: string;
}

/**
 * Lay out all four exits for a game at a hypothetical list price. This is the
 * "show me the tradeoff" view - it deliberately does NOT collapse to a single
 * recommendation, because with no sell-through history we cannot honestly
 * estimate P(sell) yet.
 */
export function exits(
  game: Game,
  credit: number | null,
  listPrice: number | null,
  opts: { instantOfferPerSeat?: number | null; feeRate?: number; creditHaircut?: number } = {},
): Exit[] {
  const feeRate = opts.feeRate ?? FEE_RATE;
  const haircut = opts.creditHaircut ?? 1;
  const phase = phaseOf(game);

  return [
    {
      key: "resale",
      label: "List & sell",
      perSeat: listPrice == null ? null : netPayout(listPrice, feeRate),
      guaranteed: false,
      available: phase !== "past",
      note: listPrice == null ? "set a list price" : `${fmt(listPrice)} listed, minus ${Math.round(feeRate * 100)}% fee`,
    },
    {
      key: "exchange",
      label: "Exchange for credit",
      perSeat: credit == null ? null : credit * haircut,
      guaranteed: true,
      available: phase === "floor_active" && credit != null,
      note:
        credit == null
          ? "tier credit not measured yet"
          : phase === "floor_active"
            ? "account credit, not cash"
            : "deadline passed",
    },
    {
      key: "instant",
      label: "Instant offer",
      perSeat: opts.instantOfferPerSeat ?? null,
      guaranteed: true,
      available: opts.instantOfferPerSeat != null && phase !== "past",
      note: opts.instantOfferPerSeat == null ? "not captured for this game" : "guaranteed cash, poor rate",
    },
    {
      key: "unsold",
      label: "Goes unsold",
      perSeat: 0,
      guaranteed: true,
      available: true,
      note: phase === "floor_active" ? "avoidable - exchange is still open" : "this is the real downside now",
    },
  ];
}

export interface Guidance {
  headline: string;
  detail: string;
  urgency: "none" | "soon" | "now" | "past";
  breakEven: number | null;
}

/**
 * Strategy guidance. The key asymmetry, stated plainly:
 *
 * While the exchange is still open, listing at ANY price above break-even weakly
 * dominates exchanging outright - if it sells you beat the credit, and if it doesn't
 * you exchange anyway at the deadline. The floor makes patience free. Once the floor
 * expires that reverses hard, and holding out costs you everything.
 */
export function guidance(game: Game, credit: number | null, now: Date = new Date()): Guidance {
  const phase = phaseOf(game, now);
  const be = credit == null ? null : breakEvenList(credit);
  const hrs = hoursUntil(exchangeDeadline(game), now);

  if (phase === "past") {
    return { headline: "Game played", detail: "No action available.", urgency: "past", breakEven: be };
  }

  if (phase === "floor_expired") {
    return {
      headline: "Floor expired - resale or nothing",
      detail:
        "The exchange window closed. An unsold ticket is now worth $0, so the only question left is what price clears the market before puck drop. Mark down aggressively.",
      urgency: "now",
      breakEven: be,
    };
  }

  const soon = hrs <= 72;
  return {
    headline: soon ? `Exchange deadline in ${Math.floor(hrs)}h` : "Floor active - list high",
    detail:
      be == null
        ? "Tier credit not measured yet, so there's no break-even to price against. Run the Return For Credit flow to the review page for this tier."
        : `Break-even list price is ${fmt(be)}. Anything above that beats the exchange, and because you can still fall back to the ${fmt(credit!)} credit until the deadline, listing high costs you nothing.`,
    urgency: soon ? "soon" : "none",
    breakEven: be,
  };
}

export function fmt(n: number | null): string {
  if (n == null) return "--";
  return `$${n.toFixed(2)}`;
}
