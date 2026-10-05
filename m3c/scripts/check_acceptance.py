#!/usr/bin/env python3
"""Fail-closed project acceptance for a single retained M3C hosted run.

This is deliberately separate from upstream job outcomes. Exit 0 means PASS in
the evaluated nominal-RC/untimed-gate flow, 1 means FAIL, and 2 INCOMPLETE. The
output is written for every outcome. It never modifies or extracts artifacts.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import re
import sys

import analyze_run as analyzer

SCHEMA = "pinscript-project-acceptance/1"
CONTRACT_SCHEMA = "pinscript-acceptance-contract/1"
CORNERS = tuple(analyzer.EXPECTED_CORNERS)
# STA report formatting and the flow's scalar metric conversion differ by up to
# 0.0000017 ns in the preserved repair run. Compare at 0.00001 ns (10 fs), while
# independently requiring both reported and metric slack to be nonnegative.
TIMING_REPORT_TOLERANCE_NS = 0.00001
PHYSICAL_ZERO = (
    "route__drc_errors", "antenna__violating__nets", "antenna__violating__pins",
    "route__antenna_violation__count", "magic__drc_error__count",
    "magic__illegal_overlap__count", "design__lvs_error__count",
    "design__lvs_device_difference__count", "design__lvs_net_difference__count",
    "design__lvs_property_fail__count", "design__lvs_unmatched_device__count",
    "design__lvs_unmatched_net__count", "design__lvs_unmatched_pin__count",
    "design__power_grid_violation__count", "design__critical_disconnected_pin__count",
    "timing__drv__floating__nets", "timing__drv__floating__pins",
)
GATE_NAMES = {"test.test_project", "test_gl_engine.gl_uart_two_bytes",
              "test_gl_engine.gl_handshake_fast"}
MAPPED_FIELDS = ("mapped_area_total_um2", "mapped_area_sequential_um2", "mapped_area_combinational_um2",
                 "stat_rpt_chip_area_um2", "mapped_instance_count", "flip_flop_count", "latch_count", "cell_types")
CHECKOUT = "/home/runner/work/pinscript-cmos5l-feasibility/pinscript-cmos5l-feasibility"


def digest(data):
    return hashlib.sha256(data).hexdigest()


def normalize_config(value):
    """Only normalize machine-location prefixes; never mask a configuration key."""
    if isinstance(value, str):
        return value.replace(CHECKOUT + "/", "<checkout>/").replace("/home/runner/pdk/", "<pdk>/")
    if isinstance(value, dict):
        return {key: normalize_config(item) for key, item in value.items()}
    if isinstance(value, list):
        return [normalize_config(item) for item in value]
    return value


class Verdict:
    def __init__(self):
        self.checks = []

    def add(self, key, status, detail, **evidence):
        self.checks.append(dict(check=key, status=status, detail=detail, **evidence))

    def equal(self, key, actual, expected):
        if actual is None or expected is None:
            self.add(key, "INCOMPLETE", "Required evidence or expected contract value missing", actual=actual, expected=expected)
        else:
            self.add(key, "PASS" if actual == expected else "FAIL", "Exact comparison", actual=actual, expected=expected)

    def count(self, key, value, expected=0):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0 or int(value) != value:
            self.add(key, "INCOMPLETE", "Required nonnegative integer count missing or invalid", actual=value)
        else:
            self.equal(key, value, expected)

    def finite(self, key, value, minimum=None):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            self.add(key, "INCOMPLETE", "Required finite metric missing or invalid", actual=value)
        else:
            self.add(key, "PASS" if minimum is None or value >= minimum else "FAIL", "Finite metric", actual=value, minimum=minimum)

    def nonempty(self, key, value):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            self.add(key, "INCOMPLETE", "Required nonempty collection or object coverage missing", actual=value)
        else:
            self.add(key, "PASS", "Nonempty required evidence", actual=value)

    def passing(self, key, value, expected="PASS"):
        if value in (None, "INCOMPLETE", "UNAVAILABLE"):
            self.add(key, "INCOMPLETE", "Required passing evidence unavailable", actual=value)
        else:
            self.equal(key, value, expected)

    @property
    def status(self):
        statuses = {item["status"] for item in self.checks}
        return "FAIL" if "FAIL" in statuses else "INCOMPLETE" if "INCOMPLETE" in statuses or not statuses else "PASS"


class Evidence:
    """Read selected members, requiring duplicate copies to be identical."""
    def __init__(self, run_dir, verdict):
        self.inputs = analyzer.Inputs(run_dir)
        self.verdict = verdict

    def select(self, suffix):
        entries = [e for e in self.inputs.entries if e.rel == suffix or e.rel.endswith("/" + suffix)]
        if not entries:
            self.verdict.add("evidence." + suffix, "INCOMPLETE", "Required artifact member missing")
            return None, None
        records = [self.inputs.read(e, "strict acceptance: " + suffix) for e in entries]
        if len({ref["sha256"] for _, ref in records}) != 1:
            self.verdict.add("evidence." + suffix, "FAIL", "Conflicting copies; possibly mixed runs or attempts", sources=[r for _, r in records])
        return records[0]

    def json(self, suffix):
        data, source = self.select(suffix)
        if data is None:
            return {}, source
        try:
            return json.loads(data), source
        except (ValueError, UnicodeError) as exc:
            self.verdict.add("parse." + suffix, "INCOMPLETE", str(exc))
            return {}, source

    def latest(self, needle, filename):
        steps = sorted({p.split("/", 1)[0] for p in self.inputs.run_files()
                        if needle in p.split("/", 1)[0]}, key=analyzer.step_sort_key)
        if not steps:
            self.verdict.add("evidence." + needle, "INCOMPLETE", "Required final flow step missing")
            return None, None
        return self.select("runs/wokwi/" + steps[-1] + "/" + filename)


def make_contract(run_dir, candidate="m3c-cts-only"):
    verdict = Verdict()
    raw = Evidence(run_dir, verdict)
    analysis = analyzer.analyze(run_dir)
    config, config_ref = raw.json("runs/wokwi/resolved.json")
    manifest, manifest_ref = raw.json("evidence/artifact-sha256.json")
    identity = analysis["identity"]
    mapped, mapped_ref = raw.latest("yosys-synthesis", config.get("DESIGN_NAME", "UNKNOWN") + ".nl.v")
    if not config or not manifest or verdict.status != "PASS" and verdict.checks:
        raise ValueError("Cannot derive a contract without unambiguous baseline configuration/manifest")
    return dict(schema=CONTRACT_SCHEMA, repository="joshvern/pinscript-cmos5l-feasibility",
                source_sha=None, run_id=None, attempt="1", candidate=candidate,
                baseline=dict(run_id=str(identity["run"]["databaseId"]), source_sha=identity["run"]["headSha"],
                              resolved_config_source=config_ref, manifest_source=manifest_ref),
                baseline_resolved_config=config,
                synthesis_netlist_sha256=digest(mapped) if mapped is not None else None,
                baseline_synthesis={k: analysis.get("area", {}).get("synthesis", {}).get(k) for k in MAPPED_FIELDS},
                baseline_geometry={k: analysis.get("area", {}).get("routed", {}).get("metric_keys", {}).get(k)
                                   for k in ("design__die__bbox", "design__core__bbox")},
                implementation_changes={"CTS_SINK_CLUSTERING_SIZE": 6},
                reporting_config_keys=["STA_EXTRA_CORNER_TCL_FILE"],
                rtl_sha256={k: v for k, v in manifest.items() if k.startswith("src/") and k.endswith(".v")},
                revisions={k: identity["revisions"][k] for k in ("pdk", "support")},
                tool_versions=identity["tool_versions"],
                librelane_image_digests=identity["librelane_image_digests"],
                librelane_python_package=identity["librelane_python_package"],
                liberty_sha256=identity["pdk_stdcell_liberty_sha256 (hosted)"])


def check_identity(v, analysis, contract, resolved, rtl_hashes):
    v.equal("contract.schema", contract.get("schema"), CONTRACT_SCHEMA)
    identity = analysis.get("identity", {})
    hosted = identity.get("hosted_run_identity", {})
    run = identity.get("run", {})
    for key, field in (("repository", "GITHUB_REPOSITORY"), ("source_sha", "GITHUB_SHA"),
                       ("run_id", "GITHUB_RUN_ID"), ("attempt", "GITHUB_RUN_ATTEMPT"), ("candidate", "CANDIDATE")):
        v.equal("identity." + key, hosted.get(field), contract.get(key))
    v.equal("identity.api_run", str(run.get("databaseId")) if run.get("databaseId") else None, contract.get("run_id"))
    v.equal("identity.api_sha", run.get("headSha"), contract.get("source_sha"))
    v.equal("identity.api_attempt", str(run["attempt"]) if run.get("attempt") is not None else None, contract.get("attempt"))
    v.equal("identity.source_revision", identity.get("revisions", {}).get("source"), contract.get("source_sha"))
    v.equal("identity.final_commit", identity.get("final_commit_id", {}).get("commit"), contract.get("source_sha"))
    v.equal("identity.implementation", hosted.get("IMPLEMENTATION_OUTCOME"), "success")
    for name in ("gds", "precheck", "gl_test"):
        jobs = [j for j in identity.get("jobs", []) if j.get("name") == name]
        v.count("jobs." + name + ".unique", len(jobs), 1)
        if len(jobs) == 1:
            v.equal("jobs." + name + ".conclusion", jobs[0].get("conclusion"), "success")
    # The aggregate workflow is intentionally not required complete: this job is
    # part of that workflow and runs after the three required diagnostic jobs.
    for key in ("tool_versions", "librelane_image_digests", "librelane_python_package"):
        v.equal("identity." + key, identity.get(key), contract.get(key))
    for key in ("pdk", "support"):
        v.equal("identity.revision." + key, identity.get("revisions", {}).get(key), contract.get("revisions", {}).get(key))
    v.equal("identity.liberty_sha256", identity.get("pdk_stdcell_liberty_sha256 (hosted)"), contract.get("liberty_sha256"))
    expected_rtl = contract.get("rtl_sha256")
    if not expected_rtl:
        v.add("identity.rtl", "INCOMPLETE", "Missing exact RTL source hash allowlist")
    else:
        v.equal("identity.rtl", rtl_hashes, expected_rtl)
    baseline = contract.get("baseline_resolved_config")
    changes = contract.get("implementation_changes")
    if not isinstance(baseline, dict) or not baseline or not isinstance(changes, dict):
        v.add("identity.config", "INCOMPLETE", "Missing complete baseline resolved configuration or change contract")
    elif not resolved:
        v.add("identity.config", "INCOMPLETE", "Missing candidate resolved configuration")
    else:
        expected = normalize_config(dict(baseline, **changes))
        actual = normalize_config(resolved)
        allowed = contract.get("reporting_config_keys", [])
        if any(k != "STA_EXTRA_CORNER_TCL_FILE" for k in allowed):
            v.add("identity.config_reporting_exclusions", "FAIL", "Unapproved configuration exclusion", keys=allowed)
        for key in allowed:
            expected.pop(key, None)
            actual.pop(key, None)
        differences = {k: {"expected": expected.get(k, "<missing>"), "actual": actual.get(k, "<missing>")}
                       for k in sorted(set(expected) | set(actual)) if expected.get(k, "<missing>") != actual.get(k, "<missing>")}
        v.equal("identity.config_unexpected_differences", differences, {})
        if contract.get("candidate") == "m3c-cts-only":
            v.equal("identity.cts_only_change", changes, {"CTS_SINK_CLUSTERING_SIZE": 6})
    v.equal("identity.resolved_process", identity.get("evidence_resolved_process_invalid"), [])
    v.equal("identity.submission_config", identity.get("tt_submission_resolved_identical"), True)
    integrity = analysis.get("integrity_vs_hosted_manifest", {})
    v.equal("identity.manifest_mismatches", integrity.get("mismatch"), [])
    v.finite("identity.manifest_matches", integrity.get("match"), 1)


def check_metrics(v, analysis, metrics, manufacturability):
    v.equal("metrics.final_basis", analysis.get("metrics_source", {}).get("basis"), "final/metrics.json")
    electrical = analysis.get("electrical_rules", {})
    v.equal("electrical.corners", sorted(electrical.get("corners", {})), sorted(CORNERS))
    for corner in CORNERS:
        rules = electrical.get("corners", {}).get(corner, {})
        v.equal("electrical." + corner + ".section", rules.get("section_found"), True)
        v.equal("electrical." + corner + ".unparsed", rules.get("unparsed_violation_lines"), [])
        for rule in ("max_fanout", "max_slew", "max_capacitance"):
            node = rules.get(rule, {})
            for key in ("count_line", "metrics_count", "rows_listed", "unique_nets", "unique_pins"):
                v.count(f"electrical.{corner}.{rule}.{key}", node.get(key))
            v.equal(f"electrical.{corner}.{rule}.rows", node.get("rows"), [])
        timing = analysis.get("timing", {}).get("corners", {}).get(corner, {})
        for kind in ("setup", "hold"):
            v.count(f"timing.{corner}.{kind}.violations", timing.get(kind + "_violating_endpoints"))
            v.finite(f"timing.{corner}.{kind}.slack_ns", timing.get("worst_" + kind + "_slack_ns"), 0)
            v.finite(f"timing.{corner}.{kind}.tns_ns", timing.get(kind + "_tns_ns"), 0)
            path = timing.get("worst_" + kind + "_path", {})
            v.nonempty(f"timing.{corner}.{kind}.path_count", path.get("paths_in_report"))
            report_slack, metric_slack = path.get("slack_ns"), timing.get("worst_" + kind + "_slack_ns")
            v.finite(f"timing.{corner}.{kind}.report_slack_ns", report_slack, 0)
            if all(isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) for x in (report_slack, metric_slack)):
                v.equal(f"timing.{corner}.{kind}.report_matches_metric_10fs", abs(report_slack - metric_slack) <= TIMING_REPORT_TOLERANCE_NS, True)
    for key in PHYSICAL_ZERO:
        v.count("physical." + key, metrics.get(key))
    v.finite("physical.routed_wirelength", metrics.get("route__wirelength"), 1)
    if manufacturability is None:
        v.add("physical.manufacturability", "INCOMPLETE", "Final manufacturability report missing")
    else:
        for section in ("Antenna", "LVS", "DRC"):
            match = re.search(r"\* " + section + r"\s*\n([^*]+)", manufacturability)
            text = match[1].strip() if match else None
            if text is None:
                v.add("physical.manufacturability." + section, "INCOMPLETE", "Missing final section")
            else:
                v.add("physical.manufacturability." + section,
                      "PASS" if text.startswith("Passed") and not re.search(r"fail|error", text, re.I) else "FAIL", text)


def check_area(v, analysis, contract):
    area = analysis.get("area", {})
    synth, routed = area.get("synthesis", {}), area.get("routed", {})
    for key in MAPPED_FIELDS:
        actual = synth.get(key)
        v.equal("area.synthesis." + key, actual, contract.get("baseline_synthesis", {}).get(key))
        if key != "latch_count":
            v.finite("area.synthesis." + key + ".positive", actual, 1)
    v.count("area.synthesis.expected_ff", synth.get("flip_flop_count"), 1377)
    v.count("area.synthesis.expected_latches", synth.get("latch_count"))
    source_paths = [s.get("path", "") for s in synth.get("sources", [])]
    for report in ("reports/stat.json", "reports/stat.rpt"):
        if not any(p.endswith(report) for p in source_paths):
            v.add("area.synthesis." + report, "INCOMPLETE", "Mapped Liberty-area source missing")
    values = [synth.get(k) for k in MAPPED_FIELDS[:3]]
    if all(isinstance(x, (int, float)) and math.isfinite(x) for x in values):
        v.equal("area.synthesis.breakdown_consistent", abs(values[0] - values[1] - values[2]) < 0.000001, True)
    for key in ("core_area_um2", "routed_stdcell_area_um2", "utilization_percent"):
        v.finite("area.routed." + key, routed.get(key), 0.000001)
    keys = routed.get("metric_keys", {})
    for key in ("design__instance__count__stdcell", "design__instance__count", "design__instance__count__class:clock_buffer"):
        v.nonempty("area.routed." + key, keys.get(key))
    v.count("area.routed.expected_ff", keys.get("design__instance__count__class:sequential_cell"), 1377)
    for key in ("design__die__bbox", "design__core__bbox"):
        v.equal("area.geometry." + key, keys.get(key), contract.get("baseline_geometry", {}).get(key))
    observed, computed = routed.get("utilization_percent"), routed.get("utilization_percent_recomputed")
    v.finite("area.routed.recomputed_utilization", computed, 0.000001)
    if all(isinstance(x, (int, float)) and math.isfinite(x) for x in (observed, computed)):
        # Flow standard-cell area is integer-rounded; no utilization improvement
        # threshold or new area target is introduced by this consistency check.
        v.equal("area.routed.utilization_consistent", abs(observed - computed) <= 0.001, True)


def check_routing(v, raw):
    global_log, _ = raw.latest("openroad-globalrouting", "openroad-globalrouting.log")
    if global_log is not None:
        text = global_log.decode()
        final = text.rsplit("Final congestion report:", 1)
        match = re.search(r"^Total\s+\d+\s+\d+\s+[\d.]+%\s+(\d+)\s*/\s*(\d+)\s*/\s*(\d+)", final[-1], re.M) if len(final) == 2 else None
        for index, name in enumerate(("horizontal", "vertical", "total"), 1):
            v.count("physical.final_global_overflow." + name, int(match[index]) if match else None)
    log, source = raw.latest("openroad-detailedrouting", "openroad-detailedrouting.log")
    if log is None:
        return
    text = log.decode()
    v.equal("physical.routing_completed", "[INFO DRT-0198] Complete detail routing." in text, True)
    counts = re.findall(r"\[INFO DRT-0199\]\s+Number of violations = (\d+)\.", text)
    v.count("physical.final_route_log_violations", int(counts[-1]) if counts else None)
    v.equal("physical.routing_errors", bool(re.search(r"\[ERROR\b", text)), False)
    # Use the final check-antennas step, never the cleaner global-route report.
    steps = sorted({p.split('/')[0] for p in raw.inputs.run_files()
                    if re.fullmatch(r"\d+-openroad-checkantennas(?:-\d+)?", p.split('/')[0])}, key=analyzer.step_sort_key)
    if not steps:
        v.add("physical.final_antenna_log", "INCOMPLETE", "No final antenna check")
        return
    step = steps[-1]
    data, _ = raw.select("runs/wokwi/" + step + "/" + step.split("-", 1)[1] + ".log")
    if data is not None:
        text = data.decode()
        for kind in ("net", "pin"):
            counts = re.findall(r"Found (\d+) " + kind + r" violations\.", text)
            v.count("physical.final_antenna_log." + kind, int(counts[-1]) if counts else None)


def check_tests(v, analysis):
    tests = analysis.get("precheck_and_gate_tests", {})
    for label, count in (("precheck", 9), ("gate_tests", 3)):
        node = tests.get(label, {})
        junit = node.get("junit", [])
        v.nonempty("tests." + label + ".junit_reports", len(junit))
        for index, report in enumerate(junit):
            prefix = f"tests.{label}.{index}"
            v.count(prefix + ".tests", report.get("tests"), count)
            v.count(prefix + ".passed", report.get("passed"), count)
            for key in ("failed", "skipped", "suite_level_failures_errors"):
                v.count(prefix + "." + key, report.get(key))
            if label == "gate_tests":
                v.equal(prefix + ".names", sorted(c.get("name", "") for c in report.get("cases", [])), sorted(GATE_NAMES))
        v.equal("tests." + label + ".junit_outcome", node.get("junit_outcome"), "PASS")
    reports = tests.get("precheck", {}).get("klayout_report_databases", {})
    for name in ("drc_sg13cmos5l.xml", "drc_zero_area.xml"):
        v.count("tests.precheck." + name, reports.get(name, {}).get("items"))
    # Original pin-label 9/9 tally is kept above, but does not satisfy coverage.


def verify_archives(run_dir, v, contract):
    path = run_dir / "artifacts.json"
    if not path.is_file():
        v.add("archives.manifest", "INCOMPLETE", "No retrieval artifact manifest")
        return
    records = json.loads(path.read_text())
    v.nonempty("archives.count", len(records))
    registered = set()
    for record in records:
        local = record.get("local_file")
        if not local:
            v.add("archives.local_file", "INCOMPLETE", "Missing retained archive path")
            continue
        target = run_dir / "artifacts" / local
        registered.add(target.resolve())
        if not target.is_file():
            v.add("archives." + local, "INCOMPLETE", "Retained archive unavailable")
            continue
        actual = hashlib.sha256()
        with target.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                actual.update(chunk)
        expected = record.get("github_digest", "").removeprefix("sha256:")
        v.equal("archives." + local + ".digest", actual.hexdigest(), expected or None)
        v.equal("archives." + local + ".local_digest", actual.hexdigest(), record.get("local_sha256"))
        v.count("archives." + local + ".bytes", target.stat().st_size, record.get("local_bytes"))
        workflow = record.get("workflow_run", {})
        v.equal("archives." + local + ".run", str(workflow.get("id")) if workflow.get("id") else None, contract.get("run_id"))
        v.equal("archives." + local + ".source", workflow.get("head_sha"), contract.get("source_sha"))
    unexpected = [str(p) for p in (run_dir / "artifacts").rglob("*") if p.is_file() and p.resolve() not in registered]
    v.equal("archives.unregistered_inputs", unexpected, [])


def check(run_dir, contract, timing_path=None, pin_label_path=None):
    v = Verdict()
    verify_archives(run_dir, v, contract)
    raw = Evidence(run_dir, v)
    analysis = analyzer.analyze(run_dir)
    resolved, _ = raw.json("runs/wokwi/resolved.json")
    metrics, _ = raw.json("runs/wokwi/final/metrics.json")
    manifest, _ = raw.json("evidence/artifact-sha256.json")
    hosted, _ = raw.json("evidence/run-identity.json")
    v.equal("identity.artifact_run_identity", hosted, analysis.get("identity", {}).get("hosted_run_identity") | {
        k: value for k, value in hosted.items()
        if k not in analysis.get("identity", {}).get("hosted_run_identity", {})})
    rtl_hashes = {}
    rtl_files = {e.rel[e.rel.rfind('src/'):] for e in raw.inputs.entries
                 if re.search(r"(?:^|/)src/[^/]+\.v$", e.rel)}
    for path in sorted(rtl_files | set(contract.get("rtl_sha256", {}))):
        data, _ = raw.select(path)
        if data is not None:
            rtl_hashes[path] = digest(data)
            v.equal("identity.hosted_rtl_manifest." + path, digest(data), manifest.get(path))
    check_identity(v, analysis, contract, resolved, rtl_hashes)
    mapped, _ = raw.latest("yosys-synthesis", resolved.get("DESIGN_NAME", "UNKNOWN") + ".nl.v")
    v.equal("identity.synthesis_netlist", digest(mapped) if mapped is not None else None, contract.get("synthesis_netlist_sha256"))
    check_area(v, analysis, contract)
    manufacturing, _ = raw.latest("misc-reportmanufacturability", "manufacturability.rpt")
    check_metrics(v, analysis, metrics, manufacturing.decode() if manufacturing is not None else None)
    check_routing(v, raw)
    check_tests(v, analysis)
    check_timing_coverage(v, raw, timing_path, contract)
    check_pin_label(v, raw, pin_label_path, contract)
    return dict(schema=SCHEMA, status=v.status, exit_code={"PASS": 0, "FAIL": 1, "INCOMPLETE": 2}[v.status],
                identity={key: contract.get(key) for key in ("repository", "source_sha", "run_id", "attempt", "candidate")},
                checks=v.checks,
                failing_checks=[x["check"] for x in v.checks if x["status"] == "FAIL"],
                incomplete_checks=[x["check"] for x in v.checks if x["status"] == "INCOMPLETE"],
                electrical_summary=analysis.get("electrical_rules", {}).get("across_corners"),
                coverage=dict(native_corner_keys=list(CORNERS), rc="nominal extracted RC only",
                              library_pvt={c: c.removeprefix("nom_") for c in CORNERS},
                              gate="3 functional cases without SDF", original_precheck="9 official cases; supplemental pin-label required"),
                limitations=["Not full signoff", "No RC extraction corner sweep", "Untimed gate simulation, no SDF", "No hardware protocol evidence"],
                sources=list(raw.inputs.registry.values()))


def check_timing_coverage(v, raw, path, contract):
    try:
        from timing_evidence import analyze_timing
        timing = analyze_timing(raw.inputs.run_dir)
    except (ImportError, ValueError, OSError) as exc:
        v.add("coverage.engine_timing", "INCOMPLETE", str(exc))
        return
    if path is not None:
        supplied = json.loads(path.read_text())
        v.equal("coverage.timing_rederived", supplied, timing)
    validate_timing(v, timing, contract)


def validate_timing(v, timing, contract):
    from timing_evidence import REQUIRED_GROUPS, OPTIONAL_ABSENT
    v.equal("coverage.timing.schema", timing.get("schema"), "pinscript-timing-evidence/2")
    for key in ("run_id", "attempt", "source_sha"):
        actual = timing.get("identity", {}).get(key)
        v.equal("coverage.timing.identity." + key, str(actual) if actual is not None else None, contract.get(key))
    if timing.get("status") in ("FAIL", "INCOMPLETE"):
        v.add("coverage.timing.result", timing["status"], "Structural timing reporter did not pass")
    else:
        v.equal("coverage.timing.result", timing.get("status"), "PASS")
    v.equal("coverage.timing.corners", sorted(timing.get("corners", {})), sorted(CORNERS))
    for corner in CORNERS:
        node = timing.get("corners", {}).get(corner, {})
        v.equal(f"coverage.timing.{corner}.time_unit", node.get("units", {}).get("time"), "ns")
        v.equal(f"coverage.timing.{corner}.capacitance_unit", node.get("units", {}).get("capacitance"), "pF")
        groups = node.get("groups", {})
        v.nonempty(f"coverage.timing.{corner}.group_count", len(groups))
        missing = sorted(set(REQUIRED_GROUPS) - set(groups))
        if missing:
            v.add(f"coverage.timing.{corner}.required_groups", "INCOMPLETE", "Required timing groups missing", missing=missing)
        else:
            v.add(f"coverage.timing.{corner}.required_groups", "PASS", "All required structural groups present")
        v.equal(f"coverage.timing.{corner}.unexpected_groups", sorted(set(groups) - set(REQUIRED_GROUPS) - set(OPTIONAL_ABSENT)), [])
        for label, group in groups.items():
            prefix = f"coverage.timing.{corner}.{label}"
            if group.get("requirement") == "NOT_APPLICABLE":
                v.equal(prefix + ".optional_allowlist", label in OPTIONAL_ABSENT, True)
                v.passing(prefix + ".status", group.get("status"), "NOT_APPLICABLE")
                v.equal(prefix + ".structural", group.get("structural_validation", {}).get("status"), "ABSENT")
                v.count(prefix + ".reachable_endpoint_count", group.get("structural_validation", {}).get("reachable_endpoint_count"))
                reason = group.get("structural_validation", {}).get("explanation")
                v.equal(prefix + ".recorded_explanation", isinstance(reason, str) and bool(reason.strip()), True)
                continue
            v.equal(prefix + ".requirement", group.get("requirement"), "REQUIRED")
            v.passing(prefix + ".status", group.get("status"))
            v.equal(prefix + ".structural", group.get("structural_validation", {}).get("status"), "PASS")
            for field in ("launch", "capture"):
                v.nonempty(prefix + "." + field + "_count", group.get(field + "_count"))
                objects = group.get(field + "_objects", [])
                v.equal(prefix + "." + field + "_objects_match_count", len(set(objects)), group.get(field + "_count"))
            for delay in ("max", "min"):
                report = group.get(delay, {})
                v.passing(prefix + "." + delay + ".status", report.get("status"))
                v.nonempty(prefix + "." + delay + ".paths", report.get("path_count"))
                for metric in ("arrival_ns", "required_ns", "slack_ns"):
                    v.finite(prefix + "." + delay + "." + metric, (report.get("worst") or {}).get(metric), 0 if metric == "slack_ns" else None)
                if not report.get("source_report"):
                    v.add(prefix + "." + delay + ".source_report", "INCOMPLETE", "Selected source report missing")
                else:
                    v.add(prefix + "." + delay + ".source_report", "PASS", "Selected source report retained", source=report["source_report"])


def check_pin_label(v, raw, path, contract):
    from check_pin_labels import PINNED_HASHES, UPSTREAM_RULE
    if path is None:
        report, _ = raw.json("pin-label.json")
    else:
        report = json.loads(path.read_text())
    validate_pin_label(v, report, contract)
    source = report.get("source", {})
    design = contract.get("baseline_resolved_config", {}).get("DESIGN_NAME")
    if not design:
        v.add("coverage.pin_label.design", "INCOMPLETE", "Missing expected top module")
        return
    for kind in ("gds", "lef"):
        data, _ = raw.select(f"runs/wokwi/final/{kind}/{design}.{kind}")
        v.equal("coverage.pin_label.source." + kind, digest(data) if data is not None else None, source.get(kind, {}).get("sha256"))
    for kind, filename in (("pdk_lyp", "sg13cmos5l.lyp"), ("pdk_stream_map", "sg13cmos5l.map")):
        v.equal("coverage.pin_label.source." + kind, source.get(kind, {}).get("sha256"), PINNED_HASHES[filename])
    v.equal("coverage.pin_label.upstream_rule", source.get("upstream_rule"), UPSTREAM_RULE)
    fixtures, _ = raw.json("pin-label-fixtures.json")
    validate_pin_label_fixtures(v, fixtures, report)


def validate_pin_label_fixtures(v, fixtures, report):
    if not fixtures:
        v.add("coverage.pin_label.fixtures", "INCOMPLETE", "Same-run pin-label fixture report missing")
        return
    expected = {"valid": "PASS", "pin_outside_drawing": "FAIL", "empty_mapping": "INCOMPLETE",
                "wrong_mapping": "INCOMPLETE", "wrong_mapping_digest": "INCOMPLETE", "missing_label": "INCOMPLETE"}
    v.passing("coverage.pin_label.fixtures.status", fixtures.get("status"))
    v.count("coverage.pin_label.fixtures.count", fixtures.get("test_count"), len(expected))
    v.count("coverage.pin_label.fixtures.skipped", fixtures.get("skipped"))
    v.equal("coverage.pin_label.fixtures.same_klayout", fixtures.get("klayout_version"), report.get("klayout_version"))
    v.equal("coverage.pin_label.fixtures.checker_source", fixtures.get("source", {}).get("checker", {}).get("sha256"),
            digest(Path(__file__).with_name("check_pin_labels.py").read_bytes()))
    tests = fixtures.get("tests", [])
    v.equal("coverage.pin_label.fixtures.names", sorted(t.get("name", "") for t in tests), sorted(expected))
    for item in tests:
        name = item.get("name")
        v.equal("coverage.pin_label.fixtures." + str(name) + ".expected", item.get("expected"), expected.get(name))
        v.equal("coverage.pin_label.fixtures." + str(name) + ".observed", item.get("observed"), expected.get(name))
        v.equal("coverage.pin_label.fixtures." + str(name) + ".passed", item.get("passed"), True)


def validate_pin_label(v, report, contract):
    v.count("coverage.pin_label.schema", report.get("schema_version"), 1)
    status = report.get("status")
    if status == "INCOMPLETE":
        v.add("coverage.pin_label.status", "INCOMPLETE", str(report.get("incomplete_reasons", [])))
    else:
        v.equal("coverage.pin_label.status", status, "PASS")
    identity = report.get("identity", {})
    for actual, expected in (("run_id", "run_id"), ("run_attempt", "attempt"), ("candidate", "candidate"), ("source_sha", "source_sha")):
        v.equal("coverage.pin_label.identity." + actual, identity.get(actual), contract.get(expected))
    v.equal("coverage.pin_label.pdk", report.get("pdk_revision"), contract.get("revisions", {}).get("pdk"))
    for count in ("relevant_object_count", "evaluated_object_count", "rule_evaluation_count", "polygon_object_count", "top_text_object_count"):
        v.nonempty("coverage.pin_label." + count, report.get(count))
    v.equal("coverage.pin_label.all_objects_evaluated", report.get("evaluated_object_count"), report.get("relevant_object_count"))
    v.count("coverage.pin_label.violations", report.get("violation_count"))
    labels = report.get("expected_labels", {})
    v.equal("coverage.pin_label.label_source", labels.get("source"), "LEF PIN declarations")
    v.nonempty("coverage.pin_label.label_count", labels.get("count"))
    v.equal("coverage.pin_label.label_names", len(set(labels.get("names", []))), labels.get("count"))
    for field, expected in (("missing", []), ("unexpected", []), ("duplicated", {})):
        v.equal("coverage.pin_label.labels." + field, labels.get(field), expected)
    rows = report.get("evaluations", [])
    v.nonempty("coverage.pin_label.evaluations", len(rows))
    for rule in ("pin_label_polygon_minus_drawing", "top_text_anchor_on_drawing", "top_labels_match_lef_pin_contract"):
        selected = [r for r in rows if r.get("rule") == rule]
        count = sum(r.get("evaluated_object_count", 0) for r in selected)
        v.nonempty("coverage.pin_label." + rule + ".objects", count)
    for index, row in enumerate(rows):
        v.count(f"coverage.pin_label.evaluation.{index}.violations", row.get("violation_count"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--contract", type=Path)
    parser.add_argument("--make-contract", action="store_true")
    parser.add_argument("--source-sha")
    parser.add_argument("--run-id")
    parser.add_argument("--attempt")
    parser.add_argument("--candidate")
    parser.add_argument("--timing", type=Path)
    parser.add_argument("--pin-label", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    out = args.out.resolve()
    run_dir = args.run_dir.resolve()
    reserved = {run_dir / "run.json", run_dir / "artifacts.json"}
    reserved.update(p.resolve() for p in (args.contract, args.timing, args.pin_label) if p)
    if (run_dir / "artifacts" in out.parents or run_dir / "logs" in out.parents or out in reserved):
        parser.error("--out must not overwrite an input or be inside artifacts")
    try:
        if args.make_contract:
            result = make_contract(args.run_dir, args.candidate or "m3c-cts-only")
        else:
            if args.contract is None:
                parser.error("--contract is required")
            contract = json.loads(args.contract.read_text())
            for key in ("source_sha", "run_id", "attempt", "candidate"):
                value = getattr(args, key)
                if value is not None:
                    contract[key] = value
            result = check(args.run_dir.resolve(), contract, args.timing, args.pin_label)
    except Exception as exc:
        result = dict(schema=SCHEMA, status="INCOMPLETE", exit_code=2, error=f"{type(exc).__name__}: {exc}",
                      limitations=["Checker failure is not acceptance; retain all evidence"])
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(analyzer.strict_json(result), indent=2, allow_nan=False) + "\n")
    print(f"{result.get('status', 'CONTRACT')}: {args.out}")
    return result.get("exit_code", 0)


if __name__ == "__main__":
    raise SystemExit(main())
