#!/usr/bin/env python3
"""HP 3457A calibration-RAM image: map, validation, decoding, and dump via PEEK.

Layout of the write-protected region $0040-$01FF (derived from ROM 03457-88810; see
docs/CALIBRATION.md). Record address = $0040 + base[class] + size * (rbase[class] + min(RNG,7)).

  11-byte DC record (DCV, DCI, OHM, OHMF):
     0-2  gain constant      (24-bit, big-endian)          (verify meaning)
     3-5  offset, REAR terminals  (VINPUT = 2)
     6-8  offset, FRONT terminals (VINPUT = 1)
     9    flags: b0 gain, b1 rear offset, b2 front offset (verify polarity)
     10   checksum = ~(sum of bytes 0-9) & 0xFF
  5-byte AC record (ACV/ACDCV, ACI/ACDCI):
     0-2  constant, 3 flags, 4 checksum = ~(sum of bytes 0-3) & 0xFF
  $0077-$007B  shared AC record (constant, flags at $007A, checksum at $007B)
  $018B-$018E  32-bit constant, complement copy at $018F-$0192
  $0193        8-bit trim (+/-23), complement at $0194
  $01FD        protection probe byte (incremented by every protected write and by Test 0's try)
  $01FE-$01FF  CALNUM (16-bit), reported by CALNUM?

Usage:
  calimage.py IMAGE.bin            validate + print a table (448-byte block, 512-byte 0000-01FF,
                                   or 8 KB 0000-1FFF RAM image)
"""
import sys

PROT_LO, PROT_HI = 0x0040, 0x01FF

# (name, function class, first RNG, number of ranges, record size, range labels)
FUNCS = [
    ("DCV", 0, -2, 5, 11, ["30mV", "300mV", "3V", "30V", "300V"]),
    ("ACV", 1, -2, 5, 5, ["30mV", "300mV", "3V", "30V", "300V"]),
    ("DCI", 2, -4, 5, 11, ["300uA", "3mA", "30mA", "300mA", "1A"]),
    ("ACI", 3, -2, 3, 5, ["30mA", "300mA", "1A"]),
    ("OHM", 4, 1, 7, 11, ["30", "300", "3k", "30k", "300k", "3M", "30M"]),
    ("OHMF", 5, 1, 7, 11, ["30", "300", "3k", "30k", "300k", "3M", "30M"]),
]
BASE = [0x00, 0x3C, 0x55, 0x8C, 0x9B, 0xF3]
RBASE = [2, 2, 4, 2, -1, -1]


def record_addr(cls, rng, size):
    return 0x0040 + BASE[cls] + size * (RBASE[cls] + min(rng, 7))


def records():
    out = []
    for name, cls, r0, n, size, labels in FUNCS:
        for k in range(n):
            a = record_addr(cls, r0 + k, size)
            out.append((f"{name} {labels[k]}", a, size))
    out.append(("AC shared", 0x0077, 5))
    return sorted(out, key=lambda r: r[1])


def checksum(block):
    """Checksum byte for a record whose last byte is the checksum (ROM w_8E97)."""
    return (~sum(block[:-1])) & 0xFF


def load(path_or_bytes):
    """Return a 512-byte view addressed 0x0000-0x01FF (bytes below 0x40 are zero)."""
    d = path_or_bytes if isinstance(path_or_bytes, (bytes, bytearray)) else open(path_or_bytes, "rb").read()
    if len(d) == 448:
        return bytes(0x40) + bytes(d)
    if len(d) >= 0x200:
        return bytes(d[:0x200])
    raise ValueError(f"unexpected image size {len(d)} (want 448, 512 or 8192 bytes)")


def s24(b):
    v = b[0] << 16 | b[1] << 8 | b[2]
    return v - (1 << 24) if v & 0x800000 else v


def validate(img):
    """Return (rows, problems). img: 512-byte image addressed from 0."""
    rows, problems = [], []
    for name, a, size in records():
        rec = img[a:a + size]
        ok = checksum(rec) == rec[-1]
        if size == 11:
            fields = {"gain": s24(rec[0:3]), "off_rear": s24(rec[3:6]),
                      "off_front": s24(rec[6:9]), "flags": rec[9]}
        else:
            fields = {"const": s24(rec[0:3]), "flags": rec[3]}
        rows.append((name, a, size, ok, fields, rec))
        if not ok:
            problems.append(f"{name} @ ${a:04X}: checksum {rec[-1]:02X} != {checksum(rec):02X}")
    g = img[0x018B:0x018F]
    gc = img[0x018F:0x0193]
    if bytes(x ^ 0xFF for x in g) != gc:
        problems.append("$018B-$018E: complement copy at $018F mismatch")
    if img[0x0193] ^ 0xFF != img[0x0194]:
        problems.append("$0193: complement at $0194 mismatch")
    return rows, problems


def summary(img):
    rows, problems = validate(img)
    lines = [f"{'record':<14}{'addr':>6} ok  fields"]
    for name, a, size, ok, f, rec in rows:
        fs = " ".join(f"{k}={v:#x}" if k == "flags" else f"{k}={v}" for k, v in f.items())
        lines.append(f"{name:<14} ${a:04X} {'Y' if ok else 'N'}   {fs}")
    g = int.from_bytes(img[0x018B:0x018F], "big")
    lines.append(f"$018B global 32-bit = {g:#010x}   $0193 trim = {img[0x0193]:#04x}   "
                 f"$01FD probe = {img[0x01FD]}   CALNUM = {img[0x01FE] << 8 | img[0x01FF]}")
    lines += ["PROBLEM: " + p for p in problems] or ["no problems found"]
    return "\n".join(lines)


def dump_via_peek(query):
    """CALDUMP with the stock firmware: query(cmd) -> response bytes (one HP-IB query).
    PEEK returns the signed 16-bit big-endian cell at addr; 224 queries cover $0040-$01FF.
    Returns a 512-byte image addressed from 0."""
    img = bytearray(0x200)
    for a in range(PROT_LO, PROT_HI + 1, 2):
        v = int(round(float(query(f"PEEK {a}").decode().strip()))) & 0xFFFF
        img[a], img[a + 1] = v >> 8, v & 0xFF
    return bytes(img)


def main():
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(1)
    print(summary(load(sys.argv[1])))


if __name__ == "__main__":
    main()
