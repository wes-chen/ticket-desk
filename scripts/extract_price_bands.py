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

CURRENT STATUS: 22/24 (92%) on the 300dpi chart at --centre 1274 1355, with the upper
ring exact at 13/13. Coverage refuses: 21/23 legend bands placed, missing CLUB 1 and
PROMENADE ROW 1 CENTER.

WHY IT IS NOT A RESOLUTION LIMIT. This block used to say the residue was a raster limit
needing a vector chart, and that was WRONG - a fifth instance of the instrument being
the thing at fault. Measured 2026-09-06 on the 2550x3300 chart, sampling every pixel and
setting min_share to 0 to see what the gate was actually discarding:

    band                      appears in   max share   where
    CLUB 1                    14 sections      5.60%   110:5.6 106:5.3 107:3.8 109:3.8
    PROMENADE ROW 1 CENTER     6 sections      7.20%   115:7.2 101:5.0
    GLASS                     12 sections      7.50%   126:7.5 112:5.9 118:5.7
    TEAL                       8 sections     12.00%   126:12.0 124:7.8

The gate is min_share=0.07. So:

  - CLUB 1 is PRESENT on the chart and inside the sampled radial window - 86% of its
    pixels are - and appears coherently in four adjacent lower sections. It is excluded
    purely because it never reaches 7% of any one section's samples.
  - Every remaining mirror mismatch is the same artefact seen twice. PROMENADE ROW 1
    CENTER is 7.2% in 115 and 5.0% in 101, so it lands on one side of the gate and not
    the other: that IS the 101/115 mismatch. GLASS 7.5% v 5.9% is the 126/118 mismatch.
    TEAL 12.0% v 7.8% is 124/120. The mirror pairs do not disagree about the chart; they
    disagree about a threshold.

THE COHERENCE GATE (--coherent, ops#53) AND WHAT IT DID AND DID NOT FIX.

Result: **coverage solved, mirror agreement not.** With radial segmentation all 23 legend
bands are placed - the gap that had blocked this for days - and the UPPER ring reaches
13/13 exactly. The lower ring sits at 6-8/11, so overall agreement is 79-83% against a 95%
gate, and the tool still REFUSES. `min_share` was not moved in either direction and the
gate was not lowered. Default behaviour is unchanged: without --coherent this reproduces
the 92% / 21-of-23 baseline exactly.

Four hypotheses were tested and killed. They are recorded because each looked right, and
re-running them is the expensive way to find that out again:

  1. PER-COLOUR coherence - score each colour's thickness and angular span on its own.
     Reached 23/23 and then admitted CLUB 1 into upper-deck section 212 and LOWER 3 into
     225. Those tails are real bands, just not that section's, and only the radial ORDER
     exposes it. Per-colour scoring discards order. This is why radial_runs segments.
  2. A KNIFE EDGE AT 4px - the assumption that MIN_BAND_PX had merely relocated the old
     7%-share threshold. Falsified by measuring: the bands that differ across a mirror
     pair are 16-34px thick, nowhere near the gate. They are absent from one side
     entirely, not marginal on it.
  3. RADIAL WINDOWS - 12 combinations of lower 0.30-0.36/0.88-0.92 and upper
     0.95-0.98/1.18. Every one gives 23/23 coverage and agreement stays 71-79%. The
     windows are not the constraint.
  4. THE BOWL CENTRE - re-searched under this gate rather than reusing the share gate's
     (1274,1355). Best 20/24 during the search, 19/24 on the final 1px pass, and coverage
     REGRESSED to 22/23. Note the search samples at --search-step and the final pass at
     --sample-step, so a search optimum does not transfer; that gap is real and unfixed.
  5. SECTOR MARGIN - shrinking each wedge more should strictly reduce neighbour clipping
     without losing an arc that spans the wedge. 0.30 -> 79%, 0.45 -> 75%, 0.60 -> 83%.
     Non-monotonic, so this is noise rather than the mechanism.

THE RESIDUAL, and the two live hypotheses for whoever picks this up. Every failing lower
pair has the same signature: one side carries exactly ONE extra thick band the other
lacks, and the sequences otherwise match in order. Which side gets the extra MOVES when
the centre moves.

  (a) Sector clipping - a label disc is not at the angular centre of its wedge, so the
      sector reaches into a neighbour. Supported by the extra being a band its neighbour
      genuinely has (101's extra CLUB 3 is 102/114's largest band). Weakened by the
      margin sweep being non-monotonic.
  (b) The lower bowl is GENUINELY not mirror-symmetric, and the 95% gate's premise is
      wrong for it. The chart shows why it might be: the long axis has the PENALTY BOX on
      the 115 side and AWAY/HOME BENCH on the 101 side, which really does displace
      seating near centre ice. The upper ring being 13/13 while the lower ring alone
      fails fits this. If (b) is right, the fix is a different oracle for the lower ring,
      NOT a lower gate.

Distinguishing them needs one thing neither this tool nor the chart can supply: an
independent statement of which bands section 101 actually contains. That is the row-level
data in ops#31, so it may be that ops#19 needs Wesley after all - for a different reason
than the one that was retired, and only for the lower bowl.

WHY SHARE IS THE WRONG STATISTIC. A band's share depends on how thick the OTHER bands in
its section are, so a genuinely one-row band can never score well no matter the
resolution - which is why 2.5x the pixels did not help, and why a vector chart would not
either. What separates a real thin band from an antialiasing artefact is not its size but
its RADIAL COHERENCE: a real band occupies a contiguous run of radii at a consistent
distance across the sector, while edge noise is scattered. Replacing the share gate with
a coherence test is the fix, and it needs nothing from Wesley. Not done here because it
changes what the tool admits, and this tool's output is a model constant.

Widening the lower radial window was tested and is NOT the answer: inner bounds of 0.46,
0.38, 0.30 and 0.24 all leave coverage at 21/23. Worth knowing because over half of
GLASS's and TEAL's pixels do sit inside r<0.46, so the window looked like a plausible
culprit and is not.

Usage:
    python3 scripts/extract_price_bands.py --image CHART.png [--out FILE]
    python3 scripts/extract_price_bands.py --image CHART.png --min-agreement 0.95
    python3 scripts/extract_price_bands.py --image CHART.png --sample-step 1
    python3 scripts/extract_price_bands.py --image CHART.png --lower-window 0.30 0.92
    python3 scripts/extract_price_bands.py --self-test

The chart is a raster inside a PDF; scripts/jpeg_to_png.mjs converts it, because there is
no JPEG decoder on this machine other than Chromium.
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
                 min_share=0.07, tol=14, step=None, coherent=None):
    """Histogram band colours inside each section's angular sector.

    Sectors, not rays. The first version walked a single radial line and its samples
    strayed across wedge boundaries into a neighbour's colour - which is how LOWER
    bands turned up in 200-level sections. A sector bounded by the half-angles to each
    neighbour, shrunk by `margin`, cannot leave its own wedge.

    SAMPLE DENSITY IS DERIVED FROM PIXELS, NOT FIXED. `step` is the target spacing in
    PIXELS; None keeps the historical fixed 91x(sector width) grid.

    This mattered more than it looks. The fixed grid took 91 radial samples whatever the
    image size, so feeding it the 2550x3300 chart instead of the 1020x1320 one threw the
    extra resolution away entirely - a single-row band 4px wide at the old size is 10px
    at the new one, and both got about four samples. ops#31 predicted a higher-resolution
    chart would fix the thin-band misses; it cannot while the sampler is resolution-blind,
    which is why the first hi-res run scored WORSE (75%) than the low-res one (92%).

    Note this is not a tunable that can buy agreement: raising density can only add
    samples inside the same sector, so it makes thin bands MORE detectable on both sides
    of a mirror pair. The parameter that can buy agreement by discarding bands is
    `min_share`, which is what coverage() guards.
    """
    n = len(pairs)
    angs = [p[1]["a"] for p in pairs]
    res = {}
    for i, (sec, d) in enumerate(pairs):
        back = ((d["a"] - angs[(i - 1) % n]) % 360) / 2
        fwd = ((angs[(i + 1) % n] - d["a"]) % 360) / 2
        a0, a1 = d["a"] - back * (1 - margin), d["a"] + fwd * (1 - margin)
        cnt, radii = Counter(), {}
        # A non-positive step means "use the historical fixed grid", which makes
        # --sample-step 0 a usable way to reproduce the old numbers rather than a
        # division by zero.
        if not step or step <= 0:
            n_a, n_r = max(6, int((a1 - a0) * 2)), 90
        else:
            # Pixel extents of this sector. max(a, b) is the larger ellipse semi-axis,
            # so both are upper bounds - erring toward oversampling rather than
            # silently under-resolving a thin band.
            span = max(a, b)
            n_r = max(90, math.ceil((r_hi - r_lo) * span / step))
            arc = math.radians(a1 - a0) * span * r_hi
            n_a = max(6, math.ceil(arc / step))
        # Per-RADIUS colour votes across the angular sweep. The sweep is replication,
        # not extra area: a band is an arc, so at a radius inside it most angular samples
        # agree, while an antialiased edge wins no radius outright.
        votes = [Counter() for _ in range(n_r + 1)]
        for ia in range(n_a + 1):
            th = math.radians(a0 + (a1 - a0) * ia / n_a)
            for ir in range(n_r + 1):
                r = r_lo + (r_hi - r_lo) * ir / n_r
                x, y = int(cx + math.cos(th) * r * a), int(cy + math.sin(th) * r * b)
                if not (0 <= x < im.w and 0 <= y < im.h):
                    continue
                m = match(im.px(x, y), palette, tol)
                if m:
                    cnt[m] += 1
                    radii.setdefault(m, []).append(r)
                    votes[ir][m] += 1
        total = sum(cnt.values()) or 1

        if coherent is None:
            keep = sorted((sum(radii[k]) / len(radii[k]), k, cnt[k] / total)
                          for k in cnt if cnt[k] / total >= min_share)
            res[sec] = [{"band": k, "share": round(sh, 3), "meanRadius": round(r, 3)}
                        for r, k, sh in keep]
            continue

        px_per_step = (r_hi - r_lo) * max(a, b) / max(1, n_r)
        res[sec] = [{"band": r["band"], "share": round(cnt[r["band"]] / total, 3),
                     "meanRadius": round(r["meanRadius"], 3),
                     "thicknessPx": round(r["thicknessPx"], 1)}
                    for r in radial_runs(votes, r_lo, r_hi, n_r, px_per_step, coherent)]
    return res


# Minimum radial thickness, in PIXELS, for a run of one colour to count as a band.
#
# THIS IS THE NUMBER THAT REPLACES min_share, and it is read off the image rather than
# fitted to the score. Antialiasing along a band boundary is 1-2px either side of the edge;
# a genuine single seating row on the 300dpi chart is ~10px. 4px sits clearly above the
# first and clearly below the second.
#
# Unlike a share threshold it is resolution-HONEST: on a bigger chart a real thin band gets
# thicker while an antialiased edge does not, so the separation improves. Share moves the
# other way, which is exactly why 2.5x the pixels made the old gate score WORSE.
MIN_BAND_PX = 4.0

# Share of a radius's angular samples the winning colour must hold to own that radius.
#
# Not a share-of-sector test in disguise - it is per-RADIUS, so a one-row band competes
# only against the handful of radii it occupies, never against how thick its neighbours are.
# That is the whole defect being fixed. A radius inside a real arc scores near 1.0.
MIN_RADIUS_VOTE = 0.5


def radial_runs(votes, r_lo, r_hi, n_r, px_per_step, cfg):
    """Segment a sector's radial profile into bands. -> ordered list, inner to outer.

    RADIAL COHERENCE INSTEAD OF SHARE, and segmentation instead of per-colour scoring.

    The bug this replaces: a band's share of a sector depends on how thick the OTHER bands
    in that section are, so a genuine one-row band can never score well at any resolution.
    CLUB 1 peaked at 5.6% against a 7% gate; PROMENADE ROW 1 CENTER hit 7.2% in section 115
    and 5.0% in its mirror 101 - so the "mirror disagreement" was two sections straddling a
    threshold, not two sections seeing different charts.

    Why segmentation rather than testing each colour on its own. A first attempt scored each
    colour's thickness and angular span independently. It reached 23/23 coverage and then
    admitted LOWER 3 and CLUB 1 inside UPPER-ring sections - lower-bowl bands whose tails
    genuinely reach past the upper window's inner edge. Judged alone they look like real
    bands, because they ARE real bands; they are just not this section's. Only the radial
    ORDER exposes that, and order is precisely what per-colour scoring throws away.

    So: bands in a section are concentric and non-overlapping, and scanning outward crosses
    them in sequence. Take the winning colour at each radius, merge equal neighbours into
    runs, and keep the runs thick enough to be seats rather than an edge. Order comes free,
    and one colour cannot appear twice - which is right, since the legend maps colour to
    band one-to-one.
    """
    min_px, min_vote = cfg
    seq = []
    for ir in range(n_r + 1):
        v = votes[ir]
        tot = sum(v.values())
        if not tot:
            seq.append(None)
            continue
        band, n = v.most_common(1)[0]
        # A radius where no colour holds a majority is a boundary, not a band. STRICTLY
        # greater: an exact 50/50 tie is the definition of a boundary pixel, and a >=
        # test let it own the radius by Counter tie-break order - i.e. by insertion
        # order, which is not a measurement of anything. Caught by the self-test.
        seq.append(band if n / tot > min_vote else None)

    runs, i = [], 0
    while i <= n_r:
        if seq[i] is None:
            i += 1
            continue
        j = i
        while j + 1 <= n_r and seq[j + 1] == seq[i]:
            j += 1
        thickness = (j - i + 1) * px_per_step
        if thickness >= min_px:
            mid = r_lo + (r_hi - r_lo) * ((i + j) / 2) / max(1, n_r)
            runs.append({"band": seq[i], "meanRadius": mid, "thicknessPx": thickness,
                         "from": i, "to": j})
        i = j + 1

    # One colour, one band. If a colour wins two separated runs, the thicker is the band and
    # the thinner is bleed from an adjacent ring or a repeated hue elsewhere in the sector.
    best = {}
    for r in runs:
        if r["band"] not in best or r["thicknessPx"] > best[r["band"]]["thicknessPx"]:
            best[r["band"]] = r
    return sorted(best.values(), key=lambda r: r["meanRadius"])


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

    # ---- radial_runs: the coherence gate (ops#53) ----
    # THE WHOLE CONTRACT IS THESE TWO CASES. A gate that admits a genuine one-row band
    # but not scattered edge noise AT THE SAME SAMPLE COUNT is measuring shape; one that
    # cannot tell them apart is measuring size, which is the min_share defect being fixed.
    def votes_from(seq, unanimous=8):
        """Build per-radius vote Counters from a list of band names (None = boundary)."""
        out = []
        for b in seq:
            c = Counter()
            if b is not None:
                c[b] = unanimous
            out.append(c)
        return out

    cfg = (4.0, 0.5)

    # A one-row band between two thick neighbours: 10px of THIN. 10 >= MIN_BAND_PX.
    # Bands are concentric row ranges, so each colour owns ONE contiguous run - hence
    # INNER/THIN/OUTER rather than BIG/THIN/BIG, which would be a colour appearing twice
    # and is tested separately below as bleed.
    seq = ["INNER"] * 40 + ["THIN"] * 10 + ["OUTER"] * 51
    runs = radial_runs(votes_from(seq), 0.0, 1.0, 100, 1.0, cfg)
    check("a one-row band is admitted on thickness",
          [r["band"] for r in runs], ["INNER", "THIN", "OUTER"])
    check("and its thickness is reported in px",
          [r["thicknessPx"] for r in runs if r["band"] == "THIN"], [10.0])

    # Scattered noise with the SAME number of samples - 10 of them - never contiguous.
    seq = ["BIG"] * 101
    for i in range(5, 100, 10):
        seq[i] = "NOISE"
    runs = radial_runs(votes_from(seq), 0.0, 1.0, 100, 1.0, cfg)
    check("scattered noise at the same sample count is rejected",
          [r["band"] for r in runs], ["BIG"])

    # A radius where no colour holds a majority is a boundary, not a band. Two colours
    # split 4/4 must own nothing - this is what keeps an antialiased edge from voting.
    tied = [Counter({"A": 4, "B": 4}) for _ in range(20)]
    check("a tied radius owns nothing", radial_runs(tied, 0.0, 1.0, 19, 1.0, cfg), [])

    # One colour, one band: the legend maps colour to band one-to-one, so a colour winning
    # two separated runs is bleed from an adjacent ring. Keep the thicker, not both.
    seq = ["X"] * 20 + ["Y"] * 40 + ["X"] * 6 + [None] * 35
    runs = radial_runs(votes_from(seq), 0.0, 1.0, 100, 1.0, cfg)
    check("a colour appearing twice is kept once", [r["band"] for r in runs], ["X", "Y"])
    check("and the thicker run is the one kept",
          [r["thicknessPx"] for r in runs if r["band"] == "X"], [20.0])

    # Order is inner-to-outer, because the radial ORDER is what distinguishes a section's
    # own band from a neighbouring ring's tail - the defect that sank per-colour scoring.
    seq = ["OUTERMOST"] * 10
    seq = ["A"] * 10 + ["B"] * 10 + ["C"] * 10 + [None] * 71
    runs = radial_runs(votes_from(seq), 0.0, 1.0, 100, 1.0, cfg)
    check("runs come back inner to outer", [r["band"] for r in runs], ["A", "B", "C"])

    # Sub-threshold thickness is rejected even when unanimous - 3px < 4px.
    seq = ["BIG"] * 40 + ["EDGE"] * 3 + ["BIG"] * 58
    runs = radial_runs(votes_from(seq), 0.0, 1.0, 100, 1.0, cfg)
    check("a 3px unanimous run is still too thin", [r["band"] for r in runs], ["BIG"])

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
    ap.add_argument("--sample-step", type=float, default=0.0, metavar="PX",
                    help="target sample spacing in PIXELS for the final extraction. "
                         "0 (the default) keeps the historical fixed grid and so "
                         "reproduces the measured 92%% baseline exactly. 1.0 uses every "
                         "pixel, which finds MORE real bands but currently scores worse "
                         "- see the share-gate finding in sector_bands.")
    ap.add_argument("--search-step", type=float, default=4.0, metavar="PX",
                    help="sample spacing during the bowl-centre search, which evaluates "
                         "hundreds of candidates and cannot afford 1px (default 4.0)")
    ap.add_argument("--lower-window", nargs=2, type=float, default=(0.46, 0.92),
                    metavar=("LO", "HI"),
                    help="radial window, in normalised elliptical radius, swept for the "
                         "lower ring (default 0.46 0.92)")
    ap.add_argument("--upper-window", nargs=2, type=float, default=(0.95, 1.18),
                    metavar=("LO", "HI"),
                    help="radial window for the upper ring (default 0.95 1.18)")
    ap.add_argument("--coherent", action="store_true",
                    help="gate bands on RADIAL COHERENCE (thickness in px, angular span, "
                         "contiguity) instead of share of the sector. See band_coherence. "
                         "Implies --sample-step 1 unless one is given, because coherence is "
                         "measured in pixels and the fixed grid throws pixels away.")
    ap.add_argument("--min-band-px", type=float, default=MIN_BAND_PX, metavar="PX",
                    help=f"radial thickness a band must reach under --coherent "
                         f"(default {MIN_BAND_PX})")
    ap.add_argument("--min-radius-vote", type=float, default=MIN_RADIUS_VOTE,
                    metavar="FRAC",
                    help=f"share of a radius's angular samples the winning colour must "
                         f"hold to own that radius (default {MIN_RADIUS_VOTE})")
    ap.add_argument("--sector-margin", type=float, default=0.30, metavar="FRAC",
                    help="shrink each section's angular sector by this fraction of the "
                         "half-angle to each neighbour (default 0.30). Bands are arcs "
                         "spanning the whole wedge, so shrinking MORE cannot lose a real "
                         "band - it only stops a sector clipping its neighbour.")
    ap.add_argument("--centre", nargs=2, type=int, metavar=("X", "Y"))
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()

    if args.self_test:
        return self_test()
    # Coherence is measured in PIXELS, and the historical fixed grid takes 91 radial
    # samples whatever the image size - so leaving step at 0 would hand the gate a
    # resolution it cannot see. Opt in to one without the other and it would silently
    # measure thickness against a grid instead of the chart.
    if args.coherent and not args.sample_step:
        args.sample_step = 1.0
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

    coh = (args.min_band_px, args.min_radius_vote) if args.coherent else None

    def evaluate(ccx, ccy, verbose=False, sample_step=None):
        """Extract at a given bowl centre and score it. Returns (ok, total, extraction)."""
        inner, outer, a, b = split_rings(pts, ccx, ccy)
        if len(inner) != len(r["lower"]) or len(outer) != len(r["upper"]):
            return -1, 1, {}
        parts, ok, tot = {}, 0, 0
        for ring, order, axis, lo, hi, label in (
                # Swept independently of the upper ring: 0.46-0.92 scores 9/11 where
                # the shared 0.46-0.88 scored 8/11. The two rings have different radial
                # extents and no reason to share a window.
                (inner, r["lower"], ("101", "115"), *args.lower_window, "lower"),
                (outer, r["upper"], ("201", "215"), *args.upper_window, "upper")):
            best = None
            for dname, seq in (("as-listed", order), ("reversed", list(reversed(order)))):
                ex = sector_bands(im, anchor(ring, seq, axis[0]), ccx, ccy, a, b,
                                  lo, hi, pal, margin=args.sector_margin,
                                  tol=args.tol, step=sample_step, coherent=coh)
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
                ok, tot, _ = evaluate(cx + dx, cy + dy, sample_step=args.search_step)
                if best is None or ok > best[0]:
                    best = (ok, tot, cx + dx, cy + dy)
        print(f"centre search: best {best[0]}/{best[1]} at ({best[2]}, {best[3]}) "
              f"- started from ({cx}, {cy})")
        cx, cy = best[2], best[3]

    lo_ok, lo_tot, _ = 0, 0, None
    ok, tot, ex = evaluate(cx, cy, verbose=True, sample_step=args.sample_step)
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
