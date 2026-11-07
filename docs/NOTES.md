# Notes

## 2026-11-06

- Identified the device: Plustek SmartPhoto F50, USB `07b3:0b00`.
- Vendor-specific interface, bulk IN + interrupt IN, no bulk OUT. Commands must
  be control transfers.
- No SANE backend, no kernel driver.
- Windows driver package is `F50V4111WebDriver.rar` (v4.1.1.1, 2009-11-05,
  28.4 MB). Plustek's download page is JS only; DriverGuide mirrors it behind a
  checkbox gate.
