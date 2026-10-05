# PinScript M3C integrated engine: CMOS5L experiment

Source-only snapshot of the M3C production RTL: SPI configuration interface
(interface version 2), 64 x 16-bit program store with one shared asynchronous
read path, 4-entry TX/RX FIFOs and the ISA v0.1 execution engine. Tiny Tapeout
6x4 tiles, 100 ns core clock constraint, 60% placement target density.

Manual workflow `.github/workflows/m3c-cmos5l.yaml`, one case per dispatch:

- `as-is`: the production `src/config.json` unchanged, plus the report-only
  `STA_EXTRA_CORNER_TCL_FILE` path queries (no constraint change).
- `repair`: the same RTL with focused electrical repairs for causes demonstrated
  in the M3B reports: bounded CTS sink clusters (`CTS_SINK_CLUSTERING_SIZE`),
  long-wire buffering in design repair (`DESIGN_REPAIR_MAX_WIRE_LENGTH`) and
  jumper-only antenna repair (`GRT_/DRT_ANTENNA_REPAIR_JUMPER_ONLY`). No limit is
  raised, no checker disabled, no exception added.

Jobs: official GDS action, precheck, and functional gate tests (UDP models
included; untimed, no SDF). Standard public runners only; no viewer, Pages,
deployments or write permissions. Pinned GDS action
`3412659307918422f3f0727917cf9b499aaca588`, support tools
`d66cf179e7bc4d296362ab7e2e3b344dc3c4f665`, LibreLane `3.1.0.dev3`, container
`sha256:f454022a9bd73f8b2f2208d3cc4a262ad7e95674780b85fa830c8bbc5b8dd542`,
PDK `ihp-sg13cmos5l` (IHP-Open-PDK `2bbec755dc67ca3db0261c3d6163e15735d66710`).

A successful job is not electrical-rule closure or signoff. Report typical,
slow and fast nominal-RC corners separately; the flow's default checker uses
the typical corner and treats max-slew as a warning, and has no fanout checker.
