#!/usr/bin/env python3
"""Parse a GT-68xx / EZ-USB style "C2" firmware image and print its load map.

Format (same as Cypress EZ-USB EEPROM boot images):
  byte 0      0xC2 magic
  bytes 1-2   USB vendor id (little endian)
  bytes 3-4   USB product id (little endian)
  bytes 5-6   device release (little endian)
  byte 7      config byte
  then records: length (2 bytes, big endian), address (2 bytes, big endian),
  data[length]. The top bit of the length marks the final record.

Usage: fwinfo.py FWofWin.bin [--dump out.bin]
  --dump writes a flat 64 KiB image with every record applied, for Ghidra.
"""
import sys

def main():
    args = sys.argv[1:]
    dump = None
    if "--dump" in args:
        i = args.index("--dump")
        dump = args[i + 1]
        del args[i:i + 2]
    data = open(args[0], "rb").read()
    if data[0] != 0xC2:
        sys.exit(f"not a C2 image (first byte {data[0]:#x})")
    vid = int.from_bytes(data[1:3], "little")
    pid = int.from_bytes(data[3:5], "little")
    did = int.from_bytes(data[5:7], "little")
    print(f"file size {len(data)}  vid {vid:04x} pid {pid:04x} did {did:04x} config {data[7]:#04x}")
    image = bytearray(0x10000)
    off = 8
    total = 0
    while off + 4 <= len(data):
        ln = int.from_bytes(data[off:off + 2], "big")
        addr = int.from_bytes(data[off + 2:off + 4], "big")
        last = bool(ln & 0x8000)
        ln &= 0x7FFF
        payload = data[off + 4:off + 4 + ln]
        print(f"  record @file {off:#06x}: {ln:5d} bytes -> code {addr:#06x}..{addr + ln - 1:#06x}{'  (final)' if last else ''}")
        image[addr:addr + ln] = payload
        total += ln
        off += 4 + ln
        if last:
            break
    print(f"records cover {total} bytes; trailing bytes in file: {len(data) - off}")
    if dump:
        open(dump, "wb").write(image)
        print(f"wrote flat image to {dump}")

if __name__ == "__main__":
    main()
