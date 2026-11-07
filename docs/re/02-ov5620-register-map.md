<!-- Produced by the RE workbench (Kimi K3) from the Windows driver binaries and the
device firmware, 2026-11-06/07. Labels: proven / inferred / guess as written by the
workbench. Read 06-corrections-and-gaps.md for the review pass; where this file and
firmware.md disagree, firmware.md wins, and where firmware.md and 06 disagree, 06 wins. -->

# 2. Sensor register map (OV5620 via SCCB, 7-bit address 0x30)

All register access goes through firmware opcodes 0x0b (write list) / 0x0c
(read list); the firmware forwards them to the OV5620 image sensor over SCCB
at 7-bit I2C address 0x30 (user-verified: PID regs 0x0a/0x0b = 0x56/0x21,
mfr regs 0x1c/0x1d = 0x7f/0xa2). [proven]

## 2.1 The 54-entry working set

Both Scandrv.dll (`FUN_100088f0`, template at `DAT_10016340`) and
WDUScan.exe's DoSensorAdjust (`fcn.00402930`, template at 0x413030) carry the
**identical** 54-entry template and the identical modifier code. The init is
a read-modify-write: copy template -> opcode 0x0c reads the live values back
-> modifiers patch entries -> opcode 0x0b writes the list (in chunks of max
29 entries) -> two extra single writes (0x80, 0x09), (0x81, 0x07).
[proven: `FUN_100088f0` asm; EXE `fcn.00402930` asm]

Template (addr,value) with the final value produced by the init modifiers
(`final`), using Reg.ini defaults (ExposureTime=600, Gain=120,
RGBGamma=180,120,100):

| # | addr | template | final (init) | OV5620 meaning |
|---|------|----------|--------------|----------------|
| 00 | 0x00 | 0x00 | f(Gain)=0x13 | GAIN — analog gain, see formula below [proven formula; OV5620 semantics inferred] |
| 01 | 0x01 | 0x80 | round(gammaR*64)=0x73 | per-channel gain (R) [inferred: OV-style x/64 gain; 0x40=1.0x] |
| 02 | 0x02 | 0x80 | round(gammaG*64)=0x4d | per-channel gain (G) [inferred] |
| 03 | 0x03 | 0x4a | (dev & 0xf0) | COM1 — window LSBs: bits[1:0]=VSTART&3=0, bits[3:2]=VSTOP&3=0 [proven layout; name per OV5620] |
| 04 | 0x04 | 0x00 | bits[2:0]=V&7=0, bit6/7=0 | AEC low bits [proven split; name inferred] |
| 05 | 0x09 | 0x01 | 0x01 | COM2 [name per OV5620; the intended `|0x02` (FUN_10008020) is dead code — bug in DLL: it checks entry 0 (addr 0x00) instead of the 0x09 entry, so bit 1 never gets set] [proven] |
| 06 | 0x0c | 0x08 | (dev & 0xce) \| 0x40 = 0x48 | COM3? [name inferred] |
| 07 | 0x0d | 0x06 | from device | COM4? |
| 08 | 0x0e | 0x01 | from device | COM5? |
| 09 | 0x0f | 0x43 | from device | COM6? |
| 10 | 0x10 | 0x63 | (V>>3)&0xff = 0x4b | AEC mid (exposure [10:3]) [proven split] |
| 11 | 0x11 | 0x00 | (dev & 0x3f) \| 0x40 = 0x40 | CLKRC — bit6 set (clock prescale/doubler select) [name per OV5620] |
| 12 | 0x12 | 0x00 | 0x00 | COM7 — output format = RAW Bayer, no reset [proven by demosaic in DLL + PSoC comment convention; name per OV5620] |
| 13 | 0x13 | 0xc7 | 0x40 | COM8 — all auto functions off (AGC/AWB/AEC bits cleared: &0x7f &0xfb &0xfd &0xfe), bit6 kept [proven bit ops; name per OV5620] |
| 14 | 0x14 | 0x40 | (dev & 0xf9) \| 0x06 = 0x46 | COM9 — bits 1,2 set [name inferred] |
| 15 | 0x15 | 0x00 | from device | COM10 (PCLK/HREF polarity) |
| 16 | 0x16 | 0x80 | round(gammaB*64)=0x40 | per-channel gain (B) [inferred] |
| 17 | 0x17 | 0x12 | HSTART>>3 = 0x12 | HSTART high [proven split; name per OV5620] |
| 18 | 0x18 | 0xb4 | HSTOP>>3 = 0xb4 | HSTOP high |
| 19 | 0x19 | 0x01 | VSTART>>2 = 2 | VSTART high |
| 20 | 0x1a | 0xf4 | VSTOP>>2 = 0x58 | VSTOP high |
| 21 | 0x1b | 0x00 | from device | PSHFT (pixel shift) |
| 22 | 0x24 | 0x78 | 0x58 | AEW — AEC high target (88) [name per OV5620 convention] |
| 23 | 0x25 | 0x68 | 0x38 | AEB — AEC low target (56) |
| 24 | 0x26 | 0xd4 | from device | VPT — fast AEC threshold |
| 25 | 0x2a | 0x00 | from device | EXHCH? |
| 26 | 0x2b | 0x00 | from device | HREF/dummy adjust |
| 27 | 0x2d | 0x00 | from device | ? |
| 28 | 0x2e | 0x00 | from device | ? |
| 29 | 0x2f | 0x00 | from device | ? |
| 30 | 0x30 | 0x08 | from device | ? |
| 31 | 0x31 | 0x30 | from device | ? |
| 32 | 0x32 | 0x00 | bits[2:0]=HSTART&7=0, bits[5:3]=HSTOP&7=0 | window LSBs + bits[7:6]=0 (FUN_10008570) [proven layout] |
| 33 | 0x45 | 0x00 | bits[5:0]=(V>>10)&0x3f=0 | AEC high (exposure [16:11]) [proven split; name inferred] |
| 34 | 0x46 | 0x00 | from device | ? |
| 35 | 0x47 | 0x00 | from device | ? |
| 36 | 0x48 | 0x00 | from device | ? |
| 37 | 0x49 | 0x00 | from device | ? |
| 38 | 0x5f | 0x00 | from device | ? |
| 39 | 0x60 | 0x00 | from device | ? |
| 40 | 0x61 | 0x00 | from device | ? |
| 41 | 0x63 | 0x00 | from device | ? |
| 42 | 0x80 | 0x01 | 0x09 (2nd write) | ? written twice: template value 0x01, final explicit write 0x09 [proven] |
| 43 | 0x81 | 0x00 | 0x07 (2nd write) | ? written twice: 0x00 then 0x07 — likely stream-enable/commit pair [guess] |
| 44 | 0x83 | 0x80 | from device | ? |
| 45 | 0x84 | 0x00 | from device | ? |
| 46 | 0x85 | 0x00 | from device | ? |
| 47 | 0x89 | 0x29 | from device | ? |
| 48 | 0x8b | 0x1f | from device | ? |
| 49 | 0xa8 | 0x10 | from device | ? |
| 50 | 0xa9 | 0xf0 | from device | ? |
| 51 | 0xb8 | 0xf0 | 0xe2 | per-channel nibble pair (2,0xe) — R [proven write; semantics guess: BLC/black-level or LED/gain trim] |
| 52 | 0xb9 | 0xf0 | 0xe2 | same — G |
| 53 | 0xba | 0xf0 | 0xe2 | same — B |

"from device" = value read back with opcode 0x0c and written unchanged
(template byte is only a placeholder in those slots). [proven mechanism]

## 2.2 Formulas [all proven from DLL asm]

Exposure (17-bit value V, Reg.ini `ExposureTime`, default 300, clamp 10..10000,
shipped value 600) — `FUN_10008060` / EXE equivalent:
```
reg 0x04 = (reg04 & 0xf8) | (V & 7)
reg 0x10 = (V >> 3) & 0xff
reg 0x45 = (reg45 & 0xc0) | ((V >> 10) & 0x3f)
```
With V=600 (0x258): 0x04[2:0]=0, 0x10=0x4b, 0x45[5:0]=0.
Runtime change helper: `FUN_10008aa0(V)` (called with V=10 and V=512 in
calibration, and with `DAT_10017dd4` at init). Semantics: exposure time in
sensor lines [inferred]; exact timebase = f(CLKRC/XCLK) — the GT-6816 feeds
the sensor clock; not derived here.

Analog gain (Reg.ini `Gain`, default 100, clamp 10..4000, shipped 120)
— `FUN_10007e20`, input g = Gain/100.0 (1.2):
```
e = min(floor(g), 16) & 0xfe            # even part
reg 0x00 = (e << 3) | round((g - (e<<3)) * 16)   # via fsubr; +0.5 rounding
```
With g=1.2: e=0 -> reg 0x00 = round(1.2*16 + 0.5) = 19 = 0x13.
(If the e<<3 exceeds 8 bits the decompile shows it as a byte store; sanity:
g>=16 -> clamp.) The result is written as the single byte of reg 0x00.

Per-channel gains (Reg.ini `RGBGamma` = 180,120,100 -> floats 1.8/1.2/1.0)
— `FUN_10007ec0`:
```
reg 0x02 = round(gammaR * 64)   # 0x73
reg 0x16 = round(gammaG * 64)   # 0x4d
reg 0x01 = round(gammaB * 64)   # 0x40
```
[channel->register order from the asm load order (st0=R -> reg 0x02,
st1=G -> reg 0x16, st2=B -> reg 0x01); inferred]

Window — `FUN_10008300(buf, 54, 0x90, 0x5a0, 8, 0x560)` (constants are
identical in the EXE copy): HSTART=0x90, HSTOP=0x5a0, VSTART=8, VSTOP=0x560.
```
reg 0x17 = HSTART >> 3            reg 0x32[2:0] = HSTART & 7
reg 0x18 = HSTOP  >> 3            reg 0x32[5:3] = HSTOP  & 7
reg 0x19 = VSTART >> 2            reg 0x03[1:0] = VSTART & 3
reg 0x1a = VSTOP  >> 2            reg 0x03[3:2] = VSTOP  & 3
```
These values are the SAME for every scan and every resolution [proven: only
one call site]. The bulk frame is always the full 2592x1944 (see 04), so
either the window registers gate only the AEC/AWB metering area, or the
OV5620 DSP rescales; the stream size does not depend on them. [inferred]

COM8/auto functions — init clears four bits of reg 0x13 in sequence
(`FUN_10007d30` &0x7f, `FUN_10007c50` &0xfb, `FUN_10007cc0` &0xfd,
`FUN_10007da0` &0xfe): 0xc7 -> 0x47 -> 0x43 -> 0x41 -> 0x40.
bit7=AGC?, bit2=?, bit1=AWB?, bit0=AEC? — all off -> fully manual
exposure/gain/white-balance. [bit ops proven; bit names per OV5620
convention = inferred]

Other forced bits:
```
reg 0x11 = (dev & 0x3f) | 0x40        # CLKRC bit6 (FUN_10008420)
reg 0x14 = (dev & 0xf9) | 0x06        # COM9 bits 1,2 (FUN_10008600)
reg 0x0c = (dev & 0xce) | 0x40        # (FUN_10008520), reg 0x12 forced 0 twice
reg 0x04 bits 6,7 = 0                 # (FUN_10007f90)
regs 0xb8/0xb9/0xba = 0xe2            # (FUN_100081b0/8220/8290: (2<<4)|0xe? stored as (0xe<<4)|2)
regs 0x24, 0x25 = 0x58, 0x38          # (FUN_100080f0/8150)
```
[all proven]

## 2.3 Concrete init bytes (Reg.ini defaults, cold-boot approximation)

Read blocks the DLL sends (entries = the 54 addresses, zero values):

```
# opcode 0x0c read, chunk 1 (29 regs) — 64 bytes exactly
0c 00 00 00 1d 00
00 00 01 00 02 00 03 00 04 00 09 00 0c 00 0d 00 0e 00 0f 00
10 00 11 00 12 00 13 00 14 00 15 00 16 00 17 00 18 00 19 00
1a 00 1b 00 24 00 25 00 26 00 2a 00 2b 00 2d 00 2e 00

# opcode 0x0c read, chunk 2 (25 regs) — 56 bytes + 8 zero pad
0c 00 00 00 19 00
2f 00 30 00 31 00 32 00 45 00 46 00 47 00 48 00 49 00 5f 00
60 00 61 00 63 00 80 00 81 00 83 00 84 00 85 00 89 00 8b 00
a8 00 a9 00 b8 00 b9 00 ba 00
```

Write blocks (values computed with ExposureTime=600, Gain=120,
RGBGamma=180,120,100; "from device" entries shown with template
placeholders — a real driver should use the values read above):

```
# opcode 0x0b write, chunk 1 (29 regs)
0b 00 00 00 1d 00
00 13 01 73 02 4d 03 40 04 00 09 01 0c 48 0d 06 0e 01 0f 43
10 4b 11 40 12 00 13 40 14 46 15 00 16 40 17 12 18 b4 19 02
1a 58 1b 00 24 58 25 38 26 d4 2a 00 2b 00 2d 00 2e 00

# opcode 0x0b write, chunk 2 (25 regs)
0b 00 00 00 19 00
2f 00 30 08 31 30 32 00 45 00 46 00 47 00 48 00 49 00 5f 00
60 00 61 00 63 00 80 01 81 00 83 80 84 00 85 00 89 29 8b 1f
a8 10 a9 f0 b8 e2 b9 e2 ba e2

# final commit pair
0b 00 00 00 02 00 80 09 81 07
```
[values proven from the modifier code; "from device" slots are
read-modify-write in the real flow]

## 2.4 Register-access block encoding (repeated from 01 for convenience)

Write: `[0]=0x0b [1]=0 [2]=0 [3]=0 [4..5]=count LE [6+2i]=addr [7+2i]=value`,
count <= 29 per control pair; more entries -> repeat.
Read: `[0]=0x0c ...same... [7+2i]=0`, reply has the read value at [7+2i].
[proven: DLL `FUN_100087d0`/`FUN_10008680` asm + firmware `0x0474`/`0x0505` asm]
