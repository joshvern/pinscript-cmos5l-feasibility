# M3B live-fetch probe RTL contract

This isolated experiment compares a **64 x 16-bit** program store with separate
and shared combinational read paths. It is a timing/area probe, not the production
execution engine. Production M1 sources, metadata, START behavior and the
hardware-tested bitstream remain unchanged. RTL simulation is not ASIC evidence.

Both Tiny Tapeout tops have the ordinary `ui_in`, `uo_out`, `uio_in`, `uio_out`,
`uio_oe`, `ena`, `clk`, `rst_n` ports:

- `tt_um_pinscript_probe_separate`: independent host and fetch reads.
- `tt_um_pinscript_probe_shared`: one live store read, arbitrated by RUN.

The explicit source list for either top is:

```text
src/pinscript_spi_cfg.v
src/pinscript_program_store.v
experiments/live-fetch/rtl/pinscript_live_fetch_probe.v
```

No `src/project.v`, `src/pinscript_registers.v` or `src/_tt_fpga_top.v` is in the
probe source list. The M1 baseline uses its unchanged four-source production list.
Both probes instantiate the unchanged production SPI and program-store modules.
The same `pinscript_probe_consumer` instance and host debug mux are used in both;
only the parameter `SHARED_READ_PORT` changes their memory wiring. Constraint,
flow, corner and floorplan comparability must be established separately by the
actual ASIC run reports.

## Store, arbitration and observation

Every word is host-loadable with the M1 sequential loader, including all sixteen
bits at all 64 addresses. Memory has no initialization and no array reset. The
store retains its loading/count/committed-valid masking and error handling.

In the separate candidate, `store.read_address` is the registered host index and
`store.fetch_address` is the zero-extended seven-bit PC. Both read results are
observable. During RUN, host READ_DATA still returns the selected host word;
READ_ADDRESS cannot change until stopped. In the shared candidate,
`store.read_address` selects PC while RUN and the host index otherwise. The
unused `store.fetch_address` is tied to zero and its result disconnected from
the consumer and read mux so synthesis may remove that second read cone.
Shared READ_DATA explicitly returns **zero while RUN**, preventing an engine
word from being presented as the host's requested word. Status/debug reads,
STOP and synthetic condition writes remain available through the SPI interface.
This is an experimental interface choice, not an adopted production change.

`fetch_valid = program_valid && (zero_extended_PC < loaded_count)`. The PC is
seven bits, including 64 for full-image fallthrough. The consumer captures the
entire fetch word, matching PC, validity, effective next PC and decoded outcome
every RUN cycle. Captures include a fault or STOP cycle and freeze when stopped;
START clears them. The read mux exposes each bit directly rather than a checksum.
The SPI serializer captures that mux in a register. The output-state register
also drives all eight `uio_out` bits; `uio_oe` is always zero. No `dont_touch`
directive fabricates liveness. The mapped retention audit must still verify
1024 storage bits and surviving read/consumer cones from actual netlists.

The decoder produces at most one store command from each SPI event; START and
a store command are mutually exclusive CONTROL values. In addition, the store
busy input is `run || start_accept`, and store command-valid is masked by
`!start_accept`. Thus no load mutation can share an accepted START edge, even
if the wrapper is later given another command source. While RUN all writes are
BUSY except STOP, CLEAR_ERROR and the synthetic occupancy register. Rejected
commands do not pause execution or alter the image. Reads have no engine effect.

## Consumer subset and timing boundary

The consumer implements these v0.1 encodings with strict reserved-bit checks:

| Instruction | Implemented timing/control effect |
| --- | --- |
| NOP `0100` | Fall through in one cycle. |
| HALT/HALT HOLD `0200`/`0201` | Stop with reason 1; both freeze output state because there is no pad-drive gate. |
| SET/CLR/TGL | Update the registered eight-bit output-state endpoint. Other PIN operations fault. |
| LD R0/R1, immediate | Load either eight-bit counter. MOV and SR destinations fault. |
| JMP | Taken/untaken selection using registered conditions; target is six bits. |
| DJNZ R0/R1 | Modulo-256 decrement and branch on the updated nonzero result. |
| LDT/LDTH | Load twelve timer bits or replace its high nibble. |
| DELAY | Load the timer, hold PC for n additional cycles, finish with timer zero: n+1 cycles total. |
| WAIT | Condition first, then zero-budget timeout target/fault, else decrement and hold. |
| PULL/PUSH and FAULT forms | Synthetic occupancy readiness stalls or faults; an accepted operation advances PC without data movement. |

Condition codes 0..5 implement ALWAYS, NEVER, TXE, TXNE, RXF, RXNF; codes 16..31
test a pin from continuously clocked two-stage synchronization of `uio_in`.
The TX/RX sources are host-written, registered three-bit occupancies constrained
to 0..4. An operation does **not** change them: there are no real FIFO pointers,
payloads, transfer counters or SR. A host condition update takes effect after
the current instruction's decision, using normal registered-state semantics.
Codes 6..9 (SR conditions) and 10..15 (reserved) fault in JMP/WAIT.

Omitted logic: all shifts, SR and MOV paths, user FAULT instruction, OE/OD/drive
gating and physical protocol-output control, real TX/RX storage/service,
ELAPSED/DIAG_IN/production stop record, every protocol controller, and the
proposed production register map. Omitted legal v0.1 instructions fault with
reason 3. This strict subset is documented experiment behavior, not an ISA
revision or a claim of full-engine cycle equivalence. Existing M2 software and
programs remain unchanged.

The priority is synchronous reset, host STOP, invalid/range fetch, illegal
word, then instruction-specific stop or state update. All stopping cycles
freeze PC/output/counters/timer/continuation state; the diagnostic capture still
records the examined word and effective held PC. Reasons are 1 HALT, 2 STOP,
3 illegal, 4 range, 5 WAIT timeout, 6 PULL underflow, 7 PUSH overflow. START is
accepted only when stopped with a committed image; it initializes RUN, PC,
output/counters/timer/continuation and captures at its acceptance edge. The
first instruction commits at the **next** edge. START does not reset input
synchronizers or synthetic occupancies. STOP while already stopped has no
consumer effect. Reset also resets wrapper/loader state and invalidates the
image. A START during a stopping RUN cycle is rejected BUSY.

The representative paths include PC and memory bits through strict decode,
conditional/target selection and PC feedback, counter decrement through branch
selection, timer decrement/zero detection through WAIT/DELAY hold selection,
registered occupancy/pin conditions, count/valid gating, and memory mask bits
through registered output-state updates. Debug capture and SPI readback add
observation overhead to both candidates. Full-engine paths and area can exceed
these measurements; passing this probe does not establish complete-chip fit,
timing closure, synchronizer reliability or protocol behavior.

## Experimental SPI map

The transport is the unchanged M1 mode-0, MSB-first four-byte framing with
eight-core-cycle minimum setup/hold/high/low/gap intervals. `ui_in[0:2]` retain
SCLK/MOSI/CS_n and `uo_out[0]` MISO. `uo_out[1]` RUN, `[2]` sticky host error,
`[3]` program valid, `[4]` loading, `[5]` stopped with reason >=3; `[7:6]` zero.
`ena` and `ui_in[7:3]` are unused. `uio_out` is an observable debug output-state
bus and `uio_oe=0`; no board test or protocol drive is part of this experiment.

| Address | Read | Write |
| --- | --- | --- |
| 00 | `4c46` (LF experiment ID) | READ_ONLY |
| 01 | `b001` separate, `b002` shared | READ_ONLY |
| 02 | M1-format STATUS: valid/loading/RUN/error in bits 0..3 | READ_ONLY |
| 03 | Scratch | Scratch, stopped only |
| 04 | Zero | 1 BEGIN, 2 COMMIT, 3 ABORT, 4 CLEAR_ERROR, 5 START, 6 STOP |
| 05 | First sticky host error | READ_ONLY |
| 06 | Expected length | 1..64, M1 store checks |
| 07 | Loaded count | READ_ONLY |
| 08 | Zero | APPEND_DATA |
| 09 | Host read address | 0..63, stopped only |
| 0a | Host READ_DATA; zero during RUN in shared candidate | READ_ONLY |
| 0b | 64 | READ_ONLY |
| 0c | `{9'b0, rx_count[2:0], 1'b0, tx_count[2:0]}` | Same packing; both <=4, remaining bits zero; permitted during RUN |
| 10 | `{4'b0, reason[3:0], run, pc[6:0]}` | READ_ONLY |
| 11 | Captured complete 16-bit fetch word | READ_ONLY |
| 12 | `{1'b0, captured_valid, captured_next_pc[6:0], captured_pc[6:0]}` | READ_ONLY |
| 13 | Captured decode/control result (below) | READ_ONLY |
| 14 | `{output_state[7:0], sync2[7:0]}` | READ_ONLY |
| 15 | `{r0[7:0], r1[7:0]}` | READ_ONLY |
| 16 | Timer | READ_ONLY |

Unknown addresses read zero and give BAD_ADDRESS on a read strobe. Writes have
M1 error codes and first-error retention, with NO_PROGRAM=13 added for START
without a committed image. CLEAR_ERROR is allowed during RUN. Each read is one
coherent 16-bit snapshot; multiple debug reads while RUN can describe different
cycles. Read while stopped for a coherent group. In particular, shared
READ_DATA is masked at the SPI snapshot edge according to RUN at that edge.

Captured control packs major opcode `[15:12]`, outcome `[11:8]`, condition result
`[7]`, subset-legal `[6]`, pre-edge delay-active `[5]`, stopping `[4]`, and
stop reason `[3:0]`. Condition result is merely decoded when the current opcode
does not use it. Outcomes are 0 idle, 1 fallthrough, 2 JMP taken, 3 JMP untaken,
4 DJNZ taken, 5 DJNZ untaken, 6 DELAY hold, 7 WAIT hold, 8 WAIT timeout,
9 FIFO blocked, 10 HALT, 11 illegal, 12 range, 13 STOP, 14 WAIT success,
15 FIFO accepted. Range/STOP can capture an arbitrary legality/condition result;
their higher-priority outcome/stop-reason and captured-valid fields govern it.

Test hierarchy before synthesis is `top.probe.consumer`; storage is
`top.probe.store.memory`. Functional gate tests must use only TT pins because
mapping/flattening may remove all internal names. No gate test alone is a
post-route timing simulation.

## Reproducible generic structure audit

Run `.venv/bin/python experiments/live-fetch/rtl/audit_structure.py --out
reports/m3b-live-fetch/generic-NEW-RUN` with a new repository-local output
directory. It uses the existing OSS CAD Suite Yosys by default (`--yosys` can
select another existing binary); it installs nothing. It preserves exact
commands, source/script hashes, revision/dirty state, tool/interpreter versions,
exit codes, logs, before-mapping and fully synthesized JSON, and a summary.

The checks require one 1024-bit logical memory; two live read ports before
mapping for separate or one for shared; 1024 distinct mapped storage flop Q
bits; paths from every storage bit to registered full-word capture; PC and
memory feedback into PC; memory-to-output-state register paths; and downstream
observability of every dynamic debug-register bit. The graph stops at flop
boundaries for feedback checks and follows flop D-to-Q for observability.
It is structural connectivity evidence, not formal reachability or timing.
`captured_control[3]` legitimately optimizes to zero because this subset's stop
reasons are 0..7. Counts exclude Yosys `$scopeinfo` metadata and report memory
storage separately from other sequential bits. Generic cell counts are never
reported as foundry cell area.
