# Display bus, and a firmware extension for an upgraded display (goal 6)

Sources: disassembly of the native driver `$7199-$73DD`, decompiled `DISP_SRV~9`/`w_8770`,
`DISP_UPD~11`, `DISP_KLD~9`, `w_853E`, `RES>FP`, `w_8668`, the 66511 schematic (fig. 8-17), and
emulation (`tools/display3457.py`, `tests/test_display.py`). **[emu]** = observed in the
emulator only. Timings assume the emulator's E clock of 2 MHz **(verify)**.

## 1. Signals (U611, OUT2 `$003C`, image `$0363`)
| OUT2 bit | Line | J612 pin | Use |
|---|---|---|---|
| b0 | PWO | 31 | high for the whole of a transaction; falls at its end |
| b1 | Φ1 | 29 | clock phase 1 |
| b2 | Φ2 | 27 | clock phase 2 (data is stable over both phases; sample on Φ2 rising) |
| b3 | INA | 25 | data bits, LSB first |
| b4 | SYNC | 21 | high while an instruction is on ISA. TONE also toggles this bit, but without Φ clocks |
| b5 | ISA | 19 | instruction bits, LSB first |
| b6 | VMCMPLT | – | not display |
| b7 | OTL\* | – | watchdog kick, held low during display traffic |

Bit-banged by the CPU: each bit is put on INA/ISA, then Φ1 is pulsed, then Φ2 (ROM `$7381`).
One clock takes 36 E-cycles = **18 µs** [emu]. While a transaction runs, interrupts are masked
and OUT1 is cleared (`MASK*` low holds off the NMI tick, whose handler rewrites OUT2).

## 2. Transactions
```
PWO start   (7 clocks: PWO low 1, then high 2)            ROM $72F3
FC  + FD    select the display (address byte on INA) + 48 idle clocks   $7311
B8 , C8     (no data)                                     HP-41 DISOFF / DISTOG? (verify)
BC  + 12 bits   annunciators = ANNUNCIATORS ($0309) bits 0-11          $7290
2A  + 48 bits   register C: punctuation bits, 4 per char  ($030B..)    $7244
0A  + 48 bits   register A: low nibble of each char, char 11 first     $71EA
1A  + 48 bits   register B: high nibble of each char                   $7215
PWO end     (112 idle clocks, then PWO low)               $7304
```
Every instruction is sent as 44 idle clocks, then SYNC high for 2 + 8 clocks with the opcode on
ISA, then 2 gap clocks; its data follows on INA (`$72B8`).

Ian Johnston's TFT firmware (`3457A_VSCODE_Display`, `Core/Src/timer.c`) reads the same frames
as **10-bit HP-41 instruction words**: the 2 leading SYNC clocks are the low bits, so
word = opcode × 4: `0A`→`028`, `1A`→`068`, `2A`→`0A8`, `BC`→`2F0`, `B8`→`2E0`, `C8`→`320`, `FC`→`3F0`.
`3F0` is the HP-41 peripheral select and `FD` the HP-41 display address. If the HP-41 names
carry over, `2E0`/`320` would be display off / display toggle **(verify)**. His decoder samples on
the rising edge of Φ2 ("O2") and only while PWO is high, as `tools/display3457.py` does.

- **Full refresh** (`$719F`): FC FD, B8, 0A, 1A, BC, 2A, C8 (LBSRs at `$71D4-$71DD`;
  checked by tests/test_diag.py). It isn't used during boot in emulation; `DISP_DUMP` is.
  *(Corrected again: the order listed here before, B8 BC 2A 0A 1A, was wrong.)*
- **Normal update** `DISP_DUMP` (`$7392`, mask in B): FC FD, B8, C8, then only the registers
  whose mask bit is set: b0 annunciators, b7 register C, b1 register A, b2 register B.
  `DISP_KLD~9` computes b1/b2 by comparing `WINDOW` with the shadow copy at `$03B5`, so only
  changed nibble planes are sent. The caller ORs in extra bits (`$F8` forces register C).
- **Characters**: `WINDOW` `$02F1` (pointer `WINDOW^` `$02EF`), 12 bytes in the 6-bit HP-41
  set (`$01` A … `$20` space, `$30` 0). Byte bits 4-5 go out in register B; B bits 2/3 and
  register C carry the punctuation. Ian's decoder (from real glass) reads a character as
  A nibble | (B & 3) << 4 | (C & 1) << 6 (a 7-bit code), with **punctuation = (B >> 2) & 3:
  1 `.`, 2 `:`, 3 `,`**. `display3457.Lcd` now decodes the same way (`Lcd.cells()`); the glyphs
  for 7-bit codes ≥ `$40` are still a guess (verify).

### Annunciator bits (`$0309`, wire bit k = word bit k)
Set via `ANNUNCIA~10 ( flag mask -- )` into the pending-change word `$03C5`; `NEW_ANNU~10` applies it.

| Bit | Condition in the firmware | Legend (Ian's TFT firmware, from real glass) |
|---|---|---|
| 0 | `SHIFT` `$03C4` set | SHIFT |
| 1 | error word `$0373` ≠ 0 | ERR |
| 2 | `VINPUT` = 2 | REAR |
| 3 | math A or B active | MATH |
| 4 | `A_RANGE` = 0 | MRNG (manual range) |
| 5 | `A_ZERO` = 0 | AZOFF |
| 6 | `FUNCTION` & `$FD` = `$11` | 4Wohm |
| 7 | `FUNCTION` & 5 = 5 | AC+DC |
| 8 | 8291A reg 4 & `$C6` (addressed) | ADRS |
| 9 | 8291A reg 3 & `$40` (also `SPBIT`) | SRQ |
| 10 | `INT` `$03C2` & `$10` | REM |
| 11 | not driven by `w_8668` (set elsewhere, not yet traced) | SMPL |

## 3. When the display is written: `DISP_SRV~9` (`$882A` → `w_8770`)
Runs from the cooperative main loop at `$DC73` (`PARSE_SR` → `KBD_SRVC` → `DISP_SRV`), never
from an interrupt. It's a state machine on `STAT` `$03C7`, rate-limited by `DISP_TIM` `$03C9` against
`TIMER_LO` (32.768 ms ticks):

| State | Action |
|---|---|
| 1 | wait for the timer → 2 |
| 2 | update annunciators; if `RDG_RDY` `$03C8`: clear it, **format the reading (`w_853E`)**, `DISP_UPD`, next reading allowed after 7 ticks (229 ms) → 1 |
| 3 | annunciator-only refresh → 5 |
| 4 | after a key-load (`DISPKLDG`): show range/function only (`FRN>DISP~11`) → 2 |
| 5 | wait for the timer → 3 |

`w_853E` writes the sign (`w_8362`), the digits rounded to `DIGITS` (`$0343` = NDIG + 1) and the
units (`w_83A4`) into `WINDOW`. The full-resolution value is still in **`RESULT` `$0339`**:
- `FPRES` `$033D` = 0: a 32-bit integer; value = `RESULT` × 10^(`OLD_RNG` + `MODE>RNG`(`OLD_ADMO`))
  (the same formula as `RES>FP`). `$7FFFFFFF` / `$80000001` = ±overload.
- `FPRES` ≠ 0 (math results): `RESULT` is an IEEE single.

## 4. Extension (a): reading records for an upgraded display
`tools/dispx_patch.py` builds `build/dispx.bin`: 475 bytes in the zero block, 2 hooked cells, and
`FREE_BOT` raised by 16 bytes for RAM.

- **Hook `$87FE`**: in `DISP_SRV` state 2, `w_853E` becomes `XRDG` = `w_853E`, then
  `XSEND` if `XDISP_V` (`$0765`) ≠ 0. So a record goes out exactly when the meter shows a new
  reading, just before the normal LCD update.
- **Wire format** (new code `XFRAME`, reusing the ROM bit-bang routines, with the same masking):
  ```
  PWO start, FC + F7 (select peripheral F7), 5A + 9 bytes (LSB-first on INA), 8 clocks, PWO end
  ```
  | Byte | Content |
  |---|---|
  | 0 | record version = 1 |
  | 1 | flags: b0 = `FPRES` (bytes 2-5 are IEEE single) |
  | 2-5 | `RESULT`, big-endian |
  | 6 | decimal exponent (signed) = `OLD_RNG` + `MODE>RNG(OLD_ADMO)` |
  | 7 | `DIGITS` (NDIG + 1, as the glass shows) |
  | 8 | `FUNCTION` code |
  Decoder: `display3457.decode_xdata()`.
- **Cost**: 8.4 ms per record with the NMI held off, versus 18.4 ms for the stock full update
  [emu]. Not sent at all unless `XDISP_V` = 1.
- **Verified [emu]**: with counts giving 1.235 mV the glass shows `+0.00124  VDC` (NDIG 5), the
  record carries 1235 × 10⁻⁶, and `TRIG SGL` over HP-IB returns `1.235E-03`. The LCD model
  ignores everything after a select other than `FD`.

## 5. Extension (b): detecting the upgraded display
**There is no spare input bit.** `IN1` (U613) = 6 key rows (10 kΩ pull-ups, RP613),
JM621/O6, `LINE_FREQ`. The 8291A and the HD6402 have no free inputs either. A diode wired
permanently into an unused key position doesn't work: `KBD_SRVC` first drives all columns low
and treats any low row as a held key, so the stock keyboard would lock up (`/ROLLING` never
re-arms).

**Handshake instead** (`DPROBE`, run once at power-on after `PON_TEST`, hook `$FD36`, and by the
`XPROBE` command):
1. OUT1 = `$FF` (**no column driven**, so no key can pull a row low). Row I5 must read high.
2. Send `FC F7`, `5B` + 1 byte (protocol version). The display MCU pulls row **I5** low.
3. Poll up to ~33 ms (200 loops) for I5 low.
4. Send `FC F7`, `5C` + 1 byte. The MCU releases I5. Poll for I5 high again.
5. `XDISP_V` = 1 only if 1, 3 and 4 all succeed. OUT1 is restored from its image.

The keyboard service can't run during this (it shares the main loop), so the probe can't cause
a phantom key [emu: held keys and a 20 ms MCU latency still detect correctly; 50 ms isn't
detected; stock ROM never probes].

**Hardware (proposal, not built):** one MCU GPIO, open-drain, through ~1 kΩ to row I5 (`IN1` bit 0;
J612 pin 13, U613 pin 2 per fig. 8-17 **(verify)**). When released the pin sees 5 V through the row's 10 kΩ pull-up, so on an
STM32F103 use a pin marked **FT** (5 V tolerant) in the datasheet pin table **(verify)**, or add an
NPN/N-MOSFET. The row's pull-up stays as it is.

**Display-MCU firmware rules:**
- Track `FC xx`: only act on frames after `FC F7`. Ignore anything after `FC FD`, which is the
  stock LCD traffic.
- On `5B`: pull I5 low within **25 ms**. On `5C`: release it within 25 ms. Auto-release after
  50 ms regardless, so a lost `5C` can't leave a stuck "key".
- `5A`: 9-byte record (see 4). Frames end at PWO falling.

## 6. HP-IB commands added by the patch
| Command | Effect |
|---|---|
| `XDISP <0\|1>` | turn reading records off/on (default 1 if no argument) |
| `XDISP?` | 0/1 |
| `XPROBE` | repeat the detection handshake; sets `XDISP_V` |

## 7. Risks and open points (before anyone flashes this)
- **Stock LCD and address `F7`** **(verify)**: the model assumes the LCD ignores frames after a
  select other than `FD`. Every stock transaction re-selects `FD` first, so a misbehaving LCD
  could at worst glitch until the next update. Check on a stock-glass meter with a scope or logic
  analyser: `XDISP 1`, then watch the glass.
- **Ian Johnston's TFT firmware** (source published, `3457A_VSCODE_Display`; no licence file, so
  treat it as all rights reserved): by code reading, its decoder arms a payload only for the 10-bit
  words `028`/`068`/`0A8`/`2F0` and doesn't track the `3F0` select at all. The extension words
  `5A`/`5B`/`5C` (= `168`/`16C`/`170`) are therefore ignored and can't corrupt the TFT
  **(verify on hardware)**. Using the records needs a small addition to his decoder: track the
  `3F0` address byte and capture `168` (9 bytes) when it is `F7`. That's his code, so it's a
  change to propose to him. His pins: O2 PB1, SYNC PB11, PWO PB12, ISA PB14, INA PB15, SPI
  PA4-7, LCD PB3-5, LED PC13. A free FT pin (e.g. in PB6-PB10 or PA8-PA10, verify) could drive I5.
- **Row I5 wiring**: a mistake there affects keyboard rows only. No cal RAM or protected writes are
  involved in this patch.
- `FREE_BOT` +16 bytes: user memory shrinks by 16 bytes. `MEM_INIT` runs at every power-on, so the
  repartition is automatic, but stored readings/subprograms from the stock layout should be
  considered lost **(verify)**.
- E clock assumed 2 MHz. If it's different, the bit time and the 33 ms probe window scale with it.
- B8/C8 meaning: confirm. Punctuation and annunciator legends now come from Ian's firmware
  (derived from real glass); bit 11 SMPL's setter in the ROM is not yet traced.
