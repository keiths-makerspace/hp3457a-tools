#!/usr/bin/env python3
"""Fault-injection tab for the virtual meter (tools/vmeter.py).

Injects the hardware faults that the main-controller CPU can detect, so the diagnostic ROM
(docs/DIAG_ROM.md) and the HP ROM's own self-test can be watched reacting to them:
RAM U603 lines and cells, ROM U602 bits, battery/cal RAM, the cross-isolation optos, the
8051, U616/U614, NMI tick, watchdog, line-frequency sense, stuck keys, the security jumper.
The fault models live in tools/emu3457.py (RamFaults, LinkFaults, Board attributes).
Changes are applied through Meter.post(), i.e. in the emulator thread.
"""
import tkinter as tk
from tkinter import ttk

from emu3457 import RamFaults, LinkFaults, E_HZ

LINK_MODES = [("ok", "working"), ("dead", "dead (line idle high)"),
              ("stuck_low", "stuck low"), ("noise", "noisy (bit errors)"),
              ("slow_rise", "slow rise (weak opto)")]
ADC_MODES = ["working", "absent / no power", "self-test fails (02)", "echo corrupts bit 6"]
WD_MODES = ["works (0.52 s)", "never fires", "fires early (0.1 s)"]
SENSE_MODES = ["works", "stuck low", "stuck high"]


def _hex(s):
    return int(s.strip().lstrip("$").replace("0x", ""), 16)


class FaultPanel:
    def __init__(self, parent, app, keys):
        """keys: {name: (row, col)} of the front panel, for the stuck-key list."""
        self.app = app
        self.keys = keys
        self.cells = {}                 # chip address -> (mask, value)
        self.v = {}
        outer = ttk.Frame(parent, padding=6)
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="Faults the main-controller CPU can detect. Most act at once; "
                  "RAM/battery faults are clearest after a power cycle.",
                  foreground="#555", wraplength=380, justify=tk.LEFT).grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 4))
        col0 = ttk.Frame(outer)
        col0.grid(row=1, column=0, sticky="n", padx=(0, 6))
        col1 = ttk.Frame(outer)
        col1.grid(row=1, column=1, sticky="n")
        self._ram(col0)
        self._rom(col0)
        self._battery(col0)
        self._link(col1)
        self._analog(col1)
        self._chips(col1)
        self._panel(col1)
        bottom = ttk.Frame(outer)
        bottom.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        ttk.Button(bottom, text="Clear all faults", command=self.clear_all).pack(side=tk.LEFT)
        ttk.Button(bottom, text="Power cycle", command=self.app.power_cycle).pack(side=tk.LEFT, padx=6)
        self.summary = ttk.Label(outer, text="", font=("Consolas", 8), foreground="#a00",
                                 wraplength=400, justify=tk.LEFT)
        self.summary.grid(row=3, column=0, columnspan=2, sticky="w", pady=(4, 0))

    # ---- helpers -----------------------------------------------------------------------
    def post(self, fn):
        self.app.m.post("call", fn)

    def combo(self, parent, key, values, width, row, col, cmd, default=0):
        var = tk.StringVar(value=values[default])
        cb = ttk.Combobox(parent, textvariable=var, values=values, width=width, state="readonly")
        cb.grid(row=row, column=col, padx=2, pady=1, sticky="w")
        cb.bind("<<ComboboxSelected>>", lambda e: cmd())
        self.v[key] = var
        return var

    def entry(self, parent, key, text, width, row, col):
        var = tk.StringVar(value=text)
        ttk.Entry(parent, textvariable=var, width=width).grid(row=row, column=col, padx=2, pady=1,
                                                              sticky="w")
        self.v[key] = var
        return var

    # ---- RAM U603 --------------------------------------------------------------------------
    def _ram(self, parent):
        f = ttk.LabelFrame(parent, text="RAM U603  (diag stage 2)", padding=4)
        f.pack(fill="x", pady=2)
        ttk.Label(f, text="Data line").grid(row=0, column=0, sticky="w")
        self.combo(f, "ram_d", ["none"] + ["D%d" % i for i in range(8)], 5, 0, 1, self.apply_ram)
        self.combo(f, "ram_dl", ["stuck 0", "stuck 1"], 7, 0, 2, self.apply_ram)
        ttk.Label(f, text="Address line").grid(row=1, column=0, sticky="w")
        self.combo(f, "ram_a", ["none"] + ["A%d" % i for i in range(13)], 5, 1, 1, self.apply_ram)
        self.combo(f, "ram_al", ["stuck 0", "stuck 1"], 7, 1, 2, self.apply_ram)
        ttk.Label(f, text="Cell $ / bit").grid(row=2, column=0, sticky="w")
        self.entry(f, "cell_a", "0A3F", 6, 2, 1)
        g = ttk.Frame(f)
        g.grid(row=2, column=2, sticky="w")
        self.v["cell_b"] = tk.StringVar(value="2")
        ttk.Spinbox(g, from_=0, to=7, width=2, textvariable=self.v["cell_b"]).pack(side=tk.LEFT)
        self.v["cell_l"] = tk.StringVar(value="0")
        ttk.Combobox(g, textvariable=self.v["cell_l"], values=["0", "1"], width=2,
                     state="readonly").pack(side=tk.LEFT, padx=2)
        h = ttk.Frame(f)
        h.grid(row=3, column=0, columnspan=3, sticky="w")
        ttk.Button(h, text="Add stuck cell bit", command=self.add_cell).pack(side=tk.LEFT)
        ttk.Button(h, text="Clear RAM faults", command=self.clear_ram).pack(side=tk.LEFT, padx=4)

    def add_cell(self):
        try:
            a, bit = _hex(self.v["cell_a"].get()), int(self.v["cell_b"].get())
        except ValueError:
            return
        if not 0x40 <= a <= 0x1FFF or not 0 <= bit <= 7:
            return
        mask, val = self.cells.get(a, (0, 0))
        mask |= 1 << bit
        val = (val & ~(1 << bit)) | (int(self.v["cell_l"].get()) << bit)
        self.cells[a] = (mask, val)
        self.apply_ram()

    def clear_ram(self):
        self.cells.clear()
        self.v["ram_d"].set("none")
        self.v["ram_a"].set("none")
        self.apply_ram()

    def apply_ram(self):
        d, a = self.v["ram_d"].get(), self.v["ram_a"].get()
        data = {} if d == "none" else {int(d[1:]): int(self.v["ram_dl"].get()[-1])}
        addr = {} if a == "none" else {int(a[1:]): int(self.v["ram_al"].get()[-1])}
        cells = dict(self.cells)
        rf = RamFaults(addr, data, cells) if (data or addr or cells) else None
        self.post(lambda b: setattr(b, "ram_faults", rf))

    # ---- ROM U602 --------------------------------------------------------------------------
    def _rom(self, parent):
        f = ttk.LabelFrame(parent, text="ROM U602  (diag stage 1)", padding=4)
        f.pack(fill="x", pady=2)
        ttk.Label(f, text="Address $ / bit").grid(row=0, column=0, sticky="w")
        self.entry(f, "rom_a", "3000", 6, 0, 1)
        self.v["rom_b"] = tk.StringVar(value="0")
        ttk.Spinbox(f, from_=0, to=7, width=2, textvariable=self.v["rom_b"]).grid(row=0, column=2)
        h = ttk.Frame(f)
        h.grid(row=1, column=0, columnspan=3, sticky="w")
        ttk.Button(h, text="Flip bit", command=self.flip_rom).pack(side=tk.LEFT)
        ttk.Button(h, text="Restore ROM", command=lambda: self.post(lambda b: b.restore_rom())
                   ).pack(side=tk.LEFT, padx=4)

    def flip_rom(self):
        try:
            a, bit = _hex(self.v["rom_a"].get()), int(self.v["rom_b"].get())
        except ValueError:
            return
        if 0x2000 <= a <= 0xFFFF and 0 <= bit <= 7:
            self.post(lambda b: b.flip_rom_bit(a, bit))

    # ---- battery / cal RAM -------------------------------------------------------------------
    def _battery(self, parent):
        f = ttk.LabelFrame(parent, text="Battery and cal RAM  (diag stage 3)", padding=4)
        f.pack(fill="x", pady=2)
        ttk.Button(f, text="Power cycle with a dead battery",
                   command=lambda: self.post(lambda b: (b.keys.clear(), b.power_cycle(
                       1.0, battery_dead=True)))).grid(row=0, column=0, columnspan=3, sticky="w")
        ttk.Label(f, text="Cal byte $").grid(row=1, column=0, sticky="w")
        self.entry(f, "cal_a", "0100", 6, 1, 1)
        ttk.Button(f, text="Corrupt", command=self.corrupt_cal).grid(row=1, column=2, sticky="w")
        self.v["prot"] = tk.BooleanVar(value=False)
        ttk.Checkbutton(f, text="cal write protection broken", variable=self.v["prot"],
                        command=self.apply_prot).grid(row=2, column=0, columnspan=3, sticky="w")

    def corrupt_cal(self):
        try:
            a = _hex(self.v["cal_a"].get())
        except ValueError:
            return
        if 0x40 <= a <= 0x1FF:
            def f(b):
                b.mem[a] ^= 0x5A
            self.post(f)

    def apply_prot(self):
        broken = self.v["prot"].get()
        self.post(lambda b: setattr(b, "protect", None if broken else (0x0040, 0x01FF)))

    # ---- link ----------------------------------------------------------------------------------
    def _link(self, parent):
        f = ttk.LabelFrame(parent, text="Cross-isolation link  (diag stage 5)", padding=4)
        f.pack(fill="x", pady=2)
        names = [n for _, n in LINK_MODES]
        ttk.Label(f, text="Main -> 8051 (U509)").grid(row=0, column=0, sticky="w")
        self.combo(f, "to_adc", names, 20, 0, 1, self.apply_link)
        ttk.Label(f, text="8051 -> main (U510)").grid(row=1, column=0, sticky="w")
        self.combo(f, "to_main", names, 20, 1, 1, self.apply_link)
        ttk.Label(f, text="Noise: bit error prob.").grid(row=2, column=0, sticky="w")
        self.entry(f, "noise", "0.01", 8, 2, 1)
        ttk.Label(f, text="A/D controller (8051)").grid(row=3, column=0, sticky="w")
        self.combo(f, "adc", ADC_MODES, 20, 3, 1, self.apply_adc)

    def apply_link(self):
        code = {n: m for m, n in LINK_MODES}
        to_adc, to_main = code[self.v["to_adc"].get()], code[self.v["to_main"].get()]
        try:
            noise = min(1.0, max(0.0, float(self.v["noise"].get())))
        except ValueError:
            noise = 0.01

        def f(b):
            b.link.to_adc, b.link.to_main, b.link.noise = to_adc, to_main, noise
            if to_adc != "stuck_low":
                b.inguard.held = False
        self.post(f)

    def apply_adc(self):
        k = ADC_MODES.index(self.v["adc"].get())

        def f(b):
            g = b.inguard
            g.absent = k == 1
            g.selftest_ok = k != 2
            g.echo_ok = k != 3
        self.post(f)

    # ---- analog board, as the HP self-test (TEST key) sees it -------------------------------
    def _analog(self, parent):
        from emu3457 import Inguard8051
        f = ttk.LabelFrame(parent, text="Analog board  (HP self-test: TEST key)", padding=4)
        f.pack(fill="x", pady=2)
        self.analog_vars = {}
        for i, (key, desc) in enumerate(Inguard8051.ANALOG_FAULTS.items()):
            v = tk.BooleanVar(value=False)
            ttk.Checkbutton(f, text=desc.split(":")[0], variable=v,
                            command=self.apply_analog).grid(row=i // 2, column=i % 2, sticky="w")
            self.analog_vars[key] = v

    def apply_analog(self):
        faults = {k for k, v in self.analog_vars.items() if v.get()}
        self.post(lambda b: setattr(b.inguard, "analog_faults", faults))

    # ---- digital-board chips and timers ---------------------------------------------------
    def _chips(self, parent):
        f = ttk.LabelFrame(parent, text="Digital board", padding=4)
        f.pack(fill="x", pady=2)
        for r, (key, text) in enumerate((("uart", "UART U616 not responding  (stage 5)"),
                                         ("gpib", "8291A U614 not responding  (stage 7)"),
                                         ("nmi", "NMI tick missing  (stage 4)"))):
            self.v[key] = tk.BooleanVar(value=False)
            ttk.Checkbutton(f, text=text, variable=self.v[key], command=self.apply_chips).grid(
                row=r, column=0, columnspan=2, sticky="w")
        ttk.Label(f, text="Watchdog  (stage 6)").grid(row=3, column=0, sticky="w")
        self.combo(f, "wd", WD_MODES, 17, 3, 1, self.apply_chips)
        ttk.Label(f, text="Line-freq. sense").grid(row=4, column=0, sticky="w")
        self.combo(f, "sense", SENSE_MODES, 17, 4, 1, self.apply_chips)

    def apply_chips(self):
        uart, gpib, nmi = self.v["uart"].get(), self.v["gpib"].get(), self.v["nmi"].get()
        wd = {0: 1 << 20, 1: None, 2: int(0.1 * E_HZ)}[WD_MODES.index(self.v["wd"].get())]
        sense = {0: "ok", 1: "low", 2: "high"}[SENSE_MODES.index(self.v["sense"].get())]

        def f(b):
            b.uart_dead, b.gpib_dead, b.nmi_dead = uart, gpib, nmi
            b.watchdog_cycles, b.line_sense = wd, sense
        self.post(f)

    # ---- front panel ------------------------------------------------------------------------
    def _panel(self, parent):
        f = ttk.LabelFrame(parent, text="Front panel  (stage 8)", padding=4)
        f.pack(fill="x", pady=2)
        ttk.Label(f, text="Stuck key").grid(row=0, column=0, sticky="w")
        names = sorted(self.keys)
        self.v["key"] = tk.StringVar(value=names[0])
        ttk.Combobox(f, textvariable=self.v["key"], values=names, width=14,
                     state="readonly").grid(row=0, column=1, sticky="w")
        h = ttk.Frame(f)
        h.grid(row=1, column=0, columnspan=2, sticky="w")
        ttk.Button(h, text="Stick", command=self.stick_key).pack(side=tk.LEFT)
        ttk.Button(h, text="Release all", command=lambda: self.post(
            lambda b: b.stuck_keys.clear())).pack(side=tk.LEFT, padx=4)
        ttk.Label(f, text="Security jumper").grid(row=2, column=0, sticky="w")
        self.combo(f, "jumper", ["NORMAL", "KEY", "DSA"], 9, 2, 1, self.apply_jumper)

    def stick_key(self):
        pos = self.keys[self.v["key"].get()]
        self.post(lambda b: b.stuck_keys.add(pos))

    def apply_jumper(self):
        j = self.v["jumper"].get()
        self.post(lambda b: setattr(b, "secure_jumper", j))

    # ---- all -------------------------------------------------------------------------------------
    def clear_all(self):
        self.cells.clear()
        for k, val in (("ram_d", "none"), ("ram_a", "none"), ("to_adc", LINK_MODES[0][1]),
                       ("to_main", LINK_MODES[0][1]), ("adc", ADC_MODES[0]), ("wd", WD_MODES[0]),
                       ("sense", SENSE_MODES[0]), ("jumper", "NORMAL")):
            self.v[k].set(val)
        for k in ("uart", "gpib", "nmi", "prot"):
            self.v[k].set(False)
        for v in self.analog_vars.values():
            v.set(False)

        def f(b):
            b.ram_faults = None
            b.restore_rom()
            b.link = LinkFaults()
            b.inguard.absent = b.inguard.held = False
            b.inguard.selftest_ok = b.inguard.echo_ok = True
            b.inguard.analog_faults = set()
            b.uart_dead = b.gpib_dead = b.nmi_dead = False
            b.watchdog_cycles = 1 << 20
            b.line_sense = "ok"
            b.stuck_keys.clear()
            b.secure_jumper = "NORMAL"
            b.protect = (0x0040, 0x01FF)
        self.post(f)

    @staticmethod
    def describe(b):
        """Active faults of a board, one short line each (for the summary)."""
        out = []
        rf = b.ram_faults
        if rf:
            out += ["RAM D%d stuck %d" % kv for kv in rf.data_stuck.items()]
            out += ["RAM A%d stuck %d" % kv for kv in rf.addr_stuck.items()]
            out += ["RAM $%04X mask %02X=%02X" % (a, m, v) for a, (m, v) in rf.cell_stuck.items()]
        flips = b.rom_flipped()
        if flips:
            out.append("ROM changed at " + ", ".join("$%04X" % a for a in flips[:4]))
        if b.link.to_adc != "ok":
            out.append("link to 8051: " + b.link.to_adc)
        if b.link.to_main != "ok":
            out.append("link to main: " + b.link.to_main)
        g = b.inguard
        if g.absent:
            out.append("8051 absent")
        if not g.selftest_ok:
            out.append("8051 self-test fails")
        if not g.echo_ok:
            out.append("8051 echo bad")
        if g.analog_faults:
            out.append("analog: " + ", ".join(sorted(g.analog_faults)))
        if b.uart_dead:
            out.append("U616 dead")
        if b.gpib_dead:
            out.append("U614 dead")
        if b.nmi_dead:
            out.append("no NMI tick")
        if b.watchdog_cycles != 1 << 20:
            out.append("watchdog " + ("dead" if b.watchdog_cycles is None else "early"))
        if b.line_sense != "ok":
            out.append("line sense stuck " + b.line_sense)
        if b.stuck_keys:
            out.append("stuck keys %s" % sorted(b.stuck_keys))
        if b.secure_jumper != "NORMAL":
            out.append("jumper " + b.secure_jumper)
        if b.protect is None:
            out.append("cal protection broken")
        return out

    def refresh(self, board):
        text = "; ".join(self.describe(board))
        text = ("Active: " + text) if text else ""
        if text != self.summary.cget("text"):
            self.summary.configure(text=text)
