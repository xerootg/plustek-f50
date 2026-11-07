#!/usr/bin/env python3
"""Deep temporal stacking and fixed-pattern calibration for the F50 (PLAN.md steps 12-13).

Frames are accumulated as they arrive (float64 sum and sum of squares); no frame list is
kept, so memory stays at ~80 MB whatever N is. Each frame is checked before it is added:

  - transfer faults: a read that comes back short is dropped whole;
  - misaligned or corrupted frames: the mean absolute residual against the running stack
    (on a 4x-subsampled grid that keeps the CFA phase) is compared with the median residual
    seen so far; a frame above 3x that median, or above it + 4 LSB, is rejected. Dropped
    bytes shift the Bayer phase and give residuals of tens of LSB; noise gives ~1-2;
  - backlight flicker: a frame whose mean differs from the running median of frame means
    by more than max(6 MAD, 0.5 LSB) is rejected.

Noise versus N is recorded at N = 1, 2, 4, ... : per CFA channel, the median over the centre
crop of the per-pixel temporal std divided by sqrt(N) (the standard error of the stack).

Masters (frames/masters/): KIND_eAEC_cCLK_gGAIN.npy (float32 mean, 8-bit units, or 10-bit
units for 10-bit stacks) with a .json sidecar. Darks match the exposure setting; flats
are dark-subtracted and normalised per CFA channel at use, so any non-clipping flat
exposure serves every film exposure (the sensor is linear).
"""
import json
import os
import time

import numpy as np

H, W = 1944, 2592
CTR = (H // 2 - 200, H // 2 + 200, W // 2 - 300, W // 2 + 300)
SUB = (slice(0, None, 4), slice(0, None, 4))    # residual grid, keeps (0,0) phase only
SUB2 = (slice(1, None, 4), slice(1, None, 4))   # and (1,1)
NAMES = ("B", "Gb", "Gr", "R")                  # (0,0) (0,1) (1,0) (1,1) for BGGR; G at (0,1) is on the B row


def cfa_split(a):
    return [a[i::2, j::2] for i in (0, 1) for j in (0, 1)]


class Stack:
    """Streaming accumulator with frame rejection and noise-versus-N tracking."""

    def __init__(self, log=print):
        self.sum = np.zeros((H, W), np.float64)
        self.sq = np.zeros((H, W), np.float64)
        self.n = 0
        self.rejected = []          # (index, reason)
        self.seen = 0
        self.means = []
        self.resid = []
        self.noise_vs_n = {}
        self.log = log
        self.ref = None

    def _resid(self, f):
        if self.ref is None:
            return None
        return float(0.5 * (np.abs(f[SUB] - self.ref[SUB]).mean() + np.abs(f[SUB2] - self.ref[SUB2]).mean()))

    def add(self, frame):
        """frame: (H, W) array (uint8 or 10-bit uint16). Returns True if accepted."""
        i = self.seen
        self.seen += 1
        f = frame.astype(np.float64)
        m = float(f[CTR[0]:CTR[1], CTR[2]:CTR[3]].mean())
        if len(self.means) >= 5:
            med = float(np.median(self.means))
            mad = float(np.median(np.abs(np.array(self.means) - med)))
            if abs(m - med) > max(6 * 1.4826 * mad, 0.5):
                self.rejected.append((i, f"mean {m:.2f} vs median {med:.2f}"))
                self.log(f"  reject frame {i}: mean {m:.2f} vs median {med:.2f}")
                return False
        r = self._resid(f)
        if r is not None and len(self.resid) >= 3:
            mr = float(np.median(self.resid))
            if r > max(3 * mr, mr + 4):
                self.rejected.append((i, f"residual {r:.2f} vs median {mr:.2f}"))
                self.log(f"  reject frame {i}: residual {r:.2f} vs median {mr:.2f}")
                return False
        if r is not None:
            self.resid.append(r)
        self.means.append(m)
        self.sum += f
        self.sq += f * f
        self.n += 1
        # reference for the residual test: the running mean (first frame until N = 3)
        if self.n <= 3 or self.n % 8 == 0:
            self.ref = self.sum / self.n
        if self.n >= 2 and (self.n & (self.n - 1)) == 0:
            self.noise_vs_n[self.n] = self.noise()
        return True

    def mean(self):
        return self.sum / max(self.n, 1)

    def pixel_std(self):
        n = max(self.n, 2)
        var = (self.sq - self.sum * self.sum / n) / (n - 1)
        return np.sqrt(np.maximum(var, 0))

    def noise(self):
        """Per CFA channel: median single-frame temporal std and stack standard error (centre)."""
        y0, y1, x0, x1 = CTR
        n = max(self.n, 2)
        s = self.sum[y0:y1, x0:x1]
        var = (self.sq[y0:y1, x0:x1] - s * s / n) / (n - 1)
        sd = np.sqrt(np.maximum(var, 0))
        out = {}
        for name, c in zip(NAMES, cfa_split(sd)):
            v = float(np.median(c))
            out[name] = {"frame_std": round(v, 4), "stack_se": round(v / np.sqrt(self.n), 5)}
        return out

    def summary(self):
        return {"frames_used": self.n, "frames_seen": self.seen, "rejected": self.rejected,
                "noise_vs_n": {str(k): v for k, v in self.noise_vs_n.items()},
                "noise_final": self.noise() if self.n >= 2 else None,
                "centre_cfa_mean": [round(float(c.mean()), 4) for c in
                                    cfa_split(self.mean()[CTR[0]:CTR[1], CTR[2]:CTR[3]])]}


def stack8(s, n, chunk=3, log=print, max_seen=None):
    """Capture until n frames are accepted (8-bit), chunk frames per bulk read."""
    import f50
    st = Stack(log=log)
    max_seen = max_seen or int(n * 1.5) + 6
    t0 = time.time()
    while st.n < n and st.seen < max_seen:
        k = min(chunk, n - st.n)
        data = s.capture(f50.FRAME_BYTES * k, log=lambda *_: None)
        if len(data) != f50.FRAME_BYTES * k:
            st.rejected.append((st.seen, f"short read {len(data)} of {f50.FRAME_BYTES * k}"))
            log(f"  short read: {len(data)} bytes")
            st.seen += k
            continue
        for fr in np.frombuffer(data, np.uint8).reshape(k, H, W):
            st.add(fr)
        if st.n and (st.n % 30 < k):
            el = time.time() - t0
            log(f"  {st.n}/{n} frames, {el:.0f} s, {el / st.n:.2f} s/frame, rejected {len(st.rejected)}")
    st.seconds = time.time() - t0
    return st


def master_name(kind, exposure, clkdiv, gain, tenbit=False):
    return f"{kind}{'10' if tenbit else ''}_e{exposure}_c{clkdiv}_g{gain}"


def save_master(dirpath, name, st, meta):
    os.makedirs(dirpath, exist_ok=True)
    mean = st.mean().astype(np.float32)
    np.save(os.path.join(dirpath, name + ".npy"), mean)
    info = {**meta, **st.summary(), "seconds": round(st.seconds, 1),
            "seconds_per_frame": round(st.seconds / max(st.n, 1), 3),
            "frame_mean_spread": round(float(np.std(st.means)), 4) if st.means else None}
    with open(os.path.join(dirpath, name + ".json"), "w") as f:
        json.dump(info, f, indent=1)
    return info


def load_master(dirpath, name):
    p = os.path.join(dirpath, name + ".npy")
    if not os.path.exists(p):
        return None, None
    return np.load(p).astype(np.float64), json.load(open(os.path.join(dirpath, name + ".json")))


def find_flat(dirpath, tenbit=False):
    """Best flat master: the most frames, then the brightest non-clipping one."""
    best = None
    if not os.path.isdir(dirpath):
        return None
    for fn in sorted(os.listdir(dirpath)):
        if fn.startswith("flat" + ("10" if tenbit else "") + "_e") and fn.endswith(".json"):
            info = json.load(open(os.path.join(dirpath, fn)))
            key = (info.get("frames_used", 0), -abs(max(info["centre_cfa_mean"]) - (700 if tenbit else 175)))
            if best is None or key > best[0]:
                best = (key, fn[:-5])
    return best[1] if best else None


def flat_gain(flat, flat_dark):
    """Per-pixel flat-field divisor, normalised to 1.0 per CFA channel over the centre crop.
    Dividing by it removes vignetting and pixel response non-uniformity but keeps each
    channel's scale (white balance is left to AsShotNeutral)."""
    f = flat - flat_dark
    g = np.empty_like(f)
    y0, y1, x0, x1 = CTR
    for i in (0, 1):
        for j in (0, 1):
            ch = f[i::2, j::2]
            ref = float(ch[y0 // 2:y1 // 2, x0 // 2:x1 // 2].mean())
            g[i::2, j::2] = ch / ref
    return np.clip(g, 0.05, None)
