"""Emulated Prologix GPIB-ETHERNET adapter (tools/prologix_server.py): a real TCP client
talks to the ROM in the emulator, using the command sequence WinGPIB's IODevices DLL sends.

Run:  python -m unittest tests.test_prologix -v      (~1.5 min)
"""
import os
import socket
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
ROM = os.path.join(os.path.dirname(__file__), "..", "firmware", "main-controller-66511",
                   "03457-88810_U602.bin")


@unittest.skipUnless(os.path.exists(ROM), "ROM image not present")
class TestPrologix(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import tempfile
        from prologix_server import PrologixAdapter, make_board, serve
        from coverage import make_valid
        img = bytearray(0x2000)
        make_valid(img, seed=11)
        cls.cal = bytes(img[0x40:0x200])                 # WinGPIB 3457A .bin layout
        cls.cal_path = os.path.join(tempfile.mkdtemp(), "3457ACalram_test.bin")
        with open(cls.cal_path, "wb") as f:
            f.write(cls.cal)
        cls.b = make_board(ROM, counts=12345, cal_bin=cls.cal_path)
        cls.ad = PrologixAdapter(cls.b, 22)
        cls.stop = False
        port = []
        cls.t = threading.Thread(target=serve, daemon=True, kwargs=dict(
            adapter=cls.ad, host="127.0.0.1", port=0, realtime=False, log=lambda m: None,
            stop=lambda: cls.stop, ready=port.append))
        cls.t.start()
        while not port:
            time.sleep(0.01)
        cls.s = socket.create_connection(("127.0.0.1", port[0]), timeout=60)
        cls.send("++addr 22\n++eoi 1\n++eot_enable 1\n++eot_char 10\n")   # as IODevices
        # The first data after the meter goes remote can be lost while the ROM finishes the
        # remote transition (BAD HEADER; docs/EMULATOR.md). Send a throw-away command first
        # (END OFF is the power-on default; a blank line wouldn't reach the bus).
        cls.send("END OFF\n")
        time.sleep(0.5)
        cls.send("TRIG HOLD\n")
        cls.drain()                 # the reading left over from TRIG AUTO at power-on

    @classmethod
    def drain(cls, idle=1.5):
        """++read and discard everything until the line goes quiet. A stale reading would
        otherwise shift every later reply by one (why WinGPIB sends 10 dummy PEEKs first)."""
        cls.send("++read\n")
        cls.s.settimeout(idle)
        end = time.time() + 20          # a meter still in TRIG AUTO would stream forever
        try:
            while cls.s.recv(4096) and time.time() < end:
                pass
        except socket.timeout:
            pass

    @classmethod
    def tearDownClass(cls):
        cls.s.close()
        cls.stop = True
        cls.t.join(5)

    @classmethod
    def send(cls, text):
        cls.s.sendall(text.encode() if isinstance(text, str) else text)

    def line(self, timeout=30):
        """Read until LF (the meter ends replies with CR LF; no EOI by default)."""
        buf = b""
        end = time.time() + timeout
        while not buf.endswith(b"\n") and time.time() < end:
            self.s.settimeout(max(0.1, end - time.time()))
            try:
                buf += self.s.recv(1)
            except socket.timeout:
                break
        return buf

    def query(self, cmd):
        self.send(cmd + "\n++read\n")
        return self.line()

    def test_1_identity_and_config(self):
        self.send("++ver\n")
        self.assertIn(b"Prologix", self.line())
        self.send("++addr\n")
        self.assertEqual(self.line(), b"22\r\n")
        self.assertEqual(self.query("ID?"), b"HP3457A\r\n")

    def test_2_wingpib_style_cal_dump(self):
        """CalRam3457.vb: TRIG 4, 10 dummy PEEKs, then PEEK every even address 64..511;
        Hex(Val(reply)) keeps the low 16 bits, written high byte first."""
        self.send("TRIG 4\n")
        for _ in range(10):
            self.query("PEEK 64")
        out = bytearray()
        for a in range(64, 512, 2):
            v = int(float(self.query(f"PEEK {a}").decode()))
            out += (v & 0xFFFF).to_bytes(2, "big")
        self.assertEqual(bytes(out), self.cal)          # the --cal-bin file, byte for byte

    def test_3_serial_poll_and_srq(self):
        self.send("RQS 4\nSRQ\n++srq\n")
        self.assertEqual(self.line(), b"1\r\n")
        self.send("++spoll\n")
        self.assertTrue(int(self.line()) & 64)
        # The ROM services the poll (SPC) and rewrites SPM; under load it can re-raise the
        # request once before its status bit is cleared, so poll until it settles.
        srq = None
        for _ in range(4):
            self.send("++srq\n")
            srq = self.line()
            if srq == b"0\r\n":
                break
            self.send("++spoll\n")
            self.line()
            time.sleep(0.2)
        self.assertEqual(srq, b"0\r\n")
        self.send("RQS 0\n")

    def test_4_get_triggers_a_reading(self):
        self.send("TRIG HOLD\n++trg\n++read\n")
        self.assertAlmostEqual(float(self.line().decode()), 1.235e-3, places=9)

    def test_5_device_clear_then_query(self):
        self.send("++clr\n")
        self.assertEqual(self.query("ID?"), b"HP3457A\r\n")
        self.send("TRIG HOLD\n")
        self.drain()

    def test_6_eoi_and_eot(self):
        self.send("END ALWAYS\nID?\n++read eoi\n")
        self.assertEqual(self.line(), b"HP3457A\r\n")
        self.assertEqual(self.line(timeout=5), b"\n")                # eot_char after EOI
        self.send("END OFF\n")

    def test_7_absent_address(self):
        self.send("++addr 5\nID?\n++read\n++spoll\n++addr 22\n++addr\n")
        self.assertEqual(self.line(), b"22\r\n")                     # nothing from address 5

    def test_parser_escapes(self):
        from prologix_server import PrologixAdapter
        ad = PrologixAdapter(self.b)            # parser only; never ticked
        ad.feed(b"\x1b+\x1b+not a command\r\n++addr 9\nDISP 2,\"A\x1b\nB\"\n")
        self.assertEqual(ad.lines, [(False, b"++not a command"), (True, b"++addr 9"),
                                    (False, b"DISP 2,\"A\nB\"")])


if __name__ == "__main__":
    unittest.main()
