/**
 * Manual-paste data from the authenticated seller page. ops#9 and ops#12.
 *
 * Both live here because they come from the same screen in the same sitting. The
 * collector cannot reach either: Ticketmaster blocks a CI runner and a residential
 * browser alike, and these numbers are login-only regardless. CLAUDE.md rule 3 names
 * manual paste as the intended answer when a public page hides something - and rule 3
 * also means nothing here ever touches that session automatically.
 *
 * Everything entered here is personal and stays in localStorage.
 */

import { useMemo, useState } from "react";
import { fmt, type Game } from "../lib/economics";
import { calibrate } from "../lib/fees";
import { impliedBid, isRoundDollar, series, verdictAgainstCredit } from "../lib/offers";
import type { Profile, Tier } from "../lib/profile";

const CARD =
  "rounded-lg border border-slate-200 bg-white p-5 dark:border-slate-800 dark:bg-slate-900";
const INPUT =
  "w-24 rounded border border-slate-300 bg-transparent px-2 py-1 text-right text-sm tabular-nums dark:border-slate-700";
const BTN =
  "rounded border border-teal-600 px-3 py-1 text-sm font-medium text-teal-700 hover:bg-teal-50 disabled:opacity-40 dark:border-teal-500 dark:text-teal-300 dark:hover:bg-teal-950/40";

const today = () => new Date().toISOString().slice(0, 10);

export default function SellerObservations({
  profile,
  setProfile,
  games,
  configuredFeeRate,
}: {
  profile: Profile;
  setProfile: (p: Profile) => void;
  games: Game[];
  configuredFeeRate: number;
}) {
  return (
    <div className="mt-8 grid gap-6 lg:grid-cols-2">
      <FeePanel profile={profile} setProfile={setProfile} configuredFeeRate={configuredFeeRate} />
      <OfferPanel
        profile={profile}
        setProfile={setProfile}
        games={games}
        feeRate={configuredFeeRate}
      />
    </div>
  );
}

// ---------------------------------------------------------------- fee calibrator

function FeePanel({
  profile,
  setProfile,
  configuredFeeRate,
}: {
  profile: Profile;
  setProfile: (p: Profile) => void;
  configuredFeeRate: number;
}) {
  const [list, setList] = useState("");
  const [net, setNet] = useState("");
  const observations = profile.feeObservations ?? [];
  const cal = useMemo(() => calibrate(observations), [observations]);

  const add = () => {
    const l = Number(list);
    const n = Number(net);
    if (!Number.isFinite(l) || !Number.isFinite(n) || l <= 0) return;
    setProfile({
      ...profile,
      feeObservations: [...observations, { list: l, net: n, on: today() }],
    });
    setList("");
    setNet("");
  };

  const remove = (i: number) =>
    setProfile({ ...profile, feeObservations: observations.filter((_, j) => j !== i) });

  return (
    <section className={CARD}>
      <h2 className="text-sm font-semibold uppercase tracking-wide text-slate-600 dark:text-slate-400">
        Seller fee calibration
      </h2>
      <p className="mt-1 text-xs text-slate-500">
        Enter a list price and the &ldquo;You will make&rdquo; figure beside it. Break-even is
        credit / (1 &minus; rate), so this constant moves every recommendation &mdash; the point
        is to keep measuring it rather than trust a number that could go stale silently.
      </p>

      <div className="mt-3 flex flex-wrap items-end gap-2">
        <label className="text-xs text-slate-500">
          List
          <input
            type="number"
            step="0.01"
            value={list}
            onChange={(e) => setList(e.target.value)}
            className={`${INPUT} mt-1 block`}
            placeholder="70.00"
          />
        </label>
        <label className="text-xs text-slate-500">
          You will make
          <input
            type="number"
            step="0.01"
            value={net}
            onChange={(e) => setNet(e.target.value)}
            className={`${INPUT} mt-1 block`}
            placeholder="63.00"
          />
        </label>
        <button onClick={add} disabled={!list || !net} className={`${BTN} mb-0.5`}>
          Record
        </button>
      </div>

      {observations.length > 0 && (
        <table className="mt-4 w-full text-sm">
          <thead className="text-left text-xs uppercase tracking-wide text-slate-500">
            <tr>
              <th className="py-1 font-medium">Date</th>
              <th className="py-1 text-right font-medium">List</th>
              <th className="py-1 text-right font-medium">Net</th>
              <th className="py-1 text-right font-medium">Implied</th>
              <th />
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-100 dark:divide-slate-800">
            {observations.map((o, i) => (
              <tr key={`${o.on}-${i}`}>
                <td className="py-1.5 tabular-nums text-slate-500">{o.on}</td>
                <td className="py-1.5 text-right tabular-nums">{fmt(o.list)}</td>
                <td className="py-1.5 text-right tabular-nums">{fmt(o.net)}</td>
                <td className="py-1.5 text-right tabular-nums font-medium">
                  {((1 - o.net / o.list) * 100).toFixed(2)}%
                </td>
                <td className="py-1.5 text-right">
                  <button
                    onClick={() => remove(i)}
                    className="text-xs text-slate-400 hover:text-red-600"
                    aria-label="Remove observation"
                  >
                    &times;
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <div className="mt-4 text-xs">
        {cal.rate == null ? (
          <p className="text-slate-500">
            No observations yet. Break-even is using the configured{" "}
            {(configuredFeeRate * 100).toFixed(0)}%, measured at two price points.
          </p>
        ) : (
          <>
            <p className="text-slate-600 dark:text-slate-400">
              Fitted rate <strong>{(cal.rate * 100).toFixed(2)}%</strong>
              {cal.fixed != null && Math.abs(cal.fixed) > 0.005 && (
                <> plus a fixed <strong>{fmt(cal.fixed)}</strong> per ticket</>
              )}{" "}
              from {cal.n} observation{cal.n === 1 ? "" : "s"}.
              {cal.n > 1 && <> Worst residual {fmt(cal.maxResidual)}.</>}
            </p>
            {cal.findings.length > 0 ? (
              <ul className="mt-2 space-y-1">
                {cal.findings.map((f) => (
                  <li
                    key={f}
                    className="rounded border border-amber-300 bg-amber-50 p-2 text-amber-900 dark:border-amber-800/50 dark:bg-amber-950/30 dark:text-amber-200"
                  >
                    {f}
                  </li>
                ))}
              </ul>
            ) : (
              <p className="mt-1 text-slate-500">
                {cal.usable ? (
                  <>Consistent across {cal.n} observations, and driving break-even below.</>
                ) : (
                  <>
                    One observation derives a rate but cannot tell a percentage from a
                    percentage-plus-fixed-fee. Record a second at a{" "}
                    <strong>different price level</strong> to separate them &mdash; break-even is
                    still using the configured {(configuredFeeRate * 100).toFixed(0)}%.
                  </>
                )}
              </p>
            )}
          </>
        )}
      </div>
    </section>
  );
}

// ------------------------------------------------------------ instant offer log

function OfferPanel({
  profile,
  setProfile,
  games,
  feeRate,
}: {
  profile: Profile;
  setProfile: (p: Profile) => void;
  games: Game[];
  feeRate: number;
}) {
  const upcoming = useMemo(
    () => games.filter((g) => new Date(g.startTimeUTC) > new Date()),
    [games],
  );
  const [gameId, setGameId] = useState(() => String(upcoming[0]?.gameId ?? ""));
  const [amount, setAmount] = useState("");
  const all = profile.instantOffers ?? {};

  const add = () => {
    const v = Number(amount);
    if (!gameId || !Number.isFinite(v) || v <= 0) return;
    const existing = all[gameId] ?? [];
    setProfile({
      ...profile,
      instantOffers: { ...all, [gameId]: [...existing, { on: today(), offerPerTicket: v }] },
    });
    setAmount("");
  };

  const rows = useMemo(
    () =>
      games
        .map((g) => ({ g, s: series(g.gameId, all[String(g.gameId)]) }))
        .filter((r) => r.s.latest != null),
    [games, all],
  );

  return (
    <section className={CARD}>
      <h2 className="text-sm font-semibold uppercase tracking-wide text-slate-600 dark:text-slate-400">
        Instant offer log
      </h2>
      <p className="mt-1 text-xs text-slate-500">
        The &ldquo;Sell Your Tickets Now&rdquo; figure. It is a buyer&rsquo;s{" "}
        <strong>bid, not an ask</strong> &mdash; a floor on market value, not fair value &mdash;
        and it is the only price signal we have from the channel you actually sell on.
      </p>

      <div className="mt-3 flex flex-wrap items-end gap-2">
        <label className="text-xs text-slate-500">
          Game
          <select
            value={gameId}
            onChange={(e) => setGameId(e.target.value)}
            className="mt-1 block max-w-[15rem] rounded border border-slate-300 bg-transparent px-2 py-1 text-sm dark:border-slate-700"
          >
            {upcoming.map((g) => (
              <option key={g.gameId} value={g.gameId}>
                {g.date} {g.opponent.abbrev} ({g.tier})
              </option>
            ))}
          </select>
        </label>
        <label className="text-xs text-slate-500">
          Offer / ticket
          <input
            type="number"
            step="0.01"
            value={amount}
            onChange={(e) => setAmount(e.target.value)}
            className={`${INPUT} mt-1 block`}
            placeholder="24.30"
          />
        </label>
        <button onClick={add} disabled={!amount || !gameId} className={`${BTN} mb-0.5`}>
          Record
        </button>
      </div>

      {rows.length === 0 ? (
        <p className="mt-4 text-xs text-slate-500">
          Nothing recorded yet. Every sample so far has implied a bid at an exact round dollar,
          and all of them netted below the tier credit &mdash; if that holds, the instant offer is
          never the right exit while the exchange window is open.
        </p>
      ) : (
        <table className="mt-4 w-full text-sm">
          <thead className="text-left text-xs uppercase tracking-wide text-slate-500">
            <tr>
              <th className="py-1 font-medium">Game</th>
              <th className="py-1 text-right font-medium">Offer</th>
              <th className="py-1 text-right font-medium">Implied bid</th>
              <th className="py-1 text-right font-medium">vs credit</th>
              <th className="py-1 text-right font-medium">n</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-100 dark:divide-slate-800">
            {rows.map(({ g, s }) => {
              const offer = s.latest!.offerPerTicket;
              const bid = impliedBid(offer, feeRate);
              const credit = g.tier ? (profile.credits[g.tier as Tier] ?? null) : null;
              const v = verdictAgainstCredit(offer, credit);
              return (
                <tr key={g.gameId}>
                  <td className="py-1.5 whitespace-nowrap">
                    <span className="tabular-nums text-slate-500">{g.date}</span>{" "}
                    {g.opponent.abbrev}
                  </td>
                  <td className="py-1.5 text-right tabular-nums">
                    {fmt(offer)}
                    {s.delta != null && s.delta !== 0 && (
                      <span
                        className={
                          s.delta > 0
                            ? "ml-1 text-xs text-emerald-600 dark:text-emerald-400"
                            : "ml-1 text-xs text-red-600 dark:text-red-400"
                        }
                      >
                        {s.delta > 0 ? "↑" : "↓"}
                        {Math.abs(s.delta)}
                      </span>
                    )}
                  </td>
                  <td
                    className="py-1.5 text-right tabular-nums"
                    title={
                      isRoundDollar(bid)
                        ? "An exact round dollar, as all four measured samples were"
                        : "NOT a round dollar - either the fee rate has changed or this is not a plain bid"
                    }
                  >
                    {fmt(bid)}
                    {!isRoundDollar(bid) && <span className="ml-1 text-amber-600">?</span>}
                  </td>
                  <td
                    className={`py-1.5 text-right text-xs ${
                      v === "below_credit"
                        ? "text-red-600 dark:text-red-400"
                        : v === "above_credit"
                          ? "text-emerald-600 dark:text-emerald-400"
                          : "text-slate-400"
                    }`}
                  >
                    {v === "below_credit"
                      ? `below ${fmt(credit)}`
                      : v === "above_credit"
                        ? `above ${fmt(credit)}`
                        : "no credit set"}
                  </td>
                  <td className="py-1.5 text-right tabular-nums text-slate-500">
                    {s.offers.length}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
    </section>
  );
}
