"""Pad-level vocabulary shared by the environment, external devices and observers.

Data definitions only. Pad resolution lives in environment.py; protocol
devices and observers use only these levels, never engine state.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Protocol

PIN_COUNT = 8
LOW = "0"
HIGH = "1"
FLOATING = "Z"    # no strong driver and no pull
CONTENTION = "X"  # strong drivers disagree, or pull-up and pull-down together
LEVELS = (LOW, HIGH, FLOATING, CONTENTION)


@dataclass(frozen=True)
class Pads:
    """Resolved pad levels for one cycle; `levels[p]` is pin p."""

    levels: tuple[str, ...]

    def __post_init__(self) -> None:
        if len(self.levels) != PIN_COUNT or any(level not in LEVELS for level in self.levels):
            raise ValueError(f"pads need {PIN_COUNT} levels from {LEVELS}: {self.levels!r}")

    def level(self, pin: int) -> str:
        return self.levels[pin]

    def value_mask(self) -> int:
        return sum(1 << pin for pin, level in enumerate(self.levels) if level == HIGH)

    def defined_mask(self) -> int:
        return sum(1 << pin for pin, level in enumerate(self.levels) if level in (LOW, HIGH))

    def __str__(self) -> str:
        """Pins 7..0, left to right, e.g. '1Z0000X1'."""
        return "".join(reversed(self.levels))

    @classmethod
    def from_string(cls, text: str) -> Pads:
        """Inverse of __str__: pins 7..0, left to right."""
        return cls(tuple(reversed(text)))


class Device(Protocol):
    """An external circuit modeled as a synchronous Moore machine on the cycle grid."""

    def drives(self) -> Mapping[int, int]:
        """Strong drive during the current cycle: pin -> 0 or 1; released pins omitted.

        Must depend only on the device's state, never on the current cycle's pads.
        """
        ...

    def observe(self, cycle: int, pads: Pads) -> None:
        """Called once per cycle at the edge ending `cycle`, with that cycle's pads."""
        ...
