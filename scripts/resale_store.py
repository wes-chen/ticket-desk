#!/usr/bin/env python3
"""Resale listings: one row per (observedDate, gameId, section, row, listingType).

ops#65. `primary_store.py` deliberately holds **primary** inventory only - the team's own
allocation and the member map. The listings that actually undercut us on the resale market
were nowhere, so the single most decision-relevant comparison this project has made (the
ops#20 price recommendation) could not be reproduced from stored data.

## Why a sibling store rather than a column on the primary one

ops#65 left the choice open and named the deciding question: does a resale row ever need
`available` / `hasHigherLevels`? It does not. A resale row is one seat at one price; the
primary store's fields describe a whole section's inventory. Bolting `listingType` onto
that shape would make five of its fields meaningless on half its rows, and a validator
that must skip checks depending on a discriminator stops being able to refuse anything.

## The unit trap, which is the reason this file is careful

**Every price on the public resale page is ALL-IN (includes buyer fees). Every price on
the member map is NOT.** They differ by ~21%, and this project has already made the
ops#20 recommendation wrong once by comparing a list price against an all-in price.

So `allIn` is REQUIRED and never defaulted. A row that does not say which unit it is in is
refused, not assumed - a default would be silently wrong on exactly the rows someone
forgot to think about.

## What must never be written here

This is the PUBLIC repo. A row keyed `(gameId, section, row)` carrying a price is a market
observation about *someone else's* seat. The same row for **our** seat is the linkage
CLAUDE.md rule 1 forbids - it ties our seat to our listing price - and dropping an `isOurs`
flag does not fix it, because section+row is itself the identifier.

Our own listings go to `data/profile/snapshots.jsonl` in the private ops repo. `isOurs` is
not a field here and must not become one; ownership is derived at read time by matching
against the profile, which lives in the browser and in ops.

**Note the guard does not cover this.** `check_privacy.py`'s linkage rule keys on
OWN_PRICE_FIELDS - `list`, `net`, `payout`, and friends - so a field named `price` beside a
`gameId` passes by design, which is what makes every market store legal. The rule above is
therefore load-bearing judgement, not something a check will catch for you.
"""
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from primary_store import SECTIONS, known_game_ids  # noqa: E402  - one source of truth

ROOT = pathlib.Path(__file__).resolve().parent.parent
DEFAULT_STORE = ROOT / "data" / "resale" / "listings.jsonl"

LISTING_TYPES = {"resale", "primary"}

# `row` is a string on purpose. Row labels are not always integers - SAP Center has
# lettered rows in some sections, and ops#65's own ladder carries "21/23" for a pair of
# team-held seats. Coercing to int would silently drop those.
REQUIRED = ("observedDate", "gameId", "section", "row", "price", "allIn", "listingType")


class Invalid(Exception):
    pass


def as_int(x):
    """int(x) or None. Never raises.

    `key()` needs numeric section and gameId, and `validate()` reaches `key()` for
    duplicate detection even on rows it has already found problems with. Casting there
    raised `ValueError` on a non-numeric value - escaping this module's own `Invalid`
    type and contradicting the refuse-rather-than-trust docstring above. Caught in review
    of ops#65; the suite missed it because its bad-section fixture used `999`, which is
    numeric and therefore the wrong KIND of wrong.
    """
    if isinstance(x, bool):  # bools are ints in Python and are never a real id
        return None
    try:
        return int(x)
    except (TypeError, ValueError):
        return None


def key(r):
    """The identity of a row. A same-day re-read UPSERTS rather than appending.

    Assumes section and gameId are numeric - `validate()` skips the duplicate check for
    any row where they are not, rather than crashing here."""
    return (r["observedDate"], int(r["gameId"]), int(r["section"]),
            str(r["row"]), r["listingType"])


def validate(rows, game_ids=None):
    """Every problem with these rows, as a list of strings. Empty means clean.

    Refuses rather than trusts, matching primary_store.validate. Each check is a mistake
    someone makes transcribing a screenshot, and each would otherwise reach a price
    recommendation looking exactly like data.
    """
    problems: list[str] = []
    seen: set[tuple] = set()

    for i, r in enumerate(rows):
        where = f"row {i}"
        missing = [f for f in REQUIRED if f not in r]
        if missing:
            problems.append(f"{where}: missing {', '.join(missing)}")
            continue

        sec, gid = r["section"], r["gameId"]
        where = f"{where} (game {gid} sec {sec} row {r['row']})"

        # Resolve the key fields FIRST. Everything below may append problems, and the
        # duplicate check at the end needs both numeric; a row that fails here is
        # reported and then skipped rather than carried into key().
        sec_i, gid_i = as_int(sec), as_int(gid)
        if sec_i is None:
            problems.append(f"{where}: section must be a number, got {sec!r}")
        elif sec_i not in SECTIONS:
            problems.append(f"{where}: section {sec_i} is not one of the 50 real sections")
        if gid_i is None:
            problems.append(f"{where}: gameId must be a number, got {gid!r}")
        elif game_ids is not None and gid_i not in game_ids:
            problems.append(f"{where}: gameId is not a known home game")
        if r["listingType"] not in LISTING_TYPES:
            problems.append(f"{where}: listingType must be one of "
                            f"{sorted(LISTING_TYPES)}, got {r['listingType']!r}")

        # The unit trap. A bool is required; truthiness is not accepted, because the
        # values that would sneak through - "false", 0, None - are exactly the ones a
        # transcription produces.
        if not isinstance(r["allIn"], bool):
            problems.append(f"{where}: allIn must be a real boolean saying whether the "
                            f"price includes buyer fees, got {r['allIn']!r}. Every public "
                            f"resale price is all-in and every member-map price is not; "
                            f"defaulting it is how the ops#20 comparison went wrong once")

        p = r["price"]
        if not isinstance(p, (int, float)) or isinstance(p, bool) or p <= 0:
            problems.append(f"{where}: price must be a positive number, got {p!r}")

        if not str(r["row"]).strip():
            problems.append(f"{where}: row label is empty - a resale row without a seat "
                            f"is a section summary, which belongs in primary_store")

        # An ownership flag must never exist here. See the module docstring: this is the
        # public repo and the flag would make the row a statement about our seat.
        for banned in ("isOurs", "ours", "mine"):
            if banned in r:
                problems.append(f"{where}: '{banned}' must not be stored in the public "
                                f"repo - ownership is derived at read time from the "
                                f"profile, see CLAUDE.md rule 1")

        if sec_i is None or gid_i is None:
            # Cannot form an identity for this row, so no duplicate check. Every other
            # problem with it has already been recorded above, and crashing here is what
            # review of ops#65 caught.
            continue
        k = key(r)
        if k in seen:
            problems.append(f"{where}: duplicate of an earlier row with the same "
                            f"(observedDate, gameId, section, row, listingType)")
        seen.add(k)

    return problems


def merge(existing, new):
    """UPSERT by key, then sort deterministically so a daily commit diffs cleanly."""
    by = {key(r): r for r in existing}
    for r in new:
        by[key(r)] = r
    return sorted(by.values(),
                  key=lambda r: (r["observedDate"], int(r["gameId"]), int(r["section"]),
                                 str(r["row"]), r["listingType"]))


def read_store(path=DEFAULT_STORE):
    if not path.exists():
        return []
    out = []
    for n, line in enumerate(path.read_text().splitlines(), 1):
        if not line.strip():
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError as e:
            # An unreadable line is an ERROR, never silently skipped data. This project
            # has produced the same silent-truncation bug three times.
            raise Invalid(f"{path}:{n} is not valid JSON: {e}") from e
    return out


def write_store(rows, path=DEFAULT_STORE):
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "\n".join(json.dumps(r, sort_keys=True) for r in rows)
    path.write_text(body + "\n" if body else "")


def ingest(new_rows, path=DEFAULT_STORE, game_ids=None):
    """Validate then write. Writes NOTHING if any row has a problem."""
    problems = validate(new_rows, game_ids=game_ids)
    if problems:
        raise Invalid("; ".join(problems))
    merged = merge(read_store(path), new_rows)
    write_store(merged, path)
    return merged


def main(argv):
    path = DEFAULT_STORE
    rows = read_store(path)
    if not rows:
        print(f"NOT CHECKED - {path.relative_to(ROOT)} is empty or absent")
        print("  ops#65 ingest is pending the gameId/observedDate for its ladder")
        return 0
    problems = validate(rows, game_ids=known_game_ids())
    print(f"{len(rows)} resale row(s) in {path.relative_to(ROOT)}")
    games = sorted({r["gameId"] for r in rows})
    for g in games:
        n = sum(1 for r in rows if r["gameId"] == g)
        print(f"  game {g}: {n} row(s)")
    if problems:
        print(f"{len(problems)} PROBLEM(S):")
        for p in problems:
            print(f"  - {p}")
        return 1
    print("clean")
    return 0


def self_test():
    import tempfile
    fails = []

    def check(name, got, want):
        if got != want:
            fails.append(f"{name}: got {got!r}, want {want!r}")

    def row(**kw):
        base = {"observedDate": "2026-09-07", "gameId": 2026010032, "section": 110,
                "row": "20", "price": 64.13, "allIn": True, "listingType": "resale"}
        base.update(kw)
        return base

    check("a good row is clean", validate([row()]), [])

    # allIn is the unit trap and the reason this store exists as its own file.
    check("allIn missing is refused", validate([{k: v for k, v in row().items()
                                                 if k != "allIn"}]),
          ["row 0: missing allIn"])
    got = validate([row(allIn="true")])
    check("allIn as a string is refused", len(got), 1)
    check("and explains the unit trap", "includes buyer fees" in got[0], True)
    # 0/1 are truthy-correct and still refused - they are what a transcription produces.
    check("allIn as 0 is refused", len(validate([row(allIn=0)])), 1)

    # An ownership flag must never reach the public store.
    got = validate([row(isOurs=True)])
    check("isOurs is refused", len(got), 1)
    check("and cites the rule", "CLAUDE.md rule 1" in got[0], True)

    check("bad section refused", len(validate([row(section=999)])), 1)

    # ops#65 review, F2. validate() reached key(), which cast int(), and RAISED on a
    # non-numeric value instead of refusing - escaping this module's own Invalid type.
    # The existing bad-section fixture used 999: numeric, and therefore the wrong KIND of
    # wrong. These assert refusal, and would raise rather than fail if it regressed.
    check("a non-numeric gameId is refused, not raised",
          len(validate([row(gameId="VGK")])), 1)
    check("a non-numeric section is refused, not raised",
          len(validate([row(section="110A")])), 1)
    check("a null section is refused, not raised",
          len(validate([row(section=None)])), 1)
    # Bools are ints in Python and are never a real id.
    check("a boolean gameId is refused", len(validate([row(gameId=True)])), 1)
    # A numeric string is a transcription artefact, not an error - accept it.
    check("a numeric-string section is accepted", validate([row(section="110")]), [])
    # Two unkeyable rows must not collide in the duplicate check either.
    check("two unkeyable rows each report once",
          len(validate([row(gameId="VGK"), row(gameId="ANA")])), 2)
    check("bad listingType refused", len(validate([row(listingType="auction")])), 1)
    check("zero price refused", len(validate([row(price=0)])), 1)
    check("boolean price refused", len(validate([row(price=True)])), 1)
    check("empty row label refused", len(validate([row(row="  ")])), 1)
    check("unknown game refused",
          len(validate([row(gameId=999)], game_ids={2026010032})), 1)

    # A non-integer row label must survive: ops#65's own ladder carries "21/23".
    check("a compound row label is fine", validate([row(row="21/23")]), [])

    # THE acceptance criterion of ops#65: a primary row and a resale row for the same
    # seat must coexist. They differ only by listingType, so this is what the key buys.
    both = [row(listingType="resale", price=64.13),
            row(listingType="primary", price=82.35)]
    check("same seat, both listing types, is valid", validate(both), [])
    check("and both survive the merge", len(merge([], both)), 2)

    # Same key twice in one batch is a transcription duplicate, not an upsert.
    check("duplicate within a batch refused", len(validate([row(), row()])), 1)

    # Across batches the same key UPSERTS - a re-read corrects rather than appends.
    merged = merge([row(price=64.13)], [row(price=61.00)])
    check("re-read upserts", len(merged), 1)
    check("and keeps the newer price", merged[0]["price"], 61.00)

    # ingest writes NOTHING when any row is bad, even if others are fine.
    tmp = pathlib.Path(tempfile.mkdtemp()) / "listings.jsonl"
    try:
        ingest([row(), row(row="19", allIn=None)], path=tmp)
        fails.append("ingest should have raised on a bad row")
    except Invalid:
        pass
    check("nothing written on refusal", tmp.exists(), False)

    ingest([row()], path=tmp)
    check("a clean ingest writes", len(read_store(tmp)), 1)
    # Round-trip: what was written reads back identically.
    check("round-trips", read_store(tmp)[0]["price"], 64.13)

    # A corrupt line is an ERROR, never silently skipped.
    tmp.write_text('{"observedDate":"2026-09-07"}\nnot json\n')
    try:
        read_store(tmp)
        fails.append("a corrupt line should raise")
    except Invalid:
        pass

    for f in fails:
        print("FAIL", f)
    print(f"resale_store self-test: {'PASS' if not fails else str(len(fails)) + ' FAILURES'}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(self_test() if "--self-test" in sys.argv[1:] else main(sys.argv[1:]))
