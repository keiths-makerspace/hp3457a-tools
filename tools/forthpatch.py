#!/usr/bin/env python3
"""Build patched HP 3457A ROM images: new threaded words and HP-IB commands in free space.

Never touches firmware/*.bin; save() refuses paths inside firmware/ and always fixes the
ROM checksum (compensation word $FFF0, see tools/fix_checksum.py).

    from forthpatch import Patch
    p = Patch()                                   # loads the stock ROM
    xt = p.colon("MYWORD", ["DUP", "+"])          # headerless word in free space
    p.command("HELLO?", [12345, "TOS>CUST", "DONE_CMD~9"])   # new HP-IB command
    p.save("build/hello.bin")

Token forms in bodies: "NAME" (resolved via decompile.Forth, NAME@XXXX allowed),
int (compiled as LIT n), ("xt", addr) raw cell, ("c", "string") inline (") string,
("label", "L") / ("branch", "L") / ("0branch", "L") for control flow.
Commands are linked into the HP-IB vocabulary by re-pointing X_MNEMONIC ($804E), the chain
head that the parser (w_B580) passes to (FIND).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from decompile import Forth, DEFAULT_ROM  # noqa: E402
from fix_checksum import fix  # noqa: E402

DOCOL, LIT, EXIT = 0x6313, 0x608D, 0x631F
BRANCH, ZBRANCH, STRLIT = 0x6040, 0x602C, 0x8066
X_MNEMONIC = 0x804E                 # ROM cell holding the newest command header
FREE = (0x3051, 0x385E)             # zero block (docs/FREE_SPACE.md)
PROT_TRAMPOLINE = 0x8E41            # existing thread fragment "w_8B7B EXIT" (inside w_8E29)


class Patch:
    def __init__(self, rom_path=DEFAULT_ROM, region=FREE):
        self.img = bytearray(open(rom_path, "rb").read())
        self.forth = Forth(rom_path)
        self.lo, self.hi = region
        self.here = self.lo
        self.new = {}               # name -> xt for words added by this patch
        for a in range(self.lo, self.hi + 1):
            if self.img[a] != 0:
                raise ValueError(f"free region not blank at ${a:04X}")

    # -- low level -----------------------------------------------------------------
    def _emit(self, data):
        a = self.here
        if a + len(data) - 1 > self.hi:
            raise ValueError("free region exhausted")
        self.img[a:a + len(data)] = data
        self.here += len(data)
        return a

    def cell(self, v):
        return self._emit(bytes([(v >> 8) & 0xFF, v & 0xFF]))

    def xt(self, name):
        if name in self.new:
            return self.new[name]
        hits = self.forth.resolve(name)
        if len(hits) != 1:
            raise KeyError(f"{name!r} -> {[hex(h) for h in hits]}")
        return hits[0]

    def _body(self, tokens):
        labels, fixups = {}, []
        for t in tokens:
            if isinstance(t, int):
                self.cell(LIT)
                self.cell(t & 0xFFFF)
            elif isinstance(t, str):
                self.cell(self.xt(t))
            elif t[0] == "xt":
                self.cell(t[1])
            elif t[0] == "c":
                s = t[1].encode("ascii")
                self.cell(STRLIT)
                self._emit(bytes([len(s)]) + s)
            elif t[0] == "label":
                labels[t[1]] = self.here
            elif t[0] in ("branch", "0branch"):
                self.cell(BRANCH if t[0] == "branch" else ZBRANCH)
                fixups.append((self.here, t[1]))
                self.cell(0)
            else:
                raise ValueError(t)
        for at, lab in fixups:               # offset is relative to the offset cell
            off = (labels[lab] - at) & 0xFFFF
            self.img[at], self.img[at + 1] = off >> 8, off & 0xFF
        self.cell(EXIT)

    # -- words and commands ----------------------------------------------------------
    def colon(self, name, tokens):
        xt = self.cell(DOCOL)
        self.new[name] = xt
        self._body(tokens)
        return xt

    def code(self, name, build):
        """Native (CODE) word: code field = xt+2. `build(addr)` returns the machine code for
        an origin of `addr`, so it can compute absolute branch targets. End with JMP NEXT."""
        xt = self.here
        body = bytes(build(xt + 2))
        self.cell(xt + 2)
        self._emit(body)
        self.new[name] = xt
        return xt

    def repoint(self, at, expect, new):
        """Replace one threaded cell in the stock code (a hook), checking its current value."""
        old = self.img[at] << 8 | self.img[at + 1]
        want = self.xt(expect) if isinstance(expect, str) else expect
        if old != want:
            raise ValueError(f"cell ${at:04X} holds ${old:04X}, expected ${want:04X}")
        v = self.xt(new) if isinstance(new, str) else new
        self.img[at], self.img[at + 1] = v >> 8, v & 0xFF
        self.hooks = getattr(self, "hooks", []) + [(at, old, v)]

    def command(self, name, tokens):
        """In-place header [len][name, last|0x80][link][DOCOL] body, linked into the vocab."""
        nm = name.encode("ascii")
        link = self.img[X_MNEMONIC] << 8 | self.img[X_MNEMONIC + 1]
        hdr = self._emit(bytes([len(nm)]) + nm[:-1] + bytes([nm[-1] | 0x80]))
        self.cell(link)
        xt = self.colon(name, tokens)
        self.img[X_MNEMONIC], self.img[X_MNEMONIC + 1] = hdr >> 8, hdr & 0xFF
        return hdr, xt

    def prot_write_word(self):
        """( value addr ... n -- ) protected write of n byte pairs through HP's w_8B7B.
        Returns into the ROM fragment at $8E41 so the IP saved across the watchdog reset is
        >= $8001 (required by the reset-vector resume checks)."""
        if "PROT-WRITE" not in self.new:
            self.colon("PROT-WRITE", [PROT_TRAMPOLINE, ">R"])
        return self.new["PROT-WRITE"]

    # -- output ----------------------------------------------------------------------
    def save(self, path):
        out = os.path.abspath(path)
        fw = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "firmware"))
        if out.startswith(fw + os.sep):
            raise ValueError("refusing to write into firmware/")
        img, _ = fix(bytes(self.img))
        os.makedirs(os.path.dirname(out), exist_ok=True)
        open(out, "wb").write(img)
        return out

    def used(self):
        return self.here - self.lo
