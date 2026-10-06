# PinScript CMOS5L feasibility runs

This repository is the public sandbox where source snapshots of
[PinScript](https://blog.janestreet.com/protocol-emulator-asic-competition/) run through the
Tiny Tapeout IHP CMOS5L flow on GitHub-hosted runners. PinScript is a small programmable digital
protocol engine for the Jane Street protocol-emulator ASIC competition (6x4 tiles, 100 ns clock).

This is not the working repository. The full project lives in a separate private repository:
RTL, assembler, reference model, protocol programs, FPGA board evidence and analysis tooling. Here
there are only the source-only snapshots the hosted flow needs. They exclude hardware records,
board identifiers, bitstreams and local environments.

## What is here

| Path | Snapshot | Manual workflows |
| --- | --- | --- |
| repository root (`src/`, `cases/`, `test/`, `gate-tests/`, `RTL.md`, `README.md`) | M3B memory, fetch and decode feasibility experiment: an isolated 64-word store with a bounded decode/next-PC consumer, not the production engine. Identity: `SOURCE_MANIFEST.sha256`, `SOURCE_IDENTITY.json` | `m3b-cmos5l.yaml` (candidate baseline, separate or shared), `m3b-gate-replay.yaml` |
| [`m3c/`](../m3c/) | M3C integrated production engine: configuration interface v2, 64 x 16 program store, 4-entry FIFOs, ISA v0.1 engine. Identity: `m3c/SOURCE_MANIFEST.sha256` | `m3c-cmos5l.yaml` (case `cts-only`), `m3c-timing-replay.yaml` |

The root `README.md` describes only the M3B snapshot. It is part of that snapshot's hash manifest,
so it is left unchanged; GitHub shows this file in its place.

## Runs so far

| Run | Workflow and case | Source | GitHub conclusion |
| --- | --- | --- | --- |
| [37261420281](https://github.com/joshvern/pinscript-cmos5l-feasibility/actions/runs/37261420281) | M3B baseline | `b4480cb` | failure |
| [37262506087](https://github.com/joshvern/pinscript-cmos5l-feasibility/actions/runs/37262506087) | M3B separate | `ee989a0` | cancelled |
| [37263012841](https://github.com/joshvern/pinscript-cmos5l-feasibility/actions/runs/37263012841) | M3B separate | `3f2cf41` | failure |
| [37263015069](https://github.com/joshvern/pinscript-cmos5l-feasibility/actions/runs/37263015069) | M3B shared | `3f2cf41` | failure |
| [37265078701](https://github.com/joshvern/pinscript-cmos5l-feasibility/actions/runs/37265078701) | M3B gate replay, baseline | `2c7a67c` | success |
| [37267485584](https://github.com/joshvern/pinscript-cmos5l-feasibility/actions/runs/37267485584) | M3B gate replay, shared | `2c7a67c` | success |
| [37268488122](https://github.com/joshvern/pinscript-cmos5l-feasibility/actions/runs/37268488122) | M3B gate replay, separate | `2c7a67c` | success |
| [37313216627](https://github.com/joshvern/pinscript-cmos5l-feasibility/actions/runs/37313216627) | M3C as-is | `e5580ef` | success |
| [37313220214](https://github.com/joshvern/pinscript-cmos5l-feasibility/actions/runs/37313220214) | M3C repair bundle | `e5580ef` | success |
| [37343495633](https://github.com/joshvern/pinscript-cmos5l-feasibility/actions/runs/37343495633) | M3C cts-only, attempt 1 | `74f50f7` | failure (report hook, before placement) |
| [37347664319](https://github.com/joshvern/pinscript-cmos5l-feasibility/actions/runs/37347664319) | M3C report-only timing replay | `b76736e` | success |
| [37348859159](https://github.com/joshvern/pinscript-cmos5l-feasibility/actions/runs/37348859159) | M3C cts-only, attempt 2 | `b76736e` | success: **adopted** |

A job's color is not the project's verdict. Each run was analyzed afterwards with the project's own
tools.

- **The green as-is run** still had 90 clock-tree fanout violations.
- **The green repair run** still had 3 antenna violations.
- **The adopted run** adds `CTS_SINK_CLUSTERING_SIZE = 6`. It passed the project's strict
  acceptance gate: 2091/2091 checks, with 0 fanout, slew, capacitance and antenna violations,
  setup and hold met at three corners, precheck 9/9 and gate-level tests 3/3.

That gate is an evaluation, not signoff:

- extracted RC at the nominal corner only;
- gate-level tests without timing (no SDF);
- no EQY equivalence or in-flow KLayout DRC/XOR;
- generic I/O constraints;
- no fabricated silicon.

## How runs are made

- **Triggers.** Every workflow is `workflow_dispatch` only: no push triggers and no Pages
  publication. A push to this repository starts nothing.
- **Sources.** Snapshots are uploaded by the project's export tooling. M3C files go under `m3c/`,
  so the M3B snapshot at the root stays byte-identical.
- **Dispatch** needs write access, for example
  `gh workflow run m3c-cmos5l.yaml -f case=cts-only --repo joshvern/pinscript-cmos5l-feasibility`.
  An implementation run takes about 1.5 hours.
- **Artifacts.** GitHub deletes run artifacts after their retention period: the M3B ones on
  2026-10-12, the M3C ones from 2026-11-04. Digest-verified copies of every artifact listed above
  are kept in the project's private evidence archive.

## License

Apache-2.0 ([LICENSE](../LICENSE)), based on the official Tiny Tapeout `cmos5l` template, whose
notices are retained.
