/**
 * Reachability probe using a real browser.
 *
 * The urllib version of this could not answer the question it was built for: these
 * sites are JavaScript SPAs, so "thin response" was indistinguishable from "blocked",
 * and matching on the string "captcha" false-positived on ordinary reCAPTCHA script
 * tags. This version runs the actual tool the collector would use and, more
 * importantly, saves a screenshot of every page so the verdict can be checked by
 * eye instead of trusted from a heuristic.
 *
 * One page load per site, logged out, with a delay between. This is a probe, not a
 * crawl, and it never touches an authenticated session - the account holds the
 * season tickets.
 *
 * Usage: node scripts/probe_browser.mjs [--label local|actions] [--out DIR]
 */

import { chromium } from "playwright";
import { mkdirSync, writeFileSync, readFileSync, existsSync } from "node:fs";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..");
const args = process.argv.slice(2);
const label = args[args.indexOf("--label") + 1] ?? "local";
const outDir = args.includes("--out") ? args[args.indexOf("--out") + 1] : join(ROOT, "probe-out");

let tmEventId = null;
const schedPath = join(ROOT, "data", "schedule.json");
if (existsSync(schedPath)) {
  const sched = JSON.parse(readFileSync(schedPath, "utf8"));
  tmEventId = sched.games.find((g) => g.tmEventId)?.tmEventId ?? null;
}

const TARGETS = [
  tmEventId && {
    platform: "ticketmaster",
    url: `https://www.ticketmaster.com/event/${tmEventId}`,
    note: "the channel actually sold on",
  },
  {
    platform: "seatgeek",
    url: "https://seatgeek.com/san-jose-sharks-tickets",
    note: "historically most scrape-tolerant",
  },
  {
    platform: "tickpick",
    url: "https://www.tickpick.com/nhl/san-jose-sharks-tickets/",
    note: "all-in pricing",
  },
  {
    platform: "gametime",
    // Correct performer URL, discovered from gametime.co/sitemap/sport-performers.xml; the two URLs used before this were both wrong, which is why ops#4 never got a verdict.
    url: "https://gametime.co/san-jose-sharks-tickets/performers/nhlsjs",
    note: "last-minute skew",
  },
  {
    platform: "stubhub",
    url: "https://www.stubhub.com/san-jose-sharks-tickets",
    note: "large secondary volume",
  },
].filter(Boolean);

// Specific enough to mean something. A reCAPTCHA <script> tag is not a block; a
// visible challenge widget or an interstitial title is.
const CHALLENGE_TITLES = [
  "just a moment",
  "access denied",
  "attention required",
  "pardon our interruption",
  "are you a robot",
  "security check",
];

async function probeOne(browser, t) {
  const ctx = await browser.newContext({
    viewport: { width: 1280, height: 900 },
    locale: "en-US",
    timezoneId: "America/Los_Angeles",
  });
  const page = await ctx.newPage();
  const started = Date.now();
  let status = null;

  try {
    const resp = await page.goto(t.url, { waitUntil: "domcontentloaded", timeout: 45000 });
    status = resp?.status() ?? null;
    // Give client-side rendering a chance; these are SPAs.
    await page.waitForTimeout(6000);

    const title = (await page.title()).toLowerCase();
    const bodyText = await page.evaluate(() => document.body?.innerText ?? "");

    // Count actual rendered dollar amounts - the thing we ultimately want to read.
    const prices = bodyText.match(/\$\s?\d{1,4}(?:\.\d{2})?/g) ?? [];
    const uniquePrices = [...new Set(prices)];

    // A visible challenge widget, not merely a script reference.
    const visibleChallenge = await page.evaluate(() => {
      const sel = ["#px-captcha", "iframe[src*='recaptcha/api2/bframe']", "#challenge-running", ".g-recaptcha"];
      return sel.some((s) => {
        const el = document.querySelector(s);
        if (!el) return false;
        const r = el.getBoundingClientRect();
        return r.width > 0 && r.height > 0;
      });
    });

    const titleChallenge = CHALLENGE_TITLES.some((c) => title.includes(c));
    const blocked = titleChallenge || visibleChallenge || status === 403;

    mkdirSync(outDir, { recursive: true });
    const shot = join(outDir, `${label}-${t.platform}.png`);
    await page.screenshot({ path: shot, fullPage: false });

    const verdict = blocked
      ? "BLOCKED"
      : status && status >= 400
        ? `http ${status}`
        : uniquePrices.length >= 5
          ? "OK - prices rendered"
          : "loaded, but no price grid visible";

    return {
      ...t,
      status,
      ms: Date.now() - started,
      title: (await page.title()).slice(0, 70),
      uniquePrices: uniquePrices.length,
      samplePrices: uniquePrices.slice(0, 6),
      titleChallenge,
      visibleChallenge,
      verdict,
      screenshot: shot,
    };
  } catch (e) {
    return { ...t, status, ms: Date.now() - started, verdict: "error", error: String(e).slice(0, 160) };
  } finally {
    await ctx.close();
  }
}

const browser = await chromium.launch({ headless: true });
const results = [];
for (const t of TARGETS) {
  const r = await probeOne(browser, t);
  results.push(r);
  const mark = r.verdict.startsWith("OK") ? "ok " : r.verdict === "BLOCKED" ? "!! " : "-- ";
  console.log(`${mark}${r.platform.padEnd(14)} ${r.verdict}`);
  console.log(`   status=${r.status} ${r.ms}ms prices=${r.uniquePrices ?? 0} title="${r.title ?? ""}"`);
  if (r.samplePrices?.length) console.log(`   sample: ${r.samplePrices.join(" ")}`);
  if (r.error) console.log(`   error: ${r.error}`);
  console.log();
  await new Promise((r) => setTimeout(r, 3000));
}
await browser.close();

mkdirSync(outDir, { recursive: true });
writeFileSync(join(outDir, `${label}-results.json`), JSON.stringify({ label, results }, null, 2));
const ok = results.filter((r) => r.verdict.startsWith("OK")).length;
console.log(`[${label}] ${ok}/${results.length} rendered a price grid  ->  ${outDir}`);
