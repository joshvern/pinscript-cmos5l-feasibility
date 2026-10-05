"""Independent pin-level SPI driver. Does not use the host frame encoder."""

from cocotb.triggers import Timer

CORE_NS = 100


class SpiPins:
    def __init__(self, dut, standalone=False):
        self.dut = dut
        self.standalone = standalone

    def drive(self, cs=1, sclk=0, mosi=0):
        if self.standalone:
            self.dut.spi_cs_n.value = cs
            self.dut.spi_sclk.value = sclk
            self.dut.spi_mosi.value = mosi
        else:
            self.dut.ui_in.value = (cs << 2) | (mosi << 1) | sclk

    def miso(self):
        if self.standalone:
            return int(self.dut.spi_miso.value)
        return int(self.dut.uo_out.value) & 1

    async def idle(self, cycles=8):
        self.drive()
        await Timer(cycles * CORE_NS, unit="ns")

    async def frame(self, command, address, data=0, *, bits=32, extra=0,
                    high=8, low=8, phase=0, gap=8, mutate=None):
        assert high >= 8 and low >= 8 and gap >= 8
        if phase:
            await Timer(phase, unit="ns")
        self.drive(cs=0)
        # The first low interval is also CS setup: exercise its actual minimum.
        if bits + extra == 0:
            await Timer(8 * CORE_NS, unit="ns")
        word = (command << 24) | (address << 16) | data
        received = 0
        for index in range(bits + extra):
            bit = ((word >> (31 - index)) & 1) if index < 32 else 1
            self.drive(cs=0, sclk=0, mosi=bit)
            await Timer(low * CORE_NS, unit="ns")
            self.drive(cs=0, sclk=1, mosi=bit)
            # Sample just after the external rising edge, before synchronized
            # recognition. A mode-0 host requires data already launched.
            await Timer(1, unit="ns")
            received = (received << 1) | self.miso()
            await Timer(high * CORE_NS - 1, unit="ns")
            self.drive(cs=0, sclk=0, mosi=bit)
            if mutate is not None:
                mutate(index)
        await Timer(8 * CORE_NS, unit="ns")
        await self.idle(gap)
        return received

    async def write(self, address, data, **kwargs):
        return await self.frame(2, address, data, **kwargs)

    async def read(self, address, **kwargs):
        response = await self.frame(1, address, **kwargs)
        assert response >> 16 == 0, f"nonzero header {response:08x}"
        return response & 0xFFFF
