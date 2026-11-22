#!/usr/bin/env python3
"""Goal 6: display-extension patch for an upgraded display (docs/DISPLAY.md).

Builds a patched ROM image (never touches firmware/) that
  (a) sends a reading record (full-resolution RESULT, exponent, NDIG, function) on the
      display bus to a second peripheral address each time the meter shows a reading, and
  (b) detects an upgraded display at power-on by a bus handshake: PROBE frame -> the display
      MCU pulls keyboard row I5 low -> RELEASE frame -> row high again. IN1 is read with no
      keyboard column driven, so a key press can't fake the answer.

    python3 tools/dispx_patch.py [build/dispx.bin]

New HP-IB commands: XDISP n (0 off / 1 on), XDISP? (0/1), XPROBE (repeat the detection).
Stock behaviour is unchanged unless a display answers the probe or XDISP 1 is sent.
EMULATION ONLY so far: see the risks section in docs/DISPLAY.md before using on a meter.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from forthpatch import Patch  # noqa: E402
from display3457 import XADDR, OP_XDATA, OP_PROBE, OP_RELEASE, XROW_BIT, XDATA_LEN  # noqa: E402

# RAM: 16 bytes reserved by raising FREE_BOT from $0765 to $0775 (docs/FREE_SPACE.md)
XDISP_V = 0x0765            # 1 = send reading records
XBUF = 0x0766               # record buffer (9 bytes)
FREE_BOT_NEW = 0x0775
PROBE_LOOPS = 200           # wait-loop passes for the MCU to answer (~13 ms, see DISPLAY.md)

# hooks into stock threaded code
HOOK_RDG = 0x87FE           # DISP_SRV state 2: "w_853E 2+ DISP_UPD~11" (format reading)
HOOK_PON = 0xFD36           # PON: "... IG_INIT PON_TEST ..."

# ROM display-driver subroutines (all expect X = $003C, A = OUT2 image)
PWO_START, SEND_INSTR, SEND_BYTE, CLOCKS, PWO_END = 0x72F3, 0x72B8, 0x7343, 0x7361, 0x7304
NEXT = 0x6317


def xframe(org):
    """XFRAME ( addr n op -- ): one bus transaction to peripheral XADDR: PWO, FC XADDR,
    instruction op, n data bytes on INA. IRQ masked and OUT1 cleared (MASK* holds off the NMI
    tick, which rewrites OUT2) exactly as DISP_UPD~11 does."""
    def jsr(a):
        return [0xBD, a >> 8, a & 0xFF]
    c = [0x34, 0x01,                    # PSHS CC
         0x1A, 0x10,                    # ORCC #$10
         0x7F, 0x00, 0x3A,              # CLR  $003A   (OUT1: NMI masked, columns low)
         0x34, 0x20,                    # PSHS Y       (IP)
         0x37, 0x06, 0x34, 0x04,        # PULU D / PSHS B   op
         0x37, 0x06, 0x34, 0x04,        # PULU D / PSHS B   count
         0x37, 0x20,                    # PULU Y       addr
         0xBE, 0x71, 0x9D]              # LDX  $719D   (= $003C)
    c += jsr(PWO_START)
    c += [0xC6, 0xFC] + jsr(SEND_INSTR)                 # FC
    c += [0xC6, XADDR] + jsr(SEND_BYTE)                 # address byte on INA
    c += [0x84, 0x37, 0xC6, 0x30] + jsr(CLOCKS)         # as ROM 7311
    c += [0xE6, 0x61] + jsr(SEND_INSTR)                 # LDB 1,S (op)
    loop = [0x6D, 0xE4, 0x27, 0x09,                     # TST ,S / BEQ done
            0xE6, 0xA0] + jsr(SEND_BYTE) + [            # LDB ,Y+ / JSR
            0x6A, 0xE4, 0x20, 0xF3]                     # DEC ,S / BRA loop
    assert len(loop) == 13
    c += loop
    c += [0x84, 0x37, 0xC6, 0x08] + jsr(CLOCKS) + jsr(PWO_END)
    c += [0x32, 0x62,                   # LEAS 2,S
          0x35, 0x20,                   # PULS Y
          0xB6, 0x03, 0x64,             # LDA  $0364  (OUT1 image)
          0xB7, 0x00, 0x3A,             # STA  $003A
          0x35, 0x02,                   # PULS A      (saved CC)
          0x84, 0x10, 0x26, 0x02,       # ANDA #$10 / BNE +2
          0x1C, 0xEF,                   # ANDCC #$EF
          0x7E, NEXT >> 8, NEXT & 0xFF]  # JMP NEXT
    return c


def wait_row(p, want_low, label):
    """Poll IN1 (no column driven) until the row is low/high or the loop count runs out."""
    row = 1 << XROW_BIT
    test = ["IN1", "C@", row, "AND"] + (["0="] if want_low else [])
    return [255, "OUT1", "C!", PROBE_LOOPS, ("label", label), "1-", "DUP", "0="] + test + \
        ["OR", ("0branch", label), "DROP"]


def build(rom=None):
    p = Patch(rom) if rom else Patch()
    row = 1 << XROW_BIT
    p.code("XFRAME", xframe)
    p.colon("XSEND", [
        1, XBUF, "C!",                                          # record version
        "FPRES", "C@", XBUF + 1, "C!",                          # b0: RESULT is IEEE single
        "RESULT", XBUF + 2, 4, "CMOVE",                         # 32-bit RESULT, big-endian
        "OLD_RNG", "B@", "OLD_ADMO~10", "C@", "MODE>RNG~15", "+", XBUF + 6, "C!",  # exponent
        "DIGITS", "C@", XBUF + 7, "C!",
        "FUNCTION", "C@", XBUF + 8, "C!",
        XBUF, XDATA_LEN, OP_XDATA, "XFRAME"])
    p.colon("XRDG", [("xt", 0x853E), XDISP_V, "C@", ("0branch", "x"), "XSEND", ("label", "x")])
    p.colon("DPROBE", [
        255, "OUT1", "C!", "IN1", "C@", row, "AND", ("0branch", "fail"),   # row idle high?
        1, XBUF, "C!", XBUF, 1, OP_PROBE, "XFRAME"]
        + wait_row(p, True, "w1") + ["IN1", "C@", row, "AND", "0="]          # answered?
        + [XBUF, 1, OP_RELEASE, "XFRAME"]
        + wait_row(p, False, "w2") + ["IN1", "C@", row, "AND", "AND"]        # released?
        + [("0branch", "fail"), 1, ("branch", "st"), ("label", "fail"), 0,
           ("label", "st"), XDISP_V, "C!", "OUT1_IMA~10", "C@", "OUT1", "C!"])
    p.colon("XPON", ["PON_TEST", "DPROBE"])
    p.command("XDISP", [0, 1, 1, ("xt", 0xDCC8), "GET_INT-~9", XDISP_V, "C!", "DONE_CMD~9"])
    p.command("XDISP?", [XDISP_V, "C@", "TOS>CUST", "DONE_CMD~9"])
    p.command("XPROBE", ["DPROBE", "DONE_CMD~9"])
    p.repoint(HOOK_RDG, 0x853E, "XRDG")
    p.repoint(HOOK_PON, "PON_TEST", "XPON")
    fb = p.xt("FREE_BOT")
    assert p.img[fb + 2:fb + 4] == b"\x07\x65"
    p.img[fb + 2:fb + 4] = FREE_BOT_NEW.to_bytes(2, "big")
    return p


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        os.path.dirname(__file__), "..", "build", "dispx.bin")
    p = build()
    path = p.save(out)
    print(f"wrote {path}: {p.used()} bytes at ${p.lo:04X}-${p.here - 1:04X}, "
          f"hooks {[f'${a:04X}' for a, _, _ in p.hooks]}, FREE_BOT ${FREE_BOT_NEW:04X}")


if __name__ == "__main__":
    main()
