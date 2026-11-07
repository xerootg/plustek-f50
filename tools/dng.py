#!/usr/bin/env python3
"""Minimal uncompressed 16-bit CFA DNG writer (no dependencies beyond numpy).

Writes one IFD with the raw Bayer plane, enough for LibRaw, darktable and RapidRAW.
The colour matrix is a placeholder (XYZ D65 -> linear sRGB), not a calibration.
"""
import struct

import numpy as np

# TIFF types
BYTE, ASCII, SHORT, LONG, RATIONAL, SRATIONAL = 1, 2, 3, 4, 5, 10
_SIZE = {BYTE: 1, ASCII: 1, SHORT: 2, LONG: 4, RATIONAL: 8, SRATIONAL: 8}
CFA_CODES = {"R": 0, "G": 1, "B": 2}

XYZ_TO_SRGB = [3.2406, -1.5372, -0.4986, -0.9689, 1.8758, 0.0415, 0.0557, -0.2040, 1.0570]


def _rat(x, signed=False, den=10000):
    return (int(round(x * den)), den)


def write_dng(path, cfa16, pattern="BGGR", black=(0, 0, 0, 0), white=65535,
              neutral=(1.0, 1.0, 1.0), make="Plustek", model="SmartPhoto F50",
              software="plustek-f50 tools/dng.py", description="", orientation=1):
    """cfa16: (H, W) uint16 Bayer plane. black: one value per 2x2 CFA position in
    row-major order. neutral: camera RGB of a neutral (AsShotNeutral). orientation: TIFF
    Orientation (1 = as stored, 2 = mirrored left-right; the F50 needs 2)."""
    a = np.ascontiguousarray(cfa16, dtype="<u2")
    h, w = a.shape
    entries = [
        (254, LONG, [0]),
        (256, LONG, [w]),
        (257, LONG, [h]),
        (258, SHORT, [16]),
        (259, SHORT, [1]),
        (262, SHORT, [32803]),          # CFA
        (270, ASCII, description),
        (271, ASCII, make),
        (272, ASCII, model),
        (273, LONG, None),              # StripOffsets, patched below
        (274, SHORT, [orientation]),
        (277, SHORT, [1]),
        (278, LONG, [h]),
        (279, LONG, [a.nbytes]),
        (284, SHORT, [1]),
        (305, ASCII, software),
        (33421, SHORT, [2, 2]),
        (33422, BYTE, [CFA_CODES[c] for c in pattern]),
        (50706, BYTE, [1, 4, 0, 0]),
        (50707, BYTE, [1, 1, 0, 0]),
        (50708, ASCII, f"{make} {model}"),
        (50710, BYTE, [0, 1, 2]),
        (50711, SHORT, [1]),
        (50713, SHORT, [2, 2]),
        (50714, RATIONAL, [_rat(b) for b in black]),
        (50717, LONG, [int(white)]),
        (50721, SRATIONAL, [_rat(x) for x in XYZ_TO_SRGB]),
        (50728, RATIONAL, [_rat(x / max(neutral)) for x in neutral]),
        (50778, SHORT, [21]),           # D65
    ]
    entries = [e for e in entries if e[2] != ""]
    entries.sort(key=lambda e: e[0])
    n = len(entries)
    ifd_off = 8
    data_off = ifd_off + 2 + 12 * n + 4
    blobs = bytearray()
    raw_entries = []
    for tag, typ, val in entries:
        if tag == 273:
            raw_entries.append((tag, typ, 1, None))
            continue
        if typ == ASCII:
            payload = val.encode() + b"\0"
            count = len(payload)
        elif typ in (RATIONAL, SRATIONAL):
            fmt = "<ii" if typ == SRATIONAL else "<II"
            payload = b"".join(struct.pack(fmt, *r) for r in val)
            count = len(val)
        else:
            fmt = {BYTE: "B", SHORT: "H", LONG: "I"}[typ]
            payload = struct.pack("<" + fmt * len(val), *val)
            count = len(val)
        if len(payload) <= 4:
            raw_entries.append((tag, typ, count, payload.ljust(4, b"\0")))
        else:
            if (data_off + len(blobs)) % 2:
                blobs += b"\0"
            off = data_off + len(blobs)
            blobs += payload
            raw_entries.append((tag, typ, count, struct.pack("<I", off)))
    strip_off = data_off + len(blobs)
    strip_off += (-strip_off) % 16
    out = bytearray(b"II*\0" + struct.pack("<I", ifd_off))
    out += struct.pack("<H", n)
    for tag, typ, count, field in raw_entries:
        if tag == 273:
            field = struct.pack("<I", strip_off)
        out += struct.pack("<HHI", tag, typ, count) + field
    out += struct.pack("<I", 0)
    out += blobs
    out += b"\0" * (strip_off - len(out))
    with open(path, "wb") as f:
        f.write(out)
        f.write(a.tobytes())
