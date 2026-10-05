"""Pin-level host and pad environment for the integrated top (tb.v).

Only top-level ports are used: configuration frames on ui_in[2:0]/uo_out[0], the
status outputs uo_out[4:1], and uio_out/uio_oe/uio_in. No internal signal is read
or written, so these helpers also work on a gate-level netlist.

PadLoop resolves the pads every cycle (falling edge) from uio_out/uio_oe with a
`pinscript.environment.Environment` (pulls + independent device models), drives
uio_in (undefined levels as X, never 0), lets the devices observe, and records
the per-cycle pads and ports for the independent observers. Cycle 0 is the cycle
in which rst_n is released.
"""
from __future__ import annotations

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import FallingEdge, Timer
from cocotb.types import LogicArray

from pinscript.interface import (INTERFACE_V2, Control, EngineControl, EngineRegister, Register,
                                 require_interface_version)
from spi_driver import CORE_NS, SpiPins


def pads_to_logic(pads_text: str) -> LogicArray:
    return LogicArray("".join(level if level in "01" else "X" for level in pads_text))


class PadLoop:
    def __init__(self, dut, env):
        self.dut = dut
        self.env = env
        self.cycle = 0
        self.pads: list[str] = []
        self.ports: list[tuple[int, int]] = []
        self.uo: list[int] = []
        self.contention: list[tuple[int, list[int]]] = []
        self.running_rise: int | None = None
        self._task = None

    def start(self):
        self._task = cocotb.start_soon(self._run())

    def stop(self):
        if self._task is not None:
            self._task.cancel()
            self._task = None

    async def _run(self):
        dut = self.dut
        while True:
            uio_out, uio_oe = int(dut.uio_out.value), int(dut.uio_oe.value)
            pads = self.env.resolve(uio_out, uio_oe)
            bad = self.env.contention_pins(pads)
            if bad:
                self.contention.append((self.cycle, bad))
            dut.uio_in.value = pads_to_logic(str(pads))
            uo = int(dut.uo_out.value)
            if self.running_rise is None and self.uo and not (self.uo[-1] >> 1) & 1 and (uo >> 1) & 1:
                self.running_rise = self.cycle
            self.pads.append(str(pads))
            self.ports.append((uio_out, uio_oe))
            self.uo.append(uo)
            self.env.observe(self.cycle, pads)
            self.cycle += 1
            await FallingEdge(dut.clk)

    def series(self, pin: int, first: int = 0, last: int | None = None) -> list[str]:
        return [pads[7 - pin] for pads in self.pads[first:last]]


class PinHost:
    """Configuration-frame host. Every operation is one complete legal frame."""

    def __init__(self, dut, bus: SpiPins, loop: PadLoop):
        self.dut, self.bus, self.loop = dut, bus, loop
        self.frames: list[dict] = []

    async def _frame(self, kind, address, data=0, **timing):
        start = self.loop.cycle
        if kind == "read":
            value = await self.bus.read(address, **timing)
        else:
            await self.bus.write(address, data, **timing)
            value = None
        self.frames.append({"start": start, "end": self.loop.cycle, "op": kind, "address": address,
                            "data": data, "value": value})
        return value

    async def read(self, address, **timing):
        return await self._frame("read", address, **timing)

    async def write(self, address, data, **timing):
        await self._frame("write", address, data, **timing)

    async def error(self):
        return await self.read(Register.ERROR)

    async def expect_error(self, code, message=""):
        value = await self.error()
        assert value == code, f"{message}: ERROR {value} != {code}"
        if code:
            await self.write(Register.CONTROL, Control.CLEAR_ERROR)

    async def load(self, program, verify=True):
        """Send the assembler's own M1 load-frame sequence, then check ERROR, STATUS,
        LOADED_COUNT and (optionally) every word by readback."""
        for line in program.frames_text().splitlines():
            frame = line.split(";")[0].strip()         # "02060016  ; comment"
            command, address, data = int(frame[0:2], 16), int(frame[2:4], 16), int(frame[4:8], 16)
            assert command == 0x02
            await self.write(address, data)
        assert await self.error() == 0, "load reported an error"
        assert await self.read(Register.STATUS) & 0b0111 == 0b0001, "valid, not loading, not busy"
        assert await self.read(Register.LOADED_COUNT) == len(program.words)
        if verify:
            for address, word in enumerate(program.words):
                await self.write(Register.READ_ADDRESS, address)
                assert await self.read(Register.READ_DATA) == word, f"readback {address}"

    async def push(self, byte):
        await self.write(EngineRegister.TX_DATA, byte)

    async def fifo_status(self):
        value = await self.read(EngineRegister.FIFO_STATUS)
        return value >> 8, value & 0xFF

    async def peek_rx(self):
        value = await self.read(EngineRegister.RX_DATA)
        return bool(value >> 8 & 1), value & 0xFF

    async def pop_rx(self):
        await self.write(Register.CONTROL, EngineControl.RX_POP)

    async def start(self):
        await self.write(Register.CONTROL, Control.START)

    async def stop(self):
        await self.write(Register.CONTROL, EngineControl.STOP)

    async def engine_state(self):
        value = await self.read(EngineRegister.ENGINE_STATE)
        return {"run_state": value >> 14, "g": value >> 13 & 1, "reason": value >> 8 & 0xF, "pc": value & 0x7F}

    async def stop_record(self):
        """Reason, PC, DIAG_IN and ELAPSED (frozen while not RUN: coherent across frames)."""
        state = await self.engine_state()
        od_diag = await self.read(EngineRegister.OD_DIAG)
        high = await self.read(EngineRegister.SR_ELAPSED_HIGH)
        low = await self.read(EngineRegister.ELAPSED_LOW)
        return {**state, "diag_in": od_diag & 0xFF, "elapsed": (high & 0xFF) << 16 | low}


async def setup(dut, env, *, check_version=True):
    """Reset with the clock running, release reset at a falling edge (pad cycle 0)."""
    dut.clk.value = 0
    dut.rst_n.value = 0
    dut.ena.value = 1
    dut.uio_in.value = pads_to_logic(str(env.resolve(0, 0)))
    bus = SpiPins(dut)
    bus.drive()
    cocotb.start_soon(Clock(dut.clk, CORE_NS, unit="ns").start())
    await Timer(10 * CORE_NS, unit="ns")
    await FallingEdge(dut.clk)
    dut.rst_n.value = 1
    loop = PadLoop(dut, env)
    loop.start()
    await bus.idle(10)
    host = PinHost(dut, bus, loop)
    if check_version:
        require_interface_version(await host.read(Register.VERSION), INTERFACE_V2)
    return host, loop
