"""External devices for the PinScript M2 cycle model: synchronous Moore machines on pad levels.

Every class implements the ``pinscript.pads.Device`` protocol:

* ``drives()`` is the strong drive during the device's *current* cycle (pin -> 0 or 1;
  released pins omitted). It depends only on the device's own state, never on the pads
  of the current cycle.
* ``observe(n, pads)`` is called once per cycle, in order from cycle 0, at the edge
  e(n+1) that ends cycle n, with that cycle's resolved pads. The current cycle is 0
  before any observe call and n+1 after ``observe(n, ...)``; a call with any other
  cycle number raises ValueError, so a device must be attached from cycle 0.

Timing convention (docs/timing.md, "Pads, environment and undefined levels"): a device
observes the pads of cycle n at e(n+1) and can change its drive from cycle n+1 at the
earliest; "delay d after observing X in cycle n" means the new drive starts in cycle
n+1+d. An *edge* is a change between the two defined levels '0' and '1' in consecutive
observed cycles: a pass through 'Z' or 'X' is never an edge. Anomalies a device notices
(undefined inputs it relies on, protocol misuse) are appended to its ``errors`` list as
(cycle, message); they never stop the simulation.

Devices see only pad levels. They never read engine state and never import the
assembler, the cycle model, the environment or the trace module.
"""

from __future__ import annotations

from typing import Sequence

from .pads import HIGH, LOW, PIN_COUNT, Pads

__all__ = ["HandshakeResponder", "LowHolder", "SpiPeripheralMode0"]

_DEFINED = (LOW, HIGH)


def _check_pin(value: object, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value < PIN_COUNT:
        raise ValueError(f"{name} must be a pin number 0..{PIN_COUNT - 1}, got {value!r}")
    return value


def _check_count(value: object, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer, got {value!r}")
    return value


def _check_distinct(pins: dict[str, int]) -> None:
    if len(set(pins.values())) != len(pins):
        raise ValueError(f"device pins must be distinct: {pins}")


def _expect_cycle(device: object, expected: int, cycle: object) -> int:
    """Devices are clocked: every cycle is observed exactly once, in order, from cycle 0."""
    if not isinstance(cycle, int) or isinstance(cycle, bool) or cycle != expected:
        raise ValueError(f"{type(device).__name__}.observe expected cycle {expected}, got {cycle!r} "
                         "(a device observes every cycle in order from cycle 0)")
    return cycle


class SpiPeripheralMode0:
    """SPI peripheral, mode 0 (CPOL=0, CPHA=0), MSB first; drives MISO only while selected.

    * CS_n observed falling in cycle n (``'1'`` in cycle n-1, ``'0'`` in cycle n) selects the
      device and loads the next response byte (0xFF once ``responses`` is exhausted). Its MSB
      is driven on MISO from cycle n+1+miso_delay; MISO stays released until then.
    * While selected, SCLK observed rising in cycle n samples the MOSI level of cycle n (the
      first cycle SCLK is high); eight samples complete one byte of ``received``.
    * While selected, SCLK observed falling in cycle n shifts the response: the next bit is
      driven from cycle n+1+miso_delay. The eighth fall after a load loads the next response
      byte and drives its MSB instead, so every load consumes one response byte -- including
      the byte loaded by the last fall of a transaction that CS_n then ends.
    * CS_n observed not low (high, or undefined) in cycle n deselects the device: MISO is
      released from cycle n+1, scheduled MISO changes are discarded and an incomplete MOSI
      byte is dropped (logged in ``errors``).

    A low CS_n that does not follow an observed high level (trace start, or after 'Z'/'X')
    does not select the device, and SCLK edges in the cycle CS_n falls are neither sampled
    nor shifted. Those cases, SCLK not low in both cycles around the CS_n fall, and undefined
    CS_n, SCLK (while selected) or MOSI (at a sample) levels are logged in ``errors``.

    ``events`` records the protocol timeline as (observed cycle, text): ``'select'``,
    ``'load 0x..'``, ``'rx 0x..'`` (at the eighth rise) and ``'deselect'``.
    """

    def __init__(self, *, sclk: int, mosi: int, miso: int, cs_n: int, responses: Sequence[int],
                 miso_delay: int = 0) -> None:
        self.sclk = _check_pin(sclk, "sclk")
        self.mosi = _check_pin(mosi, "mosi")
        self.miso = _check_pin(miso, "miso")
        self.cs_n = _check_pin(cs_n, "cs_n")
        _check_distinct({"sclk": sclk, "mosi": mosi, "miso": miso, "cs_n": cs_n})
        for index, byte in enumerate(responses):
            if not isinstance(byte, int) or isinstance(byte, bool) or not 0 <= byte <= 0xFF:
                raise ValueError(f"responses[{index}] must be a byte, got {byte!r}")
        self.responses: tuple[int, ...] = tuple(responses)
        self.miso_delay = _check_count(miso_delay, "miso_delay")
        self.received: list[int] = []
        self.events: list[tuple[int, str]] = []
        self.errors: list[tuple[int, str]] = []
        self._cycle = 0
        self._next_response = 0
        self._selected = False
        self._out_byte = 0
        self._out_shifted = 0           # falls since the current response byte was loaded (0..7)
        self._in_byte = 0
        self._in_bits = 0
        self._in_undefined = False
        self._prev_cs: str | None = None
        self._prev_sclk: str | None = None
        self._miso_level: int | None = None
        self._pending: list[tuple[int, int]] = []   # (first cycle, MISO level), in cycle order

    def drives(self) -> dict[int, int]:
        return {} if self._miso_level is None else {self.miso: self._miso_level}

    def observe(self, cycle: int, pads: Pads) -> None:
        n = _expect_cycle(self, self._cycle, cycle)
        cs = pads.level(self.cs_n)
        sclk = pads.level(self.sclk)
        if cs not in _DEFINED and cs != self._prev_cs:
            self.errors.append((n, f"CS_n undefined ({cs!r})"))
        if cs != LOW:
            if self._selected:
                self._deselect(n)
            self._pending.clear()
            self._miso_level = None
        elif self._selected:
            if sclk not in _DEFINED and sclk != self._prev_sclk:
                self.errors.append((n, f"SCLK undefined ({sclk!r}) while selected"))
            if self._prev_sclk == LOW and sclk == HIGH:
                self._sample(n, pads.level(self.mosi))
            elif self._prev_sclk == HIGH and sclk == LOW:
                self._shift(n)
        elif self._prev_cs == HIGH:
            self._select(n, sclk)
        elif self._prev_cs != LOW:
            before = "trace start" if self._prev_cs is None else repr(self._prev_cs)
            self.errors.append((n, f"CS_n low after {before}: no fall observed, not selected"))
        self._prev_cs, self._prev_sclk = cs, sclk
        self._cycle = n + 1
        while self._pending and self._pending[0][0] <= self._cycle:
            self._miso_level = self._pending.pop(0)[1]

    def _select(self, n: int, sclk: str) -> None:
        self._selected = True
        self.events.append((n, "select"))
        if self._prev_sclk != LOW or sclk != LOW:
            self.errors.append((n, f"SCLK not low around the CS_n fall ({self._prev_sclk!r} then {sclk!r}); "
                                   "no SCLK edge is processed in this cycle"))
        self._in_byte = self._in_bits = 0
        self._in_undefined = False
        self._load(n)

    def _load(self, n: int) -> None:
        if self._next_response < len(self.responses):
            byte = self.responses[self._next_response]
            self._next_response += 1
        else:
            byte = 0xFF
        self._out_byte = byte
        self._out_shifted = 0
        self.events.append((n, f"load {byte:#04x}"))
        self._pending.append((n + 1 + self.miso_delay, byte >> 7 & 1))

    def _shift(self, n: int) -> None:
        self._out_shifted += 1
        if self._out_shifted == 8:
            self._load(n)
        else:
            bit = self._out_byte >> (7 - self._out_shifted) & 1
            self._pending.append((n + 1 + self.miso_delay, bit))

    def _sample(self, n: int, mosi: str) -> None:
        if mosi in _DEFINED:
            bit = int(mosi)
        else:
            bit = 0
            self._in_undefined = True
            self.errors.append((n, f"MOSI undefined ({mosi!r}) at the SCLK rise"))
        self._in_byte = (self._in_byte << 1 | bit) & 0xFF
        self._in_bits += 1
        if self._in_bits == 8:
            if self._in_undefined:
                self.errors.append((n, "byte with undefined MOSI bits discarded"))
            else:
                self.received.append(self._in_byte)
                self.events.append((n, f"rx {self._in_byte:#04x}"))
            self._in_byte = self._in_bits = 0
            self._in_undefined = False

    def _deselect(self, n: int) -> None:
        self._selected = False
        self.events.append((n, "deselect"))
        if self._in_bits:
            self.errors.append((n, f"deselected after {self._in_bits} MOSI bit(s) of an incomplete byte"))
        self._in_byte = self._in_bits = 0
        self._in_undefined = False


class HandshakeResponder:
    """Generic four-phase request/acknowledge partner: active-low REQ and ACK, optional REQ hold.

    States and timing (n, m: observed cycles):

    * IDLE: REQ observed low in cycle n starts request k (k = 0, 1, ...), whose delay is
      ``ack_delays[k]`` (the last element repeats). A delay of None never acknowledges: the
      device waits for REQ to be observed high and returns to IDLE from the next cycle.
    * PENDING: ACK is driven low from cycle a = n+1+delay. If ``abandon_on_withdraw`` and REQ
      is observed high in a cycle before a, the request is abandoned (no ACK) and the device
      is IDLE from the next cycle; otherwise ACK is asserted at a even if REQ was released
      (a late acknowledge).
    * ASSERTED (from cycle a): ACK driven low; REQ also driven low (open-drain style) during
      cycles a .. a+hold_req_cycles-1 (never beyond the ACK release). REQ observed high in
      cycle m >= a (including m = a for a late acknowledge) releases ACK from cycle
      m+1+release_delay; ACK stays low until then and REQ is not examined. The device is IDLE
      from that release cycle, so a REQ observed low in it starts the next request.

    ``events`` holds (cycle, kind, request index): 'ack_assert' and 'ack_release' at the first
    cycle of the new ACK drive, 'req_hold_end' at the first cycle REQ is no longer held,
    'abandon' at the observed cycle in which REQ was high. ``requests`` lists the cycle each
    request started; ``errors`` logs undefined REQ levels as (cycle, message).
    """

    def __init__(self, *, req: int, ack: int, ack_delays: Sequence[int | None], abandon_on_withdraw: bool = True,
                 release_delay: int = 0, hold_req_cycles: int = 0) -> None:
        self.req = _check_pin(req, "req")
        self.ack = _check_pin(ack, "ack")
        _check_distinct({"req": req, "ack": ack})
        delays = list(ack_delays)
        if not delays:
            raise ValueError("ack_delays needs at least one element")
        for index, delay in enumerate(delays):
            if delay is not None:
                _check_count(delay, f"ack_delays[{index}]")
        self.ack_delays: tuple[int | None, ...] = tuple(delays)
        self.abandon_on_withdraw = bool(abandon_on_withdraw)
        self.release_delay = _check_count(release_delay, "release_delay")
        self.hold_req_cycles = _check_count(hold_req_cycles, "hold_req_cycles")
        self.events: list[tuple[int, str, int]] = []
        self.requests: list[int] = []
        self.errors: list[tuple[int, str]] = []
        self._cycle = 0
        self._state = "IDLE"            # IDLE | PENDING | IGNORING | ASSERTED | RELEASING
        self._request = -1
        self._assert_at = 0
        self._release_at = 0
        self._hold_end = 0
        self._ack_low = False
        self._req_low = False
        self._prev_req: str | None = None

    @property
    def state(self) -> str:
        """State during the current cycle: IDLE, PENDING, IGNORING, ASSERTED or RELEASING."""
        return self._state

    def drives(self) -> dict[int, int]:
        drive: dict[int, int] = {}
        if self._ack_low:
            drive[self.ack] = 0
        if self._req_low:
            drive[self.req] = 0
        return drive

    def observe(self, cycle: int, pads: Pads) -> None:
        n = _expect_cycle(self, self._cycle, cycle)
        req = pads.level(self.req)
        if req not in _DEFINED and req != self._prev_req:
            self.errors.append((n, f"REQ undefined ({req!r})"))
        if self._state == "IDLE":
            if req == LOW:
                self._start_request(n)
        elif self._state == "PENDING":
            if req == HIGH and self.abandon_on_withdraw:
                self.events.append((n, "abandon", self._request))
                self._state = "IDLE"
        elif self._state == "IGNORING":
            if req == HIGH:
                self._state = "IDLE"
        elif self._state == "ASSERTED":
            if req == HIGH:
                self._release_at = n + 1 + self.release_delay
                self._state = "RELEASING"
        # RELEASING: REQ is not examined until ACK has been released.
        self._prev_req = req
        self._cycle = n + 1
        self._enter_cycle(self._cycle)

    def _start_request(self, n: int) -> None:
        self._request += 1
        self.requests.append(n)
        delay = self.ack_delays[min(self._request, len(self.ack_delays) - 1)]
        if delay is None:
            self._state = "IGNORING"
        else:
            self._state = "PENDING"
            self._assert_at = n + 1 + delay

    def _enter_cycle(self, now: int) -> None:
        """Apply the drive changes scheduled to take effect in cycle `now`."""
        if self._state == "PENDING" and now == self._assert_at:
            self._state = "ASSERTED"
            self._ack_low = True
            self.events.append((now, "ack_assert", self._request))
            if self.hold_req_cycles:
                self._req_low = True
                self._hold_end = now + self.hold_req_cycles
        releasing = self._state == "RELEASING" and now == self._release_at
        # The hold covers only cycles of the assertion. With real pads REQ cannot read high while
        # it is held, so a release can only end a hold early when a test feeds inconsistent pads.
        if self._req_low and (now == self._hold_end or releasing):
            self._req_low = False
            self.events.append((now, "req_hold_end", self._request))
        if releasing:
            self._ack_low = False
            self._state = "IDLE"
            self.events.append((now, "ack_release", self._request))


class LowHolder:
    """External open-drain driver: drives `pin` low during each [start, end) window of cycles.

    Windows are absolute cycle numbers on the device's cycle count (0 before any observe
    call), so the holder must be attached from cycle 0. Outside every window the pin is
    released.
    """

    def __init__(self, pin: int, windows: Sequence[tuple[int, int]]) -> None:
        self.pin = _check_pin(pin, "pin")
        checked: list[tuple[int, int]] = []
        for index, window in enumerate(windows):
            start, end = window
            _check_count(start, f"windows[{index}] start")
            _check_count(end, f"windows[{index}] end")
            if end <= start:
                raise ValueError(f"windows[{index}] = {window!r} is empty: need start < end")
            checked.append((start, end))
        self.windows: tuple[tuple[int, int], ...] = tuple(checked)
        self._cycle = 0

    def drives(self) -> dict[int, int]:
        held = any(start <= self._cycle < end for start, end in self.windows)
        return {self.pin: 0} if held else {}

    def observe(self, cycle: int, pads: Pads) -> None:
        self._cycle = _expect_cycle(self, self._cycle, cycle) + 1
