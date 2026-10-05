#!/usr/bin/env python3
"""Report-only historical STA replay on the already installed, pinned runner image.

Downloads two fixed digest-pinned public archives into disposable scratch, reads
only selected members, then invokes sta (never synthesis/place/route). Original
archives and reports are not changed. Output deliberately has no runs/wokwi or
evidence/run-identity.json paths, so candidate analysis cannot mix historical runs.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import zipfile

from analyze_run import parse_paths, EXPECTED_CORNERS
from timing_evidence import Connectivity, PAIRS, OPTIONAL_ABSENT, evaluate_report, parse_units

REPO = "joshvern/pinscript-cmos5l-feasibility"
SOURCE_SHA = "e5580ef0c76b7dc4edc41919e2bc38609204695f"
IMAGE_ID = "sha256:369c964be30bcec52d98bf18b2f8c7d80f133c5c9cf34cc1f7d5a0d2890a3103"
BASELINES = {
    "as-is": dict(run_id=37313216627, artifact_id=11349449829, bytes=93923947,
                  sha256="d9a637639c2d7c4c570993f44b0ef3cfe439750d8a93874faac981b4fc1c6c70"),
    "repair": dict(run_id=37313220214, artifact_id=11350576051, bytes=99544463,
                   sha256="1b5a0c64e89bc8550a5b4af5bc125ae4374ab9f7065ac9cf0a297f167083bd69"),
}
DESIGN = "tt_um_joshua_vernazza_pinscript"


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def reserve(path, size):
    path.mkdir(parents=True, exist_ok=True)
    available = shutil.disk_usage(path).free
    if available < size + 2 * 1024 * 1024 * 1024:
        raise ValueError(f"insufficient disk at {path}: {available} free, need {size} plus 2 GiB margin")


def fetch(label, scratch, archives=None):
    expected = BASELINES[label]
    if archives:
        matches = list(Path(archives).glob(f"**/m3c-{label}-{expected['run_id']}-1-{expected['artifact_id']}.zip"))
        if len(matches) != 1:
            raise ValueError(f"expected one retained archive for {label}, got {matches}")
        archive = matches[0]
    else:
        reserve(scratch, expected["bytes"])
        archive = scratch / f"{label}-{expected['artifact_id']}.zip"
        if not archive.exists():
            metadata = json.loads(subprocess.check_output(["gh", "api", f"repos/{REPO}/actions/artifacts/{expected['artifact_id']}"], timeout=60))
            if metadata.get("digest") != "sha256:" + expected["sha256"] or metadata.get("size_in_bytes") != expected["bytes"]:
                raise ValueError(f"{label}: GitHub artifact identity/digest mismatch")
            with archive.open("xb") as stream:
                subprocess.run(["gh", "api", f"repos/{REPO}/actions/artifacts/{expected['artifact_id']}/zip"], stdout=stream, check=True, timeout=600)
    if archive.stat().st_size != expected["bytes"] or digest(archive) != expected["sha256"]:
        raise ValueError(f"{label}: retained/downloaded archive digest mismatch")
    return archive


def split_reports(stdout):
    result, name, lines = {}, None, []
    for line in stdout.splitlines():
        if line.startswith("%OL_CREATE_REPORT "):
            if name:
                raise ValueError("nested report marker")
            name, lines = line.split(" ", 1)[1], []
        elif line == "%OL_END_REPORT":
            if name:
                if name in result:
                    raise ValueError(f"duplicate report {name}")
                result[name] = "\n".join(lines) + "\n"
            name, lines = None, []
        elif name:
            lines.append(line)
    if name:
        raise ValueError("unterminated report")
    return result


def sta_command(output, pdk_root, corner):
    return ["docker", "run", "--rm", "--network", "none", "--entrypoint", "sta",
            "-v", f"{output.resolve()}:/replay:ro", "-v", f"{pdk_root.resolve()}:/pdk:ro",
            IMAGE_ID, "-no_splash", "-exit", f"/replay/{corner}/replay.tcl"]


def compare_reference(reference, current):
    original_paths, current_paths = parse_paths(reference), parse_paths(current)
    old = min(original_paths, key=lambda p: p["slack_ns"]) if original_paths else None
    new = min(current_paths, key=lambda p: p["slack_ns"]) if current_paths else None
    same = bool(old and new and all(abs(old[k] - new[k]) <= 0.00002 for k in
                ("slack_ns", "data_arrival_ns", "data_required_ns")))
    return dict(status="PASS" if same else "INCOMPLETE", original=old, replay=new)


def prepare(label, archive, output):
    expected = BASELINES[label]
    with zipfile.ZipFile(archive) as z:
        manifest = json.loads(z.read("evidence/artifact-sha256.json"))
        identity = json.loads(z.read("evidence/run-identity.json"))
        if (identity["GITHUB_SHA"] != SOURCE_SHA or identity["GITHUB_RUN_ID"] != str(expected["run_id"])
                or identity["GITHUB_RUN_ATTEMPT"] != "1" or identity["CANDIDATE"] != "m3c-" + label):
            raise ValueError("historical identity mismatch")
        selected = {"input.nl.v": f"runs/wokwi/final/nl/{DESIGN}.nl.v",
                    "input.sdc": f"runs/wokwi/final/sdc/{DESIGN}.sdc",
                    "input.spef": f"runs/wokwi/final/spef/nom/{DESIGN}.nom.spef"}
        reserve(output, sum(z.getinfo(member).file_size for member in selected.values()))
        sources = {}
        for target, member in selected.items():
            data = z.read(member)
            sha = hashlib.sha256(data).hexdigest()
            if manifest.get(member) != sha:
                raise ValueError(f"member digest mismatch: {member}")
            (output / target).write_bytes(data)
            sources[target] = dict(archive_member=member, sha256=sha, bytes=len(data))
        config = json.loads(z.read("runs/wokwi/55-openroad-stapostpnr/config.json"))
        references = {corner: {name: z.read(f"runs/wokwi/55-openroad-stapostpnr/{corner}/{name}.rpt").decode()
                              for name in ("max", "min", "m3c-host_address-to-host_capture-max",
                                           "m3c-host_address-to-host_capture-min", "m3c-memory-to-host_capture-max",
                                           "m3c-memory-to-host_capture-min")}
                      for corner in EXPECTED_CORNERS}
        # Check these are exactly the netlist/RC inputs of the original final STA.
        for target, original in (("input.nl.v", f"runs/wokwi/52-openroad-fillinsertion/{DESIGN}.nl.v"),
                                 ("input.spef", f"runs/wokwi/54-openroad-rcx/nom/{DESIGN}.nom.spef")):
            if manifest[original] != sources[target]["sha256"]:
                raise ValueError(f"final and original STA input differ: {target}")
    return dict(identity=dict(run_id=expected["run_id"], attempt=1, source_sha=SOURCE_SHA, candidate=label),
                artifact=expected, input_sources=sources, config=config, references=references, manifest=manifest)


def replay(label, archive, output, pdk_root, image, prepare_only=False):
    data = prepare(label, archive, output)
    connection = Connectivity((output / "input.nl.v").read_text())
    audit = connection.audit()
    result = dict(schema="pinscript-historical-timing-replay/1", status="NOT_RUN", identity=data["identity"],
                  artifact=data["artifact"], input_sources=data["input_sources"],
                  clock_period_ns=100, rc_coverage="nominal extracted RC only", corners={})
    if prepare_only:
        return result
    image_info = json.loads(subprocess.check_output(["docker", "image", "inspect", image], timeout=60))
    if len(image_info) != 1 or image_info[0]["Id"] != IMAGE_ID:
        raise ValueError("replay requires exact already-installed historical container; no pull permitted")
    result["image_id"] = IMAGE_ID
    hook = output / "report-hook.tcl"
    hook.write_bytes(Path(__file__).with_name("sta_m3c.tcl").read_bytes())
    result["hook_sha256"] = digest(hook)
    result["sta_version"] = subprocess.check_output(["docker", "run", "--rm", "--network", "none", "--entrypoint", "sta", IMAGE_ID, "-version"], text=True, timeout=60).strip()
    if result["sta_version"] != "2.7.0":
        raise ValueError("unexpected replay STA version")
    for corner in EXPECTED_CORNERS:
        directory = output / corner
        directory.mkdir()
        lines = ["set_cmd_units -time ns -capacitance pF -current mA -voltage V -resistance kOhm -distance um",
                 f"define_corners {corner}"]
        libraries = []
        for original in data["config"]["CELL_LIBS"][corner]:
            relative = Path(original).relative_to("/home/runner/pdk")
            library = pdk_root / relative
            if digest(library) != data["manifest"][original]:
                raise ValueError(f"pinned library hash mismatch: {library}")
            libraries.append(dict(path=str(relative), sha256=digest(library)))
            lines.append(f"read_liberty -corner {corner} {{/pdk/{relative}}}")
        lines += ["read_verilog {/replay/input.nl.v}", f"link_design {DESIGN}",
                  "read_sdc {/replay/input.sdc}", f"read_spef -corner {corner} {{/replay/input.spef}}",
                  f"set ::env(_CURRENT_CORNER_NAME) {corner}", f"set corner_name {corner}",
                  "source {/replay/report-hook.tcl}"]
        for delay in ("max", "min"):
            lines += [f"puts {{%OL_CREATE_REPORT {delay}.rpt}}",
                      f"report_checks -path_delay {delay} -corner {corner} -sort_by_slack -group_path_count 8 -endpoint_path_count 2 -format full_clock_expanded -digits 6",
                      "puts {%OL_END_REPORT}"]
        script = directory / "replay.tcl"
        script.write_text("\n".join(lines) + "\n")
        command = sta_command(output, pdk_root, corner)
        (directory / "command.json").write_text(json.dumps(command, indent=2) + "\n")
        # Stream logs so a timeout/failed attempt retains all output produced.
        with (directory / "stdout.log").open("w") as stdout, (directory / "stderr.log").open("w") as stderr:
            proc = subprocess.run(command, stdout=stdout, stderr=stderr, check=False, timeout=600)
        reports = split_reports((directory / "stdout.log").read_text())
        for name, text in reports.items():
            if "/" in name or not name.endswith(".rpt"):
                raise ValueError("unexpected report name")
            (directory / name).write_text(text)
        row = result["corners"][corner] = dict(exit_code=proc.returncode, command=command, libraries=libraries,
                units=parse_units(reports.get("m3c-units.rpt", "")), equivalence=[], groups={}, status="INCOMPLETE")
        for name, reference in data["references"][corner].items():
            row["equivalence"].append(dict(report=name, **compare_reference(reference, reports.get(name + ".rpt", ""))))
        specs = [(f"{s}-to-{d}", s, d, []) for s, d in PAIRS]
        specs.append(("pc-via-fetch-to-pc", "pc", "pc", [n for v in audit["fetch_cut"].values() for n in v]))
        for name, source, destination, via in specs:
            group = row["groups"][name] = dict(requirement="NOT_APPLICABLE" if name in OPTIONAL_ABSENT else "REQUIRED")
            for delay in ("max", "min"):
                group[delay] = evaluate_report(reports.get(f"m3c-{name}-{delay}.rpt", ""),
                    reports.get(f"m3c-{name}-{delay}-points.rpt", ""), connection, source, destination, via)
        groups_ok = all(all(g[d]["status"] == "PASS" and
                        (g[d]["path_count"] > 0 if g["requirement"] == "REQUIRED" else g[d]["header_status"] == "NOT_APPLICABLE")
                        for d in ("max", "min")) for g in row["groups"].values())
        if (proc.returncode == 0 and row["units"]["status"] == "PASS" and groups_ok
                and all(e["status"] == "PASS" for e in row["equivalence"])):
            row["status"] = "PASS"
    result["status"] = "PASS" if all(r["status"] == "PASS" for r in result["corners"].values()) else "INCOMPLETE"
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--scratch", type=Path, required=True)
    parser.add_argument("--pdk-root", type=Path, default=Path("/home/runner/pdk"))
    parser.add_argument("--image", default=IMAGE_ID)
    parser.add_argument("--archives", type=Path, help="reuse retained local verified ZIPs; no network")
    parser.add_argument("--prepare-only", action="store_true", help="local input validation only; never calls Docker")
    args = parser.parse_args()
    results = {}
    for label in BASELINES:
        output = args.out / label
        try:
            archive = fetch(label, args.scratch, args.archives)
            results[label] = replay(label, archive, output, args.pdk_root, args.image, args.prepare_only)
        except Exception as error:
            results[label] = dict(status="INCOMPLETE", error=str(error), identity=BASELINES[label])
        output.mkdir(parents=True, exist_ok=True)
        (output / "replay-result.json").write_text(json.dumps(results[label], indent=2) + "\n")
    (args.out / "summary.json").write_text(json.dumps(results, indent=2) + "\n")
    print(json.dumps({label: r["status"] for label, r in results.items()}))
    return 0 if args.prepare_only and all(r["status"] == "NOT_RUN" for r in results.values()) or all(r["status"] == "PASS" for r in results.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
