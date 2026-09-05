#!/usr/bin/env python3
"""Read section -> price band off the official Sharks365 chart, and REFUSE to emit a
map it cannot verify. See ops#19, ops#31.

WHY A TOOL RATHER THAN A HAND TRANSCRIPTION. ops#19 warns that reading the chart's
colour coding by eye is easy to get wrong - the same failure mode as the hand-copied
tier table. Sampling pixels removes the eye from the loop. What it does NOT remove is
the possibility of sampling the wrong pixels, so the tool scores itself.

THE SELF-GATE, which is the point of this file. The arena is mirror-symmetric about the
rink's LONG axis, so sections that mirror each other must carry identical band stacks.
The tool computes that agreement and writes nothing unless it clears --min-agreement.
A 75%-correct section map committed as fact is worse than no map: it would silently
misprice comps forever, and this repo has shipped enough confident wrong answers.

WHICH AXIS, and why it matters. The mirror is the LONG axis (101<->115), not left-right.
The chart labels one end "SHARKS ATTACK TWICE" and prices it differently: that end
carries UPPER ATTACK 1/2, the other UPPER GOAL 1/2. Scoring against a left-right mirror
gives 0/10; against the long axis, 18/24. An earlier version of check_price_bands.py
assumed left-right and would have rejected correct data.

CURRENT STATUS: on the 1020x1320 chart Wesley supplied, agreement is 18/24 (75%). The
residue is thin bands - GLASS, TEAL, PROMENADE ROW 1 - that one side of a mirror pair
detects and the other misses, because at that resolution a single-row band is ~4px.
That is a RESOLUTION limit, not a method limit, which is why ops#31 asks for a
higher-resolution chart rather than a different technique.

Usage:
    python3 scripts/extract_price_bands.py --image CHART.png [--out FILE]
    python3 scripts/extract_price_bands.py --image CHART.png --min-agreement 0.95
    python3 scripts/extract_price_bands.py --self-test
"""

import argparse
import json
import math
import pathlib
import sys
from collections import Counter, deque

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent / "lib"))

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config" / "price_bands.json"

# Clockwise-adjacent section order per level. Direction is irrelevant to the checks;
# adjacency is what matters. Sourced from config/price_bands.json rings.
def rings() -> dict:
    return json.loads(CONFIG.read_text())["rings"]


def legend() -> dict:
    """band label -> rgb, from the config's measured legendRgb values."""
    cfg = json.loads(CONFIG.read_text())
    return {b["label"].upper(): tuple(b["legendRgb"]) for b in cfg["bands"]}


def match(c, palette, tol=14):
    """Nearest legend colour under a max-channel metric, or None past the tolerance.

    Deliberately strict. A loose tolerance turns every antialiased edge pixel and every
    row-divider line into a spurious band, which is how the first version of this
    produced CLUB bands inside the upper deck.
    """
    best, bd = None, 1 << 30
    for label, k in palette.items():
        d = max(abs(c[i] - k[i]) for i in range(3))
        if d < bd:
            bd, best = d, label
    return best if bd <= tol else None


def label_discs(im, box, scale=1.0, thr=40):
    """Centroids of the chart's black section-number discs.

    Connected components on near-black. The discs are the only large solid dark blobs in
    the seating area, so shape filters separate them from text and logos.

    `thr` IS LOAD-BEARING, and it is why main() searches for it rather than fixing it.
    The chart draws section dividers as thin dark lines. Where a disc touches one, a
    loose threshold lets connectivity merge the disc into the ENTIRE line network - one
    blob of 44,463 pixels at a fill ratio of 0.02, which every shape filter then
    correctly rejects. The disc vanishes with nothing in the rejection list to explain
    it. That cost exactly one section on the 300dpi chart: 27 upper discs instead of 28.
    At 300dpi the divider lines are antialiased to mid-grey while the discs stay solid
    black, so tightening the threshold breaks the merge. Measured on that chart:

        thr < 30  -> 50 discs      thr < 50  -> 49
        thr < 40  -> 50 discs      thr < 60  -> 49  (the old default)

    An EROSION-based detector was tried first and abandoned: the discs carry white
    numerals, so only a thin annulus is solid dark, and its thickness varies with the
    digit count. Every radius either over-segmented one disc into arcs or lost the
    thin-annulus discs entirely - 105 discs at r=7px, 25 at r=10px, never 50.

    Size thresholds are RESOLUTION-RELATIVE. They were originally absolute, tuned on a
    1020px-wide chart, and silently rejected every disc on the 2550px version - a
    higher-resolution input made the tool find nothing, which is the opposite of the
    expected failure and would have read as "the PDF is not the same chart".
    """
    x0, y0, x1, y1 = box
    minpx = int(180 * scale * scale)
    maxpx = int(1400 * scale * scale)
    min_side = int(14 * scale)
    dark = lambda c: c[0] < thr and c[1] < thr and c[2] < thr
    seen = bytearray(im.w * im.h)
    out = []
    for y in range(y0, y1):
        for x in range(x0, x1):
            i = y * im.w + x
            if seen[i] or not dark(im.px(x, y)):
                continue
            q = deque([(x, y)])
            seen[i] = 1
            pts = []
            while q:
                a, b = q.popleft()
                pts.append((a, b))
                for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    nx, ny = a + dx, b + dy
                    j = ny * im.w + nx
                    if x0 <= nx < x1 and y0 <= ny < y1 and not seen[j] and dark(im.px(nx, ny)):
                        seen[j] = 1
                        q.append((nx, ny))
            if not (minpx <= len(pts) <= maxpx):
                continue
            xs = [p[0] for p in pts]
            ys = [p[1] for p in pts]
            w, h = max(xs) - min(xs) + 1, max(ys) - min(ys) + 1
            # Roughly circular and mostly filled - excludes text and logos.
            if (w < min_side or h < min_side or abs(w - h) > 8 * scale
                    or len(pts) / (w * h) < 0.45):
                continue
            out.append((sum(xs) / len(xs), sum(ys) / len(ys)))
    return out


def split_rings(pts, cx, cy):
    """Separate inner from outer ring by ELLIPTICALLY normalised radius.

    Raw radius does not work: the arena is wider than it is tall, so an upper-deck
    section at top-centre sits closer to the middle than a lower-bowl section at a
    corner. Normalising by the bounding half-axes makes the two rings separate cleanly -
    on the real chart the gap between them is 0.18, an order above the within-ring
    spacing.
    """
    a = max(abs(p[0] - cx) for p in pts) or 1
    b = max(abs(p[1] - cy) for p in pts) or 1
    items = [{"x": x, "y": y,
              "r": math.hypot((x - cx) / a, (y - cy) / b),
              "a": math.degrees(math.atan2(y - cy, x - cx)) % 360} for x, y in pts]
    items.sort(key=lambda d: d["r"])
    i = max(range(len(items) - 1), key=lambda k: items[k + 1]["r"] - items[k]["r"])
    # Each ring is returned sorted by ANGLE, not radius. anchor() walks a ring assuming
    # angular order, and returning them still radius-sorted assigns sections to
    # essentially random discs - which scored 0/11 and looked like a broken extractor
    # rather than a broken sort.
    by_angle = lambda g: sorted(g, key=lambda d: d["a"])
    return by_angle(items[:i + 1]), by_angle(items[i + 1:]), a, b


def anchor(ring, order, anchor_section, at_deg=90.0):
    """Attach section labels to detected discs by angle.

    The disc nearest `at_deg` - bottom-centre, since screen y grows downward - is taken
    to be `anchor_section`, and the rest follow in ring order. No OCR needed: the
    adjacency order is known from the venue geometry, so one anchor fixes all of them.

    Anchoring by NAME rather than by list position is load-bearing. An earlier version
    used order[0], which silently broke the moment the caller passed a reversed ring:
    reversing moves 101 off the front, so the bottom-centre disc was labelled 102 and
    every section shifted by one. Agreement fell to 21% and looked like a bad extraction
    rather than a bad index.
    """
    if anchor_section not in order:
        raise ValueError(f"{anchor_section!r} is not in the ring order")
    rot = order.index(anchor_section)
    seq = order[rot:] + order[:rot]
    k = min(range(len(ring)), key=lambda i: abs(((ring[i]["a"] - at_deg + 180) % 360) - 180))
    n = len(seq)
    return [(seq[(j - k) % n], ring[j]) for j in range(len(ring))]


def sector_bands(im, pairs, cx, cy, a, b, r_lo, r_hi, palette, margin=0.30,
                 min_share=0.07, tol=14):
    """Histogram band colours inside each section's angular sector.

    Sectors, not rays. The first version walked a single radial line and its samples
    strayed across wedge boundaries into a neighbour's colour - which is how LOWER
    bands turned up in 200-level sections. A sector bounded by the half-angles to each
    neighbour, shrunk by `margin`, cannot leave its own wedge.
    """
    n = len(pairs)
    angs = [p[1]["a"] for p in pairs]
    res = {}
    for i, (sec, d) in enumerate(pairs):
        back = ((d["a"] - angs[(i - 1) % n]) % 360) / 2
        fwd = ((angs[(i + 1) % n] - d["a"]) % 360) / 2
        a0, a1 = d["a"] - back * (1 - margin), d["a"] + fwd * (1 - margin)
        cnt, radii = Counter(), {}
        for ia in range(max(6, int((a1 - a0) * 2)) + 1):
            th = math.radians(a0 + (a1 - a0) * ia / max(6, int((a1 - a0) * 2)))
            for ir in range(91):
                r = r_lo + (r_hi - r_lo) * ir / 90
                x, y = int(cx + math.cos(th) * r * a), int(cy + math.sin(th) * r * b)
                if not (0 <= x < im.w and 0 <= y < im.h):
                    continue
                m = match(im.px(x, y), palette, tol)
                if m:
                    cnt[m] += 1
                    radii.setdefault(m, []).append(r)
        total = sum(cnt.values()) or 1
        keep = sorted((sum(radii[k]) / len(radii[k]), k, cnt[k] / total)
                      for k in cnt if cnt[k] / total >= min_share)
        res[sec] = [{"band": k, "share": round(s, 3), "meanRadius": round(r, 3)}
                    for r, k, s in keep]
    return res


def mirror_pairs(order, a, b):
    """Sections that must match under the reflection which SWAPS a and b.

    Note the semantics carefully - a first version of this docstring said "about the
    axis through a and b", which is the PERPENDICULAR reflection and the wrong one.
    Passing ("101","115") gives the reflection across the rink's LONG axis: 101 and 115
    map to each other, 102 to 104, and so on. That is the symmetry the chart actually
    has, because the two ENDS are priced differently ("Sharks attack twice") while the
    two SIDES are not.
    """
    n = len(order)
    ia, ib = order.index(a), order.index(b)
    out = []
    for i in range(n):
        j = (ia + ib - i) % n
        if i < j:
            out.append((order[i], order[j]))
    return out


def coverage(extracted, palette):
    """Legend bands that appear nowhere in the extraction.

    THE GATE NEEDS THIS BECAUSE SYMMETRY ALONE IS GAMEABLE, and I nearly gamed it.
    Raising `min_share` from 0.07 to 0.12 lifted mirror agreement from 92% to 96% -
    past the threshold - by making GLASS, TEAL and ORANGE vanish from the lower bowl
    entirely. Both halves of every mirror pair then agreed, because both omitted the
    same thin bands. 66 band-entries fell to 49 and 15 distinct bands to 11.

    That would have certified a map missing the three most expensive products in the
    building ($495, $340 and $279 per game) while reporting 96% confidence. A symmetric
    omission is invisible to a symmetry check by construction.

    The invariant that closes it: the chart prints a legend entry only for a band that
    has seats, so EVERY legend band must appear somewhere in a complete extraction.
    Absent bands are missing data, whatever the symmetry says.
    """
    seen = {e["band"] for v in extracted.values() for e in v}
    return sorted(set(palette) - seen)


def agreement(extracted, pairs):
    """Fraction of mirror pairs whose band stacks match, plus the disagreements."""
    ok, bad = 0, []
    for x, y in pairs:
        A = [e["band"] for e in extracted.get(x, [])]
        B = [e["band"] for e in extracted.get(y, [])]
        if A == B:
            ok += 1
        else:
            bad.append((x, y, A, B))
    return (ok / len(pairs) if pairs else 0.0), ok, len(pairs), bad


def self_test() -> int:
    fails = []

    def check(label, got, want):
        if got != want:
            fails.append(f"{label}: got {got!r}, want {want!r}")

    pal = {"RED": (200, 30, 30), "BLUE": (30, 30, 200)}
    check("exact colour matches", match((200, 30, 30), pal), "RED")
    check("near colour matches within tolerance", match((206, 36, 24), pal), "RED")
    check("a divider grey matches nothing", match((90, 90, 92), pal), None)
    check("white matches nothing", match((255, 255, 255), pal), None)

    # Elliptical ring split: two rings around a wide-but-short arena. Raw radius would
    # interleave them; normalised radius must not.
    inner = [(math.cos(math.radians(t)) * 200, math.sin(math.radians(t)) * 100)
             for t in range(0, 360, 30)]
    outer = [(math.cos(math.radians(t)) * 300, math.sin(math.radians(t)) * 150)
             for t in range(0, 360, 30)]
    lo, hi, _, _ = split_rings(inner + outer, 0, 0)
    check("ring split sizes", (len(lo), len(hi)), (12, 12))
    check("inner ring is the inner one", all(d["r"] < 0.9 for d in lo), True)
    # anchor() assumes angular order; returning radius order silently scrambles
    # everything, so assert it explicitly.
    check("inner ring comes back angle-sorted",
          [round(d["a"]) for d in lo] == sorted(round(d["a"]) for d in lo), True)
    check("outer ring comes back angle-sorted",
          [round(d["a"]) for d in hi] == sorted(round(d["a"]) for d in hi), True)

    # Anchoring: the section nearest bottom-centre becomes the first of the order.
    ring = [{"a": t, "r": 1.0, "x": 0, "y": 0} for t in (0, 90, 180, 270)]
    got = dict(anchor(ring, ["A", "B", "C", "D"], "A"))
    check("anchor section lands at 90 degrees", got["A"]["a"], 90)
    check("order walks from the anchor", got["B"]["a"], 180)
    # The regression that cost a debugging round: a rotated or reversed ring must still
    # put the NAMED anchor section on the bottom-centre disc.
    got = dict(anchor(ring, ["C", "D", "A", "B"], "A"))
    check("a rotated ring still anchors by name", got["A"]["a"], 90)
    check("and still walks in ring order", got["B"]["a"], 180)
    got = dict(anchor(ring, ["A", "D", "C", "B"], "A"))
    check("a reversed ring anchors by name too", got["A"]["a"], 90)
    check("reversed ring walks the other way", got["D"]["a"], 180)

    # Mirror pairing about the long axis.
    order = ["101", "102", "103", "104", "115", "116", "117", "118"]
    mp = mirror_pairs(order, "101", "115")
    # The reflection SWAPS the named sections and pairs their neighbours inward.
    check("named sections are swapped", ("101", "115") in mp, True)
    check("neighbours pair inward", ("102", "104") in mp, True)
    check("and on the far arc too", ("116", "118") in mp, True)
    check("no section pairs with itself", all(x != y for x, y in mp), True)
    # The real geometry: 22 lower sections give 11 pairs.
    check("lower ring yields 11 pairs",
          len(mirror_pairs(rings()["lower"], "101", "115")), 11)

    # Agreement scoring, including the case the gate exists for.
    ex = {"102": [{"band": "X"}], "118": [{"band": "X"}],
          "103": [{"band": "Y"}], "117": [{"band": "Z"}]}
    frac, ok, tot, bad = agreement(ex, [("102", "118"), ("103", "117")])
    check("agreement counts matching pairs", (ok, tot), (1, 2))
    check("agreement fraction", round(frac, 2), 0.5)
    check("disagreement is reported", bad[0][0], "103")

    # coverage(): the guard against buying agreement by discarding data.
    pal3 = {"A": (1, 1, 1), "B": (2, 2, 2), "C": (3, 3, 3)}
    check("nothing missing when all bands appear",
          coverage({"1": [{"band": "A"}, {"band": "B"}], "2": [{"band": "C"}]}, pal3), [])
    check("a dropped band is reported",
          coverage({"1": [{"band": "A"}], "2": [{"band": "C"}]}, pal3), ["B"])
    check("an empty extraction misses everything",
          coverage({}, pal3), ["A", "B", "C"])
    # The specific failure this exists for: a symmetric omission scores perfectly on
    # symmetry while dropping a band.
    sym = {"102": [{"band": "A"}], "128": [{"band": "A"}]}
    _, ok, tot, _ = agreement(sym, [("102", "128")])
    check("symmetric omission passes the symmetry check", (ok, tot), (1, 1))
    check("but coverage catches it", coverage(sym, pal3), ["B", "C"])

    # The config the tool reads must actually carry what it needs.
    pal_real = legend()
    check("23 legend colours available", len(pal_real), 23)
    check("legend values are rgb triples",
          all(len(v) == 3 for v in pal_real.values()), True)
    r = rings()
    check("lower ring present", len(r["lower"]), 22)
    check("upper ring present", len(r["upper"]), 28)

    for f in fails:
        print(f"  FAIL {f}", file=sys.stderr)
    print(f"self-test: {'FAILED' if fails else 'passed'} ({len(fails)} failure(s))")
    return 1 if fails else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--image", type=pathlib.Path)
    ap.add_argument("--out", type=pathlib.Path)
    ap.add_argument("--search-centre", type=int, default=0, metavar="PX",
                    help="search the bowl centre +/- PX and keep the best-scoring one")
    ap.add_argument("--tol", type=int, default=14,
                    help="max-channel colour tolerance; raise for a JPEG-derived image")
    ap.add_argument("--min-agreement", type=float, default=0.95,
                    help="refuse to write a map below this mirror agreement")
    ap.add_argument("--centre", nargs=2, type=int, metavar=("X", "Y"))
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()

    if args.self_test:
        return self_test()
    if not args.image:
        print("--image is required", file=sys.stderr)
        return 2

    import minipng
    im = minipng.Img(str(args.image))
    if args.centre:
        cx, cy = args.centre
    else:
        # Centre of the seating bowl, taken as the centroid of all label discs. The
        # image centre is close but not identical - the chart has a title band above
        # the map - and a few pixels of offset skews every sector angle.
        rough = label_discs(im, (int(im.w * 0.13), int(im.h * 0.18),
                                 int(im.w * 0.88), int(im.h * 0.63)), im.w / 1020.0)
        if not rough:
            print("no label discs found at all - wrong image?", file=sys.stderr)
            return 1
        cx = int(sum(p[0] for p in rough) / len(rough))
        cy = int(sum(p[1] for p in rough) / len(rough))
        print(f"derived bowl centre: ({cx}, {cy})")
    # Baseline is the 1020px-wide chart the thresholds were tuned on.
    scale = im.w / 1020.0
    box = (int(im.w * 0.13), int(im.h * 0.18), int(im.w * 0.88), int(im.h * 0.63))
    r = rings()
    want = len(r["lower"]) + len(r["upper"])

    # Search the darkness threshold for the one that finds exactly the known number of
    # sections, rather than hardcoding a value tuned on one image. The count is known
    # independently from the ring geometry, so this is a check with a right answer - not
    # a fit. Reported either way, and a miss is loud.
    pts, used = [], None
    for thr in (40, 30, 50, 35, 45, 60):
        cand = [p for p in label_discs(im, box, scale, thr)
                if math.hypot(p[0] - cx, p[1] - cy) > 60 * scale]
        if used is None or abs(len(cand) - want) < abs(len(pts) - want):
            pts, used = cand, thr
        if len(cand) == want:
            pts, used = cand, thr
            break
    print(f"darkness threshold <{used} -> {len(pts)} discs (expected {want})")
    if len(pts) != want:
        print(f"disc count does not match the known geometry - refusing to guess",
              file=sys.stderr)
        return 1
    print(f"section label discs: {len(pts)}")
    if len(pts) < 20:
        print("too few discs found - is this the pricing chart, and is --centre right?",
              file=sys.stderr)
        return 1

    pal = legend()

    def evaluate(ccx, ccy, verbose=False):
        """Extract at a given bowl centre and score it. Returns (ok, total, extraction)."""
        inner, outer, a, b = split_rings(pts, ccx, ccy)
        if len(inner) != len(r["lower"]) or len(outer) != len(r["upper"]):
            return -1, 1, {}
        parts, ok, tot = {}, 0, 0
        for ring, order, axis, lo, hi, label in (
                # Swept independently of the upper ring: 0.46-0.92 scores 9/11 where
                # the shared 0.46-0.88 scored 8/11. The two rings have different radial
                # extents and no reason to share a window.
                (inner, r["lower"], ("101", "115"), 0.46, 0.92, "lower"),
                (outer, r["upper"], ("201", "215"), 0.95, 1.18, "upper")):
            best = None
            for dname, seq in (("as-listed", order), ("reversed", list(reversed(order)))):
                ex = sector_bands(im, anchor(ring, seq, axis[0]), ccx, ccy, a, b,
                                  lo, hi, pal, tol=args.tol)
                _, o, n, bad = agreement(ex, mirror_pairs(seq, *axis))
                if best is None or o > best[0]:
                    best = (o, n, ex, dname, bad)
            if verbose:
                print(f"  {label} ring, direction {best[3]}: {best[0]}/{best[1]}")
                for x, y, A, B in best[4]:
                    print(f"    MISMATCH {x} {A}")
                    print(f"             {y} {B}")
            parts.update(best[2])
            ok += best[0]
            tot += best[1]
        return ok, tot, parts

    # THE BOWL CENTRE IS THE MOST SENSITIVE PARAMETER IN THIS TOOL, by a wide margin.
    # Measured on the 300dpi chart: (1273,1367) scored 62% and (1275,1355) scored 83% -
    # twelve pixels of vertical offset cost twenty-one points. Every sector angle is
    # measured from it, so a small offset rotates every wedge slightly and starts
    # sampling neighbours.
    #
    # The centroid of the label discs is NOT the bowl centre: there are 28 upper discs
    # against 22 lower, and none for the plaza boxes, so the centroid is pulled off by
    # the uneven distribution. Rather than derive it more cleverly, search it - agreement
    # is a scoreable objective with a known right answer, exactly like the threshold and
    # direction searches above.
    if args.search_centre:
        step = max(4, args.search_centre // 3)
        best = None
        for dy in range(-args.search_centre, args.search_centre + 1, step):
            for dx in range(-args.search_centre, args.search_centre + 1, step):
                ok, tot, _ = evaluate(cx + dx, cy + dy)
                if best is None or ok > best[0]:
                    best = (ok, tot, cx + dx, cy + dy)
        print(f"centre search: best {best[0]}/{best[1]} at ({best[2]}, {best[3]}) "
              f"- started from ({cx}, {cy})")
        cx, cy = best[2], best[3]

    lo_ok, lo_tot, _ = 0, 0, None
    ok, tot, ex = evaluate(cx, cy, verbose=True)
    frac = ok / tot if tot else 0.0
    print(f"mirror agreement overall: {ok}/{tot} -> {frac:.0%} "
          f"(need {args.min_agreement:.0%})")

    missing = coverage(ex, pal)
    if missing:
        print(f"coverage: {len(pal) - len(missing)}/{len(pal)} legend bands placed; "
              f"MISSING {missing}")
    else:
        print(f"coverage: all {len(pal)} legend bands placed")

    if missing:
        print(f"\nREFUSING to write a map that places no seats in {missing}. The chart "
              f"prints a legend entry only for a band that exists, so an absent band is "
              f"missing data - and a symmetric omission is invisible to the symmetry "
              f"check, which is exactly how a higher min_share can buy agreement by "
              f"discarding the thinnest bands.", file=sys.stderr)
        return 1

    if frac < args.min_agreement:
        print(f"\nREFUSING to write a map at {frac:.0%} agreement. The arena is "
              f"mirror-symmetric, so disagreeing pairs mean the extraction is wrong "
              f"somewhere - and a section map that is mostly right would misprice comps "
              f"silently forever. Get a higher-resolution chart (ops#31) or raise the "
              f"resolution of this one.", file=sys.stderr)
        return 1

    out = args.out or (ROOT / "data" / "price_band_sections.json")
    out.write_text(json.dumps({
        "_comment": ("Section -> price band, extracted from the official chart by "
                     "scripts/extract_price_bands.py and gated on mirror-symmetry "
                     "agreement. Bands are ROW RANGES within sections; this records "
                     "WHICH bands appear in each section and their radial order, not "
                     "row boundaries. See ops#19."),
        "sourceImage": args.image.name,
        "mirrorAgreement": round(frac, 4),
        "sections": ex,
    }, indent=2) + "\n")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
