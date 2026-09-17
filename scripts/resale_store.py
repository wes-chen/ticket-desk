#!/usr/bin/env python3
"""Resale listings: one row per (observedDate, gameId, band, seq, listingType).

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

## The fee-regime trap (ops#76)

**`listingType: primary` and `listingType: resale` do not carry the same buyer fee.**
Measured 2026-09-07 on the three rows this store already holds for game 2026010032:
the two resale rows (71.39, 64.13 all-in) both invert to a whole-dollar list price at
exactly 21.00% buyer fee, while the primary row (82.35 all-in, "Standard Ticket") does
not - it needs 21.10% to reach a whole $68.00. See `buyerFeeRate` vs
`primaryBuyerFeeRate` in `config/economics.json`.

So: **never compare `price` across `listingType` and read the gap as a market
signal without first converting through the fee that actually applies to each row.**
A primary-vs-resale delta is partly measuring two different fee structures, not
only demand. Nothing in this codebase does that comparison today (grepped
2026-09-13); the risk is that something will, since both listing types now live in
one file with one `price` column that invites exactly this diff.

## Why the schema is keyed on a band rather than on a seat

Rule 1 forbids seat section and row numbers in any form, and a scoped comp store is
narrow by construction, so a seat key makes its scope legible. `band` is not a locator,
and it is the unit a comp is actually about - you price against seats like yours rather
than against one specific section. `seq` restores per-row uniqueness without one.

The residual is that this store discloses a band, and that was already public: ops#167
publishes the per-seat season face, and a face resolves to exactly one band. It is not
the whole picture - see ops#192, which is open.

**`seq` is the price-ascending rank within `(observedDate, gameId, band, listingType)`,
and it is derived rather than chosen** so that a same-day re-read UPSERTS rather than
appends. An arbitrary counter would have made identity depend on capture order, which is
the one thing a re-read does not preserve.

`band` is a `config/price_bands.json` band **id**, validated against that file, so a typo
is refused rather than stored. Note what this cannot do: every `sections` array in that
file is empty until ops#19 lands, so band is NOT mechanically derivable from a section
here - whoever ingests a row establishes it and says how, via `bandBasis`.

## bandBasis: measured or inferred, per rule 4

`measured` means the band label was read directly for that seat - the public event page
renders the chart's own legend label in each seat's Description field. `inferred` means it
was derived from something adjacent, such as a neighbouring row's label or a price break.
The three rows this store was migrated with are two `inferred` and one `measured`, and
flattening that distinction is what rule 4 exists to stop.

## What must never be written here

`isOurs`, `ours`, `mine`, and now `section`, `row`, `seat` are refused outright. Ownership
is derived at read time by matching against the profile, which lives in the private ops
repo. Our own listings go to `ops:data/profile/snapshots.jsonl`.

**Note the guard does not cover this.** `check_privacy.py`'s linkage rule keys on
OWN_PRICE_FIELDS - `list`, `net`, `payout`, and friends - so a field named `price` beside a
`gameId` passes by design, which is what makes every market store legal. Nor is a literal
pass any help: the section number appears legitimately in the 50-section stores, so adding
it to `.private-patterns` would fail the build on data that is fine. The refusal list
below is the enforcement, and it is why these field names are rejected by NAME rather than
left to judgement.
"""
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from primary_store import known_game_ids  # noqa: E402  - one source of truth

ROOT = pathlib.Path(__file__).resolve().parent.parent
DEFAULT_STORE = ROOT / "data" / "resale" / "listings.jsonl"
ECONOMICS = ROOT / "config" / "economics.json"
PRICE_BANDS = ROOT / "config" / "price_bands.json"

LISTING_TYPES = {"resale", "primary"}

def band_ids():
    """The band ids `band` is validated against - read from config, never duplicated here.

    A second copy of this list would drift from price_bands.json silently, and the whole
    point of validating the field is that a typo cannot reach the store.
    """
    return {b["id"] for b in json.loads(PRICE_BANDS.read_text())["bands"]}


# Fields this store REFUSES by name, all seven of them. `isOurs`, `ours` and `mine` have
# always been banned: an ownership flag makes a row a statement about our seat. `section`,
# `row`, `seat` and `seatNumber` were added in ops#189 - see the schema section of the
# module docstring. They are refused rather than merely unused, because "do not add a
# section column" is not something a future ingest can be expected to infer from the
# absence of one. If you add a name here, add a case to the self-test loop too: that loop
# tested four of these seven until a mutation sweep pointed it out.
BANNED = ("isOurs", "ours", "mine", "section", "row", "seat", "seatNumber")

BAND_BASIS = {"measured", "inferred"}

REQUIRED = ("observedDate", "gameId", "band", "seq", "price", "allIn", "listingType",
            "bandBasis")


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

    Assumes gameId and seq are numeric - `validate()` skips the duplicate check for any
    row where they are not, rather than crashing here."""
    return (r["observedDate"], int(r["gameId"]), r["band"], int(r["seq"]),
            r["listingType"])


def validate(rows, game_ids=None):
    """Every problem with these rows, as a list of strings. Empty means clean.

    Refuses rather than trusts, matching primary_store.validate. Each check is a mistake
    someone makes transcribing a screenshot, and each would otherwise reach a price
    recommendation looking exactly like data.
    """
    problems: list[str] = []
    seen: set[tuple] = set()
    known_bands = band_ids()

    for i, r in enumerate(rows):
        where = f"row {i}"
        missing = [f for f in REQUIRED if f not in r]
        if missing:
            problems.append(f"{where}: missing {', '.join(missing)}")
            continue

        band, gid = r["band"], r["gameId"]
        where = f"{where} (game {gid} band {band} seq {r['seq']})"

        # Resolve the key fields FIRST. Everything below may append problems, and the
        # duplicate check at the end needs both numeric; a row that fails here is
        # reported and then skipped rather than carried into key().
        seq_i, gid_i = as_int(r["seq"]), as_int(gid)
        if not isinstance(band, str) or band not in known_bands:
            problems.append(f"{where}: band must be one of the {len(known_bands)} ids in "
                            f"config/price_bands.json, got {band!r}")
        if seq_i is None or seq_i < 1:
            problems.append(f"{where}: seq must be a positive whole number - it is the "
                            f"price-ascending rank within (observedDate, gameId, band, "
                            f"listingType), got {r['seq']!r}")
        if r["bandBasis"] not in BAND_BASIS:
            problems.append(f"{where}: bandBasis must be one of {sorted(BAND_BASIS)}, got "
                            f"{r['bandBasis']!r}. Rule 4: a band read directly off the "
                            f"seat is not the same claim as one derived from a "
                            f"neighbouring row, and flattening them invents precision")
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

        # Refused by NAME, not left to judgement. An ownership flag makes the row a
        # statement about our seat; a section or row column re-creates the one-section
        # scoping that ops#189 removed, and no privacy check can catch either - see the
        # module docstring on why a literal pass cannot help here.
        for banned in BANNED:
            if banned in r:
                problems.append(f"{where}: '{banned}' must not be stored in the public "
                                f"repo - this store is keyed on band + seq and carries no "
                                f"seat locator (ops#189); ownership is derived at read "
                                f"time from the profile, see CLAUDE.md rule 1")

        if seq_i is None or gid_i is None or not isinstance(band, str):
            # Cannot form an identity for this row, so no duplicate check. Every other
            # problem with it has already been recorded above, and crashing here is what
            # review of ops#65 caught - then caught again in review of ops#189, because a
            # band of an UNHASHABLE type raised inside key() where a string that merely
            # is not a known id does not. Type, not membership, is what decides this.
            continue
        k = key(r)
        if k in seen:
            problems.append(f"{where}: duplicate of an earlier row with the same "
                            f"(observedDate, gameId, band, seq, listingType)")
        seen.add(k)

    # seq is the price-ascending rank within its group, not a free counter. Checked here
    # rather than trusted, because the whole reason it is derived is that a same-day
    # re-read must land on the same identity; a hand-typed counter would not.
    #
    # A group that LOST a row to any check above is not rank-checkable, and is skipped
    # whole. Ranking a partial group blames the survivors for their neighbour's problem:
    # drop one row of a clean pair and the other is suddenly "seq 2 where 1 was expected",
    # which is an artefact of the drop and points at the wrong row. Found by a fixture
    # written for a different mutant.
    groups: dict[tuple, list] = {}
    incomplete: set[tuple] = set()

    def group_key(r):
        """The group a row ranks within, or None if that cannot be determined."""
        gid = as_int(r.get("gameId"))
        band_v, od, lt = r.get("band"), r.get("observedDate"), r.get("listingType")
        if gid is None or not isinstance(band_v, str) or not isinstance(od, str):
            return None
        if not isinstance(lt, str):
            return None
        return (od, gid, band_v, lt)

    for r in rows:
        gk = group_key(r)
        seq_v = as_int(r.get("seq"))
        usable = (
            all(f in r for f in REQUIRED)
            and gk is not None
            and seq_v is not None and seq_v >= 1
            and isinstance(r.get("price"), (int, float))
            and not isinstance(r.get("price"), bool)
        )
        if not usable:
            if gk is not None:
                incomplete.add(gk)
            continue
        groups.setdefault(gk, []).append(r)

    for gk, grp in sorted(groups.items(), key=lambda kv: str(kv[0])):
        if gk in incomplete:
            continue
        od, gid, band_v, lt = gk
        want = {id(r): i for i, r in
                enumerate(sorted(grp, key=lambda r: (r["price"], as_int(r["seq"]))), 1)}
        for r in grp:
            if as_int(r["seq"]) != want[id(r)]:
                problems.append(
                    f"game {gid} band {band_v} ({lt}, {od}): seq {r['seq']} at price "
                    f"{r['price']} should be {want[id(r)]} - seq is the price-ascending "
                    f"rank within its group, so a re-read upserts instead of appending")

    return problems


def merge(existing, new):
    """UPSERT by key, then sort deterministically so a daily commit diffs cleanly."""
    by = {key(r): r for r in existing}
    for r in new:
        by[key(r)] = r
    return sorted(by.values(),
                  key=lambda r: (r["observedDate"], int(r["gameId"]), r["band"],
                                 int(r["seq"]), r["listingType"]))


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


def buyer_fee_problems(econ):
    """ops#76: every cited (list-ish, all-in) observation must round-trip to the rate
    it supposedly measured, to the cent.

    This is what makes 'measured' honest. `buyerFeeRate` and `primaryBuyerFeeRate` in
    config/economics.json each carry a `value` and a `buyerFeeObservations` list that is
    the evidence for it. If someone edits the rate without touching the observations (or
    vice versa), the pairs stop inverting and this catches it - the acceptance criterion
    ops#76 asked for: "a test fails if buyerFeeRate is changed without the observations
    being updated." Resale observations key off `list`; the primary observation keys off
    `impliedFaceValue` since a primary row has no seller-chosen list price - see
    primaryBuyerFeeRate's own docstring in economics.json for why.
    """
    problems = []
    tm = econ["resale"]["platforms"]["ticketmaster"]

    def check_block(rate, observations, list_key, label):
        for obs in observations:
            base = obs[list_key]
            expected = round(base * (1 + rate), 2)
            got = obs["allInBuyerPrice"]
            if abs(expected - got) > 0.005:
                problems.append(
                    f"{label}: {list_key}={base} at rate {rate} implies all-in "
                    f"{expected}, but the stored observation says {got}"
                )

    # `buyerFeeObservations` for the RESALE rate is a sibling of buyerFeeRate (matches
    # the pre-existing shape); the PRIMARY rate nests its own observations under itself,
    # since it was added later and there is only ever one platform's worth of it.
    check_block(tm["buyerFeeRate"]["value"], tm["buyerFeeObservations"], "list",
                "buyerFeeRate")
    check_block(tm["primaryBuyerFeeRate"]["value"],
                tm["primaryBuyerFeeRate"]["buyerFeeObservations"], "impliedFaceValue",
                "primaryBuyerFeeRate")
    return problems


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

    def refused(name, rows, want=1, ids=None):
        """Assert a bad row is REFUSED, never raised - and report a raise by NAME.

        validate() raising escapes this module's own Invalid type; that is the ops#65 F2
        defect, and it recurred on an unhashable band in review of ops#189. A bare
        `len(validate([...]))` cannot express it: the exception propagates out of
        self_test, aborting before any FAIL prints and skipping every later assertion.
        The mutation sweep scored five guards that way - red, but proving nothing, which
        is ops#191's crash-kill. This turns the raise into the finding.
        """
        try:
            got = validate(rows, game_ids=ids)
        except Exception as e:                                  # noqa: BLE001
            fails.append(f"{name}: validate() RAISED {type(e).__name__}: {e} - it must "
                         f"refuse a bad row, never raise (ops#65 F2)")
            return
        if len(got) != want:
            fails.append(f"{name}: got {len(got)} problem(s), want {want}: {got!r}")

    def first(got):
        """first(got), or "" when empty. An assertion must not RAISE when a guard is deleted.

        A 22-mutant sweep in review of ops#189 found ten guards whose deletion emptied
        `got`, so the very next `... in first(got)` raised IndexError inside this function -
        aborting the suite before a single FAIL printed and skipping every later
        assertion. The runner judges on return code, so each one still went red and was
        banked as a kill while proving nothing about coverage. That is ops#191's
        crash-kill, and it is the same defect as a shell suite running under `set -e`.
        """
        return got[0] if got else ""

    def row(**kw):
        # Absurd values throughout (CLAUDE.md rule 1: plausible means real). The band is
        # the one field that cannot be absurd - an invented id fails the band check and
        # would never exercise anything downstream, the same constraint check_row_prices
        # has - so a real id is used that is deliberately NOT the one this store holds.
        base = {"observedDate": "1999-01-01", "gameId": 11111111, "band": "upper-goal-2",
                "seq": 1, "price": 11111111.0, "allIn": True, "listingType": "resale",
                "bandBasis": "measured"}
        base.update(kw)
        return base

    check("a good row is clean", validate([row()]), [])

    # allIn is the unit trap and the reason this store exists as its own file.
    got = None
    try:
        got = validate([{k: v for k, v in row().items() if k != "allIn"}])
    except Exception as e:                                       # noqa: BLE001
        fails.append(f"allIn missing is refused: validate() RAISED {type(e).__name__}: "
                     f"{e} - a row missing a REQUIRED field must be refused, not raised")
    if got is not None:
        check("allIn missing is refused", got, ["row 0: missing allIn"])
    got = validate([row(allIn="true")])
    check("allIn as a string is refused", len(got), 1)
    check("and explains the unit trap", "includes buyer fees" in first(got), True)
    # 0/1 are truthy-correct and still refused - they are what a transcription produces.
    check("allIn as 0 is refused", len(validate([row(allIn=0)])), 1)

    # An ownership flag must never reach the public store.
    got = validate([row(isOurs=True)])
    check("isOurs is refused", len(got), 1)
    check("and cites the rule", "CLAUDE.md rule 1" in first(got), True)

    got = validate([row(band="not-a-band")])
    check("an unknown band is refused", len(got), 1)
    check("and points at price_bands.json", "price_bands.json" in first(got), True)

    # ops#189: the fields this store must never carry again. Refused by NAME, and the
    # loop runs over BANNED itself rather than a hand-copied subset - a mutation sweep
    # found this testing four of the seven, so three names were refused by code nothing
    # exercised. Adding a name to BANNED now adds a case here automatically.
    check("every BANNED name is covered by this loop", len(BANNED), 7)
    for banned in BANNED:
        got = validate([row(**{banned: 11111111})])
        check(f"'{banned}' is refused", len(got), 1)
        check(f"and '{banned}' cites the rule", "CLAUDE.md rule 1" in first(got), True)

    # REGRESSION (review of ops#189). An unhashable band reached key() and raised
    # TypeError, escaping this module's own Invalid type - the ops#65 F2 bug again. The
    # fixture that first replaced the old one used None, which is HASHABLE, so it was the
    # wrong kind of wrong twice over. Type is what decides the skip, not membership.
    for bad_band in ([], {}, set()):
        refused(f"an unhashable band ({type(bad_band).__name__}) is refused, not raised",
                [row(band=bad_band)])
    refused("a non-string band is refused, not raised", [row(band=7)])
    check("a string band that is merely unknown is still key-able",
          len(validate([row(band="not-a-band"), row(band="not-a-band", seq=2,
                            price=22222222.0)])), 2)

    check("a non-measured bandBasis is refused",
          len(validate([row(bandBasis="probably")])), 1)
    check("bandBasis 'inferred' is accepted", validate([row(bandBasis="inferred")]), [])
    check("seq 0 is refused", len(validate([row(seq=0)])), 1)
    refused("a non-numeric seq is refused, not raised", [row(seq="first")])

    # ops#65 review, F2. validate() reached key(), which cast int(), and RAISED on a
    # non-numeric value instead of refusing - escaping this module's own Invalid type.
    # The existing bad-section fixture used 999: numeric, and therefore the wrong KIND of
    # wrong. These assert refusal, and would raise rather than fail if it regressed.
    refused("a non-numeric gameId is refused, not raised", [row(gameId="VGK")])
    refused("a null band is refused, not raised", [row(band=None)])
    # Bools are ints in Python and are never a real id.
    check("a boolean gameId is refused", len(validate([row(gameId=True)])), 1)
    # A numeric string is a transcription artefact, not an error - accept it.
    check("a numeric-string seq is accepted", validate([row(seq="1")]), [])
    # Two unkeyable rows must not collide in the duplicate check either.
    check("two unkeyable rows each report once",
          len(validate([row(gameId="VGK"), row(gameId="ANA")])), 2)
    check("bad listingType refused", len(validate([row(listingType="auction")])), 1)
    check("zero price refused", len(validate([row(price=0)])), 1)
    check("boolean price refused", len(validate([row(price=True)])), 1)

    check("unknown game refused",
          len(validate([row(gameId=999)], game_ids={2026010032})), 1)

    # THE acceptance criterion of ops#65: a primary row and a resale row for the same
    # seat must coexist. They differ only by listingType, so this is what the key buys.
    both = [row(listingType="resale", price=11111111.0),
            row(listingType="primary", price=22222222.0)]
    check("same band, both listing types, is valid", validate(both), [])
    check("and both survive the merge", len(merge([], both)), 2)

    # Same key twice in one batch is a transcription duplicate, not an upsert. Asserted
    # on the MESSAGE: the seq-rank check fires on the second row too, and a count of 1
    # here would quietly start failing for the right reason and look like a regression.
    got = validate([row(), row()])
    check("duplicate within a batch refused",
          any("duplicate of an earlier row" in g for g in got), True)

    # The seq-rank invariant itself: a counter that does not follow price order is what
    # makes a re-read append instead of upsert.
    got = validate([row(seq=1, price=22222222.0), row(seq=2, price=11111111.0)])
    check("seq out of price order is refused", len(got), 2)
    check("and says what seq means", "price-ascending rank" in first(got), True)
    check("seq in price order is clean",
          validate([row(seq=1, price=11111111.0), row(seq=2, price=22222222.0)]), [])

    # Tied prices: the rank is broken by seq, so EITHER assignment is consistent. Asserted
    # rather than left implicit - this is the one case where the invariant cannot pick a
    # winner, and the PR that introduced seq led with the invariant and never tested it.
    check("tied prices are clean in seq order",
          validate([row(seq=1, price=11111111.0), row(seq=2, price=11111111.0)]), [])
    check("tied prices are clean in the other seq order",
          validate([row(seq=2, price=11111111.0), row(seq=1, price=11111111.0)]), [])

    # The rank loop must SKIP rows already refused above rather than pile a second,
    # confusing finding onto them. One assertion per skip condition it implements.
    refused("a row with a non-numeric price is refused once", [row(price="cheap")])
    # TWO rows, so the rank loop actually has to COMPARE the prices. With one row sorted()
    # never compares and the guard is unreachable - which is why this mutant survived the
    # first sweep: the fixture could not reach the code it was meant to cover.
    refused("a non-numeric price among siblings is refused, not raised",
            [row(price="cheap"), row(seq=2, price=22222222.0)])
    for gone in REQUIRED:
        refused(f"a row missing '{gone}' is refused once, not raised",
                [{k: v for k, v in row().items() if k != gone}])
    refused("a row with an unknown gameId is refused once", [row(gameId=999)],
            ids={11111111})
    refused("a REQUIRED-incomplete row makes its whole group unrankable",
            [{k: v for k, v in row(seq=1, price=22222222.0).items() if k != "bandBasis"},
             row(seq=2, price=11111111.0)])

    # Across batches the same key UPSERTS - a re-read corrects rather than appends.
    merged = merge([row(price=11111111.0)], [row(price=22222222.0)])
    check("re-read upserts", len(merged), 1)
    check("and keeps the newer price", merged[0]["price"], 22222222.0)

    # ingest writes NOTHING when any row is bad, even if others are fine.
    tmp = pathlib.Path(tempfile.mkdtemp()) / "listings.jsonl"
    try:
        ingest([row(), row(seq=2, price=22222222.0, allIn=None)], path=tmp)
        fails.append("ingest should have raised on a bad row")
    except Invalid:
        pass
    check("nothing written on refusal", tmp.exists(), False)

    ingest([row()], path=tmp)
    check("a clean ingest writes", len(read_store(tmp)), 1)
    # Round-trip: what was written reads back identically.
    check("round-trips", read_store(tmp)[0]["price"], 11111111.0)

    # A corrupt line is an ERROR, never silently skipped.
    tmp.write_text('{"observedDate":"2026-09-07"}\nnot json\n')
    try:
        read_store(tmp)
        fails.append("a corrupt line should raise")
    except Invalid:
        pass

    # ops#76: the REAL config/economics.json, as committed, must round-trip. This is
    # against the actual captured observations (85.00->102.85 our own listing;
    # 59.00->71.39 and 53.00->64.13 the two OTHER-seller resale rows; 68.00->82.35 the
    # primary row), not invented fixtures.
    real_econ = json.loads(ECONOMICS.read_text())
    check("the real economics.json buyer-fee observations all round-trip",
          buyer_fee_problems(real_econ), [])

    # And a later edit to the rate WITHOUT updating the observations must fail loudly -
    # the acceptance criterion ops#76 asked for. Mutate a deep copy so the real file on
    # disk is untouched.
    import copy
    drifted = copy.deepcopy(real_econ)
    drifted["resale"]["platforms"]["ticketmaster"]["buyerFeeRate"]["value"] = 0.25
    check("a drifted resale rate is caught",
          len(buyer_fee_problems(drifted)) > 0, True)

    drifted2 = copy.deepcopy(real_econ)
    drifted2["resale"]["platforms"]["ticketmaster"]["primaryBuyerFeeRate"]["value"] = 0.30
    check("a drifted primary rate is caught",
          len(buyer_fee_problems(drifted2)) > 0, True)

    for f in fails:
        print("FAIL", f)
    print(f"resale_store self-test: {'PASS' if not fails else str(len(fails)) + ' FAILURES'}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(self_test() if "--self-test" in sys.argv[1:] else main(sys.argv[1:]))
