"""Behavioral oracle for register/loader operations, independent of RTL state.

There is deliberately no SPI shifter, cycle engine, or protocol program here.
The model represents only accepted complete operations and transport errors.

`interface_version=1` (default) is the M1 contract. `interface_version=2` adds
the M3C register map for an engine that is never started: an accepted START
raises `ExecutionNotModeled` because execution belongs to the cycle model
(cycle_model.Machine), not to this loader oracle.
"""

from .interface import (INTERFACE_V1, INTERFACE_V2, LAST_REGISTER, Control, EngineControl, EngineError,
                        EngineRegister, Error, Register)


class ExecutionNotModeled(RuntimeError):
    """An accepted interface-version-2 START: this oracle does not execute programs."""


class LoaderModel:
    def __init__(self, depth: int = 64, interface_version: int = INTERFACE_V1):
        if depth not in (32, 64):
            raise ValueError("tested store depths are 32 and 64")
        if interface_version not in (INTERFACE_V1, INTERFACE_V2):
            raise ValueError("interface versions 1 and 2 are modeled")
        self.depth = depth
        self.interface_version = interface_version
        self.memory: dict[int, int] = {}
        self.reset()

    def reset(self) -> None:
        # Contents intentionally survive reset: only control grants visibility.
        self.scratch = 0
        self.expected_length = 0
        self.read_address = 0
        self.loaded_count = 0
        self.loading = False
        self.valid = False
        self.busy = False
        self.error = Error.NONE
        self.tx_count = 0          # version 2 only: TX FIFO occupancy (no engine pops it here)

    def record_error(self, error: int) -> None:
        if self.error == Error.NONE:
            self.error = error

    @property
    def _v2(self) -> bool:
        return self.interface_version == INTERFACE_V2

    def read(self, address: int) -> int:
        values = {
            Register.DEVICE_ID: 0x5053,
            Register.VERSION: self.interface_version,
            Register.STATUS: (int(self.valid) | int(self.loading) << 1 |
                              int(self.busy) << 2 | int(self.error != 0) << 3),
            Register.SCRATCH: self.scratch,
            Register.CONTROL: 0,
            Register.ERROR: self.error,
            Register.EXPECTED_LENGTH: self.expected_length,
            Register.LOADED_COUNT: self.loaded_count,
            Register.APPEND_DATA: 0,
            Register.READ_ADDRESS: self.read_address,
            Register.READ_DATA: (self.memory[self.read_address]
                                 if (self.loading or self.valid) and self.read_address < self.loaded_count
                                 and not (self._v2 and self.busy) else 0),
            Register.PROGRAM_DEPTH: self.depth,
        }
        if self._v2:
            # A never-started engine: IDLE, G = 0, empty RX FIFO, all state zero.
            values.update({int(register): 0 for register in EngineRegister})
            values[EngineRegister.FIFO_STATUS] = self.tx_count << 8
        if address not in values:
            self.record_error(Error.BAD_ADDRESS)
            return 0
        return int(values[address])

    def write(self, address: int, data: int) -> None:
        if not 0 <= data <= 0xFFFF:
            raise ValueError("register data must fit in 16 bits")
        if address == Register.CONTROL and data == Control.CLEAR_ERROR:
            self.error = Error.NONE
            return
        if self.busy:
            self.record_error(Error.BUSY)
            return
        if address > LAST_REGISTER[self.interface_version]:
            self.record_error(Error.BAD_ADDRESS)
        elif address == Register.SCRATCH:
            self.scratch = data
        elif address == Register.EXPECTED_LENGTH:
            if self.loading:
                self.record_error(Error.LENGTH_LOCKED)
            elif not 1 <= data <= self.depth:
                self.record_error(Error.INVALID_LENGTH)
            else:
                self.expected_length = data
        elif address == Register.READ_ADDRESS:
            if data >= self.depth:
                self.record_error(Error.BAD_VALUE)
            else:
                self.read_address = data
        elif address == Register.APPEND_DATA:
            if not self.loading:
                self.record_error(Error.LOAD_STATE)
            elif self.loaded_count == self.expected_length:
                self.record_error(Error.OVERRUN)
            else:
                self.memory[self.loaded_count] = data
                self.loaded_count += 1
        elif address == Register.CONTROL:
            self._control(data)
        elif self._v2 and address == EngineRegister.TX_DATA:
            if data >> 8:
                self.record_error(Error.BAD_VALUE)
            elif self.tx_count == 4:
                self.record_error(EngineError.TX_FULL)
            else:
                self.tx_count += 1
        else:
            self.record_error(Error.READ_ONLY)

    def _control(self, data: int) -> None:
        if data == Control.BEGIN_LOAD:
            if self.loading:
                self.record_error(Error.LOAD_STATE)
            elif not 1 <= self.expected_length <= self.depth:
                self.record_error(Error.INVALID_LENGTH)
            else:
                self.valid, self.loading, self.loaded_count = False, True, 0
        elif data == Control.COMMIT:
            if not self.loading:
                self.record_error(Error.LOAD_STATE)
            elif self.loaded_count != self.expected_length:
                self.record_error(Error.INCOMPLETE)
            else:
                self.valid, self.loading = True, False
        elif data == Control.ABORT:
            self.valid, self.loading, self.loaded_count = False, False, 0
        elif data == Control.START:
            if not self._v2:
                self.record_error(Error.UNSUPPORTED)
            elif self.valid and not self.loading:
                raise ExecutionNotModeled("an accepted START runs the engine; use the cycle model")
            else:
                self.record_error(EngineError.NO_PROGRAM)
        elif self._v2 and data == EngineControl.STOP:
            pass                          # not running: clears G, which is already 0 here
        elif self._v2 and data == EngineControl.RX_POP:
            self.record_error(EngineError.RX_EMPTY)    # no engine ever pushes RX here
        elif self._v2 and data == EngineControl.FIFO_CLEAR:
            self.tx_count = 0
        else:
            self.record_error(Error.BAD_VALUE)
