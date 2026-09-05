import { useEffect, useMemo, useState } from "react";
import scheduleData from "../data/schedule.json";
import economics from "../config/economics.json";
import Setup from "./components/Setup";
import {
  MIN_LIST_RATIO_OF_FACE,
  breakEvenList,
  exchangeIsRealFloor,
  minListPrice,
  exchangeDeadline,
  fmt,
  guidance,
  hoursUntil,
  netPayout,
  phaseOf,
  type Game,
} from "./lib/economics";
import {
  EMPTY_PROFILE,
  consumeProfileFromHash,
  isConfigured,
  type Profile,
  type Tier,
} from "./lib/profile";
import { useLocalStorage } from "./lib/store";
import { STALE_THRESHOLD, market, marketFor, staleDays, standing, standingNote } from "./lib/market";
import { FIT_THRESHOLD, OUTCOME_LABEL, outcomeFor, sellRate, tally } from "./lib/outcomes";
import type { OutcomeKind } from "./lib/profile";
import { calibrate } from "./lib/fees";
import SellerObservations from "./components/SellerObservations";
import SeasonPnlPanel from "./components/SeasonPnl";

const GAMES = scheduleData.games as Game[];
const FEE = economics.resale.platforms.ticketmaster.sellerFeeRate;

const TIER_COLOR: Record<string, string> = {
  "A+": "bg-orange-500/15 text-orange-700 dark:text-orange-300 ring-orange-500/30",
  A: "bg-amber-500/15 text-amber-700 dark:text-amber-300 ring-amber-500/30",
  B: "bg-teal-500/15 text-teal-700 dark:text-teal-300 ring-teal-500/30",
  C: "bg-sky-500/15 text-sky-700 dark:text-sky-300 ring-sky-500/30",
  D: "bg-slate-500/15 text-slate-700 dark:text-slate-300 ring-slate-500/30",
  PRESEASON: "bg-purple-500/15 text-purple-700 dark:text-purple-300 ring-purple-500/30",
};

export default function App() {
  const [profile, setProfile] = useLocalStorage<Profile>("td.profile.v1", EMPTY_PROFILE);
  const [showSetup, setShowSetup] = useState(false);
  const [imported, setImported] = useState(false);

  // A profile arriving in the URL fragment wins over whatever is already stored,
  // and is scrubbed from the address bar immediately after being read.
  useEffect(() => {
    const fromHash = consumeProfileFromHash();
    if (fromHash) {
      setProfile(fromHash);
      setImported(true);
      setTimeout(() => setImported(false), 4000);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const tierCounts = useMemo(() => {
    const counts: Record<string, number> = {};
    for (const g of GAMES) if (g.tier) counts[g.tier] = (counts[g.tier] ?? 0) + 1;
    return counts;
  }, []);

  const configured = isConfigured(profile);

  if (!configured || showSetup) {
    return (
      <div className="min-h-screen bg-slate-50 text-slate-900 dark:bg-slate-950 dark:text-slate-100">
        <Setup
          profile={profile}
          onChange={setProfile}
          tierCounts={tierCounts}
          firstRun={!configured}
          onDone={() => setShowSetup(false)}
        />
      </div>
    );
  }

  return (
    <Dashboard
      profile={profile}
      setProfile={setProfile}
      onOpenSetup={() => setShowSetup(true)}
      imported={imported}
    />
  );
}

function Dashboard({
  profile,
  setProfile,
  onOpenSetup,
  imported,
}: {
  profile: Profile;
  setProfile: (p: Profile) => void;
  onOpenSetup: () => void;
  imported: boolean;
}) {
  const now = new Date();

  // ops#9: derive the fee rate from observations when they actually support it, rather
  // than trusting a constant that could go stale with no signal. A single observation,
  // or observations that disagree, or ones implying a fixed per-ticket component, are
  // NOT used - calibrate() reports those as findings and `usable` stays false. Falling
  // back to the configured rate is the conservative choice: it is measured at two price
  // points, which is more than one uncorroborated entry.
  const cal = useMemo(() => calibrate(profile.feeObservations ?? []), [profile.feeObservations]);
  const feeRate = cal.usable && cal.rate != null ? cal.rate : FEE;

  const rows = useMemo(() => {
    return GAMES.map((g) => {
      const credit = g.tier ? profile.credits[g.tier as Tier] ?? null : null;
      const be = credit == null ? null : breakEvenList(credit, feeRate);
      const list = profile.listPrices[String(g.gameId)] ?? null;
      const net = list == null ? null : netPayout(list, feeRate);
      const delta = net == null || credit == null ? null : net - credit;
      const mk = marketFor(g.gameId);
      const outcome = outcomeFor(profile, g.gameId);
      const floor = minListPrice(credit);
      const hasFloor = exchangeIsRealFloor(g, GAMES);
      return { g, credit, be, list, net, delta, mk, outcome, floor, hasFloor,
               guide: guidance(g, credit, GAMES, now) };
    });
  }, [profile, now, feeRate]);

  const deadlineSoon = rows
    .filter((r) => phaseOf(r.g, now) === "floor_active" && hoursUntil(exchangeDeadline(r.g), now) <= 72)
    .slice(0, 5);

  const counts = useMemo(() => tally(profile, GAMES, now), [profile, now]);
  const rate = useMemo(() => sellRate(counts), [counts]);

  const setOutcome = (gameId: number, kind: string) => {
    const next = { ...(profile.outcomes ?? {}) };
    if (kind === "") {
      delete next[String(gameId)];
    } else {
      const list = profile.listPrices[String(gameId)] ?? null;
      next[String(gameId)] = {
        kind: kind as OutcomeKind,
        on: new Date().toISOString().slice(0, 10),
        // Captured at the moment of recording, not read back later: the list price can
        // change afterwards, and an outcome is only meaningful against the price that
        // was actually standing when it happened.
        atList: list,
        netPerSeat: kind === "sold" && list != null ? Number((list * (1 - feeRate)).toFixed(2)) : null,
      };
    }
    setProfile({ ...profile, outcomes: next });
  };

  const setList = (gameId: number, v: string) => {
    const next = { ...profile.listPrices };
    if (v === "") delete next[String(gameId)];
    else next[String(gameId)] = Number(v);
    setProfile({ ...profile, listPrices: next });
  };

  return (
    <div className="min-h-screen bg-slate-50 text-slate-900 dark:bg-slate-950 dark:text-slate-100">
      <header className="border-b border-slate-200 bg-teal-800 text-white dark:border-slate-800">
        <div className="mx-auto flex max-w-7xl items-start justify-between gap-4 px-6 py-6">
          <div>
            <h1 className="text-2xl font-semibold tracking-tight">Ticket Desk</h1>
            <p className="mt-1 text-sm text-teal-100">
              Section {profile.seats.section} &middot; Row {profile.seats.row}
              {profile.seats.seats.length > 0 && <> &middot; Seats {profile.seats.seats.join(" & ")}</>}
              <span className="mx-2 opacity-50">|</span>
              {GAMES.length} home games
              <span className="mx-2 opacity-50">|</span>
              {(feeRate * 100).toFixed((feeRate * 100) % 1 === 0 ? 0 : 2)}% seller fee
              {feeRate !== FEE && <> <span className="opacity-75">(calibrated)</span></>}
            </p>
            <p className="mt-1 text-xs text-teal-200/70">
              {/* ops#18: make staleness visible rather than invisible. A precaching
                  service worker means what you are looking at may predate the last
                  deploy, and the app's highest-value moment is an irreversible
                  deadline. */}
              Build {__BUILD_TIME__.slice(0, 16).replace("T", " ")}Z &middot; market data{" "}
              {market().lastObservedDate ?? "none"}
            </p>
          </div>
          <button
            onClick={onOpenSetup}
            className="shrink-0 rounded border border-teal-500/50 px-3 py-1.5 text-sm hover:bg-teal-700"
          >
            Settings
          </button>
        </div>
      </header>

      <main className="mx-auto max-w-7xl px-6 py-8">
        {imported && (
          <div className="mb-6 rounded-lg border border-emerald-300 bg-emerald-50 p-3 text-sm text-emerald-900 dark:border-emerald-800/50 dark:bg-emerald-950/30 dark:text-emerald-200">
            Profile imported from link and saved to this device. The link has been cleared from the
            address bar.
          </div>
        )}

        {(() => {
          // A stale series is worse than no series: the numbers still look authoritative.
          // ops#24.
          const stale = staleDays(now);
          return stale != null && stale >= STALE_THRESHOLD ? (
            <div className="mb-6 rounded-lg border border-amber-300 bg-amber-50 p-3 text-sm text-amber-900 dark:border-amber-800/50 dark:bg-amber-950/30 dark:text-amber-200">
              <strong>Market data is {stale} days old</strong> &mdash; last collected{" "}
              {market().lastObservedDate}. The collector may have stopped. Arena asks below are
              stale, not current.
            </div>
          ) : null;
        })()}

        {deadlineSoon.length > 0 && (
          <section className="mb-8 rounded-lg border border-red-300 bg-red-50 p-5 dark:border-red-800/50 dark:bg-red-950/30">
            <h2 className="text-sm font-semibold uppercase tracking-wide text-red-700 dark:text-red-300">
              Exchange deadline within 72h
            </h2>
            <p className="mt-1 text-xs text-red-700/80 dark:text-red-300/70">
              After the deadline an unsold ticket is worth $0. Decide before then.
            </p>
            <ul className="mt-3 space-y-1 text-sm">
              {deadlineSoon.map((r) => (
                <li key={r.g.gameId} className="tabular-nums">
                  <strong>{r.g.date}</strong> vs {r.g.opponent.abbrev} &mdash;{" "}
                  {Math.floor(hoursUntil(exchangeDeadline(r.g), now))}h left, credit {fmt(r.credit)},
                  break-even {fmt(r.be)}
                </li>
              ))}
            </ul>
          </section>
        )}

        <section className="overflow-x-auto rounded-lg border border-slate-200 bg-white dark:border-slate-800 dark:bg-slate-900">
          <table className="w-full min-w-[56rem] text-sm">
            <thead className="border-b border-slate-200 text-left text-xs uppercase tracking-wide text-slate-500 dark:border-slate-800">
              <tr>
                <th className="px-4 py-3 font-medium">Date</th>
                <th className="px-4 py-3 font-medium">Opponent</th>
                <th className="px-4 py-3 font-medium">Tier</th>
                <th className="px-4 py-3 text-right font-medium">Credit</th>
                <th className="px-4 py-3 text-right font-medium">Break-even</th>
                <th
                  className="px-4 py-3 text-right font-medium"
                  title="Cheapest and priciest listing in the WHOLE ARENA on TickPick, all-in. Not a comp for your section, and not the channel you sell on."
                >
                  Arena ask <span className="font-normal normal-case opacity-60">(TickPick)</span>
                </th>
                <th className="px-4 py-3 text-right font-medium">Your list</th>
                <th className="px-4 py-3 text-right font-medium">Net</th>
                <th className="px-4 py-3 text-right font-medium">vs exchange</th>
                <th className="px-4 py-3 font-medium">Status</th>
                <th
                  className="px-4 py-3 font-medium"
                  title="How this game's tickets actually left your hands. The only input a sell-timing model could ever be fit against, and not recoverable once a game has been played."
                >
                  Outcome
                </th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100 dark:divide-slate-800">
              {rows.map((r) => {
                const past = phaseOf(r.g, now) === "past";
                return (
                  <tr key={r.g.gameId} className={past ? "opacity-40" : undefined}>
                    <td className="whitespace-nowrap px-4 py-2.5 tabular-nums">{r.g.date}</td>
                    <td className="whitespace-nowrap px-4 py-2.5">{r.g.opponent.name}</td>
                    <td className="px-4 py-2.5">
                      <span
                        className={`inline-flex rounded px-1.5 py-0.5 text-xs font-semibold ring-1 ring-inset ${
                          TIER_COLOR[r.g.tier ?? "D"]
                        }`}
                      >
                        {r.g.tier}
                      </span>
                    </td>
                    <td className="px-4 py-2.5 text-right tabular-nums text-slate-600 dark:text-slate-400">
                      {fmt(r.credit)}
                    </td>
                    <td
                      className="px-4 py-2.5 text-right font-medium tabular-nums"
                      title={
                        r.floor == null
                          ? "Enter this tier's credit to compute break-even"
                          : `Minimum allowed list price ${fmt(r.floor)} ` +
                            `(${Math.round(MIN_LIST_RATIO_OF_FACE * 100)}% of face; published rule). ` +
                            `You cannot mark down below it, so below that the outcome is $0 rather ` +
                            `than a cheaper sale.` +
                            (r.hasFloor
                              ? ""
                              : " NOTE: credit from this game expires at its own puck drop with no " +
                                "remaining games to spend it on, so there is no exchange floor here.")
                      }
                    >
                      {fmt(r.be)}
                      {!r.hasFloor && (
                        <span className="ml-1 text-red-600 dark:text-red-400" aria-hidden>
                          !
                        </span>
                      )}
                    </td>
                    <td className="px-4 py-2.5 text-right tabular-nums text-slate-600 dark:text-slate-400">
                      {r.mk == null ? (
                        <span className="text-slate-400">--</span>
                      ) : (
                        <span
                          title={
                            `${r.mk.observations} observation(s), latest ${r.mk.observedDate}. ` +
                            Object.entries(r.mk.otherSources ?? {})
                              .map(([n, q]) => `${n} low $${q.low}. `)
                              .join("") +
                            (r.be != null ? standingNote(standing(r.be, r.mk)) : "")
                          }
                        >
                          ${r.mk.low}
                          {r.mk.high != null && (
                            <span className="opacity-50">&ndash;{r.mk.high}</span>
                          )}
                          {r.mk.lowDelta != null && r.mk.lowDelta !== 0 && (
                            <span
                              className={
                                r.mk.lowDelta > 0
                                  ? "ml-1 text-xs text-red-600 dark:text-red-400"
                                  : "ml-1 text-xs text-emerald-600 dark:text-emerald-400"
                              }
                            >
                              {r.mk.lowDelta > 0 ? "\u2191" : "\u2193"}
                              {Math.abs(r.mk.lowDelta)}
                            </span>
                          )}
                        </span>
                      )}
                    </td>
                    <td className="px-4 py-2.5 text-right">
                      <input
                        type="number"
                        step="1"
                        value={r.list ?? ""}
                        placeholder="--"
                        onChange={(e) => setList(r.g.gameId, e.target.value)}
                        className={`w-20 rounded border bg-transparent px-2 py-1 text-right text-sm tabular-nums ${
                          r.list != null && r.floor != null && r.list < r.floor
                            ? "border-red-500 text-red-600 dark:text-red-400"
                            : "border-slate-300 dark:border-slate-700"
                        }`}
                        title={
                          r.list != null && r.floor != null && r.list < r.floor
                            ? `Below the ${fmt(r.floor)} minimum allowed list price - this listing ` +
                              `would be rejected`
                            : undefined
                        }
                      />
                    </td>
                    <td className="px-4 py-2.5 text-right tabular-nums">{fmt(r.net)}</td>
                    <td
                      className={`px-4 py-2.5 text-right font-medium tabular-nums ${
                        r.delta == null
                          ? "text-slate-400"
                          : r.delta > 0
                            ? "text-emerald-600 dark:text-emerald-400"
                            : "text-red-600 dark:text-red-400"
                      }`}
                    >
                      {r.delta == null ? "--" : `${r.delta > 0 ? "+" : ""}${r.delta.toFixed(2)}`}
                    </td>
                    <td className="px-4 py-2.5 text-xs text-slate-500">{r.guide.headline}</td>
                    <td className="px-4 py-2.5">
                      <select
                        value={r.outcome?.kind ?? ""}
                        onChange={(e) => setOutcome(r.g.gameId, e.target.value)}
                        className={`rounded border bg-transparent px-1.5 py-1 text-xs ${
                          r.outcome
                            ? "border-slate-300 dark:border-slate-700"
                            : past
                              ? "border-red-400 text-red-600 dark:border-red-700 dark:text-red-400"
                              : "border-slate-200 text-slate-400 dark:border-slate-800"
                        }`}
                        title={
                          r.outcome
                            ? `Recorded ${r.outcome.on}` +
                              (r.outcome.atList != null ? ` at $${r.outcome.atList}` : "")
                            : past
                              ? "This game has been played and nothing was recorded - a permanently lost observation"
                              : "Record what happened once it resolves"
                        }
                      >
                        <option value="">{past ? "not recorded" : "--"}</option>
                        {(Object.keys(OUTCOME_LABEL) as OutcomeKind[]).map((k) => (
                          <option key={k} value={k}>
                            {OUTCOME_LABEL[k]}
                          </option>
                        ))}
                      </select>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </section>

        <SeasonPnlPanel profile={profile} setProfile={setProfile} games={GAMES} />

        <SellerObservations
          profile={profile}
          setProfile={setProfile}
          games={GAMES}
          configuredFeeRate={FEE}
        />

        <section className="mt-8 rounded-lg border border-slate-200 bg-white p-5 dark:border-slate-800 dark:bg-slate-900">
          <h2 className="text-sm font-semibold uppercase tracking-wide text-slate-600 dark:text-slate-400">
            Recorded outcomes
          </h2>
          <p className="mt-1 text-xs text-slate-500">
            {counts.total} of {GAMES.length} games recorded. These are the only input a
            sell-timing model could ever be fit against, and they cannot be recovered after a
            game is played &mdash; there are 44 a season.
          </p>

          {counts.missed.length > 0 && (
            <p className="mt-3 rounded border border-red-300 bg-red-50 p-2 text-xs text-red-800 dark:border-red-800/50 dark:bg-red-950/30 dark:text-red-300">
              <strong>
                {counts.missed.length} played game{counts.missed.length === 1 ? "" : "s"} with
                nothing recorded
              </strong>{" "}
              &mdash; {counts.missed.slice(0, 4).map((g) => `${g.date} ${g.opponent.abbrev}`).join(", ")}
              {counts.missed.length > 4 && ` +${counts.missed.length - 4} more`}. Each is an
              observation that is gone.
            </p>
          )}

          <dl className="mt-3 grid grid-cols-2 gap-3 text-sm sm:grid-cols-4">
            {(Object.keys(OUTCOME_LABEL) as OutcomeKind[]).map((k) => (
              <div key={k} className="rounded border border-slate-200 p-2 dark:border-slate-800">
                <dt className="text-xs text-slate-500">{OUTCOME_LABEL[k]}</dt>
                <dd className="mt-0.5 text-lg font-semibold tabular-nums">{counts.byKind[k]}</dd>
              </div>
            ))}
          </dl>

          <p className="mt-3 text-xs text-slate-500">
            {rate.rate == null ? (
              <>
                No game has reached a market conclusion yet, so there is no sell rate to report.
              </>
            ) : (
              <>
                <strong>
                  {rate.sold} of {rate.concluded} sold ({Math.round(rate.rate * 100)}%)
                </strong>{" "}
                among games that reached a market conclusion.
                {rate.censored > 0 && (
                  <>
                    {" "}
                    {rate.censored} exchanged game{rate.censored === 1 ? " is" : "s are"} excluded
                    from that denominator rather than counted as failures &mdash; returning for
                    credit is a chosen exit taken before the deadline, so it is censored data, not
                    a ticket that failed to sell.
                  </>
                )}
              </>
            )}
          </p>
          <p className="mt-1 text-xs text-slate-500">
            {rate.trustworthy ? (
              <>Enough outcomes to attempt a timing fit (ops#8).</>
            ) : (
              <>
                <strong>Not a usable rate yet.</strong> {counts.needed} more outcome
                {counts.needed === 1 ? "" : "s"} before a probability-of-sale fit is worth
                attempting at all, and {FIT_THRESHOLD} is a floor rather than a target. Any curve
                drawn before then would look authoritative while being invented.
              </>
            )}
          </p>
        </section>

        <section className="mt-8 rounded-lg border border-slate-200 bg-white p-5 dark:border-slate-800 dark:bg-slate-900">
          <h2 className="text-sm font-semibold uppercase tracking-wide text-slate-600 dark:text-slate-400">
            Deadline reminders
          </h2>
          <p className="mt-1 text-xs text-slate-500">
            Subscribe once and every exchange deadline lands in your calendar with alarms at 7 days,
            24 hours, and 1 hour. A dashboard you forget to open cannot warn you, and after the
            deadline an unsold ticket is worth <strong>$0</strong> &mdash; the credit is gone, not
            reduced.
          </p>
          <p className="mt-3">
            <a
              href="deadlines.ics"
              className="inline-flex rounded border border-teal-600 px-3 py-1.5 text-sm font-medium text-teal-700 hover:bg-teal-50 dark:border-teal-500 dark:text-teal-300 dark:hover:bg-teal-950/40"
            >
              Add 44 deadlines to calendar
            </a>
          </p>
          <p className="mt-2 text-xs text-slate-400">
            The feed carries the game, the deadline and the whole-arena asking range &mdash; but not
            your credit or break-even. It is served from a public URL, so those stay in this app.
          </p>
        </section>

        <section className="mt-6 space-y-2 text-xs text-slate-500">
          <p>
            <strong className="text-slate-600 dark:text-slate-400">Arena ask</strong> is the cheapest
            and priciest listing in the whole building on TickPick, all-in &mdash;{" "}
            {market().observationDays === 1 ? (
              <>a single observation from {market().lastObservedDate}, so there is no trend yet.</>
            ) : (
              <>
                {market().observationDays} observation days, {market().firstObservedDate} to{" "}
                {market().lastObservedDate}. The arrow is the change in the cheapest ask since the
                first observation.
              </>
            )}
          </p>
          {market().crossSource && (
            <p>
              <strong>
                Cross-checked against {market().sources.filter((x) => x !== market().source).join(", ")}
              </strong>{" "}
              on {market().crossSource!.comparedGames} games. The secondary source sits at a
              median{" "}
              {((market().crossSource!.medianRatioToPrimary - 1) * 100).toFixed(1)}% above{" "}
              {market().source} (range{" "}
              {((market().crossSource!.minRatio - 1) * 100).toFixed(1)}&ndash;
              {((market().crossSource!.maxRatio - 1) * 100).toFixed(1)}%). They are kept side by
              side rather than averaged: two sources agreeing on the <em>order</em> of games while
              differing in <em>level</em> is expected, and a sudden move in that gap means a
              source changed rather than the market. Hover a row for the other source&rsquo;s low.
            </p>
          )}
          <p>
            Two things it is not. It is <strong>not a comp for your seats</strong> &mdash; the low is
            almost always an upper-deck single, and section-level listings sit behind a path
            TickPick&rsquo;s robots.txt disallows. And it is{" "}
            <strong>not the channel you sell on</strong> &mdash; Ticketmaster blocks collection, so
            this is a neighbouring market, not your achievable price.
          </p>
          <p>
            <strong className="text-slate-600 dark:text-slate-400">Two published rules the
            model now respects.</strong>{" "}
            A listing cannot be posted below{" "}
            {Math.round(MIN_LIST_RATIO_OF_FACE * 100)}% of face, so there is a hard bottom to any
            markdown &mdash; below it the outcome is $0, not a cheaper sale. And account credit
            expires at puck drop of the last home game, cannot roll into next season, and cannot
            buy playoff tickets &mdash; so the exchange is <em>not</em> a floor for the final home
            game, which is marked with a red{" "}
            <span className="text-red-600 dark:text-red-400">!</span> above.
          </p>
          <p>
            The sell-timing curve is still deliberately unbuilt. {market().observationDays} day
            {market().observationDays === 1 ? "" : "s"} of history cannot support a
            probability-of-sale estimate, and a fabricated one would look authoritative while being
            invented. The tool shows the tradeoff and leaves the call to you.
          </p>
        </section>
      </main>
    </div>
  );
}
