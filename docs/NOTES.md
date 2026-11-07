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
