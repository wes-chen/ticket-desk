"""Measured exceptions to the long-axis mirror oracle (ops#347, decision A).

Only the rink-side GLASS/TEAL corner is exempt. Keep the evidence next to each
pair so a changed chart cannot quietly inherit an unexplained exception.
"""

import re

CHART_SHA256 = "71ffeac28ab60ccb77e7780b8345f3cbbd2ee73ca20eadc4c6bd2e71aaa66ac4"

ASYMMETRIC_SECTION_PAIRS = {
    "rink_corner_104_112": {
        "pair": ("104", "112"),
        "ray_profile": "202-218 degrees: boards -> CLUB 4 at 112; reflected rays: GLASS -> TEAL at every angle at 104",
        "angle_range": (202, 218),
        "chart_hash": CHART_SHA256,
    },
    "rink_corner_106_110": {
        "pair": ("106", "110"),
        "ray_profile": "202-218 degrees: boards -> CLUB 4 at 110; reflected rays: GLASS -> TEAL at every angle at 106",
        "angle_range": (202, 218),
        "chart_hash": CHART_SHA256,
    },
}


def exempt_pairs(entries=ASYMMETRIC_SECTION_PAIRS):
    """Validate evidence before returning pairs; malformed exceptions fail closed."""
    pairs = set()
    for name, entry in entries.items():
        if not isinstance(entry, dict):
            raise ValueError(f"asymmetric pair {name}: expected an evidence record")
        for field in ("pair", "ray_profile", "angle_range", "chart_hash"):
            if field not in entry or not entry[field]:
                raise ValueError(f"asymmetric pair {name}: missing {field}")
        pair, angles = entry["pair"], entry["angle_range"]
        if (len(pair) != 2 or pair[0] == pair[1] or
                len(angles) != 2 or not 0 <= angles[0] < angles[1] <= 360 or
                not isinstance(entry["ray_profile"], str) or
                not re.fullmatch(r"[0-9a-f]{64}", entry["chart_hash"])):
            raise ValueError(f"asymmetric pair {name}: invalid evidence")
        key = frozenset(pair)
        if key in pairs:
            raise ValueError(f"asymmetric pair {name}: duplicate pair")
        pairs.add(key)
    return pairs


def remaining_pairs(pairs, entries=ASYMMETRIC_SECTION_PAIRS):
    excluded = exempt_pairs(entries)
    actual = {frozenset(pair) for pair in pairs}
    unknown = excluded - actual
    if unknown:
        raise ValueError(f"asymmetric pair(s) not in mirror geometry: {unknown}")
    return [pair for pair in pairs if frozenset(pair) not in excluded]


def verify_chart_hash(image_hash, entries=ASYMMETRIC_SECTION_PAIRS):
    exempt_pairs(entries)
    if any(entry["chart_hash"] != image_hash for entry in entries.values()):
        raise ValueError("chart hash differs from asymmetric-pair evidence")


def self_test():
    pairs = [("104", "112"), ("106", "110"), ("101", "115")]
    assert remaining_pairs(pairs) == [("101", "115")]
    for field in ("ray_profile", "angle_range", "chart_hash"):
        broken = {"test": {**ASYMMETRIC_SECTION_PAIRS["rink_corner_104_112"]}}
        del broken["test"][field]
        try:
            exempt_pairs(broken)
        except ValueError as e:
            assert field in str(e)
        else:
            raise AssertionError(f"missing {field} was accepted")
    try:
        remaining_pairs([("101", "115")])
    except ValueError:
        pass
    else:
        raise AssertionError("unknown exemption was accepted")
    verify_chart_hash(CHART_SHA256)
    try:
        verify_chart_hash("0" * 64)
    except ValueError as e:
        assert "chart hash" in str(e)
    else:
        raise AssertionError("changed chart was accepted")
    print("self-test: passed")
    return 0


if __name__ == "__main__":
    # The suite discovers this literal flag in each self-testing script.
    import sys
    if sys.argv[1:] != ["--self-test"]:
        raise SystemExit("use --self-test")
    raise SystemExit(self_test())
