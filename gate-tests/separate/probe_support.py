"""Independent pin drivers and cycle observation for the bounded M3B probe.

No assembler, candidate model, or production host encoder is imported. Encoded
instruction literals in tests are checked directly against the written contract.
"""
import os

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge, Timer

from spi_driver import CORE_NS, SpiPins


SHARED = os.environ.get("PINSCRIPT_PROBE_CANDIDATE") == "shared"


async def setup(dut):
    dut.clk.value = 0
    dut.rst_n.value = 0
    dut.ena.value = 1
    dut.uio_in.value = 0
    bus = SpiPins(dut)
    bus.drive()
    cocotb.start_soon(Clock(dut.clk, CORE_NS, unit="ns").start())
    await Timer(10 * CORE_NS, unit="ns")
    dut.rst_n.value = 1
    await bus.idle(10)
    return bus


async def load(bus, words):
    assert 1 <= len(words) <= 64
    await bus.write(4, 4)
    await bus.write(6, len(words))
    await bus.write(4, 1)
    for word in words:
        await bus.write(8, word)
    await bus.write(4, 2)
    assert await bus.read(5) == 0
    assert await bus.read(7) == len(words)
    assert await bus.read(2) == 1


async def tick(dut):
    await RisingEdge(dut.clk)
    await Timer(1, unit="ns")


def snapshot(dut):
    core = dut.probe.consumer
    fields = ("run", "pc", "reason", "output_state", "r0", "r1", "timer",
              "delay_active", "captured_word", "captured_pc", "captured_valid",
              "captured_next_pc", "captured_control")
    return {name: int(getattr(core, name).value) for name in fields}


async def trace_run(dut, max_cycles=20000):
    """Observe START initialization separately, then each executed cycle."""
    for _ in range(max_cycles):
        await tick(dut)
        state = snapshot(dut)
        if state["run"]:
            assert state["pc"] == 0 and state["captured_valid"] == 0
            assert state["r0"] == state["r1"] == state["output_state"] == 0
            break
    else:
        raise AssertionError("START never entered RUN")
    trace = []
    for _ in range(max_cycles):
        await tick(dut)
        state = snapshot(dut)
        trace.append(state)
        if not state["run"]:
            return trace
    raise AssertionError(f"probe did not stop after {max_cycles} cycles; tail={trace[-8:]}")


async def execute(dut, bus):
    task = cocotb.start_soon(trace_run(dut))
    await bus.write(4, 5)
    return await task


def check_trace(trace, words, addresses):
    actual = [row["captured_pc"] for row in trace]
    assert actual == addresses, f"PC trace {actual}, expected {addresses}; full={trace}"
    for row in trace:
        address = row["captured_pc"]
        valid = address < len(words)
        assert row["captured_valid"] == valid, row
        assert row["captured_word"] == (words[address] if valid else 0), row
        assert row["captured_next_pc"] == row["pc"], row


async def check_frozen_debug(bus, row):
    assert await bus.read(0x11) == row["captured_word"]
    assert await bus.read(0x12) == ((row["captured_valid"] << 14)
                                  | (row["captured_next_pc"] << 7)
                                  | row["captured_pc"])
    assert await bus.read(0x13) == row["captured_control"]
    assert await bus.read(0x10) == ((row["reason"] << 8) | (row["run"] << 7) | row["pc"])
    assert await bus.read(0x14) >> 8 == row["output_state"]
    assert await bus.read(0x15) == ((row["r0"] << 8) | row["r1"])
    assert await bus.read(0x16) == row["timer"]
