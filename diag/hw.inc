* HP 3457A main controller (03457-66511) hardware, as used by the diagnostic ROM.
* Sources: docs/FINDINGS.md section 2, docs/DISPLAY.md. Addresses are I/O at $0000-$003F.

UART_RX         equ     $0000           ; link from the 8051 (receive data)
UART_TX         equ     $0008           ; link to the 8051 (transmit data)
UART_ST         equ     $0010           ; status
UART_CL         equ     $0018           ; read: clear status (verify)
GPIB            equ     $0020           ; Intel 8291A, registers $20-$27
CXTRIG          equ     $0028           ; external trigger FIRQ acknowledge
* $0030-$0037 CALREQ: NEVER ACCESS (even a read arms the cal-RAM write window)
IN1             equ     $0038           ; b0-5 key rows, b6 security jumper, b7 line sense
OUT1            equ     $003A           ; key columns (active low); b0 = MASK* (NMI gate)
OUT2            equ     $003C           ; display bus, VMCMPLT, OTL* (watchdog)

* OUT2 bits
PWO             equ     $01             ; high during a display transaction
PH1             equ     $02
PH2             equ     $04             ; the LCD samples on PH2
INA             equ     $08             ; data
SYNC            equ     $10             ; instruction phase
ISA             equ     $20             ; instruction bits
VMC             equ     $40             ; voltmeter complete output
OTL             equ     $80             ; watchdog kick (pulse)

RAM_LO          equ     $0040
CAL_LO          equ     $0040           ; write-protected cal region $0040-$01FF: read only
CAL_HI          equ     $01FF
RAM_HI          equ     $1FFF
