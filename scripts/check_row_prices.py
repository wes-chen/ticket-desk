#!/usr/bin/env python3
"""Grade a pasted `section row price` table and turn it into row-range bands (ops#31).

WHY THIS EXISTS AS A GATE RATHER THAN A PARSER. ops#19 established that price bands are
ROW RANGES within sections, and that the official chart draws those bands but prints no
row numbers - so the boundaries are visible as pixels and not as data. The only source
for row numbers is a per-seat price lookup, read by hand. A hand-read table is exactly
the kind of input that poisons a constant quietly, which is why the type:input contract
requires a validator: an input contract without one is a wish.

WHAT IT PRODUCES. The thing the model actually needs and cannot currently express:
(section, rowFrom, rowTo) -> band. It derives that from prices rather than being told
it, so a misread row shows up as a boundary in the wrong place instead of vanishing.

Input format, one line per row, whitespace separated:

    110  1   142.00
    110  2   142.00
    110  15  99.00
    110  16  -          <- price not readable; recorded as a hole, never guessed

Usage:
    python3 scripts/check_row_prices.py --file paste.txt
    cat paste.txt | python3 scripts/check_row_prices.py
    python3 scripts/check_row_prices.py --self-test
"""

import argparse
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config" / "price_bands.json"

LINE = re.compile(r"^\s*([A-Za-z0-9]+)\s+(\d+)\s+(-|\$?[\d,]+(?:\.\d{1,2})?)\s*$")


def price_index(cfg: dict) -> dict[float, list[str]]:
    """Every published price -> the band(s) charging it.

    Both the renew and new columns count: the paste may come from either a member-rate
    screen or a new-buyer one, and refusing one of them would reject good data. A price
    shared by two bands is kept as a genuine ambiguity rather than silently resolved.
    """
    out: dict[float, list[str]] = {}
    for b in cfg["bands"]:
        for v in (b.get("avgPerGame") or {}).values():
            out.setdefault(float(v), []).append(b["label"])
    return out


def known_sections(cfg: dict) -> set[str]:
    return {s for ring in (cfg.get("rings") or {}).values() for s in ring}


def parse(text: str) -> tuple[list[dict], list[str]]:
    rows, problems = [], []
    for i, raw in enumerate(text.splitlines(), 1):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        m = LINE.match(raw)
        if not m:
            problems.append(f"line {i}: cannot parse {raw.strip()!r} - "
                            f"expected 'section row price'")
            continue
        sec, row, price = m.group(1), int(m.group(2)), m.group(3)
        rows.append({
            "line": i, "section": sec, "row": row,
            "price": None if price == "-" else float(price.lstrip("$").replace(",", "")),
        })
    return rows, problems


def grade(rows: list[dict], cfg: dict) -> tuple[list[str], list[str], dict]:
    """Returns (fatal, warnings, band ranges per section)."""
    fatal, warn = [], []
    prices = price_index(cfg)
    sections = known_sections(cfg)

    by_sec: dict[str, list[dict]] = {}
    for r in rows:
        by_sec.setdefault(r["section"], []).append(r)

    ranges: dict[str, list[dict]] = {}
    for sec, rs in sorted(by_sec.items()):
        if sections and sec not in sections:
            fatal.append(f"section {sec} is not on any known ring - typo, or the ring "
                         f"transcription is incomplete")

        rs.sort(key=lambda r: r["row"])
        seen = [r["row"] for r in rs]
        if len(set(seen)) != len(seen):
            dupes = sorted({x for x in seen if seen.count(x) > 1})
            fatal.append(f"section {sec}: row(s) {dupes} appear more than once")

        # Interior gaps. A row deliberately marked "-" is present-but-unpriced and is
        # NOT a gap; a row simply absent is, and silently interpolating across it would
        # invent a band boundary.
        missing = [n for n in range(min(seen), max(seen) + 1) if n not in set(seen)]
        if missing:
            warn.append(f"section {sec}: rows {missing[:8]}"
                        f"{'...' if len(missing) > 8 else ''} are absent entirely - "
                        f"mark an unreadable row as '-' rather than omitting it, so a "
                        f"hole is distinguishable from a boundary")

        priced = [r for r in rs if r["price"] is not None]
        for r in priced:
            if r["price"] not in prices:
                fatal.append(f"section {sec} row {r['row']} (line {r['line']}): "
                             f"${r['price']:.2f} matches no published band price. Either "
                             f"it was misread, or this screen does not show the prices "
                             f"in config/price_bands.json - both need knowing.")

        # Price must not RISE as rows move away from the ice.
        for a, b in zip(priced, priced[1:]):
            if b["price"] > a["price"] + 1e-9:
                fatal.append(f"section {sec}: row {b['row']} at ${b['price']:.2f} costs "
                             f"MORE than row {a['row']} at ${a['price']:.2f}. Further "
                             f"from the ice is not more expensive, so rows look "
                             f"transposed.")

        # Collapse consecutive equal prices into row ranges - the deliverable.
        out = []
        for r in priced:
            band = prices.get(r["price"], [])
            label = band[0] if len(band) == 1 else ("|".join(sorted(band)) if band else None)
            if out and out[-1]["price"] == r["price"] and r["row"] == out[-1]["rowTo"] + 1:
                out[-1]["rowTo"] = r["row"]
            else:
                out.append({"rowFrom": r["row"], "rowTo": r["row"],
                            "price": r["price"], "band": label,
                            "ambiguous": len(band) > 1})
        for e in out:
            if e["ambiguous"]:
                warn.append(f"section {sec} rows {e['rowFrom']}-{e['rowTo']}: "
                            f"${e['price']:.2f} is charged by more than one band "
                            f"({e['band']}) - price alone cannot separate them")
        ranges[sec] = out

    return fatal, warn, ranges


def run(text: str, out_path: pathlib.Path | None) -> int:
    cfg = json.loads(CONFIG.read_text())
    rows, parse_problems = parse(text)
    if not rows:
        print("no parseable rows found", file=sys.stderr)
        for p in parse_problems:
            print(f"  - {p}", file=sys.stderr)
        return 1

    fatal, warn, ranges = grade(rows, cfg)
    fatal = parse_problems + fatal

    print(f"parsed {len(rows)} row(s) across {len(ranges)} section(s)")
    for sec, es in sorted(ranges.items()):
        print(f"  section {sec}:")
        for e in es:
            span = (f"row {e['rowFrom']}" if e["rowFrom"] == e["rowTo"]
                    else f"rows {e['rowFrom']}-{e['rowTo']}")
            print(f"    {span:>14}  ${e['price']:>7.2f}  {e['band'] or '(no band matches)'}")
    for w in warn:
        print(f"  WARN {w}")

    if fatal:
        print(f"\n{len(fatal)} PROBLEM(S) - nothing written:", file=sys.stderr)
        for f in fatal:
            print(f"  - {f}", file=sys.stderr)
        return 1

    if out_path:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps({
            "_comment": ("(section, rowFrom, rowTo) -> price band, derived from a pasted "
                         "row/price table by scripts/check_row_prices.py and graded "
                         "against the published band prices. See ops#31."),
            "sections": ranges,
        }, indent=2) + "\n")
        print(f"\nwrote {out_path.relative_to(ROOT) if out_path.is_relative_to(ROOT) else out_path}")
    else:
        print("\nclean - re-run with --out to store it")
    return 0


def self_test() -> int:
    fails = []

    def check(label, got, want):
        if got != want:
            fails.append(f"{label}: got {got!r}, want {want!r}")

    cfg = json.loads(CONFIG.read_text())
    px = price_index(cfg)
    check("both price columns indexed", 141.0 in px and 148.0 in px, True)
    check("a real price maps to its band", px[92.0], ["Lower 4"])
    check("an invented price maps to nothing", 11111111.0 in px, False)

    rows, probs = parse("110 1 142.00\n110 2 $142\n110 3 -\n")
    check("three rows parsed", len(rows), 3)
    check("no parse problems", probs, [])
    check("dollar sign tolerated", rows[1]["price"], 142.0)
    check("dash means unpriced", rows[2]["price"], None)
    check("comma tolerated", parse("110 1 1,142.00")[0][0]["price"], 1142.0)
    _, probs = parse("garbage line here\n")
    check("unparseable line reported", len(probs), 1)
    check("comments and blanks skipped", parse("# note\n\n")[0], [])

    # A clean paste: two bands in one section, prices falling away from the ice.
    good = "\n".join(["110 1 92", "110 2 92", "110 3 92", "110 4 87", "110 5 87"])
    rows, _ = parse(good)
    fatal, warn, ranges = grade(rows, cfg)
    check("clean paste has no fatal findings", fatal, [])
    check("two row ranges derived", len(ranges["110"]), 2)
    check("first range spans rows 1-3",
          (ranges["110"][0]["rowFrom"], ranges["110"][0]["rowTo"]), (1, 3))
    check("first range names its band", ranges["110"][0]["band"], "Lower 4")
    check("second range names its band", ranges["110"][1]["band"], "Lower 5")

    # A price that matches nothing must be fatal, not silently kept.
    rows, _ = parse("110 1 92\n110 2 93.50")
    fatal, _, _ = grade(rows, cfg)
    check("unmatched price is fatal", any("matches no published band price" in f for f in fatal), True)

    # Rows getting MORE expensive away from the ice means a transposition.
    rows, _ = parse("110 1 87\n110 2 92")
    fatal, _, _ = grade(rows, cfg)
    check("rising price is fatal", any("costs" in f and "MORE" in f for f in fatal), True)

    # A duplicated row.
    rows, _ = parse("110 1 92\n110 1 92")
    fatal, _, _ = grade(rows, cfg)
    check("duplicate row is fatal", any("more than once" in f for f in fatal), True)

    # An unknown section.
    rows, _ = parse("999 1 92")
    fatal, _, _ = grade(rows, cfg)
    check("unknown section is fatal", any("not on any known ring" in f for f in fatal), True)

    # An absent row warns but does not block - it is a hole, not a contradiction.
    rows, _ = parse("110 1 92\n110 3 92")
    fatal, warn, ranges = grade(rows, cfg)
    check("absent row is a warning not fatal", fatal, [])
    check("absent row warned", any("absent entirely" in w for w in warn), True)
    # And it must NOT be collapsed into one range across the hole.
    check("no range spans the hole", len(ranges["110"]), 2)

    # A price two bands share must be reported ambiguous rather than picked.
    shared = [v for v, bs in px.items() if len(bs) > 1]
    if shared:
        rows, _ = parse(f"110 1 {shared[0]}")
        _, warn, ranges = grade(rows, cfg)
        check("shared price flagged ambiguous", ranges["110"][0]["ambiguous"], True)
        check("ambiguity warned", any("more than one band" in w for w in warn), True)

    for f in fails:
        print(f"  FAIL {f}", file=sys.stderr)
    print(f"self-test: {'FAILED' if fails else 'passed'} ({len(fails)} failure(s))")
    return 1 if fails else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--file", type=pathlib.Path)
    ap.add_argument("--out", type=pathlib.Path)
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()
    if args.self_test:
        return self_test()
    text = args.file.read_text() if args.file else sys.stdin.read()
    return run(text, args.out)


if __name__ == "__main__":
    sys.exit(main())
