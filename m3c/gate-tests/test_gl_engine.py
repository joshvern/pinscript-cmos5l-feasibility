"""Short engine executions through the top-level pins only (RTL and GATES=yes).

Kept small for functional gate-level simulation: the M2 UART image (115200, two
preloaded bytes, decoded by the independent observer) and the fast handshake scenario
(stop record read through the register map). No internal signals are accessed.
"""
import cocotb
from cocotb.triggers import Timer

from pin_harness import setup
from pinscript.asm import assemble_file
from pinscript.devices import HandshakeResponder
from pinscript.environment import Environment
from pinscript.observers import check_open_drain, decode_uart
from spi_driver import CORE_NS
from pathlib import Path

PROGRAMS = Path(__file__).resolve().parent / "programs"
if not PROGRAMS.is_dir():
    PROGRAMS = Path(__file__).resolve().parents[1] / "programs"


@cocotb.test()
async def gl_uart_two_bytes(dut):
    env = Environment(pulls={0: 1, **{p: 0 for p in range(1, 8)}})
    host, loop = await setup(dut, env)
    program = assemble_file(PROGRAMS / "uart_tx.psa", defines={"BIT_CYCLES": 87})
    await host.load(program)
    await host.push(0x55)
    await host.push(0xC3)
    await host.start()
    await Timer(87 * 10 * 3 * CORE_NS, unit="ns")
    state = await host.engine_state()
    assert (state["run_state"], state["pc"]) == (1, 3), "stalled at PULL with TX idle high"
    await host.stop()
    result = decode_uart(loop.series(0), cycles_per_bit=10_000_000 / 115_200)
    assert result.bytes == [0x55, 0xC3] and not result.errors, (result.bytes, result.errors)
    assert result.measured_cycles_per_bit == 87 and result.max_edge_deviation == 0
    assert not loop.contention


@cocotb.test()
async def gl_handshake_fast(dut):
    responder = HandshakeResponder(req=4, ack=5, ack_delays=[5])
    env = Environment(pulls={0: 0, 1: 0, 2: 0, 3: 0, 4: 1, 5: 1, 6: 0, 7: 0}, devices=[responder])
    host, loop = await setup(dut, env)
    program = assemble_file(PROGRAMS / "handshake.psa")
    await host.load(program, verify=False)
    await host.start()
    while (await host.engine_state())["run_state"] == 1:
        pass
    record = await host.stop_record()
    # Hand-derived M2 result (docs/status.md): HALT at pc 14, ELAPSED 22, DIAG_IN 0x30.
    assert (record["reason"], record["pc"], record["elapsed"], record["diag_in"]) == (1, 14, 22, 0x30)
    assert check_open_drain(loop.ports, 1 << 4) == [] and not loop.contention
