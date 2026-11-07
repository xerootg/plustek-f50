#!/usr/bin/env python3
"""Build a dark or flat calibration master by deep stacking (PLAN.md step 13).

  sudo python3 -I tools/f50master.py dark|flat [--exposure AEC] [--clkdiv N] [--gain REG]
                                               [--frames 200] [--dir frames/masters]

dark: backlight off, same exposure setting as the film capture it will correct.
flat: bare backlight, holder EMPTY (refuses if the frame looks like film), any exposure that
does not clip. Writes DIR/KIND_eAEC_cCLK_gGAIN.npy (float32 mean) and .json (settings,
frames used/rejected, per-channel noise versus N, capture time).
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import f50  # noqa: E402
import stack  # noqa: E402
from f50raw import film_score  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("kind", choices=("dark", "flat"))
    ap.add_argument("--exposure", type=int, default=6)
    ap.add_argument("--clkdiv", type=int, default=1)
    ap.add_argument("--gain", type=lambda x: int(x, 0), default=0)
    ap.add_argument("--frames", type=int, default=200)
    ap.add_argument("--tenbit", action="store_true", help="true 10-bit stack (bus-swap fine frames)")
    ap.add_argument("--dir", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "frames", "masters"))
    args = ap.parse_args()

    s = f50.F50()
    s.lamp(args.kind == "flat")
    s.raw_setup(exposure=args.exposure, gain_reg=args.gain, clkdiv=args.clkdiv)
    if args.kind == "flat":
        probe = s.frames(1)[0]
        fs = film_score(probe)
        print(f"film score {fs:.3f} (bare backlight ~0.06), max {probe.max()}")
        if fs > 0.15:
            raise SystemExit("holder is not empty: a flat needs the bare backlight")
        if (probe == 255).mean() > 1e-4:
            raise SystemExit("flat clips at this exposure; use a shorter one")
    try:
        st = stack.stack10(s, args.frames) if args.tenbit else stack.stack8(s, args.frames)
    finally:
        s.lamp(True)
    name = stack.master_name(args.kind, args.exposure, args.clkdiv, args.gain, args.tenbit)
    info = stack.save_master(args.dir, name, st, {
        "kind": args.kind, "exposure_lines": args.exposure, "clkdiv": args.clkdiv, "gain_reg": args.gain,
        "units": "10-bit" if args.tenbit else "8-bit (sensor bits 9..2)", "frames_requested": args.frames})
    print(json.dumps({k: v for k, v in info.items() if k != "noise_vs_n"}, indent=1))
    print("noise vs N (G on B row):", {k: v["Gb"]["stack_se"] for k, v in info["noise_vs_n"].items()})


if __name__ == "__main__":
    main()
