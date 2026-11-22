# HP 3457A main-controller emulator

Pure-Python (stdlib) emulation of the 03457-66511 main controller running the real ROM
`03457-88810`. Use it to test hypotheses and patched images before touching a meter.
**It can't validate anything on the analog side**: the 8051 is a behavioural stand-in.

```bash
python3 tools/emu3457.py --seconds 8 --profile          # boot, show AUX_ERR and hot Forth words
python3 tools/forthcall.py "U*" 1000 1000               # run one ROM word on the bare CPU
python3 -m unittest discover -s tests                   # 40 tests, ~30 s
```

```python
from emu3457 import Board
from display3457 import attach
b = Board(nvram="build/nvram.bin")       # optional battery-RAM image (loaded/saved)
dec, lcd = attach(b)                     # display-bus decoder + LCD model
b.power_on(); b.run(seconds=7)
b.hpib_write("TRIG HOLD\r\n"); b.hpib_read()
print(b.hpib_query("ID?\r\n"), lcd.text())   # b'HP3457A\r\n'  '+0.00000  VDC'
b.press_key(2, 5)                        # keyboard matrix row 0-5, column 0-7
```

Speed: roughly real time (about 1.9 MHz-equivalent on a desktop PC; the real E clock is 2 MHz).

## Layers

| Layer | File | Contents |
|---|---|---|
| 1 | `tools/emu6809.py` | MC6809 core: all documented opcodes, datasheet cycle counts, NMI/FIRQ/IRQ/SWI/CWAI/SYNC, breakpoints, trace hook |
| 1 | `tools/forthcall.py` | Runs one Forth word with a prepared data stack (RAM thread `[xt][TRAP]`) |
| 2 | `tools/emu3457.py` | Board: memory map, I/O devices, timers, watchdog, cal lock, 8051 stand-in, profiler |
| 3 | `tools/emu3457.py` (`hpib_*`, `press_key`), `tools/display3457.py` | HP-IB bus-controller API (8291A model), display-bus decoder, keyboard |

## Hardware model (sources: schematic extracts + firmware)

| Item | Model | Confidence |
|---|---|---|
| I/O decode | A0-A2 ignored, so each register is mirrored over 8 bytes (`STD $003C` writes OUT2 twice) | schematic |
| UART HD6402 (U616) | `$00` RBR, `$08` TBR, `$10` status (b7 DR, b6 TBRE, b5 OE, b4 FE, b3 PE), `$18` DRR; 117 E-cycles per byte (11 bits at 187.5 kbit/s) | schematic + firmware |
| 8291A (U614) | `$20-$27`, INT → IRQ; holdoff, BI/BO/END/DEC/GET, ADSC/REM, BOUT held until talk-addressed | schematic + firmware; register bit layout from the 8291A datasheet as recalled **(verify against the datasheet)** |
| FIRQ | external trigger; ack by writing `$28` (CXTRIG) | firmware (C9BA) |
| NMI | every 65,536 E-cycles (32.768 ms), gated by OUT1 bit 0 (MASK\*) | 66501 schematic (U514/U515), assumed the same on 66511 **(verify)** |
| Watchdog | reset after 2^20 E-cycles (524 ms) without a **rising edge** of OUT2 bit 7 (OTL\*) | 66501 schematic + firmware behaviour **(edge sensitivity inferred)** |
| Cal-RAM lock | protected `$0040-$01FF`; a hardware reset arms it; the first CALREQ (`$30`) access opens NVE; the next OTL rising edge (or 4.096 ms) closes it | **inferred** (see below) |
| IN1 `$38` | b0-5 keyboard rows (active low), b6 JM621 (KEY position follows column O6 = OUT1 b0), b7 line frequency square wave | schematic |
| OUT1 `$3A` | keyboard columns O6,O1,O4,O8,O5,O3,O2,O7 (active low); b0 also MASK\* | schematic |
| OUT2 `$3C` | b0 PWO, b1 Φ1, b2 Φ2, b3 INA, b4 SYNC, b5 ISA, b6 VMCMPLT, b7 OTL\* | schematic |

Why the cal-lock model looks like this: after its protected writes, the firmware kicks OTL and
then immediately checks that `$01FD` is **no longer** writable, raising AUXERR 16384 otherwise.
So the real latch must close within microseconds of that kick (or of the OUT1 restore). A pure
4 ms window would fail the firmware's own check. The protected range `$0040-$01FF` is a guess
from the RAM-enable description ("A13-A15 low and any of A6-A12 high"); goal 3 will refine it.

## The 8051 stand-in (modelling shortcuts, read before trusting results)

The A/D controller's ROM is unknown. `Inguard8051` only produces what the main controller is
seen to expect:

- **Reset** when more than 200 back-to-back `00` bytes arrive (IG_RESET's 16,384 zeros hold RXD
  low). After release it sends `FF` (self-test OK; `selftest_ok=False` sends `02`), then echoes
  bytes until it receives `00`, which it ACKs with `0F`.
- **Replies are keyed to the main's link state `STAT` (`$03D0`)** once a frame has gone quiet
  (3 byte-times): 2 → ACK `0F`, 3 → peek data, 4/5 → `10`, 6 → two bytes, 7 → `option`,
  8 → channel. This is a shortcut: a real 8051 decodes opcodes. It hides any framing error the
  real part would catch.
- **Readings**: while STAT = 1, it sends `10` ("conversion complete") every `conv_cycles`
  (20 ms default, not NPLC-accurate). On `7F` it returns `counts` as 2 bytes (AD_MODE < 2) or
  4 bytes, big-endian; in FREQ/PER (FUNCTION ≥ 20) 6 bytes (0.1 s gate, `counts` cycles).
  Default `counts = 0`, so the display reads `+0.00000`.
- **Abort**: a lone `67` (any STAT) is answered with `0F` and drops the conversion in
  progress. Without this every front-panel function change hung the ROM.
- **Virtual input** (`inguard.source = vinput.VirtualInput(...)`, docs/VMETER.md): replaces
  `counts` with signals in volts/amps/ohms/hertz. The stand-in converts them with the ROM's
  own scaling, sends `2x` to autorange, overload codes beyond 1.01 × full scale, and FREQ
  bytes. `realistic_timing = True` makes a conversion take NPLC / line × (2 with autozero).
  The thresholds and timing are assumptions (verify against a real 8051).
- `Board.ram_faults = RamFaults(addr_stuck={9: 0}, data_stuck={3: 1}, cell_stuck={0x0A3F: (mask, value)})`
  injects RAM U603 faults (chip address and data pins, single cells) for proving diagnostic
  messages; cells aliased below `$0040` are kept separately.
- `Board.snapshot()` / `restore()` copy the whole machine state (RAM, CPU, peripherals) for
  fast what-if runs from one booted state.
- `SP_PEEK`/`SP_POKE` operate on a 256-byte array (`inguard.mem`), not real 8051 memory.

## Cycle counts (corrected 2026-11-15)
`tools/cyclecheck.py` assembles every 6809 opcode in every addressing mode with lwasm and
single-steps each one. All 260 now agree with lwasm's tables (`tests/test_cycles.py`).
Before the fix, the emulator had two groups of errors:
- the 16-bit loads and stores (LDD/LDX/LDU/STD/STX/STU, LDY/LDS immediate) were one cycle too
  slow in every mode;
- CMPD/CMPY in memory modes were one cycle too fast.

Timings measured before the fix in code heavy with those instructions ran a few percent slow,
for example the Forth inner interpreter's `LDX ,Y++`. The display bit timing, which uses
STA/ORA/ANDA and subroutine calls, and the watchdog/NMI periods, which are counted in cycles,
were not affected.

## What has been validated against the real ROM

| Check | Result |
|---|---|
| ROM words vs Python (`+ - U* U/MOD < U< F< CMOVE RANGE` …, random inputs) | pass |
| ROM checksum word `w_CA41` | clean image 0; 1-bit patch nonzero |
| Power-on tests | AUX_ERR 0; Test 0 attempts a blocked write to the protected region |
| Link bring-up | zero burst → FF → echo 1..15 → `00`/`0F` → cal-constant download |
| Protected write (`_CAL#!` body) | watchdog reset at +0.52 s, resume via reset vector, CALREQ, `$01FD` +1, `$01FF/$01FE` = 0, lock closed, no AUX 16384 |
| HP-IB | `ID?` → `HP3457A`; `PEEK 25363` → `13344`; `PEEK 32768` → ERR 64; POKE writes one byte; PEEK is signed; `DISP 2,"…"` shows text; `KEY?` codes match the formula; `LINE?` → 60 |
| Display | power-on sequence decoded (blank → all segments → `ADDRESS= 0` → `+0.00000  VDC`) |
| Display extension (goal 6) | `DisplayMcu` model (`attach_mcu`): probe handshake via `Board.row_pull`/`in1_hooks`, reading records decoded; see docs/DISPLAY.md, tests/test_display.py |

## HP-IB over the network: emulated Prologix adapter (`tools/prologix_server.py`)
Puts the emulated meter behind an emulated **Prologix GPIB-ETHERNET** adapter, so ordinary
instrument software can talk to the real ROM.

```bash
python3 tools/prologix_server.py --valid-cal --counts 12345            # synthetic cal records
python3 tools/prologix_server.py --cal-bin 3457ACalram_2025-....bin    # a real WinGPIB dump
python3 tools/prologix_server.py --rom build/dispx.bin --nvram build/nv.bin -v
```
- Listens on **127.0.0.1:1234** (the Prologix port); meter at GPIB address **22** (`--addr`).
  One client at a time. `--nvram` keeps battery RAM between sessions (saved on exit or
  `++emu nvsave`). `--cal-bin` loads WinGPIB's 3457A `.bin` (448 bytes = `$0040-$01FF`).
- **WinGPIB**: interface *Prologix Ethernet*, IP `127.0.0.1`, address 22. Its IODevices DLL
  sends `++addr`, `++eoi 1`, `++eot_enable 1`, `++eot_char 10`, `++read`, `++spoll`, `++clr` and
  `++loc`; all are implemented. `tests/test_prologix.py` replays WinGPIB's 3457A cal dump
  (`CalRam3457.vb`: `TRIG 4`, 10 dummy PEEKs, PEEK 64..510) over a real socket and gets the
  `--cal-bin` file back byte for byte (about 0.07 s of emulated time per PEEK).
- Also: `++auto`, `++eos`, `++read eoi|<char>`, `++read_tmo_ms`, `++srq`, `++trg`, `++llo`,
  `++ifc`, `++rst` (power cycle), `++ver`, ESC escapes; extras `++emu counts <n>`,
  `++emu nvsave`, `++emu status`.
- **Deviations**: read timeouts count emulated time. A read is abandoned when new input arrives
  from the client (believed to match the real adapter, **verify**). Secondary addresses are ignored.

### Bus behaviour learned while building it
- **This ROM powers up with END OFF** (`RESET_VA` clears `EOI_EN` `$0356`): replies end in CR LF
  with **no EOI** unless `END ALWAYS` is sent. Clients must terminate on LF (WinGPIB's default)
  or set END.
- In TRIG AUTO the meter keeps a finished reading waiting for the bus. The first read after
  `TRIG HOLD` returns that stale reading and shifts every later reply by one unless the client
  drains it. That's why WinGPIB sends 10 dummy PEEKs before a dump.
- **Device clear is a warm restart** (`HPIB_DCL`: `SP! RP! DEV_CLEAR … RUN`). The ROM releases
  the 8291A's DAC holdoff with VALID (`$0F`) about 0.3 s later; GET is released in 0.8 ms. The model
  holds the handshake until then (8291A holdoff behaviour from memory, **verify** against the
  data sheet).
- Model fix: the first data byte is offered 0.5 ms after the listen-addressing change, as on a
  real bus where the addressing phase comes first. Offering it in the same instant made the ROM
  mis-parse the **first command after power-on** (TRIG HOLD ignored, `ERR?` +256 +16). The boot
  `ERR?` values quoted in earlier tests (1808 blank cal / 1296 valid) included this artifact; the
  clean values are **1536 / 1024**. Whether a very fast real controller can provoke this on a real
  meter is unknown (**verify**).

## Known gaps
- The 8051 stand-in has no analog behaviour. With a VirtualInput it does autorange, overload
  and NPLC-based timing, but from assumed thresholds, not the real 8051 firmware.
- **The first command after the meter goes remote can be lost** (`ERR?` 16 = BAD HEADER). The
  ROM reads every byte correctly (traced: `I`, `D`, `?`, LF at 0.3-0.6 ms intervals). The
  command is lost when the main loop finishes the remote transition 2-10 ms later. With a gap of
  10 ms or more between addressing and data, it never happens. Real controllers send data
  microseconds after addressing, and the ROM is the same, so a real meter may well do this too
  **(verify)**. Clients and tests should send a throw-away line first. A Prologix `++read`
  against a meter left in TRIG AUTO never ends, because readings keep arriving inside the
  timeout; that is also how the real adapter behaves.
- Emulation runs at about 0.9× real time (0.7-0.8× with the front panel open).
- The 8291A model has serial poll, SRQ, device clear, GET, GTL/LLO, IFC and the rtl aux
  command sent by the LOCAL key (see below), but no parallel poll or secondary addresses, and
  no SRQ timing.
- The external trigger and the VMCMPLT output aren't exercised.
- Annunciator bits are mapped to firmware conditions (docs/DISPLAY.md); legends on glass unverified.
- B8/C8 display instructions are recorded but not interpreted. Frames now also end at PWO falling;
  the LCD model ignores frames after a select other than `FD` (verify on real glass).

## Fault injection (`tools/faultpanel.py`, the virtual meter's "Fault injection" tab)

Faults the main-controller CPU can detect, for proving diagnostic-ROM messages and for seeing
how the HP self-test reacts. Library use:

| Attribute | Fault |
|---|---|
| `board.ram_faults = RamFaults(addr_stuck, data_stuck, cell_stuck)` | U603 address/data line stuck, cell bits stuck |
| `board.flip_rom_bit(addr, bit)`, `restore_rom()` | U602 bit errors |
| `board.power_cycle(battery_dead=True)` | battery RAM random after power-up |
| `board.protect = None` | cal-RAM write protection broken |
| `board.link.to_adc / to_main` = `dead`, `stuck_low`, `noise`, `slow_rise` | the optos A2U509 / A2U510 (11-bit frames with even parity; PE/FE reach the UART status register) |
| (8051 reset model) | the RC on RXD: a zero byte corrupted to at most two 1-bits still counts as low |
| `board.inguard.absent / selftest_ok / echo_ok` | 8051 unpowered, self-test 02, echo corrupted |
| `board.uart_dead`, `board.gpib_dead` | U616 / U614 read $FF, ignore writes (no 8291A IRQ) |
| `board.nmi_dead`, `board.watchdog_cycles` (None = never, or shorter) | NMI tick, watchdog |
| `board.line_sense` = `low`/`high`, `board.stuck_keys`, `board.secure_jumper` | IN1 b7, the key matrix, JM621 |
| `board.runaway = "watchdog"` | an undefined opcode is a runaway CPU: no more kicks, watchdog reset (default `"raise"` for finding emulator bugs; the virtual meter uses `"watchdog"`) |

**HP ROM self-test reactions in emulation** (`tests/test_faults.py` checks the starred ones).
They agree with the operating manual's AUXERR meanings. The rest are emulation results to
**verify** on a meter.

| Fault | HP ROM after 10 s |
|---|---|
| ROM bit flipped * | AUXERR 1024 (32K ROM), runs normally |
| RAM cell bit stuck * | AUXERR 4096 (non-volatile RAM) |
| RAM data line stuck * | the CPU runs wild, endless watchdog resets, blank display |
| RAM address line A9 stuck high | **passes**: the HP RAM test misses it (the diagnostic ROM's address test catches it) |
| Link dead either way, 8051 absent, RXD stuck low * | `FAILED` (DIAGNOSTIC 2) |
| 8051 self-test fails * / echo bad | AUXERR 2 / AUXERR 4, then normal |
| SIN stuck low, slow-rise opto, U616 dead | hangs during boot, display blank, AUXERR 5-7 |
| 1% bit errors 8051 -> main, 8291A dead | hangs during boot, AUXERR 0 |
| NMI tick missing * | watchdog resets every ~0.5 s (the NMI handler kicks the watchdog) |
| Line sense stuck | two watchdog resets during boot |
| Battery dead | boots with random settings (e.g. `ADDRESS=251`) |

## Planned additions

### Scanner cards (44491A armature / 44492A reed multiplexer) - not started
Purpose: exercise the ROM's scanning code (CRESET, TERM, OPT?, CHAN, CHAN?, OPEN, CLOSE, SLIST,
SLIST?, SADV) and give the virtual meter a signal per channel.

**From the operating manual** (docs/HP 3457A Operating.pdf, chapter 5, PDF pages 319-358):

| | 44491A armature (latching relays) | 44492A reed |
|---|---|---|
| `OPT?` | 44491 | 44492 |
| 2-wire channels | 0-7 (V, ACV, ACDCV, 2W ohms, FREQ/PER; no current) | 0-9 (no current, no 4W ohms, no 30 ohm range, AC to 100 kHz) |
| 4-wire channels | 10-13 (4W ohms only) | none |
| Current / actuator | 8, 9: current input (1.5 A fused) **or** actuator | none |
| OPEN / CLOSE | actuators 8, 9 (`OPEN ch,ON` adds 1.2 s delay) | not applicable |
| Speed | 33 channels/s | 300 channels/s, break-before-make |

- `TERM` OPEN 0 / FRONT 1 / REAR 2 / SCANNER 2 (REAR and SCANNER both select the rear slot).
- `CHAN?` returns −1 when no channel is selected. Power-on: no channel, actuators open, empty
  scan list, SADV HOLD. A card reset happens at power-on and on RESET/PRESET.
- `SLIST`: up to 20 entries, wraps around. The 44491 special numbers: 14/15 open actuator 8/9
  with delay, 16/17 close actuator 8/9, 18/19 open without delay. An actuator entry leaves
  the previous input channel closed and measured.
- `SADV` HOLD 0 / SGL 1 / AUTO 2 (AUTO = advance after each measurement).
- Channels 8/9 (44491) have a relay sequence: a current relay and an actuator relay, with the
  actuator closed while another channel is selected so the current path stays unbroken.
- Any scanner command without a card gives error 16, BAD HEADER.

**From the ROM:**
- The 8051 reports the card: the main asks for the OPTION byte (link STAT 7, handler B09D)
  into `OPTION` `$0392`. `OPT?` returns OPTION + 44490, so **OPTION 1 = 44491, 2 = 44492**.
  OPEN/CLOSE refuse OPTION ≠ 1, consistent with the manual. The stand-in answers 00 (no card).
- Channel selection goes out as link opcode `1B` (+ channel); `CHAN?` reads `CUR_CHAN` from the
  8051 (STAT 8, B0A9). RAM: `CHANNEL` `$0344`, `VINPUT` `$0345` (TERM), `SCAN_SIZ~9` `$0394`.
- The ◄/► status display shows `CHAN:n` when OPTION ≠ 0 (`w_F10E`).

Work needed:
1. Decode the `1B` frames (channel numbers, actuator codes, card reset) and the replies to the
   OPTION and CUR_CHAN queries.
2. Model the card in the stand-in: type, relay states (incl. the 8/9 current/actuator
   sequence), settle time from the channels/s figures.
3. VirtualInput: per-channel signals, used when TERM = REAR/SCANNER.
4. Virtual meter: a card selector (none / 44491 / 44492) and a channel table with the closed
   channel and the actuator states shown.
5. Tests: OPT?, CHAN/CHAN?, SLIST + SADV AUTO scans, the 44491 actuator codes, BAD HEADER
   without a card, the 44492 refusing channels 10-13.

### Ian Johnston's TFT display model - low priority
A second virtual screen that behaves like his STM32 firmware (10-bit HP-41 words, punctuation,
7-bit characters, his layout on a 240x960 bar panel), reimplemented from reading his source
(no licence, so no copied code). Keith expects it to ignore the dispx patch completely. It doesn't
answer the power-on `5B` probe (it only acts on 028/068/0A8/2F0), so `XDISP` stays off and no
`F7` records are sent. One real-hardware check settles it: power up the patched ROM with his
display fitted and confirm nothing is disturbed.

### Pico-based display (full 7 digits, histogram) - design not started
RP2350 (Pico 2) with PIO capture of the display bus and level shifting to 5 V. Panel: Ian's
3.71" 240x960 bar panel through the LT7680 (proven fit), or driven directly by PIO later.
Data from the dispx records (full-resolution `RESULT`). A per-conversion record hook would feed
the histogram, and optionally the raw count before `w_A85A`'s divide-by-10 for one extra digit.
Views: large digits, histogram with mean, sigma, peak-to-peak and min/max in ppm, strip chart.
Testing: portable C core built for the PC and fed by the emulator's bus decoder; a virtual
screen next to the virtual meter.

### Pico 2 W front-panel interposer (Wi-Fi remote panel) - idea
J612 (34 pins, 66511 schematic fig. 8-17) carries the display bus (PWO 31, PH1 29, PH2 27,
ISA 19, INA/SYNC), the LCD bias (V1V 22, V2V 20, V3V 18), the key columns O1-O8 (U612
through 200 ohm RP612) and the rows I1-I6 (10k pull-ups RP613, to U613; I5 = pin 13). A
board between J612 and the front-panel cable can therefore:
- capture the display (PIO), with or without the dispx records;
- see real key presses (which column is low when a row goes low);
- press keys: a crosspoint switch (MT8816-type) closing row to column like a key contact, or
  PIO pulling the row open-drain while the scan drives that column low. The scan reads IN1 a few
  microseconds after writing OUT1, which is easy for PIO;
- serve a web front panel over Wi-Fi: the LCD mirror (the 14-segment renderer and key map
  from tools/vmeter.py), clickable keys, reading stream, logging, histogram.
Can be prototyped against the virtual meter (board.keys + display3457) before any hardware.
Concerns: radio noise in a 6.5-digit meter (metal case, antenna placement; measure with the
histogram, and transmit only between readings using VMCMPLT), 5 V supply budget, and key
safety: the panel can do anything the keys can (CAL if unsecured, SSTATE overwrite, the
SHIFT + state RECALL crash). Use an allow-list.

### HP self-test analog steps (TEST key / `TEST`)
`STEST` (D4AF) runs `PON_TEST`, then eight analog steps, each setting one AUXERR bit through
`HARD_ERR` when its reading is out of limits. Decoded from the ROM:

| Step | Word | Set-up (frames, via `w_CCEB`: AD_MODE, then 1B channel, then FUNCTION/RANGE) | Check | AUXERR |
|---|---|---|---|---|
| select internal | CCCF | `1B FE` (internal channel), `FE` | - | - |
| integrator | D361 | DCV, range 0, mode 0 | -461 .. +51 counts | 8 |
| front-end zero | D385 | DCV, range -2 (30 mV), mode 3 | -150000 .. +150000 | 16 |
| current source / gain / divider | D3CB | `57` (internal 1 V reference on), DCV ranges 0..2 | 85% .. 115% of 1 V (table D3AF / D3BF) | 32 |
| amps | D3FB | `47` (test current on), DCI | 500 .. 1500 counts | 64 |
| AC amp DC offset | D431 | ACV ranges 1..2, `2F`, two-byte answer (STAT 6) | 40 .. 983 | 128 |
| AC converter | D483 | ACV ranges 0..1, `1F`, two-byte answer | 50 .. 973 | 256 |
| ohms precharge | D45F | OHM, `3F`, two-byte answer | >= 0 | 512 |

The limits are proven (ROM constants); the meaning of `57` / `47` and of the two-byte answers
is inferred from the step names in the manual's AUXERR table **(verify)**.

The 8051 stand-in models this: `1B FE` sets `inguard.internal` (any other `1B` clears it),
`57`/`47` select the reference or test current, readings on the internal channel are 0 V,
1.000 V or 1 mA scaled like any reading, and the STAT-6 answers are 500 (in limits). The
virtual source and autorange status are ignored while internal is selected. TEST therefore
passes (AUXERR 0), with or without a virtual source, and normal readings resume afterwards.

**Analog-board faults** (`board.inguard.analog_faults`, a set; also in the Faults tab under
"Analog board") each fail exactly one step:

| Fault | Effect on the internal readings | AUXERR |
|---|---|---|
| `integrator` | zero reading on range 0 +1000 counts | 8 |
| `frontend_zero` | 30 mV range zero off by 200000 counts | 16 |
| `reference` | the 1 V reference reads 0.70 V | 32 |
| `amps` | the test current reads double | 64 |
| `ac_offset` | AC offset answer 10 | 128 |
| `ac` | AC converter answer 10 | 256 |
| `ohms_precharge` | negative precharge answer | 512 |

These are models of what the ROM checks, not of real analog circuits: they show how the ROM
reports a fault, not which component causes it. `tests/test_faults.py` (`TestHpSelfTest`)
checks the pass and each bit.

### ACAL
`ACAL` (E878) runs `w_D2D1`: an AC part (`D1F7`: the AC offset and AC converter answers,
stored at `$050C-$0514`, checksum byte `$0516` = complement of their byte sum, `w_90C3`) and
an ohms/DC part (`D235`: the ohms precharge answer at `$0517` with its complement at `$0519`;
then a **gain step**: FUNCTION 16 on the internal channel, five readings averaged
(`w_CD81`), which must be 98,000,000..102,000,000 raw counts; 1/RESULT is stored as a float
at `$051B` with its complement at `$051F` (`w_9151`)). `w_CA68` then checks the block. Every
measurement set-up checks it (`w_90FB`, `w_9161`); when it's invalid the ROM uses `$0200`
for each field (`w_8D07`) and the fallback gain 1e-8 (`$322BCC77`), and raises ERR 1024
(`ACAL REQ'D`).

The stand-in answers the gain step with 1e8 counts. What the 8051 measures there is unknown
**(verify)**: the scaling rule for readings would make it 1e10 at that mode and range, so the
raw count is not a value in volts or ohms. ACAL ALL then ends with ERR 0 and leaves exactly
`coverage.ACAL_BLOCK` (`01f401f4000001f401f42b01f4fe0b322bcc77cdd43388`), which
`coverage.make_valid` now writes too, so the synthetic cal no longer boots with ACAL REQ'D.
Fault `acal_gain` (90 % of nominal) makes ACAL fail with ERR 1026. A real cal dump
(`--cal-bin`) covers `$0040-$01FF` only, so with it the meter shows ACAL REQ'D until ACAL is
run, as a real meter would after losing the block.

### How the stand-in learns the set-up
On the internal channel the stand-in takes the A/D mode, function and range from the frames,
as the 8051 must: the mode frame `m*32 + (USLINE = 0)*16 + 5`, the function frame `f*8 + 1`
followed by the range frame `(r+7)*16 + 3`, or a range frame alone (`w_A521`). Leading `7F`
bytes (the data request of the previous answer) are skipped. Elsewhere it still reads the
HP ROM's RAM. It still uses the HP ROM's link state STAT (`$03D0`) to tell which reply the
main controller expects; the diagnostic ROM's stage 10 writes STAT the same way for that
reason (docs/DIAG_ROM.md).
