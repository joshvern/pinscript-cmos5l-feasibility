"""Independent pad-level protocol observers for the PinScript M2 example programs.

Inputs are per-cycle pad level sequences -- one level per cycle from '0', '1', 'Z', 'X',
given as a string or as a list of one-character strings -- and per-cycle
(uio_out, uio_oe) port values. The observers never import the assembler, the cycle
model, the environment or the trace module, and know nothing about the program that
produced a waveform.

Cycle convention: element t is the level during cycle t. A change between cycles t-1 and
t is an edge *at cycle t* (the first cycle of the new level), and durations are
differences of edge cycles. Every framing or timing anomaly found is reported, with its
cycles, in an ``errors`` list; nothing is skipped silently. Exact timing expectations
(bit period, run lengths, setup/hold) are reported as numbers for the caller to assert.
Results are dataclasses of plain fields (``dataclasses.asdict`` gives JSON-ready data).
"""

from __future__ import annotations

import math
import re
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from fractions import Fraction
from numbers import Real
from typing import Sequence

from .pads import HIGH, LEVELS, LOW

__all__ = [
    "SpiResult", "SpiTransfer", "UartFrame", "UartResult",
    "check_open_drain", "decode_uart", "edges", "monitor_spi_mode0",
]

_DEFINED = (LOW, HIGH)
_UNDEFINED_RUN = re.compile("[ZX]+")


# --------------------------------------------------------------------------- shared helpers

def _as_line(levels: Sequence[str], name: str) -> str:
    """Validate a per-cycle level sequence and return it as one string."""
    if isinstance(levels, str):
        line = levels
    else:
        items = list(levels)
        for index, item in enumerate(items):
            if not isinstance(item, str) or len(item) != 1:
                raise ValueError(f"{name}[{index}] must be one level character, got {item!r}")
        line = "".join(items)
    invalid = set(line).difference(LEVELS)
    if invalid:
        index = next(i for i, level in enumerate(line) if level in invalid)
        raise ValueError(f"{name}[{index}] = {line[index]!r} is not a pad level {LEVELS}")
    return line


def _span(first: int, last: int) -> str:
    return f"cycle {first}" if first == last else f"cycles {first}..{last}"


def _undefined_runs(line: str, start: int = 0, end: int | None = None) -> list[tuple[int, int, str]]:
    """Maximal runs of 'Z'/'X' inside line[start:end] as (first cycle, last cycle, levels)."""
    stop = len(line) if end is None else end
    return [(m.start(), m.end() - 1, m.group()) for m in _UNDEFINED_RUN.finditer(line, start, stop)]


def _describe(levels: str) -> str:
    return "/".join(repr(level) for level in sorted(set(levels)))


def edges(levels: Sequence[str]) -> list[tuple[int, str, str]]:
    """Every level change as (cycle of the new level, old level, new level).

    Changes to and from 'Z' or 'X' are included, so ``'0011Z0'`` gives
    ``[(2, '0', '1'), (4, '1', 'Z'), (5, 'Z', '0')]``.
    """
    line = _as_line(levels, "levels")
    return [(t, line[t - 1], line[t]) for t in range(1, len(line)) if line[t] != line[t - 1]]


# --------------------------------------------------------------------------- UART

@dataclass
class UartFrame:
    """One start edge accepted by the receiver.

    ``bits`` holds every level sampled for the frame in time order: start, data bits LSB
    first, stop (fewer after a false start or at the end of the trace). ``data`` is the
    data value when every data sample was defined -- kept even when the frame has another
    error -- else None. ``error`` joins every problem found in the frame, or is None.
    """

    start_cycle: int
    data: int | None
    bits: list[str]
    stop_ok: bool
    error: str | None


@dataclass
class UartResult:
    frames: list[UartFrame]
    bytes: list[int]                        # data of the frames without error, in order
    errors: list[str]                       # every anomaly, frame errors included
    measured_cycles_per_bit: float | None   # least-squares period of the clean frames' edges
    max_edge_deviation: float | None        # cycles, worst edge vs start + k * measured
    bit_cell_cycles: list[int]              # distinct exact bit lengths, sorted
    idle_before_first_start: int | None     # idle-level cycles just before the first start edge


def _check_period(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"cycles_per_bit must be a number, got {value!r}")
    period = float(value)
    if not math.isfinite(period) or period < 2:
        raise ValueError(f"cycles_per_bit must be finite and at least 2, got {value!r}")
    return period


def _bit_name(k: int, data_bits: int) -> str:
    if k == 0:
        return "start-bit"
    if k == data_bits + 1:
        return "stop-bit"
    return f"data bit {k - 1}"


def decode_uart(levels: Sequence[str], *, cycles_per_bit: float, data_bits: int = 8, idle: str = "1") -> UartResult:
    """Decode an asynchronous serial line like a receiver, and measure its timing from edges.

    Receiver view, using the NOMINAL ``cycles_per_bit`` B. A start edge at cycle s is a change
    from the idle level (in cycle s-1) to the active level (the other defined level) in
    cycle s. Bit k of the frame (0 = start, 1..data_bits = data LSB first, data_bits+1 = stop)
    is sampled in cycle s + floor((k + 1/2) * B). The start sample must read the active level
    (else a false start: the hunt resumes after that sample); a data bit is 1 when it reads
    the idle level (so ``idle='0'`` decodes an inverted line); the stop sample must read the
    idle level. After a frame the hunt for the next start edge resumes after its stop sample.

    Every anomaly goes to ``errors``: undefined levels anywhere on the line; false starts;
    undefined data samples; bad stop bits; frames cut off by the end of the trace; more than
    one level change between two adjacent samples, or any change between the start edge and
    the start sample (a glitch a mid-bit receiver would miss); and the active level outside
    every frame (for example a line that starts low or stays low after a bad stop bit).

    Edge measurement, independent of B: in each frame without error, every change between
    defined levels after the start edge and up to the stop sample is assigned the bit
    boundary k between the two samples it falls between; the period P minimising
    sum((e - s - k P)^2) over all such edges (with the start edges as origins) is
    ``measured_cycles_per_bit``, and the largest |e - s - k P| is ``max_edge_deviation``.
    Edges that are not all on one uniform grid (deviation > 0) are an error, as is a next
    start edge that comes less than data_bits + 2 measured bits after a frame's start (stop
    bit too short). ``bit_cell_cycles`` lists the distinct lengths of in-frame runs between
    consecutive edges divided by the whole number of bits they span, where that division is
    exact. A stop bit followed by idle has no closing edge, so stop-bit length appears only
    through start-to-start spacing.
    """
    line = _as_line(levels, "levels")
    period = _check_period(cycles_per_bit)
    if not isinstance(data_bits, int) or isinstance(data_bits, bool) or not 1 <= data_bits <= 16:
        raise ValueError(f"data_bits must be an integer 1..16, got {data_bits!r}")
    if idle not in _DEFINED:
        raise ValueError(f"idle must be '0' or '1', got {idle!r}")
    active = LOW if idle == HIGH else HIGH
    n = len(line)
    errors = [f"line undefined ({_describe(text)}) in {_span(a, b)}" for a, b, text in _undefined_runs(line)]

    frames: list[UartFrame] = []
    decoded: list[int] = []
    clean: list[tuple[int, list[tuple[int, int]]]] = []    # (start edge, [(k, edge cycle)])
    covered = bytearray(n)
    search = 0
    while True:
        found = line.find(idle + active, search)
        if found < 0:
            break
        start = found + 1
        frame, last_examined, frame_edges = _uart_frame(line, start, period, data_bits, idle, active)
        frames.append(frame)
        covered[start:last_examined + 1] = b"\x01" * (last_examined + 1 - start)
        if frame.error is None:
            assert frame.data is not None
            decoded.append(frame.data)
            clean.append((start, frame_edges))
        else:
            errors.append(f"frame at cycle {start}: {frame.error}")
        search = last_examined          # the next start edge lies after the last sample

    for match in re.finditer(re.escape(active) + "+", line):
        cycle = match.start()
        while cycle < match.end():
            if covered[cycle]:
                cycle += 1
                continue
            first = cycle
            while cycle + 1 < match.end() and not covered[cycle + 1]:
                cycle += 1
            errors.append(f"line at the active level {active!r} outside any frame in {_span(first, cycle)}")
            cycle += 1

    measured = deviation = None
    cells: set[int] = set()
    numerator = sum(k * (e - s) for s, frame_edges in clean for k, e in frame_edges)
    denominator = sum(k * k for _, frame_edges in clean for k, _ in frame_edges)
    if denominator:
        fitted = Fraction(numerator, denominator)       # exact, so a uniform grid gives exactly 0
        worst = max(abs(e - s - k * fitted) for s, frame_edges in clean for k, e in frame_edges)
        measured, deviation = float(fitted), float(worst)
        if worst:
            errors.append(f"in-frame edges are not on one uniform bit grid: an edge lies {float(worst):.3f} "
                          f"cycles from start + k * {float(fitted):.3f}")
        for s, frame_edges in clean:
            previous_k, previous_e = 0, s
            for k, e in frame_edges:
                run, span = e - previous_e, k - previous_k
                if run % span == 0:
                    cells.add(run // span)
                previous_k, previous_e = k, e
        frame_bits = data_bits + 2
        for this, following in zip(frames, frames[1:]):
            gap = following.start_cycle - this.start_cycle
            if this.error is None and gap < frame_bits * fitted:
                errors.append(f"frame at cycle {this.start_cycle}: next start edge {gap} cycles later, before "
                              f"{frame_bits} bits of the measured {float(fitted):g}-cycle period (stop bit too short)")

    idle_before = None
    if frames:
        prefix = line[:frames[0].start_cycle]
        idle_before = len(prefix) - len(prefix.rstrip(idle))
    return UartResult(frames=frames, bytes=decoded, errors=errors, measured_cycles_per_bit=measured,
                      max_edge_deviation=deviation, bit_cell_cycles=sorted(cells),
                      idle_before_first_start=idle_before)


def _uart_frame(line: str, start: int, period: float, data_bits: int, idle: str,
                active: str) -> tuple[UartFrame, int, list[tuple[int, int]]]:
    """Decode the frame whose start edge is at `start` and check its edges.

    Returns the frame, the last cycle the receiver examined (the stop sample, a failed start
    sample, or the end of the trace) and the in-frame edges between defined levels as
    (bit boundary k >= 1, cycle).
    """
    n = len(line)
    points = [start + math.floor((k + 0.5) * period) for k in range(data_bits + 2)]
    bits: list[str] = []
    for k, point in enumerate(points):
        if point >= n:
            problem = f"trace ends before the {_bit_name(k, data_bits)} sample (cycle {point})"
            return UartFrame(start, None, bits, False, problem), n - 1, []
        bits.append(line[point])
        if k == 0 and line[point] != active:
            problem = f"false start: the start bit reads {line[point]!r} at its middle (cycle {point})"
            return UartFrame(start, None, bits, False, problem), point, []

    problems: list[str] = []
    value = 0
    defined = True
    for j in range(data_bits):
        level = bits[1 + j]
        if level not in _DEFINED:
            defined = False
            problems.append(f"undefined level {level!r} at the data bit {j} sample (cycle {points[1 + j]})")
        elif level == idle:
            value |= 1 << j
    stop_ok = bits[-1] == idle
    if not stop_ok:
        problems.append(f"stop bit reads {bits[-1]!r} at its middle (cycle {points[-1]})")

    # Between adjacent sample points a clean frame changes level exactly once where the two
    # bits differ and never otherwise; nothing changes before the start-bit sample.
    frame_edges: list[tuple[int, int]] = []
    previous = start
    for k, point in enumerate(points):
        changes = [c for c in range(previous + 1, point + 1) if line[c] != line[c - 1]]
        if changes and (k == 0 or len(changes) > 1):
            where = ("in the first half of the start bit" if k == 0 else
                     f"between the {_bit_name(k - 1, data_bits)} and {_bit_name(k, data_bits)} samples")
            problems.append(f"{len(changes)} level change(s) {where} (cycles {', '.join(map(str, changes))})")
        if k:
            frame_edges.extend((k, c) for c in changes if line[c - 1] in _DEFINED and line[c] in _DEFINED)
        previous = point
    error = "; ".join(problems) or None
    return UartFrame(start, value if defined else None, bits, stop_ok, error), points[-1], frame_edges


# --------------------------------------------------------------------------- SPI mode 0

@dataclass
class SpiTransfer:
    cs_fall: int                    # first cycle CS_n is low
    cs_rise: int | None             # first cycle CS_n is no longer low; None if the trace ends selected
    mosi_bytes: list[int]           # complete bytes sampled at SCLK rises (bytes with undefined bits omitted)
    miso_bytes: list[int]
    bit_count: int                  # SCLK rising edges while selected
    sclk_high: list[int]            # each complete high run (rise to next fall), in order
    sclk_low: list[int]             # each complete low run (fall to next rise), in order
    cs_setup: int | None            # first rise - CS_n fall
    cs_hold: int | None             # CS_n rise - last fall (None unless SCLK ended low)
    mosi_setup_min: int | None      # min over rises of cycles since the last MOSI change (0 = same cycle)
    mosi_hold_min: int | None       # min over rises of cycles until the next MOSI change
    errors: list[str]


@dataclass
class SpiResult:
    transfers: list[SpiTransfer]
    errors: list[str]               # global errors plus every transfer's errors (prefixed)


def monitor_spi_mode0(sclk: Sequence[str], mosi: Sequence[str], miso: Sequence[str], cs_n: Sequence[str], *,
                      msb_first: bool = True) -> SpiResult:
    """Check an SPI mode-0 bus (CPOL=0, CPHA=0) from per-cycle pad levels.

    A transfer is a maximal run of cycles with CS_n at '0': ``cs_fall`` is its first cycle
    and ``cs_rise`` the first cycle after it. While selected, SCLK rises (a '0' cycle then a
    '1' cycle, the '1' cycle selected) sample MOSI and MISO at the first high cycle; bytes
    are assembled MSB first unless ``msb_first`` is False. Run lengths, CS setup/hold and
    MOSI setup/hold are differences of edge cycles. MOSI changes anywhere in the trace count
    for setup/hold; a rise with no earlier (later) change contributes no setup (hold).

    Errors: CS_n undefined; CS_n already low at cycle 0, falling from or rising to an
    undefined level, or still low at the end of the trace; SCLK not low in both cycles
    around each CS_n edge; SCLK or MOSI undefined while selected; MOSI changing in a
    selected cycle in which SCLK is high (mode 0 changes MOSI only while SCLK is low, so a
    change in the rise cycle -- setup 0 -- is included); MISO undefined at a sample; a bit
    count that is not a multiple of 8; and bytes with undefined bits.
    """
    named = {"sclk": sclk, "mosi": mosi, "miso": miso, "cs_n": cs_n}
    lines = {name: _as_line(sequence, name) for name, sequence in named.items()}
    lengths = {name: len(line) for name, line in lines.items()}
    if len(set(lengths.values())) > 1:
        raise ValueError(f"sclk, mosi, miso and cs_n must have the same number of cycles: {lengths}")
    sclk_line, mosi_line, miso_line, cs_line = lines["sclk"], lines["mosi"], lines["miso"], lines["cs_n"]
    n = len(cs_line)
    errors = [f"CS_n undefined ({_describe(text)}) in {_span(a, b)}" for a, b, text in _undefined_runs(cs_line)]
    mosi_changes = [t for t in range(1, n) if mosi_line[t] != mosi_line[t - 1]]
    transfers: list[SpiTransfer] = []
    for match in re.finditer(LOW + "+", cs_line):
        fall = match.start()
        rise = match.end() if match.end() < n else None
        transfer = _spi_transfer(sclk_line, mosi_line, miso_line, cs_line, fall, rise, mosi_changes, msb_first)
        transfers.append(transfer)
        errors.extend(f"transfer at cycle {fall}: {error}" for error in transfer.errors)
    return SpiResult(transfers=transfers, errors=errors)


def _spi_transfer(sclk: str, mosi: str, miso: str, cs_n: str, fall: int, rise: int | None,
                  mosi_changes: list[int], msb_first: bool) -> SpiTransfer:
    n = len(cs_n)
    end = n if rise is None else rise           # selected cycles: fall .. end-1
    errors: list[str] = []
    if fall == 0:
        errors.append("CS_n already low in cycle 0: its fall was not observed")
    elif cs_n[fall - 1] != HIGH:
        errors.append(f"CS_n fell from the undefined level {cs_n[fall - 1]!r} at cycle {fall}")
    if rise is None:
        errors.append("the trace ends with CS_n low (no CS_n rise)")
    elif cs_n[rise] != HIGH:
        errors.append(f"CS_n rose to the undefined level {cs_n[rise]!r} at cycle {rise}")
    for label, edge in (("fall", fall), ("rise", rise)):
        if edge is None:
            continue
        around = [c for c in (edge - 1, edge) if 0 <= c < n]
        if any(sclk[c] != LOW for c in around):
            levels = ", ".join(f"{sclk[c]!r} in cycle {c}" for c in around)
            errors.append(f"SCLK not low around the CS_n {label} at cycle {edge} ({levels})")
    for name, line in (("SCLK", sclk), ("MOSI", mosi)):
        for a, b, text in _undefined_runs(line, fall, end):
            errors.append(f"{name} undefined ({_describe(text)}) in {_span(a, b)} while selected")

    rises: list[int] = []
    falls: list[int] = []
    order: list[tuple[int, str]] = []
    for t in range(max(fall, 1), end):
        if sclk[t - 1] == LOW and sclk[t] == HIGH:
            rises.append(t)
            order.append((t, HIGH))
        elif sclk[t - 1] == HIGH and sclk[t] == LOW:
            falls.append(t)
            order.append((t, LOW))
    sclk_high: list[int] = []
    sclk_low: list[int] = []
    for (t0, level0), (t1, level1) in zip(order, order[1:]):
        if level0 == HIGH and level1 == LOW:
            sclk_high.append(t1 - t0)
        elif level0 == LOW and level1 == HIGH:
            sclk_low.append(t1 - t0)
        # Two rises (or falls) in a row can only come from an undefined SCLK, reported above.

    for t in rises:
        if miso[t] not in _DEFINED:
            errors.append(f"MISO undefined ({miso[t]!r}) at the SCLK rise in cycle {t}")
    bit_count = len(rises)
    if bit_count % 8:
        errors.append(f"{bit_count} SCLK rising edges: {bit_count % 8} bit(s) after the last whole byte")
    mosi_bytes = _pack([mosi[t] for t in rises], msb_first, "MOSI", errors)
    miso_bytes = _pack([miso[t] for t in rises], msb_first, "MISO", errors)
    for c in mosi_changes[bisect_left(mosi_changes, fall):bisect_left(mosi_changes, end)]:
        if sclk[c] == HIGH:
            errors.append(f"MOSI changed at cycle {c} while SCLK is high")

    setups: list[int] = []
    holds: list[int] = []
    for t in rises:
        index = bisect_right(mosi_changes, t)       # changes at or before t precede the sample
        if index:
            setups.append(t - mosi_changes[index - 1])
        if index < len(mosi_changes):
            holds.append(mosi_changes[index] - t)
    cs_setup = rises[0] - fall if rises else None
    ended_low = bool(falls) and (not rises or falls[-1] > rises[-1])
    cs_hold = rise - falls[-1] if rise is not None and ended_low else None
    return SpiTransfer(cs_fall=fall, cs_rise=rise, mosi_bytes=mosi_bytes, miso_bytes=miso_bytes,
                       bit_count=bit_count, sclk_high=sclk_high, sclk_low=sclk_low, cs_setup=cs_setup,
                       cs_hold=cs_hold, mosi_setup_min=min(setups) if setups else None,
                       mosi_hold_min=min(holds) if holds else None, errors=errors)


def _pack(bits: list[str], msb_first: bool, name: str, errors: list[str]) -> list[int]:
    """Whole bytes from sampled levels; a byte with an undefined bit is omitted and reported."""
    values: list[int] = []
    for index in range(len(bits) // 8):
        chunk = bits[8 * index:8 * index + 8]
        if any(level not in _DEFINED for level in chunk):
            errors.append(f"{name} byte {index} has undefined bits ({''.join(chunk)}) and is omitted")
            continue
        values.append(int("".join(chunk if msb_first else chunk[::-1]), 2))
    return values


# --------------------------------------------------------------------------- open drain

def check_open_drain(ports: Sequence[tuple[int, int]], pin_mask: int) -> list[int]:
    """Cycles in which a pin of `pin_mask` was driven high (uio_oe bit = 1 and uio_out bit = 1).

    ``ports`` holds one (uio_out, uio_oe) pair per cycle. Under isa.md section 4 an
    open-drain pin can never be driven high; a released pin may show uio_out = 1 with
    uio_oe = 0, which is not a violation.
    """
    if not isinstance(pin_mask, int) or isinstance(pin_mask, bool) or not 0 <= pin_mask <= 0xFF:
        raise ValueError(f"pin_mask must be an 8-bit mask, got {pin_mask!r}")
    driven_high: list[int] = []
    for cycle, (uio_out, uio_oe) in enumerate(ports):
        for name, value in (("uio_out", uio_out), ("uio_oe", uio_oe)):
            if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 0xFF:
                raise ValueError(f"ports[{cycle}] {name} must be an 8-bit value, got {value!r}")
        if uio_out & uio_oe & pin_mask:
            driven_high.append(cycle)
    return driven_high
