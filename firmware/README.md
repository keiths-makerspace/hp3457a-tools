# firmware/

Not included: the HP 3457A ROM is HP/Keysight copyrighted. To use the ROM-based tools, read
the 27512 EPROM U602 (03457-88810) from your own 03457-66511 main controller and save it as:

    firmware/main-controller-66511/03457-88810_U602.bin

Or pass `--rom path/to/image.bin` to the tools. `*.bin` files are git-ignored.

## Reference image

The tools were developed against this dump (64 KB, the whole 27512; the CPU sees only
`$2000-$FFFF`):

| | |
|---|---|
| Part | 03457-88810, U602 on a 03457-66511 main controller (label "8707") |
| Size | 65536 bytes |
| MD5 | `df7c3be5c07b41c79efa069c0e40f483` |
| SHA-256 | `e17f81212c9c1de896980c7d8caab223ed685e511ea32e424a44a3e3ffc4b91d` |
| Reset vector | `$8C03` |
| 16-bit sum of `$2000-$FFFF` | `$C739` |

Check yours with `python3 tools/rom_info.py yourdump.bin`. If the hashes differ, list the differing
ranges with `python3 tools/romcompare.py yourdump.bin --ref firmware/main-controller-66511/03457-88810_U602.bin`
(needs the reference image), or post the `rom_info.py` output. A different ROM revision may
not match the addresses the tools use.
