/**
 * Whole-profile backup and restore (ops#61).
 *
 * The profile lives in `localStorage`, which is per-browser, per-device and per-origin,
 * and one "clear site data" from gone. `exportPayload` already covered `outcomes` - but
 * `listPriceHistory`, the structure ops#48 added *specifically* so that changing a price
 * does not destroy the previous value, had no route out of the browser at all. The one
 * thing built to preserve history was the one thing with no backup.
 *
 * ## Why the raw profile is stored verbatim
 *
 * The file carries `profile` exactly as held in memory, and restore reads that key back
 * unchanged. Round-tripping a *reshaped* profile is how backfill markers and other
 * optional fields quietly disappear: every reshaping step is a chance to drop a key that
 * only some records carry. `listPriceHistory[].backfilled` is exactly such a key.
 *
 * `outcomesView` beside it is a DERIVED convenience for ops#8 - outcomes joined to their
 * game date, opponent and tier, which an outcome without its game is not a training
 * example without. It is **ignored on import**, deliberately: two writable copies of the
 * same fact is how they diverge. Edit the app, not the file.
 *
 * ## Why import refuses rather than merges
 *
 * A half-applied profile is worse than a rejected one - the same reasoning that makes
 * `primary_store.py` write nothing when any row fails. Worse here, because the transfer
 * link already has a wholesale-overwrite failure mode that *looks like* a successful
 * sync (ops#61 point 2). So restore validates the entire file first and returns either a
 * complete profile or an error, and never a partial one.
 *
 * A file with no schema marker is refused outright. Loading an arbitrary JSON object as
 * a profile would half-work, which is the worst available outcome.
 */
import { EMPTY_PROFILE, type Profile } from "./profile.ts";

export const BACKUP_SCHEMA = "ticket-desk/profile-backup";
export const BACKUP_VERSION = 1;

export interface BackupFile {
  _what: string;
  _schema: string;
  _v: number;
  exportedAt: string;
  season: string;
  profile: Profile;
  outcomesView: unknown[];
}

export type RestoreResult =
  | { ok: true; profile: Profile }
  | { ok: false; error: string };

/**
 * Build the backup file. `outcomesView` is supplied by the caller rather than computed
 * here, so this module does not need the schedule and stays trivially testable.
 */
export function buildBackup(
  profile: Profile,
  outcomesView: unknown[],
  now: Date = new Date(),
): BackupFile {
  return {
    _what:
      "Full Ticket Desk profile: seats, credits, list prices, price history and outcomes. " +
      "PRIVATE - contains our seats and our listing prices. Never commit to the public repo.",
    _schema: BACKUP_SCHEMA,
    _v: BACKUP_VERSION,
    exportedAt: now.toISOString(),
    season: "2026-27",
    profile,
    outcomesView,
  };
}

function isObj(x: unknown): x is Record<string, unknown> {
  return typeof x === "object" && x !== null && !Array.isArray(x);
}

/**
 * Parse and validate a backup file. Returns a complete profile or an error - never a
 * partially applied one.
 *
 * Tolerates an older export with no `listPriceHistory`: ops#48 made that field optional
 * and a profile without it is valid, so refusing one would reject the very backups this
 * feature exists to be able to read.
 */
export function restoreBackup(text: string): RestoreResult {
  let raw: unknown;
  try {
    raw = JSON.parse(text);
  } catch {
    return { ok: false, error: "That file is not valid JSON." };
  }
  if (!isObj(raw)) {
    return { ok: false, error: "That file is not a Ticket Desk backup." };
  }
  if (raw._schema !== BACKUP_SCHEMA) {
    return {
      ok: false,
      error:
        "That file is not a Ticket Desk profile backup, so nothing was changed. " +
        "Export a fresh one from the device that has your prices.",
    };
  }
  const p = raw.profile;
  if (!isObj(p)) {
    return { ok: false, error: "The backup has no profile in it. Nothing was changed." };
  }
  if (p.v !== 1) {
    return {
      ok: false,
      error: `That backup is version ${String(p.v)}; this app reads version 1. Nothing was changed.`,
    };
  }
  if (!isObj(p.seats) || !isObj(p.credits) || !isObj(p.listPrices)) {
    return {
      ok: false,
      error: "That backup is missing seats, credits or list prices. Nothing was changed.",
    };
  }
  // Optional structures must be the right SHAPE when present. Absent is fine; present and
  // wrong is a corrupted file, and silently dropping it would lose exactly the history
  // this feature exists to protect.
  for (const k of ["listPriceHistory", "outcomes", "instantOffers"] as const) {
    if (k in p && p[k] !== undefined && !isObj(p[k])) {
      return { ok: false, error: `The backup's ${k} is malformed. Nothing was changed.` };
    }
  }
  if ("feeObservations" in p && p.feeObservations !== undefined
      && !Array.isArray(p.feeObservations)) {
    return { ok: false, error: "The backup's feeObservations is malformed. Nothing was changed." };
  }

  // Defaults for absent optional fields, so the restored object is a complete Profile.
  // Spread order matters: the file wins over the defaults for every key it carries.
  const profile = {
    ...EMPTY_PROFILE,
    ...(p as unknown as Profile),
  } as Profile;
  return { ok: true, profile };
}

/** A stable filename, so successive backups sort by date in a downloads folder. */
export function backupFilename(now: Date = new Date()): string {
  return `ticket-desk-profile-${now.toISOString().slice(0, 10)}.json`;
}
