<!-- Produced by the RE workbench (Kimi K3) from the Windows driver binaries and the
device firmware, 2026-11-06/07. Labels: proven / inferred / guess as written by the
workbench. Read 06-corrections-and-gaps.md for the review pass; where this file and
firmware.md disagree, firmware.md wins, and where firmware.md and 06 disagree, 06 wins. -->

# 5. Handshakes, waits and status polls

Everything timing/poll related found in the DLL/EXE, classified:

| what | mechanism | tag |
|------|-----------|-----|
| Per-command serialization | named mutex `Global\<suffix>_IO_Mutex`, 10 s wait (`FUN_10004250`) | [proven] |
| Process-wide scanner lock | named mutex `Global\<suffix>_SCANNER_OCCUPY_Register` (ops 0/1) | [proven] |
| WDU transfer timeout | 30000 ms per control transfer (`FUN_1000bb60`/`bc30`, EXE `fcn.004019d0`/`a40`) | [proven] |
| Stream read timeout | 30000 ms (same constant in the WDU_StreamRead path `FUN_1000b9e0`) | [proven] |
| Open probe | opcode 9 once (conditional on stream flag), then 54-reg init, then opcode 0x0a GET mode var, expect reply[5]==1 (`FUN_10005890`) | [proven] |
| Lamp-on sync | `Sleep(500)` -> opcode 8 (ack ping) -> opcode 9 start -> host waits `WarmUpPeriod*125` ms (7.5 s default) before acquiring (op 0x18 + case-0x11 wait loop) | [proven] |
| Frame-ready handshake in calibration | event flag `DAT_1001a4a0` (10 s wait) -> opcode 5 ARM -> stream read | [proven mechanism; who sets the flag: the event/COM callback path — inferred] |
| Busy/status poll | opcode 7 reply[4] bit0 (firmware bit3 of `DAT_INTMEM_20`); also clears the flag (used before+after every frame grab and at stop) | [proven] |
| Op-poll stubs | internal ops 0x16/0x17/0x19 (`0x100060f0`) are `xor ax,ax; ret` — the 40 s "wait for scanner" loop in ScannerCommander case 0x11 is dead code in this version | [proven] |
| DoCaptureImage retry | up to 6 attempts of {opcode 5, ResetPipe(0x82), stream read} | [proven, EXE `fcn.00404150`] |
| DoSensorAdjust converge | repeated 54-reg read/modify/write with `Sleep(400)` + `Sleep(300)` between passes (EXE `fcn.00402930`) until the adjust converges (AEW=0x58/AEB=0x38 targets in regs 0x24/0x25) | [proven structure; exact exit condition not traced] |
| DoCaptureImage priority boost | SetPriorityClass HIGH/REALTIME + THREAD_PRIORITY 2 during the grab, restored after | [proven] |
| Event/channel poll | event thread `LAB_10004ed0`: 3 s wait on 2 handles + `GetEventValue` (vtable +0x34): dword bit1 -> device-change event, bit0 -> button event; 50 ms idle sleep | [proven] |
| Firmware activity LED | firmware toggles P0.7 when a reply differs from the request (i.e. on any real command) — visible activity blink per command [proven from `cmd_0df0`] |
| Firmware "host ready" gate | opcode 9 only arms EP2 if opcode 5's flag bit (INTMEM 0x20 bit4) is set; opcode 7 clears bits 3+4 | [proven from firmware 0x027c/0x02b8/0x0305 asm] |
| Ping / sync | opcode 8 (tabled ack) and opcode word 0x0128 (untabled -> no reply staged, IN stalls to the 30 s timeout, failure ignored) used as connection checks | [proven; see 01 "Ping"] |
| Version handshake | open: opcode 1 -> reply "1.2.4" protocol bytes stored; op 0x12 compares packed Reg.ini `FWVersion` (0.6.1.8) with the device value and returns 0x200/0x400/0xfffb | [proven] |

## Minimal libusb bring-up sequence (suggested; inferred from the above)

```
1. control pair  [0x0a,00,00,00, 01,01]            # mode var = 1
2. control pair  [0x0c,00,00,00, 1d,00, <29 addr>] # read 54 regs (2 chunks)
3. modify per 02-init, write back with 0x0b (2 chunks)
4. control pair  [0x0b,00,00,00, 02,00, 80,09, 81,07]
5. control pair  [0x0a,00,00,00, 02,00]  -> reply[5]==1?
6. sleep 500 ms; [08,00,00,00]           # ping
7. [09,00,00,00, 04,01, 3c,00]           # start stream (warmup 60)
8. sleep ~7.5 s (lamp warm-up)
9. per frame:
     [07,00,00,00]                       # status/clear
     [05,00,00,00]                       # arm
     clear halt on EP 0x82
     bulk read exactly 0x4ce300 bytes    # one 2592x1944 Bayer frame
     [07,00,00,00]                       # stop/clear
10. at close: [07,00,00,00]              # lamp off / stop
```

The 400/300 ms DoSensorAdjust loop can probably be replaced by a single
fixed exposure write for a first Linux driver (auto functions are off:
COM8=0x40). [inferred]
