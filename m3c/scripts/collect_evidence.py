#!/usr/bin/env python3
"""Retain resolved identities without recording credentials or arbitrary environment."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

out = Path("evidence")
out.mkdir(exist_ok=True)


def command(name, args):
    try:
        result = subprocess.run(args, text=True, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, timeout=120, check=False)
        record = {"command": args, "exit_code": result.returncode, "output": result.stdout}
    except (OSError, subprocess.TimeoutExpired) as error:
        record = {"command": args, "unavailable": str(error)}
    (out / f"{name}.json").write_text(json.dumps(record, indent=2) + "\n")
    return record


identity = {key: os.environ.get(key) for key in (
    "GITHUB_REPOSITORY", "GITHUB_SHA", "GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT",
    "CANDIDATE", "IMPLEMENTATION_OUTCOME", "PDK", "PDK_ROOT",
    "ImageOS", "ImageVersion",
)}
identity["python"] = sys.version
(out / "run-identity.json").write_text(json.dumps(identity, indent=2) + "\n")
command("source-revision", ["git", "rev-parse", "HEAD"])
command("support-revision", ["git", "-C", "tt", "rev-parse", "HEAD"])
command("support-submodules", ["git", "-C", "tt", "submodule", "status", "--recursive"])
command("python-packages", [sys.executable, "-m", "pip", "freeze", "--all"])
command("docker-version", ["docker", "version", "--format", "{{json .}}"])
images = command("docker-images", ["docker", "image", "ls", "--digests", "--no-trunc", "--format", "{{json .}}"])
for index, line in enumerate(images.get("output", "").splitlines()):
    try:
        image = json.loads(line)
    except ValueError:
        continue
    if "librelane" in image.get("Repository", "").lower():
        command(f"librelane-image-{index}", ["docker", "image", "inspect", image["ID"]])
        for tool, arguments in (
            ("yosys", ["-V"]), ("openroad", ["-version"]),
            ("sta", ["-version"]), ("magic", ["--version"]),
            ("klayout", ["-v"]),
        ):
            command(f"tool-{index}-{tool}", [
                "docker", "run", "--rm", "--network", "none",
                "--entrypoint", tool, image["ID"], *arguments,
            ])

pdk_root = Path(os.environ.get("PDK_ROOT", "/home/runner/pdk"))
command("pdk-revision", ["git", "-C", str(pdk_root), "rev-parse", "HEAD"])
hashes = {}
roots = [Path("src"), Path("runs/wokwi"), pdk_root / "ihp-sg13cmos5l"]
for root in roots:
    if not root.exists():
        continue
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if root == roots[-1] and path.suffix not in {".lib", ".lef", ".tcl", ".v"} and path.name != "SOURCES":
            continue
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        hashes[str(path)] = digest.hexdigest()
(out / "artifact-sha256.json").write_text(json.dumps(hashes, indent=2) + "\n")
print(f"Recorded flow identity and {len(hashes)} artifact/library hashes.")
# Never treat an environment-only value as proof of the resolved process.
resolved = []
invalid = []
for path in sorted(Path("runs/wokwi").rglob("resolved.json")):
    config = json.loads(path.read_text())
    resolved.append({"path": str(path), "PDK": config.get("PDK"),
                     "STD_CELL_LIBRARY": config.get("STD_CELL_LIBRARY"),
                     "CLOCK_PERIOD": config.get("CLOCK_PERIOD"),
                     "DIE_AREA": config.get("DIE_AREA"),
                     "CORE_AREA": config.get("CORE_AREA"),
                     "STA_CORNERS": config.get("STA_CORNERS")})
    for key, expected in (("PDK", "ihp-sg13cmos5l"),
                          ("STD_CELL_LIBRARY", "sg13cmos5l_stdcell"),
                          ("CLOCK_PERIOD", 100)):
        if config.get(key) != expected:
            invalid.append(f"{path}: {key}={config.get(key)!r}, expected {expected!r}")
(out / "resolved-process.json").write_text(json.dumps({
    "resolved_configurations": resolved, "invalid": invalid,
    "process_evidence": "AVAILABLE" if resolved else "UNAVAILABLE",
}, indent=2) + "\n")
if invalid or (os.environ.get("IMPLEMENTATION_OUTCOME") == "success" and not resolved):
    raise SystemExit("Missing or incorrect resolved CMOS5L configuration; inspect evidence")
