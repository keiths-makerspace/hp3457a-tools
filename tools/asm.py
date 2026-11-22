#!/usr/bin/env python3
"""Assemble 6809 source with lwasm (LWTOOLS) into a 64 KB HP 3457A ROM image.

    python3 tools/asm.py diag/diag.asm build/diag.bin [--checksum] [-D NAME[=VAL]] [--keep]

- Runs lwasm natively if it is on PATH, otherwise inside WSL (distro from $HP3457_WSL_DISTRO,
  default Ubuntu-26.04; LWTOOLS installed in ~/.local/bin there, see CLAUDE.md).
- Always --6809 (lwasm defaults to 6309: wrong cycle counts, 6309-only opcodes accepted).
- Output: Intel HEX placed by address into a 64 KB image filled with $FF. Only $2000-$FFFF
  is visible to the CPU; bytes below $2000 are an error.
- Writes next to the .bin: .lst (listing with cycle counts if the source enables them),
  .map, .sym (symbol dump). tools/lwmap.py reads .map/.sym for emulator traces.
- --checksum makes the 16-bit word sum over $2000-$FFFF zero by adjusting $FFF0 (the
  convention of the HP ROM, tools/fix_checksum.py); the diag ROM checks itself that way.

Library: build(src, out, defines=None, checksum=False) -> dict(bin=..., lst=..., map=..., sym=...)
"""
import argparse
import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

DISTRO = os.environ.get("HP3457_WSL_DISTRO", "Ubuntu-26.04")
LO = 0x2000


class AsmError(Exception):
    pass


def _wslpath(p):
    out = subprocess.run(["wsl.exe", "-d", DISTRO, "-e", "wslpath", "-a", os.path.abspath(p)],
                         capture_output=True)
    return out.stdout.decode("utf-8", "replace").replace("\0", "").strip()


def have_lwasm():
    if shutil.which("lwasm"):
        return "native"
    if os.name == "nt" and shutil.which("wsl.exe"):
        r = subprocess.run(["wsl.exe", "-d", DISTRO, "-e", "bash", "-lc", "command -v lwasm"],
                           capture_output=True)
        if r.returncode == 0 and b"lwasm" in r.stdout.replace(b"\0", b""):
            return "wsl"
    return None


def run_lwasm(src, hexfile, lst, mapf, sym, defines=()):
    mode = have_lwasm()
    if not mode:
        raise AsmError("lwasm not found (natively or in WSL %s)" % DISTRO)
    files = dict(src=src, hex=hexfile, lst=lst, map=mapf, sym=sym)
    if mode == "wsl":
        files = {k: _wslpath(v) for k, v in files.items()}
    args = ["lwasm", "--6809", "--format=ihex", "--error-format=gcc",
            "-o", files["hex"], "--list=" + files["lst"], "--map=" + files["map"],
            "--symbol-dump=" + files["sym"],
            "-I", os.path.dirname(files["src"]) if mode == "native" else
            files["src"].rsplit("/", 1)[0]]
    for d in defines:
        args.append("-D" + d)
    args.append(files["src"])
    if mode == "wsl":
        cmd = " ".join("'%s'" % a.replace("'", "'\\''") for a in args)
        r = subprocess.run(["wsl.exe", "-d", DISTRO, "-e", "bash", "-lc", cmd], capture_output=True)
    else:
        r = subprocess.run(args, capture_output=True)
    out = (r.stdout + r.stderr).decode("utf-8", "replace").replace("\0", "")
    if r.returncode != 0:
        raise AsmError(out.strip() or "lwasm failed (rc=%d)" % r.returncode)
    return out


def ihex_to_image(path, fill=0xFF):
    img = bytearray([fill]) * 0x10000
    used = bytearray(0x10000)
    base = 0
    for n, line in enumerate(open(path), 1):
        line = line.strip()
        if not line:
            continue
        if line[0] != ":":
            raise AsmError("%s:%d: not Intel HEX" % (path, n))
        rec = bytes.fromhex(line[1:])
        if sum(rec) & 0xFF:
            raise AsmError("%s:%d: bad checksum" % (path, n))
        cnt, addr, typ, data = rec[0], rec[1] << 8 | rec[2], rec[3], rec[4:4 + rec[0]]
        if typ == 0:
            a = base + addr
            if a + cnt > 0x10000:
                raise AsmError("data beyond 64 KB at $%X" % a)
            for i, b in enumerate(data):
                if used[a + i] and img[a + i] != b:
                    raise AsmError("overlapping output at $%04X" % (a + i))
                img[a + i], used[a + i] = b, 1
        elif typ == 1:
            break
        elif typ == 4:
            base = (data[0] << 8 | data[1]) << 16
        elif typ in (2, 3, 5):
            pass                            # segment/start records: not used for a ROM
    lo = next((a for a in range(0x10000) if used[a]), None)
    if lo is not None and lo < LO:
        raise AsmError("code at $%04X: below $2000 is not visible to the CPU" % lo)
    return img, used


def build(src, out, defines=(), checksum=False, keep_hex=False):
    base = os.path.splitext(out)[0]
    os.makedirs(os.path.dirname(os.path.abspath(out)) or ".", exist_ok=True)
    files = dict(hex=base + ".hex", lst=base + ".lst", map=base + ".map", sym=base + ".sym")
    log = run_lwasm(src, files["hex"], files["lst"], files["map"], files["sym"], defines)
    img, used = ihex_to_image(files["hex"])
    if checksum:
        from fix_checksum import fix, word_sum
        if used[0xFFF0] or used[0xFFF1]:
            raise AsmError("$FFF0 is used: keep it free for the checksum word")
        img = bytearray(fix(img, 0xFFF0)[0])
        assert word_sum(img) == 0
    with open(out, "wb") as f:
        f.write(img)
    if not keep_hex:
        os.remove(files["hex"])
        files.pop("hex")
    files["bin"] = out
    files["log"] = log
    files["bytes_used"] = sum(used)
    return files


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("src")
    ap.add_argument("out")
    ap.add_argument("-D", "--define", action="append", default=[])
    ap.add_argument("--checksum", action="store_true")
    ap.add_argument("--keep", action="store_true", help="keep the .hex file")
    a = ap.parse_args()
    try:
        r = build(a.src, a.out, a.define, a.checksum, a.keep)
    except AsmError as e:
        sys.exit(str(e))
    if r["log"].strip():
        print(r["log"].strip())
    print("%s: %d bytes of code/data; listing %s" % (r["bin"], r["bytes_used"], r["lst"]))


if __name__ == "__main__":
    main()
