#!/usr/bin/env python3
"""Step 10: test the three backlight channels (firmware port D bits 0..2) one at a time.

  sudo python3 -I tools/f50leds.py [--exposure AEC] [--frames N] [--out frames]

Opcode 9 with block[4] = sub-command, block[5] = value: sub 1, 2, 3 switch PD0, PD1, PD2
(firmware 0x1e9b/0x1ea9/0x1eb7); sub 4 switches all three (F50.lamp()). Procedure: all
off; for each channel: on, wait 2 s, capture, off; then all on and capture the normal flat.
Writes OUT/backlight_ch{c}.png (2x2-binned RGB preview, scaled to its own maximum) and
OUT/backlight_channels.txt (plane means, R/G and B/G, a 4x4 grid of the green level to show
where each channel lights, and the sum of the three against the all-on flat).
"""
import argparse
import os
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import f50  # noqa: E402


def chan(s, sub, on):
    r = s.cmd(f50.OP_START_STREAM, bytes([0, 0, sub, 1 if on else 0]))
    return s.status(r)


def planes(fr):
    a = fr.astype(np.float64)
    return {"B": a[0::2, 0::2], "Gb": a[0::2, 1::2], "Gr": a[1::2, 0::2], "R": a[1::2, 1::2]}


def grid(g, n=4):
    h, w = g.shape
    return g[:h // n * n, :w // n * n].reshape(n, h // n, n, w // n).mean((1, 3))


def preview(fr, path):
    from PIL import Image
    p = planes(fr)
    rgb = np.stack([p["R"], (p["Gb"] + p["Gr"]) / 2, p["B"]], -1)[:, ::-1]   # mirror-corrected
    rgb = np.clip(rgb / max(rgb.max(), 1), 0, 1)
    im = Image.fromarray((rgb ** (1 / 2.2) * 255).astype(np.uint8))
    im.resize((648, 486)).save(path)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--exposure", type=int, default=6)
    ap.add_argument("--frames", type=int, default=4)
    ap.add_argument("--out", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "frames"))
    args = ap.parse_args()
    s = f50.F50()
    s.raw_setup(exposure=args.exposure)
    res, stat = {}, {}
    try:
        stat["all off"] = chan(s, 4, False)
        time.sleep(2)
        res["dark"] = s.frames(args.frames).astype(np.float64).mean(0)
        for c in (1, 2, 3):
            stat[f"ch{c} on"] = chan(s, c, True)
            time.sleep(2)
            res[f"ch{c}"] = s.frames(args.frames).astype(np.float64).mean(0)
            stat[f"ch{c} off"] = chan(s, c, False)
        stat["all on"] = chan(s, 4, True)
        time.sleep(2)
        res["all"] = s.frames(args.frames).astype(np.float64).mean(0)
    finally:
        s.lamp(True)
    dark = res["dark"]
    lines = [f"Backlight channels, AEC {args.exposure}, gain 1x, CLKRC 0x41, mean of {args.frames} 8-bit frames, "
             f"holder empty, {time.strftime('%Y-%m-%d %H:%M')}",
             "Command status: " + ", ".join(f"{k}={v:#06x}" for k, v in stat.items()),
             "Plane means are full-frame, 8-bit, minus the all-off frame. Grid: 4x4 green level, "
             "rows top to bottom of the sensor frame (not mirror-corrected), as % of its own maximum."]
    dp = {k: float(v.mean()) for k, v in planes(dark).items()}
    lines.append(f"all off (black): " + "  ".join(f"{k} {v:.2f}" for k, v in dp.items()))
    sums = None
    for key in ("ch1", "ch2", "ch3", "all"):
        fr = res[key]
        p = {k: float(v.mean()) - dp[k] for k, v in planes(fr).items()}
        g = (p["Gb"] + p["Gr"]) / 2
        sat = float((fr >= 254.5).mean())
        black = max(p.values()) < 2
        kind = "BLACK (no light)" if black else ("white/broadband" if min(p["R"], p["B"]) > 0.3 * g else
                                                  "colour LED: " + max(("R", p["R"]), ("G", g), ("B", p["B"]), key=lambda t: t[1])[0])
        gr = grid(((planes(fr)["Gb"] + planes(fr)["Gr"]) / 2) - (dp["Gb"] + dp["Gr"]) / 2)
        gr = 100 * gr / max(gr.max(), 1e-6)
        lines.append(f"{key}: B {p['B']:.2f}  Gb {p['Gb']:.2f}  Gr {p['Gr']:.2f}  R {p['R']:.2f}  "
                     f"R/G {p['R'] / max(g, 1e-6):.3f}  B/G {p['B'] / max(g, 1e-6):.3f}  clipped {sat:.4f}  -> {kind}")
        lines += ["    " + " ".join(f"{v:5.0f}" for v in row) for row in gr]
        if key != "all":
            preview(fr - dark, os.path.join(args.out, f"backlight_{key}.png"))
            sums = (fr - dark) if sums is None else sums + (fr - dark)
    allon = res["all"] - dark
    ps, pa = planes(sums), planes(allon)
    lines.append("sum of ch1+ch2+ch3 vs all on: " + "  ".join(
        f"{k} {float(ps[k].mean()):.2f}/{float(pa[k].mean()):.2f}" for k in ps))
    rel = (sums - allon)[400:1544, 600:1992]
    lines.append(f"pixelwise (sum - all on) over the centre: mean {rel.mean():.2f}, std {rel.std():.2f} (8-bit)")
    txt = "\n".join(lines)
    with open(os.path.join(args.out, "backlight_channels.txt"), "w") as f:
        f.write(txt + "\n")
    print(txt)


if __name__ == "__main__":
    main()
