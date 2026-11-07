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

Needs pyusb and numpy (Pillow for previews); run as root (or add a udev rule for `07b3:0b00`).

```
sudo python3 -I tools/f50.py                       # firmware/protocol versions

# calibration masters, once per exposure setting (holder EMPTY for the flat)
sudo python3 -I tools/f50master.py flat --tenbit --frames 200
sudo python3 -I tools/f50master.py dark --tenbit --exposure 1800 --frames 200

# one calibrated scan: 200-frame true 10-bit stack, dark + flat corrected, 16-bit DNG
sudo python3 -I tools/f50raw.py frames/neg1 --tenbit --frames 200 --exposure 1800 --calib

# exposure bracket merged linearly (each step stacked; AEC:CLKDIV:GAIN:FRAMES, darks per step)
sudo python3 -I tools/f50raw.py frames/neg1_hdr --tenbit --calib --save-steps --orientation 8 \
    --bracket "2400::0:400,10764::0:200,44220::0:100"

# a positive from the linear DNG (needs rawpy and tifffile, e.g. in a venv)
.venv/bin/python tools/f50positive.py frames/neg1_hdr.dng frames/neg1_pos --demosaic dcb
```

Each run of f50raw writes `NAME.dng` (16-bit linear BGGR CFA, TIFF Orientation from
`--orientation`, which depends on how the film sits in the holder), `NAME.pgm` (the same
samples) and `NAME.json` (frames used and rejected, noise versus N, noise floor and dynamic range per channel, timing). Frames are
2592 x 1944; see `docs/NOTES.md`.

## Status

True 10-bit raw Bayer capture (sensor bus-swap register 0x54 bit 2 plus frame pairing),
streaming deep stacks with frame rejection, dark/flat calibration masters, exposure
bracketing with a linear merge, and DNG output work. The USB controller is an
FX2-compatible microcontroller (exact part unknown). See `docs/PLAN.md` for the step list
and `docs/NOTES.md` for evidence and measured numbers.
