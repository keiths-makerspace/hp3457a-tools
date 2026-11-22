# HP 3457A tools

Python tools for working on the HP 3457A 6½-digit DMM's main-controller firmware (MC6809,
written in Forth): a 6809 emulator that boots the real ROM, a virtual front panel, an emulated
HP-IB meter behind a Prologix-Ethernet interface, a Forth decompiler, ROM patching and
checksum tools, cal-RAM dump decoding, and a diagnostic ROM written in 6809 assembly.

> **No ROM included.** The firmware is HP/Keysight copyrighted. You need to supply your own
> dump of U602 (see [firmware/README.md](firmware/README.md), which has the reference hashes).
> Without it you can still build the diagnostic ROM in `diag/` and run it in the emulator and
> virtual meter; the tests that need the HP ROM are skipped.

Python 3, standard library only (`tkinter` for the front panel).

## Tools

| Tool | What |
|---|---|
| `tools/emu6809.py` | MC6809 CPU core |
| `tools/emu3457.py` | Main-controller board around the CPU: RAM, I/O, 8291A HP-IB, display, link to the analog 8051 ([docs/EMULATOR.md](docs/EMULATOR.md)) |
| `tools/vmeter.py` | Virtual meter: front panel + virtual input ([docs/VMETER.md](docs/VMETER.md)) |
| `tools/vinput.py`, `tools/faultpanel.py` | Virtual input signal model, fault injection |
| `tools/prologix_server.py` | Emulated meter behind a Prologix-Ethernet adapter on 127.0.0.1:1234 |
| `tools/display3457.py` | Display decoding (front-panel LCD frames) |
| `tools/m6809.py` | 6809 disassembler |
| `tools/rom_info.py` | Hashes, vectors, blank runs, strings of a ROM image |
| `tools/forth_dict.py`, `tools/decompile.py` | Forth dictionary scan and threaded-code decompiler |
| `tools/forthcall.py` | Run one ROM Forth word on the CPU core |
| `tools/forthpatch.py`, `tools/dispx_patch.py` | Build patched ROM images ([docs/DISPLAY.md](docs/DISPLAY.md)) |
| `tools/fix_checksum.py` | Fix the ROM checksum of a patched image |
| `tools/romcompare.py` | Compare a ROM read from a meter with a reference image |
| `tools/calimage.py` | Validate/decode a cal-RAM dump |
| `tools/asm.py`, `tools/lwmap.py`, `tools/cyclecheck.py` | Assemble with lwasm, labelled PC traces, cycle counts |
| `tools/coverage.py` | ROM code coverage under emulation |
| `diag/` | Diagnostic ROM source ([docs/DIAG_ROM.md](docs/DIAG_ROM.md)) |

## Quick start

Without the HP ROM (needs lwasm, see below):

```bash
python3 tools/asm.py diag/diag.asm build/diag.bin --checksum   # build the diagnostic ROM
python3 tools/vmeter.py --rom build/diag.bin                   # run it on the virtual front panel
```

With your own HP ROM dump in `firmware/`:

```bash
python3 tools/emu3457.py --seconds 8 --profile          # boot the ROM in the emulator
python3 tools/vmeter.py --dcv 1.2345 --prologix         # virtual meter with front panel
python3 tools/decompile.py PEEK                         # decompile a Forth word by name
python3 -m unittest discover -s tests                   # tests (test_diag alone can take ~20 min)
```

The diagnostic ROM needs [LWTOOLS](http://www.lwtools.ca/) (`lwasm`, tested with 4.25);
`tools/asm.py` calls it natively if on PATH, otherwise through WSL (distro from `$HP3457_WSL_DISTRO`). Tests that need it are skipped if it's missing.

## Safety

Anything that writes to a real meter (POKE, SP_POKE, SP_CALL, CAL, cal-RAM writes) can destroy
the calibration constants, which can only be restored by a full recalibration. Test in the
emulator first, and back up your cal RAM before touching hardware.

Some docs refer to notes (FINDINGS.md, CALIBRATION.md, FREE_SPACE.md) kept in a separate
private research repo.

## Licence

MIT, see [LICENSE](LICENSE). The licence covers this repo's code and docs only, not HP's firmware.

Not affiliated with HP, Agilent or Keysight.
