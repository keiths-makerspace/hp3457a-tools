* Display bus driver that needs no RAM and no stack (it must work before RAM is tested).
* A copy of the HP ROM's routines $72B8-$7391 (docs/DISPLAY.md), with the same bit
* timing: every bit is presented on INA/ISA, then PH1 and PH2 are pulsed.
*
* Register conventions while a transaction is open:
*   A = image of OUT2 (never read back; the latch is write-only)
*   X = #OUT2
*   U = return link of the one-level "CALL" (no stack: JSR/BSR are never used)
*   Y = scratch (bit/clock counters, message pointer between calls)
*   B = data
* S is never loaded, so NMI stays disarmed (6809: NMI is ignored until the first LDS).

* one bus clock (ROM $7381): pulses as in the ROM (2+4 cycles each edge), then 12 cycles of
* padding. The ROM calls $7381 as a subroutine, so its clocks are never closer than 36 cycles
* (18 us); unpadded, this macro gave 24. The real LCD has only ever seen HP's speed, so we
* stay at or below it: >= 36 cycles per clock (measured in tests/test_diag.py).
CLK     macro
        ora     #PH1
        sta     ,x
        anda    #$FF-PH1
        sta     ,x
        ora     #PH2
        sta     ,x
        anda    #$FF-PH2
        sta     ,x
        nop
        nop
        nop
        nop
        nop
        nop
        endm

* \1 clocks with the lines as they are (ROM $7361)
NCLK    macro
        ldy     #\1
loop@   CLK
        leay    -1,y
        bne     loop@
        endm

* call a subroutine that returns with JMP ,U
CALL    macro
        leau    ret@,pcr
        lbra    \1
ret@    equ     *
        endm

* send instruction \1 (an 8-bit opcode)
INSTR   macro
        ldb     #\1
        CALL    isend
        endm

* open a transaction (ROM $72F3): one clock with everything low, PWO high, 2 clocks
DOPEN   macro
        clra
        sta     ,x
        CLK
        lda     #PWO
        NCLK    2
        endm

* close a transaction (ROM $7304): 112 clocks, then everything low and one clock
DCLOSE  macro
        NCLK    112
        clra
        CLK
        endm

* select the LCD (ROM $7311): instruction FC, data FD, 48 clocks
DSELECT macro
        INSTR   $FC
        ldb     #$FD
        CALL    dsend8
        anda    #$FF-INA
        NCLK    48
        endm

* pulse the watchdog (OTL*) - only between transactions. Uses A only (not X), so tests can
* hold a pointer in X.
KICK    macro
        lda     #OTL
        sta     >OUT2
        clra
        sta     >OUT2
        endm

* wait \1 x 0.2 s, kicking the watchdog every 25 ms (uses A, B, Y). Everything in the
* diagnostic ROM kicks at least every 25 ms, so a watchdog that fires early (but later than
* HP's own 32.8 ms kick interval) is measured by stage 6 instead of stopping the ROM.
WAIT    macro
        ldb     #\1*8
        KICK                            ; on entry too: SHOW's refresh takes ~16 ms
o@      ldy     #6250                   ; 8 cycles per turn = 25 ms at E = 2 MHz
i@      leay    -1,y
        bne     i@
        KICK                            ; kick after each delay: WAIT ends on a fresh kick
        decb
        bne     o@
        endm

* call the display refresh (returns with JMP ,S; S is a link register, never a stack)
REFRESH macro
        leas    ret@,pcr
        lbra    refresh
ret@    equ     *
        endm
