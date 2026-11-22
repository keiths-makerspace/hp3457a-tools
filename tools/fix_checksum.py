#!/usr/bin/env python3
"""Recompute the HP 3457A (03457-88810) ROM checksum for a patched image.

Algorithm (ROM w_CA41, called from PON_TEST via w_CA52 as `0 $2000 w_CA41`):
    D = 0; for X in $2000, $2002, ... $FFFE:  D = (D + word_be[X]) & $FFFF
    pass if D == 0, else AUXERR 1024 ("ROM checksum").
There is no stored "expected value": the image must sum to zero. This tool adjusts one
compensation word (default $FFF0, the 6809 *reserved* vector: never fetched by the CPU and
never read by the firmware - see docs/FINDINGS.md section 5.4).

Only file offsets $2000-$FFFF are covered; $0000-$1FFF is invisible to the CPU and ignored.

Usage:
  fix_checksum.py --check IMAGE.bin
  fix_checksum.py IN.bin OUT.bin [--at 0xFFF0] [--emu]
    --emu   also run the ROM's own checksum word on OUT.bin in the 6809 emulator
"""
import argparse
import os
import sys

LO = 0x2000
DEFAULT_AT = 0xFFF0


def word_sum(img, skip=None):
    s = 0
    for a in range(LO, 0x10000, 2):
        if a != skip:
            s += img[a] << 8 | img[a + 1]
    return s & 0xFFFF


def fix(img, at=DEFAULT_AT):
    if len(img) != 0x10000:
        raise ValueError(f"image must be 65536 bytes, got {len(img)}")
    if at < LO or at > 0xFFFE or (at - LO) % 2:
        raise ValueError("compensation word must be at an even address in $2000-$FFFE")
    out = bytearray(img)
    comp = (-word_sum(out, skip=at)) & 0xFFFF
    out[at], out[at + 1] = comp >> 8, comp & 0xFF
    assert word_sum(out) == 0
    return bytes(out), comp


def emu_check(path):
    """Run the ROM's own checksum routine (w_CA41) on the image; returns its result (0 = pass)."""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from forthcall import ForthMachine
    from decompile import Forth
    fm = ForthMachine(path, forth=Forth(path))
    return fm.call(0xCA41, 0x0000, 0x2000, max_cycles=5_000_000)[0]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("inp")
    ap.add_argument("out", nargs="?")
    ap.add_argument("--at", type=lambda s: int(s, 0), default=DEFAULT_AT)
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--emu", action="store_true")
    a = ap.parse_args()
    img = open(a.inp, "rb").read()
    if a.check or not a.out:
        s = word_sum(img)
        print(f"{a.inp}: word sum $2000-$FFFF = ${s:04X} -> {'PASS' if s == 0 else 'FAIL (AUXERR 1024)'}")
        if a.emu:
            print(f"ROM routine w_CA41 says: ${emu_check(a.inp):04X}")
        sys.exit(0 if s == 0 else 1)
    out_abs = os.path.abspath(a.out)
    fw_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "firmware"))
    if out_abs.startswith(fw_dir + os.sep) or out_abs == os.path.abspath(a.inp):
        sys.exit("refusing to write into firmware/ or over the input (write patched images to build/)")
    new, comp = fix(img, a.at)
    old = img[a.at] << 8 | img[a.at + 1]
    os.makedirs(os.path.dirname(out_abs) or ".", exist_ok=True)
    open(a.out, "wb").write(new)
    print(f"compensation word ${a.at:04X}: ${old:04X} -> ${comp:04X}; wrote {a.out} (sum = 0)")
    if a.emu:
        r = emu_check(a.out)
        print(f"ROM routine w_CA41 on {a.out}: ${r:04X} ({'PASS' if r == 0 else 'FAIL'})")
        sys.exit(0 if r == 0 else 1)


if __name__ == "__main__":
    main()
