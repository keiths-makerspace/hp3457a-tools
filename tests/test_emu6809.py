"""Layer-1 tests: MC6809 core (instruction semantics) and ROM Forth words run on it.

Run:  python -m unittest discover -s tests -v
"""
import os
import random
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
from emu6809 import CPU, FlatBus, IllegalOpcode, C_, V_, Z_, N_, H_, I_, F_, E_  # noqa: E402

ROM = os.path.join(os.path.dirname(__file__), "..", "firmware", "main-controller-66511",
                   "03457-88810_U602.bin")


def run_code(code, setup=None, org=0x1000, steps=None, **regs):
    bus = FlatBus(rom_lo=0x10000)          # all RAM for unit tests
    bus.mem[org:org + len(code)] = bytes(code)
    cpu = CPU(bus)
    cpu.pc = org
    cpu.s = 0x0F00
    cpu.u = 0x0E00
    for k, v in regs.items():
        setattr(cpu, k, v)
    if setup:
        setup(bus, cpu)
    end = org + len(code)
    n = 0
    while cpu.pc != end:
        cpu.step()
        n += 1
        if steps and n >= steps or n > 10000:
            break
    return cpu, bus


class TestALU(unittest.TestCase):
    def test_adda_overflow_half(self):
        cpu, _ = run_code([0x86, 0x7F, 0x8B, 0x01])          # LDA #7F / ADDA #1
        self.assertEqual(cpu.a, 0x80)
        self.assertTrue(cpu.cc & V_ and cpu.cc & N_ and cpu.cc & H_)
        self.assertFalse(cpu.cc & C_)

    def test_suba_borrow(self):
        cpu, _ = run_code([0x86, 0x00, 0x80, 0x01])          # LDA #0 / SUBA #1
        self.assertEqual(cpu.a, 0xFF)
        self.assertTrue(cpu.cc & C_ and cpu.cc & N_)
        self.assertFalse(cpu.cc & V_)

    def test_adca_sbca_chain(self):
        # D = 0x01FF + 0x0001 via ADDB/ADCA
        cpu, _ = run_code([0xCC, 0x01, 0xFF, 0xCB, 0x01, 0x89, 0x00])
        self.assertEqual(cpu.d, 0x0200)
        cpu, _ = run_code([0xCC, 0x02, 0x00, 0xC0, 0x01, 0x82, 0x00])  # SUBB/SBCA
        self.assertEqual(cpu.d, 0x01FF)

    def test_neg_cases(self):
        cpu, _ = run_code([0x86, 0x80, 0x40])                 # NEGA 0x80
        self.assertEqual(cpu.a, 0x80)
        self.assertTrue(cpu.cc & V_ and cpu.cc & C_)
        cpu, _ = run_code([0x86, 0x00, 0x40])
        self.assertEqual(cpu.a, 0)
        self.assertTrue(cpu.cc & Z_)
        self.assertFalse(cpu.cc & C_)

    def test_com_sets_carry(self):
        cpu, _ = run_code([0x86, 0x0F, 0x43])
        self.assertEqual(cpu.a, 0xF0)
        self.assertTrue(cpu.cc & C_)

    def test_daa(self):
        cpu, _ = run_code([0x86, 0x09, 0x8B, 0x01, 0x19])    # 09+01 -> 10 BCD
        self.assertEqual(cpu.a, 0x10)
        cpu, _ = run_code([0x86, 0x99, 0x8B, 0x01, 0x19])    # 99+01 -> 00, C
        self.assertEqual(cpu.a, 0x00)
        self.assertTrue(cpu.cc & C_)

    def test_mul(self):
        cpu, _ = run_code([0x86, 0xFF, 0xC6, 0xFF, 0x3D])
        self.assertEqual(cpu.d, 0xFE01)
        self.assertFalse(cpu.cc & C_)
        cpu, _ = run_code([0x86, 0x10, 0xC6, 0x08, 0x3D])
        self.assertEqual(cpu.d, 0x0080)
        self.assertTrue(cpu.cc & C_)

    def test_shifts(self):
        cpu, _ = run_code([0x86, 0x81, 0x47])                 # ASRA
        self.assertEqual(cpu.a, 0xC0)
        self.assertTrue(cpu.cc & C_)
        cpu, _ = run_code([0x86, 0x40, 0x48])                 # ASLA -> V set
        self.assertEqual(cpu.a, 0x80)
        self.assertTrue(cpu.cc & V_)
        cpu, _ = run_code([0x1A, 0x01, 0x86, 0x00, 0x46])     # SEC / RORA
        self.assertEqual(cpu.a, 0x80)

    def test_sex_addd_subd_cmpx(self):
        cpu, _ = run_code([0xC6, 0x80, 0x1D])
        self.assertEqual(cpu.d, 0xFF80)
        cpu, _ = run_code([0xCC, 0x7F, 0xFF, 0xC3, 0x00, 0x01])
        self.assertEqual(cpu.d, 0x8000)
        self.assertTrue(cpu.cc & V_)
        cpu, _ = run_code([0x8E, 0x12, 0x34, 0x8C, 0x12, 0x35])  # CMPX -> borrow
        self.assertTrue(cpu.cc & C_ and cpu.cc & N_)

    def test_tfr_exg(self):
        cpu, _ = run_code([0x8E, 0x12, 0x34, 0x1F, 0x12, 0x1E, 0x89])  # TFR X,Y / EXG A,B
        self.assertEqual(cpu.y, 0x1234)
        cpu, _ = run_code([0xCC, 0xAB, 0xCD, 0x1E, 0x89])
        self.assertEqual(cpu.d, 0xCDAB)


class TestAddressing(unittest.TestCase):
    def test_indexed_modes(self):
        def setup(bus, cpu):
            bus.mem[0x2000:0x2004] = b"\x11\x22\x33\x44"
            bus.mem[0x3000:0x3002] = b"\x20\x02"              # pointer -> 0x2002
        # LDX #2000 / LDA ,X+ / LDB ,X+ / LDY #2004 / LDD ,--Y / LDA [$3000]
        cpu, _ = run_code([0x8E, 0x20, 0x00, 0xA6, 0x80, 0xE6, 0x80,
                           0x10, 0x8E, 0x20, 0x04, 0xEC, 0xA3,
                           0xA6, 0x9F, 0x30, 0x00], setup=setup)
        self.assertEqual(cpu.x, 0x2002)
        self.assertEqual(cpu.y, 0x2002)
        self.assertEqual(cpu.a, 0x33)
        self.assertEqual(cpu.b, 0x44)

    def test_offsets_and_pcr(self):
        def setup(bus, cpu):
            bus.mem[0x2010] = 0x5A
            bus.mem[0x1FF0] = 0xA5
        cpu, _ = run_code([0x8E, 0x20, 0x00, 0xA6, 0x88, 0x10,       # LDA 16,X
                           0xE6, 0x1F,                               # LDB -1,X (5-bit)
                           0x30, 0x89, 0xFF, 0xF0],                  # LEAX -16,X
                          setup=setup)
        self.assertEqual(cpu.a, 0x5A)
        self.assertEqual(cpu.x, 0x1FF0)
        # LDA $data,PCR  (8-bit offset) ; data follows at org+4... use BRA over it
        cpu, _ = run_code([0xA6, 0x8C, 0x02, 0x20, 0x01, 0x77])
        self.assertEqual(cpu.a, 0x77)

    def test_psh_pul_order(self):
        cpu, bus = run_code([0x86, 0x01, 0xC6, 0x02, 0x8E, 0x11, 0x11, 0x10, 0x8E, 0x22, 0x22,
                             0x34, 0x36])                           # PSHS A,B,X,Y
        self.assertEqual(cpu.s, 0x0F00 - 6)
        self.assertEqual(bytes(bus.mem[cpu.s:cpu.s + 6]), b"\x01\x02\x11\x11\x22\x22")
        cpu, _ = run_code([0xCC, 0x12, 0x34, 0x36, 0x06, 0x4F, 0x5F, 0x37, 0x06])
        self.assertEqual(cpu.d, 0x1234)
        self.assertEqual(cpu.u, 0x0E00)

    def test_jsr_rts_bsr_lbsr(self):
        # org 1000: BSR +3 ; NOP*2? -> layout: 1000 BSR 1005 / 1002 LBRA 100A? simple chain
        code = [0x8D, 0x03,             # 1000 BSR 1005
                0x7E, 0x10, 0x0A,       # 1002 JMP 100A (end)
                0x17, 0x00, 0x01,       # 1005 LBSR 1009
                0x39,                   # 1008 RTS
                0x39]                   # 1009 RTS
        cpu, _ = run_code(code)
        self.assertEqual(cpu.pc, 0x100A)
        self.assertEqual(cpu.s, 0x0F00)

    def test_branches_signed(self):
        # LDA #$80 ; CMPA #$01 ; BLT +2 ; LDB #1 ; (skipped) ; end -> B stays 0
        cpu, _ = run_code([0x5F, 0x86, 0x80, 0x81, 0x01, 0x2D, 0x02, 0xC6, 0x01])
        self.assertEqual(cpu.b, 0)
        # unsigned: BHI not taken for 0x80 vs 0x81
        cpu, _ = run_code([0x5F, 0x86, 0x80, 0x81, 0x81, 0x22, 0x02, 0xC6, 0x01])
        self.assertEqual(cpu.b, 1)
        # long branch LBEQ taken
        cpu, _ = run_code([0x5F, 0x4F, 0x10, 0x27, 0x00, 0x02, 0xC6, 0x01])
        self.assertEqual(cpu.b, 0)

    def test_illegal(self):
        with self.assertRaises(IllegalOpcode):
            run_code([0x01])


class TestInterrupts(unittest.TestCase):
    def _cpu(self):
        bus = FlatBus(rom_lo=0x10000)
        bus.mem[0xFFF8:0xFFFA] = b"\x30\x00"     # IRQ  -> 3000
        bus.mem[0xFFF6:0xFFF8] = b"\x31\x00"     # FIRQ -> 3100
        bus.mem[0xFFFC:0xFFFE] = b"\x32\x00"     # NMI  -> 3200
        bus.mem[0xFFFA:0xFFFC] = b"\x33\x00"     # SWI  -> 3300
        bus.mem[0xFFFE:0x10000] = b"\x10\x00"    # RESET-> 1000
        for v in (0x3000, 0x3100, 0x3200, 0x3300):
            bus.mem[v] = 0x3B                    # RTI
        cpu = CPU(bus)
        cpu.reset()
        return cpu, bus

    def test_irq_masked_then_taken(self):
        cpu, bus = self._cpu()
        bus.mem[0x1000:0x1006] = b"\x10\xCE\x0F\x00\x1C\xEF"   # LDS #0F00 / ANDCC #EF
        cpu.step()
        cpu.irq_line = True
        cpu.step()                                # ANDCC executes (I was set)
        self.assertEqual(cpu.pc, 0x1006)
        cpu.step()                                # IRQ taken
        self.assertEqual(cpu.pc, 0x3000)
        self.assertEqual(cpu.s, 0x0F00 - 12)
        self.assertTrue(cpu.cc & I_)
        cpu.irq_line = False
        cpu.step()                                # RTI
        self.assertEqual(cpu.pc, 0x1006)
        self.assertEqual(cpu.s, 0x0F00)
        self.assertFalse(cpu.cc & I_)

    def test_firq_pushes_pc_cc_only(self):
        cpu, bus = self._cpu()
        bus.mem[0x1000:0x1006] = b"\x10\xCE\x0F\x00\x1C\xBF"   # ANDCC #BF (clear F)
        cpu.step()
        cpu.step()
        cpu.firq_line = True
        cpu.step()
        self.assertEqual(cpu.pc, 0x3100)
        self.assertEqual(cpu.s, 0x0F00 - 3)
        self.assertFalse(cpu.cc & E_)
        cpu.firq_line = False
        cpu.step()
        self.assertEqual(cpu.pc, 0x1006)

    def test_nmi_needs_lds(self):
        cpu, bus = self._cpu()
        bus.mem[0x1000:0x1004] = b"\x12\x10\xCE\x0F"
        bus.mem[0x1004] = 0x00
        cpu.nmi()
        cpu.step()                                # NOP, NMI not armed yet
        self.assertEqual(cpu.pc, 0x1001)
        cpu.step()                                # LDS -> armed
        cpu.step()
        self.assertEqual(cpu.pc, 0x3200)

    def test_swi_and_cwai(self):
        cpu, bus = self._cpu()
        bus.mem[0x1000:0x1006] = b"\x10\xCE\x0F\x00\x3F\x12"
        cpu.step()
        cpu.step()
        self.assertEqual(cpu.pc, 0x3300)
        self.assertTrue(cpu.cc & I_ and cpu.cc & F_)
        cpu.step()
        self.assertEqual(cpu.pc, 0x1005)
        cpu, bus = self._cpu()
        bus.mem[0x1000:0x1006] = b"\x10\xCE\x0F\x00\x3C\xEF"   # CWAI #EF
        cpu.step()
        cpu.step()
        self.assertEqual(cpu.waiting, "cwai")
        for _ in range(5):
            cpu.step()
        self.assertEqual(cpu.pc, 0x1006)          # still waiting
        cpu.irq_line = True
        cpu.step()
        self.assertEqual(cpu.pc, 0x3000)
        self.assertEqual(cpu.s, 0x0F00 - 12)


@unittest.skipUnless(os.path.exists(ROM), "ROM image not present")
class TestForthWords(unittest.TestCase):
    """Run real ROM words on the core and compare with Python reference semantics."""

    @classmethod
    def setUpClass(cls):
        from forthcall import ForthMachine
        cls.fm = ForthMachine(ROM)

    def c(self, w, *a, **k):
        return self.fm.call(w, *a, **k)

    def test_stack_words(self):
        self.assertEqual(self.c("DUP", 5), [5, 5])
        self.assertEqual(self.c("DROP", 1, 2), [1])
        self.assertEqual(self.c("SWAP", 1, 2), [2, 1])
        self.assertEqual(self.c("OVER", 1, 2), [1, 2, 1])
        self.assertEqual(self.c("ROT", 1, 2, 3), [2, 3, 1])
        self.assertEqual(self.c("?DUP", 0), [0])
        self.assertEqual(self.c("?DUP", 7), [7, 7])
        self.assertEqual(self.c("PICK", 10, 20, 30, 1), [10, 20, 30, 30])   # 79-STD: 1-based
        self.assertEqual(self.c("PICK", 10, 20, 30, 3), [10, 20, 30, 10])

    def test_arith_random(self):
        rnd = random.Random(3457)
        for _ in range(200):
            a, b = rnd.randrange(65536), rnd.randrange(65536)
            sa = a - 65536 if a & 0x8000 else a
            sb = b - 65536 if b & 0x8000 else b
            self.assertEqual(self.c("+", a, b), [(a + b) & 0xFFFF])
            self.assertEqual(self.c("-", a, b), [(a - b) & 0xFFFF])
            self.assertEqual(self.c("AND", a, b), [a & b])
            self.assertEqual(self.c("OR", a, b), [a | b])
            self.assertEqual(self.c("XOR", a, b), [a ^ b])
            self.assertEqual(self.c("<", a, b), [int(sa < sb)])
            self.assertEqual(self.c(">", a, b), [int(sa > sb)])
            self.assertEqual(self.c("U<", a, b), [int(a < b)])
            self.assertEqual(self.c("=", a, b), [int(a == b)])
            self.assertEqual(self.c("MAX", a, b), [max(sa, sb) & 0xFFFF])
            self.assertEqual(self.c("MIN", a, b), [min(sa, sb) & 0xFFFF])
            self.assertEqual(self.c("NEGATE", a), [(-a) & 0xFFFF])
            self.assertEqual(self.c("ABS", a), [abs(sa) & 0xFFFF])
            p = a * b
            self.assertEqual(self.c("U*", a, b), [p & 0xFFFF, p >> 16])   # ( u1 u2 -- ud )
            if b:
                hi = rnd.randrange(b)                                       # no overflow
                ud = (hi << 16) | a
                self.assertEqual(self.c("U/MOD", a, hi, b), [ud % b, ud // b])

    def test_flags(self):
        for v in (0, 1, 0x7FFF, 0x8000, 0xFFFF):
            s = v - 65536 if v & 0x8000 else v
            self.assertEqual(self.c("0=", v), [int(v == 0)])
            self.assertEqual(self.c("0<", v), [int(s < 0)])
            self.assertEqual(self.c(">BOOL", v), [int(v != 0)])

    def test_misc_words(self):
        self.assertEqual(self.c("2^", 0), [1])
        self.assertEqual(self.c("2^", 12), [4096])
        self.assertEqual(self.c("RANGE@7402", 5, 1, 9), [5, 1])          # ( n lo hi -- n f )
        self.assertEqual(self.c("RANGE@7402", 9, 1, 9), [9, 1])          # inclusive
        self.assertEqual(self.c("RANGE@7402", 10, 1, 9), [10, 0])
        self.assertEqual(self.c("><", 0x1234), [0x3412])
        self.assertEqual(self.c("1+", 0xFFFF), [0])
        self.assertEqual(self.c("2-", 1), [0xFFFF])

    def test_cmove(self):
        bus = self.fm.bus
        bus.mem[0x1000:0x1008] = b"ABCDEFGH"
        self.assertEqual(self.c("CMOVE", 0x1000, 0x1100, 8), [])
        self.assertEqual(bytes(bus.mem[0x1100:0x1108]), b"ABCDEFGH")
        self.assertEqual(self.c("CMOVE", 0x1000, 0x1200, 0), [])      # zero count: no copy
        self.assertEqual(bus.mem[0x1200], 0)

    def test_fetch_store_and_rom_protect(self):
        self.assertEqual(self.c("@", 0x6313), [0x3420])               # DOCOL bytes
        self.assertEqual(self.c("C@", 0x8C03), [0xB6])                # reset entry LDA ext
        self.c("C!", 0x5A, 0x1300)
        self.assertEqual(self.fm.bus.mem[0x1300], 0x5A)
        self.c("C!", 0x00, 0x6313)                                    # write to ROM ignored
        self.assertEqual(self.c("C@", 0x6313), [0x34])

    def test_float_compare(self):
        import struct

        def f(x):
            v = struct.unpack(">I", struct.pack(">f", x))[0]
            return [v & 0xFFFF, v >> 16]          # fig double layout ( lo hi ): 2LIT does PSHU D,X
        for a, b in ((1.0, 2.0), (2.0, 1.0), (-3.5, 0.5), (0.5, -3.5), (32767.0, 32767.4),
                     (-1.0, -2.0), (-2.0, -1.0), (-1.0, 0.0), (0.0, 1.0), (1e-3, 1e3)):
            got = self.c("F<", *(f(a) + f(b)))
            self.assertEqual(bool(got[-1]), a < b, (a, b, got))


if __name__ == "__main__":
    unittest.main()
