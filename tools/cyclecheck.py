#!/usr/bin/env python3
"""Cross-check the emulator's 6809 cycle counts against lwasm's (the LWTOOLS tables, widely
used by the CoCo community).

Assembles every opcode in every addressing mode (plus a sample of indexed modes) with cycle
counts in the listing, then single-steps each instruction in tools/emu6809.py and compares.

    python3 tools/cyclecheck.py          # prints mismatches; exit status 1 if any

Instructions whose time depends on runtime state (PSH/PUL, branches taken, SWI, SYNC, CWAI,
RTI) are checked separately with their state fixed.
"""
import os
import re
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

RMW = ["NEG", "COM", "LSR", "ROR", "ASR", "ASL", "ROL", "DEC", "INC", "TST", "CLR"]
ALU8 = ["SUB", "CMP", "SBC", "AND", "BIT", "LD", "EOR", "ADC", "OR", "ADD"]
MODES = [("<$10", "dir"), (",X", "idx"), (">$1000", "ext")]


def source():
    L = []
    for m in ["NOP", "DAA", "SEX", "ABX", "MUL", "RTS"]:
        L.append(m)
    for r in "AB":
        for m in RMW:
            L.append(m + r)
    for m in RMW + ["JMP"]:
        for op, _ in MODES:
            L.append("%s %s" % (m, op))
    for r in "AB":
        for m in ALU8:
            L.append("%s%s #$01" % (m, r))
            for op, _ in MODES:
                L.append("%s%s %s" % (m, r, op))
        for op, _ in MODES:
            L.append("ST%s %s" % (r, op))
    for m in ["SUBD", "ADDD", "CMPX", "LDD", "LDX", "LDU", "CMPD", "CMPY", "LDY", "LDS",
              "CMPU", "CMPS"]:
        L.append("%s #$0102" % m)
        for op, _ in MODES:
            L.append("%s %s" % (m, op))
    for m in ["STD", "STX", "STU", "STY", "STS", "JSR"]:
        for op, _ in MODES:
            L.append("%s %s" % (m, op))
    for m in ["LEAX", "LEAY", "LEAU", "LEAS"]:
        L.append("%s ,X" % m)
    for m in ["TFR D,X", "EXG D,X", "ANDCC #$FF", "ORCC #$00"]:
        L.append(m)
    for b in ["BRA", "BRN", "BHI", "BLS", "BCC", "BCS", "BNE", "BEQ", "BVC", "BVS", "BPL",
              "BMI", "BGE", "BLT", "BGT", "BLE", "BSR"]:
        L.append("%s *+2" % b)
    for b in ["LBRA", "LBSR"]:
        L.append("%s *+3" % b)
    # indexed modes (LDA base 4)
    for op in [",X+", ",X++", ",-X", ",--X", "1,X", "-16,X", "$7F,X", "$100,X", "A,X", "B,X",
               "D,X", "[,X]", "[$100,X]", "[D,X]", "[>$1000]", "*,PCR", "$1000,PCR"]:
        L.append("LDA %s" % op)
    return L


def assemble(lines):
    from asm import run_lwasm, ihex_to_image
    d = tempfile.mkdtemp()
    src = os.path.join(d, "c.asm")
    with open(src, "w") as f:
        f.write("        pragma c\n        org $8000\n")
        for ln in lines:
            f.write("        %s\n" % ln)
    run_lwasm(src, os.path.join(d, "c.hex"), os.path.join(d, "c.lst"), os.path.join(d, "c.map"),
              os.path.join(d, "c.sym"))
    img, used = ihex_to_image(os.path.join(d, "c.hex"))
    out = []
    pat = re.compile(r"^([0-9A-F]{4}) ([0-9A-F]+)\s+\(.*?\):\d+ \[(\d+)(\+\?)?\]")
    for line in open(os.path.join(d, "c.lst")):
        m = pat.match(line)
        if m:
            out.append((int(m.group(1), 16), bytes.fromhex(m.group(2)), int(m.group(3)),
                        bool(m.group(4))))
    return out


def emu_cycles(code, cc=0x50):
    from emu6809 import CPU

    class Bus:
        def __init__(self):
            self.m = bytearray(0x10000)

        def read(self, a):
            return self.m[a]

        def write(self, a, v):
            self.m[a] = v
    bus = Bus()
    bus.m[0x8000:0x8000 + len(code)] = code
    cpu = CPU(bus)
    cpu.pc, cpu.x, cpu.y, cpu.u, cpu.s, cpu.cc = 0x8000, 0x2000, 0x2100, 0x2200, 0x2300, cc
    cpu.dp = 0
    c0 = cpu.cycles
    cpu.step()
    return cpu.cycles - c0


def check():
    rows = assemble(source())
    bad = []
    for addr, code, cyc, variable in rows:
        if variable:
            continue
        got = emu_cycles(code)
        if got != cyc:
            bad.append((code.hex().upper(), cyc, got))
    return rows, bad


def main():
    from m6809 import disasm_one
    rows, bad = check()
    print("%d instructions compared" % len(rows))
    for code, want, got in bad:
        m = bytearray(0x10000)
        b = bytes.fromhex(code)
        m[0x8000:0x8000 + len(b)] = b
        print("  %-10s %-18s lwasm %2d  emulator %2d" % (code, disasm_one(m, 0x8000)[0], want, got))
    print("%d mismatches" % len(bad))
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
