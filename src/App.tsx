import { useEffect, useMemo, useState } from "react";
import scheduleData from "../data/schedule.json";
import economics from "../config/economics.json";
import Setup from "./components/Setup";
import {
  breakEvenList,
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

  const rows = useMemo(() => {
    return GAMES.map((g) => {
      const credit = g.tier ? profile.credits[g.tier as Tier] ?? null : null;
      const be = credit == null ? null : breakEvenList(credit, FEE);
      const list = profile.listPrices[String(g.gameId)] ?? null;
      const net = list == null ? null : netPayout(list, FEE);
      const delta = net == null || credit == null ? null : net - credit;
      return { g, credit, be, list, net, delta, guide: guidance(g, credit, now) };
    });
  }, [profile, now]);

  const deadlineSoon = rows
    .filter((r) => phaseOf(r.g, now) === "floor_active" && hoursUntil(exchangeDeadline(r.g), now) <= 72)
    .slice(0, 5);

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
              {Math.round(FEE * 100)}% seller fee
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
                <th className="px-4 py-3 text-right font-medium">Your list</th>
                <th className="px-4 py-3 text-right font-medium">Net</th>
                <th className="px-4 py-3 text-right font-medium">vs exchange</th>
                <th className="px-4 py-3 font-medium">Status</th>
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
                    <td className="px-4 py-2.5 text-right font-medium tabular-nums">{fmt(r.be)}</td>
                    <td className="px-4 py-2.5 text-right">
                      <input
                        type="number"
                        step="1"
                        value={r.list ?? ""}
                        placeholder="--"
                        onChange={(e) => setList(r.g.gameId, e.target.value)}
                        className="w-20 rounded border border-slate-300 bg-transparent px-2 py-1 text-right text-sm tabular-nums dark:border-slate-700"
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
                  </tr>
                );
              })}
            </tbody>
          </table>
        </section>

        <p className="mt-6 text-xs text-slate-500">
          Market comps and the sell-timing curve are not in this build. With no prior selling history,
          any probability-of-sale estimate would be invented rather than measured &mdash; so the tool
          shows the tradeoff and leaves the call to you until real data accumulates.
        </p>
      </main>
    </div>
  );
}
