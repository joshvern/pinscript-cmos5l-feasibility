# SPDX-FileCopyrightText: © 2024 Tiny Tapeout
# SPDX-License-Identifier: Apache-2.0
"""Pin-only smoke test retained for upstream RTL and GATES=yes workflows."""
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
    assert await bus.read(1) == 1
    await bus.write(3, 0xA55A)
    assert await bus.read(3) == 0xA55A
    await bus.write(6, 1)
    await bus.write(4, 1)
    await bus.write(8, 0x8001)
    await bus.write(4, 2)
    assert await bus.read(2) == 1
    assert await bus.read(10) == 0x8001
    assert int(dut.uio_oe.value) == 0
    assert int(dut.uio_out.value) == 0
    assert int(dut.uo_out.value) == 0
