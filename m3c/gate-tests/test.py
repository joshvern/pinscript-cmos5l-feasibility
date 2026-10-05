# SPDX-FileCopyrightText: © 2024 Tiny Tapeout
# SPDX-License-Identifier: Apache-2.0
"""Pin-only smoke test for RTL and GATES=yes workflows (interface version 2).

Loads a three-word program through the configuration pins, starts it, and checks the
programmable pins and status outputs, then STOP. Hand-encoded words (docs/isa.md):
SET 0 (0x1001), DRIVE 0 (0x1601), HALT HOLD (0x0201).
"""
import cocotb
from cocotb.clock import Clock
from cocotb.triggers import Timer
from spi_driver import CORE_NS, SpiPins


@cocotb.test()
async def test_project(dut):
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
    assert await bus.read(0) == 0x5053
    assert await bus.read(1) == 2
    await bus.write(3, 0xA55A)
    assert await bus.read(3) == 0xA55A
    assert int(dut.uio_oe.value) == 0 and int(dut.uo_out.value) == 0
    words = [0x1001, 0x1601, 0x0201]
    await bus.write(6, len(words))
    await bus.write(4, 1)
    for word in words:
        await bus.write(8, word)
    await bus.write(4, 2)
    assert await bus.read(2) == 1
    await bus.write(9, 1)
    assert await bus.read(10) == 0x1601
    await bus.write(4, 5)                       # START
    assert await bus.read(5) == 0
    assert await bus.read(0x0E) == (2 << 14) | (1 << 13) | (1 << 8) | 2   # STOPPED, G, HALT, pc 2
    assert int(dut.uio_oe.value) == 0x01 and int(dut.uio_out.value) & 1 == 1, "HALT HOLD drives pin 0 high"
    assert int(dut.uo_out.value) == 0
    await bus.write(4, 6)                       # STOP: releases the held pin
    assert int(dut.uio_oe.value) == 0
    assert await bus.read(5) == 0
