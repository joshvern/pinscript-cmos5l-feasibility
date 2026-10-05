"""Per-cycle trace records of the PinScript cycle model and small writers.

Trace row n describes cycle n (docs/timing.md, "Cycles, edges and traces"):
registered state during cycle n, the combinational pad controls and resolved
pads during n, the synchronizer stages during n, the host event presented in n
with its result, and the effects committed at the edge e(n+1) ending cycle n.
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass, fields
from os import PathLike
from typing import Iterable, Iterator, Sequence, Union

PathArg = Union[str, PathLike]


@dataclass(frozen=True, slots=True)
class CycleRecord:
    cycle: int
    run_state: str          # 'IDLE' | 'RUN' | 'STOPPED' during this cycle
    phase: str              # 'IDLE' | 'STOPPED' | 'EXEC' | 'DELAY' | 'WAIT' | 'STALL'
    pc: int                 # PC during this cycle (the frozen value while not running)
    word: int | None        # word fetched at pc when RUN and the fetch is valid, else None
    text: str               # model disassembly of `word` ('' if None)
    r0: int
    r1: int
    sr: int
    t: int
    c: int
    out: int
    oe: int
    od: int
    g: int
    uio_out: int
    uio_oe: int
    pads: str               # resolved pads, pins 7..0 ('0', '1', 'Z', 'X')
    sync1: str              # pins 7..0 ('0', '1', 'U')
    sync2: str
    tx_count: int
    rx_count: int
    elapsed: int
    reason: int
    host: str               # host event presented in this cycle and its result, or ''
    events: tuple[str, ...]  # effects committed at the edge ending this cycle

    def pad(self, pin: int) -> str:
        """Pad level of `pin` during this cycle."""
        return self.pads[len(self.pads) - 1 - pin]


FIELD_NAMES: tuple[str, ...] = tuple(field.name for field in fields(CycleRecord))
# Fields that change every running cycle (or every WAIT/DELAY cycle) and therefore
# do not by themselves make a record worth keeping in a changes-only trace.
_VOLATILE = frozenset({"cycle", "elapsed", "t"})
_COMPARED = tuple(name for name in FIELD_NAMES if name not in _VOLATILE)


def _compared(record: CycleRecord) -> tuple[object, ...]:
    return tuple(getattr(record, name) for name in _COMPARED)


def changes_only(records: Iterable[CycleRecord]) -> Iterator[CycleRecord]:
    """Keep the first and last record, every record with a host event or committed
    events, and every record whose fields other than cycle, elapsed and t differ
    from the previously kept record."""
    previous_kept: tuple[object, ...] | None = None
    held: CycleRecord | None = None     # last skipped record; emitted only if it is the final one
    first = True
    for record in records:
        key = _compared(record)
        if first or record.host or record.events or key != previous_kept:
            first = False
            held = None
            previous_kept = key
            yield record
        else:
            held = record
    if held is not None:
        yield held


def _selected(records: Iterable[CycleRecord], changes: bool) -> Iterable[CycleRecord]:
    return changes_only(records) if changes else records


def record_to_dict(record: CycleRecord) -> dict[str, object]:
    row: dict[str, object] = {name: getattr(record, name) for name in FIELD_NAMES}
    row["events"] = list(record.events)
    return row


def record_from_dict(row: dict[str, object]) -> CycleRecord:
    values = {name: row[name] for name in FIELD_NAMES}
    values["events"] = tuple(values["events"])  # type: ignore[arg-type]
    return CycleRecord(**values)  # type: ignore[arg-type]


def write_jsonl(records: Iterable[CycleRecord], path: PathArg, *, changes_only: bool = False) -> None:
    """One JSON object per line, keys in CycleRecord field order; `word` is null when absent."""
    with open(path, "w", encoding="utf-8") as handle:
        for record in _selected(records, changes_only):
            handle.write(json.dumps(record_to_dict(record)) + "\n")


def read_jsonl(path: PathArg) -> list[CycleRecord]:
    with open(path, encoding="utf-8") as handle:
        return [record_from_dict(json.loads(line)) for line in handle if line.strip()]


def write_csv(records: Iterable[CycleRecord], path: PathArg, *, changes_only: bool = False) -> None:
    """Header row of field names; `word` empty when absent; events joined with ';'."""
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(FIELD_NAMES)
        for record in _selected(records, changes_only):
            row = record_to_dict(record)
            row["word"] = "" if record.word is None else record.word
            row["events"] = ";".join(record.events)
            writer.writerow([row[name] for name in FIELD_NAMES])


def pad_series(records: Sequence[CycleRecord], pin: int) -> list[str]:
    """One pad level character per record for `pin`."""
    if not 0 <= pin <= 7:
        raise ValueError("pin must be 0..7")
    index = 7 - pin
    return [record.pads[index] for record in records]


def port_series(records: Sequence[CycleRecord]) -> list[tuple[int, int]]:
    """(uio_out, uio_oe) per record."""
    return [(record.uio_out, record.uio_oe) for record in records]
