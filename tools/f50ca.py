#!/usr/bin/env python3
"""Measure lateral chromatic aberration in a demosaiced F50 scan (PLAN.md step 17 check).

  .venv/bin/python tools/f50ca.py IN.dng [--demosaic dht]

For a grid of 256 px tiles, finds the sub-pixel shift of R and of B relative to G by
cross-correlating the gradient magnitude of log intensity (FFT, parabolic peak). Lateral CA
shows up as shifts that grow linearly with distance from the optical centre; a fit
shift = (k - 1) * (position - centre) gives the radial scale k per channel. Prints the
tiles and the fit; |k - 1| * r_corner below ~0.3 px means there is nothing to correct.
"""
import argparse
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from f50positive import demosaic, find_regions  # noqa: E402


def grad(a):
    gy, gx = np.gradient(a)
    return np.hypot(gx, gy)


def shift(ref, mov):
    """Sub-pixel (dy, dx) such that mov(x) ~ ref(x - d)."""
    win = np.outer(np.hanning(ref.shape[0]), np.hanning(ref.shape[1]))
    a = (ref - ref.mean()) * win
    b = (mov - mov.mean()) * win
    c = np.fft.ifft2(np.fft.fft2(b) * np.conj(np.fft.fft2(a))).real
    c = np.fft.fftshift(c)
    py, px = np.unravel_index(np.argmax(c), c.shape)
    if not (1 <= py < c.shape[0] - 1 and 1 <= px < c.shape[1] - 1):
        return None
    def para(m1, z, p1):
        den = m1 - 2 * z + p1
        return 0.0 if den == 0 else 0.5 * (m1 - p1) / den
    dy = py - c.shape[0] // 2 + para(c[py - 1, px], c[py, px], c[py + 1, px])
    dx = px - c.shape[1] // 2 + para(c[py, px - 1], c[py, px], c[py, px + 1])
    peak = c[py, px] / (np.sqrt((a * a).sum() * (b * b).sum()) + 1e-12)
    return dy, dx, peak


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("dng")
    ap.add_argument("--demosaic", default="dht")
    ap.add_argument("--tile", type=int, default=256)
    args = ap.parse_args()
    img = demosaic(args.dng, args.demosaic)
    _, (x0, y0, x1, y1), _ = find_regions(img)
    lg = np.log(np.maximum(img, 16.0))
    g = [grad(lg[..., c]) for c in range(3)]
    h, w = img.shape[:2]
    cy, cx = h / 2.0, w / 2.0
    t = args.tile
    rows = []
    for ty in range(y0 + 8, y1 - t - 8, t):
        for tx in range(x0 + 8, x1 - t - 8, t):
            sl = (slice(ty, ty + t), slice(tx, tx + t))
            res = [shift(g[1][sl], g[c][sl]) for c in (0, 2)]
            if None in res or min(r[2] for r in res) < 0.3:
                continue
            rows.append((ty + t / 2 - cy, tx + t / 2 - cx, res[0][0], res[0][1], res[1][0], res[1][1]))
    a = np.array(rows)
    print(f"{len(a)} tiles; image {w}x{h}, centre ({cx:.0f},{cy:.0f})")
    print("   y-off   x-off |  R dy   R dx |  B dy   B dx")
    for r in a:
        print(f"{r[0]:7.0f} {r[1]:7.0f} | {r[2]:5.2f} {r[3]:5.2f} | {r[4]:5.2f} {r[5]:5.2f}")
    pos = np.concatenate([a[:, 0], a[:, 1]])
    for name, sy, sx in (("R", 2, 3), ("B", 4, 5)):
        d = np.concatenate([a[:, sy], a[:, sx]])
        A = np.stack([pos, np.ones_like(pos)], 1)
        (k1, off), *_ = np.linalg.lstsq(A, d, rcond=None)
        rc = np.hypot(cy, cx)
        resid = d - A @ np.array([k1, off])
        print(f"{name}: radial scale k = {1 + k1:.6f} (shift at the corner {k1 * rc:+.3f} px), "
              f"constant offset {off:+.3f} px, residual rms {resid.std():.3f} px")


if __name__ == "__main__":
    main()
