/**
 * The deadline feed's two links, which are not the same thing (ops#64).
 *
 * The copy in the app promises a subscription - "Subscribe once and every exchange
 * deadline lands in your calendar" - and a plain relative `.ics` href does not deliver
 * one. On a phone that is a ONE-TIME IMPORT: the calendar copies today's events in and
 * never reads the URL again.
 *
 * That matters because the feed is regenerated on every build and the schedule genuinely
 * moves - `fetch_schedule.py` validates against the live NHL schedule precisely because
 * dates change. A stale import is worse than no import: it fires an alarm for a deadline
 * that has moved, or stays silent for one that has appeared. After the deadline an unsold
 * ticket is worth $0, so a silent miss is the expensive failure.
 *
 * ## Why the subscribe URL is absolute and hardcoded
 *
 * A calendar subscription must point at the PUBLISHED feed, not at whatever origin the
 * page happens to be served from. Subscribing from a `localhost` dev build should still
 * follow the real feed - `webcal://localhost:5173/...` would resolve to nothing on the
 * phone that opens it. So this is deliberately not derived from `window.location`.
 *
 * The download link stays RELATIVE, because that one should give you the build you are
 * actually looking at.
 *
 * `webcal://` is not universally handled, which is why both links are offered and
 * labelled differently rather than one being silently swapped for the other.
 */

/** Must match `APP_URL` in `scripts/make_calendar.py`, which publishes the feed. */
export const PUBLISHED_BASE = "https://wes-chen.github.io/ticket-desk/";

export const CALENDAR_FEED_FILE = "deadlines.ics";

/**
 * `webcal://` is the subscribe scheme. It is `https` with the scheme swapped - there is
 * no separate host or path - so deriving it by replacement keeps the two in step.
 */
export function subscribeUrl(base: string = PUBLISHED_BASE): string {
  return base.replace(/^https?:\/\//, "webcal://") + CALENDAR_FEED_FILE;
}

export const CALENDAR_SUBSCRIBE_URL = subscribeUrl();
