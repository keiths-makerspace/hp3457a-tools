#!/usr/bin/env python3
"""Dynamic ROM coverage: boot the real ROM in the emulator, drive a broad workload (all
commands, keys, CAL/ACAL/TEST, error paths) and record every ROM byte the CPU reads
(instruction fetches and data reads, $2000-$FFFF).

  python3 tools/coverage.py [--out analysis/coverage.bin] [--quick]

Output: a 64 KB map (1 = byte was read) plus a summary of untouched ranges.
Coverage only proves that something IS used; an untouched byte may still be used by a path
the workload missed. Combine with the static reference scan (docs/FREE_SPACE.md).
"""
import argparse
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from emu3457 import Board  # noqa: E402
from calimage import records, checksum  # noqa: E402

COMMANDS = """
ID?;REV?;ERR?;AUXERR?;STB?;OPT?;LINE?;CALNUM?;MSIZE?;CHAN?;TERM?;CIIL?;LFREQ?
DCV 3;DCV 0.03;DCV 300;ACV 3;ACDCV 30;OHM 3000;OHM 3E9;OHMF 300;DCI 0.03;ACI 1;ACDCI 0.3
FREQ 3;PER 30;FUNC DCV,3;FUNC OHM,30000;RANGE AUTO;RANGE 3;ARANGE ON;ARANGE OFF;ARANGE ONCE
NPLC 10;NPLC 0.0005;NPLC 1;NDIG 4;NDIG 6;NDIG 7;AZERO OFF;AZERO ONCE;AZERO ON
OCOMP ON;OCOMP OFF;FIXEDZ ON;FIXEDZ OFF;ACBAND 20,2000;FSOURCE ACV;FSOURCE ACDCV;LFREQ LINE;LFREQ 50
DELAY 0.1;DELAY -1;TIMER 1;NRDGS 5,AUTO;NRDGS 1,TIMER;TARM AUTO;TARM HOLD;TARM SGL;TRIG SGL;TRIG AUTO
TRIG EXT;TRIG HOLD;T;BEEP OFF;BEEP ON;BEEP ONCE;TBUFF ON;TBUFF OFF;INBUF ON;INBUF OFF
TRIG?;RANGE?;TIMER?;DELAY?;TARM?;NRDGS?;NPLC?;MATH?;MCOUNT?;FIXEDZ?;OCOMP?;AZERO?
MSIZE 1000;MEM FIFO;TRIG SGL;TRIG SGL;MEM LIFO;RMEM 1;RMEM 1,2;MEM OFF;MCOUNT?
MFORMAT SINT;MFORMAT DINT;MFORMAT SREAL;MFORMAT ASCII;OFORMAT SINT;OFORMAT DINT;OFORMAT SREAL;OFORMAT ASCII
MATH NULL;MATH SCALE;MATH STAT;MATH PFAIL;MATH DB;MATH DBM;MATH CTHRM;MATH FTHRM;MATH FILTER
MATH LIMIT;MATH PERC;MATH CTHRM,STAT;MATH OFF;SMATH OFFSET,1.5;SMATH SCALE,2;SMATH UPPER,5
SMATH LOWER,-5;SMATH PERC,3;SMATH DEGREE,20;SMATH REF,600;SMATH RES,50;SMATH NSAMP,10
RMATH MEAN;RMATH SDEV;RMATH NSAMP;RMATH UPPER;RMATH LOWER;RMATH HIRES;RMATH OFFSET
DISP OFF;DISP ON;DISP 2,"COVERAGE";DISP 1;DISP MSG,"A";SSTATE 1;RSTATE 1;SSTATE 3;RSTATE 3
EMASK 2047;RQS 4;RQS 0;CSB;SRQ;END ALWAYS;END OFF;END ON
OPEN 1;CLOSE 1;CRESET;SADV AUTO;SADV HOLD;SLIST 1,2,3;SLIST?;CHAN 1;CHAN 2,5;TERM REAR;TERM FRONT
SUB 1;DCV 3;TRIG SGL;SUBEND;CALL 1;PAUSE;SCRATCH;LOCK ON;LOCK OFF;TONE 1000,20
PEEK 25363;PEEK 32768;POKE 4096,165;SP_PEEK 3;SP_POKE 4,5;SP_CALL 0;_CAL#! 1;_CAL#! 0
SECURE 0,0;SECURE 1,2;FOO;DCV 3,4,5,6;NPLC 99999;RANGE -3;DISP 9;OHM X
ISCALE?;ISCALE?;PRESET;RESET;TRIG HOLD
""".replace("\n", ";").split(";")


def make_valid(mem, seed=7):
    rnd = random.Random(seed)
    for _, a, size in records():
        rec = bytearray(rnd.randrange(256) for _ in range(size - 1)) + b"\0"
        rec[-1] = checksum(rec)
        mem[a:a + size] = rec
    g = bytes(rnd.randrange(256) for _ in range(4))
    mem[0x18B:0x18F] = g
    mem[0x18F:0x193] = bytes(x ^ 0xFF for x in g)
    mem[0x193], mem[0x194] = 0x05, 0xFA
    mem[0x050C:0x0523] = ACAL_BLOCK


# The ACAL constants as the emulated ACAL ALL leaves them (docs/FINDINGS.md "ACAL"): AC offset /
# AC converter readings $050C-$0515 and their checksum $0516, the ohms precharge $0517 with its
# complement $0519, the gain factor 1/1e8 (float $322BCC77) at $051B with its complement at $051F.
# Without them every measurement set-up raises ERR 1024 (ACAL REQ'D), as on a meter whose ACAL
# constants are lost.
ACAL_BLOCK = bytes.fromhex("01f401f4000001f401f42b01f4fe0b322bcc77cdd43388")


def workload(b, quick=False, log=print):
    b.power_on()
    b.run(seconds=7)
    b.hpib_write("TRIG HOLD\r\n")
    b.hpib_read(seconds=0.3)
    for c in [c.strip() for c in COMMANDS if c.strip()]:
        b.hpib_write(c + "\r\n", seconds=1.5)
        b.run(seconds=0.4)
        if c.endswith("?") or c.startswith("PEEK") or c.startswith("SP_PEEK") or c == "T":
            b.hpib_read(seconds=0.5)
    log("commands done")
    if not quick:
        for cmd, secs in (("ACAL ALL", 40), ("DCV 3", 1), ("CAL 0,0", 8), ("TEST", 15)):
            b.hpib_write(cmd + "\r\n", seconds=1.5)
            b.run(seconds=secs)
            b.hpib_write("TRIG HOLD\r\n")
            b.hpib_read(seconds=0.3)
        log("ACAL/CAL/TEST done")
    # front panel: every key, plain and shifted (SHIFT key position found by trying all)
    for row in range(6):
        for col in range(8):
            b.press_key(row, col, seconds=0.12)
            b.run(seconds=0.3)
            for shift in ((0, 0),):
                pass
    for row in range(6):
        for col in range(8):
            b.press_key(row, col, seconds=0.12)
            b.press_key(row, (col + 3) % 8, seconds=0.12)
            b.run(seconds=0.2)
    log("keys done")
    b.inguard.counts = 0x7FFF                      # overload path
    b.hpib_write("TRIG AUTO\r\n")
    b.run(seconds=2)
    b.inguard.counts = 0
    b.power_cycle(1.0, out1_powerup=0)             # warm power-up with valid NV RAM
    b.run(seconds=8)
    log("power cycle done")


def ranges(cov, lo=0x2000, hi=0x10000, val=0, minlen=1):
    out, a = [], lo
    while a < hi:
        if cov[a] == val:
            b = a
            while b < hi and cov[b] == val:
                b += 1
            if b - a >= minlen:
                out.append((a, b - 1))
            a = b
        else:
            a += 1
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(os.path.dirname(__file__), "..", "analysis", "coverage.bin"))
    ap.add_argument("--quick", action="store_true")
    a = ap.parse_args()
    b = Board()
    make_valid(b.mem)
    b.coverage = bytearray(0x10000)
    b.ram_writes = bytearray(0x2000)
    workload(b, quick=a.quick)
    open(a.out, "wb").write(bytes(b.coverage))
    open(a.out.replace(".bin", "_ramw.bin"), "wb").write(bytes(b.ram_writes))
    tot = sum(b.coverage[0x2000:])
    print(f"ROM bytes read: {tot} of {0xE000} ({100 * tot / 0xE000:.1f}%)")
    for lo, hi in ((0x2000, 0x3050), (0x3051, 0x385E), (0x385F, 0x5FFF), (0x6000, 0x7FFF), (0x8000, 0xFFFF)):
        n = sum(b.coverage[lo:hi + 1])
        print(f"  ${lo:04X}-${hi:04X}: {n} bytes read")
    print("AUX_ERR", hex(b.aux_err()), "resets", len(b.resets))


if __name__ == "__main__":
    main()
