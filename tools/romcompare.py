#!/usr/bin/env python3
"""Compare a ROM read from a meter with the reference image this project is built on
(03457-88810, U602 of the 03457-66511 main controller), before trusting anything that depends
on it, such as the diagnostic EPROM's assumptions or the patches.

    python3 tools/romcompare.py dump.bin                 # against firmware/.../03457-88810_U602.bin
    python3 tools/romcompare.py dump.bin --ref other.bin
    python3 tools/romcompare.py dump.bin --max 40        # list at most 40 differing ranges

Accepts a 64 KB image (27512, file offset = CPU address) or a 32 KB image (27256, assumed to be
the top half, $8000-$FFFF). Other sizes are compared from offset 0 and reported as such. Only
$2000-$FFFF is visible to the CPU, so differences below $2000 (development leftovers in our
image) are listed separately and don't count.

Reports: identical or not, the ROM's own word-sum check (it must be 0), the reset vector, and
each differing range with the Forth word it falls in (names from tools/decompile.py on the
reference). Read-only: nothing here talks to a meter.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from decompile import Forth, DEFAULT_ROM  # noqa: E402

VISIBLE = 0x2000


def load(path):
    data = open(path, "rb").read()
    if len(data) == 0x10000:
        return data, "64 KB (27512): offset = address"
    if len(data) == 0x8000:
        return bytes(0x8000) + data, "32 KB (27256): placed at $8000-$FFFF (verify)"
    return data + bytes(max(0, 0x10000 - len(data))), "%d bytes: compared from 0 (unusual size)" % len(data)


def word_sum(img, lo=VISIBLE):
    return sum(img[a] << 8 | img[a + 1] for a in range(lo, 0x10000, 2)) & 0xFFFF


def ranges(a, b, lo, hi):
    out, start = [], None
    for x in range(lo, hi):
        if a[x] != b[x]:
            if start is None:
                start = x
        elif start is not None:
            out.append((start, x - 1))
            start = None
    if start is not None:
        out.append((start, hi - 1))
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("dump")
    ap.add_argument("--ref", default=DEFAULT_ROM)
    ap.add_argument("--max", type=int, default=25)
    a = ap.parse_args()
    ref, _ = load(a.ref)
    img, how = load(a.dump)
    f = Forth(a.ref)
    print("dump:      %s (%s)" % (a.dump, how))
    print("reference: %s" % a.ref)
    vec = lambda m, v: m[v] << 8 | m[v + 1]
    for name, m in (("dump", img), ("reference", ref)):
        print("%-10s word sum $2000-$FFFF = $%04X (%s), RESET vector $%04X" % (
            name + ":", word_sum(m), "PASS" if word_sum(m) == 0 else "FAIL", vec(m, 0xFFFE)))
    vis = ranges(img, ref, VISIBLE, 0x10000)
    low = ranges(img, ref, 0, VISIBLE)
    nvis = sum(e - s + 1 for s, e in vis)
    if not vis:
        print("\nIDENTICAL in the CPU-visible area $2000-$FFFF.")
    else:
        print("\n%d bytes differ in $2000-$FFFF, in %d ranges:" % (nvis, len(vis)))
        for s, e in vis[:a.max]:
            owner = f._owner(s) or "?"
            print("  $%04X-$%04X  %4d bytes  in/after %s" % (s, e, e - s + 1, owner))
        if len(vis) > a.max:
            print("  ... %d more ranges" % (len(vis) - a.max))
        print("\nThe diagnostic ROM and the patches were built against the reference. Review the "
              "differences before using either on this board.")
    if low:
        print("\n(%d bytes differ below $2000, not visible to the CPU: ignored.)" %
              sum(e - s + 1 for s, e in low))
    sys.exit(0 if not vis else 1)


if __name__ == "__main__":
    main()
