## How it works

PinScript is a reprogrammable pin-protocol engine. A host loads a program of up to 64 16-bit instructions over a slow SPI mode-0 configuration port, optionally preloads a 4-byte TX FIFO, and starts the engine. The engine executes one instruction per core clock from the loaded image (ISA v0.1: pin set/clear/toggle/drive/release/open-drain, bit shifts through an 8-bit shift register, two 8-bit counters, counted loops, conditional jumps, exact delays, bounded waits on pin or FIFO conditions, and TX/RX FIFO transfers). The same hardware runs different protocols, for example a UART transmitter, an SPI mode-0 controller or a bounded request/acknowledge handshake, purely by loading a different program.

All logic uses the core clock; reset is synchronous and active low. Inputs on the eight programmable pins pass through a two-stage synchronizer. Open-drain pins can never be driven high. Every stop except `HALT HOLD`, and every START and reset, releases all programmable pins. A stop records its reason, program counter, synchronized inputs and an elapsed-cycle count, readable through the register map (interface version 2).

Configuration SCLK, MOSI and CS_n use ui[0], ui[1] and ui[2]; MISO is on uo[0]. uo[1] is high while the engine runs, uo[2] indicates a sticky configuration error, uo[3] indicates RX FIFO data, and uo[4] indicates a stop with a fault reason. MISO is dedicated and remains driven.

## How to test

Run `make test`, `make lint` and `make fpga` locally; see the project status document for actual results. With a running core clock, assert reset through at least four rising edges, then idle CS high/SCLK low for eight cycles. Each configuration high/low period and CS interval must be at least eight core cycles. Send four bytes per CS: READ=0x01 or WRITE=0x02, register address, data high, data low. DEVICE_ID (address 0) reads 0x5053 and VERSION (address 1) reads 0x0002. Load a program with EXPECTED_LENGTH, CONTROL=BEGIN_LOAD, one APPEND_DATA write per word and CONTROL=COMMIT, verify it with READ_ADDRESS/READ_DATA, then write CONTROL=START (5). CONTROL=STOP (6) aborts execution. See the host-interface document for the complete map.

## External hardware

Prototype platform: Tiny Tapeout demo board (RP2350) with the `fabricfox` iCE40UP5K breakout, 3.3 V I/O. Protocol targets connect to the programmable uio pins; lines whose idle level matters need external pull-ups or pull-downs because stopped pins are released. The provisional clock target is 10 MHz; this is not a silicon performance claim.
