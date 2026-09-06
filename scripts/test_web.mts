/**
 * Self-test for the TypeScript libraries under src/lib/. Part of ops#17.
 *
 * The Python suite covers the collectors and validators; the money model and the
 * outcome accounting had no tests at all, despite carrying the most consequential logic
 * in the project - break-even, the exchange-deadline phase, and the censoring rule that
 * decides what sell rate gets reported.
 *
 * Runs on `node --experimental-strip-types`, which Node 24 supports natively, so this
 * adds NO dependency. A test runner (vitest and friends) would be the conventional
 * choice and would give better output, but it is not worth a new dependency for a
 * handful of pure functions - and this project has a standing preference for staying
 * credential-free and dependency-light.
 *
 * src/lib/market.ts is deliberately not covered here: it imports a JSON module, which
 * bare Node requires an import attribute for. Its logic is thin (a Map lookup and two
 * comparisons) and its consumers are exercised in the browser checks.
 */

import {
  breakEvenList, exchangeDeadline, exchangeIsRealFloor, exits, guidance, hoursUntil,
  listToNet, minListPrice, netPayout, phaseOf, remainingCreditOutlets, type Game,
} from "../src/lib/economics.ts";
import {
  CLOSING_SOON_HOURS, FIT_THRESHOLD, arenaToday, defaultOutcomeDate, exportPayload,
  pending, selectableOutcomes, sellRate, tally, withOutcomeDate,
} from "../src/lib/outcomes.ts";
import {
  EMPTY_PROFILE, decodeProfile, encodeProfile, invoicePerSeat, isConfigured,
  recordListPrice, seatCount, type Profile,
} from "../src/lib/profile.ts";

// Accepted and ignored, so scripts/run_tests.py discovers this file the same way it
// discovers the Python suites: by finding "--self-test" in the source. This file has
// only one mode, so the flag is a no-op.
void process.argv.includes("--self-test");

const fails: string[] = [];
function check(label: string, got: unknown, want: unknown) {
  const a = JSON.stringify(got);
  const b = JSON.stringify(want);
  if (a !== b) fails.push(`${label}: got ${a}, want ${b}`);
}
function eqDate(label: string, got: string, want: string) {
  if (got !== want) fails.push(`${label}: got ${got}, want ${want}`);
}
function near(label: string, got: number, want: number, tol = 0.005) {
  if (Math.abs(got - want) > tol) fails.push(`${label}: got ${got}, want ~${want}`);
}

// --- economics: the money model -------------------------------------------------

near("net of a $70 listing", netPayout(70), 63);          // measured: $70 -> $63.00
near("net of a $77 listing", netPayout(77), 69.3);        // measured: $77 -> $69.30
near("listToNet inverts netPayout", listToNet(netPayout(123.45)), 123.45);
near("break-even at a $51 credit", breakEvenList(51), 56.6667, 0.001);
near("break-even at 15% would be higher", breakEvenList(51, 0.15), 60, 0.001);

// The relationship the whole tool rests on: listing AT break-even exactly ties the
// credit. If this drifts, every recommendation drifts with it.
near("net at break-even equals the credit", netPayout(breakEvenList(51)), 51, 0.001);

// ---- listing price history (ops#48) ----
// listPrices holds one number and overwrites it, so a price change used to destroy its
// predecessor. We recorded four sources' asks daily and none of our own.
{
  const t0 = new Date("2026-09-05T10:00:00Z");
  const t1 = new Date("2026-09-05T18:00:00Z");
  const base: Profile = { ...EMPTY_PROFILE };

  const a = recordListPrice(base, 1, 70, t0);
  check("first price is recorded", a.listPrices["1"], 70);
  check("and appears in history", a.listPriceHistory!["1"].map((e) => e.price), [70]);
  check("first entry is not marked backfilled",
        a.listPriceHistory!["1"][0].backfilled, undefined);

  const b = recordListPrice(a, 1, 85, t1);
  check("history keeps BOTH prices", b.listPriceHistory!["1"].map((e) => e.price), [70, 85]);
  check("current price is the latest", b.listPrices["1"], 85);
  check("timestamps are recorded", b.listPriceHistory!["1"][1].at, t1.toISOString());

  // Re-saving the same number must not pad the series with entries carrying no
  // information - the series' value is that every entry is a real change.
  const c = recordListPrice(b, 1, 85, new Date("2026-09-06T10:00:00Z"));
  check("re-saving the same price appends nothing",
        c.listPriceHistory!["1"].length, 2);

  // A legacy price RE-SAVED UNCHANGED must still be marked backfilled. The guard used to
  // require previous !== price, which dropped the marker in exactly the case it exists
  // for: the entry then read as "set at this instant" when its real vintage is unknown.
  // Found in review.
  const sameLegacy: Profile = { ...EMPTY_PROFILE, listPrices: { "9": 70 } };
  const same = recordListPrice(sameLegacy, 9, 70, t0);
  check("a legacy price re-saved unchanged yields ONE entry",
        same.listPriceHistory!["9"].length, 1);
  check("and it is marked backfilled", same.listPriceHistory!["9"][0].backfilled, true);

  // WHY THE UI MUST COMMIT ON BLUR, NOT ON CHANGE.
  // recordListPrice appends on every change it is handed, which is correct for settled
  // values and catastrophic for keystrokes: the input is a controlled <input
  // type="number"> whose onChange fires per character, so wiring it directly recorded
  // [70, 7, 8, 85] when editing 70 -> 85 - two permanent, unflagged phantom prices in a
  // structure documented as never truncated. Asserting the hazard here so the trap is
  // visible to whoever next touches the input, rather than rediscovered in review.
  let keyed: Profile = { ...EMPTY_PROFILE, listPrices: { "7": 70 } };
  for (const v of ["7", "", "8", "85"]) {
    keyed = recordListPrice(keyed, 7, v === "" ? null : Number(v), t0);
  }
  check("feeding raw keystrokes pollutes the history - hence the draft",
        keyed.listPriceHistory!["7"].map((e) => e.price), [70, 7, 8, 85]);

  // A profile that predates history has a price and no entries. It must be preserved,
  // and its timestamp must be honest about being when history STARTED.
  const legacy: Profile = { ...EMPTY_PROFILE, listPrices: { "2": 70 } };
  const d = recordListPrice(legacy, 2, 85, t1);
  check("a pre-existing price is backfilled, not lost",
        d.listPriceHistory!["2"].map((e) => e.price), [70, 85]);
  check("and the backfilled entry says so",
        d.listPriceHistory!["2"][0].backfilled, true);
  check("while the new one does not",
        d.listPriceHistory!["2"][1].backfilled, undefined);

  // Clearing removes the current price. Deliberately appends nothing: a delist is not
  // distinguishable from a typo correction, and recording one as the other is worse.
  const e = recordListPrice(b, 1, null, t1);
  check("clearing removes the current price", "1" in e.listPrices, false);
  check("and does not truncate the history", e.listPriceHistory!["1"].length, 2);

  // A profile with no history field at all must load and work.
  const noField: Profile = { ...EMPTY_PROFILE, listPriceHistory: undefined };
  check("a profile without the field still records",
        recordListPrice(noField, 3, 60, t0).listPriceHistory!["3"].length, 1);

  // Other games must be untouched.
  const two = recordListPrice(recordListPrice(base, 1, 70, t0), 2, 40, t1);
  check("games are independent", Object.keys(two.listPriceHistory!).sort(), ["1", "2"]);
}

// ---- the credit haircut, and the invariant that broke when it stopped being 1.0 ----
// Account credit is not cash: it expires at season end, cannot buy playoffs and does not
// roll over. Wesley values it at 0.9. Break-even must compare resale against the credit's
// CASH-EQUIVALENT value, or the app shows two numbers that contradict each other.
near("break-even drops when credit is discounted", breakEvenList(51, 0.10, 0.9), 51, 0.001);
near("a full-face haircut is the old behaviour", breakEvenList(51, 0.10, 1), 56.6667, 0.001);
near("the defining relation still holds at 0.9",
     netPayout(breakEvenList(51, 0.10, 0.9)), 51 * 0.9, 0.001);

near("min list price ignores the haircut", minListPrice(51)!, 40.8, 0.001);

const game = (startUTC: string): Game => ({
  gameId: 1, date: startUTC.slice(0, 10), startTimeUTC: startUTC,
  gameType: "regular", opponent: { abbrev: "XXX", name: "Test Team" }, tier: "A",
});

// THE CROSS-CHECK. The exits panel has always applied the haircut to the exchange payout;
// breakEvenList did not. At 1.0 that was invisible. At 0.9 the panel would have offered
// "$45.90 credit" beside a "$56.67 break-even" while a $52 listing beat the credit - two
// numbers on one screen disagreeing. Assert they agree, at a haircut that is not 1.
{
  const h = 0.9, credit = 51;
  const ex = exits(game("2026-12-01T03:00:00Z"), credit, null,
                   { creditHaircut: h }).find((e) => e.key === "exchange")!;
  near("exits values credit at the haircut", ex.perSeat!, credit * h, 0.001);
  near("and break-even nets exactly that", netPayout(breakEvenList(credit, 0.10, h)),
       ex.perSeat!, 0.001);
}

// minListPrice must NOT move with the haircut. It is the platform's published rule -
// 80% of FACE - and Ticketmaster does not care what credit is worth to Wesley.

const puck = "2026-10-01T02:00:00Z";
const g = game(puck);
check("deadline is exactly 48h before puck drop",
  exchangeDeadline(g).toISOString(), "2026-09-29T02:00:00.000Z");

check("well before the deadline -> floor active",
  phaseOf(g, new Date("2026-09-01T00:00:00Z")), "floor_active");
check("one minute before the deadline -> still active",
  phaseOf(g, new Date("2026-09-29T01:59:00Z")), "floor_active");
check("exactly at the deadline -> expired",
  phaseOf(g, new Date("2026-09-29T02:00:00Z")), "floor_expired");
check("between deadline and puck drop -> expired",
  phaseOf(g, new Date("2026-09-30T12:00:00Z")), "floor_expired");
check("after puck drop -> past",
  phaseOf(g, new Date("2026-10-01T03:00:00Z")), "past");

near("hoursUntil", hoursUntil(new Date("2026-09-29T02:00:00Z"),
  new Date("2026-09-28T02:00:00Z")), 24);

// exits(): availability is what the UI branches on.
const before = exits(g, 51, 70, { instantOfferPerSeat: 24.3 });
const byKey = Object.fromEntries(before.map((e) => [e.key, e]));
near("resale exit nets list x 0.9", byKey.resale.perSeat!, 63);
check("exchange available while the floor is up", byKey.exchange.available, true);
check("exchange is guaranteed", byKey.exchange.guaranteed, true);
check("resale is not guaranteed", byKey.resale.guaranteed, false);
near("exchange pays the credit", byKey.exchange.perSeat!, 51);

const after = Object.fromEntries(
  exits(g, 51, 70, {}).map((e) => [e.key, e]),
);
check("instant offer unavailable when not captured", after.instant.available, false);

// The credit haircut is an assumed constant (ops#25) and must actually apply.
const hair = Object.fromEntries(
  exits(g, 51, 70, { creditHaircut: 0.75 }).map((e) => [e.key, e]),
);
near("credit haircut discounts the exchange exit", hair.exchange.perSeat!, 38.25);

// guidance(): the phase drives urgency, and a missing credit must not fabricate one.
const laterSeason: Game[] = [g, { ...game("2026-12-01T02:00:00Z"), gameId: 99 }];
const early = guidance(g, 51, laterSeason, new Date("2026-09-01T00:00:00Z"));
check("no urgency far out", early.urgency, "none");
near("guidance carries break-even", early.breakEven!, 56.6667, 0.001);

const soon = guidance(g, 51, laterSeason, new Date("2026-09-28T02:00:00Z"));
check("urgency inside 72h", soon.urgency, "soon");

const expired = guidance(g, 51, laterSeason, new Date("2026-09-30T00:00:00Z"));
check("urgency after the deadline", expired.urgency, "now");

const noCredit = guidance(g, null, laterSeason, new Date("2026-09-01T00:00:00Z"));
check("no credit -> no break-even invented", noCredit.breakEven, null);

// --- published exchange terms: the floor is not universal ------------------------
//
// From the Sharks365 Ticket Exchange FAQ: account credit expires at puck drop of the
// last game of the season, does not roll over, and cannot buy playoff tickets. So
// credit from returning a game must be spent on a regular-season home game that has
// not happened yet - and for the FINAL home game, none has.

const season: Game[] = [
  { ...game("2026-09-22T02:00:00Z"), gameId: 90, gameType: "preseason", tier: "PRESEASON" },
  { ...game("2026-10-01T02:00:00Z"), gameId: 91, tier: "A" },
  { ...game("2027-04-08T02:00:00Z"), gameId: 92, tier: "B" },
  { ...game("2027-04-10T02:00:00Z"), gameId: 93, tier: "A+" },
];
const [pre, first, secondLast, last] = season;

check("preseason game has every regular game as an outlet",
  remainingCreditOutlets(pre, season), 3);
check("first regular game has the later regular games", remainingCreditOutlets(first, season), 2);
check("second-to-last has exactly one", remainingCreditOutlets(secondLast, season), 1);
check("LAST home game has none", remainingCreditOutlets(last, season), 0);

// Preseason games are sources of credit but never places to spend it.
check("preseason is not counted as an outlet",
  remainingCreditOutlets({ ...game("2026-09-20T02:00:00Z"), gameId: 89 }, season), 3);

check("floor is real for the first game", exchangeIsRealFloor(first, season), true);
check("floor is NOT real for the last game", exchangeIsRealFloor(last, season), false);

// guidance() must not tell you the credit catches you when it does not.
const lastG = guidance(last, 120, season, new Date("2027-01-01T00:00:00Z"));
check("last game headline says no floor", lastG.headline, "No exchange floor - resale or nothing");
check("last game urgency is now", lastG.urgency, "now");
const firstG = guidance(first, 90, season, new Date("2026-09-01T00:00:00Z"));
check("a normal game still says list high", firstG.headline, "Floor active - list high");

// An empty season must assume the floor EXISTS - claiming otherwise without evidence
// would be worse than the bug this replaced.
check("no season provided -> floor assumed present",
  guidance(first, 90, [], new Date("2026-09-01T00:00:00Z")).headline, "Floor active - list high");

// exits(): the exchange must not be offered when credit cannot be spent.
const lastExits = Object.fromEntries(
  exits(last, 120, 200, { season }).map((e) => [e.key, e]),
);
check("exchange exit unavailable on the last game", lastExits.exchange.available, false);
check("and it says why", lastExits.exchange.note.includes("no games left"), true);
check("unsold is the real downside there",
  lastExits.unsold.note, "this is the real downside now");

const firstExits = Object.fromEntries(
  exits(first, 90, 200, { season }).map((e) => [e.key, e]),
);
check("exchange exit available on a normal game", firstExits.exchange.available, true);
check("unsold is avoidable on a normal game",
  firstExits.unsold.note, "avoidable - exchange is still open");

// The minimum list price - a floor the model previously did not have at all.
near("min list is 80% of face", minListPrice(150)!, 120);   // the FAQ's worked example
near("min list on a $51 preseason face", minListPrice(51)!, 40.8);
check("no credit -> no min list", minListPrice(null), null);
// Worst achievable net from a sale, which is what makes the endgame bite.
near("worst net at the floor is 0.72 x face", netPayout(minListPrice(100)!), 72);
// And it sits BELOW break-even, so the floor never blocks the list-high strategy -
// it only binds when marking down after the deadline.
check("the floor is below break-even", minListPrice(100)! < breakEvenList(100), true);

// After the deadline the guidance must name the floor rather than say "aggressively".
const afterDeadline = guidance(first, 90, season, new Date("2026-09-30T12:00:00Z"));
check("expired-floor guidance names the list floor",
  afterDeadline.detail.includes("floor is"), true);
check("expired-floor headline unchanged", afterDeadline.headline,
  "Floor expired - resale or nothing");

// --- outcomes: the censoring rule -----------------------------------------------

const games: Game[] = [
  game("2026-10-01T02:00:00Z"), game("2026-10-03T02:00:00Z"),
  game("2026-10-05T02:00:00Z"), game("2026-10-07T02:00:00Z"),
].map((x, i) => ({ ...x, gameId: i + 1 }));

const withOutcomes = (m: Record<number, string>): Profile => ({
  ...EMPTY_PROFILE,
  outcomes: Object.fromEntries(
    Object.entries(m).map(([k, v]) => [k, { kind: v as never, on: "2026-09-05", atList: 70, netPerSeat: null }]),
  ),
});

const now = new Date("2026-09-05T00:00:00Z");
let t = tally(withOutcomes({ 1: "sold", 2: "exchanged", 3: "unsold" }), games, now);
check("counts total outcomes", t.total, 3);
check("counts by kind", t.byKind, { sold: 1, exchanged: 1, unsold: 1, instant: 0 });
check("needed counts down to the fit threshold", t.needed, FIT_THRESHOLD - 3);

let r = sellRate(t);
// THE point of this module: an exchanged game is censored, not a failure to sell.
// Counted as a failure the rate would be 1/3; excluded it is 1/2.
check("exchanged games leave the denominator", r.concluded, 2);
near("sell rate excludes censored games", r.rate!, 0.5);
check("censored count reported", r.censored, 1);
check("not trustworthy below the threshold", r.trustworthy, false);

t = tally(withOutcomes({ 1: "instant" }), games, now);
r = sellRate(t);
check("an instant sale is a market conclusion", r.concluded, 1);
near("but it is not a 'sold'", r.rate!, 0);

// ---- the two prompts (ops#50) ----
// One outcome STOPS BEING POSSIBLE at the exchange deadline, so a single post-game prompt
// could never separate "chose the credit" from "went unsold" - and conflating them biases
// any later fit toward pessimism, counting a chosen exit as a failure to sell.
{
  // Deadline is T-48h before puck drop. Puck drop 2026-09-10T02:00Z -> deadline
  // 2026-09-08T02:00Z.
  const g = (id: number, startUTC: string): Game => ({
    gameId: id, date: startUTC.slice(0, 10), startTimeUTC: startUTC,
    opponent: { abbrev: "XXX", name: "X" }, tier: "A", gameType: "regular",
  } as Game);
  const gs = [g(1, "2026-09-10T02:00:00Z")];
  const none = EMPTY_PROFILE;

  // 12h before the deadline -> inside the window, still answerable both ways.
  let p1 = pending(none, gs, new Date("2026-09-07T14:00:00Z"));
  check("inside the closing window it prompts", p1.closing.map((x) => x.gameId), [1]);
  check("and not as played", p1.played, []);

  // 30h before the deadline -> further out than CLOSING_SOON_HOURS, no prompt yet.
  p1 = pending(none, gs, new Date("2026-09-06T20:00:00Z"));
  // NOTE: [a, b] not (a, b) - the comma operator would silently evaluate to `b` alone
  // and assert nothing about `closing`, which is the half these tests exist for.
  check("outside the window it is quiet", [p1.closing.length, p1.played.length], [0, 0]);

  // Exactly at and past the deadline: the exchange is GONE, so this is no longer the
  // closing prompt's business. h <= 0 must not be "closing".
  p1 = pending(none, gs, new Date("2026-09-08T02:00:00Z"));
  check("at the deadline it stops being a closing prompt", p1.closing, []);
  p1 = pending(none, gs, new Date("2026-09-09T00:00:00Z"));
  check("past the deadline but pre-game, neither bucket",
        [p1.closing.length, p1.played.length], [0, 0]);

  // After puck drop -> played, whatever the deadline did.
  p1 = pending(none, gs, new Date("2026-09-11T00:00:00Z"));
  check("after puck drop it is a played game", p1.played.map((x) => x.gameId), [1]);
  check("and not still closing", p1.closing, []);

  // A recorded outcome silences both prompts - the whole point.
  const done = withOutcomes({ 1: "exchanged" });
  p1 = pending(done, gs, new Date("2026-09-11T00:00:00Z"));
  check("a recorded outcome silences the prompt",
        [p1.closing.length, p1.played.length], [0, 0]);
  p1 = pending(done, gs, new Date("2026-09-07T14:00:00Z"));
  check("silenced inside the window too", p1.closing, []);

  check("the window is a day, not the whole 48h", CLOSING_SOON_HOURS, 24);
}

// ---- what is still POSSIBLE, not merely typeable (ops#55) ----
// guidance() and exits() already know the final home game has no exchange floor. The
// outcome recorder did not, so it let someone record a choice the app had already told
// them was worthless.
{
  const season = [
    { ...game("2026-10-01T02:00:00Z"), gameId: 1 },
    { ...game("2026-10-03T02:00:00Z"), gameId: 2 },
    { ...game("2027-04-10T02:00:00Z"), gameId: 3 },
  ] as Game[];

  check("an ordinary game offers all four outcomes",
        selectableOutcomes(season[0], season).length, 4);
  check("the FINAL home game does not offer 'exchanged'",
        selectableOutcomes(season[2], season).includes("exchanged"), false);
  check("but still offers the other three",
        selectableOutcomes(season[2], season).sort(),
        ["instant", "sold", "unsold"]);

  // An ALREADY-RECORDED exchange stays selectable. If it was recorded it happened, and
  // dropping it would blank the control and invite re-entry of something different -
  // rewriting history to match a model is how a dataset stops being evidence.
  check("a recorded exchange on the final game stays visible",
        selectableOutcomes(season[2], season, "exchanged").includes("exchanged"), true);
  // ...but only for that game. A recorded exchange elsewhere must not unlock it here.
  check("and the exemption does not leak to a different current value",
        selectableOutcomes(season[2], season, "sold").includes("exchanged"), false);

  // No season -> cannot tell. Allowing is the safe default: claiming "no floor" without
  // evidence is worse than the permissiveness it would replace.
  check("without a season, all options remain",
        selectableOutcomes(season[2], []).length, 4);
}

// ---- the outcome DATE (ops#54) ----
// `on` is documented as the date the outcome HAPPENED and is the dependent variable for
// P(sell | price, days-to-game). Recording the date it was TYPED biases days-to-game
// toward zero and makes sales look later than they were - and it fails quietly, producing
// well-formed rows that look exactly like a training set.
{
  const g = { ...game("2026-10-10T02:00:00Z"), date: "2026-10-09" } as Game;

  // Recorded on the day: today is before the game, so today is right.
  eqDate("recorded early, today wins",
         defaultOutcomeDate(g, new Date("2026-10-01T12:00:00Z")), "2026-10-01");
  // Recorded LATE - the failure this fixes. No outcome can happen after puck drop, so the
  // default is capped at the game date rather than growing with how long someone took to
  // open the app.
  eqDate("recorded three weeks late, capped at the game date",
         defaultOutcomeDate(g, new Date("2026-10-30T12:00:00Z")), "2026-10-09");
  eqDate("recorded on the day of the game", 
         defaultOutcomeDate(g, new Date("2026-10-09T23:00:00Z")), "2026-10-09");
  // The bias this bounds: without the cap, a late entry would read 21 days closer to the
  // game than the truth. Asserted as a property, not a date.
  const late = defaultOutcomeDate(g, new Date("2026-10-30T12:00:00Z"));
  check("the default is never after puck drop", late <= g.date, true);

  // THE BOUNDARY THE FIRST VERSION GOT WRONG. `today` was computed from toISOString(),
  // a UTC calendar date, and compared against game.date, which is ARENA-LOCAL. Pacific
  // trails UTC by 7-8h, so UTC rolls over first: a genuine sale at 21:00 Pacific on the
  // evening BEFORE the game returned the game's date - one day late, every evening,
  // reintroducing the exact late-side bias this function removes. Found in review; both
  // earlier tests sat away from the boundary and could not see it.
  eqDate("21:00 Pacific the evening before is NOT the game date",
         defaultOutcomeDate(g, new Date("2026-10-09T04:00:00Z")), "2026-10-08");
  eqDate("arena today is arena-local, not UTC",
         arenaToday(new Date("2026-10-09T04:00:00Z")), "2026-10-08");
  eqDate("and agrees with UTC in the middle of the arena's day",
         arenaToday(new Date("2026-10-09T19:00:00Z")), "2026-10-09");
}

// A supplied date must be KEPT, not silently replaced - ops#54 asked for exactly this
// assertion, and the first attempt left the setter inside the component where no test
// could reach it.
{
  const withOne: Profile = {
    ...EMPTY_PROFILE,
    outcomes: { "1": { kind: "sold", on: "2026-10-09", atList: 70, netPerSeat: 63 } },
  };
  check("a supplied date is stored",
        withOutcomeDate(withOne, 1, "2026-10-02").outcomes!["1"].on, "2026-10-02");
  check("and the rest of the outcome is untouched",
        withOutcomeDate(withOne, 1, "2026-10-02").outcomes!["1"].atList, 70);
  // A blank date is worse than a slightly wrong one: every consumer treats `on` as real.
  check("an empty date is refused", withOutcomeDate(withOne, 1, "").outcomes!["1"].on,
        "2026-10-09");
  check("a game with no outcome is untouched",
        withOutcomeDate(withOne, 99, "2026-10-02").outcomes!["1"].on, "2026-10-09");
  check("and the input profile is not mutated", withOne.outcomes!["1"].on, "2026-10-09");
}

// tally().missed and pending().played must be the SAME set, because they are now the same
// derivation. They used to be two code paths applying one rule, which agreed and could
// drift - at which point the banner and the panel beneath it would disagree about the
// same games.
{
  const gs = [game("2026-09-01T02:00:00Z"), game("2026-12-01T03:00:00Z")].map(
    (x, i) => ({ ...x, gameId: i + 1 } as Game),
  );
  const at = new Date("2026-10-01T00:00:00Z");
  check("missed is exactly pending().played",
        tally(EMPTY_PROFILE, gs, at).missed.map((x) => x.gameId),
        pending(EMPTY_PROFILE, gs, at).played.map((x) => x.gameId));
  check("and it is the played one", tally(EMPTY_PROFILE, gs, at).missed.map((x) => x.gameId), [1]);
}

// ---- export (ops#50) ----
// Outcomes live in localStorage: right for privacy, and one cleared-site-data from
// destroying up to 44 irreplaceable observations with no copy anywhere.
{
  const payload = exportPayload(withOutcomes({ 2: "sold", 1: "unsold" }), games,
                                new Date("2026-09-05T00:00:00Z"));
  check("count matches", payload.count, 2);
  check("sorted by game date", payload.outcomes.map((o) => o.gameId), [1, 2]);
  check("carries the game, not just the number", payload.outcomes[0].opponent, "XXX");
  check("carries the outcome kind", payload.outcomes[0].kind, "unsold");
  check("stamped", payload.exportedAt, "2026-09-05T00:00:00.000Z");
  check("says it is private", payload._what.includes("PRIVATE"), true);
  const empty = exportPayload(EMPTY_PROFILE, games, new Date("2026-09-05T00:00:00Z"));
  check("an empty set exports cleanly", empty.count, 0);
  check("with no rows", empty.outcomes, []);
}

r = sellRate(tally(withOutcomes({ 1: "exchanged", 2: "exchanged" }), games, now));
check("all-censored gives no rate rather than zero", r.rate, null);

// Played games with nothing recorded are the unrecoverable loss.
t = tally(EMPTY_PROFILE, games, new Date("2026-10-04T00:00:00Z"));
check("played-and-unrecorded games surfaced", t.missed.length, 2);
t = tally(EMPTY_PROFILE, games, now);
check("future games are not 'missed'", t.missed.length, 0);

// --- profile: the fragment round-trip -------------------------------------------

const full: Profile = {
  v: 1,
  seats: { section: "111", row: "9", seats: ["1", "2"] },
  invoiceTotal: 1234,
  credits: { "A+": 111, PRESEASON: 22 },
  listPrices: { "999": 70 },
  outcomes: { "999": { kind: "sold", on: "2026-09-05", atList: 70, netPerSeat: 63 } },
  feeObservations: [{ list: 70, net: 63, on: "2026-09-05" }],
  instantOffers: { "999": [{ on: "2026-09-05", offerPerTicket: 24.3 }] },
};
check("profile round-trips through the fragment encoding",
  decodeProfile(encodeProfile(full)), full);
check("instant offers survive the round-trip",
  decodeProfile(encodeProfile(full))!.instantOffers!["999"][0].offerPerTicket, 24.3);

// Forward migration: a profile encoded before a field existed must decode with that
// field defaulted, not undefined. This is what makes an old share link keep working -
// and it is why decodeProfile spreads EMPTY_PROFILE first.
const legacy = { v: 1, seats: { section: "111", row: "9", seats: ["1"] },
  invoiceTotal: null, credits: { A: 1 }, listPrices: {} };
const migrated = decodeProfile(btoa(JSON.stringify(legacy)))!;
check("an old profile gains outcomes as an empty object", migrated.outcomes, {});
check("an old profile gains instantOffers as an empty object", migrated.instantOffers, {});
check("an old profile gains feeObservations as an empty list", migrated.feeObservations, []);
check("an old profile keeps its own values", migrated.credits, { A: 1 });
check("outcomes survive the round-trip",
  decodeProfile(encodeProfile(full))!.outcomes!["999"].kind, "sold");
check("garbage decodes to null", decodeProfile("!!!not-base64!!!"), null);
check("a wrong version decodes to null", decodeProfile(btoa(JSON.stringify({ v: 2 }))), null);

check("empty profile is not configured", isConfigured(EMPTY_PROFILE), false);
check("section alone is not enough", isConfigured({ ...EMPTY_PROFILE, seats: { section: "1", row: "", seats: [] } }), false);
check("section plus a credit is configured", isConfigured(full), true);
check("seatCount floors at 1", seatCount(EMPTY_PROFILE), 1);
check("seatCount counts seats", seatCount(full), 2);
near("invoice per seat divides by seat count", invoicePerSeat(full)!, 617);
check("no invoice -> null per seat", invoicePerSeat(EMPTY_PROFILE), null);

// --- fees: calibration from observed (list, net) pairs --------------------------

import { calibrate, impliedRate, listToNetUnder, netUnder } from "../src/lib/fees.ts";

const obs = (list: number, net: number) => ({ list, net, on: "2026-09-05" });

near("implied rate of a single pair", impliedRate(obs(70, 63)), 0.1);

// The two real measurements. A flat 10% must come out flat, with no fixed component.
let cal = calibrate([obs(77, 69.3), obs(70, 63)]);
near("rate from the two measured pairs", cal.rate!, 0.1, 0.0005);
near("no fixed component implied", cal.fixed!, 0, 0.01);
check("consistent observations produce no findings", cal.findings, []);
check("two agreeing observations are usable", cal.usable, true);

// One observation derives a rate but must NOT be called usable: it cannot distinguish a
// percentage from a percentage-plus-fixed-fee.
cal = calibrate([obs(70, 63)]);
near("single observation still gives a rate", cal.rate!, 0.1);
check("single observation is not usable", cal.usable, false);
check("single observation pins fixed at zero", cal.fixed, 0);

// Same price level twice: a fixed component is unidentifiable and must be said so.
cal = calibrate([obs(70, 63), obs(70, 63)]);
check("same-price observations flag unidentifiability",
  cal.findings.some((f) => f.includes("cannot be separated")), true);
check("same-price observations are not usable", cal.usable, false);

// A FIXED per-ticket fee on top of a percentage: net = list*0.9 - 2.
cal = calibrate([obs(50, 50 * 0.9 - 2), obs(200, 200 * 0.9 - 2)]);
near("fixed component recovered", cal.fixed!, 2, 0.01);
near("rate recovered alongside a fixed fee", cal.rate!, 0.1, 0.0005);
check("fixed component is reported",
  cal.findings.some((f) => f.includes("fixed per-ticket component")), true);
check("a fixed component makes the fit unusable for plain break-even", cal.usable, false);

// A STEPPED rate: 10% at a low price, 15% at a high one. Must not be averaged away.
cal = calibrate([obs(50, 45), obs(500, 425)]);
check("stepped rate produces a finding", cal.findings.length > 0, true);
check("stepped rate is not usable", cal.usable, false);

// Nonsense observations are dropped and reported, not fitted.
cal = calibrate([obs(70, 63), obs(0, 5), obs(-10, 5)]);
check("bad observations dropped", cal.n, 1);
check("dropping is reported", cal.findings.some((f) => f.includes("ignored")), true);
check("no observations at all", calibrate([]).rate, null);

// netUnder / listToNetUnder must invert each other, fixed component included.
cal = calibrate([obs(50, 50 * 0.9 - 2), obs(200, 200 * 0.9 - 2)]);
near("netUnder applies the fixed component", netUnder(cal, 100)!, 88, 0.01);
near("listToNetUnder inverts netUnder", netUnder(cal, listToNetUnder(cal, 61)!)!, 61, 0.01);

// --- offers: instant-offer history ----------------------------------------------

import { impliedBid, isRoundDollar, series, verdictAgainstCredit } from "../src/lib/offers.ts";

// All four real samples invert to exact round dollars.
for (const [offer, bid] of [[24.3, 27], [27.9, 31], [87.3, 97], [96.3, 107]] as const) {
  near(`$${offer} implies a $${bid} bid`, impliedBid(offer, 0.1), bid, 0.005);
  check(`$${bid} is a round dollar`, isRoundDollar(impliedBid(offer, 0.1)), true);
}
check("a non-round bid is detected", isRoundDollar(impliedBid(24.5, 0.1)), false);

// Every sample so far nets below its tier credit, which is the load-bearing observation.
check("preseason offer is below the $51 credit", verdictAgainstCredit(24.3, 51), "below_credit");
check("A+ offer is below the $120 credit", verdictAgainstCredit(87.3, 120), "below_credit");
check("an offer above the credit is reported as such", verdictAgainstCredit(60, 51), "above_credit");
check("no credit means no verdict", verdictAgainstCredit(24.3, null), "no_credit");

let sr = series(1, [
  { on: "2026-09-05", offerPerTicket: 24.3 },
  { on: "2026-09-03", offerPerTicket: 20.0 },
]);
check("offers sorted oldest first", sr.offers.map((o) => o.on), ["2026-09-03", "2026-09-05"]);
near("latest offer is the newest", sr.latest!.offerPerTicket, 24.3);
near("delta measured from the first observation", sr.delta!, 4.3, 0.005);

sr = series(1, [{ on: "2026-09-05", offerPerTicket: 24.3 }]);
check("single offer has no delta", sr.delta, null);
sr = series(1, undefined);
check("no offers -> no latest", sr.latest, null);
check("no offers -> no delta", sr.delta, null);

// --- pnl: season roll-up --------------------------------------------------------

import {
  EXPECTED_RESIDUAL_RATE, RESIDUAL_ALARM_RATE, RESIDUAL_ELEVATED_RATE,
  doNothingBaseline, grossCashProceeds, reconciliation, seasonPnl,
} from "../src/lib/pnl.ts";

const pnlGames: Game[] = [
  { ...game("2026-10-01T02:00:00Z"), gameId: 1, tier: "A" },
  { ...game("2026-10-03T02:00:00Z"), gameId: 2, tier: "A" },
  { ...game("2026-10-05T02:00:00Z"), gameId: 3, tier: "B" },
  { ...game("2026-10-07T02:00:00Z"), gameId: 4, tier: "B" },
];

const pnlProfile = (outcomes: Profile["outcomes"], extra: Partial<Profile> = {}): Profile => ({
  ...EMPTY_PROFILE,
  seats: { section: "1", row: "1", seats: ["1", "2"] },
  invoiceTotal: 800,           // 2 seats -> $400/seat
  credits: { A: 100, B: 90 },  // face proxy: 2x100 + 2x90 = $380/seat
  outcomes,
  ...extra,
});

// Nothing recorded: everything is unresolved basis, and none of it is a loss.
let pl = seasonPnl(pnlProfile({}), pnlGames);
check("face total sums the tier credits", pl.faceTotal, 380);
near("invoice per seat divides by seats", pl.invoicePerSeat!, 400);
near("residual is invoice minus face", pl.nonRefundableResidual!, 20);
near("all basis unresolved", pl.unresolvedBasis, 380);
check("nothing written off before anything resolves", pl.writtenOff, 0);
check("no games resolved", pl.resolved, 0);

// A sale recovers CASH; an exchange recovers CREDIT and the two must not be conflated.
pl = seasonPnl(
  pnlProfile({
    "1": { kind: "sold", on: "2026-09-05", atList: 130, netPerSeat: 117 },
    "2": { kind: "exchanged", on: "2026-09-05", atList: null, netPerSeat: null },
  }),
  pnlGames,
);
near("cash from the sale", pl.cash, 117);
near("credit from the exchange", pl.credit, 100);
check("unspent credit is not counted as spent", pl.creditSpent, 0);
near("unspent credit reported separately", pl.creditUnspent, 100);
near("sale beat its basis", pl.lines[0].delta!, 17);
near("exchange exactly matched its basis", pl.lines[1].delta!, 0);
near("unresolved basis excludes resolved games", pl.unresolvedBasis, 180);
check("two games resolved", pl.resolved, 2);

// Spent credit is tracked distinctly - this is ops#13's explicit requirement.
pl = seasonPnl(
  pnlProfile({
    "2": { kind: "exchanged", on: "2026-09-05", atList: null, netPerSeat: null, creditSpent: true },
  }),
  pnlGames,
);
near("spent credit counted as spent", pl.creditSpent, 100);
check("nothing left unspent", pl.creditUnspent, 0);

// An unsold game writes off its basis.
pl = seasonPnl(
  pnlProfile({ "3": { kind: "unsold", on: "2026-09-05", atList: 95, netPerSeat: null } }),
  pnlGames,
);
near("unsold writes off basis", pl.writtenOff, 90);
near("unsold delta is the full basis lost", pl.lines[2].delta!, -90);

// An instant sale is cash, not credit.
pl = seasonPnl(
  pnlProfile({ "1": { kind: "instant", on: "2026-09-05", atList: null, netPerSeat: 27 } }),
  pnlGames,
);
near("instant offer counts as cash", pl.cash, 27);
check("instant offer is not credit", pl.credit, 0);

// 1099-K: GROSS cash, before basis, and excluding credit. Netting basis here would
// produce a number that does not match the form.
pl = seasonPnl(
  pnlProfile({
    "1": { kind: "sold", on: "2026-09-05", atList: 130, netPerSeat: 117 },
    "2": { kind: "exchanged", on: "2026-09-05", atList: null, netPerSeat: null },
    "3": { kind: "unsold", on: "2026-09-05", atList: null, netPerSeat: null },
  }),
  pnlGames,
);
near("gross proceeds are cash only", grossCashProceeds(pl), 117);

// A missing tier credit must make face and the residual UNKNOWN rather than understated.
pl = seasonPnl(pnlProfile({}, { credits: { A: 100 } }), pnlGames);
check("missing basis counted", pl.missingBasis, 2);
check("face is null when any game's basis is unknown", pl.faceTotal, null);
check("residual is null when face is unknown", pl.nonRefundableResidual, null);

// ---- reconciliation is JUDGED, not just displayed (sweep idea #18) ----
// The app always showed "invoice minus total face = residual". It never said whether that
// was plausible - so a mistyped tier credit rendered as a larger, legitimate-looking
// residual. Tier credits are the cost basis AND the exchange payout, so one bad entry
// moves every break-even in the model, in the same direction, silently.
{
  // pnlProfile: invoice 800 over 2 seats = 400/seat; face = 2x100 + 2x90 = 380/seat.
  // Residual 20/400 = 5%, which is ALREADY above the 3% alarm - so the fixture itself
  // demonstrates the check firing on realistic-looking data.
  const p0 = seasonPnl(pnlProfile({}), pnlGames);
  const r0 = reconciliation(p0);
  near("residual rate is computed", r0.rate!, 0.05);
  check("a 5% residual is implausible against a measured 0.3%", r0.implausible, true);
  check("and it says to check the credits", r0.message!.includes("mistyped"), true);

  // A residual inside tolerance says nothing at all.
  const tight = seasonPnl(pnlProfile({}, { invoiceTotal: 762 }), pnlGames); // 381/seat vs 380
  check("a small residual is not flagged", reconciliation(tight).implausible, false);
  check("and does not even raise a notice", reconciliation(tight).elevated, false);
  near("and its rate is tiny", reconciliation(tight).rate!, 0.0026, 0.0005);

  // THE CASE THE FIRST VERSION MISSED, on a REALISTIC tier structure rather than the toy
  // fixture. The toy has 4 games over 2 tiers, so one tier is 53% of face and any typo
  // there looks enormous - it proves nothing about the real season.
  //
  // Real games-per-tier from config/tiers.json (A+ 7, A 8, B 7, C 11, D 9 = 42). Credit
  // PROPORTIONS follow the public market medians in check_tier_market.py; the absolute
  // values are synthetic and absurd, because real credits are private (rule 1).
  //
  // Computed across all five tiers, a 10% slip on any ONE lands at 1.37%-2.93% of the
  // invoice - every one of them UNDER a 3% alarm and OVER a 1% notice. That range is why
  // there are two bands and why the first single threshold could not work.
  {
    const counts: Record<string, number> = { "A+": 7, A: 8, B: 7, C: 11, D: 9 };
    const credits: Record<string, number> = { "A+": 1380, A: 1270, B: 940, C: 720, D: 460 };
    const realish: Game[] = [];
    let id = 100;
    for (const t of Object.keys(counts)) {
      for (let i = 0; i < counts[t]; i++) {
        realish.push({ ...game("2026-11-01T03:00:00Z"), gameId: id++, tier: t } as Game);
      }
    }
    const face = realish.reduce((a, g) => a + credits[g.tier as string], 0);
    const invoice = (face / (1 - EXPECTED_RESIDUAL_RATE)) * 2; // 2 seats

    const ok = seasonPnl(
      { ...EMPTY_PROFILE, seats: { section: "1", row: "1", seats: ["1", "2"] },
        invoiceTotal: invoice, credits: credits as never }, realish);
    check("a correctly-entered 42-game season is silent", reconciliation(ok).elevated, false);
    check("and not alarming either", reconciliation(ok).implausible, false);

    // Every tier, not just a convenient one: the band must catch a slip anywhere.
    for (const t of Object.keys(counts)) {
      const typo = { ...credits, [t]: Math.round(credits[t] * 0.9) };
      const r = reconciliation(seasonPnl(
        { ...EMPTY_PROFILE, seats: { section: "1", row: "1", seats: ["1", "2"] },
          invoiceTotal: invoice, credits: typo as never }, realish));
      check(`a 10% slip on tier ${t} is caught as elevated`, r.elevated, true);
      check(`and tier ${t} does NOT reach the alarm - which is why one threshold failed`,
            r.implausible, false);
    }
  }

  // Face EXCEEDING the invoice cannot happen - a credit is too high. Different message,
  // because it points at a different mistake.
  const over = seasonPnl(pnlProfile({}, { invoiceTotal: 700 }), pnlGames); // 350/seat vs 380
  check("face above invoice is implausible", reconciliation(over).implausible, true);
  check("and says the credit is too HIGH",
        reconciliation(over).message!.includes("too high"), true);
  check("its rate is negative", reconciliation(over).rate! < 0, true);

  // Unknowns must stay silent rather than guess.
  check("no invoice -> nothing to say",
        reconciliation(seasonPnl(pnlProfile({}, { invoiceTotal: null }), pnlGames)).implausible,
        false);
  check("a missing tier credit -> nothing to say",
        reconciliation(seasonPnl(pnlProfile({}, { credits: { A: 100 } }), pnlGames)).implausible,
        false);
  check("the alarm is ten times the measured residual, not a tight fit",
        RESIDUAL_ALARM_RATE, 0.03);
}

// ---- the do-nothing baseline (sweep idea #29) ----
// Every recommendation should beat "return every ticket for credit", and nothing measured
// against that until now.
{
  const p = pnlProfile({});
  const b = doNothingBaseline(p, pnlGames);

  // THE POINT: not the sum of all four credits. The LAST home game yields credit that can
  // only be spent on a regular-season home game which has not happened - and there is
  // none - so it is worth zero and excluded. Naive face total per seat is 380; the
  // achievable baseline is 290, because the final B game (90) cannot be exchanged usefully.
  check("the final game has no exchange floor and is excluded", b.noFloor, 1);
  check("three games counted, not four", b.games, 3);
  near("per seat excludes the unexchangeable game", b.perSeat, 290);
  near("total scales by seats", b.total, 580);
  // Guard the exact overstatement the naive version would produce.
  check("and it is strictly below the naive face total", b.perSeat < 380, true);

  // Credit is not cash. Comparing a cash strategy against a face-value credit baseline
  // would flatter the credit.
  const h = doNothingBaseline(p, pnlGames, 0.9);
  near("haircut applies", h.perSeat, 261);
  near("and to the total", h.total, 522);

  // A tier with no credit entered cannot be valued - counted, not guessed at zero.
  const partial = doNothingBaseline(pnlProfile({}, { credits: { A: 100 } }), pnlGames);
  check("missing basis is reported", partial.missingBasis, 2);
  check("and those games are not silently counted", partial.games, 2);
  near("only the games with a known credit contribute", partial.perSeat, 200);

  // An empty schedule must not divide by anything or invent a floor.
  const none = doNothingBaseline(p, []);
  check("no games -> zero baseline", [none.perSeat, none.games], [0, 0]);
}

// No invoice entered -> no residual claim.
pl = seasonPnl(pnlProfile({}, { invoiceTotal: null }), pnlGames);
check("no invoice -> no per-seat invoice", pl.invoicePerSeat, null);
check("no invoice -> no residual", pl.nonRefundableResidual, null);
check("face still computable without an invoice", pl.faceTotal, 380);

// A sold outcome with no recorded net must not invent one from the list price.
pl = seasonPnl(
  pnlProfile({ "1": { kind: "sold", on: "2026-09-05", atList: 130, netPerSeat: null } }),
  pnlGames,
);
check("no net recorded -> no cash invented", pl.cash, 0);

// --- report ---------------------------------------------------------------------

for (const f of fails) console.error(`  FAIL ${f}`);
console.log(`self-test: ${fails.length ? "FAILED" : "passed"} (${fails.length} failure(s))`);
process.exit(fails.length ? 1 : 0);
