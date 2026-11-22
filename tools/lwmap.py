#!/usr/bin/env python3
"""lwasm symbols for emulator traces: turn $E1AC into 'isend' and $E1B3 into 'isend+7'.

    python3 tools/lwmap.py build/diag.sym E1AC E2F8      # look addresses up
    python3 tools/lwmap.py build/diag.sym --trace build/diag.bin 0.5
                                                         # boot it, print where the CPU ran

Library:
    syms = Symbols("build/diag.sym")          # .sym (--symbol-dump) or .map (--map)
    syms.name(0xE1B3)  -> 'isend+7'
    tr = Tracer(board, syms, depth=64)         # keeps the last PCs; tr.dump() prints them
"""
import bisect
import collections
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


class Symbols:
    def __init__(self, path=None, code_lo=0x2000):
        self.by_name = {}
        self.code_lo = code_lo
        if path:
            self.load(path)

    def load(self, path):
        pat_sym = re.compile(r"^\s*([A-Za-z_.][\w.?@$]*)\s+EQU\s+\$?([0-9A-Fa-f]+)", re.I)
        pat_map = re.compile(r"^Symbol:\s+(\S+)\s+\(.*\)\s+=\s+([0-9A-Fa-f]+)")
        for line in open(path, encoding="utf-8", errors="replace"):
            m = pat_sym.match(line) or pat_map.match(line)
            if m:
                self.by_name[m.group(1)] = int(m.group(2), 16)
        self._index()
        return self

    def _index(self):
        code = sorted((a, n) for n, a in self.by_name.items() if a >= self.code_lo)
        self._addrs = [a for a, _ in code]
        self._names = [n for _, n in code]

    def __getitem__(self, name):
        return self.by_name[name]

    def name(self, addr):
        """Nearest code label at or below addr, as 'label' or 'label+n'; hex if none."""
        i = bisect.bisect_right(self._addrs, addr) - 1
        if i < 0 or addr < self.code_lo:
            return "$%04X" % addr
        off = addr - self._addrs[i]
        return self._names[i] if off == 0 else "%s+%d" % (self._names[i], off)


class Tracer:
    """Records the last `depth` instruction addresses of a running emu3457.Board."""

    def __init__(self, board, syms, depth=64):
        self.syms = syms
        self.ring = collections.deque(maxlen=depth)
        board.cpu.trace = lambda cpu, pc: self.ring.append((cpu.cycles, pc))

    def dump(self, n=None, out=print):
        items = list(self.ring)[-(n or len(self.ring)):]
        for cyc, pc in items:
            out("%12d  $%04X  %s" % (cyc, pc, self.syms.name(pc)))

    def routines(self):
        """Counts per label (without offsets) over the recorded window."""
        c = collections.Counter(self.syms.name(pc).split("+")[0] for _, pc in self.ring)
        return c.most_common()


def main():
    import argparse
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("symfile")
    ap.add_argument("addrs", nargs="*")
    ap.add_argument("--trace", nargs=2, metavar=("ROM", "SECONDS"))
    a = ap.parse_args()
    syms = Symbols(a.symfile)
    for s in a.addrs:
        print(s, "->", syms.name(int(s, 16)))
    if a.trace:
        from emu3457 import Board
        b = Board(a.trace[0])
        tr = Tracer(b, syms, depth=40)
        b.power_on()
        b.run(seconds=float(a.trace[1]))
        tr.dump()
        print("resets:", b.resets)


if __name__ == "__main__":
    main()
