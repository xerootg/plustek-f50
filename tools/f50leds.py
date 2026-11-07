#!/usr/bin/env python3
"""Step 10: switch the three backlight lines (firmware port D bits 0..2) one at a time.

  sudo python3 -I tools/f50leds.py [--exposure AEC] [--frames N]

Opcode 9 sub-commands 1, 2, 3 set port D bit 0, 1, 2 (block[5] = 1 on, 0 off; firmware
0x1e9b/0x1ea9/0x1eb7); sub-command 4 sets all three (what F50.lamp() uses). Prints the
per-CFA means (B, Gb, Gr, R, black-subtracted) for every single line and every pair, so
it shows whether the lines are separate LEDs and what colour each one is.
"""
import argparse
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import f50  # noqa: E402
import stack  # noqa: E402


def led(s, bit, on):
    s.cmd(f50.OP_START_STREAM, bytes([0, 0, bit + 1, 1 if on else 0]))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--exposure", type=int, default=6)
    ap.add_argument("--frames", type=int, default=4)
    args = ap.parse_args()
    s = f50.F50()
    s.raw_setup(exposure=args.exposure)
    res = {}
    try:
        for combo in ((), (0,), (1,), (2,), (0, 1), (0, 2), (1, 2), (0, 1, 2)):
            s.lamp(False)
            for b in combo:
                led(s, b, True)
            fr = s.frames(args.frames + 1)[1:].astype(np.float64).mean(0)
            m = [float(c.mean()) for c in stack.cfa_split(fr[stack.CTR[0]:stack.CTR[1], stack.CTR[2]:stack.CTR[3]])]
            res[combo] = m
    finally:
        s.lamp(True)
    dark = res[()]
    print("lines on      B       Gb      Gr      R      (8-bit, minus all-off)")
    for combo, m in res.items():
        d = [a - b for a, b in zip(m, dark)]
        print(f"{str(combo):12s} " + " ".join(f"{v:7.2f}" for v in (m if combo == () else d)))
    single = sum((np.array(res[(b,)]) - dark for b in (0, 1, 2)), np.zeros(4))
    allon = np.array(res[(0, 1, 2)]) - dark
    print("sum of singles vs all on:", np.round(single, 2), np.round(allon, 2))


if __name__ == "__main__":
    main()
