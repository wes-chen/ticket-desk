import { useEffect, useMemo, useState } from "react";
import economics from "../../config/economics.json";
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

  // The seats field is free text while being edited, and only *derived* into the
  // parsed array. Binding the input directly to seats.join(", ") ate any comma the
  // moment it was typed: "16," splits to ["16",""], the empty is filtered out, and
  // it rejoins to "16". Keep what was typed; parse alongside it.
  const [seatsText, setSeatsText] = useState(profile.seats.seats.join(", "));

  const parseSeats = (raw: string) =>
    raw
      .split(/[,\s]+/)
      .map((s) => s.trim())
      .filter(Boolean);

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
              value={seatsText}
              onChange={(e) => {
                setSeatsText(e.target.value);
                set({ seats: { ...profile.seats, seats: parseSeats(e.target.value) } });
              }}
              onBlur={() => setSeatsText(parseSeats(seatsText).join(", "))}
              placeholder="e.g. 1, 2"
              className={inputCls}
            />
            <p className="mt-1 text-xs text-slate-500">
              {profile.seats.seats.length === 0
                ? "no seats yet"
                : `${profile.seats.seats.length} seat${profile.seats.seats.length === 1 ? "" : "s"}: ${profile.seats.seats.join(", ")}`}
            </p>
          </Field>
        </div>
        <div className="mt-4 max-w-xs">
          <Field label="Season invoice total (all seats)">
            <MoneyInput
              value={profile.invoiceTotal}
              onValue={(n) => set({ invoiceTotal: n })}
              placeholder="e.g. 5000.00"
            />
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
                <div className="mt-1">
                  <MoneyInput
                    value={c ?? null}
                    placeholder="--"
                    onValue={(n) => {
                      const next = { ...profile.credits };
                      if (n == null) delete next[t as Tier];
                      else next[t as Tier] = n;
                      set({ credits: next });
                    }}
                  />
                </div>
                <p className="mt-1 text-xs text-slate-500 tabular-nums">
                  {c == null ? "--" : `b/e ${fmt(breakEvenList(c, undefined, economics.exchange.creditHaircut.value))}`}
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

/**
 * Money input that keeps the typed text as the source of truth.
 *
 * Binding a number input to `Number(e.target.value)` breaks decimal entry: typing
 * "51." coerces to 51, which re-renders as "51" and swallows the dot, so "51.50"
 * can never be entered. Same failure as the seats field ate commas. Hold the raw
 * string, emit the parsed number, and only normalize on blur.
 */
function MoneyInput({
  value,
  onValue,
  placeholder,
}: {
  value: number | null;
  onValue: (n: number | null) => void;
  placeholder?: string;
}) {
  const [text, setText] = useState(value == null ? "" : String(value));

  // Re-sync when the value changes from outside (e.g. a profile import).
  useEffect(() => {
    const parsed = text.trim() === "" ? null : Number(text);
    if (parsed !== value && !(Number.isNaN(parsed) && value == null)) {
      setText(value == null ? "" : String(value));
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [value]);

  return (
    <div className="flex items-center gap-1">
      <span className="text-slate-400">$</span>
      <input
        type="text"
        inputMode="decimal"
        value={text}
        placeholder={placeholder}
        onChange={(e) => {
          const raw = e.target.value;
          // Permit intermediate states like "", "51.", "." while typing.
          if (raw !== "" && !/^\d*\.?\d*$/.test(raw)) return;
          setText(raw);
          const n = raw === "" || raw === "." ? null : Number(raw);
          onValue(n == null || Number.isNaN(n) ? null : n);
        }}
        onBlur={() => setText(value == null ? "" : String(value))}
        className={inputCls}
      />
    </div>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="block">
      <span className="text-xs uppercase tracking-wide text-slate-500">{label}</span>
      <div className="mt-1">{children}</div>
    </label>
  );
}
