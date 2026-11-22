#!/usr/bin/env python3
"""Quick facts about a 3457A main-controller ROM image: hashes, 6809 vectors,
blank runs, simple sums, and printable strings.

Usage: rom_info.py ROM.bin [--strings N]   (N = min string length, default 0 = off)
"""
import argparse
import hashlib
import re

VECTORS = ["reserved", "SWI3", "SWI2", "FIRQ", "IRQ", "SWI", "NMI", "RESET"]


def runs(d, val, minlen=64):
    out, i, n = [], 0, len(d)
    while i < n:
        if d[i] == val:
            j = i
            while j < n and d[j] == val:
                j += 1
            if j - i >= minlen:
                out.append((i, j - i))
            i = j
        else:
            i += 1
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("rom")
    ap.add_argument("--strings", type=int, default=0)
    a = ap.parse_args()
    d = open(a.rom, "rb").read()
    print(f"size   {len(d)} bytes (0x{len(d):X})")
    print(f"md5    {hashlib.md5(d).hexdigest()}")
    print(f"sha256 {hashlib.sha256(d).hexdigest()}")
    v = d[-16:]
    for k, nm in enumerate(VECTORS):
        print(f"vector {nm:8s} 0x{v[2*k] << 8 | v[2*k+1]:04X}")
    for val in (0xFF, 0x00):
        r = runs(d, val)
        print(f"runs of 0x{val:02X} >=64: " + ", ".join(f"0x{s:04X}+{l}" for s, l in r))
    print(f"sum8 whole   0x{sum(d) & 0xFF:02X}   sum16 whole   0x{sum(d) & 0xFFFF:04X}")
    m = d[0x2000:]
    print(f"sum8 mapped  0x{sum(m) & 0xFF:02X}   sum16 mapped  0x{sum(m) & 0xFFFF:04X}  (0x2000-0xFFFF)")
    if a.strings:
        for mt in re.finditer(rb"[\x20-\x7e]{%d,}" % a.strings, d):
            print(f"0x{mt.start():04X}  {mt.group().decode()}")


if __name__ == "__main__":
    main()
