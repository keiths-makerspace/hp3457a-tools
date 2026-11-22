"""Layer-2 tests: the real ROM on the 66511 board model (tools/emu3457.py).

These boot the firmware, so they take ~10-25 s of wall time.
Run:  python -m unittest tests.test_board -v
"""
import collections
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
ROM = os.path.join(os.path.dirname(__file__), "..", "firmware", "main-controller-66511",
                   "03457-88810_U602.bin")


@unittest.skipUnless(os.path.exists(ROM), "ROM image not present")
class TestBoard(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from emu3457 import Board
        from decompile import Forth
        cls.Board = Board
        cls.forth = Forth(ROM)
        b = Board(ROM)
        b.power_on()
        cls.prof = collections.Counter()
        b.run(seconds=7, profile=cls.prof)
        cls.booted = b

    def test_boot_reaches_idle_without_errors(self):
        b = self.booted
        self.assertEqual(b.aux_err(), 0, "AUX_ERR after power-on tests")
        self.assertEqual([r for _, r in b.resets], ["power-on"], "no watchdog resets")
        names = {self.forth.name(x) for x, _ in self.prof.most_common(60)}
        self.assertIn("KBD_SRVC", names)          # main service loop is running
        self.assertIn("DISP_SRV~9", names)
        # Test 0 tried to write the protected region while locked (RAM-protection self test)
        self.assertGreaterEqual(b.blocked_writes, 1)

    def test_link_handshake(self):
        log = self.booted.inguard.log
        kinds = [e[1] for e in log]
        self.assertIn("reset", kinds)
        tx = [e[2] for e in log if e[1] == "tx"]
        self.assertEqual(tx[:1], [0xFF])            # self-test OK
        self.assertEqual(tx[1:16], list(range(1, 16)))   # echo test 1..15
        self.assertEqual(tx[16], 0x0F)              # ACK of the closing 00

    def test_protected_write_sequence(self):
        """_CAL#! body from E153: arm signatures, watchdog reset, CALREQ, write 01FF/01FE."""
        b = self.Board(ROM)
        b.power_on()
        b.run(seconds=6)
        b.mem[0x01FE], b.mem[0x01FF] = 0x12, 0x34
        fd = b.mem[0x01FD]
        c = b.cpu
        c.y, c.pc = 0xE153, 0x6317
        r = b.run(seconds=3, until=lambda bd: bd.cpu.pc == 0x6317 and bd.cpu.y == 0xE18D)
        self.assertEqual(r, "until")
        self.assertEqual([w for _, w in b.resets], ["power-on", "watchdog"])
        self.assertEqual(bytes(b.mem[0x01FE:0x0200]), b"\x00\x00")
        self.assertEqual(b.mem[0x01FD], (fd + 1) & 0xFF)      # writability probe increments it
        self.assertEqual(b.aux_err() & 0x4000, 0)              # lock re-engaged
        self.assertEqual(bytes(b.mem[0x04F8:0x0502]), bytes(10))   # signatures disarmed
        b.write(0x01FE, 0x77)
        self.assertEqual(b.mem[0x01FE], 0)                     # locked again

    def test_security_jumper_and_line_sense(self):
        for jumper, expect in (("NORMAL", 0x40), ("KEY", 0x00)):
            b = self.Board(ROM, secure_jumper=jumper)
            b.out1 = 0x00                       # firmware clears OUT1 before reading (w_C814)
            self.assertEqual(b.read(0x38) & 0x40, expect, jumper)
        b = self.Board(ROM, line_hz=50)
        seen = set()
        for k in range(0, 80000, 1000):
            b.cpu.cycles = k
            seen.add(b.read(0x38) & 0x80)
        self.assertEqual(seen, {0, 0x80})


if __name__ == "__main__":
    unittest.main()
