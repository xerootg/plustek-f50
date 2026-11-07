<!-- Produced by the RE workbench (Kimi K3) from the Windows driver binaries and the
device firmware, 2026-11-06/07. Labels: proven / inferred / guess as written by the
workbench. Read 06-corrections-and-gaps.md for the review pass; where this file and
firmware.md disagree, firmware.md wins, and where firmware.md and 06 disagree, 06 wins. -->

# 1. COM wrappers and the 64-byte block protocol

## 1.1 USB transfer shape [proven]

Every command is a **pair** of control transfers (vendor request **0x04**),
always serialized under the named mutex `Global\<suffix>_IO_Mutex`
(`FUN_10004250` builds the name with `wsprintfA`, mutex wait 10000 ms):

| dir | bmRequestType | bRequest | wValue | wIndex | wLength | payload |
|-----|---------------|----------|--------|--------|---------|---------|
| OUT | 0x40 | 0x04 | 0x0011 | 0x0001 | 64 | command block |
| IN  | 0xC0 | 0x04 | 0x0010 | 0x0000 | 64 | reply block |

DLL evidence: `FUN_100041f0`/`FUN_10004220` push `0x40, 4, wValue, wIndex, buf, 0x40`
into `FUN_1000bb60`/`FUN_1000bc30`; callers pass `(0x11, 1)` for OUT and
`(0x10, 0)` for IN (`FUN_10004250`, `FUN_100087d0`, `FUN_100053f0`).
EXE evidence: `fcn.004019d0`/`fcn.00401a40` called with the same constants,
timeout 30000 ms (`0x7530`).

The firmware EP0 handler (`cmd_0df0`/`cmd_0e00`) confirms: on OUT it copies
64 bytes XDATA 0x2800..0x283F, saves bytes [0],[1] in `DAT_INTMEM_5d/5e`,
runs the dispatcher; on IN it re-runs the dispatcher and ships the 64 bytes
back. The dispatcher (`code 0x166`) writes the constant tag **reply[2]=0x34,
reply[3]=0x12** into every reply [proven from firmware asm at 0x177..0x181],
then scans the 4-byte-entry jump table at code 0x019a
`{handler_hi, handler_lo, 0x00, opcode}` matching `entry[3]==block[0] &&
entry[2]==block[1]`; so **byte 0 is the opcode and byte 1 must be 0**
[block[1]!=0 never matches -> treated as unknown].

Unknown opcode: the table terminator jumps to 0x07b5 (clear "handled" flag)
and control **falls through into the opcode-0 handler** (firmware asm
0x19a: `ljmp 0x0200`), so an unknown opcode returns the firmware version
block — the DLL uses opcode word **0x0128** as exactly such a ping
(`FUN_100079a0(0x128)`, seen in the scan-setup path `FUN_10009fc0`).
[proven from firmware asm; the ping use is inferred]

Reply byte [0]: the dispatcher increments block[0] (reply echo =
request opcode + 1, sometimes +2; the second increment is conditional on an
SFR comparison the decompile could not resolve) [proven there is an
increment; exact +1-vs-+2 rule unresolved]. The DLL does not appear to
validate the echo.

## 1.2 The six COM wrappers in Scandrv.dll [proven]

`FUN_1000b810` reads the vtable pointer global `DAT_10017d98`
(`IWDUFilmScanner*`); the object is created by `FUN_1000bd10` with
`CoCreateInstance` (CLSID bytes at `DAT_100141f0`/`0x100141f0`, ctx 7) after
ensuring the server process runs (`FUN_1000b6d0`/`FUN_1000b360` look the
server path up via the `\\\\.\\HCD%d`/`ScannerID` device enumeration).
Vtable layout recovered from the embedded typelib + call sites:

| vtable off | method | DLL wrapper | purpose |
|------------|--------|-------------|---------|
| +0x00..0x14 | IUnknown/IDispatch | — | standard |
| +0x18 | ? | `FUN_1000bc90` (used by `FUN_1000b9a0`) | release/close path helper |
| +0x1c | ? | called from `FUN_1000b9a0` | teardown |
| +0x20 | (unused?) | `FUN_1000bcc0` | passes wValue/wIndex/buf/len through |
| +0x24 | ? | `FUN_1000bcc0` | same |
| +0x28 | ? | `FUN_1000bf80` | bulk read into buffer (chunked read used by `FUN_1000b9e0`) |
| +0x2c | **DoSensorAdjust** | `FUN_1000bec0` | full sensor adjust sequence (see 03) |
| +0x30 | **GetState** | `FUN_1000c000` (arg0=1), `FUN_1000bf80` (arg0=2) | EXE `fcn.004043b0`: 1 -> return global dword (busy flag), 2 -> return global byte (state var) |
| +0x34 | **GetEventValue** | `FUN_1000be70` | EXE `fcn.004037f0`: returns 3 event dwords (device-change/button flags) |
| +0x38 | **fAWBRed** | — | EXE `fcn.00402b50`: reads 64B block, returns block[5] as float (opcode block built by `fcn.00401ec0`) |
| +0x3c | **fAWBGreen** | — | EXE `fcn.00402cf0`: same, block[6]? (same builder) |
| +0x40 | **fAWBBlue** | — | EXE `fcn.00402e90`: same, block[7]? |
| +0x44 | **DoCaptureImage** | `FUN_1000bf30` | EXE `fcn.00404150`: grab one full 2592x1944 frame (see 04) |

Mapping of the six wrappers the task asked about:

- `FUN_1000bb60` = **control OUT** (builds setup `{0x40, 4, wValue, wIndex}`,
  calls the vtable write method with the caller's 64-byte block). [proven]
- `FUN_1000bc30` = **control IN** (setup `{0xC0, 4, wValue, wIndex}`, returns
  64 bytes into the caller's block). [proven]
- `FUN_1000be70` = **GetEventValue** (vtable +0x34) — event read. Called by
  the DLL only from the internal event-pump thread `LAB_10004ed0` (see 03);
  the decompile-visible DLL code never calls it otherwise. [proven]
- `FUN_1000bf30` = **DoCaptureImage** (vtable +0x44): passes one pointer arg
  (the DLL's 5 MB frame buffer `DAT_10017ce4`); on return the wrapper copies
  0x4ce300 bytes out of the returned safearray. [proven]
- `FUN_1000c000` = **GetState(1)** (vtable +0x30, arg0=1): "firmware ready?"
  boolean; used by `FUN_1000bd10` (decides whether to re-upload firmware)
  and by `FUN_10009d10`/`FUN_1000b2a0` paths. [proven]
- `FUN_1000bf80` = dual-use: GetState(2) (small state byte, used by
  `FUN_10005890`-era code) and, with the +0x28 method, a bulk buffer read
  (used by `FUN_1000b9e0`, the chunked stream-read helper). [proven]
- `FUN_1000c190` = **GetFirmwareVersion** (vtable +0x40? no — separate COM
  entry; decompile shows it returns `DAT_10017dd1/2/3` = "6.1.0"-style bytes
  filled by `FUN_1000b950` which sends block opcode word **0x0001** and
  reads reply[4..6]). Note the *device* firmware version "0.6.1.8" comes from
  firmware opcode 0x00, while this COM call returns the *server's* version.
  [proven for the mechanics, inferred for the semantic split]

## 1.3 Block layouts per opcode [proven from DLL builders + firmware]

Common: `[0]=opcode, [1]=0, [2]=0x34/[3]=0x12 in reply (tag), [4..]=params`.

| op | name (inferred) | request layout | reply layout | DLL sender / firmware handler |
|----|-----------------|----------------|--------------|-------------------------------|
| 0x00 | GET_VERSION | none | [4..7] = 0,6,1,8 | `FUN_1000b950`? (sends op 1 for COM ver) / fw `0x0202` |
| 0x01 | GET_PROTOCOL_VER? | none | [4]=1,[5]=2,[6]=4 | `FUN_1000b950` (block word 1, reads reply[4..6]) / fw `0x023e` |
| 0x02 | DEPOSIT_BYTE | [4]=byte | — | `FUN_100054a0` / fw `0x026d` (pure ack; byte stays at XDATA 0x2804) |
| 0x03 | SET_WINDOW | [4..5]=w1,[6..7]=w2,[8..9]=w3,[10..11]=w4 (all LE16) | — | `FUN_10005560` / fw `0x0272` (pure ack; firmware reads the deposited params later from XDATA 0x2804..0x280B when streaming) |
| 0x04 | (unused by DLL) | — | — | fw `0x0277` (pure ack) |
| 0x05 | ARM/LAMP_ON | none | — | calibration `FUN_10005030`, DoCaptureImage (`fcn.00404150`) / fw `0x027c`: sets bit4 of `DAT_INTMEM_20` |
| 0x06 | (unused by DLL) | — | — | fw `0x02b3` (pure ack) |
| 0x07 | STOP/STATUS | none | [4] bit0 = busy/streaming | `FUN_10005470`, DoCaptureImage before+after grab, op 0x11 / fw `0x02b8`: copies bit3 of `DAT_INTMEM_20` into reply[4] bit0, then clears bits 3 and 4 |
| 0x08 | PING | none | — | op 0x18 lamp-on path `FUN_100079a0(8)` / fw `0x0300` (pure ack) |
| 0x09 | START_STREAM | [4]=4, [5]=1, optional [6..7]=WarmUpPeriod (LE16) | — | `FUN_10007960` / fw `0x0305`: if bit4 of `DAT_INTMEM_20` set -> arms EP2 IN (sets bit7 of `EXTMEMe612`, writes `EXTMEMe604`), starts external IRQ0 |
| 0x0a | MODE_VAR | [4]=1,[5]=value to SET; [4]=2 to GET | [5]=value | `FUN_100054c0(10)` after block fill: set `FUN_1000bd10` (writes 1), get `FUN_10005890` (expects 1) / fw `0x06c1` (`DAT_INTMEM_6d`) |
| 0x0b | WRITE_REGS (OV5620 via SCCB 0x30) | [4..5]=count LE (max 29), then count x {addr,value} at [6..] | (ignored) | `FUN_100087d0` (DLL), `fcn.00401d20` (EXE) / fw `0x0474` |
| 0x0c | READ_REGS | [4..5]=count LE, then count x {addr,0} at [6..] | [7+2i]=value for entry i | `FUN_10008680` (DLL), `fcn.00401e90` (EXE) / fw `0x0505` |
| 0x0d | SET_FIFO? | [4..5]=LE16 → `DAT_INTMEM_48/47` | — | fw `0x0288` (not called by DLL) |
| 0x0e | GPIO/LED STATUS? | [4]=1->off,[5]=1->on | [4]=state, [5]=2 | fw `0x0413` (not in DLL main flow) |
| 0x0f | GET_ENDPOINTS? | — | [4]=0x82,[5]=2,[6]=0x83,[7]=3 | fw `0x0429` (not seen from DLL) |
| 0x10..0x12 | no-op | — | — | fw `0x07b7` |
| 0x13 | SET_FIFO_PARAM | [4..5]=LE16 -> `DAT_INTMEM_48/47` | — | fw `0x059a` (not called by DLL) |
| 0x14 | GPIO/LED | [4]=1->off,[5]=1->on | [4]=state, [5]=2 | fw `0x05fc` (`func_0x1898`: INTMEM 0x43/0x44, `P0 |= 0x0c`, GPIO via `func_0x182b`) — not in DLL main flow |
| 0x15 | WRITE_XDATA_2846 | [4]=value | — | fw `0x065e` (`DAT_INTMEM_4a` -> XDATA 0x2846) |
| 0x16 | I2C_WRITE_RAW | [4]=addr,[5]=value (device 0x30) | — | fw `0x06f6` (`func_0x1868` bit-bang SCCB) |
| 0x17 | I2C_WRITE_PAIR | [4]=a,[5]=b -> XDATA 0x2840/0x2841 | — | fw `0x0758` (`FUN_CODE_14d3`) |
| 0x19 | I2C_READ_RAW? | [4]=addr | [4]=value | fw `0x0781` (`func_0x1898`) |

### Firmware opcode dispatch table (authoritative)

Read from the raw firmware bytes at code 0x019a (entries `{hi, lo, 0x00,
opcode}`, terminator `{00,00,07,b5}` = 0x07b5 "mark unhandled"):

| op | handler | behavior (8051 asm verified) |
|----|---------|------------------------------|
| 00 | 0x0202 | reply[4..7] = 0,6,1,8 (firmware version) |
| 01 | 0x023e | reply[4]=1,[5]=2,[6]=4 (protocol version) |
| 02 | 0x026d | pure ack (params stay in XDATA 0x2800) |
| 03 | 0x0272 | pure ack (DLL uses it to deposit window words) |
| 04 | 0x0277 | pure ack |
| 05 | 0x027c | `DAT_INTMEM_20 |= 0x10` — set bit 4 (arm/lamp-on flag) |
| 06 | 0x02b3 | pure ack |
| 07 | 0x02b8 | reply[4] bit0 = bit3 of `DAT_INTMEM_20` (busy/streaming), then clears bits 3+4 (stop) |
| 08 | 0x0300 | pure ack (DLL uses it as ping) |
| 09 | 0x0305 | reads block[4]; if bit4 of `DAT_INTMEM_20` set: arms EP2 IN (`EXTMEMe612 |= 0x80`, writes `EXTMEMe604`), enables external IRQ0, starts sensor stream |
| 0a | 0x06c1 | mode var get/set (`DAT_INTMEM_6d`) |
| 0b | 0x0474 | write (addr,value) reg list via SCCB |
| 0c | 0x0505 | read reg list via SCCB |
| 0d | 0x0288 | FIFO param store |
| 0e | 0x0413 | (small helper, GPIO-related) |
| 0f | 0x0429 | reply[4]=0x82,[5]=2,[6]=0x83,[7]=3 (endpoint info) |
| 10 | 0x07b7 | no-op |
| 11 | 0x07b7 | no-op |
| 12 | 0x07b7 | no-op |
| 13 | 0x059a | store [4..5] -> `DAT_INTMEM_48/47` |
| 14 | 0x05fc | GPIO/LED on/off + state |
| 15 | 0x065e | store [4] -> XDATA 0x2846 |
| 16 | 0x06f6 | SCCB bit-bang write [4]=addr,[5]=value |
| 17 | 0x0758 | store pair -> XDATA 0x2840/0x2841 |
| 19 | 0x0781 | read via `func_0x1898` |

[proven from firmware hex + r2 8051 disassembly]

Ping: the DLL uses `FUN_100079a0(8)` (tabled pure-ack opcode 8) and
`FUN_100079a0(0x128)` (block word 0x0128 -> block[0]=0x28, untabled) as
connection syncs. For an untabled opcode the firmware stages **no reply**
(dispatcher carry=0 -> `cmd_0df0` jumps to 0x0f06 without arming EP0IN), so
the control-IN NAKs until the host's 30 s WDU timeout; the DLL ignores the
failure (`FUN_10004250` returns 1 unconditionally after invoking the IN
transfer). In practice the 0x0128 branch is probably never taken for the
F50 (it requires scan-flags & 0x300 == 0). [proven from firmware
`cmd_0df0`/dispatcher asm + DLL asm; "never taken in practice" inferred]

Firmware setup-packet validation (`cmd_0df0`): only bRequest 0x04 is
handled; the IN branch requires **wValue=0x0010 and wIndex=0** (SETUPDAT[2..5]
checked at 0xe11..0xe36); the OUT branch (0x0ea1) validates wValue=0x0011,
wIndex=1 before copying the 64 bytes to XDATA 0x2800 and running the same
dispatcher. The dispatcher increments block[0] (reply echo = opcode+1) and
writes the tag 0x34,0x12 at reply[2..3]; on a table match the handler's
reply bytes survive (the version handler does NOT run after a match — the
`pop DPH; pop DPL` at 0x0c95 consumes the call return address, which *is*
the table address 0x019a, so matched handlers return straight to the EP0
code). [proven from 8051 disassembly]
