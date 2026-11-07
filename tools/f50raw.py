#!/usr/bin/env python3
"""Capture raw Bayer frames from the Plustek SmartPhoto F50.

  sudo python3 -I tools/f50raw.py OUT [--exposure LINES] [--gain REG] [--clkdiv N]
                                      [--frames N] [--tenbit] [--bracket SPEC]
                                      [--dark] [--black-from DARK.json|DARK.pgm]
                                      [--neutral-from FLAT.json|FLAT.pgm] [--save-frames]

Modes:
  default   mean of N 8-bit frames (the sensor's bits 9..2). OUT.pgm/OUT.dng hold mean x 256,
            white 65280.
  --tenbit  true 10-bit samples from frame pairs: N coarse frames (bits 9..2) and N fine
            frames with the sensor's output bus swapped (the 8 wired lines then carry bits
            0..7, bit-reversed). Each fine frame is unwrapped against the coarse mean:
            v = f + 256*k with k chosen so v is closest to 4*coarse. OUT.pgm/OUT.dng hold the
            sum of the N 10-bit frames, white 1023*N.
  --bracket "AEC[:CLKDIV[:GAIN]],..."   exposure bracket, e.g. "6,600,1800:3". Each step
            is captured (10-bit when --tenbit, else 8-bit), dark-subtracted with a lamp-off
            capture at the same settings, scaled by the exposure ratio measured from the
            data, and merged with weights proportional to exposure, dropping clipped samples.
            OUT.dng is 16-bit linear, black 0, white 65535 = the shortest step's clip point.

OUT.json records settings, statistics and the per-CFA black level in 10-bit units, which a
later --black-from accepts in any mode. --dark switches the backlight off. The DNGs carry
Orientation = 2 (the F50's image is mirrored left-right). Sensor settings are volatile; a
replug restores the scanner's boot state.
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
REV8 = np.array([int(f"{i:08b}"[::-1], 2) for i in range(256)], np.uint8)
H, W = f50.WIN_HEIGHT, f50.WIN_WIDTH
CTR = (H // 2 - 200, H // 2 + 200, W // 2 - 300, W // 2 + 300)


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


def ref_cfa_10bit(path, ctr=None):
    """Per-CFA means of a reference capture, in 10-bit units per frame."""
    if path.endswith(".json"):
        info = json.load(open(path))
        return info["centre_cfa_10bit" if ctr else "cfa_10bit"]
    a = read_pgm16(path) / SCALE * 4 + 1.5    # legacy 8-bit-mode PGM (mean x 256)
    return cfa_means(a, *ctr) if ctr else cfa_means(a)


def film_score(frame):
    """Coefficient of variation of 16x16-binned green. Bare backlight ~0.06, film > 0.15."""
    g = frame[0::2, 1::2].astype(np.float64)
    h, w = g.shape
    b = g[:h // 16 * 16, :w // 16 * 16].reshape(h // 16, 16, w // 16, 16).mean((1, 3))
    return float(b.std() / max(b.mean(), 1e-6))


# ---- capture modes; all return 10-bit-unit float arrays ----

def grab8(s, n):
    """n 8-bit frames as uint8 (n, H, W)."""
    return s.frames(n)


def grab10(s, n, log=print):
    """n true 10-bit frames from coarse/fine pairs, as uint16 (n, H, W) in 0..1023.

    Coarse: normal output (bits 9..2). Fine: bus swapped; the wired lines carry bits 0..7
    (reversed when f50.SWAP_REVERSED). Each fine frame is unwrapped against the mean
    coarse frame. Clipped coarse pixels (255 in every frame) stay at 1023."""
    s.bus_swap(False)
    coarse = s.frames(n + 1)[1:]                # first frame after a register write may straddle it
    s.bus_swap(True)
    fine = s.frames(n + 1)[1:]
    s.bus_swap(False)
    c10 = coarse.astype(np.float64).mean(0) * 4 + 1.5
    out = np.empty(fine.shape, np.uint16)
    for i, f in enumerate(fine):
        lo = (REV8[f] if f50.SWAP_REVERSED else f).astype(np.float64)
        k = np.rint((c10 - lo) / 256.0)
        out[i] = np.clip(lo + 256 * k, 0, 1023)
    clip = (coarse == 255).all(0)
    out[:, clip] = 1023
    err = (out.astype(np.float64).mean(0) - c10)[CTR[0]:CTR[1], CTR[2]:CTR[3]]
    log(f"  10-bit: mean |fine - coarse| = {np.abs(err).mean():.2f} LSB10 (centre), max {np.abs(err).max():.1f}")
    return out, coarse


def parse_bracket(spec, clkdiv, gain):
    steps = []
    for item in spec.split(","):
        p = item.split(":")
        steps.append((int(p[0]), int(p[1]) if len(p) > 1 else clkdiv, int(p[2], 0) if len(p) > 2 else gain))
    return steps


def capture_mean10(s, args, exposure, clkdiv, gain, dark=False):
    """Mean of args.frames frames at one setting, in 10-bit units; plus a clip mask and the
    single-frame noise estimate."""
    s.lamp(not dark)
    s.raw_setup(exposure=exposure, gain_reg=gain, clkdiv=clkdiv)
    if args.tenbit:
        fr, _ = grab10(s, args.frames)
        a = fr.astype(np.float64)
        clip = (fr >= 1020).any(0)
    else:
        fr = grab8(s, args.frames)
        a = fr.astype(np.float64) * 4 + 1.5
        clip = (fr == 255).any(0)
    if dark:
        s.lamp(True)
    sig1 = None
    if len(a) >= 2:
        d = a[0] - a[1]
        sig1 = float(d[CTR[0]:CTR[1], CTR[2]:CTR[3]].std() / np.sqrt(2))
    return a.mean(0), clip, sig1, fr


def merge_bracket(steps, means, darks, clips, log=print):
    """Linear HDR merge. Returns radiance in units of the longest step (10-bit), the
    relative exposure of each step, and the shortest step's clip level in those units."""
    lin = [m - d for m, d in zip(means, darks)]
    order = sorted(range(len(steps)), key=lambda i: float(lin[i][CTR[0]:CTR[1], CTR[2]:CTR[3]].mean()))
    rel = {order[0]: 1.0}
    for a, b in zip(order, order[1:]):
        ok = ~clips[a] & ~clips[b] & (lin[a] > 25) & (lin[b] < 900)
        if ok.sum() < 1000:
            ok = ~clips[b] & (lin[a] > 5)
        r = float(np.median(lin[b][ok] / lin[a][ok]))
        rel[b] = rel[a] * r
        log(f"  step {steps[b]} / {steps[a]}: measured ratio {r:.3f} over {int(ok.sum())} px")
    top = max(rel.values())
    num = np.zeros_like(lin[0])
    den = np.zeros_like(lin[0])
    for i in range(len(steps)):
        w = rel[i] * (~clips[i])
        num += w * lin[i] * (top / rel[i])
        den += w
    short = order[0]
    # pixels clipped even in the shortest step: use it anyway (it is the best we have)
    fallback = den == 0
    num[fallback] = lin[short][fallback] * top
    den[fallback] = 1
    clip_level = (1023 - float(np.mean(darks[short]))) * top
    return num / den, {steps[i]: rel[i] / top for i in rel}, clip_level, fallback


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("out")
    ap.add_argument("--exposure", type=int, default=6, help="AEC in sensor lines; effective exposure is AEC + ~388 lines for AEC >= 6")
    ap.add_argument("--gain", type=lambda x: int(x, 0), default=0, help="analog gain register 0x00 (0 = 1x)")
    ap.add_argument("--clkdiv", type=int, default=1, help="CLKRC divider: 1 (default, ~14 MB/s) or slower (longer line time)")
    ap.add_argument("--frames", type=int, default=1, help="frames to average (per step)")
    ap.add_argument("--tenbit", action="store_true", help="true 10-bit samples from coarse/fine frame pairs")
    ap.add_argument("--bracket", help='exposure bracket "AEC[:CLKDIV[:GAIN]],..." merged to a 16-bit linear DNG')
    ap.add_argument("--dark-frames", type=int, default=4, help="lamp-off frames per bracket step")
    ap.add_argument("--dark", action="store_true", help="backlight off (dark frame)")
    ap.add_argument("--black-from", help="dark reference (.json from this tool, or legacy 16-bit .pgm)")
    ap.add_argument("--neutral-from", help="bare-backlight reference (.json or legacy .pgm): AsShotNeutral")
    ap.add_argument("--save-frames", action="store_true", help="also write the first raw frame (8-bit or 16-bit PGM)")
    args = ap.parse_args()
    if args.tenbit and f50.SWAP_REG is None:
        raise SystemExit("--tenbit needs the bus-swap register (f50.SWAP_REG), not known")

    s = f50.F50()
    t0 = time.time()
    info = {"gain_reg": args.gain, "clkdiv": args.clkdiv, "frames": args.frames, "tenbit": args.tenbit,
            "dark": args.dark, "cfa": f50.CFA, "orientation": f50.ORIENTATION}
    neutral_ref = args.neutral_from

    if args.bracket:
        steps = parse_bracket(args.bracket, args.clkdiv, args.gain)
        means, darks, clips, noise = [], [], [], {}
        dargs = argparse.Namespace(**{**vars(args), "frames": args.dark_frames})
        for st in steps:
            m, c, sig, fr = capture_mean10(s, args, *st)
            print(f"step {st}: centre CFA {[round(v, 1) for v in cfa_means(m, *CTR)]}, clipped {c.mean():.4f}, film {film_score(fr[0] if fr.dtype == np.uint8 else fr[0] >> 2):.3f}")
            d, _, dsig, _ = capture_mean10(s, dargs, *st, dark=True)
            means.append(m), darks.append(d), clips.append(c)
            noise[str(st)] = {"signal_noise_1frame_10bit": sig, "dark_noise_1frame_10bit": dsig,
                              "dark_cfa_10bit": [round(v, 2) for v in cfa_means(d)]}
        rad, rel, clip_level, fallback = merge_bracket(steps, means, darks, clips)
        scale = 65535.0 / clip_level
        out16 = np.clip(np.rint(rad * scale), 0, 65535).astype(">u2")
        black, white = (0, 0, 0, 0), 65535
        info.update({"bracket": [list(st) for st in steps], "relative_exposure": {str(k): round(v, 4) for k, v in rel.items()},
                     "noise": noise, "clip_level_longest_units": round(clip_level, 1), "dng_scale": round(scale, 4),
                     "clipped_in_all_steps": float(fallback.mean())})
        # engineering dynamic range: clip level over the merged dark noise, both in longest-step units
        longest = max(range(len(steps)), key=lambda i: rel[steps[i]])
        dn = noise[str(steps[longest])]["dark_noise_1frame_10bit"]
        if dn:
            floor = dn / np.sqrt(args.frames)
            info["dr_stops_clip_over_dark_noise"] = round(float(np.log2(clip_level / floor)), 2)
        cfa_mean = cfa_means(rad)
        ctr_mean = cfa_means(rad, *CTR)
    else:
        s.lamp(not args.dark)
        s.raw_setup(exposure=args.exposure, gain_reg=args.gain, clkdiv=args.clkdiv)
        info["exposure_lines"] = args.exposure
        if args.tenbit:
            fr, coarse = grab10(s, args.frames)
            acc = fr.astype(np.float64)
            out16 = fr.astype(np.uint32).sum(0).astype(">u2")
            white = 1023 * args.frames
            ten = acc.mean(0)
            info["clipped_fraction_frame0"] = float((fr[0] >= 1023).mean())
            if args.save_frames:
                f50.save_pgm(args.out + "_f0.pgm", fr[0].astype(">u2").tobytes(), W, H, 1023)
                f50.save_pgm(args.out + "_coarse0.pgm", coarse[0].tobytes(), W, H)
            to_out = lambda v: v * args.frames
        else:
            fr = grab8(s, args.frames)
            acc = fr.astype(np.float64)
            out16 = np.clip(np.rint(acc.mean(0) * SCALE), 0, 65535).astype(">u2")
            white = WHITE
            ten = acc.mean(0) * 4 + 1.5
            info["clipped_fraction_frame0"] = float((fr[0] == 255).mean())
            if args.save_frames:
                f50.save_pgm(args.out + "_f0.pgm", fr[0].tobytes(), W, H)
            to_out = lambda v: (v - 1.5) * SCALE / 4
        if args.dark:
            s.lamp(True)
        if args.frames >= 2:
            d = acc[0] - acc[1]
            sig1 = float(d[CTR[0]:CTR[1], CTR[2]:CTR[3]].std() / np.sqrt(2))
            unit = "10bit" if args.tenbit else "8bit"
            info[f"temporal_noise_1frame_{unit}"] = round(sig1, 4)
            info[f"temporal_noise_mean_{unit}_expected"] = round(sig1 / np.sqrt(args.frames), 4)
        info["film_score"] = round(film_score(fr[0] if fr.dtype == np.uint8 else fr[0] >> 2), 3)
        black = (0, 0, 0, 0)
        if args.black_from:
            black = tuple(to_out(v) for v in ref_cfa_10bit(args.black_from))
        cfa_mean, ctr_mean = cfa_means(ten), cfa_means(ten, *CTR)

    info["seconds"] = round(time.time() - t0, 2)
    info["white"] = white
    info["cfa_10bit"] = [round(v, 3) for v in cfa_mean]
    info["centre_cfa_10bit"] = [round(v, 3) for v in ctr_mean]
    if black != (0, 0, 0, 0):
        info["black_level_dng"] = [round(b, 1) for b in black]
    f50.save_pgm(args.out + ".pgm", out16.tobytes(), W, H, 65535)

    neutral = (1.0, 1.0, 1.0)
    if neutral_ref:
        m = ref_cfa_10bit(neutral_ref, CTR)
        blk = ref_cfa_10bit(args.black_from) if args.black_from else [0, 0, 0, 0]
        m = [v - b for v, b in zip(m, blk)]
        # BGGR: (0,0)=B, (0,1)=G, (1,0)=G, (1,1)=R
        neutral = (m[3], (m[1] + m[2]) / 2, m[0])
        info["as_shot_neutral_rgb"] = [round(v / max(neutral), 4) for v in neutral]
    write_dng(args.out + ".dng", out16.astype(np.uint16), pattern=f50.CFA, black=black, white=white,
              neutral=neutral, description=json.dumps(info), orientation=f50.ORIENTATION)
    with open(args.out + ".json", "w") as f:
        json.dump(info, f, indent=1)
    print(json.dumps(info, indent=1))


if __name__ == "__main__":
    main()
