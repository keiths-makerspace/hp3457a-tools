#!/usr/bin/env python3
"""Virtual HP 3457A: the real main-controller ROM in the emulator, with a front panel
(LCD + keyboard), a virtual signal source at the input terminals, and optionally the
emulated Prologix GPIB-ETHERNET adapter on the same meter (so WinGPIB can talk to it while
you press keys).

    python3 tools/vmeter.py [--blank-cal | --cal-bin dump.bin] [--nvram build/nv.bin] [--faults]
                            [--rom build/dispx.bin] [--prologix [PORT]] [--addr 22]
                            [--dcv 1.2345] [--noise 2] [--line-hz 60] [--fast-conv]

Keyboard: digits . - , e  Enter  BackSpace  Shift (SHIFT key)  Up/Down (range)
          Left/Right (scroll)  F1-F6 DCV ACV OHM DCI ACI FREQ  Esc (LOCAL)
The front-panel layout is reconstructed from the ROM's key tables (docs/VMETER.md); it is
not a photograph of the real panel.

Only tkinter (stdlib) is needed. Emulation runs at about real time on a fast PC; the speed is
shown in the status bar.
"""
import argparse
import math
import os
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from emu3457 import DEFAULT_ROM, E_HZ  # noqa: E402
import display3457  # noqa: E402
from vinput import VirtualInput, OPEN, kind_of, sbyte  # noqa: E402

# ---------------------------------------------------------------------------------------
# Keyboard: (row, col) in the matrix (IN1 bit, OUT1 bit), legend, shifted legend.
# Derived from the ROM key table at $FE03 and the lookup tables it uses (docs/VMETER.md).
# Physical rows follow the menu letters and keypad digits: matrix rows 3, 4, 1, 5 and
# columns 1, 6, 5, 2 | 4, 0, 7 | 3.
FUNCTION_KEYS = [((0, 3), "DCV", ""), ((0, 2), "ACV", "ACDCV"), ((0, 6), "OHM", "OHMF"),
                 ((0, 7), "DCI", ""), ((0, 0), "ACI", "ACDCI"), ((0, 1), "FREQ", "PER")]
# Row order from the operating manual's photo (fig. 1-7): four arrows (RANGE, DISPLAY),
# LOCAL, SHIFT. Which arrow sits where within each pair is not legible there (verify).
CONTROL_KEYS = [((2, 3), "▲", "AUTO"), ((2, 2), "▼", "HOLD"),
                ((2, 6), "◄", "TEST"), ((2, 7), "►", "RESET"), ((2, 0), "LOCAL", "ADRS")]
SHIFT_KEY = (2, 1)
# Legends as on the real panel (operating manual fig. 1-7); groups CONFIGURATION, TRIGGER,
# INPUT/SCANNER, READING MEMORY + STATE.
GROUPS = ["CONFIGURATION", "TRIGGER", "INPUT / SCANNER", "READING MEMORY      STATE"]
GRID = [
    [((3, 1), "NPLC", "A"), ((3, 6), "AUTO\nCAL", "B"), ((3, 5), "OFFSET\nCOMP", "C"),
     ((3, 2), "AUTO\nZERO", "D"),
     ((3, 4), "7", "MATH"), ((3, 0), "8", "RECALL"), ((3, 7), "9", "STORE"), ((3, 3), "E", "SRQ")],
    [((4, 1), "AUTO", "E"), ((4, 6), "SINGLE", "F"), ((4, 5), "DELAY", "I"), ((4, 2), "NRDGS", "L"),
     ((4, 4), "4", "R MEAN"), ((4, 0), "5", "R SDEV"), ((4, 7), "6", "R NSAMP"), ((4, 3), "−", "ERROR")],
    [((1, 1), "TERM", "M"), ((1, 6), "CHAN", "N"), ((1, 5), "SCAN\nADV", "O"), ((1, 2), "DIGITS\nDISP", "P"),
     ((1, 4), "1", "NULL"), ((1, 0), "2", "dB"), ((1, 7), "3", "STAT"), ((1, 3), "BACK\nSPACE", "CLEAR")],
    [((5, 1), "CONFIG", "R"), ((5, 6), "RECALL", "S"), ((5, 5), "STORE", "T"), ((5, 2), "RECALL", "?"),
     ((5, 4), "0", "OFF"), ((5, 0), ".", "CONT"), ((5, 7), ",", "FILTER"), ((5, 3), "ENT", "LAST ENTRY")],
]
HOTKEYS = {"7": (3, 4), "8": (3, 0), "9": (3, 7), "4": (4, 4), "5": (4, 0), "6": (4, 7),
           "1": (1, 4), "2": (1, 0), "3": (1, 7), "0": (5, 4), "period": (5, 0), "comma": (5, 7),
           "minus": (4, 3), "e": (3, 3), "E": (3, 3), "Return": (5, 3), "KP_Enter": (5, 3),
           "BackSpace": (1, 3), "Up": (2, 3), "Down": (2, 2), "Left": (2, 6), "Right": (2, 7),
           "Escape": (2, 0), "Shift_L": SHIFT_KEY, "Shift_R": SHIFT_KEY,
           "F1": (0, 3), "F2": (0, 2), "F3": (0, 6), "F4": (0, 7), "F5": (0, 0), "F6": (0, 1),
           "KP_0": (5, 4), "KP_1": (1, 4), "KP_2": (1, 0), "KP_3": (1, 7), "KP_4": (4, 4),
           "KP_5": (4, 0), "KP_6": (4, 7), "KP_7": (3, 4), "KP_8": (3, 0), "KP_9": (3, 7),
           "KP_Decimal": (5, 0), "KP_Subtract": (4, 3)}
ANNUNCIATORS = ["SHIFT", "ERR", "REAR", "MATH", "MRNG", "AZOFF", "4WΩ", "AC+DC", "ADRS",
                "SRQ", "REM", "SMPL"]        # bit k of the BC word (docs/DISPLAY.md); the glass
                                             # shows them right to left (manual p. 3-57: SMPL first)
MIN_HOLD = 0.08                 # s of emulated time a key is held at least (scan + debounce)

# ---------------------------------------------------------------------------------------
# 14-segment glyphs. Segments: a top, b upper right, c lower right, d bottom, e lower left,
# f upper left, g/h middle left/right, i/j/k upper diagonal-left/vertical/diagonal-right,
# l/m/n lower diagonal-left/vertical/diagonal-right.
SEGS = {
    "a": ((0, 0), (1, 0)), "b": ((1, 0), (1, .5)), "c": ((1, .5), (1, 1)), "d": ((0, 1), (1, 1)),
    "e": ((0, .5), (0, 1)), "f": ((0, 0), (0, .5)), "g": ((0, .5), (.5, .5)),
    "h": ((.5, .5), (1, .5)), "i": ((0, 0), (.5, .5)), "j": ((.5, 0), (.5, .5)),
    "k": ((1, 0), (.5, .5)), "l": ((0, 1), (.5, .5)), "m": ((.5, .5), (.5, 1)),
    "n": ((.5, .5), (1, 1)),
}
GLYPHS = {
    " ": "", "0": "abcdef", "1": "bc", "2": "abdegh", "3": "abcdh", "4": "bcfgh",
    "5": "acdfgh", "6": "acdefgh", "7": "abc", "8": "abcdefgh", "9": "abcdfgh",
    "A": "abcefgh", "B": "abcdhjm", "C": "adef", "D": "abcdjm", "E": "adefg", "F": "aefg",
    "G": "acdefh", "H": "bcefgh", "I": "adjm", "J": "bcde", "K": "efgkn", "L": "def",
    "M": "bcefik", "N": "bcefin", "O": "abcdef", "P": "abefgh", "Q": "abcdefn", "R": "abefghn",
    "S": "acdfgh", "T": "ajm", "U": "bcdef", "V": "efkl", "W": "bcefln", "X": "ikln",
    "Y": "ikm", "Z": "adkl", "+": "ghjm", "-": "gh", "*": "ghijklmn", "/": "kl", "\\": "in",
    "=": "dgh", "<": "kn", ">": "il", "(": "kn", ")": "il", "[": "adef", "]": "abcd",
    "^": "lnj", "_": "d", "?": "abhm", "'": "j", '"': "fj", "$": "acdfghjm", "@": "abdefhj",
    "#": "bcdghjm", "%": "cfghjklm", "&": "adegikn", "!": "bc", ",": "l", ".": "", ":": "",
    ";": "l",
}


def key_names():
    """{'DCV (r0c3)': (0, 3), ...} for every key, from the tables above."""
    out = {}
    for pos, lab, _ in FUNCTION_KEYS + CONTROL_KEYS + [k for row in GRID for k in row]:
        out["%s (r%dc%d)" % (lab.replace("\n", " "), pos[0], pos[1])] = pos
    out["SHIFT (r%dc%d)" % SHIFT_KEY] = SHIFT_KEY
    return out


class Meter(threading.Thread):
    """Runs the emulated meter (and the Prologix server) in one thread, paced to real time.
    The GUI talks to it only through post()."""

    def __init__(self, board, lcd, prologix=None, host="127.0.0.1", addr=22, log=print):
        super().__init__(daemon=True)
        self.b, self.lcd = board, lcd
        self.events = queue.Queue()
        self.stopping = False
        self.fault = None
        self.prologix = prologix
        self.host, self.addr = host, addr
        self.log = log
        self.held = {}                  # key -> emulated time it went down
        self.release = set()            # keys released by the user, waiting for MIN_HOLD
        self.speed = 0.0
        self.server_note = ""
        self.port = None                # Prologix port actually bound
        self._mark = (time.monotonic(), board.cpu.cycles)

    def post(self, *ev):
        self.events.put(ev)

    @property
    def now(self):
        return self.b.cpu.cycles / E_HZ

    def _hook(self):
        while True:
            try:
                ev = self.events.get_nowait()
            except queue.Empty:
                break
            kind = ev[0]
            if kind == "down":
                self.b.keys.add(ev[1])
                self.held[ev[1]] = self.now
                self.release.discard(ev[1])
            elif kind == "up":
                self.release.add(ev[1])
            elif kind == "call":
                ev[1](self.b)
        for k in list(self.release):
            if self.now - self.held.get(k, 0) >= MIN_HOLD:
                self.b.keys.discard(k)
                self.release.discard(k)
                self.held.pop(k, None)
        time.sleep(0)                       # let the GUI thread in (GIL hand-over)
        t, c = time.monotonic(), self.b.cpu.cycles
        if t - self._mark[0] >= 1.0:
            self.speed = (c - self._mark[1]) / E_HZ / (t - self._mark[0])
            self._mark = (t, c)

    def run(self):
        while not self.stopping:
            try:
                if self.prologix is not None:
                    from prologix_server import PrologixAdapter, serve
                    ad = PrologixAdapter(self.b, self.addr)
                    self.server_note = f"Prologix on {self.host}:{self.prologix}, GPIB {self.addr}"
                    serve(ad, self.host, self.prologix, realtime=True,
                          log=lambda m: self._note(m), stop=lambda: self.stopping or self.fault,
                          hook=self._hook, ready=self._bound)
                else:
                    self._loop()
            except OSError as e:            # e.g. port in use: carry on without the server
                self.server_note = f"Prologix server failed: {e}"
                self.prologix = None
            except Exception as e:          # ROM went somewhere the emulator can't follow
                self.fault = f"{type(e).__name__}: {e}"
            while self.fault and not self.stopping:
                self._hook()                # a "call" event (power cycle) clears the fault
                time.sleep(0.05)

    def _bound(self, port):
        self.port = port
        self.server_note = f"Prologix on {self.host}:{port}, GPIB {self.addr}"

    def _note(self, m):
        if "client" in m:
            self.server_note = f"Prologix {self.host}:{self.prologix}: {m}"

    def _loop(self):
        t0, c0 = time.monotonic(), self.b.cpu.cycles
        while not (self.stopping or self.fault):
            self._hook()
            self.b.run(seconds=0.01)
            ahead = (self.b.cpu.cycles - c0) / E_HZ - (time.monotonic() - t0)
            if ahead > 0:
                time.sleep(ahead)
            elif ahead < -0.25:             # slower than real time: don't try to catch up
                t0, c0 = time.monotonic(), self.b.cpu.cycles


class LcdView(tk.Canvas):
    CW, CH, GAP, PAD = 30, 46, 10, 14
    ON, OFF, BG = "#1d2318", "#a9b49a", "#b6c2a6"

    def __init__(self, master):
        w = self.PAD * 2 + 12 * (self.CW + self.GAP)
        super().__init__(master, width=w, height=self.CH + 2 * self.PAD + 26, bg=self.BG,
                         highlightthickness=3, highlightbackground="#151515")
        self.state = None
        self.items = []
        self.lit = {}
        for i in range(12):
            x0 = self.PAD + i * (self.CW + self.GAP)
            segs = {s: self._seg(x0, self.PAD, s) for s in SEGS}
            dot = self.create_oval(x0 + self.CW + 2, self.PAD + self.CH - 5,
                                   x0 + self.CW + 7, self.PAD + self.CH, fill=self.OFF, width=0)
            tail = self.create_line(x0 + self.CW + 5, self.PAD + self.CH,
                                    x0 + self.CW + 1, self.PAD + self.CH + 7, fill=self.OFF, width=2)
            col = (self.create_oval(x0 + self.CW + 2, self.PAD + 12, x0 + self.CW + 7,
                                    self.PAD + 17, fill=self.OFF, width=0),
                   self.create_oval(x0 + self.CW + 2, self.PAD + 29, x0 + self.CW + 7,
                                    self.PAD + 34, fill=self.OFF, width=0))
            self.items.append((segs, dot, tail, col))
        self.ann = []
        y = self.PAD * 2 + self.CH + 10
        step = (w - 2 * self.PAD) / 12
        for k, name in enumerate(ANNUNCIATORS):
            self.ann.append(self.create_text(self.PAD + step * (11 - k + .5), y, text=name,
                                             fill=self.OFF, font=("Segoe UI", 8, "bold")))

    def _seg(self, x0, y0, s):
        (ax, ay), (bx, by) = SEGS[s]
        slant = 0.12 * self.CW

        def pt(x, y):
            return x0 + 3 + x * (self.CW - 6) + slant * (1 - y), y0 + 2 + y * (self.CH - 4)
        # shorten each segment a little so they read as separate bars
        sx, sy = pt(ax + (bx - ax) * .12, ay + (by - ay) * .12)
        ex, ey = pt(ax + (bx - ax) * .88, ay + (by - ay) * .88)
        return self.create_line(sx, sy, ex, ey, width=4, capstyle=tk.ROUND, fill=self.OFF)

    def _set(self, item, on):
        # only touch items that change: every Tk call has to win the GIL back from the
        # emulator thread, so a full repaint (~200 calls) would stall the GUI
        if self.lit.get(item) != on:
            self.lit[item] = on
            self.itemconfigure(item, fill=self.ON if on else self.OFF)

    def show(self, cells, annun):
        st = (tuple(cells), annun)
        if st == self.state:
            return
        self.state = st
        for (code, punct), (segs, dot, tail, col) in zip(cells, self.items):
            ch = display3457.CHARSET[code & 0x3F]
            on = GLYPHS.get(ch, "adgh")
            for s, item in segs.items():
                self._set(item, s in on)
            self._set(dot, punct in (1, 3))
            self._set(tail, punct == 3)
            for c in col:
                self._set(c, punct == 2)
        for k, item in enumerate(self.ann):
            self._set(item, bool(annun >> k & 1))


class Key(tk.Frame):
    """One key: shifted legend above (blue), cap below. Press/release go to the matrix."""

    def __init__(self, master, app, pos, label, shifted, width=7, cap="#efece4", fg="#222"):
        super().__init__(master, bg=app.PANEL)
        tk.Label(self, text=shifted, fg=app.LEGEND, bg=app.PANEL,
                 font=("Segoe UI", 7, "bold")).pack()
        self.cap = tk.Label(self, text=label, width=width, height=2, bg=cap, fg=fg,
                            relief=tk.RAISED, bd=2, font=("Segoe UI", 9, "bold"))
        self.cap.pack(padx=3, pady=(0, 4))
        self.cap.bind("<ButtonPress-1>", lambda e: app.key_down(pos))
        self.cap.bind("<ButtonRelease-1>", lambda e: app.key_up(pos))
        app.caps[pos] = self.cap


class App:
    PANEL = "#d9d7cf"              # colours from the front-panel photo (docs/photos/3457a.jpg)
    LEGEND = "#3a8fd6"
    GROUPFG = "#55554f"

    def __init__(self, root, meter, vi, nvram=None, faults_first=False):
        self.root, self.m, self.vi, self.nvram = root, meter, vi, nvram
        self._ticks = 0
        self.caps = {}
        self.down = set()
        root.title("Virtual HP 3457A (emulated ROM 03457-88810)")
        root.configure(bg=self.PANEL)
        left = tk.Frame(root, bg=self.PANEL)
        left.grid(row=0, column=0, padx=10, pady=10, sticky="n")
        self.lcd = LcdView(left)
        self.lcd.pack(pady=(0, 10))

        row = tk.Frame(left, bg=self.PANEL)
        row.pack(fill=tk.X)
        tk.Label(row, text="FUNCTION", fg=self.GROUPFG, bg=self.PANEL,
                 font=("Segoe UI", 7)).pack(anchor="w")
        for pos, lab, sh in FUNCTION_KEYS:
            Key(row, self, pos, lab, sh).pack(side=tk.LEFT)
        tk.Label(left, text="RANGE", fg=self.GROUPFG, bg=self.PANEL,
                 font=("Segoe UI", 7)).pack(anchor="w", padx=40, pady=(4, 0))
        row = tk.Frame(left, bg=self.PANEL)
        row.pack(fill=tk.X)
        for pos, lab, sh in CONTROL_KEYS:
            Key(row, self, pos, lab, sh).pack(side=tk.LEFT)
        Key(row, self, SHIFT_KEY, "", "", cap="#8ec3e6", fg="white").pack(side=tk.LEFT)
        lab = tk.Frame(left, bg=self.PANEL)
        lab.pack(fill=tk.X)
        tk.Label(lab, text="SCROLL", fg=self.GROUPFG, bg=self.PANEL, width=18,
                 font=("Segoe UI", 7)).pack(side=tk.LEFT)
        tk.Label(lab, text="DISPLAY", fg=self.GROUPFG, bg=self.PANEL, width=18,
                 font=("Segoe UI", 7)).pack(side=tk.LEFT)
        grid = tk.Frame(left, bg=self.PANEL)
        grid.pack(pady=(8, 0))
        for r, keys in enumerate(GRID):
            tk.Label(grid, text=GROUPS[r], fg=self.GROUPFG, bg=self.PANEL,
                     font=("Segoe UI", 7)).grid(row=2 * r, column=0, columnspan=4, sticky="w")
            if r == 0:
                tk.Label(grid, text="MATH", fg=self.GROUPFG, bg=self.PANEL,
                         font=("Segoe UI", 7)).grid(row=0, column=5, columnspan=3)
            for c, (pos, lab, sh) in enumerate(keys):
                digit = 4 <= c <= 6
                Key(grid, self, pos, lab, sh, width=6 if digit else 7,
                    cap="#f3f1ea" if digit else "#efece4").grid(row=2 * r + 1,
                                                               column=c + (c >= 4) + (c >= 7))
            tk.Frame(grid, width=10, bg=self.PANEL).grid(row=2 * r + 1, column=4)
            tk.Frame(grid, width=10, bg=self.PANEL).grid(row=2 * r + 1, column=8)

        nb = ttk.Notebook(root)
        nb.grid(row=0, column=1, padx=(0, 10), pady=10, sticky="n")
        tab_in, tab_f = ttk.Frame(nb), ttk.Frame(nb)
        nb.add(tab_in, text="Input terminals")
        nb.add(tab_f, text="Fault injection")
        self._inputs(tab_in)
        from faultpanel import FaultPanel
        self.faults = FaultPanel(tab_f, self, key_names())
        if faults_first:
            nb.select(tab_f)
        self.status = tk.Label(root, anchor="w", bg="#222", fg="#ddd", font=("Consolas", 9))
        self.status.grid(row=1, column=0, columnspan=2, sticky="ew")
        root.bind("<KeyPress>", self._kb_down)
        root.bind("<KeyRelease>", self._kb_up)
        root.protocol("WM_DELETE_WINDOW", self.close)
        self._tick()

    # ---- input terminals panel ------------------------------------------------------
    def _inputs(self, root):
        f = ttk.LabelFrame(root, text="Input terminals (virtual source)", padding=8)
        f.pack(fill="both", expand=True)
        self.vars = {}
        rows = [("dcv", "DC volts", "V"), ("acv", "AC volts (rms)", "V"),
                ("dci", "DC current", "A"), ("aci", "AC current (rms)", "A"),
                ("ohm", "Resistance", "Ω"), ("freq", "Frequency", "Hz")]
        for r, (k, name, unit) in enumerate(rows):
            ttk.Label(f, text=name).grid(row=r, column=0, sticky="w")
            v = tk.StringVar(value=self._fmt(self.vi.signals[k]))
            e = ttk.Entry(f, textvariable=v, width=14)
            e.grid(row=r, column=1, padx=4, pady=2)
            e.bind("<Return>", lambda ev, k=k: self._apply(k))
            e.bind("<FocusOut>", lambda ev, k=k: self._apply(k))
            ttk.Label(f, text=unit).grid(row=r, column=2, sticky="w")
            self.vars[k] = v
        self.open_var = tk.BooleanVar(value=self.vi.signals["ohm"] >= OPEN)
        ttk.Checkbutton(f, text="open circuit", variable=self.open_var,
                        command=lambda: self._apply("ohm")).grid(row=4, column=3, sticky="w")
        r = len(rows)
        ttk.Separator(f).grid(row=r, column=0, columnspan=4, sticky="ew", pady=6)
        ttk.Label(f, text="Noise (ppm of range, 1σ)").grid(row=r + 1, column=0, sticky="w")
        self.noise = tk.StringVar(value=str(self.vi.noise_ppm))
        e = ttk.Entry(f, textvariable=self.noise, width=14)
        e.grid(row=r + 1, column=1, padx=4)
        e.bind("<Return>", lambda ev: self._apply_noise())
        e.bind("<FocusOut>", lambda ev: self._apply_noise())
        ttk.Label(f, text="Sine wobble on the measured signal").grid(row=r + 2, column=0,
                                                                     columnspan=3, sticky="w")
        self.wob_amp = tk.StringVar(value="0")
        self.wob_per = tk.StringVar(value="10")
        ttk.Label(f, text="  amplitude (%)").grid(row=r + 3, column=0, sticky="w")
        ttk.Entry(f, textvariable=self.wob_amp, width=14).grid(row=r + 3, column=1, padx=4)
        ttk.Label(f, text="  period (s)").grid(row=r + 4, column=0, sticky="w")
        ttk.Entry(f, textvariable=self.wob_per, width=14).grid(row=r + 4, column=1, padx=4)
        ttk.Button(f, text="Apply wobble", command=self._apply_all).grid(row=r + 4, column=3)
        ttk.Separator(f).grid(row=r + 5, column=0, columnspan=4, sticky="ew", pady=6)
        self.measuring = ttk.Label(f, text="", font=("Consolas", 9), justify=tk.LEFT)
        self.measuring.grid(row=r + 6, column=0, columnspan=4, sticky="w")
        b = ttk.Frame(f)
        b.grid(row=r + 7, column=0, columnspan=4, sticky="w", pady=(10, 0))
        ttk.Button(b, text="Power cycle", command=self.power_cycle).pack(side=tk.LEFT)
        if self.nvram:
            ttk.Button(b, text="Save NV RAM", command=self.save_nv).pack(side=tk.LEFT, padx=6)
        ttk.Label(f, text="Keys: digits . - , e Enter BackSpace Shift arrows Esc=LOCAL\n"
                          "F1-F6 = DCV ACV OHM DCI ACI FREQ (click outside the entries first)",
                  foreground="#555").grid(row=r + 8, column=0, columnspan=4, sticky="w", pady=(8, 0))

    @staticmethod
    def _fmt(v):
        return "open" if v >= OPEN else f"{v:.10g}"

    def _parse(self, text):
        t = text.strip().lower().replace("ω", "")
        if t in ("open", "inf", ""):
            return OPEN
        mult = {"p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "m": 1e-3, "k": 1e3,
                "meg": 1e6, "g": 1e9}
        for suf in sorted(mult, key=len, reverse=True):
            if t.endswith(suf) and not t.endswith("e" + suf):
                try:
                    return float(t[:-len(suf)]) * mult[suf]
                except ValueError:
                    break
        return float(t)

    def _signal(self, k):
        if k == "ohm" and self.open_var.get():
            base = OPEN
        else:
            try:
                base = self._parse(self.vars[k].get())
            except ValueError:
                return None
        try:
            amp = float(self.wob_amp.get()) / 100.0
            per = max(0.01, float(self.wob_per.get()))
        except ValueError:
            amp, per = 0.0, 10.0
        if amp and base < OPEN:
            return lambda t, b=base: b * (1 + amp * math.sin(2 * math.pi * t / per))
        return base

    def _apply(self, k):
        v = self._signal(k)
        if v is not None:
            self.m.post("call", lambda b, k=k, v=v: self.vi.set(k, v))

    def _apply_all(self):
        for k in self.vars:
            self._apply(k)

    def _apply_noise(self):
        try:
            ppm = max(0.0, float(self.noise.get()))
        except ValueError:
            return
        self.m.post("call", lambda b: setattr(self.vi, "noise_ppm", ppm))

    # ---- keys -----------------------------------------------------------------------------
    def key_down(self, pos):
        if pos in self.down:
            return
        self.down.add(pos)
        self.caps[pos].configure(relief=tk.SUNKEN)
        self.m.post("down", pos)

    def key_up(self, pos):
        self.down.discard(pos)
        if pos in self.caps:
            self.caps[pos].configure(relief=tk.RAISED)
        self.m.post("up", pos)

    def _kb(self, e):
        if isinstance(e.widget, (tk.Entry, ttk.Entry)):
            return None
        return HOTKEYS.get(e.keysym) or HOTKEYS.get(e.char)

    def _kb_down(self, e):
        pos = self._kb(e)
        if pos:
            self.key_down(pos)

    def _kb_up(self, e):
        pos = self._kb(e)
        if pos:
            self.key_up(pos)

    # ---- periodic refresh -------------------------------------------------------------------
    def _tick(self):
        self.lcd.show(self.m.lcd.cells(), self.m.lcd.annun)
        b = self.m.b
        mem = b.mem
        kind = kind_of(mem[0x0342])
        last = self.vi.last
        txt = f"Measuring: {kind.upper():6s} range exp {sbyte(mem[0x033E]):+d}"
        txt += "  (autorange)" if mem[0x0341] else "  (fixed)"
        if last:
            v = last[1]
            txt += f"\nLast conversion: {'open' if v >= OPEN else f'{v:.7g}'}"
        txt += f"\nConversions: {b.inguard.readings}"
        if txt != self.measuring.cget("text"):
            self.measuring.configure(text=txt)
        st = f" emulated {self.m.now:8.1f} s   speed {self.m.speed:4.2f}x   "
        if len(b.resets) > 1:
            st += f"resets {len(b.resets) - 1} (last: {b.resets[-1][1]})   "
        st += self.m.server_note
        self._ticks += 1
        if self._ticks % 10 == 0:                # every 0.5 s: the ROM compare is not free
            self.faults.refresh(b)
        if self.m.fault:
            st = f" STOPPED - {self.m.fault}   (Power cycle to restart)"
        if st != self.status.cget("text"):
            self.status.configure(text=st, fg="#ff8080" if self.m.fault else "#ddd")
        self.root.after(50, self._tick)

    def power_cycle(self):
        def pc(b):
            b.keys.clear()
            b.power_cycle(1.0)
            self.m.fault = None
        self.m.post("call", pc)

    def save_nv(self):
        self.m.post("call", lambda b: b.save_nvram(self.nvram))

    def close(self):
        if self.nvram:
            self.m.post("call", lambda b: b.save_nvram(self.nvram))
            time.sleep(0.2)
        self.m.stopping = True
        self.root.destroy()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rom", default=DEFAULT_ROM)
    ap.add_argument("--nvram", help="battery-RAM image: loaded at start, saved on exit")
    ap.add_argument("--blank-cal", action="store_true",
                    help="start with blank cal RAM (default: synthetic cal records that pass "
                         "their checksums, unless --nvram exists or --cal-bin is given)")
    ap.add_argument("--cal-bin", help="cal-RAM dump to load (WinGPIB 448-byte .bin, 512 or 8K)")
    ap.add_argument("--prologix", nargs="?", type=int, const=1234, default=None, metavar="PORT",
                    help="also serve the meter as a Prologix GPIB-ETHERNET adapter (default 1234)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--addr", type=int, default=22, help="GPIB address for the Prologix server")
    ap.add_argument("--line-hz", type=int, default=60, choices=[50, 60])
    for k, unit in (("dcv", "V"), ("acv", "V rms"), ("dci", "A"), ("aci", "A rms"),
                    ("ohm", "ohm (default open)"), ("freq", "Hz")):
        ap.add_argument(f"--{k}", type=float, help=f"initial {k.upper()} signal, {unit}")
    ap.add_argument("--noise", type=float, default=0.0, help="noise, ppm of range (1 sigma)")
    ap.add_argument("--faults", action="store_true",
                    help="open the fault-injection tab first (default when --rom isn't the HP ROM)")
    ap.add_argument("--fast-conv", action="store_true",
                    help="20 ms conversions instead of NPLC-based timing")
    a = ap.parse_args()

    sys.setswitchinterval(0.001)        # GUI responsiveness next to the CPU-bound emulator
    from prologix_server import prepare_board
    b = prepare_board(a.rom, a.nvram, not a.blank_cal, a.cal_bin, line_hz=a.line_hz)
    sig = {k: getattr(a, k) for k in ("dcv", "acv", "dci", "aci", "ohm", "freq")
           if getattr(a, k) is not None}
    vi = VirtualInput(noise_ppm=a.noise, **sig)
    b.inguard.source = vi
    b.inguard.realistic_timing = not a.fast_conv
    b.runaway = "watchdog"              # a CPU running into garbage is reset, as on the real board
    dec, lcd = display3457.attach(b)
    b.power_on()
    meter = Meter(b, lcd, prologix=a.prologix, host=a.host, addr=a.addr)
    root = tk.Tk()
    App(root, meter, vi, nvram=a.nvram,
        faults_first=a.faults or os.path.abspath(a.rom) != os.path.abspath(DEFAULT_ROM))
    meter.start()
    root.mainloop()
    meter.stopping = True


if __name__ == "__main__":
    main()
