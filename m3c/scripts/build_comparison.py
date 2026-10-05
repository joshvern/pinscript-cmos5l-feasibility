#!/usr/bin/env python3
"""Side-by-side comparison of analyzed M3C hosted runs (read-only aggregation).

Inputs per case: the retrieved run directory (scripts/hosted/m3c/retrieve.py) and the JSON
written for it by scripts/hosted/m3c/analyze_run.py. Adds the provenance links the analyzer
does not make: run/attempt/head SHA, the RTL actually implemented (artifact src/*.v) versus
the uploaded export manifest and the private source commit, the per-case configuration
versus the export, and a full key-by-key diff of the two resolved flow configurations.
Everything else is copied from the analyses; a value the analyzer marks UNAVAILABLE stays
UNAVAILABLE here (never zero). No synthesis, timing estimation or remote access.

    PYTHONDONTWRITEBYTECODE=1 .venv/bin/python scripts/hosted/m3c/build_comparison.py \\
        --case as-is reports/m3c/hosted/as-is-37313216627 \\
        --case repair reports/m3c/hosted/repair-37313220214 \\
        --export reports/m3c/hosted/export-01 --source-commit 64e3de7 \\
        --out reports/m3c/hosted/comparison.json --markdown reports/m3c/hosted/comparison.md
"""
from __future__ import annotations

import argparse
from collections import Counter
import fnmatch
import hashlib
import json
from pathlib import Path
import re
import subprocess
import zipfile

from analyze_run import Netlist, OUTPUT_PINS

ROOT = Path(__file__).resolve().parents[3]
CORNERS = ("nom_typ_1p20V_25C", "nom_slow_1p08V_125C", "nom_fast_1p32V_m40C")
CORNER_LABEL = {"nom_typ_1p20V_25C": "typ 1.20 V 25 C", "nom_slow_1p08V_125C": "slow 1.08 V 125 C",
                "nom_fast_1p32V_m40C": "fast 1.32 V -40 C"}
ENGINE_PAIRS = ("pc-to-pc", "memory-to-pc", "store_control-to-pc", "timer-to-pc", "sync2-to-pc", "fifo-to-pc",
                "pc-to-pads", "memory-to-pads", "pc-to-shift", "memory-to-shift", "fifo-to-shift", "sync2-to-shift",
                "pc-to-timer", "memory-to-timer", "pc-to-record", "memory-to-record", "pc-to-fifo",
                "host_address-to-host_capture", "memory-to-host_capture", "pc-to-host_capture")
UNAVAILABLE = "UNAVAILABLE"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def rel(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(ROOT))
    except ValueError:
        return str(path)


def get(node, *keys, default=UNAVAILABLE):
    for key in keys:
        if not isinstance(node, dict) or key not in node:
            return default
        node = node[key]
    return node


class ArtifactView:
    """The implementation job's evidence artifact, including a retained failed run."""

    def __init__(self, label: str, folder: Path | None = None, archive: Path | None = None):
        self.label, self.folder, self.archive = label, folder, archive
        self._zip = zipfile.ZipFile(archive) if archive else None
        self._names = set(self._zip.namelist()) if self._zip else None

    def exists(self, member: str) -> bool:
        return (member in self._names) if self._zip else (self.folder / member).is_file()

    def read(self, member: str) -> bytes:
        return self._zip.read(member) if self._zip else (self.folder / member).read_bytes()

    def glob(self, pattern: str) -> list[str]:
        if self._zip:
            return sorted(n for n in self._names if fnmatch.fnmatchcase(n, pattern))
        return sorted(str(q.relative_to(self.folder)) for q in self.folder.glob(pattern) if q.is_file())

    def where(self, member: str) -> str:
        return f"{self.label}!{member}" if self._zip else rel(self.folder / member)


def gds_artifact(run_dir: Path) -> ArtifactView | None:
    """Find unique implementation provenance even when the flow never reached final/.

    Final metrics remain required by their individual consumers. Requiring them
    here hid valid source/configuration identity for an early failed run.
    """
    found = []
    required = {"evidence/run-identity.json", "runs/wokwi/resolved.json"}
    for identity in (run_dir / "artifacts").glob("*/evidence/run-identity.json"):
        folder = identity.parents[1]
        if all((folder / member).is_file() for member in required):
            found.append(ArtifactView(rel(folder), folder=folder))
    for archive in sorted((run_dir / "artifacts").glob("*.zip")):
        with zipfile.ZipFile(archive) as z:
            names = set(z.namelist())
        if required <= names:
            found.append(ArtifactView(rel(archive), archive=archive))
    return found[0] if len(found) == 1 else None


def provenance(name: str, run_dir: Path, analysis: dict, export: Path | None, commit: str | None) -> dict:
    run = json.loads((run_dir / "run.json").read_text()) if (run_dir / "run.json").exists() else {}
    artifact = gds_artifact(run_dir)
    result = {
        "case": name, "run_dir": rel(run_dir),
        "run_id": run.get("databaseId", UNAVAILABLE), "attempt": run.get("attempt", UNAVAILABLE),
        "head_sha": run.get("headSha", UNAVAILABLE), "conclusion": run.get("conclusion", UNAVAILABLE),
        "url": run.get("url", UNAVAILABLE),
        "jobs": [{k: job.get(k) for k in ("name", "status", "conclusion", "startedAt", "completedAt")}
                 for job in run.get("jobs", [])],
        "artifact_folders": sorted(p.name for p in (run_dir / "artifacts").iterdir() if p.is_dir())
        if (run_dir / "artifacts").is_dir() else UNAVAILABLE,
        "gds_artifact": artifact.label if artifact else UNAVAILABLE,
        "hosted_run_identity": get(analysis, "identity", "hosted_run_identity"),
        "revisions": get(analysis, "identity", "revisions"),
        "tool_versions": get(analysis, "identity", "tool_versions"),
        "librelane_python_package": get(analysis, "identity", "librelane_python_package"),
        "librelane_image_digests": get(analysis, "identity", "librelane_image_digests"),
        "pdk_stdcell_liberty_sha256": get(analysis, "identity", "pdk_stdcell_liberty_sha256 (hosted)"),
        "integrity_vs_hosted_manifest": {k: v for k, v in get(analysis, "integrity_vs_hosted_manifest",
                                                                default={}).items() if not isinstance(v, (list, dict))},
    }
    checks = []
    hosted = result["hosted_run_identity"] if isinstance(result["hosted_run_identity"], dict) else {}
    checks.append(("run id in evidence equals run.json", str(hosted.get("GITHUB_RUN_ID")) == str(result["run_id"])))
    checks.append(("attempt in evidence equals run.json", str(hosted.get("GITHUB_RUN_ATTEMPT")) == str(result["attempt"])))
    checks.append(("evidence GITHUB_SHA equals run head SHA", hosted.get("GITHUB_SHA") == result["head_sha"]))
    checks.append(("evidence CANDIDATE is m3c-<case>", hosted.get("CANDIDATE") == f"m3c-{name}"))
    if artifact:
        src = {Path(m).name: hashlib.sha256(artifact.read(m)).hexdigest() for m in artifact.glob("src/*.v")}
        result["implemented_rtl_sha256"] = src
        if export is not None:
            manifest = {}
            for line in (export / "SOURCE_MANIFEST.sha256").read_text().splitlines():
                digest, path = line.split("  ", 1)
                manifest[path] = digest
            exported = {Path(p).name: d for p, d in manifest.items() if p.startswith("src/") and p.endswith(".v")}
            checks.append(("implemented src/*.v equal the uploaded export manifest", src == exported))
            if artifact.exists("src/config.json"):
                implemented_config = hashlib.sha256(artifact.read("src/config.json")).hexdigest()
                result["implemented_config_sha256"] = implemented_config
                checks.append(("implemented src/config.json equals export cases/<case>/config.json",
                               implemented_config == manifest.get(f"cases/{name}/config.json")))
            if artifact.exists("m3c/SOURCE_MANIFEST.sha256"):
                checks.append(("artifact m3c/SOURCE_MANIFEST.sha256 equals local export manifest",
                               hashlib.sha256(artifact.read("m3c/SOURCE_MANIFEST.sha256")).hexdigest()
                               == sha256(export / "SOURCE_MANIFEST.sha256")))
        if commit is not None:
            committed = {}
            for name_v in src:
                show = subprocess.run(["git", "-C", str(ROOT), "show", f"{commit}:src/{name_v}"], capture_output=True)
                committed[name_v] = hashlib.sha256(show.stdout).hexdigest() if show.returncode == 0 else None
            checks.append((f"implemented src/*.v equal private commit {commit}", committed == src))
    result["checks"] = [{"check": text, "ok": bool(ok)} for text, ok in checks]
    return result


def resolved_config(run_dir: Path) -> dict | None:
    artifact = gds_artifact(run_dir)
    if not artifact or not artifact.exists("runs/wokwi/resolved.json"):
        return None
    return json.loads(artifact.read("runs/wokwi/resolved.json"))


def clock_topology(run_dir: Path) -> dict:
    """Physical final-netlist load counts include repair diodes and CTS dummy loads.

    Counts supplement, rather than replace, corner-specific Liberty checks.
    The mapped IHP output pin vocabulary is shared with the existing analyzer.
    """
    artifact = gds_artifact(run_dir)
    members = artifact.glob("runs/wokwi/final/nl/*.v") if artifact else []
    if len(members) != 1:
        return {"status": UNAVAILABLE, "reason": "one final routed netlist required"}
    data = artifact.read(members[0])
    nl = Netlist(data.decode())
    buffers = [name for name in nl.cell if name.startswith("clkbuf_")]
    rows = []
    for cell in sorted(buffers):
        pins = nl.pins[cell]
        outputs = [(pin, net) for pin, net in pins.items() if pin in OUTPUT_PINS]
        if len(outputs) != 1:
            return {"status": UNAVAILABLE, "reason": f"ambiguous clock-buffer output: {cell}"}
        pin, net = outputs[0]
        loads = [(inst, port) for inst, port in nl.nets[net] if (inst, port) != (cell, pin)]
        kinds = Counter(nl.load_kind(inst) for inst, _ in loads)
        count = len(loads) + len(nl.port_loads(net))
        rows.append({"cell": cell, "net": net, "leaf": cell.startswith("clkbuf_leaf_"),
                     "loads": count, "load_kinds": dict(kinds), "margin_to_limit_8": 8 - count})
    leaves = [row for row in rows if row["leaf"]]
    return {"status": "AVAILABLE" if leaves else "INCOMPLETE", "source": artifact.where(members[0]),
            "sha256": hashlib.sha256(data).hexdigest(), "tree_buffer_count": len(rows),
            "leaf_buffer_count": len(leaves), "leaf_load_histogram": dict(sorted(Counter(r["loads"] for r in leaves).items())),
            "all_clock_buffer_load_histogram": dict(sorted(Counter(r["loads"] for r in rows).items())),
            "worst_leaf_loads": max((r["loads"] for r in leaves), default=None),
            "worst_leaf_margin_to_limit_8": min((r["margin_to_limit_8"] for r in leaves), default=None),
            "antenna_loads": sum(r["load_kinds"].get("antenna_diode", 0) for r in rows),
            "dummy_loads": sum(r["load_kinds"].get("cts_dummy_load", 0) for r in rows),
            "buffers": rows,
            "boundary": "Final physical load-pin count; authoritative corner checks use Liberty fanout_load/default_max_fanout."}


def config_diff(a: dict | None, b: dict | None, names=("as-is", "repair")) -> dict:
    if a is None or b is None:
        return {"status": UNAVAILABLE}
    keys = sorted(set(a) | set(b))
    differing = {k: {names[0]: a.get(k, "<absent>"), names[1]: b.get(k, "<absent>")}
                 for k in keys if (k in a) != (k in b) or a.get(k) != b.get(k)}
    return {"keys_compared": len(keys), "differing_keys": differing}


def warnings(run_dir: Path) -> dict:
    artifact = gds_artifact(run_dir)
    if not artifact:
        return {"status": UNAVAILABLE}
    out = {}
    for member in artifact.glob("runs/wokwi/*warning*.log"):
        data = artifact.read(member)
        lines = [line.strip() for line in data.decode(errors="replace").splitlines() if line.strip()]
        out[artifact.where(member)] = {"sha256": hashlib.sha256(data).hexdigest(), "lines": len(lines),
                                       "unique": sorted(set(lines))[:80]}
    return out or {"status": "no runs/wokwi/*warning*.log found"}


def overflow_metrics(run_dir: Path) -> dict:
    artifact = gds_artifact(run_dir)
    if not artifact or not artifact.exists("runs/wokwi/final/metrics.json"):
        return {"status": UNAVAILABLE, "reason": "final metrics required"}
    metrics = json.loads(artifact.read("runs/wokwi/final/metrics.json"))
    keys = {k: v for k, v in metrics.items() if re.search(r"overflow|congestion", k)}
    return keys or {"status": "no overflow/congestion keys in final/metrics.json (see the global-routing log excerpt)"}


STEP_LOG_PATTERNS = {
    # step-name suffix: line regexes worth keeping verbatim (the raw log stays authoritative)
    "openroad-globalrouting": [r"GRT-0096", r"^(Metal\d|Total)\s+\d", r"GRT-0018", r"GRT-0014", r"overflow"],
    "openroad-detailedrouting": [r"DRT-0199", r"Number of violations", r"Total wire length", r"diode", r"jumper"],
    "openroad-cts": [r"[Cc]lustering", r"Number of [Ss]inks", r"[Bb]uffers? inserted", r"Total number of",
                     r"[Ll]eaf", r"CTS-00(?:18|24|98|99)"],
    "openroad-repairdesignpostgpl": [r"RSZ-00(?:27|34|35|36|37|38|39|40|41|43|46)", r"[Ff]ound \d+", r"[Ii]nserted",
                                     r"max_wire_length", r"repair_design"],
    "openroad-repairantennas": [r"[Ii]nserted", r"diode", r"jumper", r"ANT-", r"GRT-0(?:012|015)"],
}


def step_logs(run_dir: Path) -> dict:
    """Verbatim key lines from the largest log of selected physical-flow steps (location + sha256)."""
    artifact = gds_artifact(run_dir)
    if not artifact:
        return {"status": UNAVAILABLE}
    step_names = sorted({m.split("/")[2] for m in artifact.glob("runs/wokwi/*/*") if re.match(r"\d+-", m.split("/")[2])},
                        key=lambda n: int(n.split("-", 1)[0]))
    out = {}
    for suffix, patterns in STEP_LOG_PATTERNS.items():
        matches = [n for n in step_names if n.split("-", 1)[1] == suffix]
        if not matches:
            out[suffix] = {"status": "step not present"}
            continue
        for step in matches:
            logs = artifact.glob(f"runs/wokwi/{step}/*.log") + artifact.glob(f"runs/wokwi/{step}/*/*.log")
            if not logs:
                out[step] = {"status": "no .log in step directory"}
                continue
            sizes = {m: len(artifact.read(m)) for m in logs}
            log = max(logs, key=lambda m: sizes[m])
            data = artifact.read(log)
            kept = [line.rstrip() for line in data.decode(errors="replace").splitlines()
                    if any(re.search(pattern, line) for pattern in patterns)]
            # Keep the beginning and the end (final iteration/result) of long excerpts.
            shown = kept if len(kept) <= 80 else kept[:40] + ["... (%d lines omitted) ..." % (len(kept) - 80)] + kept[-40:]
            out[step] = {"log": artifact.where(log), "sha256": hashlib.sha256(data).hexdigest(), "lines": shown,
                         "lines_matched": len(kept), "other_logs": [m for m in logs if m != log]}
    return out


def register_min_period(run_dir: Path) -> dict:
    """OpenSTA report_clock_min_period from the post-PnR STA step (internal register paths;
    port paths excluded by default). A nominal-RC tool estimate, not a silicon Fmax claim."""
    artifact = gds_artifact(run_dir)
    if not artifact:
        return {"status": UNAVAILABLE}
    steps = sorted({m.split("/")[2] for m in artifact.glob("runs/wokwi/*-openroad-stapostpnr/*/clock.rpt")},
                   key=lambda n: int(n.split("-", 1)[0]))
    if not steps:
        return {"status": "no post-PnR clock.rpt"}
    out = {"step": steps[-1]}
    for corner in CORNERS:
        member = f"runs/wokwi/{steps[-1]}/{corner}/clock.rpt"
        if not artifact.exists(member):
            out[corner] = UNAVAILABLE
            continue
        data = artifact.read(member)
        found = re.findall(r"(\S+) period_min = ([\d.]+) fmax = ([\d.]+)", data.decode(errors="replace"))
        out[corner] = {"source": artifact.where(member), "sha256": hashlib.sha256(data).hexdigest(),
                       "clocks": [{"clock": c, "period_min_ns": float(p), "fmax_mhz": float(f)} for c, p, f in found]}
    return out


def flow_checker_scope(run_dir: Path) -> dict:
    """Which corners the flow's own checkers gate (job success is not a multi-corner gate)."""
    resolved = resolved_config(run_dir) or {}
    keys = ("TIMING_VIOLATION_CORNERS", "SETUP_VIOLATION_CORNERS", "HOLD_VIOLATION_CORNERS",
            "MAX_SLEW_VIOLATION_CORNERS", "MAX_CAP_VIOLATION_CORNERS")
    return {k: resolved.get(k, "<absent>") for k in keys} if resolved else {"status": UNAVAILABLE}


def precheck_notes(run_dir: Path) -> dict:
    """Precheck cases that checked nothing (KLayout 'NO-Check' layer tables) and per-deck totals."""
    logs = sorted((run_dir / "logs").glob("precheck-*.log"))
    if not logs:
        return {"status": UNAVAILABLE}
    text = logs[0].read_text(errors="replace")
    no_check = [line.split("Z ", 1)[-1].strip() for line in text.splitlines() if "NO-Check" in line]
    totals = [line.split("Z ", 1)[-1].strip() for line in text.splitlines() if "total error(s) among" in line]
    running = [line.split("Z ", 1)[-1].strip() for line in text.splitlines() if "INFO: Running" in line]
    return {"log": rel(logs[0]), "sha256": sha256(logs[0]), "no_check_lines": no_check,
            "deck_totals": totals, "checks_run": running}


def corner_timing(analysis: dict) -> dict:
    out = {}
    for corner in CORNERS:
        c = get(analysis, "timing", "corners", corner, default={})
        out[corner] = {key: get(c, key) for key in ("worst_setup_slack_ns", "worst_hold_slack_ns", "setup_tns_ns",
                                                     "hold_tns_ns", "setup_violating_endpoints",
                                                     "hold_violating_endpoints")}
        for kind in ("worst_setup_path", "worst_hold_path"):
            path = get(c, kind, default={})
            out[corner][kind] = {k: get(path, k) for k in ("startpoint", "endpoint", "data_arrival_ns",
                                                            "data_required_ns", "slack_ns")} if isinstance(path, dict) else path
    return out


def engine_paths(analysis: dict) -> dict:
    pairs = get(analysis, "representative_paths", "pairs", default={})
    out = {}
    for pair in ENGINE_PAIRS:
        entry = pairs.get(pair) if isinstance(pairs, dict) else None
        if not entry:
            out[pair] = UNAVAILABLE
            continue
        out[pair] = {}
        for corner in CORNERS:
            for delay in ("max", "min"):
                r = get(entry, "corners", corner, delay, default={})
                if get(r, "status") != "AVAILABLE":
                    out[pair][f"{corner}/{delay}"] = get(r, "status") if isinstance(r, dict) else UNAVAILABLE
                    continue
                out[pair][f"{corner}/{delay}"] = {
                    "slack_ns": get(r, "slack_ns"), "startpoint": get(r, "startpoint"), "endpoint": get(r, "endpoint"),
                    "data_arrival_ns": get(r, "data_arrival_ns"), "data_required_ns": get(r, "data_required_ns"),
                    "q_to_d_ns": get(r, "points", "q_to_d_ns"), "launch_Q_pins": get(r, "launch_Q_pins"),
                    "capture_D_pins": get(r, "capture_D_pins")}
    return out


def electrical(analysis: dict) -> dict:
    out = {"corners": {}, "across_corners": get(analysis, "electrical_rules", "across_corners")}
    for corner in CORNERS:
        c = get(analysis, "electrical_rules", "corners", corner, default={})
        row = {}
        for kind in ("max_fanout", "max_slew", "max_capacitance"):
            k = get(c, kind, default={})
            rows = get(k, "rows", default=[])
            worst = None
            if isinstance(rows, list) and rows:
                slacks = [r.get("slack") for r in rows if isinstance(r.get("slack"), (int, float))]
                worst_row = min(rows, key=lambda r: r.get("slack") if isinstance(r.get("slack"), (int, float)) else 0)
                worst = {"min_slack": min(slacks) if slacks else UNAVAILABLE,
                         "row": {x: worst_row.get(x) for x in ("pin", "net", "driver_pin", "driver_cell", "value",
                                                               "limit", "slack")}}
            row[kind] = {"rows_listed": get(k, "rows_listed"), "unique_nets": get(k, "unique_nets"),
                         "groups": get(k, "groups", default=None), "worst": worst,
                         "nets_with_antenna_diode_loads": get(k, "nets_with_antenna_diode_loads", default=None)}
        row["unannotated_drivers"] = get(c, "unannotated_drivers")
        row["check_setup_output_lines"] = get(c, "check_setup_output_lines")
        out["corners"][corner] = row
    nets = get(analysis, "electrical_rules", "violating_nets", default={})
    if isinstance(nets, dict):
        out["violating_nets"] = {n: {k: v.get(k) for k in ("driver_pin", "driver_cell", "load_pins", "antenna_diode_loads",
                                                            "cts_dummy_loads")} for n, v in nets.items()}
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--case", nargs=2, action="append", metavar=("NAME", "RUN_DIR"), required=True)
    parser.add_argument("--analysis-name", default="analysis.json", help="analysis JSON inside each RUN_DIR")
    parser.add_argument("--export", type=Path)
    parser.add_argument("--case-export", nargs=2, action="append", default=[], metavar=("NAME", "EXPORT"),
                        help="override export provenance for a later candidate without mixing historical manifests")
    parser.add_argument("--supplement", nargs=3, action="append", default=[], metavar=("NAME", "KIND", "JSON"),
                        help="separate later evidence: project_acceptance, corrected_timing, supplemental_pin_labels")
    parser.add_argument("--source-commit")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--markdown", type=Path)
    args = parser.parse_args()
    if len({name for name, _ in args.case}) != len(args.case):
        parser.error("case labels must be unique")
    case_exports = {name: Path(path) for name, path in args.case_export}
    cases = {}
    for name, run_dir in args.case:
        run_dir = Path(run_dir)
        analysis_path = run_dir / args.analysis_name
        analysis = json.loads(analysis_path.read_text())
        cases[name] = {
            "analysis": {"path": rel(analysis_path), "sha256": sha256(analysis_path), "schema": analysis.get("schema")},
            "provenance": provenance(name, run_dir, analysis, case_exports.get(name, args.export), args.source_commit),
            "resolved_config_essentials": get(analysis, "identity", "resolved_config"),
            "area": {"synthesis": {k: get(analysis, "area", "synthesis", k) for k in (
                         "mapped_area_total_um2", "mapped_area_sequential_um2", "mapped_area_combinational_um2",
                         "mapped_instance_count", "flip_flop_count", "flip_flop_types", "top15_cell_types_by_count")},
                     "routed": {k: get(analysis, "area", "routed", k) for k in (
                         "core_area_um2", "routed_stdcell_area_um2", "utilization_percent", "metric_keys")}},
            "timing": corner_timing(analysis),
            "timing_note": get(analysis, "timing", "note"),
            "register_min_period": register_min_period(run_dir),
            "flow_checker_scope": flow_checker_scope(run_dir),
            "precheck_notes": precheck_notes(run_dir),
            "engine_paths": engine_paths(analysis),
            "register_group_inventory": get(analysis, "representative_paths", "register_group_inventory"),
            "hook_reporting_errors": get(analysis, "representative_paths", "hook_reporting_errors"),
            "electrical": electrical(analysis),
            "clock_topology": clock_topology(run_dir),
            "physical": {"summary": get(analysis, "physical_checks", "summary"),
                         "metric_keys_used": get(analysis, "physical_checks", "metric_keys_used"),
                         "overflow_congestion_metrics": overflow_metrics(run_dir)},
            "precheck": get(analysis, "precheck_and_gate_tests", "precheck"),
            "gate_tests": get(analysis, "precheck_and_gate_tests", "gate_tests"),
            "flow_warnings": warnings(run_dir),
            "physical_flow_step_excerpts": step_logs(run_dir),
            "unavailable_items": get(analysis, "unavailable_items"),
            "project_acceptance": json.loads((run_dir / "acceptance.json").read_text())
                if (run_dir / "acceptance.json").exists() else {"status": "NOT EVALUATED"},
            "corrected_timing": json.loads((run_dir / "timing-coverage.json").read_text())
                if (run_dir / "timing-coverage.json").exists() else {"status": "INCOMPLETE (historical selector)"},
            "supplemental_pin_labels": json.loads((run_dir / "pin-label.json").read_text())
                if (run_dir / "pin-label.json").exists() else {"status": "NOT RUN"},
        }
    for name, kind, path in args.supplement:
        if name not in cases or kind not in {"project_acceptance", "corrected_timing", "supplemental_pin_labels"}:
            parser.error("unknown supplement case or kind")
        cases[name][kind] = json.loads(Path(path).read_text())
    names = list(cases)
    comparison = {
        "schema": "pinscript-m3c-hosted-comparison/1",
        "boundary": ("Hosted LibreLane implementation evidence for the M3C production RTL. Nominal extracted RC at "
                     "three library PVT corners only; functional gate tests are untimed (no SDF). A successful "
                     "workflow is not electrical-rule closure or signoff."),
        "cases": cases,
    }
    if len(names) == 2:
        a, b = (resolved_config(Path(dict(args.case)[n])) for n in names)
        comparison["resolved_config_diff"] = config_diff(a, b, names)
    elif len(names) > 2:
        base = resolved_config(Path(dict(args.case)[names[0]]))
        comparison["resolved_config_diffs_from_baseline"] = {
            name: config_diff(base, resolved_config(Path(dict(args.case)[name])), (names[0], name))
            for name in names[1:]}
    args.out.write_text(json.dumps(comparison, indent=2, sort_keys=False, default=str) + "\n")
    if args.markdown:
        args.markdown.write_text(markdown(comparison))
    print(f"wrote {rel(args.out)}" + (f" and {rel(args.markdown)}" if args.markdown else ""))


def fmt(value, digits=6):
    if value is None or (isinstance(value, str) and value.startswith("<absent")):
        return UNAVAILABLE
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def markdown(comparison: dict) -> str:
    cases = comparison["cases"]
    names = list(cases)
    lines = [f"# M3C integrated CMOS5L comparison ({' vs '.join(names)})", "", comparison["boundary"], ""]
    header = "| Quantity | " + " | ".join(names) + " |"
    sep = "| --- |" + " --- |" * len(names)

    def table(rows):
        if lines and lines[-1]:
            lines.append("")
        lines.extend([header, sep] + [f"| {label} | " + " | ".join(fmt(v) for v in values) + " |" for label, values in rows] + [""])

    prov = {n: cases[n]["provenance"] for n in names}
    lines.append("## Provenance")
    table([("run id / attempt", [f"{p['run_id']} / {p['attempt']}" for p in prov.values()]),
           ("head SHA", [p["head_sha"] for p in prov.values()]),
           ("conclusion", [p["conclusion"] for p in prov.values()]),
           ("jobs", ["; ".join(f"{j['name']}={j['conclusion']}" for j in p["jobs"]) for p in prov.values()]),
           ("provenance checks", ["; ".join(f"{c['check']}: {'OK' if c['ok'] else 'FAIL'}" for c in p["checks"])
                                  for p in prov.values()])])
    diff = comparison.get("resolved_config_diff", {})
    if "differing_keys" in diff:
        lines.append(f"Resolved configuration: {diff['keys_compared']} keys compared; differing keys:")
        lines.append("")
        lines.extend([f"- `{k}`: {v[names[0]]!r} → {v[names[1]]!r}" for k, v in diff["differing_keys"].items()] + [""])
    for name, difference in comparison.get("resolved_config_diffs_from_baseline", {}).items():
        lines.append(f"Resolved configuration vs {names[0]}: {name} ({difference.get('keys_compared', UNAVAILABLE)} keys).")
        lines.append("")
        lines.extend(f"- `{key}`: {values[names[0]]!r} → {values[name]!r}"
                     for key, values in difference.get("differing_keys", {}).items())
        lines.append("")
    lines.append("## Area")
    table([(label, [get(cases[n]["area"], *path) for n in names]) for label, path in (
        ("mapped total (µm²)", ("synthesis", "mapped_area_total_um2")),
        ("mapped sequential (µm²)", ("synthesis", "mapped_area_sequential_um2")),
        ("mapped combinational (µm²)", ("synthesis", "mapped_area_combinational_um2")),
        ("mapped instances", ("synthesis", "mapped_instance_count")),
        ("flip-flops", ("synthesis", "flip_flop_count")),
        ("core area (µm²)", ("routed", "core_area_um2")),
        ("routed non-filler std-cell area (µm²)", ("routed", "routed_stdcell_area_um2")),
        ("utilization (%)", ("routed", "utilization_percent")))])
    lines.append("## Setup / hold by corner (worst slack ns, violating endpoints)")
    rows = []
    for corner in CORNERS:
        for kind, key, count in (("setup", "worst_setup_slack_ns", "setup_violating_endpoints"),
                                 ("hold", "worst_hold_slack_ns", "hold_violating_endpoints")):
            rows.append((f"{CORNER_LABEL[corner]} {kind}", [f"{fmt(get(cases[n]['timing'], corner, key))} "
                                                             f"({fmt(get(cases[n]['timing'], corner, count))})" for n in names]))
    table(rows)
    lines.append("## Internal register paths: OpenSTA report_clock_min_period (post-PnR, port paths excluded)")
    rows = []
    for corner in CORNERS:
        vals = []
        for n in names:
            entry = get(cases[n]["register_min_period"], corner)
            clocks = get(entry, "clocks", default=[]) if isinstance(entry, dict) else []
            vals.append("; ".join(f"{c['clock']} {c['period_min_ns']} ns" for c in clocks) or UNAVAILABLE)
        rows.append((CORNER_LABEL[corner], vals))
    table(rows)
    lines.append("## Electrical rules by corner (report rows / unique nets; worst slack)")
    rows = []
    for corner in CORNERS:
        for kind in ("max_fanout", "max_slew", "max_capacitance"):
            vals = []
            for n in names:
                e = get(cases[n]["electrical"], "corners", corner, kind, default={})
                worst = get(e, "worst", default=None)
                worst_text = fmt(get(worst, "min_slack")) if isinstance(worst, dict) else "—"
                vals.append(f"{get(e, 'rows_listed')} / {get(e, 'unique_nets')}; worst {worst_text}")
            rows.append((f"{CORNER_LABEL[corner]} {kind}", vals))
    table(rows)
    lines.append("## Final clock-tree physical load inventory")
    table([(label, [get(cases[n], "clock_topology", key) for n in names]) for label, key in (
        ("Tree / CTS buffer count", "tree_buffer_count"), ("Leaf buffer count", "leaf_buffer_count"),
        ("Leaf load histogram (loads: number of buffers)", "leaf_load_histogram"),
        ("Worst leaf load count", "worst_leaf_loads"), ("Worst leaf margin to 8 loads", "worst_leaf_margin_to_limit_8"),
        ("Clock-net antenna-diode loads", "antenna_loads"), ("Clock-net CTS dummy loads", "dummy_loads"))])
    lines.append("## Physical checks")
    keys = ("route_drc_errors", "magic_drc_errors", "lvs_result", "antenna_violating_nets", "antenna_violating_pins",
            "klayout_drc_errors_in_flow", "klayout_xor", "power_grid_violations", "disconnected_pins",
            "critical_disconnected_pins")
    table([(k, [get(cases[n]["physical"], "summary", k) for n in names]) for k in keys])
    lines.append("## Precheck and functional gate tests")
    table([("precheck", [f"{get(cases[n]['precheck'], 'overall')} ({get(cases[n]['precheck'], 'overall_basis')})" for n in names]),
           ("gate tests", [f"{get(cases[n]['gate_tests'], 'overall')} ({get(cases[n]['gate_tests'], 'overall_basis')})" for n in names])])
    lines.extend(["Job failure alone does not establish a failed functional test case. "
                  "Missing test reports remain incomplete; check run step metadata and retained logs for execution status.", ""])
    lines.append("## Project acceptance and required supplemental coverage")
    table([(label, [get(cases[n], key, 'status') for n in names]) for label, key in (
        ("Strict project acceptance", "project_acceptance"),
        ("Corrected engine/register timing", "corrected_timing"),
        ("Supplemental CMOS5L pin-label rule", "supplemental_pin_labels"))])
    if any("corners" in cases[n]["corrected_timing"] for n in names):
        lines.append("## Corrected selected paths (setup / hold slack, ns)")
        lines.append("Historical replay values require its recorded equivalence checks to pass. "
                     "I/O-dominated headlines remain in the global table above. Complete selections, "
                     "path objects, arrival/required times and source reports remain in the JSON supplements.")
        lines.append("")
        groups = sorted({label for n in names for corner in CORNERS
                         for label in get(cases[n]["corrected_timing"], "corners", corner, "groups", default={})})
        rows = []
        for corner in CORNERS:
            for label in groups:
                values = []
                for name in names:
                    evidence = cases[name]["corrected_timing"]
                    block = get(evidence, "corners", corner, default={})
                    group = get(block, "groups", label, default={})
                    if group.get("requirement") == "NOT_APPLICABLE":
                        value = "NOT APPLICABLE (pad registers intervene)"
                    elif evidence.get("status") != "PASS" or block.get("status") != "PASS":
                        value = "INCOMPLETE"
                    else:
                        value = " / ".join(fmt(get(group, delay, "worst", "slack_ns")) for delay in ("max", "min"))
                    values.append(value)
                rows.append((f"{CORNER_LABEL[corner]} {label}", values))
        table(rows)
    return "\n".join(lines).rstrip() + "\n"


if __name__ == "__main__":
    main()
