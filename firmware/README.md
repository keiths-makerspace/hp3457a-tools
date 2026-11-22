# firmware/

Not included: the HP 3457A ROM is HP/Keysight copyrighted. To use the ROM-based tools, read
the 27512 EPROM U602 (03457-88810) from your own 03457-66511 main controller and save it as:

    firmware/main-controller-66511/03457-88810_U602.bin

Or pass `--rom path/to/image.bin` to the tools. `*.bin` files are git-ignored.
