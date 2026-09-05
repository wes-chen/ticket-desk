#!/usr/bin/env python3
"""Measure whether ticket marketplaces are reachable, and from where.

This is an experiment, not a scraper. It makes ONE polite logged-out request per
platform and reports what came back. Nothing is parsed for listings and nothing is
stored.

The point is the comparison: run it on a residential connection and again on a
GitHub Actions runner (datacenter IP). The delta between those two runs decides
whether the collector can live in CI for free or needs a residential proxy. That
question is worth answering with data before building four site adapters against it.

Never authenticated. Never uses a session cookie. The account holds the season
tickets; risking it to save a few requests would be a terrible trade.
"""

import argparse
import json
import pathlib
import re
import ssl
import sys
import time
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent

UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)

# Signatures that mean "you were identified as a bot", as distinct from an ordinary
# 404. Distinguishing these matters: a 404 is a wrong URL, a challenge is a wall.
BLOCK_SIGNS = [
    ("captcha", "captcha"),
    ("recaptcha", "recaptcha"),
    ("px-captcha", "perimeterx"),
    ("_incapsula_", "incapsula"),
    ("incapsula", "incapsula"),
    ("cf-browser-verification", "cloudflare challenge"),
    ("cf_chl", "cloudflare challenge"),
    ("just a moment", "cloudflare challenge"),
    ("access denied", "access denied"),
    ("are you a human", "bot wall"),
    ("unusual traffic", "rate limited"),
    ("pardon our interruption", "distil/imperva"),
]

# Weak positive signals that real content came back.
CONTENT_SIGNS = ["listing", "quantity", "section", "row ", "price", "ticket"]


def targets(sample_event: str | None) -> list[dict]:
    t = [
        {
            "platform": "seatgeek",
            "url": "https://seatgeek.com/san-jose-sharks-tickets",
            "note": "historically the most scrape-tolerant of the majors",
        },
        {
            "platform": "tickpick",
            "url": "https://www.tickpick.com/nhl/san-jose-sharks-tickets/",
            "note": "all-in pricing, so no buyer-fee reverse engineering needed",
        },
        {
            "platform": "gametime",
            "url": "https://gametime.co/nhl-hockey/san-jose-sharks-tickets",
            "note": "skews last-minute; useful signal for the timing curve",
        },
        {
            "platform": "stubhub",
            "url": "https://www.stubhub.com/san-jose-sharks-tickets",
            "note": "large secondary volume",
        },
    ]
    if sample_event:
        t.insert(
            0,
            {
                "platform": "ticketmaster",
                "url": f"https://www.ticketmaster.com/event/{sample_event}",
                "note": "the channel Wesley actually sells on; event id comes from the NHL API",
            },
        )
    return t


# What a real listing page contains that a challenge page never does. Counting subject
# matter is far more discriminating than the old generic word list, which scored block
# pages and listing pages almost identically.
MAX_BODY = 5_000_000

TEAM_TOKEN = "sharks"
VENUE_TOKEN = "sap center"
PRICE_RE = re.compile(r"\$\s?(\d{2,4})(?:\.\d\d)?\b")
TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.S | re.I)


def characterize(body: str) -> dict:
    low = body.lower()
    m = TITLE_RE.search(body)
    return {
        "title": (m.group(1).strip()[:80] if m else None),
        "distinctPrices": len(set(PRICE_RE.findall(body))),
        "teamMentions": low.count(TEAM_TOKEN),
        "venueMentions": low.count(VENUE_TOKEN),
    }


def probe(url: str, timeout: int = 25) -> dict:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        },
    )
    ctx = ssl.create_default_context()
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
            # 5MB, not the original 400KB. TickPick's listing page is 1.4MB and its
            # prices sit past the 400KB mark, so the old cap truncated away the very
            # evidence the verdict depends on and scored a working source as empty.
            # A cap is still needed - this is a probe, not a downloader - but it has to
            # be larger than a real listing page, not smaller.
            body = r.read(MAX_BODY).decode("utf-8", errors="ignore")
            status = r.status
            final = r.url
    except urllib.error.HTTPError as e:
        body = e.read(MAX_BODY).decode("utf-8", errors="ignore") if e.fp else ""
        status = e.code
        final = url
    except Exception as e:  # noqa: BLE001 - the failure mode itself is the datum
        return {"status": None, "error": f"{type(e).__name__}: {e}", "ms": int((time.time() - t0) * 1000)}

    low = body.lower()
    blocks = sorted({label for sig, label in BLOCK_SIGNS if sig in low})
    if body.strip().startswith('{"response":"identify"'):
        # Ticketmaster's device check. Distinct from its 403 IP block page, and the
        # distinction decides whether a residential browser could get through at all.
        blocks.append("tm-identify")

    chars = characterize(body)

    return {
        "status": status,
        "ms": int((time.time() - t0) * 1000),
        "bytes": len(body),
        "redirected": final != url,
        "blockSignals": blocks,
        **chars,
        "verdict": verdict(status, blocks, len(body),
                           chars["distinctPrices"], chars["teamMentions"], chars["venueMentions"]),
    }


def verdict(status, signals, size, prices=0, team=0, venue=0) -> str:
    """Classify one response.

    Rewritten 2026-09-05 after the first residential run got two of five wrong, both in
    the direction that would have changed a decision:

      * TickPick returned a 1.4MB page titled "Cheap San Jose Sharks Tickets 2026" with
        31 distinct dollar amounts and 1089 mentions of the team. The old rule needed 3
        hits from a generic word list, scored 2, and called it "thin - possibly a soft
        block". It is the one source measured to work.
      * StubHub was called BLOCKED because the body contained the string "recaptcha" -
        an ordinary script tag. ops#4 records the first probe generation making exactly
        this mistake; repeating it is the reason this function now looks at what a page
        CONTAINS rather than what words appear in it.

    So the positive test is subject-matter content - dollar amounts and mentions of the
    team and venue - which a challenge page never has, and the negative test is a small
    body, which real listing pages never are. Generic words like "price" and "ticket"
    appear in block pages too and are no longer load-bearing.

    Verdicts are deliberately distinct:
      BLOCKED      - refused. Nothing a better parser can recover.
      CHALLENGE    - a bot/device check, NOT an IP refusal. A real browser may pass it,
                     so plain HTTP cannot settle this one; it needs Chromium (ops#2).
                     Collapsing this into BLOCKED would have hidden the single most
                     important open question about Ticketmaster.
      INCONCLUSIVE - 200, but nothing that proves real content came back.
      OK           - real listing content, verified by subject matter.
    """
    if status is None:
        return "unreachable"
    if "tm-identify" in signals:
        return "CHALLENGE (bot check, not an IP block - needs a real browser)"
    if status == 404:
        return "not found (wrong URL, not a block)"
    if status and status >= 500:
        return "server error"

    # Real content beats every other signal: a page carrying prices and the team name is
    # not a block page, whatever scripts it happens to load.
    has_content = prices >= 5 and (team + venue) >= 10
    if has_content:
        return "OK - real content"

    if status == 403:
        return "BLOCKED (403)"
    if "ip-block" in signals:
        return "BLOCKED (IP block page)"
    if status == 200 and size < 20_000:
        return "BLOCKED (200 but stub-sized - soft block)"
    if status == 200:
        return "INCONCLUSIVE - 200 with no listing content found"
    if status == 401:
        return "CHALLENGE (401 - authentication or bot check)"
    return f"http {status}"


def self_test() -> int:
    """Replay MEASURED responses through verdict().

    tests/fixtures/probe_observations.json holds the characteristics of responses that
    were actually received, not invented ones. Two of these rows are the exact cases the
    previous logic misclassified, so a regression fails here instead of in a decision.
    """
    fx = json.loads((ROOT / "tests" / "fixtures" / "probe_observations.json").read_text())
    fails = []
    for o in fx["observations"]:
        got = verdict(o["status"], o["challengeSignals"], o["bytes"],
                      o["distinctPrices"], o["teamMentions"], o["venueMentions"])
        want = o["expect"]
        if not got.startswith(want):
            fails.append(f"{o['platform']} ({o['ranFrom']}): got {got!r}, expected {want}*\n      {o['label']}")

    # The two regressions that motivated the rewrite, asserted directly.
    tp = next(o for o in fx["observations"] if o["platform"] == "tickpick")
    if "thin" in verdict(tp["status"], tp["challengeSignals"], tp["bytes"],
                         tp["distinctPrices"], tp["teamMentions"], tp["venueMentions"]):
        fails.append("TickPick's 1.4MB listing page is being called thin again")
    if not verdict(200, ["recaptcha"], 359_311, 40, 500, 80).startswith("OK"):
        fails.append("a real page is still being failed for carrying a recaptcha script tag")

    for f in fails:
        print(f"  FAIL {f}", file=sys.stderr)
    print(f"self-test: {'FAILED' if fails else 'passed'} ({len(fails)} failure(s))")
    return 1 if fails else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", default="local", help="where this ran, e.g. local / actions")
    ap.add_argument("--json", action="store_true", help="emit machine-readable output")
    ap.add_argument("--self-test", action="store_true", help="replay measured observations, no network")
    args = ap.parse_args()

    if args.self_test:
        return self_test()

    sample = None
    sched = ROOT / "data" / "schedule.json"
    if sched.exists():
        games = json.loads(sched.read_text())["games"]
        sample = next((g.get("tmEventId") for g in games if g.get("tmEventId")), None)

    results = []
    for t in targets(sample):
        r = probe(t["url"])
        results.append({**t, **r})
        if not args.json:
            v = r["verdict"]
            mark = "ok " if v.startswith("OK") else ("!! " if "BLOCK" in v else "-- ")
            print(f"{mark}{t['platform']:<14} {v}")
            print(f"   {t['url']}")
            detail = f"   status={r.get('status')} {r.get('ms')}ms"
            if "bytes" in r:
                detail += (f" {r['bytes']:,}B prices={r.get('distinctPrices', 0)}"
                           f" team={r.get('teamMentions', 0)} venue={r.get('venueMentions', 0)}")
            if r.get("blockSignals"):
                detail += f" blocked_by={','.join(r['blockSignals'])}"
            if r.get("error"):
                detail += f" error={r['error']}"
            print(detail)
            print(f"   {t['note']}\n")
        time.sleep(2)  # be polite; this is a probe, not a crawl

    out = {"ranFrom": args.label, "results": results}
    if args.json:
        print(json.dumps(out, indent=2))
    else:
        ok = sum(1 for r in results if r["verdict"].startswith("OK"))
        print(f"[{args.label}] {ok}/{len(results)} returned real content")
    return 0


if __name__ == "__main__":
    sys.exit(main())
