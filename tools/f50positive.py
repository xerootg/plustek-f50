#!/usr/bin/env python3
"""Turn a linear F50 negative DNG into a positive (PLAN.md step 17).

  .venv/bin/python tools/f50positive.py IN.dng OUT [--demosaic dcb|aahd|dht|ahd]
                     [--rotate cw|ccw|180|none] [--base X0,Y0,X1,Y1] [--crop X0,Y0,X1,Y1]
                     [--range STOPS] [--crops]

Steps (no spatial filtering besides the demosaic itself):
 1. Demosaic with LibRaw through rawpy, camera-linear RGB, 16-bit, gamma 1, no white
    balance, no auto-brightness, no colour matrix. rawpy applies the DNG's Orientation tag,
    so the F50's left-right mirror is corrected here (verify: sign text reads correctly).
 2. Film base: median per channel of the clear border (unexposed film between frames),
    found automatically as the brightest band of columns at the frame's side, or --base.
 3. Density per channel: D = log10(base / x). Frame area: the part that is neither holder
    (near black) nor film base, found from the profiles, or --crop.
 4. Per-channel density normalisation between the image's own black and white points
    (0.02 and 99.98 percentiles of D inside the frame): this equalises the three dye
    layers' contrast and neutralises the orange mask, as darktable's negadoctor does with
    Dmin, white and black.
 5. Dye separation (--separation A_RB,A_G): the density chroma is expanded around the
    neutral axis, A_RB along the warm-cool (red-blue) axis and A_G along the green-magenta
    axis. Under the F50's white LED backlight each sensor channel sees parts of two dye
    layers, and the green-magenta axis suffers most (measured on take7: chroma std 0.022
    warm-cool vs 0.0085 green-magenta, foliage came out teal-grey). 1.4,3.5 was tuned by
    eye on take7 (2026-11-08); a colour target would replace it with a measured matrix.
 6. Print: scene-linear positive L = mid * 10^(D'/gamma), black point at the shadow floor,
    Reinhard shoulder (white at the --white percentile). Written as a positive linear DNG
    (OUT.dng, LinearRaw, linear sRGB primaries, values 0..1 after the shoulder), 16-bit
    sRGB TIFF and sRGB JPEG.
Coordinates are in the demosaiced (mirror-corrected, unrotated) image.
"""
import argparse
import json
import os

import numpy as np
import rawpy
import tifffile
from PIL import Image

ALGOS = {"dcb": rawpy.DemosaicAlgorithm.DCB, "aahd": rawpy.DemosaicAlgorithm.AAHD,
         "dht": rawpy.DemosaicAlgorithm.DHT, "ahd": rawpy.DemosaicAlgorithm.AHD,
         "ppg": rawpy.DemosaicAlgorithm.PPG, "vng": rawpy.DemosaicAlgorithm.VNG}


def orientation(path):
    with tifffile.TiffFile(path) as t:
        tag = t.pages[0].tags.get(274)
        return int(tag.value) if tag else 1


def demosaic(path, algo):
    """Camera-linear RGB in display orientation. LibRaw's flip handling does not follow the
    TIFF meaning for the mirrored orientations, so LibRaw is told not to flip (user_flip=0)
    and the DNG's Orientation tag is applied here."""
    with rawpy.imread(path) as raw:
        kw = dict(user_flip=0, demosaic_algorithm=ALGOS[algo], output_color=rawpy.ColorSpace.raw, output_bps=16,
                  gamma=(1, 1), no_auto_bright=True, bright=1.0, use_camera_wb=False, use_auto_wb=False,
                  user_wb=[1.0, 1.0, 1.0, 1.0], highlight_mode=rawpy.HighlightMode.Clip,
                  fbdd_noise_reduction=rawpy.FBDDNoiseReductionMode.Off, median_filter_passes=0)
        if algo == "dcb":
            kw.update(dcb_enhance=True, dcb_iterations=2)
        img = raw.postprocess(**kw).astype(np.float64)
    return apply_orientation(img, orientation(path))


def apply_orientation(img, o):
    """TIFF Orientation 1-8 -> displayed image (LibRaw mishandles the mirrored ones)."""
    t = {1: lambda a: a, 2: lambda a: a[:, ::-1], 3: lambda a: a[::-1, ::-1], 4: lambda a: a[::-1],
         5: lambda a: a.swapaxes(0, 1), 6: lambda a: np.rot90(a, -1), 7: lambda a: np.rot90(a, 2).swapaxes(0, 1),
         8: lambda a: np.rot90(a, 1)}
    return t[o](img)


def find_regions(img, margin=140):
    """Frame box and film-base box. The film base (unexposed, clearest film) is the band of
    highest transmission next to the holder on any of the four sides; the frame box is what
    lies inside the holder and the base band."""
    g = img[..., 1]
    h, w = g.shape
    floor = np.percentile(g, 0.5)
    rows, cols = np.median(g, 1), np.median(g, 0)
    lit_r = np.nonzero(rows > floor + 0.08 * (np.percentile(rows, 99) - floor))[0]
    lit_c = np.nonzero(cols > floor + 0.08 * (np.percentile(cols, 99) - floor))[0]
    y0, y1, x0, x1 = int(lit_r[0]) + 4, int(lit_r[-1]) - 4, int(lit_c[0]) + 4, int(lit_c[-1]) - 4
    rows_in, cols_in = np.median(g[:, x0:x1], 1), np.median(g[y0:y1], 0)
    cands = {"top": (rows_in[y0:y0 + margin], "r", y0), "bottom": (rows_in[y1 - margin:y1], "r", y1 - margin),
             "left": (cols_in[x0:x0 + margin], "c", x0),
             "right": (cols_in[x1 - margin:x1], "c", x1 - margin)}
    best, bval = None, -1
    for k, (prof, kind, off) in cands.items():
        v = float(np.percentile(prof, 95))
        if v > bval:
            best, bval = k, v
    prof, kind, off = cands[best]
    idx = np.nonzero(prof >= 0.97 * np.percentile(prof, 95))[0]
    a, b = off + int(idx[0]) + 3, off + int(idx[-1]) - 3
    b = max(b, a + 2)
    if kind == "r":
        base_box = (x0 + 100, a, x1 - 100, b)
        frame_box = (x0, y0, x1, a - 10) if best == "bottom" else (x0, b + 10, x1, y1)
    else:
        base_box = (a, y0 + 60, b, y1 - 60)
        frame_box = (b + 10, y0, x1, y1) if best == "left" else (x0, y0, a - 10, y1)
    return base_box, frame_box, best


def srgb(x):
    x = np.clip(x, 0, 1)
    return np.where(x <= 0.0031308, 12.92 * x, 1.055 * np.power(x, 1 / 2.4) - 0.055)


def separate(dp, a_rb, a_g):
    """Expand density chroma around the neutral axis: gain a_rb on the red-blue axis, a_g on
    the green-magenta axis. Neutral pixels (equal density in all channels) are unchanged."""
    e_n = np.ones(3) / np.sqrt(3)
    e_rb = np.array([1.0, 0.0, -1.0]) / np.sqrt(2)
    e_g = np.array([-1.0, 2.0, -1.0]) / np.sqrt(6)
    m = np.outer(e_n, e_n) + a_rb * np.outer(e_rb, e_rb) + a_g * np.outer(e_g, e_g)
    return dp @ m.T, m


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("dng")
    ap.add_argument("out")
    ap.add_argument("--demosaic", default="dcb", choices=sorted(ALGOS))
    ap.add_argument("--rotate", default="none", choices=("cw", "ccw", "180", "none"))
    ap.add_argument("--base", help="film-base box X0,Y0,X1,Y1 (default: found automatically)")
    ap.add_argument("--crop", help="frame box X0,Y0,X1,Y1 (default: found automatically)")
    ap.add_argument("--film-gamma", type=float, default=0.6, help="negative contrast (density per log10 exposure)")
    ap.add_argument("--mid", type=float, default=0.18, help="linear value of the image's median")
    ap.add_argument("--lo", type=float, default=0.2, help="black-point percentile (of mean density)")
    ap.add_argument("--hi", type=float, default=99.0, help="highlight percentile used to balance the channels")
    ap.add_argument("--white", type=float, default=99.9, help="white-point percentile for the shoulder")
    ap.add_argument("--neutral", help="neutral reference box(es) X0,Y0,X1,Y1[;...] in output coordinates "
                    "(before --rotate): per-channel density scaled so they come out grey")
    ap.add_argument("--neutral-value", type=float, default=0.18, help="linear value of the neutral patch")
    ap.add_argument("--curve", type=float, default=0.0, help="gentle S-curve strength (0 = none, ~0.2 mild)")
    ap.add_argument("--separation", default="1.4,3.5",
                    help="dye separation A_RB,A_G: density chroma gain on the warm-cool and green-magenta axes (1,1 = off)")
    ap.add_argument("--no-dng", action="store_true", help="do not write the positive DNG")
    ap.add_argument("--crops", action="store_true", help="also write 100%% crops (JPEG) of 3 regions")
    args = ap.parse_args()

    img = demosaic(args.dng, args.demosaic)
    base_box, frame_box, side = find_regions(img)
    if args.base:
        base_box = tuple(int(v) for v in args.base.split(","))
    if args.crop:
        frame_box = tuple(int(v) for v in args.crop.split(","))
    bx0, by0, bx1, by1 = base_box
    base = np.median(img[by0:by1, bx0:bx1].reshape(-1, 3), 0)
    fx0, fy0, fx1, fy1 = frame_box
    neg = img[fy0:fy1, fx0:fx1]
    d = np.log10(base / np.maximum(neg, 1.0))
    valid = (d < 2.7).all(-1) & (d > -0.5).all(-1)
    gamma = args.film_gamma
    a_sep = tuple(float(v) for v in args.separation.split(","))
    neutral_info = None
    if args.neutral:
        # neutral-reference model (negadoctor-like): film base is density 0 in every channel
        # (it prints as black), and the per-channel density scale makes the chosen scene
        # patch(es) neutral; the patch mean is placed at --neutral-value (linear)
        boxes = [tuple(int(v) for v in b.split(",")) for b in args.neutral.split(";")]
        pts = np.concatenate([d[y0:y1, x0:x1].reshape(-1, 3) for x0, y0, x1, y1 in boxes])
        dn = np.median(pts, 0)
        slope = dn.mean() / dn
        dp, sep_m = separate(d * slope, *a_sep)
        med = hi = dn
        lin = args.neutral_value * np.power(10.0, (dp - dn.mean()) / gamma)
        lb = args.neutral_value * 10 ** ((np.percentile(dp.mean(-1)[valid], args.lo) - dn.mean()) / gamma)
        lin = np.clip((lin - lb) / (1 - lb), 0, None)
        wpt = args.neutral_value * 10 ** ((np.percentile(dp.mean(-1)[valid], args.white) - dn.mean()) / gamma)
        wpt = max(wpt, 1.0)
        neutral_info = {"boxes": boxes, "patch_density": [round(float(v), 4) for v in dn]}
    else:
        # neutral axis: per-channel density is shifted so the image median is 0 in every
        # channel (grey world at the midtones) and scaled so the --hi percentile (highlights,
        # dense on the negative) is also equal across channels
        med = np.array([np.median(d[..., c][valid]) for c in range(3)])
        hi = np.array([np.percentile(d[..., c][valid], args.hi) for c in range(3)])
        slope = (hi - med).mean() / (hi - med)
        dp, sep_m = separate((d - med) * slope, *a_sep)
        # print: scene-linear L = mid_grey * 10^(D'/gamma); black point at the film's shadow
        # floor; Reinhard shoulder with white at the --white percentile
        lin = args.mid * np.power(10.0, dp / gamma)
        lb = args.mid * 10 ** (np.percentile(dp.mean(-1)[valid], args.lo) / gamma)
        lin = np.clip((lin - lb) / (1 - lb), 0, None)
        wpt = args.mid * 10 ** (np.percentile(dp.mean(-1)[valid], args.white) / gamma)
    lin = lin * (1 + lin / wpt ** 2) / (1 + lin)
    rot = {"cw": -1, "ccw": 1, "180": 2, "none": 0}[args.rotate]
    if not args.no_dng:
        import dng
        pos = np.rot90(lin, rot) if rot else lin
        dng.write_linear_dng(args.out + ".dng", np.clip(np.rint(pos * 65535), 0, 65535).astype(np.uint16),
                             description=f"positive of {os.path.basename(args.dng)}")
    if args.curve:
        # gentle display contrast: a mild S on the sRGB-encoded value (0 and 1 fixed)
        e = srgb(lin)
        out = np.clip(e + args.curve * e * (1 - e) * (2 * e - 1) * 2, 0, 1)
    else:
        out = srgb(lin)
    if rot:
        out = np.rot90(out, rot)
    out16 = np.clip(np.rint(out * 65535), 0, 65535).astype(np.uint16)
    tifffile.imwrite(args.out + ".tif", out16, photometric="rgb",
                     metadata={"Software": "plustek-f50 tools/f50positive.py"})
    im8 = Image.fromarray(np.clip(np.rint(out * 255), 0, 255).astype(np.uint8))
    im8.save(args.out + ".jpg", quality=95)
    info = {"dng": args.dng, "demosaic": args.demosaic, "film_base_side": side, "base_box": base_box,
            "film_base_rgb_linear": [round(float(v), 1) for v in base], "frame_box": frame_box,
            "density_median": [round(float(v), 4) for v in med], "density_highlight": [round(float(v), 4) for v in hi],
            "channel_slope": [round(float(v), 4) for v in slope], "film_gamma": gamma, "mid_grey": args.mid,
            "black_lin": round(float(lb), 5), "white_lin": round(float(wpt), 3),
            "neutral": neutral_info, "curve": args.curve,
            "separation": list(a_sep), "separation_matrix": np.round(sep_m, 4).tolist(),
            "positive_dng": None if args.no_dng else args.out + ".dng",
            "rotate": args.rotate, "size": list(out16.shape)}
    if args.crops:
        h, w = out16.shape[:2]
        for name, (cy, cx) in {"centre": (h // 2, w // 2), "upper": (h // 4, w // 2), "lower": (3 * h // 4, w // 3)}.items():
            y0, x0 = max(0, cy - 256), max(0, cx - 384)
            im8.crop((x0, y0, x0 + 768, y0 + 512)).save(f"{args.out}_crop_{name}.jpg", quality=95)
    with open(args.out + ".json", "w") as f:
        json.dump(info, f, indent=1)
    print(json.dumps(info, indent=1))


if __name__ == "__main__":
    main()
