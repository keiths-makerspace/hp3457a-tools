#!/usr/bin/env python3
"""Scan an HP 3457A main-controller ROM image for Forth dictionary headers.

Header layout observed in 03457-88810 (fig-Forth style, 6809, big-endian):
    [len] [name chars ... last char | 0x80] [link hi lo] [CFA hi lo] ...
  - len: low 5 bits = name length (bit 6/7 may carry IMMEDIATE/SMUDGE flags)
  - link: address of the previous word's header (0 at the end of a chain)
  - CFA: code field; for colon words it points at the inner interpreter
The image is mapped 1:1 (file offset == CPU address); only 0x2000-0xFFFF is
visible to the CPU on the 03457-66511 board.

Usage: forth_dict.py ROM.bin [--csv out.csv]
Heuristic scanner: confirm entries by following link chains (see --chains).
"""
import argparse
import csv
import sys


def scan(d, lo=0x0000, hi=None):
    hi = hi or len(d)
    out = []
    i = lo
    while i < hi - 6:
        b = d[i]
        L = b & 0x1F
        if 1 <= L <= 31 and not (b & 0x80 and L == 0):
            s = d[i + 1:i + 1 + L]
            if (len(s) == L and all(0x21 <= c < 0x7F for c in s[:-1])
                    and (s[-1] & 0x80) and 0x21 <= (s[-1] & 0x7F) < 0x7F):
                name = (s[:-1] + bytes([s[-1] & 0x7F])).decode()
                p = i + 1 + L
                link = d[p] << 8 | d[p + 1]
                cfa = d[p + 2] << 8 | d[p + 3]
                out.append({"hdr": i, "flags": b & 0xE0, "name": name,
                            "link": link, "cfa": cfa, "pfa": p + 4})
                i = p
                continue
        i += 1
    return out


def chains(words):
    """Return words whose link points at another detected header (or 0)."""
    hdrs = {w["hdr"] for w in words}
    return [w for w in words if w["link"] == 0 or w["link"] in hdrs]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("rom")
    ap.add_argument("--csv")
    ap.add_argument("--chains", action="store_true",
                    help="only print words whose link field hits another header")
    ap.add_argument("--mapped-only", action="store_true",
                    help="ignore 0x0000-0x1FFF (hidden behind RAM/I-O)")
    a = ap.parse_args()
    d = open(a.rom, "rb").read()
    words = scan(d, 0x2000 if a.mapped_only else 0)
    if a.chains:
        words = chains(words)
    if a.csv:
        with open(a.csv, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["hdr", "flags", "name", "link", "cfa", "pfa"])
            w.writeheader()
            for x in words:
                w.writerow({k: (f"0x{v:04X}" if isinstance(v, int) else v) for k, v in x.items()})
    for x in words:
        print(f"{x['hdr']:04X}  flags={x['flags']:02X}  link={x['link']:04X}  "
              f"cfa={x['cfa']:04X}  {x['name']}")
    print(f"# {len(words)} headers", file=sys.stderr)


if __name__ == "__main__":
    main()
