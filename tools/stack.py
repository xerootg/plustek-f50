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
        self.max = None                 # per-pixel maximum over accepted frames (clip mask)
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
        self.max = frame.copy() if self.max is None else np.maximum(self.max, frame)
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


def stream_offset(buf, ref, rev8, F=None):
    """Byte offset of the first frame boundary in a bulk-read buffer of fine frames.

    The firmware does not always start the FIFO at VSYNC: a chunk then holds complete
    frames that begin partway through the buffer (2026-11-07 evening: three frames in a row
    rejected, content offset by 1,237,644 bytes, the next chunk fine again). The first frame's
    worth of bytes is cross-correlated (FFT) with the fine byte stream expected from the
    reference; returns (offset, quality) where quality is the fraction of a pixel sample
    within 6 LSB of the reference after unwrapping at that offset (aligned frames measure
    0.7-0.9, a wrong offset below 0.1)."""
    F = F or H * W
    e = rev8[np.rint(ref).astype(np.int64) & 0xFF].astype(np.float64).ravel()
    f = np.frombuffer(buf, np.uint8, count=F).astype(np.float64)
    P = 1 << (2 * F - 1).bit_length()                      # >= 2F: no circular ambiguity
    cc = np.fft.irfft(np.fft.rfft(f - f.mean(), P) * np.conj(np.fft.rfft(e - e.mean(), P)), P)
    i = int(np.argmax(cc)); del cc, f
    s = i if i < P // 2 else i - P                         # buffer[n + s] ~ expected[n]
    o = s % F
    if o + F > len(buf):
        return o, 0.0
    fr = np.frombuffer(buf, np.uint8, count=F, offset=o).reshape(H, W)[::7, ::7]
    r = ref[::7, ::7]
    lo = rev8[fr].astype(np.float64)
    v = np.clip(lo + 256 * np.rint((r - lo) / 256.0), 0, 1023)
    m = r < 1000
    return o, float((np.abs(v - r)[m] < 6).mean()) if m.any() else 0.0


def stack10(s, n, chunk=3, coarse_frames=4, settle_frames=2, log=print, max_seen=None,
            dump=None, resync_after=4, max_resyncs=3):
    """Capture until n true 10-bit frames are accepted. One block of coarse frames (bits
    9..2) seeds a 10-bit reference; every later frame is a fine frame (bus swapped: bits
    0..7, bit-reversed) unwrapped against the running mean of the stack, so a deep 10-bit
    stack costs one frame per frame. Pixels at 255 in any coarse frame are clipped (1023).

    Recovery: a rejected frame is first checked for a stream offset (stream_offset): the
    FIFO sometimes starts partway through a frame, so the chunk holds whole frames that do
    not begin at the buffer start. When the boundary is found the chunk is realigned and its
    remaining whole frames are stacked (k-1 of k). After resync_after consecutive rejections
    the swap register is read back and logged, the swap bit is re-asserted and capture
    continues (2026-11-07: on film at AEC 10764 every fine frame after the 10th was rejected
    with the same residual; the offset check did not exist yet). The first rejected raw
    frames are saved as DUMP_rejectI.npy when dump is given. After max_resyncs the step is
    abandoned with an error instead of burning the rest of max_seen."""
    import f50
    rev8 = np.array([int(f"{i:08b}"[::-1], 2) for i in range(256)], np.uint8)
    s.bus_swap(False)
    # The first frames after a change of exposure straddle it (measured on film: 45,000
    # pixels of a 4x step unwrapped 256 too high from a bad reference), so drop
    # settle_frames before the coarse block
    coarse = s.frames(settle_frames + coarse_frames)[settle_frames:]
    ref = coarse.astype(np.float64).mean(0) * 4 + 1.5
    coarse_ref = ref.copy()
    clip = (coarse == 255).any(0)
    del coarse
    st = Stack(log=log)
    max_seen = max_seen or int(n * 1.5) + 6
    t0 = time.time()
    s.bus_swap(True)
    streak, resyncs, dumped, offsets = 0, 0, 0, []
    try:
        while st.n < n and st.seen < max_seen:
            k = min(chunk, n - st.n)
            data = s.capture(f50.FRAME_BYTES * k, log=lambda *_: None)
            if len(data) != f50.FRAME_BYTES * k:
                st.rejected.append((st.seen, f"short read {len(data)} of {f50.FRAME_BYTES * k}"))
                st.seen += k
                streak += k
            else:
                frames = np.frombuffer(data, np.uint8).reshape(k, H, W)
                j, realigned = 0, False
                while j < len(frames):
                    fr = frames[j]
                    j += 1
                    lo = rev8[fr].astype(np.float64)
                    v = np.clip(lo + 256 * np.rint((ref - lo) / 256.0), 0, 1023)
                    v[clip] = 1023
                    if st.add(v.astype(np.uint16)):
                        streak = 0
                        continue
                    streak += 1
                    if dump and dumped < 3:
                        path = f"{dump}_reject{st.seen - 1}.npy"
                        np.save(path, fr)
                        dumped += 1
                        log(f"  saved rejected raw frame to {path}")
                    if not realigned:
                        realigned = True
                        o, q = stream_offset(data, ref, rev8)
                        if o and q >= 0.5:
                            nf = (len(data) - o) // f50.FRAME_BYTES
                            log(f"  frame boundary found {o} bytes into the chunk ({o // W} rows + {o % W} px, "
                                f"quality {q:.2f}); realigning, {nf} of {k} frames recoverable")
                            offsets.append(o)
                            frames = np.frombuffer(data, np.uint8, count=nf * f50.FRAME_BYTES,
                                                   offset=o).reshape(nf, H, W)
                            j = 0
                        else:
                            log(f"  no stream offset found (best {o} bytes, quality {q:.2f})")
            if streak >= resync_after:
                reg = s.read_regs([f50.SWAP_REG])[f50.SWAP_REG]
                resyncs += 1
                log(f"  resync {resyncs}: {streak} frames rejected in a row; reg 0x{f50.SWAP_REG:02x} = "
                    f"0x{reg:02x} (swap bit {'set' if reg & f50.SWAP_MASK else 'CLEAR'}); re-asserting swap")
                if resyncs > max_resyncs:
                    raise RuntimeError(f"stack10: frames still rejected after {max_resyncs} resyncs "
                                       f"(last: {st.rejected[-1][1]}); {st.n} of {n} frames stacked")
                s.bus_swap(True)
                streak = 0
            if st.n:
                ref = st.mean()
            if st.n and (st.n % 30 < k):
                el = time.time() - t0
                log(f"  {st.n}/{n} frames, {el:.0f} s, {el / st.n:.2f} s/frame, rejected {len(st.rejected)}")
    finally:
        s.bus_swap(False)
    st.seconds = time.time() - t0
    st.clip = clip
    # check: the finished stack must agree with the coarse reference to within noise; a
    # whole multiple of 256 means the unwrap locked onto the wrong turn for that pixel
    # (fixed here: every accepted frame of that pixel carried the same offset)
    mean = st.mean()
    k = np.rint((mean - coarse_ref) / 256.0)
    k[clip] = 0
    bad = k != 0
    if bad.any():
        o = -256.0 * k                   # same offset in every frame of that pixel
        st.sq += 2 * o * st.sum + st.n * o * o
        st.sum += o * st.n
        log(f"  10-bit check: {int(bad.sum())} pixels were a whole turn off the coarse reference; corrected")
    st.unwrap_fixed = int(bad.sum())
    st.resyncs = resyncs
    st.offsets = offsets
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


def defect_map(dark10, flat_gain_map, hot_sigma=8.0, hot_min=6.0, dead_frac=0.25, skip_rows=6):
    """Hot pixels: dark master above its channel median by > max(hot_sigma robust sigma,
    hot_min LSB10). Dead/weak/bright pixels: flat gain off the median of its 8 same-colour
    neighbours by more than dead_frac. Returns a boolean (H, W) mask."""
    bad = np.zeros(dark10.shape, bool)
    for i in (0, 1):
        for j in (0, 1):
            d = dark10[i::2, j::2]
            med = float(np.median(d))
            sig = 1.4826 * float(np.median(np.abs(d - med)))
            bad[i::2, j::2] |= d > med + max(hot_sigma * sig, hot_min)
            g = flat_gain_map[i::2, j::2]
            p = np.pad(g, 1, mode="edge")
            nb = np.stack([p[1 + dy:1 + dy + g.shape[0], 1 + dx:1 + dx + g.shape[1]]
                           for dy in (-1, 0, 1) for dx in (-1, 0, 1) if dy or dx])
            loc = np.median(nb, 0)
            bad[i::2, j::2] |= np.abs(g / loc - 1) > dead_frac
    # rows 0-1 carry a constant (~134 LSB8) and rows 2-5 are optically black in every frame:
    # not image, never "defects"
    bad[:skip_rows] = False
    return bad


def fix_defects(cfa, bad):
    """Replace each flagged pixel by the mean of its good same-colour neighbours at +-2 px
    (horizontal and vertical); nothing else is touched."""
    out = cfa.copy()
    ys, xs = np.nonzero(bad)
    h, w = cfa.shape
    for y, x in zip(ys, xs):
        vals = [cfa[yy, xx] for yy, xx in ((y - 2, x), (y + 2, x), (y, x - 2), (y, x + 2))
                if 0 <= yy < h and 0 <= xx < w and not bad[yy, xx]]
        if vals:
            out[y, x] = float(np.mean(vals))
    return out
