"""Minimal PNG reader/writer: 8-bit RGB/RGBA, non-interlaced. Standard library only.

Exists because this machine has no PIL, no numpy, and no ImageMagick, and the
official price chart is only available as a raster image. Decoding it in pure Python
is ~60 lines and beats adding a dependency to read one file.

Handles exactly what is needed: colour types 2 and 6, bit depth 8, no interlacing.
Anything else raises rather than guessing.
"""

import struct, zlib

def read_png(path):
    data = open(path, "rb").read()
    assert data[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG"
    pos, idat, w = 8, [], None
    while pos < len(data):
        ln, typ = struct.unpack(">I4s", data[pos:pos+8])
        body = data[pos+8:pos+8+ln]
        if typ == b"IHDR":
            w, h, depth, color, comp, filt, inter = struct.unpack(">IIBBBBB", body)
            assert depth == 8 and inter == 0, f"unsupported depth/interlace {depth}/{inter}"
            assert color in (2, 6), f"unsupported colour type {color}"
            nch = 3 if color == 2 else 4
        elif typ == b"IDAT":
            idat.append(body)
        elif typ == b"IEND":
            break
        pos += 12 + ln
    raw = zlib.decompress(b"".join(idat))
    stride = w * nch
    out = bytearray(h * stride)
    prev = bytearray(stride)
    p = 0
    for y in range(h):
        ft = raw[p]; p += 1
        line = bytearray(raw[p:p+stride]); p += stride
        if ft == 1:
            for i in range(nch, stride): line[i] = (line[i] + line[i-nch]) & 0xFF
        elif ft == 2:
            for i in range(stride): line[i] = (line[i] + prev[i]) & 0xFF
        elif ft == 3:
            for i in range(stride):
                a = line[i-nch] if i >= nch else 0
                line[i] = (line[i] + ((a + prev[i]) >> 1)) & 0xFF
        elif ft == 4:
            for i in range(stride):
                a = line[i-nch] if i >= nch else 0
                b = prev[i]
                c = prev[i-nch] if i >= nch else 0
                pa, pb, pc = abs(b-c), abs(a-c), abs(a+b-2*c)
                pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                line[i] = (line[i] + pr) & 0xFF
        elif ft != 0:
            raise ValueError(f"bad filter {ft}")
        out[y*stride:(y+1)*stride] = line
        prev = line
    return w, h, nch, bytes(out)

class Img:
    def __init__(self, path):
        self.w, self.h, self.nch, self.buf = read_png(path)
    def px(self, x, y):
        i = (y * self.w + x) * self.nch
        return tuple(self.buf[i:i+3])
    def patch(self, x, y, r=3):
        """Most common colour in a small square - robust to antialiasing and text."""
        from collections import Counter
        c = Counter()
        for dy in range(-r, r+1):
            for dx in range(-r, r+1):
                xx, yy = x+dx, y+dy
                if 0 <= xx < self.w and 0 <= yy < self.h:
                    c[self.px(xx, yy)] += 1
        return c.most_common(1)[0][0]

def write_png(path, w, h, rgb_rows):
    """rgb_rows: list of bytes objects, each w*3 long."""
    import struct, zlib
    def chunk(t, d):
        c = struct.pack(">I", len(d)) + t + d
        return c + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    raw = b"".join(b"\x00" + r for r in rgb_rows)
    png = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(raw, 6))
           + chunk(b"IEND", b""))
    open(path, "wb").write(png)

def crop_scale(img, x0, y0, x1, y1, scale, out):
    w, h = (x1 - x0) * scale, (y1 - y0) * scale
    rows = []
    for y in range(h):
        sy = y0 + y // scale
        row = bytearray()
        for x in range(w):
            sx = x0 + x // scale
            row += bytes(img.px(sx, sy))
        rows.append(bytes(row))
    write_png(out, w, h, rows)
    return w, h
