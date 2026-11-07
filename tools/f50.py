#!/usr/bin/env python3
"""Minimal userspace driver for the Plustek SmartPhoto F50 (USB 07b3:0b00).

Protocol (decoded from the scanner's own firmware, see docs/NOTES.md):
  vendor request 0x04
  OUT  bmRequestType 0x40, wValue 0x0011, wIndex 0x0001, 64 bytes: command block
  IN   bmRequestType 0xC0, wValue 0x0010, wIndex 0x0000, 64 bytes: reply block
Bytes 0..1 of a block are a little-endian opcode. Image data comes back on
bulk endpoint 0x82.

This module only implements the read-only query opcodes so far. Nothing here
writes scanner registers. Run as a script to print firmware and endpoint info:
  sudo python3 -I tools/f50.py
"""
import struct
import sys

import usb.core
import usb.util

VID, PID = 0x07B3, 0x0B00
REQ = 0x04
RT_OUT = usb.util.build_request_type(usb.util.CTRL_OUT, usb.util.CTRL_TYPE_VENDOR, usb.util.CTRL_RECIPIENT_DEVICE)
RT_IN = usb.util.build_request_type(usb.util.CTRL_IN, usb.util.CTRL_TYPE_VENDOR, usb.util.CTRL_RECIPIENT_DEVICE)
BLOCK = 64

# opcodes recovered from the firmware's command switch
OP_FW_VERSION = 0x00   # reply[4..7] = version bytes (0.6.1.8 on this unit)
OP_PROTO_VERSION = 0x01  # reply[4..6] = 1, 2, 4
OP_SET_FLAG4 = 0x05
OP_READ_CLEAR_FLAG3 = 0x07
OP_START_STREAM = 0x09
OP_VAR_6D = 0x0A       # block[4]=1 set from block[5]; block[4]=2 get into reply[5]
OP_REG_WRITE = 0x0B    # block[4..5]=count, then (addr,value) byte pairs from block[6]
OP_REG_READ = 0x0C     # block[4..5]=count, addr at block[6+2i], value returned at reply[7+2i]
OP_READ_CLEAR_FLAG2 = 0x0D
OP_STATUS_FLAG0 = 0x0E  # reply[4] = flag bit0
OP_ENDPOINTS = 0x0F    # reply[4..8] = 2, 2, 0x82, 3, 0x81
OP_FLAG3 = 0x17        # reply[4] = flag bit3, then clears it
OP_VAR_6C = 0x19       # same get/set shape as 0x0A
OP_I2C_READ = 0x13     # generic I2C read: block[4..5]=count, [6]=device address, data from reply[8]
OP_I2C_WRITE = 0x14    # generic I2C write; NOT USED HERE, could hit the EEPROM
OP_EEPROM_READ = 0x15  # 32-byte page read, 16-bit page address
OP_EEPROM_WRITE = 0x16 # 32-byte page write. NEVER SEND: it overwrites the boot firmware.
FORBIDDEN = {OP_I2C_WRITE, OP_EEPROM_WRITE}

# The register opcodes are I2C transactions to the scanner engine at 7-bit
# address 0x30: 0x0b writes (reg, value) pairs, 0x0c writes reg then reads 1 byte.
ENGINE_I2C_ADDR = 0x30
STATUS_OK = 0x1234     # reply[2..3] from the firmware's command interpreter
STATUS_ERR = 0x5678


class F50:
    def __init__(self):
        self.dev = usb.core.find(idVendor=VID, idProduct=PID)
        if self.dev is None:
            raise SystemExit("SmartPhoto F50 not found")

    def cmd(self, opcode, payload=b"", timeout=2000):
        """Send one 64-byte command block and return the 64-byte reply."""
        if opcode in FORBIDDEN:
            raise ValueError(f"opcode {opcode:#04x} is blocked (it can rewrite the scanner's EEPROM)")
        block = struct.pack("<H", opcode) + bytes(payload)
        block = block.ljust(BLOCK, b"\0")
        n = self.dev.ctrl_transfer(RT_OUT, REQ, 0x0011, 0x0001, block, timeout=timeout)
        if n != BLOCK:
            raise IOError(f"short command write: {n}")
        return bytes(self.dev.ctrl_transfer(RT_IN, REQ, 0x0010, 0x0000, BLOCK, timeout=timeout))

    def fw_version(self):
        r = self.cmd(OP_FW_VERSION)
        return ".".join(str(b) for b in r[4:8]), r

    def proto_version(self):
        r = self.cmd(OP_PROTO_VERSION)
        return ".".join(str(b) for b in r[4:7]), r

    def endpoints(self):
        r = self.cmd(OP_ENDPOINTS)
        return r[4:9], r

    def get_var(self, opcode):
        r = self.cmd(opcode, bytes([0, 0, 2]))
        return r[5], r

    def status(self, reply):
        return struct.unpack_from("<H", reply, 2)[0]

    def read_regs(self, addrs):
        """Read engine registers. Block: [4..5]=count, [6+2i]=addr; reply [7+2i]=value."""
        out = {}
        for i in range(0, len(addrs), 29):
            chunk = addrs[i:i + 29]
            payload = struct.pack("<H", len(chunk)) + b"".join(bytes([a, 0]) for a in chunk)
            r = self.cmd(OP_REG_READ, bytes(2) + payload)
            if self.status(r) != STATUS_OK:
                raise IOError(f"reg read failed, status {self.status(r):#06x}: {hexdump(r)}")
            for j, a in enumerate(chunk):
                out[a] = r[7 + 2 * j]
        return out

    def dump_regs(self):
        regs = self.read_regs(list(range(256)))
        lines = []
        for row in range(0, 256, 16):
            lines.append(f"{row:02x}: " + " ".join(f"{regs[a]:02x}" for a in range(row, row + 16)))
        return regs, "\n".join(lines)


def hexdump(b):
    return " ".join(f"{x:02x}" for x in b)


if __name__ == "__main__":
    s = F50()
    for name, fn in (("fw_version", s.fw_version), ("proto_version", s.proto_version), ("endpoints", s.endpoints)):
        try:
            val, raw = fn()
            print(f"{name}: {val}\n   raw: {hexdump(raw)}")
        except usb.core.USBError as e:
            print(f"{name}: USB error {e}")
            sys.exit(1)
    for name, op in (("var_6c (0x19)", OP_VAR_6C), ("var_6d (0x0a)", OP_VAR_6D)):
        val, raw = s.get_var(op)
        print(f"{name}: {val}\n   raw: {hexdump(raw)}")
    if "--regs" in sys.argv:
        regs, text = s.dump_regs()
        print("engine registers (I2C 0x30), read-only dump:")
        print(text)
