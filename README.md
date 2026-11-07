# plustek-f50

Reverse engineering the Plustek SmartPhoto F50 USB film scanner (sold rebadged
by Brookstone and others) to pull raw sensor data out of it on Linux.

## Why

The F50 is a 5 MP CMOS camera pointed at a backlit film holder. The bundled
Windows TWAIN driver does demosaic, white balance, gamma and JPEG/TIFF output
in software, and throws away most of the sensor's usable dynamic range. The
goal here is to talk to the hardware directly and get the Bayer data before
any of that happens, so negatives can be inverted and colour-corrected
properly.

## Hardware facts (from the USB descriptors)

| Field | Value |
|---|---|
| USB ID | `07b3:0b00` (Plustek, Inc. SmartPhoto F50) |
| Strings | Manufacturer "Film Scanner", Product "Film Scanner", no serial |
| Speed | USB 2.0 High Speed |
| Class | Vendor specific (ff/ff/ff), one interface, one configuration |
| Endpoints | EP2 IN bulk, 512 bytes; EP1 IN interrupt, 1 byte, 1 ms |
| Power | Bus powered, 100 mA |

No bulk OUT endpoint, so every command goes through control transfers on EP0.
Image data comes back on the bulk endpoint. No Linux kernel driver and no SANE
backend claims the device.

## Layout

- `docs/` notes, protocol findings
- `captures/` USB captures (usbmon / pcapng) of the Windows driver talking to the scanner
- `tools/` scripts to drive the scanner from Linux
- `frames/` image output (gitignored)

## Usage

Needs pyusb and numpy; run as root (or add a udev rule for `07b3:0b00`).

```
sudo python3 -I tools/f50.py                  # firmware/protocol versions
sudo python3 -I tools/f50raw.py frames/dark --frames 16 --dark
sudo python3 -I tools/f50raw.py frames/flat --frames 16 --black-from frames/dark.pgm
sudo python3 -I tools/f50raw.py frames/neg1 --frames 8 --exposure 300 \
    --black-from frames/dark.pgm --neutral-from frames/flat.pgm
```

Each run writes `NAME.pgm` (16-bit, mean of the frames x 256), `NAME.dng` (BGGR raw)
and `NAME.json`. Frames are 2592 x 1944, 8 bits per sample; see `docs/NOTES.md`.

## Status

Raw 8-bit Bayer capture, exposure/gain/backlight control, frame averaging and DNG
output work. See `docs/PLAN.md` for the step list and `docs/NOTES.md` for evidence.
