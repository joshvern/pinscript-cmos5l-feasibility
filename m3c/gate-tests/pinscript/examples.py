"""M2 example runs: assemble the example programs, run their encoded words in the
cycle model with modeled external devices and host pacing, and check the pin
traces with the independent observers.

The cycle model knows nothing about these programs; everything protocol-specific
lives here (stimulus) or in observers.py (checks). Host service is modeled as
abstract configuration frames (isa.md section 12 proposal, timing.md
"Configuration link"), not as a pin-level SPI transport.

Run: python -m pinscript.examples [--out reports/m2] [--traces]
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence

from .asm import Program, assemble_file
from .cycle_model import Machine, RunResult
from .devices import HandshakeResponder, LowHolder, SpiPeripheralMode0
from .environment import Environment
from .isa import ISA_VERSION, ProposedHostError, Reason, RunState
from .observers import check_open_drain, decode_uart, edges, monitor_spi_mode0
from .trace import CycleRecord, pad_series, port_series, write_csv, write_jsonl

ROOT = Path(__file__).resolve().parents[2]
PROGRAMS = ROOT / "programs"
CORE_HZ = 10_000_000
NONE = ProposedHostError.NONE


# --------------------------------------------------------------------------- host pacing

@dataclass(frozen=True)
class FrameTiming:
    """Timing of one abstract configuration frame, in core cycles from its CS_n fall.

    With SCLK half-period h (every M1 interval equal to h): first rise at h, rises
    every 2h, the 16th rise at 31h, the 32nd at 63h, then high h, hold h, gap h.
    A read snapshot or write effect is presented 3 cycles after its rising edge
    (2-3 cycles of synchronizer/edge detection plus the strobe; timing.md M1 section).
    """

    half_period: int
    period: int
    snapshot: int
    commit: int

    @classmethod
    def from_half_period(cls, h: int) -> FrameTiming:
        if h < 8:
            raise ValueError("the M1 contract requires at least 8 core cycles per interval")
        return cls(half_period=h, period=66 * h, snapshot=31 * h + 3, commit=63 * h + 3)


MIN_FRAME = FrameTiming.from_half_period(8)      # 528 cycles: fastest legal host
RP_SCRIPT_FRAME = FrameTiming.from_half_period(100)  # 10 us half-periods (bring-up plan): 6600 cycles


@dataclass
class FrameLog:
    start: int
    op: str
    value: int | None = None
    result: str = ""


class PacedHost:
    """Serializes host operations into back-to-back configuration frames.

    `policy(host)` returns the next operation when the port is free, or None to
    idle for one cycle. Operations: ('push', byte), ('read_rx',), ('read_occ',),
    ('start',), ('stop',). A read captures its value at the frame's snapshot
    offset; a push, start, stop, or the pop of a valid RX read is presented as a
    model host event at the commit offset (one event per cycle at most).
    """

    def __init__(self, machine: Machine, timing: FrameTiming,
                 policy: Callable[[PacedHost], tuple | None]) -> None:
        self.machine = machine
        self.timing = timing
        self.policy = policy
        self.frames: list[FrameLog] = []
        self.received: list[int] = []
        self.last_occupancy: tuple[int, int] | None = None
        self.pushed = 0
        self._frame: tuple | None = None
        self._frame_start = 0
        self._snapshot: tuple[bool, int] | None = None

    @property
    def busy(self) -> bool:
        return self._frame is not None

    def tick(self) -> None:
        """Call once per cycle before Machine.step()."""
        now = self.machine.cycle
        if self._frame is None:
            op = self.policy(self)
            if op is None:
                return
            self._frame, self._frame_start, self._snapshot = op, now, None
            self.frames.append(FrameLog(start=now, op=op[0], value=op[1] if len(op) > 1 else None))
        offset = now - self._frame_start
        op = self._frame
        log = self.frames[-1]
        if offset == self.timing.snapshot:
            if op[0] == "read_rx":
                self._snapshot = self.machine.peek_rx()
                log.result = f"valid={int(self._snapshot[0])} byte=0x{self._snapshot[1]:02x}"
            elif op[0] == "read_occ":
                self.last_occupancy = (self.machine.tx_count, self.machine.rx_count)
                log.result = f"tx={self.last_occupancy[0]} rx={self.last_occupancy[1]}"
        if offset == self.timing.commit:
            if op[0] == "push":
                result = self.machine.host_push_tx(op[1])
                if result != NONE:
                    raise AssertionError(f"paced host provoked {result.name} at cycle {now}")
                self.pushed += 1
                log.result = result.name
            elif op[0] == "read_rx" and self._snapshot is not None and self._snapshot[0]:
                result, byte = self.machine.host_pop_rx()
                # Only the host pops RX, so the captured head must still be the head.
                if result != NONE or byte != self._snapshot[1]:
                    raise AssertionError(f"RX pop at {now} returned {result.name} {byte} after snapshot {self._snapshot}")
                self.received.append(byte)
                log.result += " popped"
            elif op[0] == "start":
                log.result = self.machine.host_start().name
            elif op[0] == "stop":
                log.result = self.machine.host_stop().name
        if offset == self.timing.period - 1:
            self._frame = None


def preload(machine: Machine, data: Sequence[int]) -> None:
    """Push bytes before START, one host event per cycle (transport time not modeled)."""
    for byte in data:
        if machine.host_push_tx(byte) != NONE:
            raise AssertionError("preload exceeded the TX FIFO")
        machine.step()


def start(machine: Machine) -> int:
    """Present START in the current cycle and return that cycle index s."""
    s = machine.cycle
    result = machine.host_start()
    if result != NONE:
        raise AssertionError(f"START rejected: {result.name}")
    machine.step()
    return s


# --------------------------------------------------------------------------- helpers

def program_summary(program: Program) -> dict:
    return {
        "source": display_path(Path(program.source_path)),
        "parameters": dict(program.parameters),
        "words": len(program.words),
        "fits_32": program.fits(32),
        "fits_64": program.fits(64),
        "image_sha256": hashlib.sha256(program.to_bin()).hexdigest(),
        "hex": [f"{w:04X}" for w in program.words],
    }


def record_dict(machine: Machine) -> dict | None:
    record = machine.stop_record()
    if record is None:
        return None
    return {"reason": Reason(record.reason).name, "pc": record.pc, "diag_in": record.diag_in,
            "diag_in_defined": record.diag_in_defined, "elapsed": record.elapsed,
            "stop_cycle": record.stop_cycle}


class PinWatcher:
    """Incrementally tracks one pin's pad level from new trace rows (O(1) per cycle)."""

    def __init__(self, machine: Machine, pin: int) -> None:
        self.machine = machine
        self.index = 7 - pin            # CycleRecord.pads lists pins 7..0
        self.seen = 0
        self.level: str | None = None
        self.falls = 0
        self.last_edge = 0

    def update(self) -> PinWatcher:
        trace = self.machine.trace
        while self.seen < len(trace):
            row = trace[self.seen]
            level = row.pads[self.index]
            if self.level is not None and level != self.level:
                self.last_edge = row.cycle
                if self.level == "1" and level == "0":
                    self.falls += 1
            self.level = level
            self.seen += 1
        return self


def run_until(machine: Machine, done: Callable[[], bool], limit: int,
              host: PacedHost | None = None) -> int:
    """Step (ticking the host first) until done() is true; return cycles stepped."""
    for steps in range(limit):
        if done():
            return steps
        if host is not None:
            host.tick()
        machine.step()
    raise AssertionError(f"scenario did not finish within {limit} cycles")


def phases_between(trace: Sequence[CycleRecord], first: int, last: int) -> dict[str, int]:
    counts: dict[str, int] = {}
    for record in trace[first:last + 1]:
        counts[record.phase] = counts.get(record.phase, 0) + 1
    return counts


def display_path(path: Path) -> str:
    """Repository-relative path when inside the repository, else the absolute path."""
    resolved = Path(path).resolve()
    try:
        return str(resolved.relative_to(ROOT))
    except ValueError:
        return str(resolved)


def write_traces(machine: Machine, out: Path | None, name: str) -> dict:
    if out is None:
        return {}
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    jsonl, csv = out / f"{name}.changes.jsonl", out / f"{name}.changes.csv"
    write_jsonl(machine.trace, jsonl, changes_only=True)
    write_csv(machine.trace, csv, changes_only=True)
    return {"trace_jsonl": display_path(jsonl), "trace_csv": display_path(csv),
            "trace_cycles": len(machine.trace)}


# --------------------------------------------------------------------------- UART TX

UART_PIN = 0


def run_uart(bit_cycles: int, data: Sequence[int], *, nominal_baud: int, timing: FrameTiming = MIN_FRAME,
             idle_gap_after: int | None = None, trace_dir: Path | None = None,
             program: Program | None = None) -> dict:
    """UART TX: up to four bytes preloaded; the rest pushed by a frame-paced host that reads
    the proposed occupancy register and then fills only the free entries (TX_FULL impossible).
    `idle_gap_after`: after that many bytes have been supplied the host waits until the TX
    FIFO is empty and the line has idled for two frame times, so PULL stalls with TX high.
    The observer decodes at the true nominal rate (receiver view) and measures edges."""
    program = program or assemble_file(PROGRAMS / "uart_tx.psa", defines={"BIT_CYCLES": bit_cycles})
    env = Environment(pulls={UART_PIN: 1, **{p: 0 for p in range(1, 8)}})
    machine = Machine(program.words, environment=env)
    first, pending = list(data[:4]), list(data[4:])
    watcher = PinWatcher(machine, UART_PIN)
    preload(machine, first)
    s = start(machine)
    frame_cycles = 10 * bit_cycles
    plan: list[tuple] = []
    state = {"supplied": len(first), "gap_done": idle_gap_after is None}

    def policy(h: PacedHost) -> tuple | None:
        if plan:
            return plan.pop(0)
        if not pending:
            return None
        if not state["gap_done"] and state["supplied"] >= idle_gap_after:
            if machine.tx_count == 0 and machine.cycle - watcher.update().last_edge > 2 * frame_cycles:
                state["gap_done"] = True
            else:
                return None
        if h.last_occupancy is not None and h.frames and h.frames[-1].op == "read_occ":
            free = 4 - h.last_occupancy[0]
            h.last_occupancy = None
            limit = len(pending) if state["gap_done"] else min(len(pending), idle_gap_after - state["supplied"])
            for _ in range(min(free, limit)):
                plan.append(("push", pending.pop(0)))
                state["supplied"] += 1
            if plan:
                return plan.pop(0)
        return ("read_occ",)

    host = PacedHost(machine, timing, policy)
    expected = len(data)

    def done() -> bool:
        if pending or plan or host.busy or machine.tx_count:
            return False
        watcher.update()
        return watcher.falls >= expected and machine.cycle - watcher.last_edge > 2 * frame_cycles

    run_until(machine, done, 60 * frame_cycles * (expected + 4), host)
    stop_cycle = machine.cycle
    machine.host_stop()
    machine.step()
    machine.step()
    levels = pad_series(machine.trace, UART_PIN)
    result = decode_uart(levels, cycles_per_bit=CORE_HZ / nominal_baud)
    measured = result.measured_cycles_per_bit
    baud = CORE_HZ / measured if measured else None
    starts = [frame.start_cycle for frame in result.frames]
    summary = {
        "bit_cycles_parameter": bit_cycles,
        "program": program_summary(program),
        "start_presented_cycle": s,
        "host": {"frame_cycles": timing.period, "frames": len(host.frames), "frame_ops": _count_ops(host.frames),
                 "pushed_during_run": host.pushed, "preloaded": len(first)},
        "decoded_bytes": result.bytes,
        "expected_bytes": list(data),
        "bytes_match": result.bytes == list(data),
        "uart_errors": result.errors,
        "receiver_nominal_cycles_per_bit": CORE_HZ / nominal_baud,
        "measured_cycles_per_bit": measured,
        "max_edge_deviation_cycles": result.max_edge_deviation,
        "measured_baud": baud,
        "nominal_baud": nominal_baud,
        "baud_error_percent": (baud - nominal_baud) / nominal_baud * 100 if baud else None,
        "first_start_edge_cycle": starts[0] if starts else None,
        "frame_start_spacing": [b - a for a, b in zip(starts, starts[1:])],
        "idle_before_first_start": result.idle_before_first_start,
        "stalled_cycles": sum(1 for r in machine.trace if r.phase == "STALL"),
        "record": record_dict(machine),
        "stop_requested_cycle": stop_cycle,
    }
    summary.update(write_traces(machine, trace_dir, f"uart_{nominal_baud}"))
    return summary


# --------------------------------------------------------------------------- SPI mode 0

SCLK, MOSI, MISO, CS_N = 0, 1, 2, 3


def spi_environment(responses: Sequence[int], miso_delay: int = 0) -> tuple[Environment, SpiPeripheralMode0]:
    peripheral = SpiPeripheralMode0(sclk=SCLK, mosi=MOSI, miso=MISO, cs_n=CS_N,
                                    responses=list(responses), miso_delay=miso_delay)
    # Pull-ups/downs hold the released lines idle (CS_n high, SCLK/MOSI low);
    # MISO is pulled up so the synchronizer never holds an undefined level.
    pulls = {SCLK: 0, MOSI: 0, MISO: 1, CS_N: 1, 4: 0, 5: 0, 6: 0, 7: 0}
    return Environment(pulls=pulls, devices=[peripheral]), peripheral


def run_spi(tx: Sequence[int], responses: Sequence[int], *, mode: str, timing: FrameTiming = MIN_FRAME,
            miso_delay: int = 0, trace_dir: Path | None = None, program: Program | None = None,
            name: str | None = None) -> dict:
    """One count-prefixed SPI transaction of len(tx) data bytes.

    mode 'preloaded': count + up to 3 data bytes pushed before START; RX drained afterwards.
    mode 'paced': count + 3 bytes preloaded, the rest pushed by a frame-paced host that reads
      the proposed occupancy register, pops RX with one read frame per byte, and fills TX.
    mode 'ideal': a labeled idealized host that pushes or pops in any cycle it legally can
      (not achievable through the configuration port; it shows the engine's own byte rate).
    """
    program = program or assemble_file(PROGRAMS / "spi_mode0.psa")
    env, peripheral = spi_environment(responses, miso_delay)
    machine = Machine(program.words, environment=env)
    stream = [len(tx) % 256, *tx]
    if mode == "preloaded" and len(stream) > 4:
        raise ValueError("a preloaded transaction must fit the four-entry TX FIFO")
    head, pending = stream[:4], list(stream[4:])
    preload(machine, head)
    s = start(machine)
    received: list[int] = []
    host: PacedHost | None = None
    n = len(tx)

    def engine_idle_at_next_txn() -> bool:
        # The program returns to `txn: PULL` (address 2) and stalls on an empty TX FIFO.
        return machine.run_state == RunState.RUN and machine.pc == 2 and machine.tx_count == 0

    if mode == "preloaded":
        run_until(machine, lambda: machine.rx_count == n and engine_idle_at_next_txn(), 400_000)
        while machine.rx_count:
            result, byte = machine.host_pop_rx()
            assert result == NONE and byte is not None
            received.append(byte)
            machine.step()
    elif mode == "ideal":
        def ideal_done() -> bool:
            return len(received) == n and engine_idle_at_next_txn()
        for _ in range(2_000_000):
            if ideal_done():
                break
            if pending and machine.tx_count < 4:
                assert machine.host_push_tx(pending.pop(0)) == NONE
            elif machine.rx_count:
                result, byte = machine.host_pop_rx()
                assert result == NONE and byte is not None
                received.append(byte)
            machine.step()
        else:
            raise AssertionError("ideal SPI run did not finish")
    elif mode == "paced":
        plan: list[tuple] = []

        def policy(h: PacedHost) -> tuple | None:
            if plan:
                return plan.pop(0)
            if len(h.received) == n:
                return None
            if h.last_occupancy is not None and h.frames and h.frames[-1].op == "read_occ":
                tx_occ, rx_occ = h.last_occupancy
                plan.extend(("read_rx",) for _ in range(rx_occ))
                plan.extend(("push", pending.pop(0)) for _ in range(min(4 - tx_occ, len(pending))))
                h.last_occupancy = None
                if plan:
                    return plan.pop(0)
            return ("read_occ",)

        host = PacedHost(machine, timing, policy)
        run_until(machine, lambda: len(host.received) == n and engine_idle_at_next_txn() and not host.busy,
                  20_000_000, host)
        received = list(host.received)
    else:
        raise ValueError(mode)

    end_cycle = machine.cycle
    machine.host_stop()
    machine.step()
    machine.step()
    trace = machine.trace
    sclk, mosi, miso, cs = (pad_series(trace, p) for p in (SCLK, MOSI, MISO, CS_N))
    monitor = monitor_spi_mode0(sclk, mosi, miso, cs)
    transfer = monitor.transfers[0] if monitor.transfers else None
    # Low phases between bytes: the low run before each rise whose index is a multiple of 8.
    rises = [c for c, old, new in edges(sclk) if old == "0" and new == "1"]
    falls = [c for c, old, new in edges(sclk) if old == "1" and new == "0"]
    between = [rises[k] - falls[k - 1] for k in range(8, len(rises), 8)] if len(falls) >= 8 else []
    within = [rises[k] - falls[k - 1] for k in range(1, len(rises)) if k % 8 != 0]
    highs = [falls[k] - rises[k] for k in range(min(len(rises), len(falls)))]
    stall_cycles = [r.cycle for r in trace if r.phase == "STALL"]
    unstable = _edges_during(stall_cycles, (sclk, cs, mosi))
    duration = (transfer.cs_rise - transfer.cs_fall) if transfer and transfer.cs_rise is not None else None
    summary = {
        "mode": mode,
        "label": ("idealized host: pushes/pops whenever legal, not achievable through the configuration port"
                  if mode == "ideal" else "frame-paced host" if mode == "paced" else "preloaded within FIFO depth"),
        "program": program_summary(program),
        "host": ({"frame_cycles": timing.period, "frames": len(host.frames),
                  "frame_ops": _count_ops(host.frames)} if host else None),
        "start_presented_cycle": s,
        "tx_bytes": list(tx),
        "peripheral_received": list(peripheral.received),
        "mosi_ok": list(peripheral.received) == list(tx),
        "responses": list(responses[:n]),
        "host_received": received,
        "miso_ok": received == list(responses[:n]),
        "monitor_errors": monitor.errors if transfer else monitor.errors + ["no transfer seen"],
        "monitor_mosi_bytes": transfer.mosi_bytes if transfer else None,
        "monitor_miso_bytes": transfer.miso_bytes if transfer else None,
        "transfers_seen": len(monitor.transfers),
        "sclk_high_cycles": sorted(set(highs)),
        "sclk_low_within_byte_cycles": sorted(set(within)),
        "sclk_low_between_bytes_cycles": between,
        "active_sclk_hz": (CORE_HZ / (highs[0] + within[0])) if highs and within else None,
        "cs_setup_cycles": transfer.cs_setup if transfer else None,
        "cs_hold_cycles": transfer.cs_hold if transfer else None,
        "mosi_setup_min_cycles": transfer.mosi_setup_min if transfer else None,
        "mosi_hold_min_cycles": transfer.mosi_hold_min if transfer else None,
        "transaction_cycles": duration,
        "payload_bytes_per_s": (n * CORE_HZ / duration) if duration else None,
        "wire_bytes_per_s_at_active_rate": (CORE_HZ / (highs[0] + within[0]) / 8) if highs and within else None,
        "stall_cycles": len(stall_cycles),
        "pin_edges_during_stalls": unstable,
        "record": record_dict(machine),
        "end_cycle": end_cycle,
    }
    summary.update(write_traces(machine, trace_dir, name or f"spi_{mode}"))
    return summary


def _count_ops(frames: Sequence[FrameLog]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for frame in frames:
        counts[frame.op] = counts.get(frame.op, 0) + 1
    return counts


def _edges_during(cycles: Sequence[int], series: Sequence[Sequence[str]]) -> int:
    """Count level changes on the given pad series in stall cycles (vs the previous cycle)."""
    changes = 0
    for c in cycles:
        if c == 0:
            continue
        changes += sum(1 for levels in series if c < len(levels) and levels[c] != levels[c - 1])
    return changes


# --------------------------------------------------------------------------- handshake

REQ, ACK = 4, 5
HANDSHAKE_PULLS = {0: 0, 1: 0, 2: 0, 3: 0, REQ: 1, ACK: 1, 6: 0, 7: 0}


@dataclass
class HandshakeScenario:
    name: str
    description: str
    ack_delays: list[int | None]
    abandon_on_withdraw: bool = True
    release_delay: int = 0
    hold_req_cycles: int = 0
    ack_low_window: tuple[int, int] | None = None   # external holder on ACK, cycles relative to START s
    defines: dict[str, int] = field(default_factory=dict)


def run_handshake(scenario: HandshakeScenario, *, trace_dir: Path | None = None,
                  program: Program | None = None) -> dict:
    program = program or assemble_file(PROGRAMS / "handshake.psa", defines=scenario.defines)
    responder = HandshakeResponder(req=REQ, ack=ACK, ack_delays=scenario.ack_delays,
                                   abandon_on_withdraw=scenario.abandon_on_withdraw,
                                   release_delay=scenario.release_delay,
                                   hold_req_cycles=scenario.hold_req_cycles)
    devices: list = [responder]
    s = 2  # START is presented in cycle 2 (after two idle cycles)
    if scenario.ack_low_window is not None:
        a, b = scenario.ack_low_window
        devices.append(LowHolder(ACK, [(s + a, s + b)]))
    env = Environment(pulls=HANDSHAKE_PULLS, devices=devices)
    machine = Machine(program.words, environment=env)
    machine.step()
    machine.step()
    assert start(machine) == s
    result: RunResult = machine.run(200_000)
    if result.outcome == "stopped":
        # Let the environment settle after the stop (released pins, responder release).
        for _ in range(scenario.hold_req_cycles + 20):
            machine.step()
    trace = machine.trace
    req, ack = pad_series(trace, REQ), pad_series(trace, ACK)
    ports = port_series(trace)
    summary = {
        "scenario": scenario.name,
        "description": scenario.description,
        "program": program_summary(program),
        "start_presented_cycle": s,
        "outcome": result.outcome,
        "violation": (f"{result.violation.kind} pin {result.violation.pin} cycle {result.violation.cycle}"
                      if result.violation else None),
        "record": record_dict(machine),
        "req_edges": edges(req),
        "ack_edges": edges(ack),
        "responder_events": [list(e) for e in responder.events],
        "open_drain_violations": check_open_drain(ports, 1 << REQ),
        "wait_outcomes": _wait_outcomes(trace),
        "waits": _waits(trace),
        "uio_oe_after_stop": [r.uio_oe for r in trace[-3:]],
    }
    summary.update(write_traces(machine, trace_dir, f"handshake_{scenario.name}"))
    return summary


def _waits(trace: Sequence[CycleRecord]) -> list[dict]:
    """One entry per executed WAIT: first and last cycle, PC, budget T at entry, samples
    taken, and how it ended: 'ok' (condition true), 'timeout' (branch) or 'fault'.
    This per-WAIT budget is reported separately from the whole-run ELAPSED."""
    waits: list[dict] = []
    current: dict | None = None
    for row, nxt in zip(trace, trace[1:]):
        if row.phase != "WAIT":
            current = None
            continue
        if current is None or current["pc"] != row.pc:
            current = {"start": row.cycle, "pc": row.pc, "budget_at_entry": row.t}
        outcome = None
        if any(event.startswith("fault") for event in row.events):
            outcome = "fault"
        elif any(event.startswith("timeout") for event in row.events):
            outcome = "timeout"
        elif any(event.startswith("stop") for event in row.events):
            outcome = "stopped"
        elif nxt.pc != row.pc:
            outcome = "ok"
        if outcome is not None:
            waits.append({**current, "end": row.cycle, "samples": row.cycle - current["start"] + 1,
                          "outcome": outcome})
            current = None
    return waits


def _wait_outcomes(trace: Sequence[CycleRecord]) -> list[tuple[int, int, str]]:
    """(last cycle, pc, outcome) of every executed WAIT."""
    return [(w["end"], w["pc"], w["outcome"]) for w in _waits(trace)]


def handshake_scenarios() -> list[HandshakeScenario]:
    n = 100   # ACK_TIMEOUT default
    return [
        HandshakeScenario("fast", "responder acknowledges 5 cycles after observing REQ low", [5]),
        HandshakeScenario("boundary", "ACK first on the pad at c+N-2: accepted on the last sample", [n - 2]),
        HandshakeScenario("one_late", "ACK at c+N-1: timeout, one attempt allowed, USER_1", [n - 1],
                          defines={"ATTEMPTS": 1}),
        HandshakeScenario("late_ack_previous_attempt",
                          "attempt 1 times out; its ACK arrives late and is held 30 cycles after REQ release; "
                          "the idle check waits it out and attempt 2 is answered", [n + 10, 5],
                          abandon_on_withdraw=False, release_delay=30),
        HandshakeScenario("ack_low_before_request", "ACK held low externally until s+60 before any request",
                          [5], ack_low_window=(0, 60)),
        HandshakeScenario("ack_stuck_low", "ACK held low past the idle budget: WAIT_TIMEOUT before any request",
                          [5], ack_low_window=(0, 400)),
        HandshakeScenario("req_held_within_budget", "responder keeps released REQ low; rises on the last sample",
                          [5], hold_req_cycles=50 + 3),
        HandshakeScenario("req_held_too_long", "responder keeps released REQ low one cycle too long: "
                          "WAIT_TIMEOUT with REQ observed low while OUT[REQ]=1", [5], hold_req_cycles=50 + 4),
        HandshakeScenario("no_response", "responder never acknowledges: three attempts then USER_1", [None]),
    ]


# --------------------------------------------------------------------------- driver

UART_BYTES = [0x00, 0xFF, 0x55, 0xA5, 0x01, 0x80, 0x3C, 0xC3]
SPI_TX = [0xA5, 0x3C, 0xFF, 0x00, 0x81, 0x7E, 0x55, 0xAA, 0x12, 0x34, 0x56, 0x78]
SPI_RESPONSES = [0x5A, 0xC3, 0x00, 0xFF, 0x18, 0xE7, 0x96, 0x69, 0xDE, 0xAD, 0xBE, 0xEF]


def run_all(out: Path, *, traces: bool) -> dict:
    trace_dir = out / "traces" if traces else None
    images = out / "programs"
    images.mkdir(parents=True, exist_ok=True)
    assembled = {}
    configs = {
        "uart_tx_115200": ("uart_tx.psa", {"BIT_CYCLES": 87}),
        "uart_tx_9600": ("uart_tx.psa", {"BIT_CYCLES": 1042}),
        "spi_mode0_100k": ("spi_mode0.psa", {}),
        "handshake": ("handshake.psa", {}),
    }
    for name, (source, defines) in configs.items():
        program = assemble_file(PROGRAMS / source, defines=defines)
        prefix = images / name
        prefix.with_suffix(".hex").write_text(program.to_hex())
        prefix.with_suffix(".bin").write_bytes(program.to_bin())
        prefix.with_suffix(".lst").write_text(program.listing_text())
        prefix.with_suffix(".json").write_text(json.dumps(program.to_json(), indent=2) + "\n")
        (images / f"{name}.frames.txt").write_text(program.frames_text())
        assembled[name] = program_summary(program)
    summary = {
        "isa_version": ISA_VERSION,
        "note": "Model-only results: Python cycle model executing assembled words. Not RTL, FPGA, hardware or ASIC evidence.",
        "assembled": assembled,
        "uart": {
            "115200": run_uart(87, UART_BYTES, nominal_baud=115_200, idle_gap_after=6, trace_dir=trace_dir),
            "9600": run_uart(1042, UART_BYTES[:4], nominal_baud=9_600, trace_dir=trace_dir),
        },
        "spi": {
            "preloaded": run_spi(SPI_TX[:3], SPI_RESPONSES, mode="preloaded", trace_dir=trace_dir),
            "paced_min_frame": run_spi(SPI_TX, SPI_RESPONSES, mode="paced", timing=MIN_FRAME,
                                       trace_dir=trace_dir, name="spi_paced_min_frame"),
            "paced_rp_script": run_spi(SPI_TX[:6], SPI_RESPONSES, mode="paced", timing=RP_SCRIPT_FRAME,
                                       trace_dir=trace_dir, name="spi_paced_rp_script"),
            "ideal_host": run_spi(SPI_TX, SPI_RESPONSES, mode="ideal", trace_dir=trace_dir, name="spi_ideal"),
        },
        "handshake": {s.name: run_handshake(s, trace_dir=trace_dir) for s in handshake_scenarios()},
    }
    return summary


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=ROOT / "reports" / "m2")
    parser.add_argument("--traces", action="store_true", help="write change-only JSONL/CSV traces")
    args = parser.parse_args(argv)
    args.out = args.out.resolve()
    summary = run_all(args.out, traces=args.traces)
    args.out.mkdir(parents=True, exist_ok=True)
    path = args.out / "examples-summary.json"
    path.write_text(json.dumps(summary, indent=2, default=_jsonable) + "\n")
    print(f"wrote {display_path(path)}")
    return 0


def _jsonable(value: object) -> object:
    if dataclasses.is_dataclass(value):
        return dataclasses.asdict(value)
    if isinstance(value, Path):
        return str(value)
    raise TypeError(type(value))


if __name__ == "__main__":
    raise SystemExit(main())
