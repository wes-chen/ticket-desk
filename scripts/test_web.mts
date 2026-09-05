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
  breakEvenList, exchangeDeadline, exits, guidance, hoursUntil, listToNet, netPayout,
  phaseOf, type Game,
} from "../src/lib/economics.ts";
import { FIT_THRESHOLD, sellRate, tally } from "../src/lib/outcomes.ts";
import {
  EMPTY_PROFILE, decodeProfile, encodeProfile, invoicePerSeat, isConfigured, seatCount,
  type Profile,
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

const game = (startUTC: string): Game => ({
  gameId: 1, date: startUTC.slice(0, 10), startTimeUTC: startUTC,
  gameType: "regular", opponent: { abbrev: "XXX", name: "Test Team" }, tier: "A",
});

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
const early = guidance(g, 51, new Date("2026-09-01T00:00:00Z"));
check("no urgency far out", early.urgency, "none");
near("guidance carries break-even", early.breakEven!, 56.6667, 0.001);

const soon = guidance(g, 51, new Date("2026-09-28T02:00:00Z"));
check("urgency inside 72h", soon.urgency, "soon");

const expired = guidance(g, 51, new Date("2026-09-30T00:00:00Z"));
check("urgency after the deadline", expired.urgency, "now");

const noCredit = guidance(g, null, new Date("2026-09-01T00:00:00Z"));
check("no credit -> no break-even invented", noCredit.breakEven, null);

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

// --- report ---------------------------------------------------------------------

for (const f of fails) console.error(`  FAIL ${f}`);
console.log(`self-test: ${fails.length ? "FAILED" : "passed"} (${fails.length} failure(s))`);
process.exit(fails.length ? 1 : 0);
