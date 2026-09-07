#!/usr/bin/env python3
"""Fetch the official 300dpi seating/price chart and render it to PNG. ops#66, ops#53, ops#19.

WHY THIS EXISTS. ops#53's measurements - CLUB 1 at 5.6%, PROMENADE ROW 1 CENTER at 7.2%
against 5.0% in its mirror - were taken from a chart that lived only inside one session's
context. `config/price_bands.json` recorded the consequence in
`_transcriptionStatus._blocker`: the chart is not in this repo. ops#66 then asked Wesley to
supply the file, on the reasonable assumption that only he could.

He could not have known, and neither could that issue: **the chart is public and
reproducible.** It is a raster inside a one-page PDF that the NHL serves without
authentication. So the durable artefact is not the file, it is this script plus a URL - and
a URL in a script is greppable forever, where the same URL in a chat message survived
exactly one session. That is the burial pattern CLAUDE.md's store rule is about, in its
purest form: the input to the project's most contested measurement existed as tribal
knowledge.

WHY THE OUTPUT IS NOT COMMITTED. The rendered PNG is ~8.9MB, and "Git never forgets" - a
committed blob is permanent whether or not the file is later deleted. Repository policy is
that raw material stays out and only small aggregates go in. Since this is deterministic
from a public URL, storing the recipe strictly dominates storing the result.

WHY IT GOES THROUGH CHROMIUM. There is no JPEG decoder on this machine - no PIL, no numpy,
no ImageMagick, no pdftoppm - and `scripts/lib/minipng.py` reads PNG only, deliberately.
`jpeg_to_png.mjs` borrows Chromium's decoder rather than adding a dependency, and refuses
rather than resample if the dimensions disagree.

VERIFY BEFORE USE, do not trust this script's success. `check_chart_asset.py` grades the
result on a colour census against the legend, because the failure that matters is a
LOSSILY RECOMPRESSED chart - still plenty of pixels, but the flat legend colours smeared
into thousands of near-misses, which would quietly poison any extraction tuned against it.
This script prints that command; it does not assume its own output is good.

Usage:
    python3 scripts/fetch_chart.py                 # -> raw-out/chart_hi.png
    python3 scripts/fetch_chart.py --out PATH
    python3 scripts/fetch_chart.py --self-test     # offline
"""

from __future__ import annotations

import argparse
import pathlib
import re
import subprocess
import sys
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent

# The 2026-27 Sharks365 full-season price chart, as a one-page PDF wrapping a 2550x3300
# JPEG. Supplied by Wesley 2026-09-06; re-verified reachable 2026-09-07 (HTTP 200,
# 2,207,413 bytes). Public - no login, no key, no account. If this 404s the chart has moved
# and the URL is what needs updating, which is exactly why it lives here and not in prose.
CHART_PDF_URL = (
    "https://media.d3.nhle.com/image/private/t_document/prd/iq7ksotmplsblik0rvqf.pdf"
)
EXPECTED_W, EXPECTED_H = 2550, 3300

# A read that hits its cap is an ERROR, never data. This project has produced the same
# silent-truncation bug three times - a probe that scored a working 1.4MB source as empty,
# and a 5MB cap that quietly clipped a 7.4MB sitemap and reported a coverage gap. So the
# cap is generous and exceeding it fails loudly instead of returning a short file.
MAX_PDF_BYTES = 20 * 1024 * 1024


def extract_jpeg(pdf: bytes) -> bytes:
    """The DCTDecode image stream out of a PDF. Raises if there is not exactly one.

    Parsed directly rather than with a PDF library because the structure needed here is
    one object, and adding a dependency to read one stream is a worse trade than forty
    lines of regex. `/DCTDecode` IS the JPEG - PDF stores it undecoded - so this is a
    byte-range extraction and not a conversion.
    """
    found = []
    for m in re.finditer(rb"<<((?:[^<>]|<<(?:[^<>]|<<[^>]*>>)*>>)*)>>\s*stream\r?\n", pdf):
        d = m.group(1)
        if b"DCTDecode" not in d or b"/Image" not in d:
            continue
        start = m.end()
        end = pdf.find(b"endstream", start)
        if end < 0:
            raise ValueError("found an image stream with no endstream marker")
        found.append(pdf[start:end].rstrip(b"\r\n"))
    if not found:
        raise ValueError("no DCTDecode image found - is this still the chart PDF?")
    if len(found) > 1:
        # Ambiguity is a change in the source document, not something to resolve by
        # guessing which image is the chart.
        raise ValueError(f"expected one embedded image, found {len(found)}")
    return found[0]


def declared_size(pdf: bytes) -> tuple[int, int] | None:
    w = re.search(rb"/Width\s+(\d+)", pdf)
    h = re.search(rb"/Height\s+(\d+)", pdf)
    return (int(w.group(1)), int(h.group(1))) if w and h else None


def fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "ticket-desk/chart-fetch"})
    with urllib.request.urlopen(req, timeout=60) as r:
        body = r.read(MAX_PDF_BYTES + 1)
    if len(body) > MAX_PDF_BYTES:
        raise ValueError(f"PDF exceeded {MAX_PDF_BYTES} bytes - refusing a truncated read")
    return body


def self_test() -> int:
    fails = []

    def check(label, got, want):
        if got != want:
            fails.append(f"{label}: got {got!r}, want {want!r}")

    def pdf_with(*dicts_and_payloads):
        out = b"%PDF-1.3\n"
        for d, payload in dicts_and_payloads:
            out += b"<<" + d + b">>\nstream\n" + payload + b"\nendstream\n"
        return out

    img = b"<</Type /XObject /Subtype /Image /Width 2550 /Height 3300 /Filter /DCTDecode"
    check("extracts the DCTDecode payload",
          extract_jpeg(pdf_with((img, b"\xff\xd8JPEGBYTES\xff\xd9"))),
          b"\xff\xd8JPEGBYTES\xff\xd9")
    check("reads the declared size",
          declared_size(pdf_with((img, b"x"))), (2550, 3300))

    # A FlateDecode stream alongside the image must be ignored, not returned. The real
    # chart PDF contains one (its colour space), and an earlier ad-hoc version of this
    # extraction returned whichever stream came first.
    flate = b"<</Filter /FlateDecode"
    check("ignores a non-image stream",
          extract_jpeg(pdf_with((flate, b"ZZZ"), (img, b"\xff\xd8OK\xff\xd9"))),
          b"\xff\xd8OK\xff\xd9")

    for label, doc, msg in (
        ("no image at all", pdf_with((flate, b"ZZZ")), "no DCTDecode"),
        ("two images is ambiguous",
         pdf_with((img, b"\xff\xd8A\xff\xd9"), (img, b"\xff\xd8B\xff\xd9")), "found 2"),
    ):
        try:
            extract_jpeg(doc)
            fails.append(f"{label}: expected a refusal, got none")
        except ValueError as e:
            if msg not in str(e):
                fails.append(f"{label}: wrong error {e!r}")

    # Truncation must be an error, never a short return.
    try:
        extract_jpeg(b"%PDF\n<<" + img + b">>\nstream\n\xff\xd8no-end-marker")
        fails.append("a stream with no endstream: expected a refusal, got none")
    except ValueError:
        pass

    print(f"self-test: {'passed' if not fails else 'FAILED'} ({len(fails)} failure(s))")
    for f in fails:
        print(f"  FAIL {f}")
    return 1 if fails else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=pathlib.Path, default=ROOT / "raw-out" / "chart_hi.png")
    ap.add_argument("--url", default=CHART_PDF_URL)
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()

    if args.self_test:
        return self_test()

    args.out.parent.mkdir(parents=True, exist_ok=True)
    pdf_path = args.out.with_suffix(".pdf")
    jpg_path = args.out.with_suffix(".jpg")

    print(f"fetching {args.url}")
    try:
        pdf = fetch(args.url)
    except Exception as e:
        print(f"FAILED to fetch the chart: {e}", file=sys.stderr)
        print("The chart is public and this URL is the only copy of that fact. If it has "
              "moved, update CHART_PDF_URL here rather than asking Wesley for the file.",
              file=sys.stderr)
        return 1
    pdf_path.write_bytes(pdf)
    print(f"  {len(pdf):,} bytes -> {pdf_path.relative_to(ROOT)}")

    try:
        jpg = extract_jpeg(pdf)
    except ValueError as e:
        print(f"FAILED: {e}", file=sys.stderr)
        return 1
    jpg_path.write_bytes(jpg)

    size = declared_size(pdf)
    print(f"  embedded JPEG {len(jpg):,} bytes, declared {size}")
    if size != (EXPECTED_W, EXPECTED_H):
        # Not fatal: a different size may be a BETTER chart. But it must be said, because
        # every measurement in ops#53 is quoted against 2550x3300.
        print(f"  NOTE: expected {EXPECTED_W}x{EXPECTED_H} - ops#53's percentages were "
              f"measured at that size and may not transfer", file=sys.stderr)

    w, h = size or (EXPECTED_W, EXPECTED_H)
    conv = subprocess.run(
        ["node", str(ROOT / "scripts" / "jpeg_to_png.mjs"), str(jpg_path), str(args.out),
         str(w), str(h)],
        capture_output=True, text=True)
    if conv.returncode != 0:
        print("FAILED to render PNG:", conv.stderr.strip()[:400], file=sys.stderr)
        return 1
    print(f"  {conv.stdout.strip()}")

    rel = args.out.relative_to(ROOT) if args.out.is_relative_to(ROOT) else args.out
    print(f"\nwrote {rel} ({args.out.stat().st_size:,} bytes)")
    print("NOT committed on purpose - ~9MB, and Git never forgets. It is deterministic "
          "from a public URL, so the recipe beats the result.")
    print(f"\nGRADE IT BEFORE EXTRACTING FROM IT:\n  npm run check:chart -- {rel}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
