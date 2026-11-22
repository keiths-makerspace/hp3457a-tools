"""Goal-5 tests: patch builder - new HP-IB commands in the zero block, protected write via the
$8E41 trampoline, FREE_BOT RAM reservation. Patched images go to a temp dir.

Run:  python -m unittest tests.test_patch -v      (~30 s)
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
class TestPatch(unittest.TestCase):
    def boot(self, path, ramw=False):
        from emu3457 import Board
        b = Board(path)
        if ramw:
            b.ram_writes = bytearray(0x2000)
        b.power_on()
        b.run(seconds=7)
        b.hpib_write("TRIG HOLD\r\n")
        b.hpib_read(seconds=0.3)
        return b

    def test_new_commands_and_protected_write(self):
        from forthpatch import Patch
        from fix_checksum import word_sum
        p = Patch(ROM)
        p.prot_write_word()
        p.command("ZZPWR", [0x1234, 0x04FA, "!", 0x5678, 0x04FC, "!", 0x8765, 0x04FE, "!",
                            0x4321, 0x0500, "!", 0x5A, 0x0180, 1, "PROT-WRITE",
                            "0.", 0x04FA, "2!", "0.", 0x04FE, "2!", "DONE_CMD~9"])
        p.command("ZZ?", [0x0180, "C@", "TOS>CUST", "DONE_CMD~9"])
        path = p.save(os.path.join(tempfile.mkdtemp(), "zz.bin"))
        img = open(path, "rb").read()
        self.assertEqual(word_sum(img), 0)
        stock = open(ROM, "rb").read()
        diff = [a for a in range(0x2000, 0x10000) if img[a] != stock[a]]
        self.assertTrue(all(0x3051 <= a <= 0x385E or a in (0x804E, 0x804F, 0xFFF0, 0xFFF1)
                            for a in diff))
        b = self.boot(path)
        self.assertEqual(b.aux_err(), 0)
        self.assertEqual(nr3(b.hpib_query("ZZ?\r\n")), 0)
        self.assertEqual(b.hpib_query("ID?\r\n"), b"HP3457A\r\n")      # old commands still found
        r0 = len(b.resets)
        b.hpib_write("ZZPWR\r\n", seconds=1)
        b.run(seconds=3)
        self.assertEqual([w for _, w in b.resets[r0:]], ["watchdog"])
        self.assertEqual(b.mem[0x0180], 0x5A)
        self.assertEqual(b.aux_err() & 0x4000, 0)
        b.hpib_write("TRIG HOLD\r\n")
        b.hpib_read(seconds=0.3)
        self.assertEqual(nr3(b.hpib_query("ZZ?\r\n")), 0x5A)

    def test_free_bot_reservation(self):
        from forthpatch import Patch
        p = Patch(ROM)
        xt = p.xt("FREE_BOT")
        self.assertEqual(bytes(p.img[xt + 2:xt + 4]), b"\x07\x65")
        p.img[xt + 2:xt + 4] = (0x0765 + 0x1D0).to_bytes(2, "big")
        b = self.boot(p.save(os.path.join(tempfile.mkdtemp(), "fb.bin")), ramw=True)
        for c in ("MSIZE 3000", "MEM FIFO", "TRIG SGL", "TRIG SGL", "MEM OFF", "SUB 1", "DCV 30",
                  "SUBEND", "CALL 1", "SSTATE 2", "RSTATE 2"):
            b.hpib_write(c + "\r\n")
            b.run(seconds=0.4)
        self.assertEqual(sum(b.ram_writes[0x0765:0x0935]), 0)
        self.assertEqual(b.aux_err(), 0)


if __name__ == "__main__":
    unittest.main()
