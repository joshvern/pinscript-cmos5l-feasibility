"""Pin-only tests usable on RTL or a functional mapped probe netlist."""
import random

import cocotb

from probe_support import SHARED, load, setup


@cocotb.test()
async def two_arbitrary_full_images_and_short_replacement(dut):
    bus = await setup(dut)
    assert await bus.read(0) == 0x4C46
    assert await bus.read(1) == (0xB002 if SHARED else 0xB001)
    assert await bus.read(0x0B) == 64
    seed = 0x4D334B64
    dut._log.info("opaque two-image seed=0x%08x", seed)
    rng = random.Random(seed)
    for image_number in range(2):
        words = [rng.getrandbits(16) for _ in range(64)]
        dut._log.info("image=%d words=%s", image_number, [f"{word:04x}" for word in words])
        await load(bus, words)
        for index, expected in enumerate(words):
            await bus.write(9, index)
            actual = await bus.read(0x0A)
            assert actual == expected, (seed, image_number, index, actual, expected)
    await load(bus, [0xA55A, 0x5AA5])
    for index, expected in ((0, 0xA55A), (1, 0x5AA5), (2, 0), (63, 0)):
        await bus.write(9, index)
        assert await bus.read(0x0A) == expected
    assert int(dut.uio_oe.value) == int(dut.uio_out.value) == 0


@cocotb.test()
async def first_fetch_and_frozen_capture_via_spi(dut):
    bus = await setup(dut)
    await load(bus, [0x1080, 0x0200])  # SET 0x80; HALT
    await bus.write(4, 5)
    assert await bus.read(0x10) == 0x0101  # HALT, stopped, PC 1
    assert await bus.read(0x11) == 0x0200
    assert await bus.read(0x12) == 0x4081  # valid, next PC 1, captured PC 1
    assert await bus.read(0x14) == 0x8000
    assert await bus.read(2) == 1
    assert await bus.read(5) == 0
    assert int(dut.uio_oe.value) == 0
    assert int(dut.uio_out.value) == 0x80
