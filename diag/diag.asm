* HP 3457A diagnostic ROM for the 03457-66511 main controller - stages 0-2
*
* Build:  python3 tools/asm.py diag/diag.asm build/diag.bin --checksum
* Run:    python3 tools/vmeter.py --rom build/diag.bin --blank-cal   (tests/test_diag.py)
*
* Sequence (repeats forever, counting passes):
*   0  "HP3457A DIAG"                     display works without RAM
*   1  ROM checksum (word sum $2000-$FFFF = 0)      -> "ROM CKSM BAD"
*   2a RAM data lines, walking 1/0 at $0400/$1C00   -> "RAM D3 BAD"
*   2b RAM address lines A0-A12, three base patterns -> "RAM A9 BAD" (aliasing only;
*      a cell that reads back wrong is reported as a cell)
*   2c RAM cells $0200-$1FFF: 00 FF 55 AA, each byte restored -> "RAM 0A3F BAD"
*   3  cal RAM, read only: 33 record checksums + 2 complement pairs
*      -> "CAL RAM OK", or "CAL BAD   nn" then each bad item ("CAL DCV 30MV"), or
*         "CAL RAM LOST" when more than half are bad (battery). Does not halt.
*   4  NMI tick period (E/65536 = 32.768 ms) and line frequency (IN1 b7, a reference
*      independent of the crystal) -> "NMI 32.768MS", "LINE 60.00HZ", or "NO NMI TICK",
*      "NO LINE SENS"; out of tolerance with ERR. Does not halt.
*   5  cross-isolation link: U616 present, transmitter empties; reset the 8051 by holding
*      TX low, its self-test byte; echo all 255 non-zero bytes and classify the errors
*      -> "A/D RESET OK", "ECHO OK", or "NO UART U616", "A/D SILENT", "LINK RX LOW",
*         "A/D TEST BAD", "ECHO ERR nnn" + "SLOW EDGE RX/TX", "ECHO BIT n", "LINK NOISY".
*   6  watchdog: stop kicking with a signature in RAM; the reset comes back to the end of
*      the pass -> "WDOG 524MS", "WDOG EARLY" (< 300 ms), "WDOG NO RST" (none in 1.2 s)
*   7  8291A U614: SPM written, SPS read back (rsv bit 6 never set) -> "HPIB U614 OK",
*      "U614 NO RESP", "U614 Dn BAD"
*   8  keyboard: stuck keys named ("KEY STUCK" + "KEY NPLC"), row shorts, the security jumper
*      ("JUMPER NORM"/"JUMPER KEY"); every pass a "PRESS KEYS" window naming each key
*      (10 s on the first pass, 3 s later, 10 s again after each key; AUTO CAL also runs
*      stage 10 on this pass)
*   9  first pass: display walk (all segments, each position, each annunciator) - by eye
*  10  first pass, every 10th, and after AUTO CAL in the key window: the HP self-test's
*      eight analog steps (TEST key), sent as
*      the HP ROM sends them, with the meter's own ACAL constants (read only) -> "A/D TEST OK"
*      or the failing step ("AC CONVERTER") and "AUXERR   256" (the manual's AUXERR bits);
*      "ACAL REQ'D" if the ACAL constants are invalid, "A/D NO ACK" / "A/D NO RDG" + step.
*      Leaves the 8051 reset (power-on state). PROPOSAL: review before use on a meter.
*   then "ALL PASSED" (or "CAL ERRORS" / "ERRORS FOUND") and "PASS    0001" (decimal count)
* Failures halt on the message with ERR lit.
*
* Rules (docs/DIAG_ROM.md): never touch $0030-$0037 (CALREQ); never write $0040-$01FF
* (cal RAM). Every RAM byte tested is restored. Only $020F-$021C (the HP firmware's PAD,
* rebuilt at every HP boot) is used as scratch, after it has passed.
* No stack: S is loaded only as a link/data register. That arms NMI, but OUT1 = 0 keeps the
* hardware NMI gate (MASK*) closed, so no NMI can occur.

        pragma  c,ct
* DP holds the message index, not 0: never let lwasm use direct-page addressing. $40xx is
* never accessed as data, so every reference to $00xx (I/O) assembles as extended.
        setdp   $40
        include "hw.inc"
        include "display.inc"

* E clock in kHz: Y601 / 4. The faulty meter's 66511 has a 7.980 MHz crystal -> 1995.
* Override with -D EKHZ=2000 for an 8.000 MHz board. Must be a multiple of 5 (see below).
        ifndef  EKHZ
EKHZ    equ     1995
        endc
EK5     equ     EKHZ/5                  ; 399 for 1995 kHz

MSGLEN  equ     14                      ; table entry: 0 sentinel, 12 characters, flags
RAMIDX  equ     $FF                     ; message index of the RAM message buffer
RAMMSG  equ     $0210                   ; RAM message: sentinel $020F, text, flags $021C
DTEST1  equ     $0400                   ; data-line test cells (outside the scratch buffer)
DTEST2  equ     $1C00
CELLLO  equ     $0200                   ; cell test range: everything above cal RAM
CELLHI  equ     $2000
ERRF    equ     $02                     ; annunciator bit 1 = ERR
* stage 3 work area: the HP firmware's data stack $028B-$02EF (volatile, rebuilt at boot)
RAM2IDX equ     $FE                     ; message index of the second RAM buffer
RAM2    equ     $02A0                   ; second RAM message: sentinel $029F, text, flags $02AC
BADLIST equ     $02B0                   ; message indices of bad cal items (up to 35)
NBAD    equ     $02D8                   ; number of bad cal items
IDX     equ     $02D9                   ; list position while showing them
* stage 4 (still in the HP data stack $028B-$02EF)
NMICNT  equ     $02DA                   ; 16-bit NMI count (handler `nmih`)
DIVN    equ     $02DC                   ; 32-bit dividend / quotient
DIVD    equ     $02E0                   ; 16-bit divisor
REM     equ     $02E2                   ; 16-bit remainder
NUM16   equ     $02E4                   ; value to print
TMP     equ     $02E6
TMP2    equ     $02E8
NWARN   equ     $02EA                   ; timing/other errors found this pass
DIG     equ     $02EB                   ; 5 ASCII digits of NUM16
STACKTOP equ    $0287                   ; real stack for stages 4-5: HP return stack area
* stage 5 variables: the HP PAD below the RAM message buffer ($0200-$020E)
SENT    equ     $0200                   ; byte being echoed
ERRS    equ     $0201                   ; echoes with wrong data
LOST    equ     $0202                   ; bytes with no echo
FLAG    equ     $0203                   ; echoes with parity/framing error
DROP    equ     $0204                   ; wrong echoes that only lost 1 bits
XORO    equ     $0205                   ; OR of (sent ^ received)
XORA    equ     $0206                   ; AND of (sent ^ received)
REENT   equ     $0207                   ; 8051 re-resets during the echo test
BRK     equ     $0208                   ; break characters (SIN low) seen
NOTH    equ     $0209                   ; unexpected bytes while waiting for self-test
OTHER   equ     $020A                   ; last unexpected byte
TRIES   equ     $020B                   ; 0.1 s waits left
LSTAT   equ     $020C                   ; result of adreset
* stage 6: survive the watchdog reset (battery RAM keeps them)
WDSIG   equ     $020D                   ; 2-byte signature: watchdog test in progress
WDCNT   equ     $021D                   ; 16-bit count of 40-cycle loops since the last kick
WDMAGIC equ     $A55A
RUNSIG  equ     $021F                   ; 2-byte signature: a pass is running
STAGE   equ     $0221                   ; stage number of the running pass
RUNMAGIC equ    $5AA5
FIRST   equ     $0222                   ; 1 on the first pass after reset (stages 8-9 extras)
* stage 7-9 variables (HP display buffer $02F1-$02FF: volatile, rebuilt by the HP ROM)
KCOL    equ     $02F1                   ; column being scanned
KROW    equ     $02F2                   ; rows read (complemented: 1 = pressed)
KIDX    equ     $02F3                   ; found key: row*8+col
KTIME   equ     $02F4                   ; 25 ms slots left in the PRESS KEYS window
RAWMODE equ     $02F5                   ; display walk: 0 = all on, 1 = position RAWPOS only
RAWPOS  equ     $02FB                   ; 16-bit (compared with Y)
ANNW    equ     $02F7                   ; 16-bit annunciator word for the raw refresh
GPAT    equ     $02F9                   ; 8291A pattern under test
GBAD    equ     $02FA                   ; 8291A bits read back wrong (OR)
KSTK    equ     $02FD                   ; a key or row is stuck: skip the press window
ADEM    equ     $02FE                   ; AUTO CAL pressed in the window: run stage 10 now
KWLONG  equ     200                     ; PRESS KEYS window in 50 ms slots: 10 s (first
KWSHORT equ     60                      ; pass and after each key), 3 s on later passes
* stage 10 variables: the unused part of the HP data stack $0288-$029E (volatile)
AFAIL   equ     $0289                   ; 16-bit AUXERR bits of the failed steps
AVAL    equ     $028B                   ; 32-bit reading / answer (signed)
AFLG    equ     $028F                   ; b0 AC block ($050C-$0516) valid, b1 $0517/$0519 valid
SPTR    equ     $0290                   ; step-table pointer
CURSTEP equ     $0292                   ; message index of the step running
ATMP    equ     $0293                   ; count / message index
ATRY    equ     $0294                   ; 12 ms waits left
ANB     equ     $0295                   ; bytes in the reading (2 or 4)
* the HP ROM's link state STAT ($03D0, volatile, rebuilt at every HP boot). Stage 10 sets it
* as the HP ROM does, after the last byte of each frame (2 = ACK, 1 = reading, 6 = 2-byte
* answer) and clears it when the reply has come.
* Nothing on the meter reads it while the diagnostic ROM runs; the emulator's 8051 stand-in
* uses it to tell what the main controller is waiting for (docs/EMULATOR.md).
STATH   equ     $03D0
* ACAL constants (battery RAM, written by ACAL; read only here)
ACALT   equ     $050C                   ; 5 words: AC offset / AC converter readings
ACALCK  equ     $0516                   ; complement of their byte sum
ACALPP  equ     $0517                   ; ohms precharge, complement at $0519
NMIWAIT equ     6000                    ; ~35-cycle loop with a kick: 0.1 s for the first tick
LTIMEOUT equ    5000                    ; 20-cycle loop: one period within 50 ms (> 20 Hz)
LWAIT   equ     3000                    ; edge waits (with a kick): ~0.1 s

* Y -> last character of message DP (ROM table or the RAM buffer); flags at 1,Y.
* Used inside a transaction, where A = PWO after an instruction; MUL clobbers A.
MSGPTR  macro
        tfr     dp,b
        cmpb    #RAMIDX
        beq     r@
        cmpb    #RAM2IDX
        beq     r2@
        lda     #MSGLEN
        mul
        leay    msgtab+12,pcr
        leay    d,y
        bra     e@
r2@     ldy     #RAM2+11
        bra     e@
r@      ldy     #RAMMSG+11
e@      lda     #PWO
        endm

* show message index B and wait \1 x 0.2 s
SHOWB   macro
        tfr     b,dp
        KICK
        REFRESH
        WAIT    \1
        endm

* NUM16 -> 5 ASCII digits at DIG (repeated subtraction; uses D, X, Y)
DEC5    macro
        ldd     >NUM16
        ldx     #DIG
        DECP    10000
        DECP    1000
        DECP    100
        DECP    10
        addb    #'0'
        stb     ,x
        endm
DECP    macro
        ldy     #'0'
l@      subd    #\1
        bcs     d@
        leay    1,y
        bra     l@
d@      addd    #\1
        exg     d,y
        stb     ,x+
        exg     d,y
        endm

* DIVN (32 bit) / DIVD (16 bit): quotient in DIVN, remainder in REM (shift-subtract).
* The divisor must be < $8000. Uses D, Y.
DIV32   macro
        ldd     #0
        std     >REM
        ldy     #32
l@      lsl     >DIVN+3
        rol     >DIVN+2
        rol     >DIVN+1
        rol     >DIVN
        rol     >REM+1
        rol     >REM
        ldd     >REM
        subd    >DIVD
        bcs     n@
        std     >REM
        inc     >DIVN+3
n@      leay    -1,y
        bne     l@
        endm

* DIVN (32 bit) := D (16 bit) x \1 (8 bit, immediate). Uses A, B, TMP, TMP2.
MULD8   macro
        std     >TMP                    ; TMP = hi:lo
        lda     >TMP+1
        ldb     #\1
        mul                             ; lo x k
        std     >DIVN+2
        lda     >TMP
        ldb     #\1
        mul                             ; hi x k
        addb    >DIVN+2
        adca    #0
        stb     >DIVN+2
        sta     >DIVN+1
        clr     >DIVN
        endm

* DIVN (32 bit) := D (16 bit) x \1 (16 bit, immediate): four 8x8 MULs. Uses A, B, TMP.
MULDW   macro
        std     >TMP                    ; TMP = a:b
        lda     >TMP+1
        ldb     #(\1)&255
        mul                             ; b*d
        std     >DIVN+2
        lda     >TMP
        ldb     #(\1)/256
        mul                             ; a*c
        std     >DIVN
        lda     >TMP
        ldb     #(\1)&255
        mul                             ; a*d << 8
        addd    >DIVN+1
        std     >DIVN+1
        bcc     n1@
        inc     >DIVN
n1@     lda     >TMP+1
        ldb     #(\1)/256
        mul                             ; b*c << 8
        addd    >DIVN+1
        std     >DIVN+1
        bcc     n2@
        inc     >DIVN
n2@     equ     *
        endm

* DIVN (32 bit) += \1 (16 bit, immediate). Uses A, B.
ADDN    macro
        ldd     >DIVN+2
        addd    #\1
        std     >DIVN+2
        bcc     a@
        ldd     >DIVN
        addd    #1
        std     >DIVN
a@      equ     *
        endm

* copy the 12-character template \1 into the second RAM buffer, flags 0 (uses A, B, Y, U)
TORAM2  macro
        leay    \1,pcr
        ldu     #RAM2
        ldb     #12
l@      lda     ,y+
        sta     ,u+
        decb
        bne     l@
        clr     >RAM2-1
        clr     >RAM2+12
        endm

* wait for a received byte, \1 turns of 16 cycles. C=1: timeout. C=0: A = data,
* B = status (b3 PE, b4 FE, b5 OE). Reads $0018 afterwards to clear DR/OE. Uses X.
RXW     macro
        ldx     #\1
l@      ldb     >UART_ST
        bmi     g@
        leax    -1,x
        bne     l@
        orcc    #1
        bra     e@
g@      lda     >UART_RX
        tst     >UART_CL
        andcc   #$FE
e@      equ     *
        endm

* wait until the transmit holding register is empty (no timeout: checked in stage 5a)
TBRE    macro
l@      lda     >UART_ST
        bita    #$40
        beq     l@
        endm

* call the raw refresh (display walk)
RAWREF  macro
        KICK
        leas    ret@,pcr
        lbra    rawref
ret@    equ     *
        endm

* 12 nibbles, character 11 first: \1 where lit, \2 where blank (Y counts positions)
RAWNIB  macro
        ldy     #11
n@      ldb     #\1
        tst     >RAWMODE
        beq     s@
        cmpy    >RAWPOS                 ; Y = character index, 11 = rightmost
        beq     s@
        ldb     #\2
s@      CALL    dsend4
        leay    -1,y
        cmpy    #-1
        bne     n@
        anda    #$FF-INA
        NCLK    8
        endm

* cal table entry: address, size (record) or length (complement pair), type, item number
CALREC  macro
        fdb     \1
        fcb     \2,\3,M_CALREC0+\4
        endm

* B (0-15) -> ASCII hex digit
HEXB    macro
        cmpb    #10
        blo     d@
        addb    #'A'-'0'-10
d@      addb    #'0'
        endm

* mark the running stage (for unexpected-reset reports)
STG     macro
        lda     #\1
        sta     >STAGE
        endm

* show message \1 (index) and wait \2 x 0.2 s
SHOW    macro
        KICK
        lda     #\1
        tfr     a,dp
        REFRESH
        WAIT    \2
        endm

* address-line test from base \1: for each line k, cells base and base^(1<<k) must be
* distinct. Both cells are saved in Y and restored. S holds the line mask.
ADDRT   macro
        KICK
        lds     #1
a@      ldx     #\1
        tfr     s,d
        eora    #(\1)/256
        eorb    #(\1)&255
        tfr     d,u
        lda     ,x
        ldb     ,u
        tfr     d,y
        lda     #$55
        sta     ,x
        lda     #$AA
        sta     ,u
        lda     ,x
        ldb     ,u
        cmpd    #$55AA
        bne     f@
        tfr     y,d
        sta     ,x
        stb     ,u
        tfr     s,d
        lslb
        rola
        tfr     d,s
        cmpd    #$2000
        bne     a@
        bra     ok@
f@      cmpa    #$AA                    ; base reads its partner's value: the cells alias
        beq     al@
        cmpa    #$55
        bne     cx@
        tfr     y,d                     ; base fine, partner wrong: the partner cell is bad
        sta     ,x
        stb     ,u
        tfr     y,d
        tfr     b,a
        tfr     u,x
        lbra    cbad
cx@     tfr     y,d                     ; base cell itself is bad
        sta     ,x
        stb     ,u
        tfr     y,d
        lbra    cbad
al@     tfr     y,d
        sta     ,x
        stb     ,u
        lbra    addrfail
ok@     equ     *
        endm

* data-line test at address \1: walking 1 and walking 0, cell restored.
* Out: A = bits wrong in the first failing pattern, 0 = pass. Uses B, U, DP.
DATAT   macro
        ldu     #\1
        ldb     ,u
        tfr     b,dp
        ldb     #1
w1@     stb     ,u
        cmpb    ,u
        bne     f1@
        lslb
        bne     w1@
        ldb     #1
w0@     tfr     b,a
        coma
        sta     ,u
        cmpa    ,u
        bne     f0@
        lslb
        bne     w0@
        clra
        bra     r@
f1@     tfr     b,a
f0@     eora    ,u
r@      tfr     dp,b
        stb     ,u
        endm

* one cell pattern: write \1, read back, fail to cbad
CELLP   macro
        ldb     #\1
        stb     ,x
        cmpb    ,x
        lbne    cbad
        endm

        org     $C000                   ; 16 KB below the vectors (CPU sees $2000-$FFFF)

* =================================================================================
reset   orcc    #$50                    ; no IRQ/FIRQ
        clra
        tfr     a,dp
        sta     >OUT1                   ; MASK* low: NMI gate closed
        sta     >OUT2
        ldd     >WDSIG                  ; reset by the watchdog test? (reading RAM is safe
        cmpd    #WDMAGIC                ; even if it's bad; it only decides where to go)
        lbeq    wdback                  ; (FIRST kept: it is the same pass)
        lda     #1                      ; first pass after a reset (RAM not yet tested:
        sta     >FIRST                  ; a write here is harmless, the cell test restores)
        ldd     >RUNSIG                 ; reset in the middle of a pass?
        cmpd    #RUNMAGIC
        lbne    loop
        ldd     #0
        std     >RUNSIG
        TORAM2  t_stage                 ; "IN STAGE n"
        lda     >STAGE
        anda    #$0F
        adda    #'0'
        sta     >RAM2+9
        lda     #ERRF
        sta     >RAM2+12
        SHOW    M_RESTART,5
        SHOW    RAM2IDX,5

loop    ldd     #RUNMAGIC
        std     >RUNSIG
        STG     0
        SHOW    M_HELLO,5
* ---- stage 1: ROM checksum --------------------------------------------------------
        STG     1
        SHOW    M_ROMCK,1
        ldy     #$2000
        ldd     #0
ck@     ldx     #1024                   ; 1024 words x 17 cycles = 8.7 ms per chunk
ci@     addd    ,y++
        leax    -1,x
        bne     ci@
        tfr     d,u                     ; keep the sum while kicking (KICK uses A)
        KICK
        tfr     u,d
        cmpy    #0                      ; Y wraps to 0 after $FFFE: 28 chunks
        bne     ck@
        cmpd    #0
        beq     ckok@
        ldb     #M_ROMBAD
        lbra    halt
ckok@   SHOW    M_ROMOK,3
* ---- stage 2a: RAM data lines ------------------------------------------------------
        STG     2
* A failure at DTEST1 is a data line only if DTEST2 fails too; otherwise that cell is bad.
        SHOW    M_RAMDAT,1
        DATAT   DTEST1
        tsta
        beq     dok@
        tfr     a,b                     ; keep the first mask in S
        clra
        tfr     d,s
        DATAT   DTEST2
        tsta
        bne     dline@
        ldx     #DTEST1                 ; only DTEST1 fails: report the cell
        lda     ,x
        lbra    cbad
dline@  tfr     s,d
        tfr     b,a
        ldb     #M_RAMDGEN
        tsta
        lbeq    halt
        ldb     #M_RAMD0-1
dbit@   incb                            ; lowest wrong bit -> "RAM Dn BAD"
        lsra
        bcc     dbit@
        lbra    halt
dok@    equ     *
* ---- stage 2b: RAM address lines -----------------------------------------------------
        SHOW    M_RAMADR,1
        ADDRT   $1FFF
        ADDRT   $0AAA
        ADDRT   $1555
* ---- stage 2c: RAM cells (each byte saved, tested, restored) -------------------------
        SHOW    M_RAMCEL,1
        ldx     #CELLLO
cell@   lda     ,x
        CELLP   $00
        CELLP   $FF
        CELLP   $55
        CELLP   $AA
        sta     ,x                      ; restore
        cmpa    ,x
        lbne    cbad
        leax    1,x
        tfr     x,d
        tstb
        bne     nk@
        KICK                            ; every 256 bytes
nk@     cmpx    #CELLHI
        bne     cell@
        SHOW    M_RAMOK,3
* ---- stage 3: cal RAM, read only ---------------------------------------------------
        STG     3
* type 0: record of `size` bytes ending in ~(sum of the others); type 1: `size` bytes
* followed by their complements. Nothing in $0040-$01FF is ever written.
        SHOW    M_CALCK,1
        clr     >NBAD
        leay    caltab,pcr
cl@     ldx     ,y                      ; item address, 0 = end of table
        beq     cdone@
        tst     3,y
        bne     cpl@
        ldb     2,y                     ; checksum record
        decb
        clra
cs@     adda    ,x+
        decb
        bne     cs@
        coma
        cmpa    ,x
        beq     cnext@
        bra     cbad@
cpl@    ldb     2,y                     ; complement pair
        leau    b,x
cp2@    lda     ,x+
        coma
        cmpa    ,u+
        bne     cbad@
        decb
        bne     cp2@
        bra     cnext@
cbad@   lda     >NBAD                   ; remember the item's message
        ldu     #BADLIST
        ldb     4,y
        stb     a,u
        inca
        sta     >NBAD
cnext@  leay    5,y
        bra     cl@
cdone@  lda     >NBAD
        bne     cbad2@
        SHOW    M_CALOK,3
        lbra    cend@
cbad2@  cmpa    #NCAL/2
        bls     clist@
        SHOW    M_CALLOST,5             ; more than half bad: battery / RAM lost
        lbra    cend@
clist@  leay    t_calbad,pcr            ; "CAL BAD   nn" in the second buffer
        ldu     #RAM2
        ldb     #12
ccp@    lda     ,y+
        sta     ,u+
        decb
        bne     ccp@
        clr     >RAM2-1
        lda     #ERRF
        sta     >RAM2+12
        lda     >NBAD
        ldb     #'0'-1
ct@     incb                            ; tens by subtraction (count <= 35)
        suba    #10
        bcc     ct@
        adda    #10+'0'
        stb     >RAM2+10
        sta     >RAM2+11
        SHOW    RAM2IDX,5
        clr     >IDX
cshow@  lda     >IDX
        cmpa    >NBAD
        bhs     cend@
        ldu     #BADLIST
        ldb     a,u
        inc     >IDX
        SHOWB   5
        bra     cshow@
cend@   equ     *
* ---- stage 4: NMI tick and line frequency ---------------------------------------------
        STG     4
* The NMI needs a real stack: RAM has passed, and the HP return stack area is free. The
* gate (OUT1 b0 = MASK*) is opened only for the measurement.
        clr     >NWARN
        SHOW    M_TIMERS,1
        KICK
        ldd     #0
        std     >NMICNT
        lds     #STACKTOP
        lda     #$01
        sta     >OUT1                   ; MASK* high: NMI gate open
        ldx     #0
nw@     KICK                            ; wait for the first tick (untimed)
        leax    1,x
        cmpx    #NMIWAIT
        bhs     nmito@
        ldb     >NMICNT+1
        beq     nw@
        ldx     #0
        incb
nm@     leax    1,x                     ; one more tick: 5+5+3 = 13 cycles/turn
        cmpb    >NMICNT+1
        bne     nm@
        bra     nmigo@
nmito@  lbra    nonmi@                  ; trampoline: keeps the timed loops at short branches
nmigo@  clra
        sta     >OUT1                   ; NMI gate closed again
        KICK
        cmpx    #10000                  ; period beyond 65 ms: report as missing
        lbhs    nonmi@
* cycles = 13 X + 44 (handler) + 12 (detection latency, centred), close to 65536, so the
* arithmetic is 32-bit: us = cycles x 200 / EK5 = (2600 X + 11200) / EK5, rounded
        tfr     x,d
        MULDW   2600
        ADDN    11200+EK5/2
        ldd     #EK5
        std     >DIVD
        DIV32
        ldd     >DIVN+2
        std     >NUM16
        TORAM2  t_nmi
        DEC5
        ldd     >DIG                    ; "NMI dd.dddMS"
        std     >RAM2+4
        ldd     >DIG+2
        std     >RAM2+7
        lda     >DIG+4
        sta     >RAM2+9
        ldd     >NUM16                  ; +-1 % of 65536 cycles at EKHZ
        cmpd    #(65536*1000/EKHZ)*99/100
        blo     nmibad@
        cmpd    #(65536*1000/EKHZ)*101/100
        bls     nmiok@
nmibad@ lda     #ERRF
        sta     >RAM2+12
        inc     >NWARN
nmiok@  SHOW    RAM2IDX,5
        bra     line@
nonmi@  clra
        sta     >OUT1                   ; close the gate
        inc     >NWARN
        SHOW    M_NONMI,5
* line frequency: 4 periods of IN1 b7, 20 cycles per loop turn
line@   KICK
        ldx     #0
l1@     KICK                            ; wait for b7 low (untimed)
        leax    1,x
        cmpx    #LWAIT
        bhs     lto@
        lda     >IN1
        bmi     l1@
l2@     KICK                            ; then the rising edge
        leax    1,x
        cmpx    #LWAIT
        bhs     lto@
        lda     >IN1
        bpl     l2@
        ldx     #0
        ldb     #1
p1@     leax    1,x
        cmpx    #LTIMEOUT
        bhs     lto@
        lda     >IN1
        bmi     p1@
p2@     leax    1,x
        cmpx    #LTIMEOUT
        bhs     lto@
        lda     >IN1
        bpl     p2@
        decb
        bne     p1@
        bra     lgo@
lto@    lbra    noline@                 ; trampoline (short branches keep 20-cycle loops)
lgo@    KICK
* f x 100 = 1 period of 20 X cycles at EKHZ kHz, x 100 = EKHZ x 5000 / X
        stx     >DIVD
        ldd     #(EKHZ*5000)/65536
        std     >DIVN
        ldd     #(EKHZ*5000)&$FFFF
        std     >DIVN+2
        DIV32
        ldd     >DIVN+2
        std     >NUM16
        TORAM2  t_line
        DEC5
        ldd     >DIG+1                  ; "LINE dd.ddHZ"
        std     >RAM2+5
        ldd     >DIG+3
        std     >RAM2+8
        ldd     >NUM16                  ; within 2 % of 50 or 60 Hz
        cmpd    #4900
        blo     lbad@
        cmpd    #5100
        bls     lok@
        cmpd    #5880
        blo     lbad@
        cmpd    #6120
        bls     lok@
lbad@   lda     #ERRF
        sta     >RAM2+12
        inc     >NWARN
lok@    SHOW    RAM2IDX,5
        bra     tend@
noline@ inc     >NWARN
        SHOW    M_NOLINE,5
tend@   equ     *
* ---- stage 5: cross-isolation link (U616, J613, optos A2U509/U510, the 8051) -----------
        STG     5
        SHOW    M_LINK,1
        lds     #STACKTOP               ; adreset is a subroutine (RAM and stack proven)
        lda     >UART_ST
        cmpa    #$FF                    ; nothing drives the data bus: U616 not answering
        bne     u1@
        inc     >NWARN
        SHOW    M_NOUART,5
        lbra    lend@
u1@     ldx     #2000                   ; the transmitter must empty its holding register
t1@     lda     >UART_ST
        bita    #$40
        bne     t2@
        leax    -1,x
        bne     t1@
        lbra    txstk@
t2@     clrb
        stb     >UART_TX
        ldx     #2000
t3@     lda     >UART_ST
        bita    #$40
        bne     t4@
        leax    -1,x
        bne     t3@
        lbra    txstk@
t4@     lbsr    adreset
        sta     >LSTAT
        tsta
        bne     r1@
        SHOW    M_ADOK,3
        lbra    echo@
r1@     cmpa    #1
        bne     r2@
        inc     >NWARN
        SHOW    M_ADTEST,5              ; the 8051 reports its own self-test failed
        lbra    echo@                   ; it still enters echo mode: test the link anyway
r2@     cmpa    #2
        bne     r3@
        inc     >NWARN
        SHOW    M_SILENT,5
        SHOW    M_CHKOPTO,5
        lbra    lend@
r3@     cmpa    #3
        bne     r4@
        inc     >NWARN
        SHOW    M_RXLOW,5
        lbra    lend@
r4@     cmpa    #4
        lbne    echo@                   ; 5: a garbled reply - the 8051 is alive, echo test
        inc     >NWARN
        TORAM2  t_adsent                ; "A/D SENT xx"
        lda     #ERRF
        sta     >RAM2+12
        ldb     >OTHER
        lsrb
        lsrb
        lsrb
        lsrb
        HEXB
        stb     >RAM2+9
        ldb     >OTHER
        andb    #$0F
        HEXB
        stb     >RAM2+10
        SHOW    RAM2IDX,5
        lbra    lend@
txstk@  inc     >NWARN
        SHOW    M_TXSTUCK,5
        lbra    lend@
* echo every non-zero byte; the 8051 echoes until it receives 00
echo@   lds     #STACKTOP
        clr     >ERRS
        clr     >LOST
        clr     >FLAG
        clr     >DROP
        clr     >XORO
        clr     >REENT
        lda     #$FF
        sta     >XORA
        lda     #1
        sta     >SENT
e1@     lda     >SENT
        anda    #$03
        bne     e2@
        KICK
e2@     TBRE
        ldb     >SENT
        stb     >UART_TX
        RXW     250                     ; 2 ms
        lbcs    elost@
        bitb    #$18
        beq     e3@
        inc     >FLAG
e3@     cmpa    >SENT
        beq     enext@
        inc     >ERRS
        cmpa    #$0F                    ; maybe an ACK: did the 8051 receive 00?
        bne     e4@
        TBRE                            ; send the byte again: still echoing -> data error
        ldb     >SENT
        stb     >UART_TX
        RXW     250
        bcs     eack@
        lda     #$0F                    ; it answered: treat the 0F as a wrong echo
        bra     e4@
eack@   inc     >DROP
        lda     >SENT
        sta     >XORO                   ; (all bits of the sent byte were lost)
        inc     >REENT
        lda     >REENT
        cmpa    #3
        bhi     edone@
        lbsr    adreset                 ; back into echo mode
        tsta
        beq     enext@
        cmpa    #5
        beq     enext@
        bra     edone@
e4@     tfr     a,b                     ; B = received
        eora    >SENT                   ; A = wrong bits
        sta     >TMP                    ; (RAM temporaries: no reliance on S as a stack)
        ora     >XORO
        sta     >XORO
        lda     >TMP
        anda    >XORA
        sta     >XORA
        lda     >TMP
        lda     >SENT                   ; only 1 bits lost? received & ~sent == 0
        coma
        stb     >TMP
        anda    >TMP
        bne     enext@
        inc     >DROP
        bra     enext@
elost@  inc     >LOST
enext@  inc     >SENT
        lbne    e1@
edone@  TBRE
        clrb
        stb     >UART_TX                ; 00 ends echo mode (the 8051 answers 0F)
        KICK
* classify
        clra
        ldb     >ERRS
        addb    >LOST
        adca    #0
        std     >NUM16
        bne     ebad@
        SHOW    M_ECHOOK,3
        lbra    lend@
ebad@   inc     >NWARN
        TORAM2  t_echoerr               ; "ECHO ERR nnn"
        lda     #ERRF
        sta     >RAM2+12
        DEC5
        ldd     >DIG+2
        std     >RAM2+9
        lda     >DIG+4
        sta     >RAM2+11
        SHOW    RAM2IDX,5
        lda     >LOST
        cmpa    #255
        bne     c1@
        SHOW    M_NOECHO,5
        lbra    lend@
c1@     lda     >ERRS
        lbeq    cnoisy@
        lda     >XORO                   ; the same single bit wrong every time?
        cmpa    >XORA
        bne     c2@
        tfr     a,b
        decb
        stb     >TMP
        anda    >TMP
        bne     c2@
        TORAM2  t_echobit               ; "ECHO BIT n"
        lda     #ERRF
        sta     >RAM2+12
        lda     >XORO
        ldb     #'0'-1
cb@     incb
        lsra
        bcc     cb@
        stb     >RAM2+9
        SHOW    RAM2IDX,5
        lbra    lend@
c2@     lda     >DROP                   ; every error only lost 1 bits: a slow edge
        cmpa    >ERRS
        lbne    cnoisy@
        ldb     >ERRS                   ; flagged by the main UART on more than half of
        lsrb                            ; the errors: the 8051 -> main path
        cmpb    >FLAG
        bhs     ctx@
        SHOW    M_SLOWRX,5
        lbra    lend@
ctx@    SHOW    M_SLOWTX,5
        lbra    lend@
cnoisy@ SHOW    M_NOISY,5
lend@   equ     *
* ---- stage 6: watchdog ---------------------------------------------------------------
        STG     6
        SHOW    M_WDOG,1
        ldd     #WDMAGIC
        std     >WDSIG
        KICK                            ; the last kick
        ldd     #0
        std     >WDCNT
wd@     ldd     >WDCNT                  ; 6+4+6+8x2+5+3 = 40 cycles per turn (listing)
        addd    #1
        std     >WDCNT
        nop
        nop
        nop
        nop
        nop
        nop
        nop
        nop
        cmpd    #60000                  ; 60000 x 40 cycles = 1.2 s without a reset
        blo     wd@
        ldd     #0
        std     >WDSIG
        KICK
        inc     >NWARN
        SHOW    M_WDNORST,5
        lbra    stage7

* the watchdog reset brings us here (from `reset`), with RAM intact
wdback  ldd     #0
        std     >WDSIG
        KICK
        ldd     >WDCNT                  ; ms = count x 40 cycles / EKHZ = count x 8 / EK5
        MULD8   8
        ADDN    EK5/2                   ; round to nearest
        ldd     #EK5
        std     >DIVD
        DIV32
        ldd     >DIVN+2
        std     >NUM16
        TORAM2  t_wdog
        DEC5
        ldd     >DIG+1                  ; "WDOG nnnnMS", leading zero blank
        cmpa    #'0'
        bne     wdz@
        lda     #' '
wdz@    std     >RAM2+5
        ldd     >DIG+3
        std     >RAM2+7
        ldd     >NUM16
        cmpd    #300
        blo     wdearly@
        cmpd    #800
        bls     wdok@
        lda     #ERRF                   ; late (but it did reset)
        sta     >RAM2+12
        inc     >NWARN
wdok@   SHOW    RAM2IDX,5
        lbra    stage7
wdearly@ lda    #ERRF
        sta     >RAM2+12
        inc     >NWARN
        SHOW    RAM2IDX,5
        SHOW    M_WDEARLY,5
* ---- stage 7: HP-IB controller 8291A U614 ---------------------------------------------
* SPM (register 3, write) reads back through SPS (register 3, read). Bit 6 (rsv, which
* would assert SRQ) is never set. Chip reset (aux 02) before and after. (verify the SPS
* readback on the data sheet)
stage7  STG     7
        SHOW    M_HPIB,1
        lda     #$02
        sta     >GPIB+5                 ; aux: chip reset
        clr     >GBAD
        leay    gpats,pcr
g1@     lda     ,y+
        sta     >GPAT
        sta     >GPIB+3                 ; SPM
        lda     >GPIB+3                 ; SPS
        eora    >GPAT
        anda    #$BF                    ; bit 6 = rsv/PEND: not compared
        ora     >GBAD
        sta     >GBAD
        lda     >GPAT
        bne     g1@                     ; table ends with 00
* Address 0 register (6W, ARS=0) reads back through 6R bits 0-6 (bit 6 = DT0): this also
* covers D6, which SPM/SPS can't. Finally DT=DL=1 in both address registers: the meter
* neither talks nor listens while the diagnostics run (the HP ROM reprograms it at boot).
        leay    gapats,pcr
g5@     lda     ,y+
        sta     >GPAT
        sta     >GPIB+6
        lda     >GPIB+6
        eora    >GPAT
        anda    #$7F                    ; bit 7 = INT: not compared
        ora     >GBAD
        sta     >GBAD
        lda     >GPAT
        cmpa    #$60
        bne     g5@                     ; table ends with $60 (DT0 = DL0 = 1)
        lda     #$E0                    ; ARS=1: Address 1 register, DT1 = DL1 = 1
        sta     >GPIB+6
        lda     #$02
        sta     >GPIB+5
        clr     >GPIB+3
        lda     >GBAD
        bne     g2@
        SHOW    M_HPIBOK,3
        lbra    stage8
g2@     inc     >NWARN
        lda     >GPIB+3                 ; SPS and Address 0 both read $FF: nothing answers
        cmpa    #$FF
        bne     g3@
        lda     >GPIB+6
        cmpa    #$FF
        bne     g3@
        SHOW    M_HPIBNO,5
        lbra    stage8
g3@     TORAM2  t_u614                  ; "U614 Dn BAD": lowest wrong bit
        lda     #ERRF
        sta     >RAM2+12
        lda     >GBAD
        ldb     #'0'-1
g4@     incb
        lsra
        bcc     g4@
        stb     >RAM2+6
        SHOW    RAM2IDX,5
* ---- stage 8: keyboard and the security jumper ----------------------------------------
* Columns O1-O8 are OUT1 bits (O6 = b0 = also MASK*, the NMI gate), rows I1-I6 are IN1
* b0-b5. With a real stack the NMI may run while columns are scanned; the gate is closed
* (OUT1 b0 = 0) again before every display refresh.
stage8  STG     8
        clr     >KSTK
        clr     >ADEM
        SHOW    M_KEYS,1
        lds     #STACKTOP
        lda     #$FF                    ; no column driven
        sta     >OUT1
        lda     >IN1
        coma
        anda    #$3F
        lbne    rowsh@                  ; a row low with no column driven: shorted
        lbsr    kscan                   ; any key down?
        tsta
        beq     kok@
        clra
        sta     >OUT1                   ; gate closed before showing
        inc     >NWARN
        SHOW    M_KSTUCK,5
        ldb     >KIDX
        addb    #M_KEY0
        SHOWB   5
        inc     >KSTK                   ; no press window with a key stuck down
        lbra    jump@
kok@    clra
        sta     >OUT1
        SHOW    M_KOK,3
        lbra    jump@
rowsh@  clra
        sta     >OUT1
        inc     >KSTK                   ; no press window with a row shorted
        inc     >NWARN
        TORAM2  t_rowsh                 ; "ROW n SHORT": lowest low row
        lda     #ERRF
        sta     >RAM2+12
        lda     >KROW
        lda     >IN1                    ; re-read with O6 driven: still informative
        coma
        anda    #$3F
        ldb     #'0'
r1@     incb
        lsra
        bcc     r1@
        stb     >RAM2+4
        SHOW    RAM2IDX,5
* security jumper JM621 (pin 1 O6, pin 2 IN1 b6 with pull-up, pin 3 GND, pin 4 open):
*   NORMAL (3-4): b6 high; SECURE DIS. "KEY" (1-2): b6 follows O6; DSA (2-3): b6 low
jump@   lds     #STACKTOP               ; O6 high opens the NMI gate: real stack
        lda     #$FE                    ; O6 (b0) low, the other columns undriven
        sta     >OUT1
        lda     >IN1
        anda    #$40
        sta     >TMP                    ; b6 with O6 low
        lda     #$FF                    ; O6 high
        sta     >OUT1
        ldb     >IN1
        clra
        sta     >OUT1                   ; gate closed again
        andb    #$40                    ; b6 with O6 high
        bne     j1@
        SHOW    M_JDSA,5                ; low either way: grounded
        bra     kwin@
j1@     tst     >TMP
        bne     jn@
        SHOW    M_JKEY,5                ; follows O6: SECURE DIS., cal security disabled
        bra     kwin@
jn@     SHOW    M_JNORM,3
* every pass: a window naming every key pressed, 10 s on the first pass and 3 s on later
* ones, 10 s again after each key. AUTO CAL in the window also runs stage 10 on this pass.
kwin@   lda     >KSTK
        lbne    stage9
        SHOW    M_PRESS,1
        lda     #KWLONG
        tst     >FIRST
        bne     kw0@
        lda     #KWSHORT
kw0@    sta     >KTIME
kw1@    lds     #STACKTOP
        lbsr    kscan
        tsta
        beq     kw2@
        clra
        sta     >OUT1
        ldb     >KIDX
        cmpb    #M_KACAL-M_KEY0
        bne     kw4@
        lda     #1
        sta     >ADEM
kw4@    addb    #M_KEY0
        SHOWB   3
        lda     #KWLONG                 ; restart the window after each key
        sta     >KTIME
        bra     kw1@
kw2@    clra                            ; ~50 ms: two waits of <= 25 ms, kicked between
        sta     >OUT1
        KICK
        ldy     #6250
kw3@    leay    -1,y
        bne     kw3@
        KICK
        ldy     #6100                   ; less the key scan (~800 cycles) before the next kick
kw5@    leay    -1,y
        bne     kw5@
        dec     >KTIME
        bne     kw1@
* ---- stage 9: display walk (first pass only; checked by eye) --------------------------
stage9  STG     9
        lda     >FIRST
        lbeq    stage10
        SHOW    M_DISPW,3
        clr     >RAWMODE                ; everything on
        ldd     #$0FFF
        std     >ANNW
        RAWREF
        WAIT    8
        lda     #1                      ; each position in turn, left to right
        sta     >RAWMODE
        ldd     #0
        std     >ANNW
        ldd     #0                      ; position 0 = leftmost (sent last)
        std     >RAWPOS
dp1@    RAWREF
        WAIT    2
        ldd     >RAWPOS
        addd    #1
        std     >RAWPOS
        cmpd    #12
        bne     dp1@
        ldd     #12                     ; each annunciator, left (SMPL, bit 11) to right
        std     >RAWPOS                 ; (position 12: none of the characters lit)
        ldd     #$0800
        std     >ANNW
da1@    RAWREF
        WAIT    2
        ldd     >ANNW
        lsra
        rorb
        std     >ANNW
        bne     da1@
* ---- stage 10: the HP self-test's analog steps (STEST D4AF; docs/DIAG_ROM.md) ----------
* First pass, every 10th (the counter still shows ...9 here) and after AUTO CAL in the key
* window: the relays and the internal reference switch at each run, as at every TEST.
stage10 STG     10
        lda     >FIRST
        bne     a10go@
        lda     >ADEM
        bne     a10go@
        lda     >RAMMSG+11
        cmpa    #'9'
        lbne    passend
a10go@  SHOW    M_ADST,3
        clr     >AFAIL
        clr     >AFAIL+1
        lda     #M_ADST
        sta     >CURSTEP
* the ACAL constants, read only (ROM w_90C3 / w_90FB): invalid ones are replaced by $0200
* as the HP ROM does, which then reports ACAL REQ'D (ERR 1024)
        clr     >AFLG
        ldx     #ACALT
        clra
ak1@    adda    ,x+
        cmpx    #ACALCK
        bne     ak1@
        coma
        cmpa    >ACALCK
        bne     ak2@
        inc     >AFLG                   ; b0
ak2@    ldd     >ACALPP
        coma
        comb
        cmpd    >ACALPP+2
        bne     ak3@
        lda     >AFLG
        ora     #2
        sta     >AFLG
ak3@    lda     >AFLG
        cmpa    #3
        beq     ak4@
        inc     >NBAD                   ; cal data, not hardware: "CAL ERRORS" at the end
        SHOW    M_ACALRQ,5
* reset the 8051 (its power-on state), leave echo mode with 00 -> 0F
ak4@    lds     #STACKTOP
        lbsr    adreset
        tsta
        bne     askip@
        lbsr    txzero
        lbsr    rxshort
        bcs     askip@
        cmpa    #$0F
        bne     askip@
        leay    stest,pcr
        sty     >SPTR
        lbra    interp
askip@  inc     >NWARN
        SHOW    M_ADSKIP,5
        lbra    a10fin

* the step-table interpreter (opcodes OP*, table `stest`). Inline code, not a subroutine:
* SHOW reloads S, so S is set again before every LBSR.
interp  KICK
        lds     #STACKTOP
        ldy     >SPTR
        lda     ,y+
        sty     >SPTR
        cmpa    #OPSEND
        beq     isend@
        cmpa    #OPPP
        beq     ipp@
        cmpa    #OPAC
        lbeq    iac@
        cmpa    #OPMODE
        lbeq    imode@
        cmpa    #OPACK
        lbeq    iack@
        cmpa    #OPRD
        lbeq    ird@
        cmpa    #OPQ
        lbeq    iq@
        cmpa    #OPCHK
        lbeq    ichk@
        cmpa    #OPWAIT
        lbeq    iwait@
        cmpa    #OPSTEP
        lbeq    istep@
        lbra    a10end                  ; OPEND
* OPSEND n, bytes: part of a frame answered by ACK
isend@  ldb     ,y+
        stb     >ATMP
is1@    ldb     ,y+
        lbsr    txb
        dec     >ATMP
        bne     is1@
        sty     >SPTR
        lbra    interp
* OPPP: the ohms precharge constant (2 bytes)
ipp@    lda     >AFLG
        bita    #2
        beq     ipp1@
        ldd     >ACALPP
        bra     ipp2@
ipp1@   ldd     #$0200
ipp2@   lbsr    txd
        lbra    interp
* OPAC i: AC constant i (word at $050C + 2i)
iac@    ldb     ,y+
        sty     >SPTR
        lda     >AFLG
        bita    #1
        beq     iac1@
        ldx     #ACALT
        aslb
        abx
        ldd     ,x
        bra     iac2@
iac1@   ldd     #$0200
iac2@   lbsr    txd
        lbra    interp
* OPMODE m: the A/D mode frame (ROM w_CCEB): (m*2 + (USLINE = 0)) * 16 + 5. STEST sets
* USLINE first (D4CD), so bit 4 is always 0 here, at 50 Hz as at 60 Hz.
imode@  lda     ,y+
        sty     >SPTR
        lsla
        lsla
        lsla
        lsla
        lsla
        ora     #5
        tfr     a,b
        lbsr    txb
        lbra    interp
* OPACK: the frame is complete; wait for the 8051's 0F
iack@   lda     #2
        sta     >STATH
        lbsr    rxshort
        bcs     inack@
        cmpa    #$0F
        bne     inack@
        clr     >STATH
        lbra    interp
inack@  ldb     #M_ADNACK
        lbra    aerr
* OPRD nn, nbytes: start a reading with 77 nn (ROM w_D345), wait for 1x, then 7F and the
* 2 or 4 data bytes (big-endian, sign-extended into AVAL)
ird@    ldb     #$77
        lbsr    txb
        ldb     ,y+
        lbsr    txb
        lda     #1
        sta     >STATH
        ldb     ,y+
        stb     >ANB
        sty     >SPTR
        lbsr    rxlong
        bcs     inrdg@
        anda    #$F0
        cmpa    #$10
        bne     inrdg@
        clr     >STATH
        ldb     #$7F
        lbsr    txb
        clr     >AVAL
        clr     >AVAL+1
        lda     >ANB
        cmpa    #4
        bne     ird2@
        lbsr    rxshort
        bcs     inrdg@
        sta     >AVAL
        lbsr    rxshort
        bcs     inrdg@
        sta     >AVAL+1
ird2@   lbsr    rxshort
        bcs     inrdg@
        sta     >AVAL+2
        lbsr    rxshort
        bcs     inrdg@
        sta     >AVAL+3
        lda     >ANB
        cmpa    #4
        lbeq    interp
        lbsr    sext
        lbra    interp
inrdg@  clr     >STATH
        ldb     #M_ADNRDG
        lbra    aerr
* OPQ cmd: a 2-byte answer (ROM w_D18D / B085): first byte, then 7F, then the second
iq@     ldb     ,y+
        sty     >SPTR
        lbsr    txb
        lda     #6
        sta     >STATH
        lbsr    rxlong
        bcs     inrdg@
        sta     >AVAL+2
        clr     >STATH
        ldb     #$7F
        lbsr    txb
        lbsr    rxshort
        bcs     inrdg@
        sta     >AVAL+3
        lbsr    sext
        lbra    interp
* OPCHK lo(4), hi(4), bit(2), msg: signed 32-bit lo <= AVAL <= hi (ROM w_D31D, RANGE_D)
ichk@   ldd     >AVAL
        cmpd    ,y
        blt     ifail@
        bgt     ic2@
        ldd     >AVAL+2
        cmpd    2,y
        blo     ifail@
ic2@    ldd     >AVAL
        cmpd    4,y
        bgt     ifail@
        blt     ipass@
        ldd     >AVAL+2
        cmpd    6,y
        bhi     ifail@
ipass@  leay    11,y
        sty     >SPTR
        lbra    interp
ifail@  ldd     >AFAIL
        ora     8,y
        orb     9,y
        std     >AFAIL
        ldb     10,y
        leay    11,y
        sty     >SPTR
        inc     >NWARN
        SHOWB   5
        lbra    interp
* OPWAIT n: n x 0.2 s (settling, ROM WAIT_TIME)
iwait@  lda     ,y+
        sty     >SPTR
        sta     >ATMP
iw1@    WAIT    1
        dec     >ATMP
        bne     iw1@
        lbra    interp
* OPSTEP msg: the step now running (named after a link error)
istep@  lda     ,y+
        sty     >SPTR
        sta     >CURSTEP
        lbra    interp

* a link error (B = message): show it and the step, then reset the 8051
aerr    stb     >ATMP
        inc     >NWARN
        ldb     >ATMP
        SHOWB   5
        ldb     >CURSTEP
        SHOWB   5
        lbra    a10fin
* the table ran to its end: "A/D TEST OK" or "AUXERR nnnnn"
a10end  ldd     >AFAIL
        bne     ae1@
        SHOW    M_ADTOK,3
        lbra    a10fin
ae1@    std     >NUM16
        TORAM2  t_aux
        DEC5
        ldd     >DIG
        std     >RAM2+7
        ldd     >DIG+2
        std     >RAM2+9
        lda     >DIG+4
        sta     >RAM2+11
        ldx     #RAM2+7                 ; leading zeros blank: "AUXERR   256"
ae2@    lda     ,x
        cmpa    #'0'
        bne     ae3@
        lda     #' '
        sta     ,x+
        cmpx    #RAM2+11
        bne     ae2@
ae3@    lda     #ERRF
        sta     >RAM2+12
        SHOW    RAM2IDX,5
* leave the 8051 as at power-on (reference, test current and internal channel off)
a10fin  clr     >STATH
        lds     #STACKTOP
        lbsr    adreset
* ---- pass counter in the RAM buffer ("PASS    0001") ----------------------------------
passend clr     >FIRST
        ldd     >RAMMSG
        cmpd    #'P'*256+'A'
        bne     init@
        ldd     >RAMMSG+2
        cmpd    #'S'*256+'S'
        beq     have@
init@   leay    t_pass,pcr
        ldu     #RAMMSG
        ldb     #12
cp@     lda     ,y+
        sta     ,u+
        decb
        bne     cp@
        clr     >RAMMSG-1
        clr     >RAMMSG+12
have@   ldu     #RAMMSG+11
inc@    lda     ,u
        inca
        cmpa    #'9'+1
        blo     st@
        lda     #'0'
        sta     ,u
        leau    -1,u
        cmpu    #RAMMSG+7
        bne     inc@
        bra     shw@
st@     sta     ,u
shw@    lda     >NWARN
        beq     nowarn@
        SHOW    M_ERRS,5
        bra     cnt@
nowarn@ lda     >NBAD
        beq     allok@
        SHOW    M_CALERRS,5
        bra     cnt@
allok@  SHOW    M_PASSED,5
cnt@    SHOW    RAMIDX,5
        lbra    loop

* =================================================================================
* failures

* halt on message B (index), forever
halt    tfr     b,dp                    ; (no stack here: S may be a link register)
        ldd     #0
        std     >RUNSIG
h@      REFRESH
        WAIT    1
        bra     h@

* address line: S = mask of the failing line -> "RAM Ak BAD"
addrfail
        tfr     s,d
        ldu     #0
c@      leau    1,u
        lsra
        rorb
        bcc     c@
        tfr     u,d
        addb    #M_RAMA0-1
        lbra    halt

* cell X failed (A = its original value): put it back, report the address
cbad    sta     ,x
        cmpx    #RAMMSG+16              ; inside the scratch buffer: fixed message
        bhs     fmt@
        ldb     #M_RAMPAD
        lbra    halt
fmt@    leay    t_cell,pcr              ; "RAM ???? BAD" with the address in hex
        ldu     #RAMMSG
        ldb     #12
cp@     lda     ,y+
        sta     ,u+
        decb
        bne     cp@
        clr     >RAMMSG-1
        lda     #ERRF
        sta     >RAMMSG+12
        tfr     x,d
        tfr     a,b
        lsrb
        lsrb
        lsrb
        lsrb
        HEXB
        stb     >RAMMSG+4
        tfr     x,d
        tfr     a,b
        andb    #$0F
        HEXB
        stb     >RAMMSG+5
        tfr     x,d
        lsrb
        lsrb
        lsrb
        lsrb
        HEXB
        stb     >RAMMSG+6
        tfr     x,d
        andb    #$0F
        HEXB
        stb     >RAMMSG+7
        ldb     #RAMIDX
        lbra    halt

* =================================================================================
* stage 10 link helpers (LBSR; stack in RAM; no SHOW inside)
* send B / send D (A first) / send 00. Uses A.
txb     pshs    b
        TBRE
        puls    b
        stb     >UART_TX
        rts
txd     pshs    b
        tfr     a,b
        bsr     txb
        puls    b
        bra     txb
txzero  clrb
        bra     txb
* receive a byte: rxshort waits up to ~0.5 s, rxlong up to ~2 s (a conversion).
* C=1: timeout. C=0: A = byte. Uses X, B.
rxshort lda     #40
        bra     rxwait
rxlong  lda     #170
rxwait  sta     >ATRY
rx1@    KICK
        RXW     1500                    ; 12 ms
        bcc     rx2@
        dec     >ATRY
        bne     rx1@
        orcc    #1
rx2@    rts
* sign-extend the 16-bit AVAL+2 into AVAL
sext    clra
        tst     >AVAL+2
        bpl     sx1@
        deca
sx1@    sta     >AVAL
        sta     >AVAL+1
        rts

* =================================================================================
* reset the 8051 and read its self-test byte (called with LBSR; stack in RAM).
* Holds TX low with 2000 back-to-back zero bytes (~117 ms; its RESET follows RXD through
* an RC of ~10 ms), draining anything received, then waits up to 1 s.
* Returns A: 0 = FF (self-test OK), 1 = 02 (self-test failed), 2 = nothing, 3 = SIN held
* low (breaks), 4 = other byte (in OTHER), 5 = garbled reply (parity/framing error).
adreset KICK
        ldy     #2000
z1@     tfr     y,d                     ; kick every 256 bytes (15 ms)
        tstb
        bne     z0@
        KICK
z0@     lda     >UART_ST
        bpl     z2@
        ldb     >UART_RX
        tst     >UART_CL
z2@     bita    #$40
        beq     z1@
        clrb
        stb     >UART_TX
        leay    -1,y
        bne     z1@
        clr     >BRK
        clr     >NOTH
        lda     #80
        sta     >TRIES
s1@     KICK
        RXW     1500                    ; 12 ms (x 80 = about 1 s)
        bcc     s2@
        dec     >TRIES
        bne     s1@
        lda     >BRK
        cmpa    #5
        bhs     slow@
        lda     >NOTH
        bne     soth@
        lda     #2
        rts
s2@     bitb    #$10                    ; framing error
        beq     s3@
        tsta
        bne     sgarb@
        inc     >BRK                    ; 00 + framing error: a break, SIN is low
        lda     >BRK
        cmpa    #20
        blo     s1@
slow@   lda     #3
        rts
s3@     bitb    #$08                    ; parity error
        bne     sgarb@
        cmpa    #$FF
        bne     s4@
        clra
        rts
s4@     cmpa    #$02
        bne     s5@
        lda     #1
        rts
s5@     sta     >OTHER
        inc     >NOTH
        lda     >NOTH
        cmpa    #50
        lblo    s1@
soth@   lda     #4
        rts
sgarb@  lda     #5
        rts

* =================================================================================
* key scan (LBSR; stack and OUT1 free to use): A = 0 if no key, else 1 with KIDX =
* row*8+col of the first key found. Leaves OUT1 = $FF (caller closes the gate).
kscan   clr     >KCOL
ks1@    ldb     >KCOL
        lda     #1
ks2@    tstb
        beq     ks3@
        lsla
        decb
        bra     ks2@
ks3@    coma                            ; column KCOL driven low
        sta     >OUT1
        nop
        nop
        lda     >IN1
        coma
        anda    #$3F
        bne     ks4@
        inc     >KCOL
        lda     >KCOL
        cmpa    #8
        bne     ks1@
        lda     #$FF
        sta     >OUT1
        clra
        rts
ks4@    sta     >KROW                   ; lowest row
        clrb
ks5@    lsra
        bcs     ks6@
        incb
        bra     ks5@
ks6@    lslb                            ; row*8 + col
        lslb
        lslb
        addb    >KCOL
        stb     >KIDX
        lda     #$FF
        sta     >OUT1
        lda     #1
        rts

* =================================================================================
* display refresh of message DP: FC FD B8 0A 1A BC 2A C8, as the HP ROM's $719F.
* Called with REFRESH (link in S). Uses A, B, X, Y, U.
refresh ldx     #OUT2
        DOPEN
        DSELECT
        INSTR   $B8
* register A: low nibble of each character code, character 11 first
        INSTR   $0A
        MSGPTR
nibl@   ldb     ,y
        beq     donel@                  ; 0 = sentinel in front of the text
        andb    #$0F
        CALL    dsend4
        leay    -1,y
        bra     nibl@
donel@  anda    #$FF-INA
        NCLK    8
* register B: bits 4-5 of each code (punctuation bits 2-3: none)
        INSTR   $1A
        MSGPTR
nibh@   ldb     ,y
        beq     doneh@
        andb    #$3F
        lsrb
        lsrb
        lsrb
        lsrb
        CALL    dsend4
        leay    -1,y
        bra     nibh@
doneh@  anda    #$FF-INA
        NCLK    8
* annunciators: the message's flags byte (word bits 0-7), then 4 zero bits (8-11)
        INSTR   $BC
        MSGPTR
        ldb     1,y
        CALL    dsend8
        clrb
        CALL    dsend4
        anda    #$FF-INA
        NCLK    44
* register C: character bit 6 - none here
        INSTR   $2A
        ldy     #12
regc@   clrb
        CALL    dsend4
        leay    -1,y
        bne     regc@
        anda    #$FF-INA
        NCLK    8
        INSTR   $C8
        DCLOSE
        jmp     ,s

* ---------------------------------------------------------------------------------
* raw refresh for the display walk (link in S, like REFRESH): position p shows all
* segments (A=$F, B=$F, C=1) if RAWMODE = 0 or p = RAWPOS, else a blank (code $20).
rawref  ldx     #OUT2
        DOPEN
        DSELECT
        INSTR   $B8
        INSTR   $0A
        RAWNIB  $0F,$00
        INSTR   $1A
        RAWNIB  $0F,$02
        INSTR   $BC
        ldb     >ANNW+1
        CALL    dsend8
        ldb     >ANNW
        CALL    dsend4
        anda    #$FF-INA
        NCLK    44
        INSTR   $2A
        RAWNIB  $01,$00
        INSTR   $C8
        DCLOSE
        jmp     ,s

* ---------------------------------------------------------------------------------
* send the instruction in B (ROM $72B8): 44 idle clocks, SYNC with 2 zero bits, 8 bits on
* ISA LSB first, SYNC off and 2 gap clocks. Uses Y as a counter. Link in U.
isend   NCLK    44
        anda    #$FF-INA-ISA
        ora     #SYNC
        CLK
        CLK
        ldy     #8
ibit@   rorb
        bcc     izero@
        ora     #ISA
        bra     iclk@
izero@  anda    #$FF-ISA
iclk@   CLK
        leay    -1,y
        bne     ibit@
        anda    #PWO+PH1+PH2+INA
        CLK
        CLK
        jmp     ,u

* send B on INA, LSB first (ROM $7343 / $7325), unrolled so that Y is left alone. Link in U.
DBIT    macro
        rorb
        bcc     zero@
        ora     #INA
        bra     clk@
zero@   anda    #$FF-INA
clk@    CLK
        endm

dsend8  DBIT
        DBIT
        DBIT
        DBIT
dsend4  DBIT
        DBIT
        DBIT
        DBIT
        jmp     ,u

* =================================================================================
* messages: 0 sentinel, exactly 12 characters (code = ASCII & $3F), annunciator flags
msgtab
m_hello fcb     0
        fcc     "HP3457A DIAG"
        fcb     0
m_romck fcb     0
        fcc     "ROM CHECKSUM"
        fcb     0
m_romok fcb     0
        fcc     "ROM OK      "
        fcb     0
m_rombad fcb    0
        fcc     "ROM CKSM BAD"
        fcb     ERRF
m_ramdat fcb    0
        fcc     "RAM DATA BUS"
        fcb     0
m_ramadr fcb    0
        fcc     "RAM ADDR BUS"
        fcb     0
m_ramcel fcb    0
        fcc     "RAM CELLS   "
        fcb     0
m_ramok fcb     0
        fcc     "RAM OK      "
        fcb     0
m_passed fcb    0
        fcc     "ALL PASSED  "
        fcb     0
m_rampad fcb    0
        fcc     "RAM PAD BAD "
        fcb     ERRF
m_ramdgen fcb   0
        fcc     "RAM DATA BAD"
        fcb     ERRF
m_ramd0 fcb     0
        fcc     "RAM D0 BAD  "
        fcb     ERRF
        fcb     0
        fcc     "RAM D1 BAD  "
        fcb     ERRF
        fcb     0
        fcc     "RAM D2 BAD  "
        fcb     ERRF
        fcb     0
        fcc     "RAM D3 BAD  "
        fcb     ERRF
        fcb     0
        fcc     "RAM D4 BAD  "
        fcb     ERRF
        fcb     0
        fcc     "RAM D5 BAD  "
        fcb     ERRF
        fcb     0
        fcc     "RAM D6 BAD  "
        fcb     ERRF
        fcb     0
        fcc     "RAM D7 BAD  "
        fcb     ERRF
m_rama0 fcb     0
        fcc     "RAM A0 BAD  "
        fcb     ERRF
        fcb     0
        fcc     "RAM A1 BAD  "
        fcb     ERRF
        fcb     0
        fcc     "RAM A2 BAD  "
        fcb     ERRF
        fcb     0
        fcc     "RAM A3 BAD  "
        fcb     ERRF
        fcb     0
        fcc     "RAM A4 BAD  "
        fcb     ERRF
        fcb     0
        fcc     "RAM A5 BAD  "
        fcb     ERRF
        fcb     0
        fcc     "RAM A6 BAD  "
        fcb     ERRF
        fcb     0
        fcc     "RAM A7 BAD  "
        fcb     ERRF
        fcb     0
        fcc     "RAM A8 BAD  "
        fcb     ERRF
        fcb     0
        fcc     "RAM A9 BAD  "
        fcb     ERRF
        fcb     0
        fcc     "RAM A10 BAD "
        fcb     ERRF
        fcb     0
        fcc     "RAM A11 BAD "
        fcb     ERRF
        fcb     0
        fcc     "RAM A12 BAD "
        fcb     ERRF
m_calck fcb     0
        fcc     "CAL RAM     "
        fcb     0
m_calok fcb     0
        fcc     "CAL RAM OK  "
        fcb     0
m_callost fcb   0
        fcc     "CAL RAM LOST"
        fcb     ERRF
m_calerrs fcb   0
        fcc     "CAL ERRORS  "
        fcb     ERRF
m_calrec0 fcb   0
        fcc     "CAL DCV 30MV"
        fcb     ERRF
        fcb     0
        fcc     "CAL DCV300MV"
        fcb     ERRF
        fcb     0
        fcc     "CAL DCV 3V  "
        fcb     ERRF
        fcb     0
        fcc     "CAL DCV 30V "
        fcb     ERRF
        fcb     0
        fcc     "CAL DCV 300V"
        fcb     ERRF
        fcb     0
        fcc     "CAL ACV 30MV"
        fcb     ERRF
        fcb     0
        fcc     "CAL ACV300MV"
        fcb     ERRF
        fcb     0
        fcc     "CAL ACV 3V  "
        fcb     ERRF
        fcb     0
        fcc     "CAL ACV 30V "
        fcb     ERRF
        fcb     0
        fcc     "CAL ACV 300V"
        fcb     ERRF
        fcb     0
        fcc     "CAL DCI300UA"
        fcb     ERRF
        fcb     0
        fcc     "CAL DCI 3MA "
        fcb     ERRF
        fcb     0
        fcc     "CAL DCI 30MA"
        fcb     ERRF
        fcb     0
        fcc     "CAL DCI300MA"
        fcb     ERRF
        fcb     0
        fcc     "CAL DCI 1A  "
        fcb     ERRF
        fcb     0
        fcc     "CAL ACI 30MA"
        fcb     ERRF
        fcb     0
        fcc     "CAL ACI300MA"
        fcb     ERRF
        fcb     0
        fcc     "CAL ACI 1A  "
        fcb     ERRF
        fcb     0
        fcc     "CAL OHM 30  "
        fcb     ERRF
        fcb     0
        fcc     "CAL OHM 300 "
        fcb     ERRF
        fcb     0
        fcc     "CAL OHM 3K  "
        fcb     ERRF
        fcb     0
        fcc     "CAL OHM 30K "
        fcb     ERRF
        fcb     0
        fcc     "CAL OHM 300K"
        fcb     ERRF
        fcb     0
        fcc     "CAL OHM 3M  "
        fcb     ERRF
        fcb     0
        fcc     "CAL OHM 30M "
        fcb     ERRF
        fcb     0
        fcc     "CAL OHMF 30 "
        fcb     ERRF
        fcb     0
        fcc     "CAL OHMF300 "
        fcb     ERRF
        fcb     0
        fcc     "CAL OHMF 3K "
        fcb     ERRF
        fcb     0
        fcc     "CAL OHMF 30K"
        fcb     ERRF
        fcb     0
        fcc     "CAL OHMF300K"
        fcb     ERRF
        fcb     0
        fcc     "CAL OHMF 3M "
        fcb     ERRF
        fcb     0
        fcc     "CAL OHMF 30M"
        fcb     ERRF
        fcb     0
        fcc     "CAL AC SHARE"
        fcb     ERRF
        fcb     0
        fcc     "CAL GLOBAL K"
        fcb     ERRF
        fcb     0
        fcc     "CAL TRIM    "
        fcb     ERRF
m_timers fcb    0
        fcc     "NMI AND LINE"
        fcb     0
m_nonmi fcb     0
        fcc     "NO NMI TICK "
        fcb     ERRF
m_noline fcb    0
        fcc     "NO LINE SENS"
        fcb     ERRF
m_errs  fcb     0
        fcc     "ERRORS FOUND"
        fcb     ERRF
m_link  fcb     0
        fcc     "LINK TEST   "
        fcb     0
m_nouart fcb    0
        fcc     "NO UART U616"
        fcb     ERRF
m_txstuck fcb   0
        fcc     "UART TX STUK"
        fcb     ERRF
m_adok  fcb     0
        fcc     "A/D RESET OK"
        fcb     0
m_adtest fcb    0
        fcc     "A/D TEST BAD"
        fcb     ERRF
m_silent fcb    0
        fcc     "A/D SILENT  "
        fcb     ERRF
m_chkopto fcb   0
        fcc     "CHK OPTO PWR"
        fcb     ERRF
m_rxlow fcb     0
        fcc     "LINK RX LOW "
        fcb     ERRF
m_echook fcb    0
        fcc     "ECHO OK     "
        fcb     0
m_noecho fcb    0
        fcc     "A/D NO ECHO "
        fcb     ERRF
m_slowrx fcb    0
        fcc     "SLOW EDGE RX"
        fcb     ERRF
m_slowtx fcb    0
        fcc     "SLOW EDGE TX"
        fcb     ERRF
m_noisy fcb     0
        fcc     "LINK NOISY  "
        fcb     ERRF
m_wdog  fcb     0
        fcc     "WATCHDOG    "
        fcb     0
m_wdnorst fcb   0
        fcc     "WDOG NO RST "
        fcb     ERRF
m_wdearly fcb   0
        fcc     "WDOG EARLY  "
        fcb     ERRF
m_restart fcb   0
        fcc     "RESTARTED   "
        fcb     ERRF
m_hpib  fcb     0
        fcc     "HPIB U614   "
        fcb     0
m_hpibok fcb    0
        fcc     "HPIB U614 OK"
        fcb     0
m_hpibno fcb    0
        fcc     "U614 NO RESP"
        fcb     ERRF
m_keys  fcb     0
        fcc     "KEYBOARD    "
        fcb     0
m_kok   fcb     0
        fcc     "NO KEY STUCK"
        fcb     0
m_kstuck fcb    0
        fcc     "KEY STUCK   "
        fcb     ERRF
m_jnorm fcb     0
        fcc     "JUMPER NORM "
        fcb     0
m_jkey  fcb     0
        fcc     "JUMPER KEY  "
        fcb     0
m_jdsa  fcb     0
        fcc     "JUMPER DSA  "
        fcb     0
m_press fcb     0
        fcc     "PRESS KEYS  "
        fcb     0
m_dispw fcb     0
        fcc     "DISPLAY TEST"
        fcb     0
m_key0  fcb     0
        fcc     "KEY ACI     "
        fcb     0
        fcb     0
        fcc     "KEY FREQ    "
        fcb     0
        fcb     0
        fcc     "KEY ACV     "
        fcb     0
        fcb     0
        fcc     "KEY DCV     "
        fcb     0
        fcb     0
        fcc     "KEY R0C4    "
        fcb     0
        fcb     0
        fcc     "KEY R0C5    "
        fcb     0
        fcb     0
        fcc     "KEY OHM     "
        fcb     0
        fcb     0
        fcc     "KEY DCI     "
        fcb     0
        fcb     0
        fcc     "KEY 2       "
        fcb     0
        fcb     0
        fcc     "KEY TERM    "
        fcb     0
        fcb     0
        fcc     "KEY DIGITS  "
        fcb     0
        fcb     0
        fcc     "KEY BACKSPC "
        fcb     0
        fcb     0
        fcc     "KEY 1       "
        fcb     0
        fcb     0
        fcc     "KEY SCANADV "
        fcb     0
        fcb     0
        fcc     "KEY CHAN    "
        fcb     0
        fcb     0
        fcc     "KEY 3       "
        fcb     0
        fcb     0
        fcc     "KEY LOCAL   "
        fcb     0
        fcb     0
        fcc     "KEY SHIFT   "
        fcb     0
        fcb     0
        fcc     "KEY DOWN    "
        fcb     0
        fcb     0
        fcc     "KEY UP      "
        fcb     0
        fcb     0
        fcc     "KEY R2C4    "
        fcb     0
        fcb     0
        fcc     "KEY R2C5    "
        fcb     0
        fcb     0
        fcc     "KEY LEFT    "
        fcb     0
        fcb     0
        fcc     "KEY RIGHT   "
        fcb     0
        fcb     0
        fcc     "KEY 8       "
        fcb     0
        fcb     0
        fcc     "KEY NPLC    "
        fcb     0
        fcb     0
        fcc     "KEY AUTOZERO"
        fcb     0
        fcb     0
        fcc     "KEY E       "
        fcb     0
        fcb     0
        fcc     "KEY 7       "
        fcb     0
        fcb     0
        fcc     "KEY OFFSCOMP"
        fcb     0
m_kacal fcb     0
        fcc     "KEY AUTOCAL "
        fcb     0
        fcb     0
        fcc     "KEY 9       "
        fcb     0
        fcb     0
        fcc     "KEY 5       "
        fcb     0
        fcb     0
        fcc     "KEY AUTO    "
        fcb     0
        fcb     0
        fcc     "KEY NRDGS   "
        fcb     0
        fcb     0
        fcc     "KEY -       "
        fcb     0
        fcb     0
        fcc     "KEY 4       "
        fcb     0
        fcb     0
        fcc     "KEY DELAY   "
        fcb     0
        fcb     0
        fcc     "KEY SINGLE  "
        fcb     0
        fcb     0
        fcc     "KEY 6       "
        fcb     0
        fcb     0
        fcc     "KEY .       "
        fcb     0
        fcb     0
        fcc     "KEY CONFIG  "
        fcb     0
        fcb     0
        fcc     "KEY RCL STAT"
        fcb     0
        fcb     0
        fcc     "KEY ENT     "
        fcb     0
        fcb     0
        fcc     "KEY 0       "
        fcb     0
        fcb     0
        fcc     "KEY STORE   "
        fcb     0
        fcb     0
        fcc     "KEY RCL MEM "
        fcb     0
        fcb     0
        fcc     "KEY ,       "
        fcb     0
m_adst  fcb     0
        fcc     "A/D SELFTEST"
        fcb     0
m_adtok fcb     0
        fcc     "A/D TEST OK "
        fcb     0
m_adskip fcb    0
        fcc     "A/D SKIPPED "
        fcb     ERRF
m_acalrq fcb    0
        fcc     "ACAL REQ'D  "
        fcb     ERRF
m_adnack fcb    0
        fcc     "A/D NO ACK  "
        fcb     ERRF
m_adnrdg fcb    0
        fcc     "A/D NO RDG  "
        fcb     ERRF
* the HP self-test steps, named as in the manual's AUXERR table
m_sint  fcb     0
        fcc     "INTEGRATOR  "
        fcb     ERRF
m_sfez  fcb     0
        fcc     "FRONTEND ZRO"
        fcb     ERRF
m_sref  fcb     0
        fcc     "REF GAIN DIV"
        fcb     ERRF
m_samps fcb     0
        fcc     "AMPS TEST   "
        fcb     ERRF
m_sacos fcb     0
        fcc     "AC AMP DC OS"
        fcb     ERRF
m_sacc  fcb     0
        fcc     "AC CONVERTER"
        fcb     ERRF
m_sopre fcb     0
        fcc     "OHMS PRECHG "
        fcb     ERRF
msgtabend

M_HELLO equ     (m_hello-msgtab)/MSGLEN
M_ROMCK equ     (m_romck-msgtab)/MSGLEN
M_ROMOK equ     (m_romok-msgtab)/MSGLEN
M_ROMBAD equ    (m_rombad-msgtab)/MSGLEN
M_RAMDAT equ    (m_ramdat-msgtab)/MSGLEN
M_RAMADR equ    (m_ramadr-msgtab)/MSGLEN
M_RAMCEL equ    (m_ramcel-msgtab)/MSGLEN
M_RAMOK equ     (m_ramok-msgtab)/MSGLEN
M_PASSED equ    (m_passed-msgtab)/MSGLEN
M_RAMPAD equ    (m_rampad-msgtab)/MSGLEN
M_RAMDGEN equ   (m_ramdgen-msgtab)/MSGLEN
M_RAMD0 equ     (m_ramd0-msgtab)/MSGLEN
M_RAMA0 equ     (m_rama0-msgtab)/MSGLEN
M_CALCK equ     (m_calck-msgtab)/MSGLEN
M_CALOK equ     (m_calok-msgtab)/MSGLEN
M_CALLOST equ   (m_callost-msgtab)/MSGLEN
M_CALERRS equ   (m_calerrs-msgtab)/MSGLEN
M_CALREC0 equ   (m_calrec0-msgtab)/MSGLEN
M_TIMERS equ    (m_timers-msgtab)/MSGLEN
M_NONMI equ     (m_nonmi-msgtab)/MSGLEN
M_NOLINE equ    (m_noline-msgtab)/MSGLEN
M_ERRS  equ     (m_errs-msgtab)/MSGLEN
M_LINK  equ     (m_link-msgtab)/MSGLEN
M_NOUART equ    (m_nouart-msgtab)/MSGLEN
M_TXSTUCK equ   (m_txstuck-msgtab)/MSGLEN
M_ADOK  equ     (m_adok-msgtab)/MSGLEN
M_ADTEST equ    (m_adtest-msgtab)/MSGLEN
M_SILENT equ    (m_silent-msgtab)/MSGLEN
M_CHKOPTO equ   (m_chkopto-msgtab)/MSGLEN
M_RXLOW equ     (m_rxlow-msgtab)/MSGLEN
M_ECHOOK equ    (m_echook-msgtab)/MSGLEN
M_NOECHO equ    (m_noecho-msgtab)/MSGLEN
M_SLOWRX equ    (m_slowrx-msgtab)/MSGLEN
M_SLOWTX equ    (m_slowtx-msgtab)/MSGLEN
M_NOISY equ     (m_noisy-msgtab)/MSGLEN
M_WDOG  equ     (m_wdog-msgtab)/MSGLEN
M_WDNORST equ   (m_wdnorst-msgtab)/MSGLEN
M_WDEARLY equ   (m_wdearly-msgtab)/MSGLEN
M_RESTART equ   (m_restart-msgtab)/MSGLEN
M_HPIB  equ     (m_hpib-msgtab)/MSGLEN
M_HPIBOK equ    (m_hpibok-msgtab)/MSGLEN
M_HPIBNO equ    (m_hpibno-msgtab)/MSGLEN
M_KEYS  equ     (m_keys-msgtab)/MSGLEN
M_KOK   equ     (m_kok-msgtab)/MSGLEN
M_KSTUCK equ    (m_kstuck-msgtab)/MSGLEN
M_JNORM equ     (m_jnorm-msgtab)/MSGLEN
M_JKEY  equ     (m_jkey-msgtab)/MSGLEN
M_JDSA  equ     (m_jdsa-msgtab)/MSGLEN
M_PRESS equ     (m_press-msgtab)/MSGLEN
M_DISPW equ     (m_dispw-msgtab)/MSGLEN
M_KEY0  equ     (m_key0-msgtab)/MSGLEN
M_KACAL equ     (m_kacal-msgtab)/MSGLEN
M_ADST  equ     (m_adst-msgtab)/MSGLEN
M_ADTOK equ     (m_adtok-msgtab)/MSGLEN
M_ADSKIP equ    (m_adskip-msgtab)/MSGLEN
M_ACALRQ equ    (m_acalrq-msgtab)/MSGLEN
M_ADNACK equ    (m_adnack-msgtab)/MSGLEN
M_ADNRDG equ    (m_adnrdg-msgtab)/MSGLEN
M_SINT  equ     (m_sint-msgtab)/MSGLEN
M_SFEZ  equ     (m_sfez-msgtab)/MSGLEN
M_SREF  equ     (m_sref-msgtab)/MSGLEN
M_SAMPS equ     (m_samps-msgtab)/MSGLEN
M_SACOS equ     (m_sacos-msgtab)/MSGLEN
M_SACC  equ     (m_sacc-msgtab)/MSGLEN
M_SOPRE equ     (m_sopre-msgtab)/MSGLEN

* ---- stage 10 step table: the frames of the HP self-test (STEST D4AF) ------------------------
* Recorded from the HP ROM in emulation and decoded (docs/DIAG_ROM.md, stage 10). The DC
* set-up frame is: function*8+1, (range+7)*16+3, $0200, precharge, scale(4), offset(4),
* delay(4); the AC one carries two AC constants instead of $0200 and the precharge.
OPEND   equ     0
OPSEND  equ     1                       ; n, bytes
OPPP    equ     2                       ; ohms precharge word ($0517, or $0200)
OPAC    equ     3                       ; i: AC word $050C+2i (or $0200)
OPMODE  equ     4                       ; m: mode frame
OPACK   equ     5                       ; wait for 0F
OPRD    equ     6                       ; nn, nbytes: 77 nn, reading
OPQ     equ     7                       ; cmd: 2-byte answer
OPCHK   equ     8                       ; lo(4), hi(4), bit(2), msg
OPWAIT  equ     9                       ; n x 0.2 s
OPSTEP  equ     10                      ; msg
* DC set-up frame for function \1, range frame \2, scale \3:\4, delay \5
DCSET   macro
        fcb     OPSEND,4,\1,\2,$02,$00,OPPP
        fcb     OPSEND,12
        fdb     \3,\4,0,0,0,\5
        fcb     OPACK
        endm
* AC set-up frame: function \1, range frame \2, AC words \3 and \4, scale \5:\6, delay \7
ACSET   macro
        fcb     OPSEND,2,\1,\2,OPAC,\3,OPAC,\4
        fcb     OPSEND,12
        fdb     \5,\6,0,0,$000F,\7
        fcb     OPACK
        endm
* check: lo hi:lo lo hi hi:lo hi, AUXERR bit, step message
CHK     macro
        fcb     OPCHK
        fdb     \1,\2,\3,\4,\5
        fcb     \6
        endm
stest   fcb     OPSEND,2,$1B,$FE,OPACK  ; internal channel (w_CCCF)
* integrator convergence (D361): DCV, range 0, mode 0, 77 30: -461..51 counts (AUXERR 8)
        fcb     OPSTEP,M_SINT,OPMODE,0,OPACK
        DCSET   $01,$73,$0034,$410E,$0082
        fcb     OPRD,$30,2
        CHK     $FFFF,$FE33,$0000,$0033,8,M_SINT
* front-end zero (D385): DCV 30 mV, mode 3, 77 40: +-150000 (16)
        fcb     OPSTEP,M_SFEZ,OPMODE,3,OPACK
        DCSET   $01,$53,$1F5B,$5817,$01DA
        fcb     OPRD,$40,4
        CHK     $FFFD,$B610,$0002,$49F0,16,M_SFEZ
* current source / gain / divider (D3CB): 57 (1 V reference), DCV ranges 0-2, 77 50:
* 85..115 % of 1 V (tables D3AF/D3BF) (32)
        fcb     OPSTEP,M_SREF,OPSEND,1,$57,OPACK
        fcb     OPMODE,3,OPACK
        DCSET   $01,$73,$1F5B,$5817,$0108
        fcb     OPRD,$50,4
        CHK     $000C,$F850,$0011,$8C30,32,M_SREF
        fcb     OPMODE,3,OPACK
        DCSET   $01,$83,$1F5B,$5817,$0108
        fcb     OPRD,$50,4
        CHK     $0001,$4C08,$0001,$C138,32,M_SREF
        fcb     OPMODE,3,OPACK
        DCSET   $01,$93,$1F5B,$5817,$0108
        fcb     OPRD,$50,4
        CHK     $0000,$2134,$0000,$2CEC,32,M_SREF
* amps (D3FB): 47 (test current), DCI, mode 3, 77 60: 500..1500 (64)
        fcb     OPSTEP,M_SAMPS,OPSEND,1,$47,OPACK
        fcb     OPMODE,3,OPACK
        DCSET   $41,$73,$1F5B,$5817,$0108
        fcb     OPRD,$60,4
        CHK     $0000,500,$0000,1500,64,M_SAMPS
* AC amp DC offset (D431): internal again, ACV ranges 2 and 0, mode 2, settle, 2F: 40..983 (128)
        fcb     OPSTEP,M_SACOS,OPSEND,2,$1B,$FE,OPACK
        fcb     OPMODE,2,OPACK
        ACSET   $39,$93,1,3,$22DA,$FC11,$41EA
        fcb     OPWAIT,1,OPQ,$2F
        CHK     $0000,40,$0000,983,128,M_SACOS
        fcb     OPMODE,2,OPACK
        ACSET   $39,$73,1,4,$22DA,$FC11,$41EA
        fcb     OPWAIT,1,OPQ,$2F
        CHK     $0000,40,$0000,983,128,M_SACOS
* AC converter (D483): ACV ranges -1 and 0, mode 0, 1F: 50..973 (256)
        fcb     OPSTEP,M_SACC,OPMODE,0,OPACK
        ACSET   $29,$63,0,3,$003A,$0F64,$41FA
        fcb     OPQ,$1F
        CHK     $0000,50,$0000,973,256,M_SACC
        fcb     OPMODE,0,OPACK
        ACSET   $29,$73,1,4,$003A,$0F64,$41FA
        fcb     OPQ,$1F
        CHK     $0000,50,$0000,973,256,M_SACC
* ohms precharge (D45F): DCV range 2, mode 3, 3F: >= 0 (512)
        fcb     OPSTEP,M_SOPRE,OPMODE,3,OPACK
        DCSET   $01,$93,$1F5B,$5817,$0108
        fcb     OPQ,$3F
        CHK     $0000,0,$7FFF,$FFFF,512,M_SOPRE
        fcb     OPEND

* cal items checked in stage 3 (docs/CALIBRATION.md): address, size/length, type, item
caltab
        CALREC  $0040,11,0,0
        CALREC  $004B,11,0,1
        CALREC  $0056,11,0,2
        CALREC  $0061,11,0,3
        CALREC  $006C,11,0,4
        CALREC  $007C,5,0,5
        CALREC  $0081,5,0,6
        CALREC  $0086,5,0,7
        CALREC  $008B,5,0,8
        CALREC  $0090,5,0,9
        CALREC  $0095,11,0,10
        CALREC  $00A0,11,0,11
        CALREC  $00AB,11,0,12
        CALREC  $00B6,11,0,13
        CALREC  $00C1,11,0,14
        CALREC  $00CC,5,0,15
        CALREC  $00D1,5,0,16
        CALREC  $00D6,5,0,17
        CALREC  $00DB,11,0,18
        CALREC  $00E6,11,0,19
        CALREC  $00F1,11,0,20
        CALREC  $00FC,11,0,21
        CALREC  $0107,11,0,22
        CALREC  $0112,11,0,23
        CALREC  $011D,11,0,24
        CALREC  $0133,11,0,25
        CALREC  $013E,11,0,26
        CALREC  $0149,11,0,27
        CALREC  $0154,11,0,28
        CALREC  $015F,11,0,29
        CALREC  $016A,11,0,30
        CALREC  $0175,11,0,31
        CALREC  $0077,5,0,32
        CALREC  $018B,4,1,33
        CALREC  $0193,1,1,34
        fdb     0
NCAL    equ     35

* templates for the RAM buffers (12 characters, no sentinel/flags)
t_cell  fcc     "RAM ???? BAD"
t_pass  fcc     "PASS    0000"
t_calbad fcc    "CAL BAD   00"
t_nmi   fcc     "NMI   .   MS"
t_line  fcc     "LINE   .  HZ"
t_adsent fcc    "A/D SENT ?? "
t_echoerr fcc   "ECHO ERR 000"
t_echobit fcc   "ECHO BIT 0  "
t_wdog  fcc     "WDOG 0000MS "
t_stage fcc     "IN STAGE 0  "
t_u614  fcc     "U614 D0 BAD "
t_rowsh fcc     "ROW 0 SHORT "
t_aux   fcc     "AUXERR 00000"
* 8291A SPM/SPS patterns (no bit 6), ending in 00
gpats   fcb     $01,$02,$04,$08,$10,$20,$80,$AA,$15,$BF,$00
* 8291A Address 0 patterns (ARS = 0), ending with DT0 = DL0 = 1 (off the bus)
gapats  fcb     $40,$15,$0A,$55,$2A,$60

* NMI (stage 4 only; the gate is closed otherwise): count ticks. 19 + 7 + 3 + 15 = 44 cycles
nmih    inc     >NMICNT+1
        bne     n@
        inc     >NMICNT
n@      rti

* =================================================================================
* vectors. $FFF0 (6809 reserved) is left free for the checksum word (tools/asm.py).
        org     $FFF2
        fdb     reset                   ; SWI3
        fdb     reset                   ; SWI2
        fdb     reset                   ; FIRQ
        fdb     reset                   ; IRQ
        fdb     reset                   ; SWI
        fdb     nmih                    ; NMI
        fdb     reset                   ; RESET
