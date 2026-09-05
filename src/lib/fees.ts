/**
 * Fee calibration from observed (list, net) pairs. ops#9.
 *
 * The seller fee is the single most load-bearing constant in the model: break-even is
 * credit / (1 - rate), so an error here biases every recommendation in the same
 * direction. It is currently measured flat at 10% across two price points ($77 -> $69.30
 * and $70 -> $63.00), but ops#9's argument for calibrating rather than hardcoding is
 * about the future, not the present: "a hardcoded 0.9 that silently goes stale would
 * bias every recommendation with no signal that anything was wrong."
 *
 * So this derives the rate from observations and says plainly how much they support.
 *
 * Two structures a single rate cannot express, both of which ops#9 asks to detect:
 *
 *   1. A STEPPED or TIERED rate. Shows up as observations that disagree with each other
 *      beyond rounding. Detected as residual spread, not averaged away.
 *   2. A FIXED PER-TICKET COMPONENT on top of a percentage. Shows up as the implied rate
 *      drifting at low prices while holding at high ones. Detected by fitting
 *      net = list x (1 - rate) - fixed and checking whether `fixed` is materially
 *      non-zero, which a percentage-only model cannot see at all.
 */

export interface FeeObservation {
  list: number;
  net: number;
  on: string;
  note?: string;
}

/** Implied rate from one pair, assuming no fixed component. */
export function impliedRate(o: FeeObservation): number {
  return 1 - o.net / o.list;
}

export interface Calibration {
  /** Usable observations, after dropping nonsense. */
  n: number;
  /** Best-estimate percentage rate. */
  rate: number | null;
  /** Estimated fixed per-ticket fee in dollars. Near zero means percentage-only. */
  fixed: number | null;
  /** Largest absolute residual in dollars against the fitted model. */
  maxResidual: number;
  /** Spread of single-pair implied rates - the tiered/stepped signal. */
  rateSpread: number;
  /** Human-readable findings, worst first. Empty when the observations are consistent. */
  findings: string[];
  /** True when the fit is trustworthy enough to drive break-even. */
  usable: boolean;
}

/** A fixed component below this is indistinguishable from rounding. */
const FIXED_TOLERANCE = 0.25;
/** Single-pair implied rates spread wider than this suggest a stepped structure. */
const RATE_SPREAD_TOLERANCE = 0.005;
/** Residual above this means the model does not describe the observations. */
const RESIDUAL_TOLERANCE = 0.05;

/**
 * Fit `net = list x (1 - rate) - fixed` by ordinary least squares.
 *
 * With one observation the system is underdetermined, so `fixed` is pinned to 0 and the
 * rate is taken directly. Pretending to estimate two parameters from one point is
 * exactly the invented precision this project refuses.
 */
export function calibrate(observations: FeeObservation[]): Calibration {
  const findings: string[] = [];
  const obs = observations.filter(
    (o) => Number.isFinite(o.list) && Number.isFinite(o.net) && o.list > 0 && o.net >= 0,
  );
  const dropped = observations.length - obs.length;
  if (dropped > 0) findings.push(`${dropped} observation(s) ignored as non-numeric or non-positive`);

  if (obs.length === 0) {
    return { n: 0, rate: null, fixed: null, maxResidual: 0, rateSpread: 0, findings, usable: false };
  }

  const rates = obs.map(impliedRate);
  const rateSpread = Math.max(...rates) - Math.min(...rates);

  let rate: number;
  let fixed: number;

  const distinctLists = new Set(obs.map((o) => o.list)).size;
  if (obs.length === 1 || distinctLists === 1) {
    // All observations at the same price level. A fixed component is unidentifiable -
    // any (rate, fixed) pair on a line through that point fits equally well.
    rate = rates.reduce((a, b) => a + b, 0) / rates.length;
    fixed = 0;
    if (obs.length > 1) {
      findings.push(
        `all ${obs.length} observations are at $${obs[0].list} - a fixed per-ticket fee ` +
          `cannot be separated from the percentage until a different price level is recorded`,
      );
    }
  } else {
    const n = obs.length;
    const sx = obs.reduce((a, o) => a + o.list, 0);
    const sy = obs.reduce((a, o) => a + o.net, 0);
    const sxx = obs.reduce((a, o) => a + o.list * o.list, 0);
    const sxy = obs.reduce((a, o) => a + o.list * o.net, 0);
    const denom = n * sxx - sx * sx;
    const slope = (n * sxy - sx * sy) / denom;
    const intercept = (sy - slope * sx) / n;
    rate = 1 - slope;
    fixed = -intercept;
  }

  const maxResidual = Math.max(
    ...obs.map((o) => Math.abs(o.net - (o.list * (1 - rate) - fixed))),
  );

  if (Math.abs(fixed) > FIXED_TOLERANCE) {
    findings.push(
      `a fixed per-ticket component of about $${fixed.toFixed(2)} is implied on top of ` +
        `${(rate * 100).toFixed(2)}% - break-even is not simply credit / (1 - rate)`,
    );
  }
  if (rateSpread > RATE_SPREAD_TOLERANCE && Math.abs(fixed) <= FIXED_TOLERANCE) {
    findings.push(
      `single-pair implied rates span ${(rateSpread * 100).toFixed(2)} points ` +
        `(${(Math.min(...rates) * 100).toFixed(2)}%–${(Math.max(...rates) * 100).toFixed(2)}%), ` +
        `which a flat percentage does not explain - suspect a stepped or tiered fee`,
    );
  }
  if (maxResidual > RESIDUAL_TOLERANCE) {
    findings.push(
      `worst observation is off the fitted model by $${maxResidual.toFixed(2)} - ` +
        `the observations are not all describing the same fee structure`,
    );
  }

  return {
    n: obs.length,
    rate,
    fixed,
    maxResidual,
    rateSpread,
    findings,
    // One observation is enough to derive a rate but not to trust it as a model: it
    // cannot distinguish a percentage from a percentage-plus-fixed-fee.
    usable: obs.length >= 2 && findings.length === 0,
  };
}

/** Net payout under a calibration that may include a fixed component. */
export function netUnder(cal: Calibration, list: number): number | null {
  if (cal.rate == null) return null;
  return list * (1 - cal.rate) - (cal.fixed ?? 0);
}

/** List price needed to net `target`, inverting a calibration with a fixed component. */
export function listToNetUnder(cal: Calibration, target: number): number | null {
  if (cal.rate == null) return null;
  return (target + (cal.fixed ?? 0)) / (1 - cal.rate);
}
