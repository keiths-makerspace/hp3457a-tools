#!/usr/bin/env python3
"""HP 3457A main controller (03457-66511) board model - emulator layer 2.

Runs the real ROM on the emu6809 core with the hardware the firmware expects.
Hardware facts are from the 66511/66501 schematics (docs/schematic-extracts) and from the
firmware; anything marked (verify) in the comments is an assumption.

  I/O (A0..A2 ignored by the decoders, so each register is mirrored over 8 bytes):
   $0000 UART RBR (read)          $0008 UART TBR (write)       HD6402, U616
   $0010 UART status: b7 DR, b6 TBRE, b5 OE, b4 FE, b3 PE (b0-2 pulled up)
   $0018 UART DRR (any access clears DR)
   $0020-$0027 Intel 8291A GPIB talker/listener (RS0-2 = A0-A2), INT -> IRQ
   $0028 external-trigger FIRQ acknowledge (write)       (FIRQ = EXT TRIG, inferred)
   $0030 CALREQ (any access)  -> write-cal-RAM lock circuit
   $0038 IN1: b0-5 keyboard rows I5,I3,I6,I1,I2,I4 (active low), b6 JM621, b7 LINE_FREQ
   $003A OUT1: keyboard columns O6,O1,O4,O8,O5,O3,O2,O7 (active low); b0 also MASK* (NMI gate)
   $003C OUT2: b0 PWO b1 PHI1 b2 PHI2 b3 INA b4 SYNC b5 ISA b6 VMCMPLT b7 OTL* (watchdog)
  Timers: E = 2 MHz. NMI every 65536 E cycles (32.768 ms) when OUT1.b0 = 1.
          Watchdog: reset after 1,048,576 cycles (524.288 ms) without a rising edge of OTL.

Usage:
  python3 tools/emu3457.py --seconds 5 [--nvram build/nvram.bin] [--io-log] [--profile]
"""
import argparse
import collections
import random
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from emu6809 import CPU, IllegalOpcode  # noqa: E402

DEFAULT_ROM = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "firmware",
                           "main-controller-66511", "03457-88810_U602.bin")
E_HZ = 1_995_000                # Y601 = 7.980 MHz / 4 (photo of the faulty meter's 66511, 2026-11-15)
NMI_PERIOD = 65536
WATCHDOG = 1 << 20
UART_BYTE_CYCLES = 117          # 11 bits at 187.5 kbit/s ~= 58.7 us


class UART:
    """HD6402: one-byte receive holding register, transmit holding + shift register."""

    def __init__(self, board):
        self.b = board
        self.rbr = 0
        self.dr = False
        self.oe = self.fe = self.pe = False
        self.tx_busy_until = 0      # shift register busy
        self.tbr_full = False
        self.tbr = 0
        self.rx_queue = collections.deque()   # (deliver_at_cycle, byte, pe, fe)
        self.break_at = None        # next break character while SIN is held low (LinkFaults)

    def status(self):
        self._tick()
        v = 0x07
        if self.dr:
            v |= 0x80
        if not self.tbr_full:
            v |= 0x40
        if self.oe:
            v |= 0x20
        if self.fe:
            v |= 0x10
        if self.pe:
            v |= 0x08
        return v

    def write(self, v):
        self._tick()
        self.tbr = v
        self.tbr_full = True
        self._tick()

    def _tick(self):
        now = self.b.cpu.cycles
        # transmitter: TBR -> TSR when the shift register is free
        if self.tbr_full and now >= self.tx_busy_until:
            start = max(now, self.tx_busy_until)
            self.tx_busy_until = start + UART_BYTE_CYCLES
            self.tbr_full = False
            self.b.link_to_adc(self.tbr, self.tx_busy_until)
        # receiver; SIN held low (LinkFaults.to_main == "stuck_low") reads as a stream of breaks
        if self.b.link.to_main == "stuck_low":
            if self.break_at is None:
                self.break_at = now + UART_BYTE_CYCLES
            while self.break_at <= now:
                self.rx_queue.append((self.break_at, 0x00, False, True))
                self.break_at += UART_BYTE_CYCLES
        else:
            self.break_at = None
        while self.rx_queue and self.rx_queue[0][0] <= now:
            _, byte, pe, fe = self.rx_queue.popleft()
            self.oe = self.oe or self.dr    # HD6402: OE latched with the character that overran
            self.rbr = byte
            self.pe, self.fe = pe, fe       # HD6402: PE/FE describe the character in RBR
            self.dr = True

    def send_to_main(self, byte, at, pe=False, fe=False):
        self.rx_queue.append((at, byte, pe, fe))


class LinkFaults:
    """Faults on the cross-isolation link: 187.5 kbit/s, 11-bit frames (start, 8 data LSB
    first, even parity, stop) through optos A2U509 (main -> 8051) and A2U510 (8051 -> main).
    Per direction ("to_adc", "to_main"):
      ok          no fault
      dead        line idles high (LED or transistor open): nothing arrives
      stuck_low   line held low. To the 8051 this holds it in RESET (its reset is derived
                  from RXD); to the main the UART sees a stream of breaks (00 + framing error)
      noise       each bit flips with probability `noise`; parity/framing errors as decoded
      slow_rise   the line can't rise within one bit time: a 1 right after a 0 reads as 0
                  (a degraded opto / pull-up). Characteristic corruptions plus parity errors.
    """
    MODES = ("ok", "dead", "stuck_low", "noise", "slow_rise")

    def __init__(self, to_adc="ok", to_main="ok", noise=0.01, seed=None):
        self.to_adc, self.to_main, self.noise = to_adc, to_main, noise
        self.rng = random.Random(seed)

    @staticmethod
    def frame(byte):
        data = [(byte >> i) & 1 for i in range(8)]
        return [0] + data + [sum(data) & 1, 1]          # even parity

    def wire(self, mode, byte):
        """-> (byte, parity_error, framing_error) as received, or None if nothing arrives."""
        if mode == "ok":
            return byte, False, False
        if mode in ("dead", "stuck_low"):
            return None
        bits = self.frame(byte)
        if mode == "noise":
            bits = [b ^ (self.rng.random() < self.noise) for b in bits]
        elif mode == "slow_rise":
            bits = [bits[0]] + [0 if bits[i] and not bits[i - 1] else bits[i]
                                for i in range(1, len(bits))]
        if bits[0]:                                     # start bit lost: no character
            return None
        data = sum(b << i for i, b in enumerate(bits[1:9]))
        return data, bool((sum(bits[1:9]) + bits[9]) & 1), not bits[10]

    def active(self):
        return self.to_adc != "ok" or self.to_main != "ok"


class Inguard8051:
    """Behavioural stand-in for the A/D controller. The real 8051 ROM is unknown; this
    only produces what the main-controller firmware is seen to expect (see docs)."""

    def __init__(self, board, selftest_ok=True, echo_ok=True):
        self.b = board
        self.state = "run"
        self.zero_run = 0
        self.last_rx = -10 ** 9
        self.frame = []
        self.selftest_ok = selftest_ok
        self.echo_ok = echo_ok
        self.mem = bytearray(256)       # SP_PEEK/SP_POKE target (model)
        self.option = 0x00              # value returned for the OPTION query (verify meaning)
        self.counts = 0                 # A/D result returned for each conversion (model)
        self.conv_cycles = 40000        # 20 ms per conversion (model, not NPLC-accurate)
        self.next_conv = 0
        self.awaiting_req = False       # sent 0x10, waiting for the main's 0x7F
        self.readings = 0
        self.log = []
        self.absent = False             # no 8051 (unpowered / missing): never answers
        # HP self-test support (STEST, docs/EMULATOR.md "Self-test"): channel FE = internal
        self.internal = False           # 1B FE received: readings come from inside the board
        self.test_src = None            # 0x57 internal 1 V reference on, 0x47 test current on
        # measurement set-up as the 8051 learns it from the frames (mode frame m*32+L*16+5,
        # function frame f*8+1, range frame (r+7)*16+3); None until a frame has set it. Used
        # on the internal channel, so the stand-in also serves the diagnostic ROM's stage 10,
        # which keeps none of the HP ROM's RAM variables.
        self.cfg_mode = self.cfg_func = self.cfg_rng = None
        self.analog_faults = set()      # see ANALOG_FAULTS
        self.held = False               # RXD held low: kept in reset (LinkFaults to_adc)
        self.source = None              # vinput.VirtualInput: signals at the terminals
        self.realistic_timing = False   # conversion time from NPLC/line/autozero (source only)

    def receive(self, byte, at):
        if self.absent:
            return
        self.log.append((at, "rx", byte))
        gap = at - self.last_rx
        self.last_rx = at
        # a long run of back-to-back 0x00 bytes holds RXD low -> RC reset of the 8051. The RC
        # (~10 ms) integrates the line: a byte with at most 2 one-bits (a zero corrupted by
        # noise on the way) still keeps it mostly low, so it doesn't break the run.
        low = bin(byte).count("1") <= 2
        if low and gap <= UART_BYTE_CYCLES * 2:
            self.zero_run += 1
        else:
            self.zero_run = 1 if byte == 0 else 0
        if self.zero_run > 200:
            if self.state != "reset":
                self.log.append((at, "reset", None))
            self.state = "reset"
            self.internal, self.test_src = False, None
            self.cfg_mode = self.cfg_func = self.cfg_rng = None
            self.frame = []             # bytes seen before the reset was detected are void
            return
        if self.state == "reset":
            return
        if self.awaiting_req and byte == 0x7F:
            # data request after "conversion complete": 2 or 4 bytes, big-endian (ROM A926/A838);
            # in FREQ/PER (FUNCTION >= 20) 6 bytes: gate ticks, cycles (ROM AA99 -> w_A99B)
            for k, byte in enumerate(self.reading_bytes(at)):
                self.reply(byte, at + k * UART_BYTE_CYCLES)
            self.awaiting_req = False
            self.readings += 1
            self.next_conv = at + self.conversion_cycles()
            return
        if self.state == "echo":
            if byte == 0:
                self.state = "run"
                self.reply(0x0F, at)
            else:
                self.reply(byte if self.echo_ok else byte ^ 0x40, at)
            return
        # command mode: frame ends after a quiet gap (handled in poll)
        self.frame.append(byte)

    def poll(self, now):
        if self.absent or self.held:
            return
        if self.state == "reset" and now - self.last_rx > 4 * UART_BYTE_CYCLES:
            # RXD released: 8051 boots, runs its self test, reports FF, enters echo mode
            self.state = "echo"
            self.reply(0xFF if self.selftest_ok else 0x02, now + 20000)
        if (self.state == "run" and not self.frame and not self.awaiting_req
                and self.b.mem[0x03D0] & 0x0F == 1 and now >= self.next_conv
                and now - self.last_rx > 3 * UART_BYTE_CYCLES):
            # main is waiting for a reading (STAT=1): report "conversion complete" (ROM AF32)
            self.awaiting_req = True
            st = 0x10
            if self.source is not None and not self.internal:   # no autorange on the internal channel
                st = self.source.status(self.b.mem, now / E_HZ)
                if st != 0x10:
                    # 2x = "changed range, converting again": the ROM (B00C) stores the
                    # range, sends that range's constants (w_A521, '53' + 4-byte groups) and
                    # returns without asking for data. A fresh 10 follows the next conversion.
                    self.log.append((now, "autorange", st))
                    self.awaiting_req = False
                    self.next_conv = now + self.conversion_cycles()
            self.reply(st, now)
            return
        if self.state == "run" and self.frame and now - self.last_rx > 3 * UART_BYTE_CYCLES:
            # MODELLING SHORTCUT: the 8051's real framing is unknown, so the stand-in answers
            # according to what the main controller is waiting for, i.e. its link state
            # STAT ($03D0) and the receive-handler table at B167 (see docs/EMULATOR.md).
            stat = self.b.mem[0x03D0] & 0x0F
            if self.frame == [0x67]:
                # abort (w_B344, from XABORT on a function/range change): the main discards
                # bytes until it sees 0F. Any conversion in progress is dropped.
                self.log.append((now, "abort", stat))
                self.frame = []
                self.awaiting_req = False
                self.next_conv = now + self.conv_cycles
                self.reply(0x0F, now)
                return
            if stat == 0:
                return                    # main is not waiting for a reply to this frame
            if stat == 1:                 # frame started a conversion (e.g. '00'): no ACK
                self.log.append((now, "trigger", bytes(self.frame).hex()))
                self.frame = []
                self.next_conv = now + self.conv_cycles
                return
            f, self.frame = self.frame, []
            self.command(f, now, stat)

    # Analog-board faults that fail one HP self-test step each (STEST, AUXERR bit):
    ANALOG_FAULTS = {
        "integrator": "integrator convergence (AUXERR 8): zero reading +1000 counts",
        "frontend_zero": "front-end zero (16): 30 mV range zero off by 200000 counts",
        "reference": "current source / gain / divider (32): reference reads 0.70 V",
        "amps": "amps (64): test current reads double",
        "ac_offset": "AC amp DC offset (128): AC offset reading 10",
        "ac": "AC converter (256): AC reading 10",
        "ohms_precharge": "ohms precharge (512): negative reading",
        "acal_gain": "ACAL gain step (ERR 2): reads 90% of nominal",
    }

    def setup(self, mem):
        """(range, A/D mode, function) for an internal reading: from the frames when they have
        set them, else from the HP ROM's RAM ($033E, $0372, $0342)."""
        rng = mem[0x033E] - 256 if mem[0x033E] > 127 else mem[0x033E]
        mode, func = mem[0x0372], mem[0x0342]
        if self.cfg_rng is not None:
            rng = self.cfg_rng
        if self.cfg_mode is not None:
            mode = self.cfg_mode
        if self.cfg_func is not None:
            func = self.cfg_func
        return rng, mode, func

    def parse_setup(self, f):
        """Mode, function and range from a set-up frame (leading 7F data requests skipped)."""
        f = list(f)
        while f and f[0] == 0x7F:
            f.pop(0)
        if len(f) == 1 and f[0] & 0x0F == 5:
            self.cfg_mode = f[0] >> 5
        elif len(f) >= 2 and f[0] & 0x07 == 1 and f[1] & 0x0F == 3:
            self.cfg_func, self.cfg_rng = f[0] >> 3, (f[1] >> 4) - 7
        elif len(f) >= 2 and f[0] & 0x0F == 3 and f[0] >= 0x30:
            self.cfg_rng = (f[0] >> 4) - 7              # range only (w_A521, e.g. F3)

    def internal_counts(self, mem):
        """A reading with the internal channel selected (the self-test steps, ROM D361-D3FB):
        0 V, the 1.000 V reference (after 57) or the 1 mA test current (after 47), scaled like
        any reading (count = value x 10^(AD_MODE+3-RANGE)), with the injected faults."""
        rng, mode, func = self.setup(mem)
        f = self.analog_faults
        if (func & ~2) == 16 and self.test_src is None:
            # ACAL gain step (D235): FUNCTION 16 on the internal channel; RESULT is the raw
            # count and must be 98e6..102e6; ACAL stores 1/RESULT ($051B). What the 8051
            # measures here is unknown (verify); the stand-in sends the nominal 1e8.
            return 90_000_000 if "acal_gain" in f else 100_000_000
        if self.test_src == 0x57:
            v = 0.70 if "reference" in f else 1.0
        elif self.test_src == 0x47:
            v = 0.002 if "amps" in f else 0.001
        else:
            v = 0.0
        c = round(v * 10.0 ** (mode + 3 - rng))
        if self.test_src is None:
            if "integrator" in f and rng == 0 and mode == 0:
                c += 1000                       # D361: limits -461..51
            if "frontend_zero" in f and rng == -2:
                c += 200000                     # D385: limits +-150000
        return c

    def reading_bytes(self, at):
        mem = self.b.mem
        if self.internal and self.setup(mem)[2] < 20:
            rng, mode, func = self.setup(mem)
            n = 4 if mode >= 2 or (func & ~2) == 16 else 2
            return (self.internal_counts(mem) & ((1 << (8 * n)) - 1)).to_bytes(n, "big")
        if self.source is not None:
            return self.source.data(mem, at / E_HZ)
        if mem[0x0342] >= 20:                   # counts mode: 0.1 s gate, `counts` cycles
            return (200000).to_bytes(3, "big") + (self.counts & 0xFFFFFF).to_bytes(3, "big")
        n = 2 if mem[0x0372] < 2 else 4
        return (self.counts & ((1 << (8 * n)) - 1)).to_bytes(n, "big")

    def conversion_cycles(self):
        if self.source is not None and self.realistic_timing and not self.internal:
            return int(self.source.conversion_seconds(self.b.mem) * E_HZ)
        return self.conv_cycles

    def command(self, f, now, stat):
        self.log.append((now, "cmd", bytes(f).hex(), stat))
        self.parse_setup(f)
        if f[0] == 0x1B and len(f) >= 2:                # channel: FE = internal (STEST w_CCCF)
            self.internal = f[1] == 0xFE
            self.test_src = None
        elif f[0] in (0x57, 0x47) and len(f) == 1:      # self-test sources (w_CC0D)
            self.test_src = f[0]
        if f[0] == 0x0D and len(f) >= 2:                # SP_PEEK (STAT 3)
            self.reply(self.mem[f[1]], now)
            return
        if f[0] == 0x4D and len(f) >= 3:                # SP_POKE
            self.mem[f[1]] = f[2]
        if stat == 2:
            self.reply(0x0F, now)                       # ACK
        elif stat == 3:
            self.reply(0x00, now)
        elif stat in (4, 5):
            self.reply(0x10, now)                       # handler B063 wants (b & 0x70) == 0x10
        elif stat == 6:                                 # handler B085 reads two bytes
            v = self.stat6_value(f[0])
            self.reply((v >> 8) & 0xFF, now)
            self.reply(v & 0xFF, now + 2 * UART_BYTE_CYCLES)
        elif stat == 7:
            self.reply(self.option, now)                # OPTION byte (B09D)
        elif stat == 8:
            self.reply(0x00, now)                       # CUR_CHAN (B0A9)
        else:
            self.log.append((now, "unhandled-stat", stat))

    def stat6_value(self, cmd):
        """Two-byte answers to 1F / 2F / 3F (AC converter, AC offset, ohms precharge; ROM
        w_D18D). In the self-test they must be 50..973, 40..983 and >= 0. Outside the internal
        channel the stand-in keeps answering 0 as before."""
        if not self.internal:
            return 0
        f = self.analog_faults
        if cmd == 0x2F:
            return 10 if "ac_offset" in f else 500
        if cmd == 0x1F:
            return 10 if "ac" in f else 500
        if cmd == 0x3F:
            return 0xFF00 if "ohms_precharge" in f else 500
        return 0

    def reply(self, byte, at):
        self.log.append((at, "tx", byte))
        self.b.link_to_main(byte, at + UART_BYTE_CYCLES)


class GPIB8291:
    """Intel 8291A talker/listener, modelled at the register level the firmware uses.

    Read:  0 DIN  1 ISR1  2 ISR2  3 SPS  4 ADS  5 CPT  6/7 ADR0/1
    Write: 0 BOUT 1 IE1   2 IE2   3 SPM  4 ADM  5 AUX  6 ADR  7 EOS
    ISR1: b0 BI b1 BO b2 ERR b3 DEC b4 END b5 GET b6 APT b7 CPT
    ISR2: b0 ADSC b1 REMC b2 LLOC b3 SPC b4 REM b5 LLO b6 SPAS b7 INT
    ADS:  b0 MJMN b1 TA b2 LA b3 TPAS b4 LPAS b5 EOI b6 lon b7 ton
    The host (bus controller) side is driven through Board.hpib_* helpers.
    """

    def __init__(self, board):
        self.b = board
        self.ie1 = self.ie2 = 0
        self.isr1 = 0
        self.isr2 = 0
        self.ads = 0
        self.spm = 0
        self.aux_a = 0
        self.addr0 = 0
        self.adr_raw = [0, 0]       # Address 0/1 registers as written via 6W (ARS selects)
        self.din = 0
        self.holdoff = False        # RFD holdoff active (needs aux 03)
        self.in_queue = collections.deque()   # (byte, eoi) from the host
        self.out = []               # (byte, eoi) talked by the meter
        self.send_eoi = False
        self.bout_pending = None
        self.int_line = False
        self.log = []
        self.data_at = 0            # cycle before which queued data isn't offered (see host_listen)
        self.dac_hold = False       # DCL/SDC/GET handshake held until the CPU writes VALID/NON-VALID

    def _update(self):
        self._feed()
        self.int_line = bool((self.isr1 & self.ie1) or (self.isr2 & self.ie2 & 0x7F))

    def _feed(self):
        if self.dac_hold or (self.data_at and self.b.cpu.cycles < self.data_at):
            return
        if self.ads & 0x04 and not self.isr1 & 0x01 and not self.holdoff and self.in_queue:
            byte, eoi = self.in_queue.popleft()
            self.din = byte
            self.isr1 |= 0x01 | (0x10 if eoi else 0)
            if self.aux_a & 0x01:
                self.holdoff = True

    def read(self, r):
        if r == 0:
            v = self.din
            self.isr1 &= ~0x01
            if not self.aux_a & 0x01:
                self.holdoff = False
            self._update()
            return v
        if r == 1:
            v = self.isr1
            self.isr1 = 0
            self._update()
            return v
        if r == 2:
            v = self.isr2 | (0x80 if self.int_line else 0)
            self.isr2 &= 0x30                  # REM/LLO are status, the rest clear on read
            self._update()
            return v
        if r == 3:
            return self.spm
        if r == 4:
            return self.ads
        if r in (6, 7):                         # Address 0/1 (6R/7R): INT|DT|DL|AD5-1
            return (0x80 if self.int_line and r == 6 else 0) | self.adr_raw[r - 6]
        return 0

    def write(self, r, v):
        self.log.append((self.b.cpu.cycles, r, v))
        if r == 0:                              # byte out
            self.isr1 &= ~0x02
            if self.ads & 0x02:                 # talker active: the host accepts it at once
                self.out.append((v, self.send_eoi))
                self.isr1 |= 0x02
            else:                               # held in BOUT until addressed to talk
                self.bout_pending = (v, self.send_eoi)
            self.send_eoi = False
        elif r == 1:
            self.ie1 = v
        elif r == 2:
            self.ie2 = v
        elif r == 3:
            self.spm = v
        elif r == 5:
            kind = v >> 5
            if kind == 0:
                cmd = v & 0x0F
                if cmd == 0x02:                 # chip reset
                    self.isr1 = self.isr2 = 0
                    self.holdoff = False
                elif cmd == 0x03:               # finish handshake
                    self.holdoff = False
                elif cmd == 0x06:               # send EOI with next byte
                    self.send_eoi = True
                elif cmd in (0x07, 0x0F):       # non-valid / valid: release DAC holdoff
                    self.dac_hold = False
                elif cmd == 0x0D:               # rtl (return to local), sent by the LOCAL key
                    # (w_9359: 0D then 05 = set, clear). Leaves remote unless LLO (verify
                    # against the 8291A data sheet).
                    if self.isr2 & 0x10 and not self.isr2 & 0x20:
                        self.isr2 = (self.isr2 & ~0x10) | 0x02
            elif kind == 4:
                self.aux_a = v & 0x1F
        elif r == 6:
            self.adr_raw[1 if v & 0x80 else 0] = v & 0x7F
            if not v & 0x80 and not v & 0x60:
                self.addr0 = v & 0x1F
        self._update()

    # ---- bus-controller side ------------------------------------------------
    ADDR_TO_DATA = 1000             # cycles (0.5 ms): command phase (ATN, MLA) before data

    def host_listen(self, data, eoi=True):
        if not self.ads & 0x04:
            self.ads = (self.ads & ~0x02) | 0x04
            self.isr2 |= 0x01                    # ADSC
            # On a real bus the addressing (ATN) phase precedes the data phase. Offering the
            # first data byte in the same instant as the very first ADSC/REMC makes the ROM
            # mis-parse the first command after power-on (ERR? +256 +16), so model a gap.
            self.data_at = self.b.cpu.cycles + self.ADDR_TO_DATA
        if not self.isr2 & 0x10:
            self.isr2 |= 0x10 | 0x02             # REM + REMC
        for i, c in enumerate(data):
            self.in_queue.append((c, eoi and i == len(data) - 1))
        self._update()

    def host_talk(self):
        if not self.ads & 0x02:
            self.ads = (self.ads & ~0x04) | 0x02
            self.isr2 |= 0x01
            if self.bout_pending is not None:   # a byte was already waiting in BOUT
                self.out.append(self.bout_pending)
                self.bout_pending = None
            self.isr1 |= 0x02                    # BO: ready for the next byte
        self._update()

    def host_unaddress(self):
        if self.ads & 0x06:
            self.ads &= ~0x06
            self.isr2 |= 0x01
            self.isr1 &= ~0x02                   # BO only while talker active
        self._update()

    # bus-controller events (8291A behaviour from the data sheet, as the ROM service A11E
    # expects it: ISR1 DEC -> w_A022, GET, ISR2 SPC -> re-read SPS, clear SRQ annunciator)
    @property
    def srq(self):
        return bool(self.spm & 0x40)

    def host_spoll(self):
        """Serial poll: return the status byte; if rsv was set the chip clears it and
        raises SPC (serial poll complete)."""
        v = self.spm
        if v & 0x40:
            self.spm &= ~0x40
            self.isr2 |= 0x08
        self._update()
        return v

    def host_clear(self):
        """SDC/DCL: device clear (ISR1 DEC). Pending input is dropped by the bus."""
        self.in_queue.clear()
        self.isr1 |= 0x08
        # With the DEC interrupt enabled the 8291A holds the bus command's handshake (DAC)
        # until the CPU answers VALID/NON-VALID (verify against the data sheet). The ROM's
        # device clear (HPIB_DCL, a warm restart) writes VALID ($0F) ~0.3 s later.
        if self.ie1 & 0x08:
            self.dac_hold = True
        self._update()

    def host_trigger(self):
        """GET: group execute trigger (ISR1 GET). The meter must be a listener."""
        if not self.ads & 0x04:
            self.ads = (self.ads & ~0x02) | 0x04
            self.isr2 |= 0x01
        self.isr1 |= 0x20
        if self.ie1 & 0x20:                     # same DAC holdoff as DEC (verify)
            self.dac_hold = True
        self._update()

    def host_local(self):
        """GTL / REN false: leave remote (REM off, REMC)."""
        if self.isr2 & 0x10:
            self.isr2 = (self.isr2 & ~0x10) | 0x02
        self._update()

    def host_llo(self):
        """LLO: local lockout (LLO on, LLOC)."""
        if not self.isr2 & 0x20:
            self.isr2 |= 0x20 | 0x04
        self._update()

    def host_ifc(self):
        """IFC: interface clear, all interface functions idle."""
        self.host_unaddress()

    def host_dcl(self):
        self.isr1 |= 0x08
        self._update()

    def host_get(self):
        self.isr1 |= 0x20
        self._update()


class RamFaults:
    """Injected faults in RAM U603 (for proving diagnostic messages). Address lines A0-A12 and
    data lines D0-D7 are the chip's pins; cells are chip addresses.
        addr_stuck = {line: 0|1}     address line forced (accesses alias)
        data_stuck = {bit: 0|1}      data line forced on reads and writes
        cell_stuck = {addr: (mask, value)}   bits of one cell read back forced
    Chip cells below $0040 (hidden behind the I/O window) live in `low`."""

    def __init__(self, addr_stuck=None, data_stuck=None, cell_stuck=None):
        self.addr_stuck = dict(addr_stuck or {})
        self.data_stuck = dict(data_stuck or {})
        self.cell_stuck = dict(cell_stuck or {})
        self.low = bytearray(0x40)

    def chip_addr(self, a):
        for line, v in self.addr_stuck.items():
            a = (a | 1 << line) if v else (a & ~(1 << line))
        return a & 0x1FFF

    def force_data(self, v):
        for bit, s in self.data_stuck.items():
            v = (v | 1 << bit) if s else (v & ~(1 << bit))
        return v

    def read(self, mem, a):
        c = self.chip_addr(a)
        v = self.low[c] if c < 0x40 else mem[c]
        if c in self.cell_stuck:
            mask, val = self.cell_stuck[c]
            v = (v & ~mask) | (val & mask)
        return self.force_data(v)

    def write(self, mem, a, v):
        c = self.chip_addr(a)
        v = self.force_data(v)
        if c < 0x40:
            self.low[c] = v
        else:
            mem[c] = v


class Board:
    def __init__(self, rom_path=DEFAULT_ROM, nvram=None, secure_jumper="NORMAL",
                 line_hz=60, protect=(0x0040, 0x01FF), inguard=None, power_on_lock_s=1.5):
        img = open(rom_path, "rb").read()
        self.mem = bytearray(0x10000)
        self.mem[0x2000:] = img[0x2000:0x10000]
        self.nvram_path = nvram
        if nvram and os.path.exists(nvram):
            data = open(nvram, "rb").read()
            self.mem[0x0040:0x2000] = data[0x0040:0x2000]
        self.cpu = CPU(self)
        self._fast_reads()
        self.uart = UART(self)
        self.inguard = inguard or Inguard8051(self)
        self.inguard.b = self
        self.gpib = GPIB8291(self)
        self.out1 = 0xFF
        self.out2 = 0x00
        self.keys = set()               # pressed (row_bit, col_bit)
        self.ram_faults = None          # RamFaults: injected U603 faults (diagnostic-ROM tests)
        # injected digital-board faults (docs/EMULATOR.md, "Fault injection")
        self.link = LinkFaults()        # cross-isolation link
        self.uart_dead = False          # U616 not responding: reads $FF, writes lost
        self.gpib_dead = False          # U614 8291A not responding: reads $FF, no interrupts
        self.nmi_dead = False           # no 32.768 ms NMI tick
        self.watchdog_cycles = WATCHDOG  # None = watchdog never fires
        self.line_sense = "ok"          # IN1 b7: "ok" | "low" | "high"
        self.stuck_keys = set()         # (row, col) held down permanently
        # what an undefined opcode does: "raise" (find emulator bugs) or "watchdog" (a real
        # runaway 6809: it stops doing useful work, the OTL kicks stop, the watchdog resets it)
        self.runaway = "raise"
        self.runaways = []              # (cycle, pc) of each runaway
        self.rom_image = bytes(self.mem[0x2000:])
        self.row_pull = 0               # IN1 row bits pulled low externally (display MCU, goal 6)
        self.in1_hooks = []             # f(cycles) -> extra row bits pulled low
        self.secure_jumper = secure_jumper   # NORMAL | KEY | DSA
        self.line_half = E_HZ // (2 * line_hz)
        self.protect = protect          # write-protected cal region (verify)
        self.ext_trig = False
        # watchdog / cal lock
        self.last_kick = 0
        self.resets = []
        self.cal_armed = False
        self.nve_until = -1
        self.last_reset_cycle = 0
        self.power_on_cycle = 0
        # U519c "POWER-ON LOCK" (66501: R552 100k x C510 15uF ~ 1.5 s): CALREQ is ignored
        # for this long after power-up (inferred from the 66501 schematic; verify on 66511)
        self.power_on_lock = int(power_on_lock_s * E_HZ)
        self.next_nmi = NMI_PERIOD
        # logging
        self.coverage = None            # bytearray(0x10000): set to record every ROM byte read
        self.ram_writes = None          # bytearray(0x2000): set to record every RAM byte written
        self.io_log = None
        self.blocked_writes = 0
        self.prot_writes = []
        self.otl_listeners = []
        self.out2_listeners = []

    def _fast_reads(self):
        """Give the CPU a direct path to RAM/ROM reads (I/O and coverage logging still go
        through read()). About 6% faster; nothing else changes."""
        mem, read, board = self.mem, self.read, self

        def rd(a):
            a &= 0xFFFF
            if a >= 0x0040 and board.coverage is None and board.ram_faults is None:
                return mem[a]
            return read(a)
        self.cpu.rd = rd

    # ------------------------------------------------------------------ link (through LinkFaults)
    def link_to_adc(self, byte, at):
        mode = self.link.to_adc
        self.inguard.held = mode == "stuck_low"
        if self.inguard.held:
            self.inguard.state = "reset"
            return
        r = self.link.wire(mode, byte)
        if r is not None:
            self.inguard.receive(r[0], at)      # the 8051's own parity check isn't modelled

    def link_to_main(self, byte, at):
        r = self.link.wire(self.link.to_main, byte)
        if r is not None:
            self.uart.send_to_main(r[0], at, r[1], r[2])

    # ------------------------------------------------------------------ ROM / RAM faults
    def flip_rom_bit(self, addr, bit=0):
        if not 0x2000 <= addr <= 0xFFFF:
            raise ValueError("ROM is $2000-$FFFF")
        self.mem[addr] ^= 1 << bit

    def restore_rom(self):
        self.mem[0x2000:] = self.rom_image

    def rom_flipped(self):
        return [0x2000 + i for i, (x, y) in enumerate(zip(self.mem[0x2000:], self.rom_image))
                if x != y]

    # ------------------------------------------------------------------ bus
    def read(self, a):
        if self.ram_faults is not None and 0x0040 <= a < 0x2000:
            return self.ram_faults.read(self.mem, a)
        if a >= 0x0040:
            if (self.coverage is not None and a >= 0x2000
                    and not 0xCA43 <= self.cpu.last_pc <= 0xCA4F):   # ignore the ROM checksum loop
                self.coverage[a] = 1
            return self.mem[a]
        v = self._io_read(a)
        if self.io_log is not None:
            self.io_log.append((self.cpu.cycles, self.cpu.last_pc, "R", a, v))
        return v

    def write(self, a, v):
        if a >= 0x2000:
            return
        if a >= 0x0040:
            if self.ram_writes is not None and not 0xCB7B <= self.cpu.last_pc <= 0xCBB4:
                self.ram_writes[a] = 1          # (power-on RAM test excluded)
            if self.protect and self.protect[0] <= a <= self.protect[1]:
                if self.cpu.cycles > self.nve_until:
                    self.blocked_writes += 1
                    return
                self.prot_writes.append((self.cpu.cycles, a, v))
            if self.ram_faults is not None:
                self.ram_faults.write(self.mem, a, v)
                return
            self.mem[a] = v
            return
        if self.io_log is not None:
            self.io_log.append((self.cpu.cycles, self.cpu.last_pc, "W", a, v))
        self._io_write(a, v)

    def _io_read(self, a):
        blk = a & 0x38
        if (self.uart_dead and blk in (0x00, 0x10, 0x18)) or (self.gpib_dead and blk == 0x20):
            return 0xFF
        if blk == 0x00:
            self.uart._tick()
            return self.uart.rbr
        if blk == 0x10:
            return self.uart.status()
        if blk == 0x18:
            self.uart.dr = False
            self.uart.oe = False
            return 0xFF
        if blk == 0x20:
            return self.gpib.read(a & 7)
        if blk == 0x30:
            self._calreq()
            return 0xFF
        if blk == 0x38:
            reg = (a >> 1) & 3
            if reg == 0:
                return self._in1()
            return 0xFF
        return 0xFF

    def _io_write(self, a, v):
        blk = a & 0x38
        if (self.uart_dead and blk in (0x08, 0x18)) or (self.gpib_dead and blk == 0x20):
            return
        if blk == 0x08:
            self.uart.write(v)
        elif blk == 0x18:
            self.uart.dr = False
            self.uart.oe = False
        elif blk == 0x20:
            self.gpib.write(a & 7, v)
        elif blk == 0x28:
            self.ext_trig = False
            self.cpu.firq_line = False
        elif blk == 0x30:
            self._calreq()
        elif blk == 0x38:
            reg = (a >> 1) & 3
            if reg == 1:
                self.out1 = v
            elif reg == 2:
                old = self.out2
                self.out2 = v
                if v & 0x80 and not old & 0x80:
                    self.last_kick = self.cpu.cycles
                    if self.nve_until >= self.cpu.cycles:
                        self.nve_until = self.cpu.cycles - 1    # OTL kick closes NVE (verify)
                for f in self.out2_listeners:
                    f(self.cpu.cycles, v)

    def _in1(self):
        v = 0x3F
        drive_low = ~self.out1 & 0xFF
        for row_bit, col_bit in (self.keys | self.stuck_keys) if self.stuck_keys else self.keys:
            if drive_low & (1 << col_bit):
                v &= ~(1 << row_bit)
        pull = self.row_pull
        for f in self.in1_hooks:
            pull |= f(self.cpu.cycles)
        v &= ~pull & 0x3F
        # JM621 (66511 schematic): pin 1 = column O6, pin 2 = IN1 b6 (RP613 pull-up), pin 3 = GND,
        # pin 4 = open. SECURE DIS. (1-2, "KEY"): b6 follows O6 (OUT1 b0); DSA (2-3): b6 grounded;
        # NORMAL (3-4): b6 pulled up.
        if self.secure_jumper == "KEY":
            v |= 0x40 if self.out1 & 1 else 0
        elif self.secure_jumper != "DSA":
            v |= 0x40
        if self.line_sense == "high" or (self.line_sense == "ok"
                                         and (self.cpu.cycles // self.line_half) & 1):
            v |= 0x80
        return v

    def _calreq(self):
        # model: a hardware reset arms the latch; the first CALREQ access after it opens
        # NVE for one 4.096 ms period, closed early by an OTL kick. (verify on hardware)
        now = self.cpu.cycles
        if now - self.power_on_cycle < self.power_on_lock:
            return
        if self.cal_armed:
            self.cal_armed = False
            self.nve_until = now + 8192

    # ------------------------------------------------------------------ control
    def hw_reset(self, why):
        self.resets.append((self.cpu.cycles, why))
        self.last_kick = self.cpu.cycles
        self.last_reset_cycle = self.cpu.cycles
        self.cal_armed = True
        self.nve_until = -1
        if why == "power-on":
            # Peripherals keep their state across a watchdog reset: the firmware never
            # re-initialises the 8291A after a protected write, yet CAL works over HP-IB on
            # real meters. (Inferred; verify U614/U616 RESET/MR wiring on the schematic.)
            self.gpib = GPIB8291(self)
        self.cpu.irq_line = self.cpu.firq_line = False
        self.cpu.reset()

    def power_on(self):
        self.power_on_cycle = self.cpu.cycles
        self.hw_reset("power-on")

    def power_cycle(self, off_seconds=1.0, out1_powerup=0xFF, battery_dead=False, seed=None):
        """Simulate losing power: CPU and peripherals reset, battery RAM kept, time passes.
        out1_powerup: U612 (74HCT374) has no reset, so OUT1 powers up arbitrary; bit 0 is
        MASK* (NMI gate), which decides whether a stuck resume is rescued by the watchdog."""
        self.cpu.cycles += int(off_seconds * E_HZ)
        self.next_nmi = self.cpu.cycles + NMI_PERIOD
        if battery_dead:                        # battery RAM forgets: random contents
            rnd = random.Random(seed)
            self.mem[0x0040:0x2000] = bytes(rnd.randrange(256) for _ in range(0x2000 - 0x40))
        self.uart = UART(self)
        keep = {k: getattr(self.inguard, k) for k in ("source", "realistic_timing", "counts",
                                                       "selftest_ok", "echo_ok", "option",
                                                       "absent", "held", "analog_faults")}
        self.inguard.__init__(self)
        vars(self.inguard).update(keep)        # what is connected to the meter stays connected
        self.out1, self.out2 = out1_powerup, 0x00
        self.power_on()

    def step(self):
        cpu = self.cpu
        cpu.step()
        now = cpu.cycles
        if now >= self.next_nmi:
            self.next_nmi += NMI_PERIOD
            if self.out1 & 1 and not self.nmi_dead:     # MASK* gate
                cpu.nmi()
        if self.watchdog_cycles is not None and now - self.last_kick > self.watchdog_cycles:
            self.hw_reset("watchdog")
        g = self.gpib
        if g.data_at and now >= g.data_at:
            g.data_at = 0
            g._update()
        self.inguard.poll(now)
        self.uart._tick()
        cpu.irq_line = self.gpib.int_line and not self.gpib_dead
        cpu.firq_line = self.ext_trig

    def run(self, cycles=None, seconds=None, until=None, profile=None):
        if seconds is not None:
            cycles = int(seconds * E_HZ)
        end = self.cpu.cycles + cycles
        while True:
            try:
                return self._run(end, until, profile)
            except IllegalOpcode:
                if self.runaway != "watchdog":
                    raise
                self._runaway(end)
                if self.cpu.cycles >= end:
                    return "time"

    def _runaway(self, end):
        """The CPU ran into garbage: model it as dead until the watchdog resets it (or for good
        if the watchdog is broken). Peripherals keep their state."""
        cpu = self.cpu
        self.runaways.append((cpu.cycles, cpu.last_pc))
        if self.watchdog_cycles is None:
            cpu.cycles = end
            return
        fire = self.last_kick + self.watchdog_cycles + 1
        cpu.cycles = min(max(cpu.cycles, fire), end)
        if cpu.cycles >= fire:
            self.next_nmi = max(self.next_nmi, cpu.cycles)
            self.hw_reset("watchdog (runaway at $%04X)" % cpu.last_pc)

    def _run(self, end, until, profile):
        cpu = self.cpu
        if profile is not None or until is not None or type(self).step is not Board.step:
            while cpu.cycles < end:
                self.step()
                if profile is not None and cpu.pc == 0x6319:     # after LDX ,Y++ in NEXT
                    profile[cpu.x] += 1
                if until is not None and until(self):
                    return "until"
            return "time"
        # same as step(), inlined for speed
        cstep, poll, tick = cpu.step, self.inguard.poll, self.uart._tick
        wd = self.watchdog_cycles if self.watchdog_cycles is not None else 1 << 62
        nmi_ok, irq_ok = not self.nmi_dead, not self.gpib_dead
        while cpu.cycles < end:
            cstep()
            now = cpu.cycles
            if now >= self.next_nmi:
                self.next_nmi += NMI_PERIOD
                if self.out1 & 1 and nmi_ok:
                    cpu.nmi()
            if now - self.last_kick > wd:
                self.hw_reset("watchdog")
                cstep, poll, tick = cpu.step, self.inguard.poll, self.uart._tick
            g = self.gpib
            if g.data_at and now >= g.data_at:
                g.data_at = 0
                g._update()
            poll(now)
            tick()
            cpu.irq_line = g.int_line and irq_ok
            cpu.firq_line = self.ext_trig
        return "time"

    def save_nvram(self, path=None):
        path = path or self.nvram_path
        if path:
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            open(path, "wb").write(bytes(self.mem[0:0x2000]))

    # ---- HP-IB host helpers ------------------------------------------------
    def hpib_write(self, text, seconds=2.0):
        """Address the meter to listen and send text (EOI on the last byte); run until
        the meter has taken every byte."""
        data = text.encode() if isinstance(text, str) else bytes(text)
        self.gpib.host_listen(data)
        self.run(seconds=seconds, until=lambda bd: not bd.gpib.in_queue and not bd.gpib.isr1 & 1)
        self.run(seconds=0.05)

    def hpib_read(self, seconds=3.0):
        """Address the meter to talk; collect bytes until EOI (or LF) or timeout."""
        self.gpib.out = []
        self.gpib.host_talk()

        def done(bd):
            o = bd.gpib.out
            return bool(o) and (o[-1][1] or o[-1][0] == 0x0A)
        self.run(seconds=seconds, until=done)
        self.gpib.host_unaddress()
        return bytes(b for b, _ in self.gpib.out)

    def press_key(self, row, col, seconds=0.15):
        """Hold the key at matrix (row 0-5 = IN1 bit, col 0-7 = OUT1 bit) for a while."""
        self.keys.add((row, col))
        self.run(seconds=seconds)
        self.keys.discard((row, col))
        self.run(seconds=0.1)

    def hpib_query(self, text, seconds=3.0):
        self.hpib_write(text)
        return self.hpib_read(seconds)

    # ---- snapshots (fast "what if" runs from one booted state) --------------
    _LINKS = {"b", "bus", "_ops", "cpu", "uart", "inguard", "gpib", "in1_hooks",
              "otl_listeners", "out2_listeners", "trace"}

    def snapshot(self):
        """Copy of the machine state (RAM, CPU registers, peripherals). Listeners and hooks
        are not part of it: they stay attached across restore()."""
        import copy

        def grab(o):
            return {k: copy.deepcopy(v) for k, v in vars(o).items() if k not in self._LINKS}
        return {name: grab(o) for name, o in (("board", self), ("cpu", self.cpu),
                                              ("uart", self.uart), ("inguard", self.inguard),
                                              ("gpib", self.gpib))}

    def restore(self, snap):
        import copy
        for name, o in (("board", self), ("cpu", self.cpu), ("uart", self.uart),
                        ("inguard", self.inguard), ("gpib", self.gpib)):
            for k, v in snap[name].items():
                cur = getattr(o, k, None)
                if isinstance(v, bytearray) and isinstance(cur, bytearray) and len(cur) == len(v):
                    cur[:] = v                  # keep the object (the CPU's fast read path holds it)
                else:
                    setattr(o, k, copy.deepcopy(v))

    # helpers
    def peek16(self, a):
        return self.mem[a] << 8 | self.mem[a + 1]

    def aux_err(self):
        return self.peek16(0x0375)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rom", default=DEFAULT_ROM)
    ap.add_argument("--seconds", type=float, default=3.0)
    ap.add_argument("--nvram", help="battery RAM image to load/save (e.g. build/nvram.bin)")
    ap.add_argument("--io-log", action="store_true")
    ap.add_argument("--profile", action="store_true", help="histogram of executed Forth words")
    ap.add_argument("--jumper", default="NORMAL", choices=["NORMAL", "KEY", "DSA"])
    a = ap.parse_args()
    b = Board(a.rom, nvram=a.nvram, secure_jumper=a.jumper)
    if a.io_log:
        b.io_log = []
    prof = collections.Counter() if a.profile else None
    b.power_on()
    b.run(seconds=a.seconds, profile=prof)
    print(b.cpu.state())
    print(f"emulated {b.cpu.cycles / E_HZ:.2f} s; resets: {b.resets}")
    print(f"AUX_ERR=${b.aux_err():04X}  blocked cal writes={b.blocked_writes}  "
          f"protected writes={len(b.prot_writes)}")
    print("inguard log:", b.inguard.log[:12], "...", b.inguard.log[-6:])
    if prof:
        from decompile import Forth
        f = Forth(a.rom)
        print("top words:", ", ".join(f"{f.name(x)}:{n}" for x, n in prof.most_common(25)))
    if a.io_log:
        c = collections.Counter((k, ad, pc) for _, pc, k, ad, _ in b.io_log)
        print("I/O hot spots:", [(k, hex(ad), hex(pc), n) for (k, ad, pc), n in c.most_common(20)])
    if a.nvram:
        b.save_nvram()


if __name__ == "__main__":
    main()
