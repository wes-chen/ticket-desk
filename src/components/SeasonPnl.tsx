/**
 * Season P&L and 1099-K groundwork. ops#13.
 *
 * Record-keeping, not tax advice, and the UI says so. The reason that caveat is
 * load-bearing rather than boilerplate: losses on personal-use property are generally
 * not deductible while gains are taxable, so the tax picture is ASYMMETRIC. A single
 * "net" figure would look authoritative and mislead in exactly the direction that costs
 * money. So cash, credit, and write-offs are shown as separate lines and never summed
 * into one number.
 */

import { fmt, type Game } from "../lib/economics";
import { grossCashProceeds, seasonPnl } from "../lib/pnl";
import type { Profile } from "../lib/profile";

export default function SeasonPnlPanel({
  profile,
  setProfile,
  games,
}: {
  profile: Profile;
  setProfile: (p: Profile) => void;
  games: Game[];
}) {
  const p = seasonPnl(profile, games);
  const gross = grossCashProceeds(p);

  const exchanged = p.lines.filter((l) => l.outcome?.kind === "exchanged");

  const markSpent = (gameId: number, spent: boolean) => {
    const key = String(gameId);
    const o = profile.outcomes?.[key];
    if (!o) return;
    setProfile({
      ...profile,
      outcomes: { ...profile.outcomes, [key]: { ...o, creditSpent: spent } },
    });
  };

  return (
    <section className="mt-8 rounded-lg border border-slate-200 bg-white p-5 dark:border-slate-800 dark:bg-slate-900">
      <h2 className="text-sm font-semibold uppercase tracking-wide text-slate-600 dark:text-slate-400">
        Season recovery
      </h2>
      <p className="mt-1 text-xs text-slate-500">
        Per-seat, from {p.resolved} of {games.length} games resolved. Because the exchange credit
        tracks face almost exactly, break-even against the exchange is also break-even against
        what you paid &mdash; so anything above basis on a game is real profit on that game.
      </p>

      <dl className="mt-4 grid grid-cols-2 gap-3 text-sm sm:grid-cols-3 lg:grid-cols-5">
        <Stat label="Cash received" value={fmt(p.cash)} tone="good" />
        <Stat
          label="Credit received"
          value={fmt(p.credit)}
          sub={p.credit > 0 ? `${fmt(p.creditSpent)} spent, ${fmt(p.creditUnspent)} not` : undefined}
          tone={p.creditUnspent > 0 ? "warn" : "good"}
        />
        <Stat label="Written off" value={fmt(p.writtenOff)} tone={p.writtenOff > 0 ? "bad" : "flat"} />
        <Stat label="Not yet resolved" value={fmt(p.unresolvedBasis)} tone="flat" />
        <Stat
          label="Paid, per seat"
          value={p.invoicePerSeat == null ? "--" : fmt(p.invoicePerSeat)}
          sub={p.invoicePerSeat == null ? "enter the invoice in Settings" : undefined}
          tone="flat"
        />
      </dl>

      <div className="mt-4 space-y-2 text-xs text-slate-500">
        {exchanged.length > 0 && (
          <div className="rounded border border-amber-300 bg-amber-50 p-2 text-amber-900 dark:border-amber-800/50 dark:bg-amber-950/30 dark:text-amber-200">
            {p.creditUnspent > 0 ? (
              <p>
                <strong>{fmt(p.creditUnspent)} of credit is unspent.</strong> Account credit only
                has value if it gets spent, so it is not counted as recovered cash above.
              </p>
            ) : (
              <p>All exchange credit has been marked spent.</p>
            )}
            <ul className="mt-2 space-y-1">
              {exchanged.map((l) => (
                <li key={l.game.gameId}>
                  <label className="flex items-center gap-2">
                    <input
                      type="checkbox"
                      checked={l.outcome?.creditSpent === true}
                      onChange={(e) => markSpent(l.game.gameId, e.target.checked)}
                    />
                    <span className="tabular-nums">{l.game.date}</span> {l.game.opponent.abbrev}{" "}
                    &mdash; {fmt(l.credit)} credit{" "}
                    {l.outcome?.creditSpent === true ? "(spent)" : "(not spent)"}
                  </label>
                </li>
              ))}
            </ul>
          </div>
        )}

        {p.nonRefundableResidual != null && (
          <p>
            <strong>{fmt(p.nonRefundableResidual)} per seat is non-refundable.</strong> That is the
            invoice ({fmt(p.invoicePerSeat)}) minus total face ({fmt(p.faceTotal)}) &mdash; the
            slice of the season the exchange structurally cannot return. It is reported once here
            and deliberately <em>not</em> allocated across games: nothing says it was incurred per
            game, and spreading it evenly would be an invention.
          </p>
        )}
        {p.faceTotal == null && (
          <p>
            Face value is unknown because {p.missingBasis} game
            {p.missingBasis === 1 ? " has" : "s have"} no tier credit entered, so the non-refundable
            residual cannot be computed. A partial sum would understate face and overstate the
            residual, so it is left blank rather than estimated.
          </p>
        )}

        <p className="rounded border border-slate-200 p-2 dark:border-slate-800">
          <strong>1099-K line: {fmt(gross)} per seat</strong>
          {p.seatCount > 1 && <> &middot; {fmt(gross * p.seatCount)} across {p.seatCount} seats</>}.
          Ticketmaster reports gross payouts, not profit, so this is cash before basis and{" "}
          <em>excludes</em> exchange credit, which is not a payout. Netting basis here would produce
          a figure that does not match the form.
        </p>

        <p>
          <strong>Record-keeping, not tax advice.</strong> The lines above are deliberately not
          summed into a single net. A loss on personal-use property is generally not deductible
          even though gains are taxable, so the tax picture is asymmetric and a single number would
          misrepresent it.
        </p>
      </div>
    </section>
  );
}

function Stat({
  label,
  value,
  sub,
  tone,
}: {
  label: string;
  value: string;
  sub?: string;
  tone: "good" | "bad" | "warn" | "flat";
}) {
  const color =
    tone === "good"
      ? "text-emerald-600 dark:text-emerald-400"
      : tone === "bad"
        ? "text-red-600 dark:text-red-400"
        : tone === "warn"
          ? "text-amber-600 dark:text-amber-400"
          : "text-slate-700 dark:text-slate-300";
  return (
    <div className="rounded border border-slate-200 p-2 dark:border-slate-800">
      <dt className="text-xs text-slate-500">{label}</dt>
      <dd className={`mt-0.5 text-lg font-semibold tabular-nums ${color}`}>{value}</dd>
      {sub && <dd className="mt-0.5 text-xs text-slate-400">{sub}</dd>}
    </div>
  );
}
