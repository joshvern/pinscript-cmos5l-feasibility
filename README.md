# PinScript M3B CMOS5L feasibility experiment

Isolated source snapshot for three manually dispatched cases: unchanged M1
baseline, separate host/fetch read paths, and a shared read path. All cases use
64 instruction words, the official Tiny Tapeout CMOS5L 6x4 allocation, a 100 ns
core clock constraint, and 60% placement target density. See RTL.md for the
bounded decode/next-PC consumer and its omissions.

This repository is an implementation experiment, not a production execution
engine, ASIC signoff, or hardware test. The baseline has no live fetch consumer.
The two probes use identical consumer/observation logic and differ only in read
port selection. Shared READ_DATA is zero while running; status and STOP remain
available independently. This experiment contract does not change production M1.

The workflow is `.github/workflows/m3b-cmos5l.yaml`, accepts one candidate per
dispatch, and serializes runs. It uses the official CMOS5L GDS action plus its
precheck and functional gate-test actions. There are no automatic push triggers
and no viewer/Pages publication. Standard public `ubuntu-24.04` runners only;
no paid runners or local installation. Action artifacts contain public source,
netlists, layout, reports and resolved tool identities. Source uploads exclude
hardware records, board identifiers, bitstreams and local environments.

Pinned GDS action: `3412659307918422f3f0727917cf9b499aaca588`.
Pinned support tools: `d66cf179e7bc4d296362ab7e2e3b344dc3c4f665`.
LibreLane package: `3.1.0.dev3`; linux/amd64 container manifest pinned to
`sha256:f454022a9bd73f8b2f2208d3cc4a262ad7e95674780b85fa830c8bbc5b8dd542`.
Resolved container identity and tool versions must also be read from each run.
PDK: `ihp-sg13cmos5l`, IHP-Open-PDK
`2bbec755dc67ca3db0261c3d6163e15735d66710`, library `sg13cmos5l_stdcell`.

Report typical, slow and fast nominal-RC corner setup/hold results separately.
The PDK's default violation checker selects typical timing; a green workflow
alone does not prove slow/fast closure. Use actual resolved core geometry and
mapped areas, never generic gate counts as an ASIC area estimate. Retain failed
runs. Functional gate tests are untimed and are not timing simulation.
