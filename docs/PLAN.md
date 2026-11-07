# Plan: raw frames from the Plustek SmartPhoto F50 on Linux

Written 2026-11-06 after the first reverse-engineering session. Read
`NOTES.md` for the evidence behind each statement here.

## Where things are

| What | Where |
|---|---|
| This repo (public) | `~/repos/plustek-f50`, github.com/xerootg/plustek-f50 |
| Vendor packages, extracted driver binaries, Ghidra decompiles and projects | `~/repos/plustek-f50-re/` (untracked, private; do not commit anything from it) |
| Working driver scaffold | `tools/f50.py` (query opcodes, register dump, EEPROM guard) |
| Firmware parser / FX2 probe | `tools/fwinfo.py`, `tools/fx2probe.py` |
| Power-on sensor register dump | `docs/engine-regs-poweron.txt` |
| RE workbench job on `Scandrv.dll` | conversation `1965f3f6-4a56-4b33-9f52-9518800156bf`, staged files in `/projects/work/plustek-f50`, results land in the findings repo under `plustek-f50/` (`wb_findings`) |

## What we know (short)

- USB `07b3:0b00`. One vendor request (0x04): OUT wValue 0x0011/wIndex 1 = 64-byte
  command, IN wValue 0x0010/wIndex 0 = 64-byte reply. Opcodes 0x00..0x19.
  Working from Linux via pyusb (`tools/f50.py`).
- The USB chip is a Grandtech GT-6816 (8051, FX2-style register map, firmware
  already in its EEPROM). It relays I2C and pipes the sensor's parallel pixel
  bus into bulk EP 0x82 through an 8-bit slave FIFO (EP2FIFOCFG=0x4C: AUTOIN,
  8-bit wide, zero-length-in).
- The image source is an OmniVision **OV5620** 5 MP Bayer CMOS sensor at I2C
  0x30 (PID 0x5621, MID 0x7fa2). No motor. Film holder is indexed by hand.
- Opcodes: 0x0b write (reg,val) pairs to the sensor, 0x0c read, 0x09 start
  stream, 0x0e/0x17/0x07 status flags, 0x0a/0x19 mode variables, 0x13/0x15
  generic I2C and EEPROM page reads. **0x14 and 0x16 can rewrite the boot
  EEPROM: never send them.**

## Status (2026-11-06, end of second session)

| Step | State |
|---|---|
| 1. OV5620 register map | Done: `docs/OV5620.md` (datasheet v1.3 in the private RE folder) |
| 2. Windows sequences | Done: decoded from `WDUScan.exe` (sensor setup, capture) and the firmware's VSYNC ISR; see NOTES.md. Workbench run finished with matching results |
| 3. Capture path | Done: `F50.capture()`, `frames()`, `win_setup()`; Windows sequence replayed, 2592x1944x8-bit frames, byte count explained |
| 4. Raw mode | Done: `raw_setup()`; 8-bit raw is the floor (8-bit FIFO); manual exposure/gain, lamp on/off |
| 5. Output format | Done: 16-bit PGM and DNG with frame averaging (`tools/f50raw.py`, `tools/dng.py`) |
| 6. Validation | Partly: dark frame, flat frame, noise vs averaging done; real negative and slide still to do (holder was empty) |

Next: load film and run step 6; confirm B/R with a slide; see "Open questions" in NOTES.md.

## Steps

1. **OV5620 register map.** Get the OV5620 datasheet (version 1.3, 2007;
   mirrors: elite.parts, datasheetspdf.com, dtsheet.com) and save it under
   `~/repos/plustek-f50-re/packages/`. Extract: COM7 (0x12) output-format and
   reset bits, windowing (HSTART/HSTOP/VSTART/VSTOP and their LSB register),
   AEC (0x10, 0x04, 0x45), AGC (0x00), COM8 (0x13: AEC/AGC/AWB enables), clock
   PLL (0x11, 0x3d..), and the 8-bit-vs-10-bit output option. Write the
   subset we need into `docs/OV5620.md`.
2. **Windows sequences.** Poll the workbench (`wb_session`, `wb_findings
   plustek-f50/`). Needed from it: the DLL's sensor init list, the format it
   selects, the window per dpi, how ExposureTime=600 / Gain=120 become
   registers, frames per scan, expected bulk frame size. Copy the conclusions
   into `docs/NOTES.md` with their proven/inferred labels. If the workbench
   stalls, continue from `~/repos/plustek-f50-re/decompiled/scandrv_all.c`
   (the COM wrappers are FUN_1000bb60/bc30/be70/bf30/c000/c190).
3. **Driver, capture path.** Extend `tools/f50.py`:
   - `write_regs([(reg,val),...])` via 0x0b (max 29 pairs per block; check
     the reply status 0x1234); verify with a read-back.
   - `start_stream()` = opcode 0x09; `read_frame(nbytes)` = bulk 0x82 reads
     with a generous timeout; a stop path (likely 0x07 or 0x17, confirm from
     the DLL) and USB pipe reset on error.
   - First test: replay the DLL's init exactly, grab one frame at the DLL's
     settings, confirm the byte count matches width x height x bytes/pixel.
     Save as PGM. Only then change settings.
4. **Raw mode.** Switch COM7 to raw Bayer output, disable AEC/AGC/AWB in COM8,
   set manual exposure and gain 1x. Decide 8-bit vs 10-bit: with an 8-bit
   FIFO, 10-bit raw only works if the sensor can emit two bytes per pixel;
   otherwise 8-bit raw Bayer is the floor and we gain depth by averaging N
   frames and by exposure bracketing (dense negatives need long exposure).
5. **Output format.** Write frames as 16-bit PGM first, then as DNG
   (CFA pattern from the datasheet, black level from a dark frame with the
   backlight off, white balance from a blank-film frame) so RapidRAW and
   darktable open them.
6. **Validation.** Dark frame, flat frame (no film), a real negative, a slide.
   Check: no clipping at gain 1x, noise drops with frame averaging, colour
   invertible from the Bayer data.

## Rules for whoever does the work

- Commit to `~/repos/plustek-f50` as each step lands; push when it builds.
- Never send opcode 0x14 or 0x16. Sensor register writes are volatile (a USB
  replug restores defaults), so experimenting on the sensor is safe.
- Keep heavy jobs (Ghidra headless, builds) sequential; the laptop is in use.
- Don't ask the user questions mid-task; note open questions at the end of
  `docs/NOTES.md`.

## Phase 2 (added 2026-11-07): squeeze the sensor

7. **Find the OV5620 bus-swap register.** The datasheet (section "MSB/LSB
   Swap") says the 10 output lines can be bit-reversed by a control register
   but does not name it; only 54 of 256 registers are documented. Sweep the
   undocumented registers with the backlight on: set candidate bits, grab one
   frame, restore, and look for a frame whose values become the low byte
   (`v & 0xff`) of the normal frame (bright flat field turning dark and
   wrapping, bit-reversed noise). Writes are volatile; a USB replug resets the
   sensor. Skip 0x12 bit 7 (soft reset) and the documented registers.
8. **True 10-bit frames from pairs.** With the swap register known, capture
   coarse (bits 9..2) and fine (bits 7..0) frames of the same static scene
   and reconstruct `v = c*4 + wrap(f - c*4)`, choosing the wrap closest to
   `c*4` (noise is about 6 LSB at 10 bits, so the 128 margin is safe). Add
   this to `tools/f50raw.py` as the default when the register is known, and
   write 10-bit DNGs (white level 1023 times the averaging factor).
9. **More range than 10 bits.** Exposure bracketing at gain 1x: two or three
   exposures, merged linearly after dark subtraction with per-pixel weights
   that drop clipped samples. Combine with frame averaging. Output a 16-bit
   linear DNG. Measure dynamic range on a real negative (densest and clearest
   areas) and report it in stops.
10. **Backlight.** Check whether the backlight is three LEDs that can be
    switched separately (firmware/opcode or sensor-side GPIO); if they are
    red, green and blue, per-colour exposures remove crosstalk through the
    Bayer filters. Test only if cheap.
11. **Validate on film.** The owner will leave a developed film strip in the
    holder. Capture it with every mode (8-bit, 10-bit pair, bracketed) and
    keep the outputs in `frames/` with a short comparison in `docs/NOTES.md`.
