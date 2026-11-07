#!/usr/bin/env python3
"""Summarise an f50sweep.py log: python3 -I tools/sweepsum.py LOG.jsonl"""
import json
import sys

recs = [json.loads(l) for l in open(sys.argv[1])]
tests = [r for r in recs if "reg" in r]
seen = {}
for r in tests:
    seen[(r["reg"], r["bit"])] = r          # a retest replaces an earlier record
ro = [k for k, r in seen.items() if r.get("ro")]
fail = [k for k, r in seen.items() if r.get("fail")]
ok = {k: r for k, r in seen.items() if "f1" in r}
regs = sorted({k[0] for k in seen})
print(f"{len(seen)} bits in {len(regs)} registers: {len(ok)} writable and captured, {len(ro)} read-only, {len(fail)} broke capture")
print("broke capture:", " ".join(f"{a:#04x}^{m:#04x}" for a, m in sorted(fail)))
best = max(max(abs(r["f1"][x]) for x in ("rev2", "plain2", "rev1")) for r in ok.values())
print(f"best |rev2|,|plain2|,|rev1| over all captured tests: {best:.3f} (hit would be > 0.8)")
wide = [(k, r["f1"]) for k, r in ok.items() if r["f1"]["std"] > 45]
print("frames with std > 45 (LSB-like spread):", " ".join(f"{a:#04x}^{m:#04x}(mean {f['mean']}, std {f['std']}, corr {f['corr']})" for (a, m), f in sorted(wide)) or "none")
chg = sorted({a for (a, m), r in ok.items() if abs(r["f1"]["dmean"]) > 5 or r["f1"]["corr"] < 0.8})
print("registers with a visible effect (|dmean|>5 or corr<0.8):", " ".join(f"{a:#04x}" for a in chg))
