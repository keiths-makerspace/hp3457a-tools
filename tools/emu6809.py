#!/usr/bin/env python3
"""MC6809 CPU core (stdlib only) - layer 1 of the HP 3457A emulator.

    from emu6809 import CPU, FlatBus
    bus = FlatBus(rom_bytes)            # or any object with read(addr) / write(addr, val)
    cpu = CPU(bus); cpu.reset()
    cpu.run(max_cycles=100000)

Features: full documented instruction set (pages 0/2/3), approximate datasheet cycle
counts, NMI/FIRQ/IRQ/SWI/SWI2/SWI3, CWAI, SYNC, RESET, breakpoints (cpu.breaks: set of PCs;
run() returns 'break'), optional trace callback cpu.trace(cpu, pc) before each instruction.
Illegal opcodes raise IllegalOpcode.
"""

E_, F_, H_, I_, N_, Z_, V_, C_ = 0x80, 0x40, 0x20, 0x10, 0x08, 0x04, 0x02, 0x01


class IllegalOpcode(Exception):
    pass


class FlatBus:
    """64 KB flat memory. Writes to [rom_lo, 0x10000) are ignored (ROM)."""

    def __init__(self, image=None, rom_lo=0x2000):
        self.mem = bytearray(0x10000)
        if image:
            self.mem[:len(image)] = image
        self.rom_lo = rom_lo

    def read(self, a):
        return self.mem[a]

    def write(self, a, v):
        if a < self.rom_lo:
            self.mem[a] = v


def _s8(v):
    return v - 0x100 if v & 0x80 else v


def _s16(v):
    return v - 0x10000 if v & 0x8000 else v


# base cycle counts for page-0 opcodes (indexed extras added separately)
_CYC = [0] * 256
for _o in range(0x00, 0x10):
    _CYC[_o] = 6
_CYC[0x0E] = 3
for _o, _c in {0x12: 2, 0x13: 4, 0x16: 5, 0x17: 9, 0x19: 2, 0x1A: 3, 0x1C: 3, 0x1D: 2,
               0x1E: 8, 0x1F: 6, 0x39: 5, 0x3A: 3, 0x3B: 6, 0x3C: 20, 0x3D: 11,
               0x3F: 19, 0x30: 4, 0x31: 4, 0x32: 4, 0x33: 4, 0x34: 5, 0x35: 5,
               0x36: 5, 0x37: 5}.items():
    _CYC[_o] = _c
for _o in range(0x20, 0x30):
    _CYC[_o] = 3
for _o in range(0x40, 0x60):
    _CYC[_o] = 2
for _o in range(0x60, 0x70):
    _CYC[_o] = 6
for _o in range(0x70, 0x80):
    _CYC[_o] = 7
_CYC[0x6E], _CYC[0x7E] = 3, 4
for _o in range(0x80, 0x100):
    lo, mode = _o & 0x0F, (_o >> 4) & 3
    if lo == 0x3 or (lo == 0xC and _o < 0xC0):              # SUBD, ADDD, CMPX: 16-bit ALU
        _CYC[_o] = [4, 6, 6, 7][mode]
    elif lo in (0xE, 0xF) or (lo in (0xC, 0xD) and _o >= 0xC0):   # LD/ST of D, X, U
        _CYC[_o] = [3, 5, 5, 6][mode]
    else:
        _CYC[_o] = [2, 4, 4, 5][mode]
    if lo == 0xD and _o < 0xC0:       # BSR / JSR
        _CYC[_o] = [7, 7, 7, 8][mode]
# (checked against lwasm's tables for every opcode and mode: tools/cyclecheck.py)


class CPU:
    def __init__(self, bus):
        self.bus = bus
        self.a = self.b = self.dp = 0
        self.x = self.y = self.u = self.s = self.pc = 0
        self.cc = I_ | F_
        self.cycles = 0
        self.nmi_armed = False
        self.nmi_pending = False
        self.irq_line = False        # level-sensitive; set by the board
        self.firq_line = False
        self.waiting = None          # None | 'cwai' | 'sync'
        self.breaks = set()
        self.trace = None
        self.last_pc = 0
        self._build()

    # ---------------------------------------------------------------- memory
    def rd(self, a):
        return self.bus.read(a & 0xFFFF)

    def wr(self, a, v):
        self.bus.write(a & 0xFFFF, v & 0xFF)

    def rd16(self, a):
        return (self.rd(a) << 8) | self.rd(a + 1)

    def wr16(self, a, v):
        self.wr(a, v >> 8)
        self.wr(a + 1, v)

    def fetch(self):
        v = self.rd(self.pc)
        self.pc = (self.pc + 1) & 0xFFFF
        return v

    def fetch16(self):
        v = self.rd16(self.pc)
        self.pc = (self.pc + 2) & 0xFFFF
        return v

    @property
    def d(self):
        return (self.a << 8) | self.b

    @d.setter
    def d(self, v):
        self.a = (v >> 8) & 0xFF
        self.b = v & 0xFF

    # stacks
    def push8(self, sp, v):
        val = (getattr(self, sp) - 1) & 0xFFFF
        setattr(self, sp, val)
        self.wr(val, v)

    def push16(self, sp, v):
        self.push8(sp, v & 0xFF)
        self.push8(sp, v >> 8)

    def pull8(self, sp):
        a = getattr(self, sp)
        setattr(self, sp, (a + 1) & 0xFFFF)
        return self.rd(a)

    def pull16(self, sp):
        hi = self.pull8(sp)
        return (hi << 8) | self.pull8(sp)

    # ---------------------------------------------------------------- flags
    def _nz8(self, r):
        self.cc = (self.cc & ~(N_ | Z_)) | (r & 0x80 and N_) | (0 if r & 0xFF else Z_)

    def _nz16(self, r):
        self.cc = (self.cc & ~(N_ | Z_)) | (N_ if r & 0x8000 else 0) | (0 if r & 0xFFFF else Z_)

    def _flag(self, f, cond):
        if cond:
            self.cc |= f
        else:
            self.cc &= ~f

    # ---------------------------------------------------------------- addressing
    def ea_direct(self):
        return (self.dp << 8) | self.fetch()

    def ea_ext(self):
        return self.fetch16()

    def ea_indexed(self):
        pb = self.fetch()
        rn = ("x", "y", "u", "s")[(pb >> 5) & 3]
        r = getattr(self, rn)
        if not pb & 0x80:
            off = pb & 0x1F
            if off & 0x10:
                off -= 32
            self.cycles += 1
            return (r + off) & 0xFFFF
        mode = pb & 0x0F
        ind = pb & 0x10
        if mode == 0x0:
            ea = r
            setattr(self, rn, (r + 1) & 0xFFFF)
            self.cycles += 2
        elif mode == 0x1:
            ea = r
            setattr(self, rn, (r + 2) & 0xFFFF)
            self.cycles += 3
        elif mode == 0x2:
            r = (r - 1) & 0xFFFF
            setattr(self, rn, r)
            ea = r
            self.cycles += 2
        elif mode == 0x3:
            r = (r - 2) & 0xFFFF
            setattr(self, rn, r)
            ea = r
            self.cycles += 3
        elif mode == 0x4:
            ea = r
        elif mode == 0x5:
            ea = r + _s8(self.b)
            self.cycles += 1
        elif mode == 0x6:
            ea = r + _s8(self.a)
            self.cycles += 1
        elif mode == 0x8:
            ea = r + _s8(self.fetch())
            self.cycles += 1
        elif mode == 0x9:
            ea = r + _s16(self.fetch16())
            self.cycles += 4
        elif mode == 0xB:
            ea = r + _s16(self.d)
            self.cycles += 4
        elif mode == 0xC:
            off = _s8(self.fetch())
            ea = self.pc + off
            self.cycles += 1
        elif mode == 0xD:
            off = _s16(self.fetch16())
            ea = self.pc + off
            self.cycles += 5
        elif mode == 0xF and ind:
            ea = self.fetch16()
            self.cycles += 2
        else:
            raise IllegalOpcode(f"indexed postbyte {pb:02X} at {self.last_pc:04X}")
        ea &= 0xFFFF
        if ind:
            ea = self.rd16(ea)
            self.cycles += 3
        return ea

    def ea_for(self, mode):
        """mode: 1 direct, 2 indexed, 3 extended"""
        if mode == 1:
            return self.ea_direct()
        if mode == 2:
            return self.ea_indexed()
        return self.ea_ext()

    # ---------------------------------------------------------------- ALU
    def add8(self, a, m, c=0):
        r = a + m + c
        self.cc &= ~(H_ | N_ | Z_ | V_ | C_)
        if (a & 0xF) + (m & 0xF) + c > 0xF:
            self.cc |= H_
        if r & 0x100:
            self.cc |= C_
        if (~(a ^ m) & (a ^ r)) & 0x80:
            self.cc |= V_
        self._nz8(r)
        return r & 0xFF

    def sub8(self, a, m, c=0):
        r = a - m - c
        self.cc &= ~(N_ | Z_ | V_ | C_)
        if r & 0x100:
            self.cc |= C_
        if ((a ^ m) & (a ^ r)) & 0x80:
            self.cc |= V_
        self._nz8(r)
        return r & 0xFF

    def add16(self, a, m):
        r = a + m
        self.cc &= ~(N_ | Z_ | V_ | C_)
        if r & 0x10000:
            self.cc |= C_
        if (~(a ^ m) & (a ^ r)) & 0x8000:
            self.cc |= V_
        self._nz16(r)
        return r & 0xFFFF

    def sub16(self, a, m):
        r = a - m
        self.cc &= ~(N_ | Z_ | V_ | C_)
        if r & 0x10000:
            self.cc |= C_
        if ((a ^ m) & (a ^ r)) & 0x8000:
            self.cc |= V_
        self._nz16(r)
        return r & 0xFFFF

    def logic8(self, r):
        self.cc &= ~V_
        self._nz8(r)
        return r & 0xFF

    # read-modify-write 8-bit ops; return new value (None for TST)
    def rmw(self, op, v):
        cc = self.cc
        if op == 0x0:      # NEG
            r = (-v) & 0xFF
            self.cc = cc & ~(N_ | Z_ | V_ | C_)
            if v == 0x80:
                self.cc |= V_
            if v != 0:
                self.cc |= C_
            self._nz8(r)
            return r
        if op == 0x3:      # COM
            r = (~v) & 0xFF
            self.cc = (cc & ~V_) | C_
            self._nz8(r)
            return r
        if op == 0x4:      # LSR
            self.cc = cc & ~(N_ | Z_ | C_)
            if v & 1:
                self.cc |= C_
            r = v >> 1
            self._nz8(r)
            return r
        if op == 0x6:      # ROR
            r = (v >> 1) | (0x80 if cc & C_ else 0)
            self.cc = cc & ~(N_ | Z_ | C_)
            if v & 1:
                self.cc |= C_
            self._nz8(r)
            return r
        if op == 0x7:      # ASR
            r = (v >> 1) | (v & 0x80)
            self.cc = cc & ~(N_ | Z_ | C_)
            if v & 1:
                self.cc |= C_
            self._nz8(r)
            return r
        if op == 0x8:      # ASL/LSL
            r = (v << 1) & 0xFF
            self.cc = cc & ~(N_ | Z_ | V_ | C_)
            if v & 0x80:
                self.cc |= C_
            if (v ^ (v << 1)) & 0x80:
                self.cc |= V_
            self._nz8(r)
            return r
        if op == 0x9:      # ROL
            r = ((v << 1) | (1 if cc & C_ else 0)) & 0xFF
            self.cc = cc & ~(N_ | Z_ | V_ | C_)
            if v & 0x80:
                self.cc |= C_
            if (v ^ (v << 1)) & 0x80:
                self.cc |= V_
            self._nz8(r)
            return r
        if op == 0xA:      # DEC
            r = (v - 1) & 0xFF
            self.cc = cc & ~(N_ | Z_ | V_)
            if v == 0x80:
                self.cc |= V_
            self._nz8(r)
            return r
        if op == 0xC:      # INC
            r = (v + 1) & 0xFF
            self.cc = cc & ~(N_ | Z_ | V_)
            if v == 0x7F:
                self.cc |= V_
            self._nz8(r)
            return r
        if op == 0xD:      # TST
            self.cc = cc & ~V_
            self._nz8(v)
            return None
        if op == 0xF:      # CLR
            self.cc = (cc & ~(N_ | V_ | C_)) | Z_
            return 0
        raise IllegalOpcode(f"rmw {op:X} at {self.last_pc:04X}")

    # ---------------------------------------------------------------- registers by code
    def get_reg(self, code):
        return [lambda: self.d, lambda: self.x, lambda: self.y, lambda: self.u,
                lambda: self.s, lambda: self.pc, None, None,
                lambda: self.a, lambda: self.b, lambda: self.cc, lambda: self.dp][code]()

    def set_reg(self, code, v):
        if code == 0:
            self.d = v & 0xFFFF
        elif code == 1:
            self.x = v & 0xFFFF
        elif code == 2:
            self.y = v & 0xFFFF
        elif code == 3:
            self.u = v & 0xFFFF
        elif code == 4:
            self.s = v & 0xFFFF
            self.nmi_armed = True
        elif code == 5:
            self.pc = v & 0xFFFF
        elif code == 8:
            self.a = v & 0xFF
        elif code == 9:
            self.b = v & 0xFF
        elif code == 10:
            self.cc = v & 0xFF
        elif code == 11:
            self.dp = v & 0xFF

    def _tfr_exg(self, exg):
        pb = self.fetch()
        src, dst = pb >> 4, pb & 0x0F
        valid = set(range(6)) | {8, 9, 10, 11}
        if src not in valid or dst not in valid:
            raise IllegalOpcode(f"TFR/EXG {pb:02X} at {self.last_pc:04X}")
        sv = self.get_reg(src)
        if exg:
            dv = self.get_reg(dst)
            if (src >= 8) != (dst >= 8):     # mixed size: 6809 pads with FF
                sv = sv | 0xFF00 if src >= 8 else sv
                dv = dv | 0xFF00 if dst >= 8 else dv
            self.set_reg(dst, sv)
            self.set_reg(src, dv)
        else:
            if src >= 8 and dst < 8:
                sv |= 0xFF00
            self.set_reg(dst, sv)

    def _psh(self, sp, mask, other):
        self.cycles += bin(mask & 0xF0).count("1") * 2 + bin(mask & 0x0F).count("1")
        if mask & 0x80:
            self.push16(sp, self.pc)
        if mask & 0x40:
            self.push16(sp, getattr(self, other))
        if mask & 0x20:
            self.push16(sp, self.y)
        if mask & 0x10:
            self.push16(sp, self.x)
        if mask & 0x08:
            self.push8(sp, self.dp)
        if mask & 0x04:
            self.push8(sp, self.b)
        if mask & 0x02:
            self.push8(sp, self.a)
        if mask & 0x01:
            self.push8(sp, self.cc)

    def _pul(self, sp, mask, other):
        self.cycles += bin(mask & 0xF0).count("1") * 2 + bin(mask & 0x0F).count("1")
        if mask & 0x01:
            self.cc = self.pull8(sp)
        if mask & 0x02:
            self.a = self.pull8(sp)
        if mask & 0x04:
            self.b = self.pull8(sp)
        if mask & 0x08:
            self.dp = self.pull8(sp)
        if mask & 0x10:
            self.x = self.pull16(sp)
        if mask & 0x20:
            self.y = self.pull16(sp)
        if mask & 0x40:
            setattr(self, other, self.pull16(sp))
            if other == "s":
                self.nmi_armed = True
        if mask & 0x80:
            self.pc = self.pull16(sp)

    def _cond(self, n):
        cc = self.cc
        c, z, v, nn = cc & C_, cc & Z_, cc & V_, cc & N_
        if n == 0:
            return True
        if n == 1:
            return False
        if n == 2:
            return not (c or z)                 # BHI
        if n == 3:
            return bool(c or z)                 # BLS
        if n == 4:
            return not c                        # BCC
        if n == 5:
            return bool(c)                      # BCS
        if n == 6:
            return not z
        if n == 7:
            return bool(z)
        if n == 8:
            return not v
        if n == 9:
            return bool(v)
        if n == 10:
            return not nn
        if n == 11:
            return bool(nn)
        lt = bool(nn) != bool(v)
        if n == 12:
            return not lt                       # BGE
        if n == 13:
            return lt                           # BLT
        if n == 14:
            return not (lt or z)                # BGT
        return bool(lt or z)                    # BLE

    # ---------------------------------------------------------------- interrupts
    def _push_all(self):
        self.push16("s", self.pc)
        self.push16("s", self.u)
        self.push16("s", self.y)
        self.push16("s", self.x)
        self.push8("s", self.dp)
        self.push8("s", self.b)
        self.push8("s", self.a)
        self.push8("s", self.cc)

    def reset(self):
        self.cc = I_ | F_
        self.dp = 0
        self.nmi_armed = False
        self.nmi_pending = False
        self.waiting = None
        self.pc = self.rd16(0xFFFE)

    def nmi(self):
        self.nmi_pending = True

    def _service_interrupts(self):
        """Returns True if an interrupt was taken."""
        if self.nmi_pending and self.nmi_armed:
            self.nmi_pending = False
            if self.waiting != "cwai":
                self.cc |= E_
                self._push_all()
            self.cc |= I_ | F_
            self.pc = self.rd16(0xFFFC)
            self.waiting = None
            self.cycles += 19
            return True
        if self.firq_line and not self.cc & F_:
            if self.waiting != "cwai":
                self.cc &= ~E_
                self.push16("s", self.pc)
                self.push8("s", self.cc)
            self.cc |= I_ | F_
            self.pc = self.rd16(0xFFF6)
            self.waiting = None
            self.cycles += 10
            return True
        if self.irq_line and not self.cc & I_:
            if self.waiting != "cwai":
                self.cc |= E_
                self._push_all()
            self.cc |= I_
            self.pc = self.rd16(0xFFF8)
            self.waiting = None
            self.cycles += 19
            return True
        if self.waiting == "sync" and (self.irq_line or self.firq_line or self.nmi_pending):
            self.waiting = None     # masked interrupt ends SYNC, execution continues
        return False

    # ---------------------------------------------------------------- execution
    def step(self):
        """Execute one instruction (or service one interrupt). Returns cycles used."""
        c0 = self.cycles
        if self._service_interrupts():
            return self.cycles - c0
        if self.waiting:
            self.cycles += 1
            return 1
        self.last_pc = self.pc
        if self.trace:
            self.trace(self, self.pc)
        op = self.fetch()
        self.cycles += _CYC[op]
        h = self._ops[op]
        if h is None:
            raise IllegalOpcode(f"opcode {op:02X} at {self.last_pc:04X}")
        h(op)
        return self.cycles - c0

    def run(self, max_cycles=None, max_steps=None, until=None):
        """Run until a breakpoint ('break'), until(cpu) is true ('until'),
        or a limit is reached ('limit')."""
        steps = 0
        end = None if max_cycles is None else self.cycles + max_cycles
        breaks = self.breaks
        while True:
            if breaks and self.pc in breaks and steps:
                return "break"
            if until is not None and until(self):
                return "until"
            self.step()
            steps += 1
            if max_steps is not None and steps >= max_steps:
                return "limit"
            if end is not None and self.cycles >= end:
                return "limit"

    # ---------------------------------------------------------------- opcode table
    def _build(self):
        t = [None] * 256
        for op in range(0x00, 0x10):
            if op in RMW_OPS:
                t[op] = self._op_rmw
        for op in range(0x60, 0x80):
            if (op & 0x0F) in RMW_OPS:
                t[op] = self._op_rmw
        for op in range(0x40, 0x60):
            if (op & 0x0F) in RMW_OPS and (op & 0x0F) != 0xE:
                t[op] = self._op_inh_ab
        for op in range(0x20, 0x30):
            t[op] = self._op_bra
        for op in range(0x80, 0x100):
            t[op] = self._op_acc
        t[0x10] = self._op_page2
        t[0x11] = self._op_page3
        t[0x12] = lambda op: None
        t[0x13] = self._op_sync
        t[0x16] = self._op_lbra
        t[0x17] = self._op_lbsr
        t[0x19] = self._op_daa
        t[0x1A] = lambda op: setattr(self, "cc", self.cc | self.fetch())
        t[0x1C] = lambda op: setattr(self, "cc", self.cc & self.fetch())
        t[0x1D] = self._op_sex
        t[0x1E] = lambda op: self._tfr_exg(True)
        t[0x1F] = lambda op: self._tfr_exg(False)
        t[0x30] = t[0x31] = t[0x32] = t[0x33] = self._op_lea
        t[0x34] = lambda op: self._psh("s", self.fetch(), "u")
        t[0x35] = lambda op: self._pul("s", self.fetch(), "u")
        t[0x36] = lambda op: self._psh("u", self.fetch(), "s")
        t[0x37] = lambda op: self._pul("u", self.fetch(), "s")
        t[0x39] = lambda op: setattr(self, "pc", self.pull16("s"))
        t[0x3A] = lambda op: setattr(self, "x", (self.x + self.b) & 0xFFFF)
        t[0x3B] = self._op_rti
        t[0x3C] = self._op_cwai
        t[0x3D] = self._op_mul
        t[0x3F] = lambda op: self._swi(0xFFFA, True)
        self._ops = t

    def _op_rmw(self, op):
        lo = op & 0x0F
        hi = op >> 4
        ea = self.ea_direct() if hi == 0 else (self.ea_indexed() if hi == 6 else self.ea_ext())
        if lo == 0xE:
            self.pc = ea
            return
        r = self.rmw(lo, self.rd(ea))
        if r is not None:
            self.wr(ea, r)

    def _op_inh_ab(self, op):
        if op < 0x50:
            r = self.rmw(op & 0x0F, self.a)
            if r is not None:
                self.a = r
        else:
            r = self.rmw(op & 0x0F, self.b)
            if r is not None:
                self.b = r

    def _op_bra(self, op):
        off = _s8(self.fetch())
        if self._cond(op - 0x20):
            self.pc = (self.pc + off) & 0xFFFF

    def _op_lbra(self, op):
        off = _s16(self.fetch16())
        self.pc = (self.pc + off) & 0xFFFF

    def _op_lbsr(self, op):
        off = _s16(self.fetch16())
        self.push16("s", self.pc)
        self.pc = (self.pc + off) & 0xFFFF

    def _op_daa(self, op):
        a = self.a
        cf = 0
        msn, lsn = a & 0xF0, a & 0x0F
        if lsn > 9 or self.cc & H_:
            cf |= 0x06
        if msn > 0x80 and lsn > 9:
            cf |= 0x60
        if msn > 0x90 or self.cc & C_:
            cf |= 0x60
        r = a + cf
        self.cc &= ~(N_ | Z_ | V_)
        if r & 0x100:
            self.cc |= C_
        self.a = r & 0xFF
        self._nz8(self.a)

    def _op_sex(self, op):
        self.a = 0xFF if self.b & 0x80 else 0
        self._nz16(self.d)
        self.cc &= ~V_

    def _op_lea(self, op):
        ea = self.ea_indexed()
        if op == 0x30:
            self.x = ea
            self._flag(Z_, ea == 0)
        elif op == 0x31:
            self.y = ea
            self._flag(Z_, ea == 0)
        elif op == 0x32:
            self.s = ea
            self.nmi_armed = True
        else:
            self.u = ea

    def _op_rti(self, op):
        self.cc = self.pull8("s")
        if self.cc & E_:
            self.a = self.pull8("s")
            self.b = self.pull8("s")
            self.dp = self.pull8("s")
            self.x = self.pull16("s")
            self.y = self.pull16("s")
            self.u = self.pull16("s")
            self.cycles += 9
        self.pc = self.pull16("s")

    def _op_cwai(self, op):
        self.cc &= self.fetch()
        self.cc |= E_
        self._push_all()
        self.waiting = "cwai"

    def _op_sync(self, op):
        self.waiting = "sync"

    def _op_mul(self, op):
        r = self.a * self.b
        self.d = r
        self._flag(Z_, r == 0)
        self._flag(C_, r & 0x80)

    def _swi(self, vec, mask):
        self.cc |= E_
        self._push_all()
        if mask:
            self.cc |= I_ | F_
        self.pc = self.rd16(vec)

    def _op_acc(self, op):
        lo = op & 0x0F
        mode = (op >> 4) & 3
        bside = op >= 0xC0
        if op == 0x8D:                                   # BSR
            off = _s8(self.fetch())
            self.push16("s", self.pc)
            self.pc = (self.pc + off) & 0xFFFF
            return
        # 16-bit operations
        if bside:
            wide = lo in (0x3, 0xC, 0xD, 0xE, 0xF)
        else:
            wide = lo in (0x3, 0xC, 0xD, 0xE, 0xF)
        if mode == 0 and (lo in (0x7, 0xF) or (lo == 0xD and bside)):
            raise IllegalOpcode(f"opcode {op:02X} at {self.last_pc:04X}")
        if wide:
            if mode == 0:
                ea = self.pc
                self.pc = (self.pc + 2) & 0xFFFF
            else:
                ea = self.ea_for(mode)
            self._wide(op, lo, bside, ea)
            return
        if mode == 0:
            ea = self.pc
            self.pc = (self.pc + 1) & 0xFFFF
        else:
            ea = self.ea_for(mode)
        acc = self.b if bside else self.a
        if lo == 0x7:                                    # STA / STB
            self.wr(ea, acc)
            self.logic8(acc)
            return
        m = self.rd(ea)
        if lo == 0x0:
            r = self.sub8(acc, m)
        elif lo == 0x1:
            self.sub8(acc, m)
            return
        elif lo == 0x2:
            r = self.sub8(acc, m, 1 if self.cc & C_ else 0)
        elif lo == 0x4:
            r = self.logic8(acc & m)
        elif lo == 0x5:
            self.logic8(acc & m)
            return
        elif lo == 0x6:
            r = self.logic8(m)
        elif lo == 0x8:
            r = self.logic8(acc ^ m)
        elif lo == 0x9:
            r = self.add8(acc, m, 1 if self.cc & C_ else 0)
        elif lo == 0xA:
            r = self.logic8(acc | m)
        elif lo == 0xB:
            r = self.add8(acc, m)
        else:
            raise IllegalOpcode(f"opcode {op:02X} at {self.last_pc:04X}")
        if bside:
            self.b = r
        else:
            self.a = r

    def _wide(self, op, lo, bside, ea):
        if not bside:
            if lo == 0x3:
                self.d = self.sub16(self.d, self.rd16(ea))
            elif lo == 0xC:
                self.sub16(self.x, self.rd16(ea))
            elif lo == 0xD:                              # JSR
                self.push16("s", self.pc)
                self.pc = ea
            elif lo == 0xE:
                self.x = self.rd16(ea)
                self._ld16_flags(self.x)
            elif lo == 0xF:
                self.wr16(ea, self.x)
                self._ld16_flags(self.x)
        else:
            if lo == 0x3:
                self.d = self.add16(self.d, self.rd16(ea))
            elif lo == 0xC:
                self.d = self.rd16(ea)
                self._ld16_flags(self.d)
            elif lo == 0xD:
                self.wr16(ea, self.d)
                self._ld16_flags(self.d)
            elif lo == 0xE:
                self.u = self.rd16(ea)
                self._ld16_flags(self.u)
            elif lo == 0xF:
                self.wr16(ea, self.u)
                self._ld16_flags(self.u)

    def _ld16_flags(self, v):
        self.cc &= ~V_
        self._nz16(v)

    def _op_page2(self, _):
        op = self.fetch()
        self.cycles += 1
        if 0x21 <= op <= 0x2F:
            off = _s16(self.fetch16())
            self.cycles += 4
            if self._cond(op - 0x20):
                self.pc = (self.pc + off) & 0xFFFF
                self.cycles += 1
            return
        if op == 0x3F:
            self.cycles += 19
            self._swi(0xFFF4, False)
            return
        lo = op & 0x0F
        mode = (op >> 4) & 3
        # CMPD/CMPY: 5/7/7/8; LDY/STY/LDS/STS: 4/6/6/7 (with the prefix cycle above)
        self.cycles += ([4, 6, 6, 7] if lo in (0x3, 0xC) else [3, 5, 5, 6])[mode]
        if mode == 0:
            ea = self.pc
            self.pc = (self.pc + 2) & 0xFFFF
        else:
            ea = self.ea_for(mode)
        if op < 0xC0:
            if lo == 0x3:
                self.sub16(self.d, self.rd16(ea))
            elif lo == 0xC:
                self.sub16(self.y, self.rd16(ea))
            elif lo == 0xE:
                self.y = self.rd16(ea)
                self._ld16_flags(self.y)
            elif lo == 0xF and mode:
                self.wr16(ea, self.y)
                self._ld16_flags(self.y)
            else:
                raise IllegalOpcode(f"10 {op:02X} at {self.last_pc:04X}")
        else:
            if lo == 0xE:
                self.s = self.rd16(ea)
                self.nmi_armed = True
                self._ld16_flags(self.s)
            elif lo == 0xF and mode:
                self.wr16(ea, self.s)
                self._ld16_flags(self.s)
            else:
                raise IllegalOpcode(f"10 {op:02X} at {self.last_pc:04X}")

    def _op_page3(self, _):
        op = self.fetch()
        self.cycles += 1
        if op == 0x3F:
            self.cycles += 19
            self._swi(0xFFF2, False)
            return
        lo = op & 0x0F
        mode = (op >> 4) & 3
        if op >= 0xC0 or lo not in (0x3, 0xC):
            raise IllegalOpcode(f"11 {op:02X} at {self.last_pc:04X}")
        self.cycles += [4, 6, 6, 7][mode]
        if mode == 0:
            ea = self.pc
            self.pc = (self.pc + 2) & 0xFFFF
        else:
            ea = self.ea_for(mode)
        self.sub16(self.u if lo == 0x3 else self.s, self.rd16(ea))

    def state(self):
        cc = "".join(ch if self.cc & bit else "." for ch, bit in
                     zip("EFHINZVC", (E_, F_, H_, I_, N_, Z_, V_, C_)))
        return (f"PC={self.pc:04X} A={self.a:02X} B={self.b:02X} X={self.x:04X} "
                f"Y={self.y:04X} U={self.u:04X} S={self.s:04X} DP={self.dp:02X} CC={cc}")


RMW_OPS = {0x0, 0x3, 0x4, 0x6, 0x7, 0x8, 0x9, 0xA, 0xC, 0xD, 0xE, 0xF}
