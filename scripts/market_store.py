#!/usr/bin/env python3
"""Shared store for per-event price series. Used by every market collector.

Extracted when a second source (Gametime) arrived alongside TickPick. The two
collectors differ only in how they find events and parse a page; the storage
semantics are identical and subtle enough that two copies would drift:

  * one row per event per UTC day, UPSERTED on (observedDate, eventId) - so a
    same-day re-run corrects that day instead of appending
  * rows sorted deterministically, so a daily commit produces a readable diff
  * a read that hits its size cap is an ERROR, never data

That last rule is here because this project has produced the same silent-truncation
bug three times: a probe capped at 400KB scored a working 1.4MB source as empty, and a
5MB cap on a 7.4MB sitemap reported 39 of 44 games as a genuine coverage gap.
"""

import json
import pathlib
import sys
import urllib.error
import urllib.request

MAX_BODY_PAGE = 8_000_000
MAX_BODY_SITEMAP = 40_000_000
TIMEOUT = 45

# An ordinary desktop Chrome UA. Not a disguise - it is what a normal client sends. The
# politeness lever is request VOLUME, which is a few dozen a day.
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")


def rel(path: pathlib.Path, root: pathlib.Path) -> str:
    """Display path, tolerating a store outside the repo (scratch dirs, smoke tests)."""
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def get(url: str, max_body: int = MAX_BODY_PAGE) -> tuple[str | None, str | None]:
    """Fetch a page. Returns (body, error) - exactly one is None."""
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    })
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            # One byte past the cap, so hitting it is detectable rather than silent.
            data = r.read(max_body + 1)
    except urllib.error.HTTPError as e:
        return None, f"http {e.code}"
    except Exception as e:  # noqa: BLE001
        return None, f"{type(e).__name__}: {e}"
    if len(data) > max_body:
        return None, (f"response exceeded {max_body:,}B cap and was truncated - raise the "
                      f"cap rather than treating a partial body as data")
    return data.decode("utf-8", "ignore"), None


def read_store(path: pathlib.Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]


def merge(existing: list[dict], new: list[dict]) -> list[dict]:
    """Upsert on (observedDate, eventId), then sort deterministically."""
    keyed = {(r["observedDate"], r["eventId"]): r for r in existing}
    for r in new:
        keyed[(r["observedDate"], r["eventId"])] = r
    return sorted(keyed.values(),
                  key=lambda r: (r["observedDate"], r.get("date", ""), str(r["eventId"])))


def write_store(path: pathlib.Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in rows))


def commit_rows(store: pathlib.Path, rows: list[dict], total_failure: bool) -> list[dict]:
    """Write unless every fetch failed.

    Partial failures ARE stored - a per-event error row is real gap information, and
    dropping it makes a missing day indistinguishable from a day the source published
    nothing. A TOTAL failure means the source or our access changed, not that the market
    went quiet, so writing dozens of identical error rows a day would bury the series.
    """
    if total_failure:
        return read_store(store)
    merged = merge(read_store(store), rows)
    write_store(store, merged)
    return merged


def self_test() -> int:
    import tempfile

    fails = []

    def check(label, got, want):
        if got != want:
            fails.append(f"{label}: got {got!r}, want {want!r}")

    def row(day, ev, low, date="2026-10-01"):
        return {"observedDate": day, "eventId": ev, "date": date, "low": low}

    m = merge([row("2026-09-05", "E1", 40)], [row("2026-09-05", "E1", 45)])
    check("same-day rerun upserts", len(m), 1)
    check("same-day rerun takes the new value", m[0]["low"], 45)

    m = merge(m, [row("2026-09-06", "E1", 50)])
    check("a new day appends", len(m), 2)
    check("sorted by observedDate", [r["observedDate"] for r in m],
          ["2026-09-05", "2026-09-06"])

    # Deterministic ordering within a day, so daily commits diff cleanly.
    a = merge([], [row("2026-09-05", "B", 1, "2026-10-02"), row("2026-09-05", "A", 1, "2026-10-01")])
    b = merge([], [row("2026-09-05", "A", 1, "2026-10-01"), row("2026-09-05", "B", 1, "2026-10-02")])
    check("ordering is independent of input order", a, b)

    # Numeric event ids must not crash the sort by mixing types with strings.
    mixed = merge([], [row("2026-09-05", 2, 1), row("2026-09-05", "10", 1)])
    check("mixed id types sort without error", len(mixed), 2)

    with tempfile.TemporaryDirectory() as d:
        p = pathlib.Path(d) / "market" / "s.jsonl"
        write_store(p, m)
        check("round-trips through jsonl", read_store(p), m)
        check("one line per row", len(p.read_text().strip().splitlines()), 2)
        check("missing file reads empty", read_store(pathlib.Path(d) / "nope.jsonl"), [])

        # commit_rows: a total failure must leave the store untouched.
        before = read_store(p)
        check("total failure writes nothing",
              commit_rows(p, [row("2026-09-07", "E1", 99)], True), before)
        check("store unchanged on disk", read_store(p), before)
        # A partial failure stores its rows, error rows included.
        after = commit_rows(p, [{"observedDate": "2026-09-07", "eventId": "E1",
                                 "date": "2026-10-01", "ok": False, "error": "http 500"}], False)
        check("partial failure stores the error row", len(after), 3)
        check("error row persisted", read_store(p)[-1]["error"], "http 500")

    check("rel inside root", rel(pathlib.Path("/a/b/c.txt"), pathlib.Path("/a")), "b/c.txt")
    check("rel outside root", rel(pathlib.Path("/x/c.txt"), pathlib.Path("/a")), "/x/c.txt")

    for f in fails:
        print(f"  FAIL {f}", file=sys.stderr)
    print(f"self-test: {'FAILED' if fails else 'passed'} ({len(fails)} failure(s))")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(self_test() if "--self-test" in sys.argv else 0)
