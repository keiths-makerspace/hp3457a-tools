#!/usr/bin/env python3
"""Minimal MC6809 disassembler (stdlib only).

Library use:
    from m6809 import disasm_one
    text, length, info = disasm_one(rom, addr)
  info: dict with optional keys 'target' (branch/jump/call address),
        'flow' in {'jmp','call','ret','cond'}, 'ea' (absolute operand address),
        'imm' (immediate value).

CLI:  m6809.py ROM.bin START [END|+LEN]
"""
import sys

REG8 = ["D", "X", "Y", "U", "S", "PC", "?6", "?7", "A", "B", "CC", "DP",
        "?C", "?D", "?E", "?F"]
IDXREG = ["X", "Y", "U", "S"]
BRANCHES = ["BRA", "BRN", "BHI", "BLS", "BCC", "BCS", "BNE", "BEQ",
            "BVC", "BVS", "BPL", "BMI", "BGE", "BLT", "BGT", "BLE"]
RMW = {0x0: "NEG", 0x3: "COM", 0x4: "LSR", 0x6: "ROR", 0x7: "ASR", 0x8: "ASL",
       0x9: "ROL", 0xA: "DEC", 0xC: "INC", 0xD: "TST", 0xE: "JMP", 0xF: "CLR"}
# 0x80-0xFF columns (low nibble) for A side (0x8-0xB) and B side (0xC-0xF)
ACC_A = ["SUBA", "CMPA", "SBCA", "SUBD", "ANDA", "BITA", "LDA", "STA",
         "EORA", "ADCA", "ORA", "ADDA", "CMPX", "JSR", "LDX", "STX"]
ACC_B = ["SUBB", "CMPB", "SBCB", "ADDD", "ANDB", "BITB", "LDB", "STB",
         "EORB", "ADCB", "ORB", "ADDB", "LDD", "STD", "LDU", "STU"]
WIDE_A = {0x3, 0xC, 0xE}          # 16-bit immediates in A columns
WIDE_B = {0x3, 0xC, 0xE}
PAGE2 = {0x83: ("CMPD", 2), 0x8C: ("CMPY", 2), 0x8E: ("LDY", 2), 0x8F: None,
         0xCE: ("LDS", 2)}
PAGE2_MEM = {0x3: "CMPD", 0xC: "CMPY", 0xE: "LDY", 0xF: "STY"}
PAGE2_MEM_B = {0xE: "LDS", 0xF: "STS"}
PAGE3_MEM = {0x3: "CMPU", 0xC: "CMPS"}
INH = {0x12: "NOP", 0x13: "SYNC", 0x19: "DAA", 0x1D: "SEX", 0x39: "RTS",
       0x3A: "ABX", 0x3B: "RTI", 0x3D: "MUL", 0x3F: "SWI"}
INH_AB = {0x0: "NEG", 0x3: "COM", 0x4: "LSR", 0x6: "ROR", 0x7: "ASR",
          0x8: "ASL", 0x9: "ROL", 0xA: "DEC", 0xC: "INC", 0xD: "TST", 0xF: "CLR"}


def s8(v):
    return v - 0x100 if v & 0x80 else v


def s16(v):
    return v - 0x10000 if v & 0x8000 else v


def _w(m, a):
    return m[a & 0xFFFF] << 8 | m[(a + 1) & 0xFFFF]


def indexed(m, a):
    """Decode indexed postbyte at a. Returns (text, extra_bytes, ea_or_None)."""
    pb = m[a]
    r = IDXREG[(pb >> 5) & 3]
    if not pb & 0x80:
        off = pb & 0x1F
        off = off - 32 if off & 0x10 else off
        return f"{off},{r}", 1, None
    ind = pb & 0x10
    mode = pb & 0x0F
    n = 1
    ea = None
    if mode == 0x0:
        t = f",{r}+"
    elif mode == 0x1:
        t = f",{r}++"
    elif mode == 0x2:
        t = f",-{r}"
    elif mode == 0x3:
        t = f",--{r}"
    elif mode == 0x4:
        t = f",{r}"
    elif mode == 0x5:
        t = f"B,{r}"
    elif mode == 0x6:
        t = f"A,{r}"
    elif mode == 0x8:
        t = f"{s8(m[a + 1])},{r}"
        n = 2
    elif mode == 0x9:
        t = f"{s16(_w(m, a + 1))},{r}"
        n = 3
    elif mode == 0xB:
        t = f"D,{r}"
    elif mode == 0xC:
        ea = (a + 2 + s8(m[a + 1])) & 0xFFFF
        t = f"${ea:04X},PCR"
        n = 2
    elif mode == 0xD:
        ea = (a + 3 + s16(_w(m, a + 1))) & 0xFFFF
        t = f"${ea:04X},PCR"
        n = 3
    elif mode == 0xF and ind:
        ea = _w(m, a + 1)
        return f"[${ea:04X}]", 3, ea
    else:
        t = "??"
    return (f"[{t}]" if ind else t), n, ea


def _pshmask(mask, other):
    names = ["CC", "A", "B", "DP", "X", "Y", other, "PC"]
    return ",".join(names[i] for i in range(8) if mask & (1 << i)) or "#0"


def _operand(m, a, mode_hi, wide):
    """mode_hi: 0=imm,1=direct,2=indexed,3=extended. a points at operand."""
    if mode_hi == 0:
        if wide:
            v = _w(m, a)
            return f"#${v:04X}", 2, {"imm": v}
        v = m[a]
        return f"#${v:02X}", 1, {"imm": v}
    if mode_hi == 1:
        v = m[a]
        return f"<${v:02X}", 1, {"ea": v}
    if mode_hi == 2:
        t, n, ea = indexed(m, a)
        return t, n, ({"ea": ea} if ea is not None else {})
    v = _w(m, a)
    return f"${v:04X}", 2, {"ea": v}


def disasm_one(m, a):
    op = m[a]
    info = {}
    if op in INH:
        mn = INH[op]
        if mn in ("RTS", "RTI"):
            info["flow"] = "ret"
        return mn, 1, info
    if op < 0x10:
        if op not in RMW:
            return f"FCB ${op:02X}", 1, info
        v = m[a + 1]
        if op == 0x0E:
            info["flow"] = "jmp"
        return f"{RMW[op]} <${v:02X}", 2, {**info, "ea": v}
    if op == 0x10 or op == 0x11:
        return _prefixed(m, a, op)
    if op in (0x16, 0x17):
        t = (a + 3 + s16(_w(m, a + 1))) & 0xFFFF
        return (("LBRA" if op == 0x16 else "LBSR") + f" ${t:04X}", 3,
                {"target": t, "flow": "jmp" if op == 0x16 else "call"})
    if op in (0x1A, 0x1C, 0x3C):
        mn = {0x1A: "ORCC", 0x1C: "ANDCC", 0x3C: "CWAI"}[op]
        return f"{mn} #${m[a + 1]:02X}", 2, info
    if op in (0x1E, 0x1F):
        pb = m[a + 1]
        mn = "EXG" if op == 0x1E else "TFR"
        return f"{mn} {REG8[pb >> 4]},{REG8[pb & 15]}", 2, info
    if 0x20 <= op <= 0x2F:
        t = (a + 2 + s8(m[a + 1])) & 0xFFFF
        info = {"target": t, "flow": "jmp" if op == 0x20 else "cond"}
        if op == 0x21:
            info = {}
        return f"{BRANCHES[op - 0x20]} ${t:04X}", 2, info
    if 0x30 <= op <= 0x33:
        t, n, ea = indexed(m, a + 1)
        mn = ["LEAX", "LEAY", "LEAS", "LEAU"][op - 0x30]
        return f"{mn} {t}", 1 + n, ({"ea": ea} if ea is not None else {})
    if 0x34 <= op <= 0x37:
        mn = ["PSHS", "PULS", "PSHU", "PULU"][op - 0x34]
        other = "U" if op in (0x34, 0x35) else "S"
        mask = m[a + 1]
        if op in (0x35, 0x37) and mask & 0x80:
            info["flow"] = "ret"
        return f"{mn} {_pshmask(mask, other)}", 2, info
    if 0x40 <= op <= 0x5F:
        lo = op & 0x0F
        if lo not in INH_AB:
            return f"FCB ${op:02X}", 1, info
        return INH_AB[lo] + ("A" if op < 0x50 else "B"), 1, info
    if 0x60 <= op <= 0x7F:
        lo = op & 0x0F
        if lo not in RMW:
            return f"FCB ${op:02X}", 1, info
        if op < 0x70:
            t, n, ea = indexed(m, a + 1)
        else:
            ea = _w(m, a + 1)
            t, n = f"${ea:04X}", 2
        if lo == 0xE:
            info["flow"] = "jmp"
            if ea is not None and op == 0x7E:
                info["target"] = ea
        if ea is not None:
            info["ea"] = ea
        return f"{RMW[lo]} {t}", 1 + n, info
    # 0x80 - 0xFF
    lo = op & 0x0F
    mode_hi = (op >> 4) & 3
    if op == 0x8D:
        t = (a + 2 + s8(m[a + 1])) & 0xFFFF
        return f"BSR ${t:04X}", 2, {"target": t, "flow": "call"}
    if op < 0xC0:
        mn = ACC_A[lo]
        wide = lo in WIDE_A
    else:
        mn = ACC_B[lo]
        wide = lo in WIDE_B
    if mode_hi == 0 and lo in (0x7, 0xD, 0xF) and op != 0x8D:
        return f"FCB ${op:02X}", 1, info
    if op >= 0xC0 and mode_hi == 0 and lo == 0xD:
        return f"FCB ${op:02X}", 1, info
    t, n, oi = _operand(m, a + 1, mode_hi, wide)
    info.update(oi)
    if mn == "JSR":
        info["flow"] = "call"
        if mode_hi == 3:
            info["target"] = oi["ea"]
    return f"{mn} {t}", 1 + n, info


def _prefixed(m, a, pre):
    op = m[a + 1]
    info = {}
    if pre == 0x10:
        if 0x21 <= op <= 0x2F:
            t = (a + 4 + s16(_w(m, a + 2))) & 0xFFFF
            return f"L{BRANCHES[op - 0x20]} ${t:04X}", 4, {"target": t, "flow": "cond"}
        if op == 0x3F:
            return "SWI2", 2, info
        lo = op & 0x0F
        mode_hi = (op >> 4) & 3
        if op >= 0xC0:
            mn = PAGE2_MEM_B.get(lo)
        else:
            mn = PAGE2_MEM.get(lo)
        if mn is None or (mode_hi == 0 and mn in ("STY", "STS")):
            return f"FCB $10,${op:02X}", 2, info
    else:
        if op == 0x3F:
            return "SWI3", 2, info
        lo = op & 0x0F
        mode_hi = (op >> 4) & 3
        mn = PAGE3_MEM.get(lo) if op < 0xC0 else None
        if mn is None:
            return f"FCB $11,${op:02X}", 2, info
    t, n, oi = _operand(m, a + 2, mode_hi, True)
    info.update(oi)
    return f"{mn} {t}", 2 + n, info


def disasm_range(m, start, end, labels=None):
    labels = labels or {}
    a = start
    while a < end:
        text, n, info = disasm_one(m, a)
        raw = " ".join(f"{m[(a + i) & 0xFFFF]:02X}" for i in range(n))
        lab = labels.get(a, "")
        cmt = ""
        tgt = info.get("target", info.get("ea"))
        if tgt is not None and tgt in labels:
            cmt = f"; {labels[tgt]}"
        yield a, f"{a:04X}  {raw:<14} {lab[:14]:<14} {text:<22}{cmt}", n, info
        a += n


def main():
    rom = open(sys.argv[1], "rb").read()
    start = int(sys.argv[2], 16)
    arg = sys.argv[3] if len(sys.argv) > 3 else "+40"
    end = start + int(arg[1:], 16) if arg.startswith("+") else int(arg, 16)
    for _, line, _, _ in disasm_range(rom, start, end):
        print(line)


if __name__ == "__main__":
    main()
