#!/usr/bin/env python3
"""Forth decompiler for the HP 3457A main-controller ROM (03457-88810, MC6809).

Threading model (verified by disassembly, see docs/FINDINGS.md section 5.1):
  indirect-threaded, 16-bit big-endian cells
  Y = IP, X = W (CFA of the word being executed), U = data stack, S = return stack
  NEXT   6317:  LDX ,Y++ / JMP [,X]
  DOCOL  6313:  PSHS Y / LEAY 2,X / (NEXT)
  EXIT   631F:  LDY ,S++ / (NEXT)
  An execution token (xt) is the address of a 2-byte code field. Code (primitive)
  words have code field == xt+2.

Usage:
  decompile.py NAME|ADDR [...]       decompile word(s) (ADDR = hex CFA, e.g. 0xCC01 or CC01)
  decompile.py --list                 print the symbol table
  decompile.py --vocab                list the command vocabulary (chain from TAIL 0xDFB1)
  decompile.py --xref NAME|ADDR       list words whose threaded body references the word
  decompile.py --all                  decompile everything (named words + all DOCOL cells)
  options: --rom PATH, --no-code (skip native disassembly), --raw (show raw cells)

Library: `from decompile import Forth; f = Forth(); f.decompile(xt)`
"""
import argparse
import collections
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from m6809 import disasm_one  # noqa: E402

DEFAULT_ROM = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "firmware",
                           "main-controller-66511", "03457-88810_U602.bin")

# --- inner interpreter / runtime code addresses (proven by disassembly) -------------
NEXT = 0x6317
DOCOL = 0x6313      # colon definitions
DOCON = 0x6413      # CONSTANT child: push cell at PFA       (CONSTANT = CREATE , (;CODE))
DOCON2 = 0x6619     # same code as DOCON; used for RAM-address "variables"  (verify name)
DOCREATE = 0x6612   # CREATE / VARIABLE child: push PFA
DODOES = 0x6486     # DOES> runtime: push PFA, IP := address after the stub
PSEMICODE = 0x1FFC  # (;CODE) - lives in the dev system (hidden), only seen inside defining words
RUNTIMES = {DOCOL: "DOCOL", DOCON: "DOCON", DOCON2: "DOCON2", DOCREATE: "DOCREATE"}

# --- tokens with inline operands ----------------------------------------------------
LIT, CLIT, FLIT = 0x608D, 0x6054, 0x607E
BRANCH, ZBRANCH, PLOOP, PPLOOP = 0x6040, 0x602C, 0x6003, 0x5F80
STRLIT = 0x8066
EXIT = 0x631F
REL_BRANCHES = {BRANCH, ZBRANCH, PLOOP, PPLOOP}

# Names for headerless words, identified by hand from their code/bodies.
# Tag (verify) means the name is inferred, not taken from a ROM header.
MANUAL_NAMES = {
    0x6040: "BRANCH",            # code = 0BRANCH tail: Y := Y + [Y]
    0x6054: "CLIT",              # LDB ,Y+ / CLRA / PSHU D
    0x607E: "2LIT",              # LDD ,Y++ / LDX ,Y++ / PSHU D,X  (4-byte literal, float?) (verify)
    0x6052: "2-'",               # alias: code field -> 2- code (62F5) (verify)
    0x6570: "R@",                # alias: code field -> I code (64EB)
    0x63FE: "WMOVE",             # like CMOVE but count*2 (named MOVE in head table)
    0x64F2: "J",                 # LDD 4,S
    0x64FB: "LEAVE",             # LDD ,S / STD 2,S
    0x665A: "K",                 # LDD 8,S (verify)
    0x664F: "><",                # swap bytes of TOS (verify name)
    0x6558: "PICK",              # 1-based (79-STD)
    0x6598: "U*",                # 16x16 -> 32 unsigned (verify)
    0x65B7: "U/",                # 32/16 unsigned divide (verify)
    0x65F0: "U<",
    0x6620: "XOR",
    0x662D: "2DROP",
    0x6634: "2DUP",
    0x6335: "<",
    0x6374: ">",
    0x638C: ">R",
    0x6259: "+",
    0x6264: "+!",
    0x627F: "-",
    0x62A0: "0<",
    0x62AF: "0=",
    0x62BE: "0>",
    0x62CF: "1+",
    0x62DB: "1-",
    0x62E7: "2+",
    0x62F3: "2-",
    0x6572: "ROT",
    0x6516: "MIN",
    0x6020: "ABS'",              # 0< IF NEGATE THEN  (duplicate of ABS)
    0x8066: "(\")",              # inline counted string, pushes its address (verify)
    0x1FFC: "(;CODE)",
}

STALE_HEADS = (0x5800, 0x6000)   # older-build copy of the head table: addresses don't match
# Regions holding separated-head tables (header -> pointer to CFA elsewhere)
HEAD_TABLES = ((0x0000, 0x2000), (0x3800, 0x5800))
WIDTH = 8                        # names longer than this are stored truncated


def h16(v):
    return f"{v:04X}"


class Forth:
    def __init__(self, rom_path=DEFAULT_ROM):
        self.m = open(rom_path, "rb").read()
        self.size = len(self.m)
        self.names = {}          # xt -> name
        self.name_src = {}       # xt -> 'head@ADDR' | 'manual'
        self.by_name = collections.defaultdict(list)
        self.aliases = {}        # xt -> [other names from the head tables]
        self.alias_names = collections.defaultdict(list)
        self.headers = []
        self.code_entries = self._find_semicode()
        self._load_heads()
        self.header_addrs = {x["hdr"]: x["name"] for x in self.headers if x["xt"] == x["cell"]}
        for xt, n in MANUAL_NAMES.items():
            if xt not in self.names:
                self._set(xt, n, "manual")

    # -- memory --------------------------------------------------------------------
    def b(self, a):
        return self.m[a & 0xFFFF]

    def w(self, a):
        return self.m[a & 0xFFFF] << 8 | self.m[(a + 1) & 0xFFFF]

    # -- headers -------------------------------------------------------------------
    def parse_header(self, h):
        """[len|flags] name (last char |0x80, may be truncated) [link] [cfa]."""
        L = self.m[h] & 0x3F
        if not 1 <= L <= 31:
            return None
        chars = []
        p = h + 1
        while True:
            c = self.m[p]
            ch = c & 0x7F
            if not 0x21 <= ch < 0x7F:
                return None
            chars.append(chr(ch))
            p += 1
            if c & 0x80:
                break
            if len(chars) >= L:
                return None
        if len(chars) != L and not (len(chars) == WIDTH and L > WIDTH and self.m[h] & 0x80):
            return None          # a suffix of a longer name, or noise
        name = "".join(chars)
        if L > len(chars):
            name += f"~{L}"      # stored truncated (WIDTH=8): show declared length
        link = self.w(p)
        cell = p + 2
        return {"hdr": h, "flags": self.m[h] & 0xC0, "len": L, "name": name,
                "stored": len(chars), "link": link, "cell": cell, "cfaval": self.w(cell)}

    def is_code_field(self, xt, strict=False):
        """True if xt looks like a real execution token in mapped ROM.
        strict: don't accept 'alias' code fields that point somewhere into the kernel."""
        if not 0x2000 <= xt < 0xFFFE:
            return False
        v = self.w(xt)
        if v in RUNTIMES or v == xt + 2 or self._is_does_stub(v):
            return True
        if strict:
            return v in self.code_entries if hasattr(self, "code_entries") else False
        return 0x5F00 <= v < 0x6700

    def _is_does_stub(self, a):
        return (0x2000 <= a < 0xFFFB and self.m[a] == 0xCC and self.w(a + 1) == DODOES
                and self.m[a + 3] == 0x1E and self.m[a + 4] == 0x05)

    def _load_heads(self):
        cands = {}
        for h in range(0, self.size - 8):
            x = self.parse_header(h)
            if x:
                cands[h] = x
        # A real length byte is never a printable name char, so a candidate that starts
        # inside another candidate's name is a suffix parse (e.g. "LE" inside "VARIABLE").
        inside = set()
        for h, x in cands.items():
            inside.update(range(h + 1, h + 1 + x["stored"]))
        cands = {h: x for h, x in cands.items() if h not in inside}
        # depth = number of candidate headers reachable by following links (incl. self).
        # Real dictionary entries sit in long chains; byte noise that happens to look
        # like a header almost never does.
        depth = {}
        for h0 in cands:
            path, h = [], h0
            onpath = set()
            while h in cands and h not in depth and h not in onpath:
                path.append(h)
                onpath.add(h)
                h = cands[h]["link"]
            t = depth.get(h, 0)
            for s in reversed(path):
                t += 1
                depth[s] = t
        for h, x in cands.items():
            if STALE_HEADS[0] <= h < STALE_HEADS[1]:
                continue
            dep = depth.get(h, 0)
            in_table = any(lo <= h < hi for lo, hi in HEAD_TABLES)
            v = x["cfaval"]
            if h >= 0x2000 and (v in RUNTIMES or v == x["cell"] + 2) and dep >= 3:
                xt = x["cell"]                               # in-place header
            elif in_table and self.is_code_field(v, strict=True) and (
                    dep >= 3 or (x["len"] >= 3 and x["flags"] & 0x80)):
                xt = v                                       # separated head
            else:
                continue
            x["xt"] = xt
            x["depth"] = dep
            self.headers.append(x)
        # priority: in-place mapped headers, then hidden head table, then 0x38xx-0x57xx table
        def prio(x):
            if x["xt"] == x["cell"]:
                return 0
            return 1 if x["hdr"] < 0x2000 else 2
        # choose one display name per xt: best region first, then the name that
        # occurs most often in the tables (e.g. CC01: PON_TEST x2 beats FAST_CAL x1)
        cand = collections.defaultdict(collections.Counter)
        first = {}
        for x in self.headers:
            if prio(x) == 2 and not x["flags"] & 0x80:
                continue        # the 0x38xx-0x57xx table only holds separated heads (flag 0x80)
            cand[x["xt"]][(prio(x), x["name"])] += 1
            first.setdefault((x["xt"], x["name"]), x["hdr"])
        for xt, c in cand.items():
            ranked = sorted(c.items(), key=lambda kv: (kv[0][0], -kv[1], kv[0][1]))
            (_, nm), _ = ranked[0]
            self._set(xt, nm, f"head@{h16(first[(xt, nm)])}")
            for (_, other), _ in ranked[1:]:
                if other != nm and other not in self.aliases.get(xt, []):
                    self.aliases.setdefault(xt, []).append(other)
                    self.alias_names[other].append(xt)

    def _set(self, xt, name, src):
        self.names[xt] = name
        self.name_src[xt] = src
        self.by_name[name].append(xt)

    def _find_semicode(self):
        """Machine-code entry points created by (;CODE): code fields that point there."""
        out = set()
        for a in range(0x2000, self.size - 2):
            if self.w(a) == PSEMICODE and self.w(a - 2) != LIT:
                out.add(a + 2)
        return out

    # -- naming ----------------------------------------------------------------------
    def name(self, xt):
        n = self.names.get(xt)
        if n is None:
            return f"w_{h16(xt)}"
        if len(self.by_name[n]) > 1:
            return f"{n}@{h16(xt)}"
        return n

    def resolve(self, s):
        s = s.strip()
        if "@" in s[1:]:                      # NAME@XXXX form printed for duplicate names
            nm, _, ad = s.rpartition("@")
            try:
                xt = int(ad, 16)
                if xt in self.by_name.get(nm, []):
                    return [xt]
            except ValueError:
                pass
        hits = list(self.by_name.get(s, [])) + [x for x in self.alias_names.get(s, [])
                                                  if x not in self.by_name.get(s, [])]
        if hits:
            return hits
        try:
            return [int(s, 16)]
        except ValueError:
            pass
        low = s.lower()
        return [xt for n, xts in self.by_name.items() if n.lower() == low for xt in xts]

    def kind(self, xt):
        v = self.w(xt)
        if v in RUNTIMES:
            return RUNTIMES[v]
        if v == xt + 2:
            return "CODE"
        if self._is_does_stub(v):
            return "DOES"
        if v in self.code_entries:
            return ";CODE"
        if 0x5F00 <= v < 0x6700:
            return "ALIAS"
        return "?"

    def is_token(self, t):
        if t == PSEMICODE:
            return True
        return self.is_code_field(t) or self.w(t) in self.code_entries

    # -- decompiling -------------------------------------------------------------------
    def body(self, xt, limit=2000):
        """Yield (addr, token, text, operand_len) for a colon body starting at PFA."""
        a = xt + 2
        far = a
        n = 0
        while n < limit:
            n += 1
            t = self.w(a)
            if a != xt + 2 and (t in RUNTIMES or (t == a + 2 and a in self.names)):
                yield a, t, f"\\ (next word begins: {RUNTIMES.get(t, 'CODE')} code field)", 0
                return
            if a in self.header_addrs:
                yield a, t, f"\ (end: header of next word '{self.header_addrs[a]}' starts here)", 0
                return
            if not self.is_token(t):
                if t in self.names:
                    yield a, t, f"{self.name(t)}  !! xt ${h16(t)} has no valid code field (overwritten?)", 0
                    a += 2
                    continue
                yield a, t, f"?? ${h16(t)} (not an xt - end of body?)", 0
                return
            nm = self.name(t)
            p = a + 2
            if t == LIT:
                v = self.w(p)
                txt = f"LIT ${h16(v)} ({v if v < 0x8000 else v - 0x10000})"
                if v in self.names:
                    txt += f"  ' {self.name(v)}"
                p += 2
            elif t == CLIT:
                v = self.b(p)
                txt = f"CLIT ${v:02X} ({v})"
                p += 1
            elif t == FLIT:
                txt = f"2LIT ${self.w(p):04X}{self.w(p + 2):04X}"
                p += 4
            elif t in REL_BRANCHES:
                off = self.w(p)
                off = off - 0x10000 if off & 0x8000 else off
                tgt = (p + off) & 0xFFFF
                txt = f"{nm} -> {h16(tgt)}"
                far = max(far, tgt)
                p += 2
            elif t == STRLIT:
                L = self.b(p)
                s = self.m[p + 1:p + 1 + L].decode("latin1")
                txt = f'{nm} "{s}"'
                p += 1 + L
            elif nm == "COMPILE" or nm == "[COMPILE]":
                v = self.w(p)
                txt = f"{nm} {self.name(v)}"
                p += 2
            elif t == PSEMICODE:
                yield a, t, nm, 0
                yield from self._native_tail(p)
                return
            else:
                txt = nm
                if self.w(t) in (DOCON, DOCON2) and t not in (0x6042, 0x6046, 0x604A, 0x604E):
                    txt += f"{{=${h16(self.w(t + 2))}}}"
            yield a, t, txt, p - a - 2
            a = p
            if t == EXIT and a > far:
                return

    def _native_tail(self, p):
        if self._is_does_stub(p):
            yield p, None, f"[DOES> stub: LDD #${h16(DODOES)} / EXG D,PC]  does-part:", 0
            fake_xt = p + 5 - 2
            yield from self.body(fake_xt)
            return
        yield from (
            (a, None, txt, 0) for a, txt in self.native(p))

    def native(self, start, maxlen=200):
        """Disassemble native code following all branches until NEXT/JMP/RTS."""
        seen = {}
        todo = [start]
        while todo:
            p = todo.pop()
            k = 0
            while p not in seen and 0x2000 <= p < 0x10000 and k < maxlen:
                k += 1
                txt, n, info = disasm_one(self.m, p)
                tgt = info.get("target")
                if tgt == NEXT:
                    txt += "        ; NEXT"
                elif tgt is not None and tgt in self.names:
                    txt += f"        ; {self.name(tgt)}"
                seen[p] = (txt, n)
                f = info.get("flow")
                if tgt is not None and f in ("cond", "jmp") and tgt != NEXT and tgt >= start - 0x40:
                    todo.append(tgt)
                p += n
                if f in ("jmp", "ret"):
                    break
        for a in sorted(seen):
            txt, n = seen[a]
            raw = " ".join(f"{self.m[a + i]:02X}" for i in range(n))
            yield a, f"    {raw:<12} {txt}"

    def decompile(self, xt, show_code=True, raw=False):
        out = []
        k = self.kind(xt)
        src = self.name_src.get(xt, "unnamed")
        out.append(f"{self.name(xt)}   xt={h16(xt)} kind={k} code_field=${h16(self.w(xt))} [{src}]")
        if xt in self.aliases:
            out.append(f"  (also named in head tables: {', '.join(self.aliases[xt])})")
        if k == "DOCOL":
            labels = set()
            lines = list(self.body(xt))
            for a, t, txt, _ in lines:
                if " -> " in txt:
                    labels.add(int(txt.rsplit(" ", 1)[1], 16))
            for a, t, txt, _ in lines:
                mark = ">" if a in labels else " "
                cell = f"{h16(t)} " if (raw and t is not None) else ""
                out.append(f" {mark}{h16(a)}  {cell}{txt}" if t is not None else f"  {h16(a)} {txt}")
        elif k in ("DOCON", "DOCON2"):
            v = self.w(xt + 2)
            out.append(f"  CONSTANT value ${h16(v)} ({v})")
        elif k == "DOCREATE":
            out.append(f"  CREATE'd: PFA {h16(xt + 2)}, cells: "
                       + " ".join(h16(self.w(xt + 2 + 2 * i)) for i in range(4)))
        elif k == "DOES":
            stub = self.w(xt)
            owner = self._owner(stub)
            out.append(f"  DOES> child of {owner}; PFA {h16(xt + 2)} cells: "
                       + " ".join(h16(self.w(xt + 2 + 2 * i)) for i in range(4)))
            out.append(f"  does-part at {h16(stub + 5)}")
        elif k in ("CODE", ";CODE", "ALIAS") and show_code:
            entry = self.w(xt)
            if k != "CODE":
                out.append(f"  code at {h16(entry)}"
                           + (f" (inside {self.name(self._owner(entry, True))})" if k == "ALIAS" else ""))
            for a, line in self.native(entry):
                out.append(f"  {h16(a)} {line}")
        return "\n".join(out)

    def _owner(self, addr, xt_only=False):
        """Nearest named word at or below addr."""
        best = max((x for x in self.names if x <= addr), default=None)
        return best if xt_only else (self.name(best) if best is not None else "?")

    # -- vocab / xref ----------------------------------------------------------------
    def chain(self, top):
        h = top
        while h:
            x = self.parse_header(h)
            if not x:
                break
            x["xt"] = x["cell"] if x["cfaval"] in RUNTIMES else x["cfaval"]
            yield x
            h = x["link"]

    def command_vocab(self):
        """Header list of the command vocabulary (from its newest entry down to TAIL)."""
        links = {x["link"] for x in self.headers}
        tops = [x for x in self.headers if x["hdr"] >= 0xDF00 and x["hdr"] not in links
                and x["xt"] == x["cell"]]
        for t in tops:
            lst = list(self.chain(t["hdr"]))
            if lst and lst[-1]["hdr"] == 0xDFB1:
                return lst
        return []

    def colon_words(self):
        """All addresses whose code field is DOCOL (any 6313 cell in mapped ROM)."""
        return [a for a in range(0x2000, self.size - 2) if self.w(a) == DOCOL]

    def xref(self, target):
        hits = []
        for xt in self.colon_words():
            try:
                for a, t, txt, _ in self.body(xt):
                    if t == target or (t == LIT and self.w(a + 2) == target):
                        hits.append((xt, a))
            except IndexError:
                pass
        return hits


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("words", nargs="*")
    ap.add_argument("--rom", default=DEFAULT_ROM)
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--vocab", action="store_true")
    ap.add_argument("--xref")
    ap.add_argument("--no-code", action="store_true")
    ap.add_argument("--raw", action="store_true")
    ap.add_argument("--all", action="store_true",
                    help="decompile every named word and every DOCOL cell >= 0x2000, by address")
    a = ap.parse_args()
    f = Forth(a.rom)
    if a.list:
        for xt in sorted(f.names):
            al = f.aliases.get(xt)
            print(f"{h16(xt)}  {f.kind(xt):<8} {f.names[xt]:<20} {f.name_src[xt]:<10}"
                  + (f"  aka {', '.join(al)}" if al else ""))
    if a.all:
        xts = sorted(set(x for x in f.names if x >= 0x2000) | set(f.colon_words()))
        for xt in xts:
            print(f.decompile(xt, show_code=not a.no_code, raw=a.raw))
            print()
    if a.vocab:
        for x in f.command_vocab():
            body = [txt for _, _, txt, _ in f.body(x["xt"])]
            print(f"{h16(x['hdr'])} {x['name']:<12} xt={h16(x['xt'])}  " + " ".join(body[:8])
                  + (" ..." if len(body) > 8 else ""))
    if a.xref:
        for target in f.resolve(a.xref):
            for xt, at in f.xref(target):
                print(f"{f.name(target)} used in {f.name(xt)} (xt {h16(xt)}) at {h16(at)}")
    for wname in a.words:
        xts = f.resolve(wname)
        if not xts:
            print(f"{wname}: not found", file=sys.stderr)
        for xt in xts:
            print(f.decompile(xt, show_code=not a.no_code, raw=a.raw))
            print()


if __name__ == "__main__":
    main()
