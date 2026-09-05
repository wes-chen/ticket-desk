/**
 * The money model.
 *
 * Every game has four possible exits, and they are NOT symmetric:
 *
 *   1. List on Ticketmaster and it sells  -> list x 0.90  (10% seller fee, measured)
 *   2. Sharks Exchange "Return For Credit" -> tier credit, but as ACCOUNT CREDIT, and
 *                                             only until 48h before puck drop
 *   3. Ticketmaster instant "Get Paid"     -> a buyer's BID at a round dollar, paid net of
 *                                             the same 10% fee. Four samples, all below the
 *                                             tier credit - so never the right exit while
 *                                             the exchange window is open.
 *   4. Don't sell it                       -> $0
 *
 * The 48-hour exchange deadline is the load-bearing detail. Before it, exit 2 is a
 * guaranteed floor under exit 1, which changes the optimal strategy completely.
 */

export const FEE_RATE = 0.1; // measured at two price points: $77 -> $69.30, $70 -> $63.00
export const EXCHANGE_DEADLINE_HOURS = 48;

/**
 * A listing may not be posted below this fraction of the game's face price.
 *
 * From the published Sharks365 variable-pricing FAQ: members may post "up to 20% below
 * the ticket price of the game selected for resale", with a worked example ($150 member
 * rate -> $120 minimum list). There is explicitly NO ceiling.
 *
 * This is a real constraint the model previously lacked entirely, and it binds exactly
 * where the guidance used to say "mark down aggressively": after the exchange deadline
 * you cannot chase the market below 0.8 x face, so the worst a sale can net is
 * 0.8 x face x 0.9 = 0.72 x face. Below that the outcome is $0, not a smaller sale.
 */
export const MIN_LIST_RATIO_OF_FACE = 0.8;

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
 *
 * The credit is compared at its CASH-EQUIVALENT value, not its face value. Account credit
 * is not money: it expires at the end of the regular season, cannot buy playoffs, and
 * cannot roll into next season's invoice (all measured - see economics.json). Wesley
 * values it at 0.9. Solving L * (1 - fee) = credit * haircut gives the form below.
 *
 * This used to ignore the haircut entirely, which was invisible only because the haircut
 * was 1.0. At 0.9 it would have contradicted the exits panel, which HAS applied the
 * haircut all along: the panel would offer "$45.90 credit" beside a "$56.67 break-even",
 * while a $52 listing beats the credit. Two numbers on one screen disagreeing is the
 * failure mode this project keeps writing validators to prevent.
 */
export function breakEvenList(
  creditPerSeat: number,
  feeRate: number = FEE_RATE,
  creditHaircut: number = 1,
): number {
  return (creditPerSeat * creditHaircut) / (1 - feeRate);
}

export function puckDrop(game: Game): Date {
  return new Date(game.startTimeUTC);
}

/**
 * The lowest price this game's tickets may be listed at.
 *
 * Face is proxied by the tier credit, which is measured to equal face. Null when that
 * tier's credit has not been entered.
 */
export function minListPrice(creditPerSeat: number | null): number | null {
  // Deliberately NOT haircut-adjusted. This is the platform's published rule - a listing
  // may not go below 80% of the ticket's FACE price - and the platform does not care what
  // account credit is worth to Wesley. Face is proxied by the tier credit because credit
  // is measured to equal face. Applying the haircut here would invent a floor the seller
  // page does not enforce.
  return creditPerSeat == null ? null : creditPerSeat * MIN_LIST_RATIO_OF_FACE;
}

/**
 * How many regular-season home games remain that credit from THIS game could be spent on.
 *
 * Published terms: account credit expires at puck drop of the last game of the season,
 * cannot roll over, and cannot buy playoff tickets - so it must be spent on a
 * regular-season home game that has not happened yet.
 *
 * Preseason games count as sources of credit (the program covers them) but never as
 * places to SPEND it, which is why they are excluded from the outlet count.
 */
export function remainingCreditOutlets(game: Game, season: Game[]): number {
  const t = puckDrop(game).getTime();
  return season.filter(
    (g) => g.gameType === "regular" && puckDrop(g).getTime() > t,
  ).length;
}

/**
 * Is the exchange a real floor for this game?
 *
 * For the final home game the answer is NO, and it is not a rounding issue: credit
 * earned there can only buy different seats to the game you just returned, which is not
 * an exit. So that game's outcomes are "it sells", the instant offer, or $0 - the
 * project's central "listing high is free because the credit catches you" argument does
 * not apply to it.
 */
export function exchangeIsRealFloor(game: Game, season: Game[]): boolean {
  return remainingCreditOutlets(game, season) > 0;
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
  opts: {
    instantOfferPerSeat?: number | null;
    feeRate?: number;
    creditHaircut?: number;
    /** The full season, needed to tell whether credit from this game can be spent. */
    season?: Game[];
  } = {},
): Exit[] {
  const feeRate = opts.feeRate ?? FEE_RATE;
  const haircut = opts.creditHaircut ?? 1;
  const phase = phaseOf(game);
  // Absent a season, assume the floor exists - that is the status quo and the safer
  // default. Claiming "no floor" without evidence would be worse than the old bug.
  const realFloor = opts.season ? exchangeIsRealFloor(game, opts.season) : true;

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
      available: phase === "floor_active" && credit != null && realFloor,
      note:
        credit == null
          ? "tier credit not measured yet"
          : !realFloor
            ? "no games left to spend the credit on - it expires at this puck drop"
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
      note:
        phase === "floor_active" && realFloor
          ? "avoidable - exchange is still open"
          : "this is the real downside now",
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
export function guidance(
  game: Game,
  credit: number | null,
  season: Game[] = [],
  now: Date = new Date(),
  creditHaircut: number = 1,
): Guidance {
  const phase = phaseOf(game, now);
  // Same haircut as the exits panel and the row's break-even. Threaded rather than read
  // from config here so this module stays pure and testable at other values.
  const be = credit == null ? null : breakEvenList(credit, FEE_RATE, creditHaircut);
  const hrs = hoursUntil(exchangeDeadline(game), now);
  const minList = minListPrice(credit);
  // Without a season, assume the floor exists - the safe default.
  const realFloor = season.length ? exchangeIsRealFloor(game, season) : true;

  if (phase === "past") {
    return { headline: "Game played", detail: "No action available.", urgency: "past", breakEven: be };
  }

  // The published terms make this a real case, not a hypothetical: credit expires at
  // puck drop of the last game and cannot roll over or buy playoffs, so credit earned
  // by returning the FINAL home game has nowhere to go. Saying "list high, the credit
  // catches you" there would be actively wrong.
  if (!realFloor) {
    return {
      headline: "No exchange floor - resale or nothing",
      detail:
        `Credit from returning this game expires at its own puck drop and can only buy ` +
        `regular-season home games, of which none remain. So there is no floor here at ` +
        `any point: the outcomes are a sale, the instant offer, or $0.` +
        (minList == null
          ? ""
          : ` You also cannot list below ${fmt(minList)} (80% of face), so if the market ` +
            `will not clear at that, the result is $0 rather than a smaller sale.`),
      urgency: "now",
      breakEven: be,
    };
  }

  if (phase === "floor_expired") {
    return {
      headline: "Floor expired - resale or nothing",
      detail:
        `The exchange window closed. An unsold ticket is now worth $0, so the only ` +
        `question left is what price clears the market before puck drop.` +
        (minList == null
          ? " Mark down as far as the platform allows."
          : ` Mark down - but the floor is ${fmt(minList)} (80% of face); listings below ` +
            `that are not accepted, so below it the outcome is $0 rather than a cheaper sale.`),
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
        : `Break-even list price is ${fmt(be)}. Anything above that beats the exchange, and because you can still fall back to the ${fmt(credit!)} credit until the deadline, listing high costs you nothing.` +
          (creditHaircut === 1
            ? ""
            : ` Break-even reflects credit valued at ${Math.round(creditHaircut * 100)}% of face, which is your own figure - at full face it would be ${fmt(breakEvenList(credit!, FEE_RATE, 1))}.`),
    urgency: soon ? "soon" : "none",
    breakEven: be,
  };
}

export function fmt(n: number | null): string {
  if (n == null) return "--";
  return `$${n.toFixed(2)}`;
}
