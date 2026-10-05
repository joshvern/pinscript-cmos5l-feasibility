#!/usr/bin/env python3
"""Read-only timing selection/structural audit; selected ZIP members, never extraction.

The connectivity graph stops at every physical register. It is a structural
check, not functional sensitization or a replacement for OpenSTA. The mapped
fetch cut is the first net with all 64 words of exactly one instruction bit in
its combinational fanin. Both retained polarities are included. No RTL names
are presumed to survive for this cut.
"""
from __future__ import annotations

import argparse
from collections import defaultdict, deque
from functools import cache
import hashlib
import json
import math
from pathlib import Path
import re

from analyze_run import Inputs, Netlist, OUTPUT_PINS, FLOP_RE, parse_paths, EXPECTED_CORNERS

SCHEMA = "pinscript-timing-evidence/2"
PATTERNS = {
    "pc": r"(^|[./])engine[./]pc(\[[0-9]+\])?$",
    "memory": r"(^|[./])memory\[[0-9]+\]\[[0-9]+\]$",
    "pad_out": r"(^|[./])uio_out\[[0-9]+\]$",
    "pad_oe": r"(^|[./])uio_oe\[[0-9]+\]$",
    "pin_intent": r"(^|[./])engine[./](out|oe|od|g)(\[[0-9]+\])?$|^core[./]drive_gate$",
    "shift": r"(^|[./])engine[./](reg_sr|sr|r0|r1)(\[[0-9]+\])?$",
    "timer": r"(^|[./])engine[./](t|c)(\[[0-9]+\])?$|^core[./]elapsed\[[0-9]+\]$",
    "record": r"(^|[./])engine[./](run|reason)(\[[0-9]+\])?$|^core[./]diag_in\[[0-9]+\]$",
    "sync2": r"(^|[./])engine[./]sync2\[[0-9]+\]$",
    "fifo": r"(^|[./])(tx_fifo|rx_fifo)[./](occupancy|read_pointer|slot[0-3])(\[[0-9]+\])?$|(^|[./])engine[./](tx_count|rx_count)\[[0-9]+\]$",
    "store_control": r"(^|[./])(program_valid|loading|loaded_count|expected_length)(\[[0-9]+\])?$",
    "host_address": r"(^|[./])read_index\[[0-9]+\]$",
    "host_capture": r"(^|[./])transmit_shift\[[0-9]+\]$",
}
EXPECTED_COUNTS = dict(pc=7, memory=1024, pad_out=8, pad_oe=8,
                       pin_intent=25, shift=24, timer=41, record=13, sync2=8,
                       fifo=74, store_control=16, host_address=16, host_capture=16,
                       registers=1377, top_pins=24)
PAIRS = [("registers", "registers"), ("pc", "pc"), ("memory", "pc"),
         ("store_control", "pc"), ("timer", "pc"), ("sync2", "pc"),
         ("fifo", "pc"), ("pc", "pads"), ("memory", "pads"),
         ("pc", "pad_out"), ("pc", "pad_oe"), ("memory", "pad_out"),
         ("memory", "pad_oe"), ("pc", "pin_intent"), ("memory", "pin_intent"),
         ("pc", "shift"), ("memory", "shift"), ("fifo", "shift"),
         ("sync2", "shift"), ("pc", "timer"), ("memory", "timer"),
         ("pc", "record"), ("memory", "record"), ("pc", "fifo"),
         ("host_address", "host_capture"), ("memory", "host_capture"),
         ("pc", "host_capture"), ("registers", "top_pins"),
         ("pads", "top_pins"), ("pin_intent", "top_pins")]
OPTIONAL_ABSENT = {"pin_intent-to-top_pins":
                   "OUT/OE/OD/G feed registered uio_out/uio_oe D inputs; those registers break the combinational path to top ports."}
REQUIRED_GROUPS = tuple(f"{s}-to-{d}" for s, d in PAIRS if f"{s}-to-{d}" not in OPTIONAL_ABSENT) + ("pc-via-fetch-to-pc",)


class Connectivity:
    def __init__(self, text):
        self.nl = nl = Netlist(text)
        self.pred = defaultdict(set)
        self.succ = defaultdict(set)
        self.groups = {name: {"q": [], "d": [], "cells": []} for name in PATTERNS}
        self.groups["registers"] = {"q": [], "d": [], "cells": []}
        self.q_nets, self.pin_nets, self.memory_sources = {}, {}, {}
        for inst, pins in nl.pins.items():
            for pin, net in pins.items():
                self.pin_nets[f"{inst}/{pin}"] = net
            if FLOP_RE.search(nl.cell[inst]):
                if "Q" not in pins or "D" not in pins:
                    raise ValueError(f"unsupported sequential pin contract: {inst}")
                q, d, net = f"{inst}/Q", f"{inst}/D", pins["Q"]
                self.q_nets[q] = net
                match = re.search(r"memory\[(\d+)\]\[(\d+)\]$", net)
                if match:
                    self.memory_sources[net] = 1 << (int(match[1]) * 16 + int(match[2]))
                for group in ["registers"] + [g for g, pat in PATTERNS.items() if re.search(pat, net)]:
                    self.groups[group]["q"].append(q)
                    self.groups[group]["d"].append(d)
                    self.groups[group]["cells"].append(inst)
                continue
            # IHP pin directions are independently recorded in the analyzer.
            for pin, net in pins.items():
                if pin in OUTPUT_PINS:
                    for p, n in pins.items():
                        if p not in OUTPUT_PINS:
                            self.pred[net].add(n)
                            self.succ[n].add(net)
        for net, aliases in nl.assigned_outputs.items():
            for alias in aliases:
                self.pred[alias].add(net)
                self.succ[net].add(alias)
        self.groups["pads"] = {p: sorted(self.groups["pad_out"][p] + self.groups["pad_oe"][p]) for p in ("q", "d", "cells")}
        self.groups["top_pins"] = {"q": [], "d": sorted(nl.outputs), "cells": []}
        for name, props in self.groups.items():
            for key, values in props.items():
                props[key] = sorted(set(values))
        for group, count in EXPECTED_COUNTS.items():
            key = "d" if group == "top_pins" else "cells"
            if len(self.groups[group][key]) != count:
                raise ValueError(f"{group}: expected {count} physical objects, got {len(self.groups[group][key])}")

    def reachable(self, starts):
        seen = set(starts)
        queue = deque(seen)
        while queue:
            for net in self.succ[queue.popleft()]:
                if net not in seen:
                    seen.add(net)
                    queue.append(net)
        return seen

    def fetch_cut(self):
        @cache
        def mask(net):
            if net in self.memory_sources:
                return self.memory_sources[net]
            value = 0
            for parent in self.pred.get(net, ()):
                value |= mask(parent)
            return value
        result = {}
        for bit in range(16):
            want = sum(1 << (word * 16 + bit) for word in range(64))
            nets = sorted(net for net in list(self.pred) if mask(net) == want and
                          all(mask(parent) != want for parent in self.pred[net]))
            if not nets:
                raise ValueError(f"no complete instruction-read convergence for bit {bit}")
            result[str(bit)] = nets
        return result

    def pair(self, source, destination, via=None):
        launch = self.groups[source]["q"]
        capture = self.groups[destination]["d"]
        reached = self.reachable(self.q_nets[q] for q in launch)
        if via is not None:
            reached = self.reachable(set(via) & reached)
        endpoints = [d for d in capture if self.pin_nets.get(d, d) in reached]
        return dict(status="PASS" if endpoints else "ABSENT", reachable_endpoint_count=len(endpoints),
                    reachable_endpoints=endpoints,
                    explanation="Flat mapped-cell combinational reachability, stopping at every Q/D boundary; D only, RESET_B and clocks excluded.")

    def audit(self):
        cut = self.fetch_cut()
        result = {f"{s}-to-{d}": self.pair(s, d) for s, d in PAIRS}
        result["pc-via-fetch-to-pc"] = self.pair("pc", "pc", [n for v in cut.values() for n in v])
        for name, row in result.items():
            row["requirement"] = "NOT_APPLICABLE" if name in OPTIONAL_ABSENT else "REQUIRED"
            if name in OPTIONAL_ABSENT:
                row["explanation"] += " " + OPTIONAL_ABSENT[name]
                if row["status"] != "ABSENT":
                    raise ValueError(f"optional-absence contract contradicted: {name}")
            elif row["status"] != "PASS":
                raise ValueError(f"required structural connection absent: {name}")
        return dict(groups=self.groups, fetch_cut=cut, pairs=result)


def selected_input(inputs, suffix):
    text, ref = inputs.run_text(suffix, "timing selection structural audit")
    if text is None:
        raise ValueError(f"missing {suffix}")
    return text, ref


def structural_audit(run_dir):
    inputs = Inputs(Path(run_dir))
    text, ref = selected_input(inputs, "final/nl/tt_um_joshua_vernazza_pinscript.nl.v")
    result = Connectivity(text).audit()
    result["source"] = ref
    synth, synth_ref = selected_input(inputs, "06-yosys-synthesis/tt_um_joshua_vernazza_pinscript.nl.v")
    result["synthesis_source"] = synth_ref
    result["synthesis_sha256"] = hashlib.sha256(synth.encode()).hexdigest()
    result["synthesis_fetch_cut"] = Connectivity(synth).fetch_cut()
    if result["fetch_cut"] != result["synthesis_fetch_cut"]:
        raise ValueError("routed fetch cut differs from mapped cut; review required")
    return result


def parse_units(text):
    values = dict(re.findall(r"^\s*(time|capacitance|resistance|voltage|current|power|distance)\s+(\S+)\s*$", text, re.M))
    for name, unit in (("time", "ns"), ("capacitance", "pF")):
        if values.get(name) in (unit, "1" + unit):
            values[name] = unit
    values["status"] = "PASS" if values.get("time") == "ns" and values.get("capacitance") == "pF" else "INCOMPLETE"
    return values


def parse_points_for_validation(text):
    paths = []
    current = None
    for line in text.splitlines():
        match = re.match(r"^PATH\t(\d+)\tstart=(\S+)\tend=(\S+)\tslack_ns=(\S+)$", line)
        if match:
            current = dict(startpoint=match[2], endpoint=match[3], slack_ns=float(match[4]), pins=[])
            paths.append(current)
        elif current and "\t" in line and not line.startswith(("pin\t", "DATA_INTERVAL_SUM")):
            current["pins"].append(line.split("\t")[0])
    return paths


def evaluate_report(text, points_text, connection, source, destination, via=()):
    """Validate v2 report selection against independently parsed routed connectivity."""
    errors = []
    launches = re.findall(r"^LAUNCH\t(.+)$", text, re.M)
    captures = re.findall(r"^CAPTURE\t(.+)$", text, re.M)
    vias = re.findall(r"^VIA\t(.+)$", text, re.M)
    if sorted(launches) != connection.groups[source]["q"]:
        errors.append("launch objects do not match physical register discovery")
    if sorted(captures) != connection.groups[destination]["d"]:
        errors.append("capture objects do not match physical D/port discovery")
    if sorted(vias) != sorted(via):
        errors.append("through objects do not match connectivity-derived fetch cut")
    count = re.search(r"^status=(\S+) path_count=(\d+)", text, re.M)
    header = re.search(r"^corner=(\S+) delay=(\S+) launch_Q=(\d+) capture_D=(\d+)$", text, re.M)
    if not header or int(header[3]) != len(launches) or int(header[4]) != len(captures):
        errors.append("missing/inconsistent selection counts")
    clock = re.search(r"^clock_period_ns=(\S+)$", text, re.M)
    clock_period = float(clock[1]) if clock else None
    if clock_period != 100.0:
        errors.append("missing/wrong 100 ns clock constraint")
    if "schema=pinscript-timing-report/2" not in text:
        errors.append("missing v2 report contract")
    checks = parse_paths(text)
    paths = parse_points_for_validation(points_text)
    if not count or int(count[2]) != len(checks) or len(paths) != len(checks):
        errors.append("missing/inconsistent full-report and point path counts")
    finite = all(all(isinstance(p[k], (int, float)) and math.isfinite(p[k]) for k in
                     ("data_arrival_ns", "data_required_ns", "slack_ns")) for p in checks)
    if not finite:
        errors.append("non-finite/missing path timing")
    for number, path in enumerate(paths):
        if path["startpoint"] not in launches or path["endpoint"] not in captures:
            errors.append(f"path {number}: actual start/end outside selected physical objects")
        pins = path["pins"]
        if not pins or pins[0] != path["startpoint"] or pins[-1] != path["endpoint"]:
            errors.append(f"path {number}: incomplete point sequence")
        nets = [connection.pin_nets.get(pin, pin) for pin in pins]
        if via and not set(via).intersection(nets):
            errors.append(f"path {number}: bypasses instruction-read cut")
        for p1, p2, n1, n2 in zip(pins, pins[1:], nets, nets[1:]):
            same_net = n1 == n2 or n2 in connection.succ.get(n1, ()) and ("/" not in p1 or "/" not in p2)
            same_cell = ("/" in p1 and "/" in p2 and p1.rsplit("/", 1)[0] == p2.rsplit("/", 1)[0]
                         and p1.rsplit("/", 1)[1] not in OUTPUT_PINS
                         and p2.rsplit("/", 1)[1] in OUTPUT_PINS
                         and n2 in connection.succ.get(n1, ())
                         and not FLOP_RE.search(connection.nl.cell.get(p1.rsplit("/", 1)[0], "")))
            if not same_net and not same_cell:
                errors.append(f"path {number}: disconnected point arc {p1} -> {p2}")
                break
        if number < len(checks) and finite and abs(path["slack_ns"] - checks[number]["slack_ns"]) > 0.00001:
            errors.append(f"path {number}: point/full report slack mismatch")
        if number < len(checks):
            for field in ("startpoint", "endpoint"):
                printed = (checks[number].get(field) or "").split(" ", 1)[0]
                actual = path[field].rsplit("/", 1)[0] if "/" in path[field] else path[field]
                if printed != actual:
                    errors.append(f"path {number}: point/full report {field} mismatch")
    worst = min(checks, key=lambda p: p["slack_ns"]) if checks and finite else None
    return dict(status="INCOMPLETE" if errors else ("FAIL" if worst and worst["slack_ns"] < 0 else "PASS"),
                header_status=count[1] if count else None, path_count=len(checks),
                clock_period_ns=clock_period, corner=header[1] if header else None,
                delay=header[2] if header else None,
                launch_count=len(launches), capture_count=len(captures), launch_objects=launches,
                capture_objects=captures, via_objects=vias, errors=errors,
                worst=(dict(arrival_ns=worst["data_arrival_ns"], required_ns=worst["data_required_ns"],
                            slack_ns=worst["slack_ns"], startpoint=paths[checks.index(worst)]["startpoint"] if len(paths) == len(checks) else worst["startpoint"],
                            endpoint=paths[checks.index(worst)]["endpoint"] if len(paths) == len(checks) else worst["endpoint"])
                       if worst else None))


def analyze_timing(run_dir: Path):
    run_dir = Path(run_dir)
    inputs = Inputs(run_dir)
    identity = json.loads((run_dir / "run.json").read_text()) if (run_dir / "run.json").is_file() else {}
    out = dict(schema=SCHEMA, status="INCOMPLETE", identity=dict(run_id=identity.get("databaseId"),
               attempt=identity.get("attempt"), source_sha=identity.get("headSha")),
               rc_coverage="nominal extracted RC only", corners={}, errors=[])
    try:
        audit = structural_audit(run_dir)
        text, ref = selected_input(inputs, "final/nl/tt_um_joshua_vernazza_pinscript.nl.v")
        connection = Connectivity(text)
        out["structural"] = audit
        steps = [step for step in inputs.steps() if step.endswith("-openroad-stapostpnr")]
        if len(steps) != 1:
            raise ValueError(f"expected one final post-PnR STA step, got {steps}")
        for corner in EXPECTED_CORNERS:
            prefix = f"{steps[0]}/{corner}/"
            units_text, units_ref = inputs.run_text(prefix + "m3c-units.rpt", "reported timing/electrical units")
            block = out["corners"][corner] = dict(units=parse_units(units_text or ""), units_source=units_ref, groups={})
            specifications = [(f"{s}-to-{d}", s, d, []) for s, d in PAIRS]
            specifications.append(("pc-via-fetch-to-pc", "pc", "pc", [n for v in audit["fetch_cut"].values() for n in v]))
            for label, source, destination, via in specifications:
                structural = audit["pairs"][label]
                row = block["groups"][label] = dict(requirement=structural["requirement"],
                       category="register-to-port" if destination == "top_pins" else "register-to-register",
                       structural_validation=structural, status="PASS")
                for delay in ("max", "min"):
                    report, report_ref = inputs.run_text(prefix + f"m3c-{label}-{delay}.rpt", "required selected timing report")
                    points, points_ref = inputs.run_text(prefix + f"m3c-{label}-{delay}-points.rpt", "timing path structural validation")
                    measurement = evaluate_report(report or "", points or "", connection, source, destination, via)
                    measurement.update(source_report=report_ref, source_points=points_ref)
                    row[delay] = measurement
                    if label in OPTIONAL_ABSENT:
                        if (measurement["errors"] or measurement["path_count"] or measurement["header_status"] != "NOT_APPLICABLE"
                                or measurement["corner"] != corner or measurement["delay"] != delay):
                            row["status"] = "INCOMPLETE"
                        elif row["status"] != "INCOMPLETE":
                            row["status"] = "NOT_APPLICABLE"
                    elif (measurement["header_status"] != "AVAILABLE" or measurement["path_count"] <= 0
                          or measurement["corner"] != corner or measurement["delay"] != delay):
                        if row["status"] != "FAIL":
                            row["status"] = "INCOMPLETE"
                    elif measurement["status"] != "PASS":
                        if row["status"] != "FAIL":
                            row["status"] = measurement["status"]
                for key in ("launch_count", "capture_count", "launch_objects", "capture_objects", "via_objects"):
                    row[key] = row["max"][key]
            block["status"] = ("PASS" if block["units"]["status"] == "PASS" and all(
                row["status"] in ("PASS", "NOT_APPLICABLE") for row in block["groups"].values()) else "INCOMPLETE")
        if all(block["status"] == "PASS" for block in out["corners"].values()):
            out["status"] = "PASS"
        elif any(row["status"] == "FAIL" for block in out["corners"].values() for row in block["groups"].values()):
            out["status"] = "FAIL"
    except (ValueError, KeyError, OSError) as error:
        out["errors"].append(str(error))
    out["sources"] = list(inputs.registry.values())
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--structural-only", action="store_true")
    args = parser.parse_args()
    result = structural_audit(args.run_dir) if args.structural_only else analyze_timing(args.run_dir)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2) + "\n")
    return 0 if args.structural_only or result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
