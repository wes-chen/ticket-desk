/**
 * The user's private data. None of this is ever committed to the repo or served
 * from GitHub Pages.
 *
 * Transfer between devices happens through a URL *fragment* (everything after `#`).
 * Fragments are never transmitted in an HTTP request - they don't reach the origin
 * server, don't appear in access logs, and aren't visible to the host. So a link
 * generated on the desktop and opened on a phone moves the data device-to-device
 * even though the app itself is served from a public URL.
 *
 * The encoding is obfuscation, not encryption. Anyone who obtains the link can read
 * it. Treat the link like the data itself.
 */

export type Tier = "A+" | "A" | "B" | "C" | "D" | "PRESEASON";

export const TIERS: Tier[] = ["A+", "A", "B", "C", "D", "PRESEASON"];

export interface Profile {
  v: 1;
  seats: {
    section: string;
    row: string;
    seats: string[];
  };
  /** Season invoice total across ALL seats, as billed. */
  invoiceTotal: number | null;
  /** Exchange credit per seat, by tier. Depends on seat location, hence private. */
  credits: Partial<Record<Tier, number>>;
  /** Per-game intended list price, keyed by NHL gameId. */
  listPrices: Record<string, number>;
}

export const EMPTY_PROFILE: Profile = {
  v: 1,
  seats: { section: "", row: "", seats: [] },
  invoiceTotal: null,
  credits: {},
  listPrices: {},
};

export function isConfigured(p: Profile): boolean {
  return p.seats.section.trim() !== "" && Object.keys(p.credits).length > 0;
}

export function seatCount(p: Profile): number {
  return Math.max(p.seats.seats.length, 1);
}

export function invoicePerSeat(p: Profile): number | null {
  if (p.invoiceTotal == null) return null;
  return p.invoiceTotal / seatCount(p);
}

// --- fragment transfer ---------------------------------------------------

function toBase64Url(s: string): string {
  const bytes = new TextEncoder().encode(s);
  let bin = "";
  for (const b of bytes) bin += String.fromCharCode(b);
  return btoa(bin).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function fromBase64Url(s: string): string {
  const b64 = s.replace(/-/g, "+").replace(/_/g, "/");
  const bin = atob(b64 + "=".repeat((4 - (b64.length % 4)) % 4));
  const bytes = Uint8Array.from(bin, (c) => c.charCodeAt(0));
  return new TextDecoder().decode(bytes);
}

export function encodeProfile(p: Profile): string {
  return toBase64Url(JSON.stringify(p));
}

export function decodeProfile(encoded: string): Profile | null {
  try {
    const parsed = JSON.parse(fromBase64Url(encoded)) as Profile;
    if (parsed?.v !== 1 || typeof parsed.seats?.section !== "string") return null;
    return { ...EMPTY_PROFILE, ...parsed };
  } catch {
    return null;
  }
}

export function shareUrl(p: Profile): string {
  const base = `${location.origin}${location.pathname}`;
  return `${base}#p=${encodeProfile(p)}`;
}

/**
 * Read a profile out of the current URL fragment, then scrub it from the address
 * bar so it isn't left sitting in history or accidentally bookmarked/shared.
 */
export function consumeProfileFromHash(): Profile | null {
  const m = location.hash.match(/[#&]p=([A-Za-z0-9_-]+)/);
  if (!m) return null;
  const p = decodeProfile(m[1]);
  if (p) history.replaceState(null, "", location.pathname + location.search);
  return p;
}
