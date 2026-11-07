# Notes

## 2026-11-06

- Identified the device: Plustek SmartPhoto F50, USB `07b3:0b00`.
- Vendor-specific interface, bulk IN + interrupt IN, no bulk OUT. Commands must
  be control transfers.
- No SANE backend, no kernel driver.
- Windows driver package is `F50V4111WebDriver.rar` (v4.1.1.1, 2009-11-05,
  28.4 MB). Plustek's download page is JS only; DriverGuide mirrors it behind a
  checkbox gate.

### Driver teardown

- `F50V4111WebDriver.rar` -> InstallShield -> `unshield x data1.cab` -> `DRV_U_GT6816/`:
  `FWofWin.bin` (7955 B), `Scandrv.dll` (scan engine, uses Jungo WinDriver
  `WDAPI1010.dll`), `Reg.ini`, `module.ini`, `Positive.map` (3 x 4096 x 16-bit
  tone LUT, so the AFE path is 12-bit).
- `Reg.ini`: `ID_0000=VID_07B3&PID_0B00`, `;Firmware=CISM216.fw`,
  `DeviceCaps=600,600,1800,1800,150,150`, `SupportDpiX/Y=1,1800`,
  `ExposureTime=600`, `Gain=120`, `RGBGamma=180,120,100`, `FWVersion=0.6.1.8`.
  "CISM" says the sensor is a CIS line sensor (a CMOS linear array with its own
  LED light), which is what the manual's "Image Capture CMOS" means. There is
  no Bayer mosaic to undo: R, G and B are separate exposures of the same line.
- The manual says the film holder is pushed through by hand and indexes per
  frame with notches, so the only motor inside moves the sensor across one frame.
- The GT-6816 is Grandtech's 8051-based scanner controller. SANE's `gt68xx`
  backend drives it for many Mustek/Plustek/Genius flatbeds, with 8/12/16-bit
  linear output, software gamma that can be set to 1.0, and calibration that
  can be switched off. That is the most direct route to "raw sensor data".
- `tools/fwinfo.py`: `FWofWin.bin` is a Cypress-style C2 image: 8-byte header
  (VID/PID/DID/config) then (length, address, data) records; the 8051 code
  covers 0x0000-0x1f38 and the final record writes 0x00 to 0xE600 (CPUCS,
  "run"). The firmware uses the EZ-USB FX2 register map (CPUCS, IFCONFIG,
  EP2CFG, SETUPDAT at 0xE6B8, EP0BUF at 0xE740) and embeds the USB descriptors
  we see from `lsusb` (strings "Film Scanner").
- `tools/fx2probe.py`: the device does NOT answer the FX2 ROM loader request
  0xA0 (every address returns the same junk), so this is not a stock Cypress
  part; the GT-6816 has an FX2-compatible core with its own boot protocol.
- SANE's `gt6816_download_firmware` sends the file raw, 64 bytes at a time, to
  address 0 via vendor request 0x01 / wValue 0x200b, then "boot" (0x69 0x01).
  Whether it needs a flat image or accepts the C2 wrapper is still open; the
  device already runs firmware from its own EEPROM (strings are present
  before any driver touches it), so the backend may not need to load anything.

### Next

1. Decode the firmware's EP0 vendor-request dispatcher (`r2 -a 8051`) and
   `Scandrv.dll`'s WinDriver control transfers (Ghidra) to confirm the request
   numbers match gt68xx's `mustek_gt6816_command_set`.
2. If they match: add a `plustek_smartphoto_f50_model` to `gt68xx_devices.c`
   (1800 dpi CIS, small scan area), build, test with the firmware already
   resident, then tune geometry, AFE gain/offset and exposure.
3. Raw output = 16-bit mode, `--gamma-value 1.0`, calibration off.

### USB protocol (from the firmware's EP0 handler, `FWofWin.bin` 0x0df0)

The firmware's standard-request switch handles GET_STATUS, CLEAR/SET_FEATURE,
GET_DESCRIPTOR, GET/SET_CONFIGURATION, GET/SET_INTERFACE. Its vendor handler
accepts exactly one bRequest, 0x04, in two forms; anything else stalls:

| Direction | bmRequestType | bRequest | wValue | wIndex | wLength | Meaning |
|---|---|---|---|---|---|---|
| OUT | 0x40 | 0x04 | 0x0011 | 0x0001 | 64 | command block, copied to XDATA 0x2800 |
| IN | 0xC0 | 0x04 | 0x0010 | 0x0000 | 64 | runs the command interpreter, returns the 64-byte reply from XDATA 0x2800 |

Image data arrives on bulk EP2 IN (512-byte packets). EP1 IN is a 1-byte
interrupt endpoint (likely the scan button).

The command interpreter (code 0x0166) switches on the first two bytes of the
block as a 16-bit little-endian opcode. Opcodes 0x00 to 0x19 exist (0x18 is
missing, 0x10 to 0x12 share one handler); handler addresses in the flat image:

```
00:0202 01:023e 02:026d 03:0272 04:0277 05:027c 06:02b3 07:02b8 08:0300 09:0305
0a:06c1 0b:0474 0c:0505 0d:0288 0e:0413 0f:0429 10/11/12:07b7 13:059a 14:05fc
15:065e 16:06f6 17:0758 19:0781 default:07b5
```

So this is Plustek's own mailbox protocol, not the gt68xx command set, even
though the chip family is the same. SANE's gt68xx backend cannot be reused
as is; its memory-write/boot requests would just stall. The device already
runs this firmware from its own EEPROM, so no download step is needed.

Host side: `Scandrv.dll` talks to the device through Microsoft's `usbscan.sys`
(`CreateFileA` + `DeviceIoControl`, interface enumerated with SetupDi) and
uses Jungo WinDriver only for streaming bulk reads (`WDU_StreamRead`).

### Host software architecture (Windows)

- `WDUScan.exe` is an out-of-process COM server ("WDUFilmScanner Class",
  CLSID `{81C5FF24-3C8B-494D-A29C-181923112BCB}`, events
  `_IWDUFilmScannerEvents`) built on Jungo WinDriver (licence string
  `WD_Ver1010_Plustek`). It exposes `DoUsbControlIn`, `DoUsbControlOut`,
  `GetEventValue` and raises the interrupt-endpoint event.
- `Scandrv.dll` (export `ScannerCommander`) is the engine: it builds the
  64-byte command blocks, sends them through the COM server, and pulls image
  data itself with `WDU_StreamRead` on pipe 0x82. Its `\\.\HCD%d` and
  SetupDi code is only the "is this a USB 2.0 port" check.
- `TWAIN_32.ds` and the WIA microdriver (`MidrvA41.dll`, `WIAA4103.dll`) sit
  on top of `ScannerCommander`.

So every register write the scanner ever sees originates in `Scandrv.dll`.
Recovering the init, lamp, calibration and scan-setup sequences from it is
the remaining reverse-engineering job (delegated to the RE workbench).

### Live results (2026-11-06, `tools/f50.py`)

The mailbox protocol works from Linux with pyusb. Replies carry `0x1234` at
bytes 2..3 on success (`0x5678` on error, from the firmware's interpreter).

| Opcode | Reply | Meaning |
|---|---|---|
| 0x00 | `00 06 01 08` | firmware 0.6.1.8 (matches `Reg.ini` FWVersion) |
| 0x01 | `01 02 04` | protocol 1.2.4 |
| 0x0f | `02 02 82 03 81` | bulk EP 0x82, interrupt EP 0x81 |
| 0x19 get | 3 | mode variable (firmware compares it with 0x0b elsewhere) |
| 0x0a get | 0 | second mode variable |

### What the register opcodes really are

The firmware's register helpers write XDATA 0xE678/0xE679, which in the FX2
register map are I2CS/I2DAT: the "registers" are an I2C slave at 7-bit address
0x30. Opcode 0x0b = I2C write of (reg, value) pairs; 0x0c = write reg, read one
byte. Opcodes 0x13/0x14 are generic I2C reads/writes to any address, and
0x15/0x16 read/write 32-byte pages with a 16-bit address: the boot EEPROM.
**Never send 0x16** (or 0x14 at the EEPROM address); `tools/f50.py` blocks
them. At boot the firmware sets IFCONFIG=0xC3 (internal 48 MHz clock, slave
FIFO mode), so the pixel stream is pushed into EP2's FIFO by the engine
hardware; the host only configures the engine over I2C and reads bulk 0x82.

### The engine is an image sensor: OmniVision OV5620

Reading the I2C 0x30 device's registers (`docs/engine-regs-poweron.txt`):
`0x0a/0x0b = 0x56 0x21` (PIDH/PIDL) and `0x1c/0x1d = 0x7f 0xa2` (MIDH/MIDL,
OmniVision's manufacturer ID). That is the OV5620, a 1/2.5" 5-megapixel
(2592 x 1944) Bayer CMOS sensor, SCCB write address 0x60 = 7-bit 0x30.
Everything fits: the manual's "Image Capture CMOS", the "5 MP" marketing,
1800 dpi (2592 px across a 36.6 mm frame), no motor, 100 mA bus power, the
hand-indexed holder, and `Reg.ini`'s ExposureTime/Gain/RGBGamma, which are
sensor exposure, sensor gain and per-channel white balance.

So the F50 is a fixed camera with a backlight. The GT-6816 only pipes the
sensor's parallel pixel bus into the USB FIFO (slave FIFO mode) and relays
I2C. "Raw sensor data" therefore means: configure the OV5620 for its raw
Bayer output, manual exposure, gain 1x, no AWB/gamma, grab full frames over
bulk 0x82, demosaic on the PC. The Windows `Positive.map`/`Negative.map`
12-bit LUTs are what the DLL applied afterwards and can be skipped.

Registers 0x04 to 0x09 change between reads (live AEC/AGC state).

## 2026-11-06 (later): Windows capture sequence, first frames on Linux

Labels: **proven** = observed on the device or read directly in code;
**inferred** = deduced, not yet tested.

### Who programs the sensor (proven, from the decompiles)

`Scandrv.dll` only builds mailbox commands for the firmware's own opcodes (1, 2, 5, 7,
8, 9, 0x0a) and calls the COM server. The sensor setup and the bulk read both live in
`WDUScan.exe` (decompiled with Ghidra headless into the private RE folder). COM vtable
of the `WDUFilmScanner` object (offsets after IDispatch):

| vtable | WDUScan function | Role |
|---|---|---|
| 0x2c | FUN_004032c0 -> FUN_00402930 | sensor setup (exposure, gain, WB, mode) |
| 0x30 | FUN_004043b0 -> FUN_00404150 | capture N bytes from bulk 0x82 |
| 0x34 | FUN_00403360 | interrupt EP 0x81 event value |
| 0x38/0x3c | | raw control OUT/IN (Scandrv FUN_1000bb60/bc30) |
| 0x28 | FUN_00403150 | sends opcode 0x16 (EEPROM page write). Never used by us. |

Scandrv's scan path (FUN_100053b0) calls 0x2c with `(1, ExposureTime=600,
Gain*0.01=1.20, 1, RGBGamma*0.01 = 1.80, 1.20, 1.00)`, then 0x30 with 0x4ce300 bytes,
then demosaics the buffer as **0xa20 x 0x798 = 2592 x 1944** (FUN_10004780 for grey,
FUN_10004330 into a 2592x1944x3 buffer for colour). The extra 1024 bytes of the
0x4ce300 buffer are never used.

### Sensor setup, WDUScan FUN_00402930 scan branch (proven)

Read the 54 registers listed in `tools/f50.py` `SENSOR_REGS` (opcode 0x0c), edit,
write all back (opcode 0x0b, 29 pairs per block):
window HSTART=0x90 HSTOP=0x5a0 VSTART=8 VSTOP=0x560 (VSTOP's 9th bit is truncated by
the 8-bit write; harmless because COM3 keeps full-size output); REG04 &= 0x3f (no
mirror/flip); COM8 &= ~0x87 (AEC, AGC, AWB, fast AEC off); GAIN = f(1.20);
RED/GREEN/BLUE = 1.80/1.20/1.00 x 64; exposure 600 lines; COM7 &= 0x87 (full res);
CLKRC = 0x40 (divider on, /2); REG32[7:6] = 0; COM9 |= 0x06 (drop out-of-tolerance
frames); AEW/AEB = 0x58/0x38; R/G/B gain limits = 0xe2; then write (0x80,0x09) and
(0x81,0x07) (CIP on, black/white pixel cancelling on). Then an auto-gain loop: gain
from 1.0 in 0.1 steps (clamped 0.1..3.9, max 21 steps, 300 ms apart) until YAVG (0x2f)
is within 0x38..0x58. Finally exposure = 600>>3 = 75 lines and CLKRC = 0x43
(PLL/8), sleep 3 s.

Exposure bits per the driver: 0x04[2:0], 0x10 = e>>3, 0x45[5:0] = e>>10. The datasheet
says 0x45 holds AEC[16:11], so the driver's e>>10 is a bug for e >= 1024 (it never
uses such values). Gain conversion (FUN_00402220) is reg = trunc((g - hi)*16 + 0.5) + hi
with hi = (trunc(g) & 0xfe)*8; for g < 2 that is just g*16, i.e. the driver's "1.0"
is register 0x10 = 2x analog gain.

### Capture, WDUScan FUN_00404150 and the firmware (proven)

Host: opcode 7, opcode 5, reset pipe 0x82, read N bytes (Jungo stream, 3 s timeout,
retries every 50 ms with pipe reset), opcode 7, drain, reset pipe; up to 6 attempts.

Firmware: opcode 5 sets flag bit 0x20.4. The INT0 ISR (0x13b8, sensor VSYNC) checks
it: when the mode variable 0x6c (opcode 0x19) is not 0x0b, it resets the EP2 FIFO and
sets EP2CFG.VALID, then clears the flag. At boot (0x16ba) the firmware writes
EP2CFG = 0xE8 and immediately clears VALID, and opcode 7 clears VALID again. So **EP2
only exists between the first VSYNC after opcode 5 and the next opcode 7**, and the
stream starts at the top of a frame. In mode 0x0b (not used by Windows) the ISR also
does a FIFO reset at every VSYNC and opcode 7 leaves EP2 valid. The main loop sets flag
bit 3 when EP2CS shows the FIFO full (overflow), which opcode 7 returns in reply[4].
IFCONFIG = 0x43 after init: slave FIFO clocked by the sensor's PCLK. EP2 is
quad-buffered 1024-byte buffers with AUTOIN length 0x200.

Opcode 9 sub-command 4 with argument 1 (sent by Scandrv at open) sets port D bits 0..2
(OED/IOD); inferred to be the backlight LEDs. Opcode 2 only acknowledges.

### Linux results (proven)

- Before EP2 is valid, xHCI reports the missing handshake as `-EPROTO` (libusb
  LIBUSB_ERROR_IO), and EP 0x81 NAKs normally. After opcode 5 the first data arrives
  0.1..0.8 s later (the next VSYNC).
- Separate synchronous reads lose bytes at every gap between them (Bayer phase flips
  at the 256 KiB read boundaries). One large read (libusb queues all URBs at once),
  retried on -EPROTO until EP2 goes valid, gives a clean stream. Reads must be whole
  512-byte packets (else `EOVERFLOW`) and under usbfs_memory_mb (16 MB by default).
- The stream is frames back to back: rows of exactly 2592 bytes (autocorrelation),
  and the vertical profile repeats every **1944** rows (mean |m[i]-m[i+L]| is 0.079 at
  L=1944 against 0.15..0.44 for L = 1936..1968). The first rows of the stream match
  rows 1944.. of the next frame, so the read starts on the first line of a frame.
  **One frame = 2592 x 1944 x 1 byte = 5,038,848 bytes**; the Windows 0x4ce300 read is
  that plus 1024 bytes of the next frame.
- Throughput with CLKRC = 0x43: 7.1..7.2 MB/s, about 0.7 s per frame; CLKRC = 0x47
  halves it (3.9 MB/s), so the rate is set by the sensor clock, not by USB.
- Windows sequence replayed exactly (power-on registers restored first, then op 9(4,1),
  op 2(1), FUN_00402930, op 7/5 + read): `captures/win_settings_frame0.pgm`, mean 254.6,
  93 % of pixels at 255. With the bare backlight (no film) its auto-gain loop cannot
  reach YAVG 0x38..0x58 and ends at the minimum; the result is overexposed, as the
  Windows driver would be without film.
- Raw-ish settings (COM8 manual, COM9 no frame drop, DSPEN 0x02 raw before CIP, DSP01
  0, DGCTRL 1x, DSP09 AWB gains off, DSP0B gamma off, BOTLMT/TOPLMT 0/255, COM34 0,
  gain 0, exposure 6 lines, CLKRC 0x43): `captures/raw8_e6_clk3_frame0.pgm`. A flat
  backlight field with dust and fibres in focus, no seam. Channel means
  (0,0)=240.0 (0,1)=202.2 (1,0)=201.6 (1,1)=138.6: the matching diagonal is green, so
  the CFA is **BGGR** as the datasheet says (proven for the green diagonal; B vs R
  inferred from the datasheet and the LED's blue-heavy spectrum).
- Captures live in `captures/` (gitignored).
