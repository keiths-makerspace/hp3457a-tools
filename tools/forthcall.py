#!/usr/bin/env python3
"""Run individual ROM Forth words on the 6809 core (layer-1 harness).

    from forthcall import ForthMachine
    fm = ForthMachine()                 # loads the ROM into a FlatBus
    fm.call("+", 2, 3)                  # -> [5]   (data stack after the call, bottom..top)
    fm.call(0xCC01)                     # xt or name (names resolved via decompile.Forth)

A two-cell thread [xt][TRAP] is built in scratch RAM; execution starts at NEXT with Y
pointing at it. TRAP is a fake code word whose code address is a breakpoint, so the call
returns as soon as the word EXITs. The data stack (U) and return stack (S) live in scratch
RAM above the ROM's own stack areas so the firmware's RAM variables stay untouched.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from emu6809 import CPU, FlatBus  # noqa: E402
from decompile import Forth, DEFAULT_ROM, NEXT  # noqa: E402

THREAD = 0x1F00      # [xt][TRAP]
TRAP = 0x1F04        # code field -> TRAP_CODE
TRAP_CODE = 0x1F06
U_TOP = 0x1E80       # data stack grows down from here
S_TOP = 0x1EF0       # return stack


class WordFault(Exception):
    pass


class ForthMachine:
    def __init__(self, rom_path=DEFAULT_ROM, bus=None, forth=None):
        self.forth = forth or Forth(rom_path)
        if bus is None:
            bus = FlatBus(open(rom_path, "rb").read())
            bus.mem[0:0x2000] = bytes(0x2000)   # 0000-1FFF is I/O+RAM on the board, not ROM
        self.bus = bus
        self.cpu = CPU(bus)

    def xt(self, word):
        if isinstance(word, int):
            return word
        hits = self.forth.resolve(word)
        if len(hits) != 1:
            raise KeyError(f"{word!r} resolves to {[hex(h) for h in hits]}")
        return hits[0]

    def poke16(self, a, v):
        self.bus.write(a, (v >> 8) & 0xFF)
        self.bus.write(a + 1, v & 0xFF)

    def peek16(self, a):
        return self.bus.read(a) << 8 | self.bus.read(a + 1)

    def call(self, word, *args, max_cycles=5_000_000, signed=False):
        cpu = self.cpu
        xt = self.xt(word)
        self.poke16(THREAD, xt)
        self.poke16(THREAD + 2, TRAP)
        self.poke16(TRAP, TRAP_CODE)
        u = U_TOP
        for v in args:                         # args given bottom..top
            u -= 2
            self.poke16(u, v & 0xFFFF)
        cpu.u = u
        cpu.s = S_TOP
        cpu.y = THREAD
        cpu.pc = NEXT
        cpu.cc |= 0x50                         # interrupts masked for unit calls
        cpu.breaks = {TRAP_CODE}
        why = cpu.run(max_cycles=max_cycles)
        if why != "break":
            raise WordFault(f"{word} did not return within {max_cycles} cycles: {cpu.state()}")
        if cpu.s != S_TOP:
            raise WordFault(f"{word}: return stack unbalanced (S={cpu.s:04X})")
        out = []
        a = U_TOP - 2
        if cpu.u > U_TOP:
            raise WordFault(f"{word}: data stack underflow (U={cpu.u:04X})")
        while a >= cpu.u:
            v = self.peek16(a)
            out.append(v - 0x10000 if signed and v & 0x8000 else v)
            a -= 2
        return out


if __name__ == "__main__":
    fm = ForthMachine()
    word = sys.argv[1]
    args = [int(x, 0) for x in sys.argv[2:]]
    print([hex(v) for v in fm.call(word, *args)], f"cycles={fm.cpu.cycles}")
