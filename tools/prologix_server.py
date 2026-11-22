#!/usr/bin/env python3
"""Emulated Prologix GPIB-ETHERNET adapter with the emulated HP 3457A on its bus.

Lets ordinary instrument software (WinGPIB "Prologix Ethernet", PyVISA via a raw socket, a
terminal) talk to the real ROM running in tools/emu3457.py, e.g. to rehearse a cal-RAM dump
before touching a meter.

    python3 tools/prologix_server.py [--rom build/x.bin] [--nvram build/nv.bin] [--valid-cal]
                                     [--cal-bin 3457ACalram_....bin]
                                     [--port 1234] [--addr 22] [--counts 12345] [-v]

Then point the software at host 127.0.0.1 (port 1234), GPIB address 22. The server listens on
localhost only unless --host is given. One client at a time (like the real adapter).

Prologix commands supported: ++addr ++auto ++clr ++eoi ++eos ++eot_enable ++eot_char ++ifc
++llo ++loc ++mode ++read [eoi|<char>] ++read_tmo_ms ++rst ++savecfg ++spoll ++srq ++trg
++ver ++help. Escapes (ESC before CR, LF, ESC, '+') as on the real adapter.
Extra (not Prologix): ++emu counts <n> (A/D value the inguard stand-in returns),
++emu nvsave (write NV RAM to --nvram), ++emu status.

Deviations (documented in docs/EMULATOR.md): ++read timeouts
and ++read_tmo_ms count emulated time; the meter answers at --addr whatever address its own
NV RAM holds; secondary addresses are ignored.
"""
import argparse
import os
import select
import socket
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from emu3457 import Board, E_HZ, DEFAULT_ROM  # noqa: E402

ESC, CR, LF = 0x1B, 0x0D, 0x0A
VERSION = "hp3457a-re emulator: Prologix GPIB-ETHERNET compatible 1.0"
EOS = {0: b"\r\n", 1: b"\r", 2: b"\n", 3: b""}


class PrologixAdapter:
    """Prologix controller-mode protocol over an emu3457.Board. Feed client bytes with
    feed(); call tick() to advance the emulator; take replies from .tx."""

    def __init__(self, board, meter_addr=22, log=None):
        self.b = board
        self.meter_addr = meter_addr
        self.log = log or (lambda *a: None)
        self.tx = bytearray()
        self.rx = bytearray()
        self.lines = []             # parsed lines waiting: (is_cmd, bytes)
        self.cfg = {"addr": meter_addr, "auto": 0, "eoi": 1, "eos": 0, "eot_enable": 0,
                    "eot_char": 0, "read_tmo_ms": 500, "mode": 1, "savecfg": 1}
        self.reading = None         # dict while a ++read is in progress

    # ---- input parsing ---------------------------------------------------------------
    def feed(self, data):
        """A line ends at an unescaped CR or LF; it is a command if it starts with two
        unescaped '+'. ESC makes the next byte literal (data may contain CR/LF/ESC/'+')."""
        self.rx += data
        cur, esc, plain = bytearray(), False, []      # plain[k]: byte k was not escaped
        consumed = 0
        for i, c in enumerate(self.rx):
            if esc:
                cur.append(c)
                plain.append(False)
                esc = False
            elif c == ESC:
                esc = True
            elif c in (CR, LF):
                if cur:
                    is_cmd = cur[:2] == b"++" and plain[0] and plain[1]
                    self.lines.append((is_cmd, bytes(cur)))
                cur, plain = bytearray(), []
                consumed = i + 1
            else:
                cur.append(c)
                plain.append(True)
        del self.rx[:consumed]

    # ---- bus helpers -----------------------------------------------------------------
    def _present(self, addr=None):
        return (self.cfg["addr"] if addr is None else addr) == self.meter_addr

    def _bus_busy(self):
        g = self.b.gpib
        return bool(g.in_queue) or bool(g.isr1 & 1) or g.dac_hold

    def _start_read(self, mode, char=None):
        g = self.b.gpib
        g.out = []
        self.reading = {"mode": mode, "char": char, "n": 0,
                        "last": self.b.cpu.cycles, "present": self._present()}
        if self.reading["present"]:
            g.host_talk()

    def _poll_read(self):
        r, g = self.reading, self.b.gpib
        tmo = self.cfg["read_tmo_ms"] * E_HZ // 1000
        done = eoi_seen = False
        while r["n"] < len(g.out):
            byte, eoi = g.out[r["n"]]
            r["n"] += 1
            r["last"] = self.b.cpu.cycles
            self.tx.append(byte)
            if eoi:
                done = eoi_seen = True
                break
            if r["mode"] == "char" and byte == r["char"]:
                done = True
                break
        if not done and self.b.cpu.cycles - r["last"] > tmo:
            done = True
            self.log("read timeout")
        if done:
            if eoi_seen and self.cfg["eot_enable"]:
                self.tx.append(self.cfg["eot_char"] & 0xFF)
            if r["present"]:
                g.host_unaddress()
            self.reading = None

    # ---- command execution ------------------------------------------------------------
    def _reply(self, s):
        self.tx += (str(s) + "\r\n").encode()

    def _num(self, name, arg, lo, hi):
        if arg is None:
            self._reply(self.cfg[name])
        else:
            try:
                v = int(arg.split()[0])
            except ValueError:
                return
            if lo <= v <= hi:
                self.cfg[name] = v

    def _command(self, text):
        parts = text[2:].strip().split(None, 1)
        if not parts:
            return
        cmd, arg = parts[0].lower(), (parts[1] if len(parts) > 1 else None)
        g = self.b.gpib
        if cmd == "addr":
            self._num("addr", arg, 0, 30)
        elif cmd in ("auto", "eoi", "eot_enable", "savecfg"):
            self._num(cmd, arg, 0, 1)
        elif cmd == "eos":
            self._num("eos", arg, 0, 3)
        elif cmd == "eot_char":
            self._num("eot_char", arg, 0, 255)
        elif cmd == "read_tmo_ms":
            self._num("read_tmo_ms", arg, 1, 3000)
        elif cmd == "mode":
            if arg is None:
                self._reply(1)                      # controller mode only
        elif cmd == "read":
            if arg is None or arg.lower() == "eoi":
                self._start_read("eoi")
            else:
                try:
                    self._start_read("char", int(arg.split()[0]) & 0xFF)
                except ValueError:
                    self._start_read("eoi")
        elif cmd == "spoll":
            addr = int(arg.split()[0]) if arg else None
            if self._present(addr):
                self._reply(g.host_spoll())
        elif cmd == "srq":
            self._reply(int(g.srq))
        elif cmd == "clr":
            if self._present():
                g.host_clear()
        elif cmd == "trg":
            addrs = [int(x) for x in arg.split()] if arg else [None]
            if any(self._present(a) for a in addrs):
                g.host_trigger()
        elif cmd == "loc":
            g.host_local()
        elif cmd == "llo":
            g.host_llo()
        elif cmd == "ifc":
            g.host_ifc()
        elif cmd == "rst":
            self.b.power_cycle(0.5)
        elif cmd == "ver":
            self._reply(VERSION)
        elif cmd == "help":
            self._reply("++addr ++auto ++clr ++eoi ++eos ++eot_enable ++eot_char ++ifc ++llo "
                        "++loc ++mode ++read ++read_tmo_ms ++rst ++savecfg ++spoll ++srq ++trg "
                        "++ver ++emu")
        elif cmd == "emu":
            self._emu(arg or "")
        else:
            self.log(f"ignored unknown command {text!r}")

    def _emu(self, arg):
        a = arg.split()
        if a[:1] == ["counts"] and len(a) > 1:
            self.b.inguard.counts = int(a[1], 0)
        elif a[:1] == ["nvsave"]:
            self.b.save_nvram()
            self._reply("saved" if self.b.nvram_path else "no --nvram file")
        elif a[:1] == ["status"]:
            self._reply(f"cycles={self.b.cpu.cycles} t={self.b.cpu.cycles / E_HZ:.1f}s "
                        f"aux=0x{self.b.aux_err():04X} meter_addr={self.meter_addr} "
                        f"rom_hpib_addr={self.b.gpib.addr0} srq={int(self.b.gpib.srq)}")

    def _process(self):
        """Run waiting lines while the bus is free (a write blocks until the meter has taken
        every byte, a read until it ends, as on the real adapter)."""
        while self.lines and self.reading is None and not self._bus_busy():
            is_cmd, data = self.lines.pop(0)
            if is_cmd:
                text = data.decode("latin-1")
                self.log(f"<< {text}")
                self._command(text)
            else:
                self.log(f"<< data {data!r}")
                if self._present():
                    self.b.gpib.host_listen(data + EOS[self.cfg["eos"]], eoi=bool(self.cfg["eoi"]))
                if self.cfg["auto"]:
                    self.lines.insert(0, (True, b"++read eoi"))

    def tick(self, seconds=0.01):
        self._process()
        self.b.run(seconds=seconds)
        if self.reading is not None:
            self._poll_read()
        if self.reading is not None and self.lines:     # new input aborts a read (as the
            self.log("read aborted by new input")       # real adapter does)
            if self.reading["present"]:
                self.b.gpib.host_unaddress()
            self.reading = None
        self._process()

    def take_tx(self):
        out, self.tx = bytes(self.tx), bytearray()
        return out


def serve(adapter, host, port, realtime=True, log=print, stop=None, ready=None, hook=None):
    """Socket loop: one client at a time; the emulator keeps running between clients.
    hook(), if given, runs once per 10 ms slice in this thread (tools/vmeter.py uses it to
    deliver key presses and input changes to the same emulated meter)."""
    ls = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    ls.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    ls.bind((host, port))
    ls.listen(1)
    ls.setblocking(False)
    if ready:
        ready(ls.getsockname()[1])
    log(f"listening on {host}:{ls.getsockname()[1]}  (meter at GPIB address {adapter.meter_addr})")
    client = None
    t0, c0 = time.monotonic(), adapter.b.cpu.cycles
    try:
        while not (stop and stop()):
            socks = [ls] + ([client] if client else [])
            r, _, _ = select.select(socks, [], [], 0)
            for s in r:
                if s is ls:
                    c, a = ls.accept()
                    if client:
                        c.close()               # the adapter serves one client
                        continue
                    c.setblocking(False)
                    c.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                    client = c
                    log(f"client {a[0]}:{a[1]} connected")
                else:
                    try:
                        data = client.recv(4096)
                    except (ConnectionError, OSError):
                        data = b""
                    if not data:
                        log("client disconnected")
                        client.close()
                        client = None
                        adapter.reading = None
                        adapter.lines.clear()
                    else:
                        adapter.feed(data)
            if hook:
                hook()
            adapter.tick()
            out = adapter.take_tx()
            if out and client:
                try:
                    client.sendall(out)
                    log(f">> {out!r}")
                except (ConnectionError, OSError):
                    client.close()
                    client = None
            if realtime:                        # don't run ahead of the wall clock
                ahead = (adapter.b.cpu.cycles - c0) / E_HZ - (time.monotonic() - t0)
                if ahead > 0:
                    time.sleep(ahead)
    finally:
        if client:
            client.close()
        ls.close()


def prepare_board(rom, nvram=None, valid_cal=False, cal_bin=None, **kw):
    """Board with battery RAM set up (not yet powered on)."""
    b = Board(rom, nvram=nvram, **kw)
    if valid_cal and not (nvram and os.path.exists(nvram)):
        from coverage import make_valid
        make_valid(b.mem)
    if cal_bin:                     # WinGPIB 3457A dump (448 bytes = $0040-$01FF) or 512/8K image
        from calimage import load
        b.mem[0x40:0x200] = load(cal_bin)[0x40:0x200]
    return b


def make_board(rom, nvram=None, valid_cal=False, counts=0, boot_seconds=7.0, cal_bin=None):
    b = prepare_board(rom, nvram, valid_cal, cal_bin)
    b.inguard.counts = counts
    b.power_on()
    b.run(seconds=boot_seconds)
    return b


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rom", default=DEFAULT_ROM)
    ap.add_argument("--nvram", help="battery-RAM image to load at start and save on exit")
    ap.add_argument("--valid-cal", action="store_true",
                    help="fill cal RAM with synthetic records that pass their checksums "
                         "(when no --nvram file exists yet)")
    ap.add_argument("--cal-bin", help="load a cal-RAM dump into $0040-$01FF: WinGPIB's 3457A "
                    ".bin (448 bytes), or a 512-byte / 8 KB image")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=1234)
    ap.add_argument("--addr", type=int, default=22, help="GPIB address the meter answers at")
    ap.add_argument("--counts", type=lambda s: int(s, 0), default=0,
                    help="A/D value returned by the inguard stand-in")
    ap.add_argument("--fast", action="store_true", help="don't pace the emulator to real time")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()
    print("booting the emulated meter ...")
    b = make_board(a.rom, a.nvram, a.valid_cal, a.counts, cal_bin=a.cal_bin)
    print(f"booted: AUXERR 0x{b.aux_err():04X}, ROM's own HP-IB address {b.gpib.addr0}")
    ad = PrologixAdapter(b, a.addr, log=print if a.verbose else None)
    try:
        serve(ad, a.host, a.port, realtime=not a.fast,
              log=print if a.verbose else (lambda m: print(m) if "client" in m or "listen" in m else None))
    except KeyboardInterrupt:
        pass
    finally:
        if a.nvram:
            b.save_nvram(a.nvram)
            print(f"NV RAM saved to {a.nvram}")


if __name__ == "__main__":
    main()
