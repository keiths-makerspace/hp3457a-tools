# Virtual meter: front panel and virtual input

`tools/vmeter.py` runs the real main-controller ROM in the emulator with:
- a **front panel**: the LCD drawn from the display bus (14-segment glyphs, punctuation,
  12 annunciators) and a clickable keyboard wired into the key matrix;
- a **virtual signal source** at the input terminals (`tools/vinput.py`): DC/AC volts,
  DC/AC current, resistance, frequency, plus optional noise and a slow sine "wobble";
- optionally the **Prologix GPIB-ETHERNET server** (`--prologix`) on the same meter, so
  WinGPIB or a script can talk to it while you press keys.

```
python3 tools/vmeter.py                                  # synthetic valid cal, input open
python3 tools/vmeter.py --dcv 1.2345678 --ohm 4.7e3 --freq 50 --noise 2
python3 tools/vmeter.py --cal-bin 3457ACalram_xxx.bin --prologix     # your meter's constants + WinGPIB
python3 tools/vmeter.py --nvram build/vm_nv.bin          # battery RAM kept between sessions
python3 tools/vmeter.py --rom build/dispx.bin            # a patched ROM image
```
Options: `--blank-cal`, `--line-hz 50|60`, `--fast-conv` (20 ms conversions), `--addr`,
`--host`. Only tkinter (stdlib) is needed. Nothing here touches a real meter.

Speed: about 0.7-0.8× real time with the window open (the 6809 core is pure Python).
Readings arrive a little slower than on a real meter; the status bar shows the speed.

## Fault injection tab
The right-hand panel has two tabs: **Input terminals** (the virtual source) and **Fault
injection**. The second injects hardware faults the CPU can detect:
- RAM lines and cells, ROM bits, a dead battery, cal-RAM corruption or broken write protection;
- the opto-isolator link (dead, stuck low, noisy, slow rise) and the 8051;
- U616, U614, the NMI tick, the watchdog and the line-frequency sense;
- stuck keys and the security jumper.

Each section names the diagnostic-ROM stage meant to detect it. It opens first with `--faults`,
or whenever `--rom` isn't the HP ROM, e.g. `python tools/vmeter.py --rom build/diag.bin`. The
models are described in docs/EMULATOR.md, "Fault injection". With RAM faults active, emulation
slows to about 0.5x because every RAM access goes through the fault model.

## Front panel

The matrix positions and functions were recovered from the ROM (key table plus emulation). The
layout and legends are confirmed by a front-panel photo (`docs/photos/3457a.jpg`). The arrow
order follows from the shifted legends: the first range key carries AUTO (ROM: SHIFT + range up =
autorange) and the second HOLD (SHIFT + range down = manual). The panel's colours are taken from
the same photo.

```
FUNCTION   DCV   ACV    OHM   DCI   ACI    FREQ           (shift: ACDCV, OHMF, ACDCI, PER)
             RANGE
           ▲     ▼      ◄     ►     LOCAL  [SHIFT]        (shift: AUTO, HOLD, TEST, RESET, ADRS)
             SCROLL      DISPLAY

CONFIGURATION                                      MATH
  A NPLC   B AUTO CAL  C OFFSET COMP  D AUTO ZERO  |  7 MATH    8 RECALL  9 STORE    | E    SRQ
TRIGGER
  E AUTO   F SINGLE    I DELAY        L NRDGS      |  4 R MEAN  5 R SDEV  6 R NSAMP  | −    ERROR
INPUT / SCANNER
  M TERM   N CHAN      O SCAN ADV     P DIGITS DISP|  1 NULL    2 dB      3 STAT     | BACK SPACE  CLEAR
READING MEMORY         STATE
  R CONFIG S RECALL    T STORE        ? RECALL     |  0 OFF     . CONT    , FILTER   | ENT  LAST ENTRY
```
(the shifted legend is shown before each key: menu letters A-T on the left block, math names after the digits)
Commands behind the legends: CONFIG = MEM, RECALL (memory) = RMEM, STORE = SSTATE,
RECALL (state) = RSTATE, SCAN ADV = SADV, DIGITS DISP = NDIG, AUTO CAL = ACAL,
OFFSET COMP = OCOMP, AUTO ZERO = AZERO.

PC keyboard: digits `. - , e`, Enter, BackSpace, Shift (= SHIFT key), ↑/↓ range,
←/→ scroll, Esc = LOCAL, F1-F6 = DCV ACV OHM DCI ACI FREQ (when no entry box has focus).

Like the real meter, the keys are **locked out in remote**. After any HP-IB command, press
LOCAL (or send `++loc`).

### Key matrix (row = IN1 bit, column = OUT1 bit)

Key code = ((row + 1) × 8 + col) × 2 + SHIFT; `DO_KEY` → `KEY_TABL` (`$FE03`, code − 16).

| row\col | 0 | 1 | 2 | 3 | 4 | 5 | 6 | 7 |
|---|---|---|---|---|---|---|---|---|
| 0 | ACI / ACDCI | FREQ / PER | ACV / ACDCV | DCV | — | — | OHM / OHMF | DCI |
| 1 | 2 / dB | TERM / M | NDIG / P | BACK SPACE / CLEAR | 1 / NULL | SADV / O | CHAN / N | 3 / STAT |
| 2 | LOCAL / ADRS | SHIFT | ▼ / MAN | ▲ / AUTO | — | — | ◄ / TEST | ► / RESET |
| 3 | 8 / RMATH | NPLC / A | AZERO / D | E / SRQ | 7 / MATH | OCOMP / C | ACAL / B | 9 / SMATH |
| 4 | 5 / SDEV | AUTO (trig) / E | NRDGS / L | − / ERROR | 4 / MEAN | DELAY / I | SGL / F | 6 / NSAMP |
| 5 | . / CONT | MEM / R | RSTATE / ? | ENTER / RECALL | 0 / OFF | SSTATE / T | RMEM / S | , / FILTER |

How each entry was identified:
- **Functions** `w_FB09` + table `$FB01` (ACV 6/7, ACI 14/15, plus FILTER); `w_FAB7` DCV,
  `w_FAE1` DCI, `w_FAC1`/`w_FAD1` OHM/OHMF, `w_FAF5`/`w_FAEB` FREQ/PER.
- **Digits** table `$F5D7` (`w_F5FD`).
- **Shifted digits** `w_FBAA` + math table `$FB3F` (codes from the `MATH` keyword table:
  0 OFF, 1 CONT, 4 DB, 6 FILTER, 9 NULL, 14 STAT); 4/5/6 → `w_FB74` shows MEAN/SDEV/NSAMP;
  7/8/9 type `MATH`, `RMATH`, `SMATH`; E → `SPBIT 4` (SRQ); − → error display (`w_FA05`).
- **Menu keys** `TYPE_KEY` (the command, then a space); shifted `DICT_KEY` (the letters).
- **▲/▼** `R_U`/`R_D` (in an entry: previous/next parameter); shifted `R_A`/`R_M`.
- **◄/►** scroll the entry, or show status (FRONT/REAR, OCOMP, FAST/SLOW…); shifted
  `STEST` (TEST), `RESET_KE`.
- **ENTER** `w_F862` runs the typed command; shifted `w_F818` recalls the last one.
- **BACK SPACE** `w_F4EB`; shifted `w_F5B9` cancels the entry.
- **,** `w_F5C3` types `,`. **AUTO (trig)** `w_FB1D`; **SGL** `w_FB2F`.
- **LOCAL** `KDISP` locally; in remote `KBD_SRVC` sends GTL (`w_9359`); shifted `w_FBDA`
  shows `ADDRESS=n`.
- **SHIFT + RSTATE** (`w_F8C0`) runs the displayed text as a command from the `X_MNEMON`
  vocabulary. In emulation it crashed (illegal opcode at `$38B0`). The panel shows a STOPPED
  message; Power cycle recovers. **Don't try it on a real meter until it's understood.**
- Positions r0c4, r0c5, r2c4, r2c5 do nothing (no key fitted, verify).

## Virtual input model (`tools/vinput.py`)

What the stand-in does with the signals, by the ROM's FUNCTION (bit 1 masked):

| FUNCTION | Quantity | Reply |
|---|---|---|
| 0 DCV | `dcv` | count = round(v × 10^(AD_MODE + 3 − RANGE)), 2 or 4 bytes |
| 4 ACV, 5 ACDCV | \|`acv`\|, √(dcv² + acv²) | same |
| 8 DCI, 12 ACI, 13 ACDCI | `dci`, \|`aci`\|, √(dci² + aci²) | same |
| 16 OHM, 17 OHMF | `ohm` (open = 1e15) | same |
| ≥ 20 FREQ/PER | \|`freq`\| | ticks (0.5 µs, 24 bit) + cycles (24 bit); the ROM inverts for PER |

The scaling and the byte formats are proven against the ROM in emulation
(docs/FINDINGS.md, "Reading protocol"). The rest are **assumptions** about the real 8051:
- full scale = 3 × 10^RANGE (1 A for the current ranges' RANGE 0); readable to **1.01 × FS**,
  beyond that the overload code ($7FFF.. / $8000..01);
- autorange (when `A_RANGE` `$0341` ≠ 0): up above 1.01 × FS, down when the value fits
  **90 %** of the next lower range; jumps straight to the right range; limits DCV/ACV
  −2..2, DCI −4..0, ACI −2..0, OHM 1..7 (3 GΩ is never chosen automatically);
- conversion time = NPLC / line frequency, ×2 with autozero on, + 1 ms (FREQ 105 ms);
- noise is gaussian, in ppm of the range's full scale (1σ), added before quantising.

Verified [emu] (`tests/test_vmeter.py`):
- 1.2345678 V shows `+1.234568 VDC`; it reads 1.23457 on the 30 V range and 1.235 at NPLC 0.0005;
- autorange steps to 12.3456 mV, −250 V and 123.45 µV;
- 400 V gives `OV.LD` / `+1E+38`;
- ACV, DCI, 4.7 kΩ and open-circuit ohms read correctly;
- FREQ 1234.5 Hz and PER 810.04 µs read correctly;
- front-panel DCI, NDIG 6 ENTER and LOCAL work;
- the GUI thread and the Prologix server share one meter.

## Library use
```python
from prologix_server import prepare_board
from vinput import VirtualInput
b = prepare_board(ROM, valid_cal=True)
b.inguard.source = VirtualInput(dcv=lambda t: 1 + 0.01 * t)   # a 10 mV/s ramp
b.inguard.realistic_timing = True
b.power_on(); b.run(seconds=8)
```
