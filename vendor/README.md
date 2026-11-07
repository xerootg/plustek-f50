# Vendor driver facts

Config files copied verbatim from Plustek's own public download server so
the hardware parameters are recorded next to the notes. The binaries
(DLLs, firmware) are not committed; grab them from the URLs below.

## Packages on Plustek's S3 bucket

All under `https://plustek.s3.amazonaws.com/downloads/english/driver/`
(`downloads.plustek.com` redirects there; the bucket listing is public).

| File | Size | SHA-256 |
|---|---|---|
| `F50V4111WebDriver.rar` (Windows v4.1.1.1, 2009) | 28449295 | `1230ce6f11af714ca4e2842cccfae35a5a0f8bfebf048dadc02aba4bb1c09a7e` |
| `SmartPhotoF50-V5000_12L.zip` (Windows v5.0.0.0) | 31557127 | `ead31fae71509a33bead8fef4f21757beeffd0bef696397a564e7a3841737ee4` |
| `SmartPhoto_F50_CMAXXX.zip` (Windows, WinDriver based) | 30700956 | `7699643c011afb81fffb4a18b58eaec4f217864f078c3ff8027cc7cbd8d64724` |
| `SmartPhoto_F50_V4.1.2.0_webDriver.dmg` (Mac) | 2339318 | `638b823733d8fb776509c03f1f2ada29a9d5702fa3a3236eca926f36c3f7ad14` |
| `SmartPhotoF50-V4200_12L_mac.dmg` (Mac) | 2270490 | `9859d5824941f25be11e78f7458b01ad3e2ff2062dcc3513c3cf26f72f9a2d6f` |
| `../user_guide/user_manual_for_F50.rar` | 8000331 | `185b456b2e1b0704b31c6f615f6316c937dac35d77ab4a1ad1a1d904e5996fbe` |

`F50V4111WebDriver.rar` unpacks (7z) to an InstallShield tree; `unshield x
data1.cab` yields `DRV_U_GT6816/` with:

| File | Size | SHA-256 |
|---|---|---|
| `FWofWin.bin` (GT-6816 8051 firmware, FW version 0.6.1.8) | 7955 | `4364a49af9abb8b6f4ec077efa7e7f8378d77a1f4d1972b22f718de111db63ac` |
| `Scandrv.dll` (scan engine, exports `ScannerCommander`, uses Jungo WDAPI1010) | 114688 | `e5ac923caf39a14e8f0afd8d116a33c051b8c381f4d45c4cc70c3dde2d64ca8e` |
| `Positive.map` (3 ch x 4096 x 16-bit tone map) | 24576 | `89a98477c5410814bd9b84b589af81bf169000783cc7fe1e4354d95561424605` |

## Files here

- `Reg.ini`, `module.ini`: the scan engine's hardware parameters for module
  "A41" (the F50). Note `;Firmware=CISM216.fw`, `DeviceCaps=600,600,1800,1800,150,150`,
  `ExposureTime=600`, `Gain=120`, `RGBGamma=180,120,100`.
- `Usbscan.inf`: the WIA/STI INF; binds `USB\VID_07b3&PID_0B00` to
  Microsoft's `usbscan.sys`, with Plustek's user-mode microdriver on top.
- `PRODUCT.dat`: the installer's product template (comments only describe
  Plustek's older ASIC families; the F50 entry is empty).
