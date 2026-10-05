#!/usr/bin/env python3
"""Download one completed M3C run's metadata, job logs and artifacts (read-only GitHub access).

Writes reports/m3c/hosted/<case>-<run_id>/{run.json, logs/, artifacts/}. Never dispatches,
re-runs or modifies anything remotely.

--zip-artifacts keeps every artifact as its original zip (artifacts/<name>-<id>.zip), checks
each against the SHA-256 digest GitHub reports for it, and writes artifacts.json. Use it when
disk space is limited: the analyzer reads zip members in memory.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess

REPO = "joshvern/pinscript-cmos5l-feasibility"
ROOT = Path(__file__).resolve().parents[3]


def gh(*args, capture=True):
    result = subprocess.run(["gh", *args], text=True, capture_output=capture)
    if result.returncode:
        raise SystemExit(f"gh {' '.join(args)} failed: {result.stderr}")
    return result.stdout


def job_log(job_id):
    """Save raw logs as evidence, never render their terminal control bytes.

    New hosted gh versions reject ANSI-containing API responses by default;
    older installed gh versions do not implement the opt-in flag. Retry this
    read-only request only for that precise compatibility diagnostic.
    """
    command = ["gh", "api", f"repos/{REPO}/actions/jobs/{job_id}/logs"]
    result = subprocess.run(command, text=True, capture_output=True)
    if result.returncode and "pass --allow-escape-sequences to output it anyway" in result.stderr:
        result = subprocess.run(command + ["--allow-escape-sequences"], text=True, capture_output=True)
    if result.returncode:
        raise SystemExit(f"job {job_id} log download failed: {result.stderr}")
    return result.stdout


def digest_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def artifact_listing(run_id):
    items = []
    page = 1
    while True:
        response = json.loads(gh("api", f"repos/{REPO}/actions/runs/{run_id}/artifacts?per_page=100&page={page}"))
        batch = response["artifacts"]
        items.extend(batch)
        if len(items) >= response["total_count"]:
            break
        if not batch:
            raise SystemExit("incomplete artifact pagination")
        page += 1
    if not items or len({a["id"] for a in items}) != len(items):
        raise SystemExit("missing or duplicate artifact IDs")
    return items


def ensure_space(path, needed, margin=2 * 1024**3):
    free = shutil.disk_usage(path).free
    if free < needed + margin:
        raise SystemExit(f"insufficient space at {path}: {free} bytes free; need {needed} + {margin} safety margin")
    return free


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("run_id")
    parser.add_argument("--out", type=Path, help="explicit destination within the current workspace")
    parser.add_argument("--allow-in-progress", action="store_true",
                        help="collect completed dependency jobs for the same run's acceptance job")
    parser.add_argument("--zip-artifacts", action="store_true",
                        help="store original artifact zips (digest-verified) instead of extracting")
    args = parser.parse_args()
    if not args.run_id.isdecimal():
        parser.error("run_id must contain decimal digits only")
    run = json.loads(gh("run", "view", args.run_id, "--repo", REPO, "--json",
                        "databaseId,attempt,displayTitle,status,conclusion,headSha,headBranch,event,createdAt,updatedAt,url,workflowName,jobs"))
    if run["status"] != "completed" and not args.allow_in_progress:
        raise SystemExit(f"run {args.run_id} is {run['status']}")
    match = re.fullmatch(r"M3C (as-is|repair|cts-only) / ([0-9a-f]{40})", run["displayTitle"])
    if not match or match[2] != run["headSha"] or str(run["databaseId"]) != args.run_id:
        raise SystemExit("unexpected run title or identity")
    case = match[1]
    out = args.out.resolve() if args.out else ROOT / "reports/m3c/hosted" / f"{case}-{args.run_id}"
    if args.out and not out.is_relative_to(Path.cwd().resolve()):
        parser.error("--out must be within the current workspace")
    ensure_space(Path.cwd(), 16 * 1024**2)
    out.mkdir(parents=True, exist_ok=True)
    if (out / "run.json").exists():
        previous = json.loads((out / "run.json").read_text())
        if any(previous.get(k) != run.get(k) for k in ("databaseId", "attempt", "headSha")):
            raise SystemExit("refusing to mix runs, attempts or source revisions in destination")
    (out / "run.json").write_text(json.dumps(run, indent=2) + "\n")
    logs = out / "logs"
    logs.mkdir(exist_ok=True)
    for job in run["jobs"]:
        if job["status"] != "completed":
            continue
        # gh run view --log rejects an in-progress enclosing run, even for a
        # completed dependency. The job-log endpoint supports this use case.
        text = job_log(job["databaseId"])
        safe_name = re.sub(r"[^A-Za-z0-9_.-]", "_", job["name"])
        (logs / f"{safe_name}-{job['databaseId']}.log").write_text(text)
    artifacts = out / "artifacts"
    if args.zip_artifacts:
        artifacts.mkdir(exist_ok=True)
        listing = artifact_listing(args.run_id)
        for item in listing:
            if item.get("expired") or item.get("workflow_run", {}).get("id") != int(args.run_id):
                raise SystemExit("expired artifact or artifact belongs to a different run")
            if not re.fullmatch(r"[A-Za-z0-9_.-]+", item["name"]):
                raise SystemExit("unsafe artifact name")
            if not re.fullmatch(r"sha256:[0-9a-f]{64}", item.get("digest") or ""):
                raise SystemExit("artifact lacks a SHA-256 digest")
        needed = sum(item["size_in_bytes"] for item in listing
                     if not (artifacts / f"{item['name']}-{item['id']}.zip").exists())
        free = ensure_space(artifacts, needed + 32 * 1024**2)
        (out / "download-space.json").write_text(json.dumps({
            "free_bytes": free, "new_compressed_bytes": needed,
            "scratch_bytes": 32 * 1024**2, "safety_margin_bytes": 2 * 1024**3,
            "mode": "original ZIP archives; no full extraction",
        }, indent=2) + "\n")
        records = []
        for item in sorted(listing, key=lambda a: a["name"]):
            path = artifacts / f"{item['name']}-{item['id']}.zip"
            expected = item["digest"].removeprefix("sha256:")
            if not path.exists():
                ensure_space(artifacts, item["size_in_bytes"] + 32 * 1024**2)
                partial = path.with_suffix(".zip.partial")
                # An earlier failed download is retained for diagnosis, never overwritten.
                if partial.exists():
                    raise SystemExit(f"partial download already exists: {partial}")
                with partial.open("xb") as handle:
                    result = subprocess.run(["gh", "api", f"repos/{REPO}/actions/artifacts/{item['id']}/zip"],
                                            stdout=handle, stderr=subprocess.PIPE)
                if result.returncode:
                    raise SystemExit(f"download of artifact {item['name']} failed: {result.stderr.decode()}")
                if digest_file(partial) != expected:
                    raise SystemExit(f"artifact digest mismatch (partial retained): {partial}")
                partial.rename(path)
            digest = digest_file(path)
            records.append({"id": item["id"], "name": item["name"], "size_in_bytes": item["size_in_bytes"],
                            "workflow_run": item.get("workflow_run"),
                            "created_at": item.get("created_at"), "expires_at": item.get("expires_at"),
                            "github_digest": item.get("digest"), "local_file": path.name,
                            "local_sha256": digest, "local_bytes": path.stat().st_size,
                            "verified": bool(expected) and digest == expected})
        (out / "artifacts.json").write_text(json.dumps(records, indent=2) + "\n")
        if not records or not all(r["verified"] for r in records):
            raise SystemExit("artifact digest mismatch or missing digest: see artifacts.json")
    else:
        raise SystemExit("use --zip-artifacts: full-tree extraction is not part of this continuation")
    print(json.dumps({"case": case, "run": args.run_id, "conclusion": run["conclusion"],
                      "jobs": {j["name"]: j["conclusion"] for j in run["jobs"]}, "out": str(out)},
                     indent=2))


if __name__ == "__main__":
    main()
