"""PinScript assembler for the ISA candidate v0.1 (docs/isa.md sections 2, 6 and 11).

The encoder places bits from the specification text. It shares only numeric
constants (``pinscript.isa``) with the cycle model and never imports it.
``is_legal_word`` is a separate legality predicate written directly from the
isa.md section 2 field rules; it checks ``.word`` values and guards the
encoder's promise that mnemonic instructions never produce reserved words.

Assembly runs in three passes over the source lines:

0. tokenize and parse every line, declaring labels and directive names;
1. assign word addresses and evaluate ``.equ``/``.param``/``.pin`` in source
   order (they may use only constants defined on earlier lines);
2. encode instructions and ``.word`` with every label known.

Errors on different lines are all collected before ``AsmError`` is raised.

Command line::

    python -m pinscript.asm SOURCE [-D NAME=VALUE ...] [--depth 32|64] [-o PREFIX] [--allow-illegal]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping, Sequence

from .interface import Command, Control, Register, encode_frame
from .isa import (
    COND_PIN_HI,
    COND_PIN_LO,
    DEFAULT_DEPTH,
    FAULT_CODE_MAX,
    IMM8_MAX,
    IMM12_MAX,
    ISA_VERSION,
    LDTH_MAX,
    PIN_COUNT,
    SRC_IMMEDIATE,
    SUPPORTED_DEPTHS,
    TARGET_BITS,
    Cond,
    Opcode,
    PinOp,
    Reg,
    SysOp,
)

__all__ = [
    "AsmError",
    "Diagnostic",
    "ListingEntry",
    "Program",
    "assemble",
    "assemble_file",
    "cycle_annotation",
    "is_legal_word",
    "main",
]

WORD_MAX = 0xFFFF
LDT_L_MAX = 0xFFFF
PIN_MAX = PIN_COUNT - 1
TARGET_MAX = (1 << TARGET_BITS) - 1
IMAGE_FORMAT = "pinscript-image"
IMAGE_FORMAT_VERSION = 1
BYTE_ORDER = "big-endian"
OUTPUT_SUFFIXES = (".hex", ".bin", ".lst", ".json", ".frames.txt")

# ---------------------------------------------------------------------------
# Legality and cycle annotation of a single word (isa.md section 2, timing.md)


def _condition_is_legal(code: int) -> bool:
    """Condition codes 0b01010..0b01111 are reserved; every other 5-bit code is defined."""
    return not 0b01010 <= code <= 0b01111


def is_legal_word(word: int) -> bool:
    """Return whether a 16-bit word is legal under isa.md section 2.

    Written from the encoding table alone (field positions and reserved
    values), independently of the encoder below. Legality depends only on the
    word: never on depth, program length, PC or machine state, so a branch
    target at or beyond the program length is still a legal word.
    """
    if isinstance(word, bool) or not isinstance(word, int) or not 0 <= word <= WORD_MAX:
        raise ValueError(f"not a 16-bit word: {word!r}")
    major = word >> 12
    if major == 0x0:  # SYS: sub-op [11:8] in 1..6, [7:4] = 0, argument [3:0]
        sub, middle, arg = (word >> 8) & 0xF, (word >> 4) & 0xF, word & 0xF
        if middle != 0:
            return False
        if sub == 1:  # NOP: argument 0
            return arg == 0
        if sub == 2:  # HALT: [3:1] = 0, [0] = hold
            return arg >> 1 == 0
        if sub == 3:  # FAULT: [3] = 0, [2:0] = code
            return arg >> 3 == 0
        if sub in (4, 5):  # PULL, PUSH: [3:1] = 0, [0] = fault flag
            return arg >> 1 == 0
        return sub == 6  # LDTH takes any argument; sub-ops 0 and 7..15 are reserved
    if major == 0x1:  # PIN: op [11:9] in 0..6, [8] = 0, any mask (including 0)
        return (word >> 9) & 0x7 != 7 and (word >> 8) & 0x1 == 0
    if major == 0x2:  # SHIFT: d [11], o [10], s [9], i [8], a [7:5], b [4:2], [1:0] = 0
        o, s, i = (word >> 10) & 1, (word >> 9) & 1, (word >> 8) & 1
        a, b = (word >> 5) & 0x7, (word >> 2) & 0x7
        if word & 0x3:
            return False
        if (o, s, i) not in ((1, 0, 0), (1, 1, 0), (1, 1, 1), (0, 1, 0), (0, 1, 1)):
            return False
        return (o == 1 or a == 0) and (i == 1 or b == 0)
    if major == 0x3:  # LD/MOV: destination [11:10] in 0..2; [7:0] = 0 unless source [9:8] = 3
        destination, source = (word >> 10) & 0x3, (word >> 8) & 0x3
        return destination != 3 and (source == 3 or word & 0xFF == 0)
    if major == 0x4:  # JMP: condition [11:7], [6] = 0, target [5:0]
        return _condition_is_legal((word >> 7) & 0x1F) and (word >> 6) & 0x1 == 0
    if major == 0x5:  # DJNZ: register [11], [10:6] = 0, target [5:0]
        return (word >> 6) & 0x1F == 0
    if major in (0x6, 0x7):  # DELAY, LDT: any 12-bit value
        return True
    if major == 0x8:  # WAIT: condition [11:7], fault flag [6], target [5:0] = 0 when [6] = 1
        fault, target = (word >> 6) & 0x1, word & 0x3F
        return _condition_is_legal((word >> 7) & 0x1F) and (fault == 0 or target == 0)
    return False  # major opcodes 0x9..0xF are reserved


def cycle_annotation(word: int) -> str:
    """Listing cycle annotation (timing.md "Instruction cycle counts").

    '1' for one-cycle instructions (including taken or not-taken branches),
    'n+1' as a number for DELAY n, '1+stall' for stalling PULL/PUSH,
    '1..T+1' for a WAIT whose length depends on the condition and the budget
    T on entry ('1' for WAIT ALWAYS, 'T+1' for WAIT NEVER, which always runs
    to timeout), and 'stop' for a word that ends the run in its first cycle
    (HALT, HALT HOLD, FAULT n, or an illegal word).
    """
    if not is_legal_word(word):
        return "stop"
    major = word >> 12
    if major == 0x0:
        sub = (word >> 8) & 0xF
        if sub in (2, 3):
            return "stop"
        if sub in (4, 5):
            return "1" if word & 0x1 else "1+stall"
        return "1"
    if major == 0x6:
        return str((word & 0xFFF) + 1)
    if major == 0x8:
        condition = (word >> 7) & 0x1F
        if condition == 0b00000:
            return "1"
        if condition == 0b00001:
            return "T+1"
        return "1..T+1"
    return "1"


# ---------------------------------------------------------------------------
# Bit placement for each instruction group (isa.md section 2 formulas)


def _sys_word(sub: int, argument: int) -> int:
    return (int(Opcode.SYS) << 12) | (sub << 8) | argument


def _pin_word(op: int, mask: int) -> int:
    return (int(Opcode.PIN) << 12) | (op << 9) | mask


def _shift_word(d: int, o: int, s: int, i: int, a: int, b: int) -> int:
    return (int(Opcode.SHIFT) << 12) | (d << 11) | (o << 10) | (s << 9) | (i << 8) | (a << 5) | (b << 2)


def _ldmov_word(destination: int, source: int, immediate: int) -> int:
    return (int(Opcode.LDMOV) << 12) | (destination << 10) | (source << 8) | immediate


def _jmp_word(condition: int, target: int) -> int:
    return (int(Opcode.JMP) << 12) | (condition << 7) | target


def _djnz_word(register: int, target: int) -> int:
    return (int(Opcode.DJNZ) << 12) | (register << 11) | target


def _delay_word(count: int) -> int:
    return (int(Opcode.DELAY) << 12) | count


def _ldt_word(value: int) -> int:
    return (int(Opcode.LDT) << 12) | value


def _wait_word(condition: int, fault: int, target: int) -> int:
    return (int(Opcode.WAIT) << 12) | (condition << 7) | (fault << 6) | target


# ---------------------------------------------------------------------------
# Vocabulary (isa.md section 11)

_PIN_MNEMONICS = ("SET", "CLR", "TGL", "DRIVE", "RELEASE", "OPENDRAIN", "PUSHPULL")
_SHIFT_FORMS: dict[str, tuple[int, int, int]] = {  # mnemonic -> (o, s, i)
    "OUTB": (1, 0, 0),
    "SHOUT": (1, 1, 0),
    "SHIO": (1, 1, 1),
    "SHIFT": (0, 1, 0),
    "SHIN": (0, 1, 1),
}
_SHIFT_SYNTAX = {
    "OUTB": "OUTB pin, LSB|MSB",
    "SHOUT": "SHOUT pin, LSB|MSB",
    "SHIO": "SHIO out_pin, in_pin, LSB|MSB",
    "SHIFT": "SHIFT LSB|MSB",
    "SHIN": "SHIN pin, LSB|MSB",
}
_MNEMONICS = frozenset(
    ("NOP", "HALT", "FAULT", "PULL", "PUSH", "LDTH", "LD", "MOV", "JMP", "DJNZ", "DELAY", "LDT", "LDT.L", "WAIT")
    + _PIN_MNEMONICS
    + tuple(_SHIFT_FORMS)
)
_DIRECTIVES = (".EQU", ".PARAM", ".PIN", ".WORD")
_DIRECTIVE_KINDS = {".EQU": "equ", ".PARAM": "param", ".PIN": "pin"}
_KEYWORDS = ("R0", "R1", "SR", "SR0", "SR7", "LSB", "MSB", "HOLD", "FAULT",
             "ALWAYS", "NEVER", "TXE", "TXNE", "RXF", "RXNF", "LO", "HI")
# "every mnemonic and directive name": directive names are reserved both with
# and without their leading dot (only the bare form can collide with a symbol).
_RESERVED = _MNEMONICS | frozenset(d[1:] for d in _DIRECTIVES) | frozenset(_KEYWORDS)

_REGISTERS = {"R0": int(Reg.R0), "R1": int(Reg.R1), "SR": int(Reg.SR)}
_SIMPLE_CONDITIONS = {
    "ALWAYS": int(Cond.ALWAYS),
    "NEVER": int(Cond.NEVER),
    "TXE": int(Cond.TXE),
    "TXNE": int(Cond.TXNE),
    "RXF": int(Cond.RXF),
    "RXNF": int(Cond.RXNF),
}
_SR_CONDITIONS = {
    ("LO", "SR0"): int(Cond.SR0_LO),
    ("HI", "SR0"): int(Cond.SR0_HI),
    ("LO", "SR7"): int(Cond.SR7_LO),
    ("HI", "SR7"): int(Cond.SR7_HI),
}
_CONDITION_STARTS = frozenset(_SIMPLE_CONDITIONS) | {"LO", "HI"}
_CONDITION_SYNTAX = ("ALWAYS, NEVER, TXE, TXNE, RXF, RXNF, LO(SR0), HI(SR0), LO(SR7), HI(SR7), "
                     "LO(pin) or HI(pin)")
_KIND_NOUNS = {"label": "label", "equ": "constant", "param": "parameter", "pin": "pin alias"}


def _condition_text(code: int) -> str:
    for name, value in _SIMPLE_CONDITIONS.items():
        if value == code:
            return name
    for (level, end), value in _SR_CONDITIONS.items():
        if value == code:
            return f"{level}({end})"
    level = "HI" if code & 0b11000 == COND_PIN_HI else "LO"
    return f"{level}({code & 0x7})"


# ---------------------------------------------------------------------------
# Public result types


@dataclass(frozen=True)
class Diagnostic:
    path: str
    line: int  # 1-based source line; 0 for problems with -D definitions
    severity: str  # 'error' | 'warning'
    message: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.severity}: {self.message}"


class AsmError(Exception):
    """Assembly failed; ``diagnostics`` holds every error, sorted by line."""

    def __init__(self, diagnostics: Sequence[Diagnostic], warnings: Sequence[Diagnostic] = ()) -> None:
        self.diagnostics: list[Diagnostic] = list(diagnostics)
        self.warnings: list[Diagnostic] = list(warnings)
        super().__init__(self.__str__())

    def __str__(self) -> str:
        return "\n".join(str(diagnostic) for diagnostic in self.diagnostics)


@dataclass(frozen=True)
class ListingEntry:
    address: int
    word: int
    line: int
    source: str  # the source line, stripped
    text: str  # the instruction as assembled, e.g. 'DELAY 84' or 'WAIT NEVER, 4'
    cycles: str  # see cycle_annotation()
    illegal: bool  # only a .word under allow_illegal can be True


def _yes(flag: bool) -> str:
    return "yes" if flag else "no"


@dataclass
class Program:
    words: list[int]
    listing: list[ListingEntry]
    labels: dict[str, int]
    constants: dict[str, int]  # .equ values
    parameters: dict[str, int]  # final .param values, after -D overrides
    pins: dict[str, int]  # .pin aliases
    source_path: str
    source_sha256: str
    depth: int
    warnings: list[Diagnostic]
    overridden: tuple[str, ...] = ()  # .param names whose value came from -D

    def fits(self, depth: int) -> bool:
        return len(self.words) <= depth

    def to_hex(self) -> str:
        return "".join(f"{word:04X}\n" for word in self.words)

    def to_bin(self) -> bytes:
        # High byte first: the byte order of APPEND_DATA frames.
        return b"".join(word.to_bytes(2, "big") for word in self.words)

    def to_json(self) -> dict:
        return {
            "format": IMAGE_FORMAT,
            "format_version": IMAGE_FORMAT_VERSION,
            "isa_version": ISA_VERSION,
            "source": self.source_path,
            "source_sha256": self.source_sha256,
            "parameters": dict(self.parameters),
            "length": len(self.words),
            "depth": self.depth,
            "fits": {"32": self.fits(32), "64": self.fits(64)},
            "words": [f"{word:04X}" for word in self.words],
            "labels": dict(self.labels),
            "byte_order": BYTE_ORDER,
            "image_sha256": hashlib.sha256(self.to_bin()).hexdigest(),
            "illegal_addresses": [entry.address for entry in self.listing if entry.illegal],
        }

    def listing_text(self) -> str:
        parameters = ", ".join(
            f"{name}={value}" + (" (-D)" if name in self.overridden else "")
            for name, value in self.parameters.items()
        ) or "(none)"
        lines = [
            "; PinScript assembler listing",
            f"; isa_version   {ISA_VERSION}",
            f"; source        {self.source_path}",
            f"; source_sha256 {self.source_sha256}",
            f"; depth         {self.depth}",
            f"; parameters    {parameters}",
            ";",
            "; addr word  line cycles  instruction             source",
        ]
        for entry in self.listing:
            text = entry.text + ("  ILLEGAL" if entry.illegal else "")
            lines.append(f"{entry.address:6d} {entry.word:04X} {entry.line:5d} {entry.cycles:<7} "
                         f"{text:<24}{entry.source}".rstrip())
        lines.append(";")
        lines.append(f"; {len(self.words)} words; fits depth 32: {_yes(self.fits(32))}; "
                     f"fits depth 64: {_yes(self.fits(64))}")
        for title, table in (("labels", self.labels), ("pins", self.pins), ("constants", self.constants)):
            if table:
                lines.append(f"; {title}: " + ", ".join(f"{name}={value}" for name, value in table.items()))
        lines.extend(f"; {warning}" for warning in self.warnings)
        return "\n".join(lines) + "\n"

    def load_frames(self) -> list[tuple[bytes, str]]:
        """M1 load sequence (isa.md section 11); hardware START stays unsupported, so none is sent."""
        length = len(self.words)
        frames = [
            (encode_frame(Command.WRITE, int(Register.EXPECTED_LENGTH), length),
             f"WRITE EXPECTED_LENGTH={length}"),
            (encode_frame(Command.WRITE, int(Register.CONTROL), int(Control.BEGIN_LOAD)),
             "WRITE CONTROL=BEGIN_LOAD"),
        ]
        for address, word in enumerate(self.words):
            frames.append((encode_frame(Command.WRITE, int(Register.APPEND_DATA), word),
                           f"WRITE APPEND_DATA=0x{word:04X} (address {address})"))
        frames.append((encode_frame(Command.WRITE, int(Register.CONTROL), int(Control.COMMIT)),
                       "WRITE CONTROL=COMMIT"))
        return frames

    def frames_text(self) -> str:
        return "".join(f"{frame.hex().upper()}  ; {comment}\n" for frame, comment in self.load_frames())


# ---------------------------------------------------------------------------
# Tokens and integer literals


_LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
_DECIMAL_DIGITS = "0123456789"
_HEX_DIGITS = _DECIMAL_DIGITS + "abcdefABCDEF"
_IDENT_START = frozenset(_LETTERS + "_")
_IDENT_CHARS = frozenset(_LETTERS + _DECIMAL_DIGITS + "_")
_PUNCTUATION = frozenset(",:()+-*")


def _parse_integer(text: str) -> int | None:
    """Value of an integer literal per isa.md section 11, or None if malformed.

    '0'; decimal '[1-9]('_'?[0-9])*'; '0x'/'0X' hex and '0b'/'0B' binary
    digits with single '_' separators between digits. ASCII only.
    """
    if text == "0":
        return 0
    if len(text) > 2 and text[0] == "0" and text[1] in "xXbB":
        base, digits = (16, text[2:]) if text[1] in "xX" else (2, text[2:])
        allowed = _HEX_DIGITS if base == 16 else "01"
    elif text[:1] in tuple("123456789"):
        base, digits, allowed = 10, text, _DECIMAL_DIGITS
    else:
        return None
    if digits[0] == "_" or digits[-1] == "_" or "__" in digits:
        return None
    if any(char not in allowed and char != "_" for char in digits):
        return None
    return int(digits.replace("_", ""), base)


def _is_identifier(text: str) -> bool:
    return bool(text) and text[0] in _IDENT_START and all(char in _IDENT_CHARS for char in text)


@dataclass(frozen=True)
class _Token:
    kind: str  # 'word' | 'directive' | 'number' | 'punct' | 'bad'
    text: str
    start: int  # 0-based offset in the line
    value: int = 0  # numbers only

    @property
    def end(self) -> int:
        return self.start + len(self.text)

    def is_punct(self, char: str) -> bool:
        return self.kind == "punct" and self.text == char

    def is_word(self, *keywords: str) -> bool:
        return self.kind == "word" and self.text.upper() in keywords


def _tokenize(code: str) -> tuple[list[_Token], list[str]]:
    """Hand-written scanner for one line with its comment removed.

    Words may contain dots ('LDT.L'); a symbol with a dot is rejected later.
    A number token is a maximal run of identifier characters starting with a
    digit, validated as a whole so that '08' or '12ab' are single errors.
    """
    tokens: list[_Token] = []
    problems: list[str] = []
    index, size = 0, len(code)
    while index < size:
        char = code[index]
        if char in " \t":
            index += 1
            continue
        start = index
        if char in _IDENT_START:
            index += 1
            while index < size:
                if code[index] in _IDENT_CHARS:
                    index += 1
                elif code[index] == "." and index + 1 < size and code[index + 1] in _IDENT_CHARS:
                    index += 2
                else:
                    break
            tokens.append(_Token("word", code[start:index], start))
        elif char == "." and index + 1 < size and code[index + 1] in _IDENT_START:
            index += 2
            while index < size and code[index] in _IDENT_CHARS:
                index += 1
            tokens.append(_Token("directive", code[start:index], start))
        elif char in _DECIMAL_DIGITS:
            index += 1
            while index < size and code[index] in _IDENT_CHARS:
                index += 1
            text = code[start:index]
            value = _parse_integer(text)
            if value is None:
                problems.append(f"invalid integer literal '{text}' (column {start + 1}); integers are 0, "
                                "decimal without leading zeros, 0x hex or 0b binary with single '_' "
                                "separators between digits")
                tokens.append(_Token("bad", text, start))
            else:
                tokens.append(_Token("number", text, start, value))
        elif char in _PUNCTUATION:
            index += 1
            tokens.append(_Token("punct", char, start))
        else:
            index += 1
            problems.append(f"unexpected character {char!r} (column {start + 1})")
            tokens.append(_Token("bad", char, start))
    return tokens, problems


def _split_operands(tokens: Sequence[_Token]) -> list[list[_Token]] | None:
    """Split on commas outside parentheses; None if any operand is empty."""
    if not tokens:
        return []
    groups: list[list[_Token]] = [[]]
    depth = 0
    for token in tokens:
        if token.is_punct("("):
            depth += 1
        elif token.is_punct(")"):
            depth -= 1
        if token.is_punct(",") and depth <= 0:
            groups.append([])
        else:
            groups[-1].append(token)
    if any(not group for group in groups):
        return None
    return groups


def _single_word(tokens: Sequence[_Token], *keywords: str) -> bool:
    return len(tokens) == 1 and tokens[0].is_word(*keywords)


def _is_condition_form(tokens: Sequence[_Token]) -> bool:
    """Whether an operand has the shape of a condition: a condition word, or LO(...) / HI(...)."""
    if len(tokens) == 1:
        return tokens[0].is_word(*_SIMPLE_CONDITIONS)
    return (len(tokens) >= 4 and tokens[0].is_word("LO", "HI") and tokens[1].is_punct("(")
            and tokens[-1].is_punct(")"))


# ---------------------------------------------------------------------------
# Expressions


class _OperandError(Exception):
    """An operand problem; the message becomes one diagnostic for its line."""


class _ExpressionParser:
    """Recursive descent over isa.md section 11:

        expr    := term (('+'|'-') term)*
        term    := unary ('*' unary)*
        unary   := '-' unary | primary
        primary := integer | symbol | '(' expr ')'

    Python integers give exact arithmetic with no intermediate range limits;
    callers range-check only the final value for its field.
    """

    MAX_NESTING = 64  # parenthesis depth; a diagnostic instead of a Python recursion error

    def __init__(self, tokens: Sequence[_Token], resolve: Callable[[_Token], int]) -> None:
        self.tokens = tokens
        self.resolve = resolve
        self.position = 0
        self.nesting = 0

    def parse(self) -> int:
        value = self._expr()
        if self.position < len(self.tokens):
            raise _OperandError(f"unexpected '{self.tokens[self.position].text}' in expression")
        return value

    def _peek_punct(self, *chars: str) -> str | None:
        if self.position < len(self.tokens):
            token = self.tokens[self.position]
            if token.kind == "punct" and token.text in chars:
                return token.text
        return None

    def _expr(self) -> int:
        value = self._term()
        while (operator := self._peek_punct("+", "-")) is not None:
            self.position += 1
            right = self._term()
            value = value + right if operator == "+" else value - right
        return value

    def _term(self) -> int:
        value = self._unary()
        while self._peek_punct("*") is not None:
            self.position += 1
            value *= self._unary()
        return value

    def _unary(self) -> int:
        negate = False
        while self._peek_punct("-") is not None:  # '-' unary, iterated
            self.position += 1
            negate = not negate
        value = self._primary()
        return -value if negate else value

    def _primary(self) -> int:
        if self.position >= len(self.tokens):
            raise _OperandError("expression ends where a value is expected")
        token = self.tokens[self.position]
        self.position += 1
        if token.kind == "number":
            return token.value
        if token.kind == "word":
            return self.resolve(token)
        if token.is_punct("("):
            self.nesting += 1
            if self.nesting > self.MAX_NESTING:
                raise _OperandError(f"expression nested more than {self.MAX_NESTING} parentheses deep")
            value = self._expr()
            if self._peek_punct(")") is None:
                raise _OperandError("missing ')' in expression")
            self.position += 1
            self.nesting -= 1
            return value
        raise _OperandError(f"expected a value, found '{token.text}'")


# ---------------------------------------------------------------------------
# The assembler


@dataclass
class _Symbol:
    name: str
    kind: str  # 'label' | 'equ' | 'param' | 'pin'
    line: int
    value: int | None = None
    state: str = "pending"  # 'pending' until pass 1 reaches it, then 'ok' or 'failed'


@dataclass
class _Statement:
    line: int
    source: str
    code: str  # the line without its comment
    label: _Symbol | None = None
    op: str = ""  # upper-case mnemonic ('LDT.L') or directive ('.EQU'); '' if none or unknown
    operands: list[list[_Token]] = field(default_factory=list)
    symbol: _Symbol | None = None  # the name defined by .equ/.param/.pin
    address: int = 0
    size: int = 0  # words emitted; counted even when the line has errors
    failed: bool = False  # a problem was already reported; do not evaluate further


_Encoded = list[tuple[int, str, bool]]  # (word, text, illegal) per emitted word


class _Assembler:
    def __init__(self, source: str, path: str, defines: Mapping[str, int], depth: int,
                 allow_illegal: bool) -> None:
        self.source = source
        self.path = path
        self.defines = defines
        self.depth = depth
        self.allow_illegal = allow_illegal
        self.errors: list[Diagnostic] = []
        self.warnings: list[Diagnostic] = []
        self.symbols: dict[str, _Symbol] = {}
        self.statements: list[_Statement] = []
        self.overrides: dict[str, int] = {}
        self.length = 0
        self.line_count = 1
        self.handlers: dict[str, Callable[[_Statement], _Encoded | None]] = {
            "NOP": self._encode_nop,
            "HALT": self._encode_halt,
            "FAULT": self._encode_fault,
            "PULL": self._encode_fifo,
            "PUSH": self._encode_fifo,
            "LDTH": self._encode_ldth,
            "LD": self._encode_ld,
            "MOV": self._encode_mov,
            "JMP": self._encode_jmp,
            "DJNZ": self._encode_djnz,
            "DELAY": self._encode_delay,
            "LDT": self._encode_ldt,
            "LDT.L": self._encode_ldt_long,
            "WAIT": self._encode_wait,
            ".WORD": self._encode_word,
        }
        self.handlers.update({name: self._encode_pin_op for name in _PIN_MNEMONICS})
        self.handlers.update({name: self._encode_shift for name in _SHIFT_FORMS})

    # -- diagnostics -------------------------------------------------------

    def _error(self, line: int, message: str) -> None:
        self.errors.append(Diagnostic(self.path, line, "error", message))

    def _warning(self, line: int, message: str) -> None:
        self.warnings.append(Diagnostic(self.path, line, "warning", message))

    # -- driver ------------------------------------------------------------

    def run(self) -> Program:
        self._parse_lines()
        self._check_defines()
        self._layout()
        self._check_size()
        listing = self._encode_all()
        if self.errors:
            raise AsmError(sorted(self.errors, key=lambda d: d.line),
                           sorted(self.warnings, key=lambda d: d.line))
        words = [entry.word for entry in listing]
        if [entry.address for entry in listing] != list(range(self.length)):
            raise RuntimeError("internal error: listing addresses are not contiguous")
        return Program(
            words=words,
            listing=listing,
            labels=self._values_of("label"),
            constants=self._values_of("equ"),
            parameters=self._values_of("param"),
            pins=self._values_of("pin"),
            source_path=self.path,
            source_sha256=hashlib.sha256(self.source.encode("utf-8")).hexdigest(),
            depth=self.depth,
            warnings=sorted(self.warnings, key=lambda d: d.line),
            overridden=tuple(name for name in self._values_of("param") if name in self.overrides),
        )

    def _values_of(self, kind: str) -> dict[str, int]:
        return {symbol.name: symbol.value for symbol in self.symbols.values()
                if symbol.kind == kind and symbol.value is not None}

    # -- pass 0: parse lines and declare names ---------------------------------

    def _parse_lines(self) -> None:
        lines = self.source.split("\n")
        if len(lines) > 1 and lines[-1] == "":
            lines.pop()  # the newline ending the last line does not start another line
        self.line_count = max(1, len(lines))
        for number, raw in enumerate(lines, start=1):
            if raw.endswith("\r"):
                raw = raw[:-1]
            if number == 1 and raw.startswith("\ufeff"):
                raw = raw[1:]
            self.statements.append(self._parse_line(number, raw))

    def _parse_line(self, number: int, raw: str) -> _Statement:
        code = raw.split(";", 1)[0]
        statement = _Statement(line=number, source=raw.strip(), code=code)
        tokens, problems = _tokenize(code)
        for problem in problems:
            self._error(number, problem)
        statement.failed = bool(problems)

        position = 0
        if len(tokens) >= 2 and tokens[1].is_punct(":"):
            if tokens[0].kind != "bad":  # a malformed token was already reported by the scanner
                statement.label = self._declare(tokens[0], "label", number)
            position = 2
            if len(tokens) >= 4 and tokens[3].is_punct(":"):
                self._error(number, f"at most one label per line: '{tokens[2].text}:' follows "
                                    f"'{tokens[0].text}:'")
                statement.failed = True
                position = 4
        if position == len(tokens):
            return statement

        head, rest = tokens[position], tokens[position + 1:]
        if head.kind == "directive":
            if head.text.upper() not in _DIRECTIVES:
                self._error(number, f"unknown directive '{head.text}' (directives: .equ, .param, .pin, .word)")
                statement.failed = True
                return statement
            statement.op = head.text.upper()
            statement.size = 1 if statement.op == ".WORD" else 0
        elif head.kind == "word" and head.text.upper() in _MNEMONICS:
            statement.op = head.text.upper()
            statement.size = 2 if statement.op == "LDT.L" else 1
        elif head.kind == "word":
            self._error(number, f"unknown mnemonic '{head.text}'")
            statement.failed = True
            statement.size = 1  # most likely a misspelled instruction
            return statement
        else:
            self._error(number, f"expected a mnemonic or directive, found '{head.text}'")
            statement.failed = True
            return statement

        operands = _split_operands(rest)
        if operands is None:
            self._error(number, f"empty operand in '{code.strip()}' (check the commas)")
            statement.failed = True
            return statement
        statement.operands = operands
        if statement.op in _DIRECTIVE_KINDS:
            self._parse_definition(statement)
        return statement

    def _parse_definition(self, statement: _Statement) -> None:
        directive = statement.op.lower()
        operands = statement.operands
        if len(operands) != 2:
            self._error(statement.line, f"{directive} takes a name and a value: {directive} NAME, value")
            statement.failed = True
        if not operands:
            return
        name = operands[0]
        if len(name) != 1:
            self._error(statement.line, f"invalid {directive} name '{self._text(statement, name)}'")
            statement.failed = True
            return
        # Declared even if the value is malformed, so that later uses report one
        # precise "has errors" message instead of an "undefined symbol" cascade.
        statement.symbol = self._declare(name[0], _DIRECTIVE_KINDS[statement.op], statement.line)

    def _declare(self, token: _Token, kind: str, line: int) -> _Symbol | None:
        noun = _KIND_NOUNS[kind]
        name = token.text
        if token.kind != "word" or not _is_identifier(name):
            self._error(line, f"invalid {noun} name '{name}' (names are [A-Za-z_][A-Za-z0-9_]*)")
            return None
        if name.upper() in _RESERVED:
            self._error(line, f"'{name}' is a reserved word and cannot be a {noun} name")
            return None
        prior = self.symbols.get(name)
        if prior is not None:
            what = "label" if kind == prior.kind == "label" else "symbol"
            self._error(line, f"duplicate {what} '{name}' (already defined as a {_KIND_NOUNS[prior.kind]} "
                              f"on line {prior.line})")
            return None
        symbol = _Symbol(name, kind, line)
        self.symbols[name] = symbol
        return symbol

    def _check_defines(self) -> None:
        for name, value in self.defines.items():
            symbol = self.symbols.get(name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                self._error(0, f"-D {name}: the value must be a non-negative integer literal, got {value!r}")
            elif symbol is None:
                self._error(0, f"-D {name}={value}: no '.param {name}' is declared; -D must name a declared .param")
            elif symbol.kind != "param":
                self._error(0, f"-D {name}={value}: '{name}' is a {_KIND_NOUNS[symbol.kind]} "
                               f"(line {symbol.line}), not a .param")
            else:
                self.overrides[name] = value

    # -- pass 1: addresses and constants -----------------------------------------

    def _layout(self) -> None:
        address = 0
        for statement in self.statements:
            statement.address = address
            if statement.label is not None:
                statement.label.value, statement.label.state = address, "ok"
            if statement.symbol is not None:
                self._define(statement, statement.symbol)
            address += statement.size
        self.length = address

    def _define(self, statement: _Statement, symbol: _Symbol) -> None:
        value: int | None = None
        if not statement.failed:
            if symbol.kind == "pin":
                value = self._pin(statement, statement.operands[1], constant=True)
            else:
                value = self._evaluate(statement, statement.operands[1], self._resolve_constant)
        if symbol.kind == "param" and symbol.name in self.overrides:
            value = self.overrides[symbol.name]
        symbol.value = value
        symbol.state = "failed" if value is None else "ok"

    def _check_size(self) -> None:
        if self.length == 0:
            self._error(self.line_count, "the program has no words; the M1 loader needs 1 to depth words")
            return
        if self.length > self.depth:
            statement = next(s for s in self.statements if s.address <= self.depth < s.address + s.size)
            self._error(statement.line, f"capacity overflow: the program is {self.length} words but depth "
                                        f"{self.depth} holds at most {self.depth}; this line assembles "
                                        f"address {self.depth}")

    # -- symbol resolution -----------------------------------------------------

    def _lookup(self, token: _Token) -> _Symbol:
        name = token.text
        if not _is_identifier(name):
            raise _OperandError(f"invalid symbol name '{name}'")
        if name.upper() in _RESERVED:
            raise _OperandError(f"reserved word '{name}' cannot be used as a value")
        symbol = self.symbols.get(name)
        if symbol is None:
            matches = [other for other in self.symbols if other.lower() == name.lower()]
            hint = f" (symbols are case-sensitive; did you mean '{matches[0]}'?)" if matches else ""
            raise _OperandError(f"undefined symbol '{name}'{hint}")
        return symbol

    def _resolve_constant(self, token: _Token) -> int:
        """Names allowed in .equ, .param and .pin values: constants defined on earlier lines."""
        symbol = self._lookup(token)
        if symbol.kind == "label":
            raise _OperandError(f"label '{symbol.name}' cannot be used here: .equ, .param and .pin values "
                                "may reference only previously defined constants")
        if symbol.kind == "pin":
            raise _OperandError(f"pin alias '{symbol.name}' is not a numeric value")
        if symbol.state == "pending":
            raise _OperandError(f"{_KIND_NOUNS[symbol.kind]} '{symbol.name}' is used before its definition "
                                f"on line {symbol.line}")
        if symbol.state == "failed" or symbol.value is None:
            raise _OperandError(f"{_KIND_NOUNS[symbol.kind]} '{symbol.name}' has no value: its definition "
                                f"on line {symbol.line} has errors")
        return symbol.value

    def _resolve_instruction(self, token: _Token) -> int:
        """Names allowed in instruction and .word operands: labels and constants, anywhere in the file."""
        symbol = self._lookup(token)
        if symbol.kind == "pin":
            raise _OperandError(f"pin alias '{symbol.name}' is not a numeric value; use it as a whole "
                                "pin operand")
        if symbol.state != "ok" or symbol.value is None:
            raise _OperandError(f"{_KIND_NOUNS[symbol.kind]} '{symbol.name}' has no value: its definition "
                                f"on line {symbol.line} has errors")
        return symbol.value

    # -- operand helpers (each reports its own error and returns None) -------------

    @staticmethod
    def _text(statement: _Statement, tokens: Sequence[_Token]) -> str:
        return statement.code[tokens[0].start:tokens[-1].end] if tokens else ""

    def _describe(self, statement: _Statement, tokens: Sequence[_Token], value: int) -> str:
        """The value for a message, preceded by its expression text unless that is just the number."""
        text = self._text(statement, tokens)
        if text.replace(" ", "").replace("\t", "") == str(value):
            return str(value)
        return f"'{text}' = {value}"

    def _evaluate(self, statement: _Statement, tokens: Sequence[_Token],
                  resolve: Callable[[_Token], int] | None = None) -> int | None:
        try:
            return _ExpressionParser(tokens, resolve or self._resolve_instruction).parse()
        except _OperandError as problem:
            self._error(statement.line, str(problem))
            return None

    def _value(self, statement: _Statement, tokens: Sequence[_Token], what: str, low: int,
               high: int) -> int | None:
        value = self._evaluate(statement, tokens)
        if value is None:
            return None
        if not low <= value <= high:
            self._error(statement.line, f"{what} {self._describe(statement, tokens, value)} is out of range "
                                        f"({low}..{high})")
            return None
        return value

    def _pin(self, statement: _Statement, tokens: Sequence[_Token], *, constant: bool = False) -> int | None:
        """A pin operand: a .pin alias used whole, or an expression with a value 0..7."""
        if len(tokens) == 1 and tokens[0].kind == "word":
            symbol = self.symbols.get(tokens[0].text)
            if symbol is not None and symbol.kind == "pin":
                if symbol.state == "pending":
                    self._error(statement.line, f"pin alias '{symbol.name}' is used before its definition "
                                                f"on line {symbol.line}")
                    return None
                if symbol.state == "failed" or symbol.value is None:
                    self._error(statement.line, f"pin alias '{symbol.name}' has no value: its definition on "
                                                f"line {symbol.line} has errors")
                    return None
                return symbol.value
        resolve = self._resolve_constant if constant else self._resolve_instruction
        value = self._evaluate(statement, tokens, resolve)
        if value is None:
            return None
        if not 0 <= value <= PIN_MAX:
            self._error(statement.line, f"pin {self._describe(statement, tokens, value)} is out of range "
                                        f"(0..{PIN_MAX})")
            return None
        return value

    def _register(self, statement: _Statement, tokens: Sequence[_Token], what: str,
                  allowed: tuple[str, ...] = ("R0", "R1", "SR")) -> int | None:
        if len(tokens) == 1 and tokens[0].is_word(*allowed):
            return _REGISTERS[tokens[0].text.upper()]
        choices = ", ".join(allowed[:-1]) + f" or {allowed[-1]}"
        self._error(statement.line, f"{what} must be {choices}, found '{self._text(statement, tokens)}'")
        return None

    def _direction(self, statement: _Statement, tokens: Sequence[_Token]) -> int | None:
        if _single_word(tokens, "LSB"):
            return 0
        if _single_word(tokens, "MSB"):
            return 1
        self._error(statement.line, f"expected the shift direction LSB or MSB, found "
                                    f"'{self._text(statement, tokens)}'")
        return None

    def _condition(self, statement: _Statement, tokens: Sequence[_Token]) -> int | None:
        first = tokens[0]
        if len(tokens) == 1 and first.is_word(*_SIMPLE_CONDITIONS):
            return _SIMPLE_CONDITIONS[first.text.upper()]
        if (len(tokens) >= 4 and first.is_word("LO", "HI") and tokens[1].is_punct("(")
                and tokens[-1].is_punct(")")):
            level, inner = first.text.upper(), tokens[2:-1]
            if _single_word(inner, "SR0", "SR7"):
                return _SR_CONDITIONS[(level, inner[0].text.upper())]
            if not (len(inner) == 1 and inner[0].kind == "word" and inner[0].text.upper() in _RESERVED):
                pin = self._pin(statement, inner)
                if pin is None:
                    return None
                return (COND_PIN_HI if level == "HI" else COND_PIN_LO) | pin
        self._error(statement.line, f"invalid condition '{self._text(statement, tokens)}'; expected "
                                    f"{_CONDITION_SYNTAX}")
        return None

    def _target(self, statement: _Statement, tokens: Sequence[_Token]) -> int | None:
        """An explicit branch target: an absolute address below the program length, never wrapped."""
        value = self._evaluate(statement, tokens)
        if value is None:
            return None
        described = self._describe(statement, tokens, value)
        if value < 0:
            self._error(statement.line, f"branch target {described} is negative")
        elif value == self.length:
            self._error(statement.line, f"branch target {described} is the program length; targets must be "
                                        "below it (a label after the last word cannot be a branch target)")
        elif value > self.length:
            self._error(statement.line, f"branch target {described} is beyond the program length "
                                        f"{self.length}")
        elif value > TARGET_MAX:
            self._error(statement.line, f"branch target {described} does not fit the {TARGET_BITS}-bit "
                                        "target field")
        else:
            return value
        return None

    def _expect_operands(self, statement: _Statement, count: int, syntax: str) -> bool:
        if len(statement.operands) == count:
            return True
        name = statement.op.lower() if statement.op.startswith(".") else statement.op
        self._error(statement.line, f"{name} takes {count} operand{'' if count == 1 else 's'}, "
                                    f"got {len(statement.operands)}: {syntax}")
        return False

    # -- pass 2: encoding ----------------------------------------------------------

    def _encode_all(self) -> list[ListingEntry]:
        listing: list[ListingEntry] = []
        for statement in self.statements:
            if statement.failed or not statement.op or statement.op in _DIRECTIVE_KINDS:
                continue
            encoded = self.handlers[statement.op](statement)
            if encoded is None:
                continue
            if len(encoded) != statement.size:
                raise RuntimeError(f"internal error: {statement.op} emitted {len(encoded)} words, "
                                   f"expected {statement.size}")
            for offset, (word, text, illegal) in enumerate(encoded):
                if not illegal and not is_legal_word(word):
                    raise RuntimeError(f"internal error: {statement.op} produced reserved word 0x{word:04X}")
                listing.append(ListingEntry(statement.address + offset, word, statement.line,
                                            statement.source, text, cycle_annotation(word), illegal))
        return listing

    def _encode_nop(self, statement: _Statement) -> _Encoded | None:
        if not self._expect_operands(statement, 0, "NOP"):
            return None
        return [(_sys_word(int(SysOp.NOP), 0), "NOP", False)]

    def _encode_halt(self, statement: _Statement) -> _Encoded | None:
        operands = statement.operands
        if not operands:
            return [(_sys_word(int(SysOp.HALT), 0), "HALT", False)]
        if len(operands) == 1 and _single_word(operands[0], "HOLD"):
            return [(_sys_word(int(SysOp.HALT), 1), "HALT HOLD", False)]
        self._error(statement.line, "HALT takes no operand, or HOLD: HALT | HALT HOLD")
        return None

    def _encode_fault(self, statement: _Statement) -> _Encoded | None:
        if not self._expect_operands(statement, 1, "FAULT code"):
            return None
        code = self._value(statement, statement.operands[0], "FAULT code", 0, FAULT_CODE_MAX)
        if code is None:
            return None
        return [(_sys_word(int(SysOp.FAULT), code), f"FAULT {code}", False)]

    def _encode_fifo(self, statement: _Statement) -> _Encoded | None:
        sub = int(SysOp.PULL) if statement.op == "PULL" else int(SysOp.PUSH)
        operands = statement.operands
        if not operands:
            return [(_sys_word(sub, 0), statement.op, False)]
        if len(operands) == 1 and _single_word(operands[0], "FAULT"):
            return [(_sys_word(sub, 1), f"{statement.op} FAULT", False)]
        self._error(statement.line, f"{statement.op} takes no operand, or FAULT: {statement.op} | "
                                    f"{statement.op} FAULT")
        return None

    def _encode_ldth(self, statement: _Statement) -> _Encoded | None:
        if not self._expect_operands(statement, 1, "LDTH value"):
            return None
        value = self._value(statement, statement.operands[0], "LDTH value", 0, LDTH_MAX)
        if value is None:
            return None
        return [(_sys_word(int(SysOp.LDTH), value), f"LDTH {value}", False)]

    def _encode_pin_op(self, statement: _Statement) -> _Encoded | None:
        if not statement.operands:
            self._error(statement.line, f"{statement.op} needs a list of 1 to {PIN_COUNT} distinct pins "
                                        "(a zero mask can be emitted only with .word)")
            return None
        pins: list[int] = []
        valid = True
        for operand in statement.operands:
            pin = self._pin(statement, operand)
            if pin is None:
                valid = False
            elif pin in pins:
                self._error(statement.line, f"duplicate pin {pin} in the {statement.op} pin list")
                valid = False
            else:
                pins.append(pin)
        if not valid:
            return None
        mask = sum(1 << pin for pin in pins)
        text = f"{statement.op} " + ", ".join(str(pin) for pin in sorted(pins))
        return [(_pin_word(int(PinOp[statement.op]), mask), text, False)]

    def _encode_shift(self, statement: _Statement) -> _Encoded | None:
        o, s, i = _SHIFT_FORMS[statement.op]
        operands = statement.operands
        needed = o + i + 1  # output pin if o, input pin if i, then the mandatory direction
        if len(operands) != needed:
            if len(operands) == needed - 1 and not (operands and _single_word(operands[-1], "LSB", "MSB")):
                self._error(statement.line, f"{statement.op} needs an explicit shift direction: the last "
                                            f"operand must be LSB or MSB ({_SHIFT_SYNTAX[statement.op]})")
            else:
                self._error(statement.line, f"{statement.op} takes {needed} operand{'' if needed == 1 else 's'}, "
                                            f"got {len(operands)}: {_SHIFT_SYNTAX[statement.op]}")
            return None
        out_pin = self._pin(statement, operands[0]) if o else 0
        in_pin = self._pin(statement, operands[o]) if i else 0
        direction = self._direction(statement, operands[-1])
        if out_pin is None or in_pin is None or direction is None:
            return None
        fields = ([str(out_pin)] if o else []) + ([str(in_pin)] if i else []) + [("LSB", "MSB")[direction]]
        text = f"{statement.op} " + ", ".join(fields)
        return [(_shift_word(direction, o, s, i, out_pin, in_pin), text, False)]

    def _encode_ld(self, statement: _Statement) -> _Encoded | None:
        if not self._expect_operands(statement, 2, "LD R0|R1|SR, value"):
            return None
        destination = self._register(statement, statement.operands[0], "LD destination")
        source = statement.operands[1]
        if _single_word(source, *_REGISTERS):
            self._error(statement.line, f"LD takes an immediate value; use MOV for a register source "
                                        f"(MOV {self._text(statement, statement.operands[0])}, "
                                        f"{source[0].text})")
            return None
        value = self._value(statement, source, "LD immediate", 0, IMM8_MAX)
        if destination is None or value is None:
            return None
        register = ("R0", "R1", "SR")[destination]
        return [(_ldmov_word(destination, SRC_IMMEDIATE, value), f"LD {register}, {value}", False)]

    def _encode_mov(self, statement: _Statement) -> _Encoded | None:
        if not self._expect_operands(statement, 2, "MOV R0|R1|SR, R0|R1|SR"):
            return None
        destination = self._register(statement, statement.operands[0], "MOV destination")
        source_tokens = statement.operands[1]
        if not _single_word(source_tokens, *_REGISTERS):
            self._error(statement.line, f"MOV takes register sources only (R0, R1 or SR); use LD for an "
                                        f"immediate value, found '{self._text(statement, source_tokens)}'")
            return None
        source = _REGISTERS[source_tokens[0].text.upper()]
        if destination is None:
            return None
        names = ("R0", "R1", "SR")
        return [(_ldmov_word(destination, source, 0), f"MOV {names[destination]}, {names[source]}", False)]

    def _encode_jmp(self, statement: _Statement) -> _Encoded | None:
        operands = statement.operands
        if len(operands) == 1:
            if _is_condition_form(operands[0]):
                self._error(statement.line, "JMP with a condition needs a target: JMP cond, target")
                return None
            if operands[0][0].is_word(*_CONDITION_STARTS):
                self._error(statement.line, f"invalid JMP operand '{self._text(statement, operands[0])}': "
                                            "expected JMP target or JMP cond, target")
                return None
            condition: int | None = int(Cond.ALWAYS)  # 'JMP t' means 'JMP ALWAYS, t'
            target = self._target(statement, operands[0])
        elif len(operands) == 2:
            condition = self._condition(statement, operands[0])
            target = self._target(statement, operands[1])
        else:
            self._error(statement.line, f"JMP takes a target or a condition and a target, got "
                                        f"{len(operands)} operands: JMP target | JMP cond, target")
            return None
        if condition is None or target is None:
            return None
        text = f"JMP {target}" if condition == int(Cond.ALWAYS) else f"JMP {_condition_text(condition)}, {target}"
        return [(_jmp_word(condition, target), text, False)]

    def _encode_djnz(self, statement: _Statement) -> _Encoded | None:
        if not self._expect_operands(statement, 2, "DJNZ R0|R1, target"):
            return None
        register = self._register(statement, statement.operands[0], "DJNZ counter", ("R0", "R1"))
        target = self._target(statement, statement.operands[1])
        if register is None or target is None:
            return None
        return [(_djnz_word(register, target), f"DJNZ R{register}, {target}", False)]

    def _encode_delay(self, statement: _Statement) -> _Encoded | None:
        if not self._expect_operands(statement, 1, "DELAY count"):
            return None
        count = self._value(statement, statement.operands[0], "DELAY count", 0, IMM12_MAX)
        if count is None:
            return None
        return [(_delay_word(count), f"DELAY {count}", False)]

    def _encode_ldt(self, statement: _Statement) -> _Encoded | None:
        if not self._expect_operands(statement, 1, "LDT value"):
            return None
        value = self._value(statement, statement.operands[0], "LDT value", 0, IMM12_MAX)
        if value is None:
            return None
        return [(_ldt_word(value), f"LDT {value}", False)]

    def _encode_ldt_long(self, statement: _Statement) -> _Encoded | None:
        # Always two words, even when v < 4096: LDTH also clears a stale T[15:12].
        if not self._expect_operands(statement, 1, "LDT.L value"):
            return None
        value = self._value(statement, statement.operands[0], "LDT.L value", 0, LDT_L_MAX)
        if value is None:
            return None
        low, high = value & IMM12_MAX, value >> 12
        return [(_ldt_word(low), f"LDT {low}", False),
                (_sys_word(int(SysOp.LDTH), high), f"LDTH {high}", False)]

    def _encode_wait(self, statement: _Statement) -> _Encoded | None:
        operands = statement.operands
        if len(operands) == 1 and _single_word(operands[0], "NEVER"):
            # The exact-delay idiom: the implicit timeout target is the next address,
            # which must exist (it is never wrapped to 0).
            target = statement.address + 1
            if target >= self.length:
                self._error(statement.line, f"WAIT NEVER is the last word: its implicit timeout target "
                                            f"(the next address, {target}) is not below the program length "
                                            f"{self.length}")
                return None
            if target > TARGET_MAX:
                self._error(statement.line, f"WAIT NEVER at address {statement.address}: the implicit target "
                                            f"{target} does not fit the {TARGET_BITS}-bit target field")
                return None
            never = int(Cond.NEVER)
            return [(_wait_word(never, 0, target), f"WAIT NEVER, {target}", False)]
        if len(operands) == 1 and _is_condition_form(operands[0]):
            self._error(statement.line, "the operand-less WAIT is accepted only as WAIT NEVER; write "
                                        "WAIT cond, target or WAIT cond, FAULT")
            return None
        if len(operands) != 2:
            self._error(statement.line, "WAIT takes a condition and a timeout target or FAULT: "
                                        "WAIT cond, target | WAIT cond, FAULT | WAIT NEVER")
            return None
        condition = self._condition(statement, operands[0])
        if _single_word(operands[1], "FAULT"):
            if condition is None:
                return None
            return [(_wait_word(condition, 1, 0), f"WAIT {_condition_text(condition)}, FAULT", False)]
        target = self._target(statement, operands[1])
        if condition is None or target is None:
            return None
        return [(_wait_word(condition, 0, target), f"WAIT {_condition_text(condition)}, {target}", False)]

    def _encode_word(self, statement: _Statement) -> _Encoded | None:
        if not self._expect_operands(statement, 1, ".word value"):
            return None
        value = self._value(statement, statement.operands[0], ".word value", 0, WORD_MAX)
        if value is None:
            return None
        if is_legal_word(value):
            return [(value, f".word 0x{value:04X}", False)]
        if not self.allow_illegal:
            self._error(statement.line, f".word 0x{value:04X} is a reserved (illegal) encoding; it is emitted "
                                        "only when illegal words are allowed (--allow-illegal)")
            return None
        self._warning(statement.line, f".word 0x{value:04X} is a reserved (illegal) encoding, emitted because "
                                      "illegal words are allowed; executing it faults ILLEGAL_INSTRUCTION")
        return [(value, f".word 0x{value:04X}", True)]


# ---------------------------------------------------------------------------
# Public entry points


def assemble(source: str, *, path: str = "<string>", defines: Mapping[str, int] | None = None,
             depth: int = DEFAULT_DEPTH, allow_illegal: bool = False) -> Program:
    """Assemble source text; raise AsmError listing every error found."""
    if depth not in SUPPORTED_DEPTHS:
        raise ValueError(f"depth must be one of {SUPPORTED_DEPTHS}, got {depth!r}")
    return _Assembler(source, path, dict(defines or {}), depth, allow_illegal).run()


def assemble_file(path: str | Path, *, defines: Mapping[str, int] | None = None, depth: int = DEFAULT_DEPTH,
                  allow_illegal: bool = False) -> Program:
    """Assemble a UTF-8 source file. OSError propagates; invalid UTF-8 raises AsmError.

    The source SHA-256 is that of the file's bytes (strict UTF-8 decoding
    round-trips exactly).
    """
    data = Path(path).read_bytes()
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as problem:
        line = data[:problem.start].count(b"\n") + 1
        raise AsmError([Diagnostic(str(path), line, "error", "the source is not valid UTF-8")]) from None
    return assemble(text, path=str(path), defines=defines, depth=depth, allow_illegal=allow_illegal)


def _parse_define_options(items: Sequence[str], path: str) -> tuple[dict[str, int], list[Diagnostic]]:
    """Parse -D NAME=VALUE options, VALUE being an integer literal (isa.md section 11)."""
    defines: dict[str, int] = {}
    problems: list[Diagnostic] = []
    for item in items:
        name, separator, text = item.partition("=")
        if not separator or not _is_identifier(name):
            problems.append(Diagnostic(path, 0, "error", f"-D {item}: expected NAME=integer"))
            continue
        value = _parse_integer(text)
        if value is None:
            problems.append(Diagnostic(path, 0, "error", f"-D {item}: '{text}' is not an integer literal "
                                                         "(0, decimal, 0x hex or 0b binary)"))
        elif name in defines:
            problems.append(Diagnostic(path, 0, "error", f"-D {name} is given more than once"))
        else:
            defines[name] = value
    return defines, problems


def _print_diagnostics(diagnostics: Sequence[Diagnostic]) -> None:
    for diagnostic in sorted(diagnostics, key=lambda d: d.line):
        print(diagnostic, file=sys.stderr)


def _render_outputs(program: Program) -> list[bytes]:
    """Output file contents in OUTPUT_SUFFIXES order."""
    return [
        program.to_hex().encode("ascii"),
        program.to_bin(),
        program.listing_text().encode("utf-8"),
        (json.dumps(program.to_json(), indent=2) + "\n").encode("utf-8"),
        program.frames_text().encode("ascii"),
    ]


def main(argv: Sequence[str] | None = None) -> int:
    """Command-line entry point; returns 0 on success, 1 on errors (nothing written), 2 on usage errors."""
    parser = argparse.ArgumentParser(
        prog="python -m pinscript.asm",
        description=f"Assemble a PinScript source file (ISA candidate {ISA_VERSION}).",
    )
    parser.add_argument("source", help="assembly source file (UTF-8)")
    parser.add_argument("-D", dest="defines", action="append", default=[], metavar="NAME=VALUE",
                        help="override a declared .param with an integer literal (repeatable)")
    parser.add_argument("--depth", type=int, choices=SUPPORTED_DEPTHS, default=DEFAULT_DEPTH,
                        help="program store depth for the capacity check (default %(default)s)")
    parser.add_argument("-o", dest="prefix", metavar="PREFIX",
                        help="output prefix for PREFIX.hex/.bin/.lst/.json/.frames.txt "
                             "(default: the source path without its suffix)")
    parser.add_argument("--allow-illegal", action="store_true",
                        help="let .word emit reserved encodings (warned and marked ILLEGAL)")
    try:
        args = parser.parse_args(None if argv is None else list(argv))
    except SystemExit as stop:  # usage errors exit 2, --help exits 0
        return stop.code if isinstance(stop.code, int) else 2

    source: str = args.source
    defines, problems = _parse_define_options(args.defines, source)
    try:
        program = assemble_file(source, defines=defines, depth=args.depth, allow_illegal=args.allow_illegal)
    except AsmError as failure:
        _print_diagnostics(problems + failure.diagnostics + failure.warnings)
        return 1
    except OSError as failure:
        reason = failure.strerror or str(failure)
        _print_diagnostics(problems + [Diagnostic(source, 0, "error", f"cannot read the source: {reason}")])
        return 1
    if problems:
        _print_diagnostics(problems + program.warnings)
        return 1

    prefix = args.prefix if args.prefix is not None else str(Path(source).with_suffix(""))
    targets = [Path(prefix + suffix) for suffix in OUTPUT_SUFFIXES]
    source_file = Path(source).resolve()
    if any(target.resolve() == source_file for target in targets):
        _print_diagnostics(program.warnings + [Diagnostic(source, 0, "error", f"output prefix '{prefix}' "
                                                          "would overwrite the source file")])
        return 1
    contents = _render_outputs(program)
    written: list[Path] = []
    try:
        for target, data in zip(targets, contents):
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            written.append(target)
    except OSError as failure:
        for target in written:
            target.unlink(missing_ok=True)
        _print_diagnostics(program.warnings + [Diagnostic(source, 0, "error",
                                                          f"cannot write {failure.filename}: "
                                                          f"{failure.strerror or failure}")])
        return 1
    _print_diagnostics(program.warnings)
    print(f"{source}: {len(program.words)} words; fits depth 32: {_yes(program.fits(32))}; "
          f"fits depth 64: {_yes(program.fits(64))}; wrote {prefix}{{{','.join(OUTPUT_SUFFIXES)}}}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
