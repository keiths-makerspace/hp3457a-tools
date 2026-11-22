"""The emulator's 6809 cycle counts agree with lwasm's tables for every opcode and mode
(tools/cyclecheck.py). Timed loops in the diagnostic ROM depend on this.

Run:  python -m unittest tests.test_cycles -v      (a few seconds; skipped without lwasm)
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
from asm import have_lwasm  # noqa: E402


@unittest.skipUnless(have_lwasm(), "lwasm not available (see CLAUDE.md)")
class TestCycles(unittest.TestCase):
    def test_every_opcode_matches_lwasm(self):
        from cyclecheck import check
        rows, bad = check()
        self.assertGreater(len(rows), 250)
        self.assertEqual(bad, [])


if __name__ == "__main__":
    unittest.main()
