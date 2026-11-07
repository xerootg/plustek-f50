# 06. Review pass: corrections and gaps (2026-11-07)

Review of the workbench findings (00 to 05 and firmware.md) against the FX2
Technical Reference Manual, the CY7C68013 datasheet, the live tests in
`docs/NOTES.md`, and a re-read of the code. Items here override the earlier
files where they conflict.

## Corrections

1. **0xE668 is INTSETUP, not a clone-specific register.** firmware.md §0 and
   §1.3 call the `0xE668 |= 0x09` write "clone engine setup [guess]". The TRM
   register summary puts INTSETUP at 0xE668; bit 3 = AV2EN and bit 0 = AV4EN,
   so 0x09 enables INT2 and INT4 autovectoring. That is the stock Cypress
   framework line and it is exactly what the autovector table at 0x1300
   needs. [proven, TRM Appendix C and §4.3/§4.5]

2. **Why the part is still not a genuine Cypress FX2.** With item 1 gone, the
   only remaining evidence is the loader request: the TRM (Table 2-2 and the
   text after it) says the 0xA0 "Firmware Load" request "is valid at all
   times, so the load feature may be used even after ReNumeration". On the
   F50 every 0xA0 read returned the same bytes regardless of address
   (`tools/fx2probe.py`). A genuine core would have returned RAM. So: an
   FX2-register-compatible core without the Cypress loader. [proven]
   Recovery after a firmware hang is therefore replug only (the loader could
   otherwise have held the CPU in reset).

3. **Opcode 9 is backlight control, not "START_STREAM".** Docs 01 and 03 name
   it START_STREAM with `[4]=4, [5]=1`; firmware.md §4 decodes it as the
   GPIO/backlight dispatcher, and the live test agrees (9 with 4,1 = all three
   channels on; 4,0 = off). Windows sends it once at lamp-on, which is why the
   sequence docs read it as "start". Streaming is armed by opcode 5 and begun
   by the VSYNC interrupt. [proven]

4. **Bayer order is BGGR**, proven on film: the top-left photosite passes the
   least light through the orange mask and the bottom-right the most. Doc 04's
   "RG/GB guess" is wrong. The sensor image is mirrored left-right; the DNG
   writer sets Orientation 2. [proven, NOTES.md]

5. **Unknown opcodes stall; reply bytes 0..1 echo unchanged.** Doc 01's
   "+1/+2 increment" and "unknown opcode returns the version block" came from
   a desynced Ghidra decompile; firmware.md §9 already retracts them and the
   live replies confirm (opcode 0x19 reply starts `19 00 34 12`). [proven]

## Gaps filled

6. **10 bits through the FIFO is impossible on this board as built.** In
   16-bit (WORDWIDE) mode the FX2 puts FD[15:8] on PORTD (TRM §9, Figures
   9-4/9-5), so FD[8], FD[9], FD[10] are the PD0, PD1, PD2 pins. The firmware
   drives the three backlight channels from PD0..PD2 (firmware.md §4,
   live-proven by opcode 9). The sensor's Y1/Y0 therefore cannot be wired to
   the pins that would carry them, and the one-byte patch at file offset
   0x16EE would only replace the backlight with pixel noise. Routes left:
   the OV5620 bus-swap register (software; sweep in progress), or rewiring
   the backlight drive to free PA pins plus a firmware change and two sensor
   traces (hardware, not planned). [proven pin mapping; conclusion inferred]

7. **What limits throughput.** The slave FIFO is clocked by the sensor's
   PCLK (IFCONFIG=0x43), so the byte rate is the sensor's pixel clock, set by
   CLKRC (0x11): 0x43 gives about 7 MB/s and 0x41 about 14 MB/s; at 0x40 the
   host misses bytes. USB high-speed bulk is not the limit; the EP2 quad
   buffer is only 4 KiB (about 0.3 ms at 14 MB/s), so any host read gap longer
   than that loses data silently unless the read is one large transfer.
   The overflow flag (opcode 0x17 / 7, bit 0) only fires while armed.
   [proven mechanism; the exact rate ceiling for a clean stream is still to
   be measured with large single reads]

8. **Windows timing worth copying.** After lamp-on the DLL waits
   WarmUpPeriod x 125 ms = 7.5 s before the first frame (doc 03 §3.2). LED
   output drifts while warming, so long stacks should wait that long after
   switching the backlight on. Calibration in Windows grabs frames at
   exposure 10 and 512 (doc 03 §3.6); the DLL's own exposure-to-register
   split is `0x04[2:0] = V & 7`, `0x10 = V >> 3`, `0x45[5:0] = V >> 10`
   (doc 02 §2.2), which `tools/f50raw.py` must match.

9. **Three backlight channels, roles unknown.** Opcode 9 sub-commands 1, 2
   and 3 switch PD0, PD1, PD2 separately. Whether they are red, green and blue
   LED strings or three white strings is untested (Phase 2 step 10).

10. **EEPROM** (firmware.md §0): 8 KB at I2C 0x51, byte-identical to the
    shipped FWofWin.bin; the C2 format has no checksum. Opcodes 0x14 and 0x16
    can rewrite it and stay blocked in `tools/f50.py`.

## Still open

- The OV5620 "MSB/LSB swap" register (datasheet mentions it, documents only
  54 of 256 registers). Sweep log: `frames/sweep/`.
- Sensor register 0x3e: writing 0x17 to it wedged the SCCB bus and hung the
  firmware's I2C wait loop (no timeout, no watchdog; firmware.md §7). Never
  write 0x3e/0x3f.
- Opcode 0x13 (generic 16-bit-addressed I2C read) could enumerate other I2C
  devices on the bus; not needed so far.
