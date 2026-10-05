"""Cycle model of the PinScript ISA candidate v0.1 (docs/isa.md, docs/timing.md
"Engine timing").

The model executes encoded 16-bit words, one modeled core cycle per `step()`.
It decodes the words itself, from the section 2 field rules (it shares no code
with the assembler), and knows nothing about protocols or program names.

Structure of one cycle n (`Machine.step`):
  1. pad controls from the registered state, pads resolved by the Environment;
  2. electrical check (CONTENTION);
  3. evaluate: compute every next-state value from the state during cycle n,
     the synchronizer output SYNC2, the FIFO occupancies and the host event of
     cycle n, into a plan; nothing is written yet, so reset, host STOP, stops
     and environment errors can discard the cycle's effects cleanly;
  4. commit the plan at the edge e(n+1); clock the synchronizer; let the
     devices observe the pads of cycle n.

State is explicit and finite: every register is masked to its width, the FIFOs
are fixed four-slot circular buffers, and synchronizer stages hold 0, 1 or U
(a value bit plus a defined bit per pin).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from functools import lru_cache
from os import PathLike
from typing import TYPE_CHECKING, Callable, Sequence, Union

from . import isa
from .environment import Environment
from .isa import ProposedHostError, Reason, RunState
from .pads import Pads
from .trace import CycleRecord

if TYPE_CHECKING:
    from .model import LoaderModel

__all__ = [
    "EnvironmentViolation", "HarnessError", "Instruction", "Machine", "RunResult", "StopRecord",
    "decode", "disassemble", "is_legal", "load_image_json",
]

BYTE = 0xFF
WORD = 0xFFFF
T_MASK = (1 << isa.TIMER_BITS) - 1
FIFO_SLOTS = isa.FIFO_DEPTH


# =============================================================================== decoding

@dataclass(frozen=True)
class Instruction:
    """One decoded legal word. `op` is the mnemonic; unused fields stay 0/False."""

    word: int
    op: str
    flag: bool = False      # HALT HOLD; PULL/PUSH FAULT; WAIT ..., FAULT
    value: int = 0          # FAULT code, LDTH nibble, PIN mask, LD immediate, DELAY/LDT count
    reg: int = 0            # LD/MOV destination, DJNZ register (0 R0, 1 R1, 2 SR)
    src: int = 0            # MOV source register
    cond: int = 0           # JMP/WAIT condition code
    target: int = 0         # JMP/DJNZ/WAIT target
    msb: bool = False       # SHIFT d: 1 = MSB first
    drive: bool = False     # SHIFT o: OUT[a] <- outgoing bit
    shift: bool = False     # SHIFT s
    sample: bool = False    # SHIFT i: incoming bit from SYNC2[b]
    out_pin: int = 0        # SHIFT a
    in_pin: int = 0         # SHIFT b


def _field(word: int, high: int, low: int) -> int:
    """word[high:low]."""
    return (word >> low) & ((1 << (high - low + 1)) - 1)


_PIN_OPS = ("SET", "CLR", "TGL", "DRIVE", "RELEASE", "OPENDRAIN", "PUSHPULL")   # word[11:9] = 0..6
_SHIFT_FORMS = {(1, 0, 0): "OUTB", (1, 1, 0): "SHOUT", (1, 1, 1): "SHIO", (0, 1, 0): "SHIFT", (0, 1, 1): "SHIN"}
_RESERVED_CONDITIONS = range(0b01010, 0b10000)
_REG_NAMES = ("R0", "R1", "SR")
_COND_NAMES = ("ALWAYS", "NEVER", "TXE", "TXNE", "RXF", "RXNF", "LO(SR0)", "HI(SR0)", "LO(SR7)", "HI(SR7)")


def _decode_sys(word: int) -> Instruction | None:
    if _field(word, 7, 4) != 0:
        return None
    sub, arg = _field(word, 11, 8), _field(word, 3, 0)
    if sub == isa.SysOp.NOP:
        return Instruction(word, "NOP") if arg == 0 else None
    if sub == isa.SysOp.HALT:
        return Instruction(word, "HALT", flag=bool(arg & 1)) if arg >> 1 == 0 else None
    if sub == isa.SysOp.FAULT:
        return Instruction(word, "FAULT", value=arg) if arg >> 3 == 0 else None
    if sub in (isa.SysOp.PULL, isa.SysOp.PUSH):
        if arg >> 1:
            return None
        return Instruction(word, "PULL" if sub == isa.SysOp.PULL else "PUSH", flag=bool(arg & 1))
    if sub == isa.SysOp.LDTH:
        return Instruction(word, "LDTH", value=arg)
    return None                                     # sub-op 0 and 7..15 reserved


def _decode_pin(word: int) -> Instruction | None:
    op = _field(word, 11, 9)
    if op == 7 or _field(word, 8, 8):
        return None
    return Instruction(word, _PIN_OPS[op], value=_field(word, 7, 0))


def _decode_shift(word: int) -> Instruction | None:
    d, o, s, i = (_field(word, bit, bit) for bit in (11, 10, 9, 8))
    a, b = _field(word, 7, 5), _field(word, 4, 2)
    form = _SHIFT_FORMS.get((o, s, i))
    if form is None or _field(word, 1, 0) != 0 or (not o and a) or (not i and b):
        return None
    return Instruction(word, form, msb=bool(d), drive=bool(o), shift=bool(s), sample=bool(i),
                       out_pin=a, in_pin=b)


def _decode_ldmov(word: int) -> Instruction | None:
    dst, src, low = _field(word, 11, 10), _field(word, 9, 8), _field(word, 7, 0)
    if dst == 3:
        return None
    if src == isa.SRC_IMMEDIATE:
        return Instruction(word, "LD", reg=dst, value=low)
    return Instruction(word, "MOV", reg=dst, src=src) if low == 0 else None


def _decode_jmp(word: int) -> Instruction | None:
    cond = _field(word, 11, 7)
    if cond in _RESERVED_CONDITIONS or _field(word, 6, 6):
        return None
    return Instruction(word, "JMP", cond=cond, target=_field(word, 5, 0))


def _decode_djnz(word: int) -> Instruction | None:
    if _field(word, 10, 6) != 0:
        return None
    return Instruction(word, "DJNZ", reg=_field(word, 11, 11), target=_field(word, 5, 0))


def _decode_wait(word: int) -> Instruction | None:
    cond, fault, target = _field(word, 11, 7), _field(word, 6, 6), _field(word, 5, 0)
    if cond in _RESERVED_CONDITIONS or (fault and target != 0):
        return None
    return Instruction(word, "WAIT", cond=cond, flag=bool(fault), target=target)


@lru_cache(maxsize=None, typed=True)
def decode(word: int) -> Instruction | None:
    """Strict decode of one 16-bit word (isa.md section 2); None if the word is reserved."""
    if not isinstance(word, int) or isinstance(word, bool) or not 0 <= word <= WORD:
        raise ValueError(f"not a 16-bit word: {word!r}")
    major = _field(word, 15, 12)
    if major == isa.Opcode.SYS:
        return _decode_sys(word)
    if major == isa.Opcode.PIN:
        return _decode_pin(word)
    if major == isa.Opcode.SHIFT:
        return _decode_shift(word)
    if major == isa.Opcode.LDMOV:
        return _decode_ldmov(word)
    if major == isa.Opcode.JMP:
        return _decode_jmp(word)
    if major == isa.Opcode.DJNZ:
        return _decode_djnz(word)
    if major == isa.Opcode.DELAY:
        return Instruction(word, "DELAY", value=_field(word, 11, 0))
    if major == isa.Opcode.LDT:
        return Instruction(word, "LDT", value=_field(word, 11, 0))
    if major == isa.Opcode.WAIT:
        return _decode_wait(word)
    return None                                     # 0x9..0xF reserved


def is_legal(word: int) -> bool:
    return decode(word) is not None


def condition_text(cond: int) -> str:
    if cond >= 0b10000:
        return f"{'HI' if cond & 0b01000 else 'LO'}({cond & 0b111})"
    if cond < len(_COND_NAMES):
        return _COND_NAMES[cond]
    return f"<reserved condition {cond:05b}>"


def _pins_text(mask: int) -> str:
    pins = [str(pin) for pin in range(8) if (mask >> pin) & 1]
    return ", ".join(pins) if pins else "(no pins)"


@lru_cache(maxsize=None, typed=True)
def disassemble(word: int) -> str:
    """The model's own text for a word, e.g. 'SHOUT 0, LSB', 'WAIT LO(5), FAULT'."""
    ins = decode(word)
    if ins is None:
        return f"<illegal 0x{word:04X}>"
    op = ins.op
    if op in ("NOP",):
        return op
    if op == "HALT":
        return "HALT HOLD" if ins.flag else "HALT"
    if op in ("PULL", "PUSH"):
        return f"{op} FAULT" if ins.flag else op
    if op in ("FAULT", "LDTH", "DELAY", "LDT"):
        return f"{op} {ins.value}"
    if op in _PIN_OPS:
        return f"{op} {_pins_text(ins.value)}"
    order = "MSB" if ins.msb else "LSB"
    if op in ("OUTB", "SHOUT"):
        return f"{op} {ins.out_pin}, {order}"
    if op == "SHIO":
        return f"SHIO {ins.out_pin}, {ins.in_pin}, {order}"
    if op == "SHIFT":
        return f"SHIFT {order}"
    if op == "SHIN":
        return f"SHIN {ins.in_pin}, {order}"
    if op == "LD":
        return f"LD {_REG_NAMES[ins.reg]}, 0x{ins.value:02X}"
    if op == "MOV":
        return f"MOV {_REG_NAMES[ins.reg]}, {_REG_NAMES[ins.src]}"
    if op == "JMP":
        if ins.cond == isa.Cond.ALWAYS:
            return f"JMP {ins.target}"
        return f"JMP {condition_text(ins.cond)}, {ins.target}"
    if op == "DJNZ":
        return f"DJNZ {_REG_NAMES[ins.reg]}, {ins.target}"
    if op == "WAIT":
        return f"WAIT {condition_text(ins.cond)}, {'FAULT' if ins.flag else ins.target}"
    raise AssertionError(op)                        # unreachable: every decoded op is listed


# =============================================================================== errors and results

class HarnessError(RuntimeError):
    """Misuse of the model API, e.g. two host events presented in one cycle."""


class EnvironmentViolation(RuntimeError):
    """An environment error in cycle `cycle`: nothing of that cycle was committed.

    kind: 'CONTENTION' (a pad resolved X on a pin not allowed to) or
    'UNDEFINED_INPUT' (an executing instruction consumed a U synchronizer bit).
    `record` describes the failing cycle (events empty: nothing committed).
    """

    def __init__(self, kind: str, pin: int, cycle: int, record: CycleRecord | None = None) -> None:
        super().__init__(f"{kind} on pin {pin} in cycle {cycle}")
        self.kind = kind
        self.pin = pin
        self.cycle = cycle
        self.record = record


@dataclass(frozen=True)
class StopRecord:
    reason: Reason
    pc: int
    diag_in: int            # SYNC2 during the stop cycle; undefined bits read 0
    diag_in_defined: int    # mask of DIAG_IN bits that were defined (0/1) in the model
    elapsed: int
    stop_cycle: int         # absolute model cycle index of the stop cycle


@dataclass(frozen=True)
class RunResult:
    outcome: str            # 'stopped' | 'cycle_limit' | 'until' | 'environment_error'
    cycles: int             # cycles stepped by this call
    record: StopRecord | None
    violation: EnvironmentViolation | None


class _UndefinedInput(Exception):
    def __init__(self, pin: int) -> None:
        super().__init__(pin)
        self.pin = pin


# =============================================================================== state

class _Core:
    """Registered engine state (isa.md section 1). Constructed with reset values."""

    __slots__ = ("run", "pc", "r0", "r1", "sr", "t", "out", "oe", "od", "g", "c",
                 "elapsed", "reason", "diag_value", "diag_defined")

    def __init__(self) -> None:
        self.run = RunState.IDLE
        self.pc = 0
        self.r0 = 0
        self.r1 = 0
        self.sr = 0
        self.t = 0
        self.out = 0
        self.oe = 0
        self.od = 0
        self.g = 0          # reset clears G; START sets it
        self.c = 0
        self.elapsed = 0
        self.reason = Reason.NONE
        self.diag_value = 0
        self.diag_defined = BYTE

    def copy(self) -> _Core:
        other = _Core.__new__(_Core)
        for name in _Core.__slots__:
            setattr(other, name, getattr(self, name))
        return other

    def register(self, code: int) -> int:
        return (self.r0, self.r1, self.sr)[code]

    def set_register(self, code: int, value: int) -> None:
        setattr(self, ("r0", "r1", "sr")[code], value & BYTE)


def _started() -> _Core:
    """State registered at the edge ending an accepted START (isa.md section 8)."""
    core = _Core()          # PC, R0, R1, SR, T, OUT, OE, OD, C, ELAPSED, REASON, DIAG_IN cleared
    core.run = RunState.RUN
    core.g = 1
    return core


class _Fifo:
    """Four 8-bit slots, a 2-bit head pointer and a 0..4 occupancy count."""

    __slots__ = ("slots", "head", "count")

    def __init__(self) -> None:
        self.slots = [0] * FIFO_SLOTS
        self.head = 0
        self.count = 0

    def front(self) -> int:
        return self.slots[self.head]

    def contents(self) -> list[int]:
        return [self.slots[(self.head + k) % FIFO_SLOTS] for k in range(self.count)]

    def apply(self, pop: bool, push: int | None) -> None:
        """Edge update; both decisions were taken on the occupancy during the cycle
        (pop needs count > 0, push needs count < 4), so the tail slot written here is
        never the head slot being popped."""
        if push is not None:
            self.slots[(self.head + self.count) % FIFO_SLOTS] = push & BYTE
        if pop:
            self.head = (self.head + 1) % FIFO_SLOTS
        self.count += (push is not None) - int(pop)

    def clear(self) -> None:
        """Reset clears pointers and occupancy only; entry storage is not observable while empty."""
        self.head = 0
        self.count = 0


@dataclass(frozen=True)
class _HostEvent:
    kind: str                     # 'start' | 'stop' | 'tx_push' | 'rx_pop' | 'abort'
    result: ProposedHostError
    byte: int | None = None       # pushed byte, or the head byte a successful pop returns

    def text(self) -> str:
        if self.kind == "tx_push":
            return f"tx_push 0x{self.byte:02x} {self.result.name}"
        if self.kind == "rx_pop" and self.byte is not None:
            return f"rx_pop 0x{self.byte:02x} {self.result.name}"
        return f"{self.kind} {self.result.name}"


class _Plan:
    """Everything the edge ending the current cycle will register."""

    __slots__ = ("core", "reset", "tx_pop", "tx_push", "rx_pop", "rx_push", "invalidate",
                 "stop", "started", "events")

    def __init__(self, core: _Core) -> None:
        self.core = core
        self.reset = False
        self.tx_pop = False
        self.tx_push: int | None = None
        self.rx_pop = False
        self.rx_push: int | None = None
        self.invalidate = False
        self.stop: Reason | None = None
        self.started = False
        self.events: list[str] = []


_RUN_NAMES = {RunState.IDLE: "IDLE", RunState.RUN: "RUN", RunState.STOPPED: "STOPPED"}
_FAULT_EVENT = {reason: f"fault {reason.name}" for reason in Reason}


@lru_cache(maxsize=None)
def _levels_text(value: int, defined: int) -> str:
    """Synchronizer stage as text, pins 7..0: '0', '1' or 'U'."""
    return "".join("U" if not (defined >> pin) & 1 else str((value >> pin) & 1) for pin in range(7, -1, -1))


# =============================================================================== machine

class Machine:
    """One PinScript engine with its program store image, FIFOs and environment.

    `words` is program memory from address 0 (it may hold stale words beyond
    `loaded_count`, which are never executed). The machine starts in the reset
    state: IDLE, cycle 0, G = 0, synchronizers 0, FIFOs empty.
    """

    def __init__(self, words: Sequence[int] = (), *, depth: int = isa.DEFAULT_DEPTH,
                 environment: Environment | None = None, loaded_count: int | None = None,
                 program_valid: bool = True, loading: bool = False, record_trace: bool = True,
                 elapsed_bits: int = isa.ELAPSED_BITS) -> None:
        if depth not in isa.SUPPORTED_DEPTHS:
            raise ValueError(f"depth must be one of {isa.SUPPORTED_DEPTHS}")
        memory = tuple(words)
        for address, word in enumerate(memory):
            if not isinstance(word, int) or isinstance(word, bool) or not 0 <= word <= WORD:
                raise ValueError(f"word {address} is not a 16-bit value: {word!r}")
        if len(memory) > depth:
            raise ValueError(f"{len(memory)} words exceed depth {depth}")
        count = len(memory) if loaded_count is None else loaded_count
        if not isinstance(count, int) or not 0 <= count <= len(memory):
            raise ValueError(f"loaded_count must be 0..{len(memory)}")
        if program_valid and loading:
            raise ValueError("M1 never holds a valid image while loading")
        if program_valid and count == 0:
            raise ValueError("a committed image holds at least one word (M1 EXPECTED_LENGTH >= 1)")
        if not isinstance(elapsed_bits, int) or elapsed_bits < 1:
            raise ValueError("elapsed_bits must be a positive integer")
        self._depth = depth
        self._pc_mask = (1 << isa.pc_bits(depth)) - 1
        self._words = memory
        self._loaded_count = count
        self._program_valid = bool(program_valid)
        self._loading = bool(loading)
        self._elapsed_bits = elapsed_bits
        self._elapsed_max = (1 << elapsed_bits) - 1
        self._environment = environment if environment is not None else Environment()
        self._record_trace = record_trace
        self.trace: list[CycleRecord] = []
        self._cycle = 0
        self._core = _Core()
        self._sync1 = (0, BYTE)          # (value, defined mask): reset value 0
        self._sync2 = (0, BYTE)
        self._tx = _Fifo()
        self._rx = _Fifo()
        self._host: _HostEvent | None = None
        self._reset = False
        self._stop: StopRecord | None = None
        self._pads_masks: dict[Pads, tuple[int, int]] = {}

    @classmethod
    def from_loader(cls, loader: LoaderModel, **kwargs: object) -> Machine:
        """Machine over an M1 LoaderModel's store: memory (including stale words
        beyond the count), loaded_count, valid, loading and depth."""
        clash = {"words", "depth", "loaded_count", "program_valid", "loading"} & set(kwargs)
        if clash:
            raise TypeError(f"from_loader takes these from the loader: {sorted(clash)}")
        size = max(loader.memory, default=-1) + 1
        words = [loader.memory.get(address, 0) for address in range(size)]
        return cls(words, depth=loader.depth, loaded_count=loader.loaded_count,
                   program_valid=loader.valid, loading=loader.loading, **kwargs)  # type: ignore[arg-type]

    # ------------------------------------------------------------------ read-only state
    @property
    def cycle(self) -> int:
        """Index of the cycle the next step() evaluates."""
        return self._cycle

    @property
    def environment(self) -> Environment:
        return self._environment

    @property
    def depth(self) -> int:
        return self._depth

    @property
    def words(self) -> tuple[int, ...]:
        return self._words

    @property
    def elapsed_bits(self) -> int:
        return self._elapsed_bits

    @property
    def run_state(self) -> RunState:
        return self._core.run

    @property
    def pc(self) -> int:
        return self._core.pc

    @property
    def r0(self) -> int:
        return self._core.r0

    @property
    def r1(self) -> int:
        return self._core.r1

    @property
    def sr(self) -> int:
        return self._core.sr

    @property
    def t(self) -> int:
        return self._core.t

    @property
    def c(self) -> int:
        return self._core.c

    @property
    def out(self) -> int:
        return self._core.out

    @property
    def oe(self) -> int:
        return self._core.oe

    @property
    def od(self) -> int:
        return self._core.od

    @property
    def g(self) -> int:
        return self._core.g

    @property
    def uio_out(self) -> int:
        return self._pad_controls(self._core)[0]

    @property
    def uio_oe(self) -> int:
        return self._pad_controls(self._core)[1]

    @property
    def sync1(self) -> tuple[int, int]:
        """(value, defined_mask) during the current cycle."""
        return self._sync1

    @property
    def sync2(self) -> tuple[int, int]:
        return self._sync2

    @property
    def tx_count(self) -> int:
        return self._tx.count

    @property
    def rx_count(self) -> int:
        return self._rx.count

    @property
    def elapsed(self) -> int:
        return self._core.elapsed

    @property
    def reason(self) -> Reason:
        return self._core.reason

    @property
    def diag_in(self) -> tuple[int, int]:
        """(value, defined_mask) of DIAG_IN."""
        return (self._core.diag_value, self._core.diag_defined)

    @property
    def program_valid(self) -> bool:
        return self._program_valid

    @property
    def loading(self) -> bool:
        return self._loading

    @property
    def loaded_count(self) -> int:
        return self._loaded_count

    def tx_contents(self) -> list[int]:
        return self._tx.contents()

    def rx_contents(self) -> list[int]:
        return self._rx.contents()

    def stop_record(self) -> StopRecord | None:
        """The record of the last stop since START or reset, else None."""
        return self._stop

    # ------------------------------------------------------------------ host events
    # Each is presented during the current cycle and takes effect at the edge ending it.
    # The result is decided on the state during the cycle (registered occupancy).

    def _present(self, event: _HostEvent) -> None:
        if self._host is not None:
            raise HarnessError(f"cycle {self._cycle} already has host event {self._host.kind!r}; "
                               "at most one host event is presented per cycle")
        self._host = event

    def host_start(self) -> ProposedHostError:
        if self._core.run is RunState.RUN:
            result = ProposedHostError.BUSY
        elif not self._program_valid or self._loading:
            result = ProposedHostError.NO_PROGRAM
        else:
            result = ProposedHostError.NONE
        self._present(_HostEvent("start", result))
        return result

    def host_stop(self) -> ProposedHostError:
        self._present(_HostEvent("stop", ProposedHostError.NONE))
        return ProposedHostError.NONE

    def host_push_tx(self, byte: int) -> ProposedHostError:
        if not isinstance(byte, int) or isinstance(byte, bool) or not 0 <= byte <= BYTE:
            raise ValueError(f"TX push takes one byte, got {byte!r}")
        result = ProposedHostError.NONE if self._tx.count < FIFO_SLOTS else ProposedHostError.TX_FULL
        self._present(_HostEvent("tx_push", result, byte))
        return result

    def host_pop_rx(self) -> tuple[ProposedHostError, int | None]:
        if self._rx.count > 0:
            byte = self._rx.front()
            self._present(_HostEvent("rx_pop", ProposedHostError.NONE, byte))
            return ProposedHostError.NONE, byte
        self._present(_HostEvent("rx_pop", ProposedHostError.RX_EMPTY))
        return ProposedHostError.RX_EMPTY, None

    def host_abort_load(self) -> ProposedHostError:
        busy = self._core.run is RunState.RUN
        result = ProposedHostError.BUSY if busy else ProposedHostError.NONE
        self._present(_HostEvent("abort", result))
        return result

    def host_reset(self) -> None:
        """rst_n low during the current cycle; overrides that cycle's host event."""
        self._reset = True

    def peek_rx(self) -> tuple[bool, int]:
        """Proposed RX_DATA read without side effects: (valid, head byte or 0)."""
        return (True, self._rx.front()) if self._rx.count else (False, 0)

    # ------------------------------------------------------------------ stepping
    def step(self) -> CycleRecord:
        """Evaluate exactly one cycle; raises EnvironmentViolation (nothing committed)."""
        record = self._step(build_record=True)
        assert record is not None
        return record

    def run(self, max_cycles: int, *, until: Callable[[Machine], bool] | None = None) -> RunResult:
        """Step until a running cycle ends the run (the engine leaves RUN: 'stopped'),
        `until(machine)` holds after a step ('until'), an environment error occurs
        ('environment_error', returned, not raised), or `max_cycles` steps ('cycle_limit').
        A stop takes precedence over the predicate in the same step. Not being RUN is not
        itself a stop: an IDLE or STOPPED machine steps until the predicate or the limit,
        unless a START presented in the first stepped cycle makes it run and stop."""
        if not isinstance(max_cycles, int) or max_cycles < 0:
            raise ValueError("max_cycles must be a non-negative integer")
        for steps in range(max_cycles):
            was_running = self._core.run is RunState.RUN
            try:
                self._step(build_record=False)
            except EnvironmentViolation as violation:
                return RunResult("environment_error", steps, self._stop, violation)
            if was_running and self._core.run is not RunState.RUN:
                return RunResult("stopped", steps + 1, self._stop, None)
            if until is not None and until(self):
                return RunResult("until", steps + 1, self._stop, None)
        return RunResult("cycle_limit", max_cycles, self._stop, None)

    @staticmethod
    def _pad_controls(core: _Core) -> tuple[int, int]:
        """isa.md section 4: uio_out = OUT & ~OD; uio_oe = G & OE & (~OD | ~OUT)."""
        uio_out = core.out & ~core.od & BYTE
        uio_oe = core.oe & (~core.od | ~core.out) & BYTE if core.g else 0
        return uio_out, uio_oe

    def _fetch(self, core: _Core) -> int | None:
        """The word at PC when the fetch is valid (RUN, committed image, PC < loaded_count)."""
        if core.run is RunState.RUN and self._program_valid and core.pc < self._loaded_count:
            return self._words[core.pc]
        return None

    def _step(self, build_record: bool) -> CycleRecord | None:
        cycle = self._cycle
        core = self._core
        uio_out, uio_oe = self._pad_controls(core)
        pads = self._environment.resolve(uio_out, uio_oe)
        word = self._fetch(core)
        ins = decode(word) if word is not None else None
        host = self._host

        contention = self._environment.contention_pins(pads)
        if contention:
            raise EnvironmentViolation("CONTENTION", contention[0], cycle,
                                       self._record(cycle, core, word, ins, pads, uio_out, uio_oe, host, ()))
        try:
            plan = self._evaluate(core, word, ins, host)
        except _UndefinedInput as undefined:
            raise EnvironmentViolation(
                "UNDEFINED_INPUT", undefined.pin, cycle,
                self._record(cycle, core, word, ins, pads, uio_out, uio_oe, host, ())) from None

        record = None
        if build_record or self._record_trace:
            record = self._record(cycle, core, word, ins, pads, uio_out, uio_oe, host, tuple(plan.events))
            if self._record_trace:
                self.trace.append(record)
        self._commit(plan, core, pads, cycle)
        self._environment.observe(cycle, pads)
        self._host = None
        self._reset = False
        self._cycle = cycle + 1
        return record

    # ------------------------------------------------------------------ evaluation (no writes)
    def _evaluate(self, core: _Core, word: int | None, ins: Instruction | None,
                  host: _HostEvent | None) -> _Plan:
        plan = _Plan(core.copy())
        if self._reset:
            # Reset > everything: the instruction and host event of this cycle commit nothing.
            plan.reset = True
            plan.events.append("reset")
            return plan
        if core.run is RunState.RUN:
            if host is not None and host.kind == "stop":
                self._stop_now(plan, core, Reason.HOST_STOP)     # pre-empts the instruction
            elif word is None:
                self._stop_now(plan, core, Reason.PC_RANGE)      # checked before decode
            elif ins is None:
                self._stop_now(plan, core, Reason.ILLEGAL_INSTRUCTION)
            else:
                self._execute(plan, core, ins)
        if host is not None:
            self._host_effect(plan, core, host)
        return plan

    def _stop_now(self, plan: _Plan, core: _Core, reason: Reason, hold: bool = False) -> None:
        """The stop cycle: no other architectural effect commits. REASON and DIAG_IN are
        captured; PC, ELAPSED and the working registers stay frozen at their values."""
        nxt = core.copy()
        nxt.run = RunState.STOPPED
        nxt.reason = reason
        nxt.diag_value, nxt.diag_defined = self._sync2
        if not hold:
            nxt.g = 0
        plan.core = nxt
        plan.tx_pop = False                         # engine-side transfers never accompany a stop
        plan.rx_push = None
        plan.stop = reason
        if reason is Reason.HALT:
            plan.events.append("halt")
        elif reason is Reason.HOST_STOP:
            plan.events.append("stop HOST_STOP")
        else:
            plan.events.append(_FAULT_EVENT[reason])

    def _input(self, pin: int) -> int:
        """Consume SYNC2[pin]; a U bit is the UNDEFINED_INPUT environment error."""
        value, defined = self._sync2
        if not (defined >> pin) & 1:
            raise _UndefinedInput(pin)
        return (value >> pin) & 1

    def _condition(self, cond: int, core: _Core) -> bool:
        """Condition table of isa.md section 2, on state during the cycle."""
        if cond >= isa.COND_PIN_LO:                 # 10ppp LO(p) / 11ppp HI(p)
            return self._input(cond & 0b111) == (cond >> 3) & 1
        if cond == isa.Cond.ALWAYS:
            return True
        if cond == isa.Cond.NEVER:
            return False
        if cond == isa.Cond.TXE:
            return self._tx.count == 0
        if cond == isa.Cond.TXNE:
            return self._tx.count != 0
        if cond == isa.Cond.RXF:
            return self._rx.count == FIFO_SLOTS
        if cond == isa.Cond.RXNF:
            return self._rx.count != FIFO_SLOTS
        if cond == isa.Cond.SR0_LO:
            return core.sr & 1 == 0
        if cond == isa.Cond.SR0_HI:
            return core.sr & 1 == 1
        if cond == isa.Cond.SR7_LO:
            return (core.sr >> 7) & 1 == 0
        if cond == isa.Cond.SR7_HI:
            return (core.sr >> 7) & 1 == 1
        raise AssertionError(f"reserved condition {cond:05b} reached execution")

    def _execute(self, plan: _Plan, core: _Core, ins: Instruction) -> None:
        """One running cycle of a legal instruction; may end in a stop instead."""
        nxt = plan.core
        op = ins.op
        next_pc = (core.pc + 1) & self._pc_mask
        if op == "NOP":
            pass
        elif op == "HALT":
            return self._stop_now(plan, core, Reason.HALT, hold=ins.flag)
        elif op == "FAULT":
            return self._stop_now(plan, core, Reason(Reason.USER_0 + ins.value))
        elif op == "PULL":
            if self._tx.count > 0:
                nxt.sr = self._tx.front()
                plan.tx_pop = True
                plan.events.append(f"pull 0x{nxt.sr:02x}")
            elif ins.flag:
                return self._stop_now(plan, core, Reason.TX_UNDERFLOW)
            else:
                next_pc = core.pc                    # stall: only ELAPSED changes
        elif op == "PUSH":
            if self._rx.count < FIFO_SLOTS:
                plan.rx_push = core.sr
                plan.events.append(f"push 0x{core.sr:02x}")
            elif ins.flag:
                return self._stop_now(plan, core, Reason.RX_OVERFLOW)
            else:
                next_pc = core.pc
        elif op == "LDTH":
            nxt.t = (ins.value << 12) | (core.t & 0x0FFF)
        elif op == "SET":
            nxt.out = core.out | ins.value
        elif op == "CLR":
            nxt.out = core.out & ~ins.value & BYTE
        elif op == "TGL":
            nxt.out = core.out ^ ins.value
        elif op == "DRIVE":
            nxt.oe = core.oe | ins.value
        elif op == "RELEASE":
            nxt.oe = core.oe & ~ins.value & BYTE
        elif op == "OPENDRAIN":
            nxt.od = core.od | ins.value
        elif op == "PUSHPULL":
            nxt.od = core.od & ~ins.value & BYTE
        elif op in ("OUTB", "SHOUT", "SHIO", "SHIFT", "SHIN"):
            self._shift(nxt, core, ins)
        elif op == "LD":
            nxt.set_register(ins.reg, ins.value)
        elif op == "MOV":
            nxt.set_register(ins.reg, core.register(ins.src))
        elif op == "JMP":
            if self._condition(ins.cond, core):
                next_pc = ins.target
        elif op == "DJNZ":
            value = (core.register(ins.reg) - 1) & BYTE
            nxt.set_register(ins.reg, value)
            if value != 0:                          # the NEW value decides: 0 on entry runs 256 times
                next_pc = ins.target
        elif op == "DELAY":
            next_pc = self._delay(nxt, core, ins, next_pc)
        elif op == "LDT":
            nxt.t = ins.value                       # zero-extended: T[15:12] <- 0
        elif op == "WAIT":
            if self._condition(ins.cond, core):
                pass                                # complete; T unchanged
            elif core.t == 0:
                if ins.flag:
                    return self._stop_now(plan, core, Reason.WAIT_TIMEOUT)
                next_pc = ins.target
                plan.events.append(f"timeout ->{ins.target}")
            else:
                nxt.t = core.t - 1
                next_pc = core.pc
        else:
            raise AssertionError(op)
        nxt.pc = next_pc & self._pc_mask
        nxt.elapsed = min(core.elapsed + 1, self._elapsed_max)

    def _delay(self, nxt: _Core, core: _Core, ins: Instruction, next_pc: int) -> int:
        """timing.md DELAY (load-n rule). First cycle (C = 0): T <- n, C <- (n >= 1);
        DELAY 0 completes at once. Continuation (C = 1): complete when T = 1
        (T <- 0, C <- 0), else T <- T - 1."""
        if not core.c:
            nxt.t = ins.value
            if ins.value >= 1:
                nxt.c = 1
                return core.pc
            return next_pc
        if core.t == 1:
            nxt.t = 0
            nxt.c = 0
            return next_pc
        nxt.t = (core.t - 1) & T_MASK
        return core.pc

    def _shift(self, nxt: _Core, core: _Core, ins: Instruction) -> None:
        """isa.md section 2 SHIFT: the outgoing bit is taken before the shift."""
        outgoing = (core.sr >> 7) & 1 if ins.msb else core.sr & 1
        if ins.drive:
            nxt.out = (core.out & ~(1 << ins.out_pin) & BYTE) | (outgoing << ins.out_pin)
        if ins.shift:
            incoming = self._input(ins.in_pin) if ins.sample else 0
            if ins.msb:
                nxt.sr = ((core.sr << 1) & BYTE) | incoming      # SR <- {SR[6:0], in}
            else:
                nxt.sr = (incoming << 7) | (core.sr >> 1)        # SR <- {in, SR[7:1]}

    def _host_effect(self, plan: _Plan, core: _Core, host: _HostEvent) -> None:
        """Effect at the edge ending the cycle of a host event decided on this cycle's state."""
        accepted = host.result is ProposedHostError.NONE
        if host.kind == "start":
            if accepted:
                plan.core = _started()
                plan.started = True
                plan.events.append("start")
        elif host.kind == "stop":
            if core.run is not RunState.RUN:
                # Not running: STOP only clears G (REASON, PC and the record unchanged).
                plan.core.g = 0
                if core.g:
                    plan.events.append("g_clear")
        elif host.kind == "tx_push":
            if accepted:
                plan.tx_push = host.byte
                plan.events.append(f"tx_push 0x{host.byte:02x}")
        elif host.kind == "rx_pop":
            if accepted:
                plan.rx_pop = True
                plan.events.append(f"rx_pop 0x{host.byte:02x}")
        elif host.kind == "abort":
            if accepted:
                plan.invalidate = True
                plan.events.append("abort")
        else:
            raise AssertionError(host.kind)

    # ------------------------------------------------------------------ commit (edge e(n+1))
    def _commit(self, plan: _Plan, core: _Core, pads: Pads, cycle: int) -> None:
        if plan.reset:
            self._core = _Core()
            self._sync1 = (0, BYTE)
            self._sync2 = (0, BYTE)
            self._tx.clear()
            self._rx.clear()
            self._program_valid = False             # per M1: reset invalidates the image
            self._loading = False
            self._loaded_count = 0
            self._stop = None
            return
        self._core = plan.core
        self._tx.apply(pop=plan.tx_pop, push=plan.tx_push)
        self._rx.apply(pop=plan.rx_pop, push=plan.rx_push)
        if plan.invalidate:                         # M1 CONTROL=3 ABORT while not running
            self._program_valid = False
            self._loading = False
            self._loaded_count = 0
        self._sync2 = self._sync1
        self._sync1 = self._sample(pads)
        if plan.stop is not None:
            # The record is the registered state after the stop edge: REASON and DIAG_IN were
            # captured, PC and ELAPSED were frozen (not advanced) at the stop cycle's values.
            committed = self._core
            self._stop = StopRecord(reason=committed.reason, pc=committed.pc, diag_in=committed.diag_value,
                                    diag_in_defined=committed.diag_defined, elapsed=committed.elapsed,
                                    stop_cycle=cycle)
        if plan.started:
            self._stop = None

    def _sample(self, pads: Pads) -> tuple[int, int]:
        """Pad levels entering SYNC1: 0/1 as is, Z and X as U."""
        masks = self._pads_masks.get(pads)
        if masks is None:
            masks = (pads.value_mask(), pads.defined_mask())
            self._pads_masks[pads] = masks
        return masks

    # ------------------------------------------------------------------ trace rows
    def _phase(self, core: _Core, ins: Instruction | None) -> str:
        """timing.md phases, from the registered state during the cycle (a same-cycle
        host STOP or reset does not change the phase; the events show the pre-emption)."""
        if core.run is RunState.IDLE:
            return "IDLE"
        if core.run is RunState.STOPPED:
            return "STOPPED"
        if core.c:
            return "DELAY"
        if ins is not None:
            if ins.op == "WAIT":
                return "WAIT"
            if ins.op == "PULL" and not ins.flag and self._tx.count == 0:
                return "STALL"
            if ins.op == "PUSH" and not ins.flag and self._rx.count == FIFO_SLOTS:
                return "STALL"
        return "EXEC"

    def _record(self, cycle: int, core: _Core, word: int | None, ins: Instruction | None, pads: Pads,
                uio_out: int, uio_oe: int, host: _HostEvent | None, events: tuple[str, ...]) -> CycleRecord:
        host_text = host.text() if host is not None else ""
        if self._reset:
            host_text = f"reset; {host_text}" if host_text else "reset"
        return CycleRecord(
            cycle=cycle, run_state=_RUN_NAMES[core.run], phase=self._phase(core, ins), pc=core.pc,
            word=word, text=disassemble(word) if word is not None else "",
            r0=core.r0, r1=core.r1, sr=core.sr, t=core.t, c=core.c,
            out=core.out, oe=core.oe, od=core.od, g=core.g, uio_out=uio_out, uio_oe=uio_oe,
            pads=str(pads), sync1=_levels_text(*self._sync1), sync2=_levels_text(*self._sync2),
            tx_count=self._tx.count, rx_count=self._rx.count, elapsed=core.elapsed,
            reason=int(core.reason), host=host_text, events=events)


# =============================================================================== images

PathArg = Union[str, PathLike]


def load_image_json(path: PathArg) -> list[int]:
    """Words of an assembler JSON image. ValueError if the format, format_version or
    isa_version differ from this model's, or if the image is inconsistent."""
    with open(path, encoding="utf-8") as handle:
        image = json.load(handle)
    if not isinstance(image, dict):
        raise ValueError("an image is a JSON object")
    if image.get("format") != "pinscript-image" or image.get("format_version") != 1:
        raise ValueError(f"unsupported image format {image.get('format')!r} "
                         f"version {image.get('format_version')!r}")
    if image.get("isa_version") != isa.ISA_VERSION:
        raise ValueError(f"image isa_version {image.get('isa_version')!r} differs from the model's "
                         f"{isa.ISA_VERSION!r}")
    texts = image.get("words")
    if not isinstance(texts, list):
        raise ValueError("image words must be a list")
    words: list[int] = []
    for address, text in enumerate(texts):
        if (not isinstance(text, str) or len(text) != 4
                or any(ch not in "0123456789abcdefABCDEF" for ch in text)):
            raise ValueError(f"word {address} is not a 4-digit hex string: {text!r}")
        words.append(int(text, 16))
    if "length" in image and image["length"] != len(words):
        raise ValueError(f"image length {image['length']!r} differs from {len(words)} words")
    if "image_sha256" in image:
        digest = hashlib.sha256(b"".join(word.to_bytes(2, "big") for word in words)).hexdigest()
        if str(image["image_sha256"]).lower() != digest:
            raise ValueError("image_sha256 does not match the words")
    return words
