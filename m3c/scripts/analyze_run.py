#!/usr/bin/env python3
"""Read-only analyzer for one downloaded hosted LibreLane run (M3C, also M3B-compatible).

Usage (stdlib only, deterministic):
    PYTHONDONTWRITEBYTECODE=1 .venv/bin/python scripts/hosted/m3c/analyze_run.py <run_dir> [--out <json>]

<run_dir> follows scripts/hosted/m3c/retrieve.py: artifacts/ (`gh run download` output: one or
more artifact folders and/or artifact zips, or a single artifact extracted directly), logs/
(job logs, optional) and run.json (optional). Files are located by searching path suffixes
(`runs/wokwi/<NN>-<step>/...`, `evidence/*.json`, `results.xml`, ...), never by a fixed
artifact-folder name or step number. Zip members are read in memory; nothing is extracted.
The only file written is the --out JSON (without --out the JSON goes to stdout).

Evidence class: hosted LibreLane flow reports for one run. Not RTL, FPGA, hardware or
signoff evidence; a successful job is not timing or electrical-rule closure.
"""
from __future__ import annotations

import argparse
from collections import Counter, OrderedDict, defaultdict
from decimal import Decimal
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys
import xml.etree.ElementTree as ET
import zipfile

ROOT = Path(__file__).resolve().parents[3]
SCHEMA = "pinscript-hosted-run-analysis/1"
EXPECTED_CORNERS = ("nom_fast_1p32V_m40C", "nom_slow_1p08V_125C", "nom_typ_1p20V_25C")
ESSENTIAL_KEYS = ("DESIGN_NAME", "PDK", "STD_CELL_LIBRARY", "CLOCK_PORT", "CLOCK_PERIOD", "DIE_AREA",
                  "CORE_AREA", "FP_SIZING", "PL_TARGET_DENSITY_PCT", "STA_CORNERS", "DEFAULT_CORNER")
OPTIONAL_KEYS = ("CTS_SINK_CLUSTERING_SIZE", "DESIGN_REPAIR_MAX_WIRE_LENGTH", "GRT_ANTENNA_REPAIR_JUMPER_ONLY",
                 "DRT_ANTENNA_REPAIR_JUMPER_ONLY", "MAX_FANOUT_CONSTRAINT", "STA_EXTRA_CORNER_TCL_FILE")
CONTEXT_KEYS = ("RUN_KLAYOUT_DRC", "RUN_KLAYOUT_XOR", "RUN_MAGIC_DRC", "RUN_LVS",
                "MAX_SLEW_VIOLATION_CORNERS", "MAX_CAP_VIOLATION_CORNERS", "TIMING_VIOLATION_CORNERS",
                "RUN_POST_GRT_DESIGN_REPAIR")
ABSENT = "<absent from resolved.json>"
NUM = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?"
# IHP sg13g2/sg13cmos5l Liberty output-pin names (verified against the typical-corner
# Liberty file: outputs X, Y, Z, Q, Q_N, GCLK, L_HI, L_LO; disjoint from all input names).
OUTPUT_PINS = frozenset({"X", "Y", "Z", "Q", "Q_N", "GCLK", "L_HI", "L_LO"})
FLOP_RE = re.compile(r"_s?df\w*_\d+$")        # dfrbp*, sdf* flip-flops
LATCH_RE = re.compile(r"_dl[hl]\w*_\d+$")     # dlh*/dll* latches (not dlygate delay cells)
BUFFER_RE = re.compile(r"_(?:buf|clkbuf|dlygate)\w*_\d+$")
STEP_RE = re.compile(r"^(\d+)-(.+)$")
PATH_REPORT_RE = re.compile(r"^(?P<prefix>m3[bc])-(?P<label>.+)-(?P<delay>max|min)(?P<points>-points)?\.rpt$")
LOG_LINE_RE = re.compile(r"^(?:(?P<job>[^\t]*)\t(?P<step>[^\t]*)\t)?\ufeff?"
                         r"(?P<ts>\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z) ?(?P<msg>.*)$")
COCOTB_SUMMARY_RE = re.compile(r"\bTESTS=(\d+)\s+PASS=(\d+)\s+FAIL=(\d+)\s+SKIP=(\d+)")
COCOTB_ROW_RE = re.compile(r"\*\*\s+(\S+)\s+(PASS|FAIL|SKIP)\s")
CHECK_RESULTS_RE = re.compile(r"(PASS \S+: \d+ passed, \d+ skipped|PASS expected count: \d+ passed|"
                              r"\d+ passed, \d+ failed, \d+ skipped; nonempty pass required|"
                              r"expected exactly \d+ passed test cases, found \d+|"
                              r"\d+ skipped test case\(s\) with --forbid-skips)")
METRIC_PHYSICAL_KEYS = (
    "route__drc_errors", "antenna__violating__nets", "antenna__violating__pins",
    "route__antenna_violation__count", "antenna_diodes_count", "design__instance__count__class:antenna_cell",
    "magic__drc_error__count", "magic__illegal_overlap__count", "klayout__drc_error__count",
    "design__xor_difference__count", "design__lvs_error__count", "design__lvs_device_difference__count",
    "design__lvs_net_difference__count", "design__lvs_property_fail__count",
    "design__lvs_unmatched_device__count", "design__lvs_unmatched_net__count",
    "design__lvs_unmatched_pin__count", "design__power_grid_violation__count",
    "design__disconnected_pin__count", "design__critical_disconnected_pin__count",
    "timing__drv__floating__nets", "timing__drv__floating__pins", "route__wirelength",
    "global_route__wirelength", "design__violations",
)
METRIC_AREA_KEYS = (
    "design__die__bbox", "design__core__bbox", "design__die__area", "design__core__area",
    "design__instance__count__stdcell", "design__instance__area__stdcell",
    "design__instance__utilization__stdcell", "design__instance__utilization",
    "design__instance__count", "design__instance__area",
    "design__instance__count__class:fill_cell", "design__instance__area__class:fill_cell",
    "design__instance__count__class:sequential_cell", "design__instance__area__class:sequential_cell",
    "design__instance__count__class:timing_repair_buffer", "design__instance__count__hold_buffer",
    "design__instance__count__class:clock_buffer", "design__instance__count__class:clock_inverter",
)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def display(path: Path) -> str:
    path = Path(os.path.abspath(path))
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        return str(path)


def unavailable(reason: str, **extra) -> dict:
    return OrderedDict(status="UNAVAILABLE", reason=reason, **extra)


def exact(value) -> Decimal:
    """Exact decimal for a value as written in JSON/report text (avoids float noise)."""
    return Decimal(repr(value)) if isinstance(value, float) else Decimal(str(value))


def as_number(value: Decimal):
    return int(value) if value == value.to_integral_value() else float(value)


def strict_json(node):
    """Non-finite floats (metrics.json carries `Infinity` for an empty r2r group) become strings."""
    if isinstance(node, float) and not math.isfinite(node):
        return "inf" if node > 0 else ("-inf" if node < 0 else "nan")
    if isinstance(node, dict):
        return OrderedDict((key, strict_json(value)) for key, value in node.items())
    if isinstance(node, list):
        return [strict_json(value) for value in node]
    return node


def step_sort_key(name: str):
    match = STEP_RE.match(name)
    return (int(match[1]) if match else -1, name)


# --------------------------------------------------------------------------- input files ---

class Entry:
    """One file inside artifacts/: a regular file or a zip member (read in memory)."""

    __slots__ = ("kind", "disk", "container", "member", "rel")

    def __init__(self, kind, rel, disk=None, container=None, member=None):
        self.kind, self.rel, self.disk, self.container, self.member = kind, rel, disk, container, member

    @property
    def scope(self) -> str:
        return self.container or ""


class Inputs:
    """Index of artifacts/ plus a registry (path, sha256, bytes, purpose) of every file read."""

    def __init__(self, run_dir: Path):
        self.run_dir = run_dir
        self.base = run_dir / "artifacts"
        self.entries: list[Entry] = []
        self.zips: dict[str, zipfile.ZipFile] = {}
        self.zip_paths: dict[str, Path] = {}
        self.registry: "OrderedDict[str, dict]" = OrderedDict()
        self.runrel_of: dict[str, str] = {}
        if self.base.is_dir():
            self._index()
        self.roots = self._run_roots()

    def _index(self):
        for directory, subdirs, files in os.walk(self.base, followlinks=False):
            subdirs.sort()
            for name in sorted(files):
                path = Path(directory) / name
                rel = path.relative_to(self.base).as_posix()
                if name.lower().endswith(".zip") and zipfile.is_zipfile(path):
                    archive = zipfile.ZipFile(path)
                    self.zips[rel] = archive
                    self.zip_paths[rel] = path
                    for info in sorted(archive.infolist(), key=lambda item: item.filename):
                        if not info.is_dir():
                            self.entries.append(Entry("zip", info.filename, container=rel, member=info.filename))
                elif path.is_file():
                    self.entries.append(Entry("disk", rel, disk=path))

    def _run_roots(self):
        roots: "dict[tuple, dict]" = {}
        tops: "dict[tuple, set]" = defaultdict(set)
        for entry in self.entries:
            match = re.match(r"^(?:(.*)/)?runs/wokwi/(.+)$", entry.rel)
            if match:
                key = (entry.scope, match[1] or "")
                roots.setdefault(key, {})[match[2]] = entry
        for entry in self.entries:
            for key in roots:
                prefix = key[1] + "/" if key[1] else ""
                if entry.scope == key[0] and entry.rel.startswith(prefix):
                    tops[key].add(entry.rel[len(prefix):].split("/", 1)[0])
        # Prefer extracted files over zip members, then the workflow's own artifact (it carries
        # evidence/), then roots that contain final metrics, then name order (deterministic).
        order = sorted(roots, key=lambda key: (key[0] != "", "evidence" not in tops[key],
                                               "final/metrics.json" not in roots[key], key))
        return OrderedDict((key, roots[key]) for key in order)

    @property
    def primary(self):
        return next(iter(self.roots), None)

    def describe_roots(self):
        result = []
        for (scope, prefix), files in self.roots.items():
            result.append(OrderedDict(container=display(self.zip_paths[scope]) if scope else None,
                                      prefix=(prefix + "/" if prefix else "") + "runs/wokwi/",
                                      files=len(files), has_final_metrics="final/metrics.json" in files))
        return result

    def label(self, entry: Entry) -> str:
        if entry.kind == "disk":
            return display(entry.disk)
        return f"{display(self.zip_paths[entry.container])}!{entry.member}"

    def _raw(self, entry: Entry) -> bytes:
        if entry.kind == "disk":
            return entry.disk.read_bytes()
        container = display(self.zip_paths[entry.container])
        if container not in self.registry:
            data = self.zip_paths[entry.container].read_bytes()
            self.registry[container] = OrderedDict(
                path=container, sha256=sha256(data), bytes=len(data),
                purposes=["artifact zip container (members read in memory, never extracted)"])
        return self.zips[entry.container].read(entry.member)

    def register(self, label: str, data: bytes, purpose: str) -> dict:
        record = self.registry.get(label)
        if record is None:
            record = self.registry[label] = OrderedDict(path=label, sha256=sha256(data), bytes=len(data),
                                                        purposes=[])
        if purpose not in record["purposes"]:
            record["purposes"].append(purpose)
        return OrderedDict(path=label, sha256=record["sha256"])

    def read(self, entry: Entry, purpose: str, alternates=()):
        data = self._raw(entry)
        label = self.label(entry)
        ref = self.register(label, data, purpose)
        record = self.registry[label]
        if alternates and "alternate_copies" not in record:
            copies = []
            for other in alternates:
                copies.append(OrderedDict(path=self.label(other), identical=sha256(self._raw(other)) == ref["sha256"]))
            record["alternate_copies"] = copies
        return data, ref

    def read_disk(self, path: Path, purpose: str):
        data = path.read_bytes()
        return data, self.register(display(path), data, purpose)

    # ---- run tree (runs/wokwi) ----
    def run_entry(self, runrel: str):
        found = [files[runrel] for files in self.roots.values() if runrel in files]
        return (found[0], found[1:]) if found else (None, [])

    def run_text(self, runrel: str, purpose: str):
        entry, alternates = self.run_entry(runrel)
        if entry is None:
            return None, None
        data, ref = self.read(entry, purpose, alternates)
        self.runrel_of[ref["path"]] = runrel
        return data.decode("utf-8", errors="replace"), ref

    def run_files(self) -> list[str]:
        names = set()
        for files in self.roots.values():
            names.update(files)
        return sorted(names)

    def steps(self) -> list[str]:
        return sorted({name.split("/", 1)[0] for name in self.run_files()
                       if "/" in name and STEP_RE.match(name.split("/", 1)[0])}, key=step_sort_key)

    def latest_step(self, step_name: str):
        pattern = re.compile(rf"^\d+-{re.escape(step_name)}(?:-\d+)?$")
        matches = [step for step in self.steps() if pattern.match(step)]
        return matches[-1] if matches else None

    # ---- other artifact files ----
    def find(self, regex: str) -> list:
        """Entries outside runs/wokwi matching regex (search on the artifact-relative path),
        best copy first: same artifact as the primary run root, extracted before zip, name order."""
        pattern = re.compile(regex)
        primary = self.primary
        hits = []
        for entry in self.entries:
            if "runs/wokwi/" in entry.rel:
                continue
            match = pattern.search(entry.rel)
            if match:
                prefix = entry.rel[:match.start()].rstrip("/")
                hits.append(((entry.scope, prefix) != primary, entry.scope != "", entry.scope, entry.rel, entry))
        return [hit[-1] for hit in sorted(hits, key=lambda hit: hit[:4])]


# ------------------------------------------------------------------------------- parsers ---

def parse_paths(text: str) -> list:
    """OpenSTA report_checks full paths: startpoint/endpoint/group/type/arrival/required/slack."""
    result = []
    for block in text.split("Startpoint:")[1:]:
        lines = block.splitlines()
        row = OrderedDict(startpoint=lines[0].strip() if lines else None)
        for key, pattern in (("endpoint", r"^Endpoint: (.+)$"), ("path_group", r"^Path Group: (.+)$"),
                             ("path_type", r"^Path Type: (.+)$")):
            match = re.search(pattern, block, re.M)
            row[key] = match[1].strip() if match else None
        for key, pattern in (("data_arrival_ns", rf"^\s*({NUM})\s+data arrival time"),
                             ("data_required_ns", rf"^\s*({NUM})\s+data required time")):
            match = re.search(pattern, block, re.M)
            row[key] = float(match[1]) if match else None
        match = re.search(rf"^\s*({NUM})\s+slack \((\w+)\)", block, re.M)
        row["slack_ns"] = float(match[1]) if match else None
        row["slack_state"] = match[2] if match else None
        result.append(row)
    return result


def worst_path_summary(text: str) -> dict:
    paths = parse_paths(text)
    if not paths:
        reason = "No paths found." if "No paths found." in text else "no 'Startpoint:' in report"
        return unavailable(reason)
    with_slack = [path for path in paths if path["slack_ns"] is not None]
    worst = min(with_slack, key=lambda path: path["slack_ns"]) if with_slack else paths[0]
    result = OrderedDict(status="AVAILABLE", paths_in_report=len(paths),
                         first_path_is_worst=worst is paths[0])
    result.update(worst)
    return result


def parse_corner_value(text: str) -> dict:
    return {match[1]: float(match[2]) for match in re.finditer(rf"^(\S+):\s*({NUM})\s*$", text, re.M)}


def parse_summary_table(text: str) -> dict:
    lines = [line.strip() for line in text.splitlines()]
    columns = None
    for line in lines:
        if line.startswith("┃") and line.endswith("┃"):
            cells = [cell.strip() for cell in line[1:-1].split("┃")]
            columns = cells if columns is None else [f"{a} {b}".strip() for a, b in zip(columns, cells)]
    if columns:
        # Hold and setup groups both have a "Reg to Reg Paths" column; keep both.
        seen = Counter()
        unique = []
        for column in columns:
            seen[column] += 1
            unique.append(column if seen[column] == 1 else f"{column} #{seen[column]}")
        columns = unique
    rows = OrderedDict()
    for line in lines:
        if line.startswith("│") and line.endswith("│") and columns:
            cells = [cell.strip() for cell in line[1:-1].split("│")]
            rows[cells[0]] = OrderedDict(zip(columns[1:], cells[1:]))
    return OrderedDict(columns=columns or [], rows=rows)


ROW_FLOAT = re.compile(rf"^(\S+)\s+({NUM})\s+({NUM})\s+({NUM})\s+\((VIOLATED|MET)\)\s*$")
ROW_FANOUT = re.compile(r"^(\S+)\s+(\d+)\s+(\d+)\s+(-?\d+)?\s*\((VIOLATED|MET)\)\s*$")


def parse_checks(text: str) -> dict:
    """checks.rpt: report_check_types -max_slew -max_cap -max_fanout -violators, counts, misc."""
    start = text.find("report_check_types -max_slew -max_cap -max_fanout -violators")
    end = text.find("report_parasitic_annotation", start) if start >= 0 else -1
    segment = text[start:end if end > 0 else None] if start >= 0 else ""
    rows = OrderedDict((kind, []) for kind in ("max_slew", "max_capacitance", "max_fanout"))
    unparsed = []
    section = None
    for line in segment.splitlines():
        stripped = line.strip()
        if stripped in ("max slew", "max capacitance", "max fanout"):
            section = stripped.replace(" ", "_")
            continue
        if section is None or "(VIOLATED)" not in stripped:
            continue
        if section == "max_fanout":
            match = ROW_FANOUT.match(stripped)
            if match:
                limit, value = int(match[2]), int(match[3])
                printed = int(match[4]) if match[4] is not None else None
                rows[section].append(OrderedDict(
                    pin=match[1], value=value, limit=limit,
                    slack=printed if printed is not None else limit - value,
                    slack_basis="report" if printed is not None else "computed limit - value (blank in report)"))
                continue
        else:
            match = ROW_FLOAT.match(stripped)
            if match:
                rows[section].append(OrderedDict(pin=match[1], value=float(match[3]), limit=float(match[2]),
                                                 slack=float(match[4]), slack_basis="report"))
                continue
        unparsed.append(line.rstrip())
    counts = OrderedDict()
    for kind, pattern in (("max_slew", r"max slew violation count (\d+)"),
                          ("max_capacitance", r"max cap violation count (\d+)"),
                          ("max_fanout", r"max fanout violation count (\d+)")):
        match = re.search(pattern, text)
        counts[kind] = int(match[1]) if match else None
    unannotated = re.search(r"Found (\d+) unannotated drivers", text)
    partially = re.search(r"Found (\d+) partially unannotated drivers", text)
    return OrderedDict(
        section_found=start >= 0, rows=rows, unparsed_violation_lines=unparsed, count_lines=counts,
        unannotated_drivers=int(unannotated[1]) if unannotated else None,
        partially_unannotated_drivers=int(partially[1]) if partially else None,
        check_setup_output_lines=section_body(text, "check_setup -verbose"),
        slack_max_minus_0p01_lines=section_body(text, "report_checks --slack_max -0.01"))


def section_body(text: str, header: str):
    """Non-empty lines of a LibreLane '=====' delimited report section (None if header absent)."""
    marker = text.find(header)
    if marker < 0:
        return None
    lines = text[marker:].splitlines()[1:]
    index = 0
    while index < len(lines) and (lines[index].startswith("=") or not lines[index].strip()):
        index += 1
    body = []
    for line in lines[index:]:
        if line.startswith("====="):
            break
        if line.strip():
            body.append(line.rstrip())
    return body


class Netlist:
    """Flat gate-level Verilog (LibreLane write_verilog). Pin direction from IHP pin names."""

    def __init__(self, text: str):
        text = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
        text = re.sub(r"//[^\n]*", "", text)
        self.cell: dict = {}
        self.pins: dict = {}
        self.nets: dict = defaultdict(list)
        self.inputs: set = set()
        self.outputs: set = set()
        self.assigned_outputs: dict = defaultdict(list)
        for statement in text.split(";"):
            s = statement.strip()
            if not s or s.startswith(("module", "endmodule", "wire ", "wire\t", "reg ")):
                continue
            match = re.match(r"^(input|output|inout)\s+(?:wire\s+)?(?:\[(\d+):(\d+)\]\s*)?(.+)$", s, re.S)
            if match:
                for name in (part.strip().lstrip("\\") for part in match[4].split(",")):
                    bits = ([f"{name}[{i}]" for i in range(min(int(match[2]), int(match[3])),
                                                           max(int(match[2]), int(match[3])) + 1)]
                            if match[2] is not None else [name])
                    (self.inputs if match[1] == "input" else self.outputs).update(bits)
                continue
            match = re.match(r"^assign\s+(\\\S+|\S+)\s*=\s*(\\\S+|\S+)$", s)
            if match:
                self.assigned_outputs[match[2].lstrip("\\")].append(match[1].lstrip("\\"))
                continue
            match = re.match(r"^(\S+)\s+(\\\S+|\S+)\s*\((.*)\)$", s, re.S)
            if not match:
                continue
            inst = match[2].lstrip("\\")
            self.cell[inst] = match[1]
            connections = {}
            for pin in re.finditer(r"\.(\w+)\s*\(\s*(\\\S+|[^\s()]*)\s*\)", match[3]):
                net = pin[2].lstrip("\\")
                if net:
                    connections[pin[1]] = net
                    self.nets[net].append((inst, pin[1]))
            self.pins[inst] = connections

    def net_of(self, inst: str, pin: str):
        return self.pins.get(inst, {}).get(pin)

    def drivers(self, net: str) -> list:
        found = [(inst, pin) for inst, pin in self.nets.get(net, []) if pin in OUTPUT_PINS]
        if not found and net in self.inputs:
            found = [("PORT", net)]
        return found

    def port_loads(self, net: str) -> list:
        return ([net] if net in self.outputs else []) + self.assigned_outputs.get(net, [])

    def load_kind(self, inst: str) -> str:
        cell = self.cell.get(inst, "")
        if inst.startswith("clkload"):
            return "cts_dummy_load"
        if "antenna" in cell:
            return "antenna_diode"
        if FLOP_RE.search(cell) or LATCH_RE.search(cell):
            return "sequential"
        if BUFFER_RE.search(cell):
            return "buffer"
        return "logic"


# ------------------------------------------------------------------------------ sections ---

def resolve_metrics(inputs: Inputs):
    text, ref = inputs.run_text("final/metrics.json", "final aggregated metrics")
    if text is not None:
        return json.loads(text), ref, "final/metrics.json"
    for step in reversed(inputs.steps()):
        text, ref = inputs.run_text(f"{step}/state_out.json", "latest step state (metrics fallback)")
        if text is not None:
            metrics = json.loads(text).get("metrics")
            if metrics:
                return metrics, ref, f"{step}/state_out.json:metrics (final/metrics.json absent)"
    return None, None, None


def section_identity(inputs: Inputs, run_dir: Path) -> dict:
    out = OrderedDict()
    sources = []
    run_json = run_dir / "run.json"
    if run_json.is_file():
        data, ref = inputs.read_disk(run_json, "run metadata (gh run view --json)")
        sources.append(ref)
        run = json.loads(data)
        out["run"] = OrderedDict((key, run.get(key)) for key in (
            "databaseId", "displayTitle", "status", "conclusion", "headSha", "createdAt", "updatedAt", "url"))
        out["jobs"] = [OrderedDict(
            name=job.get("name"), databaseId=job.get("databaseId"), status=job.get("status"),
            conclusion=job.get("conclusion"), startedAt=job.get("startedAt"), completedAt=job.get("completedAt"),
            steps=[OrderedDict(name=step.get("name"), conclusion=step.get("conclusion"))
                   for step in job.get("steps", [])]) for job in run.get("jobs", [])]
    else:
        out["run"] = unavailable("run.json absent from run_dir")
        out["jobs"] = unavailable("run.json absent from run_dir")

    # Hosted evidence written by collect_evidence.py (search, prefer same artifact as run tree).
    evidence = OrderedDict()
    seen = set()
    for entry in inputs.find(r"(?:^|/)evidence/[^/]+\.json$"):
        name = entry.rel.rsplit("/", 1)[-1]
        if name in seen or name == "artifact-sha256.json":
            continue
        seen.add(name)
        data, ref = inputs.read(entry, "hosted evidence record")
        sources.append(ref)
        try:
            evidence[name] = json.loads(data)
        except ValueError:
            evidence[name] = None
    if evidence:
        identity = evidence.get("run-identity.json") or {}
        out["hosted_run_identity"] = OrderedDict((key, identity.get(key)) for key in (
            "GITHUB_REPOSITORY", "GITHUB_SHA", "GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT", "CANDIDATE",
            "IMPLEMENTATION_OUTCOME", "PDK", "ImageOS", "ImageVersion"))
        out["revisions"] = OrderedDict(
            (key.replace("-revision.json", ""), (evidence[key] or {}).get("output", "").strip() or None)
            for key in sorted(evidence) if key.endswith("-revision.json"))
        tools = OrderedDict()
        for key in sorted(evidence):
            match = re.match(r"^tool-\d+-(\w+)\.json$", key)
            if match and evidence[key]:
                tools[match[1]] = (evidence[key].get("output") or "").strip() or evidence[key].get("unavailable")
        out["tool_versions"] = tools or unavailable("no evidence/tool-*.json records")
        digests = []
        for key in sorted(evidence):
            if key.startswith("librelane-image-") and evidence[key]:
                try:
                    images = json.loads(evidence[key].get("output") or "[]")
                except ValueError:
                    images = []
                for image in images:
                    digests.extend(image.get("RepoDigests") or [])
        out["librelane_image_digests"] = sorted(set(digests)) or unavailable("no librelane-image evidence")
        packages = (evidence.get("python-packages.json") or {}).get("output", "")
        match = re.search(r"^librelane==(\S+)$", packages, re.M)
        out["librelane_python_package"] = match[1] if match else unavailable("not in python-packages evidence")
        out["evidence_resolved_process_invalid"] = (evidence.get("resolved-process.json") or {}).get("invalid")
    else:
        out["hosted_run_identity"] = unavailable("no evidence/*.json in artifacts")
        out["tool_versions"] = unavailable("no evidence/*.json in artifacts")

    text, ref = inputs.run_text("final/commit_id.json", "flow commit identity")
    if text is not None:
        sources.append(ref)
        out["final_commit_id"] = json.loads(text)

    text, ref = inputs.run_text("resolved.json", "resolved flow configuration")
    if text is None:
        out["resolved_config"] = unavailable("runs/wokwi/resolved.json not found in any artifact")
    else:
        sources.append(ref)
        resolved = json.loads(text)
        config = OrderedDict((key, resolved.get(key, ABSENT)) for key in ESSENTIAL_KEYS)
        config["librelane_version (meta)"] = (resolved.get("meta") or {}).get("librelane_version", ABSENT)
        config["optional_keys"] = OrderedDict((key, resolved.get(key, ABSENT)) for key in OPTIONAL_KEYS)
        config["context_keys"] = OrderedDict((key, resolved.get(key, ABSENT)) for key in CONTEXT_KEYS)
        checks = []
        for key, expected in (("PDK", "ihp-sg13cmos5l"), ("STD_CELL_LIBRARY", "sg13cmos5l_stdcell"),
                              ("CLOCK_PERIOD", 100), ("STA_CORNERS", list(EXPECTED_CORNERS))):
            actual = resolved.get(key)
            if key == "STA_CORNERS" and isinstance(actual, list):
                actual = sorted(actual)
            checks.append(OrderedDict(key=key, expected=expected, actual=actual,
                                      result="MATCH" if actual == expected else "MISMATCH"))
        config["expected_flow_checks"] = checks
        out["resolved_config"] = config
        copies = inputs.find(r"(?:^|/)tt_submission/resolved\.json$")
        if copies:
            data, copy_ref = inputs.read(copies[0], "tt_submission resolved.json (cross-check)")
            sources.append(copy_ref)
            out["tt_submission_resolved_identical"] = sha256(data) == ref["sha256"]

    pdk = OrderedDict()
    for entry in inputs.find(r"(?:^|/)evidence/artifact-sha256\.json$")[:1]:
        data, ref = inputs.read(entry, "hosted sha256 manifest (integrity cross-check)")
        sources.append(ref)
        manifest = json.loads(data)
        out["_manifest"] = manifest
        for key in sorted(manifest):
            if re.search(r"/libs\.ref/[^/]*stdcell/lib/[^/]+\.lib$", key):
                pdk[key] = manifest[key]
    out["pdk_stdcell_liberty_sha256 (hosted)"] = pdk or unavailable("no evidence/artifact-sha256.json")
    out["sources"] = sources
    return out


def section_area(inputs: Inputs, metrics, metrics_ref, design, corners) -> dict:
    out = OrderedDict()
    synth = inputs.latest_step("yosys-synthesis")
    stat_text, stat_ref = (inputs.run_text(f"{synth}/reports/stat.json", "Yosys mapped-cell statistics")
                           if synth else (None, None))
    rpt_text, rpt_ref = (inputs.run_text(f"{synth}/reports/stat.rpt", "Yosys mapped-cell statistics (text)")
                         if synth else (None, None))
    if stat_text is None:
        out["synthesis"] = unavailable("no <NN>-yosys-synthesis/reports/stat.json")
    else:
        stat = json.loads(stat_text)
        module = stat.get("design")
        if not module:
            modules = stat.get("modules") or {}
            module = modules.get("\\" + design) or (next(iter(modules.values())) if len(modules) == 1 else {})
        total = module.get("area")
        sequential = module.get("sequential_area")
        sequential_basis = "stat.json design.sequential_area"
        if sequential is None and rpt_text:
            match = re.search(rf"of which used for sequential elements:\s*({NUM})", rpt_text)
            if match:
                sequential, sequential_basis = float(match[1]), "stat.rpt 'of which used for sequential elements'"
        counts = module.get("num_cells_by_type") or {}
        flops = {cell: n for cell, n in counts.items() if FLOP_RE.search(cell)}
        latches = {cell: n for cell, n in counts.items() if LATCH_RE.search(cell)}
        rpt_total = re.search(rf"Chip area for module [^:]*:\s*({NUM})", rpt_text or "")
        synthesis = OrderedDict(
            step=synth,
            method=("Yosys `stat -liberty` over the mapped netlist with the hosted flow's own Liberty files: total = "
                    "sum of instance Liberty areas, sequential = cells with Liberty ff/latch groups. This is the "
                    "same split experiments/live-fetch/analyze_mapped.py computed for M3B from Liberty ff/latch "
                    "groups; combinational = total - sequential"),
            creator=stat.get("creator"),
            mapped_area_total_um2=total,
            mapped_area_sequential_um2=sequential,
            mapped_area_combinational_um2=(as_number(exact(total) - exact(sequential))
                                           if total is not None and sequential is not None else None),
            combinational_basis=f"total - sequential ({sequential_basis})",
            stat_rpt_chip_area_um2=float(rpt_total[1]) if rpt_total else None,
            mapped_instance_count=module.get("num_cells"),
            flip_flop_count=sum(flops.values()),
            flip_flop_types=dict(sorted(flops.items())),
            latch_count=sum(latches.values()),
            sequential_type_rule=f"flip-flop cell regex {FLOP_RE.pattern!r}; latch regex {LATCH_RE.pattern!r}",
            cell_types=len(counts),
            top15_cell_types_by_count=[OrderedDict(cell=cell, count=n) for cell, n in
                                       sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:15]],
            sources=[ref for ref in (stat_ref, rpt_ref) if ref])
        if metrics:
            synthesis["cross_check_flow_metrics (rounded by flow)"] = OrderedDict(
                (key, metrics.get(key, "<absent from metrics>")) for key in (
                    "design__instance__count__class:sequential_cell", "design__instance__area__class:sequential_cell"))
            synthesis["sources"].append(metrics_ref)
        out["synthesis"] = synthesis

    if not metrics:
        out["routed"] = unavailable("no final metrics")
        return out
    values = OrderedDict((key, metrics.get(key, "<absent from metrics>")) for key in METRIC_AREA_KEYS)
    routed = OrderedDict(metric_keys=values)
    core = metrics.get("design__core__bbox")
    if isinstance(core, str) and len(core.split()) == 4:
        x0, y0, x1, y1 = (Decimal(part) for part in core.split())
        core_area = (x1 - x0) * (y1 - y0)
        routed["core_area_um2"] = as_number(core_area)
        routed["core_area_basis"] = "(x1-x0)*(y1-y0) of design__core__bbox (design__core__area is integer-rounded)"
    else:
        core_area = None
        routed["core_area_um2"] = metrics.get("design__core__area", "<absent>")
        routed["core_area_basis"] = "design__core__area (bbox unavailable)"
    stdcell = metrics.get("design__instance__area__stdcell")
    routed["routed_stdcell_area_um2"] = stdcell
    routed["routed_stdcell_area_basis"] = ("design__instance__area__stdcell: final non-filler standard cells, "
                                           "integer-rounded by the flow (fill/decap excluded)")
    utilization = metrics.get("design__instance__utilization__stdcell")
    routed["utilization_percent"] = (as_number(exact(utilization) * 100) if utilization is not None else None)
    routed["utilization_basis"] = "design__instance__utilization__stdcell x 100"
    if core_area and stdcell is not None:
        routed["utilization_percent_recomputed"] = float(round(exact(stdcell) / core_area * 100, 4))
        routed["utilization_recomputed_note"] = ("rounded stdcell area / exact core area; differs in the 4th "
                                                 "decimal only because the area metric is integer-rounded")
    routed["sources"] = [metrics_ref]
    out["routed"] = routed

    corner_dir = inputs.latest_step("openroad-stapostpnr")
    inventory = OrderedDict()
    if corner_dir:
        for corner in corners:
            for prefix in ("m3c", "m3b"):
                text, ref = inputs.run_text(f"{corner_dir}/{corner}/{prefix}-mapped-cell-inventory.rpt",
                                            "STA hook post-PnR cell inventory")
                if text is None:
                    continue
                rows = re.findall(rf"^(\S+)\t(\d+)\t({NUM})$", text, re.M)
                totals = re.search(rf"sequential_area=({NUM}) nonsequential_area=({NUM}) unavailable_cells=(\d+)",
                                   text)
                inventory[corner] = OrderedDict(
                    report=ref, instances=sum(int(row[1]) for row in rows),
                    flip_flops=sum(int(row[1]) for row in rows if FLOP_RE.search(row[0])),
                    sequential_area_um2=float(totals[1]) if totals else None,
                    nonsequential_area_including_physical_cells_um2=float(totals[2]) if totals else None,
                    unavailable_cells=int(totals[3]) if totals else None)
                break
    out["post_pnr_hook_cell_inventory"] = inventory or unavailable("no m3c-/m3b-mapped-cell-inventory.rpt")
    return out


def section_timing(inputs: Inputs, metrics, metrics_ref, corners) -> dict:
    out = OrderedDict()
    step = inputs.latest_step("openroad-stapostpnr")
    out["sta_step"] = step or unavailable("no <NN>-openroad-stapostpnr step directory")
    out["corners_expected"] = list(EXPECTED_CORNERS)
    out["corners_found"] = corners
    if step:
        text, ref = inputs.run_text(f"{step}/summary.rpt", "post-PnR STA summary table")
        out["summary_table"] = (OrderedDict(source=ref, **parse_summary_table(text)) if text is not None
                                else unavailable(f"{step}/summary.rpt absent"))
    per_corner = OrderedDict()
    for corner in corners:
        entry = OrderedDict()
        sources = []
        if metrics:
            keys = OrderedDict()
            for name in ("setup__ws", "hold__ws", "setup__tns", "hold__tns", "setup__wns", "hold__wns",
                         "setup_vio__count", "hold_vio__count", "setup_r2r__ws", "hold_r2r__ws",
                         "setup_r2r_vio__count", "hold_r2r_vio__count", "unannotated_net__count",
                         "unannotated_net_filtered__count"):
                key = f"timing__{name}__corner:{corner}"
                keys[key] = metrics.get(key, "<absent from metrics>")
            entry["metrics"] = keys
            sources.append(metrics_ref)
            entry["worst_setup_slack_ns"] = metrics.get(f"timing__setup__ws__corner:{corner}")
            entry["worst_hold_slack_ns"] = metrics.get(f"timing__hold__ws__corner:{corner}")
            entry["setup_tns_ns"] = metrics.get(f"timing__setup__tns__corner:{corner}")
            entry["hold_tns_ns"] = metrics.get(f"timing__hold__tns__corner:{corner}")
            entry["setup_violating_endpoints"] = metrics.get(f"timing__setup_vio__count__corner:{corner}")
            entry["hold_violating_endpoints"] = metrics.get(f"timing__hold_vio__count__corner:{corner}")
        if step:
            reports = OrderedDict()
            for name in ("ws.max.rpt", "ws.min.rpt", "tns.max.rpt", "tns.min.rpt", "wns.max.rpt", "wns.min.rpt"):
                text, ref = inputs.run_text(f"{step}/{corner}/{name}", "per-corner STA scalar report")
                if text is None:
                    reports[name] = unavailable("absent")
                    continue
                sources.append(ref)
                reports[name] = parse_corner_value(text).get(corner, unavailable("corner value not found"))
            entry["scalar_reports"] = reports
            for kind, name in (("worst_setup_path", "max.rpt"), ("worst_hold_path", "min.rpt")):
                text, ref = inputs.run_text(f"{step}/{corner}/{name}", "per-corner STA path report")
                if text is None:
                    entry[kind] = unavailable(f"{step}/{corner}/{name} absent")
                    continue
                sources.append(ref)
                entry[kind] = OrderedDict(source=ref, **worst_path_summary(text))
            ws_metric = entry.get("worst_setup_slack_ns")
            ws_path = entry.get("worst_setup_path", {}).get("slack_ns")
            if isinstance(ws_metric, (int, float)) and isinstance(ws_path, float):
                entry["setup_path_matches_metric_within_1e-6"] = abs(ws_metric - ws_path) <= 1e-6
            hs_metric = entry.get("worst_hold_slack_ns")
            hs_path = entry.get("worst_hold_path", {}).get("slack_ns")
            if isinstance(hs_metric, (int, float)) and isinstance(hs_path, float):
                entry["hold_path_matches_metric_within_1e-6"] = abs(hs_metric - hs_path) <= 1e-6
        entry["sources"] = sources
        per_corner[corner] = entry
    out["corners"] = per_corner
    out["note"] = ("Post-route STA at nominal extracted RC per library PVT corner; includes I/O paths. "
                   "Not min/max-RC coverage and not signoff.")
    return out


def parse_points(text: str) -> dict:
    if "PATH\t" not in text:
        line = next((line for line in text.splitlines() if line.startswith("UNAVAILABLE")), None)
        return unavailable(line or "no PATH block")
    blocks = text.split("PATH\t")[1:]
    first = blocks[0].splitlines()
    header = dict(item.split("=", 1) for item in first[0].split("\t")[1:] if "=" in item)
    rows, interval = [], None
    for line in first[2:]:
        if line.startswith("DATA_INTERVAL_SUM"):
            interval = dict(item.split("=", 1) for item in line.split("\t")[1:] if "=" in item)
            break
        fields = line.split("\t")
        if len(fields) == 6:
            rows.append(fields)
    result = OrderedDict(status="AVAILABLE", paths_listed=len(blocks), first_path_start=header.get("start"),
                         first_path_end=header.get("end"),
                         first_path_slack_ns=float(header["slack_ns"]) if "slack_ns" in header else None)
    if rows:
        result.update(first_pin=rows[0][0], last_pin=rows[-1][0], first_arrival_ns=float(rows[0][2]),
                      last_arrival_ns=float(rows[-1][2]),
                      q_to_d_ns=as_number(Decimal(rows[-1][2]) - Decimal(rows[0][2])))
    if interval:
        for key, value in interval.items():
            result[key] = float(value) if key.endswith("_ns") else int(value)
    result["boundary"] = ("q_to_d_ns = last minus first returned data-point arrival (launch Q to capture D); "
                          "excludes launch clock/clk-to-Q and capture clock/setup. Full report is authoritative.")
    return result


def section_paths(inputs: Inputs, corners) -> dict:
    step = inputs.latest_step("openroad-stapostpnr")
    if not step:
        return unavailable("no <NN>-openroad-stapostpnr step directory")
    found = defaultdict(dict)
    for name in inputs.run_files():
        parts = name.split("/")
        if len(parts) == 3 and parts[0] == step:
            match = PATH_REPORT_RE.match(parts[2])
            if match and "-to-" in match["label"]:
                key = (match["prefix"], match["label"])
                found[key].setdefault(parts[1], {}).setdefault(match["delay"], {})[
                    "points" if match["points"] else "full"] = name
    if not found:
        return unavailable(f"no m3c-/m3b-<source>-to-<destination>-max/min.rpt in {step}")
    pairs = OrderedDict()
    for (prefix, label), by_corner in sorted(found.items()):
        source, destination = label.split("-to-", 1)
        record = OrderedDict(prefix=prefix, source=source, destination=destination, corners=OrderedDict())
        for corner in corners:
            delays = by_corner.get(corner, {})
            corner_record = OrderedDict()
            for delay in ("max", "min"):
                files = delays.get(delay, {})
                if "full" not in files:
                    corner_record[delay] = unavailable("report absent for this corner")
                    continue
                text, ref = inputs.run_text(files["full"], "STA hook representative path report")
                header = re.search(r"launch_Q=(\d+)\s+capture_D=(\d+)", text)
                item = OrderedDict(report=ref)
                if header:
                    item["launch_Q_pins"], item["capture_D_pins"] = int(header[1]), int(header[2])
                unavailable_line = next((line for line in text.splitlines() if line.startswith("UNAVAILABLE")), None)
                if unavailable_line:
                    item.update(status="UNAVAILABLE", reason=unavailable_line)
                else:
                    item.update(worst_path_summary(text))
                if "points" in files:
                    points_text, points_ref = inputs.run_text(files["points"], "STA hook path-point report")
                    item["points"] = OrderedDict(report=points_ref, **parse_points(points_text))
                corner_record[delay] = item
            record["corners"][corner] = corner_record
        pairs[label] = record
    out = OrderedDict(step=step, prefixes=sorted({prefix for prefix, _ in found}), pair_count=len(pairs),
                      pairs=pairs)
    inventories = OrderedDict()
    errors = OrderedDict()
    for corner in corners:
        for prefix in out["prefixes"]:
            text, ref = inputs.run_text(f"{step}/{corner}/{prefix}-register-inventory.rpt",
                                        "STA hook register-group inventory")
            if text is not None:
                groups = OrderedDict()
                for match in re.finditer(r"^(\S+)\tQ=(\d+)\tD=(\d+)\tcells=(\d+)$", text, re.M):
                    groups[match[1]] = OrderedDict(Q=int(match[2]), D=int(match[3]), cells=int(match[4]))
                inventories[corner] = OrderedDict(report=ref, group_counts=groups)
            text, ref = inputs.run_text(f"{step}/{corner}/{prefix}-reporting-error.rpt", "STA hook error report")
            if text is not None:
                errors[corner] = OrderedDict(report=ref, first_lines=text.strip().splitlines()[:5])
    out["register_group_inventory"] = inventories or unavailable("no *-register-inventory.rpt")
    out["hook_reporting_errors"] = errors or "none found (no *-reporting-error.rpt)"
    return out


def fanout_group(driver_inst: str, net: str) -> str:
    if driver_inst.startswith("clkbuf_leaf_") or "clkload" in driver_inst or net.startswith("clknet"):
        return "clock_leaf"
    return "other"


def section_electrical(inputs: Inputs, metrics, metrics_ref, corners, design) -> dict:
    step = inputs.latest_step("openroad-stapostpnr")
    if not step:
        return unavailable("no <NN>-openroad-stapostpnr step directory")
    netlist, netlist_ref, netlist_basis = None, None, None
    candidates = [f"final/nl/{design}.nl.v"] + [name for name in inputs.run_files()
                                               if re.match(r"^final/nl/[^/]+\.nl\.v$", name)]
    fill = inputs.latest_step("openroad-fillinsertion")
    if fill:
        candidates.append(f"{fill}/{design}.nl.v")
    for candidate in candidates:
        text, ref = inputs.run_text(candidate, "final gate netlist (violating-net load derivation)")
        if text is not None:
            netlist, netlist_ref, netlist_basis = Netlist(text), ref, candidate
            break
    out = OrderedDict(step=step, netlist=netlist_ref or unavailable("final/nl netlist absent; loads not derived"),
                      pin_direction_rule=("IHP output-pin names " + ", ".join(sorted(OUTPUT_PINS)) +
                                          "; every other pin on the driver's net is a load"),
                      fanout_group_rule=("clock_leaf: driver instance clkbuf_leaf_* or containing clkload, "
                                         "or net clknet*; other: everything else"))
    nets = OrderedDict()

    def describe_net(pin: str) -> OrderedDict:
        inst, _, pin_name = pin.rpartition("/")
        if netlist is None:
            return OrderedDict(net=None, derivation="NOT DERIVED: netlist unavailable")
        net = netlist.net_of(inst, pin_name) if inst else pin_name
        if net is None:
            return OrderedDict(net=None, derivation=f"NOT DERIVED: {pin} not found in netlist")
        if net not in nets:
            drivers = netlist.drivers(net)
            driver = drivers[0] if len(drivers) == 1 else None
            loads = [(i, p) for i, p in netlist.nets.get(net, []) if (i, p) != driver]
            kinds = Counter(netlist.load_kind(i) for i, _ in loads)
            ports = netlist.port_loads(net)
            nets[net] = OrderedDict(
                net=net,
                driver_pin=f"{driver[0]}/{driver[1]}" if driver and driver[0] != "PORT" else (
                    f"PORT {driver[1]}" if driver else None),
                driver_cell=netlist.cell.get(driver[0]) if driver and driver[0] != "PORT" else None,
                driver_ambiguity=None if driver else f"{len(drivers)} output-named pins on net",
                load_pins=len(loads), output_port_loads=ports, load_kinds=dict(sorted(kinds.items())),
                antenna_diode_loads=kinds.get("antenna_diode", 0),
                loads_include_antenna_diodes=kinds.get("antenna_diode", 0) > 0,
                cts_dummy_loads=kinds.get("cts_dummy_load", 0),
                load_cells=dict(sorted(Counter(netlist.cell.get(i, "?") for i, _ in loads).items())))
        return nets[net]

    per_corner = OrderedDict()
    sources = [netlist_ref] if netlist_ref else []
    totals = defaultdict(set)
    rows_total = Counter()
    pins_by_corner = defaultdict(dict)
    for corner in corners:
        text, ref = inputs.run_text(f"{step}/{corner}/checks.rpt", "post-PnR STA checks report")
        if text is None:
            per_corner[corner] = unavailable(f"{step}/{corner}/checks.rpt absent")
            continue
        sources.append(ref)
        parsed = parse_checks(text)
        entry = OrderedDict(checks_report=ref, section_found=parsed["section_found"])
        for kind in parsed["rows"]:
            pins_by_corner[kind].setdefault(corner, set())
        for kind, metric in (("max_fanout", "design__max_fanout_violation__count"),
                             ("max_slew", "design__max_slew_violation__count"),
                             ("max_capacitance", "design__max_cap_violation__count")):
            rows = []
            for row in parsed["rows"][kind]:
                info = describe_net(row["pin"])
                item = OrderedDict(pin=row["pin"], net=info.get("net"),
                                   pin_role=("driver" if info.get("driver_pin") == row["pin"] else
                                             ("load" if info.get("net") else "unknown")),
                                   driver_pin=info.get("driver_pin"), driver_cell=info.get("driver_cell"),
                                   value=row["value"], limit=row["limit"], slack=row["slack"],
                                   slack_basis=row["slack_basis"])
                if kind == "max_fanout":
                    driver_inst = (info.get("driver_pin") or row["pin"]).split("/")[0]
                    item["group"] = fanout_group(driver_inst, info.get("net") or "")
                    if info.get("net"):
                        recomputed = info["load_pins"] + len(info["output_port_loads"])
                        item["recomputed_fanout"] = recomputed
                        item["recomputed_matches_report"] = recomputed == row["value"]
                        item["antenna_diode_loads"] = info["antenna_diode_loads"]
                        item["fanout_without_antenna_diodes"] = recomputed - info["antenna_diode_loads"]
                        item["within_limit_without_antenna_diodes"] = (
                            recomputed - info["antenna_diode_loads"] <= row["limit"])
                        item["cts_dummy_loads"] = info["cts_dummy_loads"]
                    else:
                        item["antenna_diode_loads"] = "NOT DERIVED"
                rows.append(item)
                if info.get("net"):
                    totals[kind].add(info["net"])
                pins_by_corner[kind][corner].add(row["pin"])
            rows_total[kind] += len(rows)
            unique = sorted({row["net"] for row in rows if row["net"]})
            block = OrderedDict(
                count_line=parsed["count_lines"][kind],
                metrics_count=(metrics or {}).get(f"{metric}__corner:{corner}", "<absent from metrics>"),
                rows_listed=len(rows), unique_nets=len(unique) if netlist else "NOT DERIVED",
                unique_pins=len({row["pin"] for row in rows}))
            if kind == "max_fanout":
                block["groups"] = dict(sorted(Counter(row["group"] for row in rows).items()))
                if netlist:
                    block["rows_matching_recomputed_fanout"] = sum(bool(row.get("recomputed_matches_report"))
                                                                   for row in rows)
                    block["nets_with_antenna_diode_loads"] = sorted({row["net"] for row in rows
                                                                     if row.get("antenna_diode_loads")})
            block["rows"] = rows
            entry[kind] = block
        entry["unparsed_violation_lines"] = parsed["unparsed_violation_lines"]
        entry["unannotated_drivers"] = parsed["unannotated_drivers"]
        entry["check_setup_output_lines"] = parsed["check_setup_output_lines"]
        entry["slack_max_-0.01_section_lines"] = parsed["slack_max_minus_0p01_lines"]
        per_corner[corner] = entry
    out["corners"] = per_corner
    out["across_corners"] = OrderedDict(
        unique_nets=OrderedDict((kind, len(totals[kind]) if netlist else "NOT DERIVED (no netlist)")
                                for kind in ("max_fanout", "max_slew", "max_capacitance")),
        unique_pins=OrderedDict((kind, len(set().union(*pins_by_corner[kind].values())))
                                for kind in ("max_fanout", "max_slew", "max_capacitance")),
        net_corner_rows=OrderedDict((kind, rows_total[kind]) for kind in ("max_fanout", "max_slew", "max_capacitance")),
        fanout_pin_set_identical_across_corners=(len({frozenset(v) for v in pins_by_corner["max_fanout"].values()}) <= 1),
        metrics_aggregate=OrderedDict((key, (metrics or {}).get(key, "<absent from metrics>")) for key in (
            "design__max_fanout_violation__count", "design__max_slew_violation__count",
            "design__max_cap_violation__count")))
    out["violating_nets"] = nets
    out["netlist_source"] = netlist_basis
    out["sources"] = sources + ([metrics_ref] if metrics_ref else [])
    out["note"] = ("LibreLane has no fanout checker and treats max-slew as a warning here; these counts are "
                   "report/metric values, not a pass/fail signoff verdict.")
    return out


def section_physical(inputs: Inputs, metrics, metrics_ref, resolved_context) -> dict:
    if not metrics:
        return unavailable("no final metrics")
    keys = OrderedDict((key, metrics.get(key, "<absent from metrics>")) for key in METRIC_PHYSICAL_KEYS)
    extra = OrderedDict((key, value) for key, value in sorted(metrics.items())
                        if re.search(r"drc|lvs|antenna|xor", key, re.I) and key not in keys)

    def value(key):
        found = metrics.get(key)
        return found if found is not None else "UNAVAILABLE (key absent)"

    lvs_keys = [key for key in keys if key.startswith("design__lvs_")]
    lvs_values = [metrics.get(key) for key in lvs_keys if key in metrics]
    summary = OrderedDict(
        route_drc_errors=value("route__drc_errors"),
        antenna_violating_nets=value("antenna__violating__nets"),
        antenna_violating_pins=value("antenna__violating__pins"),
        route_antenna_violation_count=value("route__antenna_violation__count"),
        lvs_error_count=value("design__lvs_error__count"),
        lvs_result=("PASS (all design__lvs_* counts 0)" if lvs_values and not any(lvs_values) else
                    ("FAIL" if lvs_values else "UNAVAILABLE")),
        magic_drc_errors=value("magic__drc_error__count"),
        magic_illegal_overlap=value("magic__illegal_overlap__count"),
        klayout_drc_errors_in_flow=(metrics["klayout__drc_error__count"] if "klayout__drc_error__count" in metrics
                                    else "NOT RUN in implementation flow (key absent; RUN_KLAYOUT_DRC="
                                         f"{resolved_context.get('RUN_KLAYOUT_DRC', ABSENT)}); see precheck"),
        klayout_xor=(metrics["design__xor_difference__count"] if "design__xor_difference__count" in metrics
                     else f"NOT RUN (key absent; RUN_KLAYOUT_XOR={resolved_context.get('RUN_KLAYOUT_XOR', ABSENT)})"),
        power_grid_violations=value("design__power_grid_violation__count"),
        disconnected_pins=value("design__disconnected_pin__count"),
        critical_disconnected_pins=value("design__critical_disconnected_pin__count"))
    return OrderedDict(summary=summary, metric_keys_used=keys, other_drc_lvs_antenna_xor_keys=extra,
                       sources=[metrics_ref])


def job_kind(name: str):
    name = name.lower()
    if "precheck" in name:
        return "precheck"
    if re.search(r"(^|[^a-z])gl([^a-z]|$)|gate|gl_test", name):
        return "gl_test"
    if "gds" in name:
        return "gds"
    return None


def classify_log(path: Path, lines: list) -> tuple:
    jobs = Counter(match["job"] for match in lines if match and match["job"])
    if jobs:
        name = jobs.most_common(1)[0][0]
        return job_kind(name) or name, f"first tab field (gh run view --log): {name!r}"
    kind = job_kind(re.sub(r"-\d+\.log$", "", path.name))
    return (kind, "file name") if kind else ("unknown", "no job marker")


def overall(outcomes: dict, job_conclusion, no_tests_reason):
    """Combine independent sources; any FAIL wins, zero collected tests is never PASS."""
    failed = [source for source, value in outcomes.items() if value == "FAIL"]
    passed = [source for source, value in outcomes.items() if value == "PASS"]
    if failed:
        return "FAIL", f"FAIL in: {', '.join(failed)}; job conclusion: {job_conclusion}"
    if job_conclusion == "failure":
        return "FAIL", "job conclusion failure (run.json)"
    if passed:
        note = "" if job_conclusion in ("success",) or str(job_conclusion).startswith("UNAVAILABLE") else \
            f"; NOTE job conclusion {job_conclusion}"
        return "PASS", f"PASS in: {', '.join(passed)}; job conclusion: {job_conclusion}{note}"
    if no_tests_reason:
        return "FAIL", no_tests_reason
    return "NOT AVAILABLE", f"no PASS/FAIL evidence; job conclusion: {job_conclusion}"


def section_tests(inputs: Inputs, run_dir: Path, run_jobs) -> dict:
    out = OrderedDict()
    log_dir = run_dir / "logs"
    if log_dir.is_dir():
        log_files, log_basis = sorted(log_dir.glob("*.log")), "run_dir/logs/*.log"
    else:
        log_files, log_basis = sorted(run_dir.glob("*.log")), "run_dir/*.log (fallback: logs/ absent)"
    logs = []
    for path in log_files:
        data, ref = inputs.read_disk(path, "job log")
        text = data.decode("utf-8", errors="replace")
        raw = text.splitlines()
        parsed = [LOG_LINE_RE.match(line) for line in raw]
        nonempty = [match for line, match in zip(raw, parsed) if line.strip()]
        sample = nonempty[:400]
        record = OrderedDict(log=ref)
        if not sample or sum(match is not None for match in sample) * 2 < len(sample):
            record.update(status="NOT A JOB LOG",
                          reason=("empty file" if not sample else
                                  "fewer than half of the lines carry GitHub runner timestamps"),
                          first_line=(raw[0][:200] if raw else ""))
            logs.append(record)
            continue
        job, basis = classify_log(path, parsed)
        messages = [match["msg"] if match else line for line, match in zip(raw, parsed)]
        summaries = []
        for message in messages:
            match = COCOTB_SUMMARY_RE.search(message)
            if match:
                summaries.append(OrderedDict(TESTS=int(match[1]), PASS=int(match[2]), FAIL=int(match[3]),
                                             SKIP=int(match[4])))
        test_rows = []
        for message in messages:
            match = COCOTB_ROW_RE.search(message)
            if match and match[1] != "TEST":
                test_rows.append(f"{match[1]} {match[2]}")
        errors = [message.split("##[error]", 1)[1][:300] for message in messages if "##[error]" in message]
        elaboration = [int(match[1]) for message in messages
                       for match in [re.search(r"(\d+) error\(s\) during elaboration", message)] if match]
        record.update(
            status="JOB LOG", job=job, job_basis=basis, lines=len(raw),
            cocotb_summaries=summaries, cocotb_test_rows=test_rows[:100],
            precheck_verdicts=[message.strip()[:300] for message in messages
                               if re.search(r"Precheck (passed|failed)", message, re.I)],
            check_results_lines=[match[0] for message in messages for match in [CHECK_RESULTS_RE.search(message)]
                                 if match],
            elaboration_error_counts=elaboration,
            error_annotation_count=len(errors), error_annotations_first=errors[:10])
        logs.append(record)
    out["logs_basis"] = log_basis
    out["logs"] = logs or unavailable("no job logs found (logs/ absent or empty, no top-level *.log)")

    junit = []
    seen = set()
    for entry in inputs.find(r"(?:^|/)results\.xml$"):
        data, ref = inputs.read(entry, "JUnit results")
        if ref["sha256"] in seen:
            junit.append(OrderedDict(source=ref, duplicate_of_earlier_identical_content=True))
            continue
        seen.add(ref["sha256"])
        try:
            root = ET.fromstring(data)
        except ET.ParseError as error:
            junit.append(OrderedDict(source=ref, status="UNPARSEABLE", reason=str(error)))
            continue
        suites = [suite.get("name") for suite in root.iter("testsuite")]
        cases = list(root.iter("testcase"))
        failed = [case for case in cases if case.find("failure") is not None or case.find("error") is not None]
        skipped = [case for case in cases if case.find("skipped") is not None]
        suite_failures = sum(int(suite.get("failures", "0") or 0) + int(suite.get("errors", "0") or 0)
                             for suite in root.iter("testsuite"))
        kind = "precheck" if any("precheck" in (name or "").lower() for name in suites) else "cocotb/other"
        seeds = [prop.get("value") for prop in root.iter("property") if prop.get("name") == "random_seed"]
        junit.append(OrderedDict(
            source=ref, kind=kind, suites=suites, tests=len(cases), passed=len(cases) - len(failed) - len(skipped),
            failed=len(failed), skipped=len(skipped), suite_level_failures_errors=suite_failures,
            random_seeds=seeds,
            cases=[OrderedDict(name=f"{case.get('classname') + '.' if case.get('classname') else ''}{case.get('name')}",
                               result="FAIL" if case in failed else ("SKIP" if case in skipped else "PASS"))
                   for case in cases]))
    drc = OrderedDict()
    for entry in inputs.find(r"(?:^|/)drc_[^/]+\.xml$"):
        name = entry.rel.rsplit("/", 1)[-1]
        if name in drc:
            continue
        data, ref = inputs.read(entry, "precheck KLayout report database")
        try:
            items = ET.fromstring(data).find("items")
            categories = Counter((item.findtext("category") or "?").strip("'") for item in
                                 (items if items is not None else []))
            drc[name] = OrderedDict(source=ref, items=sum(categories.values()), by_category=dict(categories))
        except ET.ParseError as error:
            drc[name] = OrderedDict(source=ref, status="UNPARSEABLE", reason=str(error))

    def job_conclusion(name):
        if not isinstance(run_jobs, list):
            return "UNAVAILABLE (run.json absent)"
        found = [job.get("conclusion") for job in run_jobs if job_kind(job.get("name") or "") == name]
        return found[0] if found else "UNAVAILABLE (job not in run.json)"

    job_logs = [log for log in logs if log.get("status") == "JOB LOG"]
    precheck_xml = [item for item in junit if item.get("kind") == "precheck"]
    precheck_logs = [log for log in job_logs if log["job"] == "precheck"]
    verdicts = [line for log in precheck_logs for line in log["precheck_verdicts"]]
    if precheck_xml:
        bad = any(item["failed"] or item["skipped"] or item["suite_level_failures_errors"] or item["tests"] < 1
                  for item in precheck_xml)
        xml_outcome = "FAIL" if bad else "PASS"
    else:
        xml_outcome = "NOT AVAILABLE"
    if verdicts:
        log_outcome = "FAIL" if any(re.search(r"failed", line, re.I) for line in verdicts) else "PASS"
    else:
        log_outcome = "NOT AVAILABLE" if not precheck_logs else "NO VERDICT LINE IN LOG"
    out["precheck"] = OrderedDict(
        job_conclusion=job_conclusion("precheck"),
        junit_outcome=xml_outcome,
        junit=[OrderedDict((k, item[k]) for k in ("source", "suites", "tests", "passed", "failed", "skipped",
                                                    "suite_level_failures_errors", "cases")) for item in precheck_xml],
        log_outcome=log_outcome, log_verdict_lines=verdicts,
        klayout_report_databases=drc or unavailable("no precheck drc_*.xml"),
        rule="PASS requires >=1 case and zero failed/errored/skipped cases; zero collected is never PASS")
    out["precheck"]["overall"], out["precheck"]["overall_basis"] = overall(
        {"junit": xml_outcome, "log": log_outcome}, out["precheck"]["job_conclusion"], None)

    gl_logs = [log for log in job_logs if log["job"] == "gl_test"]
    summaries = [summary for log in gl_logs for summary in log["cocotb_summaries"]]
    gl_xml = [item for item in junit if item.get("kind") == "cocotb/other"]
    if summaries:
        log_gl = "FAIL" if any(s["FAIL"] or s["SKIP"] or s["TESTS"] < 1 for s in summaries) else "PASS"
    elif gl_logs:
        errors = sum(log["error_annotation_count"] for log in gl_logs)
        log_gl = (f"NOT AVAILABLE: no cocotb TESTS= summary line (tests not run or not collected); "
                  f"{errors} ##[error] annotation(s) in gl_test log")
    else:
        log_gl = "NOT AVAILABLE: no gl_test job log"
    if gl_xml:
        xml_gl = "FAIL" if any(item["failed"] or item["skipped"] or item["suite_level_failures_errors"]
                               or item["tests"] < 1 for item in gl_xml) else "PASS"
    else:
        xml_gl = "NOT AVAILABLE"
    out["gate_tests"] = OrderedDict(
        job_conclusion=job_conclusion("gl_test"),
        log_outcome=log_gl,
        log_summaries=[OrderedDict(log=log["log"]["path"], summaries=log["cocotb_summaries"],
                                   test_rows=log["cocotb_test_rows"], check_results_lines=log["check_results_lines"],
                                   elaboration_error_counts=log["elaboration_error_counts"],
                                   error_annotations_first=log["error_annotations_first"]) for log in gl_logs],
        junit_outcome=xml_gl,
        junit=[OrderedDict((k, item[k]) for k in ("source", "suites", "tests", "passed", "failed", "skipped",
                                                    "suite_level_failures_errors", "random_seeds", "cases"))
               for item in gl_xml],
        rule="PASS requires >=1 test and zero FAIL/SKIP (check_results.py --forbid-skips semantics)",
        boundary="Functional gate-level simulation without SDF; not timing simulation")
    no_tests = None
    if gl_logs and not summaries and not gl_xml and any(log["error_annotation_count"] for log in gl_logs):
        no_tests = "no TESTS= summary and no gate JUnit, while the gl_test log has ##[error] annotations"
    out["gate_tests"]["overall"], out["gate_tests"]["overall_basis"] = overall(
        {"log": log_gl, "junit": xml_gl}, out["gate_tests"]["job_conclusion"], no_tests)
    out["junit_files_seen"] = [OrderedDict(source=item["source"], kind=item.get("kind", "duplicate"))
                               for item in junit]
    return out


# ---------------------------------------------------------------------------------- main ---

def guarded(name, function, *args):
    try:
        return function(*args)
    except Exception as error:  # noqa: BLE001 - record, never crash on a malformed input
        print(f"warning: section {name}: {type(error).__name__}: {error}", file=sys.stderr)
        return unavailable(f"analyzer exception in section {name}: {type(error).__name__}: {error}")


def analyze(run_dir: Path) -> dict:
    inputs = Inputs(run_dir)
    result = OrderedDict(schema=SCHEMA)
    script = Path(__file__).resolve()
    result["analyzer"] = OrderedDict(script=display(script), sha256=sha256(script.read_bytes()),
                                     python_major_minor=f"{sys.version_info[0]}.{sys.version_info[1]}")
    result["run_dir"] = display(run_dir)
    result["boundary"] = ("Read-only extraction from one hosted LibreLane run's downloaded reports. Not RTL, FPGA, "
                          "hardware or signoff evidence; a successful job is not timing or electrical-rule closure.")
    result["artifact_index"] = OrderedDict(
        artifacts_dir=display(inputs.base) if inputs.base.is_dir() else unavailable("artifacts/ absent"),
        files_on_disk=sum(entry.kind == "disk" for entry in inputs.entries),
        zip_containers=[OrderedDict(path=display(path), members=len(inputs.zips[rel].namelist()))
                        for rel, path in sorted(inputs.zip_paths.items())],
        run_roots_in_preference_order=inputs.describe_roots(),
        steps_present=inputs.steps())
    if len([key for key in inputs.roots if key[0] == ""]) > 1:
        result["artifact_index"]["warning"] = ("several extracted runs/wokwi roots; files are taken from the first "
                                               "root that has them (see run_roots_in_preference_order)")

    metrics, metrics_ref, metrics_basis = resolve_metrics(inputs)
    text, _ = inputs.run_text("resolved.json", "resolved flow configuration")
    resolved = json.loads(text) if text else {}
    design = resolved.get("DESIGN_NAME") or next(
        (name.split("/")[-1][:-len(".nl.v")] for name in inputs.run_files() if re.match(r"^final/nl/[^/]+\.nl\.v$", name)),
        "UNKNOWN_DESIGN")
    step = inputs.latest_step("openroad-stapostpnr")
    corner_dirs = sorted({name.split("/")[1] for name in inputs.run_files()
                          if step and name.startswith(step + "/") and name.count("/") == 2})
    metric_corners = sorted({key.split("corner:", 1)[1] for key in (metrics or {})
                             if key.startswith("timing__setup__ws__corner:")})
    corners = sorted(set(corner_dirs) | set(metric_corners)) or list(EXPECTED_CORNERS)
    result["metrics_source"] = OrderedDict(basis=metrics_basis, file=metrics_ref) if metrics else unavailable(
        "neither final/metrics.json nor any step state_out.json metrics found")

    identity = guarded("identity", section_identity, inputs, run_dir)
    manifest = identity.pop("_manifest", None) if isinstance(identity, dict) else None
    result["identity"] = identity
    result["area"] = guarded("area", section_area, inputs, metrics, metrics_ref, design, corners)
    result["timing"] = guarded("timing", section_timing, inputs, metrics, metrics_ref, corners)
    result["representative_paths"] = guarded("representative_paths", section_paths, inputs, corners)
    result["electrical_rules"] = guarded("electrical_rules", section_electrical, inputs, metrics, metrics_ref,
                                         corners, design)
    context = (resolved.get(key, ABSENT) for key in CONTEXT_KEYS)
    result["physical_checks"] = guarded("physical_checks", section_physical, inputs, metrics, metrics_ref,
                                        dict(zip(CONTEXT_KEYS, context)))
    jobs = identity.get("jobs") if isinstance(identity, dict) else None
    result["precheck_and_gate_tests"] = guarded("precheck_and_gate_tests", section_tests, inputs, run_dir, jobs)

    # Integrity: every run file read versus the hosted evidence/artifact-sha256.json manifest.
    if manifest:
        checked = OrderedDict(match=0, mismatch=[], not_listed=0)
        for label, runrel in inputs.runrel_of.items():
            expected = manifest.get(f"runs/wokwi/{runrel}")
            if expected is None:
                checked["not_listed"] += 1
            elif expected == inputs.registry[label]["sha256"]:
                checked["match"] += 1
            else:
                checked["mismatch"].append(label)
        result["integrity_vs_hosted_manifest"] = checked
    else:
        result["integrity_vs_hosted_manifest"] = unavailable("no evidence/artifact-sha256.json")

    missing = []

    def walk(node, where):
        if isinstance(node, dict):
            if node.get("status") == "UNAVAILABLE":
                missing.append(OrderedDict(item=where, reason=node.get("reason")))
            for key, value in node.items():
                walk(value, f"{where}.{key}" if where else str(key))
        elif isinstance(node, list):
            for index, value in enumerate(node):
                walk(value, f"{where}[{index}]")

    walk(result, "")
    result["unavailable_items"] = missing
    result["inputs"] = list(inputs.registry.values())
    return result


def concise(result: dict) -> str:
    lines = []
    identity = result.get("identity", {})
    run = identity.get("run", {}) if isinstance(identity, dict) else {}
    hosted = identity.get("hosted_run_identity", {}) if isinstance(identity, dict) else {}
    lines.append(f"run.json: id {run.get('databaseId')} sha {run.get('headSha')} conclusion "
                 f"{run.get('conclusion', run.get('status'))}; evidence: run {hosted.get('GITHUB_RUN_ID')} "
                 f"sha {hosted.get('GITHUB_SHA')} implementation {hosted.get('IMPLEMENTATION_OUTCOME')}")
    area = result.get("area", {})
    synthesis = area.get("synthesis", {}) if isinstance(area, dict) else {}
    routed = area.get("routed", {}) if isinstance(area, dict) else {}
    lines.append(f"mapped area total/seq/comb um2: {synthesis.get('mapped_area_total_um2')} / "
                 f"{synthesis.get('mapped_area_sequential_um2')} / {synthesis.get('mapped_area_combinational_um2')}"
                 f"; FFs {synthesis.get('flip_flop_count')}")
    lines.append(f"routed stdcell um2 {routed.get('routed_stdcell_area_um2')}, core um2 {routed.get('core_area_um2')}, "
                 f"utilization % {routed.get('utilization_percent')}")
    timing = result.get("timing", {})
    electrical = result.get("electrical_rules", {})
    for corner, entry in (timing.get("corners") or {}).items():
        rules = (electrical.get("corners") or {}).get(corner, {}) if isinstance(electrical, dict) else {}
        counts = [rules.get(kind, {}).get("rows_listed") for kind in ("max_fanout", "max_slew", "max_capacitance")]
        lines.append(f"{corner}: setup ws {entry.get('worst_setup_slack_ns')} hold ws {entry.get('worst_hold_slack_ns')} "
                     f"vio s/h {entry.get('setup_violating_endpoints')}/{entry.get('hold_violating_endpoints')}; "
                     f"fanout/slew/cap rows {counts[0]}/{counts[1]}/{counts[2]}")
    if isinstance(electrical, dict) and "across_corners" in electrical:
        lines.append(f"unique violating nets across corners: {dict(electrical['across_corners']['unique_nets'])}")
    physical = result.get("physical_checks", {})
    if isinstance(physical, dict) and "summary" in physical:
        s = physical["summary"]
        lines.append(f"route DRC {s['route_drc_errors']}, antenna nets {s['antenna_violating_nets']}, "
                     f"LVS {s['lvs_result']}, Magic DRC {s['magic_drc_errors']}")
    tests = result.get("precheck_and_gate_tests", {})
    if isinstance(tests, dict) and "precheck" in tests:
        for name in ("precheck", "gate_tests"):
            lines.append(f"{name}: {tests[name]['overall']} ({tests[name]['overall_basis']})")
    lines.append(f"unavailable items: {len(result.get('unavailable_items', []))}; inputs read: "
                 f"{len(result.get('inputs', []))}")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--out", type=Path, help="write the JSON summary here (default: JSON to stdout)")
    args = parser.parse_args()
    run_dir = args.run_dir.resolve()
    if not run_dir.is_dir():
        parser.error(f"{args.run_dir} is not a directory")
    if args.out is not None:
        out = Path(os.path.abspath(args.out))
        artifacts = run_dir / "artifacts"
        if out == artifacts or artifacts in out.parents:
            parser.error("--out must not be inside run_dir/artifacts (inputs are read-only)")
    result = analyze(run_dir)
    if args.out is None:
        sys.stdout.write(json.dumps(strict_json(result), indent=2, allow_nan=False) + "\n")
        return 0
    if any(record["path"] == display(out) for record in result["inputs"]):
        parser.error("--out would overwrite an input file")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(strict_json(result), indent=2, allow_nan=False) + "\n")
    print(concise(result))
    print(f"wrote {display(out)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
