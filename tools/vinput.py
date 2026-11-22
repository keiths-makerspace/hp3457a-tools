#!/usr/bin/env python3
"""Virtual input signals for the emulated HP 3457A (emu3457.Inguard8051.source).

The real A/D controller (an 8051 on the isolated side) is a stand-in in this emulator, so it
can't measure anything. With a VirtualInput attached, the stand-in turns "what is connected to
the terminals" into the bytes the main-controller ROM expects, given the function, range and
integration mode the ROM has set up:

  DC/AC/ohms:  counts = round(value * 10^(AD_MODE + 3 - RANGE))
               (inverse of the ROM's own scaling: w_A85A divides by 10^(AD_MODE-3) when
               AD_MODE > 3, RES>FP multiplies by 10^(RANGE - min(AD_MODE,3) - 3)) [emu]
               2 bytes when AD_MODE < 2, else 4; overload = $7FFF/$8001 or $7FFFFFFF/$80000001
  FREQ/PER:    6 bytes: gate time in 0.5 us ticks (24 bit), then cycle count (24 bit); the ROM
               computes cycles / (ticks * 5e-7) and inverts it for PER (w_A99B) [emu]
  autorange:   status 2x instead of 10 (ROM B00C: new RANGE = x - 7), then the reading [emu]

Assumptions, not taken from the real 8051 (verify on a meter):
  * full scale = 3 * 10^RANGE (1 A on the current ranges' RANGE 0); readable to OVERRANGE x FS
  * autorange goes up above OVERRANGE x FS and down below DOWN x FS of the next lower range,
    and jumps straight to the right range (the real part may step one range per reading)
  * the ROM's FUNCTION code, bit 1 masked (ROM AF43 does the same), selects the quantity
  * conversion time = NPLC / line frequency, doubled with autozero on, + 1 ms

Signals are in base units (V, A, ohm, Hz). Each may be a number or a function of emulated
time t (seconds), e.g.  vi.set("dcv", lambda t: 1.5 + 0.1 * math.sin(t)).
"""
import math
import random

OVERRANGE = 1.01        # readable to 3.03 V on the 3 V range (verify)
DOWN = 0.9              # autorange down when the reading fits 90% of the next lower range
OPEN = 1e15             # ohms reading for an open circuit (beyond every range -> overload)

# FUNCTION ($0342) & ~2 -> quantity
KINDS = {0: "dcv", 1: "dcv", 4: "acv", 5: "acdcv", 8: "dci", 9: "dci", 12: "aci",
         13: "acdci", 16: "ohm", 17: "ohmf"}
# autorange limits (RANGE exponents) per quantity
RANGES = {"dcv": (-2, 2), "acv": (-2, 2), "acdcv": (-2, 2), "dci": (-4, 0), "aci": (-2, 0),
          "acdci": (-2, 0), "ohm": (1, 7), "ohmf": (1, 7)}
UNITS = {"dcv": "V", "acv": "V", "dci": "A", "aci": "A", "ohm": "ohm", "freq": "Hz"}
NPLC = {0: 0.0005, 1: 0.005, 2: 0.1, 3: 1.0, 4: 10.0}       # NPLC? = 10^(AD_MODE-3) (E30A)

# RAM used (docs/FINDINGS.md)
RANGE, A_RANGE, FUNCTION, AD_MODE, A_ZERO, USLINE = 0x033E, 0x0341, 0x0342, 0x0372, 0x0348, 0x0354


def kind_of(function):
    if function >= 20:
        return "freq"
    return KINDS.get(function & ~2, "dcv")


def full_scale(kind, rng):
    if kind in ("dci", "aci", "acdci") and rng == 0:
        return 1.0
    return 3.0 * 10.0 ** rng


def sbyte(v):
    return v - 256 if v > 127 else v


class VirtualInput:
    """What is connected to the input terminals."""

    def __init__(self, dcv=0.0, acv=0.0, dci=0.0, aci=0.0, ohm=OPEN, freq=0.0,
                 noise_ppm=0.0, seed=None):
        self.signals = {"dcv": dcv, "acv": acv, "dci": dci, "aci": aci, "ohm": ohm,
                        "freq": freq}
        self.noise_ppm = noise_ppm          # gaussian, 1 sigma, ppm of the range's full scale
        self.rng = random.Random(seed)
        self.last = None                    # (kind, value, range) of the latest conversion

    def set(self, kind, value):
        self.signals[kind] = value

    def _sig(self, kind, t):
        v = self.signals.get(kind, 0.0)
        return v(t) if callable(v) else v

    def value(self, kind, t, rng):
        if kind == "acdcv":
            v = math.hypot(self._sig("dcv", t), self._sig("acv", t))
        elif kind == "acdci":
            v = math.hypot(self._sig("dci", t), self._sig("aci", t))
        elif kind == "ohmf":
            v = self._sig("ohm", t)
        else:
            v = self._sig(kind, t)
        if kind in ("acv", "aci", "freq"):
            v = abs(v)
        if self.noise_ppm and kind != "freq" and v < OPEN:
            v += self.rng.gauss(0.0, self.noise_ppm * 1e-6 * full_scale(kind, rng))
        return v

    # ---- what the 8051 stand-in asks ------------------------------------------------
    def status(self, mem, t):
        """Status byte for 'conversion complete': 0x10, or 0x20|(range+7) to autorange."""
        kind = kind_of(mem[FUNCTION])
        rng = sbyte(mem[RANGE])
        if kind == "freq" or not mem[A_RANGE] or kind not in RANGES:
            return 0x10
        lo, hi = RANGES[kind]
        if not lo <= rng <= hi:                 # e.g. 3 Gohm (RANGE 8): leave it alone
            return 0x10
        v = abs(self.value(kind, t, rng))
        fits = v <= OVERRANGE * full_scale(kind, rng)
        too_big_for_lower = rng == lo or v >= DOWN * full_scale(kind, rng - 1)
        if fits and too_big_for_lower:
            return 0x10
        new = hi
        for r in range(lo, hi + 1):
            if v <= DOWN * full_scale(kind, r):
                new = r
                break
        return 0x10 if new == rng else 0x20 | (new + 7)

    def data(self, mem, t):
        """Reply to the main's 7F: the reading bytes."""
        kind = kind_of(mem[FUNCTION])
        if kind == "freq":
            return self._freq_bytes(self.value("freq", t, 0))
        rng = sbyte(mem[RANGE])
        mode = mem[AD_MODE]
        v = self.value(kind, t, rng)
        self.last = (kind, v, rng)
        n = 2 if mode < 2 else 4
        top = (1 << (8 * n - 1)) - 1                       # 7FFF / 7FFFFFFF
        if abs(v) > OVERRANGE * full_scale(kind, rng) or v >= OPEN:
            c = top if v > 0 else -top
        else:
            c = round(v * 10.0 ** (mode + 3 - rng))
            c = max(-top, min(top, c))
        return (c & ((1 << (8 * n)) - 1)).to_bytes(n, "big")

    def _freq_bytes(self, f, gate=0.1):
        self.last = ("freq", f, None)
        if f <= 0:
            n, ticks = 0, int(gate / 5e-7)
        else:
            n = max(1, round(f * gate))
            ticks = round(n / (f * 5e-7))
            if ticks > 0xFFFFFF:
                n, ticks = 1, min(0xFFFFFF, round(1 / (f * 5e-7)))     # below ~0.12 Hz: clipped
            n = min(n, 0xFFFFFF)
        return ticks.to_bytes(3, "big") + n.to_bytes(3, "big")

    def conversion_seconds(self, mem):
        """Integration time the real meter would take (model; verify)."""
        if mem[FUNCTION] >= 20:
            return 0.1 + 0.005
        line = 60.0 if mem[USLINE] else 50.0
        t = NPLC.get(mem[AD_MODE], 10.0) / line
        if mem[A_ZERO] == 1:
            t *= 2
        return t + 0.001
