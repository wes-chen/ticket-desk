import { useMemo, useState } from "react";
import { breakEvenList, fmt } from "../lib/economics";
import { TIERS, invoicePerSeat, seatCount, shareUrl, type Profile, type Tier } from "../lib/profile";

/**
 * Profile editor. Doubles as first-run onboarding.
 *
 * The reconciliation readout is the interesting part: it checks the tier credits the
 * user types against their real invoice, so hand-entered numbers get validated rather
 * than trusted.
 */
export default function Setup({
  profile,
  onChange,
  tierCounts,
  onDone,
  firstRun,
}: {
  profile: Profile;
  onChange: (p: Profile) => void;
  tierCounts: Record<string, number>;
  onDone: () => void;
  firstRun: boolean;
}) {
  const [copied, setCopied] = useState(false);

  const recon = useMemo(() => {
    const perSeat = invoicePerSeat(profile);
    let accounted = 0;
    const missing: string[] = [];
    for (const t of TIERS) {
      const n = tierCounts[t] ?? 0;
      const c = profile.credits[t];
      if (c == null) {
        if (n > 0) missing.push(`${t} (${n})`);
      } else {
        accounted += c * n;
      }
    }
    const residual = perSeat == null ? null : perSeat - accounted;
    return {
      perSeat,
      accounted,
      residual,
      pct: perSeat && residual != null ? residual / perSeat : null,
      complete: missing.length === 0 && perSeat != null,
      missing,
    };
  }, [profile, tierCounts]);

  const set = (patch: Partial<Profile>) => onChange({ ...profile, ...patch });

  return (
    <div className="mx-auto max-w-3xl px-6 py-10">
      {firstRun && (
        <div className="mb-8">
          <h1 className="text-2xl font-semibold tracking-tight">Set up your seats</h1>
          <p className="mt-2 text-sm text-slate-600 dark:text-slate-400">
            None of this is sent anywhere. It&rsquo;s stored in this browser only &mdash; the site itself
            ships empty. When you&rsquo;re done you can generate a link that carries it to your phone
            without it ever touching a server.
          </p>
        </div>
      )}

      <section className="mb-8 rounded-lg border border-slate-200 bg-white p-5 dark:border-slate-800 dark:bg-slate-900">
        <h2 className="text-sm font-semibold uppercase tracking-wide text-slate-500">Your seats</h2>
        <div className="mt-4 grid gap-4 sm:grid-cols-3">
          <Field label="Section">
            <input
              value={profile.seats.section}
              onChange={(e) => set({ seats: { ...profile.seats, section: e.target.value } })}
              placeholder="e.g. 214"
              className={inputCls}
            />
          </Field>
          <Field label="Row">
            <input
              value={profile.seats.row}
              onChange={(e) => set({ seats: { ...profile.seats, row: e.target.value } })}
              placeholder="e.g. 8"
              className={inputCls}
            />
          </Field>
          <Field label="Seats (comma separated)">
            <input
              value={profile.seats.seats.join(", ")}
              onChange={(e) =>
                set({
                  seats: {
                    ...profile.seats,
                    seats: e.target.value
                      .split(",")
                      .map((s) => s.trim())
                      .filter(Boolean),
                  },
                })
              }
              placeholder="e.g. 1, 2"
              className={inputCls}
            />
          </Field>
        </div>
        <div className="mt-4 max-w-xs">
          <Field label="Season invoice total (all seats)">
            <div className="flex items-center gap-1">
              <span className="text-slate-400">$</span>
              <input
                type="number"
                step="0.01"
                value={profile.invoiceTotal ?? ""}
                onChange={(e) => set({ invoiceTotal: e.target.value === "" ? null : Number(e.target.value) })}
                placeholder="e.g. 5000.00"
                className={inputCls}
              />
            </div>
          </Field>
          {profile.invoiceTotal != null && (
            <p className="mt-1 text-xs text-slate-500 tabular-nums">
              {fmt(invoicePerSeat(profile))} per seat across {seatCount(profile)} seat
              {seatCount(profile) === 1 ? "" : "s"}
            </p>
          )}
        </div>
      </section>

      <section className="mb-8 rounded-lg border border-slate-200 bg-white p-5 dark:border-slate-800 dark:bg-slate-900">
        <h2 className="text-sm font-semibold uppercase tracking-wide text-slate-500">
          Exchange credit per seat, by tier
        </h2>
        <p className="mt-1 text-xs text-slate-500">
          Read these off the &ldquo;Return For Credit&rdquo; review page. Don&rsquo;t submit.
        </p>
        <div className="mt-4 grid grid-cols-2 gap-4 sm:grid-cols-3 lg:grid-cols-6">
          {TIERS.map((t) => {
            const c = profile.credits[t];
            return (
              <label key={t} className="block">
                <span className="text-xs font-semibold text-slate-600 dark:text-slate-300">{t}</span>
                <span className="ml-1 text-xs text-slate-400">({tierCounts[t] ?? 0})</span>
                <div className="mt-1 flex items-center gap-1">
                  <span className="text-slate-400">$</span>
                  <input
                    type="number"
                    step="0.01"
                    value={c ?? ""}
                    placeholder="--"
                    onChange={(e) => {
                      const next = { ...profile.credits };
                      if (e.target.value === "") delete next[t as Tier];
                      else next[t as Tier] = Number(e.target.value);
                      set({ credits: next });
                    }}
                    className={inputCls}
                  />
                </div>
                <p className="mt-1 text-xs text-slate-500 tabular-nums">
                  {c == null ? "--" : `b/e ${fmt(breakEvenList(c))}`}
                </p>
              </label>
            );
          })}
        </div>

        <div className="mt-5 rounded border border-slate-200 bg-slate-50 p-4 text-sm dark:border-slate-800 dark:bg-slate-950/50">
          <p className="text-xs font-semibold uppercase tracking-wide text-slate-500">
            Invoice reconciliation
          </p>
          {!recon.complete ? (
            <p className="mt-2 text-slate-600 dark:text-slate-400">
              {recon.perSeat == null
                ? "Add your invoice total to cross-check these credits."
                : `Still missing: ${recon.missing.join(", ")}`}
            </p>
          ) : (
            <>
              <p className="mt-2 tabular-nums text-slate-700 dark:text-slate-300">
                {fmt(recon.accounted)} face + {fmt(recon.residual)} residual = {fmt(recon.perSeat)} per seat
              </p>
              <p className="mt-1 text-xs text-slate-500">
                {recon.residual! < -0.5
                  ? "Credits exceed the invoice, which shouldn't happen — a tier credit is probably mistyped."
                  : recon.pct! < 0.02
                    ? `Reconciles cleanly. Residual is ${(recon.pct! * 100).toFixed(2)}% — non-refundable fees. Credit tracks face, so break-even against the exchange is also break-even against what you paid.`
                    : `Residual is ${(recon.pct! * 100).toFixed(2)}% of your invoice — that's non-refundable fee the exchange can never return.`}
              </p>
            </>
          )}
        </div>
      </section>

      <section className="mb-8 rounded-lg border border-slate-200 bg-white p-5 dark:border-slate-800 dark:bg-slate-900">
        <h2 className="text-sm font-semibold uppercase tracking-wide text-slate-500">Send to another device</h2>
        <p className="mt-1 text-xs text-slate-500">
          The link below carries your data in the URL <em>fragment</em>. Fragments are never sent to the
          server &mdash; not to GitHub, not into any access log. Open it once on your phone and it saves
          there. Anyone holding the link can read it, so treat it like the data itself.
        </p>
        <button
          onClick={() => {
            navigator.clipboard?.writeText(shareUrl(profile));
            setCopied(true);
            setTimeout(() => setCopied(false), 2000);
          }}
          className="mt-3 rounded bg-teal-700 px-3 py-1.5 text-sm font-medium text-white hover:bg-teal-800"
        >
          {copied ? "Copied" : "Copy transfer link"}
        </button>
      </section>

      <button
        onClick={onDone}
        className="rounded border border-slate-300 px-4 py-2 text-sm font-medium hover:bg-slate-100 dark:border-slate-700 dark:hover:bg-slate-800"
      >
        {firstRun ? "Done" : "Back to games"}
      </button>
    </div>
  );
}

const inputCls =
  "w-full rounded border border-slate-300 bg-transparent px-2 py-1 text-sm tabular-nums dark:border-slate-700";

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="block">
      <span className="text-xs uppercase tracking-wide text-slate-500">{label}</span>
      <div className="mt-1">{children}</div>
    </label>
  );
}
