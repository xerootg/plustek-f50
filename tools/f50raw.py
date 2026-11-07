#!/usr/bin/env python3
"""Capture, stack, calibrate and write raw Bayer DNGs from the Plustek SmartPhoto F50.

One command for a calibrated scan (PLAN.md step 15):

  sudo python3 -I tools/f50raw.py OUT --tenbit --frames 200 --calib [--exposure AEC]
                                      [--bracket "AEC[:CLKDIV[:GAIN]],..."]

Options:
  --frames N   stack N frames. Frames are accumulated as they arrive (tools/stack.py), so
               memory does not grow with N; short reads, misaligned frames and backlight
               outliers are rejected; noise versus N is reported.
  --tenbit     true 10-bit samples. One block of coarse frames (normal output, bits 9..2)
               seeds a reference; every other frame is read with the sensor's output bus
               swapped (register 0x54 bit 2: the 8 wired lines then carry bits 0..7,
               bit-reversed) and unwrapped against the running stack. Without it, frames
               are 8-bit (bits 9..2) and the stack relies on temporal noise to dither.
  --calib [DIR]  subtract the dark master for this exposure setting and divide by the flat
               master (frames/masters by default; build them with tools/f50master.py).
               10-bit masters are used when present, else 8-bit ones.
  --bracket SPEC exposure bracket, e.g. "6,1800,1800:3" (AEC lines[:CLKRC divider[:gain]]).
               Each step is stacked and dark-subtracted; exposure ratios are measured from
               the data; the merge weights each step by its exposure and drops pixels that
               clipped in any frame of that step.
  --dark       backlight off (raw dark capture, no calibration).

Output: OUT.dng (16-bit linear CFA, BGGR, Orientation 2 because the F50's image is mirrored
left-right), OUT.pgm (same samples, as stored), OUT.json (settings, frames used/rejected,
noise versus N, noise floor and dynamic range per channel, timing).
Sample scale: 10-bit units x 64 (single exposure) or the shortest bracket step's clip point
= 65535 (bracket). Calibrated output has black 0. Uncalibrated output keeps the sensor's
black level (pass --black-from DARK.json to tag it).
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
import stack  # noqa: E402
from dng import write_dng  # noqa: E402

MASTERS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "frames", "masters")
SCALE10 = 64                  # 10-bit units x 64 in the 16-bit output
REV8 = np.array([int(f"{i:08b}"[::-1], 2) for i in range(256)], np.uint8)
H, W = f50.WIN_HEIGHT, f50.WIN_WIDTH
CTR = stack.CTR


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
    return np.frombuffer(data, dt, w * h, pos).reshape(h, w).astype(np.float64), maxval


def cfa_means(a, y0=0, y1=None, x0=0, x1=None):
    """Mean of each 2x2 CFA position (row-major: (0,0) (0,1) (1,0) (1,1))."""
    c = a[y0:y1, x0:x1]
    return [float(c[i::2, j::2].mean()) for i in (0, 1) for j in (0, 1)]


def film_score(frame):
    """Coefficient of variation of 16x16-binned green. Bare backlight ~0.06, film > 0.15."""
    g = np.asarray(frame, np.float64)[0::2, 1::2]
    h, w = g.shape
    b = g[:h // 16 * 16, :w // 16 * 16].reshape(h // 16, 16, w // 16, 16).mean((1, 3))
    return float(b.std() / max(b.mean(), 1e-6))


def to10(a, info):
    """Master or stack mean -> 10-bit units."""
    return a if info.get("units", "").startswith("10") else a * 4 + 1.5


def load_master10(d, kind, exposure, clkdiv, gain):
    """10-bit master if present, else the 8-bit one; returns (mean10, info, name)."""
    for ten in (True, False):
        name = stack.master_name(kind, exposure, clkdiv, gain, ten)
        m, info = stack.load_master(d, name)
        if m is not None:
            return to10(m, info), info, name
    return None, None, None


def noise10(info):
    k = 1.0 if info.get("units", "").startswith("10") else 4.0
    return {c: v["frame_std"] * k for c, v in info["noise_final"].items()}


def flat_divisor(d):
    """Per-pixel flat divisor (1.0 per channel at the centre) and the backlight's per-channel
    signal (for AsShotNeutral), from the best flat master and its matching dark."""
    for ten in (True, False):
        fname = stack.find_flat(d, ten)
        if fname:
            break
    else:
        return None, None, None
    flat, finfo = stack.load_master(d, fname)
    flat = to10(flat, finfo)
    fdark, _, fdname = load_master10(d, "dark", finfo["exposure_lines"], finfo["clkdiv"], finfo["gain_reg"])
    if fdark is None:
        fdark = np.full_like(flat, 1.5)
    sig = flat - fdark
    fm = [float(c.mean()) for c in stack.cfa_split(sig[CTR[0]:CTR[1], CTR[2]:CTR[3]])]
    meta = {"flat": fname, "flat_frames": finfo["frames_used"], "flat_dark": fdname,
            "flat_note": "bare backlight, holder empty, captured 2026-11-07"}
    return stack.flat_gain(flat, fdark), fm, meta


def neutral_from(fm):
    nt = (fm[3], (fm[1] + fm[2]) / 2, fm[0])          # BGGR -> camera R, G, B
    return [round(v / max(nt), 4) for v in nt]


def capture_stack(s, args, exposure, clkdiv, gain, dark=False):
    """One stacked exposure: returns (mean10, clip mask, Stack)."""
    s.lamp(not dark)
    s.raw_setup(exposure=exposure, gain_reg=gain, clkdiv=clkdiv)
    try:
        if args.tenbit:
            st = stack.stack10(s, args.frames)
            clip = st.clip | (st.max >= 1020)
            return st.mean(), clip, st
        st = stack.stack8(s, args.frames)
        return st.mean() * 4 + 1.5, st.max == 255, st
    finally:
        if dark:
            s.lamp(True)


def merge_bracket(steps, lins, clips, log=print):
    """Linear HDR merge of dark-subtracted stacks. Returns radiance in units of the longest
    step, the relative exposure of each step (longest = 1), and the pixels clipped in every
    step (filled from the shortest)."""
    order = sorted(range(len(steps)), key=lambda i: float(lins[i][CTR[0]:CTR[1], CTR[2]:CTR[3]].mean()))
    rel = {order[0]: 1.0}
    for a, b in zip(order, order[1:]):
        ok = ~clips[a] & ~clips[b] & (lins[a] > 25) & (lins[b] < 900)
        if ok.sum() < 1000:
            ok = ~clips[b] & (lins[a] > 5)
        r = float(np.median(lins[b][ok] / lins[a][ok]))
        rel[b] = rel[a] * r
        log(f"  step {steps[b]} / {steps[a]}: measured exposure ratio {r:.3f} over {int(ok.sum())} px")
    top = max(rel.values())
    num = np.zeros_like(lins[0])
    den = np.zeros_like(lins[0])
    for i in range(len(steps)):
        w = rel[i] * (~clips[i])
        num += w * lins[i] * (top / rel[i])
        den += w
    fallback = den == 0
    num[fallback] = lins[order[0]][fallback] * top / rel[order[0]]
    den[fallback] = 1
    return num / den, {i: rel[i] / top for i in rel}, fallback


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("out")
    ap.add_argument("--exposure", type=int, default=6, help="AEC in sensor lines; effective exposure is AEC + ~388 lines for AEC >= 6")
    ap.add_argument("--gain", type=lambda x: int(x, 0), default=0, help="analog gain register 0x00 (0 = 1x)")
    ap.add_argument("--clkdiv", type=int, default=1, help="CLKRC divider: 1 (default, ~14 MB/s) or slower (longer line time)")
    ap.add_argument("--frames", type=int, default=1, help="frames to stack (per bracket step)")
    ap.add_argument("--tenbit", action="store_true", help="true 10-bit samples (bus-swap fine frames)")
    ap.add_argument("--bracket", help='exposure bracket "AEC[:CLKDIV[:GAIN]],..." merged to a 16-bit linear DNG')
    ap.add_argument("--calib", nargs="?", const=MASTERS, help=f"dark and flat masters directory (default {MASTERS})")
    ap.add_argument("--dark", action="store_true", help="backlight off (dark capture)")
    ap.add_argument("--black-from", help="uncalibrated output: tag the black level from a dark .json of this tool")
    ap.add_argument("--neutral-from", help="uncalibrated output: AsShotNeutral from a bare-backlight .json of this tool")
    args = ap.parse_args()
    if args.calib and args.dark:
        raise SystemExit("--calib and --dark do not go together")

    t0 = time.time()
    s = f50.F50()
    info = {"frames": args.frames, "tenbit": args.tenbit, "cfa": f50.CFA, "orientation": f50.ORIENTATION,
            "gain_reg": args.gain, "clkdiv": args.clkdiv, "dark": args.dark}
    steps = None
    if args.bracket:
        steps = []
        for item in args.bracket.split(","):
            p = item.split(":")
            steps.append((int(p[0]), int(p[1]) if len(p) > 1 else args.clkdiv, int(p[2], 0) if len(p) > 2 else args.gain))
    else:
        steps = [(args.exposure, args.clkdiv, args.gain)]
        info["exposure_lines"] = args.exposure

    lins, clips, per_step = [], [], {}
    for st_ in steps:
        m10, clip, st = capture_stack(s, args, *st_, dark=args.dark)
        summ = st.summary()
        rec = {"frames_used": st.n, "frames_seen": st.seen, "rejected": st.rejected,
               "capture_seconds": round(st.seconds, 1), "seconds_per_frame": round(st.seconds / max(st.n, 1), 3),
               "noise_vs_n": summ["noise_vs_n"], "noise_final": summ["noise_final"],
               "clipped_fraction": round(float(clip.mean()), 6), "film_score": round(film_score(m10), 3),
               "centre_cfa_10bit": [round(v, 2) for v in cfa_means(m10, *CTR)]}
        if summ["noise_final"]:
            low = [k for k, v in summ["noise_final"].items() if v["frame_std"] < (2.0 if args.tenbit else 0.5)]
            if low and not args.tenbit:
                rec["dither_warning"] = f"temporal noise < 0.5 LSB in {low}: the 8-bit mean is quantised"
        if args.calib:
            dark, dinfo, dname = load_master10(args.calib, "dark", *st_)
            if dark is None:
                raise SystemExit(f"no dark master for {st_}; build it with tools/f50master.py dark --tenbit "
                                 f"--exposure {st_[0]} --clkdiv {st_[1]} --gain {st_[2]}")
            lin = m10 - dark
            dn = noise10(dinfo)
            sn = {k: v["frame_std"] * (1 if args.tenbit else 4) for k, v in summ["noise_final"].items()} if summ["noise_final"] else dn
            # noise floor of the calibrated stack in the dark: the stack's own read noise
            # (taken from the dark master, same setting) plus the dark master's residual noise
            rec["dark_master"] = dname
            rec["floor_10bit"] = {k: round(float(dn[k] * np.sqrt(1.0 / st.n + 1.0 / dinfo["frames_used"])), 4) for k in dn}
            rec["clip_10bit"] = round(1023 - float(np.median(dark)), 1)
            rec["signal_noise_1frame_10bit"] = {k: round(v, 3) for k, v in sn.items()}
        else:
            lin = m10
        per_step[str(list(st_))] = rec
        lins.append(lin)
        clips.append(clip)
        print(f"step {st_}: {st.n} frames in {st.seconds:.0f} s, centre CFA (10-bit) {rec['centre_cfa_10bit']}, "
              f"clipped {rec['clipped_fraction']:.5f}, film score {rec['film_score']}, rejected {len(st.rejected)}")
    info["steps"] = per_step

    neutral = (1.0, 1.0, 1.0)
    if len(steps) > 1:
        rad, rel, fallback = merge_bracket(steps, lins, clips)
        info["relative_exposure"] = {str(list(steps[i])): round(v, 5) for i, v in rel.items()}
        info["clipped_in_all_steps"] = round(float(fallback.mean()), 6)
        shortest = min(rel, key=rel.get)
        clip_level = (per_step[str(list(steps[shortest]))].get("clip_10bit", 1023.0)) / rel[shortest]
        longest = max(rel, key=rel.get)
        floor = per_step[str(list(steps[longest]))].get("floor_10bit")
    else:
        rad = lins[0]
        rec = per_step[str(list(steps[0]))]
        clip_level = rec.get("clip_10bit", 1023.0)
        floor = rec.get("floor_10bit")

    if args.calib:
        g, fm, meta = flat_divisor(args.calib)
        if g is not None:
            rad = rad / g
            neutral = neutral_from(fm)
            info["calibration"] = meta
        else:
            info["calibration"] = {"flat_warning": "no flat master: vignetting and PRNU not corrected"}
        info["calibration"]["dark"] = "per-step dark master (see steps)"
        info["black"] = 0

    if floor:
        info["noise_floor_10bit"] = floor
        info["clip_level_10bit"] = round(clip_level, 1)
        info["dynamic_range_stops"] = {k: round(float(np.log2(clip_level / v)), 2) for k, v in floor.items()}

    # 16-bit output
    if len(steps) > 1:
        scale = 65535.0 / clip_level
        white = 65535
    else:
        scale = SCALE10
        white = int(min(65535, round(clip_level * SCALE10))) if args.calib else 1023 * SCALE10
    out16 = np.clip(np.rint(rad * scale), 0, 65535).astype(">u2")
    info["dng_scale_per_10bit"] = round(scale, 5)
    info["white"] = white
    black = (0, 0, 0, 0)
    if not args.calib:
        if args.black_from:
            b = json.load(open(args.black_from))["steps"]
            b = list(b.values())[0]["centre_cfa_10bit"]
            black = tuple(v * scale for v in b)
            info["black_level_dng"] = [round(v, 1) for v in black]
        if args.neutral_from:
            n = list(json.load(open(args.neutral_from))["steps"].values())[0]["centre_cfa_10bit"]
            bl = [v / scale for v in black]
            neutral = neutral_from([a - c for a, c in zip(n, bl)])
    info["as_shot_neutral_rgb"] = list(neutral)
    info["seconds_total"] = round(time.time() - t0, 1)

    f50.save_pgm(args.out + ".pgm", out16.tobytes(), W, H, 65535)
    write_dng(args.out + ".dng", out16.astype(np.uint16), pattern=f50.CFA, black=black, white=white,
              neutral=neutral, description=json.dumps({k: v for k, v in info.items() if k != "steps"}),
              orientation=f50.ORIENTATION)
    with open(args.out + ".json", "w") as f:
        json.dump(info, f, indent=1)
    short = {k: v for k, v in info.items() if k not in ("steps",)}
    print(json.dumps(short, indent=1))
    for k, rec in per_step.items():
        print(f"step {k}: noise vs N (stack standard error, Gb channel, {'10-bit' if args.tenbit else '8-bit'}): "
              + ", ".join(f"{n}:{v['Gb']['stack_se']:.3f}" for n, v in rec["noise_vs_n"].items()))
    print(f"total {info['seconds_total']} s")


if __name__ == "__main__":
    main()
