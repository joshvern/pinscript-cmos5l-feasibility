"""Pad environment for the PinScript cycle model (docs/timing.md, "Pads, environment
and undefined levels").

The environment is everything outside the engine core: it resolves the engine's
(uio_out, uio_oe) against external devices (strong drivers) and modeled pulls
(weak sources) into one pad level per pin and cycle. The core never reads it
directly; the cycle model samples resolved pads into its synchronizer.

Devices are synchronous Moore machines on the cycle grid (pinscript.pads.Device):
`drives()` describes the current cycle and must not depend on the current pads;
`observe(n, pads)` is called at the edge ending cycle n. A device's "current
cycle" is therefore 0 before any observe call and n+1 after observe(n, ...).
"""

from __future__ import annotations

from bisect import bisect_right
from typing import Iterable, Mapping, Sequence

from .pads import CONTENTION, FLOATING, HIGH, LOW, PIN_COUNT, Device, Pads

PIN_MASK = (1 << PIN_COUNT) - 1


def _check_pin(pin: object, what: str) -> int:
    if not isinstance(pin, int) or isinstance(pin, bool) or not 0 <= pin < PIN_COUNT:
        raise ValueError(f"{what} must be a pin number 0..{PIN_COUNT - 1}, got {pin!r}")
    return pin


def _check_level(level: object, what: str) -> int:
    if not isinstance(level, int) or isinstance(level, bool) or level not in (0, 1):
        raise ValueError(f"{what} must be 0 or 1, got {level!r}")
    return level


def _check_mask(mask: object, what: str) -> int:
    if not isinstance(mask, int) or isinstance(mask, bool) or not 0 <= mask <= PIN_MASK:
        raise ValueError(f"{what} must be an 8-bit pin mask, got {mask!r}")
    return mask


class Environment:
    """Pad resolution plus the external devices and pulls of one test setup.

    pulls: pin -> 1 (pull-up) or 0 (pull-down), weak. `add_pull` can add a second,
    opposing pull to a pin (the table's "pull-up and pull-down" row).
    allow_contention: pin mask for which an X pad is not reported as the
    CONTENTION environment error; it still enters the synchronizer as undefined.
    """

    def __init__(self, pulls: Mapping[int, int] | None = None, devices: Iterable[Device] = (),
                 allow_contention: int = 0) -> None:
        self._pull_up = 0
        self._pull_down = 0
        self._devices: list[Device] = []
        self._cache: dict[tuple[int, int], Pads] = {}
        self.allow_contention = _check_mask(allow_contention, "allow_contention")
        for pin, level in (pulls or {}).items():
            self.add_pull(pin, level)
        for device in devices:
            self.add(device)

    @property
    def devices(self) -> tuple[Device, ...]:
        return tuple(self._devices)

    @property
    def pull_up(self) -> int:
        """Mask of pins with a modeled pull-up."""
        return self._pull_up

    @property
    def pull_down(self) -> int:
        """Mask of pins with a modeled pull-down."""
        return self._pull_down

    def add(self, device: Device) -> None:
        if not callable(getattr(device, "drives", None)) or not callable(getattr(device, "observe", None)):
            raise TypeError(f"{device!r} does not implement drives() and observe()")
        self._devices.append(device)

    def add_pull(self, pin: int, level: int) -> None:
        bit = 1 << _check_pin(pin, "pull pin")
        if _check_level(level, f"pull level for pin {pin}"):
            self._pull_up |= bit
        else:
            self._pull_down |= bit
        self._cache.clear()

    def resolve(self, uio_out: int, uio_oe: int) -> Pads:
        """Pad levels of the current cycle (timing.md resolution table)."""
        _check_mask(uio_out, "uio_out")
        _check_mask(uio_oe, "uio_oe")
        # Strong sources: the engine on its enabled pins, then every device's drives.
        high = uio_oe & uio_out
        low = uio_oe & ~uio_out & PIN_MASK
        for device in self._devices:
            for pin, level in device.drives().items():
                bit = 1 << _check_pin(pin, f"pin driven by {device!r}")
                if _check_level(level, f"level driven by {device!r} on pin {pin}"):
                    high |= bit
                else:
                    low |= bit
        key = (high, low)
        pads = self._cache.get(key)
        if pads is None:
            pads = Pads(tuple(self._level(pin, high, low) for pin in range(PIN_COUNT)))
            self._cache[key] = pads
        return pads

    def _level(self, pin: int, high: int, low: int) -> str:
        strong_high = (high >> pin) & 1
        strong_low = (low >> pin) & 1
        if strong_high or strong_low:
            return CONTENTION if strong_high and strong_low else (HIGH if strong_high else LOW)
        weak_high = (self._pull_up >> pin) & 1
        weak_low = (self._pull_down >> pin) & 1
        if weak_high or weak_low:
            return CONTENTION if weak_high and weak_low else (HIGH if weak_high else LOW)
        return FLOATING

    def contention_pins(self, pads: Pads) -> list[int]:
        """Pins resolving to X that are not in allow_contention, lowest first."""
        return [pin for pin in range(PIN_COUNT)
                if pads.levels[pin] == CONTENTION and not (self.allow_contention >> pin) & 1]

    def observe(self, cycle: int, pads: Pads) -> None:
        """Edge ending `cycle`: every device sees that cycle's pads, in insertion order."""
        for device in self._devices:
            device.observe(cycle, pads)


class ScheduledDriver:
    """Basic Device: from cycle c (inclusive) drive `pin` at level 0/1, or release (None).

    `events` is a sequence of (cycle, level | None); before the first event the pin
    is released. The schedule is in absolute model cycles, like Machine.cycle.
    """

    def __init__(self, pin: int, events: Sequence[tuple[int, int | None]]) -> None:
        self.pin = _check_pin(pin, "driver pin")
        ordered = sorted(events, key=lambda event: event[0])
        cycles = [cycle for cycle, _ in ordered]
        for cycle, level in ordered:
            if not isinstance(cycle, int) or isinstance(cycle, bool) or cycle < 0:
                raise ValueError(f"schedule cycles must be non-negative integers, got {cycle!r}")
            if level is not None:
                _check_level(level, "scheduled level")
        if len(set(cycles)) != len(cycles):
            raise ValueError("a schedule may hold at most one event per cycle")
        self._cycles = cycles
        self._levels = [level for _, level in ordered]
        self._cycle = 0

    def level_at(self, cycle: int) -> int | None:
        """Scheduled drive during `cycle` (None: released)."""
        index = bisect_right(self._cycles, cycle) - 1
        return None if index < 0 else self._levels[index]

    def drives(self) -> dict[int, int]:
        level = self.level_at(self._cycle)
        return {} if level is None else {self.pin: level}

    def observe(self, cycle: int, pads: Pads) -> None:
        self._cycle = cycle + 1

    def __repr__(self) -> str:
        return f"ScheduledDriver(pin={self.pin})"
