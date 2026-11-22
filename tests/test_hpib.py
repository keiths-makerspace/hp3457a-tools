"""Layer-3 tests: HP-IB host, display decoding, keyboard, readings - real ROM on the model.

Boots once (~8 s wall), then drives the meter like a bus controller.
Run:  python -m unittest tests.test_hpib -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
ROM = os.path.join(os.path.dirname(__file__), "..", "firmware", "main-controller-66511",
                   "03457-88810_U602.bin")


def nr3(b):
    return float(b.decode().split(",")[0])


@unittest.skipUnless(os.path.exists(ROM), "ROM image not present")
class TestHpib(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from emu3457 import Board
        from display3457 import attach
        b = Board(ROM)
        cls.dec, cls.lcd = attach(b)
        b.power_on()
        b.run(seconds=7)
        cls.free_run_readings = b.inguard.readings
        cls.free_run_display = cls.lcd.text()
        b.hpib_write("TRIG HOLD\r\n")
        b.hpib_read(seconds=0.3)
        cls.b = b

    def q(self, cmd):
        return self.b.hpib_query(cmd + "\r\n")

    def w(self, cmd):
        self.b.hpib_write(cmd + "\r\n")

    def err(self):
        return int(nr3(self.q("ERR?")))

    def test_readings_flow_and_display(self):
        self.assertGreater(self.free_run_readings, 10)
        self.assertEqual(self.free_run_display.strip(), "+0.00000  VDC")

    def test_id(self):
        self.assertEqual(self.q("ID?"), b"HP3457A\r\n")

    def test_peek(self):
        self.err()
        self.assertEqual(nr3(self.q("PEEK 25363")), 13344)        # 0x3420 at 0x6313
        self.assertEqual(self.err(), 0)
        self.q("PEEK 32768")                                       # > 0x7FFF rejected
        self.assertEqual(self.err(), 64)

    def test_poke_byte_and_signed_peek(self):
        self.w("POKE 4096,165")
        self.assertEqual(self.b.mem[0x1000], 0xA5)
        self.assertEqual(nr3(self.q("PEEK 4096")), 0xA500 - 0x10000)

    def test_peek_hides_sec_code(self):
        self.b.mem[0x0366:0x036A] = b"\x00\x00\x0d\x81"            # SEC_CODE = 3457
        tl = self.b.peek16(0x03AF)
        v = nr3(self.q("PEEK 870"))                                # 0x0366 -> TIMER_LO
        self.assertNotEqual(v, 0x000D)
        self.assertLess(abs(v - tl), 200)                          # timer keeps ticking

    def test_disp_message(self):
        self.w('DISP 2,"HELLO 3457"')
        self.b.run(seconds=0.3)
        self.assertEqual(self.lcd.text(), "HELLO 3457  ")
        self.w("DISP 1")
        self.b.run(seconds=0.3)

    def test_key_query(self):
        self.w("KEY?")
        self.b.run(seconds=0.2)
        self.b.press_key(2, 5)
        self.assertEqual(nr3(self.b.hpib_read()), ((2 + 1) * 8 + 5) * 2)

    def test_sp_peek_poke_link(self):
        ig = self.b.inguard
        ig.mem[0x21] = 0x42
        self.assertEqual(nr3(self.q("SP_PEEK 33")), 0x42)
        self.w("SP_POKE 34,90")
        self.b.run(seconds=0.2)
        self.assertEqual(ig.mem[0x22], 90)
        frames = [e[2] for e in ig.log if e[1] == "cmd"]
        self.assertIn("0d21", frames)                              # wire frame: 0D addr
        self.assertIn("4d225a", frames)                            # wire frame: 4D addr val
        self.assertEqual(self.b.aux_err() & 1, 0)

    def test_line_freq(self):
        self.assertEqual(nr3(self.q("LINE?")), 60)


if __name__ == "__main__":
    unittest.main()
