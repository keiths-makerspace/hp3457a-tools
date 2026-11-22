"""Diagnostic ROM (diag/diag.asm), assembled with lwasm and run in the emulator, with RAM and
ROM faults injected to prove each failure message.

Run:  python -m unittest tests.test_diag -v      (~1.5 min; skipped if lwasm isn't available)
"""
import os
import random
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "tools"))
SRC = os.path.join(HERE, "..", "diag", "diag.asm")
ROM = os.path.join(HERE, "..", "firmware", "main-controller-66511", "03457-88810_U602.bin")

from asm import have_lwasm  # noqa: E402

REFRESH = [0xFC, 0xB8, 0x0A, 0x1A, 0xBC, 0x2A, 0xC8]
# regular expressions: the NMI and line readings vary by a count of the timing loop
PASS_SEQ = ["HP3457A DIAG", "ROM CHECKSUM", "ROM OK      ", "RAM DATA BUS", "RAM ADDR BUS",
            "RAM CELLS   ", "RAM OK      ", "CAL RAM     ", "CAL RAM OK  ", "NMI AND LINE",
            r"NMI 32\.8[3-6]\dMS", r"LINE (59\.9\d|60\.0\d)HZ", "LINK TEST   ", "A/D RESET OK",
            "ECHO OK     ", "WATCHDOG    ", r"WDOG  52[4-7]MS ", "HPIB U614   ",
            "HPIB U614 OK", "KEYBOARD    ", "NO KEY STUCK", "JUMPER NORM ", "PRESS KEYS  ",
            "DISPLAY TEST"]
NMI_OK = r"NMI 32\.8[3-6]\dMS"
LINE60 = r"LINE (59\.9\d|60\.0\d)HZ"
SCRATCH = range(0x0200, 0x0223)          # HP PAD: stage 5-6 variables, RAM message buffer
WORK = (set(range(0x029F, 0x02F0))      # stages 3-4: buffer, lists, counters (HP data stack)
        | set(range(0x0270, 0x0288))      # stage 4: NMI stack frames (HP return stack)
        | set(range(0x02F1, 0x02FF))      # stages 7-9 variables (HP display buffer)
        | set(range(0x0289, 0x0296))      # stage 10 variables (HP data stack)
        | {0x03D0})                       # stage 10: the HP link state STAT (emulator hint)


def ph2_periods(board, seconds):
    ts, prev = [], [0]

    def f(c, v):
        if v & 4 and not prev[0] & 4:
            ts.append(c)
        prev[0] = v
    board.out2_listeners.append(f)
    board.run(seconds=seconds)
    board.out2_listeners.remove(f)
    return [y - x for x, y in zip(ts, ts[1:])]


@unittest.skipUnless(have_lwasm(), "lwasm not available (see CLAUDE.md)")
class TestDiag(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from asm import build
        from lwmap import Symbols
        cls.tmp = tempfile.mkdtemp()
        cls.out = build(SRC, os.path.join(cls.tmp, "diag.bin"), checksum=True)
        cls.syms = Symbols(cls.out["sym"])
        cls.img = open(cls.out["bin"], "rb").read()

    def run_diag(self, seconds, faults=None, rom_flip=None, prep="valid_cal"):
        """Boot the image with random RAM contents; returns (board, decoder, lcd, ram_before,
        messages): messages = committed LCD snapshots, consecutive duplicates removed."""
        import display3457
        from emu3457 import Board
        b = Board(self.out["bin"])
        if rom_flip is not None:
            b.mem[rom_flip] ^= 0x01
        rnd = random.Random(7)
        for a in range(0x40, 0x2000):
            b.mem[a] = rnd.randrange(256)
        if prep == "valid_cal":                 # synthetic cal records that pass their checks
            from coverage import make_valid
            make_valid(b.mem)
        elif callable(prep):
            prep(b)
        before = bytes(b.mem)
        b.ram_faults = faults
        b.io_log = []
        dec, lcd = display3457.attach(b)
        b.power_on()
        b.run(seconds=seconds)
        msgs = []
        for _, text, ann in lcd.history:
            if not msgs or msgs[-1][0] != text:
                msgs.append((text, ann))
        return b, dec, lcd, before, msgs

    def assert_only_wdog_test_resets(self, b, lcd):
        """Resets other than power-on come only from the stage-6 watchdog test, and the ROM
        never had to report an unexpected restart."""
        self.assertTrue(all(r[1] == "watchdog" for r in b.resets[1:]), b.resets)
        self.assertNotIn("RESTARTED   ", [t for _, t, _ in lcd.history])

    def test_image(self):
        from fix_checksum import word_sum
        img = self.img
        self.assertEqual(len(img), 0x10000)
        self.assertEqual(word_sum(img), 0)                       # HP's checksum convention
        self.assertEqual(img[0xFFFE] << 8 | img[0xFFFF], self.syms["reset"])   # RESET vector
        self.assertEqual(img[:0x2000], b"\xff" * 0x2000)         # nothing below $2000

    def test_message_table(self):
        lo, hi = self.syms["msgtab"], self.syms["msgtabend"]
        self.assertEqual((hi - lo) % 14, 0)
        for a in range(lo, hi, 14):
            e = self.img[a:a + 14]
            self.assertEqual(e[0], 0, hex(a))                       # sentinel
            self.assertTrue(all(0x20 <= c < 0x60 for c in e[1:13]), e)  # LCD-codable text
            self.assertIn(e[13], (0, 2))                             # flags: none or ERR

    def test_clean_board_loops_and_restores_ram(self):
        b, dec, lcd, before, msgs = self.run_diag(58)   # first pass ~33 s, then ~20 s
        texts = [t for t, _ in msgs]
        for got, want in zip(texts, PASS_SEQ):
            self.assertRegex(got, "^" + want + "$")
        self.assertGreaterEqual(len(texts), len(PASS_SEQ))
        self.assertIn("ALL PASSED  ", texts)
        self.assertIn("PASS    0001", texts)
        self.assertIn("PASS    0002", texts)                        # second loop, count kept
        self.assertEqual([f[1] for f in dec.frames[:7]], REFRESH)
        self.assert_only_wdog_test_resets(b, lcd)
        self.assertTrue(all(not ann & 2 for _, ann in msgs if ann != 0x0FFF))  # no ERR
        # (0x0FFF: the display walk lights every annunciator on purpose)
        changed = [a for a in range(0x40, 0x2000) if b.mem[a] != before[a]]
        self.assertTrue(set(changed) <= set(SCRATCH) | WORK, [hex(a) for a in changed])
        io = {a for _, _, _, a, _ in b.io_log}
        self.assertEqual(io, {0x00, 0x08, 0x10, 0x18,             # UART U616
                              0x23, 0x25, 0x26,                     # 8291A SPM/SPS, aux, ADR0
                              0x38, 0x3A, 0x3C})                    # IN1, OUT1, OUT2
        self.assertFalse(io & set(range(0x30, 0x38)))               # never CALREQ ($30-$37)

    def test_no_writes_to_cal_ram(self):
        b, dec, lcd, before, msgs = self.run_diag(8)
        self.assertEqual(bytes(b.mem[0x40:0x200]), before[0x40:0x200])
        self.assertEqual(b.blocked_writes, 0)                       # never even tried

    def assert_halts_on(self, text, seconds, **kw):
        b, dec, lcd, before, msgs = self.run_diag(seconds, **kw)
        self.assertEqual(msgs[-1], (text, 0x002), msgs)             # message with ERR lit
        self.assertNotIn("ALL PASSED  ", [t for t, _ in msgs])
        self.assertEqual(b.resets, [(0, "power-on")])

    def test_rom_checksum_failure(self):
        self.assert_halts_on("ROM CKSM BAD", 4, rom_flip=0x3000)

    def test_data_line_failures(self):
        from emu3457 import RamFaults
        for bit in (0, 3, 7):
            for level in (0, 1):
                with self.subTest(bit=bit, level=level):
                    self.assert_halts_on("RAM D%d BAD  " % bit, 4,
                                         faults=RamFaults(data_stuck={bit: level}))

    def test_address_line_failures(self):
        from emu3457 import RamFaults
        for line in (0, 6, 9, 12):
            for level in (0, 1):
                with self.subTest(line=line, level=level):
                    self.assert_halts_on(("RAM A%d BAD" % line).ljust(12), 5,
                                         faults=RamFaults(addr_stuck={line: level}))

    def test_cell_failure_shows_the_address(self):
        from emu3457 import RamFaults
        self.assert_halts_on("RAM 0A3F BAD", 7,
                             faults=RamFaults(cell_stuck={0x0A3F: (0x04, 0)}))
        self.assert_halts_on("RAM 1FFF BAD", 7,
                             faults=RamFaults(cell_stuck={0x1FFF: (0x80, 0x80)}))
        self.assert_halts_on("RAM PAD BAD ", 6,                     # inside the scratch buffer
                             faults=RamFaults(cell_stuck={0x0212: (0x10, 0)}))

    def test_bad_cell_at_a_test_address_is_not_a_line_fault(self):
        """A bad cell where the data-line test runs ($0400) or at an address-test cell
        ($0AAA base, $1EFF partner of base $1FFF) is reported as that cell, not as a line."""
        from emu3457 import RamFaults
        self.assert_halts_on("RAM 0400 BAD", 5,
                             faults=RamFaults(cell_stuck={0x0400: (0x08, 0)}))
        self.assert_halts_on("RAM 0AAA BAD", 5,
                             faults=RamFaults(cell_stuck={0x0AAA: (0x01, 1)}))
        self.assert_halts_on("RAM 1EFF BAD", 5,
                             faults=RamFaults(cell_stuck={0x1EFF: (0x20, 0x20)}))

    # ---- stage 3: cal RAM, read only ---------------------------------------------------
    def cal_messages(self, prep, seconds=10):
        b, dec, lcd, before, msgs = self.run_diag(seconds, prep=prep)
        texts = [t for t, _ in msgs]
        i = texts.index("CAL RAM     ")
        self.assertEqual(bytes(b.mem[0x40:0x200]), before[0x40:0x200])     # never written
        return msgs[i + 1:]

    def test_cal_ok(self):
        out = self.cal_messages("valid_cal")
        self.assertEqual(out[0], ("CAL RAM OK  ", 0))
        self.assertEqual(out[1], ("NMI AND LINE", 0))

    def test_cal_bad_records_are_named(self):
        def prep(b):
            from coverage import make_valid
            make_valid(b.mem)
            b.mem[0x0056 + 3] ^= 0x10          # DCV 3 V record
            b.mem[0x0154 + 1] ^= 0x01          # OHMF 30 kohm record
            b.mem[0x0194] ^= 0x01              # trim complement
        out = self.cal_messages(prep, 44)
        self.assertEqual([t for t, _ in out[:4]], ["CAL BAD   03", "CAL DCV 3V  ",
                                                   "CAL OHMF 30K", "CAL TRIM    "])
        self.assertTrue(all(a & 2 for _, a in out[:4]))                    # ERR lit
        self.assertIn(("CAL ERRORS  ", 2), out)                            # end of the pass

    def test_cal_lost(self):
        out = self.cal_messages(None, 40)       # random RAM: battery lost
        self.assertEqual(out[0], ("CAL RAM LOST", 2))
        self.assertIn(("CAL ERRORS  ", 2), out)

    def test_cal_table_matches_the_hp_record_map(self):
        """The diag table holds the 32 records of docs/CALIBRATION.md (w_8D85 map), the
        shared AC record and the two complement-checked items."""
        lo = self.syms["caltab"]
        items = []
        a = lo
        while self.img[a] or self.img[a + 1]:
            items.append((self.img[a] << 8 | self.img[a + 1], self.img[a + 2], self.img[a + 3]))
            a += 5
        self.assertEqual(len(items), 35)
        recs = [(addr, size) for addr, size, t in items if t == 0]
        self.assertEqual(len(recs), 33)
        self.assertIn((0x0077, 5), recs)
        self.assertEqual([i for i in items if i[2] == 1], [(0x018B, 4, 1), (0x0193, 1, 1)])
        for addr, size in recs:                 # records never overlap and stay in cal RAM
            self.assertTrue(0x40 <= addr and addr + size <= 0x180, hex(addr))
        spans = sorted((addr, addr + size) for addr, size in recs)
        self.assertTrue(all(x[1] <= y[0] for x, y in zip(spans, spans[1:])))

    # ---- stage 4: NMI tick and line frequency ------------------------------------------
    def timer_messages(self, setup=None, **board_kw):
        import display3457
        from coverage import make_valid
        from emu3457 import Board
        b = Board(self.out["bin"], **board_kw)
        make_valid(b.mem)
        if setup:
            setup(b)
        dec, lcd = display3457.attach(b)
        b.power_on()
        b.run(seconds=12)
        msgs = []
        for _, text, ann in lcd.history:
            if not msgs or msgs[-1][0] != text:
                msgs.append((text, ann))
        i = [t for t, _ in msgs].index("NMI AND LINE")
        self.assert_only_wdog_test_resets(b, lcd)
        self.assertEqual(b.out1, 0)                                 # NMI gate closed again
        return msgs[i + 1:i + 4]

    def test_timers_60_and_50_hz(self):
        out = self.timer_messages()
        self.assertRegex(out[0][0], NMI_OK)
        self.assertRegex(out[1][0], LINE60)
        self.assertEqual([a for _, a in out[:2]], [0, 0])
        self.assertEqual(out[2], ("LINK TEST   ", 0))
        out = self.timer_messages(line_hz=50)
        self.assertRegex(out[1][0], r"LINE (49\.9\d|50\.0\d)HZ")

    def test_no_nmi_tick(self):
        out = self.timer_messages(lambda b: setattr(b, "nmi_dead", True))
        self.assertEqual(out[0], ("NO NMI TICK ", 2))
        self.assertRegex(out[1][0], LINE60)

    def test_line_sense_stuck(self):
        for level in ("low", "high"):
            with self.subTest(level=level):
                out = self.timer_messages(lambda b: setattr(b, "line_sense", level))
                self.assertEqual(out[1], ("NO LINE SENS", 2))

    def test_crystal_off_shows_in_line_frequency(self):
        """The NMI is E/65536, so it can't show a wrong crystal; mains can. A 3 % fast E
        clock fits 3 % more cycles into each line period."""
        def fast(b):
            b.line_half = int(b.line_half * 1.03)
        out = self.timer_messages(fast)
        self.assertRegex(out[0][0], NMI_OK)
        self.assertRegex(out[1][0], r"LINE 58\.[1-3]\dHZ")
        self.assertEqual(out[1][1], 2)

    # ---- stage 5: cross-isolation link ----------------------------------------------------
    def link_messages(self, setup=None, n=3):
        """Messages after "LINK TEST" (consecutive duplicates removed)."""
        import display3457
        from coverage import make_valid
        from emu3457 import Board
        b = Board(self.out["bin"])
        make_valid(b.mem)
        if setup:
            setup(b)
        dec, lcd = display3457.attach(b)
        b.power_on()
        b.run(seconds=16)
        msgs = []
        for _, text, ann in lcd.history:
            if not msgs or msgs[-1][0] != text:
                msgs.append((text, ann))
        i = [t for t, _ in msgs].index("LINK TEST   ")
        self.assert_only_wdog_test_resets(b, lcd)
        return msgs[i + 1:i + 1 + n]

    def test_link_ok(self):
        self.assertEqual(self.link_messages(),
                         [("A/D RESET OK", 0), ("ECHO OK     ", 0), ("WATCHDOG    ", 0)])

    def test_link_silent(self):
        """Dead in either direction, 8051 absent or held in reset: no self-test byte."""
        for name, setup in (
                ("to main dead", lambda b: setattr(b.link, "to_main", "dead")),
                ("to 8051 dead", lambda b: setattr(b.link, "to_adc", "dead")),
                ("8051 RXD low", lambda b: setattr(b.link, "to_adc", "stuck_low")),
                ("8051 absent", lambda b: setattr(b.inguard, "absent", True))):
            with self.subTest(name):
                self.assertEqual(self.link_messages(setup, 2),
                                 [("A/D SILENT  ", 2), ("CHK OPTO PWR", 2)])

    def test_link_rx_held_low(self):
        out = self.link_messages(lambda b: setattr(b.link, "to_main", "stuck_low"), 1)
        self.assertEqual(out, [("LINK RX LOW ", 2)])

    def test_link_slow_edges_name_the_direction(self):
        rx = self.link_messages(lambda b: setattr(b.link, "to_main", "slow_rise"), 2)
        self.assertEqual(rx, [("ECHO ERR 255", 2), ("SLOW EDGE RX", 2)])
        tx = self.link_messages(lambda b: setattr(b.link, "to_adc", "slow_rise"), 3)
        self.assertEqual(tx[0], ("A/D RESET OK", 0))
        self.assertEqual(tx[1][0][:9], "ECHO ERR ")
        self.assertEqual(tx[2], ("SLOW EDGE TX", 2))

    def test_link_noise(self):
        for d in ("to_main", "to_adc"):
            with self.subTest(d):
                out = self.link_messages(lambda b: setattr(b.link, d, "noise"), 3)
                self.assertEqual(out[0], ("A/D RESET OK", 0))
                self.assertEqual(out[2], ("LINK NOISY  ", 2))

    def test_8051_faults(self):
        bad = self.link_messages(lambda b: setattr(b.inguard, "selftest_ok", False), 2)
        self.assertEqual(bad, [("A/D TEST BAD", 2), ("ECHO OK     ", 0)])
        bit = self.link_messages(lambda b: setattr(b.inguard, "echo_ok", False), 3)
        self.assertEqual(bit, [("A/D RESET OK", 0), ("ECHO ERR 255", 2), ("ECHO BIT 6  ", 2)])

    def test_uart_missing(self):
        out = self.link_messages(lambda b: setattr(b, "uart_dead", True), 1)
        self.assertEqual(out, [("NO UART U616", 2)])

    # ---- stage 6: watchdog, and restarts ------------------------------------------------
    def wdog_messages(self, setup=None, seconds=20, cut_at=None):
        import display3457
        from coverage import make_valid
        from emu3457 import Board
        b = Board(self.out["bin"])
        make_valid(b.mem)
        if setup:
            setup(b)
        dec, lcd = display3457.attach(b)
        b.power_on()
        if cut_at:
            b.run(seconds=cut_at)
            b.power_cycle(1.0)
        b.run(seconds=seconds)
        msgs = []
        for _, text, ann in lcd.history:
            if not msgs or msgs[-1][0] != text:
                msgs.append((text, ann))
        return b, msgs

    def after(self, msgs, text, n):
        i = [t for t, _ in msgs].index(text)
        return msgs[i + 1:i + 1 + n]

    def test_watchdog_ok(self):
        b, msgs = self.wdog_messages()
        out = self.after(msgs, "WATCHDOG    ", 2)
        self.assertRegex(out[0][0], r"WDOG  52[4-7]MS ")
        self.assertEqual(out[1], ("HPIB U614   ", 0))
        self.assertEqual([r[1] for r in b.resets], ["power-on", "watchdog"])

    def test_watchdog_faults(self):
        from emu3457 import E_HZ
        b, msgs = self.wdog_messages(lambda b: setattr(b, "watchdog_cycles", None))
        self.assertEqual(self.after(msgs, "WATCHDOG    ", 1), [("WDOG NO RST ", 2)])
        b, msgs = self.wdog_messages(lambda b: setattr(b, "watchdog_cycles", int(0.1 * E_HZ)))
        self.assertEqual(self.after(msgs, "WATCHDOG    ", 2),
                         [("WDOG  100MS ", 2), ("WDOG EARLY  ", 2)])
        b, msgs = self.wdog_messages(lambda b: setattr(b, "watchdog_cycles", int(1.0 * E_HZ)))
        self.assertEqual(self.after(msgs, "WATCHDOG    ", 1), [("WDOG 1000MS ", 2)])

    def test_restart_in_the_middle_of_a_pass_is_reported(self):
        b, msgs = self.wdog_messages(seconds=6, cut_at=3.0)          # cut during stage 2
        texts = [t for t, _ in msgs]
        i = texts.index("RESTARTED   ")
        self.assertEqual(msgs[i + 1], ("IN STAGE 2  ", 2))
        self.assertEqual(texts[i + 2], "HP3457A DIAG")              # then a fresh pass

    def test_kick_interval(self):
        """Apart from the deliberate stage-6 gap, the watchdog is kicked at least every 25 ms,
        except for one NMI tick (32.8 ms, HP's own kick interval) while timing it."""
        from emu3457 import Board
        from coverage import make_valid
        b = Board(self.out["bin"])
        make_valid(b.mem)
        kicks = []
        b.out2_listeners.append(lambda c, v: kicks.append(c) if v & 0x80 else None)
        b.power_on()
        b.run(seconds=17)
        resets = [r[0] for r in b.resets[1:]]
        gaps = [c1 - c0 for c0, c1 in zip(kicks, kicks[1:])
                if not any(c0 < r <= c1 for r in resets)]
        self.assertLessEqual(max(gaps), 66000)                      # one NMI tick + a little
        self.assertLessEqual(sorted(gaps)[-4], 50100)               # everything else <= 25 ms

    # ---- stage 10: the HP self-test's analog steps --------------------------------------
    def stage10_branches(self, cases, line_hz=60):
        """Run once to the end of the display walk, then for each case restore that state,
        apply the case's set-up and run stage 10. Returns {case: (messages, board log)}."""
        import display3457
        from coverage import make_valid
        from emu3457 import Board
        b = Board(self.out["bin"], line_hz=line_hz)
        make_valid(b.mem)
        dec, lcd = display3457.attach(b)
        b.power_on()
        b.run(seconds=40, until=lambda bd: any(t == "DISPLAY TEST" for _, t, _ in lcd.history))
        b.run(seconds=40, until=lambda bd: bd.cpu.pc == self.syms["stage10"])
        snap = b.snapshot()
        out = {}
        for name, setup in cases.items():
            b.restore(snap)
            n0, l0 = len(lcd.history), len(b.inguard.log)
            setup(b)
            kicks = []
            b.out2_listeners.append(lambda c, v: kicks.append(c) if v & 0x80 else None)
            b.run(seconds=25, until=lambda bd: bd.cpu.pc == self.syms["passend"])
            b.out2_listeners.pop()
            msgs = []
            for _, text, ann in lcd.history[n0:]:
                if not msgs or msgs[-1][0] != text:
                    msgs.append((text, ann))
            gaps = [y - x for x, y in zip(kicks, kicks[1:])]
            out[name] = (msgs, b.inguard.log[l0:], max(gaps), b)
        return out

    def test_stage10_messages(self):
        def faults(*f):
            return lambda b: setattr(b.inguard, "analog_faults", set(f))

        def acal_lost(b):
            b.mem[0x050C:0x0523] = bytes(0x17)
        all7 = ("integrator", "frontend_zero", "reference", "amps", "ac_offset", "ac",
                "ohms_precharge")
        r = self.stage10_branches({
            "ok": faults(),
            "ac": faults("ac"),
            "precharge": faults("ohms_precharge"),
            "all": faults(*all7),
            "acal lost": acal_lost,
            "absent": lambda b: setattr(b.inguard, "absent", True),
        })

        def texts(k):
            return [t for t, _ in r[k][0]]
        self.assertEqual(texts("ok"), ["A/D SELFTEST", "A/D TEST OK "])
        self.assertEqual(r["ac"][0][1:], [("AC CONVERTER", 2), ("AUXERR   256", 2)])
        self.assertEqual(r["precharge"][0][1:], [("OHMS PRECHG ", 2), ("AUXERR   512", 2)])
        self.assertEqual(texts("all")[1:], ["INTEGRATOR  ", "FRONTEND ZRO", "REF GAIN DIV",
                                            "AMPS TEST   ", "AC AMP DC OS", "AC CONVERTER",
                                            "OHMS PRECHG ", "AUXERR  1016"])
        self.assertEqual(texts("acal lost"), ["A/D SELFTEST", "ACAL REQ'D  ", "A/D TEST OK "])
        self.assertEqual(texts("absent"), ["A/D SELFTEST", "A/D SKIPPED "])
        for k, (msgs, log, gap, b) in r.items():
            self.assertLessEqual(gap, 50100, k)                 # kicked at least every 25 ms
            self.assertFalse(b.inguard.internal, k)             # 8051 reset at the end
        self.assertEqual(bytes(r["ok"][3].mem[0x40:0x200]), bytes(r["acal lost"][3].mem[0x40:0x200]))

    def test_key_window_every_pass_and_autocal(self):
        """The PRESS KEYS window: 10 s on the first pass, 3 s on later ones. On a later pass
        stage 10 is skipped unless AUTO CAL is pressed in the window; then a fault injected
        after the first pass is found on that pass."""
        import display3457
        from coverage import make_valid
        from emu3457 import Board, E_HZ
        b = Board(self.out["bin"])
        make_valid(b.mem)
        dec, lcd = display3457.attach(b)
        b.power_on()

        def when(text, n=1):
            hits = [c for c, t, _ in lcd.history if t == text]
            return hits[n - 1] if len(hits) >= n else None
        b.run(seconds=90, until=lambda bd: when("PRESS KEYS  ", 2) is not None)
        first = (when("DISPLAY TEST") - when("PRESS KEYS  ")) / E_HZ
        self.assertAlmostEqual(first, 10.2, delta=0.5)          # 0.2 s message + 10 s
        snap, c0 = b.snapshot(), when("PRESS KEYS  ", 2)
        out = {}
        for name, press in (("none", False), ("autocal", True)):
            b.restore(snap)
            n0 = len(lcd.history)
            b.inguard.analog_faults = {"ac"}
            if press:
                b.run(seconds=0.5)
                b.press_key(3, 6)                               # AUTO CAL (row 3, column 6)
            b.run(seconds=40, until=lambda bd: bd.cpu.pc == self.syms["passend"])
            out[name] = [t for _, t, _ in lcd.history[n0:]]
            if not press:                                       # 0.2 s message + 3 s
                self.assertAlmostEqual((b.cpu.cycles - c0) / E_HZ, 3.2, delta=0.4)
        self.assertNotIn("A/D SELFTEST", out["none"])
        texts = out["autocal"]
        self.assertIn("KEY AUTOCAL ", texts)
        self.assertIn("AC CONVERTER", texts)
        self.assertIn("AUXERR   256", texts)

    def test_stage10_sends_the_hp_bytes(self):
        """Byte for byte what the HP ROM sends for TEST, and the same replies, at 50 Hz (the
        mode frames' bit 4 must not follow the line: STEST sets USLINE)."""
        from coverage import make_valid
        from emu3457 import Board

        def wire(log):
            w = [(e[1], e[2]) for e in log if e[1] in ("rx", "tx")]
            i = next(k for k in range(len(w) - 1) if w[k] == ("rx", 0x1B) and w[k + 1] == ("rx", 0xFE))
            j = max(k for k in range(len(w)) if w[k] == ("rx", 0x3F)) + 4
            return w[i:j]
        hp = Board(ROM, line_hz=50)
        make_valid(hp.mem)
        hp.power_on()
        hp.run(seconds=8)
        hp.hpib_query("ID?\n")
        n0 = len(hp.inguard.log)
        hp.hpib_write("TEST\n")
        hp.run(seconds=12)
        want = wire(hp.inguard.log[n0:])
        got = wire(self.stage10_branches({"ok": lambda b: None}, line_hz=50)["ok"][1])
        self.assertEqual(len([1 for k, _ in want if k == "rx"]), 243)
        self.assertEqual(got, want)

    # ---- stages 7-9 ---------------------------------------------------------------------
    def late_messages(self, setup=None, seconds=34, press=None):
        """Committed LCD snapshots from "HPIB U614" on (raw display-walk frames included)."""
        import display3457
        from coverage import make_valid
        from emu3457 import Board
        b = Board(self.out["bin"])
        make_valid(b.mem)
        if setup:
            setup(b)
        dec, lcd = display3457.attach(b)
        b.power_on()
        if press:
            b.run(seconds=seconds, until=lambda bd: any(t == "PRESS KEYS  "
                                                        for _, t, _ in lcd.history))
            b.run(seconds=0.5)
            b.press_key(*press)
        b.run(seconds=seconds if not press else 4)
        snaps = []
        for _, text, ann in lcd.history:
            if not snaps or snaps[-1] != (text, ann):
                snaps.append((text, ann))
        i = [t for t, _ in snaps].index("HPIB U614   ")
        self.assert_only_wdog_test_resets(b, lcd)
        return b, snaps[i + 1:]

    def test_hpib_ok_and_missing(self):
        b, out = self.late_messages(seconds=22)
        self.assertEqual(out[0], ("HPIB U614 OK", 0))
        self.assertEqual(b.gpib.spm, 0)                              # left with SPM = 0
        self.assertEqual(b.gpib.adr_raw, [0x60, 0x60])               # talker/listener off
        b, out = self.late_messages(lambda b: setattr(b, "gpib_dead", True), seconds=22)
        self.assertEqual(out[0], ("U614 NO RESP", 2))

    def test_keyboard(self):
        b, out = self.late_messages(lambda b: b.stuck_keys.add((3, 1)), seconds=24)  # NPLC
        texts = [t for t, _ in out]
        i = texts.index("KEY STUCK   ")
        self.assertEqual(texts[i + 1], "KEY NPLC    ")
        self.assertNotIn("PRESS KEYS  ", texts)                    # no window with a stuck key
        b, out = self.late_messages(lambda b: setattr(b, "secure_jumper", "KEY"), seconds=24)
        self.assertIn("JUMPER KEY  ", [t for t, _ in out])
        b, out = self.late_messages(lambda b: setattr(b, "secure_jumper", "DSA"), seconds=24)
        self.assertIn("JUMPER DSA  ", [t for t, _ in out])
        b, out = self.late_messages(press=(5, 3), seconds=26)        # ENT in the window
        texts = [t for t, _ in out]
        self.assertEqual(texts[texts.index("PRESS KEYS  ") + 1], "KEY ENT     ")

    def test_display_walk(self):
        b, out = self.late_messages(seconds=44)
        i = [t for t, _ in out].index("DISPLAY TEST")
        walk = out[i + 1:i + 1 + 1 + 12 + 12]
        allon = walk[0]
        self.assertEqual(allon[1], 0x0FFF)                           # every annunciator
        self.assertEqual(len(allon[0]), 24)                          # 12 characters + punctuation
        for k in range(12):                                          # one position at a time
            text, ann = walk[1 + k]
            self.assertEqual(ann, 0)
            self.assertNotEqual(text.split()[0], "")
            self.assertEqual(len(text) - len(text.lstrip(" ")), k, (k, text))
        anns = [a for _, a in walk[13:25]]
        self.assertEqual(anns, [1 << k for k in range(11, -1, -1)])  # SMPL ... SHIFT
        self.assertTrue(all(t.strip() == "" for t, _ in walk[13:25]))

    def test_bus_timing_not_faster_than_hp(self):
        import display3457
        from emu3457 import Board
        b = Board(self.out["bin"])
        display3457.attach(b)
        b.power_on()
        self.assertGreaterEqual(min(ph2_periods(b, 1.0)), 36)      # HP ROM: never below 36

    @unittest.skipUnless(os.path.exists(ROM), "HP ROM image not present")
    def test_hp_rom_refresh_order(self):
        """The HP full refresh ($719F) calls its register writers in this order: $71EA (0A),
        $7215 (1A), $7290 (BC), $7244 (2A). (It isn't used during boot; DISP_DUMP is.)"""
        rom = open(ROM, "rb").read()
        calls = []
        for at in (0x71D4, 0x71D7, 0x71DA, 0x71DD):
            self.assertEqual(rom[at], 0x17)                         # LBSR
            target = (at + 3 + int.from_bytes(rom[at + 1:at + 3], "big", signed=True)) & 0xFFFF
            k = rom.index(0xC6, target, target + 6)                 # first LDB #opcode
            calls.append(rom[k + 1])
        self.assertEqual(calls, [0x0A, 0x1A, 0xBC, 0x2A])


if __name__ == "__main__":
    unittest.main()
