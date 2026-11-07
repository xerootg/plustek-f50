<!-- Produced by the RE workbench (Kimi K3) from the Windows driver binaries and the
device firmware, 2026-11-06/07. Labels: proven / inferred / guess as written by the
workbench. Read 06-corrections-and-gaps.md for the review pass; where this file and
firmware.md disagree, firmware.md wins, and where firmware.md and 06 disagree, 06 wins. -->

# 4. Bulk image data format

## 4.1 On the wire (EP 0x82 bulk IN) [proven]

- One frame = **exactly 0x4ce300 = 5,038,848 bytes = 2592 x 1944**,
  one byte per pixel, 8-bit raw Bayer (the DLL allocates exactly w*h
  (`FUN_100053b0`), DoCaptureImage streams exactly that many bytes, and the
  DLL's de-mosaic `FUN_10004780`/`FUN_10004330` consumes it as a
  2592 x 1944 (`0xa20` x `0x798`) Bayer mosaic).
- No line headers, markers, or framing: a flat byte stream, raster order
  (row-major, top to bottom). Frame-complete = byte count reached; the
  read helpers (`FUN_1000b9e0`, `fcn.00401b70`) loop until the requested
  size is consumed or a WDU timeout (30 s) fires. [proven]
- The stream engine is started by opcode 9 (see 03) and re-armed per frame
  by opcode 5 + `WDU_ResetPipe(0x82)` inside DoCaptureImage. After the
  frame, opcode 7 clears the stream flag. [proven]
- Bit depth on the wire is always 8 bit/photosite. The DLL's 16-bit output
  modes (`DAT_10017fc3 = 0xc`) are produced host-side from the 8-bit data.
  [proven: frame size would double otherwise]
- Bayer color filter order: not determinable from the code (the de-mosaic
  is a symmetric neighbor average); OV5620 default is RG/GB with (0,0)=R.
  [guess — verify with one real scan of a known color target]
- Calibration reads only 0x4ce000 bytes (frame minus 768): 0x4ce000 is the
  frame size rounded down to a 512-byte USB packet multiple; the last 768
  bytes are ignored for statistics. [proven constants; reason inferred]

## 4.2 De-mosaic and color split (host side) [proven]

Two converters, both operate on the 2592x1944 Bayer frame:

- `FUN_10004780` (normal path, `DAT_10017fc2 != 4`): classic Bayer
  interpolation — for each photosite, the two missing color components are
  the average of the 4 same-parity diagonal neighbors
  (`(a+b+c+d) >> 2`), with edge parity handled by the
  `local_c & 1` / `uVar3 & 1` row/column tests; output = 3 separate planes
  (R,G,B) of 2592x1944 in the 15 MB buffer (`0xe6a900`).
- `FUN_10004330` (`DAT_10017fc2 == 4`): a 2x2-averaging variant producing
  half-resolution RGB (averages each 2x2 Bayer cell with neighbors before
  channel assignment) — used for the "4-frame" pixel mode.
- Black-white mode uses threshold LUTs at `DAT_100182d0`/`DAT_100183d0`
  built by `FUN_100062b0` from `DAT_10017fc1` (threshold from the B&W
  point formula in op 0x0e); gray/color line converters are the function
  pointers `DAT_10017eac` (`LAB_100066e0`, `LAB_10006990`, ... selected in
  `FUN_10009ab0` by bit depth and channel order).

## 4.3 Line splitting, dpi and sizes [proven]

- The sensor always delivers 2592x1944 @ ~1800 dpi native
  (2592 px / 36.6 mm film width; `DeviceCaps`/`SupportDpiX` lists in
  Reg.ini give the supported set; `MaxDpi` clamp 0x4b00).
- Requested-lower resolutions: `FUN_10009fc0` snaps the request to a
  supported dpi (largest supported <= requested), computes
  `_DAT_10017fc8 = supported/requested`, and the DLL decimates lines and
  pixels host-side. **Nothing resolution-dependent is ever sent to the
  device** — the only per-scan device traffic is the opcode-3 window words
  (informational; the firmware does not act on them — handler 0x0272 is a
  pure ack) and the fixed register programming. [proven]
- Line length math for output: `FUN_10009710`
  (`bytes_per_line = px * {1,2,3}` by pixel type, B&W packed /8, optional
  4-byte align when flag bit 3 set).
- Per-frame read chunking: the DLL caps each pass at
  `BufferSize MB` (Reg.ini, 4) of lines (`FUN_10009ab0`); DoCaptureImage
  itself always delivers a complete frame.

## 4.4 Positive.map / Negative.map

These files are **not referenced by Scandrv.dll** — the only
"Positive"/"Negative" strings in the binary are Reg.ini key name fragments
(`UpPositive`, `LeftNegative`, ... holder-offset calibration keys, read by
`FUN_10008d20`, and cleared by `FUN_10009030`). The tone mapping for
positive/negative film is applied from host-side gamma/LUT tables delivered
through ScannerCommander case 8 (`FUN_100011e0`/`FUN_100012d0` build the
channel LUTs from caller-supplied tables; the tables themselves live in the
TWAIN data source layer, not in the USB protocol). [proven by string and
xref search]
