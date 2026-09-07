#!/usr/bin/env python3
"""Grade a supplied seating-chart image before anyone tries to extract bands from it.

ops#53 asks for a better band-detection gate and quotes precise measurements taken from
"the 300dpi chart". That chart is **not on disk and not in git** - `price_bands.json`
already says so in `_transcriptionStatus._blocker`. The numbers in ops#53 came from an
image that existed only inside one session's context, so no later session can reproduce
them. This script exists so the next supply of that file is checked on arrival rather
than after an extraction quietly produces nonsense.

## What actually goes wrong, and why pixel dimensions do not catch it

The obvious check is resolution. It is close to useless here. The failure mode is not a
small image, it is a **lossily recompressed** one: someone screenshots the chart, or
saves it through a JPEG step, and the flat legend colours smear into thousands of
near-misses. The image still has plenty of pixels. `sector_bands()` then sees a cloud of
almost-right colours instead of a solid ring, and the share statistic ops#53 is trying to
replace gets even noisier - the tool would be tuned against a damaged input.

So the load-bearing test is a **colour census against the legend**: every `legendRgb` in
`config/price_bands.json` must appear as an EXACT pixel value, many times over. A clean
export of a vector chart has large flat runs of the exact colour. A recompressed one has
almost none, while still having neighbours within a few units - and that gap between
exact and near counts is the signature this script reports.

## The thresholds are chosen, not measured

`MIN_EXACT` and `EXACT_RATIO_FLOOR` below are judgement, because the original chart is
unavailable to calibrate against - which is the whole problem. They are set to catch the
failure that is obvious by an order of magnitude, not to draw a fine line. When a good
chart does arrive, record its real census in this docstring and tighten them.
"""
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent / "lib"))
import minipng  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
BANDS = ROOT / "config" / "price_bands.json"

# A legend colour appearing fewer times than this is not a band on the chart. A single
# section wedge at any usable resolution is thousands of pixels; 200 is far below that
# and is meant to separate "present" from "a few antialiased specks".
MIN_EXACT = 200

# Of all pixels close to a legend colour, at least this fraction must be EXACTLY it.
# A clean raster of flat vector fill is ~1.0 apart from wedge edges. Anything that has
# been through a lossy codec collapses well below this.
EXACT_RATIO_FLOOR = 0.5

# Chebyshev distance counted as "near" a legend colour when measuring the ratio above.
NEAR_TOL = 12


def palette(path=BANDS):
    d = json.loads(path.read_text())
    out = {}
    for b in d["bands"]:
        rgb = b.get("legendRgb")
        if rgb:
            out[b["id"]] = tuple(rgb)
    return out


def census(img, pal, near_tol=NEAR_TOL):
    """For each legend colour: (exact pixel count, count within near_tol)."""
    exact = {k: 0 for k in pal}
    near = {k: 0 for k in pal}
    items = list(pal.items())
    for y in range(img.h):
        base = y * img.w * img.nch
        for x in range(img.w):
            i = base + x * img.nch
            px = (img.buf[i], img.buf[i + 1], img.buf[i + 2])
            for k, c in items:
                d = max(abs(px[0] - c[0]), abs(px[1] - c[1]), abs(px[2] - c[2]))
                if d == 0:
                    exact[k] += 1
                    near[k] += 1
                    break
                if d <= near_tol:
                    near[k] += 1
                    break
    return exact, near


def grade(exact, near, pal):
    """Returns (fatal, warn). Fatal means do not extract from this file."""
    fatal, warn = [], []
    missing = [k for k in pal if exact[k] < MIN_EXACT]
    if missing:
        fatal.append(f"{len(missing)} of {len(pal)} legend colours absent or negligible "
                     f"(<{MIN_EXACT} exact px): {', '.join(sorted(missing)[:6])}"
                     + (" ..." if len(missing) > 6 else ""))
    smeared = []
    for k in pal:
        if near[k] >= MIN_EXACT:
            ratio = exact[k] / near[k]
            if ratio < EXACT_RATIO_FLOOR:
                smeared.append((k, ratio))
    if smeared:
        worst = sorted(smeared, key=lambda t: t[1])[:4]
        fatal.append(
            "image looks lossily recompressed - legend colours are present but smeared: "
            + ", ".join(f"{k} {r:.0%} exact" for k, r in worst)
            + f" (need >={EXACT_RATIO_FLOOR:.0%}). Supply the ORIGINAL export, not a "
              "screenshot or a JPEG round-trip.")
    thin = [k for k in pal if MIN_EXACT <= exact[k] < MIN_EXACT * 10]
    if thin:
        warn.append(f"{len(thin)} legend colour(s) present but thin "
                    f"(<{MIN_EXACT * 10} exact px) - extraction may be resolution-limited: "
                    + ", ".join(sorted(thin)[:6]))
    return fatal, warn


def main(argv):
    if len(argv) != 1:
        print("usage: check_chart_asset.py CHART.png", file=sys.stderr)
        return 2
    path = pathlib.Path(argv[0])
    if not path.exists():
        print(f"FAIL - no such file: {path}", file=sys.stderr)
        return 1
    pal = palette()
    if not pal:
        print("NOT CHECKED - config/price_bands.json carries no legendRgb values")
        return 0
    try:
        img = minipng.Img(str(path))
    except Exception as e:  # minipng asserts on anything it cannot handle
        print(f"FAIL - cannot decode as PNG: {e}", file=sys.stderr)
        print("  minipng handles 8-bit RGB/RGBA, non-interlaced. Re-export as PNG.",
              file=sys.stderr)
        return 1

    print(f"{path.name}: {img.w}x{img.h}, {img.nch} channels, {len(pal)} legend colours")
    exact, near = census(img, pal)
    fatal, warn = grade(exact, near, pal)

    for k in sorted(pal, key=lambda k: -exact[k]):
        print(f"  {k:<28} exact {exact[k]:>8}   near {near[k]:>8}")
    for w in warn:
        print(f"WARN {w}")
    if fatal:
        for f in fatal:
            print(f"FAIL {f}", file=sys.stderr)
        return 1
    print("OK - every legend colour present as flat exact-value pixels; usable for extraction")
    return 0


def _synthetic(path, pal, block, jitter=0):
    """Write an image with `block` px of each legend colour, optionally smeared.

    jitter>0 simulates lossy recompression: only every 4th pixel keeps the exact value,
    the rest are pushed a few units away - which is what a JPEG round-trip does to a
    flat fill.
    """
    colours = list(pal.values())
    w = block
    rows = []
    for idx, c in enumerate(colours):
        row = bytearray()
        for x in range(w):
            if jitter and x % 4:
                row += bytes(((c[0] + jitter) % 256, c[1], c[2]))
            else:
                row += bytes(c)
        rows.append(bytes(row))
    minipng.write_png(str(path), w, len(rows), rows)


def self_test():
    import tempfile
    fails = []

    def check(name, got, want):
        if got != want:
            fails.append(f"{name}: got {got!r}, want {want!r}")

    pal = {"a": (10, 20, 30), "b": (200, 100, 50)}
    tmp = pathlib.Path(tempfile.mkdtemp())

    # A clean chart: every legend colour present as a flat run.
    good = tmp / "good.png"
    _synthetic(good, pal, block=MIN_EXACT * 12)
    img = minipng.Img(str(good))
    e, n = census(img, pal)
    check("clean image counts exact pixels", e["a"], MIN_EXACT * 12)
    fatal, warn = grade(e, n, pal)
    check("clean image is not fatal", fatal, [])
    check("clean image is not thin", warn, [])

    # A lossily recompressed chart: same size, colours still 'nearly' there, but the
    # exact runs are gone. This is the case pixel dimensions cannot see.
    bad = tmp / "smeared.png"
    _synthetic(bad, pal, block=MIN_EXACT * 12, jitter=3)
    e2, n2 = census(minipng.Img(str(bad)), pal)
    check("smeared image still has near pixels", n2["a"], MIN_EXACT * 12)
    check("but few exact ones", e2["a"], MIN_EXACT * 3)
    fatal2, _ = grade(e2, n2, pal)
    check("smeared image is fatal", len(fatal2), 1)
    check("and says why", "recompressed" in fatal2[0], True)

    # A chart missing a legend colour entirely - wrong chart, or wrong season.
    e3 = {"a": MIN_EXACT * 12, "b": 0}
    n3 = {"a": MIN_EXACT * 12, "b": 0}
    fatal3, _ = grade(e3, n3, pal)
    check("absent colour is fatal", len(fatal3), 1)
    check("absent colour named", "b" in fatal3[0], True)

    # Present but thin warns rather than failing - a small chart is degraded, not wrong.
    e4 = {"a": MIN_EXACT * 12, "b": MIN_EXACT + 1}
    n4 = {"a": MIN_EXACT * 12, "b": MIN_EXACT + 1}
    fatal4, warn4 = grade(e4, n4, pal)
    check("thin colour is not fatal", fatal4, [])
    check("thin colour warns", len(warn4), 1)

    # A colour below MIN_EXACT but above zero must be treated as absent, not thin -
    # the boundary that decides whether extraction is attempted at all.
    e5 = {"a": MIN_EXACT * 12, "b": MIN_EXACT - 1}
    n5 = {"a": MIN_EXACT * 12, "b": MIN_EXACT - 1}
    check("just-below-floor is fatal", len(grade(e5, n5, pal)[0]), 1)

    # The real palette must load and be the full legend, or the check grades nothing.
    real = palette()
    check("real palette is the 23 legend bands", len(real), 23)

    for f in fails:
        print("FAIL", f)
    print(f"check_chart_asset self-test: {'PASS' if not fails else str(len(fails)) + ' FAILURES'}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(self_test() if "--self-test" in sys.argv[1:] else main(sys.argv[1:]))
