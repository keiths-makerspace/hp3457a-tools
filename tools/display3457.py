#!/usr/bin/env python3
"""HP 3457A display-bus decoder (emulator layer 3).

The main controller bit-bangs an HP-41-style LCD bus on OUT2 ($003C):
  b0 PWO  b1 PHI1  b2 PHI2  b3 INA (data)  b4 SYNC  b5 ISA (instruction)
Firmware routines (ROM 7244-7391): every bit is presented on INA/ISA, then PHI1 and
PHI2 are pulsed. Instructions are 8 bits on ISA, LSB first, while SYNC is high.
Data bits on INA follow the instruction (after 2 gap clocks) while SYNC is low.

Layer A - BusDecoder: purely electrical. Samples the lines on each PHI2 rising edge and
emits frames (instruction, [ina bits...]) and PWO edges. This is what an add-on display
MCU tapping U611 sees.

Layer B - Lcd: interprets frames like the HP-41 LCD driver, as used by this firmware:
  FC + byte FD      select the display (peripheral address 0xFD)
  0A + 48 bits      register A: 12 low nibbles, character 11 first
  1A + 48 bits      register B: 12 high nibbles
  2A + 48 bits      register C: 12 nibbles of which bit 0 is used (character bit 6)
  BC + 12 bits      annunciators (byte from $030A, then nibble from $0309)
  B8 / C8           bracket an update; C8 is used as the 'commit' point for snapshots
Character code = (C & 1) << 6 | (B & 3) << 4 | A, HP-41 set (0x00-0x1F = @A-Z[\\]^_ ,
0x20-0x3F = ' '..'?'); punctuation = (B >> 2) & 3: 1 '.', 2 ':', 3 ','. This follows Ian
Johnston's TFT display firmware, which runs on real meters (glyphs for codes >= 0x40: verify).
"""

CHARSET = "@ABCDEFGHIJKLMNOPQRSTUVWXYZ[\\]^_ !\"#$%&'()*+,-./0123456789:;<=>?"
PUNCT = ("", ".", ":", ",")


class BusDecoder:
    def __init__(self):
        self.prev = 0
        self.isa_bits = []
        self.ina_bits = []
        self.instr = None
        self.frames = []            # (cycle, instr, ina_bits)
        self.events = []            # (cycle, 'PWO', level)
        self.listeners = []

    def out2(self, cycle, v):
        p = self.prev
        self.prev = v
        if (v ^ p) & 0x01:
            self.events.append((cycle, "PWO", v & 1))
            if not v & 1:                       # PWO falling = end of transaction (ROM 7304)
                self.flush(cycle)
        if not (v & 0x04 and not p & 0x04):        # PHI2 rising edge only
            return
        sync, isa, ina = v & 0x10, v & 0x20, v & 0x08
        if sync:
            if not self.isa_bits and self.instr is not None:
                self._emit(cycle)
            self.isa_bits.append(1 if isa else 0)
        else:
            if self.isa_bits:
                bits = self.isa_bits[-8:]
                self.instr = sum(b << i for i, b in enumerate(bits))
                self.isa_bits = []
                self.ina_bits = []
            self.ina_bits.append(1 if ina else 0)

    GAP = 2     # clocks with SYNC low after the instruction before data starts (ROM 72E8)

    def _emit(self, cycle):
        fr = (cycle, self.instr, self.ina_bits[self.GAP:])
        self.frames.append(fr)
        for f in self.listeners:
            f(*fr)
        self.instr = None
        self.ina_bits = []

    def flush(self, cycle=0):
        if self.instr is not None:
            self._emit(cycle)


def _nibbles(bits, n):
    out = []
    for k in range(n):
        chunk = bits[4 * k:4 * k + 4]
        if len(chunk) < 4:
            break
        out.append(sum(b << i for i, b in enumerate(chunk)))
    return out


class Lcd:
    def __init__(self):
        self.a = [0] * 12
        self.b = [0] * 12
        self.c = [0] * 12
        self.annun = 0
        self.selected = False
        self.history = []           # (cycle, text) whenever the text changes
        self.unknown = {}
        self.ignored = 0

    def frame(self, cycle, instr, bits):
        if instr == 0xFC:
            self.selected = len(bits) >= 8 and sum(b << i for i, b in enumerate(bits[:8])) == 0xFD
            return
        if not self.selected:                       # another peripheral's traffic (verify on glass)
            self.ignored += 1
            return
        if instr in (0x0A, 0x1A, 0x2A):
            nib = _nibbles(bits, 12)
            if len(nib) == 12:
                reg = {0x0A: self.a, 0x1A: self.b, 0x2A: self.c}[instr]
                for k, v in enumerate(nib):         # first nibble sent = character 11
                    reg[11 - k] = v
            return
        if instr == 0xBC:
            if len(bits) >= 12:
                self.annun = sum(b << i for i, b in enumerate(bits[:12]))
                self._changed(cycle)
            return
        if instr == 0xC8:                           # end of an update: commit a snapshot
            self._changed(cycle)
        self.unknown[instr] = self.unknown.get(instr, 0) + 1

    def cells(self):
        """[(code7, punct)] for the 12 positions, left to right: code = A | (B & 3) << 4 |
        (C & 1) << 6, punct = (B >> 2) & 3 (0 none, 1 '.', 2 ':', 3 ',') - the decoding of
        Ian Johnston's TFT replacement, which works on real glass."""
        return [(self.a[i] | (self.b[i] & 3) << 4 | (self.c[i] & 1) << 6, (self.b[i] >> 2) & 3)
                for i in range(12)]

    def chars(self):
        out = []
        for code, p in self.cells():
            ch = CHARSET[code & 0x3F]
            if code & 0x40:                 # upper half of the HP-41 set (glyphs: verify)
                ch = ch.lower() if ch.isalpha() else ch
            out.append(ch + PUNCT[p])
        return out

    def text(self):
        return "".join(self.chars())

    def _changed(self, cycle):
        t = (self.text(), self.annun)
        if not self.history or self.history[-1][1:] != t:
            self.history.append((cycle,) + t)


def _bytes(bits, n):
    return [sum(b << i for i, b in enumerate(bits[8 * k:8 * k + 8])) for k in range(n)
            if len(bits) >= 8 * k + 8]


# Extension protocol for an upgraded display (goal 6, docs/DISPLAY.md). The patched ROM
# selects a peripheral address the stock LCD doesn't answer to, then sends one of:
XADDR = 0xF7          # FC + F7: select the display-extension peripheral
OP_XDATA = 0x5A       # + 9 bytes: reading record
OP_PROBE = 0x5B       # + 1 byte (protocol version): MCU answers by pulling a row line low
OP_RELEASE = 0x5C     # + 1 byte: MCU releases the row line
XROW_BIT = 0          # IN1 bit pulled by the MCU (I5, J612 pin 13)
XDATA_LEN = 9


def decode_xdata(rec):
    """Reading record -> dict. value is exact (int * 10^exp) or IEEE single for math results."""
    import struct
    ver, flags = rec[0], rec[1]
    raw = bytes(rec[2:6])
    exp = rec[6] - 256 if rec[6] > 127 else rec[6]
    d = {"version": ver, "fpres": flags & 1, "raw": raw.hex(), "exp": exp,
         "digits": rec[7], "function": rec[8]}
    if flags & 1:
        d["value"] = struct.unpack(">f", raw)[0]
    else:
        n = struct.unpack(">i", raw)[0]
        d["counts"] = n
        d["value"] = (float("inf") if n == 0x7FFFFFFF else float("-inf") if n == -0x7FFFFFFF
                      else n * 10.0 ** exp)
    return d


class DisplayMcu:
    """Model of an upgraded-display MCU that taps the bus (listen-only) plus one open-drain
    output to a keyboard row. Answers PROBE after `latency` CPU cycles; records XDATA."""

    def __init__(self, board, latency=0, present=True):
        self.b = board
        self.latency = latency
        self.present = present
        self.selected = False
        self.records = []           # (cycle, decoded dict)
        self.probes = 0
        self._pending = None
        self.pulling = False
        board.in1_hooks.append(self.in1_pull)

    def frame(self, cycle, instr, bits):
        if instr == 0xFC:
            self.selected = _bytes(bits, 1) == [XADDR]
            return
        if not (self.selected and self.present):
            return
        if instr == OP_XDATA:
            rec = _bytes(bits, XDATA_LEN)
            if len(rec) == XDATA_LEN:
                self.records.append((cycle, decode_xdata(rec)))
        elif instr == OP_PROBE:
            self.probes += 1
            self._set(cycle, True)
        elif instr == OP_RELEASE:
            self._set(cycle, False)

    def _set(self, cycle, on):
        self._pending = (self.b.cpu.cycles + self.latency, on)

    def in1_pull(self, cycles):
        """Board.in1_hooks callback: row bits this MCU holds low right now."""
        if self._pending and cycles >= self._pending[0]:
            self.pulling = self._pending[1]
            self._pending = None
        return (1 << XROW_BIT) if self.pulling else 0


def attach(board):
    """Hook a decoder + LCD model to an emu3457.Board; returns (decoder, lcd)."""
    dec = BusDecoder()
    lcd = Lcd()
    dec.listeners.append(lcd.frame)
    board.out2_listeners.append(dec.out2)
    return dec, lcd


def attach_mcu(board, dec, **kw):
    """Add an upgraded-display MCU model to an attached decoder; returns it."""
    mcu = DisplayMcu(board, **kw)
    dec.listeners.append(mcu.frame)
    return mcu
