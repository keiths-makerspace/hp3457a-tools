# Diagnostic EPROM for the 03457-66511 main controller

A replacement for U602 (27512), for the bench only. It runs a fixed sequence of tests, each
relying only on parts already proven, and names the failing part on the LCD. Source in `diag/`;
it never touches the original ROM image.

**Status: all stages (0-9) done in emulation.** That covers the toolchain, the display driver without RAM,
the ROM checksum and the RAM tests. Every failure message has been proven in the emulator by injecting
the fault it names. Nothing has been programmed into an EPROM or run on a meter yet.

## Toolchain
- **lwasm** (LWTOOLS 4.25), built from source in WSL Ubuntu-26.04 (`~/.local/bin`).
  Always `--6809`, because the default 6309 mode gives wrong cycle counts and accepts 6309-only
  opcodes.
- `tools/asm.py SRC OUT [--checksum] [-D NAME=VAL]` runs lwasm (natively or through WSL),
  places its Intel HEX by address in a 64 KB image filled with $FF, and rejects anything
  below $2000, which the CPU can't see. It writes `.lst` (with cycle counts), `.map` and `.sym`.
  `--checksum` sets $FFF0 so that the word sum over $2000-$FFFF is 0, the HP convention.
- `tools/lwmap.py`: `Symbols(".sym").name(0xE1B3)` gives `isend+7`. `Tracer(board, syms)` keeps
  the last PCs of an emulator run, labelled.
- `tests/test_diag.py` assembles the source and runs it in `emu3457`. It is skipped where lwasm
  isn't available.

```
python3 tools/asm.py diag/diag.asm build/diag.bin --checksum
python3 tools/lwmap.py build/diag.sym --trace build/diag.bin 0.5
python3 tools/vmeter.py --rom build/diag.bin        # watch it on the virtual panel
```

## Safety rules (built into the code; the cal constants are irreplaceable)
1. **Never access `$0030-$0037`.** Even a read of CALREQ arms the cal-RAM write window.
2. **Never write `$0040-$01FF`.** Cal RAM is checked by reading only. A test that proves the
   write protection works needs a write; it will be opt-in, with a key press to confirm, and
   written up as a proposal for Keith first.
3. **The RAM test restores what it tests.** It saves, tests and restores each byte with
   interrupts off. The remaining risk, a power loss mid-test, could corrupt one byte of user
   state, never cal data.
4. **Only harmless link traffic.** Stages 5 and 6 send the 8051 reset and echo only, which
   the stock ROM does at every power-on. Stage 10 (a **proposal**, below) sends exactly the
   frames of the HP self-test (TEST key), which a user can send at any time, and nothing
   else: no other relay/range frames, no SP_POKE, no SP_CALL, no ACAL, no CAL.
5. **Keep the watchdog kicked:** at least every 25 ms (it fires after about 0.52 s), except in
   the deliberate watchdog test and one 32.8 ms NMI-tick measurement.

## Stage 0: display without RAM (done)
- Stack-free code: S is never loaded, so the 6809 keeps NMI disarmed. JSR/BSR are never used;
  a `CALL` macro puts the return address in U and the routine ends with `JMP ,U`.
- Registers: A = image of OUT2 (the latch is write-only), X = #OUT2, B = data, Y =
  counter/pointer, DP = pass counter (DP doubles as the only "variable").
- A copy of the ROM driver `$72B8-$7391`:
  - each instruction is 44 idle clocks, then SYNC with 2 zero bits, then 8 bits on ISA, then 2 gap clocks;
  - data goes out on INA, LSB first;
  - a full refresh is `FC FD B8 0A 1A BC 2A C8`, the same order as the HP routine `$719F`.
- The bus is never clocked faster than the ROM clocks it: at least 36 cycles (18 µs) per bit,
  checked by a test.
- Result in emulation: it shows "HP3457A DIAG" and "NO RAM USED " alternately with SMPL
  blinking, the watchdog never fires, and no RAM byte changes.

## Stages 1-2: ROM checksum and RAM U603 (done)
The sequence repeats forever and counts passes:
1. `HP3457A DIAG` for 1 s.
2. **ROM checksum**: the word sum over `$2000-$FFFF` must be 0, in two halves with a watchdog
   kick between them. On failure: `ROM CKSM BAD`.
3. **Data lines**: walking 1 and walking 0 at `$0400`. If that fails, the same test runs at
   `$1C00`. Only if both fail is it a data line (`RAM D3 BAD`, the lowest wrong bit);
   otherwise the cell at `$0400` is reported.
4. **Address lines A0-A12**: from bases `$1FFF`, `$0AAA` and `$1555`, write `$55` to the base
   and `$AA` to base ^ (1 << k).
   - Base reads `$AA`: the two addresses alias, so `RAM A9 BAD`.
   - Base reads anything else: that cell is bad, and its address is reported.

   Both cells are saved and restored.
5. **Cells `$0200-$1FFF`**: each byte is saved, written and read with `00 FF 55 AA`, restored
   and checked. Watchdog kick every 256 bytes (about 0.3 s for the whole test). On failure:
   `RAM 0A3F BAD` (address in hex). For a cell inside the scratch buffer: `RAM PAD BAD`.
6. `ALL PASSED`, then `PASS    0001`, a decimal count kept in the RAM buffer. The cell test
   restores it, so it survives each pass.

Failures halt on the message with ERR lit.

Implementation:
- Messages are a ROM table of 14-byte entries (sentinel, 12 characters, annunciator flags),
  selected by DP. Index `$7F` selects the RAM buffer.
- The refresh is called with a link in S. S is also used as a data register (the
  address-line mask). Loading S arms NMI, but OUT1 = 0 keeps the NMI gate closed.
- **Scratch:** only `$020F-$021C`, inside the HP firmware's PAD, which the HP ROM rebuilds at
  every boot. It is used after it has passed the test.

**Proven in emulation** (`tests/test_diag.py`, 11 tests; `emu3457.RamFaults` injects stuck
address lines, data lines and cell bits):
- On a clean board it loops, the pass count advances, and the only RAM bytes changed are the
  14-byte scratch buffer. Cal RAM is never written, and I/O goes only to OUT1/OUT2.
- Every data line stuck high or low is named. Address lines 0, 6, 9 and 12 stuck high or low
  are named.
- Bad cells are named with their address, including cells at the test addresses, which must not
  be mistaken for line faults.
- A flipped ROM bit gives the checksum message.

**Limits:**
- The per-byte test does not find coupling faults between cells. A full march test would
  destroy the battery-backed contents.
- Cal RAM `$0040-$01FF` is not cell-tested, because it can't be written. Its lines are covered
  through the rest of the chip, and stage 3 will check its contents by reading.

## Stage 3: cal RAM, read only (done)
- **What is checked:** the 32 per-range records plus the shared AC record (`$0077`), each with
  its checksum byte = ~(sum of the other bytes). The global constant (`$018B`, 4 bytes) and the
  trim (`$0193`) are checked against their complement copies. Layout from docs/CALIBRATION.md
  (`w_8D85` map, `w_8E97`, `w_8F3D`, `w_9037`).
- **Results:**
  - All good: `CAL RAM OK`.
  - Some bad: `CAL BAD   nn`, then each bad item for 1 s with ERR (`CAL DCV 30MV` ...
    `CAL OHMF 30M`, `CAL AC SHARE`, `CAL GLOBAL K`, `CAL TRIM`).
  - More than half bad: `CAL RAM LOST`, the battery / lost-RAM signature.
- **It does not halt**, because bad cal data isn't a board fault. The end-of-pass message
  becomes `CAL ERRORS` instead of `ALL PASSED`.
- **Work area:** the HP firmware's data stack (`$029F-$02D9`, volatile): a second message
  buffer, the bad list, and counters. The pass counter in the first buffer survives.
- **Proven in emulation:** valid cal, three named bad items, and lost cal all give the expected
  messages, and `$0040-$01FF` is never written.
- **Not checked:** the ACAL constants at `$0517` (unprotected, complement pairs, extent
  unknown) and the security code. **(verify)** On a real meter, every record should pass.
  Unused ranges were never seen with bad checksums in emulation, but a meter that was never
  calibrated on some range might show those as bad.

## Stage 4: NMI tick and line frequency (done)
- **NMI tick.** With RAM proven, the ROM loads a real stack in the HP return-stack area
  (`STACKTOP $0287`) and opens the NMI gate (OUT1 b0 = MASK*). It counts a 13-cycle loop over
  4 ticks and shows `NMI 32.766MS`. The handler costs 44 cycles per tick, which is included.
  If no tick arrives within 0.2 s: `NO NMI TICK`. Anything outside ±1 % of 32.768 ms gets ERR.
  The gate is closed again afterwards.
- **Line frequency.** It times 4 periods of IN1 b7 with 20-cycle loops, then computes
  f × 100 = 40,000,000 / count with a 32/16 shift-subtract division, giving `LINE 60.00HZ`.
  If there is no edge within 0.2 s: `NO LINE SENS`. Anything outside 2 % of 50 or 60 Hz gets ERR.
- **E clock:** the ms and Hz figures use the build constant `EKHZ` (default 1995 kHz, from the
  faulty meter's 7.980 MHz Y601; `python3 tools/asm.py ... -D EKHZ=2000` for an 8 MHz board).
- **Why both.** The NMI is E/65536, so it is counted in the same clock it is measured with, and
  only shows a missing tick or a wrong divider. Mains is an independent reference, so a crystal
  3 % fast shows as `LINE 58.26HZ` with ERR.
- **Neither result halts the run.** Errors set the end-of-pass message to `ERRORS FOUND`.
- The timed loops use only short branches, with `LBRA` trampolines for the timeouts, so the
  cycle counts stay as written. `SETDP $40` makes every `$00xx` reference assemble as extended,
  because DP holds the message index.
- **Proven in emulation:** 60 Hz and 50 Hz, NMI dead, line sense stuck low or high, and a
  crystal 3 % fast. The NMI gate is closed afterwards and there is no watchdog reset.
- **On a real meter:** the reading is ±1 count of the loop (about ±0.01 %). The line reading is
  only as good as the mains frequency (typically ±0.1 %).

## Stage 5: cross-isolation link (done)
The same handshake the HP power-on Test 2 relies on, turned into a diagnosis. Path: U616
(HD6402) → J613 → opto A2U509 → 8051 RXD, and 8051 TXD → Q501 → opto A2U510 → J613 → U616.

1. **U616 present:** a status of `$FF` means nothing drives the bus: `NO UART U616`. The
   transmitter must empty its holding register (TBRE) within 2000 loop turns, otherwise
   `UART TX STUK`.
2. **8051 reset:** 2000 back-to-back zero bytes hold TX low for about 117 ms (the 8051's
   RESET follows RXD through an RC of about 10 ms). Then the ROM waits up to 1 s for the
   self-test byte:

   | Received | Message | Meaning |
   |---|---|---|
   | `FF` | `A/D RESET OK` | 8051 alive, self-test passed |
   | `02` | `A/D TEST BAD` | 8051's own RAM/ROM test failed (HP AUXERR 2); the echo test still runs |
   | nothing | `A/D SILENT`, `CHK OPTO PWR` | either direction dead, 8051 unpowered or held in reset. These can't be told apart from the CPU alone; next step is a scope on RXD/TXD or a loopback at J613 |
   | breaks (00 + framing error) | `LINK RX LOW` | SIN held low: A2U510 shorted, or the 8051 TXD / Q501 stuck |
   | another byte | `A/D SENT xx` | the 8051 runs but says something unexpected |
   | garbled (parity/framing) | (continues) | alive but the reply was corrupted: the echo test classifies it |

3. **Echo test:** the 8051 echoes every byte until it receives 00. The ROM sends 01-FF and
   counts wrong echoes, lost echoes, echoes flagged by U616 (parity/framing), echoes that only
   lost 1-bits, and the OR/AND of the wrong bits:

   | Pattern | Message | Likely place |
   |---|---|---|
   | all correct | `ECHO OK` | link fine |
   | the same single bit always wrong | `ECHO ERR nnn`, `ECHO BIT n` | 8051 side (port/bus), not the serial line |
   | only 1-bits lost, flagged by U616 | `SLOW EDGE RX` | 8051 → main: A2U510 (CTR), its pull-up, Q501 |
   | only 1-bits lost, not flagged | `SLOW EDGE TX` | main → 8051: A2U509; the 8051 got bad data and echoed it cleanly |
   | none answered | `A/D NO ECHO` | the 8051 reset but doesn't echo |
   | anything else | `LINK NOISY` | intermittent contact, cable, noise |

   A `0F` answer to anything but `0F` could be the 8051's ACK, meaning a byte arrived as 00 and
   echo mode ended. The ROM resends the byte to tell an ACK from a wrong echo. On an ACK it
   resets the 8051 again, at most three times, then stops. The error count is the count up to
   that point.
4. Finally a 00 ends echo mode, which is the state the stock ROM leaves it in.

**Proven in emulation** (`tests/test_diag.py`, link faults from `LinkFaults`): no fault; dead
either way; 8051 absent or held in reset; SIN low; slow rise on either side, with the
direction named; noise either way; 8051 self-test 02; echo bit 6; U616 missing. No watchdog
resets.

The UART status bit positions, `$FF` for a missing U616 (RP611 pull-ups) and DRR via `$0018`
are confirmed by the HD6402 data sheet and the schematic.

**Assumptions to verify on a real meter:**
- the 8051's echo protocol (taken from what the HP ROM expects: `FF`, echo until 00, then
  `0F`);
- the slow-edge and noise patterns, which come from the emulator's line models. Real optos may
  fail in other ways, and the counts and patterns are the evidence to look at.

## Stage 6: watchdog, and unexpected restarts (done)
- The ROM writes a signature (`WDSIG $020D`) to battery RAM, kicks once more, then counts a
  40-cycle loop into RAM (`WDCNT $021D`). When the reset comes, `reset` finds the signature,
  converts the count to ms (count / 50) and resumes at the end of the pass:
  - 300-800 ms: `WDOG  524MS`;
  - under 300 ms: the time with ERR, then `WDOG EARLY`;
  - over 800 ms: the time with ERR;
  - no reset within 1.2 s: `WDOG NO RST`.
- **Kick policy:** everything kicks at least every 25 ms. The one exception is the 32.8 ms
  NMI-tick measurement, the same interval as HP's own NMI-driven kick. So a watchdog that
  fires early is measured rather than looping the ROM forever. `tests/test_diag.py` checks the
  longest gaps.
- **Unexpected restarts:** each pass writes `RUNSIG $021F` and the stage number (`$0221`). A
  reset that finds them shows `RESTARTED` and `IN STAGE n` with ERR, then starts a new pass.
  The cause could be a watchdog firing outside stage 6, a power dip, or a runaway. A halt clears
  the signature, but switching off in the middle of a pass also shows it once.
- **Proven in emulation:** 524 ms; never fires; fires at 100 ms and 50 ms (`EARLY`); fires at
  1.0 s; a power cut in stage 2.

## Stage 7: HP-IB controller 8291A U614 (done)
- After an aux chip reset (`$02`), the ROM writes patterns to SPM (register 3) and reads them
  back through SPS (register 3, read). Bit 6 (rsv) is never set, so SRQ is never asserted on
  the bus. It finishes with another chip reset and SPM = 0.
- Results: `HPIB U614 OK`. If every bit is wrong and SPS reads `$FF`: `U614 NO RESP`.
  Otherwise `U614 Dn BAD` for the lowest wrong bit.
- Address 0 (6W) then reads back through 6R, with DT0 in bit 6, which covers D6 as well.
  Finally DT = DL = 1 in both address registers, so the meter neither talks nor listens during
  the diagnostics.
- **Confirmed by the 8291A data sheet (p. 3-12):** SPS reads back the SPM byte, with bit 6 =
  SRQS, and the Address registers read back through 6R/7R.

## Stage 8: keyboard and the security jumper (done)
- With no column driven (OUT1 = `$FF`), a low row means a short: `ROW n SHORT`.
- Each column is driven low in turn. A key found down gives `KEY STUCK`, then its name
  (`KEY NPLC`).
- **JM621** (wiring from the schematic, FINDINGS.md): IN1 b6 is read with O6 driven low and
  then high:
  - high both times: `JUMPER NORM`;
  - follows O6: `JUMPER KEY` (SECURE DIS., cal security disabled);
  - low both times: `JUMPER DSA`.
- **Every pass:** `PRESS KEYS` opens a window, 10 s on the first pass and 3 s on later
  passes. Every key pressed is named, and after each one the window runs for another 10 s,
  so you can work along the keyboard at your own pace. It is skipped when a key or row is
  stuck. **AUTO CAL** pressed in the window also runs stage 10 on that pass.
- Until this change the window was meant to be 3 s, but its 25 ms wait was 12.5 ms at the
  real 1.995 MHz E clock (the loop count was half of `WAIT`'s), so it closed after about
  1.5 s.
- OUT1 b0 is column O6 and also MASK*, the NMI gate, so stage 8 uses the real stack while it
  scans and closes the gate before every display refresh.

## Stage 9: display walk (done; first pass only, checked by eye)
1. Everything on: all segments, punctuation and the 12 annunciators.
2. Each character position in turn, left to right.
3. Each annunciator in turn, left to right (SMPL ... SHIFT).

It uses a raw refresh (`rawref`) with the same bus timing as the normal one.

**Proven in emulation** (`tests/test_diag.py`, 33 tests for the whole ROM): 8291A OK and
missing; a stuck NPLC key named, with no press window; jumper KEY; ENT named in the press
window; the walk order, frame by frame.

## Stage 10: the HP self-test's analog steps (PROPOSAL: review before use on a meter)
Runs on the first pass, every 10th pass, and on any pass where AUTO CAL is pressed in the
`PRESS KEYS` window (stage 8). It does not run on every pass because the relays switch at each
run, as at every TEST. A fault that appears later is therefore found on the next 10th pass, or
straight away with AUTO CAL.

**What it does.** It resets the 8051 and leaves echo mode (`00` -> `0F`), then sends the
frames of `STEST` (D4AF, docs/EMULATOR.md "HP self-test analog steps"), checks each answer
against the same limits and sets the same bits as the HP ROM's AUXERR:

| Step | Message on failure | AUXERR |
|---|---|---|
| integrator convergence | `INTEGRATOR` | 8 |
| front-end zero | `FRONTEND ZRO` | 16 |
| current source / gain / divider | `REF GAIN DIV` | 32 |
| amps | `AMPS TEST` | 64 |
| AC amp DC offset | `AC AMP DC OS` | 128 |
| AC converter | `AC CONVERTER` | 256 |
| ohms precharge | `OHMS PRECHG` | 512 |

Then `A/D TEST OK`, or the sum as `AUXERR   256` (the number HP's `AUXERR?` would give for
these steps). A link problem gives `A/D NO ACK` or `A/D NO RDG` followed by the step's name;
no 8051 gives `A/D SKIPPED`. At the end it resets the 8051 again, so the analog board is back
in its power-on state (internal reference, test current and internal channel off).

**The ACAL constants** (`$050C-$051A`, battery RAM, written by ACAL) go into some of the frames,
as the HP ROM sends them. They are **read only**. If their checksum or complement is wrong the
stage shows `ACAL REQ'D` and uses `$0200` in their place, as the HP ROM does (`w_8D07`); the
steps still run. It counts as a cal error (`CAL ERRORS` at the end of the pass), not hardware.

**How it was checked (emulation):**
- the bytes sent are identical to the HP ROM's TEST, all 243 of them, and so are the replies
  (`test_stage10_sends_the_hp_bytes`, at 50 Hz: STEST sets USLINE, so the mode frames' bit 4
  is 0 at either line frequency, and the diagnostic ROM does not use its line measurement here);
- each analog fault of the 8051 stand-in names its step and gives its AUXERR bit; all seven
  give `AUXERR  1016`; lost ACAL constants and a missing 8051 are reported;
- the watchdog is kicked at least every 25 ms; cal RAM is unchanged.

**What emulation cannot prove (risks for review):**
1. **The order and timing of the handshake** come from the HP ROM's code (ACK `0F` after each
   frame; `77 nn` then status `1x`, `7F`, data; a 2-byte answer as byte, `7F`, byte). The
   diagnostic ROM sends the next frame as soon as the reply has come, as the HP ROM does, but
   it skips the HP ROM's Forth overhead between frames (a few ms). If the real 8051 needs that
   time between frames, a step would show `A/D NO ACK`. Mitigation if seen: a short delay
   after each ACK.
2. **Settling.** The HP ROM waits about 0.2 s before the AC offset reading (`WAIT_TIME 6`); the
   diagnostic ROM waits 0.2 s too. Other steps have no explicit wait in the HP ROM either.
3. **The internal paths switch relays** and turn on the internal reference and test current,
   exactly as TEST does. Nothing is connected to the input terminals by this; leave the inputs
   open as for a TEST.
4. **Writes:** only its own variables (`$0289-$0295`, unused HP data stack) and the HP ROM's
   link state STAT (`$03D0`, volatile, rebuilt at every HP boot; the emulator's 8051 stand-in
   reads it to know what the main controller expects). No cal RAM, no ACAL constants, no
   battery-RAM user state.

**Suggested first use:** on the working meter, inputs open. Expected: `A/D TEST OK`. Then the
same meter's own TEST (SHIFT + ◄) under the HP ROM should agree. Only then the faulty meter.

## Using it
```
python3 tools/asm.py diag/diag.asm build/diag.bin --checksum
python3 tools/vmeter.py --rom build/diag.bin --blank-cal       # fault-injection tab opens first
```
The first pass takes about 46 s (with the 10 s key window, display walk and stage 10), later
passes about 23 s (3 s key window), and every 10th pass about 27 s.

Until this change, the reset routine set the first-pass flag before it checked for stage 6's
deliberate watchdog reset, so every pass looked like a first pass and ran the key window and
the display walk. Fixed: a watchdog-test reset keeps the flag.
Programming a 27C512 and fitting it in U602 is the step after review. Nothing here has run on a
real meter yet.
