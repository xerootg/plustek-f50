#!/usr/bin/env python3
"""Sweep OV5620 register bits looking for the undocumented MSB/LSB bus swap.

  sudo python3 -I tools/f50sweep.py LOG.jsonl [--phase A|B|AB] [--regs 0x0c,0x85] [--values]

The F50 only wires the sensor's Y9..Y2 pins to its 8-bit FIFO. The datasheet ("MSB/LSB
Swap", Figure 12) says a control register reverses the 10 output lines; with it set, pins
Y9..Y2 carry data bits 0..7, so each byte read becomes bitrev8(v10 & 0xff). This tool
toggles one bit at a time (XOR against the current value), grabs two frames, restores the
register, and scores the second frame against a baseline mean b (8-bit, = v10 / 4):

  rev2   circular match of bitrev8(f) >> 2 with b mod 64  (the swap; 1.0 = hit, ~0 = none)
  plain2 circular match of f >> 2 with b mod 64             (a plain 2-bit shift, f = v10 & 0xff)
  rev1 / plain1   the same for a 1-bit shift (period 128)
  dmean  mean(f) - mean(b) over the centre crop

Phase A: reserved bits inside documented registers. Phase B: every bit of every reserved
register. Read-only bits (write does not stick) are logged and skipped. Never touches 0x3e/0x3f
(writing 0x3e = 0x17 hung the scanner until a replug),
0x12 bit 7 (soft reset), 0x09 bits 2 and 4 (PWDN pin function, sleep) or the ID registers.
Sensor writes are volatile; a USB replug restores the boot state.
"""
import argparse
import json
import math
import os
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import f50  # noqa: E402

REV8 = np.array([int(f"{i:08b}"[::-1], 2) for i in range(256)], np.uint8)

# reserved bits of documented registers (datasheet v1.3 Table 11)
PHASE_A = {
    0x00: 0x80, 0x03: 0x30, 0x04: 0x38, 0x09: 0xE8, 0x0C: 0x30, 0x0D: 0xF9, 0x0E: 0xFF,
    0x0F: 0xFD, 0x11: 0x80, 0x12: 0x07, 0x13: 0x78, 0x14: 0x19, 0x15: 0xC4, 0x48: 0xFC,
    0x5F: 0xC8, 0x63: 0x80, 0x80: 0xEC, 0x81: 0xF0, 0x83: 0xFC, 0x84: 0xFF, 0x85: 0xFF,
    0x89: 0xDF, 0x8B: 0xDF,
}
RESERVED = (list(range(0x05, 0x09)) + list(range(0x1E, 0x24)) + list(range(0x27, 0x2A)) + [0x2C]
            + list(range(0x33, 0x45)) + list(range(0x4A, 0x5F)) + [0x62] + list(range(0x64, 0x80))
            + [0x82] + list(range(0x86, 0x89)) + [0x8A] + list(range(0x8C, 0xA8))
            + list(range(0xAA, 0xB8)) + list(range(0xBB, 0x100)))
NEVER = {0x12: 0x80, 0x09: 0x14, 0x3E: 0xFF, 0x3F: 0xFF, 0x0A: 0xFF, 0x0B: 0xFF, 0x1C: 0xFF, 0x1D: 0xFF}

Y0, Y1, X0, X1 = 772, 1172, 996, 1596   # centre crop (even offsets keep the CFA phase)


def circ(x, b, period):
    return float(np.mean(np.cos(2 * math.pi * (x - b) / period)))


def score(f, b):
    fc = f[Y0:Y1, X0:X1]
    bc = b[Y0:Y1, X0:X1]
    r = REV8[fc].astype(np.float64)
    p = fc.astype(np.float64)
    return {
        "mean": round(float(p.mean()), 2), "std": round(float(p.std()), 2),
        "dmean": round(float(p.mean() - bc.mean()), 2),
        "sat": round(float((fc == 255).mean()), 4), "zero": round(float((fc == 0).mean()), 4),
        "rev2": round(circ(np.floor(r / 4), bc, 64), 3), "plain2": round(circ(np.floor(p / 4), bc, 64), 3),
        "rev1": round(circ(np.floor(r / 2), bc, 128), 3), "plain1": round(circ(np.floor(p / 2), bc, 128), 3),
        "corr": round(float(np.corrcoef(p.ravel(), bc.ravel())[0, 1]) if p.std() > 0 else 0.0, 3),
    }


def grab(s, n=2):
    data = s.capture(f50.FRAME_BYTES * n, timeout=4000, arm_timeout=3.0, log=lambda *_: None)
    if len(data) != f50.FRAME_BYTES * n:
        return None
    return np.frombuffer(data, np.uint8).reshape(n, f50.WIN_HEIGHT, f50.WIN_WIDTH)


def candidates(phase, only):
    out = []
    if only:
        for a in only:
            out += [(a, 1 << k) for k in range(7, -1, -1)]
        return out
    if "A" in phase:
        for a, mask in PHASE_A.items():
            out += [(a, 1 << k) for k in range(7, -1, -1) if mask & (1 << k)]
    if "B" in phase:
        for a in RESERVED:
            out += [(a, 1 << k) for k in range(7, -1, -1)]
    return [(a, m) for a, m in out if not NEVER.get(a, 0) & m]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("log")
    ap.add_argument("--phase", default="AB")
    ap.add_argument("--regs", help="comma list: sweep every bit of these registers only")
    ap.add_argument("--start", type=lambda x: int(x, 0), default=0, help="skip registers below this")
    ap.add_argument("--skip", type=int, default=0, help="resume: skip the first N candidates")
    ap.add_argument("--exposure", type=int, default=6)
    args = ap.parse_args()
    only = [int(x, 0) for x in args.regs.split(",")] if args.regs else None

    s = f50.F50()
    s.lamp(True)
    s.raw_setup(exposure=args.exposure)
    regs0 = s.read_regs(list(range(256)))
    base = grab(s, 3)
    b = base.astype(np.float64).mean(0)
    log = open(args.log, "a")

    def emit(rec):
        log.write(json.dumps(rec) + "\n")
        log.flush()

    emit({"baseline": True, "t": time.time(), "regs": [regs0[a] for a in range(256)], **score(base[0], b)})
    print("baseline", score(base[0], b), flush=True)
    cands = [(a, m) for a, m in candidates(args.phase, only) if a >= args.start][args.skip:]
    print(f"{len(cands)} candidate bits", flush=True)
    t0 = time.time()
    for i, (a, m) in enumerate(cands):
        orig = s.read_regs([a])[a]
        val = orig ^ m
        s.write_regs([(a, val)])
        back = s.read_regs([a])[a]
        rec = {"reg": a, "bit": m, "orig": orig, "val": val, "back": back}
        if back == orig:
            rec["ro"] = True
            emit(rec)
            continue
        time.sleep(0.05)
        fr = grab(s, 2)
        s.write_regs([(a, orig)])
        if fr is None:
            rec["fail"] = True
            emit(rec)
            print(f"{a:#04x} bit {m:#04x}: capture failed", flush=True)
            time.sleep(0.5)
            chk = grab(s, 2)
            if chk is None:
                # put every register we know back the way it was
                s.write_regs([(k, regs0[k]) for k in range(256) if k not in (0x0A, 0x0B, 0x1C, 0x1D, 0x2F)
                              and k not in range(0x05, 0x09)])
                time.sleep(0.5)
                chk = grab(s, 2)
                emit({"recover": a, "ok": chk is not None})
                if chk is None:
                    print("sensor did not recover; stopping (a USB replug resets it)", flush=True)
                    break
            continue
        rec["f0"] = score(fr[0], b)
        rec["f1"] = score(fr[1], b)
        emit(rec)
        sc = rec["f1"]
        flag = max(abs(sc["rev2"]), abs(sc["plain2"]), abs(sc["rev1"]), abs(sc["plain1"])) > 0.5 or abs(sc["dmean"]) > 3
        if flag or i % 25 == 0:
            el = time.time() - t0
            print(f"[{i + 1}/{len(cands)} {el:.0f}s] {a:#04x} ^{m:#04x}: {sc}" + ("  <==" if flag else ""), flush=True)
        if i % 50 == 49:
            # health check, and a fresh baseline in case the film was moved
            chk = grab(s, 3)
            emit({"health": True, "after": a, **(score(chk[1], b) if chk is not None else {"fail": True})})
            if chk is not None:
                b = chk.astype(np.float64).mean(0)
    log.close()


if __name__ == "__main__":
    main()
