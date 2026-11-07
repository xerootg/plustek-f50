#!/usr/bin/env python3
"""Capture raw Bayer frames from the Plustek SmartPhoto F50.

  sudo python3 -I tools/f50raw.py OUT [--exposure LINES] [--gain REG] [--clkdiv N]
                                      [--frames N] [--dark] [--black-from DARK.pgm]
                                      [--neutral-from FLAT.pgm] [--save-frames]

Writes OUT.pgm (16-bit: mean of N 8-bit frames x 256, white = 65280), OUT.dng (same
samples, BGGR, black level and white balance from the optional reference frames) and
OUT.json (settings and statistics). --dark switches the backlight off for a dark frame.
Sensor settings are volatile; a replug restores the scanner's boot state.
"""
import argparse
import json
import os
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import f50  # noqa: E402
from dng import write_dng  # noqa: E402

SCALE = 256           # 8-bit samples stored as x256 in 16 bits
WHITE = 255 * SCALE


def read_pgm16(path):
    with open(path, "rb") as f:
        data = f.read()
    parts, pos = [], 0
    while len(parts) < 4:
        while data[pos:pos + 1].isspace():
            pos += 1
        if data[pos:pos + 1] == b"#":
            pos = data.index(b"\n", pos) + 1
            continue
        end = pos
        while not data[end:end + 1].isspace():
            end += 1
        parts.append(data[pos:end])
        pos = end
    pos += 1
    w, h, maxval = int(parts[1]), int(parts[2]), int(parts[3])
    dt = ">u2" if maxval > 255 else "u1"
    a = np.frombuffer(data, dt, w * h, pos).reshape(h, w).astype(np.float64)
    return a if maxval > 255 else a * SCALE


def cfa_means(a, y0=0, y1=None, x0=0, x1=None):
    """Mean of each 2x2 CFA position (row-major: (0,0) (0,1) (1,0) (1,1))."""
    c = a[y0:y1, x0:x1]
    return [float(c[i::2, j::2].mean()) for i in (0, 1) for j in (0, 1)]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("out")
    ap.add_argument("--exposure", type=int, default=6, help="AEC in sensor lines; effective exposure is AEC + ~388 lines for AEC >= 6")
    ap.add_argument("--gain", type=lambda x: int(x, 0), default=0, help="analog gain register 0x00 (0 = 1x)")
    ap.add_argument("--clkdiv", type=int, default=1, help="CLKRC divider: 1 (default, ~14 MB/s) or slower")
    ap.add_argument("--frames", type=int, default=1, help="frames to average")
    ap.add_argument("--dark", action="store_true", help="backlight off (dark frame)")
    ap.add_argument("--black-from", help="16-bit PGM dark frame: per-CFA black level for the DNG")
    ap.add_argument("--neutral-from", help="16-bit PGM of the bare backlight: AsShotNeutral for the DNG")
    ap.add_argument("--save-frames", action="store_true", help="also write the first frame as 8-bit PGM")
    args = ap.parse_args()

    s = f50.F50()
    s.lamp(not args.dark)
    s.raw_setup(exposure=args.exposure, gain_reg=args.gain, clkdiv=args.clkdiv)
    t0 = time.time()
    fr = s.frames(args.frames)
    dt = time.time() - t0
    if args.dark:
        s.lamp(True)

    acc = fr.astype(np.float64)
    mean = acc.mean(0)
    out16 = np.clip(np.rint(mean * SCALE), 0, 65535).astype(">u2")
    f50.save_pgm(args.out + ".pgm", out16.tobytes(), f50.WIN_WIDTH, f50.WIN_HEIGHT, 65535)
    if args.save_frames:
        f50.save_pgm(args.out + "_f0.pgm", fr[0].tobytes(), f50.WIN_WIDTH, f50.WIN_HEIGHT)

    H, W = mean.shape
    ctr = (H // 2 - 200, H // 2 + 200, W // 2 - 300, W // 2 + 300)
    info = {
        "exposure_lines": args.exposure, "gain_reg": args.gain, "clkdiv": args.clkdiv,
        "frames": args.frames, "dark": args.dark, "seconds": round(dt, 2),
        "cfa": f50.CFA, "scale": SCALE, "white": WHITE,
        "centre_cfa_means_8bit": [round(v, 3) for v in cfa_means(mean, *ctr)],
        "clipped_fraction_frame0": float((fr[0] == 255).mean()),
    }
    if args.frames >= 2:
        d = acc[0] - acc[1]
        sig1 = float(d[ctr[0]:ctr[1], ctr[2]:ctr[3]].std() / np.sqrt(2))
        info["temporal_noise_1frame_8bit"] = round(sig1, 4)
        info["temporal_noise_mean_8bit_expected"] = round(sig1 / np.sqrt(args.frames), 4)

    black = (0, 0, 0, 0)
    if args.black_from:
        black = tuple(cfa_means(read_pgm16(args.black_from)))
        info["black_level_x256"] = [round(b, 1) for b in black]
    neutral = (1.0, 1.0, 1.0)
    if args.neutral_from:
        m = cfa_means(read_pgm16(args.neutral_from), *ctr)
        m = [v - b for v, b in zip(m, black)]
        # BGGR: (0,0)=B, (0,1)=G, (1,0)=G, (1,1)=R
        neutral = (m[3], (m[1] + m[2]) / 2, m[0])
        info["as_shot_neutral_rgb"] = [round(v / max(neutral), 4) for v in neutral]
    write_dng(args.out + ".dng", out16.astype(np.uint16), pattern=f50.CFA, black=black, white=WHITE,
              neutral=neutral, description=json.dumps(info))
    with open(args.out + ".json", "w") as f:
        json.dump(info, f, indent=1)
    print(json.dumps(info, indent=1))


if __name__ == "__main__":
    main()
