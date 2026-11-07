<!-- Produced by the RE workbench (Kimi K3) from the Windows driver binaries and the
device firmware, 2026-11-06/07. Labels: proven / inferred / guess as written by the
workbench. Read 06-corrections-and-gaps.md for the review pass; where this file and
firmware.md disagree, firmware.md wins, and where firmware.md and 06 disagree, 06 wins. -->

# F50 8051 firmware — complete breakdown

Scope: `FWofWin.bin` (7955 B Cypress-style C2 image, shipped with driver v4.1.1.1) =
`firmware_flat_8051.bin` (64 KiB flat image, records applied; code covers
0x0000..0x1f38). The device's boot EEPROM (8 KB at I2C 0x51) was dumped read-only via
mailbox opcode 0x15: **its first 7955 bytes are byte-identical to `FWofWin.bin`**
(sha256 `4364a49a…db63ac` both), the remaining 237 bytes are 0xFF — the scanner runs
exactly the image analyzed here. [proven]

Method: radare2 (`r2 -a 8051`) disassembly of the flat image at exact entry points
(the linear sweep desyncs at jump tables; every table below was re-decoded from raw
bytes), cross-checked against the two Ghidra decompile files in the work directory.
The Ghidra 8051 decompiles are **unreliable for this target** (fused functions,
phantom `*ptr += 1` from desynced code) — where they conflict with the disassembly,
this document follows the disassembly and says so.

Authoritative register references: the staged Cypress FX2 TRM
(`wb_stage_trm/fx2_trm.txt`; cited as "TRM §…": ch. 3 = Enumeration/ReNumeration
incl. Table 3-5 "C2 Load" and vendor request 0xA0; ch. 4 = interrupts incl.
INT2VEC, EICON Table 4-5, EIE Table 4-6; ch. 6 = SUSPEND/WAKEUPCS; ch. 8 = EP0/EPx
control; ch. 9 + Figures 9-4/9-5 = slave FIFOs and WORDWIDE; §10.2.7 = 8/16-bit
data path; Appendix C = register summary) and the staged CY7C68013 datasheet
(`wb_stage_ds/cy7c68013.txt`, pin tables) — every FX2 bit name below was
verified against those two files.

Labels: **[proven]** = read directly in the code bytes or observed on the device;
**[inferred]** = deduced; **[guess]** = speculation.

## 0. Chip identity: an FX2-compatible part, not a GT-6816

[proven] The firmware uses the Cypress EZ-USB FX2 (CY7C68013) register map end to
end (FX2 TRM, Appendix C; § references below are to the staged `fx2_trm.txt`):

| address | reg | used by the firmware |
|---|---|---|
| 0xE600 | CPUCS | 0x1127/0x116a/0x16c0/0x185c/0x1876: =0x10 (48 MHz), then =0x12 (+CLKOE) |
| 0xE601 | IFCONFIG | =0xC3 at 0x1153 (internal 48 MHz IFCLK, slave FIFO), =0x43 at 0x16c6 (external IFCLK = sensor PCLK) |
| 0xE604 | FIFORESET | 0x16df: 0x80,2,4,6,8,0; 0x13df/0x1423 (VSYNC ISR) |
| 0xE609 | FIFOPINPOLAR | =0x3C at 0x1714 (SLWR/SLRD/SLOE/PKTEND active-high) |
| 0xE60B | REVCTL | =0x03 at 0x16ba (ENH_PKT+DYN_OUT, Cypress-recommended) |
| 0xE611 | EP1INCFG | =0xB0 at 0x16d6 (VALID, INTERRUPT) |
| 0xE612 | EP2CFG | =0xE8 at 0x16cc, VALID toggled at 0x16d2/0x02f4/0x1400/0x1419/0x142c |
| 0xE618..1B | EP2/4/6/8FIFOCFG | =0 at 0x115a..; EP2FIFOCFG=0x4C at 0x171d (per TRM layout: INFM2\|0\|AUTOOUT\|AUTOIN\|ZEROLENIN\|0\|WORDWIDE → 0x4C = INFM2 + AUTOIN + ZEROLENIN, WORDWIDE=0) |
| 0xE620/21 | EP2AUTOINLENH/L | cmd_1a0a (0x1a0a): =0x0200 (HS) or =0x0040 (FS) |
| 0xE65C | USBIE | `|= 0x3D` at 0x11c6 (SUDAV+SUTOK+SUSP+URES+HSGRANT) |
| 0xE65D | USBIRQ | write-1-ack in the USB ISRs (=1/2/4/8/0x10/0x20) and =0xFF at 0x1b5e |
| 0xE65F | EPIRQ | =0xFF (ack all) at 0x1b64 |
| 0xE668 | (unassigned on FX2) | `|= 0x09` at 0x11bf — clone-specific [guess: Grandtech engine setup] |
| 0xE670 | PORTACFG | `&= 0x3F` at 0x170a (PA6/PA7 = GPIO, not SLCS/FLAGD) |
| 0xE678/79/7A | I2CS/I2DAT/I2CTL | I2C engine (state machine under §7); I2CTL `|= 0x01` at 0x1173 = 400 kHz (I2CTL.0 = 400KHZ) |
| 0xE680 | USBCS | see §1.4 |
| 0xE681 | SUSPEND | see §1.4 |
| 0xE682 | WAKEUPCS | see §1.4 |
| 0xE683 | TOGCTL | endpoint data-toggle reset in CLEAR_FEATURE (0x09d6) |
| 0xE68A/8B | EP0BCH/L | EP0 arming |
| 0xE68F | EP1INBC | interrupt-EP arming (0x191b) |
| 0xE6A0/0xE6A2/0xE6A3 | EP0CS/EP1INCS/EP2CS | busy/stall/full polling |
| 0xE6B3/B4 | SUDPTRH/L | GET_DESCRIPTOR (descriptor pointer handed to the SIE) |
| 0xE6B8..BF | SETUPDAT | the 8-byte setup packet |
| 0xE740 | EP0BUF | 64-byte EP0 buffer |
| 0xE7C0 | EP1INBUF | the 1-byte interrupt payload (0x1915) |

SFRs: P0/PORTA = 0x80, DPS = 0x86 (data-ptr select, cleared in ISRs/delay code),
PCON = 0x87 (IDL bit 0 used for suspend), TCON = 0x88 (IT0/IT1 set, edge-triggered
INT0/INT1), P1/PORTB = 0x90 (**never touched** — it is FD[7:0] in FIFO mode),
EXIF = 0x91 (b4 = USBINT, b5 = I2CINT; cleared in the respective ISRs),
IE = 0xA8 (EA+EX0+EX1), OEA = 0xB2, P3/PORTD = 0xB0, OED = 0xB5,
EICON = 0xD8 (per TRM Table 4-5: init sets b5 = ERESI = enable the resume
interrupt, 0x11bc; the resume ISR clears b4 = RESI, the wakeup flag that "must be
cleared by software before exiting the ISR", 0x1f05),
EIE = 0xE8 (per TRM Table 4-6: b0 = EUSB = USB interrupt enable, b1 = EI²C = I²C
interrupt enable — exactly the two the firmware sets).

[proven] **Not a real GT-6816**: the actual GT-6816 (datasheet `GT-6816.pdf` in the
ScannerPlustek project) is a USB 1.1 full-speed part whose scanner register file
lives at 0xFFxx (GPIODR 0xFF95, LEDCR 0xFF10, SHRRR 0xFF16, …) — no 0xE6xx block at
all. Our device enumerates USB 2.0 **high speed** (512-byte bulk packets; the
firmware branches on USBCS.7 = HSM at 0x0831/0x19a2/0x1a0a), which a GT-6816 cannot
do. The NOTES.md name "GT-6816" for the F50's controller is therefore wrong; treat
it as "an FX2-compatible USB engine with a Grandtech scanner front-end".

[proven] **Not a genuine Cypress FX2 either** (clone or licensed+patched die):
1. The Cypress ROM-loader vendor request 0xA0 is not implemented (every address
   returns the same junk — device test, NOTES.md; a genuine FX2 ROM answers 0xA0
   reads/writes; FX2 TRM ch. 3).
2. Init writes 0xE668 (`|= 0x09`), an address with no FX2 assignment.
Everything else matches the FX2 TRM exactly, including the fine print:
CPUCS/IFCONFIG/EPxCFG (incl. the FX2-only EP2CFG.6 = DIR bit, used correctly —
see §1.3), FIFORESET/FIFOPINPOLAR, USBIE/USBIRQ/EPIE/EPIRQ, TOGCTL (incl. the
IO bit recipe), SUDPTR/SETUPDAT/EP0BUF, the INT2VEC autovector, EXIF/EICON/EIE,
USBCS/SUSPEND/WAKEUPCS, the I2C controller (I2CINT vector, I2CS
START/STOP/LASTRD/BERR/ACK/DONE choreography), and even the boot-EEPROM
addressing: the 8 KB part sits at 7-bit 0x51, exactly the FX2 loader's strap for
16-bit-addressed EEPROMs (slave `1010 001`; TRM ch. 13 Table 13-11 + ch. 3).
So: a faithful FX2-compatible core with at least one bolted-on Grandtech engine
register (0xE668) and a non-Cypress ROM.

## 1. Memory map and boot

### 1.1 Image format (C2) [proven, `fwinfo.py`]

8-byte header: magic 0xC2, VID 0x07b3, PID 0x0b00, DID 0x0000, config byte 0x04.
Then (length:2 BE, address:2 BE, data) records; the top bit of the length word marks
the final record:

```
rec  file 0x0008:    6 B -> 0x0000..0x0005   (reset + INT0 vectors)
rec  file 0x0012:    3 B -> 0x0013           (INT1 vector)
rec  file 0x0019:    3 B -> 0x0033           (resume? vector)
rec  file 0x0020:    3 B -> 0x0043           (INT2/USB vector)
rec  file 0x0027:    3 B -> 0x004b           (INT3/I2C vector)
rec  file 0x002e:    3 B -> 0x0053           (second USB vector)
rec  file 0x0035: 1023 B -> 0x0080..0x047e   (descriptors + dispatcher + opcode handlers)
... 1023-byte records through 0x187a..0x1c78, then 704 B -> 0x1c79..0x1f38
rec  file 0x1f0e:    1 B -> 0xE600           (FINAL: CPUCS = 0x00 = release 8051 reset)
```

The format has **no checksum**; the loader's structural requirements per TRM
Table 3-5: magic 0xC2 at byte 0, records with 10-bit lengths (0..1023) and 13-bit
addresses, loads only into RAM 0x0000-0x1FFF / data RAM 0xE000-0xE1FF / CPUCS
(only bit 0 EEPROM-loadable), and a final record = single byte 0x00 to CPUCS with
the top length bit set. Our image is fully conformant (all code records ≤ 1023 B
within 0x1FFF; final record `80 01 E6 00 00`). The VID/PID in the header are what
the device presents until firmware takes over (RENUM). Code 0x0006..0x007f not
covered by records is whatever the RAM held before (the vectors there are unused:
timers, UART — see table below).

### 1.2 Interrupt vectors [proven]

```
0x0000  ljmp 0x1447   reset (C51 startup)
0x0003  ljmp 0x13b8   INT0  = sensor VSYNC (edge, IT0=1)
0x0013  ljmp 0x17c7   INT1  = button (edge, IT1=1)
0x0033  ljmp 0x1f05   RESUME/wakeup interrupt (TRM: vector 0x0033, highest
                    priority; the ISR clears EICON.4 = RESI per the TRM's
                    "RESI must be cleared by software before exiting the ISR")
0x0043  ljmp 0x1300   USBINT, autovectored (table below)
0x004b  ljmp 0x1016   I²CINT = the I2C controller (TRM Table 4-1)
0x0053  ljmp 0x1300   IE4 (GPIF/FIFOs) — its enable (EIE.2) is never set;
                    shares the USB stub as a safety net
0x000b/0x1b/0x23/0x2b (TF0/TF1/RI+TI/TF2): not hooked — no timers, no UART use
```

USB autovector table at 0x1300 (FX2 INT2VEC: on an autovectored INT2 the SIE
substitutes INT2VEC for the contents of address 0x0045 = the low byte of the
`ljmp` at 0x43; the 4-byte table slots below) [proven table; FX2 source names
per TRM §4]:

```
0x1300  ljmp 0x1d8b   INT2VEC 0x00 SUDAV (EP0 setup)  -> setb 0x22.0,  USBIRQ=0x01
0x1304  ljmp 0x1dfe   INT2VEC 0x04 SOF                -> USBIRQ=0x02   (ack only)
0x1308  ljmp 0x1de8   INT2VEC 0x08 SUTOK              -> USBIRQ=0x04   (ack only)
0x130c  ljmp 0x1da3   INT2VEC 0x0c SUSPEND            -> setb 0x22.2,  USBIRQ=0x08
0x1310  ljmp 0x199c   INT2VEC 0x10 USBRESET           -> re-pick descriptors by HSM, USBIRQ=0x10
0x1314  ljmp 0x19d3   INT2VEC 0x14 HSGRANT (HS/FS switch) -> same re-pick, USBIRQ=0x20
0x1318  ljmp 0x1f15   (RETI; padding)
```

Every USB ISR also clears EXIF.4 (USBINT); the I2C ISR clears EXIF.5 (I2CINT).
The speed-change ISRs (0x199c/0x19d3) copy descriptor-pointer pairs in the register
banks: if USBCS.7 (HSM) set: (0x0e:0x0f)=(0x08:0x09), (0x10:0x11)=(0x12:0x13);
else the two pairs are swapped the other way. See 1.3 for what those point at.
The autovector table means the SOF/SUTOK ISRs are dead weight (USBIE = 0x3D does
not enable SOF), kept only as ack stubs.

### 1.3 Reset and main() [proven]

0x1447 (C51 startup): clear IDATA 0x00..0x7F, SP = 0x6E, walk the Keil static-init
table at code 0x1C1F (a few bit-variable clears + two low-XDATA cells; linker
boilerplate, nothing driver-relevant), enter main at 0x111E.

main() init order (0x111e..0x11e5):

1. `CPUCS = 0x10` (48 MHz; FX2 CPUCS CLKSPD = bits 4:3 = 10b)
2. flags: 0x22.2=1 (force one suspend pass), 0x21.7/0x22.1/0x22.0 = 0
3. `TCON.IT0 = 1` (0x1135) and later `TCON.IT1 = 1` (0x114a): INT0/INT1 are
   falling-edge-triggered
4. ports: `OEA &= ~0x01; P0.0 = 1` and `OEA &= ~0x08; P0.3 = 1` (PA0/PA3 inputs with
   the quasi-bidirectional pull-up); `OEA |= 0x80; P0.7 = 0` (PA7 push-pull output,
   driving 0)
5. `IFCONFIG = 0xC3` (internal 48 MHz IFCLK, slave-FIFO mode; TRM §9)
6. `EP2/4/6/8FIFOCFG = 0` (all AUTOIN/AUTOOUT off, 8-bit WORDWIDE=0)
7. `CPUCS = 0x12` (48 MHz + CLKOE: drives CLKOUT = the sensor's XCLK) [inferred]
8. `lcall 0x1ef5`: I2C state (0x1f) = 0, EIE.1 = 1 (I2C IRQ on), EA = 1
9. `I2CTL |= 0x01` → 400 kHz SCCB
10. `0x21.3 = 1; lcall 0x1ec5` → OED |= 0x07, PD0=PD1=PD2 = 1 (all three backlight
    channels ON at boot); `0x21.0 = 1; lcall 0x1e49` → OED |= 0x20, PD5 = 1
11. `lcall 0x1221` — sensor init over I2C (list below)
12. `lcall 0x16ba` — USB/endpoint init:
    - `REVCTL = 0x03`
    - `CPUCS = 0x12` again, `IFCONFIG = 0x43` — **external IFCLK pin**: from here the
      slave FIFO is clocked by the sensor's PCLK [proven]
    - `EP2CFG = 0xE8` then `EP2CFG &= 0x7F`: FX2 layout (TRM Appendix C / §8.6.2):
      VALID=1, DIR(bit6)=1 = **IN**, TYPE(bits 5:4)=10 = **BULK**, SIZE(bit3)=1 =
      **1024-byte buffers**, BUF(bits 1:0)=00 = **QUAD** (sanity check: the TRM's
      own examples use 0xA2 = VALID, OUT, BULK, 512, double); then immediately
      invalidated (VALID=0 — EP2 stays NAKed until the first armed VSYNC)
    - `EP1INCFG = 0xB0` (VALID, TYPE=11 = INTERRUPT)
    - `FIFORESET = 0x80, 0x02, 0x04, 0x06, 0x08, 0x00` (NAK-all; reset EP2/4/6/8
      FIFOs; release) — the canonical FX2 sequence
    - `TCON.IE0 = 1` (self-pend one INT0 pass), `IE.EX0 = 1`, `TCON.IE1 = 0`,
      `IE.EX1 = 1`
    - `PORTACFG &= 0x3F` (PA6/PA7 stay GPIO)
    - `FIFOPINPOLAR = 0x3C` (SLWR/SLRD/SLOE/PKTEND active-high)
    - `EP2FIFOCFG = 0x4C` = INFM2 | AUTOIN | ZEROLENIN, WORDWIDE=0 (TRM layout:
      b6=INFM2 = "In FULL flag minus 2" flag-pin timing option — the FLAG pins
      aren't mapped to PA6/PA7 here, so it's inert; b3 = AUTOIN = hardware
      packet-commit at EP2AUTOINLEN; b2 = ZEROLENIN = a zero-length packet is
      appended when the committed count is an exact packet multiple — **one
      frame = 5,038,848 B = 9834 x 512 exactly, so every frame is followed by a
      ZLP** [proven config; inferred on-the-wire visibility — libusb reads just
      see a short/0-length URB, byte alignment unaffected]). Note the FX2
      defaults WORDWIDE=1 at power-on (TRM §9) — the firmware actively selects
      the 8-bit FIFO (first the =0 write at 0x115a, then 0x4C here).
13. Descriptor-pointer preloads in the register banks (used by GET_DESCRIPTOR and
    swapped on every speed change by the USBRESET/HSGRANT ISRs):
    (0x0a:0x0b)=0x0090 device descriptor; (0x14:0x15)=0x00a2 device qualifier;
    (0x08:0x09)=0x00ac config HS (512B EP2); (0x12:0x13)=0x00ec config FS (64B EP2);
    (0x0c:0x0d)=0x00cc and (0x16:0x17)=0x010c (the two other-speed images; the
    OTHER_SPEED handler actually writes immediates, not these bank pairs);
    (0x18:0x19)=0x012c string list.
14. `EIE.0 = 1` (EUSB = USBINT on), `EICON |= 0x20` (ERESI = resume-interrupt
    enable, TRM Table 4-5), `0xE668 |= 0x09` (clone engine?),
    `USBIE |= 0x3D`, `IE.EA = 1`
15. USB reconnect/renum: if USBCS.1 (RENUM) set → `0x21.0 = 1; lcall 0x1b44`
    (DISCON pulse, see 1.4); then `USBCS &= ~0x08` (clear DISCON = connect),
    `CKCON (SFR 0x8E) &= ~0x07` (MOVX stretch = 0)

Sensor register writes at boot (0x1221; all to 7-bit 0x30 via helper 0x1eeb =
"write reg r5 = value r3", read helper 0x1dbb) [proven]:

```
0x09=0x02   0x0f=0x00   0x12=0x80 (COM7 soft reset)   0x09=0x02   0x11=0x43 (CLKRC: PLL/8)
window via 0x0cc2: H start/stop 0x90/0x5A0 (regs 0x32/0x17/0x18/0x03), V start/stop (regs 0x19/0x1a/0x03)
COM8 (0x13): fastAEC=0, AGC=0, AWB=0, AEC=1  -> COM8 = 0x41 (helpers 0x1cd2/0x1c8e/0x1cb0/0x1cf4)
0x24=0x78 (AEW)  0x25=0x68 (AEB)  0x15=0x02 (COM10)  read 0x32 (REG32) -> XDATA 0x2876 (saved copy)
0x2d=0x80  0x2e=0x01 (ADDVSL/H = +0x180 lines)  0x0c=0x08 (COM3)  0x46=0x60  0x47=0x00 (DMLNL/H)
```

matches the "F50 boot" column in `OV5620.md` (COM8=0x41, ADDVS=0x180, CLKRC=0x43…).

Main loop (0x11e7 → 0x121e forever) [proven]:

```
if (0x22.0) { cmd_07ba(); 0x22.0 = 0; }              // EP0 setup pending (SUDAV)
if (0x22.2) {                                        // USB SUSPEND IRQ seen
    0x1ed3();                                        // P0.7=1, PD0..2 shadows = 0, apply (lamp off)
    0x22.2 = 0;
    do { 0x1c46(); } while ((WAKEUPCS.7 && WAKEUPCS.1) || (WAKEUPCS.6 && WAKEUPCS.0));
    0x1b73();                                        // maybe SIGRSUME pulse
    0x1e5a();                                        // PA7=0 (LED on), PD0..2 shadows = 1, apply (lamp on)
}
0x18e4();                                            // housekeeping: EP1 byte + EP2 overflow flag
```

### 1.4 Every write to 0xE680 (USBCS) and 0xE681 (SUSPEND) [proven]

0xE680 = **USBCS** (FX2: b0 SIGRSUME, b1 RENUM, b2 NOSYNSOF, b3 DISCON, b7 HSM):

| site | write | meaning |
|---|---|---|
| 0x11db (init) | `&= ~0x08` | clear DISCON — connect to USB after init |
| 0x1b47 (0x1b44, if flag 0x21.0) | `|= 0x0A` | DISCON+RENUM: disconnect and re-enumerate under firmware control |
| 0x1b50 (0x1b44, else) | `|= 0x08` | DISCON: plain reconnect pulse |
| 0x1b6b (0x1b44 tail) | `&= ~0x08` | end of pulse (after a clock-scaled delay 0x1858(0x05DC) and USBIRQ/EPIRQ = 0xFF/0xFF) |
| 0x1b89 (0x1b73) | `|= 0x01` | SIGRSUME: remote-wakeup signaling to the host… |
| 0x1b97 (0x1b73) | `&= ~0x01` | …released after delay 0x1858(0x0014) (≈ ms-scale) |

0x1b44 runs when USBCS.1 (RENUM) was seen at init — i.e. once after a C2 EEPROM
boot [inferred] — and is the standard FX2 "disconnect, let the host re-enumerate
with the firmware owning EP0" dance. Reads of 0xE680: bit7 (HSM) selects the
OTHER_SPEED descriptor (0x0831), the descriptor-pointer swap in the USBRESET/HSGRANT
ISRs (0x19a2/0x19d9), and the EP2 AUTOIN length in SET_CONFIGURATION (cmd_1a0a).

0xE681 = **SUSPEND**: written exactly once per suspend entry, at 0x1c4d (function
0x1c46): `a = WAKEUPCS | 0xC0; WAKEUPCS = a; SUSPEND = a; PCON.0 = 1`. Per TRM
§6.2.1, **writing any value to SUSPEND lets the FX2 enter the suspended state**
(the value itself is irrelevant); the OR-0xC0 into WAKEUPCS first clears the
WU/WU2 pending flags (write-1-to-clear per TRM ch. 6), and PCON.0 = IDL then
stops the 8051 clock until any enabled interrupt. WAKEUPCS layout (TRM):
b7 = WU2 pending, b6 = WU pending, b5/b4 = WU2POL/WUPOL, b2 = DPEN (D+-activity
wakeup enable), b1 = WU2EN, b0 = WUEN — power-on defaults have DPEN = WUEN = 1.
The main loop afterwards idles while (WAKEUPCS.7 && WAKEUPCS.1) ||
(WAKEUPCS.6 && WAKEUPCS.0) — "an enabled wakeup pin is still asserting" —
and 0x1b73 pulses SIGRSUME when a wakeup is pending and enabled [mechanics
proven; which physical sources are wired on this board (pins vs. D+ activity)
inferred]. Resume wakes via the 0x33-vector resume interrupt (ERESI is enabled
at init).

## 2. The mailbox protocol and the opcode reference

### 2.1 EP0 plumbing [proven]

Standard requests (bRequest table at 0x07c6, walked by 0x0c6f on SETUPDAT[1]):

```
bRequest: 00 GET_STATUS -> 0x08a8 | 01 CLEAR_FEATURE -> 0x0953 | 03 SET_FEATURE -> 0x09ea
06 GET_DESCRIPTOR -> 0x07e2 | 08 GET_CONFIGURATION -> 0x08a2 | 09 SET_CONFIGURATION -> 0x089c
0a GET_INTERFACE -> 0x0890 | 0b SET_INTERFACE -> 0x0896 | default -> 0x0a5b = the VENDOR handler
```

- GET_STATUS: device -> EP0BUF = byte0 (bit0 = self-powered = flag 0x22.1,
  bit1 = remote-wakeup = flag 0x21.7; both 0 in practice: bus-powered, no remote
  wakeup), byte1 = 0; interface -> {0,0}; endpoint ->
  `{XDATA(0xE6A1 + 2*T[(wIndexL & 0x7E) | dirIN]) bit0, 0}` with T at code 0x1EE1 =
  `{0,1,2,2,3,3,4,4,5,5}` — against the FX2 EPxCS addresses this returns the wrong
  endpoint's stall bit for several endpoints [proven mechanics; inferred latent
  firmware bug, harmless: hosts don't query it here].
- CLEAR_FEATURE: device: wValue 1 = clear remote-wakeup flag 0x21.7; wValue 2 = ack
  no-op; endpoint: wValue 0 (ENDPOINT_HALT): `EPxCS &= ~1` (un-halt) + TOGCTL
  toggle reset (write `ep | (IN ? 0x10 : 0)` to 0xE683, read back, `|= 0x20` (R),
  write) — exactly the FX2 recipe (TRM).
- SET_FEATURE: device: wValue 1 = set 0x21.7, wValue 2 = ack no-op; endpoint: halt
  (`EPxCS |= 1`).
- GET_DESCRIPTOR: second table on wValueH: type 1 (device) → SUDPTR=(0x0a:0x0b);
  2 (config) → (0x0e:0x0f) (speed-selected); 3 (string) → walk the list at 0x012c
  by wValueL (0x1b9f; entries [len][type==3]); 6 (qualifier) → (0x14:0x15);
  7 (other-speed) → 0x010c if HSM else 0x00cc; not found → EP0CS |= 1 (STALL).
- SET_CONFIGURATION (0x089c → cmd_1a0a): stores wValueL to INTMEM 0x60; sets
  **EP2AUTOINLEN = 0x0200 if USBCS.7 (HSM) else 0x0040** — the 512/64-byte auto-commit.
- GET/SET_INTERFACE: INTMEM 0x5f get/set (only interface 0 exists).
- Every handled request exits through 0x0a67: `EP0CS |= 0x80` (HSNAK = ack the
  status stage). Unhandled/invalid → `EP0CS |= 1` (STALL).

Descriptors (code addresses; raw bytes verified): device 0x0090 (`12 01 00 02
00 00 00 40 b3 07 00 0b 00 00 01 02 00 01` — USB 2.0, VID 07b3, PID 0b00,
iManufacturer=1, iProduct=2, no serial, 1 config); qualifier 0x00a2; config
0x00ac (HS): `09 02 20 00 01 01 00 80 32` (1 interface, bus-powered, 100 mA) +
`09 04 00 00 02 ff ff ff 00` (2 endpoints, class FF/FF/FF) + `07 05 82 02 00 02
00` (EP 0x82 bulk, 512 B) + `07 05 81 03 01 00 01` (EP 0x81 interrupt, 1 B,
1 ms); other-speed 0x00cc (same, EP2 512 B); config 0x00ec (FS: same but EP2
maxpacket 64); other-speed 0x010c (FS image, 64 B); strings 0x012c: LANGID 0409,
then "Film Scanner" twice (0x0130 and 0x014a — iManufacturer and iProduct both
resolve to "Film Scanner").

Vendor handler cmd_0df0 (0x0df0), reached for any non-standard bRequest; only
bRequest = 0x04 passes [proven]:

| dir | bmRequestType | wValue | wIndex | effect |
|---|---|---|---|---|
| OUT (0x40) | required bits 7=0,6=1 | must be 0x0011 | must be 0x0001 | ack OUT, copy 64 B EP0BUF → XDATA 0x2800, save block[0]/[1] in 0x5d/0x5e |
| IN (0xC0) | required bits 7=1,6=1 | must be 0x0010 | must be 0x0000 | run the command interpreter on the mailbox, copy XDATA 0x2800..0x283f → EP0BUF, arm 64-byte IN |

Anything else: return carry=1 → the caller (0x0a5b path) STALLs EP0.
After a reply whose first two bytes differ from the saved request's, the handler
**toggles P0.7** (0x0e8d..0x0e97): the visible per-command activity LED [proven].

The interpreter cmd_0166 (0x0166): writes **reply[2]=0x34, reply[3]=0x12** (the
0x1234 tag, little-endian) unconditionally, then searches the opcode table at
0x019a for `entry[2]==block[1] && entry[3]==block[0]` — so **byte 1 of every
command must be 0**. Table entries are 4 bytes `{handler_hi, handler_lo,
required_block1, opcode}`; terminator `{00,00,07,b5}`.

**Reply echo**: block[0]/block[1] are returned *unchanged* (no increment anywhere —
findings doc 01's "reply echo = opcode+1/+2" claim was a Ghidra desync artifact;
the phantom `cjne a, 0xab` at 0x0201 is the byte-displaced `mov r3, 0x2f` of the
version handler). [proven: disassembly + live replies in NOTES.md]

**Unhandled opcodes** (including the missing 0x18 and any byte1 != 0): the
terminator runs 0x07b5 = clear the "handled" flag; the EP0-IN branch then stages
no reply and the control-IN **stalls/NAKs until the host times out** [proven].
(The half-claim in doc 01 that unknown opcodes return the version block is wrong;
its other half — that the DLL's 0x0128 ping relies on the stall and ignores the
failure — stands.)

### 2.2 Opcode table [proven from table bytes + handler disassembly]

Common: request `[0]=opcode, [1]=0`; reply `[2..3]=0x34 0x12` (status 0x1234) or
`0x78 0x56` (status **0x5678** = argument/validation failure in opcodes
0x13..0x16 only — see below; note the echo bytes [0..1] are the request's).

| op | handler | request layout | reply layout | side effects |
|---|---|---|---|---|
| 00 | 0x0202 | — | [4..7] = 00 06 01 08 | firmware version 0.6.1.8 |
| 01 | 0x023e | — | [4]=1 [5]=2 [6]=4 | protocol version 1.2.4 |
| 02 | 0x026d | any | — | pure ack (parameter bytes just sit in the mailbox; the DLL deposits window words with opcodes 2/3 that the firmware never reads) |
| 03 | 0x0272 | any | — | pure ack (same) |
| 04 | 0x0277 | — | — | pure ack |
| 05 | 0x027c | — | — | **ARM STREAM**: INTMEM_0x20 |= 0x10 (bit4). On the next VSYNC the ISR resets the EP2 FIFO and validates EP2 (§3) |
| 06 | 0x02b3 | — | — | pure ack |
| 07 | 0x02b8 | — | [4] bit0 = INTMEM_0x20 bit3 (overflow/streaming flag) | **STOP**: clears flag bits 3+4; if mode 0x6c != 0x0b: `EP2CFG &= 0x7F` (invalidate EP2 → host sees the pipe die) |
| 08 | 0x0300 | — | — | pure ack (the DLL's ping) |
| 09 | 0x0305 | [4] = sub-command 1..5, [5] = 0/1 | — | **GPIO/backlight control** (§4). NOT "start stream" (doc 01 was wrong): streaming is opcode 5 + VSYNC |
| 0a | 0x06c1 | [4]=1 → set, [5]=value; [4]=2 → get | [5] = value | mode variable INTMEM 0x6d (no other firmware consumer — session marker; Windows sets 1 at open) |
| 0b | 0x0474 | [4..5] = count **LE**, then count × {reg, value} at [6..] (max 29 pairs) | echo count at [4..5] | **sensor block write**: for each pair, I2C write reg=value to device 0x30 |
| 0c | 0x0505 | [4..5] = count LE, then count × {reg, 0} at [6..] | value i at [7+2i]; echo count at [4..5] | **sensor block read**: per entry, I2C write reg then read 1 byte (device 0x30) |
| 0d | 0x0288 | — | [4] bit0 = INTMEM_0x20 bit2 | read+clear bit2 (mode-0x0b "VSYNC seen" flag; always 0 in the normal mode) |
| 0e | 0x0413 | — | [4] = INTMEM_0x20 bit0 | button flag: 1 = not pressed, 0 = pressed (updated on INT1 edges only — see §4) |
| 0f | 0x0429 | — | [4]=2 [5]=2 [6]=0x82 [7]=3 [8]=0x81 | endpoint info: count=2, then {addr, USB-type}: 0x82 bulk(2), 0x81 interrupt(3) |
| 10 | 0x07b7 | — | (none) | tabled but leaves "handled" clear → the control-IN **stalls** (same for 0x11, 0x12) |
| 11 | 0x07b7 | — | (none) | stalls |
| 12 | 0x07b7 | — | (none) | stalls |
| 13 | 0x059a | [4]=addr LO, [5]=addr HI (LE16), [6]=7-bit I2C device, [7]=count N | N data bytes at [8..8+N) | **generic 16-bit-addressed I2C read** (per byte: write 2 address bytes, read 1; the boot EEPROM at 0x51 included) |
| 14 | 0x05fc | same + N data bytes at [8..8+N) | — | **generic 16-bit-addressed I2C write** (per-byte address resend + EEPROM ACK-poll via 0x1964). **Can overwrite the boot EEPROM — never send** |
| 15 | 0x065e | [4..5] = address LE (aligned **down** to 32), [6]=device, (count fixed 1 page) | 32 bytes at [8..39] | **EEPROM page read** (write 2 address bytes, read 32) |
| 16 | 0x06f6 | [4..5] = address LE (aligned down to 32), [6]=device, [7]=page count N, data at [8..] | — | **EEPROM page write**: stages N*32 bytes at XDATA 0x2842 and writes [addr_hi, addr_lo, data…] as ONE I2C transaction + ACK-poll. N>1 both overruns the 64-byte mailbox and wraps the EEPROM's 32-byte pages — treat as N=1 only. **Never send** |
| 17 | 0x0758 | — | [4] bit0 = INTMEM_0x20 bit3 | read+clear the overflow flag (opcode 7 also returns it but clears bits 3+4 and kills EP2) |
| 18 | — | — | — | **not in the table** → unhandled → control-IN stalls |
| 19 | 0x0781 | [4]=1 → set, [5]=value; [4]=2 → get | [5] = value | mode variable INTMEM 0x6c (the 0x0b special case, §3) |

The 0x5678 status: in opcodes 0x13..0x16 the inner helper returns carry=0 only for
null arguments (count = 0; the data pointer is never null in practice), so 0x5678
means "bad arguments". **An I2C NACK does not produce 0x5678** — the transaction
wait (0x1d53/0x1d6f) returns carry=1 on every exit path (including the bus-error
and NACK-abort states 6/7) [proven]. This matches the EEPROM dumper's note that
0x1234 comes back even when the read NAKs.

The count in 0x0b/0x0c is 16-bit little-endian at [4..5] (the two `lcall 0x0c0a`
fetches return (a=block[5], B=block[4]); verified against f50.py's working
`struct.pack("<H", count)`).

## 3. The data path: sensor frames → EP2

Hardware path [proven from the registers; sensor-side wiring inferred]:
the OV5620's parallel pixel bus + PCLK feed the controller's slave-FIFO interface
(`IFCONFIG = 0x43`: IFCFG=11 slave FIFO, IFCLKSRC=0 external IFCLK = PCLK;
FIFOPINPOLAR = 0x3C active-high WR/RD/OE/PKTEND). FD[7:0] are the PORTB pins
(never used as GPIO — SFR 0x90 appears nowhere). EP2 = bulk IN, 1024-byte
quad buffers (`EP2CFG = 0xE8`), AUTOIN commit at 512 B (HS) / 64 B (FS)
(`EP2AUTOINLEN`, set on SET_CONFIGURATION by USBCS.7), ZEROLENIN on → **ZLP after
each frame** (frame size is an exact 512 multiple).

Stream control [proven]:

- **Start**: opcode 5 sets INTMEM_0x20 bit4 ("armed"). The INT0/VSYNC ISR
  (0x13b8) on the next frame boundary, when armed:
  - normal mode (0x6c != 0x0b): if EP2 not yet valid: `FIFORESET = 0x02` (reset
    EP2's FIFO; note: no NAK-all and no release write — a bare edge-triggered
    reset), then `EP2CFG |= 0x80` (VALID). Clears the arm bit.
  - mode 0x0b: full `FIFORESET = 0x80/0x02/0x00` sequence **on every VSYNC**, sets
    flag bit2, and if armed: `EP2CFG |= 0x80`, clears the arm bit.
  So the first bytes after opcode 5 always start at the top of a frame [proven;
  live-verified per NOTES.md].
- **Stop**: opcode 7: clears flags 3+4 and (mode != 0x0b) `EP2CFG &= 0x7F` — EP2
  vanishes (host reads fail: xHCI -EPROTO [live-proven]). In mode 0x0b EP2 stays
  valid across opcode 7 and the FIFO is re-reset at every frame → a free-running
  frame-aligned continuous stream [proven code path; the mode is unused by Windows
  and untested live].
- **Overflow flag**: while armed, the housekeeping function (0x18e4, every
  main-loop pass) sets flag bit3 when `(EP2CS & 0x48) == 0x48` — per TRM
  §8.6.2.3, EP2CS bit6 = NPAK2 and bit3 = FULL (NPAK = number of packets
  committed in the FIFO, 0..4 for quad buffering), so the condition is
  "NPAK >= 4 and FULL" = **all four EP2 buffers occupied** = the host didn't
  drain fast enough / a read gap lost data [proven condition + TRM bit names].
  Returned by opcode 7 and opcode 0x17 reply[4] bit0; both clear it.
- Flag byte INTMEM 0x20 (also the EP1 payload, §4): b0 = button-not-pressed,
  b1 = button-pressed, b2 = mode-0x0b VSYNC seen (opcode 0x0d reads/clears),
  b3 = overflow (7/0x17), b4 = armed (5 sets; 7 and the VSYNC ISR clear).
- The VSYNC ISR also clears INTMEM 0x23/0x24/0x25 (unused elsewhere — leftover
  counters) and self-masks EX0 while running. [proven]
- **Throughput limit**: not USB (HS bulk can do >40 MB/s): the FIFO is clocked by
  the sensor's PCLK, so the sensor's CLKRC sets the stream rate (live: 0x43 ≈
  7.1 MB/s, 0x41 ≈ 14 MB/s, 0x40 drops bytes — NOTES.md) [proven live]. A host-side
  read gap longer than the FIFO (4 KiB ≈ 0.3 ms at 14 MB/s) loses bytes silently
  (Bayer phase flip at read boundaries; the overflow flag may not even catch it —
  it only fires while the arm bit is set) [inferred].

## 4. GPIO

| pin | direction at boot | role | driven from |
|---|---|---|---|
| PA0 (P0.0) | input, pull-up | unknown [guess: strap/sensor status] | — |
| PA3 (P0.3) | input, pull-up | **scan button** (also the INT1 source [inferred]; read in the INT1 ISR) | — |
| PA7 (P0.7) | push-pull output, =0 | **activity LED**: toggled by the EP0 vendor handler whenever a reply differs from the request; forced 1 during suspend and 0 on resume | cmd_0e00; 0x1ed3/0x1e5a |
| PD0 (P3.0) | output | **backlight channel 1** | op9 sub1 / shadows 0x21.3 (helpers 0x1e9b/0x1dd2/0x1ec5) |
| PD1 (P3.1) | output | **backlight channel 2** | op9 sub2 / shadow 0x21.4 (0x1ea9) |
| PD2 (P3.2) | output | **backlight channel 3** | op9 sub3 / shadow 0x21.5 (0x1eb7) |
| PD4 (P3.4) | — | unused (helper 0x1e38 exists, flag 0x22.4 never set — dead code) | — |
| PD5 (P3.5) | output, =1 | set once at boot via 0x1e49; never touched again [guess: engine/sensor enable or reset] | — |
| PORTB (P1) | — | FD[7:0] FIFO data in slave-FIFO mode; never GPIO | — |

So **the backlight has three independently switchable elements** [proven]:
opcode 9 sub-commands 1/2/3 with [5]=0/1 switch PD0/PD1/PD2 individually (via
scratch flag 0x21.6); sub 4 sets persistent shadows 0x21.3/4/5 = [5] and applies
all three (this is what Windows uses: 9(4,1) = lamp on, 9(4,0) = off, live-proven
in NOTES.md); sub 5 applies shadow 0x21.3 to all three. [inferred: R/G/B LED
strings — NOTES open question 6; could equally be parallel white strings.]
Suspend turns all three off and restores them on resume via the shadows
(0x1ed3/0x1e5a).

The button (PA3) is polled by interrupt: INT1 fires on the falling edge (press);
the ISR sets INTMEM_0x20 bit1=1, bit0=0. No edge on release → the flags are
stale until the next press [proven]. The host-visible button event is the EP1
byte (§5), i.e. this is edge-signaling, not level-signaling.

## 5. Interrupt endpoint 0x81

[proven] One byte per event, sent by the housekeeping function 0x18e4 when:
not streaming (flag bit4 = 0), EP1IN not busy (EP1INCS.1 = 0), and any of
INTMEM_0x20 bits 0..2 set. The byte written to EP1INBUF (0xE7C0) **is the raw
INTMEM_0x20 flag byte**; EP1INBC = 1 arms it; bits 0..2 are then cleared.

So the host reads: 0x02 = button pressed (falling edge seen), 0x01 = button
"not pressed" (only if a release edge were ever caught — with edge-triggered INT1
it practically isn't), 0x04 = mode-0x0b frame-sync flag [proven mechanics;
inferred host-visible pattern]. The Windows stack exposes this through
WDUScan's GetEventValue (driver docs 01/05). Note: EP1 sends are suppressed for
the whole armed window (opcode 5 → opcode 7).

## 6. Running the FIFO 16-bit (WORDWIDE) — exact patch

Goal: move the sensor's 10 bits (Y9..Y0) instead of 8 (Y9..Y2) — on the FX2,
WORDWIDE=1 puts FD[15:8] on the PORTD pins (TRM ch. 9, Figures 9-4/9-5).

**Firmware patch — one byte in the C2 image** [proven]:
`EP2FIFOCFG = 0x4C` is written by the instruction at code 0x171D..0x1722
(`90 E6 18 74 4C F0` = mov dptr,#0xE618; mov a,#0x4C; movx @dptr,a), inside the
record covering code 0x147B..0x1879 (record header at file 0x1444, data from file
0x1448). The immediate byte is at **file offset 0x16EE**: patch `4C → 4D` (set
bit0 = WORDWIDE). The other EP2FIFOCFG write (0x115A, value 0 from `clr a`)
needs no change (bit0 = 0 there is fine; the later write wins).

**Keeping the C2 image valid** [proven format]: a 1-byte data substitution changes
nothing structural — record lengths, addresses and ordering are untouched; the C2
format has no checksum (fwinfo.py + FX2 TRM ch. 3); keep the 0xC2 magic and the
VID/PID (07b3/0b00) in the header and keep the final record (`80 01 E6 00 00` =
length 0x8001 (final-flag set), 1 byte to CPUCS = 0x00) last. Since the boot
EEPROM is byte-identical to FWofWin.bin, the same single-byte patch at 0x16EE is
the EEPROM patch; the part is 8 KB and the image's tail is 0xFF (roomy).

**Other 8-bit assumptions to audit** [proven unless noted]:
- `EP2AUTOINLEN` (512/64) is a *byte* count and stays valid (even); no other
  firmware constant depends on the FIFO width (EP2FIFOPF untouched, no
  manual-commit code anywhere — AUTOIN does everything).
- **PORTD conflict — the real cost**: with WORDWIDE=1, PORTD is automatically
  configured as FD[15:8] (TRM §10.2.7; Figures 9-4/9-5), so PD0/PD1/PD2 (the
  three backlight channels) and PD5 stop being GPIO: the backlight can't be
  switched and its pins will toggle with pixel data [proven pin-sharing per
  TRM; inferred visible flicker/lamp-state loss].
- **Physical pins for a board check** (CY7C68013 datasheet pin tables;
  FD[9:8] are the two new lines the 10-bit sensor's Y1/Y0 would have to reach;
  FD[7:0] = PORTB pins carry Y9..Y2 today):

  | signal | 128 TQFP | 100 TQFP | 56 SSOP | 56 QFN |
  |---|---|---|---|---|
  | PD0 / FD[8]  | 102 | 80 | 52 | 45 |
  | PD1 / FD[9]  | 103 | 81 | 53 | 46 |
  | PD2 / FD[10] | 104 | 82 | 54 | 47 |
  | PD3 / FD[11] | 105 | 83 | 55 | 48 |
  | PD4 / FD[12] | 121 | 95 | 56 | 49 |
  | PD5 / FD[13] | 122 | 96 | 1  | 50 |
  | PD6 / FD[14] | 123 | 97 | 2  | 51 |
  | PD7 / FD[15] | 124 | 98 | 3  | 52 |
  | PB0 / FD[0]  | 44  | 34 | 25 | 18 |
  | PB1 / FD[1]  | 45  | 35 | 26 | 19 |
  | …PB7 / FD[7] | 51  | 41 | 32 | 25 |

  All four packages expose FD[8..15] (the "16/8 bit" column in the datasheet's
  ordering table refers to the 8051's external address/data bus, not the FIFO).
  [proven pin numbers from the datasheet] — the owner can continuity-meter from
  the OV5620's D0/D1 (Y0/Y1) pads to the controller's PD0/PD1 pins to learn
  whether the low two sensor bits are even routed. If the engine/front-end only
  drives 8 lines, the upper FIFO byte will be constant/floating garbage and the
  patch buys nothing — check the high bytes of a capture before committing an
  EEPROM [guess about the front-end wiring].
- Host side: the frame doubles to 2592×1944×2 = 10,077,696 B (still an exact
  512 multiple → ZLP framing still works); f50.py's frame size/read loop and the
  demosaicer assume 1 B/px [proven, host code].
- Suspend/EP1/opcode layers carry no pixel data — unaffected.

## 7. Hang analysis (the 0x3e incident) and recovery

All I2C transactions funnel through two wait loops with **no timeout**:
0x1d6f (write) / 0x1d53 (read) spin on the ISR-driven state variable INTMEM 0x1f
until it becomes 0 (done), 6 (bus error) or 7 (NACK abort) [proven]. Before even
starting, 0x1b12/0x1aab spin on "I2CS.6 (STOP) clear" and bail (carry=0) only if a
transaction is already in progress; there is no bus-stuck timeout either. The
EEPROM ACK-poll (0x1964) loops until the slave ACKs, again with no bound
[proven]. There is **no watchdog**: the only SFR that could be one (0x86) is DPS
(data-pointer select), cleared routinely; no WDT refresh exists anywhere
[proven].

So if the OV5620 wedges the SCCB bus mid-transaction (e.g. after the write to
register 0x3e that hung the owner's unit: the sensor stops ACKing / holds a line,
and the clone's I2C controller never raises another interrupt), the 8051 spins
forever inside the mailbox command with interrupts still enabled: VSYNC/button/
USB ISRs keep firing, but the main loop never reaches cmd_07ba again, so **EP0
dies** — exactly the observed "device descriptor read/64, error -110" [inferred
mechanism; the observed symptoms match point-for-point, proven code]. A USB bus
reset (`dev.reset()`) only raises the USBRESET ISR; it cannot break the main-loop
spin, so it does not help — **only a power cycle/replug recovers the chip**
[proven by elimination; live-observed].

Secondary notes: the EP0BUSY waits (`do {} while (EP0CS.1)`) are hardware-bounded
(EP0BUSY always clears) — safe; the suspend idle loop wakes on any enabled
interrupt — safe; the delay routines are counting loops — bounded. The only
unbounded spins are the I2C ones above. Defensive option for a future patched
image: add a cycle-count cap to 0x1d53/0x1d6f and map it to the 0x5678 status
[guess — not required for the driver].

## 8. Lineage note: ScannerPlustek (ronenkr/ScannerPlustek) comparison

That project is a *real* GT-6816 (Plustek MobileOffice S400, USB 1.1, SANE
gt68xx). Protocol family is completely different: bRequest 0x01 with
wValue = raw memory offset (memory-mapped control, e.g. 0x200B = write 64 B
program RAM, 0x2010/0x3F40 = the 64-byte "normal command" block), single-byte
SANE-style command codes (0x61..0x70, 0x14..0x17, …), no opcode jump table, and
**no 0x1234/0x5678 status words** (the only "1234/5678" hits in its disassemblies
are code addresses). Its bootstrap part is a 24LC16B whose contents the project
had not verified, and its scan firmware is host-downloaded over vendor RAM-write
requests, whereas ours boots a C2 image from an 8 KB EEPROM. Shared idioms with
the F50 firmware: (a) a
64-byte command block in XDATA as the universal command envelope, (b) split
"deposit command (OUT)" / "run + fetch reply (IN)" control transfers,
(c) Plustek's habit of pure-ack opcodes used as pings. Everything else
(dispatcher, status words, endpoint map, register addresses) is specific to this
FX2-style firmware — do not import its register map. [proven from its docs +
disassemblies]

## 9. Corrections to findings docs 01/05 (superseded by this file)

- Reply bytes [0..1] are echoed **unchanged**; there is no +1/+2 increment and no
  SFR-0xAB comparison (Ghidra desync artifact).
- Unknown opcodes (incl. 0x18) and tabled 0x10..0x12 **stall the control-IN**;
  they do not return the version block.
- Opcode 9 is the **GPIO/backlight** sub-command dispatcher, not "START_STREAM";
  streaming is armed by opcode 5 and started by the VSYNC ISR.
- Opcodes: 0x0d = read/clear flag bit2 (not "SET_FIFO"); 0x13/0x14 = generic
  16-bit-addressed I2C read/write (not "SET_FIFO_PARAM"/"GPIO/LED"); 0x15/0x16 =
  EEPROM 32-byte page read/write (0x15 is not "WRITE_XDATA_2846"; the XDATA 0x2840
  staging is internal); 0x17 = read/clear overflow flag (not "I2C_WRITE_PAIR").
- EP2CFG = 0xE8 decodes (FX2 layout: TYPE at bits 5:4, SIZE bit3, BUF bits 1:0)
  to **bulk / 1024-byte / quad**, consistent with the descriptor — no
  type-field anomaly.
- EP1's IN payload buffer is 0xE7C0 on this map (matches the working button
  events), not 0xE800.
