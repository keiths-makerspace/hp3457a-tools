"""Goal-4 tests: ROM checksum algorithm, fix_checksum.py, and power-on behaviour of patched images.

Run:  python -m unittest tests.test_checksum -v      (~25 s: boots two images)
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
ROM = os.path.join(os.path.dirname(__file__), "..", "firmware", "main-controller-66511",
                   "03457-88810_U602.bin")


@unittest.skipUnless(os.path.exists(ROM), "ROM image not present")
class TestChecksum(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rom = open(ROM, "rb").read()
        cls.tmp = tempfile.mkdtemp()

    def path(self, name, data):
        p = os.path.join(self.tmp, name)
        open(p, "wb").write(data)
        return p

    def patched(self):
        img = bytearray(self.rom)
        img[0x3100:0x3104] = b"\xDE\xAD\xBE\xEF"          # inside the zero block
        return bytes(img)

    def test_original_is_balanced(self):
        from fix_checksum import word_sum, emu_check
        self.assertEqual(word_sum(self.rom), 0)
        self.assertEqual(emu_check(ROM), 0)

    def test_fix_matches_rom_routine(self):
        from fix_checksum import word_sum, fix, emu_check
        bad = self.patched()
        self.assertNotEqual(word_sum(bad), 0)
        self.assertNotEqual(emu_check(self.path("bad.bin", bad)), 0)
        good, comp = fix(bad)
        self.assertEqual(word_sum(good), 0)
        self.assertEqual(emu_check(self.path("good.bin", good)), 0)
        self.assertEqual(good[:0xFFF0], bad[:0xFFF0])         # only the compensation word moved
        self.assertEqual(good[0xFFF2:], bad[0xFFF2:])
        good2, _ = fix(bad, at=0x3200)                          # any even address works
        self.assertEqual(emu_check(self.path("good2.bin", good2)), 0)
        with self.assertRaises(ValueError):
            fix(bad, at=0x3201)

    def test_power_on_aux_err(self):
        from emu3457 import Board
        from fix_checksum import fix
        bad = self.patched()
        for img, expect in ((bad, 1024), (fix(bad)[0], 0)):
            b = Board(self.path("boot.bin", img))
            b.power_on()
            b.run(seconds=5)
            self.assertEqual(b.aux_err() & 1024, expect)


if __name__ == "__main__":
    unittest.main()
