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
};
check("profile round-trips through the fragment encoding",
  decodeProfile(encodeProfile(full)), full);
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

// --- report ---------------------------------------------------------------------

for (const f of fails) console.error(`  FAIL ${f}`);
console.log(`self-test: ${fails.length ? "FAILED" : "passed"} (${fails.length} failure(s))`);
process.exit(fails.length ? 1 : 0);
