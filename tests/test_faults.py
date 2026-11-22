"""Hardware fault models (emu3457: LinkFaults, RamFaults, Board fault attributes, runaway CPU)
and how the HP ROM's own power-on self-test reacts to them.

Run:  python -m unittest tests.test_faults -v      (~1.5 min)
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
ROM = os.path.join(os.path.dirname(__file__), "..", "firmware", "main-controller-66511",
                   "03457-88810_U602.bin")


class TestLinkWire(unittest.TestCase):
    def test_frames(self):
        from emu3457 import LinkFaults
        lf = LinkFaults(seed=1)
        self.assertEqual(lf.frame(0x01), [0, 1, 0, 0, 0, 0, 0, 0, 0, 1, 1])   # even parity
        self.assertEqual(lf.wire("ok", 0x5A), (0x5A, False, False))
        self.assertIsNone(lf.wire("dead", 0x5A))
        self.assertIsNone(lf.wire("stuck_low", 0x5A))

    def test_slow_rise(self):
        """A 1 right after a 0 is lost: 0x55 (1,0,1,0,... after the start bit) loses every 1;
        0xFF loses only bit 0 (it follows the start bit) and then fails parity. The stop bit is
        lost whenever the parity bit before it is 0, so those bytes also show a framing error
        (the characteristic signature of a slow opto)."""
        from emu3457 import LinkFaults
        lf = LinkFaults()
        self.assertEqual(lf.wire("slow_rise", 0x55), (0x00, False, True))
        self.assertEqual(lf.wire("slow_rise", 0xFF), (0xFE, True, True))
        self.assertEqual(lf.wire("slow_rise", 0x00), (0x00, False, True))
        # bit 7 follows a 0 and is lost; the parity bit (1) has had a bit time to rise
        self.assertEqual(lf.wire("slow_rise", 0x80), (0x00, True, False))

    def test_noise_flags_parity(self):
        from emu3457 import LinkFaults
        lf = LinkFaults(noise=0.2, seed=3)
        out = [lf.wire("noise", 0xA5) for _ in range(400)]
        got = [r for r in out if r is not None]
        self.assertTrue(any(r[0] != 0xA5 for r in got))
        self.assertTrue(any(r[1] for r in got))                 # parity errors seen
        self.assertTrue(any(r is None for r in out))            # start bit lost sometimes


@unittest.skipUnless(os.path.exists(ROM), "ROM image not present")
class TestHpRomReactions(unittest.TestCase):
    """Emulated reactions of the HP ROM's self-test. They agree with the operating manual's
    AUXERR meanings (32K ROM = 1024, non-volatile RAM = 4096) and its "no reply from the A/D
    controller -> DIAGNOSTIC 2, FAILED" description."""

    def boot(self, setup, seconds=10):
        import display3457
        from coverage import make_valid
        from emu3457 import Board
        b = Board(ROM)
        make_valid(b.mem)
        b.runaway = "watchdog"
        setup(b)
        dec, lcd = display3457.attach(b)
        b.power_on()
        b.run(seconds=seconds)
        return b, lcd

    def test_rom_bit_flip_sets_auxerr_1024(self):
        b, lcd = self.boot(lambda b: b.flip_rom_bit(0x3000))
        self.assertEqual(b.aux_err(), 1024)
        b.restore_rom()
        self.assertEqual(b.rom_flipped(), [])

    def test_bad_ram_cell_sets_auxerr_4096(self):
        from emu3457 import RamFaults
        b, lcd = self.boot(lambda b: setattr(b, "ram_faults", RamFaults(cell_stuck={0x1A00: (1, 1)})))
        self.assertEqual(b.aux_err(), 4096)

    def test_dead_link_gives_failed(self):
        b, lcd = self.boot(lambda b: setattr(b.link, "to_main", "dead"))
        self.assertEqual(lcd.text().strip(), "FAILED")

    def test_8051_selftest_failure(self):
        b, lcd = self.boot(lambda b: setattr(b.inguard, "selftest_ok", False))
        self.assertEqual(b.aux_err(), 2)                        # slave processor self-test

    def test_missing_nmi_means_watchdog_resets(self):
        """The NMI handler kicks the watchdog, so with no tick the CPU is reset over and over."""
        b, lcd = self.boot(lambda b: setattr(b, "nmi_dead", True), seconds=5)
        self.assertGreater(len([r for r in b.resets if r[1] == "watchdog"]), 3)

    def test_runaway_cpu_is_reset_not_an_exception(self):
        """RAM data line stuck: the Forth stacks are garbage, the CPU runs wild; with
        runaway='watchdog' the board resets it as the real watchdog would."""
        from emu3457 import RamFaults
        b, lcd = self.boot(lambda b: setattr(b, "ram_faults", RamFaults(data_stuck={3: 0})), 5)
        self.assertTrue(b.runaways)
        self.assertTrue(any("runaway" in r[1] for r in b.resets))


@unittest.skipUnless(os.path.exists(ROM), "ROM image not present")
class TestHpSelfTest(unittest.TestCase):
    """The HP TEST (STEST): with the stand-in's internal channel it passes, also with a virtual
    source attached, and each analog fault fails exactly its own step."""

    def run_test(self, faults=(), source=True):
        import display3457
        from coverage import make_valid
        from emu3457 import Board
        from vinput import VirtualInput
        b = Board(ROM)
        make_valid(b.mem)
        if source:
            b.inguard.source = VirtualInput(dcv=1.2345, ohm=4700.0)
        b.inguard.analog_faults = set(faults)
        dec, lcd = display3457.attach(b)
        b.power_on()
        b.run(seconds=8)
        b.hpib_query("ID?\n")
        b.hpib_write("TEST\n")
        b.run(seconds=12)
        aux = int(float(b.hpib_query("AUXERR?\n")))
        b.hpib_write("DCV\n")
        b.run(seconds=2)
        return aux, lcd.text(), b

    def test_passes(self):
        aux, text, b = self.run_test()
        self.assertEqual(aux, 0)
        self.assertEqual(text, "+1.23450  VDC")                 # normal readings resume
        self.assertFalse(b.inguard.internal)

    def test_each_analog_fault_fails_its_step(self):
        from emu3457 import Inguard8051
        bits = {"integrator": 8, "frontend_zero": 16, "reference": 32, "amps": 64,
                "ac_offset": 128, "ac": 256, "ohms_precharge": 512}
        self.assertEqual(set(bits) | {"acal_gain"}, set(Inguard8051.ANALOG_FAULTS))
        for fault, bit in bits.items():
            with self.subTest(fault):
                aux, text, b = self.run_test([fault])
                self.assertEqual(aux, bit)

    def acal(self, faults=()):
        from coverage import make_valid
        from emu3457 import Board
        b = Board(ROM)
        make_valid(b.mem)
        b.mem[0x050C:0x0523] = bytes(0x17)                      # ACAL constants lost
        b.inguard.analog_faults = set(faults)
        b.power_on()
        b.run(seconds=8)
        b.hpib_query("ID?\n")
        before = int(float(b.hpib_query("ERR?\n")))
        b.hpib_write("ACAL ALL\n")
        b.run(seconds=40)
        return before, int(float(b.hpib_query("ERR?\n"))), b

    def test_acal_restores_the_constants(self):
        """With the ACAL block lost the ROM reports ACAL REQ'D (ERR 1024); ACAL ALL measures
        the internal paths and writes the block that coverage.make_valid uses."""
        from coverage import ACAL_BLOCK
        before, after, b = self.acal()
        self.assertTrue(before & 1024)                          # (+16 BAD HEADER: first-command glitch)
        self.assertEqual(after, 0)
        self.assertEqual(bytes(b.mem[0x050C:0x0523]), ACAL_BLOCK)

    def test_acal_gain_fault(self):
        before, after, b = self.acal(["acal_gain"])
        self.assertEqual(after, 1026)                           # 2 = ACAL failed, still REQ'D


if __name__ == "__main__":
    unittest.main()
