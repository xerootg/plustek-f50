#!/usr/bin/env python3
"""Minimal userspace driver for the Plustek SmartPhoto F50 (USB 07b3:0b00).

Protocol (decoded from the scanner's own firmware, see docs/NOTES.md):
  vendor request 0x04
  OUT  bmRequestType 0x40, wValue 0x0011, wIndex 0x0001, 64 bytes: command block
  IN   bmRequestType 0xC0, wValue 0x0010, wIndex 0x0000, 64 bytes: reply block
Bytes 0..1 of a block are a little-endian opcode. Image data comes back on
bulk endpoint 0x82.

The capture path replays what the Windows COM server (WDUScan.exe) does, see
docs/NOTES.md "Windows capture sequence". Sensor (OV5620) register writes are
volatile; a USB replug restores the power-on state. Run as a script to print
firmware and endpoint info:
  sudo python3 -I tools/f50.py
"""
import struct
import sys
import time

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

OP_CAPTURE = 0x05      # sets firmware flag bit4: arm one frame into bulk EP 0x82
OP_STOP = OP_READ_CLEAR_FLAG3  # 0x07: returns flag bit3, clears bits 3 and 4
EP_BULK = 0x82

# The 54 OV5620 registers WDUScan.exe reads, edits and writes back as one list
# (table at 0x413030 in WDUScan.exe, same as 0x10016340 in Scandrv.dll).
SENSOR_REGS = [
    0x00, 0x01, 0x02, 0x03, 0x04, 0x09, 0x0c, 0x0d, 0x0e, 0x0f, 0x10, 0x11, 0x12, 0x13,
    0x14, 0x15, 0x16, 0x17, 0x18, 0x19, 0x1a, 0x1b, 0x24, 0x25, 0x26, 0x2a, 0x2b, 0x2d,
    0x2e, 0x2f, 0x30, 0x31, 0x32, 0x45, 0x46, 0x47, 0x48, 0x49, 0x5f, 0x60, 0x61, 0x63,
    0x80, 0x81, 0x83, 0x84, 0x85, 0x89, 0x8b, 0xa8, 0xa9, 0xb8, 0xb9, 0xba,
]

# Frame geometry the Windows driver assumes (Scandrv.dll FUN_100053b0): 0xa20 x 0x798
# 8-bit Bayer samples, read into a 0x4ce300-byte buffer.
WIN_WIDTH, WIN_HEIGHT = 2592, 1944
WIN_READ_BYTES = 0x4CE300


def win_gain_reg(g):
    """WDUScan.exe FUN_00402220: float gain -> register 0x00 (bug-for-bug)."""
    n = int(g)
    if n > 16:
        raise ValueError("gain too large")
    hi = ((n & 0xFE) * 8) & 0xFF
    return (int((g - hi) * 16.0 + 0.5) + hi) & 0xFF


def win_set_exposure(r, e):
    """WDUScan.exe FUN_00402460: exposure (lines) into 0x04[2:0], 0x10, 0x45[5:0]."""
    r[0x04] = (r[0x04] & ~0x07 | (e & 0x07)) & 0xFF
    r[0x10] = (e >> 3) & 0xFF
    r[0x45] = (r[0x45] & ~0x3F | ((e >> 10) & 0x3F)) & 0xFF


def win_set_window(r, h0, h1, v0, v1):
    """WDUScan.exe FUN_004025f0: HSTART/HSTOP in 0x17/0x18 + 0x32, VSTART/VSTOP in 0x19/0x1a + 0x03."""
    r[0x32] = (r[0x32] & ~0x07) | (h0 & 7)
    r[0x17] = (h0 >> 3) & 0xFF
    r[0x32] = (r[0x32] & ~0x38) | ((h1 & 7) << 3)
    r[0x18] = (h1 >> 3) & 0xFF
    r[0x03] = (r[0x03] & ~0x03) | (v0 & 3)
    r[0x19] = (v0 >> 2) & 0xFF
    r[0x03] = (r[0x03] & ~0x0C) | ((v1 & 3) << 2)
    r[0x1a] = (v1 >> 2) & 0xFF


def win_set_clkrc(r, bit6, div):
    """WDUScan.exe FUN_00402700: CLKRC 0x11, bit6 and 6-bit divider."""
    v = r[0x11] & 0xBF
    if bit6:
        v |= 0x40
    r[0x11] = (v & ~0x3F) | (div & 0x3F)


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

    def write_regs(self, pairs, verify=False):
        """Write engine registers via opcode 0x0b, at most 29 (reg, value) pairs per block."""
        pairs = [(a & 0xFF, v & 0xFF) for a, v in pairs]
        for i in range(0, len(pairs), 29):
            chunk = pairs[i:i + 29]
            payload = struct.pack("<H", len(chunk)) + b"".join(bytes(p) for p in chunk)
            r = self.cmd(OP_REG_WRITE, bytes(2) + payload)
            if self.status(r) != STATUS_OK:
                raise IOError(f"reg write failed, status {self.status(r):#06x}: {hexdump(r)}")
        if verify:
            back = self.read_regs([a for a, _ in pairs])
            return {a: (v, back[a]) for a, v in pairs if back[a] != v}
        return {}

    def win_setup(self, exposure=600, gain=1.20, wb=(1.80, 1.20, 1.00), autogain=True,
                  final=True, log=print):
        """Replay WDUScan.exe FUN_00402930 in scan mode (param_2=1, param_5=1).

        Defaults are what Scandrv.dll passes from Reg.ini: ExposureTime=600, Gain=120
        (x0.01), RGBGamma=180,120,100 (x0.01). Returns the register dict written last.
        """
        r = self.read_regs(SENSOR_REGS)
        r = self.read_regs(SENSOR_REGS)
        win_set_window(r, 0x90, 0x5A0, 8, 0x560)
        r[0x04] &= 0x3F
        r[0x13] &= ~(0x80 | 0x04 | 0x02 | 0x01) & 0xFF     # COM8: fast AEC, AGC, AWB, AEC off
        r[0x00] = win_gain_reg(gain)
        r[0x02] = int(round(wb[0] * 64.0)) & 0xFF
        r[0x16] = int(round(wb[1] * 64.0)) & 0xFF
        r[0x01] = int(round(wb[2] * 64.0)) & 0xFF
        win_set_exposure(r, exposure)
        r[0x12] &= 0x87
        win_set_clkrc(r, 1, 0)
        r[0x32] &= 0x3F
        r[0x14] |= 0x06
        r[0x24], r[0x25] = 0x58, 0x38
        r[0xb8] = r[0xb9] = r[0xba] = 0xE2
        self.write_regs([(a, r[a]) for a in SENSOR_REGS])
        self.write_regs([(0x80, 0x09), (0x81, 0x07)])
        time.sleep(0.4)
        if autogain:
            g, done, n, t0 = 1.0, False, 0, time.time()
            while n < 21 and not done:
                r = self.read_regs(SENSOR_REGS)
                if g < 0.1 or g > 3.9:
                    g = 0.1 if g < 0.1 else 3.9
                    r[0x00] = win_gain_reg(g)
                    self.write_regs([(a, r[a]) for a in SENSOR_REGS])
                    done = True
                yavg = r[0x2f]
                log(f"  autogain: g={g:.1f} reg00={r[0x00]:#04x} YAVG(0x2f)={yavg:#04x}")
                if yavg < 0x38 or yavg > 0x58:
                    g += 0.1 if yavg < 0x38 else -0.1
                    n += 1
                    r[0x00] = win_gain_reg(g)
                    self.write_regs([(a, r[a]) for a in SENSOR_REGS])
                elif time.time() - t0 > 1.0:
                    done = True
                time.sleep(0.3)
        if final:
            r = self.read_regs(SENSOR_REGS)
            win_set_exposure(r, exposure >> 3)
            win_set_clkrc(r, 1, 3)
            self.write_regs([(a, r[a]) for a in SENSOR_REGS])
            time.sleep(3.0)
        return self.read_regs(SENSOR_REGS)

    def capture(self, nbytes=WIN_READ_BYTES, timeout=6000, arm_timeout=10.0, log=print):
        """Grab nbytes from the start of the next frame.

        Linux caps usbfs buffers at /sys/module/usbcore/parameters/usbfs_memory_mb (16 MB by
        default), so keep nbytes under that (3 frames need it raised).

        Sequence from WDUScan.exe FUN_00404150 (op 7, op 5, reset pipe, read, op 7), adapted
        to Linux/xHCI. The firmware only marks EP2 valid in its VSYNC interrupt after op 5,
        so IN tokens before that get no handshake (-EPROTO on xHCI). We keep re-issuing a
        single read of the full size: when it finally succeeds, EP2 went valid at a VSYNC
        and the read started at the top of a frame. One large read lets libusb queue every
        URB at once, so the 4 KiB device FIFO never overflows between reads (separate
        smaller reads drop bytes at each gap and shift the Bayer phase).
        """
        try:
            usb.util.claim_interface(self.dev, 0)
        except usb.core.USBError:
            pass
        self.cmd(OP_STOP)
        self.dev.clear_halt(EP_BULK)
        self.cmd(OP_CAPTURE)
        t0 = time.time()
        tries = 0
        data = None
        while time.time() - t0 < arm_timeout:
            try:
                # whole 512-byte packets, or the last URB overflows (EOVERFLOW)
                data = self.dev.read(EP_BULK, (nbytes + 511) // 512 * 512, timeout=timeout)[:nbytes]
                break
            except usb.core.USBTimeoutError:
                log("  bulk timeout")
                break
            except usb.core.USBError as e:
                # EP2 not valid yet: xHCI reports the missing handshake as -EPROTO, which
                # libusb maps to LIBUSB_ERROR_IO (errno 5). Anything else is a real error.
                if e.errno != 5:
                    self.cmd(OP_STOP)
                    raise
                tries += 1
                self.dev.clear_halt(EP_BULK)
        dt = time.time() - t0
        r = self.cmd(OP_STOP)
        self.dev.clear_halt(EP_BULK)
        n = 0 if data is None else len(data)
        log(f"  read {n} bytes, {tries} retries before EP2 went valid, {dt:.2f} s; op7 overflow flag={r[4]}")
        return bytes(data) if data is not None else b""

    def dump_regs(self):
        regs = self.read_regs(list(range(256)))
        lines = []
        for row in range(0, 256, 16):
            lines.append(f"{row:02x}: " + " ".join(f"{regs[a]:02x}" for a in range(row, row + 16)))
        return regs, "\n".join(lines)


def hexdump(b):
    return " ".join(f"{x:02x}" for x in b)


def save_pgm(path, data, width, height, maxval=255):
    """8-bit (maxval<256) or 16-bit big-endian PGM."""
    with open(path, "wb") as f:
        f.write(f"P5\n{width} {height}\n{maxval}\n".encode())
        f.write(data)


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
