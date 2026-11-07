<!-- Produced by the RE workbench (Kimi K3) from the Windows driver binaries and the
device firmware, 2026-11-06/07. Labels: proven / inferred / guess as written by the
workbench. Read 06-corrections-and-gaps.md for the review pass; where this file and
firmware.md disagree, firmware.md wins, and where firmware.md and 06 disagree, 06 wins. -->

# 3. Ordered command sequences

Conventions: `OUT`/`IN` = the control pair from 01. Blocks are shown as
`[op, 00, 00, 00, b4, b5, ...]` (bytes 2,3 of the request are 0; the reply
carries the constant tag 0x34,0x12 there). Ops are the Scandrv.dll internal
opcodes dispatched by `FUN_1000b2a0` through jump table `PTR_LAB_10016578`;
the TWAIN-facing `ScannerCommander` cases drive them.

Reg.ini values used below (shipped `Reg.ini`, section `ScndrvU`):
ExposureTime=600, Gain=120, RGBGamma=180,120,100, LampTurnOffTime=5,
WarmUpPeriod=60, BufferSize=4, MaxDpi (Reg.ini has no MaxDpi -> default 0?
`FUN_10008cf0(MaxDpi,0,0,0x4b00)`), FWVersion=0.6.1.8. [proven:
`FUN_10008d20` + `Reg.ini`]

## 3.0 Server/DLL startup (per process) [proven]

1. `FUN_1000bd10`: find scanner device interface (GUID via
   `\\\\.\\HCD%d`, registry `ScannerID` path) -> launch WDUScan.exe if the
   COM server isn't running -> `CoCreateInstance(IWDUFilmScanner)`.
2. Immediately: `block = [0x0a,00,00,00, 01,01, 00..]` OUT+IN —
   opcode 0x0a SET mode var = 1 (firmware `DAT_INTMEM_6d = 1`).

## 3.1 Device open (ScannerCommander case 0) [proven]

1. op 0 (internal): acquire named mutex `Global\<suffix>_SCANNER_OCCUPY_Register`
   (error 1 if another process holds it). Not USB.
2. op 4 (`FUN_100055b0`): read `DeviceCaps`/`SupportDpiX`/`SupportDpiY`
   lists from Reg.ini; `FUN_1000b950` sends `block=[01,00,00,00,...]` and
   stores reply[4..6] as the engine protocol version (device returns 1,2,4).
3. `FUN_100021e0` -> op 6: fill capability struct from globals; misc host
   setup (temp file `Plktemp.tmp`).
4. First device contact — op 0x1d handler (0x10006100):
   a. `FUN_1000b9a0`: close any old COM/handles (idempotent).
   b. `Sleep(1)`.
   c. if `DAT_10017e91 == 0` (stream not yet started): `FUN_10007960` ->
      `block=[09,00,00,00, 04,01, 00..]` OUT+IN (opcode 9 START_STREAM;
      note: before opcode 5 ARM has ever been sent this is a firmware no-op
      — opcode 9's handler only arms EP2 when flag bit4 is set).
      [proven; the conditional from r2 asm]
   d. `DAT_10017dde = 1`.
   e. `FUN_100088f0` — **the 54-register OV5620 init** (see 02):
      - `block=[0x0c,00,00,00, 0x1d,00, <29 addrs>]` IN -> live values
      - `block=[0x0c,00,00,00, 0x19,00, <25 addrs>]` IN (2nd chunk)
      - apply modifiers (window 144/1440/8/1376, exposure V=600 ->
        (0x04,0x00)(0x10,0x4b)(0x45,&~0x3f), gain -> 0x00=0x13,
        channel gains 0x02=0x73,0x16=0x4d,0x01=0x40, COM8->0x40,
        CLKRC|=0x40, COM9|=0x06, 0x0c|=0x40, 0xb8/0xb9/0xba=0xe2,
        0x24=0x58, 0x25=0x38)
      - `block=[0x0b,00,00,00, 0x1d,00, <29 (addr,value)>]` OUT+IN
      - `block=[0x0b,00,00,00, 0x19,00, <25 (addr,value)>]` OUT+IN
      - `block=[0x0b,00,00,00, 02,00, 80,09, 81,07]` OUT+IN
        (final commit pair; exact packing [proven]: two entries in one
        0x0b write: {0x80,0x09},{0x81,0x07})
   f. `FUN_10005890`: `block=[0x0a,00,00,00, 02,00, 00..]` OUT+IN;
      requires reply[5]==1 (the mode var set at 3.0). Fail -> open fails.

## 3.2 Lamp on / scan start (case 0x11) [proven]

1. op 0x0f (0x1000a580): `FUN_10007950` (nop stub), `FUN_10009ab0`
   (allocate line buffers from BufferSize MB, pick the per-format line
   converter `DAT_10017eac`, spawn consumer thread `LAB_100096c0`),
   `FUN_100062b0` (fill B&W threshold LUTs from `DAT_10017fc1`).
2. op 0x16, 0x17: **stubs, return 0** (`xor ax,ax; ret` at 0x100060f0) —
   the 40 s "wait for scanner" loop in case 0x11 always exits immediately
   in this driver version.
3. if `DAT_1001666e > 1`: op 0x14 (0x10005fe0) sets mode byte
   `DAT_10017dc9 = param & 0xff` (param from TWAIN side; read back by op
   0x15). Not USB.
4. op 0x18 = **LAMP ON + START STREAM** (0x10006020):
   a. `Sleep(500)`.
   b. `FUN_100079a0(8)`: `block=[08,00,00,00, 00..]` OUT+IN (ping/ack).
   c. if `DAT_10017e67 == 1`: preload `block[6..7] = WarmUpPeriod` (60) —
      otherwise those bytes stay 0.
   d. `FUN_10007960(DAT_10017e91)`: if the stream flag is 0:
      `block=[09,00,00,00, 04,01, 3c,00, 00..]` OUT+IN
      (opcode 9 START_STREAM, [4]=4, [5]=1, [6..7]=WarmUpPeriod LE16).
   e. returns `WarmUpPeriod * 125` ms (60 -> 7500 ms) to the caller, which
      waits that long (lamp warm-up) while pumping progress callbacks.
   [proven from r2 asm; WarmUpPeriod*125 from `lea eax,[eax+eax*4]` x3]
5. op 0x19 (poll) / op 0x1a: stubs, return 0/1. No USB.

Note: the firmware only starts EP2 streaming on opcode 9 if opcode 5's flag
(bit4 of `DAT_INTMEM_20`) is already set — but opcode 5 is only sent inside
DoCaptureImage / calibration right before the actual frame read. So the
lamp-on phase starts the stream machinery, and each frame grab re-arms it.
[inferred from firmware 0x0305 + host order]

## 3.3 Per-frame acquire (case 0x12 -> `FUN_10002d80` -> op 0x10 loop)

op 0x10 (0x1000aa90), once per film frame position:

1. `FUN_100054e0(reqW, reqH, inW, inH)` computes the 4 window words (see
   below); `FUN_10005560`:
   `block=[03,00,00,00, w1lo,w1hi, w2lo,w2hi, w3lo,w3hi, w4lo,w4hi, 00..]`
   OUT+IN. [proven]
2. `FUN_10009870`: build the host line-offset table (memory only).
3. `FUN_100053b0`:
   a. allocate `0x4ce300` (5,038,848) and, if 4-plane mode, `0xe6a900`;
   b. **DoSensorAdjust** COM call (`FUN_1000bec0`) with args
      (1, ExposureTime=600, Gain/100=1.2, 1, 1.8, 1.2, 1.0, &out8) —
      the EXE (`fcn.00402930`) then runs the full 54-register
      read/modify/write cycle (same as 3.1e) plus a converge loop with
      `Sleep(400)` / `Sleep(300)` and repeated reg writes;
   c. **DoCaptureImage** COM call (`FUN_1000bf30`, buffer arg) —
      EXE `fcn.00404150`:
      - raise process/thread priority (HIGH/REALTIME),
      - `block=[07,00,00,00, 00..]` OUT+IN (STOP/STATUS: read busy, clear
        stream flag),
      - retry loop (max 6):
        - `block=[05,00,00,00, 00..]` OUT+IN (ARM/LAMP_ON: fw bit4=1),
        - `WDU_ResetPipe(0x82)`,
        - stream-read the whole frame from EP 0x82
          (`fcn.00401b70`: stream buffer 0x735480 = 7,578,752 bytes,
          exactly `0x4ce300` bytes delivered to the DLL),
        - on success break,
      - `block=[07,00,00,00, 00..]` OUT+IN (STOP/STATUS again),
      - restore priority;
   d. demosaic into RGB planes (see 04).
4. `FUN_1000a630` + consumer thread assemble output lines; host callback
   delivers lines to the TWAIN app.

Between frames: nothing is sent to the device by the DLL itself; the
event-pump thread (below) may report the holder-advance button. The lamp
stays on (no opcode 7 between frames except inside DoCaptureImage).
[proven from code structure; inferred intent]

## 3.4 Stop / lamp off (case 0x13) [proven]

op 0x11 (0x1000b1d0):
1. wait for / terminate the consumer thread (2000 ms), close its handles;
2. `FUN_10005470`: `block=[07,00,00,00, 00..]` OUT+IN — opcode 7
   STOP/STATUS: firmware reply[4] bit0 = busy, then clears stream flag
   (lamp off);
3. `FUN_100097d0`: free the frame buffers and semaphores.

Lamp auto-off after `LampTurnOffTime` (5 min) is a host-side timer
[inferred from `FUN_10008fd0` resetting the Reg.ini value + the name].

## 3.5 Close (case 1) [proven]

`FUN_100035d0` -> op 1 (0x1000c2a0): release the SCANNER_OCCUPY mutex;
op 2/3: release the scan-thread handles (0x10005b70/0x10005c10).
The COM server keeps running (cached for the next session).

## 3.6 Calibration (op 0x2b = `FUN_10005030`) [proven]

Runs on a dedicated event-thread pair:
1. spawn event-pump thread `LAB_10004ed0`: own COM instance; loop while
   `DAT_10017ce8==1`: `WaitForMultipleObjects(2, &handles, 0, 3000)` then
   `GetEventValue` (vtable +0x34): dword bits -> SetEvent on
   device-change(0x2)/button(0x1) events; 50 ms idle sleep.
2. alloc 0x4ce300 buffer; 3 iterations of exposure pairs from
   {FUN_10008aa0(10), FUN_10008aa0(0x200)}:
   - `FUN_10008aa0(V)`: full 54-reg read-modify-write with only the
     exposure fields changed: opcode 0x0c read (2 chunks),
     `FUN_10008060` patches `(0x04,&~7|V&7)(0x10,V>>3)(0x45,&~0x3f|(V>>10)&0x3f)`,
     opcode 0x0b write (2 chunks) [proven: `FUN_10008aa0` calls
     `FUN_10008680`/`FUN_100087d0` with count 0x36],
   - wait for `DAT_1001a4a0==1` (10 s timeout; set by the event path),
   - `block=[05,00,00,00, 00..]` (ARM),
   - `FUN_10001f40` = `FUN_1000b9e0`: **WDU_StreamRead 0x4ce000 bytes**
     (0x4ce300-768) from EP 0x82,
   - `FUN_10004a10`/`FUN_10004e10` accumulate per-channel min/max/sum
     statistics over the frame (used for shading/white calibration
     (`Shading.bin`) and the gamma tables).
3. on finish/error: kill event thread (`FUN_10005a50`-style teardown:
   Sleep(1000), post, terminate), free buffer.

## 3.7 The four window words (opcode 3) — formulas [proven]

`FUN_100054e0(p1=reqW, p2=reqH, p3=DAT_10017f88, p4=DAT_10017f8a)`
with `DAT_10017e00 = {lo: native_x, hi: native_y}` (sensor 300-dpi basis
from DeviceCaps):
```
w1 = p3                                              # input width px
w2 = p4                                              # input height px
w3 = ((p1 * p3) / DAT_10017e00.lo + 3) & ~3          # output width px (rounded)
w4 = (p2 * p4) / DAT_10017e00.hi                     # output height px
```
`DAT_10017f88/f8a` come from op 0x0e (`FUN_10009fc0`): requested pixel
dims scaled by `DeviceCaps[2]/300`. For 1800 dpi color these reduce to the
native 2592x1944 (scaling factor 6); lower resolutions give smaller w3/w4 —
the decimation to the requested dpi is done **in the DLL**, the sensor
always streams the full native frame. [proven]

## 3.8 Firmware upload (op 0x2e, only if needed) [proven]

op 0x2e (0x10006170): builds `<path>\FWofWin.bin` and calls `FUN_1000c000`
(vtable +0x30? — actually the COM "WriteFirmware" path, GetState(1) is used
beforehand to decide whether the device's firmware is already running).
The upload itself goes through the same WDU control channel; the 8051 reset
handler at firmware `cmd_07ba` reads `SETUPDAT[1]` (0xe6b9) and calls
`func_0x1f0d(0xa08, ...)`. Firmware upload was not needed for the driver's
normal flow (device keeps its EEPROM firmware); not fully traced. [partial]
