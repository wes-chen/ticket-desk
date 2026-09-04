#!/usr/bin/env python3
"""Generate PWA icons without any image library.

Neither PIL nor ImageMagick is available here, and pulling in a dependency to draw
three rectangles would be silly. PNG is simple enough to emit directly: signature,
IHDR, one zlib-compressed IDAT of filter-0 scanlines, IEND.

Design: teal field, three ascending bars. Content sits inside the middle ~60% so the
same image works as a maskable icon without being clipped on Android.
"""

import pathlib
import struct
import zlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT = ROOT / "public"

BG = (0, 109, 117)  # teal-sharks #006D75
FG = (255, 255, 255)


def chunk(tag: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + tag
        + data
        + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    )


def render(size: int) -> bytes:
    m = size * 0.22           # margin -> content in the middle 56%
    baseline = size - m
    bar_w = size * 0.13
    gap = size * 0.055
    heights = [0.26, 0.42, 0.56]
    bars = []
    for i, h in enumerate(heights):
        x0 = m + i * (bar_w + gap)
        bars.append((x0, x0 + bar_w, baseline - size * h, baseline))

    rows = bytearray()
    for y in range(size):
        rows.append(0)  # filter type 0 (None)
        for x in range(size):
            px = BG
            for x0, x1, y0, y1 in bars:
                if x0 <= x < x1 and y0 <= y < y1:
                    px = FG
                    break
            rows.extend(px)

    ihdr = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)  # 8-bit RGB
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(bytes(rows), 9))
        + chunk(b"IEND", b"")
    )


def main():
    OUT.mkdir(exist_ok=True)
    for size in (192, 512):
        path = OUT / f"icon-{size}.png"
        path.write_bytes(render(size))
        print(f"wrote {path.relative_to(ROOT)} ({path.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
