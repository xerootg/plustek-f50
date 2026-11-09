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
  --bracket SPEC exposure bracket, e.g. "2400::0:400,10764::0:200" (AEC lines[:CLKRC divider
               [:gain[:frames]]]; empty fields take the defaults). AEC beyond one frame works:
               raw_setup adds vertical blanking (DMLN) to fit it.
               Each step is stacked and dark-subtracted; exposure ratios are measured from
               the data; the merge weights each step by its exposure and drops pixels that
               clipped in any frame of that step.
  --dark       backlight off (raw dark capture, no calibration).

Output: OUT.dng (16-bit linear CFA, BGGR, TIFF Orientation from --orientation: whether the
image reads mirrored depends on which side of the film faces the sensor), OUT.pgm (same samples, as stored), OUT.json (settings, frames used/rejected,
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
SAFE10 = 750                  # bracket merge ignores a step where its stack mean exceeds this
                              # (pixels saturate a little below code 1023, and not all alike)
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


def nearest_dark10(d, exposure, clkdiv, gain):
    """Dark master for this setting, else the one with the nearest exposure at the same clock
    and gain. Measured: the 10-bit darks at AEC 6, 600 and 1800 differ by < 0.3 LSB10 (dark
    current is negligible at these exposures), so the nearest one is a sound substitute."""
    m, info, name = load_master10(d, "dark", exposure, clkdiv, gain)
    if m is not None or not os.path.isdir(d):
        return m, info, name, None
    best = None
    for fn in os.listdir(d):
        if fn.startswith("dark") and fn.endswith(f"_c{clkdiv}_g{gain}.json"):
            e = int(fn.split("_e")[1].split("_")[0])
            ten = fn.startswith("dark10")
            key = (not ten, abs(e - exposure))
            if best is None or key < best[0]:
                best = (key, e)
    if best is None:
        return None, None, None, None
    m, info, name = load_master10(d, "dark", best[1], clkdiv, gain)
    return m, info, name, f"no dark master at AEC {exposure}; used {name} (nearest exposure)"


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


def capture_stack(s, args, exposure, clkdiv, gain, dark=False, frames=None):
    """One stacked exposure: returns (mean10, clip mask, Stack)."""
    frames = frames or args.frames
    s.lamp(not dark)
    s.raw_setup(exposure=exposure, gain_reg=gain, clkdiv=clkdiv)
    try:
        if args.tenbit:
            st = stack.stack10(s, frames, dump=args.out)
            m = st.mean()
            clip = st.clip | (st.max >= 1020) | (m > SAFE10)
            return m, clip, st
        st = stack.stack8(s, frames)
        m = st.mean() * 4 + 1.5
        return m, (st.max == 255) | (m > SAFE10), st
    finally:
        if dark:
            s.lamp(True)


def merge_bracket(steps, lins, clips, log=print):
    """Linear HDR merge of dark-subtracted stacks.

    Exposure ratios come from low-level pixels only (longer step 30..250 LSB10, both steps
    unclipped). Each longer step's linear ceiling is then found per CFA channel: the level
    (25-LSB bins) above which its ratio to the next shorter step departs by more than 1.5 %
    from that exposure ratio. Measured on film: AEC 44220 saturates softly from ~400 LSB10,
    far below code 1023. Samples above a step's ceiling, or clipped, get weight 0; the
    others are weighted by exposure. Returns radiance in units of the longest step, the
    relative exposures (longest = 1), the pixels clipped in every step, and the ceilings."""
    order = sorted(range(len(steps)), key=lambda i: float(lins[i][CTR[0]:CTR[1], CTR[2]:CTR[3]].mean()))
    rel = {order[0]: 1.0}
    ceil = {order[0]: [SAFE10 - 30.0] * 4}
    masks = {i: clips[i].copy() for i in range(len(steps))}
    turn_fixes = {}
    for a, b in zip(order, order[1:]):
        la, lb = lins[a], lins[b]
        ok = ~masks[a] & ~masks[b] & (la > 5) & (lb > 30) & (lb < 250)
        r = float(np.median(lb[ok] / la[ok]))
        rel[b] = rel[a] * r
        # 10-bit unwrap check across steps: the longer step must equal r x the shorter one
        # within noise; a whole multiple of 256 off means its stack locked onto the wrong
        # turn for that pixel (seen on film before stack10 dropped its settling frames)
        pred = r * la
        okk = ~masks[a] & ~masks[b] & (la > 2) & (pred < 700)
        k = np.where(okk, np.rint((lb - pred) / 256.0), 0)
        fix = (k != 0) & (np.abs(lb - 256 * k - pred) < 40)
        if fix.any():
            lins[b] = lb = lb - 256 * k * fix
            turn_fixes[b] = int(fix.sum())
            log(f"  step {steps[b]}: {int(fix.sum())} pixels a whole 256-turn off the shorter step; corrected")
        cb = []
        for k, (i, j) in enumerate(((0, 0), (0, 1), (1, 0), (1, 1))):
            pa, pb = la[i::2, j::2], lb[i::2, j::2]
            okc = ~masks[a][i::2, j::2] & ~masks[b][i::2, j::2] & (pa > 5)
            ratio = pb[okc] / pa[okc]
            lev = pb[okc]
            c = 1023.0
            for lo_ in range(250, 1000, 25):
                sel = (lev >= lo_) & (lev < lo_ + 25)
                if sel.sum() < 200:
                    continue
                if abs(float(np.median(ratio[sel])) / r - 1) > 0.015:
                    c = float(lo_)
                    break
            cb.append(c)
            masks[b][i::2, j::2] |= pb > c
        ceil[b] = cb
        log(f"  step {steps[b]} / {steps[a]}: exposure ratio {r:.4f} over {int(ok.sum())} px; "
            f"linear ceiling per channel (B Gb Gr R) {cb}")
    top = max(rel.values())
    num = np.zeros_like(lins[0])
    den = np.zeros_like(lins[0])
    for i in range(len(steps)):
        w = rel[i] * (~masks[i])
        num += w * lins[i] * (top / rel[i])
        den += w
    fallback = den == 0
    num[fallback] = lins[order[0]][fallback] * top / rel[order[0]]
    den[fallback] = 1
    merge_bracket.turn_fixes = {str(list(steps[i])): v for i, v in turn_fixes.items()}
    return num / den, {i: rel[i] / top for i in rel}, fallback, {str(list(steps[i])): ceil[i] for i in ceil}


class SavedStack:
    """Stand-in for stack.Stack when reprocessing saved step stacks (--from-steps)."""

    def __init__(self, rec, mean):
        self.n, self.seen, self.rejected = rec["frames_used"], rec["frames_seen"], rec["rejected"]
        self.seconds = rec["capture_seconds"]
        self._rec, self._mean = rec, mean

    def summary(self):
        return {"noise_vs_n": self._rec["noise_vs_n"], "noise_final": self._rec["noise_final"]}

    def mean(self):
        return self._mean


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
    ap.add_argument("--from-steps", help="reprocess: load the stacks saved by --save-steps under this OUT "
                    "prefix (with its .json) instead of capturing")
    ap.add_argument("--positive", action=argparse.BooleanOptionalAction, default=None,
                    help="also write the inverted positive (OUT_pos.dng/.tif/.jpg) with tools/f50positive.py "
                         "in the repo's .venv; default: on for calibrated (--calib) scans")
    ap.add_argument("--orientation", type=int, default=f50.ORIENTATION,
                    help="TIFF Orientation for the DNG: 1 as stored, 2 mirrored, 6/8 rotated cw/ccw, "
                         "5/7 transposed (which applies depends on how the film sits in the holder)")
    ap.add_argument("--save-steps", action="store_true", help="also save each step's stack (float32 .npy) and clip mask")
    ap.add_argument("--no-defects", action="store_true", help="with --calib: do not replace hot/dead pixels")
    ap.add_argument("--black-from", help="uncalibrated output: tag the black level from a dark .json of this tool")
    ap.add_argument("--neutral-from", help="uncalibrated output: AsShotNeutral from a bare-backlight .json of this tool")
    args = ap.parse_args()
    if args.calib and args.dark:
        raise SystemExit("--calib and --dark do not go together")

    t0 = time.time()
    s = None if args.from_steps else f50.F50()
    prev = json.load(open(args.from_steps + ".json")) if args.from_steps else None
    info = {"frames": args.frames, "tenbit": args.tenbit, "cfa": f50.CFA, "orientation": args.orientation,
            "gain_reg": args.gain, "clkdiv": args.clkdiv, "dark": args.dark}
    steps = None
    step_frames = {}
    if args.bracket:
        steps = []
        for item in args.bracket.split(","):
            p = item.split(":")
            steps.append((int(p[0]), int(p[1]) if len(p) > 1 and p[1] else args.clkdiv,
                          int(p[2], 0) if len(p) > 2 and p[2] else args.gain))
            if len(p) > 3:
                step_frames[steps[-1]] = int(p[3])
    else:
        steps = [(args.exposure, args.clkdiv, args.gain)]
        info["exposure_lines"] = args.exposure

    lins, clips, per_step = [], [], {}
    for st_ in steps:
        if args.from_steps:
            k = len(lins)
            m10 = np.load(f"{args.from_steps}_step{k}_mean10.npy").astype(np.float64)
            clip = np.load(f"{args.from_steps}_step{k}_clip.npy")
            st = SavedStack(prev["steps"][str(list(st_))], m10)
        else:
            m10, clip, st = capture_stack(s, args, *st_, dark=args.dark, frames=step_frames.get(st_))
        if args.save_steps and not args.from_steps:
            np.save(f"{args.out}_step{len(lins)}_mean10.npy", m10.astype(np.float32))
            np.save(f"{args.out}_step{len(lins)}_clip.npy", clip)
        summ = st.summary()
        rec = {"frames_used": st.n, "frames_seen": st.seen, "rejected": st.rejected,
               "resyncs": getattr(st, "resyncs", 0), "stream_offsets": getattr(st, "offsets", []),
               "capture_seconds": round(st.seconds, 1), "seconds_per_frame": round(st.seconds / max(st.n, 1), 3),
               "noise_vs_n": summ["noise_vs_n"], "noise_final": summ["noise_final"],
               "clipped_fraction": round(float(clip.mean()), 6), "film_score": round(film_score(m10), 3),
               "centre_cfa_10bit": [round(v, 2) for v in cfa_means(m10, *CTR)]}
        if summ["noise_final"]:
            low = [k for k, v in summ["noise_final"].items() if v["frame_std"] < (2.0 if args.tenbit else 0.5)]
            if low and not args.tenbit:
                rec["dither_warning"] = f"temporal noise < 0.5 LSB in {low}: the 8-bit mean is quantised"
        if args.calib:
            dark, dinfo, dname, dnote = nearest_dark10(args.calib, *st_)
            if dnote:
                rec["dark_note"] = dnote
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
            rec["clip_10bit"] = round((SAFE10 if args.bracket else 1023) - float(np.median(dark)), 1)
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
        rad, rel, fallback, ceilings = merge_bracket(steps, lins, clips)
        info["linear_ceiling_10bit"] = ceilings
        info["unwrap_turns_corrected"] = getattr(merge_bracket, "turn_fixes", {})
        info["relative_exposure"] = {str(list(steps[i])): round(v, 5) for i, v in rel.items()}
        info["clipped_in_all_steps"] = round(float(fallback.mean()), 6)
        shortest = min(rel, key=rel.get)
        clip_level = min(ceilings[str(list(steps[shortest]))]) / rel[shortest]
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
        if not args.no_defects:
            longest_step = steps[int(np.argmax([float(np.mean(l[CTR[0]:CTR[1], CTR[2]:CTR[3]])) for l in lins]))]
            dk, _, dkn, _ = nearest_dark10(args.calib, *longest_step)
            bad = stack.defect_map(dk, g if g is not None else np.ones_like(dk))
            rad = stack.fix_defects(rad, bad)
            info["calibration"]["defects_replaced"] = int(bad.sum())
            info["calibration"]["defect_map_from"] = dkn
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
              orientation=args.orientation)
    with open(args.out + ".json", "w") as f:
        json.dump(info, f, indent=1)
    short = {k: v for k, v in info.items() if k not in ("steps",)}
    print(json.dumps(short, indent=1))
    for k, rec in per_step.items():
        print(f"step {k}: noise vs N (stack standard error, Gb channel, {'10-bit' if args.tenbit else '8-bit'}): "
              + ", ".join(f"{n}:{v['Gb']['stack_se']:.3f}" for n, v in rec["noise_vs_n"].items()))
    print(f"total {info['seconds_total']} s")
    if args.positive or (args.positive is None and args.calib):
        make_positive(args.out)


def make_positive(out):
    """Run tools/f50positive.py on OUT.dng with the repo's .venv (rawpy lives there, not in the
    system Python this tool runs under with sudo). Drops back to the invoking user so the
    outputs are not root-owned."""
    import subprocess
    here = os.path.dirname(os.path.abspath(__file__))
    py = os.path.join(here, "..", ".venv", "bin", "python")
    if not os.path.exists(py):
        print(f"positive: skipped, no {py}")
        return
    kw = {}
    if os.geteuid() == 0 and os.environ.get("SUDO_UID"):
        kw = dict(user=int(os.environ["SUDO_UID"]), group=int(os.environ["SUDO_GID"]))
        for ext in (".dng", ".json", ".pgm"):
            if os.path.exists(out + ext):
                os.chown(out + ext, kw["user"], kw["group"])
    r = subprocess.run([py, os.path.join(here, "f50positive.py"), out + ".dng", out + "_pos", "--demosaic", "dcb"],
                       capture_output=True, text=True, **kw)
    print(f"positive: {out}_pos.dng/.tif/.jpg" if r.returncode == 0 else f"positive: failed\n{r.stderr[-2000:]}")


if __name__ == "__main__":
    main()
