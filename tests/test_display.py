"""Goal-6 tests: display-extension patch (tools/dispx_patch.py) on the emulated board, with a
model of the upgraded-display MCU (display3457.DisplayMcu). Images go to a temp dir.

Run:  python -m unittest tests.test_display -v      (~1 min)
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
ROM = os.path.join(os.path.dirname(__file__), "..", "firmware", "main-controller-66511",
                   "03457-88810_U602.bin")


def nr3(b):
    return float(b.decode().split(",")[0])


@unittest.skipUnless(os.path.exists(ROM), "ROM image not present")
class TestDisplayExtension(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from dispx_patch import build
        cls.path = build(ROM).save(os.path.join(tempfile.mkdtemp(), "dispx.bin"))

    def boot(self, rom=None, counts=12345, keys=(), **mcu_kw):
        from emu3457 import Board
        from display3457 import attach, attach_mcu
        b = Board(rom or self.path)
        dec, lcd = attach(b)
        mcu = attach_mcu(b, dec, **mcu_kw)
        b.inguard.counts = counts
        b.keys.update(keys)
        b.power_on()
        b.run(seconds=7)
        b.keys.clear()
        return b, lcd, mcu

    def test_detected_and_full_resolution_record(self):
        b, lcd, mcu = self.boot()
        self.assertEqual(mcu.probes, 1)
        self.assertFalse(mcu.pulling)                       # released after the handshake
        self.assertEqual(b.aux_err(), 0)
        self.assertEqual(b.mem[0x0557], 0)                  # no phantom key from the probe
        self.assertGreater(len(mcu.records), 3)
        rec = mcu.records[-1][1]
        self.assertEqual((rec["version"], rec["fpres"], rec["function"]), (1, 0, 0))
        self.assertEqual(lcd.text().strip(), "+0.00124  VDC")      # glass: NDIG 5 rounding
        self.assertGreater(lcd.ignored, 0)                  # LCD ignored the F7 traffic
        b.hpib_write("TRIG HOLD\r\n")
        b.hpib_read(seconds=0.3)
        self.assertEqual(nr3(b.hpib_query("XDISP?\r\n")), 1)
        rdg = nr3(b.hpib_query("TRIG SGL\r\n", seconds=1.5))
        self.assertAlmostEqual(rec["value"], rdg, places=12)        # record == HP-IB reading
        self.assertEqual(rec["counts"], 1235)               # one digit more than the glass

    def test_absent_display_changes_nothing(self):
        b, lcd, mcu = self.boot(present=False)
        self.assertEqual(b.mem[0x0765], 0)
        self.assertEqual(mcu.records, [])
        self.assertEqual(lcd.text().strip(), "+0.00124  VDC")
        b.hpib_write("TRIG HOLD\r\n")
        b.hpib_read(seconds=0.3)
        self.assertEqual(nr3(b.hpib_query("XDISP?\r\n")), 0)
        b.hpib_query("ERR?\r\n")                            # clear boot errors (blank cal RAM)
        b.hpib_write("XDISP 1\r\n")                         # can be forced on
        self.assertEqual(nr3(b.hpib_query("XDISP?\r\n")), 1)
        self.assertEqual(nr3(b.hpib_query("ERR?\r\n")), 0)

    def test_slow_mcu_and_held_key(self):
        from emu3457 import E_HZ
        b, _, mcu = self.boot(latency=int(0.020 * E_HZ), keys={(0, 0), (3, 2)})
        self.assertEqual(b.mem[0x0765], 1)
        b, _, mcu = self.boot(latency=int(0.050 * E_HZ))    # too slow: not detected
        self.assertEqual(b.mem[0x0765], 0)

    def test_keyboard_and_xprobe_after_boot(self):
        b, _, mcu = self.boot()
        b.hpib_write("TRIG HOLD\r\n")
        b.hpib_read(seconds=0.3)
        b.hpib_write("KEY?\r\n")
        b.run(seconds=0.2)
        b.press_key(2, 5)
        self.assertEqual(nr3(b.hpib_read()), ((2 + 1) * 8 + 5) * 2)
        mcu.present = False                                 # display unplugged
        b.hpib_write("XPROBE\r\n")
        b.run(seconds=0.5)
        self.assertEqual(nr3(b.hpib_query("XDISP?\r\n")), 0)

    def test_stock_rom_never_probes(self):
        b, _, mcu = self.boot(rom=ROM)
        self.assertEqual((mcu.probes, len(mcu.records), mcu.pulling), (0, 0, False))


if __name__ == "__main__":
    unittest.main()
