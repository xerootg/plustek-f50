<!-- Produced by the RE workbench (Kimi K3) from the Windows driver binaries and the
device firmware, 2026-11-06/07. Labels: proven / inferred / guess as written by the
workbench. Read 06-corrections-and-gaps.md for the review pass; where this file and
firmware.md disagree, firmware.md wins, and where firmware.md and 06 disagree, 06 wins. -->

# Plustek SmartPhoto F50 (USB 07b3:0b00) — protocol and register findings

Goal: reproduce the Windows driver (F50 v4.1.1.1, `F50V4111WebDriver.rar`)
command sequences with libusb so a Linux driver can be written.

Sources analyzed:
- `Scandrv_x86.dll` (scan engine, export `ScannerCommander`; imagebase 0x10000000;
  function names below are Ghidra labels, e.g. `FUN_100088f0` = code at 0x100088f0)
- `WDUScan_x86.exe` (COM out-of-process server doing the raw USB via Jungo WinDriver;
  imagebase 0x400000; functions referenced as `fcn.0040xxxx` from radare2)
- `FWofWin.bin` / `firmware_flat_8051.bin` (on-device 8051 firmware; handlers
  referenced as `cmd_0xxx` code addresses)

Every statement is tagged **[proven]** (read directly from code/disassembly),
**[inferred]** (deduced from context) or **[guess]**.

## Architecture overview [proven]

```
TWAIN app -> Scandrv.dll (ScannerCommander)
    64-byte command blocks  -> WDUScan.exe COM server (IWDUFilmScanner)
        -> Jungo WinDriver -> USB control transfers
    image frames            -> DoCaptureImage COM call (frame grab)
                            -> WDU_StreamRead on bulk EP 0x82 (chunked path)
```

The device has **no motor**: it is a fixed OmniVision **OV5620** 5 MP Bayer CMOS
camera (2592x1944) behind the film gate, with an LED backlight. The 8051
firmware (a Cypress EZ-USB FX2/ CY7C68013-compatible controller with a Grandtech
scanner front-end — **not** a GT-6816; see `firmware.md` §0 — XDATA mailbox
window at 0x2800) forwards commands to the sensor over SCCB/I2C (7-bit device
address 0x30) and streams raw frames on bulk IN EP 0x82. Lower scan
resolutions are produced by **software decimation in the DLL**, not by the
sensor [proven: only one fixed window/exposure programming exists in the DLL;
the dpi snapping logic at `FUN_10009fc0` picks a supported dpi and rescales
host-side, `_DAT_10017fc8 = supported/requested`].

**`firmware.md` is the authoritative reference for the on-device 8051 firmware**
(boot, register map, every mailbox opcode, the EP2 data path, GPIO, EP 0x81,
the 16-bit FIFO patch recipe, hang analysis). Where it disagrees with doc 01
(reply echo, opcode 9, unknown-opcode behavior, several opcode names),
firmware.md is right — doc 01 was written from a partially desynced Ghidra
decompile; see firmware.md §9 for the correction list.
