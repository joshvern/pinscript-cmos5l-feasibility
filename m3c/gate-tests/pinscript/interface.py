"""Configuration register map; see docs/host-interface.md.

`Register`, `Control` and `Error` are the unchanged interface-version-1 (M1)
contract. Interface version 2 (M3C, ADR 006) keeps every version-1 value and
adds `EngineRegister`, `EngineControl` and `EngineError`; in version 2 START
is a real command. Host tools must read VERSION and call
`require_interface_version` before relying on either map.
"""

from enum import IntEnum


class Command(IntEnum):
    READ = 0x01
    WRITE = 0x02


class Register(IntEnum):
    DEVICE_ID = 0x00
    VERSION = 0x01
    STATUS = 0x02
    SCRATCH = 0x03
    CONTROL = 0x04
    ERROR = 0x05
    EXPECTED_LENGTH = 0x06
    LOADED_COUNT = 0x07
    APPEND_DATA = 0x08
    READ_ADDRESS = 0x09
    READ_DATA = 0x0A
    PROGRAM_DEPTH = 0x0B


class Control(IntEnum):
    BEGIN_LOAD = 1
    COMMIT = 2
    ABORT = 3
    CLEAR_ERROR = 4
    START = 5


INTERFACE_V1 = 0x0001    # M1 configuration/loader prototype; START unsupported
INTERFACE_V2 = 0x0002    # M3C engine: real START/STOP, FIFOs, diagnostics
LAST_REGISTER = {INTERFACE_V1: 0x0B, INTERFACE_V2: 0x15}


class EngineRegister(IntEnum):
    """Interface version 2 additions (read-only unless noted)."""
    TX_DATA = 0x0C          # WO: push [7:0] into the TX FIFO
    RX_DATA = 0x0D          # RO: non-destructive peek {valid at [8], head at [7:0]}
    ENGINE_STATE = 0x0E     # {run state[15:14], G[13], REASON[11:8], PC[6:0]}
    PIN_INTENT = 0x0F       # {OUT, OE}
    OD_DIAG = 0x10          # {OD, DIAG_IN}
    WORK_REGISTERS = 0x11   # {R0, R1}
    SR_ELAPSED_HIGH = 0x12  # {SR, ELAPSED[23:16]}
    ELAPSED_LOW = 0x13      # ELAPSED[15:0]
    TIMER = 0x14            # T
    FIFO_STATUS = 0x15      # {TX occupancy at [10:8], RX occupancy at [2:0]}


class EngineControl(IntEnum):
    """Interface version 2 CONTROL additions (START keeps value 5)."""
    STOP = 6
    RX_POP = 7
    FIFO_CLEAR = 8


class EngineError(IntEnum):
    """Interface version 2 error additions (the last free 4-bit codes)."""
    NO_PROGRAM = 13
    TX_FULL = 14
    RX_EMPTY = 15


class Error(IntEnum):
    NONE = 0
    BAD_COMMAND = 1
    BAD_ADDRESS = 2
    READ_ONLY = 3
    INVALID_LENGTH = 4
    LOAD_STATE = 5
    LENGTH_LOCKED = 6
    INCOMPLETE = 7
    OVERRUN = 8
    BUSY = 9
    UNSUPPORTED = 10
    TRUNCATED = 11
    BAD_VALUE = 12


def _unsigned(value: int, width: int, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value < (1 << width):
        raise ValueError(f"{name} must be an unsigned {width}-bit integer")
    return value


def encode_frame(command: Command, address: int, data: int = 0) -> bytes:
    """Encode one operation. The caller supplies legal SPI/CS timing."""
    if command not in (Command.READ, Command.WRITE):
        raise ValueError("unsupported transport command")
    address = _unsigned(address, 8, "address")
    data = _unsigned(data, 16, "data")
    return bytes((int(command), address, data >> 8, data & 0xFF))


def decode_read(response: bytes) -> int:
    """Decode the four bytes sampled during a READ, validating its zero header."""
    if len(response) != 4 or response[:2] != b"\x00\x00":
        raise ValueError("a read response must contain a zero header and two data bytes")
    return int.from_bytes(response[2:], "big")


def require_interface_version(value: int, expected: int) -> None:
    """Refuse to continue when the device reports a different interface version.

    A version-1 (M1) script must not treat a version-2 engine as M1, and vice
    versa: START, busy and READ_DATA semantics differ.
    """
    if value != expected:
        raise RuntimeError(f"device reports interface version {value:#06x}; this host tool requires "
                           f"{expected:#06x} (see docs/host-interface.md)")
