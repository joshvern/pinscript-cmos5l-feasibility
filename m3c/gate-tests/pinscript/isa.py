"""Numeric constants for the PinScript ISA candidate v0.1 (docs/isa.md).

Constants only. The assembler (asm.py) encodes and the cycle model
(cycle_model.py) decodes independently from the written specification;
hand-written golden vectors in tools/tests check both against the spec.
"""

from enum import IntEnum

ISA_VERSION = "0.1"

WORD_BITS = 16
WORD_MASK = 0xFFFF
SUPPORTED_DEPTHS = (32, 64)
DEFAULT_DEPTH = 64

PIN_COUNT = 8
REGISTER_BITS = 8
SR_BITS = 8
TIMER_BITS = 16
TARGET_BITS = 6
IMM8_MAX = 0xFF
IMM12_MAX = 0xFFF
LDTH_MAX = 0xF
FAULT_CODE_MAX = 7
FIFO_DEPTH = 4
FIFO_WIDTH = 8
ELAPSED_BITS = 24
ELAPSED_MAX = (1 << ELAPSED_BITS) - 1
SYNC_STAGES = 2


def pc_bits(depth: int) -> int:
    """Width of PC: clog2(depth + 1), so PC can hold `depth` itself."""
    if depth not in SUPPORTED_DEPTHS:
        raise ValueError(f"supported program depths are {SUPPORTED_DEPTHS}")
    return depth.bit_length()


class Opcode(IntEnum):
    """word[15:12]. Values 0x9-0xF are reserved (illegal)."""
    SYS = 0x0
    PIN = 0x1
    SHIFT = 0x2
    LDMOV = 0x3
    JMP = 0x4
    DJNZ = 0x5
    DELAY = 0x6
    LDT = 0x7
    WAIT = 0x8


class SysOp(IntEnum):
    """word[11:8] when the opcode is SYS. Values 7-15 are reserved."""
    ILLEGAL = 0x0
    NOP = 0x1
    HALT = 0x2
    FAULT = 0x3
    PULL = 0x4
    PUSH = 0x5
    LDTH = 0x6


class PinOp(IntEnum):
    """word[11:9] when the opcode is PIN. Value 7 is reserved."""
    SET = 0
    CLR = 1
    TGL = 2
    DRIVE = 3
    RELEASE = 4
    OPENDRAIN = 5
    PUSHPULL = 6


class Reg(IntEnum):
    """LD/MOV destination word[11:10] and source word[9:8]."""
    R0 = 0
    R1 = 1
    SR = 2


SRC_IMMEDIATE = 3  # LD/MOV source code selecting word[7:0]; destination 3 is reserved


class Cond(IntEnum):
    """Five-bit condition shared by JMP and WAIT (word[11:7]); 0b01010-0b01111 reserved."""
    ALWAYS = 0b00000
    NEVER = 0b00001
    TXE = 0b00010
    TXNE = 0b00011
    RXF = 0b00100
    RXNF = 0b00101
    SR0_LO = 0b00110
    SR0_HI = 0b00111
    SR7_LO = 0b01000
    SR7_HI = 0b01001


COND_PIN_LO = 0b10000  # | pin: SYNC2[pin] == 0
COND_PIN_HI = 0b11000  # | pin: SYNC2[pin] == 1


class Reason(IntEnum):
    """Stop-record reason; FAULT n records USER_0 + n."""
    NONE = 0
    HALT = 1
    HOST_STOP = 2
    ILLEGAL_INSTRUCTION = 3
    PC_RANGE = 4
    WAIT_TIMEOUT = 5
    TX_UNDERFLOW = 6
    RX_OVERFLOW = 7
    USER_0 = 8
    USER_1 = 9
    USER_2 = 10
    USER_3 = 11
    USER_4 = 12
    USER_5 = 13
    USER_6 = 14
    USER_7 = 15


class RunState(IntEnum):
    IDLE = 0
    RUN = 1
    STOPPED = 2


class ProposedHostError(IntEnum):
    """Host-command results modeled for the M3 proposal (isa.md section 12).

    BUSY reuses the implemented M1 code; 13-15 are proposed, not implemented.
    """
    NONE = 0
    BUSY = 9
    NO_PROGRAM = 13
    TX_FULL = 14
    RX_EMPTY = 15
