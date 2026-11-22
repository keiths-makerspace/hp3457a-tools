"""Goal-3 tests: cal-RAM map vs ROM, real CAL in emulation, power-fail detection, PEEK dump.

Slow (~2-3 min wall): boots the ROM several times and runs CAL/ACAL.
Run:  python -m unittest tests.test_cal -v
"""
import os
import random
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
ROM = os.path.join(os.path.dirname(__file__), "..", "firmware", "main-controller-66511",
                   "03457-88810_U602.bin")
FUNC_CODE = {"DCV": 0x00, "ACV": 0x04, "DCI": 0x08, "ACI": 0x0C, "OHM": 0x10, "OHMF": 0x11}
CLASS = {0x00: 0, 0x04: 1, 0x08: 2, 0x0C: 3, 0x10: 4, 0x11: 5}


def make_valid(mem, seed=7):
    from calimage import records, checksum
    rnd = random.Random(seed)
    for _, a, size in records():
        rec = bytearray(rnd.randrange(256) for _ in range(size - 1)) + b"\0"
        rec[-1] = checksum(rec)
        mem[a:a + size] = rec
    g = bytes(rnd.randrange(256) for _ in range(4))
    mem[0x18B:0x18F] = g
    mem[0x18F:0x193] = bytes(x ^ 0xFF for x in g)
    mem[0x193], mem[0x194] = 0x05, 0xFA


def nr3(b):
    return float(b.decode().split(",")[0])


@unittest.skipUnless(os.path.exists(ROM), "ROM image not present")
class TestCalMap(unittest.TestCase):
    def test_map_and_checksum_match_rom(self):
        from calimage import records, checksum, record_addr
        from forthcall import ForthMachine
        fm = ForthMachine(ROM)
        rnd = random.Random(1)
        for name, a, size in records():
            if name == "AC shared":
                continue
            func = FUNC_CODE[name.split()[0]]
            rng = next(r for r in range(-5, 9) if record_addr(CLASS[func], r, size) == a)
            data = bytes(rnd.randrange(256) for _ in range(size))
            fm.bus.mem[a:a + size] = data
            fm.bus.mem[0x342], fm.bus.mem[0x33E] = func, rng & 0xFF
            self.assertEqual(fm.call(0x8D85), [a], name)               # ROM record address
            self.assertEqual(fm.call(0x8E97), [checksum(data)], name)  # ROM checksum


@unittest.skipUnless(os.path.exists(ROM), "ROM image not present")
class TestCalEmulated(unittest.TestCase):
    def boot(self, mem_init=None, counts=1234):
        from emu3457 import Board
        b = Board(ROM)
        if mem_init:
            mem_init(b.mem)
        b.inguard.counts = counts
        b.power_on()
        b.run(seconds=7)
        b.hpib_write("TRIG HOLD\r\n")
        b.hpib_read(seconds=0.3)
        return b

    def test_cal_zero_writes_record(self):
        from calimage import checksum
        b = self.boot()
        b.hpib_write("DCV 3\r\n")
        b.run(seconds=0.5)
        n, r = len(b.prot_writes), len(b.resets)
        b.hpib_write("CAL 0,0\r\n", seconds=1)
        b.run(seconds=6)
        w = [(a, v) for _, a, v in b.prot_writes[n:] if a != 0x1FD]
        self.assertEqual([a for a, _ in w], [0x5F, 0x60, 0x5E, 0x5D, 0x5C, 0x60, 0x1FE, 0x1FF])
        self.assertEqual(len(b.resets) - r, 5)                         # 5 protected cycles
        rec = bytes(b.mem[0x56:0x61])
        self.assertEqual(rec[6:9], (1234).to_bytes(3, "big"))           # FRONT offset slot
        self.assertEqual(rec[-1], checksum(rec))
        b.hpib_write("TRIG HOLD\r\n")
        b.hpib_read(seconds=0.3)
        self.assertEqual(nr3(b.hpib_query("CALNUM?\r\n")), 1)

    def test_peek_dump_matches_ram(self):
        from calimage import dump_via_peek, validate
        b = self.boot(mem_init=make_valid)
        img = dump_via_peek(lambda c: b.hpib_query(c + "\r\n"))
        self.assertEqual(img[0x40:0x200], bytes(b.mem[0x40:0x200]))
        self.assertEqual(validate(img)[1], [])

    def test_powerfail_between_constant_and_checksum_is_detected(self):
        from calimage import validate
        b = self.boot(mem_init=make_valid, counts=777)
        b.hpib_write("ACAL ALL\r\n", seconds=1)
        b.run(seconds=40)
        b.hpib_write("TRIG HOLD\r\n")
        b.hpib_read(seconds=0.3)
        b.hpib_write("DCV 3\r\n")
        b.run(seconds=0.5)
        b.hpib_query("ERR?\r\n")
        b.gpib.host_listen(b"CAL 0,0\r\n")
        b.run(seconds=30, until=lambda bd: any(a == 0x5C for _, a, _ in bd.prot_writes)
              and bd.cpu.pc == 0x8B97)
        self.assertEqual(len(validate(bytes(b.mem[:0x200]))[1]), 1)    # DCV 3V checksum stale
        b.power_cycle(2.0, out1_powerup=0)
        b.run(seconds=9)
        b.hpib_write("TRIG HOLD\r\n")
        b.hpib_read(seconds=0.2)
        b.hpib_read(seconds=0.2)
        b.hpib_query("ERR?\r\n")
        errs = {}
        for f in ("DCV 30", "DCV 3", "OHM 3000"):
            b.hpib_write(f + "\r\n")
            b.run(seconds=0.5)
            errs[f] = int(nr3(b.hpib_query("ERR?\r\n")))
        self.assertEqual(errs, {"DCV 30": 0, "DCV 3": 512, "OHM 3000": 0})


if __name__ == "__main__":
    unittest.main()
