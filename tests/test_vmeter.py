"""Virtual input (tools/vinput.py), the keyboard map and the virtual-meter thread
(tools/vmeter.py, without opening a window).

Run:  python -m unittest tests.test_vmeter -v      (~2 min)
"""
import os
import socket
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
ROM = os.path.join(os.path.dirname(__file__), "..", "firmware", "main-controller-66511",
                   "03457-88810_U602.bin")


@unittest.skipUnless(os.path.exists(ROM), "ROM image not present")
class TestVirtualInput(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import display3457
        from prologix_server import prepare_board
        from vinput import VirtualInput
        cls.vi = VirtualInput(dcv=1.2345678, acv=0.5, dci=0.0123, ohm=4700.0, freq=1234.5)
        cls.b = prepare_board(ROM, None, True, None)
        cls.b.inguard.source = cls.vi
        cls.dec, cls.lcd = display3457.attach(cls.b)
        cls.b.power_on()
        cls.b.run(seconds=8)
        cls.b.hpib_query("ID?\n")       # first command after boot (see EMULATOR.md)

    def setUp(self):
        self.vi.set("dcv", 1.2345678)
        self.vi.set("ohm", 4700.0)
        self.b.hpib_write("TRIG AUTO;DCV AUTO;NPLC 10;NDIG 6\n")
        self.b.run(seconds=1.0)

    def reading(self, cmd=None, settle=1.2):
        if cmd:
            self.b.hpib_write(cmd + "\n")
        self.b.run(seconds=settle)
        r = float(self.b.hpib_query("TRIG SGL\n"))
        self.b.hpib_write("TRIG AUTO\n")
        return r

    def test_1_dcv_scaling(self):
        self.assertAlmostEqual(self.reading(), 1.234568, places=6)
        self.assertEqual(self.lcd.text(), "+1.234568 VDC")
        self.assertAlmostEqual(self.reading("DCV 30"), 1.23457, places=5)   # 30 V resolution
        self.assertAlmostEqual(self.reading("DCV 3;NPLC 0.0005"), 1.235, places=3)

    def test_2_dcv_autorange(self):
        for v, rng in ((0.0123456, -2), (-250.0, 2), (0.00012345, -2), (2.5, 0)):
            self.vi.set("dcv", v)
            r = self.reading(settle=1.5)
            self.assertAlmostEqual(r, v, delta=abs(v) * 1e-5, msg=f"{v} V")
            self.assertEqual(self.b.mem[0x033E] - (256 if self.b.mem[0x033E] > 127 else 0), rng)

    def test_3_overload(self):
        self.vi.set("dcv", 400.0)
        self.assertGreater(self.reading(settle=1.5), 9e37)
        self.assertIn("O", self.lcd.text())
        self.vi.set("dcv", 1.0)
        self.assertGreater(self.reading("DCV 0.3"), 9e37)              # fixed range

    def test_4_other_functions(self):
        self.assertAlmostEqual(self.reading("ACV"), 0.5, places=6)
        self.assertAlmostEqual(self.reading("DCI"), 0.0123, places=8)
        self.assertAlmostEqual(self.reading("OHM"), 4700.0, places=2)
        self.assertAlmostEqual(self.reading("FREQ"), 1234.5, delta=0.01)
        self.assertAlmostEqual(self.reading("PER"), 1 / 1234.5, delta=1e-9)
        self.vi.set("ohm", 1e15)
        self.assertGreater(self.reading("OHM"), 9e37)                   # open circuit

    def test_5_front_panel_keys(self):
        self.b.press_key(2, 0)              # LOCAL: HP-IB left the meter in remote, keys locked
        self.b.press_key(0, 7)                                          # DCI
        self.b.run(seconds=1.5)
        self.assertTrue(self.lcd.text().endswith("MADC"), self.lcd.text())
        for rc in ((1, 2), (4, 7)):                                     # NDIG, 6
            self.b.press_key(*rc)
        self.assertEqual(self.lcd.text().rstrip(), "NDIG 6_")
        self.b.press_key(5, 3)                                          # ENTER
        self.b.press_key(0, 3)                                          # DCV
        self.b.run(seconds=1.5)
        self.assertEqual(self.lcd.text(), "+1.234568 VDC")


@unittest.skipUnless(os.path.exists(ROM), "ROM image not present")
class TestMeterThread(unittest.TestCase):
    def test_keys_and_prologix_share_one_meter(self):
        import display3457
        from prologix_server import prepare_board
        from vinput import VirtualInput
        from vmeter import Meter
        vi = VirtualInput(dcv=2.5)
        b = prepare_board(ROM, None, True, None)
        b.inguard.source = vi
        dec, lcd = display3457.attach(b)
        b.power_on()
        b.run(seconds=8)
        m = Meter(b, lcd, prologix=0)
        m.start()
        try:
            deadline = time.time() + 10
            while m.port is None and time.time() < deadline:
                time.sleep(0.05)
            self.assertIsNotNone(m.port, m.server_note or m.fault)
            s = socket.create_connection(("127.0.0.1", m.port), timeout=60)
            s.sendall(b"++addr 22\nEND OFF\n")  # throw-away first command (remote transition)
            time.sleep(1.0)
            s.sendall(b"TRIG HOLD\n")
            time.sleep(0.5)
            s.sendall(b"++read\n")              # drain the reading left from TRIG AUTO
            s.settimeout(1.5)
            end = time.time() + 20
            try:
                while s.recv(4096) and time.time() < end:
                    pass
            except socket.timeout:
                pass
            s.sendall(b"++loc\n")               # GTL: front panel back in control
            time.sleep(0.3)
            m.post("down", (0, 6))              # OHM key from the "GUI"
            time.sleep(0.3)
            m.post("up", (0, 6))
            deadline = time.time() + 60
            while not lcd.text().endswith("OHM") and time.time() < deadline:
                time.sleep(0.1)
            self.assertTrue(lcd.text().endswith("OHM"), lcd.text())
            s.settimeout(60)
            # back into remote: the first command can be lost (BAD HEADER; EMULATOR.md)
            s.sendall(b"END OFF\n")
            time.sleep(1.0)
            s.sendall(b"ID?\n++read\n")
            buf = b""
            while not buf.endswith(b"\n"):
                c = s.recv(1)
                if not c:
                    break
                buf += c
            self.assertEqual(buf, b"HP3457A\r\n")
            s.close()
        finally:
            m.stopping = True
            m.join(5)
        self.assertIsNone(m.fault)


if __name__ == "__main__":
    unittest.main()
