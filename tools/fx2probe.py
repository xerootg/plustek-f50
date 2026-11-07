#!/usr/bin/env python3
"""Read-only probe: does this device answer the Cypress EZ-USB FX2 ROM
'Firmware Load' vendor request (0xA0)? A real FX2 answers it in hardware, so
reading CPUCS and code RAM works whether or not application firmware runs.

Result on the SmartPhoto F50 (2026-11-06): every read returns the same junk
bytes regardless of address, so the F50's controller is NOT a genuine FX2 even
though its firmware uses the FX2 register map. The GT-6816 has its own loader
(the one SANE's gt68xx backend speaks: vendor request 0x01, wValue 0x200b/0x200c).

Usage: fx2probe.py [vid:pid]   (default 07b3:0b00)
Prints CPUCS (0xE600) and the first bytes of code RAM. Nothing is written.
"""
import sys
import usb.core
import usb.util

def main():
    vid, pid = 0x07B3, 0x0B00
    if len(sys.argv) > 1:
        vid, pid = (int(x, 16) for x in sys.argv[1].split(":"))
    dev = usb.core.find(idVendor=vid, idProduct=pid)
    if dev is None:
        sys.exit("device not found")
    rt_in = usb.util.build_request_type(usb.util.CTRL_IN, usb.util.CTRL_TYPE_VENDOR, usb.util.CTRL_RECIPIENT_DEVICE)
    def rd(addr, n):
        return bytes(dev.ctrl_transfer(rt_in, 0xA0, addr, 0, n, timeout=1000))
    try:
        cpucs = rd(0xE600, 1)
        print(f"CPUCS (0xE600) = {cpucs.hex()}  ({'8051 running' if cpucs[0] & 1 == 0 else '8051 held in reset'})")
        print(f"IFCONFIG (0xE601) = {rd(0xE601, 1).hex()}")
        print(f"REVID (0xE60A) = {rd(0xE60A, 1).hex()}")
        code = rd(0x0000, 64)
        print("code RAM 0x0000:", code.hex())
        print("code RAM 0x1f00:", rd(0x1F00, 64).hex())
    except usb.core.USBError as e:
        print("control transfer failed:", e)
        sys.exit(1)

if __name__ == "__main__":
    main()
