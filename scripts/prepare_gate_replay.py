#!/usr/bin/env python3
"""Bind a functional gate replay to an existing, successful physical run."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

REPOSITORY = "joshvern/pinscript-cmos5l-feasibility"
SOURCES = {
    "baseline": (37261420281, "b4480cba87814fd2e807850dfdff66ae3d06a131", "tt_um_joshua_vernazza_pinscript"),
    "separate": (37263012841, "3f2cf41d818f5a30557353ac1473335e5d5bcff3", "tt_um_pinscript_probe_separate"),
    "shared": (37263015069, "3f2cf41d818f5a30557353ac1473335e5d5bcff3", "tt_um_pinscript_probe_shared"),
}


def main():
    candidate = os.environ["CANDIDATE"]
    run_id, revision, top = SOURCES[candidate]
    out = Path("replay-evidence")
    out.mkdir(exist_ok=True)
    if sys.argv[1] == "source":
        run = json.loads(subprocess.check_output([
            "gh", "api", f"repos/{REPOSITORY}/actions/runs/{run_id}",
        ], text=True))
        jobs = json.loads(subprocess.check_output([
            "gh", "api", f"repos/{REPOSITORY}/actions/runs/{run_id}/jobs?per_page=100",
        ], text=True))["jobs"]
        assert run["head_sha"] == revision, "source revision mismatch"
        assert run["repository"]["full_name"] == REPOSITORY
        assert run["path"] == ".github/workflows/m3b-cmos5l.yaml"
        assert run["display_title"].startswith(f"M3B {candidate} / ")
        assert any(j["name"] == "gds" and j["conclusion"] == "success" for j in jobs), "physical job not successful"
        record = {
            "candidate": candidate, "source_run_id": run_id,
            "source_revision": revision, "source_run_url": run["html_url"],
            "replay_revision": os.environ.get("GITHUB_SHA"),
            "replay_run_id": os.environ.get("GITHUB_RUN_ID"),
            "physical_gds_job": [j for j in jobs if j["name"] == "gds"][0],
        }
        (out / "identity.json").write_text(json.dumps(record, indent=2) + "\n")
        with open(os.environ["GITHUB_OUTPUT"], "a") as stream:
            stream.write(f"run_id={run_id}\n")
        print(f"Verified {candidate} physical run {run_id} at {revision}")
    elif sys.argv[1] == "input":
        root = Path("replay-input")
        pdk = json.loads((root / "tt_submission/pdk.json").read_text())
        assert pdk["PDK"] == "ihp-sg13cmos5l"
        assert pdk["PDK_VERSION"] == "2bbec755dc67ca3db0261c3d6163e15735d66710"
        netlists = list((root / "tt_submission").glob("*.v"))
        assert len(netlists) == 1 and netlists[0].stem == top, "unexpected netlist"
        hashes = {
            str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file()
        }
        (out / "input-sha256.json").write_text(json.dumps(hashes, indent=2) + "\n")
        (out / "pdk.json").write_text(json.dumps(pdk, indent=2) + "\n")
        print(f"Recorded {len(hashes)} original input file hashes; no netlist changes")
    else:
        raise SystemExit("expected source or input")


if __name__ == "__main__":
    main()
