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
            body = r.read(400_000).decode("utf-8", errors="ignore")
            status = r.status
            final = r.url
    except urllib.error.HTTPError as e:
        body = e.read(400_000).decode("utf-8", errors="ignore") if e.fp else ""
        status = e.code
        final = url
    except Exception as e:  # noqa: BLE001 - the failure mode itself is the datum
        return {"status": None, "error": f"{type(e).__name__}: {e}", "ms": int((time.time() - t0) * 1000)}

    low = body.lower()
    blocks = sorted({label for sig, label in BLOCK_SIGNS if sig in low})
    content = sum(1 for s in CONTENT_SIGNS if s in low)

    return {
        "status": status,
        "ms": int((time.time() - t0) * 1000),
        "bytes": len(body),
        "redirected": final != url,
        "blockSignals": blocks,
        "contentSignals": content,
        "verdict": verdict(status, blocks, content, len(body)),
    }


def verdict(status, blocks, content, size) -> str:
    if status is None:
        return "unreachable"
    if blocks:
        return "BLOCKED"
    if status == 403:
        return "BLOCKED (403)"
    if status == 404:
        return "not found (wrong URL, not a block)"
    if status >= 500:
        return "server error"
    if status == 200 and size > 20_000 and content >= 3:
        return "OK - real content"
    if status == 200:
        return "200 but thin - possibly a shell or soft block"
    return f"http {status}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", default="local", help="where this ran, e.g. local / actions")
    ap.add_argument("--json", action="store_true", help="emit machine-readable output")
    args = ap.parse_args()

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
                detail += f" {r['bytes']:,}B contentSignals={r['contentSignals']}"
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
