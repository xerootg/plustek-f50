#!/usr/bin/env python3
"""Render an F50 negative DNG with darktable's negadoctor, for comparison (PLAN step 17).

  .venv/bin/python tools/f50negadoctor.py IN.dng POSITIVE.json OUT.jpg [--dmax 1.6]

POSITIVE.json is the sidecar of tools/f50positive.py (film base RGB). Writes a copy of the
DNG with AsShotNeutral 1,1,1 (so darktable's white balance leaves the camera RGB alone) and
an XMP whose history holds only negadoctor (params v2: film_stock, Dmin[4], wb_high[4],
wb_low[4], D_max, offset, black, gamma, soft_clip, exposure; Dmin = film base / white),
then runs darktable-cli with the "none" workflow (no auto-applied filmic/sigmoid).
"""
import argparse
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tifffile  # noqa: E402
from dng import write_dng  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("dng")
    ap.add_argument("positive_json")
    ap.add_argument("out")
    ap.add_argument("--dmax", type=float, default=1.6)
    ap.add_argument("--width", type=int, default=0)
    args = ap.parse_args()
    with tifffile.TiffFile(args.dng) as t:
        page = t.pages[0]
        cfa = page.asarray()
        tags = {k: page.tags[k].value for k in (274, 50714, 50717) if k in page.tags}
    base = np.array(json.load(open(args.positive_json))["film_base_rgb_linear"]) / float(tags.get(50717, 65535))
    tmp = tempfile.mkdtemp(prefix="f50nd")
    dng = os.path.join(tmp, "nd.dng")
    write_dng(dng, cfa, black=(0, 0, 0, 0), white=int(tags.get(50717, 65535)), neutral=(1, 1, 1),
              orientation=int(tags.get(274, 1)))
    p = struct.pack("<i4f4f4f6f", 1, *base, 0.0, 1, 1, 1, 0, 1, 1, 1, 0, args.dmax, -0.05, 0.0755, 4.0, 0.75, 0.9245)
    xmp = f'''<?xml version="1.0" encoding="UTF-8"?>
<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
<rdf:Description rdf:about="" xmlns:darktable="http://darktable.sf.net/" darktable:xmp_version="5" darktable:history_end="1">
<darktable:history><rdf:Seq><rdf:li darktable:num="0" darktable:operation="negadoctor" darktable:enabled="1"
 darktable:modversion="2" darktable:params="{p.hex()}" darktable:multi_name="" darktable:multi_priority="0"/>
</rdf:Seq></darktable:history></rdf:Description></rdf:RDF></x:xmpmeta>
'''
    with open(dng + ".xmp", "w") as f:
        f.write(xmp)
    conf = os.path.join(tmp, "conf")
    os.makedirs(conf)
    with open(os.path.join(conf, "darktablerc"), "w") as f:
        f.write("plugins/darkroom/workflow=none\n")
    cmd = ["darktable-cli", dng, dng + ".xmp", os.path.abspath(args.out), "--core", "--configdir", conf,
           "--library", ":memory:"]
    if args.width:
        cmd[4:4] = ["--width", str(args.width)]
    subprocess.run(cmd, check=True)
    shutil.rmtree(tmp)


if __name__ == "__main__":
    main()
